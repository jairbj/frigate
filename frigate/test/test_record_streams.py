"""Tests for frigate.record.types and frigate.record.queries (P0 foundation).

These cover the cache filename scheme and the shared query helpers that
back the dual-stream recording feature. See plans/dual-stream-recording.md.
"""

import unittest

from playhouse.sqlite_ext import SqliteExtDatabase

from frigate.models import Recordings
from frigate.record.queries import camera_at_time, camera_range, for_stream, overlaps
from frigate.record.types import (
    ROLE_TO_STREAM,
    RecordStreamEnum,
    cache_segment_prefix,
    parse_cache_filename,
)


class TestParseCacheFilename(unittest.TestCase):
    def test_legacy_primary_filename(self):
        self.assertEqual(
            parse_cache_filename("front_door@20260810120000+0000"),
            ("front_door", RecordStreamEnum.primary),
        )

    def test_secondary_filename(self):
        self.assertEqual(
            parse_cache_filename("front_door#secondary@20260810120000+0000"),
            ("front_door", RecordStreamEnum.secondary),
        )

    def test_camera_name_with_hyphen_underscore_digit(self):
        self.assertEqual(
            parse_cache_filename("front-door_2#secondary@20260810120000+0000"),
            ("front-door_2", RecordStreamEnum.secondary),
        )

    def test_unknown_stream_token_returns_none(self):
        self.assertIsNone(parse_cache_filename("front_door#bogus@20260810120000+0000"))

    def test_missing_at_separator_returns_none(self):
        self.assertIsNone(parse_cache_filename("garbage"))

    def test_preview_prefixed_name_has_no_special_casing(self):
        # move_files() filters preview_ files out before calling this parser;
        # the parser itself just treats it as a normal camera name.
        self.assertEqual(
            parse_cache_filename("preview_front_door@20260810120000+0000"),
            ("preview_front_door", RecordStreamEnum.primary),
        )


class TestCacheSegmentPrefix(unittest.TestCase):
    def test_primary_keeps_legacy_name(self):
        self.assertEqual(
            cache_segment_prefix("front_door", RecordStreamEnum.primary),
            "front_door",
        )

    def test_secondary_adds_suffix(self):
        self.assertEqual(
            cache_segment_prefix("front_door", RecordStreamEnum.secondary),
            "front_door#secondary",
        )

    def test_round_trips_through_parse_cache_filename(self):
        for stream in RecordStreamEnum:
            prefix = cache_segment_prefix("front_door", stream)
            basename = f"{prefix}@20260810120000+0000"
            self.assertEqual(parse_cache_filename(basename), ("front_door", stream))


class TestRoleToStream(unittest.TestCase):
    def test_known_roles(self):
        self.assertEqual(ROLE_TO_STREAM["record"], RecordStreamEnum.primary)
        self.assertEqual(ROLE_TO_STREAM["record_secondary"], RecordStreamEnum.secondary)

    def test_detect_and_audio_are_not_record_streams(self):
        self.assertNotIn("detect", ROLE_TO_STREAM)
        self.assertNotIn("audio", ROLE_TO_STREAM)


