from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent

COMMANDS_DIR = ROOT / "commands"
SERVER_DIR = ROOT / "server"
TEMPLATES_DIR = ROOT / "templates"


FILES = {
    "server/__init__.py": "",

    "server/connections.py": r'''
import asyncio
import secrets
import time
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from typing import Optional


@dataclass
class Connection:
    user_id: int
    token: str
    ip: str
    created_at: float
    last_seen: float


class ConnectionManager:
    def __init__(
        self,
        bot,
        role_id: int,
        allowed_network: str,
        timeout: int = 600,
    ):
        self.bot = bot
        self.role_id = role_id
        self.allowed_network = ip_network(
            allowed_network,
            strict=False,
        )

        self.timeout = timeout

        self.connections: dict[int, Connection] = {}
        self.tokens: dict[str, int] = {}

        self.cleanup_task: Optional[asyncio.Task] = None

    # ---------------------------------------------------------
    # TOKEN MANAGEMENT
    # ---------------------------------------------------------

    def create_token(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)

        self.tokens[token] = user_id

        return token

    def get_user_from_token(self, token: str) -> Optional[int]:
        return self.tokens.get(token)

    # ---------------------------------------------------------
    # CONNECTION
    # ---------------------------------------------------------

    def is_allowed_ip(self, ip: str) -> bool:
        try:
            address = ip_address(ip)
            return address in self.allowed_network
        except ValueError:
            return False

    def connect(
        self,
        user_id: int,
        token: str,
        ip: str,
    ) -> bool:

        if not self.is_allowed_ip(ip):
            return False

        now = time.time()

        connection = Connection(
            user_id=user_id,
            token=token,
            ip=ip,
            created_at=now,
            last_seen=now,
        )

        self.connections[user_id] = connection

        return True

    def heartbeat(
        self,
        token: str,
        ip: str,
    ) -> bool:

        user_id = self.get_user_from_token(token)

        if user_id is None:
            return False

        connection = self.connections.get(user_id)

        if connection is None:
            return False

        # Make sure the current connection is still coming
        # from the allowed LAN.
        if not self.is_allowed_ip(ip):
            return False

        # Update the address in case DHCP changed it.
        connection.ip = ip
        connection.last_seen = time.time()

        return True

    # ---------------------------------------------------------
    # ROLE MANAGEMENT
    # ---------------------------------------------------------

    async def grant_role(self, user_id: int):
        role = self.bot.get_role(self.role_id)

        if role is None:
            return

        for guild in self.bot.guilds:
            member = guild.get_member(user_id)

            if member is None:
                continue

            if role not in member.roles:
                try:
                    await member.add_roles(
                        role,
                        reason="Home LAN connection established",
                    )
                except Exception as e:
                    print(
                        f"[Connection] Failed to grant role "
                        f"to {member}: {e}"
                    )

    async def remove_role(self, user_id: int):
        role = self.bot.get_role(self.role_id)

        if role is None:
            return

        for guild in self.bot.guilds:
            member = guild.get_member(user_id)

            if member is None:
                continue

            if role in member.roles:
                try:
                    await member.remove_roles(
                        role,
                        reason="Home LAN connection expired",
                    )
                except Exception as e:
                    print(
                        f"[Connection] Failed to remove role "
                        f"from {member}: {e}"
                    )

    # ---------------------------------------------------------
    # CLEANUP
    # ---------------------------------------------------------

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(60)

            now = time.time()

            expired = []

            for user_id, connection in list(
                self.connections.items()
            ):
                if now - connection.last_seen > self.timeout:
                    expired.append(user_id)

            for user_id in expired:
                self.connections.pop(user_id, None)

                await self.remove_role(user_id)

                print(
                    f"[Connection] User {user_id} "
                    f"disconnected from LAN"
                )

    def start_cleanup(self):
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(
                self.cleanup_loop()
            )
''',

    "commands/connection.py": r'''
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
        manager = self.cog.manager

        token = manager.create_token(
            interaction.user.id
        )

        url = (
            f"{self.cog.base_url}"
            f"/connect/{token}"
        )

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
    @app_commands.default_permissions(
        administrator=True
    )
    async def connection_panel(
        self,
        interaction: discord.Interaction,
    ):
        embed = discord.Embed(
            title="🏠 Home LAN Access",
            description=(
                "Connect to the home LAN to receive "
                "the connected role.\n\n"
                "Your role remains active while your "
                "LAN connection is detected."
            ),
            color=discord.Color.green(),
        )

        embed.set_footer(
            text="Home LAN connection"
        )

        await interaction.channel.send(
            embed=embed,
            view=ConnectionView(self),
        )

        await interaction.response.send_message(
            "✅ Connection panel created.",
            ephemeral=True,
        )


async def setup(
    bot: commands.Bot,
    manager=None,
    base_url="http://192.168.28.7:9999",
):
    cog = Connection(
        bot,
        manager,
        base_url,
    )

    await bot.add_cog(cog)

    # Persistent button
    bot.add_view(
        ConnectionView(cog)
    )
''',

    "templates/connect.html": r'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">

    <meta
        name="viewport"
        content="width=device-width, initial-scale=1.0"
    >

    <title>Home LAN Connection</title>

    <style>
        body {
            margin: 0;
            min-height: 100vh;

            display: flex;
            align-items: center;
            justify-content: center;

            background: #111827;
            color: white;

            font-family:
                Arial,
                Helvetica,
                sans-serif;
        }

        .card {
            width: min(90%, 500px);

            padding: 40px;

            background: #1f2937;
            border-radius: 16px;

            text-align: center;

            box-shadow:
                0 20px 50px rgba(0, 0, 0, 0.4);
        }

        .icon {
            font-size: 60px;
            margin-bottom: 20px;
        }

        h1 {
            margin-bottom: 10px;
        }

        #status {
            margin-top: 25px;
            padding: 15px;

            border-radius: 10px;

            background: #374151;
        }

        .connected {
            background: #064e3b !important;
        }

        .error {
            background: #7f1d1d !important;
        }
    </style>
