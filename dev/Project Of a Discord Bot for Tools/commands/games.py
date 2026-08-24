import discord
from discord import app_commands
from discord.ext import commands

from processors.game_queue import blacklist_game, remove_queue_item
from processors.game_media_analyzer import analyze_game_input
from processors.input_parser import process_game_message
from utils.game_selection_close import install_game_selection_close_button

# Install the small Close control on the shared private game-selection view.
# The view itself lives in events.on_message and is also used by /games analyze.
install_game_selection_close_button()


class Games(commands.GroupCog, group_name="games"):
    """Game tools and admin controls for the massive-library queue."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _authorized(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.bot.master_id

    @app_commands.command(
        name="analyze",
        description="Analyze a game name, URL, or media source privately.",
    )
    @app_commands.describe(
        input="Game name, Steam/social/direct media URL, or optional media attachment",
        attachment="Optional image or video attachment to analyze",
    )
    async def analyze(
        self,
        interaction: discord.Interaction,
        input: str = "",
        attachment: discord.Attachment | None = None,
    ):
        """Run game identification entirely as a genuine ephemeral interaction."""
        await interaction.response.defer(ephemeral=True, thinking=True)

        cog = self.bot.get_cog("OnMessage")
        if cog is None:
            await interaction.edit_original_response(
                content="❌ The game analyzer is not available right now."
            )
            return

        class _InteractionMessage:
            """Message-compatible adapter used by the existing parser."""

            def __init__(
                self,
                interaction: discord.Interaction,
                content: str,
                attachment: discord.Attachment | None,
            ):
                self.id = interaction.id
                self.content = content
                self.attachments = [attachment] if attachment else []
                self.author = interaction.user
                self.channel = interaction.channel

        input_text = (input or "").strip()
        if not input_text and not attachment:
            await interaction.edit_original_response(
                content="❌ Provide a game name, supported URL, or image/video attachment."
            )
            return

        message = _InteractionMessage(interaction, input_text, attachment)

        try:
            parsed = await process_game_message(message)
            if parsed is None:
                await interaction.edit_original_response(
                    content="❌ I couldn't understand that input."
                )
                return

            if parsed.get("status") == "unsupported_url":
                urls = "\n".join(
                    f"• {url}" for url in parsed.get("unsupported_urls", [])
                )
                embed = discord.Embed(
                    title="🔗 Game URL Required",
                    description=(
                        f"{parsed.get('message', 'That URL is not supported.')}\n\n"
                        f"**Unrecognized URL(s):**\n{urls}"
                    ),
                )
                await interaction.edit_original_response(
                    embed=embed,
                    content=None,
                    view=None,
                )
                return

            status_embed = discord.Embed(
                title="🎮 Analyzing Games...",
                description=(
                    "Inspecting the actual media, sampling the full video for OCR, "
                    "checking visual evidence, cross-referencing sources, and using "
                    "descriptions only as a last resort."
                ),
            )
            await interaction.edit_original_response(
                embed=status_embed,
                content=None,
                view=None,
            )

            result = await analyze_game_input(parsed)
            installed = await cog.installed_game_names()
            games = result.get("games") or []

            from events.on_message import GameSelectionView

            view = GameSelectionView(cog, games, installed) if games else None
            result_embed = cog.create_result_embed(
                result,
                installed,
                input_text or (attachment.filename if attachment else None),
            )

            await interaction.edit_original_response(
                embed=result_embed,
                view=view,
                content=None,
            )

        except Exception as error:
            from utils.helper import log
            log(
                f"Ephemeral game analysis error | user={interaction.user.id} | "
                f"{type(error).__name__}: {error}"
            )
            embed = discord.Embed(
                title="❌ Game Detection Failed",
                description="Something went wrong while analyzing that content.",
            )
            await interaction.edit_original_response(
                embed=embed,
                content=None,
                view=None,
            )

    @app_commands.command(
        name="downloaded",
        description="Mark a queued game as downloaded.",
    )
    @app_commands.describe(identifier="Queue ID or game name")
    async def downloaded(self, interaction: discord.Interaction, identifier: str):
        if not await self._authorized(interaction):
            await interaction.response.send_message(
                "❌ You are not authorized to manage the download queue.",
                ephemeral=True,
            )
            return

        from processors.game_queue import complete_queue_item
        item = await complete_queue_item(identifier, "downloaded", "Downloaded")
        if item is None:
            await interaction.response.send_message(
                f"❌ No queued game matched `{identifier}`.",
                ephemeral=True,
            )
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
            await interaction.response.send_message(
                "❌ You are not authorized to manage the download queue.",
                ephemeral=True,
            )
            return

        item = await remove_queue_item(identifier, reason or "")
        if item is None:
            await interaction.response.send_message(
                f"❌ No queued game matched `{identifier}`.",
                ephemeral=True,
            )
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
            await interaction.response.send_message(
                "❌ You are not authorized to manage the download queue.",
                ephemeral=True,
            )
            return

        record = await blacklist_game(identifier, reason)
        if record is None:
            await interaction.response.send_message(
                f"❌ Could not blacklist `{identifier}`.",
                ephemeral=True,
            )
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
