"""Mention the bot to run department actions through Gemini."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import discord
from discord.ext import commands

from wsp.constants import LEVEL_LABELS, PermissionLevel
from wsp.db import now_ts
from wsp.embeds import base_embed, format_duration, ts
from wsp.permissions import resolve_user_level
from wsp.utils import apply_role_changes, fetch_live_member, member_role_ids, parse_compact_duration, quota_required_minutes, sync_duty_role

if TYPE_CHECKING:
    from wsp.bot import WSPBot

log = logging.getLogger("wsp.assistant")

_MODELS = ("gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-flash-latest")


def _str_param(description: str) -> dict[str, str]:
    return {"type": "STRING", "description": description}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "OBJECT", "properties": properties}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "parameters": schema}


_TOOLS = [
    _tool("start_shift", "Start a duty shift. Use the speaker when they say my or me.", {"member_id": _str_param("Discord user id. Omit for the speaker.")}),
    _tool("pause_shift", "Pause a duty shift.", {"member_id": _str_param("Discord user id. Omit for the speaker.")}),
    _tool("resume_shift", "Resume a paused duty shift.", {"member_id": _str_param("Discord user id. Omit for the speaker.")}),
    _tool("end_shift", "End a duty shift.", {"member_id": _str_param("Discord user id. Omit for the speaker.")}),
    _tool("shift_status", "Say whether someone is on duty.", {"member_id": _str_param("Discord user id. Omit for the speaker.")}),
    _tool(
        "request_loa",
        "Submit a leave request for the speaker.",
        {
            "start_date": _str_param("Start date such as 11/01/2026"),
            "end_date": _str_param("End date"),
            "reason": _str_param("Reason"),
        },
        ["start_date", "end_date", "reason"],
    ),
    _tool(
        "promote",
        "Promote a member. HR only.",
        {"member_id": _str_param("Discord user id"), "rank": _str_param("Rank name"), "reason": _str_param("Reason")},
        ["member_id", "rank", "reason"],
    ),
    _tool(
        "demote",
        "Demote a member. HR only.",
        {"member_id": _str_param("Discord user id"), "rank": _str_param("Rank name"), "reason": _str_param("Reason")},
        ["member_id", "rank", "reason"],
    ),
    _tool(
        "fire",
        "Fire a member. HR only.",
        {"member_id": _str_param("Discord user id"), "reason": _str_param("Reason")},
        ["member_id"],
    ),
    _tool(
        "trial_start",
        "Place a member on trial. HR only.",
        {"member_id": _str_param("Discord user id"), "days": {"type": "INTEGER", "description": "1 to 30"}},
        ["member_id", "days"],
    ),
    _tool("trial_end", "End a member's trial early. HR only.", {"member_id": _str_param("Discord user id")}, ["member_id"]),
    _tool(
        "give_role",
        "Give a Discord role. Do this instead of describing the role. HR only.",
        {"member_id": _str_param("Discord user id"), "role_name": _str_param("Exact role name")},
        ["member_id", "role_name"],
    ),
    _tool(
        "remove_role",
        "Remove a Discord role. HR only, same as -unrole.",
        {"member_id": _str_param("Discord user id"), "role_name": _str_param("Exact role name")},
        ["member_id", "role_name"],
    ),
    _tool(
        "create_role",
        "Create a new Discord role. HR only. Do not grant administrator.",
        {"name": _str_param("Role name"), "color": _str_param("Optional hex color such as cdd7eb")},
        ["name"],
    ),
    _tool(
        "delete_role",
        "Delete a Discord role by name. HR only. Never delete administrator roles.",
        {"role_name": _str_param("Exact role name")},
        ["role_name"],
    ),
    _tool(
        "delete_shift",
        "Delete one shift. Supervisor or above, same as shift admin delete. Omit shift_id to delete that member's latest shift.",
        {"member_id": _str_param("Discord user id. Omit for the speaker."), "shift_id": _str_param("Shift id. Omit for the latest shift.")},
    ),
    _tool(
        "set_shift_duration",
        "Set one finished shift to an exact duration. Supervisor or above, same as shift admin edit.",
        {
            "member_id": _str_param("Discord user id"),
            "shift_id": _str_param("Shift id"),
            "duration": _str_param("Duration such as 1h30m or 45m"),
        },
        ["member_id", "shift_id", "duration"],
    ),
    _tool(
        "add_shift",
        "Log a new finished shift or add time to the latest shift. HR only, same as -add.",
        {
            "member_id": _str_param("Discord user id"),
            "mode": _str_param("new or recent"),
            "duration": _str_param("Duration such as 3m, 1h, or 30s"),
        },
        ["member_id", "mode", "duration"],
    ),
    _tool(
        "remove_shift_time",
        "Remove time from the latest shift. HR only, same as -remove.",
        {"member_id": _str_param("Discord user id"), "duration": _str_param("Duration such as 3m")},
        ["member_id", "duration"],
    ),
    _tool("reset_shifts", "Delete every shift record and clear duty quota. HR only, same as shift reset.", {}),
    _tool(
        "fastpass",
        "Fast-pass a member to a rank. HR only.",
        {
            "member_id": _str_param("Discord user id"),
            "rank": _str_param("Rank name"),
            "needs_supervision": {"type": "BOOLEAN", "description": "Needs supervision"},
            "needs_training": {"type": "BOOLEAN", "description": "Needs training"},
        },
        ["member_id", "rank", "needs_supervision", "needs_training"],
    ),
    _tool(
        "start_loa",
        "Start approved leave for a member. HR only.",
        {
            "member_id": _str_param("Discord user id"),
            "start_date": _str_param("Start date"),
            "end_date": _str_param("End date"),
            "reason": _str_param("Reason"),
        },
        ["member_id", "start_date", "end_date"],
    ),
    _tool("end_loa", "End a member's current leave. HR only.", {"member_id": _str_param("Discord user id")}, ["member_id"]),
    _tool("quota_view", "View weekly quota. Anyone can view their own. Viewing someone else is HR only.", {"member_id": _str_param("Discord user id. Omit for the speaker.")}),
    _tool("quota_report", "Show who completed quota this week. HR only.", {}),
    _tool(
        "say",
        "Send a message in a channel. HR only, same as /say.",
        {"message": _str_param("Message text"), "channel_id": _str_param("Optional channel id. Omit for the current channel.")},
        ["message"],
    ),
]


def _find_role(guild: discord.Guild, role_name: str) -> discord.Role | None:
    wanted = (role_name or "").strip().casefold()
    if not wanted:
        return None
    roles = [role for role in guild.roles if role.id != guild.id and not role.managed]
    for role in roles:
        if role.name.casefold() == wanted:
            return role
    matches = [role for role in roles if wanted in role.name.casefold()]
    if len(matches) == 1:
        return matches[0]
    return None


async def set_member_role(guild: discord.Guild, member: discord.Member, role_name: str, *, give: bool) -> str:
    role = _find_role(guild, role_name)
    if role is None:
        return "Not found"
    live = await fetch_live_member(guild, member.id)
    if live is not None:
        member = live
    if give and role.id in member_role_ids(member):
        return f"{member.mention} already has {role.mention}."
    if not give and role.id not in member_role_ids(member):
        return f"{member.mention} does not have {role.mention}."
    added, removed = await apply_role_changes(
        member,
        add=[role] if give else [],
        remove=[] if give else [role],
        reason="WSP role update",
    )
    if give and added == 0:
        return "Could not update roles."
    if not give and removed == 0:
        return "Could not update roles."
    if give:
        return f"Gave {member.mention} {role.mention}."
    return f"Removed {role.mention} from {member.mention}."


def _generate(api_key: str, body: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(body).encode()
    last_error: Exception | None = None
    for model in _MODELS:
        request = Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=payload,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urlopen(request, timeout=25) as response:
                return json.loads(response.read().decode())
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:240]
            last_error = RuntimeError(f"{model} HTTP {exc.code}: {detail}")
            if exc.code in {404, 400}:
                log.warning("Gemini model %s was rejected: HTTP %s", model, exc.code)
                continue
            raise last_error from exc
        except URLError as exc:
            raise RuntimeError(str(exc.reason)) from exc
    raise RuntimeError(str(last_error) if last_error else "Gemini request failed")


class Assistant(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot
        self._history: dict[tuple[int, int], list[dict[str, Any]]] = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or self.bot.user is None:
            return
        if self.bot.user not in message.mentions and not message.content.startswith(f"<@{self.bot.user.id}>"):
            return
        text = _strip_mention(message.content, self.bot.user.id).strip()
        if not text or text[0] in {"?", "-"}:
            return
        first = text.split(None, 1)[0]
        if self.bot.get_command(first):
            return
        if not isinstance(message.author, discord.Member):
            return
        if not self.bot.settings.gemini_api_key:
            await message.reply("The assistant is not configured.")
            return
        async with message.channel.typing():
            reply = await self._run(message, text)
        if reply:
            await message.reply(reply[:2000])

    async def _run(self, message: discord.Message, text: str) -> str:
        assert isinstance(message.author, discord.Member)
        assert message.guild is not None
        mentions = ", ".join(f"{user} ({user.id})" for user in message.mentions if user.id != self.bot.user.id) or "none"
        moment = now_ts()
        prompt = (
            f"Speaker: {message.author} ({message.author.id})\n"
            f"Speaker permission: {LEVEL_LABELS.get(await resolve_user_level(self.bot, message.guild.id, message.author), 'Trooper')}\n"
            f"Mentioned users: {mentions}\n"
            f"Current unix time: {moment}\n"
            f"Discord timestamp to reuse when asked: {ts(moment)}\n"
            f"Request: {text}"
        )
        prior = list(self._history.get((message.guild.id, message.author.id), []))
        contents: list[dict[str, Any]] = [*prior, {"role": "user", "parts": [{"text": prompt}]}]
        system = (
            "You are the Wisconsin State Patrol Discord bot. When someone is just talking, have a normal conversation. "
            "Be brief, natural, and answer the question. "
            "When they ask you to do something the bot can do, call the matching tool and then say what you did. "
            "That includes creating and deleting roles, deleting or editing shifts, promotions, leave, quota, and sending a message. "
            "The tools enforce the same rank limits as the slash commands. If a tool returns Restricted, say they cannot do that. "
            "Do not tell them which command to run. Giving, creating, or removing a role means calling the tool, not describing the role. "
            "If they ask for a timestamp, include the Discord timestamp from the prompt or tool result."
        )
        used_shift_start = False
        try:
            for _ in range(4):
                data = await asyncio.to_thread(
                    _generate,
                    self.bot.settings.gemini_api_key,
                    {
                        "systemInstruction": {"parts": [{"text": system}]},
                        "contents": contents,
                        "tools": [{"functionDeclarations": _TOOLS}],
                    },
                )
                parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                calls = [part["functionCall"] for part in parts if part.get("functionCall")]
                text_bits = [part.get("text", "") for part in parts if part.get("text")]
                if not calls:
                    reply = "\n".join(bit.strip() for bit in text_bits if bit.strip()) or "Done."
                    if "timestamp" in text.lower() and "<t:" not in reply:
                        reply = f"{reply}\n{ts(moment)}"
                    if used_shift_start and "<t:" not in reply:
                        reply = f"{reply}\n{ts(moment)}"
                    self._remember(message.guild.id, message.author.id, text, reply)
                    return reply
                contents.append({"role": "model", "parts": parts})
                responses = []
                for call in calls:
                    name = str(call.get("name") or "")
                    args = call.get("args") or {}
                    if name == "start_shift":
                        used_shift_start = True
                    result = await self._execute(message.author, message.guild, message.channel, name, args)
                    responses.append({"functionResponse": {"name": name, "response": {"result": result}}})
                contents.append({"role": "user", "parts": responses})
        except Exception:
            log.exception("Assistant request failed")
            return "Command failed"
        return "Command failed"

    def _remember(self, guild_id: int, user_id: int, user_text: str, reply: str) -> None:
        turns = self._history.setdefault((guild_id, user_id), [])
        turns.append({"role": "user", "parts": [{"text": user_text[:1500]}]})
        turns.append({"role": "model", "parts": [{"text": reply[:1500]}]})
        if len(turns) > 12:
            del turns[:-12]

    async def _execute(
        self,
        actor: discord.Member,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        name: str,
        args: dict[str, Any],
    ) -> str:
        try:
            member = await self._target(guild, actor, args.get("member_id"))
        except ValueError:
            return "Not found"
        if member is None:
            return "Not found"
        level = await resolve_user_level(self.bot, guild.id, actor)
        own = member.id == actor.id
        if name in {
            "promote", "demote", "fire", "trial_start", "trial_end", "give_role", "remove_role",
            "create_role", "delete_role", "add_shift", "remove_shift_time", "reset_shifts",
            "fastpass", "start_loa", "end_loa", "quota_report", "say",
        } and level < PermissionLevel.HR:
            return "Restricted"
        if name in {"delete_shift", "set_shift_duration"} and level < PermissionLevel.SUPERVISOR:
            return "Restricted"
        if name in {"start_shift", "pause_shift", "resume_shift", "end_shift"} and not own and level < PermissionLevel.SUPERVISOR:
            return "Restricted"
        if name == "quota_view" and not own and level < PermissionLevel.HR:
            return "Restricted"
        if name == "create_role":
            return await self._create_role(guild, actor, args)
        if name == "delete_role":
            return await self._delete_role(guild, str(args.get("role_name") or ""))
        if name == "delete_shift":
            return await self._delete_shift(guild, actor, member, args.get("shift_id"))
        if name == "set_shift_duration":
            return await self._set_shift_duration(guild, actor, member, args)
        if name == "add_shift":
            return await self._add_shift(guild, actor, member, args)
        if name == "remove_shift_time":
            return await self._remove_shift_time(guild, actor, member, str(args.get("duration") or ""))
        if name == "reset_shifts":
            from wsp.ops import reset_shift_data

            deleted = await reset_shift_data(self.bot, guild, actor)
            return f"Reset shift data. Deleted {deleted} shift records."
        if name == "fastpass":
            from wsp.ops import apply_fastpass

            error = await apply_fastpass(
                self.bot,
                guild,
                member,
                str(args.get("rank") or ""),
                bool(args.get("needs_supervision")),
                bool(args.get("needs_training")),
                actor,
            )
            return error or f"{member.display_name} was fast-passed to {args.get('rank')}."
        if name == "start_loa":
            from wsp.cogs.loa import start_member_loa

            embed = await start_member_loa(
                self.bot,
                guild,
                member,
                str(args.get("start_date") or ""),
                str(args.get("end_date") or ""),
                str(args.get("reason") or ""),
                actor,
            )
            return embed.title or "Leave started"
        if name == "end_loa":
            from wsp.cogs.loa import end_member_loa

            embed = await end_member_loa(self.bot, guild, member, actor)
            return embed.title or "Leave ended"
        if name == "quota_view":
            return await self._quota_view(guild, member)
        if name == "quota_report":
            from wsp.cogs.quota import build_quota_report

            embed = await build_quota_report(self.bot, guild)
            return (embed.description or embed.title or "Quota report")[:1500]
        if name == "say":
            return await self._say(guild, channel, args)
        from wsp.views.shifts import begin_shift, complete_shift, pause_shift, resume_shift

        if name == "start_shift":
            result = await begin_shift(self.bot, guild, member, actor, require_certified=own)
            if result.log:
                await self.bot.notify(guild, "shift_log", result.log)
            if result.error:
                return result.error
            return f"{member.display_name} started a shift at {ts(now_ts())}."
        if name == "pause_shift":
            result = await pause_shift(self.bot, guild, member, actor)
            if result.log:
                await self.bot.notify(guild, "shift_log", result.log)
            return result.error or f"Paused {member.display_name}'s shift."
        if name == "resume_shift":
            result = await resume_shift(self.bot, guild, member, actor)
            if result.log:
                await self.bot.notify(guild, "shift_log", result.log)
            return result.error or f"Resumed {member.display_name}'s shift."
        if name == "end_shift":
            result = await complete_shift(self.bot, guild, member, actor)
            if result.log:
                await self.bot.notify(guild, "shift_log", result.log)
            if result.error:
                return result.error
            return f"Ended {member.display_name}'s shift at {ts(now_ts())}."
        if name == "shift_status":
            row = await self.bot.db.active_shift(guild.id, member.id)
            if row is None:
                return f"{member.display_name} is off shift."
            return f"{member.display_name} is {row['status']} since {ts(int(row['start_time']))}."
        if name == "request_loa":
            from wsp.cogs.loa import create_loa_request

            if not own:
                return "Restricted"
            _ok, embed = await create_loa_request(
                self.bot,
                guild,
                actor,
                str(args.get("start_date") or ""),
                str(args.get("end_date") or ""),
                str(args.get("reason") or ""),
                None,
            )
            return embed.title or "Leave request submitted"
        if name in {"promote", "demote"}:
            from wsp.ops import change_rank

            error = await change_rank(
                self.bot,
                guild,
                member,
                str(args.get("rank") or ""),
                str(args.get("reason") or "Requested in chat"),
                actor,
                "promotion" if name == "promote" else "demotion",
            )
            return error or f"{member.display_name} is now {args.get('rank')}."
        if name == "fire":
            from wsp.ops import fire_member

            return await fire_member(self.bot, guild, member, str(args.get("reason") or ""), actor)
        if name == "trial_start":
            from wsp.ops import start_member_trial

            try:
                days = int(args.get("days") or 0)
            except (TypeError, ValueError):
                return "Trial length must be 1 to 30 days."
            error = await start_member_trial(self.bot, guild, member, days, actor)
            return error or f"{member.display_name} is on a {days}-day trial."
        if name == "trial_end":
            from wsp.ops import complete_member_trial

            row = await self.bot.db.active_trial(guild.id, member.id)
            if row is None:
                return "Not on trial"
            await complete_member_trial(self.bot, guild, row, early=True, actor=actor)
            return f"Ended {member.display_name}'s trial."
        if name in {"give_role", "remove_role"}:
            return await set_member_role(guild, member, str(args.get("role_name") or ""), give=name == "give_role")
        return "Not found"

    async def _create_role(self, guild: discord.Guild, actor: discord.Member, args: dict[str, Any]) -> str:
        name = str(args.get("name") or "").strip()[:100]
        if not name:
            return "Not found"
        colour = discord.Colour.default()
        raw = str(args.get("color") or "").strip().lstrip("#")
        if raw:
            try:
                colour = discord.Colour(int(raw, 16))
            except ValueError:
                return "Invalid color"
        try:
            role = await guild.create_role(name=name, colour=colour, permissions=discord.Permissions.none(), reason=f"WSP assistant {actor}")
        except discord.Forbidden:
            return "Could not update roles."
        return f"Created {role.mention}."

    async def _delete_role(self, guild: discord.Guild, role_name: str) -> str:
        role = _find_role(guild, role_name)
        if role is None or role.permissions.administrator:
            return "Not found" if role is None else "Restricted"
        try:
            await role.delete(reason="WSP assistant")
        except discord.Forbidden:
            return "Could not update roles."
        return f"Deleted the {role.name} role."

    async def _delete_shift(self, guild: discord.Guild, actor: discord.Member, member: discord.Member, shift_id: Any) -> str:
        if shift_id in {None, ""}:
            rows = await self.bot.db.list_shifts(guild.id, member.id, limit=1)
            if not rows:
                return "Not found"
            shift_id = rows[0]["id"]
        try:
            shift_id = int(shift_id)
        except (TypeError, ValueError):
            return "Not found"
        row = await self.bot.db.get_shift(shift_id)
        if row is None or str(row["guild_id"]) != str(guild.id) or str(row["discord_id"]) != str(member.id):
            return "Not found"
        if row["status"] in {"active", "paused"}:
            cfg = await self.bot.guild_config(guild.id)
            await sync_duty_role(member, cfg, False)
        await self.bot.db.delete_shift(shift_id)
        from wsp.views.duty_tracker import refresh_duty_tracker

        await refresh_duty_tracker(self.bot, guild)
        await self.bot.notify(guild, "shift_log", base_embed("Shift deleted", f"{actor.mention} deleted {member.mention} shift `#{shift_id}`."))
        return f"Deleted shift #{shift_id} for {member.display_name}."

    async def _set_shift_duration(self, guild: discord.Guild, actor: discord.Member, member: discord.Member, args: dict[str, Any]) -> str:
        seconds = parse_compact_duration(str(args.get("duration") or ""))
        try:
            shift_id = int(args.get("shift_id"))
        except (TypeError, ValueError):
            return "Not found"
        if not seconds:
            return "Invalid duration"
        row = await self.bot.db.get_shift(shift_id)
        if row is None or str(row["guild_id"]) != str(guild.id) or str(row["discord_id"]) != str(member.id):
            return "Not found"
        was_open = row["status"] in {"active", "paused"}
        await self.bot.db.update_shift(shift_id, status="completed", end_time=int(row["start_time"]) + seconds, duration_seconds=seconds, pause_started=None)
        if was_open:
            cfg = await self.bot.guild_config(guild.id)
            await sync_duty_role(member, cfg, False)
            from wsp.cogs.quota import apply_shift_quota

            await apply_shift_quota(self.bot, guild.id, member.id, seconds)
        await self.bot.notify(guild, "shift_log", base_embed("Shift edited", f"{actor.mention} set {member.mention} shift `#{shift_id}` to **{format_duration(seconds)}**."))
        return f"{member.display_name} shift #{shift_id} is now {format_duration(seconds)}."

    async def _add_shift(self, guild: discord.Guild, actor: discord.Member, member: discord.Member, args: dict[str, Any]) -> str:
        from wsp.cogs.prefix import _adjust_recent_shift, _create_logged_shift

        seconds = parse_compact_duration(str(args.get("duration") or ""))
        if not seconds:
            return "Invalid duration"
        if str(args.get("mode") or "").lower() == "new":
            return await _create_logged_shift(self.bot, guild, member.id, seconds, actor)
        return await _adjust_recent_shift(self.bot, guild, member.id, seconds, actor)

    async def _remove_shift_time(self, guild: discord.Guild, actor: discord.Member, member: discord.Member, duration: str) -> str:
        from wsp.cogs.prefix import _adjust_recent_shift

        seconds = parse_compact_duration(duration)
        if not seconds:
            return "Invalid duration"
        return await _adjust_recent_shift(self.bot, guild, member.id, -seconds, actor)

    async def _quota_view(self, guild: discord.Guild, member: discord.Member) -> str:
        cfg = await self.bot.guild_config(guild.id)
        week = self.bot.db.week_start_ts(cfg.get("timezone") or "America/Chicago")
        week_id = await self.bot.db.ensure_week(guild.id, week)
        duty = await self.bot.db.get_quota_record(week_id, member.id, "duty")
        person = await self.bot.db.get_personnel(guild.id, member.id)
        required = quota_required_minutes(member, cfg, person["rank_name"] if person else None)
        done = int(duty["completed_minutes"]) if duty else 0
        return f"{member.display_name} has {done} / {required} minutes this week."

    async def _say(self, guild: discord.Guild, channel: discord.abc.Messageable, args: dict[str, Any]) -> str:
        text = str(args.get("message") or "").replace("@everyone", "everyone").replace("@here", "here")[:2000]
        if not text.strip():
            return "Not found"
        dest = channel
        raw = str(args.get("channel_id") or "").strip()
        if raw.isdigit():
            found = guild.get_channel(int(raw))
            if not isinstance(found, (discord.TextChannel, discord.Thread)):
                return "Not found"
            dest = found
        if not isinstance(dest, (discord.TextChannel, discord.Thread)):
            return "Not found"
        try:
            await dest.send(text)
        except discord.HTTPException:
            return "Could not send"
        return "Sent."

    async def _target(self, guild: discord.Guild, actor: discord.Member, raw: Any) -> discord.Member | None:
        text = str(raw or "").strip()
        if text.lower() in {"", "me", "self", "speaker", str(actor.id)}:
            return actor
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            return None
        member = guild.get_member(int(digits))
        if member is not None:
            return member
        try:
            return await guild.fetch_member(int(digits))
        except discord.HTTPException:
            return None


def _strip_mention(content: str, user_id: int) -> str:
    return content.replace(f"<@{user_id}>", " ").replace(f"<@!{user_id}>", " ")


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Assistant(bot))
