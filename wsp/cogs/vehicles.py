"""Fleet / vehicle assignments (logged for audit completeness)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import PermissionLevel
from wsp.embeds import base_embed, error_embed, success_embed, ts_rel
from wsp.permissions import has_level
from wsp.utils import mention_or_id

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class Vehicles(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    vehicle = app_commands.Group(name="vehicle", description="Fleet assignments")

    @vehicle.command(name="assign", description="Assign a department vehicle to a member.")
    @has_level(PermissionLevel.COMMAND)
    async def assign(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        vehicle: str,
        plate: str | None = None,
        notes: str | None = None,
    ) -> None:
        if not interaction.guild:
            return
        vid = await self.bot.db.assign_vehicle(interaction.guild.id, member.id, vehicle, plate, interaction.user.id, notes)
        await self.bot.db.audit(
            interaction.guild.id, "vehicle_assign", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=member.id, target_name=str(member), details=f"{vehicle} {plate or ''} #{vid}",
        )
        await interaction.response.send_message(
            embed=success_embed("Vehicle assigned", f"{member.mention}  •  **{vehicle}** `{plate or 'unplated'}`"),
            ephemeral=True,
        )

    @assign.autocomplete("vehicle")
    async def vehicle_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        cfg = await self.bot.guild_config(interaction.guild_id or 0)
        fleet = cfg.get("vehicles", "fleet") or []
        return [app_commands.Choice(name=v, value=v) for v in fleet if current.lower() in v.lower()][:25]

    @vehicle.command(name="release", description="Release a vehicle assignment.")
    @has_level(PermissionLevel.COMMAND)
    async def release(self, interaction: discord.Interaction, assignment_id: int, reason: str) -> None:
        if not interaction.guild:
            return
        row = await self.bot.db.get_vehicle(assignment_id)
        if row is None:
            await interaction.response.send_message(embed=error_embed("Not found"), ephemeral=True)
            return
        await self.bot.db.update_vehicle(assignment_id, status="released")
        await self.bot.db.audit(
            interaction.guild.id, "vehicle_release", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=row["discord_id"], details=f"#{assignment_id} {reason}",
        )
        await interaction.response.send_message(embed=success_embed("Released", f"Assignment `#{assignment_id}`."), ephemeral=True)

    @vehicle.command(name="list", description="List vehicle assignments.")
    @has_level(PermissionLevel.SUPERVISOR)
    async def list_vehicles(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if not interaction.guild:
            return
        rows = await self.bot.db.list_vehicles(interaction.guild.id, member.id if member else None)
        embed = base_embed("Fleet assignments")
        embed.description = "\n".join(
            f"`#{r['id']}` {mention_or_id(interaction.guild, r['discord_id'])} **{r['vehicle_name']}** `{r['plate'] or '—'}` `{r['status']}` {ts_rel(r['created_at'])}"
            for r in rows[:20]
        ) or "No assignments."
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Vehicles(bot))
