import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import aiohttp

from processors.game_db import enrich_games
from utils.helper import log

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-r1:7b")
MAX_TRANSCRIPT_CHARS = 12000
MAX_GAMES = 50


def _run(command: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def _tool(name: str) -> str | None:
    return shutil.which(name)


def _clean_candidate_name(name: str) -> str:
    name = re.sub(r"^[\s*•\-–—\d.)]+", "", str(name)).strip()
    name = re.sub(r"\s+", " ", name)
    return name.strip(" \t\r\n-–—:;")


def _dedupe_candidates(candidates: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
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


async def analyze_game_input(data: dict) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="game-detector-"))
    try:
        text = data.get("text", "").strip()
        source_types = set(data.get("source_types", []))
        has_media = bool(data.get("urls") or data.get("video_attachments") or data.get("image_attachments"))

        # Direct text is deliberately handled by a strict extraction path.
        # This prevents DeepSeek from inventing sequels, series entries, or
        # unrelated games when the user gives an explicit list.
        if text and not has_media and (not source_types or source_types == {"direct_text"}):
            candidates = await _extract_direct_text(text)
            if not candidates:
                return {"status": "unknown", "message": "No explicit game names could be extracted from the text."}
            games = await _verify_and_enrich(candidates)
            return _result(games)

        text_parts = []
        if text:
            text_parts.append(f"Discord text:\n{text}")

        media_files: list[Path] = []
        urls = data.get("urls", [])

        for url in urls[:3]:
            try:
                metadata, media = await _download_url(url, workdir)
                if metadata.get("title"):
                    text_parts.append(f"Video title:\n{metadata['title']}")
                if metadata.get("description"):
                    text_parts.append(f"Video description:\n{metadata['description'][:8000]}")
                if media:
                    media_files.append(media)
            except Exception as exc:
                log(f"Game detector URL error | {url} | {type(exc).__name__}: {exc}")

        for item in data.get("video_attachments", []):
            target = workdir / item["filename"]
            await _download_attachment(item["url"], target)
            media_files.append(target)

        for item in data.get("image_attachments", []):
            target = workdir / item["filename"]
            await _download_attachment(item["url"], target)
            ocr = await _ocr_image(target)
            if ocr:
                text_parts.append(f"Screenshot OCR:\n{ocr[:6000]}")

        if media_files:
            transcript = await _transcribe(media_files[0], workdir)
            if transcript:
                text_parts.append(f"Audio transcript:\n{transcript[:MAX_TRANSCRIPT_CHARS]}")

        evidence = "\n\n---\n\n".join(text_parts).strip()
        if not evidence:
            return {"status": "unknown", "message": "No readable text or media was found."}

        ai = await _ask_ollama(evidence)
        candidates = _dedupe_candidates(ai.get("candidates", []) if isinstance(ai, dict) else [])
        if not candidates:
            return {"status": "unknown", "message": "I couldn't identify a game from the available evidence."}

        games = await _verify_and_enrich(candidates)
        return _result(games)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _result(games: list[dict]) -> dict:
    if not games:
        return {"status": "unknown", "message": "No games could be verified from the supplied content."}
    first = games[0]
    return {
        "status": "identified",
        "game_count": len(games),
        "games": games,
        "game_name": first.get("name"),
        "confidence": float(first.get("confidence", 0)),
        "steam_url": first.get("steam_url"),
        "reason": first.get("reason", "Identification from supplied content."),
        "candidates": games,
    }


async def _extract_direct_text(text: str) -> list[dict]:
    """Extract only titles explicitly present in direct text.

    We use the model only as a structured parser here. The prompt explicitly
    forbids inference and then the returned names are checked against the
    literal input before they can reach the database matcher.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    obvious = []
    for line in lines:
        cleaned = re.sub(r"^\s*(?:[*•\-–—]|\d+[.)])\s*", "", line).strip()
        if cleaned:
            obvious.append(cleaned)

    prompt = f"""Extract game titles that are EXPLICITLY WRITTEN in the following text.
