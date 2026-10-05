"""v2.10 文章方向配比器 · 后端 SSOT helpers

锁死 v5.1.1 34 约束 · 单一权威源 · server.py / KeywordTopicGenerator / 测试都从此 import

模块组成:
  1. 常量:LOCKED_STATUSES / VALID_SOURCES / USER_CHOICE_OPTIONS
  2. helpers:is_fixed_company_topic / is_user_choice_option / is_source_valid
  3. 转换层:style_code_to_user_choice / user_choice_to_style_code
  4. 算法:largest_remainder / compute_recommended_user_choice_distribution
  5. slot 桶:compute_slot_buckets(互斥计数 + 不变量校验)
  6. 校验:validate_user_choice_distribution(行业 hard rule + sum + key 域)
"""

from __future__ import annotations
from typing import Any

from .article_style_contract import (
    USER_CHOICE_OPTIONS,
    USER_CHOICE_SELECTABLE,
    normalize_user_choice,
)

# [P1 容量合同 2026-08-08] 篇数唯一取数出口
from tools.pricing_bands import (
    LEGACY_MISSING_CAPACITY_DEFAULT as _CAPACITY_MISSING_DEFAULT,
    normalize_article_capacity as _normalize_article_capacity,
)


def _capacity_of(keyword: dict[str, Any]) -> int:
    """关键词的**可交付容量上限**(显式 0 保留 · 缺失走具名兜底常量)。"""
    return _normalize_article_capacity(
        keyword.get("required_articles"), when_missing=_CAPACITY_MISSING_DEFAULT
    )

# ============================================================
# 1. 常量 SSOT
# ============================================================

# v2.10 P0-2:锁定状态(批量配比不可改 · 防竞态)
# regenerating 必加(Codex 四审 P0-2)· LLM 正在重写标题时改方向 → user_choice/title 错位
LOCKED_STATUSES: frozenset[str] = frozenset({"completed", "writing", "regenerating"})

# v2.10 source 取值域
VALID_SOURCES: frozenset[str] = frozenset({"manual", "batch_uniform", "batch_distribution"})



# ============================================================
# 2. helpers
# ============================================================

def is_fixed_company_topic(topic: dict[str, Any]) -> bool:
    """v2.10 兼容 4 种 fixed slot 标记 · 单一权威判定(Codex 五审 P1)

    历史数据 4 种标记同时存在:
      - is_fixed (BOOLEAN field · 老数据)
      - style_code='company_profile'(英文 · v2.7.1+)
      - article_style='公司深度报道'(中文)
      - 前端 topic.is_fixed && topic.style_code === 'company_profile'(双重)

    任一命中即 fixed · 防 compute_slot_buckets 漏算 fixed → 双扣进 active_locked
    """
    if not isinstance(topic, dict):
        return False
    return bool(
        topic.get("is_fixed")
        or topic.get("style_code") == "company_profile"
        or topic.get("article_style") == "公司深度报道"
    )


def is_user_choice_option(choice: str | None) -> bool:
    """Accept canonical family codes, known legacy aliases, and outward-only directions.

    🔴 [#185] 这里用 `USER_CHOICE_SELECTABLE`(能选的)而**不是**
       `USER_CHOICE_OPTIONS`(推荐配比的定义域)。两者在本文件里都出现,
       用错哪一个都不报错:
         · 校验用窄的 -> 用户选了防御型被 400 拒,而前端明明给了这个选项;
         · 配比域用宽的 -> 系统推荐里凭空多一档,**每个存量项目**的推荐篇数都变。
    """
    if choice is None:
        return True  # NULL 合法
    return normalize_user_choice(choice) in USER_CHOICE_SELECTABLE


def is_source_valid(source: str | None) -> bool:
    """user_choice_source 必须是 NULL / manual / batch_uniform / batch_distribution"""
    if source is None:
        return True
    return source in VALID_SOURCES


