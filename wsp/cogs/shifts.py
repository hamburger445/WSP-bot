"""Shift management slash commands."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import PermissionLevel
from wsp.embeds import add_fields, base_embed, error_embed, format_duration, success_embed, ts, ts_rel
from wsp.permissions import resolve_level
from wsp.utils import current_shift_seconds, hms_to_seconds, member_can_start_shift, mention_or_id, sync_duty_role
from wsp.views.shifts import (
    ShiftActionView,
    ShiftMenuView,
    acknowledge,
    begin_shift,
    build_duty_board,
    build_leaderboard,
    build_shift_management_embed,
    complete_shift,
    pause_shift,
    reply_interaction,
    resume_shift,
)

if TYPE_CHECKING:
    from wsp.bot import WSPBot

log = logging.getLogger("wsp.shifts")


def _completed_seconds(row) -> int:
    stored = int(row["duration_seconds"] or 0)
    if stored > 0:
        return stored
    start = int(row["start_time"] or 0)
    end = int(row["end_time"] or 0)
    if not end:
        return 0
    return max(0, end - start - int(row["paused_seconds"] or 0))


def _is_finished_shift(row) -> bool:
    status = str(row["status"] or "").strip().lower()
    if status in {"active", "paused"}:
        return False
    if status in {"completed", "complete", "ended", "closed", "done"}:
        return True
    return _completed_seconds(row) > 0


def admin_shift_embed(member: discord.Member, rows) -> discord.Embed:
    completed = [row for row in rows if _is_finished_shift(row)]
    total_seconds = sum(_completed_seconds(row) for row in completed)
    average = total_seconds // len(completed) if completed else 0
    shift_type = next((row["rank_name"] for row in rows if row["rank_name"]), None) or "Trooper"
    embed = base_embed(f"Shift Management: {member}", "")
    embed.add_field(
        name="All Time Information",
        value=(
            f"**Shift Count:** {len(completed)}\n"
            f"**Total Duration:** {format_duration(total_seconds)}\n"
            f"**Average Duration:** {format_duration(average)}"
        ),
        inline=False,
    )
    embed.add_field(name="Shift Type", value=shift_type, inline=False)
    active = next((row for row in rows if row["status"] in {"active", "paused"}), None)
    embed.add_field(name="Current Status", value=str(active["status"]) if active else "Off duty", inline=False)
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
        await interaction.response.send_message(f"Shift #{row['id']} updated to {format_duration(duration)}.", ephemeral=True)
        await self.view.refresh_panel(interaction)


class AdminShiftView(discord.ui.View):
    def __init__(self, bot: WSPBot, member: discord.Member, rows) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.member = member
        self.rows = rows
        self.selected_shift_id: int | None = None
        self.action_select = discord.ui.Select(
            placeholder="Shift Actions",
            options=[
                discord.SelectOption(label="Start shift", value="start"),
                discord.SelectOption(label="Pause shift", value="pause"),
                discord.SelectOption(label="Resume shift", value="resume"),
                discord.SelectOption(label="End shift", value="end"),
                discord.SelectOption(label="Edit selected shift", value="edit"),
                discord.SelectOption(label="Delete selected shift", value="delete"),
                discord.SelectOption(label="View all shifts", value="history"),
            ],
            row=0,
        )
        self.action_select.callback = self.select_action
        self.add_item(self.action_select)
        self.shift_select = discord.ui.Select(
            placeholder="Pick a shift to edit or delete",
            options=self._shift_options(),
            row=1,
        )
        self.shift_select.callback = self.select_shift
        self.add_item(self.shift_select)
        self._sync_buttons()

    def _shift_options(self) -> list[discord.SelectOption]:
        options = [
            discord.SelectOption(
                label=f"Shift #{row['id']} - {row['status']}",
                value=str(row["id"]),
                description=f"{format_duration(self.bot.db.effective_shift_seconds(row))} • {ts_rel(row['start_time'])}",
            )
            for row in self.rows[:25]
        ]
        return options or [discord.SelectOption(label="No shifts", value="none")]

    def _active(self):
        return next((row for row in self.rows if row["status"] in {"active", "paused"}), None)

    def _sync_buttons(self) -> None:
        active = self._active()
        self.start.disabled = active is not None
        paused = active is not None and active["status"] == "paused"
        self.pause.disabled = active is None
        self.pause.label = "Resume shift" if paused else "Pause shift"
        self.end.disabled = active is None

    async def select_action(self, interaction: discord.Interaction) -> None:
        action = self.action_select.values[0]
        if action == "edit":
            if not await self.guard(interaction):
                return
            if self.selected_shift_id is None:
                await interaction.response.send_message("Pick a shift first.", ephemeral=True)
                return
            await interaction.response.send_modal(AdminShiftEditModal(self))
            return
        if not await acknowledge(interaction):
            return
        if not await self.guard(interaction):
            return
        await self.perform_action(interaction, action)

    async def perform_action(self, interaction: discord.Interaction, action: str) -> None:
        if action == "start":
            result = await begin_shift(
                self.bot, interaction.guild, self.member, interaction.user, require_certified=False
            )
            await _admin_reply(interaction, result)
            if not result.error:
                await self.refresh_panel(interaction)
            return
        if action == "end":
            result = await complete_shift(self.bot, interaction.guild, self.member, interaction.user)
            await _admin_reply(interaction, result)
            if not result.error:
                await self.refresh_panel(interaction)
            return
        if action == "pause":
            result = await pause_shift(self.bot, interaction.guild, self.member, interaction.user)
            await _admin_reply(interaction, result)
            if not result.error:
                await self.refresh_panel(interaction)
            return
        if action == "resume":
            result = await resume_shift(self.bot, interaction.guild, self.member, interaction.user)
            await _admin_reply(interaction, result)
            if not result.error:
                await self.refresh_panel(interaction)
            return
        if action == "delete":
            await self.delete_selected(interaction)
            return
        await self.show_history(interaction)

    async def guard(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or await resolve_level(interaction) < PermissionLevel.SUPERVISOR:
            await reply_interaction(interaction, error_embed("Restricted"))
            return False
        return True

    async def refresh_panel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        self.rows = await self.bot.db.list_shifts(interaction.guild.id, self.member.id, limit=25)
        self.shift_select.options = self._shift_options()
        self._sync_buttons()
        embed = admin_shift_embed(self.member, self.rows)
        try:
            await interaction.edit_original_response(embed=embed, view=self)
        except discord.HTTPException:
            if interaction.message:
                try:
                    await interaction.message.edit(embed=embed, view=self)
                except discord.HTTPException:
                    pass

    async def select_shift(self, interaction: discord.Interaction) -> None:
        if not await self.guard(interaction):
            return
        value = self.shift_select.values[0]
        if value == "none":
            await reply_interaction(interaction, error_embed("Not found"))
            return
        self.selected_shift_id = int(value)
        await reply_interaction(interaction, success_embed("Shift selected", f"Shift `#{value}`."))

    async def show_history(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.list_shifts(interaction.guild.id, self.member.id, limit=25)
        embed = base_embed(f"All shifts • {self.member}")
        embed.description = "\n".join(
            f"`#{row['id']}` {row['status']} • {format_duration(row['duration_seconds'] or current_shift_seconds(row))} • {ts(row['start_time'])}"
            for row in rows
        ) or "No records."
        await reply_interaction(interaction, embed)

    @discord.ui.button(label="Start shift", style=discord.ButtonStyle.success, row=2)
    async def start(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await acknowledge(interaction):
            return
        if not await self.guard(interaction):
            return
        await self.perform_action(interaction, "start")

    @discord.ui.button(label="Pause shift", style=discord.ButtonStyle.secondary, row=2)
    async def pause(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await acknowledge(interaction):
            return
        if not await self.guard(interaction):
            return
        active = await self.bot.db.active_shift(interaction.guild.id, self.member.id)
        action = "resume" if active and active["status"] == "paused" else "pause"
        await self.perform_action(interaction, action)

    @discord.ui.button(label="End shift", style=discord.ButtonStyle.danger, row=2)
    async def end(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await acknowledge(interaction):
            return
        if not await self.guard(interaction):
            return
        await self.perform_action(interaction, "end")

    async def delete_selected(self, interaction: discord.Interaction) -> None:
        if self.selected_shift_id is None:
            await reply_interaction(interaction, error_embed("Not found"))
            return
        row = await self.bot.db.get_shift(self.selected_shift_id)
        if row is None or str(row["guild_id"]) != str(interaction.guild.id) or str(row["discord_id"]) != str(self.member.id):
            await reply_interaction(interaction, error_embed("Not found"))
            return
        if row["status"] in {"active", "paused"}:
            await sync_duty_role(self.member, await self.bot.guild_config(interaction.guild.id), False)
        await self.bot.db.delete_shift(row["id"])
        await self.bot.db.audit(
            interaction.guild.id,
            "shift_delete",
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
            target_id=self.member.id,
            target_name=str(self.member),
            details=f"#{row['id']}",
        )
        await self.bot.notify(
            interaction.guild,
            "shift_log",
            base_embed("Shift deleted", f"{interaction.user.mention} deleted {self.member.mention} shift `#{row['id']}`."),
        )
        self.selected_shift_id = None
        await reply_interaction(interaction, success_embed("Shift deleted", f"Removed shift `#{row['id']}`."))
        await self.refresh_panel(interaction)


class Shifts(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    shift = app_commands.Group(name="shift", description="Duty shifts")

    @shift.command(name="menu", description="View your current shift status.")
    async def menu(self, interaction: discord.Interaction) -> None:
        if not await acknowledge(interaction, ephemeral=False):
            log.warning("/shift menu interaction expired before acknowledgement")
            return
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await reply_interaction(interaction, error_embed("Guild only"))
            return
        try:
            totals = await self.bot.db.shift_totals(interaction.guild.id, interaction.user.id)
            active = await self.bot.db.active_shift(interaction.guild.id, interaction.user.id)
            cfg = await self.bot.guild_config(interaction.guild.id)
            await interaction.edit_original_response(
                embed=build_shift_management_embed(
                    interaction.user,
                    shift_count=int(totals["shift_count"] or 0) if totals else 0,
                    total_seconds=int(totals["total_seconds"] or 0) if totals else 0,
                    status=active["status"] if active else None,
                ),
                view=ShiftActionView(
                    active["status"] if active else None,
                    owner_id=interaction.user.id,
                    can_start=member_can_start_shift(interaction.user, cfg),
                ),
            )
        except discord.HTTPException as exc:
            if getattr(exc, "code", None) in {10062, 40060}:
                log.warning("/shift menu interaction expired while loading the response")
                return
            log.exception("Could not build /shift menu for user %s", interaction.user.id)
            await reply_interaction(interaction, error_embed("Unavailable"))
        except Exception:
            log.exception("Could not build /shift menu for user %s", interaction.user.id)
            await reply_interaction(interaction, error_embed("Unavailable"))

    @shift.command(name="data", description="Show who is on duty.")
    async def data(self, interaction: discord.Interaction) -> None:
        if not await acknowledge(interaction, ephemeral=False):
            log.warning("/shift data interaction expired before acknowledgement")
            return
        if not interaction.guild:
            await reply_interaction(interaction, error_embed("Guild only"))
            return
        embed = await build_duty_board(self.bot, interaction.guild)
        await interaction.edit_original_response(embed=embed, view=ShiftMenuView())

    @shift.command(name="status", description="Show who is on duty.")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await acknowledge(interaction, ephemeral=False):
            log.warning("/shift status interaction expired before acknowledgement")
            return
        if not interaction.guild:
            await reply_interaction(interaction, error_embed("Guild only"))
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
        await interaction.edit_original_response(embed=embed)

    @shift.command(name="leaderboard", description="Show duty time standings.")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        # Acknowledge immediately. If Discord has already expired the interaction,
        # there is nothing useful left for the bot to send, so fail quietly.
        if not await acknowledge(interaction, ephemeral=False):
            return
        if not interaction.guild:
            await reply_interaction(interaction, error_embed("Guild only"))
            return
        try:
            embed = await build_leaderboard(self.bot, interaction.guild)
            await interaction.edit_original_response(embed=embed)
        except discord.NotFound:
            return
        except discord.HTTPException as exc:
            if getattr(exc, "code", None) in {10062, 40060}:
                return
            log.exception("Could not display /shift leaderboard")
        except Exception:
            log.exception("Could not build /shift leaderboard")

    @shift.command(name="history", description="View shift history.")
    async def history(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if not await acknowledge(interaction, ephemeral=True):
            log.warning("/shift history interaction expired before acknowledgement")
            return
        if not interaction.guild:
            await reply_interaction(interaction, error_embed("Guild only"))
            return
        target = member or interaction.user
        if member and member.id != interaction.user.id:
            if await resolve_level(interaction) < PermissionLevel.SUPERVISOR:
                await reply_interaction(interaction, error_embed("Restricted"))
                return
        rows = await self.bot.db.list_shifts(interaction.guild.id, target.id, limit=12)
        totals = await self.bot.db.shift_totals(interaction.guild.id, target.id)
        embed = base_embed(f"Shift history  •  {target}")
        if totals:
            add_fields(embed, [("All-time", format_duration(totals["total_seconds"]), True), ("Shifts", str(totals["shift_count"]), True)])
        embed.description = "\n".join(
            f"`#{r['id']}` {r['status']} {r['callsign'] or ''} {format_duration(self.bot.db.effective_shift_seconds(r))} {ts(r['start_time'])}"
            for r in rows
        ) or "No records."
        await interaction.edit_original_response(embed=embed)

    @shift.command(name="admin", description="Open shift controls for a member.")
    async def admin(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await acknowledge(interaction, ephemeral=True):
            log.warning("/shift admin interaction expired before acknowledgement")
            return
        if not interaction.guild:
            await reply_interaction(interaction, error_embed("Guild only"))
            return
        if await resolve_level(interaction) < PermissionLevel.SUPERVISOR:
            await reply_interaction(interaction, error_embed("Restricted"))
            return
        rows = await self.bot.db.list_shifts(interaction.guild.id, member.id, limit=25)
        await interaction.edit_original_response(
            embed=admin_shift_embed(member, rows),
            view=AdminShiftView(self.bot, member, rows),
        )


async def _admin_reply(interaction: discord.Interaction, result) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if result.error:
        await reply_interaction(interaction, error_embed("Shift admin", result.error))
        return
    if interaction.guild and result.log:
        await bot.notify(interaction.guild, "shift_log", result.log)
    await reply_interaction(interaction, result.notice)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Shifts(bot))