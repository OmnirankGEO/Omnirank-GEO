"""P0-C 判据 · WO_INVREL_P0_HOTFIX §9.5。

三件事:
  1. 补录只加审计行,**不碰** `customer_agent_bindings`(逐字段不变);
  2. 反向对照:裸插一条无审计的 admin_manual 绑定 → **仍然亮灯**
     (证明补录不是"按 id 加白名单");
  3. 根治:走一次「升级为服务商」,断言**当场就有** commercial_binding 审计、灯从来没亮过。
"""

from __future__ import annotations

import pytest

from services.admin_user_governance import (
    _relation_attention_sql,
    backfill_commercial_binding_audit,
    change_business_identity,
)

from .conftest import TEST_ID_BASE, make_binding, make_user

CUST = TEST_ID_BASE + 61
PROV = TEST_ID_BASE + 62
ADMIN = TEST_ID_BASE + 63
FRESH = TEST_ID_BASE + 64


def _lit(cur, user_id: int) -> bool:
    """灯亮不亮 —— 用**判定侧同一段 SQL**,不另写口径。"""
    cur.execute(
        f"""SELECT ({_relation_attention_sql()}) AS needs_attention
            FROM users u JOIN customer_agent_bindings cab ON cab.customer_user_id=u.id
            WHERE u.id=%s""",
        (int(user_id),),
    )
    row = cur.fetchone()
    return bool(row and row["needs_attention"])


def _binding_row(cur, user_id: int):
    cur.execute(
        "SELECT id, customer_user_id, agent_user_id, binding_source, bound_at, "
        "source_token, dispute_status FROM customer_agent_bindings WHERE customer_user_id=%s",
        (int(user_id),),
    )
    row = cur.fetchone()
    return dict(row) if row else None


@pytest.fixture
def manual_binding(db):
    cur = db.cursor()
    make_user(cur, PROV, phone="13920000001", name="承接服务商", agent_level=1)
    make_user(cur, CUST, phone="13920000002", name="人工归属客户", agent_level=0)
    make_user(cur, ADMIN, phone="13920000003", name="治理管理员", agent_level=0)
    make_binding(cur, customer_user_id=CUST, agent_user_id=PROV, source="admin_manual")
    # 历史绑定:bound_at 拨回一个月,任何今天写的审计都必然落在 ±5 秒窗外
    cur.execute(
        "UPDATE customer_agent_bindings SET bound_at = NOW() - INTERVAL '30 days' "
        "WHERE customer_user_id=%s", (CUST,),
    )
    db.commit()
    return cur


# ============================================================
# §9.5-1 补录不改写绑定
# ============================================================

def test_backfill_lights_off_and_never_touches_binding(manual_binding, db):
    assert _lit(manual_binding, CUST) is True, "前置:这条历史人工归属应当亮灯"
    before = _binding_row(manual_binding, CUST)

    backfill_commercial_binding_audit(
        CUST, expected_version=1, reason="2026-07 双方线下确认由该服务商承接",
        operator_user_id=ADMIN, operator_username="admin", request_id="req-backfill-1",
        ip_address="127.0.0.1",
    )

    # 🔴 灯当场熄灭
    assert _lit(manual_binding, CUST) is False
    # 🔴 绑定行**逐字段**不变 —— 尤其 bound_at 不许被改写
    assert _binding_row(manual_binding, CUST) == before


def test_backfill_is_mechanically_distinguishable(manual_binding, db):
    backfill_commercial_binding_audit(
        CUST, expected_version=1, reason="补录说明", operator_user_id=ADMIN,
        operator_username="admin", request_id="req-backfill-2", ip_address=None,
    )
    manual_binding.execute(
        "SELECT evidence_jsonb FROM admin_user_governance_audits "
        "WHERE subject_user_id=%s AND scope='commercial_binding'", (CUST,),
    )
    evidence = manual_binding.fetchone()["evidence_jsonb"]
    # 补录必须自曝是补录,且锚死当时的绑定时间 —— 不许伪装成"当时就有的审计"
    assert evidence["backfill"] == "true"
    assert evidence["binding_untouched"] is True
    assert evidence["original_bound_at"]


