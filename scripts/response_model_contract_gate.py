#!/usr/bin/env python3
"""G1 · 响应契约静态门禁(工单 WO_RESPONSE_MODEL_CONTRACT_GATE_2026-08-14 §3)。

存在的理由:同一失败模式 2026-08 内**第三次** —— handler 多返回一个键,
而 `response_model` 是 `extra="forbid"` → 该端点每次必 500。
三次都是上线后由 Owner 点按钮才发现:
    08-06 列表 `account_origin` · 08-13 详情嵌套层 `needs_attention` · 08-14 补录 `backfill`
第一次修完在代码里留了警告注释,注释就写在第二次要改的类上方十几行 —— **照样没拦住**。
**注释传不出去,只有会自己报错的东西才拦得住。**

🔴 本门禁**明确抓不到**(不是缺陷,是边界,写在这里免得被当成全覆盖):
    · `**kwargs` 展开        · `d.update(...)` 之后再 return
    · 运行时计算出来的键      · 超过一跳的间接构造
  → 所以 G1 **不能单独交付**,必须配 G2 运行时锁(`tests/response_model_contract/`)。

用法:
    python scripts/response_model_contract_gate.py            # 门禁模式,有问题 exit 1
    python scripts/response_model_contract_gate.py --report   # 只打报告,恒 exit 0
"""

from __future__ import annotations

import argparse
import datetime
import ast
import inspect
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认 GBK,中文/emoji 会崩
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ============================================================
# 模型字段树(递归展开嵌套 —— 第 2 次事故就炸在嵌套层)
# ============================================================

def _unwrap_annotation(annotation: Any) -> List[Any]:
    """把 Optional[X] / List[X] / Dict[str, X] / Union[...] 剥到里层的候选类型。"""
    import typing

    out: List[Any] = []
    origin = typing.get_origin(annotation)
    if origin is None:
        return [annotation]
    for arg in typing.get_args(annotation):
        if arg is type(None):
            continue
        out.extend(_unwrap_annotation(arg))
    return out


def model_field_tree(model: Any, _seen: Optional[Set[int]] = None) -> Optional[Dict[str, Any]]:
    """返回 {字段名: 子树 or None}。子树非 None 表示该字段是嵌套模型。

    `_seen` 防自引用模型无限递归。
    """
    from pydantic import BaseModel

    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        return None
    _seen = set(_seen or ())
    if id(model) in _seen:
        return {}
    _seen.add(id(model))

    tree: Dict[str, Any] = {}
    for name, field in model.model_fields.items():
        child = None
        for candidate in _unwrap_annotation(field.annotation):
            child = model_field_tree(candidate, _seen)
            if child is not None:
                break
        tree[name] = child
    return tree


def model_forbids_extra(model: Any) -> bool:
    from pydantic import BaseModel

    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        return False
    return (getattr(model, "model_config", {}) or {}).get("extra") == "forbid"


def iter_nested_forbid_count(model: Any, _seen: Optional[Set[int]] = None) -> int:
    """统计 response_model 树里 forbid 嵌套模型出现次数(工单 §1.2 的 78)。"""
    from pydantic import BaseModel

    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        return 0
    _seen = set(_seen or ())
    if id(model) in _seen:
        return 0
    _seen.add(id(model))
    total = 0
    for field in model.model_fields.values():
        for candidate in _unwrap_annotation(field.annotation):
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                if model_forbids_extra(candidate):
                    total += 1
                total += iter_nested_forbid_count(candidate, _seen)
    return total


# ============================================================
# handler AST:收集 return 路径上的 dict 字面量键
# ============================================================

