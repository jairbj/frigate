"""Tests for frigate.record.mixed (mixed primary/secondary playback)."""

import unittest

from playhouse.sqlite_ext import SqliteExtDatabase

from frigate.models import Recordings
from frigate.record.mixed import (
    build_mixed_slices,
    find_gaps,
    group_slices_into_runs,
)
from frigate.record.types import RecordStreamEnum


class _Segment:
    """Stand-in for a Recordings row, for find_gaps()."""

    def __init__(self, start_time: float, end_time: float):
        self.start_time = start_time
        self.end_time = end_time


class TestFindGaps(unittest.TestCase):
    def test_no_segments_is_one_gap(self):
        self.assertEqual(find_gaps([], 0, 100), [(0, 100)])

    def test_full_coverage_has_no_gaps(self):
        segments = [_Segment(0, 50), _Segment(50, 100)]
        self.assertEqual(find_gaps(segments, 0, 100), [])

    def test_gap_in_the_middle(self):
        segments = [_Segment(0, 20), _Segment(60, 100)]
        self.assertEqual(find_gaps(segments, 0, 100), [(20, 60)])

    def test_gaps_at_both_ends(self):
        segments = [_Segment(30, 40)]
        self.assertEqual(find_gaps(segments, 0, 100), [(0, 30), (40, 100)])

    def test_segments_reaching_outside_the_range_are_clamped(self):
        segments = [_Segment(-50, 10), _Segment(90, 200)]
        self.assertEqual(find_gaps(segments, 0, 100), [(10, 90)])

    def test_overlapping_segments_do_not_create_gaps(self):
        segments = [_Segment(0, 60), _Segment(10, 30), _Segment(55, 100)]
        self.assertEqual(find_gaps(segments, 0, 100), [])


class _RecordingsFixture:
    """A real sqlite DB holding the recordings the tests build slices from.

    Camera "cam" has primary segments only around review items, and
    secondary segments covering the whole hour, which is the configuration
    the mixed playback mode exists for.
    """

    @classmethod
    def setUpClass(cls):
        cls.db = SqliteExtDatabase(":memory:")
        Recordings.bind(cls.db)
        cls.db.create_tables([Recordings])

    def setUp(self):
        Recordings.delete().execute()

    @staticmethod
    def _row(id: str, start: float, end: float, stream: str, camera: str = "cam"):
        return dict(
            id=id,
            camera=camera,
            path=f"/rec/{id}.mp4",
            start_time=start,
            end_time=end,
            duration=end - start,
            stream=stream,
        )

    def _insert(self, rows: list[dict]) -> None:
        Recordings.insert_many(rows).execute()