# ============================================================
# §9.5-2 🔴 反向对照:裸插的无审计绑定仍然亮灯
# ============================================================

def test_naked_admin_manual_binding_still_lights_up(manual_binding, db):
    """证明补录不是"按 id 加白名单":另建一条一模一样的裸绑定,必须照样亮。"""
    make_user(manual_binding, FRESH, phone="13920000004", name="另一个客户", agent_level=0)
    make_binding(manual_binding, customer_user_id=FRESH, agent_user_id=PROV, source="admin_manual")
    db.commit()
    assert _lit(manual_binding, FRESH) is True

    # 给 CUST 补录后,FRESH 不受影响 —— 豁免是**逐条配对**的,不是全局开关
    backfill_commercial_binding_audit(
        CUST, expected_version=1, reason="补录说明", operator_user_id=ADMIN,
        operator_username="admin", request_id="req-backfill-3", ip_address=None,
    )
    assert _lit(manual_binding, CUST) is False
    assert _lit(manual_binding, FRESH) is True


# ============================================================
# §9.5-3 🔴 根治:身份升级同事务补齐,灯从来没亮过
# ============================================================

def test_identity_upgrade_converts_binding_so_lamp_cannot_linger(manual_binding, db):
    """如实记录**实测到的**行为:升级为服务商时,入向客户归属会被
    `_convert_customer_binding_to_channel_on_upgrade` 就地转成渠道关系,
    当前投影被关闭(来源事实进 `customer_agent_binding_history`)。

    🔴 所以 §9.3(a) 描述的机制在**升级**这条路径上不复现 —— 绑定没了,灯自然没了。
       真正会留下亮灯绑定的是「设上级」那条(见下一条用例),
       那也正是 Owner 口述的操作(「我手动设了上级、设成服务商」)。
       这条用例把这个差别钉住,免得下一个人照着工单描述去改错地方。
    """
    assert _lit(manual_binding, CUST) is True, "升级前应当是亮的"

    change_business_identity(
        CUST, "service_provider", expected_version=1,
        reason="线下签约成为服务商", operator_user_id=ADMIN,
        operator_username="admin", request_id="req-upgrade-1", ip_address=None,
    )

    assert _lit(manual_binding, CUST) is False
    # 灯灭的原因是**绑定被转换走了**,不是补了审计 —— 断言到具体机制,不只看灯
    assert _binding_row(manual_binding, CUST) is None
    manual_binding.execute(
        "SELECT count(*) AS n FROM channel_pricing_relationships "
        "WHERE buyer_dealer_id=%s AND status='active'", (CUST,),
    )
    assert int(manual_binding.fetchone()["n"]) == 1
    # 反向对照:身份确实变了
    manual_binding.execute("SELECT agent_level FROM user_wallets WHERE user_id=%s", (CUST,))
    assert int(manual_binding.fetchone()["agent_level"]) >= 1


def test_setting_upstream_backfills_lingering_binding_evidence(manual_binding, db):
    """🔴 根治本体:「设上级」不会移除已有人工归属 → 那条归属会一直缺凭证。

    这就是 u133 / u163 的形态(渠道关系版本号 `identity-upgrade-…`)。
    修好后:设上级的**同一事务**里凭证被补齐,灯当场熄灭,下次这样设置不会再亮。
    """
    from services.admin_user_governance import change_channel_relationship

    # 先把 CUST 变成服务商但**保留**那条人工归属(直接改 agent_level,
    # 绕开会做转换的升级路径 —— 这正是生产上那两条的既成形态)。
    manual_binding.execute(
        "UPDATE user_wallets SET agent_level=1 WHERE user_id=%s", (CUST,)
    )
    db.commit()
    assert _lit(manual_binding, CUST) is True, "前置:人工归属仍在且缺凭证"
    before = _binding_row(manual_binding, CUST)

    change_channel_relationship(
        CUST, PROV, 12000, expected_version=1, reason="线下确认由该上游供货",
        operator_user_id=ADMIN, operator_username="admin",
        request_id="req-upstream-1", ip_address=None,
    )

    # 🔴 灯当场熄灭,且归属行**逐字段不变**(只补凭证,不改归属)
    assert _lit(manual_binding, CUST) is False
    assert _binding_row(manual_binding, CUST) == before
    manual_binding.execute(
        "SELECT count(*) AS n FROM admin_user_governance_audits "
        "WHERE subject_user_id=%s AND scope='commercial_binding'", (CUST,),
    )
    assert int(manual_binding.fetchone()["n"]) == 1


