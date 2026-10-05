"""判别性回归 · server.py W1 跨租户 IDOR / 越权修复锁

对每个修复的 handler,断言其函数体内(def 之后、下一个 @app 之前)出现了对应的
归属/admin 校验调用。回退任一修复 → 对应断言失败。

不依赖 DB / 不 import server.py(server.py import 触发 init_db 需真实库),
用源码文本区间断言,是稳健的判别性锁。

覆盖:GEO-R1-CAN-013/014/044/088/107/118/143/159, R2-CAN-012/019
跑: pytest tests/regression/test_fix_server_idor.py -v
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = (ROOT / "server.py").read_text(encoding="utf-8")

# [2026-07-22 板块D] TV /auth 活路由已迁至 api/dashboard_api.py
DASHBOARD_SRC = (ROOT / "api" / "dashboard_api.py").read_text(encoding="utf-8")


def _dashboard_handler_body(anchor: str, max_lines: int = 60) -> str:
    """与 _handler_body 同语义，作用于 api/dashboard_api.py 源码。"""
    idx = DASHBOARD_SRC.find(anchor)
    assert idx != -1, f"未找到 handler 锚点: {anchor}"
    tail = DASHBOARD_SRC[idx:]
    lines = tail.splitlines()[:max_lines]
    return "\n".join(lines)


def _handler_body(anchor: str, max_lines: int = 60) -> str:
    """返回 anchor(函数 def 或路由装饰器)之后 max_lines 行的源码窗口。"""
    idx = SRC.find(anchor)
    assert idx != -1, f"未找到 handler 锚点: {anchor}"
    tail = SRC[idx:]
    lines = tail.splitlines()[:max_lines]
    return "\n".join(lines)


class TestCrossTenantGuards:
    def test_optimize_generate_requires_quote_access(self):
        body = _handler_body("async def api_optimize_generate(")
        helper = _handler_body("def _require_supplement_keyword_access(")
        assert "_require_supplement_keyword_access(request, req.keyword_id)" in body, \
            "GEO-R1-CAN-088/R2-CAN-019: api_optimize_generate 必须从真实关键词解析 quote"
        assert "SELECT quote_id FROM confirmed_keywords WHERE id=%s" in helper, \
            "GEO-R1-CAN-088/R2-CAN-019: 不得信任客户端提交 quote_id"
        assert "require_quote_access(request, quote_id, allow_null=False)" in helper, \
            "GEO-R1-CAN-088/R2-CAN-019: 解析后的 quote 必须执行租户归属校验"

    def test_add_media_publication_requires_quote_access(self):
        body = _handler_body("def add_media_publication(")
        assert "http_request: Request" in body, "GEO-R1-CAN-107: 必须注入 http_request"
        assert "require_quote_access(http_request, request.quote_id" in body, \
            "GEO-R1-CAN-107: add_media_publication 必须校验 quote 归属"

    def test_replace_article_requires_diagnosis_access(self):
        body = _handler_body("async def replace_article(")
        assert "require_diagnosis_access(http_request, request.diagnosis_id" in body, \
            "GEO-R1-CAN-118: replace_article 必须校验 diagnosis 归属(且先于扣费)"
        # 校验在扣费之前
        assert body.index("require_diagnosis_access") < body.index("_bill_feature_ctx"), \
            "GEO-R1-CAN-118: 归属校验必须在扣费之前"

    def test_delete_topic_requires_quote_access(self):
        body = _handler_body("def api_delete_topic(")
        assert "require_quote_access(request, topic[\"quote_id\"]" in body, \
            "GEO-R1-CAN-159: api_delete_topic 必须校验 quote 归属"
        assert "quote_id" in body and "is_admin" in body, \
            "GEO-R1-CAN-159: quote_id 为空时应 admin-only 兜底"

    def test_rewrite_article_requires_quote_access(self):
        body = _handler_body("async def api_rewrite_article(", max_lines=70)
        assert "require_quote_access(http_request, row['quote_id']" in body, \
            "GEO-R1-CAN-143: api_rewrite_article 必须校验 quote 归属"

    def test_generate_titles_requires_quote_access(self):
        body = _handler_body("async def api_generate_titles(", max_lines=40)
        assert "require_quote_access(http_request, request.quote_id" in body, \
            "GEO-R2-CAN-012: api_generate_titles 必须校验 quote 归属"

    def test_start_diagnosis_requires_brand_access(self):
        body = _handler_body("async def start_diagnosis(", max_lines=20)
        assert "require_brand_access(http_request, request.brand_id" in body, \
            "GEO-R1-CAN-044: start_diagnosis 必须校验 brand 归属"


class TestAdminGates:
    def test_platform_weights_requires_admin(self):
        body = _handler_body("async def update_platform_weights(", max_lines=20)
        assert "is_admin" in body and "403" in body, \
            "GEO-R1-CAN-014: 全局平台权重更新必须 admin 闸"

    def test_tv_dashboard_auth_requires_admin(self):
        # GEO-R1-CAN-013: TV /auth 端点加固必须 admin 闸
        # [2026-07-22 板块D] 活路由迁至 api/dashboard_api.py::authenticated_tv_dashboard
        # （admin+HttpOnly 会话双因子），server.py 旧死 handler tv_dashboard_jwt 已删除。
        body = _dashboard_handler_body("async def authenticated_tv_dashboard(")
        assert "_require_tv_session(request)" in body, \
            "GEO-R1-CAN-013: TV /auth 端点必须经 _require_tv_session(admin+会话)闸"
        gate = _dashboard_handler_body("def _require_tv_session(")
        assert "_tv_admin_id" in gate, \
            "GEO-R1-CAN-013: _require_tv_session 必须含 _tv_admin_id admin 校验"
