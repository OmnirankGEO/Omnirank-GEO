# AI-3 T0 项目理解门 · 聚合决策后端与产品 API 包

> 窗口：AI-3（唯一聚合后端 / 产品 API owner）
> 分支：`feat/geo-observation-analytics-2026-07-17`
> 工作树：`C:\AI-Test\omnirank-ai-geo-observation-analytics`
> 状态：`coded` 未开始（T0 阶段）；仅聚合后端/API，**不参与前端赛马**，前端由 Frontend-A/B 各自交付。
> 阅读完成：AGENTS.md、GEO_PROJECT_FULL_MANUAL/README + CURRENT、SYSTEM_TRUTH(05/08 present；README/00 本机不存在)、vNext README + 00_MASTER_SPEC + 机器契约 + 03 施工单 + 04 集成 Gate + 07 前端赛马 Gate + 06 外部审核裁定。

## 0. 基线与签发核对（硬门已满足）

| 项 | 值 | 证据 |
|---|---|---|
| DEVELOPMENT_START_SHA | `1fae96bdb6f4125c43fec892ffb8332c6ec8f897` | worktree HEAD；docs-only 合同指针 commit（deploy-cto "docs(geo): add frontend race and copy contract"） |
| SIGNED_RELEASE_BASE_SHA | `6059b0884421d6fd1a2c34ee57c19b98259c5bdc` | `git merge-base --is-ancestor <sha> HEAD` = 0（是 HEAD 祖先） |
| CONTRACT_FREEZE_COMMIT | `8daa1e53f85c78ee9c5b7769889e8d5aa3acfa7e` | 同上，是 HEAD 祖先 |
| worktree clean | `git status --short` 空 | 新建即 clean |
| observation_contract_v1.json SHA256 | `65C1..D5AA` == 冻结值 | 逐字节匹配 |
| frontend_copy_and_race_v1.json SHA256 | `0FF5..A0B6` == 冻结值 | 逐字节匹配 |
| frontend_race_fixture_v1.json SHA256 | `B5C4..F1B5` == 冻结值 | 逐字节匹配 |

三份合同文件哈希逐字节等于任务给定冻结值 → 我读取的是同一 Git blob，非本机可变文件。

## 1. OmniRank 为谁解决什么问题 · 三身份

OmniRank AI（全域上榜）是一个 **纯服务商端 SaaS**，帮助品牌在 AI 搜索引擎（豆包 / 千问 / DeepSeek / 元宝，历史含 Kimi）中提升"被推荐"可见度。核心链路：GEO 诊断 → 关键词/报价 → 写作 → 发布 → 监测 → 飞轮沉淀。

三身份（本包全部只读消费其身份，不改鉴权）：

- **服务商（agent，主用户）**：登录后操作全流程 9 stage，管理自己的客户/品牌，是本包"服务商监测工作台/数据洞察"的**主要读者**。只能看到自己被授权的品牌数据（brand/owner RBAC）。
- **普通用户 / 客户（C 端，链接接收方）**：不独立登录复杂 SaaS，通过现役受控报告/快照 token 查看**自己**的诊断结果。本包只在现役诊断报告里增加"AI 推荐行为"模块，不新造第二份报告、不新增 C 端一级入口。
- **管理员（admin）**：治理样本、平台矩阵、聚合质量、隐私审核。本包给 admin 一个**只读产品 API + 治理中心页面**（写操作调 AI-2 的 policy API，不由本包写）。

**用户定位铁律**：服务商 = 主用户；客户 = token-only 接收方。本包不把客户当独立 SaaS、不给客户设计操作面板。

## 2. 工厂式商业模型 · 平台直营 · 上游隐私边界

- 商业模型：算力销售 + 工厂进货 + 服务商定价 + 分润；本批 **不改** 计费、冻结、退款、钱包、库存、收益算法（`middleware/billing.py` 红线不读不改）。
- 平台直营：平台账号/上游供应商是平台内部资产。
- **上游隐私边界（本包最核心的隐私红线）**：客户 / 服务商 **不得看到上游**。对本包的落地含义 = 任何面向服务商/客户的 DTO **禁止**出现：`owner_user_id / agent_user_id / upstream_user_id`、`service_account_code(SV) / channel_account_code(CH)`、`cost_multiplier / internal_cost / provider cost / internal model route`、其他客户来源、`event/source PK`、`provider_trace_id`、价格系数与渠道关系。
- 供应商真名不给用户看：产品层把 `yuanbao_hy3_tokenhub` 显示"元宝"；`deepseek_metaso_proxy` 显示"DeepSeek 模型 + 秘塔检索代理"；`tencent_wsa_search` 显示"腾讯网页搜索能力"；技术 provider/model 只在 admin 健康页可见。

