import pytest

from reels.domain.words import RawWord, Word, build_words, display_text, normalize_token


def test_ids_are_sequential_and_empty_tokens_dropped() -> None:
    ws = build_words(
        [
            RawWord(text=" Hallo", start=0, end=0.4),
            RawWord(text=" "),
            RawWord(text="Welt.", start=0.5, end=0.9),
        ]
    )
    assert [(w.id, w.text) for w in ws] == [(0, "Hallo"), (1, "Welt.")]


def test_missing_times_are_interpolated_between_neighbours() -> None:
    ws = build_words(
        [
            RawWord(text="es", start=1.0, end=1.2),
            RawWord(text="2024"),
            RawWord(text="und"),
            RawWord(text="dann", start=2.0, end=2.3),
        ]
    )
    assert (ws[1].start, ws[1].end) == pytest.approx((1.2, 1.6))
    assert (ws[2].start, ws[2].end) == pytest.approx((1.6, 2.0))


def test_missing_times_at_edges() -> None:
    ws = build_words([RawWord(text="a"), RawWord(text="b", start=1.0, end=1.5), RawWord(text="c")])
    # no neighbour on the left: collapse onto the next known time (keeps no extra silence)
    assert (ws[0].start, ws[0].end) == (1.0, 1.0)
    assert (ws[2].start, ws[2].end) == (1.5, 1.5)


def test_all_times_missing() -> None:
    ws = build_words([RawWord(text="a"), RawWord(text="b")])
    assert all(w.start == w.end == 0.0 for w in ws)


def test_overlaps_are_resolved() -> None:
    ws = build_words([RawWord(text="a", start=0, end=1.0), RawWord(text="b", start=0.8, end=0.9)])
    assert ws[1].start == 1.0
    assert ws[1].end == 1.0


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("„Kubernetes,“", "kubernetes"),
        ("Docker-Image.", "docker-image"),
        ("ÄHM", "ähm"),
        ("...", ""),
    ],
)
def test_normalize_token(text: str, token: str) -> None:
    assert normalize_token(text) == token


def test_display_text_uses_overlay() -> None:
    w = Word(id=3, text="Cuber", start=0, end=1)
    assert display_text(w) == "Cuber"
    assert display_text(w, {3: "Kubernetes"}) == "Kubernetes"
    assert display_text(w, {3: ""}) == ""


def test_hyphen_fragments_are_joined() -> None:
    ws = build_words(
        [
            RawWord(text=" S", start=1.0, end=1.1, score=0.9),
            RawWord(text=" -Tier,", start=1.15, end=1.5, score=0.7),
            RawWord(text=" würde", start=1.6, end=1.8),
        ]
    )
    assert [w.text for w in ws] == ["S-Tier,", "würde"]
    assert [w.id for w in ws] == [0, 1]
    assert (ws[0].start, ws[0].end, ws[0].score) == (1.0, 1.5, 0.7)


def test_hyphen_fragment_with_missing_times_keeps_known_ones() -> None:
    (w,) = build_words([RawWord(text="C", start=2.0, end=2.1), RawWord(text="-Tier.")])
    assert (w.text, w.start, w.end) == ("C-Tier.", 2.0, 2.1)


@pytest.mark.parametrize(
    "spec",
    [
        [("Ende.", 0, 1), ("-Tier", 1, 2)],  # after sentence end
        [("und,", 0, 1), ("-Tier", 1, 2)],  # after clause mark
        [("Wort", 0, 1), ("-", 1, 2)],  # lone hyphen
        [("Wort", 0, 1), ("–", 1, 2)],  # dash as punctuation
        [("-Tier", 0, 1)],  # nothing before
    ],
)
def test_hyphen_not_joined(spec: list[tuple[str, float, float]]) -> None:
    ws = build_words([RawWord(text=t, start=s, end=e) for t, s, e in spec])
    assert len(ws) == len(spec)
