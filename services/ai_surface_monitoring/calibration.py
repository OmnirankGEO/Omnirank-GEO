"""可重放的模型升级检测 / 校准 harness(§7 / 复审 P2-7)。

固定金标准问题集 × 品牌实体集 × 旧/新表面 × 2-3 次重复
  → 传输指标(回答/拒绝/引用域名重合)
  + **重复稳定度**(同条件重复结果一致程度)
  + **准入阈值判定**(采集成功率 ≥ 98% 等,给平台选择用)
  + **品牌提及率 / 推荐名单 Jaccard**(需 AI-2 实体 resolver;通过 entity_judge 钩子注入,
    adapter 层不做品牌判断)。

校准数据只用于**平台选择**,不自动写客户报告或公共榜;记录 judge/问题集版本;不把表面偏差
包装成客户涨跌。品牌/推荐指标缺 entity_judge 时不臆造(留 None)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from urllib.parse import urlsplit

from services.ai_surface_monitoring.contracts import ObservationEnvelopeV1

# entity_judge(env) -> {"mentioned": bool, "recommended_brands": iterable[str]}
#   recommended_brands = 该回答**明确推荐**的品牌集合(不是"提及的全部品牌";复审 P2)。
EntityJudge = Callable[[ObservationEnvelopeV1], dict]


def _registrable_domain(url: str) -> str:
    """近似"可注册域名":去 www 前缀。**限制**(复审 P2):未接公共后缀表(PSL),
    故 ``a.co.uk`` / ``b.co.uk`` 会被当作不同注册域(实为同一二级),``foo.github.io`` 也不归并。
    仅用于校准的**表面偏差对照**(平台选择),不作公共聚合口径;精确注册域归并属 AI-2/AI-3 隐私层。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _jaccard(a: set, b: set) -> float:
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


@dataclass
class _SurfaceAgg:
    surface_key: str
    total: int = 0
    answered: int = 0
    refused: int = 0
    unknown_or_error: int = 0
    with_citations: int = 0
    citation_domains: set = field(default_factory=set)
    total_latency_ms: int = 0
    total_cost_micros: int = 0
    cost_known_count: int = 0
    # 每 question 的重复分组:question -> {"statuses": [...], "rec_sets": [frozenset,...], "mentioned": [bool,...]}
    per_question: Dict[str, dict] = field(default_factory=dict)

    def add(self, question: str, env: ObservationEnvelopeV1, judged: Optional[dict]) -> None:
        self.total += 1
        if env.response_status == "answered":
            self.answered += 1
        elif env.response_status == "refused":
            self.refused += 1
        elif env.response_status in ("unknown", "error", "timeout"):
            self.unknown_or_error += 1
        if any(c.source_type == "citation" for c in env.citations):
            self.with_citations += 1
        for c in env.citations:
            d = _registrable_domain(c.url)
            if d:
                self.citation_domains.add(d)
        self.total_latency_ms += env.latency_ms
        if env.usage.estimated_cost_micros is not None:
            self.total_cost_micros += env.usage.estimated_cost_micros
            self.cost_known_count += 1
        q = self.per_question.setdefault(question, {"statuses": [], "rec_sets": [], "mentioned": []})
        q["statuses"].append(env.response_status)
        if judged is not None:
            # 复审 P2:用 judge 明确给出的 recommended_brands 集合,不是"提及即算推荐"
            q["rec_sets"].append(frozenset(str(b) for b in (judged.get("recommended_brands") or [])))
            q["mentioned"].append(bool(judged.get("mentioned")))

    def repeat_stability_bps(self) -> int:
        """每 question 的众数一致比例,再对 question 取平均(0..10000)。"""
        if not self.per_question:
            return 0
        fracs = []
        for q in self.per_question.values():
            st = q["statuses"]
            if not st:
                continue
            modal = max(st.count(x) for x in set(st))
            fracs.append(modal / len(st))
        return round((sum(fracs) / len(fracs)) * 10000) if fracs else 0

    def recommendation_jaccard(self) -> Optional[float]:
        """有 entity_judge 时:每 question 重复间推荐名单的平均两两 Jaccard(稳定度)。"""
        pairs = []
        for q in self.per_question.values():
            sets = q["rec_sets"]
            if len(sets) < 2:
                continue
            for i in range(len(sets)):
                for j in range(i + 1, len(sets)):
                    pairs.append(_jaccard(set(sets[i]), set(sets[j])))
        if not pairs:
            return None
        return round(sum(pairs) / len(pairs), 4)

    def brand_mention_rate_bps(self) -> Optional[int]:
        flags = [m for q in self.per_question.values() for m in q["mentioned"]]
        if not flags:
            return None
        return round(sum(1 for m in flags if m) / len(flags) * 10000)

    def summary(self, has_judge: bool) -> Dict[str, object]:
        n = max(self.total, 1)
        out = {
            "surface_key": self.surface_key,
            "total": self.total,
            "answer_rate_bps": round(self.answered / n * 10000),
            "refusal_rate_bps": round(self.refused / n * 10000),
            "unknown_error_rate_bps": round(self.unknown_or_error / n * 10000),
            "citation_rate_bps": round(self.with_citations / n * 10000),
            "distinct_citation_domains": len(self.citation_domains),
            "avg_latency_ms": round(self.total_latency_ms / n),
            "avg_cost_micros_known": (round(self.total_cost_micros / self.cost_known_count)
                                      if self.cost_known_count else None),
            "cost_known_ratio_bps": round(self.cost_known_count / n * 10000),
            "repeat_stability_bps": self.repeat_stability_bps(),
            # 品牌/推荐指标:仅有 entity_judge 时给出,否则 None(不臆造)
            "brand_mention_rate_bps": self.brand_mention_rate_bps() if has_judge else None,
            "recommendation_jaccard": self.recommendation_jaccard() if has_judge else None,
        }
        return out


