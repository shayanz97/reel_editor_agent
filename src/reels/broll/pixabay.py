"""Pixabay video search (B-roll stock). https://pixabay.com/api/docs/

API rules followed here: search results are cached for 24 hours, files are downloaded once
into the local cache (no hotlinking), only a few results per spot are fetched (no systematic
mass downloads), and the source page URL is kept for attribution.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from reels.log import get_logger

log = get_logger(__name__)

API_URL = "https://pixabay.com/api/videos/"
CACHE_TTL_S = 24 * 3600
MAX_QUERY_CHARS = 100  # API limit
RENDITIONS = ("medium", "large", "small", "tiny")  # medium is ~1080p and always present


class StockUnavailableError(Exception):
    """No key, rate limit or network error: the spot gets no stock clip."""


@dataclass(frozen=True)
class StockVideo:
    id: int
    page_url: str
    tags: str
    duration: float
    url: str
    width: int
    height: int
    thumbnail: str

    @property
    def portrait(self) -> bool:
        return self.height > self.width


def _pick_rendition(videos: dict[str, Any]) -> dict[str, Any] | None:
    for name in RENDITIONS:
        rendition = videos.get(name) or {}
        if rendition.get("url"):
            return dict(rendition)
    return None


def parse_hits(data: dict[str, Any]) -> list[StockVideo]:
    results = []
    for hit in data.get("hits", []):
        rendition = _pick_rendition(hit.get("videos") or {})
        if rendition is None:
            continue
        results.append(
            StockVideo(
                id=int(hit["id"]),
                page_url=str(hit.get("pageURL", "")),
                tags=str(hit.get("tags", "")),
                duration=float(hit.get("duration") or 0),
                url=str(rendition["url"]),
                width=int(rendition.get("width") or 0),
                height=int(rendition.get("height") or 0),
                thumbnail=str(rendition.get("thumbnail") or ""),
            )
        )
    return results


class PixabayClient:
    def __init__(
        self,
        api_key: str,
        cache_dir: Path,
        http: httpx.Client | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.http = http or httpx.Client(timeout=timeout_s, follow_redirects=True)

    def search(self, query: str, min_duration_s: float, per_page: int = 10) -> list[StockVideo]:
        """Videos for `query`, at least `min_duration_s` long, portrait first."""
        query = " ".join(query.split())[:MAX_QUERY_CHARS]
        data = self._cached_search(query, per_page)
        videos = [v for v in parse_hits(data) if v.duration >= min_duration_s]
        return sorted(videos, key=lambda v: not v.portrait)  # stable: keeps the API ranking

    def _cached_search(self, query: str, per_page: int) -> dict[str, Any]:
        key = hashlib.sha1(f"{query}|{per_page}".encode()).hexdigest()
        path = self.cache_dir / "search" / f"{key}.json"
        if path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL_S:
            cached: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return cached
        params: dict[str, str | int] = {
            "key": self.api_key,
            "q": query,
            "video_type": "film",
            "safesearch": "true",
            "per_page": max(3, min(per_page, 200)),
        }
        try:
            response = self.http.get(API_URL, params=params)
        except httpx.HTTPError as exc:
            raise StockUnavailableError(f"pixabay request failed: {exc}") from exc
        if response.status_code == 429:
            raise StockUnavailableError("pixabay rate limit exceeded")
        if response.status_code != 200:
            raise StockUnavailableError(f"pixabay returned {response.status_code}")
        data: dict[str, Any] = response.json()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def _download(self, url: str, dst: Path) -> Path:
        if dst.exists() and dst.stat().st_size > 0:
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        try:
            with self.http.stream("GET", url) as response:
                if response.status_code != 200:
                    raise StockUnavailableError(f"download failed ({response.status_code})")
                with tmp.open("wb") as fh:
                    for chunk in response.iter_bytes():
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise StockUnavailableError(f"download failed: {exc}") from exc
        tmp.replace(dst)
        return dst

    def download_video(self, video: StockVideo) -> Path:
        return self._download(video.url, self.cache_dir / "videos" / f"{video.id}.mp4")

    def download_thumbnail(self, video: StockVideo) -> Path | None:
        if not video.thumbnail:
            return None
        return self._download(video.thumbnail, self.cache_dir / "thumbs" / f"{video.id}.jpg")

    @staticmethod
    def meta(video: StockVideo) -> dict[str, Any]:
        return asdict(video)
