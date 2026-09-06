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

## Panel Startup Command

If `startup.sh` has been uploaded to `/home/container`, set the startup command
to this single line:

```sh
/bin/sh /home/container/startup.sh
```

If the panel does not have that file yet, use this command instead. It downloads
the script from GitHub first, so it does not require a local startup file:

```sh
curl -fsSL https://raw.githubusercontent.com/hamburger445/WSP-bot/main/startup.sh | /bin/sh
```

Set `REPO_URL`, `REPO_BRANCH`, and `PYTHON_BIN` as environment variables when
needed. For a private repository, configure the panel's Git credentials rather
than putting a token in the startup command.
