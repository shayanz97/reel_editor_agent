import pytest
from pydantic import ValidationError

from reels.config import Settings
from reels.settings_schema import ClientSettings


def test_client_settings_defaults_match_claude_md() -> None:
    s = ClientSettings()
    assert s.max_duration_s == 180
    assert s.pause_threshold_s == 0.4
    assert s.pause_target_s == 0.15
    assert s.cut_padding_s == 0.08
    assert s.audio_fade_ms == 15
    assert s.remove_fillers is True
    assert s.zoom_on_cuts is True
    assert s.zoom_factor == 1.08
    assert s.loudness_lufs == -14
    assert s.subtitle.style == "neutral"
    assert s.subtitle.karaoke is False
    assert s.subtitle.max_words == 4
    assert s.subtitle.position == "lower_third"
    assert s.broll.enabled is False
    assert s.broll.max_coverage == 0.25
    assert s.retention_days == 30
    assert s.platform == "instagram"
    assert s.tone == "du_fachlich"


def test_client_settings_json_roundtrip() -> None:
    s = ClientSettings(subtitle={"karaoke": True}, zoom_factor=1.1)  # type: ignore[arg-type]
    assert ClientSettings.model_validate(s.model_dump(mode="json")) == s


def test_partial_and_legacy_json_loads_with_defaults() -> None:
    # Old clients may lack new fields or still carry removed ones.
    s = ClientSettings.model_validate({"subtitle": {"max_words": 3}, "removed_field": 1})
    assert s.subtitle.max_words == 3
    assert s.subtitle.karaoke is False
    assert s.pause_threshold_s == 0.4


@pytest.mark.parametrize(
    "data",
    [
        {"pause_target_s": 0.5, "pause_threshold_s": 0.4},
        {"subtitle": {"max_words": 0}},
        {"broll": {"max_coverage": 1.5}},
        {"zoom_factor": 0.9},
        {"platform": "tiktok"},
    ],
)
def test_client_settings_rejects_invalid(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ClientSettings.model_validate(data)


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_CHAT_ID", "-100123")
    monkeypatch.setenv("TRANSCRIBE_ENGINE", "mlx")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.admin_chat_id == -100123
    assert s.transcribe_engine == "mlx"
    assert s.openai_api_key is not None
    assert s.openai_api_key.get_secret_value() == "sk-test"
    assert "sk-test" not in repr(s)


def test_empty_env_values_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_CHAT_ID", "")
    monkeypatch.setenv("TELEGRAM_API_ID", "")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.admin_chat_id is None
    assert s.telegram_api_id is None


def test_env_example_lists_every_setting() -> None:
    from pathlib import Path

    example = Path(__file__).parents[2] / ".env.example"
    keys = {
        line.split("=", 1)[0]
        for line in example.read_text().splitlines()
        if line and not line.startswith("#")
    }
    assert {name.upper() for name in Settings.model_fields} <= keys
