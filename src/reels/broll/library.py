"""The client's own B-roll library: uploads (#broll), indexing, similarity search.

Indexing: a still (middle frame or the photo itself) -> description by the vision model ->
embedding (OPENAI_MODEL_EMBED) stored with pgvector. Search ranks by cosine similarity; the
vision check decides whether a clip really fits (no invented similarity threshold).
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from reels.config import Settings
from reels.db import queue
from reels.db.models import EMBED_DIM, BrollAsset, BrollAssetStatus, BrollSource
from reels.llm.base import LLMProvider, load_prompt
from reels.llm.schemas import AssetDescription
from reels.log import get_logger
from reels.media import frames
from reels.media.probe import probe

log = get_logger(__name__)

INDEX_STEP = "broll_index"
INDEX_BATCH = 10


def library_dir(data_dir: Path, client_id: int) -> Path:
    return data_dir / "clients" / str(client_id) / "broll" / "library"


def is_known_upload(session: Session, chat_id: int, message_id: int) -> bool:
    return (
        session.scalar(
            select(BrollAsset.id).where(
                BrollAsset.source == BrollSource.LIBRARY,
                BrollAsset.meta["telegram_chat_id"].as_integer() == chat_id,
                BrollAsset.meta["telegram_message_id"].as_integer() == message_id,
            )
        )
        is not None
    )


def register_upload(
    session: Session,
    client_id: int,
    source: Path,
    media_type: str,
    app: Settings,
    meta: dict[str, Any] | None = None,
) -> BrollAsset:
    """Copy into the client's library and queue indexing. Caller commits."""
    folder = library_dir(app.data_dir, client_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{uuid.uuid4().hex[:12]}{source.suffix.lower() or '.bin'}"
    shutil.copy2(source, target)
    asset = BrollAsset(
        client_id=client_id,
        source=BrollSource.LIBRARY,
        media_type=media_type,
        path=str(target.resolve().relative_to(app.data_dir.resolve())),
        status=BrollAssetStatus.PENDING,
        meta=meta or {},
    )
    session.add(asset)
    session.flush()
    queue.enqueue(session, None, INDEX_STEP)
    return asset


def _still(asset: BrollAsset, app: Settings) -> tuple[Path, dict[str, Any]]:
    """Thumbnail (and size/duration for videos) of a library asset."""
    assert asset.path is not None
    path = app.data_dir / asset.path
    thumbs = path.parent / "thumbs"
    info: dict[str, Any] = {}
    if asset.media_type == "video":
        p = probe(path)
        info = {"width": p.width, "height": p.height, "duration_s": p.duration}
        still = frames.extract_frame(path, p.duration / 2, thumbs / f"{path.stem}.jpg")
    else:
        still = path
    small = frames.thumbnail(still, thumbs / f"{path.stem}_small.jpg")
    if asset.media_type != "video":
        import cv2

        image = cv2.imread(str(path))
        if image is None:
            raise ValueError(f"cannot read image {path}")
        info = {"width": int(image.shape[1]), "height": int(image.shape[0]), "duration_s": None}
    return small, info


def index_pending(
    session: Session, app: Settings, llm: LLMProvider | None, limit: int = INDEX_BATCH
) -> dict[str, Any]:
    """Describe and embed pending library assets. Failures mark only that asset."""
    if app.openai_embed_dim != EMBED_DIM:
        raise RuntimeError(
            f"OPENAI_EMBED_DIM={app.openai_embed_dim} but broll_assets.embedding has {EMBED_DIM} "
            "dimensions; a migration is needed before changing the embedding model"
        )
    pending = session.scalars(
        select(BrollAsset)
        .where(
            BrollAsset.source == BrollSource.LIBRARY, BrollAsset.status == BrollAssetStatus.PENDING
        )
        .order_by(BrollAsset.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    done = failed = 0
    for asset in pending:
        if llm is None:
            asset.status = BrollAssetStatus.FAILED
            asset.meta = {**asset.meta, "error": "no_llm"}
            failed += 1
            continue
        try:
            small, info = _still(asset, app)
            result = llm.structured(
                "broll_describe", load_prompt("broll_describe"), AssetDescription, images=[small]
            )
            text = result.description.strip() + " " + ", ".join(result.tags)
            (vector,) = llm.embed([text])
            if len(vector) != EMBED_DIM:
                raise ValueError(f"embedding has {len(vector)} dimensions, expected {EMBED_DIM}")
        except Exception as exc:  # one broken upload must not block the others
            log.warning("broll_index_failed", extra={"asset_id": asset.id, "error": str(exc)})
            asset.status = BrollAssetStatus.FAILED
            asset.meta = {**asset.meta, "error": str(exc)[:300]}
            failed += 1
            continue
        asset.thumbnail = str(small.resolve().relative_to(app.data_dir.resolve()))
        asset.width, asset.height, asset.duration_s = (
            info["width"],
            info["height"],
            info["duration_s"],
        )
        asset.description = text
        asset.embedding = list(vector)
        asset.status = BrollAssetStatus.READY
        done += 1
    return {"indexed": done, "failed": failed}


def search(
    session: Session,
    client_id: int,
    query_vector: Sequence[float],
    min_duration_s: float,
    limit: int = 3,
) -> list[BrollAsset]:
    """The client's ready assets, most similar first; videos must be long enough."""
    stmt = (
        select(BrollAsset)
        .where(
            BrollAsset.client_id == client_id,
            BrollAsset.source == BrollSource.LIBRARY,
            BrollAsset.status == BrollAssetStatus.READY,
            BrollAsset.embedding.is_not(None),
            (BrollAsset.media_type == "image") | (BrollAsset.duration_s >= min_duration_s),
        )
        .order_by(BrollAsset.embedding.cosine_distance(list(query_vector)))
        .limit(limit)
    )
    return list(session.scalars(stmt))
