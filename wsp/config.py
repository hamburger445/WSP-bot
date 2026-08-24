"""Environment + department configuration.

Secrets live in .env. Department IDs and operational settings live in
config/default.json, then overlay onto the guild_config table so they can
be changed at runtime without editing source.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "default.json"

load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    raw = os.getenv(name, default).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        raw = raw[1:-1].strip()
    return raw


def _is_local_url(value: str) -> bool:
    lower = value.lower()
    return "127.0.0.1" in lower or "localhost" in lower or "0.0.0.0" in lower


def env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def parse_owner_ids() -> set[int]:
    raw = _env("OWNER_IDS")
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


class Settings:
    """Process-level settings from the environment."""

    def __init__(self) -> None:
        self.discord_token = _env("DISCORD_TOKEN")
        self.discord_client_id = _env("DISCORD_CLIENT_ID")
        self.discord_client_secret = _env("DISCORD_CLIENT_SECRET")
        self.owner_ids = parse_owner_ids()
        guild = _env("GUILD_ID")
        self.guild_id = int(guild) if guild.isdigit() else 0
        configured = _env("DASHBOARD_BASE_URL")
        render_url = _env("RENDER_EXTERNAL_URL")
        # A leftover local URL in env would break Discord OAuth on Render.
        if render_url and (not configured or _is_local_url(configured)):
            configured = render_url
        self.dashboard_base_url = (configured or "http://127.0.0.1:8080").rstrip("/")
        self.dashboard_secret = _env("DASHBOARD_SECRET_KEY", "change-me")
        self.host = _env("HOST", "0.0.0.0")
        self.port = env_int("PORT", 8080)
        self.database_path = Path(_env("DATABASE_PATH", "data/wsp.db"))
        if not self.database_path.is_absolute():
            self.database_path = ROOT / self.database_path
        self.timezone = _env("TIMEZONE", "America/Chicago")
        self.log_level = _env("LOG_LEVEL", "INFO").upper()
        self.data_dir = ROOT / "data"
        self.backups_dir = self.data_dir / "backups"
        self.transcripts_dir = self.data_dir / "transcripts"
        self.logs_dir = self.data_dir / "logs"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


def load_default_config() -> dict[str, Any]:
    if DEFAULT_CONFIG_PATH.exists():
        with DEFAULT_CONFIG_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


class GuildConfig:
    """Guild-scoped department configuration with JSON overlay on defaults."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = deep_merge(load_default_config(), data or {})

    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    def get(self, *path: str, default: Any = None) -> Any:
        cur: Any = self._data
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur[key]
        return cur

    def set_path(self, path: list[str], value: Any) -> None:
        cur = self._data
        for key in path[:-1]:
            nxt = cur.get(key)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[key] = nxt
            cur = nxt
        cur[path[-1]] = value

    def guild_id(self) -> int:
        raw = self.get("guild_id") or "0"
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def role_id(self, key: str) -> int:
        return _as_snowflake(self.get("roles", key))

    def rank_role_id(self, rank_name: str) -> int:
        return _as_snowflake(self.get("rank_roles", rank_name))

    def channel_id(self, key: str) -> int:
        return _as_snowflake(self.get("channels", key))

    def category_id(self, key: str) -> int:
        return _as_snowflake(self.get("categories", key))

    def all_managed_role_ids(self) -> set[int]:
        ids: set[int] = set()
        for value in (self.get("roles") or {}).values():
            sid = _as_snowflake(value)
            if sid:
                ids.add(sid)
        for value in (self.get("rank_roles") or {}).values():
            sid = _as_snowflake(value)
            if sid:
                ids.add(sid)
        return ids

    def missing_required(self) -> list[str]:
        missing: list[str] = []
        if not self.guild_id():
            missing.append("guild_id")
        for key in ("wsp", "hr", "command", "supervisor", "superintendent"):
            if not self.role_id(key):
                missing.append(f"roles.{key}")
        for key in ("audit_log", "notifications", "hr_log"):
            if not self.channel_id(key):
                missing.append(f"channels.{key}")
        return missing


def _as_snowflake(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
