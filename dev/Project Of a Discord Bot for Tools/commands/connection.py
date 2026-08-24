import discord
from discord import app_commands
from discord.ext import commands

from utils.embed_style import panel, success


class ConnectionView(discord.ui.View):
    """Persistent connection panel view."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Connect to LAN",
        emoji="🔌",
        style=discord.ButtonStyle.success,
        custom_id="home_lan_connect",
    )
    async def connect_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        token = self.cog.manager.create_token(interaction.user.id)
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

        embed = panel(
            "🔐 LAN Connection",
            "Use the button below while connected to the home network.\n\n"
            "Once the connection is detected, your connected role will be granted automatically.",
            footer="Private connection instructions",
        )
        embed.add_field(name="Status", value="Waiting for LAN connection…", inline=True)
        embed.add_field(name="Access", value="Home network only", inline=True)

        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True,
        )


class Connection(commands.Cog):
    def __init__(self, bot: commands.Bot, manager, base_url: str):
        self.bot = bot
        self.manager = manager
        self.base_url = base_url.rstrip("/")

    @app_commands.command(
        name="connection-panel",
        description="Send the Home LAN connection panel.",
    )
    async def connection_panel(self, interaction: discord.Interaction):
        if interaction.user.id != self.bot.master_id:
            await interaction.response.send_message(
                "❌ You are not authorized to use this command.",
                ephemeral=True,
            )
            return

        embed = panel(
            "🏠 Home LAN",
            "Connect your Discord account to the home network.\n\n"
            "When the LAN connection is detected, the connected role is kept active automatically.",
            footer="Home LAN access • Kepar Lab Assist",
        )
        embed.add_field(name="How it works", value="1. Click **Connect to LAN**\n2. Open the private link\n3. Stay on the home network", inline=False)
        embed.add_field(name="Privacy", value="Connection checks are limited to the configured home LAN.", inline=False)

        await interaction.channel.send(embed=embed, view=ConnectionView(self))
        await interaction.response.send_message(
            embed=success("Panel published", "The Home LAN connection panel is now available in this channel."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    cog = Connection(bot, bot.connection_manager, bot.lan_base_url)
    await bot.add_cog(cog)
    bot.add_view(ConnectionView(cog))
