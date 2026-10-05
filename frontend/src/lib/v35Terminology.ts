/**
 * V3.5 工厂模式 · 术语 SSOT 词典(批 2 · 2026-05-26)
 *
 * 单点输出 label / status / pool / source / transaction type · 按 3 角色返不同口径。
 *
 * 角色定义:
 *   - 'customer'  客户身份 · 隐藏所有工程字段(SKU/paid/bonus/tool/publish/ledger/V3.5)
 *   - 'agent'     代理身份 · 中文化经营后台口径(我的库存/销售定价/收益结算)
 *   - 'admin'     管理员身份 · 中文财务口径(代理打款审核/资金对账)
 *
 * 使用示例:
 *   import { t, transactionTypeLabel, poolLabel, sourceLabel, statusLabel } from '@/lib/v35Terminology';
 *
 *   <Badge>{t('SKU', 'customer')}</Badge>                 // → "算力包"
 *   <span>{transactionTypeLabel(tx.type, 'agent')}</span> // type='consume' → "客户使用"
 *   <span>{poolLabel(tx.pool, 'customer')}</span>          // pool='tool' → "算力"
 *   <span>{statusLabel(row.status, 'ledger', 'admin')}</span> // settled → "已可结算"
 *
 * 文档:docs/AI-CONTEXT/V35_FOLLOWUP_PLAN_2026-05-26.md §2.3
 */

export type V35Role = 'customer' | 'agent' | 'admin';

// 隐藏标识:某些字段对特定角色完全不显示
const HIDDEN = '__V35_HIDDEN__';
export function isHidden(label: string): boolean {
  return label === HIDDEN;
}

// ============================================================
// 静态字段词典(label · 顶层 key)
// ============================================================
const STATIC_DICT: Record<string, Record<V35Role, string>> = {
  SKU:               { customer: '算力包',       agent: '算力包',         admin: '算力包(模板)' },
  paid:              { customer: HIDDEN,          agent: '充值库存',       admin: '充值库存' },
  bonus:             { customer: HIDDEN,          agent: '赠送库存',       admin: '赠送库存' },
  ledger:            { customer: HIDDEN,          agent: '结算明细',       admin: '结算明细' },
  settled:           { customer: HIDDEN,          agent: '已可结算',       admin: '已可结算' },
  pending_payout:    { customer: HIDDEN,          agent: '打款处理中',     admin: '打款处理中' },
  clawback:          { customer: HIDDEN,          agent: '待抵扣',         admin: '待抵扣' },
  drift:             { customer: HIDDEN,          agent: HIDDEN,           admin: '对账异常' },
  diff_paid:         { customer: HIDDEN,          agent: HIDDEN,           admin: '充值库存差额' },
  diff_bonus:        { customer: HIDDEN,          agent: HIDDEN,           admin: '赠送库存差额' },
  diff_publish:      { customer: HIDDEN,          agent: HIDDEN,           admin: '发布算力差额' },
  factory:           { customer: HIDDEN,          agent: '进货价',         admin: '平台结算价' },
  bps:               { customer: HIDDEN,          agent: '百分比',         admin: '百分比' },
  'V3.5':            { customer: HIDDEN,          agent: HIDDEN,           admin: HIDDEN }, // 仅协议页可见(组件内单独走)
  wholesale_cents:   { customer: HIDDEN,          agent: '进货价',         admin: '出厂价' },
  platform_cost_cents: { customer: HIDDEN,        agent: HIDDEN,           admin: '平台真实成本' },
  tax_rate_bps:      { customer: HIDDEN,          agent: HIDDEN,           admin: '预扣税率' },
  agent:             { customer: '服务方',       agent: '我',             admin: '代理' },
  customer:          { customer: '我',           agent: '客户',           admin: '客户' },
  // 工程量纲
  retail_cents:      { customer: '售价',         agent: '客户售价',       admin: '客户售价' },
  amount_cents:      { customer: '金额',         agent: '金额',           admin: '金额' },
  cost_points:       { customer: '消耗',         agent: '算力用量',       admin: '算力用量' },
  // 业务名词
  recharge:          { customer: '充值',         agent: '客户充值',       admin: '客户充值' },
  white_label:       { customer: HIDDEN,          agent: '白标',           admin: '白标' },
  binding_dispute:   { customer: HIDDEN,          agent: HIDDEN,           admin: '客户归属争议' },
};

