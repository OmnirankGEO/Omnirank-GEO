/**
 * customerEvents — 客户公开页埋点 SDK (CTO-C 2026-04-26 · feat/m3-customer-signals)
 *
 * 用途:
 *   5 公开 token 链路 (/q · /s · /portal · /public/report) 上调用,
 *   写入 4 类信号 + 7 类事件 (老板拍板第一批):
 *     opened / dwell_30s / dwell_120s / saw_price /
 *     cta_click / submitted_keywords / renewed_interest
 *
 * 红线:
 *   - sendBeacon 失败静默 · 不阻塞页面 · 不抛异常
 *   - 不引大依赖 (纯 navigator API)
 *   - 不动 5 公开 token 链路语义
 *   - 不记录滚动百分比 / 鼠标轨迹 / 表单内容
 *   - 不在客户端做 ip / ua hash · 服务端立即 hash + 不入库原始
 *
 * 幂等:
 *   - opened 60s 内同 token 同 source 防重 (sessionStorage)
 *   - 后端 event_key UNIQUE 索引兜底
 *
 * 架构:
 *   - trackEvent() 单一入口 · 幂等键 + sendBeacon
 *   - mountOpened() 页面 mount 钩子 · 自动 dwell 计时器 + 卸载 flush
 *   - watchSawPrice(el) IntersectionObserver helper · 50% 可见 · 一次性
 */

const ENDPOINT = '/api/m3/customer-events/public';

const ALLOWED_SOURCES = ['public_report', 'public_quote', 'selection', 'portal'] as const;
const ALLOWED_EVENT_TYPES = [
  'opened',
  'dwell_30s',
  'dwell_120s',
  'saw_price',
  'cta_click',
  'submitted_keywords',
  'renewed_interest',
] as const;

export type EventSource = (typeof ALLOWED_SOURCES)[number];
export type EventType = (typeof ALLOWED_EVENT_TYPES)[number];

export interface TrackContext {
  source: EventSource;
  /** 公开 URL 上的 token / share_code · SDK 内不上报原值,仅用于派生 event_key + 上报至 raw_token (服务端立即 hash) */
  rawToken?: string;
  brandId?: number;
  quoteId?: number;
  diagnosisId?: number;
  /** 仅 metadata 白名单字段 · 见后端 _sanitize_metadata */
  metadata?: Record<string, string | number | boolean>;
}

interface TrackPayload {
  source: string;
  event_type: string;
  event_key?: string;
  brand_id?: number;
  quote_id?: number;
  diagnosis_id?: number;
  raw_token?: string;
  metadata?: Record<string, string | number | boolean>;
}

/** 防重 key 计算 · 不写入 DB · 仅决定 event_key */
function buildEventKey(eventType: EventType, ctx: TrackContext): string | undefined {
  // 幂等 bucket = source + token/quote/diag + 时间桶
  const idPart = ctx.rawToken
    ? 't:' + ctx.rawToken
    : ctx.quoteId != null
      ? 'q:' + ctx.quoteId
      : ctx.diagnosisId != null
        ? 'd:' + ctx.diagnosisId
        : ctx.brandId != null
          ? 'b:' + ctx.brandId
          : 'anon';

  switch (eventType) {
    case 'opened': {
      // 60s 桶 · 同 token 同 source 60s 内只 1 条
      const bucket = Math.floor(Date.now() / 60000);
      return `opened:${ctx.source}:${idPart}:${bucket}`;
    }
    case 'dwell_30s':
    case 'dwell_120s': {
      // 5min 桶 · 同 token 同 source 同阈值 5min 内只 1 条
      const bucket = Math.floor(Date.now() / 300000);
      return `${eventType}:${ctx.source}:${idPart}:${bucket}`;
    }
    case 'saw_price':
      // 一次性 (跨 session 仍允许新事件)· 5min 桶
      return `saw_price:${ctx.source}:${idPart}:${Math.floor(Date.now() / 300000)}`;
    case 'cta_click':
      // 5s 桶防双击
      return `cta_click:${ctx.source}:${idPart}:${Math.floor(Date.now() / 5000)}`;
    case 'submitted_keywords':
      // 1 hour 桶 (允许重新提交但短期防重)
      return `submitted:${ctx.source}:${idPart}:${Math.floor(Date.now() / 3600000)}`;
    case 'renewed_interest':
      return `renewed:${ctx.source}:${idPart}:${Math.floor(Date.now() / 300000)}`;
    default:
      return undefined;
  }
}

/** 60s 前端防重 (sessionStorage) · 后端 event_key 兜底 */
function isFrontendDuped(eventKey: string | undefined): boolean {
  if (!eventKey) return false;
  if (typeof sessionStorage === 'undefined') return false;
  const ssKey = 'm3_evt:' + eventKey;
  try {
    const exists = sessionStorage.getItem(ssKey);
    if (exists) return true;
    sessionStorage.setItem(ssKey, '1');
    // 简单清理 (>200 keys 时丢一半)
    const total = sessionStorage.length;
    if (total > 200) {
      const toDelete: string[] = [];
      for (let i = 0; i < total; i++) {
        const k = sessionStorage.key(i);
        if (k && k.startsWith('m3_evt:')) toDelete.push(k);
        if (toDelete.length > 100) break;
      }
      toDelete.slice(0, 50).forEach((k) => sessionStorage.removeItem(k));
    }
  } catch {
    // 静默 (隐私模式禁 sessionStorage)
  }
  return false;
}

