"""Owner setup: one-question-at-a-time ID wizard. Never creates Discord objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_NAVY
from wsp.embeds import add_fields, base_embed, error_embed, success_embed, warning_embed
from wsp.permissions import is_owner

if TYPE_CHECKING:
    from wsp.bot import WSPBot

ROLE_SPECS = [
    ("wsp", "Department membership"),
    ("hr", "Human Resources"),
    ("command", "Command staff"),
    ("supervisor", "Field supervision"),
    ("superintendent", "Department head"),
    ("on_duty", "On duty"),
    ("high_rank", "High ranking (Lieutenant and above)"),
    ("middle_rank", "Middle ranking (Sergeant)"),
    ("low_rank", "Low ranking"),
    ("shift_certified", "Required to start a shift"),
]

RANK_BIND = [
    ("probationary_trooper", "Probationary Trooper"),
    ("trooper", "Trooper"),
    ("senior_trooper", "Senior Trooper"),
    ("master_trooper", "Master Trooper"),
    ("sergeant", "Sergeant"),
    ("lieutenant", "Lieutenant"),
    ("captain", "Captain"),
    ("major", "Major"),
    ("colonel", "Colonel"),
    ("superintendent_rank", "Superintendent"),
]

CHANNEL_SPECS = [
    ("promotions", "Promotion and demotion notices"),
    ("loa", "Leave of absence requests"),
    ("quota", "Quota reminders and reports"),
    ("shift_log", "Shift start, pause, and end logs"),
    ("notifications", "Department notifications"),
    ("hr_log", "HR action log"),
    ("command_log", "Command action log"),
    ("audit_log", "Full audit trail"),
]

CATEGORY_SPECS = [
    ("logs", "Log channels"),
    ("command", "Command channels"),
]


@dataclass(frozen=True)
class WizardStep:
    kind: str
    key: str
    question: str


WIZARD_STEPS: list[WizardStep] = [
    WizardStep("role", "wsp", "Which role is WSP membership?"),
    WizardStep("role", "hr", "Which role is HR?"),
    WizardStep("role", "command", "Which role is Command?"),
    WizardStep("role", "supervisor", "Which role is Supervisor?"),
    WizardStep("role", "superintendent", "Which role is Superintendent?"),
    WizardStep("role", "on_duty", "Which role is given while on duty?"),
    WizardStep("role", "high_rank", "Which role is High Rank (Lieutenant and above)?"),
    WizardStep("role", "middle_rank", "Which role is Middle Rank (Sergeant)?"),
    WizardStep("role", "low_rank", "Which role is Low Rank?"),
    WizardStep("role", "shift_certified", "Which role is required to start a shift?"),
    *[WizardStep("rank", name, f"Which role is {name}?") for _param, name in RANK_BIND],
    WizardStep("category", "logs", "Which category should log channels live in?"),
    WizardStep("category", "command", "Which category should command channels live in?"),
    WizardStep("channel", "audit_log", "Where do you want audit logs to go?"),
    WizardStep("channel", "command_log", "Where do you want command logs to go?"),
    WizardStep("channel", "shift_log", "Where do you want shift logs to go?"),
    WizardStep("channel", "hr_log", "Where do you want HR logs to go?"),
    WizardStep("channel", "notifications", "Where do you want department notifications to go?"),
    WizardStep("channel", "promotions", "Where do you want promotion notices to go?"),
    WizardStep("channel", "loa", "Where do you want LOA requests to go?"),
    WizardStep("channel", "quota", "Where do you want quota reports to go?"),
]


class Setup(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    @app_commands.command(name="setupserver", description="Set role and channel IDs.")
    @is_owner()
    async def setupserver(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        cfg = await self.bot.guild_config(guild.id)
        cfg.set_path(["guild_id"], str(guild.id))
        await self.bot.save_config(guild.id, cfg)
        step = _first_missing_step(cfg)
        await _show_step(self.bot, interaction, step, edit=False)

    @app_commands.command(name="verifysetup", description="Check setup.")
    @is_owner()
    async def verifysetup(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        cfg = await self.bot.guild_config(guild.id)
        missing_cfg = cfg.missing_required()
        missing_discord: list[str] = []
        for key, _label in ROLE_SPECS:
            rid = cfg.role_id(key)
            if rid and guild.get_role(rid) is None:
                missing_discord.append(f"role {key} (`{rid}`)")
        for _param, name in RANK_BIND:
            rid = cfg.rank_role_id(name)
            if rid and guild.get_role(rid) is None:
                missing_discord.append(f"rank {name} (`{rid}`)")
        for key, _topic in CHANNEL_SPECS:
            cid = cfg.channel_id(key)
            if cid and guild.get_channel(cid) is None:
                missing_discord.append(f"channel {key} (`{cid}`)")
        for key, _label in CATEGORY_SPECS:
            cid = cfg.category_id(key)
            if cid and guild.get_channel(cid) is None:
                missing_discord.append(f"category {key} (`{cid}`)")
        for rid in cfg.fire_role_ids():
            if guild.get_role(rid) is None:
                missing_discord.append(f"fire extra role (`{rid}`)")
        tables = await self.bot.db.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        table_names = [r["name"] for r in tables]
        required_tables = ["personnel", "shifts", "audit_log", "loa_requests"]
        missing_tables = [t for t in required_tables if t not in table_names]
        ok = not missing_cfg and not missing_discord and not missing_tables
        embed = success_embed("Setup verification", "Setup is complete.") if ok else warning_embed(
            "Setup incomplete",
        )
        add_fields(
            embed,
            [
                ("Unset IDs", _clip(missing_cfg), False),
                ("IDs not found in this server", _clip(missing_discord), False),
                ("Missing tables", ", ".join(missing_tables) or "None", False),
                ("Tables present", str(len(table_names)), True),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="config", description="View or change settings.")
    @is_owner()
    @app_commands.describe(path="Setting name", value="New value")
    async def config(self, interaction: discord.Interaction, path: str | None = None, value: str | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        cfg = await self.bot.guild_config(interaction.guild.id)
        if not path:
            embed = base_embed("Department configuration", "Current settings.")
            roles = cfg.get("roles") or {}
            channels = cfg.get("channels") or {}
            embed.add_field(name="Roles", value="\n".join(f"`{k}` → `{v or 'unset'}`" for k, v in roles.items()) or "—", inline=True)
            ch_preview = list(channels.items())[:10]
            embed.add_field(name="Channels", value="\n".join(f"`{k}` → `{v or 'unset'}`" for k, v in ch_preview) or "—", inline=True)
            embed.add_field(
                name="Quota",
                value=(
                    f"LR `{cfg.get('quota', 'low_minutes') or 90}`  •  "
                    f"MR `{cfg.get('quota', 'middle_minutes') or 75}`  •  "
                    f"HR `{cfg.get('quota', 'high_minutes') or 30}` min/week"
                ),
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        keys = [p for p in path.split(".") if p]
        if value is None:
            current = cfg.get(*keys, default="(unset)")
            await interaction.response.send_message(embed=base_embed("Config value", f"`{path}` = `{current}`"), ephemeral=True)
            return
        if keys[0] in {"roles", "channels", "categories", "rank_roles", "guild_id"}:
            parsed_id = parse_snowflake(value)
            if not parsed_id:
                await interaction.response.send_message(embed=error_embed("Invalid ID"), ephemeral=True)
                return
            cfg.set_path(keys, str(parsed_id))
            display = str(parsed_id)
        else:
            parsed: str | int = int(value) if value.isdigit() else value
            cfg.set_path(keys, parsed)
            display = str(parsed)
        await self.bot.save_config(interaction.guild.id, cfg)
        await self.bot.db.audit(
            interaction.guild.id,
            "config",
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
            details=f"{path} = {display}",
        )
        await interaction.response.send_message(embed=success_embed("Configuration updated", f"`{path}` set to `{display}`."), ephemeral=True)

    @app_commands.command(name="sync", description="Sync commands.")
    @is_owner()
    async def sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild:
            self.bot.tree.copy_global_to(guild=interaction.guild)
            synced = await self.bot.tree.sync(guild=interaction.guild)
        else:
            synced = await self.bot.tree.sync()
        await interaction.followup.send(embed=success_embed("Commands synced", f"{len(synced)} commands published."), ephemeral=True)


async def _show_step(bot: WSPBot, interaction: discord.Interaction, step_index: int, *, edit: bool) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
        return
    if step_index >= len(WIZARD_STEPS):
        await _finish_wizard(bot, interaction, edit=edit)
        return
    cfg = await bot.guild_config(guild.id)
    step = WIZARD_STEPS[step_index]
    embed = await _step_embed(guild, cfg, step_index)
    view = SetupWizardView(step_index)
    if edit:
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def _step_embed(guild: discord.Guild, cfg, step_index: int) -> discord.Embed:
    step = WIZARD_STEPS[step_index]
    current = _format_current(guild, cfg, step)
    kind_label = {"role": "role ID", "rank": "role ID", "channel": "channel ID", "category": "category ID"}[step.kind]
    embed = base_embed(
        f"Setup  •  {step_index + 1} of {len(WIZARD_STEPS)}",
        f"**{step.question}**\n\nEnter the {kind_label}, then press **Enter ID**.",
        color=COLOR_NAVY,
    )
    embed.add_field(name="Currently", value=current, inline=False)
    embed.set_footer(text="Skip keeps the current value.")
    return embed


async def _finish_wizard(bot: WSPBot, interaction: discord.Interaction, *, edit: bool) -> None:
    guild = interaction.guild
    if guild is None:
        return
    cfg = await bot.guild_config(guild.id)
    missing = cfg.missing_required()
    embed = success_embed(
        "Setup complete",
        "All questions are done. The bot will use the IDs you entered.",
    )
    if missing:
        embed = warning_embed(
            "Setup finished with skips",
            "Some IDs were skipped. Run `/setupserver` again to fill them in, or `/verifysetup` to see what is missing.",
        )
        embed.add_field(name="Still unset", value=_clip(missing), inline=False)
    view = None
    if edit:
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


class SetupWizardView(discord.ui.View):
    def __init__(self, step_index: int) -> None:
        super().__init__(timeout=900)
        self.step_index = step_index
        if step_index <= 0:
            self.back.disabled = True

    @discord.ui.button(label="Enter ID", style=discord.ButtonStyle.primary)
    async def enter_id(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        step = WIZARD_STEPS[self.step_index]
        await interaction.response.send_modal(SetupIdModal(self.step_index, step))

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        await _show_step(bot, interaction, self.step_index + 1, edit=True)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        await _show_step(bot, interaction, max(0, self.step_index - 1), edit=True)


class SetupIdModal(discord.ui.Modal, title="Enter Discord ID"):
    snowflake = discord.ui.TextInput(
        label="ID",
        placeholder="123456789012345678",
        min_length=5,
        max_length=80,
    )

    def __init__(self, step_index: int, step: WizardStep) -> None:
        super().__init__()
        kind_label = {"role": "Role ID", "rank": "Role ID", "channel": "Channel ID", "category": "Category ID"}[step.kind]
        self.snowflake.label = kind_label
        self.step_index = step_index
        self.step = step

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        snowflake = parse_snowflake(str(self.snowflake.value))
        if not snowflake:
            await interaction.response.send_message(
                embed=error_embed("Invalid ID"),
                ephemeral=True,
            )
            return
        resolved = await _resolve_id(guild, self.step.kind, snowflake)
        if resolved is None:
            kind = {"role": "role", "rank": "role", "channel": "text channel", "category": "category"}[self.step.kind]
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No {kind} with ID `{snowflake}` in this server."),
                ephemeral=True,
            )
            return
        cfg = await bot.guild_config(guild.id)
        cfg.set_path(["guild_id"], str(guild.id))
        path = _config_path(self.step)
        cfg.set_path(path, str(snowflake))
        if self.step.kind == "rank":
            await bot.db.set_rank_role(guild.id, self.step.key, snowflake)
        await bot.db.ensure_ranks(guild.id, cfg)
        await bot.save_config(guild.id, cfg)
        await bot.db.audit(
            guild.id,
            "setup_bind",
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
            details=f"{'.'.join(path)} = {snowflake}",
        )
        await _show_step(bot, interaction, self.step_index + 1, edit=True)


def _config_path(step: WizardStep) -> list[str]:
    if step.kind == "role":
        return ["roles", step.key]
    if step.kind == "rank":
        return ["rank_roles", step.key]
    if step.kind == "channel":
        return ["channels", step.key]
    return ["categories", step.key]


def _stored_id(cfg, step: WizardStep) -> int:
    if step.kind == "role":
        return cfg.role_id(step.key)
    if step.kind == "rank":
        return cfg.rank_role_id(step.key)
    if step.kind == "channel":
        return cfg.channel_id(step.key)
    return cfg.category_id(step.key)


def _first_missing_step(cfg) -> int:
    for index, step in enumerate(WIZARD_STEPS):
        if not _stored_id(cfg, step):
            return index
    return 0


def _format_current(guild: discord.Guild, cfg, step: WizardStep) -> str:
    sid = _stored_id(cfg, step)
    if not sid:
        return "Not set yet"
    if step.kind in {"role", "rank"}:
        role = guild.get_role(sid)
        return f"{role.mention} `{sid}`" if role else f"`{sid}` (not found in this server)"
    channel = guild.get_channel(sid)
    if channel is not None:
        return f"{channel.mention} `{sid}`"
    return f"`{sid}` (not found in this server)"


async def _resolve_id(guild: discord.Guild, kind: str, snowflake: int) -> discord.abc.Snowflake | None:
    if kind in {"role", "rank"}:
        return guild.get_role(snowflake)
    obj = guild.get_channel(snowflake)
    if obj is None:
        try:
            obj = await guild.fetch_channel(snowflake)
        except discord.HTTPException:
            return None
    if kind == "channel" and isinstance(obj, discord.TextChannel):
        return obj
    if kind == "category" and isinstance(obj, discord.CategoryChannel):
        return obj
    return None


def parse_snowflake(raw: str) -> int:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if not digits:
        return 0
    try:
        return int(digits)
    except ValueError:
        return 0


def _clip(items: list[str]) -> str:
    if not items:
        return "None"
    text = "\n".join(f"`{item}`" if not item.startswith("`") else item for item in items)
    if len(text) <= 1024:
        return text
    kept: list[str] = []
    used = 0
    for item in items:
        line = f"`{item}`"
        if used + len(line) + 1 > 980:
            kept.append(f"… +{len(items) - len(kept)} more")
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Setup(bot))
