import difflib
import re
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from utils.helper import log

USER_AGENT = "KeparGameDetector/2.0"


@dataclass(frozen=True)
class StoreMatch:
    provider: str
    title: str
    url: str
    score: float
    sequence: float


def _norm(value: str) -> str:
    value = str(value or "").casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[™®©]", "", value)
    value = re.sub(r"\b(?:demo|trial)\b", "", value)
    value = re.sub(r"\s*[-–—|:]+\s*(?:battle\.net|gog\.com|epic games store).*$", "", value, flags=re.I)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _word_similarity(a: str, b: str) -> float:
    aw = _norm(a).split()
    bw = _norm(b).split()
    if not aw or not bw:
        return 0.0
    used = set()
    scores = []
    for word in aw:
        best = 0.0
        index = None
        for i, other in enumerate(bw):
            if i in used:
                continue
            s = difflib.SequenceMatcher(None, word, other).ratio()
            if s > best:
                best, index = s, i
        if index is not None:
            used.add(index)
        scores.append(best)
    return sum(scores) / len(scores)


def _credible(query: str, title: str) -> tuple[bool, float, float]:
    q = _norm(query)
    t = _norm(title)
    if not q or not t:
        return False, 0.0, 0.0
    if q == t:
        return True, 1.0, 1.0
    seq = _similarity(query, title)
    word = _word_similarity(query, title)
    # A near-perfect sequence match must not be rejected because punctuation,
    # apostrophes, or word segmentation lowered the weighted score.
    # Example: "Frogin' Around" -> "Froggin' Around".
    accepted = (
        seq >= 0.94
        or (seq >= 0.90 and word >= 0.80)
        or (seq >= 0.86 and word >= 0.90)
    )
    return accepted, seq, word


async def _get(session: aiohttp.ClientSession, url: str, **kwargs):
    try:
        return await session.get(url, timeout=aiohttp.ClientTimeout(total=15), **kwargs)
    except Exception:
        return None


async def _steam(query: str, session: aiohttp.ClientSession) -> StoreMatch | None:
    params = {"term": query, "cc": "ca", "l": "english"}
    response = await _get(session, "https://store.steampowered.com/search/", params=params)
    if response is None or response.status != 200:
        return None
    try:
        html = await response.text()
    finally:
        response.release()
    soup = BeautifulSoup(html, "html.parser")
    ranked = []
    for row in soup.select("a.search_result_row[data-ds-appid]")[:30]:
        node = row.select_one(".title")
        appid = row.get("data-ds-appid")
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


async def _duckduckgo(query: str, domains: str, session: aiohttp.ClientSession) -> list[tuple[str, str]]:
    response = await _get(
        session,
        "https://html.duckduckgo.com/html/",
        params={"q": f"site:{domains} {query}"},
    )
    if response is None or response.status != 200:
        return []
    try:
        html = await response.text()
    finally:
        response.release()
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for result in soup.select(".result")[:10]:
        anchor = result.select_one("a.result__a")
        if not anchor:
            continue
        href = anchor.get("href", "")
        title = " ".join(anchor.stripped_strings)
        if href and title:
            results.append((title, href))
    return results


async def _search_provider(query: str, provider: str, session: aiohttp.ClientSession) -> StoreMatch | None:
    domains = {
        "epic": "store.epicgames.com",
        "gog": "gog.com",
        "battle.net": "shop.battle.net",
    }[provider]
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
        # A GOG Dreamlist page means the game is requested on GOG, not sold there.
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


async def find_store(query: str) -> StoreMatch | None:
    """Check first-party PC stores independently of TheGamesDB/KeparDB."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    async with aiohttp.ClientSession(headers=headers) as session:
        match = await _steam(query, session)
        if match:
            log(f"Store verifier | Steam accepted | query={query!r} | title={match.title!r} | seq={match.sequence:.3f}")
            return match
        for provider in ("epic", "gog", "battle.net"):
            match = await _search_provider(query, provider, session)
            if match:
                log(f"Store verifier | {provider} accepted | query={query!r} | title={match.title!r} | seq={match.sequence:.3f}")
                return match
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
