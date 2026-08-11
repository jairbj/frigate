import datetime
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Mock complex imports before importing maintainer, saving originals so we can
# restore them after import and avoid polluting sys.modules for other tests.
_MOCKED_MODULES = [
    "frigate.comms.inter_process",
    "frigate.comms.detections_updater",
    "frigate.comms.recordings_updater",
    "frigate.config.camera.updater",
]
_originals = {name: sys.modules.get(name) for name in _MOCKED_MODULES}
for name in _MOCKED_MODULES:
    sys.modules[name] = MagicMock()

# Now import the class under test
from frigate.config import FrigateConfig  # noqa: E402
from frigate.record.maintainer import RecordingMaintainer  # noqa: E402
from frigate.record.types import RecordStreamEnum  # noqa: E402

# Restore original modules (or remove mock if there was no original)
for name, orig in _originals.items():
    if orig is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = orig


class TestMaintainer(unittest.IsolatedAsyncioTestCase):
    async def test_move_files_survives_bad_filename(self):
        config = MagicMock(spec=FrigateConfig)
        config.cameras = {}
        stop_event = MagicMock()

        maintainer = RecordingMaintainer(config, stop_event)

        # We need to mock end_time_cache to avoid key errors if logic proceeds
        maintainer.end_time_cache = {}

        # Mock filesystem
        # One bad file, one good file
        files = ["bad_filename.mp4", "camera@20210101000000+0000.mp4"]

        with patch("os.listdir", return_value=files):
            with patch("os.path.isfile", return_value=True):
                with patch(
                    "frigate.record.maintainer.psutil.process_iter", return_value=[]
                ):
                    with patch("frigate.record.maintainer.logger.warning") as warn:
                        # Mock validate_and_move_segment to avoid further logic
                        maintainer.validate_and_move_segment = MagicMock()

                        try:
                            await maintainer.move_files()
                        except ValueError as e:
                            if "not enough values to unpack" in str(e):
                                self.fail("move_files() crashed on bad filename!")
                            raise e
                        except Exception:
                            # Ignore other errors (like DB connection) as we only care about the unpack crash
                            pass

                        # The bad filename is encountered in multiple loops, but should only warn once.
                        matching = [
                            c
                            for c in warn.call_args_list
                            if c.args
                            and isinstance(c.args[0], str)
                            and "Skipping unexpected files in cache" in c.args[0]
                        ]
                        self.assertEqual(
                            1,
                            len(matching),
                            f"Expected a single warning for unexpected files, got {len(matching)}",
                        )

    async def test_drops_quiet_segment_when_only_motion_retention(self):
        # Regression: when motion retention is enabled but a segment has no
        # motion and no review overlaps it, the segment must still be dropped.
        # Otherwise it sits in cache forever, accumulates, and triggers the
        # "Unable to keep up with recording segments in cache" warning every
        # ~10s as the overflow trim in move_files discards the oldest one.
        config = MagicMock(spec=FrigateConfig)

        camera_config = MagicMock()
        camera_config.record.enabled = True
        camera_config.record.continuous.days = 0
        camera_config.record.motion.days = 1
        camera_config.record.event_pre_capture = 5
        camera_config.record.stream_enabled.return_value = True
        camera_config.record.get_retention.return_value = (
            camera_config.record.continuous,
            camera_config.record.motion,
        )
        config.cameras = {"test_cam": camera_config}

        stop_event = MagicMock()
        maintainer = RecordingMaintainer(config, stop_event)

        now = datetime.datetime.now(datetime.UTC)
        start_time = now - datetime.timedelta(seconds=20)
        end_time = now - datetime.timedelta(seconds=10)
        cache_path = "/tmp/cache/test_cam@20260417150000+0000.mp4"

        maintainer.end_time_cache = {cache_path: (end_time, 10.0)}
        # Single processed frame well past end_time with no motion/objects.
        maintainer.object_recordings_info["test_cam"] = [(now.timestamp(), [], [], [])]
        maintainer.audio_recordings_info["test_cam"] = []

        maintainer.drop_segment = MagicMock()
        maintainer.recordings_publisher = MagicMock()

        result = await maintainer.validate_and_move_segment(
            "test_cam",
            RecordStreamEnum.primary,
            reviews=[],
            recording={"start_time": start_time, "cache_path": cache_path},
        )

        self.assertIsNone(result)
        maintainer.drop_segment.assert_called_once_with(cache_path)

    async def test_move_files_groups_by_camera_and_stream(self):
        config = MagicMock(spec=FrigateConfig)
        camera_config = MagicMock()
        camera_config.record.enabled_streams.return_value = []
        config.cameras = {"cam": camera_config}

        stop_event = MagicMock()
        maintainer = RecordingMaintainer(config, stop_event)
        maintainer.end_time_cache = {}
        maintainer.validate_and_move_segment = AsyncMock(return_value=None)
        # move_files() reaches self.requestor.send_data() on this path (unlike
        # the mocked-and-swallowed-exception bad-filename test above), so it
        # must be a mock explicitly rather than relying on the sys.modules
        # trick above, whose effect depends on import order across the full
        # suite: if some other test file already imported
        # frigate.comms.inter_process for real before this module did, the
        # real one wins the module cache and send_data() blocks forever on a
        # REQ/REP round trip with no responder.
        maintainer.requestor = MagicMock()
        maintainer.recordings_publisher = MagicMock()

        files = [
            "cam@20260101000000+0000.mp4",
            "cam#secondary@20260101000000+0000.mp4",
        ]

        with patch("os.listdir", return_value=files):
            with patch("os.path.isfile", return_value=True):
                with patch(
                    "frigate.record.maintainer.psutil.process_iter", return_value=[]
                ):
                    await maintainer.move_files()

        # one call per (camera, stream) segment
        self.assertEqual(maintainer.validate_and_move_segment.call_count, 2)
        called_streams = {
            call.args[1] for call in maintainer.validate_and_move_segment.call_args_list
        }
        self.assertEqual(
            called_streams, {RecordStreamEnum.primary, RecordStreamEnum.secondary}
        )

    async def test_pop_loop_uses_camera_wide_floor_not_per_stream_floor(self):
        """Regression: trimming object_recordings_info to a single stream
        group's own oldest segment can discard frame data a slower sibling
        stream still needs. The floor must be the minimum oldest segment
        start time across ALL of a camera's stream groups.
        """
        config = MagicMock(spec=FrigateConfig)
        camera_config = MagicMock()
        camera_config.record.enabled_streams.return_value = []
        config.cameras = {"cam": camera_config}

        stop_event = MagicMock()
        maintainer = RecordingMaintainer(config, stop_event)
        maintainer.end_time_cache = {}
        maintainer.validate_and_move_segment = AsyncMock(return_value=None)
        # see comment on the same line in test_move_files_groups_by_camera_and_stream
        maintainer.requestor = MagicMock()
        maintainer.recordings_publisher = MagicMock()

        # secondary's oldest segment starts earlier than primary's oldest
        secondary_start = "20260101000000+0000"
        primary_start = "20260101000010+0000"  # 10s later

        files = [
            f"cam#secondary@{secondary_start}.mp4",
            f"cam@{primary_start}.mp4",
        ]

        secondary_ts = (
            datetime.datetime.strptime(secondary_start, "%Y%m%d%H%M%S%z")
            .astimezone(datetime.UTC)
            .timestamp()
        )
        primary_ts = (
            datetime.datetime.strptime(primary_start, "%Y%m%d%H%M%S%z")
            .astimezone(datetime.UTC)
            .timestamp()
        )

        # a motion frame between the two floors: after secondary's oldest
        # segment start, but before primary's. A per-group trim (using
        # whichever group happens to be processed) could discard this if
        # primary's group's floor were used instead of the camera-wide min.
        between_ts = secondary_ts + 2
        self.assertTrue(secondary_ts < between_ts < primary_ts)

        maintainer.object_recordings_info["cam"] = [(between_ts, [], [], [])]
        maintainer.audio_recordings_info["cam"] = []

        with patch("os.listdir", return_value=files):
            with patch("os.path.isfile", return_value=True):
                with patch(
                    "frigate.record.maintainer.psutil.process_iter", return_value=[]
                ):
                    await maintainer.move_files()

        self.assertEqual(
            maintainer.object_recordings_info["cam"],
            [(between_ts, [], [], [])],
        )

    async def test_move_segment_writes_to_secondary_subdirectory(self):
        config = MagicMock()
        config.cameras = {}
        stop_event = MagicMock()
        maintainer = RecordingMaintainer(config, stop_event)
        maintainer.end_time_cache = {}

        start_time = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        end_time = start_time + datetime.timedelta(seconds=10)
        segment_info = MagicMock()
        segment_info.motion_count = 0
        segment_info.active_object_count = 0
        segment_info.region_count = 0
        segment_info.average_dBFS = 0
        segment_info.motion_heatmap = None

        with patch("os.makedirs"):
            with patch("os.path.exists", return_value=False):
                with patch("asyncio.create_subprocess_exec") as mock_exec:
                    proc = AsyncMock()
                    proc.returncode = 0
                    proc.wait = AsyncMock()
                    mock_exec.return_value = proc
                    with patch("os.path.getsize", return_value=1024 * 1024):
                        with patch("os.remove"):
                            result = await maintainer.move_segment(
                                "cam",
                                RecordStreamEnum.secondary,
                                start_time,
                                end_time,
                                10.0,
                                "/tmp/cache/cam#secondary@x.mp4",
                                segment_info,
                            )

        self.assertIsNotNone(result)
        self.assertIn("/cam/secondary/", result["path"])
        self.assertEqual(result["stream"], "secondary")

    async def test_move_segment_primary_path_unchanged(self):
        config = MagicMock()
        config.cameras = {}
        stop_event = MagicMock()
        maintainer = RecordingMaintainer(config, stop_event)
        maintainer.end_time_cache = {}

        start_time = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        end_time = start_time + datetime.timedelta(seconds=10)
        segment_info = MagicMock()
        segment_info.motion_count = 0
        segment_info.active_object_count = 0
        segment_info.region_count = 0
        segment_info.average_dBFS = 0
        segment_info.motion_heatmap = None

        with patch("os.makedirs"):
            with patch("os.path.exists", return_value=False):
                with patch("asyncio.create_subprocess_exec") as mock_exec:
                    proc = AsyncMock()
                    proc.returncode = 0
                    proc.wait = AsyncMock()
                    mock_exec.return_value = proc
                    with patch("os.path.getsize", return_value=1024 * 1024):
                        with patch("os.remove"):
                            result = await maintainer.move_segment(
                                "cam",
                                RecordStreamEnum.primary,
                                start_time,
                                end_time,
                                10.0,
                                "/tmp/cache/cam@x.mp4",
                                segment_info,
                            )

        self.assertIsNotNone(result)
        self.assertNotIn("secondary", result["path"])
        self.assertEqual(result["stream"], "primary")

    async def test_secondary_retention_keeps_segment_primary_would_drop(self):
        """secondary with continuous.days=30 keeps a motionless segment
        that primary (continuous.days=0) would discard, using the same
        validate_and_move_segment call shape for both streams."""
        config = MagicMock(spec=FrigateConfig)
        camera_config = MagicMock()
        camera_config.record.continuous.days = 0
        camera_config.record.motion.days = 0
        camera_config.record.secondary.continuous.days = 30
        camera_config.record.secondary.motion.days = 0
        camera_config.record.get_retention.side_effect = lambda s: (
            (
                camera_config.record.secondary.continuous,
                camera_config.record.secondary.motion,
            )
            if s == RecordStreamEnum.secondary
            else (camera_config.record.continuous, camera_config.record.motion)
        )
        camera_config.record.stream_enabled.return_value = True
        config.cameras = {"cam": camera_config}

        stop_event = MagicMock()
        maintainer = RecordingMaintainer(config, stop_event)

        now = datetime.datetime.now(datetime.UTC)
        start_time = now - datetime.timedelta(seconds=20)
        end_time = now - datetime.timedelta(seconds=10)

        # no motion recorded for this window
        maintainer.object_recordings_info["cam"] = [(now.timestamp(), [], [], [])]
        maintainer.audio_recordings_info["cam"] = []

        maintainer.drop_segment = MagicMock()
        maintainer.move_segment = AsyncMock(return_value={"moved": True})
        maintainer.recordings_publisher = MagicMock()

        primary_cache_path = "/tmp/cache/cam@x.mp4"
        secondary_cache_path = "/tmp/cache/cam#secondary@x.mp4"
        maintainer.end_time_cache = {
            primary_cache_path: (end_time, 10.0),
            secondary_cache_path: (end_time, 10.0),
        }

        primary_result = await maintainer.validate_and_move_segment(
            "cam",
            RecordStreamEnum.primary,
            reviews=[],
            recording={"start_time": start_time, "cache_path": primary_cache_path},
        )
        secondary_result = await maintainer.validate_and_move_segment(
            "cam",
            RecordStreamEnum.secondary,
            reviews=[],
            recording={"start_time": start_time, "cache_path": secondary_cache_path},
        )

        # primary falls through continuous/motion (both 0) with no review
        # overlap, so it's neither moved nor dropped yet in this call
        # (dropped later once past event_pre_capture) -- but must NOT be
        # moved via the continuous/motion path.
        self.assertIsNone(primary_result)
        # secondary's continuous.days=30 keeps it immediately
        self.assertEqual(secondary_result, {"moved": True})
        maintainer.move_segment.assert_called_once_with(
            "cam",
            RecordStreamEnum.secondary,
            start_time,
            end_time,
            10.0,
            secondary_cache_path,
            maintainer.move_segment.call_args.args[6],
        )

    async def test_expire_stale_recordings_info_drops_only_absent_cameras(self):
        config = MagicMock(spec=FrigateConfig)
        config.cameras = {}
        stop_event = MagicMock()
        maintainer = RecordingMaintainer(config, stop_event)

        now = datetime.datetime.now().timestamp()
        ancient = now - 86400
        recent = now - 1

        maintainer.object_recordings_info["present_cam"] = [(ancient, [], [], [])]
        maintainer.audio_recordings_info["present_cam"] = [(ancient, 0, [])]

        maintainer.object_recordings_info["absent_cam"] = [
            (ancient, [], [], []),
            (recent, [], [], []),
        ]
        maintainer.audio_recordings_info["absent_cam"] = [
            (ancient, 0, []),
            (recent, 0, []),
        ]

        grouped_recordings = {"present_cam": [{"start_time": ancient}]}

        maintainer._expire_stale_recordings_info(grouped_recordings)

        self.assertEqual(
            maintainer.object_recordings_info["present_cam"], [(ancient, [], [], [])]
        )
        self.assertEqual(
            maintainer.audio_recordings_info["present_cam"], [(ancient, 0, [])]
        )

        self.assertEqual(
            maintainer.object_recordings_info["absent_cam"], [(recent, [], [], [])]
        )
        self.assertEqual(
            maintainer.audio_recordings_info["absent_cam"], [(recent, 0, [])]
        )


if __name__ == "__main__":
    unittest.main()
