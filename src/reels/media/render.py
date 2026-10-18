"""Rendering the cut video from the ORIGINAL with FFmpeg (CLAUDE.md §6)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from reels.domain.cutplan import KeepRange
from reels.media.ffmpeg import ffmpeg_bin, run
from reels.media.probe import ProbeResult
from reels.settings_schema import ClientSettings

# Characters that would need two levels of FFmpeg filtergraph escaping.
_UNSAFE_PATH_CHARS = set("'\\:,;[]")


@dataclass(frozen=True)
class Overlay:
    """Full-frame B-roll over the speaker, on the OUTPUT timeline. Its sound is not used."""

    path: Path
    start: float
    end: float
    still: bool = False  # an image (looped) instead of a video

    @property
    def duration(self) -> float:
        return self.end - self.start


# Short cross-fade in and out of every B-roll.
OVERLAY_FADE_S = 0.25


@dataclass(frozen=True)
class RenderOptions:
    width: int = 1080
    height: int = 1920
    max_fps: int = 30
    zoom_on_cuts: bool = True
    zoom_factor: float = 1.08
    audio_fade_ms: int = 15
    loudness_lufs: float = -14.0
    crf: int = 20
    preset: str = "medium"
    audio_bitrate: str = "192k"

    @classmethod
    def from_settings(cls, s: ClientSettings) -> RenderOptions:
        return cls(
            zoom_on_cuts=s.zoom_on_cuts,
            zoom_factor=s.zoom_factor,
            audio_fade_ms=s.audio_fade_ms,
            loudness_lufs=s.loudness_lufs,
        )


def _even(x: float) -> int:
    return max(2, round(x / 2) * 2)


def filter_path(path: Path) -> str:
    """Quote a file path for use as a filter option value."""
    text = str(path.resolve())
    bad = _UNSAFE_PATH_CHARS & set(text)
    if bad:
        raise ValueError(f"path contains characters unsupported in filter graphs {bad}: {text}")
    return f"'{text}'"


def output_frame_rate(src: ProbeResult, max_fps: int) -> str:
    """Source rate if it is at most `max_fps`, else `max_fps`."""
    return src.frame_rate if Fraction(src.frame_rate) <= max_fps else str(max_fps)


def build_filter_graph(
    keep: Sequence[KeepRange],
    src: ProbeResult,
    opts: RenderOptions,
    ass_path: Path | None = None,
    fonts_dir: Path | None = None,
    overlays: Sequence[Overlay] = (),
) -> str:
    """filter_complex for trim/atrim per keep range + concat, B-roll overlays, subtitles.

    Inputs: 0 = original, 1.. = overlays (in order). Outputs [vout] and [aout].
    """
    if not keep:
        raise ValueError("cut plan keeps nothing")
    n = len(keep)
    w, h = opts.width, opts.height
    fade = opts.audio_fade_ms / 1000
    chains = [
        # Cover the 9:16 frame: portrait is scaled, landscape is centre-cropped.
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,"
        f"split={n}" + "".join(f"[vs{i}]" for i in range(n)),
        "[0:a]asplit=" + str(n) + "".join(f"[as{i}]" for i in range(n)),
    ]
    zw, zh = _even(w * opts.zoom_factor), _even(h * opts.zoom_factor)
    for i, r in enumerate(keep):
        v = f"[vs{i}]trim=start={r.start:.3f}:end={r.end:.3f},setpts=PTS-STARTPTS"
        # Jump-cut zoom: alternate 1.0 and zoom_factor per kept range.
        if opts.zoom_on_cuts and i % 2 == 1 and opts.zoom_factor > 1.0:
            v += f",scale={zw}:{zh},crop={w}:{h}"
        # The zoom scale is not exactly 9:16; concat needs identical SAR on all inputs.
        chains.append(v + f",setsar=1[v{i}]")
        a = f"[as{i}]atrim=start={r.start:.3f}:end={r.end:.3f},asetpts=PTS-STARTPTS"
        duration = r.end - r.start
        if fade > 0 and duration > 2 * fade:
            a += f",afade=t=in:st=0:d={fade:.3f},afade=t=out:st={duration - fade:.3f}:d={fade:.3f}"
        chains.append(a + f"[a{i}]")
    chains.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")

    rate = output_frame_rate(src, opts.max_fps)
    chains.append(f"[vc]fps={rate}[base]")
    current = "base"
    for j, o in enumerate(overlays):
        f = min(OVERLAY_FADE_S, o.duration / 2)
        chains.append(
            f"[{j + 1}:v]trim=duration={o.duration:.3f},setpts=PTS-STARTPTS,"
            f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,"
            f"fps={rate},format=yuva420p,"
            f"fade=t=in:st=0:d={f:.3f}:alpha=1,"
            f"fade=t=out:st={o.duration - f:.3f}:d={f:.3f}:alpha=1,"
            f"setpts=PTS+{o.start:.3f}/TB[o{j}]"
        )
        chains.append(
            f"[{current}][o{j}]overlay=eof_action=pass:"
            f"enable='between(t,{o.start:.3f},{o.end:.3f})'[b{j}]"
        )
        current = f"b{j}"
    if ass_path is not None:
        # Subtitles last, so they sit on top of everything, also on top of B-roll.
        video = f"[{current}]subtitles=filename={filter_path(ass_path)}"
        if fonts_dir is not None:
            video += f":fontsdir={filter_path(fonts_dir)}"
    else:
        video = f"[{current}]null"
    chains.append(video + "[vout]")
    chains.append(f"[ac]loudnorm=I={opts.loudness_lufs:g}:TP=-1.5:LRA=11,aresample=48000[aout]")
    return ";".join(chains)


def render(
    src_path: Path,
    dst: Path,
    keep: Sequence[KeepRange],
    src: ProbeResult,
    opts: RenderOptions,
    ass_path: Path | None = None,
    fonts_dir: Path | None = None,
    timeout: float | None = None,
    overlays: Sequence[Overlay] = (),
) -> Path:
    """Render to 1080x1920 H.264 (yuv420p) + AAC 48 kHz. Writes atomically."""
    graph = build_filter_graph(keep, src, opts, ass_path, fonts_dir, overlays)
    overlay_inputs: list[str] = []
    for o in overlays:
        if o.still:
            overlay_inputs += ["-loop", "1", "-t", f"{o.duration:.3f}"]
        overlay_inputs += ["-i", str(o.path)]
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".tmp" + dst.suffix)
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src_path),
        *overlay_inputs,
        "-filter_complex", graph,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", opts.preset, "-crf", str(opts.crf),
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-c:a", "aac", "-b:a", opts.audio_bitrate, "-ar", "48000",
        "-map_metadata", "-1",
        "-movflags", "+faststart",
        str(tmp),
    ]  # fmt: skip
    run(cmd, timeout=timeout)
    tmp.replace(dst)
    return dst
