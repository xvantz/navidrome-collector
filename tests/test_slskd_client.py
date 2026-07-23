"""Tests for the slskd REST API client."""

import pytest
from navidrome_collector.slskd_client import SlskdFile, SlskdDownload


class TestSlskdFile:
    def test_dataclass_defaults(self):
        f = SlskdFile(filename="test.mp3", size=1000, bitrate=320, duration=180,
                      sample_rate=44100, username="user")
        assert f.slot_free is True
        assert f.queue_length == 0
        assert f.upload_speed == 0
        assert f.filename == "test.mp3"
        assert f.bitrate == 320

    def test_slot_free_custom(self):
        f = SlskdFile(filename="f.mp3", size=0, bitrate=0, duration=0,
                      sample_rate=0, username="u", slot_free=False)
        assert f.slot_free is False


class TestSlskdDownload:
    def test_dataclass_defaults(self):
        d = SlskdDownload(id="1", filename="f.mp3", size=5000,
                          bytes_downloaded=2000, state="InProgress")
        assert d.error is None
        assert d.username == ""
        assert d.state == "InProgress"

    def test_completed_download(self):
        d = SlskdDownload(id="2", filename="f.mp3", size=10000,
                          bytes_downloaded=10000, state="Completed",
                          username="user")
        assert d.state == "Completed"
        assert d.bytes_downloaded == d.size
