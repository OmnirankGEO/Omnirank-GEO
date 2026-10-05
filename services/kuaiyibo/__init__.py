"""第二发布渠道(开源版空壳):见 config.py。"""
from .config import (
    ID_OFFSET, LEGACY_PROVIDER_KEY, PROVIDER_KEY, PROVIDER_LABEL,
    is_configured, is_kuaiyibo_local_id, to_local_id, to_provider_id,
)

__all__ = [
    "ID_OFFSET", "LEGACY_PROVIDER_KEY", "PROVIDER_KEY", "PROVIDER_LABEL",
    "is_configured", "is_kuaiyibo_local_id", "to_local_id", "to_provider_id",
]
