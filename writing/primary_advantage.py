"""D6-A · 每篇一个主优势 + 搜索素材真正参与推荐写作(接线层)。

[最终接管工单 §6D/§6E/§8 · 2026-08-10]

## 这个模块解决什么

Gate-2 实测的病是「AI 引用了我们的文章,但不推荐我们的客户」——文章过了
第一关(被搜到)却过不了第二关(被推荐),产出「可信但不推荐」的中性说明文。
evidence-to-article-gap 考古(2026-08-09)进一步定位:1517 素材里 14 处硬事实
正文命中 0 —— **素材没走到正文,不是写法问题**。

所以 D6-A 做四件事,全部接在现役生成主链上,不新建任何表:

  1. 从客户真实事实(`brand_fact_snapshot.claims` + 蒸馏卖点)生成候选优势;
  2. 按「问题相关性 × 客户事实支持度 × 可讲清程度」**确定性**排序;
  3. 对缺证据的头部候选产出定向搜索增援 query(喂给现役
     `collect_evidence_pack`,搜索失败不阻断主链);
  4. 把候选、素材引用与排序理由注入 prompt,由**写作 LLM** 从真实素材中
     选定本篇唯一主优势;lineage 留痕(零 DDL,落 generation_request_snapshot)。

## 红线(工单 §6D 逐条)

- 每篇只打透**一个**主优势;同一优势对多个问题最相关时**允许复用**;
- **禁止**为凑矩阵强制轮换成更弱优势 —— 本模块**无任何轮换状态**:
  排序纯由本篇输入决定,同输入必同输出(测试锁死);
- 优势矩阵只做观察和运营提示,不是配额;
- 候选只从客户真实事实里长出来,不编造;缺证据走增援或收短,不填充。

## D6-B 预留

lineage 负载里预留 `reco_feedback` / `engine_recognition_state` 两个关联字段
(本包恒 None)。D6-B 按「客户 × 问题 × 引擎 × 时间窗」区分六态并回喂选题/
搜索/写作,另包实现;旧 `recommendation_signal_block`(按行业聚合 45 天高频
理由,不结合当前问题/引擎/客户状态)已判废,本包不移植。
"""
from __future__ import annotations

import os
import re
from typing import Any, Final, Sequence

PRIMARY_ADVANTAGE_VERSION: Final = "primary-advantage-v1.1"

#: 进 prompt 的候选上限。太少没得选,太多模型平均用力(与主优势唯一性目标相反)。
CANDIDATE_LIMIT: Final = 6
#: 定向搜索增援 query 上限 —— 挂在现役 collect_evidence_pack 的 query 预算上,
#: 不能把主题检索挤掉。
SEARCH_HINT_LIMIT: Final = 2

#: 泛化空话 —— 不能作为「主优势」候选(说不清、无法核验、AI 也复述不出来)。
_VAGUE_PHRASE = re.compile(
    r"实力雄厚|行业领先|经验丰富|丰富经验|一站式|全方位|广泛认可|众多客户|"
    r"深受(?:客户|用户)|口碑(?:良好|好)|值得信赖|品质卓越|精益求精|追求卓越"
)

#: 带单位的具体数值 —— 「可讲清程度」的强信号(工况/规格/数量/时间)。
_SPEC_NUMBER = re.compile(
    r"\d+(?:\.\d+)?\s*"
    r"(?:mm|cm|km|kg|m|t|吨|米|毫米|厘米|公里|平方米|㎡|层|台|套|个|人|家|项|"
    r"小时|天|周|月|年|%|％|万元|元|度|℃|Pa|kW|V|A|Hz)"
)

#: 事实字段 → 人话方向词。只用于给候选起可读的方向名,不是评价维度表。
_FIELD_HINT: Final[dict[str, str]] = {
    "core_selling_points": "核心卖点",
    "service_scope": "服务范围",
    "delivery_capability": "交付能力",
    "qualifications": "资质",
    "service_cases": "案例",
    "after_sales": "售后",
    "team_scale": "团队",
    "years_in_business": "经营年限",
}

_CJK = re.compile(r"[一-鿿 a-zA-Z0-9]+")

