"""v2.10.4 Codex 四审 v2.10.3 · 3 阻断收口

P0-1 · fixed slot SSOT 拍板 B(承认现状失效):fixed_count=len + SSOT 测试改 deprecated
P0-2 · regenerate-titles 用 _t.slot_index 查 plan(不按出现顺序重编号)
P1 · 顶部 dropdown 变 · 清 pendingDistribution + UI 互斥提示
"""
from __future__ import annotations
import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

# 🔴 [WO_205 2026-09-15] 源码切片改用**函数真实边界**,不再写魔法字节数。
#    旧写法 `src[fn_start : fn_start + N]` 的 N 是**当年**那个函数的长度;函数长大了
#    N 没跟着长 ⇒ 断言的串就在函数体内却掉在窗口外,判据假红,产品行为一个字节没变。
#    逐条量过的偏移/窗口/函数真长见 `tests/_shared/source_slice.py` 抬头(最险差 13 字符)。
#    🔴 **不是放松**:断言一个字没改;放大到整份文件才是放松(别处同名行也能满足)。
from tests._shared.source_slice import function_body


def _read_text(rel_path: str) -> str:
    with open(os.path.join(_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# P0-1 · fixed slot SSOT 拍 B
# ============================================================

def test_v2104_fixed_count_uses_len_not_max():
    """P0-1 · compute_slot_buckets fixed_count = len(fixed_topics)(承认现状 · 不预扣)"""
    src = _read_text("writing/direction_distribution.py")
    fn_start = src.find("def compute_slot_buckets")
    fn_block = src[fn_start:fn_start + 3500]

    # 必须用 len(fixed_topics) · 不能 max(1, ...)
    assert "fixed_count = len(fixed_topics)" in fn_block, \
        "v2.10.4 P0-1 破:fixed_count 应 len(fixed_topics) · 不 max(1, ...)"
    assert "max(1, len(fixed_topics))" not in fn_block, \
        "v2.10.4 P0-1 破:fixed_count 仍 max(1, ...) · 拍 B 时应改 len"


def test_v2104_style_registry_deprecated_note():
    """P0-1 · get_fixed_count_styles 必须标 deprecated 说明(SSOT 实际失效)"""
    src = _read_text("writing/style_registry.py")
    fn_start = src.find("def get_fixed_count_styles")
    fn_block = src[fn_start:fn_start + 1500]

    assert "deprecated" in fn_block.lower() or "实际失效" in fn_block, \
        "v2.10.4 P0-1 破:get_fixed_count_styles 未标 deprecated · 误导后续开发者"
    # 必须解释历史 v2.4 P0 #1 SSOT 与现状的关系
    assert "v2.4" in fn_block or "calculate_distribution" in fn_block, \
        "v2.10.4 P0-1 破:deprecated 未说明 SSOT 历史 vs 现状"


def test_v2104_fixed_count_first_time_zero():
    """P0-1 真实数据流 · 首次生成 existing=[] · fixed_count=0(不预扣 · 14 篇全可配)"""
    from writing.direction_distribution import compute_slot_buckets
    result = compute_slot_buckets(1, [{"required_articles": 14}], [])
    assert result["fixed_count"] == 0
    assert result["configurable_count"] == 14


def test_v2104_fixed_count_has_existing_fixed():
    """P0-1 · existing 含 fixed company_profile topic · fixed_count=1(跟实际数一致)"""
    from writing.direction_distribution import compute_slot_buckets
    existing = [
        {"id": 1, "is_fixed": True, "style_code": "company_profile", "status": "pending"},
        {"id": 2, "style_code": "price_roi", "status": "pending"},
    ]
    result = compute_slot_buckets(1, [{"required_articles": 14}], existing)
    assert result["fixed_count"] == 1
    # configurable = total - fixed(1) - active(0) - manual(0) = 14 - 1 = 13
    assert result["configurable_count"] == 13


# ============================================================
# P0-2 · regenerate-titles 用 _t.slot_index 查 plan
# ============================================================

def test_v2104_regenerate_uses_t_slot_index_first():
    """P0-2 · regenerate-titles 优先用 _t.slot_index · fallback 出现顺序"""
    fn_block = function_body("server.py", "api_generate_titles")

    # 必须取 _t.get("slot_index")
    assert '_t_slot = _t.get("slot_index")' in fn_block, \
        "v2.10.4 P0-2 破:generate-titles 未优先从 _t.slot_index 取 slot"
    # fallback 逻辑:slot is None 时才用 _kw_topic_idx
    assert "if _t_slot is None:" in fn_block, \
        "v2.10.4 P0-2 破:缺 slot_index None 时 fallback 逻辑"


def test_v2104_slot_index_lookup_not_reindexed():
    """P0-2 · plan_lookup key 用 (kw_id, _t_slot)· 不是按 _kw_topic_idx 重新编号"""
    fn_block = function_body("server.py", "api_generate_titles")

    # _plan_lookup 用 _t_slot 查
    assert "_plan_lookup.get((_kw_id, _t_slot)" in fn_block, \
        "v2.10.4 P0-2 破:_plan_lookup 未按 _t_slot 查 · 仍用 _kw_topic_idx 重编号"


def test_v2104_ktg_parser_rebuilds_slot_index_when_missing():
    """P0-2 · KTG._parse_response LLM 漏 slot_index 时按出现顺序重建(已在 v2.10.2 实现)"""
    fn_block = function_body("writing/keyword_topic_generator.py", "_parse_response")

    assert "_kw_seen_count" in fn_block, \
        "v2.10.4 P0-2 破:KTG._parse_response 缺 slot_index 重建逻辑"
    assert 'if "slot_index" not in topic:' in fn_block, \
        "v2.10.4 P0-2 破:KTG 未在 topic 漏 slot_index 时重建"


# ============================================================
# P1 · 顶部 dropdown 清 pendingDistribution + UI 互斥
# ============================================================

def test_v2104_top_dropdown_clears_pending_distribution():
    """P1 · 顶部统一方向控件**若存在**,切换时必须清 pendingDistribution。

    🔴 [WO_205 2026-09-15 改判据 · 附证据] 这条原先直接断言
    `src.find("setBatchUserStyle(newVal)") > 0` —— 钉的是一个**已被有意撤掉**的控件。

    证据(不是「我觉得」):
      · `setBatchUserStyle` 全前端**只剩 useState 声明那一处**,没有任何调用点;
        我第一版分诊数到「1 处」就当成调用点了 —— 那 1 处是声明。
        名字命中不等于归属,得跟到代码路径。
      · 两个 toast 串「自定义配比已清除」「已切换为统一方向」全文件 **0 处**;
        唯一的 `setPendingDistribution(null)` 是**生成成功后**的清理,不是切换控件时。
      · `git log -G 'setBatchUserStyle\('` 指到 **bdf2c05d6**
        「fix(writing): hide redundant style recommendation controls」——
        控件是**作为冗余被有意隐藏**的,不是回归掉的。
      · 方向选择现在走 `DistributionConfigDialog`(`distributionDialogOpen`)。

    所以照原样去「修」等于把一个被判定冗余的控件加回来 —— 判据在要求一件坏事。
    改成**条件锁**:控件不在 ⇒ 这条不适用(跳过并说明);
    控件哪天回来 ⇒ 那条不变量立刻重新生效,不清 pendingDistribution 就红。
    这样既不要求加回控件,也不把不变量一起删掉。

    🔴 顺带留一条给 Review(不在本单修):`batchUserStyle` 现在**只读不写**、
    恒为 'auto',而它仍被发给后端(`user_style: batchUserStyle === 'auto' ? null : ...`,
    三处)。即这一屏的「统一方向」请求字段已成死路。删它属行为相邻改动,另立单。
    """
    import re as _re

    src = _read_text("frontend/src/pages/Writing/WritingHall.tsx")

    # 控件是否回来了:看有没有**调用** setBatchUserStyle(声明那一行不算)
    # 🔴 [WO_205 注毒暴露] 排除规则必须**精确识别那一行声明**,
    #    不能用「前 120 字符里有 useState」—— 实测:把调用写在声明的**下一行**,
    #    那个窗口里就有 useState,真调用被我自己的排除规则吃掉,毒下成了却仍绿。
    #    判据里每一个 exclude 都是**自陈盲区**,注毒要专往排除域里下。
    _decl = _re.compile(r"const\s*\[\s*\w+\s*,\s*setBatchUserStyle\s*\]\s*=\s*useState")
    calls = [m for m in _re.finditer(r"setBatchUserStyle\s*\(", src)
             if not _decl.search(src, max(0, m.start() - 60), m.end() + 10)]
    if not calls:
        import pytest
        pytest.skip(
            "顶部统一方向控件已于 bdf2c05d6「hide redundant style recommendation controls」撤掉(setBatchUserStyle 零调用点);方向选择走 DistributionConfigDialog。控件回来时本条自动重新生效。")

    # 控件回来了 ⇒ 原来的不变量必须同时回来
    for m in calls:
        window = src[m.start():m.start() + 800]
        assert "setPendingDistribution(null)" in window, (
            "统一方向控件回来了,但切换时没清 pendingDistribution —— "
            "两个控件会各说一套:用户设过的自定义配比仍会被发给后端,而界面显示的是统一方向")
        assert ("自定义配比已清除" in window or "已切换为统一方向" in window), (
            "清了 pendingDistribution 却没有 toast —— 用户看不见自己刚设的配比被清掉了")


def test_v2104_pending_distribution_active_visual_hint():
    """P1 · pendingDistribution 不为 null 时 · UI 显式提示「已启用自定义配比」"""
    src = _read_text("frontend/src/pages/Writing/WritingHall.tsx")

    # 必须有"已启用自定义配比"提示文本
    assert "已启用自定义配比" in src, \
        "v2.10.4 P1 破:pendingDistribution 激活时 UI 未显式提示"
    # 必须条件渲染(pendingDistribution !== null)
    assert "pendingDistribution !== null" in src, \
        "v2.10.4 P1 破:UI 提示未按 pendingDistribution 条件渲染"


# ============================================================
# topics 表 schema 兼容
# ============================================================

def test_v2104_topics_table_has_is_fixed_and_style_code():
    """v2.10.4 · topics 表 _safe_add_column is_fixed + style_code(为 ArticleWriter fixed slot 路径 + 后续 v2.11 准备)"""
    src = _read_text("db/diagnosis_db.py")

    assert '_safe_add_column(cursor, "topics", "is_fixed"' in src, \
        "v2.10.4 破:topics 表未加 is_fixed 字段(向后 v2.11 fixed slot 创建用)"
    assert '_safe_add_column(cursor, "topics", "style_code"' in src, \
        "v2.10.4 破:topics 表未加 style_code 字段(对齐 articles.style_code SSOT)"


def test_v2104_migration_sql_exists():
    """v2.10.4 · 主线 SQL migration_v2104_topics_fixed_slot.sql 必须存在"""
    path = os.path.join(_ROOT, "scripts", "migration_v2104_topics_fixed_slot.sql")
    assert os.path.exists(path), "v2.10.4 主线 SQL migration_v2104_topics_fixed_slot.sql 不存在"
