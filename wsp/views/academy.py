"""Persistent academy tracking sticky and recruit picker."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from wsp.constants import COLOR_NAVY, FOOTER, PermissionLevel
from wsp.db import now_ts
from wsp.permissions import resolve_level

if TYPE_CHECKING:
    from wsp.bot import WSPBot


log = logging.getLogger("wsp.academy")


ACADEMY_DAYS = 30
DEFAULT_ACADEMY_CHANNEL_ID = 1548155827121295480
CREATE_BUTTON_ID = "wsp:academy:create"


_restick_locks: dict[int, asyncio.Lock] = {}
_sending_sticky: set[int] = set()


def sticky_embed() -> discord.Embed:
    """Create the main academy tracking sticky embed."""
    embed = discord.Embed(
        title="WSP Academy 30-Day Tracking",
        description=(
            "Newly hired cadets have 30 days to complete "
            "their academy training."
        ),
        color=COLOR_NAVY,
    )

    embed.set_footer(text=FOOTER)

    return embed


def academy_log_embed(
    username: str,
    discord_id: int,
    hire_ts: int,
    deadline_ts: int,
) -> discord.Embed:
    """Create an individual recruit's 30-day academy log."""
    embed = discord.Embed(
        title="WSP Academy 30-Day Tracking",
        description=(
            f"> **Name:** `{username}`\n"
            f"> **Discord ID:** `{discord_id}`\n"
            f"> **Hire Date:** <t:{hire_ts}:F>\n"
            f"> **Training Deadline:** <t:{deadline_ts}:F>\n"
            f"> **Time Remaining:** <t:{deadline_ts}:R>"
        ),
        color=COLOR_NAVY,
    )

    embed.set_footer(text=FOOTER)

    return embed


def academy_channel_id(
    bot: WSPBot,
    guild_id: int = 0,
) -> int:
    """Get the configured academy channel ID."""
    if guild_id:
        cached = bot._config_cache.get(guild_id)

        if cached:
            configured = cached.channel_id("academy")

            if configured:
                return configured

    return DEFAULT_ACADEMY_CHANNEL_ID


def _lock_for(channel_id: int) -> asyncio.Lock:
    """Get or create the restick lock for a channel."""
    lock = _restick_locks.get(channel_id)

    if lock is None:
        lock = asyncio.Lock()
        _restick_locks[channel_id] = lock

    return lock


def is_sending_sticky(channel_id: int) -> bool:
    """Check whether the bot is currently sending a sticky."""
    return channel_id in _sending_sticky


def is_sticky_message(
    bot: WSPBot,
    message: discord.Message,
) -> bool:
    """Check whether a message is the academy sticky."""
    if not bot.user:
        return False

    if message.author.id != bot.user.id:
        return False

    if not message.embeds:
        return False

    if message.embeds[0].title != "WSP Academy 30-Day Tracking":
        return False

    return bool(message.components)


async def sticky_channel(
    bot: WSPBot,
    guild: discord.Guild | None = None,
) -> discord.TextChannel | None:
    """Get the academy tracking channel."""
    channel_id = academy_channel_id(
        bot,
        guild.id if guild else 0,
    )

    channel = bot.get_channel(channel_id)

    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            log.warning(
                "Academy channel %s was not found",
                channel_id,
            )
            return None

    if isinstance(channel, discord.TextChannel):
        return channel

    return None


async def restick_academy(
    bot: WSPBot,
    channel: discord.TextChannel | None = None,
) -> discord.Message | None:
    """
    Delete the previous sticky and create a new one at the bottom.
    """

    if channel is None:
        channel = await sticky_channel(bot)

    if channel is None:
        return None

    async with _lock_for(channel.id):
        previous_id = await bot.db.get_academy_sticky(channel.id)

        # Delete the previous sticky if it exists.
        if previous_id:
            try:
                previous = await channel.fetch_message(previous_id)
                await previous.delete()

            except discord.NotFound:
                # Already deleted.
                pass

            except discord.HTTPException:
                log.exception(
                    "Could not delete academy sticky %s",
                    previous_id,
                )

        # Mark the channel as currently sending a sticky.
        # This prevents message listeners from reacting to the
        # sticky being created.
        _sending_sticky.add(channel.id)

        try:
            posted = await channel.send(
                embed=sticky_embed(),
                view=AcademyStickyView(),
            )

            # Save the new sticky message ID.
            await bot.db.set_academy_sticky(
                channel.id,
                posted.id,
            )

            return posted

        except discord.HTTPException:
            log.exception(
                "Could not post academy sticky in %s",
                channel.id,
            )

            return None

        finally:
            _sending_sticky.discard(channel.id)


