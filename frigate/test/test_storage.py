import datetime
import logging
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from peewee import DoesNotExist
from peewee_migrate import Router
from playhouse.sqlite_ext import SqliteExtDatabase
from playhouse.sqliteq import SqliteQueueDatabase

from frigate.config import FrigateConfig
from frigate.models import Event, Recordings
from frigate.record.types import RecordStreamEnum
from frigate.storage import StorageMaintainer
from frigate.test.const import TEST_DB, TEST_DB_CLEANUPS


class TestHttp(unittest.TestCase):
    def setUp(self):
        # setup clean database for each test run
        migrate_db = SqliteExtDatabase("test.db")
        del logging.getLogger("peewee_migrate").handlers[:]
        router = Router(migrate_db)
        router.run()
        migrate_db.close()
        self.db = SqliteQueueDatabase(TEST_DB)
        models = [Event, Recordings]
        self.db.bind(models)
        self.test_dir = tempfile.mkdtemp()

        self.minimal_config = {
            "mqtt": {"host": "mqtt"},
            "cameras": {
                "front_door": {
                    "ffmpeg": {
                        "inputs": [
                            {"path": "rtsp://10.0.0.1:554/video", "roles": ["detect"]}
                        ]
                    },
                    "detect": {
                        "height": 1080,
                        "width": 1920,
                        "fps": 5,
                    },
                }
            },
        }
        self.double_cam_config = {
            "mqtt": {"host": "mqtt"},
            "cameras": {
                "front_door": {
                    "ffmpeg": {
                        "inputs": [
                            {"path": "rtsp://10.0.0.1:554/video", "roles": ["detect"]}
                        ]
                    },
                    "detect": {
                        "height": 1080,
                        "width": 1920,
                        "fps": 5,
                    },
                },
                "back_door": {
                    "ffmpeg": {
                        "inputs": [
                            {"path": "rtsp://10.0.0.2:554/video", "roles": ["detect"]}
                        ]
                    },
                    "detect": {
                        "height": 1080,
                        "width": 1920,
                        "fps": 5,
                    },
                },
            },
        }

    def tearDown(self):
        if not self.db.is_closed():
            self.db.close()

        try:
            for file in TEST_DB_CLEANUPS:
                os.remove(file)
        except OSError:
            pass

    def test_segment_calculations(self):
        """Test that the segment calculations are correct."""
        config = FrigateConfig(**self.double_cam_config)
        storage = StorageMaintainer(config, MagicMock())

        time_keep = datetime.datetime.now().timestamp()
        rec_fd_id = "1234567.frontdoor"
        rec_bd_id = "1234568.backdoor"
        _insert_mock_recording(
            rec_fd_id,
            os.path.join(self.test_dir, f"{rec_fd_id}.tmp"),
            time_keep,
            time_keep + 10,
            camera="front_door",
            seg_size=4,
            seg_dur=10,
        )
        _insert_mock_recording(
            rec_bd_id,
            os.path.join(self.test_dir, f"{rec_bd_id}.tmp"),
            time_keep + 10,
            time_keep + 20,
            camera="back_door",
            seg_size=8,
            seg_dur=20,
        )
        storage.calculate_camera_bandwidth()
        assert storage.camera_storage_stats == {
            "front_door": {"bandwidth": 1440, "needs_refresh": True},
            "back_door": {"bandwidth": 2880, "needs_refresh": True},
        }

    def test_segment_calculations_with_zero_segments(self):
        """Ensure segment calculation does not fail when migrating from previous version."""
        config = FrigateConfig(**self.minimal_config)
        storage = StorageMaintainer(config, MagicMock())

        time_keep = datetime.datetime.now().timestamp()
        rec_fd_id = "1234567.frontdoor"
        _insert_mock_recording(
            rec_fd_id,
            os.path.join(self.test_dir, f"{rec_fd_id}.tmp"),
            time_keep,
            time_keep + 10,
            camera="front_door",
            seg_size=0,
            seg_dur=10,
        )
        storage.calculate_camera_bandwidth()
        assert storage.camera_storage_stats == {
            "front_door": {"bandwidth": 0, "needs_refresh": True},
        }

    def test_storage_cleanup(self):
        """Ensure that all recordings are cleaned up when necessary."""
        config = FrigateConfig(**self.minimal_config)
        storage = StorageMaintainer(config, MagicMock())

        id = "123456.keep"
        time_keep = datetime.datetime.now().timestamp()
        _insert_mock_event(
            id,
            time_keep,
            time_keep + 30,
            True,
        )
        rec_k_id = "1234567.keep"
        rec_k2_id = "1234568.keep"
        rec_k3_id = "1234569.keep"
        _insert_mock_recording(
            rec_k_id,
            os.path.join(self.test_dir, f"{rec_k_id}.tmp"),
            time_keep,
            time_keep + 10,
        )
        _insert_mock_recording(
            rec_k2_id,
            os.path.join(self.test_dir, f"{rec_k2_id}.tmp"),
            time_keep + 10,
            time_keep + 20,
        )
        _insert_mock_recording(
            rec_k3_id,
            os.path.join(self.test_dir, f"{rec_k3_id}.tmp"),
            time_keep + 20,
            time_keep + 30,
        )

        id2 = "7890.delete"
        time_delete = datetime.datetime.now().timestamp() - 360
        _insert_mock_event(id2, time_delete, time_delete + 30, False)
        rec_d_id = "78901.delete"
        rec_d2_id = "78902.delete"
        rec_d3_id = "78903.delete"
        _insert_mock_recording(
            rec_d_id,
            os.path.join(self.test_dir, f"{rec_d_id}.tmp"),
            time_delete,
            time_delete + 10,
        )
        _insert_mock_recording(
            rec_d2_id,
            os.path.join(self.test_dir, f"{rec_d2_id}.tmp"),
            time_delete + 10,
            time_delete + 20,
        )
        _insert_mock_recording(
            rec_d3_id,
            os.path.join(self.test_dir, f"{rec_d3_id}.tmp"),
            time_delete + 20,
            time_delete + 30,
        )

        storage.calculate_camera_bandwidth()
        storage.reduce_storage_consumption()
        with self.assertRaises(DoesNotExist):
            assert Recordings.get(Recordings.id == rec_k_id)
            assert Recordings.get(Recordings.id == rec_k2_id)
            assert Recordings.get(Recordings.id == rec_k3_id)
            Recordings.get(Recordings.id == rec_d_id)
            Recordings.get(Recordings.id == rec_d2_id)
            Recordings.get(Recordings.id == rec_d3_id)

    def test_storage_cleanup_keeps_retained(self):
        """Ensure that all recordings are cleaned up when necessary."""
        config = FrigateConfig(**self.minimal_config)
        storage = StorageMaintainer(config, MagicMock())

        id = "123456.keep"
        time_keep = datetime.datetime.now().timestamp()
        _insert_mock_event(
            id,
            time_keep,
            time_keep + 30,
            True,
        )
        rec_k_id = "1234567.keep"
        rec_k2_id = "1234568.keep"
        rec_k3_id = "1234569.keep"
        _insert_mock_recording(
            rec_k_id,
            os.path.join(self.test_dir, f"{rec_k_id}.tmp"),
            time_keep,
            time_keep + 10,
        )
        _insert_mock_recording(
            rec_k2_id,
            os.path.join(self.test_dir, f"{rec_k2_id}.tmp"),
            time_keep + 10,
            time_keep + 20,
        )
        _insert_mock_recording(
            rec_k3_id,
            os.path.join(self.test_dir, f"{rec_k3_id}.tmp"),
            time_keep + 20,
            time_keep + 30,
        )

        time_delete = datetime.datetime.now().timestamp() - 7200
        for i in range(0, 59):
            id = f"{123456 + i}.delete"
            _insert_mock_recording(
                id,
                os.path.join(self.test_dir, f"{id}.tmp"),
                time_delete,
                time_delete + 600,
            )

        storage.calculate_camera_bandwidth()
        storage.reduce_storage_consumption()
        assert Recordings.get(Recordings.id == rec_k_id)
        assert Recordings.get(Recordings.id == rec_k2_id)
        assert Recordings.get(Recordings.id == rec_k3_id)

    def test_reduce_storage_consumption_deletes_primary_before_secondary(self):
        """High-res (primary) must be fully drained before secondary is
        touched, since the 24x7 low-res timeline is the asset dual-stream
        recording is meant to protect."""
        config = FrigateConfig(**self.minimal_config)
        storage = StorageMaintainer(config, MagicMock())

        time_old = datetime.datetime.now().timestamp() - 3600

        primary_ids = []
        for i in range(3):
            pid = f"{1000 + i}.primary"
            primary_ids.append(pid)
            _insert_mock_recording(
                pid,
                os.path.join(self.test_dir, f"{pid}.tmp"),
                time_old + i * 10,
                time_old + i * 10 + 10,
                seg_size=10,
                stream=RecordStreamEnum.primary.value,
            )

        secondary_ids = []
        for i in range(3):
            sid = f"{2000 + i}.secondary"
            secondary_ids.append(sid)
            _insert_mock_recording(
                sid,
                os.path.join(self.test_dir, f"{sid}.tmp"),
                time_old + i * 10,
                time_old + i * 10 + 10,
                seg_size=10,
                stream=RecordStreamEnum.secondary.value,
            )

        # budget clears in the primary pass alone (30 MB of primary available,
        # budget only needs ~15), so secondary should never be touched
        storage.camera_storage_stats = {
            "front_door": {"bandwidth": 15, "needs_refresh": False}
        }
        storage.reduce_storage_consumption()

        remaining_primary = Recordings.select().where(Recordings.id << primary_ids)
        remaining_secondary = Recordings.select().where(Recordings.id << secondary_ids)

        self.assertLess(remaining_primary.count(), 3)
        self.assertEqual(remaining_secondary.count(), 3)

    def test_calculate_camera_bandwidth_sums_per_stream_rates(self):
        """A mixed-stream average over the interleaved last-100 segments
        would land between the two streams' true rates; the correct
        behavior is to average each stream separately and sum."""
        config = FrigateConfig(**self.minimal_config)
        storage = StorageMaintainer(config, MagicMock())

        time_keep = datetime.datetime.now().timestamp()

        for i in range(5):
            pid = f"{3000 + i}.primary"
            _insert_mock_recording(
                pid,
                os.path.join(self.test_dir, f"{pid}.tmp"),
                time_keep + i * 10,
                time_keep + i * 10 + 10,
                seg_size=10,
                seg_dur=10,
                stream=RecordStreamEnum.primary.value,
            )

        for i in range(5):
            sid = f"{4000 + i}.secondary"
            _insert_mock_recording(
                sid,
                os.path.join(self.test_dir, f"{sid}.tmp"),
                time_keep + i * 10,
                time_keep + i * 10 + 10,
                seg_size=1,
                seg_dur=10,
                stream=RecordStreamEnum.secondary.value,
            )

        storage.calculate_camera_bandwidth()

        # primary: 10MB / 10s -> 1 MB/s -> 3600 MB/hr
        # secondary: 1MB / 10s -> 0.1 MB/s -> 360 MB/hr
        # correct: 3600 + 360 = 3960; a naive combined average would give 1980
        self.assertEqual(storage.camera_storage_stats["front_door"]["bandwidth"], 3960)


def _insert_mock_event(
    id: str,
    start: int,
    end: int,
    retain: bool,
    camera: str = "front_door",
    label: str = "Mock",
) -> Event:
    """Inserts a basic event model with a given id."""
    return Event.insert(
        id=id,
        label=label,
        camera=camera,
        start_time=start,
        end_time=end,
        top_score=100,
        false_positive=False,
        zones=list(),
        thumbnail="",
        region=[],
        box=[],
        area=0,
        has_clip=True,
        has_snapshot=True,
        retain_indefinitely=retain,
    ).execute()


def _insert_mock_recording(
    id: str,
    file: str,
    start: int,
    end: int,
    camera="front_door",
    seg_size=8,
    seg_dur=10,
    stream="primary",
) -> Event:
    """Inserts a basic recording model with a given id."""
    # we must open the file so storage maintainer will delete it
    with open(file, "w"):
        pass

    return Recordings.insert(
        id=id,
        camera=camera,
        path=file,
        start_time=start,
        end_time=end,
        duration=seg_dur,
        motion=True,
        objects=True,
        segment_size=seg_size,
        stream=stream,
    ).execute()
