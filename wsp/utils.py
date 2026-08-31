"""Shared helpers used across cogs."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord

from wsp.constants import BAND_ROLE_KEYS, rank_band
from wsp.db import now_ts

if TYPE_CHECKING:
    from wsp.bot import WSPBot

log = logging.getLogger("wsp.roles")


def member_role_ids(member: discord.Member) -> set[int]:
    """Role IDs Discord assigned to the member, including IDs not in the local cache."""
    ids: set[int] = {member.guild.id}
    raw = getattr(member, "_roles", None)
    if raw is not None:
        try:
            ids.update(int(rid) for rid in raw)
        except TypeError:
            pass
    ids.update(role.id for role in member.roles)
    return ids


async def fetch_live_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        found = guild.get_member(user_id)
        return found if isinstance(found, discord.Member) else None


async def resolve_guild_roles(guild: discord.Guild, role_ids: list[int] | set[int]) -> dict[int, discord.Role]:
    wanted = {int(rid) for rid in role_ids if rid}
    found: dict[int, discord.Role] = {}
    missing: list[int] = []
    for rid in wanted:
        role = guild.get_role(rid)
        if role:
            found[rid] = role
        else:
            missing.append(rid)
    if not missing:
        return found
    try:
        for role in await guild.fetch_roles():
            if role.id in wanted:
                found[role.id] = role
    except discord.HTTPException as exc:
        log.warning("Could not fetch guild roles: %s", exc)
    return found


def _bot_member(guild: discord.Guild) -> discord.Member | None:
    me = guild.me
    if me is not None:
        return me
    user = getattr(getattr(guild, "_state", None), "user", None)
    if user is None:
        return None
    found = guild.get_member(user.id)
    return found if isinstance(found, discord.Member) else None


def role_is_manageable(guild: discord.Guild, role: discord.Role, me: discord.Member | None = None) -> bool:
    if role.is_default() or role.managed:
        return False
    me = me or _bot_member(guild)
    if me is None:
        return True
    if not me.guild_permissions.manage_roles:
        return False
    return role < me.top_role


async def apply_role_changes(
    member: discord.Member,
    *,
    add: list[discord.Role],
    remove: list[discord.Role],
    reason: str,
) -> tuple[int, int]:
    """Add and remove roles, skipping ones the bot cannot manage. Returns (added, removed)."""
    note = (reason or "WSP")[:512]
    me = _bot_member(member.guild)
    if me is None:
        user = getattr(getattr(member.guild, "_state", None), "user", None)
        if user is not None:
            try:
                me = await member.guild.fetch_member(user.id)
            except discord.HTTPException:
                me = None
    held = member_role_ids(member)
    unique_remove: list[discord.Role] = []
    seen: set[int] = set()
    for role in remove:
        if role.id in seen or role.id not in held:
            continue
        seen.add(role.id)
        unique_remove.append(role)
    unique_add: list[discord.Role] = []
    for role in add:
        if role.id in seen or role.id in held or role.id in {r.id for r in unique_add}:
            continue
        unique_add.append(role)

    removable = [role for role in unique_remove if role_is_manageable(member.guild, role, me)]
    addable = [role for role in unique_add if role_is_manageable(member.guild, role, me)]
    skipped = [role for role in unique_remove + unique_add if not role_is_manageable(member.guild, role, me)]
    if skipped:
        log.warning(
            "Skipping unmanageable roles for %s (%s): %s",
            member.id,
            member.guild.id,
            ", ".join(f"{role.name}:{role.id}" for role in skipped),
        )

    async def _apply(method, roles: list[discord.Role]) -> int:
        if not roles:
            return 0
        try:
            await method(*roles, reason=note)
            return len(roles)
        except discord.HTTPException:
            ok = 0
            for role in roles:
                try:
                    await method(role, reason=note)
                    ok += 1
                except discord.HTTPException as exc:
                    log.warning("Role update failed for %s role %s: %s", member.id, role.id, exc)
            return ok

    removed = await _apply(member.remove_roles, removable)
    added = await _apply(member.add_roles, addable)
    if skipped and added == 0 and removed == 0:
        leftover_remove = [role for role in unique_remove if role in skipped]
        leftover_add = [role for role in unique_add if role in skipped]
        removed += await _apply(member.remove_roles, leftover_remove)
        added += await _apply(member.add_roles, leftover_add)
    return added, removed


def parse_date(text: str, tz_name: str = "America/Chicago") -> int | None:
    """Parse YYYY-MM-DD or YYYY-MM-DD HH:MM into a UTC unix timestamp."""
    text = (text or "").strip()
    tz = ZoneInfo(tz_name)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            local = datetime.strptime(text, fmt).replace(tzinfo=tz)
            return int(local.timestamp())
        except ValueError:
            continue
    return None


async def ensure_personnel(bot: WSPBot, member: discord.Member):
    record = await bot.db.get_personnel(member.guild.id, member.id)
    if record:
        if record["username"] != str(member):
            await bot.db.update_personnel(record["id"], username=str(member))
            record = await bot.db.get_personnel(member.guild.id, member.id)
        return record
    cfg = await bot.guild_config(member.guild.id)
    rank_id = None
    role_ids = member_role_ids(member)
    ranks = await bot.db.list_ranks(member.guild.id)
    matched = None
    for rank in reversed(list(ranks)):
        rid = cfg.rank_role_id(rank["name"])
        if rid and rid in role_ids:
            matched = rank
            break
    if matched:
        rank_id = matched["id"]
    return await bot.db.upsert_personnel(member.guild.id, member.id, str(member), rank_id=rank_id)


async def sync_rank_roles(member: discord.Member, new_rank: str, cfg) -> str | None:
    """Apply rank and band roles. Returns an error message, or None on success."""
    guild = member.guild
    live = await fetch_live_member(guild, member.id)
    if live is not None:
        member = live
    wanted_ids: set[int] = set()
    rank_roles = cfg.get("rank_roles") or {}
    for name in rank_roles:
        rid = cfg.rank_role_id(str(name))
        if rid:
            wanted_ids.add(rid)
    for key in BAND_ROLE_KEYS.values():
        rid = cfg.role_id(key)
        if rid:
            wanted_ids.add(rid)
    wanted_ids.update(cfg.retired_rank_role_ids())
    wsp_role_id = cfg.role_id("wsp")
    if wsp_role_id:
        wanted_ids.add(wsp_role_id)
    roles = await resolve_guild_roles(guild, wanted_ids)
    held = member_role_ids(member)
    to_add: list[discord.Role] = []
    to_remove: list[discord.Role] = []
    target_rank_id = cfg.rank_role_id(new_rank)
    for name in rank_roles:
        rid = cfg.rank_role_id(str(name))
        role = roles.get(rid)
        if not role:
            continue
        has = rid in held
        if str(name) == new_rank and not has:
            to_add.append(role)
        elif str(name) != new_rank and has:
            to_remove.append(role)
    wanted_band = rank_band(new_rank)
    for band, key in BAND_ROLE_KEYS.items():
        rid = cfg.role_id(key)
        role = roles.get(rid)
        if not role:
            continue
        has = rid in held
        if band == wanted_band and not has:
            to_add.append(role)
        elif band != wanted_band and has:
            to_remove.append(role)
    for rid in cfg.retired_rank_role_ids():
        role = roles.get(rid)
        if role and rid in held:
            to_remove.append(role)
    wsp_role = roles.get(wsp_role_id) if wsp_role_id else None
    if wsp_role and wsp_role_id not in held:
        to_add.append(wsp_role)
    await apply_role_changes(member, add=to_add, remove=to_remove, reason=f"WSP rank update → {new_rank}")
    if not target_rank_id:
        return None
    if target_rank_id not in roles:
        return "Could not update roles."
    refreshed = await fetch_live_member(guild, member.id) or member
    if target_rank_id not in member_role_ids(refreshed):
        return "Could not update roles."
    return None


async def sync_duty_role(member: discord.Member, cfg, on_duty: bool) -> None:
    rid = cfg.role_id("on_duty")
    if not rid:
        return
    live = await fetch_live_member(member.guild, member.id)
    if live is not None:
        member = live
    roles = await resolve_guild_roles(member.guild, [rid])
    role = roles.get(rid)
    if not role:
        return
    has = rid in member_role_ids(member)
    if on_duty and not has:
        await apply_role_changes(member, add=[role], remove=[], reason="WSP on duty")
    elif not on_duty and has:
        await apply_role_changes(member, add=[], remove=[role], reason="WSP off duty")


async def member_from_id(bot: WSPBot, guild: discord.Guild | None, user_id: int) -> discord.Member | None:
    if guild is None:
        return None
    found = await bot.fetch_guild_user(guild, int(user_id))
    return found if isinstance(found, discord.Member) else None


def member_can_start_shift(member: discord.Member, cfg) -> bool:
    rid = cfg.role_id("shift_certified")
    if not rid:
        return False
    return rid in member_role_ids(member)


def quota_required_minutes(member: discord.Member | None, cfg, rank_name: str | None = None) -> int:
    high = int(cfg.get("quota", "high_minutes") or 30)
    middle = int(cfg.get("quota", "middle_minutes") or 75)
    low = int(cfg.get("quota", "low_minutes") or 90)
    if member is not None:
        ids = member_role_ids(member)
        if cfg.role_id("high_rank") in ids:
            return high
        if cfg.role_id("middle_rank") in ids:
            return middle
        if cfg.role_id("low_rank") in ids:
            return low
    band = rank_band(rank_name)
    if band == "high":
        return high
    if band == "middle":
        return middle
    return low


def hms_to_seconds(hours: int | None, minutes: int | None, seconds: int | None) -> int:
    return max(0, int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(seconds or 0))


def mention_or_id(guild: discord.Guild | None, discord_id: str | int | None) -> str:
    if not discord_id:
        return "—"
    try:
        uid = int(discord_id)
    except (TypeError, ValueError):
        return f"`{discord_id}`"
    if guild:
        member = guild.get_member(uid)
        if member:
            return member.mention
    return f"<@{uid}>"


def current_shift_seconds(row) -> int:
    from wsp.db import Database

    dummy = Database.__new__(Database)
    return dummy.effective_shift_seconds(row)
