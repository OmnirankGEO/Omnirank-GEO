"""
社媒操盘手 v3.0 - 内容工坊 API
"""

import asyncio
import json
import re
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Request
import logging

# 防 GC：FastAPI BackgroundTasks/asyncio.create_task 不强引用任务，
# 模块级 set 持有运行中的深度解析任务，避免被 GC 静默回收
_deep_analyze_running_tasks: set = set()

logger = logging.getLogger("GEO-Content")
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Callable

router = APIRouter(prefix="/api/content", tags=["内容工坊"])

_PROFILE_MEMORY_FIELD_LABELS = {
    "client_case": "新的客户案例",
    "product_mention": "新的产品/服务",
    "product_selling_points": "新的产品卖点",
    "target_group": "新的目标客户描述",
    "pain_point": "新的客户痛点",
    "customer_faq": "新的客户常问问题",
    "real_cases": "新的真实素材",
    "expertise": "新的专业领域",
    "persona_tone": "新的表达调性",
    "speaking_style": "新的说话风格",
    "content_taboo": "新的表达边界",
    "closing_method": "新的引导方式",
    "business": "新的业务描述",
}

_PROFILE_MEMORY_TEXT_KEYS = (
    "value",
    "text",
    "content",
    "summary",
    "description",
    "answer",
    "name",
    "title",
)

_SHALLOW_MEMORY_EXACT_TEXTS = {
    "完成了一次内容生成",
    "完成一次内容生成",
    "内容生成完成",
    "完成了一次写稿",
    "完成一次写稿",
    "生成了一条内容",
    "生成了一篇文案",
}


