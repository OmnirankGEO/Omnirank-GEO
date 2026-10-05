"""WP2 · 结算权威解析与完整资金句柄(规格 02 §3.7 / §8.2 · 裁定 P0-6)。

## 四种 settlement_authority,以及为什么 `admin_exempt` 不是"免费"

| authority | 付款方 | 物理句柄 |
|---|---|---|
| `organization_charge` | owner(经组织计费链) | `organization_charge_link_id` |
| `direct_freeze` | owner 本人钱包 | 完整 direct handle(见下) |
| `admin_exempt` | **平台直营账号 136** | 完整 direct handle,payer = 136 |
| `legacy_refund` | 老链 `deduct_upfront` | 无(老口径) |

🔴 **裁定 P0-6(C1 资金冲突)**:`admin_exempt` 的旧口径是
「reserved/charged = 0、无资金 handle」的**零记账**。那违反 `08_billing §5.2`
与 Owner 2026-08-16 监测同构裁决(`monitoring-billing-subject-platform-covered-2026-08-16`:
管理员一点开启花的是平台账的钱,**免费=不记账**是违规)。

改判后:admin/平台侧操作**路由到平台直营账号 136 做真实 freeze/commit**,
`reserved/charged` 与 `final_price_points` **一致**,持有 136 账户的 direct handle,
并保留 exemption 规则/actor 审计。**不得再把免扣伪装成 N=0 / 零 handle**。

## 复用而不是另造

平台账解析**复用**现役 `services.commercial_service_routing.get_platform_direct_service_user_id()`
(env `PLATFORM_DIRECT_SERVICE_USER_ID`),形态**复用** `server.py` 监测开通那段
已上线的 `billing_mode ∈ {brand_owner, platform}` 判据:

  · 只有 `is_admin` 才能走平台账;
  · 平台账没配好 → **fail-closed 503**,绝不"以为平台付、实际扣服务商"。

## 完整 direct handle 为什么必须整组冻结

规格 §3.7:「不同 freeze 表的相同数字 id 可能碰撞」。生产实测 freeze 表族有两张:
`point_freezes` 与 `customer_credit_freezes`。只存 `freeze_id` 会在两表 id 撞号时
commit/release 到错的那一笔。所以整组 = `payer_user_id + freeze_id + freeze_table
+ task_ref + reserved_amount + physical_split_snapshot`,commit/release 原样使用,
**不猜表、不猜用户**。
"""
from __future__ import annotations

from typing import Any, Final, Mapping, Optional

AUTHORITY_ORGANIZATION: Final = "organization_charge"
AUTHORITY_DIRECT: Final = "direct_freeze"
AUTHORITY_ADMIN_EXEMPT: Final = "admin_exempt"
AUTHORITY_LEGACY: Final = "legacy_refund"

BILLING_MODE_FREEZE_PER_ITEM: Final = "freeze_per_item"
BILLING_MODE_DEDUCT_UPFRONT: Final = "deduct_upfront"

#: 句柄里 `freeze_table` 的**真实取值域**:`'legacy'` / `'v35'`。
#:
#: 🔴 [返修 2026-08-18] 第一版我按 `pg_tables` 枚举写成物理表名
#:    `point_freezes` / `customer_credit_freezes` —— **那会拒掉每一个真句柄**。
#:    `middleware/billing.py:1514` 实际返回 `"freeze_table": "legacy"`,
#:    而 `_route_freeze_table` 的注释(:94)逐字写着:
#:    「调用方若回传 freeze_points 句柄里的 freeze_table('legacy'|'v35')→ 直接用,不猜」。
#:    这是「同名字段在不同层可以是不同语义」的又一例:
#:    物理层它是表名,句柄层它是**路由标签**。我从错的那一层取了词表。
#:    收敛值域的目的没变(让"猜表"不可能),但值必须取自**真正的生产者**,
#:    不是我从 schema 里看到的名字。
FREEZE_TABLE_LEGACY: Final = "legacy"
FREEZE_TABLE_V35: Final = "v35"
ALLOWED_FREEZE_TABLES: Final[frozenset[str]] = frozenset(
    {FREEZE_TABLE_LEGACY, FREEZE_TABLE_V35}
)

#: 物理表名 —— 只用于文档/取证,**不进句柄**。留在这里是为了让两层语义的差别可见。
PHYSICAL_FREEZE_TABLES: Final[frozenset[str]] = frozenset(
    {"point_freezes", "customer_credit_freezes"}
)

#: legacy 退款/清理 SQL **只能**匹配明确旧模式。
#: 🔴 迁移 034 零 DML ⇒ 存量行 billing_mode 为 NULL。写 `billing_mode <> 'freeze_per_item'`
#:    对 NULL 恒 UNKNOWN → 老行被漏掉;写 `IS NULL OR = 'deduct_upfront'` 才是对的。
#:    规格 §13 原话:「NULL 老行不得误穿透新链」——两个方向都要防。
LEGACY_SETTLEMENT_PREDICATE: Final = (
    "(billing_mode IS NULL OR billing_mode = 'deduct_upfront')"
)
#: 新链谓词。显式相等,NULL 天然匹配不上。
FREEZE_PER_ITEM_PREDICATE: Final = "(billing_mode = 'freeze_per_item')"


