#!/usr/bin/env python3
"""§2 普查 · naive 时间列对「业务日边界」的潜伏轴(WO-LATENT-TRAPS 2026-08-17)

## 这条轴是什么

`timestamp without time zone`(naive)存的是**墙钟**,没有时区。往里写 `NOW()` 时,
PostgreSQL 用**会话/数据库的 TimeZone** 把当下这一刻折成墙钟再存。
读出来做 `created_at::date` / `date_trunc('day', created_at)` / `BETWEEN d AND d+1` 时,
用的还是那个墙钟 —— 也就是说,**「今天」是哪一天,取决于 DB TimeZone**。

生产实测(2026-08-17 只读取证):`SHOW TimeZone` = **Asia/Shanghai**,
恰好 = 业务时区,所以这些查询现在**都算对**。但那是**碰巧对,不是设计对**:
DB 参数改一次 / 起一个 UTC 的副本库 / 换云厂商默认值 —— 全站业务日边界当场平移 8 小时,
资金对账、日限额、日容量、监测调度全部错位,而且**不报错**。

生产 naive 列 572 / aware 541(清单见 naive_timestamp_columns_prod_2026-08-17.txt)。

## 结构锚

不 grep「我记得哪些查询按天聚合」,而是:
  ① 从**全部 git 跟踪的 .py** 里 AST 抽出所有 SQL 字符串字面量(含 f-string 拼接片段);
  ② 在其中找**日期边界形态**:`::date` / `date_trunc('day'|'week'|'month'…)` /
     `CURRENT_DATE` / `BETWEEN` / `>= 'YYYY-MM-DD'` / `AT TIME ZONE`;
  ③ 把该形态左右的**列名**取出来,和生产 naive 列清单对照;
  ④ 逐处落进三类(与工单口径一致,无第四类):
       CONVERTED  已显式转换(带 AT TIME ZONE)→ 不依赖 DB 时区,安全
       DB_TZ      naive 列直接对日期边界比较 → **依赖 DB 时区**(这条轴上的)
       NO_BOUNDARY 不涉边界(aware 列 / 纯 INTERVAL 相对比较 / 只排序)
  ⑤ DB_TZ 的再打**业务域**标签:资金 / 扣费 / 容量 / 监测调度 四域 = 必修;
     展示统计类 = 只标注不修(工单口径)。

用法:
  python scripts/research/naive_time_business_day_census.py
  python scripts/research/naive_time_business_day_census.py --selftest
  python scripts/research/naive_time_business_day_census.py --must-fix-only
"""
from __future__ import annotations

import argparse
import ast
import io
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
NAIVE_LIST = os.path.join(HERE, "naive_timestamp_columns_prod_2026-08-17.txt")

# ---- 日期边界形态(结构锚)----
BOUNDARY_PATTERNS = [
    # CAST_DATE 不走正则,走 _scan_cast_date() —— 见那里的注释:
    # `(x AT TIME ZONE 'z')::date` 这种**已修复**的写法,冒号前面是 `)`,
    # 用 `(\w+)::date` 抓不到 → 修好的反而从普查里消失,分母静默缩水。
    ("DATE_TRUNC", re.compile(r"date_trunc\s*\(\s*'(\w+)'\s*,\s*([\w\.]+)", re.I)),
    ("CURRENT_DATE", re.compile(r"([\w\.]+)\s*(?:>=|<=|<|>|=|BETWEEN)\s*CURRENT_DATE", re.I)),
    ("DATE_LITERAL", re.compile(r"([\w\.]+)\s*(?:>=|<=|<|>|=)\s*'?\d{4}-\d{2}-\d{2}", re.I)),
    ("BETWEEN", re.compile(r"([\w\.]+)\s+BETWEEN\b", re.I)),
    ("TO_CHAR_DAY", re.compile(r"to_char\s*\(\s*([\w\.]+)\s*,\s*'YYYY-MM-DD", re.I)),
]
AT_TIME_ZONE = re.compile(r"AT\s+TIME\s+ZONE", re.I)

