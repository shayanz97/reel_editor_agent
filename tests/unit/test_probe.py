import pytest

from reels.media.probe import VideoRejectedError, check_processable, parse_probe


def _data(width: int = 1920, height: int = 1080, **video: object) -> dict[str, object]:
    return {
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": width, "height": height,
             "avg_frame_rate": "30000/1001", **video},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "12.5"},
    }  # fmt: skip


def test_parse_basic_landscape() -> None:
    r = parse_probe(_data())
    assert (r.width, r.height, r.orientation) == (1920, 1080, "landscape")
    assert r.fps == pytest.approx(29.97, abs=0.01)
    assert r.frame_rate == "30000/1001"
    assert r.duration == 12.5
    assert r.has_audio and r.audio_codec == "aac"


def test_rotation_from_display_matrix_swaps_dimensions() -> None:
    r = parse_probe(_data(side_data_list=[{"side_data_type": "Display Matrix", "rotation": -90}]))
    assert r.rotation == 270
    assert (r.width, r.height, r.orientation) == (1080, 1920, "portrait")


def test_rotation_from_legacy_tag() -> None:
    r = parse_probe(_data(tags={"rotate": "180"}))
    assert r.rotation == 180
    assert r.orientation == "landscape"


def test_frame_rate_fallback() -> None:
    r = parse_probe(_data(avg_frame_rate="0/0", r_frame_rate="25/1"))
    assert r.fps == 25


def test_no_video_is_rejected() -> None:
    with pytest.raises(VideoRejectedError):
        parse_probe({"streams": [{"codec_type": "audio"}], "format": {"duration": "1"}})


def test_check_processable() -> None:
    r = parse_probe(_data())
    check_processable(r, max_duration_s=180)
    with pytest.raises(VideoRejectedError, match="limit"):
        check_processable(r, max_duration_s=10)
    no_audio = r.model_copy(update={"has_audio": False})
    with pytest.raises(VideoRejectedError, match="audio"):
        check_processable(no_audio, max_duration_s=180)
