"""v2.10.2 Codex 复审 v2.10.1 6 P0/P1 修复 · 端到端测试套

覆盖 Codex 二审 v2.10.1 找到的 6 大 BUG:

P0-1 · 自定义配比接入首次批量生成标题(generate-titles + user_choice_distribution)
P0-2 · /api/articles/generate 整体废弃 410(不再 None 走 validate 崩)
P0-3 · KTG 返回带 slot_index · regenerate 按 (keyword_id, slot_index) 严格匹配
P1-4 · apply-distribution UPDATE RETURNING + 只 regen 实际成功 ids
P1-5 · Dialog recommended mode 单按钮 · 去"同步重写"误导
P1-6 · save_topics_batch + 各路径写 user_choice_source
"""
from __future__ import annotations

import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

# 🔴 [WO_205 2026-09-15] 本文件的源码切片改用**函数真实边界**,不再写魔法字节数。
#    旧写法 `src[fn_start : fn_start + N]` 里的 N 是**当年**那个函数的长度;
#    函数长大了 N 没跟着长 ⇒ 断言的串明明就在函数体内却掉在窗口外,判据假红,
#    而产品行为一个字节没变。分诊逐条量过偏移/窗口/函数真长,见
#    `tests/_shared/source_slice.py` 抬头那张表(最险的一条差 13 个字符)。
#    🔴 这**不是放松**:窗口从「猜的字节数」换成「函数真实边界」,断言一个字没改;
#       放大到整份文件才是放松(别处的同名行也能满足)。
from tests._shared.source_slice import code_only, function_body


