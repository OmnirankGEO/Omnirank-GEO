"""
sentiment_classifier · 品牌舆情分析(4 引擎并发 + LLM 分类)

CTO-15.23 2026-05-09 老板拍板方案:
  · 复用现有 4 引擎诊断 pipeline(query_xxx_search · 已有 retry/API key/搜索)
  · 加 1 个固定 sentiment query 模拟客户决策视角
  · 新售 4 引擎并发拿 raw response(通义千问 / DeepSeek / 豆包 / 元宝)
  · deepseek-v4 跑 1 次分类 · 输出 JSON sentiment + summary + alert
  · 0 新外部 API(不调 5118/百度/小红书)· 0 新扣费(融进诊断 · 多 4 LLM call ~¥0.05)

老板原话:"复用老数据,增加新效果,降本增效了属于是"

输出结构(写入 modules_jsonb.sentiment · 仅代理端 internal 视角渲染 · 客户决策页不显示):
{
    "query": "...",
    "engines": [
        {"engine": "dashscope", "sentiment": "positive|neutral|negative",
         "summary": "20-40 字摘要", "key_concerns": [...], "raw_response": "..."},
        ...
    ],
    "consensus": "positive|neutral|negative|mixed",
    "alert": True/False,
    "alert_reason": "...",
    "engine_count": 4,
}
"""
import asyncio
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import json
import os
from typing import Any

import httpx

from config.model_config import DEEPSEEK_CONFIG
from tools.ai_visibility.ai_tester import (
    query_dashscope_search,
    query_deepseek,
    query_doubao_search,
    query_kimi_search,
    query_yuanbao,
)

SENTIMENT_QUERY_TEMPLATE = (
    "如果我要选{industry}的服务商,{brand}值得选吗?"
    "口碑如何?有什么差评或负面新闻?用户的真实评价怎么样?"
)

DEFAULT_ENGINES = ["dashscope", "deepseek", "doubao", "yuanbao"]

# [GEO-R10-CAN-005] 单引擎回复保留窗口 · 分类直接吃整段(不再二次截到 1500 漏尾部负面)
_RAW_CAP = 2500


def _coverage_sample(text: str, budget: int) -> tuple[str, bool]:
    """[GEO-R10-CAN-005] 覆盖式采样 · 防负面证据落在回复尾部却被首部截断丢弃

    单引擎回复常把差评/负面新闻/争议放在后段;直接 text[:budget] 会漏掉这些晚出现的
    风险信号。改为 head(60%) + tail(40%) 覆盖采样 —— 首尾都进分类 bundle,兼顾开头
    的推荐语与结尾的负面点。返回 (采样文本, 是否发生截断)。
    """
    if not text:
        return "", False
    if len(text) <= budget:
        return text, False
    head_len = (budget * 3) // 5   # 60% 头部
    tail_len = budget - head_len   # 40% 尾部(负面新闻/差评常在此段)
    head = text[:head_len]
    tail = text[-tail_len:]
    return f"{head}\n…[中间省略 {len(text) - budget} 字]…\n{tail}", True


def build_sentiment_query(brand_name: str, industry: str = "") -> str:
    """生成固定 sentiment query · 模拟客户决策视角"""
    industry_label = industry.strip() if industry else "这个行业"
    return SENTIMENT_QUERY_TEMPLATE.format(brand=brand_name, industry=industry_label)


async def _query_engine_for_sentiment(
    engine: str,
    query: str,
    brand_name: str,
    *,
    observation_source_ref: str | None = None,
    owner_user_id: int | None = None,
    brand_id: int | None = None,
    industry: str = "",
) -> dict[str, Any]:
    """单引擎查 sentiment query · 复用 ai_tester.query_xxx_search · 拿 raw response

    返回 {engine, raw_response, truncated, error}
    """
    try:
        if engine == "dashscope":
            resp = await query_dashscope_search(query, check_brand=brand_name)
        elif engine == "kimi":
            resp = await query_kimi_search(query, check_brand=brand_name)
        elif engine == "doubao":
            resp = await query_doubao_search(query, check_brand=brand_name)
        elif engine == "deepseek":
            resp = await query_deepseek(query, check_brand=brand_name)
        elif engine == "yuanbao":
            resp = await query_yuanbao(
                query,
                check_brand=brand_name,
                brand_id=brand_id,
                observation_source_ref=observation_source_ref,
                observation_request_id=(
                    f"diagnosis-yuanbao-sentiment:{observation_source_ref}"
                    if observation_source_ref
                    else None
                ),
                owner_user_id=owner_user_id,
                industry=industry,
            )
        else:
            return {"engine": engine, "raw_response": "", "truncated": False, "error": f"unknown engine {engine}"}

        text = resp.content[0]["text"] if resp.content else ""
        if text.startswith("Error"):
            return {"engine": engine, "raw_response": "", "truncated": False, "error": text[:200]}

        # query_xxx_search 返回 visibility_result JSON · 含 response 字段(完整 ai 原始回复)
        try:
            data = json.loads(text)
            raw = data.get("response", "") if isinstance(data, dict) else ""
            raw = raw or ""
        except (json.JSONDecodeError, TypeError):
            raw = text
        # [GEO-R10-CAN-005] 超窗口时首尾覆盖采样(负面新闻常在尾部)+ 标记截断,
        # 而非裸 [:2500] 直接丢弃后半段
        sampled, truncated = _coverage_sample(raw, _RAW_CAP)
        return {"engine": engine, "raw_response": sampled, "truncated": truncated, "error": None}
    except Exception as e:
        return {"engine": engine, "raw_response": "", "truncated": False, "error": str(e)[:200]}


