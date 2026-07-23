"""Tests for the SQLite queue."""

import tempfile
from pathlib import Path
from queue import Queue as StdlibQueue

import pytest

from navidrome_collector.queue import Queue, QueueItem


@pytest.fixture
def q():
    """Create a temporary queue for each test."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        yield Queue(db)


class TestQueue:
    def test_add_and_next_pending(self, q):
        item_id = q.add("Artist - Song")
        assert item_id == 1

        item = q.next_pending()
        assert item is not None
        assert item.id == 1
        assert item.query == "Artist - Song"
        assert item.status == "in_progress"  # claimed

    def test_next_pending_empty(self, q):
        assert q.next_pending() is None

    def test_add_with_artist_title(self, q):
        item_id = q.add("Query", artist="Test Artist", title="Test Title")
        item = q.get(item_id)
        assert item is not None
        assert item.artist == "Test Artist"
        assert item.title == "Test Title"

    def test_mark_done(self, q):
        item_id = q.add("Track")
        q.next_pending()  # claim it
        q.mark_done(item_id, "/path/to/file.flac")

        item = q.get(item_id)
        assert item.status == "done"
        assert item.file_path == "/path/to/file.flac"

    def test_mark_failed(self, q):
        item_id = q.add("Track")
        q.next_pending()
        q.mark_failed(item_id, "Network error")

        item = q.get(item_id)
        assert item.status == "failed"
        assert item.error == "Network error"

    def test_list_items_empty(self, q):
        assert q.list_items() == []

    def test_list_items_filter_by_status(self, q):
        q.add("One")
        q.add("Two")
        pending = q.list_items(status="pending")
        assert len(pending) == 2

        first = q.next_pending()
        pending2 = q.list_items(status="pending")
        assert len(pending2) == 1

        in_progress = q.list_items(status="in_progress")
        assert len(in_progress) == 1
        assert in_progress[0].id == first.id

    def test_stats(self, q):
        assert q.stats() == {}

        q.add("A")
        q.add("B")
        stats = q.stats()
        assert stats == {"pending": 2}

        item = q.next_pending()
        q.mark_done(item.id, "/f.flac")
        stats = q.stats()
        assert stats == {"pending": 1, "done": 1}

    def test_clear_all(self, q):
        q.add("A")
        q.add("B")
        assert q.clear() == 2
        assert q.list_items() == []

    def test_clear_by_status(self, q):
        q.add("A")
        q.add("B")
        item = q.next_pending()
        q.mark_done(item.id, "/f.flac")

        q.clear(status="pending")
        assert len(q.list_items()) == 1  # only "done" remains

    def test_next_pending_atomic(self, q):
        """Simulate concurrent access: claim twice, second should be different."""
        q.add("First")
        q.add("Second")

        first = q.next_pending()
        second = q.next_pending()

        assert first.id == 1
        assert second.id == 2
        assert q.next_pending() is None  # no more pending

    def test_get_nonexistent(self, q):
        assert q.get(999) is None

    def test_dataclass_defaults(self):
        """QueueItem default values."""
        import datetime
        item = QueueItem(id=1, query="Test")
        assert item.status == "pending"
        assert item.artist is None
        assert item.title is None
        assert item.error is None
