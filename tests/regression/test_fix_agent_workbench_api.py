"""
Regression lock for GEO-R2-CAN-017 (offline-transfer-missing-idempotency).

线下划拨 / 撤回 endpoint 缺少幂等键 → 重复/双击提交会双侧翻倍账目。
修复:api/agent_workbench_api.py 为 allocate-offline / revoke-offline 引入幂等键去重
(调用方 Idempotency-Key 头优先 · 缺失走 10s 服务端去重窗口),命中重复 → 重放余额不再二次入账。

主形态 = source-inspection 判别锁:读源码断言修复标志存在,回退修复则断言失败。
不依赖 DB / 不 import server.py。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "api" / "agent_workbench_api.py").read_text(encoding="utf-8")


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


def test_idempotency_helper_exists():
    # 幂等键派生 helper 必须存在,且同时支持调用方头 + 服务端时间桶两条路径
    assert "_compute_offline_idem_ref" in SRC
    assert "Idempotency-Key" in SRC
    assert "X-Idempotency-Key" in SRC
    # 服务端去重窗口(时间桶)兜底 · 无键场景
    assert "int(time.time())" in SRC
    assert "hashlib.sha1" in SRC


def test_allocate_offline_has_dedupe_and_no_random_ref():
    """allocate-offline:ref 必须来自幂等键(不再每次随机 uuid),且落库前做重复检查+重放。"""
    handler = _slice(SRC, "async def agent_allocate_offline", "async def agent_revoke_offline")
    # ref 由幂等 helper 派生
    assert "_compute_offline_idem_ref(request" in handler
    assert "kind='alloc'" in handler or 'kind="alloc"' in handler
    # 旧的"每调用随机 offline_ref"必须被移除(回退则此断言失败)
    assert "uuid.uuid4().hex[:16]" not in handler
    # 落库前查已有 allocate 流水(same related_order_id)→ 幂等短路
    assert "customer_credit_transactions" in handler
    assert "related_order_id = %s" in handler
    assert "type = 'allocate'" in handler
    # 命中重复 → 重放当前余额(不二次扣库存/入账)
    assert "get_or_create_customer_wallet" in handler
    assert "get_or_create_inventory_wallet" in handler
    # 幂等短路必须在 allocate_offline 扣库存之前(顺序判别)
    assert handler.index("related_order_id = %s") < handler.index("agent_after = allocate_offline(")


def test_revoke_offline_has_dedupe_preserving_cap_semantics():
    """revoke-offline:加幂等去重,但 related_order_id 仍保持 None(不改 W2 总余额 cap 语义)。"""
    handler = _slice(SRC, "async def agent_revoke_offline", "# ============================================================\n# 7-8")
    assert "_compute_offline_idem_ref(request" in handler
    assert "kind='revoke'" in handler or 'kind="revoke"' in handler
    assert "idem_marker" in handler
    # 重复检查:按 description 内嵌的 [idem:<ref>] 标记去重
    assert "customer_credit_transactions" in handler
    assert "type = 'revoke'" in handler
    assert "description LIKE %s" in handler
    # cap 语义不能被破坏:revoke_credit 仍传 related_order_id=None
    assert "related_order_id=None" in handler
    # 命中重复 → 重放余额
    assert "get_or_create_customer_wallet" in handler
    assert "get_or_create_inventory_wallet" in handler


def test_idem_ref_behavior_pure_function():
    """纯函数真行为单测:相同键/相同请求同桶 → 同 ref;不同键 → 不同 ref。"""
    import importlib
    mod = importlib.import_module("api.agent_workbench_api")

    class _FakeReq:
        def __init__(self, headers):
            self.headers = headers

    class _Payload:
        customer_user_id = 7
        tool_points = 100
        publish_points = 0
        bonus_points = 0
        description = "x"
        reason = "x"

    p = _Payload()
    r1 = _FakeReq({"Idempotency-Key": "abc-123"})
    r2 = _FakeReq({"Idempotency-Key": "abc-123"})
    r3 = _FakeReq({"Idempotency-Key": "different"})

    ref1 = mod._compute_offline_idem_ref(r1, 42, p, kind="alloc")
    ref2 = mod._compute_offline_idem_ref(r2, 42, p, kind="alloc")
    ref3 = mod._compute_offline_idem_ref(r3, 42, p, kind="alloc")

    assert ref1 == ref2, "same idempotency key must yield same ref (retry dedupe)"
    assert ref1 != ref3, "different keys must yield different refs"
    assert re.fullmatch(r"[0-9a-f]{20}", ref1), "ref must be 20-hex"
    # kind 隔离:同键 alloc vs revoke 不冲突
    assert mod._compute_offline_idem_ref(r1, 42, p, kind="alloc") != \
        mod._compute_offline_idem_ref(r1, 42, p, kind="revoke")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
