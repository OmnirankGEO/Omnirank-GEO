-- ============================================================================
-- [GEO-R9-CAN-007 · Deploy-CTO NO-GO finding 5/8 v3] answer_hash 身份键数据迁移
--
-- 身份键 旧 MD5(answer_text) → 新 MD5(query ⊕ industry ⊕ answer_text)  (⊕ = E'\x1f')
-- 部署新代码后 only_pending 用【新哈希】算 NOT EXISTS → 存量 facts(旧哈希)恒判 pending
-- → 全量重抽 + 旧键孤儿。本迁移把存量 facts 就地重算为新哈希(recompute-in-place · 无损)。
--
-- 🔴🔴 v3 修正(Deploy-CTO 复现:v2 预检 0 冲突但 UPDATE 抛 UniqueViolation):
--   根因 = v2 预检只查【待迁行彼此】冲突,漏查【待迁行 vs 表中已存在新哈希行】。
--   部署后~迁移前这段窗口里,新代码可能已对同一答案插入【新哈希】的 canonical 行;把旧行 UPDATE
--   成同一新哈希 → 撞 UNIQUE(engine,batch_id,answer_hash)。
-- v3 三处收口:
--   ① 预检 0b 同时检查【待迁 vs 待迁】+【待迁 vs 已存在行】两类冲突;
--   ② UPDATE 带 NOT EXISTS 守卫(只迁"目标新哈希不撞已存在行"的),撞行【跳过不迁】不报错;
--   ③ 撞行(旧哈希 duplicate) → STEP3 单列出,交人工决定(默认 soft-skip · 不自动删,避免误删);
--      回滚覆盖 UPDATE 行 + (若人工执行了 duplicate 清理)被删 facts/entities 的重插。
--
-- ⚠️ 执行纪律(不得"切流后随时手动跑"):必须走 Deploy 控制的 quiescence(STEP-Q)→ 预检 → 备份 →
--    迁移(带 lock/timeout)→ 校验 → 切流。异常走 ROLLBACK。只交脚本与流程,不在此执行生产迁移。
-- ============================================================================


-- ============================================================================
-- STEP-Q · QUIESCENCE(Deploy 控制 · 迁移前必须先静默写入 · 非 SQL · 运维动作)
-- ----------------------------------------------------------------------------
-- Q1. 关闭/暂停会写 geo_research_answer_facts / _entities 的来源:
--     - answer-entity 抽取调度(scheduler 的 answer_entity job)· selfserve 调研入库桥接;
--     - 关 flag / 停对应 scheduler job(见 api/scheduler.py 注册的 answer_entity 任务)。
-- Q2. 等在途抽取任务归零(确认没有正在写 facts/entities 的进程):
--       SELECT COUNT(*) FROM geo_plan_tasks WHERE status='running';   -- 期望 0(或与 answer-entity 无关)
--     并观测两表 max(updated_at) 在 T 秒内不再前进(静默确认)。
-- Q3. 备份两表(与全库常规备份并行):
--       pg_dump ... -t geo_research_answer_facts -t geo_research_answer_entities > backup_answer_YYYYMMDD.sql
-- Q4. 完成 Q1-Q3 后再进入 STEP0。迁移期间【禁止】翻开 answer-entity 写入 flag。
-- ============================================================================


-- ============================================================================
-- STEP 0 · DRY-RUN(只读 · 绝不改数据)
-- ============================================================================

