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
MAX_GAMES = 10


def _run(command: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def _tool(name: str) -> str | None:
    return shutil.which(name)


async def analyze_game_input(data: dict) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="game-detector-"))
    try:
        text_parts = []
        text = data.get("text", "").strip()
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
        candidates = ai.get("candidates", []) if isinstance(ai, dict) else []
        candidates = [c for c in candidates if isinstance(c, dict) and c.get("name")][:MAX_GAMES]
        if not candidates:
            return {"status": "unknown", "message": "I couldn't identify a game from the available evidence."}

        verified = await _verify_steam(candidates)
        games = await enrich_games(verified[:MAX_GAMES] or candidates[:MAX_GAMES])

        return {
            "status": "identified",
            "game_count": len(games),
            "games": games,
            "game_name": games[0].get("name"),
            "confidence": float(games[0].get("confidence", 0)),
            "steam_url": games[0].get("steam_url"),
            "reason": games[0].get("reason", "AI identification from the supplied content."),
            "candidates": games,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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
        from PIL import Image
        return await asyncio.to_thread(lambda: pytesseract.image_to_string(Image.open(image)))
    except Exception:
        return ""


async def _ask_ollama(evidence: str) -> dict:
    prompt = f"""Identify ALL video games mentioned, shown, or strongly implied by this content.
There may be multiple distinct games. Do NOT stop after finding the first one.
Do not invent titles. Only return a game when there is meaningful evidence.
If one game is mentioned repeatedly, return it only once.

Return ONLY valid JSON in this exact shape:
{{"candidates":[{{"name":"Game title","confidence":0-100,"reason":"short evidence","evidence_type":"audio|caption|ocr|visual|other"}}]}}

Order candidates from strongest to weakest evidence. Include up to {MAX_GAMES} distinct games.
Use the title/description/transcript/OCR as evidence.

CONTENT:
{evidence[:24000]}"""

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": "You are a strict video-game identification assistant. A single piece of content can contain multiple different games. Return only the requested JSON and do not invent games."},
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": 0.1},
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
                if match:
                    candidate = dict(candidate)
                    candidate["name"] = re.sub(r"\s+", " ", match.group(2)).strip()
                    candidate["steam_appid"] = int(match.group(1))
                    candidate["steam_url"] = f"https://store.steampowered.com/app/{match.group(1)}/"
                    candidate["confidence"] = min(100, float(candidate.get("confidence", 0)) + 10)
                    candidate["steam_verified"] = True
                else:
                    candidate = dict(candidate)
                    candidate["steam_verified"] = False
                results.append(candidate)
            except Exception as exc:
                log(f"Steam verification error | {name} | {type(exc).__name__}: {exc}")
                candidate = dict(candidate)
                candidate["steam_verified"] = False
                results.append(candidate)
    return sorted(results, key=lambda x: float(x.get("confidence", 0)), reverse=True)