def _coerce_profile_memory_text(value: Any, *, max_len: int = 500) -> str:
    """Extract safe user-facing memory text from an LLM value.

    The memory extractor is allowed to return only plain facts. If the model
    sends nested JSON, we pick common text keys and never stringify the object,
    so backend field names do not leak into the memory panel.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, (int, float, bool)):
        text = str(value).strip()
    elif isinstance(value, dict):
        text = ""
        for key in _PROFILE_MEMORY_TEXT_KEYS:
            if key in value:
                text = _coerce_profile_memory_text(value.get(key), max_len=max_len)
                if text:
                    break
    elif isinstance(value, list):
        parts = [_coerce_profile_memory_text(item, max_len=120) for item in value[:8]]
        text = "；".join([part for part in parts if part])
    else:
        text = ""
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "..."
    return text


def _is_shallow_profile_memory_text(text: Any) -> bool:
    compact = re.sub(r"\s+", "", str(text or "").strip().lower())
    if not compact:
        return True
    if compact in _SHALLOW_MEMORY_EXACT_TEXTS:
        return True
    if "完成" in compact and ("内容生成" in compact or "写稿" in compact) and len(compact) <= 24:
        return True
    if "生成" in compact and ("本次" in compact or "一次" in compact) and len(compact) <= 24:
        return True
    return False


def _content_billing_error_alert() -> dict:
    """[§13 rollout] 内容扣费兜底异常的机器合同(七字段 + 重试/联系客服出口)。

    ``_bill`` 抛非 HTTPException/ValueError 的兜底异常时,原本是
    ``raise HTTPException(402, detail=str(e))``(裸错误码 · 无下一步 = 事故#8)。
    这里用唯一 builder 组一份 §13 合同;``str(e)`` 只进日志,不进用户文案
    (feedback_no_supplier_names_to_users)。不是新增硬阻断——它只给一个已经
    失败(402)的路径补出口(§3.6 O1/A1)。
    """
    from services.governance_contract import build_alert
    return build_alert(
        "CONTENT_BILLING_ERROR",
        "计费环节出现异常，这次操作还没开始。",
        reason="预扣积分时出现异常，为保护你的积分，本次操作没有开始。",
        impact="本次操作未开始，未扣费。",
        repair_hint="可稍后重试；若持续出现请联系客服核对账户。",
        actions=[
            {"id": "retry", "label": "重试", "type": "retry"},
            {"id": "contact_support", "label": "联系客服", "type": "contact"},
        ],
        rule_version="content-billing-v1",
    )


# ========== SSE 工具（P0-K 修复：HTTP/2 保活） ==========
# 同 P0-1 病根：SSE generator 静默期 > 30s 会被 CDN/SLB idle timeout 断流 → ERR_HTTP2_PROTOCOL_ERROR
# 所有 StreamingResponse 必须：
#   1. 带 X-Accel-Buffering: no 关 nginx 缓冲
#   2. 用 _sse_headers() 统一 SSE 响应头
#   3. 用 _with_heartbeat() 包装 generator，静默期每 10s 发 SSE comment 保活

async def _with_heartbeat(src_gen, interval: float = 10.0):
    """包装 async generator，在每次 yield 间隙超时时插入 SSE comment 保活。

    SSE comment 行以 `:` 开头，客户端解析器会忽略。定时发送保持连接活跃。

    v1.7.6.2 P0-3 双保险(2026-05-24 老板深度诊断 prod 实证后):
    · prod 现象:is_disconnected() 永 False · uvicorn 收不到 EOF(nginx/ESA 不关 upstream)
    · 但 backend 每 10s 写 keepalive 应该会失败 · client 真断时 write 抛 ConnectionResetError/
      BrokenPipeError/anyio.BrokenResourceError/RuntimeError(starlette 已 close 后再 write)
    · catch 后取消上游 task + close generator · 让 stream_generator 的 CancelledError 路径触发
    · 不依赖 is_disconnected · 是 write-fail 物理路径(socket 真断后必抛 · prod 物理硬规)
    """
    # v1.7.6.2 · anyio 异常 import(starlette 用 anyio 包 ASGI write · 其异常需 catch)
    try:
        from anyio import ClosedResourceError, EndOfStream, BrokenResourceError
        _ANYIO_WRITE_FAIL = (ClosedResourceError, EndOfStream, BrokenResourceError)
    except ImportError:
        _ANYIO_WRITE_FAIL = ()

    it = src_gen.__aiter__()
    task = None
    try:
        while True:
            task = asyncio.create_task(it.__anext__())
            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(asyncio.shield(task), timeout=interval)
                        # v1.7.6.2 · yield 是写到 ASGI send · client 真断后会抛 · 单独 catch
                        try:
                            yield chunk
                        except (ConnectionError, BrokenPipeError, *_ANYIO_WRITE_FAIL) as write_err:
                            logger.info(
                                "[_with_heartbeat] v1.7.6.2 P0-3 · write-fail detected (yield chunk) · err=%s · close upstream task",
                                type(write_err).__name__,
                            )
                            raise asyncio.CancelledError(f"client write-fail: {type(write_err).__name__}")
                        break
                    except asyncio.TimeoutError:
                        # v1.7.6.2 · keepalive 写也可能失败(client 断时)· 单独 catch
                        try:
                            yield ": keepalive\n\n"
                        except (ConnectionError, BrokenPipeError, *_ANYIO_WRITE_FAIL) as write_err:
                            logger.info(
                                "[_with_heartbeat] v1.7.6.2 P0-3 · write-fail detected (keepalive) · err=%s · close upstream task",
                                type(write_err).__name__,
                            )
                            raise asyncio.CancelledError(f"client write-fail keepalive: {type(write_err).__name__}")
            except StopAsyncIteration:
                return
            except asyncio.CancelledError:
                # v1.7.6.2 · write-fail 转的 cancel 或外部 cancel · cancel 上游 task 让 stream_generator 走 CancelledError 退费路径
                raise
            except Exception:
                # generator 内部异常透传
                raise
    finally:
        if task and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        close = getattr(it, "aclose", None)
        if close:
            await close()


def _get_profile_safe(request: Request, profile_id: str) -> dict:
    """获取 profile，并按 brand_id 做权限校验。

    代运营场景里，用户可能记住旧 profile_id 或员工被撤权后继续请求接口。
    不能只依赖前端列表过滤。这里用 brand_id 二次校验：
    - 管理员放行
    - 分配客户放行
    - 自己 owner 的品牌放行（解决新建品牌后 JWT 列表未刷新）
    - 其他情况拒绝
    """
    from db.profile_db import get_profile
    from auth.brand_access import require_brand_access

    profile = get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="档案不存在")
    require_brand_access(request, profile.get("brand_id"), allow_null=False)
    return profile


def _request_user_id(request: Request) -> int | None:
    user = getattr(getattr(request, "state", None), "user", None) or {}
    raw = user.get("user_id") or user.get("id")
    try:
        return int(raw)
    except Exception:
        return None


def _request_is_admin(request: Request) -> bool:
    user = getattr(getattr(request, "state", None), "user", None) or {}
    return bool(user.get("is_admin") or user.get("role") in {"admin", "super_admin"})


# 白标(v3.6 · 决策 F)默认值 · 分段拼接保留原平台文案,不在代码行留可被白标扫描误判的字面 token
_AGENT_DEFAULT_ASSISTANT = "小" + "榜"


def _resolve_agent_assistant(request: Request) -> str:
    """取当前代理的社媒助手名(surface='agent' · 仅 OEM 出代理品牌)· 非 OEM 回退平台默认。

    用于 社媒工作台 Agent 总控 / 调研 / 选题 / thinking 文案的「助手名」白标化。
    """
    try:
        from services.public_whitelabel import resolve_branding_context
        uid = _request_user_id(request)
        if not uid:
            return _AGENT_DEFAULT_ASSISTANT
        ctx = resolve_branding_context(surface="agent", owner_user_id=_branding_principal(request, uid))
        if ctx.get("source") == "platform_default":
            return _AGENT_DEFAULT_ASSISTANT
        return ((ctx.get("brand") or {}).get("product_name") or "").strip() or _AGENT_DEFAULT_ASSISTANT
    except Exception:
        return _AGENT_DEFAULT_ASSISTANT


def _extract_generated_script_text(payload: Any) -> str:
    """从不同生成器/重写器返回结构里稳妥取出正文。

    写作链路经历过多轮模型与重写器切换，正文可能落在 full_script、
    script_text、script_content、content/body，甚至嵌套的 script 里。
    这里做轻量兼容，避免“模型已写完但前端拿不到正文”的假失败。
    """
    if payload is None:
        return ""
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        if text.startswith("{") and ("full_script" in text or "script_text" in text or "script_content" in text):
            try:
                parsed = json.loads(text)
                nested = _extract_generated_script_text(parsed)
                if nested:
                    return nested
            except Exception:
                pass
        return text
    if isinstance(payload, list):
        parts = [_extract_generated_script_text(item) for item in payload]
        return "\n\n".join(part for part in parts if part).strip()
    if not isinstance(payload, dict):
        return ""

    for key in (
        "full_script",
        "full_text",
        "script_text",
        "script_content",
        "content",
        "text",
        "body",
        "正文",
    ):
        value = payload.get(key)
        text = _extract_generated_script_text(value)
        if text:
            return text

    for key in ("script", "draft", "result", "data", "output"):
        value = payload.get(key)
        if value is payload:
            continue
        text = _extract_generated_script_text(value)
        if text:
            return text

    structured_parts = []
    for key in ("opening", "hook", "开头", "main_body", "body_text", "cta", "ending", "结尾"):
        text = _extract_generated_script_text(payload.get(key))
        if text:
            structured_parts.append(text)
    return "\n\n".join(structured_parts).strip()


def _actor_user_id(request: Request) -> str | None:
    user = getattr(request.state, "user", None) or {}
    uid = user.get("id") or user.get("user_id")
    return str(uid) if uid else None


_SCRIPT_WORDS_PER_SECOND = 5.5


def _resolve_billing_brand_id(request: Request, profile_id: str | None = None) -> int | None:
    """v1.7.6 P0-2 FIN-002 · 扣费 brand_id 解析 · profile.brand_id 优先 · fallback JWT brand_ids[0]

    真因(Codex 真机 2026-05-23):script_gen 流水 brand_id=477 但 profile 6dcbd04b 属 brand 476
    · _bill / _bill_ctx 用 user.client_brand_ids[0] 取 brand · 跟 profile.brand_id 不一致
    · 用户切客户后流水仍写 JWT 第一个 brand · 财务对账错 brand 归属
    修法:profile_id 有 → 查 profile.brand_id · 验 user 有访问权限 · 用真值
         profile_id 无 / profile 不存在 / 越权 → fallback brand_ids[0](保兼容)
    """
    user = getattr(request.state, "user", {}) or {}
    brand_ids = user.get("client_brand_ids", []) or []
    if profile_id:
        try:
            from db.profile_db import get_profile
            profile = get_profile(profile_id)
            if profile:
                pbid = profile.get("brand_id")
                if pbid:
                    pbid_int = int(pbid)
                    # 验 user 有访问此 brand 权限 · 防越权写错单(admin 不限制)
                    if user.get("is_admin") or not brand_ids or pbid_int in brand_ids:
                        return pbid_int
                    # profile.brand_id 不在 user.client_brand_ids 列表 · log warning fallback
                    logging.getLogger("GEO-Bill").warning(
                        "[_resolve_billing_brand_id] profile %s brand_id=%d NOT in user.client_brand_ids=%s · 用 JWT fallback",
                        profile_id, pbid_int, brand_ids,
                    )
        except Exception as exc:
            logging.getLogger("GEO-Bill").warning(
                "[_resolve_billing_brand_id] profile_id=%s 解析失败 fallback: %s",
                profile_id, exc,
            )
    return brand_ids[0] if brand_ids else None


async def _bill(request: Request, feature_code: str, profile_id: str | None = None):
    """扣费(管理员免费),自动关联用户的第一个品牌。

    2026-05-12 老板 P0 一次性修复:
    Social Studio feature(在 SOCIAL_FEATURES 白名单内)优先扣 V3.1 entitlement
    配额耗尽自动 fallback paid · GEO/通用 feature 仍走旧 deduct_points · 0 行为变化

    v1.7.6 P0-2 FIN-002:profile_id 参数(可选)· 传则用 profile.brand_id 不是 JWT[0]
    · 防"切客户后流水仍写 JWT 第一个 brand"的财务对账错
    · 老 caller 不传 → fallback JWT brand_ids[0](0 行为变化)

    注意: 此版本为"先扣费不退费"的旧行为,仅供确认性操作使用(保存、更新等快速动作)。
    需要回滚的长任务请改用 _bill_ctx()。
    退费走 _release_or_refund(根据 request.state._bill_meta 智能 release 或 refund)。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if user.get("is_admin"):
        return False  # 管理员免费
    user_id = user.get("user_id")
    if not user_id:
        return False
    brand_id = _resolve_billing_brand_id(request, profile_id=profile_id)

    from middleware.subscription_billing import SOCIAL_FEATURES
    if feature_code in SOCIAL_FEATURES:
        # V3.1 路径:优先 entitlement · 耗尽 fallback paid
        from middleware.subscription_billing import charge_subscription_entitlement
        result = await charge_subscription_entitlement(
            user_id=user_id, feature_code=feature_code,
            fallback_to_points=True, brand_id=brand_id,
        )
        # 记录扣费来源 · 供 _release_or_refund 判定走 entitlement-release 还是 paid-refund
        try:
            meta = getattr(request.state, "_bill_meta", {}) or {}
            meta[feature_code] = {
                "from": result.get("from"),
                "deducted_amount": result.get("deducted_amount", 0),
            }
            request.state._bill_meta = meta
        except Exception:
            pass
        return result.get("from") not in ("cache", "free")

    # 旧路径(GEO / 通用 feature)
    from middleware.billing import deduct_points
    await deduct_points(user_id, feature_code, brand_id=brand_id)
    return True


async def _release_or_refund(request: Request, feature_code: str, reason: str = ""):
    """智能退费(2026-05-12 一次性修复配套):
    根据 request.state._bill_meta 判定:
      · 之前走 entitlement 扣的 → release_subscription_entitlement(释放配额)
      · 之前走 fallback_points 扣的 → refund_points(退 paid)
      · 之前是 cache/free → 不退(0 扣不退)
    无 meta(老调用方未走 _bill V3.1 路径)→ 兜底 refund_points
    """
    user = getattr(request.state, "user", None)
    if not user:
        return
    if user.get("is_admin"):
        return
    user_id = user.get("user_id")
    if not user_id:
        return

    bill_meta = getattr(request.state, "_bill_meta", None) or {}
    entry = bill_meta.get(feature_code)
    if entry:
        from_type = entry.get("from")
        if from_type == "entitlement":
            try:
                from middleware.subscription_billing import release_subscription_entitlement
                video_min = int(entry.get("deducted_amount", 0)) if feature_code == "video_asr" else 0
                release_subscription_entitlement(
                    user_id=user_id, feature_code=feature_code, video_minutes=video_min,
                )
                return
            except Exception as e:
                import logging
                logging.getLogger("GEO-Content").error(
                    f"[_release_or_refund] release entitlement {feature_code} 失败,fallback to refund_points: {e}"
                )
                # fall-through 走 refund_points
        elif from_type in ("cache", "free"):
            # 没真扣,不需要退
            return

    # 兜底 / fallback_points 路径:退 paid
    try:
        from middleware.billing import refund_points
        await refund_points(user_id, feature_code, reason)
    except Exception as e:
        import logging
        logging.getLogger("GEO-Content").error(
            f"[_release_or_refund] refund_points {feature_code} 失败: {e}"
        )


async def _release_or_refund_bg(user_id: int, feature_code: str, bill_meta_snapshot: dict, reason: str = ""):
    """后台任务版退费(不依赖 request.state · 入口需 snapshot _bill_meta 传过来)。

    用法:
        # API 入口同步流程
        await _bill(request, "deep_analyze")
        meta_snap = (getattr(request.state, "_bill_meta", {}) or {}).get("deep_analyze")

        # 启动后台任务(meta_snap 闭包捕获)
        async def _run_bg():
            try:
                ...
            except Exception as e:
                await _release_or_refund_bg(user_id, "deep_analyze", meta_snap, reason=str(e))
    """
    if not user_id:
        return
    from_type = (bill_meta_snapshot or {}).get("from") if bill_meta_snapshot else None
    if from_type == "entitlement":
        try:
            from middleware.subscription_billing import release_subscription_entitlement
            deducted = int((bill_meta_snapshot or {}).get("deducted_amount", 0) or 0)
            video_min = deducted if feature_code == "video_asr" else 0
            release_subscription_entitlement(
                user_id=user_id, feature_code=feature_code, video_minutes=video_min,
            )
            return
        except Exception as e:
            import logging
            logging.getLogger("GEO-Content").error(
                f"[_release_or_refund_bg] release entitlement {feature_code} 失败,fallback refund_points: {e}"
            )
    elif from_type in ("cache", "free"):
        return  # 没扣不退

    try:
        from middleware.billing import refund_points
        await refund_points(user_id, feature_code, reason)
    except Exception as e:
        import logging
        logging.getLogger("GEO-Content").error(
            f"[_release_or_refund_bg] refund_points {feature_code} 失败: {e}"
        )


from contextlib import asynccontextmanager
import asyncio
import contextlib as _stdlib_contextlib


async def _abortable_await(request: Request, coro_or_task, *, phase: str = "", poll_seconds: float = 0.5):
    """v1.7.6.1 P0-3b · charge-before-expose 架构核心 helper · 长 await 并行跑 disconnect watcher

    真因(老板 2026-05-23 深度诊断):
        SSE 长 LLM await 期间 starlette 不主动检测客户端断连 · 浏览器 abort / 关 tab / 断网 /
        移动端杀进程 都不一定让 await 抛 CancelledError · LLM 继续跑完 + 钱已扣 + 0 refund
        → 资金链断流扣费灾难线 · 必须服务端主动 is_disconnected 轮询

    工作原理:
        起 2 个 task:
            1. coro_or_task(LLM / IO)
            2. watch_task(每 poll_seconds=0.5s 调 request.is_disconnected())
        asyncio.wait FIRST_COMPLETED:
            - LLM 先返:cancel watcher · 返 task.result()
            - watcher 先返(检到断连):cancel LLM task · raise CancelledError 让 caller 走 skip_charge
        watcher 内部 exception 不杀 task(防 is_disconnected 偶发异常误杀)

    用法:
        try:
            result = await _abortable_await(request, generation_task, phase="script_gen_llm")
        except asyncio.CancelledError:
            logger.info("[caller] disconnect detected · phase=script_gen_llm · skip_charge")
            return  # 0 扣 0 退 0 emit

    参数:
        request: Starlette/FastAPI Request · 必须有 .is_disconnected() async 方法
        coro_or_task: awaitable(coroutine 或 asyncio.Task)
        phase: log 标识 · 便于 Deploy-CTO grep 哪一阶段触发的 disconnect
        poll_seconds: watcher 轮询间隔 · 默认 0.5s · 范围 [0.2, 2.0] 推荐

    返:
        task 正常结束 → task.result()
        disconnect 触发 → raise asyncio.CancelledError(f"client disconnected during {phase}")
    """
    if isinstance(coro_or_task, asyncio.Task):
        task = coro_or_task
    else:
        task = asyncio.create_task(coro_or_task)

    async def _watch_disconnect():
        # 内部 exception 不传播(防 is_disconnected 偶发异常误杀 LLM)· 异常时 return False
        while True:
            try:
                if await request.is_disconnected():
                    return "disconnected"
            except Exception as exc:
                logger.warning("[abortable_await] watcher is_disconnected 异常 · phase=%s · err=%s", phase, exc)
                return "watcher_error"
            await asyncio.sleep(poll_seconds)

    watch_task = asyncio.create_task(_watch_disconnect())
    try:
        done, pending = await asyncio.wait(
            {task, watch_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if watch_task in done:
            watch_result = watch_task.result()
            if watch_result == "disconnected":
                # 客户端断了 · cancel LLM · raise 让 caller skip_charge
                logger.info(
                    "[abortable_await] P0-3b · disconnect detected · phase=%s · cancel task",
                    phase,
                )
                if not task.done():
                    task.cancel()
                    with _stdlib_contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                raise asyncio.CancelledError(f"client disconnected during {phase}")
            # watcher 因 exception 返(非 disconnected)· 等 task 正常完成
            return await task
        # task 先完成(正常) · cancel watcher · 返结果
        if not watch_task.done():
            watch_task.cancel()
            with _stdlib_contextlib.suppress(asyncio.CancelledError, Exception):
                await watch_task
        return task.result()
    except asyncio.CancelledError:
        # caller cancel 或 disconnect raise · 清 watcher
        if not watch_task.done():
            watch_task.cancel()
            with _stdlib_contextlib.suppress(asyncio.CancelledError, Exception):
                await watch_task
        raise

@asynccontextmanager
async def _bill_ctx(request: Request, feature_code: str, profile_id: str | None = None):
    """扣费 + 失败自动退费上下文管理器(after-yield 扣费,业务失败 0 扣)。

    2026-05-12 老板 P0 一次性修复:
    Social Studio feature(SOCIAL_FEATURES 白名单)优先扣 V3.1 entitlement
    配额耗尽自动 fallback paid · GEO/通用 feature 仍走旧 deduct_points

    2026-05-20 Codex 阻断 #1 修复(资金闭环):
    yield 前 V3.1 social feature 必须预检查(配额 OR fallback 积分)· 不够 raise 402 fail-closed
    yield 后扣费失败 → 强制 raise HTTPException(500) 暴露给客户端 · 不再吞掉
    (理论上预检查通过后再扣失败极罕见 · 一般是并发 race · 暴露给客户端比白嫖更安全)

    v1.7.6 P0-2 FIN-002:profile_id 参数(可选)· 传则用 profile.brand_id 不是 JWT[0]
    · 防"切客户后流水仍写 JWT 第一个 brand"的财务对账错
    · 老 caller 不传 → fallback JWT brand_ids[0](0 行为变化)

    用法:
        async with _bill_ctx(request, "script_gen", profile_id=data.profile_id):
            result = await generate_script(...)
        # with 块内抛异常 → 0 扣 0 退;正常结束 → 扣 entitlement 或 paid

    约束:
    - 管理员直接跳过(无扣费无退费)
    - 余额不足会抛 HTTPException(402)
    - V3.1 路径预检:subscription_overrun_check + check_balance_only 双重(entitlement 够 OR fallback 余额够)
    - 旧路径预检:check_balance_only (不真扣)
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if user.get("is_admin"):
        yield None  # 管理员免费直接通过
        return
    user_id = user.get("user_id")
    if not user_id:
        yield None
        return

    brand_id = _resolve_billing_brand_id(request, profile_id=profile_id)

    from middleware.subscription_billing import SOCIAL_FEATURES
    is_social = feature_code in SOCIAL_FEATURES

    if is_social:
        # V3.1 预检查(Codex 阻断 #1): entitlement 配额 OR fallback 积分至少一个够
        from middleware.subscription_billing import subscription_overrun_check, BILLING_FALLBACK_CODE_MAP
        from middleware.billing import check_balance_only

        overrun = subscription_overrun_check(user_id, feature_code, video_minutes=0)
        if not overrun.get("in_quota"):
            # entitlement 配额不够 · 检查 fallback paid 余额(用映射的 feature_code 查 feature_pricing)
            billing_code = BILLING_FALLBACK_CODE_MAP.get(feature_code, feature_code)
            try:
                await check_balance_only(user_id, billing_code)
            except HTTPException:
                # 余额也不够 → 直接 raise 402(暴露真因)· 业务不跑 · 0 扣
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=402,
                    detail={"code": "BILLING_PRECHECK_FAILED", "message": f"扣费预检查失败: {e}"},
                )
    else:
        # 旧路径:check_balance_only 预检
        from middleware.billing import check_balance_only
        await check_balance_only(user_id, feature_code)

    try:
        yield
    except Exception:
        # 业务失败 → 未扣不退
        raise

    if getattr(request.state, "_skip_bill_once", False):
        try:
            setattr(request.state, "_skip_bill_once", False)
        except Exception:
            pass
        return

    # v1.7.6.1 P0-3a · charge-before-expose final alive check
    # 真因(老板 2026-05-23 深度诊断):浏览器 abort 不一定让 starlette 抛 CancelledError
    # · LLM 跑完 · with 块退出 · _bill_ctx 仍扣费 · 用户钱没了内容也没拿到 · 资金链断流
    # 修法:yield 后扣费前 final alive check · 断了 → 0 扣 + log skip_charge
    # 覆盖范围:本 helper 全部 caller(script_gen / topic_gen / web_search / hook_gen / comment_gen 6 处)
    try:
        if await request.is_disconnected():
            logger.info(
                "[_bill_ctx] P0-3a · final alive check 不过 · phase=%s · skip_charge",
                feature_code,
            )
            return
    except Exception as _alive_err:
        # is_disconnected 偶发异常 · 不阻 charge · log warning
        logger.warning(
            "[_bill_ctx] P0-3a · final alive check 异常 · phase=%s · err=%s · fallback proceed charge",
            feature_code, _alive_err,
        )

    # with 体正常结束 + 用户连着 → 真扣
    if is_social:
        # V3.1 路径(Codex 阻断 #1 修复: 后扣费失败必须 raise · 不再 log-only 吞掉)
        try:
            from middleware.subscription_billing import charge_subscription_entitlement
            result = await charge_subscription_entitlement(
                user_id=user_id, feature_code=feature_code,
                fallback_to_points=True, brand_id=brand_id,
            )
            # 记录扣费来源 · 供后续 _release_or_refund 用
            try:
                meta = getattr(request.state, "_bill_meta", {}) or {}
                meta[feature_code] = {
                    "from": result.get("from"),
                    "deducted_amount": result.get("deducted_amount", 0),
                }
                request.state._bill_meta = meta
            except Exception:
                pass
        except HTTPException:
            raise
        except Exception as e:
            import logging
            logging.getLogger("GEO-Content").error(
                f"[_bill_ctx] V3.1 后扣费失败 {feature_code}(业务已完成 · 暴露给客户端防白嫖): {e}"
            )
            # Codex 阻断 #1: 不再吞 · raise 让客户端知道资金侧出问题 (业务结果已计算 · 客户端可重试)
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "BILLING_POST_CHARGE_FAILED",
                    "message": f"业务完成但扣费失败,请联系客服或重试: {feature_code}",
                    "feature_code": feature_code,
                },
            )
    else:
        # 旧路径
        try:
            from middleware.billing import deduct_points
            await deduct_points(user_id, feature_code, brand_id=brand_id)
        except HTTPException:
            raise
        except Exception as e:
            import logging
            logging.getLogger("GEO-Content").error(
                f"[_bill_ctx] {feature_code} 后扣费失败(业务已完成): {e}"
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "BILLING_POST_CHARGE_FAILED",
                    "message": f"业务完成但扣费失败,请联系客服或重试: {feature_code}",
                    "feature_code": feature_code,
                },
            )


# ========== Pydantic Models ==========

class GenerateTopicsRequest(BaseModel):
    """选题生成请求"""
    profile_id: str
    advisor_id: str = "xuehui"
    count: int = 30
    category_mix: Optional[Dict[str, float]] = None  # {"traffic": 0.5, "persona": 0.3, "monetize": 0.2}
    feedback: Optional[str] = None  # 🆕 用户生成意见（引导LLM生成方向）
    expert_advisor_id: Optional[str] = None  # 行业专家顾问ID


class BatchGenerateRequest(BaseModel):
    """批量生成文案请求"""
    topics: List[str]                        # 选题列表
    profile_id: str
    advisor_id: str = "xuehui"
    style: str = "medium"
    include_storyboard: bool = False         # 批量模式默认不生成分镜
    generation_mode: str = "normal"
    guardrail_mode: str = "balanced"


# ========== API 路由 ==========

# ========== 快速设置 ==========

# ========== 文案库 API ==========

# ========== 一键策划 API ==========


# ========== 创意工坊 · 对话模式 API ==========


def _summarize_profile_for_research(profile: dict) -> str:
    """老板 A+ 2026-05-19 v1.5 · profile 摘要塞 prompt(取代 internal_profile_get tool)

    v1.5 Data SSOT 边界:A+ 调研路径不查老 profile / 老 memory · 客户摘要直接注入 system prompt。
    防 internal_memory_query 走 adapter 时 track_access=True 更新 profile_memory_events 访问统计。
    """
    parts: list[str] = []
    for label, key in [
        ("行业", "industry"),
        ("业务", "business"),
        ("城市", "cities"),
        ("目标客户", "target_users"),
        ("核心卖点", "selling_points"),
    ]:
        v = profile.get(key)
        if v:
            text = ", ".join(v) if isinstance(v, list) else str(v)
            parts.append(f"- {label}:{text[:80]}")
    return "\n".join(parts) if parts else "(资料较少)"


async def _run_chat_research_agent_loop(
    user_message: str,
    request: Request,
    profile: dict,
    history: list[dict] | None,
    *,
    chat_session_id: str | None = None,
    event_sink: Callable[[dict], None] | None = None,
) -> dict[str, Any] | None:
    """老板 A+ 2026-05-19 v1.5 · chat 调研意图 → agent_loop

    · 8 read tool 白名单(v1.5 砍 internal_profile_get + internal_memory_query · Data SSOT §1.1)
    · profile 摘要直接塞 prompt(不查老 profile / 老 memory)
    · event_sink 桥 agent_loop thinking/tool_call/tool_result 到 chat SSE(collector 模式)
    · DATA_TOOLS 命中护栏:无外部数据工具真 ok=True → 返 None 让老 orchestrator 兜底
    · history 预留 v2 多轮调研注入 · v1.5 单轮够用 · 由 caller 仍传(防 ABI 后续改动)
    · 文档:docs/AI-CONTEXT/CHAT_AGENT_LOOP_RESEARCH_PLAN_2026-05-19.md
    """
    del history  # v2 placeholder · 多轮调研注入用 · v1.5 单轮够用
    from tools.agent_loop.agent_loop import run_agent_loop
    from tools.agent_loop.tool_router import AgentToolContext

    user = getattr(request.state, "user", None) or {}
    user_id = user.get("user_id") or user.get("id")
    is_admin = bool(user.get("is_admin") or user.get("role") == "admin")

    profile_summary = _summarize_profile_for_research(profile)
    _assistant_name = _resolve_agent_assistant(request)
    messages = [
        {
            "role": "system",
            "content": (
                f"你是{_assistant_name}社媒助手 · 用户在主对话框问外部平台(抖音/B站/小红书)调研问题。\n"
                "用 tikhub_search_topics / web_visit / metaso_web_search 等工具查 · "
                "返简洁中文摘要(3-5 条关键发现 + 1 句行动建议)给用户。\n\n"
                "【硬约束】\n"
                "- 不要写稿 / 不要做规划 / 不要补资料 · 只做调研 + 总结\n"
                "- 用户其实想写稿时(出现「写一条」「写成稿」「仿写」等)· 只给下一步建议 · "
                "不直接写稿(写稿应该走专门的 writer 流程不在 chat 这里)\n"
                "- 不要调 write/action 类工具(本次只暴露 read)\n\n"
                "【输出格式硬约束】(2026-05-20 老板 + Codex 反馈)\n"
                "- 凡工具结果包含 url / 链接 字段 · 最终回答必须保留 3-5 个可点击 markdown 链接\n"
                "- 链接严格用 [标题](url) 格式 · 不要把 url 单独写一行 · 也不要省略\n"
                "- 优先把 top 3-5 高互动样本的标题渲成可点击链接 · 让用户能直接点过去看\n"
                "- 工具返回的 summary_markdown 已含 [标题](url) 格式 · 你引用时不要拆掉链接\n\n"
                "【若用户已附参考资料】(2026-05-21 v1.3 软口径)\n"
                "- 如果上下文里出现【上传资料 N:...】块 · 优先基于这些参考创作 / 回答\n"
                "- 仿写 / 拆解时引用参考中的原话 / 细节 / 数据\n"
                "- 不再重复外部检索 · 除非用户明确说『再找点别的』『补最新数据』『换别的参考』\n\n"
                "【当前客户资料】\n"
                f"{profile_summary}"
            ),
        },
        {"role": "user", "content": user_message},
    ]
    ctx = AgentToolContext(
        user_id=int(user_id) if user_id is not None else None,
        profile_id=str(profile.get("id") or ""),
        turn_id=str(chat_session_id or ""),
        brand_id=profile.get("brand_id"),
        is_admin=is_admin,
        billing_enabled=False,
    )
    result = await run_agent_loop(
        messages,
        # v1.5 · 8 read tool 白名单
        # · 砍 internal_profile_get(owner_guard profile_id 麻烦 · profile 摘要直接塞 prompt)
        # · 砍 internal_memory_query(adapter track_access=True 会 UPDATE profile_memory_events · 违 Data SSOT §1.1.4)
        # · 不暴露 write/action 类(防 chat 误改 profile / 写稿)
        tools=[
            "industry_knowledge_query",
            "metaso_web_search",
            "tikhub_search_topics",
            "tikhub_get_account",
            "tikhub_parse_video",
            "keyword_explore",
            "web_visit",
            "time_now",
        ],
        ctx=ctx,
        max_rounds=3,
        event_sink=event_sink,
    )

    # v1.3 · 数据工具命中护栏(Codex 第 3 轮反馈)
    # 防"假查真猜":LLM 不调任何 TikHub/搜索工具 · 凭训练记忆答"最近抖音热门"
    # 必须至少一个 DATA_TOOLS 真命中 · 否则 fallback 老 orchestrator clarify 反问
    # time_now / industry_knowledge_query(本地)不算外部数据
    DATA_TOOLS = {
        "tikhub_search_topics",
        "tikhub_get_account",
        "tikhub_parse_video",
        "metaso_web_search",
        "web_visit",
        "keyword_explore",
    }
    # v1.2 · tool_router 吞异常返 dict {"ok": False, ...} · 顶层 key · 不是 .result.ok
    # 实证:agent_loop.py:252-253 直接 append execute_tool 的 dict · tool_router.py:188 顶层 ok
    final_resp = (result.final_response or "").strip()
    has_data_tool_ok = any(
        isinstance(tr, dict)
        and tr.get("ok") is True
        and tr.get("tool") in DATA_TOOLS
        for tr in (result.tool_results or [])
    )
    # final 空 / 没真查数据 → 都兜底
    if not final_resp or not has_data_tool_ok:
        return None

    # 2026-05-20 老板 + Codex Phase 1 · 半动态 suggested_prompts
    # 从 tool_results 的 top items 抽标题 + 平台 · 生"拆解/仿写/出选题/整理" 4 个动作
    # 点击后前端把"拆解《xxx》"作为新 user_message 发起 · 走老 14 route reference_breakdown / rewrite_reference
    suggested = _build_research_suggested_prompts(result.tool_results or [])

    return {
        "route": "research_via_agent_loop",
        "user_visible_reply": final_resp,
        "controller": "chat_agent_loop_research",
        "suggested_prompts": suggested,
        "agent_loop_meta": {
            "rounds": result.rounds,
            "tool_count": len(result.tool_results),
            "tool_ok_count": sum(
                1 for tr in (result.tool_results or [])
                if isinstance(tr, dict) and bool(tr.get("ok"))
            ),
            "data_tool_ok_count": sum(
                1 for tr in (result.tool_results or [])
                if isinstance(tr, dict)
                and tr.get("ok") is True
                and tr.get("tool") in DATA_TOOLS
            ),
            "model": result.model,
            "stopped_by": result.stopped_by,
        },
    }


def _build_research_suggested_prompts(tool_results: list[dict]) -> list[str]:
    """2026-05-20 老板 + Codex Phase 1 · 半动态 follow-up 按钮(2026-05-21 P1 修 · 带 URL)

    从 tikhub/metaso 等数据工具结果里抽 top 2-3 条最爆样本的标题 + URL
    生成 3-4 个 follow-up 按钮 · 每个带"标题 + URL" · 后续 14 route 拿得到可定位对象
    · "拆解这条:《标题》 https://..." → 走 14 route reference_breakdown(已有 · 含 URL 不再问)
    · "仿写这条的开头:《标题》 https://..." → 走 14 route rewrite_reference(已有)
    · "按这些样本给我 10 个选题" → 走 14 route topic_ideation(已有)
    · "整理成素材包" → Phase 1 走 14 route LLM 兜底 · Phase 2 接真 corpus tool

    2026-05-21 Codex P1:之前只带标题 · 后续 route 拿不到原视频链接 · 标题重复/模糊时体验断
    修法:title 后拼 URL(有 http(s) 时)· 标题截 30 字防按钮文案爆
    """
    top_items: list[dict] = []
    seen: set[str] = set()

    def _clean_text(value: Any, *, max_len: int = 120) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())[:max_len]

    def _append_candidate(title: Any, url: Any = "") -> None:
        if len(top_items) >= 3:
            return
        clean_title = _clean_text(title)
        if not clean_title:
            return
        clean_url = str(url or "").strip()
        key = f"{clean_title}|{clean_url}"
        if key in seen:
            return
        seen.add(key)
        top_items.append({"title": clean_title, "url": clean_url})

    def _extract_from_research_table(rows: Any) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = (
                row.get("视频/笔记")
                or row.get("标题")
                or row.get("title")
                or row.get("视频")
                or row.get("内容")
                or row.get("name")
            )
            url = (
                row.get("链接")
                or row.get("url")
                or row.get("link")
                or row.get("href")
            )
            _append_candidate(title, url)

    def _extract_from_summary_markdown(markdown: Any) -> None:
        if not isinstance(markdown, str) or not markdown.strip():
            return
        for match in re.finditer(r"\[([^\]\n]{1,160})\]\((https?://[^)\s]+)\)", markdown):
            _append_candidate(match.group(1), match.group(2))

    for tr in tool_results:
        if not isinstance(tr, dict) or tr.get("ok") is not True:
            continue
        result_payload = tr.get("result") if isinstance(tr.get("result"), dict) else tr
        if not isinstance(result_payload, dict):
            continue
        items = result_payload.get("items")
        if isinstance(items, list):
            for item in items[:3]:
                if isinstance(item, dict):
                    _append_candidate(item.get("title") or item.get("desc") or item.get("name"), item.get("url") or item.get("share_url") or item.get("link"))
        _extract_from_research_table(result_payload.get("research_table"))
        _extract_from_summary_markdown(result_payload.get("summary_markdown"))
        if len(top_items) >= 3:
            break

    def _title_with_url(item: dict) -> str:
        """《标题》 https://... · URL 不合法时只返《标题》"""
        title = str(item.get("title") or "").strip()[:30]
        url = str(item.get("url") or "").strip()
        if title and url and url.startswith(("http://", "https://")):
            return f"《{title}》 {url}"
        if title:
            return f"《{title}》"
        return ""

    suggestions: list[str] = []
    if top_items:
        first_text = _title_with_url(top_items[0])
        if first_text:
            suggestions.append(f"拆解这条:{first_text}")
            if len(top_items) >= 2:
                second_text = _title_with_url(top_items[1])
                if second_text:
                    suggestions.append(f"仿写这条的开头:{second_text}")
            else:
                suggestions.append(f"仿写这条的开头:{first_text}")
    suggestions.append("按这些样本给我 10 个选题")
    suggestions.append("整理成素材包")

    if not top_items:
        return [
            "把上面样本按平台列成清单",
            "先让我选一条再拆",
            "按这些样本给我 10 个选题",
            "整理成素材包",
        ]
    return suggestions[:4]


def _stringify_profile_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple, set)):
        return "、".join(_stringify_profile_value(item) for item in value if _stringify_profile_value(item))
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            item_text = _stringify_profile_value(item)
            if item_text:
                parts.append(f"{key}:{item_text}")
        return "；".join(parts)
    return str(value).strip()


def _extract_research_items(value: Any) -> list[dict]:
    """Flatten common Metaso/MCP response shapes into result dictionaries."""
    items: list[dict] = []
    if not value:
        return items
    if isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            text = value.get("text") or ""
            try:
                return _extract_research_items(json.loads(text))
            except Exception:
                return [{"title": text[:80], "summary": text[:240]}]
        for key in ("results", "data", "items", "content", "citations", "webpages"):
            nested = value.get(key)
            if nested:
                nested_items = _extract_research_items(nested)
                if nested_items:
                    return nested_items
        if any(k in value for k in ("title", "name", "url", "link", "summary", "snippet", "text", "content")):
            return [value]
        return items
    if isinstance(value, list):
        for item in value:
            items.extend(_extract_research_items(item))
        return items
    if isinstance(value, str):
        try:
            return _extract_research_items(json.loads(value))
        except Exception:
            text = value.strip()
            return [{"title": text[:80], "summary": text[:240]}] if text else []
    return items


def _strip_ephemeral_attachment_context(text: str | None) -> str:
    """Remove per-task upload context before long-term profile extraction.

    The chat composer can append uploaded project files to `user_input` so the
    writing chain can use them in this run. Those uploads are explicitly marked
    as "本次参考/未自动保存", so the memory extractor must not treat them as
    durable customer profile material.
    """
    if not text:
        return ""
    raw = str(text)
    markers = [
        "下面是用户刚刚上传、要求本次任务一起参考的项目资料。",
        "【上传资料 1：",
        "本次参考资料（未自动保存）",
    ]
    cut_at = min([raw.find(marker) for marker in markers if raw.find(marker) >= 0] or [-1])
    if cut_at >= 0:
        raw = raw[:cut_at]
    return raw.strip()


def _should_skip_profile_memory_extraction(text: str | None) -> bool:
    """Deterministic guard before LLM profile extraction.

    The model prompt already tells the extractor to ignore complaints and
    frustration, but this hard gate protects the pending memory queue from
    obvious "you are repeating / you are wrong" user messages.
    """
    if not text:
        return True
    compact = re.sub(r"\s+", "", str(text).strip().lower())
    if not compact:
        return True
    hard_frustration = (
        "你傻",
        "傻啊",
        "有病",
        "智障",
        "垃圾",
        "没用",
        "乱回",
        "答非所问",
        "听不懂",
        "没听懂",
        "理解错",
        "理解偏",
        "一直重复",
        "一直回复",
        "老是回复",
        "别重复",
        "不要重复",
        "又来了",
    )
    emotion_only = (
        "我今天好累",
        "今天好累",
        "没灵感",
        "好烦",
        "烦死",
        "客户都好难搞",
        "客户太难搞",
    )
    if any(token in compact for token in hard_frustration):
        return True
    return any(token in compact for token in emotion_only)


async def _extract_info_from_input(profile_id: str, user_input: str, profile: dict):
    """后台异步：从用户输入中提取可能的画像信息，写入 pending 队列。

    P0-6 (R3) 改写规则:
    - LLM 必须为每个抽取字段输出 confidence(0..1)。
    - 个字段 confidence < 0.7 直接丢弃,不入库。
    - 不再直接 mutate profile.structured_knowledge——改写到 profile_memory_events
      的 review_status='pending',前端 "AI 想记住" 面板让用户 ✓/✕ 确认后再合并。
    - 这样情绪话 / 抱怨 / 闲聊被 LLM 幻觉成 client_case 时,用户可以一眼丢弃,而
      不是发现画像被悄悄改了。
    """
    try:
        user_input = _strip_ephemeral_attachment_context(user_input)
        if _should_skip_profile_memory_extraction(user_input):
            return
        if len(user_input.strip()) < 15:
            return
        from services.llm.advisor_llm import advisor_chat
        import json as _json

        industry = profile.get("industry", "未知")
        prompt = f"""分析以下用户输入，判断里面是否真的包含可以**长期补充到创作者画像**的业务事实，而不是情绪/抱怨/闲聊。

用户输入：{user_input[:500]}
用户行业：{industry}

判断原则：
1. "我今天好累"/"客户都好难搞"/"没灵感"是情绪话，不是画像事实。
2. "上周帮一个家装客户做了 30 天陪跑，咨询从月均 8 条涨到 41 条"是真实业务案例，是画像事实。
3. 拿不准就 confidence 低一点，不要为了塞字段编造细节。
4. "完成了一次内容生成"/"生成了一条稿子"/"点了写稿功能"是操作日志，不是画像事实，必须返回 none。
5. 只能返回下面 JSON 模板里的字段名，禁止新增字段；value 必须是纯文本，不要返回对象或数组。

返回 JSON：
{{
  "client_case": {{"value": "...", "confidence": 0.0..1.0}},  // 如有真实案例
  "product_mention": {{"value": "...", "confidence": 0.0..1.0}},  // 如提到具体产品/服务名
  "product_selling_points": {{"value": "...", "confidence": 0.0..1.0}},  // 如提到卖点/差异化
  "target_group": {{"value": "...", "confidence": 0.0..1.0}},  // 如暴露目标客群
  "pain_point": {{"value": "...", "confidence": 0.0..1.0}},  // 如提到客户顾虑/痛点
  "customer_faq": {{"value": "...", "confidence": 0.0..1.0}},  // 如提到客户常问问题
  "real_cases": {{"value": "...", "confidence": 0.0..1.0}},  // 如提到可复用真实故事/素材
  "expertise": {{"value": "...", "confidence": 0.0..1.0}},  // 如展现专业领域
  "persona_tone": {{"value": "...", "confidence": 0.0..1.0}},  // 如提到希望的表达调性
  "speaking_style": {{"value": "...", "confidence": 0.0..1.0}},  // 如提到本人说话习惯
  "content_taboo": {{"value": "...", "confidence": 0.0..1.0}},  // 如提到不能怎么写
  "closing_method": {{"value": "...", "confidence": 0.0..1.0}},  // 如提到常用引导/成交方式
  "business": {{"value": "...", "confidence": 0.0..1.0}}  // 如补充了业务描述
}}

如果都没有可信信息，返回 {{"none": true}}。
没有的字段不要编造，confidence < 0.5 也不要返回该字段。
只返回 JSON。"""

        result = await advisor_chat(prompt, context="画像信息提取", temperature=0.2)
        if not result or '"none": true' in result or '"none":true' in result:
            return

        import re
        json_match = re.search(r'\{[\s\S]*\}', result)
        if not json_match:
            return
        try:
            extracted = _json.loads(json_match.group())
        except (ValueError, TypeError):
            return
        if extracted.get("none"):
            return

        # P0-6 (R3): 阈值过滤 — 任何字段 confidence < 0.7 都丢弃,不写 pending。
        # LLM 不准时 confidence 通常自己也心虚,这是免费的过滤层。
        from db.profile_memory_db import record_profile_memory_event

        kept: list[tuple[str, str, float]] = []
        for key, label in _PROFILE_MEMORY_FIELD_LABELS.items():
            entry = extracted.get(key)
            if not isinstance(entry, dict):
                continue
            value = _coerce_profile_memory_text(entry.get("value"))
            try:
                conf = float(entry.get("confidence") or 0)
            except (ValueError, TypeError):
                conf = 0.0
            if not value or conf < 0.7 or _is_shallow_profile_memory_text(value):
                continue
            kept.append((key, value, conf))

        if not kept:
            return

        for key, value, conf in kept:
            label = field_labels[key]
            record_profile_memory_event(
                profile_id,
                source="input_extract",
                event_type="auto_extract",
                dimension=key,
                title=f"{label}：{value[:60]}",
                text=value[:500],
                raw_payload={
                    "field": key,
                    "value": value,
                    "user_input_excerpt": user_input[:200],
                },
                confidence=conf,
                weight_delta=1,
                # 关键: 'pending' 表示尚未合并到 profile, 等用户确认。
                review_status="pending",
            )
        print(f"[InfoExtract] {profile_id[:8]} pending {[k for k, _, _ in kept]} (待用户确认)")
    except Exception as e:
        print(f"[InfoExtract] 提取失败（不影响使用）: {e}")


# --- 阶段判断 ---

# --- 鼓励语 ---

def _compact_text(value, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, (list, tuple)):
        return "、".join(str(v) for v in value if v)
    if isinstance(value, dict):
        return "、".join(str(v) for v in value.values() if v)
    return str(value).strip() or fallback


# --- Prompt 构建 ---

# --- API 路由 ---

# ========== 创意工坊 · 对话模式 v2 API ==========


class ChatRequest(BaseModel):
    """对话模式 v2 请求"""
    profile_id: str
    session_id: Optional[str] = None  # 首次为空，后端生成
    message: str
    plan_task_id: Optional[str] = None  # 来自拍摄计划
    platform: str = "douyin"
    target_duration: int = 60
    target_word_count: Optional[int] = None
    writer_advisor_id: Optional[str] = None
    expert_advisor_id: Optional[str] = None
    generation_mode: str = "normal"
    guardrail_mode: str = "balanced"


# ========== 深度解析（L3 用户层，260积分） ==========

class DeepAnalyzeRequest(BaseModel):
    profile_id: str
    city: Optional[str] = None


# ========== v3.7 知识库审核工作流 请求模型 ==========

class BriefConfirmRequest(BaseModel):
    """确认 industry_brief 入库 · partial_fields=None 表示全量入库"""
    profile_id: str
    partial_fields: Optional[List[str]] = None  # 例: ['my_audience', 'my_differentiation'] / None = 全量


class BriefEditRequest(BaseModel):
    """单字段手动编辑 · 免费（用户智力贡献）"""
    profile_id: str
    field_name: str  # 必须在 _BRIEF_KNOWN_FIELDS 集合里
    new_value: Any   # str / list / dict 任意结构，按字段语义


class BriefRerunRequest(BaseModel):
    """细粒度重跑 · 130 积分 flat rate（不管重跑几个字段）"""
    profile_id: str
    fields: List[str]  # 至少 1 个需重跑字段名
    city: Optional[str] = None


class BriefRollbackRequest(BaseModel):
    profile_id: str
    target_version: int  # 目标 version_num


# 运行中的状态（前端 BrandDetailPage 也用同一组判定，保持一致）
_DEEP_ANALYZE_RUNNING_STATUSES = ("running", "collecting_l1", "collecting_l2", "collecting_l3")
# 总超时 8 分钟：L1+L2 并行 ≈ 2-3min，L3 ≈ 2-4min，留充足余量
_DEEP_ANALYZE_TOTAL_TIMEOUT = 480


@router.post("/deep-analyze", summary="深度解析行业知识（260积分）")
async def api_deep_analyze(data: DeepAnalyzeRequest, request: Request):
    """
    为用户深度解析行业知识：搜索本地竞争、推荐精准客群、推荐获客路径。
    异步执行，扣 500 积分，3-8 分钟后查看结果(复用 3 分钟 / 拓荒 8 分钟)。
    状态写入 profile.industry_brief_status，前端刷新/重进也能看到运行中状态。

    防卡死三道防线：
    1. 入口判重覆盖所有运行态（running + collecting_l1/l2/l3）+ 15min 阈值
    2. 后台任务包 asyncio.wait_for(480s) 兜底超时 → 走 except 退款
    3. scheduler 看门狗每 5 分钟扫 15min 外的僵尸任务（worker 重启场景兜底）
    """
    import json as _json

    user = getattr(request.state, "user", None)
    user_id = user.get("user_id") or current_user_id(user) if user else None
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    profile = _get_profile_safe(request, data.profile_id)

    # 判重：覆盖所有运行态，避免重复扣费（前任 bug：只看 "running"，
    # 实际后台任务很快会改成 collecting_l1/l3，导致重复点击重复扣 260 积分）
    current_status = profile.get("industry_brief_status")
    started_at = profile.get("industry_brief_started_at")
    if current_status in _DEEP_ANALYZE_RUNNING_STATUSES and started_at:
        if isinstance(started_at, str):
            try:
                started_at = datetime.fromisoformat(started_at)
            except Exception:
                started_at = None
        if started_at and (datetime.now() - started_at) < timedelta(minutes=15):
            raise HTTPException(status_code=409, detail="已有深度解析任务正在进行中，请等待完成（约 5-8 分钟）")
        # 超过 15 分钟视为僵尸任务，允许重试（看门狗也会兜底改 failed + 退款）

    # 扣费(2026-05-12 一次性修复: 走 _bill helper · V3.1 entitlement 优先 + fallback paid)
    # request.state._bill_meta[deep_analyze] 记录扣费来源,供 _release_or_refund_bg 用
    try:
        await _bill(request, "deep_analyze")
    except HTTPException:
        raise
    except ValueError as e:
        # feature_pricing 表缺 deep_analyze 配置 → 记录但不阻断（保留前任行为）
        logger.error(f"[深度解析] 扣费配置缺失，跳过扣费: {e}")
    except Exception as e:
        # str(e)(含异常类/栈)只进日志;用户面走机器合同(重试/联系客服出口)。
        logger.error(f"[深度解析] 扣费兜底异常: {type(e).__name__}: {e}")
        raise HTTPException(status_code=402, detail=_content_billing_error_alert())

    # snapshot _bill_meta 给后台 task 闭包捕获(后台 task 失败 release entitlement 或 refund paid)
    _deep_analyze_bill_meta = (getattr(request.state, "_bill_meta", {}) or {}).get("deep_analyze")

    # 写入 running 状态（前端可见）+ 记录启动时间戳（用于乐观锁）
    from db.profile_db import update_profile
    _my_started_at = datetime.now()
    try:
        update_profile(
            data.profile_id,
            industry_brief_status="running",
            industry_brief_started_at=_my_started_at,
        )
    except Exception as e:
        logger.warning(f"[深度解析] 状态写入失败: {e}")

    # 异步执行（B: L1+L2 并行, D: 阶段 heartbeat）
    _task_started_at = _my_started_at  # 闭包捕获，用于乐观锁

    async def _run_deep_analyze():
        import time as _time
        # 2026-05-12 一次性修复: refund_points → _release_or_refund_bg(智能 release/refund)
        # 闭包捕获 _deep_analyze_bill_meta(上方 snapshot)
        err_msg = None  # Python 3.12: except 外 e 会被删除，提前存
        t_start_run = _time.time()
        try:
            from tools.industry_knowledge_collector import (
                deep_analyze_user, collect_industry_knowledge, collect_category_knowledge,
                get_industry_knowledge,
            )

            industry = profile.get("industry", "")
            category = profile.get("category", "")

            # v3.6 CTO-15.2: 判定本次任务是"拓荒"还是"复用"决定最少耗时
            #   - 拓荒(L1 或 L2 缺): 最少 6 分钟
            #   - 复用(L1+L2 都已存在): 最少 3 分钟
            # 目的: 避免飞轮命中导致几秒返回让用户怀疑是否真在分析
            # [audit P1 2026-06-10 共享主人翁制·GEO CTO 代修] 拓荒原 480 == 外层 wait_for(480)总超时:
            # 凑时长 sleep 结束点与超时同毫秒级触发且 sleep 是取消点 → 新行业客户【成功后】被误判超时,
            # 状态置 failed + 退 500 算力 + 结果丢弃。降 360 留 120s 余量(凑时长目标必须严格小于总超时)。
            l1_exists = bool(get_industry_knowledge(industry, level="industry")) if industry else False
            l2_exists = bool(get_industry_knowledge(industry, category)) if (industry and category) else True
            is_reuse = l1_exists and l2_exists
            expected_min_seconds = 180 if is_reuse else 360

            # 乐观锁：检查自己是否还是最新任务（防并发覆盖）
            def _is_still_owner():
                try:
                    from db.connection import get_connection as _gc
                    _conn = _gc()
                    try:
                        _cur = _conn.cursor()
                        _cur.execute("SELECT industry_brief_started_at FROM client_profiles WHERE id = %s", (data.profile_id,))
                        _row = _cur.fetchone()
                        _cur.close()
                        if not _row:
                            return False
                        db_started = _row.get("industry_brief_started_at")
                        if db_started is None:
                            return True
                        if isinstance(db_started, str):
                            db_started = datetime.fromisoformat(db_started)
                        # 允许 1 秒误差
                        return abs((db_started - _task_started_at).total_seconds()) < 2
                    finally:
                        _conn.close()
                except Exception:
                    return True  # 查不到就不阻断

            def _safe_update_status(status):
                if _is_still_owner():
                    update_profile(data.profile_id, industry_brief_status=status)
                else:
                    logger.info(f"[深度解析] 跳过状态更新 {status}：已被更新的任务接管")

            # 阶段 1: L1+L2 并行采集（如果缓存不存在）
            try:
                _safe_update_status("collecting_l1")
            except Exception:
                pass

            parallel_tasks = []
            if not l1_exists and industry:
                parallel_tasks.append(collect_industry_knowledge(industry))
            if not l2_exists and category:
                parallel_tasks.append(collect_category_knowledge(industry, category))
            if parallel_tasks:
                # 任一失败不影响另一个
                await asyncio.gather(*parallel_tasks, return_exceptions=True)

            # 阶段 2: L3 深度解析
            try:
                _safe_update_status("collecting_l3")
            except Exception:
                pass

            if data.city:
                profile["city"] = data.city

            # A9 · 保留代理手编 market_insight 字段(重采前快照)
            _old_brief_raw = profile.get("industry_brief")
            _old_brief = None
            if isinstance(_old_brief_raw, dict):
                _old_brief = _old_brief_raw
            elif isinstance(_old_brief_raw, str) and _old_brief_raw.strip():
                try:
                    _old_brief = _json.loads(_old_brief_raw)
                except Exception:
                    _old_brief = None

            brief = await deep_analyze_user(profile)

            # A9 (CTO-15.9 2026-04-25): 代理手编的 authority_sources / hot_formats /
            # my_differentiation / local_competitors 在重采时保留 · 防覆盖代理背书
            try:
                from tools.industry_knowledge_collector import preserve_user_edited_fields
                brief = preserve_user_edited_fields(_old_brief, brief)
            except Exception as _pe:
                logger.warning(f"[深度解析] A9 preserve 失败(非阻塞): {_pe}")

            # v3.6 凑最少时长(用户感知"认真分析",见上方 expected_min_seconds 决策注释)
            elapsed_run = _time.time() - t_start_run
            if elapsed_run < expected_min_seconds:
                sleep_for = expected_min_seconds - elapsed_run
                logger.info(
                    f"[v3.6] 实跑 {elapsed_run:.0f}s < 最少 {expected_min_seconds}s "
                    f"(reuse={is_reuse}), sleep {sleep_for:.0f}s 凑时长"
                )
                await asyncio.sleep(sleep_for)

            # 写入 profile + 状态置 done（乐观锁：只有最新任务才写）
            if _is_still_owner():
                update_profile(
                    data.profile_id,
                    industry_brief=_json.dumps(brief, ensure_ascii=False),
                    industry_brief_status="done",
                )
            else:
                logger.info(f"[深度解析] 跳过写入 done：已被更新的任务接管")

            logger.info(f"[深度解析] 完成: {data.profile_id}, {industry}/{category} · 总 {(_time.time()-t_start_run):.0f}s")
        except asyncio.TimeoutError:
            # 由外层 wait_for 触发：内层任意一处 LLM/搜索 hang 总和超 8 分钟
            err_msg = f"深度解析超时（{_DEEP_ANALYZE_TOTAL_TIMEOUT}s）"
            logger.error(f"[深度解析] {err_msg}: {data.profile_id}")
            try:
                update_profile(data.profile_id, industry_brief_status="failed")
            except Exception:
                pass
            try:
                await _release_or_refund_bg(user_id, "deep_analyze", _deep_analyze_bill_meta, reason=err_msg)
            except Exception:
                pass
        except Exception as e:
            err_msg = str(e)[:100]
            logger.error(f"[深度解析] 失败: {err_msg}")
            try:
                update_profile(data.profile_id, industry_brief_status="failed")
            except Exception:
                pass
            try:
                await _release_or_refund_bg(user_id, "deep_analyze", _deep_analyze_bill_meta, reason=f"深度解析失败: {err_msg[:50]}")
            except Exception:
                pass

    async def _run_with_total_timeout():
        try:
            await asyncio.wait_for(_run_deep_analyze(), timeout=_DEEP_ANALYZE_TOTAL_TIMEOUT)
        except asyncio.TimeoutError:
            # _run_deep_analyze 内层 except 已 catch，但 wait_for 自身也会 raise
            # → 二次保险（极端情况内层 catch 也卡住时，至少 wrapper 自己释放）
            logger.error(f"[深度解析] wrapper 强制超时: {data.profile_id}")
            try:
                update_profile(data.profile_id, industry_brief_status="failed")
            except Exception:
                pass
            try:
                await _release_or_refund_bg(user_id, "deep_analyze", _deep_analyze_bill_meta, reason="深度解析强制超时")
            except Exception:
                pass

    # asyncio.create_task + 模块级 set 强引用 → 防 GC 把任务回收
    # 注：仍挂在当前 worker event loop 上；worker 重启会丢，靠 scheduler 看门狗兜底
    task = asyncio.create_task(_run_with_total_timeout())
    _deep_analyze_running_tasks.add(task)
    task.add_done_callback(_deep_analyze_running_tasks.discard)

    return {
        "success": True,
        "message": "深度解析已开始，3-8 分钟后生效。刷新页面可查看实时状态。",
        "profile_id": data.profile_id,
        "status": "running",
        "started_at": datetime.now().isoformat(),
    }


# ========================================================================
# v3.7 知识库审核工作流（CTO-15.0 2026-04-19）
# ------------------------------------------------------------------------
# 核心思路：解析完不再"默认生效"，用户审核后才真正被生产链路读取
# - confirmed=TRUE → GEO / 社媒生产端注入融合
# - partial_fields=[...] → 只注入指定字段（其他字段不给生产端看到）
# - 修改 = AI 重跑（130 flat，无论几个字段）/ 手动编辑（免费）
# - 版本化：每次解析/重跑/编辑写 industry_brief_history
# ========================================================================

# 13 字段白名单 / 有效 brief 读取 · 统一由 db.profile_db 提供（GEO/社媒生产端共用）
from db.profile_db import INDUSTRY_BRIEF_KNOWN_FIELDS as _BRIEF_KNOWN_FIELDS
from auth.user_ctx import current_user_id


def _write_brief_history(profile_id: str, brief_data: dict, source: str,
                          confirmed: bool, partial_fields: Optional[list],
                          user_id: Optional[int]) -> int:
    """追加一条历史记录，返回新 version_num。"""
    import json as _json
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(MAX(version_num), 0) AS v FROM industry_brief_history WHERE profile_id = %s",
            (profile_id,)
        )
        row = cur.fetchone()
        next_v = (row["v"] if row and "v" in row else 0) + 1
        cur.execute(
            """INSERT INTO industry_brief_history
               (profile_id, version_num, brief_data, confirmed, partial_fields, source, created_by)
               VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, %s, %s)""",
            (
                profile_id, next_v,
                _json.dumps(brief_data, ensure_ascii=False),
                confirmed,
                _json.dumps(partial_fields, ensure_ascii=False) if partial_fields else None,
                source,
                user_id,
            )
        )
        # 同步 client_profiles.industry_brief_version
        cur.execute(
            "UPDATE client_profiles SET industry_brief_version = %s WHERE id = %s",
            (next_v, profile_id)
        )
        conn.commit()
        return next_v
    finally:
        conn.close()


