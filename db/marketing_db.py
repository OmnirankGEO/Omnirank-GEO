"""
db/marketing_db.py — 营销中心(营销军师 + 物料工厂)数据访问层

照抄 db/ai_ops_db.py 的房规,不复用其业务表(总设计 §9.2 表隔离):
  - 连接:from db.connection import get_connection(池化 RealDictCursor · 非 autocommit)
  - 写:conn=_get_conn(); try: cur.execute(...); conn.commit() finally: conn.close()
  - 读:fail-soft(异常 → rollback → 返回中性值,绝不 500 拖垮总览)
  - 幂等:Postgres ON CONFLICT(case_key / grant_key / (report unit))
  - 应用层 CHECK 镜像:枚举用模块常量元组,插入前 raise ValueError 快速失败

红线:本模块零改 middleware/billing.py / db/wallet_db.py / db/connection.py / auth/*。
      真正的算力入账/冻结走 middleware.billing 公共接口(在执行器里调),此处只管台账与元数据。
"""
import json
import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger("GEO-Marketing-DB")


def _get_conn():
    # 延迟 import:让测试的 DATABASE_URL 改写在模块加载后仍生效(ai_ops_db 同款)
    from db.connection import get_connection
    return get_connection()


# ============================================================================
# 应用层 CHECK 镜像(与 migration_marketing_center_2026_07_04.sql 一致)
# ============================================================================
CASE_STATUSES = ('draft', 'pending', 'approved', 'rejected', 'changes_requested',
                 'executed', 'expired', 'cancelled')
CASE_RISK_LEVELS = ('low', 'med', 'high')
CASE_OWNER_SCOPES = ('platform', 'user')
APPROVAL_DECISIONS = ('approve', 'reject', 'request_changes')
CAMPAIGN_TYPES = ('first_charge_double', 'milestone', 'adhoc')
CAMPAIGN_STATUSES = ('draft', 'active', 'paused', 'ended')
GRANT_STATUSES = ('applied', 'reversed', 'dry_run')
TOUCH_CHANNELS = ('station', 'wecom', 'sms', 'wecom_service')
TOUCH_SEGMENTS = ('end', 'provider')
TOUCH_STATUSES = ('sent', 'failed', 'suppressed', 'dry_run')
MEASURE_METRICS = ('recharge', 'revisit', 'feature_reuse')
MATERIAL_KINDS = ('copy', 'poster', 'bundle')
JOB_STATUSES = ('pending', 'generating', 'succeeded', 'failed', 'blocked')

# 建议案件 10 字段(总设计 §9.4 · pydantic 强校验的落库列)
CASE_TEN_FIELDS = (
    'trigger_reason', 'evidence', 'audience', 'expected_impact', 'budget_cost',
    'risk_level', 'touch_copy', 'execution_plan', 'rollback_plan', 'observation_window',
)

# 策略/Kill Switch 白名单(PATCH /policies/{key} 校验;未知 key → 400)
POLICY_KEYS = (
    'marketing_agent.enabled',
    'marketing_agent.execute.enabled',
    'marketing_agent.grant.enabled',
    'marketing_agent.notification.enabled',
    'marketing_agent.kill_switch',
    'marketing_agent.control_group.enabled',
    'marketing_agent.advisor_llm.enabled',
)

# 数值配置白名单(admin 设置页可改;value_jsonb.value 存数)
CONFIG_KEYS = (
    'marketing.touch.daily_global_cap',
    'marketing.touch.night_dnd_start',
    'marketing.touch.night_dnd_end',
    'marketing.touch.freq_days',
    'marketing.grant.cap_per_user',
    'marketing.grant.cap_per_batch',
    'marketing.grant.cap_per_day',
    'marketing.grant.hard_ceiling',
    'marketing.case.budget_cap',
    'marketing.material.daily_limit',
)

_CONFIG_DEFAULTS = {
    'marketing.touch.daily_global_cap': 500,
    'marketing.touch.night_dnd_start': 21,
    'marketing.touch.night_dnd_end': 9,
    'marketing.touch.freq_days': 7,
    # [返工 R6-2] 与内置活动自洽(首充封顶 65000/里程碑档二 39000 必须发得出)
    'marketing.grant.cap_per_user': 120000,
    'marketing.grant.cap_per_batch': 200000,
    'marketing.grant.cap_per_day': 300000,
    'marketing.grant.hard_ceiling': 100000,
    'marketing.case.budget_cap': 300000,
    'marketing.material.daily_limit': 20,
}


def _dumps(obj) -> str:
    return json.dumps(obj if obj is not None else {}, ensure_ascii=False, default=str)


# ============================================================================
# 策略 / Kill Switch / 数值配置(marketing_policies · 复用 {"enabled":bool} 形状)
# ============================================================================
def get_policy(key: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value_jsonb FROM marketing_policies WHERE key = %s", (key,))
        row = cur.fetchone()
        if not row:
            return None
        val = row['value_jsonb']
        return val if isinstance(val, dict) else None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] get_policy(%s) 失败: %s", key, e)
        return None
    finally:
        conn.close()


def set_policy(key: str, value: dict, updated_by: Optional[int] = None) -> None:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_policies (key, value_jsonb, updated_by, updated_at)
            VALUES (%s, %s::jsonb, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET
              value_jsonb = EXCLUDED.value_jsonb,
              updated_by = EXCLUDED.updated_by,
              updated_at = NOW()
            """,
            (key, _dumps(value), updated_by),
        )
        conn.commit()
    finally:
        conn.close()


def get_policies() -> list:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT key, value_jsonb, updated_by, updated_at FROM marketing_policies ORDER BY key")
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] get_policies 失败: %s", e)
        return []
    finally:
        conn.close()


def is_flag_enabled(key: str, default: bool = False) -> bool:
    val = get_policy(key)
    if not isinstance(val, dict):
        return default
    return bool(val.get('enabled', default))


def is_kill_switch_enabled() -> bool:
    return is_flag_enabled('marketing_agent.kill_switch', default=False)


def get_config_int(key: str, default: Optional[int] = None) -> int:
    """读数值配置(marketing.*).缺失/异常 → 回退到内置默认或传入默认。"""
    if default is None:
        default = _CONFIG_DEFAULTS.get(key, 0)
    val = get_policy(key)
    if isinstance(val, dict) and 'value' in val:
        try:
            return int(val['value'])
        except (TypeError, ValueError):
            return default
    return default


def can_marketing_act(action_flag: str) -> tuple[bool, str]:
    """执行器动作前的多闸级联:kill switch → 总闸 → 该类执行器 flag。
    返回 (allowed, reason)。allowed=False 时是 dry_run/静默 的判据。"""
    if is_kill_switch_enabled():
        return False, 'kill_switch'
    if not is_flag_enabled('marketing_agent.enabled', default=False):
        return False, 'master_off'
    if not is_flag_enabled('marketing_agent.execute.enabled', default=False):
        return False, 'execute_off'
    if action_flag and not is_flag_enabled(action_flag, default=False):
        return False, f'{action_flag}_off'
    return True, 'ok'


# ============================================================================
# 建议案件 marketing_cases(今日军情队列 · 10 字段强 schema · 日期域幂等)
# ============================================================================
def create_case(
    *,
    rule_key: str,
    fingerprint: str = '',
    case_key: Optional[str] = None,
    owner_scope: str = 'platform',
    awareness_stage: str = 'unknown',
    trigger_reason: str = '',
    evidence: Optional[dict] = None,
    audience: Optional[dict] = None,
    expected_impact: str = '',
    budget_cost: Optional[dict] = None,
    risk_level: str = 'low',
    touch_copy: Optional[dict] = None,
    execution_plan: Optional[dict] = None,
    rollback_plan: str = '',
    observation_window_days: int = 7,
    skill_packs: Optional[list] = None,
    status: str = 'pending',
    llm_model: str = '',
    created_by: Optional[int] = None,
    expires_at: Optional[datetime] = None,
) -> tuple[dict, bool]:
    """插入建议案件(幂等:ON CONFLICT (case_key) DO NOTHING)。
    返回 (case, created)。case_key 为空时永不冲突(Postgres 允许多 NULL)。"""
    if risk_level not in CASE_RISK_LEVELS:
        raise ValueError(f"risk_level 非法: {risk_level}")
    if owner_scope not in CASE_OWNER_SCOPES:
        raise ValueError(f"owner_scope 非法: {owner_scope}")
    if status not in CASE_STATUSES:
        raise ValueError(f"status 非法: {status}")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_cases (
              case_key, rule_key, fingerprint, awareness_stage, owner_scope,
              trigger_reason, evidence_jsonb, audience_jsonb, expected_impact,
              budget_cost_jsonb, risk_level, touch_copy_jsonb, execution_plan_jsonb,
              rollback_plan, observation_window_days, skill_packs_jsonb,
              status, llm_model, created_by, expires_at
            ) VALUES (
              %s,%s,%s,%s,%s,
              %s,%s::jsonb,%s::jsonb,%s,
              %s::jsonb,%s,%s::jsonb,%s::jsonb,
              %s,%s,%s::jsonb,
              %s,%s,%s,%s
            )
            ON CONFLICT (case_key) DO NOTHING
            RETURNING *
            """,
            (
                case_key, rule_key, fingerprint, awareness_stage, owner_scope,
                trigger_reason, _dumps(evidence), _dumps(audience), expected_impact,
                _dumps(budget_cost), risk_level, _dumps(touch_copy), _dumps(execution_plan),
                rollback_plan, int(observation_window_days), _dumps(skill_packs or []),
                status, llm_model, created_by, expires_at,
            ),
        )
        row = cur.fetchone()
        if row:
            conn.commit()
            return dict(row), True
        # 冲突命中:回取已存在的
        if case_key:
            cur.execute("SELECT * FROM marketing_cases WHERE case_key = %s", (case_key,))
            existing = cur.fetchone()
            conn.commit()
            return (dict(existing) if existing else {}), False
        conn.commit()
        return {}, False
    finally:
        conn.close()


