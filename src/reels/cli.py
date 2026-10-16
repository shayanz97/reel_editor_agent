"""`reels` command line interface (operator tool, not customer-facing)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select

from reels.config import get_settings
from reels.db.clients import ClientError, create_client, get_client, list_clients
from reels.db.glossary import export_csv, import_terms, load_terms, parse_csv
from reels.db.models import Client, Project
from reels.db.session import session_scope
from reels.log import configure_logging, get_logger, log_context

log = get_logger(__name__)

app = typer.Typer(no_args_is_help=True, help="Reels automation.")
client_app = typer.Typer(no_args_is_help=True, help="Manage clients.")
glossary_app = typer.Typer(no_args_is_help=True, help="Manage a client's glossary.")
jobs_app = typer.Typer(no_args_is_help=True, help="Inspect and control the job queue.")
app.add_typer(client_app, name="client")
app.add_typer(glossary_app, name="glossary")
app.add_typer(jobs_app, name="jobs")


@app.callback()
def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


def _require_client(session: object, ref: str) -> Client:
    client = get_client(session, ref)  # type: ignore[arg-type]
    if client is None:
        raise _fail(f"unknown client {ref!r}")
    return client


# --- clients -----------------------------------------------------------------


@client_app.command("add")
def client_add(
    name: Annotated[str, typer.Argument(help="Display name of the client.")],
    telegram_user: Annotated[
        list[int] | None,
        typer.Option(
            "--telegram-user", help="Telegram user id allowed to use the bot. Repeatable."
        ),
    ] = None,
    slug: Annotated[
        str | None, typer.Option(help="Short id used in paths and on the CLI (default: from name).")
    ] = None,
) -> None:
    """Create a client with default settings."""
    try:
        with session_scope() as session:
            client = create_client(session, name, slug=slug, telegram_user_ids=telegram_user or [])
            user_ids = [u.telegram_user_id for u in client.users]
            client_id, client_slug = client.id, client.slug
    except (ClientError, ValueError) as exc:
        raise _fail(str(exc)) from exc

    log.info("client_created", extra={"client_id": client_id, "slug": client_slug})
    users = ", ".join(map(str, user_ids)) or "-"
    typer.echo(f"created client id={client_id} slug={client_slug} telegram_users={users}")


@client_app.command("list")
def client_list() -> None:
    """List all clients."""
    with session_scope() as session:
        rows = [
            (
                str(c.id),
                c.slug,
                c.name,
                "yes" if c.active else "no",
                ", ".join(str(u.telegram_user_id) for u in c.users) or "-",
            )
            for c in list_clients(session)
        ]
    if not rows:
        typer.echo("no clients")
        return
    _print_table(("ID", "SLUG", "NAME", "ACTIVE", "TELEGRAM USERS"), rows)


@client_app.command("set")
def client_set(
    client: Annotated[str, typer.Argument(help="Client slug or id.")],
    assignments: Annotated[
        list[str], typer.Argument(help="key=value, dotted for nested, e.g. subtitle.karaoke=true")
    ],
) -> None:
    """Change client settings (validated; unknown keys are rejected)."""
    from reels.settings_schema import ClientSettings, apply_assignments

    with session_scope() as session:
        c = _require_client(session, client)
        try:
            updated = apply_assignments(c.client_settings(), assignments)
        except ValueError as exc:
            raise _fail(str(exc)) from exc
        c.settings = updated.model_dump(mode="json")
        assert isinstance(updated, ClientSettings)
    for a in assignments:
        typer.echo(f"set {a}")


def _print_table(header: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> None:
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())


# --- glossary ----------------------------------------------------------------


@glossary_app.command("import")
def glossary_import(
    client: Annotated[str, typer.Argument(help="Client slug or id.")],
    csv_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Import terms from CSV (term,variants,category,note,highlight,status; variants with |)."""
    try:
        terms = parse_csv(csv_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    with session_scope() as session:
        c = _require_client(session, client)
        added, updated = import_terms(session, c.id, terms)
    typer.echo(f"glossary import: {added} added, {updated} updated")


@glossary_app.command("export")
def glossary_export(
    client: Annotated[str, typer.Argument(help="Client slug or id.")],
    out: Annotated[Path | None, typer.Option(help="Write to file instead of stdout.")] = None,
) -> None:
    """Export the glossary as CSV."""
    with session_scope() as session:
        c = _require_client(session, client)
        text = export_csv(load_terms(session, c.id))
    if out is None:
        typer.echo(text, nl=False)
    else:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"written {out}")