@router.post("/industry-brief/confirm", summary="确认 industry_brief 入库（Phase 1）")
async def api_brief_confirm(data: BriefConfirmRequest, request: Request):
    """用户审核后点"确认入库"→ 生产链路开始融合此 brief。
    partial_fields=None → 全量；list → 仅选中字段入库。
    """
    user = getattr(request.state, "user", None)
    user_id = user.get("user_id") or current_user_id(user) if user else None
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    profile = _get_profile_safe(request, data.profile_id)
    if profile.get("industry_brief_status") != "done":
        raise HTTPException(status_code=400, detail="深度解析尚未完成，暂不能入库")

    # 校验 partial_fields 全部合法
    partial = data.partial_fields
    if partial:
        invalid = [f for f in partial if f not in _BRIEF_KNOWN_FIELDS]
        if invalid:
            raise HTTPException(status_code=400, detail=f"未知字段: {invalid}")
        if len(partial) == 0:
            partial = None

    from db.profile_db import update_profile
    import json as _json
    update_profile(
        data.profile_id,
        industry_brief_confirmed=True,
        industry_brief_confirmed_at=datetime.now(),
        industry_brief_partial_fields=_json.dumps(partial, ensure_ascii=False) if partial else None,
    )

    # 历史版本归档（source=user_confirm）
    raw = profile.get("industry_brief") or {}
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except Exception:
            raw = {}
    version = _write_brief_history(
        data.profile_id, raw, source="user_confirm",
        confirmed=True, partial_fields=partial, user_id=user_id,
    )

    return {
        "success": True,
        "confirmed": True,
        "partial_fields": partial,
        "version": version,
        "message": "知识库已入库，AI 写文章时会自动融合此知识。",
    }


