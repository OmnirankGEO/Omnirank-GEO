"""[GEO-R9-CAN-007 · v5 Deploy-CTO] answer_hash 身份键迁移 · 带 run 生命周期的 precheck/forward/rollback。

身份键 旧 MD5(answer_text) → 新 MD5(query ⊕ industry ⊕ answer_text)  (⊕ = E'\\x1f')。

用法(Deploy 在 quiescence 后【手动】执行 · 配套运维步骤见 migration_answer_hash_recompute_2026_07_12.sql STEP-Q):
    DATABASE_URL=... python -m scripts.migrate_answer_hash_2026_07_12 precheck   # 碰撞/孤儿/非预期 phase → 非零退出
    DATABASE_URL=... python -m scripts.migrate_answer_hash_2026_07_12 forward
    DATABASE_URL=... python -m scripts.migrate_answer_hash_2026_07_12 rollback

[v5 req3 生命周期重做 · 取代 v4 单轮全局表模型]:
- **每轮独立 run_id(BIGSERIAL)+ phase**:runs 表登记每轮迁移 · phase ∈ ('forward','rolled_back')。
  - forward 时若已有 active(phase='forward')run → 判为【同轮重跑】· 复用其 run_id + 快照(幂等 · 不新拍快照);
  - 若无 active run(全新 / 上轮已 rolled_back)→ 起【新 run】· 拍【新轮快照】+ 新备份。
    → 满足"rollback 后再次 forward 必须创建新轮次快照"。
- **snapshot / backup 按 run_id 分轮**(单表 + run_id 列 · 不动态建表):
  - snapshot(run_id, fact_id):forward 时【全量 fact-id 快照】· rollback R3 据此确定性识别"迁移后新增行"。
  - hash_backup(run_id, fact_id, old_answer_hash):被 UPDATE 行的旧哈希备份 · rollback R1 还原。
- **完整备份 + rollback 不得无备份 DELETE**:
  - rollback R3 删【迁移后新代码新增行】(fact id 不在本轮快照)前,先把整行(facts + 其 entities)
    以 to_jsonb 完整备份进 deleted 表,再 DELETE。→ 任何客户 facts/entities 删除都可审计/可重建。
- **precheck 非零退出**:碰撞(待迁 vs 已存在新哈希行)/ 孤儿(raw 缺失)/ 非预期 phase(存在未结束 forward run)
  任一命中 → ok=False → _main 返回码 1(非零)。
- 往返正确性(连续两轮 forward→rollback 数据零丢失)见 tests/regression/test_nogo_v5_migration_lifecycle.py。
"""
from __future__ import annotations

import os
import sys

_NEW = ("MD5(COALESCE(r.query,'') || E'\\x1f' || COALESCE(r.industry,'') || E'\\x1f' || COALESCE(r.answer_text,''))")

RUNS = "answer_hash_migration_runs"
SNAP = "answer_hash_migration_snapshot"
BAK = "answer_hash_migration_hash_backup"
DEL = "answer_hash_migration_deleted"


def _one(row):
    """取单列结果值 · 兼容 RealDictCursor(dict)与普通 cursor(tuple)。"""
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


# [v6 req4] 全局 advisory lock key(forward/rollback/finalize 串行 · 防双迁移并发)
_ADVISORY_KEY = 728120726   # 固定常量(任意)· pg_advisory_xact_lock 事务级 · commit/rollback 自动释放


