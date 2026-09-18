"""Weekly quota tracking. Does not auto-punish — notifies HR/Command only."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_NAVY, PermissionLevel
from wsp.db import now_ts
from wsp.embeds import add_fields, base_embed, error_embed, success_embed
from wsp.permissions import has_level, resolve_level
from wsp.utils import member_from_id, mention_or_id, quota_required_minutes, reply_interaction

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
    minutes = duration_seconds // 60
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

    @quota.command(name="view", description="View quota.")
    async def view(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if not interaction.guild:
            return
        target = member or interaction.user
        if member and member.id != interaction.user.id:
            if await resolve_level(interaction) < PermissionLevel.HR:
                await reply_interaction(interaction, embed=error_embed("Restricted"), ephemeral=True)
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
        embed.set_footer(text="Quota resets every Monday.")
        await reply_interaction(interaction, embed=embed, ephemeral=True)

    @quota.command(name="leaderboard", description="Show quota standings.")
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
        await reply_interaction(interaction, embed=embed, ephemeral=False)

    @quota.command(name="admin", description="Change quota settings.")
    @has_level(PermissionLevel.HR)
    @app_commands.describe(
        low_minutes="Low Rank minutes",
        middle_minutes="Middle Rank minutes",
        high_minutes="High Rank minutes",
        exempt_member="Member to exempt",
    )
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
            await reply_interaction(interaction, embed=embed, ephemeral=True)
            return
        await reply_interaction(interaction, embed=success_embed("Quota updated", "\n".join(changed)), ephemeral=True)

    @quota.command(name="report", description="Show who completed quota this week and who did not.")
    @has_level(PermissionLevel.HR)
    async def report(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await reply_interaction(interaction, embed=error_embed("Guild only"), ephemeral=True)
            return
        embed = await build_quota_report(self.bot, interaction.guild)
        await reply_interaction(interaction, embed=embed, ephemeral=True)


async def build_quota_report(bot: WSPBot, guild: discord.Guild) -> discord.Embed:
    cfg = await bot.guild_config(guild.id)
    week = bot.db.week_start_ts(cfg.get("timezone") or "America/Chicago")
    week_id = await bot.db.ensure_week(guild.id, week)
    records = {
        str(row["discord_id"]): row
        for row in await bot.db.list_quota_records(week_id)
        if row["quota_type"] == "duty"
    }
    personnel = list(await bot.db.list_personnel(guild.id, "active"))
    loa_ids: set[str] = set()
    now = now_ts()
    for row in await bot.db.list_loa(guild.id, "approved"):
        if int(row["start_date"]) <= now <= int(row["end_date"]):
            loa_ids.add(str(row["discord_id"]))
    seen: set[str] = set()
    met: list[str] = []
    missed: list[str] = []
    exempt: list[str] = []
    for person in personnel:
        discord_id = str(person["discord_id"])
        seen.add(discord_id)
        _classify_quota_row(
            guild,
            discord_id,
            records.get(discord_id),
            person,
            loa_ids,
            cfg,
            met,
            missed,
            exempt,
        )
    for discord_id, row in records.items():
        if discord_id in seen:
            continue
        _classify_quota_row(guild, discord_id, row, None, loa_ids, cfg, met, missed, exempt)
    embed = base_embed("Quota report  •  this week", color=COLOR_NAVY)
    embed.add_field(name=f"Completed ({len(met)})", value=_clip_lines(met), inline=False)
    embed.add_field(name=f"Not completed ({len(missed)})", value=_clip_lines(missed), inline=False)
    embed.add_field(name=f"Exempt ({len(exempt)})", value=_clip_lines(exempt), inline=False)
    embed.set_footer(text="Quota resets every Monday.")
    return embed


def _classify_quota_row(
    guild: discord.Guild,
    discord_id: str,
    row,
    person,
    loa_ids: set[str],
    cfg,
    met: list[str],
    missed: list[str],
    exempt: list[str],
) -> None:
    member = guild.get_member(int(discord_id)) if discord_id.isdigit() else None
    rank_name = person["rank_name"] if person else None
    required = quota_required_minutes(member, cfg, rank_name)
    done = int(row["completed_minutes"]) if row else 0
    if row and row["required_minutes"] not in (None, ""):
        required = int(row["required_minutes"] or required)
    label = f"{mention_or_id(guild, discord_id)} — {done}/{required} min"
    status = str(row["status"] or "").lower() if row else ""
    if discord_id in loa_ids or (person and person["quota_exempt"]) or status in {"exempt_loa", "exempt"}:
        exempt.append(label)
        return
    if required > 0 and done >= required:
        met.append(label)
        return
    missed.append(label)


def _clip_lines(lines: list[str]) -> str:
    if not lines:
        return "None"
    kept: list[str] = []
    used = 0
    for line in lines:
        extra = len(line) + 1
        if used + extra > 980:
            kept.append(f"… +{len(lines) - len(kept)} more")
            break
        kept.append(line)
        used += extra
    return "\n".join(kept)


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
