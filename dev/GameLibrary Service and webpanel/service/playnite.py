import ctypes
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("gamelibrary.playnite")


class PlayniteBridge:
    """Read Playnite's on-disk game database and launch through Playnite itself.

    Playnite exposes the game database as individual JSON records under the
    configured library directory. The bridge only reads those records; all
    writes, installs and game-session tracking remain owned by Playnite.
    """

    def __init__(self, config=None):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))
        self.refresh_on_game_drive_change = bool(self.config.get("refreshOnGameDriveChange", True))
        self.playnite_path = self._resolve_playnite_path()
        self.library_path = self._resolve_library_path()
        self._lock = threading.RLock()
        self._games = []
        self._signature = None
        self._last_refresh = 0.0
        self._last_error = None

    @property
    def available(self):
        return bool(self.enabled and self.library_path and self.library_path.is_dir())

    @property
    def status(self):
        return {
            "enabled": self.enabled,
            "available": self.available,
            "playnite_path": str(self.playnite_path) if self.playnite_path else None,
            "library_path": str(self.library_path) if self.library_path else None,
            "last_refresh": self._last_refresh,
            "error": self._last_error,
            "game_count": len(self._games),
        }

    def _resolve_path(self, value):
        if not value:
            return None
        path = Path(os.path.expandvars(os.path.expanduser(str(value))))
        if not path.is_absolute():
            path = (Path(__file__).resolve().parent.parent / path).resolve()
        return path

    def _resolve_playnite_path(self):
        configured = self._resolve_path(self.config.get("path") or self.config.get("playnitePath"))
        if configured:
            return configured

        candidates = [
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Playnite" / "Playnite.DesktopApp.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Playnite" / "Playnite.DesktopApp.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def _resolve_library_path(self):
        configured = self._resolve_path(
            self.config.get("libraryPath") or self.config.get("library_path") or self.config.get("databasePath")
        )
        if configured:
            return configured

        # Installed Playnite defaults to %APPDATA%\\Playnite\\library. For
        # portable installations the library sits beside the executable.
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidate = Path(appdata) / "Playnite" / "library"
            if candidate.is_dir():
                return candidate
        if self.playnite_path:
            candidate = self.playnite_path.parent / "library"
            if candidate.is_dir():
                return candidate
        return None

    def _games_dir(self):
        return self.library_path / "games" if self.library_path else None

    def _file_signature(self):
        games_dir = self._games_dir()
        if not games_dir or not games_dir.is_dir():
            return None
        try:
            entries = []
            for path in games_dir.glob("*.json"):
                stat = path.stat()
                entries.append((path.name, stat.st_mtime_ns, stat.st_size))
            return hash(tuple(sorted(entries)))
        except OSError as exc:
            self._last_error = str(exc)
            return None

    def needs_refresh(self):
        signature = self._file_signature()
        return signature != self._signature

    def _read_json(self, path):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            log.debug("Ignoring unreadable Playnite game file %s: %s", path, exc)
            return None

    def _resolve_media(self, game_id, value):
        if not value:
            return None
        raw = str(value)
        path = Path(raw)
        if path.is_absolute() and path.is_file():
            return str(path)
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw

        # Database file ids are stored in library/files/<game-id>/<file-id>.
        media_root = self.library_path / "files" / str(game_id)
        candidates = [media_root / raw]
        if raw.startswith("{gameid}"):
            candidates.append(media_root / raw.replace("{gameid}", str(game_id), 1))
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return None

    def _play_action(self, game):
        actions = game.get("GameActions") or game.get("GameAction") or []
        if isinstance(actions, dict):
            actions = list(actions.values())
        for action in actions:
            if not isinstance(action, dict):
                continue
            if action.get("IsPlayAction"):
                return action
        return actions[0] if actions and isinstance(actions[0], dict) else None

    def _normalize(self, value):
        return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())

    def _normalize_path(self, value):
        if not value:
            return None
        try:
            return os.path.normcase(os.path.normpath(str(value))).rstrip("\\/")
        except Exception:
            return str(value).lower().rstrip("\\/")

    def _parse_game(self, path):
        data = self._read_json(path)
        if not isinstance(data, dict):
            return None
        game_id = data.get("Id") or data.get("id") or path.stem
        installed = bool(data.get("IsInstalled", data.get("isInstalled", False)))
        install_dir = data.get("InstallDirectory") or data.get("installDirectory") or ""
        action = self._play_action(data)
        action_path = (action or {}).get("Path") or (action or {}).get("path") or ""
        action_args = (action or {}).get("Arguments") or (action or {}).get("arguments") or ""
        working_dir = (action or {}).get("WorkingDir") or (action or {}).get("WorkingDirectory") or ""
        if not installed:
            return None

        game = {
            "playnite_id": str(game_id),
            "name": data.get("Name") or data.get("name") or "Unknown Game",
            "game_id": data.get("GameId") or data.get("gameId"),
            "source_id": data.get("SourceId") or data.get("sourceId"),
            "install_directory": str(install_dir),
            "executable": str(action_path),
            "arguments": str(action_args),
            "working_directory": str(working_dir),
            "cover": self._resolve_media(game_id, data.get("CoverImage") or data.get("coverImage")),
            "hero": self._resolve_media(game_id, data.get("BackgroundImage") or data.get("backgroundImage")),
            "logo": self._resolve_media(game_id, data.get("Icon") or data.get("icon")),
            "description": data.get("Description") or data.get("description"),
            "release_date": data.get("ReleaseDate") or data.get("releaseDate"),
            "playtime": data.get("Playtime") or data.get("playtime") or data.get("PlayTime") or 0,
            "hidden": bool(data.get("Hidden", data.get("hidden", False))),
            "raw_path": str(path),
        }
        return game

    def read_games(self, force=False):
        with self._lock:
            if not self.available:
                self._games = []
                self._signature = None
                return []
            signature = self._file_signature()
            if not force and signature == self._signature:
                return list(self._games)

            games = []
            games_dir = self._games_dir()
            for path in sorted(games_dir.glob("*.json")):
                game = self._parse_game(path)
                if game:
                    games.append(game)
            self._games = games
            self._signature = signature
            self._last_refresh = time.time()
            self._last_error = None
            log.info("Playnite library read: %d installed game(s)", len(games))
            return list(games)

    def _find_window(self):
        if not hasattr(ctypes, "windll"):
            return None
        user32 = ctypes.windll.user32
        target = {"handle": None}
        enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        @enum_proc_type
        def enum_proc(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return True
            # Get the executable name from the process using tasklist only as a
            # fallback-free Windows API is unnecessarily complex for this tiny bridge.
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid.value}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=2,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if "Playnite.DesktopApp.exe" in result.stdout:
                    target["handle"] = hwnd
                    return False
            except Exception:
                pass
            return True

        user32.EnumWindows(enum_proc, 0)
        return target["handle"]

    def refresh(self, force=False):
        """Refresh Playnite without making GameDriveMASTER depend on it.

        If Playnite is running, F5 is posted to its main window, matching the
        documented Playnite library-refresh command. If it is not running,
        starting Playnite normally lets its normal startup library update run.
        """
        if not self.enabled:
            return False
        if not force and not self.needs_refresh():
            return False

        if self._find_window():
            try:
                user32 = ctypes.windll.user32
                hwnd = self._find_window()
                user32.PostMessageW(hwnd, 0x0100, 0x74, 0)  # WM_KEYDOWN / F5
                user32.PostMessageW(hwnd, 0x0101, 0x74, 0)  # WM_KEYUP / F5
                log.info("Requested Playnite library refresh via F5")
                time.sleep(0.35)
            except Exception as exc:
                self._last_error = str(exc)
                log.warning("Could not request Playnite refresh: %s", exc)
        elif self.playnite_path and self.playnite_path.is_file():
            try:
                subprocess.Popen(
                    [str(self.playnite_path)],
                    cwd=str(self.playnite_path.parent),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                log.info("Started Playnite to refresh its library")
                time.sleep(0.75)
            except Exception as exc:
                self._last_error = str(exc)
                log.warning("Could not start Playnite: %s", exc)

        self.read_games(force=True)
        return True

    def launch(self, playnite_id):
        if not self.enabled or not self.playnite_path or not self.playnite_path.is_file():
            return {"ok": False, "error": "playnite_unavailable"}
        try:
            # Playnite's documented --start argument launches the game by its
            # database ID and keeps Playnite responsible for tracking.
            subprocess.Popen(
                [str(self.playnite_path), "--start", str(playnite_id)],
                cwd=str(self.playnite_path.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return {"ok": True, "playnite_id": str(playnite_id)}
        except Exception as exc:
            return {"ok": False, "error": "playnite_launch_failed", "detail": str(exc)}

    def uri(self, playnite_id):
        return f"playnite://playnite/start/{quote(str(playnite_id), safe='')}"
