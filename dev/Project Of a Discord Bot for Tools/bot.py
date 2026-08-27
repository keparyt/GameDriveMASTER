import asyncio
import logging

import discord
from discord.ext import commands

from config import (
    TOKEN,
    COMMAND_PREFIX,
    LAN_WEB_HOST,
    LAN_WEB_PORT,
    LAN_BASE_URL,
    LAN_NETWORK,
    HOME_ROLE_ID,
    LAN_CONNECTION_TIMEOUT,
    MASTER_ID,
    LOG_LEVEL,
    LOG_FORMAT,
    LOG_DATE_FORMAT,
)
from server.connections import ConnectionManager
from server.web import LANWebServer
from utils.helper import log

# Install the hardened evidence/candidate pipeline before the game event cog is created.
from processors import game_media_analyzer as _game_media_analyzer
from processors.game_detector_hardening import (
    _identify_from_evidence as _identify_game_titles,
    close_http_sessions as _close_game_detector_sessions,
)
from events.on_message import OnMessage
from utils.game_result_embed import create_result_embed as _create_game_result_embed

_game_media_analyzer._identify_from_evidence = _identify_game_titles


class GameResultOnMessage(OnMessage):
    """OnMessage with its shared result renderer explicitly composed in."""

    __cog_name__ = "OnMessage"
    create_result_embed = staticmethod(_create_game_result_embed)


logging.basicConfig(
    level=getattr(logging, str(LOG_LEVEL).upper(), logging.INFO),
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
)


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=COMMAND_PREFIX, intents=intents)

        self.master_id = MASTER_ID
        self.connection_manager = ConnectionManager(
            self,
            role_id=HOME_ROLE_ID,
            allowed_network=LAN_NETWORK,
            timeout=LAN_CONNECTION_TIMEOUT,
        )
        self.lan_base_url = LAN_BASE_URL
        self.lan_web = LANWebServer(self.connection_manager, LAN_WEB_HOST, LAN_WEB_PORT)

    async def setup_hook(self):
        log("Loading commands...")
        await self.load_extension("commands.connection")
        await self.load_extension("commands.games")

        log("Loading events...")
        await self.load_extension("events.on_ready")
        await self.add_cog(GameResultOnMessage(self))
        await self.load_extension("events.dm_game_prompt")
        log("All extensions loaded.")

        await self.lan_web.start()
        self.connection_manager.start_cleanup()
        synced = await self.tree.sync()
        log(f"Synced {len(synced)} slash command(s).")

    async def close(self):
        log("Shutting down game detector HTTP clients...")
        try:
            await _close_game_detector_sessions()
        except Exception as exc:
            log(f"Game detector HTTP cleanup error | {type(exc).__name__}: {exc}")
        log("Shutting down LAN web server...")
        await self.lan_web.stop()
        await super().close()


async def main():
    bot = Bot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