## 3. GEO 全链数据流 · 本包所处位置

```
建档(brands/profile) → 诊断(diagnosis_records/raw_data_json) → 关键词/报价 → 写作 → 发布
                                    ↘                              ↘
公共调研(geo_research_round) ──┐   持续监测(monitoring_*) ─────────┘
                               ▼
       [AI-2] 来源登记事件 geo_observation_events（本包只读）
                               ▼   异步处理器/终态核验/隐私清洗/品牌实体审核/晋升(processing_state=promoted)
       [AI-2] 匿名信号 geo_observation_signals + 受限 contributor_buckets（本包只读）
                               ▼
  ★[AI-3 本包]★ 可重放聚合 geo_observation_aggregates（AI-2 建表/readiness，AI-3 repository/公式/job/API）
                               ▼
       趋势 / 稳定度 / 竞争 / 来源 / 内容机会  →  唯一产品 API/DTO
                               ▼
       Frontend-A/B（消费同一 DTO/fixture/OpenAPI，盲赛）
```

本包 = 从**匿名信号**到**可解释可行动产品 API**的这一段。上游采集/隐私/晋升是 AI-1/AI-2；下游前端是 Frontend-A/B。我 **只读** events/signals/buckets，**只写** 自己独占的 aggregate repository + 产品 API。

## 4. 三来源终态、隐私与失败隔离原则（本包如何消费）

| 来源 | 现役表 | 允许晋升终态（AI-2 判定，我只读 promoted） | 隐私属性 |
|---|---|---|---|
| 公共调研 | `geo_research_round` | round `completed` + call success + 谱系完整 + 本轮 snapshot；resumable/失败不晋升 | 公共提示词，无客户绑定 |
| 付费诊断 | `diagnosis_records` / `raw_data_json` | 资金终态真实完成或明确 exempt + 结果 `published` + 非退款/released/withheld/manual 未决 + run_token 对得上 + 自定义问题先匿名化 | 客户私有；只有匿名规律才进公共层 |
| 持续监测 | `monitoring_*`（keywords/results/tasks） | task `completed` + 无引擎错误伪装成功 + 计费终态明确 + 实体非 UNKNOWN + 同品牌/题族/平台/自然日限一公共票 | 客户私有；`monitoring_query` 是商业 SSOT（逐字发题，本包不改采集） |

**失败隔离原则（I3）**：诊断/监测/调研主流程**不依赖飞轮可用性**。聚合/Redis/LLM 审核/本包处理器失败绝不能阻断客户已购交付。→ 本包所有产品 API 是**旁路只读**，503/失败时前端进真实错误/空态，不回落 fixture、不阻断主链。

## 5. 我独占的文件 · 必须复用的现役模块 · 禁止越界

**独占（新增）**：
- `services/geo_observation_analytics/**`（contract 常量、metrics 公式、aggregates repository+job、trends、opportunities、explain 语义引擎、privacy/k-anon、repository）
- `api/geo_observation_product_api.py`（私域/公共/admin 只读产品 router，输出可注册 router，不自行改 server.py）
- `schemas/geo_observation_product.py`（Pydantic DTO，私域/公共/admin 分离，`extra="forbid"`）
- `tests/geo_observation_analytics/**`（真 PG + 并发 + 隐私 + RBAC + 语义 request-body 契约测试）
- 冻结 OpenAPI / DTO / race fixture 副本 + 契约测试

**必须复用（不推倒重做，T0 逐条修正 + 回归证据，详见 §9 复用地图）**：
- 现役 `frontend/src/pages/Insights/InsightsCenter.*`、`api/distillation_api.py`、`db/distillation_db.py`（产品壳与兼容输入，六个现役口径缺陷需修正——但**前端由候选实现**，我只保证后端 DTO 能支撑修正后的口径）
- `db/connection.py` 连接/游标获取（只读复用，不改）
- 现役 brand/owner RBAC helper、admin RBAC 依赖、current-user 依赖（只读复用）
- 现役官方 DeepSeek v4-flash provider/helper（复用；若不能保证官方 base_url+精确 model，停下回报接口缺口，不另造密钥系统）
- 现役 `_StrictDTO(extra="forbid")` DTO 隐私分层范式（`schemas/public_contracts.py`）
- 现役 `geo_observation_policy`（AI-2 独占）与 `platform_health_fields`（AI-1 只读健康）——我只**展示**，不写

