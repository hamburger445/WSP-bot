"""Keep the on-duty tracker sticky while anyone is on shift."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from wsp.views.duty_tracker import (
    duty_tracker_channel_id,
    ensure_duty_tracker,
    is_sending_tracker,
    refresh_duty_tracker,
)

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class DutyTracker(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await ensure_duty_tracker(self.bot)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.TextChannel) or message.guild is None:
            return
        if message.channel.id != duty_tracker_channel_id(self.bot, message.guild.id):
            return
        if is_sending_tracker(message.channel.id):
            return
        await refresh_duty_tracker(self.bot, message.guild, restick=True)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.channel_id != duty_tracker_channel_id(self.bot, payload.guild_id or 0):
            return
        if is_sending_tracker(payload.channel_id):
            return
        stored_id = await self.bot.db.get_academy_sticky(payload.channel_id)
        if not stored_id or payload.message_id != stored_id:
            return
        guild = self.bot.get_guild(payload.guild_id or 0)
        if guild is None:
            return
        await refresh_duty_tracker(self.bot, guild, restick=True)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(DutyTracker(bot))
