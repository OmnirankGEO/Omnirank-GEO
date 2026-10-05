# -*- coding: utf-8 -*-
"""#90 · 迁移必须 **schema 可移植** —— 不许把表名钉死在 public。

## 缺陷是什么

`tests/pricing_quote_wiring/conftest.py` 给每个进程发一个**私有 schema**,把
`search_path` 指过去,再灌 4 份生产迁移。而这些迁移里的 `@index-guard` DO 块
整段把宿主写死成 `public.`:

    CREATE TABLE IF NOT EXISTS pricing_catalog_versions (...)   -- 不带 schema → 落私有
    ...
    CREATE INDEX ... ON public.pricing_catalog_versions (...)   -- 带 public → 找不到

**同一份文件里自相矛盾**:建表不带 schema、建索引带 public。实测(全新库 +
私有 search_path):迁移在第一个索引守卫处硬报
`relation "public.pricing_catalog_versions" does not exist`,整份回滚,
`tests/pricing_quote_wiring` **158 个用例全 error**。修完 148 passed / 10 failed。

第二种形态更难发现 —— `migration_agreement_gate_observability_2026_07_29.sql`
写的是 `CREATE TABLE IF NOT EXISTS public.registration_agreement_gate_events`:
它**不报错**,而是静悄悄把表建到 public 去,私有 schema 的隔离就此漏了。
🔴 报错的那种会逼人来看;不报错的那种不制造任何问题。

## 🔴 分母:按**后果**收敛,不是按类型

    scripts/*.sql 全集                                  → 194 份
    其中出现 public. 的                                  →  84 份 / 2519 处
    其中**会在 search_path 可能非 public 时被执行**的      →  62 份 / 1565 处  ← 本门作用域

作用域两个来源,都机械导出、不手写:
  · `tests/**/conftest.py` 里点名的迁移(私有 schema 夹具直接灌);
  · **生产 .py** 里点名的迁移 —— 运行时自愈会在测试进程里跑,而那时 search_path
    是私有的。第 6 份 `migration_admin_user_governance_2026_07_15.sql` 正是这么
    冒出来的:我第一版分母只数 conftest,漏掉了整整这一类,让一条真缺陷在门外
    站了一整轮。

## 为什么不是「全部改成 0」

62 份 / 1565 处是同一个代码生成模板的产物,一次性全改的爆炸半径远超本工单。
所以:**已修的必须恒为 0,其余冻结在登记表里,只许降不许升**。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 🔴 本门要求恒为 0 的文件 —— 已在 #90 修好,回退就红。
MUST_BE_ZERO = (
    "migration_pricing_dual_ssot_2026_07_12.sql",
    "migration_agent_retail_sku_decoupling_2026_07_17.sql",
    "migration_pricing_quote_wiring_2026_07_14.sql",
    "migration_notification_outbox_2026_07_17.sql",
    "migration_agreement_gate_observability_2026_07_29.sql",
    "migration_admin_user_governance_2026_07_15.sql",
)

#: 冻结欠账(2026-09-05 实测)。只许降不许升;降了要同笔改小或删条目。
FROZEN: dict[str, int] = {
    "migration_actor_artifact_brand_id_2026_07_28.sql": 2,
    "migration_admin_cross_tenant_governance_2026_07_21.sql": 37,
    "migration_ai_ops_center_2026_07_01.sql": 60,
    "migration_article_ai_review_2026_08_01.sql": 15,
    "migration_article_generation_task_state_2026_07_21.sql": 5,
    "migration_client_purchase_gate_2026_07_29.sql": 4,
    "migration_dealer_inventory_resale_2026_07_15.sql": 60,
    "migration_dealer_jit_resale_2026_07_17.sql": 45,
    "migration_diagnosis_runs_2026_07_13.sql": 37,
    "migration_direct_service_refund_agreements_2026_07_15.sql": 29,
    "migration_geo_article_closed_loop_v1_2026_07_20.sql": 57,
    "migration_geo_article_v14_2026_07_19.sql": 58,
    "migration_geo_observation_aggregate_basis_2026_07_20.sql": 135,
    "migration_geo_observation_collection_mode_2026_07_20.sql": 2,
    "migration_geo_observation_v1_2026_07_17.sql": 118,
    "migration_geo_provider_attempts_2026_07_21.sql": 18,
    "migration_geo_research_monitor.sql": 135,
    "migration_geo_research_selfserve_2026_07_05.sql": 30,
    "migration_inventory_audit_equation_2026_07_29.sql": 10,
    "migration_inventory_distribution_chain_2026_08_12.sql": 12,
    "migration_kms_billing_mode_2026_08_16.sql": 2,
    "migration_kms_keyword_source_2026_08_16.sql": 6,
    "migration_m3_first_batch.sql": 15,
    "migration_marketing_center_2026_07_04.sql": 110,
    "migration_marketing_deal_drafts_2026_07_22.sql": 3,
    "migration_marketing_deal_drafts_org_binding_2026_07_23.sql": 2,
    "migration_media_cost_snapshot_2026_06_13.sql": 10,
    "migration_media_provider_routing_2026_08_02.sql": 16,
    "migration_mhz_awaiting_sync_exit_2026_07_26.sql": 2,
    "migration_mhz_media_name_trgm_2026_07_28.sql": 2,
    "migration_monitoring_cell_retry_2026_07_21.sql": 17,
    "migration_monitoring_extra_keyword_entitlement_2026_08_04.sql": 1,
    "migration_monitoring_identity_review_2026_07_21.sql": 4,
    "migration_monitoring_outcome_backfill_journal_2026_08_15.sql": 10,
    "migration_monitoring_product_matrix_2026_07_21.sql": 4,
    "migration_notification_center_channels_2026_07_29.sql": 4,
    "migration_ondemand_minting_2026_07_29.sql": 7,
    "migration_organization_all_accounts_onboarding_2026_07_22.sql": 35,
    "migration_organization_internal_seats_2026_07_20.sql": 127,
    "migration_organization_payer_policies_2026_07_23.sql": 10,
    "migration_organization_short_code_2026_07_28.sql": 11,
    "migration_publication_notice_audit_2026_08_01.sql": 10,
    "migration_publish_records_url_verification_2026_08_19.sql": 9,
    "migration_quote_snapshots_and_archives_2026_07_21.sql": 10,
    "migration_stage3_url_budget_2026_07_16.sql": 10,
    "migration_stage3_url_budget_industry_name_2026_07_18.sql": 1,
    "migration_strategy_ai_review_2026_08_01.sql": 10,
    "migration_svideo_image_mode_2026_07_30.sql": 2,
    "migration_v10_channel_revenue_exactly_once_2026_07_13.sql": 5,
    "migration_v10b_dedup_channel_revenue_with_backup_2026_07_13.sql": 5,
    "migration_v3_2_c_end.sql": 20,
    "migration_v3_2_commissions.sql": 33,
    "migration_v3_2_trial_passes.sql": 25,
    "migration_v3_3_managed_campaign.sql": 80,
    "migration_v5_geo_plan_settlement_2026_07_13.sql": 10,
    "migration_v6_dispute_escrow_2026_07_13.sql": 10,
    "migration_v6_fund_recovery_2026_07_13.sql": 10,
    "migration_v7_fund_recovery_fencing_2026_07_13.sql": 10,
    "migration_v7_geoplan_settle_conflict_index_2026_07_13.sql": 5,
    "migration_whitelabel_backoffice_scope_2026_07_22.sql": 3,
    "migration_workers4_scheduling_2026_07_13.sql": 20,
    "migration_writing_effectiveness_report_2026_07_26.sql": 10,
}


def _strip_sql_comments(text: str) -> str:
    """注释里的 `public.` 不改变行为 —— 不剔除的话检测器会把说明文字算成缺陷。"""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"--[^\n]*", " ", text)


_CREATE_UNQUALIFIED = re.compile(
    r"(?i)\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def repo_created_tables() -> set[str]:
    """全仓「不带 schema 建过」的表名 —— 机械枚举,不手写。

    有了它,`public.<t>` 才分得清是「本仓自己的表被钉死了」还是「引用真·public
    里的外部对象」。少这一层,`public.pg_stat_statements` 之类会被误判。
    """
    out: set[str] = set()
    for path in sorted((ROOT / "scripts").glob("*.sql")):
        body = _strip_sql_comments(path.read_text(encoding="utf-8", errors="ignore"))
        out |= {m.group(1).lower() for m in _CREATE_UNQUALIFIED.finditer(body)}
    return out


_NAMED_SQL = re.compile(
    r"(migration_[A-Za-z0-9_]+\.sql|rollback_[A-Za-z0-9_]+\.sql|backfill_[A-Za-z0-9_]+\.sql)")


def live_migrations() -> set[str]:
    """会在「search_path 可能不是 public」的进程里被执行的迁移。"""
    names: set[str] = set()
    for path in ROOT.rglob("conftest.py"):
        if any(x in path.parts for x in (".git", "node_modules")):
            continue
        names |= set(_NAMED_SQL.findall(path.read_text(encoding="utf-8", errors="ignore")))
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).parts
        if not rel or rel[0] in ("tests", "scripts"):
            continue
        if any(x in rel for x in (".git", "node_modules", "frontend")):
            continue
        names |= set(_NAMED_SQL.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return names


def count_offenses(sql_text: str, repo_tables: set[str]) -> int:
    """四种把 schema 钉死的写法,逐类计数。"""
    body = _strip_sql_comments(sql_text)
    n = len(re.findall(
        r"(?i)\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?public\s*\.", body))
    n += sum(len(re.findall(r"(?i)\bpublic\s*\.\s*" + t + r"\b", body)) for t in repo_tables)
    n += len(re.findall(r"(?i)'public'\s*::\s*regnamespace", body))
    n += len(re.findall(r"(?i)table_schema\s*=\s*'public'", body))
    return n


def scan() -> dict[str, int]:
    repo_tables = repo_created_tables()
    live = live_migrations()
    out: dict[str, int] = {}
    for path in sorted((ROOT / "scripts").glob("*.sql")):
        if path.name not in live:
            continue
        n = count_offenses(path.read_text(encoding="utf-8", errors="ignore"), repo_tables)
        if n:
            out[path.name] = n
    return out


# ══ ① 分母自证 ═════════════════════════════════════════════════════
def test_the_denominator_is_mechanical_and_nonempty():
    """先证尺子在量东西 —— 空分母的绿等于什么都没说。"""
    assert len(repo_created_tables()) > 100, "仓建表集合塌了,`public.<t>` 判定会全部失效"
    live = live_migrations()
    assert len(live) > 50, f"只找到 {len(live)} 份会被执行的迁移 —— 作用域导出坏了"
    assert "migration_admin_user_governance_2026_07_15.sql" in live, (
        "第 6 份不在作用域里 —— 说明只数了 conftest,漏掉「生产代码运行时自愈」那一类。"
        "这正是第一版分母的洞。")


# ══ ② 主锁:已修的必须保持 0 ═══════════════════════════════════════
def test_fixed_migrations_stay_schema_portable():
    repo_tables = repo_created_tables()
    bad = {}
    for name in MUST_BE_ZERO:
        path = ROOT / "scripts" / name
        assert path.exists(), f"{name} 不见了 —— 改名/删除要同笔更新本表"
        n = count_offenses(path.read_text(encoding="utf-8", errors="ignore"), repo_tables)
        if n:
            bad[name] = n
    assert not bad, (
        f"这些迁移又把 schema 钉死了:{bad}\n"
        "    私有 search_path 下 `CREATE INDEX ... ON public.X` 会硬报 relation does not exist;\n"
        "    `CREATE TABLE public.X` 更糟 —— 不报错,直接把表建到 public,隔离静默失效。\n"
        "    写法:表名不带 schema(靠 search_path 解析);判索引宿主用\n"
        "    `(SELECT relnamespace FROM pg_class WHERE oid = to_regclass('<表>'))`,\n"
        "    不要 `'public'::regnamespace`。")


# ══ ③ 冻结欠账只许降不许升 ═════════════════════════════════════════
def test_frozen_backlog_does_not_grow():
    found = scan()
    grown = {k: (FROZEN.get(k, 0), v) for k, v in found.items() if v > FROZEN.get(k, 0)}
    assert not grown, (
        f"这些迁移的欠账变多了(冻结 → 实测):{grown}\n"
        "    新写的迁移不许再钉死 schema;老文件只许降。")


def test_frozen_registry_is_not_stale():
    """修好了要同笔改小 —— 登记比实际大,说明登记表在说谎。"""
    found = scan()
    stale = {k: (v, found.get(k, 0)) for k, v in FROZEN.items() if found.get(k, 0) < v}
    assert not stale, f"这些已修好/已删,却还留在冻结表里(冻结 → 实测):{stale} —— 同笔调低或删。"


# ══ ④ 判别力自证:两臂都要 ═══════════════════════════════════════════
def test_the_detector_flags_a_forged_offender():
    """🔴 没有这条,上面的 0 可能只是因为检测器什么都没看见。"""
    repo_tables = repo_created_tables()
    t = sorted(repo_tables)[0]
    for forged in (
        "CREATE INDEX ix ON public." + t + " (id);",
        "CREATE TABLE IF NOT EXISTS public." + t + " (id INT);",
        "SELECT 1 WHERE c.relnamespace = 'public'::regnamespace;",
        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public';",
    ):
        assert count_offenses(forged, repo_tables) >= 1, "没抓到:" + forged


def test_the_detector_does_not_flag_the_portable_form():
    """反臂:改好之后的写法不许被判成缺陷,否则修完还是红,门就废了。"""
    repo_tables = repo_created_tables()
    t = sorted(repo_tables)[0]
    portable = (
        "CREATE INDEX ix ON " + t + " (id);\n"
        "SELECT 1 WHERE c.relnamespace = "
        "(SELECT relnamespace FROM pg_class WHERE oid = to_regclass('" + t + "'));\n"
        "-- 历史说明:以前这里写的是 public." + t + ",见 #90\n"
    )
    assert count_offenses(portable, repo_tables) == 0, (
        "可移植写法被误判 —— 注意注释里的 public.X 不算(已剔除注释)。")
