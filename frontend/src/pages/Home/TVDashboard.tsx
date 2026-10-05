/**
 * TVDashboard — 电视大屏运营看板 v4
 * 55寸 / 2米观看距离优化 — 大字号 + 高对比
 * 新增：性能监控 + 代发业务窗口
 */

import { useState, useEffect, useCallback, useMemo, useRef, type FormEvent } from 'react';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import * as echarts from 'echarts/core';
import { MapChart, EffectScatterChart, LineChart } from 'echarts/charts';
import {
  TooltipComponent, GridComponent, VisualMapComponent,
  TitleComponent, LegendComponent, GeoComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import {
  Users, TrendingUp, DollarSign, Stethoscope, FileText, Send,
  Activity, UserPlus, Wallet, RefreshCw, BarChart3, Zap, Globe,
  Crown, Server, Database, Clock, Cpu, HardDrive,
  CheckCircle, XCircle, AlertTriangle, Pause,
} from 'lucide-react';

echarts.use([
  MapChart, EffectScatterChart, LineChart,
  TooltipComponent, GridComponent, VisualMapComponent,
  TitleComponent, LegendComponent, GeoComponent, CanvasRenderer,
]);

import { findCityCoord, getTierColor, PROVINCE_CAPITAL, TIER_LABELS } from '@/data/china-cities';
import { authFetch } from '@/lib/api';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

let chinaMapRegistered = false;
const REFRESH_INTERVAL = 30_000;
const ACCESS_REQUEST_TIMEOUT = 8_000;
const DASHBOARD_REQUEST_TIMEOUT = 12_000;

const PROVINCE_COORDS: Record<string, [number, number]> = {
  '广东': [113.3, 23.1], '浙江': [120.2, 30.3], '北京': [116.4, 39.9],
  '上海': [121.5, 31.2], '江苏': [118.8, 32.1], '四川': [104.1, 30.6],
  '山东': [117.0, 36.7], '湖北': [114.3, 30.6], '福建': [119.3, 26.1],
  '河南': [113.7, 34.8], '湖南': [113.0, 28.2], '陕西': [108.9, 34.3],
  '辽宁': [123.4, 41.8], '安徽': [117.3, 31.8], '重庆': [106.5, 29.5],
  '河北': [114.5, 38.0], '江西': [115.9, 28.7], '云南': [102.7, 25.0],
  '贵州': [106.7, 26.6], '广西': [108.3, 22.8], '山西': [112.5, 37.9],
  '吉林': [125.3, 43.9], '黑龙江': [126.6, 45.8], '天津': [117.2, 39.1],
};

const MOCK_GEO = [
  { province: '广东', city: '深圳', count: 68, active: 45 },
  { province: '广东', city: '广州', count: 42, active: 30 },
  { province: '北京', city: '北京', count: 82, active: 61 },
  { province: '上海', city: '上海', count: 76, active: 52 },
  { province: '浙江', city: '杭州', count: 55, active: 38 },
  { province: '四川', city: '成都', count: 35, active: 22 },
  { province: '江苏', city: '南京', count: 28, active: 18 },
  { province: '湖北', city: '武汉', count: 25, active: 16 },
  { province: '广东', city: '东莞', count: 18, active: 12 },
  { province: '浙江', city: '宁波', count: 22, active: 14 },
  { province: '江苏', city: '苏州', count: 24, active: 16 },
  { province: '山东', city: '青岛', count: 15, active: 10 },
  { province: '山东', city: '济南', count: 12, active: 8 },
  { province: '河南', city: '郑州', count: 18, active: 11 },
  { province: '湖南', city: '长沙', count: 16, active: 10 },
  { province: '福建', city: '厦门', count: 14, active: 9 },
  { province: '福建', city: '福州', count: 12, active: 7 },
  { province: '陕西', city: '西安', count: 15, active: 9 },
  { province: '辽宁', city: '大连', count: 11, active: 7 },
  { province: '安徽', city: '合肥', count: 12, active: 7 },
  { province: '重庆', city: '重庆', count: 14, active: 8 },
  { province: '云南', city: '昆明', count: 9, active: 5 },
  { province: '广东', city: '佛山', count: 11, active: 7 },
  { province: '浙江', city: '温州', count: 8, active: 5 },
  { province: '江苏', city: '无锡', count: 9, active: 6 },
  { province: '河北', city: '石家庄', count: 7, active: 4 },
  { province: '贵州', city: '贵阳', count: 7, active: 4 },
  { province: '广西', city: '南宁', count: 5, active: 3 },
  { province: '海南', city: '海口', count: 4, active: 2 },
  { province: '甘肃', city: '兰州', count: 3, active: 1 },
  { province: '新疆', city: '乌鲁木齐', count: 2, active: 1 },
];

const EVENT_COLORS: Record<string, string> = {
  register: '#4ade80', diagnosis: '#60a5fa', recharge: '#fbbf24',
  article: '#a78bfa', publish: '#f472b6',
};

type TvPhase = 'checking-session' | 'locked' | 'session-error' | 'loading-data' | 'ready' | 'data-error';
// 合同要求的状态枚举细分：idle/submitting 由 phase/submitting 表达，success 由 ready 表达，
// 失败五态集中由 classifyTvFailure 映射（wrong_password/forbidden/timeout/network_error/server_error）。
type TvFailureKind = 'idle' | 'submitting' | 'success' | 'wrong_password' | 'forbidden' | 'timeout' | 'network_error' | 'server_error';
type TvFailure = { kind: TvFailureKind; code?: string; message: string; requestId?: string };

class TvRequestTimeoutError extends Error {}

function tvRequestId(prefix: string): string {
  return `${prefix}-${safeRandomUUID()}`;
}

async function authFetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit,
  timeoutMs: number,
  controller: AbortController,
): Promise<Response> {
  let timedOut = false;
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    return await authFetch(input, { ...init, signal: controller.signal });
  } catch (error) {
    if (timedOut) throw new TvRequestTimeoutError('TV request timed out');
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

// 失败分类唯一出口：HTTP 状态 + 后端错误码 + 异常类型 → 失败五态
function classifyTvFailure({ status, code, error }: { status?: number; code?: string; error?: unknown }): TvFailureKind {
  if (error instanceof TvRequestTimeoutError) return 'timeout';
  if (error instanceof TypeError) return 'network_error'; // fetch 断网/跨域阻断只抛 TypeError
  if (code === 'TV_ACCESS_DENIED' || code === 'TV_ACCESS_INPUT_INVALID') return 'wrong_password';
  if (code === 'TV_ADMIN_REQUIRED' || code === 'TV_AUTH_REQUIRED') return 'forbidden';
  // 契约（2026-07-22 后端同步下发）：429 TV_ACCESS_RATE_LIMITED → forbidden 类，展示服务端 message
  if (code === 'TV_ACCESS_RATE_LIMITED' || status === 429) return 'forbidden';
  if ((status ?? 0) >= 500 || code === 'TV_ACCESS_UNAVAILABLE' || code === 'TV_DASHBOARD_UNAVAILABLE') return 'server_error';
  // 无约定 code 的 403/401：权限不足/登录态失效，给 forbidden 人话而非笼统服务器错误
  if (status === 403 || status === 401) return 'forbidden';
  return 'server_error';
}

function tvFailureFromError(error: unknown, timeoutMessage: string, networkMessage: string): TvFailure {
  const kind = classifyTvFailure({ error });
  return { kind, message: kind === 'timeout' ? timeoutMessage : networkMessage };
}

async function readTvFailure(response: Response, fallback: string): Promise<TvFailure> {
  let payload: any = null;
  try {
    payload = await response.json();
  } catch {
    // A proxy may return an empty or non-JSON error page. Keep the safe fallback.
  }
  const detail = payload?.detail;
  const detailObject = detail && typeof detail === 'object' ? detail : null;
  const code: string | undefined = payload?.code || detailObject?.code;
  const kind = classifyTvFailure({ status: response.status, code });
  const serverMessage = payload?.message || detailObject?.message || (typeof detail === 'string' ? detail : undefined);
  // 服务端未携带文案时按类别兜底：限流/权限/登录态都有明确人话，不落笼统"服务器错误"
  const kindFallback = kind === 'forbidden'
    ? (response.status === 429 || code === 'TV_ACCESS_RATE_LIMITED'
        ? '尝试次数过多，请稍后重试'
        : response.status === 401
          ? '登录状态已失效，请重新登录后再试'
          : '当前账号没有访问权限，请联系管理员')
    : fallback;
  return {
    kind,
    code,
    message: serverMessage || kindFallback,
    requestId: payload?.request_id || detailObject?.request_id || response.headers.get('X-Request-ID') || undefined,
  };
}

function RequestReference({ requestId }: { requestId?: string }) {
  if (!requestId) return null;
  return <p className="break-all text-xs text-gray-500">请求 {requestId}</p>;
}

export function TVDashboard() {
  const [data, setData] = useState<any>(null);
  const [clock, setClock] = useState(new Date());
  const [phase, setPhase] = useState<TvPhase>('checking-session');
  const [pwd, setPwd] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [gateError, setGateError] = useState<TvFailure | null>(null);
  const [dataError, setDataError] = useState<TvFailure | null>(null);
  const mountedRef = useRef(false);
  const accessInFlightRef = useRef(false);
  const exchangeTokenRef = useRef<string | null>(null);
  const sessionControllerRef = useRef<AbortController | null>(null);
  const accessControllerRef = useRef<AbortController | null>(null);
  const dashboardControllerRef = useRef<AbortController | null>(null);
  const dashboardRequestRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    // D4：URL token（一次性交换码或 legacy token）只用于一次性交换会话——
    // 先读入内存（供交换与失败重试），立刻从地址栏抹除，交换成功后停留在无 token URL。
    const currentUrl = new URL(window.location.href);
    const urlToken = currentUrl.searchParams.get('token');
    if (urlToken) {
      exchangeTokenRef.current = urlToken;
      currentUrl.searchParams.delete('token');
      window.history.replaceState(window.history.state, '', `${currentUrl.pathname}${currentUrl.search}${currentUrl.hash}`);
    }
    return () => {
      mountedRef.current = false;
      sessionControllerRef.current?.abort();
      accessControllerRef.current?.abort();
      dashboardControllerRef.current?.abort();
    };
  }, []);

  const checkSession = useCallback(async () => {
    sessionControllerRef.current?.abort();
    const controller = new AbortController();
    sessionControllerRef.current = controller;
    setGateError(null);
    setPhase('checking-session');
    const isCurrent = () => mountedRef.current && sessionControllerRef.current === controller;
    try {
      // D4：携带 URL token 进入时先一次性交换会话（交换码或 legacy token），不再用 token 直接拉数据
      const pendingExchangeToken = exchangeTokenRef.current;
      if (pendingExchangeToken) {
        try {
          const response = await authFetchWithTimeout(
            '/api/tv/access/exchange',
            {
              method: 'POST',
              cache: 'no-store',
              headers: { 'Content-Type': 'application/json', 'X-Request-ID': tvRequestId('tv-exchange') },
              body: JSON.stringify({ token: pendingExchangeToken }),
            },
            ACCESS_REQUEST_TIMEOUT,
            controller,
          );
          if (!isCurrent()) return;
          if (response.ok) {
            exchangeTokenRef.current = null;
            setPhase('loading-data');
            return;
          }
          const failure = await readTvFailure(response, '安全链接校验失败，请重试');
          if (!isCurrent()) return;
          if (failure.kind === 'wrong_password' || failure.kind === 'forbidden') {
            // 链接无效/已用/过期（DENIED/INPUT_INVALID）或账号无权限：链接不再重试，落密码门兜底
            exchangeTokenRef.current = null;
            setGateError(failure);
            setPhase('locked');
            return;
          }
          // 5xx：保留 token，「重试检查」会再次尝试交换
          setGateError(failure);
          setPhase('session-error');
          return;
        } catch (error) {
          if (!isCurrent()) return;
          // 超时/断网：保留 token，重试仍走交换
          setGateError(tvFailureFromError(error, '安全链接校验超时，请检查网络后重试', '无法连接大屏访问服务，请检查网络后重试'));
          setPhase('session-error');
          return;
        }
      }

      const response = await authFetchWithTimeout(
        '/api/tv/access/session',
        { cache: 'no-store', headers: { 'X-Request-ID': tvRequestId('tv-session') } },
        ACCESS_REQUEST_TIMEOUT,
        controller,
      );
      if (!isCurrent()) return;
      if (response.ok) {
        setPhase('loading-data');
        return;
      }
      const failure = await readTvFailure(response, '暂时无法确认大屏访问状态');
      if (!isCurrent()) return;
      if (response.status === 401 && (
        failure.code === 'TV_ACCESS_SESSION_REQUIRED'
        || failure.code === 'TV_ACCESS_SESSION_INVALID'
      )) {
        setGateError(failure.code === 'TV_ACCESS_SESSION_INVALID' ? failure : null);
        setPhase('locked');
        return;
      }
      setGateError(failure);
      setPhase('session-error');
    } catch (error) {
      if (!isCurrent()) return;
      setGateError(tvFailureFromError(error, '会话检查超时，请检查网络后重试', '无法连接大屏访问服务，请检查网络后重试'));
      setPhase('session-error');
    } finally {
      if (sessionControllerRef.current === controller) sessionControllerRef.current = null;
    }
  }, []);

  const fetchData = useCallback(async () => {
    dashboardControllerRef.current?.abort();
    const controller = new AbortController();
    dashboardControllerRef.current = controller;
    const requestVersion = ++dashboardRequestRef.current;
    setDataError(null);
    try {
      const response = await authFetchWithTimeout(
        '/api/tv/dashboard/auth',
        { cache: 'no-store', headers: { 'X-Request-ID': tvRequestId('tv-dashboard') } },
        DASHBOARD_REQUEST_TIMEOUT,
        controller,
      );
      if (!mountedRef.current || requestVersion !== dashboardRequestRef.current) return;
      if (!response.ok) {
        const failure = await readTvFailure(response, '大屏数据暂时不可用，请稍后重试');
        if (!mountedRef.current || requestVersion !== dashboardRequestRef.current) return;
        if (response.status === 401 && failure.code?.startsWith('TV_ACCESS_SESSION_')) {
          setData(null);
          setGateError(failure);
          setPhase('locked');
          return;
        }
        setDataError(failure);
        setPhase('data-error');
        return;
      }
      const d = await response.json();
      if (d.status === 'success') {
        // 如果没有真正的城市级数据（city 为空或等于 province），用 MOCK_GEO
        const geoRaw = d.geo_distribution || [];
        const hasCities = geoRaw.some((g: any) => g.city && g.city !== '' && g.city !== g.province);
        if (!geoRaw.length || !hasCities) d.geo_distribution = MOCK_GEO;
        setData(d);
        setDataError(null);
        setPhase('ready');
        return;
      }
      setDataError({ kind: 'server_error', message: d.message || '大屏返回了无法识别的数据，请重试', requestId: d.request_id });
      setPhase('data-error');
    } catch (error) {
      if (!mountedRef.current || requestVersion !== dashboardRequestRef.current) return;
      setDataError(tvFailureFromError(error, '大屏数据加载超时，请检查网络后重试', '无法连接大屏数据服务，请检查网络后重试'));
      setPhase('data-error');
    } finally {
      if (dashboardControllerRef.current === controller) dashboardControllerRef.current = null;
    }
  }, []);

  const submitPassword = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (accessInFlightRef.current || submitting) return;
    if (!pwd) {
      setGateError({ kind: 'wrong_password', message: '请输入大屏访问密码' });
      return;
    }
    accessInFlightRef.current = true;
    setSubmitting(true);
    setGateError(null);
    accessControllerRef.current?.abort();
    const controller = new AbortController();
    accessControllerRef.current = controller;
    try {
      const response = await authFetchWithTimeout(
        '/api/tv/access',
        {
          method: 'POST',
          cache: 'no-store',
          headers: { 'Content-Type': 'application/json', 'X-Request-ID': tvRequestId('tv-access') },
          body: JSON.stringify({ password: pwd }),
        },
        ACCESS_REQUEST_TIMEOUT,
        controller,
      );
      if (!mountedRef.current || accessControllerRef.current !== controller) return;
      if (!response.ok) {
        const failure = await readTvFailure(response, '大屏访问验证失败，请重试');
        if (!mountedRef.current || accessControllerRef.current !== controller) return;
        setGateError(failure);
        return;
      }
      setPhase('loading-data');
    } catch (error) {
      if (!mountedRef.current || accessControllerRef.current !== controller) return;
      setGateError(tvFailureFromError(error, '验证超时，请检查网络后重试', '无法连接大屏访问服务，请检查网络后重试'));
    } finally {
      accessInFlightRef.current = false;
      if (accessControllerRef.current === controller) accessControllerRef.current = null;
      if (mountedRef.current) {
        setPwd('');
        setSubmitting(false);
      }
    }
  }, [pwd, submitting]);

  useEffect(() => { void checkSession(); }, [checkSession]);
  useEffect(() => { if (phase === 'loading-data') void fetchData(); }, [fetchData, phase]);
  useEffect(() => {
    if (phase !== 'ready') return;
    const interval = window.setInterval(() => { void fetchData(); }, REFRESH_INTERVAL);
    return () => window.clearInterval(interval);
  }, [fetchData, phase]);
  useEffect(() => { const t = setInterval(() => setClock(new Date()), 1000); return () => clearInterval(t); }, []);
  useEffect(() => { document.documentElement.classList.add('dark'); document.body.style.overflow = 'hidden'; return () => { document.body.style.overflow = ''; }; }, []);

  if (phase === 'checking-session') {
    return (
      <div className="fixed inset-0 flex items-center justify-center gap-3 bg-[#060a14] px-4 text-cyan-100">
        <RefreshCw className="size-7 animate-spin text-cyan-500" aria-hidden="true" />
        <p className="text-sm">正在确认大屏访问状态…</p>
      </div>
    );
  }

  if (phase === 'session-error') {
    return (
      <div className="fixed inset-0 flex items-center justify-center bg-[#060a14] p-4 text-center">
        <div className="w-full max-w-md space-y-4 rounded-lg border border-white/10 bg-white/[0.03] p-6">
          <AlertTriangle className="mx-auto size-10 text-amber-400" aria-hidden="true" />
          <p className="text-sm text-red-300" role="alert">{gateError?.message}</p>
          <RequestReference requestId={gateError?.requestId} />
          <button type="button" onClick={() => void checkSession()}
            className="min-h-11 rounded-lg bg-cyan-600 px-5 text-sm font-medium text-white hover:bg-cyan-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300">
            重试检查
          </button>
          <a href="/" className="mx-auto flex min-h-11 w-fit items-center px-4 text-sm text-gray-400 hover:text-white">返回管理后台</a>
        </div>
      </div>
    );
  }

  if (phase === 'locked') {
    return (
      <div className="fixed inset-0 flex items-center justify-center overflow-y-auto bg-[#060a14] p-4">
        <form onSubmit={submitPassword} className="w-full max-w-md space-y-5 rounded-lg border border-white/10 bg-white/[0.03] p-5 text-center sm:p-8">
          <img src="/logo-tv.png" alt="运营监控中心" className="h-16 mx-auto opacity-80" />
          <h1 className="text-2xl font-bold text-gray-300">运营监控中心</h1>
          <p className="text-sm leading-6 text-gray-400">
            这是用于会议室和公共屏幕的实时运营大屏。为避免经营数据被旁观或误投屏，登录后台后仍需通过一次大屏访问门禁。
          </p>
          <p className="text-xs leading-5 text-gray-500">
            访问密码由系统管理员或运营负责人提供；忘记密码请联系他们重新获取，请勿在群聊或截图中传播。
          </p>
          <label htmlFor="tv-access-password" className="sr-only">大屏访问密码</label>
          <input id="tv-access-password" name="tv-access-password" type="password" value={pwd}
            onChange={event => { setPwd(event.target.value); setGateError(null); }}
            placeholder="输入访问密码" autoComplete="off" autoFocus disabled={submitting}
            className="mx-auto block w-full max-w-72 rounded-lg border border-white/10 bg-white/5 px-5 py-4 text-center font-mono text-xl text-white placeholder:text-gray-700 focus:border-cyan-500/50 focus:outline-none disabled:cursor-wait disabled:opacity-70" />
          {gateError && (
            <div className="space-y-1">
              <p className="text-sm text-red-400" role="alert">{gateError.message}</p>
              <RequestReference requestId={gateError.requestId} />
            </div>
          )}
          <button type="submit" disabled={submitting}
            className="mx-auto flex min-h-11 w-full max-w-72 items-center justify-center gap-2 rounded-lg bg-cyan-600 px-4 py-3 text-base font-medium text-white hover:bg-cyan-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300 disabled:cursor-not-allowed disabled:opacity-60">
            {submitting && <RefreshCw className="size-4 animate-spin" aria-hidden="true" />}
            {submitting ? '验证中…' : '进入监控中心'}
          </button>
          <a href="/" className="mx-auto inline-flex min-h-11 items-center justify-center rounded-lg px-4 text-sm text-gray-400 hover:bg-white/5 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300">
            返回管理后台
          </a>
        </form>
      </div>
    );
  }

  if (phase === 'data-error') {
    return (
      <div className="fixed inset-0 flex items-center justify-center bg-[#060a14] p-4 text-center">
        <div className="w-full max-w-md space-y-4 rounded-lg border border-white/10 bg-white/[0.03] p-6">
          <Activity className="mx-auto size-10 text-red-400" aria-hidden="true" />
          <p className="text-sm text-red-300" role="alert">{dataError?.message || '大屏数据暂时不可用，请稍后重试'}</p>
          <RequestReference requestId={dataError?.requestId} />
          <button type="button" onClick={() => setPhase('loading-data')}
            className="min-h-11 rounded-lg bg-cyan-600 px-5 text-sm font-medium text-white hover:bg-cyan-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300">
            重试加载
          </button>
          <a href="/" className="mx-auto flex min-h-11 w-fit items-center px-4 text-sm text-gray-400 hover:text-white">返回管理后台</a>
        </div>
      </div>
    );
  }

  if (phase === 'loading-data') {
    return (
      <div className="fixed inset-0 flex items-center justify-center gap-3 bg-[#060a14] px-4 text-cyan-100">
        <RefreshCw className="size-8 animate-spin text-cyan-500" aria-hidden="true" />
        <p className="text-sm">正在加载大屏数据…</p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="fixed inset-0 flex items-center justify-center bg-[#060a14] p-4 text-center">
        <div className="w-full max-w-md space-y-4 rounded-lg border border-white/10 bg-white/[0.03] p-6">
          <AlertTriangle className="mx-auto size-10 text-amber-400" aria-hidden="true" />
          <p className="text-sm text-red-300" role="alert">大屏数据未能完成加载，请重试</p>
          <button type="button" onClick={() => setPhase('loading-data')}
            className="min-h-11 rounded-lg bg-cyan-600 px-5 text-sm font-medium text-white hover:bg-cyan-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300">
            重试加载
          </button>
        </div>
      </div>
    );
  }

  const funnel = data.funnel || {};
  const content = data.content || {};
  const finance = data.finance || {};
  const geo = data.geo_distribution || [];
  const heatmap = data.activity_heatmap || [];
  const trend = data.revenue_trend || [];
  const features = data.top_features || [];
  const feed = data.live_feed || [];
  const perf = data.performance || {};
  const pub = data.publishing || {};

  return (
    <div className="fixed inset-0 bg-[#060a14] text-white overflow-hidden flex flex-col">
      {/* ===== 顶栏 ===== */}
      <div className="flex items-center justify-between px-6 py-3 border-b border-cyan-900/20 shrink-0"
        style={{ background: 'linear-gradient(90deg, rgba(6,182,212,0.05) 0%, transparent 30%, transparent 70%, rgba(6,182,212,0.05) 100%)' }}>
        <div className="flex items-center gap-5">
          <img src="/logo-tv.png" alt="" className="h-9 opacity-70" />
          <div className="h-5 w-px bg-cyan-800/40" />
          <span className="text-sm text-cyan-600 tracking-[0.3em] font-medium">实时运营监控</span>
          <div className="size-2 rounded-full bg-cyan-400 animate-pulse shadow-[0_0_8px_rgba(34,211,238,0.6)]" />
        </div>
        <div className="flex items-center gap-8">
          <span className="text-xs text-cyan-800">AUTO REFRESH · 30s</span>
          <span className="font-mono text-cyan-200 text-2xl tabular-nums tracking-wider">{clock.toLocaleTimeString('zh-CN', { hour12: false })}</span>
          <span className="text-sm text-cyan-700">{clock.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', weekday: 'short' })}</span>
        </div>
      </div>

      {/* ===== 核心指标条 ===== */}
      <div className="grid grid-cols-8 gap-px bg-cyan-900/10 shrink-0">
        {[
          { label: '总用户', value: funnel?.total_users || 0, icon: Users, color: '#60a5fa' },
          { label: '今日注册', value: funnel?.today_register || 0, icon: UserPlus, color: '#4ade80' },
          { label: '在线', value: funnel?.online_users || 0, icon: Activity, color: '#22d3ee' },
          { label: '建档率', value: `${funnel?.profile_rate || 0}%`, icon: FileText, color: '#a78bfa' },
          { label: '面试完成', value: funnel?.interview_done || 0, icon: Stethoscope, color: '#f472b6' },
          { label: '画像Lv3+', value: `${funnel.lv3_count || 0}人 ${funnel.lv3_rate || 0}%`, icon: Crown, color: '#fbbf24' },
          { label: '今日营收', value: `¥${finance?.today_revenue?.toLocaleString() || 0}`, icon: DollarSign, color: '#22d3ee' },
          { label: '月营收', value: `¥${finance?.month_revenue?.toLocaleString() || 0}`, icon: Wallet, color: '#4ade80' },
        ].map((s, i) => (
          <div key={i} className="bg-[#060a14] px-3 py-2.5 flex items-center gap-3">
            <div className="size-9 rounded-lg flex items-center justify-center shrink-0" style={{ background: `${s.color}12` }}>
              <s.icon className="size-4" style={{ color: s.color }} />
            </div>
            <div className="min-w-0">
              <div className="text-lg font-bold font-mono leading-none" style={{ color: s.color }}>{s.value}</div>
              <div className="text-xs text-gray-600 mt-0.5">{s.label}</div>
            </div>
          </div>
        ))}
      </div>

      {/* ===== 主体区域 ===== */}
      <div className="flex-1 grid grid-cols-12 gap-2 p-2 min-h-0">
        {/* 左列 3/12 */}
        <div className="col-span-3 flex flex-col gap-2">
          <Panel title="用户转化漏斗" icon={TrendingUp} className="flex-[1.2]">
            <FunnelChart funnel={funnel} />
          </Panel>
          <Panel title="内容产出" icon={FileText} className="flex-1">
            <ContentStats content={content} />
          </Panel>
          <Panel title="代发业务" icon={Send} className="flex-1">
            <PublishingStats pub={pub} />
          </Panel>
        </div>

        {/* 中列 5/12 — 地图 + 在线用户 + 系统状态 */}
        <div className="col-span-5 flex flex-col gap-2">
          <Panel title="全国用户分布" icon={Globe} className="flex-[3]">
            <ChinaMap data={geo} />
          </Panel>
          <Panel title="在线用户" icon={Users} className="flex-[2]">
            <OnlineUsersPanel users={data.online_users || []} />
          </Panel>
          <Panel title="系统状态" icon={Server} className="flex-[1]">
            <SystemStatus perf={perf} />
          </Panel>
        </div>

        {/* 右列 4/12 */}
        <div className="col-span-4 flex flex-col gap-2">
          <Panel title="实时动态" icon={Zap} className="flex-[3]">
            <LiveFeed events={feed} />
          </Panel>
          <Panel title="营收趋势" icon={TrendingUp} className="flex-[1.5]">
            <TrendChart data={trend} />
          </Panel>
        </div>
      </div>

      {/* ===== 底部滚动条 ===== */}
      <Ticker funnel={funnel} finance={finance} content={content} pub={pub} />
    </div>
  );
}

