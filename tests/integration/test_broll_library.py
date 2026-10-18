"""Etappe 6 Teil 2: #broll library (upload, index, prefer own clips) and B-roll corrections."""

import json
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from reels.bot import service, texts
from reels.bot.handlers import client
from reels.config import get_settings
from reels.db.models import (
    BrollAsset,
    BrollAssetStatus,
    BrollPlacement,
    BrollSource,
    Client,
    Job,
    PlacementStatus,
    Version,
)
from tests.integration.bot_fakes import (
    CONTAINER,
    ME,
    FakeBot,
    FakeCallback,
    FakeMessage,
    _doc,
    _go,
    _tick,
)
from tests.integration.test_broll_flow import RULES, SUGGESTIONS
from tests.integration.test_pipeline_synthetic import _run

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]


@pytest.fixture
def lib_env(
    bot_env: tuple[FakeBot, service.Access, Path], engine: Engine, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeBot, service.Access]:  # fmt: skip
    """Bot env with LLM fakes, B-roll enabled with relaxed rules, stock available."""
    import subprocess

    bot, access, _ = bot_env
    stock = tmp_path / "stock"
    stock.mkdir()
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=green:s=270x480:d=10:r=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(stock / "stock.mp4")],
        check=True,
    )  # fmt: skip
    cv2.imwrite(str(stock / "stock.jpg"), np.full((480, 270, 3), (0, 255, 0), np.uint8))
    monkeypatch.setenv("REELS_TEST_LLM", "1")
    monkeypatch.setenv("REELS_TEST_BROLL", json.dumps(SUGGESTIONS))
    monkeypatch.setenv("REELS_TEST_STOCK", str(stock))
    with Session(engine) as s:
        demo = s.scalars(select(Client)).one()
        demo.settings = {**demo.settings, "broll": {**demo.settings["broll"], **RULES}}
        s.commit()
    return bot, access


def _upload(bot: FakeBot, name: str, bgr: tuple[int, int, int]) -> None:
    host = Path(get_settings().telegram_files_host_path) / "TOKEN" / "photos"
    host.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(host / name), np.full((1920, 1080, 3), bgr, np.uint8))
    bot.file_path = f"{CONTAINER}/TOKEN/photos/{name}"


def _photo(message_id: int, caption: str | None) -> FakeMessage:
    m = FakeMessage(ME, message_id, caption=caption)
    m.photo = [type("P", (), {"file_id": f"P{message_id}", "width": 1080, "height": 1920})()]  # type: ignore[attr-defined]
    return m


def test_upload_index_and_prefer_own_clip(
    lib_env: tuple[FakeBot, service.Access], engine: Engine
) -> None:
    bot, access = lib_env
    app = get_settings()

    hint = _photo(1, "schönes Bild")
    _go(client.on_photo(hint, bot, access, engine, app))  # type: ignore[arg-type]
    assert hint.answers == [texts.BROLL_PHOTO_HINT]

    _upload(bot, "red.jpg", (0, 0, 255))
    red = _photo(2, "Serverraum #broll")
    _go(client.on_photo(red, bot, access, engine, app))  # type: ignore[arg-type]
    assert red.answers == [texts.BROLL_SAVED]
    _go(client.on_photo(_photo(2, "Serverraum #broll"), bot, access, engine, app))  # type: ignore[arg-type]
    _upload(bot, "blue.jpg", (255, 0, 0))
    _go(client.on_photo(_photo(3, "#broll"), bot, access, engine, app))  # type: ignore[arg-type]

    _run()  # broll_index
    with Session(engine) as s:
        assets = s.scalars(select(BrollAsset).order_by(BrollAsset.id)).all()
        assert len(assets) == 2, "duplicate update ignored"
        assert all(a.status is BrollAssetStatus.READY and a.embedding is not None for a in assets)
        assert assets[0].description and assets[0].description.startswith("Serverraum")
        assert assets[0].thumbnail is not None and (app.data_dir / assets[0].thumbnail).exists()

    # A video whose B-roll idea is a server room: own red photo wins over Pixabay.
    bot.file_path = f"{CONTAINER}/TOKEN/documents/file_1.mp4"
    _go(client.on_document(FakeMessage(ME, 10, document=_doc()), bot, access, engine, app))  # type: ignore[arg-type]
    _run()
    with Session(engine) as s:
        stock_spot = s.scalars(select(BrollPlacement).where(BrollPlacement.kind == "stock")).one()
        own = s.get(BrollAsset, stock_spot.asset_id)
        assert own is not None and own.source is BrollSource.LIBRARY and own.description
        assert own.description.startswith("Serverraum")
        assert (
            s.scalars(select(BrollAsset).where(BrollAsset.source == BrollSource.PIXABAY)).all()
            == []
        )
    _go(_tick(bot, engine))
    preview = bot.messages[-1].text
    assert "(eigener Clip)" in preview and "(Textkarte)" in preview


