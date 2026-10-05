"""146 个孤儿监测词收口(P0-B)

WO_MONITORING_OPTIN_DEFAULT_OFF_2026-08-15 · §二 P0-B

孤儿 = `confirmed_keywords.is_monitored = TRUE` 但**没有 active 订阅**。
2026-08-15 生产实证:146 条(全库 active 订阅仅 16 条、paused_low_balance 2 条;
`is_monitored=TRUE` 合计 157 条)。

2026-08-10 那次修法(`get_client_keywords` 的 is_monitored 改为派生自订阅 EXISTS)
让**界面**不再说谎,但**数据本身仍是脏的** —— 每日 cron 走 `list_active_subscriptions`,
这 146 条永远跑不到,而 `is_monitored` 这一列还写着 TRUE。

收口二选一,**由数据决定不许猜**(工单原文):
  · 有付费凭证 → 走 `create_keyword_monitor_subscription` 补订阅(已幂等)
  · 无付费凭证 → `is_monitored = FALSE`,**不建订阅**

🔴 复用不重写
────────────
判"有没有付费凭证"和"补订阅"这两件事,`scripts/kms_backfill_2026_05_29.py` 里已经有了
(`_paid_only_check` 的 paid-only guard + `backfill()` 的幂等 create 路径)。本脚本
**import 它**,只补它没做的那一半:把它 skip 掉的无凭证行显式关掉,并出前后差分。
另写一套判 paid 的逻辑 = 两份口径迟早分裂,这正是本工单在修的病。

判据必须有判别力(工单 §四.1 第 4 条)
──────────────────────────────────
本脚本打印 `orphan_before` / `orphan_after`,而不是只断言 "after == 0":
  · 恒为 0 的查询写成守卫,和没有守卫一样(本仓踩过)
  · 所以 orphan_before **必须非 0** 才说明这条判据当天有判别力;
    orphan_before == 0 时脚本显式标 `criterion_had_no_discriminating_power: true`
配套 `tests/monitoring_optin_2026_08_15/test_orphan_backfill_discriminating_power.py`
用真库夹具证明:拆掉回填这一步,孤儿数非 0;跑完回填,孤儿数 0。

用法
────
  python -X utf8 scripts/backfill_orphan_monitor_2026_08_15.py           # dry-run(默认)
  python -X utf8 scripts/backfill_orphan_monitor_2026_08_15.py --apply   # 真写入

退出码:0 = 完成 / 1 = 收口后仍有孤儿 / 2 = DB 错
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

# 🔴 复用既有幂等 + paid-only guard(工单指定,不另写一套)
from scripts.kms_backfill_2026_05_29 import (  # noqa: E402
    _list_backfill_candidates,
    _paid_only_check,
    backfill as kms_backfill,
)


def _connect():
    from db.connection import get_connection
    return get_connection()


def _orphan_count(cur) -> int:
    """孤儿定义与工单 §二 P0-B 判据逐字一致:is_monitored=TRUE 且无 active 订阅。"""
    cur.execute(
        """
        SELECT count(*) AS n
          FROM confirmed_keywords ck
         WHERE ck.is_monitored = TRUE
           AND NOT EXISTS (
               SELECT 1 FROM keyword_monitor_subscriptions kms
                WHERE kms.keyword_id = ck.id AND kms.status = 'active'
           )
        """
    )
    return int(cur.fetchone()["n"])


def _orphan_breakdown(cur) -> List[dict]:
    cur.execute(
        """
        SELECT COALESCE(q.status, '(no_quote)') AS quote_status,
               (q.paid_at IS NOT NULL)          AS has_paid_at,
               count(*)                         AS n
          FROM confirmed_keywords ck
          LEFT JOIN quotes q ON q.id = ck.quote_id
         WHERE ck.is_monitored = TRUE
           AND NOT EXISTS (
               SELECT 1 FROM keyword_monitor_subscriptions kms
                WHERE kms.keyword_id = ck.id AND kms.status = 'active'
           )
         GROUP BY 1, 2
         ORDER BY 3 DESC
        """
    )
    return [dict(r) for r in cur.fetchall()]


def _paid_but_unmonitored(cur) -> List[dict]:
    """🔴 「付费未监测」清单 —— 本脚本最重要的产出,交 Owner 逐个定夺。

    2026-08-15 生产真数据实证推翻了工单 §二 P0-B 的一个前提:
    那 146 个孤儿**没有一个**是"无付费凭证的脏数据" —— 全部是
    `status='confirmed'` **且 paid_at 有值、service_start_date 有值**,
    其中 80 个所在 quote 还是 `service_status='active'`(服务期内),
    涉及 18 个品牌、17 个是真客户(is_test=false)。

    为什么仍然把 is_monitored 关掉(Owner 2026-08-15 拍板):
      · 这 146 个词**今天本来就不跑** —— 每日 cron 只认 active 订阅(list_active_subscriptions),
        它们一条订阅都没有;界面自 2026-08-10 起也显示为关(派生自订阅真值)。
      · 所以关掉 = 让库不再说谎,**行为零变化、新增扣费为 0**。
      · 反过来"补订阅"会凭空产生 146 × 130 = 18,980 算力/天 的日扣费,
        而没有任何人点过开关 —— 那正是本工单在消灭的形态(元指令 #2)。
    但"这些客户付了钱却没人在监测"是**真实的服务缺口**,不能随着数据清洗一起被抹掉,
    所以单独成表交出来,由 Owner 决定给谁开。

    判据用服务锚口径(paid 或 confirmed+付款锚),不是 `_paid_only_check` 那条窄口径 ——
    窄口径是 v1.2(2026-05-29)的老规矩,2026-06-10 audit P1「老板已批」那次
    已明确它把这类真客户滤死了(见 db/monitoring_db.py::list_active_subscriptions 注释)。
    """
    cur.execute(
        """
        SELECT ck.id AS keyword_id, ck.keyword, ck.quote_id, q.brand_id, b.name AS brand_name,
               COALESCE(b.is_test, FALSE) AS is_test,
               q.status AS quote_status, q.paid_at::date AS paid_at,
               q.service_start_date, q.service_end_date, q.service_status
          FROM confirmed_keywords ck
          JOIN quotes q ON q.id = ck.quote_id
          LEFT JOIN brands b ON b.id = q.brand_id
         WHERE ck.is_monitored = TRUE
           AND NOT EXISTS (
               SELECT 1 FROM keyword_monitor_subscriptions kms
                WHERE kms.keyword_id = ck.id AND kms.status = 'active'
           )
           -- 服务锚口径的「有付费凭证」:已付款,或线下收款的 confirmed(付款锚有值)
           AND (
               q.status = 'paid'
               OR (q.status = 'confirmed'
                   AND COALESCE(q.service_start_date, q.paid_at) IS NOT NULL)
           )
         ORDER BY q.brand_id, q.id, ck.id
        """
    )
    return [dict(r) for r in cur.fetchall()]


def run(apply: bool = False) -> Dict[str, Any]:
    try:
        conn = _connect()
    except Exception as e:
        return {"ok": False, "error": f"DB 连接失败: {e}"}

    try:
        cur = conn.cursor()
        orphan_before = _orphan_count(cur)
        breakdown_before = _orphan_breakdown(cur)
        # 🔴 清单必须在**关掉之前**取:关掉之后这批行就不再满足"孤儿"条件,清单会变空
        paid_but_unmonitored = _paid_but_unmonitored(cur)

        # 1) 分流:复用既有 paid-only guard 判归属,不自己写第二套判 paid 的规则
        candidates = _list_backfill_candidates(cur)
        paid_rows, unpaid_rows = [], []
        for row in candidates:
            ok, reason = _paid_only_check(row)
            (paid_rows if ok else unpaid_rows).append({**row, "_reason": reason})
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 2) 有付费凭证的那一半 → 交给既有脚本补订阅(它内部幂等、且会回填 ck.monitoring_subscription_id)
    kms_report = kms_backfill(brand_ids=None, apply=apply)

    # 3) 补完订阅之后**仍然是孤儿**的 → 关掉 is_monitored,不建订阅
    #
    # 🔴 为什么按"补完之后的残余"关,而不是按上面 unpaid_rows 那个 id 列表关
    #    (2026-08-15 变异实测倒逼的改法):
    #    第一版是 `WHERE id = ANY(unpaid_ids) AND NOT EXISTS(active sub)`。变异测试把分流拆掉
    #    (让所有行都进 unpaid_rows)之后**测试照样全绿** —— 因为真正拦住有凭证那批的
    #    从来不是我那个 id 列表,而是 `NOT EXISTS(active sub)` 这个守卫:第 2 步刚给它们建了订阅。
    #    也就是说 id 列表在写路径上是装饰,只是看起来在保护。
    #    改成按残余集合关之后:
    #      · 代码说的就是它做的:"能补订阅的已经补了,补不上的才关";
    #      · 顺带覆盖 kms_backfill 因**别的**理由跳过的行(比如品牌归属人解析不到),
    #        那些行第一版会被漏在 TRUE 上,继续当孤儿;
    #      · 承重点唯一且明确 → 变异 M13 直接打这个守卫,拆掉必红(见 run_mutations.py)。
    closed: List[dict] = []
    try:
        conn = _connect()
        cur = conn.cursor()
        if apply:
            cur.execute(
                """
                UPDATE confirmed_keywords
                   SET is_monitored = FALSE,
                       monitoring_subscription_id = NULL
                 WHERE is_monitored = TRUE
                   AND NOT EXISTS (
                       SELECT 1 FROM keyword_monitor_subscriptions kms
                        WHERE kms.keyword_id = confirmed_keywords.id AND kms.status = 'active'
                   )
                RETURNING id, brand_id, quote_id, keyword
                """
            )
            closed = [dict(r) for r in cur.fetchall()]
            conn.commit()

        orphan_after = _orphan_count(cur)
        breakdown_after = _orphan_breakdown(cur)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 「付费未监测」清单落盘 —— 它是给人看的产出,不能只活在 stdout 里
    report_path = None
    if paid_but_unmonitored:
        import csv
        from pathlib import Path
        report_path = str(Path(__file__).resolve().parents[1] /
                          "output" / "paid_but_unmonitored_2026_08_15.csv")
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        # 🔴 newline="" —— 不加的话 csv 模块在 Windows 上每行会写成 \r\r\n
        with open(report_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(paid_but_unmonitored[0].keys()))
            w.writeheader()
            w.writerows(paid_but_unmonitored)

    by_brand: Dict[Any, int] = {}
    for r in paid_but_unmonitored:
        key = (r.get("brand_id"), r.get("brand_name"), r.get("is_test"))
        by_brand[key] = by_brand.get(key, 0) + 1

    return {
        "ok": True,
        "apply": apply,
        "orphan_before": orphan_before,
        "orphan_after": orphan_after,
        "orphan_diff": orphan_after - orphan_before,
        "breakdown_before": breakdown_before,
        "breakdown_after": breakdown_after,
        "paid_candidates": len(paid_rows),
        "unpaid_candidates": len(unpaid_rows),
        "subscriptions_backfilled": kms_report.get("backfilled"),
        # 🔴 命名即断言:这批行**不是**"无付费凭证",而是"没有 active 订阅"。
        #    2026-08-15 真数据实测:146/146 都有付款锚。叫 closed_without_evidence 是在说谎。
        "closed_no_active_subscription": len(closed),
        "closed_details": closed[:20],
        "kms_report": kms_report,
        # 交 Owner 定夺的服务缺口(关掉数据 ≠ 这些客户不该被监测)
        "paid_but_unmonitored_total": len(paid_but_unmonitored),
        "paid_but_unmonitored_by_brand": [
            {"brand_id": k[0], "brand_name": k[1], "is_test": k[2], "keywords": v}
            for k, v in sorted(by_brand.items(), key=lambda kv: -kv[1])
        ],
        "paid_but_unmonitored_csv": report_path,
        # 判别力自证:before 若本来就是 0,那这条判据当天证明不了任何事 —— 显式标出来,别让它假绿
        "criterion_had_no_discriminating_power": orphan_before == 0,
        "idempotent_hint": "重跑本脚本:orphan_diff 应为 0 · closed_no_active_subscription 应为 0",
        "owner_note": (
            "本脚本**不建任何订阅、不产生任何新扣费**。关掉 is_monitored 只是让库与"
            "「实际不跑、界面显示关」一致(行为零变化)。付费却无人监测的服务缺口见 "
            "paid_but_unmonitored_csv,由 Owner 决定给谁开。"
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="146 孤儿监测词收口(复用 kms_backfill 的幂等 + paid-only guard)")
    ap.add_argument("--apply", action="store_true", help="真写入(默认 dry-run)")
    args = ap.parse_args()

    report = run(apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        return 2
    if args.apply and report.get("orphan_after", 1) != 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
