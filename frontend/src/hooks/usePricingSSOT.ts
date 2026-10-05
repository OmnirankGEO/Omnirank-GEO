/**
 * usePricingSSOT — 双 SSOT 定价前端数据钩子(2026-07-12)
 *
 * 只读后端价格,浏览器永不计算最终价 / 积分。
 * 后端端点默认关(feature flag OFF)时返 503 {code:'SSOT_DISABLED'},
 * 本钩子把 503 / 409 归一成 typed 错误码,让调用方优雅降级(回退旧页 / 显示引导)。
 *
 * 覆盖:
 *   - 零售(购买算力):getRetailCatalog / createRetailQuote
 *   - 进货(可售库存):getProcurementCatalog / createProcurementQuote
 *   - 管理端(定价中心):listVersions / createDraft / validateDraft / publish / rollback
 */
import { useMemo } from 'react';
import type { AxiosResponse } from 'axios';
import api, { readDetailContract } from '@/lib/api';
import { getGeoPricingEntryContract } from '@/lib/pricingEntryContracts';

const CUSTOMER_RECHARGE_ENTRY = getGeoPricingEntryContract('customer-recharge');
const AGENT_INVENTORY_ENTRY = getGeoPricingEntryContract('agent-inventory-purchase');

// ==========================================
// 错误码(把后端 503 / 409 归一成 typed 状态)
// ==========================================

export type PricingErrorCode =
  | 'SSOT_DISABLED'          // 503 · flag 关 → 回退旧页
  | 'ACCOUNT_CONFIGURATION_UNAVAILABLE'
  | 'ACCOUNT_CONFIGURATION_REVIEW_REQUIRED'
  | 'PRICE_CONFIGURATION_UNAVAILABLE'
  | 'PLATFORM_DIRECT_NOT_READY' // 409 · 无显式绑定且平台直营未就绪
  /*
   * [WO_243 甲] 409 · 平台直营账号不需要进货(WO_241 甲)。
   * 🔴 **别和上一行搞混**:`PLATFORM_DIRECT_NOT_READY` 是「平台直营**未就绪**」=
   *    一种故障;这一条是「这个账号**本来就不进货**」= 正常态。
   *    两个名字只差一个词、同为 409,认错了会把一次故障渲染成一句安抚话
   *    (反过来会把正常态报成故障)。判据里按**整串**钉,别按前缀。
   */
  | 'PLATFORM_DIRECT_NO_PROCUREMENT'
  | 'NO_PUBLISHED_RETAIL'    // 409 · 无已发布零售目录
  | 'NO_PUBLISHED_PROCUREMENT'
  | 'QUOTE_NOT_FOUND'
  | 'QUOTE_EXPIRED'
  | 'QUOTE_USED'
  | 'QUOTE_CONSUMED'
  | 'QUOTE_TYPE_MISMATCH'
  | 'QUOTE_MISMATCH'
  | 'UNKNOWN';

export interface PricingError {
  ok: false;
  status: number;
  code: PricingErrorCode;
  message: string;
}

export type PricingResult<T> = { ok: true; data: T } | PricingError;

// ==========================================
// 后端返回结构(严格照端点契约,不多不少)
// ==========================================

export interface RetailCatalogItem {
  product_code: string;
  display_name: string;
  subtitle?: string | null;
  sales_pitch?: string | null;
  scene?: string | null;
  final_price_cents: number;
  points_granted: number;
  bonus_points: number;
  usage_examples: string[];
  resale_mode?: boolean;
}

export interface PlatformServiceStatus {
  service_status: 'platform_managed';
  configuration_status: 'ready' | 'review_required' | 'unavailable';
  account_configured?: boolean;
  dispute_pending?: boolean;
}

export interface RetailCatalog {
  seller_label: string;
  service: PlatformServiceStatus;
  catalog_version: string;
  items: RetailCatalogItem[];
  resale_mode?: boolean;
  consumer_policy_code?: string;
  digital_goods_notice?: string;
  requires_digital_goods_acknowledgement?: boolean;
}