// ========== Panel ==========
function Panel({ title, icon: Icon, children, className, style }: { title: string; icon?: any; children: React.ReactNode; className?: string; style?: React.CSSProperties }) {
  return (
    <div className={`relative rounded border border-cyan-900/15 bg-[#0a0f1e] overflow-hidden ${className || ''}`} style={style}>
      <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-cyan-500/20 to-transparent" />
      <div className="px-3 py-1.5 border-b border-cyan-900/15 flex items-center gap-2">
        {Icon && <Icon className="size-3.5 text-cyan-600" />}
        <span className="text-xs text-cyan-500 uppercase tracking-[0.15em] font-medium">{title}</span>
      </div>
      <div className="p-2 h-[calc(100%-30px)] overflow-hidden">{children}</div>
    </div>
  );
}

// ========== Funnel Chart ==========
function FunnelChart({ funnel }: { funnel: any }) {
  if (!funnel) return null;
  const steps = [
    { label: '注册用户', value: funnel.total_users, rate: '100%', color: '#60a5fa' },
    { label: '建档', value: funnel.profiles_count, rate: `${funnel.profile_rate}%`, color: '#a78bfa' },
    { label: '完成面试', value: funnel.interview_done, rate: `${funnel.interview_rate}%`, color: '#f472b6' },
    { label: '画像 Lv3+', value: funnel.lv3_count, rate: `${funnel.lv3_rate}%`, color: '#fbbf24' },
    { label: '付费', value: funnel.paid_count, rate: `${funnel.paid_rate}%`, color: '#4ade80' },
  ];
  const max = Math.max(funnel.total_users, 1);
  return (
    <div className="space-y-3 py-1">
      {steps.map((s) => (
        <div key={s.label}>
          <div className="flex justify-between text-sm mb-1">
            <span className="text-gray-400">{s.label}</span>
            <span className="font-mono font-bold" style={{ color: s.color }}>{s.value} <span className="text-gray-600 font-normal">({s.rate})</span></span>
          </div>
          <div className="h-2.5 bg-white/[0.03] rounded-full overflow-hidden">
            <div className="h-full rounded-full transition-all duration-1000" style={{
              width: `${Math.max((s.value / max) * 100, 2)}%`,
              background: `linear-gradient(90deg, ${s.color}, ${s.color}33)`,
              boxShadow: `0 0 8px ${s.color}30`,
            }} />
          </div>
        </div>
      ))}
      <div className="text-xs text-gray-600 pt-1">
        语料 {funnel.corpus_count} 条 · 7日活跃率 {funnel.week_active_rate}%
      </div>
    </div>
  );
}

