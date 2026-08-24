import re
from urllib.parse import urlparse

import discord

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
}

VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


async def process_game_message(message: discord.Message) -> dict | None:
    """Collect every supported game-identification input from Discord."""
    content = (message.content or "").strip()
    urls = _get_supported_urls(content)

    videos = [a for a in message.attachments if _is_video_attachment(a)]
    images = [a for a in message.attachments if _is_image_attachment(a)]

    if not content and not videos and not images:
        return None

    sources = [f"{source} link" for _, source in urls]
    if videos:
        count = len(videos)
        sources.append(f"{count} Discord video attachment{'s' if count != 1 else ''}")
    if images:
        count = len(images)
        sources.append(f"{count} Discord image/screenshot attachment{'s' if count != 1 else ''}")
    if content:
        sources.append("text/caption" if urls else "direct text")

    log(f"Game detector | message={message.id} | sources={', '.join(sources)}")

    return {
        "status": "queued",
        "message_id": message.id,
        "text": content,
        "urls": [url for url, _ in urls],
        "url_sources": [source for _, source in urls],
        "video_attachments": [_attachment_data(a) for a in videos],
        "image_attachments": [_attachment_data(a) for a in images],
        "attachment_count": len(videos) + len(images),
        "sources": sources,
    }


def _get_supported_urls(content: str) -> list[tuple[str, str]]:
    results = []
    for raw_url in URL_PATTERN.findall(content):
        normalized = raw_url.rstrip(".,!?;:)]}>")
        host = (urlparse(normalized).hostname or "").lower()
        source = SUPPORTED_HOSTS.get(host)
        if source:
            results.append((normalized, source))
    return results


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
