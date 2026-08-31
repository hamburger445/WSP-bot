"""Promotion, demotion, and termination with Discord role sync."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import PermissionLevel
from wsp.embeds import error_embed, success_embed
from wsp.ops import change_rank, fire_member
from wsp.permissions import has_level

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class Promotions(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    @app_commands.command(name="promote", description="Promote a member.")
    @has_level(PermissionLevel.HR)
    @app_commands.describe(member="Member", rank="Rank", reason="Reason")
    async def promote(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        rank: str,
        reason: str,
    ) -> None:
        await self._rank(interaction, member, rank, reason, "promotion")

    @app_commands.command(name="demote", description="Demote a member.")
    @has_level(PermissionLevel.HR)
    @app_commands.describe(member="Member", rank="Rank", reason="Reason")
    async def demote(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        rank: str,
        reason: str,
    ) -> None:
        await self._rank(interaction, member, rank, reason, "demotion")

    @app_commands.command(name="fire", description="Fire a member.")
    @has_level(PermissionLevel.HR)
    @app_commands.describe(member="Member", reason="Reason")
    async def fire(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        message = await fire_member(self.bot, interaction.guild, member, reason, interaction.user)
        if message in {"Restricted", "Could not update roles."}:
            await interaction.followup.send(embed=error_embed(message), ephemeral=True)
            return
        await interaction.followup.send(embed=success_embed("Member fired", message), ephemeral=True)

    @promote.autocomplete("rank")
    @demote.autocomplete("rank")
    async def rank_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        ranks = await self.bot.db.list_ranks(interaction.guild_id or 0)
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in ranks if current.lower() in r["name"].lower()][:25]

    async def _rank(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        rank: str,
        reason: str,
        action: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        error = await change_rank(
            self.bot, interaction.guild, member, rank, reason, interaction.user, action
        )
        if error in {"Restricted", "Could not update roles."}:
            await interaction.response.send_message(embed=error_embed(error), ephemeral=True)
            return
        if error:
            await interaction.response.send_message(embed=error_embed("Invalid rank change", error), ephemeral=True)
            return
        title = "Promotion recorded" if action == "promotion" else "Demotion recorded"
        await interaction.response.send_message(
            embed=success_embed(title, f"{member.mention} is now **{rank}**."),
            ephemeral=True,
        )


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Promotions(bot))
