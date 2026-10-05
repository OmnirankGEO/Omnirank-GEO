# AI-3 聚合后端与产品 API 包 — 一次性交付回包 (EXIT)

> 交付口径：**coded + local verified**。未部署、未迁生产、未翻配置、未 push；AI-3 无权宣布 release GO。
> 范围：GEO 统一观测飞轮 vNext 的**唯一聚合后端 + 只读产品 API**。不含前端（Frontend-A/B 赛马）、不含 observation/policy/aggregate 业务 migration（AI-2）、不含 provider adapter/采样（AI-1）。

## 1. Base / HEAD / branch / worktree / clean

| 项 | 值 |
|---|---|
| worktree | `C:\AI-Test\omnirank-ai-geo-observation-analytics` |
| branch | `feat/geo-observation-analytics-2026-07-17` |
| DEVELOPMENT_START_SHA (HEAD 基) | `1fae96bdb6f4125c43fec892ffb8332c6ec8f897`（docs-only 合同指针 commit） |
| SIGNED_RELEASE_BASE_SHA | `6059b0884421d6fd1a2c34ee57c19b98259c5bdc` — `merge-base --is-ancestor <sha> HEAD` = 0 ✅ |
| CONTRACT_FREEZE_COMMIT | `8daa1e53f85c78ee9c5b7769889e8d5aa3acfa7e` — 是 HEAD 祖先 ✅ |
| 冻结合同哈希 | observation `65C1..D5AA` / copy `0FF5..A0B6` / fixture `B5C4..F1B5` — 逐字节匹配任务给定值 ✅ |
| 红线文件 | 零改动（billing/connection/auth/server/scheduler/monitoring_api/App.tsx/module_mapping 均未 touch）✅ |
| `git diff --check` | clean（全 LF）✅ |

新增文件（全部在 AI-3 独占边界内）：
- `services/geo_observation_analytics/**`（contract, metrics, aggregates, repository, trends, opportunities, privacy, explain, evidence, errors, `contracts/`, README, T0, 本回包；重复 reference DDL 已删除）
- `schemas/geo_observation_product.py`
- `api/geo_observation_product_api.py`
- `tests/geo_observation_analytics/**`

## 2. 后端指标公式与版本

`metric_version = geo-observation-metrics-v1`，`contract_version = geo-observation-contract-v1.4`，`aggregation_version = geo-observation-aggregation-v1`。公式 SSOT 全在 `metrics.py`（无任何 LLM 参与指标计算）：

- **分母纪律**：`valid_observations` = 十分类中 8 类（排除 `entity_ambiguous`/`engine_error`）；UNKNOWN/ambiguous/engine_error 永不进分母；`null` 绝不当 0。
- `presence_rate` = 明确/条件推荐/候选/仅提及 之和 / valid；其余各率 = 各计数 / valid；`refusal_rate` = 两类拒绝之和（展示聚合，两类仍分列）。
- `citation_rate` / `source_visibility_rate` / `evidence_coverage_rate` 分列（fallback 来源不入 citation）。
- rank Top1⊆Top3⊆Top5 累计；`avg_position_milli` 仅对有位次观测求均（否则 None）。
- `share_of_voice` = presence /(presence+Σcompetitor_count)，无出现为 None。
- `volatility` = 出现序列翻转率；`model_shift_index` = 最新 model_revision 边界前后分布 TVD（一侧空为 0，**确定性 tiebreak**）；`trend_confidence` = 样本/来源/时间跨度/稳定度（公共另加品牌独立度）有界乘积。
- `stability` 优先级 insufficient > shifted > watch > stable；单桶 `confirmed_change` 恒 False（"波动中"），跨桶确认在 trend 端点。
- 全 `*_bps` 整数 0..10000，round-half-up。

## 3. API / DTO 契约与隐私 allowlist

- DTO：`schemas/geo_observation_product.py`，`_StrictDTO(extra='forbid')` 分 private/public/admin。
- **private** DTO 可含 `brand_id`（自有品牌），禁 `owner_user_id`/上游/成本/trace/source-PK/HMAC；**public** DTO 另禁 `brand_id`/`aggregate_key`；**admin** 可追溯但无密钥/他客户原文/成本 trace。
- 运行期守卫：每个非 admin 响应过 `privacy.assert_no_private_leak` / `assert_no_public_leak`（构造即失败）。
- 错误信封 `{code,message}` 与 fixture `error_cases` 逐字一致（FORBIDDEN/VERSION_CONFLICT/ENV_OVERRIDE_ACTIVE/OBSERVATION_UNAVAILABLE/INSUFFICIENT_SAMPLES）。

## 4. 三类产品端点 + 管理员端点 + 统一 race fixture

- 私域（brand-owner RBAC，不只信 brand_id）：`/api/geo-observation/brands/{id}/{summary,trend,platforms,questions,evidence/{obs},opportunities,insights,insights/{job}}`
- 公共（k-anonymity gate + 显式 insufficient，禁 fallback）：`/api/geo-observation/industries/{key}/{baseline,source-patterns,content-opportunities}`
- 管理员（admin RBAC）：`/api/admin/geo-observation/{overview,platform-health,model-shifts,aggregate-diff,content-opportunities}`
- 冻结 fixture `frontend_race_fixture_v1.json` 已 vendored + 哈希锁；OpenAPI `contracts/openapi_geo_observation_product_v1.json`（16 路径 / 35 schema）与 fixture 业务语义兼容（契约测试逐 case 断言 DTO ⊇ fixture keys）。

