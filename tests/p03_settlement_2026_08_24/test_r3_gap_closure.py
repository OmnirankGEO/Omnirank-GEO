"""【P0-3b R3】把审计实测出的 10 条真缝逐条焊上。

## 这个文件为什么存在

R2 交出去时我报的是「变异 15/15 全红」。那个数字**不能代表判据强度** ——
变异清单是我自己挑的,我挑的都是我判据抓得到的,自己出题自己批改。

一轮独立审计另挑了 10 发打在本包代码上的毒,我的 61 条判据**一发都没抓到**
(同一 harness 里放的活性对照被判红,证明尺子是活的,不是没跑起来)。

根因是判据的**结构性**问题,不是漏写了几条:

1. 整个目录**没有任何一条判据在运行时 import / 执行 `server.py`** ——
   四处 server.py 引用全是 AST 静态检查;
2. 那些 AST 锁用 `ast.dump(...)` 的**子串匹配**判"有没有用对函数"。
   子串正是被穿过去的那道缝:把 `f(result)` 改成 `f({})`,
   或改成 `{k: None for k in f(result)}`,函数名都还在 dump 里 ⇒ 锁全绿,
   而两条结算信号在每一单里恒 None。

所以这一轮的修法是**换锁的形态**,不是再加一条同款锁:
把结构锁从"子串"升级成"**节点级**"(`ast.Call` + func 解析 + 实参必须就是那个变量),
行为缝各配一条真链判据。子串缝一旦焊死,"再往里挪一层"就没有落脚点了。
"""
from __future__ import annotations

import ast
import io
import json
import pathlib

import pytest

from tests.p03_settlement_2026_08_24.test_closeout_p03b import (  # noqa: E402
    _after, _set_split_snapshot, _settle, live_db_048,  # noqa: F401  (fixture 复用)
)
from tests.p03_settlement_2026_08_24.test_settlement_real_pg import (  # noqa: E402
    _conn, _verdict, _world,
)

REPO = pathlib.Path(__file__).resolve().parents[2]


def _server_tree():
    return ast.parse(io.open(REPO / "server.py", encoding="utf-8").read())


def _completion_payload_dicts(tree):
    """完成 payload 的 dict 节点(指纹 = 同时带 diagnosis_id / share_token / terminal)。"""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if {"diagnosis_id", "share_token", "terminal"} <= keys:
            out.append(node)
    return out


# ===========================================================================
# A1 / A2 · 调用点那道缝:结构锁必须是**节点级**,不能是 ast.dump 的子串
# ===========================================================================

def test_payload_splat_is_the_assembler_called_with_the_real_result():
    """锁到节点:那条 `**` 必须**就是** `settlement_signals_from_result(result)`。

    审计实测活下来的两发,都是从"子串匹配"这道缝钻的:
      · `settlement_signals_from_result({})`      —— 函数名还在 dump 里
      · `{k: None for k in settlement_signals_from_result(result)}` —— 同上
    两发都让 delivery_verdict / identity_suspicion 在**每一单**里恒 None ⇒
    降级交付全额扣 + 疑似识别失败永不转人工,即本包存在的理由被整个抵消。

    所以这里逐个节点判:值必须是 `ast.Call`;被调的必须解析到那个组装函数;
    实参必须**恰好是一个** `Name`,且叫 `result`(= workflow 的返回值);不许有关键字参数。
    """
    payloads = _completion_payload_dicts(_server_tree())
    assert payloads, "没定位到诊断完成 payload,分母取错了"

    for node in payloads:
        splats = [v for k, v in zip(node.keys, node.values) if k is None]
        assert splats, "server.py:%d 的完成 payload 没有 ** 展开" % node.lineno

        matched = 0
        for value in splats:
            assert isinstance(value, ast.Call), (
                "server.py:%d 的 ** 展开不是一次直接调用(%s)—— "
                "包一层推导式就能把值推平成 None 而函数名还在"
                % (node.lineno, type(value).__name__))
            func = value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "settlement_signals_from_result":
                continue
            matched += 1
            assert not value.keywords, "组装函数不该有关键字参数,出现了:server.py:%d" % node.lineno
            assert len(value.args) == 1, (
                "server.py:%d 组装函数应恰好收 1 个实参,实收 %d 个" % (node.lineno, len(value.args)))
            arg = value.args[0]
            assert isinstance(arg, ast.Name) and arg.id == "result", (
                "server.py:%d 组装函数的实参不是 workflow 返回值 `result`(而是 %s)—— "
                "传 {} 进去,两条结算信号就在每一单里恒 None"
                % (node.lineno, ast.dump(arg)[:80]))
        assert matched == 1, (
            "server.py:%d 期望恰好 1 处 settlement_signals_from_result 展开,实际 %d 处"
            % (node.lineno, matched))


