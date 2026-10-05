"""geo_observation_* 低层 DB 访问(账本 repository)。

只做 events/signals/contributor_buckets 的 CRUD + WORKERS=4 claim/lease CAS + 撤回。
不含聚合 repository(AI-3 独占)。不含 policy CAS(见 policy.py)。业务决策在 promotion.py。

并发正确性(spec §10):
- claim 用 FOR UPDATE SKIP LOCKED + lease_token + lease_until;
- finish 只认当前 token 的 CAS(旧 worker 复活零写);
- 幂等门 = source_event_key(SHA-256)+ 业务四元组 UNIQUE;
- Redis 只做通知/缓存,不承担 exactly-once(本模块不依赖 Redis)。
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any, Optional

from psycopg2.extras import Json

from db.connection import get_connection
from . import SCHEMA_VERSION

DEFAULT_LEASE_SECONDS = 300

# events 可插入列(event_uuid/source_event_key 由本层生成/计算)
_EVENT_INSERT_COLS = [
    "event_uuid", "schema_version", "source_type", "source_table", "source_record_id",
    "source_subkey", "source_event_key", "owner_user_id", "brand_id", "industry_key",
    "prompt_fingerprint", "platform_key", "provider_key", "model_key", "model_revision",
    "search_provider", "surface_key", "search_mode", "search_enabled", "search_query_count",
    "country_code", "region_key", "session_mode", "run_index", "answer_hash",
    "source_terminal_state", "processing_state", "rejection_codes", "consent_policy_version",
    "processing_purpose", "promotion_legal_basis", "retention_until", "legal_hold", "observed_at",
]
# 必填且非空(source_subkey 例外:必须显式提供但允许空串 "",表示"无子键")
_EVENT_REQUIRED_NONEMPTY = [
    "source_type", "source_table", "source_record_id",
    "platform_key", "provider_key", "model_key", "surface_key", "session_mode", "observed_at",
]


def compute_source_event_key(source_type: str, source_table: str, source_record_id: str, source_subkey: str) -> str:
    """来源稳定字段的 SHA-256 幂等门。源修订必须体现在 source_subkey。"""
    return hashlib.sha256(
        "|".join([source_type, source_table, str(source_record_id), str(source_subkey)]).encode("utf-8")
    ).hexdigest()


def register_event(cur, fields: dict) -> tuple[dict, bool]:
    """在给定 cursor(=源业务事务,R8)上幂等登记一条来源事件。

    返回 (event_row, created)。已存在则返回既有行 created=False(同一来源重试只命中同一 event)。
    """
    for k in _EVENT_REQUIRED_NONEMPTY:
        if fields.get(k) in (None, ""):
            raise ValueError(f"register_event 缺必填字段: {k}")
    if fields.get("source_subkey") is None:  # 必须显式(可为空串)
        raise ValueError("register_event 缺 source_subkey(空串也必须显式提供)")

    source_event_key = compute_source_event_key(
        fields["source_type"], fields["source_table"], fields["source_record_id"], fields["source_subkey"]
    )
    row_vals: dict[str, Any] = {
        "event_uuid": str(uuid.uuid4()),
        "schema_version": fields.get("schema_version", SCHEMA_VERSION),
        "source_event_key": source_event_key,
        "processing_state": fields.get("processing_state", "pending"),
        "rejection_codes": Json(fields.get("rejection_codes", [])),
        "processing_purpose": fields.get("processing_purpose", "geo_aggregate_learning"),
        "country_code": fields.get("country_code", "CN"),
        "run_index": fields.get("run_index", 1),
        "legal_hold": fields.get("legal_hold", False),
    }
    for col in _EVENT_INSERT_COLS:
        if col not in row_vals:
            row_vals[col] = fields.get(col)

    placeholders = ", ".join(["%s"] * len(_EVENT_INSERT_COLS))
    collist = ", ".join(_EVENT_INSERT_COLS)
    cur.execute(
        f"""
        INSERT INTO public.geo_observation_events ({collist})
        VALUES ({placeholders})
        ON CONFLICT (source_type, source_table, source_record_id, source_subkey) DO NOTHING
        RETURNING *
        """,
        [row_vals[c] for c in _EVENT_INSERT_COLS],
    )
    row = cur.fetchone()
    if row is not None:
        return dict(row), True
    # 幂等命中:取既有行
    cur.execute(
        """SELECT * FROM public.geo_observation_events
           WHERE source_type=%s AND source_table=%s AND source_record_id=%s AND source_subkey=%s""",
        (fields["source_type"], fields["source_table"], str(fields["source_record_id"]), str(fields["source_subkey"])),
    )
    return dict(cur.fetchone()), False


def register_event_standalone(fields: dict) -> tuple[dict, bool]:
    """reconciler 补登记路径:自开事务(源业务已终态,无同事务可接)。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        result = register_event(cur, fields)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_event(cur, event_id: int) -> Optional[dict]:
    cur.execute("SELECT * FROM public.geo_observation_events WHERE id=%s", (event_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def get_event_by_source(cur, source_type: str, source_table: str, source_record_id: str, source_subkey: str) -> Optional[dict]:
    cur.execute(
        """SELECT * FROM public.geo_observation_events
           WHERE source_type=%s AND source_table=%s AND source_record_id=%s AND source_subkey=%s""",
        (source_type, source_table, str(source_record_id), str(source_subkey)),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def claim_next_event(cur, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Optional[dict]:
    """原子领取一条待处理/租约过期的事件(SKIP LOCKED)。返回带 lease_token 的行或 None。

    kill-9 恢复:'processing' 且 lease_until 过期的事件可被重新领取;旧 worker 复活因 token 不符零写。
    """
    token = str(uuid.uuid4())
    cur.execute(
        """
        UPDATE public.geo_observation_events
           SET processing_state='processing',
               lease_token=%s,
               lease_until=NOW() + make_interval(secs => %s),
               attempts=attempts + 1,
               updated_at=NOW()
         WHERE id = (
             SELECT id FROM public.geo_observation_events
              WHERE processing_state IN ('pending','processing')
                AND (lease_until IS NULL OR lease_until < NOW())
                AND legal_hold = FALSE
              ORDER BY created_at ASC
              FOR UPDATE SKIP LOCKED
              LIMIT 1
         )
        RETURNING *
        """,
        (token, lease_seconds),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def heartbeat_event(cur, event_id: int, lease_token: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                    *, mark_paid_call: bool = False) -> bool:
    """续租(CAS on token)。返回是否续租成功;False = 租约已丢(必须停止处理)。

    mark_paid_call=True(付费 provider POST 前的守卫首调):在**同一 CAS 事务**内持久化
    `paid_call_started_at`(COALESCE 保留首次时刻)—— 付费调用一旦发起即留耐久锚,
    kill-9/租约丢失后被重领的 worker 据此路由人工,绝不自动再次付费(见 promotion 重领路由)。
    """
    cur.execute(
        """UPDATE public.geo_observation_events
              SET lease_until=NOW() + make_interval(secs => %s),
                  paid_call_started_at=CASE WHEN %s THEN COALESCE(paid_call_started_at, NOW()) ELSE paid_call_started_at END,
                  updated_at=NOW()
            WHERE id=%s AND lease_token=%s AND processing_state='processing'
            RETURNING id""",
        (lease_seconds, mark_paid_call, event_id, lease_token),
    )
    return cur.fetchone() is not None


def lock_lease_for_commit(cur, event_id: int, lease_token: str) -> bool:
    """Phase-3 提交事务起点:对 event 行 `FOR UPDATE` 锁定并校验本 worker 仍持 lease。

    返回 True=已锁定且仍持租约(整段提交在同事务持此行锁,其他 worker 的 claim `FOR UPDATE SKIP LOCKED`
    会跳过本行→提交期间不可能被重领);False=租约已丢(token 换/非 processing)→ 调用方零写返回。
    """
    cur.execute(
        """SELECT id FROM public.geo_observation_events
            WHERE id=%s AND lease_token=%s AND processing_state='processing'
            FOR UPDATE""",
        (event_id, lease_token),
    )
    return cur.fetchone() is not None


def finish_event(
    cur,
    event_id: int,
    lease_token: str,
    new_state: str,
    *,
    rejection_codes: Optional[list] = None,
    source_terminal_state: Optional[str] = None,
    consent_policy_version: Optional[str] = None,
    promotion_legal_basis: Optional[str] = None,
    retention_until: Optional[Any] = None,
) -> bool:
    """终态 CAS(只认当前 lease token)。rowcount=1 成功;0 = 租约丢失/token 不符(旧 worker 复活零写)。

    new_state ∈ pending_review/promoted/private_only/rejected/error。撤回走 withdraw_event。
    """
    if new_state not in ("pending_review", "promoted", "private_only", "rejected", "error"):
        raise ValueError(f"finish_event 非法终态: {new_state}")
    cur.execute(
        """
        UPDATE public.geo_observation_events
           SET processing_state=%s,
               lease_token=NULL,
               lease_until=NULL,
               rejection_codes=COALESCE(%s, rejection_codes),
               source_terminal_state=COALESCE(%s, source_terminal_state),
               consent_policy_version=COALESCE(%s, consent_policy_version),
               promotion_legal_basis=COALESCE(%s, promotion_legal_basis),
               retention_until=COALESCE(%s, retention_until),
               updated_at=NOW()
         WHERE id=%s AND lease_token=%s AND processing_state='processing'
        RETURNING id
        """,
        (
            new_state,
            Json(rejection_codes) if rejection_codes is not None else None,
            source_terminal_state,
            consent_policy_version,
            promotion_legal_basis,
            retention_until,
            event_id,
            lease_token,
        ),
    )
    return cur.fetchone() is not None


def insert_signal(cur, event_id: int, signal: dict) -> bool:
    """插入唯一晋升信号(UNIQUE event_id)。已存在(并发/重放)返回 False。

    signal 必须只含匿名字段;owner/brand/原文/自定义问题/带参数 URL 一律不得出现(schema 无该列)。
    """
    cols = [
        "event_id", "industry_key", "prompt_family_key", "prompt_intent", "is_branded_prompt",
        "platform_key", "provider_key", "model_key", "model_revision", "surface_key", "search_provider",
        "response_status", "target_outcome", "target_position", "sentiment", "competitor_count",
        "source_domains", "citation_count", "source_count", "search_query_count",
        "search_query_theme_keys", "quality_score_bps", "base_weight_bps", "effective_weight_bps",
        "confidence_bps", "observed_at",
    ]
    vals = {c: signal.get(c) for c in cols}
    vals["event_id"] = event_id
    vals["source_domains"] = Json(signal.get("source_domains", []))
    vals["search_query_theme_keys"] = Json(signal.get("search_query_theme_keys", []))
    placeholders = ", ".join(["%s"] * len(cols))
    cur.execute(
        f"""INSERT INTO public.geo_observation_signals ({", ".join(cols)})
            VALUES ({placeholders})
            ON CONFLICT (event_id) DO NOTHING
            RETURNING signal_id""",
        [vals[c] for c in cols],
    )
    return cur.fetchone() is not None


def insert_contributor_bucket(
    cur,
    event_id: int,
    contributor_user_bucket: str,
    contributor_brand_bucket: str,
    prompt_family_key: str,
    platform_key: str,
    contribution_date,
    bucket_key_version: int = 1,
) -> bool:
    """插入受限贡献者桶。返回是否赢得该 5 元组公共投票(R6 唯一门,不含 source_type)。

    ON CONFLICT DO NOTHING:同 user/brand/题族/平台/自然日的第二条事件不再赢票(保留为稳定度样本)。
    先命中 event_id 唯一(一事件一桶)或命中 5 元组唯一(一票)都返回 False。
    """
    cur.execute(
        """
        INSERT INTO public.geo_observation_contributor_buckets
            (event_id, contributor_user_bucket, contributor_brand_bucket, bucket_key_version,
             contribution_date, prompt_family_key, platform_key)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (event_id, contributor_user_bucket, contributor_brand_bucket, bucket_key_version,
         contribution_date, prompt_family_key, platform_key),
    )
    return cur.fetchone() is not None


def withdraw_event(cur, event_id: int, *, reason_codes: Optional[list] = None) -> bool:
    """撤回:event → withdrawn + withdrawn_at(幂等)。signals/audit 保持不可变;聚合下轮 join 只取 promoted 自动剔除。

    可从任意非 withdrawn 态撤回(退款/withheld/注销/源删除/审计判错)。返回是否发生状态变化。
    """
    cur.execute(
        """
        UPDATE public.geo_observation_events
           SET processing_state='withdrawn',
               withdrawn_at=COALESCE(withdrawn_at, NOW()),
               lease_token=NULL,
               lease_until=NULL,
               rejection_codes=COALESCE(%s, rejection_codes),
               updated_at=NOW()
         WHERE id=%s AND processing_state <> 'withdrawn'
        RETURNING id
        """,
        (Json(reason_codes) if reason_codes is not None else None, event_id),
    )
    return cur.fetchone() is not None


def set_legal_hold(cur, event_id: int, hold: bool) -> bool:
    cur.execute(
        "UPDATE public.geo_observation_events SET legal_hold=%s, updated_at=NOW() WHERE id=%s RETURNING id",
        (hold, event_id),
    )
    return cur.fetchone() is not None
