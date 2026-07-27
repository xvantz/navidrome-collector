"""Query parsing and title matching for relevance-based ranking.

Extracts artist/title from user queries, cleans up Soulseek filenames,
and computes title similarity to bias scoring toward relevant results.
"""

import re
from pathlib import Path


def parse_query(query: str) -> tuple[str | None, str | None]:
    """Parse 'Artist - Title' format from a raw query string.

    Returns (artist, title) if a separator is found, else (None, None).
    Handles dash, en-dash, em-dash, and pipe separators.
    """
    m = re.match(r"^\s*(.+?)\s*[-–—|]\s*(.+)\s*$", query)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def extract_track_title(filename: str) -> str:
    """Extract a clean track title from a Soulseek filename.

    Strips the filesystem path, leading track numbers, disc numbers,
    and known metadata noise in brackets/parentheses.

    Examples:
        "shared\\deezer\\Eminem\\04 - Lose Yourself.flac"  → "Lose Yourself"
        "1-01 Lose Yourself.m4a"                           → "Lose Yourself"
        "03 - 8 Mile [Eminem].mp3"                         → "8 Mile"
        "Eminem - 8 Mile (Official Video).mp3"             → "8 Mile"
    """
    # Strip Windows/Unix path prefix — Soulseek paths use backslashes
    filename = filename.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    name = Path(filename).stem

    # Strip leading disc.track: "1-01 ", "01-01 "
    name = re.sub(r"^\d+[-–—]\d+\s*", "", name)
    # Strip leading track number only if ≥2 digits: "01 ", "01. ", "01 - "
    name = re.sub(r"^\d{2,}[-\.\s]+", "", name)
    # Strip "01" right at start with no separator
    name = re.sub(r"^\d{2,}\s+", "", name)

    # Strip metadata in brackets/parentheses (case-insensitive)
    noise_patterns = [
        r"\(.*?(?:official|video|audio|lyric|lyrics?|visualizer|360|explicit|edit|remaster).*?\)",
        r"\[.*?(?:official|video|audio|lyric|lyrics?|visualizer|360|explicit|edit|remaster).*?\]",
        # Bitrate / format tags
        r"\(.*?\d{3,}\s*kbps.*?\)",
        r"\[.*?\d{3,}\s*kbps.*?\]",
        r"\(.*?(?:flac|mp3|m4a|opus|wav|aac).*?\)",
        r"\[.*?(?:flac|mp3|m4a|opus|wav|aac).*?\]",
        # Year tags (4 digits)
        r"\(.*?\d{4}.*?\)",
        r"\[.*?\d{4}.*?\]",
        # Feat / featuring — keep the title but strip the extra
        r"\s*\(feat\..*?\)",
        r"\s*\[feat\..*?\]",
    ]
    for pat in noise_patterns:
        name = re.sub(pat, "", name, flags=re.IGNORECASE)

    return name.strip().strip("-\"' ")


def _tokenize(text: str) -> set[str]:
    """Split text into lowercase word tokens (letters, digits, apostrophes)."""
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def title_similarity(expected: str, actual: str) -> float:
    """Jaccard similarity between two title strings.

    Returns 0.0 (no common tokens) to 1.0 (identical token sets).
    Empty or single-character tokens are ignored to avoid false matches
    from common words like 'the', 'a', 'my'.
    """
    exp_tokens = {t for t in _tokenize(expected) if len(t) > 1}
    act_tokens = {t for t in _tokenize(actual) if len(t) > 1}

    if not exp_tokens or not act_tokens:
        return 0.0

    intersection = exp_tokens & act_tokens
    union = exp_tokens | act_tokens
    return len(intersection) / len(union)


# ── Scoring for download sources ──────────────────────────

_FORMAT_PREFERENCE = {
    ".flac": 5,
    ".wav":  4,
    ".m4a":  3,
    ".aac":  3,
    ".ogg":  2,
    ".opus": 3,   # opus at quality 0 is excellent — bump to m4a level
    ".mp3":  1,
    ".wma":  0,
}


def score_format(suffix: str) -> int:
    """Score a file format/container. Higher = better quality potential."""
    return _FORMAT_PREFERENCE.get(suffix.lower(), -1)


def score_ytdlp(yt_info: dict | None, file_path: str | Path | None = None) -> int:
    """Score a yt-dlp download result.

    Uses actual bitrate from mutagen when available, falls back to
    format-based estimate.
    """
    if not yt_info and not file_path:
        return 0

    if file_path:
        p = Path(file_path) if isinstance(file_path, str) else file_path
        ext = p.suffix.lower()
    else:
        ext = ".opus"  # default yt-dlp output

    fmt_score = score_format(ext)
    bitrate = yt_info.get("bitrate", 0) if yt_info else 0
    bitrate_score = min(bitrate / 320_000, 2.0) if bitrate else 1.0

    # yt-dlp has no queue/slot issues, but isn't lossless
    score = fmt_score * 100 + bitrate_score * 10

    return int(score)
