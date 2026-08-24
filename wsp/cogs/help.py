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
        "`/shift menu` — public start, pause, resume, and end buttons",
        "`/shift data` — public duty board and leaderboard",
        "`/shift start` `/shift status` `/shift leaderboard` `/shift history`",
        "`/quota view` `/quota leaderboard`",
        "`/profile` — your personnel file",
    ]),
    ("Leave", "Time away from duty", [
        "`/loa menu` — how to request leave",
        "`/loa request` — dates as `YYYY-MM-DD` (example: `2026-09-01`)",
        "You will be notified when your request is reviewed.",
    ]),
    ("Help", "Command directory", [
        "`/help` — this directory",
    ]),
]

STAFF_CATALOG: list[tuple[str, str, list[str]]] = [
    ("Personnel", "Roster and rank", [
        "`/personnel add` `note` `transfer` `suspend` `remove` `reinstate` `history`",
        "`/promote` `/demote` `/fire`",
        "`/shift correct` `/quota admin`",
    ]),
    ("HR / Command", "Leave and logs", [
        "`/loa approve` `/loa deny` `/loa active`",
        "`/dashboard` — includes **Reset shift data**",
        "`/audit`",
    ]),
    ("Setup", "Owners and command staff", [
        "`/setupserver` `/verifysetup` `/config` `/sync` `/resetserver`",
    ]),
]


class Help(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="How to use WSP commands.")
    async def help(self, interaction: discord.Interaction) -> None:
        staff = await _is_staff(interaction)
        await interaction.response.send_message(
            embed=_catalog_embed("members", staff),
            view=HelpView(staff),
            ephemeral=True,
        )

    @app_commands.command(name="wsp", description="Open the Wisconsin State Patrol command directory.")
    async def wsp(self, interaction: discord.Interaction) -> None:
        staff = await _is_staff(interaction)
        await interaction.response.send_message(
            embed=_catalog_embed("members", staff),
            view=HelpView(staff),
            ephemeral=True,
        )


async def _is_staff(interaction: discord.Interaction) -> bool:
    return await resolve_level(interaction) >= PermissionLevel.HR


def _catalog_embed(section: str, staff: bool) -> discord.Embed:
    if staff:
        intro = (
            "Type **/** in Discord, then start typing a name below. "
            "Members see how to use the bot. Staff tools are listed separately."
        )
    else:
        intro = (
            "Type **/** in Discord, then start typing a name below. "
            "Grouped commands appear as `/shift` or `/loa` — pick the group, then the action."
        )
    embed = base_embed("WSP command directory", intro, color=COLOR_NAVY)
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
        options = [discord.SelectOption(label="How to use the bot", value="members")]
        if staff:
            options.append(discord.SelectOption(label="Staff tools", value="staff"))
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
