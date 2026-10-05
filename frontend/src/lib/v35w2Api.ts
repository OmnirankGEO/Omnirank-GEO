/**
 * V3.5 W2 · 前端 API client
 *
 * 复用全局 authFetch 注入 JWT · 走 Vite proxy 同域 · 自动处理 401 refresh
 */

import { buildQuotedOrderBody, getGeoPricingEntryContract } from '@/lib/pricingEntryContracts';
import { apiResourceTags, authFetch } from '@/lib/api';
import { getAuthorizationEpoch, awaitConfirmedSessionToken } from '@/lib/authoritativeSession';

const AGENT_INVENTORY_PRICING_ENTRY = getGeoPricingEntryContract('agent-inventory-purchase');
const READ_TIMEOUT_MS = 20_000;
const readInFlight = new Map<string, { controller: AbortController; request: Promise<unknown>; tags: Set<string> }>();
const activeReadControllers = new Map<AbortController, Set<string>>();

function clearReadFlights(tags?: Set<string>): void {
  for (const [controller, readTags] of activeReadControllers.entries()) {
    if (tags && ![...readTags].some(tag => tags.has(tag))) continue;
    controller.abort();
    activeReadControllers.delete(controller);
  }
  for (const [key, entry] of readInFlight.entries()) {
    if (tags && ![...entry.tags].some(tag => tags.has(tag))) continue;
    readInFlight.delete(key);
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('omnirank-api-mutated', (event) => {
    const tags = new Set((event as CustomEvent<{ tags?: string[] }>).detail?.tags || []);
    if (tags.size > 0) clearReadFlights(tags);
  });
  window.addEventListener('omnirank-authorization-changed', () => clearReadFlights());
}

type CallOptions = RequestInit & { forceRefresh?: boolean };

function readKey(path: string, scope: string): string {
  return `${scope}::${path}`;
}

function retryAfterMs(response: Response): number | null {
  const raw = response.headers.get('Retry-After')?.trim();
  if (!raw) return null;
  const seconds = Number(raw);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
  const dateMs = Date.parse(raw);
  if (!Number.isFinite(dateMs)) return null;
  return Math.max(0, dateMs - Date.now());
}

function abortableDelay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'));
      return;
    }
    const timer = window.setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => {
      window.clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    }, { once: true });
  });
}

export interface V35W2ApiError extends Error {
  code?: string;
  detail?: unknown;
  response?: { status: number; data: { detail: unknown } };
}

export interface PricingPublicationScope {
  catalog_type: 'procurement' | 'retail' | string;
  scope_key?: string | null;
  agent_user_id?: number;
  action: 'noop' | 'publish' | 'blocked' | string;
  entry_count?: number;
  current_version_code?: string | null;
  source_fingerprint?: string;
  blocker?: string | null;
}

export interface PricingPublicationStatus {
  config_epoch: number;
  target_service_count: number;
  scopes: PricingPublicationScope[];
  needs_publication_count: number;
  blocked_count: number;
  publishable: boolean;
  ready: boolean;
  blockers: string[];
  review_contract: {
    config_epoch: number;
    target_mode: 'all_active' | 'explicit';
    service_user_ids: number[];
    scopes: Array<{
      catalog_type: 'procurement' | 'retail';
      scope_key: string;
      source_fingerprint: string;
    }>;
  };
}

export interface PricingReadinessResult {
  ready: boolean;
  blocker_count: number;
  blockers: string[];
  checks: Record<string, unknown>;
  flags_changed: false;
}

export function getV35W2ApiErrorCode(error: unknown): string | undefined {
  const apiError = error as V35W2ApiError;
  if (apiError.code) return apiError.code;
  const detail = apiError.response?.data?.detail ?? apiError.detail;
  if (detail && typeof detail === 'object') {
    const code = (detail as { code?: unknown }).code;
    if (typeof code === 'string') return code;
  }
  return undefined;
}