# ===========================================================================
# A6 / A7 · 守卫的**方向**与文案用的**哪个常量**,也要锁到节点
# ===========================================================================

def _cex_handlers(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.ExceptHandler) and n.name == "cex"]


def test_org_release_guard_direction_is_locked_not_just_its_presence():
    """R-a 的守卫**方向**:必须是 `organization_charge_id is not None`。

    我上一版只断言"这条 if 里提到了 organization_charge_id" —— 把 `is not None`
    整个反过来写成 `is None`,那条锁照样绿(审计实测 61 条全绿)。反过来的后果:
    org 的 charge link **永远不会被释放**,而非 org 的 run 反倒拿
    `charge_link_id=None` 去调 release,TypeError 又被下面那层 except 吞掉 ——
    比不修还坏。
    """
    tree = _server_tree()
    checked = 0
    for handler in _cex_handlers(tree):
        for node in ast.walk(handler):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_release_org_cex"):
                continue
            guards = [p for p in ast.walk(handler)
                      if isinstance(p, ast.If) and node in list(ast.walk(p))]
            ok = False
            for guard in guards:
                test = guard.test
                if (isinstance(test, ast.Compare)
                        and isinstance(test.left, ast.Name)
                        and test.left.id == "organization_charge_id"
                        and len(test.ops) == 1 and isinstance(test.ops[0], ast.IsNot)
                        and len(test.comparators) == 1
                        and isinstance(test.comparators[0], ast.Constant)
                        and test.comparators[0].value is None):
                    ok = True
            assert ok, (
                "server.py:%d org release 的守卫不是 `organization_charge_id is not None` —— "
                "方向反了会让 org charge 永不释放,且拿 None 去 release 非 org 的 run"
                % node.lineno)
            checked += 1
    assert checked == 1, "预期恰好 1 处 org release 调用,数到 %d 处" % checked


def test_identity_branch_uses_the_identity_constant_not_the_refund_one():
    """R-b/B 的文案:识别复核那一支必须用 `IDENTITY_REVIEW_MESSAGE`。

    换成 `NOT_MEASURED_MESSAGE` 时我原来那条锁照样绿(它只验常量**本身**的内容,
    不验调用点用了哪一个)。换错的后果:钱还冻着,却告诉客户"算力已退回"。
    """
    tree = _server_tree()
    consts = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if "needs_manual_review" not in keys:
            continue
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value in ("message", "error"):
                assert isinstance(v, ast.Attribute), ast.dump(v)[:80]
                consts.add(v.attr)
    assert consts, "没定位到 needs_manual_review 终态消息,分母取错了"
    assert consts == {"IDENTITY_REVIEW_MESSAGE"}, (
        "识别复核终态用了别的文案常量:%s —— 钱还冻着,不能说已退回" % sorted(consts))


def test_the_two_user_facing_constants_are_not_interchangeable():
    """配套:两条文案必须**语义可区分**(否则上面那条锁只是在比字符串名字)。"""
    from services.diagnosis_runs import IDENTITY_REVIEW_MESSAGE, NOT_MEASURED_MESSAGE

    assert IDENTITY_REVIEW_MESSAGE != NOT_MEASURED_MESSAGE
    assert "已退回" in NOT_MEASURED_MESSAGE and "已退回" not in IDENTITY_REVIEW_MESSAGE


# ===========================================================================
# A3 / A4 · 拆分快照读取侧:两道自检各配一条行为判据
# ===========================================================================

