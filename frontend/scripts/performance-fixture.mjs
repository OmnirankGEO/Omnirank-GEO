import { setTimeout as delay } from 'node:timers/promises';

const sessions = new Map();

const defaultControls = (role = 'customer') => ({
  role,
  catalogMode: 'ok',
  catalogCalls: 0,
  catalogInFlight: 0,
  maxCatalogInFlight: 0,
  retryAfterAt: [],
  authDelayMs: 0,
  auth401: false,
  permissionVersion: 1,
  brandName: '性能客户',
  clientsEnabled: true,
  contextDelayMs: 10,
  brandsMapDelayMs: 10,
  testBrandIds: [],
  permissionChangedPath: '',
  permissionChangedRemaining: 0,
  unknownRequests: [],
  requestLog: [],
});

export function userFor(role) {
  const admin = role === 'admin';
  const agent = role === 'agent';
  return {
    id: admin ? 9004 : agent ? 9003 : 9002,
    username: `perf-${role}`,
    display_name: `性能测试-${role}`,
    is_admin: admin,
    is_active: 1,
    must_change_password: 0,
    roles: [],
    permissions: ['diagnosis:view', 'quote:view', 'monitoring:view', 'writing:view', 'brands:view', 'reports:view', 'insights:view', 'settings:view', 'history:view', 'ai_agents:view', 'advisors:view'],
    client_brand_ids: [101],
    agent_level: agent || admin ? 1 : 0,
    extension_authorized: true,
  };
}