def record_material_report(asset_id: int, reporter_id: int, reason: str = '') -> dict:
    """[Codex 复审加固] 举报通道:events-only(不直接写资产 risk_flags——处置权在 admin,
    防"猜 asset_id 批量污染他人素材风控字段"滥用)+ 同人同资产幂等 + 日限流。
    返回 {status: reported|already_reported|daily_limited|not_found}。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM marketing_material_assets WHERE id = %s", (asset_id,))
        if not cur.fetchone():
            return {"status": "not_found"}
        cur.execute(
            """SELECT
                 COUNT(*) FILTER (WHERE payload_jsonb->>'asset_id' = %s)::int AS dup_cnt,
                 COUNT(*) FILTER (WHERE created_at::date = CURRENT_DATE)::int AS today_cnt
               FROM marketing_events
               WHERE event_type = 'material_reported' AND payload_jsonb->>'reporter' = %s""",
            (str(asset_id), str(reporter_id)))
        row = cur.fetchone()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] record_material_report 预检失败: %s", e)
        return {"status": "error"}
    finally:
        conn.close()
    if row and int(row["dup_cnt"]) > 0:
        return {"status": "already_reported"}
    daily_cap = get_config_int("marketing.material.report_daily_limit", 10)
    if row and int(row["today_cnt"]) >= daily_cap:
        return {"status": "daily_limited", "limit": daily_cap}
    add_event(event_type="material_reported", severity="warn", actor_id=reporter_id,
              message=f"物料被举报 asset={asset_id}: {(reason or '')[:200]}",
              payload={"asset_id": str(asset_id), "reporter": str(reporter_id),
                       "reason": (reason or '')[:500]})
    return {"status": "reported"}


def is_case_expired(case_id: int) -> bool:
    """[返工 R6-4] 过期判定收在 DB 时钟侧(expires_at 由 DB/应用混写,统一用 DB NOW() 比,
    与 expire_due_cases 的 WHERE expires_at < NOW() 同源)。查询失败保守返 False(不误杀)。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT (expires_at IS NOT NULL AND expires_at < NOW()) AS expired "
                    "FROM marketing_cases WHERE id = %s", (case_id,))
        row = cur.fetchone()
        return bool(row and row["expired"])
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] is_case_expired 失败: %s", e)
        return False
    finally:
        conn.close()


def get_case(case_id: int) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM marketing_cases WHERE id = %s", (case_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] get_case(%s) 失败: %s", case_id, e)
        return None
    finally:
        conn.close()


def list_cases(status: Optional[str] = None, limit: int = 100, offset: int = 0) -> list:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if status:
            cur.execute(
                "SELECT * FROM marketing_cases WHERE status = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (status, limit, offset),
            )
        else:
            cur.execute(
                "SELECT * FROM marketing_cases ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] list_cases 失败: %s", e)
        return []
    finally:
        conn.close()


def count_cases_by_status() -> dict:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status, COUNT(*)::int AS c FROM marketing_cases GROUP BY status")
        return {r['status']: r['c'] for r in cur.fetchall()}
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] count_cases_by_status 失败: %s", e)
        return {}
    finally:
        conn.close()


def update_case_status(case_id: int, status: str, *, approved_by: Optional[int] = None,
                       set_executed: bool = False) -> None:
    if status not in CASE_STATUSES:
        raise ValueError(f"status 非法: {status}")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        sets = ["status = %s", "updated_at = NOW()"]
        params: list = [status]
        if status == 'approved':
            sets.append("approved_at = NOW()")
            sets.append("approved_by = %s")
            params.append(approved_by)
        if set_executed:
            sets.append("executed_at = NOW()")
        params.append(case_id)
        cur.execute(f"UPDATE marketing_cases SET {', '.join(sets)} WHERE id = %s", tuple(params))
        conn.commit()
    finally:
        conn.close()


def has_recent_case(rule_key: str, fingerprint: str, within_days: int = 7) -> bool:
    """频控:同一 规则×目标 最近 within_days 天内是否已有案件(防每日重复刷屏军情队列)。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 1 FROM marketing_cases
            WHERE rule_key = %s AND fingerprint = %s
              AND created_at > NOW() - (%s || ' days')::interval
            LIMIT 1
            """,
            (rule_key, fingerprint, str(int(within_days))),
        )
        return cur.fetchone() is not None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] has_recent_case 失败: %s", e)
        return False
    finally:
        conn.close()


def expire_due_cases() -> int:
    """把过期未批的 pending 案件置 expired(防旧方案被误执行)。返回作废条数。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE marketing_cases SET status = 'expired', updated_at = NOW()
            WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < NOW()
            """
        )
        n = cur.rowcount
        conn.commit()
        return n or 0
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] expire_due_cases 失败: %s", e)
        return 0
    finally:
        conn.close()


def backfill_case_no(case_id: int) -> str:
    """回填展示号 MKT-YYYYMMDD-{id:05d}。"""
    case_no = f"MKT-{date.today().strftime('%Y%m%d')}-{case_id:05d}"
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE marketing_cases SET case_no = %s WHERE id = %s AND case_no = ''",
                    (case_no, case_id))
        conn.commit()
    finally:
        conn.close()
    return case_no


# ============================================================================
# 审批 marketing_approvals(五绿勾真校验快照)
# ============================================================================
def record_approval(*, case_id: int, decision: str, five_checks: Optional[dict] = None,
                    checks_passed: bool = False, approver_id: Optional[int] = None,
                    note: str = '') -> dict:
    if decision not in APPROVAL_DECISIONS:
        raise ValueError(f"decision 非法: {decision}")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            -- 🔴 [#114] 生产 marketing_approvals 8 列**没有 `checks_passed`**
            --    (id / case_id / decision / five_checks_jsonb / approver_id / note /
            --     executed_at / created_at)。带着它的 INSERT 每次 UndefinedColumn ⇒
            --    这条审批落库**整条失败**,不是「少存一个布尔」。
            --    删掉不丢信息:五项检查的明细都在 five_checks_jsonb 里,
            --    「是否全过」是它的**派生值**,读侧要用现算即可。
            INSERT INTO marketing_approvals
              (case_id, decision, five_checks_jsonb, approver_id, note)
            VALUES (%s,%s,%s::jsonb,%s,%s)
            RETURNING *
            """,
            (case_id, decision, _dumps(five_checks), approver_id, note),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def mark_approval_executed(approval_id: int) -> None:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE marketing_approvals SET executed_at = NOW() WHERE id = %s AND executed_at IS NULL",
                    (approval_id,))
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# 事件流 marketing_events(全量审计 + 近期营销动态时间线)
# ============================================================================
def add_event(*, event_type: str, case_id: Optional[int] = None, campaign_id: Optional[int] = None,
              severity: str = 'info', actor_id: Optional[int] = None, message: str = '',
              payload: Optional[dict] = None) -> None:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_events
              (case_id, campaign_id, event_type, severity, actor_id, message, payload_jsonb)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
            """,
            (case_id, campaign_id, event_type, severity, actor_id, message, _dumps(payload)),
        )
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] add_event(%s) 失败: %s", event_type, e)
    finally:
        conn.close()