// ============================================================
// 动态字段词典(transaction type / pool / source / status)
// ============================================================

// customer_credit_transactions.type(4 种)
const TRANSACTION_TYPE_DICT: Record<string, Record<V35Role, string>> = {
  allocate:          { customer: '算力入账',     agent: '算力划拨',       admin: '划拨入账' },
  consume:           { customer: '使用',         agent: '客户使用',       admin: '客户消费' },
  refund:            { customer: '退款回账',     agent: '退款回账',       admin: '退款回账' },
  revoke:            { customer: '撤回',         agent: '撤回未消费',     admin: '撤回未消费' },
};

// customer_credit_transactions.pool(3 种)
const POOL_DICT: Record<string, Record<V35Role, string>> = {
  tool:              { customer: '算力',         agent: '算力',           admin: '算力' },
  publish:           { customer: '发布算力',     agent: '发布算力',       admin: '发布算力' },
  bonus:             { customer: '赠送算力',     agent: '赠送算力',       admin: '赠送算力' },
  // consume_credit 可能返回组合(bonus+tool / bonus / tool)
  'bonus+tool':      { customer: '赠送+算力',   agent: '赠送+算力',     admin: '赠送+算力' },
};

// 流水 source
const SOURCE_DICT: Record<string, Record<V35Role, string>> = {
  online_payment:    { customer: '充值入账',     agent: '客户充值',       admin: '客户充值' },
  offline_allocation:{ customer: '服务方划拨',   agent: '线下划拨',       admin: '线下划拨' },
  admin_adjust:      { customer: '系统调整',     agent: '系统调整',       admin: '管理员处置' },
  tool_consume:      { customer: '工具消费',     agent: '工具消费',       admin: '工具消费' },
  refund_revoke:     { customer: '退款撤回',     agent: '退款撤回',       admin: '退款撤回' },
};

// agent_inventory_transactions.type(代理库存流水 · 6 种)
const INVENTORY_TRANSACTION_TYPE_DICT: Record<string, Record<V35Role, string>> = {
  purchase_prepay:    { customer: HIDDEN,         agent: '进货预付',       admin: '代理进货预付' },
  allocate_to_customer:{ customer: HIDDEN,        agent: '划拨给客户',     admin: '划拨给客户' },
  clawback_to_customer:{ customer: HIDDEN,        agent: '客户退款抵扣',   admin: '客户退款抵扣' },
  purchase_settle:    { customer: HIDDEN,         agent: '进货结算',       admin: '进货结算' },
  bonus_grant:        { customer: '赠送入账',     agent: '赠送给客户',     admin: '赠送入账' },
  manual_adjust:      { customer: HIDDEN,         agent: '手动调整',       admin: '手动调整' },
};