function baseMockBody(method, path, role) {
  if (method.toUpperCase() === 'POST' && path === '/api/analytics/event') return { success: true };
  if (method.toUpperCase() === 'POST' && path === '/api/pricing/admin/account-codes/dry-run') return { success: true, data: {
    target_service_count: 0, target_channel_count: 0, missing_service_count: 0, missing_channel_count: 0,
  } };
  if (method.toUpperCase() !== 'GET') return null;
  const user = userFor(role);
  if (path === '/api/auth/me') return { success: true, user };
  if (path === '/api/wallet') return {
    success: true,
    data: {
      paid_points: 12000, commission_points: 300, bonus_points: 800, frozen_points: 0,
      total_recharged: 12000, deduction_preference: 'default', customer_credit_status: 'ready',
      customer_credit: { tool_credit_points: 100, publish_credit_points: 100, bonus_credit_points: 50, total_purchased_points: 250, total_consumed_points: 0 },
      charge_notify_level: 'quiet',
    },
  };
  if (path === '/api/wallet/pricing') return { success: true, data: [] };
  if (path === '/api/user/home-stats') return {
    status: 'success', nickname: user.display_name,
    geo: { brand_count: 2, article_count: 8, published_count: 5, diagnosis_count_month: 2, latest_score: 72, recent_publishes: [] },
    balance: { total: 13200 }, recent_activities: [],
  };
  if (path === '/api/agent/agreement/v35-status') return { success: true, status: 'signed' };
  if (path === '/api/agent/channel-tier/me') return { success: true, channel_tier: { enabled: true, effective_tier: 'silver' } };
  if (path === '/api/agent/pricing/skus') return { items: [{ retail_sku_id: 'perf-pack', override_id: 1, version: 1, sku_key: 'credit_basic', category: 'credit_pack', display_name: '性能测试算力包', points_granted: 10000, wholesale_cents: 50000, retail_cents: 68000, is_active: true }] };
  if (path === '/api/agent/pricing/markup-ratio') return { ratio: 1.36, min: 1, max: 10 };
  if (path === '/api/agent/pricing/cost-per-article') return { cost_per_article: 120, system_default: 100 };
  if (path === '/api/agent/pricing/rebate-config') return { enabled: true, rebate_rate: 0.1, max_rebate_points_per_order: 500 };
  if (path === '/api/agent/finance/overview') return { pnl: { gmv: 6800, orders: 10 } };
  if (path === '/api/agent/inventory/balance') return { paid_inventory_points: 50000, bonus_inventory_points: 5000, frozen_inventory_points: 0, total_purchased_points: 55000, total_allocated_points: 5000, alert_level: 'healthy' };
  if (path === '/api/agent/inventory/transactions') return { items: [], total: 0 };
  if (path === '/api/pricing/retail/catalog') return { data: { catalog_version: 'perf-v1', digital_goods_notice: '数字商品', items: [{ product_code: 'retail-perf', display_name: '标准算力包', final_price_cents: 68000, points_granted: 10000, bonus_points: 0, usage_examples: [] }] } };
  if (path === '/api/pricing/procurement/catalog') return { data: { catalog_version: 'perf-v1', tier_progress: { enabled: true, effective_tier: 'silver', effective_bonus_rate: 0.1 }, items: [{ product_code: 'proc-perf', display_name: '标准进货包', cash_price_cents: 50000, paid_inventory_points: 10000, bonus_inventory_points: 1000, total_inventory_points: 11000 }] } };
  if (path === '/api/client-context/list') return { success: true, clients: [{ id: 101, name: '性能客户', diagnosis_count: 1, quote_count: 1, created_at: '2026-01-01' }] };
  if (path === '/api/client-context/101') return { success: true, context: { brand: { id: 101, name: '性能客户', diagnosis_count: 1 }, profile: null, materials: null, relatedQuoteIds: ['501'], socialProjects: [] } };
  if (path === '/api/quotes') return { total: 0, items: [] };
  if (path === '/api/brands/101/completeness') return { score: 60, groups: {}, missing: [] };
  if (path === '/api/monitoring/clients') return { status: 'success', clients: [{ quote_id: 501, brand_id: 101, brand_name: '性能客户', keyword_count: 0, tier: 'standard' }] };
  if (path === '/api/monitoring/schedule') return { status: 'success', monitoring_enabled: true, jobs: [] };
  if (/^\/api\/monitoring\/clients\/\d+\/keywords$/.test(path)) return { status: 'success', keywords: [], client_appearance_rate: null, super_red_ocean_keywords: [], service_days: 30, service_start_date: '2026-07-01' };
  if (path === '/api/monitoring/trend') return { status: 'success', trend: [] };
  if (/^\/api\/monitoring\/client\/\d+\/monitoring-config$/.test(path)) return { status: 'success', monitoring_enabled: true, monitoring_interval_hours: 24, monitoring_start_hour: 8 };
  if (path === '/api/monitoring/archives') return { status: 'success', archives: [] };
  if (path === '/api/monitoring/rollback/tasks') return { status: 'success', tasks: [] };
  if (path === '/api/meijiehezi/publish-history') return { status: 'success', records: [], total: 0, pages: 0, stats: null, filters: { brands: [], media_types: [] } };
  if (path === '/api/writing/projects') return { status: 'success', projects: [] };
  if (path === '/api/publish/check-first-order') return { success: true, is_first_order: false };
  if (path === '/api/meijiehezi/published-articles' || path === '/api/meijiehezi/rejected-articles') return { success: true, articles: [] };
  if (path === '/api/meijiehezi/article-publish-stats') return { success: true, stats: {} };
  if (path === '/api/meijiehezi/markup') return { status: 'success', ratio: 1.5 };
  if (path === '/api/meijiehezi/media/filters') return {
    status: 'success', areas: [], resource_types: [], news_resources: [], portal_medias: [],
    resource_type_names: [], geo_platforms: [], special_industries: [],
  };
  if (path === '/api/meijiehezi/media') return { status: 'success', media: [], total: 0, pages: 0 };
  if (path === '/api/meijiehezi/awaiting-confirmations') return { status: 'success', items: [] };
  if (path === '/api/pricing/admin/publication/status') return { success: true, data: {
    config_epoch: 1, target_service_count: 0, scopes: [], needs_publication_count: 0,
    blocked_count: 0, publishable: false, ready: true, blockers: [],
    review_contract: { config_epoch: 1, target_mode: 'all_active', service_user_ids: [], scopes: [] },
  } };
  if (path === '/api/pricing/admin/readiness') return { success: true, data: {
    ready: true, blocker_count: 0, blockers: [], checks: {}, flags_changed: false,
  } };
  if (path === '/api/admin/pricing/center') return {
    success: true,
    catalog_version: 'perf-admin-v1',
    default_rule: { points_per_yuan: 20, wholesale_discount: 0.9, agent_purchase_bonus_rate: 0.05, points_per_yuan_after_discount: 25 },
    packages: [],
    overview: { active_count: 0, min_wholesale_cents: 0, retail_range: [0, 0], non_standard_count: 0 },
    environment_override: { active: false, keys: [], message: '' },
  };
  if (/^\/api\/admin\/user-governance\/users(?:\/\d+)?$/.test(path)) return { success: true, users: [], items: [], total: 0, page: 1, page_size: 20 };
  if (path === '/api/admin/user-governance/platform-direct-readiness') return { success: true, ready: true };
  if (path === '/api/user/notifications/unread-count') return { status: 'success', count: 0, by_level: { silent: 0, light: 0, gentle: 0, important: 0, total: 0 } };
  if (path === '/api/user/notifications') return { status: 'success', notifications: [] };
  if (path === '/api/public/whitelabel') return { success: true, data: null };
  if (path === '/api/referral/whitelabel') return { success: true, data: null };
  if (path === '/api/m3/customers') return { success: true, customers: [] };
  if (path === '/api/admin/dashboard') return {
    status: 'success',
    users: { total: 12, week_new: 1, today_new: 0, active_7d: 8, active_rate: 0.67, paid_count: 5, paid_rate: 0.42 },
    finance: { total_revenue_yuan: 1200, total_cost_yuan: 300, profit_yuan: 900, profit_rate: 0.75 },
    feature_usage: [], api_costs: [],
    publishing: { total_orders: 0, published: 0, rejected: 0, media_count: 0, session_valid: null },
    referrals: { direct_leaderboard: [], indirect_leaderboard: [], commission_leaderboard: [] }, activities: [],
  };
  if (path === '/api/dashboard/flow-funnel') return {
    success: true, days: 90, scope: role === 'admin' ? 'global' : 'agent', stages: [],
    summary: { diagnosis_count: 0, pay_count: 0, publish_count: 0, pay_ratio: 0, publish_ratio: 0 },
  };
  return null;
}

