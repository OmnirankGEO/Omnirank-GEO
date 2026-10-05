"""
GEO 助手 · 纯问答 API(Phase 1 · 2026-05-25)

POST /api/xiaobang/chat (SSE) — 用户提问 · 流式返回答案 + 知识库出处 + 跳转建议

执行流(对比旧 /api/agent/chat):
  · 不调工具 · 不扣费 · 不写业务库
  · 预设答案命中 → 直接返回(不走 LLM)
  · BM25 检索 top-3 chunk → 拼 context → DeepSeek V4 Flash 生成
  · 检索分数太低 → 软拒 + 引导问题

SSE 事件:
  : ready                      // header flush
  event: text                  // { delta: "..." }
  event: meta                  // { sources: [...], link: {...}, confidence: "high"|"medium"|"low" }
  event: error                 // { message: "..." }
  event: done                  // {}
"""

import asyncio
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import httpx
from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from db.kb_db import load_all_chunks
from agents.xiaobang_presets import (
    match_preset, SOFT_REJECT_TEMPLATE, XiaobangPreset,
    render_preset_answer, render_soft_reject,
    _DEFAULT_ASSISTANT, _DEFAULT_BRAND,
)
from agents.xiaobang_canned_faq import match_canned_faq, XiaobangCannedFAQ

logger = logging.getLogger("Xiaobang-API")
router = APIRouter(tags=["GEO 助手"])


def _resolve_agent_branding(request: Request) -> tuple[str, str]:
    """v3.6 白标:取当前代理的助手名 / 品牌名(surface='agent' · 仅 OEM 出代理品牌)。

    返回 (assistant_name, brand_name);非 OEM / 解析失败回退平台默认(向后兼容)。
    """
    try:
        from services.public_whitelabel import resolve_branding_context
        user = getattr(request.state, "user", None) or {}
        uid = user.get("user_id") or user.get("id")
        if not uid:
            return _DEFAULT_ASSISTANT, _DEFAULT_BRAND
        ctx = resolve_branding_context(surface="agent", owner_user_id=_branding_principal(request, uid))
        if ctx.get("source") == "platform_default":
            return _DEFAULT_ASSISTANT, _DEFAULT_BRAND
        brand = ctx.get("brand") or {}
        assistant = (brand.get("product_name") or "").strip() or _DEFAULT_ASSISTANT
        brand_name = (brand.get("company_name") or "").strip() or _DEFAULT_BRAND
        return assistant, brand_name
    except Exception as exc:
        logger.warning("[xiaobang] resolve agent branding failed: %s", exc)
        return _DEFAULT_ASSISTANT, _DEFAULT_BRAND


# ==========================================
# 配置
# ==========================================

XIAOBANG_MODEL = os.getenv("XIAOBANG_MODEL", DEEPSEEK_OFFICIAL_FLASH)
DEEPSEEK_API_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/") + "/chat/completions"
XIAOBANG_IMAGE_MAX_BYTES = int(os.getenv("XIAOBANG_IMAGE_MAX_BYTES", str(10 * 1024 * 1024)))
XIAOBANG_IMAGE_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}


def _resolve_deepseek_key() -> str:
    """复用项目其他模块的 key 优先级(realtime/orchestrator/fallback DEEPSEEK_API_KEY)"""
    for name in ("DEEPSEEK_API_KEY_REALTIME", "DEEPSEEK_API_KEY_ORCHESTRATOR", "DEEPSEEK_API_KEY"):
        v = os.getenv(name, "").strip()
        if v:
            return v
    return ""

# BM25 检索分数阈值 · 低于此分认为"非业务问题"走软拒
BM25_THRESHOLD = float(os.getenv("XIAOBANG_BM25_THRESHOLD", "1.0"))
# 强软拒阈值:命中分数在 [BM25_THRESHOLD, STRONG_THRESHOLD] 之间且查询没业务关键词 → 软拒
# 用来防 "帮我写 Python 代码 / 北京到上海多远" 这种"非业务但偶然命中 idf 高词"的查询
STRONG_THRESHOLD = float(os.getenv("XIAOBANG_STRONG_THRESHOLD", "6.0"))
# 反问澄清阈值:top1 / top2 分差小于此值 且来自不同文档 → 触发反问而不是猜
# 2.0 是经验值:
#   - "我的客户怎么管理" (客户线索 vs 我的客户) gap=1.09 → 触发(用户真有歧义)
#   - "怎么发起诊断" (FAQ 诊断 vs 客户档案) gap=2.60 → 不触发(top1 明显胜出)
#   - "怎么改密码" (密码 FAQ vs 个人设置) gap=9.70 → 不触发
CLARIFY_GAP = float(os.getenv("XIAOBANG_CLARIFY_GAP", "2.0"))
# hybrid 加权 · 最终分 = BM25 * BM25_WEIGHT + cosine * COSINE_WEIGHT * COSINE_SCALE
# cosine ∈ [0,1] · BM25 一般在 [0,20] · COSINE_SCALE 把 cosine 拉到跟 BM25 同量级
BM25_WEIGHT = float(os.getenv("XIAOBANG_BM25_WEIGHT", "0.5"))
COSINE_WEIGHT = float(os.getenv("XIAOBANG_COSINE_WEIGHT", "0.5"))
COSINE_SCALE = float(os.getenv("XIAOBANG_COSINE_SCALE", "12.0"))
# 合并排序加权:current_page 同页 chunk ×ROUTE_MATCH_MULT(用户大概率问当前页);
# 直答型 chunk 按类型加权(sys_qa/button/error 最适合直接回答用户)。
ROUTE_MATCH_MULT = float(os.getenv("XIAOBANG_ROUTE_MATCH_MULT", "1.5"))
_TYPE_WEIGHT = {
    "sys_qa": float(os.getenv("XIAOBANG_W_SYS_QA", "1.3")),
    "sys_button": float(os.getenv("XIAOBANG_W_SYS_BUTTON", "1.25")),
    "sys_error": float(os.getenv("XIAOBANG_W_SYS_ERROR", "1.25")),
    "sys_field": float(os.getenv("XIAOBANG_W_SYS_FIELD", "1.1")),
}
# 截图抽取的辅助文字(按钮名/字段名/报错文案/状态/步骤)并入 query 的长度上限。
# ⚠️ 调用方只能传抽取后的短文本,不要塞整段 OCR;页面身份仍以 current_page 为准。
AUX_TEXT_MAX = int(os.getenv("XIAOBANG_AUX_TEXT_MAX", "200"))
# 低置信兜底:命中了 route 页面卡,但 BM25/embedding 最高内容分仍低于此 →
# 不硬答,承认"能看到你大概在这页,但这块知识库说明不够"并接人工。
ROUTE_ONLY_MIN_CONTENT = float(os.getenv("XIAOBANG_ROUTE_ONLY_MIN_CONTENT", "1.0"))

# 业务关键词白名单(出现任一即视为业务相关 · 跳过强软拒过滤)
# 这些词在 KB 里高频出现,而且对应 OmniRank 系统的具体功能/概念
_BUSINESS_KEYWORDS = set("""
诊断 体检 报告 报价 客户 关键词 选词 签约 收款 文章 写作 写文章 发布 投放
监测 排名 引用 引擎 通义 豆包 deepseek kimi
积分 钱包 充值 提现 佣金 推荐 扣费 退款 余额 计费 收费 多少钱 定价
账号 设置 个人 团队 角色 审计 权限 登录 注册 密码 手机
品牌 行业 城市 知识库 资料 档案 评分 分数 等级 总分
geo seo ai 大模型 助手
代理 客服 反馈 帮助
""".lower().split())


def _has_business_keyword(query: str) -> bool:
    """检测查询是否含业务关键词 · 用 substring 匹配(中文 jieba 不一定切好)"""
    q = query.lower()
    for kw in _BUSINESS_KEYWORDS:
        if kw in q:
            return True
    return False


# 2026-05-26 review · "老板是谁" 漏判修法
# 即使 LLM system prompt 要求只用 KB 内容,LLM 偶尔仍会"编出一段听起来合理的"答案
# 来解释 KB 里的边角词(《什么是 GEO·为啥要做》里 example 用了"老板想找..."),
# 后置 NO_ANSWER 降级靠 LLM 自陈"没找到",一旦 LLM 编了答案就绕过。
# 前置硬规则:"X 是谁/谁是 X/X 怎么样" 类抽象问句 + X 是明显非业务实体 → 软拒
_NON_BUSINESS_ENTITIES = (
    "老板", "老总", "经理", "员工", "同事", "朋友",
    "家人", "亲戚", "孩子", "妈妈", "爸爸", "妻子", "丈夫",
    "ceo", "cto", "cfo",
)
_ABSTRACT_QUESTION_PATTERNS = ("是谁", "谁是", "怎么样", "啥样", "什么样")


def _is_non_business_question(query: str) -> bool:
    """检测是否泛抽象问句(主体非业务实体)→ 该走软拒不该走 RAG"""
    if not query:
        return False
    q = query.lower()
    if not any(p in q for p in _ABSTRACT_QUESTION_PATTERNS):
        return False
    return any(e in q for e in _NON_BUSINESS_ENTITIES)


# 普通用户不能获取代理/服务方的经营模型信息。
# 这层是检索前硬闸:即使 FAQ/帮助文档未来误混了边缘词,也不让 LLM 拼出代理赚钱方式。
_NORMAL_USER_AGENT_BUSINESS_PATTERNS = (
    "代理怎么赚钱", "代理如何赚钱", "代理靠什么赚钱", "代理赚什么钱",
    "代理利润", "代理盈利", "代理收益", "代理收入", "代理佣金",
    "代理提现", "代理结算", "代理等级", "代理进货", "代理库存",
    "服务方怎么赚钱", "服务方如何赚钱", "服务方利润", "服务方收益",
    "出厂价", "进货价", "报价系数", "加价倍率", "毛利", "差价",
    "客户归属", "白标", "渠道服务费", "服务费流水",
)


def _is_normal_user_agent_business_question(query: str) -> bool:
    """普通用户询问代理经营/利润/成本/分账等信息 → 前置拒答。"""
    if not query:
        return False
    q = query.lower().replace(" ", "")
    return any(p.lower().replace(" ", "") in q for p in _NORMAL_USER_AGENT_BUSINESS_PATTERNS)


def render_normal_user_agent_business_reject() -> str:
    """普通用户问代理经营规则时的安全回复。"""
    return (
        "这个属于服务方的经营规则,不是普通用户需要了解的内容,我不能展开。\n\n"
        "你作为普通用户只需要关注这些:\n"
        # [R3-P7 ①] 原文三处是「工具额度 / 到账额度」—— 裁定 ② 域的死池名
        # (services/kb_terminology_gate.POOL_NAMES)。这段是**小榜真说给用户的话**,
        # 说一个屏幕上不存在的词,用户就照着找不到入口。
        "- 自己有多少算力\n"
        "- 购买算力页面显示的套餐和到账算力\n"
        "- 推荐有礼里你自己能拿到的返利规则\n\n"
        "具体价格、到账算力和返利结果,都以你页面上显示为准。"
    )


