import json
import logging
import sys
import threading
import traceback
from pathlib import Path

import uvicorn

from .api import create_app
from .database import Database
from .scanner import Scanner


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


class DisabledMetadataManager:
    """Fallback metadata provider so the web API can start without config.py."""

    enabled = False
    auto_lookup = False

    def __init__(self, reason=None):
        import queue

        self._queue = queue.Queue()
        self.reason = reason

    def queue_lookup(self, game_id, game_name):
        return False

    def stop(self):
        return None


def load_config():
    log = logging.getLogger("gamelibrary.service")
    log.debug("Loading service config: %s", CONFIG_PATH)
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config.json: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def setup_logging():
    handlers = [
        logging.FileHandler(LOG_DIR / "service.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("gamelibrary.service").setLevel(logging.DEBUG)


def create_metadata_manager(config):
    """Load SteamGridDB support without making it a hard startup dependency."""
    log = logging.getLogger("gamelibrary.service")
    metadata_config = config.get("metadata", {})

    try:
        # Import lazily so a missing local config.py/API key cannot prevent the
        # actual Game Library web API from starting.
        from .metadata import MetadataManager

        metadata = MetadataManager(Database(), metadata_config)
        return metadata
    except Exception as exc:
        log.exception("SteamGridDB metadata initialization failed; continuing without artwork: %s", exc)
        return DisabledMetadataManager(str(exc))


def main():
    setup_logging()
    log = logging.getLogger("gamelibrary.service")
    log.info("========== Game Library backend starting ==========")
    log.info("Python executable: %s", sys.executable)
    log.info("Base directory: %s", BASE_DIR)
    log.info("Config path: %s", CONFIG_PATH)

    database = None
    metadata = None
    stop_event = threading.Event()

    try:
        config = load_config()
        log.debug("config.json loaded successfully")

        log.debug("Initializing database")
        database = Database()
        log.debug("Database initialized")

        log.debug("Initializing scanner")
        scanner = Scanner(database, config)
        log.debug("Scanner initialized")

        metadata_config = config.get("metadata", {})
        try:
            from .metadata import MetadataManager
            metadata = MetadataManager(database, metadata_config)
            log.info(
                "Metadata manager initialized: enabled=%s auto_lookup=%s",
                metadata.enabled,
                metadata.auto_lookup,
            )
        except Exception as exc:
            # Artwork is optional. A broken/missing local SteamGridDB config must
            # never take down the HTTP server or web panel.
            log.exception(
                "Metadata manager initialization failed; starting API without artwork: %s",
                exc,
            )
            metadata = DisabledMetadataManager(str(exc))

        def scan_loop():
            interval = max(2, int(config.get("scan_interval_seconds", 10)))
            log.info("Scanner worker started: interval=%ss", interval)
            while not stop_event.is_set():
                try:
                    log.debug("Starting scanner cycle")
                    scanner.scan()
                    log.debug("Scanner cycle complete")
                    if metadata.enabled and metadata.auto_lookup:
                        rows = database.search(connected_only=True)
                        queued = 0
                        for row in rows:
                            if not row["capsule"]:
                                if metadata.queue_lookup(row["id"], row["name"]):
                                    queued += 1
                        log.debug(
                            "Artwork queue update: queued=%s pending=%s",
                            queued,
                            metadata._queue.qsize(),
                        )
                except Exception:
                    log.exception("Scanner cycle failed")
                stop_event.wait(interval)

        thread = threading.Thread(target=scan_loop, daemon=True, name="GameScanner")
        thread.start()

        app = create_app(database, metadata)
        host = config.get("api_host", "127.0.0.1")
        port = int(config.get("api_port", 8765))
        log.info("Game Library API running at http://%s:%s", host, port)
        log.info("Web panel available at http://%s:%s/", host, port)

        try:
            uvicorn.run(
                app,
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
            )
        except Exception:
            log.exception("API server stopped unexpectedly")
            raise
    except Exception:
        log.exception("FATAL backend startup failure")
        traceback.print_exc()
        raise
    finally:
        stop_event.set()
        if metadata is not None:
            try:
                metadata.stop()
            except Exception:
                log.exception("Metadata shutdown failed")
        if database is not None:
            try:
                database.close()
            except Exception:
                log.exception("Database close failed")


if __name__ == "__main__":
    main()
