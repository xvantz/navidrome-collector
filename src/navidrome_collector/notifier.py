"""Notification abstraction: console + optional Telegram."""

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


def _load_config() -> tuple[Optional[str], list[str]]:
    """Load Telegram config from env vars."""
    token = os.environ.get("NVC_TELEGRAM_TOKEN", "")
    chat_ids_raw = os.environ.get("NVC_TELEGRAM_CHAT_IDS", "")
    chat_ids = [c.strip() for c in chat_ids_raw.split(",") if c.strip()]
    return token or None, chat_ids


def send_message(text: str) -> None:
    """Send a notification. Falls back to log if Telegram not configured."""
    token, chat_ids = _load_config()

    if not token or not chat_ids:
        log.info("[NOTIFY] %s", text)
        return

    import urllib.request
    import urllib.parse
    import json as _json

    for chat_id in chat_ids:
        try:
            data = _json.dumps({
                "chat_id": chat_id.strip(),
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }).encode()
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log.warning("Telegram notification failed for %s: %s", chat_id, e)