def test_setting_upstream_does_not_spam_audits_when_light_is_off(manual_binding, db):
    """闸的反向:灯已灭时设上级**不**再补一条,避免刷审计。"""
    from services.admin_user_governance import change_channel_relationship

    manual_binding.execute("UPDATE user_wallets SET agent_level=1 WHERE user_id=%s", (CUST,))
    db.commit()
    backfill_commercial_binding_audit(
        CUST, expected_version=1, reason="先补一次", operator_user_id=ADMIN,
        operator_username="admin", request_id="req-backfill-5", ip_address=None,
    )
    assert _lit(manual_binding, CUST) is False

    change_channel_relationship(
        CUST, PROV, 12000, expected_version=1, reason="随后设上级",
        operator_user_id=ADMIN, operator_username="admin",
        request_id="req-upstream-2", ip_address=None,
    )
    manual_binding.execute(
        "SELECT count(*) AS n FROM admin_user_governance_audits "
        "WHERE subject_user_id=%s AND scope='commercial_binding'", (CUST,),
    )
    assert int(manual_binding.fetchone()["n"]) == 1, "灯已灭还补 = 刷审计"


# ============================================================
# P0-1b · 补录幂等(工单 WO_RESPONSE_MODEL_CONTRACT_GATE §2 末条 / 判据 7)
# ============================================================

def test_backfill_twice_refuses_and_never_writes_a_second_row(manual_binding, db):
    """🔴 重复补录必须**拒绝**,绝不写第 2 条审计。

    背景:事故期间该端点"写库成功但回包失败",用户看到失败必然重试 ——
    每次重试若都落一行,就会攒出一堆重复凭证。现网 binding 67 已有 1 条,
    修好后 Owner 会去点 u133,那一刻这条闸必须在。
    """
    from services.admin_user_governance import GovernanceValidationError

    backfill_commercial_binding_audit(
        CUST, expected_version=1, reason="第一次补录", operator_user_id=ADMIN,
        operator_username="admin", request_id="req-idem-1", ip_address=None,
    )
    manual_binding.execute(
        "SELECT count(*) AS n FROM admin_user_governance_audits "
        "WHERE subject_user_id=%s AND scope='commercial_binding'", (CUST,),
    )
    assert int(manual_binding.fetchone()["n"]) == 1

    # 第二次:灯已灭 → 拒绝(NO_CHANGE),不是静默成功
    with pytest.raises(GovernanceValidationError) as exc:
        backfill_commercial_binding_audit(
            CUST, expected_version=2, reason="重试(用户以为失败了)",
            operator_user_id=ADMIN, operator_username="admin",
            request_id="req-idem-2", ip_address=None,
        )
    assert exc.value.code == "NO_CHANGE"

    manual_binding.execute(
        "SELECT count(*) AS n FROM admin_user_governance_audits "
        "WHERE subject_user_id=%s AND scope='commercial_binding'", (CUST,),
    )
    assert int(manual_binding.fetchone()["n"]) == 1, "重试写出了第 2 条 —— 幂等闸失效"


def test_backfill_response_shape_passes_its_response_model(manual_binding, db):
    """🔴 打真出口:补录服务层的**真实返回体**必须能过端点声明的 response_model。

    这是 2026-08-14 事故的本地复现锁 —— 事故当时正是这里多了个 `backfill` 键。
    """
    from schemas.admin_user_governance import GovernanceMutationResponse

    result = backfill_commercial_binding_audit(
        CUST, expected_version=1, reason="补录说明", operator_user_id=ADMIN,
        operator_username="admin", request_id="req-shape-1", ip_address=None,
    )
    GovernanceMutationResponse.model_validate(result)  # 不许抛

    # 反向对照:多一个键必须红(证明这条锁不是恒真)
    with pytest.raises(Exception) as exc:
        GovernanceMutationResponse.model_validate({**result, "anything_extra": 1})
    assert "extra_forbidden" in str(exc.value)
