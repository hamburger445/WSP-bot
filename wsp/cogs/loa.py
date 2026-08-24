"""Leave of Absence requests and approvals."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_GOLD, PermissionLevel
from wsp.db import now_ts
from wsp.embeds import add_fields, base_embed, error_embed, success_embed, ts, warning_embed
from wsp.permissions import has_level
from wsp.utils import ensure_personnel, mention_or_id, parse_date

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class LOA(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    loa = app_commands.Group(name="loa", description="Leave of Absence")

    @loa.command(name="request", description="Submit a leave of absence request.")
    async def request(
        self,
        interaction: discord.Interaction,
        start_date: str,
        end_date: str,
        reason: str,
        additional_information: str | None = None,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        cfg = await self.bot.guild_config(interaction.guild.id)
        tz = cfg.get("timezone") or "America/Chicago"
        start = parse_date(start_date, tz)
        end = parse_date(end_date, tz)
        if not start or not end or end <= start:
            await interaction.response.send_message(
                embed=error_embed("Invalid dates", "Use `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`. End must be after start."),
                ephemeral=True,
            )
            return
        max_days = int(cfg.get("loa", "max_days") or 30)
        if end - start > max_days * 86400:
            await interaction.response.send_message(
                embed=error_embed("Too long", f"LOA may not exceed {max_days} days. Contact HR for exceptions."),
                ephemeral=True,
            )
            return
        await ensure_personnel(self.bot, interaction.user)
        loa_id = await self.bot.db.create_loa(interaction.guild.id, interaction.user.id, start, end, reason, additional_information)
        await self.bot.db.audit(
            interaction.guild.id, "loa_request", actor_id=interaction.user.id, actor_name=str(interaction.user),
            details=f"#{loa_id} {start_date} → {end_date}",
        )
        public = base_embed("LOA request submitted", f"{interaction.user.mention}  •  `{loa_id}`", color=COLOR_GOLD)
        add_fields(
            public,
            [
                ("Start", ts(start), True),
                ("End", ts(end), True),
                ("Reason", reason, False),
                ("Additional", additional_information or "—", False),
            ],
        )
        view = LOADecisionView(loa_id)
        await interaction.response.send_message(embed=success_embed("Request submitted", f"LOA `#{loa_id}` is pending HR review."), ephemeral=True)
        channel_id = cfg.channel_id("loa")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.TextChannel):
            await channel.send(embed=public, view=view)
        else:
            await self.bot.notify(interaction.guild, "hr_log", public)

    @loa.command(name="approve", description="Approve a pending LOA request.")
    @has_level(PermissionLevel.HR)
    async def approve(self, interaction: discord.Interaction, request_id: int, note: str | None = None) -> None:
        await _decide_loa(self.bot, interaction, request_id, "approved", note)

    @loa.command(name="deny", description="Deny a pending LOA request.")
    @has_level(PermissionLevel.HR)
    async def deny(self, interaction: discord.Interaction, request_id: int, note: str) -> None:
        await _decide_loa(self.bot, interaction, request_id, "denied", note)

    @loa.command(name="active", description="List personnel currently on approved LOA.")
    @has_level(PermissionLevel.HR)
    async def active(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        rows = await self.bot.db.list_loa(interaction.guild.id, "approved")
        now = now_ts()
        current = [r for r in rows if int(r["start_date"]) <= now <= int(r["end_date"])]
        embed = base_embed("Active LOA")
        embed.description = "\n".join(
            f"{mention_or_id(interaction.guild, r['discord_id'])} • {ts(r['start_date'])} → {ts(r['end_date'])} — {r['reason']}"
            for r in current
        ) or "No members are currently on approved leave."
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @loa.command(name="menu", description="Open the Leave of Absence menu.")
    async def loa_menu(self, interaction: discord.Interaction) -> None:
        embed = base_embed(
            "Leave of Absence",
            "Submit a request with `/loa request` using dates `YYYY-MM-DD`.\nApproved LOA prevents missed-quota flags for that window.\nHR uses `/loa approve` and `/loa deny`, or the buttons on the request post.",
        )
        await interaction.response.send_message(embed=embed, view=LOAMenuView(), ephemeral=True)


class LOAMenuView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)

    @discord.ui.button(label="Submit request", style=discord.ButtonStyle.primary)
    async def submit(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(LOARequestModal())

    @discord.ui.button(label="My requests", style=discord.ButtonStyle.secondary)
    async def mine(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        if not interaction.guild:
            return
        rows = await bot.db.fetchall(
            "SELECT * FROM loa_requests WHERE guild_id = ? AND discord_id = ? ORDER BY created_at DESC LIMIT 8",
            (str(interaction.guild.id), str(interaction.user.id)),
        )
        embed = base_embed("Your LOA requests")
        embed.description = "\n".join(
            f"`#{r['id']}` `{r['status']}` {ts(r['start_date'])} → {ts(r['end_date'])}"
            for r in rows
        ) or "No requests on file."
        await interaction.response.send_message(embed=embed, ephemeral=True)


class LOARequestModal(discord.ui.Modal, title="Leave of Absence request"):
    start_date = discord.ui.TextInput(label="Start date", placeholder="YYYY-MM-DD")
    end_date = discord.ui.TextInput(label="End date", placeholder="YYYY-MM-DD")
    reason = discord.ui.TextInput(label="Reason", max_length=200)
    additional = discord.ui.TextInput(label="Additional information", style=discord.TextStyle.paragraph, required=False, max_length=800)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        cog: LOA | None = bot.get_cog("LOA")  # type: ignore[assignment]
        if cog is None:
            await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
            return
        await cog.request.callback(
            cog,
            interaction,
            str(self.start_date.value),
            str(self.end_date.value),
            str(self.reason.value),
            str(self.additional.value) if self.additional.value else None,
        )


class ApproveLOAButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wsp:loa:approve:(?P<loa_id>[0-9]+)"):
    def __init__(self, loa_id: int = 0) -> None:
        super().__init__(
            discord.ui.Button(
                label="Approve",
                style=discord.ButtonStyle.success,
                custom_id=f"wsp:loa:approve:{loa_id}",
            )
        )
        self.loa_id = loa_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ):
        return cls(int(match["loa_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _decide_loa(interaction.client, interaction, self.loa_id, "approved", None)  # type: ignore[arg-type]


class DenyLOAButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wsp:loa:deny:(?P<loa_id>[0-9]+)"):
    def __init__(self, loa_id: int = 0) -> None:
        super().__init__(
            discord.ui.Button(
                label="Deny",
                style=discord.ButtonStyle.danger,
                custom_id=f"wsp:loa:deny:{loa_id}",
            )
        )
        self.loa_id = loa_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ):
        return cls(int(match["loa_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(LOADenyModal(self.loa_id))


class LOADecisionView(discord.ui.View):
    def __init__(self, loa_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(ApproveLOAButton(loa_id))
        self.add_item(DenyLOAButton(loa_id))


class LOADenyModal(discord.ui.Modal, title="Deny LOA"):
    note = discord.ui.TextInput(label="Reason for denial", style=discord.TextStyle.paragraph)

    def __init__(self, loa_id: int) -> None:
        super().__init__()
        self.loa_id = loa_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _decide_loa(interaction.client, interaction, self.loa_id, "denied", str(self.note.value))  # type: ignore[arg-type]


async def _decide_loa(bot: WSPBot, interaction: discord.Interaction, loa_id: int, status: str, note: str | None) -> None:
    if not interaction.guild:
        return
    from wsp.permissions import resolve_level

    if await resolve_level(interaction) < PermissionLevel.HR:
        if interaction.response.is_done():
            await interaction.followup.send(embed=error_embed("Restricted", "HR or Command required."), ephemeral=True)
        else:
            await interaction.response.send_message(embed=error_embed("Restricted", "HR or Command required."), ephemeral=True)
        return
    row = await bot.db.get_loa(loa_id)
    if row is None:
        await _reply(interaction, error_embed("Not found"))
        return
    if row["status"] != "pending":
        await _reply(interaction, error_embed("Already decided", f"This request is `{row['status']}`."))
        return
    await bot.db.update_loa(loa_id, status=status, reviewer_id=str(interaction.user.id), review_note=note)
    personnel = await bot.db.get_personnel(interaction.guild.id, int(row["discord_id"]))
    if personnel and status == "approved":
        await bot.db.update_personnel(personnel["id"], status="loa")
    if personnel and status == "denied" and personnel["status"] == "loa":
        await bot.db.update_personnel(personnel["id"], status="active")
    await bot.db.audit(
        interaction.guild.id, f"loa_{status}", actor_id=interaction.user.id, actor_name=str(interaction.user),
        target_id=row["discord_id"], details=f"#{loa_id} {note or ''}",
    )
    embed = success_embed(f"LOA {status}", f"{mention_or_id(interaction.guild, row['discord_id'])} • `{loa_id}`")
    if note:
        embed.add_field(name="Note", value=note, inline=False)
    await _reply(interaction, embed)
    await bot.notify(interaction.guild, "loa", embed)
    await bot.notify(interaction.guild, "notifications", embed)
    member = interaction.guild.get_member(int(row["discord_id"]))
    if member:
        try:
            await member.send(embed=embed)
        except discord.HTTPException:
            pass


async def _reply(interaction: discord.Interaction, embed: discord.Embed) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(LOA(bot))
