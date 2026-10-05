"""
build_trust_asset_snapshots — P0-D 信任资产快照回填脚本(2026-06-14)

用途(设计 §8 批 D0):对存量品牌(有诊断/监测)回填 brand_trust_asset_snapshot。
  复用 services.source_authority_analyzer.aggregate_source_authority(监测真实引用 + 诊断 detail_table 引用)
  + 品牌声明(官网 / authority_sources)→ tools.trust_asset_collector → db.trust_asset_db.save_trust_snapshot。

⚠️ 这是 Deploy 期 ops(部署后跑)· 报价默认 flag 关 → 即使回填了快照也 0 报价变化(消费要 D2 开 flag)。
   live 诊断链路的实时采集走 flag PRICING_P0D_TRUST_COLLECT_ENABLED(诊断 hook · 见 workflows)。

跑法:
  python scripts/build_trust_asset_snapshots.py [--limit N] [--brand-id ID] [--include-test] [--dry-run]

红线:只 INSERT brand_trust_asset_snapshot(append-only latest active)· 不碰任何既有表 · 失败逐品牌跳过。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _find_detail_table(obj, depth: int = 0):
    """递归找 detail_table(list)· 兼容 report 存储结构漂移(diagnosis_data.ai_visibility_data.detail_table)。"""
    if depth > 6 or obj is None:
        return None
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except Exception:
            return None
    if isinstance(obj, dict):
        dt = obj.get("detail_table")
        if isinstance(dt, list):
            return dt
        for v in obj.values():
            r = _find_detail_table(v, depth + 1)
            if r is not None:
                return r
    return None


def _load_detail_table(latest_diagnosis_id):
    if not latest_diagnosis_id:
        return None
    try:
        from db.diagnosis_db import get_diagnosis_by_id
        rec = get_diagnosis_by_id(int(latest_diagnosis_id))
        if not rec:
            return None
        for key in ("report_v2_modules_jsonb", "data", "raw_data_json"):
            dt = _find_detail_table(rec.get(key))
            if dt:
                return dt
    except Exception as exc:
        print(f"    detail_table 读取失败 diag={latest_diagnosis_id}: {exc}")
    return None


def _iter_brands(limit, brand_id, include_test):
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        where = []
        params = []
        if brand_id:
            where.append("id = %s")
            params.append(int(brand_id))
        if not include_test:
            where.append("(is_test IS NULL OR is_test = FALSE)")
        sql = "SELECT id, name, latest_diagnosis_id FROM brands"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        cur.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--brand-id", type=int, default=None)
    ap.add_argument("--include-test", action="store_true", help="包含测试品牌(默认排除 is_test)")
    ap.add_argument("--dry-run", action="store_true", help="只算不写库")
    args = ap.parse_args()

    from db.trust_asset_db import init_trust_asset_table, save_trust_snapshot
    from tools.trust_asset_collector import collect_trust_assets

    if not args.dry_run:
        init_trust_asset_table()

    brands = _iter_brands(args.limit, args.brand_id, args.include_test)
    print(f"待回填品牌 {len(brands)} 个(dry_run={args.dry_run} · include_test={args.include_test})")

    stats = {"collected": 0, "missing": 0, "needs_review": 0, "saved": 0, "fail": 0}
    for b in brands:
        bid, bname = b["id"], b.get("name")
        try:
            detail = _load_detail_table(b.get("latest_diagnosis_id"))
            payload = collect_trust_assets(bid, diagnosis_detail_table=detail)
            if payload is None:
                stats["fail"] += 1
                continue
            src = payload.get("trust_asset_source")
            stats[src] = stats.get(src, 0) + 1
            if payload.get("trust_asset_needs_review"):
                stats["needs_review"] += 1
            score = payload.get("trust_asset_score")
            ready = payload.get("citation_readiness_score")
            nver = len(payload.get("verified_trust_assets") or [])
            print(f"  brand {bid} {bname!r}: source={src} score={score} readiness={ready} "
                  f"verified={nver} review={payload.get('trust_asset_needs_review')}")
            if not args.dry_run:
                new_id = save_trust_snapshot(bid, payload)
                if new_id:
                    stats["saved"] += 1
        except Exception as exc:
            stats["fail"] += 1
            print(f"  brand {bid} {bname!r}: 失败 {exc}")

    print("\n=== 汇总 ===")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print("\n完成。flag PRICING_P0D_TRUST_ASSET_ENABLED 默认关 → 回填快照不影响报价(消费需 D2 开 flag)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
