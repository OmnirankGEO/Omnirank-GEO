/**
 * M3 API 聚合层
 *
 * 老板纠偏(2026-04-25):
 *   - 不能用 /api/client-context/list 推 quote · 必须拉真 /api/quotes
 *   - 不能用 quote_id=brand_id 占位
 *   - quote.status='sent' 占位错误 · 用真 status:draft/confirmed/pending_payment/paid/archived
 *   - service_status / service_start_date / service_end_date / paid_amount 全用真值
 *   - 没有真实 quote_id 时不允许显示可操作的激活按钮
 *
 * 后端真实 endpoint(已验证 server.py 2026-04-25):
 *   GET  /api/quotes?brand_id=X&limit=N         报价列表(过滤软删 deleted_at)
 *   GET  /api/quotes/{quote_id}                  报价详情({quote, keywords, topics})
 *   GET  /api/portal/tokens/{quote_id}           客户 portal token(用于 /portal/:token 链接)
 *   POST /api/quotes/{quote_id}/confirm          客户确认关键词 → status='confirmed'
 *   POST /api/quotes/{quote_id}/offline-confirm  代理线下确认 → status='pending_payment'
 *   POST /api/quotes/{quote_id}/offline-mark-paid 标记已付款 → status='paid' + service_status='active'(后端合并)
 *   POST /api/quotes/{quote_id}/service-period   设置/调整服务期(已 paid 后调整)
 */

import api from '@/lib/api';
import type { ClientBrandSummary, ClientContextDetail } from '@/lib/api';
import { getAuthorizationEpoch } from '@/lib/authoritativeSession';
import {
  inferStage,
  inferRisk,
  inferTimer,
  inferBusinessTag,
  computeActivationGate,
} from './lifecycleMapper';
import type {
  ClientWorkbenchSnapshot,
  CustomerSignal,
  Endpoint,
  LifecycleStage,
  QuoteStatus,
  ServiceStatus,
  ServiceActivationGate,
} from './types';
import { getStageLabel } from './types';
import { industryCategoryText } from '@/lib/industryTaxonomy';

/**
 * [WO_267] 行业大类显示名。本模块不是组件、拿不到字典 —— 三处来源(m3 客户列表 / 生命周期 /
 * client-context 详情)都附了 `industry_category_name`,直接用它;没有名字的英文 key 一律回空,
 * 绝不把 key 当行业显示。
 */
function categoryTextOf(raw?: string | null, name?: string | null): string | undefined {
  return industryCategoryText(raw, name, () => '') || undefined;
}

// ============================================================
// 真实 quote 类型(对齐 quotes 表 schema)
// ============================================================

export interface QuoteRecord {
  id: number;
  brand_id: number;
  brand_name?: string;
  industry?: string;
  city?: string;
  tier?: string;
  total_keywords?: number;
  total_articles?: number;
  monthly_price?: number;
  paid_amount?: number;
  status: QuoteStatus;
  confirmed_at?: string | null;
  paid_at?: string | null;
  created_at: string;
  service_start_date?: string | null;
  service_end_date?: string | null;
  service_months?: number;
  service_status?: ServiceStatus | null;
  monitoring_enabled?: boolean;
  source_type?: string;
  // Phase A.6 (CTO-15.11):后端 quotes.diagnosis_id 真值 · 前端用作"新诊断未出报价"判断
  diagnosis_id?: number | null;
}

/**
 * M1b · 6 层关键词 enum(CTO-15.17 · 2026-04-26)
 * 后端 services/keyword_layer_classifier.py · LAYER_LABEL_ZH 6 层中文 label 映射
 */
export type KeywordLayer =
  | 'brand_defense'
  | 'category_grab'
  | 'scenario_decision'
  | 'geo_conversion'
  | 'competitor_intercept'
  | 'evidence_trust';

export interface QuoteKeywordItem {
  id?: number;
  keyword: string;
  category?: string | null;
  tier?: string | null;
  final_price?: number;
  entry_price?: number;
  standard_price?: number;
  flagship_price?: number;
  required_articles?: number;
  intent?: string | null;
  // M1b · 6 层归属(GET 阶段必填 · 没真值时后端 on-the-fly 兜底)
  layer?: KeywordLayer;
  layer_label_zh?: string;
  layer_reason?: string;
  layer_confidence?: 'high' | 'medium' | 'low';
  expected_user_intent?: string;
  why_this_keyword?: string;
}

/**
 * M1b · 主题包(CTO-15.17)
 * 后端 services/theme_package_builder.ThemePackage
 * source: 'manual' = keyword_clusters 表真值(post-confirm)
 *        'auto'   = 算法生成(暂未启用)
 *        'fallback' = on-the-fly 按 layer 分组生成(fresh quote)
 */
