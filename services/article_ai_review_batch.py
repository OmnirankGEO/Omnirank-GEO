"""存量 AI 审核批跑的**选取口径 + 批处理**(包① §3D)。

工单:docs/AI-CONTEXT/WORKORDER_AI_REVIEW_REPLACES_HUMAN_2026-08-01.md §3D

节奏(已裁定):先 **120 篇**试跑验提示分布与误报率 → 当天铺 **1243**。同包两步,不加交付轮次。

🔴 三条口径,每条都是踩过的坑:

1. **`pending_human_review` 落在机审列 `article_review_status`**,不在
   `article_human_review_status`(人审列该值 **0 行** —— 2026-08-01 生产只读实测)。
   取错列 → 试跑集合直接为空,而脚本会"成功跑完 0 篇"**不报错**。
   `PILOT_SQL` 因此把列名写死并配 `count_scope()` 让调用方先看数再跑。

2. **绝不重跑机审**。`refresh_article_review()` 会把
   `article_human_review_status / _by / _at / _reason` 四个字段**一并置 NULL** ——
   在 1243 篇上跑一遍等于**抹掉全部人工签发**(生产实测 22 篇 human='approved')。
   AI 评估只读 `content`,压根不需要重跑机审。
   §3D 说的"误报抽查前先重跑机审"是**抽查那 10 篇**的动作,不是批跑动作,
   而且那 10 篇也必须先排除已签发的。见 `SAMPLE_SAFE_TO_REFRESH_SQL`。

3. **幂等 by 正文 hash**(落在 `review_article`):同 hash 已有结论就跳过,
   不重复调模型、不重复落行。批跑可以随时中断重跑。
"""
from __future__ import annotations

import logging
from typing import Any, Final

logger = logging.getLogger("GEO-ArticleAIReviewBatch")

#: 🔴 试跑集合(120 篇):机审列 = pending_human_review 且**尚未人工签发**。
#: `article_human_review_status` 只用来排除已签发的,不用来选 pending —— 见模块 §1。
PILOT_SQL: Final = """
    SELECT a.id, a.title, a.content
      FROM articles a
     WHERE a.article_review_status = 'pending_human_review'
       AND COALESCE(a.article_human_review_status, '') = ''
     ORDER BY a.id
"""

#: 全量集合(1243 篇)= 1110 legacy + 120 未签发 + 13 blocked。
#: rewrite_required(3 篇)与 approved 不在队列口径内(工单 §4 的 1243 就是这三类之和)。
FULL_SQL: Final = """
    SELECT a.id, a.title, a.content
      FROM articles a
     WHERE (
             a.article_review_status IN ('legacy_unreviewed', 'blocked')
             OR (
                  a.article_review_status = 'pending_human_review'
                  AND COALESCE(a.article_human_review_status, '') = ''
             )
           )
     ORDER BY a.id
"""

#: 🔴 误报抽查前要重跑机审的候选:**必须排除已人工签发的**。
#: refresh_article_review 会抹掉签发四字段,对已签发文章跑它 = 毁审计。
SAMPLE_SAFE_TO_REFRESH_SQL: Final = """
    SELECT a.id
      FROM articles a
     WHERE a.id = ANY(%s)
       AND COALESCE(a.article_human_review_status, '') = ''
"""

SCOPES: Final = {"pilot": PILOT_SQL, "full": FULL_SQL}


def count_scope(cursor, scope: str) -> int:
    """先看数再跑。**0 篇要当异常看**,不是"没活干"——多半是列名取错(见模块 §1)。"""
    sql = SCOPES[scope]
    cursor.execute(f"SELECT COUNT(*) AS n FROM ({sql}) t")
    return int(cursor.fetchone()["n"])


def fetch_scope(cursor, scope: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    sql = SCOPES[scope]
    if limit is not None and int(limit) > 0:
        sql = f"{sql} LIMIT {int(limit)}"
    cursor.execute(sql)
    return [dict(r) for r in cursor.fetchall()]


def run_batch(cursor, scope: str, *, limit: int | None = None,
              dry_run: bool = False) -> dict[str, Any]:
    """跑一批,返回分布统计。

    返回 {total, reused, evaluated, levels: {L1/L2/L3/not_checked: n},
          l3_ratio, vague_rejected}。
    `l3_ratio` 是裁定里"L3 占比 < 20% 当天铺 1243"的判据 —— 由调用方决定要不要继续,
    脚本不替人做这个决定。
    """
    from services.article_ai_review import review_article

    rows = fetch_scope(cursor, scope, limit=limit)
    levels: dict[str, int] = {}
    reused = 0
    evaluated = 0

    for row in rows:
        if dry_run:
            # dry-run 只统计集合规模与可命中的既有结论,绝不调模型、绝不写库
            levels["dry_run"] = levels.get("dry_run", 0) + 1
            continue
        out = review_article(
            cursor, int(row["id"]),
            title=str(row.get("title") or ""),
            content=str(row.get("content") or ""),
        )
        levels[out["level"]] = levels.get(out["level"], 0) + 1
        if out["reused"]:
            reused += 1
        else:
            evaluated += 1

    total = len(rows)
    l3 = levels.get("L3", 0)
    graded = sum(levels.get(k, 0) for k in ("L1", "L2", "L3"))
    return {
        "scope": scope,
        "total": total,
        "reused": reused,
        "evaluated": evaluated,
        "levels": levels,
        # 🔴 分母用**已分档数**而不是 total:not_checked 不该稀释 L3 占比,
        # 否则 AI 大面积失败反而会让"L3 占比"变好看,把判据骗过去。
        "l3_ratio": (l3 / graded) if graded else None,
    }
