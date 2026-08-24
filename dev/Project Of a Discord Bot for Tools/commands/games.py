import discord
from discord import app_commands
from discord.ext import commands

from processors.game_queue import blacklist_game, remove_queue_item


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

        from processors.game_queue import complete_queue_item
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
        name="remove",
        description="Remove a game from the queue without blacklisting it.",
    )
    @app_commands.describe(
        identifier="Queue ID or game name",
        reason="Optional reason for removing it",
    )
    async def remove(self, interaction: discord.Interaction, identifier: str, reason: str | None = None):
        if not await self._authorized(interaction):
            await interaction.response.send_message("❌ You are not authorized to manage the download queue.", ephemeral=True)
            return

        item = await remove_queue_item(identifier, reason or "")
        if item is None:
            await interaction.response.send_message(f"❌ No queued game matched `{identifier}`.", ephemeral=True)
            return

        await self._refresh_panel()
        suffix = f"\nReason: {reason}" if reason else ""
        await interaction.response.send_message(
            f"🗑️ Removed **{item['name']}** from the queue.{suffix}",
            ephemeral=True,
        )

    @app_commands.command(
        name="blacklist",
        description="Blacklist a game so users cannot request it.",
    )
    @app_commands.describe(
        identifier="Queue ID or game name",
        reason="Why this game is blacklisted",
    )
    async def blacklist(self, interaction: discord.Interaction, identifier: str, reason: str):
        if not await self._authorized(interaction):
            await interaction.response.send_message("❌ You are not authorized to manage the download queue.", ephemeral=True)
            return

        record = await blacklist_game(identifier, reason)
        if record is None:
            await interaction.response.send_message(f"❌ Could not blacklist `{identifier}`.", ephemeral=True)
            return

        await self._refresh_panel()
        await interaction.response.send_message(
            f"🚫 **{record['name']}** is now blacklisted.\nReason: {reason}",
            ephemeral=True,
        )

    async def _refresh_panel(self):
        cog = self.bot.get_cog("OnMessage")
        if cog is not None:
            await cog.refresh_queue_panel()


async def setup(bot: commands.Bot):
    await bot.add_cog(Games(bot))
