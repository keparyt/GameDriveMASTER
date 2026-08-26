from __future__ import annotations

import asyncio
import difflib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import config
from utils.helper import log


DOWNLOAD_SOURCES_DIR = Path(getattr(config, "DOWNLOAD_SOURCES_DIR", "download_sources"))
PRIORITY_SOURCE = "onlinefix"

_TITLE_ALIASES = {
    "civ 6": "civilization vi",
    "civ vi": "civilization vi",
    "civ 7": "civilization vii",
    "civ vii": "civilization vii",
    "gmod": "garrys mod",
    "garys mod": "garrys mod",
    "gary s mod": "garrys mod",
}

_VERSION_RE = re.compile(r"\b(?:v|ver|version|build|b)\s*[0-9][0-9a-zA-Z._+\-]*\b.*$", re.IGNORECASE)
_FREE_DOWNLOAD_RE = re.compile(r"\bfree\s+(?:pc\s+)?download\b", re.IGNORECASE)
_TRAILING_METADATA_RE = re.compile(r"\s*(?:\([^()]{0,120}\)|\[[^\[\]]{0,120}\])\s*$")


@dataclass(frozen=True)
class DownloadSourceEntry:
    source: str
    title: str
    uris: tuple[str, ...]
    file_size: str | None = None
    upload_date: str | None = None
    source_file: str | None = None

    @property
    def primary_uri(self) -> str | None:
        return self.uris[0] if self.uris else None


@dataclass(frozen=True)
class _IndexedEntry:
    entry: DownloadSourceEntry
    normalized_title: str
    tokens: frozenset[str]


@dataclass
class _SourceState:
    path: Path
    source_name: str
    signature: tuple[int, int]
    entries: tuple[_IndexedEntry, ...]


_lock = asyncio.Lock()
_sources: dict[Path, _SourceState] = {}
_initialized = False


