-- 服务商控制名下客户线上购买开关(玩法 B · 线下收费模式支持)
-- 工单:docs/AI-CONTEXT/WORKORDER_CLIENT_PURCHASE_GATE_2026-07-27.md
--
-- 商业口径:服务商线下收客户的钱、用自己账号帮客户操作。客户线上直充会把关系
-- 变成玩法 A(平台收款 + 佣金),所以服务商需要一个能关掉「客户线上直购」的开关。
--
-- 两级开关(Owner 2026-07-27 补拍板):
--   1) 主账号默认 users.allow_client_online_purchase(NOT NULL DEFAULT TRUE)
--      —— 默认 TRUE = 存量行为逐字节零变化(今天所有人都能线上买)。
--   2) 每客户覆盖 customer_agent_bindings.online_purchase_override(NULLABLE 三态)
--      —— NULL = 跟随主账号默认 / TRUE = 该客户允许线上购买 / FALSE = 该客户仅线下。
--      NULL 为初始值,存量 35 行绑定全部保持"跟随默认"。
--   判定优先级:客户级 > 主账号默认(services/client_purchase_gate.py 单点实现)。
--
-- 纯 additive:不改任何既有列/约束/数据,不动资金流、计费、退款。
--
-- 生产迁移一律 pin 到 public。绝不用 current_schema() 或裸 regclass 解析状态,
-- 免得 search_path 诱骗让就绪检查看起来健康。

ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS allow_client_online_purchase BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN public.users.allow_client_online_purchase IS
  '服务商主账号默认:名下客户能否线上直接购买。TRUE=允许(默认) / FALSE=仅线下(客户见"联系推荐人办理")。销售子账号此列不参与判定,只认主账号。';

ALTER TABLE public.customer_agent_bindings
  ADD COLUMN IF NOT EXISTS online_purchase_override BOOLEAN NULL;

COMMENT ON COLUMN public.customer_agent_bindings.online_purchase_override IS
  '单客户覆盖三态:NULL=跟随主账号默认 / TRUE=允许线上购买 / FALSE=仅线下。优先级高于 users.allow_client_online_purchase。';

DO $$
DECLARE
  users_oid OID := to_regclass('public.users');
  bindings_oid OID := to_regclass('public.customer_agent_bindings');
  bad_columns INTEGER;
  users_default TEXT;
  override_default TEXT;
BEGIN
  IF users_oid IS NULL THEN
    RAISE EXCEPTION 'client purchase gate migration target public.users is missing';
  END IF;
  IF bindings_oid IS NULL THEN
    RAISE EXCEPTION 'client purchase gate migration target public.customer_agent_bindings is missing';
  END IF;

  -- 主账号默认列必须 NOT NULL(判定层不做 NULL 兜底猜测);客户级覆盖列必须
  -- 保持 NULLABLE —— NULL 就是"跟随默认"这一态本身,收成 NOT NULL 会毁掉三态语义。
  SELECT COUNT(*) INTO bad_columns
  FROM (VALUES
    (users_oid, 'allow_client_online_purchase', 'boolean'::regtype::oid, TRUE),
    (bindings_oid, 'online_purchase_override', 'boolean'::regtype::oid, FALSE)
  ) wanted(attrelid, attname, atttypid, attnotnull)
  LEFT JOIN pg_catalog.pg_attribute actual
    ON actual.attrelid = wanted.attrelid
   AND actual.attname = wanted.attname
   AND NOT actual.attisdropped
  WHERE actual.attnum IS NULL
     OR actual.atttypid <> wanted.atttypid
     OR actual.attnotnull <> wanted.attnotnull;
  IF bad_columns <> 0 THEN
    RAISE EXCEPTION 'client purchase gate migration found missing/wrong columns';
  END IF;

  -- 存量零变化硬核验:核对**列默认值**,不核对数据行。
  -- 这条迁移每次 prestart 都会重跑;服务商上线后真去关开关是预期业务行为,
  -- 若在此断言"没有任何被拦的行",第二次部署就会 RAISE 把发布打停。
  SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid) INTO users_default
  FROM pg_catalog.pg_attribute a
  LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
  WHERE a.attrelid = users_oid AND a.attname = 'allow_client_online_purchase' AND NOT a.attisdropped;
  IF users_default IS DISTINCT FROM 'true' THEN
    RAISE EXCEPTION 'client purchase gate: users.allow_client_online_purchase default must be true, got %',
      COALESCE(users_default, '<none>');
  END IF;

  -- 客户级覆盖列必须**没有** DEFAULT:新绑定行落 NULL = 跟随主账号默认。
  SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid) INTO override_default
  FROM pg_catalog.pg_attribute a
  LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
  WHERE a.attrelid = bindings_oid AND a.attname = 'online_purchase_override' AND NOT a.attisdropped;
  IF override_default IS NOT NULL THEN
    RAISE EXCEPTION 'client purchase gate: customer_agent_bindings.online_purchase_override must have no default, got %',
      override_default;
  END IF;
END $$;
