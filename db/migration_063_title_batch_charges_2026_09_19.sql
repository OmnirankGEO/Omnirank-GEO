-- migration_063_title_batch_charges_2026_09_19.sql
-- WO_241 丙 · 标题批次 → 那一笔扣费 的映射
--
-- 为什么需要它(Review 裁定 2026-09-19):
--   「生成标题」那颗按钮有两个面孔。新加的词出标题 = **全新生产、从没付过费**
--   ⇒ 走批量端点按子集计费;某批次里**缺题的词**补救 = **已经付过费**
--   ⇒ 免费,但必须证明"这一批真的付过、这个词真的属于这一批、这个词真的还没出题"。
--
--   证明的第一环就是这张表:`generation_request_id` → 当时那笔 `charge_tx_id`。
--
-- 🔴 为什么不塞进 `topics`(那里已经有 `generation_request_id`):
--   `services/topic_generation_reservation.py` 抬头明令
--   「**扣费 / 退费口径一条不动 —— 本模块不 import billing,不碰任何积分**」。
--   把 charge_tx_id 挂到受理凭据行上就是把计费塞进那条边界里面。分开存。
--
-- 🔴 钱的标识**不下发前端、不由前端回传**:前端只带它已经有的
--   `generation_request_id`(来自项目详情里 topics 行的 `t.*`),
--   服务端拿它到这张表里解出 charge —— 校验权在服务端。
--
-- 迁移编号 063 由 `db/migration_manifest.py` 登记处分配(manifest max+1);
-- 同批已登记,分支自取的号不算数。

BEGIN;

CREATE TABLE IF NOT EXISTS title_batch_charges (
    generation_request_id TEXT PRIMARY KEY,
    user_id               INTEGER NOT NULL,
    quote_id              INTEGER NOT NULL,
    -- 组织路径走 reserve/settle 而不是 deduct_points,那时没有 consume 笔 id;
    -- 允许为空,但**为空就不给免费补救**(见 services/title_batch_charge_registry.py)。
    charge_tx_id          BIGINT,
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_title_batch_charges_quote
    ON title_batch_charges (quote_id, user_id);

COMMIT;
