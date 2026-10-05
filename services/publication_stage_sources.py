"""WP7 · 六阶段投影的 SQL 取数层(唯一带 quote predicate 的地方)。

投影核心 `publication_stage_projection.project_stages` 是纯函数;本模块负责把
四条发布链 + 制作单元 + 监测行按 **quote** 取出来喂给它。

## 四条发布链是既存事实,不是本期新建

| 链 | 表 | quote 怎么定位 |
|---|---|---|
| 人工登记 | `media_publications` | 自带 `quote_id` 列 |
| 快易播/媒介盒子代发 | `mhz_publish_order_items` + `mhz_publish_orders` | `source_geo_post_id → geo_douyin_posts.quote_id`,回落 `orders.article_id → articles.quote_id` |
| 旧代发 | `publish_order_items` + `publish_orders` | `orders.article_id → articles.quote_id` |
| 浏览器插件 | `publish_records` | `article_id → articles.quote_id` |

🔴 `articles.quote_id` 可能为 NULL(历史行),此时回落 `topics.quote_id`。这不是
   猜绑 —— topic 与 quote 是建库时就写死的外键关系,不是按品牌+时间推的。

## 为什么 quote predicate 要写在 SQL 里,而不是取回来再过滤

取回来再过滤在**行数**上等价,在**责任**上不等价:一旦有人为了性能把
"取回来"那步换成分页/采样,过滤就静默失效了。写在 SQL 里,删掉它是一次可见的 diff,
反向变异测试盯得住。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Final, Optional

from services.monitoring_identity_review import aggregate_eligible_sql
from services.publication_stage_projection import (
    STAGES,
    ProjectionUnavailable,
    project_stages,
    source_versions,
)
from tools.pricing_bands import normalize_article_capacity

#: 取数层的 quote predicate 字面量。反向变异测试用它做结构锚:
#: 任一发布链的 SQL 里少了这一段,`assert_quote_predicates_present` 必红。
QUOTE_PREDICATE_TOKEN: Final = "%(quote_id)s"

#: 每条发布链在 UNION 里的来源标签。计数不看它,但 lineage / 排障看它。
PUBLICATION_SOURCES: Final[tuple[str, ...]] = (
    "media_publications",
    "mhz_publish_order_items",
    "publish_order_items",
    "publish_records",
)


# ═══════════════════════════════════════════════════════════════════
# 浏览器自报(`publish_records`)的两处谓词 —— **单一出处**
# ═══════════════════════════════════════════════════════════════════
#
# 🔴 [第 7 棒 · R7 (5)] 这两段原本各自内联写在下面两个 SQL 里。抽出来是因为
#    文章自报包要改的正是它们,而"同一个判断写在两处"这件事本仓已经付过学费:
#    改一处、另一处静默不跟,而且**没有判据看得见**。
#
# 🔴 **跨包耦合声明**(并车门,见交付单 §4):文章自报包会给 `publish_records`
#    加一列**核实位** `public_url_verification_state`(实核列名,不是 `verification_state`),
#    届时这两个常量各改一行:
#      · occurrence 臂  → 追加 `AND rx.public_url_verification_state = 'verified'`;
#      · body proof     → 用核实位取代**来源位** `public_url_reported_explicitly`
#                         (后者只说明"URL 是显式报的",不说明"我们核实过")。
#    ⚠️ 本包**不能**先改 —— 这是实跑出来的,不是推断:改了一版之后图文全量
#    `test_wp7_projection_pg16.py` 整片 **20 条红**(该列由文章自报包的
#    `scripts/migration_publish_records_url_verification_2026_08_19.sql` 引入,
#    本树没有)。而 `published_occurrence_predicate` 有三个调用点在
#    `db/monitoring_db.py`(监测入池=扣费路径)。
#    所以这一行改法**属于两包的并车提交**:文章包里没有本文件,本包里没有那条迁移。
#    判据见 `tests/geo_image_note_2026_08_17/test_r7_self_report_seam.py`。

#: 自报 body proof —— 「这条自报到底有没有正文层面的凭据」。
#: [并车 2026-08-20 · 36 班] 已按 test_the_exact_one_line_fix_is_recorded_verbatim
#: 的逐字改法把**来源位**换成**核实位**(迁移 039 已随文章包并入本树)。
SELF_REPORT_BODY_PROOF_SQL: Final = """(r.submitted_content_snapshot_hash IS NOT NULL
        AND r.public_url_verification_state = 'verified')"""

#: 自报 occurrence 臂 —— 「这张报价有没有发生过浏览器自发」。
#: `{q}` 由 `published_occurrence_predicate` 绑到调用方的 quote 表达式。
SELF_REPORT_OCCURRENCE_SQL: Final = """EXISTS (
        SELECT 1 FROM publish_records rx
          LEFT JOIN articles rax ON rax.id = rx.article_id
          LEFT JOIN topics rtx   ON rtx.id = rax.topic_id
         WHERE COALESCE(rax.quote_id, rtx.quote_id) = {q}
           AND rx.status = 'success'
           AND rx.public_url_verification_state = 'verified'
    )"""

_CAPACITY_SQL = """
SELECT COUNT(*) AS keyword_rows,
       SUM(COALESCE(ck.required_articles, 0)) AS required_articles
  FROM confirmed_keywords ck
 WHERE ck.quote_id = %(quote_id)s