async function call<T = any>(path: string, opts: CallOptions = {}): Promise<T> {
  // [BUG-3] 在途等待而不是抛错;fail-closed 不变(未确认拿不到 token,下游按匿名走并由后端拒)
  await awaitConfirmedSessionToken();
  const scope = getAuthorizationEpoch();
  const method = (opts.method || 'GET').toUpperCase();
  const key = readKey(path, scope);
  const resourceTags = apiResourceTags(path);
  if (method === 'GET' && !opts.forceRefresh) {
    // Pending-only single-flight. Settled wallet, permission, approval and task state must
    // never be reused by this generic transport after a mutation elsewhere in the app.
    // Signals are component-owned; sharing them would let one unmount cancel another consumer.
    if (!opts.signal) {
      const pending = readInFlight.get(key);
      if (pending) return pending.request as Promise<T>;
    }
  }
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(opts.headers as Record<string, string> | undefined),
  };
  const controller = new AbortController();
  if (method === 'GET') activeReadControllers.set(controller, resourceTags);
  const request = (async (): Promise<T> => {
  const abortFromCaller = () => controller.abort();
  opts.signal?.addEventListener('abort', abortFromCaller, { once: true });
  const requestStartedAt = Date.now();
  const requestDeadlineAt = requestStartedAt + (method === 'GET' ? READ_TIMEOUT_MS : 30_000);
  const timeout = method === 'GET' ? window.setTimeout(() => controller.abort(), READ_TIMEOUT_MS) : null;
  const { forceRefresh: _forceRefresh, ...requestInit } = opts;
  let r: Response;
  try {
    r = await authFetch(path, {
      ...requestInit,
      headers,
      signal: controller.signal,
      __omnirankManagedSignal: true,
      __omnirankCallerSignal: opts.signal || undefined,
      __omnirankStartedAt: requestStartedAt,
      __omnirankDeadlineAt: requestDeadlineAt,
    });
    if (method === 'GET' && r.status === 429) {
      const delay = retryAfterMs(r);
      if (delay !== null && delay <= 30_000) {
        await abortableDelay(delay, controller.signal);
        r = await authFetch(path, {
      ...requestInit,
      headers,
      signal: controller.signal,
      __omnirankManagedSignal: true,
      __omnirankCallerSignal: opts.signal || undefined,
      __omnirankStartedAt: requestStartedAt,
      __omnirankDeadlineAt: requestDeadlineAt,
    });
      }
    }
  } finally {
    if (timeout !== null) window.clearTimeout(timeout);
    opts.signal?.removeEventListener('abort', abortFromCaller);
  }
  if (!r.ok) {
    let detail: unknown = `HTTP ${r.status}`;
    try {
      const j = await r.json();
      detail = j.detail || j.message || detail;
    } catch {
      try {
        detail = (await r.text()) || detail;
      } catch {}
    }
    // 携带 status + detail · 让 formatApiErrorForDisplay 能分流(400 显示 detail · 5xx 走友好兜底)
    const detailObject = detail && typeof detail === 'object'
      ? (detail as { code?: unknown; message?: unknown })
      : null;
    const message = typeof detail === 'string'
      ? detail
      : typeof detailObject?.message === 'string'
        ? detailObject.message
        : `HTTP ${r.status}`;
    const e = new Error(message) as V35W2ApiError;
    e.code = typeof detailObject?.code === 'string' ? detailObject.code : undefined;
    e.detail = detail;
    e.response = { status: r.status, data: { detail } };
    throw e;
  }
  const value = r.status === 204 ? undefined as unknown as T : await r.json() as T;
  return value;
  })();
  if (method === 'GET' && !opts.signal) readInFlight.set(key, { controller, request, tags: resourceTags });
  try {
    return await request;
  } finally {
    activeReadControllers.delete(controller);
    if (readInFlight.get(key)?.request === request) readInFlight.delete(key);
  }
}

/** 一个真实可点的出口(工单 §0.3:禁止只有解释、没有动作)。 */
export interface RelationAction {
  label: string;
  action: string;
  route?: string | null;
}

export interface AgentCustomerLookupItem {
  customer_user_id: number;
  display_name?: string | null;
  phone_masked?: string | null;
  brand_name?: string | null;
  /** owned=我的客户 · downstream_partner=我的下线服务商 · both=两者都有 */
  binding_status: 'owned' | 'downstream_partner' | 'both' | 'unbound' | string;
  tool_credit_points: number;
  publish_credit_points: number;
  bonus_credit_points: number;
  // —— 关系态与出口(与后端 resolve_relationship 同源)——
  // 🔴 这里**刻意没有** cost_multiplier / relationship_id / upstream_* 等字段:
  //    它们是关系反推位,后端已在出口剥掉(工单 §0.1 R5)。前端也不该有类型位置。
  relation?: 'customer' | 'downstream_partner' | 'both' | null;
  target_identity?: 'level0' | 'service_provider' | null;
  headline?: string | null;
  /** 这次划拨会发生什么 —— 进哪个钱包、按什么价。提交前必须展示(§P0-3 第 4 条)。 */
  effect_note?: string | null;
  primary_action?: RelationAction | null;
  secondary_actions?: RelationAction[];
  allowed_actions?: string[];
  ledger_note?: 'inventory_wallet' | 'available_wallet' | null;
}

// ============================================================
// Agent endpoints
// ============================================================

export interface AgentChannelTierState {
  enabled: boolean;
  natural_tier?: string;
  channel_tier?: string;
  effective_tier?: string;
  override_active?: boolean;
  benefit_floor_tier?: string | null;
  benefit_floor_rate?: number;
  natural_bonus_rate?: number;
  effective_bonus_rate?: number;
  tier_override?: string | null;
  tier_override_until?: string | null;
  rolling_12m_yuan?: number;
  next_tier?: string | null;
  next_threshold_yuan?: number | null;
  gap_to_next_yuan?: number;
  bonus_rate?: number;
  is_founder?: boolean;
  founder_rank?: number | null;
  first_order_done?: boolean;
  tier_effective_at?: string | null;
  last_evaluated_at?: string | null;
  updated_at?: string | null;
}

