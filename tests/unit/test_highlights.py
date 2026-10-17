from reels.domain.highlights import HighlightChoice, select_highlights, transcript_for_prompt
from reels.domain.segments import Segment
from tests.unit.helpers import words

# 1: "Investieren."  2: "A-Tier."  3: "Es dauert halt 100 Jahre, leider."
SEGS = [
    Segment(number=1, first_word=0, last_word=0, text="Investieren."),
    Segment(number=2, first_word=1, last_word=1, text="A-Tier."),
    Segment(number=3, first_word=2, last_word=7, text="Es dauert halt 100 Jahre, leider."),
]
ALL = set(range(8))


def _c(
    segment: int, kind: str, first: int | None = None, last: int | None = None
) -> HighlightChoice:
    return HighlightChoice(segment=segment, kind=kind, first_word=first, last_word=last)  # type: ignore[arg-type]


def test_structure_and_key_phrases() -> None:
    choices = [_c(1, "structure", 0, 0), _c(2, "structure", 1, 1), _c(3, "key", 5, 6)]
    sel = select_highlights(choices, SEGS, ALL, ALL)
    assert sel.word_ids == [0, 1, 5, 6]
    assert (sel.missing_segments, sel.rejected) == (0, 0)


def test_none_and_missing_segments() -> None:
    sel = select_highlights([_c(1, "structure", 0, 0), _c(2, "none")], SEGS, ALL, ALL)
    assert sel.word_ids == [0]
    assert sel.missing_segments == 1 and sel.rejected == 0


def test_invalid_choices_are_dropped_not_fixed() -> None:
    choices = [
        _c(1, "structure", 0, 1),  # reaches into segment 2
        _c(2, "key"),  # no words given
        _c(3, "key", 2, 7),  # longer than 4 words
        _c(3, "key", 5, 5),  # second answer for segment 3: ignored
        _c(9, "key", 0, 0),  # unknown segment
    ]
    sel = select_highlights(choices, SEGS, ALL, ALL)
    assert sel.word_ids == []
    assert sel.rejected == 5


def test_reversed_span_rejected() -> None:
    assert select_highlights([_c(3, "key", 6, 5)], SEGS, ALL, ALL).rejected == 1


def test_cut_words_reject_the_phrase_hidden_words_stay_hidden() -> None:
    kept = ALL - {6}
    assert select_highlights([_c(3, "key", 5, 6)], SEGS, kept, ALL).word_ids == []
    visible = ALL - {6}  # e.g. second word of a multi-word glossary replacement
    assert select_highlights([_c(3, "key", 5, 6)], SEGS, ALL, visible).word_ids == [5]


def test_transcript_for_prompt_numbers_segments_and_skips_hidden() -> None:
    ws = words(("Cuber", 0, 1), ("Netties", 1, 2), ("rockt.", 2, 3), ("Ja.", 3, 4))
    segs = [
        Segment(number=1, first_word=0, last_word=2, text=""),
        Segment(number=2, first_word=3, last_word=3, text=""),
    ]
    text = transcript_for_prompt(ws, segs, {0: "Kubernetes", 1: ""})
    assert text == "1: [0]Kubernetes [2]rockt.\n2: [3]Ja."
