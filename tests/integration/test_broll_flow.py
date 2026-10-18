"""Etappe 6 Teil 1: planned B-roll from (mocked) Pixabay and text cards in the export."""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from reels.bot import service
from reels.config import get_settings
from reels.db.clients import create_client
from reels.db.models import BrollAsset, BrollPlacement, BrollSource, Job, PlacementStatus, Version
from reels.pipeline.runner import create_project
from tests.fixtures.synthetic_video import make_video
from tests.integration.test_pipeline_synthetic import _run

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

# Synthetic transcript: 1 = wort0..wort4.  2 = wort5..wort10.
#                       3 = wort11..wort14. 4 = wort15..wort19.
SUGGESTIONS = [
    {"first_word_id": 5, "last_word_id": 9, "idea": "Serverraum mit blinkenden Lichtern",
     "query": "server room", "kind": "stock", "card_text": None, "priority": 1},
    {"first_word_id": 15, "last_word_id": 18, "idea": "Zahl einblenden", "query": "",
     "kind": "textcard", "card_text": "100 Jahre", "priority": 2},
    {"first_word_id": 11, "last_word_id": 12, "idea": "zu lang", "query": "",
     "kind": "textcard", "card_text": "viel zu viele Wörter für eine kleine Karte", "priority": 1},
]  # fmt: skip
# The synthetic video is only 4.6 s long: relaxed rules so that two spots fit.
RULES = {"enabled": True, "max_coverage": 0.9, "min_start_s": 0.5, "min_gap_s": 0.2,
         "min_duration_s": 0.8, "max_duration_s": 1.2}  # fmt: skip


@pytest.fixture
def broll_env(
    app_env: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> int:
    """Client with B-roll enabled, fake LLM + stock, one project queued. Returns version id."""
    import cv2

    stock = tmp_path / "stock"
    stock.mkdir()
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=270x480:d=10:r=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(stock / "stock.mp4")],
        check=True,
    )  # fmt: skip
    cv2.imwrite(str(stock / "stock.jpg"), np.full((480, 270, 3), (0, 0, 255), np.uint8))
    spec = make_video(tmp_path / "src.mp4", "portrait", size=(360, 640))
    for key, value in {
        "REELS_TEST_SPEC": str(tmp_path / "src.json"), "REELS_TEST_LLM": "1",
        "REELS_TEST_BROLL": json.dumps(SUGGESTIONS), "REELS_TEST_STOCK": str(stock),
    }.items():  # fmt: skip
        monkeypatch.setenv(key, value)
    with Session(engine) as s:
        client = create_client(s, "Demo")
        client.settings = {**client.settings, "broll": {**client.settings["broll"], **RULES}}
        version = create_project(s, client, Path(spec.path), get_settings())
        s.commit()
        return version.id


def _frame(path: Path, t: float) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        check=True, capture_output=True,
    ).stdout  # fmt: skip
    return np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3)


def _metrics(engine: Engine, version_id: int, step: str) -> dict:  # type: ignore[type-arg]
    with Session(engine) as s:
        job = s.scalars(
            select(Job)
            .where(Job.version_id == version_id, Job.step == step)
            .order_by(Job.id.desc())
        ).first()
        assert job is not None
        return dict(job.metrics)


