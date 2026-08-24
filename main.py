"""Combined web service + Discord bot entrypoint.

Run this process as a web service (Cursor, Docker, or any host). FastAPI binds
HOST:PORT and starts the Discord bot in the same event loop so personnel data,
shifts, and the dashboard share one SQLite database.
"""

from __future__ import annotations

import asyncio
import logging
import signal

import uvicorn

from wsp.bot import WSPBot
from wsp.config import Settings
from wsp.db import Database
from wsp.logging_setup import setup_logging
from wsp.web.app import create_app

log = logging.getLogger("wsp")


async def run() -> None:
    settings = Settings()
    settings.ensure_directories()
    setup_logging(settings.log_level, settings.logs_dir)

    db = Database(settings.database_path, settings.backups_dir)
    await db.connect()
    bot = WSPBot(settings, db)
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
        bot_task = asyncio.create_task(bot.start(settings.discord_token), name="wsp-discord")
    else:
        log.warning("DISCORD_TOKEN is empty — web dashboard will run without the Discord bot.")

    async def shutdown() -> None:
        log.info("Shutting down")
        try:
            await db.backup()
        except Exception:
            log.exception("Shutdown backup failed")
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