-- 0a. 影响面:总 facts / 待迁旧哈希 / 已新哈希 / raw 缺失孤儿
SELECT
  (SELECT COUNT(*) FROM geo_research_answer_facts) AS total_facts,
  (SELECT COUNT(*) FROM geo_research_answer_facts f JOIN geo_research_raw r ON f.raw_id = r.id
     WHERE f.answer_hash = MD5(COALESCE(r.answer_text,''))) AS old_hash_migratable,
  (SELECT COUNT(*) FROM geo_research_answer_facts f JOIN geo_research_raw r ON f.raw_id = r.id
     WHERE f.answer_hash = MD5(COALESCE(r.query,'')||E'\x1f'||COALESCE(r.industry,'')||E'\x1f'||COALESCE(r.answer_text,''))) AS already_new_hash,
  (SELECT COUNT(*) FROM geo_research_answer_facts f LEFT JOIN geo_research_raw r ON f.raw_id = r.id
     WHERE r.id IS NULL) AS orphan_raw_missing;

-- 0b-i. 【待迁 vs 待迁】自撞:同 engine/batch 下多个待迁行重算到同一新哈希(期望 0 行)
WITH mig AS (
  SELECT f.id, f.engine, f.batch_id,
         MD5(COALESCE(r.query,'')||E'\x1f'||COALESCE(r.industry,'')||E'\x1f'||COALESCE(r.answer_text,'')) AS new_hash
    FROM geo_research_answer_facts f JOIN geo_research_raw r ON f.raw_id = r.id
   WHERE f.answer_hash = MD5(COALESCE(r.answer_text,''))
)
SELECT engine, batch_id, new_hash, COUNT(*) AS collide_n
  FROM mig GROUP BY engine, batch_id, new_hash HAVING COUNT(*) > 1;

-- 0b-ii. 🔴【待迁 vs 已存在行】撞(v2 漏检的这一类 · 期望 0 行;>0 则这些行 STEP2 会被跳过,STEP3 列出)
WITH mig AS (
  SELECT f.id, f.engine, f.batch_id,
         MD5(COALESCE(r.query,'')||E'\x1f'||COALESCE(r.industry,'')||E'\x1f'||COALESCE(r.answer_text,'')) AS new_hash
    FROM geo_research_answer_facts f JOIN geo_research_raw r ON f.raw_id = r.id
   WHERE f.answer_hash = MD5(COALESCE(r.answer_text,''))
)
SELECT COUNT(*) AS migratable_colliding_existing
  FROM mig
 WHERE EXISTS (
   SELECT 1 FROM geo_research_answer_facts e
    WHERE e.engine = mig.engine AND e.batch_id = mig.batch_id
      AND e.answer_hash = mig.new_hash AND e.id <> mig.id
 );


-- ============================================================================
-- STEP 0c · [v5] precheck 门(碰撞/孤儿/非预期 phase 任一 → 非零退出 · 阻断 forward)
--     DATABASE_URL=... python -m scripts.migrate_answer_hash_2026_07_12 precheck ; echo "exit=$?"
--   exit=0 才可进 forward;exit=1 表示存在冲突/孤儿/未结束的上轮 forward run(需先 rollback)。
-- ============================================================================


-- ============================================================================
-- STEP 1 + STEP 2 · 备份 + 每轮快照 + 迁移 · 🔴【统一由可执行 Python 模块执行,不要在本 .sql 手工跑】
--     DATABASE_URL=... python -m scripts.migrate_answer_hash_2026_07_12 forward
--   [v5 生命周期] forward 内含:
--     - 起/续【本轮 run_id】(answer_hash_migration_runs · phase='forward');
--     - 本轮【全量 fact-id 快照】写入 answer_hash_migration_snapshot(run_id,fact_id);
--     - 待迁旧哈希备份写入 answer_hash_migration_hash_backup(run_id,fact_id,old_answer_hash);
--     - SET LOCAL lock/statement timeout + collision-safe UPDATE(NOT EXISTS 守卫)。
--   ⚠️ 不要手工复制 UPDATE 单独跑:那样【不建 run/快照/备份】,rollback 的 R1/R3 会被架空(footgun)。
--      同轮重跑 forward 复用 run + 快照(幂等);rollback 后再 forward 起【新 run + 新快照】。
-- ============================================================================


