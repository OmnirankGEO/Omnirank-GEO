"""
迁移清单单一权威源(SSOT · WORKERS=4 返工 P1-5)
================================================
server.py(dev/无 ROLE 导入期跑)与 scripts/prestart.py(生产 ROLE=prestart 单飞跑全量)共用此清单,
避免两处清单漂移。生产拓扑:ROLE=prestart 单飞 advisory-lock 跑完全部迁移 → fleet(web/cron)起时**跳过**
导入期迁移(不再每 worker 并发执行 DDL/backfill · 蓝绿重叠期不再撞库)。dev(无 ROLE)仍在 server 导入期跑。

顺序即依赖顺序 · 全部幂等(IF NOT EXISTS / 幂等 DO 块 / fail-closed 预检)。
"""

MIGRATIONS = [
    "scripts/migration_v3_2_c_end.sql",
    "scripts/migration_v3_2_commissions.sql",
    "scripts/migration_v3_2_trial_passes.sql",
    "scripts/migration_v3_3_managed_campaign.sql",
    "scripts/fix_billing_2026-04-14.sql",
    "scripts/migration_m3_first_batch.sql",
    "db/migration_010_narrative_locks_budget.sql",
    "scripts/migration_pricing_v2_2026_06_11.sql",
    "scripts/migration_media_cost_snapshot_2026_06_13.sql",
    # writer generation trigger 会直接引用 pricing_catalog_version；首次整包部署
    # 必须先由 dual-SSOT 补齐订单快照列，再建数据库 writer fence。
    "scripts/migration_pricing_dual_ssot_2026_07_12.sql",
    # 再补 system_settings.updated_at 与库存扩宽，随后执行依赖该列的 008/full-fix。
    "scripts/migration_agent_inventory_bigint_cutover_2026_07_14.sql",
    "db/migration_008_pricing_llm_first.sql",
    "scripts/migration_pricing_full_fix_2026_06_13.sql",
    "scripts/migration_ai_ops_center_2026_07_01.sql",
    "scripts/migration_marketing_center_2026_07_04.sql",
    # GEO 内容中心的付费图片 attempt 需要区分未发送/响应未知/已获 task id，
    # 并在 provider 成功与本地资产物化之间保留可恢复结果锚。运行时会无条件
    # 读取这些列，因此必须由 prestart 在 web/worker 就绪前完成。
    "scripts/migration_geo_provider_attempts_2026_07_21.sql",
    # 晒成交草稿是表单/语音/图片三路输入与后续内容包请求之间的耐久锚；
    # request_id 唯一约束承担幂等，运行时会无条件读写该表，
    # 因此必须由 prestart 在 web/worker 就绪前完成。
    "scripts/migration_marketing_deal_drafts_2026_07_22.sql",
    # 组织撤权必须传导到晒成交草稿(2026-07-23 外部审查 P1-1):组织身份创建
    # 的草稿绑定 organization_id/created_by_membership_id,读取做 live 复核。
    # 只加 nullable 列与部分索引,个人身份草稿行(NULL)行为不变。
    "scripts/migration_marketing_deal_drafts_org_binding_2026_07_23.sql",
    # 调研轮基础表 → 自助调研队列/别名/CHECK → 并发与自动续跑 seed。
    # 开关首发及缺配置均 fail-closed=false。
    "scripts/migration_geo_research_monitor.sql",
    "scripts/migration_geo_research_selfserve_2026_07_05.sql",
    "scripts/migration_research_round_concurrency_autoresume_2026_07_16.sql",
    # Stage3 URL 预算依赖调研基础表/配置；必须由生产 prestart 单飞建表，
    # 禁止等到全量轮运行时才因缺表失败。
    "scripts/migration_stage3_url_budget_2026_07_16.sql",
    # 已执行过 07-16 版本的库不会重跑旧文件；独立增量负责把 nullable
    # industry_name 收敛为唯一的空字符串口径，冲突时 fail-closed。
    "scripts/migration_stage3_url_budget_industry_name_2026_07_18.sql",
    # geofix 最终链：先扩结算/补偿 schema，再做有完整备份的重复工单折叠，
    # 最后建立 exactly-once 唯一键及 SKU tombstone。全部只由 prestart 单飞执行。
    "scripts/migration_v5_geo_plan_settlement_2026_07_13.sql",
    "scripts/migration_v6_dispute_escrow_2026_07_13.sql",
    "scripts/migration_v6_fund_recovery_2026_07_13.sql",
    "scripts/migration_v7_fund_recovery_fencing_2026_07_13.sql",
    "scripts/migration_v7_geoplan_settle_conflict_index_2026_07_13.sql",
    "scripts/migration_v9_fund_recovery_kind_widen_2026_07_13.sql",
    "scripts/migration_v10b_dedup_channel_revenue_with_backup_2026_07_13.sql",
    "scripts/migration_v10_channel_revenue_exactly_once_2026_07_13.sql",
    "scripts/migration_v11_agent_sku_override_deleted_at_2026_07_13.sql",
    # 服务商零售 SKU 独立于平台进货模板；历史 fallback 先冻结为显式 SKU，
    # 再开放 template-free 写入。运行时仍只认已发布 retail catalog/quote/snapshot。
    "scripts/migration_agent_retail_sku_decoupling_2026_07_17.sql",
    # 定价管理源 → 已发布目录/持久报价接线：依赖 dual-SSOT 表及 SKU tombstone。
    # 只加 provenance、不可变/引用/唯一约束；不翻任何 pricing flag。
    "scripts/migration_pricing_quote_wiring_2026_07_14.sql",
    # 经销商逐跳库存转售：只建 additive schema + seed 独立 flag=false；不猜历史成本/不翻闸。
    "scripts/migration_dealer_inventory_resale_2026_07_15.sql",
    # 消费者退款只反向直属服务方原订单；资金不足进负结算/耐久工单；协议证据与订单绑定。
    "scripts/migration_direct_service_refund_agreements_2026_07_15.sql",
    # 完整链 JIT/促销归属：依赖直属退款 case/ledger，增加多 hop 子表；不改旧订单、不翻闸。
    "scripts/migration_dealer_jit_resale_2026_07_17.sql",
    "scripts/migration_workers4_scheduling_2026_07_13.sql",
    "scripts/migration_diagnosis_runs_2026_07_13.sql",
    # 用户身份/商业绑定/平台权限治理：仅加 CAS 版本与不可变审计，不回填生产关系。
    "scripts/migration_admin_user_governance_2026_07_15.sql",
    # 扩展服务商渠道与密码安全治理域；只扩 CHECK 白名单，不回填关系或密码数据。
    "scripts/migration_admin_user_governance_extensions_2026_07_19.sql",
    # 关键业务通知：业务终态同事务写 outbox，cron leader exactly-once 派发 user_notifications。
    "scripts/migration_notification_outbox_2026_07_17.sql",
    # 组织内部席位依赖现役用户、品牌、定价目录与管理员治理对象；仅扩展
    # 组织/RBAC/额度/审计 schema，不启用任何组织或文章 feature flag。
    "scripts/migration_organization_internal_seats_2026_07_20.sql",
    # 所有正常激活账号可创建治理态团队；邀请开户与基础免费席位仍由版本化
    # 商品发布和独立 onboarding flag 控制，默认不发消息、不启用员工邀请。
    "scripts/migration_organization_all_accounts_onboarding_2026_07_22.sql",
    # 平台 ADMIN 跨租户治理、演示客户流程预览授权与服务商降级向导。
    # 只增加审计/授权/计划事实，不迁移客户、不写钱包、不接管商业资产。
    "scripts/migration_admin_cross_tenant_governance_2026_07_21.sql",
    # 本次报价系数只缩放现有客户售价并冻结 append-only 快照；报价/客户/写作项目
    # 的归档恢复列均按 quote_id/brand_id 写入，不启用价格或组织功能开关。
    "scripts/migration_quote_snapshots_and_archives_2026_07_21.sql",
    # Preserve row-level provenance for the retired Yuanbao monitoring default.
    # The product-matrix migration below restores only proven legacy rows to
    # classic4; paid-diagnosis Yuanbao and immutable history stay untouched.
    "scripts/migration_monitoring_yuanbao_default_2026_07_20.sql",
    # 持续监测恢复经典四路，并把 confirmed 短句绑定到版本化商品矩阵。
    # 仅精确恢复上一迁移写入的默认值；自定义配置不覆盖。
    "scripts/migration_monitoring_product_matrix_2026_07_21.sql",
    # 监测品牌身份 UNKNOWN 作为可恢复待确认格原子落库；人工决定有 CAS 与 append-only 审计。
    "scripts/migration_monitoring_identity_review_2026_07_21.sql",
    # 2026-07-22 板块 A：诊断人工确认复用监测人工确认 SSOT 的 additive 泛化
    # （source_kind/source_result_id/tenant_id/ip/reason + result_id DROP NOT NULL）。
    # 直接依赖上一迁移的 events 表形状，须紧随监测族其后；全部幂等 + fail-closed 形状核验。
    "scripts/migration_diagnosis_identity_review_2026_07_22.sql",
    # 词×平台完整执行计划、原履约覆盖凭证与单格幂等重试；不改价格/余额/flag。
    "scripts/migration_monitoring_cell_retry_2026_07_21.sql",
    # 可选扣费幂等：仅传 idempotency_key 的 deduct_points 调用启用；
    # 记录与余额/流水同事务提交，未传键保持历史路径。
    "scripts/migration_billing_deduction_idempotency_2026_07_19.sql",
    # GEO 统一观测飞轮 vNext · 唯一观测 migration。AI-2 治理账本之外，同文件追加
    # AI-1 跨 worker 成本账本及 AI-3 洞察任务表；所有开关默认 false，不改业务数据。
    "scripts/migration_geo_observation_v1_2026_07_17.sql",
    # 明确采集所有权模式；默认复用现役诊断/监测/调研并由 reconciler 导入，
    # 不注册第二套 provider collection jobs，不翻任何观测 feature flag。
    "scripts/migration_geo_observation_collection_mode_2026_07_20.sql",
    # 聚合口径使用稳定 policy basis(排除仅展示的 product bit)，刷新终态以
    # manifest 完整证明；旧 NULL-basis 聚合永不进入产品读取。
    "scripts/migration_geo_observation_aggregate_basis_2026_07_20.sql",
    # GEO article v1.4 depends on the research, monitoring and publication
    # tables created above. Prestart must finish this before web/cron starts.
    "scripts/migration_geo_article_v14_2026_07_19.sql",
    # Quote -> writing -> publish compatible sidecar.  This remains additive
    # and all runtime flags default false; historical quotes/topics stay NULL.
    "scripts/migration_geo_article_closed_loop_v1_2026_07_20.sql",
    # Stable article-task projection plus append-only full-reset audit. Existing
    # billing/refund recovery remains authoritative; no flag is enabled here.
    "scripts/migration_article_generation_task_state_2026_07_21.sql",
    # 2026-07-22 板块 C：白标作用域纠正（D1 审计/版本化 + D2 backoffice 独立授权 + D3
    # 到期/撤权回落）。只加列/审计表/append-only 触发器，既有 oem 授权按三条件映射、
    # ID 132 固定 customer-only；UPDATE 为同值重写，可连跑。置文件尾：依赖运行时
    # init_referral_tables 已建的 whitelabel_settings 基表，不阻塞前置迁移链。
    "scripts/migration_whitelabel_backoffice_scope_2026_07_22.sql",
    # 老板授权共享钱包 payer policy SSOT：仅 additive 新表/新列 + append-only 审计。
    # 老组织全部默认关闭，不 broad UPDATE、不猜测历史 consent、不翻任何 feature flag。
    # 置文件尾：依赖组织内部席位冻结 schema（organizations/charge_links 基表）。
    "scripts/migration_organization_payer_policies_2026_07_23.sql",
    # [WP6] 用户名式邀请:把 organization_invites / organization_operator_accounts 的
    # target_kind CHECK 由 (phone,email) 放宽为 (phone,email,username)。纯扩枚举域、
    # 幂等(DROP IF EXISTS + 无条件 ADD)、counts 不变。运行时会真的写入 'username' 行,
    # 且**无运行时兜底放宽函数**,因此必须由 prestart 在 web/worker 就绪前完成——
    # 漏登记会让生产 CHECK 停在 (phone,email),用户名邀请 INSERT 直接 CheckViolation。
    # 置文件尾:依赖上述组织席位/开户迁移已建好这两张表。
    "scripts/migration_organization_username_invite_2026_07_25.sql",
    # [三线合并 2026-07-26 Review-CTO] WP12 与诊断专线各自在文件尾追加,
    # 三条迁移互不依赖,全部保留:先诊断两条(依赖 matrix_2026_07_21),后 WP12。
    # [P0-2] 诊断与监测统一五引擎(Owner 2026-07-26 裁决)。additive:只追加
    # monitoring-unified5-v1 矩阵行 + 改四处列默认值,不 UPDATE 任何存量 platforms /
    # monitoring_product_version(已售监测按下单快照继续履约)。运行时
    # ``assert_monitoring_product_matrix_ready`` 会**无条件**核对新版本行与新默认值,
    # 漏登记会让监测入口直接 fail-closed —— 必须由 prestart 在 web/worker 就绪前完成。
    # 置于 matrix_2026_07_21 之后:依赖它建好矩阵表、FK 与 append-only 触发器。
    "scripts/migration_monitoring_unified5_2026_07_26.sql",
    # [P0-3] 提及词表归一:direct / llm_verified_fallback → mentioned(纯重命名,
    # 绝不升成 recommended)。应用层 services/mention_vocabulary.py 双向都认，
    # 所以漏跑不会崩,只是老行在报告里仍走兼容分支;登记进 manifest 保证一次收口。
    "scripts/migration_mention_vocabulary_2026_07_26.sql",
    # [WP12 P2-6] 两周写作有效性复核报告存档表。纯 additive 新表 + 两个索引,
    # 不改业务数据、不翻 flag、不写文体配比。cron 与 admin API 都会无条件读写
    # 这张表,漏登记 manifest 会让生产 INSERT 直接 UndefinedTable。
    # 置文件尾:不被任何前置迁移依赖。
    "scripts/migration_writing_effectiveness_report_2026_07_26.sql",
    # [P0 代发卡单出口 · 生产线 1cae0f74 已上线] awaiting_sync 反查计次 + 用户声明列。
    # 运行时(scheduler 扫描器/出口端点)会**无条件**读写 awaiting_sync_probe_attempts /
    # user_exit_claim,漏登记会让 72 小时出口扫描直接 UndefinedColumn 并被 job 的
    # except 吞掉。纯 additive(三列 + 一个部分索引),不动钱、不翻 flag、可连跑。
    "scripts/migration_mhz_awaiting_sync_exit_2026_07_26.sql",
    # [服务商控制客户线上购买开关 2026-07-29] 两级开关的两根列:
    # users.allow_client_online_purchase(NOT NULL DEFAULT TRUE)+
    # customer_agent_bindings.online_purchase_override(NULLABLE 三态)。
    # 运行时 services/client_purchase_gate.py 会在每个支付发起端点**无条件**读这两列,
    # 漏登记 manifest 会让充值/订阅下单直接 UndefinedColumn。判定层对读失败 fail-open
    # (不拦)以保资金入口可用,所以漏跑的表现是"开关静默失灵"而不是报错——更须登记。
    # 纯 additive、默认值=开、不改存量行,可反复连跑。置文件尾:不被任何前置迁移依赖。
    "scripts/migration_client_purchase_gate_2026_07_29.sql",
    # [P0 性能 2026-07-28] mhz_media.media_name 的 pg_trgm GIN 索引。
    # recommend-v2 对每个候选平台打一条 `media_name ILIKE '%kw%'`(实测 433 条),
    # 前导通配符让既有 btree 用不上 → 每条 Seq Scan 52,072 行 → 单请求 44 秒;
    # 而生产 WORKERS=1,这 44 秒里整台服务器停止响应(同屏接口全 504)。
    # 建索引后 41.8s → 2.7s,推荐结果逐条一致(只改执行计划,不改查询与排序)。
    # 生产已用 CONCURRENTLY 手工建过,此处 IF NOT EXISTS 是 no-op;登记是为了让
    # 重建库/新环境不会静默丢索引把 44 秒的坑复发。置文件尾:无前置依赖。
    "scripts/migration_mhz_media_name_trgm_2026_07_28.sql",
    # [P0 收口 2026-07-28] 团队短代码 organizations.short_code / short_code_locked
    # + organizations_short_code_unique 部分唯一索引 —— 对既有**自愈式 DDL**的补登记。
    # services/organization_short_code.py::ensure_schema 把这三条 DDL 写在业务代码里
    # (懒初始化:不跑到那条代码路径就不建列),潜伏数日后于 2026-07-28 首次被真实用户
    # 触发,当场改表 → 绕过本清单 → 绕过 services/organization_schema_contract.py 的
    # 目录指纹 → 部署就绪门与 server.py 容器启动门**双 fail-closed**,新容器一律起不来。
    # 生产已是该形态(契约变体 production_reanchor_short_code_v1 已签),故本迁移在生产
    # 是 no-op;**登记的意义是让新库/重建库确定性获得这个形态,而不是靠谁碰巧调用**。
    # 置文件尾:不被任何前置迁移依赖(只依赖组织席位迁移已建好 organizations 基表)。
    "scripts/migration_organization_short_code_2026_07_28.sql",
    # [P0 收口 2026-07-28 · 同批] actor-artifact 表的 brand_id 补登记。
    # publish_orders.brand_id / diagnosis_records.brand_id 都在 organization catalog
    # 指纹的 ACTOR_ARTIFACT_COLUMNS 里,此前却只由 db/publish_db.py 与 db/diagnosis_db.py
    # 的启动期自愈 DDL 建立,而且两处都包在裸 `except Exception: pass` 里 ——
    # 一旦吞掉真实失败,列悄悄没建、指纹漂移,容器启动门 fail-closed 且日志无痕。
    # 生产三列早已存在,故为 no-op;登记是为了让 prestart 确定性建立,
    # 不再依赖 server.py 的导入顺序。置文件尾:不被任何前置迁移依赖。
    "scripts/migration_actor_artifact_brand_id_2026_07_28.sql",
    # [P0-A 可观测 2026-07-29] registration_agreement_gate_events —— 协议门禁触发计数。
    # 2026-07-26~29 缺协议账号被 428 挡在门外 3 天且**零信号**,只能靠 Owner 自己撞上。
    # 记录器是 best-effort(写失败只记日志,绝不影响登录判定),因此漏跑本迁移的表现是
    # "管理端计数恒为空",不会造成登录故障。纯 additive,可反复连跑。
    # 置文件尾:不被任何前置迁移依赖。
    "scripts/migration_agreement_gate_observability_2026_07_29.sql",
    # [返修 2026-07-29] 库存对账 inventory_audit_runs 加失败态(status/error_message)
    # 与新等式列(agent_total_frozen / wallet_total / ledger_total),并把 int4 快照列加宽。
    # 起因:cron 里 `allocated_out` 未定义 → NameError → except Exception 只 log
    # → 对账连续静默死亡、没有任何一行落库。失败态列就是为了让"程序自己死了"
    # 落得下来、比"对上了"更响。纯 additive + 幂等 + fail-closed 自验。
    # 置文件尾:只依赖 W4 已建好 inventory_audit_runs 基表。
    "scripts/migration_inventory_audit_equation_2026_07_29.sql",
    # [通知中心 2026-07-29] user_notification_read_marks —— 「扣费通知」通道的已读水位
    # 表 + user_notifications 的 (user_id, created_at DESC, id DESC) 复合索引。
    # 扣费通知是 point_transactions 的**只读投影**(工单铁律:一行扣费逻辑都不许碰),
    # 已读状态没地方落 → 另立纯 UI 状态表存水位,和资金/对账等式完全无关。
    # 漏跑的表现是"扣费通知恒为未读"(读取方 best-effort 降级),不是报错。
    # 置文件尾:不被任何前置迁移依赖(user_notifications 由通知 outbox 迁移建好)。
    "scripts/migration_notification_center_channels_2026_07_29.sql",
    # [按需铸造第一步 2026-07-29] dealer_resale_fulfillment_allocations 的
    # allocation_kind 追加 'manufacturer_mint'(平台厂家根跳按需铸造的供给来源)。
    # 只追加分支、原样保留 existing_lot / jit_incoming 两支,并带形状守卫 + 存量行前置核验
    # + 后置逐值核验(照抄旧迁移的枚举会静默删掉在用分支,同类事故已发生过)。
    # 🔴 漏跑这条 = 平台跳落 allocation 时违反 CHECK → 支付回调整笔失败,不是静默降级。
    # 不含 manufacturer_origin_out(那是第二步 T3 存量销毁,单独交付)。
    # 置文件尾:只依赖 JIT 迁移已建好的 fulfillment 子表。
    "scripts/migration_ondemand_minting_2026_07_29.sql",
    # [短视频开闸包 T5 · 2026-07-30] mhz_short_video_drafts 补 article_type / image_urls 两列
    # (图文笔记模式的素材落点 + 图文模式的重复提交拦截键)。
    # 建表口径在 db/meijiehezi_db.py 是 CREATE TABLE IF NOT EXISTS —— 对存量表 no-op，
    # 所以存量库的这两列只能靠本迁移建立；漏跑 = 短视频下单 INSERT 报 column does not exist
    # (响亮失败，不是静默降级)，且迁移自带后置自验会让漏跑在 prestart 就非零退出。
    # 纯 additive + 幂等 + 可反复连跑。置文件尾：不被任何前置迁移依赖。
    "scripts/migration_svideo_image_mode_2026_07_30.sql",
    # [发布门三态拆分 P1 · 2026-07-31] geo_article_review_events 补 before 态一列
    # (§4.3 人审跳过留痕的 operator/time/reason/before/after 五字段里唯一缺的那个)。
    # 🔴 漏跑 = set_human_review 的 INSERT 报 column does not exist → 人工签发/跳过
    # 整条失败(响亮失败,不是静默降级);发布门判定本身不读该列,**不受影响**。
    # 纯 additive + 幂等 + nullable 无默认无约束,可反复连跑。置文件尾:无前置依赖。
    "scripts/migration_publish_gate_tristate_2026_07_31.sql",
    # [核验流融合包① · 2026-08-01] geo_article_publication_notice_audits ——
    # 内容类硬门降级为提示级后,"带未处理提示仍点了发布"的静默留痕表(§3A)。
    # 🔴 写入是 best-effort(SAVEPOINT 包住,失败只 warning),所以漏跑的表现是
    # "留痕恒为空"而不是发布报错 —— 恰恰是最该登记的一类:本仓 07-29 协议门禁
    # 缺表零信号 3 天就是同型。纯新表 + 幂等 + 无 FK,可反复连跑。
    # 置文件尾:不被任何前置迁移依赖。
    "scripts/migration_publication_notice_audit_2026_08_01.sql",
    # [核验流融合包① · 2026-08-01 · §3E] geo_article_ai_review_conclusions ——
    # AI 内容审核结论逐篇留痕(reviewer/正文 hash/结论摘要),按
    # (article_id, content_hash, reviewer) 唯一 = 幂等口径的**落库实现**。
    # 🔴 批跑脚本与前端核查入口都会**无条件**读写这张表,漏跑 = UndefinedTable
    # (响亮失败,不是静默降级)。纯新表 + 幂等 + 无 FK,可反复连跑。
    # 置文件尾:不被任何前置迁移依赖。
    "scripts/migration_article_ai_review_2026_08_01.sql",
    # [策略/绑定包② · 2026-08-01 · §3F] geo_strategy_ai_review_reports ——
    # 写作策略版本的 AI 评估报告。另起一表是因为 writing_strategy_versions.reviewed_by
    # 是 **BIGINT**(用户 id 列),塞不下 'ai:deepseek-chat@…' 这种 reviewer 串。
    # 评估端点会无条件读写本表,漏跑 = UndefinedTable(响亮失败)。
    # 纯新表 + 幂等 + 无 FK,可反复连跑。置文件尾:无前置依赖。
    "scripts/migration_strategy_ai_review_2026_08_01.sql",
    # [GEO 抖音图文 v1 · 2026-08-02 · Deploy 补登记] ——
    # 🔴 交付时这两条【不在清单里】,而 prestart 只按清单跑(不 glob 目录),
    #    等于「文件在仓库里、永远不会执行」:生产实测 geo_douyin 表 0 张、
    #    feature_pricing 65 条无本条目。后果不是功能惰性 ——
    #    `GET /posts` 等 9 条未过总闸的路由会直接打不存在的表 → 500。
    # 017:建 geo_douyin_posts + geo_douyin_post_tasks。全 IF NOT EXISTS,
    #     唯一 FK 是 tasks→posts(同文件内、同序建),无外部依赖 → 可置尾。
    # 018:把 Owner 拍板的制作费(260 算力 = ¥2)写进 feature_pricing。
    #     ON CONFLICT (feature_code) DO UPDATE,幂等;is_active 列默认 true,
    #     故首次 INSERT 不写该列也是生效的(已查 information_schema 确认,不是推断)。
    #     依赖 feature_pricing 已存在 —— 该表早于本清单,生产实测 65 行。
    # 四维核验 + dry-run(BEGIN→017→018→查→ROLLBACK)已在生产库实跑通过:
    #     建表 2 · 价目条 pts=260 compute=2.00 paidonly=false active=true · 65→66;
    #     ROLLBACK 后反向对照回到 0 表 / 65 条。
    # 🔴 rollback_017_*.sql 是【手工回滚脚本】,绝不登记(登记 = 上线即把表删了)。
    "db/migration_017_geo_douyin_posts_2026_08_01.sql",
    "db/migration_018_geo_douyin_pricing_2026_08_02.sql",
    # [双供应商择优路由 · 2026-08-02] media_provider_equivalence(同媒体等价映射)
    # + mhz_media/mhz_wemedia.hidden_by_dedupe(目录去重,与 is_active 分开)
    # + mhz_publish_order_items.routed_provider/routed_media_id/routed_cost_yuan。
    # 🔴 后三列漏跑的后果最重:status_sync._open_items() 会 SELECT routed_media_id,
    #    列不存在 = UndefinedError(响亮失败);而若只加表不加列并放行路由,则改道去
    #    快易播的单永远扫不到状态 → 24h 后被误判未发布退款,稿却已发出 = 既亏钱又亏交付。
    # 纯 additive + 幂等(IF NOT EXISTS / ON CONFLICT DO NOTHING),可反复连跑。
    # 置文件尾:不被任何前置迁移依赖。
    "scripts/migration_media_provider_routing_2026_08_02.sql",
    # 019:Owner 08-02 二次调价 —— 制作费 260→390(对齐 article_gen 390/3.00),
    #     并新增「重新生成」档 geo_douyin_image_post_regen 260/3.00
    #     (对齐 article_rewrite 260/3.00)。两条都 ON CONFLICT DO UPDATE,幂等。
    #     生产已有 260 那行 → 走 UPDATE 语义;dry-run(BEGIN→019→再跑一次→回滚脚本→ROLLBACK)
    #     已在生产库实跑:260/2.00 → 390/3.00 + 新增 regen 260/3.00,重跑不变值,
    #     回滚退回 260/2.00 且 regen 置 is_active=false;事务外复查生产仍是 260。
    #     必须排在 018 之后(它 UPDATE 的正是 018 建的那行)。
    # 🔴 rollback_019_*.sql 同样【绝不登记】(登记 = 上线即把调价改回去)。
    "db/migration_019_geo_douyin_pricing_390_2026_08_02.sql",
    # 020:详情页交互补全的四个字段(redraw_count / style_key / contact_enabled /
    #     closing_stale)。纯 additive,全 ADD COLUMN IF NOT EXISTS + 一条
    #     redraw_count >= 0 的 CHECK(建在 pg_constraint 存在性判断里,可重复跑)。
    #     必须排在 017 之后(它 ALTER 的正是 017 建的那张表)。
    #     🔴 redraw_count 是「重抽 10 次上限」的**唯一权威计数** —— 漏跑这条:
    #        `_bump_redraw` 的 UPDATE 会打 UndefinedColumn(响亮失败,不是静默放行),
    #        详情页打不开。宁可响亮挂掉也不要"上限静默失效 = 无限白嫖生图 API"。
    # 🔴 rollback_020_*.sql 同样【绝不登记】(登记 = 上线即把字段删了)。
    "db/migration_020_geo_douyin_detail_ui_2026_08_02.sql",
    # 021:按张数计价(Owner 08-03)。新增两个 feature_code —— 每张加价
    #     geo_douyin_image_post_extra_card 100、单张重抽 geo_douyin_image_post_redraw 100。
    #     纯 INSERT ... ON CONFLICT DO UPDATE,幂等,不依赖前置迁移的表结构
    #     (feature_pricing 是既有表),但排在 019 之后以保持价目类迁移的时间序。
    # 🔴 漏跑这条的后果**分两种,都不是静默**:
    #     - 加张下单:pricing.extra_card_points 抛 PricingUnavailable → 下单失败并
    #       告诉用户"价目暂时不可用",**不会**变成"多做 5 张但只收 4 张的钱";
    #     - 单张重抽:端点同样 fail-closed 拒绝,**不会**退回免费白嫖生图 API。
    #    即:漏跑 = 功能不可用(响亮),不是资金漏(静默)。这是刻意选的方向。
    # 🔴 rollback_021_*.sql 同样【绝不登记】(登记 = 上线即把本次调价撤掉)。
    "db/migration_021_geo_douyin_card_pricing_2026_08_03.sql",
    # 022:抖音「被采纳内容」语料表(选题蒸馏器的 few-shot 来源)。
    #     纯建表 + 建索引,全 IF NOT EXISTS,不依赖任何前置迁移。
    # 🔴 漏跑的后果:蒸馏器读表抛 UndefinedTable → 走既有降级路径
    #    (改用 geo_research_source_signals 的 caption,形态混合)并**如实标注降级**,
    #    功能不中断、也不会假装 few-shot 来自图文帖。同样是响亮而不是静默。
    # 🔴 表建好了**也还是空的** —— 灌数据是离线运维动作
    #    (scripts/research/douyin_corpus_ingest.py,调 TIKHUB 约 ¥0.0072/条),
    #    不在部署链上。别把"建表成功"当成"语料就绪"。
    # 🔴 rollback_022_*.sql 同样【绝不登记】。
    "db/migration_022_douyin_adopted_corpus_2026_08_03.sql",
    # 023:AI 一键蒸馏选题定价 130(Owner 08-03 拍板)。纯 INSERT ... ON CONFLICT,
    #     幂等,不依赖前置迁移(feature_pricing 是既有表)。
    # 🔴 漏跑这条的后果是**响亮不可用,不是白送**:端点用 charge_on_success 包着,
    #    进入上下文时 check_balance_only 就要读价目 → 读不到直接抛 → 蒸馏报错。
    #    刻意选这个方向:一个读库瞬时故障绝不能变成"LLM 随便调、一分不收"。
    # 🔴 rollback_023_*.sql 同样【绝不登记】(登记 = 上线即把本次定价撤掉)。
    "db/migration_023_geo_douyin_topic_distill_pricing_2026_08_03.sql",
    # 024:画幅自选(规范 §8.8)。ADD COLUMN IF NOT EXISTS + DEFAULT '3:4',幂等。
    # 🔴 漏跑这条的后果是**响亮的**:get_post 的 SELECT 列表里有 aspect_ratio,
    #    列不存在 → UndefinedColumn → 详情页打不开。不会静默变成"全都按 3:4"。
    #    宁可响亮挂掉也不要"一组里混着两种画幅"这种只有肉眼能发现的错。
    # 🔴 rollback_024_*.sql 同样【绝不登记】(登记 = 上线即把这一列删了,且不可逆)。
    "db/migration_024_geo_douyin_aspect_ratio_2026_08_03.sql",
    # R3 · 手动词(extra_keywords.platforms)存量口径清洗 —— 与其挂靠 quote 的商品矩阵
    #     对齐。必须排在 migration_monitoring_product_matrix_2026_07_21.sql 与
    #     migration_monitoring_unified5_2026_07_26.sql **之后**(它要读那两条建立/追加的
    #     monitoring_product_platform_matrices 行);放在清单末尾天然满足。
    # 🔴 本条**刻意不碰任何列默认值**:那四处默认值被 unified5 那条迁移尾部的 DO 块
    #    逐字断言,而 prestart 每次部署全量重跑清单 —— 在这里改默认值,下次部署那条
    #    断言必炸('unified5 monitoring default contract drift')→ prestart 非零退出 →
    #    候选不启动,**两槽都起不来**。要动默认值必须和那条迁移同批改。
    # 🔴 漏跑这条的后果是**静默的**(这是本清单里少见的方向,所以写清楚):
    #    代码侧 R1+R2 已经让新词口径正确、且让 cells 层永不再炸,所以漏跑不会挂;
    #    但**存量那条写歪的行会一直带着五引擎快照**,监测面板上它和同 quote 的合同词
    #    看起来引擎数不一致(执行结果一致,展示口径不一致)。属"看着别扭"不属"炸",
    #    因此没有设成 fail-closed —— 但也正因为静默,漏跑不会自己冒出来,要靠部署单核。
    "scripts/migration_monitoring_extra_keyword_entitlement_2026_08_04.sql",
    # 026:媒体域名人话目录(WO_MEDIA_BOARD_UX_CLOSURE §1)。纯建表 + 建索引 + 补列,
    #     全 IF NOT EXISTS,不依赖任何前置迁移。
    # 🔴 漏跑的后果是**静默的**(所以写清楚):读侧 `get_directory_entries` 对
    #    UndefinedTable 走 fail-soft 返空目录 → 榜照常出、点击照常、只是长尾域名
    #    继续显示裸域名 = 回到修复前的样子。刻意选静默方向:一次建表没跑成
    #    绝不能让代理连媒体榜都打不开。反过来说,漏跑不会自己冒出来,要靠部署单核。
    # 🔴 本表**只服务展示层**,不进任何评分/排序/报价口径 —— 回滚就是删表,零业务残留。
    "db/migration_026_media_domain_directory_2026_08_05.sql",
    # 025:一键蒸馏选题改异步(WO-DISTILL-TIMEOUT-ASYNC-2026-08-05)· 建任务表 + 部分唯一索引。
    #     纯建表建索引,全 IF NOT EXISTS,幂等,不依赖任何前置迁移。
    # 🔴 漏跑的后果是**响亮的**:提交蒸馏时 INSERT 抛 UndefinedTable → 端点 500 →
    #    前端拿到人话失败。刻意选响亮:这条链上静默失败 = 用户干等,比报错更糟。
    # 🔴 那条部分唯一索引 uq_geo_douyin_distill_inflight 是**防重复扣费的 DB 兜底**:
    #    改异步后请求 2 秒即返回,进程内锁若跟着请求释放,连点两下就是两次 130。
    #    漏跑它 = 兜底没了(进程内锁还在,但 WORKERS>1 时失效)。
    # 🔴 rollback_025_*.sql 同样【绝不登记】。
    "db/migration_025_geo_douyin_distill_tasks_2026_08_05.sql",
    # 027:交付闭环通电 + 品牌实体档案(WO_DELIVERY_FLYWHEEL_CLOSURE_2026-08-06 §2.1/§2.2)。
    #     纯建表 + 建索引 + 两条 CHECK,全 IF NOT EXISTS,幂等,不依赖任何前置迁移
    #     (只引用 brands.id)。**不动 brands 任何一列**。
    # 🔴 漏跑的后果是**静默的**(所以写清楚):归因同步 job 对 UndefinedTable 走 fail-soft
    #    记 0 行 + 落心跳 detail,飞轮回写退回原 geo_research 路径 = 回到修复前的恒 0 样子。
    #    刻意选静默:归因是观测面,不能让它挡住发布/监测/计费主链。漏跑不会自己冒出来,
    #    要靠部署单核 —— 部署后跑文件尾部那两条 to_regclass 确认。
    # 🔴 回滚 = 直接 DROP 这三张表,零业务残留(不写任何既有表)。
    "db/migration_027_delivery_flywheel_closure_2026_08_06.sql",
    # 028:服务期双钟分裂根治(WO_SERVICE_PERIOD_SSOT_2026-08-06 §1.2)。
    #     回填 + `ALTER COLUMN service_days SET NOT NULL` + 四条 COMMENT ON COLUMN。
    # 🔴 顺序无依赖:只动 quotes 既有列的可空性与注释,不建表不改默认值,可排在清单任何位置。
    # 🔴 漏跑的后果是**响亮的**(刻意选的方向):代码侧已把 `COALESCE(service_days,365)` 全部删净,
    #    真有 NULL 行时读点会显式跳过并告警 —— 而不是像修复前那样静默按 365 糊出一口假钟。
    # 🔴 回填在生产预期 **0 行**(2026-08-06 只读实测 392/392 行非空),它存在只是为了让
    #    这条迁移在测试库/新库也能把约束立住。
    # 🔴 无 rollback_028:撤销就是把列改回可空(`DROP NOT NULL`),而那等于把兜底洞重新挖开,
    #    不该由部署清单自动做。真要回退请人工执行并同步回退代码。
    "db/migration_028_service_period_ssot_2026_08_06.sql",
    # 029:P4 缺口作战计划支撑迁移(WO_P4_GAP_PLAN_DEV_2026-08-08 · R6 + Owner 08-08 裁定合并)。
    #     两段:①media_outlets 加 4 列(entry_assessment/verified_at/verified_by/entry_note)
    #           ②缺口作战计划 5 张新表(gap_plan_snapshots/items/publications/checkbacks
    #             + gap_assistant_audit)
    #     纯 additive:不改任何既有列的类型/可空性/默认值,不 DROP,不 UPDATE 既有行。
    #     全 IF NOT EXISTS + DO $$ 包 CHECK,幂等,不依赖任何前置迁移。
    # 🔴 顺序无依赖:只碰 media_outlets 既有表的"加列",其余全是新表,可排在清单任何位置。
    # 🔴 漏跑的后果是**响亮的**(两段都是,刻意选的方向):
    #    ①缺列 → 任务卡"怎么发"读 entry_assessment 抛 UndefinedColumn → 交付计划区块报人话错误;
    #    ②缺表 → 交付计划端点返错误合同(snapshot_unavailable + 重新获取)。
    #    选响亮不选静默的理由:这是**新增能力**,静默返空会让运营看到"这个客户没缺口"——
    #    一句假结论比一句"暂时取不到"坏得多。影响面被限制在这个新区块内,
    #    报价页其余部分 / 写作中心 / 小榜既有能力全部不受影响。
    # 🔴 与全黑的 geo_article_* closed-loop 机器**零交集**:本迁移一行都不写它的表
    #    (2026-08-08 生产实测那 4 张表全 0 行 + ARTICLE_PLAN_* flag 全 False)。
    # 🔴 无 rollback_029:回滚 = DROP 那 5 张新表 + DROP 那 4 列。新表 DROP 零业务残留;
    #    但 DROP 列会丢掉运营已录入的核实结果,不该由部署清单自动做,真要回退请人工执行。
    "db/migration_029_gap_operation_plan_2026_08_08.sql",
    # 030:客户星标 + 发布下单备注(WO_CUSTOMER_FEEDBACK_4ITEMS_2026-08-09 ③②)。
    #     纯 ADD COLUMN IF NOT EXISTS ×3 + 一条部分索引,幂等,不动既有列/默认值/约束。
    # 🔴 顺序无依赖:只引用 brands / mhz_publish_orders 两张自古就有的表,可排清单任何位置。
    # 🔴 漏跑后果**两条方向不同**(迁移文件头写了完整理由):
    #    ③ 星标是**响亮**的(列表 SQL 直接 SELECT 该列 → /api/my-clients 500,整页打不开);
    #    ② 备注是**静默**的(写入包在 try 里,退回"确认后重发不带备注")。
    #    刻意不统一方向:客户列表宁可当场炸给部署看,发布主链绝不能被观测性列挡住。
    # 🔴 rollback_030_*.sql **绝不登记**(登记 = 上线即删列)。
    "db/migration_030_brand_star_2026_08_09.sql",
    # 031:媒体上架档位独立字段 mhz_media.listing_slot(WO_VOCAB_CONVERGENCE_2026-08-09 §3-L5)。
    #     纯 additive:加一列 + 一条 CHECK + 一个部分索引,全 IF NOT EXISTS / DO $$ 守卫,幂等,
    #     不依赖任何前置迁移,**不动任何既有列的值**(Owner 批 (c) 叠加标记)。
    # 🔴 漏跑的后果是**响亮的**(刻意选的方向):写入侧四个 INSERT 都明确带 listing_slot 列,
    #    列不存在 → UndefinedColumn 抛出 → 媒体同步整批失败并告警。
    #    选响亮不选静默:静默失败 = 列加了但一行没落上,而"已经拆完了"的假象比同步挂掉更难发现
    #    (本仓「加列 ≠ 拆完三步」的前车之鉴)。
    # 🔴 回填是**另一步**:本迁移只建结构,存量 1229 行由
    #    scripts/backfill_media_vocab_2026_08_09.py 单独回填(带 --dry-run,默认不写)。
    #    只跑迁移不跑回填 = 新同步的行有 listing_slot、老行全 NULL,消费侧按 NULL 走原路径,不炸。
    # 🔴 rollback_031_*.sql 同样【绝不登记】。
    "db/migration_031_media_listing_slot_2026_08_09.sql",
    # 032 · 投放结果事实表(报价返工前置)。两张只存计数、不存任何比率的派生表。
    # 🔴 顺序无依赖:纯新建表,不动任何既有表的任何一列,可排在清单任何位置。
    # 🔴 漏跑的后果是**静默的**:builder 遇缺表走 fail-soft 记 0 行不抛(事实表是观测面,
    #    绝不能挡住发布/监测/计费主链)。所以部署后必须跑迁移文末 §验收 那两条 SQL 确认表在。
    "db/migration_032_publication_outcome_facts_2026_08_09.sql",
    # 033 · 短视频草稿记住来源 geo_post_id(WO_P1_SVIDEO_BRAND_MISATTRIBUTION_2026-08-10)。
    #     纯 ADD COLUMN IF NOT EXISTS + 一条部分索引,幂等,不动任何既有列。
    # 🔴 顺序无依赖:只引用 mhz_short_video_drafts 一张既有表,可排清单任何位置。
    # 🔴 漏跑后果**响亮**(刻意):写入侧 INSERT 显式带该列 → UndefinedColumn 抛出、
    #    短视频下单当场失败。不选静默 —— 「以为已经能追溯了」比下单挂掉更难发现。
    # 🔴 存量 3 行**不回填**:其中 2 条是错归属,回填等于把错的确认下来;
    #    订正是另一件事(备份 + dry-run + Owner 过目),不由迁移代拍。
    "db/migration_033_svideo_draft_source_2026_08_10.sql",
    # 034 · 服务商分销链路(WO_INVENTORY_POINTS_DEADLOCK_2026-08-12)。
    #     只 CREATE TABLE IF NOT EXISTS 两张新表 + 索引 + DO $$ 守卫的 CHECK,
    #     **不动任何既有表的任何一列、不动任何既有 CHECK**
    #     (特别是 agent_inventory_transactions_type_check —— 工单红线)。
    # 🔴 顺序无依赖:只 FK 到 users,可排清单任何位置。
    # 🔴 漏跑后果**响亮**(刻意):admin 库存调整/代划拨/库存自用三个端点
    #    都无条件 INSERT agent_inventory_admin_actions,缺表 → UndefinedTable 抛出、
    #    整笔资金动作回滚。选响亮不选静默 —— 「钱划了但没留痕」正是这次事故的形态。
    "scripts/migration_inventory_distribution_chain_2026_08_12.sql",
    # [P1-4 引擎定向 2026-08-14] quotes.target_engine 一列(写作项目级目标引擎)。
    #     纯 ADD COLUMN IF NOT EXISTS,无数据 UPDATE,幂等;运行时另有
    #     db/diagnosis_db._safe_add_column 兜底防漏(双保险,同 user_choice 先例)。
    # 🔴 顺序无依赖:只碰 quotes 一列,可排清单任何位置。
    # 🔴 漏跑后果**静默且安全**:读侧 SELECT * 拿不到列 → topic.target_engine 空
    #    → 生成走"不定向"现行为(A1 空值降级),lineage 如实记空,不炸不硬编。
    "scripts/migration_quotes_target_engine_2026_08_14.sql",
    # [P1-3 媒体形态分档 2026-08-14] media_domain_directory.media_form 一列
    #     (portal_site/platform_account/vertical_media/self_site;NULL=未分档)。
    #     纯 ADD COLUMN IF NOT EXISTS + DO $$ 守卫 CHECK,无数据 UPDATE,幂等。
    # 🔴 顺序无依赖:只碰 026 建的表;026 已在清单上方,同批重放顺序天然正确。
    # 🔴 漏跑后果**静默且安全**:读侧 COALESCE 空 → 渲染层按"未分档"走
    #    fail-closed(不产出形态化署名),出榜/发布主链零影响。
    "scripts/migration_media_form_2026_08_14.sql",
    # [R2-2 2026-08-15] 历史 outcome 回填旧值账本(纯建表建索引,幂等)。
    # 🔴 漏跑后果**响亮**(刻意):回填脚本无条件写账本,缺表 → 整批失败 ——
    #    「改历史不记旧值」不允许静默发生。
    "scripts/migration_monitoring_outcome_backfill_journal_2026_08_15.sql",
    # [R2-7 2026-08-15] media_form 种子分档:高频域名按已审核载体映射
    #    (verified_domain_carriers.json,R7 人工核过)派生;只填 NULL,幂等,
    #    admin 订正(source='admin')优先级不受影响。
    # 🔴 漏跑后果**静默且安全**:同 media_form 列缺失形态 —— 未分档走旧路径。
    "scripts/migration_media_form_seed_2026_08_15.sql",
    # ── [merge 2026-08-17] 以下两条来自监测大车(8f8bd101,已在生产跑过):
    #    035→036 有显式顺序依赖,保持相邻;与上方本包四条互不引用同表。
    # 035 · 手动监测词纳入开关体系,默认关(WO_MONITORING_OPTIN_DEFAULT_OFF_2026-08-15 · P0-A.1)。
    #     只 ADD COLUMN IF NOT EXISTS 三列到 extra_keywords,**不动任何既有列、不含任何 DML**
    #     (prestart 无条件重放全部迁移 → 迁移里的数据 UPDATE 会每次部署把人工关掉的词重新打开)。
    # 🔴 顺序无依赖:只引用 extra_keywords 一张既有表,可排清单任何位置。
    # 🔴 漏跑后果**响亮**(刻意):取词 SQL 显式引用 ek.is_monitored → UndefinedColumn 抛出、
    #    监测取词当场失败。选响亮不选静默 —— 「以为默认关了其实照跑照扣」正是本次事故形态。
    # ── 🪦 TOMBSTONE:backfill_monitoring_extra_optin_2026_08_15.py 已删除 ──────────
    # [WO_MONITORING_PLATFORM_COVERED v1.2 · P0-0 · 2026-08-16]
    # 原文这里写着「存量 6 个真客户在跑的词不在这里回填,必须紧接着跑那个脚本」。
    # **那句话现在是有害的**:它会指挥后来者去跑一个已经被新裁决取代的脚本。
    #
    # 为什么删而不是改守卫:
    #   1. 一次性回填**已完成使命**(2026-08-15 已执行);
    #   2. 它的守卫校验「289/662 的 active 手动词恰好 6 条」——
    #      🔴 **校验的是目标集合,不是目标状态**。而 Owner 2026-08-16 已裁定把这 6 条
    #      关列对齐(Deploy 已执行,含审计痕迹)⇒ 人工再跑一次 `--apply`
    #      会**推翻 Owner 的决定重新打开它们**,而守卫不会拦(集合没变,变的是状态);
    #   3. 给一个无人依赖的死脚本升级守卫 = 给要扔的东西镀金。
    #      判据同 A-1 删除先例(2026-08-16 monitoring-optin 复审)。
    #
    # 引爆条件:一旦有人按上面那句旧注释去跑 `--apply`,这 6 条手动词会被重新打开,
    #          次日起按 130/词/天 从服务商钱包扣费,而 Owner 明确关掉了它们。
    # 如需重建:`git log --diff-filter=D -- scripts/backfill_monitoring_extra_optin_2026_08_15.py`
    # 复活即红:tests/platform_covered_2026_08_16/test_p0_0_dead_script_removed.py
    # ─────────────────────────────────────────────────────────────────────────────
    "scripts/migration_monitoring_extra_optin_2026_08_15.sql",
    # 036 · 订阅表加来源维度(WO_MONITORING_MANUAL_KEYWORD_PARITY_2026-08-16 · P0-1)。
    #     ADD COLUMN keyword_source + CHECK + 两条唯一索引重建(先建新后删旧),**零 DML**。
    # 🔴 顺序有依赖:必须排在 035 之后 —— 本单让手动词也建订阅,而 035 才给
    #    extra_keywords 加上 is_monitored 开关列;035 没跑时本单的取词两臂会 UndefinedColumn。
    # 🔴 漏跑后果**响亮**(刻意):list_active_subscriptions 显式引用 s.keyword_source
    #    → UndefinedColumn 抛出、每日取词当场失败。选响亮不选静默 ——
    #    「手动词订阅 JOIN 到另一个客户的合同词上去扣钱」是这一列要防的事故形态。
    "scripts/migration_kms_keyword_source_2026_08_16.sql",
    # 037 · 订阅表加付款方维度 billing_mode(WO_MONITORING_PLATFORM_COVERED §2)。
    #     ADD COLUMN + CHECK + 部分索引,**零 DML** —— 存量 96 行靠 DEFAULT 'brand_owner'
    #     落值即正确,老行为一字不变(P0-4 的 brand owner 覆盖是加分支不是改默认)。
    # 🔴 顺序无依赖:只引用 keyword_monitor_subscriptions 一张既有表。
    # 🔴 漏跑后果响亮(刻意):计费主体解析显式读 billing_mode → UndefinedColumn 当场抛出。
    #    「以为记平台账、实际扣了服务商」是本单要消灭的形态,不能让它静默发生。
    "scripts/migration_kms_billing_mode_2026_08_16.sql",
    # 034 · GEO 图文创作与发布 · 合同身份/幂等/逐项资金 additive schema
    #     (WO_GEO_IMAGE_NOTE_EXECUTION_2026-08-17 · 规格 02 §3/§13)。
    # 🔴 编号 034 排在 037 之后不是笔误:本仓编号是**血缘序号**不是执行序号,
    #    执行序号由本清单的**位置**决定(文件头「顺序即依赖顺序」)。034 是 geo_douyin
    #    017-025 血缘的下一号(025 之后 GEO 图文域一直没加过迁移),而它依赖的
    #    mhz/media_publications 域在前面已就绪,故排队尾。
    # 🔴 全文件 additive 零 DML(工单 §2 禁区):无 UPDATE/DELETE/INSERT,
    #    全部 IF NOT EXISTS,历史行留 NULL。prestart 无条件重放安全。
    # 🔴 不含 geo_article_* 六表:dormant slot sidecar 的激活要先过窄 RFC
    #    (docs/AI-CONTEXT/RFC_GEO_IMAGE_NOTE_SLOT_SIDECAR_ACTIVATION_2026-08-17.md),
    #    Review 未批前零改动 —— 工单 §1.4「批了才许动,直接升格 = NO-GO」。
    # 🔴 漏跑后果响亮(刻意):新链入口的 schema readiness
    #    (services/geo_douyin_schema_contract.assert_schema_ready)显式检查这些列,
    #    未就绪时新 route 返回 503 SCHEMA_NOT_READY 且零 claim/零资金/零订单/零 provider;
    #    legacy route 不受影响(规格 02 §13 明令不得挂进无条件 FLEET_SCHEMA_GUARDS,
    #    否则一个新 lane 没就绪会把整站拒启)。
    "db/migration_034_geo_image_note_contract_2026_08_17.sql",
    # 035 · GEO 图文交付槽位渠道化(窄 RFC 已获 Review-CTO 2026-08-17 批准 R1/R2/R3)。
    # 🔴🔴 **不是纯 additive**:其 §3 对 ck_geo_article_slot_event_kind 做**原子替换**,
    #    属 schema **行为变更**(往 CHECK 加允许值会放宽既有约束 —— 本仓已明确
    #    「加允许值不算 additive」)。因此按行为变更对待:
    #      · 形态是"先比定义再决定是否 DROP",第二遍重放什么都不做(定义已相同),
    #        约束缺失窗口只在真正需要变更时出现一次,而不是每次重放都出现;
    #      · 判据必须证明「重放 ×2 后该 CHECK 定义唯一且为新版」;
    #      · 另有绕应用直接 INSERT 非法 event_kind 的变异测试。
    # 🔴 顺序依赖:必须排在 034 之后(034 建的 geo_douyin_* 表被 slot writer 引用),
    #    且依赖 scripts/migration_geo_article_closed_loop_v1_2026_07_20.sql 已建的六表。
    # 🔴 零 DML:delivery_channel 刻意**不给 DEFAULT** —— 给 'article' 会改写历史行语义,
    #    给 'douyin_image_note' 会把文章槽位变成图文槽位。历史行留 NULL,
    #    读侧按非对称口径:文章 `IS NULL OR ='article'`,图文 `='douyin_image_note'`。
    "db/migration_035_geo_image_note_slot_channel_2026_08_18.sql",
    # 036 · WP7 · publish_outcome_records 增加 same-source 严格归因三列。
    # 🔴 **纯 additive、零 DML**:只 ADD COLUMN IF NOT EXISTS + 一条 CHECK。
    #    老列 ai_citations_delta_30d 的语义**不动**(仍是品牌 × 时间窗背景量);
    #    严格 same-source 命中去新列。同一列不许在不同时间段代表两件事 ——
    #    2026-08-16 `is_monitored` 三信源事件已经为这条交过学费。
    # 🔴 CHECK 里禁 `brand:<id>` 形态:那是 WP7 修掉的品牌级口径,
    #    不许从这个口子回来(schema 层挡,不靠调用方自觉)。
    "db/migration_036_publication_stage_strict_source_2026_08_18.sql",
    # 038 · 小榜 vNext 意图协调层(WO_XIAOBANG_VNEXT_EXECUTION_2026-08-18 · WP1)。
    #     两张**纯新表** xiaobang_operation_intents / xiaobang_confirmation_receipts,
    #     全 IF NOT EXISTS + DO $$ 包 CHECK,**零 DML**,不碰任何既有表的任何一列。
    # 🔴 顺序无依赖:不引用任何既有表(刻意不加 FK —— 归属校验必须每请求现做,
    #    不能靠 FK 假装做过;而 FK 在软删/租户迁移时是部署期地雷)。
    # 🔴 漏跑后果**响亮**(刻意):五阶段端点 prepare 时显式 INSERT 这两张表
    #    → UndefinedTable 当场抛、端点 500。这是新增能力,静默返空会让用户看到
    #    "已为你准备好"而其实什么都没存 —— 假结论比"暂时不可用"坏得多。
    #    小榜既有 /context /chat /parse-image 一行不受影响。
    # 🔴 无 rollback_038:回滚 = DROP 那两张新表,零业务残留;但表里存的是
    #    用户已确认过的意图与回执,DROP 等于把"我点过确认"从审计里抹掉,
    #    不该由部署清单自动做。真要回退请人工执行。
    "db/migration_038_xiaobang_intent_2026_08_18.sql",
    # 039 · publish_records 加**权威位** public_url_verification_state
    #     (WO_ARTICLE_BROWSER_SELF_REPORT_2026-08-19 §2 首选)。
    #     ADD COLUMN ×4 + 闭集 CHECK(DO 块补挂)+ 一条部分索引。
    # 🔴 顺序无依赖:只引用 publish_records 一张既有表。
    # 🔴 唯一一条 DML 是回填:把历史「浏览器显式回报过 URL」的行归 needs_action。
    #    生产实证(2026-08-19 只读探针)该谓词命中 **0 行** → 生产是 0 行改动;
    #    写它是为测试库/未来库同样收口,不是因为生产有存量。
    # 🔴 漏跑后果**响亮**(刻意):严格链四处消费方与写入方都显式引用
    #    public_url_verification_state → UndefinedColumn 当场抛。
    #    这一列要防的正是「自报被当成核实过的事实」,静默降级回旧行为等于洞没堵。
    # 🔴 重放安全:prestart 每次部署无条件重放全部迁移(无追踪表)。
    #    ADD COLUMN IF NOT EXISTS / CHECK 走 pg_constraint 存在性判断 / 回填带
    #    `AND public_url_verification_state='unverified'` 自限幂等。
    "scripts/migration_publish_records_url_verification_2026_08_19.sql",
    # 040/041 · 防御型 GEO 两阶段诊断的两个耐久对象
    #   (窄 RFC 已获 Review-CTO 2026-08-21 批准 ①②③ ·
    #    docs/AI-CONTEXT/DEFGEO_WP0_RFC_NEW_TABLES_2026-08-21.md)。
    # 🔴 顺序无依赖:两张全新表,刻意**不加 FK** —— 归属校验必须每请求现做,
    #    不能靠 FK 假装做过;FK 在软删/租户迁移时是部署期地雷。
    # 🔴 additive-only:零 ALTER 既有表、零 CREATE 于既有表之上。
    # 🔴 **体内零 DML**:两个文件都没有 INSERT/UPDATE/DELETE。
    #    prestart 每次部署无条件重放全部迁移(无追踪表),迁移里的 DML 是常驻地雷;
    #    本包也确实不需要 backfill(全新表,无存量行)。将来若需要,落一次性脚本 + 幂等判据。
    # 🔴 重放安全:CREATE TABLE/INDEX IF NOT EXISTS;约束与 trigger 走
    #    pg_constraint / pg_trigger 存在性判断;函数用 CREATE OR REPLACE。2× 复跑第二遍全 no-op。
    # 🔴 漏跑后果**响亮**(刻意):两阶段端点显式 INSERT 这两张表 → UndefinedTable 当场抛,
    #    端点走 SafeError 受控失败。静默返空会让服务商看到"已生成题单/已锁定报价"
    #    而其实什么都没存 —— 假结论比"暂时不可用"坏得多。
    # 🔴 041 体内自证「本表无 settlement 语义列」:§3.5 禁第二套 settlement enum,
    #    confirm 后唯一可写 settlement authority 仍是 diagnosis_runs.run_status。
    # 🔴 回滚:两张表都是新表,回滚 = DROP;但表里存的是服务商已确认过的题单与
    #    已冻结的报价快照,DROP 等于把"我签过这个价"从审计里抹掉,不该由部署清单自动做。
    #    真要回退请人工执行。
    "db/migration_040_defgeo_question_plans_2026_08_21.sql",
    "db/migration_041_defgeo_run_previews_2026_08_21.sql",
    # 042 · 防御型 GEO WP4 · keyword_selection_sessions 加客户接受快照指针
    #     + accepted 审计五项 + 复合 FK(customer_confirmed_snapshot_id, quote_id)
    #     → quote_pricing_snapshots(id, quote_id)。规格 §3.2。
    # 🔴 纯 additive:ADD COLUMN IF NOT EXISTS + DO 块补幂等约束,体内零 DML。
    # 🔴 顺序依赖:必须在 quote_pricing_snapshots 与 keyword_selection_sessions
    #     都已存在之后 —— 两者均由更早的迁移/初始化建立,故置于清单末尾。
    "db/migration_042_defgeo_customer_accepted_snapshot_2026_08_21.sql",
    # 043 · 防御型 GEO WP4 · defgeo_activation_outbox(窄 RFC 2026-08-21 已批)。
    #     幂等 root = UNIQUE(accepted_snapshot_id, quote_id) —— ACT-06「20 并发恰一」
    #     全部承重在这条唯一约束上,不靠应用层 SELECT-then-INSERT。
    # 🔴 零资金列(激活本身 execution freeze=0);体内零 DML。
    # 🔴 顺序依赖:引用 quote_pricing_snapshots(id, quote_id),必须在 042 之后。
    "db/migration_043_defgeo_activation_outbox_2026_08_21.sql",
    # 044 · 防御型 GEO WP5+WP6 · 发布格 / 媒体决策快照 / 发布命令 / 发布 outbox /
    #     Z-1 核验留痕 / provider-private 执行预算(窄 RFC
    #     docs/AI-CONTEXT/DEFGEO_W3_RFC_NEW_TABLES_2026-08-21.md · 🟡 待 Owner 签)。
    #     规格 §11.3 / §12.1 / §12.2 / §12.3 / §15.7。
    # 🔴 六张**全新**表,零 ALTER 既有表、体内零 DML。
    # 🔴 三条 PG16 partial unique 是 §12.2 的承重点(同 slot ≤1 open snapshot /
    #    ≤1 非终态 command / ≤1 committed fulfillment)—— 并发收敛靠它们,
    #    不靠应用层 SELECT-then-INSERT。
    # 🔴 顺序依赖:六张表之间有 FK(slots ← snapshots ← commands ← outbox/留痕),
    #    但都在同一文件内按依赖顺序建;对既有表零依赖,故可置于清单末尾。
    "db/migration_044_defgeo_publish_decision_2026_08_21.sql",
    # 045 · 防御型 GEO 正式诊断 run 的执行派发标记(门三 G9)。
    # 🔴 **不是新表**,是 diagnosis_runs 上两列 additive(ADD COLUMN IF NOT EXISTS
    #    + 有默认值)+ 一条 partial index。既有行不动、既有 CHECK 一个不改,
    #    故不走新表 RFC 流程。
    # 🔴 存在的理由是一条门库实证的资金事故:confirm 把 run 推进 running、
    #    冻住 7800 算力后**没有任何执行器接手**,run 在 running 挂了 23 分钟。
    #    dispatched_at = §12.3 的 external-start marker(领取即 CAS,天然互斥);
    #    dispatch_attempts = 让坏 run 能落回 sweeper 判死窗口被退款的那一半。
    # 🔴 顺序依赖:只依赖 diagnosis_runs(scripts/migration_diagnosis_runs_2026_07_13.sql,
    #    在本清单更早处),故置于末尾安全。体内零 DML。
    "db/migration_045_defgeo_run_dispatch_2026_08_22.sql",
    # 046 · 防御型 GEO WP7+WP8 · 监测 lineage / 防御投影 / 报告快照 /
    #     续费建议 / 小榜冻结公开理由(窄 RFC
    #     docs/AI-CONTEXT/DEFGEO_W4_RFC_NEW_TABLES_2026-08-22.md · 🟡 待 Owner 签)。
    #     规格 §6.1 / §6.3 / §13.1 / §13.2 / §13.3 / §13.4 / §9.6。
    # 🔴 五张**全新**表,零 ALTER 既有表、体内零 DML。
    # 🔴 三条承重约束在 attempt 账本上:UNIQUE(plan_cell_id, ordinal)
    #    + 终态行 BEFORE UPDATE 触发器 + partial UNIQUE(同格至多一个在飞)。
    #    MON-03「重试不覆盖原始失败」全部承重在这三条上,不靠应用层自觉。
    # 🔴 顺序:排在窗A 的 045 之后 —— 两者互不依赖(045 动 diagnosis_runs 两列,
    #    046 是五张全新表),编号序即执行序,合流时零耦合。
    # 🔴 回滚:五张都是新表,但 attempt 账本与报告快照存的是
    #    已发生的事实与已签发给客户的快照,DROP 等于抹审计。
    #    与 040/044 同口径:不由部署清单自动做,人工执行。
    "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql",
    # 047 · 防御型 GEO 发布命令的供应商单据引用(包E · 执行侧接线)。
    #     规格 §12.3 窗口③④:worker 外调拿到上游单号后必须有地方放,
    #     否则「provider 已接受」只剩一个状态字符串,回执核对无键可依,
    #     剩下的两条路(盲重传 / 永远挂着)都被 §12.1 逐字禁掉。
    # 🔴 两列 additive(IF NOT EXISTS + 可空),不动既有 CHECK,体内零 DML。
    # 🔴 顺序依赖:只依赖 044 建的 defgeo_publish_commands,故置于其后。
    "db/migration_047_defgeo_publish_provider_ref_2026_08_24.sql",
    # 048 · diagnosis_runs 加 reserved_split_snapshot_jsonb(P0-3b · WO 挂账 A)。
    #     一条 ADD COLUMN IF NOT EXISTS,**零 DML**,不碰任何既有行。
    # 🔴 顺序无依赖:只碰 diagnosis_runs 一张既有表,无 FK 无索引。
    # 🔴 漏跑后果**刻意不响亮**:读取侧走 `run.get(...)`(get_run 是 SELECT *),
    #    列不在 → None → 回落 P0-3 行为(单池自动按比例 / 多池转人工)。
    #    这条路径动的是钱:"少自动化一点"永远好过"因为列不在就猜一个扣费顺序出来"。
    # 🔴 存量行保持 NULL 是**有意**的 —— NULL = 冻结时没留快照,绝不拿现在的冻结行
    #    去补造历史 order(那正是本列要消灭的那种猜)。
    "db/migration_048_diagnosis_reserved_split_2026_08_24.sql",
    # 049 · 结算 AI 审查员裁定记录 diagnosis_settlement_adjudications
    #       (WO_SETTLEMENT_AI_ADJUDICATOR_2026-08-25 · 迁移号 Review 2026-08-25 登记)。
    #       CREATE TABLE + 3 CHECK(DO 块补挂)+ 3 索引,**零 DML**,不碰任何既有表。
    # 🔴 顺序无依赖:不建 FK(run 行被清理时审计轨必须留存),只新建自己这一张。
    # 🔴 漏跑后果**响亮**(刻意):审查员每条路径都**先写本表再动状态机**,
    #    表不在 → UndefinedTable 当场抛 → 该单跳过并计入 tick 异常;
    #    绝不会降级成"没留痕就把钱结了"。
    # 🔴 不复用既有 diagnosis_settlement_audit 的理由见 SQL 头部(2000 硬截 /
    #    无结构化 failure_cause 列 / 无 rule_version·frozen_points);既有表不退役,
    #    审查员每落一行本表**同事务**照写一行 audit。
    "db/migration_049_settlement_adjudications_2026_08_25.sql",
    # 050 · diagnosis_runs 加 payer_user_id(WO_CODEX_P0_DIAG_FUNDING A-1 · Codex P0-1)。
    #       一条 ADD COLUMN IF NOT EXISTS,**零 DML**,不碰任何既有 CHECK / 索引 / FK。
    # 🔴 顺序无依赖:只碰 diagnosis_runs 一张既有表。
    # 🔴 漏跑后果**响亮**(刻意 · 与 048 相反):
    #    `startup_schema_guards.verify_diagnosis_schema_fail_closed` 把该列列进关键列,
    #    缺列 → web/cron 拒绝启动。因为静默后果是「平台承担腿又拿租户 id 去结算平台的冻结」——
    #    即本迁移要修的那个 bug 原样复活,且没有任何判据会红。
    # 🔴 存量行保持 NULL 是**有意**的:NULL 的语义就是「payer = owner」,与存量事实一致,
    #    不回填、也不许回填(读取回落单点 = services.diagnosis_runs.settlement_payer_user_id)。
    "db/migration_050_diagnosis_payer_identity_2026_08_25.sql",
    # 051 · 防御型 GEO 发布链结算守卫(工单B · Codex 终审 P0-4 / P1-5)。
    #       两列 additive(settlement_attempts / last_settlement_error)+ 一条
    #       provider_order_ref 的部分唯一索引(NULL 豁免),**零 DML**。
    # 🔴 号段:050 归工单A(诊断资金链),051 归本单,下一空 = 052。
    #    两单互不依赖(050 动 diagnosis_runs,051 动 defgeo_publish_commands),
    #    合流时零耦合。
    # 🔴 顺序依赖:051 依赖 044 建的 defgeo_publish_commands 与 047 加的
    #    provider_order_ref 列,故必须置于 047 之后(编号序即执行序)。
    # 🔴 漏跑后果**响亮**(刻意):`store.bump_settlement_attempt` 是结算 retry
    #    路径的必经写点,列不在 → UndefinedTable 当场抛 ⇒ 该笔进 retry,
    #    绝不会降级成"没留痕就把钱结了";唯一索引不在 → 迁移体内反查 DO 块 RAISE。
    "db/migration_051_defgeo_publish_settlement_guards_2026_08_25.sql",
    # 052 · 激活 outbox 冻结 payer 身份(工单C · Codex 终审 P1-6)。
    #       三列 additive(payer_user_id / payer_funding_policy / payer_principal_kind)
    #       + 一条"三列同生同死"的 CHECK,**零 DML**。
    # 🔴 号段:050 归工单A(诊断资金链)· 051 归工单B(发布结算守卫)· **052 归工单C** ·
    #    下一空 = 053。三单互不依赖(050 动 diagnosis_runs / 051 动 defgeo_publish_commands /
    #    052 动 defgeo_activation_outbox),合流时零耦合。
    # 🔴 顺序依赖:052 依赖 043 建的 defgeo_activation_outbox,故必须置于 043 之后
    #    (编号序即执行序)。
    # 🔴 漏跑后果**响亮**(刻意):`activation_outbox.enqueue_activation` 的 INSERT
    #    显式写这三列 → 列不在即 UndefinedColumn → ActivationEnqueueError →
    #    fail-closed 让整笔客户确认回滚;绝不会退化成"确认成功但付款人没冻结"。
    "db/migration_052_defgeo_activation_frozen_payer_2026_08_25.sql",
    # 054 · 监测格冻结租户归属(工单E3 · Codex 二审 P1-F6)。
    #       一列 additive(monitoring_run_cells.tenant_owner_user_id,可空)
    #       + 一条禁 0 的 CHECK,**零 DML**。
    # 🔴 号段:053 归工单D(审查员挂龄提醒)· **054 归本单** · 下一空 = 055。
    # 🔴 顺序依赖:依赖 scripts/migration_monitoring_cell_retry_2026_07_21.sql
    #    建的 monitoring_run_cells(本清单第 110 行),编号序即执行序。
    # 🔴 可空是有意的:NULL = "租户未知"。禁 0 的锁装在 046 的账本表上 ——
    #    未知 ⇒ 不写账本 + 告警,绝不落一个编出来的租户。
    # 🔴 存量行回填与 census 由 Deploy 发车前单跑
    #    scripts/backfill_monitoring_cell_tenant_owner_2026_08_26.sql,
    #    **刻意不放进迁移体内**:回填读当前 brands.owner_user_id,放进每次重放的
    #    迁移就等于把"归属随品牌转移漂移"以另一种形态请回来。
    # 🔴 漏跑后果**响亮**(刻意):create_monitoring_run_cells 的 INSERT 显式写这一列 →
    #    列不在即 UndefinedColumn 当场抛 ⇒ 建格整批失败,
    #    绝不会退化成"格建出来了但没冻租户"。
    "db/migration_054_monitoring_cell_tenant_owner_2026_08_26.sql",
    # 055 · 图文成品记账落到报价与词上(#150 §3.2)。
    #       两列 additive(geo_douyin_posts.quote_id / confirmed_keyword_id,可空)
    #       + 两条部分索引,**零 DML**。
    # 🔴 号段:054 归工单 E3 · **055 归本单** · 下一空 = 056。
    # 🔴 顺序依赖:依赖 db/migration_017_geo_douyin_posts_2026_08_01.sql 建的表,
    #    编号序即执行序。
    # 🔴 类型 INTEGER 不是 BIGINT —— SQL 4 维核验第 2 维实测:
    #    quotes.id / confirmed_keywords.id 都是 integer(SERIAL)。
    #    scripts/fixtures 里写的是 BIGSERIAL,**夹具不是真相**。
    # 🔴 不回填历史行:历史成品不知道自己属于哪张报价,回填只能靠
    #    "同词即同报价"去猜 —— 而本迁移存在的理由正是那个猜法会串。
    # 🔴 漏跑后果响亮:写入侧显式写这两列 → 列不在即 UndefinedColumn 当场抛,
    #    不会退化成"成品建出来了但没记账"。
    "db/migration_055_geo_douyin_post_quote_binding_2026_09_08.sql",
    # 056 · 图文批量下单的幂等台账(#150 §3.3)。新表 + 一条索引,**零 DML**。
    # 🔴 主键 (created_by, request_id):request_id 由前端生成,单键会让
    #    A 的重放读到 B 的结果 —— 那是跨租户泄漏,不是幂等。
    # 🔴 不复用 billing_deduction_idempotency:那是**扣费**台账,
    #    塞进去会让资金对账读到既不扣也不退的行。
    "db/migration_056_geo_douyin_post_batches_2026_09_08.sql",
    # 057 · 防守线派发的终态原因(#157)。一列可空,**零 DML**。
    # 🔴 不复用 reaped_reason:那是 sweeper 判死的原因,与"派发时结构上跑不起来"
    #    是两件事;挤进一个列以后分不出该查哪一头。
    "db/migration_057_defgeo_dispatch_error_2026_09_08.sql",
    # 058 · 支付意向的三个 URL(#169)。三列可空,**零 DML**。
    # 🔴 不塞 pricing_snapshot_jsonb / settlement_snapshot_jsonb:那是不可变资金证据,
    #    混入支付 URL 后就分不清哪部分是成交时的原始快照(Review 2026-09-10 §5.2)。
    # 🔴 不回填历史:老单的 URL 已丢失,猜出来的链接会把用户送去别人的收银台;
    #    NULL 在这里表示"建于本迁移之前,没有留下出口",比一个编造的值诚实。
    "db/migration_058_recharge_payment_intent_urls_2026_09_10.sql",
    # 059 · 图文选题表(WO_204 §1.1)。新表 + 两条索引,**零 DML**。
    # 🔴 不复用写文章的 `topics`:那张表挂在报价体系上、带一整套文体合同;
    #    图文选题没有文体却有城市/卡片大纲/成品 post_id。挤进一张表以后,
    #    两边的查询都要先问"这行是哪一边的",判别条件漏写就是跨板块串数据。
    # 🔴 不回填历史蒸馏产物:老任务的 topics 早被当一次性清单用掉了,
    #    补进"待做"会让每个老客户凭空多出一堆没人要的选题。
    # 🔴 去重键是 (distill_task_id, distill_index) 不是 title:
    #    两条选题的标题可以合法相同,按 title 去重会静默吞掉第二条。
    "db/migration_059_geo_douyin_topics_2026_09_13.sql",
    # 060 · 防御型写法(playbook)按品牌持久化(#185 c3)。
    #   一张新表 + 两条条件约束 + 一条部分索引,**零 DML**(逐行核过:
    #   无 INSERT/UPDATE/DELETE/TRUNCATE/DROP;CREATE TABLE/INDEX 都带 IF NOT EXISTS,
    #   两条 ADD CONSTRAINT 在 DO 块里按 pg_constraint 判存在 —— 重放是空操作)。
    #
    # 🔴 漏登记的后果**不响亮**,这正是它危险的地方:
    #   `writing/defensive_playbook.py` 全文只有 SELECT / INSERT,**不自建表**
    #   (:288 / :314),而它那三处 fail-soft 会把 UndefinedTable 吞成一行
    #   「不阻断」告警 —— 表建不出来时,防御型 playbook 静默永远为空,
    #   页面照常、日志像良性降级。该模块自己的抬头(:188)就记着这个坑。
    #
    # 🔴 **本条目所指的 .sql 文件由 #185 线(067453418)提供,不在本分支上。**
    #   本分支只做「登记」这一笔,是为了让它能干净合进 0913f:
    #   185 分支的基底没有 059,在那边改 manifest 会与 059 撞同一处上下文。
    #   ⚠️ 唯一会注意到「条目有、文件没有」的是
    #   `tests/clean_db_bootstrap_2026_09_08`(:113-115 `if not p.is_file(): 文件缺失`),
    #   但那三条判据是 **xfail(#159 未修)**,所以本分支单独跑**不会红**
    #   —— 实测 2 passed / 3 xfailed,与合成树逐字相同。
    #   也就是说:这段时间里「文件缺不缺」**没有任何信号**。
    #   合上 067453418 之后文件就在,不再有这个窗口。
    #
    # 🔴 号段:059 归图文选题线;**060 归本条**;061 归 WO_225 媒体桶换算
    #   (原取 060,与本条撞号,Review 2026-09-15 裁定改 061)。下一空 = 062。
    "db/migration_060_defensive_playbook_2026_09_14.sql",
    # 061 · 媒体桶换算表 + topics.media_bucket(WO_225-c1)。一张新表 + 一列可空,**零 DML**。
    # 🔴 这里从 059 直接跳到 061,**不是漏号**:060 归 #185 c3 的
    #   `db/migration_060_defensive_playbook_2026_09_14.sql`(在另一条在途分支上,
    #   合流时随那条线一起进 manifest)。本单原本也取 060,撞号,
    #   Review 2026-09-15 裁定本单改 061。下一空 = 062。
    #   撞号本身有锁:tests/p03_settlement_2026_08_24/test_closeout_p03b.py::
    #   test_no_two_modern_migrations_claim_the_same_number —— 两条线合进同一棵树时
    #   撞号会当场判红,而不是让 prestart 按字典序跑出个谁也没想过的顺序。
    # 🔴 v1 初值不在迁移里播:迁移体内禁 DML 是本仓红线。播种由
    #    `services/media_slot_conversion.ensure_seeded()` 幂等写入(与 social_preferences_db
    #    的 ensure_schema 同法)—— 这样重放迁移不会覆盖 Owner 之后发的新版本。
    # 🔴 不塞 settings_manager / pricing_config:两者都是保护文件,且**不带版本**。
    #    这个数会随 Owner 口径变,而旧报价必须读回**当时那一版**(08_billing §3.3)。
    #    放进无版本的配置里,等于让历史报价的解释随当前配置漂移。
    # 🔴 topics.media_bucket 可空且**不回填**:回填 = 替历史上每一条选题猜它当初按哪类
    #    媒体发,而猜出来的桶会直接改变那些单的已用额度。NULL = 建于本迁移前、没留下口径。
    # 🔴 桶名逐字复用 035 的三值,不另起一套:同一概念两套词表迟早有一处对不上而不报错。
    "db/migration_061_media_slot_conversion_2026_09_15.sql",
    # 062 · client_profiles.basic_info_fields(WO_220-c2)。一列可空,**零 DML**。
    # 🔴 只装**没有同语义真列**的两个字段(products_services / proof_cases);
    #    另外四个复用真列(business_summary / target_users / selling_points / brand_constraints)。
    #    六个字段逐个真写真读验过:前两个写进 products / success_cases 会被
    #    `profile_db` 的 `array_fields` **按顿号逗号切碎成数组**,后四个逐字往返。
    # 🔴 不改 products 的数组语义:那是有意的(客户档案页 `match.products || []` 按数组用,
    #    后端 44 份 / 前端 26 份在读)。为一张自由文本表单倒灌回去 = 替那 70 份改了形状。
    # 🔴 不把六个全塞进来:target_users / selling_points / brand_constraints 今天就被
    #    客户档案页的自由文本框编辑着。两个面共享**同一份事实**正是本单要的;
    #    全塞进来等于给同一个谓词造两个住处,迟早有一处没人验。
    # 🔴 forbidden_notes 落 brand_constraints 而**不是** negative_feedback:
    #    后者是 array_fields,且有专门的 `add_negative_feedback` 追加口走飞轮语义
    #    (content_api「我有意见」的负反馈)。把「不能说的话」写进去会污染飞轮。
    #    判据里配了反臂:写 forbidden_notes 时 negative_feedback **零写入**。
    # 🔴 不回填:老档案没有「基础资料表」这个概念,猜不出用户当初会写什么。
    "db/migration_062_client_profile_basic_info_fields_2026_09_16.sql",

    # [WO_241 丙 2026-09-19] 标题批次 → 那一笔扣费 的映射。
    # 用途:「缺题的词」免费补救要先证明「这一批真的付过 ∧ 这个词属于这一批 ∧
    #       这个词还没出题」。第一环需要 request_id → charge_tx_id。
    # 🔴 不塞进 `topics`:`services/topic_generation_reservation.py` 抬头明令
    #    「扣费/退费口径一条不动,本模块不碰任何积分」—— 那是它的边界。
    # 🔴 号 063 = manifest max+1(登记处分配)。分支自取的号不算数;
    #    09-15 已有 185/225 同日都取 060 的撞号事故(IF NOT EXISTS 不报错、
    #    未登记的那条 prestart 永不跑)。本条与迁移文件同批提交。
    "db/migration_063_title_batch_charges_2026_09_19.sql",

    # [E0d · 开源 E3 前置 2026-09-27] 冷启动补齐 knowledge_chunks 表 + advisors.knowledge_base_path / last_kb_update。
    # 在役 GET /api/advisors 读它们;新库上原先唯一的代码建表处是社媒顾问知识导入管线(已随开源 E3 删除)。
    # 🔴 对生产是空操作:每一步先查后建;触发器函数不 CREATE OR REPLACE(生产那份函数体不同,绝不覆盖);
    #    补列先查 information_schema 再 ALTER(不裸写 ADD COLUMN IF NOT EXISTS,免每次部署抢 AccessExclusive 锁)。
    "db/migration_064_knowledge_chunks_coldstart_2026_09_27.sql",

    # [WO_310 2026-09-27] mhz_refund_requests 加可空 requested_by(管理员代申请时记管理员;受益人 user_id 改记付款人)。
    # 可空、不回填;先查 information_schema 再 ALTER。
    "db/migration_065_mhz_refund_requested_by_2026_09_27.sql",

    # [开源版 E5-4b 2026-10-03] 发布渠道 API 的媒体编号映射表(只建新表,IF NOT EXISTS 可重放)。
    # 开源仓自己的迁移,接在链尾 065 之后,不留空号。
    "db/migration_066_publish_channel_offers_2026_10_03.sql",
]
