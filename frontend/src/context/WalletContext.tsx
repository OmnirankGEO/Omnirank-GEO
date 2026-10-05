/**
 * 积分钱包上下文
 * 管理双钱包余额、扣费、充值后刷新
 */
import React, { createContext, lazy, Suspense, useContext, useState, useCallback, useEffect, useRef, ReactNode } from 'react';
import { authFetch } from '@/lib/api';
import { agentApi, type AgentChannelTierState } from '@/lib/v35w2Api';
import { useAuth } from '@/context/AuthContext';
import { lazyToast } from '@/lib/lazyToast';
const ChargeNotifyGuide = lazy(() =>
  import('@/components/wallet/ChargeNotifyGuide').then((module) => ({
    default: module.ChargeNotifyGuide,
  })),
);

// ========== 类型定义 ==========

export type AgentLevel = 'free' | 'paid' | 'L1' | 'L2';
export type WalletStatus = 'initial' | 'loading' | 'ready' | 'stale' | 'error';

/**
 * 扣费提醒强度(P5b 全站静默扣费 + 提醒偏好)
 *  - 'each'  每次都提醒:扣完弹一条 toast
 *  - 'quiet' 安静一点:不弹 toast · 只亮导航红点
 *  - 'off'   不主动提醒:全沉默(只账单可查)
 *  - null    未设置:首次扣费时弹一次引导卡让用户选
 */
export type ChargeNotifyLevel = 'each' | 'quiet' | 'off';

/**
 * V3.5 客户授权算力池(customer_agent_credit_wallets)
 * - tool / publish / bonus 三轨 · 跟 user_wallets 平行
 * - 客户视角可消费余额 = user_wallets paid + customer_credit tool+publish
 * - 2026-06-08 P0 引入(原"充值算力"顶部 0 反馈"没到账"真因)
 */
interface CustomerCredit {
  toolCreditPoints: number;
  publishCreditPoints: number;
  bonusCreditPoints: number;
  totalPurchasedPoints: number;
  totalConsumedPoints: number;
}

interface WalletState {
  paidPoints: number;
  /** 佣金积分（v3.4）— 代理佣金转换本金，可消费可提现（提现功能研发中） */
  commissionPoints: number;
  bonusPoints: number;
  /** 冻结中积分（异步长任务如诊断/监测启动时会冻结，任务完成才正式扣费） */
  frozenPoints: number;
  totalRecharged: number;
  agentLevel: AgentLevel;
  /** 扣费偏好（仅代理可切）：'default' 赠送→佣金→充值 / 'agent_friendly' 赠送→充值→佣金 */
  deductionPreference: 'default' | 'agent_friendly';
  loading: boolean;
  /** 资金读取状态；initial/error 绝不能按真实 0 展示，stale 只能展示上次成功值。 */
  status: WalletStatus;
  errorMessage: string | null;
  lastUpdatedAt: string | null;
  /** 防切换账号时短暂展示前一账号资金。 */
  accountUserId: number | string | null;
  /** V3.1 订阅:当前有效订阅 id(null = 未订阅 / free 套餐) */
  activeSubscriptionId: number | null;
  /** V3.1 订阅:当前套餐 plan_id(free / personal / growth / agency / partner) */
  activePlanId: string | null;
  /** V3.1 订阅:到期时间 ISO 字符串 */
  subscriptionExpiresAt: string | null;
  /** V3.1 订阅:是否自动续费 */
  subscriptionAutoRenew: boolean;
  /** V3.1 待清算佣金积分(退款触发的 clawback 还没扣完) */
  pendingClawbackPoints: number;
  /** P5b 扣费提醒强度(null = 未设置,首次扣费弹引导) */
  chargeNotifyLevel: ChargeNotifyLevel | null;
  /** V3.5 客户授权算力池(必须合并到顶部显示) */
  customerCredit: CustomerCredit;
  /** 渠道成长等级 · 仅服务方内部展示 */
  channelTier: AgentChannelTierState | null;
  channelTierEnabled: boolean;
  /**
   * [客户线上购买门控 2026-07-29] 能否线上直接购买。
   *
   * **唯一判定源是后端** GET /api/wallet 的 can_purchase_online —— 前端绝不
   * 自己拼"客户级覆盖 + 主账号默认"两级逻辑,否则前后端判定必然漂移。
   * 读不到 / 出错时保持 true(不拦),与后端 fail-open 同口径。
   */
  canPurchaseOnline: boolean;
}

interface DeductResult {
  success: boolean;
  remaining_paid: number;
  remaining_bonus: number;
  error?: string;
}

interface TestModeConfig {
  paidPoints: number;
  commissionPoints?: number;
  bonusPoints: number;
  frozenPoints?: number;
  agentLevel: AgentLevel;
  totalRecharged: number;
  deductionPreference?: 'default' | 'agent_friendly';
  // V3.1 订阅字段(可选 · admin 模拟 growth 用户等场景)
  activeSubscriptionId?: number | null;
  activePlanId?: string | null;
  subscriptionExpiresAt?: string | null;
  subscriptionAutoRenew?: boolean;
  pendingClawbackPoints?: number;
  chargeNotifyLevel?: ChargeNotifyLevel | null;
  channelTier?: AgentChannelTierState | null;
  channelTierEnabled?: boolean;
  /** [客户线上购买门控 2026-07-29] 缺省 true(不拦),与真实链路 fail-open 同口径 */
  canPurchaseOnline?: boolean;
}

