"""Weekly quota tracking. Does not auto-punish — notifies HR/Command only."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import PermissionLevel
from wsp.embeds import add_fields, base_embed, error_embed, success_embed
from wsp.permissions import has_level, resolve_level
from wsp.utils import member_from_id, mention_or_id, quota_required_minutes

if TYPE_CHECKING:
    from wsp.bot import WSPBot


async def apply_shift_quota(bot: WSPBot, guild_id: int, discord_id: int, duration_seconds: int) -> None:
    cfg = await bot.guild_config(guild_id)
    tz = cfg.get("timezone") or "America/Chicago"
    week_id = await bot.db.ensure_week(guild_id, bot.db.week_start_ts(tz))
    guild = bot.get_guild(guild_id)
    member = await member_from_id(bot, guild, discord_id) if guild else None
    person = await bot.db.get_personnel(guild_id, discord_id)
    rank_name = person["rank_name"] if person else None
    required = quota_required_minutes(member, cfg, rank_name)
    minutes = max(0, duration_seconds // 60)
    loa = await bot.db.active_loa(guild_id, discord_id)
    status = "exempt_loa" if loa else None
    await bot.db.upsert_quota_record(week_id, discord_id, "duty", required, add_completed=minutes, status=status)
    record = await bot.db.get_quota_record(week_id, discord_id, "duty")
    if record and not loa and int(record["completed_minutes"]) >= required:
        await bot.db.upsert_quota_record(week_id, discord_id, "duty", required, status="complete")


class Quota(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    quota = app_commands.Group(name="quota", description="Weekly quota")

    @quota.command(name="view", description="View this week's quota for yourself or another member.")
    async def view(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if not interaction.guild:
            return
        target = member or interaction.user
        if member and member.id != interaction.user.id:
            if await resolve_level(interaction) < PermissionLevel.HR:
                await interaction.response.send_message(embed=error_embed("Restricted"), ephemeral=True)
                return
        cfg = await self.bot.guild_config(interaction.guild.id)
        week = self.bot.db.week_start_ts(cfg.get("timezone") or "America/Chicago")
        week_id = await self.bot.db.ensure_week(interaction.guild.id, week)
        duty = await self.bot.db.get_quota_record(week_id, target.id, "duty")
        loa = await self.bot.db.active_loa(interaction.guild.id, target.id)
        member = target if isinstance(target, discord.Member) else None
        person = await self.bot.db.get_personnel(interaction.guild.id, target.id)
        required = quota_required_minutes(member, cfg, person["rank_name"] if person else None)
        if duty:
            required = int(duty["required_minutes"] or required)
        embed = base_embed(f"Weekly quota  •  {target}")
        duty_min = int(duty["completed_minutes"]) if duty else 0
        add_fields(
            embed,
            [
                ("Duty time", f"{duty_min} / {required} minutes", True),
                ("Duty status", "Exempt (LOA)" if loa else (duty["status"] if duty and duty["status"] else _status(duty_min, required)), True),
            ],
        )
        embed.set_footer(text="Quota resets every Monday 00:00 in the department timezone. Missed quota is reported to HR — not auto-punished.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @quota.command(name="leaderboard", description="This week's quota completion board.")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        cfg = await self.bot.guild_config(interaction.guild.id)
        week = self.bot.db.week_start_ts(cfg.get("timezone") or "America/Chicago")
        week_id = await self.bot.db.ensure_week(interaction.guild.id, week)
        rows = await self.bot.db.list_quota_records(week_id)
        duty_rows = [r for r in rows if r["quota_type"] == "duty"]
        embed = base_embed("Quota leaderboard  •  this week")
        if not duty_rows:
            embed.description = "No quota activity recorded this week yet."
        else:
            embed.description = "\n".join(
                f"{mention_or_id(interaction.guild, r['discord_id'])} — **{r['completed_minutes']}** / {r['required_minutes']} min (`{r['status'] or 'in progress'}`)"
                for r in sorted(duty_rows, key=lambda r: int(r["completed_minutes"]), reverse=True)[:20]
            )
        await interaction.response.send_message(embed=embed)

    @quota.command(name="admin", description="Adjust quota settings or grant a one-week exemption.")
    @has_level(PermissionLevel.HR)
    async def admin(
        self,
        interaction: discord.Interaction,
        low_minutes: int | None = None,
        middle_minutes: int | None = None,
        high_minutes: int | None = None,
        exempt_member: discord.Member | None = None,
    ) -> None:
        if not interaction.guild:
            return
        cfg = await self.bot.guild_config(interaction.guild.id)
        changed = []
        if low_minutes is not None:
            cfg.set_path(["quota", "low_minutes"], low_minutes)
            changed.append(f"LR quota = {low_minutes} min")
        if middle_minutes is not None:
            cfg.set_path(["quota", "middle_minutes"], middle_minutes)
            changed.append(f"MR quota = {middle_minutes} min")
        if high_minutes is not None:
            cfg.set_path(["quota", "high_minutes"], high_minutes)
            changed.append(f"HR quota = {high_minutes} min")
        if changed:
            await self.bot.save_config(interaction.guild.id, cfg)
            await self.bot.db.audit(
                interaction.guild.id, "quota_config", actor_id=interaction.user.id, actor_name=str(interaction.user),
                details="; ".join(changed),
            )
        if exempt_member:
            record = await self.bot.db.get_personnel(interaction.guild.id, exempt_member.id)
            if record:
                await self.bot.db.update_personnel(record["id"], quota_exempt=1)
            week = self.bot.db.week_start_ts(cfg.get("timezone") or "America/Chicago")
            week_id = await self.bot.db.ensure_week(interaction.guild.id, week)
            required = quota_required_minutes(exempt_member, cfg, record["rank_name"] if record else None)
            await self.bot.db.upsert_quota_record(week_id, exempt_member.id, "duty", required, status="exempt_loa")
            changed.append(f"exempted {exempt_member}")
        if not changed:
            embed = base_embed("Quota settings")
            add_fields(
                embed,
                [
                    ("Low Rank", f"{cfg.get('quota', 'low_minutes') or 90} min/week", True),
                    ("Middle Rank", f"{cfg.get('quota', 'middle_minutes') or 75} min/week", True),
                    ("High Rank", f"{cfg.get('quota', 'high_minutes') or 30} min/week", True),
                    ("Timezone", cfg.get("timezone"), True),
                ],
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await interaction.response.send_message(embed=success_embed("Quota updated", "\n".join(changed)), ephemeral=True)


def _status(done: int, required: int) -> str:
    if required <= 0:
        return "n/a"
    pct = done / required * 100
    if pct >= 100:
        return "complete"
    if pct >= 50:
        return "on track"
    return "behind"


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Quota(bot))