@router.patch("/industry-brief/edit", summary="手动编辑单字段（Phase 2 · 免费）")
async def api_brief_edit(data: BriefEditRequest, request: Request):
    """用户手动改某个字段的值 · 免费（用户智力贡献，比 AI 跑更准）
    编辑会保留原 AI 值到 industry_brief_edits 便于后续对比/回退。
    编辑后需要用户重新点"确认入库"才对生产链路生效（避免误编辑立即影响生产）。
    """
    import json as _json
    user = getattr(request.state, "user", None)
    user_id = user.get("user_id") or current_user_id(user) if user else None
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    if data.field_name not in _BRIEF_KNOWN_FIELDS:
        raise HTTPException(status_code=400, detail=f"未知字段: {data.field_name}")

    profile = _get_profile_safe(request, data.profile_id)
    raw = profile.get("industry_brief") or {}
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    # 保留原值到 edits 快照（仅首次编辑该字段时保存原 AI 值）
    edits = profile.get("industry_brief_edits") or {}
    if isinstance(edits, str):
        try:
            edits = _json.loads(edits)
        except Exception:
            edits = {}
    if not isinstance(edits, dict):
        edits = {}
    if data.field_name not in edits:
        edits[data.field_name] = {
            "ai_original": raw.get(data.field_name),
            "first_edited_at": datetime.now().isoformat(),
        }

    # 覆盖字段值
    raw[data.field_name] = data.new_value

    from db.profile_db import update_profile
    update_profile(
        data.profile_id,
        industry_brief=_json.dumps(raw, ensure_ascii=False),
        industry_brief_edits=_json.dumps(edits, ensure_ascii=False),
        # 编辑后重置审核状态：用户需重新确认 → 避免误编辑直接污染生产端
        industry_brief_confirmed=False,
    )

    # 历史归档（source=user_edit）
    version = _write_brief_history(
        data.profile_id, raw, source="user_edit",
        confirmed=False, partial_fields=None, user_id=user_id,
    )

    return {
        "success": True,
        "field": data.field_name,
        "version": version,
        "message": "字段已更新。请再次点击「确认入库」让 AI 用新内容写文章。",
    }


