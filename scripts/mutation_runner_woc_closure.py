#!/usr/bin/env python
"""工单C(八项收口)的**撕锁自证**。

═══════════════════════════════════════════════════════════════════════
纪律(与本仓前几轮同源)
═══════════════════════════════════════════════════════════════════════
 · 先跑一发**不变异**的基线;基线不是全绿,后面全部作废;
 · 变异语法合法、语义精确(整段替成废代码只是 blunt kill,不算);
 · 每发声明一个**精确红集合**:少一条 = 该红的没红,多一条 = 有溢出,两种都 FAIL;
 · 还原用备份字节回写并**逐字节核对**,不用 ``git checkout``;
 · 锚点必须**唯一命中**(命中 0 次或 >1 次都当场 FAIL,不静默跳过)。

🔴 **自选 / 外选分开报数**:本仓记过「自己出题自己批改」——
   ``ORIGIN`` 字段标 ``self``(判据作者自选)或 ``audit``(判据作者之外挑的)。
   本文件目前**全部是 self**;外选那一栏留给 Review 亲毒窗口,
   报数时两栏分开,不许合并成一个"全杀"数字。

🔴 **禁并发**:本 runner 就地改源文件。同一棵树上同时跑第二个 runner
   (或另一个执行窗口在编辑)会让两边都拿到垃圾结果。
   本仓这棵树是三窗共用的 —— 跑之前先确认另外两窗没在编辑。

🔴 **库层变异必须同时打两处**(第一版只打一处,被自己的撕锁抓了个正着):

   · 只改**迁移文件** ⇒ ``ADD CONSTRAINT`` 有 ``IF NOT EXISTS`` 守卫,
     在**已经建好**的库上什么都不会发生,变异静默失效;
   · 只在**库里 DROP** ⇒ 判据底座的 session fixture **每次开跑都重放 052**,
     那条 DO 块当场把约束加回来 —— 变异在测试真正跑之前就被还原了。
     本仓记过这一条:「库层变异必须打在迁移文件上(session fixture 重放会
     悄悄还原变异)」。两句合起来才是完整的:**文件与库都要动**。

   所以 ``kind="schema"`` 一发同时做两件事:改迁移文件(让重放不再加回来)
   + DROP 真库里那条约束;还原时两件都还原,且 ADD 前先查存在性(幂等)。

跑法(仓库根)::

    WOC_DB=postgresql://geo_admin:testpw@localhost:55484/geo_defgeo_woc_test \\
    XB_DB=postgresql://geo_admin:testpw@localhost:55485/xbexec_seed_test \\
        python scripts/mutation_runner_woc_closure.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 🔴 [机制令 ① · 由工单B 顺手补] 本 runner 就地改源文件 —— 必须抢树级锁。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mutation_tree_lock import tree_lock  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]

# ── 被变异的生产文件 ────────────────────────────────────────────────────
SEL = ROOT / "api" / "selection_api.py"
MAT = ROOT / "services" / "defensive_geo" / "activation_materializer.py"
SAMPLE = ROOT / "services" / "diagnosis_sample_contract.py"
TESTER = ROOT / "tools" / "ai_visibility" / "ai_tester.py"
BRIDGE = ROOT / "services" / "defensive_geo" / "monitoring" / "run_ledger_bridge.py"
MONDB = ROOT / "db" / "monitoring_db.py"
LINKS = ROOT / "services" / "defensive_geo" / "customer_links.py"
REPAIR = ROOT / "services" / "defensive_geo" / "legal_repair.py"
ESTIMATE = ROOT / "services" / "defensive_geo" / "xiaobang" / "compute_estimate.py"
XBAPI = ROOT / "api" / "xiaobang_operations_api.py"
SERVER = ROOT / "server.py"
ADJ = ROOT / "services" / "settlement_adjudicator.py"
CENSUS = ROOT / "tests" / "defensive_geo_2026_08_21" / "test_model_pricing_census.py"
MIG052 = (ROOT / "db"
          / "migration_052_defgeo_activation_frozen_payer_2026_08_25.sql")

# ── 两把一次性库 + 两组判据面 ───────────────────────────────────────────
WOC_DB = os.environ.get(
    "WOC_DB", "postgresql://geo_admin:testpw@localhost:55484/geo_defgeo_woc_test")
XB_DB = os.environ.get(
    "XB_DB", "postgresql://geo_admin:testpw@localhost:55485/xbexec_seed_test")

WOC_SUITE = "tests/defgeo_woc_closure_2026_08_25"
XB_SUITE = "tests/xiaobang_execute_2026_08_20"

C1 = "test_c1_activation_frozen_payer_pg.py"
C2D = "test_c2_settlement_denominator.py"
C2T = "test_c2_total_timeout_degrade.py"
C3 = "test_c3_ledger_closure_pg.py"
C4 = "test_c4_customer_links_pg.py"
C5 = "test_c5_legal_repair_apply_pg.py"
C7R = "test_c7_cron_role_runtime.py"
C7S = "test_c7_settings_ghost.py"
C8 = "test_c8_adjudicator_streak_pg.py"
C6 = "test_woc_c6_gates_pg16.py"

ONLY = {x.strip() for x in os.environ.get("WOC_MUT_ONLY", "").split(",") if x.strip()}


def _nid(suite: str, fname: str, test: str) -> str:
    return f"{suite}/{fname}::{test}"


# ══════════════════════════════════════════════════════════════════════════
# 变异清单
# ══════════════════════════════════════════════════════════════════════════
MUTATIONS: list[dict] = [
    # ── C-1 ────────────────────────────────────────────────────────────
    {
        "id": "MUT-C1-01", "origin": "self", "file": MAT, "suite": "woc",
        "desc": "物化器**恢复现读** brands.owner_user_id(冻结值形同虚设)",
        "from": '    identity = frozen_identity(row)\n    tenant = identity["tenant_owner_id"]\n',
        "to": ('    identity = frozen_identity(row)\n'
               '    cur.execute("SELECT owner_user_id FROM brands WHERE id = %s",\n'
               '                (int(row["brand_id"]),))\n'
               '    tenant = int(cur.fetchone()["owner_user_id"])\n'),
        "expect": {
            _nid(WOC_SUITE, C1, "test_c1_10_brand_transfer_does_not_move_the_payer"),
            _nid(WOC_SUITE, C1,
                 "test_c1_11_materializer_no_longer_reads_the_live_brand_owner"),
        },
    },
    {
        "id": "MUT-C1-02", "origin": "self", "file": SEL, "suite": "woc",
        "desc": "入队**不再冻结** tenant/payer(回到 043 那种只写 brand 的形态)",
        "from": ("                tenant_owner_id=int(_tenant_owner),\n"
                 "                payer_user_id=int(_tenant_owner),\n"
                 "                payer_funding_policy=_payer.funding_policy,\n"
                 "                payer_principal_kind=_payer.principal_kind,\n"),
        "to": "",
        "expect": {
            _nid(WOC_SUITE, C1, "test_c1_01_production_confirm_freezes_tenant_and_payer"),
            _nid(WOC_SUITE, C1, "test_c1_02_admin_tenant_freezes_the_platform_leg"),
            _nid(WOC_SUITE, C1, "test_c1_10_brand_transfer_does_not_move_the_payer"),
        },
    },
    {
        "id": "MUT-C1-03", "origin": "self", "file": MAT, "suite": "woc",
        "desc": "转人工退回 ``_record_error``(= 8 轮之后才落 needs_review = 永久搁置)",
        "from": "            _record_needs_review(row, str(exc))\n",
        "to": "            _record_error(row, str(exc), terminal=True)\n",
        "expect": {
            _nid(WOC_SUITE, C1, "test_c1_20_legacy_rows_go_to_needs_review[None]"),
            _nid(WOC_SUITE, C1, "test_c1_20_legacy_rows_go_to_needs_review[990101]"),
            _nid(WOC_SUITE, C1,
                 "test_c1_21_needs_review_is_immediate_not_after_max_attempts"),
            _nid(WOC_SUITE, C1, "test_c1_22_hand_edited_policy_is_refused"),
        },
    },
    {
        "id": "MUT-C1-04", "origin": "self", "kind": "schema", "suite": "woc",
        "file": MIG052,
        "desc": "库层:摘掉 052 的「三列同生同死」CHECK(迁移文件 + 真库同时摘)",
        # ══════════════════════════════════════════════════════════════
        # 🔴 变异**弱化 CHECK 表达式**,不是摘掉 ADD CONSTRAINT
        # ══════════════════════════════════════════════════════════════
        # 摘掉 ADD 会让 052 **自己那道反向自证**(结尾那个查 pg_constraint 的
        # DO 块)RAISE ⇒ conftest 的迁移重放当场炸 ⇒ 整包 error。
        # 那是 blunt kill:什么都红,证明不了 test_c1_30 在守哪一格。
        #
        # 弱化成 ``CHECK (true)`` 则:约束仍然存在(自证过)、语法合法、
        # 语义精确地只丢掉「三列同生同死」这一条 —— 半冻结身份重新变得可表达。
        #
        # 配合 ``sql_break`` 先 DROP 掉库里那条**强**约束:不 DROP 的话
        # 重放时 ADD 被 IF NOT EXISTS 跳过,弱版根本装不上(变异静默失效)。
        "from": ("            ADD CONSTRAINT defgeo_activation_outbox_payer_group CHECK (\n"
                 "                (payer_user_id IS NULL\n"),
        "to": ("            ADD CONSTRAINT defgeo_activation_outbox_payer_group CHECK (\n"
               "                true OR\n"
               "                (payer_user_id IS NULL\n"),
        "sql_break": ("ALTER TABLE public.defgeo_activation_outbox "
                      "DROP CONSTRAINT IF EXISTS defgeo_activation_outbox_payer_group"),
        "sql_restore": (
            "ALTER TABLE public.defgeo_activation_outbox "
            "ADD CONSTRAINT defgeo_activation_outbox_payer_group CHECK ("
            "  (payer_user_id IS NULL AND payer_funding_policy IS NULL"
            "   AND payer_principal_kind IS NULL)"
            "  OR (payer_user_id IS NOT NULL AND payer_funding_policy IS NOT NULL"
            "      AND payer_principal_kind IS NOT NULL))"),
        "expect": {
            _nid(WOC_SUITE, C1, "test_c1_30_migration_052_forbids_half_frozen_identity"),
        },
    },
    # ── C-2 ────────────────────────────────────────────────────────────
    {
        "id": "MUT-C2-01", "origin": "self", "file": SAMPLE, "suite": "woc",
        "desc": "分母**不再收窄**到计费面(元宝重新进结算分母)",
        "from": "    narrowed = _billable_counts(av, planned)\n",
        "to": "    narrowed = None\n",
        "expect": {
            _nid(WOC_SUITE, C2D, "test_c2_01_non_billable_failure_must_not_reduce_the_bill"),
            _nid(WOC_SUITE, C2D,
                 "test_c2_02_billable_failure_must_not_be_diluted_by_a_non_billable_success"),
            _nid(WOC_SUITE, C2D, "test_c2_03_all_billable_engines_dead_is_still_a_full_refund"),
            _nid(WOC_SUITE, C2D,
                 "test_c2_12_probe_is_alive_narrowing_really_changes_the_number"),
            # 🔴 [出题订正] 原来把 C-2(c) 那条 `test_c2_21` 也列进来了 —— **错的**。
            #    它用的引擎是 ["dashscope","deepseek","doubao"],**三个全在计费面里**,
            #    于是收窄的前置("计划集里有非计费面")根本不成立,那条判据对
            #    C-2(a) 零区分力。它是纯 C-2(c) 判据。
            #    这是"自己出题自己批改"的典型误差:期望集写宽了,
            #    看起来像判据没守住,其实是我把一条不相干的判据算了进来。
        },
    },
    {
        "id": "MUT-C2-02", "origin": "self", "file": SAMPLE, "suite": "woc",
        "desc": "计费面取**运行集**而不是计价集(含元宝)—— 分母来源写错",
        "from": "        from config.ai_engines import DEFENSIVE_GEO_BILLABLE_ENGINES as _BILLABLE\n",
        "to": "        from config.ai_engines import DIAGNOSIS_RUNTIME_ENGINES as _BILLABLE\n",
        "expect": {
            _nid(WOC_SUITE, C2D,
                 "test_c2_00_billable_surface_comes_from_config_not_a_hand_written_list"),
            _nid(WOC_SUITE, C2D, "test_c2_01_non_billable_failure_must_not_reduce_the_bill"),
            _nid(WOC_SUITE, C2D,
                 "test_c2_02_billable_failure_must_not_be_diluted_by_a_non_billable_success"),
            _nid(WOC_SUITE, C2D, "test_c2_03_all_billable_engines_dead_is_still_a_full_refund"),
            _nid(WOC_SUITE, C2D,
                 "test_c2_12_probe_is_alive_narrowing_really_changes_the_number"),
            # 🔴 [出题订正 · 溢出补登记] `test_c2_11`(计划集全是计费面 ⇒ 零改动)
            #    在这一发下**应该**红,我漏登了:分母来源换成运行集之后,
            #    kimi 变成"非计费面" ⇒ 收窄对那条判据的样本也生效 ⇒
            #    `non_billable_engines_excluded` 不再是空的。
            #    那是**正确的**判别力,不是溢出噪声 —— 它恰好证明这条判据
            #    真的在守"分母取自哪一份清单"。
            _nid(WOC_SUITE, C2D, "test_c2_11_all_billable_engines_means_no_change_at_all"),
            # `test_c2_21` 同 MUT-C2-01 的订正:它三个引擎全在计费面,零区分力。
        },
    },
    {
        "id": "MUT-C2-03", "origin": "self", "file": TESTER, "suite": "woc",
        "desc": "总超时**退回整批丢弃**(``results_list = []``)",
        # 🔴 [工单 E3-5 · 2026-08-26] 锚点随本轮生产修改同步更新。
        #    旧锚含 ``done_count = sum(1 for t in tasks if t.done())`` 那一行,
        #    而 E3-5 把它删了(改成从 results_list 数真正留下来的格)。
        #    不同步 = 这一发**贴不上去**,而"没贴上去"在红集正则下与
        #    "贴上去了没杀掉"长得一模一样(本 runner 的锚点唯一性闸会停,
        #    但停下来之后仍然要有人改这里)。
        "from": "        results_list = []\n        for t in tasks:\n",
        "to": "        results_list = []\n        tasks = []\n        for t in tasks:\n",
        "expect": {
            _nid(WOC_SUITE, C2T,
                 "test_c2_20_total_timeout_keeps_the_platforms_that_already_answered"),
            _nid(WOC_SUITE, C2T,
                 "test_c2_21_degraded_result_settles_partially_not_as_a_full_refund"),
        },
    },
    {
        "id": "MUT-C2-04", "origin": "self", "file": TESTER, "suite": "woc",
        "desc": "任务退回**裸协程**(超时后拿不回任何已完成结果)",
        "from": ("    tasks = [\n"
                 "        asyncio.ensure_future(query_single(question, engine))\n"),
        "to": ("    tasks = [\n"
               "        query_single(question, engine)\n"),
        "expect": {
            _nid(WOC_SUITE, C2T,
                 "test_c2_20_total_timeout_keeps_the_platforms_that_already_answered"),
            _nid(WOC_SUITE, C2T,
                 "test_c2_21_degraded_result_settles_partially_not_as_a_full_refund"),
            # 🔴 [出题订正 · 溢出补登记] 裸协程没有 ``.done()``,收割那一段直接
            #    AttributeError ⇒ 连"全都超时"那一臂也一起红。我漏登了。
            #    这一发因此比预想的更"钝"(一处坏三条),但每一条红都是真的。
            _nid(WOC_SUITE, C2T,
                 "test_c2_22_everything_timed_out_is_still_a_full_refund"),
        },
    },
    # ── C-3 ────────────────────────────────────────────────────────────
    {
        "id": "MUT-C3-01", "origin": "self", "file": MONDB, "suite": "woc",
        "desc": "abandon 那一跳**摘掉**账本闭合(第三态原样复活)",
        "from": ("            close_unattempted_for_cells(\n"
                 "                cur, monitoring_cell_ids=reaped,\n"
                 "                reason_code=UNATTEMPTED_ABANDONED_REASON)\n"),
        "to": "",
        "expect": {
            _nid(WOC_SUITE, C3, "test_c3_01_abandoned_queued_cell_closes_the_ledger"),
            _nid(WOC_SUITE, C3, "test_c3_02_conservation_holds_after_closure"),
        },
    },
    {
        "id": "MUT-C3-02", "origin": "self", "file": BRIDGE, "suite": "woc",
        "desc": "作用域丢掉「从未派发」—— 派发过的格也被记成零 provider 调用",
        "from": "               AND c.provider_dispatched_at IS NULL\n",
        "to": "",
        "expect": {
            _nid(WOC_SUITE, C3, "test_c3_21_dispatched_cell_is_left_to_reclaim_orphans"),
        },
    },
    {
        "id": "MUT-C3-03", "origin": "self", "file": BRIDGE, "suite": "woc",
        "desc": "理由码闭集守卫失效(现场造码也照写)",
        "from": '    if str(reason_code or "") not in UNATTEMPTED_REASONS:\n',
        "to": "    if False:\n",
        "expect": {
            _nid(WOC_SUITE, C3, "test_c3_23_unregistered_reason_code_is_refused"),
        },
    },
    {
        "id": "MUT-C3-04", "origin": "self", "file": BRIDGE, "suite": "woc",
        "desc": "作用域丢掉「账本零行」—— 与 reclaim_orphans 重叠(两把锁叠同一路径)",
        "from": ("               AND NOT EXISTS (\n"
                 "                   SELECT 1 FROM public.{_ledger.TABLE} a\n"
                 "                    WHERE a.plan_cell_id = c.plan_hash\n"
                 "               )\n"),
        "to": "",
        "expect": {
            _nid(WOC_SUITE, C3, "test_c3_22_cell_that_already_has_a_ledger_row_is_untouched"),
        },
    },
    # ── C-4 ────────────────────────────────────────────────────────────
    {
        "id": "MUT-C4-01", "origin": "self", "file": LINKS, "suite": "woc",
        "desc": "报价单**退回** agent_quotes(那张表没有 brand_id)",
        "from": ('            """SELECT token, status, expires_at FROM keyword_selection_sessions\n'
                 "                WHERE brand_id = %s\n"
                 '                ORDER BY id DESC LIMIT 1""",\n'),
        "to": ('            """SELECT share_code AS token, \'quoted\' AS status,\n'
               "                      NULL AS expires_at FROM agent_quotes\n"
               "                WHERE brand_id = %s AND share_code IS NOT NULL\n"
               '                ORDER BY created_at DESC LIMIT 1""",\n'),
        "expect": {
            _nid(WOC_SUITE, C4, "test_c4_01_quote_proposal_points_at_the_selection_session"),
            _nid(WOC_SUITE, C4, "test_c4_02_portal_is_no_longer_blocked_by_the_quote_query"),
            _nid(WOC_SUITE, C4,
                 "test_c4_22_expired_selection_session_is_expired_not_not_ready"),
            _nid(WOC_SUITE, C4, "test_c4_23_confirmed_session_is_not_treated_as_expired"),
            _nid(WOC_SUITE, C4, "test_c4_20_expired_portal_token_is_not_available"),
            _nid(WOC_SUITE, C4, "test_c4_21_revoked_and_expired_are_different_statuses"),
            _nid(WOC_SUITE, C4,
                 "test_c4_30_panel_and_reissue_agree_on_the_object_in_a_multi_quote_brand"),
        },
    },
    {
        "id": "MUT-C4-02", "origin": "self", "file": LINKS, "suite": "woc",
        "desc": "**恢复整体吞异常**(查询失败静默 not_ready)",
        "from": "        raise CustomerLinksUnavailable(str(exc)) from exc\n",
        "to": "        pass\n",
        "expect": {
            _nid(WOC_SUITE, C4,
                 "test_c4_10_query_failure_raises_instead_of_silently_not_ready"),
        },
    },
    {
        "id": "MUT-C4-03", "origin": "self", "file": LINKS, "suite": "woc",
        "desc": "门户 ``expires_at`` **不再参与**状态判定(只看 is_active)",
        "from": "                elif isinstance(exp, _date) and exp < _date.today():\n",
        "to": "                elif False:\n",
        "expect": {
            _nid(WOC_SUITE, C4, "test_c4_20_expired_portal_token_is_not_available"),
        },
    },
    {
        "id": "MUT-C4-04", "origin": "self", "file": LINKS, "suite": "woc",
        "desc": "重签**另写一条** ORDER BY(面板与重签又不是同一个对象)",
        "from": "        quote_id = canonical_quote_id(cur, brand_id)\n        conn.rollback()\n",
        "to": ("        cur.execute(\n"
               '            "SELECT id FROM quotes WHERE brand_id = %s AND deleted_at IS NULL "\n'
               '            "ORDER BY created_at ASC LIMIT 1", (brand_id,))\n'
               "        _r = cur.fetchone()\n"
               '        quote_id = int(_r["id"]) if _r else None\n'
               "        conn.rollback()\n"),
        "expect": {
            _nid(WOC_SUITE, C4,
                 "test_c4_30_panel_and_reissue_agree_on_the_object_in_a_multi_quote_brand"),
        },
    },
    # ── C-5 ────────────────────────────────────────────────────────────
    {
        "id": "MUT-C5-01", "origin": "self", "file": REPAIR, "suite": "woc",
        "desc": "「用这一句」**不落库**(退回那个零消费者的形态)",
        "from": ('    cur.execute("UPDATE articles SET content = %s WHERE id = %s",\n'
                 "                (new_content, int(article_id)))\n"),
        "to": "",
        "expect": {
            _nid(WOC_SUITE, C5, "test_c5_01_apply_changes_the_article_hash_end_to_end"),
            _nid(WOC_SUITE, C5,
                 "test_c5_02_the_old_frozen_snapshot_can_no_longer_be_dispatched"),
            _nid(WOC_SUITE, C5,
                 "test_c5_03_only_that_passage_changes_the_rest_is_byte_identical"),
            _nid(WOC_SUITE, C5, "test_c5_12_offset_drift_falls_back_to_the_exact_string"),
        },
    },
    {
        "id": "MUT-C5-02", "origin": "self", "file": REPAIR, "suite": "woc",
        "desc": "落库前**不再**过尺子(手改回禁词也照落)",
        "from": "    hits = _hits(final)\n    if hits:\n",
        "to": "    hits = ()\n    if hits:\n",
        "expect": {
            _nid(WOC_SUITE, C5,
                 "test_c5_10_a_hand_edited_sentence_that_still_violates_is_refused"),
        },
    },
    {
        "id": "MUT-C5-03", "origin": "self", "file": REPAIR, "suite": "woc",
        "desc": "候选文案**退回**「可以直接用」",
        "from": ('            "note": ("已避开这条规则点名的词。"\n'
                 '                     "其它说法(数字、门店数、标题)我们这次没核,发之前你再看一眼。"),\n'),
        "to": '            "note": "已避开这条规则点名的说法,可以直接用,也可以在这基础上改",\n',
        "expect": {
            _nid(WOC_SUITE, C5, "test_c5_20_candidate_note_no_longer_says_it_is_ready_to_use"),
        },
    },
    # ── C-6(跑 xiaobang 那把库)──────────────────────────────────────
    {
        "id": "MUT-C6-01", "origin": "self", "file": ESTIMATE, "suite": "xb",
        "desc": "0 算力**重新可确认**(只挡 None)",
        "from": "    if amount is None or amount <= 0:\n",
        "to": "    if amount is None:\n",
        "expect": {
            _nid(XB_SUITE, C6, "test_c6_10_zero_price_is_not_shown_as_confirmable"),
            _nid(XB_SUITE, C6,
                 "test_c6_11_zero_price_confirm_is_refused_with_zero_side_effect"),
            _nid(XB_SUITE, C6, "test_c6_12_the_intent_really_carried_a_zero_not_a_null"),
            _nid(XB_SUITE, C6, "test_c6_13_execute_side_refuses_with_409_not_a_500"),
        },
    },
    {
        "id": "MUT-C6-02", "origin": "self", "file": XBAPI, "suite": "xb",
        "desc": "confirm 侧**摘掉**可重核门(存量 intent 又能自比较放行)",
        "from": ("            # 🔴 [工单 C-6] 先问「这三个 hash 是不是真的重算出来的」,再比。\n"
                 "            #    缺 canonical_input 时下面那三支是 x == x(恒真),\n"
                 "            #    比了等于没比 —— 那正是「确认 A 执行 B」的入口。\n"
                 "            _assert_reverifiable(live)\n"),
        "to": "",
        "expect": {
            _nid(XB_SUITE, C6, "test_c6_01_legacy_intent_cannot_be_confirmed"),
            _nid(XB_SUITE, C6, "test_c6_04_a_legacy_intent_would_have_passed_the_same_drift"),
        },
    },
    {
        "id": "MUT-C6-03", "origin": "self", "file": XBAPI, "suite": "xb",
        "desc": "execute 侧**摘掉**同一道门(上线前已确认的存量单照跑)",
        "from": ("                # 🔴 [工单 C-6] execute 侧同一道门。**两处都要**:\n"
                 "                #    只在 confirm 拦的话,本次上线**之前**已经拿到回执的那批\n"
                 "                #    存量 intent 仍然可以执行,而它们恰恰是证不了\n"
                 "                #    「确认对象 == 执行对象」的那一批。\n"
                 "                _assert_reverifiable(live)\n"),
        "to": "",
        "expect": {
            _nid(XB_SUITE, C6, "test_c6_02_legacy_intent_cannot_be_executed_either"),
        },
    },
    # ── C-7 ────────────────────────────────────────────────────────────
    {
        "id": "MUT-C7-01", "origin": "self", "file": SERVER, "suite": "woc",
        "desc": "幽灵配置**加回**保存链(每次保存设置必 AttributeError ⇒ 500)",
        "from": "            # [工单 C-7] `monitoring_tasks=` 已摘 —— 见 SettingsUpdateRequest 那段说明。\n",
        "to": "            monitoring_tasks=current.monitoring_tasks,\n",
        "expect": {
            _nid(WOC_SUITE, C7S,
                 "test_c7_02_settings_save_really_runs_and_does_not_write_the_ghost"),
            _nid(WOC_SUITE, C7S, "test_c7_04_the_save_handler_no_longer_reads_it"),
            _nid(WOC_SUITE, C7S, "test_c7_06_the_orphan_census_now_covers_the_repo_root"),
        },
    },
    {
        "id": "MUT-C7-02", "origin": "self", "file": SERVER, "suite": "woc",
        "desc": "摘掉 ROLE 闸(4 个 web worker 各跑一遍 cron —— 4× 病根复活)",
        "from": "    if _IS_CRON_ROLE:\n        from api.scheduler import register_v32_core_tasks\n",
        "to": "    if True:\n        from api.scheduler import register_v32_core_tasks\n",
        "expect": {
            _nid(WOC_SUITE, C7R, "test_c7_51_web_role_registers_nothing"),
        },
    },
    {
        "id": "MUT-C7-03", "origin": "self", "file": CENSUS, "suite": "woc",
        "desc": "孤儿普查扫描面**退回**旧 glob(仓库根又扫不到)",
        "from": '    return not any(part in _ORPHAN_EXCLUDED_TREES for part in rel.split("/"))\n',
        "to": '    return rel.startswith(("api/", "services/", "tools/"))\n',
        "expect": {
            _nid(WOC_SUITE, C7S, "test_c7_06_the_orphan_census_now_covers_the_repo_root"),
        },
    },
    # ── C-8 ────────────────────────────────────────────────────────────
    {
        "id": "MUT-C8-01", "origin": "self", "file": ADJ, "suite": "woc",
        "desc": "streak **退回逐行数**(一单双行 ⇒ 阈值 N 在第 N/2 单就触发)",
        "from": "        if token in seen:\n",
        "to": "        if False:\n",
        "expect": {
            _nid(WOC_SUITE, C8, "test_c8_01_one_order_counts_once_not_twice"),
            _nid(WOC_SUITE, C8, "test_c8_02_the_current_order_is_never_double_counted"),
            _nid(WOC_SUITE, C8, "test_c8_03_a_different_cause_breaks_the_streak"),
            _nid(WOC_SUITE, C8, "test_c8_10_n_minus_one_orders_do_not_escalate"),
            # 🔴 [出题订正 · 溢出补登记] `test_c8_11`(第 N 单**触发**)在这一发下
            #    也红:逐行数把 N-1 单历史算成 2(N-1) 行,+1 之后是 2N-1,
            #    而那条判据断言的是 `streak == N`。它本来就是这条阈值的另一侧,
            #    漏登是我的疏忽 —— 边界两侧必须成对出现在期望集里。
            _nid(WOC_SUITE, C8, "test_c8_11_the_nth_order_escalates"),
            # 🔴 [出题订正 · 撤下] `test_c8_12` 对本发**零区分力**:
            #    它只写一条 escalated 行,逐行数与按单去重都得 1+1=2。
            #    它守的是另一件事(升级过的单仍然算一次同因发生),不是双计。
            #    把一条零区分力的判据写进期望集,会让"该红没红"这个信号失真。
        },
    },
    {
        "id": "MUT-C8-02", "origin": "self", "file": ADJ, "suite": "woc",
        "desc": "不排除当前单(崩在中途重跑时同一单又被数两次)",
        "from": "    if current_run_token:\n        seen.add(str(current_run_token))\n",
        "to": "    if False:\n        seen.add(str(current_run_token))\n",
        "expect": {
            _nid(WOC_SUITE, C8, "test_c8_02_the_current_order_is_never_double_counted"),
        },
    },
]