def test_freeze_row_pools_not_summing_to_total_goes_manual(live_db_048):
    """A3:冻结行自己三池之和 ≠ 总额 → 拆分不可信 → 转人工,不按它动钱。

    (审计实测:丢掉 `sum(pools.values()) != total` 这半个条件,61 条全绿。)
    """
    w = _world(live_db_048, engines=["qwen"], successful_tests=8, frozen=650,
               split={"bonus": 0, "commission": 0, "paid": 650})
    c = _conn(live_db_048)
    # 只动三池、不动 amount_total → 和对不上(列被外部改过的形态)
    c.cursor().execute(
        "UPDATE point_freezes SET amount_paid=%s WHERE id=%s", (600, w["freeze_id"]))
    c.close()
    _settle(w, dict(w["snapshot"], delivery_verdict=_verdict("degraded", 0.25)))
    a = _after(live_db_048, w)
    assert a["run"]["run_status"] == "settlement_manual"
    assert a["freeze"]["status"] == "frozen", "拆分不可信时绝不能动钱"
    assert "reserved_split_sum_mismatch" in (a["run"]["last_settlement_error"] or "")


def test_snapshot_without_order_goes_manual_never_gets_a_default(live_db_048):
    """A4:快照没带 order → 转人工,**不许注入默认顺序**。

    注入默认顺序等于又开始猜"先扣哪个池、余额退回哪个池",
    而整个 A 方案存在的理由就是不猜。
    """
    w = _world(live_db_048, engines=["qwen"], successful_tests=8, frozen=650,
               split={"bonus": 300, "commission": 0, "paid": 350})
    _set_split_snapshot(live_db_048, w["run_token"],
                        {"bonus": 300, "commission": 0, "paid": 350})   # ← 没有 order
    _settle(w, dict(w["snapshot"], delivery_verdict=_verdict("degraded", 0.25)))
    a = _after(live_db_048, w)
    assert a["run"]["run_status"] == "settlement_manual"
    assert a["freeze"]["status"] == "frozen"
    assert "reserved_split_order_invalid" in (a["run"]["last_settlement_error"] or "")


# ===========================================================================
# A5 · 退款快照:四个键必须**真的是 None**,不是"没有等级词"就算过
# ===========================================================================

def test_refund_terminal_snapshot_nulls_all_four_leak_keys(live_db_048):
    """A5:多传一个 `score=30` 就能把泄露面重开,而我原来只查等级词 —— 查不到数字。

    改成逐键断言 `_SUCCESS_LEAK_KEYS` 在耐久终态快照里**都是 None**。
    """
    from services.diagnosis_runs import _SUCCESS_LEAK_KEYS

    w = _world(live_db_048, engines=["qwen"], successful_tests=8, total_score=30, level="危急级")
    c = _conn(live_db_048)
    c.cursor().execute("UPDATE diagnosis_records SET report_v2_modules_jsonb=%s WHERE id=%s",
                       (json.dumps({"client": {"modules": {}}}), w["diagnosis_id"]))
    c.close()
    _settle(w)
    snap = _after(live_db_048, w)["run"]["final_snapshot_jsonb"]
    if isinstance(snap, str):
        snap = json.loads(snap)
    leaked = {k: snap.get(k) for k in _SUCCESS_LEAK_KEYS if snap.get(k) is not None}
    assert not leaked, "退款终态快照漏了成功信息(非 None):%s" % leaked


def test_not_measured_snapshot_nulls_all_four_leak_keys_too():
    """同款配对:not_measured 那一支也逐键验(两支共用一个覆盖清单,别只验一支)。"""
    from services.diagnosis_runs import (
        _SUCCESS_LEAK_KEYS, _not_measured_snapshot, _refunded_snapshot,
    )

    for snap in (_refunded_snapshot("x"), _not_measured_snapshot()):
        for key in _SUCCESS_LEAK_KEYS:
            assert key in snap and snap[key] is None, (key, snap.get(key))


# ===========================================================================
# A8 · 识别谓词的严格性:只认显式 True
# ===========================================================================

#: `_identity_suspected` 会依次看四条容器路径,而**同一个 `is True` 判断在源码里写了两遍**
#: (一遍在 `container["ai_visibility"]["identity_suspicion"]`,一遍在 `container["identity_suspicion"]`)。
#: 只驱动其中一条,另一条改坏了没人会红 —— 审计那发 A8 就是打在我没驱动的那一条上活下来的。
#: 所以这里把**每条路径 × 每种伪真值**做成笛卡尔积,两处写法都被逐个考到。
IDENTITY_CONTAINER_SHAPES = [
    lambda v: {"identity_suspicion": v},                              # 顶层
    lambda v: {"ai_visibility": {"identity_suspicion": v}},           # 顶层 → ai_visibility
    lambda v: {"data": {"identity_suspicion": v}},                    # data
    lambda v: {"data": {"ai_visibility": {"identity_suspicion": v}}},  # data → ai_visibility
]