export interface QuoteThemePackage {
  package_name: string;
  primary_layer: KeywordLayer;
  primary_layer_label_zh: string;
  problem_to_solve: string;
  target_customer_question: string;
  keywords: string[];
  keyword_layers: KeywordLayer[];
  article_count: number;
  target_platforms: string[];
  monitoring_keywords: string[];
  expected_cycle: string;
  pricing_component: number;
  evidence_reason: string;
  confidence: 'high' | 'medium' | 'low';
  source: 'auto' | 'manual' | 'fallback';
  layer_ratio_hint: string;
}

export interface QuoteDetail {
  quote: QuoteRecord;
  keywords?: QuoteKeywordItem[];
  topics?: Array<{ id: number; title?: string; optimized_title?: string }>;
  // M1b · 6 层关键词矩阵 + 主题包(老前端忽略 · 新前端 M3 方案页 消费)
  theme_packages?: QuoteThemePackage[];
  layer_distribution?: Partial<Record<KeywordLayer, number>>;
}

// ============================================================
// 真 quote API · 老板红线"全部填入真值"
// ============================================================

/**
 * 拉品牌的报价列表 · 默认按 created_at DESC
 */
export async function getQuotesByBrand(brandId: number, limit = 5): Promise<QuoteRecord[]> {
  try {
    const res = await api.get<{ total: number; items: QuoteRecord[] }>(
      `/api/quotes?brand_id=${brandId}&limit=${limit}`,
    );
    return res.data?.items || [];
  } catch {
    return [];
  }
}

/**
 * 拉单个 quote 详情(含 keywords + topics)
 */
export async function getQuoteDetail(quoteId: number): Promise<QuoteDetail | null> {
  try {
    const res = await api.get<QuoteDetail>(`/api/quotes/${quoteId}`);
    return res.data ?? null;
  } catch {
    return null;
  }
}

/**
 * 拉客户门户 token(用于 /portal/:token 客户门户 · 月报/数据看板)
 *
 * ⚠ C20 修 P0 错接:此 token 不是 /q/:code 公开报价 share_code
 * /q/:code 公开报价走 agent_quotes.share_code 体系 · 与 quotes 表无关
 *
 * Bug #2 P0 (2026-04-27 staging Playwright 验收发现):
 *   后端 GET /api/portal/tokens/{quote_id} 真实返回是 NESTED 结构:
 *     {"status":"success","token":{"id":N,"quote_id":Q,"token":"Y6_...","is_active":1,"expires_at":"..."}}
 *   data.token 是对象 · 不是 string · 老代码 `data.token || data.portal_token` 返回对象
 *   调用方拼链接得到 `/portal/[object Object]` · M3 方案页 验收发现
 *   修法:嵌套对象 → 取 .token 真 string · 兼容历史 string-only 返回
 */
export async function getPortalToken(quoteId: number): Promise<string | null> {
  try {
    const res = await api.get<{
      token?: string | { token?: string; expires_at?: string; id?: number; is_active?: number };
      portal_token?: string;
      status?: string;
    }>(`/api/portal/tokens/${quoteId}`);
    const t = res.data?.token;
    if (t && typeof t === 'object') {
      return t.token || null;
    }
    if (typeof t === 'string' && t) return t;
    if (res.data?.portal_token) return res.data.portal_token;
    return null;
  } catch {
    return null;
  }
}

async function loadMonitoringSummary(
  quoteId?: number,
): Promise<NonNullable<ClientWorkbenchSnapshot['monitoring']>['summary'] | null> {
  if (!quoteId) return null;
  try {
    const res = await api.get<{
      status: string;
      keywords?: Array<{
        is_compliant?: boolean;
        lifecycle?: string;
      }>;
    }>(`/api/monitoring/clients/${quoteId}/keywords`);
    if (res.data?.status !== 'success' || !Array.isArray(res.data.keywords)) return null;
    const list = res.data.keywords;
    if (list.length === 0) return null;
    const paving = list.filter((kw) => kw.lifecycle === 'deploying').length;
    const qualified = list.filter((kw) => kw.is_compliant === true).length;
    const unqualified = list.length - qualified - paving;
    return {
      total: list.length,
      qualified,
      unqualified: Math.max(0, unqualified),
      paving,
    };
  } catch {
    return null;
  }
}

// ============================================================
// 客户列表(销售端 / 交付端 · 共用)
// ============================================================

export interface BrandCompleteness {
  /** 0-100 分(SSOT 算法 utils/brand_completeness.py) */
  score: number;
  /** 缺失字段名列表(给 UI 提示用) */
  missing: string[];
  /** identity / business / marketing / deep_analysis / market_insight 5 组分项 */
  groups?: {
    identity?: number;
    business?: number;
    marketing?: number;
    deep_analysis?: number;
    market_insight?: number;
  };
  industry_brief_state?: string;
}

