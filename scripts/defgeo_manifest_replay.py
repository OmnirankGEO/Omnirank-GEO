"""全 manifest 迁移**重放 + schema 快照**工具(索引守卫改写的等价性证据)。

用途:把 459 条 ``CREATE [UNIQUE] INDEX IF NOT EXISTS`` 换成表绑定守卫,
唯一能证明"没改坏"的办法不是读 diff,是**在真 PG16 上跑一遍看 schema 一模一样**。

  · ``replay``   —— 在指定库上按 manifest 顺序重放全部迁移(复刻 prestart 的
                    ``_apply`` 语义:剥 psql ``\\`` 元命令、剥裸 BEGIN/COMMIT、
                    每个文件一次 ``cur.execute``、任何报错向上抛)。
  · ``snapshot`` —— 导出 schema 指纹(表 / 列 / 索引定义 / 约束定义 / 触发器),
                    **索引按 (表, 索引名) 键**,这样"索引长错表"会体现在指纹里。

CLI::

    python scripts/defgeo_manifest_replay.py replay   <DSN> [--rounds 2]
    python scripts/defgeo_manifest_replay.py snapshot <DSN> --out fp.json

🔴 只对一次性测试库用。库名安全栓:必须含 ``test``(与本仓其余判据同栓)。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.migration_manifest import MIGRATIONS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _assert_throwaway(dsn: str) -> None:
    db = dsn.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in db.lower():
        raise SystemExit(f"安全栓:库名 {db!r} 不含 'test' —— 拒绝在可能是真库的地方跑")


def _prepare(sql: str) -> str:
    """与 ``scripts/prestart.py::_apply`` 逐字同构的预处理。"""
    sql = re.sub(r"^\s*\\[a-zA-Z_]+.*$", "", sql, flags=re.MULTILINE)
    sql = re.sub(r"^\s*(BEGIN|COMMIT)\s*;\s*$", "", sql, flags=re.MULTILINE | re.IGNORECASE)
    return sql


def replay(dsn: str, rounds: int = 1, stop_on_error: bool = True) -> list[dict]:
    _assert_throwaway(dsn)
    log: list[dict] = []
    for rnd in range(1, rounds + 1):
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute("SET statement_timeout TO '300s'")
                for rel in MIGRATIONS:
                    path = ROOT / rel
                    if not path.exists():
                        raise RuntimeError(f"迁移文件缺失: {rel}")
                    sql = _prepare(path.read_text(encoding="utf-8"))
                    try:
                        cur.execute(sql)
                        log.append({"round": rnd, "file": rel, "ok": True})
                    except Exception as exc:  # noqa: BLE001
                        log.append({
                            "round": rnd, "file": rel, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}".strip()[:600],
                        })
                        if stop_on_error:
                            return log
                        conn.rollback()
        finally:
            conn.close()
    return log


_SNAPSHOT_SQL = {
    # 索引按 (表, 索引名) 键 —— "索引长在别的表上"会让键变化,指纹当场不同。
    "indexes": """
        SELECT n.nspname || '.' || t.relname AS tbl, c.relname AS idx,
               pg_get_indexdef(c.oid) AS def
          FROM pg_class c
          JOIN pg_index i ON i.indexrelid = c.oid
          JOIN pg_class t ON t.oid = i.indrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('i', 'I')
           -- 🔴 'I' = 分区索引。写死 'i' 时它不进指纹 ⇒ 「分区索引被删/长错表」
           --    这一整类在等价性证据里是隐形的(独立审计 2026-08-24 实测点出)。
         ORDER BY 1, 2
    """,
    "constraints": """
        SELECT n.nspname || '.' || t.relname AS tbl, c.conname AS name,
               pg_get_constraintdef(c.oid) AS def
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = 'public'
         ORDER BY 1, 2
    """,
    "columns": """
        SELECT table_name, column_name, data_type, is_nullable,
               COALESCE(column_default, '') AS dflt
          FROM information_schema.columns
         WHERE table_schema = 'public'
         ORDER BY 1, 2
    """,
    "tables": """
        SELECT table_name FROM information_schema.tables
         WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
         ORDER BY 1
    """,
    "triggers": """
        SELECT c.relname AS tbl, t.tgname AS name, pg_get_triggerdef(t.oid) AS def
          FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE NOT t.tgisinternal AND n.nspname = 'public'
         ORDER BY 1, 2
    """,
}


def snapshot(dsn: str) -> dict:
    conn = psycopg2.connect(dsn)
    try:
        out: dict[str, list] = {}
        with conn.cursor() as cur:
            for key, q in _SNAPSHOT_SQL.items():
                cur.execute(q)
                out[key] = [list(r) for r in cur.fetchall()]
        conn.rollback()
    finally:
        conn.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["replay", "snapshot"])
    ap.add_argument("dsn")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--out")
    ap.add_argument("--keep-going", action="store_true")
    a = ap.parse_args()

    if a.cmd == "replay":
        log = replay(a.dsn, a.rounds, stop_on_error=not a.keep_going)
        bad = [e for e in log if not e["ok"]]
        print(json.dumps({"applied": len(log), "failed": len(bad), "errors": bad[:20]},
                         ensure_ascii=False, indent=2))
        return 1 if bad else 0

    fp = snapshot(a.dsn)
    text = json.dumps(fp, ensure_ascii=False, indent=1, sort_keys=True)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(json.dumps({k: len(v) for k, v in fp.items()}, ensure_ascii=False))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