TRUTHY_BUT_NOT_TRUE = [{}, {"suspected": None}, {"suspected": "yes"}, {"suspected": 1}]


@pytest.mark.parametrize("shape_idx", range(len(IDENTITY_CONTAINER_SHAPES)))
@pytest.mark.parametrize("suspicion", TRUTHY_BUT_NOT_TRUE)
def test_identity_predicate_only_accepts_an_explicit_true(shape_idx, suspicion):
    """A8:`is True` 放宽成 `is not False`,正常单会被大批误判进人工队列。

    我上一版只测了 `{"suspected": False}` —— 那一档在两种写法下结果相同,**零区分力**;
    而且只走了顶层一条路径,漏掉了源码里另一处同款判断。
    """
    from services.diagnosis_runs import _identity_suspected

    snapshot = IDENTITY_CONTAINER_SHAPES[shape_idx](suspicion)
    assert _identity_suspected(snapshot) is False, (shape_idx, suspicion)


@pytest.mark.parametrize("shape_idx", range(len(IDENTITY_CONTAINER_SHAPES)))
def test_identity_predicate_still_fires_on_a_real_true_on_every_path(shape_idx):
    """配对的必须命中:**每条**路径上的显式 True 都要照常开火(别把锁收得杀了真阳性)。"""
    from services.diagnosis_runs import _identity_suspected

    snapshot = IDENTITY_CONTAINER_SHAPES[shape_idx]({"suspected": True})
    assert _identity_suspected(snapshot) is True, shape_idx


def test_the_duplicated_identity_predicate_is_covered_on_both_write_sites():
    """分母自证:源码里那个 `suspected` 判断到底写了几处,判据就得覆盖几处。

    写死"恰好 2 处"是有意的:哪天有人加第三处(或合并成一处),这条会红,
    提醒把上面的路径矩阵一起改 —— 免得又出现"同一谓词写两处,有一处没人验"。
    """
    import inspect
    import re as _re

    from services import diagnosis_runs

    src = inspect.getsource(diagnosis_runs._identity_suspected)
    sites = _re.findall(r'suspicion\.get\("suspected"\) is True', src)
    assert len(sites) == 2, (
        "_identity_suspected 里 `is True` 判断变成了 %d 处 —— "
        "路径矩阵要跟着改,否则又会有一处没人验" % len(sites))


# ===========================================================================
# A9 / A10 · 合同侧:覆盖率下限与取数优先级
# ===========================================================================

def test_coverage_is_not_floored_at_any_minimum():
    """A9:把 `max(0.0, ...)` 抬成 `max(0.25, ...)` 就少退钱,而我原来只测了 0.25/0.75 两档。

    取一个**低于任何可能下限**的比例(1/32)来钉死"不设地板"。
    """
    from services.diagnosis_sample_contract import OUTCOME_DEGRADED, evaluate_sample

    v = evaluate_sample({
        "engines_tested": ["qwen", "deepseek", "kimi", "doubao"],
        "total_planned": 32, "total_tests": 1, "total_failed": 31,
    })
    assert v.outcome == OUTCOME_DEGRADED
    assert abs(v.billable_ratio - 1 / 32) < 1e-9, (
        "覆盖率被地板托住了(%s)—— 少退给客户的那部分就是多收" % v.billable_ratio)


def test_top_level_counters_win_over_a_stale_nested_summary():
    """A10:顶层与 summary **都在且不一致**时,必须以顶层为准。

    我原来两种形状分开测,永远测不出优先级 —— 把优先级对调,两组判据都绿。
    这里造一个两者打架的 payload,让顺序本身成为唯一变量。
    """
    from services.diagnosis_sample_contract import OUTCOME_INSUFFICIENT, evaluate_sample

    v = evaluate_sample({
        "engines_tested": ["qwen"],
        "total_planned": 32, "total_tests": 0, "total_failed": 32,   # 顶层:全失败
        "summary": {"total_planned": 32, "total_tests": 32, "total_failed": 0},  # 嵌套:全成功
    })
    assert v.outcome == OUTCOME_INSUFFICIENT, (
        "取数优先级反了:读到了过期的 summary 而不是生产真发的顶层计数")
    assert v.billable_ratio == 0.0
