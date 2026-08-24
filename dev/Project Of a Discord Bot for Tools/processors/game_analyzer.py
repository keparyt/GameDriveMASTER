import asyncio, difflib, json, os, re, shutil, subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse
import aiohttp
from bs4 import BeautifulSoup
from processors.game_db import find_game, find_local_game, normalize_name
from processors.thegamesdb import verify_game
from processors.instagram_scraper import is_instagram_url, scrape_post
from utils.helper import log

OLLAMA_URL=os.getenv('OLLAMA_URL','http://127.0.0.1:11434/api/chat'); OLLAMA_MODEL=os.getenv('OLLAMA_MODEL','deepseek-r1:7b')
MAX_TRANSCRIPT_CHARS=12000; MAX_GAMES=25; STEAM_MATCH_THRESHOLD=.88; KEPARDB_MATCH_THRESHOLD=.88; MAX_SOCIAL_MEDIA_ITEMS=20
VIDEO_EXTENSIONS=('.mp4','.mov','.mkv','.webm','.avi','.m4v'); IMAGE_EXTENSIONS=('.jpg','.jpeg','.png','.webp','.gif','.bmp')
def _run(c,timeout=180): return subprocess.run(c,capture_output=True,text=True,timeout=timeout)
def _tool(n): return shutil.which(n)
def _clean_candidate_name(n): return re.sub(r'\s+',' ',re.sub(r'^[\s*•\-–—\d.)]+','',str(n)).strip()).strip(' \t\r\n-–—:;')
def _dedupe_candidates(cs):
 seen=set(); out=[]
 for c in cs:
  if not isinstance(c,dict): continue
  n=_clean_candidate_name(c.get('name','')); k=re.sub(r'[^a-z0-9]+','',n.casefold())
  if n and k and k not in seen: seen.add(k); x=dict(c); x['name']=n; out.append(x)
 return out[:MAX_GAMES]
def _similarity(a,b): return difflib.SequenceMatcher(None,normalize_name(a),normalize_name(b)).ratio()
def _word_similarity(a,b):
 aw,bw=normalize_name(a).split(),normalize_name(b).split()
 if not aw or not bw or abs(len(aw)-len(bw))>1:return 0.0
 return sum(max((difflib.SequenceMatcher(None,w,x).ratio() for x in bw),default=0) for w in aw)/len(aw)
def _credible_name_match(q,t,threshold):
 qn,tn=normalize_name(q),normalize_name(t)
 if not qn or not tn:return False
 if qn==tn:return True
 s,w=_similarity(q,t),_word_similarity(q,t)
 return (s>=.93 if len(qn.split())==len(tn.split())==1 else s>=.82 and w>=.82) if len(qn.split())>=2 and len(tn.split())>=2 else s>=threshold and w>=.82
async def _deepseek_correct_name(name): return name

def _is_instagram_url(url):
 h=(urlparse(url).hostname or '').lower(); return is_instagram_url(url) or (h in {'instagram.com','www.instagram.com','m.instagram.com'} and re.search(r'/(p|reel|reels|tv)/',url,re.I) is not None)
def _steam_candidates_from_text(text):
 out=[]; pat=re.compile(r'https?://(?:store\.)?steampowered\.com/app/(\d+)(?:/([^/?#]+))?/?[^\s]*',re.I)
 for m in pat.finditer(text):
  appid,slug=m.group(1),m.group(2); title=unquote(slug or '').replace('_',' ').strip()
  if title: out.append({'name':title,'confidence':100,'reason':f'explicit Steam store URL (appid {appid})','evidence_type':'steam_url','steam_appid':appid,'steam_url':m.group(0)})
 return out
async def _extract_direct_text(text):
 steam=_steam_candidates_from_text(text)
 if steam:return steam
 lines=[re.sub(r'^\s*(?:[*•\-–—]|\d+[.)])\s*','',x).strip() for x in text.splitlines() if x.strip()]
 return [{'name':x,'confidence':100,'reason':'explicitly present in direct text','evidence_type':'direct_text'} for x in lines[:MAX_GAMES]]