## 5. repository / 公式 / job / API 判别测试 + 真实 PostgreSQL / 并发证据

真实 PostgreSQL（`geo_observation_test` throwaway，非 prod）+ WORKERS=4 级并发。**89 测试全绿**：
- `test_contract_lock`：哈希 + 枚举 + 聚合字段顺序 + **反查 DDL 列 == 机器契约字段**。
- `test_metrics`（22）：rate 边界/round-half-up、分母排除 UNKNOWN、rank/SoV/volatility/model-shift/trend/stability/confirmed-change。
- `test_aggregates`（14）：promoted-only、withdrawn 排除、**撤回重算（含 max-watermark 撤回）**、**全撤回 cell 被 prune=0**、computed_at 无回退、per-brand cap、**NULL 维度不与 overall 撞键**、model-shift 确定性、平台分解、**trend 取最新非最旧**。
- `test_concurrency`：20 并发聚合恰一版本。
- `test_privacy`：k-anon 全满足门 + 只升不降 + 泄漏守卫。
- `test_product_api`（15）：summary/platforms/questions/opportunities 业务字段、RBAC 跨租户 403、未登录 403、**insight IDOR 回归（他租户 job 不可读）**、语义完成/失败 503、admin 门、公共 k-anon insufficient/ok 无泄漏。
- `test_semantic`（10）：请求体零禁字段、官方身份、无 DashScope、20 并发一次调用、失败不回退、输出白名单。
- `test_openapi_compat`：路径齐、DTO ⊇ fixture、copy 合同覆盖十类 outcome + 指标标签 + 必备状态。

## 6. OpenAPI 与两前端候选可消费的类型合同

`contracts/openapi_geo_observation_product_v1.json`（LF，稳定）+ vendored `frontend_race_fixture_v1.json` / `frontend_copy_and_race_v1.json`（哈希锁）。两候选只读消费，禁改 fixture / 禁跨界改后端。

## 7. 两轮对抗审核

- **Round 1**（8 维 find→adversarial verify）：6 candidate，**6 CONFIRMED** → 全部修复：
  1. [P1 IDOR] insight job 按 job_id 单读跨租户泄漏 → job 表加 owner/brand NOT NULL + `UNIQUE(owner,brand,input_hash)`，`get_job` 三键对象级授权。
  2. [P1] overall 行与 NULL 维度分解行 aggregate_key 撞键损坏主聚合 → 非 overall 分组跳过 NULL 维值。
  3. [P1] watermark 无回退守卫挡住撤回重算（撤回数据滞留）+ 全撤回 cell 不清 → 守卫改 `computed_at`，`refresh_scope` 增 `prune_stale_cells`。
  4. [P2] `trend_series` 返回最旧 N 桶（趋势冻结） → 子查询取最新 N 再升序。
  5. [P3] `model_shift` 对 set 排序 tiebreak 非确定（破坏幂等） → `(min observed_at, rev)` 全序。
  - 每修复配判别测试（89 绿含 6 新回归测试）。
- **Round 2**（fix-of-fix，5 维 · fix-idor/fix-collision/fix-prune/fix-trend/holistic-resweep · find→adversarial verify）：**candidate=0，CONFIRMED=0** ✅ 收敛。5 个修复未引入回退（无 UNKNOWN→0、无吞异常假成功、无永久 pending、无新 silent fallback、prune 未误删跨版本/跨 scope/跨行业、composite-unique 未破坏 20 并发一任务、trend 子查询参数序正确）。

## 8. final integration 精确接线（AI-3 不自行跨界，仅说明）

见 `README.md` §"Integration wiring"。要点：生产 migration 是唯一 schema SSOT，并追加 `geo_observation_insight_jobs`（本包语义缓存，非业务表）；`server.py` `include_router(build_routers(default_deps()))`；`auth/module_mapping.py` 加 `("/api/geo-observation/", None)`；`api/scheduler.py` 按 ROLE=cron 单控制面注册 `refresh_scope` 纯函数；注入 `platform_health_provider`(AI-1)/`policy_provider`(AI-2)/确认 `evidence_source` 的 `source_record_id` 语义。

## 9. 未尽风险

见 `README.md` §"Residual risks" R1–R9（要点：paid_diagnosis/research_round 证据文本为集成 seam；insight 缓存表需集成 migration；aggregate-diff 待接 geo_engine_stats 口径；platform_health/policy 注入；聚合窗口内存物化（按 cell 有界）；单桶 confirmed_change 恒 False；官方 DeepSeek 绕过 degrading fallback 为有意；指标阈值 shadow 待回放校准；source_domains jsonb 形状假设；聚合 refresh 需 ROLE=cron 单 leader + sched_claim 序列化以防蓝绿重叠 stale 重插）。

## 11. NO-GO 复审返工(外部复审 1 P0 + 6 P1 + 2 P2 · 全修)

外部集成复审(基于 AI-2 真实 migration)判 NO-GO。已按真实 schema 逐条返工:

