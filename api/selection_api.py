"""
选词报价互动页面 API
- 客户端（/api/s/）：无需登录，通过 token 访问
- 销售端（/api/keyword-selection/）：需登录鉴权
"""

import json
import math
import secrets
import logging
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from db.diagnosis_db import (
    get_connection,
    get_quote,
    get_session_by_token,
    get_session_by_quote,
    create_selection_session,
    update_session,
    list_selection_sessions,
    save_interaction_events,
    get_interaction_logs,
    get_cached_keyword_prices,
    save_keyword_prices_cache,
    get_keywords_by_quote,
)
from auth.audit import audit
from auth.jwt_utils import decode_jwt
from services.selected_keyword_resolver import resolve_selected_keyword_texts
from services.commercial_query_policy import POLICY_VERSION as _COMMERCIAL_POLICY_VERSION
from services.quote_pricing_preferences import _resolve_fallback_unit_cost  # [P1 兜底成本地板] override>缓存真实成本>60
# [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 报价单只承诺真锁住的价 · 唯一实现在该模块
from services.price_lock_promise import (
    LOCK_MAP_KEY as _LOCK_MAP_KEY,
    lock_fields as _lock_fields,
    record_locked as _record_locked,
)
from services.notification_events import NotificationEventType
from services.notification_outbox import (
    brand_owner_notification_event_exists_durable,
    enqueue_brand_owner_notification_event,
    enqueue_brand_owner_notification_event_durable,
)

logger = logging.getLogger("GEO-Selection")

router = APIRouter(prefix="/api", tags=["选词报价"])

import asyncio
import os
# [并发-3 2026-06-10 · FABLE复审后改默认5灰度] 默认5=旧行为(fail-safe)。env 调 100 是部署最后一步:
# 必须先于此的 GEO #12 markpaid 补 FOR UPDATE(无FOR UPDATE时100并发同token双击→词翻倍)+ 资金批落地稳定。
# 报价瓶颈在 5118/metaso 冷词,放开后由数据源池/缓存承接。QUOTE_MAX_CONCURRENCY=100 + force-recreate 调高。
_quote_gen_semaphore = asyncio.Semaphore(int(os.getenv("QUOTE_MAX_CONCURRENCY", "5")))


def _durable_selection_notice(
    *, brand_id: int, quote_id: int, event_type: NotificationEventType,
    terminal_state: str, status: str, summary: str = "", amount: str | None = None,
) -> None:
    facts = {
        "business_no": f"QUOTE-{int(quote_id)}",
        "status": status,
        "occurred_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
    }
    if amount is not None:
        facts["amount"] = amount
    enqueue_brand_owner_notification_event_durable(
        brand_id=int(brand_id),
        event_type=event_type,
        business_id=f"quote:{int(quote_id)}",
        terminal_state=terminal_state,
        facts=facts,
    )


# ══════════════════════════════════════════════════════════════════════════
# [#178 P0 · 2026-09-12] 客户选中的问法全部不可交付 —— 不再是无出口的 400
# ══════════════════════════════════════════════════════════════════════════
#: 这条通知的 terminal_state。outbox 幂等键 = event_type:business_id:terminal_state:user_id,
#: business_id 固定 "quote:<id>" ⇒ **同一张报价单只推一条**,客户反复提交不会刷屏。
_NO_DELIVERABLE_TERMINAL_STATE = "selection_no_deliverable"

#: batch_pricing 在拿不到真品牌时用的占位名(`_lock_brand_industry` 也是这么判的)。
#: 绝不能把它当品牌名喂给策略引擎:引擎判 brand_direct 的条件是"品牌名是回答的子串",
#: 传 "客户" 会把任何含"客户"二字的知识词放行成可交付 —— 那是反方向的缺陷。
_PLACEHOLDER_BRAND_NAMES = frozenset({"客户", "品牌", ""})


def _row_field(row, key: str, index: int = 0):
    if isinstance(row, dict):
        return row.get(key)
    return row[index] if row else None


def _session_brand_name(conn, session: dict) -> str:
    """取本会话的品牌名,喂给唯一商业意图引擎(客户侧)。

    🔴 **不要用 `session.get("brand_name")`**:`keyword_selection_sessions`
       根本没有这一列(prod_schema_2026-09-05 逐列核过),`.get` 只会安静地返回 None。
       仓里已有一处这么写(approve_quote 的 audit after 块),它落的是 null ——
       没人发现,因为读不出错。
    来源顺序与服务商侧一致(见 :2915 与 tools/batch_pricing):
       quotes.brand_name → brands.name。

    整段包 SAVEPOINT:调用方此时正握着 `SELECT … FOR UPDATE` 的事务,
    这里任何一条 SQL 抛错都会把**业务事务**打进 InFailedSqlTransaction
    (`_get_brand_name` 的 docstring 记的就是这个事故)。取不到品牌名只该退化成
    "不传品牌名",不该让客户提交失败。
    """
    cursor = conn.cursor()
    name = ""
    try:
        cursor.execute("SAVEPOINT sp_178_brand_name")
    except Exception:
        return ""
    try:
        quote_id = session.get("quote_id")
        if quote_id is not None:
            cursor.execute("SELECT brand_name FROM quotes WHERE id = %s", (int(quote_id),))
            name = str(_row_field(cursor.fetchone(), "brand_name") or "").strip()
        if name in _PLACEHOLDER_BRAND_NAMES:
            brand_id = session.get("brand_id")
            if brand_id is not None:
                cursor.execute("SELECT name FROM brands WHERE id = %s", (int(brand_id),))
                name = str(_row_field(cursor.fetchone(), "name") or "").strip()
        cursor.execute("RELEASE SAVEPOINT sp_178_brand_name")
    except Exception as exc:
        try:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_178_brand_name")
        except Exception:
            pass
        logger.warning("[#178] 取品牌名失败,本次按不传品牌名判定: %s", exc)
        return ""
    return "" if name in _PLACEHOLDER_BRAND_NAMES else name


def _push_no_deliverable_notice(*, brand_id: int, quote_id: int, summary: str) -> bool:
    """推「客户暂无可交付问法」给品牌 owner。返回 **notify_sent**。

    🔴 notify_sent 的语义是「报价方已经收到过这条通知」,**不是**「本次插了一行」。
       enqueue 走 `ON CONFLICT (event_key) DO NOTHING`,重复推时返回 None ——
       把 None 读成"没送达"会让前端在报价方**早已被通知**的情况下继续显示
       「去通知报价方」,按一次还是 None,按钮永远点不"亮"。
    """
    try:
        enqueue_brand_owner_notification_event_durable(
            brand_id=int(brand_id),
            event_type=NotificationEventType.SELECTION_NO_DELIVERABLE_KEYWORDS,
            business_id=f"quote:{int(quote_id)}",
            terminal_state=_NO_DELIVERABLE_TERMINAL_STATE,
            facts={
                "business_no": f"QUOTE-{int(quote_id)}",
                "status": "客户暂无可交付问法",
                "occurred_at": datetime.now().isoformat(timespec="seconds"),
                "summary": summary,
            },
        )
        return True
    except Exception as exc:
        # 不阻断客户:出口卡片照给,按钮留着让客户补发。
        logger.exception("[#178] 暂无可交付问法通知写入 outbox 失败: %s", exc)
        return False


def _no_deliverable_notified(*, brand_id, quote_id) -> bool:
    """只读:这条通知在不在 outbox 里(GET 读面 / 重放分支用,绝不写)。"""
    try:
        return brand_owner_notification_event_exists_durable(
            brand_id=int(brand_id),
            event_type=NotificationEventType.SELECTION_NO_DELIVERABLE_KEYWORDS,
            business_id=f"quote:{int(quote_id)}",
            terminal_state=_NO_DELIVERABLE_TERMINAL_STATE,
        )
    except Exception as exc:
        logger.warning("[#178] 查通知状态失败,按未通知呈现: %s", exc)
        return False


def _no_deliverable_view(session: dict, keywords: list, considered_ids) -> dict:
    """从**已落库的快照**重建"零可交付"读面;不适用时返回 {}。

    为什么读面必须自己能重建(A 在 #178 契约回执里指出的缺口):
      all_excluded / 逐条原因原本只在 POST 响应里。客户看完卡片一刷新、或从微信里
      重新打开链接,前端走的是 GET —— 字段不在,卡片和原因全没了,而
      `business_lines_submitted + selected_keyword_ids=[]` 这个组合在现有前端会渲染成
      「✅ 业务方向已提交!我们正在为您准备专属关键词方案,请稍候…」。
      那是本单要修的同一个死胡同,换了个**更有欺骗性**的说法:没有人在准备方案。

    🔴 不新增列、不另存一份布尔:排除结论在提交时已逐条盖进 keywords_snapshot 的
       `delivery_exclusion`(见 _partition_delivery_exclusions),这里只是把它读回来。
       另存一份状态 = 第二个真相源,快照改了它不会改。
    """
    # 两个允许态都要认:
    #   business_lines_submitted —— 业务线步全排除,会话停在这里等报价方补词;
    #   selecting                —— submit-keywords 零可交付**不改状态**(见那条分支),
    #                               但戳已落快照。不认它,客户点「让报价方补充」会被
    #                               状态守卫 400:第一次不阻断、第二次却挡死。
    if str(session.get("status")) not in ("selecting", "business_lines_submitted"):
        return {}
    if _safe_json(session.get("selected_keyword_ids"), []) or []:
        return {}
    excluded: list[dict] = []
    for item in keywords or []:
        if not isinstance(item, dict):
            continue
        stamp = item.get("delivery_exclusion")
        if not isinstance(stamp, dict):
            continue
        try:
            keyword_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if considered_ids is not None and keyword_id not in considered_ids:
            # 只报**本次选中方向下**被排除的词:别的方向上一次提交留下的戳不算数。
            continue
        excluded.append({
            "id": keyword_id,
            "keyword": item.get("keyword", ""),
            "reason": stamp.get("reason", ""),
            "kind": stamp.get("decision", ""),
            "policy_version": stamp.get("policy_version", ""),
        })
    if not excluded:
        # [#178-B] 空集合有两种意思,必须分开:
        #   · considered_ids 是**空集合** ⇒ 客户选了方向、而该方向下一条候选词都没有
        #     ⇒ 真的零可交付,reason=no_keywords_for_lines(逐条清单本来就是空的);
        #   · 其它(考虑集不可知 / 非空但没有戳)⇒ 什么都别说。
        #     否则"报价方还没生成词"之类的空态会被贴成"你的词全被排除了" ——
        #     具体但错的报文比含糊的更糟。
        if considered_ids is not None and len(considered_ids) == 0:
            return {
                "all_excluded": True,
                "reason": "no_keywords_for_lines",
                "delivery_excluded_keywords": [],
                "next_action": _no_deliverable_next_action(
                    _no_deliverable_notified(
                        brand_id=session.get("brand_id"), quote_id=session.get("quote_id"),
                    )
                ),
            }
        return {}
    return {
        "all_excluded": True,
        "reason": "all_excluded",
        "delivery_excluded_keywords": excluded,
        "next_action": _no_deliverable_next_action(
            _no_deliverable_notified(
                brand_id=session.get("brand_id"), quote_id=session.get("quote_id"),
            )
        ),
    }


def _no_deliverable_next_action(notify_sent: bool) -> dict:
    """零可交付时给前端的**出口**契约(A 侧按此渲染两条出口)。"""
    return {"kind": "add_commercial_keywords", "notify_sent": bool(notify_sent)}


