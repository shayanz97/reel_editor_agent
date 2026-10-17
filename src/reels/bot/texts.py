"""All bot texts. Clients: German, per Du, technical and short. Admin: German, terse."""

from __future__ import annotations

from collections.abc import Sequence

from reels.domain.corrections import (
    AnyOperation,
    DeleteSegment,
    DeleteWords,
    Highlight,
    Issue,
    KeepPause,
    MoveSubtitles,
    Note,
    ReplaceText,
    RestoreSegment,
    SplitSegment,
    Unhighlight,
)
from reels.domain.segments import Segment

TELEGRAM_TEXT_LIMIT = 4096

# --- clients ---------------------------------------------------------------------

START = (
    "Hi! Schick mir dein Video (bis 3 Minuten, Hochformat).\n"
    "Am besten als Datei: Büroklammer → Datei. So bleibt die Qualität erhalten.\n\n"
    "Ich schneide Pausen raus, setze Untertitel und schicke dir eine Vorschau zur Freigabe."
)
NOT_ALLOWED = (
    "Dieser Bot ist nur für freigeschaltete Kunden. Bitte melde dich bei deinem Ansprechpartner."
)
NOT_A_VIDEO = "Ich kann nur Videos verarbeiten. Schick mir dein Video am besten als Datei."
COMPRESSED_HINT = (
    "Tipp: Schick Videos als Datei (Büroklammer → Datei), dann komprimiert Telegram sie nicht. "
    "Dieses Video verarbeite ich trotzdem."
)
DOWNLOADING = "⏳ Video wird geladen …"
DOWNLOAD_FAILED = "❌ Das Video konnte nicht geladen werden. Bitte schick es nochmal."
RECEIVED = "✅ Video empfangen. Ich melde mich, sobald die Vorschau fertig ist."
PROCESSING = "⚙️ Video in Bearbeitung: Pausen schneiden, Untertitel setzen …"
PREVIEW_READY_STATUS = "✅ Vorschau fertig (Version {number})."
PREVIEW_CAPTION = "Vorschau · Version {number}"
PREVIEW_QUESTION = (
    "Passt alles? Dann tippe auf „Freigeben“. "
    "Wenn nicht, schreib mir einfach, was anders sein soll."
)
BUTTON_APPROVE = "✅ Freigeben"
BUTTON_CHANGE = "✏️ Ändern"
APPROVED = "Freigegeben 🎉 Hier ist dein fertiges Video in voller Qualität."
APPROVED_STATUS = "✅ Freigegeben (Version {number})."
ALREADY_APPROVED = "Dieses Video ist bereits freigegeben."
OUTDATED = "Diese Vorschau ist nicht mehr aktuell. Bitte nutze die neueste Version."
NOT_READY = "Diese Version ist noch nicht fertig. Einen Moment bitte."
FORBIDDEN = "Diese Vorschau gehört nicht zu deinem Konto."
CHANGE_PROMPT = (
    "Schreib mir, was geändert werden soll. Zum Beispiel:\n"
    "„In Satz 2 heißt es Kubernetes, Satz 3 raus.“"
)

# --- corrections -------------------------------------------------------------------

CORRECTION_STARTED = "⏳ Ich schaue mir deine Änderungen an …"
CORRECTION_BUSY = "Einen Moment, ich bin noch mit deiner letzten Änderung beschäftigt."
CORRECTION_NO_PROJECT = "Ich habe gerade kein Video, das du ändern kannst. Schick mir ein Video."
CORRECTION_SUMMARY = "Ich habe verstanden (bezogen auf Version {number}):"
CORRECTION_CONFIRM = "Soll ich das so umsetzen?"
BUTTON_APPLY = "✅ Ausführen"
BUTTON_RETRY = "🔁 Nochmal"
CORRECTION_APPLIED = "Alles klar, ich erstelle Version {number}."
CORRECTION_NOTES_FORWARDED = "Deinen Hinweis habe ich ans Team weitergegeben."
CORRECTION_RETRY = "Okay. Schreib mir einfach nochmal, was geändert werden soll."
CORRECTION_QUESTION = "Da bin ich mir nicht sicher (Version {number}):"
CORRECTION_QUESTION_END = "Schreib mir bitte genauer, was geändert werden soll."
CORRECTION_FAILED = (
    "❌ Deine Änderung konnte ich gerade nicht verarbeiten. Bitte schick sie nochmal."
)
CORRECTION_UNAVAILABLE = "Änderungen per Text sind gerade nicht verfügbar. Wir melden uns bei dir."