- **P0 SQL 兼容**:tests 改为跑 **AI-2 真实 migration**(`contracts/ai2_migration_geo_observation_v1_2026_07_17.sql`,vendored + drift-guard 测试对齐 governance worktree)而非 AI-3 自建 schema。`aggregates`/`repository` 只读真实列:`search_enabled` 从 **event**(signals 无该列),删除臆造的 `brand_source_visible`/`evidence_verifiable`,`source_visibility`/`evidence_coverage` 从 `source_count`/`quality_score_bps`/`citation_count` 派生;source_domains 用 `type`(非 `stype`)。
- **P1#2 三来源公共晋升**:public 聚合不再要求 event owner/brand 为 NULL——纳入 paid_diagnosis + monitoring(私域 owner/brand 保留于 event,仅在 DTO/身份列隐藏);判别测试 `test_public_aggregation_includes_client_sources`。
- **P1#3 加权指标 + 单品牌上限**:public 展示率改为 `effective_weight` 加权 + per-brand cap 重分配(I8);私域仍原始计数。判别测试 `test_per_brand_cap_moves_public_presence_rate`(raw 7692 → capped 3023)。
- **P1#4 域名级 k 匿名**:source-patterns 按每域名独立 user/brand/source 贡献者数门控(非仅出现次数);单客户重复域名被抑制。判别测试 `test_source_patterns_domain_level_kanon`。
- **P1#5 实时 policy/health**:public 端点每请求 `deps.kanon.raised_to(policy)`(阈值只升即时生效);admin health 消费 AI-1 真实 shape(`platform_key`+`status`),`enabled_by_policy` 取自 policy;`default_deps()` policy_version=None → readiness `unavailable` fail-closed。判别测试 `test_policy_can_raise_kanon_threshold` / `test_admin_health_maps_ai1_shape_and_fail_closed`。
- **P1#6 洞察崩溃恢复**:insight job 加 lease + attempts;create 用 `ON CONFLICT DO UPDATE ... WHERE failed OR 租约过期` 原子 reclaim(failed/stuck 可重跑,一次一调用);`recover_stuck_jobs` sweeper(交 cron)。判别测试 `test_failed_job_resubmit_reruns` / `test_stuck_job_recovered_by_sweeper`。
- **P1#7 聚合原子性**:`refresh_scope` 取 `pg_advisory_xact_lock(桶键)` 后单事务 upsert+prune 一次提交(撤回原子生效,不靠 cron leader 运气;并发同桶串行)。
- **P2#8**:空 summary 各率改 `null`(非伪 0%);**P2#9**:`granularity` 加 `pattern` → 非法值 422。
- 全套 **97 测试绿 vs AI-2 真实 migration**(+7 判别测试)。红线零改;diff --check clean(LF)。
- **返工后 Round 3 复审**(7 维 · schema-binding/weighted-public/domain-kanon/policy-health/insight-recovery/atomic-refresh/regressions · find→adversarial verify):**candidate=0,CONFIRMED=0** ✅ 收敛(真实 schema 绑定正确、加权/cap 数学正确且私域不变、域名 k 匿名不漏、policy 只升即时生效、insight 无双跑/无永久 pending、refresh 原子且同桶串行、早前 5 修复无回归)。
- 干净提交 `5bfd34e1`(feature 分支,未 push);两个冻结 SHA 均为祖先。

## 13. 第二轮外部集成复审返工(1 P0 + 4 P1 + 3 P2)· HEAD `407c0038`

第二轮复审(基于 AI-2 真实 `policy.py`)判 NO-GO。触发因素=**AI-2 真实 policy 访问器 get_policy() 的嵌套形状**此前未接。逐条返工:

- **P0 真实 policy 接错**:AI-2 `get_policy()` 返回 `{policy_version, policy:{platforms, public_min_independent_brands, public_min_source_types, max_single_brand_share_bps, feature_flags}, effective_flags:{product_enabled,...}}`(嵌套),此前按扁平读。修=`_policy_view` 按真实嵌套解析;`_require_product_enabled` 对**全部**私域+公共产品端点按 `product_enabled` fail-closed(false/未接→503);`_effective_kanon` 读 policy 门(只升);admin `enabled_by_policy` 取自嵌套 platforms;readiness policy_version None→unavailable。判别:`test_product_disabled_flag_blocks_endpoints`/`test_policy_can_raise_kanon_threshold`/`test_admin_health_maps_ai1_shape_and_fail_closed`。
- **P1#1 lease 无 fencing 双跑覆盖**:接管无 claim token,旧 worker 完成无条件覆盖。修=`lease_token` fencing,所有状态写 CAS on `(job_id AND lease_token)`;被顶替的旧 token 更新 0 行无法覆盖。判别 `test_fencing_prevents_revived_old_worker_overwrite`(实测旧 worker _complete 返回 0 行,summary 保持 NEW)。
- **P1#2 多版本读旧口径**:读不筛版本,`latest` 随机取旧、trend 同日双行、又标为当前 metric_version=口径冒名。修=所有读筛 contract+aggregation+metric=代码常量 + computed_at DESC;trend `DISTINCT ON(bucket_start)`。判别 `test_reads_current_lineage_not_stale_version`。
- **P1#3 零有效伪 0%**:全 ambiguous/error 仍落 0% 行。修=`_compute_cell` valid==0 返 None 不落行。判别 `test_zero_valid_cell_produces_no_row`。
- **P1#4 缺桶 fail-open + cap 数学不成立**:public LEFT JOIN 把缺桶 event 当独立品牌(76.92% 逃逸),cap 只按原始总额→单品牌仍 >10%。修=public fetch 缺桶 event 排除(fail-closed);`_cap_brand_weights` water-filling 保证单品牌 ≤cap 占**最终**分母;`_rate_cell_kanon` 把 min brands 升到 ceil(10000/cap) 使可行。判别 `test_per_brand_cap_moves_public_presence_rate`(raw 5263→capped ≤1200)。
- **P2#2**:content-opportunities 加 status wrapper(insufficient≠空);**P2#3**:测试助手移入 `_support.py`(修隔离环境 3 模块采集失败);**P2#1**(秘塔代理通道说明)与**冻结 §7.2 + fixture**(`surface_note` 明示)直接冲突——按治理"不静默改冻结契约"保留 §7.2 行为并**上报冲突待裁定**(P2·无隐私泄漏)。
- 全套测试绿 vs AI-2 真实 migration(+新判别:fencing/product-flag/zero-valid/dual-version/加权 cap/域名 k 匿名)。红线零改;diff --check clean。
- **Round 4 复审**抓出 2 处**我方修复自身的 bug**(全修 · HEAD `034f0503`):
  - [P1] `_cap_brand_weights` water-filling 用固定 `n+2` 迭代**不收敛**(多品牌同时超限时几何收敛需更多迭代)→ 主导品牌仍 ~2x cap。修=迭代至真收敛(每轮 total 严格下降必终止)。判别 `test_cap_water_fill_actually_converges_to_cap`(5 大+6 小品牌:修前 ~1859bps → 修后 ≤1001bps)。
  - [P2] `model_shift_rows` 是唯一漏筛版本+去重的读→ policy CAS/metric bump 后 admin `/model-shifts` 出现同 cell 重复/陈旧口径行。修=加 `_VERSION_PREDICATE` + `DISTINCT ON(scope,industry,platform,model_revision,bucket)` 取 computed_at 最新。判别 `test_model_shift_rows_dedup_and_version_scoped`。
  - 全套 **103 测试绿**。
