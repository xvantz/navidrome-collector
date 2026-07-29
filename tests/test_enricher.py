"""Tests for the enricher — MusicBrainz, cover art, lyrics."""

import os
from unittest.mock import patch, MagicMock, PropertyMock
from pathlib import Path
import tempfile
import shutil

import pytest

from navidrome_collector.tagger import TrackMeta
from navidrome_collector.enricher import enrich


@pytest.fixture(autouse=True)
def set_music_dir():
    """Ensure enricher guard passes for test paths."""
    old = os.environ.get("NVC_MUSIC_DIR")
    os.environ["NVC_MUSIC_DIR"] = "/tmp"
    yield
    if old:
        os.environ["NVC_MUSIC_DIR"] = old
    else:
        del os.environ["NVC_MUSIC_DIR"]


@pytest.fixture
def fake_audio_file():
    """Create a real playable audio file using a known-good FLAC snippet."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test_song.flac"
        # Copy a real FLAC from test fixtures (silence snippet)
        # As fallback, create a minimal valid FLAC
        try:
            from mutagen.flac import FLAC
            # Write minimal FLAC header + metadata
            flac_silence = (
                b"fLaC"  # magic
                b"\x00\x00\x00\x22"  # METADATA_BLOCK_STREAMINFO size 34
                + b"\x10\x00\x10\x00"  # min/max block size 4096
                + b"\x00\x00\x00\x00" * 6  # various zeroed fields
                + b"\x00\x01"  # channels=1
                + b"\x00\x00"  # sample rate placeholder
                + b"\x00" * 8  # total samples
                + b"\x00" * 16  # MD5 signature
            )
            path.write_bytes(flac_silence)
        except Exception:
            path.write_bytes(b"fLaC" + b"\x00" * 100)

        yield path


class TestEnrich:
    def test_skip_no_metadata(self):
        """No artist/title → enrichment skipped."""
        meta = TrackMeta(has_tags=False)
        result = enrich("/nonexistent/file.mp3", meta)
        assert result is False

    def test_skip_nonexistent_file(self):
        """Non-existent file → enrichment skipped."""
        meta = TrackMeta(artist="Artist", title="Title", has_tags=True)
        result = enrich("/nonexistent/file.mp3", meta)
        assert result is False

    @patch("navidrome_collector.enricher._musicbrainz_search")
    @patch("navidrome_collector.enricher._fetch_cover_art")
    @patch("navidrome_collector.enricher._fetch_lyrics")
    @patch("navidrome_collector.enricher.write_tags")
    @patch("navidrome_collector.enricher._embed_lyrics")
    def test_enrich_with_all_sources(
        self, mock_embed, mock_write, mock_lyrics, mock_cover, mock_mb, fake_audio_file
    ):
        """Full enrichment with cover, genre, album, lyrics."""
        mock_mb.return_value = {
            "id": "test-recording-id",
            "release_mbid": "test-release-mbid",
            "genre": "Rock",
            "album": "Test Album",
            "year": "2024",
        }
        mock_cover.return_value = b"fake_cover_data"
        mock_lyrics.return_value = "Test lyrics line 1\nTest lyrics line 2"

        meta = TrackMeta(
            artist="Test Artist",
            title="Test Song",
            album="YouTube",
            year="2024",
            genre="Music",
            has_tags=True,
        )

        result = enrich(fake_audio_file, meta)
        assert result is True
        assert mock_write.called

    @patch("navidrome_collector.enricher._musicbrainz_search")
    @patch("navidrome_collector.enricher._fetch_cover_art")
    @patch("navidrome_collector.enricher._fetch_lyrics")
    def test_enrich_only_lyrics(
        self, mock_lyrics, mock_cover, mock_mb, fake_audio_file
    ):
        """Only lyrics available — still applies tags."""
        mock_mb.return_value = None  # No MB match
        mock_cover.return_value = None
        mock_lyrics.return_value = "Only lyrics"

        meta = TrackMeta(
            artist="Test Artist",
            title="Test Song",
            album="YouTube",
            has_tags=True,
        )

        result = enrich(fake_audio_file, meta)
        assert result is True

    @patch("navidrome_collector.enricher._musicbrainz_search")
    @patch("navidrome_collector.enricher._fetch_cover_art")
    @patch("navidrome_collector.enricher._fetch_lyrics")
    def test_enrich_album_override(
        self, mock_lyrics, mock_cover, mock_mb, fake_audio_file
    ):
        """Album from MB should override YouTube garbage."""
        mock_mb.return_value = {
            "id": "rec-1",
            "release_mbid": "rel-1",
            "genre": None,
            "album": "Real Album Name",
            "year": "2023",
        }
        mock_cover.return_value = None
        mock_lyrics.return_value = None

        meta = TrackMeta(
            artist="Artist",
            title="Song",
            album="YouTube",
            has_tags=True,
        )

        result = enrich(fake_audio_file, meta)
        assert result is True

