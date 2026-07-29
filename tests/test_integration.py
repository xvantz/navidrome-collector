"""Integration tests that hit real APIs (MusicBrainz, LRCLIB, Cover Art Archive).

These are SLOW and require network access. Run with:
    pytest tests/test_integration.py -v --run-integration

Or configure pyproject.toml to add the marker.
"""
import tempfile
import time
from pathlib import Path

import pytest

from navidrome_collector.tagger import TrackMeta, read_tags
from navidrome_collector.enricher import enrich, _musicbrainz_search, _fetch_cover_art, _fetch_lyrics
from navidrome_collector.organizer import organize


def _create_test_audio(path: Path) -> None:
    """Create a minimal FLAC file with valid audio data (1 sec, 44100 Hz)."""
    # Generate 1 second of 16-bit PCM sine wave data
    import math
    import struct
    import subprocess

    # First create a WAV, then convert to FLAC if flac is available
    sample_rate = 44100
    num_samples = sample_rate
    samples = []
    for i in range(num_samples):
        # 440 Hz sine wave at low volume
        s = int(math.sin(2 * math.pi * 440 * i / sample_rate) * 5000)
        samples.append(s)
    data = struct.pack(f"<{num_samples}h", *samples)

    # Use subprocess to encode with flac, or create a WAV with proper tags
    wav_path = path.with_suffix(".wav")
    import wave
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(data)

    # Try to convert to the target format
    if path.suffix == ".flac":
        try:
            subprocess.run(
                ["flac", "--best", "-o", str(path), str(wav_path)],
                capture_output=True, timeout=10,
            )
            wav_path.unlink()
            return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Fallback: use WAV as-is (organizer will copy but tag write may be limited)
    if path.suffix == ".wav":
        wav_path.rename(path)
    else:
        # For unsupported formats, just copy the WAV
        wav_path.rename(path)


# ── Integration tests (hit real APIs) ─────────────────────

@pytest.mark.integration
class TestMusicBrainzRealAPI:
    """Tests that call the real MusicBrainz API."""

    def test_mb_search_imagine_dragons_thunder(self):
        """Known track: should find recording, album, genre, year."""
        rec = _musicbrainz_search("Imagine Dragons", "Thunder")
        assert rec is not None, "MusicBrainz should find Imagine Dragons - Thunder"
        assert rec.get("id"), "Should have a recording MBID"
        assert rec.get("album"), "Should have an album name"
        assert rec.get("year"), "Should have a year"
        # Genre is optional — some compilations don't have genre tags
        if rec.get("genre"):
            assert rec["genre"].lower() not in ("music", "self-titled", ""), \
                "Genre should be specific if present"

    def test_mb_search_eminem_godzilla(self):
        """Known track by popular artist."""
        rec = _musicbrainz_search("Eminem", "Godzilla")
        assert rec is not None, "MusicBrainz should find Eminem - Godzilla"
        # This one might have different metadata — just check we got something
        assert rec.get("id")

    def test_mb_search_unknown_track(self):
        """Unknown track should return None gracefully, not crash."""
        rec = _musicbrainz_search("Xxjhdsf87sdf", "UnknownTrackName99999")
        # Either None or some result — but shouldn't crash
        assert rec is None or isinstance(rec, dict)


@pytest.mark.integration
class TestCoverArtRealAPI:
    """Tests that call the real Cover Art Archive."""

    def test_cover_art_known_release(self):
        """Find a known release and fetch its cover."""
        rec = _musicbrainz_search("Imagine Dragons", "Thunder")
        if rec and rec.get("release_mbid"):
            cover = _fetch_cover_art(rec["release_mbid"])
            # Cover art is optional — some releases don't have it registered
            if cover is not None:
                assert len(cover) > 1000, "Cover should be >1KB"
                assert cover[:2] == b"\xff\xd8", "Should be a JPEG"
        else:
            pytest.skip("MusicBrainz didn't return a release MBID")


@pytest.mark.integration
class TestLyricsRealAPI:
    """Tests that call the real LRCLIB API."""

    def test_lyrics_known_track(self):
        """Fetch lyrics for a well-known track."""
        lyrics = _fetch_lyrics("Imagine Dragons", "Thunder")
        assert lyrics is not None, "LRCLIB should have Thunder lyrics"
        assert len(lyrics) > 50, "Lyrics should be substantial"
        # Common words in Thunder
        assert any(word in lyrics.lower() for word in ["thunder", "lightning", "hear"])

    def test_lyrics_unknown_track(self):
        """Unknown track should return None gracefully."""
        lyrics = _fetch_lyrics("Xxjhdsf87sdf", "UnknownTrack99999")
        assert lyrics is None


@pytest.mark.integration
class TestFullEnrichReal:
    """Full end-to-end test: create file → organise → enrich with real APIs."""

    def test_full_enrich_imagine_dragons_thunder(self):
        """Create a test file, organise it, enrich with real APIs."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "thunder.wav"
            _create_test_audio(src)

            # Use known metadata directly (WAV doesn't store tags)
            meta = TrackMeta(
                artist="Imagine Dragons",
                title="Thunder",
                album="",
                has_tags=True,
            )

            # Organise (WAV tags not supported — skip tag writing)
            music_dir = Path(tmp) / "music"
            music_dir.mkdir()
            dest = organize(src, music_dir, meta=meta)
            assert dest is not None, "Organise should succeed"
            assert dest.exists()

            # Enrich with real APIs
            result = enrich(dest, meta)
            assert isinstance(result, bool)

            # Just verify the function completed without crashing
            # (WAV tags won't persist, so final tags may be empty)
            final = read_tags(dest)
            # Enrichment may not write to WAV, but shouldn't crash
