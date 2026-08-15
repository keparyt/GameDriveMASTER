import logging
import queue
import threading
from pathlib import Path

import requests

import config as app_config

log = logging.getLogger("gamelibrary.metadata")


class MetadataManager:
    """Resolve SteamGridDB artwork in a background, one-at-a-time worker."""

    def __init__(self, db, config=None):
        self.db = db
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True) and app_config.STEAMGRIDDB_ENABLED and app_config.STEAMGRIDDB_API_KEY)
        self.auto_lookup = bool(self.config.get("auto_lookup", True))
        self.base_url = app_config.STEAMGRIDDB_BASE_URL.rstrip("/")
        self.cache_dir = (app_config.BASE_DIR / app_config.ARTWORK_CACHE_DIR).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {app_config.STEAMGRIDDB_API_KEY}", "User-Agent": "GameDrive/1.0"})
        self._queue = queue.Queue()
        self._queued = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, name="SteamGridDBWorker", daemon=True)
        self._worker.start()
        log.info("SteamGridDB initialized: enabled=%s auto_lookup=%s cache=%s", self.enabled, self.auto_lookup, self.cache_dir)

    @staticmethod
    def _safe_name(value):
        value = "".join(c if c.isalnum() or c in " ._-" else "_" for c in value)
        return value.strip(" .") or "unknown"

    def queue_lookup(self, game_id, game_name):
        if not self.enabled or not game_name:
            return False
        with self._lock:
            if game_id in self._queued:
                return False
            self._queued.add(game_id)
            self._queue.put((game_id, game_name))
        log.debug("Queued artwork lookup: id=%s name=%r queue=%s", game_id, game_name, self._queue.qsize())
        return True

    def _worker_loop(self):
        log.info("SteamGridDB artwork worker started")
        while not self._stop.is_set():
            try:
                game_id, game_name = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                log.info("Artwork lookup started: id=%s name=%r", game_id, game_name)
                self.lookup(game_id, game_name)
                log.info("Artwork lookup finished: id=%s name=%r", game_id, game_name)
            except Exception:
                log.exception("Artwork lookup crashed: id=%s name=%r", game_id, game_name)
            finally:
                with self._lock:
                    self._queued.discard(game_id)
                self._queue.task_done()

    def stop(self):
        self._stop.set()
        if self._worker.is_alive():
            self._worker.join(timeout=2)

    def _search(self, game_name):
        url = f"{self.base_url}/search/autocomplete/{requests.utils.quote(game_name, safe='')}"
        log.debug("SteamGridDB search: %s", url)
        response = self.session.get(url, timeout=15)
        log.debug("SteamGridDB search response: HTTP %s", response.status_code)
        response.raise_for_status()
        data = response.json().get("data", [])
        if not data:
            return None
        normalized = "".join(c.lower() for c in game_name if c.isalnum())
        for game in data:
            candidate = "".join(c.lower() for c in game.get("name", "") if c.isalnum())
            if candidate == normalized:
                return game
        return data[0]

    def _artwork_url(self, category, sgdb_id):
        url = f"{self.base_url}/{category}/game/{sgdb_id}?mimes=image/jpeg,image/png"
        log.debug("SteamGridDB artwork request: category=%s id=%s", category, sgdb_id)
        response = self.session.get(url, timeout=15)
        log.debug("SteamGridDB artwork response: HTTP %s", response.status_code)
        response.raise_for_status()
        data = response.json().get("data", [])
        return data[0].get("url") if data else None

    def _download(self, url, destination):
        if not url or destination.exists():
            return
        log.debug("Downloading artwork -> %s", destination)
        response = requests.get(url, timeout=30, headers={"User-Agent": "GameDrive/1.0"})
        response.raise_for_status()
        temp = destination.with_suffix(destination.suffix + ".tmp")
        temp.write_bytes(response.content)
        temp.replace(destination)
        log.debug("Artwork saved: %s (%d bytes)", destination, destination.stat().st_size)

    def lookup(self, game_id, game_name):
        if not self.enabled:
            return None
        try:
            game = self._search(game_name)
            if not game:
                log.info("SteamGridDB: no match for %r", game_name)
                return None
            sgdb_id = game.get("id")
            matched_name = game.get("name") or game_name
            safe_name = self._safe_name(matched_name)
            game_dir = self.cache_dir / safe_name
            game_dir.mkdir(parents=True, exist_ok=True)
            log.info("SteamGridDB match: %r -> id=%s name=%r", game_name, sgdb_id, matched_name)
            assets = {"capsule": ("grids", "capsule"), "hero": ("heroes", "hero"), "logo": ("logos", "logo"), "cover": ("grids", "cover")}
            paths = {}
            for field, (category, filename) in assets.items():
                try:
                    url = self._artwork_url(category, sgdb_id)
                    if url:
                        extension = ".png" if ".png" in url.lower() else ".jpg"
                        destination = game_dir / f"{filename}{extension}"
                        self._download(url, destination)
                        paths[field] = f"/artwork/{safe_name}/{destination.name}"
                        log.info("Artwork ready: %s=%s", field, paths[field])
                except requests.RequestException:
                    log.exception("SteamGridDB %s artwork failed for %r", field, game_name)
            self.db.set_metadata(game_id, {"title": matched_name, "app_id": str(sgdb_id) if sgdb_id is not None else None, "capsule": paths.get("capsule"), "logo": paths.get("logo"), "hero": paths.get("hero"), "cover": paths.get("cover"), "release_date": None, "description": None})
            return dict(self.db.get_game(game_id))
        except requests.RequestException:
            log.exception("SteamGridDB lookup failed for id=%s name=%r", game_id, game_name)
            return None
        except Exception:
            log.exception("Metadata lookup failed for id=%s name=%r", game_id, game_name)
            return None
