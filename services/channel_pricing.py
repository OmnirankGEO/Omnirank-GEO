"""直属渠道分级价格与收益(§5.1 / P0-7 / §16 Q16-17)

老板 v1.3 拍板启用(总闸 CHANNEL_PRICING_ENABLED · 默认关)。
关键约束:
  - channel_pricing_relationships 是唯一渠道价格 SSOT · admin 管理 · 默认空。
    空 = 扁平"只认第一层"平台直营模型 = 现状(零回归)。不从 referral_links 派生。
  - 同一经销商同一时刻只有一个有效直属渠道(DB 排他索引兜底)。
  - 无环:写入时检测祖先链;运行时 visited set + 深度上限 10(§5.1.6)。
  - 平台永远是在线订单 seller/collector/refund_responsible;渠道账号只是收益受益人。
  - 只认直属(immediate upstream)· 不给间接上级发收益(§5.1.2/§16 Q13)。

红线:整数分;不碰 billing/connection/auth;无税;不放开多级佣金。
"""

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

from db.connection import get_db
from db.xact_lock_guard import require_xact_scope

logger = logging.getLogger("GEO-ChannelPricing")

MAX_DEPTH = 10  # §5.1.6 技术保护上限
MIN_COST_MULTIPLIER_BPS = 10000  # 上游赚差价模型：不得低于其有效成本
_GRAPH_ADVISORY_NAMESPACE = 920716
_GRAPH_ADVISORY_KEY = 1


class ChannelError(Exception):
    """渠道关系解析/写入的可预期错误(调用方转 4xx / 拒绝报价)。"""


def lock_channel_relationship_graph(cur) -> None:
    """Serialize every active channel-graph mutation in the caller transaction."""
    require_xact_scope(cur, where="channel_pricing.lock_channel_relationship_graph")  # §1 硬闸:autocommit 下取事务锁=没锁
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (_GRAPH_ADVISORY_NAMESPACE, _GRAPH_ADVISORY_KEY),
    )


def get_active_relationship(dealer_id: int, cur=None) -> Optional[Dict[str, Any]]:
    """当前经销商的唯一有效直属渠道关系(None = 扁平/平台直营)。"""
    sql = """SELECT * FROM channel_pricing_relationships
             WHERE buyer_dealer_id=%s AND status='active' AND effective_to IS NULL LIMIT 1"""
    if cur is not None:
        cur.execute(sql, (dealer_id,))
        r = cur.fetchone()
        return dict(r) if r else None
    with get_db() as conn:
        c = conn.cursor()
        c.execute(sql, (dealer_id,))
        r = c.fetchone()
        return dict(r) if r else None


def resolve_chain(dealer_id: int, cur=None) -> List[Dict[str, Any]]:
    """从 dealer 向上解析直属链(每跳一个有效关系),返回 [rel...](近→远)。
    visited set 防环 + 深度上限;成环/断链/超深 → ChannelError。空链 = 平台直营根。"""
    chain: List[Dict[str, Any]] = []
    visited = {dealer_id}
    node = dealer_id
    for _ in range(MAX_DEPTH + 1):
        rel = get_active_relationship(node, cur=cur)
        if not rel:
            return chain  # 到根(平台直营)
        up = rel["upstream_channel_account_id"]
        if up in visited:
            raise ChannelError(f"渠道关系成环(node={node} → {up} 已访问)· 拒绝")
        visited.add(up)
        chain.append(rel)
        node = up
    raise ChannelError(f"渠道链超过技术保护深度 {MAX_DEPTH} · 拒绝")


