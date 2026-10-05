"""服务商「发展下级服务商」的申请—审批链路(工单 §P0-2)。

存在的理由(2026-08-12 P0 事故的**直接卡点**):
    下级是普通客户还是服务商,系统走两条完全不同的链路 ——
      普通客户(`agent_level=0`)→ `customer_agent_bindings` + 线下划拨
      服务商  (`agent_level>=1`)→ 渠道关系 + JIT 逐跳转售
    而两条建绑定的路径**都拒绝服务商当客户**
    (`customer_binding.py` / `admin_w4_api.py`),
    渠道关系又**只有管理员**能建(`PUT /api/admin/user-governance/users/{id}/channel-relationship`)。
    服务商自己没有任何入口 → 操作者转而尝试"绑成客户" → 被守卫挡住 → 链路中断。

本模块补的就是服务商侧那个入口:**申请**(服务商发起)→ **审批**(管理员)→
落 `channel_pricing_relationships`(仍然只由治理服务函数写)。

🔴 为什么要审批(工单要求交付单写明结论):
    渠道关系直接决定**分润链**与进货成本系数(`cost_multiplier_bps`),
    一条边就改变了平台、上游、下游三方的钱怎么分。
    自助建边等于让服务商单方面决定别人账上的钱 —— 因此**必须管理员审批**。
    申请方只能"提议"系数,最终值以审批时管理员确认的为准。

🔴 本模块**不自己写** `channel_pricing_relationships`:
    审批时同事务调 `admin_user_governance.change_channel_relationship_cur()`,
    CAS / 版本位 / 审计 / 环检测 / 成本系数下界全部继承。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from db.connection import get_db

logger = logging.getLogger("GEO-ChannelPartnerRequests")

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"

# 与 `channel_pricing.MIN_COST_MULTIPLIER_BPS` 同源:上游赚差价模型下,
# 下级进货价不得低于上游有效成本。这里只做前置提示,最终仍由 channel_pricing 兜底。
MIN_COST_MULTIPLIER_BPS = 10000


class ChannelPartnerRequestError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        self.code = str(code)
        self.details = dict(details)
        super().__init__(message)


# ============================================================
# 🔴 关系解析统一入口(工单 v3 §P0-1)
#
# 本模块**只有这一个**关系判别函数。第二个函数 = 第二套口径,工单明令禁止。
# 划拨签发、客户搜索、渠道申请三条链路全部走 `resolve_relationship`。
# ============================================================

# 关系类型 —— 注意**没有** "upstream" 这个值,那是故意的(见 R5)。
RELATION_NONE = "none"
RELATION_CUSTOMER = "customer"
RELATION_DOWNSTREAM = "downstream_partner"
RELATION_BOTH = "both"

# 🔴 不可区分性(工单 §P0-2 / §4.1 第 8、9 条)
#   「账号不存在」「陌生账号」「我的上游」三种情况必须走**同一份**响应:
#   同一 code、同一文案、同一结构、同一 HTTP 状态。
#   手机号与账号 ID 都是可枚举的 —— 任何差异都是一个探测位。
NO_RELATION_CODE = "TARGET_NOT_FOUND"
NO_RELATION_MESSAGE = "未找到该账号"


class NoRelationError(ChannelPartnerRequestError):
    """唯一的「查无此人」出口。

    🔴 刻意**不带任何 details**:多一个字段就是一个可区分位。
    调用方不得根据"其实账号存在"再补充说明 —— 那等于把不可区分性拆掉。
    """

    def __init__(self) -> None:
        super().__init__(NO_RELATION_CODE, NO_RELATION_MESSAGE)


def _fetch_directed_binding(cur, actor_user_id: int, target_user_id: int):
    """target 是不是 actor 的**客户**?(有向:customer=target / agent=actor)"""
    cur.execute(
        """SELECT id, customer_user_id, agent_user_id, binding_source, dispute_status, bound_at
           FROM customer_agent_bindings
           WHERE customer_user_id=%s AND agent_user_id=%s""",
        (int(target_user_id), int(actor_user_id)),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _fetch_directed_channel(cur, actor_user_id: int, target_user_id: int):
    """target 是不是 actor 的**下线服务商**?(有向:buyer=target / upstream=actor)

    🔴 R5 的全部要害就在这两个占位符的顺序上。
       写成 `buyer_dealer_id=actor AND upstream=target` 就是"我的上游是谁",
       一旦那样查,下级立刻能确认自己有没有上游 —— Owner 红线当场破。
       所以这个方向的查询**在整个代码库里都不存在**,不是"查了不返回"。
    """
    cur.execute(
        """SELECT id, buyer_dealer_id, upstream_channel_account_id,
                  cost_multiplier_bps, relationship_version, status, effective_from
           FROM channel_pricing_relationships
           WHERE buyer_dealer_id=%s AND upstream_channel_account_id=%s
             AND status='active' AND effective_to IS NULL""",
        (int(target_user_id), int(actor_user_id)),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def resolve_relationship(cur, actor_user_id: int, target_user_id: int) -> Dict[str, Any]:
    """🔴 有向关系解析 · 只回答「**target 是不是 actor 的下级**」。

    **永远不回答**「target 是不是 actor 的上级」—— 下级方向的查询退化成
    `NoRelationError`,与「账号不存在」逐字节同构(工单 §0.1 R5 / §4.1 第 9 条)。

    🔴 为什么先查关系、后查身份(顺序不能倒):
        关系未命中时**根本不去 `users` 表**。于是「陌生账号」「不存在的账号」
        「我的上游」三条路径跑的是**同样两条 SQL、同样的空结果、同样的返回**——
        不可区分性成了**结构性质**,而不是"查到了但忍住不说"的补丁。
        先查 users 再判关系的话,存在与否会体现在耗时上(工单要求"同一量级耗时")。

    返回值**不含**任何关系私有字段(`cost_multiplier*` / `relationship_id` /
    `upstream_*` …)。计价要用已绑定系数请走 `resolve_bound_cost_multiplier_bps`,
    它是**后端计算用**的,结果不进响应体。
    """
    actor_id, target_id = int(actor_user_id), int(target_user_id)
    if actor_id == target_id:
        # 自己不是自己的下级。走同一条不可区分出口 —— 这里没有泄露风险
        # (谁都知道自己是谁),但保持单一出口可以少一个分支口径。
        raise NoRelationError()

    binding = _fetch_directed_binding(cur, actor_id, target_id)
    channel = _fetch_directed_channel(cur, actor_id, target_id)

    if not binding and not channel:
        raise NoRelationError()

    # —— 到这里才允许读身份:关系已确认,actor 有权知道下游是谁 ——
    # 🔴 工单 §P0-1 第 5 条:身份必须**每次重新读取**,不得沿用"有 binding 就当普通客户"
    #    的旧分支 —— §1.5 的 8124 错账正是那条旧分支产生的。
    cur.execute(
        """SELECT u.id, u.display_name, u.username, COALESCE(w.agent_level,0) AS agent_level
           FROM users u LEFT JOIN user_wallets w ON w.user_id=u.id
           WHERE u.id=%s""",
        (target_id,),
    )
    row = cur.fetchone()
    if not row:
        # 关系行存在但账号已消失 —— 数据异常,仍走同一出口,不额外描述。
        raise NoRelationError()

    is_provider = int(row["agent_level"] or 0) >= 1
    if binding and channel:
        relation = RELATION_BOTH
    elif channel:
        relation = RELATION_DOWNSTREAM
    else:
        relation = RELATION_CUSTOMER

    payload: Dict[str, Any] = {
        "relation": relation,
        "target_user_id": target_id,
        "target_display_name": row["display_name"] or row["username"],
        "target_identity": "service_provider" if is_provider else "level0",
        "has_customer_binding": bool(binding),
        "has_channel_relationship": bool(channel),
        "binding_source": (binding or {}).get("binding_source"),
        "dispute_status": (binding or {}).get("dispute_status"),
    }
    payload.update(_describe_actions(relation, is_provider))
    return payload


def _describe_actions(relation: str, is_provider: bool) -> Dict[str, Any]:
    """把路由表(工单 §P0-1)翻成用户看得懂的话 + 真实可点的出口。

    🔴 §0.3:**禁止只有解释、没有动作**。每一支都必须给出至少一个真实 route/action。
    🔴 §0.5:界面文案不许出现表名、字段名、等级编号 —— 这里输出的就是最终文案。

    🔴 [P0 热修 2026-08-13 · WO_INVREL_P0_HOTFIX §1] 上一版把「供货给下线」做成**不可执行**,
       只给"查看渠道关系 + 复制进货提醒"并附一句说教。Owner 生产实测当场被挡。
       那是把 R4「关系存在 ⇒ 必须找得到、**能操作**」做成了"找得到但不许操作"——
       比原来的"搜不到"更差。Owner 已拍板(v3 §3.2 至此关闭):
         · 服务商线下进货,不走线上;
         · 这笔算力是上游的,**怎么用是他的事**;
         · **不许阻断**。
       所以下线与 both 的主动作都是**能点的划拨**,说教文案整句删除 ——
       只陈述这次操作的实际效果,不教用户该走哪条路。
    """
    view_channel = {"label": "查看渠道关系", "action": "view_channel", "route": "/agent/channel-partners"}
    copy_notice = {"label": "复制进货提醒", "action": "copy_purchase_message"}
    allocate = {"label": "划拨给客户", "action": "allocate_customer"}
    supply = {"label": "供货给下线", "action": "supply_downstream"}

    if relation == RELATION_DOWNSTREAM:
        return {
            "headline": "这是你的下线服务商",
            "effect_note": (
                "算力进 TA 的库存算力,TA 可以继续向下分销,也可以在自己的库存中心"
                "按 1:1 转成可用算力自用。成本按你们已约定的进货价计。"
            ),
            "primary_action": supply,
            "secondary_actions": [view_channel, copy_notice],
            "allowed_actions": ["supply_downstream", "view_channel", "copy_purchase_message"],
            "ledger_note": "inventory_wallet",
        }
    if relation == RELATION_BOTH and is_provider:
        return {
            "headline": "这是你的下线服务商,同时也是你的客户",
            "effect_note": (
                "默认按下线服务商供货:算力进 TA 的库存算力,TA 可继续向下分销,"
                "也可自行转成可用算力。若改按客户划拨,算力进 TA 的可用算力,不能再向下分销。"
            ),
            "primary_action": supply,
            "secondary_actions": [allocate, view_channel, copy_notice],
            "allowed_actions": [
                "supply_downstream", "allocate_customer", "view_channel", "copy_purchase_message",
            ],
            "ledger_note": "inventory_wallet",
        }
    if relation == RELATION_BOTH:
        # 两种关系都有,但对方当前是普通用户 → 默认按客户划拨(路由表第 4 行)。
        return {
            "headline": "已绑定客户,同时与你有渠道关系",
            "effect_note": "算力进 TA 的可用算力,TA 可直接用于诊断、写作等功能。",
            "primary_action": allocate,
            "secondary_actions": [view_channel],
            "allowed_actions": ["allocate_customer", "view_channel"],
            "ledger_note": "available_wallet",
        }
    if is_provider:
        # 仅客户绑定,但对方已升级为服务商(= 存量 binding 10 那种形态)。
        # 🔴 [P0 热修] 这里原来也把唯一能执行的动作降成了次动作 —— 同一个病。
        #    没有渠道关系就没有已绑定进货价,**供货**无从计价(R3 不许回落默认),
        #    所以这一支只有「划拨给客户」可执行,那它就该是主按钮。
        #    后果如实写在 effect_note 里让操作者自己决定,不替他选。
        return {
            "headline": "已绑定客户 · TA 现在也是服务商",
            "effect_note": (
                "算力进 TA 的可用算力,TA 可直接用于诊断、写作等功能,但不能再向下分销。"
                "若要让 TA 能进货分销,需要由平台建立你们之间的渠道关系。"
            ),
            "primary_action": allocate,
            "secondary_actions": [view_channel],
            "allowed_actions": ["allocate_customer", "view_channel"],
            "ledger_note": "available_wallet",
        }
    return {
        "headline": "已绑定客户",
        "effect_note": "算力进 TA 的可用算力,TA 可直接用于诊断、写作等功能。",
        "primary_action": allocate,
        "secondary_actions": [],
        "allowed_actions": ["allocate_customer"],
        "ledger_note": "available_wallet",
    }


def resolve_bound_cost_multiplier_bps(cur, upstream_user_id: int, downstream_user_id: int) -> int:
    """读取这对上下游**已绑定**的成本系数(工单 §0.1 R3 / §4.1 第 4 条计价锁)。

    🔴 **不得回落默认值、不得按身份现算**。系数是这对人的长期约定,
       只存在于 `channel_pricing_relationships`,只有管理员通道能改(R2)。
       没有已绑定关系就抛错 —— 静默回落默认值会让计价对不上账,
       而对不上账的计价比报错危险得多。

    返回值**只供后端计算**,不得进任何面向非 admin 的响应体(R5 反推位)。
    """
    channel = _fetch_directed_channel(cur, int(upstream_user_id), int(downstream_user_id))
    if not channel:
        raise ChannelPartnerRequestError(
            "NO_BOUND_CHANNEL_RELATIONSHIP", "这对上下游之间没有已生效的渠道关系"
        )
    bps = int(channel["cost_multiplier_bps"])
    if bps < MIN_COST_MULTIPLIER_BPS:
        raise ChannelPartnerRequestError(
            "BOUND_COST_MULTIPLIER_INVALID", "已绑定的进货系数不合法,请联系平台核对"
        )
    return bps


def quote_downstream_purchase(
    cur, upstream_user_id: int, downstream_user_id: int, platform_reference_cents: int,
) -> Dict[str, Any]:
    """按**已绑定**系数算出下线这批进货要付多少钱(工单 §P0-1 「复制进货提醒」的数据源)。

    🔴 这是 §4.1 第 4 条「计价锁」的被测对象,也是 `resolve_bound_cost_multiplier_bps`
       的**真实接线点** —— 只有函数没有调用方的话,锁全绿也证明不了运行时读了绑定值
       (本仓踩过「migration 没进 manifest + builder 零调用方 → 整包惰性」)。

    复用 `dealer_inventory_resale.calculate_hop_sale`,**不重造计价公式**(工单 §5)。
    本轮不改公式、不改倍率取值,只保证倍率取的是**这对人已绑定的那个值**(§0.6)。
    """
    from services.dealer_inventory_resale import calculate_hop_sale

    reference = int(platform_reference_cents)
    if reference <= 0:
        raise ChannelPartnerRequestError("REFERENCE_AMOUNT_INVALID", "参考金额必须大于 0")
    bound_bps = resolve_bound_cost_multiplier_bps(cur, upstream_user_id, downstream_user_id)
    price_cents = calculate_hop_sale(reference, bound_bps)
    return {
        # 🔴 只回价格数值,**不回** `cost_multiplier_bps` 本身(R5:系数是反推位)。
        "price_cents": int(price_cents),
        "reference_cents": reference,
    }


def resolve_downstream_path(cur, requester_user_id: int, target_user_id: int) -> Dict[str, Any]:
    """兼容外壳 —— 判别逻辑**全部**在 `resolve_relationship` 里,这里只做展示层拼装。

    🔴 v3 改造要点(工单 §1.3b 三条,逐条对应):
      1. 原来**按身份判别**(`agent_level>=1` → "是服务商") → 改为**按关系判别**;
      2. 原来对**任意** `target_user_id` 都回展示名与身份 = 现成的账号枚举接口
         → 改为无关系一律 `NoRelationError`,与"不存在"同构;
      3. 原来即使目标已是我的下线也只回"去申请设为下级",看不见既有关系
         → 现在直接返回既有关系与可执行动作。
    """
    rel = resolve_relationship(cur, requester_user_id, target_user_id)
    # `path` 保留给老调用方(`create_request` 用它判分流),语义改为**按关系**得出。
    rel["path"] = (
        "channel_relationship"
        if rel["relation"] in (RELATION_DOWNSTREAM, RELATION_BOTH)
        else "commercial_binding"
    )
    rel["guidance"] = rel["effect_note"]
    rel["entry_route"] = (rel.get("primary_action") or {}).get("route") or "/agent/channel-partners"
    return rel


# ============================================================
# 服务商侧:发起 / 查询 / 撤回
# ============================================================

def create_request(
    *, requester_user_id: int, target_user_id: int, proposed_cost_multiplier_bps: int,
    reason: str, request_id: str,
) -> Dict[str, Any]:
    reason = (reason or "").strip()
    if len(reason) < 2:
        raise ChannelPartnerRequestError("REASON_REQUIRED", "请填写申请理由(至少 2 个字)")
    bps = int(proposed_cost_multiplier_bps)
    if bps < MIN_COST_MULTIPLIER_BPS:
        raise ChannelPartnerRequestError(
            "COST_MULTIPLIER_TOO_LOW",
            f"进货系数不得低于 {MIN_COST_MULTIPLIER_BPS}(下级进货价不能低于你的有效成本)",
        )

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(agent_level,0) AS agent_level FROM user_wallets WHERE user_id=%s",
            (int(requester_user_id),),
        )
        me = cur.fetchone()
        if not me or int(me["agent_level"] or 0) < 1:
            raise ChannelPartnerRequestError(
                "REQUESTER_NOT_SERVICE_PROVIDER", "只有服务商可以发展下级服务商"
            )
        if int(requester_user_id) == int(target_user_id):
            raise ChannelPartnerRequestError("SELF_TARGET", "不能把自己发展为自己的下级")

        # 🔴 已经是我的下级 → 可以明说(这条**有向**关系本来就是申请人自己的,不泄露别人)。
        if _fetch_directed_channel(cur, requester_user_id, target_user_id):
            raise ChannelPartnerRequestError(
                "ALREADY_DOWNSTREAM", "该服务商已经是你的下级,无需重复申请"
            )

        # 🔴 以下三种"不能申请"必须共用**同一个** code + 同一句文案(工单 §P0-2):
        #      · 账号不存在        · 账号是普通用户        · 账号已有别的渠道上游
        #   原来它们是三个不同的 code(`TARGET_NOT_FOUND` / `TARGET_IS_ORDINARY_USER` /
        #   `TARGET_HAS_UPSTREAM`),而账号 ID 与手机号都可枚举 —— 三个码就是三个探测位,
        #   能问出"这个号注册没有""是不是服务商""有没有上游"。
        #   尤其 `TARGET_HAS_UPSTREAM`:下级拿自己的号来试,就能确认自己有上游。
        cur.execute(
            """SELECT COALESCE(w.agent_level,0) AS agent_level
               FROM users u LEFT JOIN user_wallets w ON w.user_id=u.id
               WHERE u.id=%s""",
            (int(target_user_id),),
        )
        target = cur.fetchone()
        target_is_provider = bool(target) and int(target["agent_level"] or 0) >= 1
        has_any_upstream = False
        if target_is_provider:
            cur.execute(
                """SELECT 1 FROM channel_pricing_relationships
                   WHERE buyer_dealer_id=%s AND status='active' AND effective_to IS NULL
                   LIMIT 1""",
                (int(target_user_id),),
            )
            has_any_upstream = cur.fetchone() is not None
        if (not target_is_provider) or has_any_upstream:
            raise ChannelPartnerRequestError(
                "TARGET_NOT_ELIGIBLE",
                "无法对该账号发起合作申请,请与平台管理员确认对方信息",
            )
        try:
            cur.execute(
                """INSERT INTO channel_partner_requests(
                       requester_user_id, target_user_id, proposed_cost_multiplier_bps,
                       reason, status, request_id)
                   VALUES (%s,%s,%s,%s,'pending',%s) RETURNING id, created_at""",
                (int(requester_user_id), int(target_user_id), bps, reason[:500], request_id),
            )
        except Exception as exc:  # UniqueViolation:同一目标只能有一条 pending
            from psycopg2 import errors as pg_errors

            if isinstance(exc, pg_errors.UniqueViolation):
                raise ChannelPartnerRequestError(
                    "PENDING_REQUEST_EXISTS", "该账号已有一条待审批的渠道合作申请"
                ) from exc
            raise
        row = cur.fetchone()
    logger.info(
        "[ChannelPartner] request #%s %s → %s bps=%s",
        row["id"], requester_user_id, target_user_id, bps,
    )
    return {"success": True, "request_id_row": int(row["id"]), "status": STATUS_PENDING,
            "created_at": row["created_at"]}


def list_requests_for_requester(requester_user_id: int, *, limit: int = 50) -> List[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT r.id, r.target_user_id, u.display_name AS target_display_name,
                      r.proposed_cost_multiplier_bps, r.status, r.reason,
                      r.decision_note, r.created_at, r.decided_at
               FROM channel_partner_requests r
               LEFT JOIN users u ON u.id=r.target_user_id
               WHERE r.requester_user_id=%s
               ORDER BY r.created_at DESC LIMIT %s""",
            (int(requester_user_id), int(limit)),
        )
        return [dict(row) for row in cur.fetchall() or []]


