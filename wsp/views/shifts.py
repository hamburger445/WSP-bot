"""Persistent shift start/end/pause controls."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from wsp.constants import COLOR_NAVY
from wsp.db import now_ts
from wsp.embeds import add_fields, base_embed, error_embed, format_duration, success_embed, ts, ts_rel
from wsp.utils import current_shift_seconds, ensure_personnel, mention_or_id

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class ShiftMenuView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Start shift", style=discord.ButtonStyle.success, custom_id="wsp:shift:start")
    async def start(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ShiftStartModal())

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, custom_id="wsp:shift:pause")
    async def pause(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _pause_shift(interaction)

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.primary, custom_id="wsp:shift:resume")
    async def resume(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _resume_shift(interaction)

    @discord.ui.button(label="End shift", style=discord.ButtonStyle.danger, custom_id="wsp:shift:end")
    async def end(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _end_shift(interaction)

    @discord.ui.button(label="My history", style=discord.ButtonStyle.secondary, custom_id="wsp:shift:history", row=1)
    async def history(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _history(interaction)

    @discord.ui.button(label="Leaderboard", style=discord.ButtonStyle.secondary, custom_id="wsp:shift:board", row=1)
    async def board(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
            return
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        embed = await build_leaderboard(bot, interaction.guild)
        await interaction.response.send_message(embed=embed)

    @discord.ui.button(label="Refresh board", style=discord.ButtonStyle.primary, custom_id="wsp:shift:refresh", row=1)
    async def refresh(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
            return
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        embed = await build_duty_board(bot, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=ShiftMenuView())


class ShiftStartModal(discord.ui.Modal, title="Start duty shift"):
    callsign = discord.ui.TextInput(label="Callsign", placeholder="e.g. 1A-12", max_length=24)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await start_shift_for(interaction, str(self.callsign.value).strip())


async def build_duty_board(bot: WSPBot, guild: discord.Guild) -> discord.Embed:
    embed = base_embed(
        "Duty board",
        "Use the buttons to start, pause, resume, or end **your** shift. "
        "You will be asked for a callsign when you start. Confirmations stay private to you.\n"
        "Everyone can see who is on duty and the leaderboard.",
        color=COLOR_NAVY,
    )
    active = await bot.db.list_active_shifts(guild.id)
    if not active:
        embed.add_field(name="On duty", value="No troopers are currently on duty.", inline=False)
    else:
        lines = []
        for row in active[:15]:
            lines.append(
                f"{mention_or_id(guild, row['discord_id'])} `{row['callsign'] or '—'}` "
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


async def _maybe_refresh_board(interaction: discord.Interaction) -> None:
    if not interaction.guild or interaction.message is None:
        return
    if interaction.message.author != interaction.client.user:
        return
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    try:
        embed = await build_duty_board(bot, interaction.guild)
        await interaction.message.edit(embed=embed, view=ShiftMenuView())
    except discord.HTTPException:
        pass


async def start_shift_for(interaction: discord.Interaction, callsign: str) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
        return
    member = interaction.user
    existing = await bot.db.active_shift(interaction.guild.id, member.id)
    if existing:
        await interaction.response.send_message(
            embed=error_embed("Shift already active", f"Started {ts_rel(existing['start_time'])}. End or pause that shift first."),
            ephemeral=True,
        )
        return
    record = await ensure_personnel(bot, member)
    if record:
        await bot.db.update_personnel(record["id"], callsign=callsign)
        record = await bot.db.get_personnel(interaction.guild.id, member.id)
    rank_name = record["rank_name"] if record else None
    shift_id = await bot.db.start_shift(interaction.guild.id, member.id, rank_name, callsign)
    await bot.db.log_activity(interaction.guild.id, member.id, "shift_start", f"Callsign {callsign}")
    await bot.db.audit(
        interaction.guild.id,
        "shift_start",
        actor_id=member.id,
        actor_name=str(member),
        details=f"Shift #{shift_id} • {callsign}",
    )
    embed = success_embed("Shift started", f"**{rank_name or 'Trooper'}** `{callsign}` is now on duty.")
    add_fields(embed, [("Started", ts(now_ts()), True), ("Shift ID", f"#{shift_id}", True)])
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await _maybe_refresh_board(interaction)


async def _pause_shift(interaction: discord.Interaction) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild:
        await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
        return
    row = await bot.db.active_shift(interaction.guild.id, interaction.user.id)
    if not row:
        await interaction.response.send_message(embed=error_embed("No active shift"), ephemeral=True)
        return
    if row["status"] == "paused":
        await interaction.response.send_message(embed=error_embed("Already paused"), ephemeral=True)
        return
    await bot.db.update_shift(row["id"], status="paused", pause_started=now_ts())
    await interaction.response.send_message(embed=success_embed("Shift paused", "Resume when you return to duty."), ephemeral=True)
    await _maybe_refresh_board(interaction)


async def _resume_shift(interaction: discord.Interaction) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild:
        await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
        return
    row = await bot.db.active_shift(interaction.guild.id, interaction.user.id)
    if not row or row["status"] != "paused":
        await interaction.response.send_message(embed=error_embed("No paused shift"), ephemeral=True)
        return
    extra = max(0, now_ts() - int(row["pause_started"] or now_ts()))
    await bot.db.update_shift(
        row["id"],
        status="active",
        pause_started=None,
        paused_seconds=int(row["paused_seconds"] or 0) + extra,
    )
    await interaction.response.send_message(embed=success_embed("Shift resumed"), ephemeral=True)
    await _maybe_refresh_board(interaction)


async def _end_shift(interaction: discord.Interaction) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild:
        await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
        return
    row = await bot.db.active_shift(interaction.guild.id, interaction.user.id)
    if not row:
        await interaction.response.send_message(embed=error_embed("No active shift"), ephemeral=True)
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
    embed = success_embed("Shift ended", f"On-duty time recorded: **{format_duration(duration)}**.")
    add_fields(
        embed,
        [
            ("Started", ts(row["start_time"]), True),
            ("Ended", ts(end), True),
            ("Callsign", row["callsign"] or "—", True),
        ],
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await _maybe_refresh_board(interaction)


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
