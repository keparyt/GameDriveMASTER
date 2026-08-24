import asyncio
import re

import discord
from discord.ext import commands

from processors.game_media_analyzer import analyze_game_input
from processors.game_queue import add_games, list_queue
from processors.game_queue_panel import get_panel_message_id, set_panel_message_id
from processors.input_parser import process_game_message
from utils.embed_style import DANGER, INFO, PRIMARY, SUCCESS, WARNING, error, panel, status, warning
from utils.helper import log

GAME_DETECTOR_CHANNEL_ID = 1541167588476981339
GAME_QUEUE_CHANNEL_ID = 1541255483917074463
INSTALLED_GAMES_CHANNEL_ID = 1537916110488215572
QUEUE_PANEL_TITLE = "📥 Massive Library — Download Queue"
ANALYSIS_PANEL_TIMEOUT = 24 * 60 * 60


def normalize_game_name(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    return re.sub(r"\s+", " ", value).strip()


def console_text(game: dict) -> str:
    consoles = game.get("console_platforms") or game.get("console_names") or []
    if isinstance(consoles, str):
        consoles = [consoles]
    consoles = [str(x).strip() for x in consoles if str(x).strip()]
    return ", ".join(dict.fromkeys(consoles))


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
    """Private 24-hour game selection UI."""

    def __init__(self, cog, games: list[dict], installed_names: set[str]):
        super().__init__(timeout=ANALYSIS_PANEL_TIMEOUT)
        self.cog = cog
        self.games = games
        self.installed_names = installed_names
        self.resolved_indices: set[int] = set()

        options = []
        for index, game in enumerate(games[:25]):
            name = str(game.get("name", "Unknown game"))[:100]
            installed = normalize_game_name(name) in installed_names
            platform = str(game.get("selected_platform") or ("PC" if game.get("pc_available") else "Console"))
            consoles = console_text(game)
            description = "Already installed" if installed else f"{platform} • {len(consoles.split(', ')) if consoles else 0} console(s)"
            options.append(
                discord.SelectOption(
                    label=name,
                    value=str(index),
                    description=description[:100],
                    emoji="✅" if installed else "🎮",
                )
            )
            if installed:
                self.resolved_indices.add(index)

        if not options:
            return

        self.select = discord.ui.Select(
            placeholder="Select game(s) to add to the massive library…",
            min_values=1,
            max_values=len(options),
            options=options,
        )
        self.select.callback = self.select_games
        self.add_item(self.select)

    async def select_games(self, interaction: discord.Interaction):
        selected_indices = {int(value) for value in self.select.values}
        selected = [self.games[index] for index in selected_indices]
        already_installed = [g for g in selected if normalize_game_name(str(g.get("name", ""))) in self.installed_names]
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
            lines.append("### Added to download queue\n" + "\n".join(f"• **{g.get('name', 'Unknown game')}**" for g in added))
        if blocked:
            lines.append("### 🚫 Not added\n" + "\n".join(
                f"• **{g.get('attempted_name') or g.get('name') or 'Unknown game'}** — {g.get('reason') or 'This game is blacklisted.'}"
                for g in blocked
            ))
        if already_installed:
            lines.append("### Already installed\n" + "\n".join(f"• **{g.get('name', 'Unknown game')}**" for g in already_installed))
        if not lines:
            lines.append("No changes were required; the selected games are already handled.")

        all_resolved = len(self.resolved_indices) >= min(len(self.games), 25)
        if all_resolved:
            lines.append("\n**✓ All detected games are resolved.** This private panel will now close.")

        result = panel(
            "Selection updated",
            "\n\n".join(lines),
            color=SUCCESS if added else WARNING,
            footer="Private game selection",
        )
        await interaction.response.send_message(embed=result, ephemeral=True)

        if all_resolved:
            self.stop()
            await _delete_message(interaction.message)

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
        if message.channel.id == GAME_DETECTOR_CHANNEL_ID:
            asyncio.create_task(self.handle_game_detection(message))
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
        channel = self.bot.get_channel(GAME_QUEUE_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(GAME_QUEUE_CHANNEL_ID)
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
            if game.get("pc_available") and consoles:
                platform_suffix = f"`PC` → `{consoles}`"
            elif consoles:
                platform_suffix = f"`{selected_platform}` → `{consoles}`"
            else:
                platform_suffix = f"`{selected_platform}`"
            lines.append(f"**#{queue_id} · {shown}**\n`{source}`  ·  {platform_suffix}  ·  requested by {requester}")

        embed = panel(
            QUEUE_PANEL_TITLE,
            "A clean, persistent list of games selected for the massive library.\n\n" + ("\n\n".join(lines[:40]) if lines else "**Queue is clear.**\nNo games are currently waiting."),
            color=PRIMARY,
            footer=f"{len(pending)} waiting • PC prioritized when available • Console supported when PC is unavailable",
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
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                log(f"Queue panel edit error | {type(exc).__name__}: {exc}")

        panel_message = await channel.send(embed=embed)
        await set_panel_message_id(panel_message.id)
        return panel_message

    async def _open_private_analysis(self, user: discord.abc.User, embed: discord.Embed, view=None):
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
        parsed = None
        captured_input = original_input_text(message)
        try:
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
                embed = warning(
                    "🔗 Game URL required",
                    f"{parsed.get('message', 'That URL is not a supported game/media source.')}\n\n**Unrecognized URL(s)**\n{urls}",
                    footer="Private analysis • provide a direct Steam/KeparDB game link when possible",
                )
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
            embed = error(
                "❌ Game detection failed",
                "Something went wrong while analyzing that content. Please try again with the original source.",
                footer="Private analysis • no public message was created",
            )
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
        status_value = result.get("status")

        if not games and status_value not in {"partial", "identified"}:
            embed = warning("🎮 Game not identified", result.get("message", "I couldn't identify a supported game."), footer="Game analysis • no sufficiently strong match")
            if captured_input:
                embed.add_field(name="Original input", value=f"```{captured_input[:900]}```", inline=False)
            embed.add_field(name="Accuracy note", value="The detector is not 100% perfect. Results depend on the quality and clarity of the supplied media.", inline=False)
            return embed

        title = "🎮 Games identified" if not unresolved else "🎮 Games identified · Some unresolved"
        description = f"Found **{len(games)}** verified game(s). Review the matches below, then select the game(s) you want in the massive library queue."
        embed = panel(title, description, color=SUCCESS if games else WARNING, footer="Private analysis • selection panel expires after 24h or when resolved")

        if captured_input:
            embed.add_field(name="Original input", value=f"```{captured_input[:900]}```", inline=False)

        embed.add_field(
            name="Accuracy note",
            value="⚠️ **This bot is not 100% perfect.** Always review the detected titles before adding them to the queue. Media/OCR evidence is prioritized; descriptions are only supporting context.",
            inline=False,
        )

        lines = []
        for index, game in enumerate(games, start=1):
            name = str(game.get("name", "Unknown game"))
            url = game.get("steam_url") or game.get("kepargamedb_url") or game.get("library_url") or game.get("tgdb_url")
            shown = f"[{name}]({url})" if url else name
            state = "Already installed" if normalize_game_name(name) in installed_names else f"{float(game.get('confidence', 0)):.0f}% confidence"
            selected_platform = str(game.get("selected_platform") or ("PC" if game.get("pc_available") else "Console"))
            consoles = console_text(game)
            platform = f"`PC` → `{consoles}`" if game.get("pc_available") and consoles else (f"`{selected_platform}` → `{consoles}`" if consoles else f"`{selected_platform}`")
            evidence_type = str(game.get("evidence_type") or "unknown")
            lines.append(f"**{index}. {shown}**\n`{state}`  ·  `{evidence_type}`  ·  {platform}")

        for start in range(0, len(lines), 10):
            embed.add_field(name="Detected games" if start == 0 else "More detected games", value="\n\n".join(lines[start:start + 10])[:1024], inline=False)

        evidence = []
        for index, game in enumerate(games, start=1):
            reason = str(game.get("reason") or "").strip()
            if reason:
                evidence.append(f"**{index}.** {reason}")
        if evidence:
            embed.add_field(name="Why these matches", value="\n".join(evidence)[:1024], inline=False)

        if unresolved:
            unresolved_lines = []
            for item in unresolved:
                name = str(item.get("name", "Unknown game"))
                unresolved_lines.append(f"• **{name}** — send its direct Steam/KeparDB store page URL + proper name")
            embed.add_field(name="Unresolved matches", value="\n".join(unresolved_lines)[:1024], inline=False)

        embed.add_field(name="Next step", value="Use the selector below to add one or more verified games to the massive library queue. You can close this private panel at any time.", inline=False)
        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(OnMessage(bot))