export interface RetailQuote {
  quote_id: string;
  final_price_cents: number;
  points_granted: number;
  bonus_points: number;
  currency: string;
  price_valid_until: string;
  service: PlatformServiceStatus;
  seller_label: string;
  resale_mode?: boolean;
  consumer_policy_code?: string;
  digital_goods_acknowledged?: boolean;
  amount_source?: 'customer_entered_cash';
}

export interface ProcurementCatalogItem {
  product_code: string;
  display_name: string;
  cash_price_cents: number;
  paid_inventory_points: number;
  bonus_inventory_points: number;
  total_inventory_points: number;
  tier_at_order?: string | null;
  tier_source?: string | null;
  tier_bonus_rate_bps?: number;
  crosses_tier_threshold?: boolean;
  projected_rolling_12m_yuan?: string | number | null;
  reward_description?: string | null;
}

export interface ProcurementTierProgress {
  enabled: boolean;
  natural_tier?: string;
  effective_tier?: string;
  benefit_floor_tier?: string | null;
  override_active?: boolean;
  rolling_12m_yuan?: number;
  next_tier?: string | null;
  next_threshold_yuan?: number | null;
  gap_to_next_yuan?: number;
  natural_bonus_rate?: number;
  effective_bonus_rate?: number;
  benefit_floor_rate?: number;
  bonus_rate?: number;
}

export interface ProcurementCatalog {
  seller_label: string;
  service: PlatformServiceStatus;
  catalog_version: string;
  tier_progress?: ProcurementTierProgress;
  items: ProcurementCatalogItem[];
}

export interface ProcurementQuote {
  quote_id: string;
  cash_price_cents: number;
  paid_inventory_points: number;
  bonus_inventory_points: number;
  total_inventory_points: number;
  tier_at_order?: string | null;
  tier_source?: string | null;
  tier_bonus_rate_bps?: number;
  crosses_tier_threshold?: boolean;
  projected_rolling_12m_yuan?: string | number | null;
  reward_description?: string | null;
  price_valid_until: string;
  service: PlatformServiceStatus;
  seller_label?: string;
}

export type CatalogType = 'retail' | 'procurement';

export type CatalogVersionStatus = 'draft' | 'validated' | 'published' | 'archived' | 'rolled_back';

export interface CatalogVersion {
  id: number;
  version_code: string;
  catalog_type: CatalogType;
  scope_key: string;
  status: CatalogVersionStatus;
  effective_from: string | null;
  effective_to: string | null;
  created_at?: string;
}

export interface CatalogVersionList {
  versions: CatalogVersion[];
}

// admin draft/validate/publish/rollback 返回单版本(宽松容错)
export interface CatalogMutationResult {
  version?: CatalogVersion;
  id?: number;
  version_code?: string;
  status?: CatalogVersionStatus;
  validation_errors?: string[];
}

// ==========================================
// 错误归一 + 请求执行
// ==========================================

const KNOWN_CODES: ReadonlySet<string> = new Set<PricingErrorCode>([
  'SSOT_DISABLED',
  'ACCOUNT_CONFIGURATION_UNAVAILABLE',
  'ACCOUNT_CONFIGURATION_REVIEW_REQUIRED',
  'PRICE_CONFIGURATION_UNAVAILABLE',
  'PLATFORM_DIRECT_NOT_READY',
  /* 🔴 [WO_243 甲] 这里是**第二处**。只加到类型里不加这里不会报类型错 ——
        `toPricingError` 会把它静默归成 `UNKNOWN`,于是判据看见的是 UNKNOWN、
        页面走进通用错误态,而"我加过这个码了"这句话仍然是真的。
        一个名字有三个角色:**认(这里)/ 发(后端)/ 比(消费点)**。 */
  'PLATFORM_DIRECT_NO_PROCUREMENT',
  'NO_PUBLISHED_RETAIL',
  'NO_PUBLISHED_PROCUREMENT',
  'QUOTE_NOT_FOUND',
  'QUOTE_EXPIRED',
  'QUOTE_USED',
  'QUOTE_CONSUMED',
  'QUOTE_TYPE_MISMATCH',
  'QUOTE_MISMATCH',
]);

