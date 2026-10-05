"""渠道有效性报表(WO_DELIVERY_FLYWHEEL_CLOSURE §2.3)· 全站化 · 周期化 · 只读。

把 Codex 的「143 个已发布 URL 只有 14 个进了 AI 引用集合」这个一次性口径,固化成
**可复跑的按媒体位分级指标**。

🔴🔴 本模块存在的头号理由是**防一个会误杀的直觉**:
    2026-08-06 生产实测 —— 我方在 `cnblogs.com` 发了 45 条,被引 0 条。
    直觉结论是"博客园是低权重位,降权"。**但 `cnblogs.com` 在 AI 引用池里是第 3 大被引域
    (710 篇 / 1067 次引用)。** 按域降权会把一个 AI 确实在读的域砍掉。
    真正的零池域是 `mapp.to8to.com`(我方发 13 条 / 池内 0 篇)、`m.fang.com`、
    `jz.365jiankangw.cn` 这一类。
    → 所以每个域必须同时给两个数,缺一不可:
        `our_cited_rate`     我方在这个域发的东西被引的比例      → 我们做得怎么样
        `pool_presence`      这个域在 AI 引用池里的存在感        → AI 读不读这个域
      两者组合才有行动含义(见 `classify_channel` 的四象限)。

🔴 `pool_presence` 默认是**全行业口径**(`geo_research_articles` 不按客户行业切分)。
    传 `industry_like` 可做行业条件化;不传时口径偏乐观,报表会在 `caveats` 里明说。
    这一条是本模块最大的口径局限,不藏。

只读:本模块一行都不写。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("GEO-ChannelEffectiveness")

# 四象限阈值。刻意取整数而非"统计显著" —— 样本量普遍在个位数到几十条,
# 假装做显著性检验只会给出一个更好看但同样不可靠的数。
MIN_SAMPLE_FOR_VERDICT = 5      # 我方在该域发布数低于此 → 不下结论(样本太少)
POOL_PRESENT_MIN_ARTICLES = 20  # 池内文章数达到此值 → 认为 AI 确实在读这个域


def classify_channel(*, our_published: int, our_cited: int, pool_articles: int) -> dict[str, str]:
    """四象限判定。**任何一个象限都必须能出现**,否则这就是个恒定结论的假分级。"""
    if our_published < MIN_SAMPLE_FOR_VERDICT:
        return {
            "verdict": "insufficient_sample",
            "action": "继续观察",
            "why": f"我方在该域仅 {our_published} 条发布(阈值 {MIN_SAMPLE_FOR_VERDICT}),不下结论。",
        }
    pool_present = pool_articles >= POOL_PRESENT_MIN_ARTICLES
    if our_cited > 0 and pool_present:
        return {
            "verdict": "proven_effective",
            "action": "加码",
            "why": f"AI 在读这个域(池内 {pool_articles} 篇),我方也被引到了({our_cited} 条)。",
        }
    if our_cited > 0 and not pool_present:
        return {
            "verdict": "effective_thin_pool",
            "action": "小步加码",
            "why": f"我方被引 {our_cited} 条,但该域在池里只有 {pool_articles} 篇,样本薄,别重仓。",
        }
    if our_cited == 0 and pool_present:
        return {
            "verdict": "content_problem_not_channel",
            "action": "别降权 · 改内容",
            "why": (
                f"AI 确实在读这个域(池内 {pool_articles} 篇),但我方 {our_published} 条无一被引 —— "
                "问题在文章级(选题/文体/权威度),不在渠道。降权会砍错对象。"
            ),
        }
    return {
        "verdict": "channel_not_read",
        "action": "降权",
        "why": f"该域在 AI 引用池里几乎不存在(池内 {pool_articles} 篇),我方 {our_published} 条也无一被引。",
    }


_DOMAIN_SQL = """
    WITH pub AS (
        SELECT DISTINCT
               i.brand_id,
               lower(regexp_replace(regexp_replace(btrim(i.publish_url),
                     '^https?://(www\\.)?', '', 'i'), '/.*$', '')) AS domain,
               btrim(i.publish_url) AS raw_url,
               i.media_name
          FROM mhz_publish_order_items i
         WHERE i.status = 'published'
           AND btrim(COALESCE(i.publish_url, '')) <> ''
           AND i.published_at >= NOW() - make_interval(days => %s)
    ),
    cited AS (
        -- 🔴 被引口径分两列:主口径只数 body_proof=TRUE(能自证发的就是那一版);
        --    无正文证据的单独一列,给客户看得见,但不进分级判定。
        SELECT publish_domain AS domain,
               COUNT(DISTINCT publish_url_normalized) FILTER (WHERE body_proof) AS cited_urls,
               COUNT(*) FILTER (WHERE body_proof)                               AS citation_events,
               COUNT(*) FILTER (WHERE body_proof AND target_outcome = 'recommended')
                                                                                AS recommended_events,
               COUNT(DISTINCT publish_url_normalized) FILTER (WHERE NOT body_proof)
                                                                                AS unverified_cited_urls,
               COUNT(*) FILTER (WHERE NOT body_proof)                           AS unverified_citation_events
          FROM geo_article_citation_attributions
         WHERE tested_at >= NOW() - make_interval(days => %s)
         GROUP BY publish_domain
    ),
    pool AS (
        SELECT domain, COUNT(*) AS pool_articles
          FROM geo_research_articles
         GROUP BY domain
    )
    SELECT p.domain,
           COUNT(*)                                   AS our_published,
           COALESCE(MAX(c.cited_urls), 0)             AS our_cited,
           COALESCE(MAX(c.citation_events), 0)        AS citation_events,
           COALESCE(MAX(c.recommended_events), 0)     AS recommended_events,
           COALESCE(MAX(c.unverified_cited_urls), 0)  AS unverified_cited_urls,
           COALESCE(MAX(c.unverified_citation_events), 0) AS unverified_citation_events,
           COALESCE(MAX(pl.pool_articles), 0)         AS pool_articles,
           MIN(p.media_name)                          AS sample_media_name
      FROM pub p
      LEFT JOIN cited c ON c.domain = p.domain
      LEFT JOIN pool  pl ON pl.domain = p.domain
     GROUP BY p.domain
     ORDER BY COUNT(*) DESC