def _safe_json(val, default=None):
    """安全解析 JSON — jsonb 字段已自动反序列化为 list/dict，不需要再 json.loads"""
    if val is None:
        return default
    if isinstance(val, (list, dict, set)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return default


class KeywordSelectionContractError(ValueError):
    """Stable fail-closed error for ambiguous legacy selection snapshots."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _selection_contract_http_error(
    error: KeywordSelectionContractError,
) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": error.code,
            "message": error.message,
            "retryable": False,
            "recovery": "regenerate_keyword_selection_link",
        },
    )


def _normalize_business_lines_for_session(
    business_lines: list[dict],
    *,
    strict: bool = False,
) -> tuple[list[dict], dict[int, int]]:
    """兼容旧 session: 展示/提交前合并地域长尾业务线,并保留 old_id -> new_id 映射。"""
    if not business_lines:
        return business_lines, {}
    try:
        from tools.keyword_cluster import _normalize_business_lines_with_id_map
        return _normalize_business_lines_with_id_map(business_lines)
    except Exception as e:
        if strict:
            raise KeywordSelectionContractError(
                "BUSINESS_LINE_IDENTITY_AMBIGUOUS",
                "历史业务方向标识存在重复或双义，请让报价方重新生成选词链接。",
            ) from e
        logger.warning(f"业务线兼容归并失败(保持原数据): {e}")
        return business_lines, {}


def _normalize_unique_keyword_snapshot_ids(keywords: list[dict]) -> list[dict]:
    """Canonicalize numeric IDs and reject snapshots that can misprice by ID."""
    normalized: list[dict] = []
    seen: set[int] = set()
    for index, keyword in enumerate(keywords):
        if not isinstance(keyword, dict):
            raise KeywordSelectionContractError(
                "KEYWORD_SNAPSHOT_INVALID",
                f"第 {index + 1} 个关键词快照格式无效，请重新生成选词链接。",
            )
        raw_id = keyword.get("id")
        if isinstance(raw_id, bool):
            keyword_id = 0
        else:
            try:
                keyword_id = int(raw_id)
            except (TypeError, ValueError):
                keyword_id = 0
        if keyword_id <= 0:
            raise KeywordSelectionContractError(
                "KEYWORD_ID_INVALID",
                "历史关键词缺少有效标识，请重新生成选词链接。",
            )
        if keyword_id in seen:
            raise KeywordSelectionContractError(
                "KEYWORD_ID_DUPLICATED",
                "历史关键词标识重复，继续报价可能造成数量或金额错误，请重新生成选词链接。",
            )
        seen.add(keyword_id)
        item = dict(keyword)
        item["id"] = keyword_id
        normalized.append(item)
    return normalized


def _normalize_requested_selection_ids(
    raw_ids: list[int],
    *,
    allowed_ids: set[int],
    invalid_code: str,
    unknown_code: str,
    label: str,
) -> list[int]:
    """Reject ambiguous request IDs before they can affect scope or pricing."""
    normalized: list[int] = []
    seen: set[int] = set()
    for raw_id in raw_ids:
        if isinstance(raw_id, bool):
            item_id = 0
        else:
            try:
                item_id = int(raw_id)
            except (TypeError, ValueError):
                item_id = 0
        if item_id <= 0 or item_id in seen:
            raise KeywordSelectionContractError(
                invalid_code,
                f"{label}包含无效或重复标识，请刷新后重新选择。",
            )
        if item_id not in allowed_ids:
            raise KeywordSelectionContractError(
                unknown_code,
                f"{label}包含当前页面不存在的项目，请刷新后重新选择。",
            )
        seen.add(item_id)
        normalized.append(item_id)
    return normalized


def _load_selection_session_for_update(conn, token: str) -> Optional[dict]:
    """Load the authoritative session under a row lock for one-shot submission."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM keyword_selection_sessions WHERE token = %s FOR UPDATE",
        (token,),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def _check_expired_locked(conn, session: dict) -> dict:
    """Expire a locked selection session without checking out a second connection."""
    if session["status"] in (
        "confirmed",
        "expired",
        "pending_payment",
        "active",
        "payment_overdue",
        "pricing_pending_review",
        "business_lines_submitted",
    ):
        return session
    expires_at = session.get("expires_at")
    if not expires_at:
        return session
    try:
        expired = datetime.now() > datetime.fromisoformat(expires_at)
    except (ValueError, TypeError):
        return session
    if expired:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE keyword_selection_sessions
               SET status = 'expired', updated_at = %s
             WHERE token = %s
            """,
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session["token"]),
        )
        session["status"] = "expired"
    return session


def _selected_business_line_ids(business_lines: list[dict]) -> list[int]:
    return sorted(
        int(line["id"])
        for line in business_lines
        if isinstance(line, dict)
        and line.get("is_selected")
        and line.get("id") is not None
    )


def _select_keyword_ids_for_business_lines(
    keywords: list[dict],
    selected_business_line_ids: set[int],
    business_line_id_map: dict[int, int] | None = None,
    all_business_line_ids: set[int] | None = None,
) -> list[int]:
    """Resolve the exact session keyword IDs covered by explicit customer choices.

    Business-line selection is a human decision. A quality classifier may add a
    review hint, but it must never silently remove keywords after the customer
    selected their business lines.
    """
    id_map = business_line_id_map or {}
    normalized_keywords = _normalize_unique_keyword_snapshot_ids(keywords)
    matched: list[int] = []
    unassigned: list[int] = []
    assigned: list[int] = []
    seen: set[int] = set()
    for keyword in normalized_keywords:
        keyword_id = keyword["id"]
        if keyword_id in seen:
            continue
        business_line_id = keyword.get("business_line_id")
        if business_line_id is None:
            unassigned.append(keyword_id)
            seen.add(keyword_id)
            continue
        try:
            normalized_id = id_map.get(int(business_line_id), int(business_line_id))
        except (TypeError, ValueError):
            raise KeywordSelectionContractError(
                "KEYWORD_BUSINESS_LINE_ID_INVALID",
                "历史关键词关联了无效业务方向，请重新生成选词链接。",
            )
        if all_business_line_ids is not None and normalized_id not in all_business_line_ids:
            raise KeywordSelectionContractError(
                "KEYWORD_BUSINESS_LINE_ORPHANED",
                "历史关键词关联的业务方向已不存在，请重新生成选词链接。",
            )
        assigned.append(keyword_id)
        if normalized_id in selected_business_line_ids:
            matched.append(keyword_id)
        seen.add(keyword_id)

    if assigned and unassigned:
        available_ids = all_business_line_ids or set()
        if available_ids and selected_business_line_ids.issuperset(available_ids):
            return [keyword["id"] for keyword in normalized_keywords]
        raise KeywordSelectionContractError(
            "KEYWORD_BUSINESS_LINE_LINEAGE_INCOMPLETE",
            "历史关键词只有部分业务方向归属。为避免少词或错选，请全选业务方向或让报价方重新生成链接。",
        )
    if not assigned:
        return [keyword["id"] for keyword in normalized_keywords]
    return matched


def _same_review_event(left: dict, right: dict) -> bool:
    compared_fields = (
        "policy_version",
        "decision",
        "actor_kind",
        "actor_id",
        "reason",
        "original_issue",
    )
    return all(left.get(field) == right.get(field) for field in compared_fields)


def _record_keyword_review_overrides(
    keywords: list[dict],
    selected_keyword_ids: list[int],
    *,
    actor_kind: str,
    actor_id: int | str | None,
    reason: str,
    reviewed_at: str,
    brand_name: str = "",
) -> tuple[list[dict], int]:
    """Freeze human overrides into the existing keyword JSON snapshot."""
    # [SSOT §3.2 · Review-CTO 2026-07-23 P1] 用唯一引擎判是否交付资格,
    # 不再用独立分类器:知识/裸词/待澄清词由 _partition_delivery_exclusions
    # 排除,不得通过 human_continue 回到付费交付。
    from services.commercial_query_policy import evaluate as _policy_evaluate
    selected_ids = {int(keyword_id) for keyword_id in selected_keyword_ids}
    updated: list[dict] = []
    override_count = 0
    for keyword in keywords:
        if not isinstance(keyword, dict):
            continue
        item = dict(keyword)
        try:
            keyword_id = int(item.get("id"))
        except (TypeError, ValueError):
            updated.append(item)
            continue
        _decision = _policy_evaluate(
            str(item.get("keyword") or ""),
            intent_hint=item.get("intent"),
            # [#178] 与 _partition_delivery_exclusions 同一份输入。两处对**同一个词**
            #   算 `commercial_delivery_eligible`:分区那边传了品牌名、这边不传,
            #   品牌直问就会在这里被算成 knowledge_class ⇒ needs_review 恒 False ⇒
            #   该给的复核提示一条都不给。同一谓词两处,必须同源。
            brand_name=brand_name or None,
        )
        knowledge_class = (
            not _decision.commercial_delivery_eligible
            or item.get("commercial_delivery_eligible") is False
        )
        # 合格商业词的范围/默认选择争议仍走 advisory + 人工确认继续(§2.2)。
        needs_review = not knowledge_class and (
            item.get("scope_match") is False
            or item.get("default_selected") is False
            or item.get("recommended") is False
        )
        if keyword_id in selected_ids and needs_review:
            event = {
                "policy_version": "buyer-intent-review-v1",
                "decision": "human_continue",
                "actor_kind": actor_kind,
                "actor_id": actor_id,
                "reason": reason,
                "original_issue": (
                    item.get("rejection_reason")
                    or "系统建议复核该关键词的品牌推荐意图"
                ),
                "reviewed_at": reviewed_at,
            }
            current = item.get("review_advisory")
            history = [
                dict(existing)
                for existing in (item.get("review_advisory_history") or [])
                if isinstance(existing, dict)
            ]
            if isinstance(current, dict) and not any(
                _same_review_event(current, existing) for existing in history
            ):
                history.append(dict(current))
            if not any(_same_review_event(event, existing) for existing in history):
                history.append(dict(event))
                override_count += 1
            if not isinstance(current, dict) or not _same_review_event(current, event):
                item["review_advisory"] = event
            item["review_advisory_history"] = history
        updated.append(item)
    return updated, override_count


def _partition_delivery_exclusions(
    keywords: list[dict],
    selected_keyword_ids: list[int],
    *,
    reviewed_at: str,
    brand_name: str = "",
) -> tuple[list[dict], list[dict]]:
    """[SSOT geo-commercial-intent-governance-v1.0 §3.2/§3.3 · Review-CTO
    2026-07-23 P1] 商业交付资格分区 —— **只用唯一引擎** CommercialQueryPolicy.

    每一条被选中的候选都调 `evaluate()`,三分完全依据同一返回值:
      - 商业词(含品牌直问):保留,可计价;
      - 知识词 / SEO 裸词:排除,**无人工改选入口**(delivery_exclusion.decision
        = knowledge_term_not_deliverable);
      - 待澄清词(uncertain):排除,但归「需澄清区」(decision =
        needs_clarification),澄清后可重提。
    **绝不静默删减**:每条排除都盖章 + 逐条返回原因。
    禁止在本阶段再用任何独立分类器(旧 _is_informational_keyword 已弃用)。
    """
    from services.commercial_query_policy import (
        INTENT_UNCERTAIN,
        evaluate as _policy_evaluate,
        hard_block_reason as _hard_block_reason,
    )

    selected = {int(keyword_id) for keyword_id in selected_keyword_ids}
    exclusions: list[dict] = []
    updated: list[dict] = []
    for keyword in keywords:
        if not isinstance(keyword, dict):
            continue
        item = dict(keyword)
        try:
            keyword_id = int(item.get("id"))
        except (TypeError, ValueError):
            updated.append(item)
            continue
        if keyword_id in selected:
            decision = _policy_evaluate(
                str(item.get("keyword") or ""),
                intent_hint=item.get("intent"),
                # [#178 P0 · 2026-09-12] 客户侧必须和服务商侧喂同一份输入。
                #   服务商报价链走 quote_intent_gate.partition_by_commercial_policy
                #   (tools/batch_pricing.py:679)传了 brand_name;这里没传 ⇒ 同一个词
                #   「<品牌>怎么样 / 靠谱吗」在报价台可交付、在客户页被判知识/待澄清而排除。
                #   同一引擎两套输入 = 两套结论,而客户看到的是被拒的那套。
                brand_name=brand_name or None,
            )
            # [报价纠偏工单 2026-07-26 P0-4 · 裁决 D8 放行权归用户]
            #   纯 regex 文本引擎不是可以物理禁用户的理由(实证会把"…哪里正规"
            #   这类真问法判成知识词)。操作员在候选词页面**显式放行**并留下
            #   `review_override_reason` 的词 → 进付费交付,盖人工放行审计章。
            #   四条硬边界(法律/资金/越权/数据完整性)仍然物理禁,人工也放不行。
            override_reason = str(item.get("review_override_reason") or "").strip()
            hard_code = _hard_block_reason(str(item.get("keyword") or ""))
            if (
                not decision.commercial_delivery_eligible
                and override_reason
                and not hard_code
            ):
                item["commercial_delivery_eligible"] = True
                item["intent_type"] = decision.intent_type
                item["policy_version"] = decision.policy_version
                item.pop("delivery_exclusion", None)
                item["human_release"] = {
                    "policy_version": "buyer-intent-review-v1",
                    "decision": "human_release_into_delivery",
                    "engine_intent_type": decision.intent_type,
                    "engine_reason_codes": list(decision.reason_codes),
                    "reason": override_reason,
                    "released_at": reviewed_at,
                }
                updated.append(item)
                continue
            if not decision.commercial_delivery_eligible:
                if decision.intent_type == INTENT_UNCERTAIN:
                    decision_kind = "needs_clarification"
                    reason = (
                        "暂无法确认该问法是否会促使 AI 推荐具体品牌/服务商，"
                        "已放入需澄清区；改写成明确的选型/推荐/价格问法后可重新提交"
                    )
                else:
                    decision_kind = "knowledge_term_not_deliverable"
                    reason = (
                        "该问法通常不会促使 AI 推荐具体品牌、服务商、产品或方案，"
                        "不具备付费交付价值"
                    )
                item["commercial_delivery_eligible"] = False
                item["policy_version"] = decision.policy_version
                item["intent_type"] = decision.intent_type
                item["delivery_exclusion"] = {
                    "policy_version": decision.policy_version,
                    "decision": decision_kind,
                    "reason": reason,
                    "reason_codes": list(decision.reason_codes),
                    "excluded_at": reviewed_at,
                    # [P0-4] 除四条硬边界外都留人工放行出口(操作员填理由后重新提交)
                    "hard_block": bool(hard_code),
                    "hard_block_reason": hard_code or "",
                    "human_override_allowed": not hard_code,
                }
                exclusions.append(
                    {
                        "id": keyword_id,
                        "keyword": item.get("keyword", ""),
                        "reason": reason,
                        "kind": decision_kind,
                        "policy_version": decision.policy_version,
                    }
                )
            else:
                # 唯一引擎判为商业:清掉可能残留的旧排除标记,保证可计价。
                item["commercial_delivery_eligible"] = True
                item["intent_type"] = decision.intent_type
                item["policy_version"] = decision.policy_version
                item.pop("delivery_exclusion", None)
        updated.append(item)
    return updated, exclusions


# [CTO-15.23 2026-05-12 BUG fix · 严查报价板块]
# 老板报:"深圳GEO优化和传统SEO哪个好" 这种纯知识对比题被当 consideration 报价(¥981/¥1745)
# 实证:keywords_snapshot intent 字段大量为空 · 只有 category_label 中文标签可判
# 这词 category_label='对比' 被 LLM 错判 → 进报价单 · 客户付费但 0 转化
def _is_informational_keyword(kw: dict) -> tuple[bool, str]:
    """[Review-CTO 2026-07-23 P1] 判定关键词是否**无商业交付价值** ·
    返回 (is_info, reason)。

    已删除独立的 _INFO_PATTERNS / _COMMERCIAL_OVERRIDE_PATTERNS 决策(它们
    把"哪个好/哪个更/和"等商业比较词误排)。现薄委托唯一引擎
    CommercialQueryPolicy:知识词、SEO 裸词、待澄清词均视为无付费交付价值。
    仅用于业务线示例词展示等次要过滤;付费分区一律以 _partition_delivery_
    exclusions(同一引擎)为准。
    """
    if not isinstance(kw, dict):
        return False, ""
    text = kw.get("keyword", "") or ""
    if not text:
        return False, ""
    from services.commercial_query_policy import evaluate as _policy_evaluate
    decision = _policy_evaluate(str(text), intent_hint=kw.get("intent"))
    if decision.commercial_delivery_eligible:
        return False, ""
    return True, f"{decision.intent_type}:{','.join(decision.reason_codes)}"


def _delivery_eligible_field(
    keyword_text,
    *,
    intent_hint=None,
    existing=None,
) -> bool:
    """[SSOT geo-commercial-intent-governance-v1.0 · DRIFT-D] 报价 DTO 的
    ``commercial_delivery_eligible`` 字段单点来源 —— 唯一引擎 CommercialQueryPolicy。

    前端展示守卫(OnlineQuoteFlow ``isRedFlag`` §17.1)在此字段 ``=== true`` 时
    绝不与统一引擎矛盾地把合法商业词误标红。本函数把该判定收敛到唯一引擎:

    - 若上游已显式盖章 ``existing``(如 ``_partition_delivery_exclusions`` 已排除
      的知识词标 False,或快照透传的 True/False),尊重既有终值,不二次翻转;
    - 否则以唯一引擎 ``evaluate()`` 的 ``commercial_delivery_eligible`` 为准。

    纯读字段,只做展示资格分区,**绝不改动任何价格数值**(§9.6);也不是硬阻断
    (知识词照旧在物理隔离区展示,由既有排除逻辑负责,本字段仅供前端渲染)。
    """
    if existing is not None:
        return bool(existing)
    from services.commercial_query_policy import evaluate as _policy_evaluate
    return _policy_evaluate(
        str(keyword_text or ""), intent_hint=intent_hint
    ).commercial_delivery_eligible


# [报价意图闸 2026-08-04 · WO-QUOTE-INTENT-GATE] 判定/展示逻辑单点实现在
#   services/quote_intent_gate.py(与定价引擎 tools/batch_pricing.py 共用同一份)。
#   此处只做薄委托,不复制逻辑 —— 复制就会漂移。
from services.quote_intent_gate import (  # noqa: E402
    attach_policy_exclusions as _attach_policy_exclusions,
    nothing_quotable_error as _nothing_quotable_error,
    suggest_commercial_candidates as _suggest_commercial_candidates,
)


def _hydrate_delivery_eligibility(pricing_data):
    """[DRIFT-D 读路径回填 · Owner 2026-07-25 裁决 A] 存量报价单补齐交付资格字段。

    写路径(生成/审计)已填 ``commercial_delivery_eligible``,但**改动前已生成**的
    报价单持久化数据里没有这个字段;前端展示守卫读到 undefined 只能回落老启发式,
    合法商业词继续被误标红(正是本次要治的 A1 体验问题)。

    这里在**内存中**为缺字段的关键词补上唯一引擎判定。边界(逐条都是硬约束):
      - **只补缺失** —— 已有值一律尊重(含上游显式 False),不翻转任何既有结论;
      - **不写库** —— 纯读时投影,session/pricing_data 不落盘;
      - **不碰冻结快照** —— 仅在代理自有活数据分支调用,frozen 分支原样返回
        (§5.5 已确认报价按冻结快照);
      - **不改任何价格** —— 只加一个展示用布尔字段(§9.6);
      - **不阻断、不过滤** —— 不因该字段丢弃或拦截任何关键词,纯展示资格标注。
    """
    if not isinstance(pricing_data, dict):
        return pricing_data
    for kw in pricing_data.get("keywords") or []:
        if not isinstance(kw, dict):
            continue
        if kw.get("commercial_delivery_eligible") is None:
            kw["commercial_delivery_eligible"] = _delivery_eligible_field(
                kw.get("keyword", ""), intent_hint=kw.get("intent")
            )
    return pricing_data


def _inject_real_examples_into_business_lines(
    business_lines: list[dict],
    keywords_snapshot: list[dict],
    max_examples: int = 8,
) -> list[dict]:
    """[CTO-15.23 2026-05-12 BUG fix] 客户认知欺骗修复

    老板报:业务卡片下展示 9 个 example_scenarios · 但提交后实际只 commit 6 个
    根因:
      - example_scenarios 由 LLM extract_business_lines 凭知识凑(展示用 · 跟真词无关)
      - keywords_snapshot 是 quote 真关键词(每个带 business_line_id 归属)
      - 客户选业务线 → submit-business-lines 按 bl_id 过滤 keywords_snapshot · 仅 commit 该 bl 的真词
      - 客户看 9 个示范 vs 系统算 6 个真词 = 认知欺骗

    修法:GET /api/s/{token} 返回前 · 用 keywords_snapshot 里归属该 bl_id 的真词
          覆盖 example_scenarios · 客户看到 = 系统算 = 真词列表 · 数量一致

    向后兼容:
      - keywords_snapshot 缺 business_line_id(老 session)→ 保留 LLM 凑的 example_scenarios
      - 业务线下无真词归属 → 保留 LLM 凑的(防空白)
    """
    if not business_lines or not keywords_snapshot:
        return business_lines

    bl_to_real_keywords: dict = {}
    for kw in keywords_snapshot:
        if not isinstance(kw, dict):
            continue
        bl_id = kw.get("business_line_id")
        if bl_id is None:
            continue
        kw_text = kw.get("keyword", "")
        if not kw_text:
            continue
        # 信息型词不展示 · 客户看到 = 系统算 一致
        is_info, _reason = _is_informational_keyword(kw)
        if is_info:
            continue
        bl_to_real_keywords.setdefault(bl_id, []).append(kw_text)

    if not bl_to_real_keywords:
        return business_lines

    result = []
    for bl in business_lines:
        if not isinstance(bl, dict):
            result.append(bl)
            continue
        bl_id = bl.get("id")
        real_examples = bl_to_real_keywords.get(bl_id, [])
        if real_examples:
            bl_copy = {**bl, "example_scenarios": real_examples[:max_examples]}
            result.append(bl_copy)
        else:
            result.append(bl)
    return result


# ========== 状态转换规则 ==========
VALID_TRANSITIONS = {
    "selecting": {"keywords_submitted", "business_lines_submitted"},
    "business_lines_submitted": {"selecting", "keywords_submitted"},  # 操作员可回退让客户重选
    "keywords_submitted": {"selecting", "pricing_pending_review", "quoted"},
    "pricing_pending_review": {"quoted", "keywords_submitted"},
    "quoted": {"adding_keywords", "confirmed", "pricing_pending_review"},
    "adding_keywords": {"pricing_pending_review", "quoted"},
    "confirmed": {"pending_payment"},
    "pending_payment": {"active", "payment_overdue"},
    "payment_overdue": {"active"},
}

# [M4 SSOT 2026-06-07] 单个关键词最低价 import 自 keyword_value_scorer(全仓单一权威源 = ¥400·一视同仁含品牌词)
from tools.keyword_value_scorer import MIN_KEYWORD_PRICE, MIN_KEYWORD_PRICE_NATIONAL

# [P1 容量合同 2026-08-08] 篇数唯一取数出口(本文件不许再出现三档篇数字面量)
from tools.pricing_bands import (
    LEGACY_MISSING_CAPACITY_DEFAULT as _CAPACITY_MISSING_DEFAULT,
    fallback_tier_article_capacity as _fallback_tier_article_capacity,
    normalize_article_capacity as _normalize_article_capacity,
)
from auth.user_ctx import current_user_id

TIER_CONFIG = {
    # [2026-06-05 老板定稿] 目标出现率(交付验收指标 · 写"目标出现率"非"保证出现率" · 前端必展示不可删)
    #   [老板订正 2026-06-05] 「大约问 N 次出现 M 次」话术保留 + 出现率% 并存展示(不二选一):
    #     50%→问2次约1次 / 65%→问3次约2次 / 75%→问4次约3次(阈值比例对齐 frontend probability.ts · 三档各异)
    #   累计达标 30 天 = 完成交付(当日出现率≥目标记 1 达标日 · 不清零 · 累计 30 = 完成 · 见 db/monitoring_db.py)
    "entry":    {"label": "入门版", "target_share": 0.10, "ai_probability": "50%", "stars": 3},
    "standard": {"label": "标准版", "target_share": 0.20, "ai_probability": "65%", "stars": 4},
    "flagship": {"label": "旗舰版", "target_share": 0.30, "ai_probability": "75%", "stars": 5},
}


def _sync_pricing_tiers_from_config(pricing_data):
    """[P0 fix 2026-05-23 r12] 实时覆盖 cached pricing_data 的 tiers 字段
    防老 quote 在 keyword_selection_sessions.pricing_data JSON 里存的是部署前的
    ai_probability 旧值(65% / 75%)· 客户访问 /s/{token} 仍看旧值。
    返回前用当前 TIER_CONFIG 实时覆盖 ai_probability / label / target_share / stars。
    保持 total_price / total_articles 不变(那是当时算的定价 · 不能变 · 7 天价格锁)。
    """
    if not pricing_data or not isinstance(pricing_data, dict):
        return pricing_data
    tiers = pricing_data.get("tiers")
    if not isinstance(tiers, dict):
        return pricing_data
    for tier_key, tier_cfg in TIER_CONFIG.items():
        if tier_key in tiers and isinstance(tiers[tier_key], dict):
            # 仅同步元数据 · 不动 total_price / total_articles
            tiers[tier_key]["label"] = tier_cfg["label"]
            tiers[tier_key]["target_share"] = tier_cfg["target_share"]
            tiers[tier_key]["ai_probability"] = tier_cfg["ai_probability"]
            tiers[tier_key]["stars"] = tier_cfg["stars"]
    return pricing_data


def _check_expired(session: dict) -> dict:
    """检查并自动过期"""
    if session["status"] in ("confirmed", "expired", "pending_payment", "active", "payment_overdue", "pricing_pending_review", "business_lines_submitted"):
        return session
    if session.get("expires_at"):
        try:
            exp = datetime.fromisoformat(session["expires_at"])
            if datetime.now() > exp:
                update_session(session["token"], status="expired")
                session["status"] = "expired"
        except (ValueError, TypeError):
            pass
    return session


def _strip_internal_strong_tier(cluster_data: dict) -> None:
    """[前台3套餐红线 · 2026-06-05] customer-facing API 层【显式剥除】内部托管「strong/霸榜」第4档。

    前台/客户报价页只有 3 套餐(入门/标准/旗舰)。strong(SOV 0.50/霸榜)= 内部全自动托管档,
    不得进客户 payload —— 不只靠前端 TIER_META 不渲染(防漏出 + 防 90%+「几乎每次」话术泄到客户面)。
    托管/内部路径仍可用 generate_cluster_quote 原始 4 档(本剥除只作用于客户报价响应)。
    """
    if not isinstance(cluster_data, dict):
        return
    ts = cluster_data.get("tier_summaries")
    if isinstance(ts, dict):
        ts.pop("strong", None)
        ts.pop("霸榜版", None)
    for cluster in (cluster_data.get("clusters") or []):
        pricing = cluster.get("pricing")
        if isinstance(pricing, dict):
            pricing.pop("strong", None)
        for grp in ("core_keywords", "covered_keywords"):
            for kw in (cluster.get(grp) or []):
                if isinstance(kw, dict):
                    kw.pop("strong", None)
                    kw.pop("strong_price", None)
                    kw.pop("strong_articles", None)


# [audit P1 2026-06-10] 客户面词级内部字段黑名单:工厂成本/SOV 裸比例/算价底盘/审计标全部不出客户端点。
# 客户页(Selection/)对这些字段 0 引用(已核)· 代理端 QuoteCenter 读 session 全字段不受影响(只剥出口)。
# 违反话术铁律的核心三个:cost_per_article(工厂成本)· effective_competition/competition_ratio(SOV 裸数)· geo_multiplier。
_CUSTOMER_INTERNAL_KW_FIELDS = (
    "cost_per_article", "geo_multiplier", "effective_competition", "core_score",
    "value_score", "difficulty_score", "selling_price", "required_articles", "total_cost",
    "markup_ratio", "competition_ratio", "raw_price_before_band", "band_min", "band_max",
    "needs_review", "sem_price", "bidword_company_count", "content_count", "competitor_count",
    "source_authority", "source_authority_json", "search_probability", "search_volume",
    "data_source", "keyword_type", "is_broad", "audit_status", "audit_note",
    "price_before_audit", "pricing_formula_version", "classify_confidence", "classify_reason",
    "classify_source", "is_brand_keyword", "market_scope",
    # [报价解释层一期 2026-06-13] guarantee_unavailable 是内部护栏信号(价超天花板/放飞·参考价人工核),
    #   属内部定价策略 → 客户端剥除(客户只见三档价归零 → 前端「需核价·联系顾问」· 与 super_red_ocean 市场事实不同)。
    #   代理端(带 JWT)不脱敏 · OnlineQuoteFlow 仍读全字段做「为什么是这个价」人话拆解。
    "guarantee_unavailable",
    # [v2.3 DELTA 3 2026-06-14] ratio 观测字段(新值/flag-off 基线)= 内部护栏/复盘信号 → 客户端剥除(§10.3)。
    #   代理端(带 JWT)保留 · 仅供内部审核/影子报告;客户绝不见 cost_ratio/factory_ratio 等裸杠杆数。
    "cost_ratio", "value_ratio", "comp_ratio", "factory_ratio",
    # [P0-D 2026-06-14] 信任资产内部字段 → 客户端剥除(§5.3:客户绝不见裸 trust 分数/裸难度因子/采集状态)。
    #   代理端保留 · 人话 label(trust_verified_labels/trust_missing_labels)有意不在黑名单 → 客户转人话可见。
    "trust_asset_source", "trust_asset_score", "citation_readiness_score",
    "trust_asset_needs_review", "trust_ratio", "trust_asset_evidence",
    # [P0-A/B/C 收口 2026-06-15] 纵深对齐:与 guarantee_unavailable 同类的内部护栏/审计字段一并剥除。
    #   主防线是持久化 allowlist(只 append 白名单字段·当前不真泄露),此黑名单是出口兜底第二道净:
    #   · blowup_no_cache  = 缓存策略信号(护栏③ selling 过高·暗示「此词价格异常」)· 与 guarantee_unavailable 对称
    #   · guards           = 内部护栏决策 blob(含 media_factory 真实出厂成本 / value_evidence_cap 等裸杠杆)
    #   · industry_baseline= 行业月费量级背景(内部复盘)· v2_assessor_data = 内部审计 blob(cost/true_comp/llm std/ratios/trust 全量)
    #   · cost_snapshot_uncacheable = P0-A 缓存策略信号(成本非 db active)· 内部信号客户无意义
    #   (trust_asset_evidence 为预留项·当前 assessor 未产出·防将来加原始证据字段忘脱敏)
    "blowup_no_cache", "guards", "industry_baseline", "v2_assessor_data", "cost_snapshot_uncacheable",
    "super_red_ocean_level",  # [2026-06-17 Deploy-CTO hotfix] 内部 SRO 等级标签·客户只见 super_red_ocean bool
)


def _strip_internal_pricing_fields(payload) -> None:
    """[audit P1 2026-06-10] 客户面 token 端点出口脱敏(in-place):剥除词级内部字段。
    兼容 pricing_data({tiers,keywords})与 clusters_data({clusters:[{core/covered_keywords}]})两种结构。
    保留客户渲染所需:keyword/entry/standard/flagship(价/篇数)/price_locked_until/super_red_ocean(标)/
    intent/funnel_stage/category_label/recommendation_reason/upgradeable/source。session 内存储不动。
    [2026-06-13] super_red_ocean(市场事实·客户可见「需深度报价」)与 should_quote(信息型·客户可见「不报价」)
    有意保留;guarantee_unavailable(内部护栏信号)已加入黑名单脱敏(客户只见价归零「需核价」· 见上)。"""
    if not isinstance(payload, dict):
        return
    from services.quote_pricing_snapshot import strip_private_context
    strip_private_context(payload)

    # [WO_225-c1 §8.2] 🔴 整块剥除 `media_mix`,不是只剥换算键。
    #   工单只要求剥「换算键」(conversion_version / posts_per_slot_bps /
    #   posts_estimate / delivery_perspective),但这一块里**原本就有**
    #   `ratio.raw / cell_n / anchor_n / coverage_n_excluding_douyin / pool_version`
    #   —— 引用池样本数与内部比例,和 target_share 同类(元指令 #11:非代理禁裸露),
    #   今天正随冻结快照原样发给客户(本单查证:`pricing_data = frozen[pricing_snapshot]`
    #   之后没有任何一处 pop 过 media_mix)。只剥新键 = 明知旧键也不该在还留着。
    #   🔴 客户页零影响已核:全前端仅 `frontend/src/lib/quoteMediaMix.ts` 提到 media mix,
    #      没有任何组件读 `pricing_data.media_mix`(Owner ③「客户页一字不改」)。
    payload.pop("media_mix", None)

    def _clean(kws):
        for kw in (kws or []):
            if isinstance(kw, dict):
                for f in _CUSTOMER_INTERNAL_KW_FIELDS:
                    kw.pop(f, None)

    _clean(payload.get("keywords"))
    for cluster in (payload.get("clusters") or []):
        if isinstance(cluster, dict):
            _clean(cluster.get("core_keywords"))
            _clean(cluster.get("covered_keywords"))

    # [audit #2 返修] tier 级 target_share(内部 SOV)也是内部字段(违元指令#11 ·
    #   面向非代理禁裸露 SOV)。词级 35 字段已脱敏,仅 tier 级漏。客户面渲染用 ai_probability 话术,
    #   不需 target_share → 剥除。session 内存储不动(代理端仍可见)。
    tiers = payload.get("tiers")
    if isinstance(tiers, dict):
        for _tv in tiers.values():
            if isinstance(_tv, dict):
                _tv.pop("target_share", None)


def _is_owning_agent_for_session(request: Request, session: dict) -> bool:
    """[P0 2026-06-11 surface 区分] 判定 /api/s/{token} 调用方是否为该 session 归属代理本人/admin。

    /api/s/{token} 同时服务【C 端客户匿名选词页(裸 fetch 无 JWT)】与【代理端报价工作台
    OnlineQuoteFlow(api.get 带 JWT)】。audit P1 2026-06-10 的客户面脱敏无差别剥除 35 内部字段
    (含 search_volume/effective_competition/difficulty_score/value_score),误伤代理端
    (代理需看精确指标算利润 · 元指令#11 明确代理端后台可裸露百分比 · C 端仍脱敏)。

    端点在中间件白名单(PUBLIC_PREFIXES /api/s/)直接放行,不注入 request.state.user,
    故此处手动解析 Authorization 头。判定规则同 _require_session_owner_access:
    admin / created_by 本人 / brand.owner_user_id 本人。
    fail-closed:无 JWT / token 无效 / 越权他人 / 任何异常 → False(脱敏 · 保守不泄露)。
    """
    try:
        auth_header = request.headers.get("Authorization") or ""
        if not auth_header.startswith("Bearer "):
            return False
        payload = decode_jwt(auth_header[7:].strip())
        if not payload:
            return False
        user_id = payload.get("user_id") or payload.get("id")
        if not user_id:
            return False
        if bool(payload.get("is_admin")):
            return True

        # [资金红线] 本函数的返回值决定要不要给**未脱敏的代理端视图** ——
        # 里面含 cost_per_article(真实出厂成本)/ total_cost / markup_ratio /
        # factory_ratio / guards 等成本与毛利裸杠杆。看成本毛利在组织能力模型里是
        # `pricing.cost_and_margin_view`,属 OWNER_ONLY,员工角色根本配不上去。
        #
        # 但本路由挂在公开前缀 `/api/s/` 下,组织守卫**不会执行**;而员工是通过
        # 已授权的"建选词会话"路由成为 created_by 的 —— 于是员工凭自己的 token
        # 就能读到老板的进货成本和毛利。这里显式补一道:员工席位一律按客户视图脱敏。
        from auth.principal_identity import is_organization_seat_member
        if is_organization_seat_member(user_id):
            return False

        if session.get("created_by") and session.get("created_by") == user_id:
            return True
        brand_id = session.get("brand_id")
        if brand_id:
            conn = get_connection()
            try:
                cursor = conn.cursor()
                try:
                    cursor.execute("SELECT owner_user_id FROM brands WHERE id = %s", (brand_id,))
                    row = cursor.fetchone()
                    if row and row.get("owner_user_id") == user_id:
                        return True
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
            finally:
                conn.close()
    except Exception as e:
        logger.debug(f"[_is_owning_agent_for_session] surface 判定异常(默认脱敏): {e}")
    return False


def _sync_quote_monthly_price(quote_id, monthly_price) -> None:
    """[B1 2026-06-05] confirm 时把 quotes.monthly_price 回写成【客户已选核心词合计】。

    根治璧山 ¥77,791 headline:生成期 monthly_price = 全 core 求和(划线参考),
    客户确认后应 = 实际勾选核心词合计(confirmed_total_price 口径)· 否则列表/经营总览仍读全 core headline。
    防御式 DB(失败 rollback + 不阻断 confirm 主链路)· billing 0 改(只改 quotes.monthly_price 数值)。
    """
    if not quote_id:
        return
    conn = get_connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE quotes SET monthly_price = %s WHERE id = %s",
                (monthly_price, quote_id),
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"[_sync_quote_monthly_price] 回写失败 quote_id={quote_id}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        conn.close()


def _get_brand_name(brand_id: int) -> str:
    """获取品牌名

    [CTO-15.23 2026-05-10 Deploy-CTO P0-2.1 加固]
      老路径: cursor.execute 抛异常 → 直接进 finally 关连接 → 不 rollback →
              连接进池仍是 in_failed_state · 下个 request 拿到立即报 InFailedSqlTransaction
      新路径: 内层 try/except 包 SQL · 失败时 rollback + logger + 返兜底"客户" · 不阻断 mark_paid 主链路
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT name FROM brands WHERE id = %s", (brand_id,))
            row = cursor.fetchone()
            return row["name"] if row else "客户"
        except Exception as e:
            logger.warning(f"[_get_brand_name] SQL 失败 brand_id={brand_id} (返兜底文案): {e}")
            try:
                conn.rollback()
            except Exception as re:
                logger.warning(f"[_get_brand_name] rollback 失败 (连接已废): {re}")
            return "客户"
    finally:
        try:
            conn.close()
        except Exception: pass


def _require_session_owner_access(request: Request, token: str) -> dict:
    """校验当前登录用户是否有权操作该 selection session

    [CTO-15.23 2026-05-11] P0 安全漏洞修:
      老板报"非管理员无法删除自己报价"+ 全局排查发现 selection_api 3 个 DELETE 无权限检查
      规则:
      - admin 全权
      - session.created_by == 当前 user_id (创建者本人)
      - 或 session 关联 brand 的 owner_user_id == 当前 user_id (品牌归属者)

    Returns: session dict
    Raises: 401 / 403 / 404
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    user_id = user.get("user_id") or current_user_id(user)
    # [CTO-15.23 2026-05-11] 改 bool() 宽松比较 · 原 `is True` 严格匹配
    # 防 cache / JWT payload 里 is_admin 是 1 / "true" / 别的 truthy 类型时 fail
    is_admin = bool(user.get("is_admin"))

    session = get_session_by_token(token)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    if is_admin:
        return session

    # 创建者本人
    if session.get("created_by") and session["created_by"] == user_id:
        return session

    # 通过品牌 owner_user_id 校验
    brand_id = session.get("brand_id")
    if brand_id:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT owner_user_id FROM brands WHERE id = %s", (brand_id,))
                row = cursor.fetchone()
                if row and row.get("owner_user_id") == user_id:
                    return session
            except Exception as e:
                logger.warning(f"[_require_session_owner_access] brand owner SQL 失败 brand_id={brand_id}: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    raise HTTPException(status_code=403, detail="无权操作他人的报价会话")


def _mark_keywords_monitored(
    quote_id: int,
    selected_keyword_ids: list,
    *,
    keywords_snapshot=None,
    pricing_data=None,
    clusters_data=None,
) -> None:
    """[CTO-15.23 2026-05-08 监测乱入修]
    客户付费确认报价后 · 同步标记选中的 keyword 为 is_monitored=TRUE
    monitoring SQL (db/monitoring_db.py:get_client_keywords) 加 WHERE 过滤未选词

    场景:
      - 销售给客户报价 4 词(全写入 confirmed_keywords)
      - 客户在 SelectionPage 选 1 词付费
      - 没有此函数前 monitoring 拉全 4 词跑 Kimi 浪费 75% 调用
    """
    if not selected_keyword_ids or not quote_id:
        return

    selected_keywords = resolve_selected_keyword_texts(
        selected_keyword_ids,
        keywords_snapshot=keywords_snapshot,
        pricing_data=pricing_data,
        clusters_data=clusters_data,
    )
    if not selected_keywords:
        logger.warning(
            f"[mark_monitored] quote_id={quote_id} selected ids 无法解析为 keyword text: {selected_keyword_ids}"
        )
        return

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN;")
        cursor.execute(
            """
            SELECT keyword
              FROM confirmed_keywords
             WHERE quote_id = %s AND keyword = ANY(%s)
            """,
            (quote_id, selected_keywords),
        )
        matched_keywords = [
            row.get("keyword") if isinstance(row, dict) else row[0]
            for row in cursor.fetchall()
        ]
        if not matched_keywords:
            cursor.execute("ROLLBACK;")
            logger.warning(
                f"[mark_monitored] quote_id={quote_id} keyword text 命中 0 行 · 不重置 is_monitored: {selected_keywords}"
            )
            return

        cursor.execute(
            """
            UPDATE confirmed_keywords
               SET is_monitored = FALSE
             WHERE quote_id = %s
               AND COALESCE(is_monitored, FALSE) = TRUE
            """,
            (quote_id,),
        )
        cursor.execute("""
            UPDATE confirmed_keywords
               SET is_monitored = TRUE,
                   monitoring_status = 'active',
                   archived_at = NULL,
                   archive_reason = NULL
             WHERE quote_id = %s AND keyword = ANY(%s)
            RETURNING id, brand_id
        """, (quote_id, matched_keywords))
        marked_rows = [dict(r) for r in cursor.fetchall()]
        affected = len(marked_rows)

        # ══════════════════════════════════════════════════════════════
        # [WO_ORPHAN_MONITOR_FIX 2026-08-10 ①] 真建监测订阅 —— 同一个事务
        # ══════════════════════════════════════════════════════════════
        # 病史:本函数从前**只**置 is_monitored=TRUE。而每日 cron 只认
        #   `list_active_subscriptions()`(读 keyword_monitor_subscriptions),
        #   前端开关又读 is_monitored —— 于是 C 端客户一确认报价,当场产出
        #   "UI 显示开着、cron 永远不跑"的孤儿词(存量 147 条,横跨 05-11~07-15)。
        #   代理端 /enable 之所以没事,是因为它另外调了 create_keyword_monitor_subscription。
        #
        # 🔴 计费主体 = **品牌 owner**,不是操作者:C 端客户 token-only 不登录,
        #   订阅每天扣 130 算力,钱必须落到服务商头上。owner 拿不到 → fail-closed,
        #   **整笔回滚**(见下),绝不回落到别人头上。
        # 🔴 与确认报价**同事务**:要么"标了 + 订阅也建了",要么两样都不落。
        #   工单点名禁止的第三种孤儿路径(标了没建)在这里被结构性排除。
        subscribed = 0
        if marked_rows:
            from db.monitoring_db import create_keyword_monitor_subscription_with_cursor
            from services.monitor_billing import resolve_monitor_billing_user_with_cursor

            # brand_id 优先取词行自带的;为空再从报价单兜一次(老数据 confirmed_keywords.brand_id 可能空)
            _brand_id = next((r.get("brand_id") for r in marked_rows if r.get("brand_id")), None)
            if not _brand_id:
                cursor.execute("SELECT brand_id FROM quotes WHERE id = %s", (quote_id,))
                _qrow = cursor.fetchone()
                _brand_id = (_qrow or {}).get("brand_id") if _qrow else None

            billing_user_id = resolve_monitor_billing_user_with_cursor(cursor, _brand_id)
            if not billing_user_id:
                # fail-closed:确认不了付费主体就一个订阅都不建,并且**连 is_monitored 也不落**
                # —— 落了就又是一条孤儿。整笔回滚,状态回到"没开监测",可重试、可人工补。
                raise RuntimeError(
                    f"监测计费主体无法确认(brand_id={_brand_id})· 已整体回滚,不产出孤儿词")

            for _r in marked_rows:
                create_keyword_monitor_subscription_with_cursor(
                    cursor,
                    user_id=billing_user_id,
                    keyword_id=_r["id"],
                    quote_id=quote_id,
                    brand_id=_brand_id,
                )
                subscribed += 1

        conn.commit()
        conn.close()
        logger.info(
            f"[mark_monitored] quote_id={quote_id} 标记 {affected} 个 keyword is_monitored=TRUE"
            f" · 同事务建/复用监测订阅 {subscribed} 条(计费主体=品牌 owner)"
        )
    except Exception as e:
        # 🔴 不许静默(工单 ①):这里没有部分状态可留 —— 上面整段在一个事务里,
        #   没 commit 就等于没写。所以日志必须是 ERROR 级 + 说清后果,而不是从前那句 warning。
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(
            f"[mark_monitored] 失败并已整体回滚 quote_id={quote_id}(未产出孤儿词,"
            f"该单监测未开通,需人工确认): {e}", exc_info=True)
    finally:
        try:
            conn.close()
        except Exception: pass


# ================================================================
#  客户端 API(公开,/api/s/)
# ================================================================

class SubmitKeywordsRequest(BaseModel):
    selected_ids: List[int] = Field(..., min_length=1)
    custom_keywords: List[str] = Field(default_factory=list)


class SubmitBusinessLinesRequest(BaseModel):
    """客户提交业务线选择（新流程第一步）"""
    selected_business_line_ids: List[int] = Field(..., min_length=1)


class AddKeywordsRequest(BaseModel):
    keywords: List[str] = Field(..., min_length=1)


class ClusterSelectionItem(BaseModel):
    cluster_name: str
    selected_core_ids: List[int]
    covered_count: int  # 按比例缩减后的覆盖词数量

class ConfirmQuoteRequest(BaseModel):
    tier: str = Field(..., pattern="^(entry|standard|flagship)$")
    selected_keyword_ids: List[int] = Field(default_factory=list)
    clusters_selection: Optional[List[ClusterSelectionItem]] = None


def _acceptance_evidence(
    *, token: str, tier: str, selected_keyword_ids, clusters_selection=None,
    quote_id, brand_id,
) -> dict:
    """[工单 E3-4 · P1-8] 组一份受理证据。

    🔴 **一处**组、两个模式共用 —— 分模式各写一份就是同一谓词写两处,
       必有一处没人验(而两条路径落的证据不一致时,没有任何判据会红)。
    """
    from services.defensive_geo import acceptance_evidence as _ae

    payload = _ae.canonical_request(
        token=token, tier=tier, selected_keyword_ids=selected_keyword_ids,
        clusters_selection=clusters_selection)
    return {
        "token_subject": _ae.token_subject_of(quote_id=quote_id, brand_id=brand_id),
        "actor": _ae.actor_of(token),
        "request": payload,
        "request_hash": _ae.canonical_request_hash(payload),
    }


def _commit_frozen_quote_confirmation(
    *,
    token: str,
    expected_snapshot_id: int,
    selected_tier: str,
    final_keyword_ids: str,
    confirmed_at: str,
    confirmed_total_price: float,
    clusters_data: Optional[str] = None,
    accepted_snapshot_hash: Optional[str] = None,
    # [工单 E3-4 · P1-8 · 2026-08-26] 不可重建的受理事实。
    # 缺省 None ⇒ 既有调用方(含判据)行为逐字不变。
    accepted_request: Optional[dict] = None,
) -> None:
    """Commit a customer choice only against the exact quote snapshot they saw.

    [防御型 GEO WP4 · 2026-08-21] 可选的 v2 激活腿
    ------------------------------------------------
    ``accepted_snapshot_hash is None`` = **legacy 路径,行为不变**:
    SET 子句不追加任何一列,不写 accepted 指针,不入激活队列。
    分流键由调用方按**服务端 snapshot schema** 判定
    (``commercial_milestones.is_v2_enrolled``),请求体里的 ``mode`` 一个字都不读。

    传了 hash = enrolled v2:在**同一事务**里多做两件事,
    要么都成、要么随本事务一起回滚:

      1. 写 accepted 三件组(id / hash / at)。迁移 042 的
         ``keyword_selection_sessions_confirmed_group_complete`` CHECK 要求
         三列**要么全空要么全齐**,所以只能一起写。
      2. 登记 activation root(``enqueue_activation``,借本事务的游标)。

    🔴 为什么这两件必须一起做,而不是只入队:
       ``orphaned_activation_roots`` 的对账是拿 outbox 反 join
       ``keyword_selection_sessions.customer_confirmed_snapshot_id``。
       只写 outbox 不写指针,对账器**永远返空**、判据**恒绿** ——
       同一个谓词写成两半,必有一半没人验。

    🔴 为什么 ``RETURNING`` 多取两列:``quote_id`` / ``brand_id`` 要用作
       activation root 的身份。从**被更新的那一行**取是权威的;
       从调用方传进来的 session 字典取则可能与库里那一行不一致,
       而复合 FK 只校验 session 自己的 quote_id,兜不住传错。
       多返回两列对既有调用方零影响(它只做 ``fetchone()`` 真值判断)。
    """
    from services.quote_pricing_snapshot import QuoteSnapshotError

    # 追加谓词,不改写原谓词:legacy 时这两个片段都是空的,
    # 拼出来的 SQL 与本函数改动前逐字相同(判据
    # `test_legacy_confirm_sql_is_byte_identical` 钉住这一点)。
    _accepted_sets = ""
    _accepted_params: tuple = ()
    if accepted_snapshot_hash is not None:
        # [工单 E3-4 · P1-8 · Codex 二审] 三件组之外,再落**不可重建**的受理事实。
        #
        # snapshot id / hash 事后能重查;canonical request 的正文、
        # token 的 purpose / subject、点确认的 actor —— 当时不落,
        # 事后**没有任何地方**算得出来。而 activation 是拿这几样当授权依据的。
        #
        # 🔴 与三件组写在**同一条 UPDATE** 里:分两条写就出现"有指针没审计"
        #    的可见半状态,而 042 的 group_complete CHECK 只管三件组,兜不住它。
        # 🔴 legacy 路径(accepted_snapshot_hash is None)这一段整个不拼,
        #    SQL 与改动前逐字相同 —— test_legacy_confirm_sql_is_byte_identical 钉住。
        from services.defensive_geo import acceptance_evidence as _ae
        from psycopg2.extras import Json as _Json

        _req = dict(accepted_request or {})
        _accepted_sets = (",\n                customer_confirmed_snapshot_id=%s,"
                          " customer_confirmed_snapshot_hash=%s,"
                          " customer_confirmed_at=NOW(),"
                          " customer_confirmed_token_purpose=%s,"
                          " customer_confirmed_token_subject=%s,"
                          " customer_confirmed_actor=%s,"
                          " customer_confirmed_request_hash=%s,"
                          " customer_confirmed_request=%s")
        _accepted_params = (
            expected_snapshot_id, accepted_snapshot_hash,
            _ae.TOKEN_PURPOSE_CONFIRM,
            # subject 从**被更新的那一行**取不到(RETURNING 在 UPDATE 之后),
            # 所以由调用方在 canonical request 里带进来;取不到就落 NULL,
            # 让 classify_acceptance 判 legacy_unproven —— **不编一个**。
            _req.get("token_subject"),
            _req.get("actor"),
            _req.get("request_hash"),
            _Json(_req.get("request")) if _req.get("request") is not None else None,
        )

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            UPDATE keyword_selection_sessions
            SET status='confirmed', selected_tier=%s, final_keyword_ids=%s,
                confirmed_at=%s, confirmed_total_price=%s,
                clusters_data=COALESCE(%s, clusters_data), updated_at=NOW(){_accepted_sets}
            WHERE token=%s AND status='quoted' AND active_pricing_snapshot_id=%s
            RETURNING id, quote_id, brand_id
            """,
            (
                selected_tier,
                final_keyword_ids,
                confirmed_at,
                confirmed_total_price,
                clusters_data,
                *_accepted_params,
                token,
                expected_snapshot_id,
            ),
        )
        _row = cursor.fetchone()
        if not _row:
            raise QuoteSnapshotError(
                "QUOTE_CONFIRMATION_STALE",
                "报价已撤回或更新，请刷新后确认最新报价。",
                details={"recovery": "reload_public_quote"},
            )
        if accepted_snapshot_hash is not None:
            # 与上面那条 UPDATE 同游标、同事务。enqueue_activation 刻意不开
            # SAVEPOINT、不吞异常 —— 它失败就让整笔确认回滚,而不是留下
            # 「客户看到确认成功、激活却从没入列」的半状态。
            from services.defensive_geo.activation_outbox import enqueue_activation

            # ══════════════════════════════════════════════════════════════
            # 🔴 [工单 C-1 · Codex 终审 P1-6] 付款人身份**在这一刻冻结**
            # ══════════════════════════════════════════════════════════════
            # 在这之前入队只写 brand,``activation_materializer._tenant_of``
            # 每次物化都**现读** ``brands.owner_user_id``。品牌在「客户确认」与
            # 「物化」之间被转移,同一个 accepted snapshot 就解析出另一个 tenant、
            # 另一个付款方,预算签在别人头上。
            #
            # commercial basis 成立那一刻的付款人是**事实**,不该被之后的
            # 品牌转移改写 —— 所以在同一事务里把它算出来冻进队列行。
            #
            # 🔴 归属取**被更新的那一行**的 brand_id(``_row``),不取调用方传进来的
            #    session 字典 —— 与上面 RETURNING 多取两列同一条理由。
            # 🔴 判别位复用 ``payer_classification``(诊断链/发布链同源),
            #    不在这里复写一份;``activation_outbox`` 本身的结构锁禁止它
            #    import 任何计费面,所以判别必须发生在调用方这一层。
            # 🔴 ``actor_user_id`` 留 NULL 是**如实**的:本端点是 token-only
            #    客户面(``/s/{token}/confirm-quote`` 不接 ``Request``、
            #    中间件也不注入 ``request.state.user``),按下确认的是客户,
            #    不是平台用户。编一个 actor 比留空更糟。
            from services.defensive_geo import payer_classification as _payer_cls

            cursor.execute(
                "SELECT owner_user_id FROM brands WHERE id = %s",
                (int(_row["brand_id"]),),
            )
            _brand_row = cursor.fetchone()
            _tenant_owner = None
            if _brand_row is not None:
                _tenant_owner = (
                    _brand_row[0] if isinstance(_brand_row, tuple)
                    else _brand_row.get("owner_user_id")
                )
            if not _tenant_owner:
                # fail-closed:归属解析不出来就不写这条 activation 事实。
                # 写一条 payer 为空的行 = 把「谁付这笔钱」这个问题留给未来的
                # 某次现读去回答,而那正是本项要消灭的形态。
                raise QuoteSnapshotError(
                    "ACTIVATION_OWNER_UNRESOLVED",
                    "这份报价的归属暂时确认不了，请联系发你链接的人。",
                    details={"recovery": "contact_quote_owner"},
                )
            _payer = _payer_cls.classify_user_id(cursor, int(_tenant_owner))

            enqueue_activation(
                cursor,
                accepted_snapshot_id=int(expected_snapshot_id),
                quote_id=int(_row["quote_id"]),
                brand_id=int(_row["brand_id"]),
                accepted_snapshot_hash=accepted_snapshot_hash,
                tenant_owner_id=int(_tenant_owner),
                payer_user_id=int(_tenant_owner),
                payer_funding_policy=_payer.funding_policy,
                payer_principal_kind=_payer.principal_kind,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class EventsRequest(BaseModel):
    events: List[dict]


# ════════════════════════════════════════════════════════════════
# 交付计划 · 客户售前版(WO_GAPPLAN_RELOCATION_A 2026-08-10 · 阶段 2)
# ════════════════════════════════════════════════════════════════

# 客户能看到价格、且还在掏钱决策阶段的两个状态。
# 🔴 刻意不含 pricing_pending_review:那一档客户根本拿不到 pricing_data
#    (上面的分支把 pricing_data 置 None —— 销售草稿不给客户预览),
#    给一份交付计划却不给价格,是把未定稿的东西提前承诺出去。
# 🔴 也不含 confirmed 及之后:那些是**售后**,归阶段 3(暂缓,
#    gap_plan_publications 生产 0 行,现在搬过去客户只会长期看到空态)。
_PRESALE_PLAN_STATUSES = ("quoted", "adding_keywords")


def _build_customer_delivery_plan(quote_id: int) -> Optional[dict]:
    """客户售前版交付计划。同步阻塞,调用方用 asyncio.to_thread 包。

    🔴 **不新开公开端点、不复制计算逻辑**:直接调 services.gap_operation_plan 的
       build_snapshot / present_snapshot —— 与服务商侧同一份快照、同一套人话字典、
       同一道术语泄漏闸。现役 `GET /api/quotes/{id}/delivery-plan` 走不通不是因为
       逻辑不够用,是因为 `/api/quotes/` 既不在 PUBLIC_PREFIXES 也不在
       PORTAL_ALLOWED_PREFIXES,客户 token-only 会话在 auth 中间件层就 401。

    🔴 鉴权由**本端点的 session token** 承担:token → session → session["quote_id"]。
       客户拿不到别人的 token 就拿不到别人的 quote_id,不存在参数越界面
       (quote_id 不是请求参数,是服务端从 session 里读出来的)。

    🔴 受众硬编码 customer:actions 恒空、access 相关全屏蔽由服务端裁剪,
       不靠前端"不渲染按钮"。前端不渲染只是没画出来,接口照样把动作发出去了。

    🔴 任何异常一律吞掉返回 None:售前预览是加分项。它挂了不能连带把
       "客户看报价"这件主事拖成 500 —— 那是拿锦上添花换掉了雪中送炭。
    """
    from services import gap_operation_plan as plan_service
    from db import gap_plan_db

    quote = get_quote(int(quote_id))
    if not quote:
        return None
    publications = gap_plan_db.get_publications(int(quote_id))
    capacity = plan_service.compute_capacity(quote, publications)
    bundle = plan_service.build_snapshot(quote, actor_user_id=None)
    presented = plan_service.present_snapshot(
        bundle, capacity=capacity, audience=plan_service.AUDIENCE_CUSTOMER
    )
    # [WP7 收尾 2026-08-17 · 规格 03 §10]
    # 「canonical retract 后交付计划、selection token、小榜、客户 operation plan
    #   同 cutoff 都同步下架」。
    #
    # 🔴 这里不是"再查一次发布表",而是**挂上同一个 canonical 投影**:
    #    gap 侧的 `evidence_published` 是只增不减的布尔(schema 里还有单调 CHECK),
    #    撤稿在它上面永远不生效。客户看到的"已发布"必须来自会随撤稿回落的那个数。
    try:
        from services.publication_stage_adapters import quote_stage_tuple, stage_labels

        proj = quote_stage_tuple(int(quote_id))
        stages = proj.get("stages") or {}
        presented["canonical_stages"] = stages
        presented["canonical_stage_labels"] = stage_labels()
        presented["canonical_cutoff"] = proj.get("cutoff")
        presented["canonical_source_versions"] = proj.get("source_versions")
        retracted = int((proj.get("quality_counts") or {}).get(
            "publication_retracted_at_cutoff", 0))
        if retracted:
            # 不隐藏历史、也不退成"未发布":如实说明现状与下一步(01 §5.2 末)。
            presented["retraction_notice"] = {
                "count": retracted,
                "message": "有内容曾发布、现已下架",
                "next_action": "contact_agent",
            }
    except Exception as exc:                        # noqa: BLE001
        logger.debug(f"[s/token] 六阶段投影跳过 quote_id={quote_id}: {exc}")
    return presented


async def _customer_delivery_plan_or_none(session: dict) -> Optional[dict]:
    """给 /api/s/{token} 用的安全外壳。"""
    quote_id = session.get("quote_id")
    if not quote_id:
        return None
    try:
        return await asyncio.to_thread(_build_customer_delivery_plan, int(quote_id))
    except Exception as exc:                        # noqa: BLE001
        logger.debug(f"[s/token] 售前交付计划跳过 quote_id={quote_id}: {exc}")
        return None


def _milestone_payload_for_session(session: dict) -> Optional[dict]:
    """五里程碑只读投影 —— 只对 enrolled v2 报价下发,legacy 返回 ``None``。

    🔴 全程 fail-soft **但不 fail-open**:任何异常都返回 ``None``(= 什么都不加),
       绝不返回一个半截的投影。读面挂掉不该把客户的选词页一起带走,
       但"给 legacy 编一个五里程碑视图"是 ACT-01 明令禁止的漂移,
       所以宁可不下发,也不猜。

    activation 两个事实分开取(``has_durable_activation`` / ``activation_materialized``):
    投影层刻意没把它们合成三值枚举,因为合并会让「已收款未物化」和「已物化」
    共用一个入口,少传一个就静默滑到终态。这里也照样分开传。
    """
    try:
        from services.defensive_geo.commercial_milestones import NotEnrolled, project
        from services.quote_pricing_snapshot import get_frozen_snapshot

        quote_id = session.get("quote_id")
        if not quote_id:
            return None
        frozen = get_frozen_snapshot(int(quote_id))
        pricing = _safe_json((frozen or {}).get("pricing_snapshot"), {})

        has_durable = False
        materialized = False
        accepted_id = session.get("customer_confirmed_snapshot_id")
        if accepted_id:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT status FROM defgeo_activation_outbox "
                    "WHERE accepted_snapshot_id=%s AND quote_id=%s",
                    (int(accepted_id), int(quote_id)),
                )
                rows = [r["status"] for r in (cur.fetchall() or [])]
                has_durable = bool(rows)
                materialized = any(s == "materialized" for s in rows)
            finally:
                conn.close()

        view = project(
            session_status=session.get("status"),
            pricing_snapshot=pricing,
            has_durable_activation=has_durable,
            activation_materialized=materialized,
        )
        if isinstance(view, NotEnrolled):
            return None
        return {
            "milestone": view.milestone,
            "userLabel": view.user_label,
            "stepIndex": view.step_index,
            "stepTotal": view.step_total,
            "steps": list(view.steps),
            "nextAction": {"kind": view.next_action_kind, "label": view.next_action_label},
            "projectionVersion": view.projection_version,
        }
    except Exception as _ms_err:  # pragma: no cover - 读面绝不因投影而 500
        logger.debug(f"[s/token] 里程碑投影跳过: {_ms_err}")
        return None


@router.get("/s/{token}")
async def get_selection_page(token: str, request: Request):
    """获取选词页面数据（按状态返回不同字段）"""
    session = get_session_by_token(token)
    if not session:
        raise HTTPException(404, "链接不存在")

    session = _check_expired(session)

    # 增加访问计数（非阻塞，失败不影响数据返回）
    try:
        update_session(token, visit_count=(session.get("visit_count") or 0) + 1)
    except Exception as e:
        logger.debug(f"visit_count 更新跳过: {e}")

    brand_name = _get_brand_name(session["brand_id"])

    keywords = _safe_json(session.get("keywords_snapshot"), [])

    # v3.6 白标：客户公开选词页下发归属代理品牌（surface 写死 customer · 脱敏）
    # 契约见 EXEC_WHITELABEL_V36_FULL_2026-05-29.md §0「客户页后端下发契约」
    _wl_payload = {"whitelabel": None, "display_scope": "platform"}
    try:
        from services.public_whitelabel import get_public_whitelabel_data
        _wl_payload = get_public_whitelabel_data(
            quote_id=session.get("quote_id"),
            brand_id=session.get("brand_id"),
        )
    except Exception as _wl_err:
        logger.debug(f"[s/token] whitelabel 下发跳过: {_wl_err}")

    base = {
        "brand_name": brand_name,
        "status": session["status"],
        "expires_at": session.get("expires_at"),
        "whitelabel": _wl_payload.get("whitelabel"),
        "branding_status": _wl_payload.get("display_scope", "platform"),
        # [audit #13 2026-06-10] 不再下发 owner_user_id(内部代理 user_id)· 客户选词页白标改读上面内联
        #   whitelabel(已 customer-gate + 脱敏)· 防匿名串联 /api/public/whitelabel/{uid} 枚举代理画像
    }

    # [防御型 GEO WP4 · 2026-08-21] 五里程碑只读投影(读面接线)。
    #
    # 🔴 非 enrolled 时**一个键都不加** —— 不是加个 `null`,是不加。
    #    ACT-01 要求 legacy 行为零漂移;多一个键就是漂移(而且本仓 DTO
    #    `extra="forbid"` 已经炸过三次:多返回一个键 = 端点必 500)。
    #    判据 `test_legacy_selection_page_response_gains_no_key` 钉住这一点。
    _milestone = _milestone_payload_for_session(session)
    if _milestone is not None:
        base["defensive_geo"] = _milestone

    if session["status"] == "expired":
        return base

    if session["status"] in ("selecting", "business_lines_submitted"):
        business_lines, _id_map = _normalize_business_lines_for_session(
            _safe_json(session.get("business_lines"), [])
        )
        # [CTO-15.23 2026-05-12 BUG fix] 用真 keywords_snapshot 词覆盖 example_scenarios
        #   防客户看 9 LLM 示范 vs 系统算 6 真词的认知欺骗
        business_lines = _inject_real_examples_into_business_lines(business_lines, keywords)
        # [#178] 刷新后卡片还在:零可交付读面从快照重建,与 POST 同名同形。
        _considered = None
        try:
            _selected_bl_ids = {
                int(line["id"]) for line in business_lines
                if isinstance(line, dict) and line.get("is_selected") and line.get("id") is not None
            }
            if _selected_bl_ids:
                _all_bl_ids = {
                    int(line["id"]) for line in business_lines
                    if isinstance(line, dict) and line.get("id") is not None
                }
                _considered = set(_select_keyword_ids_for_business_lines(
                    keywords, _selected_bl_ids, _id_map, _all_bl_ids,
                ))
        except Exception as _bl_err:
            # 归属算不出来就不缩窄(宁可多列几条被排除的词,也不要一条都不给)。
            logger.debug(f"[#178] 业务方向归属不可用,排除清单不按方向缩窄: {_bl_err}")
            _considered = None
        return {
            **base,
            "keywords": keywords,
            "business_lines": business_lines,
            "selected_ids": _safe_json(session.get("selected_keyword_ids"), []),
            "custom_keywords": _safe_json(session.get("custom_keywords"), []),
            "selection_context": _generate_selection_context(keywords, brand_name),
            **_no_deliverable_view(session, keywords, _considered),
        }

    if session["status"] == "keywords_submitted":
        return {
            **base,
            "keywords": keywords,
            "selected_ids": _safe_json(session.get("selected_keyword_ids"), []),
            "custom_keywords": _safe_json(session.get("custom_keywords"), []),
            "keywords_submitted_at": session.get("keywords_submitted_at"),
        }

    if session["status"] in ("pricing_pending_review", "quoted", "adding_keywords"):
        is_owner = _is_owning_agent_for_session(request, session)
        if is_owner:
            # [DRIFT-D 读路径回填] 仅代理自有活数据分支;frozen 分支不碰(§5.5)。
            pricing_data = _hydrate_delivery_eligibility(
                _sync_pricing_tiers_from_config(_safe_json(session.get("pricing_data")))
            )
            clusters_data = _safe_json(session.get("clusters_data"))
        elif session["status"] == "pricing_pending_review":
            # A customer bearer token must never preview the mutable sales draft.
            pricing_data = None
            clusters_data = None
        else:
            from services.quote_pricing_snapshot import get_frozen_snapshot
            frozen = await asyncio.to_thread(get_frozen_snapshot, int(session["quote_id"]))
            if not frozen:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "QUOTE_SNAPSHOT_REQUIRED",
                        "message": "该历史报价尚未生成冻结快照，请联系报价方重新审核发送。",
                        "priority": "P1",
                        "retryable": False,
                        "recovery": "contact_quote_owner",
                    },
                )
            pricing_data = _safe_json(frozen.get("pricing_snapshot"), {})
            clusters_data = _safe_json(frozen.get("clusters_snapshot"))
            _strip_internal_pricing_fields(pricing_data)
            _strip_internal_pricing_fields(clusters_data)
        # 售前版交付计划:只在客户真能看到价格的两档下发(pricing_data 为 None 的
        # pricing_pending_review 分支不给 —— 见 _PRESALE_PLAN_STATUSES 的注释)
        delivery_plan = None
        if session["status"] in _PRESALE_PLAN_STATUSES and pricing_data is not None:
            delivery_plan = await _customer_delivery_plan_or_none(session)
        return {
            **base,
            "keywords": keywords,
            "selected_ids": _safe_json(session.get("selected_keyword_ids"), []),
            "custom_keywords": _safe_json(session.get("custom_keywords"), []),
            "pricing_data": pricing_data,
            "clusters_data": clusters_data,
            "selected_tier": session.get("selected_tier", "standard"),
            "pending_keywords": _safe_json(session.get("pending_keywords"), []),
            "selection_context": _generate_selection_context(keywords, brand_name),
            "delivery_plan": delivery_plan,
        }

    if session["status"] in ("confirmed", "pending_payment", "active", "payment_overdue"):
        is_owner = _is_owning_agent_for_session(request, session)
        if is_owner:
            # [DRIFT-D 读路径回填] 仅代理自有活数据分支;frozen 分支不碰(§5.5)。
            pricing_data = _hydrate_delivery_eligibility(
                _sync_pricing_tiers_from_config(_safe_json(session.get("pricing_data")))
            )
            clusters_data = _safe_json(session.get("clusters_data"))
        else:
            from services.quote_pricing_snapshot import get_frozen_snapshot
            frozen = await asyncio.to_thread(get_frozen_snapshot, int(session["quote_id"]))
            if not frozen:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "QUOTE_SNAPSHOT_REQUIRED",
                        "message": "该历史报价尚未生成冻结快照，请联系报价方重新审核发送。",
                        "priority": "P1",
                        "retryable": False,
                        "recovery": "contact_quote_owner",
                    },
                )
            pricing_data = _safe_json(frozen.get("pricing_snapshot"), {})
            clusters_data = _safe_json(frozen.get("clusters_snapshot"))
            _strip_internal_pricing_fields(pricing_data)
            _strip_internal_pricing_fields(clusters_data)

        # Phase B (CTO-15.11 2026-04-28):激活后给客户门户链接
        # 老板痛点(40 岁老销售):客户付款后 /s/ 重访不知道下一步看什么
        # 修法:激活/已付款时返 portal_token · ConfirmCelebration 加"查看数据看板"按钮
        portal_token = None
        try:
            qid = session.get("quote_id")
            if qid and session["status"] in ("active", "paid"):
                from db.diagnosis_db import get_connection
                _conn = get_connection()
                try:
                    _cur = _conn.cursor()
                    _cur.execute(
                        """SELECT token FROM client_access_tokens
                           WHERE quote_id = %s AND (is_active = 1 OR is_active IS NULL)
                           ORDER BY created_at DESC LIMIT 1""",
                        (qid,),
                    )
                    _row = _cur.fetchone()
                    if _row:
                        portal_token = _row.get("token") if hasattr(_row, "get") else _row[0]
                finally:
                    try:
                        _conn.close()
                    except Exception:
                        pass
        except Exception as _pe:
            logger.debug(f"[s/token] portal_token 查询跳过: {_pe}")

        return {
            **base,
            "keywords": keywords,
            "selected_ids": _safe_json(session.get("selected_keyword_ids"), []),
            "pricing_data": pricing_data,
            "clusters_data": clusters_data,
            "selected_tier": session.get("selected_tier"),
            "final_keyword_ids": _safe_json(session.get("final_keyword_ids"), []),
            "confirmed_at": session.get("confirmed_at"),
            "confirmed_total_price": session.get("confirmed_total_price"),
            "final_price": session.get("final_price"),
            "sales_confirmed_at": session.get("sales_confirmed_at"),
            "payment_received_at": session.get("payment_received_at"),
            "portal_token": portal_token,
        }

    return base


