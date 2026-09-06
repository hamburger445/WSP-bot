# Docker Hosting

The Discord bot can run on any VPS or hosting provider that supports Docker and
Docker Compose. It does not expose a website or HTTP port.

1. Copy `.env` to the host and set the rotated Discord bot token.
2. Start the service:

```sh
docker compose up -d --build
```

Keep the `wsp-data` Docker volume or map it to persistent storage; it contains
the SQLite database, backups, logs, and transcripts.

Check the bot logs with:

```sh
docker compose logs -f wsp-bot
```
