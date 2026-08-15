import discord
from discord import app_commands
from discord.ext import commands


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(
        self,
        interaction: discord.Interaction
    ) -> bool:
        """
        Every command inside this Cog is owner-only.
        """
        return await self.bot.is_owner(interaction.user)

    @app_commands.command(
        name="admin",
        description="Owner-only administration command."
    )
    async def admin(
        self,
        interaction: discord.Interaction
    ):
        await interaction.response.send_message(
            "🔐 You are the bot owner.",
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