def organization_charge_available() -> bool:
    """本 lane 现在能不能真的走**组织计费链**。

    🔴 [返工 2026-08-18 · Codex P0-06] 这个函数存在的唯一理由,是把
       「组织计费**就绪**」从一个写死的 `True` 换成一句可核验的事实。

    两个条件缺一不可:

      ① 全局开关 `ORGANIZATION_SHARED_PAYER_ENABLED` 打开;
      ② 图文 lane **确实把 `services.organization_billing.reserve_charge`
         接上了**。

    ⚠️ **当前 ② 为假,所以本函数恒 False**,并且这是刻意的:
       仓内组织计费的物理形态是 `organization_charge_links`
       (`reserve_charge` → `settle_charge` / `release_charge`),它与
       `freeze_points` 是**两套互斥**的权威(034 的
       `ck_geo_douyin_task_authority_shape` 把这条互斥写进了 CHECK)。
       本 lane 目前只接了 direct freeze 那一套。

       在没接上之前:
         · 返 False ⇒ `resolve_settlement_authority` 给 `authority=None`
           ⇒ 端点按规格 §9 返回**可执行的 403 handoff**
           (「这次操作需要由团队负责人发起」);
         · 返 True  ⇒ 声称走组织计费、实际做 direct freeze
           ⇒ 形态自检抛 ⇒ **500**。

       两害相权:403 是**真话**且有下一步;500 是谎话且没有下一步。
       规格 §9 原文也正是「member 只能 handoff owner,**绝不 fallback 扣 actor**」。

    🔴 **这是一条已申报的功能缺口,不是修好了**:组织成员目前无法自助发起
       图文制作 / 投放,必须交给团队负责人。要闭掉它,得把 `reserve_charge`
       按现役 lane 的形态(见 `server.py` 诊断链)接进本链的两个入口,
       并把 `organization_charge_link_id` 随 item 落库 —— 那牵动组织额度、
       审批上限与 owner consent 三层商业口径,属于要 Owner 拍板的范围,
       不在本次返工的授权内。接上之后,把 ② 改成真实探测即可。
    """
    try:
        from services.organization_contract import feature_flags

        if not feature_flags().get("ORGANIZATION_SHARED_PAYER_ENABLED"):
            return False
    except Exception:  # noqa: BLE001 —— 读不到开关按未就绪(fail-closed)
        return False
    # ② 见 docstring:本 lane 尚未接 reserve_charge。
    return False


class PlatformAccountUnavailable(RuntimeError):
    """平台直营账号不可用。必须 fail-closed 503,不许回落扣服务商。"""


class FundingHandleInvalid(ValueError):
    """资金句柄不完整或形态非法。这是 H0(资金守恒),不是普通参数错。"""


def resolve_platform_payer_user_id() -> int:
    """平台直营账号 id。复用现役 resolver,不读第二个 env、不写死 136。

    🔴 刻意**不**在代码里写常量 136:它是生产配置(`PLATFORM_DIRECT_SERVICE_USER_ID`),
       写死会让测试环境和生产各说各话。文档里出现 136 是**当前实测值**,不是常量。
    """
    try:
        from services.commercial_service_routing import get_platform_direct_service_user_id
        return int(get_platform_direct_service_user_id())
    except Exception as exc:  # noqa: BLE001 —— 统一成一种失败,调用方只处理一类
        raise PlatformAccountUnavailable(
            f"平台承担账户不可用,无法以平台账记账:{exc}"
        ) from exc


def resolve_settlement_authority(*, is_admin: bool, organization_id: Optional[int],
                                 organization_billing_ready: bool,
                                 owner_user_id: int) -> dict[str, Any]:
    """服务端解析这次操作走哪种结算权威、由谁付款。

    🔴 **客户端不得指定 payer**(规格 §8.2)。本函数的入参全部来自服务端已解析的
       认证/组织上下文,没有一个来自请求体。

    返回 `{authority, payer_user_id, billing_mode, exemption_rule}`。
    """
    if is_admin:
        # P0-6:admin/平台侧 → 平台直营账号真实记账。解析不到就抛,由调用方 503。
        return {
            "authority": AUTHORITY_ADMIN_EXEMPT,
            "payer_user_id": resolve_platform_payer_user_id(),
            "billing_mode": BILLING_MODE_FREEZE_PER_ITEM,
            "exemption_rule": "admin_platform_direct_2026_08_17",
        }
    if organization_id:
        if not organization_billing_ready:
            # 规格 §9:flags/policy 未开或组织上下文不完整时,member 只能 handoff owner,
            # **绝不 fallback 扣 actor**。这里返回 None 让调用方给可执行提示。
            return {
                "authority": None,
                "payer_user_id": None,
                "billing_mode": None,
                "exemption_rule": None,
                "handoff_reason": "ORGANIZATION_BILLING_NOT_READY",
            }
        return {
            "authority": AUTHORITY_ORGANIZATION,
            "payer_user_id": int(owner_user_id),
            "billing_mode": BILLING_MODE_FREEZE_PER_ITEM,
            "exemption_rule": None,
        }
    return {
        "authority": AUTHORITY_DIRECT,
        "payer_user_id": int(owner_user_id),
        "billing_mode": BILLING_MODE_FREEZE_PER_ITEM,
        "exemption_rule": None,
    }