function toPricingError(err: unknown): PricingError {
  const e = err as {
    response?: { status?: number; data?: { code?: unknown; detail?: unknown; message?: unknown } };
    message?: string;
  };
  const status = e.response?.status ?? 0;
  const body = e.response?.data;
  /*
   * code 可能在 body.code 或**结构化的** detail 里。
   *
   * 🔴 [WO_243 甲 2026-09-19] 这里原来写 `body.detail.code` —— 而
   *    `installDetailNormalizer`(`src/lib/api.ts:2147`)会把对象 `detail`
   *    **归一成字符串**并把原对象挪到 `detail_contract`。于是这一行恒读到
   *    `undefined`,**每一个结构化错误码都被静默归成 `UNKNOWN`**。
   *    静默是因为字符串 detail 还能正常渲染成文案 —— 屏幕上有话,码没了。
   * 🔴 `readDetailContract` 的抬头**早就写着**「新增按 code 分支的读取点一律走这里,
   *    不要再写 `?.detail?.code`」,还引了 2026-07-26 同形事故(428 的 `detail.code`
   *    恒失败 ⇒ 补签页永远到不了)。这个读取点是**漏网的那一个**。
   * ⇒ 后果不止本单:`SSOT_DISABLED` / `NO_PUBLISHED_*` 那几条按码分流的分支
   *    在此之前**都够不着**(见交付单的"复活清单")。
   */
  let rawCode: unknown = body?.code;
  const structured = readDetailContract(err);
  const detail = body?.detail;
  const detailObject = structured && typeof structured === 'object'
    ? (structured as { code?: unknown; message?: unknown })
    : null;
  if (!rawCode && detailObject) {
    rawCode = detailObject.code;
  }
  const code: PricingErrorCode =
    typeof rawCode === 'string' && KNOWN_CODES.has(rawCode)
      ? (rawCode as PricingErrorCode)
      : 'UNKNOWN';
  let message = '定价服务暂不可用,请稍后再试';
  if (typeof detail === 'string' && detail.trim()) message = detail;
  else if (typeof detailObject?.message === 'string' && detailObject.message.trim()) {
    message = detailObject.message;
  } else if (typeof body?.message === 'string' && body.message.trim()) message = body.message;
  else if (typeof e.message === 'string' && /Network Error|Failed to fetch|Load failed/i.test(e.message)) {
    message = '网络连接异常，请检查网络后重试';
  } else if (typeof e.message === 'string' && e.message.trim() && !/^Request failed/i.test(e.message)) {
    message = e.message;
  }
  return { ok: false, status, code, message };
}

async function run<T>(
  fn: () => Promise<AxiosResponse<unknown>>,
): Promise<PricingResult<T>> {
  try {
    const res = await fn();
    // 后端统一包 { success, data }; 少数端点直接返 payload — 两种都兼容
    const body = res.data as { data?: T } | null;
    const payload = (body && typeof body === 'object' && body.data !== undefined
      ? body.data
      : (res.data as T)) as T;
    return { ok: true, data: payload };
  } catch (err) {
    return toPricingError(err);
  }
}

// ==========================================
// 纯函数请求(无组件状态 · 模块级稳定引用)
// ==========================================

async function getRetailCatalog(): Promise<PricingResult<RetailCatalog>> {
  return run<RetailCatalog>(() => api.get(CUSTOMER_RECHARGE_ENTRY.catalog_endpoint));
}

async function createRetailQuote(
  productCode: string,
  quantity: number,
  idempotencyKey?: string,
  digitalGoodsAcknowledged = false,
  termsAcceptanceId?: string,
): Promise<PricingResult<RetailQuote>> {
  return run<RetailQuote>(() =>
    api.post(CUSTOMER_RECHARGE_ENTRY.quote_endpoints.fixed, {
      product_code: productCode,
      quantity,
      digital_goods_acknowledged: digitalGoodsAcknowledged,
      terms_acceptance_id: termsAcceptanceId,
      ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
    }),
  );
}

