"""Main pipeline: orchestrates search → download → tag → organize."""

import json
import logging
from pathlib import Path
from typing import Optional

from .queue import Queue
from .slskd_client import SlskdClient, SlskdFile
from .tagger import TrackMeta, read_tags
from .organizer import organize as organize_file

log = logging.getLogger(__name__)

_FORMAT_PREFERENCE = {
    ".flac": 5,
    ".wav":  4,
    ".m4a":  3,
    ".ogg":  2,
    ".opus": 2,
    ".mp3":  1,
    ".wma":  0,
}

_MAX_PARALLEL = 5  # how many users to enqueue at once


class Collector:
    """Orchestrates the full pipeline for a single track request."""

    def __init__(
        self,
        queue: Queue,
        slskd: SlskdClient,
        music_dir: str | Path,
        download_dir: str | Path,
        ytdlp_fallback: bool = True,
        ytdlp_dir: str | Path | None = None,
    ):
        self.queue = queue
        self.slskd = slskd
        self.music_dir = Path(music_dir)
        self.download_dir = Path(download_dir)
        self.ytdlp_fallback = ytdlp_fallback
        self.ytdlp_dir = Path(ytdlp_dir or download_dir / "ytdlp")

    def process_queue(self, max_items: int = 0) -> dict[str, int]:
        """Process pending items from the queue.

        1. First checks in-progress downloads (started on earlier runs)
        2. Then starts new pending items

        Returns dict with counts: processed, succeeded, failed.
        """
        stats = {"processed": 0, "succeeded": 0, "failed": 0}

        # 1. Check previously enqueued downloads
        for item in self.queue.list_items(status="processing"):
            result = self._check_downloads(item)
            if result:
                self.queue.mark_done(item.id, str(result))
                stats["succeeded"] += 1
            # if download still in progress — skip, next run will check again
            # if all failed — mark as failed and we can retry later

        # 2. Process new pending items
        while True:
            item = self.queue.next_pending()
            if item is None:
                break
            if max_items and stats["processed"] >= max_items:
                break

            stats["processed"] += 1
            try:
                result, enqueued = self._start_downloads(item.query)
                if result is True:
                    self.queue.mark_done(item.id, "")
                    stats["succeeded"] += 1
                elif result is None:
                    self.queue.mark_failed(item.id, "No source available")
                    stats["failed"] += 1
                else:
                    # result is False = enqueued, waiting for later check
                    self.queue.mark_processing(item.id, enqueued)
                    # don't count in processed/failed — it's pending
                    stats["processed"] -= 1
            except Exception as e:
                log.exception("Failed to process item %d: %s", item.id, e)
                self.queue.mark_failed(item.id, str(e))
                stats["failed"] += 1

        return stats

    def _start_downloads(self, query: str) -> tuple[Optional[bool], list]:
        """yt-dlp first (instant), then try Soulseek in background.

        Returns:
            (True, [])  → download complete (file organised)
            (False, enqueued) → Soulseek enqueued, waiting
            (None, [])  → nothing found at all
        """
        # Step 1: yt-dlp — always works, gives instant result
        yt = self._ytdlp_fallback(query)
        if yt:
            return (True, [])

        # Step 2: Soulseek — try to find FLAC/320kbps
        files = self.slskd.search(query)
        if not files:
            return (None, [])

        files.sort(key=lambda f: self._score(f), reverse=True)
        enqueued: list[tuple[str, str]] = []

        for chosen in files:
            if len(enqueued) >= _MAX_PARALLEL:
                break
            if chosen.size == 0 or chosen.bitrate == 0:
                continue

            dl_id = self.slskd.enqueue(chosen.username, chosen.filename)
            if dl_id:
                enqueued.append((chosen.username, chosen.filename))
            else:
                completed = self._find_completed(chosen)
                if completed:
                    result = organize_file(completed, self.music_dir)
                    if result:
                        return (True, [])

        if enqueued:
            log.info("Soulseek: enqueued %d candidates for: %s", len(enqueued), query)
            return (False, enqueued)

        return (None, [])

    def _check_downloads(self, item) -> Optional[Path]:
        """Check if any previously enqueued download completed."""
        # Parse the stored meta from the queue item
        try:
            meta = json.loads(item.error) if item.error else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}

        pending = meta.get("pending", [])

        for username, filename in pending:
            local = self._find_local_path(username, filename)
            if local:
                log.info("Download completed: %s → organising", local)
                return organize_file(local, self.music_dir)

        # Check if any downloads errored / aborted for this query
        downloads = self.slskd.get_downloads()
        still_waiting = False
        all_failed = True

        for username, filename in pending:
            for d in downloads:
                if d.username == username and d.filename == filename:
                    if "Completed" in d.state and "Aborted" not in d.state:
                        local = self._find_local_path(username, filename)
                        if local:
                            return organize_file(local, self.music_dir)
                    if d.state in ("Queued", "InProgress", "Requested") or "Locally" in d.state:
                        still_waiting = True
                        all_failed = False
                    elif "Aborted" in d.state or d.state in ("Errored", "Cancelled"):
                        continue  # this one failed, check others

        if still_waiting:
            log.info("Downloads still in progress, will check next run")
            return None  # still processing

        if all_failed:
            log.warning("All Soulseek downloads failed, trying yt-dlp")
            # Try yt-dlp now instead of waiting for re-queue
            return organize_file(self._ytdlp_download(item), self.music_dir) if item else None

    def _ytdlp_download(self, item) -> Optional[Path]:
        """Try to download a single item via yt-dlp."""
        if not self.ytdlp_fallback:
            return None
        try:
            from .ytdlp_downloader import search_and_download
            return search_and_download(item.query, self.ytdlp_dir)
        except Exception as e:
            log.warning("yt-dlp failed for %s: %s", item.query, e)
            return None

    def _ytdlp_fallback(self, query: str) -> Optional[bool]:
        """Try yt-dlp as last resort. Returns True if successful."""
        if not self.ytdlp_fallback:
            return None
        log.info("yt-dlp fallback for: %s", query)
        try:
            from .ytdlp_downloader import search_and_download
            yt_file = search_and_download(query, self.ytdlp_dir)
            if yt_file:
                result = organize_file(yt_file, self.music_dir)
                return bool(result)
            log.warning("yt-dlp returned nothing")
        except Exception as e:
            log.warning("yt-dlp failed: %s", e)
        return None

    def _find_completed(self, file: SlskdFile) -> Optional[Path]:
        """Check if a file is already downloaded."""
        return self._find_local_path(file.username, file.filename)

    def _find_local_path(self, username: str, filename: str) -> Optional[Path]:
        """Locate a downloaded file in slskd's download directory."""
        username = username  # slskd saves under this
        candidate = self.download_dir / username / filename.lstrip("/")
        if candidate.exists():
            return candidate
        name = Path(filename).name
        for p in self.download_dir.rglob(name):
            return p
        return None

    def _score(self, f: SlskdFile) -> float:
        if f.size == 0 or f.bitrate == 0:
            return -1000
        fmt_score = _FORMAT_PREFERENCE.get(Path(f.filename).suffix.lower(), -1)
        bitrate_score = min(f.bitrate / 320.0, 2.0)
        slot_bonus = 5.0 if f.slot_free else 0.0
        queue_penalty = min(f.queue_length / 50.0, 1.0)
        speed_bonus = min(f.upload_speed / 1_000_000.0, 2.0)
        return (fmt_score * 100 + bitrate_score * 10 + slot_bonus
                - queue_penalty * 3 + speed_bonus)

    def _pick_best(self, files: list[SlskdFile], query: str) -> Optional[SlskdFile]:
        """Pick the best file (used by tests)."""
        if not files:
            return None
        return max(files, key=lambda f: self._score(f))