// ========== Content Stats ==========
function ContentStats({ content }: { content: any }) {
  if (!content) return null;
  const items = [
    { label: '选题', value: content.topics, color: '#60a5fa' },
    { label: '脚本', value: content.scripts, color: '#a78bfa' },
    { label: 'GEO文章', value: content.articles, color: '#22d3ee' },
    { label: '已发布', value: content.published, color: '#4ade80' },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 py-1">
      {items.map(s => (
        <div key={s.label} className="text-center p-3 rounded-lg bg-white/[0.02]">
          <div className="text-2xl font-bold font-mono" style={{ color: s.color }}>{s.value}</div>
          <div className="text-xs text-gray-600 mt-1">{s.label}</div>
        </div>
      ))}
    </div>
  );
}

// ========== Publishing Stats ==========
function PublishingStats({ pub }: { pub: any }) {
  if (!pub) return null;
  const total = pub.total || 0;
  const published = pub.published || 0;
  const rejected = pub.rejected || 0;
  const pending = (pub.pending || 0) + (pub.queued || 0) + (pub.submitted || 0);
  const successRate = total > 0 ? Math.round(published / total * 100) : 0;

  return (
    <div className="flex flex-col gap-3 py-1 h-full">
      {/* Session 状态 */}
      <div className="flex items-center gap-2 px-1">
        {pub.session_valid === true ? (
          <><CheckCircle className="size-4 text-green-400" /><span className="text-sm text-green-400 font-medium">代发服务正常</span></>
        ) : pub.session_valid === false ? (
          <><XCircle className="size-4 text-red-400" /><span className="text-sm text-red-400 font-medium">Session 已失效</span></>
        ) : (
          <><AlertTriangle className="size-4 text-yellow-400" /><span className="text-sm text-yellow-400 font-medium">未配置</span></>
        )}
      </div>

      {/* 数字 */}
      <div className="grid grid-cols-4 gap-2">
        {[
          { label: '总订单', value: total, color: '#60a5fa' },
          { label: '已发布', value: published, color: '#4ade80' },
          { label: '拒稿', value: rejected, color: '#f87171' },
          { label: '待处理', value: pending, color: '#fbbf24' },
        ].map(s => (
          <div key={s.label} className="text-center">
            <div className="text-xl font-bold font-mono" style={{ color: s.color }}>{s.value}</div>
            <div className="text-[10px] text-gray-600 mt-0.5">{s.label}</div>
          </div>
        ))}
      </div>

      {/* 成功率 + 今日 */}
      <div className="flex items-center justify-between px-1 text-sm">
        <span className="text-gray-500">发布成功率 <span className="font-mono font-bold text-cyan-300">{successRate}%</span></span>
        <span className="text-gray-500">今日发布 <span className="font-mono font-bold text-green-400">{pub.today_published || 0}</span></span>
      </div>
    </div>
  );
}