export interface SalesClientListItem {
  id: number;                  // brand_id
  name: string;
  brand_code?: string;
  industry?: string;
  /** 城市/服务区域(M3 BFF 反 cities 字段 · Fix 3 · 关键词建议生成需要) */
  city?: string;
  stage: LifecycleStage;
  stage_label: string;
  risk: ReturnType<typeof inferRisk>;
  timer?: string;
  diagnosis_count: number;
  latest_score?: number;
  /** 最新诊断 id · A3.3 报价生成入口需要(/m3/quote/new?diagnosis_id=X) */
  latest_diagnosis_id?: number | null;
  /** 真实 quote.status · 没 quote 时 null */
  quote_status: QuoteStatus | null;
  /** 真实 quote_id · 没 quote 时 null · UI 据此判断能否显示激活按钮(老板红线) */
  quote_id: number | null;
  /** 最新 quote 服务状态 · monitor-detail fallback 需要判断 active 服务期 */
  quote_service_status?: ServiceStatus | null;
  /** 最新 quote 档位 · monitor-detail fallback 显示用 */
  quote_tier?: string | null;
  business_tag?: ReturnType<typeof inferBusinessTag>;
  /** 资料完整度(E.1 · 来自 /api/m3/customers · 老 fallback 时为 null) */
  completeness: BrandCompleteness | null;
  /** brand_type 用于阻断 Dialog 决定跳 /brands/:id (self) 还是 /my-clients/:id (client) */
  brand_type?: 'self' | 'client' | 'legacy';
  /** A.2/A.7 (CTO-15.18 · 2026-04-28):测试客户隔离 · 真客户 false / 测试 true */
  is_test?: boolean;
}

// ============================================================
// E.1 /api/m3/customers 新 endpoint 返回类型(M3 BFF · 解 N+1)
// ============================================================

interface M3CustomerLatestQuote {
  id: number;
  status: QuoteStatus;
  paid_amount?: number | null;
  monthly_price?: number | null;
  service_status?: ServiceStatus | null;
  service_start_date?: string | null;
  service_end_date?: string | null;
  service_months?: number;
  confirmed_at?: string | null;
  paid_at?: string | null;
  created_at?: string | null;
  tier?: string;
  total_keywords?: number;
  source_type?: string;
  monitoring_enabled?: boolean;
}

interface M3CustomerRow {
  id: number;
  name: string;
  brand_code?: string;
  industry?: string;
  industry_category?: string;
  /** [WO_267] m3 客户列表 / 生命周期端点附的大类中文名 */
  industry_category_name?: string | null;
  company_name?: string;
  cities?: string;
  brand_type?: string;
  diagnosis_count: number;
  latest_score?: number;
  latest_diagnosis_id?: number | null;
  /** A.2 (CTO-15.18 · 2026-04-28):测试客户隔离 · 真客户 false / 测试 true */
  is_test?: boolean;
  created_at?: string;
  updated_at?: string;
  latest_quote: M3CustomerLatestQuote | null;
  completeness: BrandCompleteness;
}

/**
 * 销售端客户漏斗 — 优先调 /api/m3/customers 聚合(E.1)· 失败 fallback 老 endpoint 不做 N+1
 *
 * E.1 实施(2026-04-25 CTO-15.13):
 *   - 新 endpoint /api/m3/customers 一次返 brand+latest_quote+completeness+diagnosis_count
 *
 * P0 修(2026-04-26 老板拍板):
 *   - 老 fallback 旧版 Promise.all(49 个 /api/quotes?brand_id=X) 直接打爆 DB pool · 必须移除
 *   - 改为 fallback 仅 /api/client-context/list 基础字段 · quote=null · stage 推断 = inquiry
 *   - degraded 状态写 sessionStorage 'm3.list-clients.degraded' · UI 顶部显示 banner
 *   - 这条 fallback 路径仅在 /api/m3/customers 5xx 时走 · 正常情况不走
 */
export const M3_LIST_CLIENTS_DEGRADED_KEY = 'm3.list-clients.degraded';

function markDegraded(reason: string): void {
  if (typeof window === 'undefined') return;
  try {
    sessionStorage.setItem(M3_LIST_CLIENTS_DEGRADED_KEY, reason);
  } catch {
    // 静默
  }
}

function clearDegraded(): void {
  if (typeof window === 'undefined') return;
  try {
    sessionStorage.removeItem(M3_LIST_CLIENTS_DEGRADED_KEY);
  } catch {
    // 静默
  }
}

