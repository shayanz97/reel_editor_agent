from reels.domain.segments import number_segments, split_sentences
from tests.unit.helpers import words


def _ws(text: str):  # type: ignore[no-untyped-def]
    return words(*[(t, i, i + 0.5) for i, t in enumerate(text.split())])


def test_split_at_sentence_punctuation() -> None:
    ws = _ws("Hallo zusammen. Heute geht es um Docker! Bereit? Los")
    spans = split_sentences(ws)
    assert [(s.first_word, s.last_word) for s in spans] == [(0, 1), (2, 6), (7, 7), (8, 8)]


def test_closing_quotes_after_punctuation() -> None:
    ws = _ws("Er sagte „Hallo.“ Dann ging er")
    assert [(s.first_word, s.last_word) for s in split_sentences(ws)] == [(0, 2), (3, 5)]


def test_long_sentence_split_at_comma_near_middle() -> None:
    ws = _ws("a b c, d e f g, h i j k l")
    spans = split_sentences(ws, max_words=6)
    assert all(s.last_word - s.first_word + 1 <= 6 for s in spans)
    assert spans[0].first_word == 0 and spans[-1].last_word == 11


def test_long_sentence_without_commas_split_in_middle() -> None:
    spans = split_sentences(_ws(" ".join("w" for _ in range(10))), max_words=4)
    assert all(s.last_word - s.first_word + 1 <= 4 for s in spans)
    assert sum(s.last_word - s.first_word + 1 for s in spans) == 10


def test_numbering_skips_fully_cut_segments_and_uses_overlay() -> None:
    ws = _ws("Eins zwei. Drei vier. Fünf sechs.")
    spans = split_sentences(ws)
    kept = {0, 1, 4, 5}  # second sentence cut away
    segs = number_segments(ws, spans, kept, overlay={4: "FÜNF"})
    assert [(s.number, s.first_word, s.text) for s in segs] == [
        (1, 0, "Eins zwei."),
        (2, 4, "FÜNF sechs."),
    ]


def test_numbering_is_deterministic() -> None:
    ws = _ws("A b. C d. E f.")
    spans = split_sentences(ws)
    kept = {w.id for w in ws}
    assert number_segments(ws, spans, kept) == number_segments(ws, spans, kept)


def test_segment_with_only_hidden_words_is_skipped() -> None:
    ws = _ws("A. B.")
    segs = number_segments(ws, split_sentences(ws), {0, 1}, overlay={0: ""})
    assert [(s.number, s.text) for s in segs] == [(1, "B.")]