// ========== System Status ==========
function SystemStatus({ perf }: { perf: any }) {
  const sys = perf?.system || {};
  const db = perf?.db || {};
  const api = perf?.api || {};
  const scheduler = perf?.scheduler || [];

  const cpuColor = sys.cpu_percent > 80 ? '#f87171' : sys.cpu_percent > 50 ? '#fbbf24' : '#4ade80';
  const memColor = sys.mem_percent > 80 ? '#f87171' : sys.mem_percent > 50 ? '#fbbf24' : '#4ade80';

  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-2 py-1 h-full">
      {/* CPU */}
      <div className="flex items-center gap-3">
        <Cpu className="size-4 shrink-0" style={{ color: cpuColor }} />
        <div className="flex-1 min-w-0">
          <div className="text-sm text-gray-400">CPU</div>
          <div className="flex items-baseline gap-1">
            <span className="text-xl font-bold font-mono" style={{ color: cpuColor }}>{sys.cpu_percent || 0}%</span>
          </div>
        </div>
      </div>

      {/* 内存 */}
      <div className="flex items-center gap-3">
        <HardDrive className="size-4 shrink-0" style={{ color: memColor }} />
        <div className="flex-1 min-w-0">
          <div className="text-sm text-gray-400">内存</div>
          <div className="flex items-baseline gap-1">
            <span className="text-xl font-bold font-mono" style={{ color: memColor }}>{sys.mem_percent || 0}%</span>
            <span className="text-[10px] text-gray-600">{sys.mem_used_gb || 0}/{sys.mem_total_gb || 0}G</span>
          </div>
        </div>
      </div>

      {/* DB */}
      <div className="flex items-center gap-3">
        <Database className="size-4 shrink-0 text-cyan-400" />
        <div className="flex-1 min-w-0">
          <div className="text-sm text-gray-400">数据库</div>
          <div className="text-lg font-bold font-mono text-cyan-300">
            {db.active_connections || 0}<span className="text-xs text-gray-600 font-normal">/{db.max_connections || 100}</span>
          </div>
        </div>
      </div>

      {/* 并发连接 */}
      <div className="flex items-center gap-3">
        <Activity className="size-4 shrink-0 text-purple-400" />
        <div className="flex-1 min-w-0">
          <div className="text-sm text-gray-400">并发</div>
          <div className="text-lg font-bold font-mono text-purple-300">{sys.concurrent || 0}<span className="text-xs text-gray-600 font-normal"> 连接</span></div>
        </div>
      </div>

      {/* 进程内存 + 定时任务 */}
      <div className="col-span-2 flex items-center gap-4 text-xs text-gray-500 border-t border-cyan-900/10 pt-1.5">
        <span>进程 <span className="font-mono text-gray-400">{sys.proc_mem_mb || 0}MB</span></span>
        <span className="text-gray-700">·</span>
        <span>API(1h) <span className="font-mono text-gray-400">{api.calls_1h || 0}</span></span>
        <span className="text-gray-700">·</span>
        <span>定时任务 <span className="font-mono text-gray-400">{scheduler.length}</span> 个</span>
        {scheduler.length > 0 && (
          <>
            <span className="text-gray-700">·</span>
            <span>下次 <span className="font-mono text-cyan-600">{scheduler[0]?.next_run || '—'}</span></span>
          </>
        )}
      </div>
    </div>
  );
}

