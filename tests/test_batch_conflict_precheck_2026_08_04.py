"""[WO-BATCH-CONFLICT-2026-08-04] 批量投放冲突预检 —— 后端四条锁。

工单要求预检「一次给全部冲突」而不是撞到第一个就抛,且每条带 article_id/media_id
(前端要靠这一对精确剔除组合)。断言尽量打在**真跑出来的结果**上;必须打源码时
先剥 docstring 与 # 注释再匹配 —— 否则"注释里写了"就能让断言通过。
"""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _strip_py_noise(text: str) -> str:
    """剥 docstring(ast 精确定位)+ # 注释。

    🔴 不能用 `\"\"\"[\\s\\S]*?\"\"\"` 一把梭:仓里的 SQL 全写在三引号里,
    一把梭会把要断言的东西连同注释一起剥掉,断言变恒假 —— 恒假和恒真一样废。
    """
    lines = text.splitlines()
    drop: set[int] = set()
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            for ln in range(first.lineno, (first.end_lineno or first.lineno) + 1):
                drop.add(ln)
    kept = [("" if i + 1 in drop else ln) for i, ln in enumerate(lines)]
    return re.sub(r"#.*", "", "\n".join(kept))


@pytest.fixture(scope="module")
def api_src() -> str:
    return _strip_py_noise((REPO / "api" / "meijiehezi_api.py").read_text(encoding="utf-8"))


# ============================================================
# 锁1 · 冲突条目结构(article_id + media_id 必须在)
# ============================================================

def test_conflict_entry_carries_ids():
    from api.meijiehezi_api import build_conflict_entry
    from db.meijiehezi_db import BLOCK_REASON_ALREADY_PUBLISHED

    e = build_conflict_entry(
        article_id=1393, article_title="惠州乘客电梯安全吗？定期检验不合格项与风险判断依据",
        media_id=125918, media_name="博客园",
        reason_code=BLOCK_REASON_ALREADY_PUBLISHED,
    )
    # 必须命中:前端靠这一对回到具体组合
    assert e["article_id"] == 1393
    assert e["media_id"] == 125918
    assert e["reason_code"] == BLOCK_REASON_ALREADY_PUBLISHED
    assert e["media_name"] == "博客园"
    # label 仍在(拼人话用),但**不是**标识符
    assert "博客园" in e["label"]
    # 必须不命中:标题不许硬截成半句(zombie R4 已修,别回退)
    assert e["label"] != "东莞乘客电梯安全吗？定期检验不合"


def test_media_name_lookup_cells():
    from api.meijiehezi_api import _media_name_of

    # 必须命中:按下标取到对应名字
    assert _media_name_of(2, [1, 2, 3], ["甲", "乙", "丙"]) == "乙"
    # 必须不命中:取不到就退回 id 字符串,**不编名字**
    assert _media_name_of(9, [1, 2, 3], ["甲", "乙", "丙"]) == "9"
    assert _media_name_of(3, [1, 2, 3], ["甲"]) == "3"


# ============================================================
# 锁2 · 一次给全部冲突(不是撞到第一个就抛)
# ============================================================

def test_batch_precheck_collects_all_before_raising(api_src: str):
    """锁的是控制流形状:循环体内不许 raise,raise 必须在循环之后。"""
    tree = ast.parse((REPO / "api" / "meijiehezi_api.py").read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_publish_batch":
            target = node
    assert target is not None, "api_publish_batch 没找到(函数改名了?)"

    # 收集冲突的 for 循环。外层 `for item in req.items` 与内层 `for d in dups`
    # 都会被 ast.walk 命中,取**最外层**那个(它才代表"整批走完没走完")。
    collect_loops = [
        n for n in ast.walk(target)
        if isinstance(n, ast.For)
        and any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "build_conflict_entry"
                for c in ast.walk(n))
    ]
    assert collect_loops, "没找到收集冲突的循环(判据无效)"
    inner_ids = {id(x) for n in collect_loops for x in ast.walk(n) if x is not n}
    outermost = [n for n in collect_loops if id(n) not in inner_ids]
    assert len(outermost) == 1, f"最外层收集循环应恰好一个,实际 {len(outermost)}"
    loop = outermost[0]

    # 🔴 必须不命中:循环体里不许有 raise —— 有就是"撞到第一个就抛"
    raises_in_loop = [n for n in ast.walk(loop) if isinstance(n, ast.Raise)]
    assert raises_in_loop == [], "预检循环体内不许 raise,否则 N 个冲突要来回 N 趟"

    # 必须命中:循环之后确实有一个抛 409 的分支(证明上一条不是因为压根没有 raise 而恒真)
    after = [n for n in target.body if isinstance(n, ast.If) and n.lineno > loop.lineno]
    assert any(isinstance(x, ast.Raise) for n in after for x in ast.walk(n)), \
        "循环之后必须有抛 409 的分支"


