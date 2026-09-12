"""Sticky 30-day academy tracking channel."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from wsp.views.academy import (
    academy_channel_id,
    ensure_academy_sticky,
    is_sending_sticky,
    is_sticky_message,
    restick_academy,
)

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class Academy(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await ensure_academy_sticky(self.bot)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return
        guild_id = message.guild.id if message.guild else 0
        if channel.id != academy_channel_id(self.bot, guild_id):
            return
        if is_sending_sticky(channel.id):
            return
        stored_id = await self.bot.db.get_academy_sticky(channel.id)
        if stored_id and message.id == stored_id:
            return
        if is_sticky_message(self.bot, message):
            return
        await restick_academy(self.bot, channel)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.channel_id != academy_channel_id(self.bot, payload.guild_id or 0):
            return
        if is_sending_sticky(payload.channel_id):
            return
        stored_id = await self.bot.db.get_academy_sticky(payload.channel_id)
        if stored_id and payload.message_id == stored_id:
            await restick_academy(self.bot)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Academy(bot))