class _ReturnDictCollector(ast.NodeVisitor):
    """收集函数内 return 的 dict 字面量(含"先赋值给变量再 return"这一种)。

    产出 [(层级路径, 键名, 行号)],层级路径为 () 表示顶层。
    """

    def __init__(self) -> None:
        self.assigned: Dict[str, ast.Dict] = {}
        # 🔴 `result = svc(...)` 之后 `return result` 是**最常见**的 handler 形状
        #    (详情端点就是)。只跟踪"被赋 dict 字面量"的变量,一跳递归永远不触发,
        #    嵌套层完全看不见 —— 判据 2 第一次就是这么漏的。
        self.assigned_calls: Dict[str, ast.Call] = {}
        self.returned_dicts: List[ast.Dict] = []
        self.returned_names: List[str] = []
        self.returned_calls: List[ast.Call] = []
        # 🔴 `result.pop("k")` 会把键**去掉**再返回 —— 不建模就是误报。
        #    门禁一旦有误报就会被当噪音整体忽略,那比没有门禁更糟。
        self.popped_keys: Set[str] = set()
        self.has_unanalyzable_return = False
        #: 本函数里定义的嵌套函数(名 → 节点)。它们的返回值属于**它们自己**,
        #: 不属于本函数;但当它们的结果被放进某个字段时,要按该字段的元素模型比。
        self.nested_funcs: Dict[str, ast.AST] = {}
        #: `name = [...]` / `name = [f(x) for x in ...]` —— 列表字段的来源。
        self.assigned_lists: Dict[str, ast.AST] = {}
        #: 🔴 [c1'] 赋过 dict 字面量、**之后又被改写**的名字。
        #:    `payload["x"]=…` / `.update()` / `.setdefault()` / `.pop()` / `|=`
        #:    都会让那份字面量变成**过期快照**。展开过期快照比不展开更坏:
        #:    不展开只是"我不知道"(记不可信),展开则是**声称看过了**。
        self.mutated: Set[str] = set()

    # 🔴 [WO_209 c1] 嵌套函数**不下钻**,但要记名字。
    #    这是 09-14 那三条假阳性的真正机制:`suggest-questions` 的 handler 里
    #    有个内层 helper `_cand()`,它 `return {"question","side","layer"}`。
    #    `generic_visit` 一路钻进去,把 helper 的返回 dict 当成端点自己的返回,
    #    拿去和**外层**模型 `SuggestQuestionsResponse` 比 —— 三个键当然都"未声明"。
    #    (工单把症状写成「跟不进 List[dict] → List[<BaseModel>]」;
    #     模型那侧 `model_field_tree` 其实早就会剥 List[X],坏的是观测键这侧。)
    #    🔴 只"不下钻"会从**误报**变成**瞎**:那些键就没人比了。
    #       所以同时记下嵌套函数,供 `_element_dicts_for` 按元素模型回来比。
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.nested_funcs[node.name] = node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return          # lambda 体不是本函数的返回

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name):        # payload |= {...}
            self.mutated.add(node.target.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)                 and func.attr in ("update", "setdefault", "pop", "popitem", "clear"):
            self.mutated.add(func.value.id)
        # 🔴 [c1''] 本地 dict **被当参数传出去** ⇒ 保守记成改写过。
        #    `enrich(payload)` 之后 payload 里有什么,静态看不见 ——
        #    被调方往里塞一个键,门禁照样声称"我展开看过了"。
        #    宁可多记一条盲区,也不要多一条谎报:盲区是知道自己不知道,
        #    谎报是不知道自己不知道。
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Name):
                self.mutated.add(arg.id)
        if isinstance(func, ast.Attribute) and func.attr in ("pop", "popitem"):
            if node.args and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str):
                    self.popped_keys.add(value)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for tgt in node.targets:                     # payload["x"] = ...
            if isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Name):
                self.mutated.add(tgt.value.id)
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if isinstance(node.value, ast.Dict):
                self.assigned[name] = node.value
            elif isinstance(node.value, ast.Call):
                self.assigned_calls[name] = node.value
            elif isinstance(node.value, (ast.List, ast.ListComp)):
                self.assigned_lists[name] = node.value
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        value = node.value
        if value is None:
            return
        if isinstance(value, ast.Dict):
            self.returned_dicts.append(value)
        elif isinstance(value, ast.Name):
            self.returned_names.append(value.id)
        elif isinstance(value, ast.Call):
            self.returned_calls.append(value)
        else:
            # 三元 / await / 属性访问等 —— 明确记为"看不懂",不当成"没问题"
            self.has_unanalyzable_return = True
        self.generic_visit(node)