# ============================================================
# 3. 转换层:style_code → user_choice(v5.1.1 P1-6)
# ============================================================

# [命名 SSOT · 2026-07-28] style_code(11 个) → family_code(6 个)。
#
# 这张表是**唯一**的 style→family 归属源。映射返 None 的 style_code 会在选题/
# 标题层被静默丢弃 —— 这正是 2026-07 "排名文放回来了却没有影子" 的直接根因:
# WP12 把 ranking_v2 复活并给了 20% 配比，但这里仍是 None，于是
# `keyword_topic_generator._family_ratio_prompt` 累加时跳过它，喂给 LLM 的
# 六类配比合计只有 80%，榜单族显示 32%(实际应为 52%)。
#
# 因此 None 只允许留给两类:
#   · 已退役、永不参与新生成的 style(trojan_horse)
#   · 不走 ratio 分配、由固定槽位产出的 style(company_profile)
# 任何 active 且可能拿到配比的 style_code 必须有家族，由
# `validate_style_family_mapping()` 与判别测试守卫。
STYLE_CODE_TO_USER_CHOICE: dict[str, str | None] = {
    "price_roi": "case_data_roi",
    "comparison_review": "multi_brand_comparison",
    "risk_compliance": "trend_policy_risk",
    "data_report": "case_data_roi",
    "buying_guide": "implementation_guide",
    "qa_recommendation": "evidence_qa",
    "brand_softarticle": "company_facts",
    # [P0-1 2026-07-28] 榜单是"选购与多品牌比较"的强形态,不是第七个家族。
    # 与 recommendation_review / comparison_review 同族,对用户仍只暴露 6 类。
    "ranking_v2": "multi_brand_comparison",
    "authority_ranking": "multi_brand_comparison",
    "recommendation_review": "multi_brand_comparison",
    # 退役:永不参与新生成(article_style_contract.DISABLED_NEW_GENERATION_STYLES)
    "trojan_horse": None,
    # 固定槽位:走 fixed_count=1,不进 ratio 分配
    "company_profile": None,
}

# None 的**唯一**合法持有者。新增 None 必须先进这个集合并说明理由。
STYLE_CODES_WITHOUT_FAMILY: frozenset[str] = frozenset({"trojan_horse", "company_profile"})


def style_code_to_user_choice(style_code: str) -> str | None:
    """style_code → family_code 映射 · 只有退役/固定槽位 style 返 None。"""
    return STYLE_CODE_TO_USER_CHOICE.get(style_code)


def validate_style_family_mapping() -> list[str]:
    """[P0-1 判别锁] 每个 active style_code 必须有家族归属。

    遍历 `style_ratios` 的全部 11 码:除退役集与固定槽位外，映射返 None 或映射到
    未知家族都是缺陷 —— 那意味着该文体的配比会在选题/标题层静默蒸发。
    返回错误码列表(空 = 通过)，供启动诊断与判别测试共用。
    """
    from config.settings_manager import get_current_settings
    from writing.article_style_contract import STYLE_FAMILIES

    errors: list[str] = []
    settings = get_current_settings()
    style_codes = set(getattr(settings, "style_ratios", None) or {})
    style_codes |= set(STYLE_CODE_TO_USER_CHOICE)

    for code in sorted(style_codes):
        if code in STYLE_CODES_WITHOUT_FAMILY:
            if STYLE_CODE_TO_USER_CHOICE.get(code) is not None:
                errors.append(f"exempt_style_must_map_to_none:{code}")
            continue
        family = STYLE_CODE_TO_USER_CHOICE.get(code)
        if family is None:
            errors.append(f"active_style_missing_family:{code}")
        elif family not in STYLE_FAMILIES:
            errors.append(f"style_maps_to_unknown_family:{code}:{family}")
    return errors


