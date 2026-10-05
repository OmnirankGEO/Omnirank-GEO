"""【P0-3c 件1】`organization_charge_id is not None` 守卫族的 census + 节点级方向锁。

## 为什么要有 census

P0-3b 我报"这个谓词在 server.py 出现 **3 次**",Review 复审数出是 **4 次**(漏了 2970)。
一个手写分母连着毒害了两件事:变异锚点打歪(考错题)、交回项范围报小了。
**手写分母漏掉的那一项不会让任何判据变红** —— 所以这里改成机械枚举 + 数量锁:
第 5 处出现时当场红,不再靠人数数。

## 作用域 = 这一类的作用域,不是我最先发现它的那个文件

P0-3c 件2 把成功路径的分派段抽进了 `services/diagnosis_runs.py`,守卫也跟着搬了一处。
如果 census 只扫 server.py,搬走的那处就悄悄退出了分母 —— 类锁必须覆盖**整类**。

## 静态锁只是地板

这四处**全部**另有运行时判据真跑(见 test_settlement_dispatch_runtime.py):
成功 org 臂 / 成功非 org 臂 / 内层 cex 释放 / 外层失败释放。
静态锁在这里的作用是"新增第 5 处时报警",不是唯一防线 ——
P0-3b 的教训就是静态锁当唯一防线会被"行在名不绑定"整类问题穿过去。
"""
from __future__ import annotations

import ast
import io
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]

PREDICATE = "organization_charge_id"

#: 每处守卫用**所在分支里唯一的被调名**锚定,不认行号(行号一改就漂)。
#: key = 该分支体内必然出现的唯一符号;value = 人话说明。
EXPECTED_SITES = {
    ("server.py", "mark_external_side_effect_started"):
        "任务启动:org 单先向 charge link 报「外部副作用已开始」",
    ("server.py", "_release_org_cex"):
        "内层 commit except:settle 中途抛 → 释放 charge(P0-3b R-a)",
    ("server.py", "_release_org_charge"):
        "外层 except:诊断失败 → 释放 charge",
    ("services/diagnosis_runs.py", "_settle_org_charge"):
        "成功路径分派:org 走 settle_charge,非 org 走 commit_run(P0-3c 件2 抽出)",
}

FILES = sorted({f for f, _ in EXPECTED_SITES})


def _tree(rel):
    return ast.parse(io.open(REPO / rel, encoding="utf-8").read())


def _guard_ifs(rel):
    """所有 test 里出现**裸** `organization_charge_id` 的 If 节点。

    裸名很重要:`_organization_charge_id`(下划线前缀)是 HTTP handler 里的另一个变量、
    另一个作用域,不属这一族。把它算进来会让分母虚高、锁变成噪音。
    """
    found = []
    for node in ast.walk(_tree(rel)):
        if not isinstance(node, ast.If):
            continue
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if PREDICATE in names:
            found.append(node)
    return found


def _body_names(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.alias):
            out.add(n.asname or n.name)
    return out


def test_every_guard_in_the_family_is_anchored_and_locked():
    """census:这一族**恰好**这几处,每处的方向都锁到节点。"""
    seen = {}
    for rel in FILES:
        for node in _guard_ifs(rel):
            # ① 方向锁(节点级):必须是 `organization_charge_id is not None`。
            #    反转成 `is None` → 这条当场红。
            test = node.test
            assert isinstance(test, ast.Compare), (
                "%s:%d 守卫不是比较表达式:%s" % (rel, node.lineno, ast.dump(test)[:120]))
            assert isinstance(test.left, ast.Name) and test.left.id == PREDICATE, (
                "%s:%d 守卫左值不是裸 %s" % (rel, node.lineno, PREDICATE))
            assert len(test.ops) == 1 and isinstance(test.ops[0], ast.IsNot), (
                "%s:%d 守卫方向反了或换了算子 —— org charge 会走进错误的分支:%s"
                % (rel, node.lineno, ast.dump(test)[:120]))
            assert (len(test.comparators) == 1
                    and isinstance(test.comparators[0], ast.Constant)
                    and test.comparators[0].value is None), (
                "%s:%d 守卫右值不是 None" % (rel, node.lineno))

            # ② 上下文锚:这一处属于哪一个已登记的站点
            names = _body_names(node)
            hits = [k for k in EXPECTED_SITES if k[0] == rel and k[1] in names]
            assert len(hits) == 1, (
                "%s:%d 这处守卫锚不到唯一站点(命中 %r)—— 要么是**新增的第 N 处**"
                "(那就把它登记进 EXPECTED_SITES 并配运行时判据),要么锚名不再唯一"
                % (rel, node.lineno, hits))
            key = hits[0]
            assert key not in seen, (
                "站点 %r 被匹配了两次(%d 与 %d)—— 锚名不唯一,锁会考错题"
                % (key, seen.get(key), node.lineno))
            seen[key] = node.lineno

    missing = set(EXPECTED_SITES) - set(seen)
    assert not missing, "登记过的守卫站点消失了(被删/被改写?):%r" % (missing,)


def test_the_predicate_count_equals_the_number_of_locked_sites():
    """数量锁:源码里这个谓词出现几次,就必须有几处上了锁。

    第 5 处出现 → 这条红。这是替代"我数了一遍是 4 处"的那个手写分母。
    """
    total = 0
    per_file = {}
    for rel in FILES:
        src = io.open(REPO / rel, encoding="utf-8").read()
        # 裸名:前面不能是标识符字符或下划线(排除 `_organization_charge_id`)
        n = len(re.findall(r"(?<![A-Za-z0-9_])" + PREDICATE + r"\s+is\s+not\s+None", src))
        per_file[rel] = n
        total += n
    assert total == len(EXPECTED_SITES), (
        "谓词出现 %d 次,但只登记/上锁了 %d 处:%r —— 新增的那一处没有任何东西在守"
        % (total, len(EXPECTED_SITES), per_file))


def test_paired_must_not_hit_the_underscore_variable_is_not_in_this_family():
    """配对的必须不命中:`_organization_charge_id` 是另一个变量,不能被算进这一族。

    没有这一条,上面那个数量锁可能靠"把所有形似的都数进来"凑对,
    真加了第 5 处裸名守卫时反而不红。
    """
    src = io.open(REPO / "server.py", encoding="utf-8").read()
    underscore = len(re.findall(r"_organization_charge_id\s+is\s+not\s+None", src))
    assert underscore > 0, (
        "预期 server.py 里存在 `_organization_charge_id is not None`(HTTP handler 侧的"
        "另一个变量)。它不存在的话,上面那条数量锁的排除逻辑就没有正样本在验")
    bare = len(re.findall(r"(?<![A-Za-z0-9_])organization_charge_id\s+is\s+not\s+None", src))
    assert bare < underscore + bare, "排除逻辑失效"
    assert bare == 3, (
        "server.py 里裸名守卫应为 3 处(件2 把成功分派那处搬进 services/diagnosis_runs.py 了),"
        "实测 %d 处" % bare)
