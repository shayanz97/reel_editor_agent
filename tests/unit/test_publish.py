import itertools
from pathlib import Path

import pytest

from reels.domain.cutplan import KeepRange
from reels.domain.glossary import GlossaryTerm, apply_glossary_text
from reels.media.frames import FrameScore, sample_times, select_candidates
from reels.publish import cover_plan, hashtags, post_text

TERMS = [GlossaryTerm(term="Kubernetes", variants=["Kubanetis", "Cuber Netties"])]


def test_sample_times_spread_over_kept_parts_away_from_cuts() -> None:
    keep = [
        KeepRange(start=0.0, end=2.0, first_word=0, last_word=1),
        KeepRange(start=5.0, end=7.0, first_word=2, last_word=3),
    ]
    times = sample_times(keep, 4)
    assert len(times) == 4
    assert all(0.2 <= t <= 1.8 or 5.2 <= t <= 6.8 for t in times)
    assert sum(t < 3 for t in times) == 2  # evenly split between both parts
    assert sample_times([KeepRange(start=0, end=0.3, first_word=0, last_word=0)], 3) == []


def _score(t: float, sharp: float, faces: int = 0, area: float = 0.0) -> FrameScore:
    return FrameScore(t, Path(f"f{t}.jpg"), sharp, faces, area)


def test_select_candidates_prefers_faces_drops_blur_keeps_gap() -> None:
    scores = [
        _score(1.0, 100), _score(1.2, 100, faces=1, area=0.1), _score(2.0, 5, faces=1, area=0.3),
        _score(4.0, 90), _score(6.0, 110), _score(8.0, 95, faces=1, area=0.05),
    ]  # fmt: skip
    picked = select_candidates(scores, k=3, min_gap_s=1.5)
    times = [p.time for p in picked]
    assert 2.0 not in times  # blurred despite the face
    assert 1.2 in times and 8.0 in times  # faces first
    assert all(abs(a - b) >= 1.5 for a, b in itertools.pairwise(times))
    assert times == sorted(times)


def test_select_candidates_relaxes_gap_when_needed() -> None:
    scores = [_score(1.0, 100), _score(1.1, 100), _score(1.2, 100)]
    assert len(select_candidates(scores, k=3, min_gap_s=5)) == 3
    assert select_candidates([], k=3) == []


def test_plan_covers_validates_and_fills() -> None:
    planned = cover_plan.plan_covers(
        [(2, "Erste Million"), (2, "Doppelt"), (9, "Gibt es nicht"), (1, "erste million"), (3, "")],
        candidate_count=5,
        first_sentence="Wege zur ersten Million in drei Schritten.",
    )
    assert next((p.number, p.candidate) for p in planned) == (1, 1)
    assert len(planned) == 3 and len({p.candidate for p in planned}) == 3
    assert planned[1].headline == "Wege zur ersten Million in drei"
    assert len({p.headline.casefold() for p in planned}) >= 2


def test_plan_covers_without_llm_and_long_headlines() -> None:
    planned = cover_plan.plan_covers([], 2, "")
    assert [p.headline for p in planned] == ["Neues Video", "Neues Video"]
    long = cover_plan.plan_covers([(1, "Wort " * 30)], 1, "x")
    assert len(long[0].headline) <= cover_plan.MAX_HEADLINE_CHARS


def test_hashtags_normalized() -> None:
    tags = hashtags.normalize(
        ["#Geld", "geld", "#Künstliche Intelligenz", "123", "", "#kubernetes", "#a", "#b"],
        max_count=4,
        terms=TERMS,
    )
    assert tags == ["#Geld", "#KünstlicheIntelligenz", "#Kubernetes", "#a"]


def test_glossary_in_free_text() -> None:
    text = apply_glossary_text("Mit kubanetis und Cuber  Netties. Kubanetisch bleibt.", TERMS)
    assert text == "Mit Kubernetes und Kubernetes. Kubanetisch bleibt."


def test_post_compose_and_fallback() -> None:
    post = post_text.finalize("Hook mit Kubanetis.", "Body.", "Frage?", ["#a", "a", "#b"], TERMS, 5)
    assert post["hook"] == "Hook mit Kubernetes."
    assert post_text.compose(post) == "Hook mit Kubernetes.\n\nBody.\n\nFrage?\n\n#a #b"
    fallback = post_text.fallback_post("Erster Satz.", TERMS, 5)
    assert post_text.compose(fallback) == "Erster Satz.\n\n#Kubernetes"


@pytest.mark.parametrize(
    ("tone", "text"), [("du_fachlich", "per Du"), ("Sie, locker", "Sie, locker")]
)
def test_tone_description(tone: str, text: str) -> None:
    assert text in post_text.tone_description(tone)
