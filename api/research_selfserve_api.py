"""[R 批 · U4] 自助单行业调研 API —— 免费预览(/draft)+ 付费发起(/self-serve)+ 轮询(/task)。

自助跑一轮单行业 GEO 调研,让本行业的「AI 真实引用媒体榜」点亮(可投放媒体推荐)。
鉴权【登录即可】(WO_211 · Owner 2026-09-14「权限需要开一下,给他们用」):未登录 401;
客户 portal / 公开 token 进不到(没有登录态)。品牌范围仍走 require_brand_access,
读端点仍逐条判属主,计费不动 —— 放开的只是身份闸这一格。

计费(freeze 生命周期,worker 侧 commit/release 见 selfserve_worker):
  create task(freeze_id=None) → freeze_points(extra_cost=动态算力) → 回填 freeze_id/freeze_table
  → BackgroundTask 触发队列消费。余额不足先标 cancelled 再冒泡 402。

归一同源:行业原文经 U3 resolver 归并到平台标准行业,industry_key/规范名之后全链(round 快照/
验收查榜/新鲜度闸)统一使用,不用品牌原文 —— 「写库归一 = 读榜归一 = 验收归一」。

红线:不改 billing/connection/auth/jwt 本体,只调用其公共函数;不碰四张公共池表。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from auth.brand_access import require_brand_access  # [GEO-R2-CAN-038] 共享 RBAC 策略(含分配客户)
from db.connection import get_connection
from db.research_selfserve_db import (
    create_selfserve_task,
    promote_selfserve_task_to_queued,
    set_selfserve_task_billing_exempt,
    update_selfserve_task_freeze,
    update_selfserve_task_industry_id,
    update_selfserve_task_prompt_snapshot,
    set_selfserve_task_status,
    get_selfserve_task,
    get_selfserve_task_by_round,
    list_user_selfserve_rounds,
    get_active_selfserve_task,
    get_user_last_selfserve,
    get_industry_last_selfserve,
    activate_selfserve_prompts,
    write_alias,
)
from services.research_monitor.industry_resolver import preview_resolve, resolve_or_create_industry
from services.research_monitor.industry_registry import ensure_research_industry
from services.research_monitor.cost_estimator import (
    # [2026-07-28 块A] `estimate_selfserve_round_cost` / `selfserve_price_points` 已不再被本模块引用:
    #   售价改成价目表驱动(feature_pricing.cost_points),不再由成本现算。两函数本身保留
    #   (仍是成本口径 SSOT,供成本核算/其他调用方用),只是不再决定用户看到和被扣的价。
    SELFSERVE_MIN_PROMPTS,
    SELFSERVE_MAX_PROMPTS,
)
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-ResearchSelfserve-API")

router = APIRouter(prefix="/api/publish/research", tags=["调研自助"])

FEATURE_CODE = "geo_research_selfserve"
FRESHNESS_DAYS = 7
SUGGEST_PROMPT_COUNT = 6
PRICE_FLOOR_POINTS = 10  # 绝不 free 静默:算成 0 时兜底最小档

# [R#8] /draft 免费预览轻量内存频控:未付费端点仍会烧一次 LLM 归并(persist=False 已挡住写库,
#   但 LLM 调用还在),防脚本高频刷爆成本。窗口取小(默认 3s)只挡机枪式刷,不误伤人类换行业再预览。
#   ⚠️ 进程内内存态(生产 WORKERS=1 有效);多 worker 需换 Redis —— TODO(见 methodology 频控通道)。
try:
    _DRAFT_THROTTLE_SECONDS = max(0, int(os.getenv("RESEARCH_DRAFT_THROTTLE_SECONDS", "3")))
except Exception:
    _DRAFT_THROTTLE_SECONDS = 3
_draft_last_call: dict[int, float] = {}


# ==================== 请求模型 ====================


class DraftRequest(BaseModel):
    industry: str = Field(..., min_length=1, max_length=200, description="用户填写的行业原文")
    brand_id: Optional[int] = Field(default=None, description="可选品牌 id(成本归属 + RBAC + 行业判定上下文)")
    category_key: Optional[str] = Field(default=None, max_length=40,
                                        description="[WO_267] 用户在弹窗里改选的行业大类 key(优先于推断)")


class SelfServeRequest(BaseModel):
    industry: str = Field(..., min_length=1, max_length=200, description="用户填写的行业原文")
    brand_id: Optional[int] = Field(default=None, description="可选品牌 id(成本归属 + RBAC + 行业判定上下文)")
    category_key: Optional[str] = Field(default=None, max_length=40,
                                        description="[WO_267] 用户在弹窗里改选的行业大类 key(优先于推断)")
    selected_prompt_ids: List[int] = Field(default_factory=list, description="勾选的已有 prompt id")
    new_prompt_texts: List[str] = Field(default_factory=list, description="自加的新题目文本")


# ==================== 鉴权 ====================


def _require_logged_in_user(request: Request) -> dict:
    """登录即可用:未登录 401。客户 portal / 公开 token 进不到这里(没有 request.state.user)。

    🔴 [WO_211 · Owner 2026-09-14] 原来这里卡 `agent_level < 1` ⇒ 403「仅服务方可使用」,
       而发布中心那颗「点亮调研」按钮对所有登录用户都显示 —— 普通用户点了就撞 403。
       Owner 原话:「权限需要开一下,给他们用」。

    🔴 放开的只是**这道身份闸**,别的一格都没松:
       · 品牌范围仍走 `_verify_brand_owner` → `require_brand_access`(:120),
         普通用户只能对自己拥有 / 被分配的品牌调研;
       · 读端点仍逐条判属主(`/task/{id}` `/round-report` `/rounds` `/active-task`
         都以调用者 user_id 为条件,非属主与不存在**同返 404**,不给枚举旁路);
       · 计费不动:同一价目键、`_require_priced()` fail-closed、`charge_on_success`
         语义一个字没改 —— 普通用户花的是**自己的**算力。

    🔴 不再查 `user_wallets.agent_level`:那次查询**唯一**的用途就是这道闸。
       留着它等于留一个没人读的读操作,下一个人会以为返回值里有身份信息。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    uid = user.get("user_id") or current_user_id(user)
    if not uid:
        raise HTTPException(status_code=401, detail="登录态异常")
    return {"user_id": int(uid), "is_admin": bool(user.get("is_admin"))}


def _verify_brand_owner(request: Request, brand_id: Optional[int]) -> None:
    """brand_id 提供时校验归属(RBAC 边界)。

    [GEO-R2-CAN-038] 改走共享策略 auth.brand_access.require_brand_access —— 之前本地实现只比对
    brands.owner_user_id,把「被分配(client_brand_ids)但非 owner」的代理误 403(fail-CLOSED 但过严,
    assigned 代理无法为自己负责的客户品牌跑自助调研)。require_brand_access 同时接受【分配客户】与
    【自有 owner】品牌,且 admin 内部跳过、None 由 allow_null=True 放行(自助调研为行业级 · brand_id 可选)。
    """
    require_brand_access(request, brand_id, allow_null=True)


