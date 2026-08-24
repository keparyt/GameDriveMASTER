import asyncio
import difflib
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

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


def _steam_candidates_from_text(text: str) -> list[dict]:
    """Treat a Steam app URL as authoritative title evidence, never as a generic URL."""
    results = []
    pattern = re.compile(r"https?://(?:store\.)?steampowered\.com/app/(\d+)(?:/([^/?#]+))?/?[^\s]*", re.I)
    for match in pattern.finditer(text):
        appid, slug = match.group(1), match.group(2)
        title = unquote(slug or "").replace("_", " ").strip()
        if not title:
            continue
        # Steam slugs often omit punctuation; keep the human text only if it
        # clearly contains the same title, otherwise the URL slug wins.
        before = text[:match.start()].strip()
        human = before.splitlines()[-1].strip() if before else ""
        human = re.sub(r"^[\s*•\-–—\d.)]+", "", human)
        human = re.sub(r"\s*:\s*", ": ", human).strip(" -–—")
        if human and _similarity(human.replace(": ", " "), title) >= 0.70:
            title = human
        results.append({
            "name": title,
            "confidence": 100,
            "reason": f"explicit Steam store URL (appid {appid})",
            "evidence_type": "steam_url",
            "steam_appid": appid,
            "steam_url": match.group(0),
        })
    return results


async def _extract_direct_text(text: str) -> list[dict]:
    steam = _steam_candidates_from_text(text)
    if steam:
        return steam[:MAX_GAMES]
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
        try:
            from rapidocr_onnxruntime import RapidOCR
            engine = RapidOCR()
            result, _ = await asyncio.to_thread(engine, prepared)
            if result:
                texts = []
                for item in result:
                    try:
                        value = str(item[1]).strip()
                    except (IndexError, TypeError):
                        continue
                    if value:
                        texts.append(value)
                if texts:
                    return "\n".join(dict.fromkeys(texts))
        except Exception as rapid_exc:
            log(f"Game detector RapidOCR fallback | {image.name} | {type(rapid_exc).__name__}: {rapid_exc}")
        try:
            import pytesseract
            return await asyncio.to_thread(pytesseract.image_to_string, prepared)
        except Exception:
            return ""
    except Exception as exc:
        log(f"Game detector OCR error | {image.name} | {type(exc).__name__}: {exc}")
        return ""
