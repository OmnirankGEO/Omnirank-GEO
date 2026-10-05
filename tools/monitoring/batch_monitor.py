"""
批量监测引擎 v1.0
功能：
- 高并发调度（可配置并发数，默认10）
- 平台适配器（可扩展）
- 速率限制器
- 结果聚合
"""

import asyncio
import hashlib
import inspect
import logging
import time
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass
from datetime import datetime
from tools.llm_call_tracker import llm_tracking_context
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

# 导入AI测试工具
import sys
sys.path.append('../..')

from tools.ai_visibility.ai_tester import (
    query_dashscope_search,
    query_kimi_search,
    query_doubao_search,
    query_dashscope_deepseek,  # legacy 对照表面(deepseek_dashscope_search_legacy),已不在默认矩阵
    query_deepseek_official,
    query_yuanbao,
)

from config.ai_engines import MONITORING_RUN_CELL_PLATFORMS

from db.monitoring_db import (
    DEFAULT_MONITORING_PLATFORMS,
    get_keywords_for_monitoring,
    create_monitoring_task,
    update_task_status,
    batch_save_results,
    save_monitoring_result,
    finish_monitoring_cell_error,
    get_monitoring_config,
    save_trend_stat,
    get_keyword_trend
)

logger = logging.getLogger("GEO-Monitoring")


@dataclass
class MonitoringTask:
    """单个监测任务"""
    keyword_id: int
    keyword: str
    target_brand: str
    platform: str
    question: str
    search_mode: str = "standard"  # "standard" | "enhanced"，仅 doubao 生效
    keyword_source: str = "unknown"
    keyword_type: str = "monitoring"
    keyword_resolver_status: str = "resolved"
    cell_id: Optional[int] = None
    cell_claim_token: Optional[str] = None
    provider_dispatched: bool = False


class RateLimiter:
    """速率限制器"""
    
    def __init__(self, requests_per_second: float = 5.0):
        self.min_interval = 1.0 / requests_per_second
        self.last_request_time = 0.0
        self._lock = asyncio.Lock()
    
    async def wait(self):
        """等待直到可以发送下一个请求"""
        async with self._lock:
            now = time.time()
            elapsed = now - self.last_request_time
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self.last_request_time = time.time()


def _filter_competitor_brands(mentioned, target_brand):
    """竞品共现:过滤目标品牌别名 + 去重 + 去空(纯逻辑 · 可单测 · 不新增 LLM 调用)。"""
    tb = (target_brand or "").strip().lower()
    seen = set()
    out = []
    for b in (mentioned or []):
        if not isinstance(b, str):
            continue
        bs = b.strip()
        if not bs:
            continue
        bl = bs.lower()
        if bl in seen:
            continue
        if tb and (tb in bl or bl in tb):  # 目标品牌/别名不算竞品
            continue
        seen.add(bl)
        out.append(bs)
    return out


def _lineage_model_fields(data, runtime_model: str) -> dict:
    """血缘的 ``model`` / ``model_source`` 两列 —— **一处**算,两个返回点共用。

    [工单 V3-C · C-3 · Codex 三审 P1-5]

    🔴 分开在两个 return 里各写一遍就是"同一谓词写两处":本仓记过,
       必有一处没人验,而两条路径落的来源不一致时没有任何判据会红。

    🔴 ``_resolve_runtime_lineage`` 返回的是**计划**模型(平台合同表),
       它回答"我们打算发哪个模型"。只有 adapter 在响应里真拿到回显
       (``echoed_model``)时才是 ``provider_echo``;拿不到就是
       ``planned_fallback`` —— 不假装被证实。
    """
    from services import monitoring_lineage as _ml

    echo = ""
    if isinstance(data, dict):
        echo = str(data.get("echoed_model") or "").strip()
    return {
        "model": echo or runtime_model,
        "model_source": (_ml.MODEL_SOURCE_PROVIDER_ECHO if echo
                         else _ml.MODEL_SOURCE_PLANNED_FALLBACK),
    }


class MonitoringLineageUnregistered(RuntimeError):
    """运行时血缘未登记该平台 —— 不猜 provider/model，fail-loud。"""


def _resolve_runtime_lineage(platform: str, search_mode: str) -> tuple[str, str, str, str]:
    """平台 → (provider, **计划** model, surface, actual_search_mode) 的**唯一**血缘表。

    🔴 [工单 V3-C · C-3] 这里的 model 是**计划值**(平台合同),不是实际值。
       它恒非空 —— 所以"model 非空 ⇒ provider_echo"那个旧谓词在生产上
       一次都不会走到 planned_fallback。取实际值走 ``_lineage_model_fields``。

    [P0-2 · 2026-07-26] 这张表原本在 ``PlatformAdapter.query`` 里抄了两份，
    统一五引擎时漏改一处 → 元宝那一格 ``KeyError`` 被当成 provider 失败，
    客户看到"该平台本次未返回可用结果"，真实原因却是我们没登记血缘。
    现在只有这一处；未登记平台显式抛错，由调用方转成 engine_error 并留日志，
    绝不静默编一个 provider/model 写进观测账本。
    """
    contract = {
        "dashscope": ("dashscope", "qwen3-max", "ai_search", "forced_search"),
        # [2026-07-27] DeepSeek 换官方原生检索。改前 provider 是 dashscope ——
        # 我们以为在测 DeepSeek,实际测的是阿里的检索行为(引用产出率 0.8%,
        # 而同用阿里检索的通义是 29.2%)。三处血缘必须与执行层同步,见工单 §1。
        #: 🔴🔴 [WO_221-c1] 这一格是**写进观测账本的那个标签**,不是调用参数 ——
        #:   它错了,整列 DeepSeek 监测数据的引擎名就是错的,而且不会报错。
        #:   官方页 `deepseek-v4-flash` 已退役。**两条端点行为不同**(2026-09-15 实测):
        #:     /v1/chat/completions   请求旧名 -> 200,回显 `deepseek-flash`(厂商归一)
        #:     /anthropic/v1/messages 请求旧名 -> 200,回显 `deepseek-v4-flash`(**原样**)
        #:   本行是血缘标签、不发请求,两条都适用:标签必须是**服务端实际承接的那个**。
        #:   🔴 下面 dashscope_fallback 那一格**保持 v4-flash 不动**:
        #:      百炼线上这个 ID 是另一家的**活**模型,DeepSeek 官方改名不改百炼。
        "deepseek": ("deepseek_official", DEEPSEEK_OFFICIAL_FLASH, "ai_search", "deepseek_native"),
        "kimi": ("moonshot", "kimi-k2.6", "ai_search", "web_search_tool"),
        # [2026-07-27] 元宝转联网:同表面 yuanbao_hy3_tokenhub,检索由 TokenHub 服务端执行。
        # [2026-08-03 订正] 模型改回 `hy3`:preview 8/31 下线 + 免费包耗尽未开后付费(402/401008);
        # 且"hy3 静默不搜"已被当日生产实测推翻(11 条 search_results / tool_usage.web_search_call=3)。
        "yuanbao": ("tencent_tokenhub", "hy3", "ai_search", "tencent_tokenhub"),
        "doubao": (
            "volc_ark",
            "doubao-seed-1-6-251015" if search_mode == "enhanced"
            else "doubao-seed-2-0-pro-260215",
            "ai_search",
            search_mode,
        ),
    }
    # [零检索兜底 · 2026-07-27] 官方零检索且软重试用尽 → 走阿里兜底。
    #   这一格的血缘必须**如实落成 dashscope**,不能继续写 deepseek_official ——
    #   否则就是红线禁止的"静默回落":报表上写着 DeepSeek,数据其实来自阿里。
    #   触发方式:query_deepseek_official 在兜底时把 search_mode 置为 dashscope_fallback。
    if platform == "deepseek" and search_mode == "dashscope_fallback":
        return ("dashscope", "deepseek-v4-flash",
                "deepseek_dashscope_search_legacy", "dashscope_fallback")

    if platform not in contract:
        raise MonitoringLineageUnregistered(platform)
    return contract[platform]