def list_events(limit: int = 50, event_type: Optional[str] = None) -> list:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if event_type:
            cur.execute("SELECT * FROM marketing_events WHERE event_type = %s "
                        "ORDER BY created_at DESC LIMIT %s", (event_type, limit))
        else:
            cur.execute("SELECT * FROM marketing_events ORDER BY created_at DESC LIMIT %s", (limit,))
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] list_events 失败: %s", e)
        return []
    finally:
        conn.close()


# ============================================================================
# 活动 marketing_campaigns(budget_cap_points NOT NULL)
# ============================================================================
def upsert_campaign(*, campaign_code: Optional[str], campaign_type: str, name: str,
                    budget_cap_points: int, params: Optional[dict] = None,
                    is_resident: bool = False, dry_run: bool = False,
                    status: str = 'draft', case_id: Optional[int] = None,
                    created_by: Optional[int] = None,
                    starts_at: Optional[datetime] = None,
                    ends_at: Optional[datetime] = None,
                    seed_mode: bool = False) -> tuple[dict, bool]:
    if campaign_type not in CAMPAIGN_TYPES:
        raise ValueError(f"campaign_type 非法: {campaign_type}")
    if status not in CAMPAIGN_STATUSES:
        raise ValueError(f"status 非法: {status}")
    if budget_cap_points is None:
        raise ValueError("budget_cap_points 不能为空(NOT NULL)")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_campaigns
              (campaign_code, campaign_type, name, budget_cap_points, params_jsonb,
               is_resident, dry_run, status, case_id, created_by, starts_at, ends_at)
            VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)
            """
            + ("ON CONFLICT (campaign_code) DO NOTHING"  # [返工 R2] seed 模式:不覆盖运营的 activate/预算/参数
               if seed_mode else
               """ON CONFLICT (campaign_code) DO UPDATE SET
              name = EXCLUDED.name, budget_cap_points = EXCLUDED.budget_cap_points,
              params_jsonb = EXCLUDED.params_jsonb, status = EXCLUDED.status,
              updated_at = NOW()""")
            + "\n            RETURNING *, (xmax = 0) AS _inserted\n            ",
            (campaign_code, campaign_type, name, int(budget_cap_points), _dumps(params),
             is_resident, dry_run, status, case_id, created_by, starts_at, ends_at),
        )
        fetched = cur.fetchone()  # seed_mode + 已存在 → DO NOTHING 无返回行
        conn.commit()
        if not fetched:
            return {}, False
        row = dict(fetched)
        inserted = bool(row.pop('_inserted', False))
        return row, inserted
    finally:
        conn.close()


def set_campaign_status(campaign_id: int, status: str) -> None:
    if status not in CAMPAIGN_STATUSES:
        raise ValueError(f"status 非法: {status}")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE marketing_campaigns SET status = %s, updated_at = NOW() WHERE id = %s",
                    (status, campaign_id))
        conn.commit()
    finally:
        conn.close()


def get_campaign_by_code(campaign_code: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        # [返工 R1] is_live 在 DB 侧算(单一时钟源):status=active 且在 starts_at/ends_at 窗内。
        # starts_at/ends_at 由 DB NOW() 写入,判活也必须用 DB NOW() 比——Python 本机钟与容器钟可能相差数小时。
        cur.execute(
            """SELECT *, (status = 'active'
                          AND (starts_at IS NULL OR starts_at <= NOW())
                          AND (ends_at   IS NULL OR ends_at   >= NOW())) AS is_live
               FROM marketing_campaigns WHERE campaign_code = %s""", (campaign_code,))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] get_campaign_by_code 失败: %s", e)
        return None
    finally:
        conn.close()


def list_campaigns(status: Optional[str] = None) -> list:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if status:
            cur.execute("SELECT * FROM marketing_campaigns WHERE status = %s ORDER BY created_at DESC", (status,))
        else:
            cur.execute("SELECT * FROM marketing_campaigns ORDER BY created_at DESC")
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] list_campaigns 失败: %s", e)
        return []
    finally:
        conn.close()


def add_campaign_spent(campaign_id: int, points: int) -> None:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE marketing_campaigns SET spent_points = spent_points + %s, updated_at = NOW() WHERE id = %s",
                    (int(points), campaign_id))
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# 发放台账 marketing_grants(pool 锁 bonus · 语义幂等键 · 台账先落幂等闸)
#   注意:此函数只写台账,不动钱包。真正入账 bonus_points 由 Package C 执行器在
#   同一事务/post-commit 里调 wallet 公共 mutator 完成。dry_run=True 时只落模拟行。
# ============================================================================
def create_grant_ledger(*, grant_key: str, user_id: int, points: int,
                        campaign_id: Optional[int] = None, grant_type: str = 'marketing',
                        related_order_id: str = '', dry_run: bool = False,
                        metadata: Optional[dict] = None) -> tuple[dict, bool]:
    """台账先落(幂等闸)。返回 (grant, inserted)。inserted=False 表示幂等命中(勿重复入账)。"""
    if points <= 0:
        return {}, False
    status = 'dry_run' if dry_run else 'applied'
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_grants
              (grant_key, campaign_id, user_id, points, pool, grant_type,
               related_order_id, status, dry_run, metadata_jsonb, applied_at)
            VALUES (%s,%s,%s,%s,'bonus',%s,%s,%s,%s,%s::jsonb,
                    CASE WHEN %s THEN NULL ELSE NOW() END)
            ON CONFLICT (grant_key) DO NOTHING
            RETURNING *
            """,
            (grant_key, campaign_id, user_id, int(points), grant_type,
             related_order_id, status, dry_run, _dumps(metadata), dry_run),
        )
        row = cur.fetchone()
        if row:
            conn.commit()
            return dict(row), True
        cur.execute("SELECT * FROM marketing_grants WHERE grant_key = %s", (grant_key,))
        existing = cur.fetchone()
        conn.commit()
        return (dict(existing) if existing else {}), False
    finally:
        conn.close()


def reverse_grant(grant_id: int) -> Optional[dict]:
    """撤销/冲销:写一条负向 grant 并把原 grant 置 reversed。返回冲销行。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM marketing_grants WHERE id = %s FOR UPDATE", (grant_id,))
        orig = cur.fetchone()
        if not orig or orig['status'] != 'applied':
            conn.rollback()
            return None
        rev_key = f"reverse:{orig['grant_key']}"
        cur.execute(
            """
            INSERT INTO marketing_grants
              (grant_key, campaign_id, user_id, points, pool, grant_type,
               related_order_id, status, dry_run, reversed_of, metadata_jsonb, reversed_at)
            VALUES (%s,%s,%s,%s,'bonus',%s,%s,'reversed',FALSE,%s,%s::jsonb,NOW())
            ON CONFLICT (grant_key) DO NOTHING
            RETURNING *
            """,
            (rev_key, orig['campaign_id'], orig['user_id'], -abs(orig['points']),
             orig['grant_type'], orig['related_order_id'], orig['id'],
             _dumps({'reversal_of': orig['id']})),
        )
        rev = cur.fetchone()
        cur.execute("UPDATE marketing_grants SET status = 'reversed', reversed_at = NOW() WHERE id = %s",
                    (grant_id,))
        conn.commit()
        return dict(rev) if rev else None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] reverse_grant(%s) 失败: %s", grant_id, e)
        return None
    finally:
        conn.close()


def sum_grants_for_user(user_id: int, since_days: Optional[int] = None) -> int:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if since_days:
            cur.execute(
                "SELECT COALESCE(SUM(points),0)::bigint AS s FROM marketing_grants "
                "WHERE user_id=%s AND status='applied' AND applied_at > NOW() - (%s||' days')::interval",
                (user_id, str(int(since_days))))
        else:
            cur.execute("SELECT COALESCE(SUM(points),0)::bigint AS s FROM marketing_grants "
                        "WHERE user_id=%s AND status='applied'", (user_id,))
        return int(cur.fetchone()['s'])
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] sum_grants_for_user 失败: %s", e)
        return 0
    finally:
        conn.close()


def sum_grants_today() -> int:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(SUM(points),0)::bigint AS s FROM marketing_grants "
                    "WHERE status='applied' AND applied_at::date = (NOW() AT TIME ZONE 'Asia/Shanghai')::date")
        return int(cur.fetchone()['s'])
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] sum_grants_today 失败: %s", e)
        return 0
    finally:
        conn.close()


# ============================================================================
# 触达记录 marketing_touch_events(= 频控数据源)
# ============================================================================
def record_touch(*, user_id: int, channel: str = 'station', audience_segment: str = 'end',
                 case_id: Optional[int] = None, campaign_id: Optional[int] = None,
                 content_ref: str = '', status: str = 'sent',
                 suppressed_reason: str = '', dry_run: bool = False) -> dict:
    if channel not in TOUCH_CHANNELS:
        raise ValueError(f"channel 非法: {channel}")
    if status not in TOUCH_STATUSES:
        raise ValueError(f"status 非法: {status}")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_touch_events
              (user_id, channel, audience_segment, case_id, campaign_id,
               content_ref, status, suppressed_reason, dry_run)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
            """,
            (user_id, channel, audience_segment, case_id, campaign_id,
             content_ref, status, suppressed_reason, dry_run),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def touched_within_days(user_id: int, days: int) -> bool:
    """频控:该用户最近 days 天内是否已被成功触达过(status='sent')。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM marketing_touch_events "
            "WHERE user_id=%s AND status='sent' AND created_at > NOW() - (%s||' days')::interval LIMIT 1",
            (user_id, str(int(days))))
        return cur.fetchone() is not None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] touched_within_days 失败: %s", e)
        return False
    finally:
        conn.close()


def count_touches_today(segment: Optional[str] = None) -> int:
    """[返工 R6-6] segment 维度分计:服务商/终端各吃各的日上限(不传 = 全局计)。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if segment:
            cur.execute("SELECT COUNT(*)::int AS c FROM marketing_touch_events "
                        "WHERE status='sent' AND created_at::date = CURRENT_DATE "
                        "AND audience_segment = %s", (segment,))
        else:
            cur.execute("SELECT COUNT(*)::int AS c FROM marketing_touch_events "
                        "WHERE status='sent' AND created_at::date = CURRENT_DATE")
        return int(cur.fetchone()['c'])
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] count_touches_today 失败: %s", e)
        return 0
    finally:
        conn.close()


