"""服务商降级：拒绝话术只列非零项、顺序陷阱、自助降级不得绕过治理。

工单 WO_PROVIDER_DOWNGRADE_UNUSABLE_2026-08-06 §3/§4。
本文件不连数据库：判据全部打在「话术构造」与「源码接线」两层上。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from services.admin_user_governance import (
    _DOWNGRADE_BLOCKER_SPECS,
    _provider_downgrade_blocker_items,
)


ROOT = Path(__file__).resolve().parents[2]
GOVERNANCE_SRC = (ROOT / "services" / "admin_user_governance.py").read_text(encoding="utf-8")
PARTNER_SRC = (ROOT / "api" / "partner_api.py").read_text(encoding="utf-8")
API_SRC = (ROOT / "api" / "admin_user_governance_api.py").read_text(encoding="utf-8")

ALL_KEYS = [spec[0] for spec in _DOWNGRADE_BLOCKER_SPECS]


def _all_zero() -> dict:
    return {key: 0 for key in ALL_KEYS}


# ---------------------------------------------------------------- 只列非零项

def test_all_zero_counters_list_nothing():
    """必须不命中：十二项全零时不许再吐出一串 0。"""
    assert _provider_downgrade_blocker_items(_all_zero()) == []


def test_single_nonzero_counter_is_the_only_thing_listed():
    """必须命中：真实生产形态 —— 十二项里只有渠道关系非零。"""
    counters = _all_zero()
    counters["active_channel_relations"] = 1
    items = _provider_downgrade_blocker_items(counters)
    assert len(items) == 1
    item = items[0]
    assert item["key"] == "active_channel_relations"
    assert item["value"] == 1
    assert item["label"] == "1 条现役渠道关系"
    # 反向对照：零值项的名词一个都不许出现在话术里
    rendered = "、".join(part["label"] for part in items)
    assert "商业绑定客户" not in rendered
    assert "待支付" not in rendered
    assert "退款" not in rendered
    assert not re.search(r"\b0 ", rendered)


def test_every_listed_item_carries_a_handling_path():
    """必须命中：每一项都要说「去哪里处理」（feedback_hint_must_help_or_hide）。"""
    counters = {key: 1 for key in ALL_KEYS}
    items = _provider_downgrade_blocker_items(counters)
    assert len(items) == len(ALL_KEYS)
    for item in items:
        assert item["handling"].strip(), f"{item['key']} 没有处理路径"
        assert len(item["handling"]) >= 8, f"{item['key']} 的处理路径太空洞"
        assert item["category"], f"{item['key']} 没有归类"
        assert item["unit"] in {"rows", "points", "cents"}
        assert "entry" in item


def test_channel_relation_handling_states_the_ordering_trap():
    """必须命中：顺序陷阱要被写进那一项自己的处理路径里，而不是散落在别处。"""
    counters = _all_zero()
    counters["active_channel_relations"] = 2
    handling = _provider_downgrade_blocker_items(counters)[0]["handling"]
    assert "降级" in handling
    assert "之前" in handling or "先" in handling


def test_channel_relation_item_points_at_an_actionable_entry():
    counters = _all_zero()
    counters["active_channel_relations"] = 1
    assert _provider_downgrade_blocker_items(counters)[0]["entry"] == "channel_relationship"
    counters = _all_zero()
    counters["bound_customers"] = 3
    assert _provider_downgrade_blocker_items(counters)[0]["entry"] == "commercial_binding"


# ------------------------------------------------- 拒绝话术与守卫的源码接线

def _identity_downgrade_slice() -> str:
    start = GOVERNANCE_SRC.index("def change_business_identity(")
    end = GOVERNANCE_SRC.index("def change_commercial_binding(")
    return GOVERNANCE_SRC[start:end]


def test_refusal_no_longer_hardcodes_the_twelve_counter_sentence():
    """必须不命中：老的「12 个计数器一股脑」f-string 必须彻底消失。"""
    body = _identity_downgrade_slice()
    for dead in (
        "{bound_customers} 个商业绑定客户",
        "{active_channel_relations} 条现役渠道关系",
        "{inventory_points} 点库存",
        "{resale_blockers} 项逐级库存",
    ):
        assert dead not in body, f"老话术残留：{dead}"


def test_refusal_is_built_from_the_nonzero_item_list():
    body = _identity_downgrade_slice()
    assert "_provider_downgrade_blocker_items(" in body
    assert 'item["label"] for item in items' in body
    # fail-closed：闸响了但一项也列不出来时，仍然必须拒绝
    assert "该服务商仍有未结清的经营依赖" in body


def test_downgrade_gate_expression_is_not_weakened():
    """🔴 必须不命中：改话术不许顺手把闸拆了。十三个判据项一个都不能少。"""
    body = _identity_downgrade_slice()
    gate = body[body.index("blockers = ("):body.index("if blockers:")]
    for term in (
        "is_platform_direct", "bound_customers", "active_channel_relations",
        "inventory_points", "pending_purchase_orders", "pending_customer_orders",
        "pending_disputes", "held_escrows", "unsettled_revenue_cents",
        "unsettled_channel_revenue", "pending_settlements", "pending_withdrawals",
        "resale_blockers",
    ):
        assert term in gate, f"降级守卫少了判据项：{term}"
    assert gate.count(" or ") == 12, "守卫的 or 项数变了，闸被改动过"


def test_validation_error_carries_structured_details_to_http():
    assert "self.details" in GOVERNANCE_SRC
    assert 'detail.update(getattr(exc, "details", None) or {})' in API_SRC


# --------------------------------------------- 自助降级不得绕过治理原语

# 🔴 这一组判据只能打在 AST 上：函数的 docstring 里逐字引用了那句被废掉的裸 SQL
# （为了讲清楚它坏在哪），按文本扫会抓到我自己写的说明文字，恒红。
_RAW_LEVEL_WRITE = re.compile(r"UPDATE\s+user_wallets\s+SET\s+agent_level", re.IGNORECASE)


def _self_downgrade_node() -> ast.AST:
    tree = ast.parse(PARTNER_SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "downgrade_self":
            return node
    raise AssertionError("downgrade_self 端点不见了")


def _executable_strings(node: ast.AST) -> list[str]:
    """函数体内真正参与执行的字符串字面量（剔除 docstring）。"""
    doc = ast.get_docstring(node, clean=False)
    values = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if doc is not None and sub.value == doc:
                continue
            values.append(sub.value)
    return values


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_self_downgrade_no_longer_writes_agent_level_directly():
    """🔴 必须不命中：裸 UPDATE 会造出「agent_level=0 但仍挂现役渠道关系」的死态，
    之后 change_channel_relationship 永久拒绝，关系再也终结不掉。"""
    for literal in _executable_strings(_self_downgrade_node()):
        assert not _RAW_LEVEL_WRITE.search(literal), f"自助降级又在裸写 agent_level：{literal!r}"


def test_raw_level_write_detector_actually_fires():
    """反向对照：证明上一条不是恒绿。把废掉的老实现塞回去，判据必须报红。"""
    revived = ast.parse(
        'async def downgrade_self():\n'
        '    """说明文字里提到 UPDATE user_wallets SET agent_level 也不该算命中。"""\n'
        '    cursor.execute("UPDATE user_wallets SET agent_level = 0 WHERE user_id = %s", (uid,))\n'
    ).body[0]
    hits = [s for s in _executable_strings(revived) if _RAW_LEVEL_WRITE.search(s)]
    assert len(hits) == 1, f"探测器失效：应抓到 1 处，实际 {len(hits)} 处"
    # 同时证明它不会把 docstring 里的引用误报成命中
    assert "说明文字" not in "".join(hits)


def test_self_downgrade_goes_through_the_governance_primitive():
    node = _self_downgrade_node()
    called = _called_names(node)
    assert "change_business_identity" in called, "自助降级没有走治理原语"
    literals = _executable_strings(node)
    assert "ordinary_user" in literals
    keywords = {
        kw.arg
        for sub in ast.walk(node) if isinstance(sub, ast.Call)
        for kw in sub.keywords if kw.arg
    }
    assert "expected_version" in keywords, "自助降级没有传乐观锁版本"
    assert "request_id" in keywords and "reason" in keywords, "自助降级没有留审计凭据"
    # 依赖闸的拒绝要原样透给调用方，含结构化明细
    handlers = [h for h in ast.walk(node) if isinstance(h, ast.ExceptHandler)]
    caught = {
        name.id
        for h in handlers for name in ast.walk(h.type) if isinstance(name, ast.Name)
    } if handlers else set()
    assert "GovernanceValidationError" in caught, "依赖闸拒绝没有被接住"