def _taxonomy_brand(brand_id: Optional[int]) -> Optional[dict]:
    """[WO_267] 行业判定的品牌上下文(名称 / 备注 / 种子词 / 用户选定的大类)。

    🔴 只在 `_verify_brand_owner` **之后**调(读的是品牌字段)。读不到 ⇒ None = 只按行业原文判。
    """
    if not brand_id:
        return None
    try:
        from db.diagnosis_db import get_brand_by_id
        return get_brand_by_id(int(brand_id))
    except Exception as exc:
        logger.warning("[selfserve] 读品牌上下文失败 brand_id=%s: %s", brand_id, str(exc)[:200])
        return None


# ==================== 工具 ====================


# ==================== 定价:唯一来源 ====================
# [2026-07-28 块A] 原 `_floor_price()` 已删:它是旧动态价的兜底(算成 0 就抬到 10)。
#   新口径基础价来自 `feature_pricing`,"没配好"不该被悄悄抬到 10 算力卖掉 ——
#   改由 `_require_priced()` 在扣费前 fail-closed 拦住。留着这个 helper 只会诱导后人
#   再造一条「自己算一份价」的旁路,那正是本单要根治的病。
# [2026-07-28 块A · Owner 拍板固定标价] 旧口径是「成本 × 2 × 130 向上取整到 10」的动态价,
# 且预览(draft)与实扣(freeze)各算各的 —— 本单要根治的就是这个「两处各算各的」。
#
# 新口径 SSOT(`OmniRank_定价成本_生产真值_SSOT_2026-06-27.md` §1):
#   客户最终价 = ( 算力数量 ÷ 130 × 厂家进货折扣 d ) × 服务商加价系数 k
#   —— **算力的「量」全平台固定**,元单价按身份变(d/k 机制都已存在,本单不动)。
# 所以「量」= `feature_pricing.cost_points`(基础价,部署时 UPDATE 成 3900)
#          + 超出含题数的加价(extra_cost)。
#
# Owner 答复(2026-07-28 定稿,取代此前互相冲突的 A-Q1/A-Q2):
#   · 基础价 3900 算力 **含 15 题**;
#   · 16..20 题每题加价(见下方常量)。
# 加价数值的由来(用系统自己的成本模型算,不是拍的):
#   `estimate_selfserve_round_cost` 的成本曲线**完全线性** —— 无固定成本,
#   所以「第 16 题」与「第 1 题」的成本相同。按仓内定价规则
#   `selfserve_price_points` = 成本 ×2(毛利)×130(元→算力)向上进 10:
#       单题边际成本 × 2 × 130 → 向上进 10(开源版成本单价为示例值,本常量随之)
SELFSERVE_BASE_INCLUDED_PROMPTS = 15   # 基础价含到第几题(Owner 2026-07-28 定)
SELFSERVE_OVERAGE_POINTS_PER_PROMPT = 200  # 第 16 题起每题加价 = 边际成本 ×2 ×130 进 10


def _selfserve_base_points() -> int:
    """基础价 = `feature_pricing.cost_points`(与 freeze_points 读同一行,保证预览==实扣)。

    读不到 / 未配置 → 返回 0,由 `_require_priced()` fail-closed 拦住(不静默贱卖)。
    """
    try:
        from db.wallet_db import get_feature_pricing
        return int(get_feature_pricing(FEATURE_CODE).get("cost_points") or 0)
    except Exception as exc:  # 未 seed / 读库失败
        logger.warning("[selfserve] 读 feature_pricing(%s) 失败: %s", FEATURE_CODE, exc)
        return 0


def _selfserve_overage_points(prompt_count) -> int:
    """超出含题数的加价算力 —— 也就是传给 `freeze_points(extra_cost=...)` 的那部分。

    `middleware/billing.py` 里 `total_cost = pricing["cost_points"] + extra_cost`,
    所以 extra_cost **只能装加价**,不能再装一份完整价(旧代码传的是完整动态价 → 会 3900+动态价 超收)。
    """
    try:
        n = int(prompt_count or 0)
    except Exception:
        n = 0
    over = max(0, n - int(SELFSERVE_BASE_INCLUDED_PROMPTS))
    return over * int(SELFSERVE_OVERAGE_POINTS_PER_PROMPT)


def _selfserve_price_points(prompt_count, base_points: Optional[int] = None) -> int:
    """**唯一价目函数**:预览价 / 落库价 / 实扣价三处全部走这里,禁止任何一处自己再算一遍。"""
    base = _selfserve_base_points() if base_points is None else int(base_points)
    return base + _selfserve_overage_points(prompt_count)


def _require_priced() -> int:
    """扣费前 fail-closed:定价没配好就不卖(而不是按底价 10 算力贱卖 ¥17 成本的服务)。

    🔴 部署顺序铁律:本单的 `UPDATE feature_pricing SET cost_points=3900 ...` **必须先于代码切流量**。
       没跑 SQL 时基础价=0,此处直接 503,发起入口关闭 —— 宁可短暂不可用,不可静默贱卖。
    """
    base = _selfserve_base_points()
    if base < PRICE_FLOOR_POINTS:
        logger.error("[selfserve] feature_pricing.%s.cost_points=%s 未配置(<%s),拒绝发起",
                     FEATURE_CODE, base, PRICE_FLOOR_POINTS)
        raise HTTPException(status_code=503, detail={
            "code": "pricing_not_configured",
            "message": "该功能定价未配置,暂不可用,请联系客服",
        })
    return base


def _throttle_draft(user_id: int) -> None:
    """[R#8] 每用户 _DRAFT_THROTTLE_SECONDS 内至多 1 次 /draft;超频 → 429。

    fail-soft:节流表本身异常绝不阻断预览(只有真超频才抛 429)。窗口内存态,防内存膨胀在
    表过大时清过期项。窗口=0(env 置 0)则完全关闭。
    """
    if _DRAFT_THROTTLE_SECONDS <= 0:
        return
    try:
        now = time.monotonic()
        last = _draft_last_call.get(int(user_id))
        if last is not None and (now - last) < _DRAFT_THROTTLE_SECONDS:
            wait = int(_DRAFT_THROTTLE_SECONDS - (now - last)) + 1
            raise HTTPException(status_code=429, detail={
                "code": "draft_too_frequent",
                "message": f"预览过于频繁,请 {wait} 秒后再试",
            })
        _draft_last_call[int(user_id)] = now
        if len(_draft_last_call) > 10000:  # 轻量兜底:超阈清过期项,避免无界增长
            cutoff = now - _DRAFT_THROTTLE_SECONDS
            for uid in [u for u, t in _draft_last_call.items() if t < cutoff]:
                _draft_last_call.pop(uid, None)
    except HTTPException:
        raise
    except Exception:
        pass  # 节流异常不阻断预览


def _days_ago(dt) -> int:
    if not isinstance(dt, datetime):
        return 0
    try:
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return max(0, (now - dt).days)
    except Exception:
        return 0


