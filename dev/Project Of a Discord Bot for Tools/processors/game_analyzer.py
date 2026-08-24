import asyncio
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup

from processors.game_db import find_game, normalize_name
from utils.helper import log

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-r1:7b")
MAX_TRANSCRIPT_CHARS = 12000
MAX_GAMES = 25
STEAM_MATCH_THRESHOLD = 0.88
KEPARDB_MATCH_THRESHOLD = 0.88


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


def _token_similarity(a: str, b: str) -> float:
    at = set(normalize_name(a).split())
    bt = set(normalize_name(b).split())
    if not at or not bt:
        return 0.0
    return len(at & bt) / len(at | bt)


def _word_similarity(a: str, b: str) -> float:
    aw = normalize_name(a).split()
    bw = normalize_name(b).split()
    if not aw or not bw or abs(len(aw) - len(bw)) > 1:
        return 0.0
    used = set()
    scores = []
    for word in aw:
        best = 0.0
        best_index = None
        for i, other in enumerate(bw):
            if i in used:
                continue
            score = difflib.SequenceMatcher(None, word, other).ratio()
            if score > best:
                best = score
                best_index = i
        if best_index is not None:
            used.add(best_index)
        scores.append(best)
    return sum(scores) / len(scores) if scores else 0.0


def _correction_confidence(original: str, corrected: str) -> float:
    original_norm = normalize_name(original)
    corrected_norm = normalize_name(corrected)
    if not original_norm or not corrected_norm:
        return 0.0
    if original_norm == corrected_norm:
        return 100.0
    sequence = _similarity(original, corrected)
    word = _word_similarity(original, corrected)
    score = (sequence * 0.60 + word * 0.40) * 100.0
    return round(max(0.0, min(99.0, score)), 1)


def _credible_name_match(query: str, title: str, threshold: float) -> bool:
    q = normalize_name(query)
    t = normalize_name(title)
    if not q or not t:
        return False
    if q == t:
        return True
    sequence = _similarity(query, title)
    word = _word_similarity(query, title)
    qt = q.split()
    tt = t.split()
    if len(qt) >= 2 and len(tt) >= 2:
        return sequence >= 0.82 and word >= 0.82
    if len(qt) == 1 and len(tt) == 1:
        return sequence >= 0.93
    return sequence >= threshold and word >= 0.82


