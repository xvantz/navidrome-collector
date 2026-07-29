"""YouTube audio downloader via yt-dlp (fallback when Soulseek fails).

Downloads best audio from YouTube and tags it with available metadata.
"""

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from .logger import stage_logger
from .tagger import TrackMeta, write_tags

log = logging.getLogger(__name__)
yt_log = stage_logger(__name__, stage="ytdlp")

# Strip suffixes from YouTube channel names to get clean artist names
_CHANNEL_CLEANUP = re.compile(
    r"(?i)\s*(?:VEVO| - Topic|Official| - Official Channel| Music| Records| Entertainment| \d+)\s*$"
)
# Strip suffixes from video titles
_TITLE_CLEANUP = re.compile(
    r"(?i)\s*(?:\(.*?Official\s*(?:Music\s*)?Video.*?\)|\(.*?Audio.*?\)|\(.*?Lyrics?.*?\)|\(.*?Visualizer.*?\)|\(.*?360\s*RA[23]?.*?\)|\(.*?Explicit.*?\)|\[.*?M/V.*?\]|\[.*?Official.*?\])\s*$"
)


def search_and_download(
    query: str,
    output_dir: str | Path,
    max_duration: int = 600,
    expected_artist: str | None = None,
    expected_title: str | None = None,
) -> tuple[Optional[Path], Optional[dict]]:
    """Search YouTube for the best audio match, download and tag it.

    Args:
        expected_artist, expected_title: known from search query — used as
            primary source for tags instead of parsing YouTube title.

    Returns:
        (filepath, info_dict) on success, (None, None) on failure.
        info_dict contains: title, channel, uploader, upload_date, url, bitrate
    """
    start = time.monotonic()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(output_dir / "%(id)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "-f", "bestaudio[ext=m4a]/bestaudio",
        "--max-filesize", "50M",
        "--match-filter", f"duration < {max_duration}",
        "--extract-audio",
        "--audio-format", "m4a",
        "--audio-quality", "0",
        "--add-metadata",
        "--embed-thumbnail",
        "--output", output_template,
        "--print", "after_move:filepath",
        "--print", "title",
        "--print", "channel",
        "--print", "uploader",
        "--print", "upload_date",
        "--print", "webpage_url",
        f"ytsearch:{query}",
    ]

    yt_log.info("searching: %s", query)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        yt_log.warning("timed out (120s): %s", query)
        return None, None
    except FileNotFoundError:
        yt_log.warning("yt-dlp not found (install nixpkgs.yt-dlp)")
        return None, None
    except Exception as e:
        yt_log.warning("subprocess error: %s", e)
        return None, None

    elapsed = time.monotonic() - start

    if result.returncode != 0:
        stderr = result.stderr.strip()
        err_msg = stderr.split("\n")[-1] if stderr else "unknown error"
        yt_log.warning("failed (%.1fs): %s", elapsed, err_msg)
        return None, None

    lines = [l.strip() for l in result.stdout.split("\n") if l.strip()]
    metadata = _parse_metadata(lines)

    # Locate the downloaded file
    file_path = None
    if lines and Path(lines[0]).exists():
        file_path = Path(lines[0])
    elif lines and (output_dir / Path(lines[0]).name).exists():
        file_path = output_dir / Path(lines[0]).name
    else:
        audio_files = sorted(
            list(output_dir.glob("*.[oO][pP][uU][sS]")) +
            list(output_dir.glob("*.[mM][44][aA]")) +
            list(output_dir.glob("*.[wW][aA][vV]")),
            key=lambda p: p.stat().st_size, reverse=True,
        )
        if audio_files:
            file_path = audio_files[0]

    if not file_path or not file_path.exists() or file_path.stat().st_size == 0:
        yt_log.warning("no audio file found after download (%.1fs)", elapsed)
        return None, None

    size_mb = file_path.stat().st_size / 1_048_576

    # Read actual bitrate from the downloaded file
    actual_bitrate = 0
    try:
        import mutagen
        af = mutagen.File(str(file_path))
        if af is not None:
            actual_bitrate = getattr(af.info, "bitrate", 0) or 0
    except Exception:
        pass

    if metadata:
        metadata["bitrate"] = actual_bitrate
        _tag_file(file_path, metadata, expected_artist=expected_artist, expected_title=expected_title)

    yt_log.info("downloaded: %s (%.1f MB, %d kbps) in %.1fs",
                file_path.name, size_mb, actual_bitrate // 1000, elapsed)

    return file_path, metadata


def _parse_metadata(lines: list[str]) -> Optional[dict]:
    """Parse yt-dlp --print output into a metadata dict.

    Lines: filepath, title, channel, uploader, upload_date, webpage_url
    """
    if len(lines) < 6:
        return None
    return {
        "title": lines[1] if len(lines) > 1 else "",
        "channel": lines[2] if len(lines) > 2 else "",
        "uploader": lines[3] if len(lines) > 3 else "",
        "upload_date": lines[4] if len(lines) > 4 else "",
        "url": lines[5] if len(lines) > 5 else "",
    }


def _clean_channel(channel: str) -> str:
    """Clean up YouTube channel name to get a proper artist name."""
    name = _CHANNEL_CLEANUP.sub("", channel).strip()
    # "EminemMusic" → "Eminem", "MilesDavisVEVO" → "Miles Davis"
    # Split camelCase and TitleCase
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    name = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return name.strip() or channel


def _clean_title(title: str) -> str:
    """Remove junk from YouTube video titles."""
    return _TITLE_CLEANUP.sub("", title).strip()


def _tag_file(path: Path, meta: dict, expected_artist: str | None = None, expected_title: str | None = None) -> None:
    """Write YouTube metadata as audio tags.

    Uses expected_artist/expected_title from the search query when available
    as the primary source, falling back to parsing the YouTube title.
    """
    yt_title = _clean_title(meta.get("title", ""))
    channel = meta.get("channel", "") or meta.get("uploader", "")
    date = meta.get("upload_date", "")
    year = date[:4] if date and len(date) >= 4 else ""

    if not yt_title and not channel:
        return

    # Use expected artist/title from search query (most reliable)
    artist = expected_artist or _clean_channel(channel)
    title = expected_title or yt_title

    # Parse "Artist - Title" from YouTube title if we still need artist/title
    if not expected_artist or not expected_title:
        m = re.match(r"^(.*?)\s*[-–—|]\s*(.*)", yt_title)
        if m:
            candidate_artist = m.group(1).strip()
            candidate_title = m.group(2).strip()
            if not expected_artist:
                if candidate_artist.lower() != artist.lower() and len(candidate_artist) > 1:
                    artist = candidate_artist
            if not expected_title:
                title = candidate_title

    # If parsing failed and title matches artist, use the raw YouTube title
    if title and artist and title.lower() == artist.lower():
        raw_title = meta.get("title", "")
        if raw_title and raw_title.lower() != artist.lower():
            title = raw_title
            yt_log.debug("fallback title: %s → %s", yt_title, title)

    track_meta = TrackMeta(
        artist=artist,
        title=title or yt_title,
        album="YouTube",
        year=year,  # "" if no upload_date, avoids garbage "http" from ffmpeg
        genre="",
        track_number="",
        album_artist=artist,
        has_tags=True,
    )

    write_tags(str(path), track_meta)

    # Clean up garbage date tags that ffmpeg may have written
    if not year:
        try:
            import mutagen
            af = mutagen.File(str(path))
            if af is not None:
                for garbage_key in ("date", "©day"):
                    if garbage_key in af:
                        del af[garbage_key]
                af.save()
        except Exception:
            pass