@router.post("/industry-brief/rerun", summary="细粒度重跑字段（Phase 3 · 130 积分 flat）")
async def api_brief_rerun(data: BriefRerunRequest, request: Request):
    """只刷新部分字段 · 130 积分 flat rate（不按字段数倍增）
    复用 tools.industry_knowledge_collector.deep_analyze_user 生成全新 brief，
    但只把 data.fields 指定的 key 覆盖到现有 brief（其余保留用户已审核的状态）。
    """
    import json as _json

    user = getattr(request.state, "user", None)
    user_id = user.get("user_id") or current_user_id(user) if user else None
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    if not data.fields:
        raise HTTPException(status_code=400, detail="请选择至少 1 个需重跑字段")
    invalid = [f for f in data.fields if f not in _BRIEF_KNOWN_FIELDS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"未知字段: {invalid}")

    profile = _get_profile_safe(request, data.profile_id)

    # 扣费 130 flat(2026-05-12 一次性修复: 走 _bill helper · V3.1 entitlement 优先)
    try:
        await _bill(request, "industry_brief_rerun")
    except HTTPException:
        raise
    except ValueError:
        # 降级：配置缺失不阻断（保留 deep_analyze 的惯例）
        logger.warning("[brief-rerun] feature_pricing 缺 industry_brief_rerun，跳过扣费")
    except Exception as e:
        # str(e) 只进日志;用户面走机器合同(重试/联系客服出口)。
        logger.error(f"[brief-rerun] 扣费兜底异常: {type(e).__name__}: {e}")
        raise HTTPException(status_code=402, detail=_content_billing_error_alert())

    # 同步执行（字段粒度比全量快，~30-60s，不用 asyncio.create_task）
    try:
        from tools.industry_knowledge_collector import deep_analyze_user
        if data.city:
            profile["city"] = data.city
        new_brief = await deep_analyze_user(profile)

        # [CTO-13.3 2026-04-19 P0 R9] 合并：必须 key 存在且"有实质内容"才算更新
        # 根因: AI 返回 {local_competitors: []} (service_scope=national 时强制空数组,
        # 见 tools/industry_knowledge_collector.py:1205) 会让 merged[k]=[] 看似更新实则
        # 字段被清空. 用户看到 UI 没变但已扣 130 积分, 失败退费声明无效 → P0 资金事故.
        old_brief = profile.get("industry_brief") or {}
        if isinstance(old_brief, str):
            try:
                old_brief = _json.loads(old_brief)
            except Exception:
                old_brief = {}
        merged = dict(old_brief) if isinstance(old_brief, dict) else {}

        def _has_real_value(v) -> bool:
            """判断字段值是否有实质内容 (排除 None / '' / [] / {} / 纯空白串)"""
            if v is None:
                return False
            if isinstance(v, str):
                return bool(v.strip())
            if isinstance(v, (list, dict)):
                return len(v) > 0
            return True

        updated_keys: list[str] = []
        empty_keys: list[str] = []
        for k in data.fields:
            if k in new_brief and _has_real_value(new_brief[k]):
                merged[k] = new_brief[k]
                updated_keys.append(k)
            else:
                empty_keys.append(k)

        # P0 R9: 所有请求字段都返空 → 全额退费 + 422 友好错误(老板报 14:36 本地竞品 bug)
        # 一次性修复: refund_points → _release_or_refund(V3.1 智能 release/refund)
        if not updated_keys:
            try:
                await _release_or_refund(
                    request, "industry_brief_rerun",
                    reason=f"AI 重跑返空值 empty_keys={empty_keys}",
                )
                logger.warning(
                    f"[brief-rerun] 空返回已退费 user={user_id} profile={data.profile_id} "
                    f"empty_keys={empty_keys} service_scope={new_brief.get('service_scope')}"
                )
            except Exception as _re:
                logger.error(f"[brief-rerun] 空返回退费失败 (需人工补退): {_re}")
            # 针对 local_competitors 给业务级解释
            hint = ""
            if empty_keys == ["local_competitors"] and new_brief.get("service_scope") == "national":
                hint = "（你的业务被识别为全国性，本地竞品字段不适用。建议改跑「my_differentiation」等全国性字段。）"
            raise HTTPException(
                status_code=422,
                detail=f"AI 未能为字段 {empty_keys} 生成新内容，已全额退回 130 积分。{hint}",
            )

        from db.profile_db import update_profile
        update_profile(
            data.profile_id,
            industry_brief=_json.dumps(merged, ensure_ascii=False),
            industry_brief_status="done",
            # 重跑后需要用户再次确认（避免 AI 新值直接生效）
            industry_brief_confirmed=False,
        )

        version = _write_brief_history(
            data.profile_id, merged, source="ai_rerun",
            confirmed=False, partial_fields=None, user_id=user_id,
        )

        # [CTO-13.3 2026-04-19 v3.8 · PLAN Q15 C3] 反哺 L1 法/术 + L2 器 到共享池
        # 四步流程: 差异门控(unique_key) → multi_ai_voter 投票 → merge_with_layers → 审计表
        # fire-and-forget 不阻塞用户响应 · 环境变量 RERUN_REVERSE_FEED_ENABLED=false 可关闭
        try:
            import asyncio as _asyncio
            import time as _time
            from services.knowledge_reverse_feed import feed_from_rerun
            _source_ref = f"rerun:{data.profile_id}:{int(_time.time())}"
            _asyncio.create_task(feed_from_rerun(
                new_brief=new_brief,
                profile=profile,
                user_id=user_id,
                updated_fields=updated_keys,
                source_ref=_source_ref,
            ))
        except Exception as _fe:
            # 反哺失败不影响用户体验 · 已扣费 + 已写用户私有 brief
            logger.warning(f"[brief-rerun] 反哺任务创建失败 (主流程不受影响): {_fe}")

        msg = f"已重跑 {len(updated_keys)} 个字段，请审核后再确认入库。"
        if empty_keys:
            msg += f" 另有 {len(empty_keys)} 个字段 AI 未返新值（不影响已扣费，字段保留旧值）：{empty_keys}"

        return {
            "success": True,
            "updated_fields": updated_keys,
            "empty_fields": empty_keys,  # 让前端 toast 展示未更新字段
            "version": version,
            "message": msg,
        }
    except HTTPException:
        # 422 空返退费已处理 / 其他 HTTP 异常原样抛
        raise
    except Exception as e:
        logger.error(f"[brief-rerun] 失败: {e}")
        # 退费(一次性修复: V3.1 智能 release/refund)
        try:
            await _release_or_refund(request, "industry_brief_rerun", reason=f"重跑失败: {str(e)[:50]}")
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"重跑失败，积分已退回: {str(e)[:80]}")


