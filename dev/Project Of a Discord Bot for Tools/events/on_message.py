import asyncio

import discord
from discord.ext import commands

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
                "Something went wrong while analyzing that content.",
                mention_author=False,
            )

    @staticmethod
    def create_result_embed(result: dict) -> discord.Embed:
        embed = discord.Embed(
            title="🎮 Game Detection Queued",
            description=result.get("message", "Content accepted."),
        )

        sources = result.get("sources", [])
        if sources:
            embed.add_field(
                name="Detected input",
                value="\n".join(f"• {source}" for source in sources)[:1024],
                inline=False,
            )

        text = result.get("text")
        if text:
            embed.add_field(
                name="Text received",
                value=text[:1024],
                inline=False,
            )

        urls = result.get("urls", [])
        if urls:
            embed.add_field(
                name="Detected links",
                value="\n".join(urls)[:1024],
                inline=False,
            )

        attachment_count = result.get("attachment_count", 0)
        if attachment_count:
            embed.add_field(
                name="Attachments",
                value=str(attachment_count),
                inline=True,
            )

        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(OnMessage(bot))
