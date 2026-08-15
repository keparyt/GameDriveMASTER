import discord
from discord.ext import commands

from utils.helper import log


class OnReady(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        log("=" * 50)
        log(f"Bot:       {self.bot.user}")
        log(f"Bot ID:    {self.bot.user.id}")
        log(f"Guilds:    {len(self.bot.guilds)}")
        log("=" * 50)


async def setup(bot: commands.Bot):
    await bot.add_cog(OnReady(bot))
