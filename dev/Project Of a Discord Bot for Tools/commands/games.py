import discord
from discord import app_commands
from discord.ext import commands

from processors.game_queue import complete_queue_item, resolve_queue_item


class Games(commands.GroupCog, group_name="games"):
    """Admin controls for the massive-library download queue."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _authorized(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.bot.master_id

    @app_commands.command(
        name="downloaded",
        description="Mark a queued game as downloaded.",
    )
    @app_commands.describe(identifier="Queue ID or game name")
    async def downloaded(self, interaction: discord.Interaction, identifier: str):
        if not await self._authorized(interaction):
            await interaction.response.send_message("❌ You are not authorized to manage the download queue.", ephemeral=True)
            return

        item = await complete_queue_item(identifier, "downloaded", "Downloaded")
        if item is None:
            await interaction.response.send_message(f"❌ No queued game matched `{identifier}`.", ephemeral=True)
            return

        await self._refresh_panel()
        await interaction.response.send_message(
            f"✅ Marked **{item['name']}** as downloaded and removed it from the queue.",
            ephemeral=True,
        )

    @app_commands.command(
        name="deny",
        description="Deny a queued game and remove it from the queue.",
    )
    @app_commands.describe(
        identifier="Queue ID or game name",
        reason="Why the game cannot or should not be downloaded",
    )
    async def deny(self, interaction: discord.Interaction, identifier: str, reason: str):
        if not await self._authorized(interaction):
            await interaction.response.send_message("❌ You are not authorized to manage the download queue.", ephemeral=True)
            return

        item = await complete_queue_item(identifier, "denied", reason)
        if item is None:
            await interaction.response.send_message(f"❌ No queued game matched `{identifier}`.", ephemeral=True)
            return

        await self._refresh_panel()
        await interaction.response.send_message(
            f"🚫 Denied **{item['name']}**.\nReason: {reason}",
            ephemeral=True,
        )

    async def _refresh_panel(self):
        # The queue panel is owned by the existing on_message cog.
        cog = self.bot.get_cog("OnMessage")
        if cog is not None:
            await cog.refresh_queue_panel()


async def setup(bot: commands.Bot):
    await bot.add_cog(Games(bot))