def family_ratio_coverage(industry: str | None = None) -> dict[str, object]:
    """[P0-1 判别锁] 折算到六家族后配比合计是否仍为 100。

    断链会让合计掉到 100 以下(蒸发的那部分就是丢掉的配比)，这是最直观、
    最难糊弄的观测量。
    """
    from config.settings_manager import get_effective_style_ratios
    from writing.article_style_contract import STYLE_FAMILIES

    internal = get_effective_style_ratios(industry, unit="percent")
    families = {code: 0.0 for code in STYLE_FAMILIES}
    dropped: dict[str, float] = {}
    for style_code, ratio in internal.items():
        value = float(ratio or 0)
        family = style_code_to_user_choice(style_code)
        if family in families:
            families[family] += value
        elif value > 0:
            dropped[style_code] = value
    return {
        "industry": industry or "",
        "family_ratios": families,
        "family_total": round(sum(families.values()), 4),
        "dropped_style_ratios": dropped,
        "dropped_total": round(sum(dropped.values()), 4),
        "complete": abs(sum(families.values()) - 100.0) < 1e-6 and not dropped,
    }


def normalize_user_choice_distribution(distribution: Any) -> Any:
    """Collapse historical choice keys into the canonical six-family domain.

    Unknown keys are preserved so the validator can reject them explicitly.
    Counts for legacy aliases that map to the same family are added together.
    """
    if not isinstance(distribution, dict):
        return distribution
    normalized: dict[str, Any] = {}
    for key, count in distribution.items():
        canonical = normalize_user_choice(str(key))
        target = canonical if canonical not in (None, "auto") else str(key)
        if target in normalized and isinstance(count, int) and isinstance(normalized[target], int):
            normalized[target] += count
        else:
            normalized[target] = count
    return normalized


def build_user_choice_style_plan(
    keywords: list[dict[str, Any]],
    distribution: dict[str, int],
    *,
    source: str | None,
    industry: str | None = None,
    posts_per_keyword: dict[Any, int] | None = None,
) -> list[dict[str, Any]]:
    """Assign an exact six-family distribution to keyword slots deterministically.

    🔴 [Review 09-28 · 自媒体单生成标题全入口 500] 文体是按**生成出来的每一条标题**分配的,
       所以对内按**条**排:`posts_per_keyword = {keyword_id: 本次要出的条数}` 给了,
       `expected_total` 与每词 slot 数都按条;不给 = 老行为(按合同**槽** `_capacity_of`),
       其他调用方不受影响。
       之前出题侧按条(`_required_article_count`,7 槽 ⇒ 35 条)算推荐总数、这里按槽(7)校验
       ⇒ 条 ≠ 槽(自媒体口径 1 槽≈5 条)时必抛,整批生成 500 + 全额退款。
    """
    # [P1 容量合同 2026-08-08] 原 `or 1` 会把**显式 0 篇**(覆盖词 is_core=False · 生产实测 218 行)
    #   当假值吃掉,静默按 1 篇排 slot。走 SSOT 规整:0 就是 0,缺失才用兜底常量。
    if posts_per_keyword is None:
        def _slots_of(keyword):
            return _capacity_of(keyword)
    else:
        def _slots_of(keyword):
            return max(0, int(posts_per_keyword.get(keyword.get("id"), 0)))
    expected_total = sum(_slots_of(keyword) for keyword in keywords)
    normalized = normalize_user_choice_distribution(distribution)
    validate_user_choice_distribution(normalized, expected_total, industry=industry)

    remaining = dict(normalized)
    keyword_slots = {
        keyword.get("id"): list(range(_slots_of(keyword)))
        for keyword in keywords
    }
    plan: list[dict[str, Any]] = []
    max_slots = max((len(slots) for slots in keyword_slots.values()), default=0)
    for slot_index in range(max_slots):
        for keyword_id, slots in keyword_slots.items():
            if slot_index >= len(slots):
                continue
            for family_code, count in remaining.items():
                if count <= 0:
                    continue
                plan.append({
                    "keyword_id": keyword_id,
                    "slot_index": slot_index,
                    "user_choice": family_code,
                    "user_choice_source": source,
                })
                remaining[family_code] = count - 1
                break

    if len(plan) != expected_total or any(remaining.values()):
        raise DistributionValidationError("六类文体配比未能完整分配到全部标题 slot")
    return plan


