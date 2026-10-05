#!/usr/bin/env python3
"""keyword_compliance_log 历史出现率回填(消除旧代码污染)· dry-run · 2026-05-29 · CTO-15.23

老板拍板 Q1/Q3:出现率头条改【原始检出率】· 回填历史污染(旧代码把真实检出日记成 detection_rate=0)。
璧山 quote 178 / ck 2212:05-21 实际 3/4 检出却记 0 → effective_rate 被拖到 6.9%(过期冻结不自愈)。

本脚本(默认只读):
  对每个 (keyword_id, source, quote_id, check_date) 已有 compliance 行,
  从 monitoring_results 按【当日真实结果】重算:
    detection_rate = 原始检出率 = SUM(is_detected)/COUNT(*) × 100(当日全平台/轮次 · Q1 口径)
    effective_rate = 忠实复刻 _compute_effective_rate_v3:
        consec_below ≥ lazy(默认7) → detection_rate(懒政暴露)
        否则 max(detection_rate, 近 window 天已重算 detection 均值)(倒反天罡 max 保护)
    is_compliant = effective_rate ≥ 该行原 target_rate
  输出 before→after CSV · 不写。

⚠️ 默认 dry-run(0 写)· 老板审 CSV 后,Deploy-CTO 用 `--apply --quote-ids` 白名单写。
   只 UPDATE keyword_compliance_log 的 detection_rate/effective_rate/is_compliant · 不碰 monitoring_results(真相源)。
   只重算"当日有 monitoring_results"的行(无结果日不动)。

红线:不碰 billing/jwt/middleware/connection/geo_scope_scorer · 不改算法代码(仅数据回填)。
用法:
  python scripts/dryrun_backfill_compliance_rate_2026_05_29.py                          # 全平台 CSV(0 写)
  python scripts/dryrun_backfill_compliance_rate_2026_05_29.py --quote-ids 178          # 仅看某 quote
  python scripts/dryrun_backfill_compliance_rate_2026_05_29.py --apply --quote-ids 178  # 写(老板批的 quote)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

ROLLING_WINDOW = 7
LAZY_THRESHOLD = 7

# 待回填行:有 compliance 行的 (keyword, source, quote, date)
ROWS_SQL = """
SELECT cl.keyword_id, cl.keyword_source, cl.quote_id, cl.check_date,
       cl.detection_rate AS old_detection, cl.effective_rate AS old_effective,
       cl.target_rate, cl.is_compliant AS old_compliant
FROM keyword_compliance_log cl
{where}
ORDER BY cl.quote_id, cl.keyword_id, cl.keyword_source, cl.check_date
"""

# 当日真实原始检出率(monitoring_results · 按 keyword_id + 当日)
DAY_RAW_SQL = """
SELECT COALESCE(ROUND(SUM(is_detected)::numeric / NULLIF(COUNT(*), 0) * 100, 1), 0) AS raw_rate,
       COUNT(*) AS n
FROM monitoring_results
WHERE keyword_id = %s AND tested_at::date = %s
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--quote-ids", default="", help="逗号分隔 · 限定/白名单 quote")
    args = ap.parse_args()
    ids = [int(x) for x in args.quote_ids.split(",") if x.strip().isdigit()]

    from db.connection import get_connection
    conn = get_connection()
    conn.autocommit = False
    cur = conn.cursor()

    where = ""
    params = []
    if ids:
        where = "WHERE cl.quote_id = ANY(%s)"
        params = [ids]
    cur.execute(ROWS_SQL.format(where=where), tuple(params))
    rows = [dict(r) for r in (cur.fetchall() or [])]

    # 按 (keyword, source, quote) 分组,按 check_date 序回填(effective 需前序)
    from collections import defaultdict, OrderedDict
    groups = defaultdict(list)
    for r in rows:
        groups[(r["keyword_id"], r["keyword_source"], r["quote_id"])].append(r)

    print("quote_id,keyword_id,source,check_date,old_detection,new_detection,old_effective,new_effective,old_compliant,new_compliant,mr_count,changed")
    changes = []
    for (kid, src, qid), series in groups.items():
        prev_det = []          # 已重算 detection(按日序)
        prev_compliant = []    # 已重算 is_compliant(按日序 · 懒政用)
        for r in series:
            cur.execute(DAY_RAW_SQL, (kid, r["check_date"]))
            day = cur.fetchone() or {}
            mr_n = int(day.get("n") or 0)
            if mr_n == 0:
                # 当日无 monitoring_results → 不重算(保留原值)
                prev_det.append(float(r["old_detection"] or 0))
                prev_compliant.append(bool(r["old_compliant"]))
                continue
            new_det = float(day.get("raw_rate") or 0)
            target = float(r["target_rate"] or 65)
            window = prev_det[-ROLLING_WINDOW:]
            # 懒政:近 LAZY_THRESHOLD 天连续不达标 → 暴露瞬时(倒反天罡 max 否则)
            recent = prev_compliant[-LAZY_THRESHOLD:]
            consec_below = 0
            for c in reversed(recent):
                if not c:
                    consec_below += 1
                else:
                    break
            if consec_below >= LAZY_THRESHOLD:
                new_eff = round(new_det, 1)
            else:
                rolling = (sum(window) + new_det) / (len(window) + 1) if window else new_det
                new_eff = round(max(new_det, rolling), 1)
            new_comp = new_eff >= target
            prev_det.append(new_det)
            prev_compliant.append(new_comp)
            changed = (round(float(r["old_detection"] or 0), 1) != round(new_det, 1)
                       or round(float(r["old_effective"] or 0), 1) != new_eff
                       or bool(r["old_compliant"]) != new_comp)
            print(f"{qid},{kid},{src},{r['check_date']},{r['old_detection']},{new_det},"
                  f"{r['old_effective']},{new_eff},{r['old_compliant']},{new_comp},{mr_n},{changed}")
            if changed:
                changes.append((kid, src, qid, r["check_date"], new_det, new_eff, new_comp))

    print(f"\n--- 汇总 --- 扫 {len(rows)} 行 · 需变更 {len(changes)} 行 ---")

    if not args.apply:
        conn.rollback(); conn.close()
        print("=== DRY-RUN · 0 写。老板审 changed=True 行后,--apply --quote-ids <批准 id> 写 ===")
        return 0
    if not ids:
        conn.rollback(); conn.close()
        print("!!! --apply 必须配 --quote-ids 白名单 · 已中止 · 0 写", file=sys.stderr)
        return 2
    try:
        for kid, src, qid, cd, det, eff, comp in changes:
            cur.execute("""
                UPDATE keyword_compliance_log
                SET detection_rate = %s, effective_rate = %s, is_compliant = %s
                WHERE keyword_id = %s AND keyword_source = %s AND quote_id = %s AND check_date = %s
            """, (det, eff, comp, kid, src, qid, cd))
        conn.commit()
        print(f"=== 已 COMMIT · 写 {len(changes)} 行 ===")
    except Exception as e:
        conn.rollback()
        print(f"!!! 失败已 ROLLBACK: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
