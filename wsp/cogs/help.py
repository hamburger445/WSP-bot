"""Slash-command directory so every WSP command is easy to find from /."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_NAVY
from wsp.embeds import base_embed

if TYPE_CHECKING:
    from wsp.bot import WSPBot

CATALOG: list[tuple[str, str, list[str]]] = [
    ("Duty", "Shifts and quota", [
        "`/shift menu` — start, pause, resume, end, history",
        "`/shift start` `/shift status` `/shift leaderboard` `/shift history` `/shift correct`",
        "`/quota view` `/quota leaderboard` `/quota admin`",
        "`/profile` — personnel file",
    ]),
    ("Personnel", "Roster and rank", [
        "`/personnel add` `note` `transfer` `suspend` `remove` `reinstate` `history`",
        "`/promote` `/demote`",
        "`/training set` `/training view`",
        "`/vehicle assign` `/vehicle release` `/vehicle list`",
    ]),
    ("Training pipeline", "Fast-pass through probation", [
        "`/fastpass start` `review` `approve` `deny`",
        "`/supervision start` `complete` `review` `history`",
        "`/probation start` `view` `review` `extend` `complete` `clear`",
    ]),
    ("HR / Command", "Leave, discipline, tickets", [
        "`/loa menu` `/loa request` `approve` `deny` `active`",
        "`/discipline add` `view` `remove`",
        "`/ticket panel` `close` `list`",
        "`/dashboard` `/audit`",
    ]),
    ("Setup", "Owners and command staff", [
        "`/setup` `/setupserver` `/verifysetup` `/config` `/sync` `/resetserver`",
        "`/help` — this directory",
    ]),
]


class Help(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="List every WSP slash command available in the / menu.")
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=_catalog_embed("all"), view=HelpView(), ephemeral=True)

    @app_commands.command(name="wsp", description="Open the Wisconsin State Patrol command directory.")
    async def wsp(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=_catalog_embed("all"), view=HelpView(), ephemeral=True)


def _catalog_embed(section: str) -> discord.Embed:
    embed = base_embed(
        "WSP command directory",
        "Type **/** in Discord to open the slash menu, then start typing a name below. "
        "Grouped commands appear as `/shift`, `/loa`, `/quota`, and so on — pick the group, then the action.",
        color=COLOR_NAVY,
    )
    rows = CATALOG if section == "all" else [row for row in CATALOG if row[0].lower() == section.lower()]
    for title, subtitle, lines in rows:
        embed.add_field(name=f"{title}  ·  {subtitle}", value="\n".join(lines), inline=False)
    return embed


class HelpView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.add_item(HelpSelect())


class HelpSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [discord.SelectOption(label="All commands", value="all")]
        options.extend(discord.SelectOption(label=title, value=title, description=subtitle) for title, subtitle, _ in CATALOG)
        super().__init__(placeholder="Filter the command list…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=_catalog_embed(self.values[0]), view=self.view)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Help(bot))
