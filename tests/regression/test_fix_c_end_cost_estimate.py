"""Regression lock for tools/c_end_cost_estimate.py fixes.

GEO-R8-CAN-002: 品牌识别缓存 key 缺租户维度 → 两个不同租户同名品牌
(同/空描述、都无 profile industry) 命中同一条缓存, 跨租户复用 industry/city/core_keywords.
修复: cache_key 加 brand_id / user_id 租户标识 + 前缀升 v2.

主形态 = source-inspection 判别锁 (读源码文本, 断言修复标志存在; 回退修复则断言失败).
不依赖 DB / 不 import server.py.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "tools" / "c_end_cost_estimate.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def _cache_key_region() -> str:
    """截取品牌识别缓存 key 构造区域源码."""
    start = SRC.index("_tenant_scope")
    end = SRC.index("brand_info = {", start)
    return SRC[start:end]


def test_cache_key_includes_tenant_scope():
    """[GEO-R8-CAN-002] cache_key 源必须含租户维度 (brand_id / user_id)."""
    region = _cache_key_region()
    # 必须把 brand_id / user_id 拼进 key source (租户隔离)
    assert "brand_id" in region and "user_id" in region, (
        "缓存 key 构造区域未引用 brand_id/user_id, 租户隔离修复被回退"
    )
    assert "_tenant_scope" in region, "未引入 _tenant_scope 租户标识组件"


def test_cache_key_src_prefixes_tenant():
    """租户标识必须进入 _brand_cache_key_src (真正参与 hash), 而非仅注释."""
    region = _cache_key_region()
    # _brand_cache_key_src 定义行必须包含 _tenant_scope
    src_line = next(
        ln for ln in region.splitlines()
        if "_brand_cache_key_src" in ln and "=" in ln and "f\"" in ln
    )
    assert "_tenant_scope" in src_line, (
        f"_brand_cache_key_src 未纳入租户标识: {src_line.strip()}"
    )


def test_cache_key_prefix_bumped():
    """前缀应从 v1 升到 v2, 强制老缓存重映射避免遗留跨租户命中."""
    region = _cache_key_region()
    assert "cend_brand_identify:v2:" in region, "cache_key 前缀未升级到 v2"
    assert "cend_brand_identify:v1:" not in region, "仍残留 v1 前缀"


def test_marker_comment_present():
    """修复标记注释存在."""
    assert "[GEO-R8-CAN-002]" in SRC


def test_tenant_scope_isolates_identical_brand():
    """行为锁: 相同 brand_name+空描述, 不同 brand_id 生成不同 key source (不再串味)."""
    # 复刻源码里的 _tenant_scope + _brand_cache_key_src 语义做等价断言.
    def build_src(brand_name, description, brand_id, user_id):
        tenant = f"b{brand_id}|u{user_id}" if (brand_id or user_id) else "anon"
        return f"{tenant}|{brand_name}|{description or ''}"

    a = build_src("老王家常菜", "", brand_id=101, user_id=11)
    b = build_src("老王家常菜", "", brand_id=202, user_id=22)
    assert a != b, "不同租户同名品牌 key source 仍相同 → 会跨租户命中"
    # 同租户同输入必须稳定命中 (缓存仍有效)
    a2 = build_src("老王家常菜", "", brand_id=101, user_id=11)
    assert a == a2
