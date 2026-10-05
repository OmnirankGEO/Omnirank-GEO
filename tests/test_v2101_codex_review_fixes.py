"""v2.10.1 Codex 复审 4 P0/P1 修复 · 端到端测试套

覆盖 Codex 找到的 4 大 BUG · 不只文本扫 · 含状态机/SQL/调用链路守护:

P0-1 · sync_rewrite_titles 不能清空 batch_distribution 写入的 user_choice
P0-2 · 单篇 dropdown 写 source='manual' · light reset 同时清 source
P0-3 · 系统推荐 mode 不调 apply-distribution · 调 reset-to-recommended
P1-4 · style_plan 含 slot_index · 保存按 topic_id 严格匹配
兼容性 · ArticleGenerateRequest.article_distribution 改 Optional
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
from tests._shared.source_slice import function_body


def _read_text(rel_path: str) -> str:
    full_path = os.path.join(_ROOT, rel_path)
    with open(full_path, "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# P0-1 · regenerate-titles 按 topic_id 写回 user_choice + source
# ============================================================

def test_v2101_regenerate_titles_uses_plan_by_topic_id():
    """P0-1 · regenerate-titles 必须按 topic_id 从 style_plan 查 user_choice · 不用全局 _persist_uc"""
    fn_block = function_body("server.py", "api_regenerate_titles")

    # 必须建 plan_by_topic_id 映射
    assert "plan_by_topic_id" in fn_block, \
        "v2.10.1 P0-1 破:未建 plan_by_topic_id 映射 · 仍按 LLM 返回顺序"
    assert "plan_by_topic_id[tid]" in fn_block, \
        "v2.10.1 P0-1 破:未按 topic_id 查 plan"
    # UPDATE 必须显式写 user_choice_source
    assert "user_choice_source = %s" in fn_block, \
        "v2.10.1 P0-1 破:regenerate-titles UPDATE 未写 user_choice_source · sync 路径会清空 batch_distribution"


def test_v2101_regenerate_titles_no_global_persist_uc_clear():
    """P0-1 · 没传 user_choice 且没 style_plan 时 · 不能清 user_choice/source(保留旧值)"""
    fn_block = function_body("server.py", "api_regenerate_titles")

    # 必须有"不动 user_choice/source"分支(旧批量重写路径)
    # 即不传 user_choice + 不传 style_plan 时 · UPDATE 只改 optimized_title + status · 不改 user_choice
    assert "不动 user_choice" in fn_block or "保留原值" in fn_block or "保留旧值" in fn_block, \
        "v2.10.1 P0-1 破:regenerate-titles 缺'旧批量重写不动 user_choice'分支"


# ============================================================
# P0-2 · 单篇 dropdown 写 source='manual' · light reset 同清 source
# ============================================================

def test_v2101_single_dropdown_writes_manual_source():
    """P0-2 · 单篇 dropdown 非 auto → regenerate-titles 必须写 source='manual'"""
    fn_block = function_body("server.py", "api_regenerate_titles")

    # 必须有 manual source 标记(单篇 dropdown 路径)
    assert '"manual"' in fn_block or "'manual'" in fn_block, \
        "v2.10.1 P0-2 破:regenerate-titles 未写 source='manual' · 单篇手动锁定不生效"
    # 必须有"单篇 dropdown = manual"语义
    assert "_default_source" in fn_block, \
        "v2.10.1 P0-2 破:缺单篇 dropdown 默认 source 变量"


def test_v2101_light_reset_clears_source():
    """P0-2 · light reset 必须同时清 user_choice + user_choice_source(防 manual 残留永久锁)"""
    src = _read_text("server.py")
    # light reset path 必须 SET user_choice = NULL, user_choice_source = NULL
    light_idx = src.find("if _is_light_reset:")
    light_block = src[light_idx:light_idx + 1500]

    assert "user_choice = NULL" in light_block, \
        "v2.10.1 P0-2 破:light reset 未清 user_choice"
    assert "user_choice_source = NULL" in light_block, \
        "v2.10.1 P0-2 破:light reset 未清 user_choice_source · manual 残留会永久锁住"


# ============================================================
# P0-3 · 系统推荐 mode 调专门 endpoint · 不写过滤后 distribution
# ============================================================

def test_v2101_reset_to_recommended_endpoint_exists():
    """P0-3 · POST /api/writing/projects/{quote_id}/reset-to-recommended endpoint 存在"""
    src = _read_text("server.py")
    assert '@app.post("/api/writing/projects/{quote_id}/reset-to-recommended")' in src, \
        "v2.10.1 P0-3 破:reset-to-recommended endpoint 未注册"


def test_v2101_reset_to_recommended_preserves_manual():
    """P0-3 · reset-to-recommended SQL 必须 IS DISTINCT FROM 'manual'(保留单篇手动锁定)"""
    src = _read_text("server.py")
    fn_start = src.find("async def api_reset_to_recommended")
    assert fn_start > 0, "找不到 api_reset_to_recommended"
    fn_block = src[fn_start:fn_start + 3000]

    assert "IS DISTINCT FROM 'manual'" in fn_block, \
        "v2.10.1 P0-3 破:reset-to-recommended 未保留 manual(IS DISTINCT FROM)"
    # 必须排除 locked status(防竞态)
    assert "completed" in fn_block and "writing" in fn_block and "regenerating" in fn_block, \
        "v2.10.1 P0-3 破:reset-to-recommended 未排除 LOCKED_STATUSES"
    # owner check
    assert "owner_user_id" in fn_block, \
        "v2.10.1 P0-3 破:reset-to-recommended 缺 owner check"


def test_v2101_dialog_recommended_mode_calls_reset_endpoint():
    """P0-3 · DistributionConfigDialog mode='recommended' 必须调 reset-to-recommended · 不调 apply-distribution"""
    src = _read_text("frontend/src/components/writing/DistributionConfigDialog.tsx")
    fn_idx = src.find("const apply = async")
    apply_block = src[fn_idx:fn_idx + 3000]

    # mode='recommended' 分支必须调 reset-to-recommended
    assert "reset-to-recommended" in apply_block, \
        "v2.10.1 P0-3 破:Dialog mode='recommended' 未调 reset-to-recommended endpoint"
    # mode='recommended' 不能调 apply-distribution
    # 找 recommended 分支内的 fetch · 不能是 apply-distribution
    rec_idx = apply_block.find("mode === 'recommended'")
    assert rec_idx > 0
    # 提取 recommended 分支(到 return; 或下一个 if 结束)
    rec_block_end = apply_block.find("if (mode === 'uniform')", rec_idx)
    rec_block = apply_block[rec_idx:rec_block_end if rec_block_end > 0 else rec_idx + 2000]

    assert "apply-distribution" not in rec_block, \
        "v2.10.1 P0-3 破:recommended 分支误调 apply-distribution(应只调 reset-to-recommended)"


# ============================================================
# P1-4 · style_plan 含 slot_index
# ============================================================

def test_v2101_plan_includes_slot_index():
    """P1-4 · apply-distribution plan 必须含 slot_index"""
    src = _read_text("server.py")
    fn_start = src.find("async def api_apply_distribution")
    fn_block = src[fn_start:fn_start + 8000]

    assert '"slot_index"' in fn_block, \
        "v2.10.1 P1-4 破:apply-distribution plan 未含 slot_index 字段"


def test_v2101_plan_uses_keyword_grouping():
    """P1-4 · plan 必须按 keyword_id 分组 round-robin · 不裸序列扫"""
    src = _read_text("server.py")
    fn_start = src.find("async def api_apply_distribution")
    fn_block = src[fn_start:fn_start + 8000]

    assert "kw_to_topics" in fn_block, \
        "v2.10.1 P1-4 破:apply-distribution 未按 keyword 分组"
    # max_slots round-robin 标记
    assert "max_slots" in fn_block or "slot_index in range" in fn_block, \
        "v2.10.1 P1-4 破:plan 未走 slot_index round-robin"


# ============================================================
# 兼容性 · ArticleGenerateRequest.article_distribution Optional
# ============================================================

def test_v2101_article_generate_request_article_distribution_optional():
    """兼容性 · ArticleGenerateRequest.article_distribution 必须是 Optional · 不传也合法

    根因(Codex 原报):必填 + 传了就 400 = 任意调用都 400(包括内部 caller)
    修法:Optional[dict] = None · 不传不 400 · 传非 None → 400 reject
    """
    src = _read_text("server.py")
    cls_idx = src.find("class ArticleGenerateRequest")
    cls_block = src[cls_idx:cls_idx + 800]

    # article_distribution 必须是 Optional · 不是裸 dict 必填
    assert "article_distribution: Optional[dict]" in cls_block, \
        "v2.10.1 兼容性破:ArticleGenerateRequest.article_distribution 仍是必填 · 内部 caller 会被一刀切 400"
    # 整行检查:`article_distribution: Optional[dict] = None`
    assert re.search(r"article_distribution:\s*Optional\[dict\]\s*=\s*None", cls_block), \
        "v2.10.1 兼容性破:article_distribution 未设 default None"


def test_v2101_article_generate_endpoint_still_rejects_distribution():
    """兼容性 · /api/articles/generate 必须整体 410(v2.10.2 修 P0-2 升级)

    v2.10.1 改 Optional + is not None → 400(留 None 走 validate_distribution(None) 崩溃)
    v2.10.2 升级:整体 410 Gone · 函数顶部 raise · 不再进 validate_distribution
    """
    src = _read_text("server.py")
    fn_start = src.find("async def generate_articles")
    fn_block = src[fn_start:fn_start + 1500]

    # v2.10.2:函数顶部直接 410(不再走 is not None + 400)
    assert "status_code=410" in fn_block, \
        "v2.10.2 兼容性破:/api/articles/generate 未整体 410 废弃(原 v2.10.1 半兼容会 None 崩)"
