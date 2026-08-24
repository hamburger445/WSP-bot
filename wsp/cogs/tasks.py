"""Background jobs: quota reminders, probation notices, backups, LOA expiry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord.ext import commands, tasks

from wsp.db import now_ts
from wsp.embeds import base_embed, warning_embed
from wsp.utils import mention_or_id

if TYPE_CHECKING:
    from wsp.bot import WSPBot

log = logging.getLogger("wsp.tasks")


class ScheduledTasks(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot
        self.hourly.start()
        self.backup_job.start()

    def cog_unload(self) -> None:
        self.hourly.cancel()
        self.backup_job.cancel()

    @tasks.loop(hours=1)
    async def hourly(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self._probation_notices(guild.id)
                await self._quota_notices(guild.id)
                await self._expire_loa(guild.id)
            except Exception:
                log.exception("Hourly tasks failed for guild %s", guild.id)

    @hourly.before_loop
    async def before_hourly(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(hours=6)
    async def backup_job(self) -> None:
        try:
            await self.bot.db.backup()
        except Exception:
            log.exception("Backup failed")

    @backup_job.before_loop
    async def before_backup(self) -> None:
        await self.bot.wait_until_ready()

    async def _probation_notices(self, guild_id: int) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        cfg = await self.bot.guild_config(guild_id)
        hours = int(cfg.get("probation", "notify_hours_before") or 48)
        threshold = now_ts() + hours * 3600
        rows = await self.bot.db.list_active_probations(guild_id)
        for row in rows:
            if row["notified_ending"]:
                continue
            if int(row["expected_end"]) <= threshold:
                await self.bot.db.update_probation(row["id"], notified_ending=1)
                embed = warning_embed(
                    "Probation ending soon",
                    f"{mention_or_id(guild, row['discord_id'])} reaches expected completion <t:{row['expected_end']}:R>.",
                )
                await self.bot.notify(guild, "probation", embed)
                await self.bot.notify(guild, "notifications", embed)
                await self.bot.notify(guild, "hr_log", embed)

    async def _quota_notices(self, guild_id: int) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        cfg = await self.bot.guild_config(guild_id)
        tz = cfg.get("timezone") or "America/Chicago"
        week_start = self.bot.db.week_start_ts(tz)
        week_id = await self.bot.db.ensure_week(guild_id, week_start)
        next_week = week_start + 7 * 86400
        remaining = next_week - now_ts()
        reminder_hours = int(cfg.get("quota", "reminder_hours_before_reset") or 24)
        approaching = int(cfg.get("quota", "approaching_percent") or 50)
        rows = await self.bot.db.list_quota_records(week_id)
        personnel = {str(p["discord_id"]): p for p in await self.bot.db.list_personnel(guild_id, "active")}
        for row in rows:
            if row["notified"] or row["quota_type"] != "duty":
                continue
            discord_id = int(row["discord_id"])
            if await self.bot.db.active_loa(guild_id, discord_id):
                continue
            person = personnel.get(str(discord_id))
            if person and person["quota_exempt"]:
                continue
            required = int(row["required_minutes"] or 0)
            done = int(row["completed_minutes"] or 0)
            if required <= 0:
                continue
            missed = remaining <= 3600 and done < required
            near_reset = remaining <= reminder_hours * 3600 and done < required
            behind = done * 100 / required < approaching
            if missed:
                await self.bot.db.set_quota_notified(row["id"])
                embed = warning_embed(
                    "Missed quota",
                    f"{mention_or_id(guild, discord_id)} finished the week at **{done}/{required}** minutes. No automatic punishment was applied.",
                )
                await self.bot.notify(guild, "quota", embed)
                await self.bot.notify(guild, "hr_log", embed)
            elif near_reset and behind:
                await self.bot.db.set_quota_notified(row["id"])
                embed = warning_embed(
                    "Quota reminder",
                    f"{mention_or_id(guild, discord_id)} is at **{done}/{required}** minutes with reset <t:{next_week}:R>.",
                )
                await self.bot.notify(guild, "quota", embed)

    async def _expire_loa(self, guild_id: int) -> None:
        rows = await self.bot.db.list_loa(guild_id, "approved")
        now = now_ts()
        guild = self.bot.get_guild(guild_id)
        for row in rows:
            if int(row["end_date"]) < now:
                await self.bot.db.update_loa(row["id"], status="expired")
                personnel = await self.bot.db.get_personnel(guild_id, int(row["discord_id"]))
                if personnel and personnel["status"] == "loa":
                    await self.bot.db.update_personnel(personnel["id"], status="active")
                if guild:
                    await self.bot.notify(
                        guild,
                        "loa",
                        base_embed("LOA ended", f"{mention_or_id(guild, row['discord_id'])} is back on the duty roster."),
                    )


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(ScheduledTasks(bot))
