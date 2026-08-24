"""Personnel profile with button navigation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_GOLD, COLOR_NAVY, PermissionLevel, SENSITIVE_PROFILE_LEVEL
from wsp.embeds import add_fields, base_embed, error_embed, format_duration, ts, ts_rel
from wsp.permissions import resolve_level
from wsp.utils import ensure_personnel, mention_or_id

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class Profile(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    @app_commands.command(name="profile", description="Open a WSP personnel profile.")
    async def profile(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        target = member or interaction.user
        level = await resolve_level(interaction)
        if target.id != interaction.user.id and level < SENSITIVE_PROFILE_LEVEL:
            await interaction.response.send_message(
                embed=error_embed("Restricted", "Only HR and Command can view another member's personnel file."),
                ephemeral=True,
            )
            return
        record = await ensure_personnel(self.bot, target)
        sensitive = level >= SENSITIVE_PROFILE_LEVEL or target.id == interaction.user.id
        # Troopers viewing themselves still hide command/HR notes unless HR+
        notes_ok = level >= SENSITIVE_PROFILE_LEVEL
        embed = await profile_overview(self.bot, interaction.guild, target, record, sensitive)
        view = ProfileView(target.id, notes_ok, sensitive)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def profile_overview(bot: WSPBot, guild: discord.Guild, member: discord.Member, record, sensitive: bool) -> discord.Embed:
    embed = base_embed(
        f"{member.display_name}",
        f"{member.mention}  •  Wisconsin State Patrol",
        color=COLOR_GOLD,
        author="Personnel file",
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    add_fields(
        embed,
        [
            ("Rank", record["rank_name"] or "Unassigned", True),
            ("Position", record["position"] or "Patrol", True),
            ("Callsign", record["callsign"] or "—", True),
            ("Status", (record["status"] or "active").title(), True),
            ("Joined", ts(record["join_date"]), True),
            ("Training", (record["training_status"] or "pending").replace("_", " ").title(), True),
            ("Supervision", (record["supervision_status"] or "none").replace("_", " ").title(), True),
            ("Probation", (record["probation_status"] or "none").replace("_", " ").title(), True),
        ],
    )
    if not sensitive:
        embed.set_footer(text="Limited view  •  Wisconsin State Patrol  •  Lakeville Roleplay")
    return embed


class ProfileView(discord.ui.View):
    def __init__(self, target_id: int, notes_ok: bool, sensitive: bool) -> None:
        super().__init__(timeout=180)
        self.target_id = target_id
        self.notes_ok = notes_ok
        self.sensitive = sensitive
        self.add_item(ProfileSelect(target_id, notes_ok, sensitive))


class ProfileSelect(discord.ui.Select):
    def __init__(self, target_id: int, notes_ok: bool, sensitive: bool) -> None:
        self.target_id = target_id
        self.notes_ok = notes_ok
        self.sensitive = sensitive
        options = [
            discord.SelectOption(label="Personnel information", value="info"),
            discord.SelectOption(label="Rank history", value="ranks"),
            discord.SelectOption(label="Training", value="training"),
            discord.SelectOption(label="Quota", value="quota"),
            discord.SelectOption(label="Discipline", value="discipline"),
            discord.SelectOption(label="Notes", value="notes"),
            discord.SelectOption(label="Activity", value="activity"),
        ]
        super().__init__(placeholder="Open a personnel section…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(embed=error_embed("Unavailable"), ephemeral=True)
            return
        member = guild.get_member(self.target_id)
        record = await bot.db.get_personnel(guild.id, self.target_id)
        if record is None or member is None:
            await interaction.response.send_message(embed=error_embed("Not found"), ephemeral=True)
            return
        choice = self.values[0]
        if choice in {"discipline", "notes"} and not self.notes_ok:
            await interaction.response.send_message(
                embed=error_embed("Restricted", "Sensitive personnel sections require HR or Command."),
                ephemeral=True,
            )
            return
        embed = await _section_embed(bot, guild, member, record, choice)
        await interaction.response.edit_message(embed=embed, view=self.view)


async def _section_embed(bot: WSPBot, guild: discord.Guild, member: discord.Member, record, choice: str) -> discord.Embed:
    if choice == "info":
        return await profile_overview(bot, guild, member, record, True)
    if choice == "ranks":
        rows = await bot.db.rank_history(record["id"])
        embed = base_embed(f"Rank history  •  {member.display_name}", color=COLOR_NAVY)
        if not rows:
            embed.description = "No promotions or demotions on file."
        else:
            embed.description = "\n".join(
                f"{ts_rel(r['created_at'])} **{r['action'].title()}** — {r['from_rank'] or '—'} → **{r['to_rank'] or '—'}**"
                for r in rows[:12]
            )
        return embed
    if choice == "training":
        rows = await bot.db.list_training(guild.id, member.id)
        embed = base_embed(f"Training  •  {member.display_name}")
        embed.add_field(name="Overall", value=(record["training_status"] or "pending").replace("_", " ").title(), inline=True)
        embed.description = "\n".join(f"• **{r['module']}** — `{r['status']}`" for r in rows) or "No modules recorded."
        return embed
    if choice == "quota":
        cfg = await bot.guild_config(guild.id)
        week = bot.db.week_start_ts(cfg.get("timezone") or "America/Chicago")
        week_id = await bot.db.ensure_week(guild.id, week)
        duty = await bot.db.get_quota_record(week_id, member.id, "duty")
        hrq = await bot.db.get_quota_record(week_id, member.id, "supervision")
        embed = base_embed(f"Quota  •  {member.display_name}")
        if duty:
            embed.add_field(name="Duty this week", value=f"{duty['completed_minutes']} / {duty['required_minutes']} min", inline=True)
        else:
            embed.add_field(name="Duty this week", value="No duty quota row yet", inline=True)
        if hrq:
            embed.add_field(name="HR supervision", value=f"{hrq['supervision_minutes']} / {hrq['required_minutes']} min", inline=True)
        totals = await bot.db.shift_totals(guild.id, member.id)
        if totals:
            embed.add_field(name="All-time duty", value=format_duration(totals["total_seconds"]), inline=True)
        return embed
    if choice == "discipline":
        rows = await bot.db.list_discipline(guild.id, member.id)
        embed = base_embed(f"Discipline  •  {member.display_name}")
        embed.description = "\n".join(f"{ts_rel(r['created_at'])} **{r['action']}** — {r['reason']}" for r in rows[:12]) or "No disciplinary records."
        return embed
    if choice == "notes":
        rows = await bot.db.list_notes(record["id"])
        embed = base_embed(f"Notes  •  {member.display_name}")
        embed.description = "\n\n".join(
            f"**{r['note_type'].upper()}** {ts_rel(r['created_at'])} — {mention_or_id(guild, r['author_id'])}\n{r['content']}"
            for r in rows[:8]
        ) or "No notes on file."
        return embed
    rows = await bot.db.activity_history(guild.id, member.id)
    embed = base_embed(f"Activity  •  {member.display_name}")
    embed.description = "\n".join(f"{ts_rel(r['created_at'])} `{r['activity_type']}` — {r['details']}" for r in rows[:15]) or "No activity recorded."
    return embed


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Profile(bot))
