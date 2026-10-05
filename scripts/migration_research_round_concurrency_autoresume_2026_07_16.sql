-- ============================================================
-- 调研全量轮 提并发 + 4h 超时自动续跑 · config seed
-- (CODEX_SPEC_RESEARCH_ROUND_CONCURRENCY_AUTORESUME_2026-07-16)
--
-- 幂等: INSERT ON CONFLICT DO NOTHING(已存在的 key 不覆盖 · admin 手改过的值不回滚)。
-- 纯 additive: 只 seed geo_research_config 三行, 无 DDL, 无数据改写。
-- 消费侧: services/research_monitor/round_runner.py
--   - fetch_concurrency_per_platform → _get_stage1_concurrency()(读不到/非法回落 8)
--   - round_auto_resume_enabled / round_auto_resume_max → run_round_with_auto_resume()
-- ⚠️ 灰度口径(2026-07-16 部署前审核收紧): round_auto_resume_enabled 首发 seed 为
--    **false**，且代码内缺配置/读取失败也回落 false（fail-closed）——
--    先只上并发, 正常轮/受控超时验证通过后由 config API 翻 true, 单独放行。
--    动机: base 既有僵尸 sweep 心跳分支可在 >10min 心跳空窗误标活轮(误收尸),
--    开闸前先实测新并发下心跳空窗不复现, 消除自动续跑与误收尸叠加面。
-- 回滚: 停止使用这些键；禁止为了回滚删除已有运营配置。
-- ============================================================

INSERT INTO geo_research_config (key, value_json, description) VALUES
    ('fetch_concurrency_per_platform', '8',
     'Stage1 每平台并发数(4 平台并行 × N · 全局有效并发=4N · 默认 8)'),
    ('round_auto_resume_enabled', 'false',
     '4h 硬超时自动续跑开关(仅 reason=4h_hard_timeout · 预算熔断等其他 failed_resumable 不自动续 · 首发灰度 false, 并发验证通过后翻 true)'),
    ('round_auto_resume_max', '3',
     '单轮自动续跑次数上限(达上限保持 failed_resumable 等人工 · admin 续跑按钮不受影响)')
ON CONFLICT (key) DO NOTHING;
