import ctypes
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

log = logging.getLogger("gamelibrary.playnite")


class PlayniteBridge:
    """Bridge to the running GameDrive Playnite plugin.

    Playnite itself is authoritative. The service intentionally does not read
    Playnite's games.db/LiteDB because that is only a serialized representation
    of the library and can disagree with the live Playnite API.
    """

    API_URL = "http://127.0.0.1:38123"

    def __init__(self, config=None):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))
        self.refresh_on_game_drive_change = bool(
            self.config.get("refreshOnGameDriveChange", True)
        )
        self.playnite_path = self._path(
            self.config.get("path") or self.config.get("playnitePath")
        ) or self._find_playnite()
        self.library_path = self._path(
            self.config.get("libraryPath") or self.config.get("library_path")
        ) or self._default_library_path()
        self._lock = threading.RLock()
        self._games = []
        self._last_refresh = 0.0
        self._last_error = None
        self._startup_attempted = False

    def _path(self, value):
        if not value:
            return None
        p = Path(os.path.expandvars(os.path.expanduser(str(value))))
        if not p.is_absolute():
            p = (Path(__file__).resolve().parent.parent / p).resolve()
        return p

    def _default_library_path(self):
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Playnite" / "library"
        return None

    def _find_playnite(self):
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Playnite"
            / "Playnite.DesktopApp.exe",
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
            / "Playnite"
            / "Playnite.DesktopApp.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
            / "Playnite"
            / "Playnite.DesktopApp.exe",
        ]
        for p in candidates:
            if p.is_file():
                return p
        return None

    @property
    def available(self):
        if not self.enabled:
            return False
        try:
            response = self._request("/health", timeout=1.5)
            return bool(response.get("ok")) and response.get("source") == "PlayniteApi.Database.Games"
        except Exception:
            return False

    @property
    def status(self):
        return {
            "enabled": self.enabled,
            "available": self.available,
            "playnite_path": str(self.playnite_path) if self.playnite_path else None,
            "library_path": str(self.library_path) if self.library_path else None,
            "library_source": "PlayniteApi.Database.Games",
            "last_refresh": self._last_refresh,
            "error": self._last_error,
            "game_count": len(self._games),
        }

    def _request(self, path, timeout=10):
        request = Request(
            self.API_URL + path,
            headers={"Accept": "application/json", "User-Agent": "GameDriveMASTER/1.0"},
        )
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict) and data.get("ok") is False:
            raise RuntimeError(data.get("error") or "Playnite API request failed")
        return data

    def _is_running(self):
        if os.name != "nt":
            return False
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Playnite.DesktopApp.exe", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=2,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return "Playnite.DesktopApp.exe" in result.stdout
        except Exception:
            return False

    def _ensure_started(self):
        if not self.enabled:
            return False
        if self._is_running():
            self._startup_attempted = True
            return True
        if not self.playnite_path or not self.playnite_path.is_file():
            self._last_error = "Playnite executable not found"
            log.warning("Playnite executable not found: %s", self.playnite_path)
            return False
        try:
            subprocess.Popen(
                [str(self.playnite_path)],
                cwd=str(self.playnite_path.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._startup_attempted = True
            log.info("Started Playnite: %s", self.playnite_path)
            return True
        except Exception as e:
            self._last_error = str(e)
            log.warning("Could not start Playnite: %s", e)
            return False

    def start(self, wait_for_api=True, timeout=8):
        """Ensure Playnite is running before the Game Library API starts."""
        if not self.enabled:
            return False
        if not self._ensure_started():
            return False
        if not wait_for_api:
            return True
        deadline = time.monotonic() + max(0, timeout)
        while time.monotonic() < deadline:
            if self.available:
                return True
            time.sleep(0.25)
        log.warning("Playnite process started, but GameDrive Playnite API is not ready yet")
        return self._is_running()

    def _action(self, game):
        executable = game.get("executable") or ""
        return {
            "Path": executable,
            "Arguments": game.get("arguments") or "",
            "WorkingDir": game.get("workingDirectory") or "",
        }

    def _parse(self, game):
        if not isinstance(game, dict):
            return None
        # The plugin filters to IsInstalled games. Keep this second guard so
        # an endpoint regression can never leak uninstalled games to the UI.
        if not bool(game.get("isInstalled", False)):
            return None
        gid = str(game.get("id") or "")
        if not gid:
            return None
        action = self._action(game)
        return {
            "playnite_id": gid,
            "name": game.get("name") or "Unknown Game",
            "game_id": game.get("gameId"),
            "source_id": game.get("sourceId"),
            "install_directory": str(game.get("installDirectory") or ""),
            "executable": str(action.get("Path") or ""),
            "arguments": str(action.get("Arguments") or ""),
            "working_directory": str(action.get("WorkingDir") or ""),
            "description": game.get("description"),
            "release_date": game.get("releaseDate"),
            "playtime": game.get("playtime") or 0,
        }

    def read_games(self, force=False):
        with self._lock:
            try:
                raw_games = self._request("/games", timeout=10)
                games = []
                for raw in raw_games if isinstance(raw_games, list) else []:
                    parsed = self._parse(raw)
                    if parsed:
                        games.append(parsed)
                self._games = games
                self._last_refresh = time.time()
                self._last_error = None
                log.info("Playnite API library read: %d installed game(s)", len(games))
                return list(games)
            except Exception as e:
                # Playnite can take a while to finish loading extensions. Do
                # not permanently disable integration after the first failed
                # request; every later scanner cycle can recover automatically.
                if self._ensure_started():
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        try:
                            raw_games = self._request("/games", timeout=2)
                            games = [g for g in (self._parse(x) for x in raw_games) if g]
                            self._games = games
                            self._last_refresh = time.time()
                            self._last_error = None
                            log.info("Playnite API library read: %d installed game(s)", len(games))
                            return list(games)
                        except Exception:
                            time.sleep(0.25)
                self._last_error = str(e)
                log.warning("Could not read Playnite API library: %s", e)
                return list(self._games)

    def _window(self):
        if not hasattr(ctypes, "windll"):
            return None
        user32 = ctypes.windll.user32
        found = {"h": None}
        cb = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        @cb
        def enum(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                r = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid.value}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if "Playnite.DesktopApp.exe" in r.stdout:
                    found["h"] = hwnd
                    return False
            except Exception:
                pass
            return True

        user32.EnumWindows(enum, 0)
        return found["h"]

    def refresh(self, force=False):
        if not self.enabled:
            return False
        try:
            self._ensure_started()
            hwnd = self._window()
            if not hwnd and self._is_running():
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not self._window():
                    time.sleep(0.25)
                hwnd = self._window()
            if hwnd:
                # F5 is Playnite's normal library refresh command. This keeps
                # refresh behavior inside Playnite rather than modifying its DB.
                u = ctypes.windll.user32
                u.PostMessageW(hwnd, 0x0100, 0x74, 0)
                u.PostMessageW(hwnd, 0x0101, 0x74, 0)
                time.sleep(0.75)
            self.read_games(force=True)
            return True
        except Exception as e:
            self._last_error = str(e)
            log.warning("Playnite refresh failed: %s", e)
            self.read_games(force=True)
            return False

    def launch(self, playnite_id):
        if not self.enabled or not self.playnite_path or not self.playnite_path.is_file():
            return {"ok": False, "error": "playnite_unavailable"}
        try:
            if not self._is_running():
                if not self._ensure_started():
                    return {"ok": False, "error": "playnite_start_failed"}
                time.sleep(0.5)
            subprocess.Popen(
                [str(self.playnite_path), "--start", str(playnite_id)],
                cwd=str(self.playnite_path.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return {"ok": True, "playnite_id": str(playnite_id)}
        except Exception as e:
            return {"ok": False, "error": "playnite_launch_failed", "detail": str(e)}

    def uri(self, playnite_id):
        return "playnite://playnite/start/" + quote(str(playnite_id), safe="")
