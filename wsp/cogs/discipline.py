"""Disciplinary records."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_DANGER, DISCIPLINE_ACTIONS, PermissionLevel
from wsp.embeds import add_fields, base_embed, error_embed, success_embed, ts, ts_rel
from wsp.permissions import has_level
from wsp.utils import ensure_personnel, parse_date

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class Discipline(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    discipline = app_commands.Group(name="discipline", description="Disciplinary records (HR/Command)")

    @discipline.command(name="add", description="Add a disciplinary action to a personnel file.")
    @has_level(PermissionLevel.COMMAND)
    async def add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        action: str,
        reason: str,
        expires: str | None = None,
    ) -> None:
        if not interaction.guild:
            return
        if action not in DISCIPLINE_ACTIONS:
            await interaction.response.send_message(embed=error_embed("Unknown action", "Use an autocomplete value."), ephemeral=True)
            return
        cfg = await self.bot.guild_config(interaction.guild.id)
        expires_ts = parse_date(expires, cfg.get("timezone") or "America/Chicago") if expires else None
        await ensure_personnel(self.bot, member)
        record_id = await self.bot.db.add_discipline(interaction.guild.id, member.id, action, reason, interaction.user.id, expires_ts)
        await self.bot.db.audit(
            interaction.guild.id, "discipline", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=member.id, target_name=str(member), details=f"{action}: {reason}",
        )
        await self.bot.db.log_activity(interaction.guild.id, member.id, "discipline", action)
        if action == "Suspension":
            record = await self.bot.db.get_personnel(interaction.guild.id, member.id)
            if record:
                await self.bot.db.update_personnel(record["id"], status="suspended")
        if action == "Removal":
            record = await self.bot.db.get_personnel(interaction.guild.id, member.id)
            if record:
                await self.bot.db.update_personnel(record["id"], status="removed")
        embed = base_embed("Disciplinary action recorded", f"{member.mention}  •  **{action}**", color=COLOR_DANGER)
        add_fields(embed, [("Reason", reason, False), ("Record", f"#{record_id}", True), ("Expires", ts(expires_ts) if expires_ts else "—", True)])
        await interaction.response.send_message(embed=success_embed("Recorded", f"{action} added to {member.mention}."), ephemeral=True)
        await self.bot.notify(interaction.guild, "discipline", embed)
        await self.bot.notify(interaction.guild, "command_log", embed)

    @add.autocomplete("action")
    async def action_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=a, value=a) for a in DISCIPLINE_ACTIONS if current.lower() in a.lower()]

    @discipline.command(name="view", description="View disciplinary records for a member.")
    @has_level(PermissionLevel.HR)
    async def view(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not interaction.guild:
            return
        rows = await self.bot.db.list_discipline(interaction.guild.id, member.id)
        embed = base_embed(f"Discipline  •  {member}", "HR/Command only.")
        if not rows:
            embed.description = "No disciplinary records."
        else:
            embed.description = "\n".join(
                f"`#{r['id']}` {ts_rel(r['created_at'])} **{r['action']}** {'(inactive)' if not r['active'] else ''}\n{r['reason']}"
                for r in rows[:12]
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discipline.command(name="remove", description="Deactivate a disciplinary record (does not erase history).")
    @has_level(PermissionLevel.COMMAND)
    async def remove(self, interaction: discord.Interaction, record_id: int, reason: str) -> None:
        if not interaction.guild:
            return
        row = await self.bot.db.get_discipline(record_id)
        if row is None or str(row["guild_id"]) != str(interaction.guild.id):
            await interaction.response.send_message(embed=error_embed("Not found"), ephemeral=True)
            return
        await self.bot.db.deactivate_discipline(record_id)
        await self.bot.db.audit(
            interaction.guild.id, "discipline_remove", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=row["discord_id"], details=f"#{record_id} {reason}",
        )
        await interaction.response.send_message(embed=success_embed("Record deactivated", f"Discipline `#{record_id}` is no longer active."), ephemeral=True)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Discipline(bot))