</head>

<body>

<div class="card">

    <div class="icon">
        🏠
    </div>

    <h1>Home LAN Connection</h1>

    <p>
        This page keeps your Discord Home LAN
        connection active.
    </p>

    <div id="status">
        Connecting...
    </div>

</div>

<script>

const statusElement =
    document.getElementById("status");

const token =
    window.location.pathname
        .split("/")
        .filter(Boolean)
        .pop();


async function heartbeat() {

    try {

        const response = await fetch(
            `/api/heartbeat/${token}`,
            {
                method: "POST",
                cache: "no-store"
            }
        );

        const data =
            await response.json();

        if (data.connected) {

            statusElement.textContent =
                "🟢 Connected to the Home LAN";

            statusElement.className =
                "connected";

        } else {

            statusElement.textContent =
                "🔴 Connection rejected";

            statusElement.className =
                "error";
        }

    } catch (error) {

        statusElement.textContent =
            "🔴 Server connection lost";

        statusElement.className =
            "error";
    }
}


heartbeat();

// Refresh every 60 seconds.
setInterval(
    heartbeat,
    60 * 1000
);

</script>

</body>
</html>
'''
}


def write_files():
    print("[+] Creating connection system...")

    for relative, content in FILES.items():

        path = ROOT / relative

        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        path.write_text(
            content,
            encoding="utf-8",
        )

        print(f"[+] Created: {relative}")


def update_config():
    path = ROOT / "config.py"

    if not path.exists():
        print("[!] config.py does not exist.")
        return

    content = path.read_text(
        encoding="utf-8"
    )

    additions = '''

# Home LAN connection configuration

LAN_WEB_HOST = "0.0.0.0"
LAN_WEB_PORT = 9999

# URL users will receive from Discord.
LAN_BASE_URL = "http://192.168.28.7:9999"

# Your home LAN subnet.
LAN_NETWORK = "192.168.28.0/24"

# Discord role that users receive while connected.
HOME_ROLE_ID = 0