def resolve_chain_snapshot(dealer_id: int, *, cur) -> List[Dict[str, Any]]:
    """Return a transaction-stable, validated relationship path (near → far).

    Relationship writers take the exclusive form of ``920716/1``. Quotes and
    order reservations take this shared form, so the complete path and every
    ``relationship_version`` remain stable until their transaction commits.
    """
    require_xact_scope(cur, where="channel_pricing.resolve_chain_snapshot")  # §1 硬闸:autocommit 下取事务锁=没锁
    cur.execute(
        "SELECT pg_advisory_xact_lock_shared(%s, %s)",
        (_GRAPH_ADVISORY_NAMESPACE, _GRAPH_ADVISORY_KEY),
    )
    chain = resolve_chain(int(dealer_id), cur=cur)
    actor_ids = {int(dealer_id)}
    for rel in chain:
        actor_ids.add(int(rel["buyer_dealer_id"]))
        actor_ids.add(int(rel["upstream_channel_account_id"]))
        if int(rel.get("cost_multiplier_bps") or 0) < MIN_COST_MULTIPLIER_BPS:
            raise ChannelError(
                f"渠道关系 {rel.get('id')} 的 cost_multiplier_bps 低于 10000 · 拒绝"
            )
        if not str(rel.get("relationship_version") or "").strip():
            raise ChannelError(f"渠道关系 {rel.get('id')} 缺少 relationship_version · 拒绝")
    invalid_actors = _validate_actor_nodes(cur, actor_ids, lock=True)
    if invalid_actors:
        raise ChannelError(
            "渠道链账号不存在/停用/非服务商: " + ",".join(map(str, invalid_actors))
        )
    return [
        {
            "relationship_id": int(rel["id"]),
            "buyer_dealer_id": int(rel["buyer_dealer_id"]),
            "upstream_channel_account_id": int(rel["upstream_channel_account_id"]),
            "cost_multiplier_bps": int(rel["cost_multiplier_bps"]),
            "relationship_version": str(rel["relationship_version"]),
        }
        for rel in chain
    ]


def resolve_effective_cost_basis(
    dealer_id: int, platform_base_cents: int, cur=None
) -> Dict[str, Any]:
    """计算该经销商的有效进货成本基准(§5.1)。

    根经销商(无直属关系): effective = platform_base × 平台合同系数(此处平台系数在报价层单独乘,
      这里返回 platform_base 作为根成本基准 + beneficiary=None)。
    有直属渠道: effective = 上游有效成本基准 × 该关系 cost_multiplier;beneficiary=直属上游。

    返回 {effective_cost_cents, beneficiary_user_id, relationship_version, depth, chain}。
    """
    if cur is None:
        with get_db() as conn:
            return resolve_effective_cost_basis(
                dealer_id, platform_base_cents, cur=conn.cursor()
            )

    # Relationship writers use the exclusive form of the same transaction lock.
    # Holding the shared form keeps the graph stable for the entire quote transaction.
    require_xact_scope(cur, where="channel_pricing.resolve_effective_cost_basis")  # §1 硬闸:autocommit 下取事务锁=没锁
    cur.execute(
        "SELECT pg_advisory_xact_lock_shared(%s, %s)",
        (_GRAPH_ADVISORY_NAMESPACE, _GRAPH_ADVISORY_KEY),
    )
    chain = resolve_chain(dealer_id, cur=cur)
    actor_ids = {int(dealer_id)}
    for rel in chain:
        actor_ids.add(int(rel["buyer_dealer_id"]))
        actor_ids.add(int(rel["upstream_channel_account_id"]))
    invalid_actors = _validate_actor_nodes(cur, actor_ids, lock=True)
    if invalid_actors:
        raise ChannelError(
            "渠道链账号不存在/停用/非服务商: " + ",".join(map(str, invalid_actors))
        )
    if not chain:
        return {"effective_cost_cents": int(platform_base_cents), "beneficiary_user_id": None,
                "relationship_version": None, "depth": 0, "chain": []}
    # 从根往下复利:根成本 = platform_base;逐级 × cost_multiplier_bps
    # [审核 P2] 纯整数天花板除法(不经 float · 大额精确)· 每跳只取整一次
    cost = int(platform_base_cents)
    for rel in reversed(chain):  # 远→近
        multiplier = int(rel["cost_multiplier_bps"])
        if multiplier < MIN_COST_MULTIPLIER_BPS:
            raise ChannelError(
                f"渠道关系 {rel['id']} 的 cost_multiplier_bps 低于 10000 · 拒绝报价"
            )
        cost = (cost * multiplier + 9999) // 10000
    immediate = chain[0]
    return {
        "effective_cost_cents": cost,
        "beneficiary_user_id": immediate["upstream_channel_account_id"],
        "relationship_version": immediate["relationship_version"],
        "depth": len(chain),
        "chain": chain,
    }


def _max_downstream_hops(cur, node: int, cap: int) -> int:
    """从任一后代向上到达 node 的最长链长(node 为叶端时 0)。用于新增边前判总链深。"""
    if cap <= 0:
        return cap  # 触底视为超深
    cur.execute(
        """SELECT buyer_dealer_id FROM channel_pricing_relationships
           WHERE upstream_channel_account_id=%s AND status='active' AND effective_to IS NULL""",
        (node,),
    )
    children = [r["buyer_dealer_id"] for r in cur.fetchall()]
    if not children:
        return 0
    return 1 + max(_max_downstream_hops(cur, c, cap - 1) for c in children)