# ══════════════════════════════════════════════════════════════════════════
# 跑批
# ══════════════════════════════════════════════════════════════════════════
def _env_for(suite: str) -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["TEST_DATABASE_URL"] = WOC_DB if suite == "woc" else XB_DB
    return env


#: 🔴 每一发的**完整** pytest 输出落盘。
#
#  这一格是被自己抓出来的:第一版只留 FAILED 行,于是同一发变异在两次跑里
#  给出不同的红集合时,我**无从判断**那是"被杀"还是"环境炸了" ——
#  而本仓铁律是「两边都红先比错误签名」,没有输出就没有签名可比。
#  产物路径打印出来,Review 复核时可以直接翻。
_ARTIFACT_DIR = ROOT / ".woc_mutation_artifacts"


def _run_suite(suite: str, *, tag: str = "baseline") -> tuple[set[str], Path]:
    """跑一组判据,返回(**失败 nodeid 集合**, 输出落盘路径)。"""
    target = WOC_SUITE if suite == "woc" else XB_SUITE
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "--no-header",
         "-p", "no:cacheprovider", "-rA"],
        cwd=str(ROOT), env=_env_for(suite), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=3600)
    out = (proc.stdout or "") + (proc.stderr or "")

    _ARTIFACT_DIR.mkdir(exist_ok=True)
    artifact = _ARTIFACT_DIR / f"{tag}.{suite}.log"
    artifact.write_text(
        f"# rc={proc.returncode}\n# target={target}\n"
        f"# TEST_DATABASE_URL={_env_for(suite)['TEST_DATABASE_URL']}\n\n{out}",
        encoding="utf-8", errors="replace")

    red: set[str] = set()
    for line in out.splitlines():
        m = re.match(r"^(?:FAILED|ERROR)\s+(\S+?::\S+)", line.strip())
        if m:
            red.add(m.group(1).replace("\\", "/"))
    if not red and " passed" not in out:
        raise SystemExit(
            f"[{suite}] pytest 没有产出可解析的结果(见 {artifact}):\n{out[-3000:]}")
    return red, artifact


