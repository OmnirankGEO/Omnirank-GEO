"""GEO 统一观测采集模式的强类型 SSOT。"""

from __future__ import annotations

from enum import Enum


class ObservationCollectionMode(str, Enum):
    """Only one collection owner may be active for an observation deployment."""

    EXISTING_COLLECTORS_RECONCILED = "existing_collectors_reconciled"
    NATIVE_SAMPLING_DRIVER = "native_sampling_driver"


DEFAULT_COLLECTION_MODE = ObservationCollectionMode.EXISTING_COLLECTORS_RECONCILED


def parse_collection_mode(value: object) -> ObservationCollectionMode:
    """Parse the persisted mode; unknown values fail closed instead of falling back."""
    if isinstance(value, ObservationCollectionMode):
        return value
    return ObservationCollectionMode(str(value or "").strip())
