"""【微单 2026-08-20】`prefill.primary_action.enabled` 必须跟 `contract.executable` 走。

## 修的是什么

在这之前 `enabled` 只看协调态,对 `executable=False` 的 operation
(`writing_center` / `geo_content_center`)照样返回
`enabled=True` + 「确认并执行 · 使用 X 算力」——
而 `POST …/execute` 对它们一律 409 `DOMAIN_ADAPTER_NOT_AVAILABLE`。

**那是一颗死按钮**:界面承诺了一件我们做不到的事。

## 判据形状

工单给的等式是 `enabled == contract.executable`,但它只在**协调态就绪**
(prepared / awaiting_confirmation)时才是等式 —— 终态给的是「重新准备」、
已执行给的是「查看进度」,那两个不是这颗按钮。所以下面**先钉住等式成立的那个窗口**,
再分别钉住窗口之外的行为,免得把一条有前提的等式当成全称命题。
"""
from __future__ import annotations

import pytest

from services import gap_operation_map as omap
from services.xiaobang_page_prefill import build_prefill, primary_action

READY_STATES = ("prepared", "awaiting_confirmation")
QUOTED = {"state": "quoted", "amount": 390, "unit": "算力"}


def _entry(operation_id: str):
    entry = omap.resolve_operation(operation_id)
    assert entry is not None and entry.command_contract is not None, operation_id
    return entry


def _row(state: str = "prepared", **extra):
    row = {
        "intent_id": "xint_microcheck01", "intent_state": state, "intent_revision": 1,
        "side_effect": "external", "compute_quote_amount": 390,
        "compute_quote_unit": "算力", "preview": {}, "reason_facts": [],
    }
    row.update(extra)
    return row


def _projection(state: str = "prepared"):
    return {
        "intent_state": state, "intent_revision": 1,
        "domain_projection": {"state": "not_started", "reference": None},
        "settlement_projection": {"state": "none", "amount": 390, "unit": "算力"},
        "external_projection": {"state": "not_started"},
    }


# ══════════════════════════════════════════════════════════════════════════
# 主判据:enabled == contract.executable(在"协调态就绪"这个窗口内)
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("operation_id", sorted(
    e.operation_id for e in omap.commandable_operations()))
@pytest.mark.parametrize("state", READY_STATES)
def test_enabled_equals_contract_executable(operation_id, state):
    """🔴 分母 = 注册表里**全部**有合同的 operation,逐条 × 两个就绪态。

    今天这批里 external 那条 `executable=True`(→ 仍 true,工单要的反向对照),
    另外两条 compute_only `executable=False`(→ 必须 false)。
    分母机械取,所以新登记一条合同时它自动进判据,而不是"新加的那条碰巧没人验"。
    """
    entry = _entry(operation_id)
    payload = build_prefill(_row(state), entry=entry, projection=_projection(state))
    action = payload["primary_action"]
    assert action["enabled"] is bool(entry.command_contract.executable), (
        operation_id, state, action, entry.command_contract.executable)


def test_the_denominator_really_contains_both_sides():
    """🔴 判别力自证:上面那批参数里**两边都有** —— 否则等式是恒真的。

    只有 executable=True 的话,`enabled is executable` 会因为"两边都 True"而恒成立;
    只有 False 同理。两边都在,这条等式才有内容。
    """
    flags = {e.command_contract.executable for e in omap.commandable_operations()}
    assert flags == {True, False}, flags


def test_the_external_tier_is_still_enabled():
    """🔁 工单点名的反向对照:external 档(publish_center)仍然是 true。

    这一条单列而不是靠上面的参数化兜住 —— 它是"别把闸关过头"的那一面:
    把 `enabled` 写死成 False 也能让上面那批里的两条过,但这条会红。
    """
    entry = _entry("publish_center")
    assert entry.command_contract.executable is True, entry.command_contract
    payload = build_prefill(_row("prepared"), entry=entry, projection=_projection())
    assert payload["primary_action"]["enabled"] is True, payload["primary_action"]
    assert payload["primary_action"]["next_action_id"] == "confirm_then_run"
    assert "使用 390 算力" in payload["primary_action"]["label"]


# ══════════════════════════════════════════════════════════════════════════
# 不许死按钮:降级要**诚实**,不是把同一句承诺画灰
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("operation_id", sorted(
    e.operation_id for e in omap.commandable_operations()
    if not e.command_contract.executable))