@router.post("/s/{token}/submit-keywords")
async def submit_keywords(token: str, req: SubmitKeywordsRequest):
    """客户提交关键词选择"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    committed = False
    replayed = False
    review_override_count = 0
    delivery_exclusions: list[dict] = []
    excluded_custom: list[dict] = []
    all_excluded = False
    notify_sent = False
    session_status = ""
    try:
        session = _load_selection_session_for_update(conn, token)
        if not session:
            raise HTTPException(404, "链接不存在")
        session = _check_expired_locked(conn, session)
        keywords = _normalize_unique_keyword_snapshot_ids(
            _safe_json(session.get("keywords_snapshot"), [])
        )
        selected_ids = _normalize_requested_selection_ids(
            req.selected_ids,
            allowed_ids={int(item["id"]) for item in keywords},
            invalid_code="KEYWORD_SELECTION_IDS_INVALID",
            unknown_code="KEYWORD_SELECTION_IDS_STALE",
            label="关键词选择",
        )
        custom_keywords = list(req.custom_keywords)
        # [SSOT geo-commercial-intent-governance-v1.0 §3.2/§3.3] 商业交付资格
        # 分区:知识词/裸词逐条排除并给出原因(绝不静默);重放比较与落库
        # 一律使用排除后的有效选择,保证同一请求重放幂等。
        brand_name = _session_brand_name(conn, session)
        keywords, delivery_exclusions = _partition_delivery_exclusions(
            keywords, selected_ids, reviewed_at=now, brand_name=brand_name,
        )
        excluded_ids = {int(item["id"]) for item in delivery_exclusions}
        if excluded_ids:
            selected_ids = [
                keyword_id for keyword_id in selected_ids
                if int(keyword_id) not in excluded_ids
            ]
        excluded_custom: list[dict] = []
        if custom_keywords:
            # [SSOT §3.2/§3.3 · Review-CTO 2026-07-23 P1-2] 自定义词按统一
            # 引擎裁决:知识词/裸词排除(无改选入口);needs_clarification
            # 的歧义词进「需澄清」——同样不得计价/确认,但理由单列,
            # 客户澄清(改写成明确商业问法)后可重新提交。
            from services.commercial_query_policy import evaluate as _policy_evaluate
            kept_custom: list[str] = []
            for custom_word in custom_keywords:
                # [#178] 客户自己写的词更常是「<品牌>怎么样 / 靠谱吗」——
                #   不传品牌名时这类词落 uncertain ⇒ 进"需澄清区" ⇒ 客户按提示改写,
                #   改出来的还是同一句,再被拒一次。品牌直问本来就是可交付的。
                decision = _policy_evaluate(str(custom_word), brand_name=brand_name or None)
                if decision.commercial_delivery_eligible:
                    kept_custom.append(custom_word)
                    continue
                if decision.needs_clarification:
                    reason = (
                        "暂无法确认该问法是否会促使 AI 推荐具体品牌/服务商，"
                        "已放入需澄清区；改写成明确的选型/推荐/价格问法后可重新提交"
                    )
                    kind = "needs_clarification"
                else:
                    reason = "该问法不会促使 AI 推荐具体品牌/服务商，不具备付费交付价值"
                    kind = "knowledge_term_not_deliverable"
                excluded_custom.append(
                    {
                        "keyword": str(custom_word),
                        "reason": reason,
                        "kind": kind,
                        "policy_version": decision.policy_version,
                    }
                )
            custom_keywords = kept_custom

        # [#178 P0 · 2026-09-12] 选中的 + 自定义的全被排除 ⇒ **不落库、不计价**,
        #   但也不是 400 字符串。和 submit-business-lines 同一份响应契约,
        #   前端拿同样的字段渲染同一张出口卡片(两条出口:改写问法 / 让报价方补词)。
        #   🔴 条件里要求"确实发生过排除"(excluded_ids or excluded_custom):
        #      客户什么都没选就提交(selected=[] 且 custom=[])是另一回事,
        #      那条路今天的行为不在本单范围,别顺手改掉。
        #   状态**保持原值**:selecting 还是 selecting,business_lines_submitted 还是它;
        #   两者都在下面那条允许态里,客户补一条商业问法可以原地再提交。
        all_excluded = bool(excluded_ids or excluded_custom) and not selected_ids and not custom_keywords
        if all_excluded:
            session_status = str(session["status"])
            if session_status not in ("selecting", "business_lines_submitted"):
                raise HTTPException(400, f"当前状态 {session_status} 不允许提交关键词")
            # [#178-B · Review 记的 P2] 原来这里直接 rollback ⇒ 逐条排除的戳没落库。
            # 后果不是"少存点东西":客户刷新后卡片消失,而且首推通知若失败,
            # 他点「让报价方补充」会被状态守卫 400 —— **第一次不阻断、第二次却挡死**。
            # 只落 keywords_snapshot(戳)+ updated_at:
            #   · **不**改 status、**不**动 selected_keyword_ids、**不**写 keywords_submitted_at
            #     ⇒ "不落 keywords_submitted、不计价"这条约束逐字保持;
            #   · 戳是判定结果的唯一真相源,读面据它重建,不另存布尔。
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE keyword_selection_sessions
                   SET keywords_snapshot = %s,
                       updated_at = %s
                 WHERE token = %s
                """,
                (json.dumps(keywords, ensure_ascii=False), now, token),
            )
            conn.commit()
        elif session["status"] == "keywords_submitted":
            current_ids = _safe_json(session.get("selected_keyword_ids"), [])
            current_custom = _safe_json(session.get("custom_keywords"), [])
            if current_ids == selected_ids and current_custom == custom_keywords:
                replayed = True
                conn.rollback()
            else:
                raise KeywordSelectionContractError(
                    "KEYWORD_SELECTION_ALREADY_COMMITTED",
                    "该选词链接已提交其他选择，请刷新查看最新结果。",
                )
        elif session["status"] not in ("selecting", "business_lines_submitted"):
            raise HTTPException(400, f"当前状态 {session['status']} 不允许提交关键词")
        else:
            keywords, review_override_count = _record_keyword_review_overrides(
                keywords,
                selected_ids,
                actor_kind="customer_keyword_selection",
                actor_id=session.get("id"),
                reason="客户在选词页面明确勾选并提交",
                reviewed_at=now,
                brand_name=brand_name,
            )
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE keyword_selection_sessions
                   SET status = 'keywords_submitted',
                       selected_keyword_ids = %s,
                       custom_keywords = %s,
                       keywords_snapshot = %s,
                       keywords_submitted_at = %s,
                       updated_at = %s
                 WHERE token = %s
                """,
                (
                    json.dumps(selected_ids),
                    json.dumps(custom_keywords, ensure_ascii=False)
                    if custom_keywords else None,
                    json.dumps(keywords, ensure_ascii=False),
                    now,
                    now,
                    token,
                ),
            )
            conn.commit()
            committed = True
    except KeywordSelectionContractError as error:
        conn.rollback()
        raise _selection_contract_http_error(error) from error
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if committed:
        try:
            _durable_selection_notice(
                brand_id=session["brand_id"], quote_id=session["quote_id"],
                event_type=NotificationEventType.BUSINESS_ACTION_REQUIRED,
                terminal_state="keywords_submitted",
                status="客户已完成关键词选择",
                summary=f"已选 {len(selected_ids)} 个关键词，请继续处理报价。",
            )
        except Exception as e:
            logger.exception("关键词提交通知写入 outbox 失败: %s", e)

    if all_excluded:
        # 零可交付也要让报价方知道(同一张报价单幂等一条);读面 notify_sent 取既有状态。
        notify_sent = _push_no_deliverable_notice(
            brand_id=session["brand_id"], quote_id=session["quote_id"],
            summary=(
                f"客户提交的 {len(delivery_exclusions) + len(excluded_custom)} 个问法"
                "都是知识/百科类或需澄清,不会促使 AI 推荐品牌,暂无可交付内容;"
                "请补充商业选型问法后重新发送。"
            ),
        )

    return {
        "success": True,
        "replayed": replayed,
        "keywords_review_overridden": review_override_count,
        # [SSOT §3.3] 数量变化必须可见:逐条列出被排除的知识词与原因,
        # 合格商业词 N 选 N 提交,绝不静默缩减。
        "selected_count": len(selected_ids),
        "delivery_excluded_keywords": delivery_exclusions,
        "delivery_excluded_custom_keywords": excluded_custom,
        # [#178] 零可交付:200 结构化,不落 keywords_submitted、不计价。
        "all_excluded": all_excluded,
        # 这条路只可能是 all_excluded:selected_ids 的请求模型是 min_length=1,
        # "一条候选词都没有"到不了这里。
        "reason": "all_excluded" if all_excluded else None,
        "status": session_status if all_excluded else "keywords_submitted",
        "next_action": _no_deliverable_next_action(notify_sent) if all_excluded else None,
    }


@router.post("/s/{token}/submit-business-lines")
async def submit_business_lines(token: str, req: SubmitBusinessLinesRequest):
    """客户提交业务线选择（新流程第一步）→ 触发后台生成关键词+报价"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    committed = False
    replayed = False
    review_override_count = 0
    selected_names: list[str] = []
    filtered_keyword_ids: list[int] = []
    delivery_exclusions: list[dict] = []
    all_excluded = False
    had_candidate_keywords = False
    no_deliverable_reason = "all_excluded"
    no_deliverable_committed = False
    notify_sent = False
    try:
        session = _load_selection_session_for_update(conn, token)
        if not session:
            raise HTTPException(404, "链接不存在")
        session = _check_expired_locked(conn, session)
        business_lines, business_line_id_map = _normalize_business_lines_for_session(
            _safe_json(session.get("business_lines"), []),
            strict=True,
        )
        if not business_lines:
            raise HTTPException(400, "未找到业务线数据")
        all_business_line_ids = {
            int(line["id"])
            for line in business_lines
            if isinstance(line, dict) and line.get("id") is not None
        }
        selected_business_line_ids = _normalize_requested_selection_ids(
            req.selected_business_line_ids,
            allowed_ids=all_business_line_ids,
            invalid_code="BUSINESS_LINE_SELECTION_IDS_INVALID",
            unknown_code="BUSINESS_LINE_SELECTION_IDS_STALE",
            label="业务方向选择",
        )
        selected_bl_ids = set(selected_business_line_ids)
        for business_line in business_lines:
            business_line["is_selected"] = int(business_line["id"]) in selected_bl_ids
            if business_line["is_selected"]:
                selected_names.append(business_line["name"])
        if not selected_names:
            raise HTTPException(400, "请至少选择一个业务线")

        keywords = _safe_json(session.get("keywords_snapshot"), [])
        keywords = _normalize_unique_keyword_snapshot_ids(keywords)
        filtered_keyword_ids = _select_keyword_ids_for_business_lines(
            keywords,
            selected_bl_ids,
            business_line_id_map,
            all_business_line_ids,
        )
        # [#178-B · Review 09-12 裁定 1] 这里原来也是一条无出口的 400
        # (「选中业务线下没有可用关键词 · 请联系销售调整业务线设置」)。
        # 触发条件与"词全被排除"不同 —— 这是该方向下**一条词都没生成**;
        # 但对客户是同一件事:被拒、没有下一步。工单点名的是实例,缺陷是类。
        # 收进**同一份响应契约**,用 reason 区分,前端据此换标题。
        had_candidate_keywords = bool(filtered_keyword_ids)

        # [SSOT geo-commercial-intent-governance-v1.0 §3.2/§3.3] 商业交付资格
        # 分区(与手动选词提交同一合同):知识词逐条排除并给出原因;重放
        # 比较与落库一律用排除后的有效选择。
        brand_name = _session_brand_name(conn, session)
        keywords, delivery_exclusions = _partition_delivery_exclusions(
            keywords, filtered_keyword_ids, reviewed_at=now, brand_name=brand_name,
        )
        excluded_ids = {int(item["id"]) for item in delivery_exclusions}
        if excluded_ids:
            filtered_keyword_ids = [
                keyword_id for keyword_id in filtered_keyword_ids
                if int(keyword_id) not in excluded_ids
            ]
        # 两种"零可交付",同一个出口,不同的 reason:
        #   all_excluded         —— 有词,但全被商业意图闸排除(知识/百科/需澄清)
        #   no_keywords_for_lines—— 该方向下**一条候选词都没有**(报价方还没生成)
        # 布尔 all_excluded 的语义照旧 = "本次没有任何可交付词",两种都为真。
        all_excluded = not filtered_keyword_ids
        no_deliverable_reason = (
            "all_excluded" if had_candidate_keywords else "no_keywords_for_lines"
        )

        if all_excluded:
            # [#178 P0 · 2026-09-12] 这里原来是
            #     raise HTTPException(400, "…均不具备付费交付价值 · 请联系报价方补充商业选型问题")
            # 前端对 4xx 一律 toast.error ⇒ 流程停在业务线步,**进不了选词步**,
            # 而自定义词入口和逐条排除原因只长在选词步 ⇒ 客户没有任何出口。
            # 治理 SSOT(GEO_COMMERCIAL_INTENT_GOVERNANCE_SSOT_2026-07-23 :133-134)原文:
            # 未命中法律禁止事实时,结果只能是 advisory / local_repair / human_decision_required,
            # **不得是无出口的全局拒绝**。§3.2「知识词不进付费交付」这条政策本身不放宽:
            # 词照样一个不计价,变的只是"告诉他怎么走下一步"。
            #
            # 落 business_lines_submitted 而不是 keywords_submitted:
            #   · 正常路径落的是 keywords_submitted(见下面那条 UPDATE)——那是"选词已定、去报价";
            #     零可交付时报价方还没补词,落进去等于宣布一张 0 词的选择成立。
            #   · business_lines_submitted 是状态机里现成的一档(:665-666 允许 selecting→它),
            #     语义正是"客户已选方向、等报价方继续"。且它同时是 submit-keywords 的允许态,
            #     客户补一条「<品牌>怎么样」还能原地提交 —— 出口打在**真的允许态**上,不是死按钮。
            #   · 不写 keywords_submitted_at:那个时间戳代表"选词完成",这里没完成。
            if session["status"] == "keywords_submitted":
                # 已经成功提交过一份选择的会话,不许被一次全排除的提交降级成空集合。
                raise KeywordSelectionContractError(
                    "BUSINESS_LINE_SELECTION_ALREADY_COMMITTED",
                    "该选词链接已提交其他业务方向，请刷新查看最新结果。",
                )
            if session["status"] not in ("selecting", "business_lines_submitted"):
                raise HTTPException(400, f"当前状态 {session['status']} 不允许提交业务线选择")
            prior_selected = _safe_json(session.get("selected_keyword_ids"), []) or []
            prior_line_ids = _selected_business_line_ids(
                _safe_json(session.get("business_lines"), [])
            )
            if (
                session["status"] == "business_lines_submitted"
                and not prior_selected
                and prior_line_ids == sorted(selected_business_line_ids)
            ):
                # 同一份选择重放:不重写、不重推通知(通知状态由读面从 outbox 取)。
                replayed = True
                conn.rollback()
            else:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE keyword_selection_sessions
                       SET business_lines = %s,
                           selected_keyword_ids = %s,
                           keywords_snapshot = %s,
                           status = 'business_lines_submitted',
                           updated_at = %s
                     WHERE token = %s
                    """,
                    (
                        json.dumps(business_lines, ensure_ascii=False),
                        json.dumps([]),
                        json.dumps(keywords, ensure_ascii=False),
                        now,
                        token,
                    ),
                )
                conn.commit()
                committed = True
                no_deliverable_committed = True
        elif session["status"] == "keywords_submitted":
            current_line_ids = _selected_business_line_ids(
                _safe_json(session.get("business_lines"), [])
            )
            current_keyword_ids = _safe_json(session.get("selected_keyword_ids"), [])
            if (
                current_line_ids == sorted(selected_business_line_ids)
                and current_keyword_ids == filtered_keyword_ids
            ):
                replayed = True
                conn.rollback()
            else:
                raise KeywordSelectionContractError(
                    "BUSINESS_LINE_SELECTION_ALREADY_COMMITTED",
                    "该选词链接已提交其他业务方向，请刷新查看最新结果。",
                )
        elif session["status"] not in ("selecting", "business_lines_submitted"):
            raise HTTPException(400, f"当前状态 {session['status']} 不允许提交业务线选择")
        else:
            keywords, review_override_count = _record_keyword_review_overrides(
                keywords,
                filtered_keyword_ids,
                actor_kind="customer_business_line_selection",
                actor_id=session.get("id"),
                reason="客户明确选择包含该关键词的业务方向",
                reviewed_at=now,
                brand_name=brand_name,
            )
            review_suggested = [
                keyword for keyword in keywords
                if isinstance(keyword, dict) and keyword.get("review_advisory")
            ]
            if review_suggested:
                logger.info(
                    "[报价复核提示] session %s 中 %d 个客户已选词建议人工复核，不做静默删除",
                    token,
                    len(review_suggested),
                )
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE keyword_selection_sessions
                   SET business_lines = %s,
                       selected_keyword_ids = %s,
                       keywords_snapshot = %s,
                       keywords_submitted_at = %s,
                       status = 'keywords_submitted',
                       updated_at = %s
                 WHERE token = %s
                """,
                (
                    json.dumps(business_lines, ensure_ascii=False),
                    json.dumps(filtered_keyword_ids),
                    json.dumps(keywords, ensure_ascii=False),
                    now,
                    now,
                    token,
                ),
            )
            conn.commit()
            committed = True
    except KeywordSelectionContractError as error:
        conn.rollback()
        raise _selection_contract_http_error(error) from error
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if committed and not no_deliverable_committed:
        try:
            _durable_selection_notice(
                brand_id=session["brand_id"], quote_id=session["quote_id"],
                event_type=NotificationEventType.BUSINESS_ACTION_REQUIRED,
                terminal_state="business_lines_submitted",
                status="客户已确认业务方向",
                summary=f"已确认 {len(selected_names)} 个业务方向，请继续计算报价。",
            )
        except Exception as e:
            logger.exception("业务方向通知写入 outbox 失败: %s", e)

    if all_excluded:
        # 🔴 这条**不能**复用上面那条:它的 summary 是「请继续计算报价」——
        #    没有任何词可以计价的时候推这句,等于让报价方去做一件做不了的事。
        #    只在**本次真落库**时推;重放不重推,读面从 outbox 取既有状态。
        if no_deliverable_committed:
            _lines = ("「" + "、".join(selected_names[:3]) + "」") if selected_names else ""
            if no_deliverable_reason == "no_keywords_for_lines":
                # 🔴 两种原因的报文必须分开:这一条不是"词被判知识类",
                #    是**这个方向下一条候选词都没有**。给报价方推错的原因,
                #    他会去找"被排除的词"而那里什么都没有,比不推更浪费时间。
                _summary = (
                    f"客户已选业务方向{_lines}，但该方向下暂无任何候选问法；"
                    "请补充该方向的关键词后重新发送选词链接。"
                )
            else:
                _summary = (
                    f"客户已选业务方向{_lines}，"
                    f"但该方向下 {len(delivery_exclusions)} 个问法都是知识/百科类，"
                    "不会促使 AI 推荐品牌，暂无可交付内容；请补充商业选型问法后重新发送。"
                )
            notify_sent = _push_no_deliverable_notice(
                brand_id=session["brand_id"], quote_id=session["quote_id"],
                summary=_summary,
            )
        else:
            notify_sent = _no_deliverable_notified(
                brand_id=session["brand_id"], quote_id=session["quote_id"],
            )

    return {
        "success": True,
        "replayed": replayed,
        "selected_count": len(selected_names),
        "selected_names": selected_names,
        "keywords_auto_selected": len(filtered_keyword_ids),
        "keywords_review_suggested": review_override_count,
        # [SSOT §3.3] 逐条展示被排除的知识词与原因,不静默缩减。
        "delivery_excluded_keywords": delivery_exclusions,
        # [#178] 零可交付不再是 400 —— 结构化告诉前端"卡在哪 + 往哪走"。
        "all_excluded": all_excluded,
        "reason": no_deliverable_reason if all_excluded else None,
        "status": "business_lines_submitted" if all_excluded else "keywords_submitted",
        "next_action": _no_deliverable_next_action(notify_sent) if all_excluded else None,
        "next_step": (
            ("补充该方向的关键词" if no_deliverable_reason == "no_keywords_for_lines"
             else "补充商业选型问法")
            if all_excluded else "计算报价"
        ),
    }


