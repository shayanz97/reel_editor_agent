"""Glossary persistence and CSV import/export."""

from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from reels.db.models import GlossaryEntry, GlossaryStatus
from reels.domain.glossary import GlossaryTerm

CSV_FIELDS = ["term", "variants", "category", "note", "highlight", "status"]
VARIANT_SEP = "|"


def load_terms(session: Session, client_id: int) -> list[GlossaryTerm]:
    rows = session.scalars(
        select(GlossaryEntry)
        .where(GlossaryEntry.client_id == client_id)
        .order_by(GlossaryEntry.term)
    )
    return [
        GlossaryTerm(
            term=r.term,
            variants=list(r.variants),
            category=r.category,
            note=r.note,
            highlight=r.highlight,
            source=r.source,
            status=r.status.value,
            hits=r.hits,
        )
        for r in rows
    ]


def add_hits(session: Session, client_id: int, hits: Counter[str]) -> None:
    for term, count in hits.items():
        session.execute(
            update(GlossaryEntry)
            .where(GlossaryEntry.client_id == client_id, GlossaryEntry.term == term)
            .values(hits=GlossaryEntry.hits + count)
        )


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "ja", "yes", "x"}


def parse_csv(text: str) -> list[GlossaryTerm]:
    """CSV with header `term,variants,category,note,highlight,status`; variants separated by `|`.

    Only `term` is required. Missing status means confirmed (the client provided the list).
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if reader.fieldnames is None or "term" not in reader.fieldnames:
        raise ValueError("CSV needs a header with at least a 'term' column")
    terms: list[GlossaryTerm] = []
    for line_no, row in enumerate(reader, start=2):
        term = (row.get("term") or "").strip()
        if not term:
            continue
        status = (row.get("status") or "confirmed").strip() or "confirmed"
        if status not in ("confirmed", "suggested"):
            raise ValueError(f"line {line_no}: invalid status {status!r}")
        variants = [v.strip() for v in (row.get("variants") or "").split(VARIANT_SEP) if v.strip()]
        terms.append(
            GlossaryTerm(
                term=term,
                variants=variants,
                category=(row.get("category") or "").strip() or None,
                note=(row.get("note") or "").strip() or None,
                highlight=_bool(row.get("highlight") or ""),
                source="import",
                status=status,  # type: ignore[arg-type]
            )
        )
    return terms


def import_terms(
    session: Session, client_id: int, terms: Iterable[GlossaryTerm]
) -> tuple[int, int]:
    """Insert new terms, update existing ones (by term). Hits are kept. Returns (added, updated)."""
    existing = {
        e.term: e
        for e in session.scalars(select(GlossaryEntry).where(GlossaryEntry.client_id == client_id))
    }
    added = updated = 0
    for t in terms:
        entry = existing.get(t.term)
        if entry is None:
            entry = GlossaryEntry(client_id=client_id, term=t.term)
            session.add(entry)
            existing[t.term] = entry
            added += 1
        else:
            updated += 1
        entry.variants = t.variants
        entry.category = t.category
        entry.note = t.note
        entry.highlight = t.highlight
        entry.source = t.source
        entry.status = GlossaryStatus(t.status)
    session.flush()
    return added, updated


def export_csv(terms: Iterable[GlossaryTerm]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=[*CSV_FIELDS, "hits"], lineterminator="\n")
    writer.writeheader()
    for t in terms:
        writer.writerow(
            {
                "term": t.term,
                "variants": VARIANT_SEP.join(t.variants),
                "category": t.category or "",
                "note": t.note or "",
                "highlight": "true" if t.highlight else "false",
                "status": t.status,
                "hits": t.hits,
            }
        )
    return out.getvalue()
