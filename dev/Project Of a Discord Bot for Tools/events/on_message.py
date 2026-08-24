import asyncio
import re

import discord
from discord.ext import commands

from processors.game_analyzer import analyze_game_input
from processors.game_queue import add_games, list_queue
from processors.game_queue_panel import get_panel_message_id, set_panel_message_id
from processors.input_parser import process_game_message
from utils.helper import log

GAME_DETECTOR_CHANNEL_ID = 1541167588476981339
GAME_QUEUE_CHANNEL_ID = 1541255483917074463
INSTALLED_GAMES_CHANNEL_ID = 1537916110488215572
QUEUE_PANEL_TITLE = "📥 Massive Library — Download Queue"


def normalize_game_name(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    return re.sub(r"\s+", " ", value).strip()


def console_text(game: dict) -> str:
    consoles = game.get("console_platforms") or game.get("console_names") or []
    if isinstance(consoles, str):
        consoles = [consoles]
    consoles = [str(x).strip() for x in consoles if str(x).strip()]
    return ", ".join(dict.fromkeys(consoles))


class GameSelectionView(discord.ui.View):
    def __init__(self, cog, games: list[dict], installed_names: set[str]):
        super().__init__(timeout=300)
        self.cog = cog
        self.games = games
        self.installed_names = installed_names
        options = []
        for index, game in enumerate(games[:25]):
            name = str(game.get("name", "Unknown game"))[:100]
            installed = normalize_game_name(name) in installed_names
            platform = str(game.get("selected_platform") or ("PC" if game.get("pc_available") else "Console"))
            consoles = console_text(game)
            description = "Already installed" if installed else f"{platform} • {len(consoles.split(', ')) if consoles else 0} console(s)"
            options.append(discord.SelectOption(label=name, value=str(index), description=description[:100], emoji="✅" if installed else "🎮"))
        self.select = discord.ui.Select(placeholder="Choose the game(s) to add to the download queue...", min_values=0, max_values=max(1, len(options)), options=options)
        self.select.callback = self.select_games
        self.add_item(self.select)

    async def select_games(self, interaction: discord.Interaction):
        selected = [self.games[int(value)] for value in self.select.values]
        already_installed = [g for g in selected if normalize_game_name(str(g.get("name", ""))) in self.installed_names]
        to_queue = [g for g in selected if g not in already_installed]
        added, blocked = await add_games(to_queue, requester_id=interaction.user.id, requester_name=interaction.user.display_name)
        await self.cog.refresh_queue_panel()
        parts = []
        if added:
            parts.append("Added to queue: " + ", ".join(str(g.get("name", "Unknown game")) for g in added))
        if blocked:
            blocked_lines = []
            for game in blocked:
                name = str(game.get("attempted_name") or game.get("name") or "Unknown game")
                reason = str(game.get("reason") or "This game is blacklisted.").strip()
                blocked_lines.append(f"{name} — {reason}")
            parts.append("🚫 Blacklisted:\n" + "\n".join(blocked_lines))
        if already_installed:
            parts.append("Already installed: " + ", ".join(str(g.get("name")) for g in already_installed))
        if not parts:
            parts.append("Nothing new was added; those games are already in the queue.")
        await interaction.response.send_message("\n".join(parts), ephemeral=True)


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
        if requester_id:
            return f"<@{requester_id}>"
        return str(game.get("requester_name") or "Unknown")[:40]

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
            source = {
                "kepargamedb": "KeparGameDB",
                "steam": "Steam",
                "thegamesdb": "TheGamesDB",
            }.get(game.get("library_source"), str(game.get("library_source") or "GameDB"))
            requester = self.requester_text(game)
            selected_platform = str(game.get("selected_platform") or ("PC" if game.get("pc_available") else "Console"))
            consoles = console_text(game)
            platform_suffix = f"`PC` → `{consoles}`" if game.get("pc_available") and consoles else (f"`{selected_platform}` → `{consoles}`" if consoles else f"`{selected_platform}`")
            lines.append(f"**#{queue_id} — {shown}** · `{source}` · {platform_suffix} · requested by {requester}")
        description = "Games selected by users that still need to be downloaded.\n\n" + ("\n".join(lines[:50]) if lines else "No games are currently waiting.")
        embed = discord.Embed(title=QUEUE_PANEL_TITLE, description=description[:4096])
        embed.set_footer(text=f"{len(pending)} game(s) waiting • PC is prioritized when available • Console support is required")
        panel = await self._find_existing_panel(channel)
        if panel:
            try:
                await panel.edit(embed=embed)
                await set_panel_message_id(panel.id)
                return panel
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                log(f"Queue panel edit error | {type(exc).__name__}: {exc}")
        panel = await channel.send(embed=embed)
        await set_panel_message_id(panel.id)
        return panel

    async def handle_game_detection(self, message: discord.Message):
        status_message = None
        try:
            parsed = await process_game_message(message)
            if parsed is None:
                return
            if parsed.get("status") == "unsupported_url":
                urls = "\n".join(f"• {url}" for url in parsed.get("unsupported_urls", []))
                embed = discord.Embed(title="🔗 Game URL Required", description=f"{parsed.get('message', 'That URL is not a supported game/media source.')}\n\n**Unrecognized URL(s):**\n{urls}\n\nFor a game, send its **direct Steam or KeparDB store page URL** and its proper game name.")
                await message.reply(embed=embed, mention_author=False)
                return

            status_message = await message.reply(embed=discord.Embed(title="🎮 Analyzing Games...", description="Collecting metadata, transcribing audio, extracting OCR and identifying every distinct game..."), mention_author=False)
            result = await analyze_game_input(parsed)
            installed = await self.installed_game_names()
            games = result.get("games") or []
            view = GameSelectionView(self, games, installed) if games else None
            await status_message.edit(embed=self.create_result_embed(result, installed), view=view)
        except Exception as error:
            log(f"Game detection error | message={message.id} | {type(error).__name__}: {error}")
            embed = discord.Embed(title="❌ Game Detection Failed", description="Something went wrong while analyzing that content.")
            if status_message:
                await status_message.edit(embed=embed)
            else:
                await message.reply(embed=embed, mention_author=False)

    @classmethod
    def create_result_embed(cls, result: dict, installed_names: set[str]) -> discord.Embed:
        games = result.get("games") or []
        unresolved = result.get("unresolved_games") or []
        status = result.get("status")
        if not games and status not in {"partial", "identified"}:
            return discord.Embed(title="🎮 Game Not Identified", description=result.get("message", "I couldn't identify the game."))

        title = "🎮 Games Identified" if not unresolved else "🎮 Games Identified — Some Unresolved"
        description = f"Found **{len(games)}** verified game(s). Select which game(s) should be sent to the massive library download queue."
        if unresolved:
            unresolved_names = ", ".join(str(item.get("name", "Unknown game")) for item in unresolved)
            description += f"\n\n⚠️ **Not verified and excluded:** {unresolved_names}"
        embed = discord.Embed(title=title, description=description[:4096])

        lines = []
        for index, game in enumerate(games, start=1):
            name = str(game.get("name", "Unknown game"))
            url = game.get("steam_url") or game.get("kepargamedb_url") or game.get("library_url") or game.get("tgdb_url")
            shown = f"[{name}]({url})" if url else name
            state = "**Already installed**" if normalize_game_name(name) in installed_names else f"{float(game.get('confidence', 0)):.0f}%"
            selected_platform = str(game.get("selected_platform") or ("PC" if game.get("pc_available") else "Console"))
            consoles = console_text(game)
            platform = f"`PC` → `{consoles}`" if game.get("pc_available") and consoles else (f"`{selected_platform}` → `{consoles}`" if consoles else f"`{selected_platform}`")
            lines.append(f"**{index}. {shown}** — {state} · {platform}")
        for start in range(0, len(lines), 15):
            embed.add_field(name="Detected games" if start == 0 else "More games", value="\n".join(lines[start:start + 15])[:1024], inline=False)

        evidence = []
        for index, game in enumerate(games, start=1):
            reason = str(game.get("reason") or "").strip()
            if reason:
                evidence.append(f"**{index}.** {reason}")
        if evidence:
            embed.add_field(name="Evidence", value="\n".join(evidence)[:1024], inline=False)

        if unresolved:
            unresolved_lines = []
            for item in unresolved:
                name = str(item.get("name", "Unknown game"))
                unresolved_lines.append(f"• **{name}** — send its direct Steam/KeparDB store page URL + proper name")
            embed.add_field(name="Need a direct game link", value="\n".join(unresolved_lines)[:1024], inline=False)
        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(OnMessage(bot))
