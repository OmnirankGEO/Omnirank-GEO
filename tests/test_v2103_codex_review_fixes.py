"""v2.10.3 Codex 三审 v2.10.2 5 P0/P1 修复 · 真实数据流测试套

覆盖 Codex 三审找到的"测试 PASS 但真实路径漏"5 大 BUG:

P0-1 · 首次生成阶段 configurable_count 跟 direction-plan 一致(不预扣 fixed)
P0-2 · fixed company slot 在 generate-titles 阶段不占位(配合 P0-1)
P0-3 · KTG prompt/parser 字段统一 · LLM 多种字段名兜底
P1 · recommended mode 未生成 topics 时清 pendingDistribution
真实数据流 · 14 篇 / 2 keyword 项目首次生成自定义配比 14 篇必须 PASS
"""
from __future__ import annotations

import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

# 🔴 [WO_205 2026-09-15] 源码切片改用**函数真实边界**,不再写魔法字节数。
#    旧写法 `src[fn_start : fn_start + N]` 的 N 是**当年**那个函数的长度;函数长大了
#    N 没跟着长 ⇒ 断言的串就在函数体内却掉在窗口外,判据假红,产品行为一个字节没变。
#    逐条量过的偏移/窗口/函数真长见 `tests/_shared/source_slice.py` 抬头(最险差 13 字符)。
#    🔴 **不是放松**:断言一个字没改;放大到整份文件才是放松(别处同名行也能满足)。
from tests._shared.source_slice import function_body


