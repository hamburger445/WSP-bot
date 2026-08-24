"""Shift management slash commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import PermissionLevel
from wsp.db import now_ts
from wsp.embeds import add_fields, base_embed, error_embed, format_duration, success_embed, ts, ts_rel
from wsp.permissions import has_level
from wsp.utils import current_shift_seconds, mention_or_id
from wsp.views.shifts import ShiftMenuView, build_duty_board, build_leaderboard, start_shift_for

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class Shifts(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    shift = app_commands.Group(name="shift", description="Duty shift management")

    @shift.command(name="menu", description="Post the public duty board and leaderboard.")
    async def menu(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        embed = await build_duty_board(self.bot, interaction.guild)
        await interaction.response.send_message(embed=embed, view=ShiftMenuView())

    @shift.command(name="status", description="Show who is currently on duty.")
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
                    f"{mention_or_id(interaction.guild, row['discord_id'])} `{row['callsign'] or '—'}` **{row['rank_name'] or ''}** "
                    f"{row['status']} • {format_duration(current_shift_seconds(row))} • started {ts_rel(row['start_time'])}"
                )
            embed.description = "\n".join(lines)[:4000]
        await interaction.response.send_message(embed=embed)

    @shift.command(name="leaderboard", description="Duty time leaderboard.")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        embed = await build_leaderboard(self.bot, interaction.guild)
        await interaction.response.send_message(embed=embed)

    @shift.command(name="history", description="View shift history for a member.")
    async def history(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if not interaction.guild:
            return
        target = member or interaction.user
        if member and member.id != interaction.user.id:
            from wsp.permissions import resolve_level

            if await resolve_level(interaction) < PermissionLevel.SUPERVISOR:
                await interaction.response.send_message(embed=error_embed("Restricted", "Supervisors and above can view other members' history."), ephemeral=True)
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

    @shift.command(name="correct", description="Manually correct a shift record (supervisor/HR).")
    @has_level(PermissionLevel.SUPERVISOR)
    async def correct(
        self,
        interaction: discord.Interaction,
        shift_id: int,
        duration_minutes: int | None = None,
        callsign: str | None = None,
        notes: str | None = None,
        force_end: bool = False,
    ) -> None:
        if not interaction.guild:
            return
        row = await self.bot.db.get_shift(shift_id)
        if row is None or str(row["guild_id"]) != str(interaction.guild.id):
            await interaction.response.send_message(embed=error_embed("Not found"), ephemeral=True)
            return
        fields: dict = {"notes": notes or row["notes"]}
        if callsign:
            fields["callsign"] = callsign
        if duration_minutes is not None:
            fields["duration_seconds"] = duration_minutes * 60
            fields["status"] = "completed"
            fields["end_time"] = int(row["start_time"]) + duration_minutes * 60
        if force_end and row["status"] in {"active", "paused"}:
            end = now_ts()
            duration = self.bot.db.effective_shift_seconds(row)
            fields.update(status="completed", end_time=end, duration_seconds=duration)
        await self.bot.db.update_shift(shift_id, **fields)
        await self.bot.db.audit(
            interaction.guild.id, "shift_correct", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=row["discord_id"], details=f"#{shift_id} {fields}",
        )
        await interaction.response.send_message(embed=success_embed("Shift corrected", f"Record `#{shift_id}` updated."), ephemeral=True)

    @shift.command(name="start", description="Start a duty shift.")
    async def start(self, interaction: discord.Interaction, callsign: str) -> None:
        await start_shift_for(interaction, callsign)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Shifts(bot))
