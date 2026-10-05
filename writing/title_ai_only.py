"""标题 AI-only 契约:进入候选池/成品的标题必须出自 AI,兜底也必须是 AI。

Owner 裁决(2026-08-17 原话口径):
  「拒绝任何一切兜底行为,必须根据用户的主题来进行标题的创作,AI 必须介入,
   否则就生成失败都行,或者用其他标题来进行兜底,**但是都一定是 AI 生成的,不是硬编码**。」

触发实证:选题池 15 条候选里 4 条与硬编码兜底表**逐字对应**
(`keyword_topic_generator._fallback_style_title_map` @ ecce9985 的
「常见问题一次说清 / 实操流程说明 / 趋势与建议 / 一文看懂」)。

本模块是**失败梯**的唯一实现,三级:

  ① 同批次已生成的**合格 AI 候选**补位 —— 模型这一批本来就多返/返错槽的候选,
     过完关键词身份复核后可以顶上失败的槽位(仍是 AI 产物,只是换个角度);
  ② **AI 重试** —— 降级 prompt + 主模型→兜底模型两跳。模型选择走
     `llm_utils.get_llm_config` / `get_fallback_llm_config`,**不硬编码型号**;
  ③ 仍失败 = **该槽位显式失败**,不硬凑满数。调用方拿到的是"该 key 缺席",
     把状态写成用户看得懂的人话(见 `TITLE_FAILURE_USER_MESSAGE`),不静默补串。

🔴 本模块**不得**出现任何可直接当标题用的中文模板串。判据 §3.1 的扫描器
会把退役模板表全集当签名去扫运行时产出;这里若抄一份就等于换个地方硬编码。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence

#: 策略版本。落进 topic 的 `title_origin_version`,便于事后归因。
TITLE_AI_ONLY_POLICY_VERSION = "title-ai-only-v1.0"

#: 标题来源枚举(落进 topic["title_origin"])。**没有 template 这一项** ——
#: 硬编码模板已退出运行时标题产出路径,枚举里留一个口子就是留一条回头路。
TITLE_ORIGIN_PRIMARY = "ai_primary"              # 主链 LLM 直出
TITLE_ORIGIN_BATCH_SURPLUS = "ai_batch_surplus"  # ① 同批合格 AI 候选补位
TITLE_ORIGIN_RETRY = "ai_retry"                  # ② AI 重试(降级 prompt / 兜底模型)
TITLE_ORIGIN_DEDUPE = "ai_dedupe_regenerated"    # 去重冲突后的 AI 重生成

ALLOWED_TITLE_ORIGINS = frozenset({
    TITLE_ORIGIN_PRIMARY,
    TITLE_ORIGIN_BATCH_SURPLUS,
    TITLE_ORIGIN_RETRY,
    TITLE_ORIGIN_DEDUPE,
})

#: 全梯失败时给用户看的话。人话、带出口(元指令「提示二选一」:有动作或不显示)。
#: 这是**状态文案**,不是标题 —— 它永远不会被写进 `optimized_title`。
TITLE_FAILURE_USER_MESSAGE = "这批标题没能生成出来，点「重新生成」再试一次"

#: 失败码(沿用既有 ArticleGenerationFailure 形状,不新造结构)。
#: 🔴 [WO_232] 这一个码原来同时背两件事:**模型没给出东西** 和 **模型给了、被我们
#:    自己的关键词身份复核拒掉**。真客户看到的是「AI 不可用,点重新生成」——
#:    而 llm_call_log 里写作线全 success,AI 一次都没失败。展示与真因不符,
#:    用户只能瞎点重试(Owner 09-13:要人猜的展示不允许)。所以拆成两个。
TITLE_FAILURE_CODE = "TITLE_AI_UNAVAILABLE"
TITLE_ALIGNMENT_FAILURE_CODE = "TITLE_ALIGNMENT_REJECTED"


def alignment_failure_user_message(keyword: object = "", retries: int = 0) -> str:
    """复核拒绝时给用户看的话 —— 说真话,并且带得出去的动作。

    元指令「提示二选一」:要么给动作,要么别显示。这里给两个动作
    (重新生成 / 手动编辑标题),所以可以显示。
    """
    core = str(keyword or "").strip()
    quoted = f"「{core}」" if core else ""
    tail = f",已自动重试 {retries} 次" if retries else ""
    return (
        f"标题没保留完整关键词{quoted}{tail}。"
        "可以点「重新生成」,或自己编辑标题(把完整关键词原样写进去)。"
    )


class TitleAIUnavailable(RuntimeError):
    """AI 三级梯全部失败。调用方必须显式失败,不许改用模板。"""

    def __init__(self, message: str = TITLE_FAILURE_USER_MESSAGE):
        super().__init__(message)
        self.user_message = message
        self.code = TITLE_FAILURE_CODE


@dataclass
class TitleSlotRequest:
    """一个待生成的标题槽位。`key` 由调用方自定(通常是 (keyword_id, slot_index))。"""

    key: Any
    keyword: str
    article_style: str = ""
    angle: str = ""
    note: str = ""


@dataclass
class TitleLadderResult:
    """失败梯的产出。`titles` 只含**成功**的槽位,失败槽位一律缺席。"""

    titles: Dict[Any, str] = field(default_factory=dict)
    origins: Dict[Any, str] = field(default_factory=dict)
    failed_keys: List[Any] = field(default_factory=list)
    retry_attempted: bool = False
    retry_succeeded: int = 0
    surplus_used: int = 0
    #: [WO_232] 模型**给了**标题、但过不了关键词身份复核的槽位:{key: [被拒标题]}。
    #: 没有这一份记录就分不清「AI 不可用」和「我们自己拒掉了」—— 两者对用户
    #: 该说的话和该给的动作完全不同。
    alignment_rejected: Dict[Any, List[str]] = field(default_factory=dict)

    def as_report(self) -> Dict[str, Any]:
        return {
            "policy_version": TITLE_AI_ONLY_POLICY_VERSION,
            "resolved": len(self.titles),
            "failed": len(self.failed_keys),
            "surplus_used": self.surplus_used,
            "retry_attempted": self.retry_attempted,
            "retry_succeeded": self.retry_succeeded,
            "alignment_rejected": len(self.alignment_rejected),
        }


def _aligned(title: str, keyword: object, brand_names: Sequence[str] = ()) -> bool:
    """关键词身份复核。判不出来就算不合格 —— 宁可少一条,也不许换客户买的词。

    [WO_232] `brand_names` 透传给复核器:关键词核就是品牌注册名时,标题里的
    受控缩写(去省市前缀 / 去组织形式后缀)算同一商业对象。不传就是老行为。
    """
    if not str(keyword or "").strip():
        return bool(str(title or "").strip())
    try:
        from writing.title_keyword_alignment import assess_title_keyword_alignment

        return bool(assess_title_keyword_alignment(
            title, keyword, brand_names=tuple(brand_names or ())).aligned)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# ① 同批合格 AI 候选补位
# ---------------------------------------------------------------------------

def take_batch_surplus_title(
    keyword: str,
    surplus_pool: Dict[str, List[str]],
    *,
    used_titles: Iterable[str] = (),
    brand_names: Sequence[str] = (),
) -> str:
    """从**同一关键词**的同批多余 AI 候选里取一条没用过的。

    🔴 只在同一关键词内取。跨关键词补位会把 A 的标题安到 B 头上 ——
    那是本仓反复强调的身份红线(客户买的词一个字都不许换),
    比"没有标题"严重得多。

    就地消费 `surplus_pool`(取走即出池),所以同一条不会顶两个槽。
    """
    pool = surplus_pool.get(str(keyword or ""))
    if not pool:
        return ""
    seen = {str(t or "").strip() for t in used_titles if str(t or "").strip()}
    while pool:
        candidate = str(pool.pop(0) or "").strip()
        if not candidate or candidate in seen:
            continue
        if not _aligned(candidate, keyword, brand_names):
            continue
        return candidate
    return ""


# ---------------------------------------------------------------------------
# ② AI 重试(降级 prompt · 主模型 → 兜底模型)
# ---------------------------------------------------------------------------

_RETRY_INSTRUCTION = (
    "你是中文 GEO 选题编辑。为下面每一条【待生成】各写 1 个标题。\n\n"
    "硬约束:\n"
    "1. 标题必须紧扣 `purchased_keyword` 这个客户已购买的问题 —— 地区、产品、"
    "采购意图一个都不许换;可以自然拆开使用,不要整串逐字硬塞;\n"
    # [WO_232] 上一轮就是栽在这里:关键词是完整公司注册名时,模型很自然地把它
    # 缩写掉(丢「有限公司」/丢省名),复核判成换了商业对象,整梯全拒。
    # 把**拒绝理由**直接写成硬约束喂回去,比让它再猜一次便宜得多。
    "1b. 每条都给了 `must_contain`:标题里必须**原样包含**这一串,一个字都不能少、"
    "不能改写、不能缩写(它是客户买的那个商业对象本身,通常是公司注册全名);\n"
    "2. 标题要贴合 `article_style` 这个回答角度;该角度确实不适合这个关键词时,"
    "在同一业务对象内换个角度写,不要写成脱离购买意图的泛话题;\n"
    "3. 不得出现绝对化/保证性用语(最好、第一、唯一、保证等);\n"
    "4. 不得虚构报价、案例、奖项、媒体背书、专家观点;\n"
    "5. 不要与【已用标题】重复,也不要套同一个句式;\n"
    "6. 只返回 JSON 数组:[{\"key\": 原 key 字符串, \"title\": \"标题\"}],不要解释。\n"
)


def _build_retry_prompt(
    requests: Sequence[TitleSlotRequest],
    *,
    brand_name: str,
    industry: str,
    avoid_titles: Sequence[str],
) -> str:
    from writing.title_keyword_alignment import title_anchor_from_purchased_keyword

    wanted = [
        {
            "key": str(r.key),
            "purchased_keyword": str(r.keyword or ""),
            # [WO_232] 关键词核 = 剥掉购买语尾之后那个商业对象本身。
            # 这正是复核器拿去比对的那一串 —— 喂给模型的和判它的是同一份,
            # 不是我另写一个"差不多"的东西(本仓:判据与被判对象必须同源)。
            "must_contain": title_anchor_from_purchased_keyword(r.keyword),
            "article_style": str(r.article_style or ""),
            "angle": str(r.angle or ""),
            "note": str(r.note or ""),
        }
        for r in requests
    ]
    return (
        f"{_RETRY_INSTRUCTION}\n"
        f"【品牌】{brand_name or ''}\n【行业】{industry or ''}\n\n"
        "【已用标题(需避开)】\n"
        f"{json.dumps(list(avoid_titles)[:120], ensure_ascii=False)}\n\n"
        f"【待生成】\n{json.dumps(wanted, ensure_ascii=False, indent=2)}\n"
    )


def _parse_retry_payload(content: str) -> Dict[str, str]:
    parsed: Any = None
    try:
        import json_repair

        parsed = json_repair.loads(content)
    except Exception:
        parsed = None
    if not isinstance(parsed, (list, dict)):
        match = re.search(r"\[[\s\S]*\]", str(content or ""))
        if not match:
            return {}
        try:
            parsed = json.loads(match.group())
        except Exception:
            return {}
    if isinstance(parsed, dict):
        parsed = parsed.get("titles") or parsed.get("topics") or []
    out: Dict[str, str] = {}
    for item in parsed if isinstance(parsed, list) else []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") if item.get("key") is not None else item.get("id") or "").strip()
        title = str(item.get("title") or item.get("optimized_title") or "").strip()
        if key and title:
            out[key] = title
    return out


async def _call_one_model(
    api_url: str, api_key: str, model: str, prompt: str, *, timeout: float = 90.0,
) -> Dict[str, str]:
    import httpx

    from writing.llm_utils import get_thinking_disabled_params

    body: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        # 重试的目的就是"换个说法",要发散。
        "temperature": 0.9,
        "max_tokens": 2000,
    }
    body.update(get_thinking_disabled_params(api_url, model))
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            api_url,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    return _parse_retry_payload(content)


def _retry_model_attempts() -> List[tuple]:
    """重试用的模型跳表。**全部来自 model config**,不硬编码型号。"""
    from writing.llm_utils import get_fallback_llm_config, get_llm_config

    attempts: List[tuple] = []
    try:
        api_url, api_key, model, _provider = get_llm_config("topic_planning", "writing")
        if api_url and api_key and model:
            attempts.append((api_url, api_key, model))
    except Exception:
        pass
    try:
        fb_url, fb_key, fb_model, _fb_provider = get_fallback_llm_config()
        if fb_url and fb_key and fb_model and (fb_url, fb_key, fb_model) not in attempts:
            attempts.append((fb_url, fb_key, fb_model))
    except Exception:
        pass
    return attempts


async def ai_retry_titles(
    requests: Sequence[TitleSlotRequest],
    *,
    brand_name: str = "",
    industry: str = "",
    avoid_titles: Sequence[str] = (),
    rejected_out: Dict[Any, List[str]] | None = None,
) -> Dict[Any, str]:
    """②:降级 prompt 重试。主模型一跳 → 兜底模型一跳,型号全部来自 model config。

    返回 ``{原 key: 标题}``,只含模型真的给出、且过了关键词身份复核的条目。
    全失败返回空 dict —— 交给 ③ 显式失败,**绝不**在这里造串。

    [WO_232] ``rejected_out`` 给进来的话,**模型给了但被复核拒掉**的标题记在里面。
    上层据此分辨「AI 不可用」与「我们自己拒的」—— 这两件事对用户该说的话不一样。
    """
    if not requests:
        return {}

    prompt = _build_retry_prompt(
        requests, brand_name=brand_name, industry=industry, avoid_titles=avoid_titles,
    )
    by_key = {str(r.key): r for r in requests}

    resolved: Dict[Any, str] = {}
    for api_url, api_key, model in _retry_model_attempts():
        if len(resolved) >= len(by_key):
            break
        try:
            payload = await _call_one_model(api_url, api_key, model, prompt)
        except Exception as exc:  # noqa: BLE001 — 换下一跳,最终由 ③ 显式失败
            print(f"  ⚠️ [title-ai-only] 重试调用失败 model={model}: {str(exc)[:120]}")
            continue
        for key_text, title in payload.items():
            request = by_key.get(key_text)
            if request is None or request.key in resolved:
                continue
            if not _aligned(title, request.keyword, (brand_name,) if brand_name else ()):
                # [WO_232] 模型**给出**了标题,是我们的复核不收 —— 记下来。
                if rejected_out is not None:
                    rejected_out.setdefault(request.key, []).append(title)
                continue
            resolved[request.key] = title
    return resolved


def _run_sync(coro):
    """同步调用点(CLI / 同步分发器)用的包装。事件循环已在跑时开新线程跑新循环。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def ai_retry_titles_sync(
    requests: Sequence[TitleSlotRequest],
    *,
    brand_name: str = "",
    industry: str = "",
    avoid_titles: Sequence[str] = (),
) -> Dict[Any, str]:
    return _run_sync(ai_retry_titles(
        requests, brand_name=brand_name, industry=industry, avoid_titles=avoid_titles,
    ))


