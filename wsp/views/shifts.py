"""Persistent shift start/end/pause controls."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from wsp.constants import COLOR_NAVY, COLOR_SUCCESS
from wsp.db import now_ts
from wsp.embeds import add_fields, base_embed, error_embed, format_duration, success_embed, ts, ts_rel
from wsp.utils import current_shift_seconds, ensure_personnel, mention_or_id

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class ShiftMenuView(discord.ui.View):
    """Public duty board controls. Shift start/pause/end live on ShiftActionView."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="My shift", style=discord.ButtonStyle.success, custom_id="wsp:shift:mine")
    async def mine(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _send_personal_controls(interaction)

    @discord.ui.button(label="My history", style=discord.ButtonStyle.secondary, custom_id="wsp:shift:history")
    async def history(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _history(interaction)

    @discord.ui.button(label="Leaderboard", style=discord.ButtonStyle.secondary, custom_id="wsp:shift:board")
    async def board(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
            return
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        embed = await build_leaderboard(bot, interaction.guild)
        await interaction.response.send_message(embed=embed)

    @discord.ui.button(label="Refresh board", style=discord.ButtonStyle.primary, custom_id="wsp:shift:refresh")
    async def refresh(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
            return
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        embed = await build_duty_board(bot, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=ShiftMenuView())


class ShiftActionView(discord.ui.View):
    """Start / pause / resume / end. Disabled state is set per message for that trooper."""

    def __init__(self, status: str | None = None, *, lock_buttons: bool = True) -> None:
        super().__init__(timeout=None)
        if not lock_buttons:
            return
        on_duty = status in {"active", "paused"}
        paused = status == "paused"
        self.start.disabled = on_duty
        self.pause.disabled = (not on_duty) or paused
        self.resume.disabled = not paused
        self.end.disabled = not on_duty

    @discord.ui.button(label="Start shift", style=discord.ButtonStyle.success, custom_id="wsp:shift:start")
    async def start(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await start_shift_for(interaction)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, custom_id="wsp:shift:pause")
    async def pause(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _pause_shift(interaction)

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.primary, custom_id="wsp:shift:resume")
    async def resume(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _resume_shift(interaction)

    @discord.ui.button(label="End shift", style=discord.ButtonStyle.danger, custom_id="wsp:shift:end")
    async def end(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _end_shift(interaction)


async def build_duty_board(bot: WSPBot, guild: discord.Guild) -> discord.Embed:
    embed = base_embed(
        "Duty board",
        "Use **My shift** for your start / pause / end controls. "
        "Confirmations stay private. Everyone can see who is on duty and the leaderboard.",
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
        embed = base_embed("Your shift", "You are off duty. Start when you go on patrol.", color=COLOR_NAVY)
        embed.add_field(name="Status", value="Off duty", inline=True)
        return embed
    elapsed = format_duration(current_shift_seconds(row))
    embed = base_embed(
        "Your shift",
        f"You are **{row['status']}**. Pause and end are available while you are on duty.",
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


async def _send_personal_controls(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
        return
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    row = await bot.db.active_shift(interaction.guild.id, interaction.user.id)
    status = row["status"] if row else None
    embed = await build_personal_shift(bot, interaction.guild, interaction.user, row)
    await interaction.response.send_message(embed=embed, view=ShiftActionView(status), ephemeral=True)


async def start_shift_for(interaction: discord.Interaction) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await _reply_ephemeral(interaction, error_embed("Unavailable"))
        return
    member = interaction.user
    existing = await bot.db.active_shift(interaction.guild.id, member.id)
    if existing:
        await _reply_ephemeral(
            interaction,
            error_embed("Shift already active", f"Started {ts_rel(existing['start_time'])}. End or pause that shift first."),
        )
        return
    record = await ensure_personnel(bot, member)
    rank_name = record["rank_name"] if record else None
    callsign = (record["callsign"] if record else None) or None
    shift_id = await bot.db.start_shift(interaction.guild.id, member.id, rank_name, callsign)
    await bot.db.log_activity(interaction.guild.id, member.id, "shift_start", f"Shift #{shift_id}")
    await bot.db.audit(
        interaction.guild.id,
        "shift_start",
        actor_id=member.id,
        actor_name=str(member),
        details=f"Shift #{shift_id}",
    )
    notice = success_embed("Shift started", f"**{rank_name or 'Trooper'}** is now on duty.")
    add_fields(notice, [("Started", ts(now_ts()), True), ("Shift ID", f"#{shift_id}", True)])
    log_embed = base_embed("Shift started", f"{member.mention} is now on duty.", color=COLOR_SUCCESS)
    add_fields(log_embed, [("Shift", f"#{shift_id}", True), ("Started", ts(now_ts()), True)])
    await _finish_shift_action(interaction, notice, log_embed)


async def _pause_shift(interaction: discord.Interaction) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild:
        await _reply_ephemeral(interaction, error_embed("Unavailable"))
        return
    row = await bot.db.active_shift(interaction.guild.id, interaction.user.id)
    if not row:
        await _reply_ephemeral(interaction, error_embed("No active shift"))
        return
    if row["status"] == "paused":
        await _reply_ephemeral(interaction, error_embed("Already paused"))
        return
    await bot.db.update_shift(row["id"], status="paused", pause_started=now_ts())
    await bot.db.audit(
        interaction.guild.id,
        "shift_pause",
        actor_id=interaction.user.id,
        actor_name=str(interaction.user),
        details=f"Shift #{row['id']}",
    )
    notice = success_embed("Shift paused", "Resume when you return to duty.")
    log_embed = base_embed("Shift paused", f"{interaction.user.mention} paused shift `#{row['id']}`.", color=COLOR_NAVY)
    await _finish_shift_action(interaction, notice, log_embed)


async def _resume_shift(interaction: discord.Interaction) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild:
        await _reply_ephemeral(interaction, error_embed("Unavailable"))
        return
    row = await bot.db.active_shift(interaction.guild.id, interaction.user.id)
    if not row or row["status"] != "paused":
        await _reply_ephemeral(interaction, error_embed("No paused shift"))
        return
    extra = max(0, now_ts() - int(row["pause_started"] or now_ts()))
    await bot.db.update_shift(
        row["id"],
        status="active",
        pause_started=None,
        paused_seconds=int(row["paused_seconds"] or 0) + extra,
    )
    await bot.db.audit(
        interaction.guild.id,
        "shift_resume",
        actor_id=interaction.user.id,
        actor_name=str(interaction.user),
        details=f"Shift #{row['id']}",
    )
    notice = success_embed("Shift resumed")
    log_embed = base_embed("Shift resumed", f"{interaction.user.mention} resumed shift `#{row['id']}`.", color=COLOR_SUCCESS)
    await _finish_shift_action(interaction, notice, log_embed)


async def _end_shift(interaction: discord.Interaction) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild:
        await _reply_ephemeral(interaction, error_embed("Unavailable"))
        return
    row = await bot.db.active_shift(interaction.guild.id, interaction.user.id)
    if not row:
        await _reply_ephemeral(interaction, error_embed("No active shift"))
        return
    end = now_ts()
    if row["status"] == "paused" and row["pause_started"]:
        extra = max(0, end - int(row["pause_started"]))
        paused = int(row["paused_seconds"] or 0) + extra
        await bot.db.update_shift(row["id"], paused_seconds=paused, pause_started=None)
        row = await bot.db.get_shift(row["id"])
    duration = bot.db.effective_shift_seconds(row)
    await bot.db.update_shift(row["id"], status="completed", end_time=end, duration_seconds=duration)
    await bot.db.log_activity(interaction.guild.id, interaction.user.id, "shift_end", format_duration(duration))
    await bot.db.audit(
        interaction.guild.id,
        "shift_end",
        actor_id=interaction.user.id,
        actor_name=str(interaction.user),
        details=f"Shift #{row['id']} • {format_duration(duration)}",
    )
    from wsp.cogs.quota import apply_shift_quota

    await apply_shift_quota(bot, interaction.guild.id, interaction.user.id, duration)
    notice = success_embed("Shift ended", f"On-duty time recorded: **{format_duration(duration)}**.")
    add_fields(
        notice,
        [
            ("Started", ts(row["start_time"]), True),
            ("Ended", ts(end), True),
        ],
    )
    log_embed = base_embed(
        "Shift ended",
        f"{interaction.user.mention} ended shift `#{row['id']}` after **{format_duration(duration)}**.",
        color=COLOR_NAVY,
    )
    add_fields(log_embed, [("Started", ts(row["start_time"]), True), ("Ended", ts(end), True)])
    await _finish_shift_action(interaction, notice, log_embed)


async def _finish_shift_action(
    interaction: discord.Interaction,
    notice: discord.Embed,
    log_embed: discord.Embed,
) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    guild = interaction.guild
    if guild:
        await bot.notify(guild, "shift_log", log_embed)
        await _refresh_duty_board_message(interaction)
        row = await bot.db.active_shift(guild.id, interaction.user.id)
        status = row["status"] if row else None
        title = _message_title(interaction)
        if title == "Your shift":
            personal = await build_personal_shift(bot, guild, interaction.user, row)
            if notice.title:
                personal.add_field(name="Update", value=notice.title, inline=False)
            view = ShiftActionView(status)
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=personal, view=view)
            elif interaction.message:
                try:
                    await interaction.message.edit(embed=personal, view=view)
                except discord.HTTPException:
                    await _reply_ephemeral(interaction, notice)
            return
    await _reply_ephemeral(interaction, notice)


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


async def _reply_ephemeral(interaction: discord.Interaction, embed: discord.Embed) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def _history(interaction: discord.Interaction) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild:
        await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
        return
    rows = await bot.db.list_shifts(interaction.guild.id, interaction.user.id, limit=8)
    totals = await bot.db.shift_totals(interaction.guild.id, interaction.user.id)
    embed = base_embed("Shift history", "Your recent duty logs.")
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