def is_opted_out(user_id: int) -> bool:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT opted_out FROM marketing_optouts WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return bool(row and row['opted_out'])
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] is_opted_out 失败: %s", e)
        return False
    finally:
        conn.close()


def set_optout(user_id: int, opted_out: bool = True, source: str = 'user') -> None:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_optouts (user_id, opted_out, source, updated_at)
            VALUES (%s,%s,%s,NOW())
            ON CONFLICT (user_id) DO UPDATE SET
              opted_out = EXCLUDED.opted_out, source = EXCLUDED.source, updated_at = NOW()
            """,
            (user_id, opted_out, source),
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# 回测 marketing_measurements(相关转化三指标 · 幂等 unit)
# ============================================================================
def upsert_measurement(*, case_id: Optional[int], metric_type: str, window_days: int,
                       user_id: Optional[int] = None, campaign_id: Optional[int] = None,
                       converted: bool = False, result: Optional[dict] = None,
                       is_control: bool = False, window_start: Optional[datetime] = None,
                       window_end: Optional[datetime] = None) -> dict:
    if metric_type not in MEASURE_METRICS:
        raise ValueError(f"metric_type 非法: {metric_type}")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_measurements
              (case_id, campaign_id, user_id, metric_type, window_days, window_start,
               window_end, converted, result_jsonb, is_control, measured_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,NOW())
            ON CONFLICT (case_id, COALESCE(user_id, -1), metric_type, window_days) DO UPDATE SET
              converted = EXCLUDED.converted, result_jsonb = EXCLUDED.result_jsonb,
              measured_at = NOW()
            RETURNING *
            """,
            (case_id, campaign_id, user_id, metric_type, int(window_days), window_start,
             window_end, converted, _dumps(result), is_control),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def measurement_summary_by_skillpack() -> list:
    """框架标签归因:按案件 skill_packs 标签聚合相关转化率(度量学习 §F.2)。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT sp AS skill_pack,
                   COUNT(*)::int AS measured,
                   COUNT(*) FILTER (WHERE m.converted)::int AS converted,
                   ROUND(100.0 * COUNT(*) FILTER (WHERE m.converted) / NULLIF(COUNT(*),0), 2) AS conv_rate_pct
            FROM marketing_measurements m
            JOIN marketing_cases c ON c.id = m.case_id
            CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE WHEN jsonb_typeof(c.skill_packs_jsonb)='array'
                     THEN c.skill_packs_jsonb ELSE '[]'::jsonb END) AS sp
            GROUP BY sp ORDER BY conv_rate_pct DESC NULLS LAST
            """
        )
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] measurement_summary_by_skillpack 失败: %s", e)
        return []
    finally:
        conn.close()


# ============================================================================
# 物料工厂 jobs / attempts / assets
# ============================================================================
def create_material_job(*, user_id: int, owner_scope: str = 'user', brand_id: Optional[int] = None,
                        case_id: Optional[int] = None, template_id: Optional[int] = None,
                        material_kind: str = 'poster', feature_code: str = '',
                        input_fields: Optional[dict] = None, final_prompt: str = '',
                        size: str = '3:4', resolution: str = '1k',
                        cost_points: int = 0, billing_ref: str = '') -> dict:
    if material_kind not in MATERIAL_KINDS:
        raise ValueError(f"material_kind 非法: {material_kind}")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_material_jobs
              (owner_scope, user_id, brand_id, case_id, template_id, material_kind,
               feature_code, input_fields_jsonb, final_prompt, size, resolution,
               status, cost_points, billing_ref)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,'pending',%s,%s)
            RETURNING *
            """,
            (owner_scope, user_id, brand_id, case_id, template_id, material_kind,
             feature_code, _dumps(input_fields), final_prompt, size, resolution,
             int(cost_points), billing_ref),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def create_or_get_material_job(
    *,
    user_id: int,
    request_id: str,
    request_hash: str,
    owner_scope: str = 'user',
    brand_id: Optional[int] = None,
    case_id: Optional[int] = None,
    template_id: Optional[int] = None,
    material_kind: str = 'bundle',
    feature_code: str = '',
    input_fields: Optional[dict] = None,
    final_prompt: str = '',
    size: str = '3:4',
    resolution: str = '1k',
    cost_points: int = 0,
    billing_ref: str = '',
) -> tuple[dict, bool]:
    """Create one material job per user/request id, without a schema change.

    The immutable GEO snapshot stores ``request_id`` and ``request_hash`` in
    ``input_fields_jsonb._geo``.  A transaction advisory lock serializes
    retries before billing.  Reusing a request id for a different payload is a
    hard conflict instead of silently replaying the wrong job.

    Returns ``(job, created)``.
    """
    request_id = str(request_id or '').strip()
    request_hash = str(request_hash or '').strip()
    if not request_id or not request_hash:
        raise ValueError('material_request_identity_required')
    if material_kind not in MATERIAL_KINDS:
        raise ValueError(f"material_kind 非法: {material_kind}")
    payload = dict(input_fields or {})
    geo_snapshot = dict(payload.get('_geo') or {})
    if geo_snapshot.get('request_id') not in (None, '', request_id):
        raise ValueError('material_request_identity_payload_mismatch')
    if geo_snapshot.get('request_hash') not in (None, '', request_hash):
        raise ValueError('material_request_identity_payload_mismatch')
    geo_snapshot.update({'request_id': request_id, 'request_hash': request_hash})
    payload['_geo'] = geo_snapshot
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"marketing-material:{int(user_id)}:{request_id}",),
        )
        cur.execute(
            """
            SELECT * FROM marketing_material_jobs
            WHERE user_id=%s
              AND input_fields_jsonb #>> '{_geo,request_id}' = %s
            ORDER BY id DESC LIMIT 1
            """,
            (int(user_id), request_id),
        )
        existing = cur.fetchone()
        if existing:
            row = dict(existing)
            geo = (row.get('input_fields_jsonb') or {}).get('_geo') or {}
            if str(geo.get('request_hash') or '') != request_hash:
                raise ValueError('material_request_id_conflict')
            conn.commit()
            return row, False
        cur.execute(
            """
            INSERT INTO marketing_material_jobs
              (owner_scope, user_id, brand_id, case_id, template_id, material_kind,
               feature_code, input_fields_jsonb, final_prompt, size, resolution,
               status, cost_points, billing_ref)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,'pending',%s,%s)
            RETURNING *
            """,
            (
                owner_scope, int(user_id), brand_id, case_id, template_id, material_kind,
                feature_code, _dumps(payload), final_prompt, size, resolution,
                int(cost_points), billing_ref,
            ),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return row, True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def patch_job_input_fields(job_id: int, patch: dict) -> None:
    """Merge a server-owned JSON patch into a job snapshot."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE marketing_material_jobs
            SET input_fields_jsonb = input_fields_jsonb || %s::jsonb
            WHERE id=%s
            """,
            (_dumps(patch), int(job_id)),
        )
        if cur.rowcount != 1:
            raise ValueError('material_job_not_found')
        conn.commit()
    finally:
        conn.close()


def update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    allowed = {'status', 'freeze_id', 'billing_ref', 'final_prompt', 'block_reason',
               'error_summary', 'cost_points'}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = %s")
            params.append(v)
    if fields.get('status') in ('succeeded', 'failed', 'blocked'):
        sets.append("finished_at = NOW()")
    if not sets:
        return
    params.append(job_id)
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE marketing_material_jobs SET {', '.join(sets)} WHERE id = %s "
            "RETURNING id,user_id,status,finished_at",
            tuple(params),
        )
        row = cur.fetchone()
        if row and row.get('status') in ('succeeded', 'failed'):
            from services.notification_events import NotificationEventType, RecipientKind
            from services.notification_outbox import enqueue_notification_event

            succeeded = row['status'] == 'succeeded'
            enqueue_notification_event(
                cur,
                event_type=(
                    NotificationEventType.ASSET_COMPLETED
                    if succeeded else NotificationEventType.ASSET_FAILED
                ),
                business_id=str(row['id']),
                terminal_state='completed' if succeeded else 'failed',
                recipient_user_id=int(row['user_id']),
                recipient_kind=RecipientKind.USER,
                facts={
                    'business_no': f"ASSET-{row['id']}",
                    'status': '营销物料已生成' if succeeded else '营销物料生成未完成',
                    'occurred_at': row['finished_at'].isoformat(timespec='seconds'),
                    'summary': '请在营销物料页面查看结果和下一步。',
                },
            )
        conn.commit()
    finally:
        conn.close()


def get_job(job_id: int) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM marketing_material_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] get_job(%s) 失败: %s", job_id, e)
        return None
    finally:
        conn.close()


def get_job_by_request_id(user_id: int, request_id: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM marketing_material_jobs WHERE user_id=%s "
            "AND input_fields_jsonb #>> '{_geo,request_id}'=%s ORDER BY id DESC LIMIT 1",
            (int(user_id), str(request_id)),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_recoverable_geo_jobs(limit: int = 100) -> list[dict]:
    """Return only durable reconciliation work; never ordinary generation."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT j.*
            FROM marketing_material_jobs j
            LEFT JOIN marketing_material_generation_attempts a ON a.job_id=j.id
            WHERE j.status='generating'
              AND j.input_fields_jsonb ? '_geo'
              AND (
                j.error_summary IN ('billing_commit_pending','billing_release_pending','provider_resolution_pending')
                OR (
                  a.resolution_state='manual_resolved'
                  AND a.submit_state='submitted'
                  AND a.poll_state='succeeded'
                  AND a.materialization_state<>'materialized'
                )
              )
            ORDER BY j.id
            LIMIT %s
            """,
            (max(1, min(int(limit), 500)),),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def count_jobs_today(user_id: int) -> int:
    """日上限实时 COUNT(不含 blocked 未扣费的)。"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*)::int AS c FROM marketing_material_jobs "
            "WHERE user_id=%s AND created_at::date = (NOW() AT TIME ZONE 'Asia/Shanghai')::date AND status <> 'blocked'",
            (user_id,))
        return int(cur.fetchone()['c'])
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] count_jobs_today 失败: %s", e)
        return 0
    finally:
        conn.close()


