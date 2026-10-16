"""`reels` command line interface (operator tool, not customer-facing)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from reels.config import get_settings
from reels.db.clients import ClientError, create_client, get_client, list_clients
from reels.db.glossary import add_hits, export_csv, import_terms, load_terms, parse_csv
from reels.db.models import Client, Project, ProjectStatus
from reels.db.session import session_scope
from reels.log import configure_logging, get_logger, log_context

log = get_logger(__name__)

app = typer.Typer(no_args_is_help=True, help="Reels automation.")
client_app = typer.Typer(no_args_is_help=True, help="Manage clients.")
glossary_app = typer.Typer(no_args_is_help=True, help="Manage a client's glossary.")
app.add_typer(client_app, name="client")
app.add_typer(glossary_app, name="glossary")


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


@app.command("process")
def process(
    video: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    client: Annotated[str, typer.Option(help="Client slug or id.")],
    engine: Annotated[
        str | None, typer.Option(help="whisperx or mlx (default: TRANSCRIBE_ENGINE).")
    ] = None,
    llm: Annotated[bool, typer.Option(help="Use OpenAI for highlights.")] = True,
    karaoke: Annotated[
        bool | None, typer.Option(help="Override the client's karaoke setting.")
    ] = None,
    project: Annotated[
        int | None,
        typer.Option(help="Re-run an existing project (reuses audio, transcript and VAD)."),
    ] = None,
    preset: Annotated[
        str | None, typer.Option(help="x264 preset override, e.g. veryfast for quick tests.")
    ] = None,
) -> None:
    """Run the full pipeline for one video and write version 1 (no bot, no queue)."""
    from reels.llm.openai_provider import OpenAIProvider
    from reels.media.ffmpeg import FFmpegError
    from reels.media.probe import VideoRejectedError
    from reels.pipeline.process import NoSpeechError, process_v1
    from reels.pipeline.steps import ProjectPaths
    from reels.transcribe.base import get_engine
    from reels.vad.silero import SileroVad

    settings = get_settings()
    with session_scope() as session:
        c = _require_client(session, client)
        client_settings = c.client_settings()
        if karaoke is not None:
            subtitle = client_settings.subtitle.model_copy(update={"karaoke": karaoke})
            client_settings = client_settings.model_copy(update={"subtitle": subtitle})
        terms = load_terms(session, c.id)
        if project is None:
            p = Project(client_id=c.id, status=ProjectStatus.PROCESSING, title=video.name)
            session.add(p)
            session.flush()
        else:
            existing = session.get(Project, project)
            if existing is None or existing.client_id != c.id:
                raise _fail(f"project {project} not found for client {c.slug!r}")
            existing.status = ProjectStatus.PROCESSING
            p = existing
        client_id, project_id = c.id, p.id

    provider = OpenAIProvider.from_settings(settings) if llm else None
    if llm and provider is None:
        typer.echo("note: OPENAI_API_KEY/OPENAI_MODEL_TEXT not set, no highlights", err=True)

    paths = ProjectPaths.for_project(settings.data_dir, client_id, project_id)
    with log_context(client_id=client_id, project_id=project_id):
        try:
            result = process_v1(
                video,
                paths,
                client_settings,
                terms,
                get_engine(engine or settings.transcribe_engine),
                SileroVad(),
                provider,
                settings.fonts_dir,
                render_preset=preset,
            )
        except (VideoRejectedError, NoSpeechError, FFmpegError, ValueError) as exc:
            _set_status(project_id, ProjectStatus.FAILED)
            log.error("process_failed", extra={"error": str(exc)})
            raise _fail(str(exc), code=2) from exc

    with session_scope() as session:
        add_hits(session, client_id, result.glossary_hits)
    _set_status(project_id, ProjectStatus.REVIEW)

    typer.echo(f"project:          {project_id} (client {client_id})")
    typer.echo(f"export:           {result.export}")
    typer.echo(f"original:         {result.original_duration:.2f} s")
    saved = result.original_duration - result.output_duration
    typer.echo(f"new duration:     {result.output_duration:.2f} s (-{saved:.2f} s)")
    typer.echo(f"cuts:             {result.cut_count}")
    typer.echo(f"removed fillers:  {result.removed_fillers}")
    typer.echo(
        f"words/segments:   {result.words} / {result.segments}, highlights: {result.highlights}"
    )
    if result.glossary_hits:
        hits = ", ".join(f"{t} x{n}" for t, n in result.glossary_hits.most_common())
        typer.echo(f"glossary:         {hits}")
    for u in result.llm_usage:
        typer.echo(
            f"llm:              {u.task} {u.model} in={u.input_tokens} out={u.output_tokens}"
        )
    typer.echo("step timings:")
    for name, seconds in result.timings.items():
        typer.echo(f"  {name:<15} {seconds:8.2f} s")
    typer.echo(f"  {'total':<15} {sum(result.timings.values()):8.2f} s")


def _set_status(project_id: int, status: ProjectStatus) -> None:
    with session_scope() as session:
        p = session.get(Project, project_id)
        if p is not None:
            p.status = status


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