@dataclass
class AdmissionThresholds:
    """平台进入核心矩阵的建议阈值(§7)。"""

    min_answer_rate_bps: int = 9800          # 采集成功率 >= 98%
    min_repeat_stability_bps: int = 8000     # 重复稳定度下界
    max_unknown_error_rate_bps: int = 500    # 未知/错误上界


@dataclass
class CalibrationReport:
    judge_version: str
    question_set_version: str
    per_surface: Dict[str, Dict[str, object]]
    domain_overlap_jaccard: Dict[str, float]
    admission: Dict[str, dict]  # surface -> {passes, reasons}

    def to_dict(self) -> Dict[str, object]:
        return {
            "judge_version": self.judge_version,
            "question_set_version": self.question_set_version,
            "per_surface": self.per_surface,
            "citation_domain_overlap_jaccard": self.domain_overlap_jaccard,
            "admission": self.admission,
        }


def evaluate_admission(surface_summary: Dict[str, object], thresholds: AdmissionThresholds) -> dict:
    reasons: List[str] = []
    if surface_summary["answer_rate_bps"] < thresholds.min_answer_rate_bps:
        reasons.append(f"answer_rate {surface_summary['answer_rate_bps']} < {thresholds.min_answer_rate_bps}")
    if surface_summary["repeat_stability_bps"] < thresholds.min_repeat_stability_bps:
        reasons.append(f"repeat_stability {surface_summary['repeat_stability_bps']} < {thresholds.min_repeat_stability_bps}")
    if surface_summary["unknown_error_rate_bps"] > thresholds.max_unknown_error_rate_bps:
        reasons.append(f"unknown_error_rate {surface_summary['unknown_error_rate_bps']} > {thresholds.max_unknown_error_rate_bps}")
    return {"passes": not reasons, "reasons": reasons}


class CalibrationHarness:
    """跑一组表面 × 金标准问题 × 重复的采集,产出确定性对照指标 + 准入判定。

    ``collect`` 为 ``async (surface_key, question_text, run_index) -> ObservationEnvelopeV1``,由调用方注入。
    ``entity_judge`` 为可选品牌判定钩子(需 AI-2 resolver);缺省则品牌/推荐指标为 None(不臆造)。
    """

    def __init__(
        self,
        collect: Callable,
        *,
        judge_version: str = "calib-v1",
        question_set_version: str = "gold-v1",
        repeats: int = 2,
        entity_judge: Optional[EntityJudge] = None,
        thresholds: Optional[AdmissionThresholds] = None,
    ):
        self._collect = collect
        self.judge_version = judge_version
        self.question_set_version = question_set_version
        self.repeats = max(repeats, 1)
        self._entity_judge = entity_judge
        self._thresholds = thresholds or AdmissionThresholds()

    async def run(self, surfaces: List[str], gold_questions: List[str]) -> CalibrationReport:
        aggs: Dict[str, _SurfaceAgg] = {sk: _SurfaceAgg(sk) for sk in surfaces}
        for sk in surfaces:
            for q in gold_questions:
                for r in range(self.repeats):
                    env = await self._collect(sk, q, r + 1)
                    judged = self._entity_judge(env) if self._entity_judge is not None else None
                    aggs[sk].add(q, env, judged)

        has_judge = self._entity_judge is not None
        per_surface = {sk: a.summary(has_judge) for sk, a in aggs.items()}
        admission = {sk: evaluate_admission(per_surface[sk], self._thresholds) for sk in surfaces}
        overlap: Dict[str, float] = {}
        for i in range(len(surfaces)):
            for j in range(i + 1, len(surfaces)):
                a, b = surfaces[i], surfaces[j]
                overlap[f"{a}|{b}"] = round(_jaccard(aggs[a].citation_domains, aggs[b].citation_domains), 4)

        return CalibrationReport(
            judge_version=self.judge_version,
            question_set_version=self.question_set_version,
            per_surface=per_surface,
            domain_overlap_jaccard=overlap,
            admission=admission,
        )