def cancel_request(*, request_row_id: int, requester_user_id: int) -> Dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM channel_partner_requests WHERE id=%s FOR UPDATE",
            (int(request_row_id),),
        )
        row = cur.fetchone()
        if not row or int(row["requester_user_id"]) != int(requester_user_id):
            raise ChannelPartnerRequestError("REQUEST_NOT_FOUND", "申请不存在")
        if str(row["status"]) != STATUS_PENDING:
            raise ChannelPartnerRequestError(
                "REQUEST_NOT_PENDING", f"该申请已是 {row['status']} 状态,不能撤回"
            )
        cur.execute(
            """UPDATE channel_partner_requests
               SET status='cancelled', decided_at=NOW(), updated_at=NOW()
               WHERE id=%s AND status='pending'""",
            (int(request_row_id),),
        )
        if cur.rowcount != 1:
            raise ChannelPartnerRequestError("REQUEST_NOT_PENDING", "该申请状态已变化,请刷新")
    return {"success": True, "status": STATUS_CANCELLED}


# ============================================================
# 管理员侧:列表 / 批准 / 驳回
# ============================================================

def list_requests_admin(*, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.cursor()
        where, params = "", []
        if status:
            where = "WHERE r.status=%s"
            params.append(str(status))
        params.append(int(limit))
        cur.execute(
            f"""SELECT r.id, r.requester_user_id, ru.display_name AS requester_display_name,
                       r.target_user_id, tu.display_name AS target_display_name,
                       r.proposed_cost_multiplier_bps, r.status, r.reason,
                       r.decision_note, r.decided_by, r.relationship_id,
                       r.created_at, r.decided_at
                FROM channel_partner_requests r
                LEFT JOIN users ru ON ru.id=r.requester_user_id
                LEFT JOIN users tu ON tu.id=r.target_user_id
                {where}
                ORDER BY r.created_at DESC LIMIT %s""",
            tuple(params),
        )
        return [dict(row) for row in cur.fetchall() or []]


def approve_request(
    *, request_row_id: int, cost_multiplier_bps: Optional[int],
    channel_expected_version: int, decision_note: str, operator_user_id: int,
    operator_username: Optional[str], request_id: str, ip_address: Optional[str],
) -> Dict[str, Any]:
    """批准渠道合作申请 · **同事务**落渠道关系。

    `cost_multiplier_bps` 为 None 时采用申请方提议值;管理员可覆写 ——
    最终生效的是审批时确认的值,不是申请方单方面填的那个。
    """
    from services.admin_user_governance import change_channel_relationship_cur

    note = (decision_note or "").strip()
    if len(note) < 2:
        raise ChannelPartnerRequestError("REASON_REQUIRED", "审批必须填写结论说明(至少 2 个字)")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM channel_partner_requests WHERE id=%s FOR UPDATE",
            (int(request_row_id),),
        )
        row = cur.fetchone()
        if not row:
            raise ChannelPartnerRequestError("REQUEST_NOT_FOUND", "申请不存在")
        if str(row["status"]) != STATUS_PENDING:
            raise ChannelPartnerRequestError(
                "REQUEST_NOT_PENDING", f"该申请已是 {row['status']} 状态"
            )
        bps = int(cost_multiplier_bps if cost_multiplier_bps is not None
                  else row["proposed_cost_multiplier_bps"])
        governance = change_channel_relationship_cur(
            cur,
            int(row["target_user_id"]),
            int(row["requester_user_id"]),
            bps,
            expected_version=int(channel_expected_version),
            reason=f"批准渠道合作申请 #{int(request_row_id)} · {note}"[:500],
            operator_user_id=int(operator_user_id),
            operator_username=operator_username,
            request_id=request_id,
            ip_address=ip_address,
        )
        cur.execute(
            """SELECT id FROM channel_pricing_relationships
               WHERE buyer_dealer_id=%s AND status='active' AND effective_to IS NULL""",
            (int(row["target_user_id"]),),
        )
        rel = cur.fetchone()
        cur.execute(
            """UPDATE channel_partner_requests
               SET status='approved', approved_cost_multiplier_bps=%s, decision_note=%s,
                   decided_by=%s, decided_at=NOW(), relationship_id=%s, updated_at=NOW()
               WHERE id=%s AND status='pending'""",
            (bps, note[:500], int(operator_user_id),
             int(rel["id"]) if rel else None, int(request_row_id)),
        )
        if cur.rowcount != 1:
            # 并发下已被别人处理 —— 整笔回滚,渠道关系一并撤销。
            raise ChannelPartnerRequestError("REQUEST_NOT_PENDING", "该申请状态已变化,请刷新")
    logger.info(
        "[ChannelPartner] approved #%s %s → %s bps=%s by=%s",
        request_row_id, row["requester_user_id"], row["target_user_id"], bps, operator_user_id,
    )
    return {
        "success": True, "status": STATUS_APPROVED,
        "cost_multiplier_bps": bps,
        "channel_version": governance.get("version"),
        "relationship_id": int(rel["id"]) if rel else None,
    }


def reject_request(
    *, request_row_id: int, decision_note: str, operator_user_id: int,
) -> Dict[str, Any]:
    note = (decision_note or "").strip()
    if len(note) < 2:
        raise ChannelPartnerRequestError("REASON_REQUIRED", "驳回必须填写理由(至少 2 个字)")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE channel_partner_requests
               SET status='rejected', decision_note=%s, decided_by=%s,
                   decided_at=NOW(), updated_at=NOW()
               WHERE id=%s AND status='pending'""",
            (note[:500], int(operator_user_id), int(request_row_id)),
        )
        if cur.rowcount != 1:
            raise ChannelPartnerRequestError(
                "REQUEST_NOT_PENDING", "该申请不存在或已不是待审批状态"
            )
    return {"success": True, "status": STATUS_REJECTED}
