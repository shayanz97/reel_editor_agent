"""Client management (used by the CLI and later by the bot's admin commands)."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from reels.db.models import Client, ClientUser
from reels.domain.naming import slugify
from reels.settings_schema import ClientSettings


class ClientError(Exception):
    pass


def create_client(
    session: Session,
    name: str,
    *,
    slug: str | None = None,
    telegram_user_ids: Iterable[int] = (),
    settings: ClientSettings | None = None,
) -> Client:
    """Create a client with default (or given) settings. Raises ClientError on conflicts."""
    slug = slugify(slug or name)
    if session.scalar(select(Client.id).where(Client.slug == slug)) is not None:
        raise ClientError(f"client with slug {slug!r} already exists")

    user_ids = list(dict.fromkeys(telegram_user_ids))
    if user_ids:
        taken = session.scalars(
            select(ClientUser.telegram_user_id).where(ClientUser.telegram_user_id.in_(user_ids))
        ).all()
        if taken:
            raise ClientError(f"telegram user(s) already assigned to a client: {sorted(taken)}")

    client = Client(
        slug=slug,
        name=name.strip(),
        settings=(settings or ClientSettings()).model_dump(mode="json"),
        users=[ClientUser(telegram_user_id=uid) for uid in user_ids],
    )
    session.add(client)
    session.flush()
    return client


def list_clients(session: Session) -> list[Client]:
    stmt = select(Client).options(selectinload(Client.users)).order_by(Client.id)
    return list(session.scalars(stmt))


def get_client(session: Session, ref: str) -> Client | None:
    """Look up a client by slug or numeric id."""
    if ref.isdigit():
        return session.get(Client, int(ref))
    return session.scalar(select(Client).where(Client.slug == ref))