@router.post("/s/{token}/notify-no-deliverable")
async def notify_no_deliverable(token: str):
    """[#178] 客户点「让报价方补充」——补发那条「暂无可交付问法」通知。

    为什么还需要这个按钮:提交时已经推过一次,但那一次可能失败(品牌没有 owner、
    outbox 写入异常),失败时不阻断客户 ⇒ 必须留一条客户自己能按的补发路径。
    幂等由 outbox 的 `ON CONFLICT (event_key) DO NOTHING` 兜住:同一张报价单最多一条,
    按多少次都一样。

    🔴 先校状态再推:否则这就是"任何拿到链接的人都能给报价方发通知"的按钮。
    """
    session = get_session_by_token(token)
    if not session:
        raise HTTPException(404, "链接不存在")
    session = _check_expired(session)
    keywords = _safe_json(session.get("keywords_snapshot"), [])
    view = _no_deliverable_view(session, keywords, None)
    if not view:
        raise HTTPException(400, "当前不是「暂无可交付问法」状态，无需通知报价方")
    already_sent = bool((view.get("next_action") or {}).get("notify_sent"))
    notify_sent = already_sent or _push_no_deliverable_notice(
        brand_id=session["brand_id"], quote_id=session["quote_id"],
        summary=(
            f"客户请求补充商业选型问法:当前 {len(view['delivery_excluded_keywords'])} 个问法"
            "都是知识/百科类或需澄清,暂无可交付内容。"
        ),
    )
    return {"success": True, "notify_sent": notify_sent, "already_sent": already_sent}


@router.post("/s/{token}/withdraw-keywords")
async def withdraw_keywords(token: str):
    """客户撤回关键词选择"""
    session = get_session_by_token(token)
    if not session:
        raise HTTPException(404, "链接不存在")
    session = _check_expired(session)
    if session["status"] != "keywords_submitted":
        raise HTTPException(400, f"当前状态 {session['status']} 不允许撤回")

    update_session(token, status="selecting", keywords_submitted_at=None)

    try:
        _durable_selection_notice(
            brand_id=session["brand_id"], quote_id=session["quote_id"],
            event_type=NotificationEventType.BUSINESS_ACTION_REQUIRED,
            terminal_state="keywords_withdrawn",
            status="客户撤回了关键词选择",
            summary="客户正在重新选择，请稍后查看。",
        )
    except Exception as e:
        logger.exception("关键词撤回通知写入 outbox 失败: %s", e)

    return {"success": True}


@router.post("/s/{token}/add-keywords")
async def add_keywords(token: str, req: AddKeywordsRequest):
    """客户在报价阶段追加关键词"""
    session = get_session_by_token(token)
    if not session:
        raise HTTPException(404, "链接不存在")
    session = _check_expired(session)
    if session["status"] not in ("quoted", "adding_keywords"):
        raise HTTPException(400, f"当前状态 {session['status']} 不允许追加关键词")

    existing_pending = _safe_json(session.get("pending_keywords"), [])
    new_pending = list(set(existing_pending + req.keywords))

    update_session(
        token,
        status="adding_keywords",
        pending_keywords=json.dumps(new_pending, ensure_ascii=False),
    )

    try:
        _durable_selection_notice(
            brand_id=session["brand_id"], quote_id=session["quote_id"],
            event_type=NotificationEventType.BUSINESS_ACTION_REQUIRED,
            terminal_state="keywords_added",
            status="客户追加了关键词",
            summary=f"新增 {len(req.keywords)} 个关键词，请重新生成报价。",
        )
    except Exception as e:
        logger.exception("关键词追加通知写入 outbox 失败: %s", e)

    return {"success": True}


@router.post("/s/{token}/cancel-add-keywords")
async def cancel_add_keywords(token: str):
    """客户取消追加关键词，回退到 quoted 状态"""
    session = get_session_by_token(token)
    if not session:
        raise HTTPException(404, "链接不存在")
    if session["status"] != "adding_keywords":
        raise HTTPException(400, f"当前状态 {session['status']} 不允许此操作")
    update_session(token, status="quoted", pending_keywords=None)
    return {"success": True}


@router.post("/s/{token}/confirm-quote")
async def confirm_quote(token: str, req: ConfirmQuoteRequest):
    """客户确认报价（兼容平铺模式和主题包模式）

    [CTO-15.23 2026-05-06] race condition 兼容:
    客户在 quoted 状态打开报价页 · 选词时销售撤回报价(quoted → pricing_pending_review) ·
    客户提交时后端校验 != 'quoted' 报错"当前状态 pricing_pending_review 不允许确认报价"
    修法:也接受 pricing_pending_review · 客户能看到此页就证明销售之前发过 quoted ·
    价格已审核过 · 客户提交相当于自动接受当前价格 · 老板拍方案 C 双保险

    [CTO-15.23 2026-05-08 502 修复] 老板报客户点确认 502
    根因:WORKERS=1 + 5+ 个 sync psycopg2 调用 in async path · clusters_data 大 JSON 写入慢 ·
    阻塞 event loop · nginx upstream timeout · 间歇 502
    修法:全部 sync DB 调用 wrap asyncio.to_thread · 不阻塞 event loop
    参考:原社媒 agent(已随开源 E3 删除)大量使用过此模式
    """
    logger.info(f"[confirm-quote] 进入 token={token[:8]}.. tier={req.tier}")
    session = await asyncio.to_thread(get_session_by_token, token)
    if not session:
        raise HTTPException(404, "链接不存在")
    session = _check_expired(session)
    if session["status"] != "quoted":
        raise HTTPException(400, f"当前状态 {session['status']} 不允许确认报价")

    try:
        _normalize_unique_keyword_snapshot_ids(
            _safe_json(session.get("keywords_snapshot"), [])
        )
    except KeywordSelectionContractError as error:
        raise _selection_contract_http_error(error) from error

    from services.quote_pricing_snapshot import QuoteSnapshotError, get_frozen_snapshot
    frozen = await asyncio.to_thread(get_frozen_snapshot, int(session["quote_id"]))
    if not frozen or int(session.get("active_pricing_snapshot_id") or 0) != int(frozen["id"]):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "QUOTE_SNAPSHOT_REQUIRED",
                "message": "报价快照尚未冻结或已经变化，请让报价方重新审核发送。",
                "priority": "P1",
                "retryable": False,
                "recovery": "contact_quote_owner",
            },
        )

    pricing_data = _safe_json(frozen.get("pricing_snapshot"), {})
    clusters_data = _safe_json(frozen.get("clusters_snapshot"))

    # [防御型 GEO WP4 · 2026-08-21] 激活分流键 —— **只认服务端 snapshot schema**。
    #
    # `is_v2_enrolled` 读的是 `pricing_snapshot.delivery_plan.schema_version`,
    # 请求体里的 `mode` 一个字都不读(§3.2 L306:禁信客户端 mode)。
    # 非 enrolled(= 现役全部存量报价)时它是 None,`_commit_frozen_quote_confirmation`
    # 走的 SQL 与本次改动前逐字相同 —— 「旧入参旧行为不变」是这么保证的。
    from services.defensive_geo.commercial_milestones import is_v2_enrolled

    _accepted_hash = None
    if is_v2_enrolled(pricing_data):
        _accepted_hash = frozen.get("snapshot_hash")
        if not _accepted_hash:
            # enrolled 却拿不到 hash:宁可整笔拒掉,也不写一条没有身份的 accepted 事实。
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "QUOTE_SNAPSHOT_REQUIRED",
                    "message": "报价快照尚未冻结或已经变化，请让报价方重新审核发送。",
                    "priority": "P1",
                    "retryable": False,
                    "recovery": "contact_quote_owner",
                },
            )

    if clusters_data and clusters_data.get("clusters") and req.clusters_selection:
        # ===== 主题包模式：使用 clusters_selection 精确记录客户选择 =====
        # 构建 cluster_name → selection 映射
        sel_map = {cs.cluster_name: cs for cs in req.clusters_selection}
        total_price = 0.0
        core_count = 0
        covered_count = 0
        cluster_count = 0
        all_selected_ids = []

        for cluster in clusters_data["clusters"]:
            cs = sel_map.get(cluster.get("cluster_name", ""))
            if not cs:
                # 客户没选这个包
                cluster["is_selected"] = False
                for kw in cluster.get("core_keywords", []):
                    kw["is_selected"] = False
                # 清空覆盖词（未选包不赠送）
                cluster["confirmed_covered_count"] = 0
                continue

            cluster["is_selected"] = True
            cluster_count += 1
            selected_set = set(cs.selected_core_ids)
            all_selected_ids.extend(cs.selected_core_ids)

            # [CTO-15.23 2026-05-05] P0 修升级词丢失链路 bug
            # 老板演示:选 3 词其中 1 个从备选(covered)提升上来 → 写作大厅只见 2 个
            # 根因:前端 selected_core_ids 含升级词 id · 但服务端 cluster.core_keywords 只有原始核心词
            # 升级词在 cluster.covered_keywords 里 · 此循环找不到 → 永远不标 is_selected → mark_paid 漏写
            # 影响传导到:写作大厅 / 监测中心 / 报告 / 账单(全用 confirmed_keywords)
            # 修法:遍历前先把 selected_set 中匹配 covered_keywords 的搬到 core_keywords
            promoted_kws = []
            remaining_covered = []
            for ck in cluster.get("covered_keywords", []):
                if ck.get("id") in selected_set:
                    promoted_kws.append({
                        "id": ck["id"],
                        "keyword": ck["keyword"],
                        "is_selected": True,
                        "upgraded_from_covered": True,
                        "entry": ck.get("entry"),
                        "standard": ck.get("standard"),
                        "flagship": ck.get("flagship"),
                        "category": cluster.get("business_tag", "通用词"),
                        "intent": ck.get("intent", "informational"),
                        "funnel_stage": ck.get("funnel_stage", "awareness"),
                        # [§4.2] 升级词保留超红海标 → 否则 confirmed_keywords 漏标 · 达标计数漏排除
                        "super_red_ocean": bool(ck.get("super_red_ocean", False)),
                        # [完整修复 2026-06-13 High1] 升级词保留爆价/放飞标 → 否则 _compute_cluster_pricing 漏跳 · 价被算进套餐
                        "guarantee_unavailable": bool(ck.get("guarantee_unavailable", False)),
                    })
                else:
                    remaining_covered.append(ck)
            if promoted_kws:
                cluster["core_keywords"].extend(promoted_kws)
                cluster["covered_keywords"] = remaining_covered

            for kw in cluster.get("core_keywords", []):
                if kw.get("id") in selected_set:
                    kw["is_selected"] = True
                    tier_info = kw.get(req.tier, {})
                    total_price += tier_info.get("price", 0)
                    core_count += 1
                else:
                    kw["is_selected"] = False

            # 按价格比例缩减覆盖词：前端已算好，后端验证
            total_core_price = sum(kw.get(req.tier, {}).get("price", 0) for kw in cluster.get("core_keywords", []))
            selected_core_price = sum(kw.get(req.tier, {}).get("price", 0) for kw in cluster.get("core_keywords", []) if kw.get("id") in selected_set)
            max_covered = len(cluster.get("covered_keywords", []))
            ratio = selected_core_price / total_core_price if total_core_price > 0 else 0
            verified_covered = min(cs.covered_count, math.ceil(max_covered * ratio))
            cluster["confirmed_covered_count"] = verified_covered
            covered_count += verified_covered

        try:
            await asyncio.to_thread(
                _commit_frozen_quote_confirmation,
                token=token,
                expected_snapshot_id=int(frozen["id"]),
                selected_tier=req.tier,
                final_keyword_ids=json.dumps(all_selected_ids),
                confirmed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                confirmed_total_price=total_price,
                clusters_data=json.dumps(clusters_data, ensure_ascii=False),
                accepted_snapshot_hash=_accepted_hash,
                accepted_request=(
                    _acceptance_evidence(
                        token=token, tier=req.tier,
                        selected_keyword_ids=all_selected_ids,
                        clusters_selection=req.clusters_selection,
                        quote_id=session.get("quote_id"),
                        brand_id=session.get("brand_id"))
                    if _accepted_hash else None),
            )
        except QuoteSnapshotError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.as_detail()) from exc

        # [B1 2026-06-05] 回写 quotes.monthly_price = 已选核心词合计(不再是生成期全 core headline)
        await asyncio.to_thread(_sync_quote_monthly_price, session.get("quote_id"), total_price)

        # [CTO-15.23 2026-05-08 监测乱入修] 同步标记客户实际选的词为 is_monitored=TRUE
        # 老板报客户截图:报价 4 词 · 实际选 1 词 · 但监测列表显示 4 词全跑(浪费 Kimi)
        # 修法:此处把 all_selected_ids 里的 confirmed_keywords 标 is_monitored=TRUE
        # monitoring SQL (db/monitoring_db.py:1519) 加 WHERE 过滤未选词
        await asyncio.to_thread(
            _mark_keywords_monitored,
            session["quote_id"],
            all_selected_ids,
            keywords_snapshot=session.get("keywords_snapshot"),
            pricing_data=session.get("pricing_data"),
            clusters_data=clusters_data,
        )

        brand_name = await asyncio.to_thread(_get_brand_name, session["brand_id"])
        tier_label = TIER_CONFIG[req.tier]["label"]
        try:
            await asyncio.to_thread(
                _durable_selection_notice,
                brand_id=session["brand_id"],
                quote_id=session["quote_id"],
                event_type=NotificationEventType.BUSINESS_ACTION_REQUIRED,
                terminal_state="quote_confirmed",
                status="客户已确认报价",
                summary=f"{tier_label}，{cluster_count} 个主题包、{core_count} 个核心词，请继续核实订单。",
            )
        except Exception as e:
            logger.exception("报价确认通知写入 outbox 失败: %s", e)

        logger.info(f"[confirm-quote] 完成 token={token[:8]}.. 主题包模式 cluster={cluster_count}")
        return {"success": True}

    # ===== 平铺模式（原有���辑不变）=====
    pricing_data = _sync_pricing_tiers_from_config(pricing_data)  # r12: 实时覆盖老 cached ai_probability
    total_price = 0.0
    if pricing_data and "keywords" in pricing_data:
        for kw in pricing_data["keywords"]:
            if kw.get("id") in req.selected_keyword_ids:
                tier_info = kw.get(req.tier, {})
                total_price += tier_info.get("price", 0)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        await asyncio.to_thread(
            _commit_frozen_quote_confirmation,
            token=token,
            expected_snapshot_id=int(frozen["id"]),
            selected_tier=req.tier,
            final_keyword_ids=json.dumps(req.selected_keyword_ids),
            confirmed_at=now,
            confirmed_total_price=total_price,
            accepted_snapshot_hash=_accepted_hash,
            accepted_request=(
                _acceptance_evidence(
                    token=token, tier=req.tier,
                    selected_keyword_ids=req.selected_keyword_ids,
                    quote_id=session.get("quote_id"),
                    brand_id=session.get("brand_id"))
                if _accepted_hash else None),
        )
    except QuoteSnapshotError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.as_detail()) from exc

    # [B1 2026-06-05] 回写 quotes.monthly_price = 已选核心词合计(平铺模式 · 与 confirmed_total_price 同口径)
    await asyncio.to_thread(_sync_quote_monthly_price, session.get("quote_id"), total_price)

    # [CTO-15.23 2026-05-08 监测乱入修] 平铺模式同步标记 is_monitored
    await asyncio.to_thread(
        _mark_keywords_monitored,
        session["quote_id"],
        req.selected_keyword_ids,
        keywords_snapshot=session.get("keywords_snapshot"),
        pricing_data=session.get("pricing_data"),
        clusters_data=session.get("clusters_data"),
    )

    brand_name = await asyncio.to_thread(_get_brand_name, session["brand_id"])
    tier_label = TIER_CONFIG[req.tier]["label"]
    try:
        await asyncio.to_thread(
            _durable_selection_notice,
            brand_id=session["brand_id"],
            quote_id=session["quote_id"],
            event_type=NotificationEventType.BUSINESS_ACTION_REQUIRED,
            terminal_state="quote_confirmed",
            status="客户已确认报价",
            summary=f"{tier_label}，{len(req.selected_keyword_ids)} 个关键词，请继续核实订单。",
        )
    except Exception as e:
        logger.exception("报价确认通知写入 outbox 失败: %s", e)

    logger.info(f"[confirm-quote] 完成 token={token[:8]}.. 平铺模式")
    return {"success": True}


@router.post("/s/{token}/events")
async def upload_events(token: str, req: EventsRequest, request: Request):
    """批量上报行为事件"""
    session = get_session_by_token(token)
    if not session:
        raise HTTPException(404, "链接不存在")

    client_ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "")
    user_agent = request.headers.get("user-agent", "")

    save_interaction_events(session["id"], req.events, client_ip, user_agent)
    return {"success": True}


# ================================================================
#  销售端 API（需登录，/api/keyword-selection/）
# ================================================================

class KeywordSnapshotItem(BaseModel):
    keyword: str
    category: str = "general"
    source: str = ""
    recommendation_reason: str = ""
    geo_recommend: Optional[bool] = None
    scope_match: Optional[bool] = None
    default_selected: Optional[bool] = None
    rejection_reason: str = ""
    review_override_reason: str = ""
    review_version: str = "buyer-intent-review-v1"
    # [SSOT geo-commercial-intent-governance-v1.0] 统一商业交付资格合同字段
    commercial_delivery_eligible: Optional[bool] = None
    intent_type: Optional[str] = None
    policy_version: Optional[str] = None


class CreateSessionRequest(BaseModel):
    quote_id: Optional[int] = None
    # 以下字段用于从「筛选关键词」步骤直接创建（尚无 quote_id）
    brand_id: Optional[int] = None
    brand_name: Optional[str] = None
    industry: Optional[str] = ""
    city: Optional[str] = ""
    keywords: Optional[List[KeywordSnapshotItem]] = None


@router.get("/keyword-selection/list")
async def list_sessions(request: Request, status: str = None, limit: int = 50):
    """
    列出所有选词会话（销售端概览）
    RBAC: 非管理员只返回分配品牌的会话

    可选参数:
    - status: 按状态筛选 (selecting, keywords_submitted, pricing_pending_review, quoted, confirmed, pending_payment, active)
    - limit: 返回条数（默认50）
    """
    from auth.brand_access import get_user_brand_filter
    sessions = list_selection_sessions(status_filter=status, limit=limit)
    # RBAC: 非管理员只看到自己分配品牌的会话
    allowed = get_user_brand_filter(request)
    if allowed is not None:
        sessions = [s for s in sessions if s.get("brand_id") in allowed]
    result = []
    for s in sessions:
        result.append({
            "token": s["token"],
            "quote_id": s["quote_id"],
            "brand_name": s.get("brand_name", ""),
            "industry": s.get("industry", ""),
            "city": s.get("city", ""),
            "status": s["status"],
            "selected_count": s.get("selected_count", 0),
            "selected_tier": s.get("selected_tier"),
            "confirmed_total_price": s.get("confirmed_total_price"),
            "final_price": s.get("final_price"),
            "expires_at": s.get("expires_at"),
            "created_at": s.get("created_at"),
            "updated_at": s.get("updated_at"),
            "keywords_submitted_at": s.get("keywords_submitted_at"),
            "confirmed_at": s.get("confirmed_at"),
        })
    return {"sessions": result, "total": len(result)}


@router.post("/keyword-selection/create")
async def create_selection_link(req: CreateSessionRequest, request: Request):
    """
    生成选词链接。

    两种调用方式：
    1. 传 quote_id → 从已有报价单读取关键词（幂等）
    2. 传 brand_id + brand_name + keywords → 自动创建草稿报价单再生成链接
    """
    from db.diagnosis_db import save_quote, get_or_create_brand
    from auth.brand_access import require_brand_access, require_quote_access

    request_user = getattr(request.state, "user", None)
    created_by = request_user.get("user_id") if request_user else None

    # ---- 方式 1：已有 quote_id ----
    if req.quote_id:
        # [GEO-R1-CAN-137] 校验调用者对该报价单的归属 · 全局安全网不读 POST body,
        #   否则任意登录用户传他人 quote_id 即可生成/领取选词 token(下游越权链的源头)
        # [对抗审核订正 R1-CAN-137] 本入口是越权链源头(创建/领取 token 即成 owner),
        # NULL-brand quote 必须 fail-closed,否则攻击者传他人 NULL-brand quote_id 仍可劫持。
        require_quote_access(request, req.quote_id, allow_null=False)
        existing = get_session_by_quote(req.quote_id)
        if existing:
            return {
                "token": existing["token"],
                "url": f"/s/{existing['token']}",
                "expires_at": existing["expires_at"],
                "status": existing["status"],
                "already_exists": True,
            }

        quote = get_quote(req.quote_id)
        if not quote:
            raise HTTPException(404, "报价单不存在")

        keywords_raw = get_keywords_by_quote(req.quote_id)
        keywords_snapshot = []
        for i, kw in enumerate(keywords_raw):
            # [DRIFT-D] 统一引擎判定交付资格,前端展示守卫即生效(§17.1)。
            _cde = _delivery_eligible_field(
                kw.get("keyword", ""),
                intent_hint=kw.get("intent"),
                existing=kw.get("commercial_delivery_eligible"),
            )
            keywords_snapshot.append({
                "id": kw.get("id", i + 1),
                "keyword": kw.get("keyword", ""),
                "category": kw.get("category", "general"),
                "category_label": _category_label(kw.get("category", "general")),
                "difficulty": _difficulty_level(kw),
                # [报价意图闸 2026-08-04] 原为写死 True —— 这正是会话 194 的 11 个词
                #   (含 6 个"…就业率真实吗"这类知识题)`recommended` 全为 true 的原因:
                #   选词页把它们当推荐词呈现 → 被勾选 → 进报价 → 出价 ¥14,677。
                #   不进付费交付的词不再标"推荐"(词照留、可人工勾选,只是不再由系统背书)。
                "recommended": bool(_cde),
                "recommendation_reason": _recommendation_reason(kw),
                "intent": kw.get("intent", "informational"),
                "commercial_delivery_eligible": _cde,
            })

        brand_id = quote.get("brand_id", 0)
        quote_id = req.quote_id

    # ---- 方式 2：从筛选关键词步骤直接创建 ----
    elif req.keywords and req.brand_name:
        # 确保品牌存在
        if req.brand_id:
            # [GEO-R1-CAN-136 / GEO-R7-CAN-013] 校验调用者对传入 brand_id 的归属 ·
            #   全局安全网不读 POST body,body 里的外租户 brand_id 会绕过 → 必须显式校验
            require_brand_access(request, req.brand_id)
            brand_id = req.brand_id
        else:
            brand_id = get_or_create_brand(
                req.brand_name,
                req.industry,
                owner_user_id=created_by,
            )

        # 创建草稿报价单
        quote_id = save_quote({
            "brand_id": brand_id,
            "brand_name": req.brand_name,
            "industry": req.industry or "",
            "city": req.city or "",
            "tier": "standard",
            "target_share": 0.20,
            "total_keywords": len(req.keywords),
            "total_articles": 0,
            "monthly_price": 0,
            "status": "draft",
            "markdown": "",
        })

        # 检查是否刚创建的 quote 已有 session（理论上不会，但防御性检查）
        existing = get_session_by_quote(quote_id)
        if existing:
            return {
                "token": existing["token"],
                "url": f"/s/{existing['token']}",
                "expires_at": existing["expires_at"],
                "status": existing["status"],
                "quote_id": quote_id,
                "already_exists": True,
            }

        # 构建快照
        keywords_snapshot = []
        for i, kw in enumerate(req.keywords):
            default_selected = (
                kw.default_selected
                if kw.default_selected is not None
                else kw.geo_recommend is not False and kw.scope_match is not False
            )
            # [P0-4] 四条硬边界(法律/资金/越权/数据完整性)物理禁选,其余可人工放行
            from services.commercial_query_policy import hard_block_reason as _hbr
            _hard_block_code = _hbr(kw.keyword)
            _operator_released = bool(
                str(kw.review_override_reason or "").strip()
            ) and not _hard_block_code
            # [SSOT v1.0 · 2026-07-26 P0-4 修订] 统一合同字段透传。
            # 引擎判不进交付的词默认 False;操作员显式放行(review_override_reason
            # 非空)且不触四条硬边界 → True,并把放行理由一并冻进快照供提交侧复核。
            # [报价意图闸 2026-08-04 修] 原兜底是 `kw.geo_recommend is not False` ——
            #   上游没显式传 commercial_delivery_eligible 时它**根本不问引擎**,
            #   只要 geo_recommend 不是 False,知识题也会被盖成"可交付"。
            #   改为回落到唯一引擎判定(操作员显式放行仍然优先,人工权力不被削)。
            if kw.commercial_delivery_eligible is not None:
                _cde = bool(kw.commercial_delivery_eligible)
            elif _operator_released:
                _cde = True
            else:
                _cde = _delivery_eligible_field(kw.keyword, intent_hint=kw.intent_type)
            snapshot_item = {
                "id": i + 1,
                "keyword": kw.keyword,
                "category": kw.category,
                "category_label": _category_label(kw.category),
                "difficulty": 3,
                # [报价意图闸 2026-08-04] 不进付费交付的词不标"推荐"(词照留 · 可人工勾选)
                "recommended": bool(default_selected and _cde),
                "geo_recommend": kw.geo_recommend,
                "scope_match": kw.scope_match,
                "default_selected": kw.default_selected,
                "rejection_reason": kw.rejection_reason,
                "recommendation_reason": kw.recommendation_reason or _recommendation_reason({"keyword": kw.keyword}),
                "commercial_delivery_eligible": _cde,
                "intent_type": kw.intent_type,
                "policy_version": kw.policy_version or _COMMERCIAL_POLICY_VERSION,
                "review_override_reason": kw.review_override_reason or "",
                "human_override_allowed": not _hard_block_code,
                "hard_block": bool(_hard_block_code),
                "hard_block_reason": _hard_block_code or "",
            }
            if (
                kw.geo_recommend is False
                or kw.scope_match is False
                or default_selected is False
            ):
                review_event = {
                    "policy_version": kw.review_version or "buyer-intent-review-v1",
                    "decision": "human_continue",
                    "actor_kind": "sales_operator_keyword_selection",
                    "actor_id": created_by,
                    "reason": (
                        kw.review_override_reason
                        or "操作员在候选词页面明确勾选并决定继续"
                    ),
                    "original_issue": (
                        kw.rejection_reason
                        or "系统建议复核该关键词的品牌推荐意图"
                    ),
                    "reviewed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                snapshot_item["review_advisory"] = review_event
                snapshot_item["review_advisory_history"] = [dict(review_event)]
            keywords_snapshot.append(snapshot_item)
    else:
        raise HTTPException(400, "请提供 quote_id 或 (brand_name + keywords)")

    token = secrets.token_urlsafe(12)  # ~16 chars
    expires_at = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    # 提取业务线（异步，但不阻塞创建链接）
    business_lines_json = None
    # [CTO-15.23 2026-05-11 P0#2] 关键词→业务线分配 · 防"客户已确认 44 个"误导
    business_lines = []
    bl_brand = ""
    bl_industry = ""
    try:
        from tools.keyword_cluster import extract_business_lines, assign_keywords_to_business_lines
        # 从报价单获取行业信息
        quote_info = get_quote(quote_id) if quote_id else {}
        bl_industry = quote_info.get("industry", "") or req.industry or ""
        bl_city = quote_info.get("city", "") or req.city or ""
        bl_brand = quote_info.get("brand_name", "") or req.brand_name or ""
        bl_scope = ""  # business_scope 不在 quote 中，用关键词推断
        # [CTO-15.23 2026-05-05 P0 fix] 删除 [:5] 截断
        # 老板原 case: 输入 9 词(3城×3类型电梯) · 旧版只送前 5 个给 LLM,
        # 后 4 个(含"惠州别墅电梯"等)被丢 · LLM 输出业务线漏"别墅电梯"
        # 客户在选词页看不到别墅电梯 → 漏选 → 销售流失
        bl_core_keywords = [kw.get("keyword", "") for kw in keywords_snapshot] if keywords_snapshot else []

        business_lines = await extract_business_lines(
            brand_name=bl_brand,
            industry=bl_industry,
            city=bl_city,
            core_keywords=bl_core_keywords,
            business_scope=bl_scope,
        )

        # [P0#2] 关键词→业务线分配 · 写进 keywords_snapshot
        if business_lines and keywords_snapshot:
            try:
                kw_bl_mapping = await assign_keywords_to_business_lines(
                    keywords=keywords_snapshot,
                    business_lines=business_lines,
                    brand_name=bl_brand,
                    industry=bl_industry,
                )
                for kw in keywords_snapshot:
                    bl_id = kw_bl_mapping.get(kw.get("id"))
                    if bl_id is not None:
                        kw["business_line_id"] = bl_id
            except Exception as e:
                logger.warning(f"关键词业务线分配失败(不影响创建链接): {e}")

        business_lines_json = json.dumps(business_lines, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"业务线提取失败（不影响创建链接）: {e}")

    try:
        session_id = create_selection_session(
            token=token,
            quote_id=quote_id,
            brand_id=brand_id,
            created_by=created_by,
            keywords_snapshot=json.dumps(keywords_snapshot, ensure_ascii=False),
            expires_at=expires_at,
        )
    except ValueError as exc:
        # [P1-A 2026-07-30] 并发/重复提交同一 quote_id 输给唯一约束 →
        #   回吐赢家那条 session(和前置 get_session_by_quote 命中时同一个响应形状),
        #   而不是把 UniqueViolation 漏成 500。本入口契约本来就是幂等的。
        if str(exc) == "SESSION_EXISTS_FOR_QUOTE":
            winner = get_session_by_quote(quote_id)
            if not winner:
                raise
            logger.info(
                "[SelectionCreateRaceCollapsed] quote_id=%s winner_token=%s "
                "loser_token_discarded=%s",
                quote_id, winner["token"], token,
            )
            return {
                "token": winner["token"],
                "url": f"/s/{winner['token']}",
                "expires_at": winner["expires_at"],
                "status": winner["status"],
                "quote_id": quote_id,
                "already_exists": True,
            }
        if str(exc) != "QUOTE_ARCHIVED_OR_SCOPE_CHANGED":
            raise
        raise HTTPException(
            status_code=409,
            detail={
                "code": "QUOTE_ARCHIVED_OR_SCOPE_CHANGED",
                "message": "报价已归档或客户归属已变化，未创建公开链接。",
                "priority": "P1",
                "retryable": False,
                "recovery": "refresh_quote_list",
            },
        ) from exc

    # 保存业务线到 session
    if business_lines_json:
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE keyword_selection_sessions SET business_lines = %s WHERE id = %s",
                (business_lines_json, session_id)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"业务线保存失败: {e}")

    return {
        "token": token,
        "url": f"/s/{token}",
        "expires_at": expires_at,
        "session_id": session_id,
        "quote_id": quote_id,
    }


@router.post("/keyword-selection/{token}/extend")
async def extend_session(token: str, request: Request):
    """延期 7 天"""
    # 🔴 [工单 V5-A · Codex fix-of-fix2 P1-4] **对象归属校验**,在任何写入之前。
    #    改动前这个端点连 `Request` 都没有:全局 JWT + quote 模块权限只说明
    #    「这个人能用报价模块」,证明不了「这个 session 是他的」。
    #    客户分享链接里的 token 是明文的,任何拿到它的 quote-enabled 登录用户
    #    都能把别人租户的会话延期(并把 expired 会话拉回 selecting 重新开放选词)。
    #    本文件里以 `{token}` 为路径参数的写端点共 19 个:7 个是 `/s/{token}` C 端
    #    token-only 页面(客户不登录,天然没有 owner 可校),另外 12 个是销售端 ——
    #    其中 11 个早就在调这个函数,唯独 extend 漏了。配了一条接线 census 钉住这个分母。
    _require_session_owner_access(request, token)
    # 🔴 [工单 V4-A · Codex fix-of-fix P1-2b] 走**原子**原语:单事务 + 锁行 + 条件更新。
    #    上一版是三条独立连接上的读→算→盲写(`UPDATE … WHERE token=%s` 不带状态条件)。
    #    Codex 真 PG16 反例:读到 expired 之后并发推进成 confirmed,
    #    这里仍把状态覆盖回 selecting —— 已确认的报价被重新打开。
    #    防御 GEO 的「重签报价单链接」也调同一个原语:同一谓词只有一份实现。
    from db.diagnosis_db import extend_selection_session_atomically

    result = extend_selection_session_atomically(token, days=7)
    if not result.get("ok"):
        if result.get("reason") == "not_found":
            raise HTTPException(404, "会话不存在")
        # 状态机方向锁:confirmed 及之后不可回退。给 typed 拒绝而不是假装延成功。
        raise HTTPException(
            409, f"这个报价已经推进到「{result.get('status')}」,不能再延期回选词状态")
    return {"new_expires_at": result["expires_at"], "status": result["status"]}


@router.post("/keyword-selection/{token}/generate-quote")
async def generate_quote_for_session(token: str, request: Request, mode: str = "auto"):
    """
    销售生成报价：调用定价引擎，结果写入 session。
    并发限制 Semaphore(5)，防止 LLM 成本爆炸。
    """
    await _quote_gen_semaphore.acquire()
    try:
        # [GEO-R1-CAN-112] owner 校验 · 防越权用他人 token 触发 LLM 重定价(且会把攻击者 markup/成本系数写入受害会话)
        _require_session_owner_access(request, token)
        user = getattr(request.state, "user", None) or {}
        return await _generate_quote_impl(token, mode, user_id=user.get("user_id"))
    finally:
        _quote_gen_semaphore.release()