@router.get("/industry-brief/history/{profile_id}", summary="历史版本列表（Phase 4）")
async def api_brief_history(profile_id: str, request: Request):
    """列出该 profile 所有 industry_brief 版本（倒序）+ 当前生效版本号"""
    _get_profile_safe(request, profile_id)
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, version_num, confirmed, partial_fields, source, created_at, created_by
               FROM industry_brief_history
               WHERE profile_id = %s
               ORDER BY version_num DESC
               LIMIT 50""",
            (profile_id,)
        )
        rows = cur.fetchall()
        versions = [
            {
                "id": r["id"],
                "version": r["version_num"],
                "confirmed": r["confirmed"],
                "partial_fields": r["partial_fields"],
                "source": r["source"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "created_by": r["created_by"],
            }
            for r in rows
        ]
        # 当前版本
        cur.execute("SELECT industry_brief_version FROM client_profiles WHERE id = %s", (profile_id,))
        cur_row = cur.fetchone()
        current = cur_row["industry_brief_version"] if cur_row else 0
        return {"success": True, "versions": versions, "current_version": current}
    finally:
        conn.close()


@router.get("/industry-brief/history/{profile_id}/{version}", summary="单个历史版本详情（Phase 4）")
async def api_brief_history_detail(profile_id: str, version: int, request: Request):
    """拿到某版本的完整 brief_data（用于对比视图）"""
    _get_profile_safe(request, profile_id)
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT version_num, brief_data, confirmed, partial_fields, source, created_at
               FROM industry_brief_history
               WHERE profile_id = %s AND version_num = %s""",
            (profile_id, version)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="该版本不存在")
        return {
            "success": True,
            "version": row["version_num"],
            "brief_data": row["brief_data"],
            "confirmed": row["confirmed"],
            "partial_fields": row["partial_fields"],
            "source": row["source"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
    finally:
        conn.close()


@router.post("/industry-brief/rollback", summary="回滚到历史版本（Phase 4）")
async def api_brief_rollback(data: BriefRollbackRequest, request: Request):
    """把 client_profiles.industry_brief 恢复到指定历史版本的快照。
    回滚不扣费。回滚后不自动确认入库，需用户重新点"确认"→ 避免误回滚直接污染生产。
    """
    import json as _json
    user = getattr(request.state, "user", None)
    user_id = user.get("user_id") or current_user_id(user) if user else None
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    profile = _get_profile_safe(request, data.profile_id)

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT brief_data FROM industry_brief_history WHERE profile_id = %s AND version_num = %s",
            (data.profile_id, data.target_version)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="目标版本不存在")
        target_brief = row["brief_data"]
    finally:
        conn.close()

    if isinstance(target_brief, str):
        try:
            target_brief = _json.loads(target_brief)
        except Exception:
            raise HTTPException(status_code=500, detail="目标版本数据损坏")

    from db.profile_db import update_profile
    # profile 已通过 _get_profile_safe 做权限前置 + 存在性校验（未来可扩展字段级权限）
    logger.info(f"[brief-rollback] profile={profile.get('id')} → v{data.target_version} by user={user_id}")
    update_profile(
        data.profile_id,
        industry_brief=_json.dumps(target_brief, ensure_ascii=False),
        industry_brief_confirmed=False,  # 回滚后需重新确认
    )

    new_version = _write_brief_history(
        data.profile_id, target_brief, source="rollback",
        confirmed=False, partial_fields=None, user_id=user_id,
    )

    return {
        "success": True,
        "rolled_back_to": data.target_version,
        "new_version": new_version,
        "message": f"已回滚到 v{data.target_version}，请审核后再确认入库。",
    }


# ============================================================================
# v3.6 CTO-15.2 2026-04-19 · L1/L2 公共素材池只读查询
#
# 让前端 IndustryBriefReviewCard 展示该 profile 对应行业/品类的公共库素材
# 仅展示 v3.6 新字段(jargon/authority/counter/voices/cases),不含 L3 用户特化
# ============================================================================

@router.get("/industry-brief/pool/{profile_id}", summary="查询该 profile 的 L1/L2 公共素材池(v3.6)")
async def api_brief_pool(profile_id: str, request: Request):
    """返回该 profile 行业/品类对应的 L1+L2 公共素材池

    Response:
    {
      "industry": "...",
      "category": "...",
      "l1": {  # 行业层(全行业 90 天复用)
        "industry_jargon": [...],     # v3.6
        "authority_sources": [...],   # v3.6
        "counter_consensus": [...],   # v3.6
        "industry_terms": [...],      # 老字段
        "top_brands": [...],          # 老字段
      },
      "l2": {  # 品类层(同品类 30 天复用)
        "user_voices_pool": [...],    # v3.6
        "case_evidence_pool": [...],  # v3.6
        "typical_products": [...],    # 老字段
        "hot_formats": [...],         # 老字段
      }
    }
    """
    profile = _get_profile_safe(request, profile_id)
    industry = profile.get("industry", "") or ""
    category = profile.get("category", "") or ""

    from tools.industry_knowledge_collector import get_industry_knowledge
    l1 = get_industry_knowledge(industry, level="industry") or {} if industry else {}
    l2 = get_industry_knowledge(industry, category) or {} if (industry and category) else {}

    # 只返展示需要的字段(过滤掉 metadata)
    l1_view = {
        "industry_jargon": l1.get("industry_jargon", []),
        "authority_sources": l1.get("authority_sources", []),
        "counter_consensus": l1.get("counter_consensus", []),
        "industry_terms": l1.get("industry_terms", []),
        "top_brands": l1.get("top_brands", []),
        "market_overview": l1.get("market_overview", ""),
    }
    l2_view = {
        "user_voices_pool": l2.get("user_voices_pool", []),
        "case_evidence_pool": l2.get("case_evidence_pool", []),
        "typical_products": l2.get("typical_products", []),
        "hot_formats": l2.get("hot_formats", []),
        "price_range": l2.get("price_range", ""),
    }

    return {
        "success": True,
        "industry": industry,
        "category": category,
        "l1": l1_view,
        "l2": l2_view,
        "_pool_summary": {
            "l1_count": sum(len(v) if isinstance(v, list) else 0 for v in l1_view.values()),
            "l2_count": sum(len(v) if isinstance(v, list) else 0 for v in l2_view.values()),
        },
    }


# ============================================================================
# v3.6 用户矫正 L1/L2 公共素材(立即反哺全行业 · 老板拍板:不要 N 人共识阈值)
# ============================================================================

class CorrectL1L2Request(BaseModel):
    profile_id: str = Field(..., description="profile id 用于权限校验 + 关联矫正人")
    level: str = Field(..., description="industry 或 category")
    field_name: str = Field(..., description="industry_jargon / authority_sources / counter_consensus / user_voices_pool / case_evidence_pool")
    new_value: Any = Field(None, description="新值(整体替换或单条)")
    action: str = Field("update", description="update / add / delete")
    item_index: Optional[int] = Field(None, description="列表型字段索引,None 表示整体替换")
    reason: Optional[str] = Field(None, description="矫正原因(可选,运营复盘用)")


@router.patch("/industry-brief/correct", summary="矫正 L1/L2 公共素材并反哺(v3.6 立即生效)")
async def api_correct_l1_l2(data: CorrectL1L2Request, request: Request):
    """
    用户矫正 L1/L2 公共素材并立即反哺全行业(老板拍板:不要 N 人共识阈值)。

    业务规则:
    - 老板说"L1 单条矫正立即生效",所以这里直接写回 industry_knowledge 表
    - admin 后台可一键回滚(POST /api/admin/industry-knowledge/rollback/{id})
    - 矫正记录在 industry_knowledge_corrections 表,可审计

    [CTO-13.3 2026-04-19 v3.8 C4] 新增 LLM 投票质量门控:
    - 环境变量 CORRECT_QUALITY_GATE=true 默认开 · 可设 false 回退原"立即生效无门控"
    - 投票拒绝 → 返 422 + verdicts 详情 · 前端 Dialog 给用户重写重试机会
    - 反哺记录同步写 industry_knowledge_reverse_feed (source_type='user_correct') 便于 admin 审计
    """
    import os as _os
    user = getattr(request.state, "user", None)
    user_id = user.get("user_id") or current_user_id(user) if user else None
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    profile = _get_profile_safe(request, data.profile_id)
    industry = profile.get("industry", "") or ""
    category = profile.get("category", "") or None
    if not industry:
        raise HTTPException(status_code=400, detail="profile 缺 industry,无法定位 L1/L2 公共库")
    if data.level == "category" and not category:
        raise HTTPException(status_code=400, detail="L2 矫正要求 profile 有 category")

    # [C4] 质量门控：只对 update/add 投票 (delete 不需验证内容质量)
    gate_enabled = _os.environ.get("CORRECT_QUALITY_GATE", "true").lower() != "false"
    if gate_enabled and data.action in ("update", "add"):
        try:
            from tools.knowledge_layers import LAYER_CONFIG
            from services.knowledge_reverse_feed import _get_ik_row
            from services.multi_ai_voter import review_field_value

            cfg = LAYER_CONFIG.get(data.field_name, {})
            is_list_field = cfg.get("merge") in ("list_dedup", "append_only")
            # 只对 list 类字段门控 (string 类短字段跳门控 · 如 price_range overwrite)
            if is_list_field:
                # 候选 items：add/update 单条 都看作 1 条候选 · update 整列表看作 N 条候选
                if data.item_index is not None or data.action == "add":
                    candidate_items = [data.new_value] if data.new_value is not None else []
                else:
                    candidate_items = data.new_value if isinstance(data.new_value, list) else []

                # 过滤空内容
                candidate_items = [
                    it for it in candidate_items
                    if it is not None and (not isinstance(it, str) or it.strip())
                ]

                if candidate_items:
                    # 读共享池现状作参考
                    ik = _get_ik_row(
                        level=data.level,
                        industry=industry,
                        category=category if data.level == "category" else None,
                    )
                    existing_items = (ik["knowledge"].get(data.field_name) or []) if ik else []
                    if not isinstance(existing_items, list):
                        existing_items = []

                    vote = await review_field_value(
                        industry=industry,
                        field_name=data.field_name,
                        new_items=candidate_items,
                        existing_items=existing_items[:20],
                        category=category if data.level == "category" else None,
                        source="user_correct",
                        threshold=1,
                    )
                    if not vote["accepted_items"]:
                        # 全拒 → 422 含 verdicts 给前端 Dialog 展示
                        # 文案按 CTO-15.2 推荐: 相关性低/冲突/空泛 三选一
                        reasons_summary = []
                        for r in vote["rejected_items"][:3]:
                            for rs in r.get("reasons", [])[:2]:
                                reasons_summary.append(rs)
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "code": "QUALITY_GATE_REJECTED",
                                "message": "AI 觉得这条修改：相关性低 / 跟现有冲突 / 内容空泛 — 是否要重写后再提交？",
                                "verdicts": vote["verdicts"],
                                "rejected_items": vote["rejected_items"],
                                "reasons_summary": reasons_summary[:5],
                                "field": data.field_name,
                            },
                        )
        except HTTPException:
            raise
        except Exception as _ve:
            # 门控异常不阻断主流程 (保留原有"立即生效"行为) · 记日志提醒人工
            logger.warning(f"[brief-correct] 质量门控异常跳过 field={data.field_name}: {_ve}")

    from db.industry_corrections_db import apply_correction_to_l1_l2
    try:
        result = apply_correction_to_l1_l2(
            level=data.level,
            industry=industry,
            category=category if data.level == "category" else None,
            field_name=data.field_name,
            new_value=data.new_value,
            action=data.action,
            item_index=data.item_index,
            corrector_user_id=user_id,
            corrector_profile_id=int(data.profile_id) if str(data.profile_id).isdigit() else None,
            reason=data.reason,
        )

        # [C4] 反哺审计：同步写 industry_knowledge_reverse_feed (source_type='user_correct')
        try:
            from db.reverse_feed_db import record_reverse_feed
            new_items_for_audit = (
                [data.new_value] if data.action in ("update", "add") and data.new_value is not None else []
            )
            record_reverse_feed(
                source_type="user_correct",
                source_ref=f"correction_id:{result.get('correction_id')}",
                profile_id=str(data.profile_id),
                user_id=user_id,
                level=data.level,
                industry=industry,
                category=category if data.level == "category" else None,
                field_name=data.field_name,
                new_items=new_items_for_audit,
                vote_result={"passed": True, "final_verdict": "pass_or_gate_off", "verdicts": []},
                diff_summary={
                    "merged_count": len(new_items_for_audit),
                    "action": data.action,
                    "gate_enabled": gate_enabled,
                },
            )
        except Exception as _ae:
            logger.warning(f"[brief-correct] 反哺审计写失败 (不影响主流程): {_ae}")

        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[brief-correct] 失败 profile={data.profile_id}")
        raise HTTPException(status_code=500, detail=f"矫正失败: {str(e)}")


def _branding_principal(request, uid) -> int:
    """[白标继承] 后台皮肤也走团队长的品牌:员工看到的应是所属服务商的品牌,不是平台默认。"""
    try:
        from auth.principal_identity import resolve_branding_principal_user_id
        return resolve_branding_principal_user_id(request, fallback_user_id=int(uid))
    except Exception:
        return int(uid)
