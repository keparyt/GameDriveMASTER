import asyncio

import discord
from discord.ext import commands

from processors.game_detector import process_game_message
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

        log(
            f"Message | "
            f"{message.author} | "
            f"{guild_name} | "
            f"{message.content}"
        )

        # The game detector only watches the dedicated channel.
        if message.channel.id == GAME_DETECTOR_CHANNEL_ID:
            asyncio.create_task(self.handle_game_detection(message))

        await self.bot.process_commands(message)

    async def handle_game_detection(self, message: discord.Message):
        try:
            async with message.channel.typing():
                result = await process_game_message(message)

            if result is None:
                await message.reply(
                    "I couldn't find a supported link or video attachment to analyze. "
                    "Send an Instagram/TikTok URL or upload a video.",
                    mention_author=False,
                )
                return

            await message.reply(
                embed=self.create_result_embed(result),
                mention_author=False,
            )

        except Exception as error:
            log(
                f"Game detection error | message={message.id} | "
                f"{type(error).__name__}: {error}"
            )
            await message.reply(
                "Something went wrong while analyzing that post.",
                mention_author=False,
            )

    @staticmethod
    def create_result_embed(result: dict) -> discord.Embed:
        status = result.get("status", "unknown")
        title = {
            "queued": "Game Detection Queued",
            "processing": "Analyzing Game",
            "unsupported": "Unsupported Source",
            "error": "Analysis Error",
        }.get(status, "Game Detection")

        embed = discord.Embed(
            title=f"🎮 {title}",
            description=result.get("message", "No result yet."),
        )

        source = result.get("source")
        if source:
            embed.add_field(name="Source", value=source, inline=True)

        attachment_count = result.get("attachment_count")
        if attachment_count is not None:
            embed.add_field(
                name="Video attachments",
                value=str(attachment_count),
                inline=True,
            )

        detected_url = result.get("url")
        if detected_url:
            # Avoid Discord embed field overflow with unusually long URLs.
            embed.add_field(
                name="Detected link",
                value=detected_url[:1024],
                inline=False,
            )

        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(OnMessage(bot))
