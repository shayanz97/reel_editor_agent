"""Covers: HTML/CSS template per client, rendered to PNG (1080 x 1920) with Playwright."""

from __future__ import annotations

import html
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from string import Template

WIDTH, HEIGHT = 1080, 1920
COVER_FONT = "Montserrat[wght].ttf"
READY_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class CoverSpec:
    number: int
    frame: Path
    headline: str


def template_path(templates_dir: Path, name: str) -> Path:
    path = templates_dir / "cover" / name / "cover.html"
    if not path.exists():
        raise FileNotFoundError(f"cover template {name!r} not found: {path}")
    return path


def render_html(
    template: str, frame: Path, headline: str, font: Path, text_color: str, accent_color: str
) -> str:
    return Template(template).substitute(
        frame_url=frame.resolve().as_uri(),
        headline=html.escape(headline),
        font_url=font.resolve().as_uri(),
        text_color=text_color,
        accent_color=accent_color,
    )


def render_covers(
    specs: Sequence[CoverSpec],
    template: Path,
    fonts_dir: Path,
    out_dir: Path,
    text_color: str,
    accent_color: str,
) -> list[Path]:
    """Render one PNG per spec (`cover_<n>.png`) with a single headless Chromium."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    source = template.read_text(encoding="utf-8")
    font = fonts_dir / COVER_FONT
    results: list[Path] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(
                viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1
            )
            for spec in specs:
                page_file = out_dir / f"cover_{spec.number}.html"
                page_file.write_text(
                    render_html(source, spec.frame, spec.headline, font, text_color, accent_color),
                    encoding="utf-8",
                )
                page.goto(page_file.resolve().as_uri(), wait_until="load")
                # Templates without the ready flag are rendered as they are after the timeout.
                with suppress(PlaywrightTimeout):
                    page.wait_for_function(
                        "document.body.dataset.ready === '1'", timeout=READY_TIMEOUT_MS
                    )
                png = out_dir / f"cover_{spec.number}.png"
                page.screenshot(path=str(png), full_page=False)
                page_file.unlink()
                results.append(png)
        finally:
            browser.close()
    return results