**禁止修改（越界即淘汰）**：AI-1 provider adapter/round runner/采样器；AI-2 migration/source hook/隐私/promotion/audit/policy 写 API；`middleware/billing.py`；`db/connection.py`；`auth/middleware.py`；`auth/jwt_utils.py`；`server.py`；`api/scheduler.py`；`api/monitoring_api.py`（归最终集成者）；`frontend/src/App.tsx`；全局 Sidebar/nav。**不新建业务 migration、不临时加列、不在前端补算指标。**

## 6. 资金 / RBAC / DB / 调度 / WORKERS 红线 · 为何本包不能拖垮付费主链

- **资金**：本批零改资金。付费诊断作为来源时只**只读核验**现役终态（AI-2 做），本包连 billing 表都不直接读——只读 AI-2 已晋升的 `promoted` 事件。`middleware/billing.py` 是扣费+退费核心红线。
- **RBAC**：私域端点复用现役 brand/owner RBAC，**不能只信 brand_id**；公共端点走 k-anonymity + cell 抑制；admin 端点走 admin 依赖。`auth/middleware.py`/`jwt_utils.py` 不改。
- **DB**：只 SELECT events/signals/buckets（promoted）；只对自己独占的 `geo_observation_aggregates` 做 upsert（AI-2 建表）。请求内禁 DDL；不 DROP/TRUNCATE/RENAME。SQL 四维核验（列名/类型/归属/dry-run）。
- **调度**：聚合 refresh job 交最终集成者按 ROLE=cron 单控制面注册；本包只提供纯函数 job，不自注册 cron、不碰双 scheduler。
- **WORKERS=4**：聚合 claim/重算必须幂等——同一 input watermark 幂等重放，20 并发聚合恰一有效版本（DB 键/CAS，不靠 Redis）。当前生产 WORKERS=1、PROGRESS_BUS 未启用只是运行开关事实。
- **为何不能拖垮主链**：本包是旁路只读观测产品；任何失败 fail-soft，主链诊断/监测/调研/写作/报告/结算零感知。

## 7. 当前签发 Base / 合同 commit / 生产开关事实 / 本包不负责的内容

- 签发 Base = `6059b088`（clean release，active blue 镜像锚定）；合同冻结 = `8daa1e53`；开发起点 = `1fae96bd`。
- 生产开关事实：active blue，WORKERS=1，PROGRESS_BUS_READY 未启用；`GEO_OBSERVATION_*` 系列 flag 与元宝 Hy3、Kimi 默认退出为本批目标态，**尚未部署/未开**。
- **本包不负责**：前端赛马与视觉实现（Frontend-A/B）；observation 业务 migration 与 readiness（AI-2）；provider adapter/采样/health 探针（AI-1）；policy 写 API/CAS/审计/epoch（AI-2）；部署/迁生产/翻 flag/宣布 release GO（Deploy-CTO / Codex）。
- 交付口径只能声明 `coded + local verified`。

## 8. 机器契约要点固化（我实现的唯一真相）

