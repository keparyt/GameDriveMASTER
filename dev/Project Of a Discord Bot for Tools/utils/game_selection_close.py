import discord


def install_game_selection_close_button():
    """Add a small Close button to every GameSelectionView instance.

    The button deletes the private result panel completely. This lets users
    dismiss a Games Identified panel before its normal 24-hour expiry.
    """
    from events.on_message import GameSelectionView

    if getattr(GameSelectionView, "_close_button_installed", False):
        return

    original_init = GameSelectionView.__init__

    async def close_ui(interaction: discord.Interaction):
        # A user explicitly closing the panel should remove both the embed and
        # its controls, rather than merely disabling/removing the controls.
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()

            await interaction.message.delete()
        except discord.NotFound:
            # Already expired or deleted; nothing else is required.
            return
        except (discord.Forbidden, discord.HTTPException):
            # Fallback for interaction message types Discord does not allow us
            # to delete directly: remove the UI so the panel cannot be used.
            try:
                await interaction.message.edit(view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    def patched_init(self, cog, games, installed_names):
        original_init(self, cog, games, installed_names)

        # A Select occupies the first row. Put the small close control below it.
        close_button = discord.ui.Button(
            label="Close",
            emoji="✖️",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        close_button.callback = close_ui
        self.close_button = close_button
        self.add_item(close_button)

    GameSelectionView.__init__ = patched_init
    GameSelectionView._close_button_installed = True
