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

from wsp.constants import PermissionLevel
from wsp.db import now_ts
from wsp.embeds import ts
from wsp.permissions import resolve_user_level
from wsp.utils import apply_role_changes, fetch_live_member, member_role_ids

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
        "Remove a Discord role. HR only.",
        {"member_id": _str_param("Discord user id"), "role_name": _str_param("Exact role name")},
        ["member_id", "role_name"],
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
            "Do not tell them which command to run. Giving or removing a role means calling the tool, not describing the role. "
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
                    result = await self._execute(message.author, message.guild, name, args)
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

    async def _execute(self, actor: discord.Member, guild: discord.Guild, name: str, args: dict[str, Any]) -> str:
        try:
            member = await self._target(guild, actor, args.get("member_id"))
        except ValueError:
            return "Not found"
        if member is None:
            return "Not found"
        level = await resolve_user_level(self.bot, guild.id, actor)
        own = member.id == actor.id
        if name in {"promote", "demote", "fire", "trial_start", "trial_end", "give_role", "remove_role"} and level < PermissionLevel.HR:
            return "Restricted"
        if name in {"start_shift", "pause_shift", "resume_shift", "end_shift"} and not own and level < PermissionLevel.SUPERVISOR:
            return "Restricted"
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