This is a literal list/extraction task, NOT a guessing task.

STRICT RULES:
- Return only titles whose words appear in the input.
- Never infer a sequel, remake, edition, series entry, or related game.
- Never replace a title with another title.
- Never invent a title.
- Do not turn a series/category such as 'LEGO Games' into individual LEGO games.
- Preserve the user's title wording as closely as possible.
- If the input says 'NBA 2K', return 'NBA 2K', not 'NBA 2K27'.
- If the input says 'Overcooked 2', return 'Overcooked 2'.
- If the input says 'Heave Ho', do not return 'Heave Ho 2'.
- Return every distinct explicit game/category name, not just 10.

Return ONLY JSON:
{{"candidates":[{{"name":"exact title from input","confidence":100,"reason":"explicitly present","evidence_type":"direct_text"}}]}}

INPUT:
{text[:30000]}"""

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": "You are a literal text extraction engine. Never infer or hallucinate game titles."},
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": 0},
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(OLLAMA_URL, json=payload, timeout=aiohttp.ClientTimeout(total=180)) as response:
                response.raise_for_status()
                body = await response.json()
        content = body.get("message", {}).get("content", "")
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError("Ollama did not return JSON")
        parsed = json.loads(match.group(0))
        candidates = _dedupe_candidates(parsed.get("candidates", []))

        # Hard safety filter: a returned title must have meaningful textual
        # overlap with an explicit line from the user's input. This blocks
        # hallucinated sequels such as 'Heave Ho 2'.
        source_lines = [re.sub(r"[^a-z0-9]+", " ", x.casefold()).strip() for x in obvious]
        safe = []
        for candidate in candidates:
            key = re.sub(r"[^a-z0-9]+", " ", candidate["name"].casefold()).strip()
            if any(key == line or key in line for line in source_lines):
                safe.append(candidate)
        if safe:
            return safe
    except Exception as exc:
        log(f"Game detector direct-text AI extraction error | {type(exc).__name__}: {exc}")

    # Deterministic fallback: every non-empty bullet/list line is preserved.
    return [
        {"name": item, "confidence": 100, "reason": "explicitly present in direct text", "evidence_type": "direct_text"}
        for item in _dedupe_text_lines(obvious)
    ]


def _dedupe_text_lines(lines: list[str]) -> list[str]:
    seen = set()
    result = []
    for line in lines:
        key = re.sub(r"[^a-z0-9]+", "", line.casefold())
        if key and key not in seen:
            seen.add(key)
            result.append(line)
    return result[:MAX_GAMES]


async def _verify_and_enrich(candidates: list[dict]) -> list[dict]:
    candidates = _dedupe_candidates(candidates)
    verified = await _verify_steam(candidates)
    return await enrich_games(verified[:MAX_GAMES])


async def _download_url(url: str, workdir: Path) -> tuple[dict, Path | None]:
    if not _tool("yt-dlp"):
        raise RuntimeError("yt-dlp is not installed")
    output = str(workdir / "source.%(ext)s")
    command = ["yt-dlp", "--no-playlist", "--no-warnings", "--dump-single-json", "--write-info-json", "-o", output, url]
    proc = await asyncio.to_thread(_run, command, 120)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])
    try:
        metadata = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        metadata = {}
    command = ["yt-dlp", "--no-playlist", "--no-warnings", "-f", "bv*[ext=mp4]+ba/b[ext=mp4]/b", "-o", output, url]
    proc = await asyncio.to_thread(_run, command, 180)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])
    files = sorted(workdir.glob("source.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    media = next((p for p in files if p.suffix.lower() not in {".json", ".jpg", ".webp"}), None)
    return metadata, media


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


async def _transcribe(video: Path, workdir: Path) -> str:
    if not _tool("ffmpeg"):
        log("Game detector | ffmpeg not installed; skipping transcription")
        return ""
    audio = workdir / "audio.wav"
    proc = await asyncio.to_thread(_run, ["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)], 120)
    if proc.returncode != 0 or not audio.exists():
        return ""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("Game detector | faster-whisper not installed; skipping transcription")
        return ""
    model_name = os.getenv("WHISPER_MODEL", "small")
    device = os.getenv("WHISPER_DEVICE", "auto")
    compute = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    def transcribe_sync():
        actual_device = "cuda" if device == "auto" and os.getenv("CUDA_VISIBLE_DEVICES", "") != "-1" else ("cpu" if device == "auto" else device)
        model = WhisperModel(model_name, device=actual_device, compute_type=compute)
        segments, _ = model.transcribe(str(audio), vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments if segment.text.strip())
    try:
        return await asyncio.to_thread(transcribe_sync)
    except Exception as exc:
        log(f"Game detector transcription error | {type(exc).__name__}: {exc}")
        return ""


async def _ocr_image(image: Path) -> str:
    try:
        import pytesseract
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
        def run_ocr():
            img = Image.open(image).convert("RGB")
            # Upscale/contrast/sharpen improves small HUD/menu text.
            scale = 2 if max(img.size) < 1800 else 1
            if scale > 1:
                img = img.resize((img.width * scale, img.height * scale))
            gray = ImageOps.grayscale(img)
            gray = ImageEnhance.Contrast(gray).enhance(1.7)
            gray = gray.filter(ImageFilter.SHARPEN)
            outputs = []
            for psm in (6, 11):
                outputs.append(pytesseract.image_to_string(gray, config=f"--psm {psm}"))
            return "\n".join(outputs)
        return await asyncio.to_thread(run_ocr)
    except Exception:
        return ""


async def _ask_ollama(evidence: str) -> dict:
    prompt = f"""Identify ALL video games actually supported by this content.
