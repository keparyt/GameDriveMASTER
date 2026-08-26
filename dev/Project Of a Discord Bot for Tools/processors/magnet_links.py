from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from urllib.parse import quote

import config
from utils.helper import log

MAGNET_LINKS_FILE = Path(getattr(config, "MAGNET_LINKS_FILE", "data/magnet_links.json"))


def _load() -> dict[str, dict]:
    try:
        value = json.loads(MAGNET_LINKS_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(value: dict[str, dict]) -> None:
    MAGNET_LINKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = MAGNET_LINKS_FILE.with_suffix(MAGNET_LINKS_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(MAGNET_LINKS_FILE)


def _link_id(magnet_url: str) -> str:
    return hashlib.sha256(magnet_url.encode("utf-8")).hexdigest()[:24]


def register_magnet(magnet_url: str, *, name: str = "", source: str = "", title: str = "") -> dict:
    magnet_url = str(magnet_url or "").strip()
    if not magnet_url.lower().startswith("magnet:?"):
        raise ValueError("Only magnet: URLs can be registered.")

    link_id = _link_id(magnet_url)
    links = _load()
    record = links.get(link_id) or {
        "id": link_id,
        "magnet_url": magnet_url,
    }
    if name:
        record["name"] = name
    if source:
        record["source"] = source
    if title:
        record["title"] = title
    links[link_id] = record
    _save(links)
    log(f"[MagnetLinks] Registered {link_id} for {name or title or 'magnet'}")
    return record


def get_magnet(link_id: str) -> dict | None:
    return _load().get(str(link_id or "").strip())


def build_public_url(link_id: str) -> str:
    base = str(getattr(config, "LAN_BASE_URL", "")).rstrip("/")
    return f"{base}/magnet/{quote(str(link_id), safe='')}"


def render_magnet_page(record: dict) -> str:
    name = html.escape(str(record.get("name") or record.get("title") or "Magnet link"))
    source = html.escape(str(record.get("source") or "Unknown source"))
    magnet = html.escape(str(record.get("magnet_url") or ""), quote=True)
    escaped_js_magnet = json.dumps(str(record.get("magnet_url") or ""))
    return f"""<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>GameDrive Magnet</title>
<style>
body{{font-family:system-ui,sans-serif;background:#070706;color:#f5f0e5;display:flex;justify-content:center;padding:40px}}
main{{max-width:720px;width:100%;background:#11100c;border:1px solid #38301f;border-radius:16px;padding:28px}}
h1{{font-size:28px;margin:0 0 8px}}p{{color:#aaa08f}}.meta{{font-size:14px;color:#b89b5a;margin-bottom:20px}}
.button{{display:inline-block;padding:12px 18px;border-radius:10px;background:#d4af5a;color:#100e09;text-decoration:none;font-weight:700;margin-right:8px}}
.secondary{{background:#1b1812;color:#f5f0e5;border:1px solid #4a402b}}
code{{display:block;word-break:break-all;background:#090806;padding:14px;border-radius:10px;color:#cfc5b3;margin-top:16px}}
</style>
</head>
<body>
<main>
<h1>🎮 {name}</h1>
<div class=\"meta\">Source: {source}</div>
<p>This page keeps the original magnet target available while providing a normal HTTPS link for Discord.</p>
<a class=\"button\" href=\"{magnet}\" id=\"open\">Open Magnet</a>
<button class=\"button secondary\" onclick=\"navigator.clipboard.writeText({escaped_js_magnet})\">Copy Magnet</button>
<code>{magnet}</code>
<script>
document.getElementById('open').addEventListener('click', function() {{ window.location.href = {escaped_js_magnet}; }});
</script>
</main>
</body>
</html>"""