def _would_create_cycle(cur, buyer_dealer_id: int, upstream_id: int) -> bool:
    """写入前检测:从 upstream 往上是否能到达 buyer_dealer_id(会成环)。"""
    if buyer_dealer_id == upstream_id:
        return True
    visited = {upstream_id}
    node = upstream_id
    for _ in range(MAX_DEPTH + 1):
        rel = get_active_relationship(node, cur=cur)
        if not rel:
            return False
        up = rel["upstream_channel_account_id"]
        if up == buyer_dealer_id:
            return True
        if up in visited:
            return False  # 既有环由既有数据负责,这里只关心新增边
        visited.add(up)
        node = up
    return True  # 超深视为不安全


def _active_graph(cur, *, for_update: bool = False) -> Dict[int, Dict[str, Any]]:
    suffix = " FOR UPDATE" if for_update else ""
    cur.execute(
        """SELECT * FROM channel_pricing_relationships
           WHERE status='active' AND effective_to IS NULL
           ORDER BY buyer_dealer_id""" + suffix
    )
    graph: Dict[int, Dict[str, Any]] = {}
    for raw in cur.fetchall():
        row = dict(raw)
        buyer = int(row["buyer_dealer_id"])
        if buyer in graph:
            raise ChannelError(f"经销商 {buyer} 存在重复直属上游 · 拒绝")
        graph[buyer] = row
    return graph


def _validate_actor_nodes(cur, node_ids: Iterable[int], *, lock: bool = False) -> List[int]:
    ids = sorted({int(value) for value in node_ids})
    if not ids:
        return []
    lock_clause = " FOR SHARE OF u, w" if lock else ""
    cur.execute(
        """
        SELECT u.id
        FROM users u
        JOIN user_wallets w ON w.user_id=u.id
        WHERE u.id = ANY(%s) AND COALESCE(u.is_active, 1)=1 AND COALESCE(w.agent_level, 0)>=1
        ORDER BY u.id
        """ + lock_clause,
        (ids,),
    )
    found = {int(row["id"]) for row in cur.fetchall()}
    return sorted(set(ids).difference(found))


def _validate_graph(graph: Dict[int, Dict[str, Any]]) -> List[str]:
    """验证最终单父图；返回全部人话 blocker，不读取任何推断关系。"""
    blockers: List[str] = []
    for buyer, rel in sorted(graph.items()):
        upstream = int(rel["upstream_channel_account_id"])
        multiplier = int(rel.get("cost_multiplier_bps") or 0)
        if multiplier < MIN_COST_MULTIPLIER_BPS:
            blockers.append(
                f"经销商 {buyer} 的渠道系数 {multiplier} 低于 10000"
            )
        if buyer == upstream:
            blockers.append(f"经销商 {buyer} 不能归属自己")
            continue
        seen = {buyer}
        node = buyer
        depth = 0
        while node in graph:
            next_node = int(graph[node]["upstream_channel_account_id"])
            depth += 1
            if next_node in seen:
                blockers.append(f"渠道关系成环（从 {buyer} 经 {node} 到 {next_node}）")
                break
            if depth > MAX_DEPTH:
                blockers.append(f"经销商 {buyer} 的渠道链超过 {MAX_DEPTH} 层")
                break
            seen.add(next_node)
            node = next_node
    return list(dict.fromkeys(blockers))


def _normalize_change(raw: Dict[str, Any]) -> Dict[str, Any]:
    change = {
        "buyer_dealer_id": int(raw["buyer_dealer_id"]),
        "upstream_channel_account_id": int(raw["upstream_channel_account_id"]),
        "expected_relationship_version": (
            str(raw.get("expected_relationship_version")).strip()
            if raw.get("expected_relationship_version") is not None else None
        ),
        "new_relationship_version": str(
            raw.get("new_relationship_version") or raw.get("relationship_version") or ""
        ).strip(),
        "cost_multiplier_bps": int(raw.get("cost_multiplier_bps", 10000)),
        "reason": str(raw.get("reason") or "").strip(),
    }
    if not change["new_relationship_version"]:
        raise ChannelError("new_relationship_version 不能为空")
    if change["cost_multiplier_bps"] < MIN_COST_MULTIPLIER_BPS:
        raise ChannelError("cost_multiplier_bps 必须 >= 10000")
    if change["buyer_dealer_id"] == change["upstream_channel_account_id"]:
        raise ChannelError("禁止自己归属自己")
    return change