def build_direct_handle(*, payer_user_id: int, freeze_id: int, freeze_table: str,
                        task_ref: str, reserved_amount: int,
                        physical_split_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """整组 direct freeze 句柄。任一格缺失即抛 —— 半个句柄比没有句柄更危险,
    因为它看起来像是"有记账"。"""
    if not payer_user_id or int(payer_user_id) <= 0:
        raise FundingHandleInvalid("direct 句柄缺 payer_user_id")
    if not freeze_id or int(freeze_id) <= 0:
        raise FundingHandleInvalid("direct 句柄缺 freeze_id")
    if freeze_table not in ALLOWED_FREEZE_TABLES:
        raise FundingHandleInvalid(
            f"未知 freeze_table {freeze_table!r};只允许 {sorted(ALLOWED_FREEZE_TABLES)} —— "
            "不同表的相同数字 id 会碰撞,commit/release 不许猜表"
        )
    if not task_ref:
        raise FundingHandleInvalid("direct 句柄缺 task_ref")
    if reserved_amount is None or int(reserved_amount) < 0:
        raise FundingHandleInvalid("direct 句柄缺 reserved_amount")
    return {
        "payer_user_id": int(payer_user_id),
        "freeze_id": int(freeze_id),
        "freeze_table": str(freeze_table),
        "task_ref": str(task_ref),
        "reserved_amount": int(reserved_amount),
        "physical_split_snapshot": dict(physical_split_snapshot or {}),
    }


def assert_reserved_matches_price(*, authority: str, reserved_amount: int,
                                  final_price_points: int) -> None:
    """规格 §6.2 末:非 exempt 模式的物理 reserve/freeze 金额必须**逐项等于**
    snapshot 的 `final_price_points`;不等则整项回滚并要求重新确认。

    🔴 裁定 P0-6 之后,`admin_exempt` **也在这条约束之内** —— 它现在是对平台账的
       真实冻结,reserved 必须等于 N。旧口径下它是 0,那正是要消灭的形态。
       所以这里**没有** exempt 分支;谁加回一个 `if authority == admin_exempt: return`,
       对应判据当场红。
    """
    if authority == AUTHORITY_LEGACY:
        return  # 老链走 deduct_upfront,不适用逐项冻结口径
    if int(reserved_amount) != int(final_price_points):
        raise FundingHandleInvalid(
            f"冻结额 {reserved_amount} 与权威价 {final_price_points} 不一致"
            f"(authority={authority});整项回滚并要求重新确认"
        )


def assert_authority_shape(row: Mapping[str, Any]) -> None:
    """应用层的形态自检,与 034 迁移里的 `ck_*_authority_shape` **同口径**。

    为什么两层都要:DB CHECK 挡住写坏的行,应用层这一道让**读**到形态不对的历史行时
    也能立刻发现,而不是把一个没有 handle 的 `admin_exempt` 当成"已记账"继续往下走。
    """
    authority = row.get("settlement_authority")
    if authority is None or authority == AUTHORITY_LEGACY:
        return
    org_link = row.get("organization_charge_link_id")
    freeze_id = row.get("freeze_id")
    freeze_table = row.get("freeze_table")
    payer = row.get("payer_user_id")
    reserved = row.get("reserved_amount")

    if authority == AUTHORITY_ORGANIZATION:
        if not org_link:
            raise FundingHandleInvalid("organization_charge 缺 organization_charge_link_id")
        if freeze_id or freeze_table:
            raise FundingHandleInvalid("organization_charge 不得同时持有 direct 句柄(两套权威互斥)")
        return

    if authority in (AUTHORITY_DIRECT, AUTHORITY_ADMIN_EXEMPT):
        if org_link:
            raise FundingHandleInvalid(f"{authority} 不得同时持有组织 charge link(两套权威互斥)")
        missing = [
            name for name, value in (
                ("freeze_id", freeze_id), ("freeze_table", freeze_table),
                ("payer_user_id", payer), ("reserved_amount", reserved),
            ) if value in (None, "")
        ]
        if missing:
            raise FundingHandleInvalid(
                f"{authority} 的 direct 句柄不完整,缺 {missing}。"
                + ("🔴 admin_exempt 自 2026-08-17 裁定 P0-6 起是**真实记账**"
                   "(平台直营账号 freeze/commit),不再是零 handle 的免扣。"
                   if authority == AUTHORITY_ADMIN_EXEMPT else "")
            )
        return

    raise FundingHandleInvalid(f"未知 settlement_authority: {authority!r}")