async def _deepseek_correct_name(name: str) -> str:
    """Normalize a candidate to the most likely real game title without dropping subtitles."""
    prompt = f"""Correct this possible video game title into the most likely REAL store title.

INPUT:
{name}

Rules:
- Fix spelling, phonetic spelling, missing apostrophes, plurals, abbreviations and obvious typing mistakes.
- Preserve every meaningful word and subtitle from the input.
- Never shorten a specific game to its base franchise/game.
- If the input contains a recognizable subtitle, preserve it.
- Expand obvious shorthand when it clearly identifies a known game.
- Correct roman numerals/numbers when the official title uses them.
- Resolve an obvious informal title when there is one overwhelmingly likely real game.
- Do NOT invent a sequel, remake, edition, or unrelated game.
- Do NOT substitute a more famous game merely because it is similar.
- If genuinely uncertain, return the input unchanged.
- Output ONLY the corrected game title.

Examples:
hello neigbour -> Hello Neighbor
five night a fweddy secu breach -> Five Nights at Freddy's: Security Breach
civilization 7 -> Sid Meier's Civilization VII
stalker 2 -> S.T.A.L.K.E.R. 2: Heart of Chornobyl
arc 2 -> ARC Raiders
elden ring -> Elden Ring

INPUT: {name}
"""
    payload = {"model": OLLAMA_MODEL, "stream": False, "messages": [{"role": "system", "content": "Correct video game names precisely. Preserve meaningful subtitles and prefer the exact real store title. Never hallucinate unrelated games."}, {"role": "user", "content": prompt}], "options": {"temperature": 0}}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(OLLAMA_URL, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as response:
                response.raise_for_status()
                body = await response.json()
        corrected = str(body.get("message", {}).get("content", "")).strip()
        corrected = re.sub(r"```.*?```", "", corrected, flags=re.DOTALL).strip()
        corrected = corrected.splitlines()[-1].strip(" \"'`*•") if corrected else ""
        if not corrected or len(corrected) > 150:
            return name
        if any(marker in corrected.casefold() for marker in ("because", "the correct", "i think", "i would", "likely ")):
            return name
        return corrected
    except Exception as exc:
        log(f"Game detector DeepSeek correction error | {name!r} | {type(exc).__name__}: {exc}")
        return name


async def analyze_game_input(data: dict) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="game-detector-"))
    try:
        text = data.get("text", "").strip()
        source_types = set(data.get("source_types", []))
        has_media = bool(data.get("urls") or data.get("video_attachments") or data.get("image_attachments"))
        if text and not has_media and (not source_types or source_types == {"direct_text"}):
            candidates = await _extract_direct_text(text)
            if not candidates:
                return {"status": "unknown", "message": "No explicit game names could be extracted from the text."}
            corrected_candidates = []
            for candidate in candidates[:MAX_GAMES]:
                original = candidate["name"]
                corrected = await _deepseek_correct_name(original)
                item = dict(candidate)
                item["detected_name"] = original
                item["name"] = corrected
                changed = normalize_name(original) != normalize_name(corrected)
                item["correction"] = corrected if changed else None
                item["ai_correction_confidence"] = _correction_confidence(original, corrected) if changed else 100.0
                if changed:
                    item["reason"] = f"DeepSeek spelling correction: {original} → {corrected}"
                corrected_candidates.append(item)
            games, unresolved = await _verify_and_enrich(corrected_candidates)
            return _result(games, unresolved)
        text_parts = []
        if text:
            text_parts.append(f"Discord text:\n{text}")
        media_files = []
        for url in data.get("urls", [])[:3]:
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
        # Normalize every AI candidate before verification. This is especially useful
        # for video lists where DeepSeek may return shorthand such as Civilization 7,
        # Arc 2, or Stalker 2 instead of the exact store title.
        corrected_candidates = []
        for candidate in candidates:
            original = str(candidate.get("name", "")).strip()
            corrected = await _deepseek_correct_name(original)
            item = dict(candidate)
            item["detected_name"] = original
            item["name"] = corrected
            changed = normalize_name(original) != normalize_name(corrected)
            item["correction"] = corrected if changed else None
            item["ai_correction_confidence"] = _correction_confidence(original, corrected) if changed else float(candidate.get("confidence", 100))
            if changed:
                item["reason"] = f"DeepSeek title normalization: {original} → {corrected}"
            corrected_candidates.append(item)
        games, unresolved = await _verify_and_enrich(corrected_candidates)
        return _result(games, unresolved)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _result(games: list[dict], unresolved: list[dict] | None = None) -> dict:
    unresolved = unresolved or []
    if not games:
        message = "No identified game could be verified on Steam or KeparDB."
        if unresolved:
            message += " Please provide a direct Steam or KeparDB store page link for each unresolved game."
        return {"status": "needs_store_link" if unresolved else "unknown", "message": message, "game_count": 0, "games": [], "unresolved_games": unresolved, "requires_store_link": bool(unresolved)}
    first = games[0]
    message = ""
    if unresolved:
        names = ", ".join(item["name"] for item in unresolved)
        message = f"{len(games)} game(s) verified. Could not verify: {names}. Please provide direct Steam or KeparDB store page links."
    return {"status": "identified" if not unresolved else "partial", "game_count": len(games), "games": games, "unresolved_games": unresolved, "requires_store_link": bool(unresolved), "game_name": first.get("name"), "confidence": float(first.get("ai_correction_confidence", first.get("confidence", 0))), "steam_url": first.get("steam_url"), "reason": first.get("reason", "Identification from supplied content."), "candidates": games, "message": message}