- `contract_version = geo-observation-contract-v1.4`；`metric_version = geo-observation-metrics-v1`（与冻结 fixture 一致）。
- 十分类 `target_outcome`（唯一 SSOT，旧七类只按 `legacy_outcome_mapping` 迁移，不写新数据）：recommended / conditionally_recommended / candidate_only / mentioned_only / criteria_only / refused_no_evidence / refused_risk / not_mentioned / entity_ambiguous / engine_error。
- **有效观测分母纪律**：valid = 十分类中排除 `entity_ambiguous` 与 `engine_error`（其余 8 类均为有效产品行为，分别统计）；UNKNOWN/ambiguous/timeout/provider_error/隐私拒绝/withdrawn 一律不进负面分母。null 不当 0。
- 聚合字段 = 机器契约 `aggregate_schema.fields` 逐项（scope/owner/brand/industry、day|week|month 桶、题族/意图/品牌词、平台/表面/model_revision/检索/来源、样本与独立 buckets、十分类计数、率/rank/SoV/波动/趋势/model_shift、记忆与检索双层、版本与 watermark）。
- `scope_type=public_industry` 的 DTO 禁含 owner/brand/aggregate_key；小样本 k 匿名 + cell 抑制。
- 聚合 eligibility = join event 只取 `processing_state='promoted'`；withdrawn/private_only/rejected/error 永不聚合；signals 不可变；撤回靠 event→withdrawn + 下一轮重算，不改/删 signals。
- 权重：全目录 base weight 可合计 10000；运行时只对 enabled+有权益平台确定性重归一到 10000；每 event 记录原始/归一权重 + policy_version + metric_version。per-brand cap = `max_single_brand_share_bps`。
- 数值硬门：public privacy leaks=0；withdrawn 贡献=0；public cell 有效观测≥10、独立 user bucket≥3、独立 brand bucket≥3、来源类型≥2；同 input+同 metric_version 重跑漂移=0；未解释新旧 shadow 漂移 >500bps 阻断。
- 语义 AI（DeepSeek 官方）：`provider=deepseek`、`base_url=https://api.deepseek.com`、`model=deepseek-v4-flash`、交互式 `thinking=disabled`；只消费匿名最小事实包；浏览/筛选/分页零 LLM；同 input 幂等；失败保留确定性图表显示"AI 洞察暂不可用"，禁回落硬编码。禁止经 DashScope/代理冒名官方、禁写 `deepseek-chat/deepseek-reasoner` alias。
- 产品 DTO 业务语义须与 `frontend_race_fixture_v1.json` 兼容；用户端标准业务中文，禁 `bps/watermark/outcome/surface_key/policy_version/metric_version/HMAC/k-anonymity/fanout` 等黑话直接暴露。

## 9. 复用地图（rg / 调用链 / schema / 前端入口）

> 本节基于工作树 `6059b088`（DEVELOPMENT_START 的代码父）的实证扫描填充。SSOT 总册/SYSTEM_TRUT/README 实际在主仓 `C:\AI-Test\omnirank-ai`（本仓 6059b088 未含该 doc），代码复用点在本仓可核。禁止在未读代码前新建第二套观测中心/指标/数据账本。

### 9.1 DB 连接与游标（只读复用，禁改 `db/connection.py`）

- `get_connection()` → `db/connection.py:153`（池化，游标工厂强制 `RealDictCursor`，行为 dict：`row["col"]`；每次 checkout `autocommit=False` 必须显式 commit）。DDL/建表须先 `conn.autocommit=True`。
- `get_db()` 上下文管理器 → `db/connection.py:197`（自动 commit/rollback/close）。
- 主流 idiom：`conn=get_connection(); try: cur=conn.cursor(); cur.execute("... %s ...",(p,)); rows=cur.fetchall(); finally: conn.close()`。占位符 `%s`（psycopg2），参数元组。
- 测试连接补丁点：`db.connection.DATABASE_URL = TEST_DATABASE_URL` + `close_pool()`（见 `tests/admin_user_governance/conftest.py`）。**我独占 repository 用同一 `get_connection`，不新建连接层。**

### 9.2 RBAC / brand 归属（只读复用，禁改 `auth/middleware.py`）

- 当前用户：`request.state.user`（dict：`user_id/username/is_admin/roles/permissions/client_brand_ids/perm_version`，注入于 `auth/middleware.py:344`）。本地 helper idiom `_get_user(request)` 见 `api/brand_api.py:21`。
- **brand 归属核验（不能只信 brand_id）**：`require_brand_access(request, brand_id, allow_null=False)` → `auth/brand_access.py:34`（admin 放行；否则 brand_id ∈ `user["client_brand_ids"]` 或 user == `brands.owner_user_id`，`_is_brand_owner`@:12；否则 403 `X-Error-Code: BRAND_ACCESS_DENIED`）。
- 列表 scope：`get_user_brand_filter(request)` → `auth/brand_access.py:196`（admin=None 无过滤 / list / 未分配 `[-1]` fail-closed）。资源级：`require_diagnosis_access`@:93 / `require_report_access`@:111 / `require_quote_access`@:129 / `require_profile_access`@:143。
- admin 守卫：全局中间件 admin short-circuit `auth/middleware.py:312`；端点级 `user.get("is_admin")`（`api/brand_api.py:669`）。
- **🔴 集成硬依赖（禁我改 · 交集成者）**：新前缀 `/api/geo-observation/` 必须加入 `auth/module_mapping.py` `ROUTE_PREFIX_MAP`（`:16`）为 `("/api/geo-observation/", None)`（= 需登录、无模块 perm、端点自守 brand scope，同 `/api/marketing/`@:33、`/api/pricing/`@:49），否则中间件对非 admin 全 403 `UNMAPPED_ROUTE`。admin 前缀 `/api/admin/geo-observation/` 未映射即对非 admin 403（正确），admin short-circuit 放行。第二层数据隔离网 `auth/middleware.py:396-414` 只读 query `?brand_id=` → 端点级 `require_brand_access` 仍必需。

