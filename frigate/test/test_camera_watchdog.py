"""Tests for CameraWatchdog's per-stream recording health tracking.

The dual-stream feature's whole safety story for the 24x7 secondary
stream rests on this: before per-stream tracking, a healthy primary kept
`latest_valid_segment_time` fresh, so a dead secondary would never be
detected. These tests pin that behavior down.
"""

import unittest
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from frigate.config import FrigateConfig
from frigate.config.camera.ffmpeg import CameraRoleEnum
from frigate.record.types import RecordStreamEnum
from frigate.video.ffmpeg import CameraWatchdog

DUAL_STREAM_CONFIG = {
    "mqtt": {"host": "mqtt"},
    "record": {
        "enabled": True,
        "continuous": {"days": 0},
        "secondary": {"enabled": True, "continuous": {"days": 30}},
    },
    "cameras": {
        "front_door": {
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
    },
}


def _build_watchdog(config: FrigateConfig) -> CameraWatchdog:
    """Construct a CameraWatchdog without running __init__.

    __init__ builds ZMQ subscribers and an InterProcessRequestor, which
    would block on a REQ/REP round trip with no responder in tests. Only
    the attributes the run() loop and the staleness helpers touch are
    populated here.
    """
    watchdog = CameraWatchdog.__new__(CameraWatchdog)
    camera_config = config.cameras["front_door"]

    watchdog.config = camera_config
    watchdog.logger = MagicMock()
    watchdog.requestor = MagicMock()
    watchdog.sleeptime = 10

    watchdog.latest_valid_segment_time = defaultdict(float)
    watchdog.latest_invalid_segment_time = defaultdict(float)
    watchdog.latest_cache_segment_time = defaultdict(float)
    watchdog.record_enable_time = None
    watchdog.detect_process_secondary_stream = None
    # 10s segments -> max(120, 2*10+30) == 120s
    watchdog.record_stale_threshold = {s.value: 120 for s in RecordStreamEnum}

    watchdog._last_detect_status = None
    watchdog._last_record_status = {}
    watchdog._last_status_update_time = 0.0
    watchdog._stall_timestamps = deque()
    watchdog._stall_active = False
    watchdog.fps_overflow_count = 0
    watchdog.reconnect_timestamps = deque()
    watchdog.reconnects = None
    watchdog.detection_frame = None

    watchdog.was_enabled = camera_config.enabled
    watchdog.was_record_enabled_in_config = camera_config.record.enabled_in_config
    watchdog.was_secondary_enabled_in_config = (
        camera_config.record.secondary.enabled_in_config
    )

    return watchdog


class TestStreamStaleness(unittest.TestCase):
    """_stream_staleness must judge each stream on its own timestamps."""

    def setUp(self):
        self.config = FrigateConfig(**DUAL_STREAM_CONFIG)
        self.watchdog = _build_watchdog(self.config)
        self.now = datetime.now().astimezone(UTC)

    def test_healthy_primary_does_not_mask_stale_secondary(self):
        """The core regression: a fresh primary must not make a dead
        secondary look healthy. Before per-stream dicts, both streams
        shared one `latest_valid_segment_time` scalar, so this was
        undetectable."""
        fresh = self.now.timestamp()
        stale = (self.now - timedelta(seconds=600)).timestamp()

        self.watchdog.latest_cache_segment_time["primary"] = fresh
        self.watchdog.latest_valid_segment_time["primary"] = fresh
        self.watchdog.latest_cache_segment_time["secondary"] = stale
        self.watchdog.latest_valid_segment_time["secondary"] = stale

        primary_stale, _ = self.watchdog._stream_staleness(
            RecordStreamEnum.primary, self.now
        )
        secondary_stale, reason = self.watchdog._stream_staleness(
            RecordStreamEnum.secondary, self.now
        )

        self.assertFalse(primary_stale, "healthy primary was reported stale")
        self.assertTrue(
            secondary_stale,
            "a secondary stream with no segments for 600s was not detected as "
            "stale -- a healthy primary is masking it",
        )
        self.assertIn("recording segments", reason)

    def test_stale_primary_does_not_condemn_healthy_secondary(self):
        """The mirror case, so the isolation is proven in both directions."""
        fresh = self.now.timestamp()
        stale = (self.now - timedelta(seconds=600)).timestamp()

        self.watchdog.latest_cache_segment_time["primary"] = stale
        self.watchdog.latest_valid_segment_time["primary"] = stale
        self.watchdog.latest_cache_segment_time["secondary"] = fresh
        self.watchdog.latest_valid_segment_time["secondary"] = fresh

        primary_stale, _ = self.watchdog._stream_staleness(
            RecordStreamEnum.primary, self.now
        )
        secondary_stale, _ = self.watchdog._stream_staleness(
            RecordStreamEnum.secondary, self.now
        )

        self.assertTrue(primary_stale)
        self.assertFalse(secondary_stale)

    def test_grace_period_suppresses_staleness_for_both_streams(self):
        """Right after enabling, neither stream has produced a segment yet;
        the 90s grace period must cover both."""
        stale = (self.now - timedelta(seconds=600)).timestamp()
        self.watchdog.record_enable_time = self.now - timedelta(seconds=10)

        for stream_key in ("primary", "secondary"):
            self.watchdog.latest_cache_segment_time[stream_key] = stale
            self.watchdog.latest_valid_segment_time[stream_key] = stale

        for stream in RecordStreamEnum:
            is_stale, _ = self.watchdog._stream_staleness(stream, self.now)
            self.assertFalse(
                is_stale, f"{stream.value} flagged stale during grace period"
            )


class TestRecordStatusTopics(unittest.TestCase):
    """Each stream reports health on its own MQTT topic."""

    def setUp(self):
        self.config = FrigateConfig(**DUAL_STREAM_CONFIG)
        self.watchdog = _build_watchdog(self.config)

    def test_streams_publish_to_distinct_topics(self):
        now = datetime.now().timestamp()

        self.watchdog._send_record_status(RecordStreamEnum.primary, "online", now)
        self.watchdog._send_record_status(RecordStreamEnum.secondary, "offline", now)

        topics = {
            call.args[0]: call.args[1]
            for call in self.watchdog.requestor.send_data.call_args_list
        }

        self.assertEqual(topics["front_door/status/record"], "online")
        self.assertEqual(topics["front_door/status/record_secondary"], "offline")

    def test_status_cache_is_tracked_per_stream(self):
        """One stream's cached status must not suppress the other's first
        publish -- a shared scalar cache would swallow it."""
        now = datetime.now().timestamp()

        self.watchdog._send_record_status(RecordStreamEnum.primary, "online", now)
        self.watchdog.requestor.send_data.reset_mock()

        # same status value, different stream: must still be published
        self.watchdog._send_record_status(RecordStreamEnum.secondary, "online", now)

        self.watchdog.requestor.send_data.assert_called_once_with(
            "front_door/status/record_secondary", "online"
        )


class TestWatchdogRestartsOnlyStaleStream(unittest.TestCase):
    """Driving one run() iteration to prove restart scoping."""

    def _make_process(self, roles: list[CameraRoleEnum], cmd: list[str]) -> dict:
        process = MagicMock()
        process.poll.return_value = None
        return {
            "cmd": cmd,
            "roles": roles,
            "process": process,
            "logpipe": MagicMock(),
            "latest_segment_time": 0,
        }

    def test_stale_secondary_restarts_only_its_own_process(self):
        config = FrigateConfig(**DUAL_STREAM_CONFIG)
        watchdog = _build_watchdog(config)

        now = datetime.now().astimezone(UTC)
        fresh = now.timestamp()
        stale = (now - timedelta(seconds=600)).timestamp()

        watchdog.latest_cache_segment_time["primary"] = fresh
        watchdog.latest_valid_segment_time["primary"] = fresh
        watchdog.latest_cache_segment_time["secondary"] = stale
        watchdog.latest_valid_segment_time["secondary"] = stale

        primary_proc = self._make_process(
            [CameraRoleEnum.record], ["ffmpeg", "primary-cmd"]
        )
        secondary_proc = self._make_process(
            [CameraRoleEnum.record_secondary], ["ffmpeg", "secondary-cmd"]
        )
        watchdog.ffmpeg_other_processes = [primary_proc, secondary_proc]

        # Skip the pre-loop startup: it would call start_all_ffmpeg() and
        # rebuild ffmpeg_other_processes, discarding the fixtures above.
        watchdog._update_enabled_state = MagicMock(return_value=False)
        watchdog.start_all_ffmpeg = MagicMock()
        # post-loop teardown is not under test
        watchdog.stop_all_ffmpeg = MagicMock()
        watchdog.logpipe = MagicMock()
        watchdog.stalls = None
        watchdog.sleeptime = 0

        # one loop iteration, then exit
        watchdog.stop_event = MagicMock()
        watchdog.stop_event.wait.side_effect = [False, True]

        watchdog.config_subscriber = MagicMock()
        watchdog.config_subscriber.check_for_updates.return_value = {}
        watchdog.segment_subscriber = MagicMock()
        watchdog.segment_subscriber.check_for_update.return_value = (None, None)

        watchdog.capture_thread = MagicMock()
        watchdog.capture_thread.is_alive.return_value = True
        watchdog.capture_thread.current_frame.value = datetime.now().timestamp()
        watchdog.camera_fps = MagicMock()
        watchdog.camera_fps.value = 5

        with patch("frigate.video.ffmpeg.start_or_restart_ffmpeg") as restart:
            restart.return_value = MagicMock()
            watchdog.run()

        self.assertEqual(
            restart.call_count,
            1,
            "expected exactly one ffmpeg restart (the stale secondary), got "
            f"{restart.call_count}",
        )
        self.assertEqual(
            restart.call_args.args[0],
            ["ffmpeg", "secondary-cmd"],
            "the wrong ffmpeg process was restarted -- a stale secondary must "
            "not take down the healthy primary recording process",
        )

        statuses = {
            call.args[0]: call.args[1]
            for call in watchdog.requestor.send_data.call_args_list
        }
        self.assertEqual(statuses["front_door/status/record_secondary"], "offline")
        self.assertEqual(
            statuses["front_door/status/record"],
            "online",
            "the primary stream was marked offline even though it was healthy",
        )


if __name__ == "__main__":
    unittest.main()