// ========== China Map ==========
// 用 ref 保存缩放级别，formatter/symbolSize 回调函数读取 ref 值
// georoam 时只更新 ref + 强制 ECharts 重绘标签，不重建 option，不重置地图位置
const _zoomRef = { current: 1.2 };

function ChinaMap({ data }: { data: any[] }) {
  const [mapReady, setMapReady] = useState(false);
  const chartRef = useRef<any>(null);
  const [zoomDisplay, setZoomDisplay] = useState(1.2);

  useEffect(() => {
    if (chinaMapRegistered) { setMapReady(true); return; }
    fetch('/map/china.json').then(r => r.json()).then(gj => {
      echarts.registerMap('china', gj);
      chinaMapRegistered = true;
      setMapReady(true);
    }).catch(() => {});
  }, []);

  // 监听缩放，更新 ref，强制 ECharts 重绘
  useEffect(() => {
    if (!mapReady || !chartRef.current) return;
    const inst = chartRef.current.getEchartsInstance();
    if (!inst) return;
    const handler = (e: any) => {
      if (!e.zoom) return;
      _zoomRef.current = Math.max(1, Math.min(10, _zoomRef.current * e.zoom));
      setZoomDisplay(_zoomRef.current);
      // 强制 ECharts 重新评估 formatter/symbolSize 回调
      // 只更新 series[1] 的 animation 属性（无害），触发回调重算
      inst.setOption({ series: [{ id: 'map' }, { id: 'points', animation: false }] });
      // 恢复动画
      setTimeout(() => inst.setOption({ series: [{ id: 'map' }, { id: 'points', animation: true }] }), 50);
    };
    inst.on('georoam', handler);
    return () => { inst.off('georoam', handler); };
  }, [mapReady]);

  const option = useMemo(() => {
    if (!mapReady) return {};
    const NM: Record<string, string> = {
      '广东':'广东省','浙江':'浙江省','北京':'北京市','上海':'上海市','江苏':'江苏省',
      '四川':'四川省','山东':'山东省','湖北':'湖北省','福建':'福建省','河南':'河南省',
      '湖南':'湖南省','陕西':'陕西省','辽宁':'辽宁省','安徽':'安徽省','重庆':'重庆市',
      '河北':'河北省','江西':'江西省','云南':'云南省','贵州':'贵州省','广西':'广西壮族自治区',
      '山西':'山西省','吉林':'吉林省','黑龙江':'黑龙江省','天津':'天津市','甘肃':'甘肃省',
      '海南':'海南省','内蒙古':'内蒙古自治区','新疆':'新疆维吾尔自治区','宁夏':'宁夏回族自治区',
      '青海':'青海省','西藏':'西藏自治区','台湾':'台湾省',
    };
    const mapData = (data || []).map(d => ({ name: NM[d.province] || d.province, value: d.count, active: d.active }));
    const max = Math.max(...(data || []).map(d => d.count), 1);

    // 合并为一个数据集：省份点 + 城市点，每个打上 layer 标签
    const allPoints: any[] = [];

    // 省份聚合点（layer='province'）
    const provinceAgg: Record<string, number> = {};
    for (const d of (data || [])) {
      provinceAgg[d.province] = (provinceAgg[d.province] || 0) + d.count;
    }
    for (const [prov, total] of Object.entries(provinceAgg)) {
      const cap = PROVINCE_CAPITAL[prov];
      if (!cap || total <= 0) continue;
      allPoints.push({ name: prov, value: [cap.lng, cap.lat, total], tier: cap.tier, layer: 'province' });
    }

    // 城市点（layer='city'）— 跳过省会城市（已有省份点在同一坐标）
    const capitalNames = new Set(Object.values(
      { '北京':'北京','上海':'上海','天津':'天津','重庆':'重庆','广东':'广州','浙江':'杭州',
        '江苏':'南京','四川':'成都','湖北':'武汉','山东':'济南','河南':'郑州','湖南':'长沙',
        '福建':'福州','安徽':'合肥','江西':'南昌','陕西':'西安','辽宁':'沈阳','云南':'昆明',
        '贵州':'贵阳','广西':'南宁','山西':'太原','河北':'石家庄','吉林':'长春','黑龙江':'哈尔滨',
        '甘肃':'兰州','海南':'海口','内蒙古':'呼和浩特','新疆':'乌鲁木齐' }
    ));
    for (const d of (data || [])) {
      // 省会城市跳过，避免和省份点重叠
      if (capitalNames.has(d.city)) continue;
      const coord = findCityCoord(d.city || '', d.province);
      const finalCoord = coord || PROVINCE_CAPITAL[d.province];
      if (!finalCoord || d.count <= 0) continue;
      allPoints.push({ name: d.city || d.province, value: [finalCoord.lng, finalCoord.lat, d.count], tier: finalCoord.tier, layer: 'city' });
    }

    return {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'item' as const,
        backgroundColor: 'rgba(6,10,20,0.95)',
        borderColor: '#0e7490',
        textStyle: { color: '#e5e7eb', fontSize: 14 },
        formatter: (p: any) => {
          if (p.seriesType === 'effectScatter') {
            const d = p.data;
            const t = d?.tier;
            const tier = t ? ` · ${TIER_LABELS[t] || t + '线'}` : '';
            const color = getTierColor(t || 3);
            const label = d?.layer === 'province' ? `${d.name}省` : d.name;
            return `<b style="color:${color};font-size:16px">${label}</b><span style="color:#6b7280">${tier}</span><br/>用户 <b>${d.value[2]}</b> 人`;
          }
          return p.data?.value
            ? `<b style="color:#22d3ee">${p.name}</b><br/>用户 <b>${p.data.value}</b><br/>活跃 <b>${p.data.active || 0}</b>`
            : p.name;
        },
      },
      visualMap: {
        min: 0, max, left: 12, bottom: 12,
        text: ['高', '低'],
        textStyle: { color: '#374151', fontSize: 10 },
        inRange: { color: ['#061a2e', '#0a3050', '#0c4a6e', '#0369a1', '#0ea5e9', '#22d3ee', '#67e8f9'] },
        show: true, itemWidth: 10, itemHeight: 70,
      },
      geo: {
        map: 'china', roam: true, zoom: 1.2, center: [104.5, 36],
        scaleLimit: { min: 1, max: 10 },
        label: { show: false },
        itemStyle: { areaColor: '#0d1a2e', borderColor: '#0e5f8a', borderWidth: 0.6, shadowColor: 'rgba(14,94,138,0.2)', shadowBlur: 4 },
        emphasis: {
          itemStyle: { areaColor: '#0c5a8e', borderColor: '#22d3ee', borderWidth: 1.5, shadowBlur: 12 },
          label: { show: false },  // 关掉！用 effectScatter 的 label 替代，避免重叠
        },
      },
      series: [
        { id: 'map', type: 'map', map: 'china', geoIndex: 0, data: mapData, selectedMode: false,
          label: { show: false }, emphasis: { label: { show: false } } },  // map series 也彻底关标签
        {
          id: 'points',
          type: 'effectScatter',
          coordinateSystem: 'geo',
          data: allPoints,
          // symbolSize 根据 layer + 当前缩放动态计算
          symbolSize: (v: number[], p: any) => {
            const z = _zoomRef.current;
            const layer = p.data?.layer;
            if (layer === 'province' && z >= 3) return 0;   // 放大到3x，省份点消失
            if (layer === 'city' && z < 2) return 0;         // 小于2x，城市点隐藏
            const base = Math.max(4, Math.min(22, v[2] / 4));
            return base * Math.min(z * 0.5, 2.5);
          },
          encode: { value: 2 },
          showEffectOn: 'render',
          rippleEffect: { brushType: 'stroke', scale: 3.5, period: 3, number: 2 },
          itemStyle: {
            color: (p: any) => getTierColor(p.data?.tier || 3),
            shadowBlur: 10, shadowColor: 'rgba(34,211,238,0.4)',
          },
          label: {
            show: true, position: 'top', align: 'center' as const,
            color: '#f0f9ff', fontSize: 13, fontWeight: 'bold' as const,
            distance: 5,
            textShadowColor: 'rgba(0,0,0,0.9)', textShadowBlur: 4,
            textBorderColor: 'rgba(0,0,0,0.6)', textBorderWidth: 2,
            // formatter 是唯一支持函数回调的 label 属性
            formatter: (p: any) => {
              const z = _zoomRef.current;
              const layer = p.data?.layer;
              if (layer === 'province' && z >= 3) return '';
              if (layer === 'city' && z < 2) return '';
              return p.data?.name || '';
            },
          },
          emphasis: {
            label: { fontSize: 18, color: '#fff', fontWeight: 'bold' as const },
            itemStyle: { shadowBlur: 24, shadowColor: 'rgba(34,211,238,0.8)' },
          },
          zlevel: 1,
        },
      ],
    };
  }, [data, mapReady]);

  if (!mapReady) return <div className="flex items-center justify-center h-full"><RefreshCw className="size-6 animate-spin text-cyan-800" /></div>;

  return (
    <div className="relative h-full w-full">
      <div className="absolute inset-0 pointer-events-none z-10 rounded"
        style={{ background: 'radial-gradient(ellipse at 50% 45%, rgba(34,211,238,0.04) 0%, transparent 65%)', animation: 'breathe 4s ease-in-out infinite' }} />
      <ReactEChartsCore ref={chartRef} echarts={echarts} option={option} style={{ height: '100%', width: '100%' }} notMerge={false} />
      <div className="absolute bottom-2 right-2 text-xs text-cyan-800 z-10">
        滚轮缩放 · 拖拽平移 · {zoomDisplay.toFixed(1)}x
      </div>
      <style>{`@keyframes breathe { 0%,100%{opacity:0.3} 50%{opacity:1} }`}</style>
    </div>
  );
}