# 2026-05-26 review · 4 条新规则
# 用户拍板:
#  1. 明确问句默认直接答 · 不走 clarify
#  2. clarify 候选 token 重叠要求更强 · 不能靠"客户/品牌/GEO"等泛词凑
#  3. 高频业务意图加 deterministic intent map(链接卡 路由强制)
#  4. "区别/对比"类优先答比较 · 不反问成两个随机文档(归到规则 1)

# 明确问句疑问词 · 命中即视为意图明确 · 不反问
_CLEAR_QUESTION_PATTERNS = (
    "怎么", "如何", "怎样", "咋",
    "在哪", "哪里", "哪儿",
    "入口", "怎么进", "怎么去", "去哪",
    "区别", "差别", "有什么区别", "差在哪", "对比", "比较", "差异",
    "怎么给", "怎么建", "怎么加", "怎么改", "怎么发", "怎么写",
    "什么", "为什么", "为啥",
    "啥意思", "什么意思", "是啥",
    "?", "?",
)


def _is_clear_question(query: str) -> bool:
    """检测含明确疑问词 · 命中即直接答(不反问)"""
    if not query:
        return False
    q = query.lower()
    return any(p in q for p in _CLEAR_QUESTION_PATTERNS)


def _match_intent(query: str) -> tuple[str, str] | None:
    """v1 薄适配：旧调用点改由唯一 OperationRegistry 生成。"""
    from services.gap_operation_map import describe, match_operation

    entry = match_operation(query)
    if entry is None:
        return None
    signed = describe(entry)
    route = signed.get("target_route")
    return (str(route), f"去{entry.display_name}") if route else None


# 2026-05-26 F2 · 越权操作意图检测
# 用"代理前缀 + 业务动词"组合判断,覆盖面比纯短语列表更广:
#   - "你直接帮我充值"(prefix='你直接帮我' + verb='充值')✓
#   - "帮我诊断一下"  (prefix='帮我' + verb='诊断')✓
#   - "替我充值100块"(prefix='替我' + verb='充值')✓
# 排除疑问词(怎么/在哪/啥)避免误伤纯问句("帮我看看怎么扣费"是疑问不是越权)

_DELEGATE_PREFIXES = (
    "帮我", "你帮我", "替我", "你来", "你给我",
    "你直接", "你直接帮我",
    "你自己", "你自动",
)

# 业务动作动词(用户想让小榜替自己执行的)· 不含"知道/查看/翻"这种纯查询
_AUTH_VERBS = (
    "诊断", "体检",
    "扣费", "充值", "退款", "提现", "下单", "付费",
    "发起", "启动", "开始",
    "发布", "投放", "代发",
    # 2026-05-26 review · 补口语化"发"动作
    # "帮我发文章到自媒体" 此前漏判 · _AUTH_VERBS 没覆盖"发文章/发文/投稿"
    "发文章", "发文", "投稿", "发到", "推送",
    "写文章", "做文章", "跑文章",
    "做诊断", "跑诊断", "做监测", "开监测",
    "跑一次", "跑一遍", "跑一下",
    "代办", "代操作",
)

# 疑问词(命中则视为纯问句 · 不是越权)
_QUESTION_WORDS = ("怎么", "怎样", "如何", "在哪", "什么", "为什么", "为啥", "啥", "?", "?")


def _is_unauthorized_request(query: str) -> bool:
    """检测用户是否在让助手替自己执行业务操作 · 命中即不出 链接卡

    判定:
      1. query 含疑问词 → 不是越权(只是问)· 返 False
      2. query 含某个代理前缀(帮我/你帮我/替我/你直接...)
      3. 该前缀后 15 字符内含业务动词(扣费/充值/诊断/发起/...)
      → True
      同时"自动+动词" 也算越权(自动扣费/自动诊断)

    设计原则:
    - 误判代价小(只是少跳转,答案仍正常)
    - 真越权命中,我们不给 deterministic CTA · 防用户误以为继续执行
    """
    if not query:
        return False
    q = query.lower()

    # 1) 含疑问词 · 视为问句,放行
    if any(w in q for w in _QUESTION_WORDS):
        return False

    # 2) 代理前缀 + 后续动词
    for pfx in _DELEGATE_PREFIXES:
        idx = q.find(pfx)
        if idx < 0:
            continue
        # 看前缀后 15 字符窗口内有无业务动词
        window = q[idx + len(pfx): idx + len(pfx) + 15]
        if any(v in window for v in _AUTH_VERBS):
            return True

    # 3) "自动" + 动词(全局看,不要求紧邻)
    if "自动" in q and any(v in q for v in _AUTH_VERBS):
        return True

    return False


def _telemetry_actor_kind(user, identity) -> str:
    """身份**类型**(不是身份)。观测事件里绝不落 user_id。"""
    try:
        from services.gap_operation_map import actor_role
        role = actor_role(user, identity=identity)
    except Exception:
        return "unknown"
    return {"admin": "admin", "agent": "agent",
            "member": "member", "normal_user": "normal_user"}.get(role, "unknown")


