"""CLI against the test database: glossary, client set, process, enqueue, jobs."""

import shutil
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from reels.cli import app
from reels.db.glossary import load_terms
from reels.db.models import Client, Job, JobStatus, Project, ProjectStatus
from reels.pipeline.runner import STEP_ORDER
from tests.fixtures.synthetic_video import make_video
from tests.integration.worker_fakes import make_providers

pytestmark = pytest.mark.integration
runner = CliRunner()
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(autouse=True)
def demo_client(app_env: Path) -> None:
    assert runner.invoke(app, ["client", "add", "Demo"]).exit_code == 0


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Synthetic clip + fake engine/VAD wired into default_providers()."""
    spec = make_video(tmp_path / "clip.mp4", "portrait", size=(360, 640))
    monkeypatch.setenv("REELS_TEST_SPEC", str(tmp_path / "clip.json"))
    monkeypatch.setenv("REELS_TEST_CALLS", str(tmp_path / "calls.txt"))
    monkeypatch.setattr("reels.pipeline.runner.default_providers", make_providers)
    providers = make_providers()
    monkeypatch.setattr("reels.transcribe.base.get_engine", providers.engine)
    return Path(spec.path)


def test_glossary_import_export(tmp_path: Path) -> None:
    csv_file = tmp_path / "terms.csv"
    csv_file.write_text("term,variants\nKubernetes,Cuber Netties|Kubernätes\nDocker,\n")
    result = runner.invoke(app, ["glossary", "import", "demo", str(csv_file)])
    assert result.exit_code == 0, result.output
    assert "2 added, 0 updated" in result.output
    again = runner.invoke(app, ["glossary", "import", "demo", str(csv_file)])
    assert "0 added, 2 updated" in again.output

    exported = runner.invoke(app, ["glossary", "export", "demo"]).output
    assert "Kubernetes,Cuber Netties|Kubernätes" in exported
    assert runner.invoke(app, ["glossary", "export", "nobody"]).exit_code == 1


def test_client_set_validates(engine: Engine) -> None:
    ok = runner.invoke(app, ["client", "set", "demo", "subtitle.karaoke=true", "zoom_factor=1.1"])
    assert ok.exit_code == 0, ok.output
    with Session(engine) as session:
        settings = session.scalars(select(Client)).one().client_settings()
        assert settings.subtitle.karaoke is True and settings.zoom_factor == 1.1

    for bad in ("nope=1", "subtitle.nope=1", "subtitle=1", "zoom_factor=0.5", "missing-equals"):
        result = runner.invoke(app, ["client", "set", "demo", bad])
        assert result.exit_code == 1, bad


@needs_ffmpeg
def test_process_runs_all_steps_and_rerun_reuses_transcript(
    fakes: Path, tmp_path: Path, engine: Engine
) -> None:
    csv_file = tmp_path / "terms.csv"
    csv_file.write_text("term,variants\nWORT1,\n")
    runner.invoke(app, ["glossary", "import", "demo", str(csv_file)])

    result = runner.invoke(app, ["process", str(fakes), "--client", "demo", "--no-llm"])
    assert result.exit_code == 0, result.output
    assert "(ready)" in result.output
    assert "cuts:             2" in result.output
    assert "glossary:         WORT1 x1" in result.output
    for step in STEP_ORDER:
        assert f"  {step} " in result.output

    with Session(engine) as session:
        project = session.scalars(select(Project)).one()
        assert project.status is ProjectStatus.REVIEW
        assert load_terms(session, project.client_id)[0].hits == 1
        project_id = project.id

    rerun = runner.invoke(
        app, ["process", str(fakes), "--client", "demo", "--project", str(project_id), "--no-llm"]
    )
    assert rerun.exit_code == 0, rerun.output
    assert "transcript:       reused" in rerun.output
    assert len((tmp_path / "calls.txt").read_text().splitlines()) == 1


@needs_ffmpeg
def test_enqueue_and_jobs_commands(fakes: Path, engine: Engine) -> None:
    result = runner.invoke(app, ["enqueue", str(fakes), "--client", "demo"])
    assert result.exit_code == 0, result.output
    assert "queued project=1 version=1" in result.output

    listing = runner.invoke(app, ["jobs", "list"])
    assert "probe" in listing.output and "queued" in listing.output
    assert runner.invoke(app, ["jobs", "list", "--status", "bogus"]).exit_code == 1

    # A failed job can be retried; a done one cannot.
    with Session(engine) as session:
        job = session.scalars(select(Job)).one()
        job.status, job.attempts, job.error = JobStatus.FAILED, 3, "boom"
        session.commit()
        job_id = job.id
    assert "failed" in runner.invoke(app, ["jobs", "list", "--status", "failed"]).output
    assert runner.invoke(app, ["jobs", "retry", str(job_id)]).exit_code == 0
    assert runner.invoke(app, ["jobs", "retry", str(job_id)]).exit_code == 1

    # rerun: rejected while the step is already queued, unknown steps rejected.
    assert runner.invoke(app, ["jobs", "rerun", "1", "--from", "probe"]).exit_code == 1
    assert runner.invoke(app, ["jobs", "rerun", "1", "--from", "nope"]).exit_code == 1
    rerun = runner.invoke(app, ["jobs", "rerun", "1", "--from", "render"])
    assert rerun.exit_code == 0 and "from render" in rerun.output


def test_process_unknown_client(tmp_path: Path) -> None:
    clip = tmp_path / "x.mp4"
    clip.write_bytes(b"")
    result = runner.invoke(app, ["process", str(clip), "--client", "nobody"])
    assert result.exit_code == 1
    assert "unknown client" in result.output