// ========== Heatmap ==========
function HeatmapGrid({ data }: { data: any[] }) {
  const max = useMemo(() => Math.max(...(data || []).map(h => h.count), 1), [data]);
  if (!data?.length) return <p className="text-sm text-gray-700 text-center py-3">暂无</p>;
  return (
    <div className="grid gap-[2px]" style={{ gridTemplateColumns: '28px repeat(24, 1fr)' }}>
      <div />
      {Array.from({ length: 24 }, (_, h) => <div key={h} className="text-[9px] text-gray-700 text-center">{h}</div>)}
      {['日','一','二','三','四','五','六'].map((day, dow) => (
        <div key={dow} className="contents">
          <div className="text-xs text-gray-600 flex items-center justify-end pr-1">{day}</div>
          {Array.from({ length: 24 }, (_, h) => {
            const c = data.find(x => x.dow === dow && x.hour === h);
            const i = c ? c.count / max : 0;
            return <div key={h} className="aspect-square rounded-[2px]" style={{ backgroundColor: i > 0 ? `rgba(6,182,212,${0.06 + i * 0.85})` : 'rgba(255,255,255,0.01)', boxShadow: i > 0.5 ? `0 0 3px rgba(6,182,212,${i*0.3})` : 'none' }} />;
          })}
        </div>
      ))}
    </div>
  );
}