- **Round 5 fix-of-fix 复审**又抓出 1 处 model_shift_rows 修复的**遗留 bug**(修 · HEAD `18628081`):DISTINCT ON 只约束了 platform/revision,未把其余分解维(surface/source/is_branded/intent/search)约束为 NULL→ 这些 platform_key=NULL 的子段行与 overall 行同组、computed_at 相同时任取一行→ 子段口径可能被冒充为行业级。修=WHERE 加全分解维 NULL(仅留 overall + per-platform)。判别 `test_model_shift_rows_excludes_subsegment_breakdowns`(稀释数据集:overall<2000 但 branded 子段=10000,断言不泄漏)。104 测绿。
- **Round 6 fix-of-fix + 全包回归复审**:全包广扫**零新增**,仅 model_shift_rows 再遗留 1 处(修 · HEAD `f887b21a`):DISTINCT ON 漏 `bucket_granularity`→ 日/月桶同 bucket_start(每月 1 号)折叠丢粗粒度行;并把治理视图 scope 到 `public_industry`(owner/brand 恒 NULL→键完整)+ DTO 加 `bucket_granularity` 标签。判别 `test_model_shift_rows_keeps_granularities_distinct`。105 测绿。
- **Round 7 fix-of-fix + 全包回归**:全包广扫**再零新增**,仅 model_shift_rows 第 4 处(修 · HEAD `8dbac464`):`>=threshold` 在 DISTINCT ON 去重**之前**的 inner WHERE→ 旧 policy_version 的高 shift 陈旧行可越过阈值赢得去重,而最新(已解决、低于阈值)行被滤掉→ 误报"仍漂移"。修=阈值移到去重**之后**的 outer WHERE(按最新口径判定)。判别 `test_model_shift_rows_threshold_after_dedup`。106 测绿。
- **Round 8 最终深审(model_shift_rows 完整性 + 全包最终回归)**:**candidate=0,CONFIRMED=0** ✅ **收敛**。model_shift_rows DISTINCT ON 键完整、阈值后置正确;全包最终回归零发现。
- 说明:第 4-8 轮所有 CONFIRMED 均集中在 `model_shift_rows`(admin-only 诊断读,无隐私/资金/数据影响);全包广扫每轮均**零新增**——核心(指标/聚合/隐私/RBAC/语义/policy)稳定,仅该 admin 诊断读的 DISTINCT ON 键+阈值语义逐轮收敛。

## 14. 第三轮外部集成复审返工(3 P1 · 全修)· 109 测绿

第三轮复审(基于 e54d68dd)判 NO-GO,主体质量认可,仍 3 处 P1 correctness/observability。逐条返工:

