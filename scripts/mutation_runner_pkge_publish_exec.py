#!/usr/bin/env python
"""包E(发布链执行侧)的**撕锁自证**。

═══════════════════════════════════════════════════════════════════════
纪律(与本仓前几轮同源)
═══════════════════════════════════════════════════════════════════════
 · 先跑一发**不变异**的基线;基线不是全绿,后面全部作废;
 · 变异语法合法、语义精确(整段替成废代码只是 blunt kill,不算);
 · 每发声明一个**精确红集合**:少一条 = 该红的没红,多一条 = 有溢出,两种都 FAIL;
 · 还原用备份字节回写并**逐字节核对**,不用 ``git checkout``。

跑法(仓库根)::

    TEST_DATABASE_URL=postgresql://geo_admin:testpw@localhost:55480/geo_defgeo_pkge_test \\
        python scripts/mutation_runner_pkge_publish_exec.py
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]

SCHED = ROOT / "api" / "scheduler.py"
PUBAPI = ROOT / "api" / "defensive_publish_api.py"
WORKER = ROOT / "services" / "defensive_geo" / "publish" / "publish_worker.py"
DISPATCH = ROOT / "services" / "defensive_geo" / "publish" / "publish_outbox.py"
RECON = ROOT / "services" / "defensive_geo" / "publish" / "reconciler.py"
MATER = ROOT / "services" / "defensive_geo" / "activation_materializer.py"
POLICY = ROOT / "services" / "defensive_geo" / "publish" / "execution_budget_policy.py"
TRANSPORT = ROOT / "services" / "defensive_geo" / "publish" / "provider_transport.py"
STORE = ROOT / "services" / "defensive_geo" / "publish" / "store.py"
#: 🔴 [R1] 本包**没改**这个文件 —— 但包E 的执行器、收敛器第 7 项与客户面 slot
#:    投影全都读它那张钱向表,而包E 原来一条判据都够不到。Review 拿 MUT-24
#:    那发毒把这个洞逼了出来(我亲手复现:51 全绿,毒活着)。
SETTLE = ROOT / "services" / "defensive_geo" / "publish" / "publish_settlement.py"
#: [R2] 付款方判别位(诊断链与发布预算链**同源消费**的那一份)。
PAYER = ROOT / "services" / "defensive_geo" / "payer_classification.py"
#: [R3] Z-1 人工核验(平台腿的结算与队列谓词都长在这里)。
REVIEW = ROOT / "services" / "defensive_geo" / "publish" / "settlement_review.py"
FUND = ROOT / "services" / "defensive_geo" / "publish" / "publish_funding.py"
COPY = ROOT / "services" / "defensive_geo" / "copy_registry.py"
ACTIONS = ROOT / "services" / "defensive_geo" / "publish" / "action_registry.py"

PYTEST_TARGETS = ("tests/defensive_geo_pkge_2026_08_24",)

DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://geo_admin:testpw@localhost:55480/geo_defgeo_pkge_test",
)

WIRE = "test_wiring_lock_runtime.py"
CHAIN = "test_real_chain_pg.py"
BUDGET = "test_p1_2_budget_and_envelope_pg.py"
OWN = "test_p1_4_ownership_pg.py"
VALUE = "test_settlement_direction_value_lock.py"
R2 = "test_r2_platform_leg_pg.py"

#: [R2] 判别反转会把**每一个**租户都改判成平台腿 ⇒ 整条主链换一条钱腿。
#: 溢出是结构性的(与 MUT-10 同族),所以红集合按机械规律生成,不手抄。
_R2_EQUIV_01 = {
    f"py::{R2}::test_r2_01_extracted_classifier_is_equivalent_to_the_old_body[{p}-{u}]"
    for p, users in (
        # platform_uid=None / 424242:两个平台账号轴上"本人就是平台账号"永不成立,
        # 所以 7 个身份格全部由 is_admin 决定 ⇒ 反转后 7 格全错。
        ("None", ("admin", "admin_and_platform", "empty", "no_user_id",
                  "none", "ordinary", "platform_account_self")),
        ("424242", ("admin", "admin_and_platform", "empty", "no_user_id",
                    "none", "ordinary", "platform_account_self")),
        # platform_uid=9705:``platform_account_self`` 与 ``admin_and_platform``
        # 两格由**第二条**规则(本人就是平台账号)兜住,反转 is_admin 也仍然对 ⇒ 不红。
        ("9705", ("admin", "empty", "no_user_id", "none", "ordinary")),
    )
    for u in users
}
_R2_EQUIV_02 = {
    f"py::{R2}::test_r2_02_endpoint_adapter_is_equivalent_to_the_old_body[{u}]"
    # 端点侧固定用当前环境里的平台账号(=9705),同上:两格由第二条规则兜住。
    for u in ("admin", "empty", "no_user_id", "none", "ordinary")
}
#: [R3] 平台腿的四条真链臂 —— 它们都走 ``_ready(who="adminowner")`` 建单,
#: 所以任何"让 admin 品牌主拿不到平台腿预算"的变异都会连带打红它们(结构性溢出)。
_R3_PLATFORM_ARMS = {
    f"py::{R2}::{n}" for n in (
        "test_r2_23_platform_leg_release_arm_refunds_the_platform_account",
        "test_r2_24_a_settled_platform_leg_is_never_settled_twice",
        "test_r2_25_admin_review_commit_settles_the_platform_freeze",
        "test_r2_26_admin_review_release_refunds_the_platform_freeze",
    )
}
#: [R4] 广告法门那条**活路径**的两条腿。它们同样走 ``_ready``/``_confirmed`` 建单,
#: 所以"拿不到预算 / 判错付款方 / 终态标记不落"这类变异会连带打红它们。
_R4_LEGAL_ARMS = {
    f"py::{R2}::test_r4_01a_legal_hit_on_a_wallet_leg_refunds_the_customer",
    f"py::{R2}::test_r4_01b_legal_hit_on_a_platform_leg_refunds_the_platform_account",
}
_R4_LEGAL_PLATFORM_ARM = {
    f"py::{R2}::test_r4_01b_legal_hit_on_a_platform_leg_refunds_the_platform_account",
}
_R2_WHOLE_CHAIN = {
    f"py::{CHAIN}::{n}" for n in (
        "test_01_confirm_freezes_exact_points",
        "test_02_dispatch_claims_and_calls_provider_once",
        "test_03_verified_publication_commits_once",
        "test_04_authoritative_rejection_releases_once",
        "test_05_unknown_outcome_never_releases",
        "test_10_window1_replay_after_claim_returns_same_root",
        "test_12_window3_recovery_never_retransmits",
        "test_13_window4_outcome_without_settlement_converges",
        "test_14_marker_is_durable_before_the_provider_call",
        "test_15_article_edited_after_confirm_is_not_published",
        "test_20_reconcile_requeues_frozen_but_never_dispatched",
        "test_21_reconcile_holds_external_started_without_outcome",
        "test_22_reconcile_releases_when_queue_gave_up_and_never_dispatched",
        "test_22b_external_started_is_never_released_even_when_the_queue_gave_up",
        "test_23_transport_not_configured_defers_instead_of_burning_the_attempt",
        "test_24_platform_cost_leg_settles_on_the_platform_account",
        "test_24b_non_platform_leg_outside_frozen_is_never_released",
    )
}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def run_criteria() -> tuple[set[str], int, int]:
    """跑判据,回 (**归一化红集合**, 绿数, 退出码)。"""
    env = dict(os.environ, TEST_DATABASE_URL=DB_URL, PYTHONIOENCODING="utf-8")
    p = subprocess.run(
        [sys.executable, "-m", "pytest", *PYTEST_TARGETS, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    out = (p.stdout or "") + (p.stderr or "")
    red: set[str] = set()
    for line in out.splitlines():
        # 🔴 token 必须长得像 node id;只写 \\S+ 会把 caplog 行也收进红集合。
        m = re.match(r"^(FAILED|ERROR)\s+([\w./" + "\\\\" + r"\-]+\.py(?:::\S+)?)$",
                     line.strip())
        if m:
            red.add("py::" + m.group(2).replace("\\", "/").split("/")[-1])
    m = re.search(r"(\d+) passed", out)
    green = int(m.group(1)) if m else 0
    return red, green, p.returncode


MUTATIONS = [
    # ══════════════════════════════════════════════════════════════════
    # ① 接线(工单① 点名的那一族)
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-01", "file": SCHED,
        "desc": "把发布派发从调度元组里**删掉**(= 工单点名的「从调度摘掉」)",
        "from": '''        (
            "defgeo_publish_dispatch",
            "services.defensive_geo.publish.publish_worker:dispatch_pending_sync",
            20,
            "防御型 GEO 发布派发（每20秒·领取 outbox → 外调 → 落 canonical outcome）",
            True,
        ),
''',
        "to": "",
        "expect": {
            f"py::{WIRE}::test_01_job_is_registered_at_runtime[defgeo_publish_dispatch]",
            f"py::{WIRE}::test_02_job_is_bound_to_the_exact_function[defgeo_publish_dispatch]",
            f"py::{WIRE}::test_03_advancing_jobs_are_not_in_the_gated_path",
            f"py::{WIRE}::test_04_dispatch_and_reconcile_are_fail_closed",
            # 🔴 第一版漏了这条,精确匹配当场报"溢出"。它红是**对的**:
            #    test_05 的末尾有一句反向对照 ``assert 'defgeo_publish_dispatch' in stub.jobs``
            #    (证明注入只打中了告警那一条)。派发被删掉之后那句自然不成立。
            #    期望错了不等于锁弱 —— 这是精确匹配相对 any() 的全部价值。
            f"py::{WIRE}::test_05_alert_job_is_not_fail_closed",
        },
    },
    {
        "id": "MUT-02", "file": SCHED,
        "desc": "🔴 ``if False:`` —— **AST/grep 锁杀不掉的那一发**(G9 教训)",
        "from": "    for _job_id, _entry, _seconds, _name, _fail_closed in (\n"
                '        (\n            "defgeo_activation_materialize",',
        "to": "    for _job_id, _entry, _seconds, _name, _fail_closed in () if False else (\n"
              '        (\n            "defgeo_activation_materialize_DISABLED",',
        "expect": {
            f"py::{WIRE}::test_01_job_is_registered_at_runtime[defgeo_activation_materialize]",
            f"py::{WIRE}::test_02_job_is_bound_to_the_exact_function"
            f"[defgeo_activation_materialize]",
            f"py::{WIRE}::test_03_advancing_jobs_are_not_in_the_gated_path",
        },
    },
    {
        "id": "MUT-03", "file": SCHED,
        "desc": "派发 job 绑到**只读告警**上(id 对了,干活的换人了)",
        "from": '"services.defensive_geo.publish.publish_worker:dispatch_pending_sync"',
        "to": '"services.defensive_geo.publish.settlement_alerts:scan_and_alert"',
        "expect": {
            f"py::{WIRE}::test_02_job_is_bound_to_the_exact_function[defgeo_publish_dispatch]",
            # 🔴 实测修正:``test_04`` **不**红。它注入的是 publish_worker 的导入失败,
            #    而 reconcile 那条仍然 import 同一个模块且同样 fail-closed ⇒
            #    RuntimeError 照样抛。这一发没有破坏 fail-closed,只是换了干活的人。
            #    红的是 test_05 的反向对照:注入 settlement_alerts 失败之后,
            #    派发 job 因为改绑到它身上而注册不上。
            f"py::{WIRE}::test_05_alert_job_is_not_fail_closed",
        },
    },
    {
        "id": "MUT-04", "file": SCHED,
        "desc": "把 Z-1 告警从无条件段删掉(等价于搬回 gated:生产 auto_monitor_enabled=0 ⇒ 永不注册)",
        "from": '''        (
            "defgeo_publish_settlement_pending_alert",
            "services.defensive_geo.publish.settlement_alerts:scan_and_alert",
            3600,
            "防御型 GEO 发布结算待核验超 7 天告警（每小时·只读 + upsert_alert）",
            False,
        ),
''',
        "to": "",
        "expect": {
            f"py::{WIRE}::test_01_job_is_registered_at_runtime"
            f"[defgeo_publish_settlement_pending_alert]",
            f"py::{WIRE}::test_02_job_is_bound_to_the_exact_function"
            f"[defgeo_publish_settlement_pending_alert]",
            f"py::{WIRE}::test_03_advancing_jobs_are_not_in_the_gated_path",
            # 🔴 实测修正:``test_05`` **不**红。整条告警都不注册了,
            #    它注入的 settlement_alerts 导入失败根本不会被触发,
            #    两句断言(不 fail-closed、告警不在册)反而都成立。
            #    这说明 test_05 守的是"不对称"而不是"这条在不在" —— 分工正确。
        },
    },
    # ══════════════════════════════════════════════════════════════════
    # ② kill window
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-05", "file": WORKER,
        "desc": "窗口③:不再把 marker 的提交钩子传下去(marker 与外调同事务)",
        "from": "            on_external_start_committed=conn.commit,",
        "to": "            on_external_start_committed=None,",
        "expect": {
            f"py::{CHAIN}::test_14_marker_is_durable_before_the_provider_call",
        },
    },
    {
        "id": "MUT-06", "file": DISPATCH,
        "desc": "窗口③:恢复路径**盲重传**(把「已有 marker 就不重传」那段摘掉)",
        "from": "    if command.get(\"external_start_at\") is not None:",
        "to": "    if False and command.get(\"external_start_at\") is not None:",
        "expect": {
            f"py::{CHAIN}::test_12_window3_recovery_never_retransmits",
        },
    },
    {
        "id": "MUT-07", "file": DISPATCH,
        "desc": "🔴 把外调异常当成「确认零接单」(§12.1 明令禁止的那一种退款)",
        "from": '            canonical_publication_state="unknown",\n'
                '            funding_state="pending_reconciliation",\n'
                '            command_state="settlement_pending",\n'
                '            status_reason="正在向平台核实结果，费用已冻结",\n'
                "        )\n"
                '        logger.error("[defgeo-publish] %s 外调抛异常,转未知态:%s", command_id, exc)',
        "to": '            canonical_publication_state="rejected_no_effect",\n'
              '            funding_state="frozen",\n'
              '            command_state="failed",\n'
              '            status_reason="正在向平台核实结果，费用已冻结",\n'
              "        )\n"
              '        logger.error("[defgeo-publish] %s 外调抛异常,转未知态:%s", command_id, exc)',
        "expect": {
            f"py::{CHAIN}::test_05_unknown_outcome_never_releases",
            # [R2 实测修正] 平台腿那一臂也把"外调结果未知"这一格驱动到了:
            # 变异把它写成 ``rejected_no_effect``,判据钉的是"未知族"。
            f"py::{R2}::test_r2_34_unknown_provider_outcome_on_a_platform_leg_does_not_abort",
        },
    },
    {
        "id": "MUT-08", "file": WORKER,
        "desc": "DEL-08:不再核对冻结正文指纹(改过的稿也照发)",
        "from": "    if actual != str(article_hash):",
        "to": "    if False and actual != str(article_hash):",
        "expect": {
            f"py::{CHAIN}::test_15_article_edited_after_confirm_is_not_published",
        },
    },
    {
        # 🔴 第一版这一发写成把 ``except`` 子句里的异常类名改错 —— 那是 **blunt kill**:
        #    任何异常经过这里都会先抛 AttributeError,于是顺带打红了两条不相干的判据
        #    (溢出)。变异必须语义精确:这里只把「放回队列」换成「判定跑不通」,
        #    其余一个字不动。
        "id": "MUT-09", "file": WORKER,
        "desc": "「通道没配好」当成「这条坏了」处理(耗尽重试、转人工,而不是等通道恢复)",
        "from": "            deferred += 1\n            _defer(int(row[\"id\"]), str(exc))",
        "to": "            deferred += 1\n            _settle_row(int(row[\"id\"]), MAX_ATTEMPTS, str(exc))",
        "expect": {
            f"py::{CHAIN}::test_23_transport_not_configured_defers_instead_of_burning_the_attempt",
        },
    },
    # ══════════════════════════════════════════════════════════════════
    # ③ P1-2 / P1-4
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-10", "file": MATER,
        "desc": "P1-2 根因复原:物化器不再签发执行预算",
        "from": "    if existing is None:\n        _store.insert_budget(cur, draft.as_store_values())\n"
                "        created = True",
        "to": "    if existing is None:\n        created = False",
        "expect": {
            f"py::{BUDGET}::test_00_budget_snapshot_has_a_production_issuer",
            f"py::{BUDGET}::test_02_after_materializer_runs_preview_succeeds",
            f"py::{BUDGET}::test_03_materializer_is_zero_freeze",
            f"py::{BUDGET}::test_07_materialize_twice_does_not_issue_two_budgets",
            f"py::{CHAIN}::test_01_confirm_freezes_exact_points",
            f"py::{CHAIN}::test_02_dispatch_claims_and_calls_provider_once",
            f"py::{CHAIN}::test_03_verified_publication_commits_once",
            f"py::{CHAIN}::test_04_authoritative_rejection_releases_once",
            f"py::{CHAIN}::test_05_unknown_outcome_never_releases",
            f"py::{CHAIN}::test_10_window1_replay_after_claim_returns_same_root",
            f"py::{CHAIN}::test_11_window2_business_object_freeze_outbox_are_one_transaction",
            f"py::{CHAIN}::test_12_window3_recovery_never_retransmits",
            f"py::{CHAIN}::test_13_window4_outcome_without_settlement_converges",
            f"py::{CHAIN}::test_14_marker_is_durable_before_the_provider_call",
            f"py::{CHAIN}::test_15_article_edited_after_confirm_is_not_published",
            f"py::{CHAIN}::test_20_reconcile_requeues_frozen_but_never_dispatched",
            f"py::{CHAIN}::test_21_reconcile_holds_external_started_without_outcome",
            f"py::{CHAIN}::test_22_reconcile_releases_when_queue_gave_up_and_never_dispatched",
            f"py::{CHAIN}::test_22b_external_started_is_never_released_even_when_the_queue_gave_up",
            f"py::{CHAIN}::test_23_transport_not_configured_defers_instead_of_burning_the_attempt",
            # [R1] 新增的两条也走 ``_confirmed_command`` ⇒ 同一结构性溢出。
            f"py::{CHAIN}::test_24_platform_cost_leg_settles_on_the_platform_account",
            f"py::{CHAIN}::test_24b_non_platform_leg_outside_frozen_is_never_released",
            f"py::{OWN}::test_11_preview_owner_path_still_works",
            # [R2] 平台腿那一族同样吃预算 ⇒ 同一结构性溢出。
            f"py::{R2}::test_r2_11_materializer_signs_the_same_row_for_an_ordinary_tenant",
            f"py::{R2}::test_r2_12_non_platform_leg_outside_the_settlement_states_is_left_alone",
            f"py::{R2}::test_r2_13_platform_leg_missing_outbox_is_requeued",
            f"py::{R2}::test_r2_20_admin_identity_runs_the_whole_chain_on_the_platform_leg",
            f"py::{R2}::test_r2_21_ordinary_identity_still_runs_the_personal_wallet_leg",
            f"py::{R2}::test_r2_30_bump_status_never_moves_a_platform_legs_funding_state",
            f"py::{R2}::test_r2_31_non_platform_leg_funding_state_still_moves",
            f"py::{R2}::test_r2_32_stuck_platform_leg_does_not_kill_the_whole_reconcile_tick",
            f"py::{R2}::test_r2_33_mirror_conflict_on_a_platform_leg_does_not_abort",
            f"py::{R2}::test_r2_34_unknown_provider_outcome_on_a_platform_leg_does_not_abort",
        } | _R3_PLATFORM_ARMS | _R4_LEGAL_ARMS,
        "expect_note": "预算没了 ⇒ 整条主链都拿不到 preview,溢出是**结构性**的,不是耦合",
    },
    {
        "id": "MUT-11", "file": PUBAPI,
        "desc": "P1-2 信封复原:预算缺失退回裸 500",
        "from": "        typed = _budget_not_ready_error(exc, accepted_snapshot_id=body.accepted_snapshot_id)\n"
                "        if typed is not None:\n            raise typed from None\n",
        "to": "",
        "expect": {
            f"py::{BUDGET}::test_01_missing_budget_is_typed_not_500",
        },
    },
    {
        "id": "MUT-12", "file": PUBAPI,
        "desc": "P1-4:归属闸不再校验品牌可达性(等于没闸)",
        "from": "    try:\n        require_brand_access(request, int(brand_id), allow_null=False)\n"
                "    except HTTPException:\n        raise _safe_error(\"OBJECT_NOT_FOUND\") from None",
        "to": "    _ = (require_brand_access, brand_id)",
        "expect": {
            f"py::{OWN}::test_02_other_tenant_gets_404_not_empty_200",
            f"py::{OWN}::test_03_foreign_and_absent_are_byte_identical",
            f"py::{OWN}::test_10_preview_refuses_someone_elses_accepted_snapshot",
            f"py::{OWN}::test_12_ownership_gate_runs_before_any_slot_row_is_created",
        },
    },
    {
        "id": "MUT-13", "file": PUBAPI,
        "desc": "P1-4:把归属闸挪到 ensure_slot **之后**(拒绝之前已经建好了格子)",
        "from": "        _assert_accepted_snapshot_access(request, cur, body.accepted_snapshot_id)\n"
                "        _store.ensure_slot(",
        "to": "        _store.ensure_slot(",
        "expect": {
            f"py::{OWN}::test_10_preview_refuses_someone_elses_accepted_snapshot",
            f"py::{OWN}::test_12_ownership_gate_runs_before_any_slot_row_is_created",
        },
    },
    # ══════════════════════════════════════════════════════════════════
    # ④ 预算口径
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-14", "file": POLICY,
        "desc": "DEL-01:把承诺量换成**容量**(§19 第 11 发同形态)",
        "from": "    return int(contract_minimums_of(plan).get(_PUBLICATIONS_KEY) or 0)",
        "to": "    from services.defensive_geo.delivery_plan import capacity_of\n\n"
              "    return int(capacity_of(plan))",
        "expect": {
            f"py::{BUDGET}::test_05_cap_reads_contract_minimum_not_capacity",
        },
    },
    {
        "id": "MUT-15", "file": POLICY,
        "desc": "cap 写死成常数(推导变成拍脑袋)",
        "from": "    scope_cap = publications * unit_ceiling",
        "to": "    scope_cap = 10000",
        "expect": {
            f"py::{BUDGET}::test_04_cap_is_derived_not_invented[1]",
            f"py::{BUDGET}::test_04_cap_is_derived_not_invented[2]",
            f"py::{BUDGET}::test_04_cap_is_derived_not_invented[5]",
        },
    },
    # ══════════════════════════════════════════════════════════════════
    # ⑤ 收敛器
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-16", "file": RECON,
        "desc": "收敛器少跑第 7 项(通道跑不通 + 零外调 ⇒ 永远冻着)",
        "from": "    actions += await _release_never_dispatched(cur, limit=limit)",
        "to": "",
        "expect": {
            f"py::{CHAIN}::test_22_reconcile_releases_when_queue_gave_up_and_never_dispatched",
            # 🔴 [R1 实测修正] 平台成本腿**长在第 7 项里面** —— 整项不跑,
            #    平台单同样不会被收敛。第一轮我漏写了这条,精确匹配当场报溢出。
            f"py::{CHAIN}::test_24_platform_cost_leg_settles_on_the_platform_account",
            # 注:``test_24b`` 断言的是"什么都别发生",第 7 项不跑时它照样成立 ——
            #    它的判别力全部来自 MUT-27(把那道闸摘掉),不来自这一发。
        },
    },
    {
        "id": "MUT-17", "file": RECON,
        "desc": "🔴 第 7 项去掉「零 external-start」这个证据条件(变成盲退款)",
        "from": "           AND c.external_start_at IS NULL\n           AND c.provider_call_count = 0\n",
        "to": "",
        # 🔴 这一发第一轮**存活**了(红 0 条)—— 那是唯一可信的「锁不够」信号。
        #    真因:我原来指望 test_05 守这一格,但它的命令是 pending_reconciliation,
        #    而这条查询要的是 frozen + outbox needs_review —— 两条判据都够不到。
        #    于是补了 test_22b(有 marker + frozen + 队列放弃),现在它精确杀死。
        "expect": {
            f"py::{CHAIN}::test_22b_external_started_is_never_released_even_when_the_queue_gave_up",
        },
    },
    {
        "id": "MUT-18", "file": RECON,
        "desc": "补入队那一项顺手把钱也退了(「不动钱」被破坏)",
        "from": '        out.append(ReconcileAction(\n            row["publish_command_id"], "requeue_outbox", "frozen", "frozen",',
        "to": '        _store.bump_status(cur, publish_command_id=row["publish_command_id"],\n'
              '                           funding_state="released")\n'
              '        out.append(ReconcileAction(\n            row["publish_command_id"], "requeue_outbox", "frozen", "frozen",',
        "expect": {
            f"py::{CHAIN}::test_20_reconcile_requeues_frozen_but_never_dispatched",
        },
    },
    # ══════════════════════════════════════════════════════════════════
    # ⑥ 此前一发都没打到的四个改动文件(「每条改动 ≥1 发」的补齐)
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-19", "file": TRANSPORT,
        "desc": "登记表里多塞一个 transport(= 分支式测试短路的另一种长相)",
        "from": "register(PRODUCTION_TRANSPORT_KEY, readiness=_mhz_readiness, call=_mhz_publish)",
        "to": ("register(PRODUCTION_TRANSPORT_KEY, readiness=_mhz_readiness, call=_mhz_publish)\n"
               "register(\"pkge_extra\", readiness=_mhz_readiness, call=_mhz_publish)"),
        "expect": {
            f"py::{CHAIN}::test_92_worker_default_resolves_the_production_transport",
        },
    },
    {
        "id": "MUT-20", "file": STORE,
        "desc": "``defer_outbox`` 把「这次没轮到」写成终局(通道恢复后再也不发了)",
        "from": "           SET status = 'pending', claim_token = NULL, last_error = %s,",
        "to": "           SET status = 'failed', claim_token = NULL, last_error = %s,",
        "expect": {
            f"py::{CHAIN}::test_23_transport_not_configured_defers_instead_of_burning_the_attempt",
        },
    },
    {
        "id": "MUT-21", "file": STORE,
        "desc": "047 那两列从 COMMAND_COLUMNS 里摘掉(读面永远读不到)",
        # 锚点 = 那两列在 COMMAND_COLUMNS 里的那一行。它后面还有别的列,
        # 所以不能拿 ")" 当右边界 —— 第一版就是那么写的,锚点命中 0 次
        # (「变异没打进去」与「变异被杀死」长得完全不同,runner 把它分开报)。
        "from": '    "provider_order_ref", "provider_last_polled_at",\n'
                '    "legal_rule_id", "legal_rule_version",',
        "to": '    "legal_rule_id", "legal_rule_version",',
        "expect": {
            f"py::{CHAIN}::test_02_dispatch_claims_and_calls_provider_once",
            f"py::{CHAIN}::test_12_window3_recovery_never_retransmits",
        },
    },
    {
        "id": "MUT-22", "file": ACTIONS,
        "desc": "把预算未就绪的出口 action 摘掉(阻塞变回死路)",
        "from": '    "view_order_progress": ActionSpec(\n'
                '        "view_service_progress", ("service_projection",), True, False),\n',
        "to": "",
        "expect": {
            f"py::{BUDGET}::test_01_missing_budget_is_typed_not_500",
        },
    },
    {
        "id": "MUT-23", "file": COPY,
        "desc": "把预算未就绪的人话解释摘掉(信封退回没有 publicExplanation)",
        "from": '    "execution_budget_not_ready": "这一单的准备工作还差一步，稍等一下再来确认；没有扣除任何算力。",\n',
        "to": "",
        "expect": {
            f"py::{BUDGET}::test_01_missing_budget_is_typed_not_500",
        },
    },
    # ══════════════════════════════════════════════════════════════════
    # ⑨ [R1 返修] 钱向真值表 + 收敛器第 7 项的平台腿
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-24", "file": SETTLE,
        "desc": "🔴 Review 的验收变异:未知态的钱向改成 release(= 结果未知自动退款)",
        # 锚点带下一行 ``conflict``:光写 ``"unknown": "hold_or_quarantine",``
        # 会同时命中 ``rejected_unknown`` / ``failed_unknown`` 两行的尾部。
        "from": '\n    "unknown": "hold_or_quarantine",\n'
                '    "conflict": "hold_or_quarantine",\n}',
        "to": '\n    "unknown": "release",\n'
              '    "conflict": "hold_or_quarantine",\n}',
        "expect": {
            f"py::{VALUE}::test_r1_01_hold_or_quarantine_row_is_pinned_value_by_value[unknown]",
            f"py::{VALUE}::test_r1_02_direction_table_discriminates_between_the_three_families",
            f"py::{VALUE}::test_r1_03_release_exact_refuses_every_unknown_state[unknown]",
            f"py::{VALUE}::test_r1_05_assert_direction_unit_arm_rejects_release[unknown]",
            f"py::{VALUE}::test_r1_07_unknown_outcome_is_never_shown_as_refunded"
            f"[frozen-True-unknown]",
            f"py::{VALUE}::test_r1_07_unknown_outcome_is_never_shown_as_refunded"
            f"[pending_reconciliation-True-unknown]",
            f"py::{VALUE}::test_r1_07_unknown_outcome_is_never_shown_as_refunded"
            f"[quarantined-True-unknown]",
            f"py::{VALUE}::test_r1_07_unknown_outcome_is_never_shown_as_refunded"
            f"[released-False-unknown]",
            f"py::{VALUE}::test_r1_07_unknown_outcome_is_never_shown_as_refunded"
            f"[released-True-unknown]",
        },
    },
    {
        "id": "MUT-26", "file": RECON,
        "desc": "候选集退回 ``= 'frozen'``(= R1 之前的形态:平台腿被 schema 保证滤光,守卫成装饰)",
        # 🔴 [R2 实测修正] R2 把 ② 的候选集也补成了同一串字面值 ⇒ 这个锚点从
        #    命中 1 次变成命中 2 次,runner 当场报「变异没打进去」(它把
        #    「锚点没命中」与「变异被杀死」分开报,这次就是靠这一点抓到的)。
        #    往下多带一行 ⑦ 独有的条件把它钉死。
        # 🔴 [R3 实测修正] 第二次被自己挤掉锚点:R3 在这一行后面插了
        #    ``AND c.settled_at IS NULL``(平台腿的终态标记),于是原锚点命中 0 次。
        #    runner 把「锚点没命中」与「变异被杀死」分开报,又一次靠这一点抓到。
        "from": "         WHERE c.funding_state IN ('frozen', 'exempt_recorded')\n"
                "           AND c.settled_at IS NULL",
        "to": "         WHERE c.funding_state = 'frozen'\n"
              "           AND c.settled_at IS NULL",
        "expect": {
            f"py::{CHAIN}::test_24_platform_cost_leg_settles_on_the_platform_account",
            # 🔴 [R2 条件③ 收紧之后才有的这一条] 候选集一退回,⑦ 的 SQL 里就没有
            #    ``exempt_recorded`` 了 ⇒ 全类锁当场红。收紧**之前**它是绿的 ——
            #    那条裸子串被本函数**注释里**的 exempt_recorded 喂绿了。
            f"py::{R2}::test_r2_40_every_platform_branch_has_exempt_recorded_in_its_candidate_set",
        },
    },
    {
        "id": "MUT-27", "file": RECON,
        "desc": "🔴 摘掉「非平台腿必须是 frozen」那道闸 ⇒ 异常形状直接走 release_exact",
        # 🔴 [R3] 锚点随守卫改写而变:平台腿不再另走一条,这道闸多了
        #    ``not is_platform and`` 前缀(见 reconciler 里 R3 那段注释)。
        "from": '        if not is_platform and str(row["funding_state"]) != "frozen":\n',
        "to": '        if False:\n',
        "expect": {
            f"py::{CHAIN}::test_24b_non_platform_leg_outside_frozen_is_never_released",
        },
    },
    # ══════════════════════════════════════════════════════════════════
    # ⑩ [R2] 平台成本腿启用 + ④ 同构姊妹洞 + 全类收口
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-28", "file": PAYER,
        "desc": "🔴 [Owner 点名] 付款方**判别反转**(admin 走个人钱包、普通人走平台账)",
        "from": "    if bool(is_admin):\n        return _PLATFORM",
        "to": "    if not bool(is_admin):\n        return _PLATFORM",
        "expect": (
            _R2_EQUIV_01 | _R2_EQUIV_02 | _R2_WHOLE_CHAIN | {
                f"py::{R2}::test_r2_04_worker_side_admin_resolution_reads_roles_not_a_column",
                f"py::{R2}::test_r2_11_materializer_signs_the_same_row_for_an_ordinary_tenant",
                f"py::{R2}::test_r2_12_non_platform_leg_outside_the_settlement_states_is_left_alone",
                f"py::{R2}::test_r2_13_platform_leg_missing_outbox_is_requeued",
                f"py::{R2}::test_r2_20_admin_identity_runs_the_whole_chain_on_the_platform_leg",
                f"py::{R2}::test_r2_21_ordinary_identity_still_runs_the_personal_wallet_leg",
                f"py::{R2}::test_r2_31_non_platform_leg_funding_state_still_moves",
                f"py::{R2}::test_r2_32_stuck_platform_leg_does_not_kill_the_whole_reconcile_tick",
                f"py::{R2}::test_r2_33_mirror_conflict_on_a_platform_leg_does_not_abort",
                f"py::{R2}::test_r2_34_unknown_provider_outcome_on_a_platform_leg_does_not_abort",
            } | _R3_PLATFORM_ARMS | _R4_LEGAL_ARMS
        ),
        "expect_note": "判别反转 ⇒ 每个租户都换一条钱腿,整条主链跟着红。溢出是结构性的",
    },
    {
        "id": "MUT-29", "file": MATER,
        # 🔴 [工单 C-1 2026-08-25] **锚点继任**,不是退役。
        #    原锚 `funding_policy=payer.funding_policy,` 那一行在工单 C-1 里被改成
        #    读**冻结值** `identity["payer_funding_policy"]`(付款方判别搬到客户
        #    确认事务,materializer 不再现算)。原锚不再存在 ⇒ 这一发会以
        #    "锚点找不到"的形态静默失效,而失效的变异与"变异被杀"在报表上长得一样
        #    (本仓记过)。继任锚打在**同一格语义**上:摘掉它 ⇒ derive 回到吃默认值
        #    `personal_wallet` ⇒ 平台腿整臂塌掉、普通身份那一臂纹丝不动,
        #    与原发的期望集合逐条相同。
        "desc": "🔴 [Owner 点名] 物化器**恢复吃默认值**(不再显式传 fundingPolicy)",
        "from": '        funding_policy=identity["payer_funding_policy"],\n',
        "to": "",
        "expect": {
            # 平台腿那一臂整臂塌掉;普通身份那一臂**纹丝不动** ——
            # 这正是「默认参数=旧行为不变」那条判据该有的样子。
            f"py::{R2}::test_r2_13_platform_leg_missing_outbox_is_requeued",
            f"py::{R2}::test_r2_20_admin_identity_runs_the_whole_chain_on_the_platform_leg",
            f"py::{R2}::test_r2_32_stuck_platform_leg_does_not_kill_the_whole_reconcile_tick",
            f"py::{R2}::test_r2_33_mirror_conflict_on_a_platform_leg_does_not_abort",
            f"py::{R2}::test_r2_34_unknown_provider_outcome_on_a_platform_leg_does_not_abort",
        } | _R3_PLATFORM_ARMS | _R4_LEGAL_PLATFORM_ARM,
    },
    {
        "id": "MUT-31", "file": RECON,
        "desc": "🔴 [Owner 点名 · ④ 姊妹洞] 候选集退回不含 ``exempt_recorded``",
        "from": "         WHERE funding_state IN ('frozen','pending_reconciliation','exempt_recorded')",
        "to": "         WHERE funding_state IN ('frozen','pending_reconciliation')",
        "expect": {
            f"py::{R2}::test_r2_20_admin_identity_runs_the_whole_chain_on_the_platform_leg",
            # [R3 实测修正] ④ 候选集一退回,平台腿的 release 臂与"不重复结算"臂
            #   都够不到被测行;全类锁也红(候选集不再为平台分支服务)。
            f"py::{R2}::test_r2_23_platform_leg_release_arm_refunds_the_platform_account",
            f"py::{R2}::test_r2_24_a_settled_platform_leg_is_never_settled_twice",
            f"py::{R2}::test_r2_40_every_platform_branch_has_exempt_recorded_in_its_candidate_set",
        },
    },
    {
        "id": "MUT-32", "file": STORE,
        "desc": "🔴 摘掉「平台腿 fundingState 是常量」那道 CASE 闸(= 全类收口点)",
        "from": '                "funding_state = CASE WHEN principal_kind = %s "\n'
                '                "THEN funding_state ELSE %s END"\n'
                "            )\n"
                "            params.extend([_PLATFORM_PRINCIPAL_KIND, _val])",
        "to": '                "funding_state = %s"\n'
              "            )\n"
              "            params.append(_val)",
        "expect": {
            f"py::{R2}::test_r2_30_bump_status_never_moves_a_platform_legs_funding_state",
            f"py::{R2}::test_r2_32_stuck_platform_leg_does_not_kill_the_whole_reconcile_tick",
            f"py::{R2}::test_r2_33_mirror_conflict_on_a_platform_leg_does_not_abort",
            f"py::{R2}::test_r2_34_unknown_provider_outcome_on_a_platform_leg_does_not_abort",
            # 🔴 下面这 10 条**不相干**的判据也红 —— 那不是耦合,那是**爆炸半径本身**:
            #    一条卡住的平台单违反 CHECK ⇒ 整轮收敛/派发抛出 ⇒ 后面每一条判据
            #    (在生产里就是**其它租户的每一条命令**)全部陪葬。
            #    这一发是本轮唯一能把"波及面"量出来的实证。
            f"py::{CHAIN}::test_03_verified_publication_commits_once",
            f"py::{CHAIN}::test_04_authoritative_rejection_releases_once",
            f"py::{CHAIN}::test_05_unknown_outcome_never_releases",
            f"py::{CHAIN}::test_13_window4_outcome_without_settlement_converges",
            f"py::{CHAIN}::test_20_reconcile_requeues_frozen_but_never_dispatched",
            f"py::{CHAIN}::test_21_reconcile_holds_external_started_without_outcome",
            f"py::{CHAIN}::test_22_reconcile_releases_when_queue_gave_up_and_never_dispatched",
            f"py::{CHAIN}::test_22b_external_started_is_never_released_even_when_the_queue_gave_up",
            f"py::{CHAIN}::test_24_platform_cost_leg_settles_on_the_platform_account",
            f"py::{CHAIN}::test_24b_non_platform_leg_outside_frozen_is_never_released",
            # 🔴 [R3] 波及面**又扩大了一圈**:平台腿现在走真结算,收口闸一摘,
            #    连它自己那几条臂(以及 Z-1 人工两臂)也一起塌。这不是耦合 ——
            #    是"一条平台单打死整轮"这件事在同构之后覆盖了更多路径。
            f"py::{R2}::test_r2_12_non_platform_leg_outside_the_settlement_states_is_left_alone",
            f"py::{R2}::test_r2_13_platform_leg_missing_outbox_is_requeued",
            f"py::{R2}::test_r2_20_admin_identity_runs_the_whole_chain_on_the_platform_leg",
        } | _R3_PLATFORM_ARMS | _R4_LEGAL_PLATFORM_ARM,
        "expect_note": "21 条陪葬(其中 10 条完全不相干)= 「一条平台单打死整轮收敛」的实证",
    },
    {
        "id": "MUT-33", "file": RECON,
        "desc": "② 的候选集退回 ``= 'frozen'``(平台单丢了 outbox 补不回来)",
        "from": "         WHERE c.funding_state IN ('frozen', 'exempt_recorded')\n"
                "           AND c.canonical_publication_state = 'not_started'",
        "to": "         WHERE c.funding_state = 'frozen'\n"
              "           AND c.canonical_publication_state = 'not_started'",
        "expect": {
            f"py::{R2}::test_r2_13_platform_leg_missing_outbox_is_requeued",
        },
    },
    {
        "id": "MUT-34", "file": RECON,
        "desc": "摘掉 ④ 新加的「非平台腿必须在结算态」那道闸(加宽候选集顺手加宽了结算面)",
        # 🔴 [R3] 同上,锚点加 ``not is_platform and`` 前缀。
        "from": '        if not is_platform and before not in ("frozen", "pending_reconciliation"):\n',
        "to": "        if False:\n",
        "expect": {
            f"py::{R2}::test_r2_12_non_platform_leg_outside_the_settlement_states_is_left_alone",
        },
    },
    # ══════════════════════════════════════════════════════════════════
    # ⑪ [R3 · Owner 批口径①] 平台账冻结与钱包腿**同构**真实结算
    # ══════════════════════════════════════════════════════════════════
    # 🔴 MUT-25 / MUT-30 在本轮**退役**,原因写清楚:它们打的是 ⑦/④ 里那两个
    #    **平台专用分支**,而 R3 的做法正是把那两个分支**删掉** ——
    #    平台腿改走与钱包腿完全同一条路(同一个 direction、同一次 commit/release、
    #    同一句 _assert_direction)。被守的行为一条没少,只是不再有"平台专用代码"
    #    可摘;继任者是 MUT-35 / MUT-36:把平台腿**重新挡回去**。
    #    (退役 ≠ 放宽:继任者红的是同一批判据 + 更多。)
    {
        "id": "MUT-35", "file": RECON,
        "desc": "🔴 [Owner 点名] ④ 把平台腿重新挡回结算之外(平台 commit/release 都不再发生)",
        "from": '        if not is_platform and before not in ("frozen", "pending_reconciliation"):',
        "to": '        if is_platform or before not in ("frozen", "pending_reconciliation"):',
        "expect": {
            f"py::{R2}::test_r2_20_admin_identity_runs_the_whole_chain_on_the_platform_leg",
            f"py::{R2}::test_r2_23_platform_leg_release_arm_refunds_the_platform_account",
            f"py::{R2}::test_r2_24_a_settled_platform_leg_is_never_settled_twice",
        },
    },
    {
        "id": "MUT-36", "file": RECON,
        "desc": "🔴 [Owner 点名] ⑦ 把平台腿重新挡回去(权威零接单不再退平台账)",
        "from": '        if not is_platform and str(row["funding_state"]) != "frozen":',
        "to": '        if is_platform or str(row["funding_state"]) != "frozen":',
        "expect": {
            f"py::{CHAIN}::test_24_platform_cost_leg_settles_on_the_platform_account",
        },
    },
    {
        "id": "MUT-37", "file": RECON,
        "desc": "🔴 [Owner 点名] **方向串**:④ 把钱向写死成 commit(真值表失效)",
        "from": '        state = str(row["canonical_publication_state"])\n'
                "        direction = _settle.settlement_direction(state)",
        "to": '        state = str(row["canonical_publication_state"])\n'
              '        direction = "commit"',
        "expect": {
            # 🔴 同构的代价与收益都在这里:方向谓词是**共用**的,所以串向会同时
            #    打红两条腿。这比"平台腿自己有一份方向逻辑"强 —— 那样只有平台臂会红。
            f"py::{R2}::test_r2_23_platform_leg_release_arm_refunds_the_platform_account",
            f"py::{CHAIN}::test_04_authoritative_rejection_releases_once",
            f"py::{CHAIN}::test_05_unknown_outcome_never_releases",
            f"py::{CHAIN}::test_13_window4_outcome_without_settlement_converges",
            f"py::{CHAIN}::test_20_reconcile_requeues_frozen_but_never_dispatched",
            f"py::{CHAIN}::test_21_reconcile_holds_external_started_without_outcome",
            f"py::{CHAIN}::test_22_reconcile_releases_when_queue_gave_up_and_never_dispatched",
            f"py::{CHAIN}::test_22b_external_started_is_never_released_even_when_the_queue_gave_up",
            f"py::{CHAIN}::test_24_platform_cost_leg_settles_on_the_platform_account",
            f"py::{CHAIN}::test_24b_non_platform_leg_outside_frozen_is_never_released",
        },
        "expect_note": "方向谓词共用 ⇒ 串向同时打红两条腿,这是同构的直接证据",
    },
    {
        "id": "MUT-38", "file": REVIEW,
        "desc": "Z-1 人工处置把平台腿重新挡在 commit 之外(走人工出口的冻结永远悬着)",
        "from": '        await _funding.commit_exact(cur, fresh, reason="平台核验确认已执行")',
        "to": '        if not _funding.is_platform_cost(str(fresh["funding_policy"])):\n'
              '            await _funding.commit_exact(cur, fresh, reason="平台核验确认已执行")',
        "expect": {
            f"py::{R2}::test_r2_25_admin_review_commit_settles_the_platform_freeze",
            # census 也红:它机械枚举每个结算调用点,看外层有没有平台守卫。
            f"py::{R2}::test_r2_43_no_settlement_call_site_still_excludes_the_platform_leg",
        },
    },
    {
        "id": "MUT-39", "file": FUND,
        "desc": "🔴 结算终态标记 ``settled_at`` 不落(平台腿掉不出候选集 ⇒ 每轮重结算)",
        "from": '    cid = command.get("publish_command_id")\n    if cur is None or not cid:',
        "to": '    cid = command.get("publish_command_id")\n    if True or cur is None or not cid:',
        "expect": {
            f"py::{R2}::test_r2_20_admin_identity_runs_the_whole_chain_on_the_platform_leg",
            f"py::{R2}::test_r2_23_platform_leg_release_arm_refunds_the_platform_account",
            f"py::{R2}::test_r2_24_a_settled_platform_leg_is_never_settled_twice",
            f"py::{CHAIN}::test_03_verified_publication_commits_once",
            f"py::{CHAIN}::test_04_authoritative_rejection_releases_once",
            f"py::{CHAIN}::test_22_reconcile_releases_when_queue_gave_up_and_never_dispatched",
            f"py::{CHAIN}::test_24_platform_cost_leg_settles_on_the_platform_account",
        } | _R4_LEGAL_ARMS,
    },
    {
        "id": "MUT-40", "file": REVIEW,
        "desc": "🔴 Z-1 队列谓词把平台腿挡回去(需要人工裁的平台单进不了队列)",
        "from": '    if _funding.is_platform_cost(str(command.get("funding_policy") or "")):',
        "to": '    if False and _funding.is_platform_cost(str(command.get("funding_policy") or "")):',
        "expect": {
            f"py::{R2}::test_r2_25_admin_review_commit_settles_the_platform_freeze",
            f"py::{R2}::test_r2_26_admin_review_release_refunds_the_platform_freeze",
            f"py::{R2}::test_r2_44_queue_membership_admits_the_platform_leg_on_both_sides",
        },
    },
    # ══════════════════════════════════════════════════════════════════
    # ⑫ [R4] 广告法门 release 是**活路径** + settled_at 单写点
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-41", "file": DISPATCH,
        "desc": "🔴 [R4-①] 广告法命中那条 release 摘掉(零外调却不退钱)",
        "from": '        await _funding.release_exact(cur, fresh, reason="广告法命中，零外调，全额退回")\n'
                '        _store.bump_status(cur, publish_command_id=command_id, funding_state="released")\n',
        "to": '        _store.bump_status(cur, publish_command_id=command_id, funding_state="released")\n',
        "expect": {
            f"py::{R2}::test_r4_01a_legal_hit_on_a_wallet_leg_refunds_the_customer",
            f"py::{R2}::test_r4_01b_legal_hit_on_a_platform_leg_refunds_the_platform_account",
            # census 的探针活性下限 ``>= 7`` 也跟着不成立 —— 少一个结算调用点。
            f"py::{R2}::test_r2_43_no_settlement_call_site_still_excludes_the_platform_leg",
        },
        "expect_note": "这条路径三轮被我误标「判据够不到」;它是活的,而且平台腿已同走",
    },
    {
        "id": "MUT-42", "file": STORE,
        "desc": "🔴 [R4-②] 把 ``settled_at`` 放回 bump_status 的 allowed(通用直写的门重新打开)",
        "from": '        "organization_charge_ref", "platform_cost_ref",\n'
                '        "provider_order_ref", "provider_last_polled_at",\n'
                "    }",
        "to": '        "organization_charge_ref", "platform_cost_ref", "settled_at",\n'
              '        "provider_order_ref", "provider_last_polled_at",\n'
              "    }",
        "expect": {
            f"py::{R2}::test_r4_02_settled_at_has_exactly_one_writer",
        },
    },
    {
        "id": "MUT-43", "file": STORE,
        "desc": "🔴 [R4-②] ``mark_settled`` 丢掉「至多一次」的 WHERE(已结算的时间戳会被改写)",
        "from": '        f"WHERE publish_command_id = %s AND settled_at IS NULL",',
        "to": '        f"WHERE publish_command_id = %s",',
        "expect": {
            f"py::{R2}::test_r4_02_settled_at_has_exactly_one_writer",
        },
    },
]


def main() -> int:
    print("═" * 72)
    print("包E 撕锁自证 · 基线")
    print("═" * 72)
    base_red, base_green, _ = run_criteria()
    if base_red:
        print(f"🔴 基线不是全绿:{sorted(base_red)} —— 后面全部作废")
        return 2
    print(f"✅ 基线全绿({base_green} 条)\n")

    passed = failed = 0
    for mut in MUTATIONS:
        path: Path = mut["file"]
        original = path.read_bytes()
        text = original.decode("utf-8")
        if text.count(mut["from"]) != 1:
            print(f"❌ {mut['id']} 锚点命中 {text.count(mut['from'])} 次(应为 1)—— 变异没打进去")
            failed += 1
            continue
        path.write_bytes(text.replace(mut["from"], mut["to"], 1).encode("utf-8"))
        try:
            red, green, _ = run_criteria()
        finally:
            path.write_bytes(original)
            assert sha(path.read_bytes()) == sha(original), f"{mut['id']} 还原后字节不一致"

        expect = set(mut["expect"])
        missing = sorted(expect - red)
        extra = sorted(red - expect)
        ok = not missing and not extra
        mark = "✅" if ok else "❌"
        print(f"{mark} {mut['id']} · {mut['desc']}")
        print(f"    红 {len(red)} 条 / 绿 {green} 条")
        if mut.get("expect_note"):
            print(f"    注:{mut['expect_note']}")
        if missing:
            print(f"    🔴 该红没红:{missing}")
        if extra:
            print(f"    🔴 溢出(没预料到也红了):{extra}")
        passed += int(ok)
        failed += int(not ok)

    print("═" * 72)
    print(f"精确匹配 {passed}/{len(MUTATIONS)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
