"""Worker process: the only place where media and AI work happens.

    uv run python -m reels.worker

- Claims jobs with SKIP LOCKED; several workers can run side by side.
- Runs every job in a child process, so a step can be killed when it exceeds its timeout.
- Resets orphaned jobs (worker died mid-step) at start and then every minute.
- SIGTERM/Ctrl+C: finish the current job, then exit. A second signal kills the job and puts
  it back into the queue without counting the attempt.
"""

from __future__ import annotations

import argparse
import importlib
import multiprocessing
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import FrameType

from sqlalchemy import select
from sqlalchemy.orm import Session

from reels.config import Settings, get_settings
from reels.db import queue
from reels.db.models import Job, JobStatus
from reels.db.session import make_engine
from reels.log import configure_logging, get_logger
from reels.pipeline.runner import (
    CLEANUP_STEP,
    Providers,
    execute_job,
    handle_failure,
    mark_failed,
)

log = get_logger(__name__)

ORPHAN_CHECK_INTERVAL_S = 60.0


@dataclass(frozen=True)
class WorkerConfig:
    database_url: str
    providers_factory: str = "reels.pipeline.runner:default_providers"
    isolate: bool = True  # child process per job (needed for timeouts)
    poll_interval_s: float = 1.0
    max_attempts: int = 3
    orphan_grace_s: float = 60.0
    step_timeout_s: float = 1800.0
    cleanup_interval_h: float = 24.0

    @classmethod
    def from_settings(cls, s: Settings) -> WorkerConfig:
        return cls(
            database_url=s.database_url,
            providers_factory=s.worker_providers_factory,
            poll_interval_s=s.worker_poll_interval_s,
            max_attempts=s.worker_max_attempts,
            orphan_grace_s=s.worker_orphan_grace_s,
            step_timeout_s=s.worker_step_timeout_s,
            cleanup_interval_h=s.worker_cleanup_interval_h,
        )


def load_factory(path: str) -> Callable[[], Providers]:
    """'package.module:function' -> function (must be importable in a spawned process)."""
    module, _, attr = path.partition(":")
    factory: Callable[[], Providers] = getattr(importlib.import_module(module), attr)
    return factory


def _child_main(config: WorkerConfig, job_id: int, token: str) -> None:
    # The parent decides about shutdown; a Ctrl+C on the terminal must not hit the step.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    engine = make_engine(config.database_url)
    try:
        execute_job(engine, job_id, token, load_factory(config.providers_factory)(),
                    config.max_attempts)  # fmt: skip
    finally:
        engine.dispose()


