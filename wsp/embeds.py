"""Consistent professional embeds and formatting helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import discord

from wsp.constants import COLOR_DANGER, COLOR_GOLD, COLOR_NAVY, COLOR_SUCCESS, COLOR_WARNING, FOOTER


def ts(value: int | float | datetime | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        unix = int(value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp())
    else:
        unix = int(value)
    return f"<t:{unix}:F>"


def ts_rel(value: int | float | None) -> str:
    if not value:
        return "—"
    return f"<t:{int(value)}:R>"


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def member_label(user: discord.abc.User | None, fallback_id: str | int | None = None) -> str:
    if user is not None:
        return f"{user.mention} (`{user.id}`)"
    if fallback_id:
        return f"`{fallback_id}`"
    return "Unknown"


def base_embed(
    title: str,
    description: str | None = None,
    *,
    color: int = COLOR_NAVY,
    author: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=FOOTER)
    embed.timestamp = datetime.now(timezone.utc)
    if author:
        embed.set_author(name=author)
    return embed


def success_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(title, description, color=COLOR_SUCCESS)


def warning_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(title, description, color=COLOR_WARNING)


def error_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(title, description, color=COLOR_DANGER)


def gold_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(title, description, color=COLOR_GOLD)


def add_fields(embed: discord.Embed, fields: list[tuple[str, Any, bool]]) -> discord.Embed:
    for name, value, inline in fields:
        text = "—" if value is None or value == "" else str(value)
        if len(text) > 1024:
            text = text[:1021] + "…"
        embed.add_field(name=name, value=text, inline=inline)
    return embed