def _dict_literal_keys(
    node: ast.Dict, assigned: Optional[Dict[str, ast.Dict]] = None,
    mutated: Optional[Set[str]] = None,
) -> List[Tuple[str, int, Optional[ast.Dict], Any]]:
    """一层 dict 字面量的 (键名, 行号, 子 dict 字面量)。

    `**expr` 展开的键**无法**静态得知 —— 返回一个哨兵,调用方据此跳过该层
    (而不是当成"这一层没有多余键")。

    🔴 子层的值若是**变量名**且该变量在本函数里被赋过 dict 字面量,要解析进去。
       不解析的话,`return {"overview": overview}` 这种写法的嵌套层完全看不见 ——
       第 2 次事故(`needs_attention` 塞进 overview)正是这个形状,
       本门禁第一版就因此漏掉了它(判据 2 当场变红,才补上这段)。
    """
    assigned = assigned or {}
    mutated = mutated or set()
    out: List[Tuple[str, int, Optional[ast.Dict], Any]] = []
    for key, value in zip(node.keys, node.values):
        if key is None:  # {**other}
            # 🔴 [WO_209 c1] `{**name, ...}` 里的 `name` 若是**本函数里赋过的
            #    dict 字面量**,它的键是静态可知的 —— 直接展开,别当"不可知"。
            #    `suggest-questions` 正是这个形状(`payload = {...}` 之后
            #    `return {**payload, "cacheKey": ..., "cachedAt": ...}`):
            #    不展开的话 `candidates` 这个键**从头到尾没人看过**,
            #    于是「把内层键删掉」这发毒打不红 —— 门禁对它是瞎的。
            #    (改之前那三条假阳性能冒出来,靠的是钻进内层 helper 当顶层返回,
            #     比的是错的对象;拿掉那条路之后,真正的覆盖缺口才露出来。)
            if (isinstance(value, ast.Name) and value.id in assigned
                    and value.id not in mutated):
                inner = assigned[value.id]
                if inner is not node:          # 自引用保护
                    out.extend(_dict_literal_keys(inner, assigned, mutated))
                    continue
            out.append((_STARSTAR, getattr(node, "lineno", 0), None, None))
            continue
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            out.append((_DYNAMIC, getattr(key, "lineno", 0), None, None))
            continue
        child: Optional[ast.Dict] = None
        if isinstance(value, ast.Dict):
            child = value
        elif isinstance(value, ast.Name) and value.id in assigned:
            child = assigned[value.id]
        out.append((key.value, getattr(key, "lineno", 0), child, value))
    return out


_STARSTAR = "<**expansion>"
_DYNAMIC = "<computed-key>"


def _element_dicts_for(value: Any, collector: Any) -> Optional[List[ast.Dict]]:
    """某字段的值是「一串元素」时,把每个元素的 dict 字面量取出来。

    [WO_209 c1 §1.1] 覆盖三种写法:
      · `"k": [{...}, {...}]`                     —— 直接列表字面量
      · `"k": [_cand(x) for x in xs]`             —— 列表推导,元素是**本地函数**调用
      · `"k": name`,而 `name = [...]` / `name = [f(x) for x in xs]`
    取到的 dict 会按该字段的**元素模型**比对,而不是外层模型 —— 这正是
    09-14 那三条假阳性的反面:不是不比,是**比错了对象**。

    🔴 跟不动的形状(元素是变量、跨模块函数、三元…)返 **None**,
       调用方据此记 `trustworthy=False`:「没查出违规」不等于「没有违规」。
       c1 第一版这里返空列表,而 docstring 写的是"调用方会置 False" ——
       调用方**没做**,于是 `{"candidates": items}`(元素来自参数)这种形状
       会被记成**可信**。注释说了什么不算数,只有代码算数。
    """
    nodes: List[ast.AST] = []
    if isinstance(value, ast.Name) and value.id in collector.assigned_lists:
        value = collector.assigned_lists[value.id]
    if isinstance(value, ast.List):
        nodes = list(value.elts)
    elif isinstance(value, ast.ListComp):
        nodes = [value.elt]
    else:
        return None                 # 跟不动:这个字段里到底放了什么键,我们不知道

    out: List[ast.Dict] = []
    for n in nodes:
        if isinstance(n, ast.Dict):
            out.append(n)
            continue
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            target = collector.nested_funcs.get(n.func.id)
            if target is not None:
                inner = _collect_from(target)
                out.extend(inner.returned_dicts)
                for nm in inner.returned_names:
                    if nm in inner.assigned:
                        out.append(inner.assigned[nm])
                # 🔴 [c1''] 内层函数**没能把返回值落成 dict 字面量**的每一种形状
                #    都算跟不动。原来只挡了 returned_calls / 看不懂的返回,漏了
                #    `return q`(直接返参数)与 `d = build(x); return d`
                #    (名字绑到调用结果)—— 那两种下 `out` 一条元素都没收到,
                #    却因为没触发任何一条"跟不动"而把这一层记成**可信**:
                #    元素零比较 + 声称看过了,正是 c1' 要消灭的那类谎报。
                unresolved = [nm for nm in inner.returned_names
                              if nm not in inner.assigned]
                if (inner.returned_calls or inner.has_unanalyzable_return
                        or unresolved or not inner.returned_dicts):
                    return None
                continue
        return None                 # 元素是变量 / 跨模块函数 / 三元 …
    return out