def _sql(suite: str, statement: str) -> None:
    import psycopg2

    conn = psycopg2.connect(WOC_DB if suite == "woc" else XB_DB)
    conn.autocommit = True
    try:
        conn.cursor().execute(statement)
    finally:
        conn.close()


#: 崩溃安全:``_apply`` 一旦动过文件就登记在这里,进程无论怎么退出都还原。
#: 🔴 工单B 2026-08-25 记过一次真实事故:runner 崩在写回之前,
#:    把**被测源文件留成 0 字节**。所以还原不依赖正常控制流。
_INFLIGHT: dict[Path, str] = {}


def _apply(mut: dict) -> Path | None:
    """就地变异。返回备份路径。

    ``kind="schema"`` **两处都动**:迁移文件 + 真库。
    只动一处会静默失效(见 MUT-C1-04 那段说明)。
    """
    if mut.get("kind") == "schema":
        _sql(mut["suite"], mut["sql_break"])          # 先把库里那条强约束摘掉
    path: Path = mut["file"]
    src = path.read_text(encoding="utf-8", newline="")
    hits = src.count(mut["from"])
    if hits != 1:
        raise SystemExit(
            f"{mut['id']} 锚点命中 {hits} 次(必须恰 1)—— 锚已过期,"
            "这一发会静默失效,而失效与被杀在报表上长得一样")
    backup = path.with_suffix(path.suffix + ".mutbak")
    shutil.copyfile(path, backup)
    _INFLIGHT[path] = src                      # 原文进内存,崩了也能还原
    path.write_text(src.replace(mut["from"], mut["to"], 1),
                    encoding="utf-8", newline="")
    return backup