async def _generate_quote_impl(token: str, mode: str = "auto", user_id: Optional[int] = None):
    session = get_session_by_token(token)
    if not session:
        raise HTTPException(404, "会话不存在")

    if session["status"] not in ("keywords_submitted", "adding_keywords", "pricing_pending_review", "quoted"):
        raise HTTPException(400, f"当前状态 {session['status']} 不允许生成报价")

    try:
        normalized_keywords_snapshot = _normalize_unique_keyword_snapshot_ids(
            _safe_json(session.get("keywords_snapshot"), [])
        )
    except KeywordSelectionContractError as error:
        raise _selection_contract_http_error(error) from error
    session = dict(session)
    session["keywords_snapshot"] = normalized_keywords_snapshot

    # [报价中心·2026-06-08 老板拍板] 给终端客户报价永远用【本人】系数/成本 · 绝不继承上级(上级只赚算力/进货链路·不干预客户报价)
    from services.quote_pricing_preferences import (
        get_quote_markup_for_quote_viewer, get_cost_per_article_for_quote_viewer,
        get_procurement_cost_multiplier_for_quote_viewer,
        get_cumulative_media_procurement_multiplier, is_default_quote_pricing,
    )
    quote_markup_ratio = get_quote_markup_for_quote_viewer(user_id)  # 本人系数实际值(含1.0)·永不继承上级
    # [P1-1] markup_override 传【实际值(含 1.0)】· 不传 override-None,防引擎回退后台 system quote_markup_ratio
    quote_markup_override = quote_markup_ratio
    quote_cost_override = get_cost_per_article_for_quote_viewer(user_id)  # 本人自设单篇成本(None=走系统动态)
    # [v2.1 2026-06-11 老板拍] 进货成本倍率:扫码下级的媒体资源进货价 = 平台价 × 上级 SKU 系数
    #   "系统自动估"成本必须乘它(否则下级按平台价算报价 < 真实进货成本 = 亏)· 自设成本不乘(填的已是真实成本)
    # [P0-A 2026-06-13 correction 3] flag 开 → 累计上级倍率(多级邀请链已加价媒体成本逐层传递·
    #   1 级链退化 == 直属上级倍率=现状等价);flag 关 → 现状直属上级倍率(0 变化)。只作用媒体成本层,不碰报价中心。
    try:
        from tools.llm_pricing_flag import is_cost_snapshot_enabled
        _p0a_cost_on = is_cost_snapshot_enabled()
    except Exception:
        _p0a_cost_on = False
    quote_cost_multiplier = (get_cumulative_media_procurement_multiplier(user_id) if _p0a_cost_on
                             else get_procurement_cost_multiplier_for_quote_viewer(user_id))
    quote_allow_cache = is_default_quote_pricing(user_id)  # [价格锁回归修 2026-06-10] 仅默认口径写共享缓存(v2.1 已含倍率判定 · flat+cluster 共用)
    if _p0a_cost_on and abs(float(quote_cost_multiplier or 1.0) - 1.0) > 0.001:
        quote_allow_cache = False  # [rule §错误4] 含(累计)上级倍率 → 不写全局共享缓存(防污染他人)

    # auto模式：从系统设置读取默认报价模式
    effective_mode = mode
    if mode == "auto":
        try:
            from config.settings_manager import get_current_settings
            effective_mode = "cluster" if get_current_settings().quote_cluster_mode else "flat"
        except Exception:
            effective_mode = "cluster"  # 设置读取失败默认用主题包模式

    if effective_mode == "cluster":
        return await _generate_quote_cluster_mode(
            token,
            session,
            quote_markup_ratio=quote_markup_ratio,
            quote_markup_override=quote_markup_override,
            cost_per_article_override=quote_cost_override,
            allow_cache_write=quote_allow_cache,
            cost_multiplier=quote_cost_multiplier,
        )
    # 否则走原有平铺逻辑

    # 收集需要报价的关键词
    keywords_snapshot = normalized_keywords_snapshot
    selected_ids = _safe_json(session.get("selected_keyword_ids"), [])
    custom_keywords = _safe_json(session.get("custom_keywords"), [])
    pending_keywords = _safe_json(session.get("pending_keywords"), [])

    # 从快照中筛选客户选择的关键词
    selected_kw_texts = []
    kw_id_map = {}
    for kw in keywords_snapshot:
        if kw["id"] in selected_ids:
            selected_kw_texts.append(kw["keyword"])
            kw_id_map[kw["keyword"]] = kw["id"]

    # 加入自定义词和追加词（去重，保留顺序）
    all_keywords = list(dict.fromkeys(selected_kw_texts + custom_keywords + pending_keywords))

    # 为自定义词和追加词分配临时 ID
    next_id = max((kw.get("id", 0) for kw in keywords_snapshot), default=0) + 1
    for kw_text in custom_keywords + pending_keywords:
        if kw_text not in kw_id_map:
            kw_id_map[kw_text] = next_id
            # 也添加到快照中
            keywords_snapshot.append({
                "id": next_id,
                "keyword": kw_text,
                "category": "custom",
                "category_label": "自定义",
                "difficulty": 3,
                "recommended": False,
                "recommendation_reason": "客户自主添加",
            })
            if next_id not in selected_ids:
                selected_ids.append(next_id)
            next_id += 1

    if not all_keywords:
        raise HTTPException(400, "没有需要报价的关键词")

    # 获取品牌信息 —— 统一使用 quote 记录的 brand_name（与线下报价共享缓存key）
    quote = get_quote(session["quote_id"])
    brand_name = (quote.get("brand_name") or _get_brand_name(session["brand_id"])) if quote else _get_brand_name(session["brand_id"])
    industry = quote.get("industry", "") if quote else ""
    city = quote.get("city", "") if quote else ""

    # 调用定价引擎
    from tools.batch_pricing import generate_batch_quote
    quote_data, markdown = await generate_batch_quote(
        keywords=all_keywords,
        brand_name=brand_name,
        target_share=0.20,
        industry=industry,
        city=city,
        markup_override=quote_markup_override,
        cost_per_article_override=quote_cost_override,  # [M2] 服务商自设单篇成本 A完全覆盖
        allow_cache_write=quote_allow_cache,  # [价格锁回归修 2026-06-10] 仅默认口径写共享缓存
        cost_multiplier=quote_cost_multiplier,  # [v2.1] 下级进货倍率(上级 SKU 系数)
        brand_id=session["brand_id"],  # [P0-D 2026-06-14] 信任资产按 brand_id 取(权威 id·不按名猜)
    )

    scored = quote_data.get("keywords", [])

    # [Codex 复诊 2026-06-11 · 真实第一] 全部断供 → 写 session 前直接失败(人话 · 不落 0 价报价)
    if quote_data.get("unavailable_keywords") and not scored:
        raise HTTPException(status_code=503, detail="网络繁忙,关键词暂时无法估价,请稍后重试")

    # [报价意图闸 2026-08-04] 全部词都不适合投放 → 给动作,不落 ¥0 报价单
    #   (上面那条 503 要求断供词非空才响,意图闸剔空时它不会响 · 见 helper docstring)
    _err = _nothing_quotable_error(scored, quote_data, keywords_snapshot, brand_name=brand_name)
    if _err:
        raise _err

    # 保留旧 pricing_data 中的审计结果（审计过的词不丢失）
    old_audit_map: dict = {}
    if session.get("pricing_data"):
        try:
            old_pd = _safe_json(session["pricing_data"], {})
            for okw in old_pd.get("keywords", []):
                if okw.get("audit_status"):
                    old_audit_map[okw["keyword"]] = {
                        "audit_status": okw["audit_status"],
                        "audit_note": okw.get("audit_note", ""),
                        "price_before_audit": okw.get("price_before_audit"),
                    }
        except (json.JSONDecodeError, TypeError):
            pass

    # 构建 pricing_data JSON
    pricing_data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tiers": {},
        "keywords": [],
        # [2026-06-11 老板拍 · 真实第一] 数据断供词(双 LLM 全挂/metaso 全挂)不出价不兜底
        #   人话 reason · 前端提示稍后重试(断供词未写缓存 · 重新算价自然全链路重试)
        "unavailable_keywords": quote_data.get("unavailable_keywords", []),
    }

    for tier_key, tier_cfg in TIER_CONFIG.items():
        total_price = 0
        total_articles = 0
        for s in scored:
            # [完整修复 2026-06-13 返修] 爆价/放飞词(guarantee_unavailable)不进任何套餐总价 ——
            #   含此处预算总价(下游喂 check_brand_continuity 连续性提醒)· 与最终 _core_kws 总价(1730)同口径。
            #   flags 全关时 guarantee_unavailable 恒 False → 0 变化;super_red_ocean 维持原预算口径(不在本次范围·防回归连续性基线)。
            if s.get("guarantee_unavailable"):
                continue
            total_price += int(s.get(f"{tier_key}_price", 0))
            total_articles += int(s.get(f"{tier_key}_articles", 0))
        pricing_data["tiers"][tier_key] = {
            "label": tier_cfg["label"],
            "target_share": tier_cfg["target_share"],
            "ai_probability": tier_cfg["ai_probability"],
            "stars": tier_cfg["stars"],
            "total_price": total_price,
            "total_articles": total_articles,
        }

    # [v2.1 2026-06-11 老板拍"影响体验就接"] 同 brand 价格连续性 ±25% 警示(flag-only · 不动价):
    #   这次标准档总价 vs 该品牌最近 90 天已成交均价 · 偏差超 ±25% → 提醒"比上次贵/便宜 N% · 发前确认"
    #   服务"价格稳定感 ≥ 精确度"铁律(同客户两次报价差太多 = 感觉乱算)· fail-soft 不阻塞报价
    try:
        from tools.brand_price_continuity import check_brand_continuity
        _std_total = pricing_data["tiers"].get("standard", {}).get("total_price", 0)
        _bc = await check_brand_continuity(session.get("brand_id"), float(_std_total or 0))
        if _bc.get("ok") and _bc.get("needs_review"):
            _dir = "高" if _bc.get("deviation_pct", 0) > 0 else "低"
            pricing_data["brand_continuity"] = {
                "needs_review": True,
                "last_avg": _bc.get("last_avg", 0),
                "deviation_pct": _bc.get("deviation_pct", 0.0),
                "hint": (f"这单标准档总价比该客户最近成交均价(¥{_bc.get('last_avg', 0):,})"
                         f"偏{_dir} {abs(_bc.get('deviation_pct', 0)):.0f}% · 发给客户前确认一下"),
            }
    except Exception:
        pass  # 连续性提醒失败不影响报价

    # [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 锁期跟【这次到底有没有写缓存】挂钩。
    #   原来这里是无条件 `now() + 7`,和上游写没写缓存毫无关系 —— 上游因为正当理由
    #   (P0-D 信任快照 per-brand 价 / 自设成本 / 进货倍率 / 爆价护栏逐词剥离)跳过了锁,
    #   下游照样按锁在承诺,客户拿到「价格锁定至 X」三天后再算是另一个数。
    #   现在只渲染定价引擎给出的**逐词**事实;不在 map 里的词不显示锁期。
    _lock_map = quote_data.get(_LOCK_MAP_KEY) or {}
    for s in scored:
        kw_text = s["keyword"]
        kw_id = kw_id_map.get(kw_text, 0)
        # 强制最低价格（全国词用350下限，地域词用120下限，与线下报价一致）
        kw_min_price = MIN_KEYWORD_PRICE_NATIONAL if s.get("is_broad") else MIN_KEYWORD_PRICE
        _sro = bool(s.get("super_red_ocean", False))
        # [完整修复 2026-06-13 High1] 爆价/放飞词(guarantee_unavailable):客户端三档价归零(剥保证价·前端按标渲染人工核)·
        #   参考价保留在内部 scored 数据(batch_pricing guarantee_quotable=False)供代理人工核 · 三 flag 全关时恒 False = 0 变化
        _guar_unavail = bool(s.get("guarantee_unavailable", False))
        # [任务3 2026-06-08] LLM-first 信息型 should_quote=False · 公式引擎无此字段默认 True(不受影响)
        _no_quote = _sro or (s.get("should_quote", True) is False) or _guar_unavail
        # [§4.2] 超红海(需单独报价)/信息型(不报价)/爆价放飞(参考价人工核)词不出保证价 → JSON 三档价归零(前端按标渲染 · 不裸 ¥0)
        entry_price = 0 if _no_quote else max(int(s.get("entry_price", 0)), kw_min_price)
        standard_price = 0 if _no_quote else max(int(s.get("standard_price", 0)), kw_min_price)
        flagship_price = 0 if _no_quote else max(int(s.get("flagship_price", 0)), kw_min_price)
        pricing_data["keywords"].append({
            "id": kw_id,
            "keyword": kw_text,
            "category_label": _find_category_label(kw_text, keywords_snapshot),
            "recommendation_reason": _find_recommendation_reason(kw_text, keywords_snapshot),
            "entry": {"price": entry_price, "articles": int(s.get("entry_articles", 0))},
            "standard": {"price": standard_price, "articles": int(s.get("standard_articles", 0))},
            "flagship": {"price": flagship_price, "articles": int(s.get("flagship_articles", 0))},
            # [价格锁承诺 2026-08-05] 真锁了才给日期;没锁给一句人话(逐词判定)
            **_lock_fields(_lock_map, kw_text),
            "intent": s.get("intent", "informational"),
            # [DRIFT-D] 报价 DTO 交付资格来自唯一引擎,前端展示守卫即生效(§17.1)。
            "commercial_delivery_eligible": _delivery_eligible_field(
                kw_text,
                intent_hint=s.get("intent"),
                existing=s.get("commercial_delivery_eligible"),
            ),
            "funnel_stage": s.get("funnel_stage", "awareness"),
            # 定价维度（供前端展示定价依据）
            "difficulty_score": round(s.get("difficulty_score", 1.0), 2),
            "value_score": round(s.get("value_score", 1.0), 2),
            "search_volume": s.get("search_volume", 0),
            "sem_price": round(s.get("sem_price", 0), 1),
            "effective_competition": s.get("effective_competition", 0),
            "search_probability": round(s.get("search_probability", 0.5), 2),
            # 【v1.3 §4.2】超红海标 · 转发 scorer SSOT(不重算)· 前端按标渲染「🔴 需单独报价」+ 不进套餐总价
            "super_red_ocean": bool(s.get("super_red_ocean", False)),
            "super_red_ocean_level": s.get("super_red_ocean_level", "none"),
            "competition_ratio": round(s.get("competition_ratio", 0.0), 3),
            "needs_review": bool(s.get("needs_review", False)),
            # [完整修复 2026-06-13 High1] 透传爆价/放飞标:前端渲染「参考价·人工核」· 不计入套餐保证总价
            "guarantee_unavailable": _guar_unavail,
            # [任务3 2026-06-08] 透传 should_quote(LLM-first):False=信息型不报价 · 前端区分「信息型·不报价」vs「🔴 需单独报价」
            "should_quote": bool(s.get("should_quote", True)),
        })

    # 恢复审计结果到对应关键词
    for kw_item in pricing_data["keywords"]:
        audit_info = old_audit_map.get(kw_item["keyword"])
        if audit_info:
            kw_item["audit_status"] = audit_info["audit_status"]
            kw_item["audit_note"] = audit_info["audit_note"]
            if audit_info.get("price_before_audit") is not None:
                kw_item["price_before_audit"] = audit_info["price_before_audit"]

    # [CTO-15.5 Q2.b 2026-04-20] 兜底保留自定义词/追加词
    # 老板反馈: 添加词提示"添加成功"但一算价就消失了
    # Root cause: batch_pricing 内部过滤(exclude_geo 命中 / LLM 不合规 / metaso 无结果) → scored 缺词
    # 修: all_keywords(客户选择+自定义+追加) 里每个词都必须出现在 pricing_data.keywords,
    #     缺失的用默认价兜底 + audit_note 告诉用户为何价格特殊
    present_keywords = {k["keyword"] for k in pricing_data["keywords"]}
    # [Codex 复诊 #2 2026-06-11 · 真实第一] 数据断供词必须排除在 Q2.b 之外(同 cluster 路径):
    #   它们是"故意不出价"不是"被过滤丢词",落进 missing_kws 会被补默认估算价 = 兜底价复活
    _unavailable_kw_set = {u.get("keyword") for u in pricing_data.get("unavailable_keywords", [])}
    # [报价意图闸 2026-08-04] 同理排除被意图闸剔的词(flat 路径 · 与 cluster 路径同口径)。
    #   不排 → Q2.b 把它们当"被 batch_pricing 过滤丢的词"补回来并套兜底价,闸白接。
    _policy_excluded_kw_set = {
        e.get("keyword") for e in (quote_data.get("policy_excluded_keywords") or [])
    }
    missing_kws = [kw for kw in all_keywords
                   if kw not in present_keywords
                   and kw not in _unavailable_kw_set
                   and kw not in _policy_excluded_kw_set]
    if missing_kws:
        logging.warning(f"[Q2.b 兜底] {len(missing_kws)} 个自定义/追加词被 batch_pricing 过滤,补默认价: {missing_kws[:5]}")
        # [P1 成本地板 2026-06-08] flat 兜底单篇成本用本人自设(quote_cost_override)· 无自设才回落 60(防高成本词被低估)
        _unit_cost = _resolve_fallback_unit_cost(quote_cost_override)
        # [完整修复 2026-06-13 Med1] 无自设成本时媒体成本层须乘进货倍率(与主算价路径 _merge_judged 一致):
        #   自设成本=填的已是真实成本(绝对·不乘);无自设=平台基准成本须 ×下级进货倍率,否则多级邀请链兜底词按平台价补 < 下级真实进货成本=亏。
        #   quote_cost_multiplier 已按 P0-A flag 选直属/累计(flag 关 + 普通代理 = 1.0 → 0 变化)。
        if quote_cost_override is None:
            _unit_cost = _unit_cost * quote_cost_multiplier
        _fallback_customer_unit = int(_unit_cost * quote_markup_ratio)
        for kw_text in missing_kws:
            kw_id = kw_id_map.get(kw_text, 0)
            # [P1 容量合同 2026-08-08] 兜底词篇数**统一走主合同阶梯**(pricing_bands 唯一取数出口)。
            #   原写死的 {entry:1, standard:2, flagship:3} 违反老板铁律「所有词 ≥5 篇 · 1-2 篇无效」,
            #   且与主合同 5/7/10 并存 = 同一张报价单里两套篇数口径。
            #   🔴 这会抬高兜底词价格(篇数进价格公式),交付单已列,回滚见 GEO_FALLBACK_ARTICLE_LADDER。
            default_articles = {
                tier: _fallback_tier_article_capacity(tier)
                for tier in ("entry", "standard", "flagship")
            }
            kw_min_price = MIN_KEYWORD_PRICE  # 120 元(地域词下限)
            entry_p = max(int(default_articles["entry"] * _fallback_customer_unit), kw_min_price)
            std_p = max(int(default_articles["standard"] * _fallback_customer_unit), kw_min_price)
            flag_p = max(int(default_articles["flagship"] * _fallback_customer_unit), kw_min_price)
            pricing_data["keywords"].append({
                "id": kw_id,
                "keyword": kw_text,
                "category_label": _find_category_label(kw_text, keywords_snapshot) or "自定义",
                "recommendation_reason": _find_recommendation_reason(kw_text, keywords_snapshot) or "客户自主添加",
                "entry": {"price": entry_p, "articles": default_articles["entry"]},
                "standard": {"price": std_p, "articles": default_articles["standard"]},
                "flagship": {"price": flag_p, "articles": default_articles["flagship"]},
                # [价格锁承诺 2026-08-05] Q2.b 兜底词是「batch_pricing 过滤掉的词」——
                #   既没进评分也不可能在缓存里(在缓存里就会作为 scored_cached 回来),
                #   所以恒不在 lock map 里 = 恒不显示锁期。走同一个出口,不另写分支。
                **_lock_fields(_lock_map, kw_text),
                "intent": "informational",
                "funnel_stage": "awareness",
                "difficulty_score": 1.0,
                "value_score": 1.0,
                "search_volume": 0,
                "sem_price": 0,
                "effective_competition": 0,
                "search_probability": 0.5,
                "audit_status": "fallback_default",
                "audit_note": "AI 搜索数据稀缺,暂用默认估算价;如需精准定价请联系销售或重新搜索",
                # 【v1.3 §4.2】兜底词无竞争数据 → 非超红海(默认 False)· 但标 needs_review 待复核
                "super_red_ocean": False,
                "super_red_ocean_level": "none",
                # [完整修复 2026-06-13 返修] 兜底估算词不走爆价护栏 → guarantee_unavailable 恒 False(响应字段形状统一)
                "guarantee_unavailable": False,
                "competition_ratio": 0.0,
                "needs_review": True,
            })

    # 重新计算各套餐总价（使用已执行最低价的数据）
    # [B1/§8 2026-06-05] 只对核心词求和 · 排除覆盖/相关搜索词(is_core=False)· 不把全候选池总价当 headline
    #   covered 词不计价不监控 → 不进 headline 总价(无 is_core 字段的旧数据视为核心,安全不变)
    for tier_key in pricing_data["tiers"]:
        # [§4.2] 超红海词不出保证价 → 不进套餐总价(同 is_core 排除口径)
        # [任务3 2026-06-08] LLM-first 信息型(should_quote=False)同样不进套餐总价
        # [完整修复 2026-06-13 High1] 爆价/放飞词(guarantee_unavailable)同口径不进套餐保证总价(belt:三档价已归零·此处显式排除双保险)
        _core_kws = [kw for kw in pricing_data["keywords"]
                     if kw.get("is_core") is not False and not kw.get("super_red_ocean")
                     and not kw.get("guarantee_unavailable")
                     and kw.get("should_quote", True) is not False]
        total_price = sum(kw[tier_key]["price"] for kw in _core_kws)
        total_articles = sum(kw[tier_key]["articles"] for kw in _core_kws)
        pricing_data["tiers"][tier_key]["total_price"] = total_price
        pricing_data["tiers"][tier_key]["total_articles"] = total_articles

    # [报价意图闸 2026-08-04] 被剔的词挂只读展示区 + 候选池现成商业词建议。
    #   放在总价重算**之后**:被剔词从来没进过 pricing_data["keywords"],
    #   所以它天然不在上面任何一档的求和里 —— 这是"不进总价"的实现方式,
    #   不是靠再加一条排除条件(少一条可漏的判据)。
    _attach_policy_exclusions(pricing_data, quote_data, keywords_snapshot, brand_name=brand_name)

    # Keep the engine output intact and stamp only the private baseline needed
    # for a deterministic per-quote coefficient preview.
    from services.quote_pricing_snapshot import stamp_calculation_context
    pricing_data, _ = stamp_calculation_context(
        pricing_data, None, coefficient=quote_markup_ratio
    )

    # 设为待审核状态（销售审核后才发给客户）
    update_session(
        token,
        status="pricing_pending_review",
        pricing_data=json.dumps(pricing_data, ensure_ascii=False),
        keywords_snapshot=json.dumps(keywords_snapshot, ensure_ascii=False),
        selected_keyword_ids=json.dumps(selected_ids),
        pending_keywords=None,
    )

    # 同步更新关联的 quote 记录（线上线下价格统一）
    if session.get("quote_id"):
        try:
            # [B1 2026-06-05 老板定稿] 生成期 headline=待客户选词 → monthly_price 写 0(不取全候选总价)
            #   客户在选词页勾选核心词 → confirm 时 _sync_quote_monthly_price 回写实选合计(前端 0 选显"请选择核心词")
            std_total = 0
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE quotes SET monthly_price = %s, markdown = %s WHERE id = %s",
                (std_total, markdown, session["quote_id"]),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logging.warning(f"同步 quote 记录失败: {e}")

    return {"success": True, "pricing_data": pricing_data, "status": "pricing_pending_review"}


async def _generate_quote_cluster_mode(
    token: str,
    session: dict,
    quote_markup_ratio: float = 1.0,  # [§4.5/决策5] 默认回成本(调用方恒传 resolved 值·此为兜底)
    quote_markup_override: Optional[float] = None,
    cost_per_article_override: Optional[float] = None,  # [M2] 服务商自设单篇成本(None=系统动态)
    allow_cache_write: Optional[bool] = None,  # [价格锁回归修 2026-06-10] 仅默认口径写共享缓存(主函数透传)
    cost_multiplier: float = 1.0,  # [v2.1] 下级进货倍率(上级 SKU 系数 · 自动估成本乘)
) -> dict:
    """主题包聚类报价模式"""
    # 收集需要报价的关键词（同原有逻辑）
    keywords_snapshot = _safe_json(session.get("keywords_snapshot"), [])
    selected_ids = _safe_json(session.get("selected_keyword_ids"), [])
    custom_keywords = _safe_json(session.get("custom_keywords"), [])
    pending_keywords = _safe_json(session.get("pending_keywords"), [])

    selected_kw_texts = []
    kw_id_map = {}
    for kw in keywords_snapshot:
        if kw["id"] in selected_ids:
            selected_kw_texts.append(kw["keyword"])
            kw_id_map[kw["keyword"]] = kw["id"]

    # 加入自定义词和追加词
    all_keywords = list(dict.fromkeys(selected_kw_texts + custom_keywords + pending_keywords))

    next_id = max((kw.get("id", 0) for kw in keywords_snapshot), default=0) + 1
    for kw_text in custom_keywords + pending_keywords:
        if kw_text not in kw_id_map:
            kw_id_map[kw_text] = next_id
            keywords_snapshot.append({
                "id": next_id,
                "keyword": kw_text,
                "category": "custom",
                "category_label": "自定义",
                "difficulty": 3,
                "recommended": False,
                "recommendation_reason": "客户自主添加",
            })
            if next_id not in selected_ids:
                selected_ids.append(next_id)
            next_id += 1

    if not all_keywords:
        raise HTTPException(400, "没有需要报价的关键词")

    quote = get_quote(session["quote_id"])
    brand_name = (quote.get("brand_name") or _get_brand_name(session["brand_id"])) if quote else _get_brand_name(session["brand_id"])
    industry = quote.get("industry", "") if quote else ""
    city = quote.get("city", "") if quote else ""

    # 调用主题包报价引擎
    from tools.batch_pricing import generate_cluster_quote
    cluster_data = await generate_cluster_quote(
        keywords=all_keywords,
        brand_name=brand_name,
        industry=industry,
        city=city,
        markup_override=quote_markup_override,
        cost_per_article_override=cost_per_article_override,  # [M2] 服务商自设单篇成本 A完全覆盖
        allow_cache_write=allow_cache_write,  # [价格锁回归修 2026-06-10] 主函数透传(仅默认口径写)
        cost_multiplier=cost_multiplier,  # [v2.1] 下级进货倍率(上级 SKU 系数)
        brand_id=session["brand_id"],  # [P0-D 2026-06-14] 信任资产按 brand_id 取(权威 id·不按名猜)
    )

    # [P2 前台3套餐红线 2026-06-05] customer-facing API 层显式剥除内部托管 strong/霸榜第4档(不只靠前端不渲染)
    _strip_internal_strong_tier(cluster_data)

    # [Codex 复诊 2026-06-11 · 真实第一] 全部断供 → 写 session 前直接失败(人话 · 不落空聚类报价)
    if cluster_data.get("unavailable_keywords") and not cluster_data.get("clusters"):
        raise HTTPException(status_code=503, detail="网络繁忙,关键词暂时无法估价,请稍后重试")

    # [报价意图闸 2026-08-04] cluster 路径同口径:全剔 → 给动作,不落空聚类报价
    _err = _nothing_quotable_error(
        cluster_data.get("clusters"), cluster_data, keywords_snapshot, brand_name=brand_name,
    )
    if _err:
        raise _err

    # 构建 clusters_data（写入 session）
    clusters_data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "clusters": [],
        "unclustered_keywords": cluster_data.get("unclustered_keywords", []),
        "stats": cluster_data.get("stats", {}),
        "tier_summaries": cluster_data.get("tier_summaries", {}),
        # [2026-06-11 老板拍 · 真实第一] 数据断供词(双 LLM 全挂/metaso 全挂)不出价不兜底
        "unavailable_keywords": cluster_data.get("unavailable_keywords", []),
    }

    # [v2.1 2026-06-11 老板拍] 同 brand 价格连续性 ±25% 警示(flag-only · 同 flat 模式口径)
    try:
        from tools.brand_price_continuity import check_brand_continuity
        _std_total = (cluster_data.get("tier_summaries", {}).get("standard", {}) or {}).get("core_price", 0)
        _bc = await check_brand_continuity(session.get("brand_id"), float(_std_total or 0))
        if _bc.get("ok") and _bc.get("needs_review"):
            _dir = "高" if _bc.get("deviation_pct", 0) > 0 else "低"
            clusters_data["brand_continuity"] = {
                "needs_review": True,
                "last_avg": _bc.get("last_avg", 0),
                "deviation_pct": _bc.get("deviation_pct", 0.0),
                "hint": (f"这单标准档总价比该客户最近成交均价(¥{_bc.get('last_avg', 0):,})"
                         f"偏{_dir} {abs(_bc.get('deviation_pct', 0)):.0f}% · 发给客户前确认一下"),
            }
    except Exception:
        pass  # 连续性提醒失败不影响报价

    # [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 同 flat 路径:锁期跟本次写没写缓存挂钩,逐词判定
    _lock_map = cluster_data.get(_LOCK_MAP_KEY) or {}

    for cluster in cluster_data["clusters"]:
        # 为核心词添加 snapshot id
        for kw in cluster["core_keywords"]:
            kw["id"] = kw_id_map.get(kw["keyword"], 0)
            kw.update(_lock_fields(_lock_map, kw["keyword"]))
        # 为可升级附赠词添加 snapshot id
        for kw in cluster["covered_keywords"]:
            if kw.get("upgradeable"):
                kw["id"] = kw_id_map.get(kw["keyword"], 0)

        clusters_data["clusters"].append(cluster)

    # 同时构建兼容的 pricing_data（旧版前端也能渲染基础信息）
    pricing_data = _build_compat_pricing_data(cluster_data, kw_id_map, keywords_snapshot, _lock_map)
    # [真实第一] 兼容 pricing_data 同样携带断供词(前端两套渲染都能提示)
    pricing_data["unavailable_keywords"] = cluster_data.get("unavailable_keywords", [])
    # [报价意图闸 2026-08-04] cluster 路径同样挂只读展示区(前端两套渲染共用同一份数据结构)
    _attach_policy_exclusions(pricing_data, cluster_data, keywords_snapshot, brand_name=brand_name)

    # [CTO-15.5 Q2.b 2026-04-20] 兜底保留自定义词/追加词(cluster 模式)
    # cluster 模式下 _build_compat_pricing_data 只收 is_selected=True 的核心词,非核心词或被过滤的自定义词会丢
    # 修: 检查 all_keywords 是否每个都在 pricing_data.keywords,缺失补兜底
    present_keywords = {k["keyword"] for k in pricing_data["keywords"]}
    # 同时检查 clusters_data 里所有词(核心 + covered)
    for cluster in clusters_data.get("clusters", []):
        for kw in cluster.get("core_keywords", []):
            present_keywords.add(kw.get("keyword", ""))
        for kw in cluster.get("covered_keywords", []):
            present_keywords.add(kw.get("keyword", ""))
    # [2026-06-11 老板拍 · 真实第一] 数据断供词必须排除在 Q2.b 兜底之外:
    #   它们是"故意不出价"不是"聚类丢词",落进 missing_kws 会被补默认估算价 = 兜底价复活
    _unavailable_kw_set = {u.get("keyword") for u in cluster_data.get("unavailable_keywords", [])}
    # [报价意图闸 2026-08-04] 同理必须排除:被意图闸剔的词也是"故意不出价"不是"聚类丢词"。
    #   不排会被 Q2.b 当缺词补回来并套 max(篇数×成本, MIN_KEYWORD_PRICE) 兜底价 →
    #   闸在上游剔干净、下游又原价复活,整条闸白接(断供词 2026-06-11 已踩过同一个坑)。
    _policy_excluded_kw_set = {
        e.get("keyword") for e in (cluster_data.get("policy_excluded_keywords") or [])
    }
    missing_kws = [kw for kw in all_keywords
                   if kw not in present_keywords
                   and kw not in _unavailable_kw_set
                   and kw not in _policy_excluded_kw_set]
    if missing_kws:
        logging.warning(f"[Q2.b cluster 兜底] {len(missing_kws)} 个自定义/追加词未进 clusters,补真实评分价: {missing_kws[:5]}")
        # [CTO-15.23 2026-05-11 Bug G] 兜底优先用真实评分价(7天缓存)· hardcode 60/120/180 是最后一道兜底
        # 老板报"业务方向词条单独成 cluster + 价格不准" · 之前 hardcode 一律 60 元/篇 跟评分脱钩
        cached_scoring = {}
        try:
            from db.diagnosis_db import get_cached_keyword_prices
            brand_for_cache = quote.get("brand_name", "") if quote else ""
            cached_scoring = get_cached_keyword_prices(
                brand_for_cache, missing_kws,
                industry=quote.get("industry") if quote else None,
                city=quote.get("city") if quote else None,
            )
        except Exception as _e:
            logging.warning(f"[Q2.b cluster 兜底] 缓存查询失败({_e}), 走 hardcode 兜底")

        # [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] cluster 兜底词走的是**独立的一次缓存查询**,
        #   命中的行是真实未过期缓存 → 这些词确实锁着(锁到该行 expires_at)。
        #   没命中的(hardcode 兜底价)不在 map 里 → 不显示锁期。
        _fallback_lock_map: dict = {}
        for _fb_kw, _fb_row in (cached_scoring or {}).items():
            _record_locked(_fallback_lock_map, [_fb_kw], (_fb_row or {}).get("expires_at"))

        # [P2-2 2026-06-08 报价中心永不吃缓存价] 缓存价编码的是历史/系统默认倍率,直接用会污染本人 1.0 口径
        #   (页面显示 1.0、兜底补回的词却拿旧倍率价 = "页面一套真实另一套")。
        #   → 缓存只取【篇数/评分参考】,价格一律按【本人系数 × 本人成本】重算。
        # [P1 容量合同 2026-08-08] 同 flat 路径:兜底词篇数统一走主合同阶梯(唯一取数出口)
        default_articles = {
            tier: _fallback_tier_article_capacity(tier)
            for tier in ("entry", "standard", "flagship")
        }
        kw_min_price = MIN_KEYWORD_PRICE
        for kw_text in missing_kws:
            kw_id = kw_id_map.get(kw_text, 0)
            c = cached_scoring.get(kw_text) or {}
            # [P1 成本地板 per-keyword 2026-06-08] 单篇成本:本人自设 > 缓存真实成本(高权威词如央媒350)> 默认60
            #   缓存只取【篇数/成本参考】· 绝不直接用缓存价(缓存价含旧倍率·会污染本人口径)· 价格按本人系数重算
            _unit_cost = _resolve_fallback_unit_cost(cost_per_article_override, c.get("cost_per_article"))
            # [完整修复 2026-06-13 Med1] 无自设成本时媒体成本层 ×进货倍率(缓存成本是平台 markup=1.0 口径·须按下级倍率抬)·
            #   与主算价路径 _merge_judged 一致 · cost_multiplier 已按 P0-A flag 选直属/累计(普通代理=1.0→0 变化)。
            if cost_per_article_override is None:
                _unit_cost = _unit_cost * cost_multiplier
            _kw_fallback_unit = int(_unit_cost * quote_markup_ratio)  # 本人系数(含 1.0)
            entry_a = c.get("entry_articles") or default_articles["entry"]
            std_a = c.get("standard_articles") or default_articles["standard"]
            flag_a = c.get("flagship_articles") or default_articles["flagship"]
            entry_p = max(int(entry_a * _kw_fallback_unit), kw_min_price)
            std_p = max(int(std_a * _kw_fallback_unit), kw_min_price)
            flag_p = max(int(flag_a * _kw_fallback_unit), kw_min_price)
            used_real = bool(c)
            pricing_data["keywords"].append({
                "id": kw_id,
                "keyword": kw_text,
                "category_label": "自定义",
                "recommendation_reason": "客户自主添加",
                "entry": {"price": entry_p, "articles": entry_a},
                "standard": {"price": std_p, "articles": std_a},
                "flagship": {"price": flag_p, "articles": flag_a},
                # [价格锁承诺 2026-08-05] 命中缓存的兜底词才有锁期(用它自己那行的 expires_at)
                **_lock_fields(_fallback_lock_map, kw_text),
                "intent": c.get("intent", "informational"),
                "funnel_stage": c.get("funnel_stage", "awareness"),
                "difficulty_score": c.get("difficulty_score", 1.0),
                "value_score": c.get("value_score", 1.0),
                "search_volume": c.get("search_volume", 0),
                "sem_price": c.get("sem_price", 0),
                "effective_competition": c.get("effective_competition", 0),
                "search_probability": c.get("search_probability", 0.5),
                "audit_status": "from_cache" if used_real else "fallback_default",
                "audit_note": ("使用历史评分数据,价格已按当前报价规则重算" if used_real else
                               "AI 搜索数据稀缺,暂用默认估算价;如需精准定价请联系销售或重新搜索"),
                # [完整修复 2026-06-13 返修] cluster 兜底词不走爆价护栏 → guarantee_unavailable 恒 False(响应字段形状统一)
                "guarantee_unavailable": False,
            })
        # cluster 模式下还要把兜底词塞进 unclustered_keywords 让前端展示
        unclustered = clusters_data.setdefault("unclustered_keywords", [])
        for kw_text in missing_kws:
            unclustered.append({
                "keyword": kw_text,
                "reason": "AI 搜索数据稀缺,未进主题包",
                "audit_status": "fallback_default",
            })

    # Stamp the private baseline after the pricing engine has completed.  No
    # cost or pricing-engine input is modified here.
    from services.quote_pricing_snapshot import stamp_calculation_context
    pricing_data, clusters_data = stamp_calculation_context(
        pricing_data, clusters_data, coefficient=quote_markup_ratio
    )

    # 写入 session
    update_session(
        token,
        status="pricing_pending_review",
        pricing_data=json.dumps(pricing_data, ensure_ascii=False),
        clusters_data=json.dumps(clusters_data, ensure_ascii=False),
        keywords_snapshot=json.dumps(keywords_snapshot, ensure_ascii=False),
        selected_keyword_ids=json.dumps(selected_ids),
        pending_keywords=None,
    )

    # 同步 quote 记录
    if session.get("quote_id"):
        try:
            # [B1 2026-06-05 老板定稿] 生成期 headline=待客户选词 → monthly_price 写 0(不取全 cluster 核心词合计)
            #   客户勾选核心词 → confirm 时 _sync_quote_monthly_price 回写实选合计(前端 0 选显"请选择核心词")
            std_total = 0
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE quotes SET monthly_price = %s WHERE id = %s",
                (std_total, session["quote_id"]),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logging.warning(f"同步 quote 记录失败: {e}")

    return {
        "success": True,
        "pricing_data": pricing_data,
        "clusters_data": clusters_data,
        "status": "pricing_pending_review",
    }


