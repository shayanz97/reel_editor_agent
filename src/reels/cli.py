"""`reels` command line interface (operator tool, not customer-facing)."""

from __future__ import annotations

from typing import Annotated

import typer

from reels.config import get_settings
from reels.db.clients import ClientError, create_client, list_clients
from reels.db.session import session_scope
from reels.log import configure_logging, get_logger

log = get_logger(__name__)

app = typer.Typer(no_args_is_help=True, help="Reels automation.")
client_app = typer.Typer(no_args_is_help=True, help="Manage clients.")
app.add_typer(client_app, name="client")


@app.callback()
def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)


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
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

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
    header = ("ID", "SLUG", "NAME", "ACTIVE", "TELEGRAM USERS")
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())


if __name__ == "__main__":
    app()
