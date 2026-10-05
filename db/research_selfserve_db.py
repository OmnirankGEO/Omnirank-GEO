"""[R 批 · 自助调研] DB helpers —— 代理自助单行业调研 队列表 + 行业别名归并表。

两张新表(任务/映射表 · 非公共素材池 · 可含 user_id):
- `geo_research_selfserve_queue`:自助调研任务队列(P0-2 全字段持久化 · 后台崩溃可靠
  commit/release · freeze_id/freeze_table 回填 + status 状态机)。
- `geo_research_industry_aliases`:用户行业原文 → 标准 geo_research_industries 的归并别名沉淀(P1-6)。

纪律(照 db/research_answer_entity_db.py 风格):
- 纯 additive · DDL 全 CREATE TABLE IF NOT EXISTS / IF NOT EXISTS index(幂等可重跑)。
- 不 DROP/TRUNCATE/RENAME · 不碰 billing/connection/auth/jwt · 不碰四张公共池表。
- 连接:from db.connection import get_connection + 显式 try/finally 归还 + 写函数 commit
  (对齐 db/geo_plan_tasks_db.py 连接规范)。
- normalized_alias 的「标准化」只用应用层 `_normalize_alias_text`(去空白/全半角/lower),
  不调 tools 飞轮 SSOT normalize_industry_key(高爆炸半径)。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from psycopg2.extras import Json

from db.connection import get_connection


# ============================================================
# 工具
# ============================================================


def _normalize_alias_text(s: Optional[str]) -> str:
    """标准化用户行业原文作为 alias 查找/存储键(纯应用层规则,幂等)。

    规则:全角 ASCII / 全角空格 → 半角 · 折叠内部连续空白 + 去首尾 · lower。
    再次 normalize 结果不变(幂等),保证写侧与读侧同键。
    """
    if not s:
        return ""
    out = []
    for ch in str(s):
        code = ord(ch)
        if code == 0x3000:            # 全角空格 → 半角空格
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:  # 全角可见 ASCII → 半角
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    # str.split() 折叠所有连续空白并去首尾
    return " ".join("".join(out).split()).lower()


def _since_clause(since_days: Optional[int], col: str = "finished_at") -> str:
    """新鲜度窗口子句(int 强转 → 注入安全,仿 research_answer_entity_db._since_clause)。"""
    if since_days and since_days > 0:
        return f" AND {col} >= NOW() - INTERVAL '{int(since_days)} days'"
    return ""


# ============================================================
# 幂等建表(bootstrap · 内联同 migration 的 DDL)
# ============================================================


def ensure_selfserve_tables() -> None:
    """建 队列表 + 别名表(幂等)。DDL 与 scripts/migration_geo_research_selfserve_2026_07_05.sql 同款。

    注:triggered_by CHECK 扩展不在此(那是 geo_research_round 的约束,由 diagnosis_db.py bootstrap /
    migration 负责),本函数只保证这两张 NEW 表存在。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_research_selfserve_queue (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                brand_id BIGINT,
                industry_raw TEXT NOT NULL,
                industry_id BIGINT,
                industry_key TEXT,
                round_id VARCHAR(50),
                freeze_id BIGINT,
                freeze_table VARCHAR(20),
                billing_exempt BOOLEAN DEFAULT FALSE,
                price_points INTEGER NOT NULL,
                prompt_snapshot JSONB,
                status VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'queued', 'running', 'completed', 'failed', 'timeout', 'cancelled')),
                failed_reason TEXT,
                idempotency_key TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now(),
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ
            )
        """)
        # [R#1] 既存表(CI 早批建过旧 schema)补 billing_exempt 列 + 扩 status CHECK(pending) —— 与 migration 同步。
        cur.execute(
            "ALTER TABLE geo_research_selfserve_queue "
            "ADD COLUMN IF NOT EXISTS billing_exempt BOOLEAN DEFAULT FALSE"
        )
        cur.execute(
            "ALTER TABLE geo_research_selfserve_queue "
            "DROP CONSTRAINT IF EXISTS geo_research_selfserve_queue_status_check"
        )
        cur.execute(
            "ALTER TABLE geo_research_selfserve_queue "
            "ADD CONSTRAINT geo_research_selfserve_queue_status_check "
            "CHECK (status IN ('pending', 'queued', 'running', 'completed', 'failed', 'timeout', 'cancelled'))"
        )
        # [R#1] 活跃态唯一索引覆盖 pending/queued/running(旧库窄 WHERE → DROP 后重建同步定义)。
        cur.execute("DROP INDEX IF EXISTS uq_selfserve_active_idem")
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_selfserve_active_idem
                ON geo_research_selfserve_queue (idempotency_key)
                WHERE status IN ('pending', 'queued', 'running')
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_selfserve_user ON geo_research_selfserve_queue(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_selfserve_industry_key ON geo_research_selfserve_queue(industry_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_selfserve_status ON geo_research_selfserve_queue(status)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_research_industry_aliases (
                id BIGSERIAL PRIMARY KEY,
                normalized_alias TEXT NOT NULL UNIQUE,
                industry_id BIGINT NOT NULL REFERENCES geo_research_industries(id),
                confidence NUMERIC(4,3),
                resolved_by VARCHAR(10) NOT NULL
                    CHECK (resolved_by IN ('llm', 'admin')),
                reviewed_by BIGINT,
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_selfserve_alias_industry ON geo_research_industry_aliases(industry_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_selfserve_alias_active ON geo_research_industry_aliases(active) WHERE active")
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# 队列表 CRUD
# ============================================================


def create_selfserve_task(user_id: int, brand_id: Optional[int], industry_raw: str,
                          industry_id: Optional[int], industry_key: Optional[str],
                          price_points: int, prompt_snapshot: Optional[list],
                          idempotency_key: str, billing_exempt: bool = False) -> int:
    """建自助调研任务,返回 task_id。

    [R#1] 初始 status='pending'(不可派发态):端点在 freeze 冻结成功回填 freeze_id(或标 billing_exempt)
    后再 promote_selfserve_task_to_queued → 'queued',关掉「freeze 未回填前被 scheduler 抢派发致免费交付 +
    随后 freeze 成孤儿」的窗口。prompt_snapshot 为勾选题目列表(存 JSONB)。freeze_id/round_id 由后续步骤回填。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO geo_research_selfserve_queue (
                user_id, brand_id, industry_raw, industry_id, industry_key,
                price_points, prompt_snapshot, idempotency_key, billing_exempt, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
            RETURNING id
        """, (
            int(user_id),
            brand_id,
            industry_raw or "",
            industry_id,
            industry_key,
            int(price_points),
            Json(prompt_snapshot or []),
            idempotency_key or "",
            bool(billing_exempt),
        ))
        task_id = int(cur.fetchone()["id"])
        conn.commit()
        return task_id
    finally:
        try:
            conn.close()
        except Exception:
            pass


def promote_selfserve_task_to_queued(task_id: int) -> bool:
    """[R#1] 冻结回填/豁免完成后把 pending → queued(仅当当前是 pending,幂等)。返回是否发生转换。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_research_selfserve_queue SET status = 'queued' "
            "WHERE id = %s AND status = 'pending'",
            (int(task_id),),
        )
        promoted = cur.rowcount == 1
        conn.commit()
        return promoted
    finally:
        try:
            conn.close()
        except Exception:
            pass


def set_selfserve_task_billing_exempt(task_id: int, exempt: bool = True) -> bool:
    """[R#1] 标记任务计费豁免(admin/零成本合法免费)。worker 据此不把 freeze_id=None 误判为孤儿。

    [FIX-3] 返回是否有行被更新(cur.rowcount == 1)。0 行(task 不存在 / 已被 reaper 漂移)是
    静默 no-op——旧实现只 -> None 让调用方无从感知,失败后任务停留 {freeze_id=NULL, exempt=FALSE},
    仍被 promote 成 queued → worker 孤儿守卫无限弹回队头 = 永久 FIFO 饥饿。调用方必须据返回值
    决定 cancel + 禁止 promote(见 api/research_selfserve_api.py else 分支)。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_research_selfserve_queue SET billing_exempt = %s WHERE id = %s",
            (bool(exempt), int(task_id)),
        )
        updated = cur.rowcount == 1
        conn.commit()
        return updated
    finally:
        try:
            conn.close()
        except Exception:
            pass


def claim_next_queued_selfserve_task() -> Optional[dict]:
    """[R#5] 原子领取最旧一条 queued 任务(FOR UPDATE SKIP LOCKED):置 running 并 RETURNING 整行。

    并发消费者只有拿到 rowcount=1 的那个 worker 拿到行,其余拿空(None)——根除「非原子 read-check-write
    致两 worker 抢同一 task → 二次完整付费轮」。返回被领取的行(dict)或 None(无排队 / 被别人抢走)。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE geo_research_selfserve_queue
               SET status = 'running', started_at = NOW()
             WHERE id = (
                 SELECT id FROM geo_research_selfserve_queue
                  WHERE status = 'queued'
                  ORDER BY created_at ASC
                  LIMIT 1
                  FOR UPDATE SKIP LOCKED
             )
            RETURNING *
        """)
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def claim_selfserve_task_by_id(task_id: int) -> bool:
    """[R#5] 按 id 原子占用一条 queued 任务(queued → running)。返回是否本调用赢得占用。

    直调 run_selfserve_task 时用:条件 UPDATE ... WHERE status='queued' 只允许一个赢家,
    败者(rowcount=0)跳过,不再进入建轮/付费链。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_research_selfserve_queue SET status = 'running', started_at = NOW() "
            "WHERE id = %s AND status = 'queued'",
            (int(task_id),),
        )
        won = cur.rowcount == 1
        conn.commit()
        return won
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_selfserve_task_round(task_id: int, round_id: str) -> None:
    """派发到 round_runner 后回填 round_id。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_research_selfserve_queue SET round_id = %s WHERE id = %s",
            (round_id, int(task_id)),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_selfserve_task_industry_id(task_id: int, industry_id: int) -> None:
    """[P0-1] freeze 成功后 persist 阶段:new 行业建成拿到真实 id 回填(create 时 industry_id=None)。

    仅写队列表(任务表,非公共池),worker 用 industry_id 取规范行业名建 round + 验收查榜。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_research_selfserve_queue SET industry_id = %s WHERE id = %s",
            (int(industry_id), int(task_id)),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_selfserve_task_prompt_snapshot(task_id: int, prompt_snapshot: Optional[list]) -> None:
    """[P0-1] persist 阶段落新题拿真实 id 后,用真实 id 回填 prompt_snapshot。

    create 时 snapshot 里新题 id=None(公共池未写);freeze 成功后 _insert_new_prompts 拿到真实 id,
    在此把 snapshot 更新成含真实 id + text —— worker 用 snapshot 建 round,确保新题带真实 id。
    仅写队列表(任务表,非公共池)。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_research_selfserve_queue SET prompt_snapshot = %s WHERE id = %s",
            (Json(prompt_snapshot or []), int(task_id)),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_selfserve_task_freeze(task_id: int, freeze_id: int, freeze_table: Optional[str] = None) -> None:
    """回填 freeze_id (+ freeze_table),镜像 db/geo_plan_tasks_db.update_freeze_id 两列写法。

    freeze_table('legacy'|'v35')回填,worker commit/release 时回传 billing 免猜表歧义。
    列未建(部署序 migration 未先行)→ 降级只写 freeze_id 不阻断。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        if freeze_table is not None:
            try:
                cur.execute(
                    "UPDATE geo_research_selfserve_queue SET freeze_id = %s, freeze_table = %s WHERE id = %s",
                    (freeze_id, freeze_table, int(task_id)),
                )
            except Exception:
                conn.rollback()
                cur.execute(
                    "UPDATE geo_research_selfserve_queue SET freeze_id = %s WHERE id = %s",
                    (freeze_id, int(task_id)),
                )
        else:
            cur.execute(
                "UPDATE geo_research_selfserve_queue SET freeze_id = %s WHERE id = %s",
                (freeze_id, int(task_id)),
            )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def set_selfserve_task_status(task_id: int, status: str, failed_reason: Optional[str] = None,
                              mark_started: bool = False, mark_finished: bool = False,
                              notification_terminal: Optional[str] = None) -> None:
    """置任务状态。mark_started → started_at=NOW();mark_finished → finished_at=NOW()。

    failed_reason 仅在非空时写(不覆盖已有为 NULL)。
    """
    sets = ["status = %s"]
    params: list[Any] = [status]
    if failed_reason is not None:
        sets.append("failed_reason = %s")
        params.append(failed_reason)
    if mark_started:
        sets.append("started_at = NOW()")
    if mark_finished:
        sets.append("finished_at = NOW()")
    params.append(int(task_id))
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE geo_research_selfserve_queue SET {', '.join(sets)} WHERE id = %s "
            "RETURNING id,user_id,status,finished_at",
            tuple(params),
        )
        row = cur.fetchone()
        if row and notification_terminal:
            from services.notification_events import NotificationEventType, RecipientKind
            from services.notification_outbox import (
                enqueue_admin_notification_events,
                enqueue_notification_event,
            )
            event_type = {
                "completed": NotificationEventType.RESEARCH_COMPLETED,
                "failed": NotificationEventType.RESEARCH_FAILED,
                "cancelled": NotificationEventType.RESEARCH_CANCELLED,
                "refunded": NotificationEventType.RESEARCH_REFUNDED,
                "manual_required": NotificationEventType.RESEARCH_MANUAL_REQUIRED,
            }[notification_terminal]
            facts = {
                "business_no": f"RESEARCH-{int(row['id'])}",
                "status": {
                    "completed": "调研已完成",
                    "failed": "调研未完成",
                    "cancelled": "调研已取消",
                    "refunded": "调研未完成，费用已退回",
                    "manual_required": "需要平台人工核验",
                }[notification_terminal],
                "occurred_at": (row.get("finished_at") or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
                "summary": "请在调研中心查看结果和下一步。",
            }
            enqueue_notification_event(
                cur,
                event_type=event_type,
                business_id=str(row["id"]),
                terminal_state=notification_terminal,
                recipient_user_id=int(row["user_id"]),
                recipient_kind=RecipientKind.USER,
                facts=facts,
            )
            if notification_terminal == "manual_required":
                enqueue_admin_notification_events(
                    cur,
                    event_type=event_type,
                    business_id=str(row["id"]),
                    terminal_state=notification_terminal,
                    facts=facts,
                )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def cancel_stale_pending_selfserve_task(task_id: int) -> Optional[dict]:
    """[FIX-8] 原子条件取消一条仍 pending 的 stale 孤儿(pending → cancelled),RETURNING freeze 字段。

    reaper 专用:消除「先退款 + 无条件 cancel」的 TOCTOU 资金竞态。只有本调用赢得
    pending→cancelled 转换(rowcount=1)才返回行,reaper 据 RETURNING 的 freeze 字段退款;
    若 task 已被 API promote / 并发漂移出 pending(queued/running · 用户已被告知成功、worker 可能
    已 claim 开跑),条件 UPDATE 命中 0 行返 None → reaper 一律不 release 不 cancel,交 worker 正常
    结算,杜绝「退款 + 并发交付」双花与静默丢单。freeze 字段以 RETURNING 为准,不用扫描时的旧快照。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_research_selfserve_queue "
            "SET status = 'cancelled', failed_reason = 'pending_orphan_reaped', finished_at = NOW() "
            "WHERE id = %s AND status = 'pending' "
            "RETURNING id, freeze_id, freeze_table, user_id",
            (int(task_id),),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_selfserve_task(task_id: int) -> Optional[dict]:
    """按 id 取任务全字段。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM geo_research_selfserve_queue WHERE id = %s", (int(task_id),))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_selfserve_task_by_round(round_id: str) -> Optional[dict]:
    """按 round_id 反查自助任务(报表端点鉴权用)· 取最近一条。

    [WO_MEDIA_BOARD_UX_CLOSURE §3] 报表按 round_id 取数,而**归属只在任务行上**
    (`user_id`)。没有这一步反查,报表端点就只能拿 round_id 当凭证 =
    任何人猜到 round_id 就能读别人花钱跑出来的调研结果。
    round_id 形如 `round_20260805_102956_659694`,是可推测的时间戳串,更不能当凭证。
    """
    rid = str(round_id or "").strip()
    if not rid:
        return None
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM geo_research_selfserve_queue WHERE round_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (rid,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_user_selfserve_rounds(user_id: int, industry_key: str, limit: int = 20) -> list[dict]:
    """该用户该行业的历轮自助调研(只列已拿到 round_id 的),新→旧。

    [WO_MEDIA_BOARD_UX_CLOSURE §3.3]「花过的算力永远找得回凭证」的数据源。
    🔴 按 `user_id` 过滤,不是按 `industry_key` 全量 —— 行业是共享的,
    别人在同一行业花的钱不该出现在我的历轮列表里。
    """
    key = str(industry_key or "").strip()
    if not key:
        return []
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 20
    n = max(1, min(100, n))
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, round_id, status, price_points, industry_key, industry_raw,
                   prompt_snapshot, created_at, started_at, finished_at
              FROM geo_research_selfserve_queue
             WHERE user_id = %s AND industry_key = %s AND COALESCE(round_id, '') <> ''
             ORDER BY created_at DESC
             LIMIT %s
        """, (int(user_id), key, n))
        return [dict(r) for r in (cur.fetchall() or [])]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_active_selfserve_task(user_id: int, industry_key: str,
                              industry_raw: Optional[str] = None) -> Optional[dict]:
    """该用户该行业活跃态(pending/queued/running)任务(幂等/双击守卫用)。取最近一条。

    [R#1] 覆盖 pending:create 后未 promote 的任务也算活跃(近乎同时的双击请求 → 端点提前返回原 task,
    不二次冻结 / 不落新题)。
    [出口审核 F2] industry_raw 非空 → 额外 OR industry_raw 兜底:active-task 服务端恢复时,若 llm-merge
    任务的 write_alias(best-effort·失败仅 warning)曾失败,恢复端 allow_llm=False 归一漂移(级3 fallback
    返 normalize(raw) ≠ 落库 normalize(canonical))→ 按 key 查不到;补按原文 industry_raw 召回在飞任务
    (zero-LLM·仍按 user_id 不跨租户泄露)。幂等/双击守卫调用方【不传】raw,保持按 key 精确语义不变。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        if industry_raw:
            cur.execute("""
                SELECT * FROM geo_research_selfserve_queue
                WHERE user_id = %s AND (industry_key = %s OR industry_raw = %s)
                      AND status IN ('pending', 'queued', 'running')
                ORDER BY created_at DESC
                LIMIT 1
            """, (int(user_id), industry_key, industry_raw))
        else:
            cur.execute("""
                SELECT * FROM geo_research_selfserve_queue
                WHERE user_id = %s AND industry_key = %s AND status IN ('pending', 'queued', 'running')
                ORDER BY created_at DESC
                LIMIT 1
            """, (int(user_id), industry_key))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_queued_selfserve_tasks(limit: int = 50) -> list[dict]:
    """待派发队列(status='queued'),FIFO(created_at 升序)。worker 拉取用。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM geo_research_selfserve_queue
            WHERE status = 'queued'
            ORDER BY created_at ASC
            LIMIT %s
        """, (int(limit),))
        return [dict(r) for r in cur.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_stale_pending_selfserve_tasks(older_than_minutes: int = 15) -> list[dict]:
    """[#4] 列出滞留 'pending' 且已回填 freeze_id 的孤儿任务(reaper 用)。

    P0-1 把 freeze→promote 窗口扩到多写(create 后 status='pending' 且 freeze_id 已回填,promote 前)。
    进程被杀(蓝绿 SIGTERM/OOM)在该窗口 → point_freezes 已 commit(钱冻)但 task 永卡 'pending':
    worker 只领 queued、freeze_sweeper 排除 pending、restart_recovery 只管 round → 钱冻死无自愈。

    pending 只由**本请求同步线程**在 persist 成功后 promote(线程死了永不 promote),故 status 仍 'pending'
    且 created_at 超过阈值(默认 15min · 远超正常请求时长)= 真孤儿。捞出交 reaper release+cancel。
    [出口审核 F1/FIX3-L1] 覆盖两类孤儿:① freeze_id IS NOT NULL(付费·已回填冻结,reaper cancel+release);
    ② billing_exempt IS TRUE(admin/零成本豁免·freeze_id=NULL,reaper 只 cancel 不 release·无钱可退)。
    旧实现只捞 ①,exempt 孤儿(freeze_id=NULL)永卡 pending→uq_selfserve_active_idem 锁死该 admin 该行业无自愈。
    reaper 的 cancel_stale_pending_selfserve_task 是原子 pending→cancelled(TOCTOU 安全);其 `if freeze_id`
    守卫对 exempt(freeze_id=NULL)自动跳过 release。older_than_minutes 强转 int(注入安全)。
    """
    mins = int(older_than_minutes)
    if mins < 0:
        mins = 0
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM geo_research_selfserve_queue "
            "WHERE status = 'pending' AND (freeze_id IS NOT NULL OR billing_exempt IS TRUE) "
            f"AND created_at < NOW() - INTERVAL '{mins} minutes' "
            "ORDER BY created_at ASC LIMIT 200"
        )
        return [dict(r) for r in (cur.fetchall() or [])]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def deactivate_empty_industry_best_effort(industry_id: int, current_task_id: Optional[int] = None) -> None:
    """[出口审核 F2] 停用【本次新建且尚无 active 题】的空行业(soft · active=FALSE · best-effort)。

    从 api/research_selfserve_api._deactivate_empty_industry_best_effort 下沉到 db 层,供 API 失败路径
    + worker reaper 共用(worker import api 会循环 import,故下沉)。双守卫(soft delete · 非 DROP):
      ① NOT EXISTS active 题 —— 已被填充的行业不停(避免误撤)。
      ② NOT EXISTS 其它活跃 task 引用(排除本 task 自身 id<>current_task_id)—— 别人正在/刚为它付费不停。
    current_task_id 缺省 → -1(BIGSERIAL 恒正,不排除任何 task)。
    """
    if not industry_id:
        return
    tid = int(current_task_id) if current_task_id is not None else -1
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_research_industries SET active = FALSE "
            "WHERE id = %s AND active = TRUE "
            "AND NOT EXISTS (SELECT 1 FROM geo_research_prompts "
            "WHERE industry_id = %s AND active = TRUE) "
            "AND NOT EXISTS (SELECT 1 FROM geo_research_selfserve_queue "
            "WHERE industry_id = %s AND status IN ('pending', 'queued', 'running') "
            "AND id <> %s)",
            (int(industry_id), int(industry_id), int(industry_id), tid),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_user_last_selfserve(user_id: int, industry_key: str, since_days: int) -> Optional[dict]:
    """该用户该行业最近一次 completed(新鲜度闸按用户用)。since_days 内无则返 None。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT * FROM geo_research_selfserve_queue
            WHERE user_id = %s AND industry_key = %s AND status = 'completed'
                  {_since_clause(since_days, 'finished_at')}
            ORDER BY finished_at DESC
            LIMIT 1
        """, (int(user_id), industry_key))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_industry_last_selfserve(industry_key: str, since_days: int) -> Optional[dict]:
    """任意用户该行业最近一次 completed(「别人刚跑过」知情提示用)。since_days 内无则返 None。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT * FROM geo_research_selfserve_queue
            WHERE industry_key = %s AND status = 'completed'
                  {_since_clause(since_days, 'finished_at')}
            ORDER BY finished_at DESC
            LIMIT 1
        """, (industry_key,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# 自助新建题激活(付费锁定后)
# ============================================================


def activate_selfserve_prompts(prompt_ids: list[int]) -> None:
    """[R#7] 付费锁定后(端点 promote pending→queued 成功之后)才把自助新建题激活。

    自助新建题在 `_insert_new_prompts` 落库时一律 active=FALSE(付费成功前的僵尸态):
    cron/manual 跑批与本行业榜都只捞 `WHERE active = TRUE`(scheduler_setup.py:278),故
    freeze 失败(402)/回填失败/promote 失败等任何 cancel 路径下,新题保持 active=FALSE =
    无害僵尸,绝不进跑批、绝不污染榜。只有 freeze 冻结 + promote 都成功(付费已锁定)后才在此激活。

    幂等 UPDATE(SET active=TRUE);空/None 列表直接返回,不建连接。id 强转 int 防注入。
    """
    ids = [int(p) for p in (prompt_ids or []) if p is not None]
    if not ids:
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_research_prompts SET active = TRUE, updated_at = now() "
            "WHERE id = ANY(%s)",
            (ids,),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# 行业别名归并表 CRUD
# ============================================================


def resolve_alias(normalized_alias: str) -> Optional[int]:
    """按标准化别名查 industry_id(仅 active)。命中返 industry_id,否则 None。"""
    key = _normalize_alias_text(normalized_alias)
    if not key:
        return None
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT industry_id FROM geo_research_industry_aliases
            WHERE normalized_alias = %s AND active
            LIMIT 1
        """, (key,))
        row = cur.fetchone()
        return int(row["industry_id"]) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def resolve_alias_row(normalized_alias: str) -> Optional[dict]:
    """[WO_267 · Review 09-23 顺序裁定] 与 `resolve_alias` 同一查询,多带回 `resolved_by`。

    行业路由前段要区分「admin 人工改判」(排在行业大类字典**之前**)与「LLM 归并沉淀」
    (排在字典**之后**)。`resolve_alias` 只回 industry_id 分不出来 —— 它本身不动(三个既有调用方照旧)。
    命中返 `{industry_id, resolved_by}`(仅 active),否则 None。
    """
    key = _normalize_alias_text(normalized_alias)
    if not key:
        return None
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT industry_id, resolved_by FROM geo_research_industry_aliases
            WHERE normalized_alias = %s AND active
            LIMIT 1
        """, (key,))
        row = cur.fetchone()
        return {"industry_id": int(row["industry_id"]), "resolved_by": str(row["resolved_by"])} if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def write_alias(normalized_alias: str, industry_id: int, confidence: Optional[float],
                resolved_by: str, reviewed_by: Optional[int] = None) -> Optional[dict]:
    """**仅首次写入**(原子)。返回库里**最终生效**的那一条,而不是"我想写的那一条"。

    ## 🔴 2026-08-09 Review 裁定的返工点:这里原来是无条件 `ON CONFLICT DO UPDATE`

    旧实现会把 `industry_id` / `resolved_by` / `reviewed_by` / `active` 一律改成
    本次入参。配合调用方的 `if resolve_alias(...) is None: write_alias(...)`,
    形成典型的 **check-then-act 非原子**:两句之间落进来的写会被无声覆盖 ——
    包括 **admin 人工改判**(`update_alias_industry` 会把 `resolved_by` 置 `admin`)。
    调用方注释宣称的「不覆盖 admin 人工改判」,旧实现**保证不了**。

    改成 `ON CONFLICT DO NOTHING` 之后,守卫从"调用方两句之间的时间差"
    收进**数据库的一次原子操作**,时间差没有了。

    ## 为什么是改本函数,不是加一个姊妹函数

    全仓三个调用点(`industry_canonical` / `industry_resolver` /
    `research_selfserve_api`)**全部**传 `resolved_by="llm"` —— 覆盖语义
    **没有任何合法用户**;`research_selfserve_api` 那处的注释本来就写着
    「alias_cache 命中说明别名已存在,无需重写」,意图一直就是"仅首次写入"。
    留一个不该被调用的覆盖版在旁边,只是给下一个人留个坑。
    admin 改判走的是**另一个**函数 `update_alias_industry`,不受此处影响。

    ## 调用方必须用返回值,不能用自己的入参

    冲突时本函数**不报错**(沉淀别名是 best-effort,不该阻断付费轮),
    所以"写没写进去"只能从返回值看。返回的是库里现存那条的
    `industry_id / resolved_by / reviewed_by / confidence / active`,
    **以它为准**,否则调用方会拿着一个从未生效的映射继续往下走。

    `active` 一并返回:`DO NOTHING` 不会把已停用的别名悄悄重新启用
    (旧实现的 `active = TRUE` 会),所以调用方要自己判断能不能采信。

    normalized_alias 内部再走 _normalize_alias_text 保证与 resolve_alias 同键。
    """
    key = _normalize_alias_text(normalized_alias)
    if not key:
        return None
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 🔴 `DO NOTHING` 时 `RETURNING` 不出行 —— 这是 PG 的既定行为,不是 bug。
        #    所以下面必须**无条件再读一次**:写进去了读到自己的,没写进去读到别人的,
        #    两种情况调用方拿到的都是"库里真正生效的那一条"。
        cur.execute("""
            INSERT INTO geo_research_industry_aliases (
                normalized_alias, industry_id, confidence, resolved_by, reviewed_by, active, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, TRUE, now())
            ON CONFLICT (normalized_alias) DO NOTHING
        """, (
            key,
            int(industry_id),
            confidence,
            resolved_by,
            reviewed_by,
        ))
        conn.commit()
        cur.execute("""
            SELECT industry_id, resolved_by, reviewed_by, confidence, active
              FROM geo_research_industry_aliases
             WHERE normalized_alias = %s
             LIMIT 1
        """, (key,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_industry_aliases(limit: int = 200) -> list[dict]:
    """别名列表(admin 审核用),join 行业名。最近创建优先。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT a.id, a.normalized_alias, a.industry_id, a.confidence,
                   a.resolved_by, a.reviewed_by, a.active, a.created_at, a.updated_at,
                   i.name AS industry_name
            FROM geo_research_industry_aliases a
            LEFT JOIN geo_research_industries i ON i.id = a.industry_id
            ORDER BY a.created_at DESC
            LIMIT %s
        """, (int(limit),))
        return [dict(r) for r in cur.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_alias_industry(alias_id: int, industry_id: int, reviewed_by: int) -> None:
    """admin 改判别名归属行业(resolved_by 置 'admin' + 记 reviewed_by)。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE geo_research_industry_aliases
            SET industry_id = %s, reviewed_by = %s, resolved_by = 'admin', updated_at = now()
            WHERE id = %s
        """, (int(industry_id), int(reviewed_by), int(alias_id)))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
