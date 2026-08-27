import asyncio
import difflib
import re
import time
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp
from bs4 import BeautifulSoup

from utils.helper import log

USER_AGENT = "KeparGameDetector/2.2"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=6, connect=2, sock_read=4)
DDG_ENDPOINTS = ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/")
DDG_MAX_ATTEMPTS = 2
DDG_RETRY_STATUSES = {429, 500, 502, 503, 504}
_DDG_FAILURES = 0
_DDG_OPEN_UNTIL = 0.0
_DDG_FAILURE_THRESHOLD = 2
_DDG_COOLDOWN_SECONDS = 300.0
_DDG_LOCK = asyncio.Lock()
_DDG_SEM = asyncio.Semaphore(1)


@dataclass(frozen=True)
class StoreMatch:
    provider: str
    title: str
    url: str
    score: float
    sequence: float


def _norm(value: str) -> str:
    value = str(value or "").casefold().replace("&", " and ")
    value = re.sub(r"[™®©]", "", value)
    value = re.sub(r"\b(?:demo|trial)\b", "", value)
    value = re.sub(r"\s*[-–—|:]+\s*(?:battle\.net|gog\.com|epic games store).*$", "", value, flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip()


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _word_similarity(a: str, b: str) -> float:
    aw, bw = _norm(a).split(), _norm(b).split()
    if not aw or not bw:
        return 0.0
    used, scores = set(), []
    for word in aw:
        best, index = 0.0, None
        for i, other in enumerate(bw):
            if i in used:
                continue
            score = difflib.SequenceMatcher(None, word, other).ratio()
            if score > best:
                best, index = score, i
        if index is not None:
            used.add(index)
        scores.append(best)
    return sum(scores) / len(scores)


def _credible(query: str, title: str) -> tuple[bool, float, float]:
    q, t = _norm(query), _norm(title)
    if not q or not t:
        return False, 0.0, 0.0
    if q == t:
        return True, 1.0, 1.0
    seq, word = _similarity(query, title), _word_similarity(query, title)
    return (seq >= 0.94 or (seq >= 0.90 and word >= 0.80) or (seq >= 0.86 and word >= 0.90)), seq, word


async def _steam(query: str, session: aiohttp.ClientSession) -> StoreMatch | None:
    try:
        async with session.get(
            "https://store.steampowered.com/search/",
            params={"term": query, "cc": "ca", "l": "english"},
            timeout=REQUEST_TIMEOUT,
        ) as response:
            if response.status != 200:
                log(f"Store verifier | Steam HTTP {response.status} | query={query!r}")
                return None
            html = await response.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log(f"Store verifier | Steam request error | query={query!r} | {type(exc).__name__}: {exc}")
        return None
    ranked = []
    for row in BeautifulSoup(html, "html.parser").select("a.search_result_row[data-ds-appid]")[:30]:
        node, appid = row.select_one(".title"), row.get("data-ds-appid")
        if not node or not appid or not str(appid).isdigit():
            continue
        title = " ".join(node.stripped_strings).strip()
        ok, seq, word = _credible(query, title)
        if ok:
            ranked.append((seq * 0.65 + word * 0.35, seq, word, title, int(appid)))
    if not ranked:
        return None
    _, seq, word, title, appid = max(ranked)
    return StoreMatch("steam", title, f"https://store.steampowered.com/app/{appid}/", seq * 0.65 + word * 0.35, seq)


def _parse_ddg_results(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select(".result a.result__a") or soup.select("a.result-link")
    results = []
    for anchor in anchors[:20]:
        href = str(anchor.get("href", "")).strip()
        title = " ".join(anchor.stripped_strings).strip()
        if href.startswith("/l/?"):
            target = parse_qs(urlparse(href).query).get("uddg", [])
            if target:
                href = unquote(target[0])
        if title and href.startswith(("http://", "https://")):
            results.append((title, href))
    return results


async def _ddg_available() -> bool:
    async with _DDG_LOCK:
        return time.monotonic() >= _DDG_OPEN_UNTIL


async def _record_ddg_failure() -> None:
    global _DDG_FAILURES, _DDG_OPEN_UNTIL
    async with _DDG_LOCK:
        _DDG_FAILURES += 1
        if _DDG_FAILURES >= _DDG_FAILURE_THRESHOLD:
            _DDG_OPEN_UNTIL = time.monotonic() + _DDG_COOLDOWN_SECONDS
            log(f"DuckDuckGo | circuit breaker opened | cooldown={_DDG_COOLDOWN_SECONDS:.0f}s")


async def _record_ddg_success() -> None:
    global _DDG_FAILURES, _DDG_OPEN_UNTIL
    async with _DDG_LOCK:
        _DDG_FAILURES = 0
        _DDG_OPEN_UNTIL = 0.0


async def _duckduckgo(query: str, domains: str, session: aiohttp.ClientSession) -> list[tuple[str, str]]:
    if not await _ddg_available():
        log(f"DuckDuckGo | circuit open | skipped | query={query!r}")
        return []
    params = {"q": f"site:{domains} {query}"}
    last_error = None
    async with _DDG_SEM:
        for attempt, endpoint in enumerate(DDG_ENDPOINTS[:DDG_MAX_ATTEMPTS], 1):
            try:
                async with session.get(
                    endpoint,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                    headers={"Accept": "text/html,application/xhtml+xml", "Referer": "https://duckduckgo.com/"},
                ) as response:
                    if response.status == 200:
                        html = await response.text()
                        results = _parse_ddg_results(html)
                        if results:
                            await _record_ddg_success()
                            log(f"DuckDuckGo | success | endpoint={endpoint} | query={query!r} | results={len(results)}")
                            return results
                        last_error = "no usable results"
                        break
                    last_error = f"HTTP {response.status}"
                    await response.read()
                    log(f"DuckDuckGo | HTTP {response.status} | attempt={attempt} | endpoint={endpoint} | query={query!r}")
                    if response.status == 429:
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                log(f"DuckDuckGo | request error | attempt={attempt} | endpoint={endpoint} | query={query!r} | {type(exc).__name__}: {exc}")
                continue
    await _record_ddg_failure()
    log(f"DuckDuckGo | unavailable | query={query!r} | error={last_error!r}")
    return []


async def _search_provider(query: str, provider: str, session: aiohttp.ClientSession) -> StoreMatch | None:
    domains = {"epic": "store.epicgames.com", "gog": "gog.com", "battle.net": "shop.battle.net"}[provider]
    results = await _duckduckgo(query, domains, session)
    ranked = []
    for title, url in results:
        host = (urlparse(url).hostname or "").lower()
        if provider == "epic" and "epicgames.com" not in host:
            continue
        if provider == "gog" and host not in {"gog.com", "www.gog.com"}:
            continue
        if provider == "battle.net" and "battle.net" not in host:
            continue
        if provider == "gog" and "/dreamlist/" in url.lower():
            continue
        ok, seq, word = _credible(query, title)
        if not ok:
            path = urlparse(url).path.replace("/", " ").replace("-", " ").replace("_", " ")
            slug_seq = _similarity(query, path)
            if slug_seq >= 0.94:
                ok, seq, word = True, slug_seq, max(word, slug_seq)
        if ok:
            ranked.append((seq * 0.65 + word * 0.35, seq, word, title, url))
    if not ranked:
        return None
    _, seq, word, title, url = max(ranked)
    return StoreMatch(provider, _norm(title).title(), url, seq * 0.65 + word * 0.35, seq)


async def find_store(query: str, providers=("epic", "gog", "battle.net")) -> StoreMatch | None:
    """Search first-party stores with strict DDG safeguards."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9", "Accept": "text/html,application/xhtml+xml"}
    connector = aiohttp.TCPConnector(limit=4, limit_per_host=1, ttl_dns_cache=300, enable_cleanup_closed=True)
    async with aiohttp.ClientSession(headers=headers, connector=connector, timeout=REQUEST_TIMEOUT) as session:
        match = await _steam(query, session)
        if match:
            log(f"Store verifier | Steam accepted | query={query!r} | title={match.title!r} | seq={match.sequence:.3f}")
            return match
        for provider in providers:
            match = await _search_provider(query, provider, session)
            if match:
                log(f"Store verifier | {provider} accepted | query={query!r} | title={match.title!r} | seq={match.sequence:.3f}")
                return match
            if not await _ddg_available():
                break
    return None


async def verify_with_stores(query: str):
    match = await find_store(query)
    if not match:
        return None
    return SimpleNamespace(
        name=match.title,
        game_title=match.title,
        title=match.title,
        url=match.url,
        game_id=None,
        selected_platform_name="PC",
        console_names=(),
        pc_platform="PC",
        store_source=match.provider,
        store_url=match.url,
    )
