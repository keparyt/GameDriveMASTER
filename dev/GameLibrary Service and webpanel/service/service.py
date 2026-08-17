import json
import logging
import subprocess
import sys
import threading
import time
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

    def queue_lookup(self, game_id, game_name):
        return False

    def stop(self):
        return None


def load_config():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config.json: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def setup_logging():
    handlers = [
        logging.FileHandler(LOG_DIR / "service.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
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

        if playnite.enabled:
            try:
                # If Playnite is already running, leave its current mode alone.
                # If it is not running, explicitly start it in Fullscreen mode.
                if playnite._is_running():
                    log.info("Playnite is already running; leaving its current mode unchanged")
                    playnite.start(wait_for_api=False)
                elif playnite.playnite_path and playnite.playnite_path.is_file():
                    subprocess.Popen(
                        [str(playnite.playnite_path), "--startfullscreen"],
                        cwd=str(playnite.playnite_path.parent),
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    playnite._startup_attempted = True
                    log.info("Playnite was not running; started it in Fullscreen mode: %s", playnite.playnite_path)
                else:
                    log.warning("Playnite executable not found: %s", playnite.playnite_path)
            except Exception:
                log.exception("Playnite startup failed; continuing and retrying in background")

            def wait_for_playnite_api():
                deadline = time.monotonic() + 90
                logged_wait = False
                while not stop_event.is_set() and time.monotonic() < deadline:
                    try:
                        if playnite._probe_api(timeout=1.0):
                            log.info("GameDrive Playnite API is ready")
                            for attempt in range(20):
                                games = playnite.read_games(force=True)
                                if games:
                                    log.info("Playnite library ready: %d installed game(s)", len(games))
                                    return
                                if attempt == 0:
                                    log.info("Playnite API is online; waiting for the Playnite library to finish loading...")
                                stop_event.wait(0.5)
                            log.warning("Playnite API is online but currently reports no installed games")
                            return
                        if not logged_wait:
                            log.info("Waiting for GameDrive Playnite API to become ready...")
                            logged_wait = True
                        if not playnite._is_running():
                            playnite.start(wait_for_api=False)
                    except Exception:
                        log.exception("Playnite readiness check failed")
                    stop_event.wait(0.5)
                if not stop_event.is_set():
                    log.warning("GameDrive Playnite API did not become ready within 90 seconds")

            threading.Thread(target=wait_for_playnite_api, daemon=True, name="PlayniteStartup").start()

        log.info(
            "Playnite bridge: enabled=%s available=%s executable=%s library=%s",
            playnite.enabled, playnite.available, playnite.playnite_path, playnite.library_path,
        )

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
            return (
                tuple((r["uuid"], r["name"], r["last_letter"], bool(r["connected"])) for r in drives),
                tuple((r["drive_id"], r["relative_path"], r["name"]) for r in games),
            )

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
                            # PlayniteBridge exposes read_games(force=True), not refresh().
                            # This schedules a fresh API read without blocking the scanner.
                            playnite.read_games(force=True)
                    else:
                        playnite.read_games()

                    if metadata.enabled and metadata.auto_lookup:
                        for row in database.search(connected_only=True):
                            if not row["capsule"]:
                                metadata.queue_lookup(row["id"], row["name"])
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
