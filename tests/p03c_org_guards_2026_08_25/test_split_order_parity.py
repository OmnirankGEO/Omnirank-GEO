"""【P0-3c · R1-D】`sorted(order) not in (...)` 同谓词双写的**跨文件一致性锁**。

## 为什么这条必须存在

同一个会动钱的判断写在两处:

  · `middleware/billing.py`         —— 拆分顺序的权威校验(**五保护文件,一行不许动**)
  · `services/diagnosis_runs.py`    —— 结算前的自检(P0-3b 加的"先自检再送")

本仓的老教训:**同一谓词写两处 ⇒ 必有一处没人验**。
两处一旦漂移,后果不是报错而是**沉默地按不同规矩动钱**:
diagnosis_runs 放行的 order,billing 可能 raise;billing 认的形态,
diagnosis_runs 可能提前判成 `reserved_split_order_invalid` 转人工。

所以锁在**测试侧**(billing.py 是保护文件,不加运行时校验),
只读两处源码断言谓词等价 —— 任一侧改一个字就红。
"""
from __future__ import annotations

import ast
import io
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

SIDES = {
    "middleware/billing.py": "拆分顺序权威校验(保护文件 · 只读不改)",
    "services/diagnosis_runs.py": "结算前自检(先自检再送进资金原语)",
}


def _order_predicates(rel):
    """找出所有 `sorted(order) not in (...)` 形态的比较节点。

    按**结构**找,不按文本找:文本匹配会被换行/空格/引号风格骗过去,
    而漂移恰恰常常是换个写法带来的。
    """
    tree = ast.parse(io.open(REPO / rel, encoding="utf-8").read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Call)
                and isinstance(left.func, ast.Name) and left.func.id == "sorted"):
            continue
        if not (len(node.ops) == 1 and isinstance(node.ops[0], ast.NotIn)):
            continue
        out.append(node)
    return out


def test_the_two_sides_of_the_split_order_predicate_are_structurally_identical():
    """必须命中:两处谓词逐节点等价。任一侧改一个字 → 红。"""
    dumps = {}
    for rel in SIDES:
        nodes = _order_predicates(rel)
        assert len(nodes) == 1, (
            "%s 里 `sorted(...) not in (...)` 形态应恰好 1 处,实测 %d 处 —— "
            "分母变了,这把锁就不知道该比哪一个" % (rel, len(nodes)))
        # 去掉行号/列号这类位置信息再比,只比结构与字面量。
        dumps[rel] = ast.dump(nodes[0], annotate_fields=True, include_attributes=False)

    a, b = (dumps[k] for k in SIDES)
    assert a == b, (
        "同谓词双写已经漂移 —— 两处会按不同规矩动钱:\n"
        "middleware/billing.py : %s\nservices/diagnosis_runs.py: %s" % (a, b))


def test_the_locked_predicate_really_is_the_split_order_one():
    """配对的必须不命中:上面那条不能靠"两边都空/都一样地错"混绿。

    这里正面钉死它锁的确实是**拆分顺序**那个谓词:被比较的两个候选
    必须就是 billing 认的那两种池顺序。这条同时保证:哪天有人把两处
    **一起**改成别的语义,也会红(一致但错了,同样是问题)。
    """
    node = _order_predicates("middleware/billing.py")[0]
    assert isinstance(node.left.args[0], ast.Name) and node.left.args[0].id == "order", (
        "sorted() 的实参不是 `order`")
    candidates = node.comparators[0]
    assert isinstance(candidates, ast.Tuple), ast.dump(candidates)[:120]
    got = []
    for elt in candidates.elts:
        assert isinstance(elt, ast.List), ast.dump(elt)[:120]
        got.append([c.value for c in elt.elts])
    assert got == [["bonus", "commission", "paid"], ["commission", "paid"]], got