def _read_text(rel_path: str) -> str:
    full_path = os.path.join(_ROOT, rel_path)
    with open(full_path, "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# P0-1 + P0-2 · slot bucket 口径统一 + fixed_count = len(fixed_topics)
# ============================================================

def test_v2103_fixed_count_no_max_prededuct():
    """P0-1/2 · compute_slot_buckets fixed_count 不强制 max(1, len) · 跟实际 existing 一致"""
    from writing.direction_distribution import compute_slot_buckets

    # 场景 1:首次生成 · existing=[] · fixed=0 · configurable=total
    confirmed_kws = [{"required_articles": 7}, {"required_articles": 7}]  # 14 篇
    result = compute_slot_buckets(1, confirmed_kws, [])
    assert result["fixed_count"] == 0, \
        f"v2.10.3 P0-1 破:首次生成 fixed_count 应=0(无 existing topics)· 实际={result['fixed_count']}"
    assert result["configurable_count"] == 14, \
        f"v2.10.3 P0-1 破:首次生成 configurable 应=14(不预扣 fixed)· 实际={result['configurable_count']}"

    # 场景 2:已生成 14 个 topics + 1 个含 company_profile · fixed=1 · configurable=13
    existing = [
        {"id": 1, "style_code": "company_profile", "status": "pending"},
    ] + [
        {"id": i, "style_code": "price_roi", "status": "pending"} for i in range(2, 15)
    ]
    result2 = compute_slot_buckets(1, confirmed_kws, existing)
    assert result2["fixed_count"] == 1, \
        f"v2.10.3 P0-1 破:已生成含 company_profile · fixed=1 · 实际={result2['fixed_count']}"
    assert result2["configurable_count"] == 13, \
        f"v2.10.3 P0-1 破:已生成 14(含 1 fixed)· configurable=13 · 实际={result2['configurable_count']}"


def test_v2103_generate_titles_uses_same_slot_bucket():
    """P0-1 · generate-titles 必须用 compute_slot_buckets 算 configurable(跟 direction-plan 同口径)"""
    src = _read_text("server.py")
    fn_start = src.find("async def api_generate_titles")
    fn_block = src[fn_start:fn_start + 14000]

    # 必须 import compute_slot_buckets · 不再用 sum(required_articles)
    assert "compute_slot_buckets" in fn_block, \
        "v2.10.3 P0-1 破:generate-titles 未用 compute_slot_buckets 统一口径"
    # configurable_count 应来自 buckets · 不是 sum(required_articles)
    assert '_buckets_for_gen["configurable_count"]' in fn_block or \
           "_buckets_for_gen['configurable_count']" in fn_block, \
        "v2.10.3 P0-1 破:_total_articles 未从 slot bucket 取(跟 direction-plan 一致)"


def test_v2103_first_time_full_14_configurable():
    """P0-1/2 真实数据流 · 14 篇 / 2 kw / 0 existing · configurable=14"""
    from writing.direction_distribution import (
        DistributionValidationError,
        compute_slot_buckets,
        compute_recommended_user_choice_distribution,
        validate_user_choice_distribution,
    )

    confirmed_kws = [{"required_articles": 7}, {"required_articles": 7}]
    result = compute_slot_buckets(1, confirmed_kws, [])

    # 首次生成场景核心断言
    assert result["total"] == 14
    assert result["fixed_count"] == 0
    assert result["active_locked_count"] == 0
    assert result["manual_locked_count"] == 0
    assert result["configurable_count"] == 14

    # 推荐配比 sum=14
    recommended = compute_recommended_user_choice_distribution(None, 14)
    assert sum(recommended.values()) == 14

    # 用户按 14 篇配自定义 · 校验 PASS
    # 🔴 [WO_205 2026-09-15 改判据] 原先写死 {price/comparison/data/guide} ——
    #    那套词表**已经不存在了**:现役是六个 family code
    #    (`writing.direction_distribution.USER_CHOICE_OPTIONS` 去掉 `auto`),
    #    连旧 style code(price_roi / comparison_review / data_report / buying_guide)
    #    都只在 `STYLE_CODE_TO_USER_CHOICE` 里作为**映射源**存在。
    #    于是 `validate_user_choice_distribution` 报「非法 key 'price'」——
    #    判据钉的**词表**过期了,它要钉的**行为**(sum == configurable ⇒ 不 400)没变。
    # 🔴 词表**从 SSOT 取**,不再手写第二份 —— 手写的那份就是这次烂掉的东西。
    #    加一档新方向时判据自动跟上;删一档时这里会因为凑不满而红,那是对的。
    user_dist = _distribution_summing_to(14)
    validate_user_choice_distribution(user_dist, configurable_count=14, industry=None)  # 不抛

    # 🔴 [WO_205 注毒暴露] 反向臂:sum 对不上**必须**抛。
    #    原来只有上面那一条「sum 对时不抛」—— 把 sum 校验整段改成恒真,
    #    它照样绿:顺利路径看不见守卫在不在(守卫拆了,顺利路径还是顺利)。
    _wrong = dict(user_dist)
    _wrong[sorted(_wrong)[0]] += 1          # 15 != 14
    try:
        validate_user_choice_distribution(_wrong, configurable_count=14, industry=None)
    except DistributionValidationError:
        pass
    else:
        raise AssertionError(
            "sum=15 vs configurable=14 竟然放行了 —— 篇数校验形同虚设,前端配多少后端就出多少")


def _distribution_summing_to(total: int) -> dict:
    """按**现役**方向词表造一份合计 = total 的配比。

    🔴 词表取自 `USER_CHOICE_OPTIONS`(SSOT)并去掉 `auto` ——
       `auto` 是「系统推荐」这个档,不是一个可分配的方向,
       `validate_user_choice_distribution` 的 keys 校验也把它排除在外。
    🔴 排序后再分配,保证同一棵树上每次跑出同一份 dict(判据不许随集合迭代序漂)。
    """
    from writing.direction_distribution import USER_CHOICE_OPTIONS

    codes = sorted(c for c in USER_CHOICE_OPTIONS if c != "auto")
    assert codes, "现役方向词表是空的 —— 判据没有被测对象"
    base, extra = divmod(int(total), len(codes))
    dist = {c: base for c in codes}
    for c in codes[:extra]:
        dist[c] += 1
    dist = {c: n for c, n in dist.items() if n > 0}
    assert sum(dist.values()) == total, (dist, total)
    return dist

# ============================================================
# P0-3 · KTG prompt/parser 统一
# ============================================================

def test_v2103_ktg_parser_supports_keyword_id_priority():
    """P0-3 · KTG._parse_response 优先按 keyword_id 严格匹配(LLM 透传时)"""
    fn_block = function_body("writing/keyword_topic_generator.py", "_parse_response")

    # 必须先按 keyword_id 匹配
    assert "_llm_kw_id = topic.get(\"keyword_id\")" in fn_block, \
        "v2.10.3 P0-3 破:parser 未优先按 keyword_id 匹配"
    # fallback 1:original_keyword / keyword 兜底
    assert 'topic.get("original_keyword") or topic.get("keyword")' in fn_block, \
        "v2.10.3 P0-3 破:parser 缺 original_keyword/keyword 兜底匹配"


def test_v2103_ktg_parser_fills_original_keyword():
    """P0-3 · parser 必须为返回 topic 补 original_keyword(防 server.py 过滤误删)"""
    fn_block = function_body("writing/keyword_topic_generator.py", "_parse_response")

    # 补 original_keyword(若 LLM 返回 keyword 而非 original_keyword)
    assert 'if not topic.get("original_keyword"):' in fn_block, \
        "v2.10.3 P0-3 破:parser 未补 original_keyword · server.py 过滤会误删"
    assert 'topic["original_keyword"] = kw.get("keyword")' in fn_block, \
        "v2.10.3 P0-3 破:parser 补 original_keyword 形式不对"


def test_v2103_ktg_prompt_consistent_field():
    """P0-3 · KTG prompt 不再要求 LLM 返回 `keyword` 字段(跟 parser 字段名打架)"""
    src = _read_text("writing/keyword_topic_generator.py")
    fn_start = src.find("_style_plan_hint")
    fn_block = src[fn_start:fn_start + 2500]

    # 不能继续要求 LLM 返回 `keyword + slot_index`(跟 parser 用 original_keyword 打架)
    assert "必须含 `keyword` + `slot_index`" not in fn_block and "必须含 `keyword`" not in fn_block, \
        "v2.10.3 P0-3 破:prompt 仍要求 LLM 返回 `keyword` · 跟 parser original_keyword 打架"
    # 应改成 original_keyword 或 slot 顺序
    assert "original_keyword" in fn_block or "slot_0/slot_1" in fn_block, \
        "v2.10.3 P0-3 破:prompt 未统一字段名"


# ============================================================
# P1 · recommended mode 清 pendingDistribution
# ============================================================

def test_v2103_recommended_mode_clears_pending_when_no_titles():
    """P1 · recommended mode + 未生成 topics → 必须调 onSavePendingDistribution(null) 清前端 state"""
    src = _read_text("frontend/src/components/writing/DistributionConfigDialog.tsx")

    # apply 函数 recommended 分支 · 必须先 if (!hasExistingTitles) 清 pending
    recommended_idx = src.find("mode === 'recommended'")
    apply_block = src[recommended_idx:recommended_idx + 2500]

    # 必须调 onSavePendingDistribution?(null) 在未生成场景
    assert "onSavePendingDistribution?.(null)" in apply_block, \
        "v2.10.3 P1 破:recommended mode 未生成场景未清 pendingDistribution"
    # 该分支必须 return(不调后端 reset · 因为没 topics 可 reset)
    has_existing_check = "if (!hasExistingTitles)" in apply_block
    assert has_existing_check, \
        "v2.10.3 P1 破:recommended mode 未按 hasExistingTitles 分支处理"


# ============================================================
# 真实场景集成 · 14 篇项目首次生成自定义 14 篇 PASS
# ============================================================

def test_v2103_real_flow_14_articles_2_keywords_custom_14_passes():
    """v2.10.3 真实数据流 · 14 篇 / 2 keyword / 自定义配比 14 篇 · 必须 PASS

    模拟 Codex 三审报的真实场景:
      项目 14 篇 · 2 个 keyword 各 7 篇 · 用户在弹窗自定义 14 篇分配
      direction-plan 返 configurable=14 · 用户配 14 篇
      点"批量生成标题" · 后端 validate sum=14 vs configurable=14 → PASS · 不再 400
    """
    from writing.direction_distribution import (
        compute_slot_buckets,
        validate_user_choice_distribution,
        DistributionValidationError,
    )

    confirmed_kws = [{"required_articles": 7}, {"required_articles": 7}]
    # direction-plan 阶段
    buckets1 = compute_slot_buckets(1, confirmed_kws, [])
    assert buckets1["configurable_count"] == 14, "direction-plan 应返 14 可分配"

    # 用户配 14 篇
    # 🔴 [WO_205 2026-09-15 改判据] 原先写死 {price/comparison/data/guide} ——
    #    那套词表**已经不存在了**:现役是六个 family code
    #    (`writing.direction_distribution.USER_CHOICE_OPTIONS` 去掉 `auto`),
    #    连旧 style code(price_roi / comparison_review / data_report / buying_guide)
    #    都只在 `STYLE_CODE_TO_USER_CHOICE` 里作为**映射源**存在。
    #    于是 `validate_user_choice_distribution` 报「非法 key 'price'」——
    #    判据钉的**词表**过期了,它要钉的**行为**(sum == configurable ⇒ 不 400)没变。
    # 🔴 词表**从 SSOT 取**,不再手写第二份 —— 手写的那份就是这次烂掉的东西。
    #    加一档新方向时判据自动跟上;删一档时这里会因为凑不满而红,那是对的。
    user_dist = _distribution_summing_to(14)
    assert sum(user_dist.values()) == 14

    # generate-titles 阶段:同口径(existing=[] · configurable=14)
    buckets2 = compute_slot_buckets(1, confirmed_kws, [])
    assert buckets2["configurable_count"] == 14, "generate-titles 应同口径 14"

    # 校验通过(不再 400)
    validate_user_choice_distribution(user_dist, configurable_count=14, industry=None)

    # 🔴 [WO_205] 同上:少一篇必须 400,否则「不再 400」这句话没有反面
    _short = dict(user_dist)
    _short[sorted(_short)[0]] -= 1          # 13 != 14
    try:
        validate_user_choice_distribution(_short, configurable_count=14, industry=None)
    except DistributionValidationError:
        pass
    else:
        raise AssertionError("sum=13 vs configurable=14 竟然放行了")


def test_v2103_real_flow_recommended_after_pending_clears():
    """P1 真实数据流 · 用户先保存 14 篇 pending → 改 recommended · 前端 state 必须清"""
    # 此测试验证 Dialog 代码逻辑(不调真前端)
    src = _read_text("frontend/src/components/writing/DistributionConfigDialog.tsx")

    # 必须存在:用户保存后又改 recommended · 必须 clear pending
    # 关键代码:if (!hasExistingTitles) { onSavePendingDistribution?.(null); ... return; }
    assert "已恢复为系统推荐 · 批量生成将走系统比例" in src, \
        "v2.10.3 P1 真实流 破:recommended 未生成场景 toast 文案不明确"