async def _download_url(url,workdir):
 if _is_instagram_url(url): return await scrape_post(url,workdir)
 if not _tool('yt-dlp'): raise RuntimeError('yt-dlp is not installed')
 output=str(workdir/'source.%(ext)s'); meta=await asyncio.to_thread(_run,['yt-dlp','--no-warnings','--ignore-errors','--dump-json','--no-playlist',url],180)
 entries=[]
 for line in meta.stdout.splitlines():
  try:
   x=json.loads(line)
   if isinstance(x,dict):entries.append(x)
  except Exception:pass
 root=entries[0] if entries else {}; dl=await asyncio.to_thread(_run,['yt-dlp','--no-warnings','--ignore-errors','--restrict-filenames','--no-playlist','-o',output,url],360)
 media=[p for p in workdir.glob('source.*') if p.is_file() and p.suffix.lower() not in {'.json','.part','.ytdl'}]
 if not entries and not media: raise RuntimeError((dl.stderr or meta.stderr or 'unknown yt-dlp error')[-2000:])
 return {'title':root.get('title'),'description':root.get('description'),'uploader':root.get('uploader') or root.get('channel'),'entries':entries},sorted(media)[:MAX_SOCIAL_MEDIA_ITEMS]
async def _download_attachment(url,target):
 async with aiohttp.ClientSession() as s:
  async with s.get(url,timeout=aiohttp.ClientTimeout(total=180)) as r:
   r.raise_for_status(); target.write_bytes(await r.read())
async def _transcribe(video,workdir,index=1): return ''

async def _steam_prefix_completion(query):
    """Return a Steam title only when the supplied text is a genuine title prefix.

    A prefix match is intentionally much stricter than fuzzy search. This prevents
    unrelated games such as 'Spheroll' from ever becoming a suggestion for 'Hela'.
    """
    q = normalize_name(query).strip()
    if not q or len(q) < 3:
        return None
    try:
        async with aiohttp.ClientSession(headers={'User-Agent':'KeparGameDetector/1.0'}) as s:
            async with s.get('https://store.steampowered.com/search/', params={'term':query,'cc':'ca','l':'english'}, timeout=15) as r:
                if r.status != 200:
                    return None
                html = await r.text()
        ranked=[]
        for row in BeautifulSoup(html,'html.parser').select('a.search_result_row[data-ds-appid]')[:30]:
            n=row.select_one('.title'); aid=row.get('data-ds-appid')
            if not n or not aid or not str(aid).isdigit():
                continue
            title=n.get_text(' ',strip=True)
            tn=normalize_name(title)
            if tn == q or tn.startswith(q + ' '):
                # Prefer the shortest genuine continuation; do not rewrite the user's prefix.
                ranked.append((len(tn), title, int(aid)))
        if not ranked:
            return None
        _, title, appid=min(ranked)
        return {'name':title,'steam_appid':appid,'steam_url':f'https://store.steampowered.com/app/{appid}/','confidence':100,'reason':f'title completion from prefix {query!r}','evidence_type':'title_completion','steam_verified':True}
    except Exception as exc:
        log(f'Steam prefix completion error | {query} | {type(exc).__name__}: {exc}')
        return None

async def _complete_candidate_name(candidate):
    """Complete a partial title when a trusted database has an actual prefix match."""
    name = _clean_candidate_name(candidate.get('name',''))
    if not name:
        return candidate

    # Local SDB is preferred because it is the canonical library database.
    try:
        local = await find_local_game(name, min_fuzzy=1.0)
        if local and normalize_name(local.name).startswith(normalize_name(name) + ' '):
            x=dict(candidate); x['name']=local.name; x['completion_source']='kepardb'; x['completion_url']=local.url
            log(f'Game title completion | {name!r} -> {local.name!r} | source=KeparDB')
            return x
    except Exception as exc:
        log(f'Game title completion KeparDB error | {name} | {type(exc).__name__}: {exc}')

    steam = await _steam_prefix_completion(name)
    if steam:
        x=dict(candidate); x.update(steam)
        log(f'Game title completion | {name!r} -> {steam["name"]!r} | source=Steam')
        return x
    return candidate

