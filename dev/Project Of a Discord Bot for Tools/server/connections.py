import asyncio
import os
import secrets
import time
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from typing import Optional

from config import LAN_PING_INTERVAL, LAN_PING_TIMEOUT


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
        self.ping_interval = LAN_PING_INTERVAL
        self.ping_timeout = LAN_PING_TIMEOUT

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

    async def ping_ip(self, ip: str) -> bool:
        if not self.is_allowed_ip(ip):
            return False
        if os.name == "nt":
            command = ["ping", "-n", "1", "-w", str(self.ping_timeout * 1000), ip]
        else:
            command = ["ping", "-c", "1", "-W", str(self.ping_timeout), ip]
        try:
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            return await process.wait() == 0
        except (OSError, asyncio.TimeoutError):
            return False

    def _get_role(self):
        for guild in self.bot.guilds:
            role = guild.get_role(self.role_id)
            if role is not None:
                return role
        return None

    async def _send_dm(self, user_id: int, message: str):
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(message)
            print(f"[Connection] DM sent to {user_id}")
        except Exception as exc:
            print(f"[Connection] Failed to DM {user_id}: {exc}")

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
            except Exception as exc:
                print(f"[Connection] Could not find user {user_id} in guild {guild.id}: {exc}")
                return
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Home LAN connection established")
                print(f"[Connection] Granted role '{role.name}' to {member} ({user_id})")
            except Exception as exc:
                print(f"[Connection] Failed to grant role '{role.name}' to {member}: {exc}")
                return
        connection = self.connections.get(user_id)
        if connection and not getattr(connection, "dm_notified", False):
            connection.dm_notified = True
            await self._send_dm(user_id, f"🏠 **Home LAN connected**\n\nYour Home LAN access is now active.\nDetected LAN IP: `{connection.ip}`")

    async def remove_role(self, user_id: int):
        role = self._get_role()
        if role is None:
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
            except Exception as exc:
                print(f"[Connection] Failed to remove role '{role.name}' from {member}: {exc}")
        await self._send_dm(user_id, "🔴 **Home LAN disconnected**\n\nYour Home LAN device is no longer reachable on the LAN, so your Home LAN access role was removed.")

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(self.ping_interval)
            for user_id, connection in list(self.connections.items()):
                reachable = await self.ping_ip(connection.ip)
                if reachable:
                    connection.last_seen = time.time()
                    await self.grant_role(user_id)
                    continue
                elapsed = time.time() - connection.last_seen
                if elapsed > self.timeout:
                    self.connections.pop(user_id, None)
                    await self.remove_role(user_id)
                    print(f"[Connection] User {user_id} disconnected from LAN")

    def start_cleanup(self):
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(self.cleanup_loop())