const retailCatalog = {
  data: {
    catalog_version: 'perf-v1',
    digital_goods_notice: '数字商品',
    items: [{ product_code: 'retail-perf', display_name: '标准算力包', final_price_cents: 68000, points_granted: 10000, bonus_points: 0, usage_examples: [] }],
  },
};

function exactDynamicBody(method, path, role, controls) {
  const normalized = method.toUpperCase();
  const user = { ...userFor(role), permission_version: controls.permissionVersion };
  if (normalized === 'GET' && path === '/api/auth/me') return { success: true, user };
  if (normalized === 'POST' && path === '/api/auth/refresh') {
    return { success: true, token: 'synthetic-perf-' + role + '-refreshed-' + controls.permissionVersion };
  }
  if (normalized === 'GET' && path === '/api/m3/customers') {
    return { success: true, customers: controls.testBrandIds.map(id => ({ id, is_test: true })) };
  }
  if (normalized === 'GET' && path === '/api/client-context/list') {
    return {
      success: true,
      clients: controls.clientsEnabled
        ? [{ id: 101, name: controls.brandName, diagnosis_count: 1, quote_count: 1, created_at: '2026-01-01' }]
        : [],
    };
  }
  if (normalized === 'GET' && path === '/api/client-context/101') {
    return {
      success: true,
      context: {
        brand: { id: 101, name: controls.brandName, diagnosis_count: 1 },
        profile: null,
        materials: null,
        relatedQuoteIds: ['501'],
        socialProjects: [],
      },
    };
  }
  return baseMockBody(normalized, path, role);
}

function parseCookies(raw = '') {
  return Object.fromEntries(raw.split(';').map(part => part.trim()).filter(Boolean).map(part => {
    const index = part.indexOf('=');
    return index < 0 ? [part, ''] : [part.slice(0, index), decodeURIComponent(part.slice(index + 1))];
  }));
}

function sessionFor(req) {
  const id = parseCookies(req.headers.cookie || '').omnirank_perf_session || 'default';
  if (!sessions.has(id)) sessions.set(id, defaultControls());
  return { id, controls: sessions.get(id) };
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (chunks.length === 0) return {};
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

function sendJson(res, status, body, headers = {}) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Timing-Allow-Origin', '*');
  for (const [name, value] of Object.entries(headers)) res.setHeader(name, String(value));
  res.end(JSON.stringify(body));
}

function roleFromRequest(req, controls) {
  const authorization = String(req.headers.authorization || '');
  const match = authorization.match(/^Bearer synthetic-(?:perf|failure)-([a-z]+)/);
  return match?.[1] || controls.role || 'customer';
}

