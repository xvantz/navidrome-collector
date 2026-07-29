"""Tests for the matcher module — query parsing, title extraction, similarity."""
import pytest
from pathlib import Path

from navidrome_collector.matcher import (
    parse_query,
    extract_track_title,
    title_similarity,
    score_format,
    score_ytdlp,
)


class TestParseQuery:
    def test_artist_dash_title(self):
        artist, title = parse_query("50 Cent - Candy Shop")
        assert artist == "50 Cent"
        assert title == "Candy Shop"

    def test_en_dash_separator(self):
        artist, title = parse_query("Imagine Dragons – Thunder")
        assert artist == "Imagine Dragons"
        assert title == "Thunder"

    def test_em_dash_separator(self):
        artist, title = parse_query("Artist — Song")
        assert artist == "Artist"
        assert title == "Song"

    def test_pipe_separator(self):
        artist, title = parse_query("Artist | Title")
        assert artist == "Artist"
        assert title == "Title"

    def test_no_separator(self):
        artist, title = parse_query("Just a song name")
        assert artist is None
        assert title is None

    def test_empty_string(self):
        artist, title = parse_query("")
        assert artist is None
        assert title is None


class TestExtractTrackTitle:
    def test_windows_path_slskd(self):
        result = extract_track_title(
            r"shared\deezer\Eminem\04 - Lose Yourself.flac"
        )
        assert "Lose Yourself" in result

    def test_disc_track_prefix(self):
        result = extract_track_title("1-03 8 Mile.m4a")
        assert result == "8 Mile"

    def test_disc_track_dash(self):
        result = extract_track_title("01 - Candy Shop.mp3")
        assert result == "Candy Shop"

    def test_leading_track_number(self):
        result = extract_track_title("05 All Star.m4a")
        assert result == "All Star"

    def test_artist_in_filename(self):
        result = extract_track_title("Eminem - 8 Mile.mp3")
        assert result == "Eminem - 8 Mile"

    def test_unix_path(self):
        result = extract_track_title("music/Imagine Dragons/Thunder.m4a")
        assert result == "Thunder"

    def test_metadata_brackets_stripped(self):
        result = extract_track_title("01 - Lose Yourself (Official Video).flac")
        assert result == "Lose Yourself"

class TestTitleSimilarity:
    def test_exact_match(self):
        assert title_similarity("Thunder", "Thunder") == 1.0

    def test_no_match(self):
        assert title_similarity("Thunder", "Lose Yourself") == 0.0

    def test_partial_match(self):
        sim = title_similarity("8 Mile", "Eminem - 8 Mile")
        assert 0.3 < sim < 0.8  # "8" is single-char → filtered, "mile" matches

    def test_case_insensitive(self):
        assert title_similarity("all star", "All Star") == 1.0

    def test_empty_tokens(self):
        assert title_similarity("a", "b") == 0.0  # single char → filtered


class TestScoreFormat:
    def test_flac_highest(self):
        assert score_format(".flac") == 5

    def test_mp3_lowest(self):
        assert score_format(".mp3") == 1

    def test_opus_m4a_level(self):
        assert score_format(".opus") == 3  # bumped to m4a level

    def test_unknown_format(self):
        assert score_format(".wma") == 0

    def test_no_extension(self):
        assert score_format("") == -1


class TestScoreYtdlp:
    def test_with_bitrate(self):
        score = score_ytdlp({"bitrate": 128_000}, Path("file.m4a"))
        assert score > 0

    def test_no_metadata(self):
        score = score_ytdlp(None, Path("file.opus"))
        assert score > 0  # default fallback

    def test_no_file(self):
        assert score_ytdlp(None) == 0
