"""
4 平台 AI 抓取器

豆包 / DeepSeek: 共用火山方舟 Responses API 协议
  - https://ark.cn-beijing.volces.com/api/v3/responses
  - tools: [{"type": "web_search"}]
  - 返回 output[].content[].annotations[].url 作为引用

Qwen: DashScope 原生 generation API + enable_search (A.3.2 实现)
Kimi: DashScope OpenAI 兼容 + builtin_function $web_search (A.3.3 实现)

A.3 review fix(2026-05-05):
- C1 Kimi 5 轮 tool_call 跑完仍空 → raise RuntimeError 让上层重试
- C2 Kimi role=tool 消息 name 字段保留(ai_tester.py 256-258 线上验证 DashScope 兼容)
- C3 火山方舟 web_search 失败用 VolcSearchFailureError 保留 raw data
- I1 Qwen rank 改用 len(citations)+1(dashscope index 可重复/乱序)
- I2 Qwen model 从 SystemSettings.research_monitor_tasks 读
- I4 query_with_retry 加 KeyError/IndexError 到重试白名单
- I5 KIMI_REF_REGEX 用行级 multiline 匹配,兼容 title 含括号
"""
import os
import re
import json
import asyncio
from typing import Any, Dict, List
from config.deepseek_models import (
    DEEPSEEK_OFFICIAL_FLASH,
    assert_official_echo,
    normalize_deepseek_model,
)

import httpx
from tools.llm_call_tracker import llm_track, usage_from_response_payload


ANSWER_CITATION_RE = re.compile(r"(?<![A-Za-z0-9_])[\[【](\d{1,3})[\]】]")

# Phase 9 (2026-05-25) · 豆包模型升级:
# doubao_app + ai_search (原生联网 ~11 引用 · 按次 ¥0.2) 替代原 web_search 工具
# 底座对齐豆包 App 「联网搜索」档 = Seed-2.0-Lite
#
# Phase 9 (2026-05-26) · 模型名集中到 geo_research_config:
#   优先级 env var > geo_research_config > 硬编码默认值
#   _load_model_from_config 每次调用现读 DB · 管理员改完无需重启
#   读取失败 fallback 默认值 + 告警 · 不阻塞业务


def _load_model_from_config(key: str, default: str) -> str:
    """从 geo_research_config 读模型名字符串 · 失败 fallback 默认值。

    value_json 期望存 JSON string (例如 '"qwen-plus-latest"') · psycopg2 取出后是 Python str。
    若 value_json 存的不是字符串 (旧数据/管理员误改),告警后 fallback 默认值。
    """
    try:
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT value_json FROM geo_research_config WHERE key = %s", (key,))
            row = cur.fetchone()
            if row:
                val = row['value_json']
                if isinstance(val, str) and val.strip():
                    return val.strip()
                import logging
                logging.getLogger("GEO-ResearchMonitor").warning(
                    f"[Phase 9] geo_research_config[{key}] 不是字符串 (值={val!r}), fallback {default!r}"
                )
        finally:
            conn.close()
    except Exception as e:
        import logging
        logging.getLogger("GEO-ResearchMonitor").warning(
            f"[Phase 9] geo_research_config[{key}] 读取失败 fallback {default!r}: {e}"
        )
    return default


def _get_doubao_app_model() -> str:
    """豆包 doubao_app 模型名 · env DOUBAO_APP_MODEL > geo_research_config.model_doubao_app > 硬编码"""
    env = os.getenv('DOUBAO_APP_MODEL', '').strip()
    if env:
        return env
    return _load_model_from_config('model_doubao_app', 'doubao-seed-2-0-lite-260215')


# 向后兼容: 老代码可能 import 这个常量 · 保留为模块导入时的快照 (硬编码 fallback)
# 实际请求请用 _get_doubao_app_model() 拿运行时值
DOUBAO_APP_MODEL = os.getenv('DOUBAO_APP_MODEL', 'doubao-seed-2-0-lite-260215').strip()


# ==================== 火山方舟共用 ====================

DEFAULT_TIMEOUT = 240.0

# 12 种已知 web_search 降级文案,出现这些就视为失败重试
SEARCH_FAILURE_PHRASES = (
    "搜索工具暂时出现故障",
    "搜索工具暂时不可用",
    "搜索功能暂时不可用",
    "搜索工具暂不可用",
    "无法访问搜索",
    "无法连接搜索",
    "搜索接口暂时无法",
    "联网功能暂不可用",
    "联网搜索暂时",
    "搜索服务暂时",
    "工具调用失败",
    "搜索失败",
)


class VolcSearchFailureError(RuntimeError):
    """
    火山方舟 web_search 触发降级文案的失败。

    继承 RuntimeError 让 query_with_retry 自动重试,
    并保留 raw_data + answer_preview 给运营排查具体哪条 prompt 触发了什么文案。
    """
    def __init__(self, message: str, raw_data: dict, answer: str):
        super().__init__(message)
        self.raw_data = raw_data
        self.answer_preview = (answer or "")[:200]


def is_search_failure(answer: str, citations: List[dict]) -> bool:
    """
    判断 AI 是否给出了 web_search 降级文案。
    有引用 = 正常,没引用且文案命中黑名单 = 失败。
    [GEO-R2-CAN-029] 采纳成功契约:无引用且答案为空 = 失败(而非静默当成功)。
      修前:空答案 + 无引用 return False → 适配器 ok=True → 上层塞占位 citation 并记 status='success',
      让空/无证据的 provider 输出被当成"成功研究证据"计入完成度/排名。现改为判失败 → 触发重试/失败。
    """
    if citations:
        return False
    if not answer:
        return True
    return any(p in answer for p in SEARCH_FAILURE_PHRASES)


def _canonical_url(value: str) -> str:
    return (value or "").strip().rstrip("/")


