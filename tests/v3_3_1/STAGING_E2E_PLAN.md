# V3.3.1 Staging E2E 测试计划

**目的**:Codex 四审 P2-4 反馈 · 静态 contract 测试只证签名 · 必须 staging 真 DB E2E 才能签 deploy-ready
**执行者**:Deploy-CTO(在 staging DB · prod 备份后)
**前置**:V3.3.1 candidate 分支 commit 已 pull · `migration_007` 已跑 + `--verify` PASS

---

## §1 必跑场景清单(11 项)

### A. Migration 与回滚

**A.1** `python -m db.migrate_v3_3_1`(无 --dry-run)
- 期望:`AST PASS` + 全部 SQL 0 ERROR + verify ≥ 7 张新表 + ≥ 7 V3.3.1 flags

**A.2** 二次 migration(幂等)
- 同命令再跑一次 · 期望 0 ERROR

**A.3** `python -m db.migrate_v3_3_1 --rollback`
- 期望:5 分钟内执行完 · 7 张表 DROP · 兼容字段保留

**A.4** rollback 后再 migration · 重建可用

### B. invite_codes + 注册接入

**B.1** L2 签发 user 邀请码 · `POST /api/identity/invite-codes/issue { "code_type": "user" }`
- 期望:invite_codes 新 record · expires_at = NOW + 30d

**B.2** 新用户用此码注册 → `consume_invite_code` 走通
- 期望:invite_codes.used_by_user_id 写入 · used_at 写入 · is_active=false
- 期望:user_wallets.agent_level=1 · referral_links 写 level=1 记录

**B.3** 同一码再注册另一个用户(并发模拟)
- 期望:抛 `InviteCodeAlreadyUsedError` · auth_api 不 fallback 老 bind_referral · 用户无推荐绑定

**B.4** 过期码注册(insert 时设 expires_at = NOW - 1d)
- 期望:抛 `InviteCodeExpiredError` · auth_api 不 fallback · 用户无推荐绑定

**B.5** 撤销码 · `POST /api/identity/invite-codes/revoke`
- 期望:invite_codes.is_active=false · 再用此码注册抛 `InviteCodeRevokedError` · 不 fallback

**B.6** 老 V3.1 referral_codes 码注册(invite_codes 查无此码)
- 期望:抛 `InviteCodeNotFoundError` · auth_api fallback 走老 bind_referral · 老逻辑兼容

### C. 服务费写入 + dual_write

**C.1** L2 邀请的下游充值 · `complete_recharge` 入口
- 期望:`service_fee_records` 新 record(net_cash_revenue_yuan 正确 · status='pending')
- 期望:dual_write flag ON 时同时 `pending_commissions` 新 record(amount_yuan/commission_rate/frozen_reason)

**C.2** UNIQUE 3 列防重复(模拟回调重试 2 次)
- 期望:service_fee_records.id 不重复 · 第二次 ON CONFLICT DO NOTHING

**C.3** L0 邀请的下游首充 · `pending_bonus_records` 写入
- 期望:rate=0.15 · settle_at=NOW+7d · UNIQUE(recharge_order_id, referrer_id)

**C.4** source ≠ external_cash_payment 时跳过(`balance_deduction` / `service_fee_conversion` / `external_media_purchase`)
- 期望:returnscope.skipped=True · skip_reason 标识 source 类型

### D. T+3 / T+7 cron + clawback

**D.1** 触发 `service_fee_t3_settle` cron
- 期望:available_at <= NOW 且无退款的 pending → settled · settled_at 写入

**D.2** T+3 前触发退款 · `on_recharge_refund(order_id, refund_reason='user_request')`
- 期望:pending → cancelled · clawback_reason='refund:user_request'

**D.3** T+3 后退款 · 已 settled
- 期望:status → clawback · LAYER 1 直接扣 amount

**D.4** 异常退款 4 类(`legal_dispute` / `platform_force` / `fraud` / `chargeback`)
- 期望:clawback_after_conversion 触发 · 后续 settled → bonus → paid → debt 表

### E. 转积分 + 大额审批闭环

**E.1** L2 转换 ¥100(在月度配额内 · 已过 T+7 退款期)
- 期望:service_fee_conversion_orders 新 record · paid_points_granted=13000 · bonus_points_granted=2600 · used_records 非空

**E.2** L2 转换超月度配额(¥6000 · 普通 quota=¥5000)
- 期望:`POST /service-fee/convert` 返 402 + `large_convert_endpoint`

**E.3** L2 调 `/service-fee/convert-large`
- 期望:service_fee_conversion_orders 新 record · status='pending_review' · requires_review=true · paid_points=0

**E.4** finance_reviewer 审批通过 · `POST /admin/service-fee/conversion-reviews/:id/decide { approve: true }`
- 期望:**同一条 record** 原地更新 · status='completed' · review_status='approved' · paid_points/bonus_points/records 填充
- 期望:不产生第二条 conversion_orders record
- 期望:用户钱包入账 paid_points + bonus_points

**E.5** 转换前置 T+7 退款期未过(模拟 available_at = NOW - 5d · min_age_days=7)
- 期望:抛 `SettledRecordsWithinRefundWindowError` · 提示 ready_amount + records_in_window

### F. 提现并发(P1-5 关键)