export interface AgentChannelTierMeResponse {
  success: boolean;
  channel_tier: AgentChannelTierState;
}

export interface AgentInventoryPurchaseOption {
  option_id: string;
  amount_cents: number;
  base_points: number;
  bonus_points: number;
  total_points?: number;
  label: string;
  reward_description?: string;
  is_first_month_bonus?: boolean;
  quote_fingerprint: string;
}

export interface AgentInventoryPurchaseOptionsResponse {
  catalog_version: string;
  options: AgentInventoryPurchaseOption[];
}

export interface LegacyAgentInventoryPurchaseRequest {
  amount_cents: number;
  channel?: string;
  option_id?: string;
  expected_catalog_version?: string;
  expected_quote_fingerprint?: string;
  price_quote_id?: never;
  idempotency_key?: never;
}

export interface QuotedAgentInventoryPurchaseRequest {
  price_quote_id: string;
  idempotency_key: string;
  channel?: string;
  amount_cents?: never;
  option_id?: never;
  expected_catalog_version?: never;
  expected_quote_fingerprint?: never;
}

export type AgentInventoryPurchaseRequest =
  | LegacyAgentInventoryPurchaseRequest
  | QuotedAgentInventoryPurchaseRequest;

export interface AgentInventoryPurchaseResponse {
  order_id: string;
  amount_cents: number;
  base_points: number;
  bonus_points: number;
  quote_fingerprint?: string;
  price_quote_id?: string;
  total_points?: number;
  actual_channel: string;
  code_url: string | null;
  payment_url_qrcode: string | null;
  payment_url_mobile: string | null;
  needs_openid: boolean;
}

export interface AgentInventoryPurchasePreviewResponse {
  catalog_version: string;
  option_id: string;
  amount_cents: number;
  base_points: number;
  bonus_points: number;
  total_points: number;
  reward_description: string;
  quote_fingerprint: string;
  tier_at_order?: string;
  tier_bonus_rate_bps?: number;
  crosses_tier_threshold?: boolean;
  projected_rolling_12m_yuan?: string;
  price_quote_id?: string;
  product_code?: string;
  expires_at?: string;
}

export interface InventoryPurchaseCatalogItem {
  option_id: string | null;
  amount_cents: number;
  is_enabled: boolean;
  sort_order: number;
  reward_description: string;
  preview: {
    base_points: number;
    bonus_points: number;
    total_points: number;
    discount_source: string;
    tier_at_order: string;
    bonus_rate_bps: number;
    quote_fingerprint: string;
  };
}

export interface InventoryPurchaseCatalogResponse {
  success: boolean;
  catalog_version: string;
  options: InventoryPurchaseCatalogItem[];
  preview_agent_user_id: number | null;
  environment_override: {
    active: boolean;
    keys: string[];
    message: string | null;
  };
  save_notice: string;
  request_id?: string;
}

export interface InventoryPurchaseCatalogPutRequest {
  expected_catalog_version: string;
  options: Array<Pick<InventoryPurchaseCatalogItem, 'option_id' | 'amount_cents' | 'is_enabled' | 'sort_order'>>;
}

export type ChannelTierNumeric = number | string;

export interface ChannelTierRuleConfig {
  min_yuan: ChannelTierNumeric;
  bonus_rate: ChannelTierNumeric;
  is_enabled: boolean;
  description: string;
}

export interface ChannelTierConfig {
  agent_tier_config: Record<'certified' | 'preferred' | 'strategic', ChannelTierRuleConfig>;
  founding: {
    cap: number;
    first_order_extra_bonus: ChannelTierNumeric;
    min_first_order_yuan: ChannelTierNumeric;
  };
  bonus_validity_months: number;
  k_default: ChannelTierNumeric;
  margin_label_thresholds: {
    loss_heavy_bps: number;
    loss_light_bps: number;
    healthy_bps: number;
    profit_excellent_bps: number;
    hard_block_bps: number;
  };
}

export interface ChannelTierConfigGetResponse {
  success: boolean;
  catalog_version: string;
  config: ChannelTierConfig;
  environment_override: { active: boolean; keys: string[]; message: string };
}

export interface ChannelTierConfigPutResponse {
  success: boolean;
  catalog_version: string;
  config: ChannelTierConfig;
  request_id: string;
}

