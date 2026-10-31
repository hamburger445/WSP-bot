"""Private /rolecolor command for a single user."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_NAVY
from wsp.embeds import error_embed, success_embed
from wsp.utils import reply_interaction

if TYPE_CHECKING:
    from wsp.bot import WSPBot

ROLECOLOR_USER_ID = 1391201462259810354
ROLECOLOR_ROLE_IDS = (
    1543665568941478070,
    1216298389088768040,
    1216298426170605579,
    1548584884967186483,
    1216298539722739793,
    1313115056078192640,
    1313162449129836585,
    1216298572278923315,
    1216298610506076190,
    1216298670450937886,
    1216488567447093378,
    1405224151769157743,
    1216298752990777416,
    1216298719545135134,
    1269764432821485569,
    1220149958196465674,
    1304311287509159946,
    1548906849250709624,
)


def parse_role_colour(value: str) -> discord.Colour | None:
    raw = (value or "").strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    raw = raw.lstrip("#")
    if len(raw) == 3 and all(ch in "0123456789abcdef" for ch in raw):
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6 or any(ch not in "0123456789abcdef" for ch in raw):
        return None
    return discord.Colour(int(raw, 16))


class RoleColor(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    @app_commands.command(name="rolecolor", description="Set the shared color for department roles.")
    @app_commands.default_permissions()
    @app_commands.guild_only()
    @app_commands.describe(color="Hex color, for example cdd7eb")
    async def rolecolor(self, interaction: discord.Interaction, color: str) -> None:
        if interaction.user.id != ROLECOLOR_USER_ID:
            await reply_interaction(interaction, embed=error_embed("Restricted"), ephemeral=True)
            return
        if interaction.guild is None:
            await reply_interaction(interaction, embed=error_embed("Guild only"), ephemeral=True)
            return
        colour = parse_role_colour(color)
        if colour is None:
            await reply_interaction(
                interaction,
                embed=error_embed("Invalid color", "Use a hex value such as `cdd7eb` or `#cdd7eb`."),
                ephemeral=True,
            )
            return
        updated: list[str] = []
        missing: list[str] = []
        failed: list[str] = []
        seen: set[int] = set()
        for role_id in ROLECOLOR_ROLE_IDS:
            if role_id in seen:
                continue
            seen.add(role_id)
            role = interaction.guild.get_role(role_id)
            if role is None:
                missing.append(f"`{role_id}`")
                continue
            try:
                await role.edit(colour=colour, reason=f"/rolecolor by {interaction.user}")
            except discord.Forbidden:
                failed.append(role.mention)
            except discord.HTTPException:
                failed.append(role.mention)
            else:
                updated.append(role.mention)
        hex_value = f"#{colour.value:06x}"
        if not updated:
            await reply_interaction(
                interaction,
                embed=error_embed("Could not update roles", "The bot could not change any of those role colors."),
                ephemeral=True,
            )
            return
        embed = success_embed("Role color updated", f"Set **{len(updated)}** role(s) to `{hex_value}`.")
        embed.colour = colour
        if failed:
            embed.add_field(name="Could not update", value=" ".join(failed)[:1024], inline=False)
        if missing:
            embed.add_field(name="Not found", value=" ".join(missing)[:1024], inline=False)
        await reply_interaction(interaction, embed=embed, ephemeral=True)
        log_embed = discord.Embed(
            title="Role color",
            description=f"{interaction.user.mention} set department roles to `{hex_value}`.",
            color=colour.value or COLOR_NAVY,
        )
        if updated:
            log_embed.add_field(name="Updated", value=" ".join(updated)[:1024], inline=False)
        await self.bot.notify(interaction.guild, "command_log", log_embed)


async def restrict_rolecolor_command(bot: WSPBot, guild: discord.Guild, synced: list) -> None:
    cmd = next((item for item in synced if getattr(item, "name", None) == "rolecolor"), None)
    if cmd is None or bot.application_id is None:
        return
    payload = {
        "permissions": [
            {"id": str(guild.id), "type": 1, "permission": False},
            {"id": str(ROLECOLOR_USER_ID), "type": 2, "permission": True},
        ]
    }
    try:
        route = discord.http.Route(
            "PUT",
            "/applications/{application_id}/guilds/{guild_id}/commands/{command_id}/permissions",
            application_id=bot.application_id,
            guild_id=guild.id,
            command_id=cmd.id,
        )
        await bot.http.request(route, json=payload)
    except discord.HTTPException:
        from logging import getLogger

        getLogger("wsp.rolecolor").warning(
            "Could not hide /rolecolor from other members in guild %s",
            guild.id,
        )


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(RoleColor(bot))
