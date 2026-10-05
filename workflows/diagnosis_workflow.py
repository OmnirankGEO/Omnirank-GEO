"""
GEO  v2.0
- 5
-
-
"""

import asyncio
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import json
import httpx
from datetime import datetime
from pathlib import Path

from tools.tikhub import search_douyin_videos, search_xiaohongshu_notes
from tools.search import search_web_for_geo
from tools.search.metaso_mcp import (
    metaso_web_search,
    metaso_search_with_citations,
    metaso_scholar_search,
    metaso_document_search,
    metaso_industry_analysis,
    extract_citations,
    AUTHORITY_DOMAINS,
)
from tools.ai_visibility import (
    batch_query_ai_engines,
    check_longtail_keywords,
    detailed_ai_visibility_test,
)  # Phase 12.8
from tools.scoring import calculate_geo_score, generate_geo_report
from tools.scoring.llm_geo_scorer import calculate_geo_score_llm
from tools.keyword_generator import generate_diagnosis_keywords, analyze_client_business
# [R3 · 2026-08-03] 业务范围裁决(brands.city_scope 只升不降)· 单独成模块见其 docstring
from services.diagnosis_business_scope import (
    resolve_brand_cities,
    resolve_effective_business_scope,
)
from tools.batch_collect import batch_collect_all
from tools.competitor.competitor_identifier import identify_competitors
from tools.competitor.competitor_benchmark import generate_benchmark_report
from tools.brand.brand_account_identifier import get_brand_content_for_scoring
from agents.review_agent import review_and_iterate
from tools.analysis.content_insights import (
    analyze_content_insights,
    analyze_content_with_llm,
)  # [Enhanced] Phase 10

# Phase 10: 报告质量深度优化 Agents
from agents.source_relevance_agent import (
    batch_verify_citations_with_llm,
    quick_relevance_check,
)
from agents.academic_insight_agent import (
    analyze_academic_papers,
    format_academic_section,
)
from agents.action_plan_agent import generate_professional_action_plan
from agents.report_enhancement_agent import (
    generate_enhanced_report,
    TokenTracker,
)  # [Phase 12] LLM增强报告 + Token统计
from tools.asr.asr_tool import (
    batch_transcribe_videos,
    extract_audio_url_from_douyin,
    analyze_video_engagement,
)  # [Phase 12.5/12.7] ASR视频转写+评论分析

# [Phase 12] 报告增强开关 - True使用LLM生成国际化报告，False使用原模板
USE_ENHANCED_REPORT = True


from utils.knowledge_manager import geo_knowledge
from utils.state_manager import DiagnosisState, retry_async
from db.diagnosis_db import save_diagnosis  # [Phase 17] 数据库存储


# ============================================================================
# CTO-15.23 2026-05-21 · 自定义题漏斗 3 层分类 helper(老板报 report 325 总分被低估)
# 仅给 verbatim 模式用 · 题目原文不变 · LLM 只做分类元数据(不违反"不主动优化"原则)
# 用 deepseek-v4-flash 跟 ai_tester._deepseek_v4_flash_verify_brand 同模型同 endpoint
# ============================================================================
class DiagnosisDataInsufficientError(RuntimeError):
    """[audit P1 2026-06-10 老板已批] 4 引擎采集数据不足以出诊断结论(API 故障,非品牌真隐形)。

    抛出后需穿透到 server._run_diagnosis_impl 顶层 except → re-raise → run_diagnosis_task
    release_freeze(失败不扣费),不再产出 0 分『隐形级』假报告。

    🔴 [audit #3 返修 claim 更正] 本分支(geo-redline)server.py **0 改**:把异常 re-raise 出去的
    `_run_diagnosis_impl` 顶层 except 改动在 **#2 geo-core 分支**。本类只负责"抛"(信号源);
    "穿透 + release_freeze"靠 #2 的 re-raise。**部署铁律:#2 geo-core 必须与本分支同批 / 先行
    部署**(REWORK §E3)。若 #3 单独上而 #2 未上,#2 之前的 except 会吞掉本异常 → 仍走"默认 0 分"
    路径(劣于现状:诊断失败仍扣 650 且无报告)。"""


def _assert_ai_visibility_sufficient(ai_visibility) -> None:
    """[C组①·V10/§12.1]委托**版本化最小有效样本合同**判定(不再用散落 >0.5 裸常量)。

    - insufficient(零可用/payload 损坏)→ 抛 DiagnosisDataInsufficientError:H0,整单不交付、全额释放;
    - degraded(部分 provider 失败但仍有有效样本)→ **不再抛**:降级交付,失败平台排除分母、
      覆盖率对用户可见、未履约部分由结算侧按 billable 比例部分计费(见 evaluate_delivery_verdict)。
    旧行为 failed/planned>0.5 直接拦死会把"已有有效结果"整批抹掉,违反 §12.1。
    """
    # 函数内 import:本函数会被红线审计测试单独抽出 exec,不能依赖模块级名字。
    from services.diagnosis_sample_contract import OUTCOME_INSUFFICIENT, evaluate_sample
    verdict = evaluate_sample(ai_visibility)
    if verdict.outcome == OUTCOME_INSUFFICIENT:
        raise DiagnosisDataInsufficientError(verdict.message)


def evaluate_delivery_verdict(ai_visibility):
    """返回本次采集的最小有效样本判定(供结算侧算 billable / 报告侧显示覆盖)。"""
    from services.diagnosis_sample_contract import evaluate_sample
    return evaluate_sample(ai_visibility)


def _legacy_assert_ai_visibility_sufficient(ai_visibility) -> None:
    """[audit P1 2026-06-10] 诊断域 fail-closed 闸门(对齐监测域 2026-06-07 engine_error 思路)。

    旧行为:4 引擎全失败/采集整体超时 → ai_visibility 空/error → 评分 fallback 0 分『隐形级』
    照常 commit 全额算力(total_failed 字段存在但评分与扣费链不消费它)→ 错误打击客户 + 错收费。

    只拦【铁证】场景防误杀(老结构/正常 0 检出不受影响):
      ① 采集层整体 error 且无 summary(Phase2 超时形态 {'error': 'timeout'})
      ② summary.total_planned>0 且 total_tests==0(4 引擎全部 API 失败 · 非"测了但没检出")
      ③ summary.total_planned>0 且 total_failed/total_planned > 0.5(过半失败)
    ⚠️ 必须在评分 try 块【之外】调用 —— 评分 try 的 except 会把异常吞成"默认 0 分"。"""
    av = ai_visibility or {}
    summary = av.get("summary") or {}
    planned = summary.get("total_planned") or 0
    if av.get("error") and not summary:
        raise DiagnosisDataInsufficientError(
            "AI 搜索引擎本次访问异常,诊断未完成,算力已退回,请稍后重试"
        )
    if planned > 0:
        tests = summary.get("total_tests") or 0
        failed = summary.get("total_failed") or 0
        if tests <= 0:
            raise DiagnosisDataInsufficientError(
                "AI 搜索引擎本次全部访问失败,诊断未完成,算力已退回,请稍后重试"
            )
        if failed / planned > 0.5:
            raise DiagnosisDataInsufficientError(
                f"AI 搜索引擎本次过半访问失败({failed}/{planned}),诊断未完成,算力已退回,请稍后重试"
            )


async def _classify_questions_to_funnel_layers(
    questions: list[str], brand_name: str, industry: str
) -> dict[str, str]:
    """LLM 把用户题分类到漏斗 3 层 · 返回 {question: layer} dict

    Layers:
    - brand_awareness: 直接问公司/品牌名(如"XX是什么公司"、"XX主营什么")
    - regional_industry: 包含地区+行业(如"上海豪车租赁哪家好"、"浙江省内XX公司推荐")
    - super_tier1: 行业通用词(全国/无地区)(如"做XX需要注意什么"、"国内XX品牌")

    失败时所有题 fallback super_tier1(跟原行为一致)· 不抛异常
    """
    import os
    import json as _json
    import httpx

    if not questions:
        return {}
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        return {q: "super_tier1" for q in questions}

    # 编号映射 · 防止 LLM 返回错乱
    numbered = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    prompt = f"""你是 GEO 行业专家 · 请把客户的诊断问题按"漏斗 3 层"分类。

【客户品牌】{brand_name}
【行业】{industry or '未指定'}

【漏斗 3 层定义】
- brand_awareness:直接问该品牌名 / 公司 / 主营业务(例:"{brand_name}是什么公司"、"{brand_name}主营什么")
- regional_industry:包含具体地区 + 行业的本地推荐问题(例:"上海XX哪家好"、"浙江省内XX推荐")
- super_tier1:全国 / 行业通用 / 不含地区的方案对比 / 选购 / 转化问题(例:"做XX需要注意什么"、"国内XX品牌")

【判定优先级(从高到低)】
1. 问题包含品牌名或要求介绍公司 → brand_awareness
2. 问题含具体地区(省/市/区)且关联行业 → regional_industry
3. 其他 → super_tier1

【待分类问题列表】
{numbered}

【输出格式】严格 JSON · 不加任何解释:
{{"1": "brand_awareness", "2": "super_tier1", "3": "regional_industry", ...}}
"""

    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "deepseek-v4-flash",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 512,
                    "temperature": 0,
                },
            )
        if resp.status_code != 200:
            print(f"[funnel_classify] HTTP {resp.status_code}: {resp.text[:200]} · 全 fallback super_tier1")
            return {q: "super_tier1" for q in questions}
        content = resp.json()["choices"][0]["message"]["content"].strip()
        # 提取 JSON · LLM 可能 wrap 在 ```json ... ```
        if content.startswith("```"):
            content = content.strip("`").lstrip("json").strip()
            if content.endswith("```"):
                content = content[:-3].strip()
        parsed = _json.loads(content)
        valid_layers = {"brand_awareness", "regional_industry", "super_tier1"}
        result: dict[str, str] = {}
        for k, v in parsed.items():
            try:
                idx = int(k) - 1
                if 0 <= idx < len(questions) and v in valid_layers:
                    result[questions[idx]] = v
            except (ValueError, TypeError):
                continue
        # 缺漏的 fallback super_tier1
        for q in questions:
            if q not in result:
                result[q] = "super_tier1"
        print(f"[funnel_classify] LLM 分类成功 · {len(result)} 题 · " + " / ".join(
            f"{layer}={sum(1 for v in result.values() if v == layer)}"
            for layer in valid_layers
        ))
        return result
    except Exception as e:
        print(f"[funnel_classify] LLM 分类异常 · 全 fallback super_tier1: {e}")
        return {q: "super_tier1" for q in questions}



# ============================================================================
# CTO-B 2026-04-26 W2 · 诊断 v2 helper(decision A:诊断主链必须真走 v2)
# ============================================================================