export const agentApi = {
  channelTierMe: (signal?: AbortSignal) => call<AgentChannelTierMeResponse>(
    '/api/agent/channel-tier/me',
    { signal },
  ),

  // 库存
  inventoryBalance: (signal?: AbortSignal, forceRefresh = false) => call<{
    paid_inventory_points: number;
    bonus_inventory_points: number;
    frozen_inventory_points: number;
    total_purchased_points: number;
    total_allocated_points: number;
    alert_level: string;
  }>('/api/agent/inventory/balance', { signal, forceRefresh }),

  inventoryTransactions: (limit = 50, offset = 0, signal?: AbortSignal, forceRefresh = false) => call<{
    items: Array<any>;
    total: number;
  }>(`/api/agent/inventory/transactions?limit=${limit}&offset=${offset}`, { signal, forceRefresh }),

  purchaseOptions: () => call<AgentInventoryPurchaseOptionsResponse>('/api/agent/inventory/purchase-options'),

  previewPurchase: (body: { amount_cents: number; option_id?: string; idempotency_key?: string }) =>
    call<AgentInventoryPurchasePreviewResponse>(AGENT_INVENTORY_PRICING_ENTRY.quote_endpoints.custom_amount, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  createPurchase: (body: AgentInventoryPurchaseRequest) =>
    call<AgentInventoryPurchaseResponse>(AGENT_INVENTORY_PRICING_ENTRY.order_endpoint, {
      method: 'POST',
      body: JSON.stringify(body.price_quote_id
        ? buildQuotedOrderBody('agent-inventory-purchase', {
            priceQuoteId: body.price_quote_id,
            channel: body.channel,
            idempotencyKey: body.idempotency_key,
          })
        : {
            amount_cents: body.amount_cents,
            channel: body.channel || 'auto',
            option_id: body.option_id,
            expected_catalog_version: body.expected_catalog_version,
            expected_quote_fingerprint: body.expected_quote_fingerprint,
          }),
    }),

  // [1C · 3C] 利润换算力:settled 利润按出厂折扣等价换 paid_inventory 算力(即时 · 无审核 · 不扣 fees/税)
  // 后端无 GET 预览端点 · 确认后 POST 返回精确 inventory_points_granted
  redeemFromCommission: (redeem_yuan: number) => call<{
    success: boolean;
    redeem_cents?: number;
    inventory_points_granted?: number;
    [k: string]: any;
  }>('/api/agent/inventory/redeem-from-commission', {
    method: 'POST',
    body: JSON.stringify({ redeem_yuan }),
  }),

  allocateOffline: (body: {
    customer_user_id: number;
    tool_points: number;
    publish_points: number;
    bonus_points: number;
    description?: string;
  }) => call('/api/agent/inventory/allocate-offline', {
    method: 'POST',
    body: JSON.stringify(body),
  }),

  revokeOffline: (body: {
    customer_user_id: number;
    tool_points: number;
    publish_points: number;
    bonus_points: number;
    reason?: string;
  }) => call('/api/agent/inventory/revoke-offline', {
    method: 'POST',
    body: JSON.stringify(body),
  }),

  // 客户额度
  lookupCustomers: (q: string, opts: { ownedOnly?: boolean; limit?: number } = {}) =>
    call<{ items: AgentCustomerLookupItem[]; total: number }>(
      `/api/agent/customers/lookup?q=${encodeURIComponent(q)}&owned_only=${opts.ownedOnly ? 'true' : 'false'}&limit=${opts.limit ?? 8}`
    ),

  /** [P0 热修 §2] 上游线下供货给下线服务商 · 进对方的**库存算力**(可继续向下分销)。 */
  supplyDownstream: (body: {
    downstream_user_id: number;
    paid_points: number;
    bonus_points: number;
    description?: string;
  }) =>
    call<{
      success: boolean;
      downstream_user_id: number;
      supplied_paid: number;
      supplied_bonus: number;
      agent_paid_inventory_after: number;
      agent_bonus_inventory_after: number;
      downstream_paid_inventory_after: number;
      downstream_bonus_inventory_after: number;
    }>('/api/agent/inventory/supply-downstream', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  customerCredit: (customer_user_id: number) =>
    call(`/api/agent/customers/${customer_user_id}/credit`),

  customerCreditTx: (customer_user_id: number, limit = 50, offset = 0) =>
    call(`/api/agent/customers/${customer_user_id}/credit/transactions?limit=${limit}&offset=${offset}`),

  // 定价
  pricingSKUs: (signal?: AbortSignal, forceRefresh = false) => call<{ items: Array<any> }>(
    '/api/agent/pricing/skus', { signal, forceRefresh },
  ),

  // [2026-06-06] 经营总览(复用 ProfitDashboard 同端点)· 取本月已售 gmv(元)+ orders 笔数
  financeOverview: (signal?: AbortSignal, forceRefresh = false) => call<{ pnl?: { gmv?: number; orders?: number } }>(
    '/api/agent/finance/overview', { signal, forceRefresh },
  ),

  // [2026-06-06 1:N 白标包] PUT by override_id(一条 override = 一个独立白标算力包)
  saveSKU: (override_id: number, body: {
    version: number;
    display_name: string;
    subtitle?: string;
    extra_promo_text?: string;
    scene?: string;
    points_granted: number;
    retail_cents: number;
    is_active?: boolean;
    sort_order?: number;
  }) => call(`/api/agent/pricing/skus/${override_id}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  }),

  // 独立服务商零售 SKU；平台模板仅为可选预填来源。
  createSKU: (body: {
    client_request_id: string;
    source_template_id?: number;
    display_name: string;
    subtitle?: string;
    extra_promo_text?: string;
    scene?: string;
    points_granted: number;
    retail_cents: number;
    is_active?: boolean;
    sort_order?: number;
  }) => call<{ success: boolean; id: number; retail_sku_id: string; version: number; margin_label?: string }>('/api/agent/pricing/skus', {
    method: 'POST',
    body: JSON.stringify(body),
  }),

  // 删除始终写 tombstone；历史订单与待支付快照继续可追溯。
  deleteSKU: (override_id: number, version: number) => call<{
    success: boolean; action?: 'soft_deleted';
  }>(
    `/api/agent/pricing/skus/${override_id}?version=${encodeURIComponent(version)}`,
    { method: 'DELETE' }
  ),

  previewSKU: (body: { points_granted: number; retail_cents: number }) => call<{
    success: boolean;
    data: {
      points_granted: number;
      retail_cents: number;
      estimated_cost_cents: number;
      estimated_profit_cents: number;
      margin_label: string;
      margin_action: string;
      is_loss: boolean;
      publishable: boolean;
      estimate_basis: string;
    };
  }>('/api/agent/pricing/skus/preview', {
    method: 'POST',
    body: JSON.stringify(body),
  }),

  // D3 全局加价系数
  getMarkupRatio: (signal?: AbortSignal, forceRefresh = false) => call<{ ratio: number | null; min: number; max: number }>(
    '/api/agent/pricing/markup-ratio', { signal, forceRefresh }
  ),

  setMarkupRatio: (ratio: number) => call('/api/agent/pricing/markup-ratio', {
    method: 'PUT',
    body: JSON.stringify({ ratio }),
  }),

  // [M2 2026-06-07] 服务商自设单篇内容成本(null=系统估算 / 设值=A完全覆盖)
  getCostPerArticle: (signal?: AbortSignal, forceRefresh = false) => call<{ cost_per_article: number | null; system_default: number }>(
    '/api/agent/pricing/cost-per-article', { signal, forceRefresh }
  ),

  setCostPerArticle: (cost: number | null) => call('/api/agent/pricing/cost-per-article', {
    method: 'PUT',
    body: JSON.stringify({ cost }),
  }),

  // [2026-06-06 返修] 仅倍数模式:applyMarkup(1.5) → {ratio} → 售价 = 进货价 × 1.5
  // (移除"客户毛利率"margin 暗门 · 与后端 MarkupRatioRequest 只留 ratio 对齐)
  applyMarkup: (ratio: number) =>
    call<{
      success: boolean; ratio?: number; markup_bps?: number;
      // 批量加价只更新已有 canonical 零售包，不会从平台规格物化新包。
      created_count: number; updated_count: number;
      applied_count: number; skipped_count: number;
      created?: Array<{ sku: string; retail_cents: number; reason?: string }>;
      updated?: Array<{ sku: string; retail_cents: number; reason?: string }>;
      applied: Array<{ sku: string; retail_cents: number }>;
      skipped: Array<{ sku: string; retail_cents: number; reason: string }>;
    }>('/api/agent/pricing/apply-markup', {
      method: 'POST',
      body: JSON.stringify({ ratio }),
    }),

  // D2-b 代理自定返利
  getRebateConfig: (signal?: AbortSignal, forceRefresh = false) => call<{
    enabled: boolean; rebate_rate: number; max_rebate_points_per_order: number | null;
  }>('/api/agent/pricing/rebate-config', { signal, forceRefresh }),

  setRebateConfig: (body: {
    enabled: boolean; rebate_rate: number; max_rebate_points_per_order?: number | null;
  }) => call('/api/agent/pricing/rebate-config', {
    method: 'PUT',
    body: JSON.stringify(body),
  }),

  // 结算
  settlementBalance: () => call('/api/agent/settlement/balance'),

  settlementAvailable: () => call<{ items: Array<any>; total_amount_cents: number }>(
    '/api/agent/settlement/available-items'
  ),

  // GAPS#4 已绑定收款账户(无则返 null)
  payoutAccount: () => call<{
    bank_name: string;
    account_holder: string;
    account_no_masked: string;
  } | null>('/api/agent/settlement/payout-account'),

  createSettlementRequest: (body: {
    amount_cents: number;
    bank_name: string;
    bank_account: string;
    account_holder: string;
    invoice_required?: boolean;
  }) => call('/api/agent/settlement/requests', {
    method: 'POST',
    body: JSON.stringify(body),
  }),

  // [1D · 3D] 提现报价三段(提 / 扣 / 到账)· 提交前预览 · W2 铁律只返金额不返费率 *_bps
  withdrawalQuote: (amount_yuan: number) => call<{
    success: boolean;
    gross_cents: number;
    available_cents: number;
    sufficient: boolean;
    platform_fee_cents: number;  // 平台服务费(通道 + 服务费合并 · 不分项)
    tax_cents: number;           // 代扣税(单列 · 金额)
    total_fee_cents: number;
    net_cents: number;           // 实际到账
    label: string;
  }>(`/api/agent/settlement/withdrawal-quote?amount_yuan=${encodeURIComponent(amount_yuan)}`),

  // 推广
  promotionQR: () => call<{
    qr_code_url: string;
    ref_link: string;
    invite_code: string;
  }>('/api/agent/promotion/qrcode'),

  promotionCustomers: (limit = 50, offset = 0) =>
    call<{
      items: Array<any>;
      total: number;
      /** [客户线上购买门控 2026-07-29] 主账号默认开关 · 列表用它解释"跟随默认"是跟随成什么 */
      default_allow_client_online_purchase?: boolean;
    }>(
      `/api/agent/promotion/customers?limit=${limit}&offset=${offset}`
    ),

  // [客户线上购买门控 2026-07-29] 服务商侧两级开关。
  // 这是服务商界面,文案可以直说;客户侧只会看到「联系您的推荐人」。
  clientPurchaseSettings: () =>
    call<{ allow_client_online_purchase: boolean }>('/api/agent/client-purchase/settings'),

  setClientPurchaseSettings: (allow: boolean) =>
    call<{ allow_client_online_purchase: boolean }>('/api/agent/client-purchase/settings', {
      method: 'PUT',
      body: JSON.stringify({ allow_client_online_purchase: allow }),
    }),

  /** override: 'inherit' | 'allow' | 'offline_only' */
  setCustomerPurchaseOverride: (customerUserId: number, override: string) =>
    call<{
      customer_user_id: number;
      online_purchase_override: string;
      can_purchase_online: boolean;
    }>(`/api/agent/client-purchase/customers/${customerUserId}`, {
      method: 'PUT',
      body: JSON.stringify({ override }),
    }),
};

// ============================================================
// Admin endpoints
// ============================================================

export const adminApi = {
  pricingPublicationStatus: () =>
    call<{ success: boolean; data: PricingPublicationStatus }>(
      '/api/pricing/admin/publication/status',
    ),
  pricingPublicationDryRun: (serviceUserIds?: number[]) =>
    call<{ success: boolean; data: PricingPublicationStatus }>(
      '/api/pricing/admin/publication/dry-run',
      {
        method: 'POST',
        body: JSON.stringify({ service_user_ids: serviceUserIds ?? null }),
      },
    ),
  pricingPublicationPublish: (body: {
    reason: string;
    expected_epoch: number;
    reviewed_target_mode: 'all_active' | 'explicit';
    reviewed_service_user_ids: number[];
    reviewed_scopes: Array<{
      catalog_type: 'procurement' | 'retail';
      scope_key: string;
      source_fingerprint: string;
    }>;
  }) => call<{
    success: boolean;
    data: {
      config_epoch: number;
      target_service_count: number;
      published_count: number;
      unchanged_count: number;
      results: Array<{
        catalog_type: string;
        scope_key: string;
        version_code: string;
        changed: boolean;
        entry_count: number;
      }>;
    };
  }>('/api/pricing/admin/publication/publish', {
    method: 'POST',
    body: JSON.stringify(body),
  }),
  pricingReadiness: () =>
    call<{ success: boolean; data: PricingReadinessResult }>(
      '/api/pricing/admin/readiness',
    ),
  accountCodeDryRun: () =>
    call<{
      success: boolean;
      data: {
        target_service_count: number;
        target_channel_count: number;
        missing_service_count: number;
        missing_channel_count: number;
      };
    }>('/api/pricing/admin/account-codes/dry-run', {
      method: 'POST',
      body: JSON.stringify({}),
    }),
  accountCodePrepare: () =>
    call<{
      success: boolean;
      data: { created_count: number; existing_count: number };
    }>('/api/pricing/admin/account-codes/prepare', {
      method: 'POST',
      body: JSON.stringify({}),
    }),

  settlementsList: (status?: string, limit = 50, offset = 0) => {
    const qs = new URLSearchParams();
    if (status) qs.set('status', status);
    qs.set('limit', String(limit));
    qs.set('offset', String(offset));
    return call<{ items: Array<any>; total: number }>(`/api/admin/settlements?${qs}`);
  },

  settlementPatch: (id: number, body: {
    action: 'approve' | 'reject' | 'mark_paid';
    transfer_proof_url?: string;
    wire_transfer_no?: string;
    admin_note?: string;
    reject_reason?: string;
  }) => call(`/api/admin/settlements/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  }),

  taxProfileGet: (agent_user_id: number) =>
    call(`/api/admin/tax-profiles/${agent_user_id}`),

  taxProfilePut: (agent_user_id: number, body: any) =>
    call(`/api/admin/tax-profiles/${agent_user_id}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  // [D5] 定价系数动态配置(全局 + per-agent)
  globalPricingConfigGet: () =>
    call<{ success: boolean; catalog_version: string; config: any }>(`/api/admin/pricing/global-config`),
  globalPricingConfigPut: (body: {
    expected_catalog_version: string;
    wholesale_discount?: number;
    wholesale_numer?: number;
    wholesale_denom?: number;
    agent_purchase_bonus_rate?: number;
  }) => call<{ success: boolean; catalog_version: string; request_id: string; updated_fields: string[] }>(
    `/api/admin/pricing/global-config`,
    { method: 'PUT', body: JSON.stringify(body) },
  ),
  channelTierConfigGet: () =>
    call<ChannelTierConfigGetResponse>(`/api/admin/pricing/channel-tier-config`),
  channelTierConfigPut: (body: ChannelTierConfig, expectedCatalogVersion: string) =>
    call<ChannelTierConfigPutResponse>(`/api/admin/pricing/channel-tier-config`, {
      method: 'PUT',
      body: JSON.stringify({ ...body, expected_catalog_version: expectedCatalogVersion }),
    }),
  inventoryPurchaseCatalogGet: (agent_user_id?: number) => {
    const query = agent_user_id == null ? '' : `?agent_user_id=${encodeURIComponent(agent_user_id)}`;
    return call<InventoryPurchaseCatalogResponse>(`/api/admin/pricing/inventory-purchase-catalog${query}`);
  },
  inventoryPurchaseCatalogPut: (body: InventoryPurchaseCatalogPutRequest) =>
    call<InventoryPurchaseCatalogResponse>('/api/admin/pricing/inventory-purchase-catalog', {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  channelTierAgents: (params: { limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams();
    qs.set('limit', String(params.limit ?? 50));
    qs.set('offset', String(params.offset ?? 0));
    return call<{ success: boolean; items: Array<any> }>(`/api/admin/channel-tier/agents?${qs}`);
  },
  channelTierFounderSeats: () =>
    call<{ success: boolean; used: number; cap: number; remaining: number }>(`/api/admin/channel-tier/founder-seats`),
  channelTierHistory: (limit = 100) =>
    call<{ success: boolean; items: Array<any> }>(`/api/admin/channel-tier/history?limit=${limit}`),
  channelTierAgentHistory: (agent_user_id: number, limit = 100) =>
    call<{ success: boolean; agent_user_id: number; items: Array<any> }>(
      `/api/admin/channel-tier/agents/${agent_user_id}/history?limit=${limit}`
    ),
  channelTierEvaluate: (agent_user_id: number) =>
    call<{ success: boolean; state: any }>(`/api/admin/channel-tier/agents/${agent_user_id}/evaluate`, {
      method: 'POST',
    }),
  channelTierSetOverride: (agent_user_id: number, body: any) =>
    call<{ success: boolean; state: any }>(`/api/admin/channel-tier/agents/${agent_user_id}/tier`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  channelTierSeedDefaultSkus: () =>
    call(`/api/admin/pricing/default-sku-packages/seed`, { method: 'POST' }),
  agentPricingOverrideGet: (agent_user_id: number) =>
    call<{ success: boolean; agent_user_id: number; override: any; catalog_version: string }>(
      `/api/admin/pricing/agent-override/${agent_user_id}`),
  agentPricingOverridePut: (agent_user_id: number, body: any) =>
    call(`/api/admin/pricing/agent-override/${agent_user_id}`, {
      method: 'PUT', body: JSON.stringify(body),
    }),
  agentPricingOverrideDelete: (agent_user_id: number, expectedCatalogVersion: string) =>
    call(`/api/admin/pricing/agent-override/${agent_user_id}?expected_catalog_version=${encodeURIComponent(expectedCatalogVersion)}`, { method: 'DELETE' }),

  pricingList: (params: { category?: string; is_active?: boolean } = {}) => {
    const qs = new URLSearchParams();
    if (params.category) qs.set('category', params.category);
    if (params.is_active !== undefined) qs.set('is_active', String(params.is_active));
    return call<{ items: Array<any> }>(`/api/admin/pricing/skus?${qs}`);
  },

  pricingPut: (sku_template_id: number, body: any) =>
    call(`/api/admin/pricing/skus/${sku_template_id}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  // [2026-06-07 算力定价中心] 聚合读:默认拿货规则 + 全部算力包 + 概览统计
  pricingCenter: () => call<{
    success: boolean;
    catalog_version: string;
    default_rule: {
      points_per_yuan: number;
      wholesale_discount: number;            // 小数(如 0.8=8折)
      agent_purchase_bonus_rate: number;     // 进货赠送比例(如 0.10)
      points_per_yuan_after_discount: number; // 1 元进货 ≈ X 算力
    };
    packages: Array<{
      sku_template_id: number;
      sku_key: string;
      sku_type: string;
      display_name: string;
      subtitle?: string | null;
      capability_pitch?: string | null;      // 适合场景
      points_granted: number;
      wholesale_cents: number;               // 服务商进货价
      retail_cents: number;                  // 建议客户售价
      margin_cents: number;                  // 服务商预计利润 = retail - wholesale
      is_active: boolean;
      non_standard: boolean;                 // 进货价不符当前出厂规则(活动/谈价包)
    }>;
    overview: {
      active_count: number;
      min_wholesale_cents: number;
      retail_range: [number, number];
      non_standard_count: number;
    };
    environment_override: {
      active: boolean;
      keys: string[];
      message: string;
    };
  }>('/api/admin/pricing/center'),

  // [2026-06-07] 新增算力包(默认不上架·软校验·不填进货价按规则算)
  pricingCreate: (body: {
    sku_type: 'credit_pack' | 'scenario_pack' | 'addon_pack';
    display_name: string;
    subtitle?: string;
    capability_pitch?: string;
    points_granted: number;
    retail_cents: number;
    wholesale_cents?: number;
    is_active?: boolean;
  }) => call<{ success: boolean; sku_template_id: number; non_standard: boolean }>(
    '/api/admin/pricing/skus',
    { method: 'POST', body: JSON.stringify(body) }
  ),

  // [2026-06-07] 抽屉编辑保存(软校验·patch 合并·non_standard 只标记不阻止)
  pricingSave: (sku_template_id: number, body: {
    display_name?: string;
    subtitle?: string;
    capability_pitch?: string;
    points_granted?: number;
    wholesale_cents?: number;
    retail_cents?: number;
    is_active?: boolean;
  }) => call<{ success: boolean; sku_template_id: number; non_standard: boolean }>(
    `/api/admin/pricing/skus/${sku_template_id}/save`,
    { method: 'POST', body: JSON.stringify(body) }
  ),

  // [2026-06-07] 复制算力包(名字 +(副本)·默认不上架)
  pricingCopy: (sku_template_id: number) =>
    call<{ success: boolean; sku_template_id: number }>(
      `/api/admin/pricing/skus/${sku_template_id}/copy`,
      { method: 'POST' }
    ),

  // [2026-06-07] 上架 / 下架算力包
  pricingToggle: (sku_template_id: number, is_active: boolean) =>
    call<{ success: boolean; is_active: boolean }>(
      `/api/admin/pricing/skus/${sku_template_id}/toggle-active`,
      { method: 'POST', body: JSON.stringify({ is_active }) }
    ),

  // [2026-06-07] 单个按当前规则重算进货价(绝不碰历史订单)
  pricingRecalc: (sku_template_id: number) =>
    call<{ success: boolean; wholesale_cents: number }>(
      `/api/admin/pricing/skus/${sku_template_id}/recalc`,
      { method: 'POST' }
    ),

  // [复审返修2] 批量按当前规则重算(only_active=仅上架·include_non_standard=是否覆盖活动包/谈价包·默认 false 跳过)
  pricingRecalcAll: (only_active = false, include_non_standard = false) =>
    call<{
      success: boolean;
      updated_count: number;
      skipped_count: number;
      // [BH-015a] 被跳过的行里,进货价确实与当前出厂规则不同的个数
      skipped_would_change?: number;
      // [BH-015a] updated_count === 0 时**必然**非空 —— 默认模式下一行都不会被更新
      // 是规则本身决定的(结构性),不是「今天恰好都已是最新价」。必须显示给 admin。
      no_op_reason?: string | null;
      items: Array<{
        id: number; old: number; new: number; skipped?: boolean;
        rule_cents?: number; would_change?: boolean;
      }>;
      notice?: string;
    }>('/api/admin/pricing/skus/recalc-all', {
      method: 'POST',
      body: JSON.stringify({ only_active, include_non_standard }),
    }),

  // [复审返修4] 按 user_id/手机号/用户名/显示名 查服务商(避免 admin 死记 user_id)
  pricingAgentLookup: (q: string) =>
    call<{
      success: boolean;
      results: Array<{
        user_id: number;
        username: string | null;
        phone: string | null;
        display_name: string | null;
        agent_level: number;
      }>;
    }>(`/api/admin/pricing/agent-lookup?q=${encodeURIComponent(q)}`),

  auditSummary: () => call('/api/admin/inventory-audit/summary'),
};

export const formatCents = (cents: number): string =>
  `¥${(cents / 100).toFixed(2)}`;

export const formatPoints = (points: number): string =>
  points >= 10000 ? `${(points / 10000).toFixed(2)}万` : String(points);