@pytest.mark.parametrize("fn_name", ["api_publish", "api_publish_batch"])
def test_precheck_runs_before_charging(fn_name: str):
    """预检零扣费:去重检查必须排在扣费调用之前。

    🔴 必须**按函数体内的行号**比,不能拿整文件 `.index()` 比 ——
    整文件里 `_recompute_publish_charge` 的定义远在两个端点之前,
    那样比出来的先后毫无判别力(我第一版就是这么写废的)。
    """
    tree = ast.parse((REPO / "api" / "meijiehezi_api.py").read_text(encoding="utf-8"))
    target = next((n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == fn_name), None)
    assert target is not None, f"{fn_name} 没找到"

    def first_line(names) -> int | None:
        hits = [n.lineno for n in ast.walk(target)
                if isinstance(n, ast.Call)
                and (getattr(n.func, "id", "") in names or getattr(n.func, "attr", "") in names)]
        return min(hits) if hits else None

    i_check = first_line({"find_active_orders_for_media"})
    i_charge = first_line({"deduct_points", "_recompute_publish_charge"})
    assert i_check is not None, f"{fn_name} 里没有去重预检,判据无效"
    assert i_charge is not None, f"{fn_name} 里没找到扣费调用,判据无效"
    assert i_check < i_charge, (
        f"{fn_name}: 去重预检(行 {i_check})必须在扣费(行 {i_charge})之前,"
        f"否则拦下来钱已经扣了")


# ============================================================
# 锁3 · 响应结构 + 旧键兼容
# ============================================================

def _func_source(fn_name: str) -> str:
    """按 AST 切出**某个函数体**的源码(已剥 docstring/注释)。

    🔴 断言必须切到函数内再打:`duplicate_media_ids` 在本文件里另有一处(行 2020,
    别的端点),整文件 `in` 判定会让"把单发那处删掉"照样绿 —— 变异 B5 当场证伪了
    我的第一版锁。断言的词在别处也有 = 弱锁。
    """
    raw = (REPO / "api" / "meijiehezi_api.py").read_text(encoding="utf-8")
    tree = ast.parse(raw)
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == fn_name), None)
    assert node is not None, f"{fn_name} 没找到"
    lines = raw.splitlines()[node.lineno - 1: (node.end_lineno or node.lineno)]
    return _strip_py_noise("\n".join(lines))


def test_response_shape_cells(api_src: str):
    single = _func_source("api_publish")
    batch = _func_source("api_publish_batch")

    # 必须命中:两条链路都给结构化 conflicts
    assert '"conflicts": ' in single, "单发链路要给结构化 conflicts"
    assert '"conflicts": ' in batch, "批量链路要给结构化 conflicts"
    # 必须命中:单发保留旧键(老前端只认它)—— 断在**单发函数体内**
    assert '"duplicate_media_ids"' in single, "单条提交的旧键不许因为改结构化就断掉"
    # 必须不命中:短命的中间键不许残留(会变成第二套词汇)
    assert '"duplicates": ' not in single
    assert '"duplicates": ' not in batch, "duplicates 已被 conflicts 取代,不许两套并存"
    # 反向对照:判据切对了函数 —— 单发体内不该出现批量独有的符号
    assert "all_dups" not in single, "切函数体的判据串味了(切到批量去了)"


# ============================================================
# 锁4 · 不许动去重闸判定本身(工单封条)
# ============================================================

def test_dedupe_gate_untouched():
    """工单红线:`check_duplicate_submission` 的 3h 阈值逻辑一个字不碰。"""
    db_src = _strip_py_noise((REPO / "db" / "meijiehezi_db.py").read_text(encoding="utf-8"))
    head = db_src.index("def check_duplicate_submission(")
    body = db_src[head:db_src.index("def ", head + 10)]
    # 必须命中:僵尸包那套判据原样还在
    assert "ORPHAN_AGE_HOURS_FOR_DEDUPE" in body
    assert "i.status = 'pending'" in body
    assert "mhz_order_id IS NULL OR i.mhz_order_id = ''" in body
    # 必须命中:阈值常量没被改动
    from db.meijiehezi_db import ORPHAN_AGE_HOURS_FOR_DEDUPE
    assert ORPHAN_AGE_HOURS_FOR_DEDUPE == 3


def test_block_reason_codes_unchanged():
    """理由码沿用僵尸包已定的拼写 —— 前端做大小写规范化,不让后端迁就前端。"""
    from db.meijiehezi_db import (
        BLOCK_REASON_ALREADY_PUBLISHED, BLOCK_REASON_IN_FLIGHT,
    )
    assert BLOCK_REASON_ALREADY_PUBLISHED == "ALREADY_PUBLISHED"
    assert BLOCK_REASON_IN_FLIGHT == "IN_FLIGHT"


# ============================================================
# 接线 · 新函数必须有真实调用点(测试不算)
# ============================================================

def test_new_helpers_are_wired(api_src: str):
    for fn in ("build_conflict_entry", "_media_name_of"):
        # 定义 1 处 + 调用 ≥2 处(单发链 + 批量链)
        assert api_src.count(fn) >= 3, f"{fn} 缺真实调用点(单发/批量两条链都要用)"


def test_frontend_contract_is_wired():
    """前端契约必须真被页面用上,不是建了个死文件。"""
    page = (REPO / "frontend" / "src" / "pages" / "Publishing" / "PublishCenter.tsx").read_text(encoding="utf-8")
    for sym in ("parseDuplicateOrderBlock", "applyConflictRemoval", "remainingAfterRemoval"):
        assert sym in page, f"{sym} 没接进发布中心"
    dialog = (REPO / "frontend" / "src" / "components" / "publishing" / "PublishRiskConfirmDialog.tsx").read_text(encoding="utf-8")
    assert "DuplicateConflictPanel" in dialog, "冲突面板没接进确认弹窗"
    assert "isSubmitBlockedByConflicts" in dialog, "提交闸没走契约里那个唯一判定"
    # 必须命中:锁套件已串进真 build 的门
    pkg = (REPO / "frontend" / "package.json").read_text(encoding="utf-8")
    build_line = [l for l in pkg.splitlines() if '"build"' in l][0]
    assert "test-batch-conflict-window.mjs" in build_line, "锁没串进 npm run build = 不会被跑"
