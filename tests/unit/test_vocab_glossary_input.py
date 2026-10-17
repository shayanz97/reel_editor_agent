import pytest

from reels.bot.glossary_input import parse_entry, parse_lines
from reels.bot.service import is_glossary_candidate
from reels.domain.glossary import GlossaryTerm
from reels.domain.segments import number_segments, split_sentences
from reels.domain.vocab import SuspiciousWord, known_tokens, select_questions, transcript_lines
from tests.unit.helpers import words

TOKENS = ["Wir", "nutzen", "Kubanetis", "und", "Docka.", "Dann", "Helm", "Charts."]
WS = words(*[(t, i, i + 0.5) for i, t in enumerate(TOKENS)])
SEGS = number_segments(WS, split_sentences(WS), {w.id for w in WS})


def _item(seg: int, first: int, last: int, suggestion: str | None = None) -> SuspiciousWord:
    return SuspiciousWord(segment=seg, first_word=first, last_word=last, suggestion=suggestion)


def test_questions_are_validated_deduplicated_and_capped() -> None:
    items = [
        _item(1, 2, 2, "Kubernetes"),
        _item(1, 2, 2, "Kubernetes"),  # same word again
        _item(1, 4, 4, None),
        _item(2, 6, 7, "Helm Charts"),  # suggestion equals text -> no suggestion
        _item(1, 4, 5),  # crosses into segment 2
        _item(9, 0, 0),  # unknown segment
        _item(1, 0, 4),  # too long
    ]
    qs = select_questions(items, WS, SEGS, {}, known=set())
    assert [(q.id, q.text, q.suggestion) for q in qs] == [
        (1, "Kubanetis", "Kubernetes"), (2, "Docka.", None), (3, "Helm Charts.", None),
    ]  # fmt: skip
    assert qs[0].context == "Wir nutzen Kubanetis und Docka." and qs[0].segment == 1
    assert len(select_questions(items, WS, SEGS, {}, known=set(), max_questions=1)) == 1


def test_known_glossary_terms_are_not_asked() -> None:
    known = known_tokens([GlossaryTerm(term="Docker", variants=["Docka"])])
    assert "docka" in known and "docker" in known
    qs = select_questions([_item(1, 4, 4, "Docker")], WS, SEGS, {}, known=known)
    assert qs == []


def test_overlay_text_is_used() -> None:
    qs = select_questions([_item(1, 2, 2)], WS, SEGS, {2: "Kubernetes"}, known=set())
    assert qs[0].text == "Kubernetes"
    assert transcript_lines(SEGS[:1], WS, {2: "Kubernetes"}).startswith(
        "1: [0]Wir [1]nutzen [2]Kubernetes"
    )


@pytest.mark.parametrize(
    ("line", "term", "variants"),
    [
        ("Kubernetes = Kubanetis, Cuber Netties", "Kubernetes", ("Kubanetis", "Cuber Netties")),
        ("Docker", "Docker", ()),
        ("„VS Code“ = vs kot | Visual Studio Code", "VS Code", ("vs kot", "Visual Studio Code")),
        ("Helm = Hellm, Hellm", "Helm", ("Hellm",)),
    ],
)
def test_parse_entry(line: str, term: str, variants: tuple[str, ...]) -> None:
    entry = parse_entry(line)
    assert (entry.term, entry.variants) == (term, variants)


def test_parse_lines_collects_unreadable() -> None:
    entries, bad = parse_lines("Kubernetes = Kubanetis\n\n- Docker\n= nur Variante\n")
    assert [e.term for e in entries] == ["Kubernetes", "Docker"]
    assert bad == ["= nur Variante"]


@pytest.mark.parametrize(
    ("find", "replace", "ok"),
    [
        ("Kubanetis", "Kubernetes", True),
        ("Cuber Netties", "Kubernetes", True),
        ("kubernetes", "Kubernetes", False),  # case only
        ("Docker,", "Docker", False),  # punctuation only
        ("das", "ein ganz anderer langer Satz hier", False),  # too long for a term
        ("x", "", False),
    ],
)
def test_glossary_candidate(find: str, replace: str, ok: bool) -> None:
    assert is_glossary_candidate(find, replace) is ok