export function isM3ListClientsDegraded(): { degraded: boolean; reason: string | null } {
  if (typeof window === 'undefined') return { degraded: false, reason: null };
  try {
    const v = sessionStorage.getItem(M3_LIST_CLIENTS_DEGRADED_KEY);
    return { degraded: !!v, reason: v };
  } catch {
    return { degraded: false, reason: null };
  }
}

export async function listClients(opts?: { includeTest?: boolean }): Promise<SalesClientListItem[]> {
  // E.1 优先调新聚合 endpoint · A.7 加 ?include_test query
  const includeTest = opts?.includeTest === true;
  const url = includeTest ? '/api/m3/customers?include_test=true' : '/api/m3/customers';
  try {
    const res = await api.get<{ success: boolean; customers: M3CustomerRow[] }>(url);
    if (res.data?.success && Array.isArray(res.data.customers)) {
      clearDegraded();
      return res.data.customers.map(m3CustomerRowToSalesItem);
    }
    throw new Error('m3 customers · response success=false');
  } catch (err) {
    const reason = (err as Error)?.message || 'm3 customers fail';
    markDegraded(reason);
    // 仅走基础字段 fallback · 绝不再做 N+1 quote 拉取(P0 · 49 个 /api/quotes 会打爆 DB pool)
    return listClientsBasicFallback();
  }
}

function m3CustomerRowToSalesItem(c: M3CustomerRow): SalesClientListItem {
  const lifecycle = {
    brand: { id: c.id },
    diagnosis:
      c.diagnosis_count > 0
        ? {
            id: c.latest_diagnosis_id ?? 0,
            total_score: c.latest_score ?? 0,
            level: '',
            created_at: c.created_at ?? '',
          }
        : null,
    quote: c.latest_quote
      ? {
          id: c.latest_quote.id,
          status: c.latest_quote.status,
          service_status: c.latest_quote.service_status ?? null,
          service_start_date: c.latest_quote.service_start_date ?? null,
          service_end_date: c.latest_quote.service_end_date ?? null,
          confirmed_at: c.latest_quote.confirmed_at ?? null,
          paid_at: c.latest_quote.paid_at ?? null,
          amount: c.latest_quote.paid_amount ?? c.latest_quote.monthly_price ?? 0,
        }
      : null,
  };
  const stage = inferStage(lifecycle);
  return {
    id: c.id,
    name: c.name,
    brand_code: c.brand_code,
    industry: c.industry || categoryTextOf(c.industry_category, c.industry_category_name),
    city: c.cities ?? undefined, // Fix 3 · M3 关键词建议块 需要
    stage,
    stage_label: getStageLabel(stage),
    risk: inferRisk(lifecycle),
    timer: inferTimer(lifecycle),
    diagnosis_count: c.diagnosis_count,
    latest_score: c.latest_score,
    latest_diagnosis_id: c.latest_diagnosis_id ?? null,
    quote_status: c.latest_quote?.status ?? null,
    quote_id: c.latest_quote?.id ?? null,
    quote_service_status: c.latest_quote?.service_status ?? null,
    quote_tier: c.latest_quote?.tier ?? null,
    business_tag: inferBusinessTag(lifecycle),
    completeness: c.completeness ?? null,
    brand_type: (c.brand_type as 'self' | 'client' | 'legacy') ?? 'legacy',
    is_test: !!c.is_test,
  };
}

/**
 * P0 fallback(2026-04-26 老板拍板):
 *   - /api/m3/customers 失败时调用 · 仅返 brand 基础字段
 *   - 严禁 Promise.all N+1 拉 quote(BLOCK-1 · 49 个并发 /api/quotes 打爆 DB pool)
 *   - quote 信息全 null · stage 推断会落到 inquiry/diagnosing(无 quote 时)
 *   - degraded 标记已经在 listClients 入口写了 · 这里不再重复写
 *   - 调用方应该读 isM3ListClientsDegraded() 显示 "部分数据加载失败 · 重试" banner
 *   - 老 listClientsLegacy 已删除(N+1 是反模式 · 不再保留)
 */
async function listClientsBasicFallback(): Promise<SalesClientListItem[]> {
  const res = await api.get<{ success: boolean; clients: ClientBrandSummary[] }>(
    '/api/client-context/list',
  );
  const list = res.data.clients || [];

  return list.map((c) => {
    const lifecycle = {
      brand: { id: c.id },
      diagnosis: c.latest_diagnosis_id
        ? {
            id: c.latest_diagnosis_id,
            total_score: c.latest_score ?? 0,
            level: '',
            created_at: c.created_at,
          }
        : null,
      quote: null, // P0 · 不再 N+1 拉 · UI 显式 degraded
    };
    const stage = inferStage(lifecycle);
    return {
      id: c.id,
      name: c.name,
      brand_code: c.brand_code,
      industry: c.industry || categoryTextOf(c.industry_category, c.industry_category_name),
      stage,
      stage_label: getStageLabel(stage),
      risk: inferRisk(lifecycle),
      timer: inferTimer(lifecycle),
      diagnosis_count: c.diagnosis_count,
      latest_score: c.latest_score,
      latest_diagnosis_id: c.latest_diagnosis_id ?? null,
      quote_status: null,
      quote_id: null,
      business_tag: inferBusinessTag(lifecycle),
      completeness: null,
      brand_type:
        ((c as unknown as { brand_type?: 'self' | 'client' | 'legacy' }).brand_type) ?? 'legacy',
    };
  });
}

