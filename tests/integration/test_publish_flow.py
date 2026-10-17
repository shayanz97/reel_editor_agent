"""Etappe 5: after approval -> 3 covers + post text -> choice -> final package."""

import shutil
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from reels.bot import service, texts
from reels.bot.handlers import client
from reels.config import get_settings
from reels.db.models import (
    Client,
    Job,
    JobStatus,
    PublishPackage,
    PublishStatus,
    Version,
    VersionStatus,
)
from reels.media import frames
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


def _chromium_installed() -> bool:
    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
    pytest.mark.skipif(not _chromium_installed(), reason="playwright chromium not installed"),
]


def _approve(bot: FakeBot, access: service.Access, engine: Engine, message_id: int = 10) -> int:
    app = get_settings()
    _go(client.on_document(FakeMessage(ME, message_id, document=_doc()), bot, access, engine, app))  # type: ignore[arg-type]
    _run()
    _go(_tick(bot, engine))
    approve = bot.messages[-1].reply_markup.inline_keyboard[0][0].callback_data
    _go(client.on_button(FakeCallback(approve, ME), bot, access, engine, app))  # type: ignore[arg-type]
    with Session(engine) as s:
        return s.scalars(select(Version).order_by(Version.id.desc())).first().id  # type: ignore[union-attr]


def _image(path: Path):  # type: ignore[no-untyped-def]
    import cv2

    return cv2.imread(str(path))


def test_frames_from_synthetic_video(app_env: Path, tmp_path: Path) -> None:
    from reels.domain.cutplan import KeepRange
    from tests.fixtures.synthetic_video import make_video

    spec = make_video(tmp_path / "v.mp4", "landscape", size=(1280, 720))
    keep = [
        KeepRange(start=0, end=3.0, first_word=0, last_word=1),
        KeepRange(start=4.0, end=6.6, first_word=2, last_word=3),
    ]
    best = frames.collect(Path(spec.path), keep, tmp_path / "frames", samples=8, k=3)
    assert len(best) == 3 and [b.time for b in best] == sorted(b.time for b in best)
    for b in best:
        assert _image(b.path).shape == (1920, 1080, 3)  # cropped like the export
        assert _image(b.path.with_name(b.path.stem + "_small.jpg")).shape[1] == 432
        assert b.sharpness > 0 and b.faces == 0