function postBeacon(payload: TrackPayload): boolean {
  if (typeof navigator === 'undefined') return false;
  try {
    const body = JSON.stringify(payload);
    if (navigator.sendBeacon) {
      const blob = new Blob([body], { type: 'application/json' });
      return navigator.sendBeacon(ENDPOINT, blob);
    }
    // fallback fetch keepalive
    if (typeof fetch !== 'undefined') {
      fetch(ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        keepalive: true,
      }).catch(() => {});
      return true;
    }
  } catch {
    // 静默
  }
  return false;
}

/** 主入口 · 永远不抛 · 失败静默 */
export function trackEvent(eventType: EventType, ctx: TrackContext): void {
  try {
    if (!ALLOWED_SOURCES.includes(ctx.source)) return;
    if (!ALLOWED_EVENT_TYPES.includes(eventType)) return;
    const eventKey = buildEventKey(eventType, ctx);
    if (isFrontendDuped(eventKey)) return;
    const payload: TrackPayload = {
      source: ctx.source,
      event_type: eventType,
      event_key: eventKey,
      brand_id: ctx.brandId,
      quote_id: ctx.quoteId,
      diagnosis_id: ctx.diagnosisId,
      raw_token: ctx.rawToken,
      metadata: ctx.metadata,
    };
    postBeacon(payload);
  } catch {
    // 永不抛 · 静默
  }
}

/**
 * mountOpened · 页面 mount 时调用 · 写 opened + 启动 dwell 计时器
 *
 * 返 cleanup 函数 (页面卸载时调用)
 *
 * 行为:
 *   - 立即写 opened (60s 防重)
 *   - 30s 后写 dwell_30s (页面可见时计时,后台 pause)
 *   - 120s 后写 dwell_120s
 *   - visibilitychange / pagehide 时不再 flush 任何 dwell · 等下次可见
 *
 * 不做:
 *   - 滚动百分比 (boss 红线)
 *   - 鼠标轨迹
 */
export function mountOpened(ctx: TrackContext): () => void {
  trackEvent('opened', ctx);

  let visibleSeconds = 0;
  let lastTickAt = Date.now();
  let dwell30Sent = false;
  let dwell120Sent = false;
  let interval: ReturnType<typeof setInterval> | null = null;
  let stopped = false;

  function tick() {
    if (stopped) return;
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') {
      lastTickAt = Date.now();
      return;
    }
    const now = Date.now();
    const delta = (now - lastTickAt) / 1000;
    lastTickAt = now;
    if (delta > 0 && delta < 60) {
      // 跳过页面隐藏后跳变 (>60s 视为 invisible 期间不计入)
      visibleSeconds += delta;
    } else {
      visibleSeconds += 0;
    }
    if (!dwell30Sent && visibleSeconds >= 30) {
      dwell30Sent = true;
      trackEvent('dwell_30s', { ...ctx, metadata: { ...(ctx.metadata || {}), dwell_seconds: 30 } });
    }
    if (!dwell120Sent && visibleSeconds >= 120) {
      dwell120Sent = true;
      trackEvent('dwell_120s', { ...ctx, metadata: { ...(ctx.metadata || {}), dwell_seconds: 120 } });
    }
  }

  function onVisibilityChange() {
    lastTickAt = Date.now();
  }

  if (typeof window !== 'undefined') {
    interval = setInterval(tick, 5000);
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibilityChange);
    }
  }

  return () => {
    stopped = true;
    if (interval) clearInterval(interval);
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibilityChange);
    }
  };
}

/**
 * watchSawPrice · 监听元素 50% 可见 · 触发 saw_price 一次后断开
 *
 * 用法:
 *   const stop = watchSawPrice(priceSectionEl, ctx);
 *   useEffect(() => () => stop(), []);
 */
export function watchSawPrice(
  target: Element | null,
  ctx: TrackContext,
): () => void {
  if (!target || typeof window === 'undefined' || !('IntersectionObserver' in window)) {
    return () => {};
  }
  let fired = false;
  const obs = new IntersectionObserver(
    (entries) => {
      if (fired) return;
      for (const entry of entries) {
        if (entry.isIntersecting && entry.intersectionRatio >= 0.5) {
          fired = true;
          trackEvent('saw_price', ctx);
          obs.disconnect();
          break;
        }
      }
    },
    { threshold: [0.5] },
  );
  obs.observe(target);
  return () => {
    fired = true;
    obs.disconnect();
  };
}

/** Helper · CTA 点击 */
export function trackCtaClick(ctx: TrackContext, ctaName?: string): void {
  trackEvent('cta_click', {
    ...ctx,
    metadata: ctaName ? { ...(ctx.metadata || {}), stage: ctaName } : ctx.metadata,
  });
}

/** Helper · 选词提交 */
export function trackSubmittedKeywords(ctx: TrackContext): void {
  trackEvent('submitted_keywords', ctx);
}

/** Helper · 续费意向点击 */
export function trackRenewedInterest(ctx: TrackContext): void {
  trackEvent('renewed_interest', ctx);
}
