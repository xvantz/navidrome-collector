"""Audio metadata reader/writer.

Primary: mutagen (direct tag reading).
Fallback: pyacoustid + MusicBrainz (audio fingerprinting).
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import mutagen

log = logging.getLogger(__name__)


def get_extension(path: str | Path) -> str:
    p = Path(path)
    return p.suffix.lower()


# ── Tag reading ───────────────────────────────────────────

@dataclass
class TrackMeta:
    """Normalized track metadata."""
    artist: str = ""
    title: str = ""
    album: str = ""
    year: str = ""
    genre: str = ""
    track_number: str = ""
    album_artist: str = ""
    has_tags: bool = False


def read_tags(path: str | Path) -> TrackMeta:
    """Read metadata from an audio file using mutagen."""
    path = Path(path)
    meta = TrackMeta()

    if not path.exists():
        log.warning("File not found: %s", path)
        return meta

    try:
        audio = mutagen.File(str(path), easy=False)
        if audio is None:
            log.warning("Unrecognised format or corrupt file: %s", path)
            return meta
    except Exception as e:
        log.warning("Failed to open %s: %s", path, e)
        return meta

    try:
        # MP4/M4A uses different tag keys (©ART, ©nam, ©alb, etc.)
        if isinstance(audio, mutagen.mp4.MP4):
            meta.artist = _get_tag(audio, "©ART", "arti", "aART", "artist")
            meta.title = _get_tag(audio, "©nam", "titl", "title")
            meta.album = _get_tag(audio, "©alb", "albu", "album")
            meta.year = _get_year(audio)
            meta.genre = _get_tag(audio, "©gen", "genre")
            meta.track_number = _get_mp4_track(audio)
            meta.album_artist = _get_tag(audio, "aART", "©ART", "albumartist", "album artist")
        else:
            meta.artist = _get_tag(audio, "artist")
            meta.title = _get_tag(audio, "title")
            meta.album = _get_tag(audio, "album")
            meta.year = _get_year(audio)
            meta.genre = _get_tag(audio, "genre")
            meta.track_number = str(_get_tag(audio, "tracknumber")).split("/")[0]
            meta.album_artist = _get_tag(audio, "albumartist", "album artist")
        meta.has_tags = bool(meta.artist and meta.title)
    except Exception as e:
        log.warning("Error reading tags from %s: %s", path, e)

    return meta


def _get_tag(audio, *keys) -> str:
    """Get first non-empty tag value from multiple possible keys."""
    for key in keys:
        try:
            val = audio.get(key)
            if val is None:
                continue
            if isinstance(val, list):
                val = val[0]
            val = str(val).strip()
            if val:
                return val
        except (KeyError, IndexError, ValueError):
            continue
    return ""


def _get_year(audio) -> str:
    """Extract year from date or year tag.
    
    MP4/M4A uses ©day, other formats use date/year.
    """
    for key in ("year", "originaldate", "©day", "date"):
        val = _get_tag(audio, key)
        if val and len(val) >= 4:
            return val[:4]
    return ""


def _get_mp4_track(audio) -> str:
    """Extract track number from MP4/M4A tags (stored as tuple)."""
    try:
        val = audio.get("trkn")
        if val and isinstance(val, list) and len(val) > 0:
            track = val[0]
            if isinstance(track, tuple) and len(track) > 0:
                return str(track[0])
            return str(track)
    except (KeyError, IndexError, ValueError, TypeError):
        pass
    return ""


# ── Tag writing ───────────────────────────────────────────

def write_tags(path: str | Path, meta: TrackMeta, cover_data: bytes | None = None) -> bool:
    """Write metadata to an audio file. Returns True on success."""
    path = Path(path)
    try:
        audio = mutagen.File(str(path), easy=False)
        if audio is None:
            return False
    except Exception as e:
        log.warning("Cannot open %s for writing: %s", path, e)
        return False

    if isinstance(audio, mutagen.mp4.MP4):
        audio["©ART"] = meta.artist
        audio["©nam"] = meta.title
        audio["©alb"] = meta.album
        if meta.year:
            audio["©day"] = meta.year
        if meta.genre:
            audio["©gen"] = meta.genre
        if meta.track_number:
            try:
                audio["trkn"] = [(int(meta.track_number), 0)]
            except ValueError:
                pass
        if meta.album_artist:
            audio["aART"] = meta.album_artist
    else:
        audio["artist"] = meta.artist
        audio["title"] = meta.title
        audio["album"] = meta.album
        if meta.year:
            audio["date"] = meta.year
        if meta.genre:
            audio["genre"] = meta.genre
        if meta.track_number:
            audio["tracknumber"] = meta.track_number
        if meta.album_artist:
            audio["albumartist"] = meta.album_artist

    # Embed cover art
    if cover_data and _can_embed_cover(audio):
        _embed_cover(audio, cover_data)

    try:
        audio.save()
        return True
    except Exception as e:
        log.warning("Failed to save tags to %s: %s", path, e)
        return False


def _can_embed_cover(audio) -> bool:
    """Check if the format supports embedded images."""
    return isinstance(audio, (MP3, FLAC, OggOpus, OggVorbis))


def _embed_cover(audio, data: bytes) -> None:
    """Embed cover art into audio file."""
    try:
        if isinstance(audio, MP3):
            from mutagen.id3 import APIC
            audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=data))
        elif isinstance(audio, FLAC):
            pic = Picture()
            pic.data = data
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.width = 0
            pic.height = 0
            pic.depth = 0
            audio.add_picture(pic)
        elif isinstance(audio, (OggOpus, OggVorbis)):
            import base64
            audio["metadata_block_picture"] = _picture_block(data)
    except Exception as e:
        log.warning("Failed to embed cover: %s", e)


def _picture_block(data: bytes) -> str:
    """Create Ogg-flac picture block string."""
    pic = Picture()
    pic.data = data
    pic.type = 3
    pic.mime = "image/jpeg"
    pic.desc = "Cover"
    pic.width = 0
    pic.height = 0
    pic.depth = 0
    pic.colors = 0
    import base64
    return base64.b64encode(pic.write()).decode()


# ── AcoustID fingerprinting (fallback for bare files) ─────

def fingerprint(path: str | Path) -> Optional[TrackMeta]:
    """Try acoustid + MusicBrainz for metadata. Returns None if unavailable.

    Requires NVC_ACOUSTID_KEY env var (free at https://acoustid.org/login).
    Without it, fingerprinting is skipped.
    """
    import os
    api_key = os.environ.get("NVC_ACOUSTID_KEY", "")
    if not api_key:
        log.debug("NVC_ACOUSTID_KEY not set, skipping fingerprint")
        return None

    try:
        import acoustid
    except ImportError:
        log.debug("pyacoustid not installed, skipping fingerprint")
        return None

    path = Path(path)
    if not path.exists():
        return None

    try:
        duration, fp = acoustid.fingerprint_file(str(path))
        log.debug("fingerprint generated: %.1fs, %d bytes", duration, len(fp or ""))

        result = acoustid.lookup(api_key, fp, duration)
        if not result or "results" not in result:
            log.debug("AcoustID lookup returned no results")
            return None

        # Find the best match (highest score)
        best = None
        best_score = 0.0
        for entry in result["results"]:
            score = entry.get("score", 0)
            recordings = entry.get("recordings", [])
            if recordings and score > best_score:
                best_score = score
                best = recordings[0]

        if not best or best_score < 0.5:
            log.debug("AcoustID: best match score=%.2f too low", best_score)
            return None

        meta = TrackMeta(has_tags=True)
        meta.title = best.get("title", "")

        # Artist
        artists = best.get("artists", [])
        if artists:
            meta.artist = " / ".join(
                a.get("name", "") for a in artists if isinstance(a, dict)
            )

        # Album + year from release groups
        release_groups = best.get("releasegroups", [])
        if release_groups:
            rg = release_groups[0]
            meta.album = rg.get("title", "")
            if rg.get("year"):
                meta.year = str(rg["year"])

        log.info("AcoustID matched: %s - %s (%.0f%%)", meta.artist, meta.title, best_score * 100)
        return meta

    except Exception as e:
        log.debug("Fingerprint lookup failed: %s", e)
        return None


def _musicbrainz_lookup(recording_id: str) -> Optional[TrackMeta]:
    """Fetch metadata from MusicBrainz by recording MBID."""
    import json
    import urllib.request
    import urllib.error

    url = (
        f"https://musicbrainz.org/ws/2/recording/{recording_id}"
        "?fmt=json&inc=artists+releases"
    )
    headers = {"User-Agent": "NavidromeCollector/0.1.0 (xvantz)"}
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        log.debug("MusicBrainz lookup failed: %s", e)
        return None

    if not data:
        return None

    meta = TrackMeta()
    meta.title = data.get("title", "")
    meta.has_tags = True

    # Artist
    if data.get("artist-credit"):
        meta.artist = " / ".join(
            c.get("artist", {}).get("name", "")
            for c in data["artist-credit"]
            if isinstance(c, dict) and "artist" in c
        )
        meta.album_artist = meta.artist

    # Release info
    if data.get("releases"):
        release = data["releases"][0]
        meta.album = release.get("title", "")
        if release.get("date"):
            meta.year = release["date"][:4]

    return meta
