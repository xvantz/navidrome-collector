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

    # Only enrich files in the real music directory — skip test/temp paths
    import os
    music_dir_hint = os.environ.get("NVC_MUSIC_DIR", "/srv/music")
    if not str(file_path).startswith(music_dir_hint):
        return False

    enriched = False
    cover_data: Optional[bytes] = None
    new_genre: Optional[str] = None
    lyrics: Optional[str] = None
    original_album = meta.album

    # 1. MusicBrainz — get recording + release info
    # Clean up title: remove "Artist - " prefix if title contains artist
    search_title = meta.title
    search_artist = meta.artist
    if search_artist and search_title:
        prefix = f"{search_artist} - "
        if search_title.upper().startswith(prefix.upper()):
            search_title = search_title[len(prefix):].strip()
            enrich_log.debug("cleaned title: %s → %s", meta.title, search_title)
    recording = _musicbrainz_search(search_artist, search_title)
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

        # 3. Genre — always override if MusicBrainz has something specific
        new_genre = recording.get("genre")
        if new_genre and new_genre.lower() != "music":
            meta.genre = new_genre
            enrich_log.info("genre: %s", new_genre)

        # 4. Album/year from MusicBrainz — override yt-dlp garbage
        mb_album = recording.get("album")
        if mb_album and mb_album.lower() not in ("youtube", "unknown", ""):
            mb_year = recording.get("year")
            meta.album = f"{mb_album} ({mb_year})" if mb_year else mb_album
            enrich_log.info("album: %s", meta.album)
        if recording.get("year"):
            meta.year = recording["year"]
            enrich_log.info("year: %s", meta.year)

    # 4. Lyrics (parallel to cover/genre - no deps)
    with timed(enrich_log, "lyrics"):
        lyrics = _fetch_lyrics(meta.artist, meta.title)
        if lyrics:
            enrich_log.info("lyrics: %s (%d chars)", file_path.name, len(lyrics))

    # Apply all enrichments in one write_tags call
    has_meta_update = bool(cover_data or new_genre or lyrics)
    # Also save if album/year were updated
    if not has_meta_update and recording:
        has_meta_update = (meta.album != original_album)

    if cover_data or new_genre or lyrics or has_meta_update:
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
    """Make a MusicBrainz API request with rate-limiting and retry."""
    clean_path = path.lstrip("/")
    url = f"{_MB_BASE}/{clean_path}&fmt=json" if "?" in clean_path else f"{_MB_BASE}/{clean_path}?fmt=json"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    last_error = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                if isinstance(data, dict) and "error" in data:
                    last_error = data["error"]
                    enrich_log.debug("MB error (attempt %d/3): %s", attempt + 1, last_error)
                    if "busy" in last_error.lower():
                        time.sleep(3.0)
                        continue
                    return None
                return data
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            last_error = str(e)
            enrich_log.debug("MB failed (attempt %d/3): %s", attempt + 1, last_error)
        # Rate limit: 1 req/s — sleep between attempts
        if attempt < 2:
            time.sleep(1.5)
    enrich_log.debug("MB request exhausted retries: %s", last_error)
    return None


def _musicbrainz_search(artist: str, title: str) -> Optional[dict]:
    """Search MusicBrainz for a recording by artist + title.

    Evaluates ALL returned recordings and picks the one whose best
    release has the highest type score (Album > Single > EP > Live).
    """
    query = urllib.parse.quote(f'artist:"{artist}" AND recording:"{title}"')
    data = _mb_request(f"/recording?query={query}&limit=10&inc=releases+release-groups")
    if not data or not data.get("recordings"):
        # Fallback: broader search without quotes
        query = urllib.parse.quote(f"artist:{artist} recording:{title}")
        data = _mb_request(f"/recording?query={query}&limit=10&inc=releases+release-groups")
    if not data or not data.get("recordings"):
        return None

    # Type preference (higher = better)
    _TYPE_SCORE = {
        "album": 10, "single": 6, "ep": 5, "mixtape": 3,
        "compilation": 2, "soundtrack": 2, "live": 1, "broadcast": 0,
    }
    # Secondary type penalties (subtracted from primary score)
    _SECONDARY_PENALTY = {
        "compilation": -5, "live": -5, "soundtrack": -4,
        "mixtape": -2, "remix": -3, "dj-mix": -3,
    }

    best_rec: Optional[dict] = None
    best_score = -1

    for rec in data["recordings"]:
        rec_id = rec.get("id", "")
        for rel in rec.get("releases", []):
            if (rel.get("status") or "").lower() not in ("official", ""):
                continue
            rg = rel.get("release-group", {}) or {}
            ptype = (rg.get("primary-type") or "").lower()
            score = _TYPE_SCORE.get(ptype, 0)
            # Penalise secondary types (e.g., Album+Compilation → -5)
            for stype in rg.get("secondary-types", []):
                score += _SECONDARY_PENALTY.get(stype.lower(), 0)
            if score > best_score:
                best_score = score
                best_rec = {
                    "id": rec_id,
                    "release_mbid": rel.get("id"),
                    "genre": None,
                    "album": rel.get("title", ""),
                    "year": (rel.get("date") or "")[:4],
                    "_type": ptype,
                }
                enrich_log.debug("  candidate: %s — %s (%s) [type=%s, score=%d]",
                                 rec.get("title", "?"), rel.get("title", "?"),
                                 (rel.get("date") or "")[:4], ptype, score)

    if not best_rec:
        return None

    # Fetch genre from recording detail (1 extra API call for the best match)
    rid = best_rec["id"]
    if rid:
        detail = _mb_request(f"/recording/{rid}?inc=genres+tags")
        if detail:
            genre_sources = detail.get("genres", []) + detail.get("tags", [])
            if genre_sources:
                genre_sources.sort(key=lambda t: t.get("count", 0), reverse=True)
                best_rec["genre"] = genre_sources[0].get("name", "").capitalize()

    enrich_log.info("MusicBrainz best: %s — %s (%s) [type=%s]",
                    artist, best_rec["album"], best_rec.get("year", "?"), best_rec.get("_type", "?"))
    return best_rec


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
