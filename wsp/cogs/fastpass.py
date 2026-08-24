"""Fast-pass knowledge evaluation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import (
    COLOR_GOLD,
    FASTPASS_CATEGORIES,
    FASTPASS_RECOMMENDATIONS,
    FASTPASS_SCALE,
    PermissionLevel,
)
from wsp.db import now_ts
from wsp.embeds import add_fields, base_embed, error_embed, success_embed, ts
from wsp.permissions import has_level
from wsp.utils import ensure_personnel, mention_or_id

if TYPE_CHECKING:
    from wsp.bot import WSPBot


class FastPass(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    fastpass = app_commands.Group(name="fastpass", description="Applicant fast-pass knowledge evaluation")

    @fastpass.command(name="start", description="Start a fast-pass evaluation for a member.")
    @has_level(PermissionLevel.HR)
    async def start(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not interaction.guild:
            return
        await ensure_personnel(self.bot, member)
        fp_id = await self.bot.db.create_fastpass(interaction.guild.id, member.id, interaction.user.id)
        await self.bot.db.audit(
            interaction.guild.id, "fastpass_start", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=member.id, target_name=str(member), details=f"#{fp_id}",
        )
        await self.bot.notify(
            interaction.guild,
            "applications",
            base_embed("Fast-pass evaluation started", f"{member.mention}  •  Evaluation `#{fp_id}` by {interaction.user.mention}"),
        )
        view = FastPassScoreView(fp_id, member.id)
        embed = base_embed(
            f"Fast-pass evaluation  #{fp_id}",
            f"Applicant: {member.mention}\nScore each category from **1** (very unfamiliar) to **5** (very confident).\nUse the page buttons, then submit notes.",
            color=COLOR_GOLD,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @fastpass.command(name="review", description="Review pending or recent fast-pass evaluations.")
    @has_level(PermissionLevel.HR)
    async def review(self, interaction: discord.Interaction, evaluation_id: int | None = None) -> None:
        if not interaction.guild:
            return
        if evaluation_id:
            row = await self.bot.db.get_fastpass(evaluation_id)
            if row is None:
                await interaction.response.send_message(embed=error_embed("Not found"), ephemeral=True)
                return
            await interaction.response.send_message(embed=_fastpass_embed(interaction.guild, row), ephemeral=True)
            return
        rows = await self.bot.db.list_fastpass(interaction.guild.id)
        embed = base_embed("Fast-pass reviews")
        if not rows:
            embed.description = "No evaluations on file."
        else:
            embed.description = "\n".join(
                f"`#{r['id']}` {mention_or_id(interaction.guild, r['applicant_id'])} — `{r['status']}` avg `{r['average'] or '—'}`"
                for r in rows[:15]
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @fastpass.command(name="approve", description="Approve a completed fast-pass evaluation and apply the recommendation.")
    @has_level(PermissionLevel.HR)
    async def approve(self, interaction: discord.Interaction, evaluation_id: int) -> None:
        await self._decide(interaction, evaluation_id, "approved")

    @fastpass.command(name="deny", description="Deny a fast-pass evaluation.")
    @has_level(PermissionLevel.HR)
    async def deny(self, interaction: discord.Interaction, evaluation_id: int, reason: str) -> None:
        await self._decide(interaction, evaluation_id, "denied", reason)

    async def _decide(self, interaction: discord.Interaction, evaluation_id: int, status: str, reason: str | None = None) -> None:
        if not interaction.guild:
            return
        row = await self.bot.db.get_fastpass(evaluation_id)
        if row is None or str(row["guild_id"]) != str(interaction.guild.id):
            await interaction.response.send_message(embed=error_embed("Not found"), ephemeral=True)
            return
        if not row["scores_json"] or not row["recommendation"]:
            await interaction.response.send_message(
                embed=error_embed("Incomplete", "Finish scoring and choose a recommendation before approving."),
                ephemeral=True,
            )
            return
        await self.bot.db.update_fastpass(evaluation_id, status=status, reviewed_at=now_ts(), reviewer_id=str(interaction.user.id))
        if reason:
            notes = (row["notes"] or "") + f"\nDecision: {reason}"
            await self.bot.db.update_fastpass(evaluation_id, notes=notes)
        applicant_id = int(row["applicant_id"])
        personnel = await self.bot.db.get_personnel(interaction.guild.id, applicant_id)
        rec = row["recommendation"]
        if status == "approved" and personnel:
            if rec == "waived":
                await self.bot.db.update_personnel(personnel["id"], training_status="waived", supervision_status="required")
                cfg = await self.bot.guild_config(interaction.guild.id)
                modules = cfg.get("training", "required_modules") or []
                for module in modules:
                    await self.bot.db.upsert_training(interaction.guild.id, applicant_id, module, "waived", interaction.user.id, "Fast-pass waived")
            elif rec == "partial_training":
                await self.bot.db.update_personnel(personnel["id"], training_status="partial", supervision_status="required")
            elif rec == "full_training":
                await self.bot.db.update_personnel(personnel["id"], training_status="required", supervision_status="none")
            elif rec == "additional_eval":
                await self.bot.db.update_personnel(personnel["id"], training_status="pending", supervision_status="required")
        await self.bot.db.audit(
            interaction.guild.id, f"fastpass_{status}", actor_id=interaction.user.id, actor_name=str(interaction.user),
            target_id=applicant_id, details=f"#{evaluation_id} {rec} {reason or ''}",
        )
        label = FASTPASS_RECOMMENDATIONS.get(rec, rec)
        public = base_embed(
            f"Fast-pass {status}",
            f"{mention_or_id(interaction.guild, applicant_id)}  •  **{label}**\nAverage **{row['average']}** / 5",
            color=COLOR_GOLD,
        )
        await interaction.response.send_message(embed=success_embed(f"Fast-pass {status}", f"Evaluation `#{evaluation_id}`."), ephemeral=True)
        await self.bot.notify(interaction.guild, "fastpass", public)
        await self.bot.notify(interaction.guild, "notifications", public)
        await self.bot.notify(interaction.guild, "applications", public)


def _fastpass_embed(guild: discord.Guild, row) -> discord.Embed:
    scores = json.loads(row["scores_json"] or "{}")
    rec = FASTPASS_RECOMMENDATIONS.get(row["recommendation"] or "", row["recommendation"] or "Pending")
    embed = base_embed(f"Fast-pass  #{row['id']}", f"Applicant {mention_or_id(guild, row['applicant_id'])}")
    add_fields(
        embed,
        [
            ("Status", row["status"], True),
            ("Average", f"{row['average']}/5" if row["average"] is not None else "—", True),
            ("Recommendation", rec, True),
            ("Reviewer", mention_or_id(guild, row["reviewer_id"]), True),
            ("Created", ts(row["created_at"]), True),
        ],
    )
    if scores:
        embed.add_field(
            name="Scores",
            value="\n".join(f"**{k}** — {v} ({FASTPASS_SCALE.get(int(v), '')})" for k, v in scores.items())[:1024],
            inline=False,
        )
    if row["notes"]:
        embed.add_field(name="Notes", value=row["notes"][:1024], inline=False)
    return embed


class FastPassScoreView(discord.ui.View):
    def __init__(self, fp_id: int, applicant_id: int, page: int = 0, scores: dict[str, int] | None = None) -> None:
        super().__init__(timeout=600)
        self.fp_id = fp_id
        self.applicant_id = applicant_id
        self.page = page
        self.scores = scores or {}
        chunk = FASTPASS_CATEGORIES[page * 4 : page * 4 + 4]
        for category in chunk:
            self.add_item(CategoryScoreSelect(category, self.scores.get(category)))
        if page > 0:
            self.add_item(PageButton("Previous", page - 1))
        if page * 4 + 4 < len(FASTPASS_CATEGORIES):
            self.add_item(PageButton("Next categories", page + 1))
        else:
            self.add_item(NotesButton())
            self.add_item(RecommendSelect())


class CategoryScoreSelect(discord.ui.Select):
    def __init__(self, category: str, current: int | None) -> None:
        self.category = category
        options = [
            discord.SelectOption(
                label=f"{n} — {FASTPASS_SCALE[n]}",
                value=str(n),
                default=current == n,
            )
            for n in range(1, 6)
        ]
        super().__init__(placeholder=category, options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: FastPassScoreView = self.view  # type: ignore[assignment]
        view.scores[self.category] = int(self.values[0])
        await interaction.response.defer()


class PageButton(discord.ui.Button):
    def __init__(self, label: str, page: int) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        view: FastPassScoreView = self.view  # type: ignore[assignment]
        new = FastPassScoreView(view.fp_id, view.applicant_id, self.page, view.scores)
        await interaction.response.edit_message(view=new)


class NotesButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Add notes", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: FastPassScoreView = self.view  # type: ignore[assignment]
        await interaction.response.send_modal(FastPassNotesModal(view))


class FastPassNotesModal(discord.ui.Modal, title="Fast-pass notes"):
    notes = discord.ui.TextInput(label="Evaluator notes", style=discord.TextStyle.paragraph, max_length=1000, required=False)

    def __init__(self, parent: FastPassScoreView) -> None:
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        await bot.db.update_fastpass(self.parent.fp_id, notes=str(self.notes.value or ""))
        await interaction.response.send_message(embed=success_embed("Notes saved"), ephemeral=True)


class RecommendSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label=label, value=key)
            for key, label in FASTPASS_RECOMMENDATIONS.items()
        ]
        super().__init__(placeholder="Select recommendation and save scores…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: FastPassScoreView = self.view  # type: ignore[assignment]
        if len(view.scores) < len(FASTPASS_CATEGORIES):
            missing = [c for c in FASTPASS_CATEGORIES if c not in view.scores]
            await interaction.response.send_message(
                embed=error_embed("Incomplete scores", "Still need: " + ", ".join(missing[:5])),
                ephemeral=True,
            )
            return
        avg = round(sum(view.scores.values()) / len(view.scores), 2)
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        await bot.db.update_fastpass(
            view.fp_id,
            scores_json=json.dumps(view.scores),
            average=avg,
            recommendation=self.values[0],
            status="pending_decision",
            reviewer_id=str(interaction.user.id),
        )
        rec = FASTPASS_RECOMMENDATIONS[self.values[0]]
        await interaction.response.send_message(
            embed=success_embed(
                "Evaluation saved",
                f"Average **{avg} / 5**. Recommendation: **{rec}**.\nUse `/fastpass approve evaluation_id:{view.fp_id}` or `/fastpass deny`.",
            ),
            ephemeral=True,
        )


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(FastPass(bot))
