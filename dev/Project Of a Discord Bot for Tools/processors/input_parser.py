import re
from urllib.parse import urlparse

import aiohttp
import discord
from bs4 import BeautifulSoup

from config import (
    IMAGE_EXTENSIONS,
    STEAM_APPDETAILS_URL,
    STEAM_COUNTRY,
    STEAM_HOSTS,
    STEAM_LANGUAGE,
    STEAM_REQUEST_TIMEOUT_SECONDS,
    STEAM_USER_AGENT,
    SUPPORTED_SOCIAL_HOSTS,
    VIDEO_EXTENSIONS,
    MAX_SOURCE_URLS,
    MAX_SOCIAL_MEDIA_ITEMS,
)
from utils.helper import log

URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
SUPPORTED_HOSTS = dict(SUPPORTED_SOCIAL_HOSTS)
SUPPORTED_HOSTS.update({host: "Steam" for host in STEAM_HOSTS})


async def process_game_message(message: discord.Message) -> dict | None:
    """Collect every supported game-identification input from Discord."""
    content = (message.content or "").strip()
    raw_urls = _extract_urls(content)
    supported_urls = _get_supported_urls(raw_urls)
    unsupported_urls = []
    steam_urls = []
    direct_media_urls = []

    for url in raw_urls:
        host = (urlparse(url).hostname or "").lower()
        if host in SUPPORTED_HOSTS:
            if SUPPORTED_HOSTS[host] == "Steam":
                steam_urls.append(url)
            continue
        if _is_direct_media_url(url):
            direct_media_urls.append(url)
            continue
        unsupported_urls.append(url)

    videos = [a for a in message.attachments if _is_video_attachment(a)]
    images = [a for a in message.attachments if _is_image_attachment(a)]

    steam_titles = []
    for url in steam_urls[:MAX_SOURCE_URLS]:
        title = await _steam_title_from_url(url)
        if title:
            steam_titles.append(title)
            log(f"Game detector | Steam URL resolved | url={url} | title={title!r}")
        else:
            log(f"Game detector | Steam URL title unresolved | url={url}")

    direct_video_data = []
    direct_image_data = []
    for index, url in enumerate(direct_media_urls[:MAX_SOCIAL_MEDIA_ITEMS], start=1):
        suffix = _media_suffix(url)
        filename = f"direct_media_{index}{suffix}"
        item = {"id": f"url-{index}", "filename": filename, "url": url, "content_type": None, "size": 0}
        if suffix in VIDEO_EXTENSIONS:
            direct_video_data.append(item)
        elif suffix in IMAGE_EXTENSIONS:
            direct_image_data.append(item)

    all_videos = [_attachment_data(a) for a in videos] + direct_video_data
    all_images = [_attachment_data(a) for a in images] + direct_image_data
    non_url_text = URL_PATTERN.sub("", content).strip()

    if unsupported_urls and not supported_urls and not steam_titles and not all_videos and not all_images and not non_url_text:
        return {
            "status": "unsupported_url",
            "unsupported_urls": unsupported_urls,
            "message": (
                "I can't identify a game from that URL. If it is a game page, please send the direct "
                "Steam or KeparDB store page URL and the game's proper name. If it is a direct image/video URL, "
                "send the direct media URL instead."
            ),
        }

    sources = [f"{source} link" for _, source in supported_urls]
    if steam_urls:
        sources.append(f"{len(steam_urls)} Steam store link{'s' if len(steam_urls) != 1 else ''}")
    if direct_media_urls:
        sources.append(f"{len(direct_media_urls)} direct media URL{'s' if len(direct_media_urls) != 1 else ''}")
    if all_videos:
        sources.append(f"{len(all_videos)} Discord/direct video attachment{'s' if len(all_videos) != 1 else ''}")
    if all_images:
        sources.append(f"{len(all_images)} Discord/direct image attachment{'s' if len(all_images) != 1 else ''}")
    if non_url_text or steam_titles:
        sources.append("text/caption" if supported_urls or steam_urls else "direct text")

    log(f"Game detector | message={message.id} | sources={', '.join(sources) if sources else 'none'}")
    merged_text = non_url_text
    if steam_titles:
        merged_text = (merged_text + "\n" if merged_text else "") + "\n".join(steam_titles)

    return {
        "status": "queued",
        "message_id": message.id,
        "text": merged_text,
        "urls": [url for url, _ in supported_urls][:MAX_SOURCE_URLS],
        "url_sources": [source for _, source in supported_urls][:MAX_SOURCE_URLS],
        "video_attachments": all_videos[:MAX_SOCIAL_MEDIA_ITEMS],
        "image_attachments": all_images[:MAX_SOCIAL_MEDIA_ITEMS],
        "attachment_count": len(all_videos) + len(all_images),
        "sources": sources,
        "unsupported_urls": unsupported_urls,
        "steam_urls": steam_urls,
        "steam_titles": steam_titles,
    }