def _answer_citation_indices(answer: str) -> set[int]:
    indices: set[int] = set()
    for match in ANSWER_CITATION_RE.finditer(answer or ""):
        try:
            idx = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if idx > 0:
            indices.add(idx)
    return indices


def _annotation_urls(value: Any) -> set[str]:
    urls: set[str] = set()
    if isinstance(value, dict):
        annotations = value.get("annotations")
        if isinstance(annotations, list):
            for ann in annotations:
                if isinstance(ann, dict):
                    url = ann.get("url") or ann.get("uri") or ann.get("source_url")
                    if url:
                        urls.add(_canonical_url(str(url)))
        for child in value.values():
            urls.update(_annotation_urls(child))
    elif isinstance(value, list):
        for child in value:
            urls.update(_annotation_urls(child))
    return urls


def mark_answer_cited_sources(answer: str, citations: List[dict], raw: Any = None) -> List[dict]:
    """Mark sources that the answer explicitly adopted via [n] or native annotations.

    Search result lists are useful exposure signals, but they are not answer
    citations by themselves.  This helper only marks sources when the model
    provides a clear in-answer marker or a native annotation URL.
    """
    cited_indices = _answer_citation_indices(answer)
    cited_urls = _annotation_urls(raw)
    marked: List[dict] = []
    for pos, citation in enumerate(citations or [], start=1):
        item = dict(citation)
        rank = int(item.get("rank") or pos)
        rank_candidates: List[int] = []
        raw_rank_candidates = item.get("answer_ranks")
        if isinstance(raw_rank_candidates, (list, tuple, set)):
            for value in raw_rank_candidates:
                try:
                    candidate = int(value)
                except (TypeError, ValueError):
                    continue
                if candidate > 0 and candidate not in rank_candidates:
                    rank_candidates.append(candidate)
        if rank not in rank_candidates:
            rank_candidates.insert(0, rank)
        url = _canonical_url(str(item.get("url") or ""))
        matched_rank = next((candidate for candidate in rank_candidates if candidate in cited_indices), None)
        is_answer_cited = matched_rank is not None or (url and url in cited_urls)
        item["is_answer_cited"] = bool(is_answer_cited)
        item["adoption_rank"] = matched_rank if matched_rank is not None else (rank if is_answer_cited else None)
        marked.append(item)
    return marked


# Phase 9 (2026-05-25) · _query_volc_responses (老通用火山 web_search) 已物理删
# 原因: 豆包改 doubao_app + ai_search · DeepSeek 改阿里百炼 v4-flash · 不再共用此 helper
# VolcSearchFailureError / is_search_failure / SEARCH_FAILURE_PHRASES 三个工具保留 (query_doubao 仍用)


# [2026-07-16 采纳口径] 豆包内联标注指令(与 2026-07-16 实测逐字一致,两题 2/2 服从):
#   要求正文按「模型所见搜索结果的原始序号」内联 [n]。编号权威=原始流序(含重复,不去重),
#   实测 p1 12/12 + p2 14/14 清单条目与 raw 原始序对齐;去重压缩 rank 会错 11/12、9/14 条。
DOUBAO_INLINE_MARKER_SUFFIX = (
    "\n\n(输出格式要求:回答正文中凡引用了某个搜索结果的信息,请在该处内联标注 [n],"
    "n 为该来源在你本次搜索结果中的序号;回答末尾再列出编号与来源标题的对应清单。不要省略标注。)"
)


