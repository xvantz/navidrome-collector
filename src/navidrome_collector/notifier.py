"""Notification abstraction: console + optional Telegram bot."""

import json as _json
import logging
import os
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

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
            return _json.loads(resp.read())
    except Exception as e:
        log.debug("Telegram API %s error: %s", method, e)
        return None


def send_message(text: str) -> None:
    """Send notification to configured chats."""
    _, chat_ids = _load_config()
    if not chat_ids:
        log.info("[NOTIFY] %s", text)
        return
    for cid in chat_ids:
        _api("sendMessage", {
            "chat_id": cid.strip(),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })


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

    return handled
