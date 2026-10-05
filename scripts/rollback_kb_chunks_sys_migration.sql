-- 回滚 Task 1：kb_chunks sys_* 迁移
-- 用途：撤销 feat/xiaobang-system-kb-20260529 分支的 Task 1 迁移，
--       恢复 source_type 原始 CHECK 约束（仅含 doc/faq/preset）并删除 origin 列。
-- 注意：执行前确保 source_type 字段中无 sys_page/sys_field/sys_error 数据，
--       否则 ADD CONSTRAINT 会因已有数据违反约束而失败。

ALTER TABLE kb_chunks DROP CONSTRAINT IF EXISTS kb_chunks_origin_check;
ALTER TABLE kb_chunks DROP COLUMN IF EXISTS origin;
ALTER TABLE kb_chunks DROP CONSTRAINT IF EXISTS kb_chunks_source_type_check;
ALTER TABLE kb_chunks ADD CONSTRAINT kb_chunks_source_type_check
  CHECK (source_type IN ('doc','faq','preset'));
ALTER TABLE kb_chunks ALTER COLUMN source_type TYPE VARCHAR(10);
