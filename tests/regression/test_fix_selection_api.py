"""
判别性回归锁 · api/selection_api.py 上线前 GEO 缺陷修复

主形态 = source-inspection:读源码文本断言修复标志存在。
回退任一修复 → 对应断言失败。不依赖 DB / 不 import server.py。

覆盖 candidate:
  GEO-R1-CAN-063  audit_single_keyword  owner authz + request 参数
  GEO-R1-CAN-073  mark_paid             owner authz
  GEO-R4-CAN-015  mark_paid             owner authz (同上,不同 root_cause_group)
  GEO-R1-CAN-110  approve_quote         owner authz + request 参数
  GEO-R1-CAN-111  edit_cluster          owner authz + request 参数
  GEO-R1-CAN-112  generate_quote        owner authz
  GEO-R1-CAN-130  sales_confirm_order   owner authz + request 参数
  GEO-R1-CAN-134  get_order_details     owner authz + request 参数
  GEO-R1-CAN-136  create_selection_link brand_id 分支 require_brand_access
  GEO-R7-CAN-013  create_selection_link brand_id 分支 (同上,不同 root_cause_group)
  GEO-R1-CAN-137  create_selection_link quote_id 分支 require_quote_access
  GEO-R1-CAN-133  token 明文脱敏(日志 + audit after 只存前缀)
  GEO-R2-CAN-030  sales_confirm_order   服务期写 fail-closed + 先于 session 翻转
  GEO-R2-CAN-015  mark_paid quote.status/paid_at 写 fail-closed(不吞异常)
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "api" / "selection_api.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def _handler_body(name: str) -> str:
    """截取 `async def <name>(` 到下一个 `@router` 装饰器之间的函数体源码。"""
    m = re.search(r"(?:async )?def " + re.escape(name) + r"\(", SRC)
    assert m, f"未找到 handler: {name}"
    start = m.start()
    nxt = SRC.find("\n@router", start + 1)
    if nxt == -1:
        nxt = len(SRC)
    return SRC[start:nxt]


# ----------------------------------------------------------------------------
# owner-authz 类:每个敏感 handler 必须调 _require_session_owner_access(request, token)
# ----------------------------------------------------------------------------

def test_can063_audit_single_keyword_owner_guard():
    body = _handler_body("audit_single_keyword")
    assert "request: Request" in body, "audit_single_keyword 缺 request 参数"
    assert "_require_session_owner_access(request, token)" in body, \
        "audit_single_keyword 缺 owner 校验 [GEO-R1-CAN-063]"


def test_can073_can015_mark_paid_owner_guard():
    body = _handler_body("mark_paid")
    assert "_require_session_owner_access(request, token)" in body, \
        "mark_paid 缺 owner 校验 [GEO-R1-CAN-073 / GEO-R4-CAN-015]"


def test_can110_approve_quote_owner_guard():
    body = _handler_body("approve_quote")
    assert "request: Request" in body, "approve_quote 缺 request 参数"
    assert "_require_session_owner_access" in body and "request, token" in body, \
        "approve_quote 缺 owner 校验 [GEO-R1-CAN-110]"


def test_can111_edit_cluster_owner_guard():
    body = _handler_body("edit_cluster")
    assert "request: Request" in body, "edit_cluster 缺 request 参数"
    assert "_require_session_owner_access(request, token)" in body, \
        "edit_cluster 缺 owner 校验 [GEO-R1-CAN-111]"


def test_can112_generate_quote_owner_guard():
    body = _handler_body("generate_quote_for_session")
    assert "_require_session_owner_access(request, token)" in body, \
        "generate_quote_for_session 缺 owner 校验 [GEO-R1-CAN-112]"


def test_can130_sales_confirm_owner_guard():
    body = _handler_body("sales_confirm_order")
    assert "request: Request" in body, "sales_confirm_order 缺 request 参数"
    assert "_require_session_owner_access(request, token)" in body, \
        "sales_confirm_order 缺 owner 校验 [GEO-R1-CAN-130]"


def test_can134_get_order_details_owner_guard():
    body = _handler_body("get_order_details")
    assert "request: Request" in body, "get_order_details 缺 request 参数"
    assert "_require_session_owner_access(request, token)" in body, \
        "get_order_details 缺 owner 校验 [GEO-R1-CAN-134]"


# ----------------------------------------------------------------------------
# create_selection_link:body 里的 brand_id / quote_id 必须显式校验归属
# ----------------------------------------------------------------------------

def test_can136_can013_create_brand_id_access():
    body = _handler_body("create_selection_link")
    assert "require_brand_access(request, req.brand_id)" in body, \
        "create_selection_link brand_id 分支缺 require_brand_access [GEO-R1-CAN-136 / GEO-R7-CAN-013]"
    # 校验必须发生在把 req.brand_id 赋给 brand_id 之前
    i_check = body.find("require_brand_access(request, req.brand_id)")
    i_assign = body.find("brand_id = req.brand_id")
    assert i_check != -1 and i_assign != -1 and i_check < i_assign, \
        "require_brand_access 必须在使用 req.brand_id 之前"


def test_can137_create_quote_id_access():
    body = _handler_body("create_selection_link")
    assert "require_quote_access(request, req.quote_id, allow_null=False)" in body, \
        "create_selection_link quote_id 分支缺 require_quote_access [GEO-R1-CAN-137]"
    # 校验必须在 get_session_by_quote(req.quote_id) 之前(领取已有 token 前就拦)
    i_check = body.find("require_quote_access(request, req.quote_id, allow_null=False)")
    i_use = body.find("get_session_by_quote(req.quote_id)")
    assert i_check != -1 and i_use != -1 and i_check < i_use, \
        "require_quote_access 必须先于 get_session_by_quote"


# ----------------------------------------------------------------------------
# GEO-R1-CAN-133 token 脱敏
# ----------------------------------------------------------------------------

def test_can133_send_quote_audit_no_raw_token():
    """approve_quote 的 send_quote audit_log after 只存 token 前缀,不落明文。"""
    body = _handler_body("approve_quote")
    # after 字典里不再出现明文 `"token": token,`
    assert '"token": token,' not in body, \
        "send_quote audit after 仍落明文 token [GEO-R1-CAN-133]"
    assert '"token": token[:8]' in body, \
        "send_quote audit after 应只存 token[:8] 前缀 [GEO-R1-CAN-133]"


def test_can133_recall_revert_delete_logs_truncated():
    """recall / revert / delete 日志只打 token 前缀。"""
    recall = _handler_body("recall_quote")
    assert "token={token}," not in recall, "recall 日志仍打明文 token [GEO-R1-CAN-133]"
    assert "token[:8]" in recall, "recall 日志应用 token[:8] [GEO-R1-CAN-133]"

    revert = _handler_body("_revert_selection_session")
    assert "状态回退: token={token}," not in revert, "revert 日志仍打明文 token [GEO-R1-CAN-133]"
    assert "token[:8]" in revert, "revert 日志应用 token[:8] [GEO-R1-CAN-133]"

    delete = _handler_body("delete_selection_session")
    assert "token={token}:" not in delete and "删除选词会话: token={token}," not in delete, \
        "delete 日志仍打明文 token [GEO-R1-CAN-133]"
    assert "token[:8]" in delete, "delete 日志应用 token[:8] [GEO-R1-CAN-133]"


# ----------------------------------------------------------------------------
# GEO-R2-CAN-030 sales_confirm 服务期写 fail-closed + 先于 session 翻转
# ----------------------------------------------------------------------------

def test_can030_sales_confirm_service_term_fail_closed():
    body = _handler_body("sales_confirm_order")
    # 服务期写块必须在 status=pending_payment 翻转之前
    i_service = body.find("UPDATE quotes SET service_months")
    i_flip = body.find('status="pending_payment"')
    assert i_service != -1 and i_flip != -1 and i_service < i_flip, \
        "服务期写必须先于 session 翻转 pending_payment [GEO-R2-CAN-030]"
    # 服务期写失败必须 raise(fail-closed),不再吞异常返 success
    seg = body[i_service:i_flip]
    assert "raise HTTPException" in seg, \
        "服务期写失败必须 fail-closed raise,不能吞异常 [GEO-R2-CAN-030]"
    assert "GEO-R2-CAN-030" in body


# ----------------------------------------------------------------------------
# GEO-R2-CAN-015 mark_paid quote.status/paid_at 写 fail-closed
# ----------------------------------------------------------------------------

def test_can015_mark_paid_quote_status_fail_closed():
    body = _handler_body("mark_paid")
    # 标志注释存在
    assert "GEO-R2-CAN-015" in body, "mark_paid 缺 GEO-R2-CAN-015 fail-closed 修复"
    # quote.status 推送外层 except 不再静默 → 必须 raise
    # 定位到 quote.status='confirmed' 推送段(update_quote_status as _uqs)
    i_push = body.find("update_quote_status as _uqs")
    assert i_push != -1, "未找到 quote.status 推送段"
    # session 翻 active 的 UPDATE 语句
    i_flip = body.find("SET status = 'active'")
    assert i_flip != -1 and i_push < i_flip, "quote.status 推送应在 session 翻 active 之前"
    seg = body[i_push:i_flip]
    # 该段内出现 fail-closed 的 raise HTTPException(报价状态同步失败)
    assert "报价状态同步失败" in seg, \
        "quote.status/paid_at 写失败必须 fail-closed raise,不能吞异常后仍翻 active [GEO-R2-CAN-015]"