def add_attempt(*, job_id: int, attempt_no: int, component_id: str = '',
                provider: str = 'apimart-gpt-image-2',
                provider_task_id: str = '', status: str = 'pending',
                provider_cost_usd: float = 0.0, safety_status: str = 'passed',
                safety_flags: Optional[list] = None, error_detail: str = '',
                 submit_state: str = 'not_started', poll_state: str = 'not_started',
                 submit_guard_token: str = '', provider_result: Optional[dict] = None,
                 materialization_state: str = 'not_started',
                 resolution_state: str = 'automatic') -> dict:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_material_generation_attempts
              (job_id, component_id, attempt_no, provider, provider_task_id, status,
               provider_cost_usd, safety_status, safety_flags_jsonb, error_detail,
               submit_state,poll_state,submit_guard_token,started_at,last_heartbeat_at,
               provider_result_jsonb,materialization_state,resolution_state)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,NOW(),NOW(),%s::jsonb,%s,%s)
            RETURNING *
            """,
            (job_id, component_id, attempt_no, provider, provider_task_id, status,
             provider_cost_usd, safety_status, _dumps(safety_flags or []), error_detail,
             submit_state, poll_state, submit_guard_token, _dumps(provider_result or {}),
             materialization_state, resolution_state),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def finish_attempt(attempt_id: int, *, status: str, provider_cost_usd: float = 0.0,
                   safety_status: str = 'passed', error_detail: str = '',
                   safety_flags: Optional[list] = None, provider_task_id: Optional[str] = None,
                   submit_state: Optional[str] = None, poll_state: Optional[str] = None,
                   provider_result: Optional[dict] = None,
                   materialization_state: Optional[str] = None,
                   resolution_state: Optional[str] = None) -> dict:
    """Atomically persist all provider/safety terminal evidence for an attempt."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE marketing_material_generation_attempts "
            "SET status=%s, provider_cost_usd=%s, safety_status=%s, error_detail=%s, "
            "safety_flags_jsonb=%s::jsonb, provider_task_id=COALESCE(%s,provider_task_id), "
            "submit_state=COALESCE(%s,submit_state),poll_state=COALESCE(%s,poll_state), "
            "provider_result_jsonb=COALESCE(%s::jsonb,provider_result_jsonb),"
            "materialization_state=COALESCE(%s,materialization_state),"
            "resolution_state=COALESCE(%s,resolution_state),"
            "resolved_at=CASE WHEN %s='manual_resolved' THEN NOW() ELSE resolved_at END, "
            "last_heartbeat_at=NOW(),finished_at=NOW() "
            "WHERE id=%s AND status IN ('pending','running') RETURNING *",
            (status, provider_cost_usd, safety_status, error_detail, _dumps(safety_flags or []),
             provider_task_id, submit_state, poll_state,
             _dumps(provider_result) if provider_result is not None else None,
             materialization_state, resolution_state, resolution_state, attempt_id))
        row = cur.fetchone()
        if not row:
            raise ValueError("provider_attempt_already_terminal")
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def list_attempts(job_id: int, component_id: Optional[str] = None) -> list[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if component_id is None:
            cur.execute("SELECT * FROM marketing_material_generation_attempts WHERE job_id=%s ORDER BY id", (job_id,))
        else:
            cur.execute("SELECT * FROM marketing_material_generation_attempts WHERE job_id=%s AND component_id=%s ORDER BY attempt_no,id", (job_id, component_id))
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def anchor_attempt_before_submit(attempt_id: int, guard_token: str) -> dict:
    """CAS a not-started row before the paid POST. A crash after this is unknown, never retryable."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE marketing_material_generation_attempts SET status='running',submit_state='anchored',"
            "submit_guard_token=%s,last_heartbeat_at=NOW(),started_at=COALESCE(started_at,NOW()) "
            "WHERE id=%s AND status='pending' AND submit_state='not_started' RETURNING *",
            (guard_token, attempt_id),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("provider_submit_guard_conflict")
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def update_attempt_provider_state(attempt_id: int, *, submit_state: Optional[str] = None,
                                  poll_state: Optional[str] = None,
                                  provider_task_id: Optional[str] = None,
                                  error_detail: Optional[str] = None,
                                  provider_result: Optional[dict] = None,
                                  materialization_state: Optional[str] = None,
                                  resolution_state: Optional[str] = None) -> dict:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE marketing_material_generation_attempts SET "
            "submit_state=COALESCE(%s,submit_state),poll_state=COALESCE(%s,poll_state),"
            "provider_task_id=COALESCE(%s,provider_task_id),error_detail=COALESCE(%s,error_detail),"
            "provider_result_jsonb=COALESCE(%s::jsonb,provider_result_jsonb),"
            "materialization_state=COALESCE(%s,materialization_state),"
            "resolution_state=COALESCE(%s,resolution_state),"
            "resolved_at=CASE WHEN %s='manual_resolved' THEN NOW() ELSE resolved_at END,"
            "last_heartbeat_at=NOW() WHERE id=%s AND status IN ('pending','running') RETURNING *",
            (submit_state, poll_state, provider_task_id, error_detail,
             _dumps(provider_result) if provider_result is not None else None,
             materialization_state, resolution_state, resolution_state, attempt_id),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("provider_attempt_not_live")
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def get_attempt(attempt_id: int) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM marketing_material_generation_attempts WHERE id=%s", (int(attempt_id),))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def resolve_unknown_attempt(*, attempt_id: int, job_id: int, resolution: str,
                            provider_task_id: str = '', image_url: str = '',
                            operator_user_id: int, note: str = '') -> dict:
    """CAS an operator-verified provider outcome on the same paid attempt.

    This never creates an attempt. ``not_sent`` is the only resolution that
    makes a later, explicitly triggered attempt eligible to submit a POST.
    """
    if resolution not in {'succeeded', 'failed', 'not_sent'}:
        raise ValueError('provider_resolution_invalid')
    if resolution == 'succeeded' and (not provider_task_id or not image_url):
        raise ValueError('provider_success_evidence_required')
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM marketing_material_generation_attempts "
            "WHERE id=%s AND job_id=%s FOR UPDATE",
            (int(attempt_id), int(job_id)),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError('provider_attempt_not_found')
        if str(row.get('submit_state') or '') != 'outcome_unknown' or str(row.get('resolution_state') or '') != 'manual_required':
            raise ValueError('provider_attempt_not_unknown')
        cur.execute("SELECT * FROM marketing_material_jobs WHERE id=%s FOR UPDATE", (int(job_id),))
        job = cur.fetchone()
        if not job:
            raise ValueError('material_job_not_found')
        audit = {
            'resolution': resolution,
            'operator_user_id': int(operator_user_id),
            'note': str(note or '')[:500],
        }
        if resolution == 'succeeded':
            result = {
                'image_url': str(image_url),
                'manual_resolution': audit,
            }
            cur.execute(
                """
                UPDATE marketing_material_generation_attempts
                SET status='running',submit_state='submitted',poll_state='succeeded',
                    provider_task_id=%s,provider_result_jsonb=%s::jsonb,
                    materialization_state='pending',resolution_state='manual_resolved',
                    resolved_at=NOW(),last_heartbeat_at=NOW(),error_detail=''
                WHERE id=%s RETURNING *
                """,
                (str(provider_task_id), _dumps(result), int(attempt_id)),
            )
        else:
            submit_state = 'not_sent' if resolution == 'not_sent' else 'rejected'
            cur.execute(
                """
                UPDATE marketing_material_generation_attempts
                SET status='failed',submit_state=%s,poll_state='failed',
                    provider_result_jsonb=%s::jsonb,materialization_state='failed',
                    resolution_state='manual_resolved',resolved_at=NOW(),
                    last_heartbeat_at=NOW(),finished_at=NOW(),error_detail=%s
                WHERE id=%s RETURNING *
                """,
                (submit_state, _dumps({'manual_resolution': audit}),
                 f'manual_provider_resolution:{resolution}', int(attempt_id)),
            )
        resolved = dict(cur.fetchone())
        next_error = 'provider_resolution_pending' if resolution == 'failed' else ''
        cur.execute(
            "UPDATE marketing_material_jobs SET status='generating',error_summary=%s WHERE id=%s",
            (next_error, int(job_id)),
        )
        if resolution == 'not_sent' and str(job.get('billing_ref') or '').startswith('org:'):
            charge_id = int(str(job['billing_ref']).split(':', 1)[1])
            # Operator proved the anchored POST never left this process. Reset
            # only execution authority/lease; reservation amounts and wallet
            # semantics remain untouched.
            cur.execute(
                """
                UPDATE organization_work_outbox
                SET status='pending',claim_token=NULL,lease_until=NULL,claimed_at=NULL,
                    execution_started_at=NULL,external_side_effect_started_at=NULL,
                    last_error_code='provider_not_sent_manually_verified',updated_at=NOW()
                WHERE charge_link_id=%s AND status IN ('claimed','running')
                """,
                (charge_id,),
            )
            if cur.rowcount != 1:
                raise ValueError('organization_unknown_resolution_conflict')
            cur.execute(
                """
                UPDATE organization_charge_links
                SET attempt_token=NULL,lease_until=NULL,execution_started_at=NULL,
                    external_side_effect_started_at=NULL,updated_at=NOW()
                WHERE id=%s AND status='reserved'
                """,
                (charge_id,),
            )
            if cur.rowcount != 1:
                raise ValueError('organization_unknown_resolution_conflict')
        conn.commit()
        return resolved
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def materialize_attempt_asset(*, attempt_id: int, provider_cost_usd: float = 0.0,
                              safety_status: str = 'passed', safety_flags: Optional[list] = None,
                              asset_kind: str = 'bundle_item', url_provider: str = '',
                              url_stored: str = '', thumbnail_url: str = '',
                              width: int = 0, height: int = 0, size_bytes: int = 0,
                              sha256: str = '') -> dict:
    """Atomically publish one asset and close its immutable provider attempt."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM marketing_material_generation_attempts WHERE id=%s FOR UPDATE",
            (int(attempt_id),),
        )
        attempt = cur.fetchone()
        if not attempt:
            raise ValueError('provider_attempt_not_found')
        component_id = str(attempt.get('component_id') or '')
        if not component_id or str(attempt.get('poll_state') or '') != 'succeeded':
            raise ValueError('provider_attempt_not_materializable')
        cur.execute(
            "SELECT * FROM marketing_material_assets WHERE job_id=%s AND bundle_slot=%s "
            "ORDER BY id DESC LIMIT 1 FOR UPDATE",
            (int(attempt['job_id']), component_id),
        )
        asset = cur.fetchone()
        if not asset:
            cur.execute(
                """
                INSERT INTO marketing_material_assets
                  (job_id,asset_kind,bundle_slot,url_provider,url_stored,thumbnail_url,
                   width,height,size_bytes,sha256,is_final,publish_allowed,rights_confirmed)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,1,0)
                RETURNING *
                """,
                (int(attempt['job_id']), asset_kind, component_id, url_provider,
                 url_stored, thumbnail_url, int(width), int(height), int(size_bytes), sha256),
            )
            asset = cur.fetchone()
        if str(attempt.get('status') or '') in {'pending', 'running'}:
            cur.execute(
                """
                UPDATE marketing_material_generation_attempts
                SET status='succeeded',provider_cost_usd=%s,safety_status=%s,
                    safety_flags_jsonb=%s::jsonb,materialization_state='materialized',
                    last_heartbeat_at=NOW(),finished_at=NOW(),error_detail=''
                WHERE id=%s
                """,
                (float(provider_cost_usd), safety_status, _dumps(safety_flags or []), int(attempt_id)),
            )
        elif str(attempt.get('status') or '') == 'succeeded':
            cur.execute(
                "UPDATE marketing_material_generation_attempts SET materialization_state='materialized' "
                "WHERE id=%s",
                (int(attempt_id),),
            )
        else:
            raise ValueError('provider_attempt_not_materializable')
        conn.commit()
        return dict(asset)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_asset(*, job_id: int, asset_kind: str = 'poster', bundle_slot: str = '',
              url_provider: str = '', url_stored: str = '', thumbnail_url: str = '',
              content_text: str = '', width: int = 0, height: int = 0,
              size_bytes: int = 0, sha256: str = '', is_final: bool = True,
              whitelabel_applied: bool = False, publish_allowed: int = 1,
              rights_confirmed: int = 0) -> dict:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_material_assets
              (job_id, asset_kind, bundle_slot, url_provider, url_stored, thumbnail_url,
               content_text, width, height, size_bytes, sha256, is_final,
               whitelabel_applied, publish_allowed, rights_confirmed)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
            """,
            (job_id, asset_kind, bundle_slot, url_provider, url_stored, thumbnail_url,
             content_text, width, height, size_bytes, sha256, is_final,
             whitelabel_applied, publish_allowed, rights_confirmed),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def list_assets(job_id: int) -> list:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM marketing_material_assets WHERE job_id = %s ORDER BY created_at", (job_id,))
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] list_assets 失败: %s", e)
        return []
    finally:
        conn.close()


def list_user_materials(user_id: int, limit: int = 50) -> list:
    """我的物料库/历史记录:该用户全部 job(含 generating/failed/blocked)+ 其成品资产。

    [2026-07-05 老板拍板"要有放历史生成的地方"] 不再只查 succeeded:
    生成中/失败(已退算力)/被合规拦截 都要在历史里看得见,异步生成才有交代。
    rights_confirmed 一并返回,前端「已可外发」徽章才有数据源。
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT j.id AS job_id, j.material_kind, j.status, j.created_at, j.input_fields_jsonb,
                   j.error_summary,
                   COALESCE(json_agg(json_build_object(
                     'id', a.id, 'kind', a.asset_kind, 'url', a.url_stored,
                     'thumb', a.thumbnail_url, 'text', a.content_text, 'slot', a.bundle_slot,
                     'bundle_slot', a.bundle_slot,
                     'rights_confirmed', a.rights_confirmed
                   ) ORDER BY a.created_at) FILTER (WHERE a.id IS NOT NULL), '[]'::json) AS assets
            FROM marketing_material_jobs j
            LEFT JOIN marketing_material_assets a ON a.job_id = j.id AND a.is_final = TRUE
            WHERE j.user_id = %s
            GROUP BY j.id ORDER BY j.created_at DESC LIMIT %s
            """,
            (user_id, limit))
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] list_user_materials 失败: %s", e)
        return []
    finally:
        conn.close()


# ============================================================================
# 模板 marketing_material_templates
# ============================================================================
def list_templates(active_only: bool = True) -> list:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if active_only:
            cur.execute("SELECT * FROM marketing_material_templates WHERE is_active = TRUE ORDER BY sort, id")
        else:
            cur.execute("SELECT * FROM marketing_material_templates ORDER BY sort, id")
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] list_templates 失败: %s", e)
        return []
    finally:
        conn.close()


def get_template(template_id: int) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM marketing_material_templates WHERE id = %s", (template_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] get_template 失败: %s", e)
        return None
    finally:
        conn.close()


def upsert_template(*, template_code: str, scene_type: str, name: str,
                    material_kind: str = 'poster', prompt_skeleton: str = '',
                    default_size: str = '3:4', default_resolution: str = '1k',
                    bundle_spec: Optional[list] = None, style_tags: Optional[list] = None,
                    feature_code: str = '', sort: int = 100, is_active: bool = True,
                    seed_mode: bool = False) -> dict:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_material_templates
              (template_code, scene_type, name, material_kind, prompt_skeleton,
               default_size, default_resolution, bundle_spec_jsonb, style_tags_jsonb,
               feature_code, sort, is_active)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)
            """
            + ("ON CONFLICT (template_code) DO NOTHING"  # [返工 R2] seed 模式:存在即不动,不覆盖运营改动
               if seed_mode else
               """ON CONFLICT (template_code) DO UPDATE SET
              scene_type=EXCLUDED.scene_type, name=EXCLUDED.name,
              material_kind=EXCLUDED.material_kind, prompt_skeleton=EXCLUDED.prompt_skeleton,
              default_size=EXCLUDED.default_size, default_resolution=EXCLUDED.default_resolution,
              bundle_spec_jsonb=EXCLUDED.bundle_spec_jsonb, style_tags_jsonb=EXCLUDED.style_tags_jsonb,
              feature_code=EXCLUDED.feature_code, sort=EXCLUDED.sort,
              is_active=EXCLUDED.is_active, updated_at=NOW()""")
            + "\n            RETURNING *\n            ",
            (template_code, scene_type, name, material_kind, prompt_skeleton,
             default_size, default_resolution, _dumps(bundle_spec or []),
             _dumps(style_tags or []), feature_code, sort, is_active),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else {}
    finally:
        conn.close()


