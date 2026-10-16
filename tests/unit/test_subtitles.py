import itertools

import pytest

from reels.domain.cutplan import KeepRange
from reels.domain.segments import split_sentences
from reels.domain.subtitles import GroupParams, build_events
from reels.domain.timeline import Timeline
from tests.unit.helpers import words


def _linear(n_words: int, texts: list[str] | None = None):  # type: ignore[no-untyped-def]
    texts = texts or [f"w{i}" for i in range(n_words)]
    ws = words(*[(t, i * 0.5, i * 0.5 + 0.4) for i, t in enumerate(texts)])
    tl = Timeline([KeepRange(start=0, end=len(ws) * 0.5, first_word=0, last_word=len(ws) - 1)])
    return ws, tl


def test_groups_respect_max_words() -> None:
    ws, tl = _linear(10)
    events = build_events(ws, tl, split_sentences(ws), GroupParams(max_words=4, max_chars=100))
    assert [len(e.words) for e in events] == [4, 4, 2]


def test_groups_break_at_sentence_and_clause() -> None:
    ws, tl = _linear(0, ["Hallo", "Welt.", "Heute,", "morgen", "auch"])
    events = build_events(ws, tl, split_sentences(ws), GroupParams(max_chars=100))
    assert [e.text for e in events] == ["Hallo Welt.", "Heute,", "morgen auch"]


def test_groups_respect_max_chars_but_keep_long_single_words() -> None:
    ws, tl = _linear(0, ["Donaudampfschifffahrtsgesellschaft", "ist", "lang", "und", "so"])
    events = build_events(ws, tl, split_sentences(ws), GroupParams(max_chars=10))
    assert [e.text for e in events] == ["Donaudampfschifffahrtsgesellschaft", "ist lang", "und so"]


def test_times_are_on_output_timeline_and_cut_words_skipped() -> None:
    ws = words(("a", 1.0, 1.4), ("b", 3.0, 3.4), ("c", 5.0, 5.4))
    tl = Timeline(
        [
            KeepRange(start=0.9, end=1.5, first_word=0, last_word=0),
            KeepRange(start=4.9, end=5.5, first_word=2, last_word=2),
        ]
    )
    events = build_events(ws, tl, split_sentences(ws), GroupParams(linger_s=0.0))
    assert len(events) == 1
    assert [w.word_id for w in events[0].words] == [0, 2]
    assert events[0].start == pytest.approx(0.1)
    assert events[0].words[1].start == pytest.approx(0.7)
    assert events[0].end == pytest.approx(1.1)


def test_linger_never_overlaps_next_event() -> None:
    ws, tl = _linear(0, ["Eins.", "Zwei.", "Drei."])
    events = build_events(ws, tl, split_sentences(ws), GroupParams(linger_s=0.25))
    assert events[0].end == pytest.approx(0.5)  # next starts at 0.5
    assert events[-1].end == pytest.approx(1.4 + 0.25)
    for a, b in itertools.pairwise(events):
        assert a.end <= b.start


def test_overlay_hidden_words_and_highlights() -> None:
    ws, tl = _linear(0, ["Cuber", "Netties", "rockt"])
    events = build_events(
        ws, tl, split_sentences(ws), GroupParams(), overlay={0: "Kubernetes", 1: ""}, highlights={0}
    )
    assert events[0].text == "Kubernetes rockt"
    assert [w.highlight for w in events[0].words] == [True, False]
