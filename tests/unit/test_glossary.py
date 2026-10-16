from reels.domain.glossary import GlossaryTerm, apply_glossary, build_prompt_hint
from tests.unit.helpers import words


def test_hint_prefers_hits_then_highlight_and_skips_suggested() -> None:
    terms = [
        GlossaryTerm(term="Zebra", hits=0),
        GlossaryTerm(term="Kubernetes", hits=5),
        GlossaryTerm(term="Docker", hits=5, highlight=True),
        GlossaryTerm(term="Helm", status="suggested", hits=99),
    ]
    assert build_prompt_hint(terms) == "Fachbegriffe: Docker, Kubernetes, Zebra."


def test_hint_respects_length_limit() -> None:
    terms = [GlossaryTerm(term=f"Begriff{i:02d}", hits=100 - i) for i in range(100)]
    hint = build_prompt_hint(terms, max_chars=60)
    assert hint is not None
    assert len(hint) <= 60
    assert hint.startswith("Fachbegriffe: Begriff00, Begriff01")


def test_hint_none_without_confirmed_terms() -> None:
    assert build_prompt_hint([GlossaryTerm(term="X", status="suggested")]) is None
    assert build_prompt_hint([]) is None


def test_single_word_variant_case_insensitive_keeps_punctuation() -> None:
    ws = words(("Wir", 0, 1), ("nutzen", 1, 2), ("„kubernätes,", 2, 3))
    overlay, hits = apply_glossary(ws, [GlossaryTerm(term="Kubernetes", variants=["Kubernätes"])])
    assert overlay == {2: "„Kubernetes,"}
    assert hits["Kubernetes"] == 1


def test_multi_word_variant_to_single_term() -> None:
    ws = words(("mit", 0, 1), ("Cuber", 1, 2), ("Netties.", 2, 3), ("Gut", 3, 4))
    overlay, hits = apply_glossary(
        ws, [GlossaryTerm(term="Kubernetes", variants=["cuber netties"])]
    )
    assert overlay == {1: "Kubernetes.", 2: ""}
    assert hits["Kubernetes"] == 1


def test_word_boundaries_are_respected() -> None:
    ws = words(("Dockerfile", 0, 1), ("dockers", 1, 2))
    overlay, hits = apply_glossary(ws, [GlossaryTerm(term="Docker", variants=["docka"])])
    assert overlay == {}
    assert not hits


def test_term_case_is_fixed_and_correct_spelling_counts_as_hit() -> None:
    ws = words(("docker", 0, 1), ("Docker", 1, 2))
    overlay, hits = apply_glossary(ws, [GlossaryTerm(term="Docker")])
    assert overlay == {0: "Docker"}
    assert hits["Docker"] == 2


def test_suggested_terms_have_no_effect() -> None:
    ws = words(("docka", 0, 1))
    overlay, _ = apply_glossary(
        ws, [GlossaryTerm(term="Docker", variants=["docka"], status="suggested")]
    )
    assert overlay == {}


def test_longest_match_wins_and_existing_overlay_is_kept() -> None:
    ws = words(("Visual", 0, 1), ("Studio", 1, 2), ("Code", 2, 3), ("toll", 3, 4))
    terms = [
        GlossaryTerm(term="Visual Studio", variants=["visual studio"]),
        GlossaryTerm(term="VS Code", variants=["visual studio code"]),
    ]
    overlay, hits = apply_glossary(ws, terms, overlay={3: "super"})
    assert overlay == {0: "VS Code", 1: "", 2: "", 3: "super"}
    assert hits == {"VS Code": 1}


def test_multi_word_term_from_single_word() -> None:
    ws = words(("chatgpt", 0, 1))
    overlay, _ = apply_glossary(ws, [GlossaryTerm(term="Chat GPT", variants=["chatgpt"])])
    assert overlay == {0: "Chat GPT"}
