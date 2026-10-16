"""Small builders for domain tests."""

from reels.domain.words import Word


def words(*spec: tuple[str, float, float]) -> list[Word]:
    return [Word(id=i, text=t, start=s, end=e) for i, (t, s, e) in enumerate(spec)]


def assert_no_cut_inside_words(boundaries: list[float], ws: list[Word]) -> None:
    for b in boundaries:
        for w in ws:
            assert not (w.start + 1e-9 < b < w.end - 1e-9), f"cut at {b} inside {w}"
