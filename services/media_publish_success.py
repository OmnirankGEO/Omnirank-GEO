"""发布成功率 → 推荐权重（工单 T3）。

一个媒体"被 AI 引用强"和"我们能把稿子发进去"是两件事。实测（证据 §5，
`mhz_publish_order_items` 全量）：

* 搜狐系 67 单 → published 9 / rejected 35 = **拒稿率 52.2%**
* 网易系 26 单 → published 14 = 53.8% 成功
* 其他 283 单 → rejected 37 = 13.1%

推荐分只看被引强度、不看发得进去，代理就会一直下注在高拒稿的位置上。
所以推荐分乘一个成功率因子。

口径（工单指定）：``published / (orders − cancelled − withdrawn)``。
cancelled/withdrawn 是我方主动撤单，不是媒体拒稿，不该算媒体头上。

**样本 <5 单不惩罚**：新媒体没有历史不等于差，冷启动惩罚会让新供给永远起不来。
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Final

logger = logging.getLogger("GEO-MediaPublishSuccess")

MEDIA_SUCCESS_VERSION: Final = "media-publish-success-v1.0"

DEFAULT_WINDOW_DAYS: Final = 180
#: Below this many settled orders we do not judge a medium at all.
MIN_SAMPLE_FOR_PENALTY: Final = 5
#: Factor floor — a bad medium is down-weighted, never silently removed
#: (SSOT §11 O1: new checks are advisory, hard blocks stay limited).
MIN_FACTOR: Final = 0.55
_CACHE_TTL_SECONDS: Final = 900


@dataclass(frozen=True)
class MediaSuccessStat:
    media_name: str
    orders: int
    settled: int
    published: int
    rejected: int
    success_rate: float | None
    factor: float
    sample_sufficient: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "media_name": self.media_name,
            "orders": self.orders,
            "settled": self.settled,
            "published": self.published,
            "rejected": self.rejected,
            "success_rate": (round(self.success_rate, 4) if self.success_rate is not None else None),
            "success_pct": (round(self.success_rate * 100, 1) if self.success_rate is not None else None),
            "factor": round(self.factor, 4),
            "sample_sufficient": self.sample_sufficient,
            "version": MEDIA_SUCCESS_VERSION,
        }


NEUTRAL_STAT: Final = MediaSuccessStat(
    media_name="", orders=0, settled=0, published=0, rejected=0,
    success_rate=None, factor=1.0, sample_sufficient=False,
)


def _factor_from_rate(rate: float) -> float:
    """Map a success rate onto a bounded multiplier.

    1.0 at/above the observed global rate (56.7%), tapering to ``MIN_FACTOR``
    at 0%.  Above-average media get a small bonus, capped at 1.15 so success
    rate never overrides the citation signal it multiplies.
    """
    value = max(0.0, min(1.0, float(rate)))
    if value >= 0.567:
        return min(1.15, 1.0 + (value - 0.567) * 0.35)
    return max(MIN_FACTOR, MIN_FACTOR + (value / 0.567) * (1.0 - MIN_FACTOR))


_cache_lock = threading.Lock()
_cache: dict[int, tuple[float, dict[str, MediaSuccessStat]]] = {}


def _fetch_success_rows(window_days: int) -> list[dict[str, Any]]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT media_name,
                   COUNT(*)                                                   AS orders,
                   COUNT(*) FILTER (WHERE status = 'published')               AS published,
                   COUNT(*) FILTER (WHERE status = 'rejected')                AS rejected,
                   COUNT(*) FILTER (WHERE status IN ('cancelled','withdrawn')) AS retracted
              FROM mhz_publish_order_items
             WHERE created_at >= NOW() - make_interval(days => %s)
               AND COALESCE(media_name, '') <> ''
             GROUP BY media_name
            """,
            (window_days,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def build_media_success_table(window_days: int | None = None) -> dict[str, MediaSuccessStat]:
    """media_name -> stat.  Any DB failure degrades to an empty (neutral) table."""
    window = max(7, min(720, int(window_days or DEFAULT_WINDOW_DAYS)))
    try:
        rows = _fetch_success_rows(window)
    except Exception as exc:
        logger.warning("[media-success] 成功率聚合不可用: %s", exc)
        return {}

    table: dict[str, MediaSuccessStat] = {}
    for row in rows:
        name = str(row.get("media_name") or "").strip()
        if not name:
            continue
        orders = int(row.get("orders") or 0)
        retracted = int(row.get("retracted") or 0)
        published = int(row.get("published") or 0)
        settled = max(0, orders - retracted)
        sufficient = settled >= MIN_SAMPLE_FOR_PENALTY
        rate = (published / settled) if settled else None
        table[name] = MediaSuccessStat(
            media_name=name,
            orders=orders,
            settled=settled,
            published=published,
            rejected=int(row.get("rejected") or 0),
            success_rate=rate,
            # 样本不足 → 因子 1.0（不惩罚也不奖励）
            factor=(_factor_from_rate(rate) if (sufficient and rate is not None) else 1.0),
            sample_sufficient=sufficient,
        )
    return table


def get_media_success_table(
    window_days: int | None = None, *, force_refresh: bool = False
) -> dict[str, MediaSuccessStat]:
    key = max(7, min(720, int(window_days or DEFAULT_WINDOW_DAYS)))
    now = time.time()
    if not force_refresh:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and now - hit[0] < _CACHE_TTL_SECONDS:
                return hit[1]
    table = build_media_success_table(key)
    with _cache_lock:
        _cache[key] = (now, table)
    return table


def reset_media_success_cache() -> None:
    with _cache_lock:
        _cache.clear()


def resolve_media_success(
    table: dict[str, MediaSuccessStat] | None, media_name: str
) -> MediaSuccessStat:
    """Exact name first, then the best-sampled sibling channel of the same domain族.

    "搜狐网新闻（官方）" and "搜狐网娱乐" are different rows in the inventory but
    the same submission pipeline; when the exact row has no history we fall back
    to the family so a known-52%-reject family is not scored as unknown.
    """
    if not table:
        return NEUTRAL_STAT
    name = str(media_name or "").strip()
    if not name:
        return NEUTRAL_STAT
    exact = table.get(name)
    if exact is not None and exact.sample_sufficient:
        return exact

    from services.citation_domain_weights import family_for_domain

    family = family_for_domain("", name)
    if family is None:
        return exact or NEUTRAL_STAT

    best: MediaSuccessStat | None = None
    for candidate_name, stat in table.items():
        if not stat.sample_sufficient:
            continue
        sibling = family_for_domain("", candidate_name)
        if sibling is None or sibling.key != family.key:
            continue
        if best is None or stat.settled > best.settled:
            best = stat
    return best or exact or NEUTRAL_STAT


def build_publish_to_citation_conversion(
    *, window_days: int = 365, limit: int = 50
) -> dict[str, Any]:
    """[T5] 每媒体「发布数 / 被引数 / 被引率」—— 垂类去留用数据自证。

    连接方式是**我们发出去的那条 URL 本身**（``publish_url`` = 飞轮
    ``geo_research_articles.url``），不是域级近似：域级会把别人在同域发的文章
    算成我们的战果。北极星现状（证据 §8）是全部发布史仅 1 条 URL 拿到 1 次被引，
    这张表就是要把这件事摆在台面上，而不是让"投了很多"糊过去。
    """
    from db.connection import get_connection

    window = max(7, min(1095, int(window_days or 365)))
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH pub AS (
                SELECT media_name, publish_url
                  FROM mhz_publish_order_items
                 WHERE status = 'published'
                   AND COALESCE(publish_url, '') <> ''
                   AND created_at >= NOW() - make_interval(days => %s)
            )
            SELECT p.media_name                                              AS media_name,
                   COUNT(*)                                                  AS published,
                   COUNT(a.id)                                               AS matched_in_flywheel,
                   COUNT(*) FILTER (WHERE COALESCE(a.total_citation_count, 0) > 0) AS cited_articles,
                   COALESCE(SUM(a.total_citation_count), 0)                  AS citations
              FROM pub p
              LEFT JOIN geo_research_articles a ON a.url = p.publish_url
             GROUP BY p.media_name
             ORDER BY citations DESC, published DESC
             LIMIT %s
            """,
            (window, max(1, min(200, int(limit or 50)))),
        )
        rows = [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        logger.warning("[publish-conversion] 转化率聚合不可用: %s", exc)
        return {
            "window_days": window, "degraded_reason": str(exc),
            "total_published": 0, "total_cited": 0, "overall_citation_rate": None, "media": [],
        }
    finally:
        conn.close()

    media = []
    total_published = total_cited = total_citations = 0
    for row in rows:
        published = int(row.get("published") or 0)
        cited = int(row.get("cited_articles") or 0)
        total_published += published
        total_cited += cited
        total_citations += int(row.get("citations") or 0)
        media.append({
            "media_name": row.get("media_name"),
            "published": published,
            "matched_in_flywheel": int(row.get("matched_in_flywheel") or 0),
            "cited_articles": cited,
            "citations": int(row.get("citations") or 0),
            "citation_rate": (round(cited / published, 4) if published else None),
            "citation_rate_pct": (round(cited / published * 100, 1) if published else None),
        })
    return {
        "window_days": window,
        "degraded_reason": "",
        "total_published": total_published,
        "total_cited": total_cited,
        "total_citations": total_citations,
        "overall_citation_rate": (
            round(total_cited / total_published, 4) if total_published else None
        ),
        "media": media,
        "version": MEDIA_SUCCESS_VERSION,
    }


def best_channel_within_family(
    table: dict[str, MediaSuccessStat] | None, candidate_names: list[str]
) -> str:
    """同域族内自动落成功率最高子频道（工单 T3 后半句）。"""
    if not table or not candidate_names:
        return candidate_names[0] if candidate_names else ""
    scored: list[tuple[float, int, str]] = []
    for name in candidate_names:
        stat = table.get(str(name or "").strip())
        if stat is None or not stat.sample_sufficient or stat.success_rate is None:
            continue
        scored.append((stat.success_rate, stat.settled, name))
    if not scored:
        return candidate_names[0]
    scored.sort(key=lambda kv: (-kv[0], -kv[1]))
    return scored[0][2]
