"""SQLite persistence with PostgreSQL-portable types and parameterized SQL."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from wsp.config import GuildConfig
from wsp.constants import DEFAULT_RANKS

log = logging.getLogger("wsp.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ranks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    role_id TEXT,
    permission_level INTEGER NOT NULL DEFAULT 1,
    UNIQUE(guild_id, name)
);

CREATE TABLE IF NOT EXISTS personnel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    username TEXT NOT NULL,
    rank_id INTEGER,
    position TEXT,
    callsign TEXT,
    join_date INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    training_status TEXT NOT NULL DEFAULT 'pending',
    supervision_status TEXT NOT NULL DEFAULT 'none',
    probation_status TEXT NOT NULL DEFAULT 'none',
    quota_exempt INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(guild_id, discord_id)
);

CREATE TABLE IF NOT EXISTS rank_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    personnel_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    from_rank TEXT,
    to_rank TEXT,
    reason TEXT,
    authorized_by TEXT,
    actor_id TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    personnel_id INTEGER NOT NULL,
    note_type TEXT NOT NULL,
    content TEXT NOT NULL,
    author_id TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS training_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    module TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'incomplete',
    instructor_id TEXT,
    notes TEXT,
    completed_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fastpass (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    applicant_id TEXT NOT NULL,
    reviewer_id TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    scores_json TEXT,
    average REAL,
    notes TEXT,
    recommendation TEXT,
    created_at INTEGER NOT NULL,
    reviewed_at INTEGER
);

CREATE TABLE IF NOT EXISTS supervisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    trooper_id TEXT NOT NULL,
    supervisor_id TEXT NOT NULL,
    start_time INTEGER NOT NULL,
    end_time INTEGER,
    duration_seconds INTEGER,
    traffic_stops INTEGER DEFAULT 0,
    radio_score INTEGER,
    driving_score INTEGER,
    scene_score INTEGER,
    communication_score INTEGER,
    policy_score INTEGER,
    overall_score INTEGER,
    comments TEXT,
    result TEXT,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS probation_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    probation_id INTEGER NOT NULL,
    reviewer_id TEXT NOT NULL,
    performance TEXT,
    issues TEXT,
    recommendations TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS probations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    supervisor_id TEXT,
    start_date INTEGER NOT NULL,
    expected_end INTEGER NOT NULL,
    actual_end INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    issues TEXT,
    recommendations TEXT,
    final_result TEXT,
    extension_reason TEXT,
    notified_ending INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    rank_name TEXT,
    callsign TEXT,
    start_time INTEGER NOT NULL,
    end_time INTEGER,
    pause_started INTEGER,
    paused_seconds INTEGER NOT NULL DEFAULT 0,
    duration_seconds INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS quota_weeks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    week_start INTEGER NOT NULL,
    UNIQUE(guild_id, week_start)
);

CREATE TABLE IF NOT EXISTS quota_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_id INTEGER NOT NULL,
    discord_id TEXT NOT NULL,
    required_minutes INTEGER NOT NULL,
    completed_minutes INTEGER NOT NULL DEFAULT 0,
    supervision_minutes INTEGER NOT NULL DEFAULT 0,
    quota_type TEXT NOT NULL DEFAULT 'duty',
    status TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    UNIQUE(week_id, discord_id, quota_type)
);

CREATE TABLE IF NOT EXISTS loa_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    start_date INTEGER NOT NULL,
    end_date INTEGER NOT NULL,
    reason TEXT NOT NULL,
    additional_info TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewer_id TEXT,
    review_note TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS discipline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    issued_by TEXT NOT NULL,
    expires_at INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS discipline_appeals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    discipline_id INTEGER NOT NULL,
    discord_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewer_id TEXT,
    review_note TEXT,
    created_at INTEGER NOT NULL,
    reviewed_at INTEGER
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    channel_id TEXT,
    opener_id TEXT NOT NULL,
    ticket_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    claimed_by TEXT,
    created_at INTEGER NOT NULL,
    closed_at INTEGER,
    close_reason TEXT,
    transcript_path TEXT
);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    author_id TEXT,
    author_name TEXT,
    content TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    vehicle_name TEXT NOT NULL,
    plate TEXT,
    status TEXT NOT NULL DEFAULT 'assigned',
    assigned_by TEXT,
    notes TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    activity_type TEXT NOT NULL,
    details TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    actor_id TEXT,
    actor_name TEXT,
    action TEXT NOT NULL,
    target_id TEXT,
    target_name TEXT,
    details TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_personnel_guild ON personnel(guild_id, discord_id);
CREATE INDEX IF NOT EXISTS idx_shifts_active ON shifts(guild_id, discord_id, status);
CREATE INDEX IF NOT EXISTS idx_audit_guild ON audit_log(guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_loa_status ON loa_requests(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_quota_week ON quota_records(week_id, discord_id);
"""


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


