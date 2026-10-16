"""`reels process` and `reels glossary` against the test database (engines faked)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from reels.cli import app
from reels.config import get_settings
from reels.db.glossary import load_terms
from reels.db.models import Project, ProjectStatus
from reels.db.session import get_engine
from tests.fixtures.synthetic_video import make_video
from tests.integration.pipeline_helpers import fakes

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture(autouse=True)
def cli_env(
    engine: Engine, migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    get_engine.cache_clear()
    assert runner.invoke(app, ["client", "add", "Demo"]).exit_code == 0
    yield
    get_engine().dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()


def test_glossary_import_export(tmp_path: Path, engine: Engine) -> None:
    csv_file = tmp_path / "terms.csv"
    csv_file.write_text("term,variants\nKubernetes,Cuber Netties|Kubernätes\nDocker,\n")
    result = runner.invoke(app, ["glossary", "import", "demo", str(csv_file)])
    assert result.exit_code == 0, result.output
    assert "2 added, 0 updated" in result.output
    assert (
        "0 added, 2 updated"
        in runner.invoke(app, ["glossary", "import", "demo", str(csv_file)]).output
    )

    exported = runner.invoke(app, ["glossary", "export", "demo"]).output
    assert "Kubernetes,Cuber Netties|Kubernätes" in exported
    assert runner.invoke(app, ["glossary", "export", "nobody"]).exit_code == 1


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg not installed")
def test_process_writes_version_1(
    tmp_path: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = make_video(tmp_path / "clip.mp4", "portrait", size=(360, 640))
    fake_engine, fake_vad = fakes(spec)
    monkeypatch.setattr("reels.transcribe.base.get_engine", lambda name: fake_engine)
    monkeypatch.setattr("reels.vad.silero.SileroVad", lambda: fake_vad)
    csv_file = tmp_path / "terms.csv"
    csv_file.write_text("term,variants\nWORT1,\n")
    runner.invoke(app, ["glossary", "import", "demo", str(csv_file)])

    result = runner.invoke(
        app, ["process", str(spec.path), "--client", "demo", "--preset", "ultrafast"]
    )
    assert result.exit_code == 0, result.output
    assert "cuts:             2" in result.output
    assert "glossary:         WORT1 x1" in result.output
    assert "render" in result.output
    assert fake_engine.calls[0][2] == "Fachbegriffe: WORT1."

    with Session(engine) as session:
        project = session.query(Project).one()
        assert project.status is ProjectStatus.REVIEW
        terms = load_terms(session, project.client_id)
        assert terms[0].hits == 1
        version_dir = (
            Path(get_settings().data_dir) / "clients" / str(project.client_id) / "projects"
            / str(project.id) / "v1"
        )  # fmt: skip
    for name in (
        "cutplan.json",
        "overlay.json",
        "segments.json",
        "highlights.json",
        "subtitles.ass",
        "export.mp4",
    ):
        assert (version_dir / name).exists(), name

    rerun = runner.invoke(
        app,
        [
            "process",
            str(spec.path),
            "--client",
            "demo",
            "--project",
            str(project.id),
            "--preset",
            "ultrafast",
        ],
    )
    assert rerun.exit_code == 0, rerun.output
    assert len(fake_engine.calls) == 1


def test_process_unknown_client(tmp_path: Path) -> None:
    clip = tmp_path / "x.mp4"
    clip.write_bytes(b"")
    result = runner.invoke(app, ["process", str(clip), "--client", "nobody"])
    assert result.exit_code == 1
    assert "unknown client" in result.output