### 9.3 API router 约定（我新增 router，禁改 `server.py`）

- 声明：`router = APIRouter(prefix="/api/...", tags=["..."])`（`api/review_api.py:18`）。请求模型 Pydantic `BaseModel+Field`；本包响应用 Pydantic `response_model` **直接返回 DTO**（对齐 fixture `cases.*.response` 无 `{success}` 包裹）。
- 错误：`raise HTTPException(status_code, detail=...)`；本包 detail 用 `{"code","message"}`（对齐 fixture `error_cases`：FORBIDDEN/VERSION_CONFLICT/ENV_OVERRIDE_ACTIVE/OBSERVATION_UNAVAILABLE/INSUFFICIENT_SAMPLES + 中文 message；HTTP 403/409/423/503/422）。
- 分页：`page`/`page_size`（`api/brand_api.py:660,719,763-765`，含 `total`）为 GEO 读 API 主流，本包 questions 端点采用之。
- 注册（**交集成者**）：`server.py:596-608` `try: include_router(...) except ImportError: warn`。本包输出可注册 router + 明确接线说明，不自行 include。

### 9.4 DeepSeek 官方语义调用（复用 key 池，不另造密钥系统）

- 现役官方通道：`tools/multi_llm_caller.py:31-38`（PROVIDERS[0]=`https://api.deepseek.com/v1/chat/completions`，`model=deepseek-v4-flash`，`extra_params={"thinking":{"type":"disabled"}}`，`env_key=DEEPSEEK_API_KEY`）；`call_llm_with_fallback`@:137。
- **⚠️ 接口缺口（回报）**：`call_llm_with_fallback` 官方优先但**失败/缺 key 静默回退 DashScope**（PROVIDERS[1]@:39-46），违反本批"禁经 DashScope 冒名官方"。→ 本包 explain.py **绕过 fallback**，严格只走官方：复用 `config/model_config.DEEPSEEK_CONFIG`（`:150-165`，base_url `https://api.deepseek.com/v1`）+ `tools/social_operator/deepseek_key_pool.pick_deepseek_api_key(role)`（复用现役 7-key 池，非新密钥系统）+ `writing/llm_utils.get_thinking_disabled_params(url,model)`（`:130` → deepseek-v4-flash 返回 `{"thinking":{"type":"disabled"}}`）。transport 可注入以便测试截获 request-body。
- 记录 `provider=deepseek/model=deepseek-v4-flash/thinking/prompt_version/schema_version/input_hash`。官方不可用保 UNKNOWN + "AI 洞察暂不可用"，不回退第三方。

### 9.5 源表 schema（AI-2 喂入，我 **只读** events/signals/buckets；不直接读源表）

- 我的聚合只 join `geo_observation_events`(promoted) × `geo_observation_signals` × `geo_observation_contributor_buckets`（AI-2 建表）。**不直接查** `diagnosis_records`/`monitoring_results`/`geo_research_round`——它们是 AI-2 的登记来源。以下仅为理解上游语义：
  - `diagnosis_records` `db/diagnosis_db.py:51`（`raw_data_json TEXT`@:110、`run_token`@:261、`result_visibility` NULL|published @:262/269）；资金终态在 `diagnosis_runs`（`scripts/migration_diagnosis_runs_2026_07_13.sql`，`billing_mode paid|exempt`、13 态 run_status、`completed_exempt`；**只读只 join，禁写 · billing 红线**）。
  - `monitoring_results` `db/monitoring_db.py:377`（`is_detected SMALLINT 0/1`、`platform`、`full_response`）；`confirmed_keywords.monitoring_query` `db/diagnosis_db.py:376`/col@:3804（商业 SSOT，采集逐字发题 · AI-1/集成者域，我不碰）。
  - `geo_research_round` `db/diagnosis_db.py:1391`（`status ... completed`、`snapshot_json`、`last_heartbeat_at`）。
