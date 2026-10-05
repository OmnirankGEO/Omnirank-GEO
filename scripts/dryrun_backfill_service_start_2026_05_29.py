#!/usr/bin/env python3
"""confirmed + 服务锚双空(service_start_date & paid_at 均 NULL)历史 backfill · dry-run 报告 · 2026-05-29 · CTO-15.23

老板拍板(D1/D2/D5):
  - service_start_date = 权威服务锚;confirmed = 服务已激活。
  - 历史 confirmed 真客户因"确认时没写锚"被 portal 隐藏(翠玉/QZQZ 同根)。
  - backfill 推荐值 = min(扣费首日, 监测首日)(最早真实业务证据)· 草稿(无证据)留 NULL。

⚠️ 默认只出 CSV 报告 · 0 写。老板逐条审后,Deploy-CTO 用 `--apply --quote-ids` 白名单写。
   不盲跑(老板已示范 2 例都看不同候选)。写的是 service_start_date(不是 paid_at · 锚口径已翻转)。

用法:
  python scripts/dryrun_backfill_service_start_2026_05_29.py                       # 全平台 CSV 报告(0 写)
  python scripts/dryrun_backfill_service_start_2026_05_29.py --apply --quote-ids 289,109   # 仅写老板批的 quote(BEGIN/COMMIT)

候选(每 quote):
  C1 first_charge   point_transactions 最早 monitoring consume(brands.owner_user_id 维度 · 真实扣费证据)
  C2 first_monitor  monitoring_results 最早 tested_at(brand 维度 · 真实跑监测)
  C3 kms_active     最早 active KMS created  C4 kms_any 最早任意 KMS created  C5 quote_created
分类:real = 有 active KMS / 真实监测行 / 真实扣费;否则 draft(留 NULL)。
推荐:min(C1, C2) 忽略 NULL;两者皆空 → 无推荐(draft)。

schema 注:feature_code 用 LIKE 'monitor%';brands.owner_user_id 为代理;字段先 \\d 核(SQL 4 维)。
红线:只读 + 可选写 quotes.service_start_date(白名单)· 不碰 billing/jwt/middleware/connection/geo_scope_scorer。
"""
from __future__ import annotations

import argparse
import sys

SCAN_SQL = """
SELECT q.id AS quote_id, q.brand_id, b.name AS brand_name, q.status, q.service_days,
  (SELECT COUNT(*) FROM keyword_monitor_subscriptions kms
     WHERE kms.quote_id = q.id AND kms.status = 'active') AS active_kms,
  (SELECT COUNT(*) FROM monitoring_results mr JOIN monitoring_tasks mt ON mt.id = mr.task_id
     WHERE mt.brand_id = q.brand_id) AS monitoring_rows,
  (SELECT MIN(pt.created_at)::date FROM point_transactions pt
     WHERE pt.type = 'consume' AND pt.feature_code LIKE 'monitor%%'
       AND pt.user_id = (SELECT owner_user_id FROM brands WHERE id = q.brand_id)) AS first_charge,
  (SELECT MIN(mr.tested_at)::date FROM monitoring_results mr JOIN monitoring_tasks mt ON mt.id = mr.task_id
     WHERE mt.brand_id = q.brand_id) AS first_monitor,
  (SELECT MIN(kms.created_at)::date FROM keyword_monitor_subscriptions kms
     WHERE kms.quote_id = q.id AND kms.status = 'active') AS kms_active_earliest,
  (SELECT MIN(kms.created_at)::date FROM keyword_monitor_subscriptions kms
     WHERE kms.quote_id = q.id) AS kms_any_earliest,
  q.created_at::date AS quote_created
FROM quotes q JOIN brands b ON b.id = q.brand_id
WHERE q.status = 'confirmed' AND q.paid_at IS NULL AND q.service_start_date IS NULL
ORDER BY q.id
"""

COLS = ["quote_id", "brand_id", "brand_name", "status", "service_days", "active_kms",
        "monitoring_rows", "first_charge", "first_monitor", "kms_active_earliest",
        "kms_any_earliest", "quote_created", "classification", "recommended_service_start"]


def _recommend(row) -> tuple:
    """返 (classification, recommended_date_or_None)。"""
    is_real = bool(row.get("active_kms") or row.get("monitoring_rows") or row.get("first_charge"))
    cands = [row.get("first_charge"), row.get("first_monitor")]
    cands = [c for c in cands if c]
    rec = min(cands) if cands else None
    return ("real" if is_real else "draft", rec)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写入(默认只出报告)· 必须配 --quote-ids 白名单")
    ap.add_argument("--quote-ids", default="", help="逗号分隔 · 仅写这些 quote(老板逐条批)")
    args = ap.parse_args()

    from db.connection import get_connection
    conn = get_connection()
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(SCAN_SQL)
    rows = [dict(r) for r in (cur.fetchall() or [])]

    # CSV 报告(stdout · Deploy-CTO 可重定向)
    print(",".join(COLS))
    enriched = []
    for r in rows:
        cls, rec = _recommend(r)
        r["classification"] = cls
        r["recommended_service_start"] = rec.isoformat() if rec else ""
        enriched.append(r)
        print(",".join(str(r.get(c, "") if r.get(c) is not None else "") for c in COLS))

    real = [r for r in enriched if r["classification"] == "real" and r["recommended_service_start"]]
    draft = [r for r in enriched if r["classification"] == "draft"]
    print(f"\n--- 汇总 --- 总 {len(enriched)} · real 可 backfill {len(real)} · draft 留 NULL {len(draft)} ·"
          f" real 无推荐(证据缺) {len([r for r in enriched if r['classification']=='real' and not r['recommended_service_start']])}")

    if not args.apply:
        conn.rollback()
        conn.close()
        print("=== DRY-RUN · 0 写。老板审 real 行后,加 --apply --quote-ids <批准的 id> 写 service_start_date ===")
        return 0

    # APPLY:仅写白名单 quote(逐条批)
    allow = {int(x) for x in args.quote_ids.split(",") if x.strip().isdigit()}
    if not allow:
        conn.rollback(); conn.close()
        print("!!! --apply 必须配 --quote-ids 白名单(老板逐条批)· 已中止 · 0 写", file=sys.stderr)
        return 2
    written = 0
    try:
        for r in enriched:
            if r["quote_id"] not in allow:
                continue
            if not r["recommended_service_start"]:
                print(f"  跳过 quote {r['quote_id']}:无推荐值(证据缺 · 不盲填)")
                continue
            cur.execute(
                "UPDATE quotes SET service_start_date = %s WHERE id = %s "
                "AND status = 'confirmed' AND service_start_date IS NULL",
                (r["recommended_service_start"], r["quote_id"]),
            )
            print(f"  写 quote {r['quote_id']} service_start_date={r['recommended_service_start']} (rowcount={cur.rowcount})")
            written += cur.rowcount
        conn.commit()
        print(f"=== 已 COMMIT · 写 {written} 行 ===")
    except Exception as e:
        conn.rollback()
        print(f"!!! 失败已 ROLLBACK: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
