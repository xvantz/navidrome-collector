"""Post-download enrichment: cover art, lyrics, genre from public APIs.

MusicBrainz API:    https://musicbrainz.org/doc/MusicBrainz_API
Cover Art Archive:  https://wiki.musicbrainz.org/Cover_Art_Archive
LRCLIB:             https://lrclib.net/docs
"""

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .logger import stage_logger, timed
from .tagger import TrackMeta, write_tags

log = logging.getLogger(__name__)
enrich_log = stage_logger(__name__, stage="enrich")

_USER_AGENT = "NavidromeCollector/0.1.0 (xvantz)"
_MB_BASE = "https://musicbrainz.org/ws/2"
_CA_BASE = "https://coverartarchive.org"
_LRC_BASE = "https://lrclib.net/api"

_THUMB_SIZE = 250  # 250px thumbnail for cover art (smaller = faster)


# ── Public API ─────────────────────────────────────────────

def enrich(file_path: str | Path, meta: TrackMeta) -> bool:
    """Enrich a music file with cover art, lyrics, and genre.

    Only runs when artist + title are known.
    Returns True if any enrichment was applied.
    """
    if not meta.artist or not meta.title:
        return False

    file_path = Path(file_path)
    if not file_path.exists():
        return False

    # Skip enrichment for test/temp paths — no real metadata to enrich
    if "/tmp/" in str(file_path):
        return False

    enriched = False
    cover_data: Optional[bytes] = None
    new_genre: Optional[str] = None
    lyrics: Optional[str] = None

    # 1. MusicBrainz — get recording + release info
    recording = _musicbrainz_search(meta.artist, meta.title)
    if recording:
        enrich_log.debug("MusicBrainz match: %s (release=%s, genre=%s)",
                         recording.get("id", "?"),
                         recording.get("release_mbid", "?"),
                         recording.get("genre", "?"))

        # 2. Cover art
        release_mbid = recording.get("release_mbid")
        if release_mbid:
            with timed(enrich_log, "cover art"):
                cover_data = _fetch_cover_art(release_mbid)
                if cover_data:
                    enrich_log.info("cover art: %s (%.1f KB)", file_path.name,
                                    len(cover_data) / 1024)

        # 3. Genre
        new_genre = recording.get("genre")

        # 4. Album from MusicBrainz if missing
        if not meta.album or meta.album in ("Unknown Album", "Unknown", ""):
            mb_album = recording.get("album")
            if mb_album:
                mb_year = recording.get("year")
                meta.album = f"{mb_album} ({mb_year})" if mb_year else mb_album
                enrich_log.info("album: %s", meta.album)

    # 4. Lyrics (parallel to cover/genre - no deps)
    with timed(enrich_log, "lyrics"):
        lyrics = _fetch_lyrics(meta.artist, meta.title)
        if lyrics:
            enrich_log.info("lyrics: %s (%d chars)", file_path.name, len(lyrics))

    # Apply all enrichments in one write_tags call
    if cover_data or new_genre or lyrics:
        update_meta = TrackMeta(
            artist=meta.artist,
            title=meta.title,
            album=meta.album,
            year=meta.year,
            genre=new_genre or meta.genre,
            track_number=meta.track_number,
            album_artist=meta.album_artist,
            has_tags=True,
        )
        write_tags(str(file_path), update_meta, cover_data=cover_data)

        if lyrics:
            _embed_lyrics(file_path, lyrics)

        enrich_log.info("enriched: %s (cover=%s, genre=%s, lyrics=%s)",
                        file_path.name,
                        "yes" if cover_data else "no",
                        new_genre or "unchanged",
                        "yes" if lyrics else "no")
        enriched = True

    return enriched


# ── MusicBrainz ────────────────────────────────────────────

def _mb_request(path: str) -> Optional[dict]:
    """Make a MusicBrainz API request with rate-limiting."""
    url = f"{_MB_BASE}/{path}&fmt=json" if "?" in path else f"{_MB_BASE}/{path}?fmt=json"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        # MusicBrainz rate limit: 1 req/s
        time.sleep(1.0)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        enrich_log.debug("MusicBrainz request failed: %s", e)
        return None


