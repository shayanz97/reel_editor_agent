from reels.domain.highlights import select_highlights, transcript_for_prompt
from reels.domain.segments import Segment
from tests.unit.helpers import words

SEGS = [
    Segment(number=1, first_word=0, last_word=2, text="a b c"),
    Segment(number=2, first_word=3, last_word=5, text="d e f"),
]


def test_select_filters_unknown_duplicates_and_caps_per_segment() -> None:
    allowed = {0, 1, 2, 3, 4}  # 5 is cut away
    assert select_highlights([2, 1, 2, 99, 5, 4, 3], SEGS, allowed) == [2, 4]


def test_select_allows_more_per_segment_when_configured() -> None:
    assert select_highlights([0, 1, 2], SEGS, {0, 1, 2}, max_per_segment=2) == [0, 1]


def test_select_empty() -> None:
    assert select_highlights([], SEGS, {0}) == []


def test_transcript_for_prompt_uses_overlay_and_skips_hidden() -> None:
    ws = words(("Cuber", 0, 1), ("Netties", 1, 2), ("rockt.", 2, 3), ("Ja.", 3, 4))
    segs = [
        Segment(number=1, first_word=0, last_word=2, text=""),
        Segment(number=2, first_word=3, last_word=3, text=""),
    ]
    text = transcript_for_prompt(ws, segs, {0: "Kubernetes", 1: ""})
    assert text == "[0]Kubernetes [2]rockt.\n[3]Ja."