_POSITIONS = {"lower_third": "unten", "center": "in die Mitte", "upper_third": "oben"}


def describe_operation(op: AnyOperation) -> str:
    match op:
        case ReplaceText():
            return f"Satz {op.segment}: „{op.find}“ → „{op.replace}“"
        case DeleteSegment():
            return f"Satz {op.segment} herausschneiden"
        case RestoreSegment():
            return f"Entfernten Satz E{op.removed} zurückholen"
        case DeleteWords():
            return f"Satz {op.segment}: „{op.find}“ herausschneiden"
        case KeepPause():
            return f"Pause nach Satz {op.segment} nicht kürzen"
        case Highlight():
            return f"Satz {op.segment}: „{op.find}“ hervorheben"
        case Unhighlight():
            if op.find is None:
                return f"Satz {op.segment}: keine Hervorhebungen"
            return f"Satz {op.segment}: „{op.find}“ nicht mehr hervorheben"
        case MoveSubtitles():
            return f"Untertitel {_POSITIONS[op.position]} setzen"
        case SplitSegment():
            return f"Satz {op.segment} nach „{op.after}“ teilen"
        case Note():
            return f"Hinweis ans Team: {op.text}"


def correction_summary(operations: Sequence[AnyOperation], version_number: int) -> str:
    lines = [CORRECTION_SUMMARY.format(number=version_number), ""]
    lines += [f"{n}. {describe_operation(op)}" for n, op in enumerate(operations, start=1)]
    lines += ["", CORRECTION_CONFIRM]
    return "\n".join(lines)[:TELEGRAM_TEXT_LIMIT]


def describe_issue(issue: Issue) -> str:
    match issue.kind:
        case "segment_missing":
            return f"Einen Satz {issue.segment} gibt es nicht."
        case "find_missing":
            return f"In Satz {issue.segment} finde ich „{issue.find}“ nicht."
        case "find_ambiguous":
            return (
                f"„{issue.find}“ kommt in Satz {issue.segment} mehrmals vor. "
                "Welche Stelle meinst du?"
            )
        case "removed_missing":
            return f"Einen entfernten Satz E{issue.segment} gibt es nicht."
        case "split_at_end":
            return f"Satz {issue.segment} endet schon nach „{issue.find}“."
        case "nothing_to_do":
            return "Ich habe keine konkrete Änderung erkannt."


def correction_question(
    issues: Sequence[Issue], clarification: str | None, version_number: int
) -> str:
    lines = [CORRECTION_QUESTION.format(number=version_number), ""]
    lines += [f"• {describe_issue(i)}" for i in issues]
    if clarification:
        lines.append(f"• {clarification}")
    lines += ["", CORRECTION_QUESTION_END]
    return "\n".join(lines)[:TELEGRAM_TEXT_LIMIT]


FAILED_GENERIC = (
    "❌ Beim Bearbeiten ist ein Fehler aufgetreten. Wir schauen uns das an und melden uns."
)
FAILED_TOO_LONG = (
    "❌ Das Video ist {duration} lang, möglich sind bis zu {limit}. "
    "Bitte kürze es und schick es nochmal."
)
FAILED_NO_AUDIO = "❌ Das Video hat keine Tonspur. Bitte schick es mit Ton nochmal."
FAILED_NO_VIDEO = "❌ Die Datei enthält kein Video. Bitte schick dein Video nochmal."
FAILED_NO_SPEECH = (
    "❌ Ich habe im Video keine Sprache gefunden. Bitte prüf den Ton und schick es nochmal."
)


def duration(seconds: float) -> str:
    """'2:05 min' / '45 s' (German style, rounded to full seconds)."""
    total = round(seconds)
    if total < 60:
        return f"{total} s"
    return f"{total // 60}:{total % 60:02d} min"


