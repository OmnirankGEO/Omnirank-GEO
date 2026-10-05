"""
判别性回归锁 — api/publish_api.py GEO 缺陷修复。

主形态 = source-inspection: 断言修复标志存在于对应 handler 区域。
回退任一修复 → 对应断言失败。不依赖 DB / 不 import server.py。

覆盖:
- GEO-R2-CAN-025  create_decision_snapshot 跨租户快照引用 → 调度 KPI 泄漏
- GEO-R1-CAN-108  deep-analyze / media-recommendation 无文章归属校验
- GEO-R9-CAN-003  正文 [:4000] 静默截断丢弃尾部风险信号

另含一条 _build_content_hint 纯函数行为单测(不需 DB/app)。
"""
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "api" / "publish_api.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def _region(anchor: str, span: int = 2500) -> str:
    """返回从 anchor 起始的一段源码文本(用于按 handler 区域断言)。"""
    idx = SRC.find(anchor)
    assert idx != -1, f"锚点未找到: {anchor}"
    return SRC[idx: idx + span]


# ---------------------------------------------------------------------------
# 公共前置: 归属 helper 已引入且存在
# ---------------------------------------------------------------------------
def test_brand_access_helpers_imported():
    assert "from auth.brand_access import require_brand_access, require_quote_access" in SRC
    assert "async def _require_article_access(" in SRC
    # helper 必须 fail-closed 校验文章 brand 归属
    helper = _region("async def _require_article_access(")
    assert "require_brand_access(request, article.get(\"brand_id\"), allow_null=False)" in helper
    assert "status_code=404" in helper  # 文章不存在 → 404


# ---------------------------------------------------------------------------
# GEO-R1-CAN-108: deep-analyze 授权先行(在读缓存之前)
# ---------------------------------------------------------------------------
def test_deep_analyze_authorizes_before_cache_read():
    region = _region("async def deep_analyze_for_publish(")
    # 授权调用存在
    assert "_require_article_access(request, req.article_id)" in region
    # 授权必须在读缓存之前 (否则跨租户命中他人缓存)
    auth_pos = region.find("_require_article_access(request, req.article_id)")
    cache_pos = region.find("get_article_publish_analysis, req.article_id")
    assert auth_pos != -1 and cache_pos != -1
    assert auth_pos < cache_pos, "授权必须先于缓存读取"
    # 标记注释在位
    assert "GEO-R1-CAN-108" in region


def test_deep_analyze_no_unauthorized_refetch():
    """确认原始无鉴权的 get_article_for_publish_recommendation 直取已被移除/替换。"""
    region = _region("async def deep_analyze_for_publish(", span=1800)
    # 原漏洞行: 直接 to_thread 取文章而不鉴权 — 不应再出现在 deep-analyze 主体
    assert "get_article_for_publish_recommendation, req.article_id" not in region


def test_media_recommendation_authorizes_article():
    region = _region("async def recommend_article_media(")
    assert "_require_article_access(request, article_id)" in region
    # 不再出现无鉴权直取
    assert "get_article_for_publish_recommendation, article_id" not in region
    assert "GEO-R1-CAN-108" in region


# ---------------------------------------------------------------------------
# GEO-R2-CAN-025: create_decision_snapshot 校验 brand/quote/article 归属
# ---------------------------------------------------------------------------
def test_decision_snapshot_authorizes_brand_quote_article():
    region = _region("async def create_decision_snapshot(")
    assert "require_brand_access(request, req.brand_id, allow_null=False)" in region
    assert "require_quote_access(request, req.quote_id, allow_null=False)" in region
    assert "_require_article_access(request, _aid)" in region
    assert "for _aid in req.article_ids" in region
    assert "GEO-R2-CAN-025" in region
    # 授权必须先于 create_publish_decision_snapshot 落库
    auth_pos = region.find("require_brand_access(request, req.brand_id")
    write_pos = region.find("create_publish_decision_snapshot")
    assert auth_pos != -1 and write_pos != -1 and auth_pos < write_pos


# ---------------------------------------------------------------------------
# GEO-R9-CAN-003: 正文不再硬截前 4000 字
# ---------------------------------------------------------------------------
def test_content_hint_no_hard_4000_slice():
    # 硬截片段已移除
    assert "article_content[:4000]" not in SRC
    assert "_build_content_hint(" in SRC
    region = _region("def _build_content_hint(")
    assert "GEO-R9-CAN-003" in SRC
    # 首尾采样标志
    assert "_CONTENT_HINT_HEAD" in region
    assert "_CONTENT_HINT_TAIL" in region


# ---------------------------------------------------------------------------
# 纯函数行为单测: 尾部合规信号必须被保留(不需 DB/app)
# ---------------------------------------------------------------------------
def test_build_content_hint_retains_tail_signal():
    from api.publish_api import _build_content_hint, _CONTENT_HINT_HEAD, _CONTENT_HINT_TAIL

    # 空正文
    assert _build_content_hint("") == "无正文"

    # 短文全量返回
    short = "合规风险: 医疗宣称" * 5
    assert _build_content_hint(short) == short.strip()

    # 长文: 在 4000 字之后(靠近结尾)埋一个独特合规信号 → 必须保留
    signal = "【尾部独特合规信号XZK9】"
    body = "普通正文内容。" * 1000  # 远超 4000 字
    long_text = body + signal
    hint = _build_content_hint(long_text)
    assert signal in hint, "4000 字之后的尾部合规信号被丢弃 — R9 未修复"
    # 且确实发生了截断(否则等于原文, 不算首尾采样)
    assert len(hint) < len(long_text)
    assert "中段略" in hint


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
