"""Main pipeline: orchestrates search → download → tag → organize."""

import logging
import time
from pathlib import Path
from typing import Optional

from .queue import Queue
from .slskd_client import SlskdClient, SlskdFile
from .tagger import TrackMeta, read_tags
from .organizer import organize as organize_file

log = logging.getLogger(__name__)

# Preference order for audio formats
_FORMAT_PREFERENCE = {
    ".flac": 5,
    ".wav":  4,
    ".m4a":  3,
    ".ogg":  2,
    ".opus": 2,
    ".mp3":  1,
    ".wma":  0,
}


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

        Args:
            max_items: Max items to process (0 = unlimited).

        Returns:
            dict with counts: processed, succeeded, failed.
        """
        stats = {"processed": 0, "succeeded": 0, "failed": 0}
        while True:
            item = self.queue.next_pending()
            if item is None:
                break
            if max_items and stats["processed"] >= max_items:
                break

            stats["processed"] += 1
            try:
                dest = self._process_item(item.query)
                if dest:
                    self.queue.mark_done(item.id, str(dest))
                    stats["succeeded"] += 1
                else:
                    self.queue.mark_failed(item.id, "No destination produced")
                    stats["failed"] += 1
            except Exception as e:
                log.exception("Failed to process item %d: %s", item.id, e)
                self.queue.mark_failed(item.id, str(e))
                stats["failed"] += 1

        return stats

    def _process_item(self, query: str) -> Optional[Path]:
        """Process a single query: search → pick → download → tag → organize."""
        # 1. Search on Soulseek
        log.info("Searching: %s", query)
        files = self.slskd.search(query)
        if not files:
            log.warning("No results for: %s", query)
            return None

        # 2. Try candidates in order until one downloads successfully
        files.sort(key=lambda f: self._score(f), reverse=True)
        errors = []

        for chosen in files[:10]:  # try top 10 candidates
            if chosen.size == 0 or chosen.bitrate == 0:
                continue  # skip files with no metadata

            log.info("Trying: %s from %s (%.0f kbps, %s)",
                     chosen.filename, chosen.username, chosen.bitrate,
                     Path(chosen.filename).suffix)

            # 3. Enqueue download
            download_id = self.slskd.enqueue(chosen.username, chosen.filename)
            if download_id is None:
                # Check actual state in slskd
                downloads = self.slskd.get_downloads()
                existing = None
                username, filename = chosen.username, chosen.filename
                for d in downloads:
                    if d.username == username and d.filename == filename:
                        existing = d
                        break

                if existing is None:
                    errors.append(f"{chosen.username}: enqueue failed (no download created)")
                    continue

                if existing.state == "Completed":
                    local = self._find_local(chosen)
                    if local:
                        return organize_file(local, self.music_dir)
                    errors.append(f"{chosen.username}: completed but file missing")
                    continue

                if "Aborted" in existing.state or existing.state in ("Errored", "Cancelled"):
                    errors.append(f"{chosen.username}: {existing.error or existing.state}")
                    continue

                # In-progress or queued — wait
                log.info("Download in progress (%s), waiting...", existing.state)
                result = self.slskd.wait_for_download(username, filename)
                if "Completed" in result.state and not "Aborted" in result.state:
                    local = self._find_local(chosen)
                    if local:
                        return organize_file(local, self.music_dir)
                errors.append(f"{chosen.username}: {result.error or result.state}")
                continue

            # 4. Wait for completion
            result = self.slskd.wait_for_download(chosen.username, chosen.filename)
            if "Completed" in result.state and not "Aborted" in result.state:
                local = self._find_local(chosen)
                if local:
                    return organize_file(local, self.music_dir)
                log.warning("Download completed but file not found: %s", chosen.filename)
                return None

            errors.append(f"{chosen.username}: {result.error or result.state}")
            # Clean up failed download
            if result.id:
                try:
                    self.slskd._delete(f"/transfers/downloads/{chosen.username}/{result.id}")
                except Exception:
                    pass
            continue

        log.warning("All candidates failed for: %s", query)
        for err in errors:
            log.warning("  - %s", err)

        # 3. Fallback: yt-dlp
        if self.ytdlp_fallback:
            log.info("Trying yt-dlp fallback for: %s", query)
            try:
                from .ytdlp_downloader import search_and_download
                yt_file = search_and_download(query, self.ytdlp_dir)
                if yt_file:
                    result = organize_file(yt_file, self.music_dir)
                    if result:
                        return result
                    log.warning("yt-dlp file downloaded but organize failed")
                else:
                    log.warning("yt-dlp returned nothing for: %s", query)
            except Exception as e:
                log.warning("yt-dlp fallback failed: %s", e)

        return None

    def _score(self, f: SlskdFile) -> float:
        """Score a single file for ranking (higher = better)."""
        # Files with missing size or bitrate are unusable — heavily penalise
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
        """Pick the best file from search results (used by tests)."""
        if not files:
            return None
        return max(files, key=lambda f: self._score(f))

    def _find_local(self, file: SlskdFile) -> Optional[Path]:
        """Locate a downloaded file in slskd's download directory.
        slskd saves files preserving their remote directory structure.
        """
        # slskd saves as {download_dir}/{username}/{filename}
        candidate = self.download_dir / file.username / file.filename.lstrip("/")
        if candidate.exists():
            return candidate

        # Fallback: search by filename
        name = Path(file.filename).name
        for p in self.download_dir.rglob(name):
            return p
        return None
