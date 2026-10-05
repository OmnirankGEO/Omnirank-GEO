"""
发布指标 · 现役链路 = 媒介盒子代发(mhz_*),不是已废弃的 publish_orders。

真实链路(逐处核实):
  - api/publish_api.py:912 明确 /api/publish/orders + publish_orders/publish_order_items
    已于 2026-04-30 废弃(返 410),不再当发布指标主口径。
  - 现役代发走 /api/meijiehezi/publish;本地下单记录 = mhz_publish_orders /
    mhz_publish_order_items(status TEXT · 在途);真实分发状态同步回 mhz_synced_orders。
  - mhz_synced_orders.status INTEGER 语义(api/meijiehezi_api.py:1098):
    -2 撤回 / -1 拒稿 / 0 待接单 / 1 发布中 / 2 已完成。created_at 可空 → COALESCE(created_at, synced_at)。

主指标 = mhz_synced_orders(真实同步状态分布);辅 = 本地在途 items;legacy = 老 publish_orders(仅标注)。
只读。子指标独立失败。
"""

from services.ai_ops.report_metrics import open_conn, sub_metric

# mhz_synced_orders.status(整数)→ 人话
_MHZ_STATUS_LABEL = {
    -2: "已撤回", -1: "已拒稿", 0: "待接单", 1: "发布中", 2: "已完成",
}


def collect(report_date) -> dict:
    conn = open_conn()
    try:
        cur = conn.cursor()

        def mhz_synced():
            # 现役代发主口径:今日同步订单数 + 真实状态分布(按 created_at,缺则 synced_at 归日)
            cur.execute(
                """
                SELECT status, COUNT(*) AS cnt
                FROM mhz_synced_orders
                WHERE COALESCE(created_at, synced_at)::date = %s
                GROUP BY status
                """, (report_date,))
            rows = [dict(r) for r in cur.fetchall()]
            by_status = {}
            total = 0
            for r in rows:
                code = int(r["status"]) if r["status"] is not None else 0
                cnt = int(r["cnt"])
                total += cnt
                label = _MHZ_STATUS_LABEL.get(code, f"未知({code})")
                by_status[label] = by_status.get(label, 0) + cnt
            completed = by_status.get("已完成", 0)
            rejected = by_status.get("已拒稿", 0)
            in_progress = by_status.get("待接单", 0) + by_status.get("发布中", 0)
            return {
                "today_orders": total,
                "by_status": by_status,
                "today_completed": completed,
                "today_rejected": rejected,
                "today_in_progress": in_progress,
            }

        def mhz_local_inflight():
            # 本地已下单但尚未同步/完成的 item(status TEXT · 反映"提交了还没落地")
            cur.execute(
                """
                SELECT status, COUNT(*) AS cnt
                FROM mhz_publish_order_items
                WHERE created_at::date = %s
                GROUP BY status
                """, (report_date,))
            by_status = {str(r["status"]): int(r["cnt"]) for r in cur.fetchall()}
            return {"today_items": sum(by_status.values()), "by_status": by_status}

        def legacy_publish_orders():
            # 老链路(2026-04-30 废弃)· 仅作 legacy 参考,不当主发布指标
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM publish_orders WHERE created_at::date = %s",
                (report_date,))
            row = dict(cur.fetchone() or {})
            return {"today_orders": int(row.get("cnt") or 0), "note": "老链路已于 2026-04-30 废弃,仅参考"}

        return {
            "mhz_synced": sub_metric(conn, mhz_synced),           # 主口径
            "mhz_local_inflight": sub_metric(conn, mhz_local_inflight),
            "legacy_publish_orders": sub_metric(conn, legacy_publish_orders),
        }
    finally:
        conn.close()
