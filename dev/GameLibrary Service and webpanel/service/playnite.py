import ctypes
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

log=logging.getLogger("gamelibrary.playnite")

class PlayniteBridge:
    def __init__(self,config=None):
        self.config=config or {};self.enabled=bool(self.config.get("enabled",True));self.refresh_on_game_drive_change=bool(self.config.get("refreshOnGameDriveChange",True));self.playnite_path=self._path(self.config.get("path") or self.config.get("playnitePath")) or self._find_playnite();self.library_path=self._path(self.config.get("libraryPath") or self.config.get("library_path") or self.config.get("databasePath")) or self._find_library();self._lock=threading.RLock();self._games=[];self._signature=None;self._checked_at=0.0;self._last_refresh=0.0;self._last_error=None
    def _path(self,value):
        if not value:return None
        p=Path(os.path.expandvars(os.path.expanduser(str(value))))
        return (Path(__file__).resolve().parent.parent/p).resolve() if not p.is_absolute() else p
    def _find_playnite(self):
        for root in (os.environ.get("PROGRAMFILES","C:/Program Files"),os.environ.get("LOCALAPPDATA","")):
            p=Path(root)/"Playnite"/"Playnite.DesktopApp.exe"
            if p.is_file():return p
        return None
    def _find_library(self):
        appdata=os.environ.get("APPDATA")
        if appdata:
            p=Path(appdata)/"Playnite"/"library"
            if p.is_dir():return p
        if self.playnite_path:
            p=self.playnite_path.parent/"library"
            if p.is_dir():return p
        return None
    @property
    def available(self):return bool(self.enabled and self.library_path and (self.library_path/"games").is_dir())
    @property
    def status(self):return {"enabled":self.enabled,"available":self.available,"playnite_path":str(self.playnite_path) if self.playnite_path else None,"library_path":str(self.library_path) if self.library_path else None,"last_refresh":self._last_refresh,"error":self._last_error,"game_count":len(self._games)}
    def _signature_now(self):
        if not self.available:return None
        now=time.monotonic()
        if now-self._checked_at<2:return self._signature
        self._checked_at=now
        try:return hash(tuple(sorted((p.name,p.stat().st_mtime_ns,p.stat().st_size) for p in (self.library_path/"games").glob("*.json"))))
        except OSError as e:self._last_error=str(e);return self._signature
    def needs_refresh(self):return self._signature_now()!=self._signature
    def _media(self,gid,value):
        if not value:return None
        s=str(value)
        if s.startswith(("http://","https://")):return s
        p=Path(s)
        if p.is_absolute() and p.is_file():return str(p)
        p=self.library_path/"files"/str(gid)/s
        return str(p) if p.is_file() else None
    def _action(self,g):
        actions=g.get("GameActions") or g.get("GameAction") or g.get("PlayAction") or []
        if isinstance(actions,dict):actions=list(actions.values())
        for a in actions:
            if isinstance(a,dict) and a.get("IsPlayAction"):return a
        return actions[0] if actions and isinstance(actions[0],dict) else {}
    def _read(self,p):
        try:return json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError,ValueError):return None
    def _parse(self,p):
        g=self._read(p)
        if not isinstance(g,dict) or not bool(g.get("IsInstalled",g.get("isInstalled",False))):return None
        gid=str(g.get("Id") or g.get("id") or p.stem);a=self._action(g)
        return {"playnite_id":gid,"name":g.get("Name") or g.get("name") or "Unknown Game","game_id":g.get("GameId") or g.get("gameId"),"source_id":g.get("SourceId") or g.get("sourceId"),"install_directory":str(g.get("InstallDirectory") or g.get("installDirectory") or ""),"executable":str(a.get("Path") or a.get("path") or ""),"arguments":str(a.get("Arguments") or a.get("arguments") or ""),"working_directory":str(a.get("WorkingDir") or a.get("WorkingDirectory") or ""),"cover":self._media(gid,g.get("CoverImage") or g.get("coverImage")),"hero":self._media(gid,g.get("BackgroundImage") or g.get("backgroundImage")),"logo":self._media(gid,g.get("Icon") or g.get("icon")),"description":g.get("Description") or g.get("description"),"release_date":g.get("ReleaseDate") or g.get("releaseDate"),"playtime":g.get("Playtime") or g.get("playtime") or 0}
    def read_games(self,force=False):
        with self._lock:
            if not self.available:self._games=[];self._signature=None;return []
            sig=self._signature_now()
            if not force and sig==self._signature:return list(self._games)
            out=[]
            for p in sorted((self.library_path/"games").glob("*.json")):
                g=self._parse(p)
                if g:out.append(g)
            self._games=out;self._signature=sig;self._last_refresh=time.time();self._last_error=None;log.info("Playnite library read: %d installed game(s)",len(out));return list(out)
    def _window(self):
        if not hasattr(ctypes,"windll"):return None
        user32=ctypes.windll.user32;found={"h":None};cb=ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)
        @cb
        def enum(hwnd,_):
            if not user32.IsWindowVisible(hwnd):return True
            pid=ctypes.c_ulong();user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
            try:
                r=subprocess.run(["tasklist","/FI",f"PID eq {pid.value}","/FO","CSV","/NH"],capture_output=True,text=True,timeout=2,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                if "Playnite.DesktopApp.exe" in r.stdout:found["h"]=hwnd;return False
            except Exception:pass
            return True
        user32.EnumWindows(enum,0);return found["h"]
    def refresh(self,force=False):
        if not self.enabled or (not force and not self.needs_refresh()):return False
        try:
            hwnd=self._window()
            if hwnd:
                u=ctypes.windll.user32;u.PostMessageW(hwnd,0x0100,0x74,0);u.PostMessageW(hwnd,0x0101,0x74,0);time.sleep(.35)
            elif self.playnite_path and self.playnite_path.is_file():
                subprocess.Popen([str(self.playnite_path)],cwd=str(self.playnite_path.parent),creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0));time.sleep(.75)
            self.read_games(force=True);return True
        except Exception as e:
            self._last_error=str(e);log.warning("Playnite refresh failed: %s",e);self.read_games(force=True);return False
    def launch(self,playnite_id):
        if not self.enabled or not self.playnite_path or not self.playnite_path.is_file():return {"ok":False,"error":"playnite_unavailable"}
        try:
            subprocess.Popen([str(self.playnite_path),"--start",str(playnite_id)],cwd=str(self.playnite_path.parent),creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0));return {"ok":True,"playnite_id":str(playnite_id)}
        except Exception as e:return {"ok":False,"error":"playnite_launch_failed","detail":str(e)}
    def uri(self,playnite_id):return "playnite://playnite/start/"+quote(str(playnite_id),safe="")
