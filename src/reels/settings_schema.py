"""Per-client settings, stored as JSON in `clients.settings`.

Every field has a default so that clients created before a field existed keep working.
Unknown keys are ignored for the same reason (a removed setting must not break loading).
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SubtitlePosition = Literal["lower_third", "center", "upper_third"]


class SubtitleSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # "neutral": white text, black outline, one accent colour for highlights
    style: str = "neutral"
    karaoke: bool = False
    max_words: int = Field(default=4, ge=1)
    # lower_third sits above the Instagram UI controls
    position: SubtitlePosition = "lower_third"


class BrollSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    max_coverage: float = Field(default=0.25, ge=0.0, le=1.0)


class ClientSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_duration_s: float = Field(default=180.0, gt=0)
    pause_threshold_s: float = Field(default=0.4, ge=0)
    pause_target_s: float = Field(default=0.15, ge=0)
    cut_padding_s: float = Field(default=0.08, ge=0)
    audio_fade_ms: int = Field(default=15, ge=0)
    # Fillers are only removed if the transcript actually contains them.
    remove_fillers: bool = True
    zoom_on_cuts: bool = True
    zoom_factor: float = Field(default=1.08, ge=1.0)
    loudness_lufs: float = -14.0
    subtitle: SubtitleSettings = Field(default_factory=SubtitleSettings)
    broll: BrollSettings = Field(default_factory=BrollSettings)
    retention_days: int = Field(default=30, ge=1)
    platform: Literal["instagram"] = "instagram"
    tone: str = "du_fachlich"

    @model_validator(mode="after")
    def _pause_target_not_above_threshold(self) -> Self:
        if self.pause_target_s > self.pause_threshold_s:
            raise ValueError("pause_target_s must not exceed pause_threshold_s")
        return self
