-- ============================================================================
-- 监测格租户归属 · **只读 census**(工单 E3-1 → V3-C C-1 → V4-C C-1 终态)
-- ============================================================================
-- 🔴 **不在 migration_manifest 里**,也绝不许放进去。由 Deploy 发车前单跑一次。
--
-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ 🔴🔴 本脚本**不再回填任何东西**。§2 已整段退役(V4-C · Codex P1-1)。      ║
-- ╚══════════════════════════════════════════════════════════════════════════╝
-- 退役的理由,是一条我自己两次没走完的推论:
--
--   第一版:把结算行的付款人一列**直接写进**租户列(谁付钱就归谁)。
--           被 Codex 三审驳倒 —— 付款人只回答"谁付钱",不决定归属。
--   第二版:只在"付款人 等于 今天的品牌 owner"时才回填,自称"两个来源
--           相互印证"。被 Codex fix-of-fix 驳倒,真 PG16 反例:
--             历史 tenant = 4242、付款人 = 9999、品牌**后来转给** 9999
--             ⇒ 条件成立 ⇒ 写入 9999 ⇒ 错归属。
--
-- 第二版错在哪:`payer == 今天的 owner` 只证明"今天的 owner 恰好是当时的付款人",
-- 它与"品牌后来转给了付款人"**完全兼容**。要排除后者,需要的正是本文件自己
-- 一开始就写明**不存在**的东西 —— 品牌归属变更史。我写下了那个约束,
-- 然后建了一条需要它才成立的推论。
--
-- 结论(Review 裁定):**没有不可变运行时证据的自动回填,一律取消**。
-- 存量 NULL 行全部走**人工按证据裁定**;本脚本只负责把"有多少、各是什么形态"
-- 如实数出来交给人。
--
-- 用法(Deploy · 生产)——**两步,没有第三步**:
--   ① 跑 §1,把四个分档数抄进发车记录(`partition_ok` 必须是 t;不是就停下报 Review);
--   ② 若 §1b 的 `attempts_tenant_zero` > 0 ⇒ **停下报 Review**(账本里已有编造租户 0
--      的行,046 的 chk_defgeo_attempt_tenant_owner_positive 会让 prestart 非零退出)。
--      046 **未**部署时该表不存在 ⇒ 返回哨兵 **-1**;已部署且账本干净 ⇒ 返回 0。
--      -1 = "这一步什么都没证到",0 = "验过了,没有编造行"。两者不许混为一谈。
--   **不再有回填步骤。** 谁要动 tenant_owner_user_id,拿着 §1 的分档名单去做
--   人工裁定,一格一格给证据。
-- ============================================================================

