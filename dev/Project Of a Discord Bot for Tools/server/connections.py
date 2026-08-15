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
    def __init__(self, bot, role_id: int, allowed_network: str, timeout: int = 600):
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
        self.connections[user_id] = Connection(user_id, token, ip, now, now)
        return True

    def heartbeat(self, token: str, ip: str) -> bool:
        user_id = self.get_user_from_token(token)
        if user_id is None:
            return False
        connection = self.connections.get(user_id)
        if connection is None or not self.is_allowed_ip(ip):
            return False
        connection.ip = ip
        connection.last_seen = time.time()
        return True

    def _get_role(self):
        for guild in self.bot.guilds:
            role = guild.get_role(self.role_id)
            if role is not None:
                return role
        return None

    async def _send_dm(self, user_id: int, message: str):
        try:
            user = self.bot.get_user(user_id)
            if user is None:
                user = await self.bot.fetch_user(user_id)
            await user.send(message)
            print(f"[Connection] DM sent to {user_id}")
        except Exception as e:
            print(f"[Connection] Failed to DM {user_id}: {e}")

    async def grant_role(self, user_id: int):
        role = self._get_role()
        if role is None:
            print(f"[Connection] Role {self.role_id} was not found in any connected guild.")
            return

        guild = role.guild
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception as e:
                print(f"[Connection] Could not find user {user_id} in guild {guild.id}: {e}")
                return

        already_connected = user_id in self.connections and role in member.roles

        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Home LAN connection established")
                print(f"[Connection] Granted role '{role.name}' to {member} ({user_id})")
            except Exception as e:
                print(f"[Connection] Failed to grant role '{role.name}' to {member}: {e}")
                return

        # Only notify when the user actually establishes a new connection,
        # not on every heartbeat.
        if not already_connected:
            connection = self.connections.get(user_id)
            ip = connection.ip if connection else "unknown"
            await self._send_dm(
                user_id,
                "🏠 **Home LAN connected**\n\n"
                f"Your Home LAN access is now active.\n"
                f"Detected LAN IP: `{ip}`",
            )

    async def remove_role(self, user_id: int):
        role = self._get_role()
        if role is None:
            print(f"[Connection] Role {self.role_id} was not found while removing access.")
            return

        guild = role.guild
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                member = None

        if member is not None and role in member.roles:
            try:
                await member.remove_roles(role, reason="Home LAN connection expired")
                print(f"[Connection] Removed role '{role.name}' from {member} ({user_id})")
            except Exception as e:
                print(f"[Connection] Failed to remove role '{role.name}' from {member}: {e}")

        await self._send_dm(
            user_id,
            "🔴 **Home LAN disconnected**\n\n"
            "Your Home LAN heartbeat expired, so your Home LAN access role was removed.",
        )

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(60)
            now = time.time()
            expired = [
                user_id
                for user_id, connection in list(self.connections.items())
                if now - connection.last_seen > self.timeout
            ]

            for user_id in expired:
                self.connections.pop(user_id, None)
                await self.remove_role(user_id)
                print(f"[Connection] User {user_id} disconnected from LAN")

    def start_cleanup(self):
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(self.cleanup_loop())
