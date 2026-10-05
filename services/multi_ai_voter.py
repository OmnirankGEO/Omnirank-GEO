"""
Multi-AI Voter — 多 AI 投票审核去噪器（v3.6 CTO-15.2 2026-04-19）

用于 industry_knowledge_collector.py 的 L1/L2 raw 素材审核。

核心机制:
  - 多 AI 并发独立审核同一条 raw content
  - 投票阈值 ≥ threshold 通过, 否则拒绝(可能是营销话术 / 跑题 / 通用黑话)
  - 取通过 AI 的 items 并集 → 下游 max 整合时去重

实测对比（2026-04-19 prototype）:
  - v2.1（单 flash 精洗）: 通用黑话 11/76 / 跑题 3/28
  - v3 多 AI 投票（去 kimi 后）: 通用黑话 0 / 跑题 0
  - 成本对比: kimi 输出价 ¥0.060/K 是元凶,去掉后 -31% 成本无质量损失

合规约束:
  - 不能用 Coding Plan API key（仅限 IDE 编程工具,不能用于业务后端）
  - 必须用按 token 付费的正式 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY
"""

import asyncio
import json
import logging
import os
from typing import Any, Optional

import httpx

from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH


logger = logging.getLogger("MultiAIVoter")


# ========== Provider 配置 ==========
# 去 kimi(输出价 ¥0.060/K 是元凶,占 v3 LLM 成本 67% 但贡献的"独到洞察"在 max 整合时被去重)
PROVIDERS = {
    # [CTO-15.5 2026-04-20] 实测后回滚 qwen3.6-max-preview → qwen3-max
    # 原因: preview 默认开思维链,输出膨胀 30x,deep_analyze 整体成本从 ¥2/次 涨到 ¥6+/次(毛利掉)
    # qwen3.6-max-preview 定义保留在 config/model_config.py 作为未来 opt-in
    "qwen3-max": {  # key 保留(按 key 选择不破)· 实际 model 升级旗舰
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "env_key": "DASHSCOPE_API_KEY",
        "model": "qwen3.7-max",  # [2026-06-06 老板] 内部 deep_analyze 升级 qwen3-max→qwen3.7-max
        "input_yuan_per_k": 0.006,   # 限时 5 折 ¥6/M
        "output_yuan_per_k": 0.018,  # 限时 5 折 ¥18/M
    },
    # 键 "deepseek-chat" 是 call_ai() 的**注册表键**(调用方按它取档),不跟着改;
    # 改的是这一档发出去的 model。
    # 🔴 [WO_206 c1b② · Owner 2026-09-14「全部改成 deepseek-flash」· 仅 GEO 干活线]
    #    Deploy 206-d2 实打:官方 /models 只剩 deepseek-flash 与 deepseek-v4-pro;
    #    deepseek-chat 仍返 200 但**回显 deepseek-flash**,且它已从官方定价页消失 ——
    #    未文档化的别名随时可能变 400。改成常量:行为与今天一致,还免掉那个风险。
    #    🔴 下面那条 deepseek-v4-pro 是 2026-05-11 Social-CTO 老板拍板刻意选的,
    #       官方仍在供应 ⇒ **一个字不动**(Review 09-14 §9.1 排除清单)。
    "deepseek-chat": {
        "url": "https://api.deepseek.com/v1/chat/completions",
        "env_key": "DEEPSEEK_API_KEY",
        "model": DEEPSEEK_OFFICIAL_FLASH,
        "input_yuan_per_k": 0.0014,
        "output_yuan_per_k": 0.0028,
    },
    # 2026-05-11 [Social-CTO-13.0 老板拍板] 深度行业调研 L3 蒸馏切 deepseek-v4-pro
    # · 走 DeepSeek 官方 API(老板订正 2026-05-11:"用官方的 V4-PRO")
    #   api.deepseek.com/v1 · api_key=DEEPSEEK_API_KEY · 不绕 DashScope 中转
    # · thinking 模式参数格式对齐 advisors/base_advisor.py:422:
    #   extra_body={"thinking": {"type": "enabled"}}
    # · 价格估算(待 Deploy-CTO 实测校正)
    "deepseek-v4-pro": {
        "url": "https://api.deepseek.com/v1/chat/completions",
        "env_key": "DEEPSEEK_API_KEY",
        "model": "deepseek-v4-pro",
        "input_yuan_per_k": 0.004,
        "output_yuan_per_k": 0.016,
    },
    "deepseek-v4-flash": {
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "env_key": "DASHSCOPE_API_KEY",
        "model": "deepseek-v4-flash",
        "input_yuan_per_k": 0.0015,
        "output_yuan_per_k": 0.006,
    },
}


