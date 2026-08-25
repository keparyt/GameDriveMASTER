import asyncio
import re

import discord
from discord.ext import commands

from config import (
    ANALYSIS_PANEL_TIMEOUT,
    GAME_DETECTOR_CHANNEL_ID,
    GAME_PARSER_GUILD_ID,
    GAME_PARSER_ROLE_ID,
    GAME_QUEUE_CHANNEL_ID,
    INSTALLED_GAMES_CHANNEL_ID,
    MAX_SELECTION_GAMES,
    QUEUE_PANEL_TITLE,
)
from processors.game_media_analyzer import analyze_game_input
from processors.game_queue import add_games, list_queue
from processors.game_queue_panel import get_panel_message_id, set_panel_message_id
from processors.input_parser import process_game_message
from utils.embed_style import PRIMARY, SUCCESS, WARNING, error, panel, status, warning
from utils.helper import log


def normalize_game_name(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    return re.sub(r"\s+", " ", value).strip()


def console_text(game: dict) -> str:
    consoles = game.get("console_platforms") or game.get("console_names") or []
    if isinstance(consoles, str):
        consoles = [consoles]
    return ", ".join(dict.fromkeys(str(x).strip() for x in consoles if str(x).strip()))


def original_input_text(message: discord.Message, parsed: dict | None = None) -> str:
    content = (message.content or "").strip()
    urls = []
    if parsed:
        urls.extend(str(x) for x in parsed.get("urls") or [])
        urls.extend(str(x) for x in parsed.get("unsupported_urls") or [])
    if not urls:
        urls = re.findall(r"https?://[^\s<>]+", content, flags=re.I)
    parts = [content] if content else []
    for attachment in message.attachments:
        parts.append(f"[attachment] {attachment.filename} — {attachment.url}")
    for url in urls:
        if url not in parts and url not in content:
            parts.append(url)
    return "\n".join(parts).strip() or "[No text — media/attachment input]"


async def _delete_message(message: discord.Message) -> None:
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


class GameSelectionView(discord.ui.View):
    """Private selection UI. It expires after the configured timeout."""

    def __init__(self, cog, games: list[dict], installed_names: set[str]):
        super().__init__(timeout=ANALYSIS_PANEL_TIMEOUT)
        self.cog = cog
        self.games = games[:MAX_SELECTION_GAMES]
        self.installed_names = installed_names
        self.resolved_indices: set[int] = set()

        options = []
        for index, game in enumerate(self.games):
            name = str(game.get("name", "Unknown game"))[:100]
            installed = normalize_game_name(name) in installed_names
            platform = str(game.get("selected_platform") or ("PC" if game.get("pc_available") else "Console"))
            consoles = console_text(game)
            description = "Already installed" if installed else f"{platform} • {len(consoles.split(', ')) if consoles else 0} console(s)"
            options.append(discord.SelectOption(
                label=name,
                value=str(index),
                description=description[:100],
                emoji="✅" if installed else "🎮",
            ))
            if installed:
                self.resolved_indices.add(index)

        if options:
            self.select = discord.ui.Select(
                placeholder="Select game(s) to add to the massive library…",
                min_values=1,
                max_values=len(options),
                options=options,
            )
            self.select.callback = self.select_games
            self.add_item(self.select)

    async def select_games(self, interaction: discord.Interaction):
        """Handle a selection without letting the 3-second Discord interaction window expire.

        add_games() and refresh_queue_panel() can perform database/history/network work and
        therefore may take longer than Discord's initial interaction-response deadline.
        Always acknowledge the component interaction immediately, then use the webhook
        follow-up for the private result. This also lets us delete the private selection
        panel after the queue has been updated.
        """
        try:
            # MUST happen before any potentially slow work. Using defer() prevents
            # Discord error 10062 (Unknown interaction) caused by the 3-second deadline.
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.NotFound as exc:
            log(f"Game selection interaction expired before defer | user={interaction.user.id} | {type(exc).__name__}: {exc}")
            return
        except discord.HTTPException as exc:
            log(f"Game selection interaction defer failed | user={interaction.user.id} | {type(exc).__name__}: {exc}")
            return

        try:
            selected_indices = {int(value) for value in self.select.values}
            selected = [self.games[index] for index in selected_indices]
            already_installed = [
                g for g in selected
                if normalize_game_name(str(g.get("name", ""))) in self.installed_names
            ]
            to_queue = [g for g in selected if g not in already_installed]

            added, blocked = await add_games(
                to_queue,
                requester_id=interaction.user.id,
                requester_name=interaction.user.display_name,
            )
            await self.cog.refresh_queue_panel()
            self.resolved_indices.update(selected_indices)

            lines = []
            if added:
                lines.append(
                    "### Added to download queue\n"
                    + "\n".join(f"• **{g.get('name', 'Unknown game')}**" for g in added)
                )
            if blocked:
                lines.append(
                    "### 🚫 Not added\n"
                    + "\n".join(
                        f"• **{g.get('attempted_name') or g.get('name') or 'Unknown game'}** — "
                        f"{g.get('reason') or 'This game is blacklisted.'}"
                        for g in blocked
                    )
                )
            if already_installed:
                lines.append(
                    "### Already installed\n"
                    + "\n".join(f"• **{g.get('name', 'Unknown game')}**" for g in already_installed)
                )
            if not lines:
                lines.append("No changes were required; the selected games are already handled.")

            all_resolved = len(self.resolved_indices) >= len(self.games)
            if all_resolved:
                lines.append("\n**✓ All detected games are resolved.** This private panel will now close.")

            result_embed = panel(
                "Selection updated",
                "\n\n".join(lines),
                color=SUCCESS if added else WARNING,
                footer="Private game selection",
            )

            # The response was deferred above, so use followup.send(), NOT
            # interaction.response.send_message(). The latter can only be called once.
            await interaction.followup.send(embed=result_embed, ephemeral=True)

            if all_resolved:
                self.stop()
                # Delete the actual private analysis/selection message only after the
                # queue update and user feedback have succeeded.
                await _delete_message(interaction.message)
            else:
                # Keep the panel usable for remaining selections.
                try:
                    await interaction.message.edit(view=self)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        except Exception as exc:
            log(
                f"Game selection processing error | user={interaction.user.id} | "
                f"{type(exc).__name__}: {exc}"
            )
            error_embed = error(
                "❌ Game selection failed",
                "The queue update could not be completed. The selection panel may be retried.",
                footer="Private game selection",
            )
            try:
                await interaction.followup.send(embed=error_embed, ephemeral=True)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as followup_exc:
                log(
                    f"Game selection error feedback failed | user={interaction.user.id} | "
                    f"{type(followup_exc).__name__}: {followup_exc}"
                )

    async def on_timeout(self):
        self.stop()
        message = getattr(self, "message", None)
        if message:
            try:
                for item in self.children:
                    item.disabled = True
                await message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class OnMessage(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _has_game_parser_role(self, user: discord.abc.User) -> bool:
        """Require the configured role, including when parsing is requested by DM."""
        try:
            if isinstance(user, discord.Member):
                if user.guild.id != GAME_PARSER_GUILD_ID:
                    return False
                return any(role.id == GAME_PARSER_ROLE_ID for role in user.roles)

            guild = self.bot.get_guild(GAME_PARSER_GUILD_ID)
            if guild is None:
                guild = await self.bot.fetch_guild(GAME_PARSER_GUILD_ID)
            member = guild.get_member(user.id) or await guild.fetch_member(user.id)
            return any(role.id == GAME_PARSER_ROLE_ID for role in member.roles)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log(f"Game parser authorization denied | user={getattr(user, 'id', 'unknown')} | {type(exc).__name__}: {exc}")
            return False
        except Exception as exc:
            log(f"Game parser authorization error | user={getattr(user, 'id', 'unknown')} | {type(exc).__name__}: {exc}")
            return False

    @commands.Cog.listener()
    async def on_ready(self):
        try:
            await self.refresh_queue_panel()
            log("Game queue panel restored.")
        except Exception as exc:
            log(f"Game queue panel restore error | {type(exc).__name__}: {exc}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        guild_name = message.guild.name if message.guild else "DM"
        log(f"Message | {message.author} | {guild_name} | {message.content}")
        if message.guild and message.channel.id == GAME_DETECTOR_CHANNEL_ID:
            if await self._has_game_parser_role(message.author):
                asyncio.create_task(self.handle_game_detection(message))
            else:
                log(f"Game detector ignored | message={message.id} | user={message.author.id} | reason=missing_required_role")
        await self.bot.process_commands(message)

    async def installed_game_names(self) -> set[str]:
        channel = self.bot.get_channel(INSTALLED_GAMES_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(INSTALLED_GAMES_CHANNEL_ID)
            except discord.HTTPException:
                return set()
        names = set()
        try:
            async for message in channel.history(limit=None, oldest_first=True):
                if message.content.strip():
                    names.add(normalize_game_name(message.content.strip()))
        except (discord.Forbidden, discord.HTTPException) as exc:
            log(f"Installed games history error | {type(exc).__name__}: {exc}")
        return names

    async def _find_existing_panel(self, channel) -> discord.Message | None:
        message_id = await get_panel_message_id()
        if message_id:
            try:
                return await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        try:
            async for message in channel.history(limit=100):
                if message.author.id == self.bot.user.id and message.embeds and message.embeds[0].title == QUEUE_PANEL_TITLE:
                    await set_panel_message_id(message.id)
                    return message
        except (discord.Forbidden, discord.HTTPException) as exc:
            log(f"Queue panel history error | {type(exc).__name__}: {exc}")
        return None

    @staticmethod
    def requester_text(game: dict) -> str:
        requester_id = game.get("requester_id")
        return f"<@{requester_id}>" if requester_id else str(game.get("requester_name") or "Unknown")[:40]

    async def refresh_queue_panel(self):
        channel = self.bot.get_channel(GAME_QUEUE_CHANNEL_ID) or await self.bot.fetch_channel(GAME_QUEUE_CHANNEL_ID)
        queue = await list_queue()
        installed = await self.installed_game_names()
        pending = [g for g in queue if normalize_game_name(str(g.get("name", ""))) not in installed]

        lines = []
        for index, game in enumerate(pending, start=1):
            queue_id = game.get("id", index)
            name = str(game.get("name", "Unknown game"))
            url = game.get("library_url") or game.get("kepargamedb_url") or game.get("steam_url") or game.get("tgdb_url")
            shown = f"[{name}]({url})" if url else name
            source = {"kepargamedb": "KeparGameDB", "steam": "Steam", "thegamesdb": "TheGamesDB"}.get(game.get("library_source"), str(game.get("library_source") or "GameDB"))
            requester = self.requester_text(game)
            selected_platform = str(game.get("selected_platform") or ("PC" if game.get("pc_available") else "Console"))
            consoles = console_text(game)
            platform_suffix = f"`PC` → `{consoles}`" if game.get("pc_available") and consoles else (f"`{selected_platform}` → `{consoles}`" if consoles else f"`{selected_platform}`")
            lines.append(f"**#{queue_id} · {shown}**\n`{source}` · {platform_suffix} · requested by {requester}")

        embed = panel(
            QUEUE_PANEL_TITLE,
            "A clean, persistent list of games selected for the massive library.\n\n" + ("\n\n".join(lines[:40]) if lines else "**Queue is clear.**\nNo games are currently waiting."),
            color=PRIMARY,
            footer=f"{len(pending)} waiting • PC prioritized when available • Console is additional support when PC is unavailable",
        )
        embed.add_field(name="Queue status", value=f"**{len(pending)}** game(s) waiting", inline=True)
        embed.add_field(name="Priority", value="PC first", inline=True)
        embed.add_field(name="Console", value="Additional support", inline=True)

        panel_message = await self._find_existing_panel(channel)
        if panel_message:
            try:
                await panel_message.edit(embed=embed)
                await set_panel_message_id(panel_message.id)
                return panel_message
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        panel_message = await channel.send(embed=embed)
        await set_panel_message_id(panel_message.id)
        return panel_message

    async def _open_private_analysis(self, user, embed, view=None):
        try:
            dm = await user.create_dm()
            message = await dm.send(embed=embed, view=view)
            if view is not None:
                view.message = message
            return message
        except (discord.Forbidden, discord.HTTPException) as exc:
            log(f"Game detector private message failed | {type(exc).__name__}: {exc}")
            return None

    async def handle_game_detection(self, message: discord.Message):
        private_message = None
        captured_input = original_input_text(message)
        try:
            if not await self._has_game_parser_role(message.author):
                log(f"Game detector aborted | message={message.id} | user={message.author.id} | reason=missing_required_role")
                return

            parsed = await process_game_message(message)
            if parsed is None:
                return
            captured_input = original_input_text(message, parsed)

            status_embed = status(
                "🎮 Analyzing game media",
                "Inspecting every media item, sampling video for OCR, cross-checking visual evidence, and using descriptions only as a last resort.",
                footer="Private analysis • results stay out of the channel",
            )
            status_embed.add_field(name="Original input", value=f"```{captured_input[:900]}```", inline=False)
            status_embed.add_field(name="Analysis", value="Media → OCR → visual evidence → source verification", inline=False)
            private_message = await self._open_private_analysis(message.author, status_embed)
            if private_message is None:
                log(f"Game detector | kept user input because private response could not be created | message={message.id}")
                return

            await _delete_message(message)
            log(f"Game detector | private response created then deleted user input | message={message.id}")

            if parsed.get("status") == "unsupported_url":
                urls = "\n".join(f"• {url}" for url in parsed.get("unsupported_urls", [])) or "• None"
                embed = warning("🔗 Game URL required", f"{parsed.get('message', 'That URL is not a supported game/media source.')}\n\n**Unrecognized URL(s)**\n{urls}", footer="Private analysis • provide a direct game link when possible")
                embed.add_field(name="Original input", value=f"```{captured_input[:900]}```", inline=False)
                await private_message.edit(embed=embed)
                return

            result = await analyze_game_input(parsed)
            installed = await self.installed_game_names()
            games = result.get("games") or []
            view = GameSelectionView(self, games, installed) if games else None
            result_embed = self.create_result_embed(result, installed, captured_input)
            if view is not None:
                view.message = private_message
            await private_message.edit(embed=result_embed, view=view)
        except Exception as exc:
            log(f"Game detection error | message={message.id} | {type(exc).__name__}: {exc}")
            embed = error("❌ Game detection failed", "Something went wrong while analyzing that content. Please try again with the original source.", footer="Private analysis • no public message was created")
            embed.add_field(name="Original input", value=f"```{captured_input[:900]}```", inline=False)
            if private_message:
                try:
                    await private_message.edit(embed=embed, view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

    @classmethod
    def create_result_embed(cls, result: dict, installed_names: set[str], captured_input: str | None = None) -> discord.Embed:
        games = result.get("games") or []
        unresolved = result.get("unresolved_games") or []
        if not games and result.get("status") not in {"partial", "identified"}:
            embed = warning("🎮 Game not identified", result.get("message", "I couldn't identify a supported game."), footer="Game analysis • no sufficiently strong match")
            if captured_input:
                embed.add_field(name="Original input", value=f"```{captured_input[:900]}```", inline=False)
            embed.add_field(name="Accuracy note", value="Only sufficiently strong title/platform matches are shown. Unresolved candidates are excluded from the queue.", inline=False)
            return embed

        title = "🎮 Games Identified" if not unresolved else "🎮 Games Identified — Some Unresolved"
        description = "Found **%d** verified game(s). Select which game(s) should be sent to the massive library download queue." % len(games)
        if unresolved:
            description += "\n\n⚠️ **Not verified and excluded:** " + ", ".join(str(x) for x in unresolved[:12])
        embed = panel(title, description, color=SUCCESS if games else WARNING, footer="Private game analysis • select only the games you want queued")
        if captured_input:
            embed.add_field(name="Original input", value=f"```{captured_input[:900]}```", inline=False)
        for index, game in enumerate(games[:MAX_SELECTION_GAMES], start=1):
            name = str(game.get("name", "Unknown game"))
            score = game.get("confidence")
            score_text = f"{float(score) * 100:.0f}%" if isinstance(score, (int, float)) else "verified"
            platform = str(game.get("selected_platform") or ("PC" if game.get("pc_available") else "Console"))
            consoles = console_text(game)
            details = f"— **{score_text}** · `{platform}`"
            if consoles:
                details += f" → `{consoles}`"
            evidence = game.get("evidence") or game.get("reason")
            if evidence:
                details += f"\n{str(evidence)[:180]}"
            if normalize_game_name(name) in installed_names:
                details += "\n✅ Already installed"
            embed.add_field(name=f"{index}. {name}", value=details[:1024], inline=False)
        return embed