def _normalize_title(value: str) -> str:
    value = str(value or "").casefold().strip()
    value = _FREE_DOWNLOAD_RE.sub(" ", value)
    value = _VERSION_RE.sub(" ", value)
    for _ in range(3):
        cleaned = _TRAILING_METADATA_RE.sub("", value).strip()
        if cleaned == value:
            break
        value = cleaned
    value = value.replace("&", " and ")
    value = re.sub(r"[™®©]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return _TITLE_ALIASES.get(value, value)


def _title_key(value: str) -> str:
    return _normalize_title(value)


def _tokens(value: str) -> frozenset[str]:
    return frozenset(token for token in _normalize_title(value).split() if len(token) > 1)


def _valid_uri(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    uri = value.strip()
    if not uri:
        return None
    if uri.casefold().startswith("magnet:?"):
        return uri
    try:
        parsed = urlparse(uri)
    except ValueError:
        return None
    if parsed.scheme.casefold() in {"http", "https", "ftp"} and parsed.netloc:
        return uri
    return None


def _parse_upload_date(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _entry_from_json(source_name: str, source_file: Path, value: object) -> DownloadSourceEntry | None:
    if not isinstance(value, dict):
        return None
    title = str(value.get("title") or "").strip()
    if not title or not _normalize_title(title):
        return None
    raw_uris = value.get("uris")
    if isinstance(raw_uris, str):
        raw_uris = [raw_uris]
    if not isinstance(raw_uris, list):
        raw_uris = []
    uris = tuple(dict.fromkeys(uri for uri in (_valid_uri(item) for item in raw_uris) if uri))
    file_size = value.get("fileSize")
    upload_date = value.get("uploadDate")
    return DownloadSourceEntry(
        source=source_name,
        title=title,
        uris=uris,
        file_size=str(file_size).strip() if file_size is not None and str(file_size).strip() else None,
        upload_date=str(upload_date).strip() if upload_date is not None and str(upload_date).strip() else None,
        source_file=source_file.name,
    )


def _read_source(path: Path) -> _SourceState | None:
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        log(f"[DownloadSources] Failed to load {path.name}: {type(exc).__name__}: {exc}")
        return None

    if not isinstance(payload, dict):
        log(f"[DownloadSources] Ignored {path.name}: root JSON value must be an object")
        return None
    source_name = str(payload.get("name") or path.stem).strip() or path.stem
    downloads = payload.get("downloads")
    if not isinstance(downloads, list):
        log(f"[DownloadSources] Ignored {path.name}: 'downloads' must be an array")
        return None

    indexed: list[_IndexedEntry] = []
    for raw in downloads:
        entry = _entry_from_json(source_name, path, raw)
        if entry:
            indexed.append(_IndexedEntry(entry, _title_key(entry.title), _tokens(entry.title)))
    return _SourceState(path, source_name, signature, tuple(indexed))


def _priority(path: Path) -> tuple[int, str]:
    return (0 if path.stem.casefold() == PRIORITY_SOURCE else 1, path.name.casefold())


async def refresh_sources(force: bool = False) -> None:
    global _sources, _initialized
    async with _lock:
        DOWNLOAD_SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        paths = sorted(DOWNLOAD_SOURCES_DIR.glob("*.json"), key=_priority)
        current = set(paths)

        for path in list(_sources):
            if path not in current:
                _sources.pop(path, None)

        for path in paths:
            try:
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                continue

            cached = _sources.get(path)
            if cached and not force and cached.signature == signature:
                continue

            state = _read_source(path)
            if state is None:
                # Keep the last known-good index if a source is temporarily being
                # rewritten or becomes malformed. A bad file must not erase a
                # previously usable source or take the bot offline.
                if cached:
                    log(f"[DownloadSources] Keeping previous valid index for {path.name}")
                continue

            _sources[path] = state
            log(f"[DownloadSources] {state.source_name}: {len(state.entries):,} entries ({path.name})")

        if not _initialized or force:
            log(f"[DownloadSources] Loaded {len(_sources)} sources from {DOWNLOAD_SOURCES_DIR}")
            _initialized = True


def _score(query: str, candidate: _IndexedEntry) -> float:
    normalized_query = _title_key(query)
    if not normalized_query or not candidate.normalized_title:
        return 0.0
    if normalized_query == candidate.normalized_title:
        base = 1.0
    elif normalized_query in candidate.normalized_title or candidate.normalized_title in normalized_query:
        shorter = min(len(normalized_query), len(candidate.normalized_title))
        longer = max(len(normalized_query), len(candidate.normalized_title))
        base = 0.94 + 0.04 * (shorter / max(longer, 1))
    else:
        sequence = difflib.SequenceMatcher(None, normalized_query, candidate.normalized_title).ratio()
        query_tokens = _tokens(normalized_query)
        overlap = len(query_tokens & candidate.tokens) / max(len(query_tokens), 1)
        candidate_overlap = len(query_tokens & candidate.tokens) / max(len(candidate.tokens), 1)
        token_score = (overlap * 0.75) + (candidate_overlap * 0.25)
        base = (sequence * 0.55) + (token_score * 0.45)
    if candidate.entry.uris:
        base += 0.015
    return min(base, 1.0)


def _best_in_source(query: str, state: _SourceState) -> tuple[float, _IndexedEntry] | None:
    ranked = []
    for candidate in state.entries:
        score = _score(query, candidate)
        if score >= 0.72:
            ranked.append((score, candidate))
    if not ranked:
        return None
    return max(
        ranked,
        key=lambda pair: (
            pair[0],
            _parse_upload_date(pair[1].entry.upload_date),
            bool(pair[1].entry.uris),
            bool(pair[1].entry.file_size),
        ),
    )


async def find_download_source(game_name: str) -> DownloadSourceEntry | None:
    """Find the best local download entry, with onlinefix.json always first."""
    query = str(game_name or "").strip()
    if not _title_key(query):
        return None
    await refresh_sources()
    ordered = sorted(_sources.values(), key=lambda state: _priority(state.path))
    for state in ordered:
        match = _best_in_source(query, state)
        if match is None:
            continue
        score, indexed = match
        entry = indexed.entry
        log(
            f"[DownloadSources] Match | requested={query!r} | title={entry.title!r} | "
            f"source={entry.source} | score={score:.3f} | size={entry.file_size or 'unknown'} | "
            f"uris={len(entry.uris)}"
        )
        return entry
    log(f"[DownloadSources] No match | requested={query!r}")
    return None


async def source_stats() -> list[dict]:
    await refresh_sources()
    return [
        {"source": state.source_name, "file": state.path.name, "entries": len(state.entries)}
        for state in sorted(_sources.values(), key=lambda state: _priority(state.path))
    ]
