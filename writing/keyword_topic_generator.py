"""
关键词驱动的标题生成器
根据客户确定的关键词批量生成优化标题
"""
import os
import asyncio
import json
import httpx
from datetime import datetime
from typing import List, Dict, Any


def _required_article_count(keyword: Dict[str, Any]) -> int:
    """本次为这个词出几**条**标题。Preserve an explicit zero.

    [WO_225-c1 §8.6] 优先级(高 → 低):
      1. `planned_count`        —— 请求里的 per_keyword_plan 明确指定的条数
      2. `planned_posts_default`—— 按这单口径换算后的默认条数(7 槽 ⇒ 自媒体约 35 条)
      3. `required_articles`    —— 冻结的授权**槽**数(老行为,k=1 时与 2 逐值相等)

    🔴 三者的**单位不同**:1/2 是条,3 是槽。之所以能排成一个优先级,
       是因为 2 由 3 乘以口径系数得来,且口径读不到时系数 = 1 ⇒ 2 == 3。
       任何时候取不到 1 和 2,落回 3 就是今天的行为。
    🔴 显式 0 必须保留(0 != 缺失):三个键都用 `is None` 判缺失,不用真值判断。
    """
    for _key in ("planned_count", "planned_posts_default"):
        _raw = keyword.get(_key)
        if _raw is not None:
            return max(0, int(_raw))
    raw_count = keyword.get("required_articles")
    return 1 if raw_count is None else max(0, int(raw_count))


def _family_ratio_prompt(industry: str | None) -> str:
    """Render the current internal ratio configuration as six outward families."""
    try:
        from config.settings_manager import get_effective_style_ratios
        from writing.article_style_contract import STYLE_FAMILIES
        from writing.direction_distribution import style_code_to_user_choice

        internal = get_effective_style_ratios(industry, unit="percent")
        family_ratios = {code: 0.0 for code in STYLE_FAMILIES}
        for style_code, ratio in internal.items():
            family_code = style_code_to_user_choice(style_code)
            if family_code in family_ratios:
                family_ratios[family_code] += float(ratio or 0)
        return "\n".join(
            f"- {STYLE_FAMILIES[code].name}：{value:g}%"
            for code, value in family_ratios.items()
        )
    except Exception:
        return "- 六类文体：按问题意图与证据条件均衡分配（配置不可用时的保守降级）"


def _legal_prohibition_prompt_parts() -> tuple[str, str]:
    """[Review-CTO P1-2/P1-4] 提示词禁令与运行时硬门同源:同一版本化清单 SSOT。"""
    from writing.evidence_first_policy import legal_prohibition_prompt_terms

    return legal_prohibition_prompt_terms()


def _build_title_generator_prompt(
    industry: str | None = None,
    *,
    question_ratio_percent: int | None = None,
    gap_block: str = "",
    intent_block: str = "",
) -> str:
    """Build titles from the outward six-family article contract.

    [P1-1 2026-08-14] ``gap_block``:D6-B 监测缺口输入段(由
    services/reco_outcome_feedback.build_title_gap_block 产出,含「打不动
    退回选词」出口)。空串 = prompt 与旧版逐字一致(A1:缺数据按现行为降级,
    依赖 P0-3 覆盖率,数据上来自然生效)。
    """
    from writing.title_formula_library import build_title_formula_prompt
    # [工单 标题问句化 2026-07-29 · T1] 比例口径与落库前的执行层同源:
    # 提示词说 70%、执行层也按 70% 收敛,不允许两处各写一份数字。
    from writing.title_question_policy import (
        build_question_ratio_prompt,
        get_question_ratio_percent,
    )

    # [P2 标题要素合同 2026-08-08] 年份/要素默认的唯一 SSOT。本函数**不再自己写**
    # 任何一句年份口径 —— 2026-07-29 那版把"全族默认不写"写死在这段 f-string 里,
    # 结果同仓另外三层(兜底模板/evidence-first 兜底/深档规格)各写各的,三种意见。
    from writing.title_element_contract import (
        build_title_element_defaults_prompt,
        build_title_year_rule_prompt,
        current_year,
    )

    year = current_year()
    family_ratio_summary = _family_ratio_prompt(industry)
    title_year_rule_block = build_title_year_rule_prompt(year=year)
    title_element_defaults_block = build_title_element_defaults_prompt()
    legal_terms_line, legal_catalog_version = _legal_prohibition_prompt_parts()
    # [标题自然化包③ §3G-3] 内部术语负面约束。**与机审侧 `scan_title_jargon` 同一份表**,
    # 不在提示词里另抄一份清单(本仓刚踩过"测试硬编码集合副本"的假绿)。
    from writing.title_jargon_blacklist import render_title_jargon_constraint

    title_jargon_constraint = render_title_jargon_constraint()
    # [WP12 P1-4] 标题公式库是唯一 SSOT,禁止在此写第二份公式口径。
    title_formula_block = build_title_formula_prompt(year=year)
    question_ratio_block = build_question_ratio_prompt(
        get_question_ratio_percent(industry, override=question_ratio_percent)
    )
    # [P1-1] 缺口段:空 → 空串拼接,prompt 与旧版逐字一致(判别测试打这一点)。
    gap_section = f"\n\n{gap_block.strip()}" if str(gap_block or "").strip() else ""
    # [标题 AI-only 2026-08-17 · 工单 §1.3] 意图适配段:AI 是第一顺位 ——
    # 先让模型自己按购买意图挑角度,不适配的族**直接省略**而不是硬凑满数。
    # 空串 → prompt 与旧版逐字一致(判别测试打这一点)。
    intent_section = (
        f"\n\n{intent_block.strip()}" if str(intent_block or "").strip() else ""
    )
    return f"""# 角色：GEO 证据型选题编辑

你的任务是把真实关键词转成可回答、可核验、与正文结构一致的标题。客户优先不等于客户固定第一；所有品牌与效果表述都必须有证据。

## 核心规则

- 标题年份**按文体走条件默认**,见下方【标题年份默认】块(唯一口径,本节不复述)
- 每个 topic 的 article_style 只能从下列六类中选择
- 输入中的 keyword_id 与关键词是客户已确认的商业事实，禁止改绑、串词或自行换成更宽泛的行业词
- 每个标题必须保留该关键词最具体的业务对象；可以自然缩写或改写问法，但不能只剩行业、品牌故事或相邻话题
- 地区、产品类型、应用阶段等会改变购买范围的限定词必须保留；六类文体只改变回答角度，不改变客户购买的问题
- 如果某个文体不适合该关键词，应在同一业务对象内换角度，不得为了凑文体生成无关标题
- 排名、推荐、哪家好、对比、价格、性价比是客户购买问题的合法核心方向
  （SSOT geo-commercial-intent-governance-v1.0 §4.4）：标题必须保留该商业
  意图，不得改写成脱离购买意图的知识/百科问题；榜单与排名标题需在正文
  披露排序依据与边界，依据不足时按「排名依据不足」提示交人工决定
- 不得仅凭关键词声称已经实测、已有案例、掌握报价、采访专家或获得平台认可
- 数字只在输入已提供可核验数量时出现；不得为了标题吸引力编造“5 家”“10 个”“3 个案例”

## 当前六类运行配比

{family_ratio_summary}

## 标题公式库（数据驱动 · 优先套用）

{title_formula_block}

## 标题形态配比（本批硬要求）

{question_ratio_block}{gap_section}{intent_section}

## 标题年份默认（分文体条件默认 · 唯一口径）

{title_year_rule_block}

## 标题要素可解释默认（年份/地区/行业对象/榜单词/数字）

{title_element_defaults_block}

## 标题×文体规则（飞轮实证矩阵 · 硬约束）

- **问句形全族优先**：六类文体的标题都优先写成读者的真实问法（"怎么选？""哪家好？"
  "如何测算？"），陈述式只在问法不自然时使用；
- **形态词与文体强绑定**：
  - 榜单词（排行/排名/TOP N/十大/推荐榜 等）**只允许**出现在「选购与多品牌比较」
    标题里；
  - 「案例、数据与 ROI」标题**禁用榜单词**（案例族写口径与结果，不写名次）；
  - 「方法与实施指南」标题**禁用"指南/攻略"字样**（写具体动作与问题，
    不贴体裁标签——"如何验收？分几步？"比"××指南"更像真实问法）；
- **地域词仅对比族鼓励**：只有「选购与多品牌比较」鼓励前置地域限定；
  其余文体保留关键词自带的地域即可，不额外添加。

## 六类文体与标题结构

### 1. 证据型问答
- 回答一个清晰问题，标题体现答案范围或核验边界
- 示例：「{{行业}}常见问题怎么判断？证据型问答与核验清单」

### 2. 选购与多品牌比较（含推荐/排名/榜单形态）
- 仅比较真实候选，强调统一字段、适用场景和限制；排名/榜单形态合法，
  但必须在正文披露排序依据，不得虚构名次或候选
- 本族是**唯一默认带当年年份**的文体（见【标题年份默认】块）；年份放中后部，不作开头
- 示例：「{{行业}}哪家好？{year}年按预算与交付方式分场景推荐」
- 示例：「{{行业}}服务商推荐与排名参考｜{year}年依据与适用边界」

### 3. 方法与实施指南
- 标题体现前置条件、步骤、检查点、风险或验收
- 示例：「{{行业}}如何落地？实施步骤、风险与验收清单」

### 4. 趋势、政策与风险分析
- 按变化→证据→影响→行动组织，不把相关性写成因果；年份按【标题年份默认】块走
  （本族默认不写，仅年度变化主题写，且不作开头）
- 示例：「{{行业}}{year}年有哪些变化？政策、风险与行动建议」（这是**年度变化主题**的例子，
  不是本族的常态默认）

### 5. 案例、数据与 ROI
- 有已核验证据时可写案例/数据；没有时只能写口径、测算框架和复核路径
- 示例：「{{行业}}投入产出如何测算？数据口径与 ROI 复核框架」

### 6. 企业事实与品牌说明
- 标题含品牌名；正文必须区分企业自述、官方材料和独立来源，并走人工审核
- 示例：「{{品牌}}适合哪些场景？企业事实、能力边界与公开信息核验」

## 分配原则

同一关键词的多篇文章优先覆盖该购买问题下的不同决策子问题，不得扩展成脱离原业务对象的泛行业意图。系统推荐比例只决定抽样，不代表哪类必然更容易被 AI 引用；样本不足时保持“证据不足”。

## 输出格式

严格返回 JSON：
```json
{{
  "topics": [
    {{
      "keyword_id": "原样返回输入 keyword_id",
      "slot_index": "同一关键词内从 0 开始",
      "original_keyword": "原始关键词",
      "optimized_title": "风格匹配的标题（年份按【标题年份默认】块分文体决定，不作开头）",
      "article_style": "六类文体之一",
      "angle": "本篇如何在不改变业务对象的前提下回答原购买关键词"
    }}
  ]
}}
```

## 禁止事项

1. 禁止绝对化/保证性用语：{legal_terms_line}
   （法律禁止清单 {legal_catalog_version} ·《广告法》第九条 · 与运行时硬门同源，
   其中最高级用语指对品牌/产品的宣称用法）；
   TOP/榜单/排名/前十等排名**形态**合法，前提是正文披露真实排序依据
2. 禁止虚构品牌、报价、案例、效果、资历、奖项、专家观点、媒体背书
3. 禁止把企业自述包装成独立结论
4. 禁止标题与 article_style 不一致或同关键词标题角度雷同
5. 禁止只正确返回 keyword_id/original_keyword、却让 optimized_title 脱离该关键词
6. 禁止把关键词**整串逐字**硬塞进句子。购买关键词若本身是问句（如「深圳哪家装修公司靠谱」），
   请自然拆开使用其中的业务对象（深圳 / 装修公司），不得写成「深圳哪家装修公司靠谱怎么选？」
   这类语法坏死的标题。**红线不变**：地区、产品、采购意图一个都不许换，变的只是行文方式。
{title_jargon_constraint}
"""