- **P1#1 readiness 假报 ready**:旧 readiness 只看 pending/rejected/stuck backlog,忽略 `aggregation_enabled=false`/缺 health/degraded-unknown/aggregate 为 NULL/reconciler 积压→未接线也报"就绪"。修=`admin_overview` 三态判定:`unavailable`=(policy_version None **或** aggregation 未开 **或** DB 探针失败 **或** aggregate_updated 为 None **或** 任一平台 unavailable);`attention_required`=(pending/rejected/stuck>0 **或** 缺 enabled 平台 health **或** degraded/unknown **或** reconciler_delay>`_RECONCILER_DELAY_ATTENTION_SECONDS`(300));否则 `ready`。判别 `test_readiness_not_ready_unless_pipeline_running`(aggregation off + 无 aggregate→unavailable;补齐后→ready)。
- **P1#2 服务商原始 API 仍泄露"秘塔检索代理"**:按最新裁定——**普通客户与服务商只显示 DeepSeek;精确 surface/provider(含秘塔代理通道)仅管理员可见**。修=用户面 `brand_platforms.surface_note=None`、`brand_evidence.channel_disclosure.note=None`(删 `_surface_notes_by_platform` 及 `SURFACE_USER_NOTES` 的用户面消费);管理员 `AdminPlatformHealthDTO` 新增 admin-only `surface_key/provider_key/model_key/model_revision`(取自 AI-1 health DTO)。判别 `test_proxy_channel_note_not_user_facing_admin_sees_surface`(用户响应 blob 断言无"秘塔",admin `/platform-health` 断言可见 `deepseek_metaso_proxy`)+ 更新 `test_platforms_display_and_historical`。
  - **⚠️ 契约修订接缝(freeze-process 所有,非 AI-3)**:冻结 `observation_contract_v1.json §7.2` 与 `frontend_race_fixture_v1.json`(第 58/90 行)仍把 surface_note/channel note 明示为**用户面**——与本裁定冲突。AI-3 **不静默改哈希锁冻结件**,须由冻结流程出**契约修订(建议 v1.5)**把 surface note 降级为 admin-only 后同步 vendored fixture 哈希。当前 `test_openapi_compat` 仅做**结构 keys 子集**断言(非值断言),故不受影响;代码已按新裁定 fail-closed。
- **P1#3 数据库异常被折算成"0 个卡死任务"**:`stuck_claim_count` 旧实现 try/except 吞异常返 0→ schema/权限/DB 故障被伪装成健康。修=`repository.stuck_claim_count` 去掉吞异常,查询失败**直接抛**;`admin_overview` 单独 try 探针,失败→`conn.rollback()` 并置 `probe_ok=False`→readiness `unavailable`(而非静默 0)。判别并入 `test_readiness_not_ready_unless_pipeline_running`。

**统一集成门禁(接缝声明,AI-3 侧已就位/或明确他方所有)**:
- **AI-2 迁移可复现 + 治理文件仍在churn**:tests 绑定 AI-2 治理迁移的 vendored 副本。本会话内 AI-2 治理迁移(仍 `?? 未跟踪`)**反复漂移(≥4 次)**:①对 12 个 events 核心列补 `ALTER … SET NOT NULL`(空表)+ 校验守卫;②policy seed 里 deepseek 默认 `surface_key` 改值;③policy 单例表加列(promotion_legal_basis/consent_policy_version/outcome_gold_gate_passed/…)。**全部 additive/seed-only,无删列/改名,AI-3 读 SQL 零影响(逐列核验 `AI-3-read columns touched: NONE`),且 events 核心列 base CREATE 已 NOT NULL→fresh 库 no-op**。终态**pin 到已知良好快照**(SHA256 `6aff9f83…`)并**总是运行的哈希锁**(`test_vendored_ai2_migration_hash_locked`=权威可复现 pin,与外部工作树无关)。`…_matches_governance` 已**改为非失败 advisory**(drift→warn+skip,不再硬断言):对一个可变的未提交外部文件硬断相等会让本套件**非确定性变红**(实证:硬断版本本会话被 AI-2 churn 触发红 4 次)。**AI-2 仍未提交干净 SHA**=**已声明门禁依赖**:AI-2 提交后集成须从提交 blob 重新 vendored 并 bump 哈希锁常量。**不追每次 churn**=有意设计(pin 快照 + advisory,避免无止境追逐)。
- **包可复现修复**:vendored AI-2 迁移与 AI-3 参考 schema 两个 `.sql` 被根 `*.sql` 规则忽略→干净检出缺文件无法复现测试。已按仓库既有约定加 `.gitignore` 定向 `!` 例外,二者现已入库。
- **公共读限流+缓存(集成接缝,后续窗口接)**:public 端点限流 + 缓存键 = `(policy_version + metric_version + aggregate_watermark)`,**撤稿(withdrawal)时失效**,**禁纯 TTL**(否则撤稿后旧口径可被缓存续命)。AI-3 已保证读随 watermark/computed_at 单调,不阻碍该层接入。

- 全套 **110 测绿** vs AI-2 真实(最新)migration;红线零改;`git diff --check` clean;OpenAPI 已重生成(仅 `AdminPlatformHealthDTO` 增 4 admin-only 字段,标题/版本保持)。
- **第三轮返工后复审**:见 §15。

## 15. 第三轮返工后对抗复审(5 lens find → adversarial verify)· 1 CONFIRMED(P2)全修 · 110 测绿

对 §14 修复 delta(HEAD `eeb39d65`)跑 5 lens(readiness/proxy-leak/db-exc/migration-hash/test-power)find → 逐条对抗 verify(默认 REFUTED)。结果 **candidates=4,CONFIRMED=1**:

