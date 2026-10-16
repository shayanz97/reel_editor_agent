"""Pure helpers for names and identifiers."""

from __future__ import annotations

import re
import unicodedata

_GERMAN = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_NON_SLUG = re.compile(r"[^a-z0-9]+")

SLUG_MAX_LEN = 64


def slugify(name: str) -> str:
    """Turn a client name into a stable, URL- and path-safe slug.

    German umlauts are transliterated (``Müller & Söhne`` -> ``mueller-soehne``).
    Raises ``ValueError`` if nothing usable is left.
    """
    text = name.strip().lower().translate(_GERMAN)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = _NON_SLUG.sub("-", text).strip("-")[:SLUG_MAX_LEN].rstrip("-")
    if not slug:
        raise ValueError(f"cannot derive a slug from {name!r}")
    return slug
