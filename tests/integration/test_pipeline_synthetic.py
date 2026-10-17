"""Pipeline as jobs on synthetic videos (Etappe 1 + 2 acceptance), run in-process."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from reels.config import get_settings
from reels.db.clients import create_client
from reels.db.models import (
    Asset,
    AssetKind,
    Job,
    JobStatus,
    Project,
    ProjectStatus,
    Version,
    VersionStatus,
)
from reels.db.models import Transcript as TranscriptRow
from reels.domain.cutplan import CutPlan
from reels.pipeline.runner import STEP_ORDER, create_project, rerun_from
from reels.worker.main import Worker, WorkerConfig
from tests.fixtures.synthetic_video import make_video
from tests.integration.worker_fakes import make_providers

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]


def _silences(path: Path) -> list[tuple[float, float]]:
    stderr = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "silencedetect=noise=-50dB:d=0.2",
         "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    ).stderr  # fmt: skip
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", stderr)]
    return list(zip(starts, ends, strict=True))


def _video_info(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    data = json.loads(out)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    return v["width"], v["height"], float(data["format"]["duration"])


def _setup(
    tmp_path: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch, orientation: str = "portrait",
    size: tuple[int, int] = (360, 640),
) -> tuple[int, Path]:  # fmt: skip
    spec = make_video(tmp_path / f"{orientation}.mp4", orientation, size=size)  # type: ignore[arg-type]
    monkeypatch.setenv("REELS_TEST_SPEC", str(tmp_path / f"{orientation}.json"))
    monkeypatch.setenv("REELS_TEST_CALLS", str(tmp_path / "calls.txt"))
    monkeypatch.setenv("REELS_TEST_NOTIFY", str(tmp_path / "notify.txt"))
    with Session(engine) as session:
        client = create_client(session, "Demo")
        version = create_project(session, client, Path(spec.path), get_settings())
        session.commit()
        return version.id, Path(spec.path)


def _run(isolate: bool = False, max_jobs: int | None = None, **overrides: object) -> int:
    config = WorkerConfig.from_settings(get_settings())
    config = WorkerConfig(**{**config.__dict__, "isolate": isolate, **overrides})  # type: ignore[arg-type]
    return Worker(config, make_providers()).run(until_idle=True, max_jobs=max_jobs)


def _calls(tmp_path: Path) -> int:
    path = tmp_path / "calls.txt"
    return len(path.read_text().splitlines()) if path.exists() else 0


@pytest.mark.parametrize(
    ("orientation", "size"), [("portrait", (720, 1280)), ("landscape", (1280, 720))]
)
def test_version_1_from_jobs(
    app_env: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    orientation: str, size: tuple[int, int],
) -> None:  # fmt: skip
    version_id, _ = _setup(tmp_path, engine, monkeypatch, orientation, size)
    assert _run() == len(STEP_ORDER)

    with Session(engine) as session:
        version = session.get(Version, version_id)
        assert version is not None
        project = session.get(Project, version.project_id)
        assert project is not None
        assert version.status is VersionStatus.READY
        assert project.status is ProjectStatus.REVIEW
        jobs = session.scalars(
            select(Job).where(Job.version_id == version_id).order_by(Job.id)
        ).all()
        assert [j.step for j in jobs] == list(STEP_ORDER)
        assert all(j.status is JobStatus.DONE and j.attempts == 1 for j in jobs)
        assert all("runtime_s" in j.metrics for j in jobs)
        plan = CutPlan.model_validate(version.cut_plan)
        assert version.subtitle_style is not None and version.subtitle_style["karaoke"] is False
        assert version.highlights == []
        export_asset = session.scalar(
            select(Asset).where(Asset.version_id == version_id, Asset.kind == AssetKind.EXPORT)
        )
        assert export_asset is not None and export_asset.path == version.export_path
        row = session.get(TranscriptRow, version.transcript_id)
        assert row is not None
        transcript_words = row.words

    # The 0.3 s pause stays, the 0.8 s and 1.5 s pauses shrink to 2 x edge padding.
    pad = plan.params["edge_padding_s"]
    assert plan.output_duration == pytest.approx(4.0 + 0.3 + 2 * (2 * pad), abs=0.005)
    for r in plan.keep:
        for edge in (r.start, r.end):
            for w in transcript_words:
                assert not (w["start"] + 1e-6 < edge < w["end"] - 1e-6), (edge, w)

    export = app_env / str(version.export_path)
    width, height, duration = _video_info(export)
    assert (width, height) == (1080, 1920)
    assert duration == pytest.approx(plan.output_duration, abs=0.1)
    silences = _silences(export)
    assert len(silences) == 1
    assert silences[0][1] - silences[0][0] == pytest.approx(0.3, abs=0.06)


def test_rerun_reuses_audio_and_transcript(
    app_env: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version_id, _ = _setup(tmp_path, engine, monkeypatch)
    _run()
    assert _calls(tmp_path) == 1

    with Session(engine) as session:
        rerun_from(session, version_id, "probe")
        session.commit()
    assert _run() == len(STEP_ORDER)
    assert _calls(tmp_path) == 1, "transcription must not run again"

    with Session(engine) as session:
        jobs = session.scalars(
            select(Job).where(Job.version_id == version_id).order_by(Job.id.desc())
        ).all()
        latest = {}
        for j in jobs:
            latest.setdefault(j.step, j)
        assert latest["audio"].metrics["reused"] is True
        assert latest["transcribe"].metrics["reused"] is True
        assert latest["vad"].metrics["reused"] is True
        assert latest["glossary"].metrics["hits_counted"] is False  # hits only once per version
        assert session.get(Version, version_id).status is VersionStatus.READY  # type: ignore[union-attr]


def test_rerun_from_subtitles_applies_new_karaoke_setting(
    app_env: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version_id, _ = _setup(tmp_path, engine, monkeypatch)
    _run()
    with Session(engine) as session:
        version = session.get(Version, version_id)
        assert version is not None
        project = session.get(Project, version.project_id)
        client = project.client_id  # type: ignore[union-attr]
        from reels.db.models import Client

        c = session.get(Client, client)
        assert c is not None
        c.settings = {**c.settings, "subtitle": {**c.settings["subtitle"], "karaoke": True}}
        rerun_from(session, version_id, "subtitles")
        session.commit()
    assert _run() == 2  # subtitles + render only
    ass = next(app_env.rglob("v1/subtitles.ass")).read_text()
    assert "\\k" in ass


def test_llm_highlights_and_token_metrics(
    app_env: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELS_TEST_LLM", "1")
    version_id, _ = _setup(tmp_path, engine, monkeypatch)
    _run()
    with Session(engine) as session:
        version = session.get(Version, version_id)
        job = session.scalar(
            select(Job).where(Job.version_id == version_id, Job.step == "highlights")
        )
        assert version is not None and job is not None
        assert len(version.highlights) == 4  # one per sentence
        assert job.metrics["llm_input_tokens"] == 120
        assert job.metrics["llm_output_tokens"] == 30
    assert "\\1c" in next(app_env.rglob("v1/subtitles.ass")).read_text()


def test_too_long_video_fails_immediately_and_notifies(
    app_env: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version_id, _ = _setup(tmp_path, engine, monkeypatch)
    with Session(engine) as session:
        from reels.db.models import Client

        c = session.scalars(select(Client)).one()
        c.settings = {**c.settings, "max_duration_s": 2}
        session.commit()
    _run()
    with Session(engine) as session:
        job = session.scalars(select(Job).where(Job.version_id == version_id)).one()
        version = session.get(Version, version_id)
        assert (job.step, job.status, job.attempts) == ("probe", JobStatus.FAILED, 1)
        assert "limit is 2s" in (job.error or "")
        assert job.metrics["error_code"] == "too_long"
        assert job.metrics["error_details"]["limit_s"] == 2
        assert version is not None and version.status is VersionStatus.FAILED
    assert "endgültig fehlgeschlagen" in (tmp_path / "notify.txt").read_text()
    assert _calls(tmp_path) == 0
