"""索引守卫改写的 **A/B 等价性台架**(warm / cold 两臂 + 毒夹具臂)。

改写 459 条 ``CREATE [UNIQUE] INDEX IF NOT EXISTS`` 之后,"没改坏"不能靠读 diff,
只能靠**在真 PG16 上跑出来的 schema 指纹逐字相同**。

三个臂各自回答一个不同的问题:

  ``warm``   生产形状库直接重放 —— 覆盖**幂等跳过**路径(生产每次部署走的就是这条)。
  ``cold``   生产形状库先把普查里能删的索引全删掉再重放 —— 逼**建索引**分支全部执行。
             没有这一臂,459 条里只有 62 条的 CREATE 分支被跑到,等价性证明是空的。
  ``poison`` 先在**别的表**上造同名索引再重放 —— 父提交臂必须复现"静默跳过",
             本包臂必须 RAISE。这是证明"修的是真病"的那一臂。

用法::

    python scripts/defgeo_index_ab_harness.py warm   <DSN> --out fp_warm.json
    python scripts/defgeo_index_ab_harness.py cold   <DSN> --out fp_cold.json
    python scripts/defgeo_index_ab_harness.py poison <DSN> --index <idx> --table <t>
    python scripts/defgeo_index_ab_harness.py diff   a.json b.json

🔴 只对一次性测试库用(库名安全栓:必须含 ``test``)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.defgeo_index_guard_scan import (  # noqa: E402
    manifest_sql_files,
    scan_declared_indexes,
)
from scripts.defgeo_manifest_replay import replay, snapshot  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

#: 毒夹具用的诱饵表名。刻意用不会与任何业务表撞名的前缀。
DECOY_TABLE = "zz_idxguard_decoy"


def _assert_throwaway(dsn: str) -> None:
    db = dsn.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in db.lower():
        raise SystemExit(f"安全栓:库名 {db!r} 不含 'test'")


def census() -> list:
    out = []
    for p in manifest_sql_files(ROOT):
        out += scan_declared_indexes(p.read_text(encoding="utf-8"),
                                     p.relative_to(ROOT).as_posix())
    return out


def _conn(dsn: str):
    c = psycopg2.connect(dsn)
    c.autocommit = True
    return c


def drop_census_indexes(dsn: str) -> dict:
    """把普查里的索引尽量删光(约束背书的删不掉,如实记下来)。"""
    _assert_throwaway(dsn)
    dropped, kept = [], []
    conn = _conn(dsn)
    try:
        with conn.cursor() as cur:
            for s in census():
                cur.execute(
                    "SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid "
                    " WHERE c.relname=%s AND i.indrelid = to_regclass(%s)",
                    (s.index, f"public.{s.table}"))
                if cur.fetchone() is None:
                    continue
                try:
                    cur.execute(f'DROP INDEX public."{s.index}"')
                    dropped.append(s.index)
                except Exception as exc:  # 约束背书的索引 → 删不掉,如实记
                    kept.append({"index": s.index, "why": str(exc).strip()[:120]})
    finally:
        conn.close()
    return {"dropped": len(dropped), "kept": len(kept), "kept_detail": kept[:40]}


def plant_decoy(dsn: str, index: str, unique: bool = False) -> None:
    """在诱饵表上造一个同名索引 —— 老形态会被它骗成"已存在"。"""
    _assert_throwaway(dsn)
    conn = _conn(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE TABLE IF NOT EXISTS public.{DECOY_TABLE} (a int, b int)")
            cur.execute(f'DROP INDEX IF EXISTS public."{index}"')
            kind = "UNIQUE " if unique else ""
            cur.execute(f'CREATE {kind}INDEX "{index}" ON public.{DECOY_TABLE} (a)')
    finally:
        conn.close()


def where_does_index_live(dsn: str, index: str) -> str | None:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.relname FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid "
                "  JOIN pg_class t ON t.oid=i.indrelid "
                " WHERE c.relname=%s AND c.relnamespace='public'::regnamespace", (index,))
            row = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()
    return row[0] if row else None


def _fp(dsn: str) -> dict:
    return snapshot(dsn)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["warm", "cold", "poison", "diff", "census", "drop"])
    ap.add_argument("dsn", nargs="?")
    ap.add_argument("--out")
    ap.add_argument("--index")
    ap.add_argument("--table")
    ap.add_argument("--unique", action="store_true")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("b", nargs="?")
    a = ap.parse_args()

    if a.cmd == "census":
        c = census()
        print(json.dumps({"count": len(c),
                          "items": [[s.path, s.index, s.table, s.unique] for s in c]},
                         ensure_ascii=False, indent=1))
        return 0

    if a.cmd == "diff":
        x = json.loads(Path(a.dsn).read_text(encoding="utf-8"))
        y = json.loads(Path(a.b).read_text(encoding="utf-8"))
        report = {}
        for k in sorted(set(x) | set(y)):
            xs = {tuple(r) for r in x.get(k, [])}
            ys = {tuple(r) for r in y.get(k, [])}
            report[k] = {"only_in_a": sorted(map(list, xs - ys))[:30],
                         "only_in_b": sorted(map(list, ys - xs))[:30],
                         "n_a": len(xs), "n_b": len(ys)}
        same = all(not v["only_in_a"] and not v["only_in_b"] for v in report.values())
        print(json.dumps({"identical": same, "detail": report}, ensure_ascii=False, indent=1))
        return 0 if same else 1

    _assert_throwaway(a.dsn)

    if a.cmd == "drop":
        print(json.dumps(drop_census_indexes(a.dsn), ensure_ascii=False, indent=1))
        return 0

    if a.cmd == "poison":
        plant_decoy(a.dsn, a.index, a.unique)
        before = where_does_index_live(a.dsn, a.index)
        log = replay(a.dsn, 1, stop_on_error=False)
        bad = [e for e in log if not e["ok"]]
        after = where_does_index_live(a.dsn, a.index)
        print(json.dumps({
            "index": a.index, "target_table": a.table,
            "lives_before_replay": before, "lives_after_replay": after,
            "replay_failures": len(bad),
            "failure_signatures": [f"{e['file']} :: {e['error'][:220]}" for e in bad][:10],
            "silently_skipped": (not bad) and after != a.table,
        }, ensure_ascii=False, indent=1))
        return 0

    if a.cmd == "cold":
        print(json.dumps(drop_census_indexes(a.dsn), ensure_ascii=False), file=sys.stderr)

    log = replay(a.dsn, a.rounds, stop_on_error=True)
    bad = [e for e in log if not e["ok"]]
    if bad:
        print(json.dumps({"replay_failed": bad}, ensure_ascii=False, indent=1))
        return 1
    fp = _fp(a.dsn)
    if a.out:
        Path(a.out).write_text(json.dumps(fp, ensure_ascii=False, indent=1, sort_keys=True),
                               encoding="utf-8")
    print(json.dumps({"applied": len(log), **{k: len(v) for k, v in fp.items()}},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