async function createRetailCustomAmountQuote(
  amountCents: number,
  idempotencyKey: string,
  digitalGoodsAcknowledged: boolean,
  termsAcceptanceId: string,
): Promise<PricingResult<RetailQuote>> {
  return run<RetailQuote>(() =>
    api.post(CUSTOMER_RECHARGE_ENTRY.quote_endpoints.custom_amount, {
      amount_cents: amountCents,
      idempotency_key: idempotencyKey,
      digital_goods_acknowledged: digitalGoodsAcknowledged,
      terms_acceptance_id: termsAcceptanceId,
    }),
  );
}

async function getProcurementCatalog(): Promise<PricingResult<ProcurementCatalog>> {
  return run<ProcurementCatalog>(() => api.get(AGENT_INVENTORY_ENTRY.catalog_endpoint));
}

async function createProcurementQuote(
  productCode: string,
  quantity: number,
  idempotencyKey?: string,
): Promise<PricingResult<ProcurementQuote>> {
  return run<ProcurementQuote>(() =>
    api.post(AGENT_INVENTORY_ENTRY.quote_endpoints.fixed, {
      product_code: productCode,
      quantity,
      ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
    }),
  );
}

// ---------- admin ----------

async function listVersions(
  catalogType: CatalogType,
  scopeKey: string,
): Promise<PricingResult<CatalogVersionList>> {
  return run<CatalogVersionList>(() =>
    api.get('/api/pricing/admin/catalog/versions', {
      params: { catalog_type: catalogType, scope_key: scopeKey },
    }),
  );
}

async function createDraft(
  payload: Record<string, unknown>,
): Promise<PricingResult<CatalogMutationResult>> {
  return run<CatalogMutationResult>(() => api.post('/api/pricing/admin/catalog/draft', payload));
}

async function validateDraft(id: number): Promise<PricingResult<CatalogMutationResult>> {
  return run<CatalogMutationResult>(() => api.post(`/api/pricing/admin/catalog/${id}/validate`));
}

async function publish(
  id: number,
  forceApprove = false,
): Promise<PricingResult<CatalogMutationResult>> {
  return run<CatalogMutationResult>(() =>
    api.post(`/api/pricing/admin/catalog/${id}/publish`, { force_approve: forceApprove }),
  );
}

async function rollback(
  id: number,
  newVersionCode: string,
  forceApprove = false,
): Promise<PricingResult<CatalogMutationResult>> {
  return run<CatalogMutationResult>(() =>
    api.post(`/api/pricing/admin/catalog/${id}/rollback`, {
      new_version_code: newVersionCode,
      force_approve: forceApprove,
    }),
  );
}

// ==========================================
// 展示格式化 helper(只做除以 100 + 千分位,绝不算价)
// ==========================================

/** 分 → "¥1,234.00"(纯展示 · 后端字段已是最终价,前端只格式化) */
export function formatCents(cents: number | null | undefined): string {
  if (cents == null || Number.isNaN(cents)) return '¥—';
  const yuan = cents / 100;
  return `¥${yuan.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** 积分千分位展示 */
export function formatPoints(points: number | null | undefined): string {
  if (points == null || Number.isNaN(points)) return '—';
  return points.toLocaleString('zh-CN');
}

// ==========================================
// 钩子
// ==========================================

export interface PricingSSOTApi {
  getRetailCatalog: typeof getRetailCatalog;
  createRetailQuote: typeof createRetailQuote;
  createRetailCustomAmountQuote: typeof createRetailCustomAmountQuote;
  getProcurementCatalog: typeof getProcurementCatalog;
  createProcurementQuote: typeof createProcurementQuote;
  listVersions: typeof listVersions;
  createDraft: typeof createDraft;
  validateDraft: typeof validateDraft;
  publish: typeof publish;
  rollback: typeof rollback;
  formatCents: typeof formatCents;
  formatPoints: typeof formatPoints;
}

export function usePricingSSOT(): PricingSSOTApi {
  return useMemo<PricingSSOTApi>(
    () => ({
      getRetailCatalog,
      createRetailQuote,
      createRetailCustomAmountQuote,
      getProcurementCatalog,
      createProcurementQuote,
      listVersions,
      createDraft,
      validateDraft,
      publish,
      rollback,
      formatCents,
      formatPoints,
    }),
    [],
  );
}

export default usePricingSSOT;
