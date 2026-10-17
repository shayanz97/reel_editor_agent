"""Etappe 4 Teil 2: glossary commands (4.9) and the vocabulary check before cutting (4.8)."""

import json
import shutil
from pathlib import Path

import pytest
from aiogram.filters import CommandObject
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from reels.bot import service, texts
from reels.bot.handlers import client
from reels.bot.handlers import glossary as glossary_handlers
from reels.config import get_settings
from reels.db.glossary import load_terms
from reels.db.models import Job, Project, Version, VersionStatus
from reels.pipeline.runner import create_project, rerun_from
from tests.integration.bot_fakes import ME, FakeBot, FakeCallback, FakeMessage, _doc, _go, _tick
from tests.integration.test_pipeline_synthetic import _run

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

# Synthetic transcript: segment 2 = wort5..wort10., segment 4 = wort15..wort19.
VOCAB = {
    "items": [
        {
            "segment": 2,
            "first_word_id": 6,
            "last_word_id": 6,
            "suggestion": "Kubernetes",
            "reason": "t",
        },
        {"segment": 4, "first_word_id": 16, "last_word_id": 16, "suggestion": None, "reason": "t"},
    ]
}


def _cmd(name: str, args: str | None) -> CommandObject:
    return CommandObject(command=name, args=args)


def _steps(engine: Engine, version_id: int) -> list[str]:
    with Session(engine) as s:
        return [
            j.step
            for j in s.scalars(select(Job).where(Job.version_id == version_id).order_by(Job.id))
        ]


# --- 4.9 ----------------------------------------------------------------------------------


def test_glossary_commands(bot_env: tuple[FakeBot, service.Access, Path], engine: Engine) -> None:
    bot, access, _ = bot_env
    m = FakeMessage(ME, 1)

    _go(glossary_handlers.on_list(m, access, engine))  # type: ignore[arg-type]
    _go(
        glossary_handlers.on_new(
            m, _cmd("begriff_neu", "Kubernetes = Kubanetis, Cuber Netties"), access, engine
        )
    )  # type: ignore[arg-type]
    _go(glossary_handlers.on_new(m, _cmd("begriff_neu", "kubernetes = K8s"), access, engine))  # type: ignore[arg-type]
    _go(glossary_handlers.on_new(m, _cmd("begriff_neu", None), access, engine))  # type: ignore[arg-type]
    _go(
        glossary_handlers.on_import(
            m, _cmd("begriffe", "Docker = Docka\nHelm\n= kaputt"), access, engine
        )
    )  # type: ignore[arg-type]
    _go(glossary_handlers.on_change(m, _cmd("begriff_aendern", "Docker = Dockr"), access, engine))  # type: ignore[arg-type]
    _go(glossary_handlers.on_change(m, _cmd("begriff_aendern", "Gibtsnicht = x"), access, engine))  # type: ignore[arg-type]
    _go(glossary_handlers.on_delete(m, _cmd("begriff_loeschen", "helm"), access, engine))  # type: ignore[arg-type]
    assert m.answers == [
        texts.GLOSSARY_EMPTY,
        texts.GLOSSARY_ADDED.format(term="Kubernetes"),
        texts.GLOSSARY_UPDATED.format(term="kubernetes"),
        texts.GLOSSARY_USAGE_NEW,
        texts.GLOSSARY_IMPORTED.format(added=2, updated=0),
        texts.GLOSSARY_UNREADABLE.format(lines="= kaputt"),
        texts.GLOSSARY_UPDATED.format(term="Docker"),
        texts.GLOSSARY_NOT_FOUND.format(term="Gibtsnicht"),
        texts.GLOSSARY_DELETED.format(term="helm"),
    ]
    with Session(engine) as s:
        terms = {t.term: t for t in load_terms(s, access.client_id)}
    assert terms["Kubernetes"].variants == ["Kubanetis", "Cuber Netties", "K8s"]
    assert terms["Docker"].variants == ["Dockr"] and set(terms) == {"Kubernetes", "Docker"}

    listing = FakeMessage(ME, 2)
    _go(glossary_handlers.on_list(listing, access, engine))  # type: ignore[arg-type]
    assert "• Kubernetes ← Kubanetis, Cuber Netties, K8s" in listing.answers[0]

    _go(glossary_handlers.on_export(m, bot, access, engine))  # type: ignore[arg-type]
    csv_text = bot.document_data[-1].decode("utf-8-sig")
    assert (
        csv_text.startswith("term,variants")
        and "Kubernetes,Kubanetis|Cuber Netties|K8s" in csv_text
    )
    assert bot.documents[-1][1].name == "woerterbuch_demo.csv"


# --- 4.8 ----------------------------------------------------------------------------------


@pytest.fixture
def vocab_env(bot_env: tuple[FakeBot, service.Access, Path], monkeypatch: pytest.MonkeyPatch
              ) -> tuple[FakeBot, service.Access]:  # fmt: skip
    monkeypatch.setenv("REELS_TEST_LLM", "1")
    monkeypatch.setenv("REELS_TEST_VOCAB", json.dumps(VOCAB))
    bot, access, _ = bot_env
    return bot, access


