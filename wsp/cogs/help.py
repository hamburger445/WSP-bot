"""Slash-command directory. Member help hides HR/Command internals."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_NAVY, PermissionLevel
from wsp.embeds import base_embed
from wsp.permissions import resolve_level

if TYPE_CHECKING:
    from wsp.bot import WSPBot

MEMBER_CATALOG: list[tuple[str, str, list[str]]] = [
    ("Duty", "Shifts and quota", [
        "`/shift menu` — start, pause, resume, or end your shift",
        "`/shift data` — duty board",
        "`/shift status` — who is on duty",
        "`/shift leaderboard` — duty standings",
        "`/shift history` — shift history",
        "`/quota view` — view quota",
        "`/quota leaderboard` — quota standings",
    ]),
    ("Leave", "Time away from duty", [
        "`/loa menu` — leave menu",
        "`/loa request` — request leave",
    ]),
    ("Help", "Commands", [
        "`/help` — command list",
        "`/ping` — ping",
    ]),
]

STAFF_CATALOG: list[tuple[str, str, list[str]]] = [
    ("Rank", "Promotions", [
        "`/promote` — promote a member",
        "`/demote` — demote a member",
        "`/fire` — fire a member",
        "`/shift admin` — start, end, edit, or delete a shift",
        "`/quota admin` — change quota settings",
    ]),
    ("Command", "Overview", [
        "`/loa active` — members on leave",
        "`/loa admin` — manage leave",
        "`/dashboard` — dashboard",
    ]),
    ("Setup", "Server", [
        "`/setupserver` — set up the server",
        "`/verifysetup` — check setup",
        "`/config` — settings",
        "`/sync` — update commands",
    ]),
]


class Help(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="Show available commands.")
    async def help(self, interaction: discord.Interaction) -> None:
        staff = await _is_staff(interaction)
        await interaction.response.send_message(
            embed=_catalog_embed("members", staff),
            view=HelpView(staff),
            ephemeral=True,
        )


    @app_commands.command(name="ping", description="Check latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong — **{ms} ms**")


async def _is_staff(interaction: discord.Interaction) -> bool:
    return await resolve_level(interaction) >= PermissionLevel.SUPERVISOR


def _catalog_embed(section: str, staff: bool) -> discord.Embed:
    intro = "Available commands."
    embed = base_embed("Commands", intro, color=COLOR_NAVY)
    rows = _rows_for(section, staff)
    for title, subtitle, lines in rows:
        embed.add_field(name=f"{title}  ·  {subtitle}", value="\n".join(lines), inline=False)
    return embed


def _rows_for(section: str, staff: bool) -> list[tuple[str, str, list[str]]]:
    if section == "staff" and staff:
        return STAFF_CATALOG
    if section == "all" and staff:
        return MEMBER_CATALOG + STAFF_CATALOG
    if section == "members":
        return MEMBER_CATALOG
    for title, subtitle, lines in MEMBER_CATALOG + (STAFF_CATALOG if staff else []):
        if title.lower() == section.lower():
            return [(title, subtitle, lines)]
    return MEMBER_CATALOG


class HelpView(discord.ui.View):
    def __init__(self, staff: bool) -> None:
        super().__init__(timeout=180)
        self.staff = staff
        self.add_item(HelpSelect(staff))


class HelpSelect(discord.ui.Select):
    def __init__(self, staff: bool) -> None:
        self.staff = staff
        options = [discord.SelectOption(label="Commands", value="members")]
        if staff:
            options.append(discord.SelectOption(label="Staff commands", value="staff"))
            options.append(discord.SelectOption(label="All commands", value="all"))
        options.extend(
            discord.SelectOption(label=title, value=title, description=subtitle)
            for title, subtitle, _ in MEMBER_CATALOG
        )
        super().__init__(placeholder="Filter the command list…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            embed=_catalog_embed(self.values[0], self.staff),
            view=self.view,
        )


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Help(bot))
