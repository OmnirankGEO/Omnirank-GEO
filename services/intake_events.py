"""
intake_events — 静默事件投递 (CTO-E 2026-04-26)

设计原则:
  - C 的 m3_customer_events 表存在 → 写入
  - 不存在 / 写入失败 → 完全静默, 不阻塞主功能, 不假装写入
  - 调用方拿不到失败信号 (本期不需要), 仅 log warning

事件类型 (本期 4 种):
  - intake_opened       客户首次 GET /api/public/intake/{token}
  - intake_submitted    客户 POST submit
  - intake_reviewed     代理 approve/reject
  - intake_expired      sweep job 标 expired (本期暂未挂 scheduler, 函数预留)

字段 (对齐 CTO-C HANDOFF Section 4.7):
  brand_id / quote_id / diagnosis_id / token_hash / source / event_type / event_key /
  metadata / ip_hash / user_agent_hash / occurred_at / created_at
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("GEO-Intake-Events")

EVENT_INTAKE_OPENED = "intake_opened"
EVENT_INTAKE_SUBMITTED = "intake_submitted"
EVENT_INTAKE_REVIEWED = "intake_reviewed"
EVENT_INTAKE_EXPIRED = "intake_expired"

_TABLE_EXISTS_CACHE: Optional[bool] = None


def _check_table_exists() -> bool:
    """检测 m3_customer_events 表是否存在. 缓存结果避免每次查 information_schema."""
    global _TABLE_EXISTS_CACHE
    if _TABLE_EXISTS_CACHE is not None:
        return _TABLE_EXISTS_CACHE

    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'm3_customer_events'
                LIMIT 1
            """)
            _TABLE_EXISTS_CACHE = cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"[Intake-Events] table check failed silently: {e}")
        _TABLE_EXISTS_CACHE = False

    return _TABLE_EXISTS_CACHE


def reset_table_cache() -> None:
    """测试用 / 表后续被创建时手动 reset."""
    global _TABLE_EXISTS_CACHE
    _TABLE_EXISTS_CACHE = None


def _hash(value: Optional[str], salt: str = "") -> Optional[str]:
    if not value:
        return None
    h = hashlib.sha256()
    h.update(((salt or "") + str(value)).encode("utf-8"))
    return h.hexdigest()[:32]


def _daily_salt() -> str:
    """日级 salt (服务端 secret + UTC 日期)。

    🔴 [WO_246 2026-09-19] 原来最后有一个**写死的兜底盐**。
       盐写在源码里 = 盐是公开的 = 哈希**可被离线穷举反推**
       (这里哈希的是手机号/邮箱这类取值空间很小的东西,
        知道盐之后枚举一遍就还原了)。
       "有个兜底所以不会崩" 在这里换来的是**匿名化失效而无人知道** ——
       和本轮一直在治的那类 fallback 同一个病:**它工作得很正常**。

    ⇒ 取不到就 **raise**,让它在启动期/首次调用就红,而不是静默用一个人人可得的盐。
       ⚠️ 我第一版在这里写「调用方本来就把整段包在 try 里」—— **那是错的**,
       `salt = _daily_salt()` 原本排在 `_safe_emit` 的 `try` **之前**,
       抛出来会**外溢到主流程**,而 `_safe_emit` 的契约白纸黑字是「永不抛,静默失败」。
       已把取盐挪进它的 try:抛异常的后果是**这条埋点不写**,不是接口挂掉。
       (差点把一条注释写成事实 —— 写完当场 grep 了一遍才发现。)
    """
    base = (os.getenv("INTAKE_EVENT_SALT") or os.getenv("SECRET_KEY") or "").strip()
    if not base:
        raise RuntimeError(
            "INTAKE_EVENT_SALT / SECRET_KEY 都未配置 —— 匿名化盐不能用写死的默认值"
        )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{base}:{today}"


