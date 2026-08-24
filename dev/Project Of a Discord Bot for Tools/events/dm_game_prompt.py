import discord
from discord.ext import commands

from utils.embed_style import INFO, SUCCESS, panel
from utils.helper import log

DM_PARSE_TIMEOUT = 24 * 60 * 60


class DMParseView(discord.ui.View):
    """Ask separately for every DM whether that exact message should be game-parsed."""

    def __init__(self, cog: commands.Cog, source_message: discord.Message):
        super().__init__(timeout=DM_PARSE_TIMEOUT)
        self.cog = cog
        self.source_message = source_message
        self.owner_id = source_message.author.id
        self.prompt_message: discord.Message | None = None
        self.handled = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("This prompt belongs to another user.", ephemeral=True)
        return False

    async def _finish(self, interaction: discord.Interaction, title: str, description: str, color: int):
        self.handled = True
        for child in self.children:
            child.disabled = True
        self.stop()
        embed = panel(title, description, color=color, footer="DM message handling")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Yes, parse this message", style=discord.ButtonStyle.success, emoji="🎮")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(
            interaction,
            "🎮 Game parsing started",
            "Parsing this exact DM for supported games and media. Your other DMs are handled independently and will each ask again.",
            SUCCESS,
        )
        game_cog = self.cog.bot.get_cog("OnMessage")
        if game_cog is None:
            log(f"DM game parser unavailable | message={self.source_message.id}")
            return
        try:
            await game_cog.handle_game_detection(self.source_message)
        except Exception as exc:
            log(f"DM game parsing error | message={self.source_message.id} | {type(exc).__name__}: {exc}")

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(
            interaction,
            "Message not parsed",
            "Okay — this DM will not be sent to the game parser. If another bot feature or command applies, it can still handle the message normally.",
            INFO,
        )

    async def on_timeout(self):
        if self.handled:
            return
        for child in self.children:
            child.disabled = True
        if self.prompt_message is not None:
            try:
                embed = panel(
                    "Game parsing request expired",
                    "No choice was made for this DM, so it was not parsed. Every new DM gets its own separate prompt.",
                    color=INFO,
                    footer="DM message handling",
                )
                await self.prompt_message.edit(embed=embed, view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class DMGamePrompt(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is not None:
            return

        # Ask for EVERY individual DM. Do not infer consent from a previous DM,
        # even if the previous one contained a game URL or media.
        if not isinstance(message.channel, discord.DMChannel):
            return

        embed = panel(
            "🎮 Parse this message for games?",
            "Do you want me to analyze **this exact DM** for game names/media?\n\n"
            "Each DM is handled independently, so I will ask again for your next message.",
            color=INFO,
            footer="Choose Yes to parse this message • No leaves it unparsed",
        )
        try:
            view = DMParseView(self, message)
            prompt = await message.channel.send(embed=embed, view=view)
            view.prompt_message = prompt
            log(f"DM game parse prompt created | message={message.id}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log(f"DM game parse prompt error | message={message.id} | {type(exc).__name__}: {exc}")


async def setup(bot: commands.Bot):
    await bot.add_cog(DMGamePrompt(bot))