def scale_distribution_to_posts(distribution: dict[str, int], total_posts: int, *,
                                defensive_cap: int | None = None) -> dict[str, int]:
    """服务商按**槽**给的自定义配比 -> 本次按**条**生成用的配比(最大余数法,合计恰为 total_posts)。

    🔴 [Review 09-28] 对服务商配比仍按槽(Owner 09-15「客户不见 5 倍」):批量配比弹窗、
       configurable_count 这些用户面口径不动;只在生成前把槽级配比按比例放大到条数,
       不许直接拿槽级计划去配条级标题。k=1(条 == 槽)时原样返回,逐项不变。
    """
    counts = {k: int(v) for k, v in (distribution or {}).items()}
    scaled = (counts if sum(counts.values()) == int(total_posts)
              else largest_remainder({k: float(v) for k, v in counts.items()}, int(total_posts)))
    # 防御型(公司词)的上限按**标题条数**计(`defensive_capacity()` = 八问 + 变体,不重复的题只有这么多;
    # 子集批次再减去同一单别的词已有的防御题,由调用方经 `defensive_cap` 传入),前端按槽封顶。
    # 放大后超出的部分按最大余数分给服务商选了的其他方向;
    # 🔴 [Review 09-28 · WO_317 第三笔] 只选了防御型时无处可分 ⇒ 钳到上限,**合计会小于 total_posts**,
    #    由调用方按合计把本批条数收小并在回包里写明(不许静默少给,也不许报错不出)。
    from .defensive_questions import defensive_capacity
    cap = defensive_capacity() if defensive_cap is None else max(0, int(defensive_cap))
    over = int(scaled.get("defensive_company") or 0) - cap
    if over > 0:
        scaled = dict(scaled)
        scaled["defensive_company"] = cap
        others = {k: float(v) for k, v in counts.items() if k != "defensive_company" and v > 0}
        if others:
            for k, extra in largest_remainder(others, over).items():
                scaled[k] = scaled.get(k, 0) + extra
    return scaled


def trim_posts_to_total(posts_per_keyword: dict[Any, int], total: int) -> dict[Any, int] | None:
    """把本批每词条数按比例收小到合计 `total`(每个词至少 1 条,最大余数法)。

    🔴 每词至少 1 条:扣费按关键词(`topic_gen_charge.keyword_count`),收成 0 条的词就是收了钱不出题。
       `total` 小于词数时无法做到 ⇒ 返回 None,由调用方在扣费前拒(或已扣则退费拒)。
    """
    posts = {k: max(0, int(v)) for k, v in posts_per_keyword.items() if int(v) > 0}
    total = int(total)
    if total >= sum(posts.values()):
        return dict(posts)
    if total < len(posts):
        return None
    rest = largest_remainder({k: float(v - 1) for k, v in posts.items()}, total - len(posts))
    return {k: 1 + int(rest.get(k, 0)) for k in posts}


# ============================================================
# 4. 算法
# ============================================================