# ---- 会话派生的「现在/今天」:这才是时区敏感的那一半 ----
# 🔴 2026-08-17 真 PG 实测(scripts/research/tz_axis_evidence_probe.py,三时区跑一遍):
#      naive::date          → 三个时区恒等,**时区无关**
#      date_trunc(day,naive)→ 三个时区恒等,**时区无关**
#      timestamptz::date    → Asia/Shanghai 得 08-18,UTC / New_York 得 08-17,**时区敏感**
#      naive::date = tstz::date → true / false / false ,**时区敏感** ← 真正的轴
#      naive::date = (tstz AT TIME ZONE 'Asia/Shanghai')::date → 三时区全 true,**修好了**
#    (夹具刻意选 2026-08-17 16:30 UTC == 2026-08-18 00:30 上海,跨日界;
#     第一版夹具用 NOW(),三个时区碰巧同一天 → 零判别力,差点得出"全都不敏感"的假结论。)
# 结论:光有 naive 的日期边界**不构成**这条轴;必须它**对上**一个会话派生的今天。
SESSION_NOW = re.compile(r"\b(CURRENT_DATE|CURRENT_TIMESTAMP|LOCALTIMESTAMP|NOW\s*\(\s*\))", re.I)
SQL_HINT = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|WITH|FROM|WHERE|GROUP\s+BY)\b", re.I)

# ---- 业务域(必修 4 域 vs 展示类)----
DOMAIN_RULES = [
    ("资金", re.compile(
        r"wallet|ledger|points|balance|recharge|refund|payout|settle|commission|"
        r"invoice|order|payment|price_quote|paid_at|revenue|withdraw", re.I)),
    ("扣费", re.compile(r"charge|billing|deduct|freeze|reserve|consum|feature_pricing|quota", re.I)),
    ("容量", re.compile(r"limit|capacity|cap_|daily_|per_day|rate_limit|usage|seat", re.I)),
    ("监测调度", re.compile(r"monitor|schedul|cron|due_|next_run|dispatch|keyword_daily|job", re.I)),
]
MUST_FIX_DOMAINS = {"资金", "扣费", "容量", "监测调度"}

# ---- 禁区:两个窗口在制,只入表标注,不动手 ----
FROZEN_PREFIXES = ("services/geo_douyin/", "writing/")

# ---- 第二根轴:这段 SQL 是**驱动行为**的,还是只**渲染数字**的 ----
# 光有业务域不够 —— `services/ai_ops/report_metrics/**` 里满是 `paid_at::date`,
# 域名正则会判「资金」,但它只是运维日报的分子分母,算错了没人少拿一分钱。
# 真正要修的是「这个日期边界的结果会改变系统做什么」的那些:日限额、服务窗口、调度是否该跑。
ONESHOT_RX = re.compile(
    r"^scripts/(backfill_|dryrun_|ops_backfill_|migrate_|migration_|dedup_|sign_|fix_)", re.I)
REPORTING_RX = re.compile(
    r"^(services/ai_ops/report_metrics/|services/flywheel_panorama\.py|"
    r"api/dashboard_api\.py|api/admin_api\.py|services/registration_agreement_gate_metrics\.py|"
    r"services/article_data_health\.py|services/publish_recommendation\.py|db/publish_db\.py)", re.I)


# 探针/普查脚本自己 —— 它们**故意**含正样本 SQL(`created_at::date = CURRENT_DATE`),
# 那是判据的正样本,不是生产代码里的踩中。
# 🔴 这条是被判据自己打出来的:本文件未进 git 前,`git ls-files` 列不到它,普查看不见自己;
#    §2 一 commit,它立刻把**自己的自测样本**报成"必修域驱动行为的踩中"。
#    所以归成独立的 PROBE 一类 —— 仍留在分母里可见,只是不进动手集合(不是从分母里抹掉)。
PROBE_RX = re.compile(r"^scripts/research/", re.I)


def _nature_of(rel: str) -> str:
    """PROBE 探针自身 / ONESHOT 历史一次性脚本 / REPORTING 只渲染数字 / BEHAVIOR 驱动行为。"""
    if PROBE_RX.match(rel):
        return "PROBE"
    if ONESHOT_RX.match(rel):
        return "ONESHOT"
    if REPORTING_RX.match(rel):
        return "REPORTING"
    return "BEHAVIOR"


