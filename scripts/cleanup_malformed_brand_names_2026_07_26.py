#!/usr/bin/env python
"""存量畸形品牌名清洗 + 受影响 0 分报告清单（P0-1 ③ · 2026-07-26）。

生产实证：``brands.id=278`` 的 name 是 ``深圳驰鲸科技\\n\\n城市:深圳``（含换行 +
"城市:" 标签串）→ 品牌识别恒不命中 → 诊断 456 / 468 都是 0 分，客户付费两次拿废报告。
换成干净名（``brands.id=712`` "深圳市驰鲸科技有限公司"）后立刻得 20 分。

本脚本做两件事，**都不自动退款**：

1. ``--apply``：把畸形品牌名清洗成建议名（幂等；已干净的行 0 改动）。
   - 清洗只用 ``utils.brand_name_hygiene.suggest_brand_name``，抽不出合法名就**跳过并报告**，
     绝不编造名字；
   - 名字全局唯一，撞名则跳过并报告（不静默改归属、不合并品牌）；
   - 每一次改名落 ``brand_name_hygiene_audit``（append-only），旧值可回溯。
2. 无论是否 ``--apply``，都输出**受影响的 0 分诊断清单**（品牌 / 诊断 id / 时间 / 分数），
   交 Owner 决定处置（退款走 admin 工单制，脚本不动钱）。

用法::

    python scripts/cleanup_malformed_brand_names_2026_07_26.py            # 只读 dry-run
    python scripts/cleanup_malformed_brand_names_2026_07_26.py --apply    # 执行清洗

只读模式零写入。``--apply`` 每行独立 SAVEPOINT，单行失败不拖累其余行，
汇总里如实计回退数（不报"已清洗 N 篇"而库里一字未改）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.brand_name_hygiene import (  # noqa: E402
    is_malformed_brand_name,
    suggest_brand_name,
)

AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS public.brand_name_hygiene_audit (
    id SERIAL PRIMARY KEY,
    brand_id INTEGER NOT NULL,
    old_name TEXT NOT NULL,
    new_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


_OPTIONAL_BRAND_COLUMNS = ("owner_user_id", "brand_type", "is_test", "is_deleted")


def _fetch_brands(cur) -> list[dict]:
    """只强依赖 id/name；其余列按实际 schema 存在与否动态取。

    清洗脚本不该因为某个环境缺一个无关列就整个跑不起来（会让人以为"没有畸形名"）。
    """
    cur.execute(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'brands'
        """
    )
    present = {
        (row["column_name"] if isinstance(row, dict) else row[0])
        for row in (cur.fetchall() or [])
    }
    extra = [col for col in _OPTIONAL_BRAND_COLUMNS if col in present]
    columns = ", ".join(["id", "name", *extra])
    cur.execute(f"SELECT {columns} FROM public.brands ORDER BY id")
    return [dict(row) for row in (cur.fetchall() or [])]


def _zero_score_diagnoses(cur, brand_ids: list[int]) -> list[dict]:
    if not brand_ids:
        return []
    try:
        cur.execute(
            """
            SELECT id, brand_id, brand_name, total_score, level, created_at
              FROM public.diagnosis_records
             WHERE brand_id = ANY(%s)
               AND COALESCE(total_score, 0) = 0
             ORDER BY brand_id, id
            """,
            (brand_ids,),
        )
    except Exception as exc:  # 表/列缺失时如实报告，不假装"没有受影响报告"
        print(f"[warn] 0 分报告清单读取失败({type(exc).__name__})，清单不完整", file=sys.stderr)
        return []
    return [dict(row) for row in (cur.fetchall() or [])]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="执行清洗（缺省为只读 dry-run）")
    args = parser.parse_args()

    from db.connection import get_connection

    conn = get_connection()
    cleaned: list[dict] = []
    skipped: list[dict] = []
    rolled_back: list[dict] = []
    try:
        cur = conn.cursor()
        brands = _fetch_brands(cur)
        malformed = [b for b in brands if is_malformed_brand_name(b["name"])]

        existing_names = {str(b["name"]) for b in brands}

        if args.apply and malformed:
            cur.execute(AUDIT_DDL)
            conn.commit()

        for brand in malformed:
            brand_id = int(brand["id"])
            old_name = str(brand["name"])
            suggestion = suggest_brand_name(old_name)
            if not suggestion:
                skipped.append({**brand, "skip_reason": "no_safe_suggestion"})
                continue
            if suggestion != old_name and suggestion in existing_names:
                # 品牌名全局唯一；撞名说明可能是重复品牌（P1-11 的场景）。
                # 这里**不合并、不改归属**，只报告，交人工用重复品牌入口处理。
                skipped.append({**brand, "skip_reason": "name_conflict", "suggestion": suggestion})
                continue

            if not args.apply:
                cleaned.append({"brand_id": brand_id, "old_name": old_name, "new_name": suggestion})
                continue

            try:
                cur.execute("SAVEPOINT brand_hygiene")
                cur.execute(
                    """
                    UPDATE public.brands
                       SET name = %s, updated_at = CURRENT_TIMESTAMP
                     WHERE id = %s AND name = %s
                    """,
                    (suggestion, brand_id, old_name),
                )
                changed = cur.rowcount
                if changed != 1:
                    # 幂等：已被别人清过 / 并发改名 → 不重试不猜
                    cur.execute("ROLLBACK TO SAVEPOINT brand_hygiene")
                    skipped.append({**brand, "skip_reason": f"unexpected_rowcount:{changed}"})
                    continue
                cur.execute(
                    """
                    INSERT INTO public.brand_name_hygiene_audit
                        (brand_id, old_name, new_name, reason)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (brand_id, old_name, suggestion, "P0-1 malformed brand name cleanup 2026-07-26"),
                )
                cur.execute("RELEASE SAVEPOINT brand_hygiene")
                conn.commit()
                existing_names.discard(old_name)
                existing_names.add(suggestion)
                cleaned.append({"brand_id": brand_id, "old_name": old_name, "new_name": suggestion})
            except Exception as exc:
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT brand_hygiene")
                    conn.commit()
                except Exception:
                    conn.rollback()
                rolled_back.append({
                    "brand_id": brand_id,
                    "old_name": old_name,
                    "new_name": suggestion,
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                })

        affected_ids = [int(b["id"]) for b in malformed]
        zero_reports = _zero_score_diagnoses(cur, affected_ids)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "malformed_found": len(malformed),
        "cleaned": cleaned,
        "cleaned_count": len(cleaned),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "rolled_back": rolled_back,
        "rolled_back_count": len(rolled_back),
        # 交 Owner 的处置清单：脚本不动钱、不自动退款（退款走 admin 工单制）。
        "zero_score_reports_for_owner_review": [
            {
                "diagnosis_id": row["id"],
                "brand_id": row["brand_id"],
                "brand_name_snapshot": row.get("brand_name"),
                "total_score": row.get("total_score"),
                "level": row.get("level"),
                "created_at": str(row.get("created_at")),
            }
            for row in zero_reports
        ],
        "zero_score_reports_count": len(zero_reports),
        "note": (
            "本脚本只清洗品牌名并出清单，不退款、不改归属、不合并品牌。"
            "0 分报告的处置（重测 / 退费）由 Owner 决定，退款走 admin 工单制。"
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if rolled_back else 0


if __name__ == "__main__":
    raise SystemExit(main())