There may be multiple distinct games.
Do not invent, infer sequels, or rename titles. A game must have meaningful evidence in the supplied content.
Return every distinct supported game, up to {MAX_GAMES}.

Return ONLY JSON:
{{"candidates":[{{"name":"Game title","confidence":0-100,"reason":"short evidence","evidence_type":"audio|caption|ocr|visual|other"}}]}}

CONTENT:
{evidence[:30000]}"""
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": "You are a strict video-game identification assistant. Never hallucinate a title."},
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": 0},
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(OLLAMA_URL, json=payload, timeout=aiohttp.ClientTimeout(total=180)) as response:
            response.raise_for_status()
            body = await response.json()
    content = body.get("message", {}).get("content", "")
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


async def _verify_steam(candidates: list[dict]) -> list[dict]:
    results = []
    async with aiohttp.ClientSession(headers={"User-Agent": "KeparGameDetector/1.0"}) as session:
        for candidate in candidates:
            name = str(candidate["name"]).strip()
            try:
                params = {"term": name, "cc": "ca", "l": "english"}
                async with session.get("https://store.steampowered.com/search/", params=params, timeout=15) as response:
                    html = await response.text()
                match = re.search(r'data-ds-appid="(\d+)"[^>]*>.*?<span class="title">([^<]+)</span>', html, re.DOTALL | re.IGNORECASE)
                candidate = dict(candidate)
                if match:
                    candidate["name"] = re.sub(r"\s+", " ", match.group(2)).strip()
                    candidate["steam_appid"] = int(match.group(1))
                    candidate["steam_url"] = f"https://store.steampowered.com/app/{match.group(1)}/"
                    candidate["confidence"] = min(100, float(candidate.get("confidence", 0)) + 10)
                    candidate["steam_verified"] = True
                else:
                    candidate["steam_verified"] = False
                results.append(candidate)
            except Exception as exc:
                log(f"Steam verification error | {name} | {type(exc).__name__}: {exc}")
                candidate = dict(candidate)
                candidate["steam_verified"] = False
                results.append(candidate)
    return results
