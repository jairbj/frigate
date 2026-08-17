"""Merging of the primary and secondary recording streams for playback.

With the "Blue Iris style" configuration the primary (high resolution)
stream is only recorded around review items while the secondary (low
resolution) stream runs 24x7. Playing the primary stream on its own then
produces a playlist that jumps from one review item to the next, because
the VOD playlist is a plain concatenation of the segments that exist.

`build_mixed_slices` fills those holes with the secondary stream so the
playhead advances at wall-clock speed: high resolution where it was
recorded, low resolution everywhere else.
"""

from dataclasses import dataclass

from frigate.models import Recordings
from frigate.record.queries import camera_range
from frigate.record.types import RecordStreamEnum

# minimum slice length, matching the VOD builder: anything shorter cannot be
# guaranteed to contain a decodable frame
MIN_SLICE_DURATION = 0.1


@dataclass
class MixedSlice:
    """A contiguous piece of a recording file used to build mixed playback."""

    recording_id: str
    path: str
    stream: RecordStreamEnum
    # wall-clock bounds of the slice
    start_time: float
    end_time: float
    # bounds of the file the slice was taken from
    segment_start_time: float
    segment_duration: float
    motion: int | None = None
    objects: int | None = None
    motion_heatmap: list[int] | None = None
    segment_size: float = 0

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time

    @property
    def clip_from(self) -> float:
        """Offset into the file where the slice starts, in seconds."""
        return max(0.0, self.start_time - self.segment_start_time)

    @property
    def is_partial(self) -> bool:
        return self.start_time > self.segment_start_time or (
            self.end_time < self.segment_start_time + self.segment_duration
        )


def _query_segments(
    camera: str, start_ts: float, end_ts: float, stream: RecordStreamEnum
) -> list[Recordings]:
    return list(
        Recordings.select(
            Recordings.id,
            Recordings.path,
            Recordings.start_time,
            Recordings.end_time,
            Recordings.duration,
            Recordings.motion,
            Recordings.objects,
            Recordings.motion_heatmap,
            Recordings.segment_size,
            Recordings.stream,
        )
        .where(camera_range(camera, start_ts, end_ts, stream))
        .order_by(Recordings.start_time.asc())
        .iterator()
    )


def _to_slice(recording: Recordings, start_time: float, end_time: float) -> MixedSlice:
    return MixedSlice(
        recording_id=recording.id,
        path=recording.path,
        stream=RecordStreamEnum(recording.stream),
        start_time=start_time,
        end_time=end_time,
        segment_start_time=recording.start_time,
        segment_duration=recording.duration,
        motion=recording.motion,
        objects=recording.objects,
        motion_heatmap=recording.motion_heatmap,
        segment_size=recording.segment_size,
    )


def find_gaps(
    segments: list[Recordings], start_ts: float, end_ts: float
) -> list[tuple[float, float]]:
    """Ranges within [start_ts, end_ts] not covered by the given segments.

    Segments must be sorted by start_time. Overlapping segments are handled
    by tracking the furthest end time seen so far.
    """
    gaps: list[tuple[float, float]] = []
    cursor = start_ts

    for segment in segments:
        if segment.start_time > cursor:
            gaps.append((cursor, min(segment.start_time, end_ts)))

        cursor = max(cursor, segment.end_time)

        if cursor >= end_ts:
            break

    if cursor < end_ts:
        gaps.append((cursor, end_ts))

    return [(gap_start, gap_end) for gap_start, gap_end in gaps if gap_end > gap_start]


def build_mixed_slices(camera: str, start_ts: float, end_ts: float) -> list[MixedSlice]:
    """Build a continuous list of slices for [start_ts, end_ts].

    Primary segments are used whole, and every stretch they do not cover is
    filled with the parts of the secondary segments that overlap it. The
    result is ordered by start time and never overlaps itself, so summing
    slice durations maps player time to wall-clock time.

    Args:
        camera: Camera name
        start_ts: Start of the requested range, as a unix timestamp
        end_ts: End of the requested range, as a unix timestamp

    Returns:
        The slices to play, in order
    """
    primary = _query_segments(camera, start_ts, end_ts, RecordStreamEnum.primary)
    gaps = find_gaps(primary, start_ts, end_ts)

    slices = [
        _to_slice(
            recording,
            max(recording.start_time, start_ts),
            min(recording.end_time, end_ts),
        )
        for recording in primary
    ]

    if gaps:
        secondary = _query_segments(
            camera, gaps[0][0], gaps[-1][1], RecordStreamEnum.secondary
        )

        for gap_start, gap_end in gaps:
            for recording in secondary:
                if recording.start_time >= gap_end:
                    break

                if recording.end_time <= gap_start:
                    continue

                slices.append(
                    _to_slice(
                        recording,
                        max(recording.start_time, gap_start),
                        min(recording.end_time, gap_end),
                    )
                )

    slices.sort(key=lambda s: s.start_time)

    return [s for s in slices if s.duration >= MIN_SLICE_DURATION]