"""

_SLOT_CAPACITY_SQL = """
SELECT COUNT(*) AS slot_rows
  FROM geo_article_delivery_slots s
 WHERE s.quote_id = %(quote_id)s
"""

_PRODUCED_SQL = """
SELECT p.id                       AS geo_post_id,
       p.delivery_slot_key        AS delivery_slot_key,
       NULL::int                  AS article_id,
       p.created_at::timestamptz  AS created_at,
       p.updated_at::timestamptz  AS ready_at
  FROM geo_douyin_posts p
 WHERE p.quote_id = %(quote_id)s
   AND p.deleted_at IS NULL
   AND COALESCE(p.counts_toward_contract, TRUE) IS TRUE
   AND p.status = 'ready'
UNION ALL
SELECT NULL::bigint, NULL::uuid, a.id,
       a.created_at::timestamptz, a.updated_at::timestamptz
  FROM articles a
  LEFT JOIN topics t ON t.id = a.topic_id
 WHERE COALESCE(a.quote_id, t.quote_id) = %(quote_id)s
   AND NULLIF(BTRIM(COALESCE(a.content, '')), '') IS NOT NULL
"""

#: 四条发布链的统一投影行。列顺序在四段 UNION 里必须逐字一致。
_ATTEMPTS_SQL = """
SELECT %(quote_id)s::int          AS quote_id,
       'media_publications'::text AS publication_source,
       m.id::bigint               AS publication_source_id,
       m.delivery_slot_key        AS delivery_slot_key,
       m.source_geo_post_id       AS geo_post_id,
       m.article_id               AS article_id,
       'manual_confirmed'::text   AS status,
       m.created_at::timestamptz  AS submitted_at,
       COALESCE(m.published_at_tz, m.publish_timestamp::timestamptz,
                m.publish_date::timestamptz, m.created_at::timestamptz) AS published_at,
       m.retracted_at             AS retracted_at,
       NULL::text                 AS availability,
       m.replaced_by_source       AS replaced_by_source,
       m.replaced_by_source_id    AS replaced_by_source_id,
       COALESCE(m.normalized_url, m.platform_url) AS normalized_url,
       COALESCE(m.body_proof, FALSE) AS body_proof
  FROM media_publications m
 WHERE m.quote_id = %(quote_id)s

UNION ALL

