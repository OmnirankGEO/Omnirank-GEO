"""AI-1 micros 成本账本 DDL stub + Postgres 原子预留 repository(复审 P1-1/P1-3/P1-7)。

AI-1 **不跑 migration、不碰 db/migration_manifest.py**。本文件提供:
  1. ``ALL_DDL``:micros 成本账本(BIGINT)+ 预留表,**自愈式**(partial schema 也补 CHECK/UNIQUE/列):
     CREATE TABLE IF NOT EXISTS(裸) + ADD COLUMN IF NOT EXISTS + DO 块按 pg_constraint 反查后补约束。
     幂等键:ledger.request_id UNIQUE(去重扣费)+ reservations.request_id UNIQUE(在途去重,防并发同 ID 双发)。
     provider 调用数用 ledger.call_count(BIGINT)按真实 attempts 计,非"业务行数"。
  2. ``PostgresBudgetLedger``:try_reserve/finalize/release/reap 用 advisory 锁 + 条件插入 +
     ON CONFLICT + 在途 request_id 检查,保证跨进程(WORKERS=4)原子且同 ID 恰一 provider 调用/恰一账。

SQL 四维核验(交集成者上线前只读确认):列名 / data_type(BIGINT micros+call_count、TIMESTAMPTZ)/
字段归属(独立表,非 float 元 geo_research_cost_log)/ 约束(amount>=0 CHECK、request_id UNIQUE、索引)。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from services.ai_surface_monitoring.cost_policy import ALREADY_CHARGED_TOKEN, BudgetDataUnavailableError
from db.xact_lock_guard import require_xact_scope

logger = logging.getLogger("GEO-AISurface-PGLedger")

COST_LEDGER_TABLE = "geo_ai_surface_cost_ledger"
COST_RESERVATIONS_TABLE = "geo_ai_surface_cost_reservations"

# 预留生命窗口:准入只计入窗口内在途预留;超窗孤儿由 reap 回收,不永久饿死预算。
_RESERVATION_WINDOW = "created_at >= NOW() - INTERVAL '15 minutes'"
_DAY_WINDOW = (
    "recorded_at >= date_trunc('day', NOW()) "
    "AND recorded_at < date_trunc('day', NOW()) + INTERVAL '1 day'"
)

# 自愈式 DDL:裸建表 + 补列 + 反查后补约束(partial schema 升级也能补齐 CHECK/UNIQUE/PK)。
ALL_DDL = f"""
CREATE TABLE IF NOT EXISTS {COST_LEDGER_TABLE} (
    id             BIGSERIAL PRIMARY KEY,
    source_kind    VARCHAR(40)  NOT NULL,
    surface_key    VARCHAR(80)  NOT NULL,
    amount_micros  BIGINT       NOT NULL,
    call_count     BIGINT       NOT NULL DEFAULT 1,
    is_estimated   BOOLEAN      NOT NULL DEFAULT FALSE,
    request_id     VARCHAR(200) NOT NULL,
    round_id       VARCHAR(64),
    recorded_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
ALTER TABLE {COST_LEDGER_TABLE} ADD COLUMN IF NOT EXISTS call_count BIGINT NOT NULL DEFAULT 1;
ALTER TABLE {COST_LEDGER_TABLE} ADD COLUMN IF NOT EXISTS is_estimated BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE {COST_LEDGER_TABLE} ADD COLUMN IF NOT EXISTS round_id VARCHAR(64);
CREATE INDEX IF NOT EXISTS idx_ai_surface_cost_ledger_src_time ON {COST_LEDGER_TABLE} (source_kind, recorded_at);
CREATE INDEX IF NOT EXISTS idx_ai_surface_cost_ledger_round ON {COST_LEDGER_TABLE} (round_id);

CREATE TABLE IF NOT EXISTS {COST_RESERVATIONS_TABLE} (
    token          VARCHAR(64)  PRIMARY KEY,
    source_kind    VARCHAR(40)  NOT NULL,
    surface_key    VARCHAR(80)  NOT NULL,
    amount_micros  BIGINT       NOT NULL,
    call_count     BIGINT       NOT NULL DEFAULT 1,
    request_id     VARCHAR(200) NOT NULL,
    round_id       VARCHAR(64),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
ALTER TABLE {COST_RESERVATIONS_TABLE} ADD COLUMN IF NOT EXISTS call_count BIGINT NOT NULL DEFAULT 1;
ALTER TABLE {COST_RESERVATIONS_TABLE} ADD COLUMN IF NOT EXISTS surface_key VARCHAR(80) NOT NULL DEFAULT '';
ALTER TABLE {COST_RESERVATIONS_TABLE} ADD COLUMN IF NOT EXISTS round_id VARCHAR(64);
CREATE INDEX IF NOT EXISTS idx_ai_surface_cost_res_src ON {COST_RESERVATIONS_TABLE} (source_kind);
CREATE INDEX IF NOT EXISTS idx_ai_surface_cost_res_created ON {COST_RESERVATIONS_TABLE} (created_at);
CREATE INDEX IF NOT EXISTS idx_ai_surface_cost_res_round ON {COST_RESERVATIONS_TABLE} (round_id);

DO $$
BEGIN
    -- amount>=0 CHECK(ledger + reservations)
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='geo_ai_surface_cost_ledger_amount_nonneg'
                   AND conrelid='{COST_LEDGER_TABLE}'::regclass) THEN
        ALTER TABLE {COST_LEDGER_TABLE} ADD CONSTRAINT geo_ai_surface_cost_ledger_amount_nonneg
            CHECK (amount_micros >= 0 AND call_count >= 0);
    END IF;
    -- ledger.request_id UNIQUE(去重扣费)
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='geo_ai_surface_cost_ledger_request_uniq'
                   AND conrelid='{COST_LEDGER_TABLE}'::regclass) THEN
        ALTER TABLE {COST_LEDGER_TABLE} ADD CONSTRAINT geo_ai_surface_cost_ledger_request_uniq
            UNIQUE (request_id);
    END IF;
    -- reservations.amount>=0 CHECK
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='geo_ai_surface_cost_res_amount_nonneg'
                   AND conrelid='{COST_RESERVATIONS_TABLE}'::regclass) THEN
        ALTER TABLE {COST_RESERVATIONS_TABLE} ADD CONSTRAINT geo_ai_surface_cost_res_amount_nonneg
            CHECK (amount_micros >= 0 AND call_count >= 0);
    END IF;
    -- reservations.request_id UNIQUE(在途去重,并发同 ID 只一份预留 → 恰一 provider 调用)
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='geo_ai_surface_cost_res_request_uniq'
                   AND conrelid='{COST_RESERVATIONS_TABLE}'::regclass) THEN
        ALTER TABLE {COST_RESERVATIONS_TABLE} ADD CONSTRAINT geo_ai_surface_cost_res_request_uniq
            UNIQUE (request_id);
    END IF;