def largest_remainder(weights: dict[str, float], total: int) -> dict[str, int]:
    """largest remainder 分配算法(Codex 二审 P0-7)

    输入:
      weights: {key: ratio}  · 比例字典(sum 通常 == 1.0 · 不强制)
      total: int  · 待分配总数(必须 > 0)

    输出:
      {key: int_count}  · 整数分配 · sum == total

    算法:
      1. raw[k] = weights[k] * total
      2. floor[k] = int(raw[k])
      3. remainder = total - sum(floor)
      4. fractional[k] = raw[k] - floor[k]
      5. 按 fractional 降序补 1 · 直到补满 remainder
      6. tie-break:fractional 相同时按 key 字典序(保证确定性)

    边界:
      total=0 → 全 0
      weights 全 0 → 全 0(remainder 不补)
      weights sum != 1.0 → 等比缩放后分配
    """
    if total <= 0 or not weights:
        return {k: 0 for k in weights}

    weight_sum = sum(weights.values())
    if weight_sum <= 0:
        return {k: 0 for k in weights}

    # 归一化(防 sum != 1.0)
    normalized = {k: v / weight_sum for k, v in weights.items()}

    # raw + floor
    raw = {k: normalized[k] * total for k in weights}
    floor_counts = {k: int(v) for k, v in raw.items()}
    fractional = {k: raw[k] - floor_counts[k] for k in weights}

    remainder = total - sum(floor_counts.values())
    if remainder <= 0:
        return floor_counts

    # 按 fractional 降序 + key 字典序 tie-break · 取前 remainder 项 +1
    sorted_keys = sorted(weights.keys(), key=lambda k: (-fractional[k], k))
    for k in sorted_keys[:remainder]:
        floor_counts[k] += 1

    # 不变量
    assert sum(floor_counts.values()) == total, \
        f"largest_remainder sum 不等于 total · {floor_counts} sum != {total}"

    return floor_counts


# ============================================================
# 5. slot 桶互斥计数(Codex 四审 P0-1 + 五审 1+2)
# ============================================================

def compute_slot_buckets(
    quote_id: int,
    confirmed_keywords: list[dict],
    existing_topics: list[dict],
) -> dict[str, Any]:
    """计算 quote 的 4 桶互斥 slot 计数 · 防双扣 + 防 configurable 负数

    输入:
      quote_id: int(仅用于日志)
      confirmed_keywords: [{required_articles, ...}, ...] · core kw 列表
      existing_topics: [{id, status, user_choice_source, style_code, article_style, is_fixed, ...}, ...]

    输出:
      {
        "total": int,
        "fixed_count": int,
        "active_locked_count": int,
        "manual_locked_count": int,
        "configurable_count": int,
        "drift_warning": bool,
      }

    互斥规则(单 slot 只归一桶 · 优先级降序):
      fixed > active_locked > manual_locked > configurable

    脏数据守护(Codex 五审 2):
      existing_topics > required_articles_sum → total = max · drift_warning=True
      不变量:fixed + active + manual + configurable == total
    """
    # [P1 容量合同 2026-08-08] 同上:显式 0 不再被 `or 1` 吃掉(否则 required_sum 虚高 → 桶计数错)
    required_sum = sum(_capacity_of(kw) for kw in confirmed_keywords)
    existing_count = len(existing_topics)

    # 脏数据:existing > required → total 取 max(防 configurable 负)
    total = max(required_sum, existing_count)
    drift_warning = existing_count > required_sum

    # 1. fixed slot(企业介绍 · 兼容 4 种标记)
    # v2.10.4 Codex 四审 P0-1 拍板:走 B · 承认现状 · 修 SSOT 测试
    # 历史背景:v2.4 P0 #1 SSOT(company_profile fixed_count=1)实际在旧 tools/article_generator.calculate_distribution 实现
    #   v2.10 已 hard 400 废弃 /api/articles/plan · 该 SSOT 链路实际失效
    # 现状:v2.7.5+ prod 实际 generate-titles 不创建 fixed topic · ArticleWriter ratio 抽
    # 修(v2.10.4):
    #   fixed_count = len(fixed_topics)(实际数 · v2.10.3 修法保留)
    #   承认"无强制 fixed slot"的当前 prod 行为(跟 v2.4 旧 SSOT 一致性已断)
    #   article_generator_service.py:382 路径保留(若 admin 迁移产生 is_fixed=True topic 仍走 company_profile)
    #   后续 v2.11 单独 sprint 真做 fixed slot 创建逻辑(若产品要恢复 SSOT 强制)
    fixed_topics = [t for t in existing_topics if is_fixed_company_topic(t)]
    fixed_count = len(fixed_topics)

    # 2. active_locked(排除 fixed · status in LOCKED_STATUSES)
    active_locked_topics = [
        t for t in existing_topics
        if not is_fixed_company_topic(t)
        and (t.get("status") or "") in LOCKED_STATUSES
    ]
    active_locked_count = len(active_locked_topics)

    # 3. manual_locked(排除 fixed + active · source='manual')
    manual_locked_topics = [
        t for t in existing_topics
        if not is_fixed_company_topic(t)
        and (t.get("status") or "") not in LOCKED_STATUSES
        and t.get("user_choice_source") == "manual"
    ]
    manual_locked_count = len(manual_locked_topics)

    # 4. configurable = total - fixed - active - manual(数学保证非负)
    configurable_count = total - fixed_count - active_locked_count - manual_locked_count

    # 不变量校验(防双扣 bug)
    assert fixed_count + active_locked_count + manual_locked_count + configurable_count == total, \
        f"slot 桶不互斥 · quote={quote_id} {fixed_count}+{active_locked_count}+{manual_locked_count}+{configurable_count}!={total}"

    # configurable 不能负(v2.10.3:fixed_count = len(fixed_topics) 后理论上不会负 · 兜底保留)
    if configurable_count < 0:
        # 极端脏数据兜底:fixed + active + manual > total → configurable=0
        configurable_count = 0
        # 重算 fixed 让不变量成立
        fixed_count = total - active_locked_count - manual_locked_count
        if fixed_count < 0:
            fixed_count = 0
            drift_warning = True

    return {
        "total": total,
        "fixed_count": fixed_count,
        "active_locked_count": active_locked_count,
        "manual_locked_count": manual_locked_count,
        "configurable_count": configurable_count,
        "drift_warning": drift_warning,
    }