def _musicbrainz_search(artist: str, title: str) -> Optional[dict]:
    """Search MusicBrainz for a recording by artist + title.

    Returns dict with keys: id, release_mbid, genre (or None).
    """
    query = urllib.parse.quote(f'artist:"{artist}" AND recording:"{title}"')
    data = _mb_request(f"/recording?query={query}&limit=5")
    if not data or not data.get("recordings"):
        # Fallback: broader search without quotes
        query = urllib.parse.quote(f"artist:{artist} recording:{title}")
        data = _mb_request(f"/recording?query={query}&limit=5")
    if not data or not data.get("recordings"):
        return None

    recording = data["recordings"][0]
    result: dict[str, Any] = {
        "id": recording.get("id"),
        "release_mbid": None,
        "genre": None,
        "album": None,
        "year": None,
    }

    # Get detailed info with releases and genres
    rid = recording.get("id", "")
    if rid:
        detail = _mb_request(f"/recording/{rid}?inc=releases+artists+genres+tags")
        if detail:
            enrich_log.debug("MusicBrainz detail: title=%s, %d release(s), %d tag(s)",
                             detail.get("title", "?"),
                             len(detail.get("releases", [])),
                             len(detail.get("tags", []) + detail.get("genres", [])))

            # Pick the first official release (case-insensitive) and extract album/year
            releases = detail.get("releases", [])
            for rel in releases:
                status = (rel.get("status") or "").lower()
                if status in ("official",):
                    result["release_mbid"] = rel.get("id")
                    result["album"] = rel.get("title", "")
                    result["year"] = (rel.get("date") or "")[:4]
                    enrich_log.debug("  official release: %s (%s, %s)",
                                     result["album"], result["release_mbid"], result["year"] or "?")
                    break
            if not result["release_mbid"] and releases:
                result["release_mbid"] = releases[0].get("id")
                result["album"] = releases[0].get("title", "")
                result["year"] = (releases[0].get("date") or "")[:4]
                enrich_log.debug("  fallback release: %s (%s, %s)",
                                 result["album"], result["release_mbid"], result["year"] or "?")

            # Genre — try genres field first (MB API v2), then tags
            genre_sources = (
                detail.get("genres", []) +
                detail.get("tags", [])
            )
            if genre_sources:
                genre_sources.sort(key=lambda t: t.get("count", 0), reverse=True)
                result["genre"] = genre_sources[0].get("name", "").capitalize()
                enrich_log.debug("  genre: %s", result["genre"])

    return result


# ── Cover Art Archive ─────────────────────────────────────

def _fetch_cover_art(release_mbid: str) -> Optional[bytes]:
    """Download cover art thumbnail from Cover Art Archive."""
    url = f"{_CA_BASE}/release/{release_mbid}/front-{_THUMB_SIZE}.jpg"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        # Try full-size if thumbnail not available
        if isinstance(e, urllib.error.HTTPError) and e.code == 404:
            url = f"{_CA_BASE}/release/{release_mbid}/front"
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.read()
            except (urllib.error.HTTPError, urllib.error.URLError):
                pass
        return None


# ── LRCLIB ─────────────────────────────────────────────────

def _fetch_lyrics(artist: str, title: str) -> Optional[str]:
    """Fetch lyrics from LRCLIB."""
    params = urllib.parse.urlencode({
        "artist_name": artist,
        "track_name": title,
    })
    url = f"{_LRC_BASE}/get?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if not data:
                return None
            # Prefer synced lyrics, fallback to plain
            lyrics = data.get("syncedLyrics") or data.get("plainLyrics")
            return lyrics or None
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        enrich_log.debug("LRCLIB request failed: %s", e)
        return None


# ── Lyrics embedding ──────────────────────────────────────

def _embed_lyrics(file_path: Path, lyrics: str) -> bool:
    """Embed lyrics as USLT (ID3) or lyrics tag (Vorbis)."""
    try:
        import mutagen
        audio = mutagen.File(str(file_path))
        if audio is None:
            return False
    except Exception as e:
        enrich_log.debug("cannot open for lyrics: %s", e)
        return False

    try:
        # MP3 — USLT frame
        if isinstance(audio, mutagen.mp3.MP3):
            from mutagen.id3 import USLT, Encoding
            audio.tags.add(USLT(
                encoding=Encoding.UTF8,
                lang="eng",
                desc="",
                text=lyrics,
            ))
        # FLAC — lyrics tag
        elif isinstance(audio, mutagen.flac.FLAC):
            audio["lyrics"] = lyrics
        # Ogg/Vorbis — lyrics tag
        elif hasattr(audio, "__class__") and "Ogg" in type(audio).__name__:
            audio["lyrics"] = lyrics
        # MP4/M4A — ©lyr atom
        elif isinstance(audio, mutagen.mp4.MP4):
            audio["©lyr"] = lyrics
        else:
            audio["lyrics"] = lyrics

        audio.save()
        return True
    except Exception as e:
        enrich_log.debug("failed to embed lyrics: %s", e)
        return False