async def query_doubao(prompt_id: int, prompt: str) -> Dict[str, Any]:
    """豆包助手 API · Phase 9 升级 [2026-05-25]

    改用 doubao_app + ai_search (原生联网),对齐豆包 App「联网搜索」档。
    单次搜十几条引用,远多于通用 web_search 插件。
    按次计费 ~¥0.2 (不计 token);每次仅能启用一个功能,故只开 ai_search。

    底座模型 doubao-seed-2-0-lite-260215。
    优先级: env DOUBAO_APP_MODEL > geo_research_config.model_doubao_app > 硬编码默认值
    """
    api_key = _research_volc_key()  # 走 os.getenv('VOLC_API_KEY') · UI 改 settings.json 也同步
    if not api_key:
        raise RuntimeError("VOLC_API_KEY 未设置")

    model = _get_doubao_app_model()
    url = "https://ark.cn-beijing.volces.com/api/v3/responses"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "ark-beta-doubao-app": "true",  # Beta 标记 · doubao_app API 必须
    }
    payload = {
        "model": model,
        "input": prompt + DOUBAO_INLINE_MARKER_SUFFIX,  # [2026-07-16 采纳口径] 见常量注释
        "stream": False,
        "tools": [{
            "type": "doubao_app",
            "feature": {
                "ai_search": {"type": "enabled"},
                "chat": {"type": "disabled"},
                "deep_chat": {"type": "disabled"},
                "reasoning_search": {"type": "disabled"},
            },
        }],
    }

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        limits=httpx.Limits(max_connections=10),
    ) as client:
        async with llm_track(
            "research_monitor",
            "doubao",
            model=model,
            # Phase 9: metadata 加 provider 字段 + doubao_ai_search_call_count
            # tracker line 278 看到 doubao_ai_search_call_count 会按 ¥0.2/次 flat 算成本
            metadata={
                "prompt_id": prompt_id,
                "api": "doubao_app",
                "feature": "ai_search",
                "provider": "volc_ark",
                "doubao_ai_search_call_count": 1,  # 触发 tracker ¥0.2/次 flat rate
            },
        ) as tracker:
            resp = await client.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
            if resp.status_code >= 400:
                tracker.record(success=False, error_msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            data = resp.json()
            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
            tracker.record(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                success=True,
            )

    answer_parts: List[str] = []
    citations: List[dict] = []
    seen_urls: Dict[str, dict] = {}
    source_pos = 0
    # 豆包助手返回 output[].type == "doubao_app_call",正文与引用都在 blocks 里
    # [2026-07-16 采纳口径] rank/answer_ranks 用【原始流序号】(每条 search result 递增,含重复不压缩):
    #   豆包正文 [n] 按它所见搜索结果的原始顺序编号(2026-07-16 两题实测 26 条清单全对齐);
    #   旧「去重后 len(citations)+1」rank 在有重复 URL 时整体偏移 → 实测会把 11/12、9/14 条
    #   采纳记错来源。与 deepseek/qwen 的 answer_ranks 多候选机制同款,mark_answer_cited_sources 原样消费。
    for item in data.get("output", []) or []:
        if item.get("type") != "doubao_app_call":
            continue
        for b in item.get("blocks", []) or []:
            bt = b.get("type")
            if bt == "output_text":
                answer_parts.append(b.get("text", "") or "")
            elif bt == "search":
                for r in b.get("results", []) or []:
                    source_pos += 1  # 与模型所见编号空间一致:每条结果都占号,url 缺失也不跳号
                    card = r.get("text_card") or {}
                    if isinstance(card, str):  # 兜底:偶发字符串化的 dict
                        try:
                            card = json.loads(card)
                        except json.JSONDecodeError:
                            continue
                    u = card.get("url")
                    if not u:
                        continue
                    existing = seen_urls.get(u)
                    if existing:
                        existing.setdefault("answer_ranks", []).append(source_pos)
                        continue
                    citation = {
                        "url": u,
                        "title": card.get("title", "") or "",
                        "rank": source_pos,
                        "answer_ranks": [source_pos],
                    }
                    seen_urls[u] = citation
                    citations.append(citation)

    answer = "".join(answer_parts)

    if is_search_failure(answer, citations):
        raise VolcSearchFailureError(
            f"doubao_app ai_search 失败(HTTP 200 但降级文案): {answer[:80]}",
            raw_data=data,
            answer=answer,
        )

    # [口径修复 2026-07-03] 豆包答案采纳标记:与 deepseek/qwen 一致调用 mark_answer_cited_sources。
    #   仅在有明确证据时标 is_answer_cited(答案正文 [n]/【n】 标记命中引用序位 或 native annotation URL),
    #   普通 search 结果的 cite_url 不会被粗暴升级为采纳 —— 无明确证据的行保持 search_result_only。
    #   修前:query_doubao 从不调用此函数 → 豆包行 is_answer_cited 恒 False,拉低全站采纳率。
    citations = mark_answer_cited_sources(answer, citations, raw=data)

    return {
        "platform": "doubao",
        "provider": "volc_ark",
        "model": model,
        "model_revision": "unknown",
        "surface": "ai_search",
        "search_mode": "doubao_app_ai_search",
        "prompt_id": prompt_id,
        "prompt": prompt,
        "answer": answer,
        "citations": citations,
        "ok": True,
        "error": None,
        "raw": data,
    }


def _research_dashscope_key() -> str:
    """调研监测 dashscope key · P14-v14 撤回 P14-v12 调研专用设计
    跟系统其他模块共用 DASHSCOPE_API_KEY (settings.json/UI 改 → load_settings sync env)
    """
    return os.getenv('DASHSCOPE_API_KEY', '').strip()


def _research_volc_key() -> str:
    """调研监测 火山 key (豆包用) · 跟系统其他模块共用 VOLC_API_KEY"""
    return (
        os.getenv('VOLC_API_KEY', '').strip()
        or os.getenv('DOUBAO_SEED_API_KEY', '').strip()
        or os.getenv('DOUBAO_API_KEY', '').strip()
    )


class NotOfficialDeepSeekError(RuntimeError):
    """响应形状不是 DeepSeek 官方(Anthropic)协议 —— 说明通道被改回去了,fail-loud。"""


DEEPSEEK_OFFICIAL_URL = "https://api.deepseek.com/anthropic/v1/messages"
DEEPSEEK_OFFICIAL_WEB_SEARCH_TOOL = {
    "type": "web_search_20250305", "name": "web_search", "max_uses": 5,
}
# [复检 R2 返修] 砍到只剩检索指令(理由见 ai_tester 同名常量注释:
#   原文的"市场调研 / 有哪些服务商或品牌 / 说明来源"三处会诱导 GEO 的被测指标本身)。
DEEPSEEK_OFFICIAL_FORCE_SEARCH_SYSTEM = (
    "回答前必须先用 web_search 工具检索最新公开信息,再基于检索结果作答。"
)

# [复检 R2 返修 · 采纳率归零的真修复]
#   飞轮 `is_answer_cited` 只认两个输入:raw 里的原生 annotations、正文里的 [n] 角标。
#   旧百炼路径靠 `search_options.enable_citation` 给原生 annotations,近 30 天采纳率 69.4%
#   (四家最高);官方 Anthropic 响应**两个都没有** → 换完采纳率会**静默归零**
#   (不报错不告警,飞轮核心指标直接塌)。这正是 2026-07-16「kimi/doubao 采纳恒 0」的重演。
#   修法照搬当时实测服从的 DOUBAO_INLINE_MARKER_SUFFIX:按**本次搜索结果原始序号**内联 [n]。
#   我方 rank 同样按原始流序分配(跨轮累加、含重复不去重),两边编号口径一致。
DEEPSEEK_INLINE_MARKER_SUFFIX = (
    "\n\n(输出格式要求:回答正文中凡引用了某个搜索结果的信息,请在该处内联标注 [n],"
    "n 为该来源在你本次搜索结果中的序号;回答末尾再列出编号与来源标题的对应清单。不要省略标注。)"
)

DEEPSEEK_FW_ZERO_SEARCH_RETRIES_DEFAULT = 3


def _fallback_enabled() -> bool:
    """零检索兜底总开关(与监测/诊断侧同名 env 共用)。默认开。"""
    import os as _os

    return _os.getenv("DEEPSEEK_ZERO_SEARCH_FALLBACK", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }


async def _deepseek_dashscope_fallback_for_research(
    prompt_id: int, prompt: str
) -> "Dict[str, Any] | None":
    """飞轮零检索兜底:走百炼拿数据,但**血缘如实落 dashscope**。

    与监测/诊断侧同一条铁律 —— 兜底不是静默替换:`provider` / `search_mode` 标成兜底态,
    `citations` 逐条打 `via`,下游要排除兜底数据时有据可依。
    兜底自身失败返 None,调用方保留官方那次的诚实零检索结果,不伪造。
    """
    try:
        fb = await _query_deepseek_via_dashscope(prompt_id, prompt)
    except Exception as exc:
        import logging
        logging.getLogger("GEO-ResearchMonitor").warning(
            "[飞轮] DeepSeek 零检索兜底失败: %s", str(exc)[:200])
        return None
    if not isinstance(fb, dict) or not fb.get("ok"):
        return None
    fb["provider"] = "dashscope"
    fb["search_mode"] = "dashscope_fallback"
    fb["fallback_used"] = True
    fb["fallback_reason"] = "official_zero_search_after_retries"
    for c in fb.get("citations") or []:
        if isinstance(c, dict):
            c["via"] = "dashscope_fallback"
    import logging
    logging.getLogger("GEO-ResearchMonitor").info(
        "[飞轮] DeepSeek 零检索 → 百炼兜底(已标 provider=dashscope)")
    return fb


def _assert_official_deepseek_shape(data: Dict[str, Any]) -> None:
    """[Owner 2026-07-27 "一定要验证是真的官方通道"] 运行时自检。

    端点写对了不等于打到了官方 —— 万一有人改回百炼、或中间加了代理,
    只看 URL 是看不出来的。这里按**响应形状**验:官方走 Anthropic Messages 协议,
    `type=='message'` 且 `content` 是块数组;百炼那条返回的是 `{"output": {...}}`,
    形状完全不同,一验即露。不符合就 fail-loud,不静默接受。
    """
    if not isinstance(data, dict):
        raise NotOfficialDeepSeekError("响应非 dict")
    if data.get("type") != "message" or not isinstance(data.get("content"), list):
        raise NotOfficialDeepSeekError(
            f"响应不是 Anthropic Messages 形状(type={data.get('type')!r},"
            f"keys={sorted(data.keys())[:6]})—— 通道可能已被改回非官方"
        )


async def query_deepseek(
    prompt_id: int, prompt: str, *, _retry_budget: int | None = None
) -> Dict[str, Any]:
    """DeepSeek · **官方原生检索** [2026-07-27 换通道]

    改前走阿里百炼 + `enable_search` —— 那测的是**阿里的检索行为**,不是 DeepSeek 的。
    生产实证(近 30 天 138 对配对):**91 对(66%)引用字节级与通义完全相同**,
    而正文 219 对无一相同 —— 两个不同模型生成的答案,检索结果却大面积同源,
    这就是"检索层被换掉"的直接证据。飞轮的语料血缘因此一直记的是阿里的口径。

    现改为官方 `api.deepseek.com/anthropic`(Anthropic Messages 协议):
      · 鉴权 `x-api-key` + `anthropic-version`,不是 Bearer;
      · `tools=[web_search_20250305]`,检索由 DeepSeek 服务端执行(我们不自建检索层);
      · 官方是**模型自主决定**是否搜(不像百炼有 forced_search),故零检索时软重试,
        重试用尽再降级百炼兜底 —— 与监测/诊断侧同一套口径。
      · 每次响应都过 `_assert_official_deepseek_shape` 自检,确认真的打在官方通道上。
    """
    import os as _os

    if _retry_budget is None:
        try:
            _retry_budget = max(0, int(_os.getenv(
                "DEEPSEEK_ZERO_SEARCH_RETRIES", str(DEEPSEEK_FW_ZERO_SEARCH_RETRIES_DEFAULT))))
        except (TypeError, ValueError):
            _retry_budget = DEEPSEEK_FW_ZERO_SEARCH_RETRIES_DEFAULT

    api_key = (_os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        # fail-closed:绝不静默回落百炼 —— 那正是"以为在测 DeepSeek 实际测阿里"的成因。
        raise RuntimeError("DEEPSEEK_API_KEY 未设置(官方通道 fail-closed,不回落百炼)")

    #: 🔴 [WO_221-c1'] 官方页 `deepseek-v4-flash` **已退役**。两条端点的行为**不一样**,
    #:   这是 2026-09-15 两边各打一发的实测,不是从别处推的:
    #:
    #:     /v1/chat/completions    请求 v4-flash -> 200,回显 **deepseek-flash**(厂商归一)
    #:     /anthropic/v1/messages  请求 v4-flash -> 200,回显 **deepseek-v4-flash**(原样)
    #:
    #:   本函数走的正是 Anthropic 那条 ⇒ **回显锁在这条端点上抓不到「旧名被 flash 承接」**:
    #:   它拿我们自己的串和我们自己的串比,是个同义反复。
    #:   🔴 所以这条线的真正防线是**发出前先归一**,不是回显。
    #:   `_load_model_from_config` 读的是运行期配置(`geo_research_config`)——
    #:   生产那一行若还写着旧名,不归一就会静默照发,而锁会放行。
    model = normalize_deepseek_model(
        _load_model_from_config('model_deepseek_official', DEEPSEEK_OFFICIAL_FLASH)
    ) or DEEPSEEK_OFFICIAL_FLASH

    url = DEEPSEEK_OFFICIAL_URL
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": model,
        "max_tokens": 2000,
        "thinking": {"type": "disabled"},
        "tools": [dict(DEEPSEEK_OFFICIAL_WEB_SEARCH_TOOL)],
        # [效率优化 · 2026-07-27 实测] 首发就带检索软约束:24 题实测命中率
        # 83.3% → 100%,延迟中位 12.0s → 11.7s,input 略降。与监测/诊断侧同口径。
        "system": DEEPSEEK_OFFICIAL_FORCE_SEARCH_SYSTEM,
        # 角标指令挂 **user prompt 尾部**(与豆包那条 2026-07-16 实测 12/12+14/14 服从的同款位置),
        # 不进 system —— system 只放检索行为指令,输出格式要求跟着问题走。
        "messages": [{"role": "user", "content": prompt + DEEPSEEK_INLINE_MARKER_SUFFIX}],
    }

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        limits=httpx.Limits(max_connections=10),
    ) as client:
        async with llm_track(
            "research_monitor",
            "deepseek_official",   # 计费/对账口径与百炼那条严格分开(价目表已登记同价)
            model=model,
            metadata={
                "prompt_id": prompt_id,
                "api": "deepseek_anthropic_messages",
                "provider": "deepseek_official",  # [2026-07-27] 真·官方直连,不再是百炼
            },
        ) as tracker:
            resp = await client.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
            if resp.status_code >= 400:
                tracker.record(success=False, error_msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            data = resp.json()
            # [复检 R2 返修] 自检**先于** record(success=True):
            #   形状不符 = 根本不是官方通道,那一次不该以 deepseek_official 记成功。
            _assert_official_deepseek_shape(data)
            #: 🔴 [WO_221-c1] 形状对了不等于是那个模型答的 —— 同一条官方通道上
            #:   flash / v4-pro / 任何替换品形状完全一样。回显锁补这个缺口。
            #:   单独一行、不并进上面那个函数:`_assert_official_deepseek_shape(data)`
            #:   这一串被 tests/engine_search_fidelity/test_r2_rework.py:112 用源码文本钉着,
            #:   改它的签名会把那条判据弄红(而它钉的是「自检先于 record」那个次序,与本单无关)。
            assert_official_echo(model, data.get('model'))
            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
            tracker.record(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                success=True,
            )

    # (真官方通道自检已在 tracker.record 之前执行)
    answer = "\n\n".join(
        (blk.get("text") or "").strip()
        for blk in (data.get("content") or [])
        if isinstance(blk, dict) and blk.get("type") == "text" and (blk.get("text") or "").strip()
    ).strip()

    # 引用只从 web_search_tool_result 取 —— 实测官方 text 块**没有** citations[] 字段。
    citations: List[dict] = []
    seen_urls: Dict[str, dict] = {}
    source_pos = 0
    for blk in data.get("content") or []:
        if not isinstance(blk, dict) or blk.get("type") != "web_search_tool_result":
            continue
        for item in blk.get("content") or []:
            if not isinstance(item, dict):
                continue
            u = item.get("url")
            if not u:
                continue
            source_pos += 1
            existing = seen_urls.get(u)
            if existing:
                # 同一 URL 在多轮检索里重复出现(实测很常见)→ 记进 answer_ranks,与百炼口径一致。
                existing.setdefault("answer_ranks", []).append(source_pos)
                continue
            citation = {
                "url": u,
                "title": item.get("title", "") or "",
                "rank": source_pos,
                "answer_ranks": [source_pos],
            }
            seen_urls[u] = citation
            citations.append(citation)
    citations = mark_answer_cited_sources(answer, citations, raw=data)

    search_requests = int(
        ((data.get("usage") or {}).get("server_tool_use") or {}).get("web_search_requests") or 0
    )

    # [复检 R2 返修] 零检索处理:改前 docstring 声称"软重试 + 兜底,与监测/诊断同口径",
    #   **实现却是单发** —— 注释不实;而且飞轮是三条路径里量最大的(2880/30 天),
    #   换官方后丢掉了百炼 forced_search 的 100% 保证却没有任何东西补位。现补齐。
    if search_requests == 0 and _retry_budget > 0:
        return await query_deepseek(prompt_id, prompt, _retry_budget=_retry_budget - 1)
    if search_requests == 0 and _fallback_enabled():
        fb = await _deepseek_dashscope_fallback_for_research(prompt_id, prompt)
        if fb is not None:
            return fb

    return {
        "platform": "deepseek",
        "provider": "deepseek_official",
        "model": model,
        "model_revision": "unknown",
        "surface": "ai_search",
        "search_mode": "deepseek_native",
        "web_search_request_count": search_requests,
        "prompt_id": prompt_id,
        "prompt": prompt,
        "answer": answer,
        "citations": citations,
        "ok": True,
        "error": None,
        "raw": data,
    }


async def _query_deepseek_via_dashscope(prompt_id: int, prompt: str) -> Dict[str, Any]:
    """[复检 R2 返修] 旧百炼路径 —— 现仅作**零检索兜底**保留,不再是默认通道。

    默认通道已换成官方 `query_deepseek`(见上)。这条保留的理由:官方是模型自主决定搜不搜,
    软重试用尽后仍零检索时需要有东西补位 —— 百炼的 `forced_search` 恒 100% 命中。
    调用方 `_deepseek_dashscope_fallback_for_research` 会把血缘如实标成 dashscope 兜底态。
    """
    api_key = _research_dashscope_key()
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未设置")

    # Phase 9 (2026-05-26): 从 geo_research_config 读模型名 · 失败回退硬编码
    #: 🔴 [WO_221-c1'] 这一条**不归一**,也不许改成常量。
    #:   `normalize_deepseek_model` 只认**官方线**的历史名;百炼上
    #:   `deepseek-v4-flash` 是另一家的**活**模型 ID,归一会把它改成一个
    #:   百炼不认识的名字。上面那条官方通道归一、这条不归一,是有意的不对称。
    model = _load_model_from_config('model_deepseek_via_dashscope', 'deepseek-v4-flash')

    url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "input": {"messages": [{"role": "user", "content": prompt}]},
        "parameters": {
            "result_format": "message",
            "enable_search": True,
            "search_options": {
                "enable_source": True,
                "enable_citation": True,
                "forced_search": True,
                "search_strategy": "max",
            },
        },
    }

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        limits=httpx.Limits(max_connections=10),
    ) as client:
        async with llm_track(
            "research_monitor",
            "deepseek",
            model=model,
            # Phase 9: deepseek 现走 dashscope (百炼 v4-flash),metadata 加 provider 区分对账
            metadata={
                "prompt_id": prompt_id,
                "api": "dashscope_generation",
                "enable_search": True,
                "provider": "dashscope",  # 实际供应商, 不是直连 deepseek 官方
            },
        ) as tracker:
            resp = await client.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
            if resp.status_code >= 400:
                tracker.record(success=False, error_msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            data = resp.json()
            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
            tracker.record(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                success=True,
            )

    output = data.get("output", {}) or {}
    answer = ""
    if "choices" in output and output["choices"]:
        answer = output["choices"][0].get("message", {}).get("content", "") or ""
    elif "text" in output:
        answer = output.get("text", "") or ""

    citations: List[dict] = []
    seen_urls: Dict[str, dict] = {}
    for source_pos, s in enumerate((output.get("search_info") or {}).get("search_results", []) or [], start=1):
        u = s.get("url")
        if not u:
            continue
        existing = seen_urls.get(u)
        if existing:
            existing.setdefault("answer_ranks", []).append(source_pos)
            continue
        citation = {
            "url": u,
            "title": s.get("title", "") or "",
            "rank": source_pos,
            "answer_ranks": [source_pos],
        }
        seen_urls[u] = citation
        citations.append(citation)
    citations = mark_answer_cited_sources(answer, citations, raw=data)

    return {
        "platform": "deepseek",
        "provider": "dashscope",
        "model": model,
        "model_revision": "unknown",
        "surface": "ai_search",
        "search_mode": "enable_search",
        "prompt_id": prompt_id,
        "prompt": prompt,
        "answer": answer,
        "citations": citations,
        "ok": True,
        "error": None,
        "raw": data,
    }


# ==================== Qwen ====================

# Phase 9 (2026-05-25) · 已知联网搜索关闭/失效的 Qwen 模型黑名单
# 即使 SystemSettings 还存这些老值, 也忽略 fallback 到 default 防升级落空
_QWEN_DEPRECATED_MODELS = frozenset({
    'qwen3.6-max-preview',  # 联网搜索已关闭
    'qwen3.7-max-preview',  # 同上
    'qwen-max-preview',     # 别名
    'qwen3-max-preview',    # 别名
})


def _load_qwen_model(default: str = "qwen-plus-latest") -> str:
    # 🔴 同一个「千问」引擎的**另一条管线**用的是 qwen3-max,不是本函数这个默认:
    #    tools/ai_visibility/ai_tester.py(诊断/监测主链,搜索请求体里写死 qwen3-max)
    #    + services/monitoring_lineage.py 的 _PLATFORM_CONTRACT["dashscope"]。
    #    两条链对客户都叫「千问」。本次不统一(模型值是被测对象),
    #    但两处代码互指,免得下一个人只看到一边就以为那是全仓口径。
    """读 Qwen 模型名 · 三级优先级:
      1. SystemSettings.research_monitor_tasks.platforms.qwen.model (历史 single source)
      2. geo_research_config.model_qwen_default (Phase 9 新增 · 跟其他 3 个平台对齐)
      3. default 参数 (硬编码 fallback)

    Phase 9 (2026-05-25/26):
      - 默认值改 qwen-plus-latest (替代 qwen3.6-max-preview · 后者联网关闭)
      - 加 _QWEN_DEPRECATED_MODELS 黑名单 · 即使 settings.json 老值污染也强制 fallback
        防生产 settings 没及时改导致升级失效
      - 默认值 fallback 走 geo_research_config 让管理员能改
    """
    # 先把 default 升级成 config 里的值 (若有) · 保留 default 入参做最终兜底
    config_default = _load_model_from_config('model_qwen_default', default)
    try:
        from config.settings_manager import load_settings
        settings = load_settings()
        rmt = getattr(settings, 'research_monitor_tasks', {}) or {}
        configured = rmt.get('platforms', {}).get('qwen', {}).get('model')
        if configured and configured.strip().lower() in _QWEN_DEPRECATED_MODELS:
            # 显式告警 · 不静默吞 settings 老值
            import logging
            logging.getLogger("GEO-ResearchMonitor").warning(
                f"[Phase 9] Qwen 配置模型 {configured!r} 在废弃黑名单中, fallback 到 {config_default!r}. "
                f"请管理员到 SystemSettings.research_monitor_tasks.platforms.qwen.model 更新"
            )
            return config_default
        return configured or config_default
    except Exception:
        return default


async def query_qwen(prompt_id: int, prompt: str) -> Dict[str, Any]:
    """Qwen (DashScope 原生 generation API + enable_search)

    返回结构跟 query_doubao 一致,但走 DashScope 原生协议:
    - output.choices[0].message.content (新格式)
    - output.text (旧格式 fallback)
    - output.search_info.search_results[].url 是引用列表

    异常:
    - DASHSCOPE_API_KEY 未设置 → RuntimeError
    - HTTP 错误 → httpx.HTTPError
    """
    api_key = _research_dashscope_key()
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未设置")

    # I2: 从 SystemSettings 读模型(spec 说 Qwen 可切),失败 fallback 默认
    model = _load_qwen_model()

    url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "input": {"messages": [{"role": "user", "content": prompt}]},
        "parameters": {
            "result_format": "message",
            "enable_search": True,
            "search_options": {
                "enable_source": True,
                "enable_citation": True,
                "forced_search": True,
                "search_strategy": "max",
            },
        },
    }

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        limits=httpx.Limits(max_connections=10),
    ) as client:
        async with llm_track(
            "research_monitor",
            "dashscope",
            model=model,
            metadata={
                "prompt_id": prompt_id,
                "api": "dashscope_generation",
                "enable_search": True,
                "provider": "dashscope",  # Phase 9: 显式标 provider 跟 deepseek 一致
            },
        ) as tracker:
            resp = await client.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
            if resp.status_code >= 400:
                tracker.record(success=False, error_msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            data = resp.json()
            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
            tracker.record(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                success=True,
            )

    output = data.get("output", {}) or {}
    answer = ""
    if "choices" in output and output["choices"]:
        answer = output["choices"][0].get("message", {}).get("content", "") or ""
    elif "text" in output:
        answer = output.get("text", "") or ""

    citations: List[dict] = []
    seen_urls: Dict[str, dict] = {}
    for source_pos, s in enumerate((output.get("search_info") or {}).get("search_results", []) or [], start=1):
        u = s.get("url")
        if not u:
            continue
        existing = seen_urls.get(u)
        if existing:
            existing.setdefault("answer_ranks", []).append(source_pos)
            continue
        citation = {
            "url": u,
            "title": s.get("title", "") or "",
            # I1: dashscope index 可重复(0/0/0)/乱序,统一用搜索列表原始位置避免 rank 撞车。
            "rank": source_pos,
            "answer_ranks": [source_pos],
        }
        seen_urls[u] = citation
        citations.append(citation)
    citations = mark_answer_cited_sources(answer, citations, raw=data)

    return {
        "platform": "qwen",
        "provider": "dashscope",
        "model": model,
        "model_revision": "unknown",
        "surface": "ai_search",
        "search_mode": "forced_search",
        "prompt_id": prompt_id,
        "prompt": prompt,
        "answer": answer,
        "citations": citations,
        "ok": True,
        "error": None,
        "raw": data,
    }


# ==================== Kimi (DashScope OpenAI 兼容 + REFERENCES 解析) ====================

# Kimi system prompt 强制吐 ---REFERENCES--- 列表 (DashScope 没官方 citation API,只能 hack)
# [2026-07-16 采纳口径] 第6条要求正文内联 [n] 标注:mark_answer_cited_sources 靠 [n]/【n】 对齐
#   REFERENCES 序号判 answer_adopted;修前 prompt 不要求内联标 → kimi 采纳恒 0(prod 30 天 0/1072 实证)。
KIMI_SYSTEM_PROMPT = """你是 Kimi。回答用户问题时必须联网搜索。

【极其重要的输出格式要求】
请在你的回答正文之后,必须附上一个完整的"引用来源"列表,格式如下:

---REFERENCES---
1. [文章标题1](完整URL1)
2. [文章标题2](完整URL2)
...

要求:
1. 必须包含你搜索时实际看到的所有网页URL(至少5条,能列多少列多少)
2. URL必须是完整的http或https开头的真实链接
3. 标题写实际的网页标题
4. 用 ---REFERENCES--- 分隔符开头,方便程序识别
5. 严禁编造URL,只能写你搜索时真实出现的网页
6. 回答正文中,凡是使用了某个网页信息的句子或段落,必须在该处内联标注 [n],n 等于"引用来源"列表中对应网页的序号(例如"……市场份额第一[3]。")。正文中至少出现一次这样的 [n] 标注;没有用到网页信息的句子不要乱标。"""

# I5: 行级匹配 markdown 引用,允许 title 含嵌套括号/中文/空格
# - ^\s*\d+\.\s*  行首数字序号(REFERENCES 列表都这样)
# - \[(.+?)\]    非贪婪 title(允许括号中文等)
# - \((https?://\S+?)\)  非贪婪 URL
# - \s*$ + re.MULTILINE  让 ^ $ 按行处理
KIMI_REF_REGEX = re.compile(
    r'^\s*\d+\.\s*\[(.+?)\]\((https?://\S+?)\)\s*$',
    re.MULTILINE,
)
KIMI_MAX_LOOPS = 5  # 多轮 tool_call 上限,防死循环


async def query_kimi(prompt_id: int, prompt: str) -> Dict[str, Any]:
    """Kimi (DashScope OpenAI 兼容 + builtin_function $web_search + REFERENCES 解析)

    复杂点:
    1. 走 OpenAI 兼容协议 /compatible-mode/v1/chat/completions
    2. tools = [{"type": "builtin_function", "function": {"name": "$web_search"}}]
    3. system prompt 强制吐 ---REFERENCES--- 列表 (DashScope 没官方 citation API)
    4. 多轮 tool_call loop 处理 finish_reason='tool_calls' (模型先调 tool 再回答)
    5. thinking={"type":"disabled"} 防 Kimi 慢思考耗 token

    异常:
    - DASHSCOPE_API_KEY 未设置 → RuntimeError
    - HTTP 错误 → httpx.HTTPError
    - C1: 5 轮跑完仍 tool_calls 未收敛 → RuntimeError 让上层 query_with_retry 重试
    - 最终回答为空 → RuntimeError
    """
    api_key = _research_dashscope_key()
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未设置")

    # Phase 9 (2026-05-26): 模型名从 geo_research_config 读
    # payload 用全名 ('kimi/kimi-k2.6') · tracker 用短名 (去掉 'kimi/' 前缀 · 跟历史 metric 兼容)
    model_full = _load_model_from_config('model_kimi_via_dashscope', 'kimi/kimi-k2.6')
    model_short = model_full.split('/', 1)[1] if '/' in model_full else model_full

    url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    messages: List[dict] = [
        {"role": "system", "content": KIMI_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    tools = [{"type": "builtin_function", "function": {"name": "$web_search"}}]

    final_content = ""
    raw_responses: List[dict] = []
    converged = False  # C1: 标记 finish=stop 是否真的发生过

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        limits=httpx.Limits(max_connections=10),
    ) as client:
        for _loop in range(KIMI_MAX_LOOPS):
            # Phase 9 (2026-05-25) · k2.5 → k2.6 升级
            # Phase 9 (2026-05-26) · 模型名从 geo_research_config.model_kimi_via_dashscope 读
            payload = {
                "model": model_full,
                "messages": messages,
                "tools": tools,
                "thinking": {"type": "disabled"},
            }
            async with llm_track(
                "research_monitor",
                "kimi",
                model=model_short,
                metadata={
                    "prompt_id": prompt_id,
                    "loop": _loop + 1,
                    "web_search": True,
                    "provider": "dashscope",  # Phase 9: Kimi 也走 dashscope 兼容模式
                },
            ) as tracker:
                resp = await client.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
                if resp.status_code >= 400:
                    tracker.record(success=False, error_msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                data = resp.json()
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                tracker.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    success=True,
                )
            raw_responses.append(data)

            choice = data["choices"][0]
            msg = choice["message"]
            finish = choice.get("finish_reason")

            if finish == "tool_calls":
                # 模型调了 tool, 把 tool_calls 加进 messages 然后给 tool 响应
                messages.append({
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": msg["tool_calls"],
                })
                for tc in msg["tool_calls"]:
                    # C2 决议: ai_tester.py:256-258 主系统线上稳定带 name,DashScope 兼容,保留
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "content": tc["function"]["arguments"],
                    })
            else:
                # 没有更多 tool_call, 这就是最终答案
                final_content = msg.get("content") or ""
                converged = True
                break

    # C1: 5 轮跑完仍 tool_calls 未收敛 → 抛错让上层重试,不能静默返空丢数据
    if not converged:
        raise RuntimeError(
            f"Kimi {KIMI_MAX_LOOPS} 轮 tool_call 未收敛, 一直在调 web_search 没给最终回答"
        )
    # C1 兜底: 即使 break 出来,如果 final_content 仍然为空也 raise 让 retry 而非吞数据
    if not final_content.strip():
        raise RuntimeError("Kimi 最终回答为空, 可能 LLM 服务异常")

    # 解析 ---REFERENCES--- 列表
    answer = final_content
    citations: List[dict] = []
    if "---REFERENCES---" in final_content:
        parts = final_content.split("---REFERENCES---", 1)
        answer = parts[0].strip()
        seen_urls = set()
        for m in KIMI_REF_REGEX.finditer(parts[1]):
            title = m.group(1).strip()
            u = m.group(2).strip()
            if u in seen_urls:
                continue
            seen_urls.add(u)
            citations.append({
                "url": u,
                "title": title,
                "rank": len(citations) + 1,
            })

    # [口径修复 2026-07-03] Kimi 答案采纳标记:与 deepseek/qwen 一致调用 mark_answer_cited_sources。
    #   Kimi 无官方 citation API,引用来自 ---REFERENCES--- 解析(rank = 列表序号)。
    #   仅当答案正文出现 [n]/【n】 且对齐引用序号时标采纳;raw 无 native annotation → 不会误升级。
    #   无明确证据(正文未内联引用)的 Kimi 行保持 search_result_only,不把 cite_url 当采纳。
    #   修前:query_kimi 从不调用此函数 → Kimi 行 is_answer_cited 恒 False,拉低全站采纳率。
    citations = mark_answer_cited_sources(answer, citations, raw=raw_responses[-1] if raw_responses else None)

    return {
        "platform": "kimi",
        "provider": "dashscope",
        "model": model_full,
        "model_revision": "unknown",
        "surface": "ai_search",
        "search_mode": "web_search_tool",
        "prompt_id": prompt_id,
        "prompt": prompt,
        "answer": answer,
        "citations": citations,
        "ok": True,
        "error": None,
        "raw": raw_responses[-1] if raw_responses else None,
    }