// ============================================================
// 销售端今日(1+2 金字塔 · 6 chip 数据)
// ============================================================

export interface SalesTodaySnapshot {
  total: number;
  chips: {
    all: number;
    follow_up: number;
    new_inquiry: number;
    diagnosing: number;
    quote_pending: number;
    quote_sent: number;
    renewal: number;
  };
  primary?: SalesClientListItem;
  secondary: SalesClientListItem[];
  /** Deploy-CTO r4 (2026-04-27 · cherry-pick from b9dcdef): 暴露 raw list 给 UI 按 chip 客户端过滤 */
  list: SalesClientListItem[];
}

export async function getSalesToday(): Promise<SalesTodaySnapshot> {
  const list = await listClients();

  const chips = {
    all: list.length,
    follow_up: list.filter((c) => c.risk === 'stalled' || c.risk === 'renewal').length,
    new_inquiry: list.filter((c) => c.stage === 1).length,
    diagnosing: list.filter((c) => c.stage === 2).length,
    quote_pending: list.filter((c) => c.stage === 3).length,
    quote_sent: list.filter((c) => c.stage === 4).length,
    renewal: list.filter((c) => c.stage === 9).length,
  };

  const sorted = [...list].sort((a, b) => stagePriorityScore(b) - stagePriorityScore(a));
  const [primary, ...rest] = sorted;
  // Polish 2 (2026-04-26):secondary 上限 2 → 12 · 老板"销售优先级尽量覆盖全量队列"
  // 显示层 M3 今日销售页 用 sortedSecondary 全展(可滚)· 性能兜底 12
  const secondary = rest.slice(0, 12);

  return { total: list.length, chips, primary, secondary, list };
}

/**
 * M3 今日销售页 顶部 7 chip 类型 (Deploy-CTO r4 · cherry-pick from b9dcdef)
 */
export type SalesTodayChipKey =
  | 'all'
  | 'follow_up'
  | 'new_inquiry'
  | 'diagnosing'
  | 'quote_pending'
  | 'quote_sent'
  | 'renewal';

/**
 * 按 chip key 过滤客户列表(M3 今日销售页 顶部 7 chip 切换用)
 * Deploy-CTO r4 (2026-04-27 · cherry-pick from b9dcdef):chip 点击之前只切 active 视觉 · 现真实过滤
 */
export function filterSalesTodayByChip(
  list: SalesClientListItem[],
  chip: SalesTodayChipKey,
): SalesClientListItem[] {
  switch (chip) {
    case 'all':
      return list;
    case 'follow_up':
      return list.filter((c) => c.risk === 'stalled' || c.risk === 'renewal');
    case 'new_inquiry':
      return list.filter((c) => c.stage === 1);
    case 'diagnosing':
      return list.filter((c) => c.stage === 2);
    case 'quote_pending':
      return list.filter((c) => c.stage === 3);
    case 'quote_sent':
      return list.filter((c) => c.stage === 4);
    case 'renewal':
      return list.filter((c) => c.stage === 9);
    default:
      return list;
  }
}

export function stagePriorityScore(c: SalesClientListItem): number {
  switch (c.risk) {
    case 'stalled':
      return 100;
    case 'renewal':
      return 90;
    case 'ready':
      return 70;
    case 'warming':
      return 30;
    default:
      return 10;
  }
}

// ============================================================
// 客户工作台聚合(stage-adaptive 7 stage 数据)
// ============================================================

const clientWorkbenchInFlight = new Map<string, Promise<ClientWorkbenchSnapshot>>();

export function getClientWorkbench(
  brandId: number,
  contextOverride?: ClientContextDetail | null,
): Promise<ClientWorkbenchSnapshot> {
  // DecisionBarBridge can re-render when ClientContext finishes hydrating. Both
  // renders describe the same authority + brand, so share the whole pending
  // bootstrap instead of repeating its quote/completeness waterfall. There is
  // deliberately no settled cache here: mutations and later reads always see
  // a fresh server result.
  const key = `${getAuthorizationEpoch()}:${brandId}`;
  const existing = clientWorkbenchInFlight.get(key);
  if (existing) return existing;
  const request = loadClientWorkbench(brandId, contextOverride);
  clientWorkbenchInFlight.set(key, request);
  request.then(
    () => { if (clientWorkbenchInFlight.get(key) === request) clientWorkbenchInFlight.delete(key); },
    () => { if (clientWorkbenchInFlight.get(key) === request) clientWorkbenchInFlight.delete(key); },
  );
  return request;
}