class Database:
    def __init__(self, path: Path, backups_dir: Path) -> None:
        self.path = path
        self.backups_dir = backups_dir
        self._db: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database is not connected")
        return self._db

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        log.info("Database ready at %s", self.path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def backup(self) -> Path | None:
        if not self.path.exists():
            return None
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = self.backups_dir / f"wsp-{stamp}.db"
        try:
            await self.conn.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception:
            pass
        shutil.copy2(self.path, dest)
        backups = sorted(self.backups_dir.glob("wsp-*.db"))
        for old in backups[:-14]:
            old.unlink(missing_ok=True)
        log.info("Database backup written to %s", dest)
        return dest

    async def snapshot_bytes(self) -> bytes:
        """WAL-checkpointed copy of the live database for GitHub backup."""
        if not self.path.exists():
            return b""
        try:
            await self.conn.execute("PRAGMA wal_checkpoint(FULL)")
            await self.conn.commit()
        except Exception:
            log.exception("WAL checkpoint failed before GitHub snapshot")
        return self.path.read_bytes()

    async def execute(self, sql: str, params: tuple | list = ()) -> aiosqlite.Cursor:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur

    async def fetchone(self, sql: str, params: tuple | list = ()) -> aiosqlite.Row | None:
        cur = await self.conn.execute(sql, params)
        return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple | list = ()) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(sql, params)
        return await cur.fetchall()

    # ── config ──────────────────────────────────────────────
    async def load_guild_config(self, guild_id: int) -> GuildConfig:
        row = await self.fetchone(
            "SELECT config_json FROM guild_config WHERE guild_id = ?",
            (str(guild_id),),
        )
        overlay = json.loads(row["config_json"]) if row else {}
        cfg = GuildConfig(overlay)
        if not cfg.guild_id() and guild_id:
            cfg.set_path(["guild_id"], str(guild_id))
        return cfg

    async def save_guild_config(self, guild_id: int, cfg: GuildConfig) -> None:
        payload = json.dumps(cfg.raw)
        await self.execute(
            """
            INSERT INTO guild_config (guild_id, config_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET config_json = excluded.config_json, updated_at = excluded.updated_at
            """,
            (str(guild_id), payload, now_ts()),
        )

    async def ensure_ranks(self, guild_id: int, cfg: GuildConfig) -> None:
        ranks = cfg.get("ranks") or []
        if not ranks:
            ranks = [{"name": n, "position": p, "permission_level": lv} for n, p, lv in DEFAULT_RANKS]
        for rank in ranks:
            role_id = cfg.rank_role_id(rank["name"]) or None
            await self.execute(
                """
                INSERT INTO ranks (guild_id, name, position, role_id, permission_level)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, name) DO UPDATE SET
                    position = excluded.position,
                    permission_level = excluded.permission_level,
                    role_id = COALESCE(excluded.role_id, ranks.role_id)
                """,
                (
                    str(guild_id),
                    rank["name"],
                    int(rank["position"]),
                    str(role_id) if role_id else None,
                    int(rank.get("permission_level", 1)),
                ),
            )

    async def list_ranks(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM ranks WHERE guild_id = ? ORDER BY position ASC",
            (str(guild_id),),
        )

    async def get_rank_by_name(self, guild_id: int, name: str) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM ranks WHERE guild_id = ? AND name = ?",
            (str(guild_id), name),
        )

    async def set_rank_role(self, guild_id: int, name: str, role_id: int) -> None:
        await self.execute(
            "UPDATE ranks SET role_id = ? WHERE guild_id = ? AND name = ?",
            (str(role_id), str(guild_id), name),
        )

    # ── personnel ───────────────────────────────────────────
    async def upsert_personnel(
        self,
        guild_id: int,
        discord_id: int,
        username: str,
        *,
        rank_id: int | None = None,
        join_if_new: bool = True,
    ) -> aiosqlite.Row:
        existing = await self.get_personnel(guild_id, discord_id)
        ts = now_ts()
        if existing:
            await self.execute(
                "UPDATE personnel SET username = ?, updated_at = ? WHERE id = ?",
                (username, ts, existing["id"]),
            )
            return await self.get_personnel(guild_id, discord_id)  # type: ignore[return-value]
        await self.execute(
            """
            INSERT INTO personnel (guild_id, discord_id, username, rank_id, join_date, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(guild_id), str(discord_id), username, rank_id, ts if join_if_new else None, ts, ts),
        )
        return await self.get_personnel(guild_id, discord_id)  # type: ignore[return-value]

    async def get_personnel(self, guild_id: int, discord_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            """
            SELECT p.*, r.name AS rank_name, r.position AS rank_position, r.permission_level AS rank_level
            FROM personnel p
            LEFT JOIN ranks r ON r.id = p.rank_id
            WHERE p.guild_id = ? AND p.discord_id = ?
            """,
            (str(guild_id), str(discord_id)),
        )

    async def get_personnel_by_id(self, personnel_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            """
            SELECT p.*, r.name AS rank_name, r.position AS rank_position, r.permission_level AS rank_level
            FROM personnel p
            LEFT JOIN ranks r ON r.id = p.rank_id
            WHERE p.id = ?
            """,
            (personnel_id,),
        )

    async def list_personnel(self, guild_id: int, status: str | None = "active") -> list[aiosqlite.Row]:
        if status:
            return await self.fetchall(
                """
                SELECT p.*, r.name AS rank_name, r.position AS rank_position
                FROM personnel p
                LEFT JOIN ranks r ON r.id = p.rank_id
                WHERE p.guild_id = ? AND p.status = ?
                ORDER BY COALESCE(r.position, 0) DESC, p.username
                """,
                (str(guild_id), status),
            )
        return await self.fetchall(
            """
            SELECT p.*, r.name AS rank_name, r.position AS rank_position
            FROM personnel p
            LEFT JOIN ranks r ON r.id = p.rank_id
            WHERE p.guild_id = ?
            ORDER BY COALESCE(r.position, 0) DESC, p.username
            """,
            (str(guild_id),),
        )

    async def update_personnel(self, personnel_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_ts()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [personnel_id]
        await self.execute(f"UPDATE personnel SET {assignments} WHERE id = ?", values)

    async def add_rank_history(
        self,
        personnel_id: int,
        action: str,
        from_rank: str | None,
        to_rank: str | None,
        reason: str,
        authorized_by: str | None,
        actor_id: str,
    ) -> None:
        await self.execute(
            """
            INSERT INTO rank_history (personnel_id, action, from_rank, to_rank, reason, authorized_by, actor_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (personnel_id, action, from_rank, to_rank, reason, authorized_by, actor_id, now_ts()),
        )

    async def rank_history(self, personnel_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM rank_history WHERE personnel_id = ? ORDER BY created_at DESC",
            (personnel_id,),
        )

    async def add_note(self, personnel_id: int, note_type: str, content: str, author_id: int) -> None:
        await self.execute(
            "INSERT INTO notes (personnel_id, note_type, content, author_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (personnel_id, note_type, content, str(author_id), now_ts()),
        )

    async def list_notes(self, personnel_id: int, note_type: str | None = None) -> list[aiosqlite.Row]:
        if note_type:
            return await self.fetchall(
                "SELECT * FROM notes WHERE personnel_id = ? AND note_type = ? ORDER BY created_at DESC",
                (personnel_id, note_type),
            )
        return await self.fetchall(
            "SELECT * FROM notes WHERE personnel_id = ? ORDER BY created_at DESC",
            (personnel_id,),
        )

    async def log_activity(self, guild_id: int, discord_id: int, activity_type: str, details: str) -> None:
        await self.execute(
            "INSERT INTO activity_log (guild_id, discord_id, activity_type, details, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(guild_id), str(discord_id), activity_type, details, now_ts()),
        )

    async def activity_history(self, guild_id: int, discord_id: int, limit: int = 25) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM activity_log WHERE guild_id = ? AND discord_id = ? ORDER BY created_at DESC LIMIT ?",
            (str(guild_id), str(discord_id), limit),
        )

    # ── training ────────────────────────────────────────────
    async def upsert_training(
        self, guild_id: int, discord_id: int, module: str, status: str, instructor_id: int | None, notes: str | None
    ) -> None:
        existing = await self.fetchone(
            "SELECT id FROM training_records WHERE guild_id = ? AND discord_id = ? AND module = ?",
            (str(guild_id), str(discord_id), module),
        )
        ts = now_ts()
        completed = ts if status == "complete" else None
        if existing:
            await self.execute(
                """
                UPDATE training_records
                SET status = ?, instructor_id = ?, notes = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, str(instructor_id) if instructor_id else None, notes, completed, existing["id"]),
            )
        else:
            await self.execute(
                """
                INSERT INTO training_records (guild_id, discord_id, module, status, instructor_id, notes, completed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(guild_id),
                    str(discord_id),
                    module,
                    status,
                    str(instructor_id) if instructor_id else None,
                    notes,
                    completed,
                    ts,
                ),
            )

    async def list_training(self, guild_id: int, discord_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM training_records WHERE guild_id = ? AND discord_id = ? ORDER BY module",
            (str(guild_id), str(discord_id)),
        )

    # ── fast-pass ───────────────────────────────────────────
    async def create_fastpass(self, guild_id: int, applicant_id: int, reviewer_id: int) -> int:
        cur = await self.execute(
            """
            INSERT INTO fastpass (guild_id, applicant_id, reviewer_id, status, created_at)
            VALUES (?, ?, ?, 'draft', ?)
            """,
            (str(guild_id), str(applicant_id), str(reviewer_id), now_ts()),
        )
        return int(cur.lastrowid)

    async def update_fastpass(self, fastpass_id: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [fastpass_id]
        await self.execute(f"UPDATE fastpass SET {assignments} WHERE id = ?", values)

    async def get_fastpass(self, fastpass_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM fastpass WHERE id = ?", (fastpass_id,))

    async def list_fastpass(self, guild_id: int, status: str | None = None) -> list[aiosqlite.Row]:
        if status:
            return await self.fetchall(
                "SELECT * FROM fastpass WHERE guild_id = ? AND status = ? ORDER BY created_at DESC",
                (str(guild_id), status),
            )
        return await self.fetchall(
            "SELECT * FROM fastpass WHERE guild_id = ? ORDER BY created_at DESC LIMIT 50",
            (str(guild_id),),
        )

    # ── supervision ─────────────────────────────────────────
    async def start_supervision(self, guild_id: int, trooper_id: int, supervisor_id: int) -> int:
        cur = await self.execute(
            """
            INSERT INTO supervisions (guild_id, trooper_id, supervisor_id, start_time, status)
            VALUES (?, ?, ?, ?, 'active')
            """,
            (str(guild_id), str(trooper_id), str(supervisor_id), now_ts()),
        )
        return int(cur.lastrowid)

    async def get_supervision(self, supervision_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM supervisions WHERE id = ?", (supervision_id,))

    async def active_supervision(self, guild_id: int, trooper_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM supervisions WHERE guild_id = ? AND trooper_id = ? AND status = 'active'",
            (str(guild_id), str(trooper_id)),
        )

    async def update_supervision(self, supervision_id: int, **fields: Any) -> None:
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [supervision_id]
        await self.execute(f"UPDATE supervisions SET {assignments} WHERE id = ?", values)

    async def list_supervisions(self, guild_id: int, discord_id: int | None = None) -> list[aiosqlite.Row]:
        if discord_id:
            return await self.fetchall(
                """
                SELECT * FROM supervisions
                WHERE guild_id = ? AND (trooper_id = ? OR supervisor_id = ?)
                ORDER BY start_time DESC
                """,
                (str(guild_id), str(discord_id), str(discord_id)),
            )
        return await self.fetchall(
            "SELECT * FROM supervisions WHERE guild_id = ? ORDER BY start_time DESC LIMIT 50",
            (str(guild_id),),
        )

    # ── probation ───────────────────────────────────────────
    async def start_probation(
        self, guild_id: int, discord_id: int, supervisor_id: int | None, duration_days: int
    ) -> int:
        start = now_ts()
        expected = start + duration_days * 86400
        cur = await self.execute(
            """
            INSERT INTO probations (guild_id, discord_id, supervisor_id, start_date, expected_end, status)
            VALUES (?, ?, ?, ?, ?, 'active')
            """,
            (str(guild_id), str(discord_id), str(supervisor_id) if supervisor_id else None, start, expected),
        )
        return int(cur.lastrowid)

    async def active_probation(self, guild_id: int, discord_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM probations WHERE guild_id = ? AND discord_id = ? AND status IN ('active', 'extended') ORDER BY start_date DESC",
            (str(guild_id), str(discord_id)),
        )

    async def get_probation(self, probation_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM probations WHERE id = ?", (probation_id,))

    async def list_active_probations(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM probations WHERE guild_id = ? AND status IN ('active', 'extended') ORDER BY expected_end",
            (str(guild_id),),
        )

    async def update_probation(self, probation_id: int, **fields: Any) -> None:
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [probation_id]
        await self.execute(f"UPDATE probations SET {assignments} WHERE id = ?", values)

    async def add_probation_review(
        self, probation_id: int, reviewer_id: int, performance: str, issues: str, recommendations: str
    ) -> None:
        await self.execute(
            """
            INSERT INTO probation_reviews (probation_id, reviewer_id, performance, issues, recommendations, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (probation_id, str(reviewer_id), performance, issues, recommendations, now_ts()),
        )

    async def list_probation_reviews(self, probation_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM probation_reviews WHERE probation_id = ? ORDER BY created_at DESC",
            (probation_id,),
        )

    # ── shifts ──────────────────────────────────────────────
    async def active_shift(self, guild_id: int, discord_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM shifts WHERE guild_id = ? AND discord_id = ? AND status IN ('active', 'paused')",
            (str(guild_id), str(discord_id)),
        )

    async def start_shift(self, guild_id: int, discord_id: int, rank_name: str | None, callsign: str | None) -> int:
        cur = await self.execute(
            """
            INSERT INTO shifts (guild_id, discord_id, rank_name, callsign, start_time, status)
            VALUES (?, ?, ?, ?, ?, 'active')
            """,
            (str(guild_id), str(discord_id), rank_name, callsign, now_ts()),
        )
        return int(cur.lastrowid)

    async def update_shift(self, shift_id: int, **fields: Any) -> None:
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [shift_id]
        await self.execute(f"UPDATE shifts SET {assignments} WHERE id = ?", values)

    async def get_shift(self, shift_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM shifts WHERE id = ?", (shift_id,))

    async def list_shifts(self, guild_id: int, discord_id: int | None = None, limit: int = 25) -> list[aiosqlite.Row]:
        if discord_id:
            return await self.fetchall(
                "SELECT * FROM shifts WHERE guild_id = ? AND discord_id = ? ORDER BY start_time DESC LIMIT ?",
                (str(guild_id), str(discord_id), limit),
            )
        return await self.fetchall(
            "SELECT * FROM shifts WHERE guild_id = ? ORDER BY start_time DESC LIMIT ?",
            (str(guild_id), limit),
        )

    async def list_active_shifts(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM shifts WHERE guild_id = ? AND status IN ('active', 'paused') ORDER BY start_time",
            (str(guild_id),),
        )

    async def reset_shifts(self, guild_id: int) -> int:
        gid = str(guild_id)
        cur = await self.execute("DELETE FROM shifts WHERE guild_id = ?", (gid,))
        weeks = await self.fetchall("SELECT id FROM quota_weeks WHERE guild_id = ?", (gid,))
        for week in weeks:
            await self.execute(
                """
                UPDATE quota_records
                SET completed_minutes = 0, status = NULL, notified = 0
                WHERE week_id = ? AND quota_type = 'duty'
                """,
                (week["id"],),
            )
        return int(cur.rowcount or 0)

    async def shift_totals(self, guild_id: int, discord_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            """
            SELECT COUNT(*) AS shift_count,
                   COALESCE(SUM(duration_seconds), 0) AS total_seconds
            FROM shifts
            WHERE guild_id = ? AND discord_id = ? AND status = 'completed'
            """,
            (str(guild_id), str(discord_id)),
        )

    async def shift_leaderboard(self, guild_id: int, since: int | None = None, limit: int = 15) -> list[aiosqlite.Row]:
        if since:
            return await self.fetchall(
                """
                SELECT discord_id, COALESCE(SUM(duration_seconds), 0) AS total_seconds, COUNT(*) AS shift_count
                FROM shifts
                WHERE guild_id = ? AND status = 'completed' AND start_time >= ?
                GROUP BY discord_id
                ORDER BY total_seconds DESC
                LIMIT ?
                """,
                (str(guild_id), since, limit),
            )
        return await self.fetchall(
            """
            SELECT discord_id, COALESCE(SUM(duration_seconds), 0) AS total_seconds, COUNT(*) AS shift_count
            FROM shifts
            WHERE guild_id = ? AND status = 'completed'
            GROUP BY discord_id
            ORDER BY total_seconds DESC
            LIMIT ?
            """,
            (str(guild_id), limit),
        )

    def effective_shift_seconds(self, row: aiosqlite.Row) -> int:
        start = int(row["start_time"])
        end = int(row["end_time"] or now_ts())
        paused = int(row["paused_seconds"] or 0)
        if row["status"] == "paused" and row["pause_started"]:
            paused += max(0, now_ts() - int(row["pause_started"]))
        return max(0, end - start - paused)

    # ── quota ───────────────────────────────────────────────
    def week_start_ts(self, tz_name: str, at: datetime | None = None) -> int:
        tz = ZoneInfo(tz_name)
        moment = at.astimezone(tz) if at else datetime.now(tz)
        monday = (moment - timedelta(days=moment.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(monday.timestamp())

    async def ensure_week(self, guild_id: int, week_start: int) -> int:
        row = await self.fetchone(
            "SELECT id FROM quota_weeks WHERE guild_id = ? AND week_start = ?",
            (str(guild_id), week_start),
        )
        if row:
            return int(row["id"])
        cur = await self.execute(
            "INSERT INTO quota_weeks (guild_id, week_start) VALUES (?, ?)",
            (str(guild_id), week_start),
        )
        return int(cur.lastrowid)

    async def get_quota_record(self, week_id: int, discord_id: int, quota_type: str) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM quota_records WHERE week_id = ? AND discord_id = ? AND quota_type = ?",
            (week_id, str(discord_id), quota_type),
        )

    async def upsert_quota_record(
        self,
        week_id: int,
        discord_id: int,
        quota_type: str,
        required_minutes: int,
        *,
        add_completed: int = 0,
        add_supervision: int = 0,
        status: str | None = None,
    ) -> None:
        existing = await self.get_quota_record(week_id, discord_id, quota_type)
        if existing:
            new_completed = int(existing["completed_minutes"]) + add_completed
            new_supervision = int(existing["supervision_minutes"]) + add_supervision
            await self.execute(
                """
                UPDATE quota_records
                SET completed_minutes = ?, supervision_minutes = ?, required_minutes = ?, status = COALESCE(?, status)
                WHERE id = ?
                """,
                (new_completed, new_supervision, required_minutes, status, existing["id"]),
            )
        else:
            await self.execute(
                """
                INSERT INTO quota_records (week_id, discord_id, required_minutes, completed_minutes, supervision_minutes, quota_type, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (week_id, str(discord_id), required_minutes, add_completed, add_supervision, quota_type, status),
            )

    async def list_quota_records(self, week_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM quota_records WHERE week_id = ? ORDER BY quota_type, completed_minutes DESC",
            (week_id,),
        )

    async def set_quota_notified(self, record_id: int) -> None:
        await self.execute("UPDATE quota_records SET notified = 1 WHERE id = ?", (record_id,))

    # ── LOA ─────────────────────────────────────────────────
    async def create_loa(
        self, guild_id: int, discord_id: int, start_date: int, end_date: int, reason: str, additional: str | None
    ) -> int:
        cur = await self.execute(
            """
            INSERT INTO loa_requests (guild_id, discord_id, start_date, end_date, reason, additional_info, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (str(guild_id), str(discord_id), start_date, end_date, reason, additional, now_ts()),
        )
        return int(cur.lastrowid)

    async def get_loa(self, loa_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM loa_requests WHERE id = ?", (loa_id,))

    async def update_loa(self, loa_id: int, **fields: Any) -> None:
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [loa_id]
        await self.execute(f"UPDATE loa_requests SET {assignments} WHERE id = ?", values)

    async def list_loa(self, guild_id: int, status: str | None = None) -> list[aiosqlite.Row]:
        if status:
            return await self.fetchall(
                "SELECT * FROM loa_requests WHERE guild_id = ? AND status = ? ORDER BY start_date DESC",
                (str(guild_id), status),
            )
        return await self.fetchall(
            "SELECT * FROM loa_requests WHERE guild_id = ? ORDER BY created_at DESC LIMIT 50",
            (str(guild_id),),
        )

    async def active_loa(self, guild_id: int, discord_id: int, at: int | None = None) -> aiosqlite.Row | None:
        moment = at or now_ts()
        return await self.fetchone(
            """
            SELECT * FROM loa_requests
            WHERE guild_id = ? AND discord_id = ? AND status = 'approved'
              AND start_date <= ? AND end_date >= ?
            """,
            (str(guild_id), str(discord_id), moment, moment),
        )

    # ── discipline ──────────────────────────────────────────
    async def add_discipline(
        self, guild_id: int, discord_id: int, action: str, reason: str, issued_by: int, expires_at: int | None
    ) -> int:
        cur = await self.execute(
            """
            INSERT INTO discipline (guild_id, discord_id, action, reason, issued_by, expires_at, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (str(guild_id), str(discord_id), action, reason, str(issued_by), expires_at, now_ts()),
        )
        return int(cur.lastrowid)

    async def list_discipline(self, guild_id: int, discord_id: int | None = None, active_only: bool = False) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM discipline WHERE guild_id = ?"
        params: list[Any] = [str(guild_id)]
        if discord_id:
            sql += " AND discord_id = ?"
            params.append(str(discord_id))
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY created_at DESC"
        return await self.fetchall(sql, params)

    async def deactivate_discipline(self, record_id: int) -> None:
        await self.execute("UPDATE discipline SET active = 0 WHERE id = ?", (record_id,))

    async def get_discipline(self, record_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM discipline WHERE id = ?", (record_id,))

    async def create_appeal(self, guild_id: int, discipline_id: int, discord_id: int, statement: str) -> int:
        cur = await self.execute(
            """
            INSERT INTO discipline_appeals
                (guild_id, discipline_id, discord_id, statement, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (str(guild_id), discipline_id, str(discord_id), statement, now_ts()),
        )
        return int(cur.lastrowid)

    async def get_appeal(self, appeal_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM discipline_appeals WHERE id = ?", (appeal_id,))

    async def get_appeal_for_discipline(self, discipline_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM discipline_appeals WHERE discipline_id = ? ORDER BY created_at DESC LIMIT 1",
            (discipline_id,),
        )

    async def list_appeals(self, guild_id: int, status: str | None = "pending") -> list[aiosqlite.Row]:
        if status:
            return await self.fetchall(
                "SELECT * FROM discipline_appeals WHERE guild_id = ? AND status = ? ORDER BY created_at DESC",
                (str(guild_id), status),
            )
        return await self.fetchall(
            "SELECT * FROM discipline_appeals WHERE guild_id = ? ORDER BY created_at DESC LIMIT 50",
            (str(guild_id),),
        )

    async def update_appeal(self, appeal_id: int, **fields: Any) -> None:
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [appeal_id]
        await self.execute(f"UPDATE discipline_appeals SET {assignments} WHERE id = ?", values)

    # ── tickets ─────────────────────────────────────────────
    async def create_ticket(self, guild_id: int, opener_id: int, ticket_type: str, channel_id: int | None) -> int:
        cur = await self.execute(
            """
            INSERT INTO tickets (guild_id, channel_id, opener_id, ticket_type, status, created_at)
            VALUES (?, ?, ?, ?, 'open', ?)
            """,
            (str(guild_id), str(channel_id) if channel_id else None, str(opener_id), ticket_type, now_ts()),
        )
        return int(cur.lastrowid)

    async def get_ticket(self, ticket_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM tickets WHERE id = ?", (ticket_id,))

    async def get_ticket_by_channel(self, channel_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM tickets WHERE channel_id = ?", (str(channel_id),))

    async def update_ticket(self, ticket_id: int, **fields: Any) -> None:
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [ticket_id]
        await self.execute(f"UPDATE tickets SET {assignments} WHERE id = ?", values)

    async def add_ticket_message(self, ticket_id: int, author_id: int | None, author_name: str, content: str) -> None:
        await self.execute(
            """
            INSERT INTO ticket_messages (ticket_id, author_id, author_name, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ticket_id, str(author_id) if author_id else None, author_name, content, now_ts()),
        )

    async def ticket_messages(self, ticket_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY created_at ASC",
            (ticket_id,),
        )

    async def list_tickets(self, guild_id: int, status: str | None = None) -> list[aiosqlite.Row]:
        if status:
            return await self.fetchall(
                "SELECT * FROM tickets WHERE guild_id = ? AND status = ? ORDER BY created_at DESC",
                (str(guild_id), status),
            )
        return await self.fetchall(
            "SELECT * FROM tickets WHERE guild_id = ? ORDER BY created_at DESC LIMIT 75",
            (str(guild_id),),
        )

    # ── vehicles ────────────────────────────────────────────
    async def assign_vehicle(
        self, guild_id: int, discord_id: int, vehicle_name: str, plate: str | None, assigned_by: int, notes: str | None
    ) -> int:
        cur = await self.execute(
            """
            INSERT INTO vehicles (guild_id, discord_id, vehicle_name, plate, status, assigned_by, notes, created_at)
            VALUES (?, ?, ?, ?, 'assigned', ?, ?, ?)
            """,
            (str(guild_id), str(discord_id), vehicle_name, plate, str(assigned_by), notes, now_ts()),
        )
        return int(cur.lastrowid)

    async def list_vehicles(self, guild_id: int, discord_id: int | None = None) -> list[aiosqlite.Row]:
        if discord_id:
            return await self.fetchall(
                "SELECT * FROM vehicles WHERE guild_id = ? AND discord_id = ? ORDER BY created_at DESC",
                (str(guild_id), str(discord_id)),
            )
        return await self.fetchall(
            "SELECT * FROM vehicles WHERE guild_id = ? ORDER BY created_at DESC LIMIT 100",
            (str(guild_id),),
        )

    async def update_vehicle(self, vehicle_id: int, **fields: Any) -> None:
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [vehicle_id]
        await self.execute(f"UPDATE vehicles SET {assignments} WHERE id = ?", values)

    async def get_vehicle(self, vehicle_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,))

    # ── audit ───────────────────────────────────────────────
    async def audit(
        self,
        guild_id: int,
        action: str,
        *,
        actor_id: int | None = None,
        actor_name: str | None = None,
        target_id: int | str | None = None,
        target_name: str | None = None,
        details: str | None = None,
    ) -> None:
        await self.execute(
            """
            INSERT INTO audit_log (guild_id, actor_id, actor_name, action, target_id, target_name, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(guild_id),
                str(actor_id) if actor_id else None,
                actor_name,
                action,
                str(target_id) if target_id is not None else None,
                target_name,
                details,
                now_ts(),
            ),
        )

    async def list_audit(self, guild_id: int, limit: int = 50, action: str | None = None) -> list[aiosqlite.Row]:
        if action:
            return await self.fetchall(
                "SELECT * FROM audit_log WHERE guild_id = ? AND action = ? ORDER BY created_at DESC LIMIT ?",
                (str(guild_id), action, limit),
            )
        return await self.fetchall(
            "SELECT * FROM audit_log WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?",
            (str(guild_id), limit),
        )

    # ── dashboard aggregates ────────────────────────────────
    async def dashboard_counts(self, guild_id: int) -> dict[str, int]:
        gid = str(guild_id)
        active = await self.fetchone("SELECT COUNT(*) AS c FROM personnel WHERE guild_id = ? AND status = 'active'", (gid,))
        shifts = await self.fetchone(
            "SELECT COUNT(*) AS c FROM shifts WHERE guild_id = ? AND status IN ('active', 'paused')", (gid,)
        )
        loa = await self.fetchone(
            "SELECT COUNT(*) AS c FROM loa_requests WHERE guild_id = ? AND status = 'approved' AND start_date <= ? AND end_date >= ?",
            (gid, now_ts(), now_ts()),
        )
        pending = await self.fetchone(
            "SELECT COUNT(*) AS c FROM loa_requests WHERE guild_id = ? AND status = 'pending'",
            (gid,),
        )
        return {
            "active_personnel": int(active["c"]) if active else 0,
            "active_shifts": int(shifts["c"]) if shifts else 0,
            "loa": int(loa["c"]) if loa else 0,
            "pending_loa": int(pending["c"]) if pending else 0,
        }
