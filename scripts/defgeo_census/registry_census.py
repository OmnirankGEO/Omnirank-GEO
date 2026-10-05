"""POR-20 · 十张公开 registry 的 **AST census**。

为什么是 AST 而不是 import 后读变量
-----------------------------------
import 后读 ``ALL_REGISTRIES`` 只能证明"这个字典里有什么",证明不了
"源码里定义的表**都**进了这个字典"。少写一行 ``ALL_REGISTRIES[...]``,
那张表就成了**死表** —— 它在源码里存在、被人维护、却没有任何消费者。
AST 从**源码结构**独立枚举 ``*: dict[str, Row] = {...}`` 形态的模块级赋值,
再与运行时字典求双向差集,才能同时抓到「死表」和「漏表」。

本仓 2026-08-08 记过这个形态:「census 裸符号名 = 把 import 当调用」;
2026-08-21 又记过「多规则扫描器的正样本只断言『有命中』= 被别的规则顺手判红」。
所以本 census 的自证(``--selftest``)对**每一类结论**分别注毒。
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import sys
from pathlib import Path

# Windows 控制台默认 GBK,直接 print emoji 会 UnicodeEncodeError 把整个 census 打挂。
# 判据脚本因编码崩掉与"判据失败"在退出码上长得一样,所以这里显式改 UTF-8。
if hasattr(sys.stdout, "reconfigure"):          # pragma: no cover - 环境相关
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    ROOT / "services" / "defensive_geo" / "presentation" / "public_registries.py"
)
MODULE_NAME = "services.defensive_geo.presentation.public_registries"


def ast_registry_tables(source: str) -> dict[str, list[str]]:
    """从源码 AST 枚举「模块级 ``NAME: dict[str, Row] = {...}``」的表及其 code。

    只认这一种形态 —— 形态收窄是有意的:宽松匹配会把无关字典也算进来,
    分母一虚,双向差集就失去意义。
    """
    tree = ast.parse(source)
    tables: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        ann = ast.unparse(node.annotation)
        if ann.replace(" ", "") != "dict[str,Row]":
            continue
        name = node.target.id
        codes: list[str] = []
        value = node.value
        # 形态:{r.code: r for r in (_r("a", ...), _r("b", ...))}
        if isinstance(value, ast.DictComp):
            for call in ast.walk(value):
                if (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == "_r"
                        and call.args
                        and isinstance(call.args[0], ast.Constant)):
                    codes.append(call.args[0].value)
        tables[name] = codes
    return tables


def ast_registered_versions(source: str) -> dict[str, str]:
    """从 ``ALL_REGISTRIES = {...}`` 里机械读出 version → 表变量名。"""
    tree = ast.parse(source)
    out: dict[str, str] = {}
    for node in tree.body:
        target_names = []
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
        elif isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "ALL_REGISTRIES" not in target_names or not isinstance(node.value, ast.Dict):
            continue
        for k, v in zip(node.value.keys, node.value.values):
            if isinstance(k, ast.Constant) and isinstance(v, ast.Name):
                out[k.value] = v.id
    return out


def census(source: str | None = None) -> dict[str, object]:
    src = source if source is not None else MODULE_PATH.read_text(encoding="utf-8")
    tables = ast_registry_tables(src)
    registered = ast_registered_versions(src)

    declared_versions = set()
    tree = ast.parse(src)
    for node in tree.body:
        names = []
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "REGISTRY_VERSIONS" in names and isinstance(node.value, ast.Tuple):
            declared_versions = {
                e.value for e in node.value.elts if isinstance(e, ast.Constant)
            }

    table_vars_in_source = set(tables)
    table_vars_registered = set(registered.values())

    return {
        "tables_in_source": sorted(table_vars_in_source),
        "tables_registered": sorted(table_vars_registered),
        # 死表:源码里定义了,却没挂进 ALL_REGISTRIES
        "dead_tables": sorted(table_vars_in_source - table_vars_registered),
        # 漏表:挂进 ALL_REGISTRIES,源码里却没有这个形态的定义
        "missing_tables": sorted(table_vars_registered - table_vars_in_source),
        "declared_versions": sorted(declared_versions),
        "registered_versions": sorted(registered),
        "version_not_registered": sorted(declared_versions - set(registered)),
        "registered_not_declared": sorted(set(registered) - declared_versions),
        "codes_by_table": {k: sorted(v) for k, v in sorted(tables.items())},
        "total_rows": sum(len(v) for v in tables.values()),
    }


def cross_check_runtime() -> list[str]:
    """AST 结论 × 运行时字典互校。两边不等 = 有一边在说谎。"""
    mod = importlib.import_module(MODULE_NAME)
    result = census()
    problems: list[str] = []

    runtime_versions = set(mod.ALL_REGISTRIES)
    ast_versions = set(result["registered_versions"])
    if runtime_versions != ast_versions:
        problems.append(
            f"运行时 registry 集合与 AST 不等:"
            f"仅运行时={sorted(runtime_versions - ast_versions)} "
            f"仅AST={sorted(ast_versions - runtime_versions)}"
        )
    for version, table in mod.ALL_REGISTRIES.items():
        var = ast_registry_tables(MODULE_PATH.read_text(encoding="utf-8"))
        registered = ast_registered_versions(MODULE_PATH.read_text(encoding="utf-8"))
        ast_codes = set(var.get(registered.get(version, ""), []))
        runtime_codes = set(table)
        if ast_codes != runtime_codes:
            problems.append(
                f"{version}: AST code 集合与运行时不等 "
                f"仅AST={sorted(ast_codes - runtime_codes)} "
                f"仅运行时={sorted(runtime_codes - ast_codes)}"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()

    result = census()
    problems = cross_check_runtime()
    for key in ("dead_tables", "missing_tables",
                "version_not_registered", "registered_not_declared"):
        if result[key]:
            problems.append(f"{key}: {result[key]}")

    if args.json:
        print(json.dumps({"census": result, "problems": problems},
                         ensure_ascii=False, indent=2))
    else:
        print(f"registry 表数(源码)={len(result['tables_in_source'])} "
              f"已登记={len(result['tables_registered'])} "
              f"行数={result['total_rows']}")
        for p in problems:
            print("  ❌ " + p)
    if problems:
        return 1
    print("✅ POR-20 registry census 通过")
    return 0


def _selftest() -> int:
    """对**每一类结论**分别注毒 —— 只验"有命中"会被别的规则顺手判红。"""
    src = MODULE_PATH.read_text(encoding="utf-8")
    ok = True

    # 毒 1:新增一张源码里有、但没挂进 ALL_REGISTRIES 的表 → dead_tables 必须非空
    poisoned = src.replace(
        "def registry(version: str)",
        'ORPHAN_TABLE: dict[str, Row] = {r.code: r for r in (_r("x", "y"),)}\n\n\n'
        "def registry(version: str)", 1)
    r1 = census(poisoned)
    hit1 = "ORPHAN_TABLE" in r1["dead_tables"]
    print(f"{'  ok  ' if hit1 else '  DEAD'} 死表可被发现")
    ok &= hit1

    # 毒 2:从 REGISTRY_VERSIONS 删一个版本 → version/registered 差集必须非空
    poisoned2 = src.replace('    "observation_error_registry_v1",\n', "", 1)
    r2 = census(poisoned2)
    hit2 = "observation_error_registry_v1" in r2["registered_not_declared"]
    print(f"{'  ok  ' if hit2 else '  DEAD'} 漏声明版本可被发现")
    ok &= hit2

    # 毒 3:从 ALL_REGISTRIES 摘掉一张表 → dead_tables 必须点名它
    poisoned3 = src.replace(
        '    "observation_error_registry_v1": OBSERVATION_ERROR,\n', "", 1)
    r3 = census(poisoned3)
    hit3 = "OBSERVATION_ERROR" in r3["dead_tables"]
    print(f"{'  ok  ' if hit3 else '  DEAD'} 从登记表摘掉一张可被发现")
    ok &= hit3

    # 反向对照:未注毒时四类结论必须全空,否则上面三条可能只是恒真
    clean = census(src)
    clean_ok = not any(clean[k] for k in (
        "dead_tables", "missing_tables",
        "version_not_registered", "registered_not_declared"))
    print(f"{'  ok  ' if clean_ok else '  DEAD'} 未注毒时四类结论全空(反向对照)")
    ok &= clean_ok

    print("✅ census 具备判别力" if ok else "❌ census 有恒绿结论")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
