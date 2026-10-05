"""防御型 GEO · WP0 census ①:``diagnosis_runs.run_status`` 值表机械导出。

规格 §3.5 要求对现役 ``run_status`` 做**逐值 exhaustive mapping**;§0.5.3 G-2 进一步
规定「``run_status`` 值表一律从 WP0 census 机械导出,本文内的手写枚举只是示例,不是分母」。

所以本文件**不写值表**。它从两个独立信源各自抽,再交叉比对:

  信源 A(权威 · DB 约束面)= ``scripts/migration_diagnosis_runs_2026_07_13.sql`` 里
      ``chk_diag_runs_status`` 的 ``CHECK (run_status IN (...))`` 字面量。
      这是数据库真正能存下的全集 —— 任何投影的分母只能是它。
  信源 B(代码写入面)= 生产模块里所有把字面量写进 ``run_status`` 的位置
      (``run_status='x'`` / ``run_status=%s`` 的实参 / ``_transition`` 的目标态)。

两个信源的差集就是缺陷信号:
  · B - A  = 代码想写但 CHECK 存不下 → 该写入必然 IntegrityError(生产事故);
  · A - B  = CHECK 允许但现役代码从不写 → 可能是历史遗留值,**仍必须进投影分母**
             (数据库里可能有存量行;规格要求未知值 quarantined 而不是假装不存在)。

🔴 本脚本是**判据的分母来源**,不是文档。``services/defensive_geo/run_status_projection.py``
   的 ``PROJECTION`` 表必须逐值覆盖信源 A;判据
   ``tests/defensive_geo_2026_08_21/test_run_status_projection.py`` 拿本脚本的输出当分母,
   而不是拿人手抄的清单 —— 手抄清单漏掉的那一项不会让任何判据变红
   (本仓 2026-08-19 记过这个坑)。

用法::

    python scripts/defgeo_census/run_status_census.py            # 人读
    python scripts/defgeo_census/run_status_census.py --json     # 机读(判据用)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 信源 A 的承载文件。**只认这一个** —— 换文件必须改这里,而不是让 census 去 glob
#: 全仓「看起来像」的 SQL(glob 会把 tests/ 下的 pg_dump 夹具也吃进来,那是别人的快照)。
CHECK_SQL = ROOT / "scripts" / "migration_diagnosis_runs_2026_07_13.sql"
CHECK_CONSTRAINT_NAME = "chk_diag_runs_status"

#: 信源 B 的扫描面 = 生产模块。排除 ``tests/`` 与 ``scripts/verify_*`` ——
#: 判据脚本里为了造反例会写各种状态字面量,把它们算进「代码写入面」会让差集永远为空。
CODE_ROOTS = ("services", "db", "api", "workflows")
CODE_EXTRA_FILES = ("server.py",)

_CHECK_RE = re.compile(
    r"CONSTRAINT\s+" + CHECK_CONSTRAINT_NAME + r"\s+CHECK\s*\(\s*run_status\s+IN\s*\((?P<body>.*?)\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)
_LITERAL_RE = re.compile(r"'([a-z_]+)'")

# 代码写入面:三种真实形态(逐个都在 41 班代码里核对过)
#   run_status='committed'                        → SQL 里直接赋值
#   run_status = ANY(%s) / IN ('a','b')           → SQL 集合谓词
#   _cas(run_token, [from...], "released", ...)   → 状态机 CAS 迁移目标
#
# 🔴 第三种是补上去的。第一版只抓前两种,结果 census 把 released/cancelled/
#    cancelled_no_freeze 报成「代码从不写」—— 它们其实全走
#    ``services/diagnosis_runs.py::_cas`` 的第三个位置参数。这就是本仓
#    「census 裸符号名 = 把 import 当调用」那类漏抽的同形态:抽取面比真实写入面窄,
#    报告就会撒谎。分母不受影响(分母来自信源 A),但「谁在写」会错。
_CODE_ASSIGN_RE = re.compile(r"run_status\s*=\s*'([a-z_]+)'")
_CODE_IN_RE = re.compile(r"run_status\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
_CODE_CAS_RE = re.compile(
    r"_(?:cas|terminal_local_txn)\s*\(\s*[^,]+,\s*\[[^\]]*\]\s*,\s*['\"]([a-z_]+)['\"]"
)

#: 🔴 信源 B 是**辅助信号,不是分母**。它抓不到经变量间接传入的目标态 ——
#: 例如 ``services/diagnosis_runs.py:1850`` 的
#: ``target = "committed" if intent == "commit" else "released"``,
#: 目标态在运行期才定下来,任何静态字面量扫描都够不着。
#: 所以 ``allowed_not_written`` 只当「值得人看一眼」的提示,
#: **绝不能**被解释成「这个值可以不进投影」。投影分母恒 = 信源 A。
_SOURCE_B_IS_ADVISORY = True


def parse_check_constraint() -> list[str]:
    """信源 A:从迁移 SQL 抽 CHECK 允许值全集。抽不到就 abort,绝不返回空集。

    返回空集会让所有「投影必须覆盖分母」类判据**零分母恒绿** ——
    本仓 2026-08-19 明确记过「零分母判据只能记『没验』」。所以这里宁可炸。
    """
    if not CHECK_SQL.exists():
        raise SystemExit(f"[census] 信源 A 文件不存在: {CHECK_SQL}")
    text = CHECK_SQL.read_text(encoding="utf-8", errors="replace")
    matches = _CHECK_RE.findall(text)
    if not matches:
        raise SystemExit(
            f"[census] 在 {CHECK_SQL.name} 中找不到 {CHECK_CONSTRAINT_NAME} 的 CHECK 定义。"
            "约束可能被改名/移文件 —— 请更新 census,不要让它静默返回空集。"
        )
    # 同一约束在该迁移里出现两次(建 + DROP 重建拓宽);两处必须逐值相同,
    # 否则说明「新装库」和「升级库」枚举不一致 —— 那本身就是缺陷。
    parsed = [sorted(set(_LITERAL_RE.findall(body))) for body in matches]
    first = parsed[0]
    for idx, other in enumerate(parsed[1:], start=2):
        if other != first:
            raise SystemExit(
                f"[census] {CHECK_CONSTRAINT_NAME} 第 1 处与第 {idx} 处枚举不一致:\n"
                f"  #1 = {first}\n  #{idx} = {other}\n"
                "新装库与升级库会得到不同的允许值集合 —— 先修迁移。"
            )
    return first


def _iter_code_files():
    for rel in CODE_ROOTS:
        base = ROOT / rel
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            yield path
    for name in CODE_EXTRA_FILES:
        path = ROOT / name
        if path.is_file():
            yield path


def parse_code_literals() -> dict[str, list[str]]:
    """信源 B:代码里写进 run_status 的字面量 → {值: [出处]}。"""
    found: dict[str, list[str]] = {}
    for path in _iter_code_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            hits: set[str] = set(_CODE_ASSIGN_RE.findall(line))
            hits.update(_CODE_CAS_RE.findall(line))
            for body in _CODE_IN_RE.findall(line):
                hits.update(_LITERAL_RE.findall(body))
            for value in hits:
                found.setdefault(value, []).append(f"{rel}:{lineno}")
    return found


def build() -> dict:
    allowed = parse_check_constraint()
    code = parse_code_literals()
    code_values = sorted(code)
    return {
        "constraint_name": CHECK_CONSTRAINT_NAME,
        "source_a_file": CHECK_SQL.relative_to(ROOT).as_posix(),
        # 🔴 这一项就是所有投影判据的分母。
        "allowed_values": allowed,
        "allowed_count": len(allowed),
        "code_values": code_values,
        "code_sites": {k: v for k, v in sorted(code.items())},
        # 代码想写但 CHECK 存不下 → 写入必炸。非空即缺陷。
        "code_not_allowed": sorted(set(code_values) - set(allowed)),
        # CHECK 允许但代码不写 → 历史值;仍进投影分母。
        "allowed_not_written": sorted(set(allowed) - set(code_values)),
    }


def main(argv: list[str]) -> int:
    # Windows 控制台默认 GBK,遇 emoji 直接 UnicodeEncodeError 把 census 打成非零退出 ——
    # 那会让「census 跑通了吗」和「census 报了缺陷吗」在退出码上混成一件事。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    data = build()
    if "--json" in argv:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    print(f"信源 A = {data['source_a_file']} :: {data['constraint_name']}")
    print(f"CHECK 允许值({data['allowed_count']} 个 · 这就是投影分母):")
    for value in data["allowed_values"]:
        sites = data["code_sites"].get(value) or []
        mark = f"代码写入 {len(sites)} 处" if sites else "🟡 静态扫描未见写入(可能变量间接 · 仍进分母)"
        print(f"  · {value:<28} {mark}")
    if data["code_not_allowed"]:
        print("\n🔴 代码会写但 CHECK 存不下(写入必 IntegrityError):")
        for value in data["code_not_allowed"]:
            for site in data["code_sites"][value]:
                print(f"  · {value} @ {site}")
    else:
        print("\n✅ 代码写入面 ⊆ CHECK 允许值(无必炸写入)")
    if data["allowed_not_written"]:
        print("\n🟡 静态扫描未见写入 —— 但这**不代表**该值不会出现:")
        print("   信源 B 抓不到变量间接的目标态。已人工核到的一例:")
        print("   services/diagnosis_runs.py:1850  target = \"committed\" if intent == \"commit\" else \"released\"")
        print("   ⇒ 下列值照样必须进投影分母,不许因为「扫不到」而省略:")
        for value in data["allowed_not_written"]:
            print(f"  · {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
