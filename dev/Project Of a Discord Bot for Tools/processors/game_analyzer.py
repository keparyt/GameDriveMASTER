import asyncio
import difflib
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from processors.game_db import find_game, normalize_name
from processors.thegamesdb import verify_game
from processors.instagram_scraper import is_instagram_url, scrape_post
from utils.helper import log

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-r1:7b")
MAX_TRANSCRIPT_CHARS = 12000
MAX_GAMES = 25
STEAM_MATCH_THRESHOLD = 0.88
KEPARDB_MATCH_THRESHOLD = 0.88
MAX_SOCIAL_MEDIA_ITEMS = 20
VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


def _run(command: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def _tool(name: str) -> str | None:
    return shutil.which(name)


def _clean_candidate_name(name: str) -> str:
    name = re.sub(r"^[\s*•\-–—\d.)]+", "", str(name)).strip()
    return re.sub(r"\s+", " ", name).strip(" \t\r\n-–—:;")


def _dedupe_candidates(candidates: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        name = _clean_candidate_name(candidate.get("name", ""))
        key = re.sub(r"[^a-z0-9]+", "", name.casefold())
        if not name or not key or key in seen:
            continue
        seen.add(key)
        item = dict(candidate)
        item["name"] = name
        result.append(item)
    return result[:MAX_GAMES]


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def _word_similarity(a: str, b: str) -> float:
    aw = normalize_name(a).split()
    bw = normalize_name(b).split()
    if not aw or not bw or abs(len(aw) - len(bw)) > 1:
        return 0.0
    used, scores = set(), []
    for word in aw:
        best, best_i = 0.0, None
        for i, other in enumerate(bw):
            if i in used:
                continue
            score = difflib.SequenceMatcher(None, word, other).ratio()
            if score > best:
                best, best_i = score, i
        if best_i is not None:
            used.add(best_i)
        scores.append(best)
    return sum(scores) / len(scores) if scores else 0.0


def _credible_name_match(query: str, title: str, threshold: float) -> bool:
    q, t = normalize_name(query), normalize_name(title)
    if not q or not t:
        return False
    if q == t:
        return True
    seq, word = _similarity(query, title), _word_similarity(query, title)
    if len(q.split()) >= 2 and len(t.split()) >= 2:
        return seq >= 0.82 and word >= 0.82
    if len(q.split()) == 1 and len(t.split()) == 1:
        return seq >= 0.93
    return seq >= threshold and word >= 0.82


def _correction_confidence(original: str, corrected: str) -> float:
    if normalize_name(original) == normalize_name(corrected):
        return 100.0
    return round((_similarity(original, corrected) * .6 + _word_similarity(original, corrected) * .4) * 100, 1)


async def _deepseek_correct_name(name: str) -> str:
    prompt = f"""Correct this video game title without removing meaningful subtitle words.
Preserve specific entries/subtitles. Do not replace a specific game with its base series.
If uncertain, return the input unchanged.
Return ONLY the corrected title.
INPUT: {name}"""
    payload = {"model": OLLAMA_MODEL, "stream": False, "messages": [
        {"role": "system", "content": "Correct video game names precisely."},
        {"role": "user", "content": prompt}], "options": {"temperature": 0}}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(OLLAMA_URL, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as response:
                response.raise_for_status()
                body = await response.json()
        corrected = str(body.get("message", {}).get("content", "")).strip()
        corrected = re.sub(r"```.*?```", "", corrected, flags=re.DOTALL).strip()
        corrected = corrected.splitlines()[-1].strip(" \"'`*•") if corrected else ""
        return corrected if corrected and len(corrected) <= 150 else name
    except Exception as exc:
        log(f"Game detector DeepSeek correction error | {name!r} | {type(exc).__name__}: {exc}")
        return name


def _is_instagram_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return is_instagram_url(url) or (host in {"instagram.com", "www.instagram.com", "m.instagram.com"} and re.search(r"/(p|reel|reels|tv)/", url, re.I) is not None)


def _dedupe_text_lines(lines: list[str]) -> list[str]:
    seen, result = set(), []
    for line in lines:
        key = re.sub(r"[^a-z0-9]+", "", line.casefold())
        if key and key not in seen:
            seen.add(key)
            result.append(line)
    return result[:MAX_GAMES]


async def _extract_direct_text(text: str) -> list[dict]:
    lines = [re.sub(r"^\s*(?:[*•\-–—]|\d+[.)])\s*", "", x).strip() for x in text.splitlines() if x.strip()]
    if len(lines) == 1:
        return [{"name": lines[0], "confidence": 100, "reason": "explicitly present in direct text", "evidence_type": "direct_text"}]
    return [{"name": x, "confidence": 100, "reason": "explicitly present in direct text", "evidence_type": "direct_text"} for x in _dedupe_text_lines(lines)]


async def _download_url(url: str, workdir: Path) -> tuple[dict, list[Path]]:
    """Download media. Instagram is deliberately NOT sent through yt-dlp."""
    if _is_instagram_url(url):
        try:
            return await scrape_post(url, workdir)
        except Exception as exc:
            log(f"Game detector Instagram scraper error | {url} | {type(exc).__name__}: {exc}")
            raise RuntimeError(f"Instagram extraction failed: {exc}") from exc

    if not _tool("yt-dlp"):
        raise RuntimeError("yt-dlp is not installed")
    output = str(workdir / "source.%(ext)s")
    metadata_cmd = ["yt-dlp", "--no-warnings", "--ignore-errors", "--dump-json", "--no-playlist", url]
    meta_proc = await asyncio.to_thread(_run, metadata_cmd, 180)
    entries = []
    for line in meta_proc.stdout.splitlines():
        try:
            value = __import__("json").loads(line)
            if isinstance(value, dict):
                entries.append(value)
        except Exception:
            pass
    root = entries[0] if entries else {}
    metadata = {"title": root.get("title"), "description": root.get("description"), "uploader": root.get("uploader") or root.get("channel"), "entries": entries}
    dl_proc = await asyncio.to_thread(_run, ["yt-dlp", "--no-warnings", "--ignore-errors", "--restrict-filenames", "--no-playlist", "-o", output, url], 360)
    media = sorted([p for p in workdir.glob("source.*") if p.is_file() and p.suffix.lower() not in {".json", ".part", ".ytdl"}], key=lambda p: p.name)
    if not entries and not media:
        error = (dl_proc.stderr or meta_proc.stderr or "unknown yt-dlp error").strip()
        raise RuntimeError(error[-2000:])
    return metadata, media[:MAX_SOCIAL_MEDIA_ITEMS]


async def _download_attachment(url: str, target: Path) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=180)) as response:
            response.raise_for_status()
            with target.open("wb") as file:
                while True:
                    chunk = await response.content.read(1024 * 1024)
                    if not chunk:
                        break
                    file.write(chunk)


async def _transcribe(video: Path, workdir: Path, index: int = 1) -> str:
    if not _tool("ffmpeg"):
        return ""
    audio = workdir / f"audio_{index}.wav"
    proc = await asyncio.to_thread(_run, ["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)], 120)
    if proc.returncode != 0 or not audio.exists():
        return ""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return ""
    model_name = os.getenv("WHISPER_MODEL", "small")
    device = os.getenv("WHISPER_DEVICE", "auto")
    compute = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    def run_transcribe():
        actual = "cuda" if device == "auto" else device
        model = WhisperModel(model_name, device=actual, compute_type=compute)
        segments, _ = model.transcribe(str(audio), vad_filter=True)
        return " ".join(s.text.strip() for s in segments if s.text.strip())
    try:
        return await asyncio.to_thread(run_transcribe)
    except Exception as exc:
        log(f"Game detector transcription error | {type(exc).__name__}: {exc}")
        return ""


async def _ocr_image(image: Path) -> str:
    """OCR an image without requiring the external Tesseract executable.

    RapidOCR is preferred because pytesseract is only a Python wrapper around
    the separately installed tesseract.exe. Tesseract remains an optional
    fallback for environments that already have it installed.
    """
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps

        def prepare_image():
            with Image.open(image) as source:
                source.verify()
            img = Image.open(image).convert("RGB")
            scale = 2 if max(img.size) < 1800 else 1
            if scale > 1:
                img = img.resize((img.width * scale, img.height * scale))
            gray = ImageOps.grayscale(img)
            return ImageEnhance.Contrast(gray).enhance(1.7).filter(ImageFilter.SHARPEN)

        prepared = await asyncio.to_thread(prepare_image)

        # Preferred OCR engine: no system executable required.
        try:
            from rapidocr_onnxruntime import RapidOCR
            engine = RapidOCR()
            result, _ = await asyncio.to_thread(engine, prepared)
            if result:
                texts = []
                for item in result:
                    try:
                        text = str(item[1]).strip()
                    except (IndexError, TypeError):
                        continue
                    if text:
                        texts.append(text)
                if texts:
                    return "\n".join(dict.fromkeys(texts))
        except Exception as rapid_exc:
            log(f"Game detector RapidOCR fallback | {image.name} | {type(rapid_exc).__name__}: {rapid_exc}")

        # Optional legacy fallback.
        try:
            import pytesseract
            return "\n".join(
                pytesseract.image_to_string(prepared, config=f"--psm {psm}")
                for psm in (6, 11)
            )
        except Exception as tess_exc:
            if type(tess_exc).__name__ != "TesseractNotFoundError":
                log(f"Game detector Tesseract fallback | {image.name} | {type(tess_exc).__name__}: {tess_exc}")
            return ""
    except Exception as exc:
        log(f"Game detector OCR error | {image.name} | {type(exc).__name__}: {exc}")
        return ""


async def _verify_and_enrich(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    verified, unresolved = [], []
    for candidate in _dedupe_candidates(candidates):
        original = str(candidate.get("name", "")).strip()
        if not original:
            continue
        platform = await verify_game(original)
        if platform is None:
            unresolved.append({"name": candidate.get("detected_name", original), "detected_name": candidate.get("detected_name", original), "confidence": float(candidate.get("confidence", 0)), "reason": "TheGamesDB could not verify a console release for this title.", "requires_store_link": True})
            continue
        steam = await _find_steam_match(original)
        if steam:
            item = dict(candidate)
            item.update(steam)
            item["detected_name"] = candidate.get("detected_name", original)
            item["verified"] = True
            item["verification_source"] = "steam"
            item["library_url"] = item["steam_url"]
            item["library_source"] = "steam"
            item["tgdb_game_id"] = platform.game_id
            item["tgdb_url"] = platform.url
            item["selected_platform"] = platform.selected_platform_name
            item["console_platforms"] = platform.console_names
            item["console_names"] = platform.console_names
            item["pc_available"] = bool(platform.pc_platform)
            item["has_console"] = True
            verified.append(item)
            continue
        db = await find_game(original)
        if db and _credible_name_match(original, db.name, KEPARDB_MATCH_THRESHOLD):
            item = dict(candidate)
            item.update({"name": db.name, "detected_name": candidate.get("detected_name", original), "verified": True, "verification_source": db.source, "library_url": db.url, "library_source": db.source, "tgdb_game_id": db.tgdb_game_id, "tgdb_url": f"https://thegamesdb.net/game.php?id={db.tgdb_game_id}" if db.tgdb_game_id else None, "selected_platform": db.selected_platform, "console_platforms": list(db.console_platforms), "console_names": list(db.console_platforms), "pc_available": db.pc_available, "has_console": bool(db.console_platforms)})
            verified.append(item)
            continue
        item = dict(candidate)
        item.update({"detected_name": candidate.get("detected_name", original), "name": getattr(platform, "game_title", None) or getattr(platform, "name", None) or original, "verified": True, "verification_source": "thegamesdb", "library_url": platform.url, "library_source": "thegamesdb", "tgdb_game_id": platform.game_id, "tgdb_url": platform.url, "selected_platform": platform.selected_platform_name, "console_platforms": platform.console_names, "console_names": platform.console_names, "pc_available": bool(platform.pc_platform), "has_console": True, "steam_verified": False})
        verified.append(item)
    return verified[:MAX_GAMES], unresolved


async def _find_steam_match(query: str) -> dict | None:
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "KeparGameDetector/1.0"}) as session:
            async with session.get("https://store.steampowered.com/search/", params={"term": query, "cc": "ca", "l": "english"}, timeout=15) as response:
                if response.status != 200:
                    return None
                html = await response.text()
        soup = BeautifulSoup(html, "html.parser")
        ranked = []
        for row in soup.select("a.search_result_row[data-ds-appid]")[:20]:
            node, appid = row.select_one(".title"), row.get("data-ds-appid")
            if not node or not appid or not str(appid).isdigit():
                continue
            title = " ".join(node.stripped_strings).strip()
            seq, word = _similarity(query, title), _word_similarity(query, title)
            ranked.append((seq * .45 + word * .55, title, int(appid)))
        if not ranked:
            return None
        score, title, appid = max(ranked)
        if not _credible_name_match(query, title, STEAM_MATCH_THRESHOLD):
            return None
        return {"name": title, "steam_appid": appid, "steam_url": f"https://store.steampowered.com/app/{appid}/", "steam_verified": True, "confidence": score * 100}
    except Exception as exc:
        log(f"Steam verification error | {query} | {type(exc).__name__}: {exc}")
        return None


def _result(games: list[dict], unresolved: list[dict] | None = None) -> dict:
    unresolved = unresolved or []
    if not games:
        message = "No identified game could be verified."
        if unresolved:
            message += " Please provide a direct game/store page link for each unresolved game."
        return {"status": "needs_store_link" if unresolved else "unknown", "message": message, "game_count": 0, "games": [], "unresolved_games": unresolved, "requires_store_link": bool(unresolved)}
    first = games[0]
    return {"status": "identified" if not unresolved else "partial", "game_count": len(games), "games": games, "unresolved_games": unresolved, "requires_store_link": bool(unresolved), "game_name": first.get("name"), "confidence": float(first.get("confidence", 0)), "steam_url": first.get("steam_url"), "reason": first.get("reason", "Identification from supplied content."), "candidates": games, "message": ""}


async def analyze_game_input(data: dict) -> dict:
    from processors.game_media_analyzer import analyze_game_input as evidence_analyzer
    return await evidence_analyzer(data)