// 各表 status · 按真实 schema 枚举(老板 r3 review · 已实证 CHECK 约束)
const STATUS_DICT: Record<string, Record<string, Record<V35Role, string>>> = {
  // agent_revenue_ledger.status(scripts/migration_v35_factory_inventory_2026_05_26.sql:225)
  // 真实枚举:'frozen' / 'settled' / 'cancelled' · 提现锁定靠 settlement_request_id 推断 · 不直接进 status
  ledger: {
    frozen:          { customer: HIDDEN,         agent: '冻结中(T+3)',   admin: '冻结中(T+3)' },
    settled:         { customer: HIDDEN,         agent: '已可结算',       admin: '已可结算' },
    cancelled:       { customer: HIDDEN,         agent: '已取消',         admin: '已取消' },
  },
  // agent_factory_agreements.status(scripts/migration_v35_w3_2026_05_26.sql:17)
  agreement: {
    unsigned:        { customer: HIDDEN,         agent: '待签署',         admin: '待签署' },
    signed:          { customer: HIDDEN,         agent: '已签署',         admin: '已签署' },
    rejected:        { customer: HIDDEN,         agent: '已拒绝',         admin: '已拒绝' },
    expired:         { customer: HIDDEN,         agent: '已过期',         admin: '已过期' },
  },
  // customer_agent_binding_disputes.status(scripts/migration_v35_w4_2026_05_26.sql:14)
  // 真实枚举:'pending' / 'resolved_keep_old' / 'resolved_reassign' / 'rejected'
  dispute: {
    pending:             { customer: HIDDEN,     agent: HIDDEN,           admin: '待处理' },
    resolved_keep_old:   { customer: HIDDEN,     agent: HIDDEN,           admin: '维持原服务方' },
    resolved_reassign:   { customer: HIDDEN,     agent: HIDDEN,           admin: '改绑新服务方' },
    rejected:            { customer: HIDDEN,     agent: HIDDEN,           admin: '已驳回' },
  },
  // inventory_audit_runs.has_drift bool · 不是 status 列 · 这里 normalize 'ok'/'drift'
  audit: {
    ok:              { customer: HIDDEN,         agent: HIDDEN,           admin: '正常' },
    drift:           { customer: HIDDEN,         agent: HIDDEN,           admin: '异常' },
  },
  // agent_settlement_requests.status(scripts/migration_v35_factory_inventory_2026_05_26.sql:252)
  // 真实枚举:'pending' / 'approved' / 'paid' / 'rejected'(无 paid_out)
  settlement: {
    pending:         { customer: HIDDEN,         agent: '审批中',         admin: '待审批' },
    approved:        { customer: HIDDEN,         agent: '已通过',         admin: '已通过' },
    paid:            { customer: HIDDEN,         agent: '已打款',         admin: '已打款' },
    rejected:        { customer: HIDDEN,         agent: '已驳回',         admin: '已驳回' },
  },
  // recharge_orders.status
  recharge: {
    pending:         { customer: '待支付',       agent: '客户待支付',     admin: '待支付' },
    paid:            { customer: '已支付',       agent: '已收款',         admin: '已收款' },
    cancelled:       { customer: '已取消',       agent: '已取消',         admin: '已取消' },
    refunded:        { customer: '已退款',       agent: '已退款',         admin: '已退款' },
  },
  // agent_revenue_ledger.invoice_status(scripts/migration_v35_factory_inventory_2026_05_26.sql:218)
  invoice: {
    none:            { customer: HIDDEN,         agent: '未申请',         admin: '未申请' },
    submitted:       { customer: HIDDEN,         agent: '已提交',         admin: '待审核' },
    approved:        { customer: HIDDEN,         agent: '已开票',         admin: '已开票' },
    rejected:        { customer: HIDDEN,         agent: '已驳回',         admin: '已驳回' },
    refunded:        { customer: HIDDEN,         agent: '已红冲',         admin: '已红冲' },
  },
};

const FALLBACK_BY_ROLE: Record<V35Role, string> = {
  customer: '其他',
  agent: '其他',
  admin: '未归类',
};

// 空态文案(scenario · 不分角色 · 必要时再分)
const EMPTY_DICT: Record<string, string> = {
  no_credit_transactions:  '暂无算力明细',
  no_inventory_records:    '暂无库存记录',
  no_ledger:               '暂无结算记录',
  no_disputes:             '暂无争议',
  no_audit_history:        '暂无对账记录',
  no_skus:                 '暂无可购买算力包',
  no_customer:             '暂无客户',
  no_binding:              '当前账户尚未完成价格配置',
};

// ============================================================
// 公共 API · 5 个 label 函数 + t() + empty()
// ============================================================

/**
 * 静态 label 翻译 · 默认 fallback 为原 key
 * 隐藏字段(HIDDEN)返回空字符串(避免显示)
 */
/**
 * 侧栏标签 SSOT(批 3 中文化经营后台口径 · 2026-05-26)。
 *
 * 🔴 在此之前**这份词典里一个侧栏名都没有** —— `test_w5_8` 读出的「自身 4/8」
 *    数的是本文件**头部注释**里的举例(第 8-9 行),不是真词条。
 *    所以这一批不是"补 4 条",是把这张表**建起来**并让 AppSidebar 真的读它。
 *
 * 🔴 两条期望名**刻意不收**(锚过期,不是缺陷):
 *    `额度包管理` / `资金与额度对账` 含「额度」,而
 *    `CLAUDE.md:1475` 与 `AppSidebar.tsx` 自己的头注释都写着
 *    「全站文案统一『算力』(禁『积分/额度/工具额度』)」——
 *    批 3(05-26)早于那条统一口径,它的名单里这两条已被后来的规则废掉。
 *    侧栏现名「资金与算力对账」才是**现行**正确名。已报 Review 定夺。
 */
