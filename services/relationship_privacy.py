"""关系类私有字段脱敏 · **全系统唯一一份清单**(工单 §0.1 R5)。

存在的理由(Owner 2026-08-13 红线,违反即 NO-GO):
    **绝不能让下级知道谁是他的上级。**
    关系是单向可见的 —— 上游看得见下游,下游永远看不见上游。

这份清单原本以**字面量元组**的形式散在 `api/brand_api.py:846` 与 `:1404` 两处。
工单 §0.1 明确:「本轮新增/改造的所有响应必须套用**同一份**脱敏清单,不要另起一套。」
两份字面量各自演化 = 两套口径 = 迟早有一处漏字段,因此这里把它提取成**单点**,
由所有调用方 import —— 加字段只有一个地方可加。

🔴 判别锁(tests/test_relationship_directionality.py):
    - 机械扫描:面向非 admin 的响应体里,这些 key 一个都不许出现(不靠人眼);
    - 变异测试:把任一 pop 删掉 → 测试必须转红。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping

# ============================================================
# 唯一清单
# ============================================================

#: 🔴 关系反推字段 —— 下游拿到其中任何一个都可能反推出上游是谁 / 关系存在与否 / 系数归属。
#: 字段名逐字对应工单 §4.1 第 9 条与 §9 完成定义 5b 的机械扫描清单。
RELATIONSHIP_PRIVATE_FIELDS: tuple[str, ...] = (
    "owner_user_id",
    "agent_user_id",
    "upstream_user_id",
    "service_account_code",
    "channel_account_code",
    "relationship_id",
    "relationship_version",
    "cost_multiplier",
)

#: `cost_multiplier*` 是**前缀**匹配(工单写的是 `cost_multiplier*`):
#: `cost_multiplier_bps` / `cost_multiplier_note` 之类换个后缀就绕过精确匹配,
#: 那正是「secret 不能是 allowed label 的子串」同型的坑,所以这里按前缀封。
RELATIONSHIP_PRIVATE_PREFIXES: tuple[str, ...] = (
    "upstream_",
    "cost_multiplier",
)


def is_private_relationship_field(name: str) -> bool:
    """单个字段名是否属于关系私有字段(精确 + 前缀两条都算)。"""
    key = str(name)
    if key in RELATIONSHIP_PRIVATE_FIELDS:
        return True
    return any(key.startswith(prefix) for prefix in RELATIONSHIP_PRIVATE_PREFIXES)


def strip_private_relationship_fields(
    payload: Any, *, extra_fields: Iterable[str] = (),
) -> Any:
    """递归剥掉关系私有字段。

    ⚠️ **递归**是必须的:泄露位常常藏在嵌套 dict / list 里(`{"items":[{...}]}`),
    只 pop 顶层等于没做。

    `extra_fields` 给调用方叠加自己那几个额外的本地私有字段(如 brand 的
    `owner_name` / `created_by`),但**不允许**调用方缩小基础清单。
    """
    extra = {str(f) for f in extra_fields}

    def _clean(node: Any) -> Any:
        if isinstance(node, Mapping):
            out: Dict[str, Any] = {}
            for key, value in node.items():
                if is_private_relationship_field(key) or str(key) in extra:
                    continue
                out[str(key)] = _clean(value)
            return out
        if isinstance(node, (list, tuple)):
            return [_clean(item) for item in node]
        return node

    return _clean(payload)


def find_private_relationship_fields(payload: Any) -> list[str]:
    """返回 payload 中**仍然存在**的关系私有字段路径 —— 给测试做机械断言用。

    空列表 = 干净。测试断言 `== []` 而不是断言某个具体字段不在,
    这样新加的泄露字段也会被自动抓到(见 feedback:判据要机械不要手挑)。
    """
    hits: list[str] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                if is_private_relationship_field(str(key)):
                    hits.append(child)
                _walk(value, child)
        elif isinstance(node, (list, tuple)):
            for idx, item in enumerate(node):
                _walk(item, f"{path}[{idx}]")

    _walk(payload, "")
    return hits