export function createPerformanceFixturePlugin() {
  return {
    name: 'omnirank-performance-http-fixture',
    configurePreviewServer(server) {
      server.middlewares.use(async (req, res, next) => {
        const url = new URL(req.url || '/', 'http://127.0.0.1');
        const method = String(req.method || 'GET').toUpperCase();

        if (url.pathname.startsWith('/assets/')) {
          res.setHeader('Cache-Control', 'public, max-age=31536000, immutable');
          next();
          return;
        }
        if (url.pathname === '/__perf/control') {
          const { id, controls } = sessionFor(req);
          if (method === 'GET') {
            sendJson(res, 200, { id, ...controls });
            return;
          }
          if (method === 'POST' || method === 'PATCH') {
            const patch = await readJson(req);
            const nextControls = { ...controls, ...patch };
            if (patch.resetCounters) {
              Object.assign(nextControls, {
                catalogCalls: 0,
                catalogInFlight: 0,
                maxCatalogInFlight: 0,
                retryAfterAt: [],
                unknownRequests: [],
                requestLog: [],
              });
              delete nextControls.resetCounters;
            }
            sessions.set(id, nextControls);
            sendJson(res, 200, { id, ...nextControls });
            return;
          }
          if (method === 'DELETE') {
            sessions.set(id, defaultControls());
            sendJson(res, 200, { id, ...sessions.get(id) });
            return;
          }
          sendJson(res, 599, { code: 'UNMOCKED_CONTROL', method, path: url.pathname });
          return;
        }

        if (!url.pathname.startsWith('/api/')) {
          if (String(req.headers.accept || '').includes('text/html')) res.setHeader('Cache-Control', 'no-store');
          next();
          return;
        }

        const { controls } = sessionFor(req);
        const role = roleFromRequest(req, controls);
        controls.requestLog.push({ method, path: url.pathname, at: Date.now() });

        if (url.pathname === '/api/auth/me') {
          if (controls.authDelayMs > 0) await delay(controls.authDelayMs);
          if (controls.auth401) {
            sendJson(res, 401, { code: 'INVALID_TOKEN', detail: { code: 'INVALID_TOKEN', message: 'expired' } });
            return;
          }
        }
        if (url.pathname === '/api/auth/refresh' && controls.auth401) {
          sendJson(res, 401, { code: 'INVALID_TOKEN', detail: { code: 'INVALID_TOKEN' } });
          return;
        }
        if (method === 'GET' && url.pathname === '/api/organization/overview') {
          sendJson(res, 404, { detail: { code: 'ORGANIZATION_NOT_FOUND' } });
          return;
        }

        if (method === 'GET'
            && url.pathname === controls.permissionChangedPath
            && controls.permissionChangedRemaining > 0) {
          controls.permissionChangedRemaining -= 1;
          sendJson(res, 401, { code: 'PERMISSION_CHANGED', detail: { code: 'PERMISSION_CHANGED' } });
          return;
        }

        if (method === 'GET' && url.pathname === '/api/pricing/retail/catalog') {
          controls.catalogCalls += 1;
          controls.catalogInFlight += 1;
          controls.maxCatalogInFlight = Math.max(controls.maxCatalogInFlight, controls.catalogInFlight);
          controls.retryAfterAt.push(Date.now());
          try {
            if (controls.catalogMode === 'slow') await delay(1200);
            if (controls.catalogMode === '500') {
              sendJson(res, 500, { detail: { message: 'catalog failed' } });
              return;
            }
            if (controls.catalogMode === 'offline') {
              res.destroy();
              return;
            }
            if (controls.catalogMode === '429-once') {
              controls.catalogMode = 'ok';
              sendJson(res, 429, { detail: 'rate limited' }, { 'Retry-After': '1' });
              return;
            }
            sendJson(res, 200, retailCatalog);
            return;
          } finally {
            controls.catalogInFlight -= 1;
          }
        }

        const body = exactDynamicBody(method, url.pathname, role, controls);
        if (body == null) {
          const unknown = { method, path: url.pathname };
          controls.unknownRequests.push(unknown);
          sendJson(res, 599, { code: 'UNMOCKED_API', ...unknown });
          return;
        }
        const responseDelay = url.pathname === '/api/client-context/101'
          ? controls.contextDelayMs
          : (url.pathname === '/api/m3/customers' ? controls.brandsMapDelayMs : 10);
        if (responseDelay > 0) await delay(responseDelay);
        sendJson(res, 200, body);
      });
    },
  };
}
