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
    def __init__(self, config=None):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))
        self.refresh_on_game_drive_change = bool(self.config.get("refreshOnGameDriveChange", True))
        self.playnite_path = self._path(self.config.get("path") or self.config.get("playnitePath")) or self._find_playnite()
        self.library_path = self._path(self.config.get("libraryPath") or self.config.get("library_path") or self.config.get("databasePath")) or self._find_library()
        self.litedb_path = self._path(self.config.get("liteDbPath") or self.config.get("litedbPath")) or self._find_litedb()
        self.database_path = (self.library_path / "games.db") if self.library_path else None
        self._lock = threading.RLock()
        self._games = []
        self._signature = None
        self._checked_at = 0.0
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

    def _find_playnite(self):
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Playnite" / "Playnite.DesktopApp.exe",
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Playnite" / "Playnite.DesktopApp.exe",
        ]
        for p in candidates:
            if p.is_file():
                return p
        return None

    def _find_library(self):
        appdata = os.environ.get("APPDATA")
        if appdata:
            p = Path(appdata) / "Playnite" / "library"
            if p.is_dir():
                return p
        if self.playnite_path:
            p = self.playnite_path.parent / "library"
            if p.is_dir():
                return p
        return None

    def _find_litedb(self):
        roots = []
        if self.playnite_path:
            roots.extend([self.playnite_path.parent, self.playnite_path.parent / "lib", self.playnite_path.parent / "Libraries"])
        if self.library_path:
            roots.extend([self.library_path, self.library_path.parent])
        roots.append(Path(__file__).resolve().parent)
        seen = set()
        for root in roots:
            if not root or not root.exists():
                continue
            try:
                for p in root.rglob("LiteDB.dll"):
                    key = str(p).lower()
                    if p.is_file() and key not in seen:
                        return p
                    seen.add(key)
            except OSError:
                pass
        return None

    @property
    def available(self):
        return bool(self.enabled and self.database_path and self.database_path.is_file())

    @property
    def status(self):
        return {
            "enabled": self.enabled,
            "available": self.available,
            "playnite_path": str(self.playnite_path) if self.playnite_path else None,
            "library_path": str(self.library_path) if self.library_path else None,
            "database_path": str(self.database_path) if self.database_path else None,
            "litedb_path": str(self.litedb_path) if self.litedb_path else None,
            "last_refresh": self._last_refresh,
            "error": self._last_error,
            "game_count": len(self._games),
        }

    def _signature_now(self):
        if not self.available:
            return None
        try:
            st = self.database_path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError as e:
            self._last_error = str(e)
            return self._signature

    def needs_refresh(self):
        return self._signature_now() != self._signature

    def _powershell_litedb(self):
        """Read Playnite's LiteDB database using the exact LiteDB.dll shipped by Playnite.

        Important: powershell.exe accepts either -Command OR -EncodedCommand, not both,
        and arguments after -EncodedCommand are not a reliable way to bind script params.
        Pass paths through an explicitly constructed environment instead.
        """
        if not self.litedb_path or not self.litedb_path.is_file():
            raise RuntimeError("LiteDB.dll was not found. Set playnite.liteDbPath to the LiteDB.dll used by Playnite.")
        if not self.database_path or not self.database_path.is_file():
            raise RuntimeError("Playnite games.db was not found.")

        script = r'''
$ErrorActionPreference = 'Stop'
$dllPath = $env:GDM_LITEDB_DLL
$dbPath = $env:GDM_PLAYNITE_DB
if ([string]::IsNullOrWhiteSpace($dllPath) -or -not (Test-Path -LiteralPath $dllPath -PathType Leaf)) {
    throw "GDM_LITEDB_DLL is missing or does not point to a file: $dllPath"
}
if ([string]::IsNullOrWhiteSpace($dbPath) -or -not (Test-Path -LiteralPath $dbPath -PathType Leaf)) {
    throw "GDM_PLAYNITE_DB is missing or does not point to a file: $dbPath"
}

Add-Type -Path $dllPath

# Playnite keeps games.db open itself. LiteDB's shared connection mode lets the
# launcher inspect the same database without requiring Playnite to be closed.
$connectionString = "Filename=$dbPath;Connection=Shared"
$db = New-Object LiteDB.LiteDatabase($connectionString)
try {
    $col = $db.GetCollection('games')
    $items = foreach ($doc in $col.FindAll()) {
        $doc.RawValue.ToString()
    }
    if ($items) {
        $items | ConvertTo-Json -Depth 30 -Compress
    } else {
        '[]'
    }
} finally {
    $db.Dispose()
}
'''
        encoded = __import__("base64").b64encode(script.encode("utf-16le")).decode("ascii")
        env = os.environ.copy()
        env["GDM_LITEDB_DLL"] = str(self.litedb_path)
        env["GDM_PLAYNITE_DB"] = str(self.database_path)

        args = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ]
        r = subprocess.run(
            args,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout or "LiteDB read failed").strip())
        return r.stdout.strip()

    def _read_litedb(self):
        raw = self._powershell_litedb()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid LiteDB JSON output: {e}")
        if isinstance(data, dict):
            return [data]
        return data if isinstance(data, list) else []

    def _action(self, g):
        actions = g.get("GameActions") or g.get("GameAction") or g.get("PlayAction") or []
        if isinstance(actions, dict):
            actions = list(actions.values())
        for a in actions:
            if isinstance(a, dict) and (a.get("IsPlayAction") or a.get("IsPlayAction") is True):
                return a
        return actions[0] if actions and isinstance(actions[0], dict) else {}

    def _parse(self, g):
        if not isinstance(g, dict) or not bool(g.get("IsInstalled", g.get("isInstalled", False))):
            return None
        gid = str(g.get("Id") or g.get("id") or "")
        if not gid:
            return None
        action = self._action(g)
        return {
            "playnite_id": gid,
            "name": g.get("Name") or g.get("name") or "Unknown Game",
            "game_id": g.get("GameId") or g.get("gameId"),
            "source_id": g.get("SourceId") or g.get("sourceId"),
            "install_directory": str(g.get("InstallDirectory") or g.get("installDirectory") or ""),
            "executable": str(action.get("Path") or action.get("path") or ""),
            "arguments": str(action.get("Arguments") or action.get("arguments") or ""),
            "working_directory": str(action.get("WorkingDir") or action.get("WorkingDirectory") or ""),
            "description": g.get("Description") or g.get("description"),
            "release_date": g.get("ReleaseDate") or g.get("releaseDate"),
            "playtime": g.get("Playtime") or g.get("playtime") or 0,
        }

    def _ensure_started(self):
        if not self.enabled or not self.playnite_path or not self.playnite_path.is_file():
            return False
        if self._window():
            return True
        try:
            subprocess.Popen([str(self.playnite_path)], cwd=str(self.playnite_path.parent), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._startup_attempted = True
            return True
        except Exception as e:
            self._last_error = str(e)
            log.warning("Could not start Playnite: %s", e)
            return False

    def read_games(self, force=False):
        with self._lock:
            if not self.available:
                if self.enabled and not self._startup_attempted and self._ensure_started():
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and not self.available:
                        time.sleep(.25)
                if not self.available:
                    self._games = []
                    self._signature = None
                    return []
            sig = self._signature_now()
            if not force and sig == self._signature and self._games:
                return list(self._games)
            try:
                raw_games = self._read_litedb()
                games = []
                for raw in raw_games:
                    g = self._parse(raw)
                    if g:
                        games.append(g)
                self._games = games
                self._signature = sig
                self._last_refresh = time.time()
                self._last_error = None
                log.info("Playnite library read: %d installed game(s)", len(games))
                return list(games)
            except Exception as e:
                self._last_error = str(e)
                log.exception("Could not read Playnite games.db")
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
                r = subprocess.run(["tasklist", "/FI", f"PID eq {pid.value}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=2, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if "Playnite.DesktopApp.exe" in r.stdout:
                    found["h"] = hwnd
                    return False
            except Exception:
                pass
            return True
        user32.EnumWindows(enum, 0)
        return found["h"]

    def refresh(self, force=False):
        if not self.enabled or (not force and not self.needs_refresh()):
            return False
        try:
            hwnd = self._window()
            if not hwnd and self._ensure_started():
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not self._window():
                    time.sleep(.25)
                hwnd = self._window()
            if hwnd:
                u = ctypes.windll.user32
                u.PostMessageW(hwnd, 0x0100, 0x74, 0)
                u.PostMessageW(hwnd, 0x0101, 0x74, 0)
                time.sleep(.5)
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
            if not self._window():
                self._ensure_started()
                time.sleep(.5)
            subprocess.Popen([str(self.playnite_path), "--start", str(playnite_id)], cwd=str(self.playnite_path.parent), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return {"ok": True, "playnite_id": str(playnite_id)}
        except Exception as e:
            return {"ok": False, "error": "playnite_launch_failed", "detail": str(e)}

    def uri(self, playnite_id):
        return "playnite://playnite/start/" + quote(str(playnite_id), safe="")
