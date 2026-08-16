import ctypes
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

log = logging.getLogger("gamelibrary.playnite")


class PlayniteBridge:
    """Fast, cached bridge to the live GameDrive Playnite API."""

    API_URL = "http://127.0.0.1:38123"
    API_CACHE_SECONDS = 2.0
    API_REQUEST_TIMEOUT = 2.0

    def __init__(self, config=None):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))
        self.refresh_on_game_drive_change = bool(self.config.get("refreshOnGameDriveChange", True))
        self.restart_if_api_unavailable = bool(self.config.get("restartIfApiUnavailable", True))
        self.playnite_path = self._path(self.config.get("path") or self.config.get("playnitePath")) or self._find_playnite()
        self.library_path = self._path(self.config.get("libraryPath") or self.config.get("library_path")) or self._default_library_path()
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._games = []
        self._last_refresh = 0.0
        self._last_error = None
        self._startup_attempted = False
        self._restart_attempted = False
        self._api_available = False
        self._api_checked_at = 0.0

    def _path(self, value):
        if not value:
            return None
        p = Path(os.path.expandvars(os.path.expanduser(str(value))))
        if not p.is_absolute():
            p = (Path(__file__).resolve().parent.parent / p).resolve()
        return p

    def _default_library_path(self):
        local_app_data = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Playnite" / "library"
        return None

    def _find_playnite(self):
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Playnite" / "Playnite.DesktopApp.exe",
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Playnite" / "Playnite.DesktopApp.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Playnite" / "Playnite.DesktopApp.exe",
        ]
        for p in candidates:
            if p.is_file():
                return p
        return None

    def _normalize_api_json(self, value):
        if isinstance(value, list):
            normalized = [self._normalize_api_json(item) for item in value]
            if normalized and all(isinstance(item, dict) and set(item.keys()) == {"Key", "Value"} for item in normalized):
                return {str(item["Key"]): item["Value"] for item in normalized}
            return normalized
        if isinstance(value, dict):
            return {str(key): self._normalize_api_json(item) for key, item in value.items()}
        return value

    def _probe_api(self, timeout=1.0):
        if not self.enabled:
            with self._lock:
                self._api_available = False
                self._api_checked_at = time.monotonic()
            return False
        try:
            response = self._request("/health", timeout=timeout)
            ready = response.get("ready", True)
            available = bool(response.get("ok")) and response.get("source") == "PlayniteApi.Database.Games" and bool(ready)
            with self._lock:
                self._api_available = available
                self._api_checked_at = time.monotonic()
            if available:
                log.info("GameDrive Playnite API ready: %s installed game(s)", response.get("installedCount", "unknown"))
            return available
        except Exception:
            with self._lock:
                self._api_available = False
                self._api_checked_at = time.monotonic()
            return False

    @property
    def available(self):
        with self._lock:
            return self._api_available

    @property
    def status(self):
        with self._lock:
            return {"enabled": self.enabled, "available": self._api_available, "playnite_path": str(self.playnite_path) if self.playnite_path else None, "library_path": str(self.library_path) if self.library_path else None, "library_source": "PlayniteApi.Database.Games", "last_refresh": self._last_refresh, "error": self._last_error, "game_count": len(self._games)}

    def _request(self, path, timeout=None):
        request = Request(self.API_URL + path, headers={"Accept": "application/json", "User-Agent": "GameDriveMASTER/1.0"})
        with urlopen(request, timeout=self.API_REQUEST_TIMEOUT if timeout is None else timeout) as response:
            raw = response.read()
        data = self._normalize_api_json(json.loads(raw.decode("utf-8")))
        if isinstance(data, dict) and data.get("ok") is False:
            raise RuntimeError(data.get("error") or "Playnite API request failed")
        return data

    def _is_running(self):
        if os.name != "nt": return False
        try:
            result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Playnite.DesktopApp.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=2, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return "Playnite.DesktopApp.exe" in result.stdout
        except Exception: return False

    def _ensure_started(self):
        if not self.enabled: return False
        if self._is_running(): self._startup_attempted = True; return True
        if not self.playnite_path or not self.playnite_path.is_file():
            self._last_error = "Playnite executable not found"; log.warning("Playnite executable not found: %s", self.playnite_path); return False
        try:
            subprocess.Popen([str(self.playnite_path)], cwd=str(self.playnite_path.parent), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._startup_attempted = True; log.info("Started Playnite: %s", self.playnite_path); return True
        except Exception as exc:
            self._last_error = str(exc); log.warning("Could not start Playnite: %s", exc); return False

    def restart(self):
        if not self.enabled or not self.restart_if_api_unavailable or self._restart_attempted: return False
        self._restart_attempted = True
        if not self.playnite_path or not self.playnite_path.is_file(): return False
        try:
            if self._is_running():
                log.info("Playnite is running without the GameDrive API; restarting it to load the current extension")
                subprocess.run([str(self.playnite_path), "--shutdown"], cwd=str(self.playnite_path.parent), capture_output=True, text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                deadline = time.monotonic() + 10
                while self._is_running() and time.monotonic() < deadline: time.sleep(0.25)
            return self._ensure_started()
        except Exception as exc:
            self._last_error = str(exc); log.warning("Could not restart Playnite: %s", exc); return False

    def start(self, wait_for_api=True, timeout=8):
        if not self.enabled or not self._ensure_started(): return False
        if not wait_for_api: return True
        deadline = time.monotonic() + max(0, timeout)
        while time.monotonic() < deadline:
            if self._probe_api(timeout=1.0): return True
            time.sleep(0.25)
        log.warning("Playnite process started, but GameDrive Playnite API is not ready yet")
        return self._is_running()

    def _action(self, game):
        return {"Path": game.get("executable") or "", "Arguments": game.get("arguments") or "", "WorkingDir": game.get("workingDirectory") or ""}

    def _media_value(self, value, game_id=None):
        if not value: return None
        value = str(value)
        if value.startswith("data:") or value.startswith("http://") or value.startswith("https://"):
            return value
        if value.startswith("file://"):
            parsed = urlparse(value)
            if parsed.netloc:
                value = f"\\\\{parsed.netloc}{unquote(parsed.path)}"
            else:
                value = unquote(parsed.path)
                if len(value) >= 3 and value.startswith("/") and value[2] == ":":
                    value = value[1:]
        # Playnite stores its downloaded artwork in library/files/<game GUID>/.
        # The API may return only the artwork filename, so resolve it directly
        # against that game's Playnite media directory.
        p = Path(os.path.expandvars(os.path.expanduser(value)))
        if not p.is_absolute() and game_id and self.library_path:
            game_media_dir = self.library_path / "files" / str(game_id)
            candidate = game_media_dir / p
            if candidate.is_file():
                return str(candidate.resolve())
        return value

    def _parse(self, game):
        if not isinstance(game, dict) or not bool(game.get("isInstalled", False)): return None
        gid = str(game.get("id") or "")
        if not gid: return None
        action = self._action(game)
        cover = self._media_value(game.get("cover") or game.get("coverImage"), gid)
        hero = self._media_value(game.get("hero") or game.get("backgroundImage"), gid)
        logo = self._media_value(game.get("logo") or game.get("icon"), gid)
        return {
            "playnite_id": gid,
            "name": game.get("name") or "Unknown Game",
            "game_id": game.get("gameId"),
            "source_id": game.get("sourceId"),
            "install_directory": str(game.get("installDirectory") or ""),
            "executable": str(action["Path"]),
            "arguments": str(action["Arguments"]),
            "working_directory": str(action["WorkingDir"]),
            "description": game.get("description"),
            "release_date": game.get("releaseDate"),
            "playtime": game.get("playtime") or 0,
            "cover": cover,
            "capsule": cover,
            "hero": hero,
            "logo": logo,
        }

    def _fetch_games(self):
        raw_games = self._request("/games", timeout=self.API_REQUEST_TIMEOUT)
        if not isinstance(raw_games, list): raise RuntimeError("Playnite /games returned an invalid response")
        games = [parsed for parsed in (self._parse(raw) for raw in raw_games) if parsed]
        with self._lock:
            self._games = games; self._last_refresh = time.time(); self._last_error = None
        log.info("Playnite API library read: %d installed game(s)", len(games))
        return games

    def _background_refresh(self):
        if not self.enabled or not self._refresh_lock.acquire(blocking=False): return
        try:
            self._fetch_games(); self._probe_api(timeout=0.5)
        except Exception as exc:
            with self._lock: self._last_error = str(exc)
            log.warning("Playnite library refresh failed: %s", exc); self._probe_api(timeout=0.5)
        finally: self._refresh_lock.release()

    def read_games(self, force=False):
        now = time.time()
        with self._lock:
            cached = list(self._games); age = now - self._last_refresh if self._last_refresh else float("inf"); api_available = self._api_available
        if not self.enabled: return cached
        if not cached and api_available:
            try: return self._fetch_games()
            except Exception as exc:
                with self._lock: self._last_error = str(exc)
                log.warning("Initial Playnite library read failed: %s", exc); return cached
        if force or age >= self.API_CACHE_SECONDS or not api_available:
            threading.Thread(target=self._background_refresh, daemon=True, name="PlayniteLibraryRefresh").start()
        return cached

    def _window(self): return None

    def uri(self, playnite_id):
        return f"playnite://game/{quote(str(playnite_id), safe='')}"

    def launch(self, playnite_id):
        if not playnite_id: return {"ok": False, "error": "missing_playnite_id"}
        try:
            result = self._request(f"/games/{quote(str(playnite_id), safe='')}/launch", timeout=5.0)
            return result if isinstance(result, dict) else {"ok": bool(result)}
        except Exception as exc:
            return {"ok": False, "error": "playnite_launch_failed", "detail": str(exc)}
