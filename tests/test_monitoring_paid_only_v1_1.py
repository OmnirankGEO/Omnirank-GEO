"""monitoring subscription enable guard · 服务锚口径回归测试

覆盖:
- enable 单点 + batch 共用服务锚 helper
- 服务锚接受 paid 且服务未结束,或 confirmed 且 service_start_date / paid_at 至少一个存在
- 服务锚 fail-closed(quote 查询失败 / 不存在 / 未进入服务期 → 全 raise)
- v1.1 P1-5 · batch-enable existing active sub 同步 ck(reused_ids · 不只 skip)
- v1.1 P1-6 · disable 清 monitoring_subscription_id=NULL(状态机一致性)
- v1.1 P1-7 · create_keyword_monitor_subscription 幂等(代码层 · DB unique index 双保险)
"""
from __future__ import annotations
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _read_text(rel_path: str) -> str:
    with open(os.path.join(_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# 服务锚 helper 存在 + 单点 + batch 都调
# ============================================================

def test_v11_service_anchor_helper_exists():
    """enable 守护必须使用服务锚 helper"""
    src = _read_text("server.py")
    assert "def _assert_keyword_quote_service_anchored_blocking" in src, \
        "服务锚 helper 未定义 · 单点 + batch 无共享约束"


def test_v11_single_enable_calls_helper():
    """单点 enable 必须调服务锚 helper"""
    src = _read_text("server.py")
    fn_start = src.find('@app.post("/api/monitoring/keyword/{keyword_id}/enable")')
    assert fn_start > 0, "找不到单点 enable endpoint"
    fn_block = src[fn_start:fn_start + 3000]

    assert "_assert_keyword_quote_service_anchored_blocking" in fn_block, \
        "单点 enable 未调服务锚 helper"


def test_v11_batch_enable_calls_helper():
    """batch-enable 必须调服务锚 helper(防绕过)"""
    src = _read_text("server.py")
    fn_start = src.find('@app.post("/api/monitoring/keyword/batch-enable")')
    assert fn_start > 0, "找不到 batch-enable endpoint"
    fn_block = src[fn_start:fn_start + 4000]

    assert "_assert_keyword_quote_service_anchored_blocking" in fn_block, \
        "batch-enable 未调服务锚 helper"
    assert "service_anchor_blocked" in fn_block, \
        "batch-enable 未在 failed list 标记 service_anchor_blocked reason"


# ============================================================
# 服务锚判定 · paid 或 confirmed+服务锚
# ============================================================

def test_v11_service_anchor_dual_policy():
    """helper 必须按服务锚判定,不能退回 paid-only"""
    src = _read_text("server.py")
    fn_start = src.find("def _assert_keyword_quote_service_anchored_blocking")
    fn_block = src[fn_start:fn_start + 3000]

    assert "SELECT status, paid_at, service_start_date, service_status" in fn_block, \
        "服务锚 helper 未 SELECT status/paid_at/service_start_date/service_status"
    assert 'status == "paid"' in fn_block or "status == 'paid'" in fn_block, \
        "服务锚 helper 未保留 paid 放行臂"
    assert 'status == "confirmed"' in fn_block or "status == 'confirmed'" in fn_block, \
        "服务锚 helper 未支持 confirmed+服务锚放行臂"
    assert "service_start_date" in fn_block and "paid_at" in fn_block, \
        "服务锚 helper 未检查 service_start_date / paid_at"
    assert "cancelled" in fn_block and "expired" in fn_block and "inactive" in fn_block, \
        "服务锚 helper 未拦截已结束/取消/停用服务"


# ============================================================
# v1.1 P0-3 · fail-closed(查询失败不放行)
# ============================================================

def test_v11_service_anchor_fail_closed_db_error():
    """v1.1 P0-3 · DB 查询异常必须 fail-closed(raise 503 · 不 pass 放行)"""
    src = _read_text("server.py")
    fn_start = src.find("def _assert_keyword_quote_service_anchored_blocking")
    fn_block = src[fn_start:fn_start + 3000]

    # except Exception 后必须 raise(不能 pass)
    assert "status_code=503" in fn_block, \
        "v1.1 P0-3 破:DB 异常未 raise 503 fail-closed · 仍 pass 放行"
    # detail 提示用户重试
    assert "稍后重试" in fn_block or "暂不可启动" in fn_block, \
        "v1.1 P0-3 破:503 detail 未提示用户重试"


def test_v11_service_anchor_fail_closed_quote_not_found():
    """v1.1 P0-3 · quote 不存在必须 raise 404"""
    src = _read_text("server.py")
    fn_start = src.find("def _assert_keyword_quote_service_anchored_blocking")
    fn_block = src[fn_start:fn_start + 3000]

    assert "status_code=404" in fn_block, \
        "v1.1 P0-3 破:quote 不存在未 raise 404"
    assert "项目不存在" in fn_block, \
        "v1.1 P0-3 破:404 detail 未明示 quote 不存在"


def test_v11_service_anchor_quote_id_none_fail_closed():
    """v1.1 P0-3 · keyword 没绑 quote_id 也 fail-closed(脏数据)"""
    src = _read_text("server.py")
    fn_start = src.find("def _assert_keyword_quote_service_anchored_blocking")
    fn_block = src[fn_start:fn_start + 3000]

    # quote_id 为空时直接 raise 402(脏数据 fail-closed)
    assert "if not quote_id:" in fn_block, \
        "v1.1 P0-3 破:keyword 没绑 quote_id 未 fail-closed"


# ============================================================
# v1.1 P1-5 · batch-enable existing reuse 同步 ck
# ============================================================

def test_v11_batch_enable_reuses_existing_sub_sync_ck():
    """v1.1 P1-5 · batch-enable existing active sub 必须同步 ck.is_monitored=TRUE · 返 reused_ids"""
    src = _read_text("server.py")
    fn_start = src.find('@app.post("/api/monitoring/keyword/batch-enable")')
    fn_block = src[fn_start:fn_start + 4000]

    # reused_ids 字段
    assert "reused_ids" in fn_block, \
        "v1.1 P1-5 破:batch-enable 未维护 reused_ids list"
    # existing 命中时调 update_keyword_monitor_state(同步 ck)
    # 找 existing 分支后 必须调 update_keyword_monitor_state
    existing_branch_idx = fn_block.find('if existing and existing.get("status")')
    assert existing_branch_idx > 0, "找不到 existing 分支"
    existing_block = fn_block[existing_branch_idx:existing_branch_idx + 1000]
    assert "update_keyword_monitor_state" in existing_block, \
        "v1.1 P1-5 破:existing 分支未调 update_keyword_monitor_state(不同步 ck)"
    assert "is_monitored=True" in existing_block, \
        "v1.1 P1-5 破:existing 分支同步 ck 时未 is_monitored=True"


def test_v11_batch_enable_response_includes_reused_count():
    """v1.1 P1-5 · 返回必须含 reused_count(老板 / 前端可见 UX)"""
    src = _read_text("server.py")
    fn_start = src.find('@app.post("/api/monitoring/keyword/batch-enable")')
    fn_block = src[fn_start:fn_start + 6000]

    assert '"reused_count"' in fn_block, \
        "v1.1 P1-5 破:batch-enable 返回未含 reused_count 字段"
    # 向后兼容 skipped_count 别名保留
    assert '"skipped_count"' in fn_block, \
        "v1.1 兼容性破:batch-enable 删了 skipped_count 别名(旧前端依赖)"


# ============================================================
# v1.1 P1-6 · disable 清 monitoring_subscription_id=NULL
# ============================================================

def test_v11_disable_clears_subscription_id():
    """update_keyword_monitor_state disable 时清 subscription_id,但保留列表可见状态"""
    src = _read_text("db/monitoring_db.py")
    fn_start = src.find("def update_keyword_monitor_state")
    fn_block = src[fn_start:fn_start + 2500]

    # disable 分支必须 SET monitoring_subscription_id = NULL
    assert "monitoring_subscription_id = NULL" in fn_block, \
        "v1.1 P1-6 破:disable 未清 monitoring_subscription_id=NULL(状态错位风险)"
    # 关闭自动监测只关开关和订阅,词条仍留在监测列表里可重开。
    assert "monitoring_status = 'inactive'" not in fn_block, \
        "disable 不应把词条状态改成 inactive,否则会从监测列表消失"


# ============================================================
# v1.1 P1-7 · create_keyword_monitor_subscription 幂等(代码层)
# ============================================================

def test_v11_create_sub_idempotent_select_first():
    """v1.1 P1-7 · create_keyword_monitor_subscription 必须先 SELECT 已有 active sub"""
    src = _read_text("db/monitoring_db.py")
    # 🔴 [parity 2026-08-16] 原来是 `find("def create_keyword_monitor_subscription")` + 写死 +3500。
    #   parity 加了 keyword_source 参数与取值校验,INSERT 被推到 3500 之外
    #   → insert_idx=-1 → 断言以 `3132 < -1` 形态红。**尺子量程不够,不是幂等保护没了。**
    #   🔴 但"量到函数真边界"也不对:那个名字命中的是**包装函数**(1654 字符、根本没有 INSERT),
    #      真正的 SELECT-before-INSERT 住在 `_with_cursor` 里。旧写法靠 +3500 跨过函数边界
    #      **恰好**扫到了下一个函数 —— 是运气,不是判据。
    #   ⇒ 直接量真正实现的那个函数,比原来更准也更严。
    fn_start = src.find("def create_keyword_monitor_subscription_with_cursor")
    assert fn_start > 0, "实现函数改名了 —— 判据锚点失效,必须回来重新对齐"
    _nx = src.find(chr(10) + "def ", fn_start + 1)
    fn_block = src[fn_start:_nx if _nx > 0 else len(src)]

    # 必须先 SELECT(幂等)
    select_idx = fn_block.find("SELECT id FROM keyword_monitor_subscriptions")
    insert_idx = fn_block.find("INSERT INTO keyword_monitor_subscriptions")
    assert 0 < select_idx < insert_idx, \
        "v1.1 P1-7 破:create_sub 未先 SELECT 已有 active sub · 仍直接 INSERT"
    # SELECT WHERE 含 active + paused_low_balance
    select_block = fn_block[select_idx:insert_idx]
    assert "active" in select_block and "paused_low_balance" in select_block, \
        "v1.1 P1-7 破:SELECT WHERE 漏 active / paused_low_balance"


def test_v11_create_sub_returns_existing_id_when_found():
    """v1.1 P1-7 · 已有 active sub 时返已有 id · 不 INSERT(代码层幂等)"""
    src = _read_text("db/monitoring_db.py")
    # 🔴 [2026-08-16] 与同文件上一条同样的病:写死 +3500 会跨过包装函数边界,
    #   靠"恰好扫到下一个函数"成立。本包给包装函数加了 billing_mode 参数(7 行),
    #   窗口就顶出去了。⇒ 直接锚真正的实现体。
    fn_start = src.find("def create_keyword_monitor_subscription_with_cursor")
    assert fn_start > 0, "实现函数改名了 —— 判据锚点失效"
    _nx = src.find(chr(10) + "def ", fn_start + 1)
    fn_block = src[fn_start:_nx if _nx > 0 else len(src)]

    # 找 row = cur.fetchone() 后必须 if row: return row['id']
    # 即:找到已有 sub 直接返(不进 INSERT 分支)
    assert "if row:" in fn_block, \
        "v1.1 P1-7 破:SELECT 后未 if row 分支(找到已有不直接返 · 仍 INSERT)"


# ============================================================
# v1.1 整体 · 报告措辞 partial unique 部署前不算完全止血
# ============================================================

def test_v11_create_sub_documents_partial_unique_dependency():
    """v1.1 · create_sub 注释必须明示"幂等代码层 + DB partial unique index 双保险"

    根因:Codex 抓 · 代码层"先 SELECT 再 INSERT"在 partial unique index 部署前仍有并发窗口
    措辞约束:报告不能说"双保险完成" · 必须说"待 Deploy-CTO 建索引完成双保险"
    """
    src = _read_text("db/monitoring_db.py")
    fn_start = src.find("def create_keyword_monitor_subscription")
    fn_block = src[fn_start:fn_start + 3500]

    # 注释必须提到 partial unique index 配合
    assert "unique" in fn_block.lower() or "uniq_kms" in fn_block, \
        "v1.1 破:create_sub 注释未提 partial unique index 双保险依赖"
