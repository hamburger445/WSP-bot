"""Ticket panel command and transcript helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_GOLD, PermissionLevel
from wsp.embeds import base_embed, error_embed, success_embed
from wsp.permissions import has_level
from wsp.views.tickets import TicketPanelView

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class Tickets(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    ticket = app_commands.Group(name="ticket", description="WSP assistance tickets")

    @ticket.command(name="panel", description="Post the public ticket panel in this channel (HR).")
    @has_level(PermissionLevel.HR)
    async def panel(self, interaction: discord.Interaction) -> None:
        embed = base_embed(
            "Wisconsin State Patrol  •  Assistance Desk",
            "Select a request type. A private channel will be opened for you and HR/Command.\n\n"
            "Resignations • LOA • HR questions • Complaints • Transfers • Appeals • General assistance",
            color=COLOR_GOLD,
        )
        await interaction.response.send_message(embed=success_embed("Panel posted"), ephemeral=True)
        if interaction.channel and isinstance(interaction.channel, discord.TextChannel):
            await interaction.channel.send(embed=embed, view=TicketPanelView())

    @ticket.command(name="close", description="Close the ticket in this channel.")
    async def close(self, interaction: discord.Interaction, reason: str) -> None:
        from wsp.views.tickets import TicketCloseModal

        modal = TicketCloseModal(None)
        modal.reason.default = reason
        await interaction.response.send_modal(modal)

    @ticket.command(name="list", description="List open tickets.")
    @has_level(PermissionLevel.HR)
    async def list_tickets(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        rows = await self.bot.db.list_tickets(interaction.guild.id, "open")
        embed = base_embed("Open tickets")
        embed.description = "\n".join(
            f"`#{r['id']}` {r['ticket_type']} <#{r['channel_id']}> <@{r['opener_id']}>"
            for r in rows
        ) or "No open tickets."
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Tickets(bot))
