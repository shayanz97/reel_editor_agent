"""Instagram post text: hook, value, call to action, hashtags."""

from __future__ import annotations

from collections.abc import Sequence

from reels.domain.glossary import GlossaryTerm, apply_glossary_text
from reels.publish.hashtags import normalize

TONES = {
    "du_fachlich": "per Du, fachlich, sachlich und knapp",
}


def tone_description(tone: str) -> str:
    """Known tones get a description for the prompt; anything else is passed on as written."""
    return TONES.get(tone, tone)


def finalize(
    hook: str,
    body: str,
    cta: str,
    hashtags: Sequence[str],
    terms: Sequence[GlossaryTerm],
    max_hashtags: int,
) -> dict[str, object]:
    """Glossary spellings applied to all parts, hashtags cleaned. Returns the post dict."""
    return {
        "hook": apply_glossary_text(hook.strip(), terms),
        "body": apply_glossary_text(body.strip(), terms),
        "cta": apply_glossary_text(cta.strip(), terms),
        "hashtags": normalize(hashtags, max_hashtags, terms),
    }


def compose(post: dict[str, object]) -> str:
    """The text the client copies to Instagram."""
    parts = [str(post.get(k) or "").strip() for k in ("hook", "body", "cta")]
    tags = " ".join(str(t) for t in post.get("hashtags") or [])  # type: ignore[attr-defined]
    return "\n\n".join(p for p in [*parts, tags] if p)


def fallback_post(
    first_sentence: str, terms: Sequence[GlossaryTerm], max_hashtags: int
) -> dict[str, object]:
    """Without LLM: the first sentence as hook, glossary terms as hashtags."""
    return finalize(first_sentence, "", "", [t.term for t in terms if t.status == "confirmed"],
                    terms, max_hashtags)  # fmt: skip
