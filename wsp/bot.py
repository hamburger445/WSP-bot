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

log = logging.getLogger("wsp.bot")

COG_MODULES = [
    "wsp.cogs.setup",
    "wsp.cogs.personnel",
    "wsp.cogs.profile",
    "wsp.cogs.fastpass",
    "wsp.cogs.supervision",
    "wsp.cogs.probation",
    "wsp.cogs.shifts",
    "wsp.cogs.quota",
    "wsp.cogs.loa",
    "wsp.cogs.promotions",
    "wsp.cogs.discipline",
    "wsp.cogs.tickets",
    "wsp.cogs.vehicles",
    "wsp.cogs.dashboard",
    "wsp.cogs.help",
    "wsp.cogs.tasks",
]


class WSPBot(commands.Bot):
    def __init__(self, settings: Settings, db: Database) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        # Privileged intents (members / message content) will keep the bot
        # completely offline if they are not enabled in the Developer Portal.
        intents.members = False
        intents.message_content = False
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.settings = settings
        self.db = db
        self.github_db = None
        self._config_cache: dict[int, GuildConfig] = {}
        self.last_error: str | None = None
        self.synced_commands: list[str] = []
        self._synced = False

    async def setup_hook(self) -> None:
        from wsp.views.shifts import ShiftActionView, ShiftMenuView
        from wsp.views.tickets import TicketControlsView, TicketPanelView
        from wsp.cogs.loa import DenyLOAButton, ApproveLOAButton
        from wsp.views.discipline import AppealOpenButton, AppealOverturnButton, AppealUpholdButton

        self.add_view(TicketPanelView())
        self.add_view(TicketControlsView())
        self.add_view(ShiftMenuView())
        self.add_view(ShiftActionView(lock_buttons=False))
        self.add_dynamic_items(ApproveLOAButton, DenyLOAButton)
        self.add_dynamic_items(AppealOpenButton, AppealUpholdButton, AppealOverturnButton)
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
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Wisconsin State Patrol  •  LVRP",
            )
        )
        for guild in self.guilds:
            cfg = await self.guild_config(guild.id)
            await self.db.ensure_ranks(guild.id, cfg)
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
        github_db = self.github_db
        if github_db is not None and github_db.enabled:
            github_db.schedule_push(self.db)

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
        cfg = await self.guild_config(guild.id)
        channel_id = cfg.channel_id(channel_key)
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                return await channel.send(embed=embed, view=view)
            except discord.HTTPException as exc:
                log.warning("Failed to send notification to %s: %s", channel_key, exc)
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
        except discord.HTTPException:
            return False

    async def fetch_guild_user(self, guild: discord.Guild | None, user_id: int) -> discord.abc.User | None:
        if guild is not None:
            member = guild.get_member(user_id)
            if member is not None:
                return member
            try:
                return await guild.fetch_member(user_id)
            except discord.HTTPException:
                pass
        try:
            return await self.fetch_user(user_id)
        except discord.HTTPException:
            return None

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        from discord import app_commands

        orig = error.original if isinstance(error, app_commands.CommandInvokeError) else error
        if isinstance(orig, InsufficientPermission) or isinstance(error, InsufficientPermission):
            required = getattr(orig, "required", None) or getattr(error, "required", None)
            msg = str(error)
            embed = discord.Embed(
                title="Access restricted",
                description=msg if msg else "You do not have permission to use this command.",
                color=COLOR_DANGER,
            )
            embed.set_footer(text=FOOTER)
            if required:
                embed.add_field(name="Required access", value=str(required.name).title(), inline=True)
            await _respond_error(interaction, embed)
            return
        if isinstance(error, app_commands.MissingPermissions):
            await _respond_error(
                interaction,
                discord.Embed(title="Missing Discord permission", description=str(error), color=COLOR_DANGER),
            )
            return
        log.error("Command error:\n%s", "".join(traceback.format_exception(error)))
        embed = discord.Embed(
            title="Command failed",
            description="An unexpected error occurred. The incident has been recorded.",
            color=COLOR_DANGER,
        )
        embed.set_footer(text=FOOTER)
        await _respond_error(interaction, embed)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type == discord.InteractionType.application_command and interaction.command:
            guild_id = interaction.guild_id or 0
            await self.db.audit(
                guild_id,
                "command",
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
                details=f"/{interaction.command.qualified_name}",
            )


async def _respond_error(interaction: discord.Interaction, embed: discord.Embed) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        pass


