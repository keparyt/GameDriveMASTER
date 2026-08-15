import discord
from discord.ext import commands

from utils.helper import log


class OnMessage(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        guild_name = (
            message.guild.name
            if message.guild
            else "DM"
        )

        log(
            f"Message | "
            f"{message.author} | "
            f"{guild_name} | "
            f"{message.content}"
        )

        await self.bot.process_commands(message)


async def setup(bot: commands.Bot):
    await bot.add_cog(OnMessage(bot))
