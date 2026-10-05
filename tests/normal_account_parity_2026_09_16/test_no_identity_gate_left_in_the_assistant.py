# -*- coding: utf-8 -*-
"""AI 助手(原 `agents/social_agent.py`,已随开源 E3 B2 删)里不再有按账号等级的放行判断。

为什么这一片要用 AST 而不是真调用:这两个工具是 pydantic-ai 的 `@agent.tool`,
跑一次要一个真 `RunContext` + 真 http_client + 真 LLM 回合。
真调用跑不动的地方,**结构判据**是可用的最强器械 —— 但它必须按**行为**认,
不能按拼法认:本仓 an-authors-poisons-come-from-an-authors-criteria 的邻居,
「v1 正则认 `sys.stdout.buffer` 的拼法 → setattr 一改就瞎」。

所以这里认的是:**函数体内有没有任何一处从 deps 读出 `agent_level` 这个值**,
不管写成 `getattr(x, "agent_level", 0)` / `x.agent_level` / `x["agent_level"]`。
"""
from __future__ import annotations

import ast
import io
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

#: 本单撤闸的两个工具
GATED_TOOLS = ("create_brand", "quick_persona_setup")


def _tree(rel: str) -> ast.AST:
    return ast.parse(io.open(REPO / rel, encoding="utf-8").read(), rel)


def _find_func(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _agent_level_reads(node: ast.AST) -> list[int]:
    """任何一种**读出 agent_level 这个值**的形态,返回行号。

    三种形态一起认(不是三选一):
      · getattr(<任意>, "agent_level", ...)   —— 本文件原来用的写法
      · <任意>.agent_level                     —— 属性直读
      · <任意>["agent_level"]                  —— 下标/字典读
    """
    hits: list[int] = []
    for n in ast.walk(node):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "getattr" and len(n.args) >= 2
                and isinstance(n.args[1], ast.Constant) and n.args[1].value == "agent_level"):
            hits.append(n.lineno)
        elif isinstance(n, ast.Attribute) and n.attr == "agent_level":
            hits.append(n.lineno)
        elif (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
              and n.slice.value == "agent_level"):
            hits.append(n.lineno)
    return sorted(set(hits))


# [开源 E3 · B2 · 2026-09-28] AI 助手 agents/social_agent.py 随 E3 删除,守它工具体的 2 格退役;
#   全仓不再发 upgrade_required 那一格在役保留。


#: 🔴 本仓有**一个**非 UTF-8 的 .py:`tools/scoring/geo_scorer_backup.py`
#:   (90KB,已被 git 跟踪,开头 4 个字节是垃圾)。本仓至少四个包已经各自记过它。
#:   它一上来就把 `ast.parse` 打崩:**整轮普查在 tools/ 里静默停住**,
#:   而读数(0 个发出点)和"真的没有"一模一样。
#:   所以这里不 `except: continue` —— 那是把盲区变成绿。而是**收集**再和已知集比对:
#:   多出一个新的不可解码文件 ⇒ 立刻红(本仓 every-exclusion-in-a-criterion-is-a-self-declared-blind-spot)。
_NON_UTF8_KNOWN = {"tools/scoring/geo_scorer_backup.py"}

#: 生产代码的边界:排掉判据 / 一次性脚本 / 测试产物 / 前端 / 文档。
_NON_PRODUCTION_TOP = {
    "tests", "scripts", "frontend", "docs", "data", "node_modules",
    "agent-test-artifacts", "agent-test-artifacts-round3", "agent-test-artifacts-round2",
}


def _production_py() -> list[pathlib.Path]:
    """生产 .py 分母。

    只走 **git 跟踪**的文件:`rglob` 会把编辑器缓存、`__pycache__`、临时产物
    一起算进来,分母就不是"这棵树里的生产代码"了
    (本仓 a-correct-enumeration-narrowed-by-my-own-postprocessing 的反向:被**撑大**)。
    """
    import subprocess
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "*.py"],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, "git ls-files 失败,分母取不到:%s" % out.stderr[-300:]
    files = []
    for line in out.stdout.splitlines():
        rel = line.strip()
        if not rel:
            continue
        top = rel.split("/", 1)[0]
        if top in _NON_PRODUCTION_TOP:
            continue
        files.append(REPO / rel)
    return files


def test_nothing_in_production_emits_upgrade_required_any_more():
    """全仓分母腿:`upgrade_required` / `UPGRADE_REQUIRED` 在生产代码里零发出点。

    🔴 222-c0 §5 报过"生产代码 0 处",那次扫的是**大写**字面量,
       漏了 AI 助手(已随开源 E3 B2 删)里的**小写** `upgrade_required`
       —— 形态没列全,分母就是错的(本仓 endpoint-counts-need-a-boundary-anchor-not-a-prefix)。
       这里两种拼法一起认,并且**打印扫了多少文件**:
       扫描器自己死掉时读数也是 0,和"真的没有"一模一样
       (本仓 everything-looks-dead-means-the-scanner-died)。
    """
    files = _production_py()
    assert len(files) > 200, "只扫到 %d 个生产 .py —— 扫描器坏了,0 不可解读" % len(files)

    hits, undecodable, unparsable = [], [], []
    for p in files:
        rel = p.relative_to(REPO).as_posix()
        try:
            src = p.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            undecodable.append(rel)
            continue
        try:
            tree = ast.parse(src, rel)
        except SyntaxError:
            unparsable.append(rel)
            continue
        for n in ast.walk(tree):
            # 精确等值:文档字符串里**提到**它的长字符串不会等于它本身,
            # 注释 AST 根本看不见 —— 两类噪声天然被排除。
            if isinstance(n, ast.Constant) and n.value in ("upgrade_required", "UPGRADE_REQUIRED"):
                hits.append("%s:%d" % (rel, n.lineno))

    # 先报盲区,再报读数 —— 盲区变大时那个 0 不可解读。
    assert set(undecodable) == _NON_UTF8_KNOWN, (
        "不可解码的 .py 变了(已知 %s,实测 %s)—— 普查有新盲区,下面的 0 不作数"
        % (sorted(_NON_UTF8_KNOWN), sorted(undecodable)))
    assert not unparsable, "有 .py 解析不了,这些文件没被扫到:%s" % unparsable
    assert not hits, "还有发出点:%s" % hits