export const SIDEBAR_LABELS: Record<string, string> = {
    agent_inventory:  '我的库存',
    agent_pricing:    '销售定价',
    agent_settlement: '收益结算',
    agent_promotion:  '获客推广',
    agent_agreement:  '合作协议',
    /**
     * 🔴 批 3 原名「代理打款审核」含「代理」—— Review 2026-09-06 补裁禁「代理」
     * (对外统一称「服务商」)。仓内先例:侧栏 `/admin/agent-inventory` 已叫
     * 「服务商库存与关系」。
     */
    admin_settlements: '服务商打款审核',
    /**
     * 🔴 批 3 原名「额度包管理」——「额度」是禁词,而且**那一页已经不存在了**:
     * `pages/Admin/PricingCenter.tsx:5` 写着「合并自:算力包管理(PricingMgmt)
     * + 定价系数配置(PricingConfig)」。所以它不是改个名,是**被合并掉**了,
     * 现行入口就是算力定价中心。
     */
    admin_pricing_center: '算力定价中心',
    /** 🔴 批 3 原名「资金与额度对账」——「额度」禁词,现行名如下(侧栏本来就是这个)。 */
    admin_fund_audit: '资金与算力对账',
};

/** 侧栏标签只从这里取;取不到就报出 key,**不回落到硬编码**(回落会让漏项永远看不见)。 */
export function sidebarLabel(key: string): string {
    return SIDEBAR_LABELS[key] ?? key;
}

export function t(key: string, role: V35Role): string {
  const entry = STATIC_DICT[key];
  if (!entry) return FALLBACK_BY_ROLE[role];
  const val = entry[role];
  return val === HIDDEN ? '' : val;
}

/**
 * customer_credit_transactions.type → 角色化 label
 */
export function transactionTypeLabel(type: string, role: V35Role): string {
  const entry = TRANSACTION_TYPE_DICT[type];
  if (!entry) return FALLBACK_BY_ROLE[role];
  const val = entry[role];
  return val === HIDDEN ? '' : val;
}

/**
 * customer_credit_transactions.pool → 角色化 label
 */
export function poolLabel(pool: string, role: V35Role): string {
  const entry = POOL_DICT[pool];
  if (!entry) return FALLBACK_BY_ROLE[role];
  const val = entry[role];
  return val === HIDDEN ? '' : val;
}

/**
 * 流水 source → 角色化 label
 */
export function sourceLabel(source: string, role: V35Role): string {
  const entry = SOURCE_DICT[source];
  if (!entry) return FALLBACK_BY_ROLE[role];
  const val = entry[role];
  return val === HIDDEN ? '' : val;
}

/**
 * 各表 status → 角色化 label
 * table: 'ledger' | 'agreement' | 'dispute' | 'audit' | 'settlement' | 'recharge'
 */
export function statusLabel(status: string, table: string, role: V35Role): string {
  const tableDict = STATUS_DICT[table];
  if (!tableDict) return FALLBACK_BY_ROLE[role];
  const entry = tableDict[status];
  if (!entry) return FALLBACK_BY_ROLE[role];
  const val = entry[role];
  return val === HIDDEN ? '' : val;
}

/**
 * agent_inventory_transactions.type → 角色化 label
 */
export function inventoryTransactionTypeLabel(type: string, role: V35Role): string {
  const entry = INVENTORY_TRANSACTION_TYPE_DICT[type];
  if (!entry) return FALLBACK_BY_ROLE[role];
  const val = entry[role];
  return val === HIDDEN ? '' : val;
}

/**
 * 空态文案
 */
export function empty(scenario: string): string {
  return EMPTY_DICT[scenario] || '暂无数据';
}

/**
 * 拼合动态文案:type + pool(常见 "使用 · 算力")
 * 用于流水列表 · 隐藏字段自动跳过
 */
export function transactionDisplay(type: string, pool: string | null | undefined, role: V35Role): string {
  const t1 = transactionTypeLabel(type, role);
  const t2 = pool ? poolLabel(pool, role) : '';
  if (t1 && t2) return `${t1} · ${t2}`;
  return t1 || t2 || FALLBACK_BY_ROLE[role];
}

/**
 * V3.5 8 个 error code → 角色化客户文案(2026-05-27 UI 审计 P3 修)
 * billing.charge / consume_credit / wallet_api 抛 HTTPException 时
 * 前端从 detail.code 查中文 · 不再裸露后端原文
 */
