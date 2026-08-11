"""Tests for per-stream retention in frigate.record.cleanup (P3)."""

import datetime
import logging
import os
import unittest
from unittest.mock import MagicMock

from peewee_migrate import Router
from playhouse.sqlite_ext import SqliteExtDatabase
from playhouse.sqliteq import SqliteQueueDatabase

from frigate.config import FrigateConfig
from frigate.models import Previews, Recordings, ReviewSegment, UserReviewStatus
from frigate.record.cleanup import RecordingCleanup
from frigate.test.const import TEST_DB, TEST_DB_CLEANUPS

CAMERA_CONFIG = {
    "ffmpeg": {
        "inputs": [
            {
                "path": "rtsp://10.0.0.1:554/main",
                "roles": ["record"],
            },
            {
                "path": "rtsp://10.0.0.1:554/sub",
                "roles": ["detect", "record_secondary"],
            },
        ]
    },
    "detect": {"height": 1080, "width": 1920, "fps": 5},
}


class TestRecordCleanup(unittest.TestCase):
    def setUp(self):
        migrate_db = SqliteExtDatabase("test.db")
        del logging.getLogger("peewee_migrate").handlers[:]
        router = Router(migrate_db)
        router.run()
        migrate_db.close()
        self.db = SqliteQueueDatabase(TEST_DB)
        self.db.bind([Recordings, ReviewSegment, Previews, UserReviewStatus])
        self.test_dir_files: list[str] = []

    def tearDown(self):
        if not self.db.is_closed():
            self.db.close()

        for f in self.test_dir_files:
            try:
                os.remove(f)
            except OSError:
                pass

        try:
            for file in TEST_DB_CLEANUPS:
                os.remove(file)
        except OSError:
            pass

    def _touch(self, path: str) -> str:
        with open(path, "w"):
            pass
        self.test_dir_files.append(path)
        return path

    def _insert_recording(
        self,
        id: str,
        camera: str,
        start: float,
        end: float,
        stream: str,
        motion: int = 0,
        dBFS: int = 0,
        objects: int = 0,
    ) -> None:
        path = self._touch(f"/tmp/{id}.mp4")
        Recordings.insert(
            id=id,
            camera=camera,
            path=path,
            start_time=start,
            end_time=end,
            duration=end - start,
            motion=motion,
            dBFS=dBFS,
            objects=objects,
            segment_size=1,
            stream=stream,
        ).execute()

    def _insert_review(
        self, id: str, camera: str, start: float, end: float, severity: str = "alert"
    ) -> None:
        ReviewSegment.insert(
            id=id,
            camera=camera,
            start_time=start,
            end_time=end,
            severity=severity,
            thumb_path=f"/tmp/{id}.thumb",
            data={"objects": [], "zones": [], "audio": []},
        ).execute()

    def _insert_preview(self, id: str, camera: str, start: float, end: float) -> None:
        path = self._touch(f"/tmp/{id}.preview.mp4")
        Previews.insert(
            id=id,
            camera=camera,
            path=path,
            start_time=start,
            end_time=end,
            duration=end - start,
        ).execute()

    def test_per_stream_retention_expiry_dates(self):
        """Secondary's own (long) retention keeps a segment primary's
        (short) retention would drop, for the same time window."""
        config = FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "record": {
                    "enabled": True,
                    "continuous": {"days": 0},
                    "motion": {"days": 0},
                    "secondary": {
                        "enabled": True,
                        "continuous": {"days": 30},
                    },
                },
                "cameras": {"cam": CAMERA_CONFIG},
            }
        )
        cleanup = RecordingCleanup(config, MagicMock())

        old = datetime.datetime.now().timestamp() - 2 * 86400  # 2 days ago
        self._insert_recording("primary-old", "cam", old, old + 10, "primary")
        self._insert_recording("secondary-old", "cam", old, old + 10, "secondary")

        cleanup.expire_recordings()

        self.assertFalse(
            Recordings.select().where(Recordings.id == "primary-old").exists()
        )
        self.assertTrue(
            Recordings.select().where(Recordings.id == "secondary-old").exists()
        )

    def test_review_bound_uses_max_across_streams(self):
        """The reviews query bound must be the most recent (max) of the
        per-stream continuous_expire_dates, not the oldest -- otherwise a
        stream with short retention (whose recent segments are immediate
        deletion candidates) gets checked against a reviews list that's
        missing recent reviews, and wrongly deletes segments that overlap
        them.
        """
        config = FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "record": {
                    "enabled": True,
                    "continuous": {"days": 0},  # bound = now (most recent)
                    "motion": {"days": 0},
                    "alerts": {"retain": {"days": 90, "mode": "all"}},
                    "secondary": {
                        "enabled": True,
                        "continuous": {"days": 60},  # bound = 60 days ago
                    },
                },
                "cameras": {"cam": CAMERA_CONFIG},
            }
        )
        cleanup = RecordingCleanup(config, MagicMock())

        recent = datetime.datetime.now().timestamp() - 3600  # 1 hour ago
        self._insert_review("recent-alert", "cam", recent, recent + 30)
        # overlaps the recent review; would be a deletion candidate under
        # primary's continuous.days=0, but must survive because it overlaps
        # a kept review (mode=all)
        self._insert_recording("primary-recent", "cam", recent, recent + 10, "primary")

        cleanup.expire_recordings()

        self.assertTrue(
            Recordings.select().where(Recordings.id == "primary-recent").exists(),
            "recording overlapping a recent review was deleted -- the "
            "reviews query bound did not reach recent enough reviews",
        )

    def test_previews_expire_once_using_combined_kept_recordings(self):
        """Previews must survive if ANY stream kept an overlapping
        recording, even if that stream isn't the first one evaluated.
        Running the previews pass per-stream (instead of once, after all
        streams' kept_recordings are known) could delete a preview during
        an earlier stream's pass that a later stream would have saved.
        """
        config = FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "record": {
                    "enabled": True,
                    "continuous": {"days": 0},
                    "motion": {"days": 0},
                    "alerts": {"retain": {"days": 90, "mode": "all"}},
                    "secondary": {
                        "enabled": True,
                        "continuous": {"days": 0},
                        "motion": {"days": 0},
                    },
                },
                "cameras": {"cam": CAMERA_CONFIG},
            }
        )
        cleanup = RecordingCleanup(config, MagicMock())

        old = datetime.datetime.now().timestamp() - 2 * 86400
        self._insert_review("kept-alert", "cam", old, old + 10)

        # primary: no overlap, no motion -> deleted, contributes nothing
        far = old - 10000
        self._insert_recording("primary-unrelated", "cam", far, far + 10, "primary")

        # secondary: overlaps the kept review -> survives, covers [old, old+10]
        self._insert_recording("secondary-overlap", "cam", old, old + 10, "secondary")

        # preview over the same window as the surviving secondary recording
        self._insert_preview("preview-1", "cam", old, old + 10)

        cleanup.expire_recordings()

        self.assertFalse(
            Recordings.select().where(Recordings.id == "primary-unrelated").exists()
        )
        self.assertTrue(
            Recordings.select().where(Recordings.id == "secondary-overlap").exists()
        )
        self.assertTrue(
            Previews.select().where(Previews.id == "preview-1").exists(),
            "preview was deleted even though a secondary-stream recording "
            "covering its time range was kept",
        )


if __name__ == "__main__":
    unittest.main()
