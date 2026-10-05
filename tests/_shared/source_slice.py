# -*- coding: utf-8 -*-
"""按**真实边界**取源码片段 —— 替掉 `src[i : i + 魔法字节数]`。

═══════════════════════════════════════════════════════════════════
🔴 这个 helper 存在的理由:同一个病在本仓复发了 9 次

判据写成 `src[fn_start : fn_start + N]`,N 是**当年**那个函数的长度。
函数长大了,N 没跟着长 —— 于是断言的那个串明明**就在函数体内**,
却掉在窗口外,判据判红。产品行为一个字节没变。

WO_205(2026-09-15)逐条量过的偏移 / 窗口 / 函数真长。

🔴 **参照物 = 基线树 `wt-c14-225-base` @ HEAD**(判据在那棵树上是红的)。
   口径:函数真长 = `ast.get_source_segment` 的字符数(不含装饰器行);
   偏移 = 该判据断言的所有串里**最远**那个,相对函数起点。
   量法脚本 `scratchpad/wo205_remeasure.py`,换棵树重跑即可复现。

| 判据 | 最远锚偏移 | 旧窗口 | 函数真长 |
|---|---|---|---|
| v2101 uses_plan_by_topic_id            | 14163              | 12000 | 21279 |
| v2101 no_global_persist_uc_clear       | 16745              | 12000 | 21279 |
| v2101 single_dropdown_writes_manual    | 14465              | 12000 | 21279 |
| v2102 strict_match_by_kw_slot          | 15296              | 14000 | 21279 |
| v2102 save_topics_batch_writes_source  | 5044 (注1)         |  3000 |  6927 |
| v2102 consistency_uc_null_source_null  | 3013               |  3000 |  6927 |
| v2103 ktg_parser_fills_original_keyword| 3834               |  3500 |  9130 |
| v2104 regenerate_uses_t_slot_index     | 15731              | 14000 | 24493 |
| v2104 slot_index_lookup_not_reindexed  | 15946              | 14000 | 24493 |

注1:这条不是被**字面量**打红的 —— 它的字面量最远只到 2185,还在窗口里。
     打红它的是那条正则 `INSERT INTO topics[^;]*user_choice_source`:
     左锚 `INSERT INTO topics` 在偏移 **5044**,被 3000 的窗口切掉了。
     🔴 量「爆没爆窗」时**正则的左锚也算锚**,只数字面量会把这条误判成「不是爆窗」。

`consistency_uc_null_source_null` 差 **13 个字符**。

🔴 换个参照物数就变:同一条在**本树**(叠了 WO_225 那笔,`save_topics_batch`
   长到 7551)偏移是 3530,超出 530。报这类数必须点名量的是哪棵树。

🔴 为什么这次做成共用 helper 而不是各改各的:
   2026-07-28 已经有人为**一个**函数修过同一个病
   (`tests/test_v2102_codex_review_fixes._generate_titles_body`,注释写得很清楚:
   「改成 AST 取真实函数体:窗口再也不会漂」)。
   但那是个**局部函数**,没传出去 —— 此后同一个病又复发了 9 次。
   注释传不出去,门才传得出去:这个模块就是那道门。

🔴 它**不放松**任何判据:窗口从「猜的字节数」换成「函数真实边界」,
   断言一个字都不改。放大窗口到整份文件才是放松(那样别处的同名行也能满足)。
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import ast
import os
from typing import Optional

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


def read_text(rel_path: str) -> str:
    """读仓内文件。路径相对仓根。"""
    with open(os.path.join(_ROOT, rel_path), "r", encoding="utf-8") as fh:
        return fh.read()


def function_body(rel_path: str, name: str, *, src: Optional[str] = None) -> str:
    """取 `rel_path` 里名为 `name` 的函数/方法的**完整源码**(含 def 行)。

    同名多处时取**第一个**,并把处数写进异常信息 —— 同名多处是个信号:
    判据可能钉错了那一个(本仓 `update_task_status` 就有三个同名函数写三张表)。

    🔴 找不到就抛,不返回空串:返回空串会让 `assert "x" in body` 稳定判红,
       而读起来像「代码里没有 x」—— 仪器坏了要出声,不要装作测到了。
    """
    source = src if src is not None else read_text(rel_path)
    tree = ast.parse(source)
    found = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    if not found:
        raise AssertionError(
            "%s 里找不到函数 %s —— 判据钉的对象没了(改名?搬家?),"
            "这不是「断言的串不在」,先去核对象" % (rel_path, name))
    body = ast.get_source_segment(source, found[0])
    if not body:
        raise AssertionError("%s::%s 取不到源码段(ast.get_source_segment 返空)" % (rel_path, name))
    if len(found) > 1:
        # 不抛:同名多处是合法的(不同类的方法)。但要让读的人知道它取了第一个。
        body = ("# [source_slice] 注意:%s 里有 %d 个同名 %s,本片段是**第一个**\n"
                % (rel_path, len(found), name)) + body
    return body


def class_body(rel_path: str, name: str, *, src: Optional[str] = None) -> str:
    """同上,取类体。"""
    source = src if src is not None else read_text(rel_path)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            body = ast.get_source_segment(source, node)
            if body:
                return body
    raise AssertionError("%s 里找不到类 %s" % (rel_path, name))


#: 行注释/块注释的形状,按语言。
_COMMENT_RULES = {
    "py": [(r"#[^\n]*", "")],
    "ts": [(r"/\*.*?\*/", ""), (r"//[^\n]*", "")],
}


def code_only(text: str, lang: str = "py") -> str:
    """把注释剥掉,只留代码。

    🔴 为什么需要它:**断言被注释满足**在本仓是累犯形态。
       WO_205 实测两处:
         · `test_v2102_save_topics_batch_writes_source` 的正则
           `INSERT INTO topics[^;]*user_choice_source`(DOTALL)——
           把列名从 INSERT 里删掉,它照样绿:参数行旁边那句
           `# v2.10.2 user_choice_source(...)` 注释满足了它;
         · `DistributionConfigDialog.tsx` 里 `distributableDirections()` 出现两次,
           一次是真调用、一次是抬头注释;把真调用换掉,注释仍让断言通过。
       两处都属「一个串被无关行满足」,而无关行是**注释** —— 注释改不改都不影响行为,
       所以它满足的断言等于没断言。

    🔴 这是个粗剥:不处理字符串字面量里的 `#` / `//`。
       够用的理由是它只服务于源码切片判据(判据本来就在做文本匹配);
       要更精确得上词法分析,那时这个函数应该被替掉而不是被加料。
    """
    import re as _re

    rules = _COMMENT_RULES.get(lang)
    if rules is None:
        raise AssertionError("code_only 不认识语言 %r(只支持 %s)"
                             % (lang, sorted(_COMMENT_RULES)))
    out = text
    for pattern, repl in rules:
        out = _re.sub(pattern, repl, out, flags=_re.DOTALL)
    return out
