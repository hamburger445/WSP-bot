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
        self.owner_ids = parse_owner_ids()
        guild = _env("GUILD_ID")
        self.guild_id = int(guild) if guild.isdigit() else 0
        self.database_path = Path(_env("DATABASE_PATH", "data/wsp.db"))
        if not self.database_path.is_absolute():
            self.database_path = ROOT / self.database_path
        self.timezone = _env("TIMEZONE", "America/Chicago")
        self.log_level = _env("LOG_LEVEL", "INFO").upper()
        configured_data_dir = _env("DATA_DIR")
        self.data_dir = Path(configured_data_dir) if configured_data_dir else self.database_path.parent
        if not self.data_dir.is_absolute():
            self.data_dir = ROOT / self.data_dir
        self.backups_dir = self.data_dir / "backups"
        self.transcripts_dir = self.data_dir / "transcripts"
        self.logs_dir = self.data_dir / "logs"
        self.github_token = _env("GITHUB_TOKEN") or _env("GH_TOKEN")
        self.github_repo = _env("GITHUB_REPO", "hamburger445/WSP-bot")
        self.github_db_branch = _env("GITHUB_DB_BRANCH", "main")
        self.github_db_path = _env("GITHUB_DB_PATH", "data/wsp.db")

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

    def fire_role_ids(self) -> list[int]:
        ids: list[int] = []
        for value in self.get("fire_roles") or []:
            sid = _as_snowflake(value)
            if sid:
                ids.append(sid)
        return ids

    def retired_rank_role_ids(self) -> list[int]:
        ids: list[int] = []
        for value in self.get("retired_rank_roles") or []:
            sid = _as_snowflake(value)
            if sid:
                ids.append(sid)
        return ids

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
        ids.update(self.fire_role_ids())
        ids.update(self.retired_rank_role_ids())
        return ids

    def apply_published_structure(self) -> bool:
        """Keep rank ladder, band/duty roles, and fire extras in lockstep with default.json."""
        defaults = load_default_config()
        changed = False
        new_rank_roles = defaults.get("rank_roles") or {}
        retired = [str(v) for v in (self._data.get("retired_rank_roles") or [])]
        for name, raw in (self._data.get("rank_roles") or {}).items():
            if name in new_rank_roles:
                continue
            sid = _as_snowflake(raw)
            if sid and str(sid) not in retired:
                retired.append(str(sid))
        if retired != [str(v) for v in (self._data.get("retired_rank_roles") or [])]:
            self._data["retired_rank_roles"] = retired
            changed = True
        if self._data.get("ranks") != defaults.get("ranks"):
            self._data["ranks"] = deepcopy(defaults.get("ranks") or [])
            changed = True
        if self._data.get("rank_roles") != new_rank_roles:
            self._data["rank_roles"] = deepcopy(new_rank_roles)
            changed = True
        wanted_fire = [str(v) for v in (defaults.get("fire_roles") or [])]
        current_fire = [str(v) for v in (self._data.get("fire_roles") or [])]
        if current_fire != wanted_fire:
            self._data["fire_roles"] = list(defaults.get("fire_roles") or [])
            changed = True
        for key in ("on_duty", "high_rank", "middle_rank", "low_rank", "shift_certified"):
            wanted = str((defaults.get("roles") or {}).get(key) or "")
            current = str((self._data.get("roles") or {}).get(key) or "")
            if current != wanted:
                self.set_path(["roles", key], wanted)
                changed = True
        wanted_loa = str((defaults.get("channels") or {}).get("loa") or "")
        current_loa = str((self._data.get("channels") or {}).get("loa") or "")
        if wanted_loa and current_loa != wanted_loa:
            self.set_path(["channels", "loa"], wanted_loa)
            changed = True
        wanted_academy = str((defaults.get("channels") or {}).get("academy") or "")
        current_academy = str((self._data.get("channels") or {}).get("academy") or "")
        if wanted_academy and current_academy != wanted_academy:
            self.set_path(["channels", "academy"], wanted_academy)
            changed = True
        quota_defaults = defaults.get("quota") or {}
        quota = self._data.setdefault("quota", {})
        if not isinstance(quota, dict):
            quota = {}
            self._data["quota"] = quota
        for key in ("low_minutes", "middle_minutes", "high_minutes"):
            if key in quota_defaults and quota.get(key) in (None, ""):
                quota[key] = quota_defaults[key]
                changed = True
        return changed

    def missing_required(self) -> list[str]:
        missing: list[str] = []
        if not self.guild_id():
            missing.append("guild_id")
        defaults = load_default_config()
        for section in ("roles", "rank_roles", "channels", "categories"):
            for key in (defaults.get(section) or {}):
                if not _as_snowflake(self.get(section, key)):
                    missing.append(f"{section}.{key}")
        return missing


def _as_snowflake(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