class PlatformAdapter:
    """平台适配器 - 统一接口调用不同AI平台"""
    
    # [P0-2 · 2026-07-26] 统一五引擎:元宝进监测执行矩阵。
    # 若这里缺了某个默认平台，``eligible_monitoring_platforms`` 会把它当
    # "non-collectable" 静默剔除 → 客户看到的引擎清单又和实际跑的不一致，
    # 也就是 P0-2 换个形式复发。判别测试锁住"默认矩阵 ⊆ SUPPORTED_PLATFORMS"。
    #
    # ⚠️ [2026-08-04] 本表回答的是「adapter 有没有实现这个平台的查询」,**不是**
    # 「监测这次会不会跑它」。后者还要过耐久账本可执行面
    # ``config.ai_engines.MONITORING_RUN_CELL_PLATFORMS``(DB CHECK 钉死的四路)。
    # yuanbao 在本表里有实现、在商品矩阵里已售,但账本当前存不下 → 监测不跑它。
    # 判"这次跑哪些引擎"一律看 ``eligible_monitoring_platforms`` 的返回,别读本表。
    SUPPORTED_PLATFORMS = {
        "dashscope": query_dashscope_search,
        "deepseek": query_deepseek_official,
        "kimi": query_kimi_search,
        "doubao": query_doubao_search,
        "yuanbao": query_yuanbao,
    }

    @staticmethod
    def normalize_monitoring_platforms(
        platforms: Optional[List[str]],
    ) -> List[str]:
        """Normalize a platform list without granting any entitlement."""
        raw_items = platforms.split(",") if isinstance(platforms, str) else (platforms or [])
        normalized: List[str] = []
        seen = set()
        for raw_platform in raw_items:
            if not isinstance(raw_platform, str):
                continue
            platform = raw_platform.strip().lower()
            if platform and platform not in seen:
                seen.add(platform)
                normalized.append(platform)
        return normalized

    @classmethod
    def eligible_monitoring_platforms(
        cls,
        platforms: Optional[List[str]],
        entitled_platforms: Optional[List[str]] = None,
    ) -> List[str]:
        """Return the ordered monitoring platforms allowed by config and entitlement.

        可执行 = 偏好 ∩ 授权 ∩ adapter 有实现 ∩ **耐久账本存得下**。

        🔴 最后那一项(``MONITORING_RUN_CELL_PLATFORMS``)是 2026-08-04 补的,它让本
        函数与 ``db.monitoring_db.create_monitoring_run_cells`` 读**同一个**白名单。
        在此之前两层各读各的:本层按 5 路 ``SUPPORTED_PLATFORMS`` 裁,cells 层按 4 路
        写,于是 entitlement 含 yuanbao 的词必然让 cells 层的
        ``executable ⊆ entitlement`` 守卫炸掉**整批**任务(生产 1543/1544 双 failed)。
        守卫本身不放宽(它是资金/授权闸),改成正常路径永远够不着它。

        账本存不下的平台按 ``ledger`` 原因单独 warning:它和「没买」「没实现」是三种
        完全不同的运维处置,合并成一句 "non-collectable" 会让排查从这里断掉。
        """
        candidates = cls.normalize_monitoring_platforms(platforms)

        entitlement = None
        if entitled_platforms is not None:
            entitlement = set(cls.normalize_monitoring_platforms(entitled_platforms))

        eligible: List[str] = []
        seen = set()
        skipped_unentitled: List[str] = []
        skipped_unsupported: List[str] = []
        skipped_ledger: List[str] = []
        for raw_platform in candidates:
            platform = raw_platform
            if platform in seen:
                continue
            seen.add(platform)
            if entitlement is not None and platform not in entitlement:
                skipped_unentitled.append(platform)
                continue
            if platform not in cls.SUPPORTED_PLATFORMS:
                skipped_unsupported.append(platform)
                continue
            if platform not in MONITORING_RUN_CELL_PLATFORMS:
                skipped_ledger.append(platform)
                continue
            eligible.append(platform)

        for reason, skipped in (
            ("unentitled", skipped_unentitled),
            ("unsupported", skipped_unsupported),
            ("ledger", skipped_ledger),
        ):
            if skipped:
                logger.warning(
                    "[Monitoring] skipped non-collectable platforms (%s): %s",
                    reason,
                    ",".join(skipped),
                )
        return eligible

    @classmethod
    def resolve_requested_monitoring_platforms(
        cls,
        requested_platforms: Optional[List[str]],
        entitled_platforms: Optional[List[str]],
    ) -> Dict[str, List[str]]:
        """Resolve an explicit request against the canonical purchased platform set.

        An omitted request uses the collectable portion of the entitlement. An explicit
        request is fail-loud when it contains any unavailable or unentitled platform;
        callers must reject before task creation, billing, or provider execution.
        """
        normalized_entitlement = cls.normalize_monitoring_platforms(
            entitled_platforms
        )

        requested_items = (
            requested_platforms.split(",")
            if isinstance(requested_platforms, str)
            else (requested_platforms or [])
        )
        normalized_requested = []
        seen_requested = set()
        for raw_platform in requested_items:
            if not isinstance(raw_platform, str):
                continue
            platform = raw_platform.strip().lower()
            if platform and platform not in seen_requested:
                seen_requested.add(platform)
                normalized_requested.append(platform)

        if not normalized_requested:
            return {
                "platforms": cls.eligible_monitoring_platforms(normalized_entitlement),
                "rejected": [],
            }

        effective = cls.eligible_monitoring_platforms(
            normalized_requested,
            normalized_entitlement,
        )
        effective_set = set(effective)
        return {
            "platforms": effective,
            "rejected": [
                platform
                for platform in normalized_requested
                if platform not in effective_set
            ],
        }

    @classmethod
    def resolve_keyword_monitoring_plan(
        cls,
        keywords: List[Dict[str, Any]],
        requested_platforms: Optional[List[str]] = None,
        configured_platforms: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Resolve the executable platform matrix from persisted per-keyword rights.

        `monitoring_config` is only a run preference. Confirmed keywords inherit
        their immutable purchased matrix through
        `confirmed_keywords.monitoring_product_version`; brand-only extra keywords
        use the persisted `extra_keywords.platforms` owner-config snapshot. A
        missing snapshot grants nothing. Explicit requests fail as a whole if any
        selected keyword does not own every requested, collectable platform.
        """
        requested = cls.normalize_monitoring_platforms(requested_platforms)
        configured = cls.normalize_monitoring_platforms(configured_platforms)
        explicit = bool(requested)
        desired = requested if explicit else configured
        planned: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        platform_union: List[str] = []
        union_seen = set()

        for keyword in keywords or []:
            entitlement = cls.normalize_monitoring_platforms(
                keyword.get("entitlement_platforms")
            )
            effective = cls.eligible_monitoring_platforms(desired, entitlement)
            effective_set = set(effective)
            if explicit:
                denied = [platform for platform in requested if platform not in effective_set]
                if denied:
                    rejected.append({
                        "keyword_id": keyword.get("id"),
                        "source": keyword.get("source") or "unknown",
                        "quote_id": keyword.get("quote_id"),
                        "platforms": denied,
                    })
                    continue

            if not effective:
                skipped.append({
                    "keyword_id": keyword.get("id"),
                    "source": keyword.get("source") or "unknown",
                    "quote_id": keyword.get("quote_id"),
                })
                continue

            planned_keyword = dict(keyword)
            planned_keyword["_eligible_monitoring_platforms"] = effective
            planned.append(planned_keyword)
            for platform in effective:
                if platform not in union_seen:
                    union_seen.add(platform)
                    platform_union.append(platform)

        return {
            "keywords": [] if rejected else planned,
            "platforms": [] if rejected else platform_union,
            "rejected": rejected,
            "skipped": skipped,
            "explicit": explicit,
        }
    
    @classmethod
    def get_supported_platforms(cls) -> List[str]:
        return list(cls.SUPPORTED_PLATFORMS.keys())
    
    @classmethod
    async def query(
        cls,
        platform: str,
        question: str,
        target_brand: str,
        search_mode: str = "standard",
        caller: str = "monitoring",
        brand_id: Optional[int] = None,
        quote_id: Optional[int] = None,
        user_id: Optional[int] = None,
        monitoring_task_id: Optional[int] = None,
        keyword_id: Optional[int] = None,
        keyword: Optional[str] = None,
    ) -> Dict[str, Any]:
        """查询指定平台

        search_mode 仅对 doubao 平台生效：
          - "standard": web_search 普通版（¥0.004/次）
          - "enhanced": doubao_app 增强版（¥0.2/次）
        """
        if platform not in cls.SUPPORTED_PLATFORMS:
            return {
                "status": "error",
                "error": "当前平台暂未接入监测",
                "error_code": "platform_not_supported",
                "is_detected": False
            }

        query_func = cls.SUPPORTED_PLATFORMS[platform]

        try:
            # search_mode 仅传给 doubao
            kwargs = {
                "query": question,
                "check_brand": target_brand,
                "brand_id": brand_id,
            }
            if platform == "doubao":
                kwargs["search_mode"] = search_mode
            if platform == "yuanbao":
                # 元宝走统一观测 SSOT:必须显式声明 monitoring 语境(不得借用付费诊断
                # 的 ingest 豁免)，并给出稳定 round_id / request_id 让单轮上限与
                # 幂等在跨 worker 时正确(observation service 对缺 round_id 是 fail-closed)。
                _round_anchor = str(monitoring_task_id or quote_id or brand_id or "adhoc")
                _request_anchor = hashlib.sha256(
                    f"{_round_anchor}|{keyword_id}|{keyword or question}|yuanbao".encode("utf-8")
                ).hexdigest()[:32]
                kwargs["observation_source_kind"] = "monitoring"
                kwargs["observation_source_ref"] = f"monitoring-task:{_round_anchor}"
                kwargs["observation_round_id"] = f"monitoring:{_round_anchor}"
                kwargs["observation_request_id"] = f"monitoring-yuanbao:{_request_anchor}"
                kwargs["owner_user_id"] = user_id
            with llm_tracking_context(
                caller=caller,
                brand_id=brand_id,
                quote_id=quote_id,
                user_id=user_id,
                metadata={
                    "task_id": monitoring_task_id,
                    "monitoring_task_id": monitoring_task_id,
                    "keyword_id": keyword_id,
                    "keyword": keyword,
                    "monitoring_platform": platform,
                    "search_mode": search_mode,
                    "question": question[:300],
                },
            ):
                result = await query_func(**kwargs)
            
            # 解析结果 - ToolResponse格式: content=[{"type": "text", "text": json_str}]
            is_detected = False
            mention_type = "none"
            response_text = ""
            search_citations_data = []
            mentioned_inline = []  # 竞品共现源:复用 ai_tester 已抽取的 mentioned_brands_inline(不新增 LLM)
            
            # 从ToolResponse提取JSON
            data = {}
            if hasattr(result, 'content') and result.content:
                content_item = result.content[0]
                if isinstance(content_item, dict) and content_item.get('type') == 'text':
                    import json
                    try:
                        data = json.loads(content_item.get('text', '{}'))
                    except:
                        # [#3-C2 P0-2 2026-06-07 资金] 引擎异常返回纯文本 'Error: ...'(非 JSON·dashscope/kimi/deepseek 的 except 分支)
                        #   → 当引擎错误(下方 engine_error 分支转 status=error·不当未检出落库扣费);其余非 JSON 维持空 dict
                        _raw = content_item.get('text', '') or ''
                        if _raw.strip().startswith('Error:'):
                            data = {"engine_error": True, "answer_summary": _raw[:200]}
            elif isinstance(result, dict):
                data = result
            
            # [#3-C2 2026-06-07 资金] 引擎自报错误(如豆包 429/空响应/异常 耗尽重试)→ 标 status=error
            #   不当"未检出"落库污染达标率 · 并经既有 per_platform_fail/release_freeze 通道退费(整片引擎失败时)
            if isinstance(data, dict) and data.get("engine_error"):
                error_code = data.get("error_code")
                if error_code == "brand_identity_data_unavailable":
                    public_error = "品牌资料暂时无法读取"
                elif error_code == "brand_identity_retry_exhausted":
                    public_error = "品牌身份重判仍未完成"
                elif error_code == "brand_identity_unresolved" or data.get("brand_detection_unknown"):
                    error_code = "brand_identity_unresolved"
                    response_text = data.get("response", "") or data.get("text", "")
                    try:
                        (
                            runtime_provider, runtime_model,
                            runtime_surface, actual_search_mode,
                        ) = _resolve_runtime_lineage(
                            platform, data.get("search_mode") or search_mode
                        )
                    except MonitoringLineageUnregistered:
                        logger.error(
                            "[Monitoring] runtime lineage 未登记 platform=%s（矩阵与血缘表漂移）",
                            platform,
                        )
                        return {
                            "status": "error",
                            "error": "该平台本次未返回可用结果",
                            "error_code": "runtime_lineage_unregistered",
                            "is_detected": False,
                        }
                    return {
                        "status": "pending_identity",
                        "error_code": error_code,
                        "is_detected": False,
                        "mention_type": "pending_identity",
                        "response_snippet": response_text[:500],
                        "full_response": response_text,
                        "search_citations": "",
                        "competitors_mentioned": [],
                        "identity_candidates": data.get("identity_candidates") or [],
                        "identity_evidence_snippet": data.get("identity_evidence_snippet") or "",
                        "sent_question_snapshot": question,
                        "target_brand": target_brand,
                        "platform": platform,
                        "provider": runtime_provider,
                        **_lineage_model_fields(data, runtime_model),
                        "model_revision": "unknown",
                        "surface": runtime_surface,
                        "search_mode": actual_search_mode,
                        "response_status": "brand_identity_unresolved",
                        "target_outcome": "entity_ambiguous",
                    }
                else:
                    error_code = "platform_unavailable"
                    public_error = "该平台本次未返回可用结果"
                return {
                    "status": "error",
                    "error": public_error,
                    "error_code": error_code,
                    "is_detected": False,
                    "mention_type": "none",
                    "response_snippet": "",
                    "competitors_mentioned": [],
                }

            if isinstance(data, dict):
                # ai_tester返回的字段是brand_detected，不是is_visible/detected
                is_detected = data.get("brand_detected", False) or data.get("is_visible", False)
                response_text = data.get("response", "") or data.get("text", "")

                # 🔑 DDS: 提取搜索引用数据
                search_citations_data = data.get("search_citations", [])

                # 竞品共现:复用 ai_tester 已抽取的 mentioned_brands_inline(已花的 LLM 钱接回 · 不新增调用)
                mentioned_inline = data.get("mentioned_brands_inline") or []

                # 判断提及类型
                # [P0-3 · 2026-07-26] 落新词表(recommended/mentioned/none/pending_identity)。
                #   旧版一律写 "direct" —— 全库没有任何"推荐"档，报告推荐率恒 0%。
                #   现在读 ai_tester 采集时算好的 target_outcome（统一观测分类器，
                #   零额外 provider 调用），映射成对客户的四档；缺 outcome 时保守
                #   落 mentioned，绝不凭 is_detected 直接升成 recommended。
                if is_detected and mention_type == "none":
                    from services.mention_vocabulary import (
                        mention_type_from_outcome,
                        normalize_mention_type,
                    )

                    if data.get("mention_type"):
                        mention_type = normalize_mention_type(data.get("mention_type"))
                    else:
                        mention_type = mention_type_from_outcome(
                            data.get("target_outcome"), is_detected=True
                        )
            
            # 调试日志
            print(f"[PlatformAdapter] response_text length: {len(response_text)}")
            if search_citations_data:
                print(f"[PlatformAdapter] search_citations: {len(search_citations_data)} 条")
            
            # 序列化 search_citations 为 JSON 字符串
            import json as _json
            search_citations_json = ""
            if search_citations_data:
                try:
                    search_citations_json = _json.dumps(search_citations_data, ensure_ascii=False)
                except:
                    search_citations_json = str(search_citations_data)
            
            # 竞品共现:过滤目标品牌别名 + 去重(只用已抽取数据 · 不新增 LLM 调用)
            competitors_mentioned = _filter_competitor_brands(mentioned_inline, target_brand)

            try:
                (
                    runtime_provider, runtime_model,
                    runtime_surface, actual_search_mode,
                ) = _resolve_runtime_lineage(
                    platform, data.get("search_mode") or search_mode
                )
            except MonitoringLineageUnregistered:
                logger.error(
                    "[Monitoring] runtime lineage 未登记 platform=%s（矩阵与血缘表漂移）",
                    platform,
                )
                return {
                    "status": "error",
                    "error": "该平台本次未返回可用结果",
                    "error_code": "runtime_lineage_unregistered",
                    "is_detected": False,
                }

            return {
                "status": "success",
                "is_detected": is_detected,
                "mention_type": mention_type,
                "response_snippet": response_text[:500] if response_text else "",
                "full_response": response_text,
                "search_citations": search_citations_json,
                "competitors_mentioned": competitors_mentioned,
                # Immutable monitoring lineage captured at the provider boundary.
                "sent_question_snapshot": question,
                "target_brand": target_brand,
                "platform": platform,
                "provider": runtime_provider,
                **_lineage_model_fields(data, runtime_model),
                "model_revision": "unknown",
                "surface": runtime_surface,
                "search_mode": actual_search_mode,
                "response_status": "success",
            }

        except Exception as e:
            logger.exception(
                "Monitoring provider failed platform=%s brand_id=%s keyword_id=%s",
                platform,
                brand_id,
                keyword_id,
            )
            return {
                "status": "error",
                "error": "该平台本次未返回可用结果",
                "error_code": "platform_unavailable",
                "is_detected": False,
                "mention_type": "none",
                "response_snippet": "",
                "competitors_mentioned": []
            }


class MonitoringScheduler:
    """监测调度器 - 高并发执行"""
    
    def __init__(
        self,
        max_concurrency: int = 10,
        requests_per_second: float = 5.0
    ):
        self.max_concurrency = max_concurrency
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.rate_limiter = RateLimiter(requests_per_second)
        self.completed_count = 0
        self.total_count = 0
        self._lock = asyncio.Lock()
    
    async def run_batch(
        self,
        tasks: List[MonitoringTask],
        progress_callback: Callable[[int, int], None] = None,
        monitoring_task_id: Optional[int] = None,
        brand_id: Optional[int] = None,
        quote_id: Optional[int] = None,
        user_id: Optional[int] = None,
        caller: str = "monitoring",
        before_first_provider: Optional[Callable[[], Any]] = None,
    ) -> List[Dict[str, Any]]:
        """并发执行批量监测"""
        self.total_count = len(tasks)
        self.completed_count = 0
        provider_boundary_lock = asyncio.Lock()
        provider_boundary_started = False

        async def ensure_provider_boundary() -> None:
            nonlocal provider_boundary_started
            if provider_boundary_started or before_first_provider is None:
                return
            async with provider_boundary_lock:
                if provider_boundary_started:
                    return
                value = before_first_provider()
                if inspect.isawaitable(value):
                    await value
                provider_boundary_started = True
        
        async def run_single(task: MonitoringTask) -> Dict[str, Any]:
            async with self.semaphore:
                try:
                    await self.rate_limiter.wait()
                    await ensure_provider_boundary()
                    if task.cell_id is not None and task.cell_claim_token:
                        from db.monitoring_db import mark_monitoring_cell_dispatched
                        mark_monitoring_cell_dispatched(
                            cell_id=int(task.cell_id),
                            claim_token=task.cell_claim_token,
                        )
                        task.provider_dispatched = True
                    result = await PlatformAdapter.query(
                        platform=task.platform,
                        question=task.question,
                        target_brand=task.target_brand,
                        search_mode=task.search_mode,
                        caller=caller,
                        brand_id=brand_id,
                        quote_id=quote_id,
                        user_id=user_id,
                        monitoring_task_id=monitoring_task_id,
                        keyword_id=task.keyword_id,
                        keyword=task.keyword,
                    )
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        if task.cell_id is not None and task.cell_claim_token:
                            try:
                                finish_monitoring_cell_error(
                                    cell_id=int(task.cell_id),
                                    claim_token=task.cell_claim_token,
                                    state=(
                                        "pending_provider_confirmation"
                                        if task.provider_dispatched else "failed"
                                    ),
                                    error_code=(
                                        "provider_outcome_unknown"
                                        if task.provider_dispatched
                                        else "worker_lost_before_dispatch"
                                    ),
                                    error_message="监测任务已取消",
                                )
                            except Exception:
                                logger.exception(
                                    "Cancelled monitoring cell settlement failed cell_id=%s",
                                    task.cell_id,
                                )
                        raise
                    result = {
                        "status": "error",
                        "error": "执行中断，已保留单格状态",
                        "error_code": (
                            "provider_outcome_unknown"
                            if task.provider_dispatched
                            else "worker_lost_before_dispatch"
                        ),
                        "is_detected": False,
                    }
                
                result.update({
                    "keyword_id": task.keyword_id,
                    "keyword": task.keyword,
                    "platform": task.platform,
                    "sent_question_snapshot": task.question,
                    "target_brand": task.target_brand,
                    "keyword_source": task.keyword_source,
                    "keyword_type": task.keyword_type,
                    "keyword_resolver_status": task.keyword_resolver_status,
                    "cell_id": task.cell_id,
                    "cell_claim_token": task.cell_claim_token,
                    "provider_dispatched": task.provider_dispatched,
                })
                
                # 更新进度
                async with self._lock:
                    self.completed_count += 1
                    if progress_callback:
                        try:
                            progress_callback(self.completed_count, self.total_count)
                        except:
                            pass
                
                return result
        
        # 并发执行所有任务
        results = await asyncio.gather(*[run_single(task) for task in tasks])
        return results


def build_question(keyword: str) -> str:
    """[SSOT geo-commercial-intent-governance-v1.0 §4.3 · 2026-07-23]

    旧行为(把关键词拼上「哪家好/推荐」后缀合成另一套问题)已废止(归档索引 C):
    监测必须复用客户确认的不可变自然问题,运行时不得重新拼接
    "关键词 + 哪家好"生成另一套问题。

    老数据没有 monitoring_query 时,按客户购买的关键词**原样**发送
    (关键词本身就是客户确认的不可变输入,不改写、不注入意图)。
    保留函数名兼容既有 import 点;单格无有效关键词只影响该格。
    """
    return str(keyword or "").strip()


def resolve_monitoring_query(kw: Dict[str, Any]) -> str:
    """P0.8 · 统一关键词 → 实际发送的 AI query 解析

    优先级(§4.3 复用确认问题 · 禁运行时拼接):
    1. kw['monitoring_query'](DB 持久化 · 客户确认 / 人工 override)
    2. 客户购买的关键词原样(老数据兼容 · 不注入"哪家好"等意图)
    """
    mq = (kw.get("monitoring_query") or "").strip()
    if mq:
        return mq
    fallback_query = build_question(kw.get("keyword", ""))
    # [SSOT §4.3 · CommercialQueryPolicy 统一接线] 消费端不得丢弃/改写已
    # 付费的监测数据;对无确认问题的遗留词仅做 advisory 可见性标注,
    # 供运营复核后在选词/确认层处理(同一引擎,不另立标准)。
    try:
        from services.commercial_query_policy import evaluate as _policy_evaluate
        _decision = _policy_evaluate(fallback_query)
        if fallback_query and not _decision.commercial_delivery_eligible:
            print(
                "[CommercialQueryPolicy][advisory] 遗留监测词未证明商业资格"
                f"(照常监测·不拦截): {fallback_query} · {list(_decision.reason_codes)}"
            )
    except Exception:
        pass
    return fallback_query


async def run_client_monitoring(
    brand_id: int = None,
    client_id: str = None,
    keyword_ids: List[int] = None,
    platforms: List[str] = None,
    concurrency: int = None,
    progress_callback: Callable[[int, int, str], None] = None,
    search_mode: str = "standard",
    keyword_keys: List[str] = None,   # [2026-06-07 老板复审 fix2] 选词范围(confirmed-123/extra-456)· 必须与 /run freeze 口径一致·否则冻结部分词却跑全量
    charge: bool = True,              # [2026-06-07 老板复审 fix1] /run 路径在 server.py 已 freeze/commit/release monitor_single·须传 charge=False 防与本函数 scheduled_monitoring 双扣;scheduler/自动监测保持 True
    organization_identity=None,
    task_created_callback: Optional[Callable[[int], Any]] = None,
    before_first_provider: Optional[Callable[[], Any]] = None,
    settlement_reference: Optional[str] = None,
    initial_fulfillment_state: str = "reserved",
) -> Dict[str, Any]:
    """
    执行客户词条批量监测
    
    Args:
        brand_id: 品牌ID（优先使用）
        client_id: 客户ID（@deprecated: 兼容期保留）
        keyword_ids: @deprecated 2026-06-07 · 已不参与过滤/记录(任务记录恒按实跑词集)· 仅签名兼容保留·勿依赖
        platforms: 平台列表（None则使用配置默认）
        concurrency: 并发数（None则使用配置默认）
        progress_callback: 进度回调 (completed, total, status)
    
    Returns:
        监测结果汇总
    """
    start_time = time.time()
    
    # 获取配置
    config = get_monitoring_config(brand_id=brand_id, client_id=client_id or "_global_")
    if concurrency is None:
        concurrency = config.get("default_concurrency", 10)
    platforms_str = config.get("default_platforms", DEFAULT_MONITORING_PLATFORMS)
    
    # 获取词条 - 使用统一适配层
    # 优先用 brand_id 查 quote_ids，fallback 用 client_id 当 quote_id
    if brand_id is not None:
        from db.monitoring_db import _resolve_id, resolve_service_anchored_quote_ids_for_brand, brand_has_confirmed_or_paid_quote
        _, _ = _resolve_id(brand_id=brand_id)  # 验证 brand_id 有效
        # [2026-06-07 老板复审] 统一走服务锚 SSOT helper · 不再自写 `SELECT id FROM quotes WHERE brand_id`(漏服务锚过滤)。
        quote_ids = resolve_service_anchored_quote_ids_for_brand(brand_id)
        if quote_ids:
            # [2026-06-07 老板复审 fix2] 贯穿 keyword_keys → 实跑词范围 == /run freeze 词范围
            keywords = get_keywords_for_monitoring(quote_ids=quote_ids, keyword_keys=keyword_keys)
        elif brand_has_confirmed_or_paid_quote(brand_id):
            # 有 quote 但无有效服务锚(脏草稿 / paid 已过期)→ 不监测·不进引擎(禁 fallback 到 extra_keywords)
            keywords = []
        else:
            # 无任何 quote → fallback brand 级 extra_keywords(代理自助监测订阅·合法)
            keywords = get_keywords_for_monitoring(brand_id=brand_id, keyword_keys=keyword_keys)
    elif client_id is not None:
        quote_id = int(client_id) if isinstance(client_id, str) else client_id
        keywords = get_keywords_for_monitoring(quote_id=quote_id, keyword_keys=keyword_keys)
    else:
        keywords = []
    
    if not keywords:
        return {
            "status": "error",
            "error": "没有可监测的词条",
            "task_id": None
        }

    platform_plan = PlatformAdapter.resolve_keyword_monitoring_plan(
        keywords,
        requested_platforms=platforms,
        configured_platforms=platforms_str,
    )
    if platform_plan["rejected"]:
        return {
            "status": "error",
            "error": "请求包含当前套餐未购买或不可用的监测引擎",
            "error_code": "requested_platform_not_entitled",
            "task_id": None,
        }
    keywords = platform_plan["keywords"]
    platforms = platform_plan["platforms"]
    if not keywords:
        return {
            "status": "error",
            "error": "所选词条没有可执行的已购监测引擎",
            "error_code": "no_eligible_monitoring_platforms",
            "task_id": None,
        }
    # Cell ledger requires the canonical tenant key even for the legacy client_id path.
    from db.monitoring_db import _resolve_id as _resolve_monitoring_brand
    resolved_plan_brand_id, _ = _resolve_monitoring_brand(brand_id=brand_id, client_id=client_id)
    if resolved_plan_brand_id is None:
        return {
            "status": "error",
            "error": "无法确认监测任务所属客户",
            "error_code": "monitoring_brand_scope_missing",
            "task_id": None,
        }
    brand_id = int(resolved_plan_brand_id)
    import uuid as _uuid
    task_ref = str(settlement_reference or f"batch_mon_{_uuid.uuid4().hex[:12]}")[:160]
    planned_test_count = sum(
        len(keyword["_eligible_monitoring_platforms"])
        for keyword in keywords
    )
    
    # 创建任务记录（双写 brand_id）
    # [CTO-15.23 2026-05-07 P0-2] 显式标 trigger_type='auto_publish'
    # · run_client_monitoring 被 _m1a_auto_monitor_after_publish 调用(发布 24h 后自动监测)
    # · 也可能被其他后台调用 · 加 trigger_type 防 default 漏归类
    task_id = create_monitoring_task(
        brand_id=brand_id,
        client_id=client_id,
        keyword_ids=[k["id"] for k in keywords],  # [#1 2026-06-07] 废弃 keyword_ids 真值开关 · 任务记录恒按实跑词集(keyword_count 准确)
        concurrency=concurrency,
        trigger_type="auto_publish",
        planned_test_count=planned_test_count,
        planned_platform_count=len(platforms),
    )
    if task_created_callback is not None:
        callback_value = task_created_callback(int(task_id))
        if inspect.isawaitable(callback_value):
            await callback_value
    import uuid as _uuid_plan
    from db.monitoring_db import create_monitoring_run_cells
    durable_cells = create_monitoring_run_cells(
        task_id=task_id,
        brand_id=brand_id,
        keywords=keywords,
        search_mode=search_mode,
        fulfillment_credential=str(_uuid_plan.uuid4()),
        fulfillment_state=initial_fulfillment_state,
        retry_coverage={
            "policy_version": "monitoring-retry-v1",
            "coverage": "included",
            "max_attempts": 1,
        },
        settlement_reference=task_ref,
    )
    durable_cell_lookup = {
        (cell["keyword_source"], int(cell["keyword_id"]), cell["platform"]): cell
        for cell in durable_cells if cell["is_planned"]
    }

    # [CTO-15.23 2026-05-08 P0-1 B1] 后台自动监测加 freeze/commit/release(scheduled_monitoring 价 38/词)
    # · 此前 batch_monitor.run_client_monitoring 0 freeze · _m1a_auto_monitor_after_publish 触发时 0 扣费
    # · 当前 user_wallets.auto_monitor_after_publish 默认 FALSE · 0 真实流量 · 但代理勾选时漏扣
    # · user_id 从 brand.owner_user_id 拿(后台无 http_request)
    # · 失败时标 task=failed · 不跑监测(防积分不足强跑亏钱)
    owner_user_id = None
    owner_lookup_error = None
    freeze_id = None
    execution_error = None
    if brand_id:
        try:
            from db.connection import get_connection as _gc_owner
            _conn_o = _gc_owner()
            try:
                _cur_o = _conn_o.cursor()
                _cur_o.execute("SELECT owner_user_id FROM brands WHERE id = %s", (brand_id,))
                _row_o = _cur_o.fetchone()
                owner_user_id = _row_o.get("owner_user_id") if _row_o else None
            finally:
                try:
                    _conn_o.close()
                except Exception:
                    pass
        except Exception as owner_error:
            owner_lookup_error = owner_error

    if charge and (owner_lookup_error is not None or not owner_user_id):
        # Background billing has no authenticated request principal to fall
        # back to. If the immutable owner cannot be proved, calling a provider
        # would be a free-delivery fail-open.
        from db.monitoring_db import set_monitoring_task_fulfillment_state
        set_monitoring_task_fulfillment_state(task_id, "released")
        update_task_status(task_id, "failed")
        return {
            "status": "error",
            "error": "无法确认监测计费主体，本次未调用平台",
            "error_code": "monitoring_billing_owner_unavailable",
            "task_id": task_id,
        }

    if charge and owner_user_id:
        # [2026-06-07 老板复审 fix1] charge=False(/run 路径)→ 不在此 freeze(server.py 已 freeze monitor_single)·防双扣。
        #   commit/release 都 gate 在 freeze_id·此处不 freeze 则 freeze_id 留 None·下游自动跳过结算。
        try:
            from middleware.billing import freeze_points
            n_kw = len(keywords)
            extra_cost = 38 * (n_kw - 1)  # scheduled_monitoring base 38/词 · 跟 scheduler 对齐
            r = await freeze_points(
                user_id=owner_user_id,
                feature_code="scheduled_monitoring",
                task_ref=task_ref,
                brand_id=brand_id,
                extra_cost=extra_cost,
                reason=f"自动监测(发布 24h 后)· {n_kw} 词 · {planned_test_count} 次平台检测",
            )
            freeze_id = r.get("freeze_id")
            print(f"[batch_monitor-Billing] freeze ok user={owner_user_id} freeze_id={freeze_id} amount={r.get('amount')} N={n_kw}")
        except Exception as fex:
            # 余额不足 / 计费异常 · 标 task failed · 不跑监测
            print(f"[batch_monitor-Billing] freeze 失败 · 不跑监测 user={owner_user_id} brand={brand_id}: {fex}")
            from db.monitoring_db import set_monitoring_task_fulfillment_state
            set_monitoring_task_fulfillment_state(task_id, "released")
            update_task_status(task_id, "failed")
            return {
                "status": "error",
                "error": f"计费失败: {str(fex)[:200]}",
                "task_id": task_id,
            }

    freeze_settled = False

    def _durable_delivery_started() -> bool:
        from db.connection import get_connection as _get_delivery_connection
        delivery_conn = _get_delivery_connection()
        try:
            delivery_cur = delivery_conn.cursor()
            delivery_cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM public.monitoring_run_cells
                     WHERE task_id=%s AND provider_dispatched_at IS NOT NULL
                ) AS started
                """,
                (int(task_id),),
            )
            row = delivery_cur.fetchone()
            return bool(row and row["started"])
        finally:
            delivery_conn.close()

    async def _settle_owned_freeze(reason: str) -> None:
        """Settle this function's freeze from the durable dispatch fence."""
        nonlocal freeze_settled
        if freeze_id is None or freeze_settled:
            return
        try:
            delivery_started = _durable_delivery_started()
        except Exception as delivery_error:
            # An unreadable dispatch fence is never proof that release is safe.
            delivery_started = True
            print(f"[batch_monitor-Billing] dispatch fence unreadable: {delivery_error}")
        try:
            from db.monitoring_db import set_monitoring_task_fulfillment_state
            if delivery_started:
                from middleware.billing import commit_freeze
                await commit_freeze(
                    task_ref=task_ref,
                    reason=f"批量监测已发送平台请求: {reason[:100]}",
                    user_id=owner_user_id,
                )
                set_monitoring_task_fulfillment_state(task_id, "covered")
            else:
                from middleware.billing import release_freeze
                await release_freeze(
                    task_ref=task_ref,
                    reason=f"批量监测发送前终止: {reason[:100]}",
                    user_id=owner_user_id,
                )
                set_monitoring_task_fulfillment_state(task_id, "released")
            freeze_settled = True
        except Exception as settlement_error:
            # A commit/release acknowledgement loss is not retried through a
            # different operation. Keep the original freeze for reconciliation.
            freeze_settled = True
            try:
                from db.monitoring_db import set_monitoring_task_fulfillment_state
                set_monitoring_task_fulfillment_state(task_id, "coverage_unknown")
            except Exception:
                pass
            print(
                f"[batch_monitor-Billing] settlement unknown freeze_id={freeze_id}: "
                f"{settlement_error}"
            )

    # 更新任务状态为运行中
    try:
        update_task_status(task_id, "running")
    except BaseException as status_error:
        await _settle_owned_freeze(str(status_error) or "task start state failed")
        raise

    # 构建所有监测任务
    monitoring_tasks = []
    for kw in keywords:
        # P0.8 · 优先用 monitoring_query 真实 query · fallback build_question
        question = resolve_monitoring_query(kw)
        for platform in kw["_eligible_monitoring_platforms"]:
            cell = durable_cell_lookup[(kw.get("source") or "unknown", int(kw["id"]), platform)]
            monitoring_tasks.append(MonitoringTask(
                keyword_id=kw["id"],
                keyword=kw["keyword"],
                target_brand=kw["target_brand"],
                platform=platform,
                question=question,
                search_mode=search_mode,
                keyword_source=kw.get("source") or "unknown",
                cell_id=int(cell["id"]),
            ))

    if progress_callback:
        try:
            progress_callback(0, len(monitoring_tasks), "正在准备监测任务...")
        except Exception as progress_error:
            print(f"[batch_monitor] start callback failed: {progress_error}")
    
    # 创建调度器并执行
    scheduler = MonitoringScheduler(
        max_concurrency=concurrency,
        requests_per_second=5.0
    )
    
    def internal_progress(completed, total):
        if progress_callback:
            progress_callback(completed, total, f"检测中 {completed}/{total}")

    # [CTO-15.23 2026-05-08 P0-1 B1] 主跑包 try · 失败 release_freeze + 标 task failed
    try:
        from db.monitoring_db import claim_monitoring_run_cell
        for monitoring_task in monitoring_tasks:
            claim = claim_monitoring_run_cell(
                cell_id=int(monitoring_task.cell_id),
                task_id=task_id,
                brand_id=brand_id,
                allowed_state="queued",
                lease_seconds=300,
            )
            monitoring_task.cell_claim_token = str(claim["claim_token"])
        quote_id_context = None
        try:
            if client_id is not None:
                quote_id_context = int(client_id)
            elif "quote_ids" in locals() and len(quote_ids) == 1:
                quote_id_context = int(quote_ids[0])
        except Exception:
            quote_id_context = None

        results = await scheduler.run_batch(
            monitoring_tasks,
            internal_progress,
            monitoring_task_id=task_id,
            brand_id=brand_id,
            quote_id=quote_id_context,
            user_id=owner_user_id,
            caller="monitoring",
            before_first_provider=before_first_provider,
        )
    except BaseException as run_err:
        execution_error = run_err
        try:
            update_task_status(task_id, "failed")
        except Exception:
            pass
        await _settle_owned_freeze(str(run_err) or "monitoring cancelled")
        raise

    # Each provider outcome now settles only its own durable cell. Successful and
    # pending-identity facts are immutable; failed cells retain the original
    # fulfillment credential for a no-new-charge manual retry.
    error_results = [r for r in results if r.get("status") == "error"]
    persisted_results = []
    for result in results:
        if result.get("status") == "error":
            error_code = str(result.get("error_code") or "platform_unavailable")
            cell_state = (
                "pending_provider_confirmation"
                if error_code in {"platform_unavailable", "provider_outcome_unknown"}
                else ("unavailable" if error_code == "platform_not_supported" else "failed")
            )
            try:
                finish_monitoring_cell_error(
                    cell_id=int(result["cell_id"]),
                    claim_token=str(result["cell_claim_token"]),
                    state=cell_state,
                    error_code=error_code,
                    error_message=str(result.get("error") or "该平台本次未返回可用结果"),
                )
            except BaseException as persist_error:
                await _settle_owned_freeze(str(persist_error) or "cell error persistence failed")
                raise
            result["cell_state"] = cell_state
            continue
        try:
            result_id = save_monitoring_result(
                task_id=task_id,
                keyword_id=int(result["keyword_id"]),
                keyword=result["keyword"],
                platform=result["platform"],
                is_detected=bool(result.get("is_detected")),
                mention_type=result.get("mention_type") or "none",
                response_snippet=result.get("response_snippet") or "",
                full_response=result.get("full_response") or "",
                search_citations=result.get("search_citations") or "",
                competitors_mentioned=result.get("competitors_mentioned") or [],
                lineage=result,
                identity_brand_id=brand_id,
                identity_candidates=result.get("identity_candidates") or [],
                identity_evidence_snippet=result.get("identity_evidence_snippet") or "",
                identity_review_state=(
                    "pending" if result.get("status") == "pending_identity" else "not_required"
                ),
                cell_id=int(result["cell_id"]),
                cell_claim_token=str(result["cell_claim_token"]),
            )
        except Exception as save_error:
            cell_state = (
                "pending_provider_confirmation"
                if result.get("provider_dispatched")
                else "failed"
            )
            error_code = (
                "provider_outcome_unknown"
                if result.get("provider_dispatched")
                else "worker_lost_before_dispatch"
            )
            try:
                finish_monitoring_cell_error(
                    cell_id=int(result["cell_id"]),
                    claim_token=str(result["cell_claim_token"]),
                    state=cell_state,
                    error_code=error_code,
                    error_message=str(save_error)[:2000],
                )
            except BaseException as persist_error:
                await _settle_owned_freeze(str(persist_error) or "cell recovery persistence failed")
                raise
            result.update({
                "status": "error",
                "cell_state": cell_state,
                "error_code": error_code,
                "error": "结果持久化未确认，已禁止自动再次请求",
            })
            error_results.append(result)
            continue
        result["result_id"] = result_id
        persisted_results.append(result)

    from services.monitoring_identity_review import is_pending_identity_result

    attempted_tests = len(results)
    eligible_results = [r for r in persisted_results if not is_pending_identity_result(r)]
    total_tests = len(eligible_results)
    pending_identity_count = sum(1 for r in persisted_results if is_pending_identity_result(r))

    # M1a T4 埋点 · stage=monitor event=complete(一次批量跑完打一次)
    if brand_id:
        try:
            from db.pipeline_stage_log_db import log_stage_event
            log_stage_event(
                brand_id=brand_id,
                stage_name="monitor",
                event="complete",
                meta={
                    "task_id": task_id,
                    "keyword_count": len(keywords),
                    "platform_count": len(platforms),
                    "attempted_tests": attempted_tests,
                    "total_tests": total_tests,
                    "pending_identity_count": pending_identity_count,
                    "detected_count": sum(
                        1 for r in eligible_results if r.get("is_detected")
                    ),
                    "search_mode": search_mode,
                    "source": "batch_monitor",
                },
            )
        except Exception:
            pass
    
    # 平台市占率权重（QuestMobile 2025.12 MAU）
    from db.monitoring_db import PLATFORM_WEIGHTS

    # 统计结果（简单计数 + 加权）
    detected_count = sum(1 for r in eligible_results if r.get("is_detected"))

    # 加权出现率
    weighted_detected = sum(
        PLATFORM_WEIGHTS.get(r["platform"], 0.25)
        for r in eligible_results if r.get("is_detected")
    )
    weighted_total = sum(PLATFORM_WEIGHTS.get(r["platform"], 0.25) for r in eligible_results)
    detection_rate = round(weighted_detected / weighted_total * 100, 1) if weighted_total > 0 else 0

    # 按词条统计（加权）
    keyword_stats = {}
    for r in eligible_results:
        kw = r["keyword"]
        w = PLATFORM_WEIGHTS.get(r["platform"], 0.25)
        if kw not in keyword_stats:
            keyword_stats[kw] = {"tests": 0, "detected": 0, "w_total": 0.0, "w_detected": 0.0}
        keyword_stats[kw]["tests"] += 1
        keyword_stats[kw]["w_total"] += w
        if r.get("is_detected"):
            keyword_stats[kw]["detected"] += 1
            keyword_stats[kw]["w_detected"] += w

    # 按平台统计
    platform_stats = {}
    for r in eligible_results:
        p = r["platform"]
        if p not in platform_stats:
            platform_stats[p] = {"tests": 0, "detected": 0, "weight": PLATFORM_WEIGHTS.get(p, 0.25)}
        platform_stats[p]["tests"] += 1
        if r.get("is_detected"):
            platform_stats[p]["detected"] += 1

    # 计算各自命中率
    for kw in keyword_stats:
        s = keyword_stats[kw]
        s["rate"] = round(s["w_detected"] / s["w_total"] * 100, 1) if s["w_total"] > 0 else 0

    for p in platform_stats:
        s = platform_stats[p]
        s["rate"] = round(s["detected"] / s["tests"] * 100, 1) if s["tests"] > 0 else 0
    
    elapsed = round(time.time() - start_time, 1)
    
    summary = {
        "task_id": task_id,
        "brand_id": brand_id,
        "client_id": client_id,  # @deprecated: 兼容期保留
        "total_keywords": len(keywords),
        "total_platforms": len(platforms),
        "attempted_tests": attempted_tests,
        "total_tests": total_tests,
        "error_count": len(error_results),
        "detected_count": detected_count,
        "pending_identity_count": pending_identity_count,
        "detection_rate": detection_rate,
        "keyword_stats": keyword_stats,
        "platform_stats": platform_stats,
        "concurrency": concurrency,
        "elapsed_seconds": elapsed,
        "completed_at": datetime.now().isoformat()
    }
    
    # 更新任务状态
    try:
        update_task_status(task_id, "completed", total_tests + pending_identity_count, summary)
    except BaseException as completion_error:
        await _settle_owned_freeze(str(completion_error) or "task completion state failed")
        raise
    
    # 保存趋势统计（支持"变化"列显示）
    today = datetime.now().strftime("%Y-%m-%d")
    for kw_name, stats in keyword_stats.items():
        # 获取关键词ID
        kw_obj = next((k for k in keywords if k.get('keyword') == kw_name), None)
        if kw_obj:
            # 计算变化率（与上次比较）
            kw_source = (
                'confirmed'
                if kw_obj.get('source') in {'confirmed', 'contract'} else 'extra'
            )
            try:
                prev_trend = get_keyword_trend(
                    kw_obj.get('id', 0), keyword_source=kw_source, limit=1
                )
                prev_rate = prev_trend[0]['detection_rate'] if prev_trend else 0
                current_rate = stats['rate']
                rate_change = round(current_rate - prev_rate, 1)
                save_trend_stat(
                    keyword_id=kw_obj.get('id', 0),
                    keyword_source=(
                        'confirmed'
                        if kw_obj.get('source') in {'confirmed', 'contract'} else 'extra'
                    ),
                    period_type='daily',
                    period_date=today,
                    test_count=stats['tests'],
                    detected_count=stats['detected'],
                    rate_change=rate_change
                )
            except Exception as e:
                print(f"[趋势统计] 保存失败 {kw_name}: {e}")
    
    if progress_callback:
        try:
            progress_callback(attempted_tests, attempted_tests, "监测完成")
        except Exception as progress_error:
            print(f"[batch_monitor] completion callback failed: {progress_error}")

    # [CTO-15.23 2026-05-08 P0-1 B1] 监测成功 · commit_freeze 转正式扣费
    await _settle_owned_freeze(f"task_id={task_id} completed")

    return {
        "status": "success",
        **summary
    }


# 快速测试
if __name__ == "__main__":
    import asyncio
    
    async def test():
        # 需要先添加测试词条
        from db.monitoring_db import init_monitoring_tables, add_keyword
        
        init_monitoring_tables()
        
        # 添加测试词条（使用 brand_id）
        add_keyword(brand_id=1, keyword="GEO优化", target_brand="全域上榜", difficulty="中等", target_rate=60)
        add_keyword(brand_id=1, keyword="AI搜索优化", target_brand="全域上榜", difficulty="中等", target_rate=60)
        
        # 执行监测（使用 brand_id）
        result = await run_client_monitoring(
            brand_id=1,
            platforms=["dashscope"],  # 测试只用一个平台
            concurrency=2
        )
        
        print(f"监测结果: {result}")
    
    asyncio.run(test())
