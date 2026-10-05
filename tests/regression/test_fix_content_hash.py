"""
回归锁: services/research_monitor/content_hash.py

覆盖 finding GEO-R10-CAN-004 (long-content-tail-ignored-fingerprint):
之前 compute_content_hash 只 hash 前 1000 字符, 两篇仅开头相同的长文会碰撞成
同一指纹, 后者被误判 is_duplicate 排除出语料库。修复后改为 hash 归一化全文。

主形态: source-inspection 判别锁 (读源码文本断言修复标志) + 纯函数行为单测。
不依赖 DB / 不 import server.py。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TARGET = ROOT / "services" / "research_monitor" / "content_hash.py"


# ---------- source-inspection 判别锁 ----------

def test_source_no_longer_truncates_prefix():
    """回退到 content[:1000] 前缀截断则失败。"""
    src = TARGET.read_text(encoding="utf-8")
    # 不再对 content 做前 1000 字符截断
    assert "content[:1000]" not in src, "仍在截断前 1000 字符, 尾部碰撞未修"
    assert "snippet = content" not in src, "仍在用 snippet 前缀"


def test_source_has_fix_marker():
    """修复标记与全文归一化存在。"""
    src = TARGET.read_text(encoding="utf-8")
    assert "GEO-R10-CAN-004" in src, "缺少修复标记注释"
    # 归一化对象是完整 content 而非截断片段
    assert "WHITESPACE_RE.sub(' ', content)" in src, "未对全文归一化"


# ---------- 纯函数行为单测 ----------

def test_same_prefix_different_tail_produces_different_hash():
    """
    两篇 >1000 字、开头 1000 字完全相同但尾部不同的文章 → 指纹必须不同。
    这是 finding 的判别性场景: 修复前二者碰撞, 修复后应区分。
    """
    from services.research_monitor.content_hash import compute_content_hash

    prefix = "A" * 1000
    art1 = prefix + " 独家研究证据一, 关于甲主题的深度分析结论。" * 20
    art2 = prefix + " 完全不同的研究证据二, 关于乙主题的另一套结论。" * 20

    assert len(art1) > 1000 and len(art2) > 1000
    h1 = compute_content_hash(art1)
    h2 = compute_content_hash(art2)
    assert h1 != h2, "开头相同尾部不同的长文碰撞成同一指纹 (尾部碰撞未修)"


def test_identical_content_still_same_hash():
    """逐字相同的转载(仅空白差异, 归一化后一致)仍得同一 hash, 保跨域名去重能力。"""
    from services.research_monitor.content_hash import compute_content_hash

    body = "同一篇文章 正文 内容 段落。 " * 300  # 本身含单空格
    # 仅把已有空白扩成多空格/换行, 函数内部归一化后应折叠回单空格 → 同一指纹
    variant = body.replace(" ", "  \n ")
    assert compute_content_hash(body) == compute_content_hash(variant), \
        "空白差异导致同文转载指纹不一致, 破坏跨域名去重"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
