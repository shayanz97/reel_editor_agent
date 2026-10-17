"""Cover candidates: frames from the ORIGINAL (no burnt-in subtitles), scored with OpenCV.

Sharpness = variance of the Laplacian (blurred frames have little edge energy).
Faces = OpenCV Haar cascade (frontal faces); reels with a speaker look best with the face.
The final choice is made by the LLM; this only pre-selects a handful of good candidates.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from reels.domain.cutplan import KeepRange
from reels.media.ffmpeg import ffmpeg_bin, run

# Stay away from cut edges: a frame right at a jump cut often shows a transition.
EDGE_MARGIN_S = 0.2


@dataclass(frozen=True)
class FrameScore:
    time: float  # original timeline
    path: Path
    sharpness: float
    faces: int
    face_area: float  # largest face, share of the frame (0..1)


def sample_times(keep: Sequence[KeepRange], count: int) -> list[float]:
    """`count` original-timeline times spread evenly over the kept parts of the video."""
    usable = [
        (r.start + EDGE_MARGIN_S, r.end - EDGE_MARGIN_S)
        for r in keep
        if r.end - r.start > 2 * EDGE_MARGIN_S
    ]
    total = sum(end - start for start, end in usable)
    if total <= 0 or count <= 0:
        return []
    times: list[float] = []
    for i in range(count):
        target = (i + 0.5) * total / count
        for start, end in usable:
            length = end - start
            if target <= length:
                times.append(round(start + target, 3))
                break
            target -= length
    return times


def extract_frame(src: Path, time: float, out: Path, width: int = 1080, height: int = 1920) -> Path:
    """One frame at `time`, cropped like the export (cover 9:16), as high-quality JPEG."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{time:.3f}", "-i", str(src), "-frames:v", "1",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
        "-q:v", "2", str(out),
    ]  # fmt: skip
    run(cmd, timeout=60)
    return out


def _cascade() -> object:
    import cv2

    path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")  # type: ignore[attr-defined]
    classifier = cv2.CascadeClassifier(path)
    if classifier.empty():
        raise RuntimeError(f"OpenCV face cascade not found: {path}")
    return classifier


def analyze(path: Path, time: float, cascade: object | None = None) -> FrameScore:
    import cv2

    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"cannot read frame {path}")
    # Analyse at half size: faster, and sharpness is compared between frames only.
    small = cv2.resize(image, (image.shape[1] // 2, image.shape[0] // 2))
    sharpness = float(cv2.Laplacian(small, cv2.CV_64F).var())
    classifier = cascade or _cascade()
    faces = classifier.detectMultiScale(small, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))  # type: ignore[attr-defined]
    area = max((w * h for (_, _, w, h) in faces), default=0) / float(
        small.shape[0] * small.shape[1]
    )
    return FrameScore(time, path, sharpness, len(faces), round(area, 4))


def select_candidates(
    scores: Sequence[FrameScore], k: int, min_gap_s: float = 1.5, blur_ratio: float = 0.5
) -> list[FrameScore]:
    """Drop clearly blurred frames (below `blur_ratio` x median sharpness), prefer frames with
    a (large) face, then sharper ones; keep `min_gap_s` between picks for variety. If that
    leaves fewer than `k`, the gap rule is relaxed."""
    if not scores:
        return []
    threshold = blur_ratio * median(s.sharpness for s in scores)
    sharp_enough = [s for s in scores if s.sharpness >= threshold] or list(scores)
    ranked = sorted(
        sharp_enough, key=lambda s: (s.faces > 0, s.face_area, s.sharpness), reverse=True
    )
    picked: list[FrameScore] = []
    for s in ranked:
        if all(abs(s.time - p.time) >= min_gap_s for p in picked):
            picked.append(s)
        if len(picked) == k:
            break
    for s in ranked:
        if len(picked) == k:
            break
        if s not in picked:
            picked.append(s)
    return sorted(picked, key=lambda s: s.time)


def thumbnail(src: Path, dst: Path, width: int = 432) -> Path:
    """Small JPEG for the vision model (only single preview images leave the machine)."""
    import cv2

    image = cv2.imread(str(src))
    if image is None:
        raise ValueError(f"cannot read frame {src}")
    height = round(image.shape[0] * width / image.shape[1])
    dst.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(dst), cv2.resize(image, (width, height)), [cv2.IMWRITE_JPEG_QUALITY, 85])
    return dst


def collect(
    src: Path, keep: Sequence[KeepRange], out_dir: Path, samples: int, k: int
) -> list[FrameScore]:
    """Extract `samples` frames, score them, return the `k` best (with thumbnails written)."""
    cascade = _cascade()
    scores = []
    for n, t in enumerate(sample_times(keep, samples), start=1):
        path = extract_frame(src, t, out_dir / f"frame_{n:02d}.jpg")
        scores.append(analyze(path, t, cascade))
    best = select_candidates(scores, k)
    for s in best:
        thumbnail(s.path, s.path.with_name(s.path.stem + "_small.jpg"))
    return best