"""


def build_channel_report(*, since_days: int = 180) -> dict[str, Any]:
    """按媒体位聚合的被引率分级报表。只读,可任意复跑。"""
    from db.connection import get_db

    caveats = [
        "pool_articles 是**全行业**口径(geo_research_articles 不按客户行业切分),"
        "对综合门户偏高、对垂直站偏低 —— 用它判「AI 读不读这个域」够用,判「AI 在本行业读不读」不够。",
        "被引侧只统计归因账本里的行;账本从 2026-08-06 迁移 027 起才有数据,"
        "更早的被引无法追溯(历史监测行 lineage_status=legacy_unknown 且无问题快照,结构性缺失)。",
        f"我方发布数 < {MIN_SAMPLE_FOR_VERDICT} 的域一律记 insufficient_sample,不下结论。",
        "unverified_* 两列是「URL 精确匹配成立、但发布正文没有提交快照可自证」的被引 —— "
        "2026-05 前的发布普遍缺快照(覆盖率 07 月 35% → 08 月 97%,前向已自愈)。"
        "它们**不参与分级判定**,只用于让客户看见确有其事;绝不能当作写作版本的功劳。",
    ]

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(_DOMAIN_SQL, (int(since_days), int(since_days)))
            rows = [dict(r) for r in (cur.fetchall() or [])]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[channel-report] 查询失败: %s", str(exc)[:300])
        return {"available": False, "reason": str(exc)[:300], "channels": [], "caveats": caveats}

    channels = []
    for row in rows:
        our_published = int(row.get("our_published") or 0)
        our_cited = int(row.get("our_cited") or 0)
        pool_articles = int(row.get("pool_articles") or 0)
        verdict = classify_channel(
            our_published=our_published, our_cited=our_cited, pool_articles=pool_articles,
        )
        channels.append({
            "domain": row.get("domain"),
            "media_name": row.get("sample_media_name"),
            "our_published": our_published,
            "our_cited": our_cited,
            "our_cited_rate": round(our_cited / our_published, 4) if our_published else None,
            "citation_events": int(row.get("citation_events") or 0),
            "recommended_events": int(row.get("recommended_events") or 0),
            # 无正文证据的被引:单独展示,不参与分级(见 SQL 里的口径注释)
            "unverified_cited_urls": int(row.get("unverified_cited_urls") or 0),
            "unverified_citation_events": int(row.get("unverified_citation_events") or 0),
            "pool_articles": pool_articles,
            **verdict,
        })

    totals = {
        "domains": len(channels),
        "published_urls": sum(c["our_published"] for c in channels),
        "cited_urls": sum(c["our_cited"] for c in channels),
    }
    totals["cited_rate"] = (
        round(totals["cited_urls"] / totals["published_urls"], 4) if totals["published_urls"] else None
    )
    by_verdict: dict[str, int] = {}
    for c in channels:
        by_verdict[c["verdict"]] = by_verdict.get(c["verdict"], 0) + 1

    return {
        "available": True,
        "since_days": int(since_days),
        "totals": totals,
        "by_verdict": by_verdict,
        "channels": channels,
        "caveats": caveats,
    }
