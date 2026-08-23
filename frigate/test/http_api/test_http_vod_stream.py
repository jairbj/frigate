"""Tests for the stream-aware /vod routes (P4)."""

from frigate.models import Recordings
from frigate.test.http_api.base_http_test import AuthTestClient, BaseTestHttp

_MULTI_CAMERA_CONFIG = {
    "mqtt": {"host": "mqtt"},
    "auth": {
        "roles": {
            "limited_user": ["front_door"],
        }
    },
    "cameras": {
        "front_door": {
            "ffmpeg": {
                "inputs": [{"path": "rtsp://10.0.0.1:554/video", "roles": ["detect"]}]
            },
            "detect": {"height": 1080, "width": 1920, "fps": 5},
        },
        "back_door": {
            "ffmpeg": {
                "inputs": [{"path": "rtsp://10.0.0.2:554/video", "roles": ["detect"]}]
            },
            "detect": {"height": 1080, "width": 1920, "fps": 5},
        },
    },
}


class TestVodStream(BaseTestHttp):
    def setUp(self):
        super().setUp([Recordings])
        self.minimal_config = _MULTI_CAMERA_CONFIG
        self.app = super().create_app()

    def tearDown(self):
        self.app.dependency_overrides.clear()
        super().tearDown()

    def test_vod_routes_are_isolated_by_stream(self):
        """The legacy /vod/{camera}/... route must only ever see primary
        segments, and the new /vod/{camera}/stream/{stream}/... route must
        only see the requested stream -- otherwise the two resolutions'
        segments would interleave in the same HLS playlist.
        """
        Recordings.insert(
            id="primary-1",
            path="primary-1",
            camera="front_door",
            start_time=1000,
            end_time=1010,
            duration=10,
            stream="primary",
        ).execute()
        Recordings.insert(
            id="secondary-1",
            path="secondary-1",
            camera="front_door",
            start_time=1000,
            end_time=1010,
            duration=10,
            stream="secondary",
        ).execute()

        with AuthTestClient(self.app) as client:
            legacy_resp = client.get("/vod/front_door/start/1000/end/1010")
            assert legacy_resp.status_code == 200
            legacy_paths = [
                c["path"] for c in legacy_resp.json()["sequences"][0]["clips"]
            ]
            assert legacy_paths == ["primary-1"]

            primary_resp = client.get(
                "/vod/front_door/stream/primary/start/1000/end/1010"
            )
            assert primary_resp.status_code == 200
            primary_paths = [
                c["path"] for c in primary_resp.json()["sequences"][0]["clips"]
            ]
            assert primary_paths == ["primary-1"]

            secondary_resp = client.get(
                "/vod/front_door/stream/secondary/start/1000/end/1010"
            )
            assert secondary_resp.status_code == 200
            secondary_paths = [
                c["path"] for c in secondary_resp.json()["sequences"][0]["clips"]
            ]
            assert secondary_paths == ["secondary-1"]

    def test_vod_clip_stream_route_isolated_by_stream(self):
        Recordings.insert(
            id="primary-1",
            path="primary-1",
            camera="front_door",
            start_time=1000,
            end_time=1010,
            duration=10,
            stream="primary",
        ).execute()
        Recordings.insert(
            id="secondary-1",
            path="secondary-1",
            camera="front_door",
            start_time=1000,
            end_time=1010,
            duration=10,
            stream="secondary",
        ).execute()

        with AuthTestClient(self.app) as client:
            resp = client.get(
                "/vod/clip/front_door/stream/secondary/start/1000/end/1010"
            )
            assert resp.status_code == 200
            paths = [c["path"] for c in resp.json()["sequences"][0]["clips"]]
            assert paths == ["secondary-1"]
            assert resp.json()["discontinuity"] is True

    def test_vod_stream_route_rejects_invalid_stream(self):
        with AuthTestClient(self.app) as client:
            resp = client.get("/vod/front_door/stream/bogus/start/1000/end/1010")
            assert resp.status_code == 422

    def test_vod_stream_route_enforces_camera_access(self):
        """A role restricted to front_door must get 403 on back_door's
        stream-scoped VOD route, and must still be able to reach its own
        camera's stream-scoped route.
        """
        Recordings.insert(
            id="back-secondary",
            path="back-secondary",
            camera="back_door",
            start_time=1000,
            end_time=1010,
            duration=10,
            stream="secondary",
        ).execute()
        Recordings.insert(
            id="front-secondary",
            path="front-secondary",
            camera="front_door",
            start_time=1000,
            end_time=1010,
            duration=10,
            stream="secondary",
        ).execute()

        with AuthTestClient(self.app) as client:
            denied = client.get(
                "/vod/back_door/stream/secondary/start/1000/end/1010",
                headers={"remote-user": "u", "remote-role": "limited_user"},
            )
            assert denied.status_code == 403

            allowed = client.get(
                "/vod/front_door/stream/secondary/start/1000/end/1010",
                headers={"remote-user": "u", "remote-role": "limited_user"},
            )
            assert allowed.status_code == 200

    def _insert(
        self,
        id: str,
        start: float,
        end: float,
        stream: str,
        camera: str = "front_door",
    ) -> None:
        Recordings.insert(
            id=id,
            path=id,
            camera=camera,
            start_time=start,
            end_time=end,
            duration=end - start,
            stream=stream,
        ).execute()

    def test_mixed_vod_fills_gaps_and_marks_discontinuity(self):
        """The mixed route plays primary where it exists and secondary in
        between, and must declare the media info as varying so the module
        emits an init segment per resolution change.
        """
        self._insert("primary-1", 1000, 1010, "primary")
        self._insert("secondary-1", 1000, 1010, "secondary")
        self._insert("secondary-2", 1010, 1020, "secondary")

        with AuthTestClient(self.app) as client:
            resp = client.get("/vod/front_door/stream/mixed/start/1000/end/1020")
            assert resp.status_code == 200

            body = resp.json()
            assert [c["path"] for c in body["sequences"][0]["clips"]] == [
                "primary-1",
                "secondary-2",
            ]
            assert body["discontinuity"] is True
            assert body["consistentSequenceMediaInfo"] is False

    def test_mixed_vod_concatenates_consecutive_same_stream_files(self):
        """Consecutive files of one stream must collapse into a single concat
        clip: nginx-vod-module caps the clips per request, and a stream cut
        into short segments would blow past it within minutes.
        """
        self._insert("primary-1", 1000, 1010, "primary")
        for index in range(6):
            start = 1010 + index
            self._insert(f"secondary-{index}", start, start + 1, "secondary")

        with AuthTestClient(self.app) as client:
            resp = client.get("/vod/front_door/stream/mixed/start/1000/end/1016")
            assert resp.status_code == 200

            clips = resp.json()["sequences"][0]["clips"]
            assert [c["type"] for c in clips] == ["source", "concat"]
            assert clips[1]["paths"] == [f"secondary-{i}" for i in range(6)]
            assert clips[1]["durations"] == [1000] * 6
            assert resp.json()["durations"] == [10000, 6000]

    def test_mixed_vod_keeps_trimmed_edges_as_their_own_clips(self):
        """A file the gap only partly covers has to be clipped, so it cannot
        join the concat clip around it.
        """
        self._insert("primary-1", 1000, 1010, "primary")
        self._insert("secondary-1", 1005, 1015, "secondary")
        self._insert("secondary-2", 1015, 1025, "secondary")

        with AuthTestClient(self.app) as client:
            resp = client.get("/vod/front_door/stream/mixed/start/1000/end/1025")
            assert resp.status_code == 200

            clips = resp.json()["sequences"][0]["clips"]
            assert [c["type"] for c in clips] == ["source", "source", "source"]
            assert clips[1]["path"] == "secondary-1"
            # the first 5s of that file are already covered in high resolution
            assert clips[1]["clipFrom"] == 5000
            assert resp.json()["durations"] == [10000, 5000, 10000]

    def test_mixed_vod_keeps_runs_longer_than_a_segment(self):
        """The maximum duration check guards against a corrupt file, so it
        must not be applied to a run of healthy files.
        """
        self._insert("primary-1", 1000, 1010, "primary")

        for index in range(120):
            start = 1010 + index * 10
            self._insert(f"secondary-{index}", start, start + 10, "secondary")

        with AuthTestClient(self.app) as client:
            resp = client.get("/vod/front_door/stream/mixed/start/1000/end/2210")
            assert resp.status_code == 200

            body = resp.json()
            assert [c["type"] for c in body["sequences"][0]["clips"]] == [
                "source",
                "concat",
            ]
            # 20 minutes of low resolution, well past MAX_SEGMENT_DURATION
            assert body["durations"] == [10000, 1200000]

    def test_mixed_vod_leaves_segment_duration_to_nginx(self):
        """A mixed clip can span a whole run, so its length says nothing about
        how long a playlist segment should be.
        """
        self._insert("primary-1", 1000, 1010, "primary")
        self._insert("secondary-1", 1010, 1020, "secondary")

        with AuthTestClient(self.app) as client:
            mixed = client.get("/vod/front_door/stream/mixed/start/1000/end/1020")
            assert mixed.status_code == 200
            assert "segment_duration" not in mixed.json()

            primary = client.get("/vod/front_door/stream/primary/start/1000/end/1020")
            assert primary.status_code == 200
            assert primary.json()["segment_duration"] == 10000
