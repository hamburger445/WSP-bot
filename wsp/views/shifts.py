"""Persistent shift start/end/pause controls."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from wsp.constants import COLOR_NAVY, COLOR_SUCCESS
from wsp.db import now_ts
from wsp.embeds import add_fields, base_embed, error_embed, format_duration, success_embed, ts, ts_rel
from wsp.utils import current_shift_seconds, ensure_personnel, member_can_start_shift, mention_or_id, sync_duty_role

if TYPE_CHECKING:
    from wsp.bot import WSPBot


@dataclass
class ShiftResult:
    error: str | None = None
    notice: discord.Embed | None = None
    log: discord.Embed | None = None
    duration: int = 0
    shift_id: int | None = None


class ShiftMenuView(discord.ui.View):
    """Public duty board. No buttons are attached to this view."""

    def __init__(self) -> None:
        super().__init__(timeout=None)


_SHIFT_CONTROL_STYLE = {
    "start": ("Start shift", discord.ButtonStyle.success),
    "pause": ("Pause", discord.ButtonStyle.secondary),
    "resume": ("Resume", discord.ButtonStyle.primary),
    "end": ("End shift", discord.ButtonStyle.danger),
}


class ShiftControlButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wsp:shift:(?P<action>start|pause|resume|end):(?P<user_id>[0-9]+)"):
    def __init__(self, action: str = "start", user_id: int = 0, *, disabled: bool = False) -> None:
        label, style = _SHIFT_CONTROL_STYLE[action]
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                disabled=disabled,
                custom_id=f"wsp:shift:{action}:{user_id}",
            )
        )
        self.action = action
        self.user_id = user_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ):
        return cls(match["action"], int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await reply_interaction(interaction, error_embed("Restricted"))
            return
        if self.action == "start":
            await start_shift_for(interaction)
        elif self.action == "pause":
            await _pause_shift(interaction)
        elif self.action == "resume":
            await _resume_shift(interaction)
        else:
            await _end_shift(interaction)


class ShiftActionView(discord.ui.View):
    """Start / pause / resume / end. Only the panel owner can use the buttons."""

    def __init__(self, status: str | None = None, *, owner_id: int, can_start: bool = True) -> None:
        super().__init__(timeout=None)
        self.owner_id = owner_id
        on_duty = status in {"active", "paused"}
        paused = status == "paused"
        self.add_item(ShiftControlButton("start", owner_id, disabled=on_duty or not can_start))
        self.add_item(ShiftControlButton("pause", owner_id, disabled=(not on_duty) or paused))
        self.add_item(ShiftControlButton("resume", owner_id, disabled=not paused))
        self.add_item(ShiftControlButton("end", owner_id, disabled=not on_duty))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await reply_interaction(interaction, error_embed("Restricted"))
        return False


async def build_shift_controls(status: str | None = None) -> discord.Embed:
    if status == "paused":
        body = "Your shift is paused."
    elif status == "active":
        body = "You are on duty."
    else:
        body = "You are off duty."
    return base_embed("Shift controls", body, color=COLOR_NAVY)


def build_shift_management_embed(
    user: discord.abc.User,
    *,
    shift_count: int = 0,
    total_seconds: int = 0,
    status: str | None = None,
) -> discord.Embed:
    average = total_seconds // shift_count if shift_count else 0
    embed = base_embed(f"Shift Management: @{user.name}", color=COLOR_NAVY)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(
        name="All Time Information",
        value=(
            f"**Shift Count**: {shift_count}\n"
            f"**Total Duration**: {format_duration(total_seconds)}\n"
            f"**Average Duration**: {format_duration(average)}"
        ),
        inline=False,
    )
    embed.add_field(name="Current Status", value=str(status) if status else "Off duty", inline=False)
    return embed


async def build_duty_board(bot: WSPBot, guild: discord.Guild) -> discord.Embed:
    embed = base_embed(
        "Duty board",
        "Who is on duty.",
        color=COLOR_NAVY,
    )
    active = await bot.db.list_active_shifts(guild.id)
    if not active:
        embed.add_field(name="On duty", value="No troopers are currently on duty.", inline=False)
    else:
        lines = []
        for row in active[:15]:
            lines.append(
                f"{mention_or_id(guild, row['discord_id'])} "
                f"**{row['status']}** • {format_duration(current_shift_seconds(row))}"
            )
        embed.add_field(name="On duty", value="\n".join(lines)[:1024], inline=False)
    board = await bot.db.shift_leaderboard(guild.id, limit=10)
    if not board:
        embed.add_field(name="Leaderboard", value="No completed shifts yet.", inline=False)
    else:
        embed.add_field(
            name="Leaderboard",
            value="\n".join(
                f"**{i + 1}.** {mention_or_id(guild, r['discord_id'])} — "
                f"{format_duration(r['total_seconds'])} ({r['shift_count']} shifts)"
                for i, r in enumerate(board)
            )[:1024],
            inline=False,
        )
    return embed


async def build_leaderboard(bot: WSPBot, guild: discord.Guild) -> discord.Embed:
    rows = await bot.db.shift_leaderboard(guild.id)
    embed = base_embed("Duty leaderboard", color=COLOR_NAVY)
    if not rows:
        embed.description = "No completed shifts yet."
    else:
        embed.description = "\n".join(
            f"**{i + 1}.** {mention_or_id(guild, r['discord_id'])} — "
            f"{format_duration(r['total_seconds'])} ({r['shift_count']} shifts)"
            for i, r in enumerate(rows)
        )
    return embed


async def build_personal_shift(bot: WSPBot, guild: discord.Guild, user: discord.abc.User, row) -> discord.Embed:
    if row is None:
        embed = base_embed("Your shift", "You are off duty.", color=COLOR_NAVY)
        embed.add_field(name="Status", value="Off duty", inline=True)
        return embed
    elapsed = format_duration(current_shift_seconds(row))
    embed = base_embed(
        "Your shift",
        f"You are **{row['status']}**.",
        color=COLOR_NAVY,
    )
    add_fields(
        embed,
        [
            ("Status", str(row["status"]), True),
            ("Elapsed", elapsed, True),
            ("Started", ts(row["start_time"]), True),
            ("Shift ID", f"#{row['id']}", True),
        ],
    )
    return embed


async def begin_shift(
    bot: WSPBot,
    guild: discord.Guild,
    member: discord.Member,
    actor: discord.abc.User,
    *,
    require_certified: bool = True,
) -> ShiftResult:
    cfg = await bot.guild_config(guild.id)
    if require_certified and not member_can_start_shift(member, cfg):
        return ShiftResult(error="Cannot start shift.")
    existing = await bot.db.active_shift(guild.id, member.id)
    if existing:
        return ShiftResult(error=f"A shift is already active (started {ts_rel(existing['start_time'])}).")
    record = await ensure_personnel(bot, member)
    rank_name = record["rank_name"] if record else None
    callsign = (record["callsign"] if record else None) or None
    shift_id = await bot.db.start_shift(guild.id, member.id, rank_name, callsign)
    await sync_duty_role(member, cfg, True)
    await bot.db.log_activity(guild.id, member.id, "shift_start", f"Shift #{shift_id}")
    await bot.db.audit(
        guild.id,
        "shift_start",
        actor_id=actor.id,
        actor_name=str(actor),
        target_id=member.id,
        target_name=str(member),
        details=f"Shift #{shift_id}",
    )
    notice = success_embed("Shift started", f"**{rank_name or 'Trooper'}** is now on duty.")
    add_fields(notice, [("Started", ts(now_ts()), True), ("Shift ID", f"#{shift_id}", True)])
    if actor.id != member.id:
        notice.add_field(name="Started by", value=actor.mention, inline=True)
    log_embed = base_embed("Shift started", f"{member.mention} is now on duty.", color=COLOR_SUCCESS)
    add_fields(log_embed, [("Shift", f"#{shift_id}", True), ("Started", ts(now_ts()), True)])
    return ShiftResult(notice=notice, log=log_embed, shift_id=shift_id)


async def complete_shift(bot: WSPBot, guild: discord.Guild, member: discord.Member, actor: discord.abc.User) -> ShiftResult:
    row = await bot.db.active_shift(guild.id, member.id)
    if not row:
        return ShiftResult(error="No active shift.")
    end = now_ts()
    if row["status"] == "paused" and row["pause_started"]:
        extra = max(0, end - int(row["pause_started"]))
        paused = int(row["paused_seconds"] or 0) + extra
        await bot.db.update_shift(row["id"], paused_seconds=paused, pause_started=None)
        row = await bot.db.get_shift(row["id"])
    duration = bot.db.effective_shift_seconds(row)
    await bot.db.update_shift(row["id"], status="completed", end_time=end, duration_seconds=duration)
    cfg = await bot.guild_config(guild.id)
    await sync_duty_role(member, cfg, False)
    await bot.db.log_activity(guild.id, member.id, "shift_end", format_duration(duration))
    await bot.db.audit(
        guild.id,
        "shift_end",
        actor_id=actor.id,
        actor_name=str(actor),
        target_id=member.id,
        target_name=str(member),
        details=f"Shift #{row['id']} • {format_duration(duration)}",
    )
    from wsp.cogs.quota import apply_shift_quota

    await apply_shift_quota(bot, guild.id, member.id, duration)
    notice = success_embed("Shift ended", f"On-duty time recorded: **{format_duration(duration)}**.")
    add_fields(notice, [("Started", ts(row["start_time"]), True), ("Ended", ts(end), True)])
    if actor.id != member.id:
        notice.add_field(name="Ended by", value=actor.mention, inline=True)
    log_embed = base_embed(
        "Shift ended",
        f"{member.mention} ended shift `#{row['id']}` after **{format_duration(duration)}**.",
        color=COLOR_NAVY,
    )
    add_fields(log_embed, [("Started", ts(row["start_time"]), True), ("Ended", ts(end), True)])
    return ShiftResult(notice=notice, log=log_embed, duration=duration, shift_id=int(row["id"]))


async def pause_shift(bot: WSPBot, guild: discord.Guild, member: discord.Member, actor: discord.abc.User) -> ShiftResult:
    row = await bot.db.active_shift(guild.id, member.id)
    if not row or row["status"] != "active":
        return ShiftResult(error="No active shift.")
    await bot.db.update_shift(row["id"], status="paused", pause_started=now_ts())
    cfg = await bot.guild_config(guild.id)
    await sync_duty_role(member, cfg, False)
    await bot.db.audit(
        guild.id,
        "shift_pause",
        actor_id=actor.id,
        actor_name=str(actor),
        target_id=member.id,
        target_name=str(member),
        details=f"Shift #{row['id']}",
    )
    notice = success_embed("Shift paused", "Resume when you return to duty.")
    if actor.id != member.id:
        notice.add_field(name="Paused by", value=actor.mention, inline=True)
    log_embed = base_embed("Shift paused", f"{member.mention} paused shift `#{row['id']}`.", color=COLOR_NAVY)
    return ShiftResult(notice=notice, log=log_embed, shift_id=int(row["id"]))


async def resume_shift(bot: WSPBot, guild: discord.Guild, member: discord.Member, actor: discord.abc.User) -> ShiftResult:
    row = await bot.db.active_shift(guild.id, member.id)
    if not row or row["status"] != "paused":
        return ShiftResult(error="No paused shift.")
    extra = max(0, now_ts() - int(row["pause_started"] or now_ts()))
    await bot.db.update_shift(
        row["id"],
        status="active",
        pause_started=None,
        paused_seconds=int(row["paused_seconds"] or 0) + extra,
    )
    cfg = await bot.guild_config(guild.id)
    await sync_duty_role(member, cfg, True)
    await bot.db.audit(
        guild.id,
        "shift_resume",
        actor_id=actor.id,
        actor_name=str(actor),
        target_id=member.id,
        target_name=str(member),
        details=f"Shift #{row['id']}",
    )
    notice = success_embed("Shift resumed")
    if actor.id != member.id:
        notice.add_field(name="Resumed by", value=actor.mention, inline=True)
    log_embed = base_embed("Shift resumed", f"{member.mention} resumed shift `#{row['id']}`.", color=COLOR_SUCCESS)
    return ShiftResult(notice=notice, log=log_embed, shift_id=int(row["id"]))


async def acknowledge(interaction: discord.Interaction, *, ephemeral: bool = False) -> bool:
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        return True
    except discord.NotFound:
        return False
    except discord.HTTPException as exc:
        if getattr(exc, "code", None) in {10062, 40060}:
            return interaction.response.is_done()
        raise


async def reply_interaction(interaction: discord.Interaction, embed: discord.Embed, *, ephemeral: bool = True) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
    except discord.HTTPException:
        pass


async def _send_personal_controls(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
        return
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    row = await bot.db.active_shift(interaction.guild.id, interaction.user.id)
    status = row["status"] if row else None
    embed = await build_personal_shift(bot, interaction.guild, interaction.user, row)
    await interaction.response.send_message(
        embed=embed,
        view=ShiftActionView(status, owner_id=interaction.user.id),
        ephemeral=True,
    )


async def start_shift_for(interaction: discord.Interaction) -> None:
    if not await acknowledge(interaction):
        return
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await reply_interaction(interaction, error_embed("Unavailable"))
        return
    result = await begin_shift(bot, interaction.guild, interaction.user, interaction.user)
    if result.error:
        await reply_interaction(interaction, error_embed("Cannot start shift", result.error))
        return
    await _finish_shift_action(interaction, result.notice, result.log)


async def _pause_shift(interaction: discord.Interaction) -> None:
    if not await acknowledge(interaction):
        return
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await reply_interaction(interaction, error_embed("Unavailable"))
        return
    result = await pause_shift(bot, interaction.guild, interaction.user, interaction.user)
    if result.error:
        await reply_interaction(interaction, error_embed("Cannot pause shift", result.error))
        return
    await _finish_shift_action(interaction, result.notice, result.log)


async def _resume_shift(interaction: discord.Interaction) -> None:
    if not await acknowledge(interaction):
        return
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await reply_interaction(interaction, error_embed("Unavailable"))
        return
    result = await resume_shift(bot, interaction.guild, interaction.user, interaction.user)
    if result.error:
        await reply_interaction(interaction, error_embed("Cannot resume shift", result.error))
        return
    await _finish_shift_action(interaction, result.notice, result.log)


async def _end_shift(interaction: discord.Interaction) -> None:
    if not await acknowledge(interaction):
        return
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await reply_interaction(interaction, error_embed("Unavailable"))
        return
    result = await complete_shift(bot, interaction.guild, interaction.user, interaction.user)
    if result.error:
        await reply_interaction(interaction, error_embed("Cannot end shift", result.error))
        return
    await _finish_shift_action(interaction, result.notice, result.log)


async def _finish_shift_action(
    interaction: discord.Interaction,
    notice: discord.Embed | None,
    log_embed: discord.Embed | None,
) -> None:
    if notice is None or log_embed is None:
        return
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    guild = interaction.guild
    if guild:
        await bot.notify(guild, "shift_log", log_embed)
        await _refresh_duty_board_message(interaction)
        row = await bot.db.active_shift(guild.id, interaction.user.id)
        status = row["status"] if row else None
        title = _message_title(interaction)
        if title in {"Your shift", "Shift controls"} or title.startswith("Shift Management"):
            if title == "Shift controls":
                panel = await build_shift_controls(status)
            elif title.startswith("Shift Management"):
                totals = await bot.db.shift_totals(guild.id, interaction.user.id)
                panel = build_shift_management_embed(
                    interaction.user,
                    shift_count=int(totals["shift_count"] or 0) if totals else 0,
                    total_seconds=int(totals["total_seconds"] or 0) if totals else 0,
                    status=status,
                )
            else:
                panel = await build_personal_shift(bot, guild, interaction.user, row)
            if notice.title:
                panel.add_field(name="Update", value=notice.title, inline=False)
            cfg = await bot.guild_config(guild.id)
            can_start = isinstance(interaction.user, discord.Member) and member_can_start_shift(interaction.user, cfg)
            view = ShiftActionView(status, owner_id=interaction.user.id, can_start=can_start)
            try:
                if interaction.message:
                    await interaction.message.edit(embed=panel, view=view)
                else:
                    await interaction.edit_original_response(embed=panel, view=view)
            except discord.HTTPException:
                await reply_interaction(interaction, notice)
                return
            if title == "Shift controls" or title.startswith("Shift Management"):
                await reply_interaction(interaction, notice)
            return
    await reply_interaction(interaction, notice)


async def _refresh_duty_board_message(interaction: discord.Interaction) -> None:
    if not interaction.guild or interaction.message is None:
        return
    if interaction.message.author != interaction.client.user:
        return
    if _message_title(interaction) != "Duty board":
        return
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    try:
        embed = await build_duty_board(bot, interaction.guild)
        await interaction.message.edit(embed=embed, view=ShiftMenuView())
    except discord.HTTPException:
        pass


def _message_title(interaction: discord.Interaction) -> str:
    if interaction.message and interaction.message.embeds:
        return interaction.message.embeds[0].title or ""
    return ""


async def _history(interaction: discord.Interaction) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild:
        await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
        return
    rows = await bot.db.list_shifts(interaction.guild.id, interaction.user.id, limit=8)
    totals = await bot.db.shift_totals(interaction.guild.id, interaction.user.id)
    embed = base_embed("Shift history")
    if totals:
        embed.add_field(name="All-time duty", value=format_duration(totals["total_seconds"]), inline=True)
        embed.add_field(name="Completed shifts", value=str(totals["shift_count"]), inline=True)
    if not rows:
        embed.description = "No shift records yet."
    else:
        lines = []
        for row in rows:
            dur = row["duration_seconds"] if row["status"] == "completed" else current_shift_seconds(row)
            lines.append(f"`#{row['id']}` {row['status']} • {format_duration(dur)} • {ts_rel(row['start_time'])}")
        embed.add_field(name="Recent", value="\n".join(lines)[:1024], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)