// ========== Live Feed ==========
function LiveFeed({ events }: { events: any[] }) {
  if (!events?.length) return <p className="text-sm text-gray-700 text-center py-6">等待...</p>;
  return (
    <div className="space-y-1 overflow-hidden h-full">
      {events.map((e: any, i: number) => (
        <div key={i} className="flex items-start gap-2.5 py-2 border-b border-white/[0.02] last:border-0">
          <div className="size-2 rounded-full mt-2 shrink-0" style={{ backgroundColor: EVENT_COLORS[e.type] || '#6b7280', boxShadow: `0 0 6px ${EVENT_COLORS[e.type] || '#6b7280'}60` }} />
          <div className="flex-1 min-w-0">
            <span className="text-sm font-medium text-gray-300">{e.user}</span>
            <span className="text-sm text-gray-500 ml-1.5">{e.detail}</span>
          </div>
          <span className="text-xs text-gray-700 font-mono shrink-0">{e.time}</span>
        </div>
      ))}
    </div>
  );
}

// ========== Online Users Panel ==========
// API 路径 → 人话页面名
const PATH_NAME_MAP: [RegExp | string, string][] = [
  [/^\/api\/diagnosis/, '做诊断'],
  [/^\/api\/content\/chat/, '对话模式工坊'],
  [/^\/api\/content\/generate-script/, '写脚本'],
  [/^\/api\/content\/batch-generate/, '批量生成'],
  [/^\/api\/content\/quick-generate-stream/, '快速生成'],
  [/^\/api\/content\/generate-topics/, '生成选题'],
  [/^\/api\/content\/generate-hook/, '生成开篇'],
  [/^\/api\/keyword-selection\/.*\/generate-quote/, '生成报价'],
  [/^\/api\/keyword-selection/, '选词'],
  [/^\/api\/placement\/generate/, '投放建议'],
  [/^\/api\/placement/, '投放中心'],
  [/^\/api\/social\/rewrite/, '爆款仿写'],
  [/^\/api\/research\/user-breakdown/, '博主拆解'],
  [/^\/api\/research/, '研究中心'],
  [/^\/api\/advisors\/.*\/chat/, '和专家对话'],
  [/^\/api\/advisors/, '专家市场'],
  [/^\/api\/interview/, 'AI建档'],
  [/^\/api\/agent\/chat/, 'AI助手对话'],
  [/^\/api\/agent/, 'AI助手'],
  [/^\/api\/monitoring/, '监测中心'],
  [/^\/api\/publish/, '发布中心'],
  [/^\/api\/wallet/, '钱包'],
  [/^\/api\/referral/, '推广中心'],
  [/^\/api\/my-clients/, '客户管理'],
  [/^\/api\/my-brand/, '我的品牌'],
  [/^\/api\/social\/teams/, '团队'],
  [/^\/api\/social\/corpus/, '语料中心'],
  [/^\/api\/social\/inspirations/, '选题灵感'],
  [/^\/api\/social\/personality/, '创作者画像'],
  [/^\/api\/admin/, '管理后台'],
];

