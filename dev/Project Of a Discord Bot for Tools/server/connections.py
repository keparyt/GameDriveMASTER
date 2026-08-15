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
        self.allowed_network = ip_network(allowed_network, strict=False)
        self.timeout = timeout
        self.connections: dict[int, Connection] = {}
        self.tokens: dict[str, int] = {}
        self.cleanup_task: Optional[asyncio.Task] = None

    def create_token(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        self.tokens[token] = user_id
        return token

    def get_user_from_token(self, token: str) -> Optional[int]:
        return self.tokens.get(token)

    def is_allowed_ip(self, ip: str) -> bool:
        try:
            return ip_address(ip) in self.allowed_network
        except ValueError:
            return False

    def connect(self, user_id: int, token: str, ip: str) -> bool:
        if not self.is_allowed_ip(ip):
            return False

        now = time.time()
        self.connections[user_id] = Connection(
            user_id=user_id,
            token=token,
            ip=ip,
            created_at=now,
            last_seen=now,
        )
        return True

    def heartbeat(self, token: str, ip: str) -> bool:
        user_id = self.get_user_from_token(token)
        if user_id is None:
            return False

        connection = self.connections.get(user_id)
        if connection is None:
            return False

        if not self.is_allowed_ip(ip):
            return False

        connection.ip = ip
        connection.last_seen = time.time()
        return True

    # ---------------------------------------------------------
    # ROLE MANAGEMENT
    # ---------------------------------------------------------

    def _get_role(self):
        """Get the configured role from any guild the bot is in.

        discord.py's Bot does not expose get_role(); Client does.
        Looking through guilds also works reliably when the bot is
        connected to multiple guilds and the configured role exists
        in one of them.
        """
        for guild in self.bot.guilds:
            role = guild.get_role(self.role_id)
            if role is not None:
                return role

        return None

    async def grant_role(self, user_id: int):
        role = self._get_role()

        if role is None:
            print(
                f"[Connection] Role {self.role_id} was not found "
                "in any connected guild."
            )
            return

        guild = role.guild
        member = guild.get_member(user_id)

        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception as e:
                print(
                    f"[Connection] Could not find user {user_id} "
                    f"in guild {guild.id}: {e}"
                )
                return

        if role not in member.roles:
            try:
                await member.add_roles(
                    role,
                    reason="Home LAN connection established",
                )
                print(
                    f"[Connection] Granted role '{role.name}' "
                    f"to {member} ({user_id})"
                )
            except Exception as e:
                print(
                    f"[Connection] Failed to grant role "
                    f"'{role.name}' to {member}: {e}"
                )

    async def remove_role(self, user_id: int):
        role = self._get_role()

        if role is None:
            print(
                f"[Connection] Role {self.role_id} was not found "
                "while removing access."
            )
            return

        guild = role.guild
        member = guild.get_member(user_id)

        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                return

        if role in member.roles:
            try:
                await member.remove_roles(
                    role,
                    reason="Home LAN connection expired",
                )
                print(
                    f"[Connection] Removed role '{role.name}' "
                    f"from {member} ({user_id})"
                )
            except Exception as e:
                print(
                    f"[Connection] Failed to remove role "
                    f"'{role.name}' from {member}: {e}"
                )

    # ---------------------------------------------------------
    # CLEANUP
    # ---------------------------------------------------------

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(60)

            now = time.time()
            expired = []

            for user_id, connection in list(self.connections.items()):
                if now - connection.last_seen > self.timeout:
                    expired.append(user_id)

            for user_id in expired:
                self.connections.pop(user_id, None)
                await self.remove_role(user_id)
                print(
                    f"[Connection] User {user_id} disconnected from LAN"
                )

    def start_cleanup(self):
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(
                self.cleanup_loop()
            )