def _plan_relationship_changes(
    cur, changes: Sequence[Dict[str, Any]], *, lock: bool, validate_nodes: bool = True,
) -> Dict[str, Any]:
    normalized = [_normalize_change(dict(raw)) for raw in changes]
    if not normalized:
        raise ChannelError("关系清单不能为空")
    buyers = [item["buyer_dealer_id"] for item in normalized]
    if len(set(buyers)) != len(buyers):
        raise ChannelError("同一批次不能为同一经销商提交多个直属上游")

    graph = _active_graph(cur, for_update=lock)

    planned: List[Dict[str, Any]] = []
    final_graph = {buyer: dict(rel) for buyer, rel in graph.items()}
    for item in normalized:
        buyer = item["buyer_dealer_id"]
        current = graph.get(buyer)
        expected = item["expected_relationship_version"]
        if current:
            current_version = str(current["relationship_version"])
            exact_retry = (
                current_version == item["new_relationship_version"]
                and int(current["upstream_channel_account_id"]) == item["upstream_channel_account_id"]
                and int(current["cost_multiplier_bps"]) == item["cost_multiplier_bps"]
            )
            if exact_retry:
                planned.append({**item, "action": "unchanged", "current": current})
                continue
            if expected != current_version:
                raise ChannelError(
                    f"经销商 {buyer} 关系版本已变化（expected={expected!r}, current={current_version!r}）"
                )
        elif expected not in (None, ""):
            raise ChannelError(f"经销商 {buyer} 当前无关系，expected_relationship_version 必须为空")

        cur.execute(
            """SELECT * FROM channel_pricing_relationships
               WHERE buyer_dealer_id=%s AND relationship_version=%s LIMIT 1""",
            (buyer, item["new_relationship_version"]),
        )
        reused = cur.fetchone()
        if reused:
            raise ChannelError(
                f"经销商 {buyer} 的关系版本 {item['new_relationship_version']} 已使用，拒绝重放"
            )
        final_graph[buyer] = {
            "buyer_dealer_id": buyer,
            "upstream_channel_account_id": item["upstream_channel_account_id"],
            "relationship_version": item["new_relationship_version"],
            "cost_multiplier_bps": item["cost_multiplier_bps"],
        }
        planned.append({**item, "action": "create", "current": current})

    if validate_nodes:
        final_actor_ids = [
            value
            for buyer, rel in final_graph.items()
            for value in (buyer, int(rel["upstream_channel_account_id"]))
        ]
        orphan_ids = _validate_actor_nodes(cur, final_actor_ids, lock=lock)
        if orphan_ids:
            raise ChannelError("账号不存在/停用/非服务商: " + ",".join(map(str, orphan_ids)))

    blockers = _validate_graph(final_graph)
    if blockers:
        raise ChannelError("；".join(blockers))
    return {
        "changes": planned,
        "active_before": len(graph),
        "active_after": len(final_graph),
        "blockers": [],
    }


