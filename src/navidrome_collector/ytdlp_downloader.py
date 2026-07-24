"""YouTube audio downloader via yt-dlp (fallback when Soulseek fails).

Downloads best audio from YouTube and tags it with available metadata.
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from .tagger import TrackMeta, write_tags

log = logging.getLogger(__name__)

# Strip suffixes from YouTube channel names to get clean artist names
_CHANNEL_CLEANUP = re.compile(
    r"(?i)\s*(?:VEVO| - Topic|Official| - Official Channel| Music| Records| Entertainment| \d+)\s*$"
)
# Strip suffixes from video titles
_TITLE_CLEANUP = re.compile(
    r"(?i)\s*(?:\(.*?Official\s*(?:Music\s*)?Video.*?\)|\(.*?Audio.*?\)|\(.*?Lyrics?.*?\)|\(.*?Visualizer.*?\)|\(.*?360\s*RA[23]?.*?\)|\(.*?Explicit.*?\)|\[.*?M/V.*?\]|\[.*?Official.*?\])\s*$"
)


def search_and_download(query: str, output_dir: str | Path, max_duration: int = 600) -> Optional[Path]:
    """Search YouTube for the best audio match, download and tag it."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(output_dir / "%(id)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "-f", "bestaudio[ext!=webm]/bestaudio",
        "--max-filesize", "50M",
        "--match-filter", f"duration < {max_duration}",
        "--extract-audio",
        "--audio-format", "opus",
        "--audio-quality", "0",
        "--output", output_template,
        "--print", "after_move:filepath",
        "--print", "title",
        "--print", "channel",
        "--print", "uploader",
        "--print", "upload_date",
        "--print", "webpage_url",
        f"ytsearch:{query}",
    ]

    log.info("yt-dlp: searching %s", query)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        log.warning("yt-dlp timed out for: %s", query)
        return None
    except FileNotFoundError:
        log.warning("yt-dlp not found. Install with: nixpkgs.yt-dlp")
        return None
    except Exception as e:
        log.warning("yt-dlp failed for %s: %s", query, e)
        return None

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if stderr:
            log.warning("yt-dlp error: %s", stderr.split("\n")[-1])
        return None

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
        log.warning("yt-dlp: no audio file found in %s", output_dir)
        return None

    log.info("yt-dlp: downloaded %s (%.1f MB)", file_path.name, file_path.stat().st_size / 1_048_576)

    if metadata:
        _tag_file(file_path, metadata)
        log.info("yt-dlp: tagged with metadata from YouTube")

    return file_path


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


def _tag_file(path: Path, meta: dict) -> None:
    """Write YouTube metadata as audio tags."""
    yt_title = _clean_title(meta.get("title", ""))
    channel = meta.get("channel", "") or meta.get("uploader", "")
    date = meta.get("upload_date", "")
    year = date[:4] if date and len(date) >= 4 else ""

    if not yt_title and not channel:
        return

    # Parse "Artist - Title" from cleaned YouTube title
    artist = _clean_channel(channel)
    title = yt_title

    m = re.match(r"^(.*?)\s*[-–—|]\s*(.*)", yt_title)
    if m:
        candidate_artist = m.group(1).strip()
        candidate_title = m.group(2).strip()
        # Only use parsed artist if it's meaningfully different from channel
        if candidate_artist.lower() != artist.lower() and len(candidate_artist) > 1:
            artist = candidate_artist
        title = candidate_title

    track_meta = TrackMeta(
        artist=artist,
        title=title or yt_title,
        album="YouTube",
        year=year,
        genre="",
        track_number="",
        album_artist=artist,
        has_tags=True,
    )

    write_tags(str(path), track_meta)
