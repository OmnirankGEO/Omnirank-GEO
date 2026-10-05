"""小榜每轮的**结构化观测事件**(包 E · 工单 §6 包 E)。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 设计的第一条:这是一个**只出不进**的白名单

工单 §6 包 E 的禁列很长(JWT / Authorization / 完整截图 OCR / 用户消息正文 /
联系方式 / 跨租户对象内容)。**按黑名单做必然漏** —— 漏的那一天没有人会发现,
因为日志里多一个字段不会让任何东西变红。

所以这里反过来:`build_turn_event()` **只组装白名单里的键**,
其余一律进不来;`assert_no_sensitive_payload()` 再做一次形态复核,
两层都在同一个模块里,判据逐条打。

## 第二条:correlation id 不可反推

`turn_id` / `session_correlation_id` 都是**盐化哈希**,不是 token 的截断,
也不是 session_id 原文 —— 后者能直接拿去跟浏览器 localStorage 对上号。

## 第三条:六个管理端指标必须**能从这些字段算出来**

工单要的六个率(帮助中心唯一答复率 / 重复 fallback 率 / 无动作低置信率 /
无效 route 率 / 人工接管率 / 已解决率)+ 知识版本分布,
每一个都对应下面某几个字段的组合。判据 `test_every_required_metric_is_derivable`
逐个把它们算一遍 —— 「记了一堆字段但算不出要的指标」是最常见的观测面假绿。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Iterable, Mapping, Optional

_LOGGER = logging.getLogger("GEO-XiaobangTelemetry")

EVENT_NAME = "xiaobang.turn"

#: 🔴 事件的**完整**键集合。多一个键都进不来(见 build_turn_event 末尾的断言)。
ALLOWED_KEYS: frozenset[str] = frozenset({
    "event", "turn_id", "session_correlation_id",
    "actor_kind", "current_route", "route_valid",
    "intent_kind", "deterministic_hit",
    "knowledge_manifest_version", "knowledge_status",
    "retrieval_state", "answer_state", "freshness_state",
    "exit_ids", "action_kinds",
    "repeat_fallback", "help_center_only",
    "handoff_submitted", "ticket_id", "resolution",
    "error_kind", "adapter_state",
})

#: 身份**类型**,不是身份本身(不落 user_id)。
ACTOR_KINDS = ("normal_user", "agent", "member", "admin", "unknown")


def _salted(value: str, *, kind: str) -> str:
    """盐化哈希。

    🔴 盐里带 `kind`,让同一个原值在不同用途下算出不同哈希 ——
       否则拿 session 的哈希去跟 turn 的哈希碰撞就能反推关系。
       这里**不保留原值**,也不做可逆编码。
    """
    raw = "{0}::{1}".format(kind, value or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ── 敏感形态复核(白名单之外的第二层)────────────────────────────────
_JWT_SHAPE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_BEARER = re.compile(r"(?i)\b(bearer|authorization)\b")
_PHONE = re.compile(r"\b1[3-9]\d{9}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def assert_no_sensitive_payload(event: Mapping[str, Any], *, where: str = "") -> None:
    """形态复核:事件里不许出现 token / 联系方式 / 长文本正文。

    白名单已经挡住了「结构上的泄漏」,这一层挡的是「值里的泄漏」——
    比如有人把用户正文塞进了 `error_kind`。
    """
    for key, value in event.items():
        if not isinstance(value, str):
            continue
        if _JWT_SHAPE.search(value):
            raise ValueError("{0}: {1} 里出现了 JWT 形态".format(where, key))
        if _BEARER.search(value):
            raise ValueError("{0}: {1} 里出现了 Authorization 形态".format(where, key))
        if _PHONE.search(value):
            raise ValueError("{0}: {1} 里出现了手机号".format(where, key))
        if _EMAIL.search(value):
            raise ValueError("{0}: {1} 里出现了邮箱".format(where, key))
        # 长文本 = 大概率是把正文塞进来了。枚举值都很短。
        if len(value) > 120:
            raise ValueError(
                "{0}: {1} 长度 {2} —— 观测事件里不许出现正文".format(where, key, len(value)))


def build_turn_event(
    *,
    session_id: Optional[str] = None,
    request_id: Optional[str] = None,
    actor_kind: str = "unknown",
    current_route: str = "",
    route_valid: Optional[bool] = None,
    intent_kind: str = "unknown",
    deterministic_hit: bool = False,
    knowledge_manifest_version: Optional[str] = None,
    knowledge_status: str = "unknown",
    retrieval_state: str = "unknown",
    answer_state: str = "unknown",
    freshness_state: str = "unknown",
    exits: Iterable[Mapping[str, Any]] = (),
    action_kinds: Iterable[str] = (),
    repeat_fallback: bool = False,
    help_center_only: bool = False,
    handoff_submitted: bool = False,
    ticket_id: Optional[int] = None,
    resolution: str = "unknown",
    error_kind: str = "",
    adapter_state: str = "ok",
) -> dict[str, Any]:
    """组装一条 turn 事件。**只有白名单里的键**。

    注意签名里**没有** `message` / `answer` / `attachment_text` / `token` /
    `user_id` —— 它们连进来的入口都没有。这是刻意的:
    「忘了脱敏」这种错在这里连写都写不出来。
    """
    event: dict[str, Any] = {
        "event": EVENT_NAME,
        # 不可反推:盐化哈希,不是 session_id 原文,也不是 token 截断
        "turn_id": _salted(request_id or "", kind="turn"),
        "session_correlation_id": _salted(session_id or "", kind="session"),
        "actor_kind": actor_kind if actor_kind in ACTOR_KINDS else "unknown",
        # 只留**路由**(路由形状本身不是用户数据);查询串会带 id,一律砍掉
        "current_route": str(current_route or "").split("?", 1)[0][:120],
        "route_valid": route_valid,
        "intent_kind": intent_kind,
        "deterministic_hit": bool(deterministic_hit),
        "knowledge_manifest_version": knowledge_manifest_version,
        "knowledge_status": knowledge_status,
        "retrieval_state": retrieval_state,
        "answer_state": answer_state,
        "freshness_state": freshness_state,
        "exit_ids": sorted({str(e.get("exit_id")) for e in exits if e.get("exit_id")}),
        "action_kinds": sorted({str(k) for k in action_kinds if k}),
        "repeat_fallback": bool(repeat_fallback),
        "help_center_only": bool(help_center_only),
        "handoff_submitted": bool(handoff_submitted),
        "ticket_id": int(ticket_id) if ticket_id else None,
        "resolution": resolution,
        "error_kind": str(error_kind or "")[:64],
        "adapter_state": adapter_state,
    }
    extra = set(event) - ALLOWED_KEYS
    if extra:
        raise ValueError("观测事件里出现了白名单外的键:{0}".format(sorted(extra)))
    assert_no_sensitive_payload(event, where="build_turn_event")
    return event


def emit(event: Mapping[str, Any]) -> None:
    """把事件写进应用日志。

    🔴 观测**永远不许打断问答**:写日志失败就算了,不往上抛。
    """
    try:
        assert_no_sensitive_payload(event, where="emit")
        _LOGGER.info("%s", dict(event))
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("[xiaobang] 观测事件丢弃(不影响问答): %s", type(exc).__name__)
