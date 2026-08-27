"""Leave of Absence requests and approvals."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_DANGER, COLOR_GOLD, PermissionLevel
from wsp.db import now_ts
from wsp.embeds import add_fields, base_embed, error_embed, success_embed, ts
from wsp.permissions import has_level, resolve_level
from wsp.utils import ensure_personnel, mention_or_id, parse_date

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class LOA(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    loa = app_commands.Group(name="loa", description="Leave of absence")

    @loa.command(name="request", description="Request leave.")
    @app_commands.describe(
        start_date="Start date",
        end_date="End date",
        reason="Reason",
        additional_information="More information",
    )
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
        ok, embed = await create_loa_request(
            self.bot,
            interaction.guild,
            interaction.user,
            start_date,
            end_date,
            reason,
            additional_information,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @loa.command(name="active", description="List members on leave.")
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

    @loa.command(name="menu", description="Open the leave menu.")
    async def loa_menu(self, interaction: discord.Interaction) -> None:
        embed = base_embed("Leave of Absence", "Submit a leave request or view your requests.")
        await interaction.response.send_message(embed=embed, view=LOAMenuView(), ephemeral=True)

    @loa.command(name="admin", description="Manage a member's leave.")
    @has_level(PermissionLevel.HR)
    @app_commands.describe(member="Member")
    async def admin(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        embed = await build_loa_admin_embed(self.bot, interaction.guild, member)
        await interaction.response.send_message(embed=embed, view=LOAAdminView(member), ephemeral=True)


async def create_loa_request(
    bot: WSPBot,
    guild: discord.Guild,
    member: discord.Member,
    start_date: str,
    end_date: str,
    reason: str,
    additional_information: str | None = None,
) -> tuple[bool, discord.Embed]:
    cfg = await bot.guild_config(guild.id)
    tz = cfg.get("timezone") or "America/Chicago"
    start = parse_date(start_date, tz)
    end = parse_date(end_date, tz)
    if not start or not end or end <= start:
        return False, error_embed("Invalid dates", "Use `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`. End must be after start.")
    max_days = int(cfg.get("loa", "max_days") or 30)
    if end - start > max_days * 86400:
        return False, error_embed("Too long", f"Leave may not exceed {max_days} days.")
    await ensure_personnel(bot, member)
    loa_id = await bot.db.create_loa(guild.id, member.id, start, end, reason, additional_information)
    await bot.db.audit(
        guild.id, "loa_request", actor_id=member.id, actor_name=str(member),
        details=f"#{loa_id} {start_date} → {end_date}",
    )
    public = base_embed("LOA request submitted", f"{member.mention}  •  `{loa_id}`", color=COLOR_GOLD)
    add_fields(
        public,
        [
            ("Start", ts(start), True),
            ("End", ts(end), True),
            ("Reason", reason, False),
            ("Additional", additional_information or "—", False),
        ],
    )
    posted = await _post_loa_review(bot, guild, public, LOADecisionView(loa_id))
    if not posted:
        return False, error_embed("Could not submit request")
    return True, success_embed("Request submitted")


async def start_member_loa(
    bot: WSPBot,
    guild: discord.Guild,
    member: discord.Member,
    start_date: str,
    end_date: str,
    reason: str | None,
    actor: discord.abc.User,
) -> discord.Embed:
    parsed = await _parse_loa_range(bot, guild, start_date, end_date)
    if isinstance(parsed, discord.Embed):
        return parsed
    start, end = parsed
    existing = await bot.db.active_loa(guild.id, member.id)
    if existing:
        return error_embed("Already on leave", f"{member.mention} is already on leave `#{existing['id']}`.")
    overlap = await _overlapping_approved(bot, guild.id, member.id, start, end)
    if overlap:
        return error_embed("Leave already scheduled", f"{member.mention} already has leave `#{overlap['id']}`.")
    for pending in await bot.db.list_member_loa(guild.id, member.id):
        if pending["status"] != "pending":
            continue
        if int(pending["start_date"]) < end and int(pending["end_date"]) > start:
            await bot.db.update_loa(pending["id"], status="denied", review_note="Replaced by started leave")
    await ensure_personnel(bot, member)
    note = (reason or "").strip() or "Leave"
    loa_id = await bot.db.create_loa(
        guild.id,
        member.id,
        start,
        end,
        note,
        None,
        status="approved",
        reviewer_id=str(actor.id),
    )
    await _sync_personnel_leave(bot, guild, member.id)
    await bot.db.audit(
        guild.id,
        "loa_start",
        actor_id=actor.id,
        actor_name=str(actor),
        target_id=member.id,
        target_name=str(member),
        details=f"#{loa_id} {start_date} → {end_date}",
    )
    embed = success_embed("Leave started", f"{member.mention}  •  `#{loa_id}`")
    add_fields(embed, [("Start", ts(start), True), ("End", ts(end), True), ("Reason", note, False)])
    await _announce_loa(bot, guild, embed, member, f"You are on leave.\n{ts(start)} → {ts(end)}")
    return embed


async def end_member_loa(
    bot: WSPBot,
    guild: discord.Guild,
    member: discord.Member,
    actor: discord.abc.User,
) -> discord.Embed:
    row = await _open_loa(bot, guild, member, None)
    if row is None:
        return error_embed("Not on leave")
    now = now_ts()
    fields = {"status": "expired", "reviewer_id": str(actor.id)}
    if int(row["start_date"]) <= now:
        fields["end_date"] = now
    await bot.db.update_loa(row["id"], **fields)
    await _sync_personnel_leave(bot, guild, member.id)
    await bot.db.audit(
        guild.id,
        "loa_end",
        actor_id=actor.id,
        actor_name=str(actor),
        target_id=member.id,
        target_name=str(member),
        details=f"#{row['id']}",
    )
    embed = success_embed("Leave ended", f"{member.mention}  •  `#{row['id']}`")
    await _announce_loa(bot, guild, embed, member, "Your leave has ended.")
    return embed


async def edit_member_loa(
    bot: WSPBot,
    guild: discord.Guild,
    member: discord.Member,
    actor: discord.abc.User,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    reason: str | None = None,
    loa_id: int | None = None,
) -> discord.Embed:
    start_text = (start_date or "").strip()
    end_text = (end_date or "").strip()
    note = (reason or "").strip()
    if not start_text and not end_text and not note:
        return error_embed("Nothing to change", "Enter a new start date, end date, or reason.")
    row = await _open_loa(bot, guild, member, loa_id, include_recent=True)
    if row is None:
        return error_embed("Not found")
    if row["status"] == "denied":
        return error_embed("Not found")
    tz = await _guild_tz(bot, guild)
    start = parse_date(start_text, tz) if start_text else int(row["start_date"])
    end = parse_date(end_text, tz) if end_text else int(row["end_date"])
    if start_text and start is None:
        return error_embed("Invalid dates", "Use `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`.")
    if end_text and end is None:
        return error_embed("Invalid dates", "Use `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`.")
    if not start or not end or end <= start:
        return error_embed("Invalid dates", "End must be after start.")
    overlap = await _overlapping_approved(bot, guild.id, member.id, start, end, exclude_id=int(row["id"]))
    if overlap:
        return error_embed("Leave already scheduled", f"{member.mention} already has leave `#{overlap['id']}`.")
    now = now_ts()
    status = str(row["status"])
    if status == "pending":
        pass
    elif end < now:
        status = "expired"
    else:
        status = "approved"
    fields: dict[str, object] = {
        "start_date": start,
        "end_date": end,
        "status": status,
        "reviewer_id": str(actor.id),
    }
    if note:
        fields["reason"] = note
    await bot.db.update_loa(row["id"], **fields)
    await _sync_personnel_leave(bot, guild, member.id)
    await bot.db.audit(
        guild.id,
        "loa_edit",
        actor_id=actor.id,
        actor_name=str(actor),
        target_id=member.id,
        target_name=str(member),
        details=f"#{row['id']} {ts(start)} → {ts(end)}",
    )
    embed = success_embed("Leave updated", f"{member.mention}  •  `#{row['id']}`")
    add_fields(
        embed,
        [
            ("Start", ts(start), True),
            ("End", ts(end), True),
            ("Reason", note or row["reason"] or "—", False),
        ],
    )
    await _announce_loa(bot, guild, embed, member, f"Your leave was updated.\n{ts(start)} → {ts(end)}")
    return embed


async def _guild_tz(bot: WSPBot, guild: discord.Guild) -> str:
    cfg = await bot.guild_config(guild.id)
    return cfg.get("timezone") or "America/Chicago"


async def _parse_loa_range(
    bot: WSPBot, guild: discord.Guild, start_date: str, end_date: str
) -> tuple[int, int] | discord.Embed:
    tz = await _guild_tz(bot, guild)
    start = parse_date(start_date, tz)
    end = parse_date(end_date, tz)
    if not start or not end or end <= start:
        return error_embed("Invalid dates", "Use `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`. End must be after start.")
    return start, end


async def _overlapping_approved(
    bot: WSPBot,
    guild_id: int,
    discord_id: int,
    start: int,
    end: int,
    *,
    exclude_id: int | None = None,
) -> object | None:
    for row in await bot.db.list_member_loa(guild_id, discord_id):
        if exclude_id is not None and int(row["id"]) == exclude_id:
            continue
        if row["status"] != "approved":
            continue
        if int(row["start_date"]) < end and int(row["end_date"]) > start:
            return row
    return None


async def _open_loa(
    bot: WSPBot,
    guild: discord.Guild,
    member: discord.Member,
    loa_id: int | None,
    *,
    include_recent: bool = False,
):
    if loa_id:
        row = await bot.db.get_loa(loa_id)
        if (
            row is None
            or str(row["guild_id"]) != str(guild.id)
            or str(row["discord_id"]) != str(member.id)
        ):
            return None
        return row
    active = await bot.db.active_loa(guild.id, member.id)
    if active:
        return active
    now = now_ts()
    upcoming = [
        r
        for r in await bot.db.list_member_loa(guild.id, member.id)
        if r["status"] == "approved" and int(r["end_date"]) >= now
    ]
    upcoming.sort(key=lambda r: int(r["start_date"]))
    if upcoming:
        return upcoming[0]
    if include_recent:
        rows = [r for r in await bot.db.list_member_loa(guild.id, member.id) if r["status"] != "denied"]
        return rows[0] if rows else None
    return None


async def _sync_personnel_leave(bot: WSPBot, guild: discord.Guild, discord_id: int) -> None:
    personnel = await bot.db.get_personnel(guild.id, discord_id)
    if personnel is None or personnel["status"] not in {"active", "loa"}:
        return
    wanted = "loa" if await bot.db.active_loa(guild.id, discord_id) else "active"
    if personnel["status"] != wanted:
        await bot.db.update_personnel(personnel["id"], status=wanted)


async def _announce_loa(
    bot: WSPBot,
    guild: discord.Guild,
    embed: discord.Embed,
    member: discord.Member,
    dm_text: str,
) -> None:
    await bot.notify(guild, "loa", embed)
    await bot.notify(guild, "hr_log", embed)
    await bot.try_dm(member, base_embed("Leave of Absence", dm_text))


async def build_loa_admin_embed(
    bot: WSPBot, guild: discord.Guild, member: discord.Member | None = None
) -> discord.Embed:
    embed = base_embed("Leave admin", "Start, end, or change a member's leave.")
    rows = await bot.db.list_loa(guild.id, "approved")
    now = now_ts()
    current = [r for r in rows if int(r["start_date"]) <= now <= int(r["end_date"])]
    listing = "\n".join(
        f"{mention_or_id(guild, r['discord_id'])} • {ts(r['start_date'])} → {ts(r['end_date'])}"
        for r in current[:15]
    )
    embed.add_field(name="On leave", value=listing or "No one is on leave.", inline=False)
    if member:
        row = await _open_loa(bot, guild, member, None, include_recent=True)
        if row and row["status"] != "expired":
            embed.add_field(
                name="Selected",
                value=f"{member.mention}  •  `#{row['id']}`\n{ts(row['start_date'])} → {ts(row['end_date'])}",
                inline=False,
            )
        else:
            embed.add_field(name="Selected", value=f"{member.mention}  •  not on leave", inline=False)
    return embed


def _is_error_embed(embed: discord.Embed) -> bool:
    return embed.colour is not None and embed.colour.value == COLOR_DANGER


async def _admin_allowed(interaction: discord.Interaction) -> bool:
    if await resolve_level(interaction) < PermissionLevel.HR:
        await interaction.response.send_message(embed=error_embed("Restricted"), ephemeral=True)
        return False
    return True


class LOAAdminView(discord.ui.View):
    def __init__(self, member: discord.Member | None = None) -> None:
        super().__init__(timeout=240)
        self.member_id = member.id if member else None
        if member:
            for item in self.children:
                if isinstance(item, discord.ui.UserSelect):
                    item.default_values = [member]

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a member", row=0)
    async def pick_member(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        if not await _admin_allowed(interaction):
            return
        picked = select.values[0]
        self.member_id = picked.id
        member = await _resolve_admin_member(interaction, picked.id)
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        embed = await build_loa_admin_embed(bot, interaction.guild, member) if interaction.guild else error_embed("Guild only")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Start leave", style=discord.ButtonStyle.success, row=1)
    async def start_leave(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await _admin_allowed(interaction):
            return
        member = await _selected_admin_member(self, interaction)
        if member is None:
            return
        await interaction.response.send_modal(LOAAdminStartModal(member))

    @discord.ui.button(label="End leave", style=discord.ButtonStyle.danger, row=1)
    async def end_leave(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await _admin_allowed(interaction):
            return
        member = await _selected_admin_member(self, interaction)
        if member is None:
            return
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        result = await end_member_loa(bot, interaction.guild, member, interaction.user)
        if _is_error_embed(result):
            await interaction.response.send_message(embed=result, ephemeral=True)
            return
        embed = await build_loa_admin_embed(bot, interaction.guild, member)
        await interaction.response.edit_message(embed=embed, view=LOAAdminView(member))

    @discord.ui.button(label="Change leave", style=discord.ButtonStyle.primary, row=1)
    async def change_leave(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await _admin_allowed(interaction):
            return
        member = await _selected_admin_member(self, interaction)
        if member is None:
            return
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        row = await _open_loa(bot, interaction.guild, member, None, include_recent=True)
        if row is None:
            await interaction.response.send_message(embed=error_embed("Not on leave"), ephemeral=True)
            return
        start_default = ""
        end_default = ""
        reason_default = ""
        if row and interaction.guild:
            tz = ZoneInfo(await _guild_tz(bot, interaction.guild))
            start_default = datetime.fromtimestamp(int(row["start_date"]), tz).strftime("%Y-%m-%d")
            end_default = datetime.fromtimestamp(int(row["end_date"]), tz).strftime("%Y-%m-%d")
            reason_default = str(row["reason"] or "")
        await interaction.response.send_modal(LOAAdminChangeModal(member, start_default, end_default, reason_default))


class LOAAdminStartModal(discord.ui.Modal, title="Start leave"):
    start_date = discord.ui.TextInput(label="Start date", placeholder="YYYY-MM-DD")
    end_date = discord.ui.TextInput(label="End date", placeholder="YYYY-MM-DD")
    reason = discord.ui.TextInput(label="Reason", required=False, max_length=200)

    def __init__(self, member: discord.Member) -> None:
        super().__init__()
        self.member = member

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _finish_admin_modal(
            interaction,
            self.member,
            start_member_loa,
            str(self.start_date.value),
            str(self.end_date.value),
            str(self.reason.value) if self.reason.value else None,
        )


class LOAAdminChangeModal(discord.ui.Modal, title="Change leave"):
    start_date = discord.ui.TextInput(label="Start date", placeholder="YYYY-MM-DD")
    end_date = discord.ui.TextInput(label="End date", placeholder="YYYY-MM-DD")
    reason = discord.ui.TextInput(label="Reason", required=False, max_length=200)

    def __init__(self, member: discord.Member, start_default: str, end_default: str, reason_default: str) -> None:
        super().__init__()
        self.member = member
        if start_default:
            self.start_date.default = start_default
        if end_default:
            self.end_date.default = end_default
        if reason_default:
            self.reason.default = reason_default[:200]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        async def _edit(bot, guild, member, start_date, end_date, reason, actor):
            return await edit_member_loa(
                bot, guild, member, actor, start_date=start_date, end_date=end_date, reason=reason
            )

        await _finish_admin_modal(
            interaction,
            self.member,
            _edit,
            str(self.start_date.value),
            str(self.end_date.value),
            str(self.reason.value) if self.reason.value else None,
        )


async def _finish_admin_modal(
    interaction: discord.Interaction,
    member: discord.Member,
    action,
    start_date: str,
    end_date: str,
    reason: str | None,
) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild:
        await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
        return
    result = await action(bot, interaction.guild, member, start_date, end_date, reason, interaction.user)
    if _is_error_embed(result):
        await interaction.response.send_message(embed=result, ephemeral=True)
        return
    embed = await build_loa_admin_embed(bot, interaction.guild, member)
    await interaction.response.edit_message(embed=embed, view=LOAAdminView(member))


async def _selected_admin_member(view: LOAAdminView, interaction: discord.Interaction) -> discord.Member | None:
    if view.member_id is None:
        await interaction.response.send_message(embed=error_embed("Select a member"), ephemeral=True)
        return None
    member = await _resolve_admin_member(interaction, view.member_id)
    if member is None:
        await interaction.response.send_message(embed=error_embed("Member not found"), ephemeral=True)
    return member


async def _resolve_admin_member(interaction: discord.Interaction, member_id: int) -> discord.Member | None:
    if not interaction.guild:
        return None
    member = interaction.guild.get_member(member_id)
    if member:
        return member
    try:
        return await interaction.guild.fetch_member(member_id)
    except discord.HTTPException:
        return None


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
                label="Accept",
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


async def _post_loa_review(bot: WSPBot, guild: discord.Guild, embed: discord.Embed, view: discord.ui.View) -> bool:
    cfg = await bot.guild_config(guild.id)
    channel_id = cfg.channel_id("loa")
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is None and channel_id:
        try:
            fetched = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            fetched = None
        channel = fetched if isinstance(fetched, discord.TextChannel) else None
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=embed, view=view)
            return True
        except discord.HTTPException:
            return False
    return False


async def _decide_loa(bot: WSPBot, interaction: discord.Interaction, loa_id: int, status: str, note: str | None) -> None:
    if not interaction.guild:
        return
    from wsp.permissions import resolve_level

    if await resolve_level(interaction) < PermissionLevel.HR:
        if interaction.response.is_done():
            await interaction.followup.send(embed=error_embed("Restricted", "HR only."), ephemeral=True)
        else:
            await interaction.response.send_message(embed=error_embed("Restricted", "HR only."), ephemeral=True)
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
    if interaction.message:
        try:
            await interaction.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass
    await bot.notify(interaction.guild, "notifications", embed)
    if status == "approved":
        member_embed = success_embed(
            "Leave approved",
            f"Your LOA `#{loa_id}` is approved.\n{ts(row['start_date'])} → {ts(row['end_date'])}",
        )
    else:
        member_embed = error_embed(
            "Leave request not approved",
            f"Your LOA `#{loa_id}` was not approved.",
        )
    if note:
        member_embed.add_field(name="Note", value=note, inline=False)
    member = await bot.fetch_guild_user(interaction.guild, int(row["discord_id"]))
    await bot.try_dm(member, member_embed)


async def _reply(interaction: discord.Interaction, embed: discord.Embed) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(LOA(bot))
