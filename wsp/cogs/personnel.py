"""Personnel records, notes, transfers, and status changes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_NAVY, PermissionLevel
from wsp.embeds import base_embed, error_embed, success_embed, ts
from wsp.permissions import has_level
from wsp.utils import ensure_personnel, sync_rank_roles

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class Personnel(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    personnel = app_commands.Group(name="personnel", description="Department personnel management")

    @personnel.command(name="add", description="Register a member on the WSP roster.")
    @has_level(PermissionLevel.HR)
    @app_commands.describe(member="Discord member", rank="Starting rank", position="Assignment / position", callsign="Unit callsign")
    async def add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        rank: str,
        position: str | None = None,
        callsign: str | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        rank_row = await self.bot.db.get_rank_by_name(interaction.guild.id, rank)
        if not rank_row:
            await interaction.response.send_message(embed=error_embed("Unknown rank", "Use an autocomplete suggestion."), ephemeral=True)
            return
        record = await self.bot.db.upsert_personnel(
            interaction.guild.id, member.id, str(member), rank_id=rank_row["id"]
        )
        fields = {"status": "active"}
        if position:
            fields["position"] = position
        if callsign:
            fields["callsign"] = callsign
        await self.bot.db.update_personnel(record["id"], **fields)
        cfg = await self.bot.guild_config(interaction.guild.id)
        await sync_rank_roles(member, rank, cfg)
        await self.bot.db.add_rank_history(record["id"], "appointment", None, rank, "Initial roster appointment", str(interaction.user.id), str(interaction.user.id))
        await self.bot.db.audit(
            interaction.guild.id, "personnel_add", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=member.id, target_name=str(member), details=rank,
        )
        await interaction.response.send_message(
            embed=success_embed("Personnel added", f"{member.mention} is now on the roster as **{rank}**."),
            ephemeral=True,
        )
        await self.bot.notify(
            interaction.guild,
            "command_log",
            base_embed("Personnel added", f"{member.mention} appointed **{rank}** by {interaction.user.mention}."),
        )

    @add.autocomplete("rank")
    async def rank_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await _rank_choices(self.bot, interaction, current)

    @personnel.command(name="note", description="Add an HR or Command note to a personnel file.")
    @has_level(PermissionLevel.HR)
    @app_commands.describe(member="Target member", note_type="hr or command", content="Note text")
    @app_commands.choices(note_type=[app_commands.Choice(name="HR", value="hr"), app_commands.Choice(name="Command", value="command")])
    async def note(self, interaction: discord.Interaction, member: discord.Member, note_type: str, content: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        record = await ensure_personnel(self.bot, member)
        await self.bot.db.add_note(record["id"], note_type, content, interaction.user.id)
        await self.bot.db.audit(
            interaction.guild.id, "personnel_note", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=member.id, target_name=str(member), details=note_type,
        )
        await interaction.response.send_message(embed=success_embed("Note recorded", f"{note_type.upper()} note added to {member.mention}."), ephemeral=True)
        log_key = "hr_log" if note_type == "hr" else "command_log"
        await self.bot.notify(
            interaction.guild,
            log_key,
            base_embed(f"{note_type.upper()} note", f"{member.mention}\n{content}"),
        )

    @personnel.command(name="transfer", description="Update a member's department position.")
    @has_level(PermissionLevel.HR)
    async def transfer(self, interaction: discord.Interaction, member: discord.Member, position: str, reason: str) -> None:
        if not interaction.guild:
            return
        record = await ensure_personnel(self.bot, member)
        previous = record["position"]
        await self.bot.db.update_personnel(record["id"], position=position)
        await self.bot.db.add_rank_history(record["id"], "transfer", previous, position, reason, str(interaction.user.id), str(interaction.user.id))
        await self.bot.db.audit(
            interaction.guild.id, "transfer", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=member.id, target_name=str(member), details=f"{previous} → {position}: {reason}",
        )
        embed = success_embed("Transfer recorded", f"{member.mention} assigned to **{position}**.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self.bot.notify(interaction.guild, "hr_log", base_embed("Transfer", f"{member.mention}: {previous or '—'} → **{position}**\n{reason}"))

    @personnel.command(name="suspend", description="Suspend a member from duty.")
    @has_level(PermissionLevel.COMMAND)
    async def suspend(self, interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
        if not interaction.guild:
            return
        record = await ensure_personnel(self.bot, member)
        await self.bot.db.update_personnel(record["id"], status="suspended")
        await self.bot.db.audit(
            interaction.guild.id, "suspend", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=member.id, target_name=str(member), details=reason,
        )
        await interaction.response.send_message(embed=success_embed("Member suspended", f"{member.mention}\n{reason}"), ephemeral=True)
        await self.bot.notify(interaction.guild, "hr_log", base_embed("Suspension", f"{member.mention} — {reason}"))

    @personnel.command(name="remove", description="Remove a member from the WSP roster.")
    @has_level(PermissionLevel.COMMAND)
    async def remove(self, interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
        if not interaction.guild:
            return
        record = await ensure_personnel(self.bot, member)
        await self.bot.db.update_personnel(record["id"], status="removed")
        await self.bot.db.audit(
            interaction.guild.id, "personnel_remove", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=member.id, target_name=str(member), details=reason,
        )
        await interaction.response.send_message(embed=success_embed("Member removed", f"{member.mention} is no longer on the roster."), ephemeral=True)
        await self.bot.notify(interaction.guild, "hr_log", base_embed("Roster removal", f"{member.mention} — {reason}"))
        await self.bot.notify(interaction.guild, "notifications", base_embed("Roster removal", f"{member.mention} — {reason}"))

    @personnel.command(name="reinstate", description="Return a suspended or removed member to active status.")
    @has_level(PermissionLevel.COMMAND)
    async def reinstate(self, interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
        if not interaction.guild:
            return
        record = await ensure_personnel(self.bot, member)
        await self.bot.db.update_personnel(record["id"], status="active")
        await self.bot.db.audit(
            interaction.guild.id, "personnel_reinstate", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=member.id, target_name=str(member), details=reason,
        )
        await interaction.response.send_message(embed=success_embed("Reinstated", f"{member.mention} is active."), ephemeral=True)
        await self.bot.notify(
            interaction.guild,
            "command_log",
            base_embed("Reinstated", f"{member.mention} — {reason}"),
        )

    @personnel.command(name="history", description="View rank and status history for a member.")
    @has_level(PermissionLevel.HR)
    async def history(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not interaction.guild:
            return
        record = await ensure_personnel(self.bot, member)
        rows = await self.bot.db.rank_history(record["id"])
        embed = base_embed(f"Personnel history  •  {member}", color=COLOR_NAVY)
        if not rows:
            embed.description = "No recorded actions."
        else:
            lines = []
            for row in rows[:15]:
                lines.append(f"{ts(row['created_at'])} **{row['action']}** {row['from_rank'] or '—'} → {row['to_rank'] or '—'}\n{row['reason'] or ''}")
            embed.description = "\n\n".join(lines)[:4000]
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def _rank_choices(bot: WSPBot, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    ranks = await bot.db.list_ranks(interaction.guild_id or 0)
    return [
        app_commands.Choice(name=r["name"], value=r["name"])
        for r in ranks
        if current.lower() in r["name"].lower()
    ][:25]


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Personnel(bot))