def test_stock_and_textcard_in_export(broll_env: int, engine: Engine) -> None:
    _run()
    version_id = broll_env
    with Session(engine) as s:
        placements = s.scalars(select(BrollPlacement).order_by(BrollPlacement.number)).all()
        assert [(p.number, p.kind, p.status) for p in placements] == [
            (1, "stock", PlacementStatus.READY), (2, "textcard", PlacementStatus.READY),
        ]  # fmt: skip
        stock = s.get(BrollAsset, placements[0].asset_id)
        assert stock is not None and stock.source is BrollSource.PIXABAY
        assert stock.source_url == "https://pixabay.com/videos/server-4711/"
        assert placements[0].score == 8.0
        card = s.get(BrollAsset, placements[1].asset_id)
        assert card is not None and card.source is BrollSource.TEXTCARD
        assert s.get(Version, version_id).edits["broll_candidates"][0]["query"] == "server room"  # type: ignore[union-attr]
        export = get_settings().data_dir / s.get(Version, version_id).export_path  # type: ignore[union-attr, operator]
    assert _metrics(engine, version_id, "broll_plan")["valid"] == 2  # long card text dropped
    assert _metrics(engine, version_id, "render")["broll"] == 2

    # B1 sits on sentence 2 (output ~1.08 s on), B2 on sentence 4.
    red = _frame(export, 1.6)[200, 540]
    assert tuple(int(c) for c in red) == pytest.approx((0, 0, 255), abs=40)
    plain = _frame(export, 0.3)[200, 540]
    assert not (int(plain[2]) > 200 and int(plain[1]) < 60)  # no B-roll during the first seconds

    # Preview list: sentence number and source (Pixabay attribution).
    from reels.db.models import Project
    from reels.domain.segments import Segment
    from reels.pipeline.steps import ProjectPaths

    with Session(engine) as s:
        v = s.get(Version, version_id)
        assert v is not None
        project = s.get(Project, v.project_id)
        assert project is not None
        paths = ProjectPaths.for_project(get_settings().data_dir, project.client_id, project.id)
        segs = [
            Segment.model_validate(x) for x in json.loads(paths.version(1).segments.read_text())
        ]
        lines = service._broll_lines(s, version_id, segs)
    assert lines == [
        "[B1] Satz 2 · Serverraum mit blinkenden Lichtern (Pixabay)",
        "[B2] Satz 4 · 100 Jahre (Textkarte)",
    ]


def test_bad_match_is_skipped_not_shown(
    broll_env: int, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELS_TEST_BROLL_SCORE", "3")
    _run()
    with Session(engine) as s:
        stock = s.scalars(select(BrollPlacement).where(BrollPlacement.kind == "stock")).one()
        assert (stock.status, stock.reason, stock.asset_id) == (
            PlacementStatus.SKIPPED,
            "no_match",
            None,
        )
    assert _metrics(engine, broll_env, "render")["broll"] == 1  # only the text card


def test_without_stock_key_only_text_cards(
    broll_env: int, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REELS_TEST_STOCK")
    _run()
    with Session(engine) as s:
        stock = s.scalars(select(BrollPlacement).where(BrollPlacement.kind == "stock")).one()
        assert stock.reason == "no_stock_key"
    assert _metrics(engine, broll_env, "broll_fetch")["textcard"] == 1


def test_disabled_by_default(app_env: Path, engine: Engine, tmp_path: Path,
                             monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    spec = make_video(tmp_path / "src.mp4", "portrait", size=(360, 640))
    monkeypatch.setenv("REELS_TEST_SPEC", str(tmp_path / "src.json"))
    monkeypatch.setenv("REELS_TEST_LLM", "1")
    monkeypatch.setenv("REELS_TEST_BROLL", json.dumps(SUGGESTIONS))
    with Session(engine) as s:
        version = create_project(s, create_client(s, "Demo"), Path(spec.path), get_settings())
        s.commit()
        version_id = version.id
    _run()
    assert _metrics(engine, version_id, "broll_plan")["skipped"] == "disabled"
    assert _metrics(engine, version_id, "render")["broll"] == 0


def test_deleted_segment_removes_its_broll(broll_env: int, engine: Engine) -> None:
    """Acceptance: B-roll is anchored to words; cut the words and the B-roll is gone."""
    from reels.domain.corrections import DeleteSegment, Resolved, VersionState, apply
    from reels.pipeline.runner import create_next_version

    _run()
    with Session(engine) as s:
        base = s.get(Version, broll_env)
        assert base is not None
        words_removed = Resolved(
            op=DeleteSegment(op="delete_segment", segment=2), word_ids=list(range(5, 11))
        )
        state = apply(
            [words_removed],
            [],
            VersionState.from_version(base.text_overlay, base.highlights, base.edits),
        )
        v2 = create_next_version(s, base, state, "cutplan")
        s.commit()
        v2_id = v2.id
        assert (
            len(s.scalars(select(BrollPlacement).where(BrollPlacement.version_id == v2_id)).all())
            == 2
        )
    _run()
    assert _metrics(engine, v2_id, "render")["broll"] == 1  # the stock clip on sentence 2 is gone
