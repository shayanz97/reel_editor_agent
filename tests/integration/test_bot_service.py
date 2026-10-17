"""Bot service layer against Postgres (no Telegram involved)."""

from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from reels.bot import service
from reels.bot.service import (
    AdminJobNotice,
    Decision,
    FailureNotice,
    PreviewNotice,
    ProcessingNotice,
)
from reels.config import get_settings
from reels.db.clients import ClientError, create_client
from reels.db.models import Job, JobStatus, Project, ProjectStatus, Version, VersionStatus
from reels.domain.segments import Segment
from reels.pipeline.steps import ProjectPaths, write_json

pytestmark = pytest.mark.integration

ME, OTHER = 1001, 2002


@pytest.fixture
def setup(app_env: Path, engine: Engine, tmp_path: Path) -> tuple[service.Access, Path]:
    with Session(engine) as s:
        create_client(s, "Demo", telegram_user_ids=[ME])
        create_client(s, "Andere", telegram_user_ids=[OTHER])
        s.commit()
        access = service.find_access(s, ME)
    assert access is not None
    video = tmp_path / "in.mp4"
    video.write_bytes(b"not really a video")
    return access, video


def _register(
    session: Session, access: service.Access, video: Path, message_id: int = 7
) -> tuple[int, int]:
    return service.register_video(
        session, access, video, get_settings(), chat_id=ME, message_id=message_id,
        status_message_id=99, caption="Mein Text", title="in.mp4",
    )  # fmt: skip


def _make_ready(session: Session, version_id: int) -> None:
    version = session.get(Version, version_id)
    assert version is not None
    project = session.get(Project, version.project_id)
    assert project is not None
    paths = ProjectPaths.for_project(get_settings().data_dir, project.client_id, project.id)
    vp = paths.version(version.number)
    vp.dir.mkdir(parents=True, exist_ok=True)
    vp.export.write_bytes(b"mp4")
    write_json(vp.segments, [Segment(number=1, first_word=0, last_word=1, text="Hallo Welt.")])
    version.status = VersionStatus.READY
    version.export_path = str(vp.export.relative_to(get_settings().data_dir))
    version.cut_plan = {
        "keep": [{"start": 0.5, "end": 2.0, "first_word": 0, "last_word": 1}],
        "params": {},
    }
    project.status = ProjectStatus.REVIEW


def test_access(setup: tuple[service.Access, Path], engine: Engine) -> None:
    access, _ = setup
    assert access.client_slug == "demo"
    with Session(engine) as s:
        assert service.find_access(s, 4242) is None
        from reels.db.models import Client

        demo = s.scalars(select(Client).where(Client.slug == "demo")).one()
        demo.active = False
        s.commit()
        assert service.find_access(s, ME) is None


def test_register_video_and_duplicates(setup: tuple[service.Access, Path], engine: Engine) -> None:
    access, video = setup
    with Session(engine) as s:
        project_id, version_id = _register(s, access, video)
        s.commit()
        project = s.get(Project, project_id)
        assert project is not None
        assert (project.caption, project.status_message_id, project.telegram_message_id) == (
            "Mein Text",
            99,
            7,
        )
        assert service.is_known_message(s, ME, 7)
        assert not service.is_known_message(s, ME, 8)
        job = s.scalars(select(Job)).one()
        assert (job.version_id, job.step) == (version_id, "probe")

        with pytest.raises(service.DuplicateMessageError):
            _register(s, access, video)
        s.commit()
        assert len(s.scalars(select(Project)).all()) == 1


def test_notices_are_sent_once(setup: tuple[service.Access, Path], engine: Engine) -> None:
    access, video = setup
    app = get_settings()
    with Session(engine) as s:
        project_id, version_id = _register(s, access, video)
        s.commit()
        assert service.pending_notices(s, app) == []

        s.get(Project, project_id).status = ProjectStatus.PROCESSING  # type: ignore[union-attr]
        s.commit()
        (notice,) = service.pending_notices(s, app)
        assert notice == ProcessingNotice(project_id, ME, 99)
        service.mark_notice_sent(s, notice)
        s.commit()
        assert service.pending_notices(s, app) == []

        _make_ready(s, version_id)
        s.commit()
        (preview,) = service.pending_notices(s, app)
        assert isinstance(preview, PreviewNotice)
        assert preview.version_number == 1 and preview.segments[0].text == "Hallo Welt."
        assert preview.export.exists() and preview.duration_s == pytest.approx(1.5)
        service.mark_notice_sent(s, preview, message_id=555)
        s.commit()
        assert service.pending_notices(s, app) == []
        assert s.get(Version, version_id).preview_message_id == 555  # type: ignore[union-attr]