def test_package_offer_choice_revision_final(
    bot_env: tuple[FakeBot, service.Access, Path], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELS_TEST_LLM", "1")
    bot, access, _ = bot_env
    app = get_settings()
    version_id = _approve(bot, access, engine)
    assert bot.documents[-1][2] == texts.APPROVED  # the video comes right away

    _run()  # frames, cover_plan, covers, post_text
    with Session(engine) as s:
        steps = [
            j.step
            for j in s.scalars(select(Job).where(Job.version_id == version_id).order_by(Job.id))
        ]
        package = s.scalars(select(PublishPackage)).one()
        package_id = package.id
        assert steps[-4:] == ["frames", "cover_plan", "covers", "post_text"]
        assert package.status is PublishStatus.READY
        assert [c["headline"] for c in package.covers] == ["Titel 1", "Titel 2", "Titel 3"]
        assert package.post["hashtags"] == ["#Geld", "#Finanzentipps"]  # cleaned, deduplicated
        assert "Was ist dein Weg?" in (package.post_text or "")
        covers = [app.data_dir / c["path"] for c in package.covers]
    for png in covers:
        assert _image(png).shape == (1920, 1080, 3)
    assert len({png.read_bytes() for png in covers}) == 3  # three different covers

    assert _go(_tick(bot, engine)) == 1
    assert [p[1] for p in bot.photos] == covers
    assert [p[2].text for p in bot.photos] == [f"Cover {n}: Titel {n}" for n in (1, 2, 3)]
    offer_text = bot.messages[-1]
    assert offer_text.text.startswith("📝 Beitragstext (Vorschlag):")
    assert _go(_tick(bot, engine)) == 0

    cover2 = bot.photos[1][2].reply_markup.inline_keyboard[0][0].callback_data
    _go(client.on_button(FakeCallback(cover2, ME), bot, access, engine, app))  # type: ignore[arg-type]
    assert [m.text for m in bot.messages[-2:]] == [
        texts.PUBLISH_COVER_CHOSEN.format(number=2), texts.PUBLISH_OPEN_TEXT,
    ]  # fmt: skip

    # Text change via free text (reply to the text message).
    reply = FakeMessage(
        ME,
        50,
        text="Bitte kürzer",
        reply_to_message=type("R", (), {"message_id": offer_text.message_id})(),
    )
    reply.from_user = type("U", (), {"id": ME})()  # type: ignore[attr-defined]
    _go(client.on_text(reply, access, engine))  # type: ignore[arg-type]
    assert reply.answers == [texts.PUBLISH_REVISING]
    busy = FakeMessage(ME, 51, text="und ohne Emojis")
    busy.from_user = reply.from_user  # type: ignore[attr-defined]
    _go(client.on_text(busy, access, engine))  # type: ignore[arg-type]
    assert busy.answers == [texts.PUBLISH_BUSY]
    text_ok = offer_text.reply_markup.inline_keyboard[0][0].callback_data
    early = FakeCallback(text_ok, ME)
    _go(client.on_button(early, bot, access, engine, app))  # type: ignore[arg-type]
    assert early.alerts == [texts.PUBLISH_BUSY]

    _run()  # post_revise
    assert _go(_tick(bot, engine)) == 1
    revised = bot.messages[-1]
    assert revised.text.startswith("📝 Überarbeiteter Beitragstext:") and "Kürzer." in revised.text

    ok = revised.reply_markup.inline_keyboard[0][0].callback_data
    _go(client.on_button(FakeCallback(ok, ME), bot, access, engine, app))  # type: ignore[arg-type]
    assert bot.messages[-1].text == texts.PUBLISH_TEXT_CONFIRMED

    assert _go(_tick(bot, engine)) == 1  # final package
    chat, final_cover, caption = bot.documents[-1]
    assert (chat, final_cover, caption) == (ME, covers[1], texts.PUBLISH_FINAL)
    assert bot.messages[-1].text.startswith("Kürzer.") and "#Geld" in bot.messages[-1].text
    with Session(engine) as s:
        package = s.get(PublishPackage, package_id)
        assert package is not None and package.status is PublishStatus.DONE
        assert package.final_sent_at is not None
    assert _go(_tick(bot, engine)) == 0

    late = FakeCallback(cover2, ME)
    _go(client.on_button(late, bot, access, engine, app))  # type: ignore[arg-type]
    assert late.alerts == [texts.PUBLISH_DONE]


def test_template_is_per_client_and_failure_keeps_version(
    bot_env: tuple[FakeBot, service.Access, Path], engine: Engine, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    bot, access, _ = bot_env
    templates = tmp_path / "templates"
    shutil.copytree(Path(__file__).parents[2] / "assets" / "templates", templates)
    custom = templates / "cover" / "knallrot"
    custom.mkdir()
    (custom / "cover.html").write_text(
        "<html><body style='margin:0;width:1080px;height:1920px;background:#ff0000'>"
        "$headline<!-- $frame_url $font_url $text_color $accent_color --></body></html>"
    )
    monkeypatch.setenv("TEMPLATES_DIR", str(templates))
    get_settings.cache_clear()
    with Session(engine) as s:
        demo = s.scalars(select(Client)).one()
        demo.settings = {**demo.settings, "publish": {"cover_template": "knallrot"}}
        s.commit()

    version_id = _approve(bot, access, engine)
    _run()  # no LLM: fallback headlines and text
    with Session(engine) as s:
        package = s.scalars(select(PublishPackage)).one()
        assert package.status is PublishStatus.READY
        pixel = _image(get_settings().data_dir / package.covers[0]["path"])[1800, 50]
        assert tuple(int(c) for c in pixel) == (0, 0, 255)  # BGR red: the client's template
        assert package.post_text  # fallback text exists

    # Unknown template: the package fails, the approved version stays as it is.
    with Session(engine) as s:
        demo = s.scalars(select(Client)).one()
        demo.settings = {**demo.settings, "publish": {"cover_template": "gibtsnicht"}}
        from reels.pipeline.runner import rerun_from

        rerun_from(s, version_id, "covers")
        s.commit()
    _run()
    with Session(engine) as s:
        package = s.scalars(select(PublishPackage)).one()
        version = s.get(Version, version_id)
        failed = s.scalars(select(Job).where(Job.status == JobStatus.FAILED)).one()
        assert package.status is PublishStatus.FAILED and failed.step == "covers"
        assert version is not None and version.status is VersionStatus.READY and version.approved_at
    _go(_tick(bot, engine))
    assert any(m.text == texts.PUBLISH_FAILED for m in bot.messages if m.chat_id == ME)
    assert any(m.chat_id == ADMIN and "(covers)" in m.text for m in bot.messages)