SELECT %(quote_id)s::int, 'mhz_publish_order_items'::text, i.id::bigint,
       gp.delivery_slot_key, i.source_geo_post_id,
       COALESCE(o.article_id, ao.id),
       i.status, i.submitted_at::timestamptz,
       COALESCE(i.published_at_tz, i.published_at::timestamptz),
       i.retracted_at, i.availability, i.replaced_by_source, i.replaced_by_source_id,
       i.publish_url,
       (i.submitted_content_snapshot_hash IS NOT NULL
        AND i.submitted_content_snapshot_at IS NOT NULL)
  FROM mhz_publish_order_items i
  LEFT JOIN mhz_publish_orders o     ON o.id = i.order_id
  LEFT JOIN geo_douyin_posts gp      ON gp.id = i.source_geo_post_id
  LEFT JOIN articles ao              ON ao.id = o.article_id
  LEFT JOIN topics ato               ON ato.id = ao.topic_id
 WHERE COALESCE(gp.quote_id, ao.quote_id, ato.quote_id) = %(quote_id)s

UNION ALL

SELECT %(quote_id)s::int, 'publish_order_items'::text, i.id::bigint,
       NULL::uuid, NULL::bigint, o.article_id,
       i.status, i.submitted_at::timestamptz, i.published_at::timestamptz,
       NULL::timestamptz, NULL::text, NULL::text, NULL::text,
       i.publish_url, FALSE
  FROM publish_order_items i
  JOIN publish_orders o  ON o.id = i.order_id
  LEFT JOIN articles a   ON a.id = o.article_id
  LEFT JOIN topics t     ON t.id = a.topic_id
 WHERE COALESCE(a.quote_id, t.quote_id) = %(quote_id)s

UNION ALL

SELECT %(quote_id)s::int, 'publish_records'::text, r.id::bigint,
       NULL::uuid, NULL::bigint, r.article_id,
       r.status, r.created_at::timestamptz, r.created_at::timestamptz,
       NULL::timestamptz, NULL::text, NULL::text, NULL::text,
       r.public_url,
       """ + SELF_REPORT_BODY_PROOF_SQL + """
  FROM publish_records r
  LEFT JOIN articles a ON a.id = r.article_id
  LEFT JOIN topics t   ON t.id = a.topic_id
 WHERE COALESCE(a.quote_id, t.quote_id) = %(quote_id)s
"""

_MONITORING_SQL = """
SELECT mr.id, mt.quote_id,
       -- 🔴 `monitoring_results.tested_at` 是 `timestamp WITHOUT time zone`,
       --    存的是**库时区的本地时间**(生产 DB TimeZone=Asia/Shanghai)。
       --    直接取回 Python 会被当成 UTC → 与已经是 timestamptz 的 published_at
       --    比较时凭空差 8 小时:发布当天的监测会被判成"发布之前",coverage 与
       --    严格归因整片丢。WP5 已经为同一个坑付过一次学费(capacity_date)。
       --    在 SQL 里 ::timestamptz 由 PG 按库时区转换,才是同一把尺子。
       mr.tested_at::timestamptz AS tested_at, mr.search_citations,
       mr.confirmed_keyword_id, mr.keyword_id, mr.provider, mr.platform,
       mr.lineage_status, mr.sent_question_snapshot
  FROM monitoring_results mr
  JOIN monitoring_tasks mt ON mt.id = mr.task_id
 WHERE mt.quote_id = %(quote_id)s
   AND mr.tested_at <= %(cutoff)s
   AND {eligible}
