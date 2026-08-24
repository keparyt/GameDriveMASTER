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
                    title="🎮 Analyzing Game...",
                    description="Collecting metadata, audio, screenshots and other evidence...",
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
    def create_result_embed(result: dict) -> discord.Embed:
        status = result.get("status")

        if status == "identified":
            name = result.get("game_name", "Unknown game")
            confidence = float(result.get("confidence", 0))
            embed = discord.Embed(
                title="🎮 Game Identified",
                description=f"**{name}**\n\nConfidence: **{confidence:.0f}%**",
            )
            if result.get("steam_url"):
                embed.add_field(name="Steam", value=result["steam_url"], inline=False)
            if result.get("reason"):
                embed.add_field(name="Evidence", value=result["reason"][:1024], inline=False)

            candidates = result.get("candidates", [])
            if len(candidates) > 1:
                text = "\n".join(
                    f"• **{c.get('name', 'Unknown')}** — {float(c.get('confidence', 0)):.0f}%"
                    for c in candidates[1:5]
                )
                embed.add_field(name="Other candidates", value=text[:1024], inline=False)
            return embed

        return discord.Embed(
            title="🎮 Game Not Identified",
            description=result.get("message", "I couldn't identify the game."),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(OnMessage(bot))