def _iso(dt):
    return dt.isoformat() if isinstance(dt, datetime) else dt


def _merged_note(resolved: dict) -> str:
    """归并结果对用户的人话说明(F1 弹窗明示用)。"""
    name = resolved.get("industry_name") or ""
    raw = resolved.get("user_industry_raw") or ""
    rb = resolved.get("resolved_by")
    if resolved.get("is_new"):
        return f"识别为新行业「{name}」,将为其建立调研档案"
    if rb == "alias_cache":
        return f"已归并到已有行业「{name}」"
    if rb == "llm":
        return f"「{raw}」已归并到行业「{name}」"
    return f"按你填写的「{raw or name}」进行调研"


def _resolved_public(resolved: dict) -> dict:
    return {
        "industry_name": resolved.get("industry_name"),
        "industry_key": resolved.get("industry_key"),
        "is_new": resolved.get("is_new"),
        "resolved_by": resolved.get("resolved_by"),
        "merged_note": _merged_note(resolved),
        # [WO_267] 判出的行业大类(弹窗显示 + 可改选;改选后带 category_key 重新 /draft)
        "category": resolved.get("category"),
    }


def _fetch_active_prompts(industry_id: Optional[int]) -> List[dict]:
    """该行业现有 active prompts → [{id, text}]。"""
    if not industry_id:
        return []
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, prompt_text FROM geo_research_prompts "
            "WHERE industry_id = %s AND active = TRUE ORDER BY sort_order, id",
            (int(industry_id),),
        )
        return [{"id": r["id"], "text": r["prompt_text"]} for r in (cur.fetchall() or [])]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _clean_texts(texts: List[str], existing_lower: set) -> List[str]:
    """去空白/去重(自身 + 已存在),保序。existing_lower 是已占用 prompt_text 的 lower 集合。"""
    out: List[str] = []
    seen = set(existing_lower)
    for t in texts or []:
        s = (t or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _insert_new_prompts(industry_id: int, texts: List[str]) -> List[dict]:
    """把已去重的新题目落库,命中现有则复用其 id。返回 [{id, text, is_new}]。

    [R#7] 新建题一律以 **active=FALSE** 落库(source='ai_generated'):付费成功前不进跑批/榜。
    端点在 freeze 冻结 + promote pending→queued 都成功(付费已锁定)之后,才对本次【新建】的 id
    调 activate_selfserve_prompts 激活。任何 cancel 路径(402/freeze/promote 失败)新题保持 FALSE =
    无害僵尸(cron/榜只捞 active=TRUE),无需补偿删除。is_new 区分本次新插(需激活)与复用既有 id。
    """
    if not texts:
        return []
    conn = get_connection()
    out: List[dict] = []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, prompt_text FROM geo_research_prompts WHERE industry_id = %s",
            (int(industry_id),),
        )
        existing = {(r["prompt_text"] or "").strip().lower(): r["id"] for r in (cur.fetchall() or [])}
        cur.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM geo_research_prompts WHERE industry_id = %s",
            (int(industry_id),),
        )
        max_so = int(cur.fetchone()["m"])
        for s in texts:
            k = s.lower()
            if k in existing:
                out.append({"id": existing[k], "text": s, "is_new": False})
                continue
            max_so += 1
            # active=FALSE:付费锁定(promote 成功)后由 activate_selfserve_prompts 再激活。
            cur.execute(
                "INSERT INTO geo_research_prompts "
                "(industry_id, prompt_text, sort_order, active, source, created_at, updated_at) "
                "VALUES (%s, %s, %s, FALSE, 'ai_generated', NOW(), NOW()) RETURNING id",
                (int(industry_id), s, max_so),
            )
            rid = cur.fetchone()["id"]
            existing[k] = rid
            out.append({"id": rid, "text": s, "is_new": True})
        conn.commit()
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _deactivate_empty_industry_best_effort(industry_id: int, current_task_id: Optional[int] = None) -> None:
    """[出口审核 F2] 已下沉到 db.research_selfserve_db.deactivate_empty_industry_best_effort(API + worker
    reaper 共用·双守卫防误删)。本 wrapper 保留原名让现有调用点(persist-except / promote-fail)零改动。"""
    from db.research_selfserve_db import deactivate_empty_industry_best_effort
    deactivate_empty_industry_best_effort(industry_id, current_task_id)


# ==================== LLM 建议题(deepseek-v4-flash · fail-soft) ====================


def _fallback_suggest_prompts(industry_name: str, n: int = SUGGEST_PROMPT_COUNT) -> List[str]:
    """LLM 不可用时的口语兜底建议题(永不阻断:总能给出一组)。"""
    name = (industry_name or "").strip() or "该行业"
    base = [
        f"{name}哪个品牌好",
        f"{name}推荐",
        f"{name}怎么选",
        f"{name}十大排名",
        f"{name}哪家专业",
        f"{name}性价比高的推荐",
    ]
    return base[: max(1, n)]


