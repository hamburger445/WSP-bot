# Wisconsin State Patrol — Lakeville Roleplay

Production Discord bot and **Command Center** web dashboard for Wisconsin State Patrol (WSP) in Lakeville Roleplay (LVRP).

One process runs both:

* The Discord bot (`discord.py` 2.x slash commands, persistent buttons, department workflows)
* A FastAPI web service (health check + Discord-OAuth dashboard)

SQLite is the default database. Table shapes use portable SQL so you can move to PostgreSQL later without rewriting the schema.

---

## Installation

**Python 3.11 or newer** is required (3.12 recommended).

```powershell
cd "C:\Users\ivers\Downloads\bots\WSP bot"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

On macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

---

## Python requirements

See `requirements.txt`. Primary packages:

| Package | Role |
|---|---|
| `discord.py` 2.x | Slash commands, views, privileged intents |
| `aiosqlite` | Async SQLite |
| `python-dotenv` | `.env` secrets |
| `fastapi` + `uvicorn` | Web service + dashboard |
| `jinja2` | Dashboard templates |
| `httpx` | Discord OAuth |
| `tzdata` | Windows timezone support (`America/Chicago`) |

---

## Environment variables

Copy `.env.example` to `.env` and fill in values. **Never commit `.env`.**

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Bot token |
| `DISCORD_CLIENT_ID` | Application ID (OAuth + bot) |
| `DISCORD_CLIENT_SECRET` | OAuth2 secret (dashboard login) |
| `OWNER_IDS` | Comma-separated Discord user IDs with owner override |
| `GUILD_ID` | WSP Discord server ID |
| `DASHBOARD_BASE_URL` | Public URL of this service, no trailing slash |
| `DASHBOARD_SECRET_KEY` | Long random string for session cookies |
| `HOST` | Bind address (`0.0.0.0` for a web service) |
| `PORT` | HTTP port (default `8080`) |
| `DATABASE_PATH` | SQLite file (default `data/wsp.db`) |
| `TIMEZONE` | Default `America/Chicago` |
| `LOG_LEVEL` | `INFO` or `DEBUG` |

Department role/channel IDs are **not** hardcoded. Run `/setupserver` and paste each existing role, channel, and category ID when asked. The bot does not create Discord roles or channels during setup.

---

## Discord bot setup

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create an application.
2. **Bot** tab: create a bot, copy the token into `DISCORD_TOKEN`.
3. Enable privileged intents:
   * Server Members Intent
   * Message Content Intent
4. **OAuth2 → General**: copy Client ID and Client Secret.
5. **OAuth2 → Redirects**: add  
   `{DASHBOARD_BASE_URL}/auth/callback`  
   Local example: `http://127.0.0.1:8080/auth/callback`
6. Invite the bot with scopes `bot` and `applications.commands`, and permissions:
   * Manage Roles, Manage Channels, View Channels
   * Send Messages, Embed Links, Attach Files, Read Message History
   * Use Application Commands
   * Manage Messages (ticket close / claim)
7. Put the bot’s role **above** the WSP rank roles so it can assign them.

---

## Database setup

No separate database server is required.

On first start the bot creates `data/wsp.db` and all tables. Automatic backups are written to `data/backups/` every six hours and on shutdown (last 14 kept). Ticket transcripts go to `data/transcripts/`.

If `GITHUB_TOKEN` is set, the same database is also stored on GitHub on the **`data` branch** (`data/wsp.db`). That is where shift logs, `/setupserver` IDs, personnel, and the rest persist across Render deploys. The bot restores from GitHub on startup when the local file is empty, and pushes an update every 15 minutes, after setup changes, and on shutdown.

Create a token: GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens**. Grant this repo **Contents: Read and write**. Put the token in `GITHUB_TOKEN` locally and on Render. Leave it off `main` — snapshots go to the `data` branch so deploys are not retriggered.

To move to PostgreSQL later, keep the same table names/columns and swap the engine in `wsp/db.py`.

---

## Configuration

1. Start the service (see below).
2. In Discord, run **`/setupserver`** as an owner listed in `OWNER_IDS`.
   The bot asks one question at a time (for example: “Where do you want audit logs to go?”). Paste the Discord ID, then it saves and moves to the next.
3. Run **`/verifysetup`** to confirm every ID is set and exists in the server.
4. Tune values with **`/config`** (example: `/config path:quota.weekly_minutes value:180`).

Operational defaults live in `config/default.json` (ranks, quota minutes, probation length, fleet names, training modules). Per-guild overlays are stored in the database.

---

## Running the bot (and web dashboard)

This project is a **web service**. `main.py` binds `HOST:PORT` and starts the Discord bot in the same event loop.

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

Then open:

* Dashboard: http://127.0.0.1:8080
* Health: http://127.0.0.1:8080/health

### Run as a web service in Cursor

1. Put your token and IDs in `.env`.
2. Run `python main.py` in the Cursor terminal (or start it as the workspace web service on port **8080**).
3. When Cursor forwards the port, copy the public HTTPS URL into `DASHBOARD_BASE_URL` (no trailing slash).
4. Add `{DASHBOARD_BASE_URL}/auth/callback` as an OAuth redirect in the Developer Portal.
5. Restart once so Discord OAuth uses the public URL.

Sign into the dashboard with Discord. Access follows WSP roles (Trooper / Supervisor / HR / Command / Superintendent / Owner).

### Deploy on Render (website + Discord bot)

This app is already a single **Web Service**: `python main.py` binds Render’s `PORT` and starts the Discord bot in the same process.