# ---------------------------------------------------------------------------
# 🔴 [标题 AI-only 2026-08-17 · Owner 裁决] 硬编码兜底模板表**已整体退役**。
#
# 退役的四个符号(本文件内已删除,全仓零 caller):
#   `_fallback_style_title_map` / `_fallback_form_completion_map`
#   / `fallback_templates_for_form` / `_safe_fallback_title`
#
# 病在哪:这张 6 族 × 3 条的表是"最后兜底",但生产实测它根本不是最后 ——
# 选题池 15 条候选里 4 条与它逐字对应(「常见问题一次说清」「实操流程说明」
# 「趋势与建议」「一文看懂」)。原因是上游"硬凑满数":不适配的文体族凑不出
# 标题,就落到这张表。**先有硬凑,才有兜底。**
#
# 现在:标题只能出自 AI。失败梯在 `writing/title_ai_only.py`
# (① 同批合格 AI 候选补位 → ② AI 重试 → ③ 显式失败,不硬凑满数);
# 意图适配在 `writing/title_intent_style_gate.py`(不适配的族不出题)。
#
# ⚠️ 想在这里"临时加一条模板顶一下"的人:那正是本裁决要消灭的东西。
#    产不出标题就让它显式失败 —— 用户看得见、点得动重试,比拿到一条
#    与十个客户一模一样的模板标题好。
# ---------------------------------------------------------------------------