# ============================================================
# 6. 推荐配比计算(largest_remainder + style_code→user_choice 转换)
# ============================================================

def compute_recommended_user_choice_distribution(
    industry: str | None,
    configurable_count: int,
) -> dict[str, int]:
    """系统推荐配比 · 返 user_choice 维度的整数篇数(不返 style_code)

    输入:
      industry: 行业 key(医疗/法律会有 override · 0 ranking)
      configurable_count: 可分配数(由 compute_slot_buckets 算)

    输出:
      {family_code: int_count}  · 六项内可能部分为 0 · sum == configurable_count

    流程:
      1. 从 config.settings_manager 取 get_effective_style_ratios(industry)
      2. 按 STYLE_CODE_TO_USER_CHOICE 转换层 · 系统保留(ranking 等)累加但不返
      3. 仅返 outward family 维度(六项)
      4. largest_remainder 整数分配 · sum == configurable_count

    医疗/法律 hard rule:
      industry_overrides 已让 ranking_v2/authority_ranking = 0
      转换层自动跳过(STYLE_CODE_TO_USER_CHOICE[ranking_v2]=None)
      所以医疗/法律 user_choice 不出榜单(0 比例)
    """
    if configurable_count <= 0:
        return {uc: 0 for uc in USER_CHOICE_OPTIONS if uc != "auto"}

    # lazy import 防循环
    try:
        from config.settings_manager import get_effective_style_ratios
        style_ratios = get_effective_style_ratios(industry, unit="fraction")
    except Exception:
        # 兜底:平均分配六项(排除 auto)
        valid_choices = [uc for uc in USER_CHOICE_OPTIONS if uc != "auto"]
        weights = {uc: 1.0 / len(valid_choices) for uc in valid_choices}
        return largest_remainder(weights, configurable_count)

    # 转换 style_code → user_choice · 累加(多 style_code 映射同一 user_choice 的情况)
    user_choice_weights: dict[str, float] = {}
    for style_code, ratio in style_ratios.items():
        uc = style_code_to_user_choice(style_code)
        if uc is None:
            continue  # 系统保留 · 不进用户 distribution
        user_choice_weights[uc] = user_choice_weights.get(uc, 0.0) + ratio

    # 确保六项都在(0 的也返 · 前端 UI 展示)
    for uc in USER_CHOICE_OPTIONS:
        if uc == "auto":
            continue
        user_choice_weights.setdefault(uc, 0.0)

    # auto 不进 distribution(系统推荐就是分配过程本身)
    user_choice_weights.pop("auto", None)

    return largest_remainder(user_choice_weights, configurable_count)


