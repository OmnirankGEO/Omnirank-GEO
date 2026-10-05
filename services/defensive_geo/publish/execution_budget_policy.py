"""provider-private 执行预算的**签发口径**(规格 §3.4)。

═══════════════════════════════════════════════════════════════════════
🔴 这个模块存在的理由 = 终审 P1-2 的真根因
═══════════════════════════════════════════════════════════════════════
Codex 终审记 P1-2 为「``POST /decision-snapshots/preview`` 对任何输入必 500」。
我按「先查根因再修」查下去,根因不在 preview 里:

    preview → ``publish_funding.check_and_lock_budget``
            → ``store.get_budget`` 返回 None
            → ``FundingError``
            → handler 的兜底 except → 受控 500

而 ``store.insert_budget`` 的**生产调用者集合是空的**(census 2026-08-24,
排 tests 后只剩定义处)。也就是说 ``defgeo_provider_execution_budgets``
这张表在生产里永远是空的,于是**任何输入**都会走到那个 raise ——
"任何输入必 500" 是这条空缺的必然结果,不是 preview 自己写错了。

所以修法有两半,缺一半都不算修:
  ① **签发者**(本模块 + ``activation_materializer``):让预算快照真的有人产;
  ② **信封**(``defensive_publish_api``):即使真的取不到,也必须是 typed
     可解释的拒绝 + 下一步,而不是裸 500。§0.5.6 铁律
     「任何阻塞与错误必须自带解决方案」。

═══════════════════════════════════════════════════════════════════════
🔴 何时签发:commercial basis 成立那一刻,**零冻结**
═══════════════════════════════════════════════════════════════════════
§3.4 逐字:「客户 accepted delivery plan 冻结交付/RMB;另一个 provider-private
immutable budget snapshot 冻结总执行 points 上限……两者以 accepted customer
snapshot/service projection 绑定」。§3.1 / ACT-06/13 同样逐字:
「commercial_basis_established 只写耐久 activation event;**不等于**已冻结执行算力」。

签一张 cap 快照 **不是** freeze —— 它一分钱不动,只是把「这一单最多能花多少」
写死。所以它落在 activation 物化那一步是对的,而且必须**零 freeze**。

═══════════════════════════════════════════════════════════════════════
🟡 待 Owner 签的那半格:**天花板系数**
═══════════════════════════════════════════════════════════════════════
cap 的**机制**(谁签、绑谁、什么时候签、怎么守恒)由规格定死,本模块完整实现。
cap 的**数值口径**是商业决策,规格没给,我不自行裁定、也不写「默认……」绕过 ——
本模块把它做成一个**具名、版本化、可被判据钉住**的推导:

    scope_cap(media_publication)
        = 合同承诺的发布篇数 × 当前目录中**合规可选媒体的最高单价**

它的性质(判据打的是这三条,不是那个数字):
  · **推导而非拍脑袋**:两个乘数都来自库里已有的不可变事实
    (承诺篇数来自客户已接受的交付计划;单价来自现役媒体目录);
  · **只会拦不会放**:cap 只在"要花的钱超过上限"时挡下,它永远不会
    让任何一次扣费变多。选错方向的代价是"拦得太松",不是"多扣钱";
  · **篇数守恒不靠它**:一格一 slot、一 slot 一条 live command 才是篇数闸
    (§12.2)。cap 是**第二条独立的**点数闸,两者互不替代。

Owner 若要改成别的口径(例如按客户成交额的某个比例、或按套餐档位固定值),
改 :data:`CEILING_KIND` 与 :func:`derive` 一处即可,版本号随之滚动;
历史快照不受影响(它是 immutable 的)。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Mapping, NamedTuple

logger = logging.getLogger("GEO-DefGeoExecutionBudget")

BUDGET_POLICY_VERSION = "defgeo-execution-budget-policy-v1"

#: 天花板取法的**具名**口径(🟡 待 Owner 签,见模块 docstring)。
#: 具名的意义:判据可以钉住"当前生效的是哪一种",换口径必须显式改这里,
#: 而不是有人把某个乘数悄悄调了一下。
CEILING_KIND = "catalog_max_unit_price_times_committed_publications"

#: v1 只有这一个 scope。列按 scope 存(迁移 044),加 scope 不用改表。
SCOPE_KEY = "media_publication"

#: 交付计划里表示「承诺发布篇数」的那个键。取自
#: ``delivery_plan.CONTRACT_MINIMUM_KEYS``,不手抄 —— 手抄会在 schema 改名时静默取 0。
_PUBLICATIONS_KEY = "publications"


class BudgetPolicyError(RuntimeError):
    """推不出预算。**不签一个猜出来的 cap** —— 猜大了闸就是摆设,猜小了拦死交付。"""


class BudgetDraft(NamedTuple):
    """一张待落库的 provider-private 预算快照。``budget_hash`` 是它自己的指纹。"""

    execution_budget_snapshot_id: str
    budget_version: int
    tenant_owner_id: int
    accepted_snapshot_id: int
    service_projection_id: str
    global_cap_points: int
    scope_key: str
    scope_cap_points: int
    funding_policy: str
    #: 🔴 [E2-3 · Owner 2026-08-26 拍板「动态」] 这里存的是**租户 owner**,
    #:    不是物理付款账号,预算**不声称锁定**任何钱包。
    #:    平台承担腿的物理账号由 ``publish_funding`` 在**冻结那一刻现取**;
    #:    ``budget_hash`` 的分母里也刻意没有它(只有 ``fundingPolicy``)——
    #:    资金方向进指纹,物理账号不进,正是"冻结前动态"的直接体现。
    payer_user_id: int | None
    budget_hash: str
    #: 推导过程的可复算记录。**只进日志与判据,不进任何客户面**。
    derivation: Mapping[str, Any]

    def as_store_values(self) -> dict[str, Any]:
        return {
            "execution_budget_snapshot_id": self.execution_budget_snapshot_id,
            "budget_version": self.budget_version,
            "tenant_owner_id": self.tenant_owner_id,
            "accepted_snapshot_id": self.accepted_snapshot_id,
            "service_projection_id": self.service_projection_id,
            "global_cap_points": self.global_cap_points,
            "scope_key": self.scope_key,
            "scope_cap_points": self.scope_cap_points,
            "funding_policy": self.funding_policy,
            "payer_user_id": self.payer_user_id,
            "budget_hash": self.budget_hash,
        }


def service_projection_id_for(accepted_snapshot_id: int) -> str:
    """本单的 service projection 身份。**确定性派生**,不是随机串。

    §12.2 要求 publish slot 由 ``service_projection_id`` 等参数确定性派生;
    如果这个 id 每次现造,同一交付格会派生出不同 slot ——
    「同一 accepted 交付格跨 preview/version/HTTP key 始终是同一 slot」当场破。
    """
    return f"defgeo-svc-{int(accepted_snapshot_id)}"


def _catalog_max_unit_price(cur) -> int:
    """目录里合规可选媒体的最高单价(算力)。

    🔴 逐列写明、只读 ``our_price_points``(对服务商的最终算力)——
       采购价那几列本模块**根本不读**(§11.2 末句)。
    """
    cur.execute(
        "SELECT COALESCE(MAX(our_price_points), 0) AS m FROM mhz_media "
        " WHERE is_active = TRUE AND our_price_points IS NOT NULL AND our_price_points > 0"
    )
    row = cur.fetchone()
    if row is None:
        raise BudgetPolicyError("媒体目录读不到 —— 不签一个猜出来的上限")
    value = row["m"] if isinstance(row, Mapping) else row[0]
    return int(value or 0)


def _accepted_plan(cur, accepted_snapshot_id: int) -> tuple[Mapping[str, Any], int]:
    """取客户已接受那一版报价快照里的交付计划 + 它所属的 brand。"""
    cur.execute(
        "SELECT pricing_snapshot, brand_id FROM quote_pricing_snapshots WHERE id = %s",
        (int(accepted_snapshot_id),),
    )
    row = cur.fetchone()
    if row is None:
        raise BudgetPolicyError(
            f"accepted snapshot {accepted_snapshot_id} 不存在 —— 没有客户承诺就不该有执行预算"
        )
    snapshot = row["pricing_snapshot"] if isinstance(row, Mapping) else row[0]
    brand_id = row["brand_id"] if isinstance(row, Mapping) else row[1]
    if not isinstance(snapshot, Mapping):
        raise BudgetPolicyError("报价快照不是一个对象")
    plan = snapshot.get("delivery_plan")
    if not isinstance(plan, Mapping):
        raise BudgetPolicyError(
            "这一单不是 server-enrolled v2 交付计划(没有 delivery_plan)—— "
            "legacy 走它自己的现役执行链,不经过本预算"
        )
    return plan, int(brand_id)


def committed_publications(plan: Mapping[str, Any]) -> int:
    """合同承诺的发布篇数。**只读 contract minimum,不读 capacity**(DEL-01)。

    两者共用一个 getter 迟早会有人拿容量当承诺 —— 那正是 §19 第 11 发变异。
    """
    from services.defensive_geo.delivery_plan import contract_minimums_of

    return int(contract_minimums_of(plan).get(_PUBLICATIONS_KEY) or 0)


def derive(
    cur,
    *,
    tenant_owner_id: int,
    accepted_snapshot_id: int,
    funding_policy: str = "personal_wallet",
    payer_user_id: int | None = None,
    budget_version: int = 1,
) -> BudgetDraft:
    """按 :data:`CEILING_KIND` 推出一张预算草稿。**只读库,不写任何表。**"""
    plan, _brand_id = _accepted_plan(cur, accepted_snapshot_id)
    publications = committed_publications(plan)
    if publications <= 0:
        raise BudgetPolicyError(
            "这一单的交付计划里承诺发布篇数为 0 —— 没有发布承诺就不签发布预算"
        )
    unit_ceiling = _catalog_max_unit_price(cur)
    if unit_ceiling <= 0:
        raise BudgetPolicyError("媒体目录里没有任何可选媒体的单价 —— 推不出上限")

    scope_cap = publications * unit_ceiling
    derivation = {
        "policyVersion": BUDGET_POLICY_VERSION,
        "ceilingKind": CEILING_KIND,
        "committedPublications": publications,
        "catalogMaxUnitPoints": unit_ceiling,
        "scopeKey": SCOPE_KEY,
    }
    projection = service_projection_id_for(accepted_snapshot_id)
    digest = hashlib.sha256(
        json.dumps(
            {
                "tenantOwnerId": int(tenant_owner_id),
                "acceptedSnapshotId": int(accepted_snapshot_id),
                "serviceProjectionId": projection,
                "budgetVersion": int(budget_version),
                "globalCapPoints": scope_cap,
                "scopeKey": SCOPE_KEY,
                "scopeCapPoints": scope_cap,
                "fundingPolicy": funding_policy,
                **derivation,
            },
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    return BudgetDraft(
        # 🔴 id 由 hash 确定性派生 —— 重放同一份推导得到同一个 id,
        #    于是 ``insert_budget`` 撞主键就是"已经签过了",而不是签出第二张。
        execution_budget_snapshot_id="pbud_" + digest[:52],
        budget_version=int(budget_version),
        tenant_owner_id=int(tenant_owner_id),
        accepted_snapshot_id=int(accepted_snapshot_id),
        service_projection_id=projection,
        global_cap_points=scope_cap,
        scope_key=SCOPE_KEY,
        scope_cap_points=scope_cap,
        funding_policy=funding_policy,
        payer_user_id=int(payer_user_id) if payer_user_id is not None else None,
        budget_hash=digest,
        derivation=derivation,
    )


def census() -> dict[str, Any]:
    return {
        "policyVersion": BUDGET_POLICY_VERSION,
        "ceilingKind": CEILING_KIND,
        "scopeKey": SCOPE_KEY,
        "ownerSignoffPending": True,
        "ownerSignoffScope": "天花板系数(cap 的数值口径);机制与绑定关系由 §3.4 定死,不待签",
    }
