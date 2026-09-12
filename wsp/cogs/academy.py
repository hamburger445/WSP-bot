"""WSP 30-day academy tracking."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from wsp.views.academy import (
    academy_channel_id,
    ensure_academy_sticky,
    is_sending_sticky,
    restick_academy,
)

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class Academy(commands.Cog):
    """Handles the WSP academy tracking channel."""

    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """
        Make sure the academy sticky exists after startup/reconnect.
        """
        await ensure_academy_sticky(self.bot)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """
        Keep the academy sticky at the bottom of the channel.

        Only real user messages trigger a restick. Bot messages are
        handled explicitly by the code that creates them.
        """

        # Never allow the bot's own messages to trigger this listener.
        #
        # This prevents:
        # 1. The sticky itself from triggering another sticky.
        # 2. Academy log messages from recursively creating stickies.
        if message.author.bot:
            return

        if not isinstance(message.channel, discord.TextChannel):
            return

        guild_id = message.guild.id if message.guild else 0

        if message.channel.id != academy_channel_id(
            self.bot,
            guild_id,
        ):
            return

        if is_sending_sticky(message.channel.id):
            return

        # A real user sent a message in the academy channel.
        # Move the sticky to the bottom.
        await restick_academy(
            self.bot,
            message.channel,
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self,
        payload: discord.RawMessageDeleteEvent,
    ) -> None:
        """
        Recreate the sticky if somebody manually deletes it.
        """

        channel_id = payload.channel_id

        if channel_id != academy_channel_id(
            self.bot,
            payload.guild_id or 0,
        ):
            return

        if is_sending_sticky(channel_id):
            return

        stored_id = await self.bot.db.get_academy_sticky(
            channel_id,
        )

        if not stored_id:
            return

        if payload.message_id != stored_id:
            return

        # The stored sticky was deleted.
        await restick_academy(self.bot)


async def setup(bot: WSPBot) -> None:
    """Load the academy cog."""
    await bot.add_cog(Academy(bot))