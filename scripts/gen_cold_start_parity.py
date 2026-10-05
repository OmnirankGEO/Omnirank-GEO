# -*- coding: utf-8 -*-
"""生成 db/cold_start_parity.sql:空库冷启动与生产 schema 的对齐基线(E10 · Review 09-28 AO 单)。

为什么要它:本仓一部分表 / 列只由**不在 migration_manifest 里的历史 SQL / 手工 ALTER** 建出来,
生产上早就有,空库冷启动(prestart + import server)之后没有 —— 在役代码一碰就报
`column "…" does not exist` / `relation "…" does not exist`(例:api/selection_api.py 确认报价
`UPDATE confirmed_keywords … RETURNING id, brand_id`,冷库上没有 brand_id)。

怎么生成(只读两个一次性库,不碰生产):
  1. 冷库:空库 → `python -m scripts.prestart` → `import server`(与 C 的 selfcheck 对照臂同形);
  2. 生产形状库:灌 .deploy_toolkit 的生产 schema 快照 → 同一棵树 prestart;
  3. 两边比 public 下的表与列:
     · 缺的**表**:只收非测试后端代码(.py)里有 SQL 读写引用的(FROM/INTO/UPDATE/JOIN/DELETE FROM/TABLE);
       没有任何引用的(归档 / 备份 / 一次性快照表)记为「未用」不收;
       DDL 用 `pg_dump --schema-only -t` 从生产形状库取(建表 + 序列 + 约束 + 索引),
       去掉 OWNER / GRANT / COMMENT(生产注释可能写着内部排障细节,不进开源树);
     · 缺的**列**(表两边都有):全收,`ALTER TABLE … ADD COLUMN IF NOT EXISTS`,类型 / 默认值照生产。
     · 类型差不自动改,只进报告。
用法:python scripts/gen_cold_start_parity.py --cold-url … --prod-url … --container … --out db/cold_start_parity.sql
     --report <json>(对比明细)
执行时机:scripts/prestart.py 在**开跑时是空库**(哨兵 users 不在)的情况下,跑完 manifest 之后执行本文件;
生产库有 users ⇒ 一步不走(空操作)。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLS_SQL = ("SELECT c.table_name, c.column_name, c.data_type, c.udt_name, "
            "coalesce(c.character_maximum_length::text,''), c.is_nullable, coalesce(c.column_default,''), "
            "pg_catalog.format_type(a.atttypid, a.atttypmod) "
            "FROM information_schema.columns c "
            "JOIN information_schema.tables t ON t.table_schema=c.table_schema AND t.table_name=c.table_name "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid = ('public.' || quote_ident(c.table_name))::regclass "
            "  AND a.attname = c.column_name "
            "WHERE c.table_schema='public' AND t.table_type='BASE TABLE' ORDER BY 1, c.ordinal_position")


def psql(container: str, db: str, sql: str) -> str:
    return subprocess.run(["docker", "exec", container, "psql", "-U", "postgres", "-d", db, "-AtF", "\t", "-c", sql],
                          capture_output=True, text=True, encoding="utf-8", check=True).stdout


def columns(container: str, db: str) -> dict:
    res: dict = {}
    for line in psql(container, db, COLS_SQL).splitlines():
        t, c, dt, udt, ln, nul, dflt, fmt = line.split("\t")
        res.setdefault(t, {})[c] = {"udt": udt, "len": ln, "nullable": nul, "default": dflt, "format": fmt}
    return res


def code_texts() -> dict:
    files = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    out = {}
    for f in files:
        if f.startswith(("tests/", "docs/", "frontend/")) or "/tests/" in f:
            continue
        try:
            out[f] = (ROOT / f).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            pass
    return out


def sql_users(table: str, texts: dict) -> list:
    rx = re.compile(r"(?i)\b(FROM|INTO|UPDATE|JOIN|TABLE(?:\s+IF\s+(?:NOT\s+)?EXISTS)?)\s+(?:public\.)?\"?%s\"?\b"
                    % re.escape(table))
    return sorted(f for f, s in texts.items() if rx.search(s))


_DROP_LINE = re.compile(r"^(ALTER\s+\S+.*\s+OWNER\s+TO\b|GRANT\b|REVOKE\b|COMMENT\s+ON\b|SET\b|SELECT\s+pg_catalog\.set_config|"
                        r"\\restrict|\\unrestrict|--)", re.I)


def dump_tables(container: str, db: str, tables: list) -> str:
    args = ["docker", "exec", container, "pg_dump", "-U", "postgres", "-d", db, "--schema-only", "--no-owner",
            "--no-privileges", "--no-comments"]
    for t in tables:
        args += ["-t", "public.%s" % t]
    raw = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", check=True).stdout
    # 逐条语句过滤(pg_dump 一条语句可跨多行,以 ; 结尾)
    stmts, cur = [], []
    for line in raw.splitlines():
        if not cur and (not line.strip() or _DROP_LINE.match(line.strip())):
            continue
        cur.append(line)
        if line.rstrip().endswith(";"):
            stmts.append("\n".join(cur))
            cur = []
    return "\n\n".join(stmts) + "\n"


#: 类型差里**对类型敏感**、要在冷库对齐成生产类型的列(Review 09-28 WO_318 定法:在役读者对类型敏感才改,逐行给证据)。
#: 其余类型差只进报告(长度 / 时区 / text、或读者不敏感)。
#: 这三列仓内建表(db/diagnosis_db.py)写 BOOLEAN,生产是 smallint;在役代码按生产写整数 ——
#: 冷库上 boolean = integer 没有运算符、整数也不能写进 boolean 列,员工 / 部门 / 会议相关读写直接报错。
TYPE_ALIGN: dict[tuple[str, str], str] = {
    ("employee_configs", "is_active"):
        "db/diagnosis_db.get_employee_configs `AND e.is_active = 1`;employees/employee_registry.py `AND is_active = 1`;"
        "upsert 写 `int(is_active)`",
    ("departments", "is_active"):
        "db/diagnosis_db `UPDATE departments SET is_active = 0 …`",
    ("employee_meetings", "is_internal"):
        "db/diagnosis_db 建会议写 `int(is_internal)`;列表查询 `m.is_internal = %s` 传 `int(is_internal)`",
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", required=True)
    ap.add_argument("--cold-db", required=True)
    ap.add_argument("--prod-db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", required=True)
    a = ap.parse_args(argv)
    cold, prod = columns(a.container, a.cold_db), columns(a.container, a.prod_db)
    texts = code_texts()
    missing_tables = sorted(set(prod) - set(cold))
    table_rows, used_tables = [], []
    for t in missing_tables:
        users = sql_users(t, texts)
        table_rows.append({"table": t, "status": "在役读写" if users else "未用", "code_users": users})
        if users:
            used_tables.append(t)
    col_rows, type_rows, alters = [], [], []
    for t in sorted(set(prod) & set(cold)):
        for c, p in prod[t].items():
            k = cold[t].get(c)
            if k is None:
                rx_c = re.compile(r"\b%s\b" % re.escape(c))
                rx_t = re.compile(r"\b%s\b" % re.escape(t))
                users = sorted(f for f, s in texts.items() if rx_t.search(s) and rx_c.search(s))
                col_rows.append({"table": t, "column": c, "prod_type": p["format"], "prod_default": p["default"],
                                 "status": "在役读写(文件同现)" if users else "未用", "code_users": users})
                ddl = 'ALTER TABLE public.%s ADD COLUMN IF NOT EXISTS "%s" %s' % (t, c, p["format"])
                if p["default"]:
                    ddl += " DEFAULT %s" % p["default"]
                alters.append(ddl + ";")
            elif (k["udt"], k["len"]) != (p["udt"], p["len"]):
                aligned = (t, c) in TYPE_ALIGN
                type_rows.append({"table": t, "column": c, "cold": k["format"], "prod": p["format"],
                                  "action": "对齐为生产类型" if aligned else "已知 · 不改",
                                  "evidence": TYPE_ALIGN.get((t, c), "")})
                if aligned:
                    alters.append('ALTER TABLE public.%s ALTER COLUMN "%s" DROP DEFAULT;' % (t, c))
                    alters.append('ALTER TABLE public.%s ALTER COLUMN "%s" TYPE %s USING ("%s"::integer);'
                                  % (t, c, p["format"], c))
                    if p["default"]:
                        alters.append('ALTER TABLE public.%s ALTER COLUMN "%s" SET DEFAULT %s;' % (t, c, p["default"]))
    body = dump_tables(a.container, a.prod_db, used_tables) if used_tables else ""
    head = ("-- 由 scripts/gen_cold_start_parity.py 生成,勿手改(重新生成见该脚本抬头)。\n"
            "-- 只在空库冷启动时由 scripts/prestart.py 在 manifest 之后执行;生产库一步不走。\n"
            "-- 缺表 %d 张收 %d 张(在役读写),缺列 %d 个全收,类型差 %d 处里对齐 %d 列(TYPE_ALIGN,证据见生成器)。\n\n"
            % (len(missing_tables), len(used_tables), len(col_rows), len(type_rows), len(TYPE_ALIGN)))
    Path(a.out).write_text(head + body + "\n-- ── 缺列 / 类型对齐 ──\n" + "\n".join(alters) + "\n",
                           encoding="utf-8", newline="\n")
    json.dump({"missing_tables": table_rows, "missing_columns": col_rows, "type_diffs": type_rows},
              open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    stale = sorted(set(TYPE_ALIGN) - {(r["table"], r["column"]) for r in type_rows})
    if stale:
        print("TYPE_ALIGN 名单过期(这些列已不再有类型差,删掉或核对):%s" % stale, file=sys.stderr)
        return 3
    print("missing_tables=%d used=%d missing_cols=%d type_diffs=%d aligned_types=%d"
          % (len(missing_tables), len(used_tables), len(col_rows), len(type_rows), len(TYPE_ALIGN)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
