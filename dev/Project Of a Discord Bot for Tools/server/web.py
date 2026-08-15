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

        self.app = web.Application(
            logger=log,
            access_log=log,
        )

        self.app.router.add_get(
            "/connect/{token}",
            self.connect,
        )
        self.app.router.add_post(
            "/api/heartbeat/{token}",
            self.heartbeat,
        )
        self.app.router.add_get(
            "/health",
            self.health,
        )

    @staticmethod
    def client_ip(request: web.Request) -> str:
        # request.remote is the address of the machine that directly
        # connected to this LAN server. Do not trust forwarded headers.
        return request.remote or "unknown"

    async def connect(self, request: web.Request):
        token = request.match_info["token"]
        user_id = self.manager.get_user_from_token(token)
        client_ip = self.client_ip(request)

        log.info(
            "CONNECT request: ip=%s token=%s user_id=%s",
            client_ip,
            token[:8] + "..." if len(token) > 8 else token,
            user_id,
        )

        if user_id is None:
            log.warning(
                "Rejected connection: invalid token from %s",
                client_ip,
            )
            return web.Response(
                status=404,
                text="Invalid or expired connection token.",
            )

        if client_ip == "unknown" or not self.manager.is_allowed_ip(client_ip):
            log.warning(
                "Rejected connection: IP %s is outside allowed LAN %s",
                client_ip,
                self.manager.allowed_network,
            )
            return web.Response(
                status=403,
                text="You must be connected to the home LAN.",
            )

        success = self.manager.connect(
            user_id=user_id,
            token=token,
            ip=client_ip,
        )

        if not success:
            log.warning(
                "Connection manager rejected user_id=%s ip=%s",
                user_id,
                client_ip,
            )
            return web.Response(
                status=403,
                text="Connection rejected.",
            )

        await self.manager.grant_role(user_id)

        log.info(
            "LAN connection established: user_id=%s ip=%s",
            user_id,
            client_ip,
        )

        template = (
            Path(__file__).resolve().parent.parent
            / "templates"
            / "connect.html"
        )

        if not template.exists():
            log.error("Missing connection template: %s", template)
            return web.Response(
                status=500,
                text="Connection page template is missing.",
            )

        return web.FileResponse(template)

    async def heartbeat(self, request: web.Request):
        token = request.match_info["token"]
        client_ip = self.client_ip(request)

        connected = False

        if client_ip != "unknown":
            connected = self.manager.heartbeat(
                token,
                client_ip,
            )

        user_id = self.manager.get_user_from_token(token)

        if connected and user_id is not None:
            await self.manager.grant_role(user_id)
            log.info(
                "HEARTBEAT: user_id=%s ip=%s OK",
                user_id,
                client_ip,
            )
        else:
            log.warning(
                "HEARTBEAT: ip=%s user_id=%s rejected",
                client_ip,
                user_id,
            )

        return web.json_response({"connected": connected})

    async def health(self, request: web.Request):
        return web.json_response(
            {
                "status": "ok",
                "service": "home-lan-connection",
            }
        )

    async def start(self):
        if self.runner is not None:
            log.warning("LAN web server is already running")
            return

        log.info("========================================")
        log.info("Starting LAN web server")
        log.info("Host: %s", self.host)
        log.info("Port: %s", self.port)
        log.info("Allowed network: %s", self.manager.allowed_network)
        log.info("========================================")

        try:
            self.runner = web.AppRunner(
                self.app,
                access_log=log,
            )
            await self.runner.setup()

            self.site = web.TCPSite(
                self.runner,
                self.host,
                self.port,
                reuse_address=True,
            )

            await self.site.start()

            log.info(
                "LAN WEB SERVER READY: http://%s:%s",
                self.host,
                self.port,
            )
            log.info(
                "LAN connection URL: configured LAN_BASE_URL + /connect/<token>"
            )
            log.info(
                "Health check: http://<server-ip>:%s/health",
                self.port,
            )

        except Exception:
            log.exception("FAILED TO START LAN WEB SERVER")
            self.runner = None
            self.site = None
            raise

    async def stop(self):
        if self.runner is None:
            return

        log.info("Stopping LAN web server...")

        try:
            await self.runner.cleanup()
            log.info("LAN web server stopped")
        finally:
            self.runner = None
            self.site = None
