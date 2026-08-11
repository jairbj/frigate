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
