#!/usr/bin/env python
"""工单 E3 撕锁 runner(**自选**变异)。

纪律(逐条来自本仓既有 runner,违反任何一条这一轮的数字都失效):
 · 每发执行前再验一次**锚点唯一**(count == 1),≠1 立刻停,不静默跳过;
 · 备份**落盘** ``.mutbak`` + 还原走 ``tmp + os.replace``(原子);还原后逐字节核 sha;
 · **禁 `git checkout`**;
 · 起跑前扫全树残留备份,发现即拒跑(本 runner 就地改源,明文禁并发);
 · 起跑前查磁盘余量(2026-08-25 实测:盘满时炸在**还原**那一步,源文件被留成 0 字节);
 · 基线必须全绿,否则后面全部作废;
 · 「预测被杀」按**精确红集合**判,溢出与欠红都记 FAIL;
 · 红 0 时**绿数必须守恒** —— 少一条就说明有测试没跑,而"没跑"在红集正则下
   长得跟"跑了没红"一模一样。

🔴 本清单是**自选**(判据作者=我)。自己出题自己批改证明不了什么 ——
   外选那一栏留空,等 Review 或另一窗供题。

🔴 「失败签名」模式(``expect_signature``):有些变异的正确形态不是"几条判据红",
   而是**迁移当场拒绝应用** ⇒ session fixture 起不来 ⇒ 所有**要库**的判据 error
   (纯读文件的那些**理应仍绿**,别把它们当漏网)。
   这一类判三件:绿数真的掉了、有红、失败文本里出现那个对象名。
   不硬抄几十个 node id 当期望集 —— 手抄清单漏一个都不会红。

🔴 本单跨**三个**判据包(各有各的一次性库),所以每发变异要声明 ``suite``。
   跨包跑的代价是慢;不跨包的代价是"改了 A 包的生产代码却只跑 B 包" ——
   那种绿是假的。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mutation_tree_lock import tree_lock  # noqa: E402

MIN_FREE_BYTES = 512 * 1024 * 1024

#: 每个包一条一次性库。库名安全栓由各包 conftest 自己再验一次。
SUITES: dict[str, tuple[str, str]] = {
    "e3":  ("tests/defgeo_e3_2026_08_26",
            "postgresql://geo_admin:testpw@localhost:55487/geo_e3_ledger_test"),
    "woc": ("tests/defgeo_woc_closure_2026_08_25",
            "postgresql://geo_admin:testpw@localhost:55484/geo_defgeo_woc_test"),
    "pkgf": ("tests/defensive_geo_pkgf_2026_08_23",
             "postgresql://geo_admin:testpw@localhost:55475/geo_defgeo_pkgf_test"),
    "w3":  ("tests/defensive_geo_w3_2026_08_21",
            "postgresql://geo_admin:testpw@localhost:55475/geo_defgeo_w3c_test"),
    # [MUT-E4-04b] 进度端点的**真 HTTP** 臂长在这个包里。
    "w4":  ("tests/defensive_geo_w4_2026_08_22",
            "postgresql://geo_admin:testpw@localhost:55475/geo_defgeo_w4_test"),
}

BRIDGE = "services/defensive_geo/monitoring/run_ledger_bridge.py"
LEGACY = "services/defensive_geo/monitoring/legacy_bridge.py"
MDB = "db/monitoring_db.py"
M046 = "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql"
M054 = "db/migration_054_monitoring_cell_tenant_owner_2026_08_26.sql"
PKGF_CONF = "tests/defensive_geo_pkgf_2026_08_23/conftest.py"
REPAIR = "services/defensive_geo/legal_repair.py"
ASSIST = "api/defensive_geo_assist_api.py"
GEN = "scripts/defgeo_readiness_gen.py"
LINEAGE = "services/monitoring_lineage.py"
CMPFEED = "services/defensive_geo/monitoring/comparability_feed.py"
MONAPI = "api/defensive_monitoring_api.py"
REPORTAPI = "api/defensive_geo_report_api.py"
ACCEPT = "services/defensive_geo/acceptance_evidence.py"
TESTER = "tools/ai_visibility/ai_tester.py"

MUTATIONS: list[dict] = [
    # ── E3-1 租户归属 ────────────────────────────────────────────────
    {
        "id": "MUT-E1-01", "suite": "e3", "file": BRIDGE,
        "title": "唯一谓词退回「取不到就落 0」",
        "from": "    _alert_tenant_unresolved(\n",
        "to": "    return 0\n    _alert_tenant_unresolved(\n",
        # 🔴 [期望订正 · 首跑实测] 只有 e1_21 红,不是五条。
        #    原因是**两把锁叠在同一条路径上**(本仓记过):落 0 之后那条 INSERT
        #    撞 046 的 chk_defgeo_attempt_tenant_owner_positive,guarded 把它
        #    回滚吞掉 ⇒ 函数照样返 None、账本照样零行 ——
        #    e1_22/24/28/30 断言的正是"返 None + 零行",于是它们**看不见**这一发。
        #    真正抓住它的是 e1_21 里那句 len(alerts) == 1(应用层有没有喊)。
        #    ⇒ 结论不是"判据够用",是**那四条守的是 DB CHECK,不是应用谓词**。
        #      两层是纵深:CHECK 被单独摘掉时(MUT-E1-07)它们才轮到自己上场。
        "expect": {
            "test_e1_21_claim_writes_nothing_when_the_tenant_is_unknown",
        },
    },
    {
        "id": "MUT-E1-02", "suite": "e3", "file": BRIDGE,
        "title": "claim 退回读幻列 billing_user_id",
        "from": "        tenant_owner = _require_tenant_owner(\n"
                "            cell, what=\"open_for_claim\", plan_cell_id=plan_cell_id)",
        "to": "        tenant_owner = int(cell.get(\"billing_user_id\") or 0)",
        # [期望订正] e1_04(cell 键 census)也红 —— 我漏登了;
        # e1_22 不红,同 MUT-E1-01 的 DB-CHECK 吸收。
        "expect": {
            "test_e1_04_every_key_the_bridges_read_off_a_cell_is_a_real_column",
            "test_e1_20_claim_writes_the_frozen_tenant",
            "test_e1_21_claim_writes_nothing_when_the_tenant_is_unknown",
            "test_e1_40_every_ledger_write_goes_through_the_one_predicate",
            "test_e1_41_no_bridge_still_reads_the_phantom_column",
        },
    },
    {
        "id": "MUT-E1-03", "suite": "e3", "file": BRIDGE,
        "title": "未尝试收口退回**现读** brands.owner_user_id",
        "from": "                   c.tenant_owner_user_id\n"
                "              FROM public.monitoring_run_cells c\n",
        "to": "                   COALESCE(b.owner_user_id, 0) AS tenant_owner_user_id\n"
              "              FROM public.monitoring_run_cells c\n"
              "              LEFT JOIN public.brands b ON b.id = c.brand_id\n",
        "expect": {
            "test_e1_26_a_brand_transfer_does_not_rewrite_history",
        },
    },
    {
        "id": "MUT-E1-04", "suite": "e3", "file": BRIDGE,
        "title": "人工确认又把**操作者**写成租户",
        "from": "             str(run_authority_id or \"\"), tenant_owner, int(brand_id),",
        "to": "             str(run_authority_id or \"\"), int(actor_user_id), int(brand_id),",
        "expect": {
            "test_e1_27_human_resolution_records_the_tenant_not_the_operator",
        },
    },
    {
        "id": "MUT-E1-05", "suite": "e3", "file": LEGACY,
        "title": "存量抢救退回读幻列",
        "from": "                tenant_owner,\n",
        "to": "                int(cell.get(\"billing_user_id\") or 0) or 0,\n",
        # [期望订正] e1_04 也红 —— 我漏登了(census 覆盖两个桥模块)。
        "expect": {
            "test_e1_04_every_key_the_bridges_read_off_a_cell_is_a_real_column",
            "test_e1_29_legacy_backfill_writes_the_frozen_tenant",
            "test_e1_41_no_bridge_still_reads_the_phantom_column",
        },
    },
    {
        "id": "MUT-E1-06", "suite": "pkgf", "file": MDB,
        "title": "建格时不再冻结租户(落 NULL)",
        "from": "        frozen_tenant_owner = int(_raw_owner) if _raw_owner else None",
        "to": "        frozen_tenant_owner = None",
        "expect": {
            "test_create_run_cells_records_policy_skip_through_the_live_chain",
        },
    },
    {
        "id": "MUT-E1-07", "suite": "e3", "file": M046,
        "title": "账本禁 0 弱化成 >= 0",
        "from": "            CHECK (tenant_owner_user_id > 0);",
        "to": "            CHECK (tenant_owner_user_id >= 0);",
        # 🔴 [期望两次订正 · 实测] 这一发按**失败签名**判,不按红集:
        #    弱化 CHECK ⇒ 库里的定义与 046 那块**机械生成的 readiness** 逐字对不上
        #    ⇒ 迁移在 session fixture 里当场 RAISE ⇒ 所有**要库**的判据 error。
        #    第一次我登成 1 条红(错);第二次登成"整包炸、零绿"(也错 ——
        #    纯读文件的 32 条判据不碰库,理应仍绿)。
        #    硬抄 30 个 node id 当期望集又是手抄清单(漏一个都不会红)。
        #    所以判三件:绿数**真的掉**了、有红、且失败文本里出现那个约束名。
        "expect_signature": "chk_defgeo_attempt_tenant_owner_positive",
    },
    {
        "id": "MUT-E1-08", "suite": "e3", "file": M054,
        "title": "格上禁 0 弱化成 >= 0",
        "from": "            CHECK (tenant_owner_user_id IS NULL OR tenant_owner_user_id > 0);",
        "to": "            CHECK (tenant_owner_user_id IS NULL OR tenant_owner_user_id >= 0);",
        # [期望订正] 同 MUT-E1-07:054 的 readiness 块逐字对不上 ⇒ 要库的判据全 error,
        # 纯读文件的仍绿。按失败签名判。
        "expect_signature": "chk_monitoring_run_cells_tenant_owner_positive",
    },
    {
        "id": "MUT-E1-09", "suite": "e3", "file": PKGF_CONF,
        "title": "夹具又造一列生产没有的 billing_user_id",
        "from": "    tenant_owner_user_id  INTEGER,\n    claim_token           UUID,",
        "to": "    tenant_owner_user_id  INTEGER,\n    billing_user_id       INTEGER,\n"
              "    claim_token           UUID,",
        "expect": {
            "test_e1_03_the_pkgf_fixture_may_not_invent_columns",
        },
    },
    # ── E3-2 法律修复原子性 ──────────────────────────────────────────
    {
        "id": "MUT-E2-01", "suite": "woc", "file": REPAIR,
        "title": "退回「吞掉刷新异常、照样返新 hash」",
        "from": "        raise LegalRepairNotApplied(\n"
                "            \"这一次没有改成,稿子一个字都没动\") from exc",
        "to": "        pass",
        "expect": {
            "test_e2_01_a_failed_refresh_leaves_body_and_both_hashes_untouched[db]",
            "test_e2_01_a_failed_refresh_leaves_body_and_both_hashes_untouched[plain]",
            "test_e2_02_the_failure_is_typed_as_not_applied_not_as_a_rejection",
            "test_e2_03_the_connection_survives_the_rollback_and_the_next_apply_works",
        },
    },
    {
        "id": "MUT-E2-02", "suite": "woc", "file": REPAIR,
        "title": "SAVEPOINT 挪到 UPDATE **之后**(回滚撤不掉正文)",
        "from": "    cur.execute(f\"SAVEPOINT {_APPLY_SAVEPOINT}\")\n    try:\n"
                "        cur.execute(\"UPDATE articles SET content = %s WHERE id = %s\",\n"
                "                    (new_content, int(article_id)))\n",
        "to": "    cur.execute(\"UPDATE articles SET content = %s WHERE id = %s\",\n"
              "                (new_content, int(article_id)))\n"
              "    cur.execute(f\"SAVEPOINT {_APPLY_SAVEPOINT}\")\n    try:\n",
        # [期望订正] db 臂与"连接还能用"那条也红 —— 我只登了 plain 臂。
        # 回滚点挪到 UPDATE 之后 ⇒ 正文撤不掉(plain),且 aborted 事务救不回来(db)。
        "expect": {
            "test_e2_01_a_failed_refresh_leaves_body_and_both_hashes_untouched[db]",
            "test_e2_01_a_failed_refresh_leaves_body_and_both_hashes_untouched[plain]",
            "test_e2_03_the_connection_survives_the_rollback_and_the_next_apply_works",
        },
    },
    {
        "id": "MUT-E2-03", "suite": "woc", "file": REPAIR,
        "title": "多命中退回「取第一处」",
        "from": "    occurrences = text.count(needle)\n    if occurrences > 1:",
        "to": "    occurrences = text.count(needle)\n    if False:",
        "expect": {
            "test_e2_10_a_passage_that_appears_twice_is_ambiguous_not_first_hit",
        },
    },
    {
        "id": "MUT-E2-04", "suite": "woc", "file": ASSIST,
        "title": "歧义被判成**可重试**(前端会陷入死循环重试)",
        "from": "        raise _safe_error(\n            \"VALIDATION_FAILED\",\n"
                "            reason_key=\"legal_repair_ambiguous\",",
        "to": "        raise _safe_error(\n            \"POLICY_UNAVAILABLE\",\n"
              "            reason_key=\"legal_repair_ambiguous\",",
        # 🔴 [首跑**存活** ⇒ 补洞后重登] e2_20 拿 code 当**入参**自己构造信封,
        #    验的是"给定 code 信封对不对",不是"handler 会不会给这个 code" ——
        #    所以改映射整发存活。补了 e2_22(读真源码的映射 + 可重试性相反)。
        "expect": {
            "test_e2_22_the_handler_really_maps_each_outcome_to_the_right_code",
        },
    },
    # ── E3-3 迁移精确守卫 ────────────────────────────────────────────
    {
        "id": "MUT-E3-01", "suite": "e3", "file": GEN,
        "title": "readiness 轴把 051 摘出去",
        "from": "    \"051\": \"db/migration_051_defgeo_publish_settlement_guards_2026_08_25.sql\",\n",
        "to": "",
        "expect": {
            "test_e3_30_the_three_migrations_are_on_the_readiness_axis",
        },
    },
    {
        "id": "MUT-E3-02", "suite": "e3", "file": M054,
        "title": "054 的列类型核验被摘(同名错类型列静默放行)",
        "from": "    IF col_type <> 'integer' THEN",
        "to": "    IF FALSE THEN",
        "expect": {
            "test_e3_21_054_refuses_a_same_named_wrong_type_column",
        },
    },
    # ── E3-4 三件延期最低条件 ────────────────────────────────────────
    {
        "id": "MUT-E4-01", "suite": "e3", "file": LINEAGE,
        "title": "回落值也被标成 provider_echo",
        "from": "    model_source = \"provider_echo\" if str(model or \"\").strip() else \"planned_fallback\"",
        "to": "    model_source = \"provider_echo\"",
        "expect": {
            "test_e4_20_a_missing_provider_model_is_marked_planned_fallback",
        },
    },
    {
        "id": "MUT-E4-02", "suite": "e3", "file": CMPFEED,
        "title": "计划值又能进 matched cohort",
        "from": "    return tuple(c for c in cells\n"
                "                 if str(getattr(c, \"model_source\", \"\")) == MODEL_SOURCE_COMPARABLE)",
        "to": "    return tuple(cells)",
        "expect": {
            "test_e4_23_planned_fallback_cells_do_not_enter_the_matched_cohort",
        },
    },
    {
        "id": "MUT-E4-03", "suite": "e3", "file": MDB,
        "title": "回落值又被当成 observed 落账",
        "from": "                observed_model=(\n"
                "                    lineage_payload[\"model\"]\n"
                "                    if lineage_payload.get(\"model_source\") == \"provider_echo\"\n"
                "                    else None),",
        "to": "                observed_model=lineage_payload[\"model\"],",
        "expect": {
            "test_e4_22_a_planned_fallback_is_never_written_as_observed",
        },
    },
    {
        "id": "MUT-E4-04", "suite": "e3", "file": MONAPI,
        "title": "legacy 任务又拿到 200/progressPct=0",
        # 🔴 [锚点订正] 首跑在这里**停住**(命中 0 次)—— 因为我在写完 runner
        #    之后把分流从「品牌 enrollment」改成了「这次运行有没有 v2 耐久计划」。
        #    锚点唯一性闸按设计工作了:它没有自作主张换个锚点继续跑,
        #    而"换锚点继续跑"会让这一发悄悄贴在别的地方,红集从此没有意义。
        "from": "            if not plan_cell_ids:\n"
                "                raise _safe_error(\"MONITORING_LEGACY_RUN\")",
        "to": "            if False and not plan_cell_ids:\n"
              "                raise _safe_error(\"MONITORING_LEGACY_RUN\")",
        # 🔴 [首跑**存活** ⇒ 加固后重登] 原来的 e4_32 只判"两个名字在不在 AST 里",
        #    而 ``if False and not plan_cell_ids:`` 两个名字**都还在** ——
        #    整发穿过去(本仓记过「elif orelse 骗过接线锁」同形)。
        #    现在 e4_32 判**分支条件的形状**(必须是 ``not plan_cell_ids``)。
        "expect": {
            "test_e4_32_the_progress_endpoint_branches_on_enrollment",
        },
    },
    {
        # 🔴 结构臂(上一发)只证明"形状对";**这一支真的会走**要真 HTTP 来证。
        #    同一处生产改动打两个包 —— 单靠 AST 那一版已经被穿过一次了。
        "id": "MUT-E4-04b", "suite": "w4", "file": MONAPI,
        "title": "legacy 任务又拿到 200/progressPct=0(真 HTTP 臂)",
        "from": ("            if not plan_cell_ids:\n"
                 "                raise _safe_error(\"MONITORING_LEGACY_RUN\")"),
        "to": ("            if False and not plan_cell_ids:\n"
               "                raise _safe_error(\"MONITORING_LEGACY_RUN\")"),
        # 🔴 节点 id 带**类前缀** —— 这条判据在 ``class TestRunProgressHttp`` 里。
        #    runner 取的是 ``split("::", 1)[-1]``,所以类名留在里面。
        #    首跑我按裸函数名登,红集"多出/欠缺"各一条、指的是同一条判据 ——
        #    这种不匹配不是洞,但**必须报**:红集正则一旦对不上,
        #    "杀错了"与"名字写错了"在数字上长得一模一样。
        "expect": {
            "TestRunProgressHttp::"
            "test_progress_on_a_task_without_a_v2_plan_is_typed_not_a_zero",
        },
    },
    {
        "id": "MUT-E4-05", "suite": "e3", "file": REPORTAPI,
        "title": "五卡又按 brand_id 汇总全部历史监测",
        "from": "                        \"   AND created_at >= %s AND created_at <= %s\",",
        "to": "                        \"\",",
        "expect": {
            "test_e4_33_the_report_cards_query_is_window_scoped",
        },
    },
    {
        "id": "MUT-E4-06", "suite": "e3", "file": ACCEPT,
        "title": "受理证据只看三件组(审计列缺了也算 proven)",
        "from": "    missing = [c for c in REQUIRED_EVIDENCE_COLUMNS if row.get(c) is None]",
        "to": "    missing = [c for c in POINTER_COLUMNS if row.get(c) is None]",
        "expect": {
            "test_e4_11_any_missing_audit_column_makes_it_legacy_unproven"
            "[customer_confirmed_token_purpose]",
            "test_e4_11_any_missing_audit_column_makes_it_legacy_unproven"
            "[customer_confirmed_token_subject]",
            "test_e4_11_any_missing_audit_column_makes_it_legacy_unproven"
            "[customer_confirmed_actor]",
            "test_e4_11_any_missing_audit_column_makes_it_legacy_unproven"
            "[customer_confirmed_request_hash]",
            "test_e4_11_any_missing_audit_column_makes_it_legacy_unproven"
            "[customer_confirmed_request]",
            "test_e4_13_a_legacy_pointer_only_row_is_unproven",
        },
    },
    # ── E3-5 总超时计数口径 ──────────────────────────────────────────
    {
        "id": "MUT-E5-01", "suite": "woc", "file": TESTER,
        "title": "总超时日志退回数 done()(被取消的也算保留)",
        "from": "        kept_count = sum(1 for r in results_list if r is not None)",
        "to": "        kept_count = sum(1 for t in tasks if t.done())",
        "expect": {
            "test_e5_01_the_timeout_log_reports_kept_cells_not_terminal_tasks",
            "test_e5_02_the_kept_count_matches_the_settlement_count",
        },
    },
]

_NODE = re.compile(r"^(FAILED|ERROR)\s+(tests/\S+|\S*\.py::\S+)")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _preflight() -> None:
    free = shutil.disk_usage(ROOT).free
    if free < MIN_FREE_BYTES:
        raise SystemExit(f"🔴 可用空间 {free // 1024 // 1024} MB 不足 —— 拒绝开跑")
    stale = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.mutbak")]
    stale += [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.mutrestore")]
    if stale:
        raise SystemExit(f"🔴 树上有残留备份 {stale} —— 明文禁并发,先人工核对")
    for name, (rel, _dsn) in SUITES.items():
        if not (ROOT / rel).is_dir():
            raise SystemExit(f"🔴 判据包 {rel} 不在 —— {name} 的分母是空的")


def run(suite: str) -> tuple[set[str], int, str]:
    rel, dsn = SUITES[suite]
    env = dict(os.environ)
    env["TEST_DATABASE_URL"] = dsn
    env["DATABASE_URL"] = dsn
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", rel, "-q", "-p", "no:warnings",
         "--no-header", "-ra"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    red = {m.group(2).split("/")[-1].split("::", 1)[-1]
           for m in (_NODE.match(ln.strip()) for ln in out.splitlines()) if m}
    g = re.search(r"(\d+) passed", out)
    return red, (int(g.group(1)) if g else -1), out


def main() -> int:
    # 🔴 [Review 机制令 2026-08-26 ①] 树级排他锁 —— 本 runner 就地改源文件。
    #    合流尖 f5417c561 上被接线 census 抓到本 runner 漏接:
    #    ``scripts/test_mutation_tree_lock.py::test_every_in_place_runner_takes_the_tree_lock``
    #    按行为特征机械枚举"就地改源的 runner",E3 这一个新增时没接上。
    #    漏接的后果与 2026-08-26 三伤同形:并发下毒落在谁的基线上无法归属。
    with tree_lock("mutation_runner_e3_2026_08_26"):
        return _main_locked()


def _main_locked() -> int:
    _preflight()
    used = sorted({m["suite"] for m in MUTATIONS})
    base: dict[str, tuple[set[str], int]] = {}
    for suite in used:
        red, green, _out = run(suite)
        base[suite] = (red, green)
        print(f"基线[{suite}]:绿 {green} / 红 {len(red)} "
              f"{sorted(red) if red else ''}")
        if red:
            raise SystemExit(f"🔴 基线[{suite}]不全绿 —— 后面全部作废")

    killed = survived = mismatched = 0
    for mut in MUTATIONS:
        path = ROOT / mut["file"]
        original = path.read_bytes()
        text = original.decode("utf-8")
        n = text.count(mut["from"])
        if n != 1:
            raise SystemExit(
                f"🔴 {mut['id']} 锚点命中 {n} 次(要求 1)—— 停下人工核对,"
                f"不自行改锚点继续跑。file={mut['file']}\n"
                f"    anchor={mut['from'][:140]!r}")
        backup = path.with_suffix(path.suffix + ".mutbak")
        backup.write_bytes(original)
        try:
            mutated = text.replace(mut["from"], mut["to"], 1)
            if mutated == text:
                raise SystemExit(f"🔴 {mut['id']} 替换后文件无变化 —— 这不是变异")
            path.write_bytes(mutated.encode("utf-8"))
            if path.suffix == ".py":
                import ast
                try:
                    ast.parse(path.read_text(encoding="utf-8"))
                except SyntaxError as exc:
                    raise SystemExit(f"🔴 {mut['id']} 变异后语法不合法:{exc}")
            red, green, out = run(mut["suite"])
        finally:
            tmp = path.with_suffix(path.suffix + ".mutrestore")
            tmp.write_bytes(original)
            os.replace(tmp, path)
            if sha(path.read_bytes()) != sha(original):
                raise SystemExit(f"🔴 {path} 还原后字节不一致 —— 立刻停")
            backup.unlink(missing_ok=True)

        base_red, base_green = base[mut["suite"]]

        # ── 「整包炸」模式 ────────────────────────────────────────────
        # 有些变异的正确形态不是"几条判据红",而是**迁移当场拒绝应用** ⇒
        # session fixture 起不来 ⇒ 整包 error。硬抄那 30 个 node id 当期望集
        # 反而是手抄清单(漏一个都不会红)。这里改判**失败签名**:
        # 必须真的没有绿、且错误文本里出现那个对象名。
        want_sig = mut.get("expect_signature")
        if want_sig:
            if green >= base_green:
                mismatched += 1
                print(f"⚠️  {mut['id']} · {mut['title']} · 绿数没掉 "
                      f"({base_green} → {green}) —— 这一发没杀到任何东西")
            elif not red:
                mismatched += 1
                print(f"⚠️  {mut['id']} · {mut['title']} · 零红 —— 只是少跑了几条?")
            elif want_sig not in out:
                mismatched += 1
                print(f"⚠️  {mut['id']} · {mut['title']} · 掉绿了,但失败签名里"
                      f"找不到 {want_sig!r} —— 可能死在别的原因上")
            else:
                killed += 1
                print(f"💀 杀 {mut['id']} · {mut['title']} · 绿 {base_green}→{green} · "
                      f"红 {len(red)} · 失败签名命中 {want_sig!r}")
            continue

        new_red = red - base_red
        if not new_red:
            if green != base_green:
                raise SystemExit(
                    f"🔴 {mut['id']} 红 0 但绿数 {base_green} → {green} —— 有测试没跑")
            survived += 1
            print(f"🟢 存活 {mut['id']} · {mut['title']} —— 判据洞")
            continue
        if new_red == set(mut["expect"]):
            killed += 1
            print(f"💀 杀 {mut['id']} · {mut['title']} · 红集精确({len(new_red)} 条)")
        else:
            mismatched += 1
            print(f"⚠️  {mut['id']} · {mut['title']} · 红集**不匹配**")
            print(f"      多出:{sorted(new_red - set(mut['expect']))}")
            print(f"      欠缺:{sorted(set(mut['expect']) - new_red)}")
    print(f"\n合计 {len(MUTATIONS)} 发 · 精确杀 {killed} · 存活 {survived} · "
          f"红集不符 {mismatched}")
    print("外选栏:**0**(留给 Review 或另一窗供题 —— 自选清单证明不了判据面的完整性)")
    return 0 if survived == 0 and mismatched == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
