"""AI 内容审核评估层(包① §3E)· 官方 DeepSeek V4 Flash 直连。

工单:docs/AI-CONTEXT/WORKORDER_AI_REVIEW_REPLACES_HUMAN_2026-08-01.md §2 / §3D / §3E

Owner 08-01 拍板:"能不人工审核就不人工审核,加模型评估,用官方 DeepSeek V4 Flash,
人工看不懂,超纲了。" → 本模块给每篇文章一个 AI 结论,把待人工面收敛到极小。

🔴 四条硬性质(缺一条这功能就会从"减负"变成"事故源"):

1. **advisory,绝不阻塞发布**。本模块产出的 level 不进 `evaluate_publication_eligibility`
   的 `eligible` 派生。§3A 已经把内容类降为提示级,AI 结论只是把提示说得更准,
   不是给它加回一道新门。

2. **fail-closed 但不阻断**。AI 调用失败 → 该篇停在 `not_checked`,
   **绝不冒充"已核查"**(那才是真危险:客户以为核过了)。同时它照样能发布 ——
   fail-closed 说的是"不假装通过",不是"拦住不让走"。

3. **幂等 by 正文 hash**。同一篇 + 同一份正文 + 同一个 reviewer 只可能有一行;
   重跑批处理时先查后跳,不重复调模型、不重复落审计行。

4. **具体触发原因**。§3D 明令:提示必须显示具体触发原因,不许一句
   "高风险或无全文核验证据"糊弄 —— 盘点实证 93% 的笼统文案真因是
   `verified_count==0`,那是能说清楚的事,没有理由含糊。
   本模块对每条 finding 强制要求非空 `trigger`,拿不到就丢弃该条。

🔴 模型渠道是硬约束(§2):**官方直连** `https://api.deepseek.com`,
禁 OpenRouter / DashScope 等平台的 deepseek 代理版。复审会抽验 base_url,配错 = 打回项。
平台承担成本,零扣用户积分(billing 零改动)。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Final

from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

logger = logging.getLogger("GEO-ArticleAIReview")

# ========== 模型渠道(§2 硬约束) ==========
#: 🔴 官方直连,写死。锁 8 会断言运行时用的就是这个 host —— 换成任何代理版都转红。
DEEPSEEK_BASE_URL: Final = "https://api.deepseek.com"
DEEPSEEK_CHAT_PATH: Final = "/v1/chat/completions"
# ═══════════════════════════════════════════════════════════════════
# 🔴 [WO_206 c1e] 这里有**一个名字扮两个角色**,必须拆开:
#
#   ① **发出去的模型名** —— 请求体里的 `"model"`。官方 2026-09-13 改名,
#      它得跟着改,否则思考开关不认新名(默认开思考,慢约 17% 贵约 35%)、
#      key 池掉兜底档、计价落通用价。
#   ② **reviewer 身份串** —— `AI_REVIEWER` 进的是**幂等键**:
#      `_SELECT_SQL` 按它查"这篇文章这版内容已经被谁评过",
#      写回时也按它留痕。改它 = 换了一个 reviewer = **存量文章全部重评**。
#
#   原来两者共用 `AI_MODEL` 一个字面量,于是「跟着官方改名」与
#   「别动幂等键」直接打架 —— c1b/c1c 因此把这一行整个标成 later 绕过去了。
#   拆开之后两件事各走各的:发出名取常量,身份串**逐字节冻住**。
# ═══════════════════════════════════════════════════════════════════
AI_MODEL: Final = DEEPSEEK_OFFICIAL_FLASH

#: prompt / 判定口径的版本号。**换 prompt 必须改这里** —— reviewer 串里带着它,
#: 改了就等于换了 reviewer,幂等键随之变化 → 存量会被重新评估,旧结论留痕不删。
AI_PROMPT_VERSION: Final = "v1-2026-08-01"

#: 🔴 **身份串,不是模型名**。里面那个 `deepseek-chat` 是 2026-08-01 起
#: 落库的历史标签,**逐字节不许动** —— 它是幂等键的一部分,
#: 动一个字符就等于宣布"换了个 reviewer",存量文章会被全部重评。
#: 🔴 所以这里**故意不用** `AI_MODEL`:两者今天不同值,而且本来就该不同 ——
#:    一个回答"发给谁",一个回答"这条结论是谁下的"。
#:    什么时候该重评(比如换了模型确实要重评),那是一次**迁移**,要单独定口径。
AI_REVIEWER: Final = "ai:deepseek-chat@" + AI_PROMPT_VERSION

AI_TIMEOUT_SECONDS: Final = 45

# ========== 结论分层(Owner 08-01:L1 自动 / L2 span 修复 / L3 转人工) ==========
LEVEL_AUTO_PASS: Final = "L1"
LEVEL_AUTO_REPAIR: Final = "L2"
LEVEL_HUMAN: Final = "L3"
#: 🔴 AI 失败的诚实态。它**不是**"通过",也**不是**"拦住";它是"没核查过"。
LEVEL_NOT_CHECKED: Final = "not_checked"

VALID_LEVELS: Final = frozenset(
    {LEVEL_AUTO_PASS, LEVEL_AUTO_REPAIR, LEVEL_HUMAN, LEVEL_NOT_CHECKED}
)

_PROMPT: Final = """你是内容合规与事实一致性审核员。审核下面这篇中文文章,给出结论。

