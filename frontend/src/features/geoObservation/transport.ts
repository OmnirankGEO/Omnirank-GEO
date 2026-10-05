/**
 * 观测产品 API 的传输层。
 *
 * 关键设计：
 * - 生产只有一个真实 transport（httpTransport），用现役 authFetch 打真后端。
 * - 前端**没有任何 fixture 兜底**。后端不可用时抛 ObservationApiError，页面进入真实错误/空态。
 * - 冻结 fixture 只在 Playwright 网络层（page.route）注入，绝不进任何生产 bundle。
 * - 集成期只替换 transport 的 baseUrl/真实 endpoint，不改组件语义。
 */

import { authFetch } from '@/lib/api';
import { ObservationApiError, type ObservationErrorBody } from './types';

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH';
  body?: unknown;
  signal?: AbortSignal;
  /** 附加请求头（如 X-Request-Id 幂等键）。 */
  headers?: Record<string, string>;
}

/** 从任意 JSON 里提取结构化错误信封 `{detail:{code,message,retryable?}}` 或 `{code,message}`。 */
function extractErrorEnvelope(json: unknown): Partial<ObservationErrorBody> | null {
  if (!json || typeof json !== 'object') return null;
  const src =
    'detail' in json && (json as Record<string, unknown>).detail && typeof (json as Record<string, unknown>).detail === 'object'
      ? (json as Record<string, unknown>).detail
      : json;
  const o = src as Record<string, unknown>;
  if (typeof o.code === 'string' && typeof o.message === 'string') {
    return { code: o.code, message: o.message, retryable: typeof o.retryable === 'boolean' ? o.retryable : undefined };
  }
  return null;
}

export interface ObservationTransport {
  request<T>(path: string, options?: RequestOptions): Promise<T>;
}

async function parseError(res: Response): Promise<ObservationApiError> {
  let body: Partial<ObservationErrorBody> = {};
  try {
    const env = extractErrorEnvelope(await res.clone().json());
    if (env) body = env;
  } catch {
    /* 非 JSON 错误 → 用状态码兜底文案 */
  }
  return new ObservationApiError(res.status, body);
}

/** 生产 transport：真实 HTTP，永不返回合成数据。 */
export const httpTransport: ObservationTransport = {
  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = { ...(options.headers || {}) };
    const init: RequestInit = { method: options.method || 'GET', signal: options.signal };
    if (options.body !== undefined) {
      init.body = JSON.stringify(options.body);
      headers['Content-Type'] = 'application/json';
    }
    if (Object.keys(headers).length) init.headers = headers;
    let res: Response;
    try {
      res = await authFetch(path, init);
    } catch (e) {
      if ((e as { name?: string })?.name === 'AbortError') throw e;
      // 网络层失败：等同服务不可用，交给页面显示 503 态
      throw new ObservationApiError(503, { code: 'OBSERVATION_UNAVAILABLE' });
    }
    if (!res.ok) throw await parseError(res);
    // 204 无内容
    if (res.status === 204) return undefined as unknown as T;
    let json: unknown;
    try {
      json = await res.json();
    } catch {
      throw new ObservationApiError(res.status, { code: 'OBSERVATION_UNAVAILABLE' });
    }
    // AI-3 用 HTTPException(status=200, detail={code:INSUFFICIENT_SAMPLES,...})：2xx 里也可能藏错误信封
    const env = extractErrorEnvelope(json);
    if (env && env.code) throw new ObservationApiError(res.status, env);
    return json as T;
  },
};
