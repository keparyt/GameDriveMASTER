
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
