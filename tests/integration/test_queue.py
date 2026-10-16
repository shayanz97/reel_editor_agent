"""Queue semantics against Postgres (SKIP LOCKED, retries, orphan reset, fencing)."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from reels.db import queue
from reels.db.models import Client, Job, JobStatus, Project, Version

pytestmark = pytest.mark.integration


def _version(session: Session) -> int:
    client = Client(slug="q", name="Q")
    session.add(client)
    session.flush()
    project = Project(client_id=client.id)
    session.add(project)
    session.flush()
    version = Version(project_id=project.id, number=1)
    session.add(version)
    session.flush()
    return version.id


def test_enqueue_is_deduplicated_per_active_step(session: Session) -> None:
    vid = _version(session)
    first = queue.enqueue(session, vid, "probe")
    assert first is not None
    assert queue.enqueue(session, vid, "probe") is None
    assert queue.enqueue(session, vid, "audio") is not None
    session.execute(update(Job).where(Job.id == first).values(status=JobStatus.DONE))
    assert queue.enqueue(session, vid, "probe") is not None  # done jobs do not block
    session.commit()


def test_open_claim_is_skipped_by_second_claimer(engine: Engine) -> None:
    with Session(engine) as s:
        vid = _version(s)
        a_id, b_id = queue.enqueue(s, vid, "a"), queue.enqueue(s, vid, "b")
        s.commit()
    with Session(engine) as one, Session(engine) as two:
        first = queue.claim_next(one, "w1")  # transaction still open: row locked
        second = queue.claim_next(two, "w2")
        assert {first, second} == {a_id, b_id}
        one.commit()
        two.commit()


def test_two_workers_never_claim_the_same_job(engine: Engine) -> None:
    with Session(engine) as s:
        vid = _version(s)
        ids = {queue.enqueue(s, vid, f"step{i}") for i in range(60)}
        s.commit()
    claimed: dict[str, list[int]] = {"w1": [], "w2": [], "w3": []}

    def work(name: str) -> None:
        with Session(engine) as session:
            while (job_id := queue.claim_next(session, name)) is not None:
                session.commit()
                claimed[name].append(job_id)

    threads = [threading.Thread(target=work, args=(n,)) for n in claimed]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    all_claims = [j for js in claimed.values() for j in js]
    assert len(all_claims) == len(set(all_claims)) == len(ids)
    assert set(all_claims) == ids


def test_claim_increments_attempts_and_sets_token(session: Session) -> None:
    vid = _version(session)
    job_id = queue.enqueue(session, vid, "probe")
    assert queue.claim_next(session, "tok") == job_id
    session.commit()
    job = session.get(Job, job_id)
    assert job is not None
    assert (job.status, job.attempts, job.locked_by) == (JobStatus.RUNNING, 1, "tok")
    assert job.locked_at is not None and job.started_at is not None


def test_claim_can_be_limited_to_one_version(session: Session) -> None:
    v1 = _version(session)
    v2 = Version(project_id=session.get(Version, v1).project_id, number=2)  # type: ignore[union-attr]
    session.add(v2)
    session.flush()
    queue.enqueue(session, v1, "probe")
    other = queue.enqueue(session, v2.id, "probe")
    assert queue.claim_next(session, "t", version_id=v2.id) == other


def test_fail_retries_until_max_attempts(session: Session) -> None:
    vid = _version(session)
    job_id = queue.enqueue(session, vid, "render")
    statuses = []
    for _ in range(3):
        assert queue.claim_next(session, "t") == job_id
        statuses.append(queue.fail(session, job_id, "boom"))
    assert statuses == [JobStatus.QUEUED, JobStatus.QUEUED, JobStatus.FAILED]
    job = session.get(Job, job_id)
    assert job is not None and job.attempts == 3 and job.error == "boom"
    assert queue.claim_next(session, "t") is None


def test_permanent_failure_skips_retries(session: Session) -> None:
    job_id = queue.enqueue(session, _version(session), "probe")
    queue.claim_next(session, "t")
    assert queue.fail(session, job_id, "too long", permanent=True) is JobStatus.FAILED


def test_fail_on_job_that_is_not_running_is_ignored(session: Session) -> None:
    job_id = queue.enqueue(session, _version(session), "probe")
    assert job_id is not None
    assert queue.fail(session, job_id, "x") is None


def test_lock_for_execution_fences_stale_claims(engine: Engine) -> None:
    with Session(engine) as s:
        job_id = queue.enqueue(s, _version(s), "probe")
        queue.claim_next(s, "mine")
        s.commit()
    with Session(engine) as s:
        assert queue.lock_for_execution(s, job_id, "other") is None  # type: ignore[arg-type]
        assert queue.lock_for_execution(s, job_id, "mine") is not None  # type: ignore[arg-type]


def test_reset_orphaned_requeues_dead_but_skips_live_jobs(engine: Engine) -> None:
    with Session(engine) as s:
        vid = _version(s)
        dead, live = queue.enqueue(s, vid, "dead"), queue.enqueue(s, vid, "live")
        queue.claim_next(s, "t-dead")
        queue.claim_next(s, "t-live")
        old = datetime.now(UTC) - timedelta(minutes=10)
        s.execute(update(Job).values(locked_at=old))
        s.commit()

    with Session(engine) as worker, Session(engine) as restarter:
        assert queue.lock_for_execution(worker, live, "t-live") is not None  # type: ignore[arg-type]
        reset = queue.reset_orphaned(restarter, grace_s=60)
        restarter.commit()
        assert [(r.job_id, r.status) for r in reset] == [(dead, JobStatus.QUEUED)]
        worker.commit()

    with Session(engine) as s:
        statuses = {row[0]: row[1] for row in s.execute(select(Job.id, Job.status))}
        assert statuses == {dead: JobStatus.QUEUED, live: JobStatus.RUNNING}


def test_reset_orphaned_respects_grace_and_max_attempts(session: Session) -> None:
    vid = _version(session)
    fresh = queue.enqueue(session, vid, "fresh")
    exhausted = queue.enqueue(session, vid, "exhausted")
    queue.claim_next(session, "a")
    queue.claim_next(session, "b")
    session.execute(
        update(Job)
        .where(Job.id == exhausted)
        .values(attempts=3, locked_at=datetime.now(UTC) - timedelta(minutes=5))
    )
    session.commit()
    reset = queue.reset_orphaned(session, grace_s=60)
    assert [(r.job_id, r.status) for r in reset] == [(exhausted, JobStatus.FAILED)]
    assert session.get(Job, fresh).status is JobStatus.RUNNING  # type: ignore[union-attr]


def test_release_does_not_count_attempt(session: Session) -> None:
    job_id = queue.enqueue(session, _version(session), "probe")
    queue.claim_next(session, "t")
    queue.release(session, job_id)  # type: ignore[arg-type]
    session.commit()
    job = session.get(Job, job_id)
    assert job is not None and (job.status, job.attempts) == (JobStatus.QUEUED, 0)


def test_retry_failed_job(session: Session) -> None:
    vid = _version(session)
    job_id = queue.enqueue(session, vid, "render")
    assert job_id is not None
    queue.claim_next(session, "t")
    queue.fail(session, job_id, "x", permanent=True)
    assert queue.retry(session, job_id)
    job = session.get(Job, job_id)
    assert job is not None and (job.status, job.attempts, job.error) == (JobStatus.QUEUED, 0, None)
    assert not queue.retry(session, job_id)  # only failed jobs


def test_list_jobs(session: Session) -> None:
    vid = _version(session)
    for step in ("a", "b", "c"):
        queue.enqueue(session, vid, step)
    queue.claim_next(session, "t")
    assert [j.step for j in queue.list_jobs(session)] == ["c", "b", "a"]
    assert [j.step for j in queue.list_jobs(session, [JobStatus.RUNNING])] == ["a"]