def _extract_urls(content: str) -> list[str]:
    return [raw.rstrip(".,!?;:)]}>") for raw in URL_PATTERN.findall(content)]


def _get_supported_urls(urls: list[str]) -> list[tuple[str, str]]:
    results = []
    for normalized in urls:
        host = (urlparse(normalized).hostname or "").lower()
        source = SUPPORTED_HOSTS.get(host)
        if source and source != "Steam":
            results.append((normalized, source))
    return results


def _is_direct_media_url(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    return path.endswith(VIDEO_EXTENSIONS + IMAGE_EXTENSIONS)


def _media_suffix(url: str) -> str:
    path = (urlparse(url).path or "").lower()
    for extension in VIDEO_EXTENSIONS + IMAGE_EXTENSIONS:
        if path.endswith(extension):
            return extension
    return ".bin"


async def _steam_title_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname not in STEAM_HOSTS:
        return None

    match = re.search(r"/app/(\d+)", parsed.path, re.IGNORECASE)
    if not match:
        return None
    appid = match.group(1)
    headers = {"User-Agent": STEAM_USER_AGENT}

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(
                STEAM_APPDETAILS_URL,
                params={"appids": appid, "cc": STEAM_COUNTRY, "l": STEAM_LANGUAGE},
                timeout=aiohttp.ClientTimeout(total=STEAM_REQUEST_TIMEOUT_SECONDS),
            ) as response:
                if response.status == 200:
                    payload = await response.json(content_type=None)
                    app_data = payload.get(str(appid), {}) if isinstance(payload, dict) else {}
                    data = app_data.get("data", {}) if isinstance(app_data, dict) else {}
                    api_title = str(data.get("name") or "").strip()
                    if api_title:
                        return api_title
                else:
                    log(f"Game detector Steam API | HTTP {response.status} | appid={appid}")

            async with session.get(url, timeout=aiohttp.ClientTimeout(total=STEAM_REQUEST_TIMEOUT_SECONDS)) as response:
                if response.status != 200:
                    return _steam_title_from_slug(parsed.path)
                html = await response.text(errors="ignore")

        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one("meta[property='og:title']")
        if node and node.get("content"):
            title = _clean_steam_title(str(node["content"]).strip())
            if title:
                return title
        title_node = soup.select_one("title")
        if title_node:
            title = _clean_steam_title(title_node.get_text(" ", strip=True))
            if title:
                return title
    except Exception as exc:
        log(f"Game detector Steam URL error | {url} | {type(exc).__name__}: {exc}")

    return _steam_title_from_slug(parsed.path)


def _clean_steam_title(value: str) -> str | None:
    title = re.sub(r"\s+on Steam\s*$", "", str(value or ""), flags=re.I).strip()
    title = re.sub(r"^Save\s+\d+%\s+on\s+", "", title, flags=re.I).strip()
    title = re.sub(r"^\d+%\s+off\s+", "", title, flags=re.I).strip()
    title = re.sub(r"^Free\s+to\s+Play\s+", "", title, flags=re.I).strip()
    return title or None


def _steam_title_from_slug(path: str) -> str | None:
    match = re.search(r"/app/\d+/([^/?#]+)", path, re.IGNORECASE)
    if not match:
        return None
    slug = match.group(1).replace("_", " ").replace("-", " ")
    slug = re.sub(r"\s+", " ", slug).strip()
    return slug.title() if slug else None


def _is_video_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    return content_type.startswith("video/") or attachment.filename.lower().endswith(VIDEO_EXTENSIONS)


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    return content_type.startswith("image/") or attachment.filename.lower().endswith(IMAGE_EXTENSIONS)


def _attachment_data(attachment: discord.Attachment) -> dict:
    return {
        "id": attachment.id,
        "filename": attachment.filename,
        "url": attachment.url,
        "content_type": attachment.content_type,
        "size": attachment.size,
    }
