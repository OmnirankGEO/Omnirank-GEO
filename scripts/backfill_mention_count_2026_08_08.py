#!/usr/bin/env python3
"""存量回填 · 改写单元格后没跟着刷新的汇总计数。

[WO_MENTION_COUNT_NOT_RECOMPUTED 2026-08-08 §4.2]

背景:``brand_verdict`` 被改写过的诊断(人工确认 / 存量重建),
``ai_visibility.detected_count`` 与 ``overall_mention_rate`` 停在旧值,
客户版报告正文那句「品牌被提及 **N** 次」因此说错数 ——
5 份真客户被**少报**(确认动作白做),2 份内部账号被**多报**(与隐形级自相矛盾)。

本脚本**只重算汇总、不重判单元格**——与 ``rebuild_parenthetical_false_mentions``
的区别正在这里:那个会改 ``brand_verdict``,这个一个格子都不碰。
人工已决格(``identity_review_state ∈ {confirmed, rejected}``)天然不受影响,
因为本脚本压根不走判定逻辑(§3.3)。

用法(§4.2 要求先 dry-run 逐份过目,且真客户与内部账号分批):

    # 第 1 批 · 内部账号(验证链路)
    python scripts/backfill_mention_count_2026_08_08.py --ids 541,563
    python scripts/backfill_mention_count_2026_08_08.py --ids 541,563 --apply

    # 第 2 批 · 真客户(确认第 1 批无异常后再跑)
    python scripts/backfill_mention_count_2026_08_08.py --ids 501,505,529,545,547
    python scripts/backfill_mention_count_2026_08_08.py --ids 501,505,529,545,547 --apply

🔴 **没有默认 ids**:必须显式传。上一单的教训是脚本自带默认名单容易被"顺手全跑",
这次要求每批都由执行人亲手写出来。

🔴 死锁纪律沿用上一单(`799bf9a2`)的三条不变式,直接复用那边的实现,不另写一套:
装配链在开事务前预加载 · 只读+装配全部前移到第一条写语句之前 · 逐份提交 ·
写窗口内禁止开新连接(违反当场抛 ``ConnectionInsideLockWindow``)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 复用上一单的死锁护栏与预加载(§4.2 不许再写第二套)
from scripts.rebuild_parenthetical_false_mentions_2026_08_06 import (  # noqa: E402
    _forbid_new_connections,
    _load,
    _preload_report_chain,
)


def backfill_one(conn, diagnosis_id: int, *, apply: bool) -> dict:
    """重算一份诊断的汇总计数。**不重判单元格。**"""
    from services.diagnosis_identity_decision import (
        _aggregates,
        _ai_visibility,
        _funnel_from_dimension_stats,
        _load_identity_for_review,
        _parse_raw_data,
        _rate_or_none,
        apply_recomputed_totals,
    )

    record = _load(conn, diagnosis_id)
    if not record:
        return {"diagnosis_id": diagnosis_id, "status": "not_found"}
    brand_id = int(record.get("brand_id") or 0)
    if not brand_id:
        return {"diagnosis_id": diagnosis_id, "status": "no_brand_id"}

    raw = _parse_raw_data(record.get("raw_data_json"))
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    ai = _ai_visibility(raw)
    if not ai.get("detail_table"):
        return {"diagnosis_id": diagnosis_id, "status": "no_detail_table"}

    identity = _load_identity_for_review(
        brand_id, fallback_name=record.get("brand_name") or ""
    )
    if getattr(identity, "load_error", False):
        # fail-closed:身份读不到就不算(否则会把全部格算成未提到,反向造一次少报)
        return {"diagnosis_id": diagnosis_id, "status": "identity_load_failed"}

    # 🔴 落库前先留一份单元格指纹:本脚本承诺"一个格子都不改",
    #    结束时用它自证(而不是靠"我没写改格的代码"这种口头保证)。
    cells_fingerprint_before = _cells_fingerprint(ai)

    aggregates = _aggregates(ai, identity)
    totals_delta = apply_recomputed_totals(ai, aggregates)
    funnel = _funnel_from_dimension_stats(aggregates["dimension_stats"])
    data["ai_visibility"] = ai
    raw["data"] = data

    cells_fingerprint_after = _cells_fingerprint(ai)
    if cells_fingerprint_before != cells_fingerprint_after:
        raise RuntimeError(
            f"diagnosis_id={diagnosis_id}:回填过程改动了单元格 —— 本脚本不允许这样,已中止"
        )

    result = {
        "diagnosis_id": diagnosis_id,
        "brand_id": brand_id,
        "brand_name": record.get("brand_name"),
        "status": "backfilled" if apply else "dry_run",
        "detected_before": totals_delta["detected_count"]["before"],
        "detected_after": totals_delta["detected_count"]["after"],
        "rate_before": totals_delta["overall_mention_rate"]["before"],
        "rate_after": totals_delta["overall_mention_rate"]["after"],
        "col_detected_before": record.get("ai_detected_count"),
        "col_rate_before": record.get("ai_mention_rate"),
        # 头牌分数按口径**不该**因本次回填而变(它来自 dimension_stats,不来自计数)。
        # 仍然打印出来给执行人肉眼核:一旦变了就是出事了,别默默放过。
        "score_before": record.get("total_score"),
        "score_after_recomputed": funnel.get("total_score"),
        "level_before": record.get("level"),
        "level_after_recomputed": funnel.get("level"),
    }
    if not apply:
        return result

    cur = conn.cursor()

    # ── 阶段 1:只读 + 报告装配(此时一条写语句都还没发)──────────────────
    report_error = None
    v2 = None
    try:
        cur.execute("SELECT * FROM brands WHERE id = %s", (brand_id,))
        brand_row = cur.fetchone()
        cur.execute("SELECT * FROM client_profiles WHERE brand_id = %s LIMIT 1", (brand_id,))
        profile_row = cur.fetchone()
        cur.execute(
            "SELECT * FROM quotes WHERE brand_id = %s ORDER BY created_at DESC LIMIT 1",
            (brand_id,),
        )
        quote_row = cur.fetchone()

        from services.diagnosis_report_v2 import assemble_diagnosis_report_v2

        v2 = assemble_diagnosis_report_v2(
            diagnosis_results=raw,
            brand=dict(brand_row) if brand_row else {},
            profile=dict(profile_row) if profile_row else {},
            quote=dict(quote_row) if quote_row else {},
            brand_id=brand_id,
            score_data=raw.get("score_data") or raw.get("scores") or {},
            # 🔴 [#54/#55] 本脚本没有可透传的游标(装配深处自开连接会重放 08-10 自锁死),
            #    所以**显式**传不可用,并写明 reason —— 不是省略、也不是默认值。
            #    后果:回填/重生成产物在这一格上退出分母,与主链路口径不同,已在交付里点名。
            published={"count": None, "available": False, "reason": "backfill_no_cursor"},
        )
        report_error = v2.get("error")
        v2["funnel_score"] = funnel
    except Exception as exc:  # noqa: BLE001
        report_error = f"v2 报告重生异常: {exc}"
        v2 = None

    # ── 阶段 2:写窗口(到 commit 为止,禁止再开新连接)────────────────────
    report_rebuilt = False
    with _forbid_new_connections("backfill_one 写窗口"):
        cur.execute(
            "UPDATE diagnosis_records SET raw_data_json = %s WHERE id = %s",
            (json.dumps(raw, ensure_ascii=False, default=str), diagnosis_id),
        )
        cur.execute(
            "UPDATE diagnosis_records SET ai_detected_count=%s,"
            " ai_mention_rate=COALESCE(%s, ai_mention_rate) WHERE id=%s",
            (
                int(totals_delta["detected_count"]["after"]),
                _rate_or_none(totals_delta),  # 无分母 → None → 保留原列值,绝不写 0
                diagnosis_id,
            ),
        )

        if v2 is not None and not report_error:
            from services.diagnosis_report_v2 import update_diagnosis_v2_in_db

            cur.execute("SAVEPOINT sp_v2")
            try:
                if update_diagnosis_v2_in_db(diagnosis_id, v2, conn=conn):
                    report_rebuilt = True
                else:
                    report_error = "v2 报告回写失败"
            except Exception as exc:  # noqa: BLE001
                report_error = f"v2 报告回写异常: {exc}"
            if not report_rebuilt:
                cur.execute("ROLLBACK TO SAVEPOINT sp_v2")
            cur.execute("RELEASE SAVEPOINT sp_v2")

    # 🔴 分数这件事说准一点(第一版注释写得不准,被判据打红才发现):
    #    · 本脚本**自己**一条 total_score / level / brands.latest_score 都不写;
    #    · 但它触发的 v2 重生(`update_diagnosis_v2_in_db`)在 funnel 有效时
    #      **会**把 total_score / level 两列同步成评分 SSOT 的值。
    #    这不是"顺手改分":funnel 由 dimension_stats 决定,而本脚本一个单元格都不改
    #    → 对一份本来就自洽的记录,同步前后逐字相同;真变了只说明那两列**本来就**
    #    与 SSOT 不一致(例如历史上写进过非规范等级字符串),纠正它是对的。
    #    判据把这条不变式钉死:同步后的值必须 == 评分 SSOT 对该分数给出的值。
    #    brands.latest_score 本脚本确实一个字不动(与重建脚本不同,那边会同步)。
    result["report_rebuilt"] = report_rebuilt
    result["report_error"] = report_error
    return result


def _cells_fingerprint(ai: dict) -> str:
    """单元格状态指纹 —— 只取会影响判定的字段,用来自证"没改格子"。"""
    import hashlib

    parts: list[str] = []
    for item in ai.get("detail_table") or []:
        if not isinstance(item, dict):
            continue
        for engine, cell in sorted((item.get("results") or {}).items()):
            if not isinstance(cell, dict):
                continue
            parts.append(
                "|".join(
                    [
                        str(item.get("question", "")),
                        str(engine),
                        str(cell.get("brand_verdict", "")),
                        str(cell.get("brand_detected", "")),
                        str(cell.get("detection_reason", "")),
                        str(cell.get("matched_text", "")),
                        str(cell.get("identity_review_state", "")),
                    ]
                )
            )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", required=True, help="逗号分隔;**必传**,没有默认名单")
    parser.add_argument("--apply", action="store_true", help="真写库(默认只 dry-run)")
    args = parser.parse_args()
    ids = [int(x) for x in str(args.ids).split(",") if str(x).strip()]
    if not ids:
        print("[ABORT] --ids 解析为空")
        return 2

    # 死锁不变式①:装配链在开事务之前导入完
    _preload_report_chain()

    from db.connection import get_connection

    ok = 0
    failed = 0
    for did in ids:
        conn = get_connection()
        try:
            res = backfill_one(conn, did, apply=args.apply)
            if args.apply and res.get("status") == "backfilled":
                conn.commit()  # 死锁不变式③:逐份提交
                ok += 1
            else:
                conn.rollback()
                if args.apply:
                    failed += 1
            print(json.dumps(res, ensure_ascii=False, default=str))
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            failed += 1
            print(json.dumps(
                {"diagnosis_id": did, "status": "error", "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ))
        finally:
            conn.close()

    print(f"[{'APPLIED' if args.apply else 'DRY-RUN'}] committed={ok} failed={failed} total={len(ids)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