1. Push this repo to GitHub (already configured as `https://github.com/starplatinumora66-design/WSP-bot.git`).
2. In [Render](https://dashboard.render.com), click **New → Blueprint** and select that repo, **or** **New → Web Service** and connect the repo.
3. If you create the service manually:
   * **Runtime:** Python
   * **Build command:** `pip install -r requirements.txt`
   * **Start command:** `python main.py`
   * **Health check path:** `/health`
4. Choose a **paid** instance (Starter or higher). Free web services sleep when idle and the Discord bot will go offline.
5. Add a **persistent disk** mounted at `/var/data` (1 GB is enough) so personnel records survive deploys. Set `DATABASE_PATH` to `/var/data/wsp.db`. The included `render.yaml` does this for Blueprint deploys.
6. Set environment variables (same names as `.env.example`):

   | Key | Value |
   |---|---|
   | `DISCORD_TOKEN` | Bot token |
   | `DISCORD_CLIENT_ID` | Application ID |
   | `DISCORD_CLIENT_SECRET` | OAuth2 secret |
   | `OWNER_IDS` | Your Discord user ID |
   | `GUILD_ID` | Server ID |
   | `DASHBOARD_SECRET_KEY` | Long random string (Blueprint can auto-generate this) |
   | `DASHBOARD_BASE_URL` | `https://<your-service>.onrender.com` (optional; Render’s public URL is used if empty) |
   | `HOST` | `0.0.0.0` |
   | `DATABASE_PATH` | `/var/data/wsp.db` |
   | `TIMEZONE` | `America/Chicago` |
   | `GITHUB_TOKEN` | Fine-grained PAT with **Contents: Read and write** on this repo |
   | `GITHUB_REPO` | `hamburger445/WSP-bot` (optional if git origin is set) |

7. After the first deploy, copy the `onrender.com` URL. In the Discord Developer Portal → OAuth2 → Redirects, add:  
   `https://<your-service>.onrender.com/auth/callback`
8. Open that URL in a browser. You should see the Command Center login. The bot should show as online in Discord.

`PORT` is set automatically by Render. Do not pin it in the dashboard.

### Docker

```powershell
docker build -t wsp-lvrp .
docker run --env-file .env -p 8080:8080 wsp-lvrp
```

---

## Updating the bot

```powershell
git pull
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

After new slash commands, run `/sync` as an owner (or restart; `setup_hook` syncs on boot).

SQLite files in `data/` are left in place across updates.

---

## Feature map

| Area | Commands / UI |
|---|---|
| Setup | `/setupserver` `/verifysetup` `/config` `/resetserver` `/sync` |
| Personnel | `/personnel add` `note` `transfer` `suspend` `remove` `reinstate` `history` |
| Profile | `/profile` with section dropdown |
| Training | `/training set` `/training view` |
| Fast-pass | `/fastpass start` `review` `approve` `deny` |
| Supervision | `/supervision start` `complete` `review` `history` |
| Probation | `/probation start` `view` `review` `extend` `complete` `clear` |
| Shifts | `/shift menu` `status` `leaderboard` `history` `correct` `start` |
| Quota | `/quota view` `leaderboard` `admin` |
| LOA | `/loa menu` `request` `approve` `deny` `active` |
| Rank | `/promote` `/demote` (updates Discord roles) |
| Discipline | `/discipline add` `view` `remove` |
| Tickets | `/ticket panel` `close` `list` + dropdown panel |
| Vehicles | `/vehicle assign` `release` `list` |
| Command | `/dashboard` `/audit` |

Active shifts and open tickets survive restarts. Quota missed/approaching and probation-ending notices go to configured channels. Members are **never auto-punished** for missed quota; HR is notified instead. Approved LOA blocks missed-quota flags for that window. Fast-pass “Training Waived” sets supervision required. A passing supervision session starts probation automatically.

Permission levels (Discord roles **and** rank mapping): Trooper → Supervisor → HR → Command → Superintendent → Owner. Discord Administrator is not enough by itself.

---

## Troubleshooting

**Slash commands missing**  
Invite with `applications.commands`. Set `GUILD_ID`. Run `/sync`. Guild sync can take a minute.

**`Missing Access` / cannot assign roles**  
Raise the bot role above WSP rank roles. Grant Manage Roles.

**Dashboard login fails**  
Check `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DASHBOARD_BASE_URL`, and the OAuth redirect. `error=guild` means the account is not in the WSP server.

**Bot online, dashboard empty**  
`GUILD_ID` must match the server. Run `/setupserver`, then `/personnel add`.

**Privileged intents error**  
Enable Server Members and Message Content in the Developer Portal.

**Bot keeps saying shutting down / goes offline**  
The process logs a stop message only when the host sends SIGTERM (deploy, sleep, or a failed health check). The HTTP server now binds before a slow GitHub restore can time out, and it pings `/health` every 8 minutes so Render does not idle-sleep. Use a **paid** instance for true 24/7. Set `GITHUB_TOKEN` and `DASHBOARD_BASE_URL` on Render. Check `/health` — if `ok` is true the web service is up even while Discord reconnects.

**Database locked / lost data**  
Stop extra `python main.py` processes. Restore from `data/backups/`. Do not delete `data/wsp.db` unless you intend a full reset.

**`/resetserver`**  
Clears stored configuration. It does **not** wipe personnel records. Discord channels/roles are deleted only if you pass `wipe_discord: true` and confirm.

Logs: `data/logs/wsp.log`.
