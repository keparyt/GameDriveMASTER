import asyncio

import discord
from discord.ext import commands

from processors.game_analyzer import analyze_game_input
from processors.input_parser import process_game_message
from utils.helper import log


GAME_DETECTOR_CHANNEL_ID = 1541167588476981339


class OnMessage(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        guild_name = message.guild.name if message.guild else "DM"
        log(f"Message | {message.author} | {guild_name} | {message.content}")

        if message.channel.id == GAME_DETECTOR_CHANNEL_ID:
            asyncio.create_task(self.handle_game_detection(message))

        await self.bot.process_commands(message)

    async def handle_game_detection(self, message: discord.Message):
        status_message = None
        try:
            parsed = await process_game_message(message)
            if parsed is None:
                return

            status_message = await message.reply(
                embed=discord.Embed(
                    title="🎮 Analyzing Games...",
                    description=(
                        "Collecting metadata, transcribing audio, extracting OCR from "
                        "frames/screenshots, identifying every distinct game, and verifying "
                        "matches against Steam..."
                    ),
                ),
                mention_author=False,
            )

            result = await analyze_game_input(parsed)
            await status_message.edit(embed=self.create_result_embed(result))

        except Exception as error:
            log(f"Game detection error | message={message.id} | {type(error).__name__}: {error}")
            embed = discord.Embed(
                title="❌ Game Detection Failed",
                description="Something went wrong while analyzing that content.",
            )
            if status_message:
                await status_message.edit(embed=embed)
            else:
                await message.reply(embed=embed, mention_author=False)

    @staticmethod
    def _game_line(game: dict, index: int) -> str:
        name = str(game.get("name", "Unknown game"))
        confidence = float(game.get("confidence", 0))
        steam_url = game.get("steam_url")
        evidence_type = game.get("evidence_type")

        # Discord embed markdown: [Game Name](https://...) makes the game name
        # itself clickable when Steam verification produced a store URL.
        linked_name = f"[{name}]({steam_url})" if steam_url else name
        line = f"**{index}. {linked_name}** — {confidence:.0f}%"
        if evidence_type:
            line += f"\n↳ `{evidence_type}`"
        return line

    @classmethod
    def create_result_embed(cls, result: dict) -> discord.Embed:
        status = result.get("status")

        if status == "identified":
            games = result.get("games") or result.get("candidates") or []

            # Backward compatibility if an older analyzer only returns one game.
            if not games and result.get("game_name"):
                games = [{
                    "name": result.get("game_name"),
                    "confidence": result.get("confidence", 0),
                    "steam_url": result.get("steam_url"),
                    "reason": result.get("reason"),
                }]

            embed = discord.Embed(
                title="🎮 Games Identified",
                description=f"Found **{len(games)}** distinct game(s).",
            )

            # Put every game in the result. Steam-confirmed titles become direct
            # clickable hyperlinks instead of showing a raw URL.
            lines = []
            for index, game in enumerate(games, start=1):
                line = cls._game_line(game, index)
                if len("\n".join(lines + [line])) > 1000:
                    embed.add_field(
                        name="Games",
                        value="\n".join(lines),
                        inline=False,
                    )
                    lines = []
                lines.append(line)

            if lines:
                embed.add_field(
                    name="Games",
                    value="\n".join(lines)[:1024],
                    inline=False,
                )

            # Add compact evidence per game so multiple games do not lose the
            # OCR/transcript clues that caused them to be identified.
            evidence_lines = []
            for index, game in enumerate(games, start=1):
                reason = str(game.get("reason") or "").strip()
                if reason:
                    evidence_lines.append(f"**{index}.** {reason}")

            if evidence_lines:
                value = "\n".join(evidence_lines)
                if len(value) <= 1024:
                    embed.add_field(name="Evidence", value=value, inline=False)
                else:
                    # Discord fields are limited to 1024 characters.
                    embed.add_field(name="Evidence", value=value[:1021] + "...", inline=False)

            return embed

        return discord.Embed(
            title="🎮 Game Not Identified",
            description=result.get("message", "I couldn't identify the game."),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(OnMessage(bot))
