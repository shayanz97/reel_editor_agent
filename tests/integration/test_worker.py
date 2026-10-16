"""Worker processes: retries, timeouts, crash recovery, graceful shutdown, cleanup."""

import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from reels.config import get_settings
from reels.db.models import Asset, AssetKind, Job, JobStatus, Project, Version, VersionStatus
from reels.pipeline.cleanup import cleanup_expired
from reels.pipeline.runner import CLEANUP_STEP, STEP_ORDER
from reels.worker.main import Worker, WorkerConfig
from tests.integration.test_pipeline_synthetic import _calls, _run, _setup
from tests.integration.worker_fakes import make_providers

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

FAKES = "tests.integration.worker_fakes:make_providers"


def _jobs(engine: Engine, version_id: int) -> list[Job]:
    with Session(engine) as session:
        return list(
            session.scalars(select(Job).where(Job.version_id == version_id).order_by(Job.id))
        )


def _wait_for(predicate, timeout: float = 60.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError("condition not reached in time")


def test_failing_step_is_tried_three_times_then_failed(
    app_env: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELS_TEST_ENGINE", "fail")
    version_id, _ = _setup(tmp_path, engine, monkeypatch)
    _run(isolate=True, providers_factory=FAKES)

    jobs = {j.step: j for j in _jobs(engine, version_id)}
    transcribe = jobs["transcribe"]
    assert (transcribe.status, transcribe.attempts) == (JobStatus.FAILED, 3)
    assert "engine exploded" in (transcribe.error or "")
    assert "glossary" not in jobs  # chain stops
    assert _calls(tmp_path) == 3
    with Session(engine) as session:
        assert session.get(Version, version_id).status is VersionStatus.FAILED  # type: ignore[union-attr]
    notes = (tmp_path / "notify.txt").read_text().splitlines()
    assert len(notes) == 1 and f"Job {transcribe.id} (transcribe)" in notes[0]


def test_step_timeout_kills_the_step(
    app_env: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELS_TEST_ENGINE", "sleep-first:30")
    version_id, _ = _setup(tmp_path, engine, monkeypatch)
    start = time.monotonic()
    _run(isolate=True, providers_factory=FAKES, step_timeout_s=8.0, max_attempts=1)
    assert time.monotonic() - start < 25

    jobs = {j.step: j for j in _jobs(engine, version_id)}
    assert jobs["transcribe"].status is JobStatus.FAILED
    assert (jobs["transcribe"].error or "").startswith("timeout")


def _start_worker(env_overrides: dict[str, str]) -> subprocess.Popen[bytes]:
    env = {**os.environ, "WORKER_PROVIDERS_FACTORY": FAKES, "WORKER_POLL_INTERVAL_S": "0.2",
           **env_overrides}  # fmt: skip
    return subprocess.Popen(
        [sys.executable, "-m", "reels.worker"],
        env=env,
        start_new_session=True,  # own process group: we can kill worker + step together
    )


def _running(engine: Engine, version_id: int, step: str) -> bool:
    return any(j.step == step and j.status is JobStatus.RUNNING for j in _jobs(engine, version_id))


def test_worker_killed_mid_job_loses_and_duplicates_nothing(
    app_env: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELS_TEST_ENGINE", "sleep-first:20")
    version_id, _ = _setup(tmp_path, engine, monkeypatch)

    proc = _start_worker({})
    try:
        _wait_for(lambda: _running(engine, version_id, "transcribe"))
        time.sleep(1.0)  # the step holds its row lock now
        os.killpg(proc.pid, signal.SIGKILL)  # worker and its step process die together
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)

    assert _running(engine, version_id, "transcribe")  # left behind by the dead worker
    _run(isolate=True, providers_factory=FAKES, orphan_grace_s=0.0)

    jobs = _jobs(engine, version_id)
    assert [j.step for j in jobs] == list(STEP_ORDER)  # one job per step, nothing doubled
    assert all(j.status is JobStatus.DONE for j in jobs)
    transcribe = next(j for j in jobs if j.step == "transcribe")
    assert transcribe.attempts == 2  # the killed attempt counts
    assert _calls(tmp_path) == 2
    with Session(engine) as session:
        assert session.get(Version, version_id).status is VersionStatus.READY  # type: ignore[union-attr]


def test_sigterm_finishes_current_job_then_exits(
    app_env: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELS_TEST_ENGINE", "sleep-first:4")
    version_id, _ = _setup(tmp_path, engine, monkeypatch)

    proc = _start_worker({})
    try:
        _wait_for(lambda: _running(engine, version_id, "transcribe"))
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=60) == 0
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)

    jobs = {j.step: j for j in _jobs(engine, version_id)}
    assert jobs["transcribe"].status is JobStatus.DONE
    assert jobs["glossary"].status is JobStatus.QUEUED  # next step waits for the next worker


def test_cleanup_deletes_originals_and_intermediates_after_retention(
    app_env: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version_id, _ = _setup(tmp_path, engine, monkeypatch)
    _run()
    with Session(engine) as session:
        version = session.get(Version, version_id)
        assert version is not None
        project = session.get(Project, version.project_id)
        assert project is not None
        project.approved_at = datetime.now(UTC) - timedelta(days=29)
        session.commit()
        root = app_env / "clients" / str(project.client_id) / "projects" / str(project.id)

    with Session(engine) as session:
        assert cleanup_expired(session, get_settings().data_dir)["projects_cleaned"] == 0
        session.commit()
    assert (root / "original.mp4").exists()

    later = datetime.now(UTC) + timedelta(days=2)  # 31 days after approval
    with Session(engine) as session:
        metrics = cleanup_expired(session, get_settings().data_dir, now=later)
        session.commit()
        kinds = set(session.scalars(select(Asset.kind)).all())
    assert metrics["projects_cleaned"] == 1 and metrics["bytes_freed"] > 0
    remaining = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    assert remaining == ["v1/export.mp4"]
    assert kinds == {AssetKind.EXPORT}

    with Session(engine) as session:  # idempotent
        assert cleanup_expired(session, get_settings().data_dir, now=later)["files_deleted"] == 0


def test_cleanup_is_scheduled_once_per_interval(app_env: Path, engine: Engine) -> None:
    config = WorkerConfig.from_settings(get_settings())
    worker = Worker(WorkerConfig(**{**config.__dict__, "isolate": False}), _DummyProviders())
    assert worker.schedule_cleanup() is not None
    assert worker.schedule_cleanup() is None
    worker.run(until_idle=True)
    with Session(engine) as session:
        job = session.scalars(select(Job).where(Job.step == CLEANUP_STEP)).one()
        assert job.status is JobStatus.DONE
        assert job.metrics["projects_cleaned"] == 0
    assert worker.schedule_cleanup() is None  # ran within the interval


def _DummyProviders():  # type: ignore[no-untyped-def]  # noqa: N802
    from reels.pipeline.runner import Providers

    return Providers(engine=lambda _n: None, vad=lambda: None, llm=lambda: None)  # type: ignore[arg-type,return-value]


def test_make_providers_is_importable() -> None:
    # Spawned worker processes import the factory by name.
    assert callable(make_providers)