# --- pipeline ----------------------------------------------------------------


@app.command("enqueue")
def enqueue(
    video: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    client: Annotated[str, typer.Option(help="Client slug or id.")],
) -> None:
    """Create a project with version 1 and queue it for the worker."""
    from reels.pipeline.runner import create_project

    settings = get_settings()
    with session_scope() as session:
        c = _require_client(session, client)
        version = create_project(session, c, video, settings)
        project_id, version_id = version.project_id, version.id
    typer.echo(f"queued project={project_id} version={version_id}")
    typer.echo("start a worker to process it: uv run python -m reels.worker")


@app.command("process")
def process(
    video: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    client: Annotated[str, typer.Option(help="Client slug or id.")],
    engine: Annotated[
        str | None, typer.Option(help="whisperx or mlx (default: TRANSCRIBE_ENGINE).")
    ] = None,
    llm: Annotated[bool, typer.Option(help="Use OpenAI for highlights.")] = True,
    project: Annotated[
        int | None,
        typer.Option(help="Re-run an existing project (reuses audio, transcript and VAD)."),
    ] = None,
) -> None:
    """Run all steps for one video right here (same jobs as the worker, no waiting)."""
    from reels.db.models import Version
    from reels.pipeline.runner import create_project, default_providers, rerun_from
    from reels.transcribe.base import get_engine
    from reels.worker.main import Worker, WorkerConfig

    settings = get_settings()
    with session_scope() as session:
        c = _require_client(session, client)
        if project is None:
            version = create_project(session, c, video, settings)
        else:
            p = session.get(Project, project)
            if p is None or p.client_id != c.id:
                raise _fail(f"project {project} not found for client {c.slug!r}")
            latest = session.scalar(
                select(Version).where(Version.project_id == p.id).order_by(Version.number.desc())
            )
            if latest is None:
                raise _fail(f"project {project} has no version")
            rerun_from(session, latest.id, "probe")
            version = latest
        project_id, version_id, client_id = version.project_id, version.id, c.id

    providers = default_providers()
    if engine is not None:
        providers.engine = lambda _name: get_engine(engine)
    if not llm:
        providers.llm = lambda: None
    elif providers.llm() is None:
        typer.echo("note: OPENAI_API_KEY/OPENAI_MODEL_TEXT not set, no highlights", err=True)

    config = WorkerConfig.from_settings(settings)
    worker = Worker(WorkerConfig(**{**config.__dict__, "isolate": False}), providers)
    with log_context(client_id=client_id, project_id=project_id, version_id=version_id):
        worker.run(until_idle=True, version_id=version_id)
    _print_summary(version_id)


def _print_summary(version_id: int) -> None:
    from reels.db.models import Job, JobStatus, Version, VersionStatus

    with session_scope() as session:
        version = session.get(Version, version_id)
        assert version is not None
        jobs = session.scalars(
            select(Job).where(Job.version_id == version_id).order_by(Job.id)
        ).all()
        done = {j.step: j.metrics for j in jobs if j.status is JobStatus.DONE}
        failed = [j for j in jobs if j.status is JobStatus.FAILED]
        status, export, project_id = version.status, version.export_path, version.project_id

    typer.echo(f"project/version:  {project_id} / {version_id} ({status.value})")
    if failed:
        j = failed[-1]
        raise _fail(f"step {j.step} failed after {j.attempts} attempts: {j.error}", code=2)
    if status is not VersionStatus.READY:
        typer.echo("not finished here; remaining steps run in a worker (see `reels jobs list`)")
        return
    probe, cut = done.get("probe", {}), done.get("cutplan", {})
    words = done.get("transcribe", {})
    typer.echo(f"export:           {get_settings().data_dir / (export or '')}")
    original, output = probe.get("duration_s", 0.0), cut.get("output_s", 0.0)
    typer.echo(f"original:         {original:.2f} s")
    typer.echo(f"new duration:     {output:.2f} s (-{original - output:.2f} s)")
    typer.echo(f"cuts:             {cut.get('cuts')}")
    typer.echo(f"removed fillers:  {cut.get('removed_words')}")
    typer.echo(
        f"words/segments:   {words.get('words')} / {done.get('segments', {}).get('segments')}, "
        f"highlights: {done.get('highlights', {}).get('highlights')}"
    )
    if words.get("reused"):
        typer.echo("transcript:       reused")
    hits = done.get("glossary", {}).get("hits") or {}
    if hits:
        typer.echo("glossary:         " + ", ".join(f"{t} x{n}" for t, n in hits.items()))
    hl = done.get("highlights", {})
    if hl.get("llm_input_tokens"):
        typer.echo(f"llm tokens:       in={hl['llm_input_tokens']} out={hl['llm_output_tokens']}")
    typer.echo("step timings:")
    total = 0.0
    for step, metrics in done.items():
        runtime = float(metrics.get("runtime_s", 0.0))
        total += runtime
        typer.echo(f"  {step:<15} {runtime:8.2f} s")
    typer.echo(f"  {'total':<15} {total:8.2f} s")