# ==================== 通用重试装饰器 ====================

async def query_with_retry(
    fetcher,
    prompt_id: int,
    prompt: str,
    max_retries: int = 3,
    backoff_seconds: tuple = (2, 4, 8),
) -> Dict[str, Any]:
    """
    带指数退避的平台调用重试。

    fetcher: query_doubao / query_deepseek / query_qwen / query_kimi 之一

    重试策略:
    - RuntimeError(含 VolcSearchFailureError / Kimi 未收敛 / 配置缺失)→ 重试
    - httpx.HTTPError(网络/HTTP 5xx)→ 重试
    - asyncio.TimeoutError → 重试
    - I4: KeyError / IndexError(LLM 返回结构异常如 error 体没 choices)→ 重试
    - ValueError / TypeError(配置错误 / 类型错误)→ 不重试,直接抛

    退避: 第 1 次失败等 backoff[0] 秒, 第 2 次等 backoff[1] 秒, 以此类推

    异常: max_retries 次都失败 → 抛最后一次的异常包装为 RuntimeError
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            result = await fetcher(prompt_id, prompt)
            # [GEO-R6-CAN-006] 回报真实 provider 调用次数(attempt 从 0 起 → +1)。
            #   修前 query_with_retry 只返回最终结果、不暴露实际重试了几次,
            #   上层 round_runner 硬编码 attempts=3 并按 round_call 行数(每逻辑 prompt×platform 1 行)
            #   算成本/预算 → 重试产生的额外 provider 调用被漏计(成本低估、预算失真)。
            #   现把真实尝试次数挂在返回体上,供上层按真实调用数入账(消费侧改动在 round_runner,归属另一文件)。
            if isinstance(result, dict):
                result.setdefault("attempts", attempt + 1)
            return result
        except (RuntimeError, httpx.HTTPError, asyncio.TimeoutError, KeyError, IndexError) as e:
            last_exc = e
            if attempt < max_retries - 1:
                wait_seconds = backoff_seconds[min(attempt, len(backoff_seconds) - 1)]
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
            continue
        except (ValueError, TypeError):
            # 配置/类型错误不重试,直接抛(让上层 dev 看到具体问题)
            raise

    # max_retries 次都失败
    raise RuntimeError(f"重试 {max_retries} 次仍失败: {type(last_exc).__name__}: {last_exc}")
