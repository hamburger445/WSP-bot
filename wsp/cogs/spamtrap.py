"""Spam-trap channel: sticky warning, softban, and 24-hour message purge."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from wsp.constants import COLOR_DANGER, FOOTER
from wsp.embeds import base_embed
from wsp.utils import mention_or_id

if TYPE_CHECKING:
    from wsp.bot import WSPBot

log = logging.getLogger("wsp.spamtrap")

DEFAULT_SPAM_TRAP_CHANNEL_ID = 1557906824391565432
DELETE_SECONDS = 86400
STICKY_TITLE = "Do not send messages here"

_restick_locks: dict[int, asyncio.Lock] = {}
_sending_sticky: set[int] = set()
_softbanning: set[int] = set()


def sticky_embed() -> discord.Embed:
    embed = discord.Embed(
        title=STICKY_TITLE,
        description=(
            "This channel is a **spam trap**. Do not talk here.\n\n"
            "If you send a message in this channel:\n"
            "• You will be **removed from the server**\n"
            "• Your messages from the **last 24 hours** will be deleted\n\n"
            "You can rejoin afterward. Do not post here again."
        ),
        color=COLOR_DANGER,
    )
    embed.set_footer(text=FOOTER)
    return embed


def spam_trap_channel_id(bot: WSPBot, guild_id: int = 0) -> int:
    if guild_id:
        cached = bot._config_cache.get(guild_id)
        if cached:
            configured = cached.channel_id("spam_trap")
            if configured:
                return configured
    return DEFAULT_SPAM_TRAP_CHANNEL_ID


def _lock_for(channel_id: int) -> asyncio.Lock:
    lock = _restick_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _restick_locks[channel_id] = lock
    return lock


async def trap_channel(bot: WSPBot, guild: discord.Guild | None = None) -> discord.TextChannel | None:
    channel_id = spam_trap_channel_id(bot, guild.id if guild else 0)
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            log.warning("Spam trap channel %s was not found", channel_id)
            return None
    if isinstance(channel, discord.TextChannel):
        return channel
    return None


async def restick_spam_trap(
    bot: WSPBot,
    channel: discord.TextChannel | None = None,
) -> discord.Message | None:
    if channel is None:
        channel = await trap_channel(bot)
    if channel is None:
        return None
    async with _lock_for(channel.id):
        previous_id = await bot.db.get_academy_sticky(channel.id)
        if previous_id:
            try:
                previous = await channel.fetch_message(previous_id)
                await previous.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.exception("Could not delete spam trap sticky %s", previous_id)
        _sending_sticky.add(channel.id)
        try:
            posted = await channel.send(embed=sticky_embed())
            await bot.db.set_academy_sticky(channel.id, posted.id)
            return posted
        except discord.HTTPException:
            log.exception("Could not post spam trap sticky in %s", channel.id)
            return None
        finally:
            _sending_sticky.discard(channel.id)


async def ensure_spam_trap_sticky(bot: WSPBot) -> None:
    channel = await trap_channel(bot)
    if channel is None:
        return
    stored_id = await bot.db.get_academy_sticky(channel.id)
    if stored_id:
        try:
            existing = await channel.fetch_message(stored_id)
        except discord.NotFound:
            existing = None
        except discord.HTTPException:
            log.exception("Could not fetch spam trap sticky %s", stored_id)
            return
        if existing is not None:
            latest = None
            async for message in channel.history(limit=1):
                latest = message
            if latest is not None and latest.id == existing.id:
                if existing.embeds and existing.embeds[0].title == STICKY_TITLE:
                    return
    await restick_spam_trap(bot, channel)


async def _softban(guild: discord.Guild, user: discord.abc.User, reason: str) -> bool:
    target = discord.Object(id=user.id)
    try:
        await guild.ban(
            target,
            reason=reason,
            delete_message_seconds=DELETE_SECONDS,
        )
    except discord.Forbidden:
        log.warning("Cannot softban %s: missing permission or role hierarchy", user.id)
        return False
    except discord.HTTPException:
        log.exception("Could not ban %s from spam trap", user.id)
        return False
    try:
        await guild.unban(target, reason="WSP spam trap softban")
    except discord.HTTPException:
        log.exception("Banned %s from spam trap but could not unban", user.id)
        return True
    return True


class SpamTrap(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await ensure_spam_trap_sticky(self.bot)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not isinstance(message.channel, discord.TextChannel) or message.guild is None:
            return
        if message.channel.id != spam_trap_channel_id(self.bot, message.guild.id):
            return
        if message.channel.id in _sending_sticky:
            return
        if message.author.bot:
            return
        if message.author.id in self.bot.settings.owner_ids:
            await restick_spam_trap(self.bot, message.channel)
            return
        if message.author.id in _softbanning:
            return
        _softbanning.add(message.author.id)
        try:
            await self.bot.try_dm(
                message.author,
                base_embed(
                    "Removed for speaking in a restricted channel",
                    "You posted in a spam-trap channel. You were removed from the server "
                    "and your messages from the last 24 hours were deleted.",
                    color=COLOR_DANGER,
                ),
            )
            banned = await _softban(
                message.guild,
                message.author,
                "Spoke in spam trap channel",
            )
            if not banned:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
            await self.bot.db.audit(
                message.guild.id,
                "spam_trap_softban",
                actor_id=self.bot.user.id if self.bot.user else None,
                actor_name=str(self.bot.user) if self.bot.user else "WSP Bot",
                target_id=message.author.id,
                target_name=str(message.author),
                details="Spoke in spam trap",
            )
            embed = base_embed(
                "Spam trap",
                f"{mention_or_id(message.guild, message.author.id)} was softbanned for talking in the trap channel. "
                "Messages from the last 24 hours were deleted.",
                color=COLOR_DANGER,
            )
            await self.bot.notify(message.guild, "command_log", embed)
            await self.bot.notify(message.guild, "hr_log", embed)
            await restick_spam_trap(self.bot, message.channel)
        finally:
            _softbanning.discard(message.author.id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        channel_id = payload.channel_id
        if channel_id != spam_trap_channel_id(self.bot, payload.guild_id or 0):
            return
        if channel_id in _sending_sticky:
            return
        stored_id = await self.bot.db.get_academy_sticky(channel_id)
        if not stored_id or payload.message_id != stored_id:
            return
        await restick_spam_trap(self.bot)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(SpamTrap(bot))