class TestQueryHelpers(unittest.TestCase):
    """Verify camera_range()/overlaps() against a real sqlite DB.

    In particular, proves overlaps() is equivalent to the 3-clause
    `between | between | contains` predicate it replaces across ~10 call
    sites, using the same segment shapes seen in production: segments that
    start before the range, end after it, are fully contained, or don't
    overlap at all.
    """

    @classmethod
    def setUpClass(cls):
        cls.db = SqliteExtDatabase(":memory:")
        Recordings.bind(cls.db)
        cls.db.create_tables([Recordings])

        # camera "a": segments at [0,10), [10,20), [20,30), [100,110)
        # camera "b": segment at [5,15) with stream="secondary"
        rows = [
            dict(
                id="a1",
                camera="a",
                path="/rec/a1.mp4",
                start_time=0,
                end_time=10,
                duration=10,
                stream="primary",
            ),
            dict(
                id="a2",
                camera="a",
                path="/rec/a2.mp4",
                start_time=10,
                end_time=20,
                duration=10,
                stream="primary",
            ),
            dict(
                id="a3",
                camera="a",
                path="/rec/a3.mp4",
                start_time=20,
                end_time=30,
                duration=10,
                stream="primary",
            ),
            dict(
                id="a4-far",
                camera="a",
                path="/rec/a4.mp4",
                start_time=100,
                end_time=110,
                duration=10,
                stream="primary",
            ),
            dict(
                id="b1-secondary",
                camera="b",
                path="/rec/b1.mp4",
                start_time=5,
                end_time=15,
                duration=10,
                stream="secondary",
            ),
        ]
        Recordings.insert_many(rows).execute()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def _legacy_predicate(self, start_ts: float, end_ts: float):
        """The 3-clause OR predicate camera_range()/overlaps() replaced."""
        return (
            Recordings.start_time.between(start_ts, end_ts)
            | Recordings.end_time.between(start_ts, end_ts)
            | ((start_ts > Recordings.start_time) & (end_ts < Recordings.end_time))
        )

    def _ids(self, query) -> set:
        return {r.id for r in query}

    def test_overlaps_matches_legacy_predicate(self):
        # a broad matrix of ranges relative to camera "a"'s segments
        cases = [
            (0, 10),  # exact match on a1
            (5, 15),  # spans a1/a2 boundary
            (-5, 5),  # starts before a1
            (25, 200),  # spans a3 and the far segment, open-ended in effect
            (12, 18),  # fully inside a2
            (40, 60),  # no overlap with anything
            (0, 1000),  # everything
        ]
        for start_ts, end_ts in cases:
            with self.subTest(start_ts=start_ts, end_ts=end_ts):
                legacy = self._ids(
                    Recordings.select().where(
                        (Recordings.camera == "a")
                        & self._legacy_predicate(start_ts, end_ts)
                    )
                )
                new = self._ids(
                    Recordings.select().where(
                        camera_range("a", start_ts, end_ts, RecordStreamEnum.primary)
                    )
                )
                self.assertEqual(new, legacy)

    def test_camera_range_filters_by_camera(self):
        ids = self._ids(Recordings.select().where(camera_range("a", 0, 1000, None)))
        self.assertNotIn("b1-secondary", ids)

    def test_camera_range_stream_none_returns_all_streams(self):
        ids = self._ids(Recordings.select().where(camera_range("b", 0, 20, None)))
        self.assertIn("b1-secondary", ids)

    def test_camera_range_stream_primary_excludes_secondary(self):
        ids = self._ids(
            Recordings.select().where(
                camera_range("b", 0, 20, RecordStreamEnum.primary)
            )
        )
        self.assertNotIn("b1-secondary", ids)

    def test_camera_range_stream_secondary_only_matches_secondary(self):
        ids = self._ids(
            Recordings.select().where(
                camera_range("b", 0, 20, RecordStreamEnum.secondary)
            )
        )
        self.assertEqual(ids, {"b1-secondary"})

    def test_for_stream_none_is_no_filter(self):
        self.assertIsNone(for_stream(None))

    def test_for_stream_explicit(self):
        clause = for_stream(RecordStreamEnum.secondary)
        self.assertIsNotNone(clause)

    def test_overlaps_excludes_disjoint_segment(self):
        ids = self._ids(
            Recordings.select().where((Recordings.camera == "a") & overlaps(40, 60))
        )
        self.assertEqual(ids, set())

    def test_camera_at_time_point_inside_segment(self):
        ids = self._ids(
            Recordings.select().where(camera_at_time("a", 15, RecordStreamEnum.primary))
        )
        self.assertEqual(ids, {"a2"})

    def test_camera_at_time_point_outside_any_segment(self):
        ids = self._ids(
            Recordings.select().where(camera_at_time("a", 50, RecordStreamEnum.primary))
        )
        self.assertEqual(ids, set())

    def test_camera_at_time_respects_stream(self):
        # t=10 falls inside b's secondary segment [5,15)
        ids = self._ids(
            Recordings.select().where(camera_at_time("b", 10, RecordStreamEnum.primary))
        )
        self.assertEqual(ids, set())

        ids = self._ids(
            Recordings.select().where(
                camera_at_time("b", 10, RecordStreamEnum.secondary)
            )
        )
        self.assertEqual(ids, {"b1-secondary"})


if __name__ == "__main__":
    unittest.main()
