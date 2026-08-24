"""Owner setup, verification, and configuration commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from wsp.constants import COLOR_GOLD, COLOR_NAVY, DEFAULT_RANKS, PermissionLevel
from wsp.embeds import add_fields, base_embed, error_embed, success_embed, warning_embed
from wsp.permissions import has_level, is_owner

if TYPE_CHECKING:
    from wsp.bot import WSPBot

ROLE_SPECS = [
    ("wsp", "WSP", 0x0D2137, "Wisconsin State Patrol membership"),
    ("hr", "WSP | HR", 0xC9A227, "Human Resources"),
    ("command", "WSP | Command", 0x8B1E3F, "Command staff"),
    ("supervisor", "WSP | Supervisor", 0x3D5A80, "Field supervision"),
    ("superintendent", "WSP | Superintendent", 0xC9A227, "Department head"),
]

RANK_COLORS = {
    "Probationary Trooper": 0x6B7280,
    "Trooper": 0x3D5A80,
    "Senior Trooper": 0x3D5A80,
    "Master Trooper": 0x2E5A88,
    "Corporal": 0x1F6B4A,
    "Sergeant": 0x1F6B4A,
    "Lieutenant": 0xC9782A,
    "Captain": 0xC9782A,
    "Major": 0x8B1E3F,
    "Lieutenant Colonel": 0x8B1E3F,
    "Colonel": 0x8B1E3F,
    "Superintendent": 0xC9A227,
}

CHANNEL_SPECS = [
    ("applications", "wsp-applications", "Fast-pass and application traffic"),
    ("promotions", "wsp-promotions", "Promotion and demotion notices"),
    ("discipline", "wsp-discipline", "Disciplinary actions"),
    ("loa", "wsp-loa", "Leave of absence requests"),
    ("quota", "wsp-quota", "Quota reminders and reports"),
    ("notifications", "wsp-notifications", "Department notifications"),
    ("hr_log", "wsp-hr-log", "HR action log"),
    ("command_log", "wsp-command-log", "Command action log"),
    ("audit_log", "wsp-audit-log", "Full audit trail"),
    ("fastpass", "wsp-fastpass", "Fast-pass evaluations"),
    ("supervision", "wsp-supervision", "Ride-along / supervision"),
    ("probation", "wsp-probation", "Probationary period tracking"),
    ("tickets_log", "wsp-ticket-log", "Ticket transcripts and closures"),
    ("ticket_panel", "wsp-tickets", "Public ticket panel"),
    ("resignations", "wsp-resignations", "Resignation notices"),
]


class Setup(commands.Cog):
    def __init__(self, bot: WSPBot) -> None:
        self.bot = bot

    @app_commands.command(name="setupserver", description="Create WSP roles, categories, and channels (owner only). Does not delete existing data.")
    @is_owner()
    async def setupserver(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        cfg = await self.bot.guild_config(guild.id)
        cfg.set_path(["guild_id"], str(guild.id))

        created_roles: list[str] = []
        for key, name, color, _desc in ROLE_SPECS:
            existing_id = cfg.role_id(key)
            role = guild.get_role(existing_id) if existing_id else discord.utils.get(guild.roles, name=name)
            if role is None:
                role = await guild.create_role(name=name, colour=discord.Colour(color), mentionable=True, reason="WSP setup")
                created_roles.append(name)
            cfg.set_path(["roles", key], str(role.id))

        for name, _pos, _lv in DEFAULT_RANKS:
            existing_id = cfg.rank_role_id(name)
            role = guild.get_role(existing_id) if existing_id else discord.utils.get(guild.roles, name=f"WSP | {name}")
            if role is None:
                role = await guild.create_role(
                    name=f"WSP | {name}",
                    colour=discord.Colour(RANK_COLORS.get(name, 0x3D5A80)),
                    mentionable=False,
                    reason="WSP rank setup",
                )
                created_roles.append(role.name)
            cfg.set_path(["rank_roles", name], str(role.id))
            await self.bot.db.set_rank_role(guild.id, name, role.id)

        async def ensure_category(key: str, name: str) -> discord.CategoryChannel:
            existing_id = cfg.category_id(key)
            ch = guild.get_channel(existing_id) if existing_id else discord.utils.get(guild.categories, name=name)
            if not isinstance(ch, discord.CategoryChannel):
                ch = await guild.create_category(name, reason="WSP setup")
            cfg.set_path(["categories", key], str(ch.id))
            return ch

        logs_cat = await ensure_category("logs", "WSP Logs")
        command_cat = await ensure_category("command", "WSP Command")
        tickets_cat = await ensure_category("tickets", "WSP Tickets")

        hr_role = guild.get_role(cfg.role_id("hr"))
        command_role = guild.get_role(cfg.role_id("command"))
        super_role = guild.get_role(cfg.role_id("superintendent"))
        wsp_role = guild.get_role(cfg.role_id("wsp"))

        staff_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        for role in (hr_role, command_role, super_role):
            if role:
                staff_overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        created_channels: list[str] = []
        for key, name, topic in CHANNEL_SPECS:
            existing_id = cfg.channel_id(key)
            channel = guild.get_channel(existing_id) if existing_id else discord.utils.get(guild.text_channels, name=name)
            category = tickets_cat if key == "ticket_panel" else (command_cat if key in {"promotions", "discipline", "notifications"} else logs_cat)
            overwrites = None if key in {"ticket_panel", "notifications"} else staff_overwrites
            if key in {"ticket_panel", "notifications"} and wsp_role:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    wsp_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
                }
                for role in (hr_role, command_role, super_role):
                    if role:
                        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            if not isinstance(channel, discord.TextChannel):
                channel = await guild.create_text_channel(name, category=category, topic=topic, overwrites=overwrites, reason="WSP setup")
                created_channels.append(name)
            cfg.set_path(["channels", key], str(channel.id))

        await self.bot.db.ensure_ranks(guild.id, cfg)
        await self.bot.save_config(guild.id, cfg)

        panel = guild.get_channel(cfg.channel_id("ticket_panel"))
        if isinstance(panel, discord.TextChannel):
            from wsp.views.tickets import TicketPanelView

            embed = base_embed(
                "Wisconsin State Patrol  •  Assistance Desk",
                "Select a request type below. A private ticket channel will be created for you and HR/Command.",
                color=COLOR_GOLD,
            )
            await panel.send(embed=embed, view=TicketPanelView())

        embed = success_embed(
            "Server setup complete",
            "Roles and channels were created or reused. Existing Discord data was not deleted.",
        )
        add_fields(
            embed,
            [
                ("Roles created", ", ".join(created_roles) or "None (reused existing)", False),
                ("Channels created", ", ".join(created_channels) or "None (reused existing)", False),
                ("Guild ID", str(guild.id), True),
            ],
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="verifysetup", description="Check that required roles, channels, and database tables exist.")
    @has_level(PermissionLevel.COMMAND)
    async def verifysetup(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        cfg = await self.bot.guild_config(guild.id)
        missing_cfg = cfg.missing_required()
        missing_discord: list[str] = []
        for key in ("wsp", "hr", "command", "supervisor", "superintendent"):
            rid = cfg.role_id(key)
            if rid and guild.get_role(rid) is None:
                missing_discord.append(f"role {key}")
        for key, _name, _topic in CHANNEL_SPECS:
            cid = cfg.channel_id(key)
            if cid and guild.get_channel(cid) is None:
                missing_discord.append(f"channel {key}")
        tables = await self.bot.db.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        table_names = [r["name"] for r in tables]
        required_tables = ["personnel", "shifts", "audit_log", "tickets", "loa_requests", "discipline"]
        missing_tables = [t for t in required_tables if t not in table_names]
        ok = not missing_cfg and not missing_discord and not missing_tables
        embed = success_embed("Setup verification", "All required pieces are in place.") if ok else warning_embed(
            "Setup incomplete", "Review the missing items below, then run `/setupserver` or `/config`."
        )
        add_fields(
            embed,
            [
                ("Config gaps", "\n".join(missing_cfg) or "None", False),
                ("Missing Discord objects", "\n".join(missing_discord) or "None", False),
                ("Missing tables", ", ".join(missing_tables) or "None", False),
                ("Tables present", str(len(table_names)), True),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="setup", description="Interactive setup overview and next steps.")
    @has_level(PermissionLevel.COMMAND)
    async def setup(self, interaction: discord.Interaction) -> None:
        embed = base_embed(
            "WSP department setup",
            "Use the buttons below. `/setupserver` creates Discord structure. `/config` edits IDs without touching Discord.",
            color=COLOR_NAVY,
        )
        view = SetupNavView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="config", description="View or update department configuration keys.")
    @has_level(PermissionLevel.SUPERINTENDENT)
    @app_commands.describe(path="Dot path such as channels.audit_log or quota.weekly_minutes", value="New value (omit to view)")
    async def config(self, interaction: discord.Interaction, path: str | None = None, value: str | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only"), ephemeral=True)
            return
        cfg = await self.bot.guild_config(interaction.guild.id)
        if not path:
            embed = base_embed("Department configuration", "Current high-level IDs. Use `/config path:channels.hr_log value:123` to change.")
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
        parsed: str | int = value
        if value.isdigit():
            parsed = int(value)
        cfg.set_path(keys, str(parsed) if keys[0] in {"roles", "channels", "categories", "rank_roles", "guild_id"} else parsed)
        await self.bot.save_config(interaction.guild.id, cfg)
        await self.bot.db.audit(
            interaction.guild.id,
            "config",
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
            details=f"{path} = {value}",
        )
        await interaction.response.send_message(embed=success_embed("Configuration updated", f"`{path}` set to `{value}`."), ephemeral=True)

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

    @app_commands.command(name="resetserver", description="Clear stored department configuration for this guild. Does not delete Discord roles/channels unless confirmed.")
    @is_owner()
    @app_commands.describe(wipe_discord="If true, also delete WSP-managed roles and channels created by name")
    async def resetserver(self, interaction: discord.Interaction, wipe_discord: bool = False) -> None:
        view = ResetConfirmView(wipe_discord)
        await interaction.response.send_message(
            embed=warning_embed(
                "Confirm configuration reset",
                "This removes stored WSP configuration for this guild. Personnel records in the database are kept.\n"
                + ("Discord roles/channels matching WSP setup names will also be deleted." if wipe_discord else "Discord roles and channels will be left in place."),
            ),
            view=view,
            ephemeral=True,
        )


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
            embed=success_embed("Configuration reset", "Stored settings cleared." + (f" Deleted: {', '.join(deleted) or 'none'}." if self.wipe_discord else "")),
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=base_embed("Cancelled", "No changes made."), view=None)


async def setup(bot: WSPBot) -> None:
    await bot.add_cog(Setup(bot))
