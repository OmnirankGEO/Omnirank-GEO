/**
 * M3 SSOT 类型与枚举
 *
 * 老板元指令 17 严禁硬编码:阶段枚举 / 报价状态 / 付款状态 / 服务期状态 / 评分等级
 * 这里集中定义所有 M3 业务枚举 · 其他模块 import 不重复。
 *
 * 与后端真实字段对齐:
 *   - quotes.status ∈ {draft, confirmed, pending_payment, paid, archived}  server.py 真实 5 态(2026-04-25 验证)
 *   - quotes.service_status ∈ {active, expiring, expired, paused}     api/scheduler.py:_check_service_periods
 *   - quotes.service_start_date / service_end_date / service_months   api/selection_api.py:1939-1947
 *   - brand.brand_type ∈ {self, client}                                db/profile_db.py
 *   - brand.industry / city / contact / company_name                  client_profiles
 */

// ============================================================
// 9 步生命周期 stage(代理 GEO 主链路)
// ============================================================

export const LIFECYCLE_STAGES = [
  { value: 1, key: 'inquiry', label: '询价' },
  { value: 2, key: 'diagnosing', label: '诊断中' },
  { value: 3, key: 'quote_pending', label: '待报价' },
  { value: 4, key: 'quote_sent', label: '报价中' },
  { value: 5, key: 'writing', label: '写作' },
  { value: 6, key: 'publishing', label: '投放' },
  { value: 7, key: 'monitoring', label: '监测' },
  { value: 8, key: 'reporting', label: '报告' },
  { value: 9, key: 'renewal', label: '续费' },
] as const;

export type LifecycleStage = (typeof LIFECYCLE_STAGES)[number]['value'];
export type LifecycleStageKey = (typeof LIFECYCLE_STAGES)[number]['key'];

export function getStageLabel(stage: LifecycleStage): string {
  return LIFECYCLE_STAGES.find((s) => s.value === stage)?.label ?? '未知';
}

export function getStageKey(stage: LifecycleStage): LifecycleStageKey {
  return LIFECYCLE_STAGES.find((s) => s.value === stage)?.key ?? 'inquiry';
}

// ============================================================
// B.3 (CTO-15.18 · 2026-04-28):9 stage 折叠 4 阶段(原对齐后端的 M3 阶段校验器,该后端已随开源 E3 删除)
// ============================================================
//
// 老板红线(2026-04-28):
// - 9 stage 概念保留(垂直 SaaS 城墙)· UI 默认折叠 4 阶段
// - 4 阶段:销售 / 写作 / 监测 / 续费
// - 隐藏 stage 数字 · 用 ✓ 和高亮
// - 点"展开"看 9 stage 详情(折叠面板)

export type StageGroupKey = 'sales' | 'writing' | 'monitoring' | 'renewal';

export const LIFECYCLE_STAGE_GROUPS = [
  { key: 'sales' as StageGroupKey, label: '销售', stages: [1, 2, 3, 4] as LifecycleStage[] },
  { key: 'writing' as StageGroupKey, label: '写作', stages: [5] as LifecycleStage[] },
  { key: 'monitoring' as StageGroupKey, label: '监测', stages: [6, 7, 8] as LifecycleStage[] },
  { key: 'renewal' as StageGroupKey, label: '续费', stages: [9] as LifecycleStage[] },
] as const;

export function getStageGroupKey(stage: LifecycleStage): StageGroupKey {
  if (stage <= 4) return 'sales';
  if (stage === 5) return 'writing';
  if (stage <= 8) return 'monitoring';
  return 'renewal';
}

export function getStageGroupLabel(stage: LifecycleStage): string {
  const k = getStageGroupKey(stage);
  return LIFECYCLE_STAGE_GROUPS.find((g) => g.key === k)?.label ?? '销售';
}

// ============================================================
// 报价状态(后端真实 5 态 · server.py 验证 2026-04-25)
// ============================================================

export const QUOTE_STATUS_VALUES = [
  'draft',            // 草稿 · 关键词未确认
  'confirmed',        // 客户在 /s/:token 选词入库后(走 /api/quotes/{quote_id}/confirm)
  'pending_payment',  // 代理走 offline-confirm 后(线下已确认 · 等付款)
  'paid',             // 走 offline-mark-paid · 同时 service_status='active'(后端合并 · UI 明示)
  'archived',         // 服务期结束 / 客户终止
] as const;
export type QuoteStatus = (typeof QUOTE_STATUS_VALUES)[number];