/** 空 CustomerCredit · 默认值 · DRY 防重复字面量 */
const EMPTY_CUSTOMER_CREDIT: CustomerCredit = {
  toolCreditPoints: 0,
  publishCreditPoints: 0,
  bonusCreditPoints: 0,
  totalPurchasedPoints: 0,
  totalConsumedPoints: 0,
};

function authAgentLevel(agentLevel: number | undefined): AgentLevel {
  if ((agentLevel ?? 0) >= 2) return 'L2';
  if ((agentLevel ?? 0) >= 1) return 'L1';
  return 'free';
}

function emptyWalletState(accountUserId: number | string | null = null): WalletState {
  return {
    paidPoints: 0,
    commissionPoints: 0,
    bonusPoints: 0,
    frozenPoints: 0,
    totalRecharged: 0,
    agentLevel: 'free',
    deductionPreference: 'default',
    loading: true,
    status: 'initial',
    errorMessage: null,
    lastUpdatedAt: null,
    accountUserId,
    activeSubscriptionId: null,
    activePlanId: null,
    subscriptionExpiresAt: null,
    subscriptionAutoRenew: false,
    pendingClawbackPoints: 0,
    chargeNotifyLevel: null,
    customerCredit: EMPTY_CUSTOMER_CREDIT,
    channelTier: null,
    channelTierEnabled: false,
    // 未读到后端判定前一律按"可购买"渲染:门控失灵绝不能把付款路堵死。
    canPurchaseOnline: true,
  };
}

function requiredWalletNumber(wallet: Record<string, unknown>, key: string): number {
  const value = wallet[key];
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error('钱包返回的数据不完整');
  }
  return value;
}

async function walletResponseError(response: Response): Promise<string> {
  if (response.status === 401) return '登录状态已过期，请重新登录后再查看余额';
  if (response.status === 400) return '余额请求未被接受，请重试；这不代表余额为 0，也不会因此扣钱';
  if (response.status === 402) return '当前功能可用算力不足；请先确认算力口径，本次查询没有扣钱';
  if (response.status === 403) return '当前账号没有查看余额的权限，请联系管理员';
  if (response.status === 404) return '暂时找不到钱包资料，请联系管理员；这不代表余额为 0';
  if (response.status === 409) return '余额正在更新，请稍后重试；请勿重复操作';
  if (response.status === 422) return '余额资料校验失败，请联系管理员；这不代表余额为 0';
  if (response.status === 429) return '余额查询太频繁，请稍后再试；这不代表余额为 0';
  if (response.status >= 500) return '余额服务暂时不可用，请稍后重试；这不代表余额为 0';
  let detail: unknown;
  try {
    detail = (await response.clone().json())?.detail;
  } catch {
    detail = null;
  }
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: unknown; msg?: unknown }).message
      ?? (detail as { msg?: unknown }).msg;
    if (typeof message === 'string' && message.trim()) return message;
  }
  return `暂时无法读取余额，请重试；这不代表余额为 0`;
}

function walletRetryAfterMs(response: Response): number {
  const raw = response.headers.get('Retry-After')?.trim();
  if (!raw) return 30_000;
  const seconds = Number(raw);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
  const dateMs = Date.parse(raw);
  if (Number.isFinite(dateMs)) return Math.max(0, dateMs - Date.now());
  return 30_000;
}

interface WalletContextType extends WalletState {
  /** 总余额展示值 = paid + tool + publish + bonus + commission · 仅顶部 chip / WalletPage 展示用 · 严禁预检 */
  totalPoints: number;
  cnyEquivalent: string;
  /** V3.5 客户视角"充值算力"展示值 = paidPoints + customer_credit.tool + customer_credit.publish · 给 WalletPage 顶部卡 · 不可预检 */
  paidPointsDisplayed: number;
  /** V3.5 客户视角"赠送算力"展示值 = bonusPoints + customer_credit.bonus_credit */
  bonusPointsDisplayed: number;
  /**
   * V3.5 v8 [P0 2026-06-08 Codex 复审]工具类预检可用余额(诊断/写作/监测/advisor 对话/...)
   *  = paid + tool_credit + bonus + bonus_credit + commission
   *  ⚠️ 工具类 feature 预检【必须】用这个 · 不能用 totalPoints(含 publish 误判)
   */
  toolPointsAvailable: number;
  /**
   * V3.5 v8 [P0 2026-06-08 老板拍板]发布类预检可用余额(media_publish 类)
   *  = paid + tool_credit + publish_credit + commission
   *  铁律:"充值算力可发布 · 赠送禁发布" · tool_credit 也是充值性质(代理代充)·可兜底发布
   *  ⚠️ 发布类 feature 预检【必须】用这个 · 不能用 totalPoints(误含 bonus)
   */
  publishPointsAvailable: number;
  fetchBalance: () => Promise<void>;
  refreshBalance: () => Promise<void>;
  deductPoints: (featureCode: string, featureName: string) => Promise<DeductResult>;
  /** 管理员测试模式：覆盖钱包数据，模拟不同身份 */
  setTestMode: (config: TestModeConfig | null) => void;
  testMode: boolean;

