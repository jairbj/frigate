from enum import Enum

from pydantic import Field

from frigate.const import MAX_PRE_CAPTURE
from frigate.record.types import RecordStreamEnum
from frigate.review.types import SeverityEnum

from ..base import FrigateBaseModel

__all__ = [
    "ChaptersEnum",
    "RecordConfig",
    "RecordExportConfig",
    "RecordPreviewConfig",
    "RecordQualityEnum",
    "RecordSecondaryConfig",
    "EventsConfig",
    "ReviewRetainConfig",
    "RecordRetainConfig",
    "RetainModeEnum",
]


class RecordRetainConfig(FrigateBaseModel):
    days: float = Field(
        default=0,
        ge=0,
        title="Retention days",
        description="Days to retain recordings.",
    )


class RetainModeEnum(str, Enum):
    all = "all"
    motion = "motion"
    active_objects = "active_objects"


class ReviewRetainConfig(FrigateBaseModel):
    days: float = Field(
        default=10,
        ge=0,
        title="Retention days",
        description="Number of days to retain recordings of detection events.",
    )
    mode: RetainModeEnum = Field(
        default=RetainModeEnum.motion,
        title="Retention mode",
        description="Mode for retention: all (save all segments), motion (save segments with motion), or active_objects (save segments with active objects).",
    )


class EventsConfig(FrigateBaseModel):
    pre_capture: int = Field(
        default=5,
        title="Pre-capture seconds",
        description="Number of seconds before the detection event to include in the recording.",
        le=MAX_PRE_CAPTURE,
        ge=0,
    )
    post_capture: int = Field(
        default=5,
        ge=0,
        title="Post-capture seconds",
        description="Number of seconds after the detection event to include in the recording.",
    )
    retain: ReviewRetainConfig = Field(
        default_factory=ReviewRetainConfig,
        title="Event retention",
        description="Retention settings for recordings of detection events.",
    )


class RecordQualityEnum(str, Enum):
    very_low = "very_low"
    low = "low"
    medium = "medium"
    high = "high"
    very_high = "very_high"


class RecordPreviewConfig(FrigateBaseModel):
    quality: RecordQualityEnum = Field(
        default=RecordQualityEnum.medium,
        title="Preview quality",
        description="Preview quality level (very_low, low, medium, high, very_high).",
    )


class ChaptersEnum(str, Enum):
    none = "none"
    recording_segments = "recording_segments"
    review_items = "review_items"


class RecordExportConfig(FrigateBaseModel):
    hwaccel_args: str | list[str] = Field(
        default="auto",
        title="Export hwaccel args",
        description="Hardware acceleration args to use for export/transcode operations.",
    )
    max_concurrent: int = Field(
        default=3,
        ge=1,
        title="Maximum concurrent exports",
        description="Maximum number of export jobs to process at the same time.",
    )
    chapters: ChaptersEnum = Field(
        default=ChaptersEnum.review_items,
        title="Chapter metadata to embed in exported recordings",
    )


class RecordSecondaryConfig(FrigateBaseModel):
    enabled: bool = Field(
        default=False,
        title="Enable secondary recording",
        description="Enable a second, continuous recording stream (e.g. a low-resolution substream) alongside the primary record stream.",
    )
    continuous: RecordRetainConfig = Field(
        default_factory=RecordRetainConfig,
        title="Secondary continuous retention",
        description="Number of days to retain the secondary stream's recordings regardless of tracked objects or motion.",
    )
    motion: RecordRetainConfig = Field(
        default_factory=RecordRetainConfig,
        title="Secondary motion retention",
        description="Number of days to retain the secondary stream's recordings triggered by motion regardless of tracked objects.",
    )
    enabled_in_config: bool | None = Field(
        default=None,
        title="Original secondary recording state",
        description="Indicates whether secondary recording was enabled in the original static configuration.",
    )


class RecordConfig(FrigateBaseModel):
    enabled: bool = Field(
        default=False,
        title="Enable recording",
        description="Enable or disable recording for all cameras; can be overridden per-camera.",
    )
    expire_interval: int = Field(
        default=60,
        title="Record cleanup interval",
        description="Minutes between cleanup passes that remove expired recording segments.",
    )
    continuous: RecordRetainConfig = Field(
        default_factory=RecordRetainConfig,
        title="Continuous retention",
        description="Number of days to retain recordings regardless of tracked objects or motion. Set to 0 if you only want to retain recordings of alerts and detections.",
    )
    motion: RecordRetainConfig = Field(
        default_factory=RecordRetainConfig,
        title="Motion retention",
        description="Number of days to retain recordings triggered by motion regardless of tracked objects. Set to 0 if you only want to retain recordings of alerts and detections.",
    )
    detections: EventsConfig = Field(
        default_factory=EventsConfig,
        title="Detection retention",
        description="Recording retention settings for detection events including pre/post capture durations.",
    )
    alerts: EventsConfig = Field(
        default_factory=EventsConfig,
        title="Alert retention",
        description="Recording retention settings for alert events including pre/post capture durations.",
    )
    export: RecordExportConfig = Field(
        default_factory=RecordExportConfig,
        title="Export config",
        description="Settings used when exporting recordings such as timelapse and hardware acceleration.",
    )
    preview: RecordPreviewConfig = Field(
        default_factory=RecordPreviewConfig,
        title="Preview config",
        description="Settings controlling the quality of recording previews shown in the UI.",
    )
    secondary: RecordSecondaryConfig = Field(
        default_factory=RecordSecondaryConfig,
        title="Secondary recording stream",
        description="Settings for a second, continuous recording stream (e.g. low-resolution 24x7 coverage).",
    )
    enabled_in_config: bool | None = Field(
        default=None,
        title="Original recording state",
        description="Indicates whether recording was enabled in the original static configuration.",
    )

    @property
    def event_pre_capture(self) -> int:
        return max(
            self.alerts.pre_capture,
            self.detections.pre_capture,
        )

    def get_review_pre_capture(self, severity: SeverityEnum) -> int:
        if severity == SeverityEnum.alert:
            return self.alerts.pre_capture
        else:
            return self.detections.pre_capture

    def get_review_post_capture(self, severity: SeverityEnum) -> int:
        if severity == SeverityEnum.alert:
            return self.alerts.post_capture
        else:
            return self.detections.post_capture

    def get_retention(
        self, stream: RecordStreamEnum
    ) -> tuple[RecordRetainConfig, RecordRetainConfig]:
        """Return (continuous, motion) retention config for the given stream."""
        if stream == RecordStreamEnum.secondary:
            return self.secondary.continuous, self.secondary.motion
        return self.continuous, self.motion

    def stream_enabled(self, stream: RecordStreamEnum) -> bool:
        """Whether the given stream should currently be recorded."""
        if stream == RecordStreamEnum.secondary:
            return self.enabled and self.secondary.enabled
        return self.enabled

    def enabled_streams(self) -> list[RecordStreamEnum]:
        """All streams that should currently be recorded."""
        return [s for s in RecordStreamEnum if self.stream_enabled(s)]

    def timeline_stream(self) -> RecordStreamEnum:
        """The stream with the broadest continuous coverage; drives timeline visuals."""
        if (
            self.secondary.enabled
            and self.secondary.continuous.days >= self.continuous.days
        ):
            return RecordStreamEnum.secondary
        return RecordStreamEnum.primary