export const QUOTE_STATUS_LABEL: Record<QuoteStatus, string> = {
  draft: '草稿 · 关键词未确认',
  confirmed: '客户已确认关键词 · 等付款',
  pending_payment: '已确认订单 · 等收款',
  paid: '已付款 · 服务期已激活',
  archived: '已归档',
};

// ============================================================
// 服务期状态(quotes.service_status)
// ============================================================

export const SERVICE_STATUS_VALUES = ['inactive', 'active', 'expiring', 'expired', 'paused'] as const;
export type ServiceStatus = (typeof SERVICE_STATUS_VALUES)[number];

export const SERVICE_STATUS_LABEL: Record<ServiceStatus, string> = {
  inactive: '未激活',
  active: '服务中',
  expiring: '即将到期',
  expired: '已到期',
  paused: '已暂停',
};

// ============================================================
// 4 个独立状态变迁动作(老板要 audit log)
// ============================================================

export type LifecycleAction =
  | 'quote_confirm' // 客户线上选词确认 · 或代理线下确认
  | 'offline_confirm' // 代理在后台手动标记线下确认
  | 'mark_paid' // 标记已收款(微信支付回调 / 代理手工标记)
  | 'service_period_activate'; // 激活服务期(开始 service_start_date)

export const LIFECYCLE_ACTION_LABEL: Record<LifecycleAction, string> = {
  quote_confirm: '客户确认报价',
  offline_confirm: '线下已确认(代理手动)',
  mark_paid: '标记已付款',
  service_period_activate: '激活服务期',
};

// ============================================================
// 业务标签(给客户人工/自动打)
// ============================================================

export type BusinessTag = 'vip' | 'large' | 'slow' | 'trial';

export const BUSINESS_TAG_LABEL: Record<BusinessTag, string> = {
  vip: 'VIP',
  large: '大单',
  slow: '慢热',
  trial: '试水',
};

// ============================================================
// 风险等级(销售视角: 客户活跃度 / 流失风险)
// ============================================================

export type RiskLevel = 'stalled' | 'renewal' | 'ready' | 'ok' | 'warming';

export const RISK_LABEL: Record<RiskLevel, string> = {
  stalled: '停滞中',
  renewal: '续费窗',
  ready: '可成交',
  ok: '正常',
  warming: '预热中',
};

// ============================================================
// 客户实时信号(报价/方案打开追踪)
// ============================================================

export type SignalType =
  | 'opened'
  | 'duration'
  | 'scrolled_to_price'
  | 'forwarded'
  | 'reopened'
  | 'no_open_24h';

export const SIGNAL_LABEL: Record<SignalType, string> = {
  opened: '打开了链接',
  duration: '停留时长',
  scrolled_to_price: '看到报价部分',
  forwarded: '转发了链接',
  reopened: '再次打开链接',
  no_open_24h: '24h 未打开',
};

export interface CustomerSignal {
  type: SignalType;
  time: string;
  detail: string;
  urgent?: boolean;
}

// ============================================================
// 端(销售/交付)/ 视角(双视角切换)
// ============================================================

export type Endpoint = 'sales' | 'delivery';
export type Viewpoint = 'sales' | 'delivery';

// ============================================================
// 客户工作台聚合数据(M3Workbench API 返回)
// ============================================================

export interface ClientWorkbenchSnapshot {
  id: number;
  name: string;
  brand_code?: string;
  industry?: string;
  city?: string;
  contact?: string;
  weChat?: string;
  /** CTO-15.13 cleanup · 扩 legacy 兼容历史 brand · ?? 'client' fallback 保底 */
  brand_type: 'self' | 'client' | 'legacy';

  // 生命周期
  stage: LifecycleStage;
  stage_label: string;
  /** 简化完整度(legacy · 8 字段平均 · 给 stage rail 视觉用) */
  completeness: number; // 0-100 资料完整度
  /** SSOT 完整度(A.1 · 来自 /api/brands/{id}/completeness · null = 拉取失败 fallback) */
  completenessDetail: import('./api').BrandCompleteness | null;