  // ===== P5b 全站静默扣费 + 提醒偏好 =====
  /** 当前扣费提醒强度(null = 未设置) */
  chargeNotifyLevel: ChargeNotifyLevel | null;
  /** 写偏好(PATCH /api/wallet/charge-notify-preference) · 乐观更新 */
  setChargeNotifyLevel: (level: ChargeNotifyLevel) => Promise<void>;
  /**
   * 扣费已发生 → 按偏好通知(静默化的统一通知入口)
   *  - each : sonner toast「已使用 N 工具算力 · 已记到账单，可在钱包查看」
   *  - quiet: 不弹 toast · 只亮导航红点(派发 wallet:charge-quiet 事件)
   *  - off  : 全沉默
   *  - null : 首次扣费 → 弹一次引导卡(只弹一次)
   * @param points 扣了多少工具算力(可选 · 缺省时文案不带数字)
   */
  notifyCharge: (points?: number) => void;
  /**
   * 长任务先占用算力的提示(freeze 型,如诊断/监测启动)
   * 始终 toast(占用心智很重要)·「已先占用 N 工具算力 · 做成才会正式扣，失败会自动退回」
   * off 偏好也会弹(这是"占用"不是"扣费完成",必须让用户知道算力被预留了)
   */
  notifyFreezeHold: (points?: number) => void;
}

// 静默偏好下的"安静红点"事件(NotificationBell 监听 → 亮蓝点)
export const WALLET_CHARGE_QUIET_EVENT = 'wallet:charge-quiet';

const EXCHANGE_RATE = 130;
const SOCIAL_STUDIO_PATHS = new Set([
  'login',
  'approval',
  'match',
  'chat',
  'history',
  'persona-setup',
  'my-ip',
  'video-script',
  'stats',
  'experts',
  'managed',
  'workshop',
  'content-workshop',
  'ideas',
  'topics',
  'rewrite',
  'imitate',
  'planning',
  'plan',
  'profile',
  'creator-profile',
  'my-profile',
  'clients',
  'my-clients',
  'advisors',
  'advisor-market',
  'review',
  'data-review',
  'interaction',
  'interaction-management',
  'interview',
  'creator-test',
  'corpus',
  'team',
  'scripts',
  'research',
  'trending',
  'operation',
  'home',
]);

function isPublicSelectionRoute(pathname: string) {
  const parts = pathname.split('/').filter(Boolean);
  return parts.length === 2 && parts[0] === 's' && !SOCIAL_STUDIO_PATHS.has(parts[1]);
}

const WalletContext = createContext<WalletContextType | null>(null);

// ========== Provider ==========

