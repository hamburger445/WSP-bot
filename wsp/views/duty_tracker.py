"""Sticky on-duty tracker with shared shift controls."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import discord

from wsp.constants import COLOR_NAVY
from wsp.embeds import base_embed, format_duration
from wsp.utils import callsign_from_nickname, current_shift_seconds, member_from_id, mention_or_id

if TYPE_CHECKING:
    from wsp.bot import WSPBot

log = logging.getLogger("wsp.duty_tracker")

DEFAULT_DUTY_TRACKER_CHANNEL_ID = 1240078977020727337
TRACKER_TITLE = "On duty"

_restick_locks: dict[int, asyncio.Lock] = {}
_sending: set[int] = set()


def duty_tracker_channel_id(bot: WSPBot, guild_id: int = 0) -> int:
    if guild_id:
        cached = bot._config_cache.get(guild_id)
        if cached:
            configured = cached.channel_id("duty_tracker")
            if configured:
                return configured
    return DEFAULT_DUTY_TRACKER_CHANNEL_ID


def is_sending_tracker(channel_id: int) -> bool:
    return channel_id in _sending


def _lock_for(channel_id: int) -> asyncio.Lock:
    lock = _restick_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _restick_locks[channel_id] = lock
    return lock


class DutyControlButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wsp:duty:(?P<action>start|pause|resume|end)"):
    def __init__(self, action: str = "start") -> None:
        from wsp.views.shifts import _SHIFT_CONTROL_STYLE

        label, style = _SHIFT_CONTROL_STYLE[action]
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"wsp:duty:{action}",
            )
        )
        self.action = action

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ):
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        from wsp.views.shifts import _end_shift, _pause_shift, _resume_shift, start_shift_for

        if self.action == "start":
            await start_shift_for(interaction)
        elif self.action == "pause":
            await _pause_shift(interaction)
        elif self.action == "resume":
            await _resume_shift(interaction)
        else:
            await _end_shift(interaction)


class DutyTrackerView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        for action in ("start", "pause", "resume", "end"):
            self.add_item(DutyControlButton(action))


async def tracker_channel(bot: WSPBot, guild: discord.Guild | None = None) -> discord.TextChannel | None:
    channel_id = duty_tracker_channel_id(bot, guild.id if guild else 0)
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            log.warning("Duty tracker channel %s was not found", channel_id)
            return None
    if isinstance(channel, discord.TextChannel):
        return channel
    return None


async def build_duty_tracker_embed(bot: WSPBot, guild: discord.Guild) -> discord.Embed | None:
    rows = await bot.db.list_active_shifts(guild.id)
    if not rows:
        return None
    lines: list[str] = []
    for row in rows[:20]:
        member = await member_from_id(bot, guild, int(row["discord_id"]))
        callsign = callsign_from_nickname(member) or row["callsign"] or "—"
        status = "paused" if row["status"] == "paused" else "on duty"
        lines.append(
            f"**{callsign}** {mention_or_id(guild, row['discord_id'])} • {status} • "
            f"{format_duration(current_shift_seconds(row))}"
        )
    embed = base_embed(
        TRACKER_TITLE,
        "Use the buttons below for **your** shift. You do not need `/shift menu`.",
        color=COLOR_NAVY,
    )
    embed.add_field(name="Troopers", value="\n".join(lines)[:1024], inline=False)
    return embed


async def refresh_duty_tracker(
    bot: WSPBot,
    guild: discord.Guild,
    *,
    restick: bool = False,
) -> None:
    channel = await tracker_channel(bot, guild)
    if channel is None:
        return
    async with _lock_for(channel.id):
        embed = await build_duty_tracker_embed(bot, guild)
        stored_id = await bot.db.get_academy_sticky(channel.id)
        if embed is None:
            if stored_id:
                try:
                    previous = await channel.fetch_message(stored_id)
                    await previous.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    log.exception("Could not hide duty tracker %s", stored_id)
                await bot.db.clear_academy_sticky(channel.id)
            return
        if stored_id and not restick:
            try:
                previous = await channel.fetch_message(stored_id)
                await previous.edit(embed=embed, view=DutyTrackerView())
                return
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.exception("Could not edit duty tracker %s", stored_id)
        if stored_id:
            try:
                previous = await channel.fetch_message(stored_id)
                await previous.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.exception("Could not delete duty tracker %s", stored_id)
        _sending.add(channel.id)
        try:
            posted = await channel.send(embed=embed, view=DutyTrackerView())
            await bot.db.set_academy_sticky(channel.id, posted.id)
        except discord.HTTPException:
            log.exception("Could not post duty tracker in %s", channel.id)
        finally:
            _sending.discard(channel.id)


async def ensure_duty_tracker(bot: WSPBot) -> None:
    for guild in bot.guilds:
        try:
            await refresh_duty_tracker(bot, guild)
        except Exception:
            log.exception("Could not restore duty tracker for guild %s", guild.id)