# ============================================================================
# 技能包 marketing_skill_packs(prompt 装配热加载)
# ============================================================================
def list_active_skill_packs() -> list:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM marketing_skill_packs WHERE is_active = TRUE ORDER BY weight DESC, id")
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] list_active_skill_packs 失败: %s", e)
        return []
    finally:
        conn.close()


def upsert_skill_pack(*, pack_code: str, title: str, content: str,
                      bind_signals: Optional[list] = None, weight: int = 100,
                      is_active: bool = True, seed_mode: bool = False) -> dict:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_skill_packs (pack_code, title, content, bind_signals_jsonb, weight, is_active)
            VALUES (%s,%s,%s,%s::jsonb,%s,%s)
            """
            + ("ON CONFLICT (pack_code) DO NOTHING"  # [返工 R2] seed 模式:热更新的包不被重启还原("改包不发版"保真)
               if seed_mode else
               """ON CONFLICT (pack_code) DO UPDATE SET
              title=EXCLUDED.title, content=EXCLUDED.content,
              bind_signals_jsonb=EXCLUDED.bind_signals_jsonb, weight=EXCLUDED.weight,
              is_active=EXCLUDED.is_active, updated_at=NOW()""")
            + "\n            RETURNING *\n            ",
            (pack_code, title, content, _dumps(bind_signals or []), weight, is_active),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else {}
    finally:
        conn.close()


# ============================================================================
# 巡逻打卡 marketing_patrol_runs
# ============================================================================
def record_patrol_run(*, signals_matched: int, cases_opened: int, cases_suppressed: int,
                      duration_ms: int, note: str = '') -> None:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_patrol_runs
              (signals_matched, cases_opened, cases_suppressed, duration_ms, note)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (signals_matched, cases_opened, cases_suppressed, duration_ms, note))
        # 自剪:只保留最近 500 条(照 ai_ops_patrol_runs)
        cur.execute(
            "DELETE FROM marketing_patrol_runs WHERE id NOT IN "
            "(SELECT id FROM marketing_patrol_runs ORDER BY ran_at DESC LIMIT 500)")
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] record_patrol_run 失败: %s", e)
    finally:
        conn.close()


def last_patrol_run() -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT *, EXTRACT(EPOCH FROM (NOW()-ran_at))::bigint AS age_seconds "
                    "FROM marketing_patrol_runs ORDER BY ran_at DESC LIMIT 1")
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] last_patrol_run 失败: %s", e)
        return None
    finally:
        conn.close()


# ============================================================================
# 只读视图读取(admin 面板用 · 白名单 marketing_v_*)
# ============================================================================
_VIEW_WHITELIST = {
    'marketing_v_funnel', 'marketing_v_pending_orders', 'marketing_v_wallet_dist',
    'marketing_v_feature_usage', 'marketing_v_effect', 'marketing_v_levers',
    'marketing_v_brands_health', 'marketing_v_self_ledger',
    'marketing_v_register_no_diagnosis', 'marketing_v_recharge_pulse',  # [返工 R6-9]
}


def read_view(view_name: str, limit: int = 200) -> list:
    """读白名单视图(admin 面板)。非白名单 → 拒绝(防注入)。fail-soft。"""
    if view_name not in _VIEW_WHITELIST:
        logger.warning("[marketing_db] read_view 拒绝非白名单: %s", view_name)
        return []
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {view_name} LIMIT %s", (limit,))
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] read_view(%s) 失败: %s", view_name, e)
        return []
    finally:
        conn.close()


def read_view_one(view_name: str) -> Optional[dict]:
    rows = read_view(view_name, limit=1)
    return rows[0] if rows else None


# ============================================================================
# 信号快照 get_marketing_signals(一次 round-trip · 全部日期数学 DB 侧 · fail-soft)
#   巡逻规则(Package B services/marketing/patrol.py)是纯函数,只吃这个 dict。
#   读 marketing_v_* 视图 + 少量定向查询(应用池连接,非受限 marketing_reader)。
# ============================================================================
def get_marketing_signals() -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
              -- 漏斗快照
              (SELECT row_to_json(f) FROM marketing_v_funnel f LIMIT 1) AS funnel,
              -- 钱包分布(体验耗尽人群)
              (SELECT COALESCE(SUM(exhausted_trial_cohort),0)::int FROM marketing_v_wallet_dist) AS exhausted_trial,
              -- pending 订单 >24h(挽单 · 手机号已掩码)
              (SELECT COALESCE(json_agg(json_build_object(
                  'order_id', order_id, 'user_id', user_id, 'username', username,
                  'phone_masked', phone_masked, 'amount_yuan', amount_yuan, 'age_seconds', age_seconds)), '[]'::json)
                FROM (SELECT * FROM marketing_v_pending_orders
                      WHERE age_seconds > 86400 ORDER BY age_seconds DESC LIMIT 20) p) AS pending_orders,
              -- 注册>7天且从未诊断(激活断点)· [返工 R6-9] 改读视图,裸表查询已收进 marketing_v_*
              (SELECT COALESCE(json_agg(json_build_object('user_id', v.user_id, 'username', v.username,
                  'age_days', v.age_days)), '[]'::json)
                FROM (SELECT user_id, username, age_days FROM marketing_v_register_no_diagnosis
                      ORDER BY created_at DESC LIMIT 20) v) AS register_no_diagnosis,
              -- 近 7 天付费充值笔数 + 最近充值距今(连续无人充值信号)· [返工 R6-9] 读视图
              (SELECT paid_recharges_7d FROM marketing_v_recharge_pulse) AS paid_recharges_7d,
              (SELECT last_recharge_age_seconds FROM marketing_v_recharge_pulse) AS last_recharge_age_seconds,
              -- 沉睡品牌(idle_30d 聚合)
              (SELECT COALESCE(SUM(idle_30d),0)::int FROM marketing_v_brands_health) AS idle_brands,
              -- 近 3 天新达标事件(案例时刻)
              (SELECT COALESCE(json_agg(json_build_object(
                  'quote_id', quote_id, 'check_date', check_date, 'compliant', keywords_compliant,
                  'avg_rate', avg_detection_rate)), '[]'::json)
                FROM (SELECT * FROM marketing_v_effect
                      WHERE keywords_compliant > 0 AND check_date > CURRENT_DATE - 3
                      ORDER BY check_date DESC LIMIT 20) e) AS recent_compliant,
              -- 功能使用聚合(低使用率卖点)
              (SELECT COALESCE(json_agg(json_build_object(
                  'feature_code', feature_code, 'feature_name', feature_name,
                  'usage', usage_count, 'users', distinct_users)), '[]'::json)
                FROM (SELECT * FROM marketing_v_feature_usage ORDER BY usage_count ASC LIMIT 20) fu) AS feature_usage_low
            """
        )
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("[marketing_db] get_marketing_signals 失败(本轮跳过): %s", e)
        return None
    finally:
        conn.close()