const ERROR_CODE_DICT: Record<string, Record<V35Role, string>> = {
  AGENT_NOT_SIGNED:            { customer: '当前账户价格配置尚未完成 · 请稍后重试',     agent: '当前账户服务配置尚未完成 · 请联系平台', admin: '代理协议未签' },
  AGENT_REQUIRED:              { customer: '当前账户价格配置尚未完成，请稍后重试',        agent: '客户价格配置尚未完成',                 admin: '客户未绑定代理' },
  AGENT_MISMATCH:              { customer: '账户配置发生变化 · 请刷新后重试',           agent: '账户配置发生变化 · 请刷新后重试',      admin: '代理与客户不匹配' },
  NEED_BIND_AGENT:             { customer: '当前账户价格配置尚未完成，请稍后重试',        agent: '客户价格配置尚未完成',                 admin: '客户需先绑定代理' },
  INSUFFICIENT_CREDIT:         { customer: '当前功能可用算力不足 · 请充值或联系平台',    agent: '客户当前功能可用算力不足 · 请充值',    admin: '客户算力不足' },
  PUBLISH_REQUIRES_PAID_CREDIT:{ customer: '发布功能需要使用充值算力',                  agent: '发布需要充值算力(非赠送)',              admin: '发布需充值算力(非赠送)' },
  V35_FACTORY_DISABLED:        { customer: '服务暂未开启 · 请联系平台',                 agent: '当前账户服务暂未开启 · 请联系平台',    admin: '工厂模式未开启' },
  V35_FREEZE_NOT_SUPPORTED:    { customer: '该功能正在升级 · 请稍后再试或联系平台',     agent: '长任务暂未开放 · 客户可用短任务',      admin: '长任务冻结链未实现(批 1B-2)' },
  // legacy 兼容(老 user_wallets 路径仍可能抛)
  INSUFFICIENT_POINTS:         { customer: '可用算力不足 · 请充值',                     agent: '算力不足 · 请充值',                    admin: '算力不足' },
  INSUFFICIENT_PAID_POINTS:    { customer: '充值算力不足 · 请充值',                     agent: '充值算力不足',                         admin: '充值算力不足' },
  NO_WALLET:                   { customer: '账户初始化中 · 请稍后再试',                 agent: '钱包不存在',                           admin: '钱包不存在' },
  DEDUCT_CONFLICT:             { customer: '操作冲突 · 请稍后再试',                     agent: '扣费冲突 · 请重试',                    admin: '扣费冲突' },
};

/**
 * V3.5 error code → 角色化客户文案
 * 未命中返 null · 调用方走 fallback
 */
export function errorCodeLabel(code: string | null | undefined, role: V35Role): string | null {
  if (!code) return null;
  const entry = ERROR_CODE_DICT[code];
  if (!entry) return null;
  const val = entry[role];
  return val === HIDDEN ? null : val;
}

/**
 * agent_settlement_requests.payout_method · admin 视角中文化
 * 用于打款审核页 raw enum
 */
export function payoutMethodLabel(method: string | null | undefined): string {
  const key = (method || '').toLowerCase();
  const labels: Record<string, string> = {
    wechat: '微信',
    wechat_pay: '微信',
    alipay: '支付宝',
    bank: '银行卡',
    bank_transfer: '银行卡',
    manual: '人工',
  };
  return labels[key] || method || '未指定';
}

/**
 * inventory_audit_runs.triggered_by · admin 视角中文化
 */
export function triggerByLabel(trigger: string | null | undefined): string {
  const key = (trigger || '').toLowerCase();
  const labels: Record<string, string> = {
    cron: '定时',
    scheduled: '定时',
    manual: '手动',
    admin: '管理员',
    api: '接口',
    system: '系统',
  };
  return labels[key] || trigger || '-';
}

/**
 * 通用 task / job status 翻译(全局 raw status 兜底)
 * 用于 Workspace / RollbackDialog / AgentTrace / 社媒工作台 等 raw {status} 渲染
 */
