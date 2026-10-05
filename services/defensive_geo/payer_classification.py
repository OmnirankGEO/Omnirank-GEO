"""**谁付这笔钱** —— 防御型 GEO 全域唯一的付款方判别位(规格 §15.3 四格矩阵)。

═══════════════════════════════════════════════════════════════════════
🔴 这个模块存在的唯一理由:同一谓词不许写两处
═══════════════════════════════════════════════════════════════════════
判别位原来只长在 ``api/defensive_geo_api._classify_payer`` 里(诊断链)。
发布链的预算签发者 ``activation_materializer`` 够不到它(services 不能 import api),
于是它吃了 ``derive()`` 的默认值 ``personal_wallet`` —— **静默默认**。
后果不是"少了个功能",而是**两条链对同一个人给出两个付款方**:
admin 在诊断链走平台账,在发布链走自己的钱包,撞上 billing 的 admin 免单旁路
(``freeze_points`` 对 ``is_admin`` 直接返回零句柄且不写任何表)⇒ 裸 500。

所以判别位搬到 services 层,两条链**同源消费**。
搬家 = 行为零变化:诊断链那一侧只剩一个读 ``request.state.user`` 的适配器,
等价判据 ``test_r2_01`` 逐格核对两者输出相同。

═══════════════════════════════════════════════════════════════════════
🔴 分两层:纯判别 vs 取身份
═══════════════════════════════════════════════════════════════════════
``classify_payer`` 是**纯函数**(吃事实,不碰库)—— 判据可以逐格穷举。
取身份是另一件事,按调用方所在的层各有各的来源:

  · 端点侧:``request.state.user`` 已由 ``auth/middleware`` 填好 ``is_admin``;
  · worker 侧:只有一个 ``user_id``,必须**现查角色**
    (本仓 ``users`` 表**没有** ``is_admin`` 列 —— 它是
     ``roles.name == 'admin'`` 的派生值,``db/auth_db.get_user:490`` 与
     ``services/commercial_service_routing._read_provider`` 都是这么算的)。

把这两件事分开,是因为混在一起就没法让判别逻辑接受判据穷举 ——
一个吃 ``Request`` 的函数,判据只能造假 Request,而假 Request 的键
常常是生产从来不会发的那几个(本仓 2026-08-20 实录)。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, NamedTuple

logger = logging.getLogger("GEO-DefGeoPayer")

PAYER_CLASSIFICATION_VERSION = "defensive-geo-payer-classification-v1"

#: 迁移 044 ``chk_defgeo_pcmd_platform_state`` 用的就是这个字面值。
PLATFORM_PRINCIPAL_KIND = "platform_cost_center"


class PayerClassification(NamedTuple):
    """三元组与 ``_classify_payer`` 的历史返回值**逐位同形** —— 搬家不改形状。"""

    funding_policy: str
    principal_kind: str
    sponsor_policy_ref: str | None


#: 判别结果的全集。**只有两格** —— ``organization_budget`` 的腿尚未接通,
#: ``sponsor_platform_ledger`` 必须绑已签 sponsor policy(没有 policy 就签发它,
#: 等于让平台白掏钱且无从追责)。判据拿它当分母。
CLASSIFIABLE_POLICIES: tuple[str, ...] = ("admin_platform_ledger", "personal_wallet")

_PLATFORM = PayerClassification("admin_platform_ledger", PLATFORM_PRINCIPAL_KIND, None)
_PERSONAL = PayerClassification("personal_wallet", "personal", None)


def classify_payer(
    *,
    is_admin: bool,
    user_id: int | None,
    platform_direct_user_id: int | None,
) -> PayerClassification:
    """**纯**判别:三个事实 → 付款方三元组。

    · admin,或本人就是平台直营服务账号 → 平台成本腿;
    · 其余 → 个人钱包。

    🔴 ``platform_direct_user_id`` 是**入参**不是这里现查的:现查会让这个
       函数碰库,判据就没法穷举它;而穷举正是四格矩阵唯一守得住的方式。
    """
    if bool(is_admin):
        return _PLATFORM
    if (
        platform_direct_user_id is not None
        and user_id is not None
        and int(user_id) == int(platform_direct_user_id)
    ):
        return _PLATFORM
    return _PERSONAL


def platform_direct_service_user_id() -> int | None:
    """现役平台账腿的账号。取不到返回 ``None``(由调用方翻成 typed 拒绝,不是 500)。

    🔴 复用 ``services.commercial_service_routing.get_platform_direct_service_user_id``
       —— 那是本仓「平台账」的**现役 SSOT**(``scheduler.py`` 的 ``_billing_uid_for_sub``
       在 ``billing_mode=='platform'`` 时走的就是它,``publish_funding``
       ``_platform_service_user_id`` 也调它)。不造第三种换号谓词。
    """
    try:
        from services.commercial_service_routing import get_platform_direct_service_user_id

        uid = get_platform_direct_service_user_id()
        return int(uid) if uid else None
    except Exception as exc:  # noqa: BLE001 —— 未配置/表缺失都算「门栈不可用」
        logger.info("[defgeo-payer] 平台直营服务账号不可用:%s", exc)
        return None


def classify_request_user(user: Mapping[str, Any] | None) -> PayerClassification:
    """端点侧适配器:``request.state.user`` → 判别结果。

    键名逐字取自 ``auth/middleware`` 写进去的那两个(``is_admin`` / ``user_id``)——
    **不猜键名**:夹具用一个生产从来不会发的键,会让整片端点在生产必炸而判据全绿。
    """
    u = user or {}
    return classify_payer(
        is_admin=bool(u.get("is_admin")),
        user_id=u.get("user_id"),
        platform_direct_user_id=platform_direct_service_user_id(),
    )


def is_admin_user(cur, user_id: int | None) -> bool:
    """worker 侧取身份:**现查角色**。``users`` 表没有 ``is_admin`` 列。

    谓词与 ``commercial_service_routing._read_provider`` / ``auth_db.get_user``
    同形(``roles.name == 'admin'``)—— 三处必须是同一个意思,否则
    "谁是 admin" 会随入口不同而不同。
    """
    if not user_id:
        return False
    cur.execute(
        "SELECT EXISTS("
        "  SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id"
        "   WHERE ur.user_id = %s AND r.name = 'admin'"
        ") AS is_admin",
        (int(user_id),),
    )
    row = cur.fetchone()
    if row is None:
        return False
    return bool(row["is_admin"] if not isinstance(row, tuple) else row[0])


def classify_user_id(cur, user_id: int | None) -> PayerClassification:
    """worker 侧:一个 ``user_id`` → 付款方三元组(现查角色 + 平台账号)。"""
    return classify_payer(
        is_admin=is_admin_user(cur, user_id),
        user_id=user_id,
        platform_direct_user_id=platform_direct_service_user_id(),
    )


def is_platform_principal(principal_kind: object) -> bool:
    """一行事实:这条命令是不是平台成本中心。**与迁移 044 的 CHECK 同一字面值**。"""
    return str(principal_kind or "") == PLATFORM_PRINCIPAL_KIND


def census() -> dict[str, Any]:
    """机械分母。判据从这里取,不手抄。"""
    return {
        "version": PAYER_CLASSIFICATION_VERSION,
        "platformPrincipalKind": PLATFORM_PRINCIPAL_KIND,
        "classifiablePolicies": list(CLASSIFIABLE_POLICIES),
        "cells": {
            "admin": _PLATFORM._asdict(),
            "platform_direct_account": _PLATFORM._asdict(),
            "ordinary": _PERSONAL._asdict(),
        },
    }
