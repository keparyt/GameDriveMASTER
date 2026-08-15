import logging
from pathlib import Path

import requests

from .. import config as app_config

log = logging.getLogger("gamelibrary.metadata")


class MetadataManager:
    """Resolve game metadata and cache SteamGridDB artwork locally."""

    def __init__(self, db, config=None):
        self.db = db
        self.config = config or {}
        self.enabled = bool(
            self.config.get("enabled", True)
            and app_config.STEAMGRIDDB_ENABLED
            and app_config.STEAMGRIDDB_API_KEY
        )
        self.base_url = app_config.STEAMGRIDDB_BASE_URL.rstrip("/")
        self.cache_dir = Path(app_config.ARTWORK_CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {app_config.STEAMGRIDDB_API_KEY}",
            "User-Agent": "GameDrive/1.0"
        })

    @staticmethod
    def _safe_name(value):
        value = "".join(
            c if c.isalnum() or c in " ._-" else "_"
            for c in value
        ).strip(" .")
        return value or "unknown"

    def _search(self, game_name):
        url = f"{self.base_url}/search/autocomplete/{requests.utils.quote(game_name, safe='')}"
        response = self.session.get(url, timeout=15)
        response.raise_for_status()
        data = response.json().get("data", [])
        if not data:
            return None

        normalized = "".join(c.lower() for c in game_name if c.isalnum())
        for game in data:
            name = game.get("name", "")
            candidate = "".join(c.lower() for c in name if c.isalnum())
            if candidate == normalized:
                return game
        return data[0]

    def _artwork_url(self, category, sgdb_id):
        url = f"{self.base_url}/{category}/game/{sgdb_id}?mimes=image/jpeg,image/png"
        response = self.session.get(url, timeout=15)
        response.raise_for_status()
        data = response.json().get("data", [])
        return data[0].get("url") if data else None

    def _download(self, url, destination):
        if not url or destination.exists():
            return

        response = requests.get(
            url,
            timeout=30,
            headers={"User-Agent": "GameDrive/1.0"}
        )
        response.raise_for_status()
        temp = destination.with_suffix(destination.suffix + ".tmp")
        temp.write_bytes(response.content)
        temp.replace(destination)

    def lookup(self, game_id, game_name):
        row = self.db.get_game(game_id)
        if row and row["title"] and row["capsule"]:
            return dict(row)

        if not self.enabled:
            log.debug("SteamGridDB disabled; no metadata lookup for %s", game_name)
            return dict(row) if row else None

        try:
            game = self._search(game_name)
            if not game:
                log.info("SteamGridDB: no match for %s", game_name)
                return dict(row) if row else None

            sgdb_id = game.get("id")
            safe_name = self._safe_name(game.get("name") or game_name)
            game_dir = self.cache_dir / safe_name
            game_dir.mkdir(parents=True, exist_ok=True)

            assets = {
                "capsule": ("grids", "capsule"),
                "hero": ("heroes", "hero"),
                "logo": ("logos", "logo"),
                "cover": ("grids", "cover")
            }

            paths = {}
            for field, (category, filename) in assets.items():
                try:
                    url = self._artwork_url(category, sgdb_id)
                    if url:
                        extension = ".png" if ".png" in url.lower() else ".jpg"
                        destination = game_dir / f"{filename}{extension}"
                        self._download(url, destination)
                        paths[field] = f"/artwork/{safe_name}/{destination.name}"
                except requests.RequestException as exc:
                    log.warning("SteamGridDB %s artwork failed for %s: %s", field, game_name, exc)

            metadata = {
                "title": game.get("name") or game_name,
                "app_id": str(sgdb_id) if sgdb_id is not None else None,
                "capsule": paths.get("capsule"),
                "logo": paths.get("logo"),
                "hero": paths.get("hero"),
                "cover": paths.get("cover"),
                "release_date": None,
                "description": None
            }
            self.db.set_metadata(game_id, metadata)
            return dict(self.db.get_game(game_id))

        except requests.RequestException as exc:
            log.warning("SteamGridDB lookup failed for %s: %s", game_name, exc)
            return dict(row) if row else None
        except Exception:
            log.exception("Metadata lookup failed for %s", game_name)
            return dict(row) if row else None