def test_a_non_executable_operation_never_promises_execution(operation_id):
    """🔴 执行不了的那一档:**不许**出现「确认并执行」,**不许**出现算力数字。

    把按钮画灰但文案照旧写「确认并执行 · 使用 390 算力」,用户仍然会按那个数字
    做决定 —— 死按钮换了个材质而已。
    """
    entry = _entry(operation_id)
    action = build_prefill(_row("prepared"), entry=entry,
                           projection=_projection())["primary_action"]
    assert action["enabled"] is False, action
    assert "确认并执行" not in action["label"], action
    assert "算力" not in action["label"], action
    assert action["next_action_id"] != "confirm_then_run", action
    # 诚实降级:说清楚下一步是真实存在的那一个,并且**不吓人**(内容都在、不会重复)
    assert action.get("note"), action
    assert "不会重复" in action["note"], action


def test_the_gate_does_not_leak_into_the_terminal_and_running_branches():
    """🔁 边界对照:终态 / 已执行那两支**不受**这道闸影响。

    它们给的是「重新准备」「查看进度」——那两件事与领域执行链开没开没关系,
    把它们一起关掉就是矫枉过正(用户连回头路都没了)。
    """
    entry = _entry("writing_center")               # executable=False 的那一档
    assert entry.command_contract.executable is False
    for state, expected_id in (("cancelled", "restart"),
                               ("expired", "restart"),
                               ("execution_linked", "view_progress")):
        action = build_prefill(_row(state), entry=entry,
                               projection=_projection(state))["primary_action"]
        assert action["next_action_id"] == expected_id, (state, action)
        assert action["enabled"] is True, (state, action)


def test_the_default_keeps_the_historical_behaviour():
    """`primary_action` 的 `executable` 默认 True = 既有直接调用点逐字节不变。"""
    action = primary_action(_row("prepared"), needs_approval=False, compute=QUOTED)
    assert action["enabled"] is True and action["next_action_id"] == "confirm_then_run"


# ══════════════════════════════════════════════════════════════════════════
# 顺手验掉:小榜抽屉 CTA 那一格(不可点 或 诚实降级,不许死按钮)
# ══════════════════════════════════════════════════════════════════════════

def test_the_drawer_only_renders_actions_the_server_says_are_usable():
    """🔴 抽屉动作卡:`enabled !== true` 的动作**根本不渲染**(= 不可点)。

    判的是前端那个过滤器的**真实口径**,从源码取,不是"我记得它过滤了"。
    四个条件缺一不可:enabled / registry_version / 现役路由 / 不是协议相对 URL。
    """
    from pathlib import Path

    src = Path("frontend/src/components/xiaobang/XiaobangGapCard.tsx").read_text(
        encoding="utf-8")
    body = src[src.index("export function navigableActions"):]
    body = body[:body.index("export function XiaobangGapCard")]
    for needle in ("a.enabled === true", "a.registry_version",
                   "a.target_route.startsWith('/')", "!a.target_route.startsWith('//')"):
        assert needle in body, (needle, body[:400])
    # 🔁 反向对照:这四条不是写在注释里的 —— 去掉注释后仍然在
    code = "\n".join(line for line in body.splitlines()
                     if not line.strip().startswith("//"))
    assert "a.enabled === true" in code, code[:400]


def test_the_strip_visually_degrades_a_disabled_primary_action():
    """🔴 预填条:`enabled=false` 时**视觉上也要塌下去** + 把 note 说出来。

    服务端已经如实说了,前端还渲染成一行自信的 CTA 文案 = 死按钮换了个材质。
    这里打源码形态(那一格没有真浏览器判据能单独打到的分支就靠它兜),
    真浏览器那一跳在 `frontend/tests/xiaobang-prefill/prefill-pages.spec.ts`。
    """
    from pathlib import Path

    src = Path("frontend/src/components/publishing/XiaobangPrefillStrip.tsx").read_text(
        encoding="utf-8")
    code = "\n".join(line for line in src.splitlines()
                     if not line.strip().startswith("//") and not line.strip().startswith("*"))
    assert "data-enabled=" in code, code[:300]
    assert "aria-disabled=" in code, code[:300]
    assert "xiaobang-prefill-primary-note" in code, code[:300]
    assert "primary_action?.enabled === false" in code, code[:300]
