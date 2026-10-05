"""结算 AI 审查员 · 契约/结构判据(不需要真跑一单的那些)。

三类:
  ① **零新资金口** census —— 机械枚举模块引用的全部标识符,不是我手写"我没调"。
  ② **接线探针** —— 直接证明 `run_diagnosis_sweep` 在运行时真的调到了审查员。
  ③ **规则矩阵** —— 纯函数 `adjudicate()` 的笛卡尔积,钉死优先级。
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import io
import pathlib

import pytest

import services.settlement_adjudicator as adj

ROOT = pathlib.Path(__file__).resolve().parents[2]


# ===========================================================================
# ① 零新资金口(census · 机械枚举)
# ===========================================================================

#: 全部会**直接改余额/冻结**的原语。审查员一个都不许碰 —— 它只出 decision,
#: 钱由既有状态机动。手写这份名单是有风险的(漏一项不会有任何判据变红),
#: 所以下面配了一条"名单本身必须都真实存在"的自检:名字打错 = 白名单形同虚设。
MONEY_PRIMITIVES = {
    "freeze_points", "commit_freeze", "release_freeze", "_do_settlement",
    "_partial_commit_points", "settle_charge", "release_charge", "reserve_charge",
    "claim_live_charge", "refund_credit", "refund_points", "commit_run",
    "dispatch_success_settlement", "deduct_points", "complete_recharge",
    "grant_trial_bonus",
}


def _identifiers(path: pathlib.Path) -> set[str]:
    """模块里出现过的全部标识符(Name / Attribute 属性名 / import 的名字)。

    不用 grep:grep 会被注释和 docstring 骗(本文件的 docstring 里就写着那些原语名)。
    走 AST 只认代码。
    """
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                out.add((a.asname or a.name).split(".")[0])
    return out


def test_adjudicator_module_touches_zero_money_primitives():
    """审查员模块**不引用任何资金原语**。零新资金口是结构性的,不是纪律性的。"""
    used = _identifiers(ROOT / "services" / "settlement_adjudicator.py")
    hit = sorted(used & MONEY_PRIMITIVES)
    assert hit == [], "审查员直接引用了资金原语 %r —— 它只该出 decision,钱归既有状态机" % (hit,)


def test_the_money_primitive_blacklist_is_not_a_list_of_typos():
    """黑名单自检:名单里每一项都必须**真的存在**于资金侧模块。

    名字打错的黑名单是零区分力的 —— 上面那条会永远绿。
    """
    import importlib
    # 资金侧模块的**全集**。少写一个模块 ⇒ 真实存在的名字被误判成 ghost,
    # 这条会红 —— 宁可红,不可让黑名单里躺着一个永远命中不了的错名字。
    known = set()
    for m in ("services.diagnosis_runs", "services.organization_billing",
              "middleware.billing", "db.wallet_db", "services.customer_credit"):
        known |= set(dir(importlib.import_module(m)))
    ghosts = sorted(n for n in MONEY_PRIMITIVES if n not in known)
    assert ghosts == [], "黑名单里这些名字在资金侧根本不存在(打错了 ⇒ 白名单形同虚设):%r" % (ghosts,)


def test_adjudicator_goes_through_the_same_gate_a_human_admin_uses():
    """配对的必须命中:审查员**必须**引用 `verify_and_resolve_manual`。

    上面那条只说"没碰资金原语" —— 一个什么都不做的空模块同样能全绿。
    这条钉住它确实走了 admin 那把闸门。
    """
    used = _identifiers(ROOT / "services" / "settlement_adjudicator.py")
    assert "verify_and_resolve_manual" in used, \
        "审查员没走 admin 处置那把 CAS —— 要么没接线,要么自己另开了资金路径"


# ===========================================================================
# ② 接线探针(证明 cron 面真的调到了审查员)
# ===========================================================================

def test_the_cron_face_really_calls_the_adjudicator(live_server, live_dsn, monkeypatch):
    """接线锁:`run_diagnosis_sweep()`(既有 cron job 的调用面)运行时**真的**调到审查员。

    新增「要被调度调的函数」必须同 commit 写接线锁(本仓老教训:
    函数写好了、判据全绿、但从来没有人调它)。这条不看源码里有没有那一行 ——
    源码有那行而符号绑不上时(NameError)它是绿的,而运行时探针是红的。
    """
    called = []
    import services.settlement_adjudicator as mod

    def _probe():
        called.append(1)
        return {"scanned": 0, "auto_commit": 0, "auto_release": 0, "escalated": 0,
                "escalate_skipped_dup": 0, "execution_failed": 0,
                "skipped_no_config": 1, "errors": 0}

    monkeypatch.setattr(mod, "run_adjudication_tick", _probe)
    from services.diagnosis_runs import run_diagnosis_sweep
    asyncio.run(run_diagnosis_sweep())

    assert called == [1], "cron 面一次都没调到审查员 —— 接线断了(函数存在≠被调用)"


def test_adjudicator_failure_does_not_take_down_the_sweeper(live_server, live_dsn, monkeypatch):
    """配对的必须不命中:审查员炸掉**不许**带走 sweeper 前四段(判死/收尸/结算/告警)。"""
    import services.settlement_adjudicator as mod

    def _boom():
        raise RuntimeError("injected adjudicator explosion")

    monkeypatch.setattr(mod, "run_adjudication_tick", _boom)
    from services.diagnosis_runs import run_diagnosis_sweep
    stats = asyncio.run(run_diagnosis_sweep())   # 不许抛

    assert isinstance(stats, dict) and "reaped_running" in stats, \
        "审查员炸掉把整个 sweeper 带走了:%r" % (stats,)


# ===========================================================================
# ③ 规则矩阵(纯函数 · 笛卡尔积 · 钉死优先级)
# ===========================================================================

COEFFS = {"max_frozen_points": 1000, "same_cause_limit": 3}


def _ev(*, product=True, frozen=500, missing=None, freeze_status="frozen"):
    return {
        "run_token": "rt", "frozen_points": frozen, "failure_cause": "c",
        "missing": list(missing or []),
        "product_present": product,
        "product": {"diagnosis_id": 7} if product else None,
        "freeze_row": None if freeze_status is None else {"id": 1, "status": freeze_status},
    }


@pytest.mark.parametrize("product,expect", [(True, "commit"), (False, "release")])
def test_clean_evidence_within_limit_decides_by_product(product, expect):
    out = adj.adjudicate(_ev(product=product), COEFFS, same_cause_streak=1)
    assert out["decision"] == expect, out
    assert out["escalation_code"] is None, out


@pytest.mark.parametrize("product", [True, False])
def test_over_limit_escalates_in_both_directions(product):
    """限额门控**两向都门** —— 判交付和判全退都要过同一道限额。"""
    out = adj.adjudicate(_ev(product=product, frozen=5000), COEFFS, same_cause_streak=1)
    assert out["decision"] == "escalate", out
    assert out["escalation_code"] == adj.ESCALATE_OVER_LIMIT, out


@pytest.mark.parametrize("product", [True, False])
@pytest.mark.parametrize("frozen", [500, 5000])
def test_same_cause_streak_outranks_everything(product, frozen):
    """同因≥N **优先级最高**:证据再齐、金额再小,也停手报 bug。

    这条优先级不是随便排的 —— 系统性故障期间"每单都很干净"恰恰是最危险的样子:
    自动处置会把一整批钱按同一个错误逻辑放掉。
    """
    out = adj.adjudicate(_ev(product=product, frozen=frozen), COEFFS, same_cause_streak=3)
    assert out["escalation_code"] == adj.ESCALATE_SAME_CAUSE_STREAK, out


@pytest.mark.parametrize("kw", [
    {"missing": ["freeze_handle_incomplete"]},
    {"freeze_status": None},
    {"freeze_status": "consumed"},
    {"frozen": 0},
])
def test_incomplete_or_contradictory_evidence_never_moves_money(kw):
    """证据不全 / 自相矛盾 ⇒ 一律 escalate。**不猜**。"""
    out = adj.adjudicate(_ev(**kw), COEFFS, same_cause_streak=1)
    assert out["decision"] == "escalate", (kw, out)
    assert out["escalation_code"] == adj.ESCALATE_EVIDENCE_INCOMPLETE, (kw, out)


def test_adjudicate_is_pure_no_llm_no_io():
    """裁定核心必须是**机械规则**:纯函数,零 IO、零 LLM(WO 红线)。

    源码级枚举 `adjudicate` 里的调用:不许出现任何取数/取模型的名字。
    """
    src = inspect.getsource(adj.adjudicate)
    tree = ast.parse(src.lstrip())
    # 🔴 不能把 `get` / `post` 放进黑名单:`evidence.get(...)` 会命中 —— 那是**判据自己的
    #    假阳性**,不是被测代码的问题。黑名单只列取数/取模型的具体名字。
    banned = {"get_db", "connect", "execute", "fetchone", "fetchall", "cursor",
              "requests", "urlopen", "openai", "chat", "llm", "completion",
              "dashscope", "invoke", "generate"}
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            calls.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    hit = sorted(calls & banned)
    assert hit == [], "裁定核心里出现了 IO/LLM 调用 %r —— 金额必须可复算可审计" % (hit,)


# ===========================================================================
# 纪律:六保护文件零 diff
# ===========================================================================

#: Review 2026-08-25 定案的官方六保护清单。
#: 🔴 权威可执行分母是 `tests/defensive_geo_w3_2026_08_21/test_wp6_structural_anchors.py`
#:    的 PROTECTED 元组,但那份文件长在 feat/defgeo-* 系分支上,**不在本包的底里**,
#:    import 不到 —— 所以这里复制一份,并钉死"必须恰 6 项"(少一项即红)。
#:    两边合并时以那份为准。
PROTECTED = (
    "middleware/billing.py",
    "db/wallet_db.py",
    "db/connection.py",
    "auth/middleware.py",
    "auth/jwt_utils.py",
    "config/settings_manager.py",
)


def test_protected_files_have_zero_diff():
    import subprocess
    assert len(PROTECTED) == 6, "六保护清单被改动过:%r" % (PROTECTED,)
    # [合流 2026-08-25 Review-CTO · 按本文件上方预裁「两边合并时以那份为准」执行]
    # config/settings_manager.py 在合并树上带着包F ⑦ 的 Owner 逐行白名单两行
    # (相对 dfaafd40b 非零 diff,但每一行都过了批)。它的守卫权移交权威分母
    # tests/defensive_geo_w3_2026_08_21/test_wp6_structural_anchors.py::
    # test_protected_files_have_zero_diff(逐行白名单 + 判别力自证,合并树上在跑)。
    # 本条只守其余五件的「相对本包底零 diff」—— 那五件任何包都不许动,语义不变。
    others = tuple(p for p in PROTECTED if p != "config/settings_manager.py")
    out = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--name-only", "dfaafd40b", "--", *others],
        capture_output=True, text=True, check=False).stdout.strip()
    # 🔴 [2026-09-05] **收窄,不是放宽**:本轮 `middleware/billing.py` 经授权改动。
    #    Owner 2026-09-05 亲口授权(「billing.py 这笔我批了」)· OWNER_AUTHORIZATIONS_2026-09-05.md sha256 b673b188ff01e377
    #    其余保护文件任一被改 ⇒ 仍然红;billing.py 改回不动 ⇒ **也红**。
    _changed = tuple(x for x in out.split() if x.strip())
    # 🔴 [#106b · 2026-09-06] 授权集从 1 个变 2 个 —— **收窄不是放宽**:
    #    多出 db/wallet_db.py 是因为 Owner 在 C 窗口又批了一笔;
    #    任何**第三个**保护文件被改仍然红,而这两个之一改回不动 **也红**
    #    (授权是一笔一授,用完要显式收回,不是永久解锁)。
    #    顺序按 git 的输出(字典序),不是按批准先后。
    assert _changed == ("db/wallet_db.py", "middleware/billing.py"), (
        "保护文件改动集 %r != 已授权集 · Owner 2026-09-05 亲口授权(「billing.py 这笔我批了」)· OWNER_AUTHORIZATIONS_2026-09-05.md sha256 b673b188ff01e377" % (_changed,))


def test_the_protected_diff_probe_can_actually_see_changes():
    """配对的必须命中:同一把尺去量**本包真的改过**的文件,必须量得出来。

    否则上面那条可能只是因为 git 命令写错了而恒绿。
    """
    import subprocess
    out = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--name-only", "dfaafd40b", "--",
         "services/diagnosis_runs.py"],
        capture_output=True, text=True, check=False).stdout.strip()
    assert "services/diagnosis_runs.py" in out, "尺子是坏的:量不出已知的改动"


# ===========================================================================
# 限额边界(WO §2 口径:单笔 ≤ 系数 → 自动;> 系数 → 升级)
# ===========================================================================

@pytest.mark.parametrize("frozen,expect_auto", [
    (999, True),    # 低于限额
    (1000, True),   # **恰好等于限额** —— WO 说「≤ 系数」放行
    (1001, False),  # 刚过线
])
def test_limit_gate_boundary_is_inclusive(frozen, expect_auto):
    """边界锁:`<= max` 放行 / `> max` 升级,**两侧各一个样本**。

    补这条的由来:交付书里我本来只把它记成"已知未覆盖" —— 但限额是 Owner 后台可调的
    系数,踩线单迟早出现,而 `>` 与 `>=` 互换在没有边界样本时是**静默存活**的:
    离边界很远的夹具(650 vs 100 / 650 vs 10000)对它零区分力。
    """
    out = adj.adjudicate(_ev(frozen=frozen), COEFFS, same_cause_streak=1)
    if expect_auto:
        assert out["decision"] == "commit", (frozen, out)
        assert out["escalation_code"] is None, (frozen, out)
    else:
        assert out["escalation_code"] == adj.ESCALATE_OVER_LIMIT, (frozen, out)