"""

_KEYWORD_IDS_SQL = """
SELECT ck.id FROM confirmed_keywords ck WHERE ck.quote_id = %(quote_id)s
"""

#: 「这张报价**发生过**发布吗」的 SQL 谓词,给批量扫描的调度器用
#: (逐 quote 起一次投影在几百张报价上不现实)。
#:
#: 🔴 为什么不能继续用 `articles.first_published_at IS NOT NULL`:
#:    那一列只有人工登记链回写。生产实测四条链里有三条不写它 ——
#:    只走代发/插件发布的客户,在这个判据眼里"从没发过文",于是监测调度
#:    直接把他跳过。客户付了钱、发了稿、监测不跑,而且**不会报错**。
#:
#: 🔴 用的是 **occurrence** 不是 active:撤稿不该让已经在跑的监测凭空停掉
#:    (规格 03 §10:「撤稿仍保留 occurrence,是否停止监测不由本功能擅改」)。
PUBLISHED_OCCURRENCE_EXISTS_SQL: Final = """
    EXISTS (
        SELECT 1 FROM media_publications mpx WHERE mpx.quote_id = {q}
    ) OR EXISTS (
        SELECT 1 FROM mhz_publish_order_items ix
          LEFT JOIN mhz_publish_orders ox ON ox.id = ix.order_id
          LEFT JOIN geo_douyin_posts gpx  ON gpx.id = ix.source_geo_post_id
          LEFT JOIN articles ax           ON ax.id = ox.article_id
          LEFT JOIN topics tx             ON tx.id = ax.topic_id
         WHERE COALESCE(gpx.quote_id, ax.quote_id, tx.quote_id) = {q}
           AND (ix.submitted_at IS NOT NULL OR ix.published_at IS NOT NULL)
    ) OR EXISTS (
        SELECT 1 FROM publish_order_items px
          JOIN publish_orders pox ON pox.id = px.order_id
          LEFT JOIN articles pax  ON pax.id = pox.article_id
          LEFT JOIN topics ptx    ON ptx.id = pax.topic_id
         WHERE COALESCE(pax.quote_id, ptx.quote_id) = {q}
           AND (px.submitted_at IS NOT NULL OR px.published_at IS NOT NULL)
    ) OR """ + SELF_REPORT_OCCURRENCE_SQL + """
