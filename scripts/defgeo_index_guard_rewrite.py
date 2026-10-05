"""把 manifest 里全部 ``CREATE [UNIQUE] INDEX IF NOT EXISTS`` 机械改写成**表绑定守卫**。

一次性改写脚本,但**留在仓里**:分母、模板、round-trip 自证都在这里,
Review 可以重跑它并核对"再跑一遍零改动"(幂等 ⇒ 改写确实是机械的,不是手抄的)。

改写后的三条腿(缺一条 :func:`defgeo_index_guard_scan.bound_guard_defects` 就报):
  · 同表已有 → ``NULL``(幂等跳过,与老形态行为一致)
  · **异表/异类同名 → RAISE**(老形态在这里静默跳过 —— 就是本单要修的病)
  · 都没有   → 真建索引

用法::

    python scripts/defgeo_index_guard_rewrite.py --check      # 只报,不写
    python scripts/defgeo_index_guard_rewrite.py --apply      # 就地改写
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.defgeo_index_guard_scan import (  # noqa: E402
    IndexStatement,
    bound_drop_guard_defects,
    bound_guard_defects,
    manifest_sql_files,
    scan_bound_index_guards,
    scan_ifne_index_statements,
)

ROOT = Path(__file__).resolve().parent.parent

_RAISE = """\
{i}    RAISE EXCEPTION '[index-guard] {idx} 已存在但不在 public.{tbl} 上(实际宿主:%)—— 拒绝静默跳过',
{i}        (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
{i}           LEFT JOIN pg_index i ON i.indexrelid = c.oid
{i}           LEFT JOIN pg_class t ON t.oid = i.indrelid
{i}          WHERE c.relname = '{idx}' AND c.relnamespace = 'public'::regnamespace)
{i}        USING ERRCODE = 'duplicate_object';"""

_BODY = """\
{i}IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
{i}            WHERE c.relname = '{idx}' AND i.indrelid = to_regclass('public.{tbl}')) THEN
{i}    NULL;  -- 已在 public.{tbl} 上 → 幂等跳过
{i}ELSIF EXISTS (SELECT 1 FROM pg_class c
{i}               WHERE c.relname = '{idx}' AND c.relnamespace = 'public'::regnamespace) THEN
{raise_}
{i}ELSE
{i}    CREATE {kw}INDEX {idx} ON public.{tbl} {body};
{i}END IF;"""


def _render(stmt: IndexStatement, indent: str) -> str:
    kw = "UNIQUE " if stmt.unique else ""
    kind = "unique" if stmt.unique else "plain"
    body = " ".join(stmt.body.split())
    inner_indent = indent + ("    " if not stmt.inside_dollar else "")
    core = _BODY.format(
        i=inner_indent, idx=stmt.index, tbl=stmt.table, kw=kw, body=body,
        raise_=_RAISE.format(i=inner_indent, idx=stmt.index, tbl=stmt.table),
    )
    anchor = f"{indent}-- @index-guard {stmt.index} ON {stmt.table} {kind}"
    if stmt.inside_dollar:
        block = f"{anchor}\n{core}"
    else:
        block = (f"{anchor}\n"
                 f"{indent}DO $idxguard$\n"
                 f"{indent}BEGIN\n"
                 f"{core}\n"
                 f"{indent}END $idxguard$;")
    # 替换从 ``CREATE`` 那个字符起 —— 行首的缩进原文里已经有了,首行不能再来一遍。
    return block[len(indent):] if indent else block


def _line_indent(text: str, pos: int) -> str:
    bol = text.rfind("\n", 0, pos) + 1
    return text[bol:pos] if text[bol:pos].strip() == "" else ""


def rewrite_text(sql: str, rel: str) -> tuple[str, int]:
    stmts = scan_ifne_index_statements(sql, rel)
    if not stmts:
        return sql, 0
    out = sql
    for s in sorted(stmts, key=lambda x: x.start, reverse=True):
        if "$idxguard$" in s.body or "$$" in s.body:
            raise AssertionError(f"{rel}::{s.index} 的索引体里出现 dollar 定界符 —— 拒绝机械改写")
        if not s.table or not s.index:
            raise AssertionError(f"{rel} 解析出空的索引名/表名 —— 拒绝改写")
        indent = _line_indent(out, s.start)
        out = out[: s.start] + _render(s, indent) + out[s.end:]
    return out, len(stmts)


def verify_roundtrip(before: str, after: str, rel: str) -> None:
    """改写必须**无信息损失**:新守卫解析出的三元组与老语句逐条相等。"""
    old = scan_ifne_index_statements(before, rel)
    left = scan_ifne_index_statements(after, rel)
    if left:
        raise AssertionError(f"{rel} 改写后仍残留 {len(left)} 条旧形态:{[s.index for s in left]}")
    new = scan_bound_index_guards(after, rel)
    old_keys = [(s.index, s.table, s.unique) for s in old]
    new_keys = [(s.index, s.table, s.unique) for s in new]
    if old_keys != new_keys:
        raise AssertionError(f"{rel} round-trip 不等:\n  old={old_keys}\n  new={new_keys}")
    for s in new:
        d = bound_guard_defects(s)
        if d:
            raise AssertionError(f"{rel}::{s.index} 新守卫缺腿:{d}")
    # 索引体逐字保留(只归一空白)
    for o, n in zip(old, new):
        want = " ".join(o.body.split())
        if want not in " ".join(n.body.split()):
            raise AssertionError(f"{rel}::{o.index} 索引体在改写中丢失:{want!r}")


# ══════════════════════════════════════════════════════════════════════════
# 轴C:DROP INDEX 表绑定守卫
# ══════════════════════════════════════════════════════════════════════════
# 🔴 解析一律**跟着 search_path 走**(``to_regclass('<表>')`` / ``current_schema()``),
#    不写死 public。老形态 ``DROP INDEX <名>`` 本来就是 search_path 相对的,写死 public
#    会改变语义 —— 独立审计实测:``migration_billing_deduction_idempotency`` 里那 4 条
#    ``CREATE INDEX`` 是**不带 schema 限定**的,4 条既有资金域判据把迁移装进
#    per-test schema 重放,写死 public 的守卫在那里三条腿全落空 ⇒ 第二遍 DuplicateTable。
#    生产 search_path 就是 public,所以线上行为不变;这样写只是不再多钉一层。
_DROP_RAISE = """\
{i}    RAISE EXCEPTION '[drop-index-guard] {idx} 不在 {tbl} 上(实际宿主:%)—— 拒绝删掉别的表的索引',
{i}        (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
{i}           LEFT JOIN pg_index i ON i.indexrelid = c.oid
{i}           LEFT JOIN pg_class t ON t.oid = i.indrelid
{i}          WHERE c.relname = '{idx}' AND c.relnamespace = current_schema()::regnamespace)
{i}        USING ERRCODE = 'wrong_object_type';"""

# 🔴 真删那一句带 ``IF EXISTS``(if-exists 站点):目录探测与 DROP 之间有 TOCTOU 窗口 ——
#    独立审计用"B 会话先 LOCK TABLE 逼 A 的 DROP 排队、确认入队后再删掉索引"确定性复现:
#    老形态打 NOTICE 后成功,不带 IF EXISTS 的新守卫抛 42704 ⇒ 整份迁移回滚 ⇒
#    prestart 非零退出 ⇒ deploy abort。表绑定由上面的 IF 条件负责,``IF EXISTS``
#    只负责"探测之后它自己没了"这一种,语义一点不损失。
#    strict 站点(034)老形态本来就没有 IF EXISTS,同一竞态下老形态同样 42704 ⇒ 保持裸删。
_DROP_BODY = """\
{i}IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
{i}            WHERE c.relname = '{idx}' AND i.indrelid = to_regclass('{tbl}')) THEN
{i}    DROP INDEX {drop_ifex}{idx};
{i}ELSIF EXISTS (SELECT 1 FROM pg_class c
{i}               WHERE c.relname = '{idx}' AND c.relnamespace = current_schema()::regnamespace) THEN
{raise_}
{i}ELSE
{i}    {tail}
{i}END IF;"""

#: 不存在分支必须与**原语句**的 IF EXISTS 口径一致(PG16 实测:老形态不带 IF EXISTS 时
#: 会 ERROR ``index "X" does not exist``;带 IF EXISTS 时打 NOTICE 跳过)。
_TAIL_IFEXISTS = "NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)"
_TAIL_STRICT = ("RAISE EXCEPTION 'index \"{idx}\" does not exist'\n"
                "{i}        USING ERRCODE = 'undefined_object';  -- 与老形态(不带 IF EXISTS)同口径")


def render_drop_guard(index: str, table: str, if_exists: bool, indent: str,
                      inline: bool) -> str:
    """生成一条 DROP 守卫。``inline`` = 原语句已在别人的 DO 块里,不能再嵌 DO。"""
    inner = indent + ("" if inline else "    ")
    tail = (_TAIL_IFEXISTS if if_exists
            else _TAIL_STRICT.format(idx=index, i=inner))
    core = _DROP_BODY.format(
        i=inner, idx=index, tbl=table, tail=tail,
        drop_ifex="IF EXISTS " if if_exists else "",
        raise_=_DROP_RAISE.format(i=inner, idx=index, tbl=table))
    mode = "if-exists" if if_exists else "strict"
    anchor = f"{indent}-- @drop-index-guard {index} ON {table} {mode}"
    block = (f"{anchor}\n{core}" if inline else
             f"{anchor}\n{indent}DO $dropguard$\n{indent}BEGIN\n{core}\n"
             f"{indent}END $dropguard$;")
    return block[len(indent):] if indent else block


_DROP_STMT = re.compile(
    r"DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?P<ifex>IF\s+EXISTS\s+)?"
    r"(?:public\s*\.\s*)?\"?(?P<idx>[A-Za-z_][A-Za-z_0-9$]*)\"?\s*;",
    re.IGNORECASE)


def rewrite_drops(sql: str, rel: str, expected: dict[tuple[str, str], dict]
                  ) -> tuple[str, int]:
    """把本文件里的裸 DROP INDEX 换成表绑定守卫。

    ``expected`` = ``{(file, index): {table, if_exists, inline}}`` —— 期望表**只从
    审定清单来**,不在这里现猜(猜错比不改更糟:本来能删的删不掉)。
    """
    from scripts.defgeo_index_guard_scan import (
        scan_bound_drop_guards, scan_dynamic_drop_sites, strip_sql_noise,
    )
    masked = strip_sql_noise(sql)
    # 🔴 动态豁免按**字符区间**,不按行号(独立审计 FG-3:蹭同一行的裸 DROP 会被一并放行)
    dyn_spans = [(a, b) for _r, _ln, a, b in scan_dynamic_drop_sites(sql, rel)]
    guard_spans = [(g.start, g.end) for g in scan_bound_drop_guards(sql, rel)]
    hits = []
    for m in _DROP_STMT.finditer(masked):
        if any(a <= m.start() < b for a, b in guard_spans):
            continue
        if any(a <= m.start() < b for a, b in dyn_spans):
            continue
        spec = expected.get((rel, m.group("idx")))
        if spec is None:
            raise AssertionError(
                f"{rel}::{m.group('idx')} 不在审定清单里 —— 期望表没有出处,拒绝机械改写")
        hits.append((m, spec))
    out = sql
    for m, spec in reversed(hits):
        indent = _line_indent(out, m.start())
        out = (out[:m.start()]
               + render_drop_guard(m.group("idx"), spec["table"],
                                   bool(m.group("ifex")), indent, spec["inline"])
               + out[m.end():])
    return out, len(hits)


def verify_drop_roundtrip(before: str, after: str, rel: str,
                          expected: dict[tuple[str, str], dict]) -> None:
    from scripts.defgeo_index_guard_scan import (
        scan_bound_drop_guards, scan_naked_drop_index_sites,
    )
    left = scan_naked_drop_index_sites(after, rel)
    if left:
        raise AssertionError(f"{rel} 改写后仍残留裸 DROP:{left}")
    for g in scan_bound_drop_guards(after, rel):
        spec = expected.get((rel, g.index))
        if spec is None:
            raise AssertionError(f"{rel}::{g.index} 守卫不在审定清单里")
        if g.table != spec["table"]:
            raise AssertionError(
                f"{rel}::{g.index} 守卫绑到了 {g.table},审定清单说 {spec['table']}")
        d = bound_drop_guard_defects(g)
        if d:
            raise AssertionError(f"{rel}::{g.index} DROP 守卫缺腿:{d}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if not (a.apply or a.check):
        a.check = True

    total = 0
    touched = 0
    for path in manifest_sql_files(ROOT):
        rel = path.relative_to(ROOT).as_posix()
        # 🔴 Windows 读改写必须 newline="" 两端都加,否则 LF 全变 CRLF(本仓记过)
        with io.open(path, encoding="utf-8", newline="") as fh:
            before = fh.read()
        after, n = rewrite_text(before, rel)
        if n == 0:
            continue
        verify_roundtrip(before, after, rel)
        total += n
        touched += 1
        if a.apply and after != before:
            with io.open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(after)
    verb = "改写" if a.apply else "待改写"
    print(f"{verb} {total} 条 · 覆盖 {touched} 个迁移文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
