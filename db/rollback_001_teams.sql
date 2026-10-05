-- rollback_001_teams.sql
-- 回滚 Phase 0: 团队隔离体系

DROP TABLE IF EXISTS user_notifications CASCADE;
DROP TABLE IF EXISTS team_members CASCADE;
DROP TABLE IF EXISTS teams CASCADE;
