"""Text cards: a short text on a full-frame card (HTML template -> PNG -> short video)."""

from __future__ import annotations

import hashlib
import html
from pathlib import Path
from string import Template

from reels.media.ffmpeg import ffmpeg_bin, run
from reels.publish.covers import COVER_FONT, screenshot_pages


def template_path(templates_dir: Path, name: str = "default") -> Path:
    path = templates_dir / "textcard" / name / "card.html"
    if not path.exists():
        raise FileNotFoundError(f"text card template {name!r} not found: {path}")
    return path


def render_card_html(
    template: str, text: str, font: Path, text_color: str, accent_color: str, background: str
) -> str:
    return Template(template).substitute(
        text=html.escape(text),
        font_url=font.resolve().as_uri(),
        text_color=text_color,
        accent_color=accent_color,
        background=background,
    )


def render_textcard(
    text: str,
    out_dir: Path,
    duration_s: float,
    template: Path,
    fonts_dir: Path,
    text_color: str = "#FFFFFF",
    accent_color: str = "#FFD60A",
    background: str = "#111111",
    fps: int = 30,
) -> Path:
    """MP4 (1080 x 1920, no audio) of `duration_s`. Same inputs -> same file (cached)."""
    key = hashlib.sha1(
        f"{template}|{text}|{text_color}|{accent_color}|{background}|{duration_s}".encode()
    ).hexdigest()[:16]
    video = out_dir / f"card_{key}.mp4"
    if video.exists() and video.stat().st_size > 0:
        return video
    png = out_dir / f"card_{key}.png"
    content = render_card_html(
        template.read_text(encoding="utf-8"),
        text,
        fonts_dir / COVER_FONT,
        text_color,
        accent_color,
        background,
    )
    screenshot_pages([(content, png)], out_dir)
    tmp = video.with_suffix(".tmp.mp4")
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-loop", "1", "-framerate", str(fps), "-i", str(png), "-t", f"{duration_s:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an",
        str(tmp),
    ]  # fmt: skip
    run(cmd, timeout=120)
    tmp.replace(video)
    png.unlink()
    return video
