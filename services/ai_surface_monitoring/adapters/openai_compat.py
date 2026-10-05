"""OpenAI Chat Completions 兼容 adapter(新建)。

用于:
  - ``yuanbao_hy3_tokenhub``:腾讯 TokenHub hy3(Codex #4:HY3_API_KEY + tokenhub.tencentmaas.com/v1
    + hy3;**回显不符 fail-closed,无 silent fallback**;用户端显示"元宝",管理员 trace 留 provider/model)。
  - ``deepseek_native_no_search``:DeepSeek 官方 api.deepseek.com deepseek-v4-flash(Codex #3;无搜索)。

安全:API key 只从环境变量读(spec.env_key_var);缺失即 fail-closed(→ error),不硬编码、不入日志。
依赖注入:``http_post`` 为 ``async (url, headers, json_body, timeout) -> (status_code, json)``;
默认 httpx 实现。测试注入 fake,零真实网络。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from services.ai_surface_monitoring.adapters.base import BaseSurfaceAdapter, EngineResult, extract_openai_usage
from services.ai_surface_monitoring.contracts import CollectionRequest, utcnow

logger = logging.getLogger("GEO-AISurface-OpenAICompat")

HttpPostFn = Callable[[str, Dict[str, str], Dict[str, Any], float], Awaitable[Tuple[int, dict]]]

DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_TOKENS = 8192


class ModelEchoMismatchError(RuntimeError):
    """响应回显的 model 与期望不符 → fail-closed,禁止 silent fallback 后仍记为该表面。"""


class MissingApiKeyError(RuntimeError):
    """API key 环境变量缺失 → fail-closed。"""


async def _httpx_post(url: str, headers: Dict[str, str], json_body: Dict[str, Any], timeout: float) -> Tuple[int, dict]:
    import httpx  # 惰性,保证 import 干净

    async with httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=10)) as client:
        resp = await client.post(url, headers=headers, json=json_body, timeout=timeout)
        try:
            payload = resp.json()
        except Exception:
            payload = {"_non_json_body": resp.text[:500]}
        return resp.status_code, payload


def _model_echo_matches(echo: Optional[str], expected: str) -> bool:
    """**严格**匹配:精确 或 仅"版本/日期"后缀(分隔符 + 数字开头,可含前导 v)。

    观测系统不能把不同模型混进同一时间序列(复审 P1-5 / 复审#2 / 复审#2-R2):
      - 通过:精确 ``hy3`` / ``deepseek-v4-flash``;或纯"版本/日期"后缀 ``hy3-20260716`` /
        ``hy3-v2`` / ``hy3-2.5`` / ``hy3-v2.5`` / ``hy3-2026-07-16``(每段=分隔符+可选 v+``数字[.数字]*``)。
      - **拒绝**:``<expected>-lite`` / ``hy35pro`` / ``hy3x`` / ``deepseek-chat``;
        (注:2026-07-27 起元宝的 expected 已是 ``hy3-preview`` 本身 —— 精确相等即通过;
         此时被拒的是 ``hy3-preview-lite`` 这类再加词尾的变体,语义不变)
        数字打头夹词变体 ``hy3-2preview`` / ``hy3-0-lite``;点作分隔的 ``hy3.v3``(收紧)→ fail-closed。
    复审#2-R2 关键(修 ReDoS):分隔符类**不含 ``.``**,点只出现在 ``\\d+(?:\\.\\d+)*`` 内,故"分隔符/
    内容"无重叠 → 正则线性无灾难性回溯。旧式 ``[\\d.]*`` 让 ``.`` 兼作分隔符与内容,在 ``(?:...)+`` 下
    对失败输入(``hy3`` + ``.9``×N + 词)指数级回溯,可被半可信 provider 回显打挂(``_fetch`` /
    ``_probe_once`` 同步 CPU 阻塞事件循环 · WORKERS=1)。超 128 字符回显直接判异模型(输入成本上界)。
    """
    if not echo:
        return False
    echo = str(echo)
    if echo == expected:
        return True
    # 防御:真实模型名很短;超长回显直接判不同模型(兼作 provider 输入成本上界,杜绝病态长串)
    if len(echo) > 128:
        return False
    # 复审#2-R2 修 ReDoS:分隔符类 [-_:@/]**不含 .**;点仅出现在 \d+(?:\.\d+)* 内 →"分隔符/内容"
    # 无重叠歧义 → 正则线性、无灾难性回溯。每段=分隔符+可选 v+数字[.数字]*;字母词尾仍拒。
    return bool(re.fullmatch(re.escape(expected) + r"(?:[-_:@/]v?\d+(?:\.\d+)*)+", echo))


class OpenAICompatibleAdapter(BaseSurfaceAdapter):
    """通用 OpenAI 兼容表面 adapter。"""

    def __init__(
        self,
        surface_key: str,
        *,
        http_post: Optional[HttpPostFn] = None,
        api_key_getter: Optional[Callable[[str], str]] = None,
        verify_model_echo: bool = True,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.9,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        super().__init__(surface_key)
        if not self.spec.base_url:
            raise ValueError(f"{surface_key} 缺 base_url,不能用 OpenAICompatibleAdapter")
        if not self.spec.env_key_var:
            raise ValueError(f"{surface_key} 缺 env_key_var")
        self._http_post: HttpPostFn = http_post if http_post is not None else _httpx_post
        self._api_key_getter = api_key_getter if api_key_getter is not None else (lambda name: os.getenv(name, ""))
        self._verify_model_echo = verify_model_echo
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._timeout = timeout

    def _api_key(self) -> str:
        key = (self._api_key_getter(self.spec.env_key_var) or "").strip()
        if not key:
            raise MissingApiKeyError(f"{self.spec.env_key_var} 未设置(fail-closed;禁止硬编码/silent fallback)")
        return key

    def _endpoint(self) -> str:
        base = self.spec.base_url.rstrip("/")
        return f"{base}/chat/completions"

    def _thinking_params(self) -> Dict[str, Any]:
        # 复用既有 get_thinking_disabled_params(不 fork);import 轻(stdlib only)
        from writing.llm_utils import get_thinking_disabled_params

        return get_thinking_disabled_params(self.spec.base_url, self.spec.default_model_key) or {}

    async def _fetch(self, request: CollectionRequest, prompt_text: str) -> EngineResult:
        api_key = self._api_key()  # 缺 key → MissingApiKeyError → base 归 error(不 fallback)
        expected_model = self.spec.default_model_key
        body: Dict[str, Any] = {
            "model": expected_model,
            "messages": [{"role": "user", "content": prompt_text}],
            "stream": False,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        body.update(self._thinking_params())
        # [C · 2026-07-27] 联网检索:TokenHub 自带能力,同端点同 key,只加一个字段。
        #   search_source:**标准版 standard(12 元/千次)** —— Owner 2026-07-27 指定,不用轻量版 lite(7 元/千次)。
        #   官方"不传即默认 standard",但这里**显式写出来**:让请求体自己说清走的是哪一档,
        #   免得将来官方改默认值或有人误以为在省钱档。
        #   只对声明了检索的表面加,其它表面请求体逐字节不变。
        if self.spec.default_search_enabled and self.spec.search_provider == "tencent_tokenhub":
            body["web_search_options"] = {"enable": True, "search_source": "standard"}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        status_code, payload = await self._http_post(self._endpoint(), headers, body, self._timeout)
        if status_code >= 400:
            # 结构化错误,不伪装空回答成功;不 fallback
            #
            # 🔴🔴 [2026-08-03] 这里原来只 raise,**响应体从未进过日志** —— 代价是实打实的:
            #   元宝连续 402 了一整天,面板上只有一句 `skipped non-collectable platforms: yuanbao`,
            #   与"没订阅"/"没配 key"长得一模一样。排查时先后误判成【没接线】→【没配 key】
            #   →【账户欠费】→【web_search_options 触发计费】,四个结论全错,
            #   而真答案就写在那 400 字响应体里:`401008 服务免费体验额度已耗尽,且未开启后付费`。
            #   → 供应商把原因告诉我们了,是我们没记下来。**必须落日志再抛。**
            #
            # 只记状态码 + 响应体摘要 + 供应商 trace_id;**绝不记 api_key / 提示词 / 答案正文**。
            detail = _redact_provider_error(payload)
            logger.error(
                "[%s] 供应商拒绝 · HTTP %s · model=%s · reason=%s · detail=%s",
                self.surface_key, status_code, expected_model,
                _classify_provider_error(status_code, payload), detail,
            )
            raise RuntimeError(f"HTTP {status_code}: {detail}")
        if not isinstance(payload, dict):
            raise RuntimeError("响应非 JSON dict")

        echo = payload.get("model")
        if self._verify_model_echo and not _model_echo_matches(echo, expected_model):
            # Codex #4:回显不符 fail-closed,禁止 silent fallback 后仍记为本表面。
            # 先留痕(trace_id + served echo;绝不记 api_key / answer 正文)使供应商换模型可审计,再 fail-closed。
            trace_id = payload.get("id")
            logger.warning(
                "[%s] model-echo mismatch · expected=%s served=%s trace_id=%s → fail-closed(丢弃答案)",
                self.surface_key, expected_model, echo, trace_id,
            )
            raise ModelEchoMismatchError(
                f"{self.surface_key} 期望 model {expected_model!r},实际回显 {echo!r}(trace_id={trace_id})"
            )

        choices = payload.get("choices") or []
        message = (choices[0].get("message") if choices else {}) or {}
        answer = message.get("content") or ""
        finish_reason = choices[0].get("finish_reason") if choices else None
        provider_refused = finish_reason in ("content_filter",) or bool(message.get("refusal"))

        inp, out, cached = extract_openai_usage(payload)
        model_revision = str(echo) if echo and str(echo) != expected_model else None

        # [C · 2026-07-27] 检索证据。检索**真的发生过**才算 enabled ——
        # 不拿"我们在请求里开了开关"当"已检索"(元宝开通前正是:传了开关、200、却一条没搜)。
        citations = _extract_tokenhub_search_results(message, answer)
        search_calls = _count_tokenhub_search_calls(payload)
        search_enabled = (
            bool(search_calls > 0) if self.spec.default_search_enabled
            else self.spec.default_search_enabled
        )
        if self.spec.default_search_enabled and search_calls > 0 and not citations:
            # 搜了却拿不到来源 = 解析口径与响应结构脱节,留痕不静默。
            logger.warning(
                "[%s] 检索已触发 %s 次但未解析到来源 · message keys=%s · trace_id=%s",
                self.surface_key, search_calls, sorted(message.keys()), payload.get("id"),
            )
        elif self.spec.default_search_enabled and search_calls == 0:
            # 开了检索却一次没搜 —— 可能是控制台额度用尽/被关,必须看得见,不许静默退化成"原生回答"。
            logger.warning(
                "[%s] 已声明联网检索但本次 0 次调用(额度耗尽?模型不支持?)· trace_id=%s",
                self.surface_key, payload.get("id"),
            )

        return EngineResult(
            answer=answer,
            citations=citations,  # 无搜索表面仍为 [](citations=[] 是合法一等状态)
            raw=payload,
            provider_key=self.spec.provider_key,
            model_key=expected_model,          # 规范模型名(元宝对客户显示"元宝",此为管理员 trace)
            model_revision=model_revision,
            search_provider=self.spec.search_provider,
            search_enabled=search_enabled,
            search_queries=None,
            provider_trace_id=payload.get("id"),
            provider_refused=provider_refused,
            input_tokens=inp,
            output_tokens=out,
            cached_tokens=cached,
        )

    async def _probe_once(self) -> tuple[str, Optional[int], Optional[str], Optional[str]]:
        """零成本健康探针:GET {base_url}/models,确认可达 + key 有效 + 期望模型在列。不回显 key。"""
        try:
            api_key = self._api_key()
        except MissingApiKeyError:
            return "unavailable", None, None, "missing_api_key"

        import time

        started = time.monotonic()
        try:
            status_code, payload = await self._http_get_models(api_key)
        except Exception as exc:
            return "unavailable", None, None, f"probe_error:{type(exc).__name__}"
        latency_ms = int((time.monotonic() - started) * 1000)

        if status_code >= 500:
            return "degraded", latency_ms, None, f"http_{status_code}"
        if status_code >= 400:
            return "unavailable", latency_ms, None, f"http_{status_code}"

        # 检查期望模型是否在列(尽力而为;列不出不算致命)
        model_ids = _extract_model_ids(payload)
        if model_ids and not any(_model_echo_matches(m, self.spec.default_model_key) for m in model_ids):
            return "degraded", latency_ms, None, "expected_model_absent"
        return "healthy", latency_ms, None, None

    async def _http_get_models(self, api_key: str) -> Tuple[int, dict]:
        """默认走 httpx;为便于测试也可被子类/注入替换(此处直接实现)。"""
        import httpx

        base = self.spec.base_url.rstrip("/")
        url = f"{base}/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=headers)
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            return resp.status_code, payload


#: 形如 sk-xxx / Bearer xxx 的疑似凭据 —— 供应商偶尔会把请求内容回显进错误体,
#: 落日志前一律打码。宁可多打一次码,也不能把 key 写进日志。
_SECRETISH_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9_\-\.]{8,})")


def _redact_provider_error(payload: Any, limit: int = 400) -> str:
    """供应商错误体 → 可安全落日志的摘要。

    [2026-08-03] 存在的理由:元宝 402 整天没人看懂,因为**响应体从未被记下来**。
    供应商其实把原因写清楚了(`401008 免费体验额度已耗尽,且未开启后付费`)。
    """
    text = str(payload)
    text = _SECRETISH_RE.sub("<redacted>", text)
    text = " ".join(text.split())          # 压掉换行,保证单行可 grep
    return text[:limit]


#: 供应商错误分类 → 决定"该不该叫人"。
#: 计费/额度类必须显性告警(它不会自愈);未订阅/参数类属正常跳过或代码问题。
def _classify_provider_error(status_code: int, payload: Any) -> str:
    blob = str(payload).lower()
    # 402 与额度/后付费关键词:腾讯 TokenHub 用 401008;其它供应商用词各异,故按语义词兜底。
    if status_code == 402 or any(
        k in blob for k in ("quota", "billing", "insufficient", "arrears",
                            "postpaid", "额度", "欠费", "后付费", "余额")
    ):
        return "billing_or_quota:需人工处理(不会自愈)"
    if status_code in (401, 403):
        return "auth:凭据或权限"
    if status_code == 429:
        return "rate_limit:限流(可重试)"
    if status_code == 404:
        return "not_found:端点或模型不存在(供应商可能已下线该模型)"
    if 400 <= status_code < 500:
        return "bad_request:请求参数"
    return "provider_error:供应商侧"


def _count_tokenhub_search_calls(payload: Any) -> int:
    """TokenHub 服务端检索次数:``usage.tool_usage.web_search_call``(2026-07-27 实测字段)。"""
    if not isinstance(payload, dict):
        return 0
    tool_usage = (payload.get("usage") or {}).get("tool_usage") or {}
    try:
        return int(tool_usage.get("web_search_call") or 0)
    except (TypeError, ValueError):
        return 0


def _extract_tokenhub_search_results(message: Any, answer_text: str = "") -> list:
    """``choices[0].message.search_results[]`` → base 层认识的 citation dict 列表。

    实测字段(2026-07-27):``index`` / ``name`` / ``site`` / ``snippet`` / ``url``。
    - 标题取 ``name``(不是 ``title``);``snippet`` 是正文摘要,不落 citation;
    - 同一轮里同 URL 会重复出现(实测 13 条里有重复),按 URL 去重并保留最靠前的 ``index``;
    - **``is_answer_cited`` 按正文里的 ``[n]`` 角标如实判**:元宝正文会写 ``...[1][2]``,
      带角标的才是"答案真的采纳了它",没角标的只是"检索到但没用上"。
      契约里 citation(采纳) 与 source(仅访问) 的区分靠这个,不能一律标成采纳。

    返回**普通 dict**(键 url/title/rank/is_answer_cited)—— base._envelope 负责转
    CitationEvidence 并做防御式解析,这里不越俎代庖构造模型对象。
    """
    out: list = []
    if not isinstance(message, dict):
        return out
    answer = str(answer_text or "")
    seen: set = set()
    for item in message.get("search_results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        raw_index = item.get("index")
        try:
            rank = int(raw_index) if raw_index is not None else len(out) + 1
        except (TypeError, ValueError):
            rank = len(out) + 1
        out.append({
            "url": url,
            "title": (str(item.get("name")).strip() or None) if item.get("name") else None,
            "rank": rank,
            "is_answer_cited": bool(raw_index is not None and f"[{raw_index}]" in answer),
        })
    return out


def _extract_model_ids(payload: Any) -> list:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("id"):
            out.append(str(item["id"]))
    return out
