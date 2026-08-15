import json
import logging
import sys
import threading
import traceback
from pathlib import Path

import uvicorn

from .api import create_app
from .database import Database
from .playnite import PlayniteBridge
from .scanner import Scanner

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

class DisabledMetadataManager:
    enabled = False
    auto_lookup = False
    def __init__(self, reason=None):
        import queue
        self._queue = queue.Queue()
        self.reason = reason
    def queue_lookup(self, game_id, game_name): return False
    def stop(self): return None

def load_config():
    if not CONFIG_PATH.exists(): raise FileNotFoundError(f"Missing config.json: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

def setup_logging():
    handlers = [logging.FileHandler(LOG_DIR / "service.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", handlers=handlers, force=True)
    logging.getLogger("gamelibrary.service").setLevel(logging.DEBUG)

def main():
    setup_logging()
    log = logging.getLogger("gamelibrary.service")
    database = None
    metadata = None
    stop_event = threading.Event()
    try:
        config = load_config()
        database = Database()
        scanner = Scanner(database, config)
        playnite = PlayniteBridge(config.get("playnite", {}))
        log.info("Playnite bridge: enabled=%s available=%s source=%s", playnite.enabled, playnite.available, "PlayniteApi.Database.Games")
        if playnite.enabled:
            try: playnite.refresh(force=True)
            except Exception: log.exception("Initial Playnite refresh failed; continuing without Playnite")
        try:
            from .metadata import MetadataManager
            metadata = MetadataManager(database, config.get("metadata", {}))
        except Exception as exc:
            log.exception("Metadata manager initialization failed; starting API without artwork: %s", exc)
            metadata = DisabledMetadataManager(str(exc))

        def drive_signature():
            with database.lock:
                drives = database.conn.execute("SELECT uuid,name,last_letter,connected FROM drives ORDER BY uuid").fetchall()
                games = database.conn.execute("SELECT drive_id,relative_path,name FROM games ORDER BY drive_id,relative_path").fetchall()
            return tuple((r["uuid"], r["name"], r["last_letter"], bool(r["connected"])) for r in drives), tuple((r["drive_id"], r["relative_path"], r["name"]) for r in games)

        def scan_loop():
            interval = max(2, int(config.get("scan_interval_seconds", 10)))
            previous_signature = drive_signature()
            while not stop_event.is_set():
                try:
                    scanner.scan()
                    current_signature = drive_signature()
                    if current_signature != previous_signature:
                        previous_signature = current_signature
                        if playnite.enabled and playnite.refresh_on_game_drive_change:
                            log.info("GameDrive state changed; requesting Playnite library refresh")
                            playnite.refresh(force=True)
                    else:
                        playnite.read_games()
                    if metadata.enabled and metadata.auto_lookup:
                        for row in database.search(connected_only=True):
                            if not row["capsule"]: metadata.queue_lookup(row["id"], row["name"])
                except Exception:
                    log.exception("Scanner cycle failed")
                stop_event.wait(interval)

        threading.Thread(target=scan_loop, daemon=True, name="GameScanner").start()
        app = create_app(database, metadata, config, playnite)
        host, port = config.get("api_host", "127.0.0.1"), int(config.get("api_port", 8765))
        log.info("Game Library API running at http://%s:%s", host, port)
        uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    except Exception:
        log.exception("FATAL backend startup failure")
        traceback.print_exc()
        raise
    finally:
        stop_event.set()
        if metadata is not None:
            try: metadata.stop()
            except Exception: log.exception("Metadata shutdown failed")
        if database is not None:
            try: database.close()
            except Exception: log.exception("Database close failed")

if __name__ == "__main__": main()