def _build_compat_pricing_data(cluster_data: dict, kw_id_map: dict,
                               keywords_snapshot: list, lock_map: dict) -> dict:
    """
    从主题包数据构建兼容旧版前端的 pricing_data 结构。
    只包含核心词（is_selected=True 的），让旧版 UI 至少能显示基础信息。

    [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 第 4 个参数由 `cache_expires: str`
    (无条件今天+7 的假日期)换成 `lock_map: dict`(定价引擎给的逐词事实)。
    """
    pricing_data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tiers": {},
        "keywords": [],
    }

    # 收集所有核心词
    all_core_keywords = []
    for cluster in cluster_data["clusters"]:
        if not cluster.get("is_selected", True):
            continue
        for kw in cluster["core_keywords"]:
            if kw.get("is_selected", True):
                all_core_keywords.append(kw)

    # 填充 tiers
    # [§4.2] 超红海词不出保证价 → 不进套餐总价(cluster compat 口径同 flat)
    # [完整修复 2026-06-13 High1] 爆价/放飞词(guarantee_unavailable)同口径不进套餐保证总价
    _quotable_cores = [kw for kw in all_core_keywords
                       if not (kw.get("super_red_ocean") or kw.get("guarantee_unavailable"))]
    for tier_key, tier_cfg in TIER_CONFIG.items():
        total_price = sum(
            kw.get(tier_key, {}).get("price", 0) for kw in _quotable_cores
        )
        total_articles = sum(
            kw.get(tier_key, {}).get("articles", 0) for kw in _quotable_cores
        )
        pricing_data["tiers"][tier_key] = {
            "label": tier_cfg["label"],
            "target_share": tier_cfg["target_share"],
            "ai_probability": tier_cfg["ai_probability"],
            "stars": tier_cfg["stars"],
            "total_price": total_price,
            "total_articles": total_articles,
        }

    # 填充 keywords
    for kw in all_core_keywords:
        kw_text = kw["keyword"]
        pricing_data["keywords"].append({
            "id": kw_id_map.get(kw_text, 0),
            "keyword": kw_text,
            "category_label": _find_category_label(kw_text, keywords_snapshot),
            "recommendation_reason": _find_recommendation_reason(kw_text, keywords_snapshot),
            "entry": kw.get("entry", {"price": 0, "articles": 0}),
            "standard": kw.get("standard", {"price": 0, "articles": 0}),
            "flagship": kw.get("flagship", {"price": 0, "articles": 0}),
            # [价格锁承诺 2026-08-05] 与 clusters_data 里同一个词用同一份事实(同一个 lock_map),
            #   两套渲染不许一个说锁了一个说没锁
            **_lock_fields(lock_map, kw_text),
            "intent": kw.get("intent", "informational"),
            # [DRIFT-D] cluster compat DTO 同口径填充交付资格(唯一引擎·§17.1)。
            "commercial_delivery_eligible": _delivery_eligible_field(
                kw_text,
                intent_hint=kw.get("intent"),
                existing=kw.get("commercial_delivery_eligible"),
            ),
            "difficulty_score": round(kw.get("difficulty_score", 1.0), 2),
            "value_score": round(kw.get("value_score", 1.0), 2),
            "search_volume": kw.get("search_volume", 0),
            "effective_competition": kw.get("effective_competition", 0),
            "search_probability": round(kw.get("search_probability", 0.5), 2),
            # 【v1.3 §4.2】超红海标(cluster compat)· 转发聚类词标 · 前端「🔴 需单独报价」
            "super_red_ocean": bool(kw.get("super_red_ocean", False)),
            "super_red_ocean_level": kw.get("super_red_ocean_level", "none"),
            "competition_ratio": round(kw.get("competition_ratio", 0.0), 3),
            "needs_review": bool(kw.get("needs_review", False)),
            # [完整修复 2026-06-13 High1] 爆价/放飞标(cluster compat)· 前端「参考价·人工核」· 不计入套餐保证总价
            "guarantee_unavailable": bool(kw.get("guarantee_unavailable", False)),
        })

    return pricing_data


@router.delete("/keyword-selection/{token}/keywords/{keyword_id}")
async def remove_keyword_from_session(token: str, keyword_id: int, request: Request):
    """
    从会话中删除一个关键词（审核阶段用）

    - 从 keywords_snapshot 和 selected_keyword_ids 中移除
    - 从 pricing_data.keywords 中移除
    - 重新计算各套餐总价

    [CTO-15.23 2026-05-11] P0 安全漏洞修:加 owner RBAC(防越权删他人会话的词)
    """
    session = _require_session_owner_access(request, token)
    if session["status"] not in ("pricing_pending_review", "keywords_submitted", "quoted"):
        raise HTTPException(400, f"当前状态 {session['status']} 不允许删除关键词")

    # 从 keywords_snapshot 移除
    snapshot = _safe_json(session.get("keywords_snapshot"), [])
    snapshot = [kw for kw in snapshot if kw.get("id") != keyword_id]

    # 从 selected_keyword_ids 移除
    selected_ids = _safe_json(session.get("selected_keyword_ids"), [])
    selected_ids = [sid for sid in selected_ids if sid != keyword_id]

    updates = {
        "keywords_snapshot": json.dumps(snapshot, ensure_ascii=False),
        "selected_keyword_ids": json.dumps(selected_ids),
    }

    # 从 pricing_data 移除并重新汇总
    if session.get("pricing_data"):
        pricing_data = _safe_json(session["pricing_data"], {})
        if "keywords" in pricing_data:
            pricing_data["keywords"] = [
                kw for kw in pricing_data["keywords"] if kw.get("id") != keyword_id
            ]
            # 重新计算各套餐总价
            for tier_key in ("entry", "standard", "flagship"):
                tier_info = pricing_data.get("tiers", {}).get(tier_key)
                if tier_info:
                    tier_info["total_price"] = sum(
                        kw.get(tier_key, {}).get("price", 0)
                        for kw in pricing_data["keywords"]
                    )
                    tier_info["total_articles"] = sum(
                        kw.get(tier_key, {}).get("articles", 0)
                        for kw in pricing_data["keywords"]
                    )
            updates["pricing_data"] = json.dumps(pricing_data, ensure_ascii=False)

    # 从 clusters_data 移除并重算
    if session.get("clusters_data"):
        clusters_data = _safe_json(session["clusters_data"], {})
        if clusters_data.get("clusters"):
            for cluster in clusters_data["clusters"]:
                # 从核心词移除
                cluster["core_keywords"] = [
                    kw for kw in cluster.get("core_keywords", []) if kw.get("id") != keyword_id
                ]
                # 从覆盖词移除
                cluster["covered_keywords"] = [
                    kw for kw in cluster.get("covered_keywords", []) if kw.get("id") != keyword_id
                ]
                # 重算 cluster 级别的 pricing
                if "pricing" in cluster:
                    for tier_key in ("entry", "standard", "flagship"):
                        tp = cluster["pricing"].get(tier_key)
                        if tp:
                            tp["core_price"] = sum(
                                kw.get(tier_key, {}).get("price", 0)
                                for kw in cluster["core_keywords"]
                            )
                            tp["core_articles"] = sum(
                                kw.get(tier_key, {}).get("articles", 0)
                                for kw in cluster["core_keywords"]
                            )
                # 更新核心词数量
                cluster["covered_keyword_count"] = len(cluster.get("covered_keywords", []))
            # 删除空 cluster（核心词为 0）
            clusters_data["clusters"] = [
                c for c in clusters_data["clusters"] if len(c.get("core_keywords", [])) > 0
            ]
            # 重算 tier_summaries
            if "tier_summaries" in clusters_data:
                for tier_key in ("entry", "standard", "flagship"):
                    ts = clusters_data["tier_summaries"].get(tier_key)
                    if ts:
                        ts["core_price"] = sum(
                            c.get("pricing", {}).get(tier_key, {}).get("core_price", 0)
                            for c in clusters_data["clusters"]
                        )
                        ts["total_articles"] = sum(
                            c.get("pricing", {}).get(tier_key, {}).get("core_articles", 0)
                            for c in clusters_data["clusters"]
                        )
            updates["clusters_data"] = json.dumps(clusters_data, ensure_ascii=False)

    update_session(token, **updates)

    return {
        "success": True,
        "remaining_keywords": len(snapshot),
        "removed_keyword_id": keyword_id,
    }


@router.post("/keyword-selection/{token}/audit-keyword/{keyword_id}")
async def audit_single_keyword(token: str, keyword_id: int, request: Request):
    """
    对单个异常关键词进行 LLM 审计 + AI 深探，修正价格

    流程：把该关键词交给 pricing_auditor 做 LLM 审计 + 深探，
    如果价格需要调整，按比例更新三个套餐的价格并持久化到 pricing_data
    """
    # [GEO-R1-CAN-063] owner 校验 · 防越权用他人 token 触发 LLM+深探重定价并改写 pricing_data
    session = _require_session_owner_access(request, token)
    if session["status"] not in ("pricing_pending_review", "adding_keywords"):
        raise HTTPException(400, f"当前状态 {session['status']} 不允许审计关键词")

    pricing_data = _safe_json(session.get("pricing_data"))
    pricing_data = _sync_pricing_tiers_from_config(pricing_data)  # r12: 实时覆盖老 cached ai_probability
    if not pricing_data or "keywords" not in pricing_data:
        raise HTTPException(400, "报价数据不存在")

    # 找到目标关键词
    target_kw = None
    target_idx = None
    for idx, kw in enumerate(pricing_data["keywords"]):
        if kw.get("id") == keyword_id:
            target_kw = kw
            target_idx = idx
            break

    if target_kw is None:
        raise HTTPException(404, f"关键词 ID {keyword_id} 不存在")

    # 构造 pricing_auditor 所需的 scored_keyword 格式
    std_price = target_kw["standard"]["price"]
    scored_kw = {
        "keyword": target_kw["keyword"],
        "selling_price": std_price,
        "sem_price": target_kw.get("sem_price", 0),
        "search_volume": target_kw.get("search_volume", 0),
        "bidword_company_count": 0,
        "competitor_count": target_kw.get("effective_competition", 0),
        "intent": target_kw.get("intent", "informational"),
        "value_score": target_kw.get("value_score", 1.0),
        "difficulty_score": target_kw.get("difficulty_score", 1.0),
        "content_count": 0,
        "data_source": "",
    }

    # 用整个关键词列表的统计信息来做异常检测
    all_scored = []
    for kw in pricing_data["keywords"]:
        all_scored.append({
            "keyword": kw["keyword"],
            "selling_price": kw["standard"]["price"],
            "sem_price": kw.get("sem_price", 0),
            "search_volume": kw.get("search_volume", 0),
            "bidword_company_count": 0,
            "competitor_count": kw.get("effective_competition", 0),
            "intent": kw.get("intent", "informational"),
            "value_score": kw.get("value_score", 1.0),
            "difficulty_score": kw.get("difficulty_score", 1.0),
            "content_count": 0,
            "data_source": "",
        })

    from tools.pricing_auditor import (
        detect_anomalies,
        llm_audit_anomalies,
        deep_probe_keywords,
        apply_corrections,
        MIN_KEYWORD_PRICE,
    )

    # Step 1: 异常检测（用全部词做统计背景）
    anomalies = detect_anomalies(all_scored)

    # 只保留目标关键词的异常信息
    target_anomaly = [a for a in anomalies if a["keyword"] == target_kw["keyword"]]

    # 如果规则没检出异常，手动构造一条让 LLM 审计
    if not target_anomaly:
        target_anomaly = [{
            "keyword": target_kw["keyword"],
            "current_price": std_price,
            "reasons": ["销售手动触发审计：请检查该词公开市场行情，判断当前定价是否合理"],
            "severity": 1,
            "raw_data": scored_kw,
        }]

    # Step 2: LLM 审计
    quote = get_quote(session["quote_id"]) if session.get("quote_id") else None
    industry = quote.get("industry", "") if quote else ""
    audit_results = await llm_audit_anomalies(target_anomaly, industry)

    # Step 3: 深探（如果 LLM 建议）
    deep_probe_kws = [a["keyword"] for a in audit_results if a.get("action") == "deep_probe"]
    probe_results = {}
    if deep_probe_kws:
        probe_results = await deep_probe_keywords(deep_probe_kws)

    # Step 4: 应用修正到 scored_kw
    apply_corrections([scored_kw], audit_results, probe_results)

    new_std_price = scored_kw.get("selling_price", std_price)
    audit_status = scored_kw.get("audit_status", "reviewed_ok")
    audit_note = scored_kw.get("audit_note", "")

    # 如果价格有变动，按比例调整三个套餐
    # [完整修复 2026-06-13 返修] std_price=0(super_red_ocean/guarantee_unavailable 参考价人工核词·三档价归零)不走比例缩放:
    #   防 ratio = new/0 ZeroDivisionError(my 改动令更多词价归零·此前 super_red_ocean 已潜在可触发)·
    #   此类词改价走 approve_quote 显式 adj_map(直接赋值)·不走 audit 比例缩放。
    if new_std_price != std_price and new_std_price > 0 and std_price > 0:
        ratio = new_std_price / std_price
        for tier_key in ("entry", "standard", "flagship"):
            old_tier_price = target_kw[tier_key]["price"]
            new_tier_price = max(MIN_KEYWORD_PRICE, int(old_tier_price * ratio))
            target_kw[tier_key]["price"] = new_tier_price

        # 重新计算各套餐总价
        for tier_key in ("entry", "standard", "flagship"):
            tier_info = pricing_data.get("tiers", {}).get(tier_key)
            if tier_info:
                tier_info["total_price"] = sum(
                    kw.get(tier_key, {}).get("price", 0)
                    for kw in pricing_data["keywords"]
                )

    # 记录审计信息到关键词数据
    target_kw["audit_status"] = audit_status
    target_kw["audit_note"] = audit_note
    if new_std_price != std_price:
        target_kw["price_before_audit"] = std_price
    # [DRIFT-D] 审计返回/持久化的是 target_kw(客户可见 DTO,census 3027 锚在内部
    #   scored_kw 的 intent 行)。老 session 的 target_kw 可能没有该字段 → 用唯一
    #   引擎回填,确保返回 keyword_data 与持久化 pricing_data 都携带交付资格。
    #   仅补字段,不触碰任何价格(§9.6)。
    if target_kw.get("commercial_delivery_eligible") is None:
        target_kw["commercial_delivery_eligible"] = _delivery_eligible_field(
            target_kw.get("keyword", ""),
            intent_hint=target_kw.get("intent"),
        )

    # [audit P2 2026-06-10] cluster 模式双写:旧版只写 pricing_data,clusters_data(客户端渲染源)不同步
    # → 审计调价客户端看不到、两份数据打架。对齐 approve_quote 同步块(~:2315)同款:改词三档 + 重算包级 + tier_summaries。
    clusters_data = _safe_json(session.get("clusters_data"))
    _cluster_synced = False
    if clusters_data and clusters_data.get("clusters"):
        for cl in clusters_data["clusters"]:
            _hit = False
            for ckw in (cl.get("core_keywords") or []) + (cl.get("covered_keywords") or []):
                if ckw.get("keyword") != target_kw.get("keyword"):
                    continue
                for tier_key in ("entry", "standard", "flagship"):
                    if ckw.get(tier_key) and target_kw.get(tier_key):
                        ckw[tier_key]["price"] = int(target_kw[tier_key]["price"])
                ckw["audit_status"] = audit_status
                ckw["audit_note"] = audit_note
                _hit = True
            if not _hit:
                continue
            _cluster_synced = True
            # 重算包级价格(同 approve_quote)
            for tier_key in ("entry", "standard", "flagship"):
                selected_core = [k for k in cl.get("core_keywords", []) if k.get("is_selected")]
                core_price = sum(k.get(tier_key, {}).get("price", 0) for k in selected_core)
                core_articles = sum(k.get(tier_key, {}).get("articles", 0) for k in selected_core)
                all_kws_price = core_price
                for _ck in cl.get("covered_keywords", []):
                    all_kws_price += _ck.get(tier_key, {}).get("price", 0) if _ck.get(tier_key) else 0
                if cl.get("pricing", {}).get(tier_key):
                    cl["pricing"][tier_key]["core_price"] = core_price
                    cl["pricing"][tier_key]["core_articles"] = core_articles
                    cl["pricing"][tier_key]["full_price"] = all_kws_price
                    cl["pricing"][tier_key]["savings"] = all_kws_price - core_price
        if _cluster_synced:
            # 重算 tier_summaries(同 approve_quote)
            for tier_key in ("entry", "standard", "flagship"):
                selected_clusters = [c for c in clusters_data["clusters"]
                                     if c.get("is_selected") and c.get("pricing", {}).get(tier_key)]
                clusters_data.setdefault("tier_summaries", {})[tier_key] = {
                    "core_price": sum(c["pricing"][tier_key]["core_price"] for c in selected_clusters),
                    "full_price": sum(c["pricing"][tier_key]["full_price"] for c in selected_clusters),
                    "savings": sum(c["pricing"][tier_key]["savings"] for c in selected_clusters),
                    "total_articles": sum(c["pricing"][tier_key]["core_articles"] for c in selected_clusters),
                }

    # 持久化(cluster 命中时双写)
    if _cluster_synced:
        update_session(
            token,
            pricing_data=json.dumps(pricing_data, ensure_ascii=False),
            clusters_data=json.dumps(clusters_data, ensure_ascii=False),
        )
    else:
        update_session(token, pricing_data=json.dumps(pricing_data, ensure_ascii=False))

    return {
        "success": True,
        "keyword": target_kw["keyword"],
        "audit_status": audit_status,
        "audit_note": audit_note,
        "old_price": std_price,
        "new_price": target_kw["standard"]["price"],
        "price_changed": new_std_price != std_price,
        "keyword_data": target_kw,
    }


class ClusterEditRequest(BaseModel):
    """销售端主题包编辑请求"""
    action: str  # "remove_cluster" | "toggle_core"
    cluster_index: int  # clusters_data.clusters 中的索引
    keyword_id: Optional[int] = None  # toggle_core 时需要


@router.post("/keyword-selection/{token}/edit-cluster")
async def edit_cluster(token: str, request: Request, req: ClusterEditRequest):
    """销售端编辑主题包：删除包 / 切换核心词↔附赠词"""
    # [GEO-R1-CAN-111] owner 校验 · 防越权用他人 token 删包/移词/改写 clusters_data
    session = _require_session_owner_access(request, token)
    if session["status"] not in ("pricing_pending_review", "quoted"):
        raise HTTPException(400, f"当前状态 {session['status']} 不允许编辑主题包")

    clusters_data = _safe_json(session.get("clusters_data"))
    if not clusters_data or not clusters_data.get("clusters"):
        raise HTTPException(400, "当前报价非主题包模式")

    clusters = clusters_data["clusters"]
    if req.cluster_index < 0 or req.cluster_index >= len(clusters):
        raise HTTPException(400, f"主题包索引 {req.cluster_index} 超出范围")

    cl = clusters[req.cluster_index]

    if req.action == "remove_cluster":
        clusters.pop(req.cluster_index)
        # 重算 tier_summaries
        for tier_key in ("entry", "standard", "flagship"):
            selected = [c for c in clusters if c.get("is_selected", True)]
            clusters_data.setdefault("tier_summaries", {})[tier_key] = {
                "core_price": sum(c.get("pricing", {}).get(tier_key, {}).get("core_price", 0) for c in selected),
                "full_price": sum(c.get("pricing", {}).get(tier_key, {}).get("full_price", 0) for c in selected),
                "savings": sum(c.get("pricing", {}).get(tier_key, {}).get("savings", 0) for c in selected),
                "core_articles": sum(c.get("pricing", {}).get(tier_key, {}).get("core_articles", 0) for c in selected),
                "cluster_count": len(selected),
            }

    elif req.action == "toggle_core":
        if req.keyword_id is None:
            raise HTTPException(400, "toggle_core 需要 keyword_id")
        # 在core_keywords里找 → 移到covered
        core_kws = cl.get("core_keywords", [])
        covered_kws = cl.get("covered_keywords", [])
        moved = False
        for i, kw in enumerate(core_kws):
            if kw.get("id") == req.keyword_id:
                kw_item = core_kws.pop(i)
                kw_item["source"] = "demoted"
                kw_item["upgradeable"] = True
                covered_kws.append(kw_item)
                moved = True
                break
        if not moved:
            # 在covered里找 → 升级为core
            for i, kw in enumerate(covered_kws):
                if kw.get("id") == req.keyword_id and kw.get("upgradeable"):
                    kw_item = covered_kws.pop(i)
                    kw_item.pop("source", None)
                    kw_item.pop("upgradeable", None)
                    core_kws.append(kw_item)
                    moved = True
                    break
        if not moved:
            raise HTTPException(404, f"关键词 {req.keyword_id} 不在该主题包中")

        # 重算包级价格
        for tier_key in ("entry", "standard", "flagship"):
            core_price = sum(kw.get(tier_key, {}).get("price", 0) for kw in core_kws if kw.get("is_selected", True))
            full_price = core_price + sum(kw.get(tier_key, {}).get("price", 0) for kw in covered_kws if kw.get(tier_key))
            cl.setdefault("pricing", {})[tier_key] = {
                "core_price": core_price,
                "full_price": full_price,
                "savings": full_price - core_price,
                "core_articles": sum(kw.get(tier_key, {}).get("articles", 0) for kw in core_kws if kw.get("is_selected", True)),
            }
        cl["core_keyword_count"] = len(core_kws)
        cl["covered_keyword_count"] = len(covered_kws)

        # 重算 tier_summaries
        for tier_key in ("entry", "standard", "flagship"):
            selected = [c for c in clusters if c.get("is_selected", True)]
            clusters_data.setdefault("tier_summaries", {})[tier_key] = {
                "core_price": sum(c.get("pricing", {}).get(tier_key, {}).get("core_price", 0) for c in selected),
                "full_price": sum(c.get("pricing", {}).get(tier_key, {}).get("full_price", 0) for c in selected),
                "savings": sum(c.get("pricing", {}).get(tier_key, {}).get("savings", 0) for c in selected),
                "core_articles": sum(c.get("pricing", {}).get(tier_key, {}).get("core_articles", 0) for c in selected),
                "cluster_count": len(selected),
            }
    else:
        raise HTTPException(400, f"未知操作: {req.action}")

    update_session(token, clusters_data=json.dumps(clusters_data, ensure_ascii=False))
    return {"success": True, "clusters_data": clusters_data}


class ApproveQuoteRequest(BaseModel):
    """审核发送报价请求 — 可选调整价格"""
    price_adjustments: Optional[dict] = None  # {keyword_id: {tier: new_price, ...}, ...}


class QuoteCoefficientRequest(BaseModel):
    coefficient: float = Field(..., ge=1.0, le=5.0)
    reason: str = Field(..., min_length=1, max_length=300)
    expected_snapshot_hash: str = Field(..., min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


def _quote_snapshot_actor(request: Request) -> tuple[int, Optional[int]]:
    identity = getattr(request.state, "organization_identity", None)
    if identity is not None:
        return int(identity.actor_user_id or identity.authenticated_user_id), identity.membership_id
    user = getattr(request.state, "user", None) or {}
    user_id = user.get("user_id") or user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")
    return int(user_id), None


@router.get("/quotes/{quote_id}/coefficient-preview")
async def preview_quote_coefficient(quote_id: int, coefficient: float, request: Request):
    """Preview plans, keyword prices, and totals using the immutable quote id."""
    from auth.brand_access import require_quote_access
    from services.quote_pricing_snapshot import QuoteSnapshotError, build_coefficient_preview

    require_quote_access(request, quote_id, allow_null=False)
    session = await asyncio.to_thread(get_session_by_quote, quote_id)
    if not session:
        raise HTTPException(status_code=404, detail="该报价单没有关联选词会话")
    try:
        preview = build_coefficient_preview(
            session.get("pricing_data"),
            session.get("clusters_data"),
            new_coefficient=coefficient,
        )
    except QuoteSnapshotError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.as_detail()) from exc
    return {
        "quote_id": quote_id,
        "old_coefficient": preview["old_coefficient"],
        "new_coefficient": preview["new_coefficient"],
        "calculation_version": preview["calculation_version"],
        "summaries": preview["summaries"],
        "keywords": preview["keywords"],
        "keyword_count": preview["keyword_count"],
        "snapshot_hash": preview["snapshot_hash"],
    }


@router.post("/quotes/{quote_id}/coefficient")
async def save_quote_coefficient(quote_id: int, payload: QuoteCoefficientRequest, request: Request):
    """Save a per-quote coefficient as a new immutable pricing snapshot."""
    from auth.brand_access import require_quote_access
    from services.quote_pricing_snapshot import QuoteSnapshotError, save_coefficient_snapshot

    require_quote_access(request, quote_id, allow_null=False)
    actor_user_id, membership_id = _quote_snapshot_actor(request)
    try:
        result = await asyncio.to_thread(
            save_coefficient_snapshot,
            quote_id,
            actor_user_id=actor_user_id,
            actor_membership_id=membership_id,
            new_coefficient=payload.coefficient,
            reason=payload.reason,
            expected_snapshot_hash=payload.expected_snapshot_hash,
        )
    except QuoteSnapshotError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.as_detail()) from exc
    return {
        "success": True,
        "quote_id": quote_id,
        "old_coefficient": result["old_coefficient"],
        "new_coefficient": result["new_coefficient"],
        "calculation_version": result["calculation_version"],
        "summaries": result["summaries"],
        "keyword_count": result["keyword_count"],
        "snapshot": result["snapshot"],
    }


# ============================================================
# 【P1 篇数容量合同 · 2026-08-08】
#   语义:报价冻结的 required_articles = **可交付容量上限**,交付允许 0..capacity。
#   本节两个端点是容量的唯一对外读写口,P4 缺口作战计划直接消费,不自己算。
# ============================================================

class CapacityEvaluationCandidate(BaseModel):
    """候选**只带问题本身**。价格/篇数字段一律拒收(前端不算钱 · 08_billing §3.3)。"""
    keyword: str = Field(..., min_length=1, max_length=200)
    plan_item_id: Optional[str] = Field(None, max_length=64)
    gap_reason: Optional[str] = Field(None, max_length=200)


class CapacityEvaluationRequest(BaseModel):
    candidates: List[CapacityEvaluationCandidate] = Field(..., min_length=1, max_length=50)
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    origin: str = Field("delivery_plan", max_length=64)


@router.get("/quotes/{quote_id}/capacity")
async def get_quote_capacity(quote_id: int, request: Request):
    """报价单的容量态(P4 `delivery_plan_snapshot.capacity` 的唯一数据源)。

    返回字段与 `04-P4-api-contract-draft` §2 的 capacity 块逐字对齐;
    **未付款 / 容量为 0 → display_status=capacity_zero**,消费方据此不出"去写这篇"。
    """
    from auth.brand_access import require_quote_access
    from services.article_capacity_contract import (
        CapacityContractError,
        resolve_quote_capacity,
    )
    from services.quote_pricing_snapshot import list_capacity_evaluation_requests

    # 行级归属:与写端点同一把闸(不依赖中间件兜底 —— 系统缺资源级归属层的旧账)
    require_quote_access(request, quote_id, allow_null=False)

    def _read() -> dict:
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            return resolve_quote_capacity(cur, quote_id)
        finally:
            conn.close()

    try:
        capacity = await asyncio.to_thread(_read)
    except CapacityContractError as exc:
        raise HTTPException(status_code=404, detail={
            "code": "QUOTE_CAPACITY_NOT_FOUND",
            "message": "报价不存在或已归档。",
            "retryable": False,
        }) from exc
    pending = await asyncio.to_thread(list_capacity_evaluation_requests, quote_id)
    return {
        "success": True,
        "quote_id": quote_id,
        "capacity": capacity,
        # 已登记待重新算价的评估请求(「加入报价评估」的回显 · 不含任何价格)
        "pending_evaluation_requests": [
            {
                "idempotency_key": entry.get("idempotency_key"),
                "origin": entry.get("origin"),
                "status": entry.get("status"),
                "candidates": entry.get("candidates") or [],
            }
            for entry in pending
        ],
    }


@router.post("/quotes/{quote_id}/capacity/evaluation-requests")
async def request_quote_capacity_evaluation(
    quote_id: int, payload: CapacityEvaluationRequest, request: Request
):
    """【加入报价评估】把研究候选追加进报价评估队列。

    做什么:登记一条 append-only 的评估请求(新的报价快照版本)+ 通知服务商重新算价。
    **不做什么**(资金红线,逐条对应 08_billing):
      · 不改已冻结的那一版报价,`active_pricing_snapshot_id` 原地不动(§7.5 历史不重算);
      · 不定价、不改容量、不建订单、不冻结/扣任何资金(§10 唯一写入口);
      · 容量真正增加仍走既有链路:服务商重新算价 → 新报价 → 客户确认付款。
    """
    from auth.brand_access import require_quote_access
    from services.quote_pricing_snapshot import (
        QuoteSnapshotError,
        record_capacity_evaluation_request,
    )

    # 行级归属:报价 → 品牌 → 租户/组织制品三段校验(allow_null=False → NULL-brand 单 fail-closed)
    quote_row = require_quote_access(request, quote_id, allow_null=False)
    actor_user_id, membership_id = _quote_snapshot_actor(request)
    try:
        result = await asyncio.to_thread(
            record_capacity_evaluation_request,
            quote_id,
            actor_user_id=actor_user_id,
            actor_membership_id=membership_id,
            candidates=[item.model_dump() for item in payload.candidates],
            origin=payload.origin,
            idempotency_key=payload.idempotency_key,
        )
    except QuoteSnapshotError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.as_detail()) from exc

    if not result["already_recorded"]:
        # 消费方:服务商侧待办。没有这一步,登记就是死数据(没人知道要重新算价)。
        try:
            _durable_selection_notice(
                brand_id=(quote_row or {}).get("brand_id"),
                quote_id=quote_id,
                event_type=NotificationEventType.BUSINESS_ACTION_REQUIRED,
                terminal_state="capacity_evaluation_requested",
                status="有新的候选问题待纳入报价评估",
                summary=f"新增 {len(result['candidates'])} 个候选问题，请重新算价后再发送报价。",
            )
        except Exception as exc:  # noqa: BLE001 通知失败不能把已登记的评估请求回滚掉
            logger.exception("加入报价评估通知写入 outbox 失败 quote=%s: %s", quote_id, exc)

    return {
        "success": True,
        "quote_id": quote_id,
        "already_recorded": result["already_recorded"],
        "snapshot_version": result["version"],
        "candidates": result["candidates"],
        # 明示下一步,避免前端误以为容量已经加上去了
        "capacity_changed": False,
        "next_step": "服务商重新算价并发送新报价后，容量才会变化。",
    }


