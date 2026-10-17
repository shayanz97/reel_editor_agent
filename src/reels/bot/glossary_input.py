"""Parsing glossary entries typed in Telegram: `Begriff = Variante 1, Variante 2`."""

from __future__ import annotations

from dataclasses import dataclass

MAX_TERM_LEN = 200


@dataclass(frozen=True)
class EntryInput:
    term: str
    variants: tuple[str, ...]


def parse_entry(line: str) -> EntryInput:
    """'Kubernetes = Kubanetis, Cuber Netties' -> term + variants. Variants are optional."""
    term, _, rest = line.partition("=")
    term = term.strip().strip("„“\"'")
    if not term or len(term) > MAX_TERM_LEN:
        raise ValueError(line)
    parts = rest.replace("|", ",").split(",")
    variants = tuple(dict.fromkeys(p.strip().strip("„“\"'") for p in parts if p.strip()))
    return EntryInput(term, variants)


def parse_lines(text: str) -> tuple[list[EntryInput], list[str]]:
    """One entry per line; returns (entries, lines that could not be read)."""
    entries: list[EntryInput] = []
    bad: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-•*").strip()
        if not line:
            continue
        try:
            entries.append(parse_entry(line))
        except ValueError:
            bad.append(line)
    return entries, bad
