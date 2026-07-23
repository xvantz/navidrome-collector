"""Tests for the tagger module.

Uses real FLAC/MP3 files for tag operations.
For minimal FLAC, we create a valid enough header.
"""

from pathlib import Path
import struct

import pytest
import mutagen

from navidrome_collector.tagger import (
    TrackMeta,
    read_tags,
    write_tags,
)


def _valid_minimal_flac(path: Path) -> Path:
    """Create a minimal valid FLAC file mutagen can work with.

    A FLAC file must have "fLaC" marker + STREAMINFO block (34 bytes).
    Returns the path.
    """
    flac = b"fLaC"

    # Metadata block header: last-metadata-block=1 (0x80), type=0 (STREAMINFO)
    # 3-byte length = 34
    block_header = bytes([0x80]) + (34).to_bytes(3, "big")

    # STREAMINFO: 34 bytes total
    info = b""
    info += struct.pack(">HH", 4096, 4096)    # min/max blocksize
    info += b"\x00\x00\x00\x00\x00\x00"        # min/max framesize = 0
    # sample_rate=44100 (0xAC44), channels=2 (0b010),
    # sample_rate=44100 (0xAC44), channels=2, bits_per_sample=16, total_samples=0
    # Packed as 20+3+5+36 = 64 bits = 8 bytes
    # Bytes: 0x0A 0xC4 0x42 0xF0 0x00 0x00 0x00 0x00
    info += b"\x0A\xC4\x42\xF0\x00\x00\x00\x00"  # audio properties (8 bytes)
    info += b"\x00" * 16  # MD5 signature = all zeros

    assert len(info) == 34, f"STREAMINFO must be 34 bytes, got {len(info)}"

    flac += block_header + info
    path.write_bytes(flac)
    return path


class TestReadTags:
    def test_read_tags_valid_flac(self, tmp_path):
        path = _valid_minimal_flac(tmp_path / "test.flac")

        # Should not crash
        meta = read_tags(path)
        assert isinstance(meta, TrackMeta)
        assert meta.has_tags is False  # no tags in minimal flac

    def test_read_tags_nonexistent(self):
        meta = read_tags("/nonexistent/file.mp3")
        assert meta.has_tags is False
        assert meta.artist == ""

    def test_read_tags_unknown_format(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("not audio")
        meta = read_tags(f)
        assert meta.has_tags is False


class TestWriteTags:
    def test_write_tags_to_flac(self, tmp_path):
        """Write tags to a minimal FLAC, read them back."""
        path = _valid_minimal_flac(tmp_path / "test.flac")

        # Ensure mutagen can read it first
        audio = mutagen.File(str(path))
        assert audio is not None, "Mutagen should be able to open our minimal FLAC"

        meta = TrackMeta(
            artist="Writer",
            title="Write Test",
            album="Test Album",
            year="2023",
            genre="Rock",
            track_number="5",
            has_tags=True,
        )

        result = write_tags(path, meta)
        assert result is True, "write_tags should succeed on valid FLAC"

        reread = read_tags(path)
        assert reread.artist == "Writer"
        assert reread.title == "Write Test"

    def test_write_tags_no_crash_binary(self, tmp_path):
        """write_tags shouldn't crash on random bytes — should return False."""
        path = tmp_path / "junk.flac"
        path.write_bytes(b"\x00" * 1024)

        meta = TrackMeta(artist="A", title="B", album="C", has_tags=True)
        result = write_tags(path, meta)
        assert result is False  # can't write to garbage
