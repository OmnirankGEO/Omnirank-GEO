"""多渠道分流(开源版空壳):只有主渠道一家,所有条目原样归主渠道,不改道。"""
from __future__ import annotations

from typing import Any, Iterable

from .config import LEGACY_PROVIDER_KEY


def provider_of_media(media_id: Any) -> str:
    return LEGACY_PROVIDER_KEY


def apply_provider_preference(items: list[dict[str, Any]], *,
                              is_wemedia: bool) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    return list(items or ()), {}


def persist_routing(routed_meta: dict[int, dict[str, Any]]) -> None:
    return None


def clear_routing(item_ids: Iterable[int]) -> int:
    return 0


def split_items_by_provider(items: Iterable[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    items = list(items or ())
    return [(LEGACY_PROVIDER_KEY, items)] if items else []


def client_for_provider(provider: str, legacy_client: Any) -> Any:
    return legacy_client


def build_provider_kwargs_extras(items: list[dict[str, Any]], provider: str) -> dict[str, Any]:
    return {}
