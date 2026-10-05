"""[v6 req5] GEO 结算态 schema(v5/v6)【独立可执行 rollback】。

安全序:先 reconcile 排空补偿队列(真 commit/release 幂等 · 校验 freeze 真状态)→ 再校验无残留 pending
       (settlement_pending / refund_pending / settle_conflict / settling)→ 才 shrink schema 回 6 态。
铁律:pending 非零 → 【fail-closed 中止】· 绝不用直接 UPDATE 伪造 done/failed(那会凭空坐实资金终态)。

用法(Deploy 在 quiescence 后手动):
    DATABASE_URL=... python -m scripts.rollback_geo_plan_settlement_v5v6 precheck    # 只读 · 报残留 pending
    DATABASE_URL=... python -m scripts.rollback_geo_plan_settlement_v5v6 reconcile   # 多轮排空补偿队列(真动 freeze)
    DATABASE_URL=... python -m scripts.rollback_geo_plan_settlement_v5v6 rollback    # 校验安全后 shrink;pending≠0 则非零退出中止

restore(前滚回 v5/v6 schema)= 直接重跑 scripts/migration_v5_geo_plan_settlement_2026_07_13.sql(幂等)。
往返正确性见 tests/regression/test_nogo_v6_schema_rollback.py(经测试 · 非仅备份后删)。
"""
from __future__ import annotations

import os
import sys

# rollback 必须 fail-closed 的态 · 分两类(计数口径不同 · 见 v7 finding5):
#  A. CHECK-illegal-after-shrink 态(v5/v6 新态):shrink 后这些值直接违反新 CHECK →
#     ADD CONSTRAINT / ALTER TYPE 时【全表】(含 archived)校验 · 只要有一行(哪怕归档)就 CheckViolation。
#     故必须【全表计数 · 不排除 archived】。
#  B. 在途旧态(queued/running):CHECK-legal 但其【合法后继】(running→settling / queued|running→refund_pending)
#     在 shrink 后运行时撞 CHECK → 崩溃 + freeze 卡死。归档任务不再转移(惰性)· 故仅【非归档】需 quiesce。
# [v7 finding5 修] 旧实现对【全部】blocking 态都加 archived_at IS NULL → 归档的 settle_conflict/settling 等被漏算,
#   precheck 报 safe=True,随后收窄 CHECK 却因归档行 CheckViolation 崩(rollback 中途炸)。现按 A/B 分口径。
_SHRINK_ILLEGAL_STATES = ("settling", "settlement_pending", "refund_pending", "settle_conflict")
_INFLIGHT_STATES = ("queued", "running")


def precheck(cur) -> dict:
    by = {}
    # A. CHECK-illegal 态:全表计数(含 archived · 归档行同样会让 shrink DDL CheckViolation)
    cur.execute(
        "SELECT status, COUNT(*) AS n FROM geo_plan_tasks WHERE status = ANY(%s) GROUP BY status",
        (list(_SHRINK_ILLEGAL_STATES),),
    )
    for r in cur.fetchall():
        st = r["status"] if isinstance(r, dict) else r[0]
        n = r["n"] if isinstance(r, dict) else r[1]
        by[st] = by.get(st, 0) + int(n)
    # B. 在途态:仅非归档(归档 queued/running 惰性 · 不会转移触发运行时 CHECK)
    cur.execute(
        "SELECT status, COUNT(*) AS n FROM geo_plan_tasks WHERE status = ANY(%s) "
        "AND archived_at IS NULL GROUP BY status",
        (list(_INFLIGHT_STATES),),
    )
    for r in cur.fetchall():
        st = r["status"] if isinstance(r, dict) else r[0]
        n = r["n"] if isinstance(r, dict) else r[1]
        by[st] = by.get(st, 0) + int(n)
    pending = sum(by.values())
    return {"blocking_by_state": by, "pending": pending, "safe": pending == 0,
            "shrink_illegal_states": list(_SHRINK_ILLEGAL_STATES), "inflight_states": list(_INFLIGHT_STATES)}


async def reconcile() -> dict:
    """多轮 reconcile 排空补偿队列(真 commit/release · 幂等)· 直到无 scanned。"""
    from services.geo_plan_settlement import reconcile_pending
    total = {"settled_done": 0, "refunded_terminal": 0, "still_pending": 0, "conflict": 0, "rounds": 0}
    for _ in range(30):
        s = await reconcile_pending(limit=500)
        total["rounds"] += 1
        for k in ("settled_done", "refunded_terminal", "still_pending", "conflict"):
            total[k] += s.get(k, 0)
        if not s.get("scanned"):
            break
    return total


def rollback(cur) -> dict:
    """仅在无残留 pending 时 shrink schema 回 6 态;否则 raise(fail-closed · 不伪造终态)。"""
    # [v7 对抗审 P3] 取 ACCESS EXCLUSIVE 表锁,让 precheck 快照与随后的 ADD CONSTRAINT/ALTER 对写者原子:
    #   否则 precheck(safe=True)与 shrink DDL 之间若有 worker 提交新的 settling/*_pending/settle_conflict 行,
    #   ADD CONSTRAINT 全表校验会 CheckViolation(虽被 _main 回滚 · 无损但白跑)。加锁把"quiescence 后执行"由文档变成代码强约束。
    cur.execute("LOCK TABLE geo_plan_tasks IN ACCESS EXCLUSIVE MODE")
    pc = precheck(cur)
    if not pc["safe"]:
        raise RuntimeError(
            f"[rollback fail-closed] 仍有 {pc['pending']} 个未收口任务 {pc['blocking_by_state']} · "
            f"拒绝回滚(绝不直接 UPDATE 伪造 done/failed)· 请先 reconcile 排空 / quiescence 后重试"
        )
    # 安全:收窄 CHECK 回 6 态 + 列宽回 VARCHAR(16) + 删补偿队列索引(保留 pending_terminal 等列 · 无害)
    cur.execute("ALTER TABLE geo_plan_tasks DROP CONSTRAINT IF EXISTS geo_plan_tasks_status_check")
    cur.execute("ALTER TABLE geo_plan_tasks ADD CONSTRAINT geo_plan_tasks_status_check "
                "CHECK (status IN ('queued','running','done','failed','cancelled','timeout'))")
    cur.execute("ALTER TABLE geo_plan_tasks ALTER COLUMN status TYPE VARCHAR(16)")
    cur.execute("DROP INDEX IF EXISTS idx_geoplan_settle_pending")
    cur.execute("DROP INDEX IF EXISTS idx_geoplan_brand_status")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_geoplan_brand_status "
                "ON geo_plan_tasks(brand_id, status) WHERE status IN ('queued','running')")
    return {"rolled_back": True, "precheck": pc}


def _main(action: str) -> int:
    import psycopg2
    import psycopg2.extras
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL 未设置", file=sys.stderr)
        return 2
    conn = psycopg2.connect(url)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    cur = conn.cursor()
    try:
        if action == "precheck":
            r = precheck(cur); conn.rollback(); print(r)
            return 0 if r["safe"] else 1       # 有残留 pending → 非零退出
        if action == "reconcile":
            import asyncio
            r = asyncio.run(reconcile()); print(r); return 0
        if action == "rollback":
            r = rollback(cur); conn.commit(); print(r); return 0
        print(f"未知 action: {action}(precheck|reconcile|rollback)", file=sys.stderr)
        return 2
    except Exception as e:
        conn.rollback()
        print(f"失败(已回滚事务 · fail-closed): {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1] if len(sys.argv) > 1 else ""))
