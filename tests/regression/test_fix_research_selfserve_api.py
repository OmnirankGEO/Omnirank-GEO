"""[GEO-R2-CAN-038] 判别性回归锁 —— api/research_selfserve_api.py。

修复:自助调研的 brand RBAC 从本地 owner-only helper 改走共享策略
auth.brand_access.require_brand_access(接受分配客户 client_brand_ids + 自有 owner)。

主形态 = source-inspection 判别锁:读源码文本断言修复标志存在。回退修复则断言失败。
不依赖 DB / 不 import server.py。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "api" / "research_selfserve_api.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def _region(anchor: str, span: int = 1200) -> str:
    idx = SRC.find(anchor)
    assert idx != -1, f"锚点未找到: {anchor}"
    return SRC[idx: idx + span]


def test_imports_shared_brand_access_policy():
    """[GEO-R2-CAN-038] 顶部 import 共享 RBAC 策略,而非只依赖本地 owner-only 查询。"""
    assert "from auth.brand_access import require_brand_access" in SRC


def test_verify_brand_owner_delegates_to_shared_policy():
    """[GEO-R2-CAN-038] _verify_brand_owner 委托给 require_brand_access(接受分配客户)。

    回退到旧的 owner-only 实现(直接比对 brands.owner_user_id)则断言失败。
    """
    region = _region("def _verify_brand_owner(", span=1400)
    assert "require_brand_access(" in region, "未走共享策略 require_brand_access"
    # 旧的 owner-only 实现特征:helper 内直接 SELECT owner_user_id 并逐一比对 —— 不应再存在
    assert "SELECT owner_user_id FROM brands" not in region, "仍保留旧 owner-only SQL 比对"


def test_verify_brand_owner_signature_takes_request():
    """[GEO-R2-CAN-038] helper 新签名接受 Request(才能读 request.state.user.client_brand_ids)。"""
    assert re.search(r"def _verify_brand_owner\(request: Request,", SRC), "helper 未接收 Request"


def test_self_serve_calls_shared_brand_access():
    """[GEO-R2-CAN-038] 付费 /self-serve handler 用新签名(request, brand_id)校验归属。"""
    region = _region("async def self_serve(", span=1400)
    assert "_verify_brand_owner(request, req.brand_id)" in region


def test_draft_calls_shared_brand_access():
    """[GEO-R2-CAN-038] 免费 /draft handler 用新签名(request, brand_id)校验归属。"""
    region = _region("async def draft(", span=1400)
    assert "_verify_brand_owner(request, req.brand_id)" in region


def test_no_stale_owner_only_call_signature():
    """[GEO-R2-CAN-038] 不残留旧三参调用(brand_id, user_id, is_admin)。"""
    assert "agent[\"is_admin\"])" not in SRC or "_verify_brand_owner(req.brand_id" not in SRC
    assert "_verify_brand_owner(req.brand_id, user_id" not in SRC
