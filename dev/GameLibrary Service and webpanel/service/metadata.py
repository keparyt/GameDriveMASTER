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
        self.api_key = (app_config.STEAMGRIDDB_API_KEY or "").strip()
        self.enabled = bool(self.config.get("enabled", True) and app_config.STEAMGRIDDB_ENABLED and self.api_key)
        self.auto_lookup = bool(self.config.get("auto_lookup", True))
        self.base_url = app_config.STEAMGRIDDB_BASE_URL.rstrip("/")
        self.cache_dir = (app_config.BASE_DIR / app_config.ARTWORK_CACHE_DIR).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.api_key}", "User-Agent": "GameDrive/1.0"})
        self._queue = queue.Queue()
        self._queued = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._auth_failed = False
        self._worker = threading.Thread(target=self._worker_loop, name="SteamGridDBWorker", daemon=True)
        self._worker.start()
        if self.enabled:
            log.info("SteamGridDB initialized: enabled=True auto_lookup=%s cache=%s", self.auto_lookup, self.cache_dir)
        else:
            log.warning("SteamGridDB disabled: no API key configured")

    @staticmethod
    def _safe_name(value):
        value = "".join(c if c.isalnum() or c in " ._-" else "_" for c in value)
        return value.strip(" .") or "unknown"

    def queue_lookup(self, game_id, game_name):
        if not self.enabled or self._auth_failed or not game_name:
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
                if not self._auth_failed:
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

    def _handle_response_error(self, response, game_name):
        if response.status_code == 401:
            self._auth_failed = True
            log.error("SteamGridDB authentication failed (HTTP 401). The API key is missing, revoked, or invalid. Artwork lookups are paused. Set STEAMGRIDDB_API_KEY locally and restart the service.")
            return True
        if response.status_code == 403:
            log.error("SteamGridDB access denied (HTTP 403) while looking up %r", game_name)
            return True
        response.raise_for_status()
        return False

    def _search(self, game_name):
        url = f"{self.base_url}/search/autocomplete/{requests.utils.quote(game_name, safe='')}"
        log.debug("SteamGridDB search: %s", url)
        response = self.session.get(url, timeout=15)
        log.debug("SteamGridDB search response: HTTP %s", response.status_code)
        if self._handle_response_error(response, game_name):
            return None
        data = response.json().get("data", [])
        if not data:
            return None
        normalized = "".join(c.lower() for c in game_name if c.isalnum())
        for game in data:
            candidate = "".join(c.lower() for c in game.get("name", "") if c.isalnum())
            if candidate == normalized:
                return game
        return data[0]

    def _artwork_url(self, category, sgdb_id, game_name):
        url = f"{self.base_url}/{category}/game/{sgdb_id}?mimes=image/jpeg,image/png"
        log.debug("SteamGridDB artwork request: category=%s id=%s", category, sgdb_id)
        response = self.session.get(url, timeout=15)
        log.debug("SteamGridDB artwork response: HTTP %s", response.status_code)
        if response.status_code == 400 and category == "logos":
            log.info("SteamGridDB: no logo artwork available for %r", game_name)
            return None
        if self._handle_response_error(response, game_name):
            return None
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
        if not self.enabled or self._auth_failed:
            return None
        try:
            game = self._search(game_name)
            if self._auth_failed or not game:
                if not self._auth_failed:
                    log.info("SteamGridDB: no match for %r", game_name)
                return None
            sgdb_id = game.get("id")
            matched_name = game.get("name") or game_name
            safe_name = self._safe_name(matched_name)
            game_dir = self.cache_dir / safe_name
            game_dir.mkdir(parents=True, exist_ok=True)
            log.info("SteamGridDB match: %r -> id=%s name=%r", game_name, sgdb_id, matched_name)

            # Download the capsule first so the most visible artwork becomes
            # available as soon as possible. Each completed asset is published
            # to SQLite immediately; the web panel can pick it up on its next
            # background poll without requiring a page refresh.
            assets = [
                ("capsule", "grids", "capsule"),
                ("hero", "heroes", "hero"),
                ("logo", "logos", "logo"),
                ("cover", "grids", "cover"),
            ]
            paths = {}
            for field, category, filename in assets:
                if self._auth_failed:
                    break
                try:
                    url = self._artwork_url(category, sgdb_id, game_name)
                    if not url:
                        continue
                    extension = ".png" if ".png" in url.lower() else ".jpg"
                    destination = game_dir / f"{filename}{extension}"
                    self._download(url, destination)
                    path = f"/artwork/{safe_name}/{destination.name}"
                    paths[field] = path

                    # Publish this single asset immediately. Existing metadata
                    # fields are preserved by update_metadata_fields().
                    saved = self.db.update_metadata_fields(game_id, {
                        "title": matched_name,
                        "app_id": str(sgdb_id) if sgdb_id is not None else None,
                        field: path,
                    })
                    if not saved:
                        log.debug("Metadata result discarded: game id=%s no longer exists", game_id)
                        return None
                    log.info("Artwork ready: %s=%s", field, path)
                except requests.RequestException as exc:
                    log.warning("SteamGridDB %s artwork failed for %r: %s", field, game_name, exc)

            if self._auth_failed:
                return None
            row = self.db.get_game(game_id)
            return dict(row) if row else None
        except requests.RequestException as exc:
            log.warning("SteamGridDB request failed for id=%s name=%r: %s", game_id, game_name, exc)
            return None
        except Exception:
            log.exception("Metadata lookup failed for id=%s name=%r", game_id, game_name)
            return None
