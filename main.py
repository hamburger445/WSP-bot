"""Discord bot entrypoint."""

from __future__ import annotations

import asyncio
import logging

import discord

from wsp.bot import WSPBot
from wsp.config import Settings
from wsp.db import Database
from wsp.github_db import GitHubDatabase
from wsp.logging_setup import setup_logging

log = logging.getLogger("wsp")


async def run() -> None:
    settings = Settings()
    settings.ensure_directories()
    setup_logging(settings.log_level, settings.logs_dir)

    db = Database(settings.database_path, settings.backups_dir)
    github_db = GitHubDatabase.from_settings(settings)
    if github_db is not None:
        await github_db.restore(settings.database_path)
    await db.connect()
    if github_db is not None:
        github_db.bind(db)
    bot = WSPBot(settings, db)
    stop = asyncio.Event()
    shutting_down = False
    bot_task: asyncio.Task | None = None

    async def shutdown() -> None:
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        stop.set()
        log.info("Stop signal received — saving the database and closing Discord")
        try:
            if github_db is not None:
                await github_db.flush()
        except Exception:
            log.exception("GitHub database flush failed")
        try:
            await db.backup()
        except Exception:
            log.exception("Shutdown backup failed")
        try:
            if not bot.is_closed():
                await bot.close()
        except Exception:
            log.exception("Error while closing the Discord client")
        await db.close()

    async def run_discord() -> None:
        backoff = 5
        while not stop.is_set():
            try:
                await bot.start(settings.discord_token, reconnect=True)
                if not stop.is_set():
                    log.warning("Discord session ended; retrying")
                return
            except discord.LoginFailure:
                bot.last_error = "invalid DISCORD_TOKEN"
                log.exception(
                    "Discord rejected the bot token. Reset it in the Developer Portal and update DISCORD_TOKEN."
                )
                return
            except discord.PrivilegedIntentsRequired:
                bot.last_error = "privileged intents"
                log.exception(
                    "Enable SERVER MEMBERS INTENT in Discord Developer Portal → Bot → Privileged Gateway Intents."
                )
                return
            except discord.HTTPException as exc:
                bot.last_error = "Discord gateway temporarily rate limited or blocked"
                try:
                    await bot.http.close()
                    bot.http.clear()
                except Exception:
                    log.exception("Could not close the Discord HTTP session after gateway failure")
                # Cloudflare 1015 blocks the host IP, so repeated retries only
                # extend the block. Give the gateway a long cooling-off window.
                backoff = max(backoff, 900)
                log.warning("Discord gateway HTTP %s — retrying in %ss", exc.status, backoff)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                bot.last_error = str(exc)
                log.exception("Discord gateway error — retrying in %ss (web stays up)", backoff)
            if stop.is_set() or bot.is_closed():
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 900)

    if settings.discord_token:
        log.info(
            "Discord token loaded (%s chars). Guild ID=%s. Starting bot…",
            len(settings.discord_token),
            settings.guild_id or "unset",
        )
        bot_task = asyncio.create_task(run_discord(), name="wsp-discord")
    else:
        log.error("DISCORD_TOKEN is empty — the bot cannot start.")
        await db.close()
        return

    try:
        await bot_task
    finally:
        stop.set()
        await shutdown()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