async function loadClientWorkbench(
  brandId: number,
  contextOverride?: ClientContextDetail | null,
): Promise<ClientWorkbenchSnapshot> {
  // 1. 客户档案(industry / city / contact / wechat 等)
  const ctx = contextOverride?.brand.id === brandId
    ? contextOverride
    : (await api.get<{
    success: boolean;
    context: ClientContextDetail;
  }>(`/api/client-context/${brandId}`)).data.context;
  const profile = ctx.profile || {};
  const latestDiagnosisId = ctx.brand.latest_diagnosis_id ?? null;
  const latestDiagnosisCreatedAt = ctx.brand.latest_diagnosis_created_at ?? '';

  // 2. 真 quote(老板红线:不再用 client-context 推 status='sent' 占位)
  // Phase A.4 (CTO-15.11 2026-04-28):修 stage 倒退
  //   老 limit=1 + 后端默认 created_at DESC → 新建 draft 覆盖 active quote → stage 退回"待报价"
  //   新:拉 10 条 + 客户端按优先级选(active > paid > pending_payment > confirmed > draft)
  //       同优先级再按 created_at DESC
  //   并:如果主 quote 已 paid/active 但还有更新的 draft → 返 draft_quote_hint banner
  const quotes = await getQuotesByBrand(brandId, 10);
  const latest = pickPrimaryQuote(quotes);
  const draftHint = pickDraftQuoteHint(quotes, latest);

  // Phase A.6 (CTO-15.11):新诊断未出报价感知
  // 客户最近一次诊断有没有被任何 quote 引用 · 没引用则提示代理"出新报价"
  let diagnosisPendingQuote: ClientWorkbenchSnapshot['diagnosis_pending_quote'] = null;
  if (latestDiagnosisId && latestDiagnosisId > 0) {
    const usedByQuote = quotes.some((q) => q.diagnosis_id === latestDiagnosisId);
    if (!usedByQuote) {
      diagnosisPendingQuote = {
        diagnosis_id: latestDiagnosisId,
        total_score: ctx.brand.latest_score ?? null,
        level: null,
        created_at: latestDiagnosisCreatedAt || undefined,
      };
    }
  }

  // 3. 客户门户 token(/portal/:token 客户长期门户 · 月报/数据看板)
  // ⚠ 这不是 /q/:code 公开报价 share_code(那是 agent_quotes 体系)
  let portalToken: string | null = null;
  if (latest) {
    portalToken = await getPortalToken(latest.id);
  }

  const monitoringSummary = await loadMonitoringSummary(latest?.id);

  // 4. SSOT 完整度(A.1 · /api/brands/{id}/completeness · 失败 fallback null)
  let completenessDetail: BrandCompleteness | null = null;
  try {
    const compRes = await api.get<{
      score?: number;
      groups?: BrandCompleteness['groups'];
      missing?: string[];
    }>(`/api/brands/${brandId}/completeness`);
    if (compRes.data && typeof compRes.data.score === 'number') {
      completenessDetail = {
        score: compRes.data.score,
        missing: compRes.data.missing ?? [],
        groups: compRes.data.groups,
      };
    }
  } catch {
    // 静默 fallback · UI 显示 null 时不阻断
  }

  const lifecycle = {
    brand: { id: ctx.brand.id },
    diagnosis: ctx.brand.diagnosis_count > 0
      ? {
          id: latestDiagnosisId ?? 0,
          total_score: ctx.brand.latest_score ?? 0,
          level: '',
          created_at: latestDiagnosisCreatedAt,
        }
      : null,
    quote: latest
      ? {
          id: latest.id,
          status: latest.status,
          service_status: latest.service_status ?? null,
          service_start_date: latest.service_start_date ?? null,
          service_end_date: latest.service_end_date ?? null,
          confirmed_at: latest.confirmed_at ?? null,
          paid_at: latest.paid_at ?? null,
          amount: latest.paid_amount ?? latest.monthly_price ?? 0,
        }
      : null,
    monitoring: monitoringSummary ? { running: true, summary: monitoringSummary } : null,
  };

  const stage = inferStage(lifecycle);
  // CTO-15.18 A.4 · 资料完整度全站 SSOT 统一(老板裁决全站一个数字)
  // 优先 SSOT(/api/brands/:id/completeness · 后端 utils/brand_completeness.py)
  // 老 legacy computeCompleteness(profile) 仅 fallback(SSOT 拉取失败时)
  const completeness = completenessDetail?.score ?? computeCompleteness(profile);

  // 服务期到期天数计算(用于 contract.days_to_expire)
  let contract: ClientWorkbenchSnapshot['contract'] | undefined;
  if (latest?.service_start_date && latest?.service_end_date) {
    const end = new Date(latest.service_end_date).getTime();
    const days = Math.ceil((end - Date.now()) / (1000 * 60 * 60 * 24));
    contract = {
      start: latest.service_start_date,
      end: latest.service_end_date,
      days_to_expire: days,
    };
  }

  return {
    id: ctx.brand.id,
    name: ctx.brand.name,
    brand_code: ctx.brand.brand_code,
    industry: ctx.brand.industry || categoryTextOf(ctx.brand.industry_category, ctx.brand.industry_category_name),
    city: typeof profile.city === 'string' ? profile.city : (latest?.city ?? undefined),
    contact: typeof profile.contact === 'string' ? profile.contact : undefined,
    weChat: typeof profile.wechat === 'string' ? profile.wechat : undefined,
    brand_type: ctx.brand.brand_type ?? 'client',
    stage,
    stage_label: getStageLabel(stage),
    // Phase A.5 (CTO-15.11):全站完整度 SSOT
    // 优先用 /api/brands/:id/completeness 返的 SSOT 分数(M1c v1.0 算法 · 100 分制 5 组)
    // SSOT 失败才降级 legacy 8 字段 computeCompleteness(profile) — 不再让 13/47 数字打架
    completeness:
      typeof completenessDetail?.score === 'number' ? completenessDetail.score : completeness,
    completenessDetail,
    business_tag: inferBusinessTag(lifecycle),
    risk: inferRisk(lifecycle),
    timer: inferTimer(lifecycle),
    why: undefined,
    diagnosis: ctx.brand.diagnosis_count > 0
      ? {
          id: latestDiagnosisId ?? 0,
          total_score: ctx.brand.latest_score ?? 0,
          level: '',
          created_at: latestDiagnosisCreatedAt,
        }
      : undefined,
    quote: latest
      ? {
          id: latest.id,
          status: latest.status,
          service_status: latest.service_status ?? undefined,
          tier: latest.tier,
          monthly_price: latest.monthly_price,
          paid_amount: latest.paid_amount,
          service_start_date: latest.service_start_date ?? undefined,
          service_end_date: latest.service_end_date ?? undefined,
          service_months: latest.service_months,
          created_at: latest.created_at,
          confirmed_at: latest.confirmed_at ?? undefined,
          paid_at: latest.paid_at ?? undefined,
          portal_token: portalToken ?? undefined,
        }
      : undefined,
    monitoring: monitoringSummary
      ? {
          running: true,
          summary: monitoringSummary,
        }
      : undefined,
    contract,
    draft_quote_hint: draftHint,
    diagnosis_pending_quote: diagnosisPendingQuote,
  };
}