**F.1** L2 用户 (KYC 通过 + 绑卡 + settled 余额 ¥500)申请提现 ¥200
- 期望:service_fee_settlements 新 record · status='pending' · settled records → withdraw_requested

**F.2** 同一用户**并发** 2 笔提现请求(异步同时 POST `/withdraw-request`)
- 期望:advisory lock 串行化 · 第二笔抛 `WithdrawalRateLimitError`(weekly_pending=1)
- 期望:partial unique 兜底 · DB 层抛 UniqueViolation 不会有 2 条 pending

**F.3** Finance approve 提现 · settled records → withdrawn

**F.4** Finance partial_approve ¥100(申请 ¥200)
- 期望:FIFO 把 ¥100 → withdrawn · 剩余 ¥100 拆分回 settled
- 期望:service_fee_records 4 条状态(已 withdrawn 部分 + 已 settled 剩余)账目精确

**F.5** Finance reject · settled records 退回 settled · 用户重申

### G. failed_service_fee_jobs 补偿队列

**G.1** 故意制造 hook 异常(临时把 service_fee_records 表 RENAME)
- 期望:充值成功 · referral_api hook 抛异常 · failed_service_fee_jobs 新 record · retry_count=0

**G.2** 修复 schema · 触发 `service_fee_failed_jobs_retry` cron
- 期望:retry → 成功 · status='succeeded' · 真实 service_fee_records 入账

**G.3** 多次失败 · retry_count >= 3
- 期望:status='abandoned' · 财务 admin 手动处理

### H. ROLE gate + 蓝绿双跑测试

**H.1** 蓝绿两容器都设 V3_3_1_CRON_ROLE_GATE=primary
- 期望:仅 ROLE=primary 容器注册 V3.3.1 cron · backup 容器拒绝 + warning

**H.2** V3_3_1_CRON_ROLE_GATE=any(default)
- 期望:两容器都注册(注意:会双跑 · 仅开发/单实例使用)

### I. 钱包 UI

**I.1** L2 用户访问 `/wallet` · 钱包页底部见 ServiceFeeWalletCard(三按钮)

**I.2** L1/L0 用户访问 `/wallet` · ServiceFeeWalletCard 不渲染(组件内部自检 is_l2)

**I.3** L2 点"转换为充值积分" · 弹窗 + 不可撤销提示 + 425/402 异常处理

**I.4** L2 点"申请人工提现" · KYC + 实名 + 频次提示

**I.5** `/wallet/service-fee-history` 流水页 · 8 态筛选

### J. 老表归档兼容(P6 切流后)

**J.1** dual_write feature flag OFF · 老 pending_commissions 不再写入

**J.2** 一次性迁移 SQL · 老数据 → service_fee_records · 0 差异

**J.3** 老表只读 · 90 天后 archive

### K. 性能基线

**K.1** wallet GET 增加 5 子查询(L2)· EXPLAIN ANALYZE Total < 10ms

**K.2** convert_service_fee_to_points 锁定 100 records · < 100ms

**K.3** 提现 advisory lock 在 1000 并发用户下 · queue depth < 5

---

## §2 执行模板

```bash
# 1. 备份 prod DB
SSH prod:
docker exec omnirank-db pg_dump -U geo_admin geo_agentscope \
  > /backup/v3_3_1_pre_e2e_$(date +%Y%m%d_%H%M).sql

# 2. 在 staging 跑 migration
cd c:/AI-Test/AgentsCope-07-identity-v3.3.1
python -m db.migrate_v3_3_1
python -m db.migrate_v3_3_1 --verify

# 3. 启动 staging server
APP_ENV=staging python -m uvicorn server:app --port 8001

# 4. 执行 A-K 全部场景
# 每个场景手动 / 脚本验证 · 输出 PASS/FAIL 标记

# 5. 任何 FAIL → rollback
python -m db.migrate_v3_3_1 --rollback
```

---

## §3 与 KICKOFF_CHECKLIST 联动

本 STAGING_E2E_PLAN 是 KICKOFF_CHECKLIST §4 staging 准备(第 16-20 项)的细化:
- #16 staging DB 备份 → 本文 §2 步骤 1
- #17 staging migration dry-run → 本文 A.1-A.4(实际跑 + 幂等 + 回滚)
- #18 staging migration 幂等性 → 本文 A.2
- #19 staging Python AST + tsc PASS → 已在 candidate 分支 commit 前完成
- #20 回滚 SQL 准备完毕 → 本文 A.3 实测

V3.3.1 全部 deploy-ready 门禁:
- ✅ Codex 三审 + 四审反馈全修
- ⏳ A-K 11 类场景 staging E2E 全 PASS(本文)
- ⏳ 财务签字 净现金基数(IDENTITY_DECISIONS_LOCK Q31-Q32)
- ⏳ 法务签字 三协议(IDENTITY_DECISIONS_LOCK Q30)
- ⏳ 老板签字 Q1-Q37

---

**文档作者**:CTO-15.23
**创建日期**:2026-05-12
**关联**:
- [V3.3.1_HANDOFF_2026-05-12.md](../../../../AgentsCope-07/docs/AI-CONTEXT/V3.3.1_HANDOFF_2026-05-12.md)
- KICKOFF_CHECKLIST §4
- Codex 四审 P2-4
