"""Shift management slash commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import PermissionLevel
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


class Shifts(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    shift = app_commands.Group(name="shift", description="Duty shifts")
    admin = app_commands.Group(name="admin", description="Manage a member's shifts.", parent=shift)

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

    @admin.command(name="start", description="Start a member's shift.")
    @has_level(PermissionLevel.SUPERVISOR)
    async def admin_start(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        result = await begin_shift(self.bot, interaction.guild, member, interaction.user)
        await _admin_reply(interaction, result)

    @admin.command(name="end", description="End a member's shift.")
    @has_level(PermissionLevel.SUPERVISOR)
    async def admin_end(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        result = await complete_shift(self.bot, interaction.guild, member, interaction.user)
        await _admin_reply(interaction, result)

    @admin.command(name="edit", description="Change a shift's duration.")
    @has_level(PermissionLevel.SUPERVISOR)
    @app_commands.describe(
        member="Member",
        shift_id="Shift ID",
        hours="Hours",
        minutes="Minutes",
        seconds="Seconds",
    )
    async def admin_edit(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        shift_id: int,
        hours: int = 0,
        minutes: int = 0,
        seconds: int = 0,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        duration = hms_to_seconds(hours, minutes, seconds)
        if duration <= 0:
            await interaction.response.send_message(
                embed=error_embed("Invalid duration", "Enter hours, minutes, and/or seconds greater than zero."),
                ephemeral=True,
            )
            return
        row = await self.bot.db.get_shift(shift_id)
        if row is None or str(row["guild_id"]) != str(interaction.guild.id) or str(row["discord_id"]) != str(member.id):
            await interaction.response.send_message(embed=error_embed("Not found"), ephemeral=True)
            return
        was_open = row["status"] in {"active", "paused"}
        end_time = int(row["start_time"]) + duration
        await self.bot.db.update_shift(
            shift_id,
            status="completed",
            end_time=end_time,
            duration_seconds=duration,
            pause_started=None,
        )
        if was_open:
            cfg = await self.bot.guild_config(interaction.guild.id)
            await sync_duty_role(member, cfg, False)
            from wsp.cogs.quota import apply_shift_quota

            await apply_shift_quota(self.bot, interaction.guild.id, member.id, duration)
        await self.bot.db.audit(
            interaction.guild.id,
            "shift_edit",
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
            target_id=member.id,
            target_name=str(member),
            details=f"#{shift_id} → {format_duration(duration)}",
        )
        notice = success_embed("Shift updated", f"{member.mention} shift `#{shift_id}` is now **{format_duration(duration)}**.")
        await interaction.response.send_message(embed=notice, ephemeral=True)
        await self.bot.notify(
            interaction.guild,
            "shift_log",
            base_embed("Shift edited", f"{interaction.user.mention} set {member.mention} shift `#{shift_id}` to **{format_duration(duration)}**."),
        )

    @admin.command(name="delete", description="Delete a shift.")
    @has_level(PermissionLevel.SUPERVISOR)
    async def admin_delete(self, interaction: discord.Interaction, member: discord.Member, shift_id: int) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        row = await self.bot.db.get_shift(shift_id)
        if row is None or str(row["guild_id"]) != str(interaction.guild.id) or str(row["discord_id"]) != str(member.id):
            await interaction.response.send_message(embed=error_embed("Not found"), ephemeral=True)
            return
        if row["status"] in {"active", "paused"}:
            cfg = await self.bot.guild_config(interaction.guild.id)
            await sync_duty_role(member, cfg, False)
        await self.bot.db.delete_shift(shift_id)
        await self.bot.db.audit(
            interaction.guild.id,
            "shift_delete",
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
            target_id=member.id,
            target_name=str(member),
            details=f"#{shift_id}",
        )
        await interaction.response.send_message(
            embed=success_embed("Shift deleted", f"Removed shift `#{shift_id}` for {member.mention}."),
            ephemeral=True,
        )
        await self.bot.notify(
            interaction.guild,
            "shift_log",
            base_embed("Shift deleted", f"{interaction.user.mention} deleted {member.mention} shift `#{shift_id}`."),
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
