"""反向对照(工单 §4 硬要求):真挂 1 条现役渠道关系的服务商必须被拒,
且拒绝话术要**准确点名是渠道关系**;十二项全零的服务商必须一步降级成功。

若「有依赖也放行」或「仍报 12 项」,整条判据作废 —— 所以正反两侧同文件成对写。

种子里 provider 200 天生挂着 1 条现役渠道关系(conftest:615),正是生产上
145/150/162 三个账号的同款形态:十二项里只有这一项非零。
"""

from __future__ import annotations

import os

import psycopg2
import pytest

from services.admin_user_governance import (
    GovernanceValidationError,
    change_business_identity,
    change_channel_relationship,
)


DB_URL = os.environ["TEST_DATABASE_URL"]
PROVIDER = 200


def _execute(sql: str, params=()) -> None:
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def _scalar(sql: str, params=()):
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None


def _version(user_id: int, scope: str) -> int:
    """直接读乐观锁版本表。

    🔴 刻意不走 get_admin_user_detail:仓库现有基线上它是红的
    (regorigin 上线的 `_relationships` 查了 `m.is_owner`,测试 conftest 的
    schema 里没有这一列 → 9 条既有集成用例同因失败)。本包的判据不该被
    一条与它无关的基线故障连坐,否则等于没有实跑证据。
    """
    value = _scalar(
        """SELECT version FROM admin_user_governance_versions
           WHERE subject_user_id=%s AND scope=%s""",
        (user_id, scope),
    )
    return int(value) if value is not None else 1


def _downgrade(user_id: int):
    return change_business_identity(
        user_id, "ordinary_user",
        expected_version=_version(user_id, "business_identity"),
        reason="反向对照用例",
        operator_user_id=1, operator_username="admin",
        request_id=f"test-downgrade-{user_id}", ip_address="127.0.0.1",
    )


def _agent_level(user_id: int) -> int:
    return int(_scalar("SELECT agent_level FROM user_wallets WHERE user_id=%s", (user_id,)))


def _end_channel_relation() -> None:
    """按正确顺序的第一步:账号还是服务商时终结渠道关系。"""
    change_channel_relationship(
        PROVIDER, None, 10000,
        expected_version=_version(PROVIDER, "channel_relationship"),
        reason="降级前置：终结直属上游",
        operator_user_id=1, operator_username="admin",
        request_id="test-end-channel", ip_address="127.0.0.1",
    )


def _active_relations() -> int:
    return int(_scalar(
        """SELECT COUNT(*) FROM channel_pricing_relationships
           WHERE status='active' AND effective_to IS NULL
             AND (buyer_dealer_id=%s OR upstream_channel_account_id=%s)""",
        (PROVIDER, PROVIDER),
    ))


# --------------------------- 反面:唯一非零项是渠道关系 → 拒绝,且点名渠道关系

def test_provider_with_one_channel_relation_is_refused_and_named_precisely():
    assert _active_relations() == 1, "反向对照的前提没了：种子里应有 1 条现役渠道关系"

    with pytest.raises(GovernanceValidationError) as caught:
        _downgrade(PROVIDER)

    exc = caught.value
    assert exc.code == "ACTIVE_PROVIDER_DEPENDENCIES"
    # 🔴 闸必须真的挡住 —— 不许「话术变好了但人已经降下去了」
    assert _agent_level(PROVIDER) == 1

    message = str(exc)
    assert "现役渠道关系" in message, f"没点名是渠道关系：{message}"

    # 必须不命中:其余十一项一个都不许出现在话术里
    for absent in (
        "商业绑定客户", "服务商库存", "待支付进货订单", "待支付客户订单",
        "待裁决客户归属争议", "在途争议托管", "未结服务商收益", "未结渠道收益",
        "在途结算申请", "在途提现申请", "平台生产主体",
    ):
        assert absent not in message, f"零值项被列进了话术：{absent} —— {message}"

    blockers = exc.details["blockers"]
    assert len(blockers) == 1, f"应只列 1 项，实际 {len(blockers)} 项：{blockers}"
    assert blockers[0]["key"] == "active_channel_relations"
    assert blockers[0]["value"] == 1
    assert blockers[0]["entry"] == "channel_relationship", "没给出可操作入口"
    assert blockers[0]["handling"].strip(), "没说下一步怎么办"


# ------------------------------------------------ 正面:全零 → 一步降级成功

def test_provider_downgrades_in_one_step_once_the_only_blocker_is_cleared():
    _end_channel_relation()
    assert _active_relations() == 0

    result = _downgrade(PROVIDER)
    assert result["success"] is True
    assert result["after"]["business_identity"] == "ordinary_user"
    assert _agent_level(PROVIDER) == 0


# ---------------------------------------------- 顺序陷阱本身:先降级就永久卡死

def test_channel_relation_cannot_be_ended_after_downgrade():
    """🔴 「顺序不可换」不是文案偏好,是硬约束:降级后 agent_level=0,
    change_channel_relationship 从此永久拒绝。所以向导必须驱动顺序。"""
    _end_channel_relation()
    _downgrade(PROVIDER)
    assert _agent_level(PROVIDER) == 0

    # 再挂一条关系(模拟「降级时漏掉了一条」),此刻已经没有任何治理途径能终结它
    _execute(
        """INSERT INTO channel_pricing_relationships(
               buyer_dealer_id, upstream_channel_account_id, relationship_version,
               cost_multiplier_bps, status, effective_to)
           VALUES (%s, 28, 'stuck-v1', 10000, 'active', NULL)""",
        (PROVIDER,),
    )
    with pytest.raises(GovernanceValidationError) as caught:
        change_channel_relationship(
            PROVIDER, None, 10000,
            expected_version=_version(PROVIDER, "channel_relationship"),
            reason="降级之后再想终结渠道关系",
            operator_user_id=1, operator_username="admin",
            request_id="test-channel-after-downgrade", ip_address="127.0.0.1",
        )
    assert caught.value.code == "CHANNEL_SUBJECT_MUST_BE_SERVICE_PROVIDER"
    assert _active_relations() == 1, "关系确实终结不掉 —— 这就是死态"
