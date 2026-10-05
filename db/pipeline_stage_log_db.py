"""
pipeline_stage_log — M1a T4 · 9 步代理工作流埋点表 + 数据访问

CTO-15.9 2026-04-25

定位(PRD M1a §6 + Codex v1.1 审后口径):
  本表是 M4 `stage_runs` 主表的兼容壳 · 不是另起炉灶
  M1a 只建最小 log 字段 · M4 Phase B 时 stage_runs 继承本表 schema + ALTER 加状态机字段(数据不迁)

字段:
  id          SERIAL PK
  brand_id    INTEGER NOT NULL · 关联 brands(id)
  stage_name  TEXT NOT NULL · diagnosis/quote/pay/write/publish/monitor/report/renew
  event       TEXT NOT NULL · start/complete/fail
  meta        JSONB · 自由扩展字段(quote_id / diagnosis_id / article_id / reason 等)
  actor_user_id INTEGER · 操作代理(可空 · 系统触发时为 NULL)
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP

索引:
  (brand_id, stage_name, event, created_at DESC) · 支持 flow-funnel 漏斗查询
  (created_at DESC) · 支持全局时间线

写入策略:
  - 失败不 block 主流程(logger.warning · 不抛异常)
  - 异步安全(helper 内部建连接 · 不依赖调用方事务)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("GEO-PipelineStageLog")


# PRD §4.1 · 9 步工作流 stage_name 枚举
VALID_STAGES = {
    "profile",      # 1. 建档
    "diagnosis",    # 2. 诊断
    "keyword",      # 3. 关键词(可选 · selection)
    "quote",        # 4. 报价(生成 + 发送)
    "review",       # 5. 审核(横切)
    "pay",          # 4b. 付款(M1a 重点)
    "write",        # 6. 写文章
    "publish",      # 7. 投放
    "monitor",      # 8. 监测
    "report",       # 9. 复盘/报告
    "renew",        # M4 续费
}

VALID_EVENTS = {"start", "complete", "fail"}


def _get_connection():
    from db.connection import get_connection
    return get_connection()


def init_pipeline_stage_log_table() -> None:
    """启动时建表 + 索引 · 幂等"""
    conn = _get_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_stage_log (
                id SERIAL PRIMARY KEY,
                brand_id INTEGER NOT NULL,
                stage_name TEXT NOT NULL,
                event TEXT NOT NULL,
                meta JSONB,
                actor_user_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pipeline_stage_log_brand_stage
            ON pipeline_stage_log (brand_id, stage_name, event, created_at DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pipeline_stage_log_created
            ON pipeline_stage_log (created_at DESC)
            """
        )
        logger.info("[pipeline_stage_log] 表 + 索引初始化完成")
    except Exception as e:
        logger.warning(f"[pipeline_stage_log] 初始化失败(非阻塞): {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def log_stage_event(
    brand_id: int,
    stage_name: str,
    event: str = "complete",
    meta: Optional[dict[str, Any]] = None,
    actor_user_id: Optional[int] = None,
) -> Optional[int]:
    """写一条 pipeline_stage_log · 失败不抛异常(只记 warning)

    Args:
        brand_id: 必传 · 关联 brands.id
        stage_name: diagnosis/quote/pay/write/publish/monitor/report/renew 等
        event: start / complete / fail(默认 complete)
        meta: 可选 JSONB · {quote_id, diagnosis_id, article_id, amount, reason, ...}
        actor_user_id: 可选 · 操作代理 user_id

    Returns:
        log_id(int)或 None(失败)
    """
    if not brand_id or not stage_name:
        logger.warning(f"[pipeline_stage_log] 参数缺失 brand_id={brand_id} stage={stage_name}")
        return None
    # 允许未知 stage(向前兼容 · 只日志 warning · 不 reject)
    if stage_name not in VALID_STAGES:
        logger.warning(f"[pipeline_stage_log] 未知 stage_name={stage_name} · 仍写入但建议对齐枚举")
    if event not in VALID_EVENTS:
        logger.warning(f"[pipeline_stage_log] 未知 event={event} · 仍写入")

    meta_json = None
    if meta is not None:
        try:
            meta_json = json.dumps(meta, ensure_ascii=False, default=str)
        except Exception as e:
            logger.warning(f"[pipeline_stage_log] meta 序列化失败 · 丢弃 meta: {e}")

    conn = _get_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO pipeline_stage_log (brand_id, stage_name, event, meta, actor_user_id)
            VALUES (%s, %s, %s, %s::jsonb, %s) RETURNING id
            """,
            (brand_id, stage_name, event, meta_json, actor_user_id),
        )
        row = cur.fetchone()
        return row["id"] if row else None
    except Exception as e:
        logger.warning(f"[pipeline_stage_log] 写入失败(非阻塞) brand={brand_id} stage={stage_name}: {e}")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


# PRD M1a flow-funnel 9 步顺序(默认 stage_name 序)
FUNNEL_DEFAULT_ORDER = [
    ("diagnosis", "诊断"),
    ("quote", "真实报价"),
    ("pay", "付款"),
    ("write", "写文章"),
    ("publish", "发布"),
    ("monitor", "监测"),
    ("report", "复盘"),
    ("renew", "续费"),
]


def compute_flow_funnel(
    days: int = 90,
    actor_user_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """聚合最近 N 天 pipeline_stage_log · 按 stage 计算漏斗

    Args:
        days: 统计窗口(默认 90 天)
        actor_user_id: 可选 · 只看某代理的数据(None=全局)

    Returns:
        [
          {name, label, count, ratio(相对 base_count)},
          ...
        ]
        · base_count = diagnosis 阶段 count · 若 diagnosis=0 则 ratio 全 0
        · 只统计 event='complete' · 去重 (brand_id, stage_name)

    PRD §8 DoD "9 步漏斗 pipeline_stage_log 可出漏斗图"
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()

        where_clauses = [
            "event = 'complete'",
            "created_at >= NOW() - INTERVAL '%s days'" % int(days),
        ]
        params: list[Any] = []
        if actor_user_id is not None:
            where_clauses.append("actor_user_id = %s")
            params.append(actor_user_id)
        where_sql = " AND ".join(where_clauses)

        # 每 stage 去重 (brand_id · 同一品牌同一 stage 多次 complete 只算 1 次)
        cur.execute(
            f"""
            SELECT stage_name, COUNT(DISTINCT brand_id) AS cnt
            FROM pipeline_stage_log
            WHERE {where_sql}
            GROUP BY stage_name
            """,
            tuple(params),
        )
        rows = cur.fetchall() or []
        stage_count: dict[str, int] = {row["stage_name"]: row["cnt"] for row in rows}

        # ── [CTO-15.23 2026-05-29] publish/monitor 真实表口径覆盖(漏斗止血)──────────────
        # 根因:这两阶段埋点 2026-05-25 才补(仅 4 天) vs 90 天窗口 → 严重低估
        #   (prod 实证:publish 埋点 2 brand / 真实 7 · monitor 埋点 5 brand / 真实 20)。
        # 老板 Q2 拍板:publish/monitor 改读真实业务表;diagnosis/quote/pay/write/report 维持 pipeline_stage_log。
        # 口径仍是 COUNT(DISTINCT brand_id)·窗口/owner 过滤与埋点口径一致(global=全量 · agent=按 brands.owner_user_id)。
        # TODO(tech-debt): 长期所有 stage dual-source(埋点 + 真实表对账 + backfill);当前仅 publish/monitor 止血。
        _owner_where = "AND b.owner_user_id = %s" if actor_user_id is not None else ""
        _owner_params = (actor_user_id,) if actor_user_id is not None else ()
        try:
            _pub_join = "JOIN brands b ON b.id = q.brand_id" if actor_user_id is not None else ""
            cur.execute(
                f"""
                SELECT COUNT(DISTINCT q.brand_id) AS cnt
                FROM mhz_publish_order_items i
                JOIN mhz_publish_orders mo ON mo.id = i.order_id
                JOIN articles a ON a.id = mo.article_id
                JOIN quotes q ON q.id = a.quote_id
                {_pub_join}
                WHERE i.status = 'published'
                  AND COALESCE(i.published_at, i.created_at) >= NOW() - INTERVAL '{int(days)} days'
                  AND q.brand_id IS NOT NULL
                  {_owner_where}
                """,
                _owner_params,
            )
            _r = cur.fetchone()
            stage_count["publish"] = int((_r.get("cnt") if _r else 0) or 0)
        except Exception as _pe:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning(f"[flow-funnel] publish 真实表口径失败 · 回退埋点口径: {_pe}")
        try:
            _mon_join = "JOIN brands b ON b.id = mt.brand_id" if actor_user_id is not None else ""
            cur.execute(
                f"""
                SELECT COUNT(DISTINCT mt.brand_id) AS cnt
                FROM monitoring_tasks mt
                {_mon_join}
                WHERE mt.created_at >= NOW() - INTERVAL '{int(days)} days'
                  AND mt.brand_id IS NOT NULL
                  {_owner_where}
                """,
                _owner_params,
            )
            _r = cur.fetchone()
            stage_count["monitor"] = int((_r.get("cnt") if _r else 0) or 0)
        except Exception as _me:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning(f"[flow-funnel] monitor 真实表口径失败 · 回退埋点口径: {_me}")
        # ──────────────────────────────────────────────────────────────────────────────

        base_count = stage_count.get("diagnosis", 0)
        stages = []
        for name, label in FUNNEL_DEFAULT_ORDER:
            cnt = stage_count.get(name, 0)
            ratio = round(cnt / base_count, 4) if base_count else 0.0
            stages.append({"name": name, "label": label, "count": cnt, "ratio": ratio})
        return stages
    except Exception as e:
        logger.warning(f"[pipeline_stage_log] flow-funnel 查询失败: {e}")
        return [{"name": n, "label": l, "count": 0, "ratio": 0.0} for n, l in FUNNEL_DEFAULT_ORDER]
    finally:
        try:
            conn.close()
        except Exception:
            pass
