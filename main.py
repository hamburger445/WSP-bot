"""Combined web service + Discord bot entrypoint.

Run this process as a web service (Cursor, Docker, or any host). FastAPI binds
HOST:PORT and starts the Discord bot in the same event loop so personnel data,
shifts, and the dashboard share one SQLite database.
"""

from __future__ import annotations

import asyncio
import logging
import signal

import discord
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
    try:
        await github_db.restore(settings.database_path)
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
    )
    server = uvicorn.Server(config)

    bot_task: asyncio.Task | None = None
    if settings.discord_token:
        log.info(
            "Discord token loaded (%s chars). Guild ID=%s. Starting bot…",
            len(settings.discord_token),
            settings.guild_id or "unset",
        )

        async def run_discord() -> None:
            try:
                await bot.start(settings.discord_token)
            except discord.LoginFailure:
                bot.last_error = "invalid DISCORD_TOKEN"
                log.exception("Discord rejected the bot token. Reset it in the Developer Portal and update the Render env var.")
            except discord.PrivilegedIntentsRequired:
                bot.last_error = "privileged intents"
                log.exception(
                    "Enable SERVER MEMBERS INTENT in Discord Developer Portal → Bot → Privileged Gateway Intents."
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                bot.last_error = str(exc)
                log.exception("Discord bot failed to start")

        bot_task = asyncio.create_task(run_discord(), name="wsp-discord")
    else:
        bot.last_error = "DISCORD_TOKEN is empty"
        log.warning("DISCORD_TOKEN is empty — web dashboard will run without the Discord bot.")

    async def shutdown() -> None:
        log.info("Shutting down")
        try:
            await db.backup()
        except Exception:
            log.exception("Shutdown backup failed")
        if github_db.enabled:
            try:
                await github_db.push(db)
            except Exception:
                log.exception("Shutdown GitHub database push failed")
        if bot.is_ready() or bot_task:
            await bot.close()
        if bot_task and not bot_task.done():
            bot_task.cancel()
        await db.close()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
        except NotImplementedError:
            # Windows
            pass

    try:
        await server.serve()
    finally:
        await shutdown()
        if bot_task:
            try:
                await bot_task
            except (asyncio.CancelledError, Exception):
                pass


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