async def ensure_academy_sticky(bot: WSPBot) -> None:
    """
    Make sure the academy sticky exists and is the newest message.
    """

    channel = await sticky_channel(bot)

    if channel is None:
        return

    stored_id = await bot.db.get_academy_sticky(channel.id)

    if stored_id:
        try:
            existing = await channel.fetch_message(stored_id)

        except discord.NotFound:
            existing = None

        except discord.HTTPException:
            log.exception(
                "Could not fetch academy sticky %s",
                stored_id,
            )
            return

        if existing is not None:
            latest = None

            async for message in channel.history(limit=1):
                latest = message

            # The sticky already exists and is the latest message.
            if latest is not None and latest.id == existing.id:
                return

    # Sticky doesn't exist or isn't the latest message.
    await restick_academy(bot, channel)


class AcademyRecruitSelect(discord.ui.UserSelect):
    """Recruit selector used when creating an academy log."""

    def __init__(self) -> None:
        super().__init__(
            placeholder="Select the recruit",
            min_values=1,
            max_values=1,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ) -> None:
        view: AcademyRecruitView = self.view  # type: ignore[assignment]

        await view.create_log(
            interaction,
            self.values[0],
        )


class AcademyRecruitView(discord.ui.View):
    """Recruit selection menu."""

    def __init__(self) -> None:
        super().__init__(timeout=120)

        self.add_item(AcademyRecruitSelect())

    async def create_log(
        self,
        interaction: discord.Interaction,
        recruit: discord.User | discord.Member,
    ) -> None:
        bot: WSPBot = interaction.client  # type: ignore[assignment]

        # Verify the user is authorized WSP staff.
        if await resolve_level(interaction) < PermissionLevel.HR:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Restricted",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Restricted",
                    ephemeral=True,
                )

            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "Restricted",
                ephemeral=True,
            )
            return

        # The exact moment the recruit is selected is used
        # as the hire time.
        hire = now_ts()

        # Exactly 30 days after the hire time.
        deadline = hire + (ACADEMY_DAYS * 86400)

        username = recruit.name

        # Create the academy record in the existing database.
        log_id = await bot.db.create_academy_log(
            interaction.guild.id,
            recruit.id,
            username,
            hire,
            deadline,
            interaction.user.id,
        )

        channel = interaction.channel

        if not isinstance(channel, discord.TextChannel):
            channel = await sticky_channel(
                bot,
                interaction.guild,
            )

        if channel is None:
            await interaction.response.send_message(
                "Not found",
                ephemeral=True,
            )
            return

        # Send the recruit's academy log.
        #
        # This is a bot message, so Academy.on_message now
        # ignores it and will NOT create another sticky.
        posted = await channel.send(
            embed=academy_log_embed(
                username,
                recruit.id,
                hire,
                deadline,
            )
        )

        # Save the academy log's message ID.
        await bot.db.set_academy_log_message(
            log_id,
            posted.id,
        )

        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Created",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Created",
                ephemeral=True,
            )

        self.stop()


class AcademyStickyView(discord.ui.View):
    """Persistent button attached to the academy sticky."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Create 30-Day Academy Log",
        style=discord.ButtonStyle.primary,
        custom_id=CREATE_BUTTON_ID,
    )
    async def create_log(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        # Only authorized WSP staff can create academy logs.
        if await resolve_level(interaction) < PermissionLevel.HR:
            await interaction.response.send_message(
                "Restricted",
                ephemeral=True,
            )
            return

        # Open the recruit selector.
        await interaction.response.send_message(
            "Select the recruit for this 30-day academy log.",
            view=AcademyRecruitView(),
            ephemeral=True,
        )