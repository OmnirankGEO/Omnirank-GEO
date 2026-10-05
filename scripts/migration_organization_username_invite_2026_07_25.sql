-- [WP6] 用户名式邀请开户:owner 设定登录用户名,被邀请人凭邀请 token 直接进入并自设
-- 初始密码(不走短信/邮箱验证码)。落库层只需放宽两张会持有 'username' 行的表的
-- target_kind CHECK:organization_invites(邀请本身)与 organization_operator_accounts
-- (开户成功后写入的操作账号)。verification_challenges / delivery_outbox 不会出现
-- 'username' 行(username 邀请显式不创建 challenge/outbox),保持更严约束不动。
--
-- 约束名沿用 PostgreSQL 对内联列 CHECK 的标准命名 <table>_target_kind_check(已实测)。
-- Prestart-only,幂等(DROP IF EXISTS + 无条件 ADD:每次应用 end-state 一致),
-- 纯 additive 语义(仅扩大枚举域,不删任何已有值;约束数不变=384)。
--
-- ⚠️ 冻结 schema 契约:本迁移改变 organization_invites / operator_accounts 的
-- target_kind CHECK 定义 → catalog fingerprint 改变。已同步在
-- services/organization_schema_contract.py 的 ACCEPTED_CATALOG_VARIANTS 增加
-- fresh+username 变体(counts 534/384/104 不变,仅 fingerprint 变)。生产部署侧
-- 另需按 sign_production_payer_catalog_variant 流程为生产 schema 形态签名变体
-- (Deploy-CTO 步骤,见 EXIT)。

ALTER TABLE organization_invites
    DROP CONSTRAINT IF EXISTS organization_invites_target_kind_check;
ALTER TABLE organization_invites
    ADD CONSTRAINT organization_invites_target_kind_check
    CHECK (target_kind IN ('phone', 'email', 'username'));

ALTER TABLE organization_operator_accounts
    DROP CONSTRAINT IF EXISTS organization_operator_accounts_target_kind_check;
ALTER TABLE organization_operator_accounts
    ADD CONSTRAINT organization_operator_accounts_target_kind_check
    CHECK (target_kind IN ('phone', 'email', 'username'));