# ---------------------------------------------------------------------------
# [P0-2 2026-08-14] 结构化取值 + 形态闸(根因 B:repr 污染 33/35 篇)
#
# 研究定稿实证:`brand_fact_snapshot.claims[].value` 为 dict/嵌套结构时,旧代码
# `[str(v) for v in value]` 把 Python repr(`{'name': '…', 'desc': '…'}`)原样
# 送进候选 → planned_primary_advantage 带着大括号进 prompt 与 lineage(35 篇
# 抽样 33 篇污染)。修法两层:
#   1. **结构化取值**:dict 只取已知文本键,永不 repr;
#   2. **形态闸**:repr/JSON 形态的短语一律拒进候选(变异点:拆闸 → 泄漏样本转红)。
# ---------------------------------------------------------------------------
#: dict 型 claim value 里可作为优势文本的键(按优先序取第一个非空)。
_ATOM_TEXT_KEYS: Final = ("advantage", "claim", "text", "value", "description", "desc", "content", "name", "title")
#: repr / JSON 形态特征:大括号、方括号包裹、`'k':` / `"k":` 键值形态。
_MALFORMED_SHAPE = re.compile(r"[{}\[\]]|['\"]\s*[\w一-鿿]+\s*['\"]\s*[:：]")


def _value_texts(value: Any, *, _depth: int = 0) -> list[str]:
    """从 claim value 里**结构化**取出候选文本段,永不产生 repr。"""
    if _depth > 3:
        return []
    if isinstance(value, str):
        return re.split(r"[;；\n]|(?<=[。！？])", value)
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, dict):
        out: list[str] = []
        for key in _ATOM_TEXT_KEYS:
            out.extend(_value_texts(value.get(key), _depth=_depth + 1))
        return out  # 只认已知文本键,未知键不倒出来(不 repr 是底线)
    if isinstance(value, (list, tuple)):
        out = []
        for entry in value:
            out.extend(_value_texts(entry, _depth=_depth + 1))
        return out
    return []


def is_malformed_advantage(phrase: str) -> bool:
    """形态闸:repr/JSON 形态短语禁入候选(该 lane 跳过,不阻断整篇 —— O1 式降级)。"""
    return bool(_MALFORMED_SHAPE.search(str(phrase or "")))


#: [P0-2] relevance 最小门槛 —— **机制先建全,数值走配置**(工单 §四):
#: 默认 0.0 = 不比现状激进(现状无下限);Owner 拍板后改环境变量开闸。
#: 低于门槛的候选**不删除**:标 low_relevance 降为辅证(A1 —— 局部提示 +
#: 换优势/补资料/保持继续三出口,见 frontend_state_copy.md §3),不做硬阻断。
def min_relevance_threshold() -> float:
    try:
        value = float(os.getenv("GEO_ADVANTAGE_MIN_RELEVANCE", "0"))
    except (TypeError, ValueError):
        value = 0.0
    return max(0.0, min(1.0, value))


def _bigrams(text: str) -> set[str]:
    joined = "".join(_CJK.findall(str(text or "")))
    return {joined[i:i + 2] for i in range(len(joined) - 1)} if len(joined) > 1 else set()


def _overlap(cand: set[str], target: set[str]) -> float:
    if not cand or not target:
        return 0.0
    return len(cand & target) / max(1, min(len(cand), len(target)))


def _relevance(candidate: str, question: str, keyword: str) -> float:
    """候选优势与本篇问题的相关性(0..1,确定性,无 LLM 成本)。

    🔴 关键词通道与问题通道**分开算再加权**,不做并集:并集会让题面泛词
    (行业名/品类名,每个候选都沾边)淹没意图词 —— 实测「售后响应哪家靠谱」
    的问题把交付优势排到了售后优势前面,就是这个病。关键词是选题链下发的
    意图浓缩,权重不低于整句问题。
    """
    cand = _bigrams(candidate)
    return 0.5 * _overlap(cand, _bigrams(keyword)) + 0.5 * _overlap(cand, _bigrams(question))


def _clarity(candidate: str) -> float:
    """可讲清程度:有具体数值/场景词的优势,AI 才复述得出来。"""
    score = 0.4
    if _SPEC_NUMBER.search(candidate):
        score += 0.4
    if re.search(r"适合|适用|场景|以内|以下|以上|范围|定制|周期|响应", candidate):
        score += 0.2
    return min(score, 1.0)


