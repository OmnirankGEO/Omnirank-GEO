"""判别性回归测试 · 修复批第2轮 · api/meijiehezi_api.py

source-inspection 锁(读源码断言修复标志·回退则失败),不 import server.py / 不依赖 DB。
覆盖:
  - GEO-R8-CAN-006  P1  /admin/* 跨租户路由必须走 is_admin 闸(_require_admin),不再是 _require_writing
  - GEO-R5-CAN-012  P3  发布/落单标题以 DB articles.title 为准,不发客户端 req.article_title / item.article_title
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SRC_PATH = Path(__file__).resolve().parents[2] / "api" / "meijiehezi_api.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def _func_body(name: str) -> str:
    """截取 `async def <name>(` 或 `def <name>(` 到下一个顶层 @router / def 之前的源码块。"""
    m = re.search(r"\n(?:async +)?def %s\(" % re.escape(name), SRC)
    assert m, f"未找到函数 {name}"
    start = m.start()
    # 下一个顶层装饰器或顶层 def(行首无缩进)
    rest = SRC[start + 1:]
    nxt = re.search(r"\n@router\.|\n(?:async +)?def [a-zA-Z_]", rest)
    end = start + 1 + (nxt.start() if nxt else len(rest))
    return SRC[start:end]


# ─────────────────────────── GEO-R8-CAN-006 ───────────────────────────

def test_require_admin_helper_exists():
    """必须新增独立的平台管理员闸 helper(仅放行 is_admin)。"""
    assert "def _require_admin(" in SRC, "缺少 _require_admin 管理员闸 helper"
    body = _func_body("_require_admin")
    assert "is_admin" in body and "403" in body, "_require_admin 未按 is_admin 做 403 拦截"


CROSS_TENANT_ADMIN_HANDLERS = [
    "api_admin_stats",        # /admin/stats  全平台统计
    "api_admin_list_refunds", # /admin/refund/list  全租户退款申请
    "api_admin_orders",       # /admin/orders  list_orders(user_id=None) 全租户订单
    "api_sync_status",        # /admin/sync/status  触发全局外部同步
    "api_sync_result",        # /admin/sync/result  全局同步配置
]


def test_cross_tenant_admin_routes_use_admin_gate():
    """跨租户 /admin/* handler 必须调用 _require_admin,且不得再用弱 _require_writing 闸。"""
    for fn in CROSS_TENANT_ADMIN_HANDLERS:
        body = _func_body(fn)
        assert "_require_admin(user)" in body, f"{fn} 未加 _require_admin 管理员闸(P1 越权回退)"
        assert "_require_writing(user)" not in body, f"{fn} 仍残留弱 _require_writing 闸"


def test_user_scoped_awaiting_routes_keep_writing_gate():
    """用户自查端点(user_id 已收窄)不应被误升为 admin-only,保持 _require_writing。"""
    for fn in ("api_list_awaiting_confirmations", "api_confirm_awaiting_item"):
        body = _func_body(fn)
        assert "_require_writing(user)" in body, f"{fn} 用户自查端点被误改,普通代理将无法使用"


# ─────────────────────────── GEO-R5-CAN-012 ───────────────────────────

def test_canonical_title_helper_exists():
    """必须有从 DB articles.title 读权威标题的 helper。"""
    assert "def _get_canonical_article_title(" in SRC, "缺少 _get_canonical_article_title helper"
    body = _func_body("_get_canonical_article_title")
    assert "FROM articles" in body and "title" in body, "helper 未从 articles.title 取权威标题"


def test_single_publish_order_uses_canonical_title():
    """单发落单 create_order 用 _canonical_title,不再直接传 req.article_title。"""
    body = _func_body("api_publish")
    assert "_canonical_title = _get_canonical_article_title(req.article_id)" in body, \
        "单发未计算 DB 权威标题"
    assert "create_order(user_id, req.article_id, _canonical_title" in body, \
        "单发 create_order 仍用客户端标题"
    # 客户端标题不得再作为落单/发布标题直接使用
    assert "create_order(user_id, req.article_id, req.article_title" not in body, \
        "单发 create_order 仍残留 req.article_title"


def test_single_publish_provider_send_uses_canonical_title():
    """单发送外部通道(publish / publish_wemedia)标题用 _canonical_title,不发 req.article_title。"""
    body = _func_body("api_publish")
    assert "title=req.article_title" not in body, "单发 provider 发送仍用客户端 req.article_title"
    assert body.count("title=_canonical_title") >= 2, \
        "单发软文+自媒体两条 provider 发送未都用 DB 权威标题"


def test_batch_publish_uses_canonical_title():
    """批量落单与发送标题以 DB 为准,不发客户端 item.article_title。"""
    body = _func_body("api_publish_batch")
    # 落单
    assert "_item_title = _get_canonical_article_title(item.article_id)" in body, \
        "批量落单未计算 DB 权威标题"
    assert "create_order(user_id, item.article_id, _item_title" in body, \
        "批量 create_order 仍用客户端标题"
    assert "create_order(user_id, item.article_id, item.article_title" not in body, \
        "批量 create_order 仍残留 item.article_title"
    # 提交查询取 a.title 且发送用 _item_title
    assert "SELECT a.title, a.content" in body, "批量提交内容查询未同源取 a.title"
    assert "title=item.article_title" not in body, "批量 provider 发送仍用客户端 item.article_title"
    assert body.count("title=_item_title") >= 2, \
        "批量软文+自媒体两条 provider 发送未都用 DB 权威标题"