// Phase A.4 (CTO-15.11):quote 优先级排序 · 跟后端 _BRAND_LATERAL ORDER BY 同口径
//   1) service_status='active' (合同执行中) > 2) status='paid' > 3) 'pending_payment' >
//   4) 'confirmed' > 5) 其他(draft 等)· 同档按 created_at DESC
function _quotePriorityKey(q: QuoteRecord): number {
  if (q.service_status === 'active') return 0;
  if (q.status === 'paid') return 1;
  if (q.status === 'pending_payment') return 2;
  if (q.status === 'confirmed') return 3;
  return 4;
}

function pickPrimaryQuote(quotes: QuoteRecord[]): QuoteRecord | undefined {
  if (!quotes || quotes.length === 0) return undefined;
  const sorted = [...quotes].sort((a, b) => {
    const pa = _quotePriorityKey(a);
    const pb = _quotePriorityKey(b);
    if (pa !== pb) return pa - pb;
    const ta = new Date(a.created_at || 0).getTime();
    const tb = new Date(b.created_at || 0).getTime();
    return tb - ta; // newer first
  });
  return sorted[0];
}

function pickDraftQuoteHint(
  quotes: QuoteRecord[],
  primary: QuoteRecord | undefined,
): ClientWorkbenchSnapshot['draft_quote_hint'] {
  if (!primary || !quotes) return null;
  // 主 quote 必须是 paid/pending_payment/confirmed(active 也是 paid)· draft 主 quote 时无需 banner
  const primaryAdvanced =
    primary.status === 'paid' ||
    primary.status === 'pending_payment' ||
    primary.status === 'confirmed';
  if (!primaryAdvanced) return null;
  // 找最新的 draft(不能是 primary 本身)
  const drafts = quotes
    .filter((q) => q.status === 'draft' && q.id !== primary.id)
    .sort((a, b) => new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime());
  const draft = drafts[0];
  if (!draft) return null;
  return {
    id: draft.id,
    created_at: draft.created_at,
    total_keywords: draft.total_keywords ?? undefined,
    tier: draft.tier,
    diagnosis_id: (draft as unknown as { diagnosis_id?: number | null }).diagnosis_id ?? null,
  };
}

