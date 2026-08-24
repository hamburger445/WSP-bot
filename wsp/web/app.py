"""FastAPI command-center dashboard sharing the bot database."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from wsp.constants import LEVEL_LABELS, PermissionLevel
from wsp.db import now_ts
from wsp.embeds import format_duration
from wsp.web import auth

if TYPE_CHECKING:
    from wsp.bot import WSPBot
    from wsp.config import Settings
    from wsp.db import Database

log = logging.getLogger("wsp.web")
WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _fmt_dt(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_dur(seconds: int | None) -> str:
    return format_duration(seconds)


templates.env.filters["when"] = _fmt_dt
templates.env.filters["duration"] = _fmt_dur


def create_app(bot: WSPBot, db: Database, settings: Settings) -> FastAPI:
    app = FastAPI(title="WSP Command Center", docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.dashboard_secret,
        same_site="lax",
        https_only=settings.dashboard_base_url.startswith("https://"),
        max_age=60 * 60 * 12,
    )
    static = WEB_DIR / "static"
    static.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static)), name="static")

    def guild_id() -> int:
        return settings.guild_id or (bot.guilds[0].id if bot.guilds else 0)

    def current_user(request: Request) -> dict[str, Any] | None:
        return auth.session_user(request)

    def redirect_uri() -> str:
        return f"{settings.dashboard_base_url}/auth/callback"

    def ctx(request: Request, **extra: Any) -> dict[str, Any]:
        user = current_user(request)
        payload = {
            "request": request,
            "user": user,
            "level_label": LEVEL_LABELS.get(PermissionLevel(int(user["level"])), "Trooper") if user else None,
            "department": "Wisconsin State Patrol",
            "community": "Lakeville Roleplay",
            "bot_ready": bot.is_ready(),
            "nav": NAV,
        }
        payload.update(extra)
        return payload

    def render(name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
        return templates.TemplateResponse(
            context["request"], name, context, status_code=status_code
        )

    def gated(request: Request, minimum: PermissionLevel) -> RedirectResponse | None:
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if int(user.get("level", 0)) < int(minimum):
            return RedirectResponse("/?denied=1", status_code=302)
        return None

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "service": "wsp-command-center",
                "bot": bot.is_ready(),
                "user": str(bot.user) if bot.user else None,
                "guilds": len(bot.guilds),
            }
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Any:
        if current_user(request):
            return RedirectResponse("/", status_code=302)
        ready = bool(settings.discord_client_id and settings.discord_client_secret)
        return render("login.html", ctx(request, oauth_ready=ready, error=request.query_params.get("error")))

    @app.get("/auth/discord")
    async def auth_discord(request: Request) -> Any:
        if not settings.discord_client_id or not settings.discord_client_secret:
            return RedirectResponse("/login?error=oauth", status_code=302)
        state = secrets.token_urlsafe(16)
        request.session["oauth_state"] = state
        return RedirectResponse(auth.login_url(settings.discord_client_id, redirect_uri(), state), status_code=302)

    @app.get("/auth/callback")
    async def auth_callback(request: Request, code: str | None = None, state: str | None = None) -> Any:
        if not code or state != request.session.get("oauth_state"):
            return RedirectResponse("/login?error=state", status_code=302)
        try:
            token = await auth.exchange_code(
                settings.discord_client_id, settings.discord_client_secret, redirect_uri(), code
            )
            profile = await auth.fetch_user(token["access_token"])
        except Exception:
            log.exception("OAuth exchange failed")
            return RedirectResponse("/login?error=oauth", status_code=302)

        user_id = int(profile["id"])
        gid = guild_id()
        level = PermissionLevel.TROOPER
        if user_id in settings.owner_ids:
            level = PermissionLevel.OWNER
        elif gid:
            try:
                member = await auth.fetch_member(token["access_token"], gid, user_id)
            except Exception:
                member = None
            if member is None and user_id not in settings.owner_ids:
                return RedirectResponse("/login?error=guild", status_code=302)
            if member and level < PermissionLevel.OWNER:
                cfg = await bot.guild_config(gid)
                role_ids = {int(r) for r in member.get("roles", []) if str(r).isdigit()}
                mapping = [
                    (cfg.role_id("superintendent"), PermissionLevel.SUPERINTENDENT),
                    (cfg.role_id("command"), PermissionLevel.COMMAND),
                    (cfg.role_id("hr"), PermissionLevel.HR),
                    (cfg.role_id("supervisor"), PermissionLevel.SUPERVISOR),
                ]
                for rid, lv in mapping:
                    if rid and rid in role_ids:
                        level = lv
                        break
                record = await db.get_personnel(gid, user_id)
                if record and record["rank_level"]:
                    rank_lv = PermissionLevel(int(record["rank_level"]))
                    if rank_lv > level:
                        level = rank_lv

        request.session.pop("oauth_state", None)
        request.session["user"] = {
            "id": user_id,
            "username": profile.get("global_name") or profile.get("username"),
            "avatar": profile.get("avatar"),
            "level": int(level),
        }
        return RedirectResponse("/", status_code=302)

    @app.get("/logout")
    async def logout(request: Request) -> Any:
        request.session.clear()
        return RedirectResponse("/login", status_code=302)

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> Any:
        bounce = gated(request, PermissionLevel.TROOPER)
        if bounce:
            return bounce
        gid = guild_id()
        counts = await db.dashboard_counts(gid) if gid else {
            "active_personnel": 0, "active_shifts": 0, "loa": 0, "probation": 0, "awaiting_supervision": 0
        }
        cfg = await bot.guild_config(gid) if gid else None
        week_id = None
        quota_rows = []
        if gid and cfg:
            week_id = await db.ensure_week(gid, db.week_start_ts(cfg.get("timezone") or "America/Chicago"))
            quota_rows = [r for r in await db.list_quota_records(week_id) if r["quota_type"] == "duty"]
        complete = sum(
            1 for r in quota_rows if r["status"] == "complete" or int(r["completed_minutes"]) >= int(r["required_minutes"] or 1)
        )
        shifts = await db.list_active_shifts(gid) if gid else []
        promotions = await db.list_audit(gid, 6, "promotion") if gid else []
        discipline = (await db.list_discipline(gid))[:6] if gid else []
        return render(
            "overview.html",
            ctx(
                request,
                page="overview",
                counts=counts,
                quota_complete=complete,
                quota_total=len(quota_rows),
                shifts=shifts,
                promotions=promotions,
                discipline=discipline,
                denied=request.query_params.get("denied"),
            ),
        )

    @app.get("/personnel", response_class=HTMLResponse)
    async def personnel_list(request: Request) -> Any:
        bounce = gated(request, PermissionLevel.HR)
        if bounce:
            return bounce
        gid = guild_id()
        rows = await db.list_personnel(gid, None) if gid else []
        return render("personnel.html", ctx(request, page="personnel", rows=rows))

    @app.get("/personnel/{discord_id}", response_class=HTMLResponse)
    async def personnel_detail(request: Request, discord_id: int) -> Any:
        bounce = gated(request, PermissionLevel.HR)
        if bounce:
            return bounce
        gid = guild_id()
        record = await db.get_personnel(gid, discord_id)
        if record is None:
            return render("notfound.html", ctx(request, page="personnel"), status_code=404)
        history = await db.rank_history(record["id"])
        notes = await db.list_notes(record["id"])
        training = await db.list_training(gid, discord_id)
        discipline = await db.list_discipline(gid, discord_id)
        activity = await db.activity_history(gid, discord_id)
        vehicles = await db.list_vehicles(gid, discord_id)
        return render(
            "personnel_detail.html",
            ctx(
                request,
                page="personnel",
                record=record,
                history=history,
                notes=notes,
                training=training,
                discipline=discipline,
                activity=activity,
                vehicles=vehicles,
            ),
        )

    @app.get("/shifts", response_class=HTMLResponse)
    async def shifts_page(request: Request) -> Any:
        bounce = gated(request, PermissionLevel.SUPERVISOR)
        if bounce:
            return bounce
        gid = guild_id()
        active = await db.list_active_shifts(gid) if gid else []
        recent = await db.list_shifts(gid, None, 40) if gid else []
        board = await db.shift_leaderboard(gid) if gid else []
        return render("shifts.html", ctx(request, page="shifts", active=active, recent=recent, board=board))

    @app.get("/quota", response_class=HTMLResponse)
    async def quota_page(request: Request) -> Any:
        bounce = gated(request, PermissionLevel.HR)
        if bounce:
            return bounce
        gid = guild_id()
        cfg = await bot.guild_config(gid) if gid else None
        rows: list[Any] = []
        if gid and cfg:
            week_id = await db.ensure_week(gid, db.week_start_ts(cfg.get("timezone") or "America/Chicago"))
            rows = await db.list_quota_records(week_id)
        return render("quota.html", ctx(request, page="quota", rows=rows))

    @app.get("/loa", response_class=HTMLResponse)
    async def loa_page(request: Request) -> Any:
        bounce = gated(request, PermissionLevel.HR)
        if bounce:
            return bounce
        gid = guild_id()
        rows = await db.list_loa(gid) if gid else []
        now = now_ts()
        return render("loa.html", ctx(request, page="loa", rows=rows, now=now))

    @app.get("/discipline", response_class=HTMLResponse)
    async def discipline_page(request: Request) -> Any:
        bounce = gated(request, PermissionLevel.HR)
        if bounce:
            return bounce
        gid = guild_id()
        rows = await db.list_discipline(gid) if gid else []
        return render("discipline.html", ctx(request, page="discipline", rows=rows))

    @app.get("/tickets", response_class=HTMLResponse)
    async def tickets_page(request: Request) -> Any:
        bounce = gated(request, PermissionLevel.HR)
        if bounce:
            return bounce
        gid = guild_id()
        rows = await db.list_tickets(gid) if gid else []
        return render("tickets.html", ctx(request, page="tickets", rows=rows))

    @app.get("/fastpass", response_class=HTMLResponse)
    async def fastpass_page(request: Request) -> Any:
        bounce = gated(request, PermissionLevel.HR)
        if bounce:
            return bounce
        gid = guild_id()
        rows = await db.list_fastpass(gid) if gid else []
        return render("fastpass.html", ctx(request, page="fastpass", rows=rows))

    @app.get("/supervision", response_class=HTMLResponse)
    async def supervision_page(request: Request) -> Any:
        bounce = gated(request, PermissionLevel.SUPERVISOR)
        if bounce:
            return bounce
        gid = guild_id()
        rows = await db.list_supervisions(gid) if gid else []
        return render("supervision.html", ctx(request, page="supervision", rows=rows))

    @app.get("/probation", response_class=HTMLResponse)
    async def probation_page(request: Request) -> Any:
        bounce = gated(request, PermissionLevel.HR)
        if bounce:
            return bounce
        gid = guild_id()
        rows = await db.list_active_probations(gid) if gid else []
        return render("probation.html", ctx(request, page="probation", rows=rows))

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request) -> Any:
        bounce = gated(request, PermissionLevel.HR)
        if bounce:
            return bounce
        gid = guild_id()
        rows = await db.list_audit(gid, 100) if gid else []
        return render("audit.html", ctx(request, page="audit", rows=rows))

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> Any:
        bounce = gated(request, PermissionLevel.SUPERINTENDENT)
        if bounce:
            return bounce
        gid = guild_id()
        cfg = await bot.guild_config(gid) if gid else None
        missing = cfg.missing_required() if cfg else ["guild not configured"]
        return render("settings.html", ctx(request, page="settings", cfg=cfg.raw if cfg else {}, missing=missing))

    return app


NAV = [
    ("overview", "/", "Overview"),
    ("personnel", "/personnel", "Personnel"),
    ("shifts", "/shifts", "Shifts"),
    ("quota", "/quota", "Quota"),
    ("loa", "/loa", "LOA"),
    ("supervision", "/supervision", "Supervision"),
    ("probation", "/probation", "Probation"),
    ("fastpass", "/fastpass", "Fast-pass"),
    ("discipline", "/discipline", "Discipline"),
    ("tickets", "/tickets", "Tickets"),
    ("audit", "/audit", "Audit"),
    ("settings", "/settings", "Settings"),
]
