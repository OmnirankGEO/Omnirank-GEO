-- rollback_geo_research_monitor.sql
-- GEO 调研监测工具的回滚脚本(出问题反悔用)
-- 跑法: docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope < scripts/rollback_geo_research_monitor.sql
-- 验证: docker exec omnirank-db psql -U geo_admin -d geo_agentscope -c "\dt geo_research_*"  → 应返"未找到关系"

BEGIN;

-- 反向 DROP 顺序: 子表(有 FK 引用别人的)先 DROP,再父表
-- 1. 引用最多的表先删
DROP TABLE IF EXISTS geo_research_review_log CASCADE;
DROP TABLE IF EXISTS geo_research_article_citations CASCADE;
-- 2. articles 表(自引用 primary_article_id + 被 review_log/citations 引用)
DROP TABLE IF EXISTS geo_research_articles CASCADE;
-- 3. round_call (引用 round)
DROP TABLE IF EXISTS geo_research_round_call CASCADE;
-- 4. round (被 round_call/citations 引用,但前面已经删了)
DROP TABLE IF EXISTS geo_research_round CASCADE;
-- 5. prompts (引用 industries)
DROP TABLE IF EXISTS geo_research_prompts CASCADE;
-- 6. industries (被 prompts/citations 引用,但前面已经删了)
DROP TABLE IF EXISTS geo_research_industries CASCADE;
-- 7. 辅助表(独立,顺序无所谓)
DROP TABLE IF EXISTS geo_research_cost_log CASCADE;
DROP TABLE IF EXISTS geo_research_config CASCADE;

-- 删 migration marker
DELETE FROM _migration_markers WHERE marker = 'research_monitor_v1_initial';

-- 注意: 不删 reference_articles 已 archive 的行(那是 admin approve 进去的内部素材库)
-- 如果想恢复 archive 状态的素材,运行:
-- UPDATE reference_articles SET status='active' WHERE status='archived' AND archive_reason='research_monitor_undo';

COMMIT;
