"""Shared query predicates for the Recordings table."""

import operator
from functools import reduce

from peewee import Expression

from frigate.models import Recordings
from frigate.record.types import RecordStreamEnum


def overlaps(start_ts: float, end_ts: float) -> Expression:
    """Segments overlapping [start_ts, end_ts]."""
    return (Recordings.end_time >= start_ts) & (Recordings.start_time <= end_ts)


def for_stream(stream: RecordStreamEnum | None) -> Expression | None:
    """Stream predicate, or None for all streams."""
    return None if stream is None else (Recordings.stream == stream.value)


def camera_range(
    camera: str,
    start_ts: float,
    end_ts: float,
    stream: RecordStreamEnum | None,
) -> Expression:
    """Recordings for a camera overlapping [start_ts, end_ts].

    stream has no default: every call site must state whether it wants a
    single stream or all streams (None). See plans/dual-stream-recording.md
    section 6.1 for why an implicit default is unsafe here.
    """
    clauses = [Recordings.camera == camera, overlaps(start_ts, end_ts)]
    stream_clause = for_stream(stream)
    if stream_clause is not None:
        clauses.append(stream_clause)
    return reduce(operator.and_, clauses)


def camera_at_time(
    camera: str,
    frame_time: float,
    stream: RecordStreamEnum | None,
) -> Expression:
    """The recording for a camera containing frame_time.

    stream has no default for the same reason as camera_range.
    """
    clauses = [
        Recordings.camera == camera,
        Recordings.start_time <= frame_time,
        Recordings.end_time >= frame_time,
    ]
    stream_clause = for_stream(stream)
    if stream_clause is not None:
        clauses.append(stream_clause)
    return reduce(operator.and_, clauses)
