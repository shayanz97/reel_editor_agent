import pytest

from reels.domain.word_compare import compare_words
from tests.unit.helpers import words


def test_identical_transcripts() -> None:
    a = words(("Hallo", 0.0, 0.5), ("Welt.", 0.6, 1.0))
    c = compare_words(a, a)
    assert c.matched == 2
    assert c.text_similarity == 1.0
    assert c.start_diff_s is not None and c.start_diff_s.max == 0
    assert c.differences == []


def test_timing_offsets_and_text_differences() -> None:
    a = words(
        ("Heute", 0.0, 0.4),
        ("geht", 0.5, 0.7),
        ("es", 0.8, 0.9),
        ("um", 1.0, 1.1),
        ("Docker.", 1.2, 1.6),
    )
    b = words(("heute", 0.1, 0.4), ("gehts", 0.5, 0.8), ("um", 1.05, 1.1), ("Docker", 1.2, 1.7))
    c = compare_words(a, b)
    assert c.matched == 3  # heute, um, docker (case/punctuation-insensitive)
    assert c.start_diff_s is not None and c.end_diff_s is not None
    assert c.start_diff_s.max == pytest.approx(0.1)
    assert c.end_diff_s.max == pytest.approx(0.1)
    assert [(d.a, d.b) for d in c.differences] == [("geht es", "gehts")]


def test_empty_side() -> None:
    c = compare_words(words(("a", 0, 1)), [])
    assert c.matched == 0
    assert c.start_diff_s is None
    assert c.differences[0].a == "a"
