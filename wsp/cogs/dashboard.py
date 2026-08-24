"""Command dashboard with section navigation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_GOLD, COLOR_NAVY, PermissionLevel
from wsp.embeds import add_fields, base_embed, error_embed, format_duration, ts_rel
from wsp.permissions import has_level
from wsp.utils import mention_or_id

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class Dashboard(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    @app_commands.command(name="dashboard", description="Open the WSP command dashboard.")
    @has_level(PermissionLevel.HR)
    async def dashboard(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        embed = await overview_embed(self.bot, interaction.guild)
        await interaction.response.send_message(embed=embed, view=DashboardView(), ephemeral=True)

    @app_commands.command(name="audit", description="View recent department audit log entries.")
    @has_level(PermissionLevel.HR)
    async def audit(self, interaction: discord.Interaction, action: str | None = None) -> None:
        if not interaction.guild:
            return
        rows = await self.bot.db.list_audit(interaction.guild.id, 20, action)
        embed = base_embed("Audit log")
        embed.description = "\n".join(
            f"{ts_rel(r['created_at'])} `{r['action']}` {r['actor_name'] or ''} → {r['target_name'] or r['target_id'] or ''} {r['details'] or ''}"
            for r in rows
        )[:4000] or "No entries."
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def overview_embed(bot: WSPBot, guild: discord.Guild) -> discord.Embed:
    counts = await bot.db.dashboard_counts(guild.id)
    cfg = await bot.guild_config(guild.id)
    week = bot.db.week_start_ts(cfg.get("timezone") or "America/Chicago")
    week_id = await bot.db.ensure_week(guild.id, week)
    quota_rows = await bot.db.list_quota_records(week_id)
    duty = [r for r in quota_rows if r["quota_type"] == "duty"]
    complete = sum(1 for r in duty if (r["status"] == "complete") or int(r["completed_minutes"]) >= int(r["required_minutes"]))
    embed = base_embed("Command dashboard", "Wisconsin State Patrol  •  Lakeville Roleplay", color=COLOR_GOLD)
    add_fields(
        embed,
        [
            ("Active personnel", str(counts["active_personnel"]), True),
            ("On duty", str(counts["active_shifts"]), True),
            ("On LOA", str(counts["loa"]), True),
            ("On probation", str(counts["probation"]), True),
            ("Awaiting supervision", str(counts["awaiting_supervision"]), True),
            ("Quota complete (week)", f"{complete}/{len(duty) or 0}", True),
        ],
    )
    return embed


class DashboardView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=240)
        self.add_item(DashboardSelect())


class DashboardSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Open a dashboard section…",
            options=[
                discord.SelectOption(label="Overview", value="overview"),
                discord.SelectOption(label="Active shifts", value="shifts"),
                discord.SelectOption(label="Personnel on LOA", value="loa"),
                discord.SelectOption(label="Probationary personnel", value="probation"),
                discord.SelectOption(label="Awaiting supervision", value="supervision"),
                discord.SelectOption(label="Quota completion", value="quota"),
                discord.SelectOption(label="Recent promotions", value="promotions"),
                discord.SelectOption(label="Recent discipline", value="discipline"),
                discord.SelectOption(label="Recent resignations", value="resignations"),
                discord.SelectOption(label="Training activity", value="training"),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
            return
        key = self.values[0]
        embed = await section_embed(bot, guild, key)
        await interaction.response.edit_message(embed=embed, view=self.view)


async def section_embed(bot: WSPBot, guild: discord.Guild, key: str) -> discord.Embed:
    if key == "overview":
        return await overview_embed(bot, guild)
    if key == "shifts":
        rows = await bot.db.list_active_shifts(guild.id)
        embed = base_embed("Active shifts", color=COLOR_NAVY)
        embed.description = "\n".join(
            f"{mention_or_id(guild, r['discord_id'])} `{r['callsign'] or '—'}` {r['status']} {format_duration(bot.db.effective_shift_seconds(r))}"
            for r in rows
        ) or "No active shifts."
        return embed
    if key == "loa":
        rows = await bot.db.list_loa(guild.id, "approved")
        from wsp.db import now_ts

        now = now_ts()
        current = [r for r in rows if int(r["start_date"]) <= now <= int(r["end_date"])]
        embed = base_embed("Personnel on LOA")
        embed.description = "\n".join(
            f"{mention_or_id(guild, r['discord_id'])} — {r['reason']} ({ts_rel(r['end_date'])})"
            for r in current
        ) or "None."
        return embed
    if key == "probation":
        rows = await bot.db.list_active_probations(guild.id)
        embed = base_embed("Probationary personnel")
        embed.description = "\n".join(
            f"{mention_or_id(guild, r['discord_id'])} `{r['status']}` ends {ts_rel(r['expected_end'])}"
            for r in rows
        ) or "None."
        return embed
    if key == "supervision":
        rows = await bot.db.list_personnel(guild.id, "active")
        waiting = [r for r in rows if r["supervision_status"] == "required"]
        embed = base_embed("Awaiting supervision")
        embed.description = "\n".join(f"{mention_or_id(guild, r['discord_id'])} **{r['rank_name'] or ''}**" for r in waiting) or "None."
        return embed
    if key == "quota":
        cfg = await bot.guild_config(guild.id)
        week = bot.db.week_start_ts(cfg.get("timezone") or "America/Chicago")
        week_id = await bot.db.ensure_week(guild.id, week)
        rows = [r for r in await bot.db.list_quota_records(week_id) if r["quota_type"] == "duty"]
        embed = base_embed("Quota completion")
        embed.description = "\n".join(
            f"{mention_or_id(guild, r['discord_id'])} {r['completed_minutes']}/{r['required_minutes']} min `{r['status'] or 'in progress'}`"
            for r in rows[:20]
        ) or "No quota rows this week."
        return embed
    if key == "promotions":
        rows = await bot.db.list_audit(guild.id, 12, "promotion")
        embed = base_embed("Recent promotions")
        embed.description = "\n".join(f"{ts_rel(r['created_at'])} {r['target_name'] or r['target_id']} — {r['details']}" for r in rows) or "None."
        return embed
    if key == "discipline":
        rows = await bot.db.list_discipline(guild.id)
        embed = base_embed("Recent disciplinary actions")
        embed.description = "\n".join(
            f"{ts_rel(r['created_at'])} {mention_or_id(guild, r['discord_id'])} **{r['action']}**"
            for r in rows[:12]
        ) or "None."
        return embed
    if key == "resignations":
        rows = await bot.db.list_audit(guild.id, 12, "ticket_close")
        personnel = await bot.db.list_personnel(guild.id, "resigned")
        embed = base_embed("Recent resignations")
        embed.description = "\n".join(f"{mention_or_id(guild, r['discord_id'])} {ts_rel(r['updated_at'])}" for r in personnel[:12]) or "None on roster."
        return embed
    rows = await bot.db.list_audit(guild.id, 15, "training")
    embed = base_embed("Recent training activity")
    embed.description = "\n".join(f"{ts_rel(r['created_at'])} {r['target_name'] or ''} — {r['details']}" for r in rows) or "None."
    return embed


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Dashboard(bot))