def _restore(mut: dict, backup: Path | None) -> None:
    path: Path = mut["file"]
    original = _INFLIGHT.pop(path, None)
    assert original is not None, f"{mut['id']} 原文没登记 —— 拒绝盲目还原"
    path.write_text(original, encoding="utf-8", newline="")
    # 🔴 **逐字节核对**还原到位。不用 ``git checkout``:这棵树是三窗共用的,
    #    checkout 会顺手把别人的在途改动一起撤掉。
    assert path.read_text(encoding="utf-8", newline="") == original, \
        f"{mut['id']} 还原后与原文不逐字节相同 —— 停机人工核"
    if backup is not None and backup.exists():
        backup.unlink()
    if mut.get("kind") == "schema":
        # 🔴 库那一半:**先摘再加**,而且加之前查存在性。
        #    第一版直接 ADD,撞上"判据 session 已经按原迁移把强约束加回来了"
        #    ⇒ DuplicateObject ⇒ runner 当场崩(实录)。
        #    库层还原必须幂等 —— 它面对的是一个**别人也会写**的对象。
        _sql(mut["suite"], mut["sql_break"])
        _sql(mut["suite"], mut["sql_restore"])


def _emergency_restore() -> None:
    """无论怎么退出都把动过的文件放回去。"""
    for path, original in list(_INFLIGHT.items()):
        try:
            path.write_text(original, encoding="utf-8", newline="")
            print(f"[emergency-restore] {path.name} 已还原")
        except Exception as exc:                          # noqa: BLE001
            print(f"[emergency-restore] 🔴 {path} 还原失败:{exc}")
        _INFLIGHT.pop(path, None)


