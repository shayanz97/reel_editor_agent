"""Job queue on Postgres (`SELECT … FOR UPDATE SKIP LOCKED`), no Redis.

Lifecycle: queued -> running (claim) -> done | queued (retry) | failed.

While a job's step executes, its row stays locked (`lock_for_execution`) inside the step's
transaction. A running job whose row is *not* locked therefore has no live process behind it
and can be reset safely (`reset_orphaned`), no matter how long the step normally takes.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from reels.db.models import Job, JobStatus

DEFAULT_MAX_ATTEMPTS = 3
ERROR_MAX_CHARS = 4000
_ACTIVE = text("status IN ('queued', 'running')")


def new_lock_token() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def enqueue(session: Session, version_id: int | None, step: str) -> int | None:
    """Queue a step. Returns the job id, or None if the step is already queued or running."""
    stmt = (
        pg_insert(Job)
        .values(version_id=version_id, step=step, status=JobStatus.QUEUED)
        .on_conflict_do_nothing(index_elements=["version_id", "step"], index_where=_ACTIVE)
        .returning(Job.id)
    )
    return session.scalar(stmt)


def claim_next(session: Session, token: str, version_id: int | None = None) -> int | None:
    """Atomically take the oldest queued job (optionally of one version). Caller commits."""
    candidate = (
        select(Job.id)
        .where(Job.status == JobStatus.QUEUED)
        .order_by(Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if version_id is not None:
        candidate = candidate.where(Job.version_id == version_id)
    now = func.now()
    stmt = (
        update(Job)
        .where(Job.id == candidate.scalar_subquery())
        .values(
            status=JobStatus.RUNNING,
            attempts=Job.attempts + 1,
            locked_at=now,
            locked_by=token,
            started_at=func.coalesce(Job.started_at, now),
        )
        .returning(Job.id)
    )
    return session.scalar(stmt)


def lock_for_execution(session: Session, job_id: int, token: str) -> Job | None:
    """Lock the job row for the duration of the step. None if the claim is no longer ours."""
    return session.scalar(
        select(Job)
        .where(Job.id == job_id, Job.status == JobStatus.RUNNING, Job.locked_by == token)
        .with_for_update()
    )


def complete(session: Session, job: Job, metrics: dict[str, Any]) -> None:
    job.status = JobStatus.DONE
    job.metrics = metrics
    job.error = None
    job.finished_at = datetime.now(UTC)


def fail(
    session: Session,
    job_id: int,
    error: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    permanent: bool = False,
    metrics: dict[str, Any] | None = None,
) -> JobStatus | None:
    """Record a failed attempt: back to `queued` while attempts remain, else `failed`.

    Returns the new status, or None if the job was not running (already handled elsewhere).
    """
    job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None or job.status != JobStatus.RUNNING:
        return None
    job.error = error[-ERROR_MAX_CHARS:]
    job.locked_by = None
    if metrics:
        job.metrics = {**job.metrics, **metrics}
    if permanent or job.attempts >= max_attempts:
        job.status = JobStatus.FAILED
        job.finished_at = datetime.now(UTC)
    else:
        job.status = JobStatus.QUEUED
    return job.status


def release(session: Session, job_id: int) -> None:
    """Give a running job back without counting the attempt (worker shut down on purpose)."""
    session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.RUNNING)
        .values(status=JobStatus.QUEUED, attempts=Job.attempts - 1, locked_by=None)
    )


@dataclass(frozen=True)
class ResetJob:
    job_id: int
    status: JobStatus


def reset_orphaned(
    session: Session, grace_s: float = 60.0, max_attempts: int = DEFAULT_MAX_ATTEMPTS
) -> list[ResetJob]:
    """Requeue running jobs that no live process holds (their worker died).

    Rows locked by an executing step are skipped. `grace_s` covers the moment between claim
    and the executing process taking its row lock. The lost attempt counts, so a job that
    keeps killing its worker ends up `failed` instead of looping forever.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=grace_s)
    orphans = session.scalars(
        select(Job)
        .where(Job.status == JobStatus.RUNNING, Job.locked_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    result: list[ResetJob] = []
    for job in orphans:
        job.locked_by = None
        job.error = "worker stopped while the job was running"
        if job.attempts >= max_attempts:
            job.status = JobStatus.FAILED
            job.finished_at = datetime.now(UTC)
        else:
            job.status = JobStatus.QUEUED
        result.append(ResetJob(job.id, job.status))
    return result


def retry(session: Session, job_id: int) -> bool:
    """Put a failed job back into the queue with fresh attempts. False if not possible."""
    job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None or job.status != JobStatus.FAILED:
        return False
    active = session.scalar(
        select(Job.id).where(
            Job.version_id == job.version_id,
            Job.step == job.step,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
    )
    if active is not None:
        return False
    job.status = JobStatus.QUEUED
    job.attempts = 0
    job.error = None
    job.finished_at = None
    return True


def list_jobs(session: Session, statuses: Sequence[JobStatus] = (), limit: int = 50) -> list[Job]:
    stmt = select(Job).order_by(Job.id.desc()).limit(limit)
    if statuses:
        stmt = stmt.where(Job.status.in_(list(statuses)))
    return list(session.scalars(stmt))