"""


def published_occurrence_predicate(quote_ref: str) -> str:
    """把上面的谓词绑到调用方的 quote 表达式上(例如 ``q.id``)。"""
    if not quote_ref or "%" in quote_ref:
        raise ValueError("quote_ref 必须是列引用(如 q.id),不接受占位符")
    return PUBLISHED_OCCURRENCE_EXISTS_SQL.format(q=quote_ref)


def _rows(cur, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    cur.execute(sql, params)
    return [dict(row) for row in (cur.fetchall() or [])]


def load_allocated_capacity(cur, quote_id: int) -> tuple[Optional[int], str]:
    """(容量, 来源)。**容量为 None 与容量为 0 是两件事**。

    · `None` = 这张报价还没签发过任何词 → 门户显示"未分配",不是"分配了 0 篇";
    · `0`   = 签发了,但图文/文章篇数确实是 0(覆盖词 `is_core=False` 的合法形态)。

    WP2 已经为这个区别付过一次学费(`channel_allocated_capacity` 那处),
    这里沿用同一口径。
    """
    slot_rows = _rows(cur, _SLOT_CAPACITY_SQL, {"quote_id": quote_id})
    slots = int((slot_rows[0] if slot_rows else {}).get("slot_rows") or 0)
    if slots:
        return slots, "delivery_slots"
    rows = _rows(cur, _CAPACITY_SQL, {"quote_id": quote_id})
    row = rows[0] if rows else {}
    if not int(row.get("keyword_rows") or 0):
        return None, "not_issued"
    # 🔴 `when_missing=0`:P0-5 那两处 `or 1` 的同一坑 —— 显式 0 必须保留。
    return normalize_article_capacity(row.get("required_articles"), when_missing=0), "confirmed_keywords"


def load_quote_projection(cur, *, quote_id: int, cutoff: datetime | None = None) -> dict[str, Any]:
    """取数 + 投影。调用方只需要这一个入口。"""
    if not quote_id:
        raise ProjectionUnavailable("quote_id 必填:六阶段投影是 quote-scoped 的,没有品牌级形态")
    cutoff_dt = cutoff or datetime.now(timezone.utc)
    if cutoff_dt.tzinfo is None:
        cutoff_dt = cutoff_dt.replace(tzinfo=timezone.utc)
    params = {"quote_id": int(quote_id), "cutoff": cutoff_dt}

    capacity, capacity_source = load_allocated_capacity(cur, int(quote_id))
    produced = _rows(cur, _PRODUCED_SQL, {"quote_id": int(quote_id)})
    attempts = _rows(cur, _ATTEMPTS_SQL, {"quote_id": int(quote_id)})
    monitoring = _rows(cur, _MONITORING_SQL.format(eligible=aggregate_eligible_sql("mr")), params)
    keyword_ids = [r["id"] for r in _rows(cur, _KEYWORD_IDS_SQL, {"quote_id": int(quote_id)})]

    projection = project_stages(
        quote_id=int(quote_id),
        cutoff=cutoff_dt,
        allocated_capacity=capacity,
        produced_units=produced,
        publication_attempts=attempts,
        monitoring_rows=monitoring,
        quote_keyword_ids=keyword_ids,
    )
    projection["capacity_source"] = capacity_source
    projection["watermark"] = {
        "attempt_rows": len(attempts),
        "monitoring_rows": len(monitoring),
        "produced_rows": len(produced),
        "quote_keyword_count": len(keyword_ids),
    }
    return projection


def load_service_completion(quote_id: int) -> dict[str, Any]:
    """W6 per-keyword 达标天数的**透传**。

    🔴 本函数刻意**不产出 quote 级 `complete`**。规格 03 §10:
       「W6 未 live 或 quote 聚合未签发时显示 unavailable,不猜 quote 总 complete。」
       把 per-keyword 的 complete 用 `all()` 合成一个 quote complete,正是那句话
       禁止的"猜"—— 某个词还没开始跑与某个词没达标,在 `all()` 里长得一样。
    """
    try:
        from db.monitoring_db import get_keyword_compliance_summary
    except Exception as exc:  # pragma: no cover - 只有 W6 未 live 才走到
        return {"available": False, "reason": f"w6_not_live: {exc}", "per_keyword": []}
    try:
        summary = get_keyword_compliance_summary(int(quote_id)) or {}
    except Exception as exc:
        return {"available": False, "reason": f"w6_query_failed: {exc}", "per_keyword": []}
    if not summary:
        return {"available": False, "reason": "quote_aggregate_not_issued", "per_keyword": []}
    per_keyword = []
    for kw_id, data in summary.items():
        compliant = data.get("compliant_days")
        service = data.get("service_days")
        per_keyword.append({
            "keyword_id": kw_id,
            "compliant_days": compliant,
            "service_days": service,
            # 单词级 complete 是 W6 的原始口径,可以透传;quote 级的不许合成。
            "complete": (bool(service) and compliant is not None and compliant >= service)
            if service else None,
        })
    return {
        "available": True,
        "reason": None,
        "per_keyword": per_keyword,
        "quote_complete": None,
        "quote_complete_reason": "not_derivable_from_content_stages",
    }


def assert_quote_predicates_present() -> None:
    """结构锚自证:四条发布链 + 监测 + 容量的 SQL **每一段**都带 quote predicate。

    这不是"数一数出现了几次"—— 数次数会被一段 SQL 里出现两次骗过去。
    做法是按 `FROM <表>` 切段,逐段要求命中,并且要求段数 == 已知链数。
    """
    blocks = [b for b in _ATTEMPTS_SQL.split("UNION ALL") if b.strip()]
    if len(blocks) != len(PUBLICATION_SOURCES):
        raise AssertionError(
            f"发布链段数 {len(blocks)} 与已知链数 {len(PUBLICATION_SOURCES)} 不符;"
            "新增一条链却没进本清单 = 该链的 quote 隔离无人看管")
    for block in blocks:
        if QUOTE_PREDICATE_TOKEN not in block.split("WHERE")[-1]:
            raise AssertionError(f"发布链缺 quote predicate:{block.strip()[:80]}")
    for name, sql in (("monitoring", _MONITORING_SQL), ("capacity", _CAPACITY_SQL),
                      ("slot_capacity", _SLOT_CAPACITY_SQL), ("produced", _PRODUCED_SQL),
                      ("keyword_ids", _KEYWORD_IDS_SQL)):
        if QUOTE_PREDICATE_TOKEN not in sql:
            raise AssertionError(f"{name} SQL 缺 quote predicate")


__all__ = [
    "PUBLICATION_SOURCES",
    "QUOTE_PREDICATE_TOKEN",
    "STAGES",
    "assert_quote_predicates_present",
    "load_allocated_capacity",
    "load_quote_projection",
    "load_service_completion",
    "source_versions",
]
