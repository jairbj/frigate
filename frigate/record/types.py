"""Types shared by the recording pipeline."""

from enum import Enum


class RecordStreamEnum(str, Enum):
    primary = "primary"
    secondary = "secondary"


# role name (str) -> stream. Kept as plain strings to avoid importing config.
ROLE_TO_STREAM: dict[str, RecordStreamEnum] = {
    "record": RecordStreamEnum.primary,
    "record_secondary": RecordStreamEnum.secondary,
}


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
