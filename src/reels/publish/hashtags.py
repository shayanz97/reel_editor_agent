"""Hashtag suggestions: cleaned, deduplicated, limited. Suggestions only, no reach promises."""

from __future__ import annotations

import re
from collections.abc import Sequence

from reels.domain.glossary import GlossaryTerm

_NOT_WORD = re.compile(r"[^\w]", re.UNICODE)


def normalize(raw: Sequence[str], max_count: int, terms: Sequence[GlossaryTerm] = ()) -> list[str]:
    """'#Künstliche Intelligenz' -> '#KünstlicheIntelligenz'. Drops empty and digit-only tags,
    duplicates (ignoring case) and everything beyond `max_count`. Glossary terms keep their
    spelling (e.g. '#kubernetes' -> '#Kubernetes')."""
    spelling = {_NOT_WORD.sub("", t.term).casefold(): _NOT_WORD.sub("", t.term) for t in terms}
    seen: set[str] = set()
    result: list[str] = []
    for tag in raw:
        body = _NOT_WORD.sub("", tag.strip().lstrip("#"))
        key = body.casefold()
        if not body or body.isdigit() or key in seen:
            continue
        seen.add(key)
        result.append("#" + spelling.get(key, body))
        if len(result) >= max_count:
            break
    return result
