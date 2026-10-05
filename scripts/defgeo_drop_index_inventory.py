"""轴C:19 处 ``DROP INDEX`` 的**期望表审定清单**(单一权威源)。

═══════════════════════════════════════════════════════════════════════════
为什么需要"审定"
═══════════════════════════════════════════════════════════════════════════
``DROP INDEX [IF EXISTS] <name>`` 在 PostgreSQL 语法上**不带表名**,所以要给它
加表绑定守卫,就必须先回答"这条本来该删哪张表上的索引"。这个答案不在语句里,
只能从上下文推。推错了比不改更糟 —— 会把本来能删的删不掉。

所以每一处的期望表都必须**有出处**,分三档:

  ``A1`` 同文件、DROP 之后最近的同名 ``-- @index-guard <idx> ON <tbl>``
         (drop-then-recreate 对)—— 完全机械,判据现推现比;
  ``A2`` 同文件、DROP 之后最近的同名**裸** ``CREATE [UNIQUE] INDEX <idx> ON <tbl>``
         (轴A 没碰过这种形态,它本来就带表名)—— 同样机械;
  ``M``  上面两档都够不到 —— **人工审定**,依据逐条写进 :data:`ADJUDICATED`
         的 ``evidence``,且必须是可核的(行号 / 运行期代码 / 同文件后继索引)。

判据侧:``A1``/``A2`` 两档由 :func:`derive_expected_table` **现推**,与本表比对,
手抄漂移当场红;``M`` 档冻结,改一个字都要红。

═══════════════════════════════════════════════════════════════════════════
一处**不改写**的例外
═══════════════════════════════════════════════════════════════════════════
``migration_geo_observation_aggregate_basis_2026_07_20.sql:777`` 的
``EXECUTE format('DROP INDEX public.%I', item.index_name)`` 是**动态**的,
索引名来自一个游标。但它**本来就是绑表的** —— 那个游标的 WHERE 里写着
``i.indrelid='public.geo_observation_insight_jobs'::regclass``,
候选集根本不可能含别的表的索引。所以它不属于"裸 DROP",不改写,
由 :data:`DYNAMIC_BOUND_SITES` 冻结 + 判据盯住那句 indrelid 过滤不许消失。
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.defgeo_index_guard_scan import (
    scan_bound_index_guards,
    scan_drop_index_sites,
    strip_sql_noise,
)

__all__ = [
    "ADJUDICATED",
    "DYNAMIC_BOUND_SITES",
    "derive_expected_table",
    "bare_create_index_map",
]

#: 同文件裸 ``CREATE [UNIQUE] INDEX <idx> ON <tbl>``(不带 IF NOT EXISTS —— 轴A 没碰过它)。
_BARE_CREATE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?!IF\s+NOT\s+EXISTS)"
    r"\"?(?P<idx>[A-Za-z_][A-Za-z_0-9$]*)\"?\s+ON\s+"
    r"(?:public\s*\.\s*)?\"?(?P<tbl>[A-Za-z_][A-Za-z_0-9$]*)\"?",
    re.IGNORECASE,
)


def bare_create_index_map(sql: str) -> dict[str, tuple[str, int]]:
    """``{索引名: (表, 行号)}`` —— 同文件里的裸 CREATE INDEX。"""
    masked = strip_sql_noise(sql)
    out: dict[str, tuple[str, int]] = {}
    for m in _BARE_CREATE.finditer(masked):
        out.setdefault(m.group("idx"), (m.group("tbl"), sql[:m.start()].count("\n") + 1))
    return out


def derive_expected_table(sql: str, rel: str, index: str, drop_line: int
                          ) -> tuple[str, str, str] | None:
    """现推 A1/A2 两档。回 ``(档位, 表, 依据)``;推不出回 ``None``。"""
    for g in scan_bound_index_guards(sql, rel):
        if g.index != index:
            continue
        gl = sql[:g.start].count("\n") + 1
        if gl >= drop_line:
            return ("A1", g.table, f"同文件 L{gl} 的 @index-guard 配对")
    hit = bare_create_index_map(sql).get(index)
    if hit and hit[1] >= drop_line:
        return ("A2", hit[0], f"同文件 L{hit[1]} 的裸 CREATE INDEX 配对")
    return None


#: 🔴 19 组 ``(文件, 行, 索引名) → 期望表``。A1/A2 由 :func:`derive_expected_table` 现推核对;
#:    M 档冻结 + 依据可核。行号只作定位,判据不按行号断言(行号会随编辑漂移)。
ADJUDICATED: tuple[dict, ...] = (
    # ── A1:drop-then-recreate,同文件 @index-guard 配对(8 处)──────────────
    {"file": "db/migration_034_geo_image_note_contract_2026_08_17.sql",
     "index": "uq_mhz_item_live_revision_root", "table": "mhz_publish_order_items",
     "tier": "A1", "if_exists": False, "inline": True,
     "evidence": "同文件 @index-guard uq_mhz_item_live_revision_root ON mhz_publish_order_items"},
    {"file": "scripts/migration_geo_research_selfserve_2026_07_05.sql",
     "index": "uq_selfserve_active_idem", "table": "geo_research_selfserve_queue",
     "tier": "A1", "if_exists": True, "inline": False,
     "evidence": "同文件 @index-guard uq_selfserve_active_idem ON geo_research_selfserve_queue"},
    {"file": "scripts/migration_v5_geo_plan_settlement_2026_07_13.sql",
     "index": "idx_geoplan_brand_status", "table": "geo_plan_tasks",
     "tier": "A1", "if_exists": True, "inline": False,
     "evidence": "同文件 @index-guard idx_geoplan_brand_status ON geo_plan_tasks"},
    {"file": "scripts/migration_v6_fund_recovery_2026_07_13.sql",
     "index": "uniq_fund_recovery_open", "table": "fund_recovery_orders",
     "tier": "A1", "if_exists": True, "inline": False,
     "evidence": "同文件 @index-guard uniq_fund_recovery_open ON fund_recovery_orders"},
    {"file": "scripts/migration_v7_fund_recovery_fencing_2026_07_13.sql",
     "index": "uniq_fund_recovery_open", "table": "fund_recovery_orders",
     "tier": "A1", "if_exists": True, "inline": False,
     "evidence": "同文件 @index-guard uniq_fund_recovery_open ON fund_recovery_orders"},
    {"file": "scripts/migration_v7_fund_recovery_fencing_2026_07_13.sql",
     "index": "uniq_fund_recovery_geoplan_open", "table": "fund_recovery_orders",
     "tier": "A1", "if_exists": True, "inline": False,
     "evidence": "同文件 @index-guard uniq_fund_recovery_geoplan_open ON fund_recovery_orders"},
    {"file": "scripts/migration_v7_geoplan_settle_conflict_index_2026_07_13.sql",
     "index": "idx_geoplan_brand_status", "table": "geo_plan_tasks",
     "tier": "A1", "if_exists": True, "inline": False,
     "evidence": "同文件 @index-guard idx_geoplan_brand_status ON geo_plan_tasks"},
    {"file": "scripts/migration_v10_channel_revenue_exactly_once_2026_07_13.sql",
     "index": "uniq_fund_recovery_channel_open", "table": "fund_recovery_orders",
     "tier": "A1", "if_exists": True, "inline": False,
     "evidence": "同文件 @index-guard uniq_fund_recovery_channel_open ON fund_recovery_orders"},

    # ── A2:同文件裸 CREATE INDEX 配对(5 处)────────────────────────────────
    {"file": "scripts/migration_billing_deduction_idempotency_2026_07_19.sql",
     "index": "idx_billing_deduction_idempotency_created",
     "table": "billing_deduction_idempotency",
     "tier": "A2", "if_exists": True, "inline": False,
     "evidence": "紧随其后的裸 CREATE INDEX … ON billing_deduction_idempotency(created_at DESC);"
                 "上一句还有 ALTER TABLE billing_deduction_idempotency DROP CONSTRAINT IF EXISTS 同名"},
    {"file": "scripts/migration_billing_deduction_idempotency_2026_07_19.sql",
     "index": "idx_billing_deduction_charge_identity",
     "table": "billing_deduction_idempotency",
     "tier": "A2", "if_exists": True, "inline": False,
     "evidence": "紧随其后的裸 CREATE UNIQUE INDEX … ON billing_deduction_idempotency(...)"},
    {"file": "scripts/migration_billing_deduction_idempotency_2026_07_19.sql",
     "index": "idx_billing_debt_offset_pending", "table": "billing_debt_offset_outbox",
     "tier": "A2", "if_exists": True, "inline": False,
     "evidence": "紧随其后的裸 CREATE INDEX … ON billing_debt_offset_outbox(next_retry_at, created_at)"},
    {"file": "scripts/migration_billing_deduction_idempotency_2026_07_19.sql",
     "index": "idx_billing_debt_offset_charge", "table": "billing_debt_offset_outbox",
     "tier": "A2", "if_exists": True, "inline": False,
     "evidence": "紧随其后的裸 CREATE UNIQUE INDEX … ON billing_debt_offset_outbox(ledger_type, charge_tx_id)"},
    {"file": "scripts/migration_marketing_deal_drafts_2026_07_22.sql",
     "index": "uq_marketing_deal_drafts_request", "table": "marketing_deal_drafts",
     "tier": "A2", "if_exists": True, "inline": False,
     "evidence": "紧随其后的裸 CREATE UNIQUE INDEX … ON public.marketing_deal_drafts(owner_user_id, request_id)"},

    # ── M:人工审定(5 处)· 依据逐条可核 ─────────────────────────────────
    {"file": "scripts/migration_v6_fund_recovery_2026_07_13.sql",
     "index": "idx_fund_recovery_status", "table": "fund_recovery_orders",
     "tier": "M", "if_exists": True, "inline": False,
     "evidence": "被同文件下一句的后继索引 idx_fund_recovery_claim ON fund_recovery_orders 取代"
                 "(注释「认领扫描索引(pending 且到重试点)」);运行期同源见 "
                 "db/fund_recovery_db.py:275 DROP INDEX IF EXISTS idx_fund_recovery_status "
                 "紧接 CREATE INDEX idx_fund_recovery_claim ON fund_recovery_orders(...)"},
    {"file": "scripts/migration_v7_fund_recovery_fencing_2026_07_13.sql",
     "index": "uniq_fund_recovery_open_nullcharge", "table": "fund_recovery_orders",
     "tier": "M", "if_exists": True, "inline": False,
     "evidence": "注释「NULL charge_tx_id 工单【不去重】…若历史误建过 null-charge 唯一键则清除」;"
                 "该唯一键与同表的 uniq_fund_recovery_open 同族;运行期同源见 "
                 "db/fund_recovery_db.py:296(其上下文全在 fund_recovery_orders 上)"},
    {"file": "scripts/migration_diagnosis_runs_2026_07_13.sql",
     "index": "uq_diag_refund_extref", "table": "diagnosis_refund_records",
     "tier": "M", "if_exists": True, "inline": False,
     "evidence": "行内注释「旧唯一(external_ref)废弃 → 换 refund_tx_ref」;"
                 "external_ref 是 diagnosis_refund_records 的旧列(同文件 L374/L385/L399 的升级块);"
                 "紧随其后的后继守卫是 @index-guard uq_diag_refund_run ON diagnosis_refund_records"},
    {"file": "scripts/migration_kms_keyword_source_2026_08_16.sql",
     "index": "uniq_kms_keyword_active", "table": "keyword_monitor_subscriptions",
     "tier": "M", "if_exists": True, "inline": False,
     "evidence": "运行期硬证:db/monitoring_db.py:2088 "
                 "CREATE UNIQUE INDEX IF NOT EXISTS uniq_kms_keyword_active "
                 "ON keyword_monitor_subscriptions(keyword_id) WHERE status IN (...)"},
    {"file": "scripts/migration_kms_keyword_source_2026_08_16.sql",
     "index": "idx_kms_unique_active", "table": "keyword_monitor_subscriptions",
     "tier": "M", "if_exists": True, "inline": False,
     "evidence": "被同文件上一句的后继索引 idx_kms_unique_active_v2 "
                 "ON keyword_monitor_subscriptions 取代(_v2 命名即前身关系);"
                 "该文件全篇只操作 keyword_monitor_subscriptions"},
)

#: 唯一一处**动态但本来就绑表**的 DROP —— 不改写,冻结 + 盯住它的 indrelid 过滤。
DYNAMIC_BOUND_SITES: tuple[dict, ...] = (
    {"file": "scripts/migration_geo_observation_aggregate_basis_2026_07_20.sql",
     "snippet": "EXECUTE format('DROP INDEX public.%I',item.index_name);",
     "bound_by": "i.indrelid='public.geo_observation_insight_jobs'::regclass",
     "why": "索引名来自游标,而游标的 WHERE 已经把候选集限死在 "
            "geo_observation_insight_jobs 上 —— 不可能删到别的表的索引。"
            "动态语句加不了静态表绑定守卫,所以不改写,只冻结 + 盯住那句 indrelid 过滤。"},
)


def audit_rows(root: Path) -> list[dict]:
    """把审定表与**现推**结果并排,供判据比对。"""
    out = []
    for row in ADJUDICATED:
        sql = (root / row["file"]).read_text(encoding="utf-8", errors="replace")
        lines = [ln for _r, ln, nm in scan_drop_index_sites(sql, row["file"])
                 if nm == row["index"]]
        derived = None
        for ln in lines:
            derived = derive_expected_table(sql, row["file"], row["index"], ln)
            if derived:
                break
        out.append({**row, "drop_lines": lines, "derived": derived})
    return out
