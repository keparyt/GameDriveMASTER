import ctypes
import json
import html
import io
import os
import socket
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

import qrcode
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from .playnite import PlayniteBridge
from .apps import scan_apps, find_app

BASE_DIR=Path(__file__).resolve().parent.parent
WEB_DIR=BASE_DIR/"web"
ARTWORK_DIR=BASE_DIR/"data"/"images"
APPS_DIR=BASE_DIR/"apps"

def _physical_disks():
    if not hasattr(ctypes,"windll"): return []
    command="Get-Disk -ErrorAction SilentlyContinue | Select-Object Number,FriendlyName,SerialNumber,BusType,MediaType,Size,OperationalStatus,HealthStatus,IsOffline,IsReadOnly | ConvertTo-Json -Compress"
    try:
        r=subprocess.run(["powershell","-NoProfile","-NonInteractive","-Command",command],capture_output=True,text=True,timeout=5,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if r.returncode or not r.stdout.strip(): return []
        data=json.loads(r.stdout); data=[data] if isinstance(data,dict) else data
        return [{"number":d.get("Number"),"name":d.get("FriendlyName") or "Unknown disk","serial":d.get("SerialNumber") or "","bus":d.get("BusType") or "Unknown","media":d.get("MediaType") or "Unknown","size":int(d.get("Size") or 0),"status":d.get("OperationalStatus") or "Unknown","health":d.get("HealthStatus") or "Unknown","offline":bool(d.get("IsOffline")),"readonly":bool(d.get("IsReadOnly"))} for d in data]
    except Exception: return []

def _storage_layout():
    if not hasattr(ctypes,"windll"): return []
    command="Get-Disk -ErrorAction SilentlyContinue | ForEach-Object { $d=$_; [pscustomobject]@{ Number=$d.Number; Name=$d.FriendlyName; Serial=$d.SerialNumber; Bus=$d.BusType; Size=$d.Size; Status=$d.OperationalStatus; Health=$d.HealthStatus; Offline=$d.IsOffline; Partitions=@(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{ Number=$_.PartitionNumber; Letter=([string]$_.DriveLetter); Size=$_.Size; Type=$_.Type; Status=$_.OperationalStatus } }) } } | ConvertTo-Json -Compress -Depth 5"
    try:
        r=subprocess.run(["powershell","-NoProfile","-NonInteractive","-Command",command],capture_output=True,text=True,timeout=5,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if r.returncode or not r.stdout.strip(): return []
        data=json.loads(r.stdout); data=[data] if isinstance(data,dict) else data
        out=[]
        for d in data:
            parts=d.get("Partitions") or []
            parts=[parts] if isinstance(parts,dict) else parts
            out.append({"number":d.get("Number"),"name":d.get("Name") or "Unknown disk","serial":d.get("Serial") or "","bus":d.get("Bus") or "Unknown","size":int(d.get("Size") or 0),"status":d.get("Status") or "Unknown","health":d.get("Health") or "Unknown","offline":bool(d.get("Offline")),"partitions":[{"number":p.get("Number"),"letter":p.get("Letter") or "","size":int(p.get("Size") or 0),"type":p.get("Type") or "","status":p.get("Status") or "Unknown"} for p in parts]})
        return out
    except Exception as exc:
        return []

def _local_ip():
    """
    Return the local LAN IPv4 address used by the web-panel QR code.

    Only 192.168.x.x addresses are accepted intentionally.
    This prevents the QR code from ever advertising 127.0.0.1,
    localhost, VPN addresses, or other interfaces.
    """
    try:
        # Windows: use ipconfig so we get the same LAN address
        # the user sees from their network configuration.
        result = subprocess.run(
            ["ipconfig"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
        )

        for line in result.stdout.splitlines():
            line = line.strip()

            if "IPv4 Address" not in line:
                continue

            # Handles:
            # IPv4 Address. . . . . . . . . . . : 192.168.1.123
            # IPv4 Address. . . . . . . . . . . : 192.168.1.123 (Preferred)
            if ":" not in line:
                continue

            ip = line.rsplit(":", 1)[1].strip()
            ip = ip.split("(")[0].strip()

            if ip.startswith("192.168."):
                parts = ip.split(".")

                if len(parts) == 4 and all(
                    part.isdigit() and 0 <= int(part) <= 255
                    for part in parts
                ):
                    return ip

    except Exception:
        pass

    # Secondary method in case ipconfig parsing fails.
    try:
        hostname = socket.gethostname()

        for ip in socket.gethostbyname_ex(hostname)[2]:
            if ip.startswith("192.168."):
                parts = ip.split(".")

                if len(parts) == 4 and all(
                    part.isdigit() and 0 <= int(part) <= 255
                    for part in parts
                ):
                    return ip

    except Exception:
        pass

    # Do NOT return 127.0.0.1.
    # Returning None lets the caller know that no suitable LAN
    # address was found.
    return None

def _steam_details(app_id):
    req=urllib.request.Request(f"https://store.steampowered.com/api/appdetails?appids={urllib.parse.quote(str(app_id))}&cc=ca&l=english",headers={"User-Agent":"GameLibrary/1.0"})
    with urllib.request.urlopen(req,timeout=8) as r: payload=json.loads(r.read().decode("utf-8"))
    item=payload.get(str(app_id),{})
    if not item.get("success"): return {}
    d=item.get("data",{}); movies=d.get("movies") or []; trailer=None
    if movies:
        m=movies[0]; trailer=(m.get("mp4") or {}).get("max") or (m.get("mp4") or {}).get("480") or (m.get("webm") or {}).get("max") or (m.get("webm") or {}).get("480")
    return {"title":d.get("name"),"description":d.get("short_description") or d.get("detailed_description"),"release_date":(d.get("release_date") or {}).get("date"),"hero":d.get("background_raw") or d.get("background"),"capsule":d.get("header_image"),"trailer":trailer,"steam_url":f"https://store.steampowered.com/app/{app_id}/"}

def _steam_search(name):
    req=urllib.request.Request("https://store.steampowered.com/api/storesearch/?"+urllib.parse.urlencode({"term":name,"cc":"ca","l":"english"}),headers={"User-Agent":"GameLibrary/1.0"})
    with urllib.request.urlopen(req,timeout=8) as r: payload=json.loads(r.read().decode("utf-8"))
    items=payload.get("items") or []
    return _steam_details(items[0]["id"]) if items and items[0].get("id") else {}

def _norm(v): return "".join(c.lower() for c in str(v or "") if c.isalnum())
def _npath(v):
    if not v:return None
    try:return os.path.normcase(os.path.normpath(str(v))).rstrip("\\/")
    except Exception:return str(v).lower().rstrip("\\/")
def _inside(a,b):
    a,b=_npath(a),_npath(b)
    if not a or not b:return False
    try:return os.path.commonpath([a,b])==b
    except ValueError:return False

def _matches(gd,pn):
    if not gd.get("last_letter"):return False
    root=Path(f"{gd['last_letter']}:\\"); game=root/gd["relative_path"]/"Game"; install=pn.get("install_directory")
    if install and _npath(install)==_npath(game):return True
    if install and (_inside(install,game) or _inside(game,install)):return True
    try:
        f=root/gd["relative_path"]/"exepath.txt"
        if f.is_file() and pn.get("executable"):
            rel=f.read_text(encoding="utf-8-sig").strip().replace("/","\\")
            if rel and not Path(rel).is_absolute() and ".." not in Path(rel).parts and _npath(pn["executable"])==_npath(game/rel):return True
    except (OSError,ValueError):pass
    return bool(install and _norm(gd.get("title") or gd.get("name"))==_norm(pn.get("name")) and _inside(install,root))

def _media_file_path(value, base_paths=()):
    if not value:return None
    value=str(value).strip()
    if not value:return None
    if value.startswith("file://"):
        parsed=urllib.parse.urlparse(value)
        if parsed.netloc:value=f"\\\\{parsed.netloc}{urllib.parse.unquote(parsed.path)}"
        else:
            value=urllib.parse.unquote(parsed.path)
            if len(value)>=3 and value.startswith("/") and value[2]==":": value=value[1:]
    value=os.path.expandvars(os.path.expanduser(value)); p=Path(value); candidates=[]
    if p.is_absolute(): candidates.append(p)
    else:
        for base in base_paths:
            if base:candidates.append(Path(base)/p)
        candidates.append(Path.cwd()/p)
    for candidate in candidates:
        try:
            resolved=candidate.resolve()
            if resolved.is_file():return resolved
        except Exception:pass
    return None

def create_app(db,metadata=None,config=None,playnite=None):
    app=FastAPI(title="Game Library API",version="1.2.0")
    app.add_middleware(CORSMiddleware,allow_origins=["http://127.0.0.1","http://localhost"],allow_methods=["GET","POST"],allow_headers=["*"])
    playnite=playnite or PlayniteBridge((config or {}).get("playnite",{}))
    def p_games(): return playnite.read_games()
    def media(pid,kind,value):
        if not value:return None
        value=str(value)
        if value.startswith(("http://","https://","data:")):return value
        return "/api/playnite/media/"+urllib.parse.quote(str(pid),safe="")+"/"+kind
    def app_media_url(app_id,kind,value):
        if not value:return None
        return "/api/apps/media/"+urllib.parse.quote(str(app_id),safe="")+"/"+kind
    def library(q="",connected=False,mode="playlist"):
        gd=[dict(r) for r in db.search(q,connected)]; pn=p_games(); used=set(); out=[]
        if mode=="apps":
            apps=scan_apps()
            if q: apps=[a for a in apps if _norm(q) in _norm(a["name"])]
            for a in apps:
                for k in ("cover","capsule","hero","icon","logo"):
                    if a.get(k):a[k]=app_media_url(a["app_id"],k,a[k])
            return sorted(apps,key=lambda x:str(x.get("title") or x.get("name") or "").lower())
        for g in gd:
            m=next((p for p in pn if p["playnite_id"] not in used and _matches(g,p)),None)
            if m:used.add(m["playnite_id"])
            x=dict(g); x["unified_id"]="gd:"+str(g["id"]); x["source"]="gamedrive"; x["playnite_managed"]=bool(m); x["playnite_id"]=m["playnite_id"] if m else None
            if m:
                for k in ("cover","capsule","hero","logo"):
                    if m.get(k):x[k]=media(m["playnite_id"],k,m[k])
                for k in ("description","release_date","playtime"):
                    if m.get(k) is not None:x[k]=m[k]
                x["installation_state"]="Installed";x["launch_source"]="Playnite"
            else:x["installation_state"]="Installed" if g["connected"] else "Offline";x["launch_source"]="GameDrive"
            out.append(x)
        if mode!="drives":
            for p in pn:
                if p["playnite_id"] in used or (q and _norm(q) not in _norm(p["name"])):continue
                x=dict(p);x.update({"id":None,"unified_id":"pn:"+str(p["playnite_id"]),"source":"playnite","playnite_managed":True,"playnite_id":p["playnite_id"],"title":p["name"],"connected":True,"last_letter":Path(p["install_directory"]).drive.rstrip(":") if p.get("install_directory") else None,"drive_name":"Playnite","installation_state":"Installed","launch_source":"Playnite"})
                for k in ("cover","capsule","hero","logo"):x[k]=media(p["playnite_id"],k,x.get(k))
                out.append(x)
        return sorted(out,key=lambda x:str(x.get("title") or x.get("name") or "").lower())
    def find(uid):
        app_item=next((x for x in scan_apps() if x["unified_id"].casefold()==str(uid).casefold()),None)
        if app_item:
            for k in ("cover","capsule","hero","icon","logo"):
                if app_item.get(k):app_item[k]=app_media_url(app_item["app_id"],k,app_item[k])
            return app_item
        return next((x for x in library() if x["unified_id"]==uid),None)
    @app.get("/",response_class=HTMLResponse)
    def index():return (WEB_DIR/"index.html").read_text(encoding="utf-8")
    for route,file,ctype in [("/style.css","style.css","text/css"),("/theme.css","theme.css","text/css"),("/network.css","network.css","text/css"),("/app.js","app.js","application/javascript"),("/network.js","network.js","application/javascript")]:app.add_api_route(route,lambda f=file,c=ctype:FileResponse(WEB_DIR/f,media_type=c),methods=["GET"])
    @app.get("/artwork/{game_name}/{filename}")
    def artwork(game_name:str,filename:str):
        path=(ARTWORK_DIR/game_name/filename).resolve();root=ARTWORK_DIR.resolve();return FileResponse(path) if root in path.parents and path.is_file() else {"error":"not_found"}
    @app.get("/api/network")
    def network_info():
        ip = _local_ip()

        return {
            "ip": ip,
            "local_ip": ip,
            "lan_ip": ip,
            "host": ip,
            "port": 8765,
            "url": f"http://{ip}:8765" if ip else None,
            "playnite_configured": bool(PLAYNITE_FULLSCREEN_PATH),
        }
    @app.get("/api/network/qr")
    def network_qr(text:str|None=None):
        ip=_local_ip()
        target=f"http://{ip}:8765"
        if text:
            candidate=urllib.parse.unquote(str(text)).strip(); parsed=urllib.parse.urlparse(candidate)
            if parsed.scheme in ("http","https") and parsed.hostname and parsed.port and parsed.hostname in ("127.0.0.1","localhost","0.0.0.0",ip):target=candidate
        qr=qrcode.QRCode(version=None,error_correction=qrcode.constants.ERROR_CORRECT_M,box_size=8,border=4); qr.add_data(target); qr.make(fit=True)
        image=qr.make_image(fill_color="black",back_color="white"); buf=io.BytesIO(); image.save(buf,format="PNG"); buf.seek(0)
        return Response(buf.getvalue(),media_type="image/png",headers={"Cache-Control":"no-store"})
    @app.get("/api/system/storage")
    def system_storage():
        return _storage_layout()
    @app.get("/api/apps/media/{app_id}/{kind}")
    def app_media(app_id:str,kind:str):
        if kind not in ("cover","capsule","hero","icon","logo"):return {"error":"not_found"}
        app_name=urllib.parse.unquote(str(app_id)); a=find_app(app_name)
        if not a:return {"error":"not_found"}
        app_folder=Path(a.get("relative_path") or a.get("app_id") or app_name); root=Path(os.environ.get("GAMEDRIVE_APPS_PATH") or APPS_DIR).resolve(); folder=(root/app_folder).resolve()
        if root not in folder.parents or not folder.is_dir():return {"error":"not_found"}
        names={"cover":("Capsule.jpg","Capsule.jpeg","Capsule.png","Capsule.webp"),"capsule":("Capsule.jpg","Capsule.jpeg","Capsule.png","Capsule.webp"),"hero":("Hero.jpg","Hero.jpeg","Hero.png","Hero.webp","Background.jpg","Background.jpeg","Background.png","Background.webp"),"icon":("Icon.png","Icon.jpg","Icon.jpeg","Icon.webp","Capsule.png","Capsule.jpg","Capsule.jpeg","Capsule.webp"),"logo":("Icon.png","Icon.jpg","Icon.jpeg","Icon.webp","Capsule.png","Capsule.jpg","Capsule.jpeg","Capsule.webp")}
        wanted={n.casefold() for n in names[kind]}
        try:path=next((p for p in folder.iterdir() if p.is_file() and p.name.casefold() in wanted),None)
        except OSError:path=None
        if path and path.is_file():return FileResponse(path,headers={"Cache-Control":"public, max-age=86400"})
        return {"error":"not_found"}
    @app.get("/api/playnite/media/{playnite_id}/{kind}")
    def p_media(playnite_id:str,kind:str):
        if kind not in ("cover","capsule","hero","logo"):return {"error":"not_found"}
        g=next((x for x in p_games() if x["playnite_id"]==playnite_id),None)
        if not g:return {"error":"not_found"}
        value=g.get("cover") if kind=="capsule" else g.get(kind)
        if not value:return {"error":"not_found"}
        value=str(value)
        if value.startswith("http://") or value.startswith("https://"):
            try:
                req=urllib.request.Request(value,headers={"User-Agent":"GameLibrary/1.0"})
                with urllib.request.urlopen(req,timeout=10) as r:return Response(r.read(),media_type=r.headers.get_content_type(),headers={"Cache-Control":"public, max-age=86400"})
            except Exception:return {"error":"not_found"}
        path=_media_file_path(value,[Path(g.get("install_directory") or ""),BASE_DIR/"apps"])
        if path:return FileResponse(path,headers={"Cache-Control":"public, max-age=86400"})
        return {"error":"not_found"}
    @app.get("/api/games")
    def games(q:str="",connected_only:bool=Query(False),mode:str="playlist"):return library(q,connected_only,mode)
    @app.get("/api/games/{game_id}")
    def game(game_id:str):return find(game_id) or {"error":"not_found"}
    @app.get("/api/games/{game_id}/details")
    def details(game_id:str):
        x=find(game_id)
        if not x:return {"error":"not_found"}
        return x
    @app.post("/api/games/{game_id}/launch")
    def launch(game_id:str):
        x=find(game_id)
        if not x:return {"ok":False,"error":"not_found"}
        if x.get("source")=="apps":
            exe=x.get("executable")
            if not exe:return {"ok":False,"error":"app_not_launchable"}
            try:
                args=x.get("arguments") or []; args=[args] if isinstance(args,str) else args; subprocess.Popen([exe,*args],cwd=x.get("working_directory") or None); return {"ok":True}
            except Exception as e:return {"ok":False,"error":"app_launch_failed","detail":str(e)}
        if x.get("playnite_id") and x.get("launch_source")=="Playnite":return playnite.launch(x["playnite_id"])
        return {"ok":False,"error":"not_launchable"}
    return app