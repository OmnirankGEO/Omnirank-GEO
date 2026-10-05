"""
Stage 3 URL 质量预算 (2026-07-16 P1 返工)

问题: 生产 Stage 2 输出 10,795 URL, Jina ~38 URL/min → Stage 3 预计 4-5h,
违反全轮 ≤1.5h。本模块给 URL 打质量分 + 按行业/平台/域名配额 + 总量上限选出
预算子集, 不无差别抓全部。

设计原则:
- 保留高相关(答案实际引用/靠前 rank)、高权威(domain whitelist)、被多次/跨平台
  引用的来源; 去除低价值重复来源(同域名刷屏、单次引用的 gray 尾部)。
- 全纯函数、确定性(同输入同输出, tie-break 用 url_hash 稳定排序), 便于单测与
  幂等续跑(续跑重算得到同一预算集)。
- DB IO 不在本模块(raw 行由调用方查好传入; 选中集由调用方持久化)。

术语: candidate = stage2 kept 的一个 URL(全局首现去重后, 每 URL 唯一);
signals = 从 geo_research_raw 全量按 normalized_url 聚合的引用信号。
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Any

from services.research_monitor.url_normalizer import normalize_url


# ==================== 质量分权重(注释即口径; 需调走 config 层, 见 round_runner) ====================

# 答案实际引用来源权重最高(要求3明示"保留答案实际引用来源")
W_ANSWER_CITED = 3.0
# 域名权威度
W_AUTHORITY = 2.0
# 引用位置(rank 越靠前越相关)
W_RANK = 1.5
# 被引用次数(跨 prompt 多次引用 = 高价值)
W_CITATION_COUNT = 1.5
# 跨平台数(多平台共同引用 = 强信号)
W_CROSS_PLATFORM = 1.0

_TIER_WEIGHT = {'whitelist': 1.0, 'gray': 0.4, 'blacklist': 0.0}
_CITATION_COUNT_CAP = 5      # 引用次数封顶(防个别 URL 刷分)
_CROSS_PLATFORM_MAX = 4      # 平台总数

# Jina 实测吞吐(URL/min · 并发 JINA_CONCURRENCY=2 限速)· 预算算 stage3 时长用
JINA_THROUGHPUT_URL_PER_MIN = 38.0


@dataclass
class UrlSignals:
    """某 normalized_url 从 raw 全量聚合出的引用信号。"""
    citation_count: int = 0
    platforms: Set[str] = field(default_factory=set)
    min_cite_position: Optional[int] = None   # 最靠前的引用位置(越小越相关)
    any_answer_cited: bool = False             # 是否被任一答案实际引用


def normalize_and_aggregate_signals(raw_rows: List[Dict[str, Any]]) -> Dict[str, UrlSignals]:
    """把 geo_research_raw 全量行按 normalized_url 聚合成信号字典(纯函数)。

    raw_rows 每行需含: cite_url, engine, cite_position, is_answer_cited(可选缺省 False)。
    归一化用与 stage2 相同的 normalize_url, 保证 key 与 candidate.normalized_url 对齐。
    """
    signals: Dict[str, UrlSignals] = {}
    for row in raw_rows:
        cite_url = row.get('cite_url') or ''
        if not cite_url:
            continue
        norm = normalize_url(cite_url)
        if not norm:
            continue
        sig = signals.get(norm)
        if sig is None:
            sig = UrlSignals()
            signals[norm] = sig
        sig.citation_count += 1
        engine = row.get('engine') or ''
        if engine:
            sig.platforms.add(engine)
        pos = row.get('cite_position')
        if isinstance(pos, int) and pos > 0:
            if sig.min_cite_position is None or pos < sig.min_cite_position:
                sig.min_cite_position = pos
        if row.get('is_answer_cited'):
            sig.any_answer_cited = True
    return signals


def _rank_weight(min_pos: Optional[int]) -> float:
    """引用位置 → [0,1] 权重。pos=1(最靠前)≈1.0, 无位置信息给中性 0.5。"""
    if not min_pos or min_pos <= 0:
        return 0.5
    return 1.0 / (1.0 + (min_pos - 1) * 0.5)  # pos1=1.0 pos2≈0.67 pos3=0.5 ...


def score_candidate(domain_tier: str, signals: Optional[UrlSignals]) -> float:
    """给一个 candidate 打质量分(纯函数, 确定性)。signals 缺失(raw 无记录, 罕见)按最弱。"""
    tier_w = _TIER_WEIGHT.get(domain_tier, 0.4)
    if signals is None:
        # 无聚合信号(理论上 candidate 必来自 raw, 兜底给权威分 + 中性 rank)
        return W_AUTHORITY * tier_w + W_RANK * 0.5
    answer_w = 1.0 if signals.any_answer_cited else 0.0
    rank_w = _rank_weight(signals.min_cite_position)
    count_w = min(signals.citation_count, _CITATION_COUNT_CAP) / _CITATION_COUNT_CAP
    cross_w = (min(len(signals.platforms), _CROSS_PLATFORM_MAX) - 1) / max(_CROSS_PLATFORM_MAX - 1, 1)
    cross_w = max(cross_w, 0.0)
    return (
        W_ANSWER_CITED * answer_w
        + W_AUTHORITY * tier_w
        + W_RANK * rank_w
        + W_CITATION_COUNT * count_w
        + W_CROSS_PLATFORM * cross_w
    )


@dataclass
class BudgetConfig:
    budget_max: int = 1500          # 总量上限(全轮 ≤1.5h · stage3 ≈ budget/38 min)
    min_urls_per_industry: int = 40  # 每行业保覆盖底线
    min_urls_per_platform: int = 50  # 每平台保覆盖底线(4 平台代表性)
    max_urls_per_domain: int = 30    # 单域名上限(去低价值重复来源)
    max_urls_per_industry: int = 0   # 0=自动(budget_max//industries × 2 软上限, 防单行业霸占)


def select_url_budget(
    candidates: List[Dict[str, Any]],
    config: BudgetConfig,
) -> List[Dict[str, Any]]:
    """按质量分 + 配额选出预算子集(纯函数, 确定性)。

    candidates 每项需含: url_hash, normalized_url, industry_name, platform, domain, score
    (score 由调用方用 score_candidate 算好)。返回选中项 list(原 dict + 'budget_rank'),
    按最终 (score desc, url_hash) 全局排序, budget_rank 从 1 起。

    分配(覆盖优先 → 质量填充, 全程受 max_per_domain 约束):
      Round-1a 行业覆盖: 每行业内 score desc 取 top min_urls_per_industry
      Round-1b 平台覆盖: 每平台全局补足到 min_urls_per_platform
      Round-2  质量填充: 剩余候选全局 score desc 填到 budget_max, 受 max_per_industry 软上限
    """
    if config.budget_max <= 0 or not candidates:
        return []

    # 确定性全局排序键: score desc, 然后 url_hash asc(稳定 tie-break)
    def _sort_key(c):
        return (-float(c.get('score') or 0.0), str(c.get('url_hash') or ''))

    num_industries = len({c.get('industry_name') or '' for c in candidates}) or 1
    max_per_industry = config.max_urls_per_industry
    if max_per_industry <= 0:
        max_per_industry = max(
            config.min_urls_per_industry,
            (config.budget_max // num_industries) * 2,
        )

    # [轮1审核修] 行业覆盖底线钳到公平上界: 行业数多时若 min_per_industry×N > budget_max,
    # round-1a 纯字母序贪心会把预算吃光 → 字母序尾部行业被饿死 + 平台/质量填充静默归零。
    # 钳制后 N ≤ budget_max 时覆盖率【整体等比降级】而非饿死固定子集;
    # N > budget_max(单轮 >1500 行业, 生产不可达)时物理上无法人人有份, 退化为字母序
    # 前 budget_max 个行业各 1(仍严格优于 pre-fix 的字母序前 ~37 行业各 40)。
    effective_industry_floor = min(
        config.min_urls_per_industry,
        max(config.budget_max // num_industries, 1),
    )

    selected: Dict[str, Dict[str, Any]] = {}  # key = (url_hash, industry_name) 唯一
    domain_count: Dict[str, int] = {}
    industry_count: Dict[str, int] = {}
    platform_count: Dict[str, int] = {}

    def _key(c):
        return f"{c.get('url_hash')}|{c.get('industry_name') or ''}"

    def _try_add(c, *, enforce_industry_cap: bool = True) -> bool:
        k = _key(c)
        if k in selected:
            return False
        if len(selected) >= config.budget_max:
            return False
        dom = c.get('domain') or ''
        ind = c.get('industry_name') or ''
        if domain_count.get(dom, 0) >= config.max_urls_per_domain:
            return False
        # [轮1审核修] max_per_industry 软上限统一在 _try_add 生效(原仅 round-2),
        # 兑现"防单行业霸占"承诺。auto 模式(生产恒走, 见 max_per_industry 计算)下
        # effective_industry_floor ≤ min ≤ max_per_industry 成立; 但若调用方【显式】配
        # max < min(误配)则 floor 可能 > max。故 round-1a 保覆盖轮显式关闭本上限
        # (enforce_industry_cap=False, 覆盖优先于软上限, 与"覆盖→质量填充"设计一致),
        # 防止误配下覆盖底线被自身上限挡。防霸占只作用于 round-1b/round-2。
        if enforce_industry_cap and industry_count.get(ind, 0) >= max_per_industry:
            return False
        selected[k] = c
        domain_count[dom] = domain_count.get(dom, 0) + 1
        industry_count[ind] = industry_count.get(ind, 0) + 1
        plat = c.get('platform') or ''
        platform_count[plat] = platform_count.get(plat, 0) + 1
        return True

    # 分组(组内确定性排序)
    by_industry: Dict[str, List[Dict]] = {}
    by_platform: Dict[str, List[Dict]] = {}
    for c in candidates:
        by_industry.setdefault(c.get('industry_name') or '', []).append(c)
        by_platform.setdefault(c.get('platform') or '', []).append(c)
    for lst in by_industry.values():
        lst.sort(key=_sort_key)
    for lst in by_platform.values():
        lst.sort(key=_sort_key)

    # Round-1a 行业覆盖(确定性: 行业名排序遍历; 底线用 effective_industry_floor 防饿死)
    # 覆盖优先于 max_per_industry 软上限(enforce_industry_cap=False), 防误配 max<min 时
    # 覆盖底线被自身上限挡(轮2审核 LOW 修)。
    for ind in sorted(by_industry.keys()):
        taken = 0
        for c in by_industry[ind]:
            if taken >= effective_industry_floor:
                break
            if _try_add(c, enforce_industry_cap=False):
                taken += 1

    # Round-1b 平台覆盖
    for plat in sorted(by_platform.keys()):
        for c in by_platform[plat]:
            if platform_count.get(plat, 0) >= config.min_urls_per_platform:
                break
            _try_add(c)

    # Round-2 质量填充(全局 score desc, 受 max_per_industry 软上限)
    for c in sorted(candidates, key=_sort_key):
        if len(selected) >= config.budget_max:
            break
        ind = c.get('industry_name') or ''
        if industry_count.get(ind, 0) >= max_per_industry:
            continue
        _try_add(c)

    # 输出: 全局 score desc 排序 + budget_rank
    out = sorted(selected.values(), key=_sort_key)
    for i, c in enumerate(out, start=1):
        c = dict(c)
        c['budget_rank'] = i
        out[i - 1] = c
    return out
