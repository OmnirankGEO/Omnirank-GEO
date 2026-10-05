"""§8 接线自检 · 本包每个新增函数必须有 >=1 个真实调用点

🔴 为什么要有它:上一批栽过「零调用零测试的死函数照样过审」。
   规则写在文档里挡不住,做成执行时会自己报错的脚本才挡得住。

判据(用 AST,不用正则 —— 正则会把注释/字符串里的同名字样算成调用):
  对每个新增模块里的 public 函数 F:
     调用点 = 全仓(排除自身定义文件)中 `F(...)` 或 `x.F(...)` 的调用
              ∪ 测试文件中的引用
  private(_ 前缀)函数只要求在【本模块内】被调用或被测试引用。

用法:
    python scripts/research/check_geovid_wiring.py
    python scripts/research/check_geovid_wiring.py --selftest
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import sys
from typing import Dict, List, Set

ROOT = pathlib.Path(__file__).resolve().parents[2]

# 本包新增的实现文件(测试文件不算被检查对象,但算调用点来源)
TARGETS = [
    ROOT / "services" / "geo_douyin" / "config.py",
    ROOT / "services" / "geo_douyin" / "title_engine.py",
    ROOT / "services" / "geo_douyin" / "content_generator.py",
    ROOT / "services" / "geo_douyin" / "image_pipeline.py",
    ROOT / "services" / "geo_douyin" / "production_task.py",
    ROOT / "services" / "geo_douyin" / "publish_adapter.py",
    ROOT / "services" / "geo_douyin" / "card_templates.py",
    ROOT / "services" / "geo_douyin" / "knowledge_context.py",
    # 2026-08-02 异步化新增。🔴 新模块必须同时进这张表 ——
    # 不进表 = 这个文件里的死函数永远不会被发现(死函数=复审漏接线的同型坑)
    ROOT / "services" / "geo_douyin" / "task_progress.py",
    ROOT / "services" / "geo_douyin" / "redraw.py",
    # §15 部分成功交付。🔴 新模块必须同时进这张表 —— 上一包刚踩过:
    # 不进表 = 这个文件里的死函数永远不会被发现。
    ROOT / "services" / "geo_douyin" / "settlement.py",
    # 2026-08-03 全量包新增五个模块。🔴 我差点又漏 —— 上面两条注释已经写着
    # "新模块必须同时进这张表",第一次跑接线时这五个文件根本不在扫描范围内,
    # 结果是"134 个函数全部有调用点 ✅"这句**看起来绿、其实没测到新代码**。
    # 同一个坑第三次:不进表 = 这个文件里的死函数永远不会被发现。
    ROOT / "services" / "geo_douyin" / "pricing.py",
    ROOT / "services" / "geo_douyin" / "series_plan.py",
    ROOT / "services" / "geo_douyin" / "topic_distiller.py",
    ROOT / "services" / "client_knowledge.py",
    ROOT / "db" / "douyin_corpus_db.py",
    ROOT / "db" / "geo_douyin_db.py",
    ROOT / "api" / "geo_douyin_api.py",
    # 2026-08-03 收尾包。同一条规矩第四次写在这里:**新模块必须同时进这张表**。
    # 上一批的教训不是"记得加",是"不加不会红" —— 不进表 = 这个文件里的
    # 死函数永远不会被发现,而"134 个函数全绿"那句话看起来一模一样。
    ROOT / "services" / "geo_douyin" / "ocr_qa.py",
    # 🔴 `kb_consistency.py` 是**上一批就该进而一直没进**的:它 2026-08-02 建的,
    #    三次补表都漏了它。本批往里加了对齐核验,顺手补进来。
    ROOT / "services" / "geo_douyin" / "kb_consistency.py",
    # 内容规划层(2026-08-03)。同一条规矩第五次:新模块必须同时进这张表。
    ROOT / "services" / "geo_douyin" / "content_plan.py",
]

# 扫调用点的范围
SCAN_DIRS = ["services", "api", "db", "tests", "scripts", "workflows", "tools"]
SCAN_FILES = [ROOT / "server.py"]

# 豁免:FastAPI 路由处理函数由装饰器注册,没有显式调用点
ROUTE_DECORATORS = {"get", "post", "put", "delete", "patch"}


def _is_route_handler(node) -> bool:
    for dec in getattr(node, "decorator_list", []):
        f = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(f, ast.Attribute) and f.attr in ROUTE_DECORATORS:
            return True
    return False


def defined_functions(path: pathlib.Path) -> List[tuple]:
    """返回 [(name, is_private, is_route)]，跳过嵌套函数与 dataclass 方法。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: List[tuple] = []
    for node in tree.body:  # 只看模块顶层
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, node.name.startswith("_"), _is_route_handler(node)))
    return out