- **[CONFIRMED · P2 · 已修]** readiness 健康判定**未按 enabled-by-policy 平台限定**:`unavailable`/`degraded-unknown` 谓词遍历**全部**上报平台。一个已退役/禁用的 surface(kimi ships `enabled=false` + 属 `HISTORICAL_PLATFORM_KEYS`)被 AI-1 报 `unavailable` 时,即使所有 enabled 平台健康、管道完全在跑,也会把整品 readiness 误判 `unavailable`(或 degraded/unknown→永久 `attention_required`)。**不对称**:兄弟 `missing_health` 检查已按 `enabled_platforms` 限定,而这两个健康谓词没有。修=先 `enabled_health = [h for h in health_items if h.enabled_by_policy]`,两谓词只遍历 enabled_health。判别:①`test_readiness_...` 增子例——doubao(enabled)健康 + kimi(disabled)unavailable → **ready**;②反向 guard——doubao(enabled)unavailable → **unavailable**(防修复过度限定藏真故障)。
- **[REFUTED×3]**(对抗 verify 逐条驳回,均"非提交代码缺陷"):①aggregate 只检存在不检**新鲜度**→ 驳回:模块内无任何新鲜度 SLA(唯一阈值是 300s reconciler 队列延迟),存在性即约定口径,复用 300s 判 day/week/month aggregate 反而错;②readiness `unavailable` 断言过定(未隔离 `aggregation_enabled` 子句)→ 驳回为 test-power 建议(非缺陷);③DB 探针失败→unavailable 路径未被测试覆盖→ 驳回为 test-power(提交代码正确)。

**但②③是真实 test-power 缺口 + §14 exit 报告对 P1#3 有 overclaim**(声称"判别并入"实则该路径未被触发)。按外部复审"补判别测试"要求全部补齐(诚实回填):
- 隔离 `aggregation_enabled` 子句:新增子例——aggregate **已存在**但 `aggregation_enabled=false` → `unavailable`(此时是"关"而非"缺 aggregate"逼出 unavailable,可杀该 mutant)。
- DB 探针失败判别:①`test_stuck_claim_count_raises_on_db_error_no_swallow`——对已关闭连接调 `repo.stuck_claim_count` 断言**抛异常**(证 repository 半边不吞);②readiness 测内 monkeypatch `repo.stuck_claim_count` 抛错 → readiness `unavailable` 且 `stuck_claims==0`(证 admin_overview 探针半边 rollback+fail-closed)。