# Seconds before a connection is considered dead.
LAN_CONNECTION_TIMEOUT = 600
'''

    if "LAN_WEB_HOST" not in content:
        content += additions

        path.write_text(
            content,
            encoding="utf-8",
        )

        print("[+] Updated config.py")
    else:
        print("[=] config.py already contains LAN config")


def update_bot():
    path = ROOT / "bot.py"

    if not path.exists():
        print("[!] bot.py does not exist.")
        return

    content = path.read_text(
        encoding="utf-8"
    )

    # Add imports
    if "from config import TOKEN" in content:
        content = content.replace(
            "from config import TOKEN",
            """from config import (
    TOKEN,
    LAN_WEB_HOST,
    LAN_WEB_PORT,
    LAN_BASE_URL,
    LAN_NETWORK,
    HOME_ROLE_ID,
    LAN_CONNECTION_TIMEOUT,
)

from server.connections import ConnectionManager
""",
        )

    # Add manager creation
    marker = """class Bot(commands.Bot):
    def __init__(self):
"""

    replacement = """class Bot(commands.Bot):
    def __init__(self):
"""

    # Don't modify class body here if already installed.
    # Instead insert after super initialization.
    if "self.connection_manager" not in content:

        target = """        super().__init__(
            command_prefix="!",
            intents=intents
        )
"""

        replacement = target + """
        self.connection_manager = ConnectionManager(
            self,
            role_id=HOME_ROLE_ID,
            allowed_network=LAN_NETWORK,
            timeout=LAN_CONNECTION_TIMEOUT,
        )
"""

        if target in content:
            content = content.replace(
                target,
                replacement,
                1,
            )
        else:
            print(
                "[!] Could not locate Bot.__init__ "
                "super().__init__ block."
            )

    # Add extension loading
    if "commands.connection" not in content:

        target = """        await self.load_extension("commands.admin")
"""

        replacement = target + """
        await self.load_extension(
            "commands.connection",
            manager=self.connection_manager,
            base_url=LAN_BASE_URL,
        )
"""

        if target in content:
            content = content.replace(
                target,
                replacement,
                1,
            )

    # Start connection cleanup
    if "connection_manager.start_cleanup()" not in content:

        target = """        log("All extensions loaded.")
"""

        replacement = target + """
        self.connection_manager.start_cleanup()

"""

        if target in content:
            content = content.replace(
                target,
                replacement,
                1,
            )

    path.write_text(
        content,
        encoding="utf-8",
    )

    print("[+] Updated bot.py")


def update_requirements():
    path = ROOT / "requirements.txt"

    if not path.exists():
        path.write_text(
            "discord.py>=2.6\\naiohttp>=3.12\\n",
            encoding="utf-8",
        )

        print("[+] Created requirements.txt")
        return

    content = path.read_text(
        encoding="utf-8"
    )

    if "aiohttp" not in content.lower():

        if not content.endswith("\n"):
            content += "\n"

        content += "aiohttp>=3.12\n"

        path.write_text(
            content,
            encoding="utf-8",
        )

        print("[+] Added aiohttp to requirements.txt")
    else:
        print("[=] aiohttp already installed")


def main():
    print("=" * 60)
    print(" Home LAN Discord Connection Installer")
    print("=" * 60)
    print()

    if not (ROOT / "bot.py").exists():
        print(
            "[ERROR] bot.py was not found."
        )

        print(
            "Run this generator from the root "
            "of your existing Discord bot project."
        )

        return

    write_files()

    update_config()
    update_requirements()
    update_bot()

    print()
    print("=" * 60)
    print(" Installation complete!")
    print("=" * 60)

    print()
    print("Edit config.py:")
    print()
    print("  LAN_BASE_URL = "
          '"http://192.168.28.7:9999"')
    print('  LAN_NETWORK = "192.168.28.0/24"')
    print("  HOME_ROLE_ID = YOUR_ROLE_ID")
    print()
    print("Then install dependencies:")
    print()
    print("  pip install -r requirements.txt")
    print()
    print("Start the bot:")
    print()
    print("  py bot.py")
    print()
    print("Then use:")
    print()
    print("  /connection-panel")
    print()


if __name__ == "__main__":
    main()