def test_vocab_questions_pause_until_answered(
    vocab_env: tuple[FakeBot, service.Access], engine: Engine
) -> None:
    bot, access = vocab_env
    app = get_settings()
    _go(client.on_document(FakeMessage(ME, 10, document=_doc()), bot, access, engine, app))  # type: ignore[arg-type]
    _run()
    with Session(engine) as s:
        version = s.scalars(select(Version)).one()
        version_id = version.id
    assert _steps(engine, version_id) == ["probe", "audio", "transcribe", "glossary", "vocab"]

    assert _go(_tick(bot, engine)) == 2  # processing + vocab questions
    intro, q1, q2 = bot.messages[-3:]
    assert intro.text == texts.VOCAB_INTRO.format(count=2)
    assert "Ist „wort6“ richtig geschrieben?" in q1.text and "Satz 2:" in q1.text
    assert [b.text for b in q1.reply_markup.inline_keyboard[0]] == [
        texts.BUTTON_VOCAB_KEEP,
        "✏️ Kubernetes",
    ]
    assert len(q2.reply_markup.inline_keyboard[0]) == 1  # no suggestion
    assert _go(_tick(bot, engine)) == 0

    # Text that answers no question while questions are open.
    stray = FakeMessage(ME, 20, text="Hallo?")
    _go(client.on_text(stray, access, engine))  # type: ignore[arg-type]
    assert stray.answers == [texts.VOCAB_REPLY_HINT]

    # Q1 via suggestion button; pressing again is refused.
    suggestion = q1.reply_markup.inline_keyboard[0][1].callback_data
    _go(client.on_button(FakeCallback(suggestion, ME), bot, access, engine, app))  # type: ignore[arg-type]
    assert bot.messages[-1].text == texts.VOCAB_FIXED.format(text="wort6", answer="Kubernetes")
    _go(client.on_button(FakeCallback(suggestion, ME), bot, access, engine, app))  # type: ignore[arg-type]
    assert bot.messages[-1].text == texts.VOCAB_ALREADY
    assert _run() == 0, "still waiting for question 2"

    # Q2 by replying with the correct spelling: last answer -> pipeline continues.
    reply = FakeMessage(
        ME, 21, text="Wortsechzehn", reply_to_message=type("R", (), {"message_id": q2.message_id})()
    )
    reply.from_user = type("U", (), {"id": ME})()  # type: ignore[attr-defined]
    _go(client.on_text(reply, access, engine))  # type: ignore[arg-type]
    assert reply.answers == [
        texts.VOCAB_FIXED.format(text="wort16", answer="Wortsechzehn"), texts.VOCAB_DONE,
    ]  # fmt: skip
    _run()

    with Session(engine) as s:
        version = s.get(Version, version_id)
        assert version is not None and version.status is VersionStatus.READY
        assert (
            version.text_overlay["6"] == "Kubernetes"
            and version.text_overlay["16"] == "Wortsechzehn"
        )
        terms = {t.term: t for t in load_terms(s, access.client_id)}
    assert terms["Kubernetes"].status == "suggested" and terms["Kubernetes"].variants == ["wort6"]
    assert terms["Wortsechzehn"].source == "vocab"
    _go(_tick(bot, engine))
    assert "Kubernetes" in bot.messages[-1].text  # preview sentence list uses the answers

    # A rerun does not ask again.
    with Session(engine) as s:
        rerun_from(s, version_id, "probe")
        s.commit()
    _run()
    with Session(engine) as s:
        assert s.get(Version, version_id).status is VersionStatus.READY  # type: ignore[union-attr]


def test_keep_answers_and_no_pause_without_chat(
    vocab_env: tuple[FakeBot, service.Access], engine: Engine, tmp_path: Path
) -> None:
    bot, access = vocab_env
    app = get_settings()

    # Created via CLI (no Telegram chat): questions recorded, nothing waits.
    with Session(engine) as s:
        from reels.db.models import Client

        demo = s.scalars(select(Client)).one()
        cli_version = create_project(s, demo, tmp_path / "src.mp4", app)
        s.commit()
        cli_id = cli_version.id
    _run()
    with Session(engine) as s:
        v = s.get(Version, cli_id)
        assert v is not None and v.status is VersionStatus.READY
        assert len(v.edits["vocab"]["questions"]) == 2

    # Via Telegram, answered "passt so" twice: text stays as transcribed.
    _go(client.on_document(FakeMessage(ME, 30, document=_doc()), bot, access, engine, app))  # type: ignore[arg-type]
    _run()
    _go(_tick(bot, engine))
    for question in bot.messages[-2:]:
        keep = question.reply_markup.inline_keyboard[0][0].callback_data
        _go(client.on_button(FakeCallback(keep, ME), bot, access, engine, app))  # type: ignore[arg-type]
    assert [m.text for m in bot.messages[-3:]] == [
        texts.VOCAB_KEPT.format(text="wort6"),
        texts.VOCAB_KEPT.format(text="wort16"),
        texts.VOCAB_DONE,
    ]
    _run()
    with Session(engine) as s:
        project = s.scalars(select(Project).where(Project.telegram_message_id == 30)).one()
        v = s.scalars(select(Version).where(Version.project_id == project.id)).one()
        assert v.status is VersionStatus.READY and "6" not in v.text_overlay
        assert load_terms(s, access.client_id) == []
