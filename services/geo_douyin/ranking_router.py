"""榜单形态路由 · 十母版族 + 面×行业默认版式 + 有据/无据两半式

## 三层决策,顺序不能换

```
① 有没有据(客户能不能拿到实名次)  → 决定 form(能不能叫"榜单")
② 面 × 行业                        → 决定默认版式(不是开关行业)
③ 候选证据的形状                    → 决定 template_id(十选一)
```

## 🔴 ① 两半式:治理 SSOT D12④ 客户存在感三铁律

> 客户品牌必须在**正文前 15% 与结论段**出现;永不出现对客户的负面定性;
> 对比/榜单类**客户证据卡信息量不得低于竞品**,**榜单内必须有有依据的实质位次**。

| 分支 | 条件 | form | 能不能叫"榜单" |
|---|---|---|---|
| **有据** | 客户在 `answer_entities` 有 ≥1 引擎实名次,或 `client_position.best_rank` 有值 | `ranking` | ✅ |
| **无据** | 查无位次(生产实测:262 个真客户里只有 6 个能精确命中自己) | `scenario_pick` / `selection_matrix` | ❌ **必须改名** |

**禁**:挂"排行榜"的名把客户放榜外;**禁**:给客户塞一个没依据的名次。

## 🔴 ② 不整关行业 —— 换默认版式,不是关开关

Owner 亲裁治理 SSOT D12② 是**文体按类目分治**,不是"某些行业禁做榜单";
而且 D12 v2.4 的唯一成功案例恰是**家装 TOP10 文章**。所以家装不能关。

抖音 caption 端的默认版式按实测走(而实测只覆盖 caption 面 × 豆包,见工单 §1.3):

| 行业 | 默认 | 实测依据 |
|---|---|---|
| `home_improvement` | **避坑/场景版式** | 自采 榜单 −9.1pp(n=161);`card_templates.py:50` 避坑家装 **+11.9** |
| `finance` | 避坑/场景版式 | 自采 −5.1pp |
| `geo_优化服务` / `auto` / `retail_ecommerce` / `education` | 榜单型 | 自采 +15.3 / +8.3 / +7.4 / +7.3 |
| 其余 | 避坑式兜底 | `card_templates.DEFAULT_HOOK` 是唯一跨层都正的那个 |

**这些是默认值,不是判废闸** —— 全部允许手动覆盖,并按行业做 A/B
(工单 §1.4 三条硬约束:可覆盖 / 不进判废闸 / 验收只卡"默认值已接线")。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final, Optional, Sequence

from services.geo_douyin.ranking_payload import (
    ENTITY_COUNT_MIN,
    FORM_MATRIX,
    FORM_RANKING,
    FORM_SCENARIO,
    safe_merge_key,
)

logger = logging.getLogger("GEO-Douyin-Ranking")

RANKING_VISUAL_SPEC: Final = "geo-ranking-visual-v1.0"


# ===========================================================================
# 发布面 · 服务端冻结(P1-2)
# ===========================================================================
#
# 🔴 **不接受前端传值**。目标面是产品事实,不是用户选项 ——
#    「制作 GEO 图文」只服务抖音图文发布,而抖音图文实际只有豆包会引用。
#
# 生产实证(2026-08-07 只读通道现取,两条独立信源互相印证):
#
#   来源信号 `geo_research_source_signals`(域名命中 douyin):
#     豆包 5,610 行 = 该引擎全部来源的 **20.97%**
#     DeepSeek 96 = 0.40% · 千问 92 = 0.48% · **Kimi 0 = 0.00%**
#   答案引用 `geo_research_answer_facts.citation_urls`:
#     豆包 8,494 / 35,265 = **24.09%** · DeepSeek 0.37% · 千问 0.45% · Kimi 1 条
#   各引擎最常引域名 Top5:豆包第一名就是 `iesdouyin.com`(5,596);
#     其余三家的 Top5 里**一个抖音域都没有**。
#
# 🔴 这条**只管发布面**(谁会引用我们发出去的内容),
#    **不收窄证据面**(哪些引擎的回答证明某家该上榜)—— 见 `ranking_source` 的说明。
#    两者是不同的轴;混成一个会把跨引擎共识("N 个 AI 都提到")整条删掉,
#    并撞上 `MIN_ENGINE_CONSENSUS = 2` → 候选恒空 → 功能恒降级。

#: 目标引擎(规范名)。**必须是 `_normalize_engine` 归一后的那个写法** ——
#: `geo_research_answer_entities.engine` 存中文,`geo_research_source_signals.engine`
#: 存小写拉丁,写死任一侧的字面量都会在另一侧命中 0 行且不报错。
TARGET_ENGINE: Final = "豆包"
TARGET_SURFACE: Final = "douyin_image_post"


def target_face() -> dict[str, str]:
    """发布面快照。落 `generation_meta`,再次创作从这里继承。"""
    return {"target_engine": TARGET_ENGINE, "target_surface": TARGET_SURFACE}


@dataclass(frozen=True)
class RankingRequest:
    """**用户点的那一份**(而不是最终做成的那一份)。

    🔴 P1-3 的落点:再次创作从这个快照继承,**不靠客户端重新拼参数**。
       上一版 `RegenerateRequest` 只收 `style_key` / `extra_hint`,
       服务端也没从库里取回榜单参数 —— 于是一条付费的「再次创作」
       会把榜单作品做成普通图文,而且全程不报错。

    🔴 `entity_count` 存的是**用户要几家**,不是最终冻结了几家。
       存结果会让"这次只凑到 4 家"在下一次重做时变成"他要 4 家",一路缩水。
    """
    content_form: str = FORM_RANKING
    entity_count: int = 0
    template: str = ""
    force_ranking: bool = False
    target_engine: str = TARGET_ENGINE
    target_surface: str = TARGET_SURFACE

    def to_dict(self) -> dict:
        return {
            "content_form": self.content_form,
            "entity_count": int(self.entity_count or 0),
            "template": self.template,
            "force_ranking": bool(self.force_ranking),
            "target_engine": self.target_engine,
            "target_surface": self.target_surface,
        }

    @classmethod
    def from_meta(cls, meta: Optional[dict]) -> Optional["RankingRequest"]:
        """从 `generation_meta.ranking.request` 还原。**没有就返回 None** ——
        卡组型作品本来就没有这一块,不许凭空造一个默认榜单请求出来。"""
        block = ((meta or {}).get("ranking") or {}).get("request")
        if not isinstance(block, dict) or not block:
            return None
        if str(block.get("content_form") or "").strip() != FORM_RANKING:
            return None
        return cls(
            content_form=FORM_RANKING,
            entity_count=int(block.get("entity_count") or 0),
            template=str(block.get("template") or ""),
            force_ranking=bool(block.get("force_ranking")),
            # 🔴 目标面**取当前服务端常量**,不取快照里的旧值:
            #    它是产品事实不是用户选项,改了就该对存量作品一起生效。
            target_engine=TARGET_ENGINE,
            target_surface=TARGET_SURFACE,
        )


# ===========================================================================
# 十母版族(规范 §6:十种**购买决策结构**,不是十套行业皮肤)
# ===========================================================================

@dataclass(frozen=True)
class RankingTemplate:
    template_id: str
    label: str
    when: str                 # 触发条件(人话)
    composition: str          # 只补进生图 prompt 的 Composition/Style 段
    min_entities: int = 3
    max_entities: int = 6


TEMPLATES: Final[dict[str, RankingTemplate]] = {
    "industrial_overview": RankingTemplate(
        "industrial_overview", "工业设备综合榜",
        "多候选 + 多综合维度,需要快速总览",
        "Procurement-oriented comprehensive ranking with a strong industrial hero image. "
        "3-5 ranked rows; each row = rank, company, best-fit scenario, one evidence-backed reason. "
        "Engineering photography, navy/steel/white palette, restrained orange highlights. "
        "Do not show scores unless the frozen payload contains a real traceable score."),
    "tech_spec_matrix": RankingTemplate(
        "tech_spec_matrix", "技术参数对比榜",
        "候选有可比的真实技术字段,用户关心参数边界",
        "Clean technical comparison matrix. Columns come only from dimensions in the frozen payload. "
        "Highlight differences and application boundaries instead of declaring a universal winner. "
        "Never invent units, standards, capacity or certifications."),
    "scenario_fit": RankingTemplate(
        "scenario_fit", "商务场景适配榜",
        "不同客户场景对应不同最优选择",
        "Divide the page into buyer scenarios. Map one frozen recommendation to each scenario. "
        "Make the scenario label visually stronger than the rank. Calm executive palette."),
    "top3_provider": RankingTemplate(
        "top3_provider", "TOP3 服务商榜",
        "只有 3 家证据充分,需要快速给结论",
        "Strong headline plus three large ranked provider panels. Each provider gets one "
        "'best for' line and one short reason. Do not add a fourth provider; do not create "
        "fake popularity, review or market-share data.",
        min_entities=3, max_entities=3),
    "investment_stage": RankingTemplate(
        "investment_stage", "投入阶段适配榜",
        "用户处在试水/扩张/体系化等不同阶段",
        "Arrange recommendations along a maturity path: trial, focused rollout, scaled "
        "implementation, long-term operation. Do not invent budgets."),
    "delivery_capability": RankingTemplate(
        "delivery_capability", "交付能力榜",
        "选择关键在实施/上线/服务/运维链路",
        "Center the visual on the delivery chain: discovery, planning, implementation, "
        "acceptance, support. Rank by delivery fit, not marketing slogans. "
        "No fake completion percentages."),
    "collab_mode": RankingTemplate(
        "collab_mode", "合作模式适配榜",
        "项目制/顾问制/陪跑制/代运营等合作方式不同",
        "Compare project-based, consulting, co-pilot and managed-service collaboration modes. "
        "Editorial consulting visuals, not a generic corporate handshake poster."),
    "regional_service": RankingTemplate(
        "regional_service", "区域服务适配榜",
        "城市/门店/区域覆盖是核心决策条件",
        "Abstract regional grid or city nodes, never a precise coverage map unless verified "
        "geodata exists. State that region fit depends on verified service coverage, "
        "not company registration location."),
    "retrofit_upgrade": RankingTemplate(
        "retrofit_upgrade", "改造升级适配榜",
        "既有系统或设备需要改造",
        "Organize by retrofit object or migration target. Realistic before/after scenes "
        "without numerical performance claims. End with assess, plan, retrofit, operate."),
    "enterprise_scale": RankingTemplate(
        "enterprise_scale", "企业规模适配榜",
        "单点试验/多地点/规模化/长期运营差异明显",
        "Four-stage path: single-site trial, multi-site deployment, demanding operations, "
        "long-term management. Do not infer scale from company fame."),
}


#: 绝不出现在给前端的 DTO 里的键 —— 内部 ID / 供应商 / 抽取器版本 / 合同 hash。
#: 🔴 判据不是"我记得没放",是下面那条锁按这张表逐键查 DTO。
_INTERNAL_ONLY_KEYS: Final = (
    "contract_hash", "entity_row_ids", "row_id", "llm_model", "model",
    "extractor_version", "answer_fact_id", "entity_key", "batch_id",
    "cleaning_log", "dropped_candidates", "source",
)


def ranking_summary(generation_meta: Optional[dict]) -> Optional[dict]:
    """给前端的**窄 DTO**:再次创作弹窗要的默认值 + 人话提示。

    🔴 为什么不让前端直接读 `generation_meta.ranking`:那一块里有
       `contract_hash` / `entity_row_ids` / `llm_model` / `extractor_version`
       这类内部字段。窄 DTO 让"前端能拿到什么"变成一个**可被锁住的清单**,
       而不是"我们暂时没渲染它"。

    返回 None = 这条不是榜单作品(卡组型),前端据此什么都不显示。
    """
    meta = generation_meta or {}
    block = meta.get("ranking")
    if not isinstance(block, dict) or not block:
        return None
    req = block.get("request") if isinstance(block.get("request"), dict) else {}
    tpl = str(block.get("template_id") or req.get("template") or "")
    frozen = block.get("frozen") if isinstance(block.get("frozen"), dict) else {}
    gates = [g for g in (meta.get("ranking_gates") or []) if isinstance(g, dict)]
    return {
        # ── 再次创作的默认值(P2-2:默认必须来自快照)──
        "content_form": str(req.get("content_form") or block.get("requested_form") or ""),
        "template": tpl,
        "template_label": (TEMPLATES[tpl].label if tpl in TEMPLATES else ""),
        "entity_count": int(req.get("entity_count") or 0),
        # 实际冻结了几家(可能少于用户要的)——用于"要 6 家、只凑到 4 家"的如实显示
        "entity_count_actual": len(frozen.get("items") or []),
        # ── 人话提示 ──
        "degraded": bool(block.get("degraded")),
        "fallback_notice": block.get("fallback_notice") or None,
        "engine_label": str(block.get("target_engine") or TARGET_ENGINE),
        "gates": [{"gate": str(g.get("gate") or ""),
                   "message": str(g.get("message") or ""),
                   "card_indices": list(g.get("card_indices") or [])}
                  for g in gates if g.get("message")],
    }


def ranking_template_options() -> list[dict[str, str]]:
    """给前端的可选版式清单。**后端是 SSOT,前端不写死一份。**

    🔴 `composition` 不外露 —— 那是进生图 prompt 的英文段,给用户看没有意义,
       而且它是我们的实现细节。只给 key / label / when(人话)。
    """
    return [{"key": t.template_id, "label": t.label, "when": t.when,
             "entity_min": str(t.min_entities), "entity_max": str(t.max_entities)}
            for t in TEMPLATES.values()]


# ===========================================================================
# ① 有据 / 无据
# ===========================================================================

@dataclass(frozen=True)
class EvidenceVerdict:
    has_evidence: bool
    form: str
    client_rank: Optional[int] = None
    client_engine: str = ""
    reason: str = ""


def decide_form(candidates: Sequence[dict], *, client_brand: str = "",
                client_position: Optional[dict] = None) -> EvidenceVerdict:
    """客户有没有据 → 决定形态与能不能叫"榜单"。

    两个证据源任一命中即算有据:
      ① 客户出现在行业候选池里且有实名次(`best_rank`)
      ② `keyword_insights.client_position.best_rank` 有值
    """
    key = safe_merge_key(client_brand) if client_brand else ""
    if key:
        for c in candidates or []:
            if safe_merge_key(str(c.get("entity_name") or "")) != key:
                continue
            rank = c.get("best_rank")
            if rank and int(rank) > 0:
                eng = (c.get("source") or {}).get("engine") or ""
                return EvidenceVerdict(True, FORM_RANKING, int(rank), str(eng),
                                       "客户在行业候选池里有实名次")

    cp = client_position or {}
    best = cp.get("best_rank")
    if best and int(best) > 0:
        return EvidenceVerdict(True, FORM_RANKING, int(best), "",
                               "client_position.best_rank 有值")

    # 无据:切形态**并改名**。候选够多用选型矩阵,否则场景推荐。
    form = FORM_MATRIX if len([c for c in (candidates or [])]) >= 4 else FORM_SCENARIO
    return EvidenceVerdict(
        False, form, None, "",
        "客户查无实质位次 —— 按 D12④ 切换形态并改名,不挂「排行榜」把客户放榜外")


# ===========================================================================
# ② 面 × 行业(默认版式,不是开关)
# ===========================================================================

SURFACE_DOUYIN_CAPTION: Final = "douyin_caption"
SURFACE_ARTICLE: Final = "article"

#: 抖音 caption 面:实测**榜单为负**的行业 → 默认走避坑/场景版式。
#: 🔴 这不是"关掉这些行业的榜单" —— 手动覆盖始终可用,见 `route_template(force_ranking=)`。
_CAPTION_RANKING_NEGATIVE: Final = frozenset({"home_improvement", "finance"})

#: 抖音 caption 面:实测榜单显著为正的行业。
_CAPTION_RANKING_POSITIVE: Final = frozenset({
    "geo_优化服务", "auto", "retail_ecommerce", "education", "文娱游戏",
})


def default_prefers_ranking(industry_key: str, *,
                            surface: str = SURFACE_DOUYIN_CAPTION) -> bool:
    """这个面 × 这个行业,**默认**要不要走榜单型版式。

    文章端不归本模块管(按 D12② 类目分治,本单不改文章链)——
    传 `SURFACE_ARTICLE` 一律返回 True 以免本模块意外收窄文章端。
    """
    if surface != SURFACE_DOUYIN_CAPTION:
        return True
    ind = str(industry_key or "").strip()
    if ind in _CAPTION_RANKING_NEGATIVE:
        return False
    return ind in _CAPTION_RANKING_POSITIVE


# ===========================================================================
# ③ 选 template_id
# ===========================================================================

# ── ④ 降级不许静默换货(2026-08-06 二次返工)────────────────────────────
#
# 🔴 上一版 `build_ranking_plan()` 返 None 就直接退回卡组型,**只写一行日志**。
#    用户点的是「做榜单」,拿到的是卡组 —— 而且没有任何一处告诉他这件事,
#    更没有给他"补充候选"或"就用这版"的选择。这是**静默换货**。
#
# 现在:每一次降级都产出 `requested_form / effective_form / fallback_reason`
# 三元组落 `generation_meta`,并配一句**商业语言**的告知 + 两个出口。
# 🔴 措辞禁"合规/不足以出榜"腔 —— 用户没做错任何事,是我们这次的数据不够。

#: 完全没做榜单,走原有卡组型路径。
FORM_CARD_GROUP: Final = "card_group"

FALLBACK_NONE: Final = ""
#: 🔴 2026-08-08 **口径收窄**:本常量原来是"取不到候选"的**总口袋**,
#:    把「归并成功但池子真没货」和「我们没认出这个行业写法」混成一句话说 ——
#:    对前者是真话,对后者是**假话**(Owner 在生产看到的正是这一句)。
#:    现在它**只表示** `resolved_no_inventory`:归并成功、行业确实没有同行数据。
#:    另两种由下面两个常量各自承担。
FALLBACK_NO_CANDIDATES: Final = "no_candidates"
#: 平台可能有数据,只是这个行业写法还没被归并(`alias_missing`)。
#: **不能说"这个行业没数据"** —— 我们并不知道,我们只是没认出来。
FALLBACK_INDUSTRY_UNMERGED: Final = "industry_unmerged"
#: 这段行业原文同时像好几个平台行业(`ambiguous`)。宁可让人选,也不硬塞。
FALLBACK_INDUSTRY_AMBIGUOUS: Final = "industry_ambiguous"
FALLBACK_SELECTION_EMPTY: Final = "selection_empty"
FALLBACK_NO_CLIENT_EVIDENCE: Final = "no_client_evidence"
FALLBACK_INDUSTRY_DEFAULT: Final = "industry_default_not_ranking"
FALLBACK_ERROR: Final = "orchestration_error"
#: 张数装不下家数(封面 + 收尾各占一张)。这是**结构约束**,不是数据不足。
FALLBACK_CARD_BUDGET: Final = "card_budget_too_small"
# 🔴 `FALLBACK_CONFIRMED_NO_EVIDENCE`("有人工确认名单但名单内没几家有位次")
#    2026-08-08 **删除** —— 唯一产出它的分支已改成"回落全量池"(见 build_ranking_plan
#    里那段偏离说明),常量随之不可达。留着就是"死元数据"家族第五次:
#    读代码的人会以为系统还会因为这个原因降级,而它永远不会再发生。


def _fallback_notice(reason: str, *, client_brand: str = "",
                     effective_form: str = "",
                     industry: Optional[dict] = None) -> Optional[dict]:
    """降级告知。**商业语言**,给两个出口(补充候选 / 就用这版)。"""
    # 🔴 **只留一道判空**(下面 `if not msg`)。原来这里还有一道 `if not reason`,
    #    两道守卫互相遮蔽 —— 各自的变异体都被对方兜住,双双"存活",
    #    看着像"锁没判别力",其实是等价变异。一件事只留一处判定。
    who = str(client_brand or "").strip() or "这个客户"
    # 🔴 nav 类动作必须自带 `href`,前端**不许自己猜路由** ——
    #    本页历史上就出过一次:注释里那个 `/quote-center` 是编的,点了没反应。
    #    `/monitoring` 是全站在用的真路由(PublishCenter / M3 / PublishHistory 都跳它),
    #    而候选池 `geo_research_answer_entities` 正是监测那条链攒出来的。
    supplement = {"id": "supplement_candidates", "label": "去跑监测攒候选",
                  "type": "nav", "href": "/monitoring"}
    keep = {"id": "keep_current", "label": "就用这版", "type": "action"}
    # 🔴 归并态相关的两句**不许说"这个行业没数据"** —— 见 FALLBACK_INDUSTRY_UNMERGED 注释。
    ind = industry if isinstance(industry, dict) else {}
    ind_raw = str(ind.get("raw") or "").strip()
    shown = ind_raw if 0 < len(ind_raw) <= 24 else "这个客户填的行业"
    picks = [str(c) for c in (ind.get("candidates") or []) if str(c).strip()][:4]
    table = {
        FALLBACK_NO_CANDIDATES: (
            "这个行业还没攒够可以点名的同行数据,这次做成了卡组版式。"
            "补充候选之后可以重新生成榜单。"),
        FALLBACK_INDUSTRY_UNMERGED: (
            f"「{shown}」这个写法我们还没对上平台的行业分类,所以这次没取到同行数据,"
            "做成了卡组版式。这不代表这个行业没有数据 —— 是我们没认出来。"
            "确认一下它属于哪个行业,榜单就能重做。"),
        # 🔴 歧义有**两种来源,话不能一样说**:
        #    ① 多个候选(子串碰撞 ≥2 家)→ "同时像好几个行业"。
        #    ② **单个候选**(包 B 低置信裁决:LLM 猜了一个但没把握)→ 说成"像好几个"是句病句,
        #       而且丢掉了那个建议 —— 用户其实只需要"确认/否掉"一下,不必从零选。
        #    ③ 候选为空(展示前被过滤掉)→ 退回不列举的说法,不能留一对空括号。
        FALLBACK_INDUSTRY_AMBIGUOUS: (
            (f"「{shown}」我们倾向归到「{picks[0]}」,但没有把握,不敢替你定 —— "
             "定错了榜单里会混进别的行业的同行。这次做成了卡组版式,"
             f"确认一下是不是「{picks[0]}」就能重做。")
            if len(picks) == 1 else
            (f"「{shown}」同时像好几个行业"
             + (f"({'、'.join(picks)})" if picks else "")
             + ",我们不敢替你选 —— 选错了榜单里会混进别的行业的同行。"
               "这次做成了卡组版式,指定一下行业就能重做。")),
        FALLBACK_SELECTION_EMPTY: (
            "这次没能凑齐可以点名的同行(候选都没过跨引擎共识),已做成卡组版式。"
            "补充候选之后可以重新生成榜单。"),
        FALLBACK_NO_CLIENT_EVIDENCE: (
            f"暂时查不到「{who}」在 AI 回答里的实际位次,这次做成了场景推荐版式 —— "
            "挂着榜单的名把客户放在榜外,反而帮倒忙。跑一次监测拿到位次后可以重做成榜单。"),
        FALLBACK_INDUSTRY_DEFAULT: (
            "这一类在抖音上榜单式的实测效果偏弱,所以默认用了场景推荐版式。"
            "想要榜单的话,在版式里手动选一个榜单型母版就行。"),
        FALLBACK_ERROR: (
            "这次取候选没成功,已做成卡组版式,没有耽误这一单。稍后可以再生成一次榜单。"),
        FALLBACK_CARD_BUDGET: (
            "图的张数不够摆下这么多家(封面和收尾各占一张),这次做成了卡组版式。"
            "把张数加上去,或者少点几家,就能出榜单。"),
    }
    msg = table.get(reason)
    if not msg:
        return None
    if reason == FALLBACK_INDUSTRY_DEFAULT:
        # 这一支**现在就能自助解决**:手动选一个榜单型母版重做即可。
        actions = ({"id": "force_ranking_template", "label": "手动选榜单版式重做",
                    "type": "action"}, keep)
    elif reason == FALLBACK_NO_CLIENT_EVIDENCE:
        # 这一支缺的是**客户自己的位次**,补位次的动作在监测那条链上。
        actions = ({"id": "refresh_monitoring", "label": "去跑一次监测",
                    "type": "nav", "href": "/monitoring"}, keep)
    elif reason == FALLBACK_CARD_BUDGET:
        # 🔴 这一支**当场就能自助解决**,与监测无关 —— 给"去跑监测"是答非所问。
        actions = ({"id": "raise_card_count", "label": "加张数重做",
                    "type": "action"}, keep)
    elif reason in (FALLBACK_INDUSTRY_UNMERGED, FALLBACK_INDUSTRY_AMBIGUOUS):
        # 🔴 这两支缺的是**行业对不上**,不是候选不够 —— 给"去跑监测攒候选"同样是
        #    答非所问(跑再多监测也还是对不上那个写法)。正确动作是确认行业,
        #    去客户档案改成平台认得的行业。
        actions = ({"id": "confirm_industry", "label": "确认这个客户的行业",
                    "type": "nav", "href": "/my-clients"}, keep)
    else:
        actions = (supplement, keep)
    return {
        "reason": reason,
        "effective_form": effective_form,
        "message": msg,
        "actions": [dict(a) for a in actions],
    }


@dataclass(frozen=True)
class RankingOutcome:
    """一次榜单编排的**完整结果** —— 含"做没做成、做成了什么、为什么"。

    🔴 `plan is None` 不再等于"什么都没发生":`fallback_reason` 说明为什么,
       `notice()` 给用户一句人话 + 两个出口。
    """
    plan: Optional["RankingPlan"]
    requested_form: str = FORM_RANKING
    effective_form: str = FORM_CARD_GROUP
    fallback_reason: str = FALLBACK_NONE
    client_brand: str = ""
    #: 用户点的那一份(P1-3 再次创作从这里继承)。
    request: Optional["RankingRequest"] = None
    #: 行业归并留痕(`services.industry_canonical.CanonicalIndustry.to_meta()`)。
    #: 🔴 **成功时也落** —— 它同时是包 B 的冻结件:再次创作直接吃这一份,
    #:    不必重新归并,也就不会因为期间 admin 改了行业名而换一个池子。
    industry: Optional[dict] = None

    @property
    def degraded(self) -> bool:
        return bool(self.fallback_reason)

    def notice(self) -> Optional[dict]:
        return _fallback_notice(self.fallback_reason,
                                client_brand=self.client_brand,
                                effective_form=self.effective_form,
                                industry=self.industry)

    def to_meta(self) -> dict:
        """落 `generation_meta.ranking` 的那一份。

        🔴 `plan is None` 时**照样落** —— 降级留痕正是本字段存在的理由。
        """
        meta: dict = {
            "requested_form": self.requested_form,
            "effective_form": self.effective_form,
            "fallback_reason": self.fallback_reason,
            "degraded": self.degraded,
            # 🔴 用户点的那一份 + 服务端冻结的发布面。**再次创作只认这一块。**
            "request": (self.request or RankingRequest()).to_dict(),
            **target_face(),
        }
        if self.industry:
            meta["industry"] = dict(self.industry)
        notice = self.notice()
        if notice:
            meta["fallback_notice"] = notice
        if self.plan is not None:
            meta.update(self.plan.to_meta())
        return meta


def _load_confirmed_allowlist(brand_id: Optional[int], keyword: str) -> list:
    """人工确认竞品(共用实现在 `services/client_knowledge`,本处不再造一套)。

    取不到一律返回 `[]` —— 没有人工名单是**常态**(生产 392 张报价单里
    只有 51 张模式为真且带名单),链路必须能自然落到 ②监测 / ③研究。
    """
    if not brand_id:
        return []
    try:
        from services.client_knowledge import load_confirmed_competitors
        kws = [keyword] if str(keyword or "").strip() else []
        return load_confirmed_competitors(int(brand_id), kws) or []
    except Exception as e:  # noqa: BLE001 — 取不到名单不该挡住整条内容
        logger.warning("[douyin-ranking] 人工确认名单读取失败 brand=%s: %s",
                       brand_id, str(e)[:200])
        return []


def build_ranking_plan(*, industry_key: str, keyword: str = "", city: str = "",
                       client_brand: str = "", want: int = 0,
                       client_position: Optional[dict] = None,
                       force_template: str = "", force_ranking: Optional[bool] = None,
                       window_days: int = 0,
                       requested_form: str = FORM_RANKING,
                       card_budget: int = 0,
                       client_brand_id: Optional[int] = None,
                       frozen_industry: Optional[dict] = None,
                       ) -> "RankingOutcome":
    """**榜单主链的唯一编排入口** —— 付费任务从这里进来。

    取候选 → 定形态 → 选模板 → 清洗 → 冻结,一次做完,交出可直接进生成与渲染的冻结件。

    🔴 2026-08-06 返工:上一版这五个符号(`fetch_ranking_candidates` / `decide_form` /
       `route_template` / `FrozenRankingPayload` / `rank_statement`)在运行时代码里
       **零调用方** —— 200 条锁证明的是模块自洽,不是付费链成立。本函数是把它们
       接进付费链的那一跳,`production_task` 直接调它。

    🔴 2026-08-06 二次返工:返回值从 `Optional[RankingPlan]` 改成 `RankingOutcome` ——
       **永不返回 None**。`outcome.plan is None` 表示这一单退回卡组型,
       但此时 `fallback_reason` 一定有值,调用方必须把它落进 meta 告诉用户。
       (旧签名的问题不是不好看,是"降级"和"没发生"在类型上分不开。)

    **任何异常都不外抛** —— 榜单做不成不该让整单失败(永不中断)。
    """
    from services.geo_douyin.ranking_payload import (
        AggregationContract, FrozenRankingPayload, RankingItem,
        clean_phrases, rank_statement, select_entities,
    )
    from services.geo_douyin.ranking_source import (
        DEFAULT_WINDOW_DAYS, fetch_ranking_candidates,
    )
    from services.industry_canonical import (
        STATUS_ALIAS_MISSING, STATUS_AMBIGUOUS, from_frozen, resolve_readonly,
        with_inventory,
    )

    # 🔴 2026-08-08 P0:**入口归一一次,往下全用归一后的键**。
    #    `industry_key` 是品牌档案的自由文本,而下游三处都按**受控枚举**精确查表:
    #      ① `fetch_ranking_candidates` → `geo_research_answer_entities.industry_key`
    #      ② `route_template` → `default_prefers_ranking` → `_CAPTION_RANKING_*`
    #      ③ 冻结件 `FrozenRankingPayload.industry_key`(取数决策的留痕)
    #    三处全部静默落空/落默认。归一放在这里而不是各查表处,是因为**这一跳是
    #    自由文本进入枚举键空间的边界** —— 边界上转一次,下游不必各自记得转。
    #    (`fetch_ranking_candidates` 内部也转一次:它是公共读取口,得自己站得住。
    #     归一幂等,转两次与转一次逐字同结果。)
    # 🔴 归一**必须在 try 之内**:本函数的契约是"任何异常都不外抛"(榜单做不成不该
    #    让整单失败)。放在 try 外面,归一自己抛一次就把这条契约破了。

    req = RankingRequest(
        content_form=requested_form, entity_count=int(want or 0),
        template=str(force_template or ""), force_ranking=bool(force_ranking))

    def _give_up(reason: str, *, industry: Any = None) -> "RankingOutcome":
        return RankingOutcome(plan=None, requested_form=requested_form,
                              effective_form=FORM_CARD_GROUP,
                              fallback_reason=reason, client_brand=client_brand,
                              request=req,
                              industry=industry.to_meta() if industry is not None else None)

    try:
        # 🔴 2026-08-08 包 A:归一之前**先归并**。`normalize_industry_key` 单用只对
        #    13 条别名表内的行业有效,表外**退化成 slugify** —— 品牌档案填的
        #    「AI搜索优化/GEO服务」这类长尾永远对不上受控枚举的候选池。
        #    归并走全站唯一只读入口(零 LLM · 不写共享表),见 `services/industry_canonical`。
        # 🔴 优先吃**冻结件**(包 B 在下单前就归并好了):执行期不再查库、不再临时归并,
        #    期间 admin 改行业名也不影响这一单。冻结件缺失/口径过期 → 现场归并兜底。
        ci = from_frozen(frozen_industry) or resolve_readonly(industry_key)
        ind_key = ci.industry_key
        win = int(window_days) or DEFAULT_WINDOW_DAYS
        # 🔴 `prefer_engine` **只影响"引谁的名次"**,不筛候选:
        #    共识面维持四引擎,否则 `MIN_ENGINE_CONSENSUS = 2` 会把池子挡光。
        pool = fetch_ranking_candidates(ind_key, window_days=win,
                                        prefer_engine=TARGET_ENGINE)
        ci = with_inventory(ci, len(pool))
        if not pool:
            # 🔴 取不到候选**有三种完全不同的原因**,过去一律说成"这个行业没数据":
            #    真没货(说得对)/ 我们没认出这个写法 / 这段原文同时像好几个行业。
            #    后两种是**假话**,而且把用户引向"去跑监测"这个解决不了问题的动作。
            reason = {
                STATUS_ALIAS_MISSING: FALLBACK_INDUSTRY_UNMERGED,
                STATUS_AMBIGUOUS: FALLBACK_INDUSTRY_AMBIGUOUS,
            }.get(ci.status, FALLBACK_NO_CANDIDATES)
            logger.info("[douyin-ranking] industry=%s(归并 %s → 键 %s · %s)无候选,退回卡组型",
                        industry_key, ci.canonical_name, ind_key, ci.status)
            return _give_up(reason, industry=ci)

        # ── P1-1:人工确认名单是**允许名单的上界**,不是"再加几家" ──────────
        #
        # 🔴 优先级链(工单 §P1-1):①人工确认 → ②监测 → ③研究。
        #    ①存在时,②③**只能在①之内**,不许把名单外的公司加进来 ——
        #    代理亲手确认过的名单被系统悄悄扩写,是最难发现的一种失控。
        # 🔴 但**上界不等于放行**:治理 SSOT D12④「榜单内必须有有依据的实质位次」。
        #    名单里的公司如果查不到位次,就只有名字没有依据 —— 那不叫榜单。
        #
        # 🔴 2026-08-08 偏离(相对 REVIEW_VERDICT_GEOVID_CONTEXT_R3_2026-08-07 已采纳的
        #    "严格上界",需要重新裁定):**对不齐时不再降级,回落全量池并留痕**。
        #
        #    上一版是"交集 < 3 就 `_give_up(confirmed_list_lacks_evidence)`"。
        #    生产实证(2026-08-08 · quote 386 / home_improvement)证明这条会误伤:
        #      A 源(报价单人工确认)写「好莱客**全屋定制**」「欧派**全屋定制**」…
        #      B 源(候选池)写「好莱客」「欧派」…
        #    同一家公司两种写法,`safe_merge_key` 精确键判定 12 家只命中 3 家 ——
        #    而这 12 家里有 11 家在池子里既过跨引擎共识又有名次,其中索菲亚 45 提及/
        #    4 引擎/最好第 1、好莱客 37/4/第 1、欧派 29/4/第 1 全被写法差异挡在外面。
        #    坚持严格上界的结果是:要么整单降级,要么用剩下 3 家做一份明显更差的榜单。
        #
        #    为什么不放宽键(改 `family_key` 或加"剥行业尾词"容错)——**试过,不行**:
        #      · `family_key` 剥的是 `_GENERIC_INDUSTRY_TERMS`(电梯/科技/酒店那类),
        #        实测对这 10 对同样 2/10,帮不上;而且它把「通力」和「通力电梯」错合了,
        #        那正是括号包 P0 裁定**不能合**的一对(行业词是身份的一部分)。
        #      · 字面上无解:「好莱客 / 好莱客全屋定制」是同一家,
        #        「通力 / 通力电梯」不是 —— 字符串层面区分不了。
        #    合错的代价(把用户没确认的公司放上榜)大于漏合,所以**键一格不放宽**,
        #    改在"对不齐之后怎么办"上让步。
        #
        #    让步的安全边界:池子里的候选全部来自 AI 回答实测,不是虚构;
        #    D12④ 要的"有依据的实质位次"仍然成立(全量池每家都带 `recommendation_rank`)。
        #    丢掉的只是"只点用户亲手确认过的那几家"这条**内部**约束 ——
        #    它没有对用户做过承诺(已核验竞品面板在写作大厅,不在图文创建流里)。
        confirmed = _load_confirmed_allowlist(client_brand_id, keyword)
        allowlist_audit = {"size": len(confirmed), "hits": 0, "applied": False}
        # 🔴 2026-08-08 A 层:人工确认名单里那段**画像**带进来,按 merge key 索引。
        #    用途只有一个 —— 进企业卡 prompt 当**选材背景**,让四张卡写出真差异
        #    (这家柔性生产 / 这家环保板材 / 这家意大利设计),而不是四张长一个样。
        #    **不进 meta、不外露原句**:它是我们自己 LLM 综述的,不是可引用事实。
        #    (名字口径对不上的那几家自然取不到画像,不影响主链 —— 少一段背景而已。)
        #    B 层(2026-08-08)在这里多带一位 `citable`:可引用的那几家,
        #    画像里的**事实**可以进卡面(仍不许整段照抄);不可引用的只定角度。
        #    判定收在 `client_knowledge` → `competitor_name_contract`,本处不重判。
        entity_profiles = {
            safe_merge_key(str(c.get("display_name") or "")): {
                "text": str(c.get("profile") or ""),
                "citable": bool(c.get("profile_citable")),
            }
            for c in confirmed if str(c.get("profile") or "").strip()
        }
        entity_profiles.pop("", None)
        if confirmed:
            allow = {safe_merge_key(c["display_name"]) for c in confirmed}
            allow.discard("")
            if client_brand:
                allow.add(safe_merge_key(client_brand))   # 客户本人恒在名单内
            inside = [c for c in pool
                      if safe_merge_key(str(c.get("entity_name") or "")) in allow]
            allowlist_audit["hits"] = len(inside)
            if len(inside) >= ENTITY_COUNT_MIN:
                logger.info("[douyin-ranking] 人工确认名单 %s 家,命中 %s 家,按上界收窄",
                            len(confirmed), len(inside))
                pool = inside
                allowlist_audit["applied"] = True
            else:
                # WARNING 而不是 INFO:这是**名字口径没对齐**的信号,
                # 攒够样本就能反推该把哪一侧的写法统一过去。留痕在 meta 里。
                logger.warning(
                    "[douyin-ranking] 人工确认名单 %s 家,候选池只命中 %s 家(<%s)—— "
                    "两侧名字口径未对齐,本次不施加上界,用全量候选池",
                    len(confirmed), len(inside), ENTITY_COUNT_MIN)

        routed = route_template(pool, industry_key=ind_key, city=city,
                                client_brand=client_brand,
                                client_position=client_position,
                                force_template=force_template,
                                force_ranking=force_ranking)

        want_n = int(want) or routed["entity_count_max"]
        want_n = max(routed["entity_count_min"], min(want_n, routed["entity_count_max"]))

        # 🔴 张数装不下家数(P1-4 的结构约束):榜单形态下封面与收尾各占一张,
        #    剩下的才是企业卡 —— 每家一张,由代码指派讲哪一家。
        #    装不下时**不能悄悄少做几家**(用户按 N 家付的钱),而是如实降级并说清楚。
        if int(card_budget or 0) > 0:
            from services.geo_douyin.series_plan import entity_slots_for
            slots = entity_slots_for(int(card_budget))
            if slots < routed["entity_count_min"]:
                logger.info("[douyin-ranking] 张数 %s 只剩 %s 个企业卡位,装不下最少 %s 家",
                            card_budget, slots, routed["entity_count_min"])
                return _give_up(FALLBACK_CARD_BUDGET)
            want_n = min(want_n, slots)

        picked, dropped = select_entities(pool, want=want_n, client_brand=client_brand)
        if not picked:
            logger.info("[douyin-ranking] industry=%s(归一 %s)候选无一入选,退回卡组型",
                        industry_key, ind_key)
            return _give_up(FALLBACK_SELECTION_EMPTY)

        # 聚合合同:七要素目前给不齐(query 是逐行的、算法未定版)——
        # 给不齐就**明确留空**,让 `rank_statement` 走"某引擎对某问题列第 N"那一支。
        contract = AggregationContract(
            query=str(keyword or ""),
            name_normalization="safe_merge_key v1",
            engine_scope=tuple(sorted({e for c in picked for e in (c.get("engines") or [])})),
        )

        items: list[RankingItem] = []
        cleaning_log: list[dict] = []
        for i, c in enumerate(picked, start=1):
            cleaned = clean_phrases(c.get("evidence_phrases") or [])
            tags = tuple(x.text for x in cleaned if x.kept)[:4]
            for x in cleaned:
                if not x.kept:
                    cleaning_log.append({"entity": c.get("entity_name"),
                                         "dropped": x.original, "reason": x.reason})
            items.append(RankingItem(
                rank=i, entity_id=str(c.get("entity_key") or ""),
                display_name=str(c.get("entity_name") or ""),
                tags=tags, source=dict(c.get("source") or {}),
                is_client=(safe_merge_key(str(c.get("entity_name") or ""))
                           == safe_merge_key(client_brand) if client_brand else False),
            ))

        payload = FrozenRankingPayload(
            template_id=routed["template_id"], form=routed["form"],
            # 🔴 冻结件记**归一后**的键:快照要如实反映"这一单实际是按哪个键取的数、
            #    选的版式",不是"调用方传了什么字符串"。品牌档案那句自由文本仍在
            #    `geo_douyin_posts.industry_key` 上,一个字没丢。
            industry_key=ind_key, city=city,
            buyer_question=str(keyword or ""),
            ranking_basis=tuple(contract.to_dict()["engine_scope"]),
            items=tuple(items), client_brand=client_brand,
            aggregation=contract,
            dropped_candidates=tuple(dropped),
            cleaning_log=tuple(cleaning_log),
        )
        statements = [rank_statement(it, contract) for it in items]
        plan = RankingPlan(payload=payload, routed=routed, statements=statements,
                           allowlist=dict(allowlist_audit),
                           profiles=dict(entity_profiles))

        # 做成了,但**做成的不一定是用户点的那个**。这里判"要不要告诉他"。
        # 🔴 三分支**穷尽且互斥**,不留 `else` 兜底:
        #    `route_template` 只在"有据 + 这个面×行业默认不走榜单"时把
        #    RANKING 降成 SCENARIO,所以 (eff != ranking 且 有据) ⟺ 行业默认那一支。
        #    上一版多写了一个到不了的 else,并且让它复用 `no_client_evidence` ——
        #    结果"有据判定被短路"这个变异从那个洞穿了过去(两支报同一个原因,
        #    测试分不出来)。**到不了的分支 + 复用别人的取值 = 判别力的洞。**
        eff = str(routed["form"])
        if eff == FORM_RANKING:
            reason = FALLBACK_NONE
        elif not routed["has_client_evidence"]:
            # 查无客户实质位次 → D12④ 换形态并改名。这是最常见的一支。
            reason = FALLBACK_NO_CLIENT_EVIDENCE
        else:
            # 有据,但这个面×行业的实测默认不走榜单式。
            reason = FALLBACK_INDUSTRY_DEFAULT
        return RankingOutcome(plan=plan, requested_form=requested_form,
                              effective_form=eff, fallback_reason=reason,
                              client_brand=client_brand, request=req,
                              industry=ci.to_meta())
    except Exception as e:  # noqa: BLE001 — 榜单做不成不该让整单失败
        logger.warning("[douyin-ranking] 编排失败 industry=%s: %s",
                       industry_key, str(e)[:200])
        return _give_up(FALLBACK_ERROR)


@dataclass(frozen=True)
class RankingPlan:
    payload: Any
    routed: dict
    statements: list
    #: 人工确认名单这一跳的审计:`{size, hits, applied}`。
    #: 🔴 它**有明确消费方**才留(不是又一个没人读的字段):
    #:    `applied=False` 是"两侧名字口径没对齐"的唯一落库信号 ——
    #:    攒够样本才知道该把哪一侧的写法统一过去,否则这件事永远只活在日志里。
    #:    (`default_factory` 而不是 `= {}`:dataclass 不许可变默认值。)
    allowlist: dict = field(default_factory=dict)
    #: `safe_merge_key(名字) → 画像文本`。**只喂 prompt,不进 meta、不外露原句。**
    #: 🔴 刻意**不**放进 `to_meta()`:详情端点目前裸吐 generation_meta(已另立单),
    #:    画像一旦落进 meta 就等于对外发布了一段我们自己综述的第三方描述。
    profiles: dict = field(default_factory=dict)

    def to_meta(self) -> dict:
        """落 `generation_meta` 的那一份(冻结快照 + 路由决策)。"""
        return {
            "template_id": self.routed["template_id"],
            "form": self.routed["form"],
            "allows_ranking_wording": self.routed["allows_ranking_wording"],
            "has_client_evidence": self.routed["has_client_evidence"],
            "confirmed_allowlist": dict(self.allowlist or {}),
            "frozen": self.payload.to_dict(),
            "statements": list(self.statements),
        }

    def allowed_names(self) -> list:
        """R1 的白名单 —— 冻结名单 + 客户品牌,**按 merge key 去重**。

        客户本人若已在名单里(有据分支就是这种情况),不重复列一遍。
        """
        out: list = []
        seen: set = set()
        for name in [it.display_name for it in self.payload.items] + [self.payload.client_brand]:
            n = str(name or "").strip()
            if not n:
                continue
            k = safe_merge_key(n)
            if k in seen:
                continue
            seen.add(k)
            out.append(n)
        return out


def _has_comparable_dimensions(candidates: Sequence[dict]) -> bool:
    """候选是否有可比的结构化维度(≥2 家各自至少 2 条中性特征标签)。"""
    rich = 0
    for c in candidates or []:
        if len([p for p in (c.get("evidence_phrases") or []) if str(p).strip()]) >= 2:
            rich += 1
    return rich >= 2


def _has_regional_signal(city: str, candidates: Sequence[dict]) -> bool:
    return bool(str(city or "").strip())


def route_template(candidates: Sequence[dict], *, industry_key: str = "",
                   city: str = "", surface: str = SURFACE_DOUYIN_CAPTION,
                   client_brand: str = "", client_position: Optional[dict] = None,
                   force_template: str = "", force_ranking: Optional[bool] = None,
                   ) -> dict[str, Any]:
    """一次算完三层,返回给上层的路由决策。**永不抛异常**。

    `force_template` / `force_ranking` 是**手动覆盖**入口(工单 §1.4:默认值可覆盖)。
    """
    verdict = decide_form(candidates, client_brand=client_brand,
                          client_position=client_position)

    prefers = (bool(force_ranking) if force_ranking is not None
               else default_prefers_ranking(industry_key, surface=surface))

    # 无据 → 一律不叫榜单(手动覆盖也不能把无据说成有据 —— 这是 D12④ 的硬约束,
    # 不是版式偏好)。force_ranking 只影响"版式要不要榜单感",不影响 form。
    form = verdict.form
    if form == FORM_RANKING and not prefers:
        # 有据但这个面×行业默认不走榜单 → 保留实名次,但用场景版式呈现
        form = FORM_SCENARIO

    n = len([c for c in (candidates or [])])
    if force_template and force_template in TEMPLATES:
        tid = force_template
    elif form == FORM_RANKING and n <= 3:
        tid = "top3_provider"
    elif _has_comparable_dimensions(candidates):
        tid = "tech_spec_matrix"
    elif _has_regional_signal(city, candidates):
        tid = "regional_service"
    else:
        tid = "scenario_fit"

    tpl = TEMPLATES[tid]
    return {
        "template_id": tid,
        "template_label": tpl.label,
        "composition": tpl.composition,
        "form": form,
        "allows_ranking_wording": form == FORM_RANKING,
        "has_client_evidence": verdict.has_evidence,
        "client_rank": verdict.client_rank,
        "client_engine": verdict.client_engine,
        "evidence_reason": verdict.reason,
        "entity_count_min": tpl.min_entities,
        "entity_count_max": tpl.max_entities,
        "default_prefers_ranking": prefers,
        "surface": surface,
        "spec": RANKING_VISUAL_SPEC,
        "overridden": bool(force_template) or force_ranking is not None,
    }