export function WalletProvider({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const userId = auth.user?.id || (auth.user as { user_id?: number } | null)?.user_id;
  const authorizationScope = auth.authorizationScope;
  const identityAgentLevel = authAgentLevel(auth.user?.agent_level);
  const [state, setState] = useState<WalletState>(() => emptyWalletState(userId ?? null));
  const [dataOwnerScope, setDataOwnerScope] = useState<string | null>(null);
  const dataOwnerScopeRef = useRef<string | null>(null);
  const [testConfig, setTestConfig] = useState<TestModeConfig | null>(null);
  const requestSequenceRef = useRef(0);
  const balanceAbortRef = useRef<AbortController | null>(null);
  const balanceInFlightRef = useRef<{ scope: string; request: Promise<void> } | null>(null);
  const retryNotBeforeRef = useRef(0);
  const retryOwnerScopeRef = useRef<string | null>(null);
  const lastSuccessAtRef = useRef(0);
  const channelTierAbortRef = useRef<AbortController | null>(null);
  const activeUserIdRef = useRef<number | string | null>(userId ?? null);
  const activeAuthorizationScopeRef = useRef(authorizationScope);
  activeUserIdRef.current = userId ?? null;
  activeAuthorizationScopeRef.current = authorizationScope;
  dataOwnerScopeRef.current = dataOwnerScope;

  // P5b 首次扣费引导卡开关(charge_notify_level == null 时第一次扣费弹一次)
  const [guideOpen, setGuideOpen] = useState(false);
  // 防同一会话内引导反复弹(即使后端 PATCH 还没回来)
  const guideShownRef = useRef(false);

  // 2026-04-18 v3.4: 升级代理瞬间弹一次 toast（localStorage 标记避免重弹）
  const prevLevelRef = useRef<AgentLevel | null>(null);

  // P5b: deductPoints 在 notifyCharge 声明之前定义,用 ref 转发避免 hoisting 问题
  const notifyChargeRef = useRef<(points?: number) => void>(() => {});

  // P5b(对抗 review P1-2): 实时镜像总余额(paid+bonus)· deductPoints 算"扣了多少"用最新值,
  // 避免 async 扣费期间用闭包 stale 余额导致提示数字偏差
  const balanceRef = useRef(0);
  useEffect(() => {
    balanceRef.current = state.paidPoints + state.bonusPoints;
  }, [state.paidPoints, state.bonusPoints]);

  const fetchBalance = useCallback(async () => {
    const requestedUserId = userId ?? null;
    const requestedScope = authorizationScope;
    channelTierAbortRef.current?.abort();
    channelTierAbortRef.current = null;
    if (requestedUserId === null) return;
    if (retryOwnerScopeRef.current === requestedScope && Date.now() < retryNotBeforeRef.current) return;
    const existing = balanceInFlightRef.current;
    if (existing?.scope === requestedScope) return await existing.request;
    balanceAbortRef.current?.abort();
    const deferred = { resolve: () => {} };
    const inFlight = new Promise<void>((resolve) => { deferred.resolve = resolve; });
    balanceInFlightRef.current = { scope: requestedScope, request: inFlight };
    const requestSequence = ++requestSequenceRef.current;
    const abortController = new AbortController();
    balanceAbortRef.current = abortController;
    const timeoutId = window.setTimeout(() => abortController.abort(), 15_000);
    setState(prev => {
      const sameAccount = prev.accountUserId === requestedUserId && dataOwnerScopeRef.current === requestedScope;
      const base = sameAccount ? prev : emptyWalletState(requestedUserId);
      return {
        ...base,
        agentLevel: identityAgentLevel,
        loading: true,
        status: 'loading',
        errorMessage: null,
        accountUserId: requestedUserId,
      };
    });
    try {
      const res = await authFetch('/api/wallet', { signal: abortController.signal });
      if (requestSequence !== requestSequenceRef.current
        || activeUserIdRef.current !== requestedUserId
        || activeAuthorizationScopeRef.current !== requestedScope) return;
      if (!res.ok) {
        if (res.status === 429) {
          retryOwnerScopeRef.current = requestedScope;
          retryNotBeforeRef.current = Date.now() + walletRetryAfterMs(res);
        }
        throw new Error(await walletResponseError(res));
      }
      retryNotBeforeRef.current = 0;
      retryOwnerScopeRef.current = null;
      const data = await res.json().catch(() => { throw new Error('余额响应无法读取，请重试；这不代表余额为 0'); });
      if (!data || data.success !== true || !data.data || typeof data.data !== 'object') {
        throw new Error('余额响应不完整，请重试；这不代表余额为 0');
      }
      const wallet = data.data as Record<string, unknown>;
      const paidPoints = requiredWalletNumber(wallet, 'paid_points');
      const commissionPoints = requiredWalletNumber(wallet, 'commission_points');
      const bonusPoints = requiredWalletNumber(wallet, 'bonus_points');
      const frozenPoints = requiredWalletNumber(wallet, 'frozen_points');
      const totalRecharged = requiredWalletNumber(wallet, 'total_recharged');
      const mappedLevel = identityAgentLevel;

        // 升级检测（free → 代理；底层 agentLevel 字段保留 L1/L2 仅作数据层标识）
        const prev = prevLevelRef.current;
        if (prev === 'free' && (mappedLevel === 'L1' || mappedLevel === 'L2')) {
          const shownKey = `agent_upgrade_toast_shown_${mappedLevel}`;
          if (typeof window !== 'undefined' && !localStorage.getItem(shownKey)) {
            try {
              lazyToast.success(
                '恭喜成为服务商！现在把链接发给客户，成交后可获得服务收益（可提现）。' +
                '历史朋友的首次充值按当时身份结算，不补差额。',
                { duration: 8000 }
              );
              localStorage.setItem(shownKey, '1');
            } catch { /* ignore */ }
          }
        }
        prevLevelRef.current = mappedLevel;

        // V3.5 客户授权算力池是当前功能可用算力 SSOT。只有后端明确标记 ready 且字段完整，
        // 才允许钱包进入 ready；缺失/查询失败绝不能降级为 0。
        if (wallet.customer_credit_status !== 'ready') {
          throw new Error('当前功能可用算力暂时无法确认，请稍后重试；这不代表算力为 0');
        }
        const ccRaw = wallet.customer_credit && typeof wallet.customer_credit === 'object'
          ? wallet.customer_credit as Record<string, unknown>
          : null;
        if (!ccRaw) {
          throw new Error('当前功能可用算力响应不完整，请重试；这不代表算力为 0');
        }
        const optionalNumber = (value: unknown, fallback = 0) =>
          typeof value === 'number' && Number.isFinite(value) ? value : fallback;
        const customerCredit: CustomerCredit = {
          toolCreditPoints: requiredWalletNumber(ccRaw, 'tool_credit_points'),
          publishCreditPoints: requiredWalletNumber(ccRaw, 'publish_credit_points'),
          bonusCreditPoints: requiredWalletNumber(ccRaw, 'bonus_credit_points'),
          totalPurchasedPoints: requiredWalletNumber(ccRaw, 'total_purchased_points'),
          totalConsumedPoints: requiredWalletNumber(ccRaw, 'total_consumed_points'),
        };

        if (requestSequence !== requestSequenceRef.current
          || activeUserIdRef.current !== requestedUserId
          || activeAuthorizationScopeRef.current !== requestedScope) return;
        lastSuccessAtRef.current = Date.now();
        setDataOwnerScope(requestedScope);
        setState({
          paidPoints,
          commissionPoints,
          bonusPoints,
          frozenPoints,
          totalRecharged,
          agentLevel: mappedLevel,
          deductionPreference: (wallet.deduction_preference === 'agent_friendly' ? 'agent_friendly' : 'default'),
          loading: false,
          status: 'ready',
          errorMessage: null,
          lastUpdatedAt: new Date().toISOString(),
          accountUserId: requestedUserId,
          // V3.1 订阅字段(db/wallet_db.py R3 patch 已 prod 部署)
          activeSubscriptionId: typeof wallet.active_subscription_id === 'number' ? wallet.active_subscription_id : null,
          activePlanId: typeof wallet.active_plan_id === 'string' ? wallet.active_plan_id : null,
          subscriptionExpiresAt: typeof wallet.subscription_expires_at === 'string' ? wallet.subscription_expires_at : null,
          subscriptionAutoRenew: wallet.subscription_auto_renew === true,
          pendingClawbackPoints: optionalNumber(wallet.pending_clawback_points),
          // P5b: 后端 GET /api/wallet 也返回 charge_notify_level(null = 未设置)
          chargeNotifyLevel: (wallet.charge_notify_level === 'each' || wallet.charge_notify_level === 'quiet' || wallet.charge_notify_level === 'off')
            ? wallet.charge_notify_level
            : null,
          customerCredit,
          channelTier: null,
          channelTierEnabled: false,
          // 只认后端显式 false;字段缺失/非布尔一律 true(fail-open)。
          canPurchaseOnline: wallet.can_purchase_online !== false,
        });

        // 渠道成长等级只是服务商附加信息，不能阻塞钱包资金状态。独立后台读取并限时 3 秒；
        // 超时/失败保持 null，钱包仍维持可信 ready。
        if (mappedLevel === 'L1' || mappedLevel === 'L2') {
          const tierOwnerSequence = requestSequence;
          window.setTimeout(() => {
            if (tierOwnerSequence !== requestSequenceRef.current
              || activeUserIdRef.current !== requestedUserId
              || activeAuthorizationScopeRef.current !== requestedScope
              || document.visibilityState === 'hidden') return;
            const tierAbortController = new AbortController();
            channelTierAbortRef.current = tierAbortController;
            const tierTimeoutId = window.setTimeout(() => tierAbortController.abort(), 3_000);
            void agentApi.channelTierMe(tierAbortController.signal)
              .then(tierRes => {
                if (tierOwnerSequence !== requestSequenceRef.current
                  || activeUserIdRef.current !== requestedUserId
                  || activeAuthorizationScopeRef.current !== requestedScope) return;
                const channelTier = tierRes.channel_tier || null;
                setState(prev => prev.accountUserId === requestedUserId
                  ? { ...prev, channelTier, channelTierEnabled: !!channelTier?.enabled }
                  : prev);
              })
              .catch(() => undefined)
              .finally(() => {
                window.clearTimeout(tierTimeoutId);
                if (channelTierAbortRef.current === tierAbortController) {
                  channelTierAbortRef.current = null;
                }
              });
          }, 2_500);
        }
    } catch (error) {
      if (requestSequence !== requestSequenceRef.current
        || activeUserIdRef.current !== requestedUserId
        || activeAuthorizationScopeRef.current !== requestedScope) return;
      const rawMessage = error instanceof Error ? error.message : '';
      const message = error instanceof DOMException && error.name === 'AbortError'
        ? '余额读取超时，请稍后重试；这不代表余额为 0'
        : (/Failed to fetch|NetworkError|Load failed/i.test(rawMessage)
          ? '网络连接异常，请检查网络后重试；这不代表余额为 0'
          : (rawMessage || '网络连接异常，请检查网络后重试；这不代表余额为 0'));
      setState(prev => {
        const hasSuccessfulValue = prev.accountUserId === requestedUserId
          && dataOwnerScopeRef.current === requestedScope
          && prev.lastUpdatedAt !== null;
        return {
          ...(prev.accountUserId === requestedUserId ? prev : emptyWalletState(requestedUserId)),
          agentLevel: identityAgentLevel,
          loading: false,
          status: hasSuccessfulValue ? 'stale' : 'error',
          errorMessage: message,
          accountUserId: requestedUserId,
        };
      });
    } finally {
      window.clearTimeout(timeoutId);
      if (balanceAbortRef.current === abortController) balanceAbortRef.current = null;
      if (balanceInFlightRef.current?.request === inFlight) balanceInFlightRef.current = null;
      deferred.resolve();
    }
  }, [authorizationScope, identityAgentLevel, userId]);

  useEffect(() => () => {
    balanceAbortRef.current?.abort();
    balanceAbortRef.current = null;
    balanceInFlightRef.current = null;
    channelTierAbortRef.current?.abort();
    channelTierAbortRef.current = null;
  }, []);

  const refreshBalance = useCallback(async () => {
    await fetchBalance();
  }, [fetchBalance]);

  const deductPoints = useCallback(async (featureCode: string, _featureName: string): Promise<DeductResult> => {
    if (!testConfig && state.status !== 'ready') {
      return {
        success: false,
        remaining_paid: state.paidPoints,
        remaining_bonus: state.bonusPoints,
        error: state.status === 'stale'
          ? '余额数据可能已过期，请先刷新余额再继续'
          : '暂时无法确认余额，请先重试；本次没有发起扣费',
      };
    }
    try {
      const res = await authFetch('/api/wallet/deduct', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ feature_code: featureCode }),
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => null);
        return {
          success: false,
          remaining_paid: state.paidPoints,
          remaining_bonus: state.bonusPoints,
          error: typeof errData?.detail === 'string'
            ? errData.detail
            : (typeof errData?.detail?.message === 'string' ? errData.detail.message : '扣费失败，请稍后重试'),
        };
      }

      const data = await res.json();
      const deduct = data.data || data;
      if (deduct.success !== false && data.success !== false) {
        // P5b: 扣费成功 → 按偏好后置通知(从 paid+bonus 余额差算出扣了多少)
        const beforeTotal = balanceRef.current; // 实时余额(非 async 闭包 stale · 对抗 review P1-2)
        const afterTotal = (deduct.remaining_paid ?? state.paidPoints) + (deduct.remaining_bonus ?? state.bonusPoints);
        const charged = Math.max(0, beforeTotal - afterTotal);
        notifyChargeRef.current(charged > 0 ? charged : undefined);
        setState(prev => ({
          ...prev,
          paidPoints: deduct.remaining_paid ?? prev.paidPoints,
          bonusPoints: deduct.remaining_bonus ?? prev.bonusPoints,
        }));
        // 延迟 1 秒从服务器重新拉余额，确保和后端一致
        setTimeout(() => fetchBalance(), 1000);
      }
      return {
        success: data.success,
        remaining_paid: deduct.remaining_paid ?? state.paidPoints,
        remaining_bonus: deduct.remaining_bonus ?? state.bonusPoints,
        error: deduct.error ?? data.error,
      };
    } catch {
      return {
        success: false,
        remaining_paid: state.paidPoints,
        remaining_bonus: state.bonusPoints,
        error: '网络错误，请稍后重试',
      };
    }
  }, [fetchBalance, state.paidPoints, state.bonusPoints, state.status, testConfig]);

  useEffect(() => {
    const p = typeof window !== 'undefined' ? window.location.pathname : '';
    // v1_3 (CTO-15.1 2026-04-20 老板反馈"积分全消失"根因):
    // 原 '/s/' 前缀整段被判 public → 社媒 C 端用户（/s/chat, /s/my-ip 等）钱包永远 fetch 不到
    // → 三轨全 0 假象（后端实际 1,000,088）· Deploy-CTO 诊断 frozen=0 证实是纯前端 bug
    // 修复：精确匹配公开门户 /s/:token（SelectionPage 一级路径），不放过整个 /s/*
    // /s/chat /s/my-ip /s/rewrite 等登录后路由照常 fetch 钱包
    const isPublicMToken = /^\/m\/[^/]+$/.test(p);            // /m/:token 物料确认
    const isPublicSToken = isPublicSelectionRoute(p);
    const isPublicPage = isPublicMToken || isPublicSToken || p.startsWith('/portal') || p === '/landing';
    if (isPublicPage) {
      balanceAbortRef.current?.abort();
      balanceAbortRef.current = null;
      balanceInFlightRef.current = null;
      channelTierAbortRef.current?.abort();
      channelTierAbortRef.current = null;
      requestSequenceRef.current += 1;
      setState(prev => ({ ...prev, loading: false, status: 'initial', channelTier: null, channelTierEnabled: false }));
      return;
    }
    if (userId) {
      balanceAbortRef.current?.abort();
      balanceAbortRef.current = null;
      balanceInFlightRef.current = null;
      channelTierAbortRef.current?.abort();
      channelTierAbortRef.current = null;
      requestSequenceRef.current += 1;
      retryNotBeforeRef.current = 0;
      retryOwnerScopeRef.current = null;
      lastSuccessAtRef.current = 0;
      // The new scope owns a deliberately empty initial/error state immediately. This
      // keeps the previous scope's balance out of the DOM while still allowing failures
      // for the current session to render honestly instead of looking like endless load.
      setDataOwnerScope(authorizationScope);
      setState({ ...emptyWalletState(userId), agentLevel: identityAgentLevel });
      fetchBalance();
    } else {
      balanceAbortRef.current?.abort();
      balanceAbortRef.current = null;
      balanceInFlightRef.current = null;
      channelTierAbortRef.current?.abort();
      channelTierAbortRef.current = null;
      requestSequenceRef.current += 1;
      setDataOwnerScope(null);
      setState({ ...emptyWalletState(null), loading: false });
    }
  }, [authorizationScope, fetchBalance, identityAgentLevel, userId]);

  // ==========================================
  // Layered Defense 余额自动同步(2026-05-12 老板 P0 · "关乎钱,稳定第一")
  // ==========================================
  //
  // Layer 1: authFetch 内 POST/PUT/PATCH/DELETE 2xx → dispatch 'wallet:auto-refresh'(主防线)
  //          见 frontend/src/lib/api.ts shouldTriggerWalletRefresh + scheduleWalletRefresh
  // Layer 2: visibilitychange + window.focus → 切 tab 回来立即刷新
  // Layer 3: 可见时 120s 兜底 polling → 抓 cron 月底重置 / 后端别处扣费 / 漏拦截
  // Layer 4: window.dispatchEvent(new CustomEvent('wallet:refresh')) → 业务方按需精确触发
  //
  // 防递归 + 性能:
  //   · 公开页 / 未登录 / testMode 全部跳过(0 触发)
  //   · fetchBalance 本身用 setState · React 18 自动 batch · 多源并发不会爆 setState
  //   · debounce 500ms 在 authFetch 端做 · 这里只接 event
  //   · 120s polling 选择 = 兜底,主防线 < 500ms 已覆盖 90% 场景
  useEffect(() => {
    if (typeof window === 'undefined') return;
    if (!userId) return;
    if (testConfig) return; // testMode 不刷,免动数据
    const p = window.location.pathname;
    const isPublicMToken = /^\/m\/[^/]+$/.test(p);
    const isPublicSToken = isPublicSelectionRoute(p);
    const isPublicPage = isPublicMToken || isPublicSToken || p.startsWith('/portal') || p === '/landing';
    if (isPublicPage) return;

    // 缓冲:同一 tick 内多次触发只跑一次(visibility + auto-refresh + event 可能同时来)
    let inflight = false;
    const safeFetch = async (force = false) => {
      if (inflight) return;
      if (document.visibilityState === 'hidden') return; // 后台 tab 不浪费请求
      // Browser focus can fire during initial paint (and repeatedly when several tabs are
      // restored). A fresh successful balance must not be fetched again immediately.
      if (!force && Date.now() - lastSuccessAtRef.current < 15_000) return;
      inflight = true;
      try {
        await fetchBalance();
      } finally {
        inflight = false;
      }
    };

    // Layer 1: authFetch 拦截事件
    const onAutoRefresh = () => { void safeFetch(true); };
    window.addEventListener('wallet:auto-refresh', onAutoRefresh);

    // Layer 4: 业务方按需精确触发的扩展点(语义跟 Layer 1 一样,留独立名字让业务方好理解)
    const onManualRefresh = () => { void safeFetch(true); };
    window.addEventListener('wallet:refresh', onManualRefresh);

    // Layer 2: tab 切回前台立即刷
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') void safeFetch();
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('focus', onVisibilityChange);

    // Layer 3: 可见时 120s 兜底 polling(抓 cron/服务端别处扣费/漏拦截)
    // 120s 平衡:用户感知延迟 vs 服务端 QPS(1 万活跃用户 × 30 reqs/h = 30 万/h · 可接受)
    const POLL_INTERVAL_MS = 120 * 1000;
    const pollTimer = window.setInterval(() => {
      void safeFetch();
    }, POLL_INTERVAL_MS);

    return () => {
      window.removeEventListener('wallet:auto-refresh', onAutoRefresh);
      window.removeEventListener('wallet:refresh', onManualRefresh);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('focus', onVisibilityChange);
      window.clearInterval(pollTimer);
    };
  }, [authorizationScope, userId, testConfig, fetchBalance]);

  // 测试模式 setTestMode(testConfig 已在前面声明)
  const setTestMode = useCallback((config: TestModeConfig | null) => {
    setTestConfig(config);
    if (config) {
      setDataOwnerScope(authorizationScope);
      setState({
        paidPoints: config.paidPoints,
        commissionPoints: config.commissionPoints ?? 0,
        bonusPoints: config.bonusPoints,
        frozenPoints: config.frozenPoints ?? 0,
        agentLevel: config.agentLevel,
        totalRecharged: config.totalRecharged,
        deductionPreference: config.deductionPreference ?? 'default',
        loading: false,
        status: 'ready',
        errorMessage: null,
        lastUpdatedAt: new Date().toISOString(),
        accountUserId: userId ?? null,
        // P3-b: TestModeConfig 可显式设 V3.1 订阅字段,缺省默认未订阅(free)
        activeSubscriptionId: config.activeSubscriptionId ?? null,
        activePlanId: config.activePlanId ?? null,
        subscriptionExpiresAt: config.subscriptionExpiresAt ?? null,
        subscriptionAutoRenew: config.subscriptionAutoRenew ?? false,
        pendingClawbackPoints: config.pendingClawbackPoints ?? 0,
        chargeNotifyLevel: config.chargeNotifyLevel ?? null,
        customerCredit: EMPTY_CUSTOMER_CREDIT,
        channelTier: config.channelTier ?? null,
        channelTierEnabled: config.channelTierEnabled ?? false,
        canPurchaseOnline: config.canPurchaseOnline ?? true,
      });
    } else {
      // 退出测试模式，重新拉取真实数据
      fetchBalance();
    }
  }, [authorizationScope, fetchBalance, userId]);

  const effectiveState: WalletState = testConfig ? {
    paidPoints: testConfig.paidPoints,
    commissionPoints: testConfig.commissionPoints ?? 0,
    bonusPoints: testConfig.bonusPoints,
    frozenPoints: testConfig.frozenPoints ?? 0,
    agentLevel: testConfig.agentLevel,
    totalRecharged: testConfig.totalRecharged,
    deductionPreference: testConfig.deductionPreference ?? 'default',
    loading: false,
    status: 'ready',
    errorMessage: null,
    lastUpdatedAt: state.lastUpdatedAt,
    accountUserId: userId ?? null,
    // P3-b: TestModeConfig 可显式设 V3.1 订阅字段,缺省默认未订阅(free)
    activeSubscriptionId: testConfig.activeSubscriptionId ?? null,
    activePlanId: testConfig.activePlanId ?? null,
    subscriptionExpiresAt: testConfig.subscriptionExpiresAt ?? null,
    subscriptionAutoRenew: testConfig.subscriptionAutoRenew ?? false,
    pendingClawbackPoints: testConfig.pendingClawbackPoints ?? 0,
    chargeNotifyLevel: testConfig.chargeNotifyLevel ?? null,
    customerCredit: EMPTY_CUSTOMER_CREDIT,
    channelTier: testConfig.channelTier ?? null,
    channelTierEnabled: testConfig.channelTierEnabled ?? false,
    canPurchaseOnline: testConfig.canPurchaseOnline ?? true,
  } : (state.accountUserId === (userId ?? null) && dataOwnerScope === authorizationScope
    ? state
    : { ...emptyWalletState(userId ?? null), agentLevel: identityAgentLevel });

  // ==========================================
  // P5b 全站静默扣费 + 提醒偏好
  // ==========================================

  // 写偏好(乐观更新 → 失败回滚) · 同时关掉引导卡
  const setChargeNotifyLevel = useCallback(async (level: ChargeNotifyLevel) => {
    setGuideOpen(false);
    const prevLevel = state.chargeNotifyLevel;
    // 乐观更新本地 state(testMode 下 effectiveState 走 testConfig,不污染真实写入)
    setState(prev => ({ ...prev, chargeNotifyLevel: level }));
    try {
      const res = await authFetch('/api/wallet/charge-notify-preference', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ level }),
      });
      if (!res.ok) {
        setState(prev => ({ ...prev, chargeNotifyLevel: prevLevel }));
        return;
      }
      const data = await res.json().catch(() => null);
      const serverLevel = data?.data?.charge_notify_level;
      if (serverLevel === 'each' || serverLevel === 'quiet' || serverLevel === 'off') {
        setState(prev => ({ ...prev, chargeNotifyLevel: serverLevel }));
      }
    } catch {
      setState(prev => ({ ...prev, chargeNotifyLevel: prevLevel }));
    }
  }, [state.chargeNotifyLevel]);

  // 扣费已发生 → 按偏好通知(静默化后的统一通知入口)
  const notifyCharge = useCallback((points?: number) => {
    const level = effectiveState.chargeNotifyLevel;
    // 未设置 → 首次扣费弹一次引导(本会话只弹一次)
    if (level == null) {
      if (!guideShownRef.current) {
        guideShownRef.current = true;
        setGuideOpen(true);
      }
      return;
    }
    if (level === 'off') return;
    if (level === 'quiet') {
      // 安静一点:不弹 toast · 只亮导航红点
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent(WALLET_CHARGE_QUIET_EVENT));
      }
      return;
    }
    // each:每次都提醒
    const n = (points && points > 0) ? `${points.toLocaleString()} ` : '';
    lazyToast.success(`已使用 ${n}算力 · 已记到账单，可在钱包查看`, { duration: 4000 });
  }, [effectiveState.chargeNotifyLevel]);

  // 长任务先占用算力的提示(freeze 型,如诊断/监测启动)· 按「扣费提醒设置」偏好(与 notifyCharge 统一·尊重"安静一点/不主动提醒")
  const notifyFreezeHold = useCallback((points?: number) => {
    const level = effectiveState.chargeNotifyLevel;
    // 未设置 → 弹一次引导(长任务用户也能见到偏好设置)
    if (level == null) {
      if (!guideShownRef.current) {
        guideShownRef.current = true;
        setGuideOpen(true);
      }
      return;
    }
    if (level === 'off') return;
    if (level === 'quiet') {
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent(WALLET_CHARGE_QUIET_EVENT));
      }
      return;
    }
    // each:每次都提醒
    const n = (points && points > 0) ? `${points.toLocaleString()} ` : '';
    lazyToast.message(`已先占用 ${n}算力 · 做成才会正式扣，失败会自动退回`, { duration: 5000 });
  }, [effectiveState.chargeNotifyLevel]);

  // 保持 ref 指向最新 notifyCharge,供 deductPoints(声明在前)调用
  useEffect(() => {
    notifyChargeRef.current = notifyCharge;
  }, [notifyCharge]);

  // V3.5 客户视角合并(P0 2026-06-08 · 老板 + Codex 复审):
  //   paidPointsDisplayed = paid + customer_credit.tool + customer_credit.publish(仅展示 · 顶部卡)
  //   bonusPointsDisplayed = bonus + customer_credit.bonus_credit
  //   totalPoints = 全部加和 · 仅展示 · 严禁预检(老板拍 + Codex 抓 P0)
  //
  // [V3.5 v8 P0 2026-06-08 老板拍板]算力二轨铁律:
  //   充值算力(paid + tool_credit + publish_credit + commission)→ 可换全功能含发布
  //   赠送算力(bonus + bonus_credit)→ 只工具 · 禁发布
  // 必须拆字段独立预检:toolPointsAvailable(工具)+ publishPointsAvailable(发布)
  // 否则 V3.5 客户 publish>>tool 时 totalPoints 误判够 · 后端 _v35_check_credit_only 拒 402
  const cc = effectiveState.customerCredit;
  const paidPointsDisplayed = effectiveState.paidPoints + cc.toolCreditPoints + cc.publishCreditPoints;
  const bonusPointsDisplayed = effectiveState.bonusPoints + cc.bonusCreditPoints;
  const totalPoints = paidPointsDisplayed + effectiveState.commissionPoints + bonusPointsDisplayed;
  // 工具预检可用:paid + tool + bonus + bonus_credit + commission(不含 publish · 严格隔离工具入口)
  const toolPointsAvailable = effectiveState.paidPoints + cc.toolCreditPoints
    + effectiveState.bonusPoints + cc.bonusCreditPoints + effectiveState.commissionPoints;
  // 发布预检可用:所有充值算力(老板拍 · tool_credit 也是充值性质可兜底)· 不含 bonus(赠送禁发布)
  const publishPointsAvailable = effectiveState.paidPoints + cc.toolCreditPoints + cc.publishCreditPoints + effectiveState.commissionPoints;
  const cnyEquivalent = (totalPoints / EXCHANGE_RATE).toFixed(2);

  return (
    <WalletContext.Provider
      value={{
        ...effectiveState,
        // 身份只来自 /api/auth/me；钱包响应与测试余额都不能改变 RBAC 身份。
        agentLevel: identityAgentLevel,
        totalPoints,
        cnyEquivalent,
        paidPointsDisplayed,
        bonusPointsDisplayed,
        toolPointsAvailable,
        publishPointsAvailable,
        fetchBalance,
        refreshBalance,
        deductPoints,
        setTestMode,
        testMode: testConfig !== null,
        chargeNotifyLevel: effectiveState.chargeNotifyLevel,
        setChargeNotifyLevel,
        notifyCharge,
        notifyFreezeHold,
      }}
    >
      {children}
      {/* P5b 首次扣费引导卡(非阻断 · 只弹一次) */}
      {guideOpen && (
        <Suspense fallback={null}>
          <ChargeNotifyGuide
            open
            onClose={() => setGuideOpen(false)}
            onChoose={(level) => { void setChargeNotifyLevel(level); }}
          />
        </Suspense>
      )}
    </WalletContext.Provider>
  );
}

// ========== Hook ==========

export function useWallet() {
  const ctx = useContext(WalletContext);
  if (!ctx) {
    throw new Error('useWallet must be used within WalletProvider');
  }
  return ctx;
}