function computeCompleteness(profile: Record<string, string | number | boolean | null>): number {
  const fields = [
    'industry', 'city', 'contact', 'wechat', 'company_name',
    'target_users', 'selling_points', 'business_scope',
  ];
  const filled = fields.filter((f) => {
    const v = profile[f];
    return typeof v === 'string' ? v.trim().length > 0 : !!v;
  }).length;
  return Math.round((filled / fields.length) * 100);
}

// ============================================================
// 服务期激活闸门(老板红线 · 用真 quote 数据)
// ============================================================

export async function getActivationGate(brandId: number): Promise<ServiceActivationGate> {
  const quotes = await getQuotesByBrand(brandId, 1);
  const latest = quotes[0];
  const lifecycle = {
    brand: { id: brandId },
    quote: latest
      ? {
          id: latest.id,
          status: latest.status,
          service_status: latest.service_status ?? null,
          service_start_date: latest.service_start_date ?? null,
          service_end_date: latest.service_end_date ?? null,
        }
      : null,
  };
  return computeActivationGate(lifecycle);
}

// ============================================================
// 实时信号 · 明确未实装(老板 C17 红线:不假装"6 类信号已完成")
// ============================================================

/**
 * 客户实时信号(打开 / 看到价格 / 提交 / 续费意向)
 *
 * CTO-C 2026-04-26 接通 (feat/m3-customer-signals):
 *   /api/m3/customer-events?quote_id=X · 真实埋点数据
 *
 * 老板红线:
 *   - endpoint 失败 → 返 null · UI 显式 "信号暂不可用"
 *   - 没事件 → 返 [] · UI 显示 "暂无客户行为信号"
 *
 * 注意:
 *   - M3 信号时间线 组件直接用 fetchSignalsEvents (走 brand_id 而非 quote_id)
 *   - 此函数保留兼容旧 quoteId 入参 · 内部转去拉相同 endpoint
 */
export async function getCustomerSignals(quoteId: number | string): Promise<CustomerSignal[] | null> {
  try {
    const id = typeof quoteId === 'string' ? parseInt(quoteId, 10) : quoteId;
    if (!id || Number.isNaN(id)) return null;
    const params = new URLSearchParams();
    params.set('quote_id', String(id));
    params.set('days', '30');
    params.set('limit', '50');
    const res = await api.get<{
      success?: boolean;
      events?: Array<{
        id: number;
        source: string;
        event_type: string;
        occurred_at: string;
      }>;
    }>(`/api/m3/customer-events?${params.toString()}`);
    if (!res.data?.success || !Array.isArray(res.data.events)) return null;

    // 映射 (source, event_type) → 老 SignalType 词汇 (用于兼容旧消费方)
    const TYPE_MAP: Record<string, CustomerSignal['type']> = {
      'public_quote.opened': 'opened',
      'public_quote.dwell_30s': 'duration',
      'public_quote.dwell_120s': 'duration',
      'public_quote.saw_price': 'scrolled_to_price',
      'public_quote.cta_click': 'forwarded',
      'public_report.opened': 'opened',
      'public_report.dwell_30s': 'duration',
      'public_report.dwell_120s': 'duration',
      'public_report.cta_click': 'forwarded',
      'selection.opened': 'opened',
      'selection.dwell_30s': 'duration',
      'selection.dwell_120s': 'duration',
      'selection.submitted_keywords': 'forwarded',
      'portal.opened': 'opened',
      'portal.dwell_30s': 'duration',
      'portal.dwell_120s': 'duration',
      'portal.renewed_interest': 'forwarded',
    };

    const out: CustomerSignal[] = [];
    for (const ev of res.data.events) {
      const key = `${ev.source}.${ev.event_type}`;
      const t = TYPE_MAP[key];
      if (!t) continue;
      out.push({
        type: t,
        time: ev.occurred_at,
        detail: key,
      });
    }
    return out;
  } catch {
    return null;
  }
}

// ============================================================
// 集中导出
// ============================================================

export const m3Api = {
  listClients,
  getSalesToday,
  getClientWorkbench,
  getActivationGate,
  getCustomerSignals,
  getQuotesByBrand,
  getQuoteDetail,
  getPortalToken,
};

export type { Endpoint };
