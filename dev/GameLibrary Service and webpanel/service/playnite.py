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
    """Read Playnite's installed games from its LiteDB library and launch by GUID."""

    def __init__(self, config=None):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))
        self.refresh_on_game_drive_change = bool(self.config.get("refreshOnGameDriveChange", True))
        self.playnite_path = self._path(self.config.get("path") or self.config.get("playnitePath")) or self._find_playnite()
        self.library_path = self._path(
            self.config.get("libraryPath") or self.config.get("library_path") or self.config.get("databasePath")
        ) or self._find_library()
        self.litedb_path = self._path(self.config.get("liteDbPath") or self.config.get("litedbPath"))
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
        return (Path(__file__).resolve().parent.parent / p).resolve() if not p.is_absolute() else p

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

    def _find_library(self):
        appdata = os.environ.get("APPDATA")
        if appdata:
            p = Path(appdata) / "Playnite" / "library"
            if (p / "games.db").is_file():
                return p
        if self.playnite_path:
            p = self.playnite_path.parent / "library"
            if (p / "games.db").is_file():
                return p
        return None

    @property
    def database_file(self):
        return self.library_path / "games.db" if self.library_path else None

    @property
    def available(self):
        return bool(self.enabled and self.database_file and self.database_file.is_file())

    @property
    def status(self):
        return {
            "enabled": self.enabled,
            "available": self.available,
            "playnite_path": str(self.playnite_path) if self.playnite_path else None,
            "library_path": str(self.library_path) if self.library_path else None,
            "database_path": str(self.database_file) if self.database_file else None,
            "litedb_path": str(self.litedb_path) if self.litedb_path else None,
            "last_refresh": self._last_refresh,
            "error": self._last_error,
            "game_count": len(self._games),
        }

    def _signature_now(self):
        if not self.available:
            return None
        now = time.monotonic()
        if now - self._checked_at < 2:
            return self._signature
        self._checked_at = now
        try:
            p = self.database_file.stat()
            return (p.st_mtime_ns, p.st_size)
        except OSError as e:
            self._last_error = str(e)
            return self._signature

    def needs_refresh(self):
        return self._signature_now() != self._signature

    def _media(self, gid, value):
        if not value:
            return None
        s = str(value)
        if s.startswith(("http://", "https://")):
            return s
        p = Path(s)
        if p.is_absolute() and p.is_file():
            return str(p)
        p = self.library_path / "files" / str(gid) / s
        return str(p) if p.is_file() else None

    @staticmethod
    def _norm_key(d, *keys):
        lowered = {str(k).lower(): v for k, v in d.items()} if isinstance(d, dict) else {}
        for key in keys:
            if key in d:
                return d[key]
            if str(key).lower() in lowered:
                return lowered[str(key).lower()]
        return None

    def _parse_game(self, g):
        if not isinstance(g, dict):
            return None
        installed = self._norm_key(g, "IsInstalled", "isInstalled", "Installed")
        if not bool(installed):
            return None

        gid = self._norm_key(g, "Id", "id", "_id", "GameId")
        if isinstance(gid, dict):
            gid = gid.get("$guid") or gid.get("guid") or gid.get("value")
        if not gid:
            return None
        gid = str(gid)

        actions = self._norm_key(g, "GameActions", "gameActions") or []
        if isinstance(actions, dict):
            actions = list(actions.values())
        play_action = {}
        for action in actions if isinstance(actions, list) else []:
            if not isinstance(action, dict):
                continue
            if bool(self._norm_key(action, "IsPlayAction", "isPlayAction")):
                play_action = action
                break
        if not play_action and isinstance(actions, list) and actions and isinstance(actions[0], dict):
            play_action = actions[0]

        return {
            "playnite_id": gid,
            "name": self._norm_key(g, "Name", "name") or "Unknown Game",
            "game_id": self._norm_key(g, "GameId", "gameId"),
            "source_id": self._norm_key(g, "SourceId", "sourceId"),
            "install_directory": str(self._norm_key(g, "InstallDirectory", "installDirectory") or ""),
            "executable": str(self._norm_key(play_action, "Path", "path") or ""),
            "arguments": str(self._norm_key(play_action, "Arguments", "arguments") or ""),
            "working_directory": str(self._norm_key(play_action, "WorkingDir", "WorkingDirectory", "workingDirectory") or ""),
            "cover": self._media(gid, self._norm_key(g, "CoverImage", "coverImage")),
            "hero": self._media(gid, self._norm_key(g, "BackgroundImage", "backgroundImage")),
            "logo": self._media(gid, self._norm_key(g, "Icon", "icon")),
            "description": self._norm_key(g, "Description", "description"),
            "release_date": self._norm_key(g, "ReleaseDate", "releaseDate"),
            "playtime": self._norm_key(g, "Playtime", "playtime") or 0,
        }

    def _litedb_dll(self):
        if self.litedb_path and self.litedb_path.is_file():
            return self.litedb_path
        if not self.playnite_path:
            return None
        roots = [
            self.playnite_path.parent,
            self.playnite_path.parent / "Libraries",
            self.playnite_path.parent / "lib",
        ]
        # Playnite installs LiteDB with the application, but the exact subfolder
        # differs between versions. Search the install tree instead of assuming
        # one fixed path.
        for root in roots:
            try:
                for p in root.rglob("LiteDB.dll"):
                    if p.is_file():
                        self.litedb_path = p
                        return p
            except OSError:
                pass
        return None

    @staticmethod
    def _ps_quote(path):
        # PowerShell single-quoted string escaping.
        return str(path).replace("'", "''")

    def _read_litedb(self):
        """Use Playnite's own LiteDB dependency to read the real games.db."""
        db = self.database_file
        dll = self._litedb_dll()
        if not db or not db.is_file():
            return []
        if not dll or not dll.is_file():
            raise RuntimeError(
                "LiteDB.dll was not found in the configured Playnite installation. "
                "Set playnite.liteDbPath to the actual LiteDB.dll if Playnite uses a custom/plugin layout."
            )

        db_ps = self._ps_quote(db)
        dll_ps = self._ps_quote(dll)
        script = f'''$ErrorActionPreference = 'Stop'
Add-Type -Path '{dll_ps}'
$db = New-Object LiteDB.LiteDatabase("Filename={db_ps}; Mode=Shared")
try {{
    $collection = $db.GetCollection("games")
    $items = @()
    foreach ($doc in $collection.FindAll()) {{
        $obj = [ordered]@{{}}
        foreach ($key in $doc.Keys) {{
            $v = $doc[$key]
            if ($null -eq $v) {{ $obj[$key] = $null; continue }}
            try {{
                if ($v.IsDocument) {{ $obj[$key] = $v.AsDocument.RawValue }}
                elseif ($v.IsArray) {{ $obj[$key] = $v.AsArray.RawValue }}
                elseif ($v.IsGuid) {{ $obj[$key] = $v.AsGuid.ToString() }}
                elseif ($v.IsString) {{ $obj[$key] = $v.AsString }}
                elseif ($v.IsBoolean) {{ $obj[$key] = $v.AsBoolean }}
                elseif ($v.IsInt32) {{ $obj[$key] = $v.AsInt32 }}
                elseif ($v.IsInt64) {{ $obj[$key] = $v.AsInt64 }}
                elseif ($v.IsDouble) {{ $obj[$key] = $v.AsDouble }}
                elseif ($v.IsDateTime) {{ $obj[$key] = $v.AsDateTime.ToString('o') }}
                else {{ $obj[$key] = $v.RawValue }}
            }} catch {{ $obj[$key] = $v.ToString() }}
        }}
        $items += [pscustomobject]$obj
    }}
    $items | ConvertTo-Json -Depth 20 -Compress
}} finally {{ $db.Dispose() }}'''
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout or "LiteDB read failed").strip())
        raw = r.stdout.strip()
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]

    def _ensure_started(self):
        if not self.enabled or not self.playnite_path or not self.playnite_path.is_file():
            return False
        if self._window():
            return True
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
            return True
        except Exception as e:
            self._last_error = str(e)
            log.warning("Could not start Playnite: %s", e)
            return False

    def read_games(self, force=False):
        with self._lock:
            if not self.available:
                if self.enabled and not self._startup_attempted and self._ensure_started():
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline and not self.available:
                        time.sleep(.25)
                if not self.available:
                    self._games = []
                    self._signature = None
                    return []

            sig = self._signature_now()
            if not force and sig == self._signature:
                return list(self._games)

            try:
                raw_games = self._read_litedb()
                out = []
                for g in raw_games:
                    parsed = self._parse_game(g)
                    if parsed:
                        out.append(parsed)
                self._games = out
                self._signature = sig
                self._last_refresh = time.time()
                self._last_error = None
                log.info("Playnite LiteDB read: %d installed game(s)", len(out))
                return list(out)
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
        if not self.enabled or (not force and not self.needs_refresh()):
            return False
        try:
            hwnd = self._window()
            if not hwnd and self._ensure_started():
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and not self._window():
                    time.sleep(.25)
                hwnd = self._window()
            if hwnd:
                u = ctypes.windll.user32
                u.PostMessageW(hwnd, 0x0100, 0x74, 0)
                u.PostMessageW(hwnd, 0x0101, 0x74, 0)
                time.sleep(.75)
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
                time.sleep(.75)
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
