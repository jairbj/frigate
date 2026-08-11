"""Handle storage retention and usage."""

import logging
import shutil
import threading
from collections.abc import Iterable, Iterator
from multiprocessing.synchronize import Event as MpEvent
from pathlib import Path
from typing import Any

from peewee import SQL, fn

from frigate.config import FrigateConfig
from frigate.const import RECORD_DIR, REPLAY_CAMERA_PREFIX
from frigate.models import Event, Recordings
from frigate.record.types import RecordStreamEnum
from frigate.util.builtin import clear_and_unlink

logger = logging.getLogger(__name__)
bandwidth_equation = Recordings.segment_size / (
    Recordings.end_time - Recordings.start_time
)

MAX_CALCULATED_BANDWIDTH = 10000  # 10Gb/hr


class StorageMaintainer(threading.Thread):
    """Maintain frigates recording storage."""

    def __init__(self, config: FrigateConfig, stop_event: MpEvent) -> None:
        super().__init__(name="storage_maintainer")
        self.config = config
        self.stop_event = stop_event
        self.camera_storage_stats: dict[str, dict] = {}

    def calculate_camera_bandwidth(self) -> None:
        """Calculate an average MB/hr for each camera, summed across its streams.

        A mixed-stream average (over the last 100 segments regardless of
        stream) would land between the two streams' true rates whenever
        both are active, since primary and secondary segments interleave
        in start_time order. Each stream is averaged separately and the
        results summed instead.
        """
        for camera in self.config.cameras.keys():
            # Skip replay cameras
            if camera.startswith(REPLAY_CAMERA_PREFIX):
                continue

            # cameras with < 50 segments should be refreshed to keep size accurate
            # when few segments are available
            if self.camera_storage_stats.get(camera, {}).get("needs_refresh", True):
                total_bandwidth = 0.0
                any_needs_refresh = False

                for stream in RecordStreamEnum:
                    if (
                        Recordings.select(fn.COUNT("*"))
                        .where(
                            Recordings.camera == camera,
                            Recordings.stream == stream.value,
                            Recordings.segment_size > 0,
                        )
                        .scalar()
                        < 50
                    ):
                        any_needs_refresh = True

                    # calculate MB/hr from last 100 segments of this stream
                    try:
                        # Subquery to get last 100 segments, then average their bandwidth
                        last_100 = (
                            Recordings.select(bandwidth_equation.alias("bw"))
                            .where(
                                Recordings.camera == camera,
                                Recordings.stream == stream.value,
                                Recordings.segment_size > 0,
                            )
                            .order_by(Recordings.start_time.desc())
                            .limit(100)
                            .alias("recent")
                        )

                        stream_bandwidth = round(
                            Recordings.select(fn.AVG(SQL("bw")))
                            .from_(last_100)
                            .scalar()
                            * 3600,
                            2,
                        )
                    except TypeError:
                        stream_bandwidth = 0

                    total_bandwidth += stream_bandwidth

                if total_bandwidth > MAX_CALCULATED_BANDWIDTH:
                    logger.warning(
                        f"{camera} has a bandwidth of {total_bandwidth} MB/hr which exceeds the expected maximum. This typically indicates an issue with the cameras recordings."
                    )
                    total_bandwidth = MAX_CALCULATED_BANDWIDTH

                self.camera_storage_stats[camera] = {
                    "needs_refresh": any_needs_refresh,
                    "bandwidth": total_bandwidth,
                }
                logger.debug(f"{camera} has a bandwidth of {total_bandwidth} MiB/hr.")

    def calculate_camera_usages(self) -> dict[str, dict]:
        """Calculate the storage usage of each camera."""
        usages: dict[str, dict] = {}

        for camera in self.config.cameras.keys():
            # Skip replay cameras
            if camera.startswith(REPLAY_CAMERA_PREFIX):
                continue

            camera_storage = (
                Recordings.select(fn.SUM(Recordings.segment_size))
                .where(Recordings.camera == camera, Recordings.segment_size != 0)
                .scalar()
            )

            camera_key = (
                getattr(self.config.cameras[camera], "friendly_name", None) or camera
            )
            usages[camera_key] = {
                "usage": camera_storage,
                "bandwidth": self.camera_storage_stats.get(camera, {}).get(
                    "bandwidth", 0
                ),
            }

        return usages

    def check_storage_needs_cleanup(self) -> bool:
        """Return if storage needs cleanup."""
        # currently runs cleanup if less than 1 hour of space is left
        # disk_usage should not spin up disks
        hourly_bandwidth = sum(
            [b["bandwidth"] for b in self.camera_storage_stats.values()]
        )
        remaining_storage = round(shutil.disk_usage(RECORD_DIR).free / pow(2, 20), 1)
        logger.debug(
            f"Storage cleanup check: {hourly_bandwidth} hourly with remaining storage: {remaining_storage}."
        )
        return remaining_storage < float(hourly_bandwidth)

    @staticmethod
    def _stream_recordings_query(stream: RecordStreamEnum) -> Iterator[Any]:
        return (
            Recordings.select(
                Recordings.id,
                Recordings.camera,
                Recordings.start_time,
                Recordings.end_time,
                Recordings.segment_size,
                Recordings.path,
            )
            .where(Recordings.stream == stream.value)
            .order_by(Recordings.start_time.asc())
            .namedtuples()
            .iterator()
        )

    @staticmethod
    def _delete_pass(
        query: Iterable[Any],
        retained_events: Any,
        budget: float,
        deleted_size: float,
    ) -> tuple[float, list[Any]]:
        """Delete recordings from query, skipping ones retained_events keeps.

        Stops once deleted_size exceeds budget. retained_events must be
        sorted by start_time; reusing the same cached namedtuples sequence
        across multiple calls (e.g. once per stream) is safe since each
        call tracks its own event_start cursor.
        """
        event_start = 0
        deleted_recordings = []
        for recording in query:
            if deleted_size > budget:
                break

            keep = False

            # Now look for a reason to keep this recording segment
            for idx in range(event_start, len(retained_events)):
                event = retained_events[idx]

                # if the event starts in the future, stop checking events
                # and let this recording segment expire
                if event.start_time > recording.end_time:
                    keep = False
                    break

                # if the event is in progress or ends after the recording starts, keep it
                # and stop looking at events
                if event.end_time is None or event.end_time >= recording.start_time:
                    keep = True
                    break

                # if the event ends before this recording segment starts, skip
                # this event and check the next event for an overlap.
                # since the events and recordings are sorted, we can skip events
                # that end before the previous recording segment started on future segments
                if event.end_time < recording.start_time:
                    event_start = idx

            # Delete recordings not retained indefinitely
            if not keep:
                try:
                    clear_and_unlink(Path(recording.path), missing_ok=False)
                    deleted_recordings.append(recording)
                    deleted_size += recording.segment_size
                except FileNotFoundError:
                    # this file was not found so we must assume no space was cleaned up
                    pass

        return deleted_size, deleted_recordings

    @staticmethod
    def _force_delete_pass(
        query: Iterable[Any], budget: float, deleted_size: float
    ) -> tuple[float, list[Any]]:
        """Delete recordings from query unconditionally (ignoring retention)."""
        deleted_recordings = []
        for recording in query:
            if deleted_size > budget:
                break

            try:
                clear_and_unlink(Path(recording.path), missing_ok=False)
                deleted_size += recording.segment_size
                deleted_recordings.append(recording)
            except FileNotFoundError:
                # this file was not found so we must assume no space was cleaned up
                pass

        return deleted_size, deleted_recordings

    def reduce_storage_consumption(self) -> None:
        """Remove oldest hour of recordings, high-res before low-res.

        The premise of dual-stream recording is that the low-res 24x7
        timeline is the asset worth protecting; the high-res stream is a
        bonus around events. Under disk pressure the correct sacrifice is
        high-res, so primary is fully drained (both the non-retained and,
        if still short, the force-delete pass) before secondary is touched.
        """
        logger.debug("Starting storage cleanup.")
        deleted_segments_size = 0.0
        hourly_bandwidth = sum(
            [b["bandwidth"] for b in self.camera_storage_stats.values()]
        )

        retained_events = (
            Event.select(
                Event.start_time,
                Event.end_time,
            )
            .where(
                Event.retain_indefinitely == True,
                Event.has_clip,
            )
            .order_by(Event.start_time.asc())
            .namedtuples()
        )

        deleted_recordings = []
        for stream in RecordStreamEnum:
            if deleted_segments_size > hourly_bandwidth:
                break
            deleted_segments_size, newly_deleted = self._delete_pass(
                self._stream_recordings_query(stream),
                retained_events,
                hourly_bandwidth,
                deleted_segments_size,
            )
            deleted_recordings.extend(newly_deleted)

        # check if need to delete retained segments
        if deleted_segments_size < hourly_bandwidth:
            logger.error(
                f"Could not clear {hourly_bandwidth} MB, currently {deleted_segments_size:.2f} MB have been cleared. Retained recordings must be deleted."
            )
            for stream in RecordStreamEnum:
                if deleted_segments_size > hourly_bandwidth:
                    break
                deleted_segments_size, newly_deleted = self._force_delete_pass(
                    self._stream_recordings_query(stream),
                    hourly_bandwidth,
                    deleted_segments_size,
                )
                deleted_recordings.extend(newly_deleted)
        else:
            logger.info(f"Cleaned up {deleted_segments_size:.2f} MB of recordings")

        logger.debug(f"Expiring {len(deleted_recordings)} recordings")
        # delete up to 100,000 at a time
        max_deletes = 100000

        # Update has_clip for events that overlap with deleted recordings
        if deleted_recordings:
            # Group deleted recordings by camera
            camera_recordings = {}
            for recording in deleted_recordings:
                if recording.camera not in camera_recordings:
                    camera_recordings[recording.camera] = {
                        "min_start": recording.start_time,
                        "max_end": recording.end_time,
                    }
                else:
                    camera_recordings[recording.camera]["min_start"] = min(
                        camera_recordings[recording.camera]["min_start"],
                        recording.start_time,
                    )
                    camera_recordings[recording.camera]["max_end"] = max(
                        camera_recordings[recording.camera]["max_end"],
                        recording.end_time,
                    )

            # Find all events that overlap with deleted recordings time range per camera
            events_to_update = []
            for camera, time_range in camera_recordings.items():
                overlapping_events = Event.select(Event.id).where(
                    Event.camera == camera,
                    Event.has_clip == True,
                    Event.start_time < time_range["max_end"],
                    Event.end_time > time_range["min_start"],
                )

                for event in overlapping_events:
                    events_to_update.append(event.id)

            # Update has_clip to False for overlapping events
            if events_to_update:
                for i in range(0, len(events_to_update), max_deletes):
                    batch = events_to_update[i : i + max_deletes]
                    Event.update(has_clip=False).where(Event.id << batch).execute()
                logger.debug(
                    f"Updated has_clip to False for {len(events_to_update)} events"
                )

        deleted_recordings_list = [r.id for r in deleted_recordings]
        for i in range(0, len(deleted_recordings_list), max_deletes):
            Recordings.delete().where(
                Recordings.id << deleted_recordings_list[i : i + max_deletes]
            ).execute()

    def run(self) -> None:
        """Check every 5 minutes if storage needs to be cleaned up."""
        if self.config.safe_mode:
            logger.info("Safe mode enabled, skipping storage maintenance")
            return

        self.calculate_camera_bandwidth()
        while not self.stop_event.wait(300):
            if not self.camera_storage_stats or True in [
                r["needs_refresh"] for r in self.camera_storage_stats.values()
            ]:
                self.calculate_camera_bandwidth()
                logger.debug(f"Default camera bandwidths: {self.camera_storage_stats}.")

            if self.check_storage_needs_cleanup():
                logger.info(
                    "Less than 1 hour of recording space left, running storage maintenance..."
                )
                self.reduce_storage_consumption()

        logger.info("Exiting storage maintainer...")