async def _verify_and_enrich(candidates):
 verified=[]; unresolved=[]
 for original in _dedupe_candidates(candidates):
  # Never allow a fuzzy verifier to replace a short/partial title with an unrelated game.
  c=await _complete_candidate_name(original)
  name=c['name']; steam_url=c.get('steam_url')
  if steam_url:
   x=dict(c); x.update({'verified':True,'verification_source':'steam_url','library_url':steam_url,'library_source':'steam','pc_available':True,'has_console':False,'console_platforms':[],'console_names':[]}); verified.append(x); continue
  platform=await verify_game(name)
  steam=await _find_steam_match(name)
  if steam:
   x=dict(c); x.update(steam); x.update({'verified':True,'verification_source':'steam','library_url':x['steam_url'],'library_source':'steam','pc_available':True,'has_console':False,'console_platforms':[],'console_names':[]}); verified.append(x); continue
  if platform:
   x=dict(c); x.update({'name':getattr(platform,'game_title',None) or name,'verified':True,'verification_source':'thegamesdb','library_url':platform.url,'library_source':'thegamesdb','pc_available':bool(platform.pc_platform),'console_platforms':platform.console_names,'console_names':platform.console_names,'has_console':bool(platform.console_names)}); verified.append(x); continue
  db=await find_game(name)
  if db and _credible_name_match(name,db.name,KEPARDB_MATCH_THRESHOLD):
   x=dict(c); x.update({'name':db.name,'verified':True,'verification_source':db.source,'library_url':db.url,'library_source':db.source,'pc_available':db.pc_available,'console_platforms':list(db.console_platforms),'console_names':list(db.console_platforms),'has_console':bool(db.console_platforms)}); verified.append(x); continue
  unresolved.append({'name':name,'detected_name':name,'confidence':float(c.get('confidence',0)),'reason':'Could not verify title.','requires_store_link':True})
 return verified[:MAX_GAMES],unresolved
async def _find_steam_match(query):
 try:
  async with aiohttp.ClientSession(headers={'User-Agent':'KeparGameDetector/1.0'}) as s:
   async with s.get('https://store.steampowered.com/search/',params={'term':query,'cc':'ca','l':'english'},timeout=15) as r:
    if r.status!=200:return None
    html=await r.text()
  ranked=[]
  for row in BeautifulSoup(html,'html.parser').select('a.search_result_row[data-ds-appid]')[:20]:
   n=row.select_one('.title'); aid=row.get('data-ds-appid')
   if n and aid and str(aid).isdigit(): ranked.append((_similarity(query,n.get_text(strip=True)),n.get_text(strip=True),int(aid)))
  if not ranked:return None
  score,title,appid=max(ranked)
  # Fuzzy Steam results are accepted only with a strong title match. A result
  # that merely happens to be the search engine's top result must never leak out.
  if not _credible_name_match(query,title,STEAM_MATCH_THRESHOLD):
   log(f'Steam fallback | rejected unrelated result | query={query!r} | best={title!r} | score={score:.3f}')
   return None
  return {'name':title,'steam_appid':appid,'steam_url':f'https://store.steampowered.com/app/{appid}/','steam_verified':True,'confidence':score*100}
 except Exception as e: log(f'Steam verification error | {query} | {type(e).__name__}: {e}'); return None
def _result(games,unresolved=None):
 unresolved=unresolved or []
 if not games:return {'status':'needs_store_link' if unresolved else 'unknown','message':'No identified game could be verified.','game_count':0,'games':[],'unresolved_games':unresolved,'requires_store_link':bool(unresolved)}
 f=games[0]; return {'status':'identified' if not unresolved else 'partial','game_count':len(games),'games':games,'unresolved_games':unresolved,'requires_store_link':bool(unresolved),'game_name':f.get('name'),'confidence':float(f.get('confidence',0)),'steam_url':f.get('steam_url'),'reason':f.get('reason','Identification from supplied content.'),'candidates':games,'message':''}
async def analyze_game_input(data):
 from processors.game_media_analyzer import analyze_game_input as evidence_analyzer
 return await evidence_analyzer(data)
