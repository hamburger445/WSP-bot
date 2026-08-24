"""Supervision / ride-along sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_GOLD, PermissionLevel, SUPERVISION_SCORE_FIELDS
from wsp.db import now_ts
from wsp.embeds import add_fields, base_embed, error_embed, format_duration, success_embed, ts, ts_rel
from wsp.permissions import has_level
from wsp.utils import ensure_personnel, mention_or_id

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class Supervision(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    supervision = app_commands.Group(name="supervision", description="Ride-along / field supervision")

    @supervision.command(name="start", description="Start a supervision session with a trooper.")
    @has_level(PermissionLevel.SUPERVISOR)
    async def start(self, interaction: discord.Interaction, trooper: discord.Member) -> None:
        if not interaction.guild:
            return
        if trooper.id == interaction.user.id:
            await interaction.response.send_message(embed=error_embed("Invalid", "You cannot supervise yourself."), ephemeral=True)
            return
        existing = await self.bot.db.active_supervision(interaction.guild.id, trooper.id)
        if existing:
            await interaction.response.send_message(
                embed=error_embed("Already active", f"Session `#{existing['id']}` is still open."),
                ephemeral=True,
            )
            return
        await ensure_personnel(self.bot, trooper)
        sid = await self.bot.db.start_supervision(interaction.guild.id, trooper.id, interaction.user.id)
        personnel = await self.bot.db.get_personnel(interaction.guild.id, trooper.id)
        if personnel:
            await self.bot.db.update_personnel(personnel["id"], supervision_status="in_progress")
        await self.bot.db.audit(
            interaction.guild.id, "supervision_start", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=trooper.id, target_name=str(trooper), details=f"#{sid}",
        )
        embed = success_embed("Supervision started", f"{trooper.mention} with {interaction.user.mention}\nSession `#{sid}`")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self.bot.notify(interaction.guild, "supervision", base_embed("Supervision started", f"{trooper.mention} • supervisor {interaction.user.mention} • `#{sid}`"))

    @supervision.command(name="complete", description="Complete an active supervision session and submit the evaluation.")
    @has_level(PermissionLevel.SUPERVISOR)
    async def complete(self, interaction: discord.Interaction, trooper: discord.Member) -> None:
        if not interaction.guild:
            return
        row = await self.bot.db.active_supervision(interaction.guild.id, trooper.id)
        if row is None:
            await interaction.response.send_message(embed=error_embed("No active session"), ephemeral=True)
            return
        if str(row["supervisor_id"]) != str(interaction.user.id):
            from wsp.permissions import resolve_level

            if await resolve_level(interaction) < PermissionLevel.HR:
                await interaction.response.send_message(embed=error_embed("Restricted", "Only the assigned supervisor or HR can complete this session."), ephemeral=True)
                return
        view = SupervisionEvalView(int(row["id"]), trooper.id)
        await interaction.response.send_message(
            embed=base_embed("Supervision evaluation", f"Session `#{row['id']}` for {trooper.mention}. Score each area 1–5, then pass or fail."),
            view=view,
            ephemeral=True,
        )

    @supervision.command(name="review", description="Review a supervision session.")
    @has_level(PermissionLevel.SUPERVISOR)
    async def review(self, interaction: discord.Interaction, session_id: int) -> None:
        if not interaction.guild:
            return
        row = await self.bot.db.get_supervision(session_id)
        if row is None:
            await interaction.response.send_message(embed=error_embed("Not found"), ephemeral=True)
            return
        await interaction.response.send_message(embed=_sup_embed(interaction.guild, row), ephemeral=True)

    @supervision.command(name="history", description="View supervision history for a member.")
    @has_level(PermissionLevel.SUPERVISOR)
    async def history(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if not interaction.guild:
            return
        target = member or interaction.user
        rows = await self.bot.db.list_supervisions(interaction.guild.id, target.id)
        embed = base_embed(f"Supervision history  •  {target}")
        embed.description = "\n".join(
            f"`#{r['id']}` {r['status']} {r['result'] or ''} • {format_duration(r['duration_seconds'])} • {ts_rel(r['start_time'])}"
            for r in rows[:15]
        ) or "No sessions on file."
        await interaction.response.send_message(embed=embed, ephemeral=True)


def _sup_embed(guild: discord.Guild, row) -> discord.Embed:
    embed = base_embed(f"Supervision  #{row['id']}")
    add_fields(
        embed,
        [
            ("Trooper", mention_or_id(guild, row["trooper_id"]), True),
            ("Supervisor", mention_or_id(guild, row["supervisor_id"]), True),
            ("Result", (row["result"] or "pending").title(), True),
            ("Started", ts(row["start_time"]), True),
            ("Ended", ts(row["end_time"]), True),
            ("Duration", format_duration(row["duration_seconds"]), True),
            ("Traffic stops", str(row["traffic_stops"] or 0), True),
        ],
    )
    scores = []
    for field, label in SUPERVISION_SCORE_FIELDS:
        if row[field] is not None:
            scores.append(f"**{label}** — {row[field]}/5")
    if scores:
        embed.add_field(name="Scores", value="\n".join(scores), inline=False)
    if row["comments"]:
        embed.add_field(name="Comments", value=row["comments"][:1024], inline=False)
    return embed


class SupervisionEvalView(discord.ui.View):
    def __init__(
        self,
        session_id: int,
        trooper_id: int,
        page: int = 0,
        scores: dict[str, int] | None = None,
        traffic_stops: int = 0,
        comments: str = "",
    ) -> None:
        super().__init__(timeout=600)
        self.session_id = session_id
        self.trooper_id = trooper_id
        self.page = page
        self.scores = scores or {}
        self.traffic_stops = traffic_stops
        self.comments = comments
        chunk = SUPERVISION_SCORE_FIELDS[page * 3 : page * 3 + 3]
        for field, label in chunk:
            self.add_item(ScoreSelect(field, label))
        if page == 0:
            self.add_item(SupPageButton("Next scores", 1))
        else:
            self.add_item(SupPageButton("Back", 0))
            self.add_item(CommentsButton())
            self.add_item(PassButton())
            self.add_item(FailButton())


class SupPageButton(discord.ui.Button):
    def __init__(self, label: str, page: int) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SupervisionEvalView = self.view  # type: ignore[assignment]
        nxt = SupervisionEvalView(
            view.session_id, view.trooper_id, self.page, view.scores, view.traffic_stops, view.comments
        )
        await interaction.response.edit_message(view=nxt)


class CommentsButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Stops & comments", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SupervisionEvalView = self.view  # type: ignore[assignment]
        await interaction.response.send_modal(SupervisionNotesModal(view))


class PassButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Pass", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SupervisionEvalView = self.view  # type: ignore[assignment]
        await finalize_supervision(interaction, view, "pass")


class FailButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Fail", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SupervisionEvalView = self.view  # type: ignore[assignment]
        await finalize_supervision(interaction, view, "fail")


class ScoreSelect(discord.ui.Select):
    def __init__(self, field: str, label: str) -> None:
        self.field = field
        super().__init__(
            placeholder=label,
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=str(n), value=str(n)) for n in range(1, 6)],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SupervisionEvalView = self.view  # type: ignore[assignment]
        view.scores[self.field] = int(self.values[0])
        await interaction.response.defer()


class SupervisionNotesModal(discord.ui.Modal, title="Supervision notes"):
    stops = discord.ui.TextInput(label="Traffic stops observed", placeholder="0", max_length=4)
    comments = discord.ui.TextInput(label="Supervisor comments", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, parent: SupervisionEvalView) -> None:
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            self.parent.traffic_stops = int(str(self.stops.value or "0"))
        except ValueError:
            self.parent.traffic_stops = 0
        self.parent.comments = str(self.comments.value)
        await interaction.response.send_message(embed=success_embed("Notes stored", "Continue with Pass or Fail."), ephemeral=True)


async def finalize_supervision(interaction: discord.Interaction, view: SupervisionEvalView, result: str) -> None:
    bot: WSPBot = interaction.client  # type: ignore[assignment]
    if not interaction.guild:
        return
    missing = [label for field, label in SUPERVISION_SCORE_FIELDS if field not in view.scores]
    if missing:
        await interaction.response.send_message(embed=error_embed("Incomplete", "Score: " + ", ".join(missing)), ephemeral=True)
        return
    row = await bot.db.get_supervision(view.session_id)
    if row is None:
        await interaction.response.send_message(embed=error_embed("Not found"), ephemeral=True)
        return
    end = now_ts()
    duration = max(0, end - int(row["start_time"]))
    fields = {
        "end_time": end,
        "duration_seconds": duration,
        "traffic_stops": view.traffic_stops,
        "comments": view.comments,
        "result": result,
        "status": "completed",
        **view.scores,
    }
    await bot.db.update_supervision(view.session_id, **fields)
    personnel = await bot.db.get_personnel(interaction.guild.id, view.trooper_id)
    if personnel:
        if result == "pass":
            await bot.db.update_personnel(personnel["id"], supervision_status="complete", probation_status="active")
            cfg = await bot.guild_config(interaction.guild.id)
            days = int(cfg.get("probation", "duration_days") or 14)
            await bot.db.start_probation(interaction.guild.id, view.trooper_id, interaction.user.id, days)
            await bot.notify(
                interaction.guild,
                "probation",
                base_embed("Probation started", f"{mention_or_id(interaction.guild, view.trooper_id)} entered a **{days}-day** probationary period after successful supervision."),
            )
            await bot.notify(
                interaction.guild,
                "notifications",
                base_embed("Probation started", f"{mention_or_id(interaction.guild, view.trooper_id)} is now on probation."),
            )
        else:
            await bot.db.update_personnel(personnel["id"], supervision_status="required")
    minutes = max(1, duration // 60)
    from wsp.cogs.quota import apply_supervision_quota

    await apply_supervision_quota(bot, interaction.guild.id, interaction.user.id, minutes)
    await bot.db.audit(
        interaction.guild.id, "supervision_complete", actor_id=interaction.user.id, actor_name=str(interaction.user),
        target_id=view.trooper_id, details=f"#{view.session_id} {result} {format_duration(duration)}",
    )
    public = base_embed(
        "Supervision completed",
        f"{mention_or_id(interaction.guild, view.trooper_id)}  •  **{result.upper()}**  •  {format_duration(duration)}",
        color=COLOR_GOLD,
    )
    await interaction.response.send_message(embed=success_embed("Session recorded", f"Result: **{result}**."), ephemeral=True)
    await bot.notify(interaction.guild, "supervision", public)
    await bot.notify(interaction.guild, "notifications", public)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Supervision(bot))
