"""Discord role + internal permission-level checks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import DEFAULT_RANKS, PermissionLevel

if TYPE_CHECKING:
    from wsp.bot import WSPBot

_LEVEL_BY_RANK_PERM = {
    1: PermissionLevel.TROOPER,
    2: PermissionLevel.SUPERVISOR,
    3: PermissionLevel.HR,
    4: PermissionLevel.COMMAND,
    5: PermissionLevel.SUPERINTENDENT,
}
_POSITION_BY_NAME = {name: position for name, position, _level in DEFAULT_RANKS}
_PERM_BY_NAME = {name: _LEVEL_BY_RANK_PERM[level] for name, _position, level in DEFAULT_RANKS}


class InsufficientPermission(app_commands.CheckFailure):
    def __init__(self, required: PermissionLevel) -> None:
        self.required = required
        super().__init__("Restricted")


async def _role_ids(bot: WSPBot, guild_id: int, user: discord.abc.User) -> set[int]:
    if isinstance(user, discord.Member):
        return {role.id for role in user.roles}
    guild = bot.get_guild(guild_id)
    if guild is None:
        return set()
    member = guild.get_member(user.id)
    if member is None:
        try:
            member = await guild.fetch_member(user.id)
        except discord.HTTPException:
            return set()
    return {role.id for role in member.roles}


def _level_from_roles(cfg, roles: set[int]) -> PermissionLevel:
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
    rank_roles = cfg.get("rank_roles") or {}
    for name in rank_roles:
        rid = cfg.rank_role_id(str(name))
        if rid and rid in roles:
            mapped = _PERM_BY_NAME.get(str(name))
            if mapped and mapped > level:
                level = mapped
    return level


async def resolve_user_level(bot: WSPBot, guild_id: int, user: discord.abc.User) -> PermissionLevel:
    if user.id in bot.settings.owner_ids:
        return PermissionLevel.OWNER
    cfg = await bot.guild_config(guild_id)
    roles = await _role_ids(bot, guild_id, user)
    level = _level_from_roles(cfg, roles)
    record = await bot.db.get_personnel(guild_id, user.id)
    if record and record["rank_level"]:
        mapped = _LEVEL_BY_RANK_PERM.get(int(record["rank_level"]), PermissionLevel.TROOPER)
        if mapped > level:
            level = mapped
    return level


async def rank_position_for(bot: WSPBot, guild_id: int, user: discord.abc.User) -> int:
    if user.id in bot.settings.owner_ids:
        return 99
    cfg = await bot.guild_config(guild_id)
    roles = await _role_ids(bot, guild_id, user)
    best = 0
    if cfg.role_id("middle_rank") in roles or cfg.role_id("supervisor") in roles:
        best = max(best, _POSITION_BY_NAME["Sergeant"])
    if cfg.role_id("high_rank") in roles or cfg.role_id("hr") in roles:
        best = max(best, _POSITION_BY_NAME["Lieutenant"])
    if cfg.role_id("command") in roles:
        best = max(best, _POSITION_BY_NAME["Captain"])
    if cfg.role_id("superintendent") in roles:
        best = max(best, _POSITION_BY_NAME["Superintendent"])
    rank_roles = cfg.get("rank_roles") or {}
    for name in rank_roles:
        rid = cfg.rank_role_id(str(name))
        if rid and rid in roles:
            best = max(best, _POSITION_BY_NAME.get(str(name), 0))
    record = await bot.db.get_personnel(guild_id, user.id)
    if record and record["rank_position"]:
        best = max(best, int(record["rank_position"]))
    return best


async def can_manage_rank(
    bot: WSPBot,
    guild_id: int,
    actor: discord.abc.User,
    *,
    target_position: int,
    new_position: int | None = None,
) -> bool:
    actor_pos = await rank_position_for(bot, guild_id, actor)
    if actor_pos >= 99:
        return True
    if target_position and target_position >= actor_pos:
        return False
    if new_position is not None and new_position >= actor_pos:
        return False
    return True


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