export function taskStatusLabel(status: string | null | undefined): string {
  const key = String(status || '').toLowerCase();
  const labels: Record<string, string> = {
    pending: '待处理',
    queued: '排队中',
    running: '处理中',
    in_progress: '处理中',
    processing: '处理中',
    completed: '已完成',
    success: '已完成',
    succeeded: '已完成',
    done: '已完成',
    failed: '失败',
    error: '失败',
    cancelled: '已取消',
    canceled: '已取消',
    timeout: '超时',
    skipped: '已跳过',
    paused: '已暂停',
    rolled_back: '已回退',
    partial_success: '部分成功',
    pending_review: '待审核',
    approved: '已通过',
    rejected: '已驳回',
    auto_skipped: '已自动跳过',
    crawled: '已抓取',
    cleaned: '已清洗',
    ok: '正常',
  };
  return labels[key] || status || '-';
}

/**
 * GEO 调研监测后台 · domain_tier 域名分层 → 中文化(r12 2026-05-27 老板 r11 盲区修)
 * 后端原始 enum:whitelist / gray / blacklist · 直接渲染会露英文
 * 未命中返"未分级"
 */
export function domainTierLabel(tier: string | null | undefined): string {
  const key = String(tier || '').toLowerCase().trim();
  if (!key) return '-';
  const labels: Record<string, string> = {
    whitelist: '白名单',
    gray: '中性',
    blacklist: '黑名单',
  };
  return labels[key] || '未分级';
}

/**
 * 跑批阶段 stage → 中文化(r11 2026-05-27 老板 r10 盲区修)
 * GEO 调研监测后台 RoundsPanel · current_stage 后端原始英文 enum
 * 未命中返"未知阶段"避免裸露后端值
 */
export function stageLabel(stage: string | null | undefined): string {
  const key = String(stage || '').toLowerCase().trim();
  if (!key) return '-';
  const labels: Record<string, string> = {
    pending: '待启动',
    queued: '排队中',
    crawl: '抓取',
    crawling: '抓取中',
    clean: '清洗',
    cleaning: '清洗中',
    cleaned: '已清洗',
    classify: '分类',
    classifying: '分类中',
    extract: '提取',
    extracting: '提取中',
    extracted: '已提取',
    score: '评分',
    scoring: '评分中',
    scored: '已评分',
    review: '审核',
    reviewing: '审核中',
    reviewed: '已审核',
    publish: '发布',
    publishing: '发布中',
    published: '已发布',
    running: '处理中',
    in_progress: '处理中',
    processing: '处理中',
    completed: '已完成',
    finished: '已完成',
    success: '已完成',
    done: '已完成',
    failed: '失败',
    error: '失败',
    cancelled: '已取消',
    canceled: '已取消',
    partial_success: '部分成功',
    failed_resumable: '可续跑',
    timeout: '超时',
    skipped: '已跳过',
  };
  return labels[key] || '其他阶段';
}

/**
 * 通用 active / inactive / disabled 状态(各类后台表头用)
 */
export function activeStatusLabel(status: string | boolean | number | null | undefined): string {
  if (status === true || status === 'active' || status === 'enabled' || status === 1) return '启用中';
  if (status === false || status === 'inactive' || status === 'disabled' || status === 0) return '已停用';
  if (status === 'pending') return '待审';
  return String(status || '-');
}

export function paymentChannelLabel(channel: string | null | undefined): string {
  const key = (channel || '').toLowerCase();
  const labels: Record<string, string> = {
    auto: '自动选择',
    wechat: '微信支付',
    wechat_pay: '微信支付',
    wechat_native: '微信扫码支付',
    native: '微信扫码支付',
    wechat_h5: '微信 H5(已停用)',
    h5: '浏览器支付',
    xunhupay: '备用微信支付',
    huipi: '备用微信支付',
    manual: '人工确认',
    alipay: '支付宝',
  };
  return labels[key] || '支付通道';
}

/**
 * 金额格式化(分 → 元 · 不带"¥"前缀 · 调用方按需加)
 * V3.5 给客户/代理/admin 都用元 · 不暴露分量纲
 */
export function yuan(cents: number | null | undefined): string {
  if (cents == null) return '0.00';
  return (cents / 100).toFixed(2);
}

/**
 * 百分比格式化(bps → %)
 * V3.5 内部 tax_rate_bps 等用 bps · 显示给 admin 时转 %
 */
export function bpsToPercent(bps: number | null | undefined): string {
  if (bps == null) return '0';
  return (bps / 100).toFixed(2);
}