class TestBuildMixedSlices(_RecordingsFixture, unittest.TestCase):
    def test_primary_only_matches_the_primary_segments(self):
        self._insert(
            [
                self._row("p1", 0, 10, "primary"),
                self._row("p2", 10, 20, "primary"),
            ]
        )

        slices = build_mixed_slices("cam", 0, 20)

        self.assertEqual([s.recording_id for s in slices], ["p1", "p2"])
        self.assertTrue(
            all(s.stream == RecordStreamEnum.primary for s in slices),
        )
        self.assertEqual([s.clip_from for s in slices], [0, 0])

    def test_gap_between_primary_segments_is_filled_with_secondary(self):
        self._insert(
            [
                self._row("p1", 0, 10, "primary"),
                self._row("p2", 40, 50, "primary"),
                self._row("s1", 0, 20, "secondary"),
                self._row("s2", 20, 40, "secondary"),
            ]
        )

        slices = build_mixed_slices("cam", 0, 50)

        self.assertEqual(
            [(s.recording_id, s.start_time, s.end_time) for s in slices],
            [
                ("p1", 0, 10),
                ("s1", 10, 20),
                ("s2", 20, 40),
                ("p2", 40, 50),
            ],
        )
        # the fill starts partway into the first secondary segment
        self.assertEqual(slices[1].clip_from, 10)
        self.assertEqual(slices[2].clip_from, 0)

    def test_slices_are_contiguous_so_player_time_tracks_wall_clock(self):
        self._insert(
            [
                self._row("p1", 15, 25, "primary"),
                self._row("s1", 0, 20, "secondary"),
                self._row("s2", 20, 40, "secondary"),
            ]
        )

        slices = build_mixed_slices("cam", 0, 40)

        self.assertEqual(slices[0].start_time, 0)
        self.assertEqual(slices[-1].end_time, 40)
        for previous, current in zip(slices, slices[1:]):
            self.assertEqual(previous.end_time, current.start_time)
        self.assertEqual(sum(s.duration for s in slices), 40)

    def test_range_without_primary_is_all_secondary(self):
        self._insert(
            [
                self._row("s1", 0, 20, "secondary"),
                self._row("s2", 20, 40, "secondary"),
            ]
        )

        slices = build_mixed_slices("cam", 5, 35)

        self.assertEqual(
            [(s.recording_id, s.start_time, s.end_time) for s in slices],
            [("s1", 5, 20), ("s2", 20, 35)],
        )
        self.assertEqual(slices[0].clip_from, 5)

    def test_no_secondary_leaves_the_gaps_alone(self):
        self._insert(
            [
                self._row("p1", 0, 10, "primary"),
                self._row("p2", 40, 50, "primary"),
            ]
        )

        slices = build_mixed_slices("cam", 0, 50)

        self.assertEqual([s.recording_id for s in slices], ["p1", "p2"])

    def test_other_cameras_are_not_mixed_in(self):
        self._insert(
            [
                self._row("p1", 0, 10, "primary"),
                self._row("other", 10, 40, "secondary", camera="other_cam"),
            ]
        )

        slices = build_mixed_slices("cam", 0, 40)

        self.assertEqual([s.recording_id for s in slices], ["p1"])

    def test_too_short_slices_are_dropped(self):
        self._insert(
            [
                self._row("p1", 0, 10, "primary"),
                self._row("p2", 10.05, 20, "primary"),
                # only 50ms of this segment falls in the gap
                self._row("s1", 0, 30, "secondary"),
            ]
        )

        slices = build_mixed_slices("cam", 0, 20)

        self.assertEqual([s.recording_id for s in slices], ["p1", "p2"])

    def test_segments_are_clipped_to_the_requested_range(self):
        self._insert(
            [
                self._row("p1", 0, 30, "primary"),
                self._row("s1", 30, 60, "secondary"),
            ]
        )

        slices = build_mixed_slices("cam", 10, 45)

        self.assertEqual(
            [(s.recording_id, s.start_time, s.end_time) for s in slices],
            [("p1", 10, 30), ("s1", 30, 45)],
        )
        self.assertEqual(slices[0].clip_from, 10)
        self.assertEqual(slices[0].duration, 20)
        self.assertEqual(slices[1].duration, 15)


class TestGroupSlicesIntoRuns(_RecordingsFixture, unittest.TestCase):
    """Runs are what the VOD playlist turns into clips."""

    def _runs(self, start: float, end: float) -> list[list[str]]:
        return [
            [s.recording_id for s in run]
            for run in group_slices_into_runs(build_mixed_slices("cam", start, end))
        ]

    def test_no_slices(self):
        self.assertEqual(group_slices_into_runs([]), [])

    def test_consecutive_files_of_one_stream_form_one_run(self):
        self._insert(
            [
                self._row("s1", 0, 10, "secondary"),
                self._row("s2", 10, 20, "secondary"),
                self._row("s3", 20, 30, "secondary"),
            ]
        )

        self.assertEqual(self._runs(0, 30), [["s1", "s2", "s3"]])

    def test_stream_change_starts_a_new_run(self):
        self._insert(
            [
                self._row("p1", 10, 20, "primary"),
                self._row("s1", 0, 10, "secondary"),
                self._row("s2", 20, 30, "secondary"),
            ]
        )

        self.assertEqual(self._runs(0, 30), [["s1"], ["p1"], ["s2"]])

    def test_hole_in_one_stream_starts_a_new_run(self):
        # nothing covers [10, 20), so the two secondary segments cannot be
        # concatenated into a single clip
        self._insert(
            [
                self._row("s1", 0, 10, "secondary"),
                self._row("s2", 20, 30, "secondary"),
            ]
        )

        self.assertEqual(self._runs(0, 30), [["s1"], ["s2"]])

    def test_trimmed_edges_stay_in_the_run(self):
        self._insert(
            [
                self._row("p1", 0, 10, "primary"),
                self._row("s1", 5, 15, "secondary"),
                self._row("s2", 15, 25, "secondary"),
            ]
        )

        runs = group_slices_into_runs(build_mixed_slices("cam", 0, 20))

        self.assertEqual(
            [[s.recording_id for s in run] for run in runs],
            [["p1"], ["s1", "s2"]],
        )
        # the clipped pieces are the ones that cannot join a concat clip
        self.assertEqual([s.is_partial for s in runs[1]], [True, True])

    def test_whole_files_are_not_partial(self):
        self._insert(
            [
                self._row("s1", 0, 10, "secondary"),
                self._row("s2", 10, 20, "secondary"),
            ]
        )

        runs = group_slices_into_runs(build_mixed_slices("cam", 0, 20))

        self.assertEqual([s.is_partial for s in runs[0]], [False, False])


if __name__ == "__main__":
    unittest.main(buffer=True)