def dry_run_relationships(changes: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """admin dry-run：只读最终图验证，零关系写入。"""
    with get_db() as conn:
        cur = conn.cursor()
        lock_channel_relationship_graph(cur)
        plan = _plan_relationship_changes(cur, changes, lock=False)
        return {
            "valid": True,
            "active_before": plan["active_before"],
            "active_after": plan["active_after"],
            "changes": [
                {key: value for key, value in item.items() if key != "current"}
                for item in plan["changes"]
            ],
            "blockers": [],
        }


def save_relationships(
    changes: Sequence[Dict[str, Any]], *, approved_by: Optional[int] = None,
    created_by: Optional[int] = None, validate_nodes: bool = True,
) -> Dict[str, Any]:
    """显式单条/批量保存；全图锁 + OCC + 一事务 all-or-nothing。"""
    with get_db() as conn:
        return save_relationships_cur(
            conn.cursor(), changes, approved_by=approved_by, created_by=created_by,
            validate_nodes=validate_nodes,
        )


def save_relationships_cur(
    cur,
    changes: Sequence[Dict[str, Any]],
    *,
    approved_by: Optional[int] = None,
    created_by: Optional[int] = None,
    validate_nodes: bool = True,
) -> Dict[str, Any]:
    """Write relationships inside the caller's transaction.

    Admin governance uses this entry point so the relationship, CAS version and
    immutable audit evidence commit together. The graph lock remains identical
    to the standalone writer.
    """
    lock_channel_relationship_graph(cur)
    plan = _plan_relationship_changes(cur, changes, lock=True, validate_nodes=validate_nodes)
    changed = [item for item in plan["changes"] if item["action"] == "create"]
    if changed:
        cur.execute(
            """UPDATE channel_pricing_relationships
               SET status='archived', effective_to=NOW(), archived_at=NOW()
               WHERE buyer_dealer_id=ANY(%s) AND status='active' AND effective_to IS NULL""",
            ([item["buyer_dealer_id"] for item in changed],),
        )
    results: List[Dict[str, Any]] = []
    for item in plan["changes"]:
        if item["action"] == "unchanged":
            results.append({**dict(item["current"]), "idempotent": True})
            continue
        cur.execute(
            """INSERT INTO channel_pricing_relationships
               (buyer_dealer_id, upstream_channel_account_id, relationship_version,
                cost_multiplier_bps, reason, approved_by, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                item["buyer_dealer_id"], item["upstream_channel_account_id"],
                item["new_relationship_version"], item["cost_multiplier_bps"],
                item["reason"], approved_by, created_by,
            ),
        )
        results.append({**dict(cur.fetchone()), "idempotent": False})
    return {"relationships": results, "changed_count": len(changed)}


def archive_relationship_cur(
    cur,
    buyer_dealer_id: int,
    *,
    expected_relationship_version: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Archive the active relationship inside the caller's transaction."""
    lock_channel_relationship_graph(cur)
    current = get_active_relationship(int(buyer_dealer_id), cur=cur)
    if not current:
        return None
    if (
        expected_relationship_version is not None
        and str(current["relationship_version"]) != str(expected_relationship_version)
    ):
        raise ChannelError("渠道关系版本已变化，请刷新后重试")
    cur.execute(
        """UPDATE channel_pricing_relationships
           SET status='archived', effective_to=NOW(), archived_at=NOW()
           WHERE id=%s AND status='active' AND effective_to IS NULL
           RETURNING *""",
        (int(current["id"]),),
    )
    archived = cur.fetchone()
    if not archived:
        raise ChannelError("渠道关系已被其他操作修改，请刷新后重试")
    return dict(archived)


def relationship_status(cur=None) -> Dict[str, Any]:
    """readiness 使用的只读全图/版本/孤儿诊断。"""
    if cur is None:
        with get_db() as conn:
            return relationship_status(cur=conn.cursor())

    blockers: List[str] = []
    try:
        graph = _active_graph(cur)
    except ChannelError as exc:
        graph = {}
        blockers.append(str(exc))
    cur.execute(
        """
        SELECT buyer_dealer_id, COUNT(*) AS c
        FROM channel_pricing_relationships
        WHERE status='active' AND effective_to IS NULL
        GROUP BY buyer_dealer_id HAVING COUNT(*) > 1
        """
    )
    duplicate_buyers = [int(row["buyer_dealer_id"]) for row in cur.fetchall()]
    if duplicate_buyers:
        blockers.append("重复直属上游账号: " + ",".join(map(str, duplicate_buyers)))
    cur.execute(
        """SELECT id FROM channel_pricing_relationships
           WHERE status='active' AND effective_to IS NULL
             AND BTRIM(COALESCE(relationship_version,''))=''"""
    )
    invalid_versions = [int(row["id"]) for row in cur.fetchall()]
    if invalid_versions:
        blockers.append("关系版本为空: " + ",".join(map(str, invalid_versions)))
    cur.execute(
        """SELECT id FROM channel_pricing_relationships
           WHERE status='active' AND effective_to IS NULL
             AND cost_multiplier_bps < %s ORDER BY id""",
        (MIN_COST_MULTIPLIER_BPS,),
    )
    invalid_multipliers = [int(row["id"]) for row in cur.fetchall()]
    if invalid_multipliers:
        blockers.append(
            "活动渠道关系 cost_multiplier_bps 低于 10000: "
            + ",".join(map(str, invalid_multipliers))
        )
    node_ids = [value for buyer, rel in graph.items() for value in (
        buyer, int(rel["upstream_channel_account_id"])
    )]
    orphan_ids = _validate_actor_nodes(cur, node_ids)
    if orphan_ids:
        blockers.append("关系存在孤儿/停用/非服务商账号: " + ",".join(map(str, orphan_ids)))
    blockers.extend(_validate_graph(graph))
    blockers = list(dict.fromkeys(blockers))
    return {
        "ready": not blockers,
        "active_relationship_count": len(graph),
        "duplicate_buyer_ids": duplicate_buyers,
        "invalid_version_ids": invalid_versions,
        "invalid_multiplier_relationship_ids": invalid_multipliers,
        "orphan_user_ids": orphan_ids,
        "blockers": blockers,
    }


def create_relationship(
    *, buyer_dealer_id: int, upstream_channel_account_id: int, relationship_version: str,
    cost_multiplier_bps: int = 10000, reason: str = "", approved_by: Optional[int] = None,
    created_by: Optional[int] = None,
) -> Dict[str, Any]:
    """兼容旧内部调用；新 admin API 必须显式传 expected version 到 save_relationships。"""
    with get_db() as conn:
        cur = conn.cursor()
        current = get_active_relationship(buyer_dealer_id, cur=cur)
    result = save_relationships([{
        "buyer_dealer_id": buyer_dealer_id,
        "upstream_channel_account_id": upstream_channel_account_id,
        "expected_relationship_version": current.get("relationship_version") if current else None,
        "new_relationship_version": relationship_version,
        "cost_multiplier_bps": cost_multiplier_bps,
        "reason": reason,
    }], approved_by=approved_by, created_by=created_by, validate_nodes=False)
    return result["relationships"][0]


def archive_relationship(buyer_dealer_id: int, expected_relationship_version: Optional[str] = None) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (_GRAPH_ADVISORY_NAMESPACE, _GRAPH_ADVISORY_KEY),
        )
        current = get_active_relationship(buyer_dealer_id, cur=cur)
        if not current:
            return 0
        if expected_relationship_version is not None and str(current["relationship_version"]) != str(expected_relationship_version):
            raise ChannelError("关系版本已变化，拒绝归档")
        cur.execute(
            """UPDATE channel_pricing_relationships
               SET status='archived', effective_to=NOW(), archived_at=NOW()
               WHERE buyer_dealer_id=%s AND status='active' AND effective_to IS NULL""",
            (buyer_dealer_id,),
        )
        return cur.rowcount


def record_channel_revenue(
    cur, *, recharge_order_id: str, buyer_dealer_id: int, beneficiary_user_id: int,
    upstream_cost_basis_cents: int, buyer_paid_cents: int, relationship_version: Optional[str] = None,
    price_quote_id: Optional[str] = None, catalog_version: Optional[str] = None,
) -> Optional[int]:
    """写直属渠道收益台账(须在下单主事务内)。UNIQUE(order_id) 幂等:重复 → 跳过返回 None。
    channel_revenue = buyer_paid − upstream_cost_basis；允许零差价，禁止负收益。"""
    revenue = int(buyer_paid_cents) - int(upstream_cost_basis_cents)
    if revenue < 0:
        raise ChannelError("买方实付低于上游成本 · 拒绝记录负渠道收益")
    cur.execute(
        """INSERT INTO channel_revenue_ledger
           (channel_beneficiary_user_id, buyer_dealer_id, recharge_order_id, relationship_version,
            price_quote_id, catalog_version, upstream_cost_basis_cents, buyer_paid_cents,
            channel_revenue_cents, platform_seller, status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'PLATFORM','recorded')
           ON CONFLICT (recharge_order_id) DO NOTHING
           RETURNING id""",
        (beneficiary_user_id, buyer_dealer_id, recharge_order_id, relationship_version,
         price_quote_id, catalog_version, int(upstream_cost_basis_cents), int(buyer_paid_cents), revenue),
    )
    row = cur.fetchone()
    return row["id"] if row else None


def reverse_channel_revenue(cur, recharge_order_id: str) -> int:
    """退款/撤单时冲销渠道收益(幂等)。"""
    cur.execute(
        """UPDATE channel_revenue_ledger SET status='reversed', reversed_at=NOW()
           WHERE recharge_order_id=%s AND status='recorded'""",
        (recharge_order_id,),
    )
    return cur.rowcount
