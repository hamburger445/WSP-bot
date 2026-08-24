"""Probationary period tracking."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_GOLD, COLOR_WARNING, PermissionLevel
from wsp.db import now_ts
from wsp.embeds import add_fields, base_embed, error_embed, success_embed, ts, warning_embed
from wsp.permissions import has_level
from wsp.utils import ensure_personnel, mention_or_id

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class Probation(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    probation = app_commands.Group(name="probation", description="Probationary period management")

    @probation.command(name="view", description="View a member's probation record.")
    @has_level(PermissionLevel.HR)
    async def view(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not interaction.guild:
            return
        row = await self.bot.db.active_probation(interaction.guild.id, member.id)
        if row is None:
            row = await self.bot.db.fetchone(
                "SELECT * FROM probations WHERE guild_id = ? AND discord_id = ? ORDER BY start_date DESC",
                (str(interaction.guild.id), str(member.id)),
            )
        if row is None:
            await interaction.response.send_message(embed=error_embed("No probation record"), ephemeral=True)
            return
        reviews = await self.bot.db.list_probation_reviews(row["id"])
        embed = base_embed(f"Probation  •  {member}")
        add_fields(
            embed,
            [
                ("Status", row["status"], True),
                ("Supervisor", mention_or_id(interaction.guild, row["supervisor_id"]), True),
                ("Start", ts(row["start_date"]), True),
                ("Expected end", ts(row["expected_end"]), True),
                ("Result", row["final_result"] or "—", True),
                ("Issues", row["issues"] or "—", False),
                ("Recommendations", row["recommendations"] or "—", False),
            ],
        )
        if reviews:
            embed.add_field(
                name="Reviews",
                value="\n".join(f"{ts(r['created_at'])} {r['performance']}" for r in reviews[:5])[:1024],
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @probation.command(name="review", description="Add a performance review to an active probation.")
    @has_level(PermissionLevel.SUPERVISOR)
    async def review(self, interaction: discord.Interaction, member: discord.Member, performance: str, issues: str | None = None, recommendations: str | None = None) -> None:
        if not interaction.guild:
            return
        row = await self.bot.db.active_probation(interaction.guild.id, member.id)
        if row is None:
            await interaction.response.send_message(embed=error_embed("No active probation"), ephemeral=True)
            return
        await self.bot.db.add_probation_review(row["id"], interaction.user.id, performance, issues or "", recommendations or "")
        await self.bot.db.audit(
            interaction.guild.id, "probation_review", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=member.id, target_name=str(member), details=performance,
        )
        await interaction.response.send_message(embed=success_embed("Review recorded"), ephemeral=True)

    @probation.command(name="extend", description="Extend probation with a reason.")
    @has_level(PermissionLevel.HR)
    async def extend(self, interaction: discord.Interaction, member: discord.Member, additional_days: int, reason: str) -> None:
        if not interaction.guild:
            return
        row = await self.bot.db.active_probation(interaction.guild.id, member.id)
        if row is None:
            await interaction.response.send_message(embed=error_embed("No active probation"), ephemeral=True)
            return
        new_end = int(row["expected_end"]) + additional_days * 86400
        await self.bot.db.update_probation(
            row["id"], expected_end=new_end, status="extended", extension_reason=reason, notified_ending=0
        )
        personnel = await self.bot.db.get_personnel(interaction.guild.id, member.id)
        if personnel:
            await self.bot.db.update_personnel(personnel["id"], probation_status="extended")
        embed = warning_embed("Probation extended", f"{member.mention} extended by **{additional_days} days**.\n{reason}")
        await interaction.response.send_message(embed=success_embed("Extended", f"New end: {ts(new_end)}"), ephemeral=True)
        await self.bot.notify(interaction.guild, "probation", embed)
        await self.bot.notify(interaction.guild, "hr_log", embed)

    @probation.command(name="complete", description="Close probation as passed or failed.")
    @has_level(PermissionLevel.HR)
    @app_commands.choices(result=[app_commands.Choice(name="Passed", value="passed"), app_commands.Choice(name="Failed", value="failed")])
    async def complete(self, interaction: discord.Interaction, member: discord.Member, result: str, recommendations: str | None = None) -> None:
        if not interaction.guild:
            return
        row = await self.bot.db.active_probation(interaction.guild.id, member.id)
        if row is None:
            await interaction.response.send_message(embed=error_embed("No active probation"), ephemeral=True)
            return
        await self.bot.db.update_probation(
            row["id"], status=result, final_result=result, actual_end=now_ts(), recommendations=recommendations
        )
        personnel = await ensure_personnel(self.bot, member)
        await self.bot.db.update_personnel(personnel["id"], probation_status=result)
        await self.bot.db.audit(
            interaction.guild.id, "probation_complete", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=member.id, target_name=str(member), details=result,
        )
        public = base_embed("Probation closed", f"{member.mention}  •  **{result.title()}**", color=COLOR_GOLD)
        if recommendations:
            public.add_field(name="Recommendations", value=recommendations, inline=False)
        await interaction.response.send_message(embed=success_embed("Probation closed", result.title()), ephemeral=True)
        await self.bot.notify(interaction.guild, "probation", public)
        await self.bot.notify(interaction.guild, "notifications", public)

    @probation.command(name="start", description="Manually place a member on probation.")
    @has_level(PermissionLevel.HR)
    async def start(self, interaction: discord.Interaction, member: discord.Member, supervisor: discord.Member | None = None, days: int | None = None) -> None:
        if not interaction.guild:
            return
        existing = await self.bot.db.active_probation(interaction.guild.id, member.id)
        if existing:
            await interaction.response.send_message(embed=error_embed("Already on probation"), ephemeral=True)
            return
        cfg = await self.bot.guild_config(interaction.guild.id)
        duration = days or int(cfg.get("probation", "duration_days") or 14)
        await ensure_personnel(self.bot, member)
        pid = await self.bot.db.start_probation(interaction.guild.id, member.id, supervisor.id if supervisor else interaction.user.id, duration)
        personnel = await self.bot.db.get_personnel(interaction.guild.id, member.id)
        if personnel:
            await self.bot.db.update_personnel(personnel["id"], probation_status="active")
        await interaction.response.send_message(embed=success_embed("Probation started", f"{member.mention} • {duration} days • `#{pid}`"), ephemeral=True)
        await self.bot.notify(interaction.guild, "probation", base_embed("Probation started", f"{member.mention} • {duration} days"))
        await self.bot.notify(interaction.guild, "notifications", base_embed("Probation started", f"{member.mention} is now on a probationary period."))

    @probation.command(name="clear", description="Clear probation status without a pass/fail (administrative).")
    @has_level(PermissionLevel.COMMAND)
    async def clear(self, interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
        if not interaction.guild:
            return
        row = await self.bot.db.active_probation(interaction.guild.id, member.id)
        if row:
            await self.bot.db.update_probation(row["id"], status="cleared", actual_end=now_ts(), final_result="cleared", issues=reason)
        personnel = await ensure_personnel(self.bot, member)
        await self.bot.db.update_personnel(personnel["id"], probation_status="none")
        await interaction.response.send_message(embed=success_embed("Probation cleared", member.mention), ephemeral=True)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Probation(bot))