async def _classify_via_llm(
    brand_name: str, industry: str, engine_responses: list[dict]
) -> dict[str, Any]:
    """1 次 deepseek-chat 把 4 引擎回复合并打成 sentiment 分类 + 摘要"""
    api_key = DEEPSEEK_CONFIG.get("api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
    base_url = DEEPSEEK_CONFIG.get("base_url", "https://api.deepseek.com")
    if not api_key:
        return {"_error": "no DEEPSEEK_API_KEY"}

    parts = []
    truncated_any = False
    for r in engine_responses:
        eng = r.get("engine", "?")
        if r.get("error"):
            parts.append(f"## 引擎: {eng} (查询失败)\n错误: {r.get('error', '')}\n")
        else:
            # [GEO-R10-CAN-005] 直接吃整个保留窗口(已在查询阶段首尾覆盖采样),
            # 不再二次截到 1500 —— 否则 char 1500 之后的负面证据永远进不了分类器
            truncated_any = truncated_any or bool(r.get("truncated"))
            parts.append(f"## 引擎: {eng}\n{r.get('raw_response', '')}\n")
    bundle = "\n".join(parts) or "(所有引擎查询失败)"

    industry_label = industry.strip() if industry else "(未指定行业)"
    prompt = f"""你是企业口碑分析师。分析下面 4 大主流 AI 引擎对品牌「{brand_name}」(行业:{industry_label})的回复,判断每个引擎呈现的舆情倾向。

输入(4 引擎对客户决策视角 query 的回复):

{bundle}

要求严格输出 JSON(不要任何额外说明文字):

{{
  "engines": [
    {{
      "engine": "dashscope|deepseek|doubao|yuanbao",
      "sentiment": "positive|neutral|negative",
      "summary": "20-40 字摘要 · 概括该引擎对该品牌的核心评价",
      "key_concerns": ["关键负面点或风险点 1", "关键负面点或风险点 2"]
    }},
    ...
  ],
  "consensus": "positive|neutral|negative|mixed",
  "alert": true/false,
  "alert_reason": "如 alert=true · 一句话说明哪些引擎给出负面 + 关键负面点 / 如 alert=false · 留空字符串"
}}

判定标准(严):
- positive: 主动推荐 / 长处突出 / 0 负面提及
- neutral: 中立介绍 / 优缺点平衡 / 无明确推荐 / 完全没听说过该品牌(不推荐也不反对)
- negative: 提到差评/投诉/负面新闻/明确不推荐/争议事件 任一即 negative
- consensus: 看 4 引擎结果分布:全 positive=positive / 全 neg=neg / 全 neu=neu / 混合=mixed
- alert: 任一 engine == "negative" 必须 alert=true(老板拍板"任一负面就警示" · 防漏掉低概率风险)
- key_concerns: 仅在 negative 引擎给出 1-3 条具体负面点 · positive/neutral 引擎留空数组 []
"""
    try:
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=90) as client:
            async with llm_track(
                "brand_sentiment_classifier",
                "deepseek",
                model=DEEPSEEK_OFFICIAL_FLASH,
            ) as tracker:
                response = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": DEEPSEEK_OFFICIAL_FLASH,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1500,
                        "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                    },
                )
                if response.status_code == 200:
                    data_for_usage = response.json()
                    input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data_for_usage)
                    tracker.record(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_tokens=cached_tokens,
                        success=True,
                    )
                else:
                    tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
            if response.status_code != 200:
                return {"_error": f"HTTP {response.status_code} - {response.text[:200]}"}
            content = response.json()["choices"][0]["message"]["content"]
            result = json.loads(content)
            if isinstance(result, dict):
                # [GEO-R10-CAN-005] 暴露截断标志 · 供 renderer 提示"分类基于采样(可能不完整)"
                result["_truncated"] = truncated_any
            return result
    except json.JSONDecodeError as e:
        return {"_error": f"json decode: {str(e)[:200]}"}
    except Exception as e:
        return {"_error": str(e)[:200]}