复审元教训沉淀:①**enabled-scope 一致性**——同一 handler 内对"平台集合"的多处判定(缺失/不可用/降级)必须用**同一** enabled 过滤,漏一处即不对称 bug;②对可变的未提交外部依赖(AI-2 治理文件)**禁硬断言相等**,否则套件非确定性变红——权威证据用**总是运行的内容哈希锁**,外部对比降级为 warn/skip advisory;③返工后声称"判别测试已覆盖"必须**实跑反向 mutant 自证**(revert 修复→测试变红),否则就是 overclaim(本轮 exit 报告即被抓到 P1#3 overclaim)。[[feedback_expose_weakness_over_pretend_pass]] [[feedback_test_must_follow_code_change]]

### §15b 收敛复审(对 §15 修复 delta `ae70d0a8` 再审)· 2 CONFIRMED(P2)全修 · mutation 自证

对 §15 的 enabled-scope 修复 + 补测 + advisory 再跑 3 lens find→verify,**candidates=2,CONFIRMED=2**——**我的修复自身引入 1 个 fail-OPEN 回归 + 漏判别另一半**:

- **[CONFIRMED · P2 · 已修]** **enabled-scope 修复 fail-OPEN 回归**:当 `enabled_health` 为空(policy.platforms 为空/半接线——受支持输入,如中途部署)时,两个 enabled-scoped 谓词对空列表求值恒 False→ 即使 AI-1 报告**全平台 unavailable**(全站故障),只要 aggregation 开 + aggregate 存在 + 探针 ok,readiness 竟判 `ready`。与模块 fail-closed 原则(`_PolicyView` docstring "Everything fail-closed")相反,且比修复前更糟(修前遍历全部 health 会捕获)。修=`unavailable` 谓词加 `or not enabled_platforms`(零 enabled 平台=什么都没观测=绝不 ready)。判别 `c_empty`(空 platforms + 全 AI-1 unavailable→unavailable)。
- **[CONFIRMED · P2 · 已修]** **attention 半边(degraded/unknown 的 enabled-scoping)零判别**:补测只覆盖 disabled 平台报 `unavailable`(判别 L630),没覆盖 disabled 平台报 `degraded/unknown`(L635)→ 单独 revert L635 全套仍绿(mutant 存活)。补 `c_deg`(doubao enabled healthy + kimi disabled degraded→ready)。
- **mutation 自证(reviewer 铁律)**:逐一 revert 三处修复子句(去 `or not enabled_platforms` / L630 enabled_health→health_items / L635 enabled_health→health_items),readiness 测**均变红**;还原后**绿**——三子句全被判别。
- 元教训:**enabled-scope 修复必须连空集边界一起想**——把"遍历全体"改成"遍历子集"时,子集为空是新的 fail-open 面(空 any()=False 吞掉信号);缩小判定域的重构必须补"域为空"的 fail-closed 分支 + 判别测试。这正是我上一轮"enabled-scope 一致性"教训的**反面陷阱**,两条要成对记。[[feedback_expose_weakness_over_pretend_pass]]
- 全套 **110 测(109 passed + 1 advisory skip)**;红线零改;diff --check clean。**收敛于 CONFIRMED=0 见 §15c**。

### §15c 第二次收敛复审(对 `c21ae65d` 4 lens 全扫)· CONFIRMED=0 收敛 · readiness 全子句判别补齐 · 118 测

对 §15b 修复后的完整 readiness 谓词 + 空集修复 + 核心回归再跑 4 lens(readiness-complete 真值表全扫 / regression / core-sweep 隐私·RBAC·聚合·语义·policy / test-discrim)→ verify。**candidates=3,CONFIRMED=0**——**代码已收敛**。3 候选全 REFUTED(均 test-power 观察,非 wrong-output 缺陷):①整个 attention 分支无判别测试;②U1(policy_version None)/U5(aggregate 缺)unavailable 子句无判别;③error/raw pending 事件态对 readiness 与 counts DTO 不可见(经核 event_state_counts GROUP BY 全枚举、AdminCountsDTO 符合契约,非缺陷)。

**代码 CONFIRMED=0 收敛**;但②③暴露的 test-power gap 是真实的(mutation 存活)。按 ultracode + 外部复审"补判别测试"标准,**补齐 readiness 每条子句的独立判别测试**(reviewer 铁律:实跑反向 mutant 自证):
- 新增 8 测(baseline-ready 基线 + 逐子句隔离):U1 policy 未接线→unavailable;U5 aggregate 缺→unavailable;missing_health(enabled 平台无 health 上报)→attention;pending_review 积压→attention;rejected 积压→attention;reconciler_delay>300(pending 事件 raw 改 created_at 2h 前)→attention;stuck(processing 事件 raw 改 lease_until 过期)→attention;+ baseline 读 ready 的正例。
- **mutation 自证全 12 子句**:U1/U2/U3/U4/U5/U6 + A1/A2/A3/A4/A5/A6 逐一 neutralize→对应 readiness 测**变红**,还原→**全绿**。readiness 谓词现**每条子句都有反向 mutant 判别**,任何未来回归必被捕获。
- 本轮 **tests-only**(readiness 代码 c21ae65d 未动=已正确);118 测(117 passed + 1 advisory skip);红线零改;diff --check clean。
- 元教训沉淀:缩小判定域的重构(遍历全体→遍历子集)有**成对陷阱**——子集为空是新 fail-open 面(§15b),且新子句易漏判别测试(§15c)。终局做法=**判定谓词每条子句配一个"仅该子句触发"的隔离测试 + 全子句 mutation 自证**,别只测"典型 ready/unavailable"。[[feedback_test_must_follow_code_change]] [[feedback_expose_weakness_over_pretend_pass]]

## 16. 第四轮外部集成复审 NO-GO(2 P1 代码 + 1 P1 集成门禁)· 全修 · 129 测绿 0 skip

第四轮复审判 NO-GO,逐条返工(全部按"消费 AI-1 真实产物 + 补判别 + mutation 自证"):

- **P1#1 readiness 只按 platform_key 匹配,未按精确 surface**:策略选表面 X(不可用)、健康只有 legacy 表面 → 竟 ready;策略选健康 legacy、同平台另一非选中表面不可用 → 竟 unavailable。修=按 **exact `(platform_key, surface_key)`** 匹配:`_enabled_surface_set(pv)` 取每个 enabled 平台的**选中**(平台,表面)对;`_admin_health_items.enabled_by_policy = _health_surface_key(h) in enabled_surfaces`;`missing_health`/`enabled_health`/`not enabled_surfaces` 全部改按 surface 对。**非选中表面既不替代选中表面(缺→missing→attention)也不拖累 readiness**。判别:selected-absent→attention / selected-down→unavailable / non-selected-down→ready(3 测)。DEFAULT_TEST_POLICY 补真实 per-platform surface_key(镜像 AI-2 policy seed)。
- **P1#2 未接入 AI-1 控制面 readiness**:仅读平台健康行,未消费 AI-1 的 sampling driver / reservation reaper 接线 / registry 表面替代 / scheduler 可运行。修=`ProductApiDeps` 加 **`collection_readiness_provider`**(集成者从 AI-1 `scheduler_wiring.check_readiness(){ready,problems}` + `registry.readiness(){status∈ready/attention_required/blocked}` 组合成整体注入;AI-3 定契约+消费,不 import AI-1 包=与 platform_health/policy 注入同构)。`_collection_readiness` fail-closed:未注入/异常/未识别形状→unavailable;`status` 权威(blocked→unavailable、attention→attention);仅 `ready` 形状 False→unavailable;**矛盾 `ready:False`+`status:ready`(接线但没跑)降级 unavailable**。折入 readiness:coll unavailable→整体 unavailable、coll attention→整体 attention。`AdminOverviewDTO` 加 `collection_readiness{status,problems}` 供管理员可见。判别 6 测(未注入/driver-reaper 未接/blocked/substitution→attention/probe 抛/矛盾降级)。
  - **⚠️ 集成接缝**:`collection_readiness_provider` 的"整体"组合由集成者用 AI-1 两函数(`services/ai_surface_monitoring/{scheduler_wiring,registry}.py`)完成——取 worst;AI-3 侧已 fail-closed 兜底任一原始形状。
- **P1#3 migration 漂移降级 advisory=门禁被绕过**:上一轮我把跨包漂移改 warn+skip(为避 AI-2 churn 致套件非确定),**但这掩盖了统一 release 门禁**。修=**恢复 hard-fail**(治理工作树在场→硬断相等;仅缺席时 skip);**重新 vendored 到 AI-2 最新治理快照**(SHA256 `5e7bfb88…`,含新增合规 + 金标准列);always-run 内容哈希锁保留 + 加漂移探测判别测试(`test_hash_lock_detects_migration_drift`:改一字节即 digest 变)。**最终统一 release 仍须待 AI-2 提交干净 SHA**→从提交 blob 重 vendored + bump pin + 全套重跑(已声明门禁依赖)。**教训纠偏**:advisory skip 规避 churn 非确定性 = 用 fail-open 掩盖真实 not-ready gate,与 readiness fail-closed 原则自相矛盾;门禁宁可非确定性变红也不可静默放行。
- **mutation 自证(新增)**:①surface-exact revert→platform-only:non-selected-drag / selected-absent 测变红;②去 `coll unavailable` 子句:collection-未接线测变红;③去 `coll attention` 子句:substitution 测变红——全 red,还原全 green。
- 全套 **129 测绿 0 skip** vs AI-2 真实(最新)migration;红线零改;`git diff --check` clean;OpenAPI 重生成(+CollectionReadinessDTO + AdminOverviewDTO.collection_readiness,标题/版本保持)。
- **本轮复审后再对抗审见 §16b**。

### §16b NO-GO#4 修复后对抗复审(5 lens · 会话限额中断)· 1 CONFIRMED(P2)全修 · 131 测绿

对 §16 delta(HEAD `a9c39d36`)跑 5 lens find→verify。**会话限额致 3 agent 中途失败**(surface-match finder + collection/regression 两 verifier),故自动复审**不完整**,不作 CONFIRMED=0 依据。已完成 agent 结果:drift-gate/test-discrim lens **零发现**;两个 finder **一致**报同一 P2;其余 verifier 未跑。

- **[CONFIRMED · P2 · 已修]** `_collection_readiness` fail-closed 契约有洞=**fail-OPEN 崩溃**:try 只裹 `provider()` 调用,后续 `raw.get(...)` 在 try 外。provider 返回真值**非 dict**(list/bool/int/str,如把 AI-1 两函数组成"problems-only"列表)或 `problems` 非可迭代(如 `{"status":"ready","problems":500}`)时 raise AttributeError/TypeError;`admin_overview` 未裹此调用(只裹 stuck 探针 try/finally)→ 整个 overview **HTTP 500**(counts/platform_health/readiness 全丢),违反本函数自述"unrecognized shape → unavailable"契约。修=整个 call+parse 裹进 try;非 dict 显式判别 unavailable;problems 非 list/tuple 强制成单元素;任何异常→unavailable。判别:非 dict provider(list/True/int/str)→200+unavailable(非 500);非可迭代 problems→200+status 照常;mutation 还原 parse-outside-try→非 dict 测 500 变红。
- **未覆盖 lens 手工自审**(会话限额致 surface-match finder 未跑):逐边界核 surface-exact——enabled 平台 surface=None(malformed)/health 缺 surface/非选中 surface unavailable/全平台 disabled/None-vs-None 碰撞——均 fail-closed(missing→attention 或 not-empty→unavailable),生产真实 surface 两侧齐备无 fail-open;admin 无条件见 surface_key。collection 真值表 14 形态逐一核(None/raise/{}/{ready}/{status}/矛盾/非 dict)全 fail-closed 正确。
- 131 测绿 0 skip;红线零改;diff --check clean。**因会话限额,自动复审未跑满一轮 CONFIRMED=0;修复+手工自审+mutation 自证已就位,建议下轮会话额度恢复后补一次完整 5-lens 自动复审再进统一合并。**

## 12. 明确声明

coded + local verified；**未部署 / 未迁生产 / 未翻配置 / 未 push**；开发 AI 无权宣布 release GO。前端赛马与集成接线由后续窗口/集成者按 `04`/`07` Gate 执行。

## 17. Deploy-CTO 独立复审补充（2026-07-18）

结论：**本包代码可进入统一集成，但当前分支仍是 NO-GO，不能单独部署。**

独立复审额外确认并修复 4 个 fail-closed 缺口：

1. enabled policy 与 health 同时缺少 `surface_key` 时，旧逻辑会让 `(platform, None)` 错误精确匹配并读成 ready；现要求 enabled 平台必须有唯一、非空的 `(platform_key, surface_key)`。
2. collection readiness 的 `ready` 字段曾接受 `0/1/"false"` 等非 bool 值；现一律降为 unavailable。
3. AI-2 policy provider 抛异常或返回畸形对象时，旧逻辑会让产品/admin API 500；现降为未接线策略，产品 fail-closed、admin readiness unavailable。
4. AI-1 platform health provider 抛异常或返回畸形对象时，旧逻辑会让 admin API 500；现 overview 返回 unavailable，独立 health 端点返回 `OBSERVATION_UNAVAILABLE`。

迁移门禁也改为读取固定 AI-2 commit `d604c7984b0e6c553395514fc0058668fd2c8942` 的 committed blob，不再依赖某台机器的 sibling worktree，也不允许缺席时 skip。独立验证：`136 passed / 0 skipped`，`py_compile`、`git diff --check`、红线 diff 均通过。

仍阻断统一 release 的事实：

- AI-1 `689fa1812430e7b682db5a12829250f149fdc9ef` 与 AI-2 `d604c7984b0e6c553395514fc0058668fd2c8942` 均不是本分支祖先。
- 本分支 `server.py` 尚未注册 product/admin router，`auth/module_mapping.py` 尚未登记 `/api/geo-observation/`，`default_deps()` 也没有注入 AI-1 health、AI-2 policy、AI-1 collection readiness 的真实组合器；这是明确的统一集成工作，不得宣称已接线。
- AI-2 governance worktree 在本次复审时仍有未提交的 migration 增量；最终集成必须等待其 clean commit，重新 vendored、更新 source pin，并在最终统一 SHA 上重跑全套。