-- ============================================================================
-- STEP 3 · 迁移后校验 + 撞行清单(只读)
-- ============================================================================
-- 3a. 仍是旧哈希且 raw 存在的行(= 被 STEP2 跳过的"撞已存在行"的 duplicate;应等于 0b-ii 的数)
SELECT COUNT(*) AS remaining_old_hash_with_raw
  FROM geo_research_answer_facts f JOIN geo_research_raw r ON f.raw_id = r.id
 WHERE f.answer_hash = MD5(COALESCE(r.answer_text,''));

-- 3b. 撞行明细(交人工决定 · 默认 soft-skip 不动 · 如确认新代码 canonical 行已含实体,可人工清理):
SELECT f.id AS old_fact_id, f.engine, f.batch_id,
       (SELECT e.id FROM geo_research_answer_facts e
         WHERE e.engine=f.engine AND e.batch_id=f.batch_id
           AND e.answer_hash = MD5(COALESCE(r.query,'')||E'\x1f'||COALESCE(r.industry,'')||E'\x1f'||COALESCE(r.answer_text,''))
           AND e.id<>f.id LIMIT 1) AS canonical_new_fact_id
  FROM geo_research_answer_facts f JOIN geo_research_raw r ON f.raw_id = r.id
 WHERE f.answer_hash = MD5(COALESCE(r.answer_text,''));

-- 3c. 撞行 duplicate 处置:【一律保留,交人工核对,不在本批自动删】。
--     [v4 对抗审 P2 修] 原 v4 初版提供"备份+删 duplicate"的 STEP3c 被撤销 —— forward 从不删 fact,
--     故 rollback 无需(也无法从仅存哈希的 BAK)重建整行;强删会破坏 forward↔rollback 往返无损。
--     撞行是"旧哈希 duplicate 撞到新代码 canonical 行"的少量残留,保留无害(不匹配新抽取,幂等重抽即可)。


-- ============================================================================
-- STEP 4 · 切流(Deploy 控制):校验通过后再翻开 answer-entity 写入 flag / 恢复 scheduler job。
-- ============================================================================


-- ============================================================================
-- FORWARD / ROLLBACK · 【可执行 · v5 生命周期 · 非占位】由 Python 迁移模块执行:
--     scripts/migrate_answer_hash_2026_07_12.py   (precheck | forward | rollback)
--   DATABASE_URL=... python -m scripts.migrate_answer_hash_2026_07_12 forward
--   DATABASE_URL=... python -m scripts.migrate_answer_hash_2026_07_12 rollback
-- [v5 生命周期辅助表]:
--   answer_hash_migration_runs(run_id BIGSERIAL, phase 'forward'|'rolled_back')  — 每轮独立 run_id + phase
--   answer_hash_migration_snapshot(run_id, fact_id)                              — 本轮全量 fact-id 快照
--   answer_hash_migration_hash_backup(run_id, fact_id, old_answer_hash)          — 被迁行旧哈希备份
--   answer_hash_migration_deleted(run_id, kind, row_id, row_json JSONB)          — rollback 删行前【完整备份】
-- rollback 完整覆盖(全部可执行,非注释):
--   R1 从 hash_backup 还原被 UPDATE 行旧哈希(旧 only_pending 口径恢复);
--   R3 🔴【迁移后新代码新增行】= fact id 不在【本轮 run 快照】的行 →【先 to_jsonb 完整备份(facts+entities)
--      进 deleted 表,再 DELETE】(不得无备份删客户 facts/entities);标本轮 run phase='rolled_back'。
--   —— 用【本轮 run_id 快照】确定性识别新增行,不依赖易错的部署时间戳;每轮备份/快照互不覆盖。
-- 本 .sql 保留为 dry-run 只读查询(STEP0)+ STEP-Q quiescence 运维手册;forward/rollback 以上述模块为可执行 SSOT。
-- 往返正确性(连续两轮 forward→新增→rollback 数据零丢失)见 tests/regression/test_nogo_v5_migration_lifecycle.py。
