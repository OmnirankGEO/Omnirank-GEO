#!/usr/bin/env python
"""工单B(发布资金链五修)的**撕锁自证**。

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
   报数时两栏分开,不许合并成一个"全杀"数字。

🔴 **禁并发**:本 runner 就地改源文件。同一棵树上同时跑第二个 runner
   (或另一个执行窗口在编辑)会让两边都拿到垃圾结果。

跑法(仓库根)::

    TEST_DATABASE_URL=postgresql://geo_admin:testpw@localhost:55850/geo_defgeo_wob_test \\
        python scripts/mutation_runner_wob_publish_funding.py
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mutation_tree_lock import criteria_fingerprint, tree_lock  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]

FUND = ROOT / "services" / "defensive_geo" / "publish" / "publish_funding.py"
STORE = ROOT / "services" / "defensive_geo" / "publish" / "store.py"
RECON = ROOT / "services" / "defensive_geo" / "publish" / "reconciler.py"
DISPATCH = ROOT / "services" / "defensive_geo" / "publish" / "publish_outbox.py"
REVIEW = ROOT / "services" / "defensive_geo" / "publish" / "settlement_review.py"
WORKER = ROOT / "services" / "defensive_geo" / "publish" / "publish_worker.py"
PUBAPI = ROOT / "api" / "defensive_publish_api.py"
MHZ = ROOT / "services" / "meijiehezi" / "client.py"
TRANSPORT = ROOT / "services" / "defensive_geo" / "publish" / "provider_transport.py"
MIG051 = ROOT / "db" / "migration_051_defgeo_publish_settlement_guards_2026_08_25.sql"

#: 🔴 红集合的**分母**。默认只跑本包;``WOB_MUT_TARGETS=both`` 时把既有全绿
#:    判据族(E 包 159 条)也纳入 —— 外选变异里有好几发打在**平台成本腿**上,
#:    而平台腿的行为分母在本包里是 0(它的真链臂长在 E 包)。
#:    分母比结论小,结论就是假的:所以外选那一轮用 both 跑。
_TARGETS = {
    "wob": ("tests/defgeo_wob_publish_funding_2026_08_25",),
    "both": ("tests/defgeo_wob_publish_funding_2026_08_25",
             "tests/defensive_geo_pkge_2026_08_24"),
}
PYTEST_TARGETS = _TARGETS[os.environ.get("WOB_MUT_TARGETS", "wob")]

#: E 包用自己那把一次性库(库名硬闸要求同时含 defgeo/pkge/test)。
PKGE_DB_URL = os.environ.get(
    "PKGE_DATABASE_URL",
    "postgresql://geo_admin:testpw@localhost:55480/geo_defgeo_pkge_test",
)

#: 只跑指定的几发(逗号分隔 id);空 = 全跑。
ONLY = {x.strip() for x in os.environ.get("WOB_MUT_ONLY", "").split(",") if x.strip()}

#: 🔴 [2026-08-27] 按**来源**过滤(``self`` / ``audit`` / ``review_extsel``)。
#:    报数纪律是「自选/他选分栏,永不合并成一个数」,而在此之前 B7 那七发
#:    (Review 终单亲挑 = **他选**)在数据里写着 ``origin: "self"`` ——
#:    字段自己在撒谎,「自选 26」只能靠人记得"再减掉 B7"。已订正成 review_extsel,
#:    于是 ``WOB_MUT_ORIGIN=self`` 机械地就是那 26 发。
ORIGIN = {x.strip() for x in os.environ.get("WOB_MUT_ORIGIN", "").split(",") if x.strip()}

DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://geo_admin:testpw@localhost:55850/geo_defgeo_wob_test",
)

B1 = "test_b1_settlement_order_pg.py"
B2 = "test_b2_outbox_fencing_pg.py"
B3 = "test_b3_capability_and_attempts_pg.py"
B4 = "test_b4_budget_denominator_pg.py"
B5 = "test_b5_provider_ref_uniqueness_pg.py"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


#: 起跑前至少要有这么多可用空间。不够就拒绝开跑 ——
#: 2026-08-25 实测:盘写满时炸在**还原**那一步,被测文件被留成 0 字节。
MIN_FREE_BYTES = 256 * 1024 * 1024


def _assert_disk_headroom() -> None:
    free = shutil.disk_usage(ROOT).free
    if free < MIN_FREE_BYTES:
        raise SystemExit(
            f"🔴 可用空间只有 {free / 1024 / 1024:.0f} MB(要求 ≥ "
            f"{MIN_FREE_BYTES / 1024 / 1024:.0f} MB)——**拒绝开跑**。\n"
            "   理由不是洁癖:盘满时会炸在**还原**那一步,而 write_bytes 是"
            "先截断再写 ⇒ 被测源文件被留成 0 字节(2026-08-25 实测发生过)。"
        )


def _restore(path: Path, original: bytes) -> None:
    """**原子**还原:tmp + os.replace。崩在这里也不会把原文件截断。"""
    tmp = path.with_suffix(path.suffix + ".mutrestore")
    tmp.write_bytes(original)
    os.replace(tmp, path)
    if sha(path.read_bytes()) != sha(original):            # pragma: no cover
        raise SystemExit(f"🔴 {path} 还原后字节不一致 —— 立刻停,别再跑下去")


def _one_target(target: str, db_url: str) -> tuple[set[str], int, int]:
    env = dict(os.environ, TEST_DATABASE_URL=db_url, PYTHONIOENCODING="utf-8")
    p = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    out = (p.stdout or "") + (p.stderr or "")
    red: set[str] = set()
    for line in out.splitlines():
        m = re.match(r"^(FAILED|ERROR)\s+([\w./" + "\\\\" + r"\-]+\.py(?:::\S+)?)$",
                     line.strip())
        if m:
            red.add("py::" + m.group(2).replace("\\", "/").split("/")[-1])
    m = re.search(r"(\d+) passed", out)
    green = int(m.group(1)) if m else 0
    # 🔴 collection / internal error 也要当红:本仓记过 runner 把"测试根本没跑"
    #    认证成通过。绿数为 0 且退出码非 0 ⇒ 直接判定尺子坏了。
    if green == 0 and p.returncode != 0:
        red.add("py::__COLLECTION_OR_INTERNAL_ERROR__::" + target)
    return red, green, p.returncode


def run_criteria() -> tuple[set[str], int, int]:
    """跑判据,回 (**归一化红集合**, 绿数, 退出码)。两个包各跑各的库,红集合取并集。"""
    red: set[str] = set()
    green = 0
    rc = 0
    for target in PYTEST_TARGETS:
        db = PKGE_DB_URL if "pkge" in target else DB_URL
        r, g, c = _one_target(target, db)
        red |= r
        green += g
        rc = rc or c
    return red, green, rc


#: 参数化臂的红集合按机械规律生成,不手抄。
_B1_01_ALL = {f"py::{B1}::test_b1_01_commit_that_did_not_settle_writes_no_terminal[{s}-{v}]"
              for s, v in (("not_found", "retry"), ("ambiguous", "manual"),
                           ("idempotent_released", "manual"))}
_B1_02_ALL = {f"py::{B1}::test_b1_02_release_that_did_not_settle_writes_no_terminal[{s}-{v}]"
              for s, v in (("not_found", "retry"), ("ambiguous", "manual"),
                           ("idempotent_committed", "manual"))}
_B2_STALE = {f"py::{B2}::test_b2_02_stale_lease_cannot_finish_the_row[{op}]"
             for op in ("settle", "defer")}


MUTATIONS = [
    # ══════════════════════════════════════════════════════════════════
    # B-1 · 顺序反转(P0-4)
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-B1-01", "origin": "self", "file": FUND,
        "desc": "🔴 把终态写回 billing **之前**(= P0-4 原样复活)",
        "from": '''    _assert_direction(command, expected=intent)

    # ── 幂等 ①''',
        "to": '''    _assert_direction(command, expected=intent)
    _write_terminal(cur, command, intent=intent,
                    terminal_command_state=terminal_command_state)

    # ── 幂等 ①''',
        "expect": _B1_01_ALL | _B1_02_ALL | {
            f"py::{B1}::test_b1_03_billing_exception_writes_no_terminal",
            f"py::{B1}::test_b1_04_manual_verdict_lands_in_the_z1_queue",
            f"py::{B1}::test_b1_92_mark_settled_never_precedes_billing",
            f"py::{B1}::test_b1_30_commit_retry_has_a_ceiling",
            f"py::{B1}::test_b1_31_release_retry_has_no_ceiling",
            # 顺序换回去之后这两条也真的坏了(第一版漏写,实跑报"溢出"):
            #   · 已结算的再调不再是 no-op(前置那一手无条件写终态);
            #   · 两个相反的人工处置都能推进(第一手写完终态才去动钱)。
            f"py::{B1}::test_b1_13_already_settled_command_is_a_noop",
            f"py::{B1}::test_b1_22_two_opposite_admin_actions_cannot_both_win",
        },
        # 🟡 ``test_b1_20`` **不在**红集合里,这是对的:变异前置的那一手
        #    ``_write_terminal`` 拿的是同一个过期 statusVersion,CAS 照样落空,
        #    随后真正那一手也落空 ⇒ 裁决仍是 retry。CAS 由 MUT-B1-05 单独钉。
    },
    {
        "id": "MUT-B1-02", "origin": "self", "file": FUND,
        "desc": "🔴 R3 摘掉:``success`` 不是 True 也照落终态",
        "from": '''    if r.get("success") is True:''',
        "to": '''    if True:''',
        "expect": {
            f"py::{B1}::test_b1_01_commit_that_did_not_settle_writes_no_terminal[not_found-retry]",
            f"py::{B1}::test_b1_02_release_that_did_not_settle_writes_no_terminal[not_found-retry]",
            f"py::{B1}::test_b1_30_commit_retry_has_a_ceiling",
            f"py::{B1}::test_b1_31_release_retry_has_no_ceiling",
            f"py::{B1}::test_b1_90_truth_table_matches_the_diagnosis_chain",
        },
        # 🟡 ``test_b1_03`` 不红是对的:它注入的是**异常**,在 R3 那一格之前
        #    就被 ``except`` 收走了。异常那一格由 MUT-B1-08 钉。
    },
    {
        "id": "MUT-B1-03", "origin": "self", "file": FUND,
        "desc": "🔴 R2 ambiguous 那一格摘掉(跨表撞号自动坐实错的那一笔)",
        "from": '''    if r.get("ambiguous"):
        return SettlementOutcome("manual", intent, "ambiguous_cross_table", r)''',
        "to": '''    if False:
        return SettlementOutcome("manual", intent, "ambiguous_cross_table", r)''',
        "expect": {
            f"py::{B1}::test_b1_01_commit_that_did_not_settle_writes_no_terminal[ambiguous-manual]",
            f"py::{B1}::test_b1_02_release_that_did_not_settle_writes_no_terminal[ambiguous-manual]",
            f"py::{B1}::test_b1_04_manual_verdict_lands_in_the_z1_queue",
            f"py::{B1}::test_b1_05_manual_command_is_not_reattempted_next_round",
            f"py::{B1}::test_b1_90_truth_table_matches_the_diagnosis_chain",
        },
    },
    {
        "id": "MUT-B1-04", "origin": "self", "file": FUND,
        "desc": "🔴 幂等冲突守卫摘掉(想 commit 已 released 也当成功)",
        "from": '''            if (intent == "commit" and idem_status == "released") or \\
               (intent == "release" and idem_status == "committed"):''',
        "to": '''            if (intent == "commit" and idem_status == "__never__") or \\
               (intent == "release" and idem_status == "__never__"):''',
        "expect": {
            f"py::{B1}::test_b1_01_commit_that_did_not_settle_writes_no_terminal[idempotent_released-manual]",
            f"py::{B1}::test_b1_02_release_that_did_not_settle_writes_no_terminal[idempotent_committed-manual]",
            f"py::{B1}::test_b1_90_truth_table_matches_the_diagnosis_chain",
        },
    },
    {
        "id": "MUT-B1-05", "origin": "self", "file": FUND,
        "desc": "🔴 CAS 摘掉(两个相反的人工处置可以各自基于旧状态推进)",
        "from": '''        expect_status_version=int(expect) if expect is not None else None,''',
        "to": '''        expect_status_version=None,''',
        "expect": {
            f"py::{B1}::test_b1_20_terminal_write_is_status_version_cas",
        },
    },
    {
        "id": "MUT-B1-06", "origin": "self", "file": REVIEW,
        "desc": "🔴 人工处置的第一手 CAS 摘掉(Codex 复现的交错分裂入口)",
        "from": '''        expect_status_version=int(command["status_version"]),''',
        "to": '''        expect_status_version=None,''',
        "expect": {
            f"py::{B1}::test_b1_21_admin_review_refuses_a_stale_disposition",
        },
    },
    {
        "id": "MUT-B1-07", "origin": "self", "file": REVIEW,
        "desc": "🔴 人工面不看 verdict(物理没动却回 200 = P0-4 在人工面的形态)",
        "from": '''        outcome = await _funding.commit_exact(
            cur, fresh, reason="平台核验确认已执行", terminal_command_state="completed")
        _assert_settled(outcome, publish_command_id)''',
        "to": '''        await _funding.commit_exact(
            cur, fresh, reason="平台核验确认已执行", terminal_command_state="completed")''',
        "expect": {
            f"py::{B1}::test_b1_95_every_settlement_call_site_consumes_the_verdict",
        },
    },
    {
        "id": "MUT-B1-08", "origin": "self", "file": FUND,
        "desc": "🔴 retry 不记次数(commit 重试永远到不了上限 ⇒ 另一种无限挂钱)",
        "from": '''    attempts = _store.bump_settlement_attempt(
        cur, publish_command_id=str(command_id), error=outcome.reason,
    )''',
        "to": '''    attempts = 0''',
        "expect": {
            f"py::{B1}::test_b1_03_billing_exception_writes_no_terminal",
            f"py::{B1}::test_b1_30_commit_retry_has_a_ceiling",
            f"py::{B1}::test_b1_31_release_retry_has_no_ceiling",
        },
    },
    {
        "id": "MUT-B1-09", "origin": "self", "file": FUND,
        "desc": "🔴 已结算的再调不再是 no-op(重复结算改写终态时间戳)",
        "from": '''    if command.get("settled_at") is not None:''',
        "to": '''    if False:''',
        "expect": {
            f"py::{B1}::test_b1_13_already_settled_command_is_a_noop",
        },
    },
    {
        "id": "MUT-B1-10", "origin": "self", "file": STORE,
        "desc": "🔴 终态入口放开:任何资金态都能从这里写出去",
        "from": '''    if funding_state not in SETTLEMENT_TERMINAL_FUNDING_STATES:
        raise StoreError(''',
        "to": '''    if False:
        raise StoreError(''',
        "expect": {
            f"py::{B1}::test_b1_93_terminal_funding_states_agree_across_modules",
        },
    },
    {
        "id": "MUT-B1-11", "origin": "self", "file": RECON,
        "desc": "🔴 收敛器不看 verdict(裁决 retry/manual 也照落 action + 终态)",
        "from": '''    if outcome.settled:
        return True
    _funding.apply_unsettled(cur, row, outcome)''',
        "to": '''    if True:
        return True
    _funding.apply_unsettled(cur, row, outcome)''',
        "expect": _B1_01_ALL | {
            f"py::{B1}::test_b1_02_release_that_did_not_settle_writes_no_terminal[ambiguous-manual]",
            f"py::{B1}::test_b1_02_release_that_did_not_settle_writes_no_terminal[idempotent_committed-manual]",
            f"py::{B1}::test_b1_02_release_that_did_not_settle_writes_no_terminal[not_found-retry]",
            f"py::{B1}::test_b1_04_manual_verdict_lands_in_the_z1_queue",
            f"py::{B1}::test_b1_05_manual_command_is_not_reattempted_next_round",
            f"py::{B1}::test_b1_30_commit_retry_has_a_ceiling",
            # 正当溢出(第一版漏写):收敛器不再调 apply_unsettled ⇒
            # settlement_attempts 一次都不涨,异常臂与 release 臂的计数断言也垮。
            f"py::{B1}::test_b1_03_billing_exception_writes_no_terminal",
            f"py::{B1}::test_b1_31_release_retry_has_no_ceiling",
        },
    },
    # ══════════════════════════════════════════════════════════════════
    # B-2 · 租约 fencing(P1-1)
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-B2-01", "origin": "self", "file": STORE,
        "desc": "🔴 settle 的 token CAS 摘掉(迟到的收尾盖掉新持有者)",
        "from": '''         WHERE id = %s AND status = 'claimed' AND claim_token = %s
        """,
        (status, (last_error or None), int(outbox_id), str(claim_token)),''',
        "to": '''         WHERE id = %s AND status = 'claimed' AND %s IS NOT NULL
        """,
        (status, (last_error or None), int(outbox_id), str(claim_token)),''',
        "expect": {
            f"py::{B2}::test_b2_02_stale_lease_cannot_finish_the_row[settle]",
        },
    },
    {
        "id": "MUT-B2-02", "origin": "self", "file": STORE,
        "desc": "🔴 defer 的 token CAS 摘掉(过期租约把新持有者的 available_at 推后)",
        "from": '''         WHERE id = %s AND status = 'claimed' AND claim_token = %s
        """,
        (last_error[:2000], int(seconds), int(outbox_id), str(claim_token)),''',
        "to": '''         WHERE id = %s AND status = 'claimed' AND %s IS NOT NULL
        """,
        (last_error[:2000], int(seconds), int(outbox_id), str(claim_token)),''',
        "expect": {
            f"py::{B2}::test_b2_02_stale_lease_cannot_finish_the_row[defer]",
        },
    },
    {
        "id": "MUT-B2-03", "origin": "self", "file": STORE,
        "desc": "🔴 claim 不返 token(fencing 无凭据可用)",
        "from": '''                  o.status, o.attempt_count, o.claim_token''',
        "to": '''                  o.status, o.attempt_count''',
        "expect": _B2_STALE | {
            f"py::{B2}::test_b2_01_claim_returns_the_fencing_token",
            f"py::{B2}::test_b2_03_current_lease_can_finish_the_row[settle]",
            f"py::{B2}::test_b2_03_current_lease_can_finish_the_row[defer]",
            # 正当溢出(第一版漏写):worker 取 row["claim_token"] 会 KeyError,
            # 整条真派发挂掉 ⇒ B-5 里依赖真派发的两条臂一起红。
            f"py::{B5}::test_b5_10_duplicate_ref_refuses_binding_and_quarantines",
            f"py::{B5}::test_b5_11_distinct_refs_both_bind",
        },
    },
    {
        "id": "MUT-B2-04", "origin": "self", "file": DISPATCH,
        "desc": "🔴 「已结算不外发」那道闸摘掉(先退款后外发窗口重开)",
        "from": '''    if command.get("settled_at") is not None:
        logger.warning("[defgeo-publish] %s 已结算(settled_at 非空),拒绝外发", command_id)''',
        "to": '''    if False:
        logger.warning("[defgeo-publish] %s 已结算(settled_at 非空),拒绝外发", command_id)''',
        "expect": {
            f"py::{B2}::test_b2_10_a_settled_command_is_never_dispatched",
        },
    },
    # ══════════════════════════════════════════════════════════════════
    # B-3 · 通道能力 + attempt 上限(P1-2)
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-B3-01", "origin": "self", "file": PUBAPI,
        "desc": "🔴 capability 改回写死 True(那一格守卫的候选集永远为空)",
        "from": '''        capability_available=_transport.readiness().ready,''',
        "to": '''        capability_available=True,''',
        "expect": {
            f"py::{B3}::test_b3_02_confirm_is_typed_503_with_zero_side_effects",
            f"py::{B3}::test_b3_04_capability_flag_is_not_hardcoded",
        },
    },
    {
        "id": "MUT-B3-02", "origin": "self", "file": WORKER,
        "desc": "🔴 attempt 上限摘掉(通道不通 ⇒ 无限延期挂钱)",
        "from": '''            if int(row.get("attempt_count") or 0) >= MAX_ATTEMPTS:''',
        "to": '''            if False:''',
        "expect": {
            f"py::{B3}::test_b3_11_transport_down_stops_deferring_at_the_ceiling",
            f"py::{B3}::test_b3_12_the_reconciler_can_now_reach_it_and_refund",
            f"py::{B3}::test_b3_13_ceiling_branch_exists_structurally",
        },
    },
    {
        "id": "MUT-B3-03", "origin": "self", "file": WORKER,
        "desc": "🔴 达限时顺手动资金态(队列失败与钱向混成一件事)",
        "from": '''                status="needs_review",
                last_error=f"外发通道持续不可用,已达 {MAX_ATTEMPTS} 次:{reason}"[:2000],''',
        "to": '''                status="needs_review",
                last_error=f"通道不可用:{reason}"[:2000],''',
        "expect": {
            f"py::{B3}::test_b3_11_transport_down_stops_deferring_at_the_ceiling",
        },
    },
    # ══════════════════════════════════════════════════════════════════
    # B-4 · 预算分母(P1-3)
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-B4-01", "origin": "self", "file": STORE,
        "desc": "🔴 平台腿改回不进预算分母(= P1-3 原样复活)",
        "from": '''    "exempt_recorded": "by_settlement",''',
        "to": '''    "exempt_recorded": "none",''',
        "expect": {
            f"py::{B4}::test_b4_03_budget_denominator_covers_every_funding_state",
            f"py::{B4}::test_b4_10_unsettled_platform_leg_counts_as_reserved",
            f"py::{B4}::test_b4_11_committed_platform_leg_counts_as_committed",
            f"py::{B4}::test_b4_14_cap_gate_sees_the_platform_leg",
        },
    },
    {
        "id": "MUT-B4-02", "origin": "self", "file": STORE,
        "desc": "🔴 覆盖性自证摘掉(分母漏一格静默按 0,不再有人红)",
        "from": '''    if missing or extra:
        raise StoreError(''',
        "to": '''    if False:
        raise StoreError(''',
        "expect": {
            f"py::{B4}::test_b4_04_coverage_assert_is_alive",
        },
    },
    {
        "id": "MUT-B4-03", "origin": "self", "file": STORE,
        "desc": "🔴 「哪些 canonical 算已扣」改回手抄两个字符串",
        "from": '''        if _s.settlement_direction(st) in ("commit", "preserve_historical_commit")''',
        "to": '''        if st in ("verified_published",)''',
        "expect": {
            f"py::{B4}::test_b4_05_commit_direction_states_are_derived_not_hardcoded",
            # 🔴 这一条是本发变异**逼出来**的:第一版只有 b4_11(verified_published),
            #    而手抄清单里恰好留着它 ⇒ 行为面一条都没红。
            #    新增 b4_11b 覆盖 retracted(preserve_historical_commit)那一格。
            f"py::{B4}::test_b4_11b_retracted_platform_leg_still_counts_as_committed",
        },
    },
    # ══════════════════════════════════════════════════════════════════
    # B-5 · 上游单号唯一(P1-5)
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-B5-01", "origin": "self", "file": DISPATCH,
        "desc": "🔴 撞号预检摘掉(旧单绑到新命令 / UniqueViolation 炸掉整个派发事务)",
        "from": '''        if owner is not None and str(owner["publish_command_id"]) != str(command_id):''',
        "to": '''        if False:''',
        "expect": {
            f"py::{B5}::test_b5_10_duplicate_ref_refuses_binding_and_quarantines",
        },
    },
    {
        "id": "MUT-B5-02", "origin": "self", "file": MHZ,
        "desc": "🔴 strict 反查改回「挑第一条」(旧订单被绑到新命令上)",
        "from": '''                if strict_unique and len(_matched) > 1:''',
        "to": '''                if False and len(_matched) > 1:''',
        "expect": {
            # 🔴 这一发**第一版整发存活**:``test_b5_20`` 只断言返回 ""——
            #    而摘掉拒绝之后 strict 分支从不赋 _hit_sn,照样返回 ""。
            #    「拒绝绑定」与「这一轮没匹配到」在返回值上不可区分 ⇒ 区分力为 0。
            #    b5_20b 钉的是两个**可观测**的差别:只发一次请求 + 落拒绝日志。
            f"py::{B5}::test_b5_20b_refusal_is_distinguishable_from_no_match",
        },
    },
    {
        "id": "MUT-B5-03", "origin": "self", "file": MHZ,
        "desc": "🔴 批量反查那一半的同族谓词摘掉(同一目标的语法形态只堵一种 = 没堵)",
        "from": '''                        if len(sns) > 1:''',
        "to": '''                        if False:''',
        "expect": {
            f"py::{B5}::test_b5_24_batch_lookup_refuses_multiple_matches_too",
        },
    },
    {
        "id": "MUT-B5-04", "origin": "self", "file": TRANSPORT,
        "desc": "🔴 发布链不开 strict(上游反查仍会挑第一条)",
        "from": '''                strict_order_ref_binding=True,''',
        "to": '''                strict_order_ref_binding=False,''',
        "expect": {
            f"py::{B5}::test_b5_23_defgeo_transport_opts_into_strict_binding",
        },
    },
    {
        "id": "MUT-B5-05", "origin": "self", "file": MHZ,
        "desc": "🔴 把 strict 变成默认(老链行为被改写 —— 追加谓词变成改写原谓词)",
        "from": '''        delay_seconds: float = 1.0,
        strict_unique: bool = False,
    ) -> str:''',
        "to": '''        delay_seconds: float = 1.0,
        strict_unique: bool = True,
    ) -> str:''',
        "expect": {
            f"py::{B5}::test_b5_22_legacy_default_behaviour_is_byte_for_byte_unchanged",
        },
    },

    # ══════════════════════════════════════════════════════════════════
    # B-7 · Review §7.4-4 五条缺口的新判据 · 自选撕锁(第一轮 observe)
    # ══════════════════════════════════════════════════════════════════
    # 🔴 observe 不是偷懒:这几发会不会牵连既有判据族,我现在猜不准。
    #    编一个 expect 出来、跑完再改成实测值 = 自己给自己批改。
    #    第一轮记实测红集,合树尖那轮再定死。
    {
        "id": "MUT-B7-01", "origin": "review_extsel", "file": RECON, "expect": None,
        "desc": "🔴 ⑦ 删掉「零调用计数」那一半保险(只剩 marker 一层)",
        "from": "           AND c.provider_call_count = 0\n",
        "to": "",
    },
    {
        "id": "MUT-B7-02", "origin": "review_extsel", "file": RECON, "expect": None,
        "desc": "🔴 ⑦ 的候选集不再排除已隔离命令(收敛器替人工把钱结了)",
        "from": "           AND c.command_state NOT IN "
                "('completed','failed','cancelled','quarantined')\n",
        "to": "           AND c.command_state NOT IN "
              "('completed','failed','cancelled')\n",
    },
    {
        "id": "MUT-B7-03", "origin": "review_extsel", "file": RECON, "expect": None,
        "desc": "🔴 ④ 的候选集不再排除已隔离命令(同一纪律的第二处落点)",
        "from": "           AND command_state <> 'quarantined'\n",
        "to": "",
    },
    {
        "id": "MUT-B7-04", "origin": "review_extsel", "file": WORKER, "expect": None,
        "desc": "🔴 _settle_row 上限 off-by-one(第 N+1 次才终局 = 多挂一轮)",
        "from": "    terminal = attempt_count >= MAX_ATTEMPTS",
        "to": "    terminal = attempt_count > MAX_ATTEMPTS",
    },
    {
        "id": "MUT-B7-05", "origin": "review_extsel", "file": WORKER, "expect": None,
        "desc": "🔴 _settle_row 的兜底只收窄成 ValueError(收尾失败会掀翻整轮派发)",
        "from": "    except Exception as exc:                              # noqa: BLE001\n"
                '        logger.warning("[defgeo-publish] outbox=%s 收尾失败:%s", outbox_id, exc)',
        "to": "    except ValueError as exc:\n"
              '        logger.warning("[defgeo-publish] outbox=%s 收尾失败:%s", outbox_id, exc)',
    },
    {
        "id": "MUT-B7-06", "origin": "review_extsel", "file": MIG051, "expect": None,
        "desc": "🔴 051 index-guard 的「异表同名 ⇒ RAISE」分支退化成静默跳过",
        "from": "    ELSIF EXISTS (SELECT 1 FROM pg_class c\n",
        "to": "    ELSIF FALSE THEN NULL;\n    ELSIF EXISTS (SELECT 1 FROM pg_class c\n",
    },
    {
        "id": "MUT-B7-07", "origin": "review_extsel", "file": MHZ, "expect": None,
        "desc": "🔴 publish 单 media 那个调用点丢掉 strict(枚举锁要把它抓成未分类)",
        "from": '                        retries=3, delay_seconds=1.0,\n'
                '                        strict_unique=strict_order_ref_binding,\n'
                '                    )\n'
                '                    if order_sn:',
        "to": '                        retries=3, delay_seconds=1.0,\n'
              '                    )\n'
              '                    if order_sn:',
    },
    # ══════════════════════════════════════════════════════════════════
    # 外选(独立审计挑的 —— 判据作者没参与选题)· 第一轮 observe
    # ══════════════════════════════════════════════════════════════════
    # 🔴 本仓记过:自己写判据又自己挑变异 = 自己出题自己批改。
    #    下面这批由一个**只读了代码与判据、没写过判据**的审计方挑,
    #    并且它**预判全部存活**。所以必须配 MUT-AUD-CTRL(预判大面积红)
    #    一起跑:对照不红 ⇒ 这一整轮结论作废(尺子是死的)。
    {
        "id": "MUT-AUD-CTRL", "origin": "audit", "file": FUND, "expect": None,
        "desc": "🟢 活性对照:commit / release 两个 billing 原语对调(预判大面积红)",
        "from": '    primitive = commit_freeze if intent == "commit" else release_freeze',
        "to": '    primitive = release_freeze if intent == "commit" else commit_freeze',
    },
    {
        "id": "MUT-AUD-01", "origin": "audit", "file": FUND, "expect": None,
        "desc": "🔴 commit 重试上限 off-by-one:第 6 次才转人工(多挂一轮钱)",
        "from": '    if outcome.intent == "commit" and attempts >= SETTLE_MAX_ATTEMPTS:',
        "to": '    if outcome.intent == "commit" and attempts > SETTLE_MAX_ATTEMPTS:',
    },
    {
        "id": "MUT-AUD-02", "origin": "audit", "file": FUND, "expect": None,
        "desc": "🔴 结算重试上限改值(注释自称与诊断链同值,无人对账)",
        "from": "SETTLE_MAX_ATTEMPTS = 5",
        "to": "SETTLE_MAX_ATTEMPTS = 3",
    },
    {
        "id": "MUT-AUD-03", "origin": "audit", "file": DISPATCH, "expect": None,
        "desc": "🔴 钱向→commandState 闭表里 commit / release 两格对调",
        "from": '        "commit": "completed",\n'
                '        "preserve_historical_commit": "completed",\n'
                '        "release": "failed",',
        "to": '        "commit": "failed",\n'
              '        "preserve_historical_commit": "completed",\n'
              '        "release": "completed",',
    },
    {
        "id": "MUT-AUD-04", "origin": "audit", "file": FUND, "expect": None,
        "desc": "🔴 无物理腿那条早退分支:CAS 落空却报「已结算」",
        "from": '            return SettlementOutcome("retry", intent, "terminal_cas_missed", {})',
        "to": '            return SettlementOutcome("settled", intent, "no_physical_leg", {})',
    },
    {
        "id": "MUT-AUD-05", "origin": "audit", "file": STORE, "expect": None,
        "desc": "🔴 mark_settled 丢掉「至多一次」(已结算的 settled_at 可被改写)",
        "from": '        f"WHERE publish_command_id = %s AND settled_at IS NULL",',
        "to": '        f"WHERE publish_command_id = %s",',
    },
    {
        "id": "MUT-AUD-06", "origin": "audit", "file": STORE, "expect": None,
        "desc": "🔴 预算分母覆盖自证的**取值域**那一半变成死代码",
        "from": "    bad = sorted(v for v in _BUDGET_CONTRIBUTION.values()"
                " if v not in BUDGET_CONTRIBUTIONS)",
        "to": "    bad = []",
    },
    {
        "id": "MUT-AUD-07", "origin": "audit", "file": RECON, "expect": None,
        "desc": "🔴 收敛器⑦ 丢掉「权威零接单」证据(已外调的也自动退款)",
        "from": "           AND c.external_start_at IS NULL\n"
                "           AND c.provider_call_count = 0",
        "to": "           AND (c.external_start_at IS NULL"
              " OR c.external_start_at IS NOT NULL)\n"
              "           AND c.provider_call_count >= 0",
    },
    {
        "id": "MUT-AUD-08", "origin": "audit", "file": RECON, "expect": None,
        "desc": "🔴 收敛器④ 候选集丢掉隔离闸(已转人工的每轮被重新结算)",
        "from": "           AND command_state <> 'quarantined'",
        "to": "           AND command_state <> '__never_a_command_state__'",
    },
    {
        "id": "MUT-AUD-09", "origin": "audit", "file": RECON, "expect": None,
        "desc": "🔴 收敛器⑦ 终态排除表丢掉 quarantined(人工核验中的被退款抢跑)",
        "from": "           AND c.command_state NOT IN"
                " ('completed','failed','cancelled','quarantined')",
        "to": "           AND c.command_state NOT IN ('completed','failed','cancelled')",
    },
    {
        "id": "MUT-AUD-10", "origin": "audit", "file": RECON, "expect": None,
        "desc": "🔴「加宽候选集不许加宽结算面」那道闸退化成只打日志",
        "from": "            logger.warning(\n"
                '                "[defgeo-reconcile] %s 非平台腿却是 %s,跳过结算",\n'
                '                row["publish_command_id"], before)\n'
                "            continue",
        "to": "            logger.warning(\n"
              '                "[defgeo-reconcile] %s 非平台腿却是 %s,跳过结算",\n'
              '                row["publish_command_id"], before)\n'
              "            pass",
    },
    {
        "id": "MUT-AUD-11", "origin": "audit", "file": DISPATCH, "expect": None,
        "desc": "🔴 上游单号绑定不再限于 accepted(错误文案被当结算身份)",
        "from": '    if result.kind == "accepted" and result.detail:',
        "to": "    if result.detail:",
    },
    {
        "id": "MUT-AUD-12", "origin": "audit", "file": DISPATCH, "expect": None,
        "desc": "🔴 external_start marker 从**外调前**挪到外调后(崩在中间=钱花了但没留痕)",
        # 🔴 审计方原始编号 12,唯一一发**块移动**型;单锚整块替换,已实测 count=1。
        "from": '    if on_external_start_committed is not None:\n'
                '        # 🔴 marker 落盘**在外调之前**。这一行失败就一步都不再往下走 ——\n'
                '        #    提交不了意味着 marker 没熬过去,此时外调等于把「调过没有」\n'
                '        #    这件事交给运气。\n'
                '        on_external_start_committed()\n'
                '\n'
                '    # ── ④ 真正外调 ────────────────────────────────────────────────────\n'
                '    try:\n'
                '        result = provider_call(dict(command))',
        "to": '    # ── ④ 真正外调 ────────────────────────────────────────────────────\n'
              '    try:\n'
              '        result = provider_call(dict(command))\n'
              '        if on_external_start_committed is not None:\n'
              '            on_external_start_committed()',
    },
    {
        "id": "MUT-AUD-13", "origin": "audit", "file": REVIEW, "expect": None,
        "desc": "🔴 Z-1 的「维持隔离」变成一个**会扣钱**的动作",
        "from": '    "admin_hold": None,',
        "to": '    "admin_hold": "committed",',
    },
    {
        "id": "MUT-AUD-14", "origin": "audit", "file": MHZ, "expect": None,
        "desc": "🔴 批量 strict 去掉 sn 去重(合法订单被误判撞号,永不绑定)",
        "from": "                        bucket = _seen.setdefault(mid, [])\n"
                "                        if sn not in bucket:\n"
                "                            bucket.append(sn)",
        "to": "                        bucket = _seen.setdefault(mid, [])\n"
              "                        bucket.append(sn)",
    },
    {
        "id": "MUT-AUD-15", "origin": "audit", "file": RECON, "expect": None,
        "desc": "🔴 ⑦ 双保险里单独摘掉 provider_call_count = 0 那一层",
        "from": "           AND c.provider_call_count = 0",
        "to": "           AND c.provider_call_count >= 0",
    },
    {
        "id": "MUT-AUD-16", "origin": "audit", "file": FUND, "expect": None,
        "desc": "🔴 _assert_direction 静默放宽(未起步态也放行)",
        "from": "    if direction != expected:",
        "to": '    if direction not in (expected, "none"):',
    },
]


# ══════════════════════════════════════════════════════════════════════════
# expect 追加表 · [Review 令 ② 2026-08-26] 判据 90 → 103 之后的分母修正
# ══════════════════════════════════════════════════════════════════════════
# 🔴 这**不是**"把实测抄进期望"。这 26 发的 expect 集当初是按 **90 条**判据的
#    分母写的;B-6(12 条)与 B-7(13 条)落地后,新判据也站在同一条纪律上,
#    于是同一发变异多杀几条。不更新 ⇒ runner 永远报"不精确"(实测 15/26)
#    ⇒ **真正的溢出会被淹在这 11 发已知噪音里**。
# 🔴 每条都必须写得出「为什么这一条**该**红」。写不出来的不加 ——
#    那种情况说明要么变异出题错,要么判据出题错,得停下来查,不是调期望。
EXPECT_ADDENDA: dict[str, dict[str, str]] = {
    "MUT-B1-01": {
        # 顺序反转被打回 ⇒ 物理没成也落终态。b6_01 驱动的正是"第 N 次达限"
        # 那条边界:终态提前落下,retry 计数根本走不到上限。
        f"py::test_b6_audit_gaps_pg.py::test_b6_01_commit_ceiling_fires_on_exactly_the_nth_attempt":
            "终态提前落 ⇒ 永远走不到重试上限,边界判据当然红",
        f"py::test_b6_audit_gaps_pg.py::test_b6_10_zero_amount_command_settles_without_touching_billing":
            "「无物理腿」早退分支与终态写点是同一段;顺序一反,零额单也被当成走过 billing",
        f"py::test_b7_review_gaps_pg.py::test_b7_02_the_same_shape_with_zero_calls_is_really_refunded":
            "⑦ 的活性对照臂依赖 release 真落终态;顺序反转后它落的是假终态",
    },
    "MUT-B1-02": {
        f"py::test_b6_audit_gaps_pg.py::test_b6_01_commit_ceiling_fires_on_exactly_the_nth_attempt":
            "success 不为 True 也落终态 ⇒ 同样走不到上限",
    },
    "MUT-B1-05": {
        f"py::test_b6_audit_gaps_pg.py::test_b6_11_zero_amount_command_with_a_stale_version_is_retry_not_settled":
            "CAS 摘掉 ⇒ 陈旧版本也能推进,这条正是钉「陈旧版本必须 retry」",
    },
    "MUT-B1-08": {
        f"py::test_b6_audit_gaps_pg.py::test_b6_01_commit_ceiling_fires_on_exactly_the_nth_attempt":
            "retry 不记次数 ⇒ 上限判据直接失去输入",
    },
    "MUT-B1-11": {
        f"py::test_b6_audit_gaps_pg.py::test_b6_01_commit_ceiling_fires_on_exactly_the_nth_attempt":
            "收敛器不看 verdict ⇒ retry 裁决被当成已结算,计数不再累积",
    },
    "MUT-B2-01": {
        f"py::test_b7_review_gaps_pg.py::test_b7_23_settle_row_carries_the_fencing_token":
            "settle 的 token CAS 就是 _settle_row 那条出口用的同一道 fencing",
    },
    "MUT-B2-03": {
        # claim 不返 token ⇒ 所有拿 token 收尾的路径全断,B-7 那五条
        # 打 _settle_row 的判据(它们都要真领一次租约)整族红。
        f"py::test_b6_audit_gaps_pg.py::test_b6_41_authoritative_rejection_lands_on_failed_not_completed":
            "真链驱动 rejected 钱向要先领租约派发;领不到 token 就走不到那一步",
        f"py::test_b6_audit_gaps_pg.py::test_b6_50_non_accepted_detail_never_becomes_an_order_ref":
            "同上:绑定谓词判据要真派发一次",
        f"py::test_b7_review_gaps_pg.py::test_b7_20_settle_row_defers_below_the_ceiling":
            "_settle_row 五条判据都要真领一次租约拿 claim_token",
        f"py::test_b7_review_gaps_pg.py::test_b7_21_the_ceiling_is_inclusive_not_off_by_one": "同上",
        f"py::test_b7_review_gaps_pg.py::test_b7_22_settle_row_never_touches_the_command_funding_state": "同上",
        f"py::test_b7_review_gaps_pg.py::test_b7_23_settle_row_carries_the_fencing_token": "同上",
        f"py::test_b7_review_gaps_pg.py::test_b7_24_settle_row_swallows_db_errors_instead_of_raising": "同上",
        # [Review 三裁 ② 2026-08-26] b7_21 拆轴后新增的两条,机制与上面五条**完全相同**:
        # 都经 `_fresh_outbox` 真领一次租约;claim 不返 token ⇒ 领不到 ⇒ 根本走不到被测那一步。
        # 这不是「把观测抄进期望」—— 是同一个**已记录在案**的原因多覆盖了两条判据。
        f"py::test_b7_review_gaps_pg.py::test_b7_21b_the_ceiling_actually_ends_the_retry_loop": "同上",
        f"py::test_b7_review_gaps_pg.py::test_b7_21c_an_exhausted_row_lands_in_the_one_terminal_a_human_reads": "同上",
        # [E1 · 2026-08-26] E1-1 之后 `dispatch_once` **必须带租约凭据**
        # (marker 的 CAS 要验「outbox 仍被我这次租约持有」)。所以
        # 「真领一次租约」这件事从 _settle_row 那几条扩散到了整条派发链:
        # claim 不返 token ⇒ 判据拿不到凭据 ⇒ 断在起跑线,走不到被测那一步。
        # 与上面 b7_20..24 是**同一个已在案的机制**,不是新理由。
        f"py::test_b2_outbox_fencing_pg.py::test_b2_11_an_unsettled_command_still_dispatches":
            "活路径要真领一次租约才发得出去 —— 领不到就发不出",
        f"py::test_e1_publish_race_pg.py::test_e1_00_a_healthy_command_with_a_live_lease_is_marked":
            "E1 族每一条都真领一次租约(_fresh);claim 不返 token 就没有凭据",
        f"py::test_e1_publish_race_pg.py::test_e1_01_a_stale_lease_cannot_mark": "同上",
        f"py::test_e1_publish_race_pg.py::test_e1_02_a_settled_command_cannot_mark": "同上",
        f"py::test_e1_publish_race_pg.py::test_e1_03_a_non_dispatchable_funding_state_cannot_mark": "同上",
        f"py::test_e1_publish_race_pg.py::test_e1_04_a_non_dispatchable_command_state_cannot_mark": "同上",
        f"py::test_e1_publish_race_pg.py::test_e1_10_the_reported_four_step_interleaving_dispatches_nothing":
            "同上 —— 双连接交错那条也要先真领一次租约",
    },
    "MUT-B3-02": {
        f"py::test_b7_review_gaps_pg.py::test_b7_01_a_command_with_provider_calls_is_never_auto_refunded":
            "这两条的前提是「通道不通达上限 ⇒ outbox 进 needs_review」,上限摘掉就造不出前提",
        f"py::test_b7_review_gaps_pg.py::test_b7_02_the_same_shape_with_zero_calls_is_really_refunded": "同上",
        # [E1 · 2026-08-26] 上限摘掉 ⇒ 永远不达限 ⇒ 不转人工 ⇒
        # quarantined 计数为 0 ⇒ 这条**活性对照**(真转人工必须计数)当场落空。
        # 正向承重,它就该在这一发下红。
        f"py::test_e1_publish_race_pg.py::test_e1_33_a_quarantine_that_landed_is_counted":
            "上限摘掉就永远不转人工,这条活性对照当场落空",
    },
    "MUT-B4-01": {
        f"py::test_b4_budget_denominator_pg.py::test_b4_11b_retracted_platform_leg_still_counts_as_committed":
            "b4_11b 本身就是审计逼出来的「平台腿进分母」行为判据,与本发同一条纪律",
    },
    "MUT-B5-04": {
        f"py::test_b7_review_gaps_pg.py::test_b7_32_the_defgeo_entrypoint_really_asks_for_strict_binding":
            "b7_32 直接断言 provider_transport 传了 strict_order_ref_binding=True",
    },
    # 🔴 **撤下**(2026-08-26):原本给 MUT-B5-05 追加了
    #    `test_b5_03_null_order_refs_do_not_collide`,理由写的是「strict 变默认
    #    = 改写老链默认路径」。但同一发变异 v1 红、v2 不红 —— 理由站不住:
    #    b5_03 验的是**库层**唯一索引的 NULL 豁免,而 strict 只影响 MHZ 上游反查,
    #    两者没有因果。更可能是跨轮库状态污染(v2 那一轮正撞上别的窗口在提交)。
    #    追加表的规矩是「写不出理由的不加」——撤下,单独复跑定性。
    # "MUT-B5-05": {...}
}


# 🔴 [E1 · 2026-08-26] 追加表**不许有重复键**。
#    Python 字典字面量的重复键是**后者静默覆盖前者** —— 没有语法错、没有告警。
#    我就是这么把 MUT-B3-02 原有的 b7_01/b7_02 两条挤掉的,
#    结果它们从期望集消失、在报表里变成"溢出",看起来像判据在说话。
def _assert_no_duplicate_addenda_keys() -> None:
    import ast as _ast
    import pathlib as _pl

    src = _pl.Path(__file__).read_text(encoding="utf-8")
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.AnnAssign):
            continue
        tgt = getattr(node.target, "id", "")
        if tgt != "EXPECT_ADDENDA" or not isinstance(node.value, _ast.Dict):
            continue
        keys = [k.value for k in node.value.keys if isinstance(k, _ast.Constant)]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise SystemExit(
                f"🔴 EXPECT_ADDENDA 有重复键 {dupes} —— "
                "字典字面量重复键会**静默**丢掉前一份,期望集会缺条目")
        return
    raise SystemExit("🔴 没扫到 EXPECT_ADDENDA 的字典字面量 —— 自证探针写废了")


_assert_no_duplicate_addenda_keys()

for _mut in MUTATIONS:
    _extra = EXPECT_ADDENDA.get(_mut["id"])
    if _extra and _mut.get("expect") is not None:
        _mut["expect"] = set(_mut["expect"]) | set(_extra)


#: 红数占基线的比例达到这个值就判**钝杀** —— 杀成立,但红集不携带定位信息。
#: 见 ``_reset_package_db`` 上方那段:会动钱的变异会在**同一 session 内**
#: 把后续每条钱包断言连锁打红,那不是"这些判据都承重"。
BLUNT_KILL_RED_FRACTION = 0.5

def _reset_package_db() -> None:
    """把本包的一次性库**推倒重建**。每发变异前调一次。

    🔴 [E1-3① 追加 · 2026-08-26 实测逼出来的] 26 发共用一个库时,残留会
       跨变异累积:批跑里 18 发报"不精确"、溢出最多 58 条(48 条是既有判据、
       9 发共享同一签名),而**逐发单跑全部精确**。溢出条数随批次位置单调增长
       —— 那是残留曲线,不是判据在说话。

       包里的 ``_clean_queue`` 是 session 级,且只清本包写的那几张表;
       钱包 / 冻结 / 种子这些**清理范围之外**的行会攒下来。

       所以这里做整库级隔离:不列表、不挑表 —— 手写表名清单漏掉的那一张
       不会让任何判据变红(本仓记过)。库由 conftest 的 ``_schema`` 重建。
    """
    import psycopg2

    dsn = DB_URL
    name = dsn.rsplit("/", 1)[-1].split("?")[0]
    low = name.lower()
    # 安全栓与 conftest 同一套:库名必须同时含这三段,少一段就拒绝 DROP。
    for token in ("defgeo", "wob", "test"):
        if token not in low:
            raise SystemExit(
                f"安全栓:库名 {name!r} 不含 {token!r} —— 拒绝 DROP 一个可能是真库的库")
    admin = dsn.rsplit("/", 1)[0] + "/postgres"
    conn = psycopg2.connect(admin)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()


def main() -> int:
    # 🔴 [Review 机制令 2026-08-26 ①] 树级排他锁 —— 本 runner 就地改源文件,
    #    并发下毒落在谁的基线上无法归属(2026-08-26 三伤,口头串行第二次失守)。
    with tree_lock("mutation_runner_wob_publish_funding"):
        return _main_locked()


def _main_locked() -> int:
    _assert_disk_headroom()
    print("═" * 72)
    print("工单B 撕锁自证 · 基线")
    print("═" * 72)
    # 🔴 [Review 机制令 ②] 本轮面对的是**哪一版判据** —— 报数必须挂得上指纹,
    #    否则「同一发变异 v1 红 v2 不红」这种事永远只能靠猜(MUT-B5-05 前科)。
    _fp, _meta = criteria_fingerprint(PYTEST_TARGETS)
    print(f"判据指纹 {_fp} · {_meta['n_files']} 个判据文件 · 目标 {list(PYTEST_TARGETS)}")
    # 🔴 [E1-3①] 基线也要量在干净库上 —— 上一轮跑剩的残留会让基线本身带色。
    _reset_package_db()
    print("库已重建(基线前)\n")
    base_red, base_green, _ = run_criteria()
    if base_red:
        print(f"🔴 基线不是全绿:{sorted(base_red)} —— 后面全部作废")
        return 2
    print(f"✅ 基线全绿({base_green} 条)\n")

    passed = failed = 0
    blunt_kills: list[str] = []
    # 🔴 三种来源各自成栏(self / audit / review_extsel)。用 defaultdict:
    #    写死两个 key 时,新来源会 KeyError —— 而"报数崩掉"比"报数合并"好,
    #    真正危险的是它**不崩**、悄悄把他选算进自选那一栏。
    import collections as _collections
    by_origin: dict[str, list[int]] = _collections.defaultdict(lambda: [0, 0])
    observed: list[tuple[str, str, int]] = []
    for mut in MUTATIONS:
        if ONLY and mut["id"] not in ONLY:
            continue
        if ORIGIN and str(mut.get("origin", "self")) not in ORIGIN:
            continue
        # 🔴 [E1-3①] **每发之前重建库**。不这样做,第一发级联之后
        #    后面每一发的红集都掺着上一发的残留,而那些红看起来像发现。
        _reset_package_db()
        path: Path = mut["file"]
        origin = str(mut.get("origin", "self"))
        original = path.read_bytes()
        text = original.decode("utf-8")
        hits = text.count(mut["from"])
        if hits != 1:
            print(f"❌ {mut['id']} 锚点命中 {hits} 次(应为 1)—— 变异没打进去")
            failed += 1
            by_origin.setdefault(origin, [0, 0])[1] += 1
            continue
        # 🔴 备份先落盘:崩了也能人工还原(``<name>.mutbak``)。
        backup = path.with_suffix(path.suffix + ".mutbak")
        backup.write_bytes(original)
        path.write_bytes(text.replace(mut["from"], mut["to"], 1).encode("utf-8"))
        try:
            red, green, _ = run_criteria()
        finally:
            _restore(path, original)
            backup.unlink(missing_ok=True)     # 只有还原**核验通过**才删备份

        if mut.get("expect") is None:
            # 🔴 observe 模式:先**看**实际红了什么,再决定它是"该被杀"还是
            #    "判据面真有洞"。自己先写死一个期望再去跑,等于自己出题自己批改。
            mark = "🟢 KILLED  " if red else "🔴 SURVIVED"
            print(f"{mark} {mut['id']} [{origin}] · {mut['desc']}")
            print(f"    实红 {len(red)} 条 / 绿 {green} 条")
            for n in sorted(red):
                print(f"      · {n}")
            observed.append((mut["id"], origin, len(red)))
            continue
        expect = set(mut["expect"])
        missing = sorted(expect - red)
        extra = sorted(red - expect)
        blunt = (not missing) and len(red) >= base_green * BLUNT_KILL_RED_FRACTION
        ok = not missing and not extra
        if blunt and not ok:
            # 钝杀:靶向判据都红了,但红面铺满整包 —— 杀成立,红集不可用于定位。
            mark = "🟡"
            verdict = "钝杀"
        else:
            mark = "✅" if ok else "❌"
            verdict = "精确" if ok else ("欠红" if missing else "溢出")
        print(f"{mark} {mut['id']} [{origin}] · {verdict} · {mut['desc']}")
        print(f"    红 {len(red)} 条 / 绿 {green} 条(基线 {base_green})")
        if missing:
            print(f"    🔴 该红没红:{missing}")
        if blunt and not ok:
            print(f"    🟡 红面 {len(red)}/{base_green} ≥ "
                  f"{BLUNT_KILL_RED_FRACTION:.0%} —— 判**钝杀**:"
                  "期望的都红了,其余是同 session 内的连锁(会动钱的变异会把"
                  "后续每条钱包断言拖红),红集不携带定位信息,不按溢出计。")
        elif extra:
            print(f"    🔴 溢出(没预料到也红了):{extra}")
        blunt_kills.append(mut["id"]) if (blunt and not ok) else None
        passed += int(ok)
        failed += int(not ok and not blunt)
        by_origin.setdefault(origin, [0, 0])[int(not ok and not blunt)] += 1

    if observed:
        print("═" * 72)
        killed = [o for o in observed if o[2] > 0]
        survived = [o for o in observed if o[2] == 0]
        print(f"observe(外选第一轮):杀 {len(killed)} / 存活 {len(survived)}"
              f" (共 {len(observed)} 发)")
        if survived:
            print("  🔴 存活:" + ", ".join(o[0] for o in survived))
    print("═" * 72)
    _scored = [m for m in MUTATIONS
               if m.get("expect") is not None
               and (not ONLY or m["id"] in ONLY)
               and (not ORIGIN or str(m.get("origin", "self")) in ORIGIN)]
    print(f"精确匹配 {passed}/{len(_scored)} · 钝杀 {len(blunt_kills)} · "
          f"真问题(欠红/溢出) {failed}")
    if blunt_kills:
        print("  🟡 钝杀(杀成立、红集不可用于定位):" + ", ".join(blunt_kills))
    print("  🔴 报数口径:精确 与 钝杀 **分栏**,不合并成一个"
          "「26/26」——两者的证明力不同。")
    for origin, (ok, bad) in sorted(by_origin.items()):
        if ok or bad:
            print(f"  · {origin}: 精确 {ok} / 不精确 {bad}(共 {ok + bad} 发)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