async def analyze_brand_sentiment(
    brand_name: str,
    industry: str = "",
    engines: list[str] | None = None,
    *,
    observation_source_ref: str | None = None,
    owner_user_id: int | None = None,
    brand_id: int | None = None,
) -> dict[str, Any]:
    """品牌舆情分析主入口 · 4 引擎并发 + LLM 分类

    Args:
        brand_name: 品牌名(必填)
        industry: 行业(可选 · 用于 query 行业上下文)
        engines: 引擎列表 · 默认 4 引擎全跑

    Returns:
        sentiment 结构(写入 modules_jsonb.sentiment)· 失败时仍返回结构含 error 字段
    """
    if not brand_name:
        return {
            "error": "brand_name required",
            "query": "",
            "engines": [],
            # [GEO-R10-CAN-006] 缺输入 = 未分析,用 'unknown' 区别于真实"中性"共识
            "consensus": "unknown",
            "alert": False,
            "engine_count": 0,
        }

    eng_list = engines if engines is not None else list(DEFAULT_ENGINES)
    query = build_sentiment_query(brand_name, industry)

    print(f"  🔍 [sentiment] 4 引擎并发查询舆情: {brand_name}")
    engine_responses = await asyncio.gather(
        *[
            _query_engine_for_sentiment(
                eng,
                query,
                brand_name,
                observation_source_ref=observation_source_ref,
                owner_user_id=owner_user_id,
                brand_id=brand_id,
                industry=industry,
            )
            for eng in eng_list
        ],
        return_exceptions=False,
    )

    valid_count = sum(1 for r in engine_responses if not r.get("error"))
    if valid_count == 0:
        # 全失败 · 不阻断诊断 · 返回 fallback
        print("  ⚠️ [sentiment] 4 引擎全失败 · 跳过 sentiment")
        return {
            "error": "all engines failed",
            "query": query,
            "engines": [
                {
                    "engine": r["engine"],
                    "sentiment": "neutral",
                    "summary": f"({r.get('error', '失败')})",
                    "key_concerns": [],
                    "raw_response": "",
                }
                for r in engine_responses
            ],
            # [GEO-R10-CAN-006] 4 引擎全失败 = 无法分析,'unknown' 而非伪装成"中性"
            "consensus": "unknown",
            "alert": False,
            "engine_count": 0,
        }

    print(f"  🧠 [sentiment] LLM 分类 4 引擎回复 → sentiment + alert")
    cls = await _classify_via_llm(brand_name, industry, engine_responses)
    if cls.get("_error"):
        print(f"  ⚠️ [sentiment] LLM 分类失败: {cls['_error']}")
        return {
            "error": cls["_error"],
            "query": query,
            "engines": [
                {
                    "engine": r["engine"],
                    "sentiment": "neutral",
                    "summary": (r.get("raw_response", "")[:60] or "(无)"),
                    "key_concerns": [],
                    "raw_response": r.get("raw_response", ""),
                }
                for r in engine_responses
            ],
            # [GEO-R10-CAN-006] LLM 分类失败 = 未得出结论,'unknown' 而非伪装成"中性"
            "consensus": "unknown",
            "alert": False,
            "engine_count": valid_count,
        }

    # 合 raw_response 进 cls.engines(给 renderer 折叠展示原文)
    # [GEO-R10-CAN-005] 展示保留窗口整段(_RAW_CAP),与分类器实际所见一致,不再截到 1500
    raw_map = {r["engine"]: r.get("raw_response", "") for r in engine_responses}
    for e in cls.get("engines", []):
        e["raw_response"] = (raw_map.get(e.get("engine"), "") or "")[:_RAW_CAP]

    cls["query"] = query
    cls["engine_count"] = valid_count
    # [GEO-R10-CAN-005] 把内部 _truncated 归一成对外 truncated 字段供 renderer 提示
    cls["truncated"] = bool(cls.pop("_truncated", False))
    if cls.get("alert"):
        print(
            f"  🚨 [sentiment] alert=true · {cls.get('alert_reason', '')[:80]}"
        )
    else:
        print(f"  ✅ [sentiment] consensus={cls.get('consensus', '?')} · 0 alert")
    return cls
