import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from reels.db.models import Client, Job, JobStatus, Project, ProjectStatus, Version

pytestmark = pytest.mark.integration


def _client(session: Session) -> Client:
    client = Client(slug="demo", name="Demo")
    session.add(client)
    session.flush()
    return client


def test_server_defaults_and_enum_roundtrip(session: Session) -> None:
    client = _client(session)
    project = Project(client_id=client.id)
    session.add(project)
    session.flush()
    version = Version(project_id=project.id, number=1)
    session.add(version)
    session.flush()
    job = Job(version_id=version.id, step="probe")
    session.add(job)
    session.commit()
    session.expire_all()

    job = session.scalars(select(Job)).one()
    assert job.status is JobStatus.QUEUED
    assert job.attempts == 0
    assert job.metrics == {}
    assert session.get(Project, project.id).status is ProjectStatus.RECEIVED  # type: ignore[union-attr]
    assert session.get(Version, version.id).highlights == []  # type: ignore[union-attr]


def test_invalid_status_rejected_by_check_constraint(session: Session) -> None:
    client = _client(session)
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(
            text("INSERT INTO projects (client_id, status) VALUES (:c, 'bogus')"),
            {"c": client.id},
        )


def test_duplicate_telegram_message_rejected(session: Session) -> None:
    client = _client(session)
    session.add(Project(client_id=client.id, telegram_chat_id=1, telegram_message_id=10))
    session.commit()
    session.add(Project(client_id=client.id, telegram_chat_id=1, telegram_message_id=10))
    with pytest.raises(IntegrityError):
        session.commit()


def test_version_numbers_unique_per_project(session: Session) -> None:
    client = _client(session)
    project = Project(client_id=client.id)
    session.add(project)
    session.flush()
    session.add_all([Version(project_id=project.id, number=1)])
    session.commit()
    session.add(Version(project_id=project.id, number=1))
    with pytest.raises(IntegrityError):
        session.commit()