def _norm_phrase(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().strip("。;；,，")


def candidate_advantages(
    brand_fact_snapshot: dict[str, Any] | None,
    *,
    selling_points: str = "",
) -> list[dict[str, str]]:
    """从客户真实事实里生成候选优势短语。**只取真实存在的输入,不生成新内容。**

    Returns:
        ``[{"advantage": 短语, "field": 来源字段, "fact_ref": BF-xxx 或 ""}, ...]``
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def _push(phrase: str, field: str, fact_ref: str, provenance: str = "") -> None:
        phrase = _norm_phrase(phrase)
        # 6~80 字:太短说不清,太长不是"一句话能说完"的主优势。
        if not (6 <= len(phrase) <= 80):
            return
        # [P0-2] 形态闸:repr/JSON 形态一律拒 —— 结构化取值(_value_texts)已把
        # dict 的正路走通,还长这样的只能是坏数据,进 prompt 就是 33/35 那个病。
        if is_malformed_advantage(phrase):
            return
        if _VAGUE_PHRASE.search(phrase) and not _SPEC_NUMBER.search(phrase):
            return  # 纯空话不进候选;带具体数值的句子即使含套话也保留
        if phrase in seen:
            return
        seen.add(phrase)
        out.append({
            "advantage": phrase,
            "field": field,
            "fact_ref": fact_ref,
            # [P0-2] AdvantageAtom 结构化补齐:方向词(人话)+ 事实出处 ——
            # 复用 brand_fact_snapshot.claims 已有的 provenance,不造第二套口径。
            "why": _FIELD_HINT.get(field, ""),
            "provenance": provenance,
        })

    snapshot = brand_fact_snapshot if isinstance(brand_fact_snapshot, dict) else {}
    for claim in snapshot.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        field = str(claim.get("field") or "")
        ref = str(claim.get("claim_id") or "")
        prov = str(claim.get("provenance") or "")
        # [P0-2] 结构化取值替换 `[str(v) for v in value]`:dict/嵌套只取文本键,
        # 永不 repr(根因 B 主犯就在这一行的旧写法)。
        for part in _value_texts(claim.get("value")):
            _push(part, field, ref, prov)

    for part in re.split(r"[;；\n]|(?<=[。！？])", str(selling_points or "")):
        _push(part, "selling_points", "", "customer_provided")
    return out


def _evidence_refs(candidate: str, evidence_pack: dict[str, Any] | None) -> list[str]:
    """候选优势在 Evidence Pack 里的支持条目(按二元组重叠判定,确定性)。

    🔴 [返修 R6 2026-08-11] 计分只认**明确 `support`** 关系的条目:
    旧版只排除 refute,`background`(行业背景)也给客户优势抬分 ——
    一条通用行业背景可以把与它词面重叠的弱优势顶上去,选优势被噪音带偏。
    background 仍留在 prompt 素材里(写作可用),只是**不计入排序分**。
    🔴 防过度矫正(§0 裁决一):零外部 support 的纯客户事实优势**仍是合法
    候选、仍可被选中**(客户事实支持度分由 fact_ref 承担,不依赖本函数);
    本函数只决定"外部素材加分",不决定"入场资格"。
    """
    if not isinstance(evidence_pack, dict):
        return []
    cand = _bigrams(candidate)
    if not cand:
        return []
    from writing.evidence_pack import iter_entity_admissible_items

    refs: list[str] = []
    # [P0-2/R2-1/R5 · H0-ENTITY-BINDING] 评分同轴:R5 起统一走唯一消费 API
    # (错实体/缺绑定/无标记无轴条目撑我方优势 = 错实体污染,一律不计)。
    for item in iter_entity_admissible_items(evidence_pack):
        if str(item.get("relationship") or "") != "support":
            continue  # 只有明确支持关系才计分(refute 是 D4 红线,background 是 R6)
        hay = _bigrams(f"{item.get('title') or ''} {item.get('claim') or ''} {item.get('excerpt') or ''}")
        overlap = len(cand & hay) / max(1, len(cand))
        if overlap >= 0.25:
            eid = str(item.get("evidence_id") or "").strip()
            if eid:
                refs.append(eid)
    return refs[:5]


def rank_candidates(
    candidates: Sequence[dict[str, str]],
    *,
    question: str,
    keyword: str = "",
    evidence_pack: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """按「问题相关性 × 客户事实支持度 × 可讲清程度」排序(工单 §6D 原文次序)。

    🔴 无轮换状态:本函数不读取任何历史使用记录,同输入必同输出。
    「同一优势对多个问题最相关时允许复用」由此天然成立;
    「不为凑矩阵轮换成更弱优势」由此天然成立(测试双向锁死)。
    """
    ranked: list[dict[str, Any]] = []
    for cand in candidates:
        adv = str(cand.get("advantage") or "")
        refs = _evidence_refs(adv, evidence_pack)
        rel = _relevance(adv, question, keyword)
        support = 0.5 + (0.5 if cand.get("fact_ref") else 0.0)
        support = min(1.0, support + 0.2 * len(refs))
        clarity = _clarity(adv)
        score = round(rel * 0.5 + support * 0.25 + clarity * 0.25, 4)
        ranked.append({
            "advantage": adv,
            "field": cand.get("field") or "",
            "fact_ref": cand.get("fact_ref") or "",
            "why": cand.get("why") or "",
            "provenance": cand.get("provenance") or "",
            "evidence_refs": refs,
            "relevance": round(rel, 4),
            "support": round(support, 4),
            "clarity": round(clarity, 4),
            "score": score,
            # [P0-2] relevance 门槛:低于阈值只**标记**不删除(A1 —— 降辅证 +
            # 出口卡),默认阈值 0.0 = 行为与现状逐字节一致(工单 §四 安全侧)。
            "low_relevance": bool(rel < min_relevance_threshold()),
            "reason": f"相关性{rel:.2f}·支持度{support:.2f}·可讲清{clarity:.2f}",
        })
    # 分数并列时按候选文本排序 —— 保证确定性,不引入位置轮换。
    ranked.sort(key=lambda item: (-item["score"], item["advantage"]))
    return ranked[:CANDIDATE_LIMIT]


def advantage_search_hints(
    ranked: Sequence[dict[str, Any]],
    *,
    keyword: str = "",
    limit: int = SEARCH_HINT_LIMIT,
) -> list[str]:
    """给缺公开证据的头部候选产出定向增援 query(工单 §8 第 3 步)。

    只出 query 文本,不自己发搜索 —— 搜索仍走现役 `collect_evidence_pack`
    (它带预算、缓存与失败兜底;搜索失败不阻断主链是它既有的契约)。
    🔴 query 只带优势方向词与关键词,不带客户单方材料原文(隐私边界与
    collect_evidence_pack 的既有纪律一致)。
    """
    hints: list[str] = []
    for item in ranked:
        if len(hints) >= max(0, int(limit)):
            break
        if item.get("evidence_refs"):
            continue  # 已有支持素材,不占增援预算
        adv = str(item.get("advantage") or "")
        core = _norm_phrase(re.sub(_SPEC_NUMBER, " ", adv))[:24]
        if len(core) < 4:
            continue
        hints.append(f"{core} {keyword}".strip())
    return hints


def build_primary_advantage_block(
    brand_name: str,
    question: str,
    ranked: Sequence[dict[str, Any]],
) -> str:
    """注入 system_prompt 的主优势选择块。没有候选时返回空串(不注入空块)。

    选择权交写作 LLM(工单 §8 第 4 步):这里给的是真实素材与排序参考,
    不是指定答案 —— 模型可以在候选里选更贴合正文走向的一条,但必须只选一条。
    """
    brand = str(brand_name or "").strip()
    candidates = [item for item in ranked if str(item.get("advantage") or "").strip()]
    # [P0-2] relevance 门槛:低相关候选降为**辅证**(仍下发但不占主优势候选位),
    # 全军覆没时保底放行原候选 —— 门槛永远不能把主优势块整个饿死(D8 零阻断)。
    top = [item for item in candidates if not item.get("low_relevance")]
    aux = [item for item in candidates if item.get("low_relevance")]
    if not top and aux:
        top, aux = aux, []
    if not brand or not top:
        return ""
    lines = [
        f"【本篇主优势 {PRIMARY_ADVANTAGE_VERSION}】",
        f"围绕本篇问题「{str(question or '').strip()}」,从下面**客户真实事实**里"
        f"选**一条**作为 {brand} 的本篇主优势(按相关性排序,供参考):",
    ]
    for idx, item in enumerate(top, 1):
        refs = "、".join(item.get("evidence_refs") or []) or "暂无公开素材"
        lines.append(
            f"{idx}. {item['advantage']}({item['reason']};支持素材:{refs})"
        )
    if aux:
        lines.append(
            "以下候选与本篇问题相关性偏低,**不作主优势**,只可作次要支撑(如与正文无关则不用):"
        )
        for item in aux:
            lines.append(f"- {item['advantage']}({item['reason']})")
    lines.extend([
        "",
        "🔴 选择与写作规则:",
        "- **只选一条**与本篇问题最相关、客户事实撑得住、一句话能说清的主优势,"
        "整篇围绕它打透:开头点出、正文用具体事实与来源展开、结论回扣;",
        "- 其余候选可作次要支撑,但**不许**平均用力写成「样样都好」;",
        "- 🔴 **不许**为了和客户其他文章「换着写」而选一条更弱的优势 —— "
        "同一优势对多个问题都最相关时,重复选它是**正确**的;",
        "- 候选素材不足以支撑时,按降级阶梯收短表述,**不编造**数字、案例或资质;",
        "- 「支持素材」给出的是 Evidence Pack 条目编号:写作时使用**那条素材的"
        "来源方 + 日期**做归属,编号本身不进正文。",
    ])
    return "\n".join(lines)


def lineage_payload(
    ranked: Sequence[dict[str, Any]],
    *,
    question: str,
    style_code: str = "",
    family_code: str = "",
    spec_version: str = "",
    strategy_version: str = "",
    target_engine: str = "",
    search_hints: Sequence[str] = (),
    search_hints_planned: Sequence[str] = (),
    injected: bool = False,
    reco_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """落 `generation_request_snapshot` 的留痕负载(零 DDL,工单 §8 字段逐一对齐)。

    `target_engine`:生成时没有明确目标引擎就传空串,**如实记录,不硬编**
    (工单 §8 原文)。`selected` 是模型在 prompt 内自选,系统侧记录的是
    「计划主优势 = 排序第一」与完整候选;发布后真实效果回查归 D6-B。

    🔴 [返工 R3 2026-08-11] `search_hints` 语义收紧为**实际执行**的增援
    (调用方从 evidence pack 落盘的 queries 数组回填);计划值走
    `search_hints_planned` 单列 —— 留痕只许记实际发生的,计划≠执行。
    """
    top = ranked[0] if ranked else None
    return {
        "version": PRIMARY_ADVANTAGE_VERSION,
        "family_id": str(family_code or ""),
        "style_id": str(style_code or ""),
        "spec_version": str(spec_version or ""),
        "strategy_version": str(strategy_version or ""),
        "target_question": str(question or ""),
        "target_engine": str(target_engine or ""),
        "planned_primary_advantage": (top or {}).get("advantage") or "",
        "planned_reason": (top or {}).get("reason") or "",
        "candidates": [dict(item) for item in ranked],
        "search_hints": list(search_hints),                  # 实际执行(R3)
        "search_hints_planned": list(search_hints_planned),  # 计划(可与执行对账)
        "selection": "llm_from_prompt_block",
        "injected": bool(injected),
        # ---- [P1-5b 2026-08-14] D6-B 回流接通:预留字段由
        # services/reco_outcome_feedback.reco_feedback_for_lineage 供给。
        # 样本不足 → None(与预留期行为一致 = 诚实"还没有数据",不硬凑)。----
        "reco_feedback": (dict(reco_feedback) if isinstance(reco_feedback, dict) else None),
        "engine_recognition_state": (
            (reco_feedback or {}).get("engine_recognition_state")
            if isinstance(reco_feedback, dict) else None
        ),
    }