# ============================================================================
# 晒成交草稿(marketing_deal_drafts · migration_marketing_deal_drafts_2026_07_22.sql)
# ============================================================================
# 应用层 CHECK 镜像(与迁移 SQL 的 marketing_deal_drafts_status_ck 一致)
DEAL_DRAFT_STATUSES = ('draft', 'analyzed', 'redacted', 'confirmed', 'generating', 'completed', 'archived')


def create_or_get_deal_draft(
    *,
    owner_user_id: int,
    request_id: str,
    request_hash: str,
    brand_id: Optional[int] = None,
    form: Optional[dict] = None,
    organization_id: Optional[int] = None,
    created_by_membership_id: Optional[int] = None,
    compatible_request_hashes: Optional[tuple[str, ...]] = None,
) -> tuple[dict, bool]:
    """Create one deal draft per (owner, request id); replay or conflict loudly.

    The UNIQUE index is ``(owner_user_id, request_id)`` — the same granularity
    as the advisory lock and as ``get_job_by_request_id`` — so one tenant can
    neither squat another tenant's request ids nor probe their existence.
    ``request_hash`` is frozen into ``form_jsonb._request_hash``.  A transaction
    advisory lock serializes concurrent first submissions.  Reusing a request
    id for a different payload raises ``ValueError('deal_draft_request_id_conflict')``
    instead of silently replaying the wrong draft.

    ``organization_id``/``created_by_membership_id``(2026-07-23 外部审查 P1-1)
    只在首次 INSERT 冻结:组织身份创建的草稿绑定组织与创建成员,读取侧对
    绑定草稿做 live 成员复核,撤权/离组织即 fail-closed。个人身份草稿两列
    为 NULL,行为与历史完全一致。

    Concurrent same-key INSERTs: the loser's unique violation is caught, the
    winner's row is re-read outside the lock, and the outcome is the same
    replay/409 decision as the non-concurrent path — never a bare 500.

    Returns ``(draft, created)``.
    """
    request_id = str(request_id or '').strip()
    request_hash = str(request_hash or '').strip()
    if not request_id or not request_hash:
        raise ValueError('deal_draft_request_identity_required')
    payload = dict(form or {})
    if payload.get('_request_hash') not in (None, '', request_hash):
        raise ValueError('deal_draft_request_identity_payload_mismatch')
    payload['_request_hash'] = request_hash
    compatible_hashes = {
        str(value or '').strip()
        for value in (compatible_request_hashes or ())
        if str(value or '').strip()
    }

    def _is_replay(row: dict) -> bool:
        old_form = row.get('form_jsonb') or {}
        old_hash = str(old_form.get('_request_hash') or '')
        if old_hash == request_hash:
            return True
        if old_hash not in compatible_hashes:
            return False

        # Compatibility is intentionally narrow: it only accepts the immediately
        # preceding hash recipe, and only when every authority/payload field frozen
        # outside that hash still matches. A rejoined member therefore cannot
        # recover a draft created by the previous membership generation.
        def _same_optional_int(actual, expected) -> bool:
            if actual is None and expected is None:
                return True
            if actual is None or expected is None:
                return False
            return int(actual) == int(expected)

        public_old_form = {
            str(key): value for key, value in old_form.items()
            if not str(key).startswith('_')
        }
        return (
            _same_optional_int(row.get('brand_id'), brand_id)
            and _same_optional_int(row.get('organization_id'), organization_id)
            and _same_optional_int(
                row.get('created_by_membership_id'),
                created_by_membership_id,
            )
            and public_old_form == dict(form or {})
        )
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"marketing-deal-draft:{int(owner_user_id)}:{request_id}",),
        )
        cur.execute(
            "SELECT * FROM marketing_deal_drafts WHERE owner_user_id=%s AND request_id=%s",
            (int(owner_user_id), request_id),
        )
        existing = cur.fetchone()
        if existing:
            row = dict(existing)
            if not _is_replay(row):
                raise ValueError('deal_draft_request_id_conflict')
            conn.commit()
            return row, False
        try:
            cur.execute(
                """
                INSERT INTO marketing_deal_drafts
                  (owner_user_id, brand_id, request_id, form_jsonb, status,
                   organization_id, created_by_membership_id)
                VALUES (%s,%s,%s,%s::jsonb,'draft',%s,%s)
                RETURNING *
                """,
                (
                    int(owner_user_id), brand_id, request_id, _dumps(payload),
                    int(organization_id) if organization_id is not None else None,
                    int(created_by_membership_id) if created_by_membership_id is not None else None,
                ),
            )
            row = dict(cur.fetchone())
            conn.commit()
            return row, True
        except Exception as insert_exc:
            # 并发下同 (owner, request_id) 的首发:锁粒度与唯一索引一致,但
            # advisory lock 只挡得住同键竞争者;不同连接近乎同时的 INSERT 仍可能
            # 一方撞唯一约束。撞约束不裸抛 500:回滚后重查,命中即按 hash 走
            # replay/409,与顺序到达路径完全同口径。
            try:
                conn.rollback()
            except Exception:
                pass
            if not _is_unique_violation(insert_exc):
                raise
            winner = get_deal_draft_by_request_id(int(owner_user_id), request_id)
            if winner is None:
                raise
            if not _is_replay(winner):
                raise ValueError('deal_draft_request_id_conflict') from None
            return winner, False
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _is_unique_violation(exc: Exception) -> bool:
    """psycopg2 IntegrityError/unique_violation (SQLSTATE 23505), import-tolerant."""
    pgcode = getattr(exc, 'pgcode', None) or getattr(exc, 'sqlstate', None)
    if pgcode == '23505':
        return True
    return type(exc).__name__ == 'IntegrityError' and 'duplicate key' in str(exc).lower()


