import discord
from discord import app_commands
from discord.ext import commands


class ConnectionView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Connect",
        emoji="🔌",
        style=discord.ButtonStyle.success,
        custom_id="home_lan_connect",
    )
    async def connect_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        token = self.cog.manager.create_token(
            interaction.user.id
        )

        url = f"{self.cog.base_url}/connect/{token}"

        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="Open LAN Connection",
                emoji="🌐",
                style=discord.ButtonStyle.link,
                url=url,
            )
        )

        await interaction.response.send_message(
            "Click the button below while you are connected "
            "to the home LAN.",
            view=view,
            ephemeral=True,
        )


class Connection(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        manager,
        base_url: str,
    ):
        self.bot = bot
        self.manager = manager
        self.base_url = base_url.rstrip("/")

    @app_commands.command(
        name="connection-panel",
        description="Send the Home LAN connection panel.",
    )
    @app_commands.default_permissions(administrator=True)
    async def connection_panel(
        self,
        interaction: discord.Interaction,
    ):
        embed = discord.Embed(
            title="🏠 Home LAN Access",
            description=(
                "Connect to the home LAN to receive the connected role.\n\n"
                "Your role remains active while your LAN connection is detected."
            ),
            color=discord.Color.green(),
        )

        embed.set_footer(text="Home LAN connection")

        await interaction.channel.send(
            embed=embed,
            view=ConnectionView(self),
        )

        await interaction.response.send_message(
            "✅ Connection panel created.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    cog = Connection(
        bot,
        bot.connection_manager,
        bot.lan_base_url,
    )

    await bot.add_cog(cog)
    bot.add_view(ConnectionView(cog))