@router.post("/keyword-selection/{token}/approve-quote")
async def approve_quote(token: str, request: Request, req: ApproveQuoteRequest = ApproveQuoteRequest()):
    """销售审核通过报价 → 发送给客户（状态变为 quoted）"""
    # [GEO-R1-CAN-110] owner 校验 · 防越权用他人 token 套用价格调整并把报价发给客户
    session = await asyncio.to_thread(_require_session_owner_access, request, token)
    # [WJ-13 2026-05-31] 幂等:已发送(quoted)再点直接返成功 · 覆盖并发双击/重复点,不报错
    if session["status"] == "quoted":
        return {"success": True, "already_sent": True, "message": "报价已发送给客户"}
    if session["status"] != "pricing_pending_review":
        raise HTTPException(400, f"当前状态 {session['status']} 不允许审核发送报价")

    from services.quote_pricing_snapshot import quote_payload_hash
    expected_source_hash = quote_payload_hash(session.get("pricing_data"), session.get("clusters_data"))
    pricing_data = _safe_json(session.get("pricing_data"))
    pricing_data = _sync_pricing_tiers_from_config(pricing_data)  # r12: 实时覆盖老 cached ai_probability
    if not pricing_data:
        raise HTTPException(400, "报价数据不存在，请先生成报价")

    clusters_data = _safe_json(session.get("clusters_data"))

    # 应用价格调整（同时更新 pricing_data 和 clusters_data）
    if req.price_adjustments:
        # 建立 keyword_id → adjustment 映射
        adj_map = req.price_adjustments

        # 更新 pricing_data（flat 兼容层）
        for kw in pricing_data["keywords"]:
            kw_id_str = str(kw["id"])
            adj = adj_map.get(kw_id_str)
            if not adj:
                continue
            # [任务3 2026-06-08] 超红海(需单独报价)/信息型(不报价)词不出保证价 · 豁免地板校验
            #   (它们价=0 或由销售单独定价 · 不该撞 MIN_KEYWORD_PRICE 拦截整单发送)
            # [完整修复 2026-06-13 High1] 爆价/放飞词(guarantee_unavailable)价=0 同口径豁免地板(否则 0<MIN 拦整单发送)
            _exempt = (bool(kw.get("super_red_ocean")) or bool(kw.get("guarantee_unavailable"))
                       or (kw.get("should_quote", True) is False))
            for tier_key in ("entry", "standard", "flagship"):
                if tier_key in adj:
                    new_price = int(adj[tier_key])
                    if not _exempt and new_price < MIN_KEYWORD_PRICE:
                        raise HTTPException(
                            400,
                            f"关键词「{kw['keyword']}」的{tier_key}价格 ¥{new_price} 低于最低价 ¥{MIN_KEYWORD_PRICE}"
                        )
                    kw[tier_key]["price"] = new_price

        # 重新计算各套餐总价 · [B1/§8 2026-06-05] 与 confirm 路径一致:只核心词求和(排除覆盖词·防 flat 层未来混入回归)
        for tier_key in ("entry", "standard", "flagship"):
            _core_kws = [kw for kw in pricing_data["keywords"] if kw.get("is_core") is not False]
            total_price = sum(kw[tier_key]["price"] for kw in _core_kws)
            total_articles = sum(kw[tier_key]["articles"] for kw in _core_kws)
            pricing_data["tiers"][tier_key]["total_price"] = total_price
            pricing_data["tiers"][tier_key]["total_articles"] = total_articles

        # 同步更新 clusters_data 中对应的核心词价格
        if clusters_data and clusters_data.get("clusters"):
            for cl in clusters_data["clusters"]:
                for kw in cl.get("core_keywords", []):
                    kw_id_str = str(kw.get("id"))
                    adj = adj_map.get(kw_id_str)
                    if not adj:
                        continue
                    for tier_key in ("entry", "standard", "flagship"):
                        if tier_key in adj and kw.get(tier_key):
                            kw[tier_key]["price"] = int(adj[tier_key])
                # 重算包级价格
                for tier_key in ("entry", "standard", "flagship"):
                    selected_core = [k for k in cl.get("core_keywords", []) if k.get("is_selected")]
                    core_price = sum(k.get(tier_key, {}).get("price", 0) for k in selected_core)
                    core_articles = sum(k.get(tier_key, {}).get("articles", 0) for k in selected_core)
                    all_kws_price = core_price
                    for ck in cl.get("covered_keywords", []):
                        all_kws_price += ck.get(tier_key, {}).get("price", 0) if ck.get(tier_key) else 0
                    if cl.get("pricing", {}).get(tier_key):
                        cl["pricing"][tier_key]["core_price"] = core_price
                        cl["pricing"][tier_key]["core_articles"] = core_articles
                        cl["pricing"][tier_key]["full_price"] = all_kws_price
                        cl["pricing"][tier_key]["savings"] = all_kws_price - core_price

            # 重算 tier_summaries
            for tier_key in ("entry", "standard", "flagship"):
                selected_clusters = [c for c in clusters_data["clusters"] if c.get("is_selected")]
                clusters_data.setdefault("tier_summaries", {})[tier_key] = {
                    "core_price": sum(c["pricing"][tier_key]["core_price"] for c in selected_clusters),
                    "full_price": sum(c["pricing"][tier_key]["full_price"] for c in selected_clusters),
                    "savings": sum(c["pricing"][tier_key]["savings"] for c in selected_clusters),
                    "total_articles": sum(c["pricing"][tier_key]["core_articles"] for c in selected_clusters),
                }

    # 再次验证所有价格不低于最低价
    for kw in pricing_data["keywords"]:
        # [任务3 2026-06-08] 超红海(需单独报价)/信息型(不报价)词不出保证价(price=0)· 豁免地板(不撞 MIN_KEYWORD_PRICE 拦发送)
        # [完整修复 2026-06-13 High1] 爆价/放飞词(guarantee_unavailable)价=0 同口径豁免地板
        if (bool(kw.get("super_red_ocean")) or bool(kw.get("guarantee_unavailable"))
                or (kw.get("should_quote", True) is False)):
            continue
        for tier_key in ("entry", "standard", "flagship"):
            if kw[tier_key]["price"] < MIN_KEYWORD_PRICE:
                raise HTTPException(
                    400,
                    f"关键词「{kw['keyword']}」的{tier_key}价格 ¥{kw[tier_key]['price']} 低于最低价 ¥{MIN_KEYWORD_PRICE}"
                )

    # Freeze the exact payload before it becomes customer-visible.  The state
    # transition and both snapshot pointers commit in the same transaction.
    from services.quote_pricing_snapshot import QuoteSnapshotError, publish_frozen_snapshot
    actor_user_id, membership_id = _quote_snapshot_actor(request)
    try:
        await asyncio.to_thread(
            publish_frozen_snapshot,
            int(session["quote_id"]),
            actor_user_id=actor_user_id,
            actor_membership_id=membership_id,
            pricing_data=pricing_data,
            clusters_data=clusters_data,
            reason="审核发送客户 · 冻结公开报价",
            expected_source_hash=expected_source_hash,
        )
    except QuoteSnapshotError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.as_detail()) from exc

    # B2.3 (CTO-15.9 session 3 · 2026-04-25 · M1a KR2 报价发送率埋点)
    # 旧版 0 处写 send_quote · KR2 ≥ 70% 无法度量
    # 新版:每次 approve-quote(代理审完发给客户)写一条 audit_log
    try:
        from db.auth_db import create_audit_log
        # 抽取关键指标(便于运营 SQL 聚合 send_quote 趋势)
        # [B1/§8 2026-06-05] send_quote 埋点用 core_price(核心词合计)· 不用 full_price(全量候选含覆盖词·会把全量价当成交价)
        tier_summaries = (clusters_data or {}).get("tier_summaries") or {}
        _std = tier_summaries.get("standard") or {}
        std_total = _std.get("core_price") or _std.get("full_price") or 0
        kw_count = len(pricing_data.get("keywords") or [])
        create_audit_log(
            action="send_quote",
            module="quote",
            entity_type="keyword_selection_session",
            entity_id=session.get("id"),
            summary=f"代理审核通过报价 · 发送给客户 · 关键词 {kw_count} 个 · 标准档总价 ¥{int(std_total)}",
            after={
                # [GEO-R1-CAN-133] 不落库明文 selection token(7 天 bearer · 持有即可提交) · 只存前缀做关联
                "token": token[:8],
                "session_id": session.get("id"),
                "brand_id": session.get("brand_id"),
                "brand_name": session.get("brand_name"),
                "keyword_count": kw_count,
                "standard_total_price": int(std_total),
                "price_adjustments": bool(req.price_adjustments),
            },
        )
    except Exception as _ae:
        logger.warning(f"[approve_quote] B2.3 send_quote audit_log 失败(非阻塞): {_ae}")

    return {"success": True, "status": "quoted", "pricing_data": pricing_data}


@router.post("/keyword-selection/{token}/recall")
async def recall_quote(token: str, request: Request):
    """销售端撤回报价 — 从 quoted/adding_keywords 回退到 pricing_pending_review,允许修改后重发
    [P0-9 fix 2026-05-23 老板授权] Codex 跨 AI 审计 · 加 owner 校验"""
    session = _require_session_owner_access(request, token)
    if session["status"] not in ("quoted", "adding_keywords"):
        raise HTTPException(400, f"当前状态 {session['status']} 不允许撤回")

    # 合并 pending_keywords 到 snapshot（如果有）
    pending = _safe_json(session.get("pending_keywords"), [])
    update_kwargs = {"status": "pricing_pending_review", "pending_keywords": None}

    if pending:
        snapshot = _safe_json(session.get("keywords_snapshot"), [])
        existing_texts = {kw.get("keyword", "") for kw in snapshot}
        max_id = max((kw.get("id", 0) for kw in snapshot), default=0)
        for kw_text in pending:
            if kw_text not in existing_texts:
                max_id += 1
                snapshot.append({"id": max_id, "keyword": kw_text, "source": "customer_added"})
        update_kwargs["keywords_snapshot"] = json.dumps(snapshot, ensure_ascii=False)

    update_session(token, **update_kwargs)
    # [GEO-R1-CAN-133] 日志只打 token 前缀 · 明文 token 是 7 天 bearer 凭证
    logger.info(f"销售撤回报价: token={token[:8]}.., 合并 {len(pending)} 个待处理词")
    return {"success": True, "merged_pending": len(pending)}


# 回退状态映射：当前状态 → 回退目标（按前端可见步骤回退）
# active/pending_payment/confirmed 都在"签约收款"步骤，回退应直接到 quoted（客户确认步骤）
REVERT_MAP = {
    "keywords_submitted": "selecting",
    "pricing_pending_review": "keywords_submitted",
    "quoted": "pricing_pending_review",
    "confirmed": "quoted",
    "pending_payment": "quoted",
    "active": "quoted",
}


def _revert_selection_session(session: dict) -> dict:
    """按选词会话状态执行一步回退，供 token 和 quote_id 两种入口复用。"""
    old_status = session["status"]
    new_status = REVERT_MAP.get(old_status)
    if not new_status:
        raise HTTPException(400, f"当前状态 {old_status} 不支持回退")

    # 从签约/收款阶段回退到 quoted 时,清掉写作大厅关键词 + 复位服务生命周期 + 取消监测订阅
    # [audit P2 2026-06-10] 旧版只 DELETE 关键词,不复位 quotes.service_status/service_start/end_date、
    #   不取消监测订阅 → 已付款订单回退后变"有服务期 + 在跑监测扣费,但 0 关键词"的僵尸态(daily 继续烧算力)。
    #   修:同事务复位服务锚 + 取消该 quote 监测订阅;DELETE/复位失败 → fail-closed 不翻状态(可重试)。
    #   注:paid_at/paid_amount 不在此清(收款事实留痕·退款走独立流程·回退不误抹财务记录)。
    if old_status in ("active", "pending_payment", "confirmed") and new_status == "quoted" and session.get("quote_id"):
        _qid = session["quote_id"]
        # [audit #8 返修 必做] 业务态守卫:已付款 / 服务进行中的报价【禁止】一步回退 ——
        #   DELETE confirmed_keywords 会铲平在交付的付费客户(prod 20 单 paid/active:已生成文章 +
        #   履约日志 FK 引用 confirmed_keywords,且 paid_at 留痕却无退费对账)。这类订单必须走
        #   【订单作废 / 退款】独立流程,不允许销售端一步回退误抹交付。
        try:
            _gconn = get_connection()
            try:
                _gcur = _gconn.cursor()
                _gcur.execute("SELECT status, service_status, paid_at FROM quotes WHERE id = %s", (_qid,))
                _qrow = _gcur.fetchone()
            finally:
                try:
                    _gconn.close()
                except Exception:
                    pass
        except Exception as _ge:
            logger.error(f"[revert] quote_id={_qid} 业务态查询失败 · fail-closed 拒回退: {_ge}")
            raise HTTPException(500, "回退前业务态校验失败,请重试")
        _q_status = (_qrow.get("status") if _qrow else None)
        _q_svc = (_qrow.get("service_status") if _qrow else None)
        _q_paid_at = (_qrow.get("paid_at") if _qrow else None)
        # [audit #8 返修v2 · Fable] 守卫扩到 confirmed:玩法B 交付单(prod quote 156/124)= status='confirmed'
        #   + service_status='pending'(不命中旧 paid/active 守卫)+ 携带履约数据(156:50词/3050履约行;
        #   124:6词/406行)。一步回退会 DELETE confirmed_keywords + 孤儿化数千行履约日志 = 铲平在交付客户。
        #   口径:status∈(paid,confirmed) OR service_status='active' OR paid_at 非空 → 一律拒(走作废/退款)。
        if _q_status in ("paid", "confirmed") or _q_svc == "active" or _q_paid_at is not None:
            raise HTTPException(
                status_code=409,
                detail="该报价已付款 / 已确认 / 服务进行中,不能直接回退(会清空已交付内容)· 请走订单作废或退款流程处理。",
            )
        # [audit #8 返修 P3] try/finally 包 DB 块:失败必 rollback + close(原 except 只 log+raise →
        #   连接不归还 + 进池仍 in_failed_state → 下个 request 拿到立即 InFailedSqlTransaction)。
        try:
            conn = get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM confirmed_keywords WHERE quote_id = %s", (_qid,))
                cursor.execute("DELETE FROM keyword_clusters WHERE quote_id = %s", (_qid,))
                # 复位服务生命周期(回退跨越签约/收款步骤 → 服务尚未成立)
                cursor.execute(
                    """UPDATE quotes
                       SET service_status = NULL, service_start_date = NULL, service_end_date = NULL
                       WHERE id = %s""",
                    (_qid,),
                )
                conn.commit()
                logger.info(f"已清理写作大厅数据 + 复位服务生命周期: quote_id={_qid}")
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except HTTPException:
            raise
        except Exception as e:
            # fail-closed:清理/复位失败 → 不翻状态(防"状态回退了但服务期/监测还在跑"的半截僵尸)
            logger.error(f"[revert] quote_id={_qid} 清理+复位失败 · 中止回退(fail-closed): {e}")
            raise HTTPException(500, f"回退失败 · 服务数据未能安全复位,请重试: {str(e)[:120]}")
        # 取消该 quote 监测订阅(quote 级·不波及同 brand 其他 quote)· 事务外·失败只 log 不阻断
        try:
            from db.monitoring_db import cancel_subscriptions_by_quote
            _cancelled = cancel_subscriptions_by_quote(_qid, reason="selection_reverted")
            if _cancelled:
                logger.info(f"[revert] quote_id={_qid} 取消监测订阅 {_cancelled} 条")
        except Exception as e:
            logger.warning(f"[revert] quote_id={_qid} 取消监测订阅失败(不阻断·daily 仍被服务锚口径挡): {e}")

    token = session["token"]
    update_session(token, status=new_status)
    # [GEO-R1-CAN-133] 日志只打 token 前缀 · 明文 token 是 7 天 bearer 凭证
    logger.info(f"状态回退: token={token[:8]}.., {old_status} → {new_status}")
    return {"success": True, "old_status": old_status, "new_status": new_status}


@router.post("/keyword-selection/{token}/revert-status")
async def revert_session_status(token: str, request: Request):
    """通用状态回退 — 回到上一步
    [P0-9 fix 2026-05-23 老板授权] Codex 跨 AI 审计 · 加 owner 校验"""
    session = _require_session_owner_access(request, token)
    return _revert_selection_session(session)


@router.post("/quotes/{quote_id}/revert-status")
async def revert_quote_selection_status(quote_id: int, request: Request):
    """兼容旧报价页:通过 quote_id 找选词会话并回退状态。
    [P0-9 fix 2026-05-23 老板授权] Codex 跨 AI 审计 · 加 require_quote_access"""
    from auth.brand_access import require_quote_access
    require_quote_access(request, quote_id)
    session = get_session_by_quote(quote_id)
    if not session:
        raise HTTPException(404, "该报价单没有关联选词会话")
    return _revert_selection_session(session)


class QuoteArchiveRequest(BaseModel):
    reason: str = Field(default="用户归档", min_length=1, max_length=300)


@router.get("/quotes/{quote_id}/archive-preview")
async def preview_quote_archive(quote_id: int, request: Request):
    from auth.brand_access import require_quote_access
    from services.artifact_archive import ArtifactArchiveError, get_quote_archive_preview

    require_quote_access(request, quote_id, allow_null=False)
    try:
        return await asyncio.to_thread(get_quote_archive_preview, quote_id)
    except ArtifactArchiveError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.as_detail()) from exc


@router.delete("/quotes/{quote_id}")
async def archive_quote_by_id(
    quote_id: int,
    request: Request,
    payload: QuoteArchiveRequest = QuoteArchiveRequest(),
):
    from auth.brand_access import require_quote_access
    from services.artifact_archive import ArtifactArchiveError, archive_quote

    require_quote_access(request, quote_id, allow_null=False)
    actor_user_id, _ = _quote_snapshot_actor(request)
    try:
        result = await asyncio.to_thread(
            archive_quote,
            quote_id,
            actor_user_id=actor_user_id,
            reason=payload.reason,
            organization_identity=getattr(request.state, "organization_identity", None),
        )
    except ArtifactArchiveError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.as_detail()) from exc
    try:
        from db.auth_db import create_audit_log
        user = getattr(request.state, "user", None) or {}
        create_audit_log(
            user_id=actor_user_id,
            username=user.get("username"),
            action="quote_archive",
            module="quote",
            entity_type="quote",
            entity_id=quote_id,
            summary=f"归档报价 #{quote_id}",
            after=result,
        )
    except Exception as exc:
        logger.warning("quote archive audit failed quote_id=%s: %s", quote_id, exc)
    return {"success": True, **result}


@router.post("/quotes/{quote_id}/restore")
async def restore_quote_by_id(quote_id: int, request: Request):
    from auth.brand_access import require_quote_access
    from services.artifact_archive import ArtifactArchiveError, restore_quote

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT brand_id FROM quotes WHERE id=%s AND deleted_at IS NOT NULL", (quote_id,))
        archived = cursor.fetchone()
    finally:
        conn.close()
    if not archived:
        raise HTTPException(status_code=404, detail="未找到可恢复的归档报价")
    require_quote_access(request, quote_id, allow_null=False)
    actor_user_id, _ = _quote_snapshot_actor(request)
    try:
        result = await asyncio.to_thread(
            restore_quote,
            quote_id,
            actor_user_id=actor_user_id,
            organization_identity=getattr(request.state, "organization_identity", None),
        )
    except ArtifactArchiveError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.as_detail()) from exc
    return {"success": True, "quote_id": quote_id, **result}


@router.delete("/keyword-selection/{token}")
async def delete_selection_session(token: str, request: Request):
    """Retired destructive token mutation; callers must use immutable quote_id."""
    _require_session_owner_access(request, token)
    logger.info("retired token delete rejected token=%s..", token[:8])
    raise HTTPException(
        status_code=409,
        detail={
            "code": "IMMUTABLE_QUOTE_ID_REQUIRED",
            "message": "该删除入口已停用，请先读取归档影响并使用 quote_id 归档。",
            "priority": "P1",
            "retryable": False,
            "recovery": "GET /api/quotes/{quote_id}/archive-preview",
        },
    )


@router.delete("/keyword-selection/{token}/price-cache")
async def clear_price_cache(token: str, request: Request):
    """清空该品牌的报价缓存，下次生成报价将重新计算

    [CTO-15.23 2026-05-11] P0 安全漏洞修:加 owner RBAC(price_cache 跨 brand 全局 · 越权清缓存会污染他人报价)
    """
    session = _require_session_owner_access(request, token)
    raise HTTPException(
        status_code=409,
        detail={
            "code": "QUOTE_CACHE_BRAND_ID_NAMESPACE_REQUIRED",
            "message": "缓存仍缺少完整 brand_id 命名空间，已拒绝按品牌名清理以避免同名客户串删。",
            "priority": "P1",
            "retryable": False,
            "details": {"brand_id": session.get("brand_id"), "recovery": "regenerate_quote_without_cache_clear"},
        },
    )


@router.get("/keyword-selection/{token}/analysis")
async def get_behavior_analysis(token: str):
    """行为分析 + 犹豫度计算"""
    session = get_session_by_token(token)
    if not session:
        raise HTTPException(404, "会话不存在")

    logs = get_interaction_logs(session["id"])

    # 构建 keyword_id → keyword_text 映射（从快照中获取）
    keywords_snapshot = _safe_json(session.get("keywords_snapshot"), [])
    kw_text_map: dict[int, str] = {kw["id"]: kw["keyword"] for kw in keywords_snapshot}

    # 按关键词聚合犹豫度
    keyword_stats: dict[int, dict] = {}
    timeline = []

    for log in logs:
        kw_id = log.get("keyword_id")
        event_type = log.get("event_type", "")
        event_data = json.loads(log["event_data"]) if log.get("event_data") else {}
        kw_text = kw_text_map.get(kw_id, "") if kw_id else log.get("keyword_text")

        timeline.append({
            "time": log.get("created_at"),
            "type": event_type,
            "keyword_id": kw_id,
            "keyword_text": kw_text,
            "phase": log.get("phase"),
            "data": event_data,
        })

        if kw_id is not None:
            if kw_id not in keyword_stats:
                keyword_stats[kw_id] = {
                    "keyword_id": kw_id,
                    "keyword_text": kw_text_map.get(kw_id, ""),
                    "toggle_count": 0,
                    "hover_seconds": 0.0,
                    "view_count": 0,
                }

            ks = keyword_stats[kw_id]
            if event_type in ("keyword_select", "keyword_deselect", "price_keyword_deselect", "price_keyword_reselect"):
                ks["toggle_count"] += 1
            if event_type == "keyword_hover":
                ks["hover_seconds"] += event_data.get("duration_ms", 0) / 1000.0
                ks["view_count"] += 1  # 仅计算停留查看次数，不计切换

    # 计算犹豫度: 切换次数(反复选/取消) + 停留时长(长时间犹豫) + 停留次数(反复查看)
    hesitation_keywords = []
    for ks in keyword_stats.values():
        score = ks["toggle_count"] * 2 + ks["hover_seconds"] * 0.5 + ks["view_count"] * 1.0
        level = "果断" if score < 3 else ("思考中" if score < 8 else "高犹豫")
        hesitation_keywords.append({
            **ks,
            "hesitation_score": round(score, 1),
            "level": level,
        })

    hesitation_keywords.sort(key=lambda x: x["hesitation_score"], reverse=True)

    return {
        "session": {
            "status": session["status"],
            "visit_count": session.get("visit_count", 0),
            "created_at": session.get("created_at"),
            "keywords_submitted_at": session.get("keywords_submitted_at"),
            "confirmed_at": session.get("confirmed_at"),
        },
        "hesitation_keywords": hesitation_keywords,
        "timeline": timeline,
        "stats": {
            "total_events": len(logs),
            "high_hesitation_count": sum(1 for h in hesitation_keywords if h["level"] == "高犹豫"),
        },
    }


# ========== 销售确认 & 收款 ==========

class SalesConfirmRequest(BaseModel):
    final_price: float = Field(..., gt=0)
    discount_info: Optional[str] = None
    gift_keywords: Optional[List[str]] = None
    gift_articles: Optional[int] = 0
    sales_notes: Optional[str] = None
    # [服务期 SSOT 2026-08-06 §1.2] 服务月数**必填**,不再 `Optional = 1`。
    #   旧默认值把"销售压根没填"和"销售真的选了 1 个月"压成同一个值 —— 一份多月合同
    #   会被激活成 1 个月,一个月后自动监测静默停轮换,而客户界面还显示服务充足。
    service_months: int = Field(..., ge=1, le=24, description="服务月数 1-24 · 必填,系统不猜")
    service_start_date: Optional[str] = None  # YYYY-MM-DD


