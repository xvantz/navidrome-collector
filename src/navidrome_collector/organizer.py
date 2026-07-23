"""File organization: sorts downloaded tracks into Navidrome's music library."""

import logging
import shutil
from pathlib import Path
from typing import Optional

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
    source = Path(source_path)
    if not source.exists():
        log.error("Source file not found: %s", source)
        return None

    # Read or fetch metadata
    if meta is None:
        meta = read_tags(source)

    if not meta.has_tags:
        log.info("No tags found for %s, trying AcoustID fingerprint...", source)
        fp_meta = fingerprint(source)
        if fp_meta and fp_meta.has_tags:
            meta = fp_meta
        else:
            # Last resort: use filename as title
            meta = TrackMeta(
                title=source.stem,
                artist="Unknown",
                album="Unknown Album",
                has_tags=True,
            )

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

    log.info("Organized: %s → %s", source, dest)
    return dest