async def _run_diagnosis_v2_assemble_and_persist(
    *,
    diagnosis_id: int,
    brand_id: int,
    results: dict,
    score_data: dict,
):
    """诊断主链 v2 装配 + 落库 helper

    决策点 A:不能只做壳 · 诊断必须真正走 v2
    决策点 5:不静默降级 · v2 异常时记 report_v2_error · 前端显示「报告生成异常」

    流程:
      1. is_diagnosis_v2_enabled() 闸门(默认 ON · 紧急 disabled flag 关)
      2. 拉 brand + profile + quote 行
      3. assemble_diagnosis_report_v2 → 同时产 internal + client
      4. update_diagnosis_v2_in_db 写入新列(report_v2_internal_md / client_md / modules_jsonb / completeness)
      5. 如果 v2 internal markdown 非空 · 把它写入 results["report"] 覆盖 v1 → .md 文件 + raw_data_json 都用 v2

    异常时不抛 · 仅 log + 写 report_v2_error 列(决策点 5)
    """
    try:
        from services.diagnosis_report_v2 import (
            assemble_diagnosis_report_v2,
            update_diagnosis_v2_in_db,
            is_diagnosis_v2_enabled,
        )
    except Exception as ie:
        print(f"  ⚠️ [v2] 模块导入失败 · 跳过(报告仍走 v1): {ie}")
        return

    if not is_diagnosis_v2_enabled(brand_id=brand_id):
        print(f"  ⏭️ [v2] is_diagnosis_v2_enabled 关 · 跳过(brand_id={brand_id})")
        return

    # 拉 brand + profile + quote
    try:
        from db.connection import get_connection as _gc
        conn = _gc()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM brands WHERE id = %s", (brand_id,))
            brand_row = cur.fetchone()
            brand_data = dict(brand_row) if brand_row else {}

            cur.execute(
                "SELECT * FROM client_profiles WHERE brand_id = %s LIMIT 1",
                (brand_id,),
            )
            prof_row = cur.fetchone()
            profile_data = dict(prof_row) if prof_row else {}

            # 🔴 [#54/#55] 按**本次诊断**取报价,不走「品牌最新一张」——
            #    后者会把另一次服务的报价算到这份报告头上。
            #    确定性规则:created_at DESC, id DESC(与 diagnosis_identity_decision 同口径)。
            cur.execute(
                """SELECT * FROM quotes WHERE diagnosis_id = %s
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (diagnosis_id,),
            )
            quote_row = cur.fetchone()
            quote_data = dict(quote_row) if quote_row else {}

            # 🔴 [#54/#55] 复用权威谓词,不写第二份;`cursor=cur` 必须透传
            #    (不传会自开连接 —— 08-10 那次把生产打成 503 的形态)。
            from services.publication_stage_adapters import quote_published_active

            _qid = quote_data.get("id")
            if _qid:
                _n = quote_published_active(int(_qid), cursor=cur)
                published_state = (
                    {"count": int(_n), "available": True, "reason": None}
                    if _n is not None else
                    {"count": None, "available": False, "reason": "projection_unavailable"}
                )
            else:
                published_state = {"count": None, "available": False, "reason": "no_quote"}

        finally:
            conn.close()
    except Exception as be:
        print(f"  ⚠️ [v2] brand/profile/quote 拉取失败 · 用空值继续: {be}")
        brand_data, profile_data, quote_data = {}, {}, {}
        # 🔴 拉取失败时**显式**给 None,不让它意外落到默认值:
        #    这条路上分不清「没有报价」与「查不到」,记成缺失是说了一件没根据的话;
        #    退出分母是这两种未知里唯一诚实的处置(整份报告此时已是降级产物)。
        # 🔴 拉取失败:分不清「没有报价」与「查不到」⇒ 记 unavailable,
        #    reason 与 no_quote **分开**;整份报告此时已是降级产物(上面已告警)。
        published_state = {"count": None, "available": False, "reason": "lookup_failed"}

    # 装配
    try:
        v2_result = assemble_diagnosis_report_v2(
            diagnosis_results=results,
            brand=brand_data,
            profile=profile_data,
            quote=quote_data,
            brand_id=brand_id,
            score_data=score_data,
            published=published_state,
        )
    except Exception as ae:
        print(f"  ❌ [v2] 装配异常 · 仅写 report_v2_error: {ae}")
        # 异常仍写一行错误进 DB(决策点 5 透明化)
        try:
            from db.connection import get_connection as _gc
            conn = _gc()
            cur = conn.cursor()
            cur.execute(
                """UPDATE diagnosis_records
                   SET report_v2_error = %s, report_v2_generated_at = NOW()
                   WHERE id = %s""",
                (f"v2 装配异常: {ae}", diagnosis_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
        return

    # 落库新列
    ok = update_diagnosis_v2_in_db(diagnosis_id, v2_result)
    if not ok:
        print(f"  ⚠️ [v2] update_diagnosis_v2_in_db 失败 · diagnosis_id={diagnosis_id}")

    # 决策 A:诊断主链必须走 v2 · 用 v2 internal markdown 覆盖 results["report"]
    # (后续 .md 文件落盘 + raw_data_json 都同步用 v2 · 兼容老的 share_api 读 report_md_path)
    internal_md = (v2_result.get("internal") or {}).get("full_markdown") or ""
    if internal_md:
        results["report"] = internal_md
        results["report_version"] = "v2"
        completeness_score = (v2_result.get("completeness") or {}).get("score")
        print(
            f"  ✅ [v2] 诊断报告 v2 已装配 · diagnosis_id={diagnosis_id} · "
            f"完整度={completeness_score} · evidence={v2_result.get('internal', {}).get('evidence_count')}"
        )
        if v2_result.get("error"):
            print(f"  ⚠️ [v2] 部分模块装配异常已记 report_v2_error: {v2_result['error']}")
    else:
        print(f"  ⚠️ [v2] internal markdown 为空 · 保留 v1 输出")


def _enqueue_report_v3_narrative_if_ready(diagnosis_id: int):
    """Queue v3 narrative enrichment after v2 JSONB has been persisted."""
    try:
        from services.narrative_job_runner import enqueue_narrative_enrichment

        result = enqueue_narrative_enrichment(diagnosis_id)
        status = result.get("status")
        if status == "pending":
            print(f"  🧾 [report-v3] narrative enrichment 已入队 · diagnosis_id={diagnosis_id}")
        elif status == "skipped":
            print(
                f"  ⏭️ [report-v3] narrative enrichment 跳过 · "
                f"diagnosis_id={diagnosis_id} · reason={result.get('reason')}"
            )
    except Exception as exc:
        print(f"  ⚠️ [report-v3] narrative enrichment 入队失败(非 block): {exc}")


def _resolve_bridge_brand_id(
    *, brand_id, brand_name: str, industry, industry_category,
    owner_user_id,
) -> int:
    """诊断产物(v2 报告 / stage log / 信任资产)要挂在**哪个品牌**上。

    🔴 [2026-08-31 · 生产实证 brands.id=936] ``brand_id`` 是**现有品牌可信身份
       SSOT**(``run_diagnosis_workflow`` 签名逐字)。有它就用它。

       上一版两处桥接点写的是
       ``int(brand_id) if organization_identity is not None else
       get_or_create_brand(..., owner_user_id=effective_owner_user_id)`` ——
       非组织路径**把手上已有的 id 丢掉**,改按「名字 + 发起人」重新建档。
       而 ``get_or_create_brand`` 按 ``(name, owner_user_id)`` 去重、跨 owner
       同名**合法**(生产上一个名字 9 个 owner),于是 admin(112)代跑他人
       品牌 812(owner 113)时约束不拦,静默 INSERT 出一条同名副本 936,
       下游三处(v2 报告 / stage log / 信任资产)全部挂到副本上 ——
       真品牌拿不到自己这次诊断的产物,副本还进了 admin 的客户列表占额度。

       判它是缺陷不是设计的硬证据:同一个三元的**另一支**用的就是
       ``int(brand_id)`` —— id 在那一刻就在手上,不是拿不到。

    🔴 兜底**保留**:真的没有 id(新品牌直接填名字)时仍然建档,
       否则修复就退化成把"新品牌诊断"整条砍掉。
       falsy 一律走兜底 —— 前端 ``?? 0`` 兜出来的 ``0`` 若被 ``int()`` 当成
       一个存在的品牌 id 传下去,比建副本更糟(挂到不存在的品牌上)。

    🔴 归属只做**定位**,不做改写:命中已有 id 时一个字段都不动。

    🔴 单点:两处桥接点都调这里,不许任何一处自己去 ``get_or_create_brand``
       (接线锁 ``test_both_bridge_sites_use_the_single_resolver`` /
       ``test_only_the_resolver_may_create_a_brand_in_this_module`` 钉住)。
       同一谓词写两处、必有一处没人验 —— 本缺陷就是这么来的。
    """
    if brand_id:
        return int(brand_id)

    from db.diagnosis_db import get_or_create_brand

    return get_or_create_brand(
        brand_name, industry, industry_category, owner_user_id=owner_user_id)


def build_social_search_keywords(diagnosis_keywords, brand_name):
    """社媒搜索词 = 诊断关键词 + 品牌名(缺则插到第 0 位)。

    🔴 抽出来只为**可驱动**:原先这 3 行长在 `run_diagnosis_workflow` 里,
    而那是个 2400 行的单体 —— 判据没法证「batch_collect 实际拿到了非空搜索词」,
    只能去看「:752 那行插了一项」,那是看写法不是看结果(Review §16 明确不认)。

    ⚠️ 与 :1554 一带那段**不是同一个东西**,别合:那段插的是品牌名**变体**
    (全称 + 去后缀简称),这里只插全称。合了会悄悄改行为。
    """
    out = list(diagnosis_keywords)
    name = (brand_name or "").strip()
    # 🔴 空白品牌名**不插**。原来无条件 insert(0, brand_name),
    #    brand_name 为空时会把空串塞进搜索词第 0 位 —— 白搜一次、拿回垃圾。
    #    这是既有缺陷(不是本次引入),判据 §16 正臂撞出来的,顺手钉住。
    if name and name not in out:
        out.insert(0, name)
    return out


async def run_diagnosis_workflow(
    brand_name: str,
    industry: str,
    keywords: list[str],
    longtail_keywords: list[str] = None,
    ai_engines: list[str] = None,
    output_file: str = None,
    upload_file: str = "",
    additional_info: str = "",
    competitors: list[str] = None,  # 指定竞品（用于深度分析）
    own_accounts: list[str] = None,  # [NEW] 客户自有账号（排除在竞品分析外）
    business_context: dict = None,  # [P2] 复测时传入上次的业务上下文
    session_id: str = None,
    progress_callback: callable = None,
    report_style: str = None,  # [DEPRECATED] 向后兼容，使用 diagnosis_scope
    diagnosis_scope: str = "geo",  # [NEW] "geo" | "social" | "full"
    brand_display_names: list[
        str
    ] = None,  # [NEW] 对外品牌名（如"奥莱超级会员店"），用于AI测试检测
    client_location: str = "",  # [NEW] 客户区域（如"深圳"），用于长尾词地域锁定
    business_scope: str = "",  # [P0-4 2026-07-26] 业务范围 national/regional（缺省按 regional）
    creator_user_id: int = None,  # [FIX] 诊断发起人 user_id，用于品牌 owner 关联
    brand_id: int = None,  # 现有品牌可信身份 SSOT；新建品牌可为空
    organization_identity=None,  # 组织 actor/payer/assignment 证据；legacy 保持 None
    custom_questions: list[str] = None,  # [CTO-15.23 2026-05-21] 用户自定义诊断问题
    question_meta: list = None,  # [#149 2026-09-08] 每题来源 {text,origin,side,layer};缺省=全 customer
    ai_optimize_custom: bool = False,  # [CTO-15.23 2026-05-21 老板订正] 主动优化 toggle:False=verbatim 只跑 custom / True=双轨 system+custom
    run_token: str = None,  # [返工2 P1-4] 资金 run fencing:落库/外部副作用前确认 run 仍 running(lease 丢失即中止落库)
) -> dict:
    """
     GEO  v2.0

    Args:
        brand_name: 品牌名称
        industry: 行业
        keywords: 关键词列表
        longtail_keywords: 长尾关键词
        ai_engines: AI引擎列表
        output_file: 输出文件路径
        upload_file: 上传文件
        additional_info: 额外信息
        competitors: 指定竞品列表（优先深度分析）
        own_accounts: 客户自有账号列表（将被排除在竞品外，并用于品牌内容识别）
        session_id: 会话ID
        progress_callback: 进度回调
        report_style: 报告风格 - "sales"销售版(V4) / "technical"技术版

    特点:
    - 5 (asyncio.gather)
    -  (DiagnosisState)
    -  +
    """
    # In organization work the employee remains the immutable actor, while all
    # customer data, observations and public branding belong to the owner
    # principal. Legacy work keeps the historical creator-owned namespace.
    effective_owner_user_id = (
        int(organization_identity.principal_user_id)
        if organization_identity is not None
        else creator_user_id
    )

    if ai_engines is None:
        # [P0-2 返修③ 2026-08-23] 清单从 config/ai_engines 单源取,**不在这里再写一份**。
        # 原来这里是硬编码的四个引擎,而计价那一侧写的是另一份(前端 ['qwen',…]),
        # 两份各自演化 —— 客户为 kimi 付钱、管线跑 yuanbao,而 'qwen' 谁都不认识。
        # 值与改动前逐字节相同(判据 test_workflow_default_equals_the_ssot 钉住),
        # 这一步只是把「唯一那份清单」搬到有人守的地方。
        from config.ai_engines import DIAGNOSIS_RUNTIME_ENGINES

        ai_engines = list(DIAGNOSIS_RUNTIME_ENGINES)

    #
    if session_id is None:
        session_id = f"{brand_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    state = DiagnosisState(session_id)
    print(f"  ID: {session_id}")

    # [R3 · 2026-08-03] business_scope 以 brands.city_scope 为准(只升不降)· 详见
    # resolve_effective_business_scope 的 docstring。放在 results 之前 → input_params
    # 记的是**生效后**的值,复测能拿到同一口径。
    business_scope = resolve_effective_business_scope(brand_id, business_scope)

    # [R5 · 2026-08-04] 品牌档案经营城市一次性取出，全程只当「地域 token 池的第二个来源」。
    # 表单的「客户区域」一旦被手改过，之后每次复测都会继承那个手改值（server.py
    # “历史诊断 client_location 优先于 brand.cities”），档案再也进不来 —— 生产实证
    # brand 737 诊断 529：表单 “广东省，香港” vs 档案 “广东省深圳市龙岗区” → 4 条双地名赘字。
    # 🔴 [#147-B] 它现在由**共享的品牌侧输入**产出（见下方装配点）——
    #    预览端取的是同一个函数的返回值，两边不可能再各算一份。

    results = {
        "brand": brand_name,
        "industry": industry,
        "keywords": keywords,
        "session_id": session_id,
        "timestamp": datetime.now().isoformat(),
        # [P2] 保存原始输入参数供复测使用
        "input_params": {
            "own_accounts": own_accounts or [],
            "competitors": competitors or [],
            "additional_info": additional_info or "",
            "client_location": client_location or "",
            "business_scope": business_scope or "",
            "brand_display_names": brand_display_names or [],
            "diagnosis_scope": diagnosis_scope,
            "report_style": report_style,  # 保留向后兼容
            # [CTO-15.23 2026-05-21] 复测时前端 prefill 用 · 老板拍板 #7 默认继承可改
            "custom_questions": list(custom_questions) if custom_questions else [],
            # [CTO-15.23 2026-05-21 老板订正] 复测继承 toggle 状态
            "ai_optimize_custom": bool(ai_optimize_custom),
        },
        "data": {},
        "scores": {},
        "report": "",
        "brand_id": brand_id,
    }

    # 向后兼容：旧 report_style 映射到新 diagnosis_scope
    if report_style and not diagnosis_scope:
        diagnosis_scope = {"sales": "geo", "technical": "full"}.get(report_style, "geo")
    if not diagnosis_scope:
        diagnosis_scope = "geo"

    # [NEW] 按 scope 决定采集/评分/报告逻辑
    IS_LITE_MODE = diagnosis_scope in ("geo", "social")  # geo和social都走轻量采集路径
    results["data"]["diagnosis_type"] = diagnosis_scope  # "geo" / "social" / "full"

    scope_labels = {"geo": "GEO专项", "social": "社媒专项", "full": "全面诊断"}
    if IS_LITE_MODE:
        print(f"  ⚡ {scope_labels.get(diagnosis_scope, diagnosis_scope)}模式 - 按scope精简采集")
    else:
        print(f"  🔬 全面诊断模式 - 执行所有步骤")

    #
    print("...")
    await geo_knowledge.initialize()

    # ===== 步骤 0: 业务深度理解（LLM分析客户真实业务场景）=====
    print(" 0/7: 业务深度理解...")
    if progress_callback:
        await progress_callback(
            "business_analysis", "正在深度分析客户业务...", 15 if IS_LITE_MODE else 2
        )

    # ===== [B4 接主链 · 工单 B 2026-07-27] 行业上下文装配点 =====
    # 🔴 [#147-B · 2026-09-07] 品牌侧输入改从**共享的一份**取:
    #    `services.diagnosis_generator_inputs.brand_side_generator_inputs`。
    #    动机不是「预览端参数没补齐」—— 补齐只修今天这一次;下次有人给出题器
    #    加第 6 个品牌侧输入,两边照样分家,**而且不会有任何东西变红**。
    #    所以两侧从同一处取值,再配数据流锁把分家钉住。
    #
    #    🔴 `resolve_missing_keywords=False` 是行为保持的命门:这里的 `keywords`
    #       是**调用方**已经跑过阶梯的结果,哪怕是空列表也要原样透传。让 helper
    #       替它重解析,会把「调用方给了空词」变成「按品牌名重新造词」甚至抛
    #       `NoKeywordSource` —— 那是行为改变,不是抽取。
    from services.diagnosis_generator_inputs import brand_side_generator_inputs

    _brand_side, _brand_side_meta = await brand_side_generator_inputs(
        brand_id=brand_id,
        brand_name=brand_name,
        industry=industry,
        client_location=client_location,
        keywords=keywords,
        resolve_missing_keywords=False,
    )
    flywheel_material = _brand_side["flywheel_material"]
    # advisory 素材原样存档：效果归因要能回答"这次诊断到底吃到了什么沉淀"。
    # 只在真有素材时写这个 key —— 关闸时连键都不多一个，落库 payload 与今日逐字节一致。
    if flywheel_material:
        results["data"]["flywheel_reuse"] = flywheel_material

    # [P2] 复测模式：如果传入了business_context则直接使用，确保测试问题一致
    if business_context and business_context.get("real_user_questions"):
        print(f"  🔄 复测模式: 使用上次的业务上下文（确保测试问题一致）")
        business_context_result = business_context
    else:
        cached_business = state.get_checkpoint("business_analysis")
        if cached_business:
            business_context_result = cached_business
            print(f"  📂 从检查点恢复: business_analysis")
        else:
            business_context_result = await analyze_client_business(
                brand_name=brand_name,
                industry=industry,
                # 表单期才有的两项 —— 品牌侧那一份里**没有**它们(见 helper 的边界说明):
                # 页面加载那一刻用户还没填,预览端只能留空。
                additional_info=additional_info or upload_file,
                # [P0-4] 城市 + 业务范围必须传进去：选词的地域适配全靠这两个值，
                #   缺了就只能靠品牌名瞎猜（驰鲸案例的 0 命中根因之一）。
                business_scope=business_scope,
                # 🔴 品牌侧四项(keywords / client_location / brand_cities /
                #    flywheel_material)**一律**从共享那份展开 —— 预览端取的是同一个
                #    函数的返回值。谁把它们改回手写实参,数据流锁就该红。
                **_brand_side,
            )
            state.save_checkpoint("business_analysis", business_context_result)

    # 打印业务理解结果
    print(f"   ✅ 核心业务: {business_context_result.get('core_business', industry)}")
    print(
        f"   ✅ 目标客户: {business_context_result.get('target_customers', '未识别')}"
    )
    print(
        f"   ✅ 真实价值: {business_context_result.get('value_proposition', '未识别')}"
    )
    if business_context_result.get("real_user_questions"):
        print(
            f"   ✅ 真实用户问题: {len(business_context_result['real_user_questions'])}个"
        )
    if business_context_result.get("search_keyword_groups"):
        print(
            f"   ✅ 精准搜索词组: {len(business_context_result['search_keyword_groups'])}组"
        )

    # ===== [性能优化v2] 步骤 0.5 + 步骤 1: 并行执行5118查询和关键词生成 =====
    # 两者互不依赖，都只需要原始输入参数
    async def _step_05_5118():
        """查询5118行业搜索量"""
        if business_context_result.get("5118_data"):
            return None  # 已有数据
        try:
            from tools.opportunity_calculator import calculate_opportunity
            search_keyword = industry or brand_name
            print(f"   🔍 查询5118流量指数: {search_keyword}")
            opp_result = await calculate_opportunity(search_keyword)
            data = {
                "keyword": opp_result.keyword,
                "pc_index": opp_result.pc_index,
                "mobile_index": opp_result.mobile_index,
                "sem_price": opp_result.sem_price,
                "ai_monthly": opp_result.ai_monthly,
                "traditional_monthly": opp_result.traditional_monthly,
            }
            print(f"   ✅ 5118流量: PC={opp_result.pc_index} 移动={opp_result.mobile_index} AI月搜索={opp_result.ai_monthly}")
            return data
        except Exception as e:
            print(f"   ⚠️ 5118查询失败（使用默认值）: {e}")
            return None

    async def _step_1_keywords():
        """生成诊断关键词"""
        if not IS_LITE_MODE:
            cached_keywords = state.get_checkpoint("keywords")
            if cached_keywords:
                return cached_keywords
            kws = await generate_diagnosis_keywords(
                company_name=brand_name,
                industry=industry,
                user_keywords=keywords,
                company_profile=upload_file,
            )
            state.save_checkpoint("keywords", kws)
            return kws
        return None

    print(" 0.5+1: 并行执行5118查询和关键词生成...")
    _5118_result, _kw_result = await asyncio.gather(
        _step_05_5118(),
        _step_1_keywords(),
        return_exceptions=True,
    )

    # 合并5118结果
    if not isinstance(_5118_result, Exception) and _5118_result:
        business_context_result["5118_data"] = _5118_result

    results["data"]["business_context"] = business_context_result

    # ===== [销售版] Phase 2: 独立并行采集路径 =====
    if IS_LITE_MODE:
        print("\n⚡ ===== 销售版并行采集模式 =====")
        if progress_callback:
            await progress_callback("parallel_collect", "启动并行数据采集...", 50)

        # 从业务理解中提取关键数据
        # [CTO-15.23 2026-05-21] 系统题永远 8 道(主分稳定);自定义题来自前端(双轨独立打分)
        real_questions = business_context_result.get("real_user_questions", [])[:8]
        # [CTO-15.23 2026-05-21 P1 修一致性] 用户题只做自身去重(server validator 已做兜底)
        # 不再跟系统题去重:用户填的题就跑(尊重用户意图 · 字面碰撞也跑两次问 LLM 不浪费)
        # 这样:扣费 N × 100 = 实际跑 N 题 = input_params.custom_questions = ai_data.custom_visibility.questions
        custom_questions_list = []
        if custom_questions:
            _seen = set()
            for q in custom_questions:
                if isinstance(q, str) and q.strip() and q.strip() not in _seen:
                    custom_questions_list.append(q.strip())
                    _seen.add(q.strip())
        # 把实际跑的 custom_questions 回写 results.input_params(与扣费/落库/复测继承全部一致)
        try:
            results["input_params"]["custom_questions"] = list(custom_questions_list)
            # [#149] 元数据与题集**同处回写** —— 分开写会出现「题落了、来源没落」,
            #   复测时那批 AI 题会退化成 customer,品牌定向豁免又回来了。
            if question_meta:
                results["input_params"]["question_meta"] = [
                    dict(m) for m in question_meta if isinstance(m, dict)]
        except Exception:
            pass
        refined_keywords = business_context_result.get("search_keyword_groups", [])
        if refined_keywords:
            diagnosis_keywords = [" ".join(group) for group in refined_keywords[:5]]
        else:
            diagnosis_keywords = keywords[:5]

        # 构建社媒搜索词(逻辑抽成 build_social_search_keywords,判据才驱得动)
        social_search_keywords = build_social_search_keywords(
            diagnosis_keywords, brand_name)

        print(f"  📋 AI测试问题: 系统 {len(real_questions)} 题 + 自定义 {len(custom_questions_list)} 题")
        print(f"  📋 搜索关键词: {diagnosis_keywords}")

        # ========== 定义并行任务 ==========
        async def task_social_search():
            """社媒搜索任务"""
            try:
                cached = state.get_checkpoint("social_data")
                if cached:
                    return ("social_data", cached, True)
                data = await batch_collect_all(social_search_keywords)
                return ("social_data", data, False)
            except Exception as e:
                return ("social_data", {"error": str(e)}, False)

        async def task_web_search():
            """网页搜索任务 · 含 LLM 核验 citations(给 5 维评分提供 brand_direct_count)"""
            try:
                cached = state.get_checkpoint("web_search")
                if cached:
                    return ("web_search", cached, True)
                query = f"{brand_name} {industry}"
                # [FIX] 使用返回dict的metaso_search_with_citations，避免ToolResponse序列化问题
                data = await metaso_search_with_citations(
                    query, scope="webpage", size=20
                )

                # [CTO-15.23 2026-05-22 老板订正] LLM 核验 citations → brand_direct vs industry_ref
                # 老板报告 report 325 复测 5 维雷达图 4 维 0 分实证根因:
                #   IS_LITE_MODE task_web_search 之前直接返 metaso 原始结果 · 缺 brand_direct_count
                #   → tools/scoring/geo_scope_scorer.py 5 维评分全部 fallback 0
                #   (网页内容 0/25 · 权威背书 0/20 · 结构化 0/15 · 品牌基础 0/10)
                # 老板原话"让核验名字的 LLM 升级为所有数据核验的 LLM"= 这条 fix
                # 修法:复用 full 模式 do_web_test (line 1572) 已有的 batch_verify_citations_with_llm
                # LLM 严苛核验每条 citation 是否真的引用了该品牌(防字面模糊匹配假阳)
                citations = data.get("citations", []) or []
                if citations:
                    try:
                        from agents.source_relevance_agent import batch_verify_citations_with_llm
                        verification = await batch_verify_citations_with_llm(
                            brand_name=brand_name,
                            company_full_name=f"{brand_name}公司",
                            industry=industry,
                            citations=citations,
                            batch_size=10,
                        )
                        brand_direct = verification.get("brand_direct", []) or []
                        industry_ref = verification.get("industry_reference", []) or []
                        data["brand_direct_count"] = len(brand_direct)
                        data["brand_direct_citations"] = brand_direct[:20]
                        data["industry_ref_count"] = len(industry_ref)
                        print(f"[task_web_search] LLM 核验:{len(citations)} 条 → brand_direct {len(brand_direct)} · industry_ref {len(industry_ref)}")
                    except Exception as ve:
                        # LLM 核验失败 · 保守降级(brand_direct_count=0 · 跟旧行为一致)
                        print(f"[task_web_search] LLM 核验失败 · 5 维评分会偏低: {ve}")
                        data.setdefault("brand_direct_count", 0)
                        data.setdefault("brand_direct_citations", [])
                else:
                    data.setdefault("brand_direct_count", 0)
                    data.setdefault("brand_direct_citations", [])

                return ("web_search", data, False)
            except Exception as e:
                return ("web_search", {"error": str(e), "brand_direct_count": 0, "brand_direct_citations": []}, False)

        async def task_competitor_stats():
            """竞品统计任务（简化版）"""
            try:
                cached = state.get_checkpoint("competitor_analysis")
                if cached:
                    return ("competitor_analysis", cached, True)
                # 简化版竞品分析，不做深度LLM分析
                result = await identify_competitors(
                    social_data={},  # 稍后合并
                    brand_name=brand_name,
                    industry=industry,
                    deep_analysis=False,  # 销售版关闭深度分析
                    keyword_count=3,
                    own_accounts=own_accounts,
                    specified_competitors=competitors,
                )
                competitor_data = result.data if hasattr(result, "data") else result
                return ("competitor_analysis", competitor_data, False)
            except Exception as e:
                return (
                    "competitor_analysis",
                    {"competitors": [], "error": str(e)},
                    False,
                )

        async def task_ai_visibility():
            """AI可见度测试（4引擎并行）"""
            try:
                cached = state.get_checkpoint("ai_visibility")
                if cached:
                    # 验证缓存有效性：detail_table 非空且有成功的测试
                    cached_detail = cached.get("detail_table", [])
                    cached_summary = cached.get("brand_detection_summary", {})
                    cached_total = cached_summary.get("total_tests", 0)
                    if cached_detail and cached_total > 0:
                        return ("ai_visibility", cached, True)
                    else:
                        print(f"  ⚠️ ai_visibility 缓存无效（detail_table={len(cached_detail)}, total_tests={cached_total}），重新执行")

                # [CTO-15.23 2026-05-21 老板订正] 主动优化 toggle 三分支
                # ai_optimize_custom 由前端 toggle 决定:
                #   - False(默认): custom 非空时只跑 custom verbatim · 主表=custom · 主分=custom · 客户填啥跑啥
                #   - True: 客户主动选优化 · 跑 system+custom 双轨 · 主表=system · custom 独立 section
                #   - custom 为空: 总是走 system 8 题(toggle 无意义)
                system_test_questions_fallback = (
                    real_questions
                    if real_questions
                    else [
                        f"{industry}哪家好？",
                        f"推荐几家靠谱的{industry}",
                        f"{industry}一般怎么收费？",
                        f"做{industry}需要注意什么？",
                        f"有没有专业的{industry}团队推荐",
                    ][:8]
                )

                # 模式判断
                mode_verbatim = bool(custom_questions_list) and not ai_optimize_custom
                mode_dual = bool(custom_questions_list) and ai_optimize_custom
                mode_default = not custom_questions_list  # toggle 在此模式无意义

                if mode_verbatim:
                    # 模式 A:verbatim · 只跑 custom · 主表=custom · 主分=custom
                    print(f"  📋 模式:custom verbatim · 只跑用户填的 {len(custom_questions_list)} 题(不混合 system · 不主动优化)")
                    ai_result_primary = await detailed_ai_visibility_test(
                        questions=list(custom_questions_list),
                        check_brand=brand_name,
                        industry=industry,
                        engines=ai_engines,
                        custom_brand_variants=brand_display_names,
                        brand_id=brand_id,
                        observation_source_ref=run_token or session_id,
                        observation_scope="custom_primary",
                        owner_user_id=effective_owner_user_id,
                    )
                    primary_questions = list(custom_questions_list)
                    primary_detail = ai_result_primary.get("detail_table", []) if isinstance(ai_result_primary, dict) else []
                    custom_ai_result = None
                else:
                    # 模式 B(双轨)或 C(default)· 主表都是 system 题
                    print(
                        f"  📋 模式:{'双轨(主动优化)' if mode_dual else 'default(无自定义)'} · "
                        f"system {len(system_test_questions_fallback)} 题"
                        + (f' + custom {len(custom_questions_list)} 题独立 section' if mode_dual else '')
                    )
                    _sys_task = detailed_ai_visibility_test(
                        questions=system_test_questions_fallback,
                        check_brand=brand_name,
                        industry=industry,
                        engines=ai_engines,
                        custom_brand_variants=brand_display_names,
                        brand_id=brand_id,
                        observation_source_ref=run_token or session_id,
                        observation_scope="system_primary",
                        owner_user_id=effective_owner_user_id,
                    )
                    if mode_dual:
                        _cust_task = detailed_ai_visibility_test(
                            questions=list(custom_questions_list),
                            check_brand=brand_name,
                            industry=industry,
                            engines=ai_engines,
                            custom_brand_variants=brand_display_names,
                            brand_id=brand_id,
                            observation_source_ref=run_token or session_id,
                            observation_scope="custom_secondary",
                            owner_user_id=effective_owner_user_id,
                        )
                        ai_result_primary, custom_ai_result = await asyncio.gather(_sys_task, _cust_task)
                    else:
                        ai_result_primary = await _sys_task
                        custom_ai_result = None
                    primary_questions = system_test_questions_fallback
                    primary_detail = ai_result_primary.get("detail_table", []) if isinstance(ai_result_primary, dict) else []

                # engine_stats / bds 都基于 primary(主表)
                bds = ai_result_primary.get("brand_detection_summary", {}) if isinstance(ai_result_primary, dict) else {}
                bds_by_engine = bds.get("by_engine", {})
                engine_stats = {}
                for engine in ai_engines:
                    eng_bds = bds_by_engine.get(engine, {})
                    engine_stats[engine] = {
                        "detected": eng_bds.get("detected", 0),
                        "total": eng_bds.get("total", 0),
                        "rate": eng_bds.get("rate", 0),
                    }

                # dimension_stats question_types 来源:
                # - default/dual 模式:business_context.question_types(LLM 生成系统题时带)
                # - verbatim 模式:调 _classify_questions_to_funnel_layers LLM 给用户题分类
                #   老板报 report 325 总分 26/100 被低估根因 = verbatim 模式用户题全 fallback super_tier1
                #   前 2 层 0/0 "无样本" → 漏斗主分大量丢失
                #   修法:LLM 分类(题目原文不变 · 仅辅助报告 stats 归类 · 不违反"不主动优化")
                if mode_verbatim:
                    # [#149 2026-09-08] AI 出的候选**已经带层**(生成器算过一次),
                    #   只对没有层的题(客户手写的)再花一次 LLM。
                    #   🔴 分类器调用点保留 —— 全 customer 无 layer 时仍然会调用
                    #   (正样本臂:不是把这条路砍掉,是让它只跑该跑的题)。
                    from services.diagnosis_question_origin import (
                        resolve_verbatim_question_types)
                    sales_question_types = await resolve_verbatim_question_types(
                        question_meta, custom_questions_list,
                        classify=lambda qs: _classify_questions_to_funnel_layers(
                            qs, brand_name, industry))
                else:
                    sales_question_types = business_context_result.get("question_types", {})
                # [2026-07-22 板块A A3/A6] 五态显式化聚合 · SSOT=services.diagnosis_identity_review
                # YES/NO 进确定分母 · PENDING_IDENTITY/PROVIDER_UNKNOWN/NOT_COLLECTED 单列 · UNKNOWN 不再当 0
                from services.diagnosis_identity_review import aggregate_dimension_stats
                # [WO_BRAND_QUESTION_LEAK 2026-08-05 §2.2] 传品牌名 → 题面含品牌名的题
                #   一律归品牌认知层,不受上游错标影响。
                # [#149 2026-09-08] 豁免由整体改为**逐题**:只有客户自己写的题才豁免。
                #   AI 出的品牌定向题(Owner 截图第一道)沿用整体豁免会进竞争分母,
                #   把提及率顶高 —— 谁享豁免只由 diagnosis_question_origin 一处判定。
                from services.diagnosis_question_origin import (
                    brand_filter_exempt_questions, origins_from_meta)
                _question_origins = origins_from_meta(question_meta)
                _exempt = brand_filter_exempt_questions(
                    {"is_custom_mode": mode_verbatim,
                     "question_origins": _question_origins},
                    list(custom_questions_list),
                )
                sales_dim_stats = aggregate_dimension_stats(
                    primary_detail, sales_question_types,
                    brand_name=brand_name,
                    exempt_questions=_exempt,
                )

                # 双轨模式 · custom 独立 section
                custom_visibility = None
                if mode_dual and custom_ai_result:
                    custom_detail = custom_ai_result.get("detail_table", []) if isinstance(custom_ai_result, dict) else []
                    custom_engine_stats = {e: {"detected": 0, "total": 0, "rate": 0} for e in ai_engines}
                    for item in custom_detail:
                        for engine, eng_result in (item.get("results") or {}).items():
                            if engine not in custom_engine_stats or not isinstance(eng_result, dict):
                                continue
                            answer = eng_result.get("answer_summary", "")
                            if "查询失败" in answer or (isinstance(answer, str) and answer.startswith("Error")):
                                continue
                            custom_engine_stats[engine]["total"] += 1
                            if eng_result.get("brand_detected"):
                                custom_engine_stats[engine]["detected"] += 1
                    for _e in custom_engine_stats.values():
                        _e["rate"] = (_e["detected"] / _e["total"]) if _e["total"] > 0 else 0
                    cu_total = sum(e["total"] for e in custom_engine_stats.values())
                    cu_detected = sum(e["detected"] for e in custom_engine_stats.values())
                    custom_visibility = {
                        "questions": list(custom_questions_list),
                        "detail_table": custom_detail,
                        "engine_stats": custom_engine_stats,
                        "total_tests": cu_total,
                        "detected_count": cu_detected,
                        "mention_rate": (cu_detected / cu_total) if cu_total > 0 else 0,
                    }

                ai_data = {
                    "test_questions": primary_questions,
                    "engines_tested": ai_engines,
                    "total_tests": bds.get("total_tests", len(primary_questions) * len(ai_engines)),
                    "total_planned": bds.get("total_planned", len(primary_questions) * len(ai_engines)),
                    "total_failed": bds.get("total_failed", 0),
                    "detected_count": bds.get("detected_count", 0),
                    "overall_mention_rate": bds.get("detection_rate", 0),
                    "engine_stats": engine_stats,
                    "detail_table": primary_detail,
                    # [WO 2026-08-06 §1] 与下面 tech 分支同源:两条组装路径都要带,
                    # 漏一条 = 走那条路的诊断报告里没有近失线索(静默不一致)。
                    "near_miss_summary": (
                        ai_result_primary.get("near_miss_summary") or []
                        if isinstance(ai_result_primary, dict) else []
                    ),
                    "question_types": sales_question_types,
                    "dimension_stats": sales_dim_stats,
                    "total_engines": len(ai_engines),
                    "engines_mentioned": sum(
                        1 for e in engine_stats.values() if e.get("detected", 0) > 0
                    ),
                    # 模式标识 · 给报告 / 前端区分 verbatim vs dual
                    "diagnosis_mode": ("verbatim" if mode_verbatim else ("dual" if mode_dual else "default")),
                    "is_custom_mode": mode_verbatim,
                    # [#149] 题面来源随 ai_visibility_data 走:下游(身份复核 / 报告)
                    #   拿到的就是这个 dict,**够不到** results["input_params"]。
                    #   input_params 里那份是给**复测继承**的,用途不同不是重复。
                    "question_origins": _question_origins,
                    # 双轨模式下 custom 独立 section · verbatim 模式 None(主表已是 custom)
                    "custom_visibility": custom_visibility,
                }
                return ("ai_visibility", ai_data, False)
            except Exception as e:
                return ("ai_visibility", {"total_engines": 0, "error": str(e)}, False)

        # ========== 执行并行采集 ==========
        if diagnosis_scope == "geo":
            # GEO: 网页搜索 + 竞品 + AI可见度（跳过社媒）
            parallel_tasks = [
                task_web_search(),
                task_competitor_stats(),
                task_ai_visibility(),
            ]
            task_count = 3
            print(f"  🚀 Phase 2: 并行执行 {task_count} 个采集任务（GEO模式，跳过社媒采集）...")
        elif diagnosis_scope == "social":
            # 社媒: 社媒采集 + 竞品（跳过AI可见度和网页搜索）
            parallel_tasks = [
                task_social_search(),
                task_competitor_stats(),
            ]
            task_count = 2
            print(f"  🚀 Phase 2: 并行执行 {task_count} 个采集任务（社媒模式，跳过AI可见度测试）...")
        else:
            parallel_tasks = [
                task_social_search(),
                task_web_search(),
                task_competitor_stats(),
                task_ai_visibility(),
            ]
            task_count = 4
            print(f"  🚀 Phase 2: 并行执行 {task_count} 个采集任务...")
        try:
            parallel_results = await asyncio.wait_for(
                asyncio.gather(*parallel_tasks, return_exceptions=True),
                timeout=300,  # 5分钟总超时，防止无限挂起
            )
        except asyncio.TimeoutError:
            print("  ⚠️ Phase 2 并行采集超时(5分钟)，使用已有结果继续...")
            parallel_results = [
                ("social_data", {"douyin": {}, "xiaohongshu": {}}, False),
                ("web_search", {"error": "timeout"}, False),
                ("competitor_analysis", {"competitors": [], "error": "timeout"}, False),
                ("ai_visibility", {"total_engines": 0, "error": "timeout"}, False),
            ]
        # 过滤异常结果
        parallel_results = [
            r if not isinstance(r, Exception) else ("unknown", {"error": str(r)}, False)
            for r in parallel_results
        ]

        # 处理结果
        task_labels = {
            "social_data": "📱 社媒数据采集",
            "web_search": "🌐 网页搜索",
            "competitor_analysis": "🏆 竞品统计",
            "ai_visibility": "🤖 AI可见度测试",
        }
        for idx, (task_name, result, was_cached) in enumerate(parallel_results, 1):
            if task_name == "social_data":
                # 特殊处理社媒数据
                social_data = result
                douyin_data = social_data.get("douyin", {})
                xhs_data = social_data.get("xiaohongshu", {})

                # [BUG#4修复] 社媒内容相关性过滤 — 排除品牌名含通用词时的无关内容
                try:
                    from tools.batch_collect import filter_relevant_content

                    douyin_top20 = douyin_data.get("top20", [])
                    xhs_top20 = xhs_data.get("top20", [])
                    if douyin_top20:
                        douyin_top20 = await filter_relevant_content(
                            douyin_top20,
                            brand_name,
                            industry,
                            platform="douyin",
                            top_n=20,
                        )
                    if xhs_top20:
                        xhs_top20 = await filter_relevant_content(
                            xhs_top20,
                            brand_name,
                            industry,
                            platform="xiaohongshu",
                            top_n=20,
                        )
                except Exception as e:
                    print(f"  ⚠️ 相关性过滤失败(使用原始数据): {e}")
                    douyin_top20 = douyin_data.get("top20", [])
                    xhs_top20 = xhs_data.get("top20", [])

                results["data"]["douyin"] = {
                    "video_count": len(douyin_data.get("videos", [])),
                    "top20": douyin_top20,
                    "raw": douyin_data,
                }
                results["data"]["xiaohongshu"] = {
                    "note_count": len(xhs_data.get("notes", [])),
                    "top20": xhs_top20,
                    "raw": xhs_data,
                }
            else:
                results["data"][task_name] = result

            if not was_cached:
                state.save_checkpoint(task_name, result)

            status = "📂 缓存" if was_cached else "✅ 完成"
            print(f"    {status}: {task_name}")
            # [看板增强] 通知前端每个并行任务的完成状态
            label = task_labels.get(task_name, task_name)
            if progress_callback:
                await progress_callback(
                    "parallel_collect", f"{label}完成 ({idx}/{task_count})", 50 + idx * 7
                )

        # GEO scope 不采集社媒，设置空数据避免下游 KeyError
        if diagnosis_scope == "geo":
            results["data"]["douyin"] = {"video_count": 0, "top20": [], "raw": {}}
            results["data"]["xiaohongshu"] = {"note_count": 0, "top20": [], "raw": {}}

        # 社媒 scope 不做AI可见度和网页搜索，设置空数据
        if diagnosis_scope == "social":
            results["data"]["ai_visibility"] = {"total_engines": 0, "skipped": True}
            results["data"]["web_search"] = {"skipped": True}

        # 设置跳过的数据为空
        results["data"]["scholar_search"] = {"skipped": True}
        results["data"]["industry_analysis"] = {"skipped": True}
        results["data"]["longtail"] = {"skipped": True}
        results["diagnosis_keywords"] = diagnosis_keywords

        # ========== 社媒专项 ASR 视频转写 ==========
        if diagnosis_scope == "social":
            print("\n  🎙️ Phase 2.5: ASR视频转写（社媒核心步骤）...")
            if progress_callback:
                await progress_callback("asr", "正在提取热门视频文案...", 60)

            asr_transcripts = []
            top_videos = results["data"].get("douyin", {}).get("top20", [])[:20]
            if top_videos:
                # 过滤客户自己的视频
                def _is_own_video(video):
                    author_name = video.get("author", {}).get("nickname", "").lower()
                    author_uid = video.get("author", {}).get("unique_id", "").lower()
                    brand_lower = brand_name.lower()
                    if brand_lower in author_name or brand_lower in author_uid or author_name in brand_lower:
                        return True
                    if own_accounts:
                        for acc in own_accounts:
                            if acc.lower() in author_name or author_name in acc.lower():
                                return True
                    return False

                def _get_engagement(video):
                    stats = video.get("stats") or video.get("statistics") or {}
                    digg = stats.get("digg", 0) or stats.get("digg_count", 0) or video.get("like_count", 0)
                    share = stats.get("share", 0) or stats.get("share_count", 0) or video.get("share_count", 0)
                    return digg + share * 3

                competitor_videos = [v for v in top_videos if not _is_own_video(v)]
                if len(top_videos) - len(competitor_videos) > 0:
                    print(f"    已排除 {len(top_videos) - len(competitor_videos)} 条客户视频，保留 {len(competitor_videos)} 条")

                # 按互动量排序，取TOP3
                sorted_videos = sorted(
                    [v for v in competitor_videos if _get_engagement(v) > 0],
                    key=_get_engagement, reverse=True
                )[:3]

                if sorted_videos:
                    print(f"    选择 {len(sorted_videos)} 个高互动视频进行ASR转写...")
                    try:
                        asr_transcripts = await batch_transcribe_videos(
                            sorted_videos, platform="douyin", max_count=3
                        )
                        for i, result in enumerate(asr_transcripts):
                            if i < len(sorted_videos):
                                video = sorted_videos[i]
                                stats = video.get("stats") or video.get("statistics") or {}
                                result["title"] = video.get("desc", "")[:50]
                                result["digg_count"] = stats.get("digg", 0) or stats.get("digg_count", 0) or video.get("like_count", 0)
                                result["author"] = video.get("author", {}).get("nickname", "")
                                result["fans"] = video.get("author", {}).get("follower_count", 0)
                        success_count = sum(1 for r in asr_transcripts if r.get("status") == "success")
                        print(f"    ✅ 成功转写 {success_count} 个视频")
                    except Exception as e:
                        print(f"    ⚠️ ASR转写失败: {e}")
                        asr_transcripts = []
                else:
                    print(f"    ⚠️ 无有效竞品视频可转写")
            else:
                print(f"    ⚠️ 无抖音视频数据，跳过ASR")

            results["data"]["asr_transcripts"] = asr_transcripts
        else:
            # GEO scope 不需要ASR
            results["data"]["asr_transcripts"] = []

        # 品牌社媒内容识别 — GEO scope 不需要（没有社媒数据）
        if diagnosis_scope != "geo":
            try:
                brand_content_stats = get_brand_content_for_scoring(
                    results["data"], brand_name, own_accounts
                )
                results["data"]["brand_identification"] = brand_content_stats
                print(
                    f"  📋 品牌内容识别: 抖音{brand_content_stats.get('douyin_content_count', 0)}条, "
                    f"小红书{brand_content_stats.get('xiaohongshu_content_count', 0)}条"
                )
            except Exception as e:
                print(f"  ⚠️ 品牌内容识别失败: {e}")
                results["data"]["brand_identification"] = {}
        else:
            results["data"]["brand_identification"] = {}

        # ========== Phase 3: 评分和报告生成 ==========
        print("\n⚡ Phase 3: 评分与报告生成...")
        if progress_callback:
            if diagnosis_scope == "social":
                await progress_callback(
                    "scoring", "正在计算社媒评分...", 80
                )
            else:
                await progress_callback(
                    "ai_test", "AI可见度测试完成", 75
                )
                await progress_callback(
                    "scoring", "正在计算GEO评分...", 80
                )

        # === Step 1: 按scope评分 ===
        # [audit P1 2026-06-10] fail-closed 闸门:必须在下方评分 try 之外(except 会吞成"默认 0 分")。
        # 4 引擎采集铁证失败 → raise 穿透 → release_freeze 不扣费,不出 0 分假报告。
        if diagnosis_scope == "geo":
            _assert_ai_visibility_sufficient(results["data"].get("ai_visibility"))
            # [C组①]把最小有效样本判定挂进结果:降级交付时结算侧据此只收已履约部分
            # (未履约不计费);数据由结算侧再校验版本与比例,不合法则转人工,不猜。
            try:
                _v = evaluate_delivery_verdict(results["data"].get("ai_visibility"))
                results["data"]["delivery_verdict"] = {
                    "version": _v.version, "outcome": _v.outcome,
                    "planned": _v.planned, "succeeded": _v.succeeded,
                    "coverage_ratio": round(_v.coverage_ratio, 4),
                    "billable_ratio": round(_v.billable_ratio, 4),
                    "failed_platforms": list(_v.failed_platforms),
                    "message": _v.message,
                }
            except Exception as _ve:
                print(f"  ⚠️ 交付判定附加失败(不影响诊断): {_ve}")

        score_data = {
            "total_score": 0,
            "max_score": 100,
            "level": "空白",
            "dimension_scores": {},
        }
        try:
            if diagnosis_scope == "geo":
                from tools.scoring.geo_scope_scorer import calculate_geo_scope_score
                score_data = calculate_geo_scope_score(
                    ai_visibility_data=results["data"].get("ai_visibility") or {},
                    web_search_data=results["data"].get("web_search") or {},
                )
            elif diagnosis_scope == "social":
                from tools.scoring.social_scope_scorer import calculate_social_scope_score
                score_data = calculate_social_scope_score(
                    platform_data=results["data"],
                    brand_content_stats=results["data"].get("brand_identification") or {},
                    competitor_data=results["data"].get("competitor_analysis") or {},
                )
            results["scores"] = score_data
            print(
                f"  ✅ {scope_labels.get(diagnosis_scope)}评分完成: {score_data.get('total_score', 0)}/{score_data.get('max_score', 100)} ({score_data.get('level', '未知')})"
            )
        except Exception as e:
            print(f"  ⚠️ 评分失败(使用默认): {e}")
            results["scores"] = score_data

        # === Step 2: 报告生成 (即使评分失败也执行) ===
        try:
            report_label = scope_labels.get(diagnosis_scope, diagnosis_scope)
            if progress_callback:
                await progress_callback("report", f"生成{report_label}报告...", 95)

            # [v3.6 白标] 报告是客户可见产物 → surface=customer 解析发起人(代理)品牌
            # 平台默认(无白标/无 creator)→ 传 None 保持现状(报告函数内回退平台)
            _report_branding = None
            try:
                from services.public_whitelabel import resolve_branding_context
                _brand_ctx = resolve_branding_context(
                    surface="customer", owner_user_id=effective_owner_user_id
                )
                if _brand_ctx.get("source") != "platform_default":
                    _report_branding = _brand_ctx.get("brand")
            except Exception:
                _report_branding = None

            # [audit P1 2026-06-10] lite 路径补总超时 600s 对齐 full 路径(:2230)·
            # 旧版裸 await,LLM 挂死 → 诊断卡死成永久 NULL + 冻结悬挂;超时走本 except 降级文案(报告失败不致命)
            enhanced_report = await asyncio.wait_for(
                generate_enhanced_report(
                    brand_name=brand_name,
                    industry=industry,
                    geo_score_data=score_data,
                    ai_visibility_data=results["data"].get("ai_visibility") or {},
                    platform_data=results["data"],
                    competitor_data=results["data"].get("competitor_analysis") or {},
                    business_context=results["data"].get("business_context") or {},
                    asr_transcripts=results["data"].get("asr_transcripts") or [],
                    diagnosis_scope=diagnosis_scope,
                    progress_callback=progress_callback,
                    branding=_report_branding,  # [v3.6 白标]
                ),
                timeout=600,
            )
            # generate_enhanced_report 返回字符串
            if isinstance(enhanced_report, dict):
                results["report"] = enhanced_report.get("report", "")
            else:
                results["report"] = enhanced_report
            results["report_version"] = f"{diagnosis_scope}_v1.0"
            results["review"] = {"status": "skipped", "reason": f"{diagnosis_scope}_mode"}
            print(f"  ✅ 销售版报告生成完成")
        except Exception as e:
            print(f"  ❌ 报告生成失败: {e}")
            import traceback

            traceback.print_exc()  # 打印详细错误堆栈
            results["report"] = f"# 报告生成失败\n\n错误: {e}"

        # ========== 保存结果 ==========
        if not output_file:
            output_file = f"output/{brand_name}_{session_id}"

        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 保存JSON
        # [audit P1 2026-06-10] 写盘包 try 对齐 full 路径配对:磁盘满(ENOSPC·prod 历史发生过)等
        # IO 失败不应让已完成的诊断整体 raise → 失败也扣费;DB 才是结果 SSOT,文件只是导出副本
        json_path = output_path.with_suffix(".json")
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
        except Exception as _io_err:
            print(f"  ⚠️ JSON 导出写盘失败(不影响诊断结果): {_io_err}")

        # 保存Markdown报告
        if results.get("report"):
            md_path = output_path.with_suffix(".md")
            try:
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write(results["report"])
                print(f"\n📄 报告已保存: {md_path}")
                results["report_file"] = str(md_path)
            except Exception as _io_err:
                print(f"  ⚠️ Markdown 导出写盘失败(不影响诊断结果): {_io_err}")

        results["json_file"] = str(json_path)
        print(f"📊 JSON已保存: {json_path}")

        # 保存到数据库
        if progress_callback:
            await progress_callback("saving", "正在保存诊断结果...", 99)
        if creator_user_id:
            results["creator_user_id"] = creator_user_id
        # [返工2 P1-4] DB fencing:写正式产物(save_diagnosis + 下游桥接/装配/外部副作用)**前**确认 run 仍 running。
        #   asyncio.cancel() 无法中断已进入的同步 save_diagnosis/to_thread —— lease 丢失(被 sweeper 收尸
        #   release_pending / 换态)后旧任务若续写正式诊断行,会"钱已 released(withheld)却存在可见产物"。fence 命中即
        #   raise 中止落库 + 全部下游(桥接品牌/报价/v2 装配/信任采集/埋点),交状态机退款。DB 抖动 fail-open(下游兜底)。
        if run_token:
            try:
                from services.diagnosis_runs import run_lease_lost
                if run_lease_lost(run_token):
                    print(f"  🛑 [fencing] run={run_token} 落库前 run 已非 running(lease 丢失/被收尸)· 中止落库")
                    raise RuntimeError(f"diagnosis_run_fenced:run={run_token} 已非 running · 中止落库(lease 丢失)")
            except RuntimeError:
                raise
            except Exception as _fe:
                print(f"  ⚠️ [fencing] run={run_token} 查询异常(放行落库 · 下游结算/可见性兜底): {_fe}")
        try:
            diagnosis_id = save_diagnosis(
                results,
                organization_identity=organization_identity,
            )
            if not isinstance(diagnosis_id, int) or diagnosis_id <= 0:
                raise RuntimeError("diagnosis persistence returned no durable record id")
            results["diagnosis_id"] = diagnosis_id
            print(f"💾 诊断记录已保存: ID={diagnosis_id}")

            # [Bug6补完] 销售版也需要自动创建基础报价单
            try:
                from db.diagnosis_db import (
                    save_quote,
                    get_connection as get_db_conn,
                )

                industry_category = results.get("data", {}).get("industry_category", "")
                bridge_brand_id = _resolve_bridge_brand_id(
                    brand_id=brand_id,
                    brand_name=brand_name,
                    industry=industry,
                    industry_category=industry_category,
                    owner_user_id=effective_owner_user_id,
                )

                # CTO-B 2026-04-26 W2 · 销售版路径 v2 装配 + 落库(决策点 A)
                # · v2 异常不抛 · 仅 log + 写 report_v2_error · 决策点 5 透明化
                try:
                    await _run_diagnosis_v2_assemble_and_persist(
                        diagnosis_id=diagnosis_id,
                        brand_id=bridge_brand_id,
                        results=results,
                        score_data=score_data,
                    )
                    if results.get("report_version") == "v2":
                        _enqueue_report_v3_narrative_if_ready(diagnosis_id)
                except Exception as _v2e:
                    print(f"  ⚠️ [v2 sales-path] 顶层异常 · 跳过(报告保留 v1): {_v2e}")

                # M1a T4 埋点 · stage=diagnosis event=complete
                try:
                    from db.pipeline_stage_log_db import log_stage_event
                    log_stage_event(
                        brand_id=bridge_brand_id,
                        stage_name="diagnosis",
                        event="complete",
                        meta={
                            "diagnosis_id": diagnosis_id,
                            "brand_name": brand_name,
                            "industry": industry,
                            "total_score": results.get("total_score"),
                            "level": results.get("level"),
                            "report_version": results.get("report_version"),
                        },
                        actor_user_id=creator_user_id,
                    )
                except Exception as _le:
                    print(f"  ⚠️ diagnosis stage_log 埋点失败(非 block): {_le}")

                # [P0-D 2026-06-14] 信任资产采集(flag PRICING_P0D_TRUST_COLLECT_ENABLED 控 · 默认关 = 0 行为变化)
                #   fire-and-forget · 复用本次诊断 ai_visibility.detail_table(顺手归并·不重跑搜索)·
                #   绝不 await/阻塞诊断,绝不因采集失败影响诊断结果(collect_and_store 内部全 fail-soft)。
                try:
                    from tools.llm_pricing_flag import is_trust_collect_enabled
                    if is_trust_collect_enabled():
                        from tools.trust_asset_collector import collect_and_store
                        _p0d_detail = (results.get("data", {}).get("ai_visibility") or {}).get("detail_table")
                        asyncio.create_task(collect_and_store(
                            bridge_brand_id, diagnosis_detail_table=_p0d_detail))
                except Exception as _tce:
                    print(f"  ⚠️ [P0-D] 信任资产采集调度失败(非 block): {_tce}")

                conn_check = get_db_conn()
                c_check = conn_check.cursor()
                c_check.execute(
                    "SELECT id FROM quotes WHERE diagnosis_id = %s", (diagnosis_id,)
                )
                existing_quote = c_check.fetchone()
                conn_check.close()

                if existing_quote:
                    print(f"  ⏭️ 该诊断已有报价单: quote_id={existing_quote['id']}")
                else:
                    # P0.5 (CTO-15.7 2026-04-24 空壳 quote 止血):
                    # 删除自动 save_quote · 157→1 付款元凶根治 · 不再制造空壳假商机
                    # 代理主动点"生成 GEO 方案书"CTA 触发(POST /api/diagnosis/{id}/generate-quote ¥400)
                    print(f"  📋 诊断完成 · 方案书生成待代理主动触发 · diagnosis_id={diagnosis_id}")
            except Exception as e:
                print(f"  ⚠️ 自动创建报价单失败（不影响诊断记录）: {e}")
        except Exception as e:
            print(f"⚠️ 数据库保存失败: {e}")
            raise

        # [CTO-15.23 2026-05-05] P1 文案准确:此处只是诊断核心完成
        # server.py 还有后处理(分享链接 + HTML 报告生成 ~30-60s)
        # 真"完工"由 server.py L1503 send done=True 触发
        # 老板原话:不要写"完成了"但实际等 1 分钟才跳转 · 用户会去其他地方找
        if progress_callback:
            try:
                await progress_callback(
                    "saving_report",
                    f"{scope_labels.get(diagnosis_scope, diagnosis_scope)}诊断核心完成 · 正在生成可视化报告...",
                    95,
                )
            except Exception:
                pass

        print("\n" + "=" * 50)
        print(f"⚡ {scope_labels.get(diagnosis_scope, diagnosis_scope)}诊断核心完成 · 后续生成报告页面")
        print("=" * 50 + "\n")

        return results

    # ===== [技术版] 继续原有完整流程 =====
    # 步骤 1 结果已在上面并行执行完毕
    if not isinstance(_kw_result, Exception) and _kw_result:
        diagnosis_keywords = _kw_result
    else:
        # 降级：使用原始关键词
        diagnosis_keywords = keywords[:5]
        print(f"   ⚠️ 关键词生成失败，使用原始关键词")

    results["diagnosis_keywords"] = diagnosis_keywords
    print(f"   ✅ 诊断关键词: {diagnosis_keywords}")

    if progress_callback:
        await progress_callback("keywords", "关键词生成完成", 5)

    # ===== 步骤 2: 社媒平台搜索 =====
    print(" 2/6: ...")
    if progress_callback:
        await progress_callback("social", "正在搜索社媒平台数据...", 15)

    # 构建社媒搜索词：诊断关键词 + 品牌名（验证客户自己的社媒存在）
    social_search_keywords = list(diagnosis_keywords)  # 复制诊断关键词

    # 生成品牌名变体（用于更准确地搜索客户社媒内容）
    def get_brand_variants(name: str) -> list:
        """生成品牌名变体，如 '驰鲸科技' -> ['驰鲸科技', '驰鲸']"""
        variants = [name]
        # 去除常见后缀生成简称
        suffixes = ["科技", "公司", "有限公司", "集团", "网络", "服务", "技术"]
        short_name = name
        for suffix in suffixes:
            short_name = short_name.replace(suffix, "")
        if short_name and short_name != name and len(short_name) >= 2:
            variants.append(short_name)
        return variants

    brand_variants = get_brand_variants(brand_name)

    # 确保品牌名和简称都在搜索词中（关键！用于验证客户社媒存在）
    for variant in reversed(brand_variants):  # 倒序插入，保证全称在前
        if variant not in social_search_keywords:
            social_search_keywords.insert(0, variant)

    print(
        f"  📋 社媒搜索词 ({len(social_search_keywords)}个): {social_search_keywords}"
    )
    print(f"  📋 品牌名变体: {brand_variants}")
    if brand_display_names:
        print(f"  🎯 客户指定品牌别名: {brand_display_names}（用于AI测试/竞品检测）")

    cached_social = state.get_checkpoint("social_data")
    if cached_social:
        social_data = cached_social
    else:
        social_data = await batch_collect_all(social_search_keywords)
        state.save_checkpoint("social_data", social_data)

    #
    douyin_data = social_data.get("douyin", {})
    xhs_data = social_data.get("xiaohongshu", {})

    results["data"]["douyin"] = {
        "video_count": len(douyin_data.get("videos", [])),
        "total_raw": douyin_data.get("total_raw", 0),
        "top20": douyin_data.get("top20", []),
        "raw": douyin_data,
    }
    results["data"]["xiaohongshu"] = {
        "note_count": len(xhs_data.get("notes", [])),
        "total_raw": xhs_data.get("total_raw", 0),
        "top20": xhs_data.get("top20", []),
        "raw": xhs_data,
    }

    print(
        f"   : {results['data']['douyin']['video_count']} , : {results['data']['xiaohongshu']['note_count']} "
    )

    # ===== 步骤 2.5: ASR视频转写（获取真实爆款文案）=====
    # [销售版] 跳过ASR，节省3-5分钟
    if IS_LITE_MODE:
        print(" 2.5/6: ⚡ [销售版] 跳过ASR视频转写")
        if progress_callback:
            await progress_callback("asr", "销售版跳过ASR...", 25)
        asr_transcripts = []
    else:
        print(" 2.5/6: ASR视频转写（提取爆款文案）...")
        if progress_callback:
            await progress_callback("asr", "正在提取热门视频文案...", 25)

        cached_asr = state.get_checkpoint("asr_transcripts")
        # FIX: 只有当缓存中有至少1条成功转写时才跳过，否则重新执行
        has_valid_cache = cached_asr and any(
            r.get("status") == "success" and r.get("text") for r in cached_asr
        )
        if has_valid_cache:
            asr_transcripts = cached_asr
            print(f"  📂 从检查点恢复: asr_transcripts ({sum(1 for r in cached_asr if r.get('status')=='success')}条成功)")
        else:
            if cached_asr:
                print(f"  ⚠️ 检查点中的ASR数据无有效转写，重新执行")
            asr_transcripts = []
        # 从抖音top20中选取竞品的高互动视频进行转写（排除客户自己的视频）
        top_videos = results["data"]["douyin"].get("top20", [])[:20]
        if top_videos and not has_valid_cache:
            # [ENHANCED] 辅助函数：检查是否是客户自己的视频（使用品牌名 + own_accounts）
            def is_client_video(video):
                """排除客户自己的视频，只保留竞品视频用于分析"""
                author_name = video.get("author", {}).get("nickname", "")
                author_name_lower = author_name.lower()
                author_unique = video.get("author", {}).get("unique_id", "").lower()
                brand_lower = brand_name.lower()

                # 1. 检查账号名/昵称是否包含品牌名
                if (
                    brand_lower in author_name_lower
                    or brand_lower in author_unique
                    or author_name_lower in brand_lower
                ):
                    return True

                # 2. [NEW] 检查是否在用户提供的own_accounts列表中
                if own_accounts:
                    for acc in own_accounts:
                        if (
                            acc.lower() in author_name_lower
                            or author_name_lower in acc.lower()
                        ):
                            return True
                        # 精确匹配也检查
                        if author_name == acc:
                            return True

                return False

            # 辅助函数：获取视频互动数据（兼容stats和statistics两种字段名）
            def get_video_engagement(video):
                # TikHub可能返回stats或statistics
                stats = video.get("stats") or video.get("statistics") or {}
                # 点赞数可能是digg/digg_count/like_count
                digg = (
                    stats.get("digg", 0)
                    or stats.get("digg_count", 0)
                    or video.get("like_count", 0)
                )
                # 分享数
                share = (
                    stats.get("share", 0)
                    or stats.get("share_count", 0)
                    or video.get("share_count", 0)
                )
                return digg + share * 3  # 权重计算

            # 辅助函数：获取视频时长（秒）
            def get_video_duration(video):
                return (
                    video.get("duration", 0)
                    or video.get("video", {}).get("duration", 0)
                    or 0
                )

            # 过滤：排除客户自己的视频，只保留竞品
            competitor_videos = [v for v in top_videos if not is_client_video(v)]
            client_video_count = len(top_videos) - len(competitor_videos)
            if client_video_count > 0:
                print(
                    f"   📋 已排除 {client_video_count} 条客户自己的视频，保留 {len(competitor_videos)} 条竞品视频"
                )

            # [NEW] LLM相关性评分：确保选择与客户业务相关的视频
            async def score_video_relevance(videos_list, target_industry):
                """使用LLM评估视频与客户业务的相关性"""
                import httpx, os

                api_key = os.getenv("DASHSCOPE_API_KEY")
                if not api_key or len(videos_list) == 0:
                    return {
                        v.get("aweme_id", str(i)): 5 for i, v in enumerate(videos_list)
                    }  # 默认中等分

                # 准备视频摘要
                video_summaries = []
                for i, v in enumerate(videos_list[:10]):  # 最多评估10条
                    vid = v.get("aweme_id", str(i))
                    title = v.get("desc", v.get("title", "无标题"))[:100]
                    author = v.get("author", {}).get("nickname", "未知")
                    video_summaries.append(f"[{vid}] {title} (作者:{author})")

                prompt = f"""你是行业相关性评估专家。请评估以下视频与"{target_industry}"行业的相关性。

目标行业：{target_industry}
业务关键词：{", ".join(keywords[:5])}

视频列表：
{chr(10).join(video_summaries)}

评分规则（0-10分）：
- 10分：完全相关（视频主题与目标行业高度匹配）
- 7-9分：较相关（有一定关联，可作为行业参考）
- 4-6分：弱相关（边缘行业内容）
- 0-3分：不相关（完全不同的行业，如视频讲千川投放但客户是GEO服务）

⚠️ 特别注意：
- "全域"在抖音通常指"千川全域推广"，与"全域上榜GEO"是完全不同的行业
- 需要排除那些看起来有关键词重叠但实际上是不同行业的视频

请输出JSON格式（只输出JSON，不要其他文字）：
{{"scores": {{"视频ID": 分数, ...}}}}
"""

                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                        response = await client.post(
                            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                            headers={
                                "Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json",
                            },
                            json={
                                "model": "qwen3.7-max",
                                "messages": [{"role": "user", "content": prompt}],
                                "temperature": 0.3,
                                "enable_thinking": False,
                            },
                        )
                        if response.status_code == 200:
                            import json, re

                            content = response.json()["choices"][0]["message"][
                                "content"
                            ]
                            clean = re.sub(r"```json\s*|\s*```", "", content).strip()
                            result = json.loads(clean)
                            return result.get("scores", {})
                except Exception as e:
                    print(f"   ⚠️ LLM相关性评分失败: {e}")

                return {v.get("aweme_id", str(i)): 5 for i, v in enumerate(videos_list)}

            # 使用LLM评估相关性
            valid_videos = [v for v in competitor_videos if get_video_engagement(v) > 0]
            if len(valid_videos) > 3:  # 如果超过3条，用LLM筛选
                print(f"   🔍 使用LLM评估 {len(valid_videos)} 条视频的业务相关性...")
                relevance_scores = await score_video_relevance(valid_videos, industry)

                # 综合评分：相关性权重60% + 互动量权重40%
                for v in valid_videos:
                    vid = v.get("aweme_id", str(id(v)))
                    rel_score = relevance_scores.get(vid, 5)
                    eng_score = min(get_video_engagement(v) / 1000, 10)  # 归一化到0-10
                    v["_combined_score"] = rel_score * 0.6 + eng_score * 0.4

                # 按综合评分排序，取TOP3
                sorted_videos = sorted(
                    valid_videos,
                    key=lambda v: v.get("_combined_score", 0),
                    reverse=True,
                )[:3]
                print(f"   ✅ 已选择 {len(sorted_videos)} 条业务相关视频")
            else:
                # 视频数量少，直接按互动量排序
                sorted_videos = sorted(
                    valid_videos, key=get_video_engagement, reverse=True
                )[:3]

            # 标注长视频（>5分钟），用于切换ASR模型
            for video in sorted_videos:
                duration = get_video_duration(video)
                video["_asr_use_long_model"] = duration > 300  # 超过5分钟
                if video["_asr_use_long_model"]:
                    print(
                        f"   ⏱️ 检测到长视频 ({duration // 60}分{duration % 60}秒)，将使用长视频ASR模型"
                    )

            print(
                f"   有效竞品视频: {len(valid_videos)}/{len(competitor_videos)}, 选择TOP3进行转写"
            )

            if sorted_videos:
                print(f"   选择 {len(sorted_videos)} 个高互动视频进行ASR转写...")
                try:
                    asr_transcripts = await batch_transcribe_videos(
                        sorted_videos, platform="douyin", max_count=3
                    )
                    # 附加视频标题和作者信息
                    for i, result in enumerate(asr_transcripts):
                        if i < len(sorted_videos):
                            video = sorted_videos[i]
                            stats = video.get("stats") or video.get("statistics") or {}
                            result["title"] = video.get("desc", "")[:50]
                            result["digg_count"] = (
                                stats.get("digg", 0)
                                or stats.get("digg_count", 0)
                                or video.get("like_count", 0)
                            )
                            result["author"] = video.get("author", {}).get(
                                "nickname", ""
                            )
                            result["fans"] = video.get("author", {}).get(
                                "follower_count", 0
                            )
                    print(
                        f"   ✅ 成功转写 {sum(1 for r in asr_transcripts if r.get('status') == 'success')} 个视频"
                    )

                    # [Phase 12.7][性能优化v2] 并行分析所有成功转写视频的评论区钩子效果
                    engagement_tasks = []
                    engagement_indices = []
                    for idx, result in enumerate(asr_transcripts):
                        if result.get("status") == "success" and result.get("video_id"):
                            print(f"   📊 分析评论区钩子效果: {result.get('title', '')[:20]}...")
                            engagement_tasks.append(
                                analyze_video_engagement(result["video_id"], result.get("text", ""))
                            )
                            engagement_indices.append(idx)

                    if engagement_tasks:
                        engagement_results = await asyncio.gather(*engagement_tasks, return_exceptions=True)
                        for i, engagement in enumerate(engagement_results):
                            idx = engagement_indices[i]
                            if isinstance(engagement, Exception):
                                print(f"      ⚠️ 评论分析失败: {str(engagement)[:50]}")
                                continue
                            if engagement.get("status") == "success" and engagement.get("analysis"):
                                analysis = engagement["analysis"]
                                asr_transcripts[idx]["comment_analysis"] = {
                                    "total_comments": analysis.get("total_comments", 0),
                                    "hook_responses": analysis.get("hook_responses", 0),
                                    "response_rate": analysis.get("response_rate", 0),
                                    "top_hooks": analysis.get("top_hooks", {}),
                                    "engagement_score": analysis.get("engagement_score", 0),
                                }
                                print(
                                    f"      钩子响应率: {analysis.get('response_rate', 0)}% ({analysis.get('hook_responses', 0)}/{analysis.get('total_comments', 0)})"
                                )

                except Exception as e:
                    print(f"   ⚠️ ASR转写失败: {e}")
                    asr_transcripts = []

        state.save_checkpoint("asr_transcripts", asr_transcripts)

    # 保存到结果
    results["data"]["asr_transcripts"] = asr_transcripts

    # ===== [性能优化] 步骤 3-3.4: 并行执行4个搜索任务 =====
    print(" 3/6: 并行执行搜索任务...")
    if progress_callback:
        await progress_callback("search", "正在并行搜索网络信息...", 40)

    # 构建网页搜索词：使用Step 0蒸馏的搜索词组
    web_search_queries = []
    if business_context_result.get("search_keyword_groups"):
        for group in business_context_result["search_keyword_groups"]:
            # 每个词组合并成一个搜索查询
            query = " ".join(group)
            web_search_queries.append(query)

    # 确保品牌名在搜索词中
    if brand_name not in " ".join(web_search_queries):
        web_search_queries.insert(0, f"{brand_name} {industry}")

    # 最多使用5个搜索词组
    web_search_queries = web_search_queries[:5]
    print(f"  📋 网页搜索词组 ({len(web_search_queries)}个): {web_search_queries}")

    # 定义各搜索任务
    async def do_web_search():
        """网页搜索任务 - 使用Step 0蒸馏的搜索词组"""
        cached = state.get_checkpoint("web_search")
        if cached:
            return ("web_search", cached, True)
        try:
            all_citations = []
            seen_urls = set()
            authority_sources = []

            # [性能优化v2] 并行执行所有搜索词组查询（原为串行for循环）
            web_data_list = await asyncio.gather(
                *[metaso_search_with_citations(query=q, scope="webpage", size=15) for q in web_search_queries],
                return_exceptions=True,
            )
            for web_data in web_data_list:
                if isinstance(web_data, Exception):
                    print(f"      ⚠️ 单个搜索词查询失败: {web_data}")
                    continue
                for citation in web_data.get("citations", []):
                    url = citation.get("url", "")
                    if url and url not in seen_urls:
                        all_citations.append(citation)
                        seen_urls.add(url)
                authority_sources.extend(web_data.get("authority_sources", []))

            # 去重权威来源
            authority_sources = list(set(authority_sources))

            # LLM验证
            try:
                verification_result = await batch_verify_citations_with_llm(
                    brand_name=brand_name,
                    company_full_name=f"{brand_name}公司",
                    industry=industry,
                    citations=all_citations,
                    batch_size=10,
                )
                brand_direct = verification_result["brand_direct"]
                industry_ref = verification_result["industry_reference"]
                all_relevant = brand_direct + industry_ref
            except:
                brand_direct, industry_ref, all_relevant = (
                    [],
                    all_citations,
                    all_citations,
                )

            result = {
                "query": web_search_queries,
                "result_count": len(all_relevant),
                "brand_direct_count": len(brand_direct),
                "industry_ref_count": len(industry_ref),
                "citations": all_relevant[:50],
                "brand_direct_citations": brand_direct[:20],
                "authority_sources": authority_sources,
                "source": "metaso_mcp",
            }
            return ("web_search", result, False)
        except Exception as e:
            return ("web_search", {"result_count": 0, "error": str(e)}, False)

    async def do_scholar_search():
        """学术搜索任务"""
        cached = state.get_checkpoint("scholar_search")
        if cached:
            return ("scholar_search", cached, True)
        try:
            scholar_query = f"{industry} 研究"
            scholar_data = await metaso_scholar_search(scholar_query, size=15)
            raw_citations = scholar_data.get("citations", [])[:10]
            try:
                academic_analysis = await analyze_academic_papers(
                    papers=raw_citations,
                    industry=industry,
                    brand_name=brand_name,
                    business_keywords=[industry, "GEO", "AI搜索"],
                )
                relevant_papers = academic_analysis.get("relevant_papers", [])
            except:
                relevant_papers = raw_citations
                academic_analysis = {}

            result = {
                "query": scholar_query,
                "result_count": len(relevant_papers),
                "citations": relevant_papers,
                "academic_analysis": academic_analysis,
                "source": "metaso_scholar",
            }
            return ("scholar_search", result, False)
        except Exception as e:
            return ("scholar_search", {"result_count": 0, "error": str(e)}, False)

    async def do_document_search():
        """文库搜索任务"""
        cached = state.get_checkpoint("document_search")
        if cached:
            return ("document_search", cached, True)
        try:
            document_query = f"{industry} 白皮书 报告"
            document_data = await metaso_document_search(document_query, size=10)
            result = {
                "query": document_query,
                "result_count": document_data.get("result_count", 0),
                "citations": document_data.get("citations", [])[:10],
                "source": "metaso_document",
            }
            return ("document_search", result, False)
        except Exception as e:
            return ("document_search", {"result_count": 0, "error": str(e)}, False)

    async def do_industry_analysis():
        """行业分析任务"""
        cached = state.get_checkpoint("industry_analysis")
        if cached:
            return ("industry_analysis", cached, True)
        try:
            industry_insights = await metaso_industry_analysis(
                brand_name=brand_name, industry=industry
            )
            return ("industry_analysis", industry_insights, False)
        except Exception as e:
            return (
                "industry_analysis",
                {"insights": [], "top_brands": [], "error": str(e)},
                False,
            )

    # ===== [性能优化v2] 步骤 3-5: 搜索+竞品+AI测试+长尾 全并行 =====
    # 竞品分析只需social_data(Step 2), AI测试只需business_context(Step 0)
    # 它们与搜索任务无数据依赖，可以全部并行执行

    # --- 定义竞品分析任务 ---
    async def do_competitor_analysis():
        """竞品分析任务"""
        cached = state.get_checkpoint("competitor_analysis")
        if cached:
            return ("competitor_analysis", cached, True)
        try:
            keyword_count = len(diagnosis_keywords) if diagnosis_keywords else 5
            competitor_result_response = await identify_competitors(
                social_data=social_data,
                brand_name=brand_name,
                industry=industry,
                deep_analysis=True,
                keyword_count=keyword_count,
                own_accounts=own_accounts,
                specified_competitors=competitors,
            )
            competitor_result = (
                competitor_result_response.data
                if hasattr(competitor_result_response, "data")
                else competitor_result_response
            )
            if competitor_result.get("competitors"):
                benchmark_result = generate_benchmark_report(
                    brand_name=brand_name,
                    own_data=social_data,
                    competitors=competitor_result["competitors"],
                )
                competitor_result["benchmark"] = benchmark_result
            return ("competitor_analysis", competitor_result, False)
        except Exception as e:
            return ("competitor_analysis", {"error": str(e), "competitors": []}, False)

    # --- 准备AI测试问题 ---
    # 优先使用Step 0业务分析生成的真实用户问题
    if (
        business_context_result.get("real_user_questions")
        and len(business_context_result["real_user_questions"]) >= 6
    ):
        ai_test_questions = business_context_result["real_user_questions"][:8]
        print(
            f"   🎯 使用业务分析生成的真实问题 ({len(ai_test_questions)}个: 2品牌词+3地区词+3场景词)"
        )
    else:
        # 降级：使用通用模板（避免额外LLM调用）
        ai_test_questions = [
            f"{industry}哪家好？给我推荐几家靠谱的",
            f"想找{industry}服务商，有推荐的吗",
            f"{industry}一般怎么收费？哪家性价比高",
            f"做{industry}需要注意什么？有好的公司推荐吗",
            f"有没有专业的{industry}团队推荐",
        ]
        print(f"   ⚠️ 使用通用模板问题 ({len(ai_test_questions)}个)")

    # [CTO-15.23 2026-05-21 P1 防御] full 模式也支持自定义诊断问题(跟 IS_LITE_MODE 等价处理)
    # 当前前端 scope='geo' 写死 · 但 server.py 对 full 模式仍扣 extra_cost · 此分支必须处理 custom_questions
    # 否则 scheduler / API 直调 / 未来 full 模式入口会出现"扣钱不跑题"的资金链 BUG
    custom_questions_list_full = []
    if custom_questions:
        _seen_full = set()
        for q in custom_questions:
            if isinstance(q, str) and q.strip() and q.strip() not in _seen_full:
                custom_questions_list_full.append(q.strip())
                _seen_full.add(q.strip())
    try:
        results["input_params"]["custom_questions"] = list(custom_questions_list_full)
    except Exception:
        pass
    if custom_questions_list_full:
        print(f"   📋 自定义诊断问题(full 模式): {len(custom_questions_list_full)} 题")

    # 准备长尾关键词
    if longtail_keywords is None:
        longtail_keywords = [
            f"{industry}{diagnosis_keywords[0]}"
            if diagnosis_keywords
            else f"{industry}",
            f"{diagnosis_keywords[1]}"
            if len(diagnosis_keywords) > 1
            else f"{industry}",
            f"{diagnosis_keywords[2]}"
            if len(diagnosis_keywords) > 2
            else f"{industry}",
        ]

    # --- 定义AI测试任务 ---
    async def do_ai_test():
        """AI可见度测试任务"""
        cached = state.get_checkpoint("ai_visibility")
        if cached:
            return ("ai_visibility", cached, True)
        try:
            # [CTO-15.23 2026-05-21 老板订正] 主动优化 toggle 三分支 · 与 IS_LITE_MODE 一致
            mode_verbatim_full = bool(custom_questions_list_full) and not ai_optimize_custom
            mode_dual_full = bool(custom_questions_list_full) and ai_optimize_custom

            if mode_verbatim_full:
                # verbatim · 只跑 custom
                print(f"   🎯 (full)verbatim 模式:只跑用户填的 {len(custom_questions_list_full)} 题")
                ai_detail_data = await detailed_ai_visibility_test(
                    questions=list(custom_questions_list_full),
                    check_brand=brand_name,
                    industry=industry,
                    engines=ai_engines,
                    custom_brand_variants=brand_display_names,
                    brand_id=brand_id,
                    observation_source_ref=run_token or session_id,
                    observation_scope="custom_primary",
                    owner_user_id=effective_owner_user_id,
                )
                final_test_questions = list(custom_questions_list_full)
                custom_ai_result_full = None
            else:
                # dual(主动优化)或 default(无自定义)· 主表都用 system
                print(
                    f"   🎯 (full){'dual 主动优化' if mode_dual_full else 'default'}:"
                    f" system {len(ai_test_questions)} 题"
                    + (f' + custom {len(custom_questions_list_full)} 题独立 section' if mode_dual_full else '')
                )
                _sys_task = detailed_ai_visibility_test(
                    questions=ai_test_questions,
                    check_brand=brand_name,
                    industry=industry,
                    engines=ai_engines,
                    custom_brand_variants=brand_display_names,
                    brand_id=brand_id,
                    observation_source_ref=run_token or session_id,
                    observation_scope="system_primary",
                    owner_user_id=effective_owner_user_id,
                )
                if mode_dual_full:
                    _cust_task = detailed_ai_visibility_test(
                        questions=list(custom_questions_list_full),
                        check_brand=brand_name,
                        industry=industry,
                        engines=ai_engines,
                        custom_brand_variants=brand_display_names,
                        brand_id=brand_id,
                        observation_source_ref=run_token or session_id,
                        observation_scope="custom_secondary",
                        owner_user_id=effective_owner_user_id,
                    )
                    ai_detail_data, custom_ai_result_full = await asyncio.gather(_sys_task, _cust_task)
                else:
                    ai_detail_data = await _sys_task
                    custom_ai_result_full = None
                final_test_questions = list(ai_test_questions)

            engine_stats = ai_detail_data.get("brand_detection_summary", {}).get(
                "by_engine", {}
            )
            # question_types:default/dual 走 business_context · verbatim 走 LLM 分类(报告 325 修)
            if mode_verbatim_full:
                question_types = await _classify_questions_to_funnel_layers(
                    list(custom_questions_list_full), brand_name, industry
                )
            else:
                question_types = business_context_result.get("question_types", {})

            detail_table = ai_detail_data.get("detail_table", [])
            # [2026-07-22 板块A A3/A6] 五态显式化聚合 · SSOT=services.diagnosis_identity_review
            # YES/NO 进确定分母 · PENDING_IDENTITY/PROVIDER_UNKNOWN/NOT_COLLECTED 单列 · UNKNOWN 不再当 0
            from services.diagnosis_identity_review import aggregate_dimension_stats
            # [WO_BRAND_QUESTION_LEAK 2026-08-05 §2.2] 同上:文本判据兜住上游错标。
            dimension_stats = aggregate_dimension_stats(
                detail_table, question_types,
                brand_name=("" if mode_verbatim_full else brand_name),
            )

            # 双轨模式 custom 独立 section
            custom_visibility = None
            if mode_dual_full and custom_ai_result_full:
                cust_detail = custom_ai_result_full.get("detail_table", []) if isinstance(custom_ai_result_full, dict) else []
                cust_eng = {e: {"detected": 0, "total": 0, "rate": 0} for e in ai_engines}
                for item in cust_detail:
                    for engine, eng_result in (item.get("results") or {}).items():
                        if engine not in cust_eng or not isinstance(eng_result, dict):
                            continue
                        answer = eng_result.get("answer_summary", "")
                        if "查询失败" in answer or (isinstance(answer, str) and answer.startswith("Error")):
                            continue
                        cust_eng[engine]["total"] += 1
                        if eng_result.get("brand_detected"):
                            cust_eng[engine]["detected"] += 1
                for _e in cust_eng.values():
                    _e["rate"] = (_e["detected"] / _e["total"]) if _e["total"] > 0 else 0
                cu_total = sum(e["total"] for e in cust_eng.values())
                cu_detected = sum(e["detected"] for e in cust_eng.values())
                custom_visibility = {
                    "questions": list(custom_questions_list_full),
                    "detail_table": cust_detail,
                    "engine_stats": cust_eng,
                    "total_tests": cu_total,
                    "detected_count": cu_detected,
                    "mention_rate": (cu_detected / cu_total) if cu_total > 0 else 0,
                }

            # [P0-1 ② · 2026-07-26] 全层 0 命中 → 判「疑似品牌识别失败」，不当 0 分交付。
            #   生产实证 brand 278（名字带换行 + "城市:" 标签）→ 诊断 456/468 都是
            #   0 分，客户付费两次拿废报告；换干净名（brand 712）立刻 20 分。
            #   这里不加任何阻断：报告照出、原文照存，只是给交付物打标 + 给出口。
            from services.diagnosis_identity_suspicion import assess_identity_suspicion

            identity_suspicion = assess_identity_suspicion(
                brand_name=brand_name,
                dimension_stats=dimension_stats,
                detail_table=detail_table,
            )
            if identity_suspicion.get("suspected"):
                print(
                    "  ⚠️ [诊断] 全层 0 命中且命中识别可疑信号 "
                    f"{identity_suspicion.get('signals')} → 标记疑似品牌识别失败(不按 0 分交付)"
                )

            tech_bds = ai_detail_data.get("brand_detection_summary", {})
            ai_data = {
                "identity_suspicion": identity_suspicion,
                "test_questions": final_test_questions,
                "engines_tested": ai_engines,
                "total_tests": tech_bds.get("total_tests", 0),
                "total_planned": tech_bds.get("total_planned", 0),
                "total_failed": tech_bds.get("total_failed", 0),
                "overall_mention_rate": tech_bds.get("detection_rate", 0),
                "detected_count": tech_bds.get("detected_count", 0),
                "engine_stats": engine_stats,
                "detail_table": detail_table,
                # [WO 2026-08-06 §1] 疑似同品牌变体汇总(待确认线索,不是命中)。
                #   诊断 561 实证:23/32 格原文写「阿强龙虾」,系统检出了三次
                #   又丢了三次,报告上一个字都没有 —— 这一行就是让它有字。
                "near_miss_summary": ai_detail_data.get("near_miss_summary") or [],
                "question_types": question_types,
                "dimension_stats": dimension_stats,
                "total_engines": len(ai_engines),
                "brand_detected_count": sum(
                    1 for e in engine_stats.values() if e.get("detected", 0) > 0
                ),
                "engines_mentioned": sum(
                    1 for e in engine_stats.values() if e.get("detected", 0) > 0
                ),
                "diagnosis_mode": ("verbatim" if mode_verbatim_full else ("dual" if mode_dual_full else "default")),
                "is_custom_mode": mode_verbatim_full,
                "custom_visibility": custom_visibility,
            }
            return ("ai_visibility", ai_data, False)
        except Exception as e:
            return (
                "ai_visibility",
                {
                    "total_engines": 0,
                    "error": str(e),
                    "test_questions": ai_test_questions,
                },
                False,
            )

    async def do_longtail_test():
        """长尾词测试任务"""
        cached = state.get_checkpoint("longtail")
        if cached:
            return ("longtail", cached, True)
        try:
            longtail_result = await retry_async(
                check_longtail_keywords,
                keywords=longtail_keywords[:3],
                check_brand=brand_name,
                engines=["deepseek"],
            )
            longtail_data = json.loads(longtail_result.content[0]["text"])
            return ("longtail", longtail_data, False)
        except Exception as e:
            return ("longtail", {"coverage_rate": 0, "error": str(e)}, False)

    # [CTO-15.23 2026-05-09] 舆情诊断 · 老板拍板降本增效:复用 4 引擎现有 pipeline + 1 个固定 query
    # 0 新外部 API · 0 新扣费 · 多 4 LLM call ~¥0.05 · 拿到品牌口碑/差评/负面新闻
    # 仅代理端内部视角 render_report_html 渲染(modules_jsonb.sentiment)· 客户决策页不显示
    async def do_sentiment_test():
        """舆情分析任务 · 4 引擎并发 + LLM 分类 + alert"""
        cached = state.get_checkpoint("sentiment")
        if cached:
            return ("sentiment", cached, True)
        try:
            from tools.sentiment_classifier import analyze_brand_sentiment
            sentiment_data = await analyze_brand_sentiment(
                brand_name=brand_name,
                industry=industry,
                engines=ai_engines,
                observation_source_ref=run_token or session_id,
                owner_user_id=effective_owner_user_id,
                brand_id=brand_id,
            )
            return ("sentiment", sentiment_data, False)
        except Exception as e:
            return (
                "sentiment",
                {
                    "error": str(e)[:200],
                    "engines": [],
                    "consensus": "neutral",
                    "alert": False,
                    "engine_count": 0,
                },
                False,
            )

    # ========== [性能优化v2] 一个 gather 执行所有搜索+竞品+AI测试+长尾 ==========
    print("  🚀 并行执行: 搜索(4路) + 竞品分析 + AI测试 + 长尾词测试...")
    if progress_callback:
        await progress_callback("search", "启动并行搜索: 网页+学术+文库+行业分析...", 40)

    # 销售版只执行部分任务
    if IS_LITE_MODE:
        print("  ⚡ [销售版] 跳过学术搜索/行业分析/长尾词测试")
        all_parallel_results = await asyncio.gather(
            do_web_search(),
            do_document_search(),
            do_competitor_analysis(),
            do_ai_test(),
            do_sentiment_test(),
            return_exceptions=True,
        )
        results["data"]["scholar_search"] = {"result_count": 0, "skipped": True}
        results["data"]["industry_analysis"] = {"insights": [], "skipped": True}
        results["data"]["longtail"] = {"coverage_rate": 0, "skipped": True}
    else:
        all_parallel_results = await asyncio.gather(
            do_web_search(),
            do_scholar_search(),
            do_document_search(),
            do_industry_analysis(),
            do_competitor_analysis(),
            do_ai_test(),
            do_longtail_test(),
            do_sentiment_test(),
            return_exceptions=True,
        )

    # 统一处理结果
    all_parallel_results = [
        r if not isinstance(r, Exception) else ("unknown", {"error": str(r)}, False)
        for r in all_parallel_results
    ]
    for task_name, result, was_cached in all_parallel_results:
        results["data"][task_name] = result
        if not was_cached:
            state.save_checkpoint(task_name, result)

    # 打印摘要
    web_result = results["data"].get("web_search", {})
    scholar_result = results["data"].get("scholar_search", {})
    doc_result = results["data"].get("document_search", {})
    industry_result = results["data"].get("industry_analysis", {})

    print(f"   ✅ 并行搜索完成:")
    print(f"      - 网页: {web_result.get('brand_direct_count', 0)}条品牌引用")
    print(f"      - 学术: {scholar_result.get('result_count', 0)}篇论文")
    print(f"      - 文库: {doc_result.get('result_count', 0)}条文档")
    print(f"      - 行业: {len(industry_result.get('insights', []))}个洞察")

    comp_data = results["data"].get("competitor_analysis", {})
    print(f"   ✅ 竞品分析完成: {len(comp_data.get('competitors', []))}个竞品")

    ai_data = results["data"].get("ai_visibility", {})
    # [P0-3 · 2026-08-24] 技术版(full)也要挂交付判定 —— 否则"部分引擎失败"只在 geo 版
    #   按已履约比例扣费(:1188),full 版一律全额,同样的降级交付两种收费口径。
    #   这里只**挂判定**不抛异常:零成功那一档由结算收口
    #   (services/diagnosis_runs.require_complete_product 的耐久行判定)统一拦并退款,
    #   不在采集侧再写第二个会动钱的谓词(同一判断写两处必有一处没人验)。
    try:
        _fv = evaluate_delivery_verdict(ai_data)
        results["data"]["delivery_verdict"] = {
            "version": _fv.version, "outcome": _fv.outcome,
            "planned": _fv.planned, "succeeded": _fv.succeeded,
            "coverage_ratio": round(_fv.coverage_ratio, 4),
            "billable_ratio": round(_fv.billable_ratio, 4),
            "failed_platforms": list(_fv.failed_platforms),
            "message": _fv.message,
        }
    except Exception as _fve:
        print(f"  ⚠️ 交付判定附加失败(不影响诊断): {_fve}")
    print(
        f"   ✅ AI测试完成: {ai_data.get('detected_count', 0)}/{ai_data.get('total_tests', 0)}次检测到品牌"
    )
    for engine, stats in ai_data.get("engine_stats", {}).items():
        print(
            f"      - {engine}: {stats.get('detected', 0)}/{stats.get('total', 0)} 提及"
        )

    all_mentioned = set()
    for item in ai_data.get("detail_table", []):
        for eng_result in item.get("results", {}).values():
            all_mentioned.update(eng_result.get("mentioned_brands", []))
    if all_mentioned and brand_name not in " ".join(all_mentioned):
        print(f"   📊 AI引擎推荐了: {', '.join(list(all_mentioned)[:8])}")

    longtail_data = results["data"].get("longtail", {})
    print(f"   ✅ 长尾词测试完成: 覆盖率{longtail_data.get('coverage_rate', 0)}%")

    if progress_callback:
        await progress_callback("competitor", "竞品分析完成", 55)
        await progress_callback("ai_test", "AI引擎测试完成", 75)

    # =====  6:  =====
    print(" 6/6: ...")
    if progress_callback:
        await progress_callback("report", "正在生成专业诊断报告及优化建议...", 90)
    try:
        # [NEW]  -
        print("   识别品牌账号...")
        social_data = {
            "douyin": results["data"].get("douyin", {}),
            "xiaohongshu": results["data"].get("xiaohongshu", {}),
        }

        # [Debug] 检查社媒数据
        dy_videos = social_data.get("douyin", {}).get("videos", []) or social_data.get(
            "douyin", {}
        ).get("top20", [])
        xhs_notes = social_data.get("xiaohongshu", {}).get(
            "notes", []
        ) or social_data.get("xiaohongshu", {}).get("top20", [])
        print(f"   社媒数据: 抖音{len(dy_videos)}条, 小红书{len(xhs_notes)}条")

        brand_content_stats = get_brand_content_for_scoring(
            social_data=social_data, brand_name=brand_name
        )

        # [Debug] 详细输出品牌识别结果
        print(
            f"   品牌识别结果: has_brand_presence={brand_content_stats.get('has_brand_presence')}"
        )
        if brand_content_stats.get("has_brand_presence"):
            print(f"   发现品牌账号: {brand_content_stats.get('account_count', 0)}个")
            print(
                f"   品牌内容: 抖音{brand_content_stats.get('douyin_content_count', 0)}条, 小红书{brand_content_stats.get('xiaohongshu_content_count', 0)}条"
            )
        else:
            print("   未检测到品牌账号（账号名和内容中均未包含品牌名）")

        results["data"]["brand_identification"] = brand_content_stats

        # [性能优化v2] 内容洞察(LLM) + LLM评分 并行执行
        print("   并行执行: 内容洞察分析 + LLM综合评分...")
        if progress_callback:
            await progress_callback("report", "正在并行进行内容洞察和LLM评分...", 91)

        # 1. Python基础分析（纯计算，立即完成）
        try:
            content_insights = analyze_content_insights(
                douyin_data=results["data"].get("douyin", {}),
                xiaohongshu_data=results["data"].get("xiaohongshu", {}),
                brand_name=brand_name,
            )
        except Exception as e:
            print(f"   内容洞察分析跳过: {e}")
            content_insights = {}

        # 2. 并行: LLM内容解读 + LLM综合评分
        async def _content_llm_task():
            try:
                llm_insights = await analyze_content_with_llm(
                    raw_insights=content_insights,
                    brand_name=brand_name,
                    industry=industry,
                    competitor_data=results["data"]
                    .get("competitor_analysis", {})
                    .get("competitors", []),
                )
                return llm_insights
            except Exception as e:
                print(f"   LLM洞察跳过: {e}")
                return {}

        async def _score_llm_task():
            return await calculate_geo_score_llm(
                brand_name=brand_name,
                industry=industry,
                douyin_data=results["data"].get("douyin", {}),
                xiaohongshu_data=results["data"].get("xiaohongshu", {}),
                web_search_data=results["data"].get("web_search", {}),
                ai_visibility_data=results["data"].get("ai_visibility", {}),
                brand_content_stats=brand_content_stats,
                competitor_data=results["data"].get("competitor_analysis", {}),
            )

        llm_insights, score_data = await asyncio.gather(
            _content_llm_task(),
            _score_llm_task(),
        )

        # 合并结果
        content_insights["llm_insights"] = llm_insights
        results["data"]["content_insights"] = content_insights
        results["scores"] = score_data

        if llm_insights:
            print(f"   AI洞察: {llm_insights.get('ai_summary', '')[:50]}...")
        combined = content_insights.get("combined", {})
        kw_list = combined.get("industry_keywords", [])[:5]
        keyword_str = ", ".join([k.get("keyword", "") for k in kw_list])
        if keyword_str:
            print(f"   热门话题: {keyword_str}")

        # [Phase 10] 生成专业优化建议
        print("   生成专业优化建议...")
        if progress_callback:
            await progress_callback("report", "正在生成专业优化建议...", 92)
        try:
            action_plan_result = await generate_professional_action_plan(
                brand_name=brand_name,
                industry=industry,
                score_data=score_data,
                brand_identification=results["data"].get("brand_identification", {}),
                content_insights=results["data"].get("content_insights", {}),
                competitor_data=results["data"]
                .get("competitor_analysis", {})
                .get("competitors", []),
                industry_analysis=results["data"].get("industry_analysis", {}),
            )
            results["data"]["action_plan"] = action_plan_result
            print(
                f"   优化建议已生成 (基于{len(action_plan_result.get('weak_dimensions', []))}个短板维度)"
            )
        except Exception as e:
            print(f"   优化建议生成失败: {e}")
            results["data"]["action_plan"] = {}

        # [Phase 12] 生成报告 - 支持增强版和基础版
        if progress_callback:
            await progress_callback("report", "正在生成诊断报告内容...", 93)
        if USE_ENHANCED_REPORT:
            print(f"   使用 LLM 增强版报告生成（{scope_labels.get(diagnosis_scope, diagnosis_scope)}）...")
            # [NEW] 初始化Token追踪器
            TokenTracker.get_instance().reset()

            # [v3.6 白标] 报告是客户可见产物 → surface=customer 解析发起人(代理)品牌
            # 平台默认(无白标/无 creator)→ 传 None 保持现状(报告函数内回退平台)
            _report_branding = None
            try:
                from services.public_whitelabel import resolve_branding_context
                _brand_ctx = resolve_branding_context(
                    surface="customer", owner_user_id=effective_owner_user_id
                )
                if _brand_ctx.get("source") != "platform_default":
                    _report_branding = _brand_ctx.get("brand")
            except Exception:
                _report_branding = None

            try:
                # [Deploy-CTO 2026-05-22] 加 10 min timeout 防 LLM 章节生成 hang(实证 session 4jZrVn 卡 91% 1 小时)
                # 超时落入 except → fallback basic 版报告 → 用户可正常完成诊断 · 不会永久卡死
                enhanced_report = await asyncio.wait_for(
                    generate_enhanced_report(
                        brand_name=brand_name,
                        industry=industry,
                        geo_score_data=score_data,
                        platform_data=results["data"],
                        ai_visibility_data=results["data"].get("ai_visibility"),
                        competitor_data=results["data"].get("competitor_analysis", {}),
                        content_insights=results["data"].get("content_insights", {}),
                        asr_transcripts=results["data"].get(
                            "asr_transcripts", []
                        ),
                        business_context=results["data"].get(
                            "business_context", {}
                        ),
                        industry_analysis=results["data"].get(
                            "industry_analysis", {}
                        ),
                        web_search_data=results["data"].get("web_search", {}),
                        diagnosis_scope=diagnosis_scope,
                        progress_callback=progress_callback,
                        branding=_report_branding,  # [v3.6 白标]
                    ),
                    timeout=600,  # 10 分钟
                )
                results["report"] = enhanced_report
                results["diagnosis_scope"] = diagnosis_scope
                results["report_version"] = f"{diagnosis_scope}_v1.0"
            except asyncio.TimeoutError:
                print(f"   ⚠️ LLM 增强报告超时(>10min) · 回退到基础版")
                report_result = await asyncio.wait_for(
                    generate_geo_report(
                        brand_name=brand_name,
                        industry=industry,
                        geo_score_data=score_data,
                        ai_visibility_data=results["data"].get("ai_visibility"),
                        platform_data=results["data"],
                        company_profile=upload_file,
                        additional_info=additional_info,
                    ),
                    timeout=300,
                )
                results["report"] = report_result.content[0]["text"]
                results["report_version"] = "basic_v5.1_timeout_fallback"
            except Exception as e:
                print(f"   LLM增强报告失败，回退到基础版: {e}")
                report_result = await asyncio.wait_for(
                    generate_geo_report(
                        brand_name=brand_name,
                        industry=industry,
                        geo_score_data=score_data,
                        ai_visibility_data=results["data"].get("ai_visibility"),
                        platform_data=results["data"],
                        company_profile=upload_file,
                        additional_info=additional_info,
                    ),
                    timeout=300,
                )
                results["report"] = report_result.content[0]["text"]
                results["report_version"] = "basic_v5.1"
        else:
            # [Deploy-CTO 2026-05-22] basic 版也加 5 min timeout 防 LLM hang
            report_result = await asyncio.wait_for(
                generate_geo_report(
                    brand_name=brand_name,
                    industry=industry,
                    geo_score_data=score_data,
                    ai_visibility_data=results["data"].get("ai_visibility"),
                    platform_data=results["data"],
                    company_profile=upload_file,
                    additional_info=additional_info,
                ),
                timeout=300,
            )
            results["report"] = report_result.content[0]["text"]
            results["report_version"] = "basic_v5.1"
        print(f"   评分完成: {score_data.get('total_score', 0)}/100")

        # ===== 步骤 6.5: 报告审核 =====
        # [性能优化v2] 移除冗余的review_and_iterate
        # chief_editor_review已在generate_enhanced_report内部执行了报告审校
        # review_and_iterate与其功能重复，去掉可省30-90秒且不影响质量
        results["review"] = {"status": "skipped", "reason": "chief_editor_already_reviewed"}
        if progress_callback:
            await progress_callback("review", "报告审核完成", 98)
    except Exception as e:
        print(f"   评分/报告失败: {e}")
        results["scores"] = {"error": str(e)}

    #  ( output )
    if not output_file:
        output_file = f"output/{brand_name}_{session_id}"

    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    #  JSON
    json_path = output_path.with_suffix(".json")
    md_path = output_path.with_suffix(".md")  # #4 修复：提前定义，防止 NameError
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f" JSON : {json_path}")
    except Exception as e:
        print(f"  ⚠️ JSON文件保存失败: {e}")

    #  Markdown
    if results.get("report"):
        md_path = output_path.with_suffix(".md")
        try:
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(results["report"])
            print(f"\n : {md_path}")
        except Exception as e:
            print(f"  ⚠️ Markdown文件保存失败: {e}")

    # 保存报告文件路径到results中
    results["report_file"] = str(md_path) if results.get("report") else ""
    results["json_file"] = str(json_path)

    # [Phase 17] 保存到数据库
    if creator_user_id:
        results["creator_user_id"] = creator_user_id
    # [返工2 P1-4] DB fencing:写正式产物前确认 run 仍 running(与主路径一致 · lease 丢失即中止落库 · DB 抖动 fail-open)
    if run_token:
        try:
            from services.diagnosis_runs import run_lease_lost
            if run_lease_lost(run_token):
                print(f"  🛑 [fencing] run={run_token} 落库前 run 已非 running(lease 丢失/被收尸)· 中止落库")
                raise RuntimeError(f"diagnosis_run_fenced:run={run_token} 已非 running · 中止落库(lease 丢失)")
        except RuntimeError:
            raise
        except Exception as _fe:
            print(f"  ⚠️ [fencing] run={run_token} 查询异常(放行落库 · 下游结算/可见性兜底): {_fe}")
    try:
        diagnosis_id = save_diagnosis(
            results,
            organization_identity=organization_identity,
        )
        if not isinstance(diagnosis_id, int) or diagnosis_id <= 0:
            raise RuntimeError("diagnosis persistence returned no durable record id")
        results["diagnosis_id"] = diagnosis_id  # 保存ID供前端使用
        print(f"  💾 诊断记录已保存: ID={diagnosis_id}")

        # [Bug6修复] 自动创建基础报价单，桥接 诊断→报价→写作 数据流
        try:
            from db.diagnosis_db import (
                save_quote,
                get_connection as get_db_conn,
            )

            # brand_id 由调用方传入(现有品牌可信身份 SSOT);
            # 只有真的没有时才在 _resolve_bridge_brand_id 里兜底建档。
            # 🔴 原注释写的是「save_diagnosis 内部已创建,这里获取」——
            #    实核 save_diagnosis 体内零 get_or_create_brand,它不建品牌。
            industry_category = results.get("data", {}).get("industry_category", "")
            bridge_brand_id = _resolve_bridge_brand_id(
                brand_id=brand_id,
                brand_name=brand_name,
                industry=industry,
                industry_category=industry_category,
                owner_user_id=effective_owner_user_id,
            )

            # CTO-B 2026-04-26 W2 · technical_full 路径 v2 装配 + 落库(决策点 A)
            # · 注意:此路径 results["report"] 已写入 .md 文件 · v2 写库后追加重写 .md
            try:
                await _run_diagnosis_v2_assemble_and_persist(
                    diagnosis_id=diagnosis_id,
                    brand_id=bridge_brand_id,
                    results=results,
                    score_data=score_data,
                )
                if results.get("report_version") == "v2":
                    _enqueue_report_v3_narrative_if_ready(diagnosis_id)
                # 如果 v2 装配成功 · 用 v2 markdown 重写 .md 文件(老路径 share_api 仍读这个)
                if results.get("report_version") == "v2" and results.get("report"):
                    try:
                        with open(md_path, "w", encoding="utf-8") as f:
                            f.write(results["report"])
                        print(f"  📝 [v2] .md 文件已用 v2 markdown 重写: {md_path}")
                    except Exception as _wmd:
                        print(f"  ⚠️ [v2] .md 文件重写失败(非 block): {_wmd}")
            except Exception as _v2e:
                print(f"  ⚠️ [v2 full-path] 顶层异常 · 跳过(报告保留 v1): {_v2e}")

            # 检查是否已有该诊断的报价单（防止重复创建）
            conn_check = get_db_conn()
            c_check = conn_check.cursor()
            c_check.execute(
                "SELECT id FROM quotes WHERE diagnosis_id = %s", (diagnosis_id,)
            )
            existing_quote = c_check.fetchone()
            conn_check.close()

            if existing_quote:
                print(f"  ⏭️ 该诊断已有报价单: quote_id={existing_quote['id']}")
            else:
                # P0.5 (CTO-15.7 2026-04-24 空壳 quote 止血):
                # 删除自动 save_quote · 157→1 付款元凶根治 · 不再制造空壳假商机
                # 代理主动点"生成 GEO 方案书"CTA 触发(POST /api/diagnosis/{id}/generate-quote ¥400)
                print(
                    f"  📋 诊断完成 · 方案书生成待代理主动触发 · diagnosis_id={diagnosis_id}, brand_id={bridge_brand_id}"
                )
        except Exception as e:
            print(f"  ⚠️ 自动创建报价单失败（不影响诊断记录）: {e}")

        # [NEW] 自动提取客户资料（如果有additional_info或file_content）
        if additional_info and len(additional_info.strip()) > 50:
            try:
                from db.diagnosis_db import save_client_materials, get_client_materials
                import httpx
                import os

                async def call_llm_inline(prompt: str) -> str:
                    """内联的LLM调用函数 · [failover 2026-06-11] deepseek 多 key 失败自动换下一个(单 key=直调)·保留 DASHSCOPE 跨provider兜底"""
                    from services.llm.deepseek_key_pool import has_deepseek_key, adeepseek_post_with_failover
                    body = {
                        "model": DEEPSEEK_OFFICIAL_FLASH,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                    }
                    if has_deepseek_key():
                        resp = await adeepseek_post_with_failover(body, timeout=60.0)
                        return resp.json()["choices"][0]["message"]["content"]
                    # 无 deepseek key:回落 DASHSCOPE(跨 provider 兜底·保持原逻辑)
                    api_key = os.getenv("DASHSCOPE_API_KEY")
                    if not api_key:
                        raise ValueError("No API key configured")
                    async with httpx.AsyncClient(timeout=60.0) as client:
                        response = await client.post(
                            "https://api.deepseek.com/v1/chat/completions",
                            headers={
                                "Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json",
                            },
                            json=body,
                        )
                        if response.status_code == 200:
                            return response.json()["choices"][0]["message"]["content"]
                        raise Exception(f"LLM call failed: {response.status_code}")

                # 检查是否已有客户资料
                existing = get_client_materials(diagnosis_id)
                if not existing:
                    print(f"   📋 自动提取客户资料...")
                    extract_prompt = f"""你是一个专业的信息提取助手。请从以下公司资料中提取关键信息，并以JSON格式返回。

【公司资料】
{additional_info[:8000]}

【请提取以下信息，返回JSON格式】
{{
  "company_intro": "公司简介（200字内）",
  "founding_year": 2018,
  "team_size": "团队规模（如50-100人）",
  "service_area": "服务范围",
  "unique_value": "一句话价值主张",
  "methodology": "服务方法论/流程",
  "core_selling_points": [
    {{"point": "核心卖点1", "evidence": "证据/数据"}},
    {{"point": "核心卖点2", "evidence": "证据/数据"}}
  ],
  "case_studies": [
    {{"client": "客户名称", "result": "效果数据", "timeline": "服务周期"}}
  ]
}}

注意：
- 只提取资料中明确提到的信息，不要编造
- 如果某类信息不存在，返回空数组或空字符串
- founding_year如果不确定返回null
- 只返回JSON，不要其他内容
"""
                    result = await call_llm_inline(extract_prompt)

                    # 解析JSON
                    json_str = result.strip()
                    if json_str.startswith("```"):
                        json_str = json_str.split("```")[1]
                        if json_str.startswith("json"):
                            json_str = json_str[4:]
                    json_str = json_str.strip()

                    extracted = json.loads(json_str)
                    save_client_materials(
                        diagnosis_id,
                        extracted,
                        organization_identity=organization_identity,
                    )
                    print(f"   ✅ 客户资料已自动提取并保存")
            except Exception as e:
                print(f"   ⚠️ 客户资料自动提取失败: {e}")
    except Exception as e:
        print(f"  ⚠️ 数据库保存失败: {e}")
        raise

    #
    results["success"] = True

    # 技术版完成信号（销售版在line 649已发送）
    if progress_callback:
        try:
            await progress_callback("complete", "技术版诊断完成!", 100)
        except Exception:
            pass

    return results


#
if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv

    load_dotenv()

    parser = argparse.ArgumentParser(description="GEO ")
    parser.add_argument("--brand", required=True, help="")
    parser.add_argument("--industry", required=True, help="")
    parser.add_argument("--keywords", nargs="+", default=[], help="")
    parser.add_argument("--output", default="output/diagnosis", help="")

    args = parser.parse_args()

    asyncio.run(
        run_diagnosis_workflow(
            brand_name=args.brand,
            industry=args.industry,
            keywords=args.keywords,
            output_file=args.output,
        )
    )
