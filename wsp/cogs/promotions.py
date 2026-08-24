"""Promotion and demotion workflows with Discord role sync."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_GOLD, COLOR_NAVY, PermissionLevel
from wsp.embeds import add_fields, base_embed, error_embed, success_embed
from wsp.permissions import has_level
from wsp.utils import ensure_personnel, sync_rank_roles

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class Promotions(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    @app_commands.command(name="promote", description="Promote a WSP member and update Discord roles.")
    @has_level(PermissionLevel.COMMAND)
    async def promote(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        rank: str,
        reason: str,
        authorizing_command: discord.Member,
    ) -> None:
        await self._change_rank(interaction, member, rank, reason, authorizing_command, "promotion")

    @app_commands.command(name="demote", description="Demote a WSP member and update Discord roles.")
    @has_level(PermissionLevel.COMMAND)
    async def demote(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        rank: str,
        reason: str,
        authorizing_command: discord.Member,
    ) -> None:
        await self._change_rank(interaction, member, rank, reason, authorizing_command, "demotion")

    @promote.autocomplete("rank")
    @demote.autocomplete("rank")
    async def rank_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        ranks = await self.bot.db.list_ranks(interaction.guild_id or 0)
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in ranks if current.lower() in r["name"].lower()][:25]

    async def _change_rank(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        rank: str,
        reason: str,
        authorizing: discord.Member,
        action: str,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        new_rank = await self.bot.db.get_rank_by_name(guild.id, rank)
        if new_rank is None:
            await interaction.response.send_message(embed=error_embed("Unknown rank"), ephemeral=True)
            return
        record = await ensure_personnel(self.bot, member)
        from_rank = record["rank_name"]
        from_pos = int(record["rank_position"] or 0)
        to_pos = int(new_rank["position"])
        if action == "promotion" and to_pos <= from_pos:
            await interaction.response.send_message(
                embed=error_embed("Invalid promotion", f"{member.display_name} is already at or above **{rank}**. Use `/demote` if needed."),
                ephemeral=True,
            )
            return
        if action == "demotion" and to_pos >= from_pos:
            await interaction.response.send_message(
                embed=error_embed("Invalid demotion", f"{member.display_name} is already at or below **{rank}**. Use `/promote` if needed."),
                ephemeral=True,
            )
            return
        await self.bot.db.update_personnel(record["id"], rank_id=new_rank["id"])
        await self.bot.db.add_rank_history(
            record["id"], action, from_rank, rank, reason, str(authorizing.id), str(interaction.user.id)
        )
        cfg = await self.bot.guild_config(guild.id)
        await sync_rank_roles(member, rank, cfg)
        await self.bot.db.audit(
            guild.id,
            action,
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
            target_id=member.id,
            target_name=str(member),
            details=f"{from_rank} → {rank} | auth {authorizing} | {reason}",
        )
        await self.bot.db.log_activity(guild.id, member.id, action, f"{from_rank} → {rank}")
        color = COLOR_GOLD if action == "promotion" else COLOR_NAVY
        title = "Promotion" if action == "promotion" else "Demotion"
        public = base_embed(title, f"{member.mention}  •  {from_rank or 'Unassigned'} → **{rank}**", color=color)
        add_fields(
            public,
            [
                ("Reason", reason, False),
                ("Processed by", interaction.user.mention, True),
                ("Authorized by", authorizing.mention, True),
            ],
        )
        dm = base_embed(
            title,
            f"Your rank is now **{rank}**.\n{from_rank or 'Unassigned'} → **{rank}**",
            color=color,
        )
        dm.add_field(name="Reason", value=reason, inline=False)
        dm_ok = await self.bot.try_dm(member, dm)
        note = "" if dm_ok else " Could not DM the member (DMs may be closed)."
        await interaction.response.send_message(
            embed=success_embed(f"{title} recorded", f"{member.mention} is now **{rank}**.{note}"),
            ephemeral=True,
        )
        await self.bot.notify(guild, "promotions", public)
        await self.bot.notify(guild, "notifications", public)
        await self.bot.notify(guild, "command_log", public)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Promotions(bot))