class KeywordTopicGenerator:
    """关键词驱动的标题生成器（V2: 支持客户专属知识库）"""
    
    def __init__(
        self,
        keywords: List[Dict],
        brand_name: str,
        industry: str,
        brand_id: int = None,
        quote_id: int = None,
        force_chinese_style: str = None,  # v2.8 强制全部标题用同一文体(None=走默认 ratio)
        style_plan: List[Dict] = None,  # v2.10 per-topic plan struct(优先级高于 force_chinese_style)
        title_form: str = None,  # [T1 2026-07-29] 用户显式指定标题形态 → 比例抽签不介入
        question_ratio_percent: int = None,  # [T1] 显式覆盖问句式占比(None=走可配置默认 70)
        defensive_plan: List[Dict] = None,  # [#185 c3] 防御型槽位:[{keyword, slot_index, question, angles}]
    ):
        """
        Args:
            keywords: 关键词列表 [{"keyword": "...", "required_articles": N, ...}]
            brand_name: 品牌名称
            industry: 行业
            brand_id: 品牌ID（用于检索客户专属知识库）
            quote_id: 报价单ID（备用）
            force_chinese_style: v2.8 用户选题阶段强制文体(中文 article_style 名 ·
                                  来自 style_registry.resolve_user_choice_to_chinese_style)·
                                  None = 走系统推荐均衡分布
            style_plan: v2.10 per-topic plan(Codex 二审 P1-4):
                [{"keyword_id": int, "slot_index": int, "user_choice": str, "user_choice_source": str}, ...]
                优先级:style_plan > force_chinese_style > default ratio
                作用:LLM prompt 注入 per-topic 风格分配 · 保存标题时严格匹配 · 防"标题正文错位"
        """
        self.keywords = keywords
        self.brand_name = brand_name
        self.industry = industry
        self.brand_id = brand_id
        self.quote_id = quote_id
        self.force_chinese_style = force_chinese_style  # v2.8
        # [工单 T1 2026-07-29 · §1.2 用户 > 默认] 用户/运营显式点了标题形态就直接
        # 照办,**不走比例抽签**;"auto"/None 视为未指定。
        _form = str(title_form or "").strip().lower()
        self.title_form = _form if _form in ("question", "open") else None
        self.question_ratio_percent = question_ratio_percent
        self.title_question_report: Dict | None = None
        self.title_dedupe_report: Dict | None = None
        # [标题 AI-only 2026-08-17] 失败梯与意图闸的账本。调用方据此对用户显式报错,
        # 不许静默少给标题、更不许拿模板凑数。
        self.title_failure_report: Dict | None = None
        self.title_intent_report: Dict | None = None
        # 同批"合格但放不下"的 AI 候选(按关键词原文分组)。失败梯 ① 从这里补位。
        self._ai_surplus_titles: Dict[str, List[str]] = {}
        self.style_plan_mode = "custom" if style_plan else "recommended"
        if style_plan:
            self.style_plan = style_plan
        elif not force_chinese_style:
            from writing.direction_distribution import (
                build_user_choice_style_plan,
                compute_recommended_user_choice_distribution,
            )
            total_articles = sum(_required_article_count(item) for item in keywords)
            recommended = compute_recommended_user_choice_distribution(industry, total_articles)
            plannable_keywords = [
                item for item in keywords
                if _required_article_count(item) > 0
            ]
            # 🔴 [Review 09-28] 配比按**条**排:推荐总数本来就按条算(上面 total_articles),
            #    这里把每词条数一并交进去,否则它按槽校验 ⇒ 自媒体单(1 槽≈5 条)必抛。
            self.style_plan = build_user_choice_style_plan(
                plannable_keywords,
                recommended,
                source=None,
                industry=industry,
                posts_per_keyword={item.get("id"): _required_article_count(item)
                                   for item in plannable_keywords},
            ) if total_articles else []
        else:
            self.style_plan = None
            self.style_plan_mode = "uniform"

        # [标题 AI-only 2026-08-17 · 工单 §1.3] 意图适配闸:把排到不适配文体族的
        # 槽位就地改判到同一意图下的适配族。**不减篇数**(合同交付数不归本闸管),
        # 只让"选服务商意图排到趋势展望族"这种硬凑从源头消失。改判逐条留痕。
        if self.style_plan:
            from writing.title_intent_style_gate import filter_style_plan_by_intent

            try:
                self.style_plan, _intent_changes = filter_style_plan_by_intent(
                    self.style_plan, self.keywords, industry=self.industry,
                )
                self.title_intent_report = {
                    "reassigned": len(_intent_changes),
                    "changes": _intent_changes[:50],
                }
                if _intent_changes:
                    print(
                        f"  🎯 意图适配改判 {len(_intent_changes)} 个槽位"
                        f"(不适配的文体族不出题)"
                    )
            except Exception as exc:  # noqa: BLE001 — 闸失效不许阻断选题产出
                print(f"  ⚠️ 意图适配闸失败,按原计划继续: {exc}")
                self.title_intent_report = {"error": str(exc)[:200]}
        # [#185 c3] 防御型(公司词)槽位:题面**走 LLM**,八问降为把关与兜底。
        # 这里只存"哪些槽是防御型、各自对应哪一问、playbook 给了什么角度";
        # 题面由 LLM 出,回来之后由 `defensive_questions.vet_llm_titles` 把关。
        self.defensive_plan = list(defensive_plan or [])
        self.knowledge_context = ""  # 知识库上下文
    
    def _load_client_knowledge(self) -> str:
        """加载客户专属知识库内容（同步包装async调用）"""
        if not self.brand_id:
            return ""

        try:
            from tools.unified_knowledge import get_unified_rag
            rag = get_unified_rag()

            # 检索与行业和品牌相关的知识
            search_query = f"{self.industry} {self.brand_name} 核心业务 卖点 优势 服务"
            print(f"  📚 检索客户知识库 (brand_id={self.brand_id}): {search_query[:50]}...")

            # rag.retrieve() 是async方法，需要用事件循环执行
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                # 已在async上下文中，用新线程的事件循环执行
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    kb_results = pool.submit(
                        lambda: asyncio.run(rag.retrieve(
                            query=search_query,
                            brand_id=self.brand_id,
                            top_k=5,

                            use_client=True,
                            use_role=False
                        ))
                    ).result(timeout=15)
            else:
                # 不在async上下文中，直接asyncio.run
                kb_results = asyncio.run(rag.retrieve(
                    query=search_query,
                    brand_id=self.brand_id,
                    top_k=5,
                    use_client=True,
                    use_role=False
                ))

            # [2026-06-07 P0-1b] rag.retrieve 返 List[Dict] —— 原当 dict(.get('results')) → 「'list' object has no attribute 'get'」→ 知识库永不生效
            if kb_results:
                print(f"  ✅ 检索到 {len(kb_results)} 条相关知识")
                return "\n\n".join([
                    f"- {str(r.get('full_content') or r.get('content', ''))[:300]}"
                    for r in kb_results[:5]
                ])
            else:
                print("  ⚠️ 客户知识库为空")
        except Exception as e:
            print(f"  ⚠️ 知识库检索失败: {e}")

        return ""
    
    async def generate(self) -> List[Dict]:
        """批量生成标题(10并发)"""
        from .llm_utils import get_llm_config, get_fallback_llm_config, get_thinking_disabled_params  # noqa: F401

        # [SSOT §4.4 · CommercialQueryPolicy 统一接线] 写作消费的是客户已
        # 购买/确认的关键词,不得在消费端丢弃(§3.3);逐词过唯一引擎做
        # advisory 可见性标注,供运营复核遗留知识词(准入层已由报价链把关)。
        try:
            from services.commercial_query_policy import evaluate as _policy_evaluate
            for _kw in self.keywords:
                _text = str(_kw.get("keyword") or "")
                if not _text:
                    continue
                _decision = _policy_evaluate(_text, brand_name=self.brand_name)
                _kw["commercial_policy"] = _decision.as_dict()
                if not _decision.commercial_delivery_eligible:
                    print(
                        "[CommercialQueryPolicy][advisory] 已购关键词未证明商业"
                        f"资格(照常生成·不拦截): {_text} · {list(_decision.reason_codes)}"
                    )
        except Exception:
            pass

        # 获取LLM配置
        api_url, api_key, model, provider = get_llm_config("topic_planning", "writing")

        # 预加载兜底模型配置
        self._fb_api_url, self._fb_api_key, self._fb_model, self._fb_provider = get_fallback_llm_config()

        if not api_key:
            # [标题 AI-only 2026-08-17] 主模型没配 key **不再退回模板**。
            # 走同一条失败梯:① 无同批候选(压根没生成过)→ ② AI 重试(兜底模型
            # 仍可能有 key)→ ③ 全失败就显式失败,少出候选而不是硬凑。
            print("⚠️ LLM API KEY未配置 · 标题只能由 AI 产出 → 走 AI 重试梯")
            return self._finalize_titles(
                await self._apply_batch_title_dedupe(
                    await self._resolve_pending_titles(
                        self._pending_generate()
                    )
                )
            )

        # ✅ V2新增：加载客户知识库
        self.knowledge_context = self._load_client_knowledge()
        
        # 智能分批: 按目标标题总数分批(每批最多30个标题防止超上下文)
        batches = self._chunk_by_titles(max_titles_per_batch=30)
        print(f"📝 分批处理: {len(batches)}批, 共{sum(_required_article_count(kw) for kw in self.keywords)}个标题")
        semaphore = asyncio.Semaphore(int(os.getenv("WRITING_TOPIC_CONCURRENCY", "5")))  # [env化 2026-06-11] 默认5(旧行为)·部署可调高
        
        async def process_batch(batch):
            async with semaphore:
                return await self._generate_batch(batch, api_url, api_key, model)
        
        results = await asyncio.gather(*[process_batch(b) for b in batches])
        
        # 展平结果
        all_topics = []
        for batch_result in results:
            all_topics.extend(batch_result)

        # [标题 AI-only 2026-08-17] 失败梯必须在去重/形态收敛**之前**跑完:
        # 下游两层都假设手上的是成品标题,空标题进去只会被当成"没有标题"静默略过。
        all_topics = await self._resolve_pending_titles(all_topics)

        return self._finalize_titles(await self._apply_batch_title_dedupe(all_topics))

    def _existing_quote_titles(self) -> List[str]:
        """同 quote 下**已存在(未发布)**的标题(工单 §7 明确要求一并比对)。

        只跟本批比 = 第二批会原样复现第一批。已发布的不参与:它们已经对外了,
        再去重也追不回来,且会把可用模板池白白占掉。
        失败一律返回空列表 —— 去重是锦上添花,**永不阻断选题产出**(与本仓
        `_apply_title_question_policy` 的既有口径一致)。
        """
        if not self.quote_id:
            return []
        try:
            from db.connection import get_connection

            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT t.optimized_title
                      FROM topics t
                      LEFT JOIN articles a ON a.id = t.article_id
                     WHERE t.quote_id = %s
                       AND COALESCE(t.optimized_title,'') <> ''
                       AND COALESCE(a.first_published_at IS NOT NULL, FALSE) = FALSE
                     ORDER BY t.id
                     LIMIT 2000
                    """,
                    (self.quote_id,),
                )
                return [str(r["optimized_title"]) for r in cur.fetchall()]
            finally:
                conn.close()
        except Exception as exc:
            print(f"  ⚠️ 已有标题读取失败,仅按本批去重: {exc}")
            return []

    async def _regenerate_conflicting_titles(
        self, topics: List[Dict], conflicts: List[Dict], used_titles: List[str],
    ) -> Dict[int, List[str]]:
        """把**恰好那几条冲突**打进**一次** LLM 请求换新标题。

        工单 §7:"冲突只重生成冲突项,不重跑整批;重生成提示词带入本批已用标题与角度"。
        - 只重生成冲突项 → 请求体里只有 conflicts,非冲突项根本不进 prompt;
        - 一次而不是 N 次 → N 条冲突只花一次调用(每条一次会把成本和延迟按冲突数放大);
        - 已用标题与角度随 prompt 注入,让模型知道要避开什么。
        任何失败都返回空 dict,交给结构化兜底 —— 去重永不阻断选题产出。
        """
        api_url = getattr(self, "_fb_api_url", None)
        api_key = getattr(self, "_fb_api_key", None)
        model = getattr(self, "_fb_model", None)
        if not (api_url and api_key and model) or not conflicts:
            return {}
        import httpx

        wanted = [
            {
                "id": c["index"],
                "purchased_keyword": str(c.get("keyword") or ""),
                "article_style": c.get("article_style") or "",
                "angle": c.get("angle") or "",
                "current_title": c.get("title") or "",
                "why": c["reason"],
            }
            for c in conflicts
        ]
        prompt = (
            "以下标题与同批/同项目已有标题重复或套用了同一公式，请为每一条换一个**结构不同**的新标题。\n\n"
            "【必须遵守】\n"
            "1. 🔴 绝对不许更换客户购买的问题、地区、产品或采购意图——"
            "`purchased_keyword` 必须原样体现在新标题里，只能换表达结构与角度；\n"
            "2. 新标题之间、以及与下方【已用标题】都不得重复，也不要套同一个句式模板；\n"
            "3. 不要为了求新加入夸大或绝对化用语（最好/第一/唯一等）；\n"
            "4. 只返回 JSON 数组：[{\"id\": 原id, \"title\": \"新标题\"}]，不要解释。\n\n"
            f"【已用标题（需避开）】\n{json.dumps(used_titles[:120], ensure_ascii=False)}\n\n"
            f"【待换标题】\n{json.dumps(wanted, ensure_ascii=False, indent=2)}\n"
        )
        try:
            from .llm_utils import get_thinking_disabled_params

            body = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                # 温度略高于主链(0.7):这一次的目的就是"换个说法",要的是发散。
                "temperature": 0.9,
                "max_tokens": 2000,
            }
            body.update(get_thinking_disabled_params(api_url, model))
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(
                    api_url,
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json=body,
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
            import json_repair

            parsed = json_repair.loads(content)
            out: Dict[int, List[str]] = {}
            for item in parsed if isinstance(parsed, list) else []:
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                title = str(item.get("title") or "").strip()
                if title:
                    out.setdefault(idx, []).append(title)
            return out
        except Exception as exc:
            print(f"  ⚠️ 冲突标题重生成失败,改用结构化兜底: {exc}")
            return {}

    def _split_defensive(self, topics: List[Dict]) -> tuple:
        """把防御槽从「整批形态处理」里摘出来,返回 `(其余, 防御的)`。

        [#185 c3] 防御题的形态是 WO_185 §6 **逐字定死的八问**,不参加整批抽签:

        · 问句配比改写(`enforce_question_ratio`)会把这一篇的
          `original_keyword` 当锚点续到标题后面 —— 但防御题的商业对象是
          **品牌**,不是那个关键词,续上去得到的是
          「<品牌>怎么样原关键词1适合哪些场景?」这种病句;
          反向的"回落"更糟:它会把八问中的问句改成陈述句,
          于是「每问恰好一题」在标题上就看不出来了。
        · 批次去重改写会**悄悄改标题文本、却仍然标 `source='llm'`** ——
          而防御槽的去重本来就有自己那一处(`vet_llm_titles`:重复的落模板题
          并逐条标 `source`)。同一个谓词留两处,赢的那处会改掉另一处的读数。

        🔴 摘出去**不等于不管**:防御槽的把关全部集中在 `vet_llm_titles`。
        """
        if not self.defensive_plan:
            return list(topics or []), []
        from writing.title_keyword_alignment import normalize_semantic_text

        keys = {(normalize_semantic_text(_d.get("keyword")), int(_d.get("slot_index") or 0))
                for _d in self.defensive_plan}
        rest, defensive = [], []
        for _t in (topics or []):
            if not isinstance(_t, dict):
                rest.append(_t)
                continue
            _k = (normalize_semantic_text(_t.get("original_keyword")),
                  int(_t.get("slot_index") or 0))
            (defensive if _k in keys else rest).append(_t)
        return rest, defensive

    async def _apply_batch_title_dedupe(self, topics: List[Dict]) -> List[Dict]:
        """[P4 · 工单 §7] 落库前最后一道:批次级标题多样性收敛。

        🔴 顺序:排在 `_apply_title_question_policy` **之前**。
        我第一版写反了(先形态收敛、再去重),被回归 A/B 当场抓出两个真错误:
          1. 去重把标题换成兜底模板,**打乱刚刚收敛好的问句式比例**
             (`test_t1_lock1_*` 由 7/10 变 9/10,超出 70%±容差);
          2. 用户显式指定 `title_form='open'` 时形态层会早退不介入,而去重仍然把
             标题换成了**问句式**兜底模板 —— 直接违反"用户 > 默认"铁律。
        所以形态层必须是**最后一句话**。去重可能重新引入的碰撞,由
        `_finalize_titles` 在形态收敛后做**只探测不改写**的复核并如实记账
        (改写会再次打乱比例,所以只报不改)。
        """
        from writing.title_batch_dedupe import (
            detect_title_conflicts,
            dedupe_topic_titles,
        )

        # [#185 c3] 防御槽不进整批去重改写(理由见 `_split_defensive`)。
        topics, _defensive_topics = self._split_defensive(topics)
        try:
            existing = self._existing_quote_titles()
            conflicts = detect_title_conflicts(topics, existing_titles=existing)
            candidates: Dict[int, List[str]] = {}
            if conflicts:
                used_titles = list(existing) + [
                    str(t.get("optimized_title") or "") for t in topics
                    if isinstance(t, dict) and t.get("optimized_title")
                ]
                candidates = await self._regenerate_conflicting_titles(
                    topics, conflicts, used_titles,
                )
            report = dedupe_topic_titles(
                topics,
                existing_titles=existing,
                candidates_by_index=candidates,
                # 用户显式指定形态时形态层会早退不介入 → 去重必须自己保住形态,
                # 否则等于绕过形态层改掉用户的显式选择(回归 A/B 抓到的真 bug)。
                preserve_form=bool(self.title_form),
            )
            report["llm_regenerated_offered"] = sum(len(v) for v in candidates.values())
            self.title_dedupe_report = report
            if report.get("conflicts"):
                print(
                    "  🔁 标题批次去重: "
                    f"冲突 {report['conflicts']} · 已重生成 {report['regenerated']} "
                    f"· 未能分化 {report['unresolved']} "
                    f"· 身份保护否决 {report['identity_rejected']} "
                    f"· 明细 {report.get('by_reason')}"
                )
        except Exception as exc:  # 去重永不阻断选题产出
            print(f"  ⚠️ 标题批次去重失败,保留原标题: {exc}")
            self.title_dedupe_report = {"error": str(exc)[:200]}
        return topics + _defensive_topics

    def _finalize_titles(self, topics: List[Dict]) -> List[Dict]:
        """形态收敛(最后一句话)+ 收敛后碰撞**只探测不改写**的复核。

        为什么只探测:形态层刚把问句式比例收敛到目标,这里再改写标题就会把比例
        重新打乱(第一版正是这么红的)。所以把"改写后是否又撞上"如实记进报告,
        交由运营/后续包处理,**不静默假装为 0**。
        """
        topics = self._apply_title_question_policy(topics)
        try:
            from writing.title_batch_dedupe import detect_title_conflicts

            residual = detect_title_conflicts(topics)
            if isinstance(self.title_dedupe_report, dict):
                self.title_dedupe_report["post_policy_conflicts"] = len(residual)
            if residual:
                print(
                    f"  ℹ️ 形态收敛后仍有 {len(residual)} 条标题公式相近"
                    "(只记账不改写,避免再次打乱问句式比例)"
                )
        except Exception:
            pass
        return topics

    def _apply_title_question_policy(self, topics: List[Dict]) -> List[Dict]:
        """[工单 T1 2026-07-29] topics 落库前的唯一形态收敛点。

        为什么放在**全部批次汇总之后**而不是每批之内:比例是"该批标题"的比例,
        而 `_chunk_by_titles` 会把一次请求切成多批(每批 ≤30 条)。逐批收敛会让
        每批各自凑 70%,小批次的取整误差叠加后整单会明显偏离目标。

        LLM 路径与模板兜底路径共用这一个入口 —— 兜底标题同样会进生产,
        不能只给在线路径上锁(本仓已有"兜底比在线更宽松"的历史教训)。
        """
        from writing.title_question_policy import enforce_question_ratio

        # [#185 c3] 防御槽不参加问句配比抽签(理由见 `_split_defensive`)。
        # 🔴 也要把它们从**分母**里去掉:留在分母里会让配比按更大的总数分配
        #    名额,于是普通篇被多改写几条去凑一个它们并不承担的比例。
        topics, _defensive_topics = self._split_defensive(topics)
        for _t in _defensive_topics:
            _t.setdefault("title_form_source", "defensive_fixed_form")
        try:
            self.title_question_report = enforce_question_ratio(
                topics,
                title_key="optimized_title",
                keyword_key="original_keyword",
                industry=self.industry,
                ratio_percent=self.question_ratio_percent,
                user_specified=bool(self.title_form),
            )
            report = self.title_question_report or {}
            print(
                "  🧭 标题形态配比: "
                f"{report.get('question_after')}/{report.get('total')} 问句式 "
                f"(目标 {report.get('ratio_percent')}% · 提升 {report.get('promoted')} "
                f"· 回落 {report.get('demoted')} · 用户指定={report.get('user_specified')})"
            )
        except Exception as exc:  # 形态是运营偏好,永不阻断选题产出
            print(f"  ⚠️ 标题形态配比执行失败,保留原标题: {exc}")
            self.title_question_report = {"error": str(exc)[:200]}
        return topics + _defensive_topics


    def _chunk_keywords(self, size: int) -> List[List[Dict]]:
        """将关键词分批(按关键词数量)"""
        return [self.keywords[i:i+size] for i in range(0, len(self.keywords), size)]
    
    def _chunk_by_titles(self, max_titles_per_batch: int = 30) -> List[List[Dict]]:
        """智能分批: 按标题总数分批,避免LLM输出超上下文"""
        batches = []
        current_batch = []
        current_count = 0
        
        for kw in self.keywords:
            titles_needed = _required_article_count(kw)
            if titles_needed <= 0:
                continue
            
            # 如果当前批次加上这个关键词会超过限制,先保存当前批次
            if current_count + titles_needed > max_titles_per_batch and current_batch:
                batches.append(current_batch)
                current_batch = []
                current_count = 0
            
            current_batch.append(kw)
            current_count += titles_needed
        
        # 添加最后一批
        if current_batch:
            batches.append(current_batch)
        
        return batches
    
    async def _generate_batch(
        self,
        batch: List[Dict],
        api_url: str,
        api_key: str,
        model: str
    ) -> List[Dict]:
        """生成一批标题"""
        # 构建输入
        keyword_list = []
        for kw in batch:
            count = _required_article_count(kw)
            entry = {
                "keyword": kw.get("keyword"),
                "required_articles": count,
                "keyword_id": kw.get("id"),
            }
            if kw.get("cluster_id"):
                entry["cluster_name"] = kw.get("cluster_name", "")
            keyword_list.append(entry)
        
        # 检测同一主题包内的关键词，提示LLM避免重复内容
        cluster_groups = {}
        for kw in batch:
            cid = kw.get("cluster_id")
            cname = kw.get("cluster_name", "")
            if cid:
                cluster_groups.setdefault(cid, {"name": cname, "keywords": []})
                cluster_groups[cid]["keywords"].append(kw.get("keyword"))

        cluster_hint = ""
        if cluster_groups:
            lines = []
            for cid, info in cluster_groups.items():
                kw_str = "、".join(info["keywords"])
                lines.append(f"  主题包「{info['name']}」: {kw_str}")
            cluster_hint = f"""
【⚠️ 同主题包关键词去重】
以下关键词属于同一主题包，它们的文章会一起发布。请确保同包内不同关键词的标题角度互不重复：
{chr(10).join(lines)}
"""

        # v2.10 style_plan per-topic 注入(优先级高于 force_chinese_style)
        # Codex 二审 P1-4 + v2.10.2 P0-3:LLM 返回 keyword + slot_index 严格匹配 · 防数组顺序错位
        _style_plan_hint = ""
        if self.style_plan:
            from writing.direction_distribution import STYLE_CODE_TO_USER_CHOICE
            try:
                from writing.style_registry import USER_CHOICE_TO_STYLE, STYLE_CODE_TO_CHINESE_NAME
            except Exception:
                USER_CHOICE_TO_STYLE = {}
                STYLE_CODE_TO_CHINESE_NAME = {}

            plan_lines = []
            kw_groups: Dict[str, List[Dict]] = {}
            for p in self.style_plan:
                kw_id = p.get("keyword_id")
                kw_name = next((k.get("keyword") for k in self.keywords if k.get("id") == kw_id), f"kw_{kw_id}")
                kw_groups.setdefault(kw_name, []).append(p)

            for kw_name, plan_items in kw_groups.items():
                plan_items_sorted = sorted(plan_items, key=lambda x: x.get("slot_index", 0))
                styles_for_kw = []
                for p in plan_items_sorted:
                    uc = p.get("user_choice", "")
                    style_code = USER_CHOICE_TO_STYLE.get(uc)
                    chinese_name = STYLE_CODE_TO_CHINESE_NAME.get(style_code, uc) if style_code else uc
                    styles_for_kw.append(f"slot_{p.get('slot_index')}={chinese_name}")
                plan_lines.append(f"  关键词「{kw_name}」: {', '.join(styles_for_kw)}")

            _plan_title = "用户自定义六类文体配比" if self.style_plan_mode == "custom" else "系统推荐六类文体配比"
            _style_plan_hint = f"""
【⚠️ {_plan_title} per-topic plan】
本次每条标题已分配到具体文体 · 严格按下表执行 · 不要自由分配:
{chr(10).join(plan_lines)}
返回 JSON 中每个 topic 必须保留 `original_keyword` 字段(等于上表关键词文本)·
按每个 keyword 下 slot_0/slot_1/... 顺序生成对应风格的标题
article_style 字段必须按上表风格写入
忽略默认系统推荐均衡分布 · 忽略 force_chinese_style(v2.10 plan 优先级最高)
"""

        # ── [#185 c3] 防御型(公司词)提示块 ────────────────────────────
        # Owner 2026-09-13:「防御型公司词的标题生成也需要经过 AI,
        #   要蒸馏一下研究一下防御型的这些文章怎么写。」
        #
        # 🔴 **问名不交给 LLM 挑**:它由上游 `plan_defensive_titles` 定死,
        #    这里只把"这一槽要写哪一问"告诉它。让 LLM 自己挑问名的话,
        #    「八问覆盖」就没有分母了 —— 它会挑好写的那几问,
        #    而用户看到的只有"防御型"三个字。
        # 🔴 品牌名**必须出现在标题里**:主语丢了就不是公司词。
        #    这里说一遍,回来之后 `vet_llm_titles` 还要再校一遍 ——
        #    提示是请求,把关才是保证。
        _defensive_hint = ""
        if self.defensive_plan:
            # 🔴 [c3''] 上限取 `defensive_questions._MAX_TITLE_CHARS`,不写字面量。
            #    这里是**告诉 LLM** 的上限,`vet_llm_titles` 是**把关**用的上限 ——
            #    两处各写一个 48,改一处另一处静默不同意:提示说 480、把关仍按 48,
            #    结果是 LLM 照着 480 写、回来全被判 too_long 落模板,而没有任何东西报错。
            #    (Review 09-15 毒 X2 实测:48→480 全包绿 —— 数值那一侧没人钉。)
            from writing.defensive_questions import _MAX_TITLE_CHARS

            _d_lines = []
            for _d in sorted(self.defensive_plan,
                             key=lambda x: (str(x.get("keyword") or ""),
                                            int(x.get("slot_index") or 0))):
                _angles = [str(a) for a in (_d.get("angles") or []) if str(a).strip()]
                _angle_text = ("参考角度: " + " / ".join(_angles[:4])) if _angles else                     "(本品牌暂无蒸馏角度,按这一问的常识写)"
                _d_lines.append(
                    "  关键词「%s」slot_%s: 问「%s」· %s"
                    % (_d.get("keyword"), _d.get("slot_index"),
                       _d.get("question"), _angle_text))
            _defensive_hint = """
【⚠️ 防御型(公司词)槽位 · 题的主语是公司,不是关键词】
下列槽位的标题必须围绕**公司本身**写,每一槽写**指定的那一问**:
%s
硬性要求(违反的那一条会被退回并落回模板题):
1. 标题里**必须出现品牌名「%s」** —— 主语是公司,不是行业词
2. 每槽只写**上表指定的那一问**,不要自己换问法、不要合并两问
3. **不点名、不排名同行**;可以讲差异,不许讲"比 X 好"
4. 一句话说清,不堆形容词;标题不超过 %d 字
""" % (chr(10).join(_d_lines), self.brand_name, _MAX_TITLE_CHARS)

        # v2.8 force_chinese_style 段:用户在选题阶段选了文体 → 强制全部标题用同一风格
        # v2.10:若 style_plan 已设 · force_chinese_style 跳过(plan 优先)
        _force_style_hint = ""
        if self.force_chinese_style and not self.style_plan:
            _force_style_hint = f"""
【⚠️ 用户强制文体(v2.8)】
本次所有标题必须使用风格:**{self.force_chinese_style}**
所有 topic 的 article_style 字段必须 = "{self.force_chinese_style}" · 不要分配其他风格
标题模板严格按 system prompt 中"{self.force_chinese_style}"对应的格式生成
忽略默认的系统推荐均衡分布 · 本次用户已主动选定单一文体
"""

        user_message = f"""品牌名称: {self.brand_name}
行业: {self.industry}
{f'''
【📚 客户专属知识库参考】
以下是客户的专业知识,生成标题时请融入这些专业术语和核心卖点:
{self.knowledge_context}
''' if self.knowledge_context else ''}{cluster_hint}{_style_plan_hint}{_defensive_hint}{_force_style_hint}
请为以下关键词生成优化标题(每个关键词生成对应篇数的不同角度标题):

{json.dumps(keyword_list, ensure_ascii=False, indent=2)}
"""
        
        # [P1-1 2026-08-14] D6-B 监测缺口段:数据不足/查询失败 → 空串,
        # system prompt 与旧版逐字一致(A1;依赖 P0-3 覆盖率,数据上来自然生效)。
        _gap_block = ""
        try:
            from services.reco_outcome_feedback import build_title_gap_block

            _gap_block = build_title_gap_block(int(getattr(self, "brand_id", 0) or 0))
        except Exception as _gap_err:  # noqa: BLE001
            print(f"    ⚠️ [P1-1] 缺口段生成失败,按无缺口继续: {_gap_err}")

        # [标题 AI-only 2026-08-17 · 工单 §1.3] 意图适配段:不适配的族让模型**别出题**。
        # 空串 → system prompt 与旧版逐字一致(判别测试打这一点)。
        _intent_block = ""
        try:
            from writing.title_intent_style_gate import build_intent_style_constraint

            _intent_block = build_intent_style_constraint(batch)
        except Exception as _intent_err:  # noqa: BLE001
            print(f"    ⚠️ 意图适配段生成失败,按无约束继续: {_intent_err}")

        messages = [
            {"role": "system", "content": _build_title_generator_prompt(
                self.industry, gap_block=_gap_block, intent_block=_intent_block,
            )},
            {"role": "user", "content": user_message}
        ]

        from .llm_utils import get_thinking_disabled_params

        # 第1次：主模型
        main_body = {
            "model": model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 8000
        }
        main_body.update(get_thinking_disabled_params(api_url, model))
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                response = await client.post(
                    api_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json=main_body
                )
                response.raise_for_status()
                result = response.json()
                content = result["choices"][0]["message"]["content"]
                return self._parse_response(content, batch)

        except Exception as e:
            print(f"⚠️ 主模型批量生成失败({model}): {e}")

        # 第2次：兜底模型 deepseek
        if self._fb_api_key:
            try:
                print(f"🔄 切换兜底模型({self._fb_provider}/{self._fb_model})")
                fb_body = {
                    "model": self._fb_model,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 8000
                }
                fb_body.update(get_thinking_disabled_params(self._fb_api_url, self._fb_model))
                async with httpx.AsyncClient(timeout=180.0) as client:
                    response = await client.post(
                        self._fb_api_url,
                        headers={
                            "Authorization": f"Bearer {self._fb_api_key}",
                            "Content-Type": "application/json"
                        },
                        json=fb_body
                    )
                    response.raise_for_status()
                    result = response.json()
                    content = result["choices"][0]["message"]["content"]
                    topics = self._parse_response(content, batch)
                    print(f"✅ 兜底模型标题生成成功: {len(topics)} 个")
                    return topics
            except Exception as e2:
                print(f"⚠️ 兜底模型也失败({self._fb_model}): {e2}")

        # [标题 AI-only 2026-08-17] 两个模型都失败 → **不再降级到模板**。
        # 产出待生成槽位,交 `_resolve_pending_titles` 走 AI 失败梯;梯子也失败就显式失败。
        print("⚠️ 主模型+兜底模型均失败 · 标题只能由 AI 产出 → 交 AI 失败梯")
        return self._pending_batch(batch, reason="both_models_failed")
    
    def _collect_ai_surplus(self, keyword: str, title: object) -> None:
        """[失败梯 ①] 收一条"合格但放不下"的 AI 候选,按**关键词原文**分组。

        只在同一关键词内复用 —— 跨关键词补位会把 A 的标题安到 B 头上,
        那是身份红线,比"没有标题"严重得多。
        """
        text = str(title or "").strip()
        keyword_text = str(keyword or "").strip()
        if not text or not keyword_text:
            return
        pool = self._ai_surplus_titles.setdefault(keyword_text, [])
        if text not in pool and len(pool) < 20:
            pool.append(text)

    @staticmethod
    def _topic_key(topic: Dict) -> tuple:
        keyword_id = topic.get("keyword_id")
        identity = ("id", str(keyword_id)) if keyword_id is not None else (
            "text",
            str(topic.get("original_keyword") or "").strip(),
        )
        return identity, int(topic.get("slot_index") or 0)

    def _parse_response(self, content: str, batch: List[Dict]) -> List[Dict]:
        """Parse, bind and align model topics against authoritative keywords.

        Compatibility invariant retained from v2.10.2::
        ``if "slot_index" not in topic:`` uses ``_kw_seen_count`` to rebuild
        the per-keyword position before any topic can be persisted.
        """

        try:
            import re
            from writing.title_keyword_alignment import (
                TITLE_KEYWORD_ALIGNMENT_VERSION,
                assess_title_keyword_alignment,
                normalize_semantic_text,
            )

            json_match = re.search(r'\{[\s\S]*\}', content)
            if not json_match:
                return self._pending_batch(batch, reason="model_output_unparsable")
            data = json.loads(json_match.group())
            topics = data.get("topics", [])
            if not isinstance(topics, list):
                return self._pending_batch(batch, reason="model_output_unparsable")

            by_id = {
                str(item.get("id")): item
                for item in batch
                if item.get("id") is not None
            }
            by_text_candidates: Dict[str, List[Dict]] = {}
            for item in batch:
                normalized_text = normalize_semantic_text(item.get("keyword"))
                if normalized_text:
                    by_text_candidates.setdefault(normalized_text, []).append(item)

            def _unique_keyword_by_text(value: Any) -> Dict | None:
                candidates = by_text_candidates.get(normalize_semantic_text(value), [])
                return candidates[0] if len(candidates) == 1 else None

            _kw_seen_count: Dict[tuple, int] = {}
            used_slots = set()
            plan_lookup = {
                (str(item.get("keyword_id")), int(item.get("slot_index", 0))): item
                for item in (self.style_plan or [])
            }
            # ── [#185 c3] 防御槽:关键词身份复核**不适用** ────────────────
            # 🔴 这道复核问的是「标题还在不在客户买的那个商业对象上」。
            #    防御型(公司词)的商业对象是**品牌自己**,不是这一篇的
            #    `original_keyword` —— 题面就是「<品牌>怎么样?」。拿关键词去
            #    量它,**每一条都判不过**,于是每一槽都落模板题:c3 在生产上
            #    会表现成"接了 AI 但产出跟以前一模一样",而且全程零报错。
            # 🔴 不是"放开不管":防御槽的把关在 `vet_llm_titles`(品牌名必入 /
            #    长度 / 跨批去重),那里**恰好**是 c3 定的那一道。
            #    同一个谓词只留一处 —— 两处判"有没有品牌名",赢的那处会把
            #    `fallback_reason` 改成另一个词,而前端按 reason 显示。
            _defensive_slots = {
                (normalize_semantic_text(_d.get("keyword")), int(_d.get("slot_index") or 0))
                for _d in (self.defensive_plan or [])
            }
            result = []

            for raw_topic in topics:
                if not isinstance(raw_topic, dict):
                    continue
                topic = dict(raw_topic)
                _llm_kw_id = topic.get("keyword_id")
                supplied_keyword = topic.get("original_keyword") or topic.get("keyword")
                identity_conflict = False
                kw = by_id.get(str(_llm_kw_id)) if _llm_kw_id is not None else None
                if _llm_kw_id is not None and kw is None:
                    kw = _unique_keyword_by_text(supplied_keyword) if supplied_keyword else None
                    if kw is None:
                        continue
                    identity_conflict = True
                elif _llm_kw_id is None and supplied_keyword:
                    kw = _unique_keyword_by_text(supplied_keyword)

                # If both identity fields are present they must agree. A correct
                # ID may not launder a title generated for another keyword. We
                # keep the authoritative ID/slot only to replace its unsafe text
                # with the local fallback, never to save the crossed title.
                identity_conflict = identity_conflict or bool(
                    kw is not None
                    and supplied_keyword
                    and normalize_semantic_text(supplied_keyword)
                    != normalize_semantic_text(kw.get("keyword"))
                )
                if kw is None and len(batch) == 1 and _llm_kw_id is None and not supplied_keyword:
                    kw = batch[0]
                if kw is None:
                    continue

                keyword_id = kw.get("id")
                canonical_keyword = str(kw.get("keyword") or "").strip()
                topic["keyword_id"] = keyword_id
                if not topic.get("original_keyword"):
                    topic["original_keyword"] = kw.get("keyword")
                # The model copy is never authoritative, even when it was
                # present and matched after normalization.
                topic["original_keyword"] = canonical_keyword
                identity = ("id", str(keyword_id)) if keyword_id is not None else (
                    "text",
                    normalize_semantic_text(kw.get("keyword")),
                )
                expected_count = _required_article_count(kw)
                if "slot_index" not in topic:
                    slot_index = _kw_seen_count.get(identity, 0)
                    while (identity, slot_index) in used_slots:
                        slot_index += 1
                else:
                    try:
                        slot_index = int(topic.get("slot_index"))
                    except (TypeError, ValueError):
                        continue
                # [标题 AI-only 2026-08-17 · 失败梯 ①] 放不下的槽位(超出合同篇数 /
                # 该 slot 已被占)如果标题本身是合格 AI 产出,收进**同关键词**的候选池
                # 备用,而不是直接丢。这是"同批次已生成的合格 AI 候选补位"的供给方。
                if (slot_index < 0 or slot_index >= expected_count
                        or (identity, slot_index) in used_slots):
                    self._collect_ai_surplus(
                        canonical_keyword,
                        topic.get("optimized_title") or topic.get("title") or "",
                    )
                    continue
                slot_key = (identity, slot_index)
                used_slots.add(slot_key)
                _kw_seen_count[identity] = slot_index + 1

                topic["slot_index"] = slot_index
                if kw.get("cluster_id"):
                    topic["cluster_id"] = kw["cluster_id"]

                from writing.style_registry import (
                    STYLE_CODE_TO_CHINESE_NAME,
                    normalize_style_code,
                    resolve_user_choice_to_chinese_style,
                )
                planned = plan_lookup.get((str(keyword_id), slot_index))
                if planned:
                    planned_choice = planned.get("user_choice")
                    topic["user_choice"] = planned_choice
                    topic["user_choice_source"] = planned.get("user_choice_source")
                    topic["article_style"] = (
                        resolve_user_choice_to_chinese_style(planned_choice, self.industry)
                        or "证据型问答"
                    )
                else:
                    normalized_style = normalize_style_code(topic.get("article_style"))
                    topic["article_style"] = STYLE_CODE_TO_CHINESE_NAME.get(
                        normalized_style or "qa_recommendation",
                        "证据型问答",
                    )

                from writing.evidence_first_policy import rewrite_legacy_ranking_title
                generated_title = "" if identity_conflict else rewrite_legacy_ranking_title(
                    topic.get("optimized_title") or topic.get("title") or "",
                    canonical_keyword,
                )
                _is_defensive = (
                    (normalize_semantic_text(canonical_keyword), slot_index)
                    in _defensive_slots)
                if _is_defensive and not identity_conflict:
                    # 原样交给下游的 `vet_llm_titles` 把关(见上方说明)。
                    topic["optimized_title"] = generated_title
                    topic["title_alignment_status"] = "defensive_exempt"
                    topic["title_alignment_reason"] = "defensive_company"
                    topic["title_alignment_version"] = TITLE_KEYWORD_ALIGNMENT_VERSION
                    from writing.title_ai_only import TITLE_ORIGIN_PRIMARY

                    topic["title_origin"] = TITLE_ORIGIN_PRIMARY
                    result.append(topic)
                    continue

                # [WO_232] 把品牌注册名交给复核器:关键词核就是这家公司的注册名时,
                # 标题里的自然缩写(丢「有限公司」/丢省名)算同一商业对象。
                # 不传就是老行为 —— 放宽只在"这家公司自己的名字"上成立。
                verdict = assess_title_keyword_alignment(
                    generated_title, canonical_keyword,
                    brand_names=(self.brand_name,) if self.brand_name else (),
                )
                if verdict.aligned:
                    topic["optimized_title"] = generated_title
                    topic["title_alignment_status"] = "passed"
                    from writing.title_ai_only import TITLE_ORIGIN_PRIMARY

                    topic["title_origin"] = TITLE_ORIGIN_PRIMARY
                else:
                    # [标题 AI-only 2026-08-17] 旧实现在这里塞硬编码模板
                    # (`_safe_fallback_title`),生产实测正是候选池里那 4 条模板串的来源。
                    # 现在:该槽位标记为待生成,交 `_resolve_pending_titles` 走 AI 失败梯;
                    # 梯子全失败就显式失败,**不用模板顶**。
                    topic["optimized_title"] = ""
                    topic["title_alignment_status"] = "pending_ai"
                    topic["title_generation_status"] = "pending_ai"
                    topic["title_pending_reason"] = (
                        "keyword_identity_conflict" if identity_conflict else verdict.reason
                    )
                    print(
                        "⚠️ 标题未过关键词身份复核 → 交 AI 失败梯(不用模板) "
                        f"keyword_id={keyword_id} slot={slot_index} "
                        f"reason={'keyword_identity_conflict' if identity_conflict else verdict.reason}"
                    )
                topic["title_alignment_reason"] = (
                    "keyword_identity_conflict" if identity_conflict else verdict.reason
                )
                topic["title_alignment_version"] = TITLE_KEYWORD_ALIGNMENT_VERSION
                result.append(topic)

            # [标题 AI-only 2026-08-17 · 范围说明] 模型**漏返**的槽位仍按既有行为
            # 静默少给,本包不动。我试过在这里补待生成槽位,当场撞上本文件既有的
            # 9 条 fail-closed 锁(身份冲突不得建 topic / 显式 0 篇不得建 topic /
            # 重复与越界槽不得跨写 …)—— 那些锁是真事故换来的,而"漏返静默少给"
            # 是工单范围之外的既有行为。改它要单独立项,不搭本包的车。

            # 意图适配地板:模型没听 prompt 时,族标签就地改判(只改标签,不改标题文本)。
            from writing.title_intent_style_gate import enforce_topic_intent_fit

            return enforce_topic_intent_fit(result)
        except Exception as exc:
            print(f"⚠️ 标题语义绑定解析失败 · 交 AI 失败梯(不使用模板): {exc}")
            return self._pending_batch(batch, reason="parse_failed")
    
    def _pending_generate(self) -> List[Dict]:
        """整单没有任何 AI 产出时的**待生成**槽位(不是标题)。"""
        return self._pending_batch(self.keywords, reason="llm_unavailable")

    def _pending_batch(self, batch: List[Dict], *, reason: str = "llm_failed") -> List[Dict]:
        """[标题 AI-only 2026-08-17] 取代 `_fallback_batch`。

        旧实现在 provider 失败时用 6 族 × 3 条硬编码模板**直接造出标题**,
        那批标题会原样落库、进候选池、被客户看到 —— 生产实测 15 条候选里
        4 条与模板表逐字对应。Owner 2026-08-17 裁决:标题必须 AI 生成,
        产不出来就显式失败。

        所以这里只产**待生成槽位**:`optimized_title` 留空,
        `title_generation_status='pending_ai'`。真正的标题由
        `_resolve_pending_titles` 走 `title_ai_only` 三级梯去要;
        梯子全失败的槽位会被丢弃并计入 `title_failure_report`,
        **不硬凑满数**。
        """
        pending: List[Dict] = []

        # 文体族仍按既有计划走(计划本身已过意图闸)。计划缺失时按六族轮转,
        # 与旧实现同口径 —— 这一层决定的是"回答角度",不产任何标题文本。
        from writing.article_style_contract import STYLE_FAMILIES

        _balanced_styles = [family.name for family in STYLE_FAMILIES.values()]

        _planned_styles: Dict[tuple, str] = {}
        _planned_choices: Dict[tuple, Dict] = {}
        if self.style_plan:
            from writing.style_registry import resolve_user_choice_to_chinese_style
            for planned in self.style_plan:
                key = (planned.get("keyword_id"), int(planned.get("slot_index", 0)))
                try:
                    planned_style = resolve_user_choice_to_chinese_style(
                        planned.get("user_choice"), self.industry
                    )
                except ValueError:
                    planned_style = None
                if planned_style:
                    _planned_styles[key] = planned_style
                    _planned_choices[key] = planned

        from writing.title_intent_style_gate import reassign_family

        for kw in batch:
            count = _required_article_count(kw)
            keyword = kw.get("keyword", "")
            style_index = 0
            for i in range(count):
                planned_style = _planned_styles.get((kw.get("id"), i))
                if planned_style:
                    style = planned_style
                else:
                    style = _balanced_styles[style_index % len(_balanced_styles)]
                    style_index += 1
                # 意图闸同样作用在无计划路径上:不适配的族不出题。
                style = reassign_family(keyword, style, slot_index=i)

                topic_entry = {
                    "keyword_id": kw.get("id"),
                    "original_keyword": keyword,
                    "optimized_title": "",
                    "article_style": style,
                    "slot_index": i,
                    "angle": f"角度{i+1}",
                    "title_generation_status": "pending_ai",
                    "title_pending_reason": reason,
                }
                planned_choice = _planned_choices.get((kw.get("id"), i))
                if planned_choice:
                    topic_entry["user_choice"] = planned_choice.get("user_choice")
                    topic_entry["user_choice_source"] = planned_choice.get("user_choice_source")
                if kw.get("cluster_id"):
                    topic_entry["cluster_id"] = kw["cluster_id"]
                pending.append(topic_entry)

        return pending

    async def _resolve_pending_titles(self, topics: List[Dict]) -> List[Dict]:
        """[标题 AI-only 2026-08-17 · 工单 §1.2] 失败梯的唯一执行点。

        ① 同批已生成的合格 AI 候选补位(同一关键词内、过身份复核)
        ② AI 重试(降级 prompt · 主模型 → 兜底模型,型号走 model config)
        ③ 仍失败 = 该槽位显式失败 → **从产出里剔除**,计入 `title_failure_report`

        ③ 不是"静默少给":上层(server.py / optimize_title_jobs)会把
        `title_failure_report` 转成用户看得见的人话与重试入口。
        """
        from writing.title_ai_only import (
            TITLE_ALIGNMENT_FAILURE_CODE,
            TITLE_FAILURE_CODE,
            TITLE_FAILURE_USER_MESSAGE,
            TITLE_ORIGIN_PRIMARY,
            TitleSlotRequest,
            alignment_failure_user_message,
            resolve_titles_ai_only,
        )

        items = [t for t in (topics or []) if isinstance(t, dict)]
        requested = len(items)
        pending_index = [
            i for i, t in enumerate(items)
            if not str(t.get("optimized_title") or "").strip()
        ]
        for i, topic in enumerate(items):
            if i not in set(pending_index) and not topic.get("title_origin"):
                topic["title_origin"] = TITLE_ORIGIN_PRIMARY

        if not pending_index:
            self.title_failure_report = {
                "requested": requested, "produced": requested, "failed": 0,
                "failed_slots": [],
            }
            return items

        avoid = [
            str(t.get("optimized_title") or "") for t in items
            if str(t.get("optimized_title") or "").strip()
        ]
        requests = [
            TitleSlotRequest(
                key=i,
                keyword=str(items[i].get("original_keyword") or ""),
                article_style=str(items[i].get("article_style") or ""),
                angle=str(items[i].get("angle") or ""),
                note=str(items[i].get("title_pending_reason") or ""),
            )
            for i in pending_index
        ]
        ladder = await resolve_titles_ai_only(
            requests,
            brand_name=self.brand_name,
            industry=self.industry,
            surplus_pool=self._ai_surplus_titles,
            avoid_titles=avoid,
        )

        failed_slots: List[Dict] = []
        resolved: List[Dict] = []
        for i, topic in enumerate(items):
            if i in ladder.titles:
                topic["optimized_title"] = ladder.titles[i]
                topic["title_origin"] = ladder.origins.get(i)
                topic["title_generation_status"] = "ai_recovered"
                topic.pop("title_pending_reason", None)
                resolved.append(topic)
            elif not str(topic.get("optimized_title") or "").strip():
                # ③ 显式失败:不硬凑满数,该槽位就是没有标题。
                #
                # [WO_232] 这一槽是**哪一种**失败?两个信源,任一成立即"复核拒绝":
                #   · 梯子里模型给了标题、被复核拒掉(ladder.alignment_rejected);
                #   · 进梯子之前主链就已经因复核不过而挂起(title_pending_reason)。
                # 都不成立才是真的「AI 不可用」。
                #
                # 🔴 `title_pending_reason` 必须按**具名集合**判,不能"非空即复核拒绝":
                #    模型什么都没给时这里是 `empty_title` —— 那是 AI 不可用,
                #    不是我们拒了他。第一版就是这么写的,被既有判据 G1
                #    (LLM 全死那条)当场打红。
                from writing.title_keyword_alignment import ALIGNMENT_REJECTION_REASONS

                _alignment_reasons = ALIGNMENT_REJECTION_REASONS | {"keyword_identity_conflict"}
                _pending_reason = str(topic.get("title_pending_reason") or "")
                _rejected_by_alignment = bool(
                    ladder.alignment_rejected.get(i)
                    or _pending_reason in _alignment_reasons
                )
                failed_slots.append({
                    "keyword_id": topic.get("keyword_id"),
                    "keyword": topic.get("original_keyword"),
                    "slot_index": topic.get("slot_index"),
                    "article_style": topic.get("article_style"),
                    "failure_kind": (
                        "alignment_rejected" if _rejected_by_alignment else "ai_unavailable"),
                    "alignment_reason": _pending_reason or None,
                    "rejected_titles": list(ladder.alignment_rejected.get(i) or [])[:3],
                })
            else:
                resolved.append(topic)

        _alignment_failed = [s for s in failed_slots
                             if s.get("failure_kind") == "alignment_rejected"]
        if not failed_slots:
            _code, _message = None, None
        elif _alignment_failed:
            # 🔴 只要有一条是被我们自己拒的,整批就不能再说「AI 不可用」——
            #    那句话会让用户去猜,而真正该做的是保留完整关键词重试或手动编辑。
            _code = TITLE_ALIGNMENT_FAILURE_CODE
            _message = alignment_failure_user_message(
                _alignment_failed[0].get("keyword"),
                1 if ladder.retry_attempted else 0,
            )
        else:
            _code, _message = TITLE_FAILURE_CODE, TITLE_FAILURE_USER_MESSAGE

        self.title_failure_report = {
            "requested": requested,
            "produced": len(resolved),
            "failed": len(failed_slots),
            "failed_slots": failed_slots[:100],
            "alignment_rejected": len(_alignment_failed),
            "error_code": _code,
            "user_message": _message,
            "ladder": ladder.as_report(),
        }
        if failed_slots:
            print(
                f"  ⛔ 标题失败 {len(failed_slots)}/{requested} 条"
                f"(复核拒绝 {len(_alignment_failed)} · AI 不可用"
                f" {len(failed_slots) - len(_alignment_failed)})"
                f" · 这些槽位显式失败,不用模板凑数 · {_message}"
            )
        return resolved
