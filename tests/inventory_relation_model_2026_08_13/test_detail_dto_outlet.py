"""🔴 打**真出口**的 DTO 判据 —— Deploy NO-GO 2026-08-13 的返修锁。

事故:热修给 `overview` 塞了 `needs_attention` / `attention_label`,
      而端点 `GET /api/admin/user-governance/users/{id}` 的 response_model
      `AdminUserDetailResponse.overview` 是 `extra="forbid"` 的 `UserOverview`,
      20 个字段里没有这两个 → **上线后该端点每次都 500**。
      讽刺的是 `schemas/admin_user_governance.py:57` 就写着这条警告。

**同型第二次**(仓里已有 `dto-forbid-extra-outlet-lock-2026-08-06`
「DTO forbid 上线即挂 / 锁要打真出口」)—— 上次那道锁没覆盖 user-governance 这个出口,
本文件把它补上。

判据设计要点:
  · 打**真出口**:走 `get_admin_user_detail()` 的真实返回,再过端点声明的**同一个**
    response_model。只校验服务层字典、或只 grep 字段名,都抓不到这条 —— 事故正是这么漏的。
  · 配**反向对照**:往 overview 塞一个未声明键必须转红。
    没有反向对照的话,哪天 `extra="forbid"` 被改成 "ignore",本锁会静默失效。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.admin_user_governance_api import AdminUserDetailResponse
from services.admin_user_governance import get_admin_user_detail

from .conftest import TEST_ID_BASE, make_binding, make_user

CUST = TEST_ID_BASE + 81
PROV = TEST_ID_BASE + 82


@pytest.fixture
def detail_subject(db):
    cur = db.cursor()
    make_user(cur, PROV, phone="13930000001", name="承接服务商", agent_level=1)
    make_user(cur, CUST, phone="13930000002", name="人工归属客户", agent_level=0)
    make_binding(cur, customer_user_id=CUST, agent_user_id=PROV, source="admin_manual")
    cur.execute(
        "UPDATE customer_agent_bindings SET bound_at = NOW() - INTERVAL '30 days' "
        "WHERE customer_user_id=%s", (CUST,),
    )
    db.commit()
    return cur


def test_detail_payload_passes_the_endpoint_response_model(detail_subject):
    """🔴 真出口:服务层返回必须能过端点声明的 response_model。

    这条如果红,就是"上线后该端点 500"的**本地复现**。
    """
    payload = get_admin_user_detail(CUST)
    validated = AdminUserDetailResponse.model_validate(payload)  # 不许抛
    # 灯确实被带出来了(不是"通过校验但字段被吞了")
    assert validated.overview.needs_attention is True
    assert validated.overview.attention_label == "旧人工归属缺少直接审计"


def test_detail_payload_passes_when_lamp_is_off(detail_subject, db):
    """反向对照 ①:灯灭时同样能过模型,且值确实是 False —— 不是恒 True。"""
    from services.admin_user_governance import backfill_commercial_binding_audit

    backfill_commercial_binding_audit(
        CUST, expected_version=1, reason="补录说明", operator_user_id=PROV,
        operator_username="admin", request_id="req-dto-outlet-1", ip_address=None,
    )
    validated = AdminUserDetailResponse.model_validate(get_admin_user_detail(CUST))
    assert validated.overview.needs_attention is False
    assert validated.overview.attention_label is None


def test_undeclared_overview_key_is_rejected(detail_subject):
    """🔴 反向对照 ②:往 overview 塞一个**未声明**的键必须转红。

    没有这条,`extra="forbid"` 哪天被放宽成 "ignore" 时,上面那条锁会静默失效 ——
    校验照样通过,而字段被悄悄吞掉,和事故当时一样看不出来。
    """
    payload = get_admin_user_detail(CUST)
    payload["overview"]["totally_undeclared_field"] = 1
    with pytest.raises(ValidationError) as exc:
        AdminUserDetailResponse.model_validate(payload)
    assert "extra_forbidden" in str(exc.value)


def test_lamp_fields_are_declared_on_both_list_and_detail_models():
    """两侧模型都得有这盏灯 —— 事故根因就是"列表侧有、详情侧没有"。

    机械比对字段集合,不靠人眼看两个类。
    """
    from schemas.admin_user_governance import AdminUserListItem, UserOverview

    for field in ("needs_attention", "attention_label"):
        assert field in AdminUserListItem.model_fields, f"列表侧缺 {field}"
        assert field in UserOverview.model_fields, f"详情侧缺 {field}"
