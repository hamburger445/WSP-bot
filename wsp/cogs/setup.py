"""Owner setup: bind existing Discord role and channel IDs. Never creates them."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_GOLD, COLOR_NAVY, PermissionLevel
from wsp.embeds import add_fields, base_embed, error_embed, success_embed, warning_embed
from wsp.permissions import has_level, is_owner

if TYPE_CHECKING:
    from wsp.bot import WSPBot

ROLE_SPECS = [
    ("wsp", "Department membership"),
    ("hr", "Human Resources"),
    ("command", "Command staff"),
    ("supervisor", "Field supervision"),
    ("superintendent", "Department head"),
]

RANK_BIND = [
    ("probationary_trooper", "Probationary Trooper"),
    ("trooper", "Trooper"),
    ("senior_trooper", "Senior Trooper"),
    ("master_trooper", "Master Trooper"),
    ("corporal", "Corporal"),
    ("sergeant", "Sergeant"),
    ("lieutenant", "Lieutenant"),
    ("captain", "Captain"),
    ("major", "Major"),
    ("lieutenant_colonel", "Lieutenant Colonel"),
    ("colonel", "Colonel"),
    ("superintendent_rank", "Superintendent"),
]

CHANNEL_SPECS = [
    ("applications", "Fast-pass and application traffic"),
    ("promotions", "Promotion and demotion notices"),
    ("discipline", "Disciplinary actions"),
    ("loa", "Leave of absence requests"),
    ("quota", "Quota reminders and reports"),
    ("notifications", "Department notifications"),
    ("hr_log", "HR action log"),
    ("command_log", "Command action log"),
    ("audit_log", "Full audit trail"),
    ("fastpass", "Fast-pass evaluations"),
    ("supervision", "Ride-along / supervision"),
    ("probation", "Probationary period tracking"),
    ("tickets_log", "Ticket transcripts and closures"),
    ("ticket_panel", "Public ticket panel"),
    ("resignations", "Resignation notices"),
]

CATEGORY_SPECS = [
    ("logs", "Log channels"),
    ("command", "Command channels"),
    ("tickets", "Ticket channels are opened in this category"),
]

BIND_KEYS = {
    "role": [key for key, _ in ROLE_SPECS],
    "rank": [name for _, name in RANK_BIND],
    "channel": [key for key, _ in CHANNEL_SPECS],
    "category": [key for key, _ in CATEGORY_SPECS],
}


class Setup(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    setup_group = app_commands.Group(
        name="setup",
        description="Bind existing Discord roles and channels by ID. Does not create them.",
    )

    @setup_group.command(name="menu", description="See which role and channel IDs still need to be bound.")
    @has_level(PermissionLevel.COMMAND)
    async def menu(self, interaction: discord.Interaction) -> None:
        await _send_setup_menu(self.bot, interaction)

    @setup_group.command(name="roles", description="Bind existing department roles. Does not create roles.")
    @has_level(PermissionLevel.SUPERINTENDENT)
    async def roles(
        self,
        interaction: discord.Interaction,
        wsp: discord.Role | None = None,
        hr: discord.Role | None = None,
        command: discord.Role | None = None,
        supervisor: discord.Role | None = None,
        superintendent: discord.Role | None = None,
    ) -> None:
        chosen = {
            "wsp": wsp,
            "hr": hr,
            "command": command,
            "supervisor": supervisor,
            "superintendent": superintendent,
        }
        await _bind_roles(self.bot, interaction, chosen)

    @setup_group.command(name="ranks", description="Bind existing rank roles. Does not create roles.")
    @has_level(PermissionLevel.SUPERINTENDENT)
    async def ranks(
        self,
        interaction: discord.Interaction,
        probationary_trooper: discord.Role | None = None,
        trooper: discord.Role | None = None,
        senior_trooper: discord.Role | None = None,
        master_trooper: discord.Role | None = None,
        corporal: discord.Role | None = None,
        sergeant: discord.Role | None = None,
        lieutenant: discord.Role | None = None,
        captain: discord.Role | None = None,
        major: discord.Role | None = None,
        lieutenant_colonel: discord.Role | None = None,
        colonel: discord.Role | None = None,
        superintendent_rank: discord.Role | None = None,
    ) -> None:
        chosen = {
            "Probationary Trooper": probationary_trooper,
            "Trooper": trooper,
            "Senior Trooper": senior_trooper,
            "Master Trooper": master_trooper,
            "Corporal": corporal,
            "Sergeant": sergeant,
            "Lieutenant": lieutenant,
            "Captain": captain,
            "Major": major,
            "Lieutenant Colonel": lieutenant_colonel,
            "Colonel": colonel,
            "Superintendent": superintendent_rank,
        }
        await _bind_rank_roles(self.bot, interaction, chosen)

    @setup_group.command(name="channels", description="Bind existing text channels. Does not create channels.")
    @has_level(PermissionLevel.SUPERINTENDENT)
    async def channels(
        self,
        interaction: discord.Interaction,
        applications: discord.TextChannel | None = None,
        promotions: discord.TextChannel | None = None,
        discipline: discord.TextChannel | None = None,
        loa: discord.TextChannel | None = None,
        quota: discord.TextChannel | None = None,
        notifications: discord.TextChannel | None = None,
        hr_log: discord.TextChannel | None = None,
        command_log: discord.TextChannel | None = None,
        audit_log: discord.TextChannel | None = None,
        fastpass: discord.TextChannel | None = None,
        supervision: discord.TextChannel | None = None,
        probation: discord.TextChannel | None = None,
        tickets_log: discord.TextChannel | None = None,
        ticket_panel: discord.TextChannel | None = None,
        resignations: discord.TextChannel | None = None,
    ) -> None:
        chosen = {
            "applications": applications,
            "promotions": promotions,
            "discipline": discipline,
            "loa": loa,
            "quota": quota,
            "notifications": notifications,
            "hr_log": hr_log,
            "command_log": command_log,
            "audit_log": audit_log,
            "fastpass": fastpass,
            "supervision": supervision,
            "probation": probation,
            "tickets_log": tickets_log,
            "ticket_panel": ticket_panel,
            "resignations": resignations,
        }
        await _bind_channels(self.bot, interaction, chosen)

    @setup_group.command(name="categories", description="Bind existing categories. Does not create categories.")
    @has_level(PermissionLevel.SUPERINTENDENT)
    async def categories(
        self,
        interaction: discord.Interaction,
        logs: discord.CategoryChannel | None = None,
        command: discord.CategoryChannel | None = None,
        tickets: discord.CategoryChannel | None = None,
    ) -> None:
        chosen = {"logs": logs, "command": command, "tickets": tickets}
        await _bind_categories(self.bot, interaction, chosen)

    @setup_group.command(name="bind", description="Paste a Discord ID for one role, rank, channel, or category.")
    @has_level(PermissionLevel.SUPERINTENDENT)
    @app_commands.describe(kind="What this ID is for", key="Config key (use autocomplete)", snowflake="Discord ID, mention, or copied snowflake")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="Department role", value="role"),
            app_commands.Choice(name="Rank role", value="rank"),
            app_commands.Choice(name="Channel", value="channel"),
            app_commands.Choice(name="Category", value="category"),
        ]
    )
    async def bind(self, interaction: discord.Interaction, kind: app_commands.Choice[str], key: str, snowflake: str) -> None:
        await _bind_snowflake(self.bot, interaction, kind.value, key, snowflake)

    @bind.autocomplete("key")
    async def bind_key_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        kind_raw = getattr(interaction.namespace, "kind", None) if interaction.namespace else None
        if isinstance(kind_raw, app_commands.Choice):
            kind = str(kind_raw.value)
        else:
            kind = str(kind_raw or "")
        keys = BIND_KEYS.get(kind, [])
        needle = current.lower()
        matches = [k for k in keys if needle in k.lower()] or keys
        return [app_commands.Choice(name=k, value=k) for k in matches[:25]]

    @setup_group.command(name="panel", description="Post the ticket panel in the bound ticket-panel channel.")
    @has_level(PermissionLevel.SUPERINTENDENT)
    async def panel(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        cfg = await self.bot.guild_config(guild.id)
        channel = guild.get_channel(cfg.channel_id("ticket_panel"))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                embed=error_embed("Ticket panel channel not bound", "Bind it first with `/setup channels` or `/setup bind`."),
                ephemeral=True,
            )
            return
        from wsp.views.tickets import TicketPanelView

        embed = base_embed(
            "Wisconsin State Patrol  •  Assistance Desk",
            "Select a request type below. A private ticket channel will be created for you and HR/Command.",
            color=COLOR_GOLD,
        )
        await channel.send(embed=embed, view=TicketPanelView())
        await interaction.response.send_message(
            embed=success_embed("Ticket panel posted", f"Posted in {channel.mention}."),
            ephemeral=True,
        )

    @app_commands.command(name="setupserver", description="Open setup. Bind existing IDs — nothing is created.")
    @is_owner()
    async def setupserver(self, interaction: discord.Interaction) -> None:
        await _send_setup_menu(self.bot, interaction)

    @app_commands.command(name="verifysetup", description="Check that required role and channel IDs are bound and exist.")
    @has_level(PermissionLevel.COMMAND)
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
        tables = await self.bot.db.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        table_names = [r["name"] for r in tables]
        required_tables = ["personnel", "shifts", "audit_log", "tickets", "loa_requests", "discipline"]
        missing_tables = [t for t in required_tables if t not in table_names]
        ok = not missing_cfg and not missing_discord and not missing_tables
        embed = success_embed("Setup verification", "All required IDs are bound and exist.") if ok else warning_embed(
            "Setup incomplete",
            "Bind missing IDs with `/setup roles`, `/setup ranks`, `/setup channels`, `/setup categories`, or `/setup bind`.",
        )
        add_fields(
            embed,
            [
                ("Unbound IDs", _clip(missing_cfg), False),
                ("IDs not found in this server", _clip(missing_discord), False),
                ("Missing tables", ", ".join(missing_tables) or "None", False),
                ("Tables present", str(len(table_names)), True),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="config", description="View or update department configuration keys.")
    @has_level(PermissionLevel.SUPERINTENDENT)
    @app_commands.describe(path="Dot path such as channels.audit_log or quota.weekly_minutes", value="New value (omit to view)")
    async def config(self, interaction: discord.Interaction, path: str | None = None, value: str | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        cfg = await self.bot.guild_config(interaction.guild.id)
        if not path:
            embed = base_embed("Department configuration", "Current IDs. Use `/setup bind` or `/config path:channels.hr_log value:123`.")
            roles = cfg.get("roles") or {}
            channels = cfg.get("channels") or {}
            embed.add_field(name="Roles", value="\n".join(f"`{k}` → `{v or 'unset'}`" for k, v in roles.items()) or "—", inline=True)
            ch_preview = list(channels.items())[:10]
            embed.add_field(name="Channels", value="\n".join(f"`{k}` → `{v or 'unset'}`" for k, v in ch_preview) or "—", inline=True)
            embed.add_field(
                name="Quota / probation",
                value=f"Duty `{cfg.get('quota', 'weekly_minutes')}` min/week\nHR supervision `{cfg.get('quota', 'hr_supervision_minutes')}` min\nProbation `{cfg.get('probation', 'duration_days')}` days",
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
                await interaction.response.send_message(embed=error_embed("Invalid ID", "Paste a Discord snowflake ID."), ephemeral=True)
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

    @app_commands.command(name="sync", description="Re-sync slash commands to this guild (owner).")
    @is_owner()
    async def sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild:
            self.bot.tree.copy_global_to(guild=interaction.guild)
            synced = await self.bot.tree.sync(guild=interaction.guild)
        else:
            synced = await self.bot.tree.sync()
        await interaction.followup.send(embed=success_embed("Commands synced", f"{len(synced)} commands published."), ephemeral=True)

    @app_commands.command(name="resetserver", description="Clear stored WSP IDs for this guild. Discord roles and channels are kept.")
    @is_owner()
    @app_commands.describe(wipe_discord="If true, also delete the bound Discord channels (not recommended)")
    async def resetserver(self, interaction: discord.Interaction, wipe_discord: bool = False) -> None:
        view = ResetConfirmView(wipe_discord)
        await interaction.response.send_message(
            embed=warning_embed(
                "Confirm configuration reset",
                "This removes stored WSP IDs for this guild. Personnel records in the database are kept.\n"
                + (
                    "Bound Discord channels will also be deleted."
                    if wipe_discord
                    else "Your existing Discord roles and channels will be left in place."
                ),
            ),
            view=view,
            ephemeral=True,
        )


async def _send_setup_menu(bot: WSPBot, interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
        return
    cfg = await bot.guild_config(guild.id)
    missing = cfg.missing_required()
    embed = base_embed(
        "WSP department setup",
        "This bot does **not** create roles, channels, or categories. "
        "Pick existing ones, or paste IDs with `/setup bind`.\n\n"
        "`/setup roles`  `/setup ranks`  `/setup channels`  `/setup categories`  `/setup bind`  `/setup panel`",
        color=COLOR_NAVY,
    )
    embed.add_field(name="Department roles", value=_role_status(guild, cfg), inline=False)
    embed.add_field(name="Rank roles", value=_rank_status(cfg), inline=False)
    embed.add_field(name="Channels", value=_channel_status(guild, cfg), inline=False)
    embed.add_field(name="Categories", value=_category_status(guild, cfg), inline=False)
    embed.add_field(name="Still unbound", value=_clip(missing), inline=False)
    await interaction.response.send_message(embed=embed, view=SetupNavView(), ephemeral=True)


async def _bind_roles(bot: WSPBot, interaction: discord.Interaction, chosen: dict[str, discord.Role | None]) -> None:
    updates = {key: role for key, role in chosen.items() if role is not None}
    if not updates:
        await _send_setup_menu(bot, interaction)
        return
    cfg = await _prepare_cfg(bot, interaction)
    if cfg is None:
        return
    lines = []
    for key, role in updates.items():
        cfg.set_path(["roles", key], str(role.id))
        lines.append(f"`roles.{key}` → {role.mention} `{role.id}`")
    await _finish_bind(bot, interaction, cfg, lines)


async def _bind_rank_roles(bot: WSPBot, interaction: discord.Interaction, chosen: dict[str, discord.Role | None]) -> None:
    updates = {name: role for name, role in chosen.items() if role is not None}
    if not updates:
        await _send_setup_menu(bot, interaction)
        return
    cfg = await _prepare_cfg(bot, interaction)
    if cfg is None:
        return
    guild = interaction.guild
    assert guild is not None
    lines = []
    for name, role in updates.items():
        cfg.set_path(["rank_roles", name], str(role.id))
        await bot.db.set_rank_role(guild.id, name, role.id)
        lines.append(f"`rank_roles.{name}` → {role.mention} `{role.id}`")
    await _finish_bind(bot, interaction, cfg, lines)


async def _bind_channels(bot: WSPBot, interaction: discord.Interaction, chosen: dict[str, discord.TextChannel | None]) -> None:
    updates = {key: channel for key, channel in chosen.items() if channel is not None}
    if not updates:
        await _send_setup_menu(bot, interaction)
        return
    cfg = await _prepare_cfg(bot, interaction)
    if cfg is None:
        return
    lines = []
    for key, channel in updates.items():
        cfg.set_path(["channels", key], str(channel.id))
        lines.append(f"`channels.{key}` → {channel.mention} `{channel.id}`")
    await _finish_bind(bot, interaction, cfg, lines)


async def _bind_categories(bot: WSPBot, interaction: discord.Interaction, chosen: dict[str, discord.CategoryChannel | None]) -> None:
    updates = {key: category for key, category in chosen.items() if category is not None}
    if not updates:
        await _send_setup_menu(bot, interaction)
        return
    cfg = await _prepare_cfg(bot, interaction)
    if cfg is None:
        return
    lines = []
    for key, category in updates.items():
        cfg.set_path(["categories", key], str(category.id))
        lines.append(f"`categories.{key}` → {category.mention} `{category.id}`")
    await _finish_bind(bot, interaction, cfg, lines)


async def _bind_snowflake(bot: WSPBot, interaction: discord.Interaction, kind: str, key: str, raw: str) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
        return
    allowed = BIND_KEYS.get(kind, [])
    if key not in allowed:
        await interaction.response.send_message(
            embed=error_embed("Unknown key", f"`{key}` is not a {kind} key. Use autocomplete."),
            ephemeral=True,
        )
        return
    snowflake = parse_snowflake(raw)
    if not snowflake:
        await interaction.response.send_message(embed=error_embed("Invalid ID", "Paste a Discord snowflake ID."), ephemeral=True)
        return
    if kind == "role":
        obj = guild.get_role(snowflake)
        if obj is None:
            await interaction.response.send_message(embed=error_embed("Role not found", f"No role `{snowflake}` in this server."), ephemeral=True)
            return
        await _bind_roles(bot, interaction, {key: obj})
        return
    if kind == "rank":
        obj = guild.get_role(snowflake)
        if obj is None:
            await interaction.response.send_message(embed=error_embed("Role not found", f"No role `{snowflake}` in this server."), ephemeral=True)
            return
        await _bind_rank_roles(bot, interaction, {key: obj})
        return
    obj = guild.get_channel(snowflake)
    if obj is None:
        try:
            obj = await guild.fetch_channel(snowflake)
        except discord.HTTPException:
            obj = None
    if kind == "channel":
        if not isinstance(obj, discord.TextChannel):
            await interaction.response.send_message(embed=error_embed("Channel not found", f"No text channel `{snowflake}` in this server."), ephemeral=True)
            return
        await _bind_channels(bot, interaction, {key: obj})
        return
    if not isinstance(obj, discord.CategoryChannel):
        await interaction.response.send_message(embed=error_embed("Category not found", f"No category `{snowflake}` in this server."), ephemeral=True)
        return
    await _bind_categories(bot, interaction, {key: obj})


async def _prepare_cfg(bot: WSPBot, interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
        return None
    cfg = await bot.guild_config(guild.id)
    cfg.set_path(["guild_id"], str(guild.id))
    return cfg


async def _finish_bind(bot: WSPBot, interaction: discord.Interaction, cfg, lines: list[str]) -> None:
    guild = interaction.guild
    assert guild is not None
    await bot.db.ensure_ranks(guild.id, cfg)
    await bot.save_config(guild.id, cfg)
    await bot.db.audit(
        guild.id,
        "setup_bind",
        actor_id=interaction.user.id,
        actor_name=str(interaction.user),
        details="; ".join(lines)[:1500],
    )
    remaining = cfg.missing_required()
    embed = success_embed("IDs bound", "\n".join(lines))
    if remaining:
        embed.add_field(name="Still unbound", value=_clip(remaining), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


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


def _rank_status(cfg) -> str:
    bound = 0
    missing: list[str] = []
    for _param, name in RANK_BIND:
        if cfg.rank_role_id(name):
            bound += 1
        else:
            missing.append(name)
    if not missing:
        return f"{bound}/12 bound"
    return f"{bound}/12 bound\nUnset: " + ", ".join(missing)


def _role_status(guild: discord.Guild, cfg) -> str:
    lines = []
    for key, _label in ROLE_SPECS:
        rid = cfg.role_id(key)
        role = guild.get_role(rid) if rid else None
        lines.append(f"`{key}` → {role.mention if role else ('`' + str(rid) + '` (missing)' if rid else 'unset')}")
    return "\n".join(lines)


def _channel_status(guild: discord.Guild, cfg) -> str:
    lines = []
    for key, _topic in CHANNEL_SPECS:
        cid = cfg.channel_id(key)
        channel = guild.get_channel(cid) if cid else None
        if isinstance(channel, discord.TextChannel):
            lines.append(f"`{key}` → {channel.mention}")
        elif cid:
            lines.append(f"`{key}` → `{cid}` (missing)")
        else:
            lines.append(f"`{key}` → unset")
    text = "\n".join(lines)
    return text[:1024]


def _category_status(guild: discord.Guild, cfg) -> str:
    lines = []
    for key, _label in CATEGORY_SPECS:
        cid = cfg.category_id(key)
        channel = guild.get_channel(cid) if cid else None
        if isinstance(channel, discord.CategoryChannel):
            lines.append(f"`{key}` → {channel.mention}")
        elif cid:
            lines.append(f"`{key}` → `{cid}` (missing)")
        else:
            lines.append(f"`{key}` → unset")
    return "\n".join(lines)


class SetupNavView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)

    @discord.ui.button(label="Verify now", style=discord.ButtonStyle.primary)
    async def verify(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        cog: Setup | None = interaction.client.get_cog("Setup")  # type: ignore[assignment]
        if cog:
            await cog.verifysetup.callback(cog, interaction)
        else:
            await interaction.response.send_message("Setup cog unavailable.", ephemeral=True)

    @discord.ui.button(label="Show config", style=discord.ButtonStyle.secondary)
    async def show_config(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        cog: Setup | None = interaction.client.get_cog("Setup")  # type: ignore[assignment]
        if cog:
            await cog.config.callback(cog, interaction, None, None)
        else:
            await interaction.response.send_message("Setup cog unavailable.", ephemeral=True)


class ResetConfirmView(discord.ui.View):
    def __init__(self, wipe_discord: bool) -> None:
        super().__init__(timeout=60)
        self.wipe_discord = wipe_discord

    @discord.ui.button(label="Confirm reset", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        bot: WSPBot = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        cfg = await bot.guild_config(guild.id)
        deleted: list[str] = []
        if self.wipe_discord:
            for cid in (cfg.get("channels") or {}).values():
                try:
                    ch = guild.get_channel(int(cid)) if cid else None
                except (TypeError, ValueError):
                    ch = None
                if ch:
                    try:
                        await ch.delete(reason="WSP resetserver")
                        deleted.append(getattr(ch, "name", str(ch.id)))
                    except discord.HTTPException:
                        pass
            for cat_id in (cfg.get("categories") or {}).values():
                try:
                    cat = guild.get_channel(int(cat_id)) if cat_id else None
                except (TypeError, ValueError):
                    cat = None
                if isinstance(cat, discord.CategoryChannel):
                    try:
                        await cat.delete(reason="WSP resetserver")
                    except discord.HTTPException:
                        pass
        await bot.db.execute("DELETE FROM guild_config WHERE guild_id = ?", (str(guild.id),))
        bot.invalidate_config(guild.id)
        await bot.db.audit(guild.id, "resetserver", actor_id=interaction.user.id, actor_name=str(interaction.user), details=f"wipe_discord={self.wipe_discord}")
        await interaction.response.edit_message(
            embed=success_embed("Configuration reset", "Stored IDs cleared." + (f" Deleted: {', '.join(deleted) or 'none'}." if self.wipe_discord else "")),
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=base_embed("Cancelled", "No changes made."), view=None)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Setup(bot))