def called_names_in(path: pathlib.Path) -> Set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
        # 也算"被引用"(monkeypatch 字符串、from x import f)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        # 🔴 裸名字引用也算调用点:`asyncio.to_thread(load_client_materials, ...)`
        #    把函数当参数传出去,是货真价实的接线 —— 只数 ast.Call 会把它误报成死函数
        #    (实测踩过:load_client_materials 被 to_thread 调用却报 0 调用点)。
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            tail = node.value.rsplit(".", 1)[-1]
            if tail.isidentifier():
                names.add(tail)
    return names


def build_callsite_index() -> Dict[pathlib.Path, Set[str]]:
    index: Dict[pathlib.Path, Set[str]] = {}
    files: List[pathlib.Path] = list(SCAN_FILES)
    for d in SCAN_DIRS:
        base = ROOT / d
        if base.exists():
            files.extend(base.rglob("*.py"))
    for f in files:
        index[f] = called_names_in(f)
    return index


def run() -> int:
    index = build_callsite_index()
    problems: List[str] = []
    checked = 0

    for target in TARGETS:
        if not target.exists():
            problems.append(f"目标文件不存在: {target}")
            continue
        for name, is_private, is_route in defined_functions(target):
            checked += 1
            if is_route:
                continue  # 路由处理函数由装饰器注册
            # 🔴 调用点要含【本模块自身】:只在自己模块里被调用的 helper 也是活的。
            #    第一版把自身文件排除掉,把 scan_ad_law(在本模块内被调用)误报成死函数 ——
            #    "死函数"的定义是【全仓零调用点】,不是"没有跨模块调用点"。
            hits = sum(1 for f, names in index.items() if name in names)
            if hits < 1:
                problems.append(
                    f"{target.relative_to(ROOT)}::{name} 调用点 0 个"
                    f"({'private' if is_private else 'public'})")

    print(f"[wiring] 检查 {checked} 个顶层函数")
    if problems:
        print(f"[wiring] 🔴 {len(problems)} 个疑似死函数:")
        for p in problems:
            print("   -", p)
        return 1
    print("[wiring] ✅ 全部函数调用点 >= 1")
    return 0


def selftest() -> int:
    """核验检查器本身有判别力:造一个真死函数,必须被抓到。"""
    tmp = ROOT / "services" / "geo_douyin" / "_wiring_selftest_probe.py"
    tmp.write_text(
        "def a_function_nobody_calls_zzz():\n    return 1\n", encoding="utf-8")
    try:
        TARGETS.append(tmp)
        code = run()
    finally:
        TARGETS.remove(tmp)
        tmp.unlink(missing_ok=True)
    if code == 0:
        print("[wiring-selftest] 🔴 造了个死函数却没被抓到 = 恒真检查器")
        return 1
    print("[wiring-selftest] ✅ 死函数被抓到(检查器有判别力)")
    # 反向面:移除探针后必须恢复绿
    if run() != 0:
        print("[wiring-selftest] 🔴 移除探针后仍红 = 有真实死函数")
        return 1
    print("[wiring-selftest] ✅ 移除探针后恢复绿")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()
    sys.exit(selftest() if args.selftest else run())
