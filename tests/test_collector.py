"""Tests for the collector (pipeline)."""

from unittest.mock import Mock, MagicMock, patch
from pathlib import Path
import tempfile

import pytest

from navidrome_collector.collector import Collector
from navidrome_collector.queue import Queue
from navidrome_collector.slskd_client import SlskdFile, SlskdDownload


@pytest.fixture
def queue():
    with tempfile.TemporaryDirectory() as tmp:
        yield Queue(Path(tmp) / "test.db")


@pytest.fixture
def slskd():
    client = MagicMock()
    # By default, slskd is reachable
    client.ping.return_value = True
    return client


@pytest.fixture
def collector(queue, slskd):
    return Collector(
        queue=queue,
        slskd=slskd,
        music_dir=Path("/tmp/music"),
        download_dir=Path("/tmp/downloads"),
    )


class TestPickBest:
    def test_pick_flac_over_mp3(self, collector):
        files = [
            SlskdFile(filename="song.mp3", size=5_000_000, bitrate=320,
                      duration=200, sample_rate=44100, username="u1"),
            SlskdFile(filename="song.flac", size=20_000_000, bitrate=1000,
                      duration=200, sample_rate=44100, username="u2"),
        ]
        best = collector._pick_best(files, "query")
        assert best is not None
        assert best.filename.endswith(".flac")

    def test_pick_higher_bitrate_same_format(self, collector):
        files = [
            SlskdFile(filename="song.mp3", size=3_000_000, bitrate=128,
                      duration=200, sample_rate=44100, username="u1"),
            SlskdFile(filename="song.mp3", size=6_000_000, bitrate=320,
                      duration=200, sample_rate=44100, username="u2"),
        ]
        best = collector._pick_best(files, "query")
        assert best is not None
        assert best.bitrate == 320

    def test_pick_prefers_free_slot(self, collector):
        files = [
            SlskdFile(filename="song.mp3", size=5_000_000, bitrate=320,
                      duration=200, sample_rate=44100, username="u1", slot_free=False),
            SlskdFile(filename="song.mp3", size=5_000_000, bitrate=256,
                      duration=200, sample_rate=44100, username="u2", slot_free=True),
        ]
        best = collector._pick_best(files, "query")
        assert best is not None
        assert best.slot_free is True

    def test_pick_empty_list(self, collector):
        assert collector._pick_best([], "query") is None

    def test_pick_ogg_vs_mp3(self, collector):
        """Ogg/Opus preferred over MP3 but less than FLAC."""
        files = [
            SlskdFile(filename="song.ogg", size=4_000_000, bitrate=192,
                      duration=200, sample_rate=44100, username="u1"),
            SlskdFile(filename="song.flac", size=18_000_000, bitrate=900,
                      duration=200, sample_rate=44100, username="u2"),
            SlskdFile(filename="song.mp3", size=5_000_000, bitrate=320,
                      duration=200, sample_rate=44100, username="u3"),
        ]
        best = collector._pick_best(files, "query")
        assert best is not None
        assert best.filename.endswith(".flac")

    def test_pick_prefers_short_queue(self, collector):
        """Prefer shorter queue over longer one, all else equal."""
        files = [
            SlskdFile(filename="song.flac", size=20_000_000, bitrate=1000,
                      duration=200, sample_rate=44100, username="u1",
                      queue_length=50),
            SlskdFile(filename="song.flac", size=20_000_000, bitrate=1000,
                      duration=200, sample_rate=44100, username="u2",
                      queue_length=2),
        ]
        best = collector._pick_best(files, "query")
        assert best is not None
        assert best.username == "u2"  # shorter queue


class TestProcessQueue:
    def test_empty_queue(self, collector):
        stats = collector.process_queue()
        assert stats == {"processed": 0, "succeeded": 0, "failed": 0}

    def test_search_no_results(self, collector, slskd):
        collector.queue.add("Unknown Artist - Rare Song")
        slskd.search.return_value = []

        stats = collector.process_queue()
        assert stats["processed"] == 1
        assert stats["failed"] == 1

        item = collector.queue.get(1)
        assert item.status == "failed"

    def test_full_pipeline_success(self, collector, slskd):
        collector.queue.add("Test Artist - Test Song")

        # Mock slskd responses
        slskd.search.return_value = [
            SlskdFile(filename="test_song.mp3", size=5_000_000, bitrate=320,
                      duration=200, sample_rate=44100, username="soulseeker",
                      slot_free=True),
        ]
        slskd.enqueue.return_value = "dl-1"
        slskd.wait_for_download.return_value = SlskdDownload(
            id="dl-1", filename="test_song.mp3", size=5_000_000,
            bytes_downloaded=5_000_000, state="Completed",
            username="soulseeker",
        )

        # Mock _find_local to return a fake file
        with tempfile.TemporaryDirectory() as tmp:
            fake_file = Path(tmp) / "test_song.mp3"
            fake_file.write_bytes(b"\x00" * 1000)

            # We need to patch find_local but also the organize function
            with patch.object(collector, "_find_local", return_value=fake_file):
                with patch("navidrome_collector.collector.organize_file",
                           return_value=Path("/srv/music/Test_Artist/Album/test_song.mp3")):
                    stats = collector.process_queue()

        assert stats["processed"] == 1
        assert stats["succeeded"] == 1