def _read_text(rel_path: str) -> str:
    full_path = os.path.join(_ROOT, rel_path)
    with open(full_path, "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# P0-1 · generate-titles 接 user_choice_distribution
# ============================================================

def test_v2102_title_generate_request_has_user_choice_distribution():
    """P0-1 · TitleGenerateRequest 必须含 user_choice_distribution: Optional[dict]"""
    src = _read_text("server.py")
    cls_idx = src.find("class TitleGenerateRequest")
    cls_block = src[cls_idx:cls_idx + 800]

    assert "user_choice_distribution" in cls_block, \
        "v2.10.2 P0-1 破:TitleGenerateRequest 未加 user_choice_distribution 字段"
    assert re.search(r"user_choice_distribution:\s*Optional\[dict\]", cls_block), \
        "v2.10.2 P0-1 破:user_choice_distribution 必须 Optional[dict]"


def test_v2102_generate_titles_validates_distribution():
    """P0-1 · generate-titles 必须 validate_user_choice_distribution(reject style_code/sum/ranking)"""
    fn_block = _generate_titles_body()

    assert "validate_user_choice_distribution" in fn_block, \
        "v2.10.2 P0-1 破:generate-titles 未校验 user_choice_distribution"
    assert "_style_plan_for_gen" in fn_block, \
        "v2.10.2 P0-1 破:generate-titles 未算 style_plan_for_gen(分配 per-topic)"


def _generate_titles_body() -> str:
    """精确取 api_generate_titles 的函数体,不用魔法字符窗口。

    [2026-07-28] 原先是 `src[fn_start:fn_start+12000/14000]` —— 端点里加几十行
    (本次 T5「先落库再生成」)就会把 `batch_uniform` 挤出窗口,断言假红,
    而产品行为一个字节没变。改成 AST 取真实函数体:窗口再也不会漂。
    """
    import ast

    src = _read_text("server.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))                 and node.name == "api_generate_titles":
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("server.py 里找不到 api_generate_titles")


def test_v2102_generate_titles_writes_batch_distribution_source():
    """P0-1 · generate-titles distribution 路径必须写 source='batch_distribution'"""
    fn_block = _generate_titles_body()

    assert "batch_distribution" in fn_block, \
        "v2.10.2 P0-1 破:generate-titles distribution 路径未写 source='batch_distribution'"
    assert "batch_uniform" in fn_block, \
        "v2.10.2 P0-1 破:generate-titles user_style 路径未写 source='batch_uniform'"


def test_v2102_frontend_passes_user_choice_distribution_to_generate_titles():
    """P0-1 · 前端 generateTitles 必须传 user_choice_distribution(pendingDistribution)"""
    src = _read_text("frontend/src/pages/Writing/WritingHall.tsx")

    assert "pendingDistribution" in src, \
        "v2.10.2 P0-1 破:WritingHall 未加 pendingDistribution state"
    assert "user_choice_distribution: pendingDistribution" in src, \
        "v2.10.2 P0-1 破:generateTitles 未传 user_choice_distribution 给后端"


def test_v2102_dialog_saves_pending_for_no_existing_titles():
    """P0-1 · 未生成 topics 时 Dialog 必须调 onSavePendingDistribution · 不调 apply-distribution"""
    src = _read_text("frontend/src/components/writing/DistributionConfigDialog.tsx")

    assert "onSavePendingDistribution" in src, \
        "v2.10.2 P0-1 破:Dialog 缺 onSavePendingDistribution prop"
    assert "hasExistingTitles" in src, \
        "v2.10.2 P0-1 破:Dialog 未根据 hasExistingTitles 分支处理"


# ============================================================
# P0-2 · /api/articles/generate 整体废弃 410
# ============================================================

def test_v2102_articles_generate_returns_410():
    """P0-2 · /api/articles/generate 整体 410 Gone · 不再走 validate_distribution 调用(防 None 崩)"""
    src = _read_text("server.py")
    fn_start = src.find("async def generate_articles")
    fn_block = src[fn_start:fn_start + 2000]

    assert "status_code=410" in fn_block, \
        "v2.10.2 P0-2 破:/api/articles/generate 未整体 410 废弃"

    # 410 raise 必须在 is_valid, error = validate_distribution(...) 调用之前
    # docstring 文本中的 validate_distribution 不算 · 用真实调用形态匹配
    raise_idx = fn_block.find("raise HTTPException(")
    # 真实调用形态:`is_valid, error = validate_distribution(`
    validate_call_idx = fn_block.find("is_valid, error = validate_distribution(")
    if validate_call_idx > 0:
        assert 0 <= raise_idx < validate_call_idx, \
            f"v2.10.2 P0-2 破:410 raise(idx={raise_idx}) 未在 validate_distribution 调用(idx={validate_call_idx}) 之前 · 仍会 None 崩"


# ============================================================
# P0-3 · KTG 返回带 slot_index · 严格匹配
# ============================================================

def test_v2102_ktg_parse_response_adds_slot_index():
    """P0-3 · KTG._parse_response 必须为每个 topic 加 slot_index(LLM 漏返回时按出现顺序重建)"""
    src = _read_text("writing/keyword_topic_generator.py")
    fn_start = src.find("def _parse_response")
    fn_block = src[fn_start:fn_start + 3000]

    assert '"slot_index"' in fn_block or "topic[\"slot_index\"]" in fn_block, \
        "v2.10.2 P0-3 破:KTG._parse_response 未为 topic 加 slot_index"
    assert "_kw_seen_count" in fn_block or "slot_index" in fn_block.lower(), \
        "v2.10.2 P0-3 破:KTG._parse_response 未按 keyword 计数重建 slot_index"


def test_v2102_regenerate_titles_strict_match_by_kw_slot():
    """P0-3 · regenerate-titles 保存按 (keyword_id, slot_index) 严格匹配 · 不靠 LLM 数组顺序"""
    fn_block = function_body("server.py", "api_regenerate_titles")

    # 必须建 new_topics_by_key 反向 lookup
    assert "new_topics_by_key" in fn_block, \
        "v2.10.2 P0-3 破:regenerate-titles 未建 new_topics_by_key 反向 lookup · 仍按 LLM 数组顺序"
    # lookup key 是 (keyword_id, slot_index)
    assert 'nt.get("keyword_id"), nt.get("slot_index")' in fn_block, \
        "v2.10.2 P0-3 破:lookup key 不是 (keyword_id, slot_index)"


# ============================================================
# P1-4 · apply-distribution UPDATE RETURNING + 只 regen 成功 ids
# ============================================================

def test_v2102_apply_distribution_uses_returning():
    """P1-4 · apply-distribution UPDATE 必须 RETURNING id(拿实际成功 ids 防并发绕过)"""
    src = _read_text("server.py")
    fn_start = src.find("async def api_apply_distribution")
    fn_block = src[fn_start:fn_start + 10000]

    assert "RETURNING id" in fn_block, \
        "v2.10.2 P1-4 破:apply-distribution UPDATE 未 RETURNING id"
    assert "actually_updated_ids" in fn_block, \
        "v2.10.2 P1-4 破:未维护 actually_updated_ids 集合"


def test_v2102_apply_distribution_regen_only_actual_updated():
    """P1-4 · sync_rewrite 只把实际 UPDATE 成功的 ids 传给 regenerate(并发保护)"""
    src = _read_text("server.py")
    fn_start = src.find("async def api_apply_distribution")
    fn_block = src[fn_start:fn_start + 10000]

    assert "actually_updated_plan" in fn_block, \
        "v2.10.2 P1-4 破:regen 用了原 plan · 应只用 actually_updated_plan(实际 UPDATE 成功)"
    # regen 时 topic_ids 来自 actually_updated_plan
    assert re.search(r"topic_ids\s*=\s*\[p\[\"topic_id\"\]\s+for\s+p\s+in\s+actually_updated_plan\]", fn_block), \
        "v2.10.2 P1-4 破:regen topic_ids 未从 actually_updated_plan 派生"


# ============================================================
# P1-5 · Dialog recommended mode 单按钮
# ============================================================

def test_v2102_dialog_recommended_single_button():
    """P1-5 · recommended mode 只一个按钮 · 去"同步重写"误导"""
    src = _read_text("frontend/src/components/writing/DistributionConfigDialog.tsx")

    # 必须有按 mode 分支渲染按钮
    assert "mode === 'recommended'" in src or 'mode === "recommended"' in src, \
        "v2.10.2 P1-5 破:Dialog footer 未按 mode='recommended' 分支"
    # recommended mode 按钮文案
    assert "恢复为系统推荐" in src, \
        "v2.10.2 P1-5 破:recommended mode 缺'恢复为系统推荐'按钮"


def test_v2102_dialog_recommended_no_filtered_preview():
    """P1-5 · recommended mode 不能展示过滤后系统推荐分布(误导用户)"""
    src = _read_text("frontend/src/components/writing/DistributionConfigDialog.tsx")

    # 不能含"系统推荐分布"+ 每项篇数预览
    assert "系统推荐分布" not in src, \
        "v2.10.2 P1-5 破:recommended mode 仍展示过滤后分布(误导)"


# ============================================================
# P1-6 · save_topics_batch + 各路径写 user_choice_source
# ============================================================

def test_v2102_save_topics_batch_writes_source():
    """P1-6 · save_topics_batch INSERT 必须写 user_choice_source"""
    fn_block = function_body("db/diagnosis_db.py", "save_topics_batch")
    # 🔴 [WO_205 注毒暴露] 在**剥掉注释**的代码上匹配。
    #    实测:把列名从 INSERT 的字段列表里删掉,这两条断言**照样绿** ——
    #    参数行旁边那句 `# v2.10.2 user_choice_source(...)` 注释就满足了它们
    #    (正则还是 DOTALL,`[^;]*` 跨行一路吃到那句注释)。
    #    注释改不改都不影响行为,**被注释满足的断言等于没断言**。
    code = code_only(fn_block)

    assert "user_choice_source" in code, \
        "v2.10.2 P1-6 破:save_topics_batch INSERT 未写 user_choice_source"
    # INSERT 字段必含 user_choice_source
    assert re.search(r"INSERT INTO topics[^;]*user_choice_source", code, re.DOTALL), \
        "v2.10.2 P1-6 破:save_topics_batch INSERT 字段列表缺 user_choice_source"


def test_v2102_save_topics_batch_consistency_uc_null_source_null():
    """P1-6 · save_topics_batch:_uc=NULL 时 _source 强制 NULL(状态机一致性)"""
    fn_block = function_body("db/diagnosis_db.py", "save_topics_batch")

    # _uc is None 时 _source = None
    assert "if _uc is None" in fn_block, \
        "v2.10.2 P1-6 破:save_topics_batch 未检查 _uc is None 时强制 source=None"


# ============================================================
# 综合:跨链路状态机守护
# ============================================================

def test_v2102_state_machine_all_paths_write_source():
    """v2.10.2 · 全链路 user_choice 写入路径都必须写 source(防漏)"""
    # generate-titles
    src = _read_text("server.py")
    fn1 = src[src.find("async def api_generate_titles"):src.find("async def api_generate_titles") + 14000]
    assert "user_choice_source" in fn1, \
        "v2.10.2 状态机:generate-titles 路径未写 user_choice_source"

    # regenerate-titles
    fn2 = src[src.find("async def api_regenerate_titles"):src.find("async def api_regenerate_titles") + 14000]
    assert "user_choice_source" in fn2, \
        "v2.10.2 状态机:regenerate-titles 路径未写 user_choice_source"

    # apply-distribution
    fn3 = src[src.find("async def api_apply_distribution"):src.find("async def api_apply_distribution") + 10000]
    assert "user_choice_source" in fn3, \
        "v2.10.2 状态机:apply-distribution 路径未写 user_choice_source"

    # save_topics_batch
    src2 = _read_text("db/diagnosis_db.py")
    fn4 = src2[src2.find("def save_topics_batch"):src2.find("def save_topics_batch") + 3000]
    assert "user_choice_source" in fn4, \
        "v2.10.2 状态机:save_topics_batch 路径未写 user_choice_source"
