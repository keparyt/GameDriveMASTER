import asyncio
import logging

import discord
from discord.ext import commands

from config import (
    TOKEN,
    LAN_WEB_HOST,
    LAN_WEB_PORT,
    LAN_BASE_URL,
    LAN_NETWORK,
    HOME_ROLE_ID,
    LAN_CONNECTION_TIMEOUT,
    MASTER_ID,
)

from server.connections import ConnectionManager
from server.web import LANWebServer
from utils.helper import log


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix="!",
            intents=intents,
        )

        self.master_id = MASTER_ID
        self.connection_manager = ConnectionManager(
            self,
            role_id=HOME_ROLE_ID,
            allowed_network=LAN_NETWORK,
            timeout=LAN_CONNECTION_TIMEOUT,
        )
        self.lan_base_url = LAN_BASE_URL
        self.lan_web = LANWebServer(
            self.connection_manager,
            LAN_WEB_HOST,
            LAN_WEB_PORT,
        )

    async def setup_hook(self):
        log("Loading commands...")

        await self.load_extension("commands.connection")
        await self.load_extension("commands.games")

        log("Loading events...")
        await self.load_extension("events.on_ready")
        await self.load_extension("events.on_message")
        await self.load_extension("events.dm_game_prompt")

        log("All extensions loaded.")

        await self.lan_web.start()
        self.connection_manager.start_cleanup()

        synced = await self.tree.sync()
        log(f"Synced {len(synced)} slash command(s).")

    async def close(self):
        log("Shutting down LAN web server...")
        await self.lan_web.stop()
        await super().close()


async def main():
    bot = Bot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
