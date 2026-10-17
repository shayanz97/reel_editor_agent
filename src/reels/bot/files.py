"""Files from the local Bot API server."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp"}


def to_host_path(file_path: str, container_prefix: str, host_dir: Path) -> Path:
    """Map a path reported by the Bot API server (inside its container) to the host.

    `/var/lib/telegram-bot-api/<token>/documents/file_1.mp4`
    -> `<host_dir>/<token>/documents/file_1.mp4`
    """
    path = PurePosixPath(file_path)
    prefix = PurePosixPath(container_prefix)
    try:
        rel = path.relative_to(prefix)
    except ValueError as exc:
        raise ValueError(
            f"file path {file_path!r} is not below {container_prefix!r}; "
            "is the Bot API server running with --local and the volume mapped?"
        ) from exc
    if ".." in rel.parts:
        raise ValueError(f"refusing path with '..': {file_path!r}")
    return host_dir.joinpath(*rel.parts)


def looks_like_video(file_name: str | None, mime_type: str | None) -> bool:
    if mime_type and mime_type.startswith("video/"):
        return True
    return bool(file_name) and Path(file_name or "").suffix.lower() in VIDEO_SUFFIXES
