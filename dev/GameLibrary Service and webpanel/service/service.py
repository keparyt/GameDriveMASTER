import json
import logging
import sys
import threading
from pathlib import Path

import uvicorn

from .api import create_app
from .database import Database
from .metadata import MetadataManager
from .scanner import Scanner


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def setup_logging():
    handlers = [
        logging.FileHandler(LOG_DIR / "service.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers
    )


def main():
    setup_logging()
    log = logging.getLogger("gamelibrary.service")
    config = load_config()
    database = Database()
    scanner = Scanner(database, config)
    metadata = MetadataManager(database, config.get("metadata", {}))

    stop_event = threading.Event()

    def scan_loop():
        interval = max(2, int(config.get("scan_interval_seconds", 10)))
        while not stop_event.is_set():
            try:
                scanner.scan()
                if metadata.enabled and config.get("metadata", {}).get("auto_lookup", True):
                    for row in database.search(connected_only=True):
                        if not row["capsule"]:
                            metadata.lookup(row["id"], row["name"])
            except Exception:
                log.exception("Scanner cycle failed")
            stop_event.wait(interval)

    thread = threading.Thread(target=scan_loop, daemon=True, name="GameScanner")
    thread.start()

    app = create_app(database, metadata)
    host = config.get("api_host", "127.0.0.1")
    port = int(config.get("api_port", 8765))
    log.info("Game Library API running at http://%s:%s", host, port)

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    except Exception:
        log.exception("API server stopped unexpectedly")
        raise
    finally:
        stop_event.set()
        try:
            database.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
