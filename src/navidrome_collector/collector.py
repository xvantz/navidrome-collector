"""Main pipeline: orchestrates search → download → tag → organize."""

import json
import logging
import re
from pathlib import Path
from typing import Optional

from .logger import stage_logger, timed
from .queue import Queue
from .slskd_client import SlskdClient, SlskdFile
from .tagger import TrackMeta, read_tags
from .organizer import organize as organize_file
from .matcher import parse_query, extract_track_title, title_similarity

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

    def process_queue(self, max_items: int = 0) -> dict:
        """Process pending items from the queue.

        1. First checks in-progress downloads (started on earlier runs)
        2. Then starts new pending items

        Returns dict with:
            processed, succeeded, failed — counts
            items — list of completed items with details for notifications
        """
        from .notifier import format_track_summary
        stats = {"processed": 0, "succeeded": 0, "failed": 0, "items": []}
        log.info("process_queue: checking %d processing item(s)...",
                 len(self.queue.list_items(status="processing")))

        # 1. Check previously enqueued downloads
        for item in self.queue.list_items(status="processing"):
            clog = stage_logger(__name__, stage="download", item_id=item.id)
            clog.info("checking %s", item.query)
            result_path = self._check_downloads(item)
            if result_path:
                self.queue.mark_done(item.id, str(result_path))
                stats["succeeded"] += 1
                clog.info("download complete → %s", result_path)
                stats["items"].append({
                    "id": item.id,
                    "query": item.query,
                    "status": "done",
                    "file_path": str(result_path),
                    "source": "Soulseek",
                })
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
            clog = stage_logger(__name__, stage="search", item_id=item.id)
            clog.info("starting: %s", item.query)
            try:
                result, enqueued = self._start_downloads(item.query, artist=item.artist, title=item.title)
                if result is True:
                    self.queue.mark_done(item.id, "")
                    stats["succeeded"] += 1
                    clog.info("done ✓")
                    stats["items"].append({
                        "id": item.id,
                        "query": item.query,
                        "status": "done",
                        "file_path": "",
                        "source": "yt-dlp",
                    })
                elif result is None:
                    self.queue.mark_failed(item.id, "No source available")
                    stats["failed"] += 1
                    clog.warning("no source found")
                else:
                    # result is False = enqueued, waiting for later check
                    self.queue.mark_processing(item.id, enqueued)
                    # don't count in processed/failed — it's pending
                    stats["processed"] -= 1
                    clog.info("enqueued %d Soulseek download(s), will check later", len(enqueued))
            except Exception as e:
                log.exception("Failed to process item %d: %s", item.id, e)
                self.queue.mark_failed(item.id, str(e))
                stats["failed"] += 1

        return stats

    def _start_downloads(self, query: str, artist: str | None = None, title: str | None = None) -> tuple[Optional[bool], list]:
        """Try yt-dlp and Soulseek, compare scores, pick best.

        Uses artist/title for relevance scoring if available;
        falls back to parsing 'Artist - Title' from query.

        Returns:
            (True, [])  → download complete (file organised)
            (False, enqueued) → Soulseek enqueued, waiting
            (None, [])  → nothing found at all
        """
        # Parse query for artist/title if not explicit
        if not artist or not title:
            parsed_artist, parsed_title = parse_query(query)
            artist = artist or parsed_artist
            title = title or parsed_title

        # Step 1: yt-dlp — fast download, score the result
        yt_path: Optional[Path] = None
        yt_info: Optional[dict] = None
        yt_score = 0
        if self.ytdlp_fallback:
            clog = stage_logger(__name__, stage="ytdlp")
            clog.info("trying yt-dlp: %s", query)
            with timed(clog, "yt-dlp download"):
                from .ytdlp_downloader import search_and_download
                yt_path, yt_info = search_and_download(query, self.ytdlp_dir,
                                                      expected_artist=artist, expected_title=title)
            if yt_path:
                from .matcher import score_ytdlp
                yt_score = score_ytdlp(yt_info, yt_path)
                clog.info("yt-dlp result: %s (score=%d)", yt_path.name, yt_score)

        # Step 2: Soulseek search
        clog = stage_logger(__name__, stage="slskd")
        clog.info("Soulseek search: %s", query)
        with timed(clog, "Soulseek search"):
            files = self.slskd.search(query)

        if not files:
            if yt_path:
                clog.info("no Soulseek results, using yt-dlp")
                result = organize_file(yt_path, self.music_dir)
                return (bool(result), [])
            clog.warning("no results from any source: %s", query)
            return (None, [])

        # Score and sort Soulseek with relevance bonus
        for f in files:
            base_score = self._score(f)
            if title:
                f_title = extract_track_title(f.filename)
                sim = title_similarity(title, f_title)
                relevance_bonus = sim * 150
                mismatch_penalty = -400 if (sim < 0.3 and len(title) > 2) else 0
                f._score = base_score + relevance_bonus + mismatch_penalty
                f._title = f_title
                f._similarity = sim
            else:
                f._score = base_score
                f._title = ""
                f._similarity = 0.0

        files.sort(key=lambda f: f._score, reverse=True)
        best_slskd = files[0]
        clog.info("found %d file(s), scoring + relevance...", len(files))
        clog.debug("best Soulseek: %s (%s, score=%.0f, sim=%.2f)",
                   Path(best_slskd.filename).name, best_slskd.username,
                   best_slskd._score, best_slskd._similarity)

        # Step 3: Compare — pick the best source
        if yt_path and yt_score >= best_slskd._score:
            clog.info("yt-dlp (score=%d) >= best Soulseek (%.0f), using yt-dlp",
                      yt_score, best_slskd._score)
            result = organize_file(yt_path, self.music_dir)
            return (bool(result), [])

        if yt_path:
            clog.info("Soulseek (%.0f) beats yt-dlp (%d), enqueuing Soulseek",
                      best_slskd._score, yt_score)
            # yt-dlp temp file will be cleaned up on next run
        else:
            clog.info("yt-dlp failed, using Soulseek")

        # Enqueue Soulseek candidates
        enqueued: list[tuple[str, str]] = []
        for chosen in files:
            if len(enqueued) >= _MAX_PARALLEL:
                break
            if chosen.size == 0 or chosen.bitrate == 0:
                continue

            with timed(clog, "enqueue"):
                dl_id = self.slskd.enqueue(chosen.username, chosen.filename, chosen.size)
            if dl_id:
                enqueued.append((chosen.username, chosen.filename))
                clog.info("enqueued from %s: %s (%d kbps, sim=%.2f)", chosen.username,
                          getattr(chosen, '_title', Path(chosen.filename).name),
                          chosen.bitrate, getattr(chosen, '_similarity', 0))
            else:
                completed = self._find_completed(chosen)
                if completed:
                    result = organize_file(completed, self.music_dir)
                    if result:
                        return (True, [])

        if enqueued:
            clog.info("enqueued %d candidate(s) for: %s", len(enqueued), query)
            return (False, enqueued)

        # Nothing enqueued — use yt-dlp as last resort if we have it
        if yt_path:
            clog.info("Soulseek enqueue all failed, falling back to yt-dlp")
            result = organize_file(yt_path, self.music_dir)
            return (bool(result), [])

        clog.warning("no candidates could be enqueued: %s", query)
        return (None, [])

    def _check_downloads(self, item) -> Optional[Path]:
        """Check if any previously enqueued download completed."""
        clog = stage_logger(__name__, stage="download", item_id=item.id)
        # Parse the stored meta from the queue item
        try:
            meta = json.loads(item.error) if item.error else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}

        pending = meta.get("pending", [])
        clog.debug("checking %d pending download(s)", len(pending))

        for username, filename in pending:
            local = self._find_local_path(username, filename)
            if local:
                clog.info("found completed: %s → organising", local)
                return organize_file(local, self.music_dir)

        # Check if any downloads errored / aborted for this query
        with timed(clog, "get_downloads"):
            downloads = self.slskd.get_downloads()
        still_waiting = False
        all_failed = True
        found_any = False

        for username, filename in pending:
            # Normalise Soulseek @@ prefixes for matching with slskd download state
            norm_fn = re.sub(r"^@+", "", filename)
            for d in downloads:
                if d.username == username and d.filename == norm_fn:
                    found_any = True
                    if "Completed" in d.state and "Aborted" not in d.state:
                        local = self._find_local_path(username, filename)
                        if local:
                            clog.info("completed in slskd state: %s", d.state)
                            return organize_file(local, self.music_dir)
                        # Download completed but file not found at expected path
                        # Try harder: list the user's download directory
                        clog.debug("completed but not at expected path, scanning...")
                        user_dir = self.download_dir / username
                        if user_dir.exists():
                            for found_file in user_dir.rglob("*"):
                                if found_file.is_file() and found_file.stat().st_size > 0:
                                    # Found ANY file for this user — use it
                                    basename = Path(filename).name
                                    if basename.lower() in found_file.name.lower():
                                        clog.info("found via user dir scan: %s → %s",
                                                  basename, found_file)
                                        return organize_file(found_file, self.music_dir)
                            clog.debug("no files found in %s", user_dir)
                        else:
                            clog.debug("user dir does not exist: %s", user_dir)
                        # Don't mark as failed — download completed, file exists
                        all_failed = False
                    if d.state in ("Queued", "InProgress", "Requested") or "Locally" in d.state:
                        still_waiting = True
                        all_failed = False
                    elif "Aborted" in d.state or d.state in ("Errored", "Cancelled"):
                        clog.debug("candidate failed: %s/%s → %s", username, filename, d.state)
                        continue  # this one failed, check others

        if still_waiting:
            clog.info("downloads still in progress, will check next run")
            return None  # still processing

        if not found_any:
            clog.warning("pending downloads not found in slskd state (%d downloads checked), "
                         "may have been dropped or queued differently", len(downloads))
            # Don't mark as failed yet — could be transient
            return None

        if all_failed:
            clog.warning("all Soulseek downloads failed, trying yt-dlp")
            # Try yt-dlp now instead of waiting for re-queue
            yt_result = self._ytdlp_download(item)
            if yt_result:
                return organize_file(yt_result, self.music_dir) if item else None
            return None

        clog.debug("no pending downloads match current slskd state")
        return None

    def _ytdlp_download(self, item) -> Optional[Path]:
        """Try to download a single item via yt-dlp. Returns file path or None."""
        if not self.ytdlp_fallback:
            return None
        clog = stage_logger(__name__, stage="ytdlp", item_id=item.id)
        clog.info("yt-dlp download: %s", item.query)
        from .matcher import parse_query
        exp_artist = item.artist or parse_query(item.query)[0]
        exp_title = item.title or parse_query(item.query)[1]
        with timed(clog, "yt-dlp download"):
            try:
                from .ytdlp_downloader import search_and_download
                path, _ = search_and_download(item.query, self.ytdlp_dir,
                                              expected_artist=exp_artist,
                                              expected_title=exp_title)
                return path
            except Exception as e:
                clog.warning("yt-dlp failed: %s", e)
                return None

    def _ytdlp_fallback(self, query: str) -> Optional[bool]:
        """Try yt-dlp as last resort. Returns True if successful."""
        if not self.ytdlp_fallback:
            return None
        clog = stage_logger(__name__, stage="ytdlp")
        clog.info("yt-dlp fallback for: %s", query)
        from .matcher import parse_query
        exp_artist, exp_title = parse_query(query)
        with timed(clog, "yt-dlp fallback"):
            try:
                from .ytdlp_downloader import search_and_download
                path, _ = search_and_download(query, self.ytdlp_dir,
                                              expected_artist=exp_artist,
                                              expected_title=exp_title)
                if path:
                    result = organize_file(path, self.music_dir)
                    if result:
                        return bool(result)
                clog.warning("yt-dlp returned nothing")
            except Exception as e:
                clog.warning("yt-dlp failed: %s", e)
        return None

    def _find_completed(self, file: SlskdFile) -> Optional[Path]:
        """Check if a file is already downloaded."""
        return self._find_local_path(file.username, file.filename)

    def _find_local_path(self, username: str, filename: str) -> Optional[Path]:
        """Locate a downloaded file in slskd's download directory.

        slskd reports filenames with Windows backslashes even on Linux,
        but saves files with native separators. Handle both.
        Also strips Soulseek @username prefixes from paths.
        """
        clog = stage_logger(__name__, stage="find_file")
        # Strip Soulseek @-prefixed share identifiers (e.g., @@loeih -> loeih)
        clean_fn = re.sub(r"^@+", "", filename)

        # Normalise backslashes to native separators
        native_name = clean_fn.replace("\\", "/")
        candidate = self.download_dir / username / native_name.lstrip("/")
        clog.debug("trying: %s", candidate)
        if candidate.exists():
            clog.info("found: %s", candidate)
            return candidate
        # Also try with original backslashes
        candidate_bs = self.download_dir / username / filename.lstrip("/")
        clog.debug("trying: %s", candidate_bs)
        if candidate_bs.exists():
            clog.info("found (bs): %s", candidate_bs)
            return candidate_bs
        # Try without username subdir (flat structure)
        flat = self.download_dir / Path(filename).name
        clog.debug("trying flat: %s", flat)
        if flat.exists():
            return flat
        # Last resort: search by filename only (expensive but thorough)
        name = Path(filename).name
        clog.debug("rglob searching for: %s in %s", name, self.download_dir)
        for p in self.download_dir.rglob(name):
            if p.is_file():
                clog.info("found via rglob: %s", p)
                return p
        clog.warning("not found: %s/%s (tried %d paths)", username, name,
                     sum(1 for _ in self.download_dir.rglob(name)) if False else 3)
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
