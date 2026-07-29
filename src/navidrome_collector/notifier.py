"""Notification abstraction: console + optional Telegram bot."""

import json as _json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Optional

from .logger import stage_logger

log = logging.getLogger(__name__)
tg_log = stage_logger(__name__, stage="telegram")

_offset = 0  # last processed Telegram update ID


def _load_config() -> tuple[Optional[str], list[str]]:
    token = os.environ.get("NVC_TELEGRAM_TOKEN", "")
    chat_ids_raw = os.environ.get("NVC_TELEGRAM_CHAT_IDS", "")
    chat_ids = [c.strip() for c in chat_ids_raw.split(",") if c.strip()]
    return token or None, chat_ids


def _api(method: str, payload: dict) -> Optional[dict]:
    """Call Telegram Bot API."""
    token, _ = _load_config()
    if not token:
        return None
    try:
        data = _json.dumps(payload).encode()
        url = f"https://api.telegram.org/bot{token}/{method}"
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())
        tg_log.debug("API %s → ok", method)
        return result
    except Exception as e:
        tg_log.warning("API %s failed: %s", method, e)
        return None


def send_message(text: str) -> None:
    """Send notification to configured chats."""
    _, chat_ids = _load_config()
    if not chat_ids:
        log.info("[NOTIFY] %s", text)
        return
    tg_log.info("sending to %d chat(s): %.80s", len(chat_ids), text)
    for cid in chat_ids:
        _api("sendMessage", {
            "chat_id": cid.strip(),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })


def send_photo(photo_data: bytes, caption: str, filename: str = "cover.jpg") -> None:
    """Send a photo with caption to configured Telegram chats.

    Uses multipart/form-data upload via urllib.
    """
    _, chat_ids = _load_config()
    if not chat_ids or not photo_data:
        return
    for cid in chat_ids:
        _api_photo(cid.strip(), photo_data, caption, filename)


def _api_photo(chat_id: str, photo_data: bytes, caption: str, filename: str) -> Optional[dict]:
    """Upload a photo via Telegram Bot API (multipart/form-data)."""
    token, _ = _load_config()
    if not token:
        return None
    try:
        boundary = "----NVCFormBoundary" + _json.dumps(hash(chat_id))[1:8]
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
            f"{chat_id}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="parse_mode"\r\n\r\n'
            f"HTML\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + photo_data + (
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n'
            f"{caption}\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = _json.loads(resp.read())
        tg_log.debug("sendPhoto → ok (%d bytes)", len(photo_data))
        return result
    except Exception as e:
        tg_log.warning("sendPhoto failed: %s", e)
        return None


def listen_and_handle(queue_add_fn, queue_list_fn) -> int:
    """Poll Telegram for commands and handle them.

    Args:
        queue_add_fn: callable(query) to add a track
        queue_list_fn: callable() returning list of queue items

    Returns:
        Number of commands handled.
    """
    global _offset
    token, allowed_chats = _load_config()
    if not token or not allowed_chats:
        return 0

    result = _api("getUpdates", {
        "offset": _offset,
        "timeout": 5,
        "allowed_updates": ["message"],
    })
    if not result or not result.get("ok"):
        return 0

    handled = 0
    for update in result.get("result", []):
        _offset = update.get("update_id", 0) + 1
        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()

        # Only respond to allowed chats
        if chat_id not in allowed_chats:
            tg_log.debug("ignoring message from unauthorised chat %s", chat_id)
            continue

        # Dispatch commands
        if text.startswith("/add "):
            query = text[5:].strip()
            if query:
                idx = queue_add_fn(query)
                _api("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"✅ Added #{idx}: {query}",
                })
                handled += 1
                tg_log.info("command /add: #%d %s (chat %s)", idx, query, chat_id)
            else:
                _api("sendMessage", {
                    "chat_id": chat_id,
                    "text": "Usage: /add Artist - Song",
                })

        elif text == "/list":
            items = queue_list_fn()
            if not items:
                _api("sendMessage", {"chat_id": chat_id, "text": "Queue is empty."})
            else:
                lines = [f"<b>Queue ({len(items)}):</b>"]
                for it in items[:10]:
                    status_icon = {"pending": "⏳", "processing": "🔄", "done": "✅", "failed": "❌"}.get(it.status, "❓")
                    lines.append(f"{status_icon} #{it.id} {it.query}")
                if len(items) > 10:
                    lines.append(f"... and {len(items) - 10} more")
                _api("sendMessage", {
                    "chat_id": chat_id,
                    "text": "\n".join(lines),
                    "parse_mode": "HTML",
                })
            handled += 1
            tg_log.info("command /list (%d items, chat %s)", len(items) if items else 0, chat_id)

        elif text == "/start" or text == "/help":
            _api("sendMessage", {
                "chat_id": chat_id,
                "text": "🎵 <b>Navidrome Collector</b>\n\n"
                        "/add Artist - Song — add track\n"
                        "/list — show queue\n"
                        "/status — check connectivity",
                "parse_mode": "HTML",
            })
            handled += 1
            tg_log.info("command /start (chat %s)", chat_id)

        elif text == "/status":
            from .queue import Queue
            q = Queue(os.environ.get("NVC_DB", "/var/lib/navidrome-collector/queue.db"))
            stats = q.stats()
            total = sum(stats.values())
            _api("sendMessage", {
                "chat_id": chat_id,
                "text": f"📊 <b>Status</b>\nTotal: {total}\n"
                        + "\n".join(f"  {s}: {c}" for s, c in sorted(stats.items())),
                "parse_mode": "HTML",
            })
            handled += 1
            tg_log.info("command /status (%d items, chat %s)", total, chat_id)

    return handled


