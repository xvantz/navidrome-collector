"""File organization: sorts downloaded tracks into Navidrome's music library."""

import logging
import shutil
import time
from pathlib import Path
from typing import Optional

from .logger import stage_logger
from .tagger import TrackMeta, read_tags, fingerprint

log = logging.getLogger(__name__)

# Characters forbidden in filesystem paths
_FORBIDDEN = '\\/:*?"<>|'


def sanitize(name: str) -> str:
    """Remove characters that are invalid in file/folder names."""
    return "".join(c if c not in _FORBIDDEN else "_" for c in name).strip()


def organize(
    source_path: str | Path,
    music_dir: str | Path,
    meta: Optional[TrackMeta] = None,
) -> Optional[Path]:
    """Read tags from file, build destination path, move file.

    Path scheme:
        /srv/music/{Artist}/{Album} ({Year})/{TrackNumber} - {Title}.{ext}

    If metadata is missing, tries AcoustID fingerprint as fallback.

    Returns the destination path, or None if the source file is missing.
    """
    start = time.monotonic()
    clog = stage_logger(__name__, stage="organize")
    source = Path(source_path)
    if not source.exists():
        log.error("Source file not found: %s", source)
        return None

    clog.info("organising: %s (%.1f MB)", source.name, source.stat().st_size / 1_048_576)

    # Read or fetch metadata
    if meta is None:
        meta = read_tags(source)

    original_album = meta.album or ""

    if not meta.has_tags:
        log.info("No tags found for %s, trying AcoustID fingerprint...", source)
        fp_meta = fingerprint(source)
        if fp_meta and fp_meta.has_tags:
            meta = fp_meta
            clog.info("AcoustID matched: %s - %s", meta.artist, meta.title)
        else:
            # Last resort: use filename as title
            meta = TrackMeta(
                title=source.stem,
                artist="Unknown",
                album="Unknown Album",
                has_tags=True,
            )
            clog.info("using filename as title: %s", source.stem)

    artist = sanitize(meta.artist or "Unknown")
    album = sanitize(meta.album or "Unknown Album")
    if meta.year:
        album = f"{album} ({meta.year})"
    track = meta.track_number or ""

    title = sanitize(meta.title or source.stem)
    ext = source.suffix.lower()

    # Build filename
    if track:
        filename = f"{int(track):02d} - {title}{ext}"
    else:
        filename = f"{title}{ext}"

    dest = Path(music_dir) / artist / album / filename
    dest.parent.mkdir(parents=True, exist_ok=True)

    clog.info("destination: %s", dest)

    # Move (or copy+remove for cross-fs safety)
    if dest.exists():
        log.warning("Destination exists, overwriting: %s", dest)

    try:
        shutil.copy2(source, dest)
        source.unlink()  # Remove original from downloads dir
    except (OSError, shutil.Error) as e:
        log.error("Failed to move %s to %s: %s", source, dest, e)
        return None

    # Write tags to the new location
    from .tagger import write_tags
    write_tags(dest, meta)

    elapsed = time.monotonic() - start
    clog.info("organised → %s (%.1fs)", dest, elapsed)

    # Post-process: enrich with cover art, lyrics, genre
    # Only enrich if music dir looks like a real library path (not test/temp)
    music_path = str(Path(music_dir).resolve())
    if not music_path.startswith("/tmp"):
        import os
        old_music_dir = os.environ.get("NVC_MUSIC_DIR")
        os.environ["NVC_MUSIC_DIR"] = music_path
        from .enricher import enrich
        enrich(dest, meta)
        if old_music_dir:
            os.environ["NVC_MUSIC_DIR"] = old_music_dir
        else:
            del os.environ["NVC_MUSIC_DIR"]

    # If enrichment changed the album, move file to correct path
    if meta.album and meta.album != original_album and "Unknown" not in meta.album:
        org_artist = sanitize(meta.artist or "Unknown")
        org_album = sanitize(meta.album or "Unknown Album")
        if meta.year and meta.year not in org_album:
            org_album = f"{org_album} ({meta.year})"
        org_track = meta.track_number or ""
        org_title = sanitize(meta.title or source.stem)
        org_ext = dest.suffix.lower()
        org_name = f"{int(org_track):02d} - {org_title}{org_ext}" if org_track else f"{org_title}{org_ext}"
        correct_dest = Path(music_dir) / org_artist / org_album / org_name

        if correct_dest != dest:
            correct_dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(dest), str(correct_dest))
                clog.info("re-organised → %s", correct_dest)
                return correct_dest
            except (OSError, shutil.Error) as e:
                log.warning("failed to re-organise: %s", e)

    return dest