判定分三档:
- L1:没有需要处理的问题,可直接发布。
- L2:有问题,但都是措辞层面、可以局部改写解决(如绝对化用语、夸大表述)。
- L3:有问题且需要人来判断(如事实/数据存疑、引用与正文对不上、涉及医疗法律金融的实质主张)。

只输出 JSON,格式:
{{"level":"L1|L2|L3","summary":"一句话结论","findings":[{{"trigger":"具体触发了什么(必须指出原文里的哪句话或哪个数据)","suggestion":"怎么改"}}]}}

🔴 findings 里每条的 trigger 必须具体到原文的某句话/某个数据。
禁止输出"存在风险""缺少证据""内容质量不高"这类没有落点的话——那等于什么都没说。
L1 时 findings 为空数组。

文章标题:{title}
文章正文:
{content}
"""


def _deepseek_key() -> str:
    """取 DeepSeek key(优先 key 池轮询 · 回落 env)。

    与 services/domain_authority_ai.py / config/model_config.py 同款逻辑 ——
    刻意复用而不是新写一套取 key 的路子(本仓已有三处,再加第四处只会更难收口)。
    """
    try:
        from services.llm.deepseek_key_pool import pick_deepseek_api_key

        k = pick_deepseek_api_key("realtime")
        if k:
            return k
    except Exception:
        pass
    return os.getenv("DEEPSEEK_API_KEY", "").strip()


def content_hash(content: str) -> str:
    """幂等键的正文侧。与发布门 `evaluate_publication_eligibility` 同款 sha256,
    两处必须同源,否则"同一篇正文"在两个模块里会算出两个 hash。"""
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _normalize_findings(raw: Any) -> list[dict[str, str]]:
    """只保留带**具体触发原因**的 finding(§3D)。

    🔴 丢弃无 trigger 的条目是刻意的:留着它们等于把"高风险"这种话原样透给客户,
    而那正是本单点名要消灭的形态。宁可少一条,不可糊一条。
    """
    out: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        trigger = str(item.get("trigger") or "").strip()
        if not trigger:
            continue
        out.append({
            "trigger": trigger,
            "suggestion": str(item.get("suggestion") or "").strip(),
        })
    return out


def _parse_ai_json(text: str) -> dict[str, Any]:
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


def _post_chat(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    """真正发出 HTTP 的唯一一处 —— 也是测试**唯一该替换的接缝**。

    🔴 存在的理由是一次实测事故:锁 7/8 最初直接 `monkeypatch.setattr(httpx, "Client", ...)`
    改全局。单跑我这个文件、或不带我这个文件跑别人,都全绿;可一旦我的文件排在
    `test_c4_review_autopilot`(其 module fixture 会 `import server`)之前,
    c4 就整片 24 个 error(`I/O operation on closed file`)——
    **改全局第三方符号的副作用会漏到别的测试文件去**,而且只在特定顺序下暴露。
    留一个自家接缝,测试换掉的就只是本模块的一个函数,炸不到别人。
    """
    import httpx

    with httpx.Client(timeout=AI_TIMEOUT_SECONDS) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()


def evaluate_article_content(title: str, content: str) -> dict[str, Any]:
    """调官方 DeepSeek 评估一篇文章 → {level, summary, findings}。

    **任何失败都返回 not_checked,不抛** —— 上层是批处理与前端核查入口,
    抛异常会让整批中断或让核查按钮报错,两者都比"这篇没核过"更糟。
    """
    api_key = _deepseek_key()
    if not api_key:
        logger.warning("[ai-review] 无可用 DEEPSEEK_API_KEY · fail-closed 停 not_checked")
        return {"level": LEVEL_NOT_CHECKED, "summary": "AI 核查未执行(缺少可用密钥)", "findings": []}

    try:
        payload = {
            "model": AI_MODEL,
            "messages": [{
                "role": "user",
                "content": _PROMPT.format(title=str(title or ""), content=str(content or "")[:20000]),
            }],
            # 低温:同一篇正文两次评估结论要稳(与 7 天价格锁同一个稳定性思路)
            "temperature": 0.1,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        data = _post_chat(DEEPSEEK_BASE_URL + DEEPSEEK_CHAT_PATH, headers, payload)
        content_out = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    except Exception as exc:
        logger.warning("[ai-review] 调用失败 · fail-closed 停 not_checked: %s", exc)
        return {"level": LEVEL_NOT_CHECKED, "summary": "AI 核查未完成(服务暂时不可用)", "findings": []}

    parsed = _parse_ai_json(content_out)
    level = str(parsed.get("level") or "").strip().upper()
    if level not in {LEVEL_AUTO_PASS, LEVEL_AUTO_REPAIR, LEVEL_HUMAN}:
        # 🔴 模型吐了个看不懂的档 = 没拿到有效结论,同样走 not_checked。
        # 绝不"猜一个 L1" —— 那是把解析失败伪装成审核通过。
        logger.warning("[ai-review] 结论档位无法识别(%r) · fail-closed 停 not_checked", level)
        return {"level": LEVEL_NOT_CHECKED, "summary": "AI 核查结论无法解析", "findings": []}

    findings = _normalize_findings(parsed.get("findings"))
    if level in {LEVEL_AUTO_REPAIR, LEVEL_HUMAN} and not findings:
        # 说有问题却给不出具体触发点 = 正是 §3D 要消灭的笼统文案。
        # 不冒充通过,也不把空话透给客户 → not_checked,让它被重跑。
        logger.warning("[ai-review] level=%s 但无带 trigger 的 finding · 视为未核查", level)
        return {"level": LEVEL_NOT_CHECKED, "summary": "AI 核查结论缺少具体触发原因", "findings": []}

    return {
        "level": level,
        "summary": str(parsed.get("summary") or "").strip(),
        "findings": findings,
    }


# ========== 落库(幂等 by 正文 hash) ==========

_SELECT_SQL: Final = """
    SELECT id, level, summary, findings, created_at
      FROM geo_article_ai_review_conclusions
     WHERE article_id = %s AND content_hash = %s AND reviewer = %s
     LIMIT 1
