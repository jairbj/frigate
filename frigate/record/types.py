"""Types shared by the recording pipeline."""

from enum import Enum


class RecordStreamEnum(str, Enum):
    primary = "primary"
    secondary = "secondary"


class PlaybackStreamEnum(str, Enum):
    """Stream selection for playback APIs.

    Kept separate from RecordStreamEnum because "mixed" is not a stored
    value: it means "primary, with the gaps filled from secondary".
    """

    primary = "primary"
    secondary = "secondary"
    mixed = "mixed"

    def as_record_stream(self) -> RecordStreamEnum | None:
        """The stored stream this maps to, or None for mixed."""
        if self == PlaybackStreamEnum.mixed:
            return None

        return RecordStreamEnum(self.value)


# role name (str) -> stream. Kept as plain strings to avoid importing config.
ROLE_TO_STREAM: dict[str, RecordStreamEnum] = {
    "record": RecordStreamEnum.primary,
    "record_secondary": RecordStreamEnum.secondary,
}

# stream -> role name, e.g. for building "{camera}/status/{role}" MQTT topics.
STREAM_TO_ROLE: dict[RecordStreamEnum, str] = {v: k for k, v in ROLE_TO_STREAM.items()}


def cache_segment_prefix(camera: str, stream: RecordStreamEnum) -> str:
    """Cache filename prefix for a camera/stream (primary keeps the legacy name)."""
    return camera if stream == RecordStreamEnum.primary else f"{camera}#{stream.value}"


def parse_cache_filename(basename: str) -> tuple[str, RecordStreamEnum] | None:
    """Parse a cache basename (no extension) into (camera, stream), or None."""
    try:
        left, _date = basename.rsplit("@", maxsplit=1)
    except ValueError:
        return None

    if "#" in left:
        camera, raw = left.split("#", 1)
        try:
            return camera, RecordStreamEnum(raw)
        except ValueError:
            return None

    return left, RecordStreamEnum.primary
