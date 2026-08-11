from pydantic import BaseModel
from pydantic.json_schema import SkipJsonSchema

from frigate.record.types import RecordStreamEnum


class MediaRecordingsSummaryQueryParams(BaseModel):
    timezone: str = "utc"
    cameras: str | None = "all"


class MediaRecordingsAvailabilityQueryParams(BaseModel):
    cameras: str = "all"
    before: float | SkipJsonSchema[None] = None
    after: float | SkipJsonSchema[None] = None
    scale: int = 30
    stream: RecordStreamEnum | SkipJsonSchema[None] = None


class RecordingsDeleteQueryParams(BaseModel):
    keep: str | None = None
    cameras: str | None = "all"
