"""Discord role + internal permission-level checks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

import discord
from discord import app_commands

from wsp.constants import LEVEL_LABELS, PermissionLevel

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class InsufficientPermission(app_commands.CheckFailure):
    def __init__(self, required: PermissionLevel) -> None:
        self.required = required
        super().__init__(f"Requires {LEVEL_LABELS[required]} access or higher.")


async def resolve_level(interaction: discord.Interaction, member: discord.Member | None = None) -> PermissionLevel:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    user = member or (interaction.user if isinstance(interaction.user, discord.Member) else None)
    if user is None:
        return PermissionLevel.TROOPER
    if user.id in bot.settings.owner_ids:
        return PermissionLevel.OWNER

    cfg = await bot.guild_config(interaction.guild_id or 0)
    roles = {r.id for r in user.roles}

    if cfg.role_id("superintendent") in roles:
        return PermissionLevel.SUPERINTENDENT
    if cfg.role_id("command") in roles:
        return PermissionLevel.COMMAND
    if cfg.role_id("hr") in roles:
        return PermissionLevel.HR
    if cfg.role_id("supervisor") in roles:
        return PermissionLevel.SUPERVISOR

    record = await bot.db.get_personnel(interaction.guild_id or 0, user.id)
    if record and record["rank_level"]:
        rank_level = int(record["rank_level"])
        mapped = {
            5: PermissionLevel.SUPERINTENDENT,
            4: PermissionLevel.COMMAND,
            3: PermissionLevel.HR,
            2: PermissionLevel.SUPERVISOR,
            1: PermissionLevel.TROOPER,
        }.get(rank_level, PermissionLevel.TROOPER)
        # Rank-based HR is only for Lieutenant+ mapped as 3; still honour Discord HR role above.
        return mapped

    if cfg.role_id("wsp") in roles:
        return PermissionLevel.TROOPER
    return PermissionLevel.TROOPER


def has_level(required: PermissionLevel) -> Callable:
    async def predicate(interaction: discord.Interaction) -> bool:
        level = await resolve_level(interaction)
        if level >= required:
            return True
        raise InsufficientPermission(required)

    return app_commands.check(predicate)


def is_owner() -> Callable:
    async def predicate(interaction: discord.Interaction) -> bool:
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        if interaction.user.id in bot.settings.owner_ids:
            return True
        raise InsufficientPermission(PermissionLevel.OWNER)

    return app_commands.check(predicate)


async def is_self_or_level(
    interaction: discord.Interaction, target_id: int, required: PermissionLevel
) -> bool:
    if interaction.user.id == target_id:
        return True
    return await resolve_level(interaction) >= required