async def _extract_direct_text(text: str) -> list[dict]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    obvious = [re.sub(r"^\s*(?:[*•\-–—]|\d+[.)])\s*", "", line).strip() for line in lines]
    obvious = [x for x in obvious if x]
    if len(obvious) == 1:
        return [{"name": obvious[0], "confidence": 100, "reason": "explicitly present in direct text", "evidence_type": "direct_text"}]
    prompt = f"""Extract game titles EXPLICITLY WRITTEN in this text. Literal extraction only.
Never infer a sequel, remake, edition, series entry, or related game.
Never invent or replace a title. Preserve spelling exactly as supplied.
Return every distinct explicit title, maximum {MAX_GAMES}.
Return ONLY JSON: {{\"candidates\":[{{\"name\":\"exact text\",\"confidence\":100,\"reason\":\"explicitly present\",\"evidence_type\":\"direct_text\"}}]}}
INPUT:\n{text[:30000]}"""
    payload = {"model": OLLAMA_MODEL, "stream": False, "messages": [{"role": "system", "content": "Literal extraction only. Never hallucinate game titles."}, {"role": "user", "content": prompt}], "options": {"temperature": 0}}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(OLLAMA_URL, json=payload, timeout=aiohttp.ClientTimeout(total=180)) as response:
                response.raise_for_status()
                body = await response.json()
        match = re.search(r"\{.*\}", body.get("message", {}).get("content", ""), re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            candidates = _dedupe_candidates(parsed.get("candidates", []))
            source = [normalize_name(x) for x in obvious]
            safe = [c for c in candidates if normalize_name(c["name"]) in source]
            if safe:
                return safe
    except Exception as exc:
        log(f"Game detector direct-text AI extraction error | {type(exc).__name__}: {exc}")
    return [{"name": item, "confidence": 100, "reason": "explicitly present in direct text", "evidence_type": "direct_text"} for item in _dedupe_text_lines(obvious)]


def _dedupe_text_lines(lines: list[str]) -> list[str]:
    seen, result = set(), []
    for line in lines:
        key = re.sub(r"[^a-z0-9]+", "", line.casefold())
        if key and key not in seen:
            seen.add(key)
            result.append(line)
    return result[:MAX_GAMES]


async def _verify_and_enrich(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    verified, unresolved = [], []
    for candidate in _dedupe_candidates(candidates):
        original_name = str(candidate.get("name", "")).strip()
        if not original_name:
            continue
        steam_match = await _find_steam_match(original_name)
        if steam_match:
            item = dict(candidate)
            item.update(steam_match)
            item["detected_name"] = candidate.get("detected_name", original_name)
            item["verified"] = True
            item["verification_source"] = "steam"
            item["library_url"] = item["steam_url"]
            item["library_source"] = "steam"
            item["correction"] = steam_match["name"] if normalize_name(item["detected_name"]) != normalize_name(steam_match["name"]) else None
            if item.get("correction"):
                item["ai_correction_confidence"] = _correction_confidence(item["detected_name"], steam_match["name"])
                item["reason"] = f"DeepSeek title correction: {item['detected_name']} → {steam_match['name']}"
                item["confidence"] = item["ai_correction_confidence"]
            verified.append(item)
            continue
        db_match = await find_game(original_name)
        if db_match and _credible_name_match(original_name, db_match.name, KEPARDB_MATCH_THRESHOLD):
            item = dict(candidate)
            item["detected_name"] = candidate.get("detected_name", original_name)
            item["name"] = db_match.name
            item["kepargamedb_name"] = db_match.name
            item["kepargamedb_url"] = db_match.url
            item["library_url"] = db_match.url
            item["library_source"] = "kepargamedb"
            item["verified"] = True
            item["verification_source"] = "kepardb"
            item["correction"] = db_match.name if normalize_name(item["detected_name"]) != normalize_name(db_match.name) else None
            if item.get("correction"):
                item["ai_correction_confidence"] = _correction_confidence(item["detected_name"], db_match.name)
                item["reason"] = f"DeepSeek title correction: {item['detected_name']} → {db_match.name}"
                item["confidence"] = item["ai_correction_confidence"]
            verified.append(item)
            continue
        unresolved.append({"name": candidate.get("detected_name", original_name), "detected_name": candidate.get("detected_name", original_name), "confidence": float(candidate.get("ai_correction_confidence", candidate.get("confidence", 0))), "reason": "No sufficiently reliable Steam or KeparDB match.", "requires_store_link": True})
    return verified[:MAX_GAMES], unresolved


async def _find_steam_match(query: str) -> dict | None:
    try:
        params = {"term": query, "cc": "ca", "l": "english"}
        async with aiohttp.ClientSession(headers={"User-Agent": "KeparGameDetector/1.0"}) as session:
            async with session.get("https://store.steampowered.com/search/", params=params, timeout=15) as response:
                if response.status != 200:
                    return None
                html = await response.text()
        soup = BeautifulSoup(html, "html.parser")
        ranked = []
        for row in soup.select("a.search_result_row[data-ds-appid]")[:20]:
            title_node = row.select_one(".title")
            appid = row.get("data-ds-appid")
            if not title_node or not appid or not str(appid).isdigit():
                continue
            title = " ".join(title_node.stripped_strings).strip()
            if not title:
                continue
            seq = _similarity(query, title)
            word = _word_similarity(query, title)
            token = _token_similarity(query, title)
            ranked.append((seq, word, token, title, int(appid)))
        if not ranked:
            return None
        seq, word, token, title, appid = max(ranked, key=lambda x: (x[0] * 0.45 + x[1] * 0.55, x[0], x[1]))
        if not _credible_name_match(query, title, STEAM_MATCH_THRESHOLD):
            log(f"Game verification | Steam rejected | query={query!r} best={title!r} similarity={seq:.3f} word={word:.3f} tokens={token:.3f}")
            return None
        return {"name": title, "steam_appid": appid, "steam_url": f"https://store.steampowered.com/app/{appid}/", "steam_verified": True, "confidence": (seq * 0.45 + word * 0.55) * 100}
    except Exception as exc:
        log(f"Steam verification error | {query} | {type(exc).__name__}: {exc}")
        return None


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
            scale = 2 if max(img.size) < 1800 else 1
            if scale > 1:
                img = img.resize((img.width * scale, img.height * scale))
            gray = ImageOps.grayscale(img)
            gray = ImageEnhance.Contrast(gray).enhance(1.7)
            gray = gray.filter(ImageFilter.SHARPEN)
            return "\n".join(pytesseract.image_to_string(gray, config=f"--psm {psm}") for psm in (6, 11))
        return await asyncio.to_thread(run_ocr)
    except Exception:
        return ""


async def _ask_ollama(evidence: str) -> dict:
    prompt = f"""Identify ALL video games actually supported by this content.
There may be multiple distinct games.
Do not invent, infer sequels, or rename titles. A game must have meaningful evidence in the supplied content.
Return every distinct supported game, maximum {MAX_GAMES}.
Return ONLY JSON: {{\"candidates\":[{{\"name\":\"Game title\",\"confidence\":0-100,\"reason\":\"short evidence\",\"evidence_type\":\"audio|caption|ocr|visual|other\"}}]}}
CONTENT:\n{evidence[:30000]}"""
    payload = {"model": OLLAMA_MODEL, "stream": False, "messages": [{"role": "system", "content": "You are a strict video-game identification assistant. Never hallucinate a title."}, {"role": "user", "content": prompt}], "options": {"temperature": 0}}
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
