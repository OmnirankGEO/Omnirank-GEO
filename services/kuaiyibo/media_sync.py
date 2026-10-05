"""第二发布渠道目录同步(开源版空壳):没有接入,不拉任何目录。

``normalize_source_domain`` 是通用的「案例 URL → 发布域」归一,别处也用,照常保留。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SyncResult:
    total_seen: int = 0
    article_upserted: int = 0
    wemedia_upserted: int = 0
    skipped: int = 0
    deactivated: int = 0
    deactivate_blocked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"共{self.total_seen}条: 软文{self.article_upserted} 自媒体{self.wemedia_upserted} "
                f"跳过{self.skipped} 下架{self.deactivated} 错误{len(self.errors)}")


def normalize_source_domain(case_url: Any) -> str:
    """案例 URL -> 归一化发布域。"""
    try:
        from services.citation_domain_weights import normalize_domain

        return normalize_domain(case_url)[:160]
    except Exception:
        return ""


def sync_from_api(*, max_pages: int | None = None, deactivate_missing: bool = True) -> SyncResult:
    return SyncResult()
