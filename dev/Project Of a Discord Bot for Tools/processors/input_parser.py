import re
from urllib.parse import urlparse

import aiohttp
import discord
from bs4 import BeautifulSoup

from utils.helper import log

URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

SUPPORTED_HOSTS = {
    "instagram.com": "Instagram",
    "www.instagram.com": "Instagram",
    "m.instagram.com": "Instagram",
    "tiktok.com": "TikTok",
    "www.tiktok.com": "TikTok",
    "vm.tiktok.com": "TikTok",
    "vt.tiktok.com": "TikTok",
    "youtube.com": "YouTube",
    "www.youtube.com": "YouTube",
    "m.youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "store.steampowered.com": "Steam",
    "steamcommunity.com": "Steam",
}

VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


async def process_game_message(message: discord.Message) -> dict | None:
    """Collect every supported game-identification input from Discord.

    Supported:
    - YouTube / Instagram / TikTok media URLs
    - direct Steam store URLs
    - direct image/video URLs
    - Discord image/video attachments
    - direct text

    Unknown URLs are reported to the caller instead of being fed to the game AI as
    if the URL itself were a game title.
    """
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

    # Resolve Steam store links to their canonical store title before handing the
    # text to the existing analyzer. This keeps the normal Steam verification path.
    steam_titles = []
    for url in steam_urls[:10]:
        title = await _steam_title_from_url(url)
        if title:
            steam_titles.append(title)
            log(f"Game detector | Steam URL resolved | url={url} | title={title!r}")

    # Direct media URLs are represented exactly like Discord attachments so the
    # existing OCR/transcription pipeline processes them.
    direct_video_data = []
    direct_image_data = []
    for index, url in enumerate(direct_media_urls[:20], start=1):
        suffix = _media_suffix(url)
        filename = f"direct_media_{index}{suffix}"
        item = {"id": f"url-{index}", "filename": filename, "url": url, "content_type": None, "size": 0}
        if suffix in VIDEO_EXTENSIONS:
            direct_video_data.append(item)
        elif suffix in IMAGE_EXTENSIONS:
            direct_image_data.append(item)

    all_videos = [_attachment_data(a) for a in videos] + direct_video_data
    all_images = [_attachment_data(a) for a in images] + direct_image_data

    # If a message contains an unsupported URL and no usable input besides it,
    # don't let the AI hallucinate a game from the URL. The event handler can ask
    # the user for the direct Steam/KeparDB game page and proper game name.
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

    videos_count = len(all_videos)
    images_count = len(all_images)
    sources = [f"{source} link" for _, source in supported_urls]
    if steam_urls:
        sources.append(f"{len(steam_urls)} Steam store link{'s' if len(steam_urls) != 1 else ''}")
    if direct_media_urls:
        sources.append(f"{len(direct_media_urls)} direct media URL{'s' if len(direct_media_urls) != 1 else ''}")
    if videos_count:
        sources.append(f"{videos_count} Discord/direct video attachment{'s' if videos_count != 1 else ''}")
    if images_count:
        sources.append(f"{images_count} Discord/direct image attachment{'s' if images_count != 1 else ''}")
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
        "urls": [url for url, _ in supported_urls],
        "url_sources": [source for _, source in supported_urls],
        "video_attachments": all_videos,
        "image_attachments": all_images,
        "attachment_count": videos_count + images_count,
        "sources": sources,
        "unsupported_urls": unsupported_urls,
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
    """Extract the canonical Steam game title from a direct store page URL."""
    parsed = urlparse(url)
    if parsed.hostname not in {"store.steampowered.com", "steamcommunity.com"}:
        return None
    match = re.search(r"/app/(\d+)", parsed.path)
    if not match:
        return None
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "KeparGameDetector/1.0"}) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status != 200:
                    return None
                html = await response.text(errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one("meta[property='og:title']")
        if node and node.get("content"):
            title = str(node["content"]).strip()
            title = re.sub(r"\s+on Steam\s*$", "", title, flags=re.I).strip()
            if title:
                return title
        title_node = soup.select_one("title")
        if title_node:
            title = title_node.get_text(" ", strip=True)
            title = re.sub(r"\s+on Steam\s*$", "", title, flags=re.I).strip()
            return title or None
    except Exception as exc:
        log(f"Game detector Steam URL error | {url} | {type(exc).__name__}: {exc}")
    return None


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
