import discord


def install_game_selection_close_button():
    """Add a small Close button to every GameSelectionView instance.

    Kept as a patch module so the existing selection logic stays untouched.
    The button only removes the UI controls; the result embed remains visible.
    """
    from events.on_message import GameSelectionView

    if getattr(GameSelectionView, "_close_button_installed", False):
        return

    original_init = GameSelectionView.__init__

    async def close_ui(interaction: discord.Interaction):
        # The selection panel is delivered privately (DM or ephemeral).
        # Remove only its controls so the analysis result remains available.
        if interaction.response.is_done():
            try:
                await interaction.message.edit(view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            return

        await interaction.response.edit_message(view=None)

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
