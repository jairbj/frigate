"""Facilitates communication between processes."""

import logging
from enum import Enum
from typing import Any

from .zmq_proxy import Publisher, Subscriber

logger = logging.getLogger(__name__)


class RecordingsDataTypeEnum(str, Enum):
    all = ""
    saved = "saved"  # segment has been saved to db
    latest = "latest"  # segment is in cache
    valid = "valid"  # segment is valid
    invalid = "invalid"  # segment is invalid


class RecordingsDataPublisher(Publisher[Any]):
    """Publishes latest recording data.

    Payloads are always (camera, stream, timestamp, cache_path) 4-tuples,
    where stream is a RecordStreamEnum .value string. There is no generic
    publish() here on purpose: renaming it to publish_segment() means a
    caller written against the old 3-tuple shape fails loudly with
    AttributeError instead of silently publishing a malformed payload that
    would crash CameraWatchdog's drain loop.
    """

    topic_base = "recordings/"

    def __init__(self) -> None:
        super().__init__()

    def publish_segment(
        self,
        camera: str,
        stream: str,
        timestamp: float | None,
        cache_path: str | None,
        sub_topic: str,
    ) -> None:
        super().publish((camera, stream, timestamp, cache_path), sub_topic)


class RecordingsDataSubscriber(Subscriber):
    """Receives latest recording data."""

    topic_base = "recordings/"

    def __init__(self, topic: RecordingsDataTypeEnum) -> None:
        super().__init__(topic.value)

    def _return_object(
        self, topic: str, payload: tuple | None
    ) -> tuple[str, Any] | tuple[None, None]:
        if payload is None:
            return (None, None)

        return (topic, payload)