-- ── §1 census(只读 · 单一 CASE 优先级互斥分类)──────────────────────────────
-- 🔴 为什么必须是**一个 CASE** 而不是四条各自带 WHERE 的子查询:
--    上一版就是四条独立子查询,于是 `owner IS NULL` + `payer IS NOT NULL` 的格
--    **同时**命中 `manual_payer_conflict` 与 `unresolvable` —— 四数之和 2、
--    待回填总数 1,发车记录上的账**闭不上**(Codex 真 PG16 输出)。
--    单一 CASE 天然互斥:一格只会落进第一个命中的分支。
--    并且 `partition_ok` 把"互斥且穷尽"这件事**在 SQL 里当场自证**,
--    不依赖谁记得去手加。
\echo '=== §1 census(所有 NULL 行都走人工裁定;本脚本不回填)==='
WITH classified AS (
    SELECT CASE
             -- 优先级 1:连今天的 owner 都没有 —— 最没有线索的一档
             WHEN b.owner_user_id IS NULL OR b.owner_user_id <= 0
                  THEN 'manual_no_owner_today'
             -- 优先级 2:没有同期结算行 ⇒ 关于"当时"零证据
             WHEN s.billing_user_id IS NULL
                  THEN 'manual_no_payer_evidence'
             -- 优先级 3:付款人 == 今天的 owner。**看起来像**互证,实际不是:
             --           它与"品牌后来转给了付款人"完全兼容(V4-C 反例)。
             WHEN s.billing_user_id = b.owner_user_id
                  THEN 'manual_payer_equals_owner_today'
             -- 优先级 4:付款人 != 今天的 owner(垫付/代付/转移,分不开)
             ELSE 'manual_payer_differs_from_owner'
           END AS bucket
      FROM public.monitoring_run_cells c
      LEFT JOIN public.brands b
             ON b.id = c.brand_id
      LEFT JOIN public.monitoring_keyword_settlements s
             ON s.settlement_reference = c.settlement_reference
     WHERE c.tenant_owner_user_id IS NULL
)
SELECT
    (SELECT COUNT(*) FROM public.monitoring_run_cells)                    AS cells_total,
    COUNT(*)                                                              AS cells_tenant_null,
    COUNT(*) FILTER (WHERE bucket = 'manual_no_owner_today')              AS manual_no_owner_today,
    COUNT(*) FILTER (WHERE bucket = 'manual_no_payer_evidence')           AS manual_no_payer_evidence,
    COUNT(*) FILTER (WHERE bucket = 'manual_payer_equals_owner_today')    AS manual_payer_equals_owner_today,
    COUNT(*) FILTER (WHERE bucket = 'manual_payer_differs_from_owner')    AS manual_payer_differs_from_owner,
    -- 🔴 互斥且穷尽的**机械自证**:四档之和必须等于待回填总数。
    --    这一位是 f 的时候,发车记录上的四个数就是一本闭不上的账,
    --    §1 的任何结论都不许引用。
    (COUNT(*) FILTER (WHERE bucket = 'manual_no_owner_today')
     + COUNT(*) FILTER (WHERE bucket = 'manual_no_payer_evidence')
     + COUNT(*) FILTER (WHERE bucket = 'manual_payer_equals_owner_today')
     + COUNT(*) FILTER (WHERE bucket = 'manual_payer_differs_from_owner')
     = COUNT(*))                                                          AS partition_ok
  FROM classified;

\echo '=== §1b 账本存量编造租户(预期 0;>0 必须停下报 Review)==='
SELECT
    CASE WHEN to_regclass('public.defgeo_monitoring_attempts') IS NULL
         THEN -1                                    -- -1 = 表不存在(046 未部署)
         -- 🔴 计数必须走 query_to_xml 的**动态**执行(表名在字符串里,计划期不解析)。
         --    直接写子查询时,PostgreSQL 在**计划期**就要解析 CASE 两条腿里的表名,
         --    `to_regclass(...) IS NULL` 这个运行期判断挡不住它 ——
         --    表不存在时整条 SQL 抛 UndefinedTable,-1 哨兵**永远拿不到**
         --    (2026-08-27 实测)。
         ELSE (xpath('/row/c/text()', query_to_xml(
                 'SELECT count(*) AS c FROM public.defgeo_monitoring_attempts'
                 ' WHERE tenant_owner_user_id <= 0', false, true, '')))[1]::text::int
    END AS attempts_tenant_zero;

-- ── §2 回填:**已退役,不留可执行残骸** ──────────────────────────────────────
-- 这里曾经有一条 UPDATE。它两次都建立在"从今天的状态推断当时的归属"之上,
-- 两次都被真 DML 反例驳倒(见文件头)。现在这一段**只剩这段说明**:
--
--   · 不留注释掉的 SQL —— 注释掉的 UPDATE 是一份"取消注释就能跑"的现成品,
--     而它的错误恰恰是"看起来完全合理"。判据 `test_e5_24` 钉住这一点:
--     本文件内不许出现任何写 tenant_owner_user_id 的语句,**注释里也不许**。
--   · 要改归属,走人工裁定:拿 §1 的分档名单,一格一格找当时的不可变证据
--     (合同/工单/客户确认记录),由人签字。没有证据的格**保持 NULL**——
--     账本对它们拒绝落账并告警(046 的 CHECK),那是可见的坏;
--     错归属是不可见的坏:所有按租户对账/隔离/计价的口径都会静默指向错的人。
