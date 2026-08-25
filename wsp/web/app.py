"""FastAPI command center — owner-only controls for the live bot."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import discord
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from wsp.constants import LEVEL_LABELS, PermissionLevel
from wsp.db import now_ts
from wsp.embeds import format_duration
from wsp.ops import change_rank, decide_loa_record, end_active_shift, fire_member, reset_shift_data, set_status
from wsp.utils import ensure_personnel, sync_rank_roles
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


class _Actor:
    def __init__(self, user: dict[str, Any]) -> None:
        self.id = int(user["id"])
        self.name = str(user.get("username") or self.id)
        self.mention = f"<@{self.id}>"

    def __str__(self) -> str:
        return self.name


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

    def guild() -> discord.Guild | None:
        gid = guild_id()
        return bot.get_guild(gid) if gid else None

    def current_user(request: Request) -> dict[str, Any] | None:
        return auth.session_user(request)

    def is_owner(user: dict[str, Any] | None) -> bool:
        if not user:
            return False
        return int(user.get("id", 0)) in settings.owner_ids or int(user.get("level", 0)) >= int(PermissionLevel.OWNER)

    def public_origin(request: Request) -> str:
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
        host = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or request.url.netloc
        )
        host = (host or "").split(",")[0].strip()
        if host:
            return f"{proto}://{host}".rstrip("/")
        return settings.dashboard_base_url.rstrip("/")

    def redirect_uri(request: Request) -> str:
        return f"{public_origin(request)}/auth/callback"

    def ctx(request: Request, **extra: Any) -> dict[str, Any]:
        user = current_user(request)
        payload = {
            "request": request,
            "user": user,
            "level_label": LEVEL_LABELS.get(PermissionLevel(int(user["level"])), "Owner") if user else None,
            "department": "Wisconsin State Patrol",
            "community": "Lakeville Roleplay",
            "bot_ready": bot.is_ready(),
            "nav": NAV,
            "nav_groups": NAV_GROUPS,
            "notice": request.query_params.get("ok"),
            "error": request.query_params.get("err"),
            "denied": request.query_params.get("denied"),
        }
        payload.update(extra)
        return payload

    def render(name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
        return templates.TemplateResponse(context["request"], name, context, status_code=status_code)

    def gated(request: Request) -> RedirectResponse | None:
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if not is_owner(user):
            request.session.clear()
            return RedirectResponse("/login?error=owner", status_code=302)
        return None

    def bounce_to(path: str, ok: str | None = None, err: str | None = None) -> RedirectResponse:
        if ok:
            return RedirectResponse(f"{path}?ok={quote(ok)}", status_code=303)
        if err:
            return RedirectResponse(f"{path}?err={quote(err)}", status_code=303)
        return RedirectResponse(path, status_code=303)

    async def member_for(discord_id: int) -> discord.Member | None:
        g = guild()
        if g is None:
            return None
        found = await bot.fetch_guild_user(g, discord_id)
        return found if isinstance(found, discord.Member) else None

    @app.api_route("/health", methods=["GET", "HEAD"], response_model=None)
    async def health(request: Request) -> Any:
        if request.method == "HEAD":
            return Response(status_code=200)
        return JSONResponse(
            {
                "ok": True,
                "service": "wsp-command-center",
                "bot": bot.is_ready(),
                "user": str(bot.user) if bot.user else None,
                "guilds": len(bot.guilds),
                "token_configured": bool(settings.discord_token),
                "guild_id": settings.guild_id or None,
                "commands": getattr(bot, "synced_commands", []),
                "bot_error": getattr(bot, "last_error", None),
            }
        )

    @app.api_route("/", methods=["HEAD"], response_model=None)
    async def root_head() -> Any:
        return Response(status_code=200)

    @app.get("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Any:
        if current_user(request) and is_owner(current_user(request)):
            return RedirectResponse("/", status_code=302)
        ready = bool(settings.discord_client_id and settings.discord_client_secret)
        return render(
            "login.html",
            ctx(
                request,
                oauth_ready=ready,
                error=request.query_params.get("error"),
                oauth_redirect=redirect_uri(request),
            ),
        )

    @app.get("/auth/discord")
    async def auth_discord(request: Request) -> Any:
        if not settings.discord_client_id or not settings.discord_client_secret:
            return RedirectResponse("/login?error=oauth", status_code=302)
        state = secrets.token_urlsafe(16)
        callback = redirect_uri(request)
        request.session["oauth_state"] = state
        request.session["oauth_redirect"] = callback
        return RedirectResponse(auth.login_url(settings.discord_client_id, callback, state), status_code=302)

    @app.get("/auth/callback")
    async def auth_callback(request: Request, code: str | None = None, state: str | None = None) -> Any:
        if not code or state != request.session.get("oauth_state"):
            return RedirectResponse("/login?error=state", status_code=302)
        try:
            token = await auth.exchange_code(
                settings.discord_client_id,
                settings.discord_client_secret,
                request.session.get("oauth_redirect") or redirect_uri(request),
                code,
            )
            profile = await auth.fetch_user(token["access_token"])
        except Exception:
            log.exception("OAuth exchange failed")
            return RedirectResponse("/login?error=oauth", status_code=302)

        user_id = int(profile["id"])
        if user_id not in settings.owner_ids:
            return RedirectResponse("/login?error=owner", status_code=302)

        request.session.pop("oauth_state", None)
        request.session["user"] = {
            "id": user_id,
            "username": profile.get("global_name") or profile.get("username"),
            "avatar": profile.get("avatar"),
            "level": int(PermissionLevel.OWNER),
        }
        return RedirectResponse("/", status_code=302)

    @app.get("/logout")
    async def logout(request: Request) -> Any:
        request.session.clear()
        return RedirectResponse("/login", status_code=302)

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> Any:
        bounce = gated(request)
        if bounce:
            return bounce
        gid = guild_id()
        counts = await db.dashboard_counts(gid) if gid else {
            "active_personnel": 0, "active_shifts": 0, "loa": 0, "pending_loa": 0
        }
        cfg = await bot.guild_config(gid) if gid else None
        quota_rows: list[Any] = []
        if gid and cfg:
            week_id = await db.ensure_week(gid, db.week_start_ts(cfg.get("timezone") or "America/Chicago"))
            quota_rows = [r for r in await db.list_quota_records(week_id) if r["quota_type"] == "duty"]
        complete = sum(
            1 for r in quota_rows if r["status"] == "complete" or int(r["completed_minutes"]) >= int(r["required_minutes"] or 1)
        )
        shifts = await db.list_active_shifts(gid) if gid else []
        promotions = await db.list_audit(gid, 6, "promotion") if gid else []
        ranks = await db.list_ranks(gid) if gid else []
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
                ranks=ranks,
            ),
        )

    @app.get("/personnel", response_class=HTMLResponse)
    async def personnel_list(request: Request) -> Any:
        bounce = gated(request)
        if bounce:
            return bounce
        gid = guild_id()
        rows = await db.list_personnel(gid, None) if gid else []
        ranks = await db.list_ranks(gid) if gid else []
        return render("personnel.html", ctx(request, page="personnel", rows=rows, ranks=ranks))

    @app.get("/personnel/{discord_id}", response_class=HTMLResponse)
    async def personnel_detail(request: Request, discord_id: int) -> Any:
        bounce = gated(request)
        if bounce:
            return bounce
        gid = guild_id()
        record = await db.get_personnel(gid, discord_id)
        if record is None:
            return render("notfound.html", ctx(request, page="personnel"), status_code=404)
        history = await db.rank_history(record["id"])
        notes = await db.list_notes(record["id"])
        activity = await db.activity_history(gid, discord_id)
        ranks = await db.list_ranks(gid)
        return render(
            "personnel_detail.html",
            ctx(request, page="personnel", record=record, history=history, notes=notes, activity=activity, ranks=ranks),
        )

    @app.get("/shifts", response_class=HTMLResponse)
    async def shifts_page(request: Request) -> Any:
        bounce = gated(request)
        if bounce:
            return bounce
        gid = guild_id()
        active = await db.list_active_shifts(gid) if gid else []
        recent = await db.list_shifts(gid, None, 40) if gid else []
        board = await db.shift_leaderboard(gid) if gid else []
        return render("shifts.html", ctx(request, page="shifts", active=active, recent=recent, board=board))

    @app.get("/quota", response_class=HTMLResponse)
    async def quota_page(request: Request) -> Any:
        bounce = gated(request)
        if bounce:
            return bounce
        gid = guild_id()
        cfg = await bot.guild_config(gid) if gid else None
        rows: list[Any] = []
        low, middle, high = 90, 75, 30
        if gid and cfg:
            week_id = await db.ensure_week(gid, db.week_start_ts(cfg.get("timezone") or "America/Chicago"))
            rows = [r for r in await db.list_quota_records(week_id) if r["quota_type"] == "duty"]
            low = int(cfg.get("quota", "low_minutes") or 90)
            middle = int(cfg.get("quota", "middle_minutes") or 75)
            high = int(cfg.get("quota", "high_minutes") or 30)
        return render("quota.html", ctx(request, page="quota", rows=rows, low=low, middle=middle, high=high))

    @app.get("/loa", response_class=HTMLResponse)
    async def loa_page(request: Request) -> Any:
        bounce = gated(request)
        if bounce:
            return bounce
        gid = guild_id()
        rows = await db.list_loa(gid) if gid else []
        return render("loa.html", ctx(request, page="loa", rows=rows, now=now_ts()))

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request) -> Any:
        bounce = gated(request)
        if bounce:
            return bounce
        gid = guild_id()
        rows = await db.list_audit(gid, 100) if gid else []
        return render("audit.html", ctx(request, page="audit", rows=rows))

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> Any:
        bounce = gated(request)
        if bounce:
            return bounce
        gid = guild_id()
        cfg = await bot.guild_config(gid) if gid else None
        missing = cfg.missing_required() if cfg else ["guild not configured"]
        return render("settings.html", ctx(request, page="settings", cfg=cfg.raw if cfg else {}, missing=missing))

    @app.post("/actions")
    async def actions(
        request: Request,
        action: str = Form(...),
        discord_id: str | None = Form(None),
        reason: str | None = Form(None),
        rank: str | None = Form(None),
        position: str | None = Form(None),
        callsign: str | None = Form(None),
        note_type: str | None = Form(None),
        content: str | None = Form(None),
        loa_id: int | None = Form(None),
        weekly_minutes: int | None = Form(None),
        low_minutes: int | None = Form(None),
        middle_minutes: int | None = Form(None),
        high_minutes: int | None = Form(None),
        next: str = Form("/"),
    ) -> Any:
        bounce = gated(request)
        if bounce:
            return bounce
        user = current_user(request)
        assert user is not None
        actor = _Actor(user)
        g = guild()
        if g is None:
            return bounce_to(next, err="Bot is not in the guild yet.")
        try:
            message = await _run_action(
                action=action,
                actor=actor,
                g=g,
                discord_id=int(discord_id) if discord_id and str(discord_id).isdigit() else None,
                reason=reason or "",
                rank=rank,
                position=position,
                callsign=callsign,
                note_type=note_type,
                content=content,
                loa_id=loa_id,
                weekly_minutes=weekly_minutes,
                low_minutes=low_minutes,
                middle_minutes=middle_minutes,
                high_minutes=high_minutes,
            )
        except ValueError as exc:
            return bounce_to(next, err=str(exc))
        except Exception:
            log.exception("Web action %s failed", action)
            return bounce_to(next, err="That action failed. Check Render logs.")
        return bounce_to(next, ok=message)

    async def _run_action(**kwargs: Any) -> str:
        action = kwargs["action"]
        actor: _Actor = kwargs["actor"]
        g: discord.Guild = kwargs["g"]
        discord_id: int | None = kwargs["discord_id"]
        reason: str = kwargs["reason"] or "Updated from Command Center"
        member = await member_for(discord_id) if discord_id else None

        if action == "reset_shifts":
            deleted = await reset_shift_data(bot, g, actor)
            return f"Cleared {deleted} shift record(s) and duty quota totals."

        if action == "quota_set":
            cfg = await bot.guild_config(g.id)
            parts = []
            if kwargs.get("low_minutes") is not None:
                cfg.set_path(["quota", "low_minutes"], int(kwargs["low_minutes"]))
                parts.append(f"LR {kwargs['low_minutes']}")
            if kwargs.get("middle_minutes") is not None:
                cfg.set_path(["quota", "middle_minutes"], int(kwargs["middle_minutes"]))
                parts.append(f"MR {kwargs['middle_minutes']}")
            if kwargs.get("high_minutes") is not None:
                cfg.set_path(["quota", "high_minutes"], int(kwargs["high_minutes"]))
                parts.append(f"HR {kwargs['high_minutes']}")
            if kwargs.get("weekly_minutes") is not None and not parts:
                cfg.set_path(["quota", "low_minutes"], int(kwargs["weekly_minutes"]))
                parts.append(f"LR {kwargs['weekly_minutes']}")
            if parts:
                await bot.save_config(g.id, cfg)
                return "Quota updated: " + ", ".join(parts)
            return "No quota values submitted."

        if action in {"loa_approve", "loa_deny"} and kwargs["loa_id"]:
            status = "approved" if action == "loa_approve" else "denied"
            err = await decide_loa_record(bot, g, int(kwargs["loa_id"]), status, reason or None, actor)
            if err:
                raise ValueError(err)
            return f"LOA #{kwargs['loa_id']} {status}."

        if discord_id is None:
            raise ValueError("Discord ID is required.")

        if action == "add":
            if member is None:
                raise ValueError("Could not find that Discord member in the guild.")
            if not kwargs["rank"]:
                raise ValueError("Rank is required.")
            rank_row = await db.get_rank_by_name(g.id, kwargs["rank"])
            if not rank_row:
                raise ValueError("Unknown rank.")
            record = await db.upsert_personnel(g.id, member.id, str(member), rank_id=rank_row["id"])
            fields: dict[str, Any] = {"status": "active"}
            if kwargs["position"]:
                fields["position"] = kwargs["position"]
            if kwargs["callsign"]:
                fields["callsign"] = kwargs["callsign"]
            await db.update_personnel(record["id"], **fields)
            cfg = await bot.guild_config(g.id)
            await sync_rank_roles(member, kwargs["rank"], cfg)
            return f"{member} added as {kwargs['rank']}."

        if member is None:
            raise ValueError("Could not find that Discord member in the guild. The bot may need the Members intent.")

        if action == "note":
            record = await ensure_personnel(bot, member)
            await db.add_note(record["id"], kwargs["note_type"] or "hr", kwargs["content"] or reason, actor.id)
            return f"Note added to {member}."

        if action == "transfer":
            record = await ensure_personnel(bot, member)
            await db.update_personnel(record["id"], position=kwargs["position"] or "Patrol")
            return f"{member} transferred to {kwargs['position'] or 'Patrol'}."

        if action == "suspend":
            await set_status(bot, g, member, "suspended", reason, actor)
            return f"{member} suspended."
        if action == "remove":
            await set_status(bot, g, member, "removed", reason, actor)
            return f"{member} removed from the roster."
        if action == "reinstate":
            await set_status(bot, g, member, "active", reason, actor)
            return f"{member} reinstated."
        if action == "promote":
            err = await change_rank(bot, g, member, kwargs["rank"] or "", reason, actor, actor, "promotion")
            if err:
                raise ValueError(err)
            return f"{member} promoted to {kwargs['rank']}."
        if action == "demote":
            err = await change_rank(bot, g, member, kwargs["rank"] or "", reason, actor, actor, "demotion")
            if err:
                raise ValueError(err)
            return f"{member} demoted to {kwargs['rank']}."
        if action == "fire":
            return await fire_member(bot, g, member, reason, actor, actor)
        if action == "end_shift":
            await end_active_shift(bot, g, member.id)
            return f"Active shift ended for {member}."
        raise ValueError(f"Unknown action {action}")

    return app


NAV_GROUPS = [
    (
        "Operations",
        [
            ("overview", "/", "Overview"),
            ("shifts", "/shifts", "Shifts"),
            ("quota", "/quota", "Quota"),
            ("loa", "/loa", "LOA"),
        ],
    ),
    (
        "People",
        [
            ("personnel", "/personnel", "Personnel"),
        ],
    ),
    (
        "System",
        [
            ("audit", "/audit", "Audit"),
            ("settings", "/settings", "Settings"),
        ],
    ),
]

NAV = [item for _group, items in NAV_GROUPS for item in items]