def _parse_json_safe(text: str) -> Optional[dict]:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text.strip())
    except Exception:
        try:
            from json_repair import repair_json
            return json.loads(repair_json(text.strip()))
        except Exception:
            return None


async def call_ai(
    provider_name: str, prompt: str, temperature: float = 0.3, max_tokens: int = 2000, timeout: float = 120.0,
    extra_body: Optional[dict] = None,
) -> Optional[str]:
    """单 AI 调用（OpenAI compatible），失败返 None

    extra_body: 额外 body 字段(透传到 HTTP request body)· 用于 thinking 等非标准参数
                deepseek-v4-pro thinking 模式:extra_body={"enable_thinking": True}
                · 走 DashScope compatible-mode · 长 timeout 建议 ≥180s(thinking 思考耗时)
    """
    if provider_name not in PROVIDERS:
        return None
    p = PROVIDERS[provider_name]
    api_key = os.environ.get(p["env_key"])
    if not api_key:
        logger.warning(f"[{provider_name}] env key {p['env_key']} 未配置")
        return None
    body: dict = {
        "model": p["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extra_body:
        body.update(extra_body)
    try:
        from tools.llm_call_tracker import infer_platform_from_url, llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with llm_track(
                "multi_ai_voter",
                infer_platform_from_url(p["url"]),
                model=p["model"],
                metadata={"provider_name": provider_name},
            ) as tracker:
                r = await client.post(
                    p["url"],
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=body,
                )
                if r.status_code == 200:
                    payload = r.json()
                    input_tokens, output_tokens, cached_tokens = usage_from_response_payload(payload)
                    tracker.record(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_tokens=cached_tokens,
                        success=True,
                    )
                else:
                    tracker.record(success=False, error_msg=f"HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code != 200:
                logger.warning(f"[{provider_name}] HTTP {r.status_code}: {r.text[:120]}")
                return None
            return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning(f"[{provider_name}] EXCEPTION: {str(e)[:120]}")
        return None


async def multi_ai_review(
    prompt: str,
    providers: Optional[list[str]] = None,
    threshold: int = 1,
    items_key: str = "items",
    relevance_key: str = "is_relevant_industry",
) -> dict:
    """
    多 AI 投票审核

    Args:
        prompt: 已格式化好的完整 prompt(必须让 AI 输出 {relevance_key, items} JSON)
        providers: AI 列表,默认全用 PROVIDERS
        threshold: 通过的最少 AI 数(2 AI 默认 1, 3 AI 默认 2)
        items_key: AI 输出 JSON 里 items 的字段名
        relevance_key: AI 输出 JSON 里 is_relevant 的字段名

    Returns:
        {
            "passed": bool,           # 是否通过 ≥ threshold
            "vote_pass_count": int,   # 通过 AI 数
            "vote_total": int,        # 实际响应 AI 数
            "items": [...],           # 通过 AI 们 items 的并集
            "verdicts": [             # 详细投票
                {"ai": "qwen3.6-max-preview", "is_relevant": True, "reasoning": "...", "items": [...]},
                ...
            ]
        }
    """
    providers = providers or list(PROVIDERS.keys())
    ai_tasks = [call_ai(p, prompt) for p in providers]
    responses = await asyncio.gather(*ai_tasks, return_exceptions=True)

    verdicts = []
    for ai_name, resp in zip(providers, responses):
        if isinstance(resp, Exception) or not resp:
            continue
        parsed = _parse_json_safe(resp)
        if not parsed:
            continue
        is_relevant = bool(parsed.get(relevance_key, False))
        verdicts.append({
            "ai": ai_name,
            "is_relevant": is_relevant,
            "reasoning": parsed.get("reasoning", "")[:80],
            "items": parsed.get(items_key, []) if is_relevant else [],
        })

    pass_count = sum(1 for v in verdicts if v["is_relevant"])
    passed = pass_count >= threshold

    items_union = []
    if passed:
        for v in verdicts:
            if v["is_relevant"]:
                for item in v["items"]:
                    if isinstance(item, dict):
                        item["__voted_by"] = v["ai"]
                items_union.extend(v["items"])

    return {
        "passed": passed,
        "vote_pass_count": pass_count,
        "vote_total": len(verdicts),
        "items": items_union,
        "verdicts": verdicts,
    }


async def review_field_value(
    industry: str,
    field_name: str,
    new_items: list,
    existing_items: Optional[list] = None,
    category: Optional[str] = None,
    source: str = "ai_rerun",       # 'ai_rerun' | 'user_correct'
    providers: Optional[list[str]] = None,
    threshold: int = 1,
) -> dict:
    """
    按"字段值"投票审核 — v3.8 反哺专用（CTO-13.3 · PLAN Q15 避坑 #2）

    和 `multi_ai_review` 差异：
      - multi_ai_review 输入是"网页 raw content"，让 AI 从中提取 items
      - review_field_value 输入是"已有 items 候选"，让 AI 逐项判断 accept/reject

    判定维度（3 条）：
      1. 与行业相关性（不相关 → reject）
      2. 与 existing_items 是否冲突/矛盾（信息冲突 → reject）
      3. 内容质量（空泛 / 纯通用废话 / 低于 3 字 / 纯标点 → reject）

    Returns:
        {
          "passed": bool,                          # final_verdict == 'pass' 或 'partial'
          "final_verdict": "pass|partial|reject",
          "vote_pass_count": int,                  # 响应 AI 中至少接受 1 条 item 的数
          "vote_total": int,
          "accepted_items": [item, ...],           # 通过阈值的 item 并集
          "rejected_items": [{"item": ..., "reasons": [...]}],
          "verdicts": [                            # 每个 AI 的细节
            {"ai": "qwen3.6-max-preview", "accepted_idx": [0,2], "rejected": [{"idx":1, "reason":"通用废话"}]},
            ...
          ]
        }
    """
    if not new_items:
        return {
            "passed": False, "final_verdict": "reject",
            "vote_pass_count": 0, "vote_total": 0,
            "accepted_items": [], "rejected_items": [], "verdicts": [],
        }

    providers = providers or list(PROVIDERS.keys())
    # 压缩 items 给 AI 看（太长会爆 token）
    def _compact(it, max_len=160):
        if isinstance(it, str):
            return it[:max_len]
        try:
            s = json.dumps(it, ensure_ascii=False)[:max_len]
            return s
        except Exception:
            return str(it)[:max_len]
    items_lines = "\n".join(f"#{i}: {_compact(it)}" for i, it in enumerate(new_items))
    existing_sample = ""
    if existing_items:
        existing_sample = "\n".join(f"- {_compact(it, 120)}" for it in existing_items[:8])

    source_label = "AI 重跑生成" if source == "ai_rerun" else "用户手动矫正"
    prompt = f"""你是「{industry}」行业{('/' + category) if category else ''}知识库审核员。

任务: 审核 {source_label}的「{field_name}」字段候选 items, 逐条判断 accept / reject.

铁律 (任一不满足 → reject):
1. ✅ 必须与「{industry}」{('/' + category) if category else ''}行业紧密相关
2. ✅ 不能是空泛通用废话 ("赋能"/"抓手"/"生态"/"协同"这类)
3. ✅ 长度 ≥ 4 字 (非纯标点 / 纯数字)
4. ✅ 不与现有库存冲突 (现有: {'无' if not existing_sample else '见下'})

现有库存 (参考, 不要让候选和这些冲突/重复):
{existing_sample if existing_sample else '(空)'}

候选 items (逐条判断):
{items_lines}

只输出 JSON, 不要 markdown:
{{
  "accepted_idx": [通过的 # 序号, ...],
  "rejected": [{{"idx": 序号, "reason": "20字内原因"}}, ...]
}}"""

    ai_tasks = [call_ai(p, prompt, temperature=0.2, max_tokens=2000) for p in providers]
    responses = await asyncio.gather(*ai_tasks, return_exceptions=True)

    verdicts = []
    for ai_name, resp in zip(providers, responses):
        if isinstance(resp, Exception) or not resp:
            continue
        parsed = _parse_json_safe(resp)
        if not parsed:
            continue
        accepted_idx = parsed.get("accepted_idx") or []
        rejected = parsed.get("rejected") or []
        # 过滤非法 idx
        accepted_idx = [i for i in accepted_idx if isinstance(i, int) and 0 <= i < len(new_items)]
        verdicts.append({
            "ai": ai_name,
            "accepted_idx": accepted_idx,
            "rejected": rejected,
        })

    # 聚合: item 按 "至少 threshold 个 AI 接受" 才通过
    from collections import Counter
    vote_counter: Counter = Counter()
    reject_reasons: dict[int, list] = {}
    for v in verdicts:
        for idx in v["accepted_idx"]:
            vote_counter[idx] += 1
        for rej in v["rejected"]:
            idx = rej.get("idx")
            if isinstance(idx, int) and 0 <= idx < len(new_items):
                reject_reasons.setdefault(idx, []).append(f"{v['ai']}: {rej.get('reason', '')[:40]}")

    accepted_items = []
    rejected_items = []
    for idx, item in enumerate(new_items):
        if vote_counter.get(idx, 0) >= threshold:
            accepted_items.append(item)
        else:
            rejected_items.append({
                "item": item,
                "reasons": reject_reasons.get(idx, ["未被任一 AI 接受"]),
            })

    pass_count = sum(1 for v in verdicts if v["accepted_idx"])
    if accepted_items and rejected_items:
        final = "partial"
    elif accepted_items:
        final = "pass"
    else:
        final = "reject"

    return {
        "passed": bool(accepted_items),
        "final_verdict": final,
        "vote_pass_count": pass_count,
        "vote_total": len(verdicts),
        "accepted_items": accepted_items,
        "rejected_items": rejected_items,
        "verdicts": verdicts,
    }


async def multi_ai_direct(
    prompt: str,
    providers: Optional[list[str]] = None,
    items_key: str = "items",
) -> dict:
    """
    多 AI 直接答(用于 jargon / counter_synth 不依赖 metaso 的素材)

    Returns:
        {
            "by_ai": {"qwen3.6-max-preview": [...], "deepseek-chat": [...]},
            "all_items": [...],     # 各 AI 答案合并(每条标 __source_ai)
            "ai_count": int,
        }
    """
    providers = providers or list(PROVIDERS.keys())
    ai_tasks = [call_ai(p, prompt, temperature=0.5, max_tokens=3000) for p in providers]
    responses = await asyncio.gather(*ai_tasks, return_exceptions=True)

    by_ai = {}
    all_items = []
    for ai_name, resp in zip(providers, responses):
        if isinstance(resp, Exception) or not resp:
            continue
        parsed = _parse_json_safe(resp)
        if not parsed:
            continue
        items = parsed.get(items_key, [])
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    item["__source_ai"] = ai_name
                    all_items.append(item)
            by_ai[ai_name] = items

    return {
        "by_ai": by_ai,
        "all_items": all_items,
        "ai_count": len(by_ai),
    }


# ========== 通用 prompt 模板 ==========

def review_prompt_template(
    industry: str, title: str, content: str, task_desc: str, item_schema: str
) -> str:
    """统一审核 prompt 模板（让 AI 判定 is_relevant_industry + 提取 items）"""
    return f"""你是「{industry}」行业素材采集员。

任务: {task_desc}

铁律:
- ✅ 必须与「{industry}」行业相关
- ❌ 其他行业的(其他领域/通用商业道理) → 严格 skip
- ❌ 营销话术 / 软文推广 → skip
- 不相关时 items 必须为空数组

只输出 JSON,不要 markdown 包裹:
{{"is_relevant_industry": true/false, "reasoning": "20字内", "items": {item_schema}}}

网页标题: {title}
网页正文:
{content}"""
