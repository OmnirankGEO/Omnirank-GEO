"""[WO-KYB-ROUTING D2] 等价映射 dry-run + 独有清单 —— **只读**,给 Deploy 随车跑。

用法(在应用容器内跑,别在宿主跑 —— 宿主没有 DATABASE_URL 与业务模块):

    docker exec -i omnirank-blue python - < scripts/d2_equivalence_dryrun.py

🔴 只读保证(三条,缺一不可):
  1. `rebuild_table(..., dry_run=True)` —— 该函数 dry_run 分支不执行任何 INSERT/UPDATE;
  2. 本脚本自己**一条写语句都没有**;
  3. 跑之前先做一次"必失败的写"自证连接确实是只读意图(见 `_prove_readonly`)——
     它**不改变**连接的读写属性,只是让"我以为是只读"这句话有个可证伪的实证。

输出落到 stdout(JSON),Deploy 原样贴回即可。**不落盘、不发外部。**
"""
import json
import sys

sys.path.insert(0, "/app")   # 容器内业务模块根;不加的话 sys.path 首位是 '' 或 /tmp


def _prove_readonly() -> str:
    """自证:本轮不打算写库。用一条注定失败的语句留痕。

    🔴 这不是"保证",是**可证伪的实证** —— 如果它没报错,说明我对连接的假设错了,
    整轮取数作废(而不是"大概没事")。
    """
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        try:
            cur.execute("UPDATE mhz_config SET value = value WHERE key = '__d2_probe__'")
            return "🔴 只读自证失败:那条 UPDATE 没报错 → 本轮取数作废"
        except Exception as exc:
            conn.rollback()
            return f"只读自证 ✅ 写被拒:{type(exc).__name__}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _all_kyb_ids(table: str) -> list[int]:
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM {table} WHERE provider = 'kyb'")
        return [int(r["id"]) for r in cur.fetchall()]
    finally:
        conn.close()


def _approved_rows(table: str) -> list[dict]:
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT kyb_media_id, mhz_media_id, mhz_price, kyb_price,"
            "       preferred_provider, confidence"
            "  FROM media_provider_equivalence"
            " WHERE table_name = %s AND is_enabled IS TRUE", (table,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def main() -> int:
    from services.media_provider_equivalence import (
        ARTICLE_TABLE, WEMEDIA_TABLE, rebuild_table,
    )
    from services.media_provider_equivalence_audit import (
        check_partition_invariants, check_preferred_provider_rows,
        partition_kyb_catalog, sample_for_review, summarize,
    )

    report: dict = {"readonly_probe": _prove_readonly(), "tables": {}}
    for table in (ARTICLE_TABLE, WEMEDIA_TABLE):
        res = rebuild_table(table, dry_run=True)
        auto = res.get("auto_rows_preview") or res.get("auto_rows") or []
        human = res.get("human_rows") or []

        all_kyb = _all_kyb_ids(table)
        approved = _approved_rows(table)
        part = partition_kyb_catalog(all_kyb, approved)

        report["tables"][table] = {
            "stats_from_rebuild": res.get("stats", res),
            "summary": summarize(auto, human, part),
            # 🔴 两组不变量校验:空 = 全过。非空**必须**在交付说明里如实贴出来。
            "invariant_problems": check_partition_invariants(all_kyb, part),
            "preferred_problems": check_preferred_provider_rows(approved)[:20],
            "sample_sizes": {k: len(v) for k, v in sample_for_review(auto).items()},
            "kyb_total": len(all_kyb),
            "approved_pairs": len(approved),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
