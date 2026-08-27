"""Discord role + internal permission-level checks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import PermissionLevel

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class InsufficientPermission(app_commands.CheckFailure):
    def __init__(self, required: PermissionLevel) -> None:
        self.required = required
        super().__init__("Restricted")


async def resolve_user_level(bot: WSPBot, guild_id: int, user: discord.abc.User) -> PermissionLevel:
    if user.id in bot.settings.owner_ids:
        return PermissionLevel.OWNER
    cfg = await bot.guild_config(guild_id)
    roles = {r.id for r in user.roles} if isinstance(user, discord.Member) else set()
    level = PermissionLevel.TROOPER
    if cfg.role_id("wsp") in roles:
        level = PermissionLevel.TROOPER
    if cfg.role_id("supervisor") in roles or cfg.role_id("middle_rank") in roles:
        level = PermissionLevel.SUPERVISOR
    if cfg.role_id("hr") in roles or cfg.role_id("high_rank") in roles:
        level = PermissionLevel.HR
    if cfg.role_id("command") in roles:
        level = PermissionLevel.COMMAND
    if cfg.role_id("superintendent") in roles:
        level = PermissionLevel.SUPERINTENDENT
    record = await bot.db.get_personnel(guild_id, user.id)
    if record and record["rank_level"]:
        mapped = {
            5: PermissionLevel.SUPERINTENDENT,
            4: PermissionLevel.COMMAND,
            3: PermissionLevel.HR,
            2: PermissionLevel.SUPERVISOR,
            1: PermissionLevel.TROOPER,
        }.get(int(record["rank_level"]), PermissionLevel.TROOPER)
        if mapped > level:
            level = mapped
    return level


async def resolve_level(interaction: discord.Interaction, member: discord.Member | None = None) -> PermissionLevel:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    user = member or interaction.user
    return await resolve_user_level(bot, interaction.guild_id or 0, user)


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


def prefix_has_level(required: PermissionLevel) -> Callable:
    async def predicate(ctx: commands.Context) -> bool:
        bot: WSPBot = ctx.bot  # type: ignore[assignment]
        guild_id = ctx.guild.id if ctx.guild else 0
        level = await resolve_user_level(bot, guild_id, ctx.author)
        if level >= required:
            return True
        raise commands.CheckFailure("Restricted")

    return commands.check(predicate)


def prefix_is_owner() -> Callable:
    async def predicate(ctx: commands.Context) -> bool:
        bot: WSPBot = ctx.bot  # type: ignore[assignment]
        if ctx.author.id in bot.settings.owner_ids:
            return True
        raise commands.CheckFailure("Restricted")

    return commands.check(predicate)


async def is_self_or_level(
    interaction: discord.Interaction, target_id: int, required: PermissionLevel
) -> bool:
    if interaction.user.id == target_id:
        return True
    return await resolve_level(interaction) >= required
