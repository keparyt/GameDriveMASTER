from pathlib import Path
import ctypes
import html
import io
import json
import socket
import subprocess
import urllib.parse
import urllib.request

import qrcode
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
ARTWORK_DIR = BASE_DIR / "data" / "images"


def _physical_disks():
    if not hasattr(ctypes, "windll"):
        return []
    command = ("Get-Disk -ErrorAction SilentlyContinue | Select-Object Number,FriendlyName,SerialNumber,BusType,MediaType,Size,"
               "OperationalStatus,HealthStatus,IsOffline,IsReadOnly | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0 or not result.stdout.strip(): return []
        data = json.loads(result.stdout)
        if isinstance(data, dict): data = [data]
        return [{"number": d.get("Number"), "name": d.get("FriendlyName") or "Unknown disk", "serial": d.get("SerialNumber") or "", "bus": d.get("BusType") or "Unknown", "media": d.get("MediaType") or "Unknown", "size": int(d.get("Size") or 0), "status": d.get("OperationalStatus") or "Unknown", "health": d.get("HealthStatus") or "Unknown", "offline": bool(d.get("IsOffline")), "readonly": bool(d.get("IsReadOnly"))} for d in data]
    except Exception:
        return []


def _local_ip():
    """Return the LAN address other devices can normally use to reach this PC."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def _configured_playnite_path(config):
    value = str((config or {}).get("playnite_fullscreen_path", "") or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    return path


def _steam_details(app_id):
    url = f"https://store.steampowered.com/api/appdetails?appids={urllib.parse.quote(str(app_id))}&cc=ca&l=english"
    request = urllib.request.Request(url, headers={"User-Agent": "GameLibrary/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    item = payload.get(str(app_id), {})
    if not item.get("success"): return {}
    data = item.get("data", {})
    movies = data.get("movies") or []
    trailer = None
    if movies:
        movie = movies[0]
        trailer = (movie.get("mp4") or {}).get("max") or (movie.get("mp4") or {}).get("480")
        if not trailer: trailer = (movie.get("webm") or {}).get("max") or (movie.get("webm") or {}).get("480")
    return {"title": data.get("name"), "description": data.get("short_description") or data.get("detailed_description"), "release_date": (data.get("release_date") or {}).get("date"), "hero": data.get("background_raw") or data.get("background"), "capsule": data.get("header_image"), "trailer": trailer, "steam_url": f"https://store.steampowered.com/app/{app_id}/"}


def _steam_search(name):
    url = "https://store.steampowered.com/api/storesearch/?" + urllib.parse.urlencode({"term": name, "cc": "ca", "l": "english"})
    request = urllib.request.Request(url, headers={"User-Agent": "GameLibrary/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    items = payload.get("items") or []
    if not items: return {}
    return _steam_details(items[0].get("id")) if items[0].get("id") else {}


def create_app(db, metadata=None, config=None):
    app = FastAPI(title="Game Library API", version="1.0.0")
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1", "http://localhost"], allow_methods=["GET", "POST"], allow_headers=["*"])

    @app.get("/", response_class=HTMLResponse)
    def index(): return (WEB_DIR / "index.html").read_text(encoding="utf-8")
    @app.get("/style.css")
    def css(): return FileResponse(WEB_DIR / "style.css", media_type="text/css")
    @app.get("/theme.css")
    def theme(): return FileResponse(WEB_DIR / "theme.css", media_type="text/css")
    @app.get("/network.css")
    def network_css(): return FileResponse(WEB_DIR / "network.css", media_type="text/css")
    @app.get("/app.js")
    def javascript(): return FileResponse(WEB_DIR / "app.js", media_type="application/javascript")
    @app.get("/network.js")
    def network_javascript(): return FileResponse(WEB_DIR / "network.js", media_type="application/javascript")
    @app.get("/artwork/{game_name}/{filename}")
    def artwork(game_name: str, filename: str):
        path = (ARTWORK_DIR / game_name / filename).resolve(); root = ARTWORK_DIR.resolve()
        if root not in path.parents or not path.is_file(): return {"error": "not_found"}
        return FileResponse(path)
    @app.get("/api/health")
    def health(): return {"ok": True, "service": "GameLibrary", "metadata_enabled": bool(metadata and metadata.enabled), "artwork_queue": metadata._queue.qsize() if metadata else 0}
    @app.get("/api/network")
    def network():
        host = _local_ip()
        port = int((config or {}).get("api_port", 8765))
        return {"ip": host, "port": port, "url": f"http://{host}:{port}/", "playnite_configured": bool(_configured_playnite_path(config))}
    @app.get("/api/network/qr")
    def network_qr(text: str = Query(..., min_length=1, max_length=500)):
        image = qrcode.make(text)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return Response(content=buffer.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})
    @app.post("/api/playnite/fullscreen")
    def playnite_fullscreen():
        path = _configured_playnite_path(config)
        if not path: return {"ok": False, "error": "playnite_not_configured"}
        if not path.is_file(): return {"ok": False, "error": "playnite_path_not_found"}
        try:
            subprocess.Popen([str(path)], cwd=str(path.parent), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return {"ok": True, "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": "playnite_launch_failed", "detail": str(exc)}
    @app.get("/api/drives")
    def drives():
        with db.lock: rows = db.conn.execute("SELECT id,uuid,name,description,last_letter,connected,last_seen FROM drives ORDER BY connected DESC,name COLLATE NOCASE").fetchall()
        return [dict(row) for row in rows]
    @app.get("/api/system/disks")
    def system_disks(): return _physical_disks()
    @app.get("/api/games")
    def games(q: str = Query("", max_length=200), connected_only: bool = False):
        rows = db.search(q, connected_only)
        if metadata and metadata.auto_lookup:
            for row in rows:
                if not row["capsule"]: metadata.queue_lookup(row["id"], row["name"])
        return [dict(row) for row in db.search(q, connected_only)]
    @app.get("/api/games/{game_id}")
    def game(game_id: int):
        row = db.get_game(game_id)
        if not row: return {"error": "not_found"}
        if metadata and not row["capsule"]: metadata.queue_lookup(game_id, row["name"])
        return dict(db.get_game(game_id))
    @app.get("/api/games/{game_id}/details")
    def game_details(game_id: int):
        row = db.get_game(game_id)
        if not row: return {"error": "not_found"}
        result = dict(row)
        steam = {}
        try:
            if row["app_id"]: steam = _steam_details(row["app_id"])
            else: steam = _steam_search(row["title"] or row["name"])
        except Exception:
            steam = {}
        result.update({k: v for k, v in steam.items() if v})
        if result.get("description"): result["description"] = html.unescape(result["description"])
        return result
    @app.post("/api/games/{game_id}/launch")
    def launch_game(game_id: int):
        row = db.get_game(game_id)
        if not row: return {"ok": False, "error": "not_found"}
        if not row["connected"] or not row["last_letter"]: return {"ok": False, "error": "drive_offline"}
        drive_root = Path(f"{row['last_letter']}:\\")
        game_folder = (drive_root / row["relative_path"]).resolve(); game_dir = (game_folder / "Game").resolve(); exe_file = (game_folder / "exepath.txt").resolve()
        try: exe_relative = exe_file.read_text(encoding="utf-8-sig").strip().replace("/", "\\")
        except Exception: return {"ok": False, "error": "missing_exepath"}
        if not exe_relative or Path(exe_relative).is_absolute() or ".." in Path(exe_relative).parts: return {"ok": False, "error": "invalid_exepath"}
        executable = (game_dir / exe_relative).resolve()
        if game_dir not in executable.parents or not executable.is_file(): return {"ok": False, "error": "executable_not_found"}
        try:
            import os; os.startfile(str(executable)); return {"ok": True, "path": str(executable)}
        except Exception as exc: return {"ok": False, "error": "launch_failed", "detail": str(exc)}
    return app
