"""标题自然度 AI 质检旁路(包③ §3G-4)· advisory,不阻断。

工单:docs/AI-CONTEXT/WORKORDER_TITLE_NATURALNESS_ADOPTED_PATTERN_2026-08-01.md §1 末

**为什么在黑名单之外还要加这一道**(Owner 规则"能加不加需写理由"):
黑名单是**枚举**,拼接感是**语义**。枚举堵不完 —— 换一批同样别扭的词照样能写出
"…候选如何评估?…维度对照说明"这种一眼假的标题。所以让模型判一句
"这像真人编辑起的标题,还是模板拼接?"。

🔴 四件套(与包① §3E 同一口径,不另起一套):
  · **advisory 不阻断**:结论不参与任何门,不改标题,只给建议;
  · **留痕**:reviewer + 标题 hash + 结论落库;
  · **fail-closed**:调用/解析失败一律 `not_checked`,绝不猜"自然";
  · **幂等 by 标题 hash**:同一条标题不重复调模型。

🔴 渠道复用包①:`_post_chat` / `_deepseek_key` 一律走 `services.article_ai_review`,
不复制第二份 —— 官方直连(api.deepseek.com)的锁因此**自动覆盖本模块**,
不会出现"包①锁着、包③偷偷走代理版"的漂移。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Final

# 🔴 **按模块引用,不 `from ... import` 函数本身**:直接 import 会在本模块绑一个
# 局部名字,测试 monkeypatch `article_ai_review._post_chat` 就打不中(实测踩过)。
# 常量可以直接取(不会被替换),函数一律走 `_air.` 前缀。
from services import article_ai_review as _air
from services.article_ai_review import (  # noqa: F401
    DEEPSEEK_BASE_URL,
    DEEPSEEK_CHAT_PATH,
    AI_MODEL,
)

logger = logging.getLogger("GEO-TitleQualityAI")

TITLE_PROMPT_VERSION: Final = "v1-2026-08-01"
TITLE_REVIEWER: Final = f"ai:{AI_MODEL}@title-{TITLE_PROMPT_VERSION}"

VERDICT_NATURAL: Final = "natural"        # 像真人编辑起的
VERDICT_TEMPLATED: Final = "templated"    # 一眼模板拼接
VERDICT_NOT_CHECKED: Final = "not_checked"

_PROMPT: Final = """下面是一条文章标题。判断它读起来像真人编辑起的,还是像模板拼接。

只输出 JSON:
{{"verdict":"natural|templated","reason":"一句话说明理由(必须指出标题里的哪一部分)","suggestion":"如果是 templated,给一条改写建议;否则空字符串"}}

判 templated 的典型信号:
- 用了行业内部才懂的词(客户搜索时不会这么说)
- 关键词被整串硬塞进句子,读起来语法别扭
- 「A｜B」这种拼接感很重的结构,且 B 是空话

标题:{title}
"""


def title_hash(title: str) -> str:
    return hashlib.sha256(str(title or "").strip().encode("utf-8")).hexdigest()


def _parse(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw.strip("`")
        raw = raw.removeprefix("json").strip()
    try:
        obj = json.loads(raw)
    except Exception:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            obj = json.loads(raw[start:end + 1])
        except Exception:
            return {}
    return obj if isinstance(obj, dict) else {}


def assess_title_naturalness(title: str) -> dict[str, Any]:
    """判一条标题。**任何失败都返回 not_checked,不抛** —— 这是旁路,不许拖累主链。"""
    api_key = _air._deepseek_key()
    if not api_key:
        return {"verdict": VERDICT_NOT_CHECKED, "reason": "标题质检未执行(缺少可用密钥)",
                "suggestion": ""}
    try:
        data = _air._post_chat(
            DEEPSEEK_BASE_URL + DEEPSEEK_CHAT_PATH,
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            {
                "model": AI_MODEL,
                "messages": [{"role": "user", "content": _PROMPT.format(title=str(title or ""))}],
                "temperature": 0.1,
            },
        )
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    except Exception as exc:
        logger.warning("[title-qc] 调用失败 · fail-closed: %s", exc)
        return {"verdict": VERDICT_NOT_CHECKED, "reason": "标题质检未完成(服务暂时不可用)",
                "suggestion": ""}

    parsed = _parse(content)
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in {VERDICT_NATURAL, VERDICT_TEMPLATED}:
        # 🔴 解析不出有效结论 = 没判过,绝不默认"自然"(那是把失败伪装成通过)
        logger.warning("[title-qc] 结论无法识别(%r) · fail-closed", verdict)
        return {"verdict": VERDICT_NOT_CHECKED, "reason": "标题质检结论无法解析",
                "suggestion": ""}

    reason = str(parsed.get("reason") or "").strip()
    if verdict == VERDICT_TEMPLATED and not reason:
        # 说"像模板"却指不出哪里像 = 与 §3D 同型的笼统文案,不透给用户
        logger.warning("[title-qc] templated 但无具体理由 · 视为未核查")
        return {"verdict": VERDICT_NOT_CHECKED, "reason": "标题质检结论缺少具体理由",
                "suggestion": ""}

    return {
        "verdict": verdict,
        "reason": reason,
        "suggestion": str(parsed.get("suggestion") or "").strip(),
    }