def get_deal_draft(draft_id: int) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM marketing_deal_drafts WHERE id=%s", (int(draft_id),))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_deal_draft_by_request_id(owner_user_id: int, request_id: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM marketing_deal_drafts WHERE owner_user_id=%s AND request_id=%s",
            (int(owner_user_id), str(request_id or '')),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_deal_draft(
    draft_id: int,
    *,
    form: Optional[dict] = None,
    materials: Optional[list] = None,
    sheet: Optional[dict] = None,
    status: Optional[str] = None,
    expected_updated_at=None,
) -> dict:
    """Replace whole JSONB columns (callers pass the merged document).

    ``expected_updated_at`` enables an optimistic lock: the UPDATE only lands
    when the row's ``updated_at`` still equals the value the caller read.
    A mismatch raises ``ValueError('deal_draft_update_conflict')`` so the
    caller can re-read and replay instead of silently overwriting a
    concurrent writer's changes (read-modify-whole-column write pattern).
    """
    if status is not None and status not in DEAL_DRAFT_STATUSES:
        raise ValueError(f"deal_draft_status 非法: {status}")
    sets: list[str] = ["updated_at=NOW()"]
    params: list = []
    if form is not None:
        sets.append("form_jsonb=%s::jsonb")
        params.append(_dumps(form))
    if materials is not None:
        sets.append("materials_jsonb=%s::jsonb")
        params.append(_dumps(list(materials)))
    if sheet is not None:
        sets.append("sheet_jsonb=%s::jsonb")
        params.append(_dumps(sheet))
    if status is not None:
        sets.append("status=%s")
        params.append(status)
    where = "id=%s"
    params.append(int(draft_id))
    if expected_updated_at is not None:
        where += " AND updated_at=%s"
        params.append(expected_updated_at)
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE marketing_deal_drafts SET {', '.join(sets)} WHERE {where} RETURNING *",
            tuple(params),
        )
        row = cur.fetchone()
        if not row:
            if expected_updated_at is not None:
                cur.execute(
                    "SELECT 1 FROM marketing_deal_drafts WHERE id=%s",
                    (int(draft_id),),
                )
                if cur.fetchone():
                    conn.rollback()
                    raise ValueError('deal_draft_update_conflict')
            raise ValueError('deal_draft_not_found')
        conn.commit()
        return dict(row)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