# --- jobs --------------------------------------------------------------------


@jobs_app.command("list")
def jobs_list(
    status: Annotated[
        list[str] | None, typer.Option(help="Filter: queued, running, done, failed. Repeatable.")
    ] = None,
    limit: Annotated[int, typer.Option(min=1)] = 30,
) -> None:
    """Most recent jobs first."""
    from reels.db import queue
    from reels.db.models import JobStatus

    try:
        statuses = [JobStatus(s) for s in status or []]
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    with session_scope() as session:
        rows = [
            (
                str(j.id),
                str(j.version_id or "-"),
                j.step,
                j.status.value,
                str(j.attempts),
                f"{j.metrics.get('runtime_s', 0):.1f}" if j.metrics.get("runtime_s") else "-",
                (j.error or "").splitlines()[0][:60] if j.error else "",
            )
            for j in queue.list_jobs(session, statuses, limit)
        ]
    if not rows:
        typer.echo("no jobs")
        return
    _print_table(("ID", "VERSION", "STEP", "STATUS", "TRIES", "SECONDS", "ERROR"), rows)


@jobs_app.command("retry")
def jobs_retry(job_id: Annotated[int, typer.Argument()]) -> None:
    """Put a failed job back into the queue (attempts start from zero)."""
    from reels.db import queue

    with session_scope() as session:
        ok = queue.retry(session, job_id)
    if not ok:
        raise _fail(f"job {job_id} is not failed or the step is already queued")
    typer.echo(f"job {job_id} queued again")


@jobs_app.command("rerun")
def jobs_rerun(
    version_id: Annotated[int, typer.Argument()],
    from_step: Annotated[str, typer.Option("--from", help="First step to run again.")] = "probe",
) -> None:
    """Run a version again from a step on; earlier results (audio, transcript) are reused."""
    from reels.pipeline.runner import rerun_from

    try:
        with session_scope() as session:
            job_id = rerun_from(session, version_id, from_step)
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    if job_id is None:
        raise _fail(f"step {from_step} is already queued or running for version {version_id}")
    typer.echo(f"queued job {job_id}: version {version_id} from {from_step}")


@app.command("bench-transcribe")
def bench_transcribe(
    video: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    engine: Annotated[
        list[str] | None, typer.Option(help="Engines to compare (default: whisperx and mlx).")
    ] = None,
    runs: Annotated[int, typer.Option(min=1, help="Runs per engine (2+ shows warm timings).")] = 1,
    client: Annotated[
        str | None, typer.Option(help="Use this client's glossary as transcription hint.")
    ] = None,
    out: Annotated[
        Path | None, typer.Option(help="Append the Markdown report to this file.")
    ] = None,
) -> None:
    """Run both transcription engines on the same video and compare them."""
    from reels.domain.glossary import build_prompt_hint
    from reels.media.audio import extract_audio
    from reels.pipeline.bench import format_report, run_benchmark
    from reels.pipeline.steps import write_json
    from reels.transcribe.base import get_engine

    settings = get_settings()
    hint = None
    if client:
        with session_scope() as session:
            hint = build_prompt_hint(load_terms(session, _require_client(session, client).id))

    digest = hashlib.sha1(f"{video.resolve()}:{video.stat().st_size}".encode()).hexdigest()[:8]
    work = settings.data_dir / "bench" / f"{video.stem}-{digest}"
    audio = extract_audio(video, work / "audio.wav")
    engines = {name: get_engine(name) for name in (engine or ["whisperx", "mlx"])}
    result = run_benchmark(video.name, audio, engines, runs=runs, hint=hint)
    for run in result.runs:
        write_json(work / f"transcript_{run.engine}.json", run.transcript)

    report = format_report(result)
    typer.echo(report)
    if out is not None:
        with out.open("a", encoding="utf-8") as fh:
            fh.write(report + "\n\n")
        typer.echo(f"report appended to {out}", err=True)


if __name__ == "__main__":
    app()