# ---------------------------------------------------------------------------
# 三级梯统一入口
# ---------------------------------------------------------------------------

async def resolve_titles_ai_only(
    requests: Sequence[TitleSlotRequest],
    *,
    brand_name: str = "",
    industry: str = "",
    surplus_pool: Dict[str, List[str]] | None = None,
    avoid_titles: Sequence[str] = (),
) -> TitleLadderResult:
    """① 同批合格 AI 候选 → ② AI 重试 → ③ 显式失败。

    ③ 表现为:该 key **不出现在** ``result.titles`` 里,而是进 ``failed_keys``。
    调用方据此少出一条候选(不硬凑满数),并把状态写成用户看得懂的人话。
    """
    result = TitleLadderResult()
    if not requests:
        return result

    used = [str(t) for t in avoid_titles if str(t or "").strip()]
    pool = {k: list(v) for k, v in (surplus_pool or {}).items()}

    brands = (brand_name,) if str(brand_name or "").strip() else ()

    still_pending: List[TitleSlotRequest] = []
    for request in requests:
        candidate = take_batch_surplus_title(
            request.keyword, pool, used_titles=used, brand_names=brands)
        if candidate:
            result.titles[request.key] = candidate
            result.origins[request.key] = TITLE_ORIGIN_BATCH_SURPLUS
            result.surplus_used += 1
            used.append(candidate)
        else:
            still_pending.append(request)

    if still_pending:
        result.retry_attempted = True
        retried = await ai_retry_titles(
            still_pending, brand_name=brand_name, industry=industry, avoid_titles=used,
            rejected_out=result.alignment_rejected,
        )
        for request in still_pending:
            title = retried.get(request.key)
            if title:
                result.titles[request.key] = title
                result.origins[request.key] = TITLE_ORIGIN_RETRY
                result.retry_succeeded += 1
                used.append(title)
            else:
                result.failed_keys.append(request.key)

    return result


def resolve_titles_ai_only_sync(
    requests: Sequence[TitleSlotRequest],
    *,
    brand_name: str = "",
    industry: str = "",
    surplus_pool: Dict[str, List[str]] | None = None,
    avoid_titles: Sequence[str] = (),
) -> TitleLadderResult:
    return _run_sync(resolve_titles_ai_only(
        requests, brand_name=brand_name, industry=industry,
        surplus_pool=surplus_pool, avoid_titles=avoid_titles,
    ))