def _check_dict_against_tree(
    node: ast.Dict, tree: Optional[Dict[str, Any]], path: Tuple[str, ...],
    assigned: Optional[Dict[str, ast.Dict]] = None,
    collector: Any = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """把一层 dict 字面量与模型字段树逐键比对,递归进嵌套层。

    返回 (违规列表, 本层是否可信)。本层含 `**` 或计算键时**不可信** ——
    那种情况下"没查出违规"不等于"没有违规"(G1 的已知盲区)。
    """
    if tree is None:
        return [], True
    violations: List[Dict[str, Any]] = []
    trustworthy = True
    for key, lineno, child, value in _dict_literal_keys(
            node, assigned, getattr(collector, "mutated", None)):
        if key in (_STARSTAR, _DYNAMIC):
            trustworthy = False
            continue
        if key not in tree:
            violations.append({"path": path + (key,), "lineno": lineno})
            continue
        if child is not None:
            sub_violations, sub_ok = _check_dict_against_tree(
                child, tree[key], path + (key,), assigned, collector,
            )
            violations.extend(sub_violations)
            trustworthy = trustworthy and sub_ok
        elif collector is not None and isinstance(tree[key], dict):
            # [WO_209 c1] 字段是嵌套模型,但值不是单个 dict —— 多半是**一串元素**。
            # 把元素的键归到**元素模型**再比(见 `_element_dicts_for`)。
            elems = _element_dicts_for(value, collector)
            if elems is None:
                # 🔴 跟不动这个字段里放了什么 ⇒ 记不可信,**不是**记"没问题"。
                trustworthy = False
                continue
            for elem in elems:
                sub_violations, sub_ok = _check_dict_against_tree(
                    elem, tree[key], path + (key,), assigned, collector,
                )
                violations.extend(sub_violations)
                trustworthy = trustworthy and sub_ok
    return violations, trustworthy



def _func_ast(func: Any) -> Optional[ast.FunctionDef]:
    node, _origin = _func_ast_with_origin(func)
    return node


def _func_ast_with_origin(func: Any):
    """返回 (AST, (源文件, 起始行-1))。

    🔴 行号必须还原成**真实文件行号**:`inspect.getsource` 拿到的是函数片段,
       `ast.parse` 出来的 lineno 从 1 起算。不加偏移就会指到别的文件的第 64 行 ——
       门禁报了个错位置,等于让人去错的地方找,比不报还费时间。
    """
    try:
        source = inspect.getsource(func)
        _, start = inspect.getsourcelines(func)
        filename = inspect.getsourcefile(func)
    except (OSError, TypeError):
        return None, (None, 0)
    try:
        parsed = ast.parse(_dedent(source))
    except SyntaxError:
        return None, (None, 0)
    for node in ast.walk(parsed):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node, (filename, max(start - 1, 0))
    return None, (None, 0)


def _dedent(source: str) -> str:
    import textwrap

    return textwrap.dedent(source)


def _resolve_one_hop(func: Any, call: ast.Call) -> Optional[Any]:
    """handler 直接 `return some_service(...)` 时,取到那个 service 函数对象。

    只跳**一跳**(工单 §3 G1 第 2 条)。更深的间接构造是已知盲区。
    """
    target = call.func
    name = None
    if isinstance(target, ast.Name):
        name = target.id
    elif isinstance(target, ast.Attribute):
        name = target.attr
    if not name:
        return None
    module = inspect.getmodule(func)
    if module is None:
        return None
    candidate = getattr(module, name, None)
    if callable(candidate):
        return candidate
    # handler 里常见 `from services.x import f` 写在函数体内 —— 扫函数内的 import
    node = _func_ast(func)
    if node is None:
        return None
    for sub in ast.walk(node):
        if isinstance(sub, ast.ImportFrom) and sub.module:
            for alias in sub.names:
                if (alias.asname or alias.name) == name:
                    try:
                        mod = __import__(sub.module, fromlist=[alias.name])
                    except Exception:  # noqa: BLE001
                        return None
                    return getattr(mod, alias.name, None)
    return None


def _collect_from(func_node: Any) -> "_ReturnDictCollector":
    """在**函数体**上跑收集器,而不是在 FunctionDef 节点上跑。

    🔴 必须逐条 visit `body`:收集器现在有 `visit_FunctionDef`(嵌套函数不下钻),
       直接 `visit(func_node)` 会被它自己拦下 —— 整个函数体一行都不走,
       于是「0 违规」和「0 盲区」一起出现,看起来像全通过。
       2026-09-15 改这条时当场踩了一次:违规 3→0 的同时盲区 51→0,
       是那个**盲区分母**把仪器死掉这件事暴露出来的。
    """
    c = _ReturnDictCollector()
    for stmt in getattr(func_node, "body", []):
        c.visit(stmt)
    return c


def analyze_endpoint(func: Any, response_model: Any) -> Dict[str, Any]:
    """对一条路由做静态比对。"""
    tree = model_field_tree(response_model)
    result: Dict[str, Any] = {"violations": [], "analyzed": False, "trustworthy": True,
                              "reason": ""}
    if tree is None:
        result["reason"] = "response_model 不是 BaseModel(可能是 dict/Any)"
        return result

    node = _func_ast(func)
    if node is None:
        result["reason"] = "拿不到 handler 源码"
        result["trustworthy"] = False
        return result

    _, handler_origin = _func_ast_with_origin(func)
    collector = _collect_from(node)

    # 第三位从 `assigned` 换成**收集器本身**:`_element_dicts_for` 还要用它的
    # `nested_funcs` / `assigned_lists` 把列表字段的元素解析出来。
    dicts: List[Tuple[ast.Dict, Tuple[Optional[str], int], Any]] = [
        (d, handler_origin, collector) for d in collector.returned_dicts
    ]
    one_hop_calls: List[ast.Call] = list(collector.returned_calls)
    for name in collector.returned_names:
        if name in collector.assigned:
            dicts.append((collector.assigned[name], handler_origin, collector))
        elif name in collector.assigned_calls:
            # `result = svc(...)` → `return result`:等价于直接返回该函数结果,同样走一跳
            one_hop_calls.append(collector.assigned_calls[name])
        else:
            result["trustworthy"] = False  # 返回了一个我们没跟踪到的变量

    # 递归一跳:handler 直接返回 service 函数结果
    for call in one_hop_calls:
        target = _resolve_one_hop(func, call)
        if target is None:
            result["trustworthy"] = False
            continue
        inner, inner_origin = _func_ast_with_origin(target)
        if inner is None:
            result["trustworthy"] = False
            continue
        inner_collector = _collect_from(inner)
        dicts.extend(
            (d, inner_origin, inner_collector)
            for d in inner_collector.returned_dicts
        )
        for name in inner_collector.returned_names:
            if name in inner_collector.assigned:
                dicts.append(
                    (inner_collector.assigned[name], inner_origin, inner_collector)
                )
            else:
                result["trustworthy"] = False
        if inner_collector.has_unanalyzable_return:
            result["trustworthy"] = False

    if collector.has_unanalyzable_return:
        result["trustworthy"] = False

    popped = set(collector.popped_keys)
    for literal, origin, scope in dicts:
        violations, ok = _check_dict_against_tree(
            literal, tree, (), scope.assigned, scope)
        # handler 在 return 之前 pop 掉的顶层键不算违规(只扣顶层 —— 嵌套层的 pop
        # 需要跟踪对象身份,超出静态分析边界,那种情况仍会报,宁可误报也不漏报)
        violations = [v for v in violations if not (len(v["path"]) == 1 and v["path"][0] in popped)]
        filename, offset = origin
        for violation in violations:
            violation["file"] = filename
            violation["lineno"] = violation["lineno"] + offset
        result["violations"].extend(violations)
        result["trustworthy"] = result["trustworthy"] and ok

    result["analyzed"] = bool(dicts)
    return result


# ============================================================
# 遍历路由表(🔴 不维护清单 —— 清单必漏)
# ============================================================

def collect_routes() -> Tuple[List[Dict[str, Any]], int]:
    os.environ.setdefault("GEO_SKIP_STARTUP_TASKS", "1")
    import server  # noqa: F401  —— 触发全部 router 注册

    app = server.app
    routes: List[Dict[str, Any]] = []
    total = 0
    for route in app.routes:
        total += 1
        model = getattr(route, "response_model", None)
        if model is None:
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        routes.append({
            "path": getattr(route, "path", "?"),
            "methods": sorted(getattr(route, "methods", []) or []),
            "model": model,
            "endpoint": endpoint,
        })
    return routes, total


#: [WO_209 c1 §1.2] 三种结局,三个退出码。
#: 🔴 「没跑成」既不许显示成「有违规」(2026-08-14 那次误告:主仓工作树没有这个
#:    脚本 → python 报 can't open file → rc≠0 → preflight 打出「上线即 500」),
#:    也不许显示成 0(那是把 dark 当通过 —— 09-04 之后 G1 dark 了数天没人知道)。
#:    假红和假绿在退出码上一模一样时,人会学会忽略这个闸。
EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_DID_NOT_RUN = 3

#: 🔴 机器可读的结论行。**别让下游去读「stdout 第一行」**:
#:    这个门禁 `import server`,导入期会刷一屏 `[OK] xxx 表已创建`,
#:    第一行根本不归门禁管(2026-09-15 写判据时当场撞上)。
#:    preflight(209-d1)grep 这个前缀取结论,不解析自然语言。
VERDICT_PREFIX = "G1-VERDICT:"
_VERDICT_TEXT = {EXIT_OK: "ok", EXIT_VIOLATION: "violation",
                 EXIT_DID_NOT_RUN: "did-not-run"}


def _verdict(code: int, human: str) -> None:
    print("%s %s · %s" % (VERDICT_PREFIX, _VERDICT_TEXT[code], human))


def _tree_depth(tree: Optional[Dict[str, Any]]) -> int:
    """字段树的最大嵌套深度(分母之一:门禁到底跟进了几层)。"""
    if not tree:
        return 0
    return 1 + max((_tree_depth(v) for v in tree.values() if isinstance(v, dict)),
                   default=0)


def _state_path() -> str:
    """最近一次**真跑起来**的时间戳落盘处。

    默认写包树里的 `.g1_state/`;Deploy 侧用 `G1_STATE_DIR` 指到
    `.deploy_toolkit/state/`(工单 §1.3)。不硬编码绝对路径:
    这个脚本在包树里跑,写死绝对路径就会跟着某一台机器走。
    """
    base = os.environ.get("G1_STATE_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".g1_state")
    return os.path.join(base, "g1_last_ok.txt")


def _read_last_ok() -> Optional[str]:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _write_last_ok(stamp: str) -> Optional[str]:
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(stamp)
            fh.write(chr(10))
        return path
    except OSError as exc:              # 落不了盘不该让门禁失败,但要说出来
        print("  警告:时间戳落盘失败(%s)—— dark 检测会失效,请检查 G1_STATE_DIR" % exc)
        return None


def _dark_hours(last: Optional[str]) -> Optional[float]:
    if not last:
        return None
    try:
        then = datetime.datetime.fromisoformat(last)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - then).total_seconds() / 3600.0


def main(argv: Optional[List[str]] = None) -> int:
    # 🔴 收 argv:在别的进程里被 import 再调用时(判据就是这么用的),
    #    `parse_args()` 会去读那个进程的 sys.argv —— pytest 的参数一进来就 SystemExit(2),
    #    而那种退出码既不是 0/1 也不是 3,谁都解释不了。
    parser = argparse.ArgumentParser(description="响应契约静态门禁(G1)")
    parser.add_argument("--report", action="store_true", help="只打报告,恒 exit 0")
    parser.add_argument("--max-dark-hours", type=float, default=48.0,
                        help="上次成功运行超过这么多小时就出声(默认 48)")
    args = parser.parse_args(argv)

    last_ok = _read_last_ok()
    dark_h = _dark_hours(last_ok)

    # ── 先分「跑没跑起来」,再分「过没过」 ──────────────────────────
    try:
        routes, total_routes = collect_routes()
    except Exception as exc:                                  # noqa: BLE001
        _verdict(EXIT_DID_NOT_RUN, "路由收集抛异常 —— %s: %s"
                 % (type(exc).__name__, str(exc)[:200]))
        print("  这**不是**发现了违规。常见原因:库不可达 / server 导入失败。")
        return EXIT_DID_NOT_RUN

    if total_routes == 0:
        _verdict(EXIT_DID_NOT_RUN, "收集到 0 条路由 —— server 没起来或 router 一个都没注册")
        print("  这**不是**「没有违规」。0 条路由时门禁什么都没检查。")
        return EXIT_DID_NOT_RUN
    if not routes:
        _verdict(EXIT_DID_NOT_RUN, "%d 条路由里没有一条声明 response_model" % total_routes)
        print("  覆盖数为 0 = 门禁是瞎的,不能按通过处理。")
        return EXIT_DID_NOT_RUN

    trees = [model_field_tree(r["model"]) for r in routes]
    parsed = [t for t in trees if t is not None]
    if not parsed:
        _verdict(EXIT_DID_NOT_RUN, "%d 条路由的 response_model 一个都没解析出字段树" % len(routes))
        print("  多半是 pydantic 版本/导入出了问题,不是这些端点都合规。")
        return EXIT_DID_NOT_RUN

    forbid_routes = [r for r in routes if model_forbids_extra(r["model"])]
    nested_forbid = sum(iter_nested_forbid_count(r["model"]) for r in routes)
    max_depth = max((_tree_depth(t) for t in parsed), default=0)

    findings: List[str] = []
    untrusted = 0
    for route in routes:
        outcome = analyze_endpoint(route["endpoint"], route["model"])
        if not outcome["trustworthy"]:
            untrusted += 1
        for violation in outcome["violations"]:
            where = ".".join(violation["path"])
            model_name = getattr(route["model"], "__name__", str(route["model"]))
            src = "%s:%s" % (violation.get("file") or "?", violation["lineno"])
            findings.append(
                "🔴 %s %s%s     模型 %s 未声明键: %s%s     位置 %s"
                % ("/".join(route["methods"]), route["path"], chr(10),
                   model_name, where, chr(10), src))

    # ── 第一行就说清是哪一种结局(工单 §1.2)──────────────────────
    if findings:
        _verdict(EXIT_VIOLATION,
                 "发现 %d 处 handler 返回了模型未声明的键(上线即 500)" % len(findings))
    else:
        _verdict(EXIT_OK, "%d 条路由里 %d 条声明了 response_model,未发现未声明键"
                 % (total_routes, len(routes)))

    # 🔴 [c1'] 这一句**两条路径都要打**(工单 §1.3「每次运行打印」)。
    #    c1 第一版只在通过时打,而 stamp 那条判据容忍 rc=1 却断言这句 ——
    #    真出现违规那天,判据会自己红,红的原因还跟违规无关。
    print("真跑了 %d 条路由(其中 %d 条声明了 response_model)"
          % (total_routes, len(routes)))
    print("=" * 72)
    print("G1 · 响应契约静态门禁")
    print("  路由总数(反向对照)          = %d" % total_routes)
    print("  声明了 response_model 的路由 = %d   ← 覆盖数" % len(routes))
    print("  解析出字段树的模型            = %d" % len(parsed))
    print("  跟进的最大嵌套层数            = %d" % max_depth)
    print("  其中 response_model 本身 forbid = %d" % len(forbid_routes))
    print("  response_model 树里 forbid 嵌套出现 = %d" % nested_forbid)
    if last_ok:
        print("  上次成功运行                  = %s%s"
              % (last_ok, ("(%.1f 小时前)" % dark_h) if dark_h is not None else ""))
    else:
        print("  上次成功运行                  = 无记录(首次运行或状态目录换了地方)")
    print("=" * 72)

    if findings:
        print("")
        for item in findings:
            print(item)
    else:
        print("")
        print("✅ 未发现未声明键")

    print("")
    print("⚠️ 静态分析盲区:%d 条路由含 `**展开` / 计算键 / 跟不到的返回值," % untrusted)
    print("   这些路由「没查出违规」**不等于**没有违规 —— 由 G2 运行时锁兜底"
          "(tests/response_model_contract/)。")

    # 🔴 跑起来了就落时间戳 —— 落的是「门禁真的执行过」,不是「仓库没问题」。
    #    有违规照样落:有违规说明它在工作,不是 dark。
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    where = _write_last_ok(stamp)
    if where:
        print("")
        print("🕒 本次运行时间戳已落盘:%s" % where)
    if dark_h is not None and dark_h > args.max_dark_hours:
        print("")
        print("🔴 上一次成功运行在 %.1f 小时前,超过 %.0fh ——" % (dark_h, args.max_dark_hours))
        print("   这段时间里这道闸是**瞎的**,期间放行的批次没有被它看过。")

    if args.report:
        return EXIT_OK
    return EXIT_VIOLATION if findings else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
