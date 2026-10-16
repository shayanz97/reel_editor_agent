"""Retention: delete originals and intermediate files `retention_days` after approval.

Kept: the exports (`v<n>/export.mp4`) and covers (`v<n>/covers/`). Idempotent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from reels.db.models import Asset, AssetKind, Client, Project
from reels.log import get_logger
from reels.pipeline.steps import ProjectPaths

log = get_logger(__name__)

_DELETED_KINDS = (AssetKind.ORIGINAL, AssetKind.AUDIO, AssetKind.PREVIEW)


def _keep(rel: Path) -> bool:
    parts = rel.parts
    if len(parts) >= 2 and parts[0].startswith("v") and parts[0][1:].isdigit():
        return parts[1] == "export.mp4" or parts[1] == "covers"
    return False


def cleanup_expired(
    session: Session, data_dir: Path, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    projects = session.scalars(select(Project).where(Project.approved_at.is_not(None))).all()
    cleaned = files = freed = 0
    for project in projects:
        client = session.get(Client, project.client_id)
        assert client is not None and project.approved_at is not None
        if project.approved_at + timedelta(days=client.client_settings().retention_days) > now:
            continue
        root = ProjectPaths.for_project(data_dir, client.id, project.id).root
        removed_here = 0
        if root.exists():
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_file() and not _keep(path.relative_to(root)):
                    freed += path.stat().st_size
                    path.unlink()
                    removed_here += 1
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
        session.execute(
            delete(Asset).where(Asset.project_id == project.id, Asset.kind.in_(_DELETED_KINDS))
        )
        if removed_here:
            cleaned += 1
            files += removed_here
            log.info(
                "project_cleaned",
                extra={"project_id": project.id, "files": removed_here},
            )
    return {"projects_cleaned": cleaned, "files_deleted": files, "bytes_freed": freed}