- **我 RBAC/聚合需读**：`brands`（`owner_user_id`@`db/diagnosis_db.py:116/124`，active 过滤 `(is_deleted IS NULL OR is_deleted=FALSE)`）。private_brand 聚合 group by `events.owner_user_id/brand_id`（events 私有列）+ signals 指标；public_industry group by `industry_key` 且 DTO 无 owner/brand。
- **现役影子基准 `geo_engine_stats`**（`db/diagnosis_db.py:1006-1022`，写方 `services/placement_service.py:2156` + `round_runner.py:1997`，读方 publish/flywheel/c_end）——本批 `geo_observation_aggregates` **只作影子，不替换、不改**它。

### 9.6 调度（禁改 `scheduler.py` / `api/scheduler.py`；我只出纯函数）

- 双 BackgroundScheduler：根 `scheduler.py`（`job_daily_monitoring`@:136 → `run_detection_for_keyword`@`api/monitoring_api.py:2343`）+ `api/scheduler.py`（`setup_schedule`@:265）。leader 门 `cron_should_fire()`（`scheduler.py:42-50`/`api/scheduler.py:92-104`）+ 每 job `sched_claim(name,ttl)`。
- 本包只提供纯"聚合 refresh"函数（幂等、DB claim 友好），**交最终集成者**按 ROLE=cron 单控制面注册；不自注册 cron、不碰双 scheduler、不改监测取题。

### 9.7 现役产品壳（保留 · 不推倒 · 前端由候选改；我出正确后端 DTO 支撑修正口径）

- 洞察壳：`frontend/src/pages/Insights/InsightsCenter.tsx`（4 tab radar/patterns/sources/roi，调 `/api/insights/*`）；后端 `api/distillation_api.py`（prefix `/api/insights`，`{status,data}` 包裹，RBAC `require_brand_access`，注册 `server.py:802`）；DB `db/distillation_db.py`（表 `keyword_insights` upsert `(task_id,keyword)`、`brand_aliases`）。
- **六现役口径缺陷**（`03` §8 · 我 **不改** 旧 distillation，改由新 `/api/geo-observation/*` 正确 DTO 承接，胜出前端消费）：
  1. 竞品雷达只靠 LLM `brands_found`（`db/distillation_db.py:280/297`）+ 前端硬编码噪声词（`InsightsCenter.tsx:291-294`）→ 新 DTO 走 signals 统一 resolver + 十分类 outcome。
  2. "心智份额"误当市场份额（`InsightsCenter.tsx:308` 标题 / `db/distillation_db.py:336` `share_pct`）→ 新 DTO 命名"样本内品牌提及占比"+口径。
  3. 成功模式给全平台加票（`distillation_api.py:81-92` 对 `platforms_analyzed` 全部 +1）→ 新聚合按平台/表面真实命中分层。
  4. citation fallback 混入 citation_rate（`distillation_api.py:242-290` 同池；`placement_service.py:2262-2271` 单 rate）→ 新 DTO `citation_rate` 与 `source_visibility_rate` 分列，fallback 明示不入 citation_rate。
  5. ROI 前后窗不分 model/surface/metric_version（`db/distillation_db.py:403-474` 忽略 `llm_model/algorithm_version`）→ 新聚合按连续性分段 + `model_shift_index`。
  6. `dds_patterns.md` 无门写入（`distillation_api.py:_save_patterns_file:564-584` 每次 synthesize 覆盖，喂 `writing/article_writer.py:1892`）→ 本批只在满足样本/隐私/稳定度/metric_version 门后才允许反哺（本包只输出机会与门控信号，不自动写 dds）。
- 前端路由入口（**交集成者/胜出前端**）：`/monitoring` 原地增强、`/insights`+诊断报告原地增强、admin 新增 `/admin/geo-observation-center`；不新增客户一级入口。

### 9.8 禁止新增点（红线）

不新建：第二套观测中心 / 第二套指标公式（前端不得复制 TS 公式）/ 第二套数据账本（不新建业务 migration、不临时加列，聚合表由 AI-2 建）/ 第二套资金·报价·身份·品牌 SSOT / 第二套 DeepSeek 密钥系统 / 第二条飞轮。已存在即复用：连接层、RBAC、DTO 隐私范式、DeepSeek key 池、`geo_engine_stats`（只作影子不替换）。

## 10. 本包不做（非目标）

不重写 FastAPI/React；不新建第二套资金/报价/身份/品牌 SSOT；不自动把诊断结果公开给其他客户；不因 Kimi 历史而默认调用；不把 WSA 伪装成元宝 App 完整答案；不在客户请求里同步跑脱敏/晋升 LLM；不让 LLM 算指标/定 RBAC/k 匿名/执行写作发布/替代确定性聚合；不自动写作/发布/投放/改媒体订单/扣费。