def _emit_turn_observation(obs: dict) -> None:
    """把这一轮的观测底稿组装并发出。**永远不抛**(观测不许打断问答)。"""
    try:
        from services.xiaobang_telemetry import build_turn_event, emit
        from db.kb_db import load_release_manifest
        try:
            _m = load_release_manifest() or {}
        except Exception:
            _m = {}
        exits = obs.get("exits") or []
        emit(build_turn_event(
            session_id=obs.get("session_id"),
            request_id=obs.get("request_id"),
            actor_kind=obs.get("actor_kind", "unknown"),
            current_route=obs.get("current_route", ""),
            intent_kind=obs.get("intent_kind", "unknown"),
            deterministic_hit=obs.get("deterministic_hit", False),
            knowledge_manifest_version=_m.get("content_version"),
            answer_state=obs.get("answer_state", "unknown"),
            exits=exits,
            repeat_fallback=obs.get("repeat_fallback", False),
            help_center_only=obs.get("help_center_only", False),
            error_kind=obs.get("error_kind", ""),
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[xiaobang] 观测事件组装失败(不影响问答): %s", type(exc).__name__)


def _should_short_circuit_canned(req) -> bool:
    """确定性答案(preset / canned)这一轮能不能短路返回。

    🔴 [包 B② 2026-08-21] 规则**换掉了**,工单 §4 P1-1:

    旧规则:``not current_page and not attachment_text``
      —— 抽屉每次请求都带 ``current_page``(`useXiaobangChat` 恒传),
      于是真实抽屉里 preset/canned **基本等于被全局关掉**:
      「你除了帮助中心还能做什么」命不中 `what_can_you_do`,
      同一句话在 API 直调与抽屉里得到两种结果(而判据一直打的是直调那条)。
      这是把「页面提示」误当成「用户意图」。

    新规则:页面上下文**只在用户真的在指页面时**才压过确定性答案。
      判据落在 `services/xiaobang_question_intent.py`(唯一谓词,判结构不判词表)。
      ``current_page`` 保留它原本就有的**检索加权**作用(`ROUTE_MATCH_MULT`,没动),
      只是不再当一道全局闸。
    """
    from services.xiaobang_question_intent import deterministic_answer_allowed

    return deterministic_answer_allowed(
        (getattr(req, "message", "") or ""),
        current_page=(getattr(req, "current_page", "") or "").strip(),
        attachment_text=(getattr(req, "attachment_text", None) or "").strip(),
    )


def _with_operation(link: dict) -> dict:
    """[WO-B2 ① 2026-08-20] 给 链接卡 补上 ``operation_id`` —— 深链 producer 的**唯一入口**。

    ## 这一格解决的是什么

    在这之前,全仓**没有任何地方**构造 ``?xiaobang_intent=``:
    三个页面的消费方(WO-B ① 接的)全都只能靠手拼 URL 才到得了。
    卡片带上 ``operation_id`` 之后,前端才知道"这一跳可以先 prepare 一个 intent 再去",
    于是深链有了 producer。

    🔴 **只在这个路由背后真有一条可 prepare 的 operation 时才带**。
       映射从注册表机械派生(``commandable_operation_for_route``),不手写:
       手写的映射在下一次加/删合同时会悄悄漂,而漂的表现是"这张卡不再签发深链",
       没有任何东西会变红。
    🔴 拿不到就**原样返回**(只有 route/label)—— 前端那一支是老行为,逐字节不变。
       给一个没有合同的 route 签发深链 = 给用户一个必然 404 的链接
       (``_resolve_entry`` 对无合同一律 404)。
    """
    from services.gap_operation_map import commandable_operation_for_route

    operation_id = commandable_operation_for_route(link.get("route") or "")
    if operation_id:
        link["operation_id"] = operation_id
    return link


def _pick_link(
    results: list[tuple[dict, float]],
    query_tokens: set[str],
    query_text: str = "",
) -> dict | None:
    """F1 · 链接卡 路由二次判定 + 2026-05-26 intent map 优先

    策略(优先级从高到低):
      1. **Intent map** 命中(eg query 含 "积分/扣费" → /wallet)→ 直接用 intent route
         deterministic · 不被 BM25 抖动影响
      2. **BM25 + title boost** 综合排序(fallback):
         - title_match desc(source_title 含 query token 数多的优先)
         - source_type doc=0 / faq=1(同 title_match 时 doc 优先)
         - BM25 原排序

    设计:
    - intent map 治高频意图(报价/钱包/客户/诊断等)· 准且稳
    - BM25 兜底其他长尾意图
    """
    # 1) Intent map 优先(deterministic)
    if query_text:
        intent = _match_intent(query_text)
        if intent:
            return _with_operation({"route": intent[0], "label": intent[1]})

    if not results:
        return None

    # 2) BM25 + title boost 综合排序(fallback)
    def title_match(c: dict) -> int:
        title = (c.get("source_title") or "").lower()
        return sum(1 for tok in query_tokens if tok and tok in title)

    candidates: list[tuple[int, int, int, dict]] = []  # (-hits, type_rank, idx, chunk)
    for idx, (c, _s) in enumerate(results):
        if not c.get("route"):
            continue
        hits = title_match(c)
        type_rank = 0 if c.get("source_type") == "doc" else 1
        candidates.append((-hits, type_rank, idx, c))

    if not candidates:
        return None

    candidates.sort()
    best = candidates[0][3]
    return _with_operation({"route": best["route"],
                            "label": best.get("route_label") or "去看"})


def _should_soft_reject(
    results: list,
    top_score: float,
    has_biz_kw: bool,
    has_page_card: bool,
) -> bool:
    """命中当前页面卡时一律不软拒（页面卡是保底上下文，命中就给）。"""
    if has_page_card:
        return False
    return (
        not results
        or top_score < BM25_THRESHOLD
        or (top_score < STRONG_THRESHOLD and not has_biz_kw)
    )


def _is_route_only_insufficient(page_card: dict | None, results: list, top_score: float) -> bool:
    """低置信兜底(检索层判断):命中了 route 页面卡,但 BM25/embedding 最高内容分
    仍低于 ROUTE_ONLY_MIN_CONTENT → 不靠单薄页面卡硬答,承认页面但知识不足、接人工。"""
    return bool(page_card) and (not results or top_score < ROUTE_ONLY_MIN_CONTENT)


def _clip_attachment_text(text: str | None) -> str:
    """截图抽取的辅助文字进 query(BM25 + embedding)前**统一截断**到 AUX_TEXT_MAX,
    防前端误传整段 OCR 污染向量/检索。embedding 与 BM25 必须吃同一份截断结果。"""
    return (text or "").strip()[:AUX_TEXT_MAX]


def _maybe_clarify(results: list[tuple[dict, float]], query: str) -> list[dict] | None:
    """检测是否需要反问澄清 · 命中返回 [option, ...] 否则 None

    2026-05-26 review · 4 条加固规则:
      规则 1 · 明确问句不反问(_is_clear_question 命中 → 直接 None)
      规则 2 · 候选 title 重叠要求加严(≥ 2 个 token 或 title 整词在 query 里)
      F3 · 路由主导规则(top-K 某 route 占 >= 50% → 不反问)
      F3 · 同 route 候选合并
    """
    if len(results) < 2:
        return None

    # 规则 1 · 明确问句一律不反问 · 让 LLM 看 top3 综合答
    # "怎么/如何/在哪/区别/差别/对比" 等疑问词命中 → 用户意图清晰 · 反问只会打断
    if _is_clear_question(query):
        return None

    c1, s1 = results[0]
    c2, s2 = results[1]
    if s1 < BM25_THRESHOLD or (s1 - s2) >= CLARIFY_GAP:
        return None
    if c1.get("source_slug") == c2.get("source_slug"):
        return None  # 同一文档的不同 section 没必要反问

    # F3 · 路由主导规则
    # top-K 里某个 route 占多数 → 用户主意图明确 · 不反问
    route_list = [c.get("route") for c, _ in results[:5] if c.get("route")]
    if route_list:
        from collections import Counter
        top_route, top_count = Counter(route_list).most_common(1)[0]
        if top_count >= 2 and top_count / len(route_list) >= 0.5:
            return None

    query_tokens = set(tokenize(query))

    def title_match_count(c: dict) -> int:
        """source_title 含的 query token 数(用于反问候选过滤 · 不含 section)"""
        if not query_tokens:
            return 0
        title = (c.get("source_title") or "").lower()
        return sum(1 for tok in query_tokens if tok in title)

    def is_strong_candidate(c: dict) -> bool:
        """规则 2 · 反问候选 token 重叠加严
        要求:
          a. source_title 含 >= 2 个 query token,或者
          b. source_title 整体作为子串在 query 里(完全相关)
        防"客户/品牌/GEO"等单个泛词凑成反问候选(_pick_link 现有的 title_match=1 太松)
        """
        if not query_tokens:
            return True
        title = (c.get("source_title") or "").lower()
        if title_match_count(c) >= 2:
            return True
        # source_title 整体作为 query 子串
        if title and len(title) >= 2 and title in query.lower():
            return True
        return False

    def build_option(c: dict) -> dict:
        title = c.get("source_title") or ""
        section = c.get("section_title") or ""
        if section and len(section) <= 16:
            label = f"问《{title}》· {section}"
        else:
            label = f"问《{title}》"
        section_clean = section if section and len(section) <= 30 else ""
        query_text = f"{title}{(' · ' + section_clean) if section_clean else ''} 怎么用"
        return {
            "label": label,
            "query": query_text,
            "source_slug": c.get("source_slug") or "",
            "source_title": title,
            "section_title": section,
            "source_type": c.get("source_type") or "doc",
        }

    # 收候选 · 同 slug / 同 route 各只留最高分一个 + 规则 2 严过滤
    candidates: list[tuple[dict, float]] = []
    seen_slugs: set[str] = set()
    seen_routes: set[str] = set()
    for c, s in results[:5]:
        slug = c.get("source_slug") or ""
        route = c.get("route") or ""
        if slug in seen_slugs:
            continue
        if route and route in seen_routes:
            continue
        if not is_strong_candidate(c):
            continue
        seen_slugs.add(slug)
        if route:
            seen_routes.add(route)
        candidates.append((c, s))
        if len(candidates) >= 3:
            break

    if len(candidates) < 2:
        return None

    return [build_option(c) for c, _s in candidates]

# top-K chunk 拼 context · 太多 LLM 容易胡说,太少召回不全
TOP_K = 5

# 用户消息长度上限(防 prompt injection 灌大文本)
MAX_MSG_LEN = 500


# ==========================================
# In-memory KB cache(进程级缓存)
# ==========================================

_CHUNKS_CACHE: dict[str, Any] = {
    "non_admin": [],    # is_admin_only = FALSE（任意 visible_to · 向后兼容默认池）
    "all": [],          # 全部(管理员能看到管理员节点 · 不按 visible_to 过滤)
    "agent": [],        # 非管理 + visible_to ∈ (agent, both) —— 代理 KB
    "normal_user": [],  # 非管理 + visible_to ∈ (normal_user, both) —— 普通用户 KB
    "loaded_at": 0,
    "avgdl": 0.0,
    "doc_count": 0,
}
_CACHE_TTL_SEC = 300  # 5 分钟刷一次(管理员重建索引后这么久内会生效)


def _select_pool(is_admin: bool, identity: str | None):
    """按身份选检索池（两套知识库 RBAC 核心）。

    - is_admin=True            → all（内部员工看全部）
    - identity='agent'         → agent 池（visible_to ∈ agent/both）
    - identity='normal_user'   → normal_user 池（visible_to ∈ normal_user/both）
    - identity=None（老调用/测试）→ non_admin（全非管理，向后兼容）
    """
    if is_admin:
        return _CHUNKS_CACHE["all"]
    if identity == "agent":
        return _CHUNKS_CACHE["agent"]
    if identity == "normal_user":
        return _CHUNKS_CACHE["normal_user"]
    return _CHUNKS_CACHE["non_admin"]


# ==========================================
# 检索身份判定（两套知识库 · 代理 vs 普通用户）
# ==========================================

# [GEO-R10-CAN-026] 缓存三元组：user_id -> (ts, level, perm_version)。
# 绑定 permission_version 后，角色降级(agent→0，会 bump users.permission_version)会让缓存版本
# 失配 → 强制回库，杜绝"降级后仍命中代理池"的越权窗口。TTL 从 300s 收紧到 60s(与 perm_cache 对齐)
# 兜底那些暂未 bump 版本的变更路径的残余窗口。
_AGENT_LEVEL_CACHE: dict[int, tuple[float, int, int]] = {}
_AGENT_LEVEL_TTL_SEC = 60


def invalidate_agent_level_cache(user_id: int | None = None) -> None:
    """[GEO-R10-CAN-026] 立即失效 agent_level 进程缓存。

    供角色变更方(partner_api 主动退代理 / admin 改 agent_level)在 mutation 后调用，
    保证降级即时生效、不给 stale 代理身份留 KB 越权窗口。不传 user_id 则清空全部。
    """
    if user_id is None:
        _AGENT_LEVEL_CACHE.clear()
    else:
        _AGENT_LEVEL_CACHE.pop(user_id, None)


def _current_perm_version(user_id: int | None) -> int:
    """[GEO-R10-CAN-026] 读当前 permission_version(角色/权限变更即 +1)。

    失败返 -1 —— 与任何真实版本必不相等 → 视为版本变化强制回库(fail-closed，
    宁可多打一次库也绝不用可能过期的代理身份。)
    """
    if not user_id:
        return -1
    try:
        from auth.perm_cache import get_cached_permission_version
        return int(get_cached_permission_version(user_id))
    except Exception as e:
        logger.debug("[xiaobang] 读取 permission_version 失败 user=%s err=%s（强制回库）", user_id, e)
        return -1


def _fetch_agent_level(user_id: int | None) -> int:
    """查 user_wallets.agent_level（权威源）· 带进程内 TTL + 权限版本双重校验的缓存，避免每次问答打库。"""
    if not user_id:
        return 0
    now = time.time()
    # [GEO-R10-CAN-026] 命中前先取当前权限版本，缓存必须同时满足"未过期"且"版本未变"
    cur_ver = _current_perm_version(user_id)
    cached = _AGENT_LEVEL_CACHE.get(user_id)
    if cached and now - cached[0] < _AGENT_LEVEL_TTL_SEC and cached[2] == cur_ver:
        return cached[1]
    level = 0
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT agent_level FROM user_wallets WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if row:
                raw = row.get("agent_level") if isinstance(row, dict) else row[0]
                level = int(raw or 0)
        finally:
            conn.close()
    except Exception as e:
        # 失败降级按普通用户处理（fail-safe：宁可代理少看到内容，也绝不让普通用户误入代理池）
        logger.warning("[xiaobang] 读取 agent_level 失败 user=%s err=%s（降级按普通用户）", user_id, e)
        level = 0
    # [GEO-R10-CAN-026] 落缓存时一并记录取数时的权限版本
    _AGENT_LEVEL_CACHE[user_id] = (now, level, cur_ver)
    return level


def _resolve_kb_identity(user: dict, organization_identity: Any = None) -> tuple[bool, str]:
    """兼容旧调用的知识池薄适配；新请求只调用 ``_resolve_request_identity`` 一次。"""
    resolved = _resolve_request_identity(user, organization_identity)
    return resolved.is_admin, resolved.knowledge_identity


@dataclass(frozen=True)
class _ResolvedXiaobangIdentity:
    """一次请求共用的知识与动作身份，不把钱包字段写回 JWT 用户对象。"""

    is_admin: bool
    knowledge_identity: str
    operation_identity: Any
    source: str


def _resolve_request_identity(
    user: Mapping[str, Any], organization_identity: Any = None,
) -> _ResolvedXiaobangIdentity:
    """按管理员 → 组织身份 → 钱包等级解析一次，供所有回答分支共用。

    组织员工/Owner 的权威源是 OrganizationGuard 每请求重验后注入的
    ``organization_identity``；只有非组织账号才回退 user_wallets.agent_level。
    员工先进入服务侧语义池，随后每个 chunk 再按 OperationRegistry capability
    过滤，不能把 ``agent_level=0`` 的员工误当普通用户，也不能让员工读完整代理池。
    """
    if bool(user.get("is_admin")):
        return _ResolvedXiaobangIdentity(True, "admin", "admin", "admin")
    if getattr(organization_identity, "is_member", False):
        return _ResolvedXiaobangIdentity(
            False, "agent", organization_identity, "organization_member",
        )
    if getattr(organization_identity, "is_owner", False):
        return _ResolvedXiaobangIdentity(
            False, "agent", organization_identity, "organization_owner",
        )
    level = user.get("agent_level")
    if level is None:
        level = _fetch_agent_level(user.get("user_id"))
    role = "agent" if (level or 0) >= 1 else "normal_user"
    return _ResolvedXiaobangIdentity(False, role, role, "wallet_agent_level")


def _ensure_cache_loaded():
    """懒加载 + TTL 过期重新读

    重要:preset 类型 chunks 排除在 BM25 池外 · preset 走 match_preset() 关键词匹配
    优先返回,不参与检索。不排除的话 preset 里"诊断/报价/监测"等高频词会污染 BM25
    导致正经业务问题命中"在吗"等闲聊 preset。
    """
    now = time.time()
    if _CHUNKS_CACHE["loaded_at"] and now - _CHUNKS_CACHE["loaded_at"] < _CACHE_TTL_SEC:
        return
    raw = load_all_chunks(include_admin=True)
    # 旧 FAQ 即使仍留在 faq_items / 老 kb_chunks，也不能进入运行时召回。
    # 路径事实由 OperationRegistry 签发；这里仅淘汰已裁定失效的 IA/动态承诺。
    raw = [c for c in raw if not _is_stale_knowledge_chunk(c)]
    # 过滤掉 preset(只剩 doc + faq 参与 BM25)
    all_chunks = [c for c in raw if c.get("source_type") != "preset"]
    non_admin = [c for c in all_chunks if not c.get("is_admin_only")]
    # 两套知识库身份池：均建立在 non_admin 之上（管理员节点对客户身份永不可见）
    # visible_to 缺省视为 'both'（老 doc/faq chunk 未标 visible_to 时两身份都收）
    agent_pool = [c for c in non_admin if (c.get("visible_to") or "both") in ("agent", "both")]
    normal_user_pool = [c for c in non_admin if (c.get("visible_to") or "both") in ("normal_user", "both")]
    total_len = sum(len(c.get("token_keywords") or []) for c in all_chunks)
    avgdl = total_len / len(all_chunks) if all_chunks else 0.0
    _CHUNKS_CACHE["all"] = all_chunks
    _CHUNKS_CACHE["non_admin"] = non_admin
    _CHUNKS_CACHE["agent"] = agent_pool
    _CHUNKS_CACHE["normal_user"] = normal_user_pool
    _CHUNKS_CACHE["avgdl"] = avgdl
    _CHUNKS_CACHE["doc_count"] = len(all_chunks)
    _CHUNKS_CACHE["loaded_at"] = now
    logger.info("[xiaobang] 加载 KB 缓存 · BM25 池 %d (doc+faq · 非管理 %d / 管理 %d) · 身份池 代理 %d / 普通 %d · preset 不参与检索",
                len(all_chunks), len(non_admin), len(all_chunks) - len(non_admin),
                len(agent_pool), len(normal_user_pool))


_STALE_KNOWLEDGE_PATTERNS = (
    re.compile(r"销售\s*(?:分组|→|->)"),
    re.compile(r"运营\s*(?:分组|→|->).{0,12}(?:排名监测|效果监测)"),
    re.compile(r"制作\s*(?:→|->).{0,12}发布管理"),
    re.compile(r"AI\s*引用率约\s*\d+%", re.IGNORECASE),
    re.compile(r"固定(?:积分|引用率|评分公式)"),
)


def _is_stale_knowledge_chunk(chunk: Mapping[str, Any]) -> bool:
    text = " ".join(str(chunk.get(key) or "") for key in (
        "source_title", "section_title", "content",
    ))
    return any(pattern.search(text) for pattern in _STALE_KNOWLEDGE_PATTERNS)


# ==========================================
# 分词(运行时也要 · 跟索引时同一逻辑)
# ==========================================

_STOPWORDS = set("""
的 了 在 是 我 你 他 她 它 我们 你们 他们 这 那 这个 那个 这些 那些
和 与 跟 及 或 或者 但 但是 不过 然后 还有 还是
吗 呢 啊 哦 哈 嗯 哇 呀 哎 哼 呵
有 没 没有 不 不要 别 不能 不会 不是
的话 一下 一次 一个 一种 一些 一直 一定 一起 一样
去 来 在 把 被 给 让 让我
就是 还是 已经 还 又 再 也
比较 非常 很 太 真 真的 挺 蛮
""".split())

_TOKEN_NUM = re.compile(r"^[\d.,，。]+$")


def tokenize(text: str) -> list[str]:
    """跟 tools.xiaobang_kb_indexer.tokenize 保持一致:单字全过滤 + 域词典"""
    try:
        import jieba
    except ImportError:
        return [w for w in text.lower().split() if w and len(w) >= 2 and w not in _STOPWORDS]
    # 复用索引器的域词典加载逻辑(避免重复维护词表)
    from tools.xiaobang_kb_indexer import _ensure_jieba_dict_loaded
    _ensure_jieba_dict_loaded()
    text = re.sub(r"[^\w一-鿿]+", " ", text)
    tokens = jieba.lcut(text)
    out = []
    for t in tokens:
        t = t.strip().lower()
        if not t or len(t) < 2:
            continue
        if t in _STOPWORDS:
            continue
        if _TOKEN_NUM.match(t):
            continue
        out.append(t)
    return out


# ==========================================
# route 页面卡注入（Task 3）
# ==========================================

# 路由别名：前端 <Navigate> 重定向页 → 真实有页面卡的路由。
# 小榜 current_page 可能传重定向前的旧路由（如侧边栏「今日工作台」是 /dashboard/today），
# 不归一会导致 route 注入漏命中。来源 = frontend/src/App.tsx 的 <Navigate to=...>。
_ROUTE_ALIASES = {
    "/dashboard/today": "/dashboard",
    "/account": "/account/profile",
    "/m3/help": "/help",
}


def _canonical_route(current_page: str) -> str:
    """把重定向别名归一到真实页面卡路由（去尾部斜杠后再查别名表）。"""
    if not current_page:
        return current_page
    p = current_page.rstrip("/") or "/"
    return _ROUTE_ALIASES.get(p, p)


def _actor_can_read_route(
    route: str | None,
    *,
    user: Mapping[str, Any] | None,
    organization_identity: Any,
) -> bool:
    """真实请求身份只能读取唯一注册表允许的页面帮助。"""
    if not route:
        # 无 route 的 preset 是问候/元问题，不携带业务页面知识。
        return True
    from urllib.parse import urlsplit
    from services import gap_operation_map as operation_map

    target = _canonical_route(urlsplit(str(route)).path)
    candidates = (
        entry for entry in operation_map.all_operations()
        if _canonical_route(urlsplit(entry.route_template).path) == target
    )
    return any(
        operation_map.is_operation_allowed(
            entry, dict(user or {}), identity=organization_identity,
        )
        for entry in candidates
    )


def _actor_can_read_chunk(
    chunk: Mapping[str, Any],
    *,
    user: Mapping[str, Any] | None,
    organization_identity: Any,
) -> bool:
    """route chunk 统一按请求身份过滤；员工无落点代理散文 fail-closed。"""
    route = str(chunk.get("route") or "").strip()
    if route and user is not None:
        return _actor_can_read_route(
            route, user=user, organization_identity=organization_identity,
        )
    # 无 route 的通用知识可读；无 route 的 agent-only 内容没有能力归属，不能下发。
    if getattr(organization_identity, "is_member", False):
        return (chunk.get("visible_to") or "both") == "both"
    return True


def _inject_route_page_card(
    current_page: str,
    is_admin: bool,
    identity: str | None = None,
    *,
    user: Mapping[str, Any] | None = None,
    organization_identity: Any = None,
) -> dict | None:
    """按当前页面路由从 KB 池取 sys_page 卡片 · 严格按角色/身份选池（RBAC）

    安全边界：
      - is_admin=True             → 从 _CHUNKS_CACHE["all"] 找
      - identity='agent'          → 代理池（visible_to ∈ agent/both）
      - identity='normal_user'    → 普通用户池（visible_to ∈ normal_user/both）
      - identity=None（老调用/测试）→ non_admin（全非管理，向后兼容）
      - 绝不直接查全库 / 绕过 is_admin_only / 绕过 visible_to 过滤

    current_page 先经 _canonical_route 归一重定向别名（如 /dashboard/today → /dashboard），
    再匹配第一个 source_type=='sys_page' 且 route==归一后路由 的 chunk，否则 None。
    """
    if not current_page:
        return None
    target = _canonical_route(current_page)
    _ensure_cache_loaded()
    pool = _select_pool(is_admin, identity)
    for c in pool:
        if (
            c.get("source_type") == "sys_page"
            and c.get("route") == target
            and _actor_can_read_chunk(
                c, user=user, organization_identity=organization_identity,
            )
        ):
            return c
    return None


# ==========================================
# BM25 检索
# ==========================================

def bm25_search(
    query: str,
    is_admin: bool,
    top_k: int = TOP_K,
    query_vec: list[float] | None = None,
    identity: str | None = None,
    current_page: str | None = None,
    aux_text: str | None = None,
    user: Mapping[str, Any] | None = None,
    organization_identity: Any = None,
) -> list[tuple[dict, float]]:
    """Hybrid 检索 · BM25(精确词) + source_title boost + (可选) embedding cosine(语义)

    分数 = BM25 + title_boost(题目 token 命中加权)
          + (有 query_vec 时) cosine(query, chunk.embedding) * COSINE_SCALE * COSINE_WEIGHT
          BM25 已经隐含 BM25_WEIGHT(可调,但默认 0.5 跟 cosine 各半)
    合并后再乘:current_page 同页 ×ROUTE_MATCH_MULT、按 chunk 类型 ×_TYPE_WEIGHT(直答型优先)。

    query_vec=None 时退化为纯 BM25 + boost(DashScope embedding 不可用时的 fallback)。

    identity: 'agent' | 'normal_user' | None —— 两套知识库身份池路由（RBAC,检索前过滤）。
              None 时回退 non_admin（向后兼容老调用/测试）。
    current_page: 当前页面路由(真值);命中同 route 的 chunk 加权。
    aux_text: 截图抽取的辅助文字(按钮/字段/报错/状态/步骤,**已截短的短文本,非整段OCR**),
              并入 BM25 query 提升按钮/字段/报错召回;页面身份仍以 current_page 为准。
              query_vec 应由调用方按"query + aux_text"同一文本生成,使语义召回也覆盖辅助文字。
    """
    _ensure_cache_loaded()
    pool = [
        chunk for chunk in _select_pool(is_admin, identity)
        if _actor_can_read_chunk(
            chunk, user=user, organization_identity=organization_identity,
        )
    ]
    if not pool:
        return []

    # 辅助文字(截图抽取)并入 query,做 BM25 召回;防御性截断,绝不塞整段 OCR
    effective_query = query
    if aux_text and aux_text.strip():
        effective_query = f"{query} {aux_text.strip()[:AUX_TEXT_MAX]}"

    query_tokens = tokenize(effective_query)
    if not query_tokens:
        return []

    # 计算每个 query token 的 IDF(基于普通用户的池子算 idf 更稳)
    N = len(pool)
    avgdl = _CHUNKS_CACHE["avgdl"] or 1.0
    k1, b = 1.5, 0.75

    # 预算 df(出现在多少 chunks 里)
    df: dict[str, int] = {}
    for q in set(query_tokens):
        cnt = 0
        for c in pool:
            if q in (c.get("token_keywords") or []):
                cnt += 1
        df[q] = cnt

    idf: dict[str, float] = {}
    for q, n in df.items():
        # smoothed BM25 idf · 总是 >= 0
        idf[q] = math.log(1 + (N - n + 0.5) / (n + 0.5))

    # 预算每个 chunk 的 title 文本(用于 title boost)
    # 注:用 substring 而不是 jieba 分词 · 简单可靠 · 标题短不丢信号
    query_token_set = set(query_tokens)
    query_text_lower = effective_query.lower()
    canon_page = _canonical_route(current_page) if current_page else None

    scored: list[tuple[dict, float]] = []
    for c in pool:
        doc_tokens = c.get("token_keywords") or []
        if not doc_tokens:
            continue
        dl = len(doc_tokens)
        # tf for each query token in this doc
        tf_counter: dict[str, int] = {}
        for t in doc_tokens:
            if t in idf:
                tf_counter[t] = tf_counter.get(t, 0) + 1
        bm25_score = 0.0
        for q in query_tokens:
            if q not in tf_counter:
                continue
            tf = tf_counter[q]
            bm25_score += idf[q] * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))

        # ----- source_title boost -----
        src_title = (c.get("source_title") or "").lower()
        sec_title = (c.get("section_title") or "").lower()
        title_boost = 0.0
        for q in query_token_set:
            if q in src_title:
                title_boost += 3.0
            elif q in sec_title:
                title_boost += 1.0
        if len(query_text_lower) >= 3 and query_text_lower in src_title:
            title_boost += 5.0

        # ----- cosine boost(embedding 可用时)-----
        cos_score = 0.0
        if query_vec is not None:
            chunk_vec = c.get("embedding")
            if chunk_vec:
                from tools.xiaobang_embed import cosine
                cos_score = cosine(query_vec, chunk_vec)

        # 合成总分
        # - 纯 BM25 模式(query_vec None): score = (BM25 + title_boost)
        # - hybrid 模式: score = BM25_WEIGHT * (BM25 + title_boost) + COSINE_WEIGHT * cos_score * COSINE_SCALE
        if query_vec is None:
            score = bm25_score + title_boost
        else:
            score = (
                BM25_WEIGHT * (bm25_score + title_boost)
                + COSINE_WEIGHT * cos_score * COSINE_SCALE
            )

        # ----- 合并排序加权:路由同页 + chunk 类型(直答型优先) -----
        if score > 0:
            if canon_page and c.get("route") == canon_page:
                score *= ROUTE_MATCH_MULT
            score *= _TYPE_WEIGHT.get(c.get("source_type"), 1.0)
            scored.append((c, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


# ==========================================
# LLM 调用(流式)
# ==========================================

# 白标(v3.6 · 决策 F):{assistant}=助手名 / {brand}=品牌名 占位 · 渲染由 xiaobang_chat
# 调 resolve_branding_context(surface='agent') 注入;非 OEM 回退 _DEFAULT_ASSISTANT/_DEFAULT_BRAND
SYSTEM_PROMPT_TPL = """你是 {brand} 的 GEO 助手「{assistant}」,专门帮用户搞懂这套系统怎么用。

回答要求:
1. **只用下面给你的「参考资料」回答**问题。资料里没有的内容,直接说"这个我没找到准确答案,你可以去帮助中心翻文档"。如果参考资料里已经写明答案,就直接回答,不要再夹杂"没找到"。
2. 用大白话中文 · 简洁 · 别啰嗦 · 别用 emoji
3. 不要替用户做决策(扣费 / 发起诊断 / 下单 等) · 你只回答和引导
4. 不要透露你的底层是什么模型,如果用户问就说"{brand} 自研助手"
5. 不要把内部字段名、功能码、接口名、数据库表名、下划线英文代码说给用户；看到这类内容时,一律改成用户能听懂的中文功能名
6. 答案末尾别说"以上"、"希望对你有帮助"等套话
7. 如果有「截图定位信息」,优先用它判断用户正在问哪个子页面、按钮、字段、状态或报错;但页面身份仍以当前路由为准,不要因为截图文字猜错到别的页面
8. 不要对用户说"参考资料""知识库""上下文"这些内部词;找不到就直接说"这个我没找到准确答案,你可以去帮助中心翻文档"
9. 不输出 URL、可点击按钮名、价格、扣费结果或业务完成状态；这些只由服务端结构化动作和现役数据签发

【参考资料】
{context}

{screenshot_context}

【用户提问】
{question}
"""


async def stream_llm(
    system_prompt: str,
    user_message: str,
    history: "list[dict[str, str]] | None" = None,
) -> "tuple[asyncio.Queue[Optional[str]], dict]":
    """启动 LLM 流式调用,把 delta 推进 Queue · 完成后推 None 表示结束

    返回 (queue, status):
      - queue 推 str(LLM 流式 delta)· 完成时推 None
      - status 是个 mutable dict · _run 跑完后写入 {"succeeded": bool}
        外层据此判断要不要发 meta(sources/link/confidence)
        失败时外层应抑制 meta · 防"超时文案 + high confidence 跳转"误导

    错误处理:
    - 网络瞬时挂(ConnectError / ReadTimeout / 连接 reset)→ 重试一次
    - 重试还挂 → 推用户友好文案("现在有点忙,请再问一次")· status.succeeded=False
    - HTTP 4xx/5xx(API 真坏)→ 不重试 · 推友好文案 · succeeded=False
    - DEEPSEEK_API_KEY 缺 → 推后端配置错误提示 · succeeded=False
    """
    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    status: dict = {"succeeded": False}

    async def _try_once(api_key: str) -> tuple[bool, str]:
        """返回 (success, error_label) · 成功时 error_label 为空"""
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": XIAOBANG_MODEL,
            # [包 A③] 有界历史夹在 system 与当前问题之间,以**真实 role** 进 messages。
            # 不拼进 system_prompt 字符串:那样模型分不清「用户说过的话」和「系统指令」,
            # 而且历史里的内容会获得 system 级权重(等于给了前端一条注入通道)。
            "messages": [
                {"role": "system", "content": system_prompt},
                *(history or []),
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.3,
            "max_tokens": 1500,
            "stream": True,
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            async with client.stream(
                "POST", DEEPSEEK_API_URL, headers=headers, json=payload
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    logger.warning(
                        "[xiaobang] LLM HTTP %d resp=%s",
                        resp.status_code, body[:200].decode("utf-8", errors="replace"),
                    )
                    return False, f"http_{resp.status_code}"
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        await queue.put(content)
        return True, ""

    async def _run():
        api_key = _resolve_deepseek_key()
        if not api_key:
            await queue.put("(后端未配置 DEEPSEEK_API_KEY · 无法生成答案)")
            await queue.put(None)
            return

        # 第一次尝试
        try:
            ok, err = await _try_once(api_key)
            if ok:
                status["succeeded"] = True
                await queue.put(None)
                return
            # HTTP 错误(4xx/5xx)· 不重试
            await queue.put("现在有点忙,请再问一次试试。")
            await queue.put(None)
            return
        except httpx.TimeoutException:
            logger.warning("[xiaobang] LLM 首次超时 · 重试")
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as e:
            logger.warning("[xiaobang] LLM 首次网络挂 (%s) · 重试", type(e).__name__)
        except Exception as e:
            logger.warning("[xiaobang] LLM 首次异常 (%s: %s) · 重试", type(e).__name__, e)

        # 第二次(重试)· 真挂了用友好文案
        try:
            ok, err = await _try_once(api_key)
            if ok:
                status["succeeded"] = True
                await queue.put(None)
                return
            await queue.put("现在有点忙,请再问一次试试。")
        except Exception as e:
            logger.warning("[xiaobang] LLM 重试仍挂 (%s: %s)", type(e).__name__, e)
            await queue.put("网络好像有点抖,请稍后再问一次。")
        finally:
            await queue.put(None)

    asyncio.create_task(_run())
    return queue, status


# ==========================================
# SSE 工具
# ==========================================

def sse(event: str, data: dict | None = None) -> str:
    payload = json.dumps(data or {}, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


# ==========================================
# Request/Response models
# ==========================================

class XiaobangChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MSG_LEN)
    session_id: Optional[str] = None
    current_page: Optional[str] = Field(default="", max_length=512)
    # 截图抽取的辅助文字(按钮/字段/报错/状态/步骤,功能1 接线后由前端传)。
    # 只作召回辅助,页面身份仍以 current_page 为准;进 query 前会截断到 AUX_TEXT_MAX。
    attachment_text: Optional[str] = None
    # 只传引用，不传事实。支持 brand/client、quote、article、publication、monitoring task；
    # 服务端逐项重验归属，任意跨租户组合统一 404。
    context_refs: Optional[dict] = None
    # 幂等边界的一半(另一半是 snapshot_version)。同 id 重放同结果,不重复留痕。
    assistant_request_id: Optional[str] = Field(default=None, max_length=128)
    # [包 A③ 2026-08-21] 浏览器随身带的**有界最近对话**。
    # 🔴 服务端**不持久化**任何对话;`session_id` 只是浏览器侧的池子 id,不代表服务端记得你。
    #    「这个按钮」「刚才那个」能被解析,靠的就是这几轮随请求带上来。
    # 🔴 类型刻意写成 `Any`(不是 `list`、更不是 `list[TypedModel]`):
    #    fail-open 合同要求**脏历史被丢掉,而不是把整个问答请求打成 422**。
    #    第一版写的是 `Optional[list]` —— 判据当场抓到:传个字符串/数字/对象,
    #    pydantic 在进 handler 之前就 422 了,净化器根本没机会跑。
    #    「类型收紧」在这里是反的:校验的权威是 sanitize_recent_turns,不是 pydantic。
    recent_turns: Optional[Any] = None


class XiaobangContextRequest(BaseModel):
    current_page: Optional[str] = Field(default="", max_length=512)
    context_refs: Optional[dict] = None


@router.post("/api/xiaobang/context")
async def xiaobang_context(payload: XiaobangContextRequest, request: Request):
    """抽屉顶部的可信上下文与 CustomerOperationPlan；只读、无模型。"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    from services.customer_operation_plan import (
        build_customer_operation_plan,
        resolve_authorized_context,
    )

    context = resolve_authorized_context(
        request, payload.context_refs, payload.current_page or ""
    )
    try:
        plan = build_customer_operation_plan(context)
        return {"success": True, "context": plan["context"], "operation_plan": plan}
    except Exception as exc:  # O1：运营汇总挂了不阻断导航/聊天
        logger.warning("[xiaobang] context plan unavailable: %s", exc)
        return {
            "success": True,
            "context": context.public_context(),
            "operation_plan": None,
            "warning": {
                "message": "运营数据暂时无法读取，请重试；页面功能不受影响。",
                "retryable": True,
                "next_action_operation_id": "client_list",
            },
        }


@router.post("/api/xiaobang/parse-image")
async def xiaobang_parse_image(request: Request, file: UploadFile = File(...)):
    """小榜截图问答:图片转文字（免费、不落盘、不扣用户积分）。

    复用 chat_attachments.parse_image 的视觉管线(qwen3.6-plus),但本接口不走
    chat 附件的预扣/结算逻辑;只把截图里的页面、字段、按钮、状态、报错等
    转成 attachment_text,供 /api/xiaobang/chat 做辅助召回。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")

    filename = file.filename or "screenshot.png"
    content_type = (file.content_type or "").split(";")[0].lower().strip()
    if content_type and content_type not in XIAOBANG_IMAGE_ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail="仅支持 JPG / PNG / WebP / GIF / BMP 图片")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="图片为空")
    if len(raw) > XIAOBANG_IMAGE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"图片过大 · 请压缩到 {XIAOBANG_IMAGE_MAX_BYTES // (1024 * 1024)}MB 以内",
        )

    try:
        from services.chat_attachments import parse_image
        parsed = await parse_image(raw, filename)
    except Exception as exc:
        logger.warning("[xiaobang] parse-image failed file=%s err=%s", filename, exc)
        raise HTTPException(status_code=500, detail="图片识别失败，请直接打字描述问题")

    if not parsed.ok:
        logger.info("[xiaobang] parse-image no result file=%s err=%s", filename, parsed.error)
        raise HTTPException(status_code=422, detail="图片没看清，请直接打字描述问题")

    text = (parsed.markdown_summary or "").strip()
    # parse_image 返回带标题的 markdown;保留页面/字段/按钮/报错细节,但限制长度。
    attachment_text = text[:2000]
    return {
        "ok": True,
        "title": parsed.title or filename,
        "attachment_text": attachment_text,
        "char_count": len(attachment_text),
        "free": True,
    }


# ==========================================
# 主端点
# ==========================================

@router.post("/api/xiaobang/chat")
async def xiaobang_chat(req: XiaobangChatRequest, request: Request):
    # 取当前用户(auth 中间件注入 · 没登录直接 401)
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    # 一次解析后贯穿 KB、FAQ、OperationRegistry 和动作签发，禁止各链重新猜身份。
    organization_identity = getattr(request.state, "organization_identity", None)
    resolved_identity = _resolve_request_identity(user, organization_identity)
    is_admin = resolved_identity.is_admin
    kb_identity = resolved_identity.knowledge_identity
    operation_identity = resolved_identity.operation_identity

    user_message = req.message.strip()
    # v3.6 白标:OEM 代理出代理品牌/助手名,否则平台默认(小榜 / OmniRank)
    assistant_name, brand_name = _resolve_agent_branding(request)

    # 固定编排 1-3：先重验上下文、匹配确定性操作、读取统一运营计划。
    # 这段在 StreamingResponse 建立前执行，因此跨租户提示能以真实 404 fail-closed，
    # 不会先回 200 再在 SSE 里泄漏错误形态。
    from services.customer_operation_plan import (
        build_customer_operation_plan,
        resolve_authorized_context,
    )
    from services.gap_assistant import build_answer as build_structured_answer
    from services.gap_operation_map import match_operation

    resolved_context = resolve_authorized_context(
        request, req.context_refs, req.current_page or ""
    )
    try:
        operation_plan = build_customer_operation_plan(resolved_context)
    except Exception as exc:  # O1：汇总失败不应使系统入口失效
        logger.warning("[xiaobang] operation plan unavailable, navigation remains: %s", exc)
        # None 是显式“不可用”，不是“全零计划”。统一内核会签发重试出口，
        # 但绝不会说待写/已完成/已发布为 0。
        operation_plan = None
    import hashlib as _hashlib
    structured_answer = build_structured_answer(
        question=user_message,
        context_refs=req.context_refs,
        request=request,
        assistant_request_id=(
            req.assistant_request_id
            or f"xb:{req.session_id or 'anon'}:{_hashlib.sha256(user_message.encode()).hexdigest()[:16]}"
        ),
        actor_user_id=(user or {}).get("user_id") or (user or {}).get("id"),
        current_page=req.current_page or "",
        identity=operation_identity,
        resolved_context=resolved_context,
        operation_plan=operation_plan,
        ensure_fallback=True,
    )
    # [包 A③] 有界最近对话:**进 StreamingResponse 之前**就净化好。
    # 放这里而不是流里:净化是纯函数、不该在流中途才失败;而且判据能打到一个确定的值。
    from services.xiaobang_recent_turns import (
        sanitize_recent_turns,
        turns_as_chat_messages,
    )
    # [包 D①⑤] 出口装配与重复兜底检测。重复检测**不需要服务端会话表** ——
    # 靠的正是上面这几轮 recent_turns:服务端因此看得见自己上一轮说过什么。
    from services.xiaobang_answer_exits import (
        assert_has_exit,
        build_exits,
        is_repeat_fallback,
    )

    recent_turns = sanitize_recent_turns(req.recent_turns)
    recent_history_messages = turns_as_chat_messages(recent_turns)

    # [包 E] 意图分档只用于观测,不参与任何产品判断(判断在 _should_short_circuit_canned)
    from services.xiaobang_question_intent import (
        is_self_contained_question as _isc,
        mentions_current_page as _mcp,
    )
    _self_contained = _isc(user_message)
    _page_deictic = _mcp(user_message)

    # [包 B② 2026-08-21] 确定性操作直答这条短路**先于一切层**返回,
    # 所以它一旦误命中,后面改多少提示词都没用。两种误命中在这里掐掉:
    #   · 自足能力问句(「你还能做什么」)—— 用户要能力清单,不是被导航走;
    #   · 命中短语正被排除(「除了帮助中心还能…」)—— 裸子串匹配读不出否定。
    # 词表不在这里抄第二份:短语直接取自注册表条目本身。
    from services.xiaobang_question_intent import should_suppress_operation_shortcut

    _operation_hit = match_operation(user_message)
    if _operation_hit and should_suppress_operation_shortcut(
        user_message, (_operation_hit.display_name, *_operation_hit.synonyms)
    ):
        _operation_hit = None

    deterministic_direct = bool(_operation_hit) or any(
        token in user_message for token in (
            "今天先做什么", "下一步去哪", "为什么这样安排", "运营建议", "这个客户",
            "这个页面怎么用", "当前页面怎么用", "这页怎么用",
        )
    )

    def structured_meta(base: dict[str, Any] | None = None) -> dict[str, Any]:
        out = dict(base or {})
        # 旧 FAQ / preset / RAG 的 route 不再有签发权。动作统一来自注册表。
        out.pop("link", None)
        out.update(structured_answer.as_meta())
        return out

    # [包 E] 这一轮的观测底稿。各分支只往里**记结果**,发送由下面的 finally 统一做。
    turn_obs: dict[str, Any] = {
        "session_id": req.session_id or "",
        "request_id": req.assistant_request_id or "",
        "actor_kind": _telemetry_actor_kind(user, operation_identity),
        "current_route": req.current_page or "",
        "intent_kind": ("self_contained" if _self_contained
                        else ("page_deictic" if _page_deictic else "other")),
        "deterministic_hit": bool(deterministic_direct),
        "answer_state": "unknown",
        "exits": [],
        "repeat_fallback": False,
    }

    async def event_stream():
        # 首帧 · 触发 response header flush(同 agent_api 防 nginx 缓冲)
        yield ": ready\n\n"

        try:
            # 确定性导航/运营问题不必等待模型；模型完全拔掉时仍返回同一动作与事实。
            if deterministic_direct:
                for chunk in _split_for_streaming(structured_answer.plain_text()):
                    yield sse("text", {"delta": chunk})
                    await asyncio.sleep(0.02)
                yield sse("meta", structured_meta({"sources": [], "confidence": "high"}))
                yield sse("done")
                return

            # ===== 第 1 层 · 预设答案优先命中 =====
            preset: XiaobangPreset | None = match_preset(user_message)
            if (
                preset and _should_short_circuit_canned(req)
                and _actor_can_read_route(
                    preset.route, user=user, organization_identity=operation_identity,
                )
            ):
                # 把答案分块流式吐(让用户体验一致 · 不一闪而过)· v3.6 白标渲染助手名/品牌名
                preset_answer = render_preset_answer(
                    preset, assistant=assistant_name, brand=brand_name)
                for chunk in _split_for_streaming(preset_answer):
                    yield sse("text", {"delta": chunk})
                    await asyncio.sleep(0.02)
                meta: dict[str, Any] = {
                    "sources": [{
                        "source_slug": f"preset_{preset.key}",
                        "source_title": "预设答案",
                        "source_type": "preset",
                    }],
                    "confidence": "high",
                }
                if preset.route:
                    meta["link"] = _with_operation(
                        {"route": preset.route, "label": preset.route_label or "去看"})
                yield sse("meta", structured_meta(meta))
                yield sse("done")
                return

            # ===== 第 1.2 层 · Canned FAQ 高频问题硬兜底 =====
            # 10+ 条高频销售问题 deterministic 答案 · 不调 LLM 不走 BM25
            # 实际条数见 agents/xiaobang_canned_faq.py::CANNED_FAQS · 直接 len 拿不踩"数字过期"坑
            # 防"摇骰子"问题:同 query 多次问 confidence 飘 / 答案飘 / link 飘
            # 命中:输出固定答案 + 固定 source + 固定 route + 高置信度
            #
            # ⚠️ 越权 query 也可能命中 canned(eg "你能帮我自动发布文章吗" 含"发布文章"
            #    命中 how_to_publish patterns)· 此时 链接卡 必须抑制
            #    答案文本仍给(解释流程对用户有用)· 但 链接卡 不出(防"小榜继续执行"误判)
            canned: XiaobangCannedFAQ | None = match_canned_faq(user_message)
            if (
                canned and _should_short_circuit_canned(req)
                and _actor_can_read_route(
                    canned.route, user=user, organization_identity=operation_identity,
                )
            ):
                canned_unauth = _is_unauthorized_request(user_message)
                logger.info(
                    "[xiaobang] canned FAQ 命中 key=%s unauth=%s · query=%s",
                    canned.key, canned_unauth, user_message[:30],
                )
                for chunk in _split_for_streaming(canned.answer):
                    yield sse("text", {"delta": chunk})
                    await asyncio.sleep(0.02)
                meta: dict[str, Any] = {
                    "sources": [{
                        "source_slug": f"canned_{canned.key}",
                        "source_title": canned.title,
                        "source_type": "faq",
                    }],
                    "confidence": "high",
                }
                # F2 越权拦截:canned 命中 + 越权 → 不出 链接卡
                if canned.route and not canned_unauth:
                    meta["link"] = _with_operation(
                        {"route": canned.route, "label": canned.route_label or "去看"})
                yield sse("meta", structured_meta(meta))
                yield sse("done")
                return

            # ===== 第 1.5 层 · 非业务抽象问句前置硬拒 =====
            # "老板是谁/朋友是谁/同事怎么样" 这类 LLM 即使看到 KB 偶然命中的边角词
            # 也容易"编出听起来合理的答案",绕过后置 NO_ANSWER 降级。
            # 用确定性硬规则前置拒绝 · 不让进 RAG。
            if _is_non_business_question(user_message):
                logger.info("[xiaobang] 检测到非业务抽象问句 · 走软拒: %s", user_message[:30])
                _soft = render_soft_reject(assistant=assistant_name, brand=brand_name)
                for chunk in _split_for_streaming(_soft):
                    yield sse("text", {"delta": chunk})
                    await asyncio.sleep(0.02)
                yield sse("meta", structured_meta({"sources": [], "confidence": "low"}))
                yield sse("done")
                return

            # ===== 第 1.6 层 · 普通用户询问代理经营信息前置硬拒 =====
            # 普通用户 KB 不能回答代理/服务方怎么赚钱、利润、成本、结算等经营信息。
            # 这类问题不进检索、不交给 LLM,避免普通文档边缘词被模型拼成代理经营模式。
            if kb_identity == "normal_user" and _is_normal_user_agent_business_question(user_message):
                logger.info("[xiaobang] 普通用户询问代理经营信息 · 前置拒答: %s", user_message[:30])
                safe_answer = render_normal_user_agent_business_reject()
                for chunk in _split_for_streaming(safe_answer):
                    yield sse("text", {"delta": chunk})
                    await asyncio.sleep(0.02)
                yield sse("meta", structured_meta({
                    "sources": [], "confidence": "high", "should_escalate": False,
                }))
                yield sse("done")
                return

            # ===== 第 2 层 · Hybrid 检索(BM25 + embedding cosine)+ 业务关键词二级过滤 =====
            # 查询 embedding · 失败(API 挂 / key 缺)就 fallback 到纯 BM25(query_vec=None)
            # 截图抽取的辅助文字(按钮/字段/报错/状态/步骤)——功能1 接线后由前端传 attachment_text。
            # 页面身份始终以 current_page 为准,attachment_text 只补召回。
            # ⚠️ 在入口处一次性截断到 AUX_TEXT_MAX,保证 embedding 和 BM25 吃到的是同一份截断文本,
            #    即使前端误传整段 OCR 也不会污染向量。
            attachment_text = _clip_attachment_text(req.attachment_text)
            # query embedding 按"问题 + 截断后辅助文字"同一文本生成,让语义召回也覆盖辅助文字
            embed_input = f"{user_message} {attachment_text}".strip() if attachment_text else user_message
            query_vec = None
            try:
                from tools.xiaobang_embed import embed_one
                query_vec = await asyncio.wait_for(embed_one(embed_input, text_type="query"), timeout=3.0)
            except (asyncio.TimeoutError, Exception) as _e:
                logger.warning("[xiaobang] query embedding 失败 · fallback 到纯 BM25: %s", _e)

            results = bm25_search(
                user_message, is_admin=is_admin, top_k=TOP_K, query_vec=query_vec,
                identity=kb_identity, current_page=req.current_page, aux_text=attachment_text,
                user=user, organization_identity=operation_identity,
            )
            top_score = results[0][1] if results else 0.0
            has_biz_kw = _has_business_keyword(user_message)
            # F2 · 越权意图检测前置 · clarify 之前算
            # 原因:命中越权时不该让 clarify 跑("替我充值100块"反问"充值入口"会让用户
            # 以为小榜要执行 → 违反 deterministic 承诺)。越权 query 直接走正常 RAG
            # 答(LLM 解释流程),但抑制 链接卡。
            unauthorized = _is_unauthorized_request(user_message)
            logger.info(
                "[xiaobang] 查询 '%s' top_score=%.2f hits=%d biz_kw=%s unauth=%s embed=%s",
                user_message[:30], top_score, len(results), has_biz_kw, unauthorized,
                "yes" if query_vec else "no",
            )

            # ===== 第 2.5 层 · 反问澄清(越权跳过)=====
            # 触发条件:
            #  - top 1 / top 2 分差 < CLARIFY_GAP (2.0)
            #  - top 2 个 chunk 来自不同 source_slug(不同文档)
            #  - 候选项标题与 query 有 token 重叠(过滤偶然误召回)
            # 命中后:不调 LLM 不消费 token · 让用户先选哪个意图,再二次查询
            #
            # 越权 query 跳过 clarify · 因为反问"扣费入口在哪"这种选项会被用户当成
            # "小榜要执行" · 而我们承诺不替用户操作。
            if not unauthorized:
                clarify_options = _maybe_clarify(results, user_message)
                if clarify_options:
                    yield sse("clarify", {
                        "prompt": "你问的具体是哪种?选一下我才答得准",
                        "options": clarify_options,
                    })
                    yield sse("meta", structured_meta({"sources": [], "confidence": "medium"}))
                    yield sse("done")
                    return

            # Task 3 · route 页面卡提前取（必须在软拒之前）
            # spec §5.1：页面卡是保底上下文，命中就给，不靠 BM25 分数。
            # 提前算好 page_card，传给 _should_soft_reject 决定是否豁免软拒。
            page_card = _inject_route_page_card(
                req.current_page or "", is_admin, identity=kb_identity,
                user=user, organization_identity=operation_identity,
            )

            # 软拒判定:
            #  - 分数 < BM25_THRESHOLD                  → 一定软拒
            #  - 分数 < STRONG_THRESHOLD 且 无业务关键词  → 软拒(防 "帮我写代码 / 天气" 这种偶然命中)
            #  - 当前页面卡命中                           → 不软拒（保底，spec §5.1）
            if _should_soft_reject(results, top_score, has_biz_kw, has_page_card=bool(page_card)):
                _soft = render_soft_reject(assistant=assistant_name, brand=brand_name)
                for chunk in _split_for_streaming(_soft):
                    yield sse("text", {"delta": chunk})
                    await asyncio.sleep(0.02)
                yield sse("meta", structured_meta({"sources": [], "confidence": "low"}))
                yield sse("done")
                return

            # ===== 低置信兜底(检索层判断)=====
            # 命中了 route 页面卡,但 BM25/embedding 最高内容分仍低于阈值 →
            # 不靠单薄的页面卡硬答(会编),承认"能看到你在这页但知识不够"并接人工反馈。
            if _is_route_only_insufficient(page_card, results, top_score):
                _pname = page_card.get("source_title") or "这个页面"
                _msg = (
                    f"我能看到你大概在「{_pname}」这个页面，但这块的知识库说明还不够，"
                    f"没法给你准确解答。建议点下方把问题反馈给工作人员，会有人帮你跟进。"
                )
                # [包 D⑤ 2026-08-21] 重复兜底 → 自动升级为「先澄清 + 人工接管」。
                # 🔴 靠的是包 A③ 带上来的 recent_turns:服务端**看得见自己上一轮说过什么**,
                #    不需要新建任何会话表(§7.4 明令)。同一条兜底连着来第二次,
                #    对用户就是「你在循环」—— 工单 §8 S06 点名禁止。
                _repeat = is_repeat_fallback(recent_turns, _msg)
                if _repeat:
                    _msg = (
                        f"我还是没能在「{_pname}」这块给你准的答案 —— 同一句我不想再说第二遍。"
                        f"要么你把按钮上的字或报错原文发我,我换个方向再定位一次;"
                        f"要么点下面直接转人工,我把这次的问题、你在哪一页、"
                        f"以及我刚才答过什么一起带过去。"
                    )
                for chunk in _split_for_streaming(_msg):
                    yield sse("text", {"delta": chunk})
                    await asyncio.sleep(0.02)
                _exits = build_exits(confidence="low", handoff=True, repeat_fallback=_repeat)
                assert_has_exit(_exits, where="route_only_insufficient")
                turn_obs.update(answer_state="low_confidence", exits=_exits,
                                repeat_fallback=_repeat)
                yield sse("meta", structured_meta({
                    "sources": [], "confidence": "low", "handoff": True,
                    "should_escalate": _repeat,
                    "exits": _exits,
                }))
                yield sse("done")
                return

            # ===== 第 3 层 · LLM 生成 =====
            # 拼 context · 每段截短防 prompt 超长
            # 兼容"results 空但有 page_card"：context_parts 可为空列表，page_note 仍会拼入
            context_parts = []
            for c, _s in results:
                hdr = f"《{c['source_title']}》"
                if c.get("section_title"):
                    hdr += f" · {c['section_title']}"
                context_parts.append(f"{hdr}\n{c['content']}")
            context = "\n\n---\n\n".join(context_parts)

            # route 页面卡注入（按角色选池 RBAC，复用上方提前计算的 page_card）
            # 不改检索 query 本身，只把当前页面说明拼进 context 末尾
            if page_card:
                page_note = (
                    f"\n\n【当前页面：{page_card['source_title']}】\n{page_card['content']}"
                )
                context += page_note

            screenshot_context = ""
            if attachment_text:
                screenshot_context = (
                    "【截图定位信息】\n"
                    f"{attachment_text}\n"
                    "请把这段信息当作用户截图里的页面细节,用来判断他指的是哪个子页面、按钮、字段、状态或报错。"
                )

            system_prompt = SYSTEM_PROMPT_TPL.format(
                assistant=assistant_name,
                brand=brand_name,
                context=context,
                screenshot_context=screenshot_context,
                question=user_message,
            )

            # 置信度 · 由 top_score 决定 SourceCard 样式
            confidence = "high" if top_score >= 3.0 else ("medium" if top_score >= 1.5 else "low")

            # unauthorized 已在 clarify 前算过(F2 越权前置 · 见上方 logger)
            if unauthorized:
                logger.info("[xiaobang] 检测到越权意图 · 抑制链接卡: %s", user_message[:40])

            # 启动 LLM 流 · 边 yield 边累积 full text(用于 NO_ANSWER 后置降级)
            # [包 A③] 有界最近对话:净化 → 转 chat messages → 夹在 system 与当前问题之间。
            # 净化的唯一权威在 services/xiaobang_recent_turns.py(浏览器只负责少发点)。
            llm_queue, llm_status = await stream_llm(
                system_prompt, user_message, history=recent_history_messages
            )
            llm_full_text = ""
            while True:
                delta = await llm_queue.get()
                if delta is None:
                    break
                llm_full_text += delta
                yield sse("text", {"delta": delta})

            llm_succeeded = llm_status.get("succeeded", False)

            # F2 · LLM 失败时 meta 降级:不带 link · sources 空 · confidence='low'
            # 防"超时文案 + high confidence + 跳转按钮"组合让用户误以为认真答了
            if not llm_succeeded:
                fallback_text = "\n\n" + structured_answer.plain_text()
                yield sse("text", {"delta": fallback_text})
                # [包 D①] O1:模型这一跳挂了是**可重试**的,给重试 + 手动 + 人工三个出口。
                # 「失败且没有任何出口」是工单 §5.1/§9.1 明令禁止的形态。
                _exits_llm = build_exits(confidence="low", handoff=True, retryable=True)
                assert_has_exit(_exits_llm, where="llm_failed")
                turn_obs.update(answer_state="llm_failed", exits=_exits_llm)
                yield sse("meta", structured_meta({
                    "sources": [], "confidence": "low",
                    "handoff": True, "exits": _exits_llm,
                }))
                yield sse("done")
                return

            # F2 · "LLM 自己说没找到答案"后置降级
            # 场景:KB 召回到了 chunk(BM25 分数 > 阈值)但 LLM 看 context 后判断不够答
            # 比如"老板是谁" BM25 召回《什么是 GEO·为啥要做》(里面举例提到"老板想找..."),
            # 但 LLM 看了发现根本没在答用户问的"老板是谁",所以诚实说"没找到准确答案"。
            # 这种情况 SourceCard / 链接卡 显示《什么是 GEO》会误导用户以为有答案。
            # 检测 LLM 文本含明显"我不知道"信号 → meta 降低 + 清 sources + 无 link。
            NO_ANSWER_SIGNALS = (
                "没找到准确", "没找到答案", "没找到直接的答案",
                "没有准确", "没有直接的答案",
                "我不知道", "我无法确定", "无法准确",
                "翻一下文档", "去帮助中心翻文档",
            )
            if any(sig in llm_full_text for sig in NO_ANSWER_SIGNALS):
                logger.info("[xiaobang] LLM 自陈无答案 · meta 降级: %s", user_message[:30])
                # F6 加强:LLM 说"没找到"时清空 sources · 但 intent map 命中的话还给 route
                # 用户至少能跳到对的页面自己看(比 high+错答 + 无 link 体验好)
                # [包 D①⑤] 这一支就是截图里那句「去帮助中心翻文档」的出处
                # (`NO_ANSWER_SIGNALS` 里逐字列着它)。它必须:
                #   · 带出口,不能只留一句甩锅;
                #   · 同一条兜底第二次出现时升级成澄清 + 人工。
                _repeat_dn = is_repeat_fallback(recent_turns, llm_full_text)
                if _repeat_dn:
                    yield sse("text", {"delta": (
                        "\n\n同一句我不想再说第二遍 —— 你把按钮上的字或报错原文发我,"
                        "我换个方向再定位;或者点下面直接转人工。")})
                meta_dn: dict[str, Any] = {
                    "sources": [], "confidence": "low",
                    "handoff": True, "should_escalate": _repeat_dn,
                }
                _exits_dn = build_exits(confidence="low", handoff=True,
                                        repeat_fallback=_repeat_dn)
                assert_has_exit(_exits_dn, where="llm_no_answer")
                meta_dn["exits"] = _exits_dn
                turn_obs.update(answer_state="low_confidence", exits=_exits_dn,
                                repeat_fallback=_repeat_dn,
                                help_center_only=not bool(meta_dn.get("link")))
                if not unauthorized:
                    intent = _match_intent(user_message)
                    if intent:
                        meta_dn["link"] = _with_operation(
                            {"route": intent[0], "label": intent[1]})
                yield sse("meta", structured_meta(meta_dn))
                yield sse("done")
                return

            # 正常 meta(sources + 跳转)
            sources_out: list[dict[str, Any]] = []
            seen_slugs = set()
            for c, _s in results:
                slug = c["source_slug"]
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                sources_out.append({
                    "source_slug": slug,
                    "source_title": c["source_title"],
                    "section_title": c.get("section_title") or "",
                    "source_type": c["source_type"],
                })

            # F1 · 路由二次判定 + intent map · F2 · 越权不出 link
            query_tokens_set = set(tokenize(user_message))
            link_out = None if unauthorized else _pick_link(results, query_tokens_set, user_message)
            meta = {
                "sources": sources_out[:3],
                "confidence": confidence,
            }
            if link_out:
                meta["link"] = link_out

            turn_obs["answer_state"] = "answered"
            yield sse("meta", structured_meta(meta))
            yield sse("done")

        except Exception as e:
            logger.exception("[xiaobang] chat 处理失败")
            turn_obs["error_kind"] = type(e).__name__
            turn_obs["answer_state"] = "error"
            _exits_err = build_exits(confidence="low", handoff=True, retryable=True)
            assert_has_exit(_exits_err, where="chat_exception")
            turn_obs["exits"] = _exits_err
            yield sse("text", {
                "delta": f"{assistant_name}这次没能读完资料。\n\n{structured_answer.plain_text()}",
            })
            yield sse("meta", structured_meta({
                "sources": [], "confidence": "low",
                "handoff": True, "exits": _exits_err,
            }))
            yield sse("done")
        finally:
            # 🔴 [包 E 2026-08-21] 观测事件的**唯一**收口点。
            #    这条流里有 12 个 `yield sse("done")` 出口 —— 逐支埋 emit,
            #    必然有一支被漏掉,而漏掉的那支不会让任何东西变红
            #    (同一谓词写多处 = 必有一处没人验)。放在 finally 里,
            #    无论从哪一支返回、乃至抛异常,都恰好发一次。
            #    `emit` 内部吞掉一切异常:观测永远不许打断问答。
            _emit_turn_observation(turn_obs)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


# ==========================================
# 工具:把 preset / 软拒答案切成"几十字一帧"的伪流
# 让前端 cursor 闪动效果跟 LLM 流式一致
# ==========================================

def _split_for_streaming(text: str, max_chunk: int = 30) -> list[str]:
    out: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if len(buf) >= max_chunk and ch in "。!?,;\n、 ":
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


# ==========================================
# 健康检查 · 给运维 / 索引重建后验证用
# ==========================================

@router.get("/api/xiaobang/health")
async def xiaobang_health(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    _ensure_cache_loaded()
    from db.kb_db import count_chunks, load_release_manifest
    from services.kb_health_verdict import evaluate

    # 🔴 [包 C⑥ 2026-08-21 · 工单 §3.5] `ok` 原来是**字面量 True** ——
    #    「系统显示 health ok / lint ok,实际却在使用不同年代的知识」。
    #    现在它是**裁定结果**:manifest 与当前代码逐项对账
    #    (术语规则版本 / 注册表版本 / 路由指纹)+ manifest 声明条数与库里实际条数对账。
    #    不一致时 status = stale / mixed / incomplete,且 reasons 里写清谁跟谁不一致。
    #    取数失败**不当健康**:算不出裁定就报 incomplete,不退回 ok=True。
    live_counts = count_chunks()
    try:
        verdict = evaluate(load_release_manifest(), live_counts)
    except Exception as exc:
        logger.warning("[xiaobang] health 裁定算不出来: %s", exc)
        verdict = {
            "status": "incomplete", "ok": False,
            "reasons": ["健康裁定取数失败:{0}".format(type(exc).__name__)],
            "manifest_version": None,
        }
    return {
        # 旧消费方读的这几个键**一个没动**(形状向后兼容)
        "ok": verdict["ok"],
        "model": XIAOBANG_MODEL,
        "kb_chunks": live_counts,
        "bm25_threshold": BM25_THRESHOLD,
        "deepseek_key_configured": bool(_resolve_deepseek_key()),
        "cache_loaded_at": _CHUNKS_CACHE["loaded_at"],
        # 新增:说得出「为什么不健康」
        "status": verdict["status"],
        "reasons": verdict["reasons"],
        "manifest_version": verdict["manifest_version"],
    }


# ==========================================
# KB 自动更新 · 重建索引接口 + 缓存失效
# ==========================================

def invalidate_kb_cache() -> None:
    """清掉进程内 KB 缓存 · 下次 /chat 请求会触发懒加载

    被 faq_api.py 在 CRUD 后调用 · 也可以在 trigger_reindex 内部用
    """
    _CHUNKS_CACHE["loaded_at"] = 0
    logger.info("[xiaobang] KB 缓存已失效 · 下次请求重新加载")


async def trigger_reindex(scope: str = "faq") -> dict:
    """触发索引重建(异步包装同步脚本)

    scope: 'faq' 只重建 FAQ chunks(秒级 · 适合 CRUD hook)
           'all' 重建全部(docs + faq + preset · 7-10 秒 · 适合管理员手动 + 部署)
    """
    from tools.xiaobang_kb_indexer import reindex_faq, reindex_docs, reindex_presets
    from services.kb_release_ops import track_reindex

    # [WO-A ② · 2026-08-20] 状态位套在**这里**,不套在各调用点:
    #   admin 手动重建与 FAQ CRUD 的 fire-and-forget 钩子都从这个函数下去,
    #   这是两条路唯一的共同收口点。套在调用点上 = 同一个谓词写两处,
    #   必有一处漏掉(而漏掉的那次失败就再次变成静默)。
    result: dict[str, Any] = {"scope": scope}
    with track_reindex(scope):
        if scope == "faq":
            n = await asyncio.to_thread(reindex_faq)
            result["faq"] = n
        else:
            n_docs = await asyncio.to_thread(reindex_docs)
            n_faq = await asyncio.to_thread(reindex_faq)
            n_preset = await asyncio.to_thread(reindex_presets)
            result.update({"doc": n_docs, "faq": n_faq, "preset": n_preset})
    invalidate_kb_cache()
    logger.info("[xiaobang] 重建索引完成 scope=%s result=%s", scope, result)
    return result


@router.post("/api/admin/xiaobang/reindex-system")
async def admin_reindex_system(request: Request):
    """管理员触发系统知识库重建（只读 final 目录，不跑浏览器采集）

    直接调 reindex_system() 而不经 trigger_reindex()，原因：
      · trigger_reindex 负责 faq/docs/preset 三类，system 是独立来源；
      · 不想把 scope 字符串白名单耦合进来，保持各来源独立入口。
    """
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可操作")

    from tools.xiaobang_system_kb import reindex_system
    from db.kb_db import count_chunks, load_all_chunks
    from services.kb_release_ops import track_reindex

    # 系统知识库重建同样进状态位:它是 fail-closed 的(见 R3-P8 ④),
    # 失败时管理员当场看到 500,但**前台状态位**也要留下痕迹 —— 否则
    # 换个人打开页面只会看到「一切正常」。
    with track_reindex("system"):
        inserted = await asyncio.to_thread(reindex_system)
    invalidate_kb_cache()

    all_chunks = load_all_chunks(include_admin=True)
    routes = sorted({c["route"] for c in all_chunks if c.get("source_type") == "sys_page"})

    return {
        "ok": True,
        "inserted": inserted,
        "counts": count_chunks(),
        "routes": routes,
    }


@router.get("/api/admin/xiaobang/reindex-status")
async def admin_xiaobang_reindex_status(request: Request):
    """[WO-A ② · 2026-08-20] 索引重建状态位:`running` / `failed` / `unknown` / `idle`。

    存在的理由:重建是 fire-and-forget —— FAQ CRUD 返 200 **不等于**索引更了。
    失败读的是 `ai_ops_alerts`(跨进程 · 部署期那次重建是另一个进程,
    它的失败也从这里看得见);进行中是本进程的。
    """
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    from services.kb_release_ops import reindex_status

    return {"ok": True, **reindex_status()}


@router.post("/api/admin/xiaobang/reindex")
async def admin_xiaobang_reindex(request: Request, scope: str = "all"):
    """管理员手动触发助手知识库重建

    scope=all  重建 docs + faq + preset(用于改完 docs-data.ts / 调 preset 后)
    scope=faq  只重建 faq(用于 DB 里直接改 FAQ 后)
    """
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    if scope not in ("all", "faq"):
        raise HTTPException(status_code=400, detail="scope 只能是 all 或 faq")
    result = await trigger_reindex(scope=scope)
    return {"ok": True, **result}


def _branding_principal(request, uid) -> int:
    """[白标继承] 后台皮肤也走团队长的品牌:员工看到的应是所属服务商的品牌,不是平台默认。"""
    try:
        from auth.principal_identity import resolve_branding_principal_user_id
        return resolve_branding_principal_user_id(request, fallback_user_id=int(uid))
    except Exception:
        return int(uid)
