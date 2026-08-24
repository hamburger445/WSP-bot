"""Ticket panel, type select, and channel controls."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from wsp.constants import COLOR_GOLD, COLOR_NAVY, TICKET_TYPES
from wsp.db import now_ts
from wsp.embeds import base_embed, error_embed, success_embed, ts
from wsp.permissions import PermissionLevel, resolve_level

if TYPE_CHECKING:
    from wsp.bot import WSPBot

log = logging.getLogger("wsp.tickets")

TYPE_LABELS = dict(TICKET_TYPES)


class TicketPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.select(
        custom_id="wsp:ticket:type",
        placeholder="Select a request type to open a ticket…",
        options=[
            discord.SelectOption(label=label, value=key, description=f"Open a {label.lower()} ticket")
            for key, label in TICKET_TYPES
        ],
    )
    async def select_type(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        ticket_type = select.values[0]
        await interaction.response.send_modal(TicketOpenModal(ticket_type))


class TicketOpenModal(discord.ui.Modal):
    def __init__(self, ticket_type: str) -> None:
        super().__init__(title=f"{TYPE_LABELS.get(ticket_type, 'Ticket')} request")
        self.ticket_type = ticket_type
        self.summary = discord.ui.TextInput(
            label="Summary",
            placeholder="Briefly describe your request",
            max_length=200,
        )
        self.details = discord.ui.TextInput(
            label="Details",
            style=discord.TextStyle.paragraph,
            placeholder="Provide any additional information Command/HR should know.",
            required=False,
            max_length=1500,
        )
        self.add_item(self.summary)
        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(embed=error_embed("Unavailable", "Use this in the WSP server."), ephemeral=True)
            return
        cfg = await bot.guild_config(guild.id)
        category_id = cfg.category_id("tickets")
        category = guild.get_channel(category_id) if category_id else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                embed=error_embed("Tickets not configured", "A ticket category has not been set. Ask an owner to run `/setupserver`."),
                ephemeral=True,
            )
            return

        existing = await bot.db.fetchone(
            "SELECT * FROM tickets WHERE guild_id = ? AND opener_id = ? AND status = 'open' AND ticket_type = ?",
            (str(guild.id), str(interaction.user.id), self.ticket_type),
        )
        if existing and existing["channel_id"]:
            await interaction.response.send_message(
                embed=error_embed("Already open", f"You already have an open ticket: <#{existing['channel_id']}>"),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        ticket_id = await bot.db.create_ticket(guild.id, interaction.user.id, self.ticket_type, None)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        }
        for key in ("hr", "command", "superintendent"):
            rid = cfg.role_id(key)
            role = guild.get_role(rid) if rid else None
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True, manage_messages=True
                )
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True, read_message_history=True
            )

        label = TYPE_LABELS.get(self.ticket_type, self.ticket_type)
        name = f"{self.ticket_type}-{interaction.user.name}"[:95]
        try:
            channel = await guild.create_text_channel(
                name=name,
                category=category,
                overwrites=overwrites,
                topic=f"WSP {label} ticket #{ticket_id} • {interaction.user}",
                reason=f"WSP ticket #{ticket_id}",
            )
        except discord.Forbidden:
            await interaction.followup.send(embed=error_embed("Cannot create channel", "The bot needs Manage Channels."), ephemeral=True)
            return

        await bot.db.update_ticket(ticket_id, channel_id=str(channel.id))
        embed = base_embed(
            f"{label}  •  Ticket #{ticket_id}",
            f"{interaction.user.mention} opened this ticket.\n\n**Summary**\n{self.summary.value}",
            color=COLOR_GOLD,
        )
        if self.details.value:
            embed.add_field(name="Details", value=self.details.value[:1024], inline=False)
        embed.add_field(name="Opened", value=ts(now_ts()), inline=True)
        await channel.send(content=interaction.user.mention, embed=embed, view=TicketControlsView())
        await bot.db.add_ticket_message(ticket_id, interaction.user.id, str(interaction.user), f"[OPEN] {self.summary.value}")
        await bot.db.audit(
            guild.id,
            "ticket_open",
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
            target_id=channel.id,
            details=f"{label} #{ticket_id}",
        )
        await bot.notify(
            guild,
            "tickets_log",
            base_embed("Ticket opened", f"{label} ticket #{ticket_id} by {interaction.user.mention} in {channel.mention}"),
        )
        if self.ticket_type == "resignation":
            await bot.notify(
                guild,
                "resignations",
                base_embed("Resignation ticket opened", f"{interaction.user.mention} opened ticket #{ticket_id}."),
            )
        await interaction.followup.send(
            embed=success_embed("Ticket opened", f"Continue in {channel.mention}."),
            ephemeral=True,
        )


class TicketControlsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _ticket_id(self, interaction: discord.Interaction) -> int | None:
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        if interaction.channel_id:
            row = await bot.db.get_ticket_by_channel(interaction.channel_id)
            if row:
                return int(row["id"])
        return None

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.secondary, custom_id="wsp:ticket:claim")
    async def claim(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        level = await resolve_level(interaction)
        if level < PermissionLevel.HR:
            await interaction.response.send_message(embed=error_embed("Restricted", "Only HR or Command can claim tickets."), ephemeral=True)
            return
        ticket_id = await self._ticket_id(interaction)
        if not ticket_id:
            await interaction.response.send_message(embed=error_embed("Not found", "This channel is not a ticket."), ephemeral=True)
            return
        await bot.db.update_ticket(ticket_id, claimed_by=str(interaction.user.id))
        await bot.db.audit(
            interaction.guild_id or 0,
            "ticket_claim",
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
            details=f"Ticket #{ticket_id}",
        )
        await interaction.response.send_message(embed=success_embed("Claimed", f"{interaction.user.mention} is handling this ticket."))

    @discord.ui.button(label="Close ticket", style=discord.ButtonStyle.danger, custom_id="wsp:ticket:close")
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        ticket_id = await self._ticket_id(interaction)
        await interaction.response.send_modal(TicketCloseModal(ticket_id))


class TicketCloseModal(discord.ui.Modal, title="Close ticket"):
    reason = discord.ui.TextInput(label="Close reason", style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, ticket_id: int | None) -> None:
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
            return
        level = await resolve_level(interaction)
        ticket = None
        if self.ticket_id:
            ticket = await bot.db.get_ticket(self.ticket_id)
        elif interaction.channel_id:
            ticket = await bot.db.get_ticket_by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message(embed=error_embed("Not found", "Ticket record missing."), ephemeral=True)
            return
        opener_ok = str(interaction.user.id) == str(ticket["opener_id"])
        if level < PermissionLevel.HR and not opener_ok:
            await interaction.response.send_message(embed=error_embed("Restricted", "Only HR/Command or the opener can close this ticket."), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await _close_ticket(bot, guild, ticket, interaction.user, str(self.reason.value))
        await interaction.followup.send(embed=success_embed("Ticket closed", "Transcript saved. This channel will be removed shortly."), ephemeral=True)


async def _close_ticket(bot: WSPBot, guild: discord.Guild, ticket, closer: discord.abc.User, reason: str) -> None:
    channel = guild.get_channel(int(ticket["channel_id"])) if ticket["channel_id"] else None
    lines = [f"WSP Ticket #{ticket['id']} ({ticket['ticket_type']})", f"Closed by {closer} — {reason}", ""]
    html = [
        "<html><head><meta charset='utf-8'><title>WSP Ticket Transcript</title>",
        "<style>body{font-family:Georgia,serif;background:#0d2137;color:#f4efe4;padding:24px} .m{margin:8px 0;border-bottom:1px solid #c9a22733;padding-bottom:8px} .a{color:#c9a227}</style></head><body>",
        f"<h1>Wisconsin State Patrol — Ticket #{ticket['id']}</h1>",
        f"<p>Type: {ticket['ticket_type']} • Closed by {closer}</p>",
    ]
    if isinstance(channel, discord.TextChannel):
        try:
            async for message in channel.history(limit=500, oldest_first=True):
                stamp = message.created_at.strftime("%Y-%m-%d %H:%M")
                content = message.content or ""
                for embed in message.embeds:
                    if embed.title:
                        content += f" [{embed.title}]"
                    if embed.description:
                        content += f" {embed.description}"
                await bot.db.add_ticket_message(int(ticket["id"]), message.author.id, str(message.author), content)
                lines.append(f"[{stamp}] {message.author}: {content}")
                html.append(f"<div class='m'><span class='a'>{stamp} {message.author}</span><br>{discord.utils.escape_markdown(content)}</div>")
        except discord.HTTPException:
            pass

    path = bot.settings.transcripts_dir / f"ticket-{ticket['id']}.txt"
    html_path = bot.settings.transcripts_dir / f"ticket-{ticket['id']}.html"
    bot.settings.transcripts_dir.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    html.append("</body></html>")
    html_path.write_text("\n".join(html), encoding="utf-8")

    await bot.db.update_ticket(
        int(ticket["id"]),
        status="closed",
        closed_at=now_ts(),
        close_reason=reason,
        transcript_path=str(path),
    )
    await bot.db.audit(
        guild.id,
        "ticket_close",
        actor_id=closer.id,
        actor_name=str(closer),
        details=f"Ticket #{ticket['id']}: {reason}",
    )
    log_embed = base_embed(
        f"Ticket #{ticket['id']} closed",
        f"Type: **{TYPE_LABELS.get(ticket['ticket_type'], ticket['ticket_type'])}**\nClosed by {closer.mention}\nReason: {reason}",
        color=COLOR_NAVY,
    )
    await bot.notify(guild, "tickets_log", log_embed)
    if ticket["ticket_type"] == "resignation":
        personnel = await bot.db.get_personnel(guild.id, int(ticket["opener_id"]))
        if personnel:
            await bot.db.update_personnel(personnel["id"], status="resigned")
        await bot.notify(
            guild,
            "resignations",
            base_embed("Resignation processed", f"<@{ticket['opener_id']}> — ticket #{ticket['id']} closed."),
        )
        await bot.notify(guild, "notifications", base_embed("Resignation", f"<@{ticket['opener_id']}> resignation ticket was closed."))

    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=success_embed("Closed", "This channel will be deleted in a few seconds."))
            await channel.delete(reason=f"WSP ticket #{ticket['id']} closed")
        except discord.HTTPException:
            log.warning("Could not delete ticket channel %s", getattr(channel, "id", None))
