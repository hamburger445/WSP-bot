"""Text commands using the ? prefix. Same names as slash commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from wsp.constants import PermissionLevel
from wsp.cogs.dashboard import overview_embed, DashboardView
from wsp.cogs.help import HelpView, _catalog_embed
from wsp.cogs.quota import Quota
from wsp.embeds import add_fields, base_embed, error_embed, format_duration, success_embed, ts, ts_rel
from wsp.ops import change_rank, fire_member
from wsp.permissions import prefix_has_level, prefix_is_owner, resolve_user_level
from wsp.utils import current_shift_seconds, hms_to_seconds, member_can_start_shift, mention_or_id, quota_required_minutes, sync_duty_role
from wsp.views.shifts import (
    ShiftActionView,
    ShiftMenuView,
    begin_shift,
    build_duty_board,
    build_leaderboard,
    build_shift_controls,
    complete_shift,
)

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class Prefix(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        return ctx.guild is not None

    @commands.command(name="help")
    async def help_cmd(self, ctx: commands.Context) -> None:
        staff = await resolve_user_level(self.bot, ctx.guild.id, ctx.author) >= PermissionLevel.SUPERVISOR  # type: ignore[union-attr]
        owner = await resolve_user_level(self.bot, ctx.guild.id, ctx.author) >= PermissionLevel.OWNER  # type: ignore[union-attr]
        await ctx.send(embed=_catalog_embed("members", staff, owner), view=HelpView(staff, owner))

    @commands.command(name="ping")
    async def ping_cmd(self, ctx: commands.Context) -> None:
        ms = round(self.bot.latency * 1000)
        await ctx.send(f"Pong — **{ms} ms**")

    @commands.command(name="say")
    @prefix_has_level(PermissionLevel.HR)
    async def say_cmd(self, ctx: commands.Context, *, message: str) -> None:
        if not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            return
        text = message.replace("@everyone", "everyone").replace("@here", "here")[:2000]
        try:
            await ctx.channel.send(text)
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
        except discord.HTTPException:
            await ctx.send(embed=error_embed("Could not send"))

    @commands.command(name="dashboard")
    @prefix_has_level(PermissionLevel.HR)
    async def dashboard_cmd(self, ctx: commands.Context) -> None:
        embed = await overview_embed(self.bot, ctx.guild)  # type: ignore[arg-type]
        await ctx.send(embed=embed, view=DashboardView())

    @commands.command(name="promote")
    @prefix_has_level(PermissionLevel.HR)
    async def promote_cmd(self, ctx: commands.Context, member: discord.Member, *, rest: str) -> None:
        rank, reason = await _split_rank_reason(self.bot, ctx.guild.id, rest)  # type: ignore[union-attr]
        if not rank or not reason:
            await ctx.send(embed=error_embed("Command failed", "That command could not be run."))
            return
        await self._rank(ctx, member, rank, reason, "promotion")

    @commands.command(name="demote")
    @prefix_has_level(PermissionLevel.HR)
    async def demote_cmd(self, ctx: commands.Context, member: discord.Member, *, rest: str) -> None:
        rank, reason = await _split_rank_reason(self.bot, ctx.guild.id, rest)  # type: ignore[union-attr]
        if not rank or not reason:
            await ctx.send(embed=error_embed("Command failed", "That command could not be run."))
            return
        await self._rank(ctx, member, rank, reason, "demotion")

    @commands.command(name="fire")
    @prefix_has_level(PermissionLevel.HR)
    async def fire_cmd(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "",
    ) -> None:
        message = await fire_member(self.bot, ctx.guild, member, reason, ctx.author)  # type: ignore[arg-type]
        if message in {"Restricted", "Could not update roles."}:
            await ctx.send(embed=error_embed(message))
            return
        await ctx.send(embed=success_embed("Member fired", message))

    async def _rank(
        self,
        ctx: commands.Context,
        member: discord.Member,
        rank: str,
        reason: str,
        action: str,
    ) -> None:
        error = await change_rank(self.bot, ctx.guild, member, rank, reason, ctx.author, action)  # type: ignore[arg-type]
        if error in {"Restricted", "Could not update roles."}:
            await ctx.send(embed=error_embed(error))
            return
        if error:
            await ctx.send(embed=error_embed("Invalid rank change", error))
            return
        title = "Promotion recorded" if action == "promotion" else "Demotion recorded"
        await ctx.send(embed=success_embed(title, f"{member.mention} is now **{rank}**."))

    @commands.group(name="shift", invoke_without_command=True)
    async def shift_grp(self, ctx: commands.Context) -> None:
        await ctx.send(embed=base_embed("Shift", "Start, pause, resume, or end a shift."))

    @shift_grp.command(name="menu")
    async def shift_menu(self, ctx: commands.Context) -> None:
        if not isinstance(ctx.author, discord.Member) or ctx.guild is None:
            return
        row = await self.bot.db.active_shift(ctx.guild.id, ctx.author.id)
        status = row["status"] if row else None
        cfg = await self.bot.guild_config(ctx.guild.id)
        await ctx.send(
            embed=await build_shift_controls(status),
            view=ShiftActionView(status, can_start=member_can_start_shift(ctx.author, cfg)),
        )

    @shift_grp.command(name="data")
    async def shift_data(self, ctx: commands.Context) -> None:
        embed = await build_duty_board(self.bot, ctx.guild)  # type: ignore[arg-type]
        await ctx.send(embed=embed, view=ShiftMenuView())

    @shift_grp.command(name="status")
    async def shift_status(self, ctx: commands.Context) -> None:
        rows = await self.bot.db.list_active_shifts(ctx.guild.id)  # type: ignore[union-attr]
        embed = base_embed("Active shifts")
        if not rows:
            embed.description = "No troopers are currently on duty."
        else:
            embed.description = "\n".join(
                f"{mention_or_id(ctx.guild, row['discord_id'])} **{row['rank_name'] or ''}** "
                f"{row['status']} • {format_duration(current_shift_seconds(row))} • started {ts_rel(row['start_time'])}"
                for row in rows
            )[:4000]
        await ctx.send(embed=embed)

    @shift_grp.command(name="leaderboard")
    async def shift_leaderboard(self, ctx: commands.Context) -> None:
        await ctx.send(embed=await build_leaderboard(self.bot, ctx.guild))  # type: ignore[arg-type]

    @shift_grp.command(name="history")
    async def shift_history(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        target = member or ctx.author
        if member and member.id != ctx.author.id:
            level = await resolve_user_level(self.bot, ctx.guild.id, ctx.author)  # type: ignore[union-attr]
            if level < PermissionLevel.SUPERVISOR:
                await ctx.send(embed=error_embed("Restricted"))
                return
        rows = await self.bot.db.list_shifts(ctx.guild.id, target.id, limit=12)  # type: ignore[union-attr]
        totals = await self.bot.db.shift_totals(ctx.guild.id, target.id)  # type: ignore[union-attr]
        embed = base_embed(f"Shift history  •  {target}")
        if totals:
            add_fields(embed, [("All-time", format_duration(totals["total_seconds"]), True), ("Shifts", str(totals["shift_count"]), True)])
        embed.description = "\n".join(
            f"`#{r['id']}` {r['status']} {r['callsign'] or ''} {format_duration(r['duration_seconds'] or current_shift_seconds(r))} {ts(r['start_time'])}"
            for r in rows
        ) or "No records."
        await ctx.send(embed=embed)

    @shift_grp.group(name="admin", invoke_without_command=True)
    @prefix_has_level(PermissionLevel.SUPERVISOR)
    async def shift_admin(self, ctx: commands.Context) -> None:
        await ctx.send(embed=base_embed("Shift admin", "Start, end, edit, or delete a member's shift."))

    @shift_admin.command(name="start")
    @prefix_has_level(PermissionLevel.SUPERVISOR)
    async def shift_admin_start(self, ctx: commands.Context, member: discord.Member) -> None:
        result = await begin_shift(self.bot, ctx.guild, member, ctx.author)  # type: ignore[arg-type]
        await _prefix_shift_result(ctx, result)

    @shift_admin.command(name="end")
    @prefix_has_level(PermissionLevel.SUPERVISOR)
    async def shift_admin_end(self, ctx: commands.Context, member: discord.Member) -> None:
        result = await complete_shift(self.bot, ctx.guild, member, ctx.author)  # type: ignore[arg-type]
        await _prefix_shift_result(ctx, result)

    @shift_admin.command(name="edit")
    @prefix_has_level(PermissionLevel.SUPERVISOR)
    async def shift_admin_edit(
        self,
        ctx: commands.Context,
        member: discord.Member,
        shift_id: int,
        hours: int = 0,
        minutes: int = 0,
        seconds: int = 0,
    ) -> None:
        duration = hms_to_seconds(hours, minutes, seconds)
        if duration <= 0:
            await ctx.send(embed=error_embed("Invalid duration", "Enter hours, minutes, and/or seconds greater than zero."))
            return
        row = await self.bot.db.get_shift(shift_id)
        if row is None or str(row["guild_id"]) != str(ctx.guild.id) or str(row["discord_id"]) != str(member.id):  # type: ignore[union-attr]
            await ctx.send(embed=error_embed("Not found"))
            return
        was_open = row["status"] in {"active", "paused"}
        await self.bot.db.update_shift(
            shift_id,
            status="completed",
            end_time=int(row["start_time"]) + duration,
            duration_seconds=duration,
            pause_started=None,
        )
        if was_open:
            cfg = await self.bot.guild_config(ctx.guild.id)  # type: ignore[union-attr]
            await sync_duty_role(member, cfg, False)
            from wsp.cogs.quota import apply_shift_quota

            await apply_shift_quota(self.bot, ctx.guild.id, member.id, duration)  # type: ignore[union-attr]
        await ctx.send(embed=success_embed("Shift updated", f"{member.mention} shift `#{shift_id}` is now **{format_duration(duration)}**."))
        await self.bot.notify(
            ctx.guild,
            "shift_log",
            base_embed("Shift edited", f"{ctx.author.mention} set {member.mention} shift `#{shift_id}` to **{format_duration(duration)}**."),
        )

    @shift_admin.command(name="delete")
    @prefix_has_level(PermissionLevel.SUPERVISOR)
    async def shift_admin_delete(self, ctx: commands.Context, member: discord.Member, shift_id: int) -> None:
        row = await self.bot.db.get_shift(shift_id)
        if row is None or str(row["guild_id"]) != str(ctx.guild.id) or str(row["discord_id"]) != str(member.id):  # type: ignore[union-attr]
            await ctx.send(embed=error_embed("Not found"))
            return
        if row["status"] in {"active", "paused"}:
            cfg = await self.bot.guild_config(ctx.guild.id)  # type: ignore[union-attr]
            await sync_duty_role(member, cfg, False)
        await self.bot.db.delete_shift(shift_id)
        await ctx.send(embed=success_embed("Shift deleted", f"Removed shift `#{shift_id}` for {member.mention}."))
        await self.bot.notify(
            ctx.guild,
            "shift_log",
            base_embed("Shift deleted", f"{ctx.author.mention} deleted {member.mention} shift `#{shift_id}`."),
        )

    @commands.group(name="quota", invoke_without_command=True)
    async def quota_grp(self, ctx: commands.Context) -> None:
        await ctx.send(embed=base_embed("Quota", "View quota or standings."))

    @quota_grp.command(name="view")
    async def quota_view(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        cog: Quota | None = self.bot.get_cog("Quota")  # type: ignore[assignment]
        if cog is None:
            return
        target = member or ctx.author
        if member and member.id != ctx.author.id:
            if await resolve_user_level(self.bot, ctx.guild.id, ctx.author) < PermissionLevel.HR:  # type: ignore[union-attr]
                await ctx.send(embed=error_embed("Restricted"))
                return
        cfg = await self.bot.guild_config(ctx.guild.id)  # type: ignore[union-attr]
        week = self.bot.db.week_start_ts(cfg.get("timezone") or "America/Chicago")
        week_id = await self.bot.db.ensure_week(ctx.guild.id, week)  # type: ignore[union-attr]
        duty = await self.bot.db.get_quota_record(week_id, target.id, "duty")
        loa = await self.bot.db.active_loa(ctx.guild.id, target.id)  # type: ignore[union-attr]
        person = await self.bot.db.get_personnel(ctx.guild.id, target.id)  # type: ignore[union-attr]
        member_obj = target if isinstance(target, discord.Member) else None
        required = quota_required_minutes(member_obj, cfg, person["rank_name"] if person else None)
        if duty:
            required = int(duty["required_minutes"] or required)
        duty_min = int(duty["completed_minutes"]) if duty else 0
        embed = base_embed(f"Weekly quota  •  {target}")
        add_fields(
            embed,
            [
                ("Duty time", f"{duty_min} / {required} minutes", True),
                ("Duty status", "Exempt (LOA)" if loa else (duty["status"] if duty and duty["status"] else "in progress"), True),
            ],
        )
        await ctx.send(embed=embed)

    @quota_grp.command(name="leaderboard")
    async def quota_leaderboard(self, ctx: commands.Context) -> None:
        cfg = await self.bot.guild_config(ctx.guild.id)  # type: ignore[union-attr]
        week = self.bot.db.week_start_ts(cfg.get("timezone") or "America/Chicago")
        week_id = await self.bot.db.ensure_week(ctx.guild.id, week)  # type: ignore[union-attr]
        rows = [r for r in await self.bot.db.list_quota_records(week_id) if r["quota_type"] == "duty"]
        embed = base_embed("Quota leaderboard  •  this week")
        if not rows:
            embed.description = "No quota activity recorded this week yet."
        else:
            embed.description = "\n".join(
                f"{mention_or_id(ctx.guild, r['discord_id'])} — **{r['completed_minutes']}** / {r['required_minutes']} min (`{r['status'] or 'in progress'}`)"
                for r in sorted(rows, key=lambda r: int(r["completed_minutes"]), reverse=True)[:20]
            )
        await ctx.send(embed=embed)

    @quota_grp.command(name="admin")
    @prefix_has_level(PermissionLevel.HR)
    async def quota_admin(self, ctx: commands.Context) -> None:
        cfg = await self.bot.guild_config(ctx.guild.id)  # type: ignore[union-attr]
        embed = base_embed("Quota settings")
        add_fields(
            embed,
            [
                ("Low Rank", f"{cfg.get('quota', 'low_minutes') or 90} min/week", True),
                ("Middle Rank", f"{cfg.get('quota', 'middle_minutes') or 75} min/week", True),
                ("High Rank", f"{cfg.get('quota', 'high_minutes') or 30} min/week", True),
            ],
        )
        await ctx.send(embed=embed)

    @commands.group(name="loa", invoke_without_command=True)
    async def loa_grp(self, ctx: commands.Context) -> None:
        await ctx.send(embed=base_embed("LOA", "Request leave or view leave."))

    @loa_grp.command(name="menu")
    async def loa_menu(self, ctx: commands.Context) -> None:
        from wsp.cogs.loa import LOAMenuView

        embed = base_embed("Leave of Absence", "Submit a leave request or view your requests.")
        await ctx.send(embed=embed, view=LOAMenuView())

    @loa_grp.command(name="request")
    async def loa_request(self, ctx: commands.Context, start_date: str, end_date: str, *, reason: str) -> None:
        from wsp.cogs.loa import create_loa_request

        if not isinstance(ctx.author, discord.Member) or ctx.guild is None:
            return
        _ok, embed = await create_loa_request(self.bot, ctx.guild, ctx.author, start_date, end_date, reason, None)
        await ctx.send(embed=embed)


    @loa_grp.command(name="active")
    @prefix_has_level(PermissionLevel.HR)
    async def loa_active(self, ctx: commands.Context) -> None:
        from wsp.db import now_ts

        rows = await self.bot.db.list_loa(ctx.guild.id, "approved")  # type: ignore[union-attr]
        now = now_ts()
        current = [r for r in rows if int(r["start_date"]) <= now <= int(r["end_date"])]
        embed = base_embed("Active LOA")
        embed.description = "\n".join(
            f"{mention_or_id(ctx.guild, r['discord_id'])} • {ts(r['start_date'])} → {ts(r['end_date'])} — {r['reason']}"
            for r in current
        ) or "No members are currently on approved leave."
        await ctx.send(embed=embed)

    @loa_grp.command(name="admin")
    @prefix_has_level(PermissionLevel.HR)
    async def loa_admin(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        from wsp.cogs.loa import LOAAdminView, build_loa_admin_embed

        embed = await build_loa_admin_embed(self.bot, ctx.guild, member)  # type: ignore[arg-type]
        await ctx.send(embed=embed, view=LOAAdminView(member))

    @commands.command(name="setupserver")
    @prefix_is_owner()
    async def setupserver_cmd(self, ctx: commands.Context) -> None:
        await ctx.send(embed=base_embed("Setup", "Set up the server."))

    @commands.command(name="verifysetup")
    @prefix_is_owner()
    async def verifysetup_cmd(self, ctx: commands.Context) -> None:
        await ctx.send(embed=base_embed("Verify setup", "Check setup."))

    @commands.command(name="config")
    @prefix_is_owner()
    async def config_cmd(self, ctx: commands.Context) -> None:
        await ctx.send(embed=base_embed("Config", "View or change settings."))

    @commands.command(name="sync")
    @prefix_is_owner()
    async def sync_cmd(self, ctx: commands.Context) -> None:
        if ctx.guild:
            self.bot.tree.copy_global_to(guild=ctx.guild)
            synced = await self.bot.tree.sync(guild=ctx.guild)
        else:
            synced = await self.bot.tree.sync()
        await ctx.send(embed=success_embed("Commands synced", f"{len(synced)} commands published."))


async def _split_rank_reason(bot: WSPBot, guild_id: int, rest: str) -> tuple[str | None, str]:
    text = rest.strip()
    if not text:
        return None, ""
    ranks = await bot.db.list_ranks(guild_id)
    names = sorted((str(row["name"]) for row in ranks), key=len, reverse=True)
    lower = text.lower()
    for name in names:
        key = name.lower()
        if lower == key or lower.startswith(f"{key} "):
            return name, text[len(name) :].strip()
    parts = text.split(None, 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


async def _prefix_shift_result(ctx: commands.Context, result) -> None:
    bot: WSPBot = ctx.bot  # type: ignore[assignment]
    if result.error:
        await ctx.send(embed=error_embed("Shift admin", result.error))
        return
    if ctx.guild and result.log:
        await bot.notify(ctx.guild, "shift_log", result.log)
    await ctx.send(embed=result.notice)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Prefix(bot))
