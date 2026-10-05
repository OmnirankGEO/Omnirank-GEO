"""KMS Backfill · v1.6 2026-05-29

老板 P0 6 大任务 #5 · brand 6/428 类 backfill(走 endpoint · 不裸插)

根因:
  - confirmed_keywords.is_monitored=TRUE 但无 active keyword_monitor_subscriptions
  - 老 enable 流程 bug / 数据迁移漏写 → ck 标记开但 KMS 没创建
  - scheduler.py 走 list_active_subscriptions(KMS 表)· 这种词永远跑不到监测
  - 等同"客户付钱但实际没监测" · 信任灾难

修法(老板 P0):
  ✅ 走 create_keyword_monitor_subscription(同 endpoint 路径 · 幂等 + paid-only guard)
  ❌ 不直接 SQL INSERT INTO keyword_monitor_subscriptions

用法:
  python -X utf8 scripts/kms_backfill_2026_05_29.py --dry-run                 # 默认 dry-run
  python -X utf8 scripts/kms_backfill_2026_05_29.py --brand-ids 6,428         # 限定 brand
  python -X utf8 scripts/kms_backfill_2026_05_29.py --apply                   # 真写入
  python -X utf8 scripts/kms_backfill_2026_05_29.py --brand-ids 6,428 --apply

退出码:0 = 完成 / 1 = 有 paid-only 拒绝 / 2 = DB 错
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))


def _connect():
    from db.connection import get_connection
    return get_connection()


def _list_backfill_candidates(cur, brand_ids: Optional[List[int]] = None) -> List[dict]:
    """列出 is_monitored=TRUE 但无 active KMS 的 confirmed_keyword

    返字段:keyword_id, brand_id, quote_id, keyword, quote_status, quote_paid_at,
           brand_owner_user_id(= 计费主体)

    🔴 2026-08-15 修(WO_MONITORING_OPTIN_DEFAULT_OFF · P0-B 复用本脚本时实证发现):
       原来这里选的是 `q.user_id`,而 **quotes 表根本没有 user_id 这一列**
       (生产 information_schema 实测:owner_user_id / created_by_user_id /
        responsible_user_id / archived_by_user_id 四个,没有 user_id;
        直接跑 `SELECT q.user_id FROM quotes` 报 `column q.user_id does not exist`)。
       也就是说本脚本自 quotes 那次 user_id → owner_user_id 改名起就**跑第一条 SQL 就炸**,
       几个月无人察觉 —— 因为它的既有测试全是对源码文本做 grep,从没真跑过 SQL。
       同时把计费主体口径对齐 2026-06-10 audit P0-4 的裁决:
       计费主体锚 **brands.owner_user_id**(不是 quote 上的人、更不是操作者),
       解析不到就不创建(见 backfill() 里的 fail-closed),绝不回落成 0 号幽灵用户。
    """
    where_clauses = [
        "ck.is_monitored = TRUE",
        "COALESCE(ck.monitoring_status, 'active') = 'active'",
        "s.id IS NULL",  # 无 active sub
    ]
    params: list = []
    if brand_ids:
        where_clauses.append("ck.brand_id = ANY(%s)")
        params.append(brand_ids)
    sql = f"""
        SELECT ck.id AS keyword_id, ck.brand_id, ck.quote_id, ck.keyword,
               q.status AS quote_status, q.paid_at AS quote_paid_at,
               COALESCE(b.owner_user_id, qb.owner_user_id) AS brand_owner_user_id
        FROM confirmed_keywords ck
        LEFT JOIN keyword_monitor_subscriptions s
          ON s.keyword_id = ck.id AND s.status = 'active'
        LEFT JOIN quotes q ON q.id = ck.quote_id
        LEFT JOIN brands b ON b.id = ck.brand_id AND COALESCE(b.is_deleted, FALSE) = FALSE
        LEFT JOIN brands qb ON qb.id = q.brand_id AND COALESCE(qb.is_deleted, FALSE) = FALSE
        WHERE {' AND '.join(where_clauses)}
        ORDER BY ck.brand_id, ck.quote_id, ck.id
    """
    cur.execute(sql, tuple(params) if params else None)
    return [dict(r) for r in cur.fetchall()]


def _paid_only_check(row: dict) -> Tuple[bool, str]:
    """复制 server.py _assert_keyword_quote_paid_blocking 的语义 · 但用普通返值

    返 (is_paid, reason)
    """
    if not row.get("quote_id"):
        return (False, "quote_id 为空(脏数据)")
    if not row.get("quote_status"):
        return (False, "quote 不存在")
    if row.get("quote_status") != "paid":
        return (False, f"quote.status = '{row['quote_status']}' != 'paid'")
    if not row.get("quote_paid_at"):
        return (False, "paid_at NULL(legacy)")
    return (True, "ok")


def backfill(
    brand_ids: Optional[List[int]] = None,
    apply: bool = False,
    daily_points: int = 130,
    feature_code: str = "monitoring_keyword_daily",
) -> dict:
    """主流程

    返字段:
      candidates: 总候选数
      paid_only_skipped: 因 paid-only 拒绝跳过的数 + 原因
      backfilled: 走 create_keyword_monitor_subscription 创建的数(dry-run 时计数 但不调用)
      details: 每条样本
    """
    try:
        conn = _connect()
    except Exception as e:
        return {"ok": False, "error": f"DB 连接失败: {e}"}

    candidates: List[dict] = []
    skipped: List[dict] = []
    created: List[dict] = []
    errors: List[dict] = []

    try:
        cur = conn.cursor()
        candidates = _list_backfill_candidates(cur, brand_ids)

        # 不在循环内 import · 提前 import 避免 dry-run 时也加载
        if apply:
            from db.monitoring_db import (
                create_keyword_monitor_subscription,
                update_keyword_monitor_state,
            )

        for row in candidates:
            paid_ok, reason = _paid_only_check(row)
            if not paid_ok:
                skipped.append({
                    "keyword_id": row["keyword_id"],
                    "brand_id": row["brand_id"],
                    "quote_id": row["quote_id"],
                    "reason": reason,
                })
                continue

            if not apply:
                # dry-run 仅计数
                created.append({
                    "keyword_id": row["keyword_id"],
                    "brand_id": row["brand_id"],
                    "quote_id": row["quote_id"],
                    "dry_run": True,
                })
                continue

            # [2026-08-15 修 · 对齐 audit P0-4 fail-closed] 计费主体解析不到 → 不创建订阅。
            #   旧代码 `or 0` 会把订阅挂到 0 号用户名下(幽灵计费主体),
            #   而端点侧 2026-06-10 已明确:owner 无法确认就拒绝创建、禁回落。
            billing_uid = row.get("brand_owner_user_id")
            if not billing_uid:
                skipped.append({
                    "keyword_id": row["keyword_id"],
                    "brand_id": row["brand_id"],
                    "quote_id": row["quote_id"],
                    "reason": "无法确认品牌归属人(brands.owner_user_id 空)· 不创建订阅",
                })
                continue

            # apply 模式:走 endpoint 同路径 · 幂等
            try:
                sub_id = create_keyword_monitor_subscription(
                    user_id=billing_uid,
                    keyword_id=row["keyword_id"],
                    quote_id=row["quote_id"],
                    brand_id=row["brand_id"],
                    daily_points=daily_points,
                    feature_code=feature_code,
                )
                # [v1.7 P1-4 老板复审] create 后必须回填 ck.monitoring_subscription_id
                # 否则 brand 6/428 从 "KMS NULL" 变 "有 active KMS 但 ck.sub_id 仍 NULL"
                # → 健康检查 #4(active KMS 但 ck.is_monitored 不一致)仍会报 → backfill 形同未跑
                ck_synced = False
                try:
                    ck_synced = update_keyword_monitor_state(
                        keyword_id=row["keyword_id"],
                        is_monitored=True,
                        subscription_id=sub_id,
                    )
                except Exception as _ck_err:
                    # ck 同步失败不致命 · 但记 error · 后续健康检查会捞出
                    errors.append({
                        "keyword_id": row["keyword_id"],
                        "brand_id": row["brand_id"],
                        "stage": "ck_sync",
                        "sub_id": sub_id,
                        "error": str(_ck_err),
                    })
                created.append({
                    "keyword_id": row["keyword_id"],
                    "brand_id": row["brand_id"],
                    "quote_id": row["quote_id"],
                    "sub_id": sub_id,
                    "ck_synced": ck_synced,
                })
            except Exception as e:
                errors.append({
                    "keyword_id": row["keyword_id"],
                    "brand_id": row["brand_id"],
                    "stage": "create_sub",
                    "error": str(e),
                })
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {
        "ok": True,
        "apply": apply,
        "brand_ids_filter": brand_ids,
        "candidates_total": len(candidates),
        "paid_only_skipped": len(skipped),
        "skipped_details": skipped[:20],
        "backfilled": len(created),
        "backfilled_details": created[:20],
        "errors": errors,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="KMS backfill · 走 endpoint 不裸插")
    ap.add_argument("--brand-ids", default="", help="逗号分隔 brand_id 列表 · 空 = 全部")
    ap.add_argument("--apply", action="store_true", help="真写入(默认 dry-run)")
    ap.add_argument("--daily-points", type=int, default=130)
    ap.add_argument("--feature-code", default="monitoring_keyword_daily")
    args = ap.parse_args()

    brand_ids: Optional[List[int]] = None
    if args.brand_ids.strip():
        try:
            brand_ids = [int(x.strip()) for x in args.brand_ids.split(",") if x.strip()]
        except ValueError:
            print(json.dumps({"ok": False, "error": "brand-ids 必须是逗号分隔整数"}, ensure_ascii=False))
            return 2

    report = backfill(
        brand_ids=brand_ids,
        apply=args.apply,
        daily_points=args.daily_points,
        feature_code=args.feature_code,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    if not report.get("ok"):
        return 2
    if report.get("paid_only_skipped", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