class Worker:
    def __init__(self, config: WorkerConfig, providers: Providers | None = None) -> None:
        self.config = config
        self.engine = make_engine(config.database_url)
        self.providers = providers or load_factory(config.providers_factory)()
        self._stop = threading.Event()
        self._force = threading.Event()
        self._last_orphan_check = 0.0
        self._last_cleanup_check = 0.0

    # --- lifecycle -------------------------------------------------------

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    def _on_signal(self, signum: int, frame: FrameType | None) -> None:
        if self._stop.is_set():
            log.warning("worker_force_stop", extra={"signal": signum})
            self._force.set()
        else:
            log.info("worker_stopping", extra={"signal": signum})
            self._stop.set()

    def stop(self) -> None:
        self._stop.set()

    def run(
        self,
        *,
        until_idle: bool = False,
        version_id: int | None = None,
        max_jobs: int | None = None,
    ) -> int:
        """Process jobs until stopped (or until nothing is queued, with `until_idle`)."""
        log.info("worker_started", extra={"isolate": self.config.isolate})
        self.reset_orphans()
        processed = 0
        while not self._stop.is_set():
            claim = self._claim(version_id)
            if claim is None:
                if until_idle:
                    break
                self._idle_housekeeping()
                self._stop.wait(self.config.poll_interval_s)
                continue
            self._run_job(*claim)
            processed += 1
            if max_jobs is not None and processed >= max_jobs:
                break
        log.info("worker_stopped", extra={"jobs": processed})
        self.engine.dispose()
        return processed

    # --- queue -----------------------------------------------------------

    def _claim(self, version_id: int | None) -> tuple[int, str] | None:
        token = queue.new_lock_token()
        with Session(self.engine) as session:
            job_id = queue.claim_next(session, token, version_id=version_id)
            session.commit()
        return (job_id, token) if job_id is not None else None

    def reset_orphans(self) -> None:
        self._last_orphan_check = time.monotonic()
        with Session(self.engine) as session:
            reset = queue.reset_orphaned(
                session, self.config.orphan_grace_s, self.config.max_attempts
            )
            for r in reset:
                if r.status is JobStatus.FAILED:
                    job = session.get(Job, r.job_id)
                    if job is not None:
                        mark_failed(session, job)
            session.commit()
        for r in reset:
            log.warning("job_orphan_reset", extra={"job_id": r.job_id, "status": r.status.value})
            if r.status is JobStatus.FAILED:
                self.providers.notifier.notify_admin(
                    f"Job {r.job_id} endgültig fehlgeschlagen: Worker während des Jobs beendet"
                )

    def _idle_housekeeping(self) -> None:
        now = time.monotonic()
        if now - self._last_orphan_check >= ORPHAN_CHECK_INTERVAL_S:
            self.reset_orphans()
        if now - self._last_cleanup_check >= ORPHAN_CHECK_INTERVAL_S:
            self._last_cleanup_check = now
            self.schedule_cleanup()

    def schedule_cleanup(self) -> int | None:
        """Queue the retention cleanup if it has not run within the configured interval."""
        since = datetime.now(UTC) - timedelta(hours=self.config.cleanup_interval_h)
        with Session(self.engine) as session:
            recent = session.scalar(
                select(Job.id).where(Job.step == CLEANUP_STEP, Job.created_at > since).limit(1)
            )
            job_id = None if recent is not None else queue.enqueue(session, None, CLEANUP_STEP)
            session.commit()
        return job_id

    # --- execution -------------------------------------------------------

    def _run_job(self, job_id: int, token: str) -> None:
        if not self.config.isolate:
            execute_job(self.engine, job_id, token, self.providers, self.config.max_attempts)
            return
        ctx = multiprocessing.get_context("spawn")
        child = ctx.Process(target=_child_main, args=(self.config, job_id, token))
        child.start()
        deadline = time.monotonic() + self.config.step_timeout_s
        while child.is_alive() and time.monotonic() < deadline and not self._force.is_set():
            child.join(timeout=0.5)

        if child.is_alive():
            forced = self._force.is_set()
            _kill(child)
            with Session(self.engine) as session:
                if forced:
                    queue.release(session, job_id)
                    session.commit()
                    log.warning("job_released", extra={"job_id": job_id})
                    return
            handle_failure(
                self.engine,
                job_id,
                f"timeout: step exceeded {self.config.step_timeout_s:.0f} s",
                self.providers.notifier,
                max_attempts=self.config.max_attempts,
            )
        elif child.exitcode != 0:
            # Crashed before it could record anything (segfault, out of memory, …).
            handle_failure(
                self.engine,
                job_id,
                f"worker process exited with code {child.exitcode}",
                self.providers.notifier,
                max_attempts=self.config.max_attempts,
            )


def _kill(child: multiprocessing.process.BaseProcess) -> None:
    child.terminate()
    child.join(timeout=5)
    if child.is_alive():
        child.kill()
        child.join()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m reels.worker", description=__doc__)
    parser.add_argument("--until-idle", action="store_true", help="exit when the queue is empty")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    worker = Worker(WorkerConfig.from_settings(settings))
    worker.install_signal_handlers()
    worker.run(until_idle=args.until_idle)
