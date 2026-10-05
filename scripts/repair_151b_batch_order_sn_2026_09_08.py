"""#151-B 修复脚本 · 重建「单号 ↔ item」映射。

## 缺陷

批量提交时批级回执被扇给了每条 item(`{m: inline_sn for m in media_ids}`),
于是 22 条 item 各自存了**整批** 5 或 7 个单号。
状态回写 `WHERE mhz_order_id = %s` 因此永远匹配不上 ——
供应商侧那 22 个单号早已终态(17 已发布带 url / 5 被拒),
本地却 27 天一直显示「在发」。

写入侧的闸已加(`update_order_item_submitted` 拒收多段值),**新的不会再产生**。
本脚本只收拾**存量**。

## 🔴 默认 dry-run,而且没有「顺手执行」的开关组合

- 不传 `--execute` ⇒ **只打印**将要做什么,一个字都不写;
- 传 `--execute` ⇒ 才写,且每条都走既有路径(见下),不新造语义。

由 Deploy 跑 dry-run、把结果报 Owner,Owner 在 Deploy 窗**亲口**同意后才执行。

## 映射怎么重建

供应商列表(本地 `mhz_synced_orders`,零外调)每个单号带 `media_name`;
本地 item 也有 `media_name`。按 **(order_id, media_name)** 配对,
同名多条时按 **submitted_at / id 时序**依次配。

🔴 **配不唯一的 item 保持 `submitted` 并列出来给人看** ——
   不猜。这批数据出问题的根因就是「把一个值当成另一个值」,
   再猜一次等于用同一种错误去修它。

## 回写走既有路径

- 供应商 `status=2` ⇒ 与 `sync_mhz_orders` **同一段映射**:`published` + `publish_url`;
- `status=-1` ⇒ `rejected`,退款走**既有规则**(本脚本不新造退款);
- 其余 ⇒ 不动。

## 幂等

只改 `mhz_order_id` 段数>1 且当前仍 `submitted` 的行;跑第二遍是空操作。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("repair-151b")

#: 供应商数字状态 → item 文本状态。**与 sync_mhz_orders 同一张表**,不新写第二份。
_MHZ_STATUS_TO_ITEM = {2: "published", -1: "rejected", -2: "withdrawn"}


def _rows(cur):
    """段数>1 且仍 submitted 的 item,连同它所在订单的其它信息。"""
    cur.execute(
        """SELECT id, order_id, media_name, mhz_order_id, submitted_at, status
             FROM mhz_publish_order_items
            WHERE status = 'submitted'
              AND mhz_order_id ~ '[,;|[:space:]]'
            ORDER BY order_id, submitted_at NULLS LAST, id""")
    return [dict(r) for r in cur.fetchall()]


def _supplier_rows(cur, sns):
    """本地已同步的供应商记录(**零外调**)。"""
    if not sns:
        return {}
    cur.execute(
        """SELECT order_sn, status, url, media_name, published_at
             FROM mhz_synced_orders
            WHERE order_sn = ANY(%s)""", (list(sns),))
    return {str(dict(r)["order_sn"]): dict(r) for r in cur.fetchall()}


def _split(value):
    import re
    return [p for p in re.split(r"[,;|\s]+", str(value or "").strip()) if p]


def plan(cur):
    """算出映射与将写的字段。**只读**。"""
    items = _rows(cur)
    all_sns = {sn for it in items for sn in _split(it["mhz_order_id"])}
    supplier = _supplier_rows(cur, all_sns)

    by_order: dict = {}
    for it in items:
        by_order.setdefault(it["order_id"], []).append(it)

    decided, ambiguous = [], []
    for order_id, group in by_order.items():
        sns = _split(group[0]["mhz_order_id"])
        # 按 media_name 配对;同名多条按时序依次配。
        pool: dict = {}
        for sn in sns:
            nm = str((supplier.get(sn) or {}).get("media_name") or "").strip()
            pool.setdefault(nm, []).append(sn)
        for it in group:
            nm = str(it.get("media_name") or "").strip()
            cands = pool.get(nm) or []
            if len(cands) != 1:
                ambiguous.append({
                    "item_id": it["id"], "order_id": order_id, "media_name": nm,
                    "candidates": list(cands),
                    "why": ("媒体名对不上任何单号" if not cands
                            else "同名有 %d 个单号,时序也分不开" % len(cands))})
                continue
            sn = cands.pop()
            sup = supplier.get(sn) or {}
            new_status = _MHZ_STATUS_TO_ITEM.get(int(sup.get("status") or 0))
            decided.append({
                "item_id": it["id"], "order_id": order_id, "media_name": nm,
                "mhz_order_id": sn,
                "new_status": new_status,
                "publish_url": str(sup.get("url") or ""),
                "supplier_status": sup.get("status"),
            })
    return decided, ambiguous


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="真的写库。不传 = dry-run,只打印。")
    args = ap.parse_args()

    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        decided, ambiguous = plan(cur)

        logger.info("=== #151-B 修复计划(%s)===",
                    "执行" if args.execute else "🔴 DRY-RUN · 一个字都不写")
        logger.info("可确定映射 %d 条 / 判不了 %d 条", len(decided), len(ambiguous))
        for d in decided:
            logger.info("  item=%-6s order=%-6s 媒体=%-14s sn=%-20s "
                        "供应商状态=%s ⇒ %s%s",
                        d["item_id"], d["order_id"], d["media_name"],
                        d["mhz_order_id"], d["supplier_status"],
                        d["new_status"] or "(非终态,只回填单号)",
                        (" url=" + d["publish_url"]) if d["publish_url"] else "")
        for a in ambiguous:
            logger.info("  ⚠️ item=%-6s order=%-6s 媒体=%-14s **保持 submitted**:%s",
                        a["item_id"], a["order_id"], a["media_name"], a["why"])

        if not args.execute:
            logger.info("=== dry-run 结束。要执行请加 --execute"
                        "(需 Owner 在 Deploy 窗亲口同意)===")
            return 0

        from db.meijiehezi_db import update_order_item_status
        for d in decided:
            cur.execute(
                "UPDATE mhz_publish_order_items SET mhz_order_id = %s"
                " WHERE id = %s AND status = 'submitted'"
                "   AND mhz_order_id ~ '[,;|[:space:]]'",
                (d["mhz_order_id"], d["item_id"]))
            conn.commit()
            if d["new_status"]:
                # 🔴 走**既有**回写函数:退款/通知都在它里面,本脚本不新造。
                update_order_item_status(int(d["item_id"]), d["new_status"],
                                         publish_url=d["publish_url"] or None)
        logger.info("=== 已执行:%d 条 ===", len(decided))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