@router.post("/keyword-selection/{token}/sales-confirm")
async def sales_confirm_order(token: str, request: Request, req: SalesConfirmRequest):
    """销售核实确认订单 → 状态变为 pending_payment"""
    # [GEO-R1-CAN-130] owner 校验 · 防越权用他人 token 覆写 final_price / 赠送 / 服务期 / 销售备注
    session = _require_session_owner_access(request, token)
    if session["status"] != "confirmed":
        raise HTTPException(400, f"当前状态 {session['status']} 不允许销售确认，需先由客户确认")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # [GEO-R2-CAN-030] 先写服务期(quotes.service_months/start_date)· 失败 fail-closed raise ·
    #   此时 session 仍为 confirmed(未翻转)→ 销售可重试;绝不再"吞异常仍返 success",
    #   否则 session=pending_payment + final_price 已定,但 service_months 为空/旧值,
    #   下游 mark_paid 默认 1 个月激活多月订单(false success 引发的错交付)。
    # [服务期 SSOT 2026-08-06 §1.2] 这里写的是**服务期意向**(月数 + 起始日),
    #   不写 service_end_date —— (start, end) 这一对 SSOT 只在激活事务里成对落库,
    #   任何"只写一半"的路径都是新的分裂点。月数经 SSOT 校验,不再 `or 1`。
    from services.service_period import (
        ServicePeriodError,
        normalize_service_months,
    )
    try:
        _months = normalize_service_months(req.service_months)
    except ServicePeriodError as _spe:
        raise HTTPException(400, _spe.user_message)
    try:
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE quotes SET service_months = %s, service_start_date = %s
                WHERE id = %s
            """, (_months, req.service_start_date, session["quote_id"]))
            conn.commit()
            logger.info(f"已保存服务期意向: quote_id={session['quote_id']}, months={_months}, start={req.service_start_date}")
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.error(f"保存服务期失败(fail-closed·session 保持 confirmed 可重试): {e}")
        raise HTTPException(500, "保存服务期失败,请重试")

    # 服务期落库成功后再翻转 session → pending_payment(两写不同表·先 quotes 后 session·保证服务期不丢)
    update_session(
        token,
        status="pending_payment",
        final_price=req.final_price,
        discount_info=req.discount_info,
        gift_keywords=json.dumps(req.gift_keywords, ensure_ascii=False) if req.gift_keywords else None,
        gift_articles=req.gift_articles or 0,
        sales_notes=req.sales_notes,
        sales_confirmed_at=now,
    )

    try:
        _durable_selection_notice(
            brand_id=session["brand_id"], quote_id=session["quote_id"],
            event_type=NotificationEventType.PAYMENT_REMINDER,
            terminal_state="pending_payment",
            status="订单已确认，等待客户付款",
            summary="请在客户报价页面跟进收款。",
        )
    except Exception as e:
        logger.exception("待付款通知写入 outbox 失败: %s", e)

    return {"success": True, "status": "pending_payment"}


class MarkPaidRequest(BaseModel):
    confirm_code: str = ""


def _clear_quote_pending_keywords_and_clusters(quote_id: int):
    """[audit #17 2026-06-10] mark_paid 同步前清上次失败残留(幂等),防 fail-closed 重试时 save_*
    (纯 INSERT·非幂等)重复落库。只清 status='pending' 的 confirmed_keywords(未进写作/正式态)
    + 本 quote 的 keyword_clusters;先删 confirmed_keywords(其 cluster_id 引用 clusters)再删 clusters。
    清失败 raise(让同步 try fail-closed),绝不带残留重建导致词翻倍。"""
    from db.diagnosis_db import get_connection
    _conn = get_connection()
    try:
        _cur = _conn.cursor()
        _cur.execute("DELETE FROM confirmed_keywords WHERE quote_id = %s AND status = 'pending'", (quote_id,))
        # [audit #17 · 红队 P2 + #12 返修] clusters 只删【无任何子表引用】的 cluster ——
        #   对齐上面 confirmed_keywords 的 status='pending' 守卫,避免删父 cluster 撞 FK RESTRICT
        #   导致 clear 永久 500。两条 FK 子表都要避:
        #     (1) confirmed_keywords.cluster_id → keyword_clusters(id)
        #     (2) topics.cluster_id → keyword_clusters(id)  ← #12 Fable 实证漏过:17 个 titles_ready+
        #         topics 的 quote 重试时撞 topics_cluster_id_fkey 永久 500。
        _cur.execute(
            """
            DELETE FROM keyword_clusters
             WHERE quote_id = %s
               AND id NOT IN (
                   SELECT DISTINCT cluster_id FROM confirmed_keywords
                    WHERE quote_id = %s AND status <> 'pending' AND cluster_id IS NOT NULL
               )
               AND id NOT IN (
                   SELECT cluster_id FROM topics
                    WHERE quote_id = %s AND cluster_id IS NOT NULL
               )
            """,
            (quote_id, quote_id, quote_id),
        )
        _conn.commit()
    except Exception:
        try:
            _conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            _conn.close()
        except Exception:
            pass


@router.post("/keyword-selection/{token}/mark-paid")
async def mark_paid(token: str, request: Request, req: MarkPaidRequest = MarkPaidRequest()):
    """标记已收款 → 状态变为 active，自动创建写作任务

    confirm_code 验证已下线(Phase A · CTO-15.10 · 老板红线 2026-04-27):
        SaaS 化无差别一键激活 · 所有用户(含 admin)跳过 confirm_code
        审计仍走 audit + create_audit_log,保留可追溯性
    """
    # [GEO-R1-CAN-073 / GEO-R4-CAN-015] owner 校验 · 防越权用他人 pending_payment token
    #   一键激活服务 / 落库 confirmed_keywords(confirm_code 已按红线下线,object-owner 是唯一门)
    session = _require_session_owner_access(request, token)
    if session["status"] not in ("pending_payment", "payment_overdue"):
        raise HTTPException(400, f"当前状态 {session['status']} 不允许标记收款")

    # [audit #12 返修 P2 并发 2026-06-10] 串行化同一 quote 的 mark_paid:防双击 / 支付回调重放
    #   并发跑两次同步 → confirmed_keywords 重复落库(_clear 删 pending 后两个 runner 各 INSERT 一份)。
    #   session-level advisory lock(pg_advisory_lock(quote_id))持锁到函数结束;持锁后重检 session
    #   状态,已被并发请求激活则幂等返回。lock 随专用连接 finally 显式 unlock + close 释放。
    _ml_conn = get_connection()
    _ml_cur = _ml_conn.cursor()
    _ml_cur.execute("SELECT pg_advisory_lock(%s)", (int(session["quote_id"]),))
    try:
        # [audit #12 返修v2 · 老板升必须] 闸全量放行(100%)→ 并发安全顶格:advisory lock(quote 级)
        #   之外,再对 session 行 SELECT ... FOR UPDATE(同 _ml_conn · 持锁到函数结束 finally commit/close),
        #   双保险串行化读-改-写。FOR UPDATE 行锁关死 get_session_by_token 裸 SELECT 的双跑窗口;在锁内读
        #   权威 status 做幂等判定(并发第二个 runner 阻塞在此 → 待第一个释放后读到 active → 幂等返回)。
        _ml_cur.execute(
            "SELECT id, status FROM keyword_selection_sessions WHERE token = %s FOR UPDATE",
            (token,),
        )
        _lock_row = _ml_cur.fetchone()
        _locked_status = None
        if _lock_row:
            _locked_status = _lock_row["status"] if isinstance(_lock_row, dict) else _lock_row[1]
        if _locked_status == "active":
            logger.info(f"[mark_paid] quote={session['quote_id']} 已被并发请求激活 · 幂等返回(advisory lock + FOR UPDATE)")
            return {"success": True, "status": "active", "idempotent": True, "keywords_created": 0, "clusters_created": 0}
        _fresh_sess = get_session_by_token(token)
        session = _fresh_sess or session
        final_amount = session.get("confirmed_total_price") or session.get("final_price")
        if not final_amount or float(final_amount) <= 0:
            # 通知事实必须来自已确认的真实订单金额；在任何激活/同步写入前拒绝
            # 缺金额的旧会话，避免业务已提交后才因通知契约失败。
            raise HTTPException(409, "订单金额缺失，无法确认收款，请刷新后重试")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # [audit #17 2026-06-10] session 状态翻转(→active)已【后移】到关键词同步成功之后(见本函数末尾)。
        #   原先在此最早翻转 → 后续同步失败仅 log 不回滚 → session active 但 confirmed_keywords 空/不全 →
        #   再调 mark_paid 被「已 active」400 拒 → 永久空交付。现:同步成功才翻转,失败 raise 保 pending 可重试。

        # 激活服务期：根据 service_months 计算 service_end_date
        # FIX (Phase A · 2026-04-27): get_db_connection() 是历史 typo · 应为 get_connection
        # 之前一直在 except 静默失败 · 现在修对(SaaS 化激活路径开始用)
        # [CTO-15.23 2026-05-10 Deploy-CTO P0-2.2 加固]
        #   老路径: 任意一步抛异常 → 直接进外层 except · conn 不 rollback 不 close ·
        #           __del__ GC 兜底归还但 in_failed_state · 池里下个 request 拿到立即 InFailedSqlTransaction
        #   新路径: 内层 try/except/finally · 失败 rollback + logger + finally 强 close 归还
        # [服务期 SSOT 2026-08-06 §1.2] 激活事务是 (service_start_date, service_end_date)
        #   这一对 SSOT 的**唯一写入点**,两列必须同一条 UPDATE 一起落库。
        #   两处根治:
        #     ① `months or 1` 拔掉 —— 换算走 resolve_activation_period(缺失/非法抛错进人工);
        #     ② 原来整块的 except 只 logger.error 就放行 → 服务期没写成也照样把 session
        #        翻成 active、关键词照样进写作大厅,客户界面显示在期而轮换闸永远看不到这张单。
        #        现在 fail-closed raise:此时 session 仍是 pending_payment(翻转在本函数末尾),
        #        销售可重试,不留"付了款没服务期"的半激活单。
        from services.service_period import (
            ServicePeriodError,
            resolve_activation_period,
        )
        try:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT service_months, service_start_date FROM quotes WHERE id = %s", (session["quote_id"],))
                row = cur.fetchone()
                if not row:
                    raise ServicePeriodError("报价单不存在,无法激活服务期")
                _months_raw = row.get("service_months") if hasattr(row, "get") else row[0]
                _start_raw = row.get("service_start_date") if hasattr(row, "get") else row[1]
                start_date, end_date = resolve_activation_period(
                    start_date=_start_raw, months=_months_raw
                )
                cur.execute("""
                    UPDATE quotes SET service_status = 'active', service_start_date = %s, service_end_date = %s
                    WHERE id = %s
                """, (start_date, end_date, session["quote_id"]))
                conn.commit()
                logger.info(f"服务期已激活: quote_id={session['quote_id']}, {start_date} ~ {end_date}")
            except Exception:
                try:
                    conn.rollback()
                except Exception as re:
                    logger.warning(f"[mark_paid 激活服务期] rollback 失败 (连接已废): {re}")
                raise
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except ServicePeriodError as _spe:
            logger.error(f"激活服务期被拒(fail-closed·session 保持 pending 可重试): {_spe}")
            raise HTTPException(400, _spe.user_message)
        except Exception as e:
            logger.error(f"激活服务期失败(fail-closed·session 保持 pending 可重试): {e}")
            raise HTTPException(500, "激活服务期失败,请重试")

        # [audit #12 返修 2026-06-10] quote.status='confirmed' 推送 + paid_at/paid_amount 补写
        #   已【后移】到关键词同步成功之后(见本函数末尾 update_session 前)。
        #   原先在此(同步前)独立 try 推 confirmed 并 commit → 若随后同步 fail-closed(raise 500),
        #   quote.status 残留 'confirmed' 但 confirmed_keywords 空 → 写作大厅(WHERE status IN
        #   ('confirmed','paid'))显示空交付(prod quote 268 实证)。现:与 session 翻转同进退,
        #   同步成功才推 quote.status;失败则 quote 维持原状(draft)+ session 维持 pending 可重试。

        # 将关键词同步到 confirmed_keywords 表（写作大厅数据源）
        kw_count = 0
        cluster_count = 0
        try:
            from db.diagnosis_db import save_confirmed_keywords, update_quote_status, save_keyword_cluster
            # [audit #17 2026-06-10] 幂等:同步前清上次失败残留(pending confirmed_keywords + 本 quote clusters),
            #   防 fail-closed 重试时 save_*(纯 INSERT·非幂等)导致关键词/主题包重复落库。
            _clear_quote_pending_keywords_and_clusters(session["quote_id"])
            # M1b (CTO-15.17 · 2026-04-26):客户确认付款 · 此处同步 6 层分类 → 持久化到 confirmed_keywords.layer
            # · 没 layer 时 GET /api/quotes/{id} on-the-fly 兜底 · 持久化为锦上添花(免每次 GET 计算)
            # · brand context 从 quotes 表反查(api/selection_api.py 路径无直接访问)
            try:
                from services.keyword_layer_classifier import classify_keyword_layers_batch as _classify_layers
            except Exception as _layer_import_error:
                logger.warning(f"关键词层级分类器不可用，跳过 layer 注入: {_layer_import_error}")
                _classify_layers = None
            _quote_brand_ctx = {"brand_name": "", "industry": "", "city": "", "competitors": []}
            try:
                from db.diagnosis_db import get_quote as _get_q
                _q = _get_q(session["quote_id"])
                if _q:
                    _quote_brand_ctx["brand_name"] = (_q.get("brand_name") or "").strip()
                    _quote_brand_ctx["industry"] = (_q.get("industry") or "").strip()
                    _quote_brand_ctx["city"] = (_q.get("city") or "").strip()
                    _bid = _q.get("brand_id")
                    if _bid:
                        try:
                            from db.diagnosis_db import get_connection as _gc
                            _bc = _gc()
                            try:
                                _bcur = _bc.cursor()
                                _bcur.execute(
                                    "SELECT competitors_jsonb FROM brands WHERE id = %s", (_bid,)
                                )
                                _br = _bcur.fetchone()
                                if _br:
                                    _raw = _br.get("competitors_jsonb") if isinstance(_br, dict) else _br[0]
                                    if isinstance(_raw, str):
                                        try:
                                            _raw = json.loads(_raw)
                                        except (ValueError, TypeError):
                                            _raw = []
                                    if isinstance(_raw, list):
                                        for _c in _raw:
                                            if isinstance(_c, dict) and _c.get("name"):
                                                _quote_brand_ctx["competitors"].append(str(_c["name"]).strip())
                                            elif isinstance(_c, str) and _c.strip():
                                                _quote_brand_ctx["competitors"].append(_c.strip())
                            finally:
                                try:
                                    _bc.close()
                                except Exception:
                                    pass
                        except Exception:
                            pass
            except Exception:
                pass

            def _attach_layer(_kw_list: list[dict]) -> list[dict]:
                """对 kw_list 内每个 dict 注入 layer / layer_reason / layer_confidence"""
                if not _kw_list:
                    return _kw_list
                _names = [_kw.get("keyword", "") for _kw in _kw_list if _kw.get("keyword")]
                if not _names or _classify_layers is None:
                    return _kw_list
                _layer_map = _classify_layers(
                    _names,
                    brand_name=_quote_brand_ctx["brand_name"],
                    city=_quote_brand_ctx["city"],
                    industry=_quote_brand_ctx["industry"],
                    competitors=_quote_brand_ctx["competitors"],
                )
                for _kw in _kw_list:
                    _name = _kw.get("keyword", "")
                    _res = _layer_map.get(_name)
                    if _res:
                        _kw.setdefault("layer", _res["layer"])
                        _kw.setdefault("layer_reason", _res["reason"])
                        _kw.setdefault("layer_confidence", _res["confidence"])
                return _kw_list

            tier = session.get("selected_tier", "standard")
            clusters_data = _safe_json(session.get("clusters_data"))
            final_ids_for_monitoring = _safe_json(session.get("final_keyword_ids"), [])

            if clusters_data and clusters_data.get("clusters"):
                # ── Cluster mode: 写入 keyword_clusters + confirmed_keywords ──
                for cl in clusters_data["clusters"]:
                    if not cl.get("is_selected"):
                        continue
                    cluster_id = save_keyword_cluster(session["quote_id"], token, cl)
                    cluster_count += 1

                    kw_list = []
                    # 核心词 → is_core=True，写入写作大厅
                    for kw in cl.get("core_keywords", []):
                        if not kw.get("is_selected"):
                            continue
                        tier_data = kw.get(tier, {})
                        kw_list.append({
                            "keyword": kw["keyword"],
                            "category": cl.get("business_tag", "通用词"),
                            "tier": tier,
                            "base_price": tier_data.get("price", 0),
                            "city_premium": 1.0,
                            "final_price": tier_data.get("price", 0),
                            "competitor_count": 0,
                            # [P1 容量合同 2026-08-08] 冻结的是**可交付容量上限**,不是必须完成量。
                            #   走 SSOT 规整:显式 0 保留;缺失时兜底值 = 具名常量(与上线前同值 1)。
                            "required_articles": _normalize_article_capacity(
                                tier_data.get("articles"), when_missing=_CAPACITY_MISSING_DEFAULT),
                            "intent": kw.get("intent", "informational"),
                            "funnel_stage": kw.get("funnel_stage", "awareness"),
                            "cluster_id": cluster_id,
                            "is_core": True,
                            "super_red_ocean": bool(kw.get("super_red_ocean", False)),  # §4.2 达标排除
                        })

                    # 真实覆盖词 → is_core=False，仅追踪不生产内容
                    for kw in cl.get("covered_keywords", []):
                        if kw.get("source") == "generated_variant":
                            continue  # 生成变体不写入 DB
                        kw_list.append({
                            "keyword": kw["keyword"],
                            "category": cl.get("business_tag", "通用词"),
                            "tier": tier,
                            "base_price": 0,
                            "final_price": 0,
                            "competitor_count": 0,
                            "required_articles": 0,
                            "intent": "informational",
                            "funnel_stage": "awareness",
                            "cluster_id": cluster_id,
                            "is_core": False,
                        })

                    if kw_list:
                        # M1b · 注入 6 层 layer 字段
                        save_confirmed_keywords(session["quote_id"], _attach_layer(kw_list))
                        kw_count += len(kw_list)

                # 未分组词（自定义词等未进入任何主题包的核心词）
                for uk in clusters_data.get("unclustered_keywords", []):
                    uk_tier_data = uk.get(tier, {})
                    if uk_tier_data.get("price", 0) > 0:
                        # M1b · 注入 6 层
                        save_confirmed_keywords(session["quote_id"], _attach_layer([{
                            "keyword": uk["keyword"],
                            "category": "自定义",
                            "tier": tier,
                            "base_price": uk_tier_data.get("price", 0),
                            "final_price": uk_tier_data.get("price", 0),
                            "competitor_count": 0,
                            # [P1 容量合同 2026-08-08] 同上:容量上限口径,走 SSOT 规整
                            "required_articles": _normalize_article_capacity(
                                uk_tier_data.get("articles"), when_missing=_CAPACITY_MISSING_DEFAULT),
                            "intent": uk.get("intent", "informational"),
                            "funnel_stage": uk.get("funnel_stage", "awareness"),
                            "is_core": True,
                            "super_red_ocean": bool(uk.get("super_red_ocean", False)),  # §4.2 达标排除
                        }]))
                        kw_count += 1
            else:
                # ── Flat mode (backward compat) ──
                pricing_data = _safe_json(session.get("pricing_data"), {})
                pricing_data = _sync_pricing_tiers_from_config(pricing_data)  # r12: 实时覆盖老 cached ai_probability
                final_ids = set(_safe_json(session.get("final_keyword_ids"), []))
                gift_keywords = _safe_json(session.get("gift_keywords"), [])

                kw_list = []
                for pk in pricing_data.get("keywords", []):
                    if pk["id"] in final_ids:
                        tier_data = pk.get(tier, {})
                        kw_list.append({
                            "keyword": pk["keyword"],
                            "category": pk.get("category_label", "通用词"),
                            "tier": tier,
                            "base_price": tier_data.get("price", 0),
                            "city_premium": 1.0,
                            "final_price": tier_data.get("price", 0),
                            "competitor_count": 0,
                            # [P1 容量合同 2026-08-08] 冻结的是**可交付容量上限**,不是必须完成量。
                            #   走 SSOT 规整:显式 0 保留;缺失时兜底值 = 具名常量(与上线前同值 1)。
                            "required_articles": _normalize_article_capacity(
                                tier_data.get("articles"), when_missing=_CAPACITY_MISSING_DEFAULT),
                            "intent": pk.get("intent", "informational"),
                            "funnel_stage": pk.get("funnel_stage", "awareness"),
                            "super_red_ocean": bool(pk.get("super_red_ocean", False)),  # §4.2 达标排除
                        })

                # [P0 2026-06-05 老板定稿] gift_keywords 不再写入 confirmed_keywords(根治"不计价核心词后门")
                #   旧:附赠词以 price=0 写 confirmed_keywords · save_confirmed_keywords 默认 is_core=True
                #   → 进核心词/监测/写作链 = 不计价即获监测,违「想监测须作为已选核心词单独计价」模型。
                #   现:gift_keywords 仅作 session 级【服务备注】(display 读 session.gift_keywords)· 不进 confirmed_keywords。
                #   额外服务必须走已选核心词或单独加购。

                if kw_list:
                    # M1b · 注入 6 层 layer 字段
                    save_confirmed_keywords(session["quote_id"], _attach_layer(kw_list))
                    kw_count = len(kw_list)

            # 如果 confirmed_keywords 是在收款阶段才落库，客户确认阶段的标记可能尚无目标行。
            # 这里在落库后再按 session-local id -> keyword text 兜底标记一次。
            _mark_keywords_monitored(
                session["quote_id"],
                final_ids_for_monitoring,
                keywords_snapshot=session.get("keywords_snapshot"),
                pricing_data=session.get("pricing_data"),
                clusters_data=clusters_data,
            )

            # update_quote_status 已移到独立 try block(L2245 附近)· 此处不再重复推
            logger.info(f"已为 quote_id={session['quote_id']} 创建 {kw_count} 个写作任务"
                         + (f"（{cluster_count} 个主题包）" if cluster_count else ""))
        except HTTPException:
            raise
        except Exception as e:
            # [audit #17 2026-06-10] fail-closed:关键词同步失败 → 不翻转 session(保持 pending_payment 可重试)
            #   + raise 500,绝不再静默 log 让 session 停在「active 但空交付」。重试时 clear 残留幂等续做。
            logger.error(f"[mark_paid] 关键词同步失败 fail-closed(状态未翻转·可重试) quote_id={session['quote_id']}: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="订单激活未完成(关键词同步失败),请重试;若反复失败请联系客服。",
            )

        # [audit #12 返修 2026-06-10] 同步成功 → 此刻才推 quote.status='confirmed' + paid_at/paid_amount。
        #   与下方 session 翻转同进退:确保 quotes.status 进 confirmed 时 confirmed_keywords 已落库,
        #   写作大厅(WHERE status IN ('confirmed','paid'))绝不再出现「状态 confirmed 但 0 词」空交付。
        #   独立连接 + inner try/except/finally(失败 rollback + close 防泄漏/in_failed_state)。
        try:
            from db.diagnosis_db import update_quote_status as _uqs
            _uqs(session["quote_id"], "confirmed")
            _conn = get_connection()
            try:
                _cur = _conn.cursor()
                _cur.execute(
                    """
                    UPDATE quotes
                       SET paid_at = COALESCE(paid_at, %s::timestamp),
                           paid_amount = COALESCE(paid_amount, %s)
                     WHERE id = %s
                    """,
                    (now, session.get("confirmed_total_price"), session["quote_id"]),
                )
                _conn.commit()
                logger.info(
                    f"quote.status -> confirmed + paid_at/paid_amount 已补(同步成功后): quote_id={session['quote_id']}"
                )
            except Exception as inner_qs_err:
                logger.error(
                    f"!!! 推送 quote.paid_at/paid_amount SQL 失败 (rollback) quote_id={session['quote_id']}: {inner_qs_err}",
                    exc_info=True,
                )
                try:
                    _conn.rollback()
                except Exception as re:
                    logger.warning(f"[mark_paid quote.status 推送] rollback 失败 (连接已废): {re}")
                # [GEO-R2-CAN-015] 不吞 · 上抛到外层做 fail-closed(见下),否则 quote.status 未落 confirmed
                #   但 session 仍会翻 active → 写作大厅(WHERE status IN ('confirmed','paid'))空交付。
                raise
            finally:
                try:
                    _conn.close()
                except Exception:
                    pass
        except HTTPException:
            raise
        except Exception as _qs_err:
            logger.error(
                f"!!! 推送 quote.status 失败 (外层) quote_id={session['quote_id']}: {_qs_err}",
                exc_info=True,
            )
            # [GEO-R2-CAN-015] fail-closed:quote.status/paid_at 未落库 → 绝不翻转 session 为 active。
            #   保持 pending_payment 可重试(下次 _clear 幂等清残留后续做),对齐关键词同步的 fail-closed 口径。
            raise HTTPException(
                status_code=500,
                detail="订单激活未完成(报价状态同步失败),请重试;若反复失败请联系客服。",
            )

        # [audit #17 2026-06-10] 同步成功 → 此刻才翻转 session 为 active(收款完成·交付就绪)
        # [audit #12 返修v3 · Fable 自死锁修] 必须用 _ml_conn(持 FOR UPDATE 行锁的同一连接同一事务)做翻转,
        #   不能调 update_session(它另开池连接 UPDATE 同一 session 行 → 等本连接 FOR UPDATE 行锁 →
        #   单请求自死锁 · PG 检测不到[应用层等]· statement/lock_timeout=0 → 永久挂 → 池耗尽 mark_paid 全瘫)。
        #   同事务内对自己锁的行 UPDATE 合法;在 finally 的 commit 一起落库 + 释放行锁/advisory lock。
        #   字段对齐 update_session(status='active' 业务字段会刷 updated_at)。
        _ml_cur.execute(
            "UPDATE keyword_selection_sessions SET status = 'active', payment_received_at = %s, updated_at = %s WHERE token = %s",
            (now, now, token),
        )

        # Durable zero-cost sidecar; the savepoint isolates every failure from
        # the commercial transaction and the reconciler can repair omissions.
        try:
            from services.article_delivery_plan import enqueue_quote_event_in_transaction_if_enabled

            enqueue_quote_event_in_transaction_if_enabled(
                _ml_cur,
                int(session["quote_id"]),
                "quote_paid_standard",
            )
        except Exception as plan_event_error:
            logger.warning(
                "[mark_paid] article plan event bypassed without affecting payment quote=%s: %s",
                session["quote_id"],
                plan_event_error,
            )

        brand_name = _get_brand_name(session["brand_id"])
        enqueue_brand_owner_notification_event(
            _ml_cur,
            brand_id=int(session["brand_id"]),
            event_type=NotificationEventType.PAYMENT_RECEIVED,
            business_id=f"quote:{int(session['quote_id'])}",
            terminal_state="paid",
            facts={
                "business_no": f"QUOTE-{int(session['quote_id'])}",
                "amount": f"{float(final_amount):,.2f} 元",
                "status": "已确认收款，订单已生效",
                "occurred_at": now,
                "summary": (
                    f"{cluster_count} 个主题包、{kw_count} 个核心词已进入写作大厅。"
                    if cluster_count else f"{kw_count} 个关键词已进入写作大厅。"
                ),
            },
        )

        # 审计日志：标记收款成功
        summary_parts = [f"品牌:{brand_name}", f"关键词:{kw_count}个"]
        if cluster_count:
            summary_parts.append(f"主题包:{cluster_count}个")
        audit(request, "mark_paid", "quote", entity_type="quote",
              entity_id=session.get("quote_id"),
              summary=f"使用确认码标记收款成功 ({', '.join(summary_parts)})")

        return {"success": True, "status": "active", "keywords_created": kw_count, "clusters_created": cluster_count}
    finally:
        try:
            _ml_cur.execute("SELECT pg_advisory_unlock(%s)", (int(session["quote_id"]),))
            _ml_conn.commit()
        except Exception:
            pass
        try:
            _ml_conn.close()
        except Exception:
            pass


@router.get("/keyword-selection/{token}/order-details")
async def get_order_details(token: str, request: Request):
    """获取订单核实详情（销售端用）"""
    # [GEO-R1-CAN-134] owner 校验 · 这是销售端端点,返回内部商业字段
    #   (sales_notes / discount_info / gift_* / final_price),仅 admin/创建者/品牌归属者可读
    session = _require_session_owner_access(request, token)

    pricing_data = _safe_json(session.get("pricing_data"))
    pricing_data = _sync_pricing_tiers_from_config(pricing_data)  # r12: 实时覆盖老 cached ai_probability
    clusters_data = _safe_json(session.get("clusters_data"))
    tier = session.get("selected_tier", "standard")
    final_ids = set(_safe_json(session.get("final_keyword_ids"), []))

    # 计算客户确认的价格
    client_price = session.get("confirmed_total_price", 0)

    # ── Cluster mode: 按主题包组织数据 ──
    clusters_summary = None
    if clusters_data and clusters_data.get("clusters"):
        clusters_summary = []
        for cl in clusters_data["clusters"]:
            if not cl.get("is_selected"):
                continue
            core_kws = [kw for kw in cl.get("core_keywords", []) if kw.get("is_selected")]
            tier_pricing = cl.get("pricing", {}).get(tier, {})
            clusters_summary.append({
                "cluster_name": cl.get("cluster_name", ""),
                "business_tag": cl.get("business_tag", ""),
                "city_tag": cl.get("city_tag", ""),
                "scenario_tag": cl.get("scenario_tag", ""),
                "core_keyword_count": len(core_kws),
                "covered_keyword_count": cl.get("covered_keyword_count", 0),
                "core_keywords": [
                    {"keyword": kw["keyword"], "price": kw.get(tier, {}).get("price", 0), "articles": kw.get(tier, {}).get("articles", 0),
                     "super_red_ocean": bool(kw.get("super_red_ocean", False))}
                    for kw in core_kws
                ],
                "price": tier_pricing.get("core_price", 0),
                "full_price": tier_pricing.get("full_price", 0),
                "savings": tier_pricing.get("savings", 0),
                "articles": tier_pricing.get("core_articles", 0),
            })

    # ── Flat mode: 选中的关键词详情 ──
    selected_keywords = []
    if pricing_data:
        for pk in pricing_data.get("keywords", []):
            if pk["id"] in final_ids:
                tier_data = pk.get(tier, {})
                selected_keywords.append({
                    "keyword": pk["keyword"],
                    "category_label": pk.get("category_label", ""),
                    "price": tier_data.get("price", 0),
                    "articles": tier_data.get("articles", 0),
                    "super_red_ocean": bool(pk.get("super_red_ocean", False)),  # §4.2 代理端风险提示
                })

    # 计算已等待天数（pending_payment 状态）
    days_waiting = 0
    if session.get("sales_confirmed_at") and session["status"] in ("pending_payment", "payment_overdue"):
        confirmed_dt = datetime.fromisoformat(session["sales_confirmed_at"])
        days_waiting = (datetime.now() - confirmed_dt).days

    # 计算汇总数
    keyword_count = len(selected_keywords)
    cluster_count = 0
    core_keyword_count = 0
    if clusters_summary:
        cluster_count = len(clusters_summary)
        core_keyword_count = sum(c["core_keyword_count"] for c in clusters_summary)
        keyword_count = core_keyword_count  # 核心词数量作为关键词数

    return {
        "status": session["status"],
        "tier": tier,
        "tier_label": TIER_CONFIG.get(tier, {}).get("label", tier),
        "client_price": client_price,
        "final_price": session.get("final_price"),
        "discount_info": session.get("discount_info"),
        "gift_keywords": _safe_json(session.get("gift_keywords"), []),
        "gift_articles": session.get("gift_articles", 0),
        "sales_notes": session.get("sales_notes"),
        "selected_keywords": selected_keywords,
        "keyword_count": keyword_count,
        "clusters": clusters_summary,
        "cluster_count": cluster_count,
        "core_keyword_count": core_keyword_count,
        "confirmed_at": session.get("confirmed_at"),
        "sales_confirmed_at": session.get("sales_confirmed_at"),
        "payment_received_at": session.get("payment_received_at"),
        "days_waiting": days_waiting,
    }


# ========== 辅助函数 ==========

CATEGORY_LABELS = {
    "question": "问答词",
    "price": "价格词",
    "location": "地域词",
    "longtail": "长尾词",
    "brand": "品牌词",
    "comparison": "对比词",
    "general": "通用词",
    "custom": "自定义",
    "informational": "信息词",
    "transactional": "交易词",
    "commercial": "商业词",
}


def _category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category)


def _difficulty_level(kw: dict) -> int:
    """竞争度 1-5"""
    count = kw.get("competitor_count", 0) or 0
    if count >= 40:
        return 5
    elif count >= 20:
        return 4
    elif count >= 10:
        return 3
    elif count >= 5:
        return 2
    return 1


def _recommendation_reason(kw: dict) -> str:
    """
    生成关键词推荐理由（面向客户）

    逻辑：先检测关键词模式 → 匹配到最相关的推荐理由
    文案风格：专业但易懂，说明"为什么这个词对你有价值"
    """
    text = kw.get("keyword", "")

    # S级 — 排名榜单词（转化价值最高）
    if any(w in text for w in ("十大", "排行榜", "排行", "排名", "TOP")):
        return "榜单类搜索词，AI引擎回答时必定列出品牌清单——出现在清单中即获得高质量曝光"
    if any(w in text for w in ("哪个品牌好", "哪个牌子好", "什么品牌好", "什么牌子好")):
        return "品牌选择类搜索，AI回答会直接推荐具体品牌，命中即获得精准客户"
    if any(w in text for w in ("品牌推荐", "品牌排行", "牌子推荐")):
        return "直接品牌推荐需求，AI回答覆盖率高，ROI排名前列的词类"

    # A级 — 决策阶段词
    if any(w in text for w in ("推荐", "值得买", "值得入手")):
        return "决策阶段搜索词——用户已有购买意向，此时被AI推荐转化率最高"
    if any(w in text for w in ("怎么选", "怎么挑", "如何选", "如何挑")):
        return "选型指导需求，AI会给出结构化推荐，适合在回答中植入品牌优势"
    if any(w in text for w in ("哪个好", "哪家好", "什么好", "哪款好")):
        return "对比决策类搜索，用户正在做最终选择——AI推荐直接影响购买决定"
    if any(w in text for w in ("用什么", "买什么", "买哪个", "买哪款")):
        return "即时购买意图，用户搜完即下单，GEO曝光直接带来转化"
    if "性价比" in text:
        return "价值导向搜索，AI按档位推荐品牌，适合突出产品差异化定位"

    # B级 — 专业/技术类词
    tech_words = ("型号", "参数", "规格", "选型", "配置", "方案", "技术")
    if any(w in text for w in tech_words):
        return "专业搜索词——您的目标客户是懂行的决策者，这类精准词转化质量极高"
    if any(w in text for w in ("解决方案", "应用", "案例", "场景")):
        return "场景/方案类搜索，目标客户在评估具体解决方案，AI推荐具有高参考权重"

    # B级 — 避坑选购词
    if any(w in text for w in ("避坑", "避雷", "注意事项", "坑")):
        return "避坑类搜索，AI在列完注意事项后会推荐可信赖的品牌作为安全之选"
    if any(w in text for w in ("区别", "对比", "vs")):
        return "对比分析需求，AI完成优劣分析后会给出推荐结论，品牌植入自然有效"
    if any(w in text for w in ("便宜", "贵不贵", "多少钱", "价位", "价格", "费用", "收费")):
        return "价格调研搜索，AI按价格区间推荐品牌，覆盖高中低端客户群"

    # 地域词
    geo_markers = ("省", "市", "区", "县", "镇", "哪里", "附近", "本地", "当地")
    cities = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "南京",
              "苏州", "天津", "重庆", "西安", "长沙", "东莞", "佛山", "厦门"]
    if any(g in text for g in geo_markers) or any(c in text for c in cities):
        return "地域精准词——锁定本地客户搜索流量，竞争度低于全国词，性价比高"

    # 品牌/牌子通用匹配
    if any(w in text for w in ("品牌", "牌子")):
        return "品牌意图搜索，AI回答大概率列出品牌清单，曝光价值高"

    # 按 intent 兜底
    intent = kw.get("intent", "informational")
    if intent == "transactional":
        return "高转化意图词——用户有明确购买需求，GEO曝光直接驱动成交"
    elif intent == "commercial":
        return "商业调研词——用户在做购买前的最后调研，被AI推荐容易促成转化"
    return "行业认知词——通过专业内容建立品牌权威度，为后续转化蓄力"


def _find_category_label(keyword_text: str, snapshot: list) -> str:
    for kw in snapshot:
        if kw.get("keyword") == keyword_text:
            return kw.get("category_label", "通用词")
    return "通用词"


def _find_recommendation_reason(keyword_text: str, snapshot: list) -> str:
    for kw in snapshot:
        if kw.get("keyword") == keyword_text:
            return kw.get("recommendation_reason", "")
    return ""


def _generate_selection_context(keywords: list[dict], brand_name: str = "") -> dict:
    """
    从关键词列表推理客户画像和选词方法论（面向客户展示）

    纯规则推理，无LLM调用，根据关键词特征判断目标客户类型
    """
    import re

    kw_texts = [kw.get("keyword", "") for kw in keywords]
    total = len(keywords)

    # ---- 推理客户画像 ---- 逐词打分，统计每个类别命中的词条数
    tech_kw_count = 0
    consumer_kw_count = 0
    b2b_kw_count = 0

    # tech: 硬技术词（型号、参数、编程等专业术语）
    tech_signals = ["型号", "参数", "规格", "选型", "配置", "编程", "调试",
                    "安装", "维修", "接线", "原理", "图纸", "手册", "说明书",
                    "仪器", "工艺", "材料", "加工", "伺服", "变频", "传感"]
    # b2b_entity: 强B2B实体词 — 含这些词说明在找企业/供应商，优先级最高
    b2b_entity_signals = ["服务商", "供应商", "厂家", "集成商", "工程商", "渠道商",
                          "代理商", "经销商", "制造商", "生产商"]
    # b2b_action: B2B行为词
    b2b_action_signals = ["解决方案", "外包", "定制开发", "批发", "OEM",
                          "招标", "采购", "代工", "贴牌", "工程"]
    # consumer: 消费端查询模式词（推荐/哪家好等常作为查询修饰语）
    consumer_signals = ["推荐", "哪家好", "排名", "怎么选", "避坑", "性价比",
                        "多少钱", "便宜", "好用", "值得买", "价格", "费用",
                        "口碑", "评价", "十大", "排行", "效果图",
                        "怎么样", "靠谱", "划算"]

    # 型号正则：英文+数字组合(>=3字符)，如 ACS580, S7-1200, MR-J4
    model_pattern = re.compile(r'[A-Za-z]+[\-]?\d[\w\-]*|[\d]+[A-Za-z]+[\w\-]*')

    for kw_text in kw_texts:
        has_b2b_entity = any(s in kw_text for s in b2b_entity_signals)
        has_b2b_action = any(s in kw_text for s in b2b_action_signals)
        has_tech = any(s in kw_text for s in tech_signals)
        has_consumer = any(s in kw_text for s in consumer_signals)
        has_model = bool(model_pattern.search(kw_text))

        # 优先级: B2B实体词 > 技术型号 > 技术术语 > B2B行为词 > 消费端修饰词
        # 含"供应商推荐" → B2B（实体词优先于修饰词）
        # 含"ACS580参数" → Tech（型号+术语）
        # 含"全屋定制推荐" → Consumer
        if has_b2b_entity:
            b2b_kw_count += 1
        elif has_model or (has_tech and not has_consumer):
            tech_kw_count += 1
        elif has_b2b_action and not has_consumer:
            b2b_kw_count += 1
        elif has_tech and has_consumer:
            tech_kw_count += 1  # "设备选型怎么选" → 偏技术
        elif has_b2b_action and has_consumer:
            b2b_kw_count += 1  # "外包哪家好" → 偏B2B
        elif has_consumer:
            consumer_kw_count += 1
        # else: 无信号，不计入

    # 判定：用词条占比（>=40%）或绝对数（>=2）双重阈值，取较容易满足的
    tech_ratio = tech_kw_count / max(total, 1)
    consumer_ratio = consumer_kw_count / max(total, 1)
    b2b_ratio = b2b_kw_count / max(total, 1)

    def _dominant(count, ratio, abs_min=2, ratio_min=0.4):
        return count >= abs_min or ratio >= ratio_min

    if _dominant(tech_kw_count, tech_ratio) and tech_kw_count > consumer_kw_count:
        persona_type = "专业技术人员"
        persona_desc = "您的目标客户是具备专业知识的技术决策者，他们搜索时倾向于使用具体的技术术语、型号参数和选型关键词"
        search_behavior = "精准搜索：直接搜型号、参数、技术对比，很少搜泛词"
        decision_journey = "技术调研 → 参数对比 → 选型评估 → 询价采购"
    elif _dominant(b2b_kw_count, b2b_ratio) and b2b_kw_count > consumer_kw_count:
        persona_type = "B端企业采购决策者"
        persona_desc = "您的目标客户是企业采购负责人或业务决策者，他们在寻找可靠的合作伙伴和解决方案"
        search_behavior = "场景搜索：搜行业+解决方案、服务商评价、成功案例"
        decision_journey = "需求确认 → 服务商筛选 → 方案对比 → 商务洽谈"
    elif _dominant(consumer_kw_count, consumer_ratio) and consumer_kw_count > tech_kw_count:
        persona_type = "C端消费者"
        persona_desc = "您的目标客户是有明确购买需求的终端消费者，他们在做购买决策前的最后调研"
        search_behavior = "泛搜索：搜推荐、排名、怎么选、避坑指南"
        decision_journey = "产生需求 → 搜索对比 → 看推荐/评价 → 下单"
    else:
        persona_type = "混合型客户"
        persona_desc = "您的目标客户既有终端消费者也有企业级客户，关键词策略兼顾两端"
        search_behavior = "混合搜索：既有泛搜推荐词，也有精准场景词"
        decision_journey = "多元渠道触达 → 内容种草 → 深度了解 → 成交转化"

    # ---- 统计商业意图占比 ----
    # 优先用 intent 字段；若多数为空则从文本推断
    commercial_keywords = 0
    commercial_text_signals = ["哪家好", "推荐", "怎么选", "靠谱", "排名", "十大",
                               "价格", "多少钱", "费用", "报价", "供应商", "厂家",
                               "服务商", "采购", "招标"]
    for kw in keywords:
        intent = kw.get("intent", "")
        if intent in ("commercial", "transactional"):
            commercial_keywords += 1
        elif not intent or intent == "informational":
            # intent缺失或信息型 → 从文本推断是否有商业意图
            kw_text = kw.get("keyword", "")
            if any(s in kw_text for s in commercial_text_signals):
                commercial_keywords += 1
    commercial_pct = int(commercial_keywords / max(total, 1) * 100)

    # ---- 构建方法论说明 ----
    methodology_steps = [
        "市场热度分析 — 分析真实搜索量、SEM竞价出价和竞争企业数，确认市场需求真实存在",
        "AI搜索竞争度 — 抓取当前AI搜索结果，分析竞品内容分布和权威度",
        "智能意图分析 — 判断每个词的搜索意图（了解/对比/购买）和转化漏斗阶段",
    ]

    return {
        "customer_persona": {
            "type": persona_type,
            "description": persona_desc,
            "search_behavior": search_behavior,
            "decision_journey": decision_journey,
        },
        "methodology": {
            "steps": methodology_steps,
            "summary": f"基于三维数据交叉验证，从{total}个候选词中筛选出最匹配您目标客户搜索习惯的关键词，其中{commercial_pct}%具有直接商业转化潜力。",
        },
    }

