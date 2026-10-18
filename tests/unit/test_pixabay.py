"""Pixabay client against a mocked HTTP transport (no network)."""

import json
from pathlib import Path

import httpx
import pytest

from reels.broll.pixabay import PixabayClient, StockUnavailableError, parse_hits


def _hit(id_: int, w: int, h: int, duration: float, large_url: str = "") -> dict:  # type: ignore[type-arg]
    return {
        "id": id_, "pageURL": f"https://pixabay.com/videos/x-{id_}/", "tags": "server, room",
        "duration": duration,
        "videos": {
            "large": {"url": large_url, "width": 0, "height": 0, "size": 0, "thumbnail": ""},
            "medium": {"url": f"https://cdn.example/{id_}.mp4", "width": w, "height": h, "size": 1,
                       "thumbnail": f"https://cdn.example/{id_}.jpg"},
        },
    }  # fmt: skip


HITS = {
    "total": 3,
    "totalHits": 3,
    "hits": [_hit(1, 1920, 1080, 12), _hit(2, 1080, 1920, 8), _hit(3, 1080, 1920, 1)],
}


def _client(tmp_path: Path, handler) -> tuple[PixabayClient, list[httpx.Request]]:  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(record))
    return PixabayClient("KEY", tmp_path, http=http), seen


def test_parse_hits_prefers_medium_rendition() -> None:
    (video,) = parse_hits({"hits": [_hit(7, 1080, 1920, 5)]})
    assert video.url == "https://cdn.example/7.mp4" and video.portrait
    assert video.page_url.endswith("x-7/") and video.thumbnail.endswith("7.jpg")


def test_search_filters_sorts_and_caches(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, lambda r: httpx.Response(200, json=HITS))
    results = client.search("server   room", min_duration_s=2.0)
    assert [v.id for v in results] == [2, 1]  # too short dropped, portrait first
    params = dict(seen[0].url.params)
    assert (
        params["q"] == "server room" and params["safesearch"] == "true" and params["key"] == "KEY"
    )
    client.search("server room", min_duration_s=2.0)
    assert len(seen) == 1, "results are cached for 24 h"


def test_rate_limit_and_errors_become_unavailable(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, lambda r: httpx.Response(429, text="API rate limit exceeded"))
    with pytest.raises(StockUnavailableError, match="rate limit"):
        client.search("x", 1.0)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client, _ = _client(tmp_path, boom)
    with pytest.raises(StockUnavailableError):
        client.search("y", 1.0)


def test_download_once_into_cache(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".mp4"):
            return httpx.Response(200, content=b"video-bytes")
        if request.url.path.endswith(".jpg"):
            return httpx.Response(200, content=b"jpg")
        return httpx.Response(200, json=HITS)

    client, seen = _client(tmp_path, handler)
    video = client.search("server room", 2.0)[0]
    path = client.download_video(video)
    assert path.read_bytes() == b"video-bytes" and path == tmp_path / "videos" / "2.mp4"
    client.download_video(video)
    assert sum(r.url.path.endswith(".mp4") for r in seen) == 1
    assert client.download_thumbnail(video) == tmp_path / "thumbs" / "2.jpg"
    assert json.loads(json.dumps(client.meta(video)))["page_url"].endswith("x-2/")
