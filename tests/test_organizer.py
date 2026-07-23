"""Tests for the organizer module."""

import tempfile
from pathlib import Path

import pytest

from navidrome_collector.organizer import sanitize, organize
from navidrome_collector.tagger import TrackMeta


class TestSanitize:
    def test_removes_forbidden_chars(self):
        assert sanitize('AC/DC') == 'AC_DC'
        assert sanitize('Foo:Bar') == 'Foo_Bar'
        assert sanitize('Test?Song*') == 'Test_Song_'

    def test_preserves_normal_names(self):
        assert sanitize('Metallica') == 'Metallica'
        assert sanitize('Master of Puppets') == 'Master of Puppets'
        assert sanitize('Nirvana') == 'Nirvana'

    def test_strips_whitespace(self):
        assert sanitize('  Artist  ') == 'Artist'


class TestOrganize:
    @pytest.fixture
    def music_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            yield Path(tmp)

    @pytest.fixture
    def source_file(self):
        """Create a dummy audio file with tags."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "test.mp3"
            # Just create an empty file — we test path logic, not tag I/O
            src.write_bytes(b"\x00" * 100)
            yield src

    def test_organize_with_metadata(self, music_dir, source_file):
        meta = TrackMeta(
            artist="Test Artist",
            title="Test Song",
            album="Test Album",
            year="2024",
            track_number="3",
            has_tags=True,
        )

        dest = organize(source_file, music_dir, meta=meta)

        assert dest is not None
        expected = music_dir / "Test Artist" / "Test Album (2024)" / "03 - Test Song.mp3"
        assert dest == expected
        assert dest.exists()

    def test_organize_without_track_number(self, music_dir, source_file):
        meta = TrackMeta(
            artist="Artist",
            title="Song",
            album="Album",
            year="2024",
            has_tags=True,
        )

        dest = organize(source_file, music_dir, meta=meta)
        assert dest is not None
        expected = music_dir / "Artist" / "Album (2024)" / "Song.mp3"
        assert dest == expected
        assert dest.exists()

    def test_organize_missing_source(self, music_dir):
        result = organize("/nonexistent/path.flac", music_dir)
        assert result is None
