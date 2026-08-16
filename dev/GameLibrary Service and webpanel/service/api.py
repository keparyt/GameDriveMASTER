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

BASE_DIR=Path(__file__).resolve().parent.parent
WEB_DIR=BASE_DIR/"web"
ARTWORK_DIR=BASE_DIR/"data"/"images"

def _physical_disks():
    if not hasattr(ctypes,"windll"): return []
    command="Get-Disk -ErrorAction SilentlyContinue | Select-Object Number,FriendlyName,SerialNumber,BusType,MediaType,Size,OperationalStatus,HealthStatus,IsOffline,IsReadOnly | ConvertTo-Json -Compress"
    try:
        r=subprocess.run(["powershell","-NoProfile","-NonInteractive","-Command",command],capture_output=True,text=True,timeout=5,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if r.returncode or not r.stdout.strip(): return []
        data=json.loads(r.stdout); data=[data] if isinstance(data,dict) else data
        return [{"number":d.get("Number"),"name":d.get("FriendlyName") or "Unknown disk","serial":d.get("SerialNumber") or "","bus":d.get("BusType") or "Unknown","media":d.get("MediaType") or "Unknown","size":int(d.get("Size") or 0),"status":d.get("OperationalStatus") or "Unknown","health":d.get("HealthStatus") or "Unknown","offline":bool(d.get("IsOffline")),"readonly":bool(d.get("IsReadOnly"))} for d in data]
    except Exception: return []

def _local_ip():
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    try: s.connect(("8.8.8.8",80)); return s.getsockname()[0]
    except OSError:
        try: return socket.gethostbyname(socket.gethostname())
        except OSError: return "127.0.0.1"
    finally: s.close()

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
    """Resolve Playnite local artwork paths, including Windows file:// and relative paths."""
    if not value:return None
    value=str(value).strip()
    if not value:return None
    if value.startswith("file://"):
        parsed=urllib.parse.urlparse(value)
        if parsed.netloc:
            value=f"\\\\{parsed.netloc}{urllib.parse.unquote(parsed.path)}"
        else:
            value=urllib.parse.unquote(parsed.path)
            if len(value)>=3 and value.startswith("/") and value[2]==":": value=value[1:]
    value=os.path.expandvars(os.path.expanduser(value))
    p=Path(value)
    candidates=[]
    if p.is_absolute():
        candidates.append(p)
    else:
        for base in base_paths:
            if base: candidates.append(Path(base)/p)
        candidates.append(Path.cwd()/p)
    for candidate in candidates:
        try:
            resolved=candidate.resolve()
            if resolved.is_file():return resolved
        except Exception:pass
    return None

def create_app(db,metadata=None,config=None,playnite=None):
    app=FastAPI(title="Game Library API",version="1.1.0")
    app.add_middleware(CORSMiddleware,allow_origins=["http://127.0.0.1","http://localhost"],allow_methods=["GET","POST"],allow_headers=["*"])
    playnite=playnite or PlayniteBridge((config or {}).get("playnite",{}))
    def p_games(): return playnite.read_games()
    def media(pid,kind,value):
        if not value:return None
        value=str(value)
        if value.startswith(("http://","https://","data:")):return value
        return "/api/playnite/media/"+urllib.parse.quote(str(pid),safe="")+"/"+kind
    def library(q="",connected=False,mode="playlist"):
        gd=[dict(r) for r in db.search(q,connected)]; pn=p_games(); used=set(); out=[]
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
    def find(uid):return next((x for x in library() if x["unified_id"]==uid),None)
    @app.get("/",response_class=HTMLResponse)
    def index():return (WEB_DIR/"index.html").read_text(encoding="utf-8")
    for route,file,ctype in [("/style.css","style.css","text/css"),("/theme.css","theme.css","text/css"),("/network.css","network.css","text/css"),("/app.js","app.js","application/javascript"),("/network.js","network.js","application/javascript")]:app.add_api_route(route,lambda f=file,c=ctype:FileResponse(WEB_DIR/f,media_type=c),methods=["GET"])
    @app.get("/artwork/{game_name}/{filename}")
    def artwork(game_name:str,filename:str):
        path=(ARTWORK_DIR/game_name/filename).resolve();root=ARTWORK_DIR.resolve();return FileResponse(path) if root in path.parents and path.is_file() else {"error":"not_found"}
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
                with urllib.request.urlopen(req,timeout=8) as r:data=r.read();ctype=r.headers.get_content_type() or "application/octet-stream"
                return Response(data,media_type=ctype,headers={"Cache-Control":"public, max-age=86400"})
            except Exception:return {"error":"media_unavailable"}
        appdata=Path(os.environ.get("APPDATA","") ) if os.environ.get("APPDATA") else None
        bases=[playnite.library_path, playnite.library_path.parent if playnite.library_path else None, playnite.playnite_path.parent if playnite.playnite_path else None, appdata/"Playnite" if appdata else None]
        path=_media_file_path(value,bases)
        if path:
            allowed=[]
            for root in bases:
                if root:
                    try: allowed.append(Path(root).resolve())
                    except Exception: pass
            if any(root==path or root in path.parents for root in allowed):
                return FileResponse(path)
        return {"error":"not_found"}
    @app.get("/api/health")
    def health():return {"ok":True,"service":"GameLibrary","playnite":playnite.status,"metadata_enabled":bool(metadata and metadata.enabled)}
    @app.get("/api/network")
    def network():host,port=_local_ip(),int((config or {}).get("api_port",8765));return {"ip":host,"port":port,"url":f"http://{host}:{port}/","playnite_configured":bool(playnite.playnite_path)}
    @app.get("/api/network/qr")
    def qr(text:str=Query(...,min_length=1,max_length=500)):
        image=qrcode.make(text);b=io.BytesIO();image.save(b,format="PNG");return Response(b.getvalue(),media_type="image/png",headers={"Cache-Control":"no-store"})
    @app.post("/api/playnite/fullscreen")
    def fullscreen():
        if not playnite.playnite_path:return {"ok":False,"error":"playnite_not_configured"}
        if not playnite.playnite_path.is_file():return {"ok":False,"error":"playnite_path_not_found"}
        try:subprocess.Popen([str(playnite.playnite_path),"--startfullscreen"],cwd=str(playnite.playnite_path.parent),creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0));return {"ok":True}
        except Exception as e:return {"ok":False,"error":"playnite_launch_failed","detail":str(e)}
    @app.get("/api/drives")
    def drives():
        with db.lock:r=db.conn.execute("SELECT id,uuid,name,description,last_letter,connected,last_seen FROM drives ORDER BY connected DESC,name COLLATE NOCASE").fetchall()
        return [dict(x) for x in r]
    @app.get("/api/system/disks")
    def disks():return _physical_disks()
    @app.get("/api/games")
    def games(q:str=Query("",max_length=200),connected_only:bool=False,mode:str="playlist"):return library(q,connected_only,mode if mode in ("playlist","drives") else "playlist")
    @app.get("/api/games/{game_id}")
    def game(game_id:str):return find(game_id) or {"error":"not_found"}
    @app.get("/api/games/{game_id}/details")
    def details(game_id:str):
        x=find(game_id)
        if not x:return {"error":"not_found"}
        result=dict(x)
        if result.get("playnite_id"):result["playnite_uri"]=playnite.uri(result["playnite_id"])
        try:
            steam=_steam_details(result["app_id"]) if result.get("app_id") else _steam_search(result.get("title") or result.get("name"))
            result.update({k:v for k,v in steam.items() if v and not result.get(k)})
        except Exception:pass
        if result.get("description"):result["description"]=html.unescape(result["description"])
        return result
    @app.post("/api/games/{game_id}/launch")
    def launch(game_id:str):
        x=find(game_id)
        if not x:return {"ok":False,"error":"not_found"}
        if x.get("playnite_id"):return playnite.launch(x["playnite_id"])
        if not x.get("connected") or not x.get("last_letter"):return {"ok":False,"error":"drive_offline"}
        root=Path(f"{x['last_letter']}:\\");folder=(root/x["relative_path"]).resolve();game_dir=(folder/"Game").resolve();ef=(folder/"exepath.txt").resolve()
        try:rel=ef.read_text(encoding="utf-8-sig").strip().replace("/","\\")
        except Exception:return {"ok":False,"error":"missing_exepath"}
        if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:return {"ok":False,"error":"invalid_exepath"}
        exe=(game_dir/rel).resolve()
        if game_dir not in exe.parents or not exe.is_file():return {"ok":False,"error":"executable_not_found"}
        try:os.startfile(str(exe));return {"ok":True,"path":str(exe)}
        except Exception as e:return {"ok":False,"error":"launch_failed","detail":str(e)}
    return app