def _extract_cover(audio) -> Optional[bytes]:
    """Extract cover art bytes from an audio file opened with mutagen."""
    try:
        # MP4/M4A
        covr = audio.get("covr")
        if covr and isinstance(covr, list) and len(covr) > 0:
            cover = covr[0]
            if hasattr(cover, "write"):
                return cover.write()
            return bytes(cover)
        # FLAC / Ogg
        pics = audio.get("metadata_block_picture")
        if pics:
            import base64
            pic_data = base64.b64decode(pics[0]) if isinstance(pics, list) else base64.b64decode(pics)
            # Parse Picture block to extract image data
            from mutagen.flac import Picture
            pic = Picture(pic_data)
            return pic.data
        # MP3 APIC
        if hasattr(audio, "get"):
            for tag in ("APIC:", "APIC:Cover", "APIC:Front"):
                apic = audio.get(tag)
                if apic and hasattr(apic, "data"):
                    return apic.data
    except Exception:
        pass
    return None


def format_track_summary(item: dict) -> str:
    """Build a rich Telegram notification for a completed track.
    Returns (text, cover_data) for sending photo + caption.
    """
    query = item.get("query", "")
    file_path = item.get("file_path", "")

    artist = title = album = genre = ""
    lines = 0
    cover_data: Optional[bytes] = None
    source = item.get("source", "")

    if not source and file_path:
        if "YouTube" in file_path or "ytdlp" in file_path:
            source = "yt-dlp"
        elif Path(file_path).parent.name:
            source = "Soulseek"

    if file_path and Path(file_path).exists():
        try:
            from .tagger import read_tags
            meta = read_tags(file_path)
            if meta:
                artist = meta.artist or ""
                title = meta.title or ""
                album = meta.album or ""
                genre = meta.genre or ""
            import mutagen
            af = mutagen.File(file_path)
            if af is not None:
                cover_data = _extract_cover(af)
                for tag_key in ("lyrics", "USLT::'eng'", "©lyr"):
                    val = af.get(tag_key)
                    if val:
                        if isinstance(val, list):
                            val = val[0]
                        lines = len(str(val).split("\n"))
                        break
        except Exception:
            pass

    parts = [f"🔍 <b>{query}</b>"]
    if artist and title:
        parts.append(f"✅ <b>{artist} - {title}</b>")
    if source:
        parts.append(f"   📡 {source}")

    if album and album.lower() not in ("youtube", "unknown", ""):
        album_clean = album.split(" (")[0]
        if not (artist and album_clean.lower().startswith(artist.lower())):
            parts.append(f"   💿 {album}")

    chips = []
    if genre and genre.lower() not in ("music", "self-titled", "none", ""):
        chips.append(f"🎵 {genre}")
    if lines:
        chips.append(f"📝 {lines} lines")
    if cover_data:
        chips.append("🖼 cover")
    if chips:
        parts.append("   " + " | ".join(chips))

    if file_path:
        p = Path(file_path)
        short = "/".join(p.parts[-3:]) if len(p.parts) >= 3 else p.name
        parts.append(f"   📁 {short}")

    return "\n".join(parts)