"""

_INSERT_SQL: Final = """
    INSERT INTO geo_article_ai_review_conclusions
        (article_id, reviewer, content_hash, level, summary, findings)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (article_id, content_hash, reviewer) DO NOTHING
    RETURNING id
"""


def get_existing_conclusion(cursor, article_id: int, chash: str) -> dict[str, Any] | None:
    cursor.execute(_SELECT_SQL, (int(article_id), chash, AI_REVIEWER))
    row = cursor.fetchone()
    return dict(row) if row else None


def review_article(cursor, article_id: int, *, title: str, content: str,
                   force: bool = False) -> dict[str, Any]:
    """评估一篇并落库。**幂等**:同 hash 已有结论则直接返回,不调模型不落行。

    返回 {level, summary, findings, reused: bool, persisted: bool}。
    `reused=True` 就是幂等生效的可观测信号 —— 锁 8 靠它断言"二跑零新增调用"。
    """
    chash = content_hash(content)
    if not force:
        existing = get_existing_conclusion(cursor, article_id, chash)
        if existing:
            return {
                "level": existing["level"],
                "summary": existing.get("summary") or "",
                "findings": existing.get("findings") or [],
                "reused": True,
                "persisted": False,
            }

    verdict = evaluate_article_content(title, content)

    # 🔴 not_checked 也落库:否则"这篇为什么没结论"永远查不出来,
    # 下次批跑还会再调一次模型(既费钱又永远收敛不了)。
    from psycopg2.extras import Json

    cursor.execute(_INSERT_SQL, (
        int(article_id), AI_REVIEWER, chash, verdict["level"],
        verdict["summary"], Json(verdict["findings"]),
    ))
    persisted = cursor.fetchone() is not None
    return {**verdict, "reused": False, "persisted": persisted}