def failure_text(code: str | None, details: dict[str, float]) -> str:
    if code == "too_long" and "duration_s" in details and "limit_s" in details:
        return FAILED_TOO_LONG.format(
            duration=duration(details["duration_s"]), limit=duration(details["limit_s"])
        )
    return {
        "no_audio": FAILED_NO_AUDIO,
        "no_video": FAILED_NO_VIDEO,
        "no_speech": FAILED_NO_SPEECH,
    }.get(code or "", FAILED_GENERIC)


def preview_messages(
    segments: Sequence[Segment],
    removed: Sequence[str] = (),
    limit: int = TELEGRAM_TEXT_LIMIT,
) -> list[str]:
    """Numbered sentences (+ removed ones as E1 …) and the question, split below the limit."""
    lines = ["Sätze in dieser Version:", ""]
    lines += [f"{s.number}. {s.text}" for s in segments]
    if removed:
        lines += ["", "Entfernte Sätze:"]
        lines += [f"E{n}. {text}" for n, text in enumerate(removed, start=1)]
    lines += ["", PREVIEW_QUESTION]
    messages: list[str] = []
    current = ""
    for line in lines:
        if len(line) > limit:
            line = line[: limit - 1] + "…"
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            messages.append(current)
            current = line
        else:
            current = candidate
    messages.append(current)
    return messages


# --- admin -----------------------------------------------------------------------

ADMIN_UNKNOWN_USER = (
    "👤 Unbekannter Nutzer schreibt dem Bot: {name} ({username}), ID {user_id}.\n"
    "Freischalten: /kunde_user <kunde> {user_id}"
)
ADMIN_JOB_FAILED = (
    "❌ Job {job_id} ({step}) fehlgeschlagen nach {attempts} Versuch(en)\n"
    "Kunde: {client} · Projekt {project} · Version {version}\n"
    "Ursache: {cause}"
)
ADMIN_NOTE = "📝 Hinweis von {client} (Projekt {project_id}, Version {number}): {text}"
ADMIN_DOWNLOAD_FAILED = "❌ Download fehlgeschlagen ({client}, Nachricht {message_id}): {cause}"
ADMIN_HELP = (
    "Admin-Befehle:\n"
    "/kunde_neu <Name> [| Telegram-ID …] – Kunde anlegen\n"
    "/kunde_user <kunde> <Telegram-ID> – Nutzer freischalten\n"
    "/status – Queue und Projekte"
)
ADMIN_CLIENT_CREATED = "Kunde angelegt: {name} (Kürzel {slug}, ID {id}). Nutzer: {users}"
ADMIN_USER_ADDED = "Nutzer {user_id} für {name} ({slug}) freigeschaltet."
ADMIN_USAGE_NEW = (
    "Nutzung: /kunde_neu <Name> [| Telegram-ID …], z. B. /kunde_neu Müller GmbH | 12345678"
)
ADMIN_USAGE_USER = "Nutzung: /kunde_user <kunde> <Telegram-ID>"
ADMIN_ERROR = "Fehler: {error}"


def admin_job_failed(
    job_id: int,
    step: str,
    attempts: int,
    error: str,
    client: str | None,
    project_id: int | None,
    version: int | None,
) -> str:
    cause = (error.splitlines()[0] if error else "unbekannt")[:300]
    return ADMIN_JOB_FAILED.format(
        job_id=job_id,
        step=step,
        attempts=attempts,
        client=client or "–",
        project=project_id if project_id is not None else "–",
        version=version if version is not None else "–",
        cause=cause,
    )


def admin_status(jobs: dict[str, int], projects: dict[str, int], failed_24h: int,
                 oldest_queued_s: float | None) -> str:  # fmt: skip
    def fmt(counts: dict[str, int]) -> str:
        return ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "–"

    waiting = duration(oldest_queued_s) if oldest_queued_s is not None else "–"
    return (
        f"Jobs: {fmt(jobs)}\n"
        f"Fehlgeschlagen (24 h): {failed_24h}\n"
        f"Ältester wartender Job: {waiting}\n"
        f"Projekte: {fmt(projects)}"
    )
