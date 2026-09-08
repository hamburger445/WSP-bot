"""Shift management slash commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import PermissionLevel
from wsp.db import now_ts
from wsp.embeds import add_fields, base_embed, error_embed, format_duration, success_embed, ts, ts_rel
from wsp.permissions import has_level, resolve_level
from wsp.utils import current_shift_seconds, hms_to_seconds, member_can_start_shift, mention_or_id, sync_duty_role
from wsp.views.shifts import (
    ShiftActionView,
    ShiftMenuView,
    begin_shift,
    build_duty_board,
    build_leaderboard,
    build_shift_controls,
    complete_shift,
)

if TYPE_CHECKING:
    from wsp.bot import WSPBot


def admin_shift_embed(member: discord.Member, rows) -> discord.Embed:
    embed = base_embed("Shift administration", f"Controls for {member.mention}.")
    active = next((row for row in rows if row["status"] in {"active", "paused"}), None)
    add_fields(embed, [("Current status", str(active["status"]) if active else "off duty", True)])
    if rows:
        embed.add_field(
            name="Recent shifts",
            value="\n".join(
                f"`#{row['id']}` {row['status']} • {format_duration(row['duration_seconds'] or current_shift_seconds(row))} • {ts_rel(row['start_time'])}"
                for row in rows[:8]
            )[:1024],
            inline=False,
        )
    else:
        embed.add_field(name="Recent shifts", value="No shift records.", inline=False)
    return embed


class AdminShiftEditModal(discord.ui.Modal, title="Edit shift time"):
    hours = discord.ui.TextInput(label="Hours", default="0", max_length=4, required=True)
    minutes = discord.ui.TextInput(label="Minutes", default="0", max_length=2, required=True)
    seconds = discord.ui.TextInput(label="Seconds", default="0", max_length=2, required=True)

    def __init__(self, view: "AdminShiftView") -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            duration = hms_to_seconds(int(self.hours.value), int(self.minutes.value), int(self.seconds.value))
        except ValueError:
            await interaction.response.send_message("Enter whole numbers for hours, minutes, and seconds.", ephemeral=True)
            return
        if duration <= 0:
            await interaction.response.send_message("The duration must be greater than zero.", ephemeral=True)
            return
        row = await self.view.bot.db.get_shift(self.view.selected_shift_id or 0)
        if row is None or str(row["guild_id"]) != str(interaction.guild.id) or str(row["discord_id"]) != str(self.view.member.id):
            await interaction.response.send_message("That shift could not be found.", ephemeral=True)
            return
        was_open = row["status"] in {"active", "paused"}
        end_time = int(row["start_time"]) + duration
        await self.view.bot.db.update_shift(
            row["id"], status="completed", end_time=end_time, duration_seconds=duration, pause_started=None
        )
        if was_open:
            cfg = await self.view.bot.guild_config(interaction.guild.id)
            await sync_duty_role(self.view.member, cfg, False)
            from wsp.cogs.quota import apply_shift_quota

            await apply_shift_quota(self.view.bot, interaction.guild.id, self.view.member.id, duration)
        await self.view.bot.db.audit(
            interaction.guild.id,
            "shift_edit",
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
            target_id=self.view.member.id,
            target_name=str(self.view.member),
            details=f"#{row['id']} -> {format_duration(duration)}",
        )
        await self.view.bot.notify(
            interaction.guild,
            "shift_log",
            base_embed("Shift edited", f"{interaction.user.mention} set {self.view.member.mention} shift `#{row['id']}` to **{format_duration(duration)}**."),
        )
        self.view.rows = await self.view.bot.db.list_shifts(interaction.guild.id, self.view.member.id, limit=25)
        await interaction.response.send_message(f"Shift #{row['id']} updated to {format_duration(duration)}.", ephemeral=True)


class AdminShiftView(discord.ui.View):
    def __init__(self, bot: WSPBot, member: discord.Member, rows) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.member = member
        self.rows = rows
        self.selected_shift_id: int | None = None
        options = [
            discord.SelectOption(
                label=f"Shift #{row['id']} - {row['status']}",
                value=str(row["id"]),
                description=f"{format_duration(row['duration_seconds'] or current_shift_seconds(row))} • {ts_rel(row['start_time'])}",
            )
            for row in rows[:25]
        ]
        self.shift_select = discord.ui.Select(placeholder="Pick a shift to edit", options=options or [discord.SelectOption(label="No shifts", value="none")])
        self.shift_select.callback = self.select_shift
        self.add_item(self.shift_select)

    async def guard(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or await resolve_level(interaction) < PermissionLevel.SUPERVISOR:
            await interaction.response.send_message(embed=error_embed("Restricted"), ephemeral=True)
            return False
        return True

    async def select_shift(self, interaction: discord.Interaction) -> None:
        if not await self.guard(interaction):
            return
        value = self.shift_select.values[0]
        if value == "none":
            await interaction.response.send_message("This user has no shifts.", ephemeral=True)
            return
        self.selected_shift_id = int(value)
        await interaction.response.send_message(f"Selected shift #{value}.", ephemeral=True)

    @discord.ui.button(label="Start shift", style=discord.ButtonStyle.success, row=1)
    async def start(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self.guard(interaction):
            return
        result = await begin_shift(self.bot, interaction.guild, self.member, interaction.user)
        await _admin_reply(interaction, result)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, row=1)
    async def pause(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self.guard(interaction):
            return
        row = await self.bot.db.active_shift(interaction.guild.id, self.member.id)
        if not row or row["status"] != "active":
            await interaction.response.send_message(embed=error_embed("Cannot pause", "The user has no active shift."), ephemeral=True)
            return
        await self.bot.db.update_shift(row["id"], status="paused", pause_started=now_ts())
        await sync_duty_role(self.member, await self.bot.guild_config(interaction.guild.id), False)
        await interaction.response.send_message(embed=success_embed("Shift paused"), ephemeral=True)

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.primary, row=1)
    async def resume(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self.guard(interaction):
            return
        row = await self.bot.db.active_shift(interaction.guild.id, self.member.id)
        if not row or row["status"] != "paused":
            await interaction.response.send_message(embed=error_embed("Cannot resume", "The user has no paused shift."), ephemeral=True)
            return
        extra = max(0, now_ts() - int(row["pause_started"] or now_ts()))
        await self.bot.db.update_shift(
            row["id"], status="active", pause_started=None, paused_seconds=int(row["paused_seconds"] or 0) + extra
        )
        await sync_duty_role(self.member, await self.bot.guild_config(interaction.guild.id), True)
        await interaction.response.send_message(embed=success_embed("Shift resumed"), ephemeral=True)

    @discord.ui.button(label="End shift", style=discord.ButtonStyle.danger, row=1)
    async def end(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self.guard(interaction):
            return
        result = await complete_shift(self.bot, interaction.guild, self.member, interaction.user)
        await _admin_reply(interaction, result)

    @discord.ui.button(label="Most recent", style=discord.ButtonStyle.primary, row=2)
    async def recent(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self.guard(interaction):
            return
        if not self.rows:
            await interaction.response.send_message("This user has no shifts.", ephemeral=True)
            return
        self.selected_shift_id = int(self.rows[0]["id"])
        await interaction.response.send_message(f"Selected most recent shift #{self.selected_shift_id}.", ephemeral=True)

    @discord.ui.button(label="Edit selected", style=discord.ButtonStyle.primary, row=2)
    async def edit(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self.guard(interaction):
            return
        if self.selected_shift_id is None:
            await interaction.response.send_message("Pick a shift or choose Most recent first.", ephemeral=True)
            return
        await interaction.response.send_modal(AdminShiftEditModal(self))

    @discord.ui.button(label="View all shifts", style=discord.ButtonStyle.secondary, row=2)
    async def history(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self.guard(interaction):
            return
        rows = await self.bot.db.list_shifts(interaction.guild.id, self.member.id, limit=25)
        embed = base_embed(f"All shifts • {self.member}")
        embed.description = "\n".join(
            f"`#{row['id']}` {row['status']} • {format_duration(row['duration_seconds'] or current_shift_seconds(row))} • {ts(row['start_time'])}"
            for row in rows
        ) or "No records."
        await interaction.response.send_message(embed=embed, ephemeral=True)


class Shifts(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    shift = app_commands.Group(name="shift", description="Duty shifts")

    @shift.command(name="menu", description="Start, pause, resume, or end your shift.")
    async def menu(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        row = await self.bot.db.active_shift(interaction.guild.id, interaction.user.id)
        status = row["status"] if row else None
        cfg = await self.bot.guild_config(interaction.guild.id)
        embed = await build_shift_controls(status)
        await interaction.response.send_message(
            embed=embed,
            view=ShiftActionView(status, can_start=member_can_start_shift(interaction.user, cfg)),
        )

    @shift.command(name="data", description="Show who is on duty.")
    async def data(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        embed = await build_duty_board(self.bot, interaction.guild)
        await interaction.response.send_message(embed=embed, view=ShiftMenuView())

    @shift.command(name="status", description="Show who is on duty.")
    async def status(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        rows = await self.bot.db.list_active_shifts(interaction.guild.id)
        embed = base_embed("Active shifts")
        if not rows:
            embed.description = "No troopers are currently on duty."
        else:
            lines = []
            for row in rows:
                lines.append(
                    f"{mention_or_id(interaction.guild, row['discord_id'])} **{row['rank_name'] or ''}** "
                    f"{row['status']} • {format_duration(current_shift_seconds(row))} • started {ts_rel(row['start_time'])}"
                )
            embed.description = "\n".join(lines)[:4000]
        await interaction.response.send_message(embed=embed)

    @shift.command(name="leaderboard", description="Show duty time standings.")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        embed = await build_leaderboard(self.bot, interaction.guild)
        await interaction.response.send_message(embed=embed)

    @shift.command(name="history", description="View shift history.")
    async def history(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if not interaction.guild:
            return
        target = member or interaction.user
        if member and member.id != interaction.user.id:
            if await resolve_level(interaction) < PermissionLevel.SUPERVISOR:
                await interaction.response.send_message(
                    embed=error_embed("Restricted"),
                    ephemeral=True,
                )
                return
        rows = await self.bot.db.list_shifts(interaction.guild.id, target.id, limit=12)
        totals = await self.bot.db.shift_totals(interaction.guild.id, target.id)
        embed = base_embed(f"Shift history  •  {target}")
        if totals:
            add_fields(embed, [("All-time", format_duration(totals["total_seconds"]), True), ("Shifts", str(totals["shift_count"]), True)])
        embed.description = "\n".join(
            f"`#{r['id']}` {r['status']} {r['callsign'] or ''} {format_duration(r['duration_seconds'] or current_shift_seconds(r))} {ts(r['start_time'])}"
            for r in rows
        ) or "No records."
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @shift.command(name="admin", description="Open shift controls for a member.")
    @has_level(PermissionLevel.SUPERVISOR)
    async def admin(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        rows = await self.bot.db.list_shifts(interaction.guild.id, member.id, limit=25)
        await interaction.response.send_message(
            embed=admin_shift_embed(member, rows),
            view=AdminShiftView(self.bot, member, rows),
            ephemeral=True,
        )


async def _admin_reply(interaction: discord.Interaction, result) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if result.error:
        await interaction.response.send_message(embed=error_embed("Shift admin", result.error), ephemeral=True)
        return
    if interaction.guild and result.log:
        await bot.notify(interaction.guild, "shift_log", result.log)
    await interaction.response.send_message(embed=result.notice, ephemeral=True)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Shifts(bot))
