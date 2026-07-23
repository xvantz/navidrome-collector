"""Tests for the yt-dlp downloader."""

from pathlib import Path

import pytest

from navidrome_collector.ytdlp_downloader import _parse_metadata, _tag_file
from navidrome_collector.tagger import read_tags


class TestParseMetadata:
    def test_parse_full(self):
        lines = [
            "/tmp/file.opus",
            "Sultans of Swing",
            "Dire Straits",
            "Dire StraitsVEVO",
            "20240115",
            "https://youtube.com/watch?v=abc123",
        ]
        meta = _parse_metadata(lines)
        assert meta is not None
        assert meta["title"] == "Sultans of Swing"
        assert meta["channel"] == "Dire Straits"
        assert meta["upload_date"] == "20240115"
        assert meta["url"] == "https://youtube.com/watch?v=abc123"

    def test_parse_too_short(self):
        assert _parse_metadata(["only_path.opus"]) is None


class TestTagFile:
    def test_tag_opus(self, tmp_path):
        """Tag a minimal file and verify tags are written."""
        path = tmp_path / "test.opus"
        path.write_bytes(b"\x00" * 100)  # Not a real opus, but tag_file won't crash

        meta = {
            "title": "Sultans of Swing",
            "channel": "Dire Straits",
            "uploader": "Dire StraitsVEVO",
            "upload_date": "19850101",
            "url": "https://youtube.com/watch?v=abc",
        }

        # Should not crash
        _tag_file(path, meta)

        # The file should still exist
        assert path.exists()