def test_index_without_llm_marks_failed(lib_env: tuple[FakeBot, service.Access], engine: Engine,
                                        monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    bot, access = lib_env
    monkeypatch.delenv("REELS_TEST_LLM")
    _upload(bot, "red.jpg", (0, 0, 255))
    _go(client.on_photo(_photo(5, "#broll"), bot, access, engine, get_settings()))  # type: ignore[arg-type]
    _run()
    with Session(engine) as s:
        asset = s.scalars(select(BrollAsset)).one()
        assert asset.status is BrollAssetStatus.FAILED and asset.meta["error"] == "no_llm"


def _jobs(engine: Engine, number: int) -> list[str]:
    with Session(engine) as s:
        v = s.scalars(select(Version).where(Version.number == number)).one()
        return [
            j.step for j in s.scalars(select(Job).where(Job.version_id == v.id).order_by(Job.id))
        ]


def _correct(bot: FakeBot, access: service.Access, engine: Engine, monkeypatch: pytest.MonkeyPatch,
             ops: list[dict], message_id: int) -> str:  # type: ignore[type-arg]  # fmt: skip
    monkeypatch.setenv(
        "REELS_TEST_CORRECTION", json.dumps({"operations": ops, "clarification": None})
    )
    m = FakeMessage(ME, message_id, text="Änderung")
    m.from_user = type("U", (), {"id": ME})()  # type: ignore[attr-defined]
    _go(client.on_text(m, access, engine))  # type: ignore[arg-type]
    _run()
    _go(_tick(bot, engine))
    summary = bot.messages[-1]
    apply_button = summary.reply_markup.inline_keyboard[0][0].callback_data
    _go(client.on_button(FakeCallback(apply_button, ME), bot, access, engine, get_settings()))  # type: ignore[arg-type]
    _run()
    _go(_tick(bot, engine))
    return summary.text


def test_broll_corrections(
    lib_env: tuple[FakeBot, service.Access], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, access = lib_env
    _go(
        client.on_document(
            FakeMessage(ME, 10, document=_doc()), bot, access, engine, get_settings()
        )
    )  # type: ignore[arg-type]
    _run()
    _go(_tick(bot, engine))
    assert "[B1] Satz 2" in bot.messages[-1].text and "[B2] Satz 4" in bot.messages[-1].text

    summary = _correct(bot, access, engine, monkeypatch, [{"op": "remove_broll", "broll": 1}], 20)
    assert "Zwischenbild B1 entfernen" in summary
    assert _jobs(engine, 2)[0] == "segments", "no re-cut for B-roll edits"
    preview = bot.messages[-1].text
    assert "[B1]" not in preview and "[B2] Satz 4" in preview

    replace = {"op": "replace_broll", "broll": 2, "idea": "Karte", "query": "", "card_text": "Neu!"}
    add = {"op": "add_broll", "segment": 2, "find": None, "idea": "Zahl", "query": "",
           "card_text": "3 Wege"}  # fmt: skip
    summary = _correct(bot, access, engine, monkeypatch, [replace, add], 21)
    assert "Zwischenbild B2 ersetzen: Textkarte „Neu!“" in summary
    preview = bot.messages[-1].text
    assert (
        "[B2] Satz 4 · Neu! (Textkarte)" in preview
        and "[B3] Satz 2 · 3 Wege (Textkarte)" in preview
    )

    _correct(bot, access, engine, monkeypatch, [{"op": "set_broll_coverage", "coverage": 0}], 22)
    with Session(engine) as s:
        v4 = s.scalars(select(Version).where(Version.number == 4)).one()
        assert v4.edits["broll_rendered"] == []
        ready = s.scalars(select(BrollPlacement).where(
            BrollPlacement.version_id == v4.id, BrollPlacement.status == PlacementStatus.READY
        )).all()  # fmt: skip
        assert len(ready) == 2  # placements stay, coverage 0 keeps them out of the video
    assert "Zwischenbilder (B-Roll)" not in bot.messages[-1].text

    unknown = (
        _correct(bot, access, engine, monkeypatch, [{"op": "remove_broll", "broll": 9}], 23)
        if False
        else None
    )
    del unknown