async def _llm_suggest_prompts(industry_name: str, n: int = SUGGEST_PROMPT_COUNT) -> List[str]:
    """为行业生成 n 条高价值调研问题(不落库,仅预览)。任何失败 → 兜底,绝不阻断。"""
    industry_name = (industry_name or "").strip()
    if not industry_name:
        return _fallback_suggest_prompts(industry_name, n)
    try:
        import httpx
        from services.research_monitor.industry_resolver import (
            _dashscope_key,
            DASHSCOPE_COMPAT_URL,
            DEFAULT_RESOLVER_MODEL,
        )
        from services.research_monitor.article_intent_classifier import _extract_json_object
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        api_key = _dashscope_key()
        if not api_key:
            return _fallback_suggest_prompts(industry_name, n)

        system = (
            "你是中文 GEO 调研选题助手。为给定行业生成用户在 AI 搜索里最可能问的、"
            "能触发品牌/产品/服务商推荐的高价值口语问题。只返回 JSON。"
        )
        user = (
            f"行业: {industry_name}\n"
            f"生成 {n} 个中文调研问题,每个是终端用户会向 AI 助手提问的自然口语问题"
            "(如「XX 哪个品牌好」「XX 推荐」「XX 怎么选」),要能引出 AI 给出品牌/产品/服务商推荐。"
            "不要带具体公司名。只返回 JSON:{\"prompts\": [\"问题1\", \"问题2\"]}"
        )
        payload = {
            "model": DEFAULT_RESOLVER_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with llm_track(
                "research_monitor", "dashscope",
                model=DEFAULT_RESOLVER_MODEL,
                metadata={"task": "selfserve_suggest_prompts"},
            ) as tracker:
                try:
                    resp = await client.post(DASHSCOPE_COMPAT_URL, headers=headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    it, ot, ct = usage_from_response_payload(data)
                    tracker.record(input_tokens=it, output_tokens=ot, cached_tokens=ct, success=True)
                except Exception as exc:
                    tracker.record(success=False, error_msg=str(exc)[:500])
                    raise
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        obj = _extract_json_object(content)
        raw = obj.get("prompts") or obj.get("questions") or []
        out: List[str] = []
        seen = set()
        for p in raw:
            s = str(p or "").strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
        return out[:n] if out else _fallback_suggest_prompts(industry_name, n)
    except Exception as exc:
        logger.warning("[selfserve] LLM 生成建议题失败,降级兜底: %s", str(exc)[:200])
        return _fallback_suggest_prompts(industry_name, n)


def _task_response(task: dict, resolved: dict, *, note: Optional[str] = None) -> dict:
    tid = task.get("id")
    out = {
        "task_id": tid,
        "status": task.get("status"),
        "price_points": task.get("price_points"),
        "resolved": _resolved_public(resolved),
        "poll_url": f"/api/publish/research/task/{tid}",
        "reused": True,
    }
    if note:
        out["note"] = note
    return out


# ==================== 端点 1:POST /draft(免费预览,不冻结) ====================


@router.post("/draft")
async def draft(req: DraftRequest, request: Request):
    """免费预览:解析行业(免费 · 不落库不扣费 · 仅 LLM 归并判断)→ 现有题目 + LLM 建议题
    → 预览价 + 新鲜度知情。

    [R#8] 行业解析走 preview_resolve(persist=False)【只读】:不 ensure_research_industry、
    不 write_alias —— 未付费端点绝不往共享行业表 / 别名表写,也不让 auto-created 垃圾行业进选项池。
    别名沉淀留给正式 /self-serve(那次 persist=True)。另加每用户轻量频控防高频刷 LLM(超频 429)。
    """
    actor = _require_logged_in_user(request)
    user_id = actor["user_id"]
    _throttle_draft(user_id)  # [R#8] 免费端点防高频刷 LLM(fail-soft · 超频 429)
    _verify_brand_owner(request, req.brand_id)  # [GEO-R2-CAN-038] 共享 RBAC(含分配客户)

    resolved = await preview_resolve(req.industry or "", taxonomy=True,
                                     brand=_taxonomy_brand(req.brand_id), category_key=req.category_key)
    industry_id = resolved.get("industry_id")
    industry_key = resolved.get("industry_key")
    industry_name = resolved.get("industry_name")

    existing = _fetch_active_prompts(industry_id)
    suggested: List[str] = []
    if resolved.get("is_new") or len(existing) < SUGGEST_PROMPT_COUNT:
        suggested = await _llm_suggest_prompts(industry_name, SUGGEST_PROMPT_COUNT)

    # 默认全选题目数(现有优先,否则建议题数),钳到 1..20 估价
    default_count = len(existing) if existing else len(suggested)
    default_count = max(SELFSERVE_MIN_PROMPTS, min(SELFSERVE_MAX_PROMPTS, default_count or 1))
    # [块A] 预览价与实扣价同源:统一走 `_selfserve_price_points`(基础价读 feature_pricing,
    #   与 freeze_points 读同一行)。一次读出 base 复用,避免同一请求内多次查库。
    _base = _selfserve_base_points()
    _priced = _base >= PRICE_FLOOR_POINTS  # 未配置时前端据此禁用发起,不展示假价
    default_price = _selfserve_price_points(default_count, base_points=_base)
    per_one = _selfserve_price_points(1, base_points=_base)

    industry_last = None
    if industry_key:
        last = get_industry_last_selfserve(industry_key, FRESHNESS_DAYS)
        if last and last.get("finished_at"):
            industry_last = {"days_ago": _days_ago(last.get("finished_at"))}

    # [FIX-1] 服务端恢复源:本人本行业在飞任务(pending/queued/running)。localStorage 只是快路径缓存,
    #   清缓存/换设备/换浏览器都找不回 → 真相在服务端。弹窗 open 时据此直接进 running 态续轮询,
    #   兑现文案「可稍后回来查看」承诺。get_active_selfserve_task 以 user_id 为条件(不跨租户);
    #   new 行业 preview_resolve 的 industry_key=normalize(name) 与 create 落库同源,可匹配。
    active_task = None
    if industry_key:
        at = get_active_selfserve_task(user_id, industry_key)
        if at:
            active_task = {"task_id": at["id"], "status": at["status"]}

    # [U6] 1..20 题各档预览价(纯常量/SQL 口径,无 LLM)。让弹窗按当前勾选题数即时查表,
    #   动态显示总价零额外请求。JSON 序列化时 int key 自动转字符串,前端按 String(count) 查。
    price_by_count = {
        c: _selfserve_price_points(c, base_points=_base)
        for c in range(SELFSERVE_MIN_PROMPTS, SELFSERVE_MAX_PROMPTS + 1)
    }

    return {
        "resolved": _resolved_public(resolved),
        "existing_prompts": existing,
        "suggested_prompts": suggested,
        "default_price_points": default_price,
        "per_count_hint": per_one,
        "price_by_count": price_by_count,
        # [块A] 定价未配置(部署时 SQL 还没跑)→ 前端禁用发起按钮并提示,不展示假价、不让用户白发起被 503
        "pricing_configured": _priced,
        "industry_last_by_others": industry_last,
        "active_task": active_task,  # [FIX-1] 本人本行业在飞任务(服务端恢复源·非属主天然查不到)
    }


@router.get("/active-task")
async def active_task(request: Request, industry: str, brand_id: Optional[int] = None,
                      category_key: Optional[str] = None):
    """[FIX-1] 轻量查本人本行业在飞任务(pending/queued/running)· 服务端恢复源。

    发布中心选中客户/行业时调用:清 localStorage / 换设备 / 换浏览器也能恢复"进行中"指示。
    [出口审核 FIX1-01] 走 resolve_or_create_industry(allow_llm=False·persist=False)【零 LLM 只读】:
      级1 alias 命中(付费过的行业)→ canonical key;级1 miss(未付费 / CSV·admin 录入的行业)→ 级3
      fail-soft 兜底 normalize(raw)——【绝不烧 LLM】。旧版用 preview_resolve(allow_llm=True)每次切客户
      对未付费行业烧一发 deepseek-v4-flash = 成本泄漏 / 可被浏览客户打成 DoS(且无频控)。
      归一同源:fallback 任务落库 industry_key=normalize(raw)、llm-merge 任务落 canonical,两类恢复都精确
      匹配 create 落库键(顺带闭合 FIX1-03 fallback 恢复缝隙)。不建议题不建库。
    非属主天然查不到(get_active_selfserve_task 以 user_id 为条件),不引入跨租户泄露。
    """
    actor = _require_logged_in_user(request)
    user_id = actor["user_id"]
    # [WO_267] 行业判定吃品牌上下文 ⇒ 读品牌前先过 RBAC(原来这里 brand_id 收了却从不校验)
    _verify_brand_owner(request, brand_id)
    resolved = await resolve_or_create_industry(industry or "", allow_llm=False, persist=False,
                                                taxonomy=True, brand=_taxonomy_brand(brand_id),
                                                category_key=category_key)
    industry_key = resolved.get("industry_key")
    at = None
    if industry_key:
        # [出口审核 F2] 传 industry_raw 兜底:write_alias 失败致 llm-merge 任务 key 漂移时,按原文仍能召回。
        row = get_active_selfserve_task(user_id, industry_key, industry_raw=(industry or "").strip() or None)
        if row:
            at = {"task_id": row["id"], "status": row["status"]}
    return {"active_task": at, "industry_key": industry_key}


# ==================== 端点 2:POST /self-serve(付费发起) ====================


@router.post("/self-serve")
async def self_serve(req: SelfServeRequest, request: Request, background_tasks: BackgroundTasks):
    """付费发起自助调研(冻结前只读组装 → freeze → 冻结后 persist 公共池 → 触发消费)。

    [P0-1] freeze 成功前公共池零写入:
      冻结【前】纯只读——preview_resolve(persist=False)绝不 ensure_research_industry / write_alias;
      _fetch_active_prompts 只读现有题作去重基准;不 INSERT 行业 / 别名 / 题。仅写队列表(任务表)。
      freeze 402/异常 → 标 cancelled 冒泡,此刻 3 张公共池表(geo_research_industries /
      geo_research_industry_aliases / geo_research_prompts)0 新增。
      冻结【后】(付费已锁定)才 persist:ensure 行业(new 建)+ write_alias(llm 归并沉淀)+
      _insert_new_prompts(新题落库)。persist 中途失败 → release_freeze + cancel + 尽力清理。
    """
    actor = _require_logged_in_user(request)          # (a) 登录即可(WO_211)
    user_id = actor["user_id"]
    _verify_brand_owner(request, req.brand_id)  # [GEO-R2-CAN-038] 共享 RBAC(含分配客户)

    # (b) [P0-1] 冻结前【只读】归并:preview_resolve(persist=False)绝不写共享行业表 / 别名表。
    #   merge/alias 命中 → 真实 industry_id(只读 SELECT);new 决策 → industry_id=None(待 freeze 后建)。
    #   freshness / idempotency / 计价全程只需 industry_key(=normalize(规范名)),不需要 industry_id。
    resolved = await preview_resolve(req.industry or "", taxonomy=True,
                                     brand=_taxonomy_brand(req.brand_id), category_key=req.category_key)
    preview_industry_id = resolved.get("industry_id")     # merge/alias 命中真实 id;new → None
    industry_key = resolved.get("industry_key")
    industry_name = resolved.get("industry_name") or ""
    industry_raw = resolved.get("user_industry_raw") or req.industry
    resolved_by = resolved.get("resolved_by")
    confidence = resolved.get("confidence")
    if not industry_key or not industry_name:
        raise HTTPException(status_code=500, detail={"code": "industry_resolve_failed", "message": "行业识别失败,请重试"})

    # (b.2) [R#4] 行业名过泛(通用/全部行业类)→ 验收 get_publish_media_board 会把 industry_key 置空、
    #   scope 退成 all_industry(≠'industry')→ 验收永假 → 完成也退款,但 4 引擎成本已花 = 平台净亏。
    #   冻结【之前】(此处 industry_key 已算出,只读)直接 422 拒绝,不进付费链、不写公共池。
    from services.media_entity_flywheel import is_all_industry_scope
    if is_all_industry_scope(industry_name) or is_all_industry_scope(industry_key):
        raise HTTPException(status_code=422, detail={
            "code": "industry_too_generic",
            "message": "请填写更具体的行业(如「杭州电梯维保」,而非「通用 / 全部行业」)",
        })

    # (c) 用户级新鲜度闸(7 天内已调研过本行业 → 409 · 只用 industry_key)
    ulast = get_user_last_selfserve(user_id, industry_key, FRESHNESS_DAYS)
    if ulast:
        days = _days_ago(ulast.get("finished_at")) if ulast.get("finished_at") else 0
        raise HTTPException(status_code=409, detail={
            "code": "already_researched",
            "message": f"你 {days} 天前已调研过本行业,{max(0, FRESHNESS_DAYS - days)} 天后可再次调研",
            "last_task_id": ulast.get("id"),
        })

    # (d) 组装题目集(只读 · 校验 1..20 · 不落库)。merge/alias 命中 → _fetch_active_prompts 作去重基准;
    #   new 行业 preview_industry_id=None → _fetch_active_prompts 返 [](无现有题)。
    existing = _fetch_active_prompts(preview_industry_id)
    existing_by_id = {int(p["id"]): p for p in existing}
    selected: List[dict] = []
    picked_ids: set = set()
    for pid in (req.selected_prompt_ids or []):
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if pid in existing_by_id and pid not in picked_ids:
            picked_ids.add(pid)
            selected.append(existing_by_id[pid])
    occupied_lower = {(p["text"] or "").strip().lower() for p in existing} | {(s["text"] or "").strip().lower() for s in selected}
    new_clean = _clean_texts(req.new_prompt_texts or [], occupied_lower)
    n = len(selected) + len(new_clean)
    if n < SELFSERVE_MIN_PROMPTS or n > SELFSERVE_MAX_PROMPTS:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_prompt_count",
            "message": f"题目数必须在 {SELFSERVE_MIN_PROMPTS}..{SELFSERVE_MAX_PROMPTS} 之间,当前 {n}",
            "count": n,
        })

    # (e) 幂等/双击:已有活跃任务 → 直接返回,不二次冻结、不落新题
    active = get_active_selfserve_task(user_id, industry_key)
    if active:
        return _task_response(active, resolved, note="已有进行中的自助调研任务")

    # (f) 组装 prompt_snapshot【不落库】:选中现有 {id,text} + 新题 {id:None,text}。
    #   新题的真实 id 在 freeze 后 persist 阶段 _insert_new_prompts 才拿到,再回填 snapshot。
    prompt_snapshot: List[dict] = []
    snap_ids: set = set()
    for p in selected:
        pid = int(p["id"])
        if pid in snap_ids:
            continue
        snap_ids.add(pid)
        prompt_snapshot.append({"id": pid, "text": p["text"]})
    for t in new_clean:
        prompt_snapshot.append({"id": None, "text": t})

    # (f') 计价(>0,绝不 free 静默)
    # [块A] 与预览同源:base 读 feature_pricing(freeze_points 待会读的是同一行)。
    #   `_require_priced()` 在扣费前 fail-closed —— 定价没配好宁可 503 也不贱卖。
    base_points = _require_priced()
    overage_points = _selfserve_overage_points(n)
    price_points = _selfserve_price_points(n, base_points=base_points)  # 落库=用户实付总额

    # (g) idempotency_key(配合 U1 活跃态唯一索引防并发双击)
    idem = f"{user_id}:{industry_key}"

    # (h) create task(队列表 · 任务表非公共池,允许写;industry_id 可 None = new 待 freeze 后建)
    try:
        task_id = create_selfserve_task(
            user_id, req.brand_id, industry_raw, preview_industry_id, industry_key,
            price_points, prompt_snapshot, idem,
        )
    except Exception as exc:
        # 并发双击撞活跃态唯一索引(pending/queued/running)→ 返回已有活跃任务;否则 500
        active2 = get_active_selfserve_task(user_id, industry_key)
        if active2:
            return _task_response(active2, resolved, note="已有进行中的自助调研任务")
        logger.exception("[selfserve] create_selfserve_task 失败 user=%s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail={"code": "create_failed", "message": "创建任务失败,请重试"})

    # (i) freeze(余额不足 → 先标 cancelled 再冒泡 402;admin/零成本 freeze_id=None 走豁免)。
    #   [P0-1] 此刻公共池仍 0 写入 —— 402/异常直接 cancel 冒泡,无任何行业/别名/题孤儿需清理。
    fr = None
    try:
        from middleware.billing import freeze_points
        fr = await freeze_points(
            user_id=user_id,
            feature_code=FEATURE_CODE,
            task_ref=f"selfres_{task_id}",
            brand_id=req.brand_id,
            # [块A · 超收根因] 旧代码传的是完整动态价,而 billing 里
            #   total_cost = pricing["cost_points"] + extra_cost
            #   → 基础价改 3900 后会变成「3900 + 动态价」。extra_cost 只能装**加价部分**。
            extra_cost=overage_points,
            reason="GEO 单行业自助调研",
        )
        # 🔴 [#79] 记下这次到底扣没扣 —— 错误响应的信封从这里取。
        #    没写过 ⇒ 处理器兜底 `no`(还没走到扣费那步),那是安全的默认。
        try:
            from middleware.billing import charged_envelope as _chg_env
            request.state.charged_envelope = _chg_env(fr)
        except Exception:
            pass        # 信封是附加信息,绝不因它影响主流程
    except HTTPException:
        # 余额不足(402)等 → 标 cancelled 再原样冒泡(此时尚未冻结、公共池 0 写入,无孤儿)
        try:
            set_selfserve_task_status(task_id, "cancelled", failed_reason="insufficient_balance")
        except Exception:
            pass
        raise
    except Exception as exc:
        try:
            set_selfserve_task_status(task_id, "cancelled", failed_reason="freeze_error")
        except Exception:
            pass
        logger.exception("[selfserve] freeze_points 失败 task=%s: %s", task_id, exc)
        raise HTTPException(status_code=500, detail={"code": "freeze_failed", "message": "扣费冻结失败,请重试"})

    freeze_id = fr.get("freeze_id")
    freeze_table = fr.get("freeze_table")

    # ---- 冻结后统一退款:release_freeze(带 user_id + freeze_table · V3.5 路由铁律)· best-effort ----
    async def _release_freeze_quiet(reason: str) -> None:
        if not freeze_id:
            return
        try:
            from middleware.billing import release_freeze
            await release_freeze(
                freeze_id=freeze_id, task_ref=f"selfres_{task_id}",
                user_id=user_id, freeze_table=freeze_table, reason=reason,
            )
        except Exception:
            pass  # release 失败仍有 12h freeze_sweeper 兜底(且已被排除,不误当僵尸)

    # (i.2) [R#2] freeze 回填与冻结【分开处理】:freeze_points 已 commit 冻结成功后,若回填抛错,
    #   必须主动 release 已冻结的钱再标 cancelled——否则钱成 12h 孤儿冻结 + 重试双冻。
    if freeze_id:
        try:
            update_selfserve_task_freeze(task_id, freeze_id, freeze_table)
        except Exception as exc:
            await _release_freeze_quiet("回填 freeze 失败·即时退款")
            try:
                set_selfserve_task_status(task_id, "cancelled", failed_reason="freeze_persist_failed")
            except Exception:
                pass
            logger.exception("[selfserve] task=%s 回填 freeze 失败·已即时退款: %s", task_id, exc)
            raise HTTPException(status_code=500, detail={"code": "freeze_persist_failed", "message": "扣费登记失败,已退款,请重试"})
    else:
        # [R#1] freeze_id=None = admin 豁免 / 零成本免费 → 标 billing_exempt=True,
        #   worker 据此把「合法免费」与「freeze 未回填(孤儿)」区分开,不静默免费交付付费任务。
        # [FIX-3 · P1 源头 fail-closed] exempt 标记是 worker 区分「合法免费」vs「孤儿」的唯一依据。
        #   旧实现失败只 warning 继续 → 任务停留 {freeze_id=NULL, exempt=FALSE} 仍被 promote 成 queued
        #   → worker 孤儿守卫无限弹回队头 = 永久 FIFO 饥饿堵死所有后续付费单。故与 persist/promote
        #   失败对称:标记失败(抛异常 OR set 返回 False=0 行命中)→ 立即 cancel(freeze_id 本就
        #   None 无钱可退,无需 release)+ 禁止继续 persist/promote(在此 raise,保证公共池零新增)。
        exempt_ok = False
        try:
            exempt_ok = set_selfserve_task_billing_exempt(task_id, True)
        except Exception as exc:
            logger.exception("[selfserve] task=%s 标 billing_exempt 抛异常: %s", task_id, exc)
        if not exempt_ok:
            try:
                set_selfserve_task_status(
                    task_id, "cancelled",
                    failed_reason="billing_exempt_persist_failed", mark_finished=True,
                )
            except Exception:
                pass
            logger.error(
                "[selfserve] task=%s 标 billing_exempt 失败(未命中/异常)·已取消·禁止入队(防孤儿队头饥饿)",
                task_id,
            )
            raise HTTPException(
                status_code=500,
                detail={"code": "billing_exempt_persist_failed", "message": "任务登记失败,请重试"},
            )

    # (i.3) [P0-1] 付费已锁定(freeze 冻结)→ 才 persist 公共池。任何失败 → release + cancel + 尽力清理。
    #   持久化三件:① new 行业 ensure_research_industry 建(create 时 industry_id=None)+ 回填 task
    #   ② write_alias 沉淀(仅 llm 归并;fallback 无置信映射不写、alias_cache 已存在)
    #   ③ _insert_new_prompts 落新题(active=FALSE)→ 拿真实 id 回填 prompt_snapshot(worker 用 snapshot 建轮)。
    new_prompt_ids: List[int] = []
    newly_created_industry_id: Optional[int] = None   # 本次 persist 新建 / 点亮的行业(失败时尽力停用)
    try:
        persist_industry_id = preview_industry_id
        if resolved_by == "taxonomy" and (persist_industry_id is None
                                         or not resolved.get("taxonomy_row_active")):
            # [WO_267 · Review Q2 付费即点亮] 落到行业大类字典指定的那一行:没建 ⇒ 按**字典 slug** 建
            #   (不按 name:管理员改过名的行会被当成不存在);停用 ⇒ 翻 active。只改「落到哪一行」,
            #   付费语义与原来「判 new ⇒ 付费后现场建活跃行业」一致;失败撤回走同一个尽力停用。
            from services.research_monitor.industry_registry import ensure_taxonomy_research_row
            _tid, _created, _activated = ensure_taxonomy_research_row(
                resolved.get("taxonomy_slug") or "", industry_name)
            if not _tid:
                raise RuntimeError("ensure_taxonomy_research_row 返回空")
            if _created or _activated:
                newly_created_industry_id = int(_tid)
            if persist_industry_id is None:
                update_selfserve_task_industry_id(task_id, int(_tid))
            persist_industry_id = int(_tid)
        elif persist_industry_id is None:
            # new 行业:付费后才建(此前 preview 只读,未写共享表)。
            # [#2/#5] 用 return_created 精确判「本请求是否真 INSERT 了该行业」:并发同名两用户各判 new
            #   (freshness/idempotency 都 per-user 不互斥)→ ensure 是 find-or-create,胜者真 INSERT
            #   was_created=True,败者 ON CONFLICT re-SELECT 拿胜者 id → was_created=False。只有 was_created=True
            #   的请求才把该行业记为 newly_created;否则(命中他人刚建的)绝不在自身 persist 失败时软删他人行业。
            persist_industry_id, _was_created = ensure_research_industry(
                industry_name, return_created=True
            )
            if not persist_industry_id:
                raise RuntimeError("ensure_research_industry 返回空")
            newly_created_industry_id = int(persist_industry_id) if _was_created else None
            update_selfserve_task_industry_id(task_id, int(persist_industry_id))

        # 别名沉淀:仅 llm 归并写(第二次起零 LLM 命中级 1)。fallback 无置信映射不写(CHECK 只许 llm|admin);
        #   alias_cache 命中说明别名已存在,无需重写。write_alias 非关键 · 失败不阻断付费轮。
        if resolved_by == "llm":
            try:
                # 🔴 2026-08-09:`write_alias` 已改成**仅首次写入 + 写后重读**
                #    (原来是无条件 UPSERT,会把 admin 人工改判盖掉)。
                #    本处**不改本任务已经确定的行业** —— 那是上面 ensure 出来的、
                #    付费已锁定的结果,改它会让用户付了钱拿到另一个行业。
                #    这里只把"别名归谁"这件事的分歧记下来给 admin 看。
                _eff = write_alias(industry_raw, int(persist_industry_id),
                                   confidence, resolved_by="llm")
                if _eff and int(_eff.get("industry_id") or 0) != int(persist_industry_id):
                    logger.warning(
                        "[selfserve] task=%s 别名 %r 已被 %s 定为 industry_id=%s,"
                        "本任务用的是 %s —— 别名未被覆盖(设计如此),请 admin 核对",
                        task_id, industry_raw, _eff.get("resolved_by"),
                        _eff.get("industry_id"), persist_industry_id)
            except Exception as exc:
                logger.warning("[selfserve] task=%s write_alias 失败(不阻断): %s", task_id, str(exc)[:200])

        # 新题落库(active=FALSE · source='ai_generated')→ [R#7] promote 成功后才 activate。
        new_prompts = _insert_new_prompts(int(persist_industry_id), new_clean)
        new_prompt_ids = [int(p["id"]) for p in new_prompts if p.get("is_new") and p.get("id") is not None]

        # 用真实 id 回填 prompt_snapshot(选中现有 + 新题 · 去重保序)
        final_snapshot: List[dict] = []
        seen_ids: set = set()
        for p in selected:
            pid = int(p["id"])
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            final_snapshot.append({"id": pid, "text": p["text"]})
        for p in new_prompts:
            pid = int(p["id"]) if p.get("id") is not None else None
            if pid is not None and pid in seen_ids:
                continue
            if pid is not None:
                seen_ids.add(pid)
            final_snapshot.append({"id": pid, "text": p["text"]})
        update_selfserve_task_prompt_snapshot(task_id, final_snapshot)
    except Exception as exc:
        await _release_freeze_quiet("persist 公共池失败·即时退款")
        # 尽力清理已建部分:新题 active=FALSE = cron/榜不捞的无害僵尸(无需删);本次新建的空行业 best-effort 停用。
        #   传 task_id 让 _deactivate 守卫排除本 task 自身(其它并发活跃 task 引用则不软删 · #5-(b))。
        if newly_created_industry_id:
            try:
                _deactivate_empty_industry_best_effort(newly_created_industry_id, task_id)
            except Exception:
                pass
        try:
            set_selfserve_task_status(task_id, "cancelled", failed_reason="persist_public_pool_failed")
        except Exception:
            pass
        logger.exception("[selfserve] task=%s persist 公共池失败·已退款: %s", task_id, exc)
        raise HTTPException(status_code=500, detail={"code": "persist_failed", "message": "行业/题目登记失败,已退款,请重试"})

    # (i.4) [R#1] persist 完成 → promote pending→queued(此刻才可被 worker 派发,关掉抢派发窗口)
    try:
        # promote 是条件 UPDATE(WHERE id=%s AND status='pending')返 bool,不抛异常。
        # 若 reaper/并发已把 status 从 pending 漂移成 cancelled → 返 False:此时 DB 并未 queued,
        # 绝不能继续 activate 新题/触发消费/返回 queued(最坏:钱已被 reaper 退、公共池新题却被激活)。
        # 显式 raise 让下方 except 统一走 release + cancel + 停用新建行业 + 500。
        promoted = promote_selfserve_task_to_queued(task_id)
        if not promoted:
            raise RuntimeError("promote 返回 False·status 在 queued 前已漂移(reaper/并发改成 cancelled)")
    except Exception as exc:
        # promote 失败 → 任务卡 pending 不会被跑;主动退款避免孤儿冻结
        await _release_freeze_quiet("promote 失败·即时退款")
        # [#1] 与 persist-except 对称:promote 失败也尽力停用本次新建的空行业(commit 4a97852f 承诺
        #   「persist/promote 任一失败 → 尽力停用本次新建空行业」,原仅 persist-except 有,此处补齐)。
        #   传 task_id 让守卫排除本 task 自身(#5-(b))。
        if newly_created_industry_id:
            try:
                _deactivate_empty_industry_best_effort(newly_created_industry_id, task_id)
            except Exception:
                pass
        try:
            set_selfserve_task_status(task_id, "cancelled", failed_reason="promote_failed")
        except Exception:
            pass
        logger.exception("[selfserve] task=%s promote 失败: %s", task_id, exc)
        raise HTTPException(status_code=500, detail={"code": "promote_failed", "message": "任务入队失败,已退款,请重试"})

    # (i.5) [R#7] 付费已锁定(freeze 冻结 + persist + promote 都成功)→ 激活本次【新建】的题(insert 时 active=FALSE)。
    #   在此之前任何 cancel 路径(402/freeze/persist/promote 失败)新题都保持 active=FALSE = cron/榜不捞的无害僵尸。
    #   自助本轮用 prompt_snapshot(已回填真实 id)跑,与 active 标志无关;active=TRUE 只影响未来 cron 是否复用。
    #   fail-soft:激活失败仅退化为「本轮跑但不进未来 cron」,不阻断。
    if new_prompt_ids:
        try:
            activate_selfserve_prompts(new_prompt_ids)
        except Exception as exc:
            logger.warning("[selfserve] task=%s 激活新建题失败(未来 cron 不复用·不阻断): %s", task_id, str(exc)[:200])

    # (j) 触发队列消费(BackgroundTask · new_event_loop 包装)
    try:
        from services.research_monitor.selfserve_worker import consume_selfserve_queue_sync
        background_tasks.add_task(consume_selfserve_queue_sync)
    except Exception as exc:
        # 触发失败不阻断:scheduler 周期 job 会兜底消费
        logger.warning("[selfserve] 触发消费失败(scheduler 兜底): %s", str(exc)[:200])

    logger.info("[selfserve] task=%s user=%s industry_key=%s n=%s price=%s", task_id, user_id, industry_key, n, price_points)
    return {
        "task_id": task_id,
        "status": "queued",
        "price_points": price_points,
        "resolved": _resolved_public(resolved),
        "poll_url": f"/api/publish/research/task/{task_id}",
    }


# ==================== 端点 3:GET /task/{task_id}(轮询状态) ====================


@router.get("/task/{task_id}")
async def get_task(task_id: int, request: Request):
    """轮询自助调研任务状态(RBAC:仅本人或 admin)。"""
    actor = _require_logged_in_user(request)
    user_id = actor["user_id"]
    task = get_selfserve_task(task_id)
    # [R#9] 消除枚举 oracle:task_id 是密集 BIGSERIAL,若「不存在」返 404 而「他人任务」返 403,
    #   状态码差异 = 跨租户探测存在性的旁路。非 admin 且非属主 → 与「不存在」返回【完全相同】的 404
    #   task_not_found(先判属主,非属主一律当作不存在)。admin 保留全量可见,属主正常轮询不受影响。
    if not task or (not actor["is_admin"] and int(task.get("user_id") or 0) != user_id):
        raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": "任务不存在"})
    return {
        "task_id": task.get("id"),
        "status": task.get("status"),
        "round_id": task.get("round_id"),
        "price_points": task.get("price_points"),
        "failed_reason": task.get("failed_reason"),
        "industry_key": task.get("industry_key"),
        "created_at": _iso(task.get("created_at")),
        "started_at": _iso(task.get("started_at")),
        "finished_at": _iso(task.get("finished_at")),
    }


# ==================== 端点 4/5:本轮结果报表 + 历轮回看(WO §3) ====================
#
# 生产实证(2026-08-05 Owner 亲测单 queue id 3 / round_20260805_102956_659694):
#   反哺**其实成功了**(4 次引擎调用、29 条引用落库、榜数据源已更新),
#   断的是交付面 —— 完成只有一句 toast,无结果视图、无「已反哺」明示,
#   且该单归并进已有行业 → 用户肉眼看不出任何变化,**花 3900 算力像什么都没发生**。
#
# 🔴 两个端点都**零新增采集**:按 round_id 从既有落库数据直出,不调引擎、不调 LLM
#    (§1 域名人话名走目录缓存,缺名时只**后台**排队,不在本请求里等)。


@router.get("/round-report/{round_id}")
async def round_report(round_id: str, request: Request):
    """本轮调研结果报表(只读 · RBAC:仅本人或 admin)。

    🔴 **round_id 不是凭证**:它形如 `round_20260805_102956_659694`,是可推测的
    时间戳串。归属只在自助任务行上(`geo_research_selfserve_queue.user_id`),
    故先反查任务行再判属主 —— 与 `/task/{task_id}` 同一条 R#9 纪律:
    **非属主与不存在返回完全相同的 404**,不给跨租户探测存在性的旁路。

    fail-soft(工单 §3.4):报表取数失败 → `has_data=false` 的空报表,
    由前端显示「结果整理中,稍后回来看」,绝不 500 打断主流程。
    """
    actor = _require_logged_in_user(request)
    task = get_selfserve_task_by_round(round_id)
    if not task or (not actor["is_admin"] and int(task.get("user_id") or 0) != actor["user_id"]):
        raise HTTPException(status_code=404, detail={"code": "round_not_found", "message": "调研轮次不存在"})

    from services.research_monitor.round_report import build_round_report

    try:
        report = build_round_report(str(task.get("round_id") or round_id))
    except Exception as exc:
        logger.warning("[selfserve] round-report 失败(返回空报表): %s", str(exc)[:200])
        report = {"round_id": round_id, "has_data": False}

    industry_key = str(task.get("industry_key") or "")
    return {
        **report,
        "task_id": task.get("id"),
        "industry_key": industry_key,
        "industry_raw": task.get("industry_raw"),
        "price_points": task.get("price_points"),
        "task_status": task.get("status"),
        # [§3.2 反哺明示] 反哺是**自动的**(生产已实证),用户缺的不是一个"一键反哺"
        # 按钮,而是**被告知它发生了**。这句结论随报表出网,前端原样显示。
        "flywheel_note": "本轮数据已自动计入本行业「AI 真实引用媒体榜」。",
    }


@router.get("/rounds")
async def list_rounds(request: Request, industry: str, limit: int = 20,
                      brand_id: Optional[int] = None, category_key: Optional[str] = None):
    """本人在该行业的历轮自助调研(时间 / 题数 / 引用数)· 只读。

    [§3.3] 「花过的算力永远找得回凭证」。列表只给轻量摘要,点开某一轮再调
    `/round-report/{round_id}` 拿明细 —— 列表页不为每一轮跑一次全量聚合。

    行业归一与 `/active-task` 同源(`resolve_or_create_industry(allow_llm=False,
    persist=False)`):**零 LLM 只读**,不会因为翻历史记录烧钱。
    """
    actor = _require_logged_in_user(request)
    _verify_brand_owner(request, brand_id)   # [WO_267] 读品牌上下文前先过 RBAC
    resolved = await resolve_or_create_industry(industry or "", allow_llm=False, persist=False,
                                                taxonomy=True, brand=_taxonomy_brand(brand_id),
                                                category_key=category_key)
    industry_key = str(resolved.get("industry_key") or "")
    if not industry_key:
        return {"industry_key": "", "rounds": []}

    try:
        rows = list_user_selfserve_rounds(actor["user_id"], industry_key, limit=limit)
    except Exception as exc:
        logger.warning("[selfserve] 历轮列表查询失败(返空): %s", str(exc)[:200])
        rows = []

    rounds = []
    for r in rows:
        snapshot = r.get("prompt_snapshot")
        rounds.append({
            "task_id": r.get("id"),
            "round_id": r.get("round_id"),
            "status": r.get("status"),
            "price_points": r.get("price_points"),
            "prompt_count": len(snapshot) if isinstance(snapshot, list) else 0,
            "created_at": _iso(r.get("created_at")),
            "finished_at": _iso(r.get("finished_at")),
        })
    return {"industry_key": industry_key, "rounds": rounds}


# NOTE (D · 可选): 行业别名 admin 只读端点(list_industry_aliases)留给 U5 admin 前端时补,
#   需 _require_admin 鉴权 + db.research_selfserve_db.list_industry_aliases。本文件不放,避免越权面。