def _advisory_lock(cur) -> None:
    """[v7 finding7 修] 先设有效超时(取锁 / DDL 之前)· 再用【try 版】advisory lock 快速失败。

    旧实现:阻塞版 pg_advisory_xact_lock(锁竞争时无限等)· 且 lock_timeout 在【取锁后】才设(对已阻塞的取锁无效,
      且 lock_timeout 本就不作用于 advisory 函数)→ "锁竞争不快速失败"(finding7)。
    现:SET LOCAL 超时在最前(护住后续 CREATE TABLE / UPDATE 的表锁等待)+ pg_try_advisory_xact_lock 拿不到立即 raise。
    并发迁移的"至多一个 active forward run"另有 uniq_active_forward_run 唯一索引兜底(双保险)。
    """
    cur.execute("SET LOCAL lock_timeout = '5s'")
    cur.execute("SET LOCAL statement_timeout = '120s'")
    cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS got", (_ADVISORY_KEY,))
    row = cur.fetchone()
    got = (row["got"] if isinstance(row, dict) else row[0]) if row else False
    if not got:
        raise RuntimeError(
            f"[migrate_answer_hash] 另一迁移进程持有 advisory lock(key={_ADVISORY_KEY})· 快速失败中止 · 请待其完成后重试"
        )


def _ensure_tables(cur) -> None:
    """幂等建 run 生命周期辅助表。"""
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {RUNS} (
        run_id     BIGSERIAL PRIMARY KEY,
        phase      TEXT NOT NULL DEFAULT 'forward',
        note       TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CHECK (phase IN ('forward','accepted','rolled_back')))""")
    # [v6 req4] 旧表(v5 只有 forward/rolled_back)幂等放宽 CHECK 到含 'accepted'
    cur.execute(f"ALTER TABLE {RUNS} DROP CONSTRAINT IF EXISTS {RUNS}_phase_check")
    cur.execute(f"ALTER TABLE {RUNS} ADD CONSTRAINT {RUNS}_phase_check "
                f"CHECK (phase IN ('forward','accepted','rolled_back'))")
    # [v6 req4] 唯一活动轮约束:任一时刻至多【一个】phase='forward' 的 run(部分唯一索引 · 常量列)
    cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_forward_run "
                f"ON {RUNS} ((1)) WHERE phase='forward'")
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {SNAP} (
        run_id  BIGINT NOT NULL,
        fact_id BIGINT NOT NULL,
        PRIMARY KEY (run_id, fact_id))""")
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {BAK} (
        run_id          BIGINT NOT NULL,
        fact_id         BIGINT NOT NULL,
        old_answer_hash CHAR(32) NOT NULL,
        backed_up_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (run_id, fact_id))""")
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {DEL} (
        id         BIGSERIAL PRIMARY KEY,
        run_id     BIGINT NOT NULL,
        kind       TEXT NOT NULL,          -- 'fact' | 'entity'
        row_id     BIGINT NOT NULL,
        row_json   JSONB NOT NULL,         -- to_jsonb 整行完整备份(可审计/可重建)
        deleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")


def _active_run_id(cur):
    """返回当前唯一 active(phase='forward')run 的 run_id · 无则 None。"""
    cur.execute(f"SELECT run_id FROM {RUNS} WHERE phase='forward' ORDER BY run_id DESC LIMIT 1")
    row = cur.fetchone()
    return _one(row) if row else None


def precheck(cur) -> dict:
    """只读:影响面 + 两类冲突 + 非预期 phase。ok=False 时 _main 非零退出。"""
    _advisory_lock(cur); _ensure_tables(cur)   # [v6 对抗审 P3] 与 forward/rollback 一致:先锁后 DDL(防并发建表撞)
    cur.execute(f"""
        SELECT
          (SELECT COUNT(*) FROM geo_research_answer_facts) AS total_facts,
          (SELECT COUNT(*) FROM geo_research_answer_facts f JOIN geo_research_raw r ON f.raw_id=r.id
             WHERE f.answer_hash = MD5(COALESCE(r.answer_text,''))) AS old_hash_migratable,
          (SELECT COUNT(*) FROM geo_research_answer_facts f LEFT JOIN geo_research_raw r ON f.raw_id=r.id
             WHERE r.id IS NULL) AS orphan_raw_missing
    """)
    row = dict(cur.fetchone())
    # 待迁 vs 已存在新哈希行 撞(v2 漏检的这一类)
    cur.execute(f"""
        WITH mig AS (
          SELECT f.id, f.engine, f.batch_id, {_NEW} AS new_hash
            FROM geo_research_answer_facts f JOIN geo_research_raw r ON f.raw_id=r.id
           WHERE f.answer_hash = MD5(COALESCE(r.answer_text,''))
        )
        SELECT COUNT(*) AS n FROM mig
         WHERE EXISTS (SELECT 1 FROM geo_research_answer_facts e
                        WHERE e.engine=mig.engine AND e.batch_id=mig.batch_id
                          AND e.answer_hash=mig.new_hash AND e.id<>mig.id)
    """)
    row["migratable_colliding_existing"] = cur.fetchone()["n"]
    # 非预期 phase:存在未结束的 forward run(上轮迁移未 rollback / 未确认完成)
    active = _active_run_id(cur)
    row["active_forward_run_id"] = active

    problems = []
    if row["migratable_colliding_existing"] > 0:
        problems.append(f"待迁行与已存在新哈希行撞 {row['migratable_colliding_existing']} 条(UNIQUE 冲突风险)")
    if row["orphan_raw_missing"] > 0:
        problems.append(f"raw 缺失孤儿 fact {row['orphan_raw_missing']} 条")
    if active is not None:
        problems.append(f"存在未结束的 forward run(run_id={active})· 需先 rollback 或确认上轮已完成")
    row["ok"] = len(problems) == 0
    row["problems"] = problems
    return row


def forward(cur, note: str | None = None) -> dict:
    """起/续一轮 forward · 拍快照 + 备份旧哈希 + collision-safe UPDATE。

    返回 {run_id, phase, reused, snapshot, backed_up, updated}。
    - reused=True:命中 active run(同轮重跑)· 复用快照(幂等 · 不新拍)· UPDATE 通常迁 0 行。
    - reused=False:新 run · 新轮快照。
    """
    # [v6 req4 + v7 finding7] advisory lock 必须在 _ensure_tables(DDL)之【前】—— 否则并发 forward 都跑 CREATE TABLE 会撞
    #   pg_type 目录唯一键(CREATE TABLE IF NOT EXISTS 非并发安全)。锁不依赖任何表,可最先取。
    #   超时(lock_timeout/statement_timeout)已在 _advisory_lock 内【取锁前】设好(finding7 · 不再取锁后补设)。
    _advisory_lock(cur)
    _ensure_tables(cur)

    active = _active_run_id(cur)
    if active is not None:
        run_id = active
        reused = True
    else:
        cur.execute(f"INSERT INTO {RUNS} (phase, note) VALUES ('forward', %s) RETURNING run_id", (note,))
        run_id = _one(cur.fetchone())
        reused = False
        # 新轮:拍【全量 fact-id 快照】(rollback R3 据此识别迁移后新增行)
        cur.execute(f"INSERT INTO {SNAP} (run_id, fact_id) SELECT %s, id FROM geo_research_answer_facts", (run_id,))

    cur.execute(f"SELECT COUNT(*) AS n FROM {SNAP} WHERE run_id=%s", (run_id,))
    snap_n = cur.fetchone()["n"]

    # 备份待迁行旧哈希(幂等 · ON CONFLICT DO NOTHING)
    cur.execute(f"""INSERT INTO {BAK} (run_id, fact_id, old_answer_hash)
        SELECT %s, f.id, f.answer_hash
          FROM geo_research_answer_facts f JOIN geo_research_raw r ON f.raw_id=r.id
         WHERE f.answer_hash = MD5(COALESCE(r.answer_text,''))
        ON CONFLICT (run_id, fact_id) DO NOTHING""", (run_id,))
    cur.execute(f"SELECT COUNT(*) AS n FROM {BAK} WHERE run_id=%s", (run_id,))
    bak_n = cur.fetchone()["n"]

    # collision-safe UPDATE(目标新哈希撞已存在行则跳过 · 不抛 UniqueViolation)
    cur.execute(f"""
        WITH upd AS (
          UPDATE geo_research_answer_facts f
             SET answer_hash = {_NEW}, updated_at = NOW()
            FROM geo_research_raw r
           WHERE f.raw_id = r.id
             AND f.answer_hash = MD5(COALESCE(r.answer_text,''))
             AND NOT EXISTS (SELECT 1 FROM geo_research_answer_facts e
                              WHERE e.engine=f.engine AND e.batch_id=f.batch_id
                                AND e.answer_hash = {_NEW} AND e.id<>f.id)
          RETURNING f.id)
        SELECT COUNT(*) AS n FROM upd""")
    updated = cur.fetchone()["n"]
    cur.execute(f"UPDATE {RUNS} SET updated_at=NOW() WHERE run_id=%s", (run_id,))
    return {"run_id": run_id, "phase": "forward", "reused": reused,
            "snapshot": snap_n, "backed_up": bak_n, "updated": updated}


def rollback(cur) -> dict:
    """回滚当前 active run:R1 还原旧哈希 · R3 备份后删迁移后新增行 · 标 run phase='rolled_back'。

    返回 {run_id, restored, purged_facts, purged_entities, backed_up_deleted}。
    无 active run → {run_id:None, ...:0}(no-op · 不误删)。
    """
    _advisory_lock(cur); _ensure_tables(cur)   # [v6 req4] 先锁后 DDL(与 forward 互斥 · 防 CREATE TABLE 并发撞)
    run_id = _active_run_id(cur)
    if run_id is None:
        return {"run_id": None, "restored": 0, "purged_facts": 0,
                "purged_entities": 0, "backed_up_deleted": 0}

    # R1:被 UPDATE 行还原旧哈希(旧 only_pending 口径恢复)
    cur.execute(f"""UPDATE geo_research_answer_facts f
        SET answer_hash = b.old_answer_hash, updated_at = NOW()
        FROM {BAK} b WHERE b.run_id=%s AND f.id=b.fact_id AND f.answer_hash <> b.old_answer_hash""",
        (run_id,))
    restored = cur.rowcount

    # R3:迁移后新代码新增行 = fact id 不在本轮快照 → 【先完整备份(facts + entities)再删】
    #    (回旧代码后这些新哈希行在旧 only_pending 口径下是孤儿)。
    # R3a 备份将删的 entities(整行 to_jsonb)
    cur.execute(f"""INSERT INTO {DEL} (run_id, kind, row_id, row_json)
        SELECT %s, 'entity', e.id, to_jsonb(e.*)
          FROM geo_research_answer_entities e
         WHERE e.answer_fact_id IN (
            SELECT f.id FROM geo_research_answer_facts f
             WHERE f.id NOT IN (SELECT fact_id FROM {SNAP} WHERE run_id=%s))""",
        (run_id, run_id))
    # R3b 备份将删的 facts(整行 to_jsonb)
    cur.execute(f"""INSERT INTO {DEL} (run_id, kind, row_id, row_json)
        SELECT %s, 'fact', f.id, to_jsonb(f.*)
          FROM geo_research_answer_facts f
         WHERE f.id NOT IN (SELECT fact_id FROM {SNAP} WHERE run_id=%s)""",
        (run_id, run_id))
    cur.execute(f"SELECT COUNT(*) AS n FROM {DEL} WHERE run_id=%s", (run_id,))
    backed_up_deleted = cur.fetchone()["n"]

    # R3c 删 entities(已备份)
    cur.execute(f"""DELETE FROM geo_research_answer_entities e
        WHERE e.answer_fact_id IN (
            SELECT f.id FROM geo_research_answer_facts f
             WHERE f.id NOT IN (SELECT fact_id FROM {SNAP} WHERE run_id=%s))""", (run_id,))
    purged_entities = cur.rowcount
    # R3d 删 facts(已备份)
    cur.execute(f"""DELETE FROM geo_research_answer_facts f
        WHERE f.id NOT IN (SELECT fact_id FROM {SNAP} WHERE run_id=%s)""", (run_id,))
    purged_facts = cur.rowcount

    cur.execute(f"UPDATE {RUNS} SET phase='rolled_back', updated_at=NOW() WHERE run_id=%s", (run_id,))
    return {"run_id": run_id, "restored": restored, "purged_facts": purged_facts,
            "purged_entities": purged_entities, "backed_up_deleted": backed_up_deleted}


def finalize(cur) -> dict:
    """[v6 req4] 校验通过后 finalize:active forward run → phase='accepted'(迁移确认成功 · 关闭本轮 · 释放唯一活动轮位)。

    accepted 后不再被 rollback 的 _active_run_id 命中(rollback 只作用于 forward);新 forward 可起下一轮。
    """
    _advisory_lock(cur); _ensure_tables(cur)   # [v6 req4] 先锁后 DDL
    run_id = _active_run_id(cur)
    if run_id is None:
        return {"run_id": None, "accepted": False, "reason": "无 active forward run"}
    cur.execute(f"UPDATE {RUNS} SET phase='accepted', updated_at=NOW() WHERE run_id=%s", (run_id,))
    return {"run_id": run_id, "accepted": True}


def restore(cur, run_id: int) -> dict:
    """[v6 req4] 经测试的 restore:把某轮 rollback 时【备份后删除】的 facts/entities 从 DEL 表整行重插回来。

    用于"rollback 删错了/需撤销删除"—— 保证"备份后删除"可逆(不是只删不回)。
    先重插 facts(id 冲突则跳过 · 已存在不重复),再重插 entities。返回 {restored_facts, restored_entities}。
    """
    _advisory_lock(cur); _ensure_tables(cur)   # [v6 req4] 先锁后 DDL
    # 重插 facts(整行 to_jsonb → jsonb_populate_record → INSERT · ON CONFLICT (id) DO NOTHING)
    cur.execute(f"""
        INSERT INTO geo_research_answer_facts
        SELECT (jsonb_populate_record(NULL::geo_research_answer_facts, d.row_json)).*
          FROM {DEL} d WHERE d.run_id=%s AND d.kind='fact'
        ON CONFLICT (id) DO NOTHING""", (run_id,))
    rf = cur.rowcount
    cur.execute(f"""
        INSERT INTO geo_research_answer_entities
        SELECT (jsonb_populate_record(NULL::geo_research_answer_entities, d.row_json)).*
          FROM {DEL} d WHERE d.run_id=%s AND d.kind='entity'
        ON CONFLICT (id) DO NOTHING""", (run_id,))
    re_ = cur.rowcount
    return {"run_id": run_id, "restored_facts": rf, "restored_entities": re_}


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
            r = precheck(cur); conn.rollback()
            print(r)
            return 0 if r.get("ok") else 1   # 碰撞/孤儿/非预期 phase → 非零退出
        elif action == "forward":
            r = forward(cur); conn.commit(); print(r); return 0
        elif action == "rollback":
            r = rollback(cur); conn.commit(); print(r); return 0
        elif action == "finalize":
            r = finalize(cur); conn.commit(); print(r); return 0 if r.get("accepted") else 1
        elif action == "restore":
            if len(sys.argv) < 3:
                print("restore 需 run_id: python -m scripts.migrate_answer_hash_2026_07_12 restore <run_id>", file=sys.stderr)
                return 2
            r = restore(cur, int(sys.argv[2])); conn.commit(); print(r); return 0
        else:
            print(f"未知 action: {action}(precheck|forward|finalize|rollback|restore <run_id>)", file=sys.stderr); return 2
    except Exception as e:
        conn.rollback(); print(f"失败(已回滚事务): {e}", file=sys.stderr); return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1] if len(sys.argv) > 1 else ""))