def _event_key_or_none(raw: str) -> Optional[str]:
    """算 event_key;**盐取不到就返回 None**,绝不把异常抛给业务路径。

    🔴 [WO_246] 工单点名的是 `_daily_salt` 那一行,但**能抛到业务路径的调用点有五处**:
       `_safe_emit` 一处 + 四个公开 helper(`emit_opened` / `emit_submitted` /
       `emit_reviewed` / `emit_expired`)各自**在 try 之外**直接调它。
       只改我先看到的那一处 = 只修了我点名的那一处,剩下四条路照样把异常外溢
       ——「工单点的实例不是缺陷的类」,这次类的边界是**这个函数的全部调用点**。

    盐缺失时 key 为 None ⇒ 去重退化(表上有 `ON CONFLICT DO NOTHING`),
    而随后的 `_safe_emit` 自己也会在 try 内因同样原因失败 ⇒ 这条埋点不写。
    **不写**是对的:没有盐就没有匿名化,宁可不记。
    """
    try:
        return _hash(raw, _daily_salt())
    except Exception as exc:                 # noqa: BLE001
        logger.debug("[Intake-Events] salt unavailable, skip event_key: %s",
                     type(exc).__name__)
        return None


def _safe_emit(
    event_type: str,
    *,
    brand_id: Optional[int] = None,
    quote_id: Optional[int] = None,
    diagnosis_id: Optional[int] = None,
    token: Optional[str] = None,
    event_key: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> bool:
    """单条事件写入. 永不抛, 静默失败. 返 True 表示写入成功."""
    if not _check_table_exists():
        return False

    try:
        # [WO_246] 取盐挪进 try:盐缺失时这条埋点不写,而**不破坏本函数
        #   「永不抛、静默失败」的契约**(见上面 docstring)。
        salt = _daily_salt()
        token_hash = _hash(token, salt) if token else None
        ip_hash = _hash(ip, salt) if ip else None
        ua_hash = _hash(user_agent, salt) if user_agent else None

        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            # 用 ON CONFLICT (event_key) DO NOTHING 做去重 (如果 C 给 event_key 加唯一索引)
            cur.execute("""
                INSERT INTO m3_customer_events
                  (brand_id, quote_id, diagnosis_id, token_hash, source, event_type, event_key,
                   metadata, ip_hash, user_agent_hash, occurred_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT DO NOTHING
            """, (
                brand_id, quote_id, diagnosis_id, token_hash,
                "intake", event_type, event_key,
                json.dumps(metadata or {}, ensure_ascii=False),
                ip_hash, ua_hash,
            ))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"[Intake-Events] silent emit fail ({event_type}): {type(e).__name__}")
        return False


# ==================== 公开 helper ====================

def emit_opened(
    *,
    token: str,
    brand_id: Optional[int],
    diagnosis_id: Optional[int] = None,
    quote_id: Optional[int] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    # event_key 用 (token, opened, day) 60s 内同 token 重复 GET 不重复记 opened
    # 这里简单 token-level dedupe; 真严格 60s 由 C 的 sendBeacon 客户端控制
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = _event_key_or_none(f"{token}:opened:{day}")
    _safe_emit(
        EVENT_INTAKE_OPENED,
        brand_id=brand_id, diagnosis_id=diagnosis_id, quote_id=quote_id,
        token=token, event_key=key,
        metadata={"channel": "intake_link"},
        ip=ip, user_agent=user_agent,
    )


def emit_submitted(
    *,
    token: str,
    brand_id: Optional[int],
    submission_id: int,
    diagnosis_id: Optional[int] = None,
    quote_id: Optional[int] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    key = _event_key_or_none(f"{token}:submitted:{submission_id}")
    _safe_emit(
        EVENT_INTAKE_SUBMITTED,
        brand_id=brand_id, diagnosis_id=diagnosis_id, quote_id=quote_id,
        token=token, event_key=key,
        metadata={"submission_id": submission_id},
        ip=ip, user_agent=user_agent,
    )


def emit_reviewed(
    *,
    submission_id: int,
    brand_id: Optional[int],
    review_status: str,
    applied_count: int,
    completeness_after: int,
    reviewer_id: int,
) -> None:
    key = _event_key_or_none(f"sub:{submission_id}:reviewed:{review_status}")
    _safe_emit(
        EVENT_INTAKE_REVIEWED,
        brand_id=brand_id,
        event_key=key,
        metadata={
            "submission_id": submission_id,
            "review_status": review_status,
            "applied_count": applied_count,
            "completeness_after": completeness_after,
            "reviewer_id": reviewer_id,
        },
    )


def emit_expired(*, token: str, brand_id: Optional[int]) -> None:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = _event_key_or_none(f"{token}:expired:{day}")
    _safe_emit(
        EVENT_INTAKE_EXPIRED,
        brand_id=brand_id, token=token, event_key=key,
        metadata={},
    )