  // 业务标签
  business_tag?: BusinessTag;
  risk: RiskLevel;
  timer?: string; // 显示用 · "27h" / "T-5"
  why?: string; // 为什么是现在 · 显式 mono tag

  // CTA
  primaryCta?: { label: string; api?: string; cost?: number; safety?: string };
  script?: string; // 推荐微信话术

  // stage-adaptive 数据
  diagnosis?: { id: number; total_score: number; level: string; created_at: string };
  /**
   * 真实 quote 数据(从 /api/quotes?brand_id=X&limit=1 拉)
   * 字段对齐 quotes 表 schema(server.py 验证 2026-04-25):
   *   - quotes 表无 sent_at · 用 created_at 替代
   *   - paid_amount = 月费 · monthly_price = 标价
   *
   * ⚠ token 链路澄清(C20 修 P0 错接):
   *   - portal_token = client_access_tokens.token · 用于 /portal/:token 客户门户(月报/数据看板)
   *     从 /api/portal/tokens/{quote_id} 拉
   *   - /q/:code 公开报价 = agent_quotes.share_code 体系(白标报价单)· 仅展示 · 无"确认"动作
   *     与 quotes 表无关 · 与 portal_token 无关 · 由 /agent/preview 生成
   *   - 客户关键词确认走 /s/:token 选词链路(SelectionPage)· 不是 /q/:code
   */
  quote?: {
    id: number;                       // 真 quote_id · 没有则不显示激活按钮(老板红线)
    status: QuoteStatus;
    service_status?: ServiceStatus;
    tier?: string;                     // entry / standard / flagship
    total_keywords?: number | null;
    monthly_price?: number;
    paid_amount?: number;              // 实际成交价 · offline-confirm 写入
    service_start_date?: string;
    service_end_date?: string;
    service_months?: number;
    created_at?: string;
    confirmed_at?: string;
    paid_at?: string;
    /**
     * 客户门户 token · 用于 /portal/:token(月报 / 数据看板)
     * ⚠ 不是 /q/:code 报价 share_code · 不能用于公开报价确认链路
     */
    portal_token?: string;
  };
  articles?: {
    total: number;
    done: number;
    drafting: number;
    pending: number;
    published?: number;
  };
  monitoring?: {
    running: boolean;
    summary?: {
      total: number;
      qualified: number;
      unqualified: number;
      paving?: number;
    };
    last_anomaly?: { keyword: string; from: number; to: number; engine?: string };
  };
  contract?: { start: string; end: string; days_to_expire: number };

  /**
   * Phase A.4 (CTO-15.11 2026-04-28):有 paid/active 主 quote + 还有新 draft 时返提示
   * 客户工作台顶部 banner "有新草稿 #X 待发" · 防 stage 倒退到"待报价"
   * null = 没新草稿(主 quote 本身是 draft 时也是 null · 因为 draft 是主 quote 不需要 banner)
   */
  draft_quote_hint?: {
    id: number;
    created_at?: string;
    total_keywords?: number;
    tier?: string;
    diagnosis_id?: number | null;
  } | null;

  /**
   * Phase A.6 (CTO-15.11 2026-04-28):新诊断未出报价感知
   * 老板痛点(40 岁老销售):跑了诊断 210 扣 650 · 客户工作台不感知 · 决策条仍卡"写作"
   * 修法:lifecycle 检测 latest_diagnosis 是否被任何 quote 引用 · 否则返 hint
   * 前端展示决策条 banner "🔥 你跑了新诊断 X/100 · 还没出新报价" + "出报价"按钮
   */
  diagnosis_pending_quote?: {
    diagnosis_id: number;
    total_score?: number | null;
    level?: string | null;
    created_at?: string;
  } | null;

  // 信号
  signals?: CustomerSignal[];
}

// ============================================================
// 服务期激活前置检查(老板红线: 不能伪装开工)
// ============================================================

export interface ServiceActivationGate {
  /** 报价已确认 */
  quoteConfirmed: boolean;
  /** 已收款(线下或线上) */
  paymentRecorded: boolean;
  /** 服务期已激活(service_start_date 已落 + service_status='active') */
  servicePeriodActive: boolean;
  /** 当前阶段是否被锁定(写/发/监不能开工) */
  deliveryLocked: boolean;
  /** 锁定原因(给 UI 显示人话) */
  lockReason?: string;
}
