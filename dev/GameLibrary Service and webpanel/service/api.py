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
from .apps import scan_apps, find_app

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

def _norm(v): return "".join(c.lower() for c in str(v or "") if c.isalnum())
def _media_file_path(value,base_paths=()):
    if not value:return None
    value=str(value).strip()
    if value.startswith("file://"):
        parsed=urllib.parse.urlparse(value)
        if parsed.netloc:value=f"\\\\{parsed.netloc}{urllib.parse.unquote(parsed.path)}"
        else:
            value=urllib.parse.unquote(parsed.path)
            if len(value)>=3 and value.startswith("/") and value[2]==":":value=value[1:]
    value=os.path.expandvars(os.path.expanduser(value));p=Path(value);candidates=[p] if p.is_absolute() else [Path(b)/p for b in base_paths if b]
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
    def p_games():return playnite.read_games()
    def media(pid,kind,value):
        if not value:return None
        value=str(value)
        if value.startswith(("http://","https://","data:")):return value
        return "/api/playnite/media/"+urllib.parse.quote(str(pid),safe="")+"/"+kind
    def app_media_url(app_id,kind,value):
        if not value:return None
        return "/api/apps/media/"+urllib.parse.quote(str(app_id),safe="")+"/"+kind
    def library(q="",connected=False,mode="playlist"):
        if mode=="apps":
            apps=scan_apps()
            if q:apps=[a for a in apps if _norm(q) in _norm(a["name"])]
            for a in apps:
                for k in ("cover","capsule","hero","icon","logo"):
                    if a.get(k):a[k]=app_media_url(a["app_id"],k,a[k])
            return sorted(apps,key=lambda x:str(x.get("title") or x.get("name") or "").lower())
        gd=[dict(r) for r in db.search(q,connected)];pn=p_games();used=set();out=[]
        for g in gd:
            x=dict(g);x["unified_id"]="gd:"+str(g["id"]);x["source"]="gamedrive";x["playnite_managed"]=False;x["playnite_id"]=None;out.append(x)
        if mode!="drives":
            for p in pn:
                if q and _norm(q) not in _norm(p["name"]):continue
                x=dict(p);x.update({"id":None,"unified_id":"pn:"+str(p["playnite_id"]),"source":"playnite","playnite_managed":True,"playnite_id":p["playnite_id"],"title":p["name"],"connected":True,"drive_name":"Playnite","installation_state":"Installed","launch_source":"Playnite"})
                for k in ("cover","capsule","hero","logo"):x[k]=media(p["playnite_id"],k,x.get(k))
                out.append(x)
        return sorted(out,key=lambda x:str(x.get("title") or x.get("name") or "").lower())
    def find(uid):
        app_item=next((x for x in scan_apps() if x["unified_id"].casefold()==str(uid).casefold()),None)
        if app_item:return app_item
        return next((x for x in library() if x["unified_id"]==uid),None)
    @app.get("/",response_class=HTMLResponse)
    def index():return (WEB_DIR/"index.html").read_text(encoding="utf-8")
    for route,file,ctype in [("/style.css","style.css","text/css"),("/theme.css","theme.css","text/css"),("/network.css","network.css","text/css"),("/app.js","app.js","application/javascript"),("/network.js","network.js","application/javascript")]:app.add_api_route(route,lambda f=file,c=ctype:FileResponse(WEB_DIR/f,media_type=c),methods=["GET"])
    @app.get("/api/apps/media/{app_id}/{kind}")
    def app_media(app_id:str,kind:str):
        if kind not in ("cover","capsule","hero","icon","logo"):return {"error":"not_found"}
        a=find_app(app_id)
        if not a:return {"error":"not_found"}
        value=a.get("cover") if kind in ("cover","capsule") else a.get(kind)
        if not value:return {"error":"not_found"}
        path=_media_file_path(value,[Path(a["install_directory"]),Path(a["install_directory"]).parent,BASE_DIR/"apps"])
        if path:return FileResponse(path,headers={"Cache-Control":"public, max-age=86400"})
        return {"error":"not_found"}
    @app.get("/api/playnite/media/{playnite_id}/{kind}")
    def p_media(playnite_id:str,kind:str):
        if kind not in ("cover","capsule","hero","logo"):return {"error":"not_found"}
        g=next((x for x in p_games() if x["playnite_id"]==playnite_id),None)
        if not g:return {"error":"not_found"}
        value=g.get("cover") if kind=="capsule" else g.get(kind)
        if not value:return {"error":"not_found"}
        value=str(value)
        if value.startswith(("http://","https://")):
            try:
                req=urllib.request.Request(value,headers={"User-Agent":"GameLibrary/1.0"})
                with urllib.request.urlopen(req,timeout=8) as r:data=r.read();ctype=r.headers.get_content_type() or "application/octet-stream"
                return Response(data,media_type=ctype,headers={"Cache-Control":"public, max-age=86400"})
            except Exception:return {"error":"media_unavailable"}
        bases=[playnite.library_path,playnite.library_path.parent if playnite.library_path else None,playnite.playnite_path.parent if playnite.playnite_path else None]
        path=_media_file_path(value,bases)
        if path:return FileResponse(path)
        return {"error":"not_found"}
    @app.get("/api/apps")
    def apps(q:str=Query("",max_length=200)):return library(q,False,"apps")
    @app.get("/api/games")
    def games(q:str=Query("",max_length=200),connected_only:bool=False,mode:str="playlist"):
        return library(q,connected_only,mode if mode in ("playlist","drives","apps") else "playlist")
    @app.get("/api/games/{game_id}")
    def game(game_id:str):return find(game_id) or {"error":"not_found"}
    @app.get("/api/games/{game_id}/details")
    def details(game_id:str):
        x=find(game_id)
        if not x:return {"error":"not_found"}
        if x.get("source")=="apps":x["description"]="Local GameDrive application";return x
        return x
    @app.post("/api/games/{game_id}/launch")
    def launch(game_id:str):
        x=find(game_id)
        if not x:return {"ok":False,"error":"not_found"}
        if x.get("source")=="apps":
            a=find_app(x.get("app_id"))
            if not a or not a.get("executable"):return {"ok":False,"error":"app_not_launchable"}
            try:
                subprocess.Popen([a["executable"]]+list(a.get("arguments") or []),cwd=a["working_directory"],creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0));return {"ok":True,"path":a["executable"]}
            except Exception as e:return {"ok":False,"error":"app_launch_failed","detail":str(e)}
        if x.get("playnite_id"):return playnite.launch(x["playnite_id"])
        return {"ok":False,"error":"not_launchable"}
    return app
