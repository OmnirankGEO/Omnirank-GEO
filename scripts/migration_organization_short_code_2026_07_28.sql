-- 团队短代码(organizations.short_code / short_code_locked + 部分唯一索引)
-- —— 对既有「自愈式 DDL」的补登记。
--
-- 【为什么会有这个文件】
--   services/organization_short_code.py::ensure_schema 把这三条 DDL 直接写在业务代码里,
--   docstring 原话「老库无需单独迁移脚本」。那是**懒初始化**:不跑到那条代码路径就不建列。
--   代码随 1d5da671 早已上线并潜伏数日,2026-07-28 16:43~17:22 之间第一次被真实用户走到,
--   当场给 organizations 加了 2 列 + 1 个部分唯一索引。
--
--   因为它绕开了迁移清单,也就绕开了 services/organization_schema_contract.py 的目录指纹,
--   于是两道 fail-closed 门同时锁死:
--     ① scripts/verify_unified_release_readiness.py → 一切部署被拦;
--     ② 🔴 server.py::_organization_schema_readiness_gate → **任何新容器启动即崩**。
--   当时 omnirank-blue 陷入崩溃循环,omnirank-green 还在服务纯粹因为它在漂移之前就已启动
--   —— 站点处于「一次进程死亡即不可恢复」的单点状态,而公网看上去完全正常。
--
-- 【本迁移在生产上是 no-op】
--   生产已由 ensure_schema 建出完全相同的形态(已按三闸签成契约变体
--   production_reanchor_short_code_v1 · 537/384/105 · bacbb607…)。这里全部用
--   IF NOT EXISTS,连跑无副作用。
--   **登记 manifest 的意义不是"让生产变化",而是让新库/重建库确定性地获得这个形态,
--   而不是靠"碰巧有人走到那条代码路径"。** 这正是本次事故的根因。
--
-- 【DDL 与 ensure_schema 逐字等价】
--   下面三条与 organization_short_code.ensure_schema 的三条 cursor.execute() 语义完全一致,
--   且 ADD COLUMN 顺序一致(short_code 先、short_code_locked 后),因此列序数(attnum)
--   与生产一致。生产实测渲染(签名时逐字取回,作为对拍基准):
--     organizations.short_code         text     可空,无默认
--     organizations.short_code_locked  boolean  NOT NULL DEFAULT false
--     organizations_short_code_unique  CREATE UNIQUE INDEX organizations_short_code_unique
--                                      ON organizations USING btree (short_code)
--                                      WHERE (short_code IS NOT NULL)
--
-- 【为什么 ensure_schema 仍然保留】
--   删掉它意味着任何"迁移没跑到"的环境会在运行时 UndefinedColumn 崩。保留幂等自愈是对的,
--   错的是**用它替代迁移登记**。docstring 里那句「老库无需单独迁移脚本」已删除。
--
-- 【安全性】纯 additive:新增 2 列 + 1 索引,不改任何既有列/约束/数据,不翻任何 feature flag。
--   短代码是**席位登录名的一部分**(见 organization_short_code 模块 docstring),
--   生产已有团队分配到 short_code,因此**绝不能**用"摘列回退"作为回滚手段 —— 会直接
--   弄坏该团队的席位登录。回滚只在确认无数据时进行,且须同步撤销契约变体签名。
--
-- 生产迁移一律 pin 到 public。

ALTER TABLE public.organizations
    ADD COLUMN IF NOT EXISTS short_code TEXT;

ALTER TABLE public.organizations
    ADD COLUMN IF NOT EXISTS short_code_locked BOOLEAN NOT NULL DEFAULT FALSE;

-- @index-guard organizations_short_code_unique ON organizations unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'organizations_short_code_unique' AND i.indrelid = to_regclass('public.organizations')) THEN
        NULL;  -- 已在 public.organizations 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'organizations_short_code_unique' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] organizations_short_code_unique 已存在但不在 public.organizations 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'organizations_short_code_unique' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX organizations_short_code_unique ON public.organizations (short_code) WHERE short_code IS NOT NULL;
    END IF;
END $idxguard$;

-- fail-closed 自验:形态不对就当场炸,不要留到运行时才 UndefinedColumn。
DO $$
DECLARE
    v_missing TEXT;
BEGIN
    SELECT string_agg(want.attname, ', ' ORDER BY want.attname)
      INTO v_missing
      FROM (VALUES ('short_code'), ('short_code_locked')) AS want(attname)
     WHERE NOT EXISTS (
        SELECT 1
          FROM pg_attribute a
         WHERE a.attrelid = 'public.organizations'::regclass
           AND a.attname = want.attname
           AND a.attnum > 0
           AND NOT a.attisdropped
     );
    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION 'organizations 缺少团队短代码列: %', v_missing;
    END IF;

    -- short_code 必须可空:老团队在被分配之前就是 NULL,部分唯一索引正是为此而设。
    IF EXISTS (
        SELECT 1 FROM pg_attribute
         WHERE attrelid = 'public.organizations'::regclass
           AND attname = 'short_code' AND attnotnull
    ) THEN
        RAISE EXCEPTION 'organizations.short_code 不应为 NOT NULL';
    END IF;

    -- short_code_locked 必须 NOT NULL DEFAULT false:set_short_code 靠它判"已经改过一次",
    -- 可空会让"从未改过"和"未知"混成一个状态,团队长可能改第二次而弄坏已发出的登录名。
    IF NOT EXISTS (
        SELECT 1
          FROM pg_attribute a
          LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
         WHERE a.attrelid = 'public.organizations'::regclass
           AND a.attname = 'short_code_locked'
           AND a.attnotnull
           AND pg_get_expr(ad.adbin, ad.adrelid, TRUE) = 'false'
    ) THEN
        RAISE EXCEPTION 'organizations.short_code_locked 必须是 NOT NULL DEFAULT false';
    END IF;

    -- 索引必须存在、唯一、valid + ready,且谓词正是 short_code IS NOT NULL。
    -- 谓词错了会把"多个团队都没分配短代码(全 NULL)"判成唯一键冲突,建团队直接失败。
    -- 谓词字符串用 pretty=TRUE 渲染,与 organization_schema_contract.catalog_snapshot
    -- 的 predicate 字段同一口径(pretty 形态无外层括号;pg_get_indexdef 里带括号的
    -- `WHERE (short_code IS NOT NULL)` 是另一种渲染,别拿来对拍)。
    IF NOT EXISTS (
        SELECT 1
          FROM pg_index i
          JOIN pg_class c ON c.oid = i.indexrelid
         WHERE i.indrelid = 'public.organizations'::regclass
           AND c.relname = 'organizations_short_code_unique'
           AND i.indisunique
           AND i.indisvalid
           AND i.indisready
           AND pg_get_expr(i.indpred, i.indrelid, TRUE) = 'short_code IS NOT NULL'
    ) THEN
        RAISE EXCEPTION 'organizations_short_code_unique 缺失或形态不符(唯一/valid/ready/部分谓词)';
    END IF;
END
$$;
