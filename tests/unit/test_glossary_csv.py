import pytest

from reels.db.glossary import export_csv, parse_csv
from reels.domain.glossary import GlossaryTerm


def test_parse_csv_full_and_minimal_rows() -> None:
    text = (
        "﻿term,variants,category,note,highlight,status\n"
        "Kubernetes,Cuber Netties|Kubernätes,tool,Container,ja,\n"
        "Helm,,,,,suggested\n"
        ",ignored,,,,\n"
    )
    terms = parse_csv(text)
    assert [t.term for t in terms] == ["Kubernetes", "Helm"]
    k, h = terms
    assert k.variants == ["Cuber Netties", "Kubernätes"]
    assert (k.category, k.note, k.highlight, k.status, k.source) == (
        "tool",
        "Container",
        True,
        "confirmed",
        "import",
    )
    assert (h.variants, h.highlight, h.status) == ([], False, "suggested")


def test_parse_csv_only_term_column() -> None:
    assert [t.term for t in parse_csv("term\nDocker\n")] == ["Docker"]


def test_parse_csv_errors() -> None:
    with pytest.raises(ValueError, match="header"):
        parse_csv("begriff\nDocker\n")
    with pytest.raises(ValueError, match="line 2"):
        parse_csv("term,status\nDocker,maybe\n")


def test_export_roundtrip() -> None:
    terms = [
        GlossaryTerm(term="Kubernetes", variants=["k8s", "Cuber Netties"], highlight=True, hits=3)
    ]
    text = export_csv(terms)
    assert text.splitlines()[0] == "term,variants,category,note,highlight,status,hits"
    (back,) = parse_csv(text)
    assert (back.term, back.variants, back.highlight) == (
        "Kubernetes",
        ["k8s", "Cuber Netties"],
        True,
    )
