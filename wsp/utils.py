"""Shared helpers used across cogs."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord

from wsp.constants import BAND_ROLE_KEYS, rank_band
from wsp.db import now_ts

if TYPE_CHECKING:
    from wsp.bot import WSPBot


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
    role_ids = {r.id for r in member.roles}
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


async def sync_rank_roles(member: discord.Member, new_rank: str, cfg) -> None:
    rank_roles = cfg.get("rank_roles") or {}
    managed = []
    for name, raw in rank_roles.items():
        try:
            rid = int(raw)
        except (TypeError, ValueError):
            continue
        role = member.guild.get_role(rid)
        if role:
            managed.append((name, role))
    to_add = []
    to_remove = []
    for name, role in managed:
        has = role in member.roles
        if name == new_rank and not has:
            to_add.append(role)
        elif name != new_rank and has:
            to_remove.append(role)
    wanted_band = rank_band(new_rank)
    for band, key in BAND_ROLE_KEYS.items():
        rid = cfg.role_id(key)
        role = member.guild.get_role(rid) if rid else None
        if not role:
            continue
        has = role in member.roles
        if band == wanted_band and not has:
            to_add.append(role)
        elif band != wanted_band and has:
            to_remove.append(role)
    for rid in cfg.retired_rank_role_ids():
        role = member.guild.get_role(rid)
        if role and role in member.roles:
            to_remove.append(role)
    wsp_role_id = cfg.role_id("wsp")
    wsp_role = member.guild.get_role(wsp_role_id) if wsp_role_id else None
    reason = f"WSP rank update → {new_rank}"
    seen: set[int] = set()
    unique_remove = []
    for role in to_remove:
        if role.id not in seen:
            seen.add(role.id)
            unique_remove.append(role)
    unique_add = []
    for role in to_add:
        if role.id not in seen and role.id not in {r.id for r in unique_add}:
            unique_add.append(role)
    try:
        if unique_remove:
            await member.remove_roles(*unique_remove, reason=reason)
        if unique_add:
            await member.add_roles(*unique_add, reason=reason)
        if wsp_role and wsp_role not in member.roles:
            await member.add_roles(wsp_role, reason=reason)
    except discord.Forbidden:
        pass


async def sync_duty_role(member: discord.Member, cfg, on_duty: bool) -> None:
    rid = cfg.role_id("on_duty")
    if not rid:
        return
    role = member.guild.get_role(rid)
    if not role:
        return
    has = role in member.roles
    try:
        if on_duty and not has:
            await member.add_roles(role, reason="WSP on duty")
        elif not on_duty and has:
            await member.remove_roles(role, reason="WSP off duty")
    except discord.Forbidden:
        pass


async def member_from_id(bot: WSPBot, guild: discord.Guild | None, user_id: int) -> discord.Member | None:
    if guild is None:
        return None
    found = await bot.fetch_guild_user(guild, int(user_id))
    return found if isinstance(found, discord.Member) else None


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