def main() -> int:
    # 🔴 [机制令 ①] 树级排他锁。
    with tree_lock("mutation_runner_woc_closure"):
        return _main_locked()


def _main_locked() -> int:
    print("=" * 78)
    print("工单C 撕锁 · 基线(不变异)")
    print("=" * 78)
    base_woc, a1 = _run_suite("woc", tag="baseline")
    base_xb, a2 = _run_suite("xb", tag="baseline")
    if base_woc or base_xb:
        print("🔴 基线不是全绿,后面全部作废:")
        for n in sorted(base_woc | base_xb):
            print("   ", n)
        print(f"   完整输出:{a1} / {a2}")
        return 2
    print("基线全绿 ✓\n")

    killed: list[str] = []
    survived: list[str] = []
    for mut in MUTATIONS:
        if ONLY and mut["id"] not in ONLY:
            continue
        print("-" * 78)
        print(f"{mut['id']} [{mut['origin']}] {mut['desc']}")
        backup = _apply(mut)
        try:
            red, artifact = _run_suite(mut["suite"], tag=mut["id"])
        finally:
            _restore(mut, backup)
        want = set(mut["expect"])
        missing = want - red
        extra = red - want
        if not missing and not extra:
            killed.append(mut["id"])
            print(f"  ✓ 精确命中 {len(want)} 条")
        else:
            survived.append(mut["id"])
            if missing:
                print(f"  ✗ 该红没红({len(missing)}):")
                for n in sorted(missing):
                    print("      -", n)
            if extra:
                print(f"  ✗ 溢出({len(extra)}):")
                for n in sorted(extra):
                    print("      +", n)
            # 🔴 不精确时**必须**能翻到原因:是被杀、还是环境炸了,
            #    只看 nodeid 分不出来(本仓铁律:两边都红先比错误签名)。
            print(f"      完整输出:{artifact}")

    print("=" * 78)
    self_n = [m for m in MUTATIONS if m["origin"] == "self"
              and (not ONLY or m["id"] in ONLY)]
    audit_n = [m for m in MUTATIONS if m["origin"] == "audit"
               and (not ONLY or m["id"] in ONLY)]
    print(f"自选 {len(self_n)} 发 · 外选 {len(audit_n)} 发")
    print(f"精确杀 {len(killed)} · 未精确 {len(survived)}")
    if survived:
        print("未精确清单:", ", ".join(survived))
    return 0 if not survived else 1


if __name__ == "__main__":
    import atexit

    atexit.register(_emergency_restore)
    try:
        _rc = main()
    except BaseException:
        _emergency_restore()
        raise
    raise SystemExit(_rc)
