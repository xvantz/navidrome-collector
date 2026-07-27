"""Centralized logging for navidrome-collector.

Provides:
  - Contextual loggers with stage + item_id
  - Timing context manager for measuring operation duration
  - Consistent setup across CLI and daemon modes

Usage:
    from .logger import stage_logger, timed, setup_logging

    log = stage_logger(__name__, stage="organize", item_id=42)
    log.info("moving file...")

    with timed(log, "copy"):
        shutil.copy2(src, dst)
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Optional


# ── Stage names (consistent across modules) ────────────────

STAGE_QUEUE = "queue"
STAGE_SEARCH = "search"
STAGE_DOWNLOAD = "download"
STAGE_ORGANIZE = "organize"
STAGE_TAG = "tag"
STAGE_YTD = "ytdlp"
STAGE_SLSKD = "slskd"
STAGE_TG = "telegram"
STAGE_DAEMON = "daemon"


# ── Contextual logger ──────────────────────────────────────

class StageAdapter(logging.LoggerAdapter):
    """LoggerAdapter that prepends stage and item_id context."""

    def process(self, msg, kwargs):
        stage = self.extra.get("stage", "")
        item_id = self.extra.get("item_id")
        ctx = ""
        if stage:
            ctx = f":{stage}"
        if item_id is not None:
            ctx += f" [#{item_id}]"
        return (f"{ctx} {msg}" if ctx else msg), kwargs


def stage_logger(name: str, stage: str = "", item_id: Optional[int] = None) -> StageAdapter:
    """Get a contextual logger for a specific stage and optional queue item."""
    return StageAdapter(logging.getLogger(name), {"stage": stage, "item_id": item_id})


# ── Timing ─────────────────────────────────────────────────

@contextmanager
def timed(logger: StageAdapter | logging.Logger, label: str = "", level: int = logging.INFO) -> Iterator[None]:
    """Time a block and log duration on success or failure.

    Usage:
        with timed(log, "slskd.search"):
            files = slskd.search(query)

    Logs on success:
        :search [#42] slskd.search completed in 2.3s

    Logs on failure:
        :search [#42] slskd.search FAILED after 1.2s: timeout
    """
    start = time.monotonic()
    try:
        yield
    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.log(level, "%s FAILED after %.1fs: %s", label, elapsed, exc)
        raise
    else:
        elapsed = time.monotonic() - start
        logger.log(level, "%s completed in %.1fs", label, elapsed)


# ── Root configuration ─────────────────────────────────────

_DATE_FMT = "%H:%M:%S"
_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s%(message)s"


def setup_logging(verbose: bool = False) -> None:
    """Configure root logger once.

    Call once at CLI/daemon entry-point, before any other imports.
    Strict mode: level=INFO (or DEBUG if verbose), stderr, ISO time.
    """
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FMT))
    logging.basicConfig(level=level, handlers=[handler], force=True)
