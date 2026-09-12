"""Wisconsin State Patrol Discord bot."""

from __future__ import annotations

import asyncio
import logging
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from wsp.config import GuildConfig, Settings
from wsp.constants import COLOR_DANGER, FOOTER
from wsp.db import Database
from wsp.permissions import InsufficientPermission
from wsp.utils import member_from_id, sync_duty_role

log = logging.getLogger("wsp.bot")

PUBLIC_SLASH = frozenset({
    "shift menu",
    "shift data",
    "shift status",
    "shift leaderboard",
    "ping",
})


class WSPCommandTree(app_commands.CommandTree):
    """Defer as soon as Discord delivers the command so the 3-second window is not missed."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.type != discord.InteractionType.application_command:
            return True
        if interaction.response.is_done():
            return True
        name = ""
        if interaction.command is not None:
            name = interaction.command.qualified_name
        try:
            await interaction.response.defer(ephemeral=name not in PUBLIC_SLASH)
        except discord.NotFound:
            log.warning("Ignored expired Discord interaction for %s", name or "unknown")
            return False
        except discord.HTTPException as exc:
            if getattr(exc, "code", None) not in {10062, 40060}:
                raise
            return interaction.response.is_done()
        return True

COG_MODULES = [
    "wsp.cogs.setup",
    "wsp.cogs.shifts",
    "wsp.cogs.quota",
    "wsp.cogs.loa",
    "wsp.cogs.promotions",
    "wsp.cogs.dashboard",
    "wsp.cogs.help",
    "wsp.cogs.prefix",
    "wsp.cogs.tasks",
    "wsp.cogs.academy",
]


class WSPBot(commands.Bot):
    def __init__(self, settings: Settings, db: Database) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = False
        intents.message_content = True
        super().__init__(
            command_prefix="?",
            intents=intents,
            help_command=None,
            case_insensitive=True,
            tree_cls=WSPCommandTree,
        )
        self.settings = settings
        self.db = db
        self._config_cache: dict[int, GuildConfig] = {}
        self.last_error: str | None = None
        self.synced_commands: list[str] = []
        self._synced = False
        self._ready_initialized = False

    async def setup_hook(self) -> None:
        from wsp.views.shifts import ShiftControlButton, ShiftMenuView
        from wsp.views.academy import AcademyStickyView
        from wsp.cogs.loa import DenyLOAButton, ApproveLOAButton

        self.add_view(ShiftMenuView())
        self.add_view(AcademyStickyView())
        self.add_dynamic_items(ShiftControlButton, ApproveLOAButton, DenyLOAButton)
        self.tree.on_error = self.on_app_command_error

        for module in COG_MODULES:
            await self.load_extension(module)
            log.info("Loaded cog %s", module)

        self._prepare_slash_menu()

    def _prepare_slash_menu(self) -> None:
        """Keep slash commands in guild / menus, not DMs or user-install apps."""
        for command in self.tree.walk_commands():
            desc = getattr(command, "description", None)
            if isinstance(desc, str) and len(desc) > 100:
                command.description = desc[:97].rstrip() + "..."
            command.guild_only = True
            try:
                command.allowed_contexts = app_commands.AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                )
                command.allowed_installs = app_commands.AppInstallationType(guild=True, user=False)
            except Exception:
                pass

    def _flatten_names(self, commands) -> list[str]:
        names: list[str] = []
        for cmd in commands:
            names.append(cmd.name)
            options = getattr(cmd, "options", None) or []
            for opt in options:
                if getattr(opt, "type", None) == discord.AppCommandOptionType.subcommand:
                    names.append(f"{cmd.name} {opt.name}")
                elif getattr(opt, "type", None) == discord.AppCommandOptionType.subcommand_group:
                    for child in getattr(opt, "options", []) or []:
                        names.append(f"{cmd.name} {opt.name} {child.name}")
        return names

    async def sync_app_commands(self) -> None:
        guild_ids: set[int] = {g.id for g in self.guilds}
        if self.settings.guild_id:
            guild_ids.add(self.settings.guild_id)
        if not guild_ids:
            synced = await self.tree.sync()
            self.synced_commands = self._flatten_names(synced)
            log.info("Synced %s global commands: %s", len(synced), self.synced_commands)
            return
        names: list[str] = []
        for gid in guild_ids:
            guild = discord.Object(id=gid)
            self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            names = self._flatten_names(synced)
            log.info("Synced %s root commands (%s menu entries) to guild %s: %s", len(synced), len(names), gid, names)
        self.synced_commands = names

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %s guild(s)", self.user, self.user.id if self.user else "?", len(self.guilds))
        self.last_error = None
        if self._ready_initialized:
            return
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Wisconsin State Patrol  •  LVRP",
            )
        )
        for guild in self.guilds:
            cfg = await self.guild_config(guild.id)
            if cfg.apply_published_structure():
                await self.save_config(guild.id, cfg)
            await self.db.ensure_ranks(guild.id, cfg)
            await self.db.repair_shift_records(guild.id, remap_all=len(self.guilds) == 1)
            for row in await self.db.list_active_shifts(guild.id):
                member = await member_from_id(self, guild, int(row["discord_id"]))
                if member:
                    await sync_duty_role(member, cfg, row["status"] == "active")
        if not self._synced:
            try:
                await self.sync_app_commands()
                self._synced = True
            except discord.Forbidden:
                self.last_error = "command sync forbidden — re-invite the bot with the applications.commands scope"
                log.exception(self.last_error)
            except Exception as exc:
                self.last_error = f"command sync failed: {exc}"
                log.exception("Command sync failed")
            self._ready_initialized = True

    async def guild_config(self, guild_id: int) -> GuildConfig:
        if guild_id in self._config_cache:
            return self._config_cache[guild_id]
        cfg = await self.db.load_guild_config(guild_id)
        if self.settings.guild_id and not cfg.guild_id():
            cfg.set_path(["guild_id"], str(self.settings.guild_id))
        if self.settings.timezone:
            cfg.set_path(["timezone"], self.settings.timezone)
        self._config_cache[guild_id] = cfg
        return cfg

    async def save_config(self, guild_id: int, cfg: GuildConfig) -> None:
        await self.db.save_guild_config(guild_id, cfg)
        self._config_cache[guild_id] = cfg

    def invalidate_config(self, guild_id: int) -> None:
        self._config_cache.pop(guild_id, None)

    async def notify(
        self,
        guild: discord.Guild | None,
        channel_key: str,
        embed: discord.Embed,
        view: discord.ui.View | None = None,
    ) -> discord.Message | None:
        if guild is None:
            return None
        channel = await self.resolve_log_channel(guild, channel_key)
        if channel is None:
            log.warning("No log channel configured for %s in guild %s", channel_key, guild.id)
            return None
        try:
            return await channel.send(embed=embed, view=view)
        except discord.HTTPException as exc:
            log.warning("Failed to send notification to %s: %s", channel_key, exc)
            return None

    async def resolve_log_channel(
        self, guild: discord.Guild, channel_key: str
    ) -> discord.abc.Messageable | None:
        cfg = await self.guild_config(guild.id)
        channel = await self._sendable_channel(guild, cfg.channel_id(channel_key))
        if channel is not None:
            return channel
        found = _match_log_channel(guild, channel_key)
        if found is None:
            return None
        cfg.set_path(["channels", channel_key], str(found.id))
        await self.save_config(guild.id, cfg)
        log.info("Bound %s to #%s (%s)", channel_key, found.name, found.id)
        return found

    async def _sendable_channel(
        self, guild: discord.Guild, channel_id: int
    ) -> discord.abc.Messageable | None:
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    async def try_dm(
        self,
        user: discord.abc.User | None,
        embed: discord.Embed,
        view: discord.ui.View | None = None,
    ) -> bool:
        if user is None:
            return False
        try:
            await user.send(embed=embed, view=view)
            return True
        except discord.HTTPException as exc:
            return False

    async def fetch_guild_user(self, guild: discord.Guild | None, user_id: int) -> discord.abc.User | None:
        if guild is not None:
            member = guild.get_member(user_id)
            if member is not None:
                return member
            try:
                return await guild.fetch_member(user_id)
            except discord.HTTPException as exc:
                pass
        try:
            return await self.fetch_user(user_id)
        except discord.HTTPException as exc:
            return None

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        from discord import app_commands

        orig = error.original if isinstance(error, app_commands.CommandInvokeError) else error
        if isinstance(orig, discord.NotFound) and getattr(orig, "code", None) in {10062, 40060}:
            log.warning("Ignored expired Discord interaction for %s", getattr(interaction.command, "qualified_name", "unknown"))
            return
        if isinstance(orig, discord.HTTPException) and getattr(orig, "code", None) == 40060:
            log.warning("Ignored already-acknowledged Discord interaction for %s", getattr(interaction.command, "qualified_name", "unknown"))
            return
        if isinstance(orig, InsufficientPermission) or isinstance(error, InsufficientPermission):
            embed = discord.Embed(
                title="Restricted",
                description="You cannot use this command.",
                color=COLOR_DANGER,
            )
            embed.set_footer(text=FOOTER)
            await _respond_error(interaction, embed)
            return
        if isinstance(error, app_commands.MissingPermissions):
            await _respond_error(
                interaction,
                discord.Embed(title="Restricted", description="You cannot use this command.", color=COLOR_DANGER),
            )
            return
        log.error("Command error:\n%s", "".join(traceback.format_exception(error)))
        embed = discord.Embed(
            title="Command failed",
            description="Something went wrong.",
            color=COLOR_DANGER,
        )
        embed.set_footer(text=FOOTER)
        await _respond_error(interaction, embed)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type == discord.InteractionType.application_command and interaction.command:
            guild_id = interaction.guild_id or 0
            asyncio.create_task(
                self._audit_interaction(
                    guild_id,
                    interaction.user.id,
                    str(interaction.user),
                    f"/{interaction.command.qualified_name}",
                ),
                name="wsp-interaction-audit",
            )

    async def _audit_interaction(self, guild_id: int, actor_id: int, actor_name: str, details: str) -> None:
        try:
            await self.db.audit(
                guild_id,
                "command",
                actor_id=actor_id,
                actor_name=actor_name,
                details=details,
            )
        except Exception:
            log.exception("Interaction audit failed")

    async def on_command(self, ctx: commands.Context) -> None:
        if ctx.command is None:
            return
        guild_id = ctx.guild.id if ctx.guild else 0
        await self.db.audit(
            guild_id,
            "command",
            actor_id=ctx.author.id,
            actor_name=str(ctx.author),
            details=f"?{ctx.command.qualified_name}",
        )

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        orig = error.original if isinstance(error, commands.CommandInvokeError) else error
        if isinstance(error, commands.CheckFailure):
            embed = discord.Embed(
                title="Restricted",
                description="You cannot use this command.",
                color=COLOR_DANGER,
            )
            embed.set_footer(text=FOOTER)
            try:
                await ctx.send(embed=embed)
            except discord.HTTPException:
                pass
            return
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            embed = discord.Embed(
                title="Command failed",
                description="That command could not be run.",
                color=COLOR_DANGER,
            )
            embed.set_footer(text=FOOTER)
            try:
                await ctx.send(embed=embed)
            except discord.HTTPException:
                pass
            return
        log.error("Prefix command error:\n%s", "".join(traceback.format_exception(error)))
        try:
            await ctx.send(
                embed=discord.Embed(
                    title="Command failed",
                    description="Something went wrong.",
                    color=COLOR_DANGER,
                )
            )
        except discord.HTTPException:
            pass
        _ = orig


_LOG_CHANNEL_NAMES: dict[str, tuple[str, ...]] = {
    "shift_log": ("shift-log", "shift-logs", "shift_log", "shiftlogs"),
    "command_log": ("command-log", "command-logs", "command_log"),
    "hr_log": ("hr-log", "hr-logs", "hr_log"),
    "notifications": ("notifications", "department-notifications"),
    "promotions": ("promotions", "promotion-log", "promotion-logs"),
    "quota": ("quota", "quota-log", "quota-logs"),
    "audit_log": ("audit-log", "audit-logs"),
    "loa": ("loa", "leave-of-absence", "loa-requests"),
}
_GENERIC_LOG_NAMES = ("logs", "wsp-logs", "department-logs")


def _channel_slug(name: str) -> str:
    return name.lower().replace(" ", "-").replace("_", "-")


def _match_log_channel(guild: discord.Guild, key: str) -> discord.TextChannel | None:
    aliases = _LOG_CHANNEL_NAMES.get(key, (key.replace("_", "-"),))
    ranked: list[tuple[int, discord.TextChannel]] = []
    for channel in guild.text_channels:
        slug = _channel_slug(channel.name)
        if slug in aliases:
            ranked.append((0, channel))
        elif any(slug.endswith(alias) or slug.startswith(f"{alias}-") for alias in aliases):
            ranked.append((1, channel))
        elif key != "loa" and slug in _GENERIC_LOG_NAMES:
            ranked.append((2, channel))
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1] if ranked else None


async def _respond_error(interaction: discord.Interaction, embed: discord.Embed) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        pass


