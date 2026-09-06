# Generic Docker Hosting

This service can run on any VPS or hosting provider that supports Docker and
Docker Compose. It does not require Render.

1. Copy `.env` to the host and set the rotated Discord and GitHub credentials.
2. Set `DASHBOARD_BASE_URL` to the public HTTPS URL.
3. Add `https://your-domain.example/auth/callback` as a Discord OAuth2 redirect.
4. Start the service:

```sh
docker compose up -d --build
```

The dashboard listens on port `8080`. Keep the `wsp-data` Docker volume or map
it to persistent storage; it contains the SQLite database and backups.

Check the service with:

```sh
curl http://127.0.0.1:8080/health
docker compose logs -f wsp-bot
```

For a reverse proxy, forward HTTPS traffic to `127.0.0.1:8080` and preserve
the `Host` and `X-Forwarded-Proto` headers.