function mapPathToPageName(path: string): string {
  if (!path) return '浏览页面';
  for (const [pattern, name] of PATH_NAME_MAP) {
    if (pattern instanceof RegExp ? pattern.test(path) : path.startsWith(pattern)) {
      return name;
    }
  }
  return '浏览页面';
}

function formatTimeAgo(iso: string | null): string {
  if (!iso) return '';
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 10) return '刚刚';
  if (diff < 60) return `${Math.floor(diff)}s前`;
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  return `${Math.floor(diff / 3600)}小时前`;
}

function formatLastAction(action: any): string {
  if (!action) return '';
  const { action: act, module, summary, entity_type } = action;
  if (summary) return `${act}《${summary}》`;
  if (entity_type) return `${act} · ${entity_type}`;
  if (module) return `${module} · ${act}`;
  return act || '';
}

function OnlineUsersPanel({ users }: { users: any[] }) {
  if (!users?.length) {
    return <p className="text-sm text-gray-700 text-center py-6">暂无在线用户</p>;
  }
  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between px-1 pb-1.5 shrink-0">
        <span className="text-[11px] text-cyan-700 tracking-wider">● 共 {users.length} 人</span>
        <span className="text-[10px] text-gray-800">近 1 分钟活跃</span>
      </div>
      <div className="flex-1 overflow-y-auto space-y-1.5 pr-0.5">
        {users.map((u: any) => {
          const pageName = mapPathToPageName(u.current_path);
          const actionText = formatLastAction(u.last_action);
          return (
            <div key={u.id} className="flex items-start gap-2 py-1.5 px-1.5 rounded border border-white/[0.03] bg-white/[0.015]">
              <div className="size-2 rounded-full mt-1.5 shrink-0 bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.6)] animate-pulse" />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5 text-xs">
                  <span className="font-medium text-gray-200 truncate">{u.name}</span>
                  <span className="text-gray-700">·</span>
                  <span className="text-cyan-400/80 truncate">{pageName}</span>
                  <span className="text-gray-700 ml-auto font-mono text-[10px] shrink-0">{formatTimeAgo(u.last_active_at)}</span>
                </div>
                {actionText && (
                  <div className="text-[11px] text-amber-300/80 mt-0.5 truncate font-medium">
                    {actionText}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ========== Trend Chart ==========
function TrendChart({ data }: { data: any[] }) {
  if (!data?.length) return <p className="text-sm text-gray-700 text-center py-3">暂无</p>;
  const option = useMemo(() => ({
    grid: { top: 8, right: 8, bottom: 22, left: 40 },
    tooltip: { trigger: 'axis' as const, backgroundColor: '#0a0f1e', borderColor: '#0e7490', textStyle: { color: '#e5e7eb', fontSize: 13 } },
    xAxis: { type: 'category' as const, data: data.map(d => d.date.slice(5)), axisLabel: { color: '#374151', fontSize: 10 }, axisLine: { lineStyle: { color: '#111827' } } },
    yAxis: { type: 'value' as const, axisLabel: { color: '#374151', fontSize: 10, formatter: '¥{value}' }, splitLine: { lineStyle: { color: '#0f172a' } } },
    series: [
      { name: '营收', type: 'line', data: data.map(d => d.revenue), smooth: true, lineStyle: { color: '#4ade80', width: 2 }, itemStyle: { color: '#4ade80' }, areaStyle: { color: new echarts.graphic.LinearGradient(0,0,0,1,[{offset:0,color:'rgba(74,222,128,0.15)'},{offset:1,color:'rgba(74,222,128,0)'}])}, symbol: 'none' },
    ],
  }), [data]);
  return <ReactEChartsCore echarts={echarts} option={option} style={{ height: '100%' }} />;
}

// ========== Ticker ==========
function Ticker({ funnel, finance, content, pub }: { funnel: any; finance: any; content: any; pub: any }) {
  const items = [
    ['总用户', funnel?.total_users || 0],
    ['建档率', `${funnel?.profile_rate || 0}%`],
    ['面试完成', funnel?.interview_done || 0],
    ['画像Lv3+', `${funnel?.lv3_count || 0}人`],
    ['付费率', `${funnel?.paid_rate || 0}%`],
    ['7日活跃率', `${funnel?.week_active_rate || 0}%`],
    ['今日营收', `¥${finance?.today_revenue?.toLocaleString() || 0}`],
    ['月营收', `¥${finance?.month_revenue?.toLocaleString() || 0}`],
    ['代发成功', pub?.published || 0],
    ['今日发布', pub?.today_published || 0],
    ['选题', content?.topics || 0],
    ['脚本', content?.scripts || 0],
    ['GEO文章', content?.articles || 0],
  ];
  const repeated = [...items, ...items, ...items, ...items];
  return (
    <div className="border-t border-cyan-900/15 bg-[#050810] overflow-hidden relative shrink-0">
      <div className="absolute left-0 top-0 bottom-0 w-20 bg-gradient-to-r from-[#050810] to-transparent z-10" />
      <div className="absolute right-0 top-0 bottom-0 w-20 bg-gradient-to-l from-[#050810] to-transparent z-10" />
      <div className="flex items-center gap-12 py-2 px-4 ticker-scroll whitespace-nowrap">
        {repeated.map(([k, v], i) => (
          <span key={i} className="text-sm inline-flex items-center gap-2">
            <span className="size-1.5 rounded-full bg-cyan-800" />
            <span className="text-cyan-800">{k}</span>
            <span className="text-cyan-300 font-mono font-bold">{v ?? '—'}</span>
          </span>
        ))}
      </div>
      <style>{`@keyframes ts{0%{transform:translateX(0)}100%{transform:translateX(-25%)}}.ticker-scroll{animation:ts 35s linear infinite}`}</style>
    </div>
  );
}
