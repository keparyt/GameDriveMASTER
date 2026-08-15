import logging
from pathlib import Path

from aiohttp import web


log = logging.getLogger("lan_web")


class LANWebServer:
    def __init__(self, manager, host: str, port: int):
        self.manager = manager
        self.host = host
        self.port = port
        self.runner = None
        self.site = None

        # aiohttp Application does not accept access_log. Access logging
        # is configured on AppRunner below.
        self.app = web.Application()

        self.app.router.add_get("/connect/{token}", self.connect)
        self.app.router.add_post("/api/heartbeat/{token}", self.heartbeat)
        self.app.router.add_get("/health", self.health)

    @staticmethod
    def client_ip(request: web.Request) -> str:
        return request.remote or "unknown"

    async def connect(self, request: web.Request):
        token = request.match_info["token"]
        user_id = self.manager.get_user_from_token(token)
        client_ip = self.client_ip(request)
        short_token = token[:8] + "..." if len(token) > 8 else token

        log.info(
            "CONNECT ip=%s token=%s user_id=%s path=%s",
            client_ip, short_token, user_id, request.path,
        )

        if user_id is None:
            log.warning("REJECTED reason=invalid_token ip=%s token=%s", client_ip, short_token)
            return web.Response(status=404, text="Invalid or expired connection token.")

        if client_ip == "unknown" or not self.manager.is_allowed_ip(client_ip):
            log.warning(
                "REJECTED reason=outside_lan ip=%s allowed_network=%s user_id=%s",
                client_ip, self.manager.allowed_network, user_id,
            )
            return web.Response(status=403, text="You must be connected to the home LAN.")

        success = self.manager.connect(user_id=user_id, token=token, ip=client_ip)

        if not success:
            log.warning("REJECTED reason=manager user_id=%s ip=%s", user_id, client_ip)
            return web.Response(status=403, text="Connection rejected.")

        await self.manager.grant_role(user_id)
        log.info("CONNECTED user_id=%s ip=%s role_granted=true", user_id, client_ip)

        template = Path(__file__).resolve().parent.parent / "templates" / "connect.html"

        if not template.exists():
            log.error("ERROR missing_template=%s", template)
            return web.Response(status=500, text="Connection page template is missing.")

        return web.FileResponse(template)

    async def heartbeat(self, request: web.Request):
        token = request.match_info["token"]
        client_ip = self.client_ip(request)
        connected = False

        if client_ip != "unknown":
            connected = self.manager.heartbeat(token, client_ip)

        user_id = self.manager.get_user_from_token(token)

        if connected and user_id is not None:
            await self.manager.grant_role(user_id)
            log.info("HEARTBEAT user_id=%s ip=%s status=OK", user_id, client_ip)
        else:
            log.warning("HEARTBEAT user_id=%s ip=%s status=REJECTED", user_id, client_ip)

        return web.json_response({"connected": connected})

    async def health(self, request: web.Request):
        log.info("HEALTH ip=%s status=OK", self.client_ip(request))
        return web.json_response({"status": "ok", "service": "home-lan-connection"})

    async def start(self):
        if self.runner is not None:
            log.warning("LAN WEB SERVER already running")
            return

        log.info("========================================")
        log.info("LAN WEB SERVER STARTING")
        log.info("Bind host: %s", self.host)
        log.info("Bind port: %s", self.port)
        log.info("Allowed network: %s", self.manager.allowed_network)
        log.info("========================================")

        try:
            self.runner = web.AppRunner(
                self.app,
                access_log=log,
                access_log_format='%a "%r" %s %b "%{Referer}i" "%{User-Agent}i"',
            )
            await self.runner.setup()

            log.info("Binding TCP listener on %s:%s...", self.host, self.port)

            self.site = web.TCPSite(
                self.runner,
                self.host,
                self.port,
                reuse_address=True,
            )
            await self.site.start()

            log.info("LAN WEB SERVER READY")
            log.info("Listening: http://%s:%s", self.host, self.port)
            log.info("Configured connection URL: %s/connect/<token>", self.manager.bot.lan_base_url.rstrip("/"))
            log.info("Health check URL: http://<LAN-IP>:%s/health", self.port)
            log.info("Waiting for LAN connection requests...")

        except Exception:
            log.exception("LAN WEB SERVER FAILED TO START")
            self.runner = None
            self.site = None
            raise

    async def stop(self):
        if self.runner is None:
            log.info("LAN WEB SERVER is not running")
            return

        log.info("LAN WEB SERVER STOPPING...")
        try:
            await self.runner.cleanup()
            log.info("LAN WEB SERVER STOPPED")
        finally:
            self.runner = None
            self.site = None
