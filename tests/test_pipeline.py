"""Integration test for the full pipeline flow: add → process → organize → enrich.

Tests the complete chain with mocked external services so results are predictable.
"""

from unittest.mock import patch, MagicMock, PropertyMock
from pathlib import Path
import tempfile

import pytest

from navidrome_collector.queue import Queue
from navidrome_collector.collector import Collector
from navidrome_collector.slskd_client import SlskdFile, SlskdDownload


@pytest.fixture
def queue():
    with tempfile.TemporaryDirectory() as tmp:
        yield Queue(Path(tmp) / "test.db")


@pytest.fixture
def slskd():
    client = MagicMock()
    client.ping.return_value = True
    return client


@pytest.fixture
def collector(queue, slskd):
    return Collector(
        queue=queue,
        slskd=slskd,
        music_dir=Path("/tmp/music"),
        download_dir=Path("/tmp/downloads"),
        ytdlp_fallback=False,  # don't try yt-dlp in tests
    )


class TestPipeline:
    """Full pipeline: add → process → check with mocked externals.

    Expected chain:
      1. queue.add("Artist - Title") → pending
      2. process_queue → in_progress → search Soulseek
      3. If Soulseek has good results → enqueue → processing
      4. Next cycle → check → find completed → organize → enrich → done
    """

    def test_full_pipeline_soulseek_wins(self, collector, slskd):
        """Soulseek finds results → enqueue → processing state."""
        collector.queue.add("50 Cent - Candy Shop")

        # Mock Soulseek search results
        slskd.search.return_value = [
            SlskdFile(
                filename=r"Music\50 Cent\The Massacre\07 Candy Shop.m4a",
                size=8_000_000,
                bitrate=885,
                duration=240,
                username="tayjan",
                slot_free=True,
            ),
        ]
        slskd.enqueue.return_value = "dl-candy-1"

        # Run process
        stats = collector.process_queue(max_items=1)

        # Verify state: enqueued, not counted as done
        assert stats["processed"] == 0  # not counted — waiting
        assert stats["succeeded"] == 0

        # Queue item should be "processing"
        item = collector.queue.get(1)
        assert item is not None
        assert item.status == "processing"
        assert "tayjan" in (item.error or "")

    def test_full_pipeline_soulseek_no_results(self, collector, slskd):
        """Soulseek returns nothing → marked as failed."""
        collector.queue.add("Unknown Artist - Very Rare Track")
        slskd.search.return_value = []

        stats = collector.process_queue()

        assert stats["failed"] == 1
        item = collector.queue.get(1)
        assert item.status == "failed"

    def test_full_pipeline_check_complete(self, collector, slskd):
        """Processing item → Soulseek download completed → organized → done."""
        # Setup: item already in processing state with pending downloads
        item_id = collector.queue.add("Test - Track")
        collector.queue.mark_processing(item_id, [
            ("tayjan", r"Music\Test Artist\Album\01 Track.m4a"),
        ])

        # Mock: file exists in download dir
        with tempfile.TemporaryDirectory() as tmp:
            fake_file = Path(tmp) / "Track.m4a"
            fake_file.write_bytes(b"\x00" * 100)

            with patch.object(collector, "_find_local_path", return_value=fake_file):
                with patch("navidrome_collector.collector.organize_file",
                           return_value=Path("/srv/music/Test Artist/Album/01 Track.m4a")):
                    stats = collector.process_queue()

            assert stats["succeeded"] == 1
            item = collector.queue.get(item_id)
            assert item.status == "done"
            assert item.file_path == str(Path("/srv/music/Test Artist/Album/01 Track.m4a"))


class TestPipelineWithYtdlp:
    """Tests for the yt-dlp vs Soulseek comparison flow."""

    @patch("navidrome_collector.ytdlp_downloader.search_and_download")
    def test_ytdlp_wins_over_soulseek(self, mock_yt, queue):
        """yt-dlp quality > Soulseek → use yt-dlp result."""
        slskd = MagicMock()
        slskd.ping.return_value = True
        slskd.search.return_value = [
            SlskdFile(
                filename="song.mp3", size=3_000_000, bitrate=128,
                username="user1", slot_free=True,
            ),
        ]

        # Mock yt-dlp returning a high-quality file
        fake_file = Path("/tmp/fake_ytdlp_output.m4a")
        mock_yt.return_value = (fake_file, {
            "title": "Test Artist - Test Song",
            "channel": "Test Artist",
            "bitrate": 256_000,
        })

        collector = Collector(
            queue=queue,
            slskd=slskd,
            music_dir=Path("/tmp/music"),
            download_dir=Path("/tmp/downloads"),
            ytdlp_fallback=True,
            ytdlp_dir=Path("/tmp/ytdlp"),
        )

        queue.add("Test Artist - Test Song")

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "stat") as mock_stat:
                mock_stat.return_value.st_size = 5_000_000
                with patch("navidrome_collector.collector.organize_file",
                           return_value=Path("/srv/music/Artist/Album/track.m4a")):
                    stats = collector.process_queue(max_items=1)

        assert stats["succeeded"] == 1

    @patch("navidrome_collector.ytdlp_downloader.search_and_download")
    def test_soulseek_beats_ytdlp(self, mock_yt, queue):
        """Soulseek has FLAC → enqueue instead of using yt-dlp."""
        slskd = MagicMock()
        slskd.ping.return_value = True
        slskd.search.return_value = [
            SlskdFile(
                filename="song.flac", size=25_000_000, bitrate=1000,
                username="flac-user", slot_free=True,
            ),
        ]
        slskd.enqueue.return_value = "dl-flac-1"

        # yt-dlp returns opus
        fake_file = Path("/tmp/fake_opus.opus")
        mock_yt.return_value = (fake_file, {
            "title": "Test Artist - Test Song",
            "channel": "Test Artist",
            "bitrate": 93_000,
        })

        collector = Collector(
            queue=queue,
            slskd=slskd,
            music_dir=Path("/tmp/music"),
            download_dir=Path("/tmp/downloads"),
            ytdlp_fallback=True,
            ytdlp_dir=Path("/tmp/ytdlp"),
        )

        queue.add("Test Artist - Test Song")

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "stat") as mock_stat:
                mock_stat.return_value.st_size = 3_000_000
                stats = collector.process_queue(max_items=1)

        # Soulseek should win → enqueued, not done
        assert stats["processed"] == 0  # enqueued, waiting
        assert stats["succeeded"] == 0
        item = queue.get(1)
        assert item.status == "processing"
