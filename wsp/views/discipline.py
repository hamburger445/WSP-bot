"""Persistent appeal buttons for members and HR review."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import discord

from wsp.constants import COLOR_DANGER, COLOR_GOLD, PermissionLevel
from wsp.db import now_ts
from wsp.embeds import add_fields, base_embed, error_embed, success_embed
from wsp.permissions import resolve_level
from wsp.utils import mention_or_id

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class MemberAppealView(discord.ui.View):
    def __init__(self, discipline_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(AppealOpenButton(discipline_id))


class AppealReviewView(discord.ui.View):
    def __init__(self, appeal_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(AppealUpholdButton(appeal_id))
        self.add_item(AppealOverturnButton(appeal_id))


class AppealOpenButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wsp:discipline:appeal:(?P<did>[0-9]+)"):
    def __init__(self, discipline_id: int = 0) -> None:
        super().__init__(
            discord.ui.Button(
                label="Appeal this action",
                style=discord.ButtonStyle.primary,
                custom_id=f"wsp:discipline:appeal:{discipline_id}",
            )
        )
        self.discipline_id = discipline_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ):
        return cls(int(match["did"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AppealModal(self.discipline_id))


class AppealUpholdButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wsp:appeal:uphold:(?P<aid>[0-9]+)"):
    def __init__(self, appeal_id: int = 0) -> None:
        super().__init__(
            discord.ui.Button(
                label="Uphold",
                style=discord.ButtonStyle.secondary,
                custom_id=f"wsp:appeal:uphold:{appeal_id}",
            )
        )
        self.appeal_id = appeal_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ):
        return cls(int(match["aid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await decide_appeal(interaction.client, interaction, self.appeal_id, "upheld", None)  # type: ignore[arg-type]


class AppealOverturnButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wsp:appeal:overturn:(?P<aid>[0-9]+)"):
    def __init__(self, appeal_id: int = 0) -> None:
        super().__init__(
            discord.ui.Button(
                label="Overturn",
                style=discord.ButtonStyle.success,
                custom_id=f"wsp:appeal:overturn:{appeal_id}",
            )
        )
        self.appeal_id = appeal_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ):
        return cls(int(match["aid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(OverturnModal(self.appeal_id))


class AppealModal(discord.ui.Modal, title="Appeal disciplinary action"):
    statement = discord.ui.TextInput(
        label="Why should this action be reviewed?",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    def __init__(self, discipline_id: int) -> None:
        super().__init__()
        self.discipline_id = discipline_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await submit_appeal(
            interaction.client,  # type: ignore[arg-type]
            interaction,
            self.discipline_id,
            str(self.statement.value),
        )


class OverturnModal(discord.ui.Modal, title="Overturn disciplinary action"):
    note = discord.ui.TextInput(label="Review note", style=discord.TextStyle.paragraph, max_length=800)

    def __init__(self, appeal_id: int) -> None:
        super().__init__()
        self.appeal_id = appeal_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await decide_appeal(
            interaction.client,  # type: ignore[arg-type]
            interaction,
            self.appeal_id,
            "overturned",
            str(self.note.value),
        )


async def submit_appeal(
    bot: WSPBot,
    interaction: discord.Interaction,
    discipline_id: int,
    statement: str,
) -> None:
    record = await bot.db.get_discipline(discipline_id)
    if record is None or str(record["discord_id"]) != str(interaction.user.id):
        await _reply(interaction, error_embed("Not found", "That record is not on your file."))
        return
    if not int(record["active"] or 0):
        await _reply(interaction, error_embed("Not appealable", "That record is no longer active."))
        return
    existing = await bot.db.get_appeal_for_discipline(discipline_id)
    if existing:
        status = existing["status"]
        if status == "pending":
            await _reply(interaction, error_embed("Already submitted", "An appeal for this record is already on file."))
        else:
            await _reply(interaction, error_embed("Already appealed", "This record has already been appealed."))
        return
    guild = bot.get_guild(int(record["guild_id"]))
    if guild is None:
        await _reply(interaction, error_embed("Unavailable", "Could not reach the department server."))
        return
    appeal_id = await bot.db.create_appeal(guild.id, discipline_id, interaction.user.id, statement)
    await bot.db.audit(
        guild.id,
        "discipline_appeal",
        actor_id=interaction.user.id,
        actor_name=str(interaction.user),
        target_id=record["discord_id"],
        details=f"appeal #{appeal_id} for discipline #{discipline_id}",
    )
    await _reply(
        interaction,
        success_embed(
            "Appeal submitted",
            f"Your appeal for record `#{discipline_id}` is on file. You will be notified when it is reviewed.",
        ),
    )
    review = base_embed(
        "Discipline appeal",
        f"{mention_or_id(guild, interaction.user.id)} is appealing **{record['action']}** `#{discipline_id}`",
        color=COLOR_GOLD,
    )
    add_fields(
        review,
        [
            ("Appeal", f"#{appeal_id}", True),
            ("Record", f"#{discipline_id}", True),
            ("Action", record["action"], True),
            ("Original reason", record["reason"], False),
            ("Statement", statement, False),
        ],
    )
    view = AppealReviewView(appeal_id)
    posted = await bot.notify(guild, "discipline", review, view=view)
    if posted is None:
        await bot.notify(guild, "hr_log", review, view=view)


async def decide_appeal(
    bot: WSPBot,
    interaction: discord.Interaction,
    appeal_id: int,
    status: str,
    note: str | None,
) -> None:
    if not interaction.guild:
        await _reply(interaction, error_embed("Guild only"))
        return
    if await resolve_level(interaction) < PermissionLevel.HR:
        await _reply(interaction, error_embed("Restricted", "You do not have permission to review appeals."))
        return
    row = await bot.db.get_appeal(appeal_id)
    if row is None or str(row["guild_id"]) != str(interaction.guild.id):
        await _reply(interaction, error_embed("Not found"))
        return
    if row["status"] != "pending":
        await _reply(interaction, error_embed("Already decided", f"This appeal is `{row['status']}`."))
        return
    record = await bot.db.get_discipline(int(row["discipline_id"]))
    await bot.db.update_appeal(
        appeal_id,
        status=status,
        reviewer_id=str(interaction.user.id),
        review_note=note,
        reviewed_at=now_ts(),
    )
    if status == "overturned" and record:
        await bot.db.deactivate_discipline(int(record["id"]))
        await _restore_status_if_needed(bot, interaction.guild, record)
    await bot.db.audit(
        interaction.guild.id,
        f"appeal_{status}",
        actor_id=interaction.user.id,
        actor_name=str(interaction.user),
        target_id=row["discord_id"],
        details=f"appeal #{appeal_id} discipline #{row['discipline_id']} {note or ''}",
    )
    staff = success_embed(
        f"Appeal {status}",
        f"{mention_or_id(interaction.guild, row['discord_id'])} • appeal `#{appeal_id}`",
    )
    if note:
        staff.add_field(name="Note", value=note, inline=False)
    await _reply(interaction, staff)
    if interaction.message:
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            pass
    await bot.notify(interaction.guild, "discipline", staff)
    if status == "overturned":
        member_embed = success_embed(
            "Appeal granted",
            f"Your appeal of record `#{row['discipline_id']}` was granted. "
            "That disciplinary action is no longer active on your file.",
        )
    else:
        member_embed = base_embed(
            "Appeal reviewed",
            f"Your appeal of record `#{row['discipline_id']}` was reviewed. "
            "The disciplinary action remains on your file.",
            color=COLOR_DANGER,
        )
    if note:
        member_embed.add_field(name="Note", value=note, inline=False)
    member = await bot.fetch_guild_user(interaction.guild, int(row["discord_id"]))
    await bot.try_dm(member, member_embed)


async def _restore_status_if_needed(bot: WSPBot, guild: discord.Guild, record) -> None:
    if record["action"] not in {"Suspension", "Removal"}:
        return
    others = await bot.db.list_discipline(guild.id, int(record["discord_id"]), active_only=True)
    still = [
        r for r in others
        if int(r["id"]) != int(record["id"]) and r["action"] in {"Suspension", "Removal"}
    ]
    if still:
        return
    personnel = await bot.db.get_personnel(guild.id, int(record["discord_id"]))
    if personnel and personnel["status"] in {"suspended", "removed"}:
        await bot.db.update_personnel(personnel["id"], status="active")


async def _reply(interaction: discord.Interaction, embed: discord.Embed) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)
