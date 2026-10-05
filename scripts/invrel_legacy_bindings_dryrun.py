#!/usr/bin/env python3
"""存量 5 条关系 · 逐条差分报告(工单 v3 §P0-4 / §4.4)· **默认且本轮只有 dry-run**。

🔴 v3 定性(推翻 v2):这 5 条记录的是**真实存在过的商业关系**,客户侧后来升级为服务商。
   按 R1「关系不因身份变化失效」——**不删除、不退休、不"审计退休"**。
   本工具**永远不删除任何关系行**,连删除的代码路径都不存在。

目标固定为 binding 1 / 10 / 11 / 38 / 67(工单 §1.4 表),不接受任意 id 参数 ——
窄范围是刻意的:一个能对任意 binding 跑的工具迟早会被对着别的行跑。

用法(只读):
    python scripts/invrel_legacy_bindings_dryrun.py            # 文本报告
    python scripts/invrel_legacy_bindings_dryrun.py --json     # 机器可读差分

🔴 `--apply` 本轮**不可用**,且是**故意**的 —— 见 `_refuse_apply()` 里的理由:
   工单让复用 `close_current_version` 补历史,但那个原语在补完快照后会
   `UPDATE ... SET effective_to=NOW()`,即把这条关系标记为**已结束**。
   而这 5 条关系按 R1 **仍然有效**,盖上结束时间戳就是伪造历史。
   补历史的写法需要 Owner/Review 先裁定,不由执行方自行发明(§9 第 9 条)。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 工单 §1.4 的五条,写死。
TARGET_BINDING_IDS = (1, 10, 11, 38, 67)

# 生产取证基线(工单 §7 要求校验)。用 `state.sh` 现取值核对,不信文档快照。
EXPECTED_PROD_SHA = "00466fd7de9bdc5dfc74a708de2d01005c544b34"


def _rows(cur, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    cur.execute(sql, params)
    return [dict(r) for r in (cur.fetchall() or [])]


def _one(cur, sql: str, params: tuple = ()) -> Dict[str, Any]:
    got = _rows(cur, sql, params)
    return got[0] if got else {}


def collect(cur) -> List[Dict[str, Any]]:
    """逐条采集:绑定快照 / 当前身份 / 历史版本计数 / active 渠道关系 / 三类流水汇总。

    🔴 全程只读。锁的问题在这里不存在 —— dry-run 不 `FOR UPDATE`:
       2026-08-10 有过"只读探针里的未提交语句把生产锁队列堵死 16 分钟"的前科,
       对着生产跑的报告工具**一个锁都不该拿**。锁只属于 `--apply`(未启用)。
    """
    out: List[Dict[str, Any]] = []
    for binding_id in TARGET_BINDING_IDS:
        binding = _one(
            cur,
            "SELECT id, customer_user_id, agent_user_id, binding_source, source_token, "
            "bound_at, dispute_status, dispute_note FROM customer_agent_bindings WHERE id=%s",
            (binding_id,),
        )
        if not binding:
            out.append({"binding_id": binding_id, "found": False,
                        "note": "该 binding 已不存在 —— 本工具不创建、不推测,如实报告"})
            continue

        customer = int(binding["customer_user_id"])
        agent = int(binding["agent_user_id"])

        identity = _one(
            cur,
            "SELECT COALESCE(w.agent_level,0) AS agent_level, "
            "       COALESCE(NULLIF(u.display_name,''), u.username) AS name "
            "FROM users u LEFT JOIN user_wallets w ON w.user_id=u.id WHERE u.id=%s",
            (customer,),
        )
        history_count = _one(
            cur,
            "SELECT COUNT(*) AS n FROM customer_agent_binding_history WHERE customer_user_id=%s",
            (customer,),
        ).get("n", 0)
        # 同一对人的 active 渠道关系(有向:buyer=客户侧 / upstream=服务商侧)
        channel = _one(
            cur,
            "SELECT id, cost_multiplier_bps, relationship_version, effective_from "
            "FROM channel_pricing_relationships "
            "WHERE buyer_dealer_id=%s AND upstream_channel_account_id=%s "
            "  AND status='active' AND effective_to IS NULL",
            (customer, agent),
        )
        inv = _one(
            cur,
            "SELECT COUNT(*) AS n, COALESCE(SUM(points),0) AS total, "
            "       COALESCE(array_agg(id ORDER BY id), '{}') AS ids "
            "FROM agent_inventory_transactions "
            "WHERE agent_user_id=%s AND related_customer_user_id=%s",
            (agent, customer),
        )
        pts = _one(
            cur,
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS total "
            "FROM point_transactions WHERE user_id=%s AND type='agent_grant'",
            (customer,),
        )
        orders = _one(
            cur,
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents),0) AS total_cents "
            "FROM recharge_orders WHERE user_id=%s",
            (customer,),
        )

        is_provider = int(identity.get("agent_level") or 0) >= 1
        out.append({
            "binding_id": binding_id,
            "found": True,
            "customer_user_id": customer,
            "customer_name": identity.get("name"),
            "agent_user_id": agent,
            "binding_source": binding["binding_source"],
            "dispute_status": binding.get("dispute_status"),
            "bound_at": str(binding.get("bound_at")),
            "customer_is_service_provider": is_provider,
            "history_version_rows": int(history_count or 0),
            "active_channel_relationship": channel or None,
            "inventory_tx": {"count": int(inv.get("n") or 0), "points_sum": int(inv.get("total") or 0),
                             "ids": list(inv.get("ids") or [])},
            "point_tx_agent_grant": {"count": int(pts.get("n") or 0), "points_sum": int(pts.get("total") or 0)},
            "recharge_orders": {"count": int(orders.get("n") or 0), "amount_cents_sum": int(orders.get("total_cents") or 0)},
            "planned_action": _planned_action(is_provider, int(history_count or 0), bool(channel)),
            # 🔴 无论 dry-run 还是将来的 apply,这三件事都必须为真。写进报告供复审机械核对。
            "invariants": {
                "relationship_rows_deleted": 0,
                "historical_ledger_rows_modified": 0,
                "balances_modified": 0,
            },
        })
    return out


def _planned_action(is_provider: bool, history_rows: int, has_channel: bool) -> str:
    if not is_provider:
        return "无需处理:客户侧当前仍是普通用户,关系形态与账本路径一致"
    parts = ["保留关系(R1)"]
    if history_rows == 0:
        parts.append("补写关系沿革快照(缺历史版本行)")
    parts.append("写一条标注审计:该对人同时存在服务归属"
                 + ("与渠道关系" if has_channel else "但无 active 渠道关系"))
    return " · ".join(parts)


def render(report: List[Dict[str, Any]]) -> str:
    lines = [
        "存量 5 条关系 · dry-run 差分报告(工单 §P0-4)",
        f"生产取证基线(期望):{EXPECTED_PROD_SHA}",
        "🔴 本次为 **dry-run**:未加锁、未写入、未删除任何行。",
        "",
    ]
    for r in report:
        lines.append("=" * 68)
        if not r.get("found"):
            lines.append(f"binding {r['binding_id']}:{r['note']}")
            continue
        ch = r["active_channel_relationship"]
        lines += [
            f"binding {r['binding_id']}  客户 u{r['customer_user_id']}({r['customer_name']}) "
            f"← 服务商 u{r['agent_user_id']}",
            f"  绑定来源      : {r['binding_source']} · 建立于 {r['bound_at']}",
            f"  客户当前身份  : {'服务商' if r['customer_is_service_provider'] else '普通用户'}",
            f"  关系历史版本  : {r['history_version_rows']} 行",
            f"  active 渠道关系: " + (
                f"id={ch['id']} · 已绑定系数 {ch['cost_multiplier_bps']}bps · 版本 {ch['relationship_version']}"
                if ch else "无"
            ),
            f"  库存流水      : {r['inventory_tx']['count']} 笔 · 合计 {r['inventory_tx']['points_sum']} "
            f"· ids={r['inventory_tx']['ids']}",
            f"  客户入账流水  : {r['point_tx_agent_grant']['count']} 笔 · 合计 {r['point_tx_agent_grant']['points_sum']}",
            f"  充值订单      : {r['recharge_orders']['count']} 笔 · 合计 {r['recharge_orders']['amount_cents_sum']} 分",
            f"  计划动作      : {r['planned_action']}",
            "  预期差分      : 关系行 0 删除 / 历史流水 0 改写 / 余额 0 变化",
        ]
        if r["binding_id"] == 67:
            lines.append(
                "  ⚠️ 附注(工单 §1.5):tx 86/87 共 8124 算力走了**客户账本路径**"
                "(进可用算力而非库存算力),属历史事实。关系有效且保留;"
                "资金处理另案,不在本工单。"
            )
    lines += ["=" * 68, "", "🔴 未执行任何写操作。`--apply` 本轮不可用,理由见 --apply 提示。"]
    return "\n".join(lines)


def _refuse_apply() -> int:
    print(
        "🔴 `--apply` 本轮不可用,且这是**故意**的:\n"
        "\n"
        "  工单 §P0-4 让复用 `services/commercial_binding_history.close_current_version`\n"
        "  来「补写关系沿革快照(只补历史,不关闭当前投影)」。\n"
        "  但读实现(该文件 :59-63)发现:它在补完快照后会执行\n"
        "      UPDATE customer_agent_binding_history SET effective_to=NOW() ...\n"
        "  即把这条关系历史标记为**已结束**。\n"
        "\n"
        "  而这 5 条关系按 R1 **仍然有效**。给一条有效关系盖上结束时间戳 = 伪造历史,\n"
        "  与 §1.8「禁止伪造『当时已有审计』的历史记录」直接冲突。\n"
        "\n"
        "  → 补历史到底该用哪个写法,需要 Owner / Review 先裁定(§9 第 9 条:\n"
        "    Owner 未拍板的处置方式不由执行方自行发明)。裁定前本工具只出报告。\n",
        file=sys.stderr,
    )
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="存量 5 条关系 dry-run 差分报告")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--apply", action="store_true", help="(本轮不可用,见提示)")
    args = parser.parse_args()

    if args.apply:
        return _refuse_apply()

    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        # 只读事务:即便将来有人往 collect() 里加了写语句,数据库层也会当场拒绝。
        cur.execute("SET TRANSACTION READ ONLY")
        report = collect(cur)
        conn.rollback()

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str) if args.json
          else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