def load_naive_columns() -> Tuple[Set[str], Set[str]]:
    """返回 (裸列名集合, 'table.column' 全限定集合)。"""
    fq: Set[str] = set()
    bare: Set[str] = set()
    with io.open(NAIVE_LIST, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or "." not in line:
                continue
            fq.add(line.lower())
            bare.add(line.split(".", 1)[1].lower())
    return bare, fq


def _read(path: str) -> str:
    with io.open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


def _tracked(root: str, pattern: str) -> List[str]:
    out = subprocess.run(
        ["git", "ls-files", pattern], cwd=root, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    return [p for p in out.splitlines() if p.strip()]


def _sql_literals(path: str):
    """AST 抽 SQL 字符串字面量(含 f-string 的常量片段),返回 [(lineno, text)]。"""
    src = _read(path)
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as exc:
        # 语法错的文件**不许静默跳过**(那会让分母缩水且无人知道),
        # 但也不该整轮中止 —— 仓里有 tools/scoring/geo_scorer_backup.py 这种历史损坏文件。
        # 折中:退回**整文件裸文本**扫,行号记 0,并在汇总里单列 UNPARSEABLE 让它可见。
        return None, "%s: %s" % (path, exc), src
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if SQL_HINT.search(node.value):
                out.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            merged = "".join(
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            if merged and SQL_HINT.search(merged):
                out.append((node.lineno, merged))
    return out, None, None


def _domain_of(text: str, path: str) -> str:
    hay = path + " " + text
    for name, rx in DOMAIN_RULES:
        if rx.search(hay):
            return name
    return "展示统计"


def _scan_cast_date(sql: str):
    """扫 `<expr>::date`,把 expr 里的列名取出来。两种形态都要收:

        created_at::date                                  ← 裸列(这条轴上的)
        (created_at AT TIME ZONE 'Asia/Shanghai')::date   ← 括号表达式,**已修复**的写法

    第二种若漏收,「修好的」就会从普查里消失 —— 分母变小、还看不出来。
    2026-08-17 探针自证时正是被这一条打红的(正则 `(\\w+)::date` 冒号前是 `)` 匹配不上)。
    返回 [(形态, 列名, 起, 止)]。
    """
    out = []
    for m in re.finditer(r"::\s*date\b", sql, re.I):
        head = sql[: m.start()].rstrip()
        if head.endswith(")"):
            depth, i = 0, len(head) - 1
            while i >= 0:
                if head[i] == ")":
                    depth += 1
                elif head[i] == "(":
                    depth -= 1
                    if depth == 0:
                        break
                i -= 1
            expr = head[i + 1: len(head) - 1] if i >= 0 else head
            span_start = max(0, i)
        else:
            im = re.search(r"([\w\.]+)$", head)
            expr = im.group(1) if im else ""
            span_start = max(0, m.start() - len(expr))
        cols = re.findall(r"[\w\.]+", expr)
        col = cols[0] if cols else ""
        if col:
            out.append(("CAST_DATE", col, span_start, m.end()))
    return out


def _classify(sql: str, bare_naive: Set[str]):
    """在一段 SQL 里找日期边界形态,返回 [(形态, 列名, 是否 naive, 是否已显式转换)]。"""
    found = []
    for kind, col, lo0, hi0 in _scan_cast_date(sql):
        col_bare = col.split(".")[-1].lower()
        if col_bare in ("date", "day", "week", "month", "year", "hour"):
            continue
        lo, hi = max(0, lo0 - 40), min(len(sql), hi0 + 40)
        found.append((kind, col, col_bare in bare_naive,
                      bool(AT_TIME_ZONE.search(sql[lo:hi])), bool(SESSION_NOW.search(sql[lo:hi]))))
    for kind, rx in BOUNDARY_PATTERNS:
        for m in rx.finditer(sql):
            groups = [g for g in m.groups() if g]
            col = groups[-1] if kind != "DATE_TRUNC" else (groups[-1] if len(groups) > 1 else groups[0])
            col_bare = col.split(".")[-1].lower()
            if col_bare in ("date", "day", "week", "month", "year", "hour"):
                continue
            is_naive = col_bare in bare_naive
            # 「显式转换」判在该匹配附近 ±120 字符,不判全文(全文里别处的 AT TIME ZONE 不算数)
            lo, hi = max(0, m.start() - 120), min(len(sql), m.end() + 120)
            converted = bool(AT_TIME_ZONE.search(sql[lo:hi]))
            # 🔴 会话「今天」必须**紧邻**这个边界比较,不能只是"同一条 SQL 里出现过"。
            #    实测反例:`UPDATE ... SET archived_at = NOW() ... WHERE q.paid_at::date ...`
            #    —— NOW() 是写入值,不是被比较的今天;按"整条 SQL 含 NOW()"判会多报 4 处。
            near_lo, near_hi = max(0, m.start() - 60), min(len(sql), m.end() + 60)
            found.append((kind, col, is_naive, converted, bool(SESSION_NOW.search(sql[near_lo:near_hi]))))
    return found


def census(root: str):
    bare_naive, _fq = load_naive_columns()
    rows = []
    unparseable: List[str] = []
    files = _tracked(root, "*.py")
    scanned_sql = 0
    for rel in files:
        if rel.startswith("tests/") or "/tests/" in rel:
            continue
        path = os.path.join(root, rel)
        src = _read(path)
        if not SQL_HINT.search(src):
            continue
        lits, bad, raw = _sql_literals(path)
        if lits is None:                       # 语法错 → 裸文本兜底 + 单列可见
            unparseable.append(bad)
            lits = [(0, raw)]
        for lineno, sql in lits:
            scanned_sql += 1
            for kind, col, is_naive, converted, near_now in _classify(sql, bare_naive):
                if converted:
                    verdict = "CONVERTED"
                elif is_naive and near_now:
                    verdict = "DB_TZ_LIVE"          # naive 日期边界 对上 会话派生的今天 = 真踩中
                elif is_naive:
                    verdict = "DB_TZ_WRITEONLY"     # 读侧时区无关;只剩"写入时存的是谁的墙钟"
                else:
                    verdict = "NO_BOUNDARY"
                rows.append({
                    "file": rel,
                    "line": lineno,
                    "form": kind,
                    "col": col,
                    "naive": is_naive,
                    "verdict": verdict,
                    "domain": _domain_of(sql, rel),
                    "frozen": rel.startswith(FROZEN_PREFIXES),
                    "nature": _nature_of(rel),
                    "sql": re.sub(r"\s+", " ", sql).strip()[:90],
                })
    return rows, len(files), scanned_sql, unparseable


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--must-fix-only", action="store_true")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    if a.selftest:
        return selftest(root)

    rows, n_files, n_sql, unparseable = census(root)
    bare_naive, fq = load_naive_columns()

    def key(r):
        return (r["verdict"] != "DB_TZ_LIVE", r["domain"] not in MUST_FIX_DOMAINS, r["file"], r["line"])

    shown = [r for r in rows if (not a.must_fix_only) or
             (r["verdict"] == "DB_TZ_LIVE" and r["domain"] in MUST_FIX_DOMAINS)]
    print("%-12s %-9s %-7s %-46s %6s %-22s %s" % ("verdict", "domain", "form", "file", "line", "column", "SQL 片段"))
    print("-" * 175)
    for r in sorted(shown, key=key):
        flag = " [禁区·只标注]" if r["frozen"] else ""
        print("%-12s %-9s %-7s %-46s %6d %-22s %s%s" % (
            r["verdict"], r["domain"], r["form"], r["file"], r["line"], r["col"][:22], r["sql"][:56], flag))

    print("-" * 175)
    print("扫描分母:git 跟踪 .py %d 个 · 其中含 SQL 字面量 %d 段 · 命中日期边界形态 %d 处"
          % (n_files, n_sql, len(rows)))
    print("  语法错无法 AST 解析、已退回裸文本扫的文件 %d 个(单列可见,不静默丢):" % len(unparseable))
    for u in unparseable:
        print("    UNPARSEABLE %s" % u)
    vc = Counter(r["verdict"] for r in rows)
    print()
    print("== 工单口径 ==")
    print("  CONVERTED        已显式转换(带 AT TIME ZONE)          %d" % vc["CONVERTED"])
    print("  DB_TZ_LIVE       naive 日期边界 对上 会话派生的今天 → 真踩中 %d" % vc["DB_TZ_LIVE"])
    print("  DB_TZ_WRITEONLY  naive 日期边界但读侧时区无关(实测)     %d" % vc["DB_TZ_WRITEONLY"])
    print("  NO_BOUNDARY      不涉边界(aware 列/相对区间)          %d" % vc["NO_BOUNDARY"])
    print("  ──────────────────────────────────────────────")
    tot = vc["CONVERTED"] + vc["DB_TZ_LIVE"] + vc["DB_TZ_WRITEONLY"] + vc["NO_BOUNDARY"]
    print("  合计 %d  (= 命中总数 %d)" % (tot, len(rows)))
    assert tot == len(rows), "分类没盖全"
    print("  注:DB_TZ_WRITEONLY 既不是安全、也不是要改读侧 —— 真 PG 实测 naive::date 读侧时区无关,")
    print("     它只剩「写入那一刻存的是谁的墙钟」这半条,属于写侧口径,不在本工单的修复面。")

    print()
    print("== DB_TZ 按业务域拆(资金/扣费/容量/监测调度 = 工单要求必修)==")
    dom = Counter(r["domain"] for r in rows if r["verdict"] == "DB_TZ_LIVE")
    frozen = sum(1 for r in rows if r["verdict"] == "DB_TZ_LIVE" and r["frozen"])
    for d, n in dom.most_common():
        mark = "  ← 必修" if d in MUST_FIX_DOMAINS else "  (展示类·只标注)"
        print("  %-10s %3d%s" % (d, n, mark))
    must = sum(n for d, n in dom.items() if d in MUST_FIX_DOMAINS)
    print("  必修合计 %d · 展示类 %d · 其中落在禁区(geo_douyin/writing,只标注不动手) %d"
          % (must, vc["DB_TZ_LIVE"] - must, frozen))

    print()
    print("== 必修域 DB_TZ 再按「驱动行为 / 只渲染数字 / 历史一次性脚本」拆 ==")
    nat = Counter(r["nature"] for r in rows
                  if r["verdict"] == "DB_TZ_LIVE" and r["domain"] in MUST_FIX_DOMAINS)
    for n in ("BEHAVIOR", "REPORTING", "ONESHOT", "PROBE"):
        note = {"BEHAVIOR": "  ← 本包动手改的就是这些",
                "REPORTING": "  (只渲染数字·入表标注)",
                "ONESHOT": "  (历史一次性脚本·已跑过,改它=改历史记录)",
                "PROBE": "  (探针自己的正样本·留在分母里可见,不进动手集合)"}[n]
        print("  %-10s %3d%s" % (n, nat[n], note))
    fixset = sorted({(r["file"], r["line"]) for r in rows
                     if r["verdict"] == "DB_TZ_LIVE" and r["domain"] in MUST_FIX_DOMAINS
                     and r["nature"] == "BEHAVIOR" and not r["frozen"]})
    print("  → 去重后的动手集合(file,line)共 %d 处:" % len(fixset))
    for f, l in fixset:
        print("      %s:%d" % (f, l))
    print()
    print("生产事实(2026-08-17 只读取证):DB TimeZone = Asia/Shanghai · naive 列 %d 个" % len(fq))
    return 0


# ------------------------------------------------------------------- selftest
_POS = "SELECT count(*) FROM wallet_ledger WHERE created_at::date = CURRENT_DATE"
_NEG_CONVERTED = ("SELECT count(*) FROM wallet_ledger "
                  "WHERE (created_at AT TIME ZONE 'Asia/Shanghai')::date = CURRENT_DATE")
_NEG_AWARE = "SELECT count(*) FROM t WHERE some_tz_column::date = CURRENT_DATE"
_NEG_RELATIVE = "SELECT count(*) FROM wallet_ledger WHERE created_at >= NOW() - INTERVAL '7 days'"


def selftest(root: str) -> int:
    bare, _ = load_naive_columns()
    ok = True

    def check(label, sql, want_verdicts):
        nonlocal ok
        got = []
        for kind, col, is_naive, conv, live in _classify(sql, bare):
            got.append("CONVERTED" if conv else
                       ("DB_TZ_LIVE" if (is_naive and live) else
                        ("DB_TZ_WRITEONLY" if is_naive else "NO_BOUNDARY")))
        good = got == want_verdicts
        ok = ok and good
        print("  [%s] %s: 期望 %s 实得 %s" % ("PASS" if good else "FAIL", label, want_verdicts, got))

    print("§2 普查探针 · 判别力自证")
    print("① 正样本 · naive 列裸对日期边界 —— 必须命中 DB_TZ")
    check("created_at::date = CURRENT_DATE", _POS, ["DB_TZ_LIVE"])
    print("② 反向对照 · 同一列已显式 AT TIME ZONE —— 必须不判 DB_TZ")
    check("显式转换", _NEG_CONVERTED, ["CONVERTED"])
    print("③ 反向对照 · 非 naive 列 —— 必须不判 DB_TZ")
    check("aware 列", _NEG_AWARE, ["NO_BOUNDARY"])
    print("③b 反向对照 · naive 日期边界但不对会话今天 —— 必须判 WRITEONLY 不判 LIVE")
    check("无会话今天", "SELECT created_at::date AS d, count(*) FROM wallet_ledger GROUP BY 1",
          ["DB_TZ_WRITEONLY"])
    print("④ 反向对照 · 相对区间不涉日边界 —— 必须一个形态都不命中")
    check("相对 INTERVAL", _NEG_RELATIVE, [])
    print("⑤ naive 列清单必须非空且来自生产取证")
    print("  naive 裸列名 %d 个 · 清单文件 %s" % (len(bare), os.path.basename(NAIVE_LIST)))
    if len(bare) < 10:
        print("  [FAIL] 清单太小,判据零判别力")
        ok = False
    else:
        print("  [PASS]")
    print("SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
