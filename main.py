"""Combined web service + Discord bot entrypoint.

Run this process as a web service (Cursor, Docker, or any host). FastAPI binds
HOST:PORT and starts the Discord bot in the same event loop so personnel data,
shifts, and the dashboard share one SQLite database.
"""

from __future__ import annotations

import asyncio
import logging

import discord
import httpx
import uvicorn

from wsp.bot import WSPBot
from wsp.config import Settings
from wsp.db import Database
from wsp.github_db import GitHubDatabase
from wsp.logging_setup import setup_logging
from wsp.web.app import create_app

log = logging.getLogger("wsp")


async def run() -> None:
    settings = Settings()
    settings.ensure_directories()
    setup_logging(settings.log_level, settings.logs_dir)

    github_db = GitHubDatabase(settings)
    if github_db.enabled:
        log.info(
            "GitHub database sync enabled for %s on branch %s",
            settings.github_repo,
            settings.github_db_branch,
        )
    # Restore must finish before db.connect() so an empty disk can load GitHub
    # data, but it cannot block the HTTP port. Render SIGTERMs a process that
    # has not bound PORT yet — that was logging "Shutting down" in a loop.
    try:
        await asyncio.wait_for(github_db.restore(settings.database_path), timeout=12)
    except asyncio.TimeoutError:
        log.warning("GitHub restore timed out; starting with the local database")
    except Exception:
        log.exception("Could not restore the database from GitHub; starting with the local file")

    db = Database(settings.database_path, settings.backups_dir)
    await db.connect()
    bot = WSPBot(settings, db)
    bot.github_db = github_db
    app = create_app(bot, db, settings)

    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        lifespan="off",
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=75,
        timeout_graceful_shutdown=25,
    )
    server = uvicorn.Server(config)
    stop = asyncio.Event()
    shutting_down = False

    async def shutdown() -> None:
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        stop.set()
        server.should_exit = True
        log.info("Stop signal received — saving the database and closing Discord")
        try:
            await db.backup()
        except Exception:
            log.exception("Shutdown backup failed")
        if github_db.enabled:
            try:
                await github_db.push(db)
            except Exception:
                log.exception("Shutdown GitHub database push failed")
        try:
            if not bot.is_closed():
                await bot.close()
        except Exception:
            log.exception("Error while closing the Discord client")
        if bot_task and not bot_task.done():
            bot_task.cancel()
        if keep_task and not keep_task.done():
            keep_task.cancel()
        await db.close()

    async def run_discord() -> None:
        backoff = 5
        while not stop.is_set() and not server.should_exit:
            try:
                await bot.start(settings.discord_token, reconnect=True)
                if not stop.is_set():
                    log.warning("Discord session ended; the web service stays online")
                return
            except discord.LoginFailure:
                bot.last_error = "invalid DISCORD_TOKEN"
                log.exception(
                    "Discord rejected the bot token. Reset it in the Developer Portal and update the Render env var."
                )
                return
            except discord.PrivilegedIntentsRequired:
                bot.last_error = "privileged intents"
                log.exception(
                    "Enable SERVER MEMBERS INTENT in Discord Developer Portal → Bot → Privileged Gateway Intents."
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                bot.last_error = str(exc)
                log.exception("Discord gateway error — retrying in %ss (web stays up)", backoff)
            if stop.is_set() or server.should_exit or bot.is_closed():
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def keep_alive() -> None:
        url = settings.keep_alive_origin()
        if not url:
            log.info("Keep-alive ping skipped (no public URL). Set DASHBOARD_BASE_URL on Render for 24/7.")
            return
        await asyncio.sleep(20)
        log.info("Keep-alive pinging %s/health every 8 minutes so the host does not idle-sleep", url)
        while not stop.is_set() and not server.should_exit:
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                    response = await client.get(f"{url}/health")
                if response.status_code >= 400:
                    log.warning("Keep-alive ping returned HTTP %s", response.status_code)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Keep-alive ping failed; will retry")
            try:
                await asyncio.wait_for(stop.wait(), timeout=480)
            except asyncio.TimeoutError:
                pass

    bot_task: asyncio.Task | None = None
    keep_task = asyncio.create_task(keep_alive(), name="wsp-keepalive")
    if settings.discord_token:
        log.info(
            "Discord token loaded (%s chars). Guild ID=%s. Starting bot…",
            len(settings.discord_token),
            settings.guild_id or "unset",
        )
        bot_task = asyncio.create_task(run_discord(), name="wsp-discord")
    else:
        bot.last_error = "DISCORD_TOKEN is empty"
        log.warning("DISCORD_TOKEN is empty — web dashboard will run without the Discord bot.")

    try:
        await server.serve()
    finally:
        await shutdown()
        for task in (bot_task, keep_task):
            if not task:
                continue
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
