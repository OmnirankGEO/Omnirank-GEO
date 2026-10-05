"""存量榜单文「留 / 替」清单（只读）· 2026-07-28

工单第二部分：Owner 问"排名文存量重新生成还是改旧的？"

裁决（Review-CTO）：**重新生成，不改旧文**。依据：
  1. 文章真实成本 ¥0.05-1/篇，重生成近乎免费；人工改旧文的审核成本远高于此；
  2. 旧榜单文是 WP9 前口径：自创评分体系（现 H0 红线）+ 3500 字短文（现 ≥12000），
     改造等于重写；
  3. **飞轮数据当判官**：被引过的旧文是正在产生引用的资产，留着别动；
     零被引的旧文不值得改，新单直接按新口径生成。

本脚本只做第 3 条：列出存量榜单文的被引数，给运营一张留/替清单。

* **纯只读**：不 UPDATE、不 DELETE、不生成、不扣费。
* 血缘与 `services/writing_effectiveness_report` 同源：
  `mhz_publish_order_items.order_id → mhz_publish_orders.article_id`
  （历史缺陷是查 `items.article_id`，该列只存在于测试 fixture）。
* 排除 tombstone：`domain_tier <> 'blacklist'` 且 `clean_status = 'cleaned'`。

用法：
    DATABASE_URL=... python scripts/wsu_legacy_ranking_citation_audit_2026_07_28.py
    DATABASE_URL=... python scripts/wsu_legacy_ranking_citation_audit_2026_07_28.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 榜单族的全部历史 style_code（含已退役写法与中文别名落库的情况）
RANKING_STYLE_CODES = (
    "ranking_v2",
    "authority_ranking",
    "recommendation_review",
    "comparison_review",
    "ranking_v9",
    "premium_ranking",
)

KEEP = "留（正在产生引用，别动）"
REPLACE = "替（零被引，新单按新口径重生成）"
WATCH = "观察（有被引但已过窗口）"

_SQL = """
WITH published AS (
    SELECT DISTINCT o.article_id         AS article_id,
                    btrim(i.publish_url) AS url
      FROM mhz_publish_order_items i
      JOIN mhz_publish_orders o ON o.id = i.order_id
     WHERE i.status = 'published'
       AND btrim(COALESCE(i.publish_url, '')) <> ''
       AND o.article_id IS NOT NULL
),
matched AS (
    SELECT p.article_id, p.url, gra.id AS research_id, gra.domain
      FROM published p
      LEFT JOIN geo_research_articles gra
             ON gra.url = p.url
            AND gra.domain_tier <> 'blacklist'
            AND gra.clean_status = 'cleaned'
)
SELECT a.id                                            AS article_id,
       a.title                                         AS title,
       COALESCE(NULLIF(a.style_code, ''), NULLIF(a.style, '')) AS style_code,
       a.created_at                                    AS created_at,
       length(regexp_replace(COALESCE(a.content, ''), '\\s', '', 'g')) AS effective_chars,
       m.url                                           AS publish_url,
       m.domain                                        AS domain,
       COALESCE(c.total_citations, 0)                  AS total_citations,
       COALESCE(c.recent_citations, 0)                 AS recent_citations,
       c.avg_rank                                      AS avg_rank_in_response,
       c.engines                                       AS engines
  FROM articles a
  JOIN matched m ON m.article_id = a.id
  LEFT JOIN LATERAL (
        SELECT COUNT(*)                                       AS total_citations,
               COUNT(*) FILTER (
                   WHERE x.cited_at >= NOW() - make_interval(days => %(window)s)
               )                                              AS recent_citations,
               AVG(NULLIF(x.rank_in_response, 0))             AS avg_rank,
               string_agg(DISTINCT x.platform, ',' ORDER BY x.platform) AS engines
          FROM geo_research_article_citations x
         WHERE x.article_id = m.research_id
       ) c ON TRUE
 WHERE COALESCE(NULLIF(a.style_code, ''), NULLIF(a.style, '')) = ANY(%(styles)s)
 ORDER BY COALESCE(c.total_citations, 0) DESC, a.id DESC
"""


def classify(row: dict[str, Any]) -> str:
    total = int(row.get("total_citations") or 0)
    recent = int(row.get("recent_citations") or 0)
    if recent > 0:
        return KEEP
    if total > 0:
        return WATCH
    return REPLACE


def fetch(window_days: int) -> list[dict[str, Any]]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(_SQL, {"window": int(window_days), "styles": list(RANKING_STYLE_CODES)})
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def render(rows: list[dict[str, Any]], window_days: int) -> str:
    buckets: dict[str, list[dict[str, Any]]] = {KEEP: [], WATCH: [], REPLACE: []}
    for row in rows:
        buckets[classify(row)].append(row)

    lines = [
        f"# 存量榜单文 留/替 清单（只读 · 观测窗口 {window_days} 天）",
        "",
        f"命中榜单族 style_code 的已发布文章：**{len(rows)}** 篇",
        f"- {KEEP}：{len(buckets[KEEP])} 篇",
        f"- {WATCH}：{len(buckets[WATCH])} 篇",
        f"- {REPLACE}：{len(buckets[REPLACE])} 篇",
        "",
        "> 裁决：重新生成，不改旧文。被引过的留着别动；零被引的不值得改，",
        "> 新单直接按新口径（榜单族 ≥12000 字 · 位次须有可核验依据 · 禁自创评分）生成。",
        "",
        "| 处置 | article_id | 字数 | 累计被引 | 窗口内被引 | 平均引用位次 | 引擎 | 域 | 标题 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label in (KEEP, WATCH, REPLACE):
        for row in buckets[label]:
            avg_rank = row.get("avg_rank_in_response")
            lines.append(
                "| {label} | {aid} | {chars} | {total} | {recent} | {rank} | {eng} | {dom} | {title} |".format(
                    label=label.split("（")[0],
                    aid=row.get("article_id"),
                    chars=row.get("effective_chars") or 0,
                    total=row.get("total_citations") or 0,
                    recent=row.get("recent_citations") or 0,
                    rank=f"{float(avg_rank):.2f}" if avg_rank is not None else "-",
                    eng=row.get("engines") or "-",
                    dom=row.get("domain") or "(未匹配到调研库)",
                    title=str(row.get("title") or "")[:40].replace("|", "｜"),
                )
            )
    if not rows:
        lines.append("")
        lines.append("（当前库里没有命中榜单族 style_code 的已发布文章。）")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="存量榜单文被引数只读审计")
    parser.add_argument("--window-days", type=int, default=90)
    parser.add_argument("--json", dest="json_path", default="")
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        raise SystemExit(
            "[BLOCKED] 需要 DATABASE_URL 指向含存量文章的库（生产只读副本或 staging）。\n"
            "本脚本纯只读，不写任何一行。"
        )

    rows = fetch(args.window_days)
    print(render(rows, args.window_days))
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(
                [{**r, "disposition": classify(r)} for r in rows],
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
            newline="\n",
        )
        print(f"\n[saved] {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