# ============================================================
# 7. user_choice_distribution 校验(Codex 二审 P0-2 + 三审 P0-2)
# ============================================================

class DistributionValidationError(Exception):
    """user_choice_distribution 校验失败 · 后端 raise 400"""
    pass


def validate_user_choice_distribution(
    distribution: dict[str, int],
    configurable_count: int,
    industry: str | None = None,
) -> None:
    """v2.10 user_choice_distribution 后端校验 · 失败 raise DistributionValidationError(→ 400)

    校验项(Codex 二审/三审 + 老板 5 拍):
      1. keys 必须是六个 family code · 不接 style_code/ranking_v2/company 等工程词
      2. ranking 不暴露(自定义模式 · USER_CHOICE_OPTIONS 已不含 ranking)
      3. sum(distribution.values()) == configurable_count(不是 total)
      4. 医疗/法律 hard rule(虽然 USER_CHOICE_OPTIONS 已不含 ranking · 但 defense-in-depth)
      5. count 必须非负整数
    """
    if not isinstance(distribution, dict):
        raise DistributionValidationError("user_choice_distribution 必须是 dict 类型")

    # 1. keys 校验
    for key in distribution.keys():
        # [#185] 自定义配比可以点防御型 -> 用 SELECTABLE;推荐配比的定义域
        # 仍是六家族(见 `compute_recommended_user_choice_distribution`)。
        if key not in USER_CHOICE_SELECTABLE or key == "auto":
            raise DistributionValidationError(
                f"user_choice_distribution 含非法 key '{key}' · 只接受可选的文章方向(auto 除外)"
            )

    # 5. count 必须非负整数
    for key, count in distribution.items():
        if not isinstance(count, int) or count < 0:
            raise DistributionValidationError(
                f"user_choice_distribution['{key}']={count} · 必须非负整数"
            )

    # [#185] 防御型(公司词)有**容量上限** = 企业八问 + 变体。
    # 超了不是"少给几篇",是会出现两条几乎一样的标题 —— 那比少两篇更难跟客户解释。
    # 前端也封顶,但前端封顶只是提示:绕过 UI 的调用必须在这里被拦住。
    from .defensive_questions import defensive_capacity
    _cap = defensive_capacity()
    _want = int(distribution.get("defensive_company") or 0)
    if _want > _cap:
        raise DistributionValidationError(
            f"防御型(公司词)最多 {_cap} 篇,再多就会出现几乎一样的标题。"
            f"本次选了 {_want} 篇,请调到 {_cap} 篇以内。"
        )

    # 3. sum 校验
    total = sum(distribution.values())
    if total != configurable_count:
        raise DistributionValidationError(
            f"user_choice_distribution sum={total} != configurable_count={configurable_count} · "
            f"自定义配比合计必须 = 可分配数"
        )

    # 4. 医疗/法律 hard rule(defense-in-depth · 即使 USER_CHOICE_OPTIONS 已不含 ranking)
    # ranking 不在 USER_CHOICE_OPTIONS · 但若未来扩展加入 · 此处兜底
    if industry in ("medical", "legal", "医疗", "法律"):
        # 当前 distribution keys 都是六个 outward family · 不含 ranking · 无需额外拦
        # 留兜底点(v2.10.1 若开放 ranking → 此处加 raise)
        pass
