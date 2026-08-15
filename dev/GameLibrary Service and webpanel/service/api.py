from pathlib import Path
import ctypes
import html
import io
import json
import os
import socket
import subprocess
import urllib.parse
import urllib.request

import qrcode
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response

from .playnite import PlayniteBridge

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
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try: return socket.gethostbyname(socket.gethostname())
        except OSError: return "127.0.0.1"
    finally: sock.close()


def _steam_details(app_id):
    url = f"https://store.steampowered.com/api/appdetails?appids={urllib.parse.quote(str(app_id))}&cc=ca&l=english"
    request = urllib.request.Request(url, headers={"User-Agent": "GameLibrary/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response: payload = json.loads(response.read().decode("utf-8"))
    item = payload.get(str(app_id), {})
    if not item.get("success"): return {}
    data = item.get("data", {})
    movies = data.get("movies") or []
    trailer = None
    if movies:
        movie = movies[0]
        trailer = (movie.get("mp4") or {}).get("max") or (movie.get("mp4") or {}).get("480") or (movie.get("webm") or {}).get("max") or (movie.get("webm") or {}).get("480")
    return {"title": data.get("name"), "description": data.get("short_description") or data.get("detailed_description"), "release_date": (data.get("release_date") or {}).get("date"), "hero": data.get("background_raw") or data.get("background"), "capsule": data.get("header_image"), "trailer": trailer, "steam_url": f"https://store.steampowered.com/app/{app_id}/"}


def _steam_search(name):
    url = "https://store.steampowered.com/api/storesearch/?" + urllib.parse.urlencode({"term": name, "cc": "ca", "l": "english"})
    request = urllib.request.Request(url, headers={"User-Agent": "GameLibrary/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response: payload = json.loads(response.read().decode("utf-8"))
    items = payload.get("items") or []
    return _steam_details(items[0].get("id")) if items and items[0].get("id") else {}


def _norm_name(value): return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())

def _norm_path(value):
    if not value: return None
    try: return os.path.normcase(os.path.normpath(str(value))).rstrip("\\/")
    except Exception: return str(value).lower().rstrip("\\/")

def _path_is_inside(child, parent):
    child, parent = _norm_path(child), _norm_path(parent)
    if not child or not parent: return False
    try: return os.path.commonpath([child, parent]) == parent
    except ValueError: return False


def _merge_match(gd, pn):
    gd_root = Path(f"{gd['last_letter']}:\\") if gd.get("last_letter") else None
    gd_folder = (gd_root / gd["relative_path"] / "Game") if gd_root else None
    gd_exe = None
    try:
        exe_file = Path(f"{gd['last_letter']}:\\") / gd["relative_path"] / "exepath.txt"
        if exe_file.is_file():
            relative = exe_file.read_text(encoding="utf-8-sig").strip().replace("/", "\\")
            if relative and not Path(relative).is_absolute() and ".." not in Path(relative).parts:
                gd_exe = str((Path(f"{gd['last_letter']}:\\") / gd["relative_path"] / "Game" / relative).resolve())
    except (OSError, ValueError): pass
    pn_install, pn_exe = pn.get("install_directory"), pn.get("executable")
    if pn_install and gd_folder and _norm_path(pn_install) == _norm_path(gd_folder): return True
    if pn_exe and gd_exe and _norm_path(pn_exe) == _norm_path(gd_exe): return True
    if pn_install and gd_folder and (_path_is_inside(pn_install, gd_folder) or _path_is_inside(gd_folder, pn_install)): return True
    if _norm_name(gd["title"] or gd["name"]) == _norm_name(pn["name"]):
        if pn_install and gd_root and _path_is_inside(pn_install, str(gd_root)): return True
    return False


def create_app(db, metadata=None, config=None, playnite=None):
    app = FastAPI(title="Game Library API", version="1.1.0")
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1", "http://localhost"], allow_methods=["GET", "POST"], allow_headers=["*"])
    playnite = playnite or PlayniteBridge((config or {}).get("playnite", {}))

    def playnite_games():
        if playnite.needs_refresh(): playnite.read_games(force=True)
        return playnite.read_games()

    def unified_games(query="", connected_only=False, mode="playlist"):
        gd_rows = [dict(row) for row in db.search(query, connected_only)]
        pn_rows = playnite_games()
        used = set(); result = []
        for gd in gd_rows:
            match = next((pn for pn in pn_rows if pn["playnite_id"] not in used and _merge_match(gd, pn)), None)
            if match: used.add(match["playnite_id"])
            item = dict(gd)
            item.update({"unified_id": f"gd:{gd['id']}", "source": "gamedrive", "playnite_managed": bool(match), "playnite_id": match["playnite_id"] if match else None, "playtime": match.get("playtime", 0) if match else 0})
            if match:
                for field in ("cover", "hero", "logo", "description", "release_date"):
                    if match.get(field): item[field] = match[field]
                item["installation_state"], item["launch_source"] = "Installed", "Playnite"
            else:
                item["installation_state"] = "Installed" if gd["connected"] else "Offline"
                item["launch_source"] = "GameDrive"
            result.append(item)
        if mode != "drives":
            for pn in pn_rows:
                if pn["playnite_id"] in used: continue
                if query and _norm_name(query) not in _norm_name(pn["name"]): continue
                item = dict(pn)
                item.update({"id": None, "unified_id": f"pn:{pn['playnite_id']}", "source": "playnite", "playnite_managed": True, "playnite_id": pn["playnite_id"], "title": pn["name"], "connected": True, "last_letter": Path(pn["install_directory"]).drive.rstrip(":") if pn.get("install_directory") else None, "drive_name": "Playnite", "installation_state": "Installed", "launch_source": "Playnite"})
                result.append(item)
        result.sort(key=lambda x: str(x.get("title") or x.get("name") or "").lower())
        return result

    def find_unified(unified_id):
        for item in unified_games():
            if item["unified_id"] == unified_id: return item
        return None

    @app.get("/")
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
    @app.get("/api/playnite/media/{playnite_id}/{kind}")
    def playnite_media(playnite_id: str, kind: str):
        game = next((g for g in playnite_games() if g["playnite_id"] == playnite_id), None)
        if not game or kind not in ("cover", "hero", "logo"): return {"error": "not_found"}
        value = game.get(kind)
        if not value or str(value).startswith(("http://", "https://")): return {"error": "not_found"}
        path = Path(value).resolve(); root = playnite.library_path.resolve()
        if root not in path.parents or not path.is_file(): return {"error": "not_found"}
        return FileResponse(path)
    @app.get("/api/health")
    def health(): return {"ok": True, "service": "GameLibrary", "metadata_enabled": bool(metadata and metadata.enabled), "artwork_queue": metadata._queue.qsize() if metadata else 0, "playnite": playnite.status}
    @app.get("/api/network")
    def network():
        host, port = _local_ip(), int((config or {}).get("api_port", 8765))
        return {"ip": host, "port": port, "url": f"http://{host}:{port}/", "playnite_configured": bool(playnite.playnite_path)}
    @app.get("/api/network/qr")
    def network_qr(text: str = Query(..., min_length=1, max_length=500)):
        image = qrcode.make(text); buffer = io.BytesIO(); image.save(buffer, format="PNG")
        return Response(content=buffer.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})
    @app.post("/api/playnite/fullscreen")
    def playnite_fullscreen():
        path = playnite.playnite_path
        if not path: return {"ok": False, "error": "playnite_not_configured"}
        if not path.is_file(): return {"ok": False, "error": "playnite_path_not_found"}
        try:
            subprocess.Popen([str(path), "--startfullscreen"], cwd=str(path.parent), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return {"ok": True, "path": str(path)}
        except Exception as exc: return {"ok": False, "error": "playnite_launch_failed", "detail": str(exc)}
    @app.get("/api/drives")
    def drives():
        with db.lock: rows = db.conn.execute("SELECT id,uuid,name,description,last_letter,connected,last_seen FROM drives ORDER BY connected DESC,name COLLATE NOCASE").fetchall()
        return [dict(row) for row in rows]
    @app.get("/api/system/disks")
    def system_disks(): return _physical_disks()
    @app.get("/api/games")
    def games(q: str = Query("", max_length=200), connected_only: bool = False, mode: str = Query("playlist")):
        if mode not in ("playlist", "drives"): mode = "playlist"
        rows = unified_games(q, connected_only, mode)
        if metadata and metadata.auto_lookup:
            for row in rows:
                if row.get("id") is not None and not row.get("capsule"): metadata.queue_lookup(row["id"], row.get("name") or row.get("title") or "")
        return rows
    @app.get("/api/games/{game_id}")
    def game(game_id: str): return find_unified(game_id) or {"error": "not_found"}
    @app.get("/api/games/{game_id}/details")
    def game_details(game_id: str):
        item = find_unified(game_id)
        if not item: return {"error": "not_found"}
        result = dict(item)
        if result.get("playnite_id"):
            result["playnite_uri"] = playnite.uri(result["playnite_id"])
            for kind in ("cover", "hero", "logo"):
                if result.get(kind) and not str(result[kind]).startswith(("http://", "https://")):
                    result[kind] = f"/api/playnite/media/{urllib.parse.quote(str(result['playnite_id']), safe='')}/{kind}"
        steam = {}
        try:
            if result.get("app_id"): steam = _steam_details(result["app_id"])
            elif result.get("title") or result.get("name"): steam = _steam_search(result.get("title") or result.get("name"))
        except Exception: pass
        result.update({k: v for k, v in steam.items() if v and not result.get(k)})
        if result.get("description"): result["description"] = html.unescape(result["description"])
        return result
    @app.post("/api/games/{game_id}/launch")
    def launch_game(game_id: str):
        item = find_unified(game_id)
        if not item: return {"ok": False, "error": "not_found"}
        if item.get("playnite_id"): return playnite.launch(item["playnite_id"])
        if not item.get("connected") or not item.get("last_letter"): return {"ok": False, "error": "drive_offline"}
        drive_root = Path(f"{item['last_letter']}:\\"); game_folder = (drive_root / item["relative_path"]).resolve(); game_dir = (game_folder / "Game").resolve(); exe_file = (game_folder / "exepath.txt").resolve()
        try: exe_relative = exe_file.read_text(encoding="utf-8-sig").strip().replace("/", "\\")
        except Exception: return {"ok": False, "error": "missing_exepath"}
        if not exe_relative or Path(exe_relative).is_absolute() or ".." in Path(exe_relative).parts: return {"ok": False, "error": "invalid_exepath"}
        executable = (game_dir / exe_relative).resolve()
        if game_dir not in executable.parents or not executable.is_file(): return {"ok": False, "error": "executable_not_found"}
        try: os.startfile(str(executable)); return {"ok": True, "path": str(executable)}
        except Exception as exc: return {"ok": False, "error": "launch_failed", "detail": str(exc)}
    return app
