"""Shared department operations used by slash commands and the website."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from wsp.constants import COLOR_DANGER, COLOR_GOLD, COLOR_NAVY
from wsp.db import now_ts
from wsp.embeds import add_fields, base_embed, error_embed, format_duration, success_embed
from wsp.utils import ensure_personnel, sync_rank_roles

if TYPE_CHECKING:
    from wsp.bot import WSPBot


async def strip_managed_roles(member: discord.Member, cfg, reason: str = "WSP termination") -> int:
    managed = cfg.all_managed_role_ids()
    to_remove = [role for role in member.roles if role.id in managed]
    if not to_remove:
        return 0
    try:
        await member.remove_roles(*to_remove, reason=reason)
    except discord.Forbidden:
        return 0
    return len(to_remove)


async def change_rank(
    bot: WSPBot,
    guild: discord.Guild,
    member: discord.Member,
    rank: str,
    reason: str,
    actor: discord.abc.User,
    authorizing: discord.abc.User,
    action: str,
) -> str | None:
    """Promote or demote. Returns an error message, or None on success."""
    new_rank = await bot.db.get_rank_by_name(guild.id, rank)
    if new_rank is None:
        return "Unknown rank."
    record = await ensure_personnel(bot, member)
    from_rank = record["rank_name"]
    from_pos = int(record["rank_position"] or 0)
    to_pos = int(new_rank["position"])
    if action == "promotion" and to_pos <= from_pos:
        return f"{member.display_name} is already at or above **{rank}**."
    if action == "demotion" and to_pos >= from_pos:
        return f"{member.display_name} is already at or below **{rank}**."
    await bot.db.update_personnel(record["id"], rank_id=new_rank["id"])
    await bot.db.add_rank_history(
        record["id"], action, from_rank, rank, reason, str(authorizing.id), str(actor.id)
    )
    cfg = await bot.guild_config(guild.id)
    await sync_rank_roles(member, rank, cfg)
    await bot.db.audit(
        guild.id,
        action,
        actor_id=actor.id,
        actor_name=str(actor),
        target_id=member.id,
        target_name=str(member),
        details=f"{from_rank} → {rank} | auth {authorizing} | {reason}",
    )
    await bot.db.log_activity(guild.id, member.id, action, f"{from_rank} → {rank}")
    color = COLOR_GOLD if action == "promotion" else COLOR_NAVY
    title = "Promotion" if action == "promotion" else "Demotion"
    public = base_embed(title, f"{member.mention}  •  {from_rank or 'Unassigned'} → **{rank}**", color=color)
    add_fields(
        public,
        [
            ("Reason", reason, False),
            ("Processed by", actor.mention, True),
            ("Authorized by", authorizing.mention, True),
        ],
    )
    dm = base_embed(title, f"Your rank is now **{rank}**.\n{from_rank or 'Unassigned'} → **{rank}**", color=color)
    dm.add_field(name="Reason", value=reason, inline=False)
    await bot.try_dm(member, dm)
    await bot.notify(guild, "promotions", public)
    await bot.notify(guild, "notifications", public)
    await bot.notify(guild, "command_log", public)
    return None


async def fire_member(
    bot: WSPBot,
    guild: discord.Guild,
    member: discord.Member,
    reason: str,
    actor: discord.abc.User,
    authorizing: discord.abc.User,
) -> str:
    record = await ensure_personnel(bot, member)
    from_rank = record["rank_name"]
    await end_active_shift(bot, guild, member.id)
    await bot.db.update_personnel(record["id"], status="removed")
    await bot.db.add_rank_history(
        record["id"], "termination", from_rank, None, reason, str(authorizing.id), str(actor.id)
    )
    cfg = await bot.guild_config(guild.id)
    stripped = await strip_managed_roles(member, cfg, reason=f"WSP fire: {reason}"[:80])
    await bot.db.audit(
        guild.id,
        "personnel_fire",
        actor_id=actor.id,
        actor_name=str(actor),
        target_id=member.id,
        target_name=str(member),
        details=reason,
    )
    await bot.db.log_activity(guild.id, member.id, "termination", reason)
    public = base_embed("Terminated", f"{member.mention} has been removed from Wisconsin State Patrol.", color=COLOR_DANGER)
    add_fields(
        public,
        [
            ("Previous rank", from_rank or "Unassigned", True),
            ("Roles removed", str(stripped), True),
            ("Reason", reason, False),
            ("Processed by", actor.mention, True),
            ("Authorized by", authorizing.mention, True),
        ],
    )
    dm = base_embed(
        "Removed from WSP",
        "Your Wisconsin State Patrol roles have been removed.",
        color=COLOR_DANGER,
    )
    dm.add_field(name="Reason", value=reason, inline=False)
    await bot.try_dm(member, dm)
    await bot.notify(guild, "command_log", public)
    await bot.notify(guild, "notifications", public)
    await bot.notify(guild, "hr_log", public)
    return f"{member.mention} has been fired. {stripped} WSP role(s) removed."


async def reset_shift_data(bot: WSPBot, guild: discord.Guild, actor: discord.abc.User) -> int:
    deleted = await bot.db.reset_shifts(guild.id)
    await bot.db.audit(
        guild.id,
        "shift_reset",
        actor_id=actor.id,
        actor_name=str(actor),
        details=f"Deleted {deleted} shift records and cleared duty quota minutes",
    )
    await bot.notify(
        guild,
        "shift_log",
        base_embed("Shift data reset", f"{actor.mention} cleared all shift records and duty quota totals."),
    )
    await bot.notify(
        guild,
        "command_log",
        base_embed("Shift data reset", f"{actor.mention} cleared all shift records."),
    )
    return deleted


async def set_status(
    bot: WSPBot,
    guild: discord.Guild,
    member: discord.Member,
    status: str,
    reason: str,
    actor: discord.abc.User,
) -> None:
    record = await ensure_personnel(bot, member)
    await bot.db.update_personnel(record["id"], status=status)
    await bot.db.audit(
        guild.id,
        f"personnel_{status}",
        actor_id=actor.id,
        actor_name=str(actor),
        target_id=member.id,
        target_name=str(member),
        details=reason,
    )
    title = {
        "suspended": "Suspension",
        "removed": "Roster removal",
        "active": "Reinstated",
        "loa": "Leave of absence",
    }.get(status, status.title())
    embed = base_embed(title, f"{member.mention} — {reason}")
    await bot.notify(guild, "hr_log", embed)
    if status in {"removed", "suspended"}:
        await bot.notify(guild, "notifications", embed)
    if status == "active":
        await bot.notify(guild, "command_log", embed)
        cfg = await bot.guild_config(guild.id)
        if record["rank_name"]:
            await sync_rank_roles(member, record["rank_name"], cfg)


async def decide_loa_record(
    bot: WSPBot,
    guild: discord.Guild,
    loa_id: int,
    status: str,
    note: str | None,
    actor: discord.abc.User,
) -> str | None:
    row = await bot.db.get_loa(loa_id)
    if row is None:
        return "LOA request not found."
    if str(row["guild_id"]) != str(guild.id):
        return "That request is not in this guild."
    if row["status"] != "pending":
        return f"This request is already `{row['status']}`."
    await bot.db.update_loa(loa_id, status=status, reviewer_id=str(actor.id), review_note=note)
    personnel = await bot.db.get_personnel(guild.id, int(row["discord_id"]))
    if personnel and status == "approved":
        await bot.db.update_personnel(personnel["id"], status="loa")
    if personnel and status == "denied" and personnel["status"] == "loa":
        await bot.db.update_personnel(personnel["id"], status="active")
    await bot.db.audit(
        guild.id,
        f"loa_{status}",
        actor_id=actor.id,
        actor_name=str(actor),
        target_id=row["discord_id"],
        details=f"#{loa_id} {note or ''}",
    )
    embed = success_embed(f"LOA {status}", f"<@{row['discord_id']}> • `{loa_id}`")
    if note:
        embed.add_field(name="Note", value=note, inline=False)
    await bot.notify(guild, "loa", embed)
    await bot.notify(guild, "notifications", embed)
    member = await bot.fetch_guild_user(guild, int(row["discord_id"]))
    if status == "approved":
        member_embed = success_embed("Leave approved", f"Your LOA `#{loa_id}` is approved.")
    else:
        member_embed = error_embed("Leave request not approved", f"Your LOA `#{loa_id}` was not approved.")
    if note:
        member_embed.add_field(name="Note", value=note, inline=False)
    await bot.try_dm(member, member_embed)
    return None


async def end_active_shift(bot: WSPBot, guild: discord.Guild, discord_id: int) -> None:
    row = await bot.db.active_shift(guild.id, discord_id)
    if row is None:
        return
    end = now_ts()
    if row["status"] == "paused" and row["pause_started"]:
        extra = max(0, end - int(row["pause_started"]))
        paused = int(row["paused_seconds"] or 0) + extra
        await bot.db.update_shift(row["id"], paused_seconds=paused, pause_started=None)
        row = await bot.db.get_shift(row["id"])
    duration = bot.db.effective_shift_seconds(row)
    await bot.db.update_shift(row["id"], status="completed", end_time=end, duration_seconds=duration)
    from wsp.cogs.quota import apply_shift_quota

    await apply_shift_quota(bot, guild.id, discord_id, duration)
    await bot.notify(
        guild,
        "shift_log",
        base_embed(
            "Shift ended",
            f"<@{discord_id}> shift `#{row['id']}` closed after **{format_duration(duration)}** (termination).",
        ),
    )