END $$;
"""

# 向后兼容别名
COST_LEDGER_DDL = ALL_DDL
COST_RESERVATIONS_DDL = ALL_DDL


class PostgresBudgetLedger:
    """cost_policy.BudgetLedger 的 Postgres 实现(跨进程原子)。表由集成者从 ALL_DDL 建。

    fail-closed:读/写不可用 → 抛 BudgetDataUnavailableError(上层转 budget_blocked),绝不吞成 0。
    """

    def __init__(self, get_connection: Any = None):
        self._get_connection = get_connection

    def _conn(self):
        if self._get_connection is not None:
            return self._get_connection()
        from db.connection import get_connection  # 惰性 import(允许 import,禁止编辑)

        return get_connection()

    # ---- 原子准入(micros + 调用双上限;同 request_id 恰一预留) ----
    def try_reserve(self, source_kind: str, projected_micros: int, projected_calls: int, *, surface_key: str,
                    limit_micros: Optional[int], limit_calls: Optional[int], request_id: str,
                    round_id: Optional[str] = None, limit_round_calls: Optional[int] = None) -> Optional[str]:
        import uuid as _uuid

        token = _uuid.uuid4().hex
        pc = max(int(projected_calls), 1)
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                # 复审#2-R6-3 P1-3:**锁作用域必须 == 预算作用域**。单轮上限是**全局按 round**(下方 SQL 按
                # round_id 汇总,跨 source_kind),故除 source_kind 锁(日预算,按 source_kind 汇总)外,round_id
                # 存在时**还须取 round_id 锁**,否则跨 source_kind 同 round 的两事务不串行 → 单轮上限被穿透
                # (research+monitoring 各占满 cap)。两锁**按锁键排序**获取(所有事务一致顺序)→ 无死锁。
                _lock_keys = [f"ai_surface_budget:{source_kind}"]
                if round_id is not None:
                    _lock_keys.append(f"ai_surface_round:{round_id}")
                for _lk in sorted(_lock_keys):
                    require_xact_scope(cur, where="migrations_stub.try_reserve")  # §1 硬闸:autocommit 下取事务锁=没锁
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_lk,))
                # 快路径幂等:已入账 → 不再预留(调用方据 ALREADY_CHARGED_TOKEN 不再发 provider 调用)。
                # 这是**优化**而非正确性来源;真正的去重在下方 INSERT 的 WHERE 中(单快照)。
                cur.execute(f"SELECT 1 FROM {COST_LEDGER_TABLE} WHERE request_id = %s", (request_id,))
                if cur.fetchone() is not None:
                    conn.commit()
                    return ALREADY_CHARGED_TOKEN
                # 条件插入预留(**单条语句 = 单快照**,复审#2 P1-1):当日已花 + 窗口内在途预留
                # (micros & call 双维)+ 本次 ≤ limit;且 request_id 既无在途预留(NOT EXISTS 预留表)
                # **也无已入账**(NOT EXISTS 账本表 —— 关键:防 finalize 在本次快路径 SELECT 与 INSERT
                # 之间提交造成"账本已入账却仍成功预留 → 第二次 provider 调用"的两快照竞态)。
                # ON CONFLICT(request_id) 兜住并发同 ID 竞态。
                cur.execute(
                    f"""
                    INSERT INTO {COST_RESERVATIONS_TABLE}
                        (token, source_kind, surface_key, amount_micros, call_count, request_id, round_id)
                    SELECT %(token)s, %(sk)s, %(surf)s, %(micros)s, %(calls)s, %(rid)s, %(round)s
                    WHERE
                      NOT EXISTS (SELECT 1 FROM {COST_RESERVATIONS_TABLE}
                                  WHERE request_id=%(rid)s AND {_RESERVATION_WINDOW})
                      AND NOT EXISTS (SELECT 1 FROM {COST_LEDGER_TABLE} WHERE request_id=%(rid)s)
                      AND (%(lim_micros)s IS NULL OR
                        (SELECT COALESCE(SUM(amount_micros),0) FROM {COST_LEDGER_TABLE}
                           WHERE source_kind=%(sk)s AND {_DAY_WINDOW})
                        + (SELECT COALESCE(SUM(amount_micros),0) FROM {COST_RESERVATIONS_TABLE}
                           WHERE source_kind=%(sk)s AND {_RESERVATION_WINDOW})
                        + %(micros)s <= %(lim_micros)s)
                      AND (%(lim_calls)s IS NULL OR
                        (SELECT COALESCE(SUM(call_count),0) FROM {COST_LEDGER_TABLE}
                           WHERE source_kind=%(sk)s AND {_DAY_WINDOW})
                        + (SELECT COALESCE(SUM(call_count),0) FROM {COST_RESERVATIONS_TABLE}
                           WHERE source_kind=%(sk)s AND {_RESERVATION_WINDOW})
                        + %(calls)s <= %(lim_calls)s)
                      -- 复审#2-R6 P1-1:单轮 provider 调用(attempts)硬限(round 已入账 + 在途预留 + 本次 ≤ cap;
                      -- 跨进程同 round_id 也不越顶,与单快照原子插入同事务判定)。
                      -- 复审#2-R6-R2:lim_round <= 0 视为不限(与 cap_round_calls / RpmLimiter 的 <=0 约定一致)。
                      AND (%(lim_round)s IS NULL OR %(lim_round)s <= 0 OR %(round)s IS NULL OR
                        (SELECT COALESCE(SUM(call_count),0) FROM {COST_LEDGER_TABLE}
                           WHERE round_id=%(round)s)
                        + (SELECT COALESCE(SUM(call_count),0) FROM {COST_RESERVATIONS_TABLE}
                           WHERE round_id=%(round)s AND {_RESERVATION_WINDOW})
                        + %(calls)s <= %(lim_round)s)
                    ON CONFLICT (request_id) DO NOTHING
                    RETURNING token
                    """,
                    {"token": token, "sk": source_kind, "surf": surface_key, "micros": int(projected_micros),
                     "calls": pc, "rid": request_id, "lim_micros": limit_micros, "lim_calls": limit_calls,
                     "round": round_id, "lim_round": limit_round_calls},
                )
                row = cur.fetchone()
                if row is None:
                    # 0 行 = 预留未成。同一持锁事务内复读账本消歧:已入账(竞态命中)→ ALREADY_CHARGED
                    # (调用方短路,不发 provider 调用);否则 None(预算超限 / 在途同 ID 去重)。
                    cur.execute(f"SELECT 1 FROM {COST_LEDGER_TABLE} WHERE request_id = %s", (request_id,))
                    charged = cur.fetchone() is not None
                    conn.commit()
                    return ALREADY_CHARGED_TOKEN if charged else None
            conn.commit()
            return token
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise BudgetDataUnavailableError(f"try_reserve 失败: {exc}") from exc
        finally:
            _maybe_close(conn)

    # ---- 耐久入账(幂等;call_count 按真实 attempts) ----
    def finalize(self, token: str, *, actual_micros: int, actual_calls: int, surface_key: str,
                 is_estimated: bool) -> None:
        if token == ALREADY_CHARGED_TOKEN:
            return
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT source_kind, request_id, round_id FROM {COST_RESERVATIONS_TABLE} WHERE token = %s",
                    (token,))
                row = cur.fetchone()
                if row is None:
                    conn.commit()
                    return  # 已 finalize/release(幂等)
                if isinstance(row, dict):
                    source_kind, req_id, round_id = row["source_kind"], row["request_id"], row.get("round_id")
                else:
                    source_kind, req_id, round_id = row[0], row[1], row[2]
                cur.execute(
                    f"""INSERT INTO {COST_LEDGER_TABLE}
                        (source_kind, surface_key, amount_micros, call_count, is_estimated, request_id, round_id)
                        SELECT %s, %s, %s, %s, %s, %s, %s
                        ON CONFLICT (request_id) DO NOTHING""",
                    (source_kind, surface_key, int(actual_micros), max(int(actual_calls), 1),
                     bool(is_estimated), req_id, round_id),
                )
                cur.execute(f"DELETE FROM {COST_RESERVATIONS_TABLE} WHERE token = %s", (token,))
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise BudgetDataUnavailableError(f"finalize 失败: {exc}") from exc
        finally:
            _maybe_close(conn)

    def release(self, token: str) -> None:
        if token == ALREADY_CHARGED_TOKEN:
            return
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {COST_RESERVATIONS_TABLE} WHERE token = %s", (token,))
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise BudgetDataUnavailableError(f"release 失败: {exc}") from exc
        finally:
            _maybe_close(conn)

    def reap_stale_reservations(self, ttl_seconds: int = 900) -> int:
        """回收超窗孤儿预留:原子 DELETE 超期预留并保守按预留额(micros+call_count)charge(ON CONFLICT 幂等)。"""
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    WITH stale AS (
                        DELETE FROM {COST_RESERVATIONS_TABLE}
                        WHERE created_at < NOW() - (%(ttl)s || ' seconds')::interval
                        RETURNING source_kind, surface_key, amount_micros, call_count, request_id, round_id
                    )
                    INSERT INTO {COST_LEDGER_TABLE}
                        (source_kind, surface_key, amount_micros, call_count, is_estimated, request_id, round_id)
                    SELECT source_kind, surface_key, amount_micros, call_count, TRUE, request_id, round_id FROM stale
                    ON CONFLICT (request_id) DO NOTHING
                    """,
                    {"ttl": int(ttl_seconds)},
                )
                reaped = cur.rowcount
            conn.commit()
            return int(reaped or 0)
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise BudgetDataUnavailableError(f"reap_stale_reservations 失败: {exc}") from exc
        finally:
            _maybe_close(conn)

    def spent_micros_today(self, source_kind: str) -> int:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COALESCE(SUM(amount_micros),0) AS t FROM {COST_LEDGER_TABLE} "
                    f"WHERE source_kind=%s AND {_DAY_WINDOW}", (source_kind,))
                row = cur.fetchone()
            total = (row["t"] if isinstance(row, dict) else row[0]) if row else 0
            return int(total or 0)
        finally:
            _maybe_close(conn)

    def calls_today(self, source_kind: str) -> int:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COALESCE(SUM(call_count),0) AS c FROM {COST_LEDGER_TABLE} "
                    f"WHERE source_kind=%s AND {_DAY_WINDOW}", (source_kind,))
                row = cur.fetchone()
            c = (row["c"] if isinstance(row, dict) else row[0]) if row else 0
            return int(c or 0)
        finally:
            _maybe_close(conn)

    def calls_in_round(self, round_id: str) -> int:
        """单轮已入账 provider attempts(P1-1;round 非日窗,直接按 round_id 聚合 ledger)。"""
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COALESCE(SUM(call_count),0) AS c FROM {COST_LEDGER_TABLE} WHERE round_id=%s",
                    (round_id,))
                row = cur.fetchone()
            c = (row["c"] if isinstance(row, dict) else row[0]) if row else 0
            return int(c or 0)
        finally:
            _maybe_close(conn)


def _maybe_close(conn: Any) -> None:
    """**显式**归还连接到池(复审#2-R6-3-R4 加固)。db.connection.get_connection docstring 明确"调用者必须
    conn.close() 归还";其 _PooledConnection.close() 会 putconn。虽有 __del__ 安全网靠 refcount 兜底,但显式
    close 才是**既有惯例的正路**,不依赖 CPython 即时 refcount 时机(PyPy/持引用/延迟 GC 下更稳)。
    close 失败不崩业务(降级忽略);测试注入的无 close() 假连接经 getattr 守卫安全跳过。"""
    close = getattr(conn, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.debug("[PGLedger] conn.close 归还失败(忽略)", exc_info=True)
