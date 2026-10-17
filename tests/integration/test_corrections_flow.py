"""Etappe 4 (Teil 1) end to end: free text -> plan -> confirmation -> new version."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from reels.bot import service, texts
from reels.bot.handlers import client
from reels.config import get_settings
from reels.db.models import Correction, CorrectionStatus, Job, Version, VersionStatus
from tests.integration.bot_fakes import (
    ADMIN,
    ME,
    FakeBot,
    FakeCallback,
    FakeMessage,
    _doc,
    _go,
    _tick,
)
from tests.integration.test_pipeline_synthetic import _run

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

# Synthetic transcript: 1 = wort0..wort4.  2 = wort5..wort10.
#                       3 = wort11..wort14. 4 = wort15..wort19.
EXAMPLE = {
    "operations": [
        {"op": "replace_text", "segment": 2, "find": "wort6", "replace": "Kubernetes"},
        {"op": "delete_segment", "segment": 3},
    ],
    "clarification": None,
}


@pytest.fixture
def ready(bot_env: tuple[FakeBot, service.Access, Path], engine: Engine, tmp_path: Path,
          monkeypatch: pytest.MonkeyPatch) -> tuple[FakeBot, service.Access]:  # fmt: skip
    """Video received, version 1 rendered, preview sent."""
    bot, access, _ = bot_env
    monkeypatch.setenv("REELS_TEST_LLM", "1")
    monkeypatch.setenv("REELS_TEST_PROMPTS", str(tmp_path / "prompts.txt"))
    _go(
        client.on_document(
            FakeMessage(ME, 10, document=_doc()), bot, access, engine, get_settings()
        )
    )  # type: ignore[arg-type]
    _run()
    _go(_tick(bot, engine))
    return bot, access


def _say(bot: FakeBot, access: service.Access, engine: Engine, text: str, message_id: int,
         reply_to: int | None = None) -> FakeMessage:  # fmt: skip
    reply = type("M", (), {"message_id": reply_to})() if reply_to else None
    msg = FakeMessage(ME, message_id, text=text, reply_to_message=reply)
    _go(client.on_text(msg, access, engine))  # type: ignore[arg-type]
    return msg


def _llm_answers(monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]) -> None:
    monkeypatch.setenv("REELS_TEST_CORRECTION", json.dumps(answer))


def _click(bot: FakeBot, access: service.Access, engine: Engine, data: str) -> FakeCallback:
    cb = FakeCallback(data, ME)
    _go(client.on_button(cb, bot, access, engine, get_settings()))  # type: ignore[arg-type]
    return cb


def _jobs(engine: Engine, number: int) -> list[str]:
    with Session(engine) as s:
        v = s.scalars(select(Version).where(Version.number == number)).one()
        return [
            j.step for j in s.scalars(select(Job).where(Job.version_id == v.id).order_by(Job.id))
        ]


def test_example_text_fix_and_delete_creates_version_2(
    ready: tuple[FakeBot, service.Access], engine: Engine, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    bot, access = ready
    preview_v1 = bot.messages[-1]
    _llm_answers(monkeypatch, EXAMPLE)

    msg = _say(bot, access, engine, "In 2 heißt es Kubernetes, Satz 3 raus", 50)
    assert msg.answers == [texts.CORRECTION_STARTED]
    _run()  # worker interprets
    assert "In 2 heißt es Kubernetes, Satz 3 raus" in (tmp_path / "prompts.txt").read_text()
    with Session(engine) as s:
        assert s.scalars(select(Version)).all()[-1].number == 1, (
            "nothing happens without confirmation"
        )

    assert _go(_tick(bot, engine)) == 1
    summary = bot.messages[-1]
    assert "1. Satz 2: „wort6“ → „Kubernetes“" in summary.text
    assert "2. Satz 3 herausschneiden" in summary.text
    apply_button, retry_button = summary.reply_markup.inline_keyboard[0]

    _click(bot, access, engine, apply_button.callback_data)
    assert bot.messages[-2].text == texts.CORRECTION_APPLIED.format(number=2)
    glossary_question = bot.messages[-1]
    assert glossary_question.text == texts.GLOSSARY_ASK.format(find="wort6", replace="Kubernetes")
    assert _jobs(engine, 2) == ["cutplan"]
    _run()
    # Transcription never runs again; segment deletion re-cuts.
    assert _jobs(engine, 2) == ["cutplan", "segments", "highlights", "subtitles", "render"]

    with Session(engine) as s:
        v1, v2 = s.scalars(select(Version).order_by(Version.number)).all()
        assert v2.status is VersionStatus.READY and v2.parent_id == v1.id
        assert v2.text_overlay == {"6": "Kubernetes"}
        assert v2.edits["removed_words"] == [11, 12, 13, 14]
        assert v2.highlights == v1.highlights  # kept, no new LLM call for highlights
        out1 = sum(r["end"] - r["start"] for r in v1.cut_plan["keep"])  # type: ignore[index]
        out2 = sum(r["end"] - r["start"] for r in v2.cut_plan["keep"])  # type: ignore[index]
        assert out1 - out2 == pytest.approx(0.8 + 0.3, abs=0.2)  # sentence 3 and its pause
        correction = s.scalars(select(Correction)).one()
        assert (
            correction.status is CorrectionStatus.APPLIED and correction.result_version_id == v2.id
        )

    assert _go(_tick(bot, engine)) == 1
    preview = bot.messages[-1]
    assert "2. wort5 Kubernetes wort7" in preview.text
    assert "Entfernte Sätze:" in preview.text and "E1. wort11" in preview.text
    assert "4. " not in preview.text.split("Entfernte")[0]

    # Old preview: approve and corrections refused (4.6).
    old_approve = preview_v1.reply_markup.inline_keyboard[0][0].callback_data
    assert _click(bot, access, engine, old_approve).alerts == [texts.OUTDATED]
    old_reply = _say(bot, access, engine, "Satz 1 raus", 51, reply_to=preview_v1.message_id)
    assert old_reply.answers == [texts.OUTDATED]
    assert _click(bot, access, engine, retry_button.callback_data).alerts == [texts.OUTDATED]


def test_text_fix_does_not_recut(
    ready: tuple[FakeBot, service.Access], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, access = ready
    _llm_answers(monkeypatch, {"operations": [
        {"op": "replace_text", "segment": 1, "find": "wort0", "replace": "Hallo"},
        {"op": "move_subtitles", "position": "upper_third"},
    ], "clarification": None})  # fmt: skip
    _say(bot, access, engine, "Satz 1 Hallo statt wort0, Untertitel nach oben", 60)
    _run()
    _go(_tick(bot, engine))
    _click(bot, access, engine, bot.messages[-1].reply_markup.inline_keyboard[0][0].callback_data)
    _run()
    assert _jobs(engine, 2) == ["segments", "highlights", "subtitles", "render"]
    with Session(engine) as s:
        v2 = s.scalars(select(Version).where(Version.number == 2)).one()
        assert v2.subtitle_style is not None and v2.subtitle_style["position"] == "upper_third"
        cutplan_jobs = s.scalars(select(Job).where(Job.step == "cutplan")).all()
        assert len(cutplan_jobs) == 1  # only version 1 was cut


def test_unclear_request_asks_back_and_answer_completes_it(
    ready: tuple[FakeBot, service.Access], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, access = ready
    _llm_answers(monkeypatch, {"operations": [
        {"op": "replace_text", "segment": 2, "find": "Docker", "replace": "Kubernetes"},
    ], "clarification": None})  # fmt: skip
    _say(bot, access, engine, "Docker soll Kubernetes heißen", 70)
    _run()
    _go(_tick(bot, engine))
    question = bot.messages[-1].text
    assert "In Satz 2 finde ich „Docker“ nicht." in question
    assert bot.messages[-1].reply_markup is None

    _llm_answers(monkeypatch, {"operations": [], "clarification": "Welcher Satz ist gemeint?"})
    _say(bot, access, engine, "Ich meine Satz 4", 71)
    _run()
    with Session(engine) as s:
        latest = s.scalars(select(Correction).order_by(Correction.id.desc())).first()
        assert latest is not None
        assert latest.message_text == "Docker soll Kubernetes heißen\nNachtrag: Ich meine Satz 4"
        assert latest.status is CorrectionStatus.NEEDS_CLARIFICATION
    _go(_tick(bot, engine))
    assert "Welcher Satz ist gemeint?" in bot.messages[-1].text


def test_retry_note_busy_and_no_project(
    ready: tuple[FakeBot, service.Access], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, access = ready
    _llm_answers(
        monkeypatch, {"operations": [{"op": "delete_segment", "segment": 4}], "clarification": None}
    )
    _say(bot, access, engine, "Satz 4 raus", 80)
    assert _say(bot, access, engine, "Und Satz 1 auch", 81).answers == [texts.CORRECTION_BUSY]
    _run()
    _go(_tick(bot, engine))
    retry = bot.messages[-1].reply_markup.inline_keyboard[0][1].callback_data
    _click(bot, access, engine, retry)
    assert bot.messages[-1].text == texts.CORRECTION_RETRY
    with Session(engine) as s:
        assert len(s.scalars(select(Version)).all()) == 1

    _llm_answers(
        monkeypatch,
        {"operations": [{"op": "note", "text": "Logo oben rechts"}], "clarification": None},
    )
    _say(bot, access, engine, "Kannst du mein Logo einblenden?", 82)
    _run()
    _go(_tick(bot, engine))
    assert "Hinweis ans Team: Logo oben rechts" in bot.messages[-1].text
    _click(bot, access, engine, bot.messages[-1].reply_markup.inline_keyboard[0][0].callback_data)
    assert bot.messages[-1].text == texts.CORRECTION_NOTES_FORWARDED
    assert any(m.chat_id == ADMIN and "Logo oben rechts" in m.text for m in bot.messages)
    with Session(engine) as s:
        assert len(s.scalars(select(Version)).all()) == 1  # a note changes nothing in the video

    # After approval, text is no longer a correction.
    approve = next(m for m in bot.messages if m.reply_markup and "approve" in
                   m.reply_markup.inline_keyboard[0][0].callback_data)  # fmt: skip
    _click(bot, access, engine, approve.reply_markup.inline_keyboard[0][0].callback_data)
    assert _say(bot, access, engine, "Doch noch was", 83).answers == [texts.ALREADY_APPROVED]


def test_without_llm_the_client_is_told(
    bot_env: tuple[FakeBot, service.Access, Path], engine: Engine
) -> None:
    bot, access, _ = bot_env
    assert _say(bot, access, engine, "Satz 1 raus", 90).answers == [texts.CORRECTION_NO_PROJECT]
    _go(
        client.on_document(
            FakeMessage(ME, 10, document=_doc()), bot, access, engine, get_settings()
        )
    )  # type: ignore[arg-type]
    _run()
    _go(_tick(bot, engine))
    _say(bot, access, engine, "Satz 1 raus", 91)
    _run()  # REELS_TEST_LLM not set: no LLM
    _go(_tick(bot, engine))
    assert bot.messages[-1].text == texts.CORRECTION_UNAVAILABLE


def test_confirmed_glossary_fix_applies_to_next_video(
    ready: tuple[FakeBot, service.Access], engine: Engine, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """4.7 acceptance: „Ja, immer“ -> the next video gets the spelling automatically."""
    bot, access = ready
    _llm_answers(monkeypatch, {"operations": [
        {"op": "replace_text", "segment": 2, "find": "wort6", "replace": "Kubernetes"},
    ], "clarification": None})  # fmt: skip
    _say(bot, access, engine, "wort6 heißt Kubernetes", 100)
    _run()
    _go(_tick(bot, engine))
    _click(bot, access, engine, bot.messages[-1].reply_markup.inline_keyboard[0][0].callback_data)
    question = bot.messages[-1]
    yes, no = question.reply_markup.inline_keyboard[0]
    _click(bot, access, engine, yes.callback_data)
    assert bot.messages[-1].text == texts.GLOSSARY_ASK_SAVED.format(
        find="wort6", replace="Kubernetes"
    )
    assert _click(bot, access, engine, no.callback_data).alerts == [None]  # no error after yes

    from reels.db.glossary import load_terms

    with Session(engine) as s:
        (term,) = load_terms(s, access.client_id)
    assert (term.term, term.variants, term.status, term.source) == (
        "Kubernetes", ["wort6"], "confirmed", "correction",
    )  # fmt: skip

    # Next video: same misheard word, corrected without asking.
    from tests.integration.bot_fakes import CONTAINER

    upload = Path(get_settings().telegram_files_host_path) / "TOKEN" / "documents" / "file_2.mp4"
    shutil.copy(tmp_path / "src.mp4", upload)
    bot.file_path = f"{CONTAINER}/TOKEN/documents/file_2.mp4"
    _go(
        client.on_document(
            FakeMessage(ME, 200, document=_doc()), bot, access, engine, get_settings()
        )
    )  # type: ignore[arg-type]
    _run()
    with Session(engine) as s:
        newest = s.scalars(select(Version).order_by(Version.id.desc())).first()
        assert newest is not None and newest.number == 1
        assert newest.text_overlay.get("6") == "Kubernetes"