def test_failure_notices_for_client_and_admin(
    setup: tuple[service.Access, Path], engine: Engine
) -> None:
    access, video = setup
    app = get_settings()
    with Session(engine) as s:
        _, version_id = _register(s, access, video)
        job = s.scalars(select(Job)).one()
        job.status, job.attempts, job.error = JobStatus.FAILED, 1, "PermanentStepError: too long"
        job.metrics = {
            "error_code": "too_long",
            "error_details": {"duration_s": 200.0, "limit_s": 180.0},
        }
        s.get(Version, version_id).status = VersionStatus.FAILED  # type: ignore[union-attr]
        s.commit()

        notices = service.pending_notices(s, app)
        failure = next(n for n in notices if isinstance(n, FailureNotice))
        admin = next(n for n in notices if isinstance(n, AdminJobNotice))
        assert (failure.error_code, failure.error_details["limit_s"]) == ("too_long", 180.0)
        assert (admin.job_id, admin.step, admin.client_name, admin.version_number) == (
            job.id,
            "probe",
            "Demo",
            1,
        )
        for n in notices:
            service.mark_notice_sent(s, n)
        s.commit()
        assert service.pending_notices(s, app) == []


def test_admin_notice_for_job_without_version(app_env: Path, engine: Engine) -> None:
    with Session(engine) as s:
        s.add(Job(step="cleanup", status=JobStatus.FAILED, attempts=3, error="disk full"))
        s.commit()
        (notice,) = service.pending_notices(s, get_settings())
        assert isinstance(notice, AdminJobNotice)
        assert (notice.step, notice.project_id, notice.client_name) == ("cleanup", None, None)


def test_approve_rules(setup: tuple[service.Access, Path], engine: Engine) -> None:
    access, video = setup
    app = get_settings()
    with Session(engine) as s:
        project_id, v1 = _register(s, access, video)
        s.commit()
        assert service.approve(s, v1, ME, app).decision is Decision.NOT_READY
        assert service.approve(s, v1, OTHER, app).decision is Decision.FORBIDDEN

        _make_ready(s, v1)
        v2 = Version(project_id=project_id, number=2, status=VersionStatus.PENDING)
        s.add(v2)
        s.commit()
        assert service.approve(s, v1, ME, app).decision is Decision.OUTDATED
        assert service.change_request(s, v1, ME).decision is Decision.OUTDATED

        s.delete(v2)
        s.commit()
        assert service.change_request(s, v1, ME).decision is Decision.OK
        result = service.approve(s, v1, ME, app)
        s.commit()
        assert (
            result.decision is Decision.OK and result.export is not None and result.export.exists()
        )
        project = s.get(Project, project_id)
        assert project is not None and project.approved_at is not None
        assert project.status is ProjectStatus.APPROVED
        assert s.get(Version, v1).approved_at is not None  # type: ignore[union-attr]
        assert service.approve(s, v1, ME, app).decision is Decision.ALREADY_APPROVED
        assert service.pending_notices(s, app) == []  # approved projects get no preview again


def test_admin_commands(setup: tuple[service.Access, Path], engine: Engine) -> None:
    with Session(engine) as s:
        client = service.admin_create_client(s, "Neu GmbH", [3003])
        assert client.slug == "neu-gmbh"
        service.admin_add_user(s, "neu-gmbh", 3004)
        s.commit()
        assert service.find_access(s, 3004).client_slug == "neu-gmbh"  # type: ignore[union-attr]
        with pytest.raises(ClientError, match="already belongs"):
            service.admin_add_user(s, "demo", 3004)
        with pytest.raises(ClientError, match="unknown client"):
            service.admin_add_user(s, "nope", 5)


def test_status_summary(setup: tuple[service.Access, Path], engine: Engine) -> None:
    access, video = setup
    with Session(engine) as s:
        _register(s, access, video)
        s.commit()
        summary = service.status_summary(s)
        assert summary.jobs == {"queued": 1}
        assert summary.projects == {"received": 1}
        assert summary.oldest_queued_s is not None and summary.failed_24h == 0
