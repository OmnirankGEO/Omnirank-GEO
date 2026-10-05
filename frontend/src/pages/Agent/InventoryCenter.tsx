/**
 * V3.5 W2 · 代理库存中心
 *
 * 路由: /agent/inventory
 * 责任:
 * - 展示 paid_inventory / bonus_inventory 两池余额 + 30/10/0% 预警
 * - 进货档位选择 + 创建预付订单(走 W2 早分支)
 * - 进货 / 自动补 / 线下划拨 / revoke 流水
 * - 线下划拨弹窗(明示"从算力库存扣除")
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  agentApi,
  formatCents,
  formatPoints,
  getV35W2ApiErrorCode,
  type AgentCustomerLookupItem,
  type AgentInventoryPurchaseOption,
  type AgentInventoryPurchaseResponse,
} from '@/lib/v35w2Api';
import {
  usePricingSSOT,
  type PricingError,
  type ProcurementCatalogItem,
  type ProcurementTierProgress,
} from '@/hooks/usePricingSSOT';
import { MAX_AGENT_PURCHASE_AMOUNT_YUAN, yuanInputToCents } from '@/lib/inventoryPurchaseCatalog';
import { inventoryTransactionTypeLabel, poolLabel, empty, paymentChannelLabel } from '@/lib/v35Terminology';
import { authFetch, formatApiErrorForDisplay } from '@/lib/api';
import { cn } from '@/lib/utils';
import { useWallet } from '@/context/WalletContext';
import { useAuth } from '@/context/AuthContext';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { AGENT_AGREEMENT_URL } from '@/lib/legalAgreements';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogTrigger } from '@/components/ui/dialog';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import {
  Boxes, ShoppingCart, ArrowRightLeft, Undo2, RefreshCw, AlertTriangle, ExternalLink,
  ArrowRight, Gift, Wallet, Info, Users, FileText, Inbox, ListChecks, Search,
  Package,
} from 'lucide-react';
import { lazyToast } from '@/lib/lazyToast';
import { PayExit } from '@/components/payment/PayExit';
import { useResumeOrder } from '@/components/payment/useResumeOrder';
import { toInventoryPayInfo } from '@/components/payment/resumeShape';
import { ChannelTierBadge } from '@/components/agent/ChannelTierBadge';
import { bonusRateLabel } from '@/lib/channelTierTerminology';
import { exchangeOpenid, isInWechatBrowser, payJsapi } from '@/lib/wechatJsapi';
import { safeRandomUUID } from '@/lib/safeRandomUUID';
import { payLinkTargetProps } from '@/lib/paymentEnv';
import { trackPayDialogShown, trackPayLinkClicked, usePayJumpTracking } from '@/lib/paymentTelemetry';

interface Balance {
  paid_inventory_points: number;
  bonus_inventory_points: number;
  frozen_inventory_points: number;
  total_purchased_points: number;
  total_allocated_points: number;
  alert_level: string;
}

interface InvTx {
  id: number;
  type: string;
  pool: string;
  points: number;
  balance_paid_after: number;
  balance_bonus_after: number;
  related_customer_user_id?: number | null;
  related_order_id?: string | null;
  description?: string | null;
  created_at: string;
}

type LegacyPurchaseOption = AgentInventoryPurchaseOption & { pricing_mode: 'legacy' };

interface QuotedPurchaseOption {
  pricing_mode: 'quoted';
  option_id: string;
  product_code: string;
  amount_cents: number;
  base_points: number;
  bonus_points: number;
  total_points: number;
  label: string;
  reward_description: string;
  tier_at_order?: string | null;
  tier_bonus_rate_bps?: number;
  crosses_tier_threshold?: boolean;
  projected_rolling_12m_yuan?: string | number | null;
}

type PurchaseOption = LegacyPurchaseOption | QuotedPurchaseOption;
type PayInfo = AgentInventoryPurchaseResponse;

interface CustomQuoteConfirmation {
  requestedAmountCents: number;
  priceQuoteId: string;
  orderIdempotencyKey: string;
  amountCents: number;
  basePoints: number;
  bonusPoints: number;
  totalPoints: number;
  tierAtOrder?: string | null;
  tierBonusRateBps?: number;
  crossesTierThreshold?: boolean;
}

interface FixedQuoteConfirmation {
  option: QuotedPurchaseOption;
  priceQuoteId: string;
  orderIdempotencyKey: string;
  amountCents: number;
  basePoints: number;
  bonusPoints: number;
  totalPoints: number;
  tierAtOrder?: string | null;
  tierBonusRateBps?: number;
  crossesTierThreshold?: boolean;
}

const AGENT_PENDING_JSAPI_KEY = 'omnirank_agent_inventory_pending_jsapi';
const QUOTE_REFRESH_CODES = new Set([
  'QUOTE_NOT_FOUND',
  'QUOTE_EXPIRED',
  'QUOTE_USED',
  'QUOTE_CONSUMED',
  'QUOTE_TYPE_MISMATCH',
  'QUOTE_MISMATCH',
  'QUOTE_INVALID',
  'PROCUREMENT_QUOTE_INVALID',
]);

function createIdempotencyKey(prefix: string): string {
  return `${prefix}-${safeRandomUUID()}`;
}

function pricingErrorToError(error: PricingError): Error {
  return Object.assign(new Error(error.message), {
    code: error.code,
    response: { status: error.status, data: { detail: { code: error.code, message: error.message } } },
  });
}

function isRefreshableQuoteError(error: unknown): boolean {
  const code = getV35W2ApiErrorCode(error);
  return Boolean(code && QUOTE_REFRESH_CODES.has(code));
}

function toQuotedPurchaseOption(item: ProcurementCatalogItem): QuotedPurchaseOption {
  return {
    pricing_mode: 'quoted',
    option_id: item.product_code,
    product_code: item.product_code,
    amount_cents: item.cash_price_cents,
    base_points: item.paid_inventory_points,
    bonus_points: item.bonus_inventory_points,
    total_points: item.total_inventory_points ?? item.paid_inventory_points + item.bonus_inventory_points,
    label: item.display_name,
    reward_description: item.reward_description || (item.bonus_inventory_points > 0
      ? `另赠送 ${item.bonus_inventory_points.toLocaleString()} 算力`
      : ''),
    tier_at_order: item.tier_at_order,
    tier_bonus_rate_bps: item.tier_bonus_rate_bps,
    crosses_tier_threshold: item.crosses_tier_threshold,
    projected_rolling_12m_yuan: item.projected_rolling_12m_yuan,
  };
}

const TIER_LABELS: Record<string, string> = {
  none: '普通服务商',
  certified: '认证服务商',
  preferred: '优选服务商',
  strategic: '战略服务商',
};

function tierLabel(tier?: string | null): string {
  return TIER_LABELS[String(tier || 'none')] || '当前等级';
}

function _savePendingJsapiOrder(info: PayInfo) {
  try {
    sessionStorage.setItem(AGENT_PENDING_JSAPI_KEY, JSON.stringify({ ...info, saved_at: Date.now() }));
  } catch { /* ignore */ }
}

function _loadPendingJsapiOrder(): PayInfo | null {
  try {
    const raw = sessionStorage.getItem(AGENT_PENDING_JSAPI_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PayInfo & { saved_at?: number };
    return parsed?.order_id ? parsed : null;
  } catch {
    return null;
  }
}

function _clearPendingJsapiOrder() {
  try { sessionStorage.removeItem(AGENT_PENDING_JSAPI_KEY); } catch { /* ignore */ }
}

// 预警徽章(沿用原 alert_level 映射 · 仅换配色)
function AlertBadge({ level }: { level?: string }) {
  if (level === 'empty') return <Badge variant="destructive" className="text-sm"><AlertTriangle className="w-3.5 h-3.5 mr-1" />0%</Badge>;
  if (level === 'warn_10') return <Badge variant="destructive" className="text-sm">{'<10%'}</Badge>;
  if (level === 'warn_30') return <Badge variant="outline" className="text-sm border-orange-500/40 text-orange-500">{'<30%'}</Badge>;
  return <Badge variant="outline" className="text-sm border-emerald-500/40 text-emerald-500">正常</Badge>;
}

// 进货档位卡(库存套餐感)
function PurchaseCard({ opt, onBuy, disabled = false }: { opt: PurchaseOption; onBuy: () => void; disabled?: boolean }) {
  const totalPoints = opt.pricing_mode === 'quoted'
    ? opt.total_points
    : (opt.total_points ?? opt.base_points + opt.bonus_points);
  const crossesTier = opt.pricing_mode === 'quoted' && opt.crosses_tier_threshold;
  const displayLabel = opt.label?.trim() !== opt.option_id ? opt.label?.trim() : '';
  return (
    <Card onClick={disabled ? undefined : onBuy} className={cn("flex h-full flex-col transition-colors", disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer hover:border-foreground/30")}>
      <CardContent className="flex flex-1 flex-col p-5">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <span className="text-2xl font-bold tabular-nums">{formatCents(opt.amount_cents)}</span>
          {crossesTier
            ? <Badge variant="outline" className="shrink-0 border-emerald-500/40 text-emerald-500">本单升级</Badge>
            : opt.reward_description && <Badge variant="outline" className="max-w-full whitespace-normal text-right leading-tight border-emerald-500/40 text-emerald-500">{opt.reward_description}</Badge>}
        </div>
        <div className="mt-4 space-y-1.5 text-sm">
          <div className="flex justify-between gap-2">
            <span className="text-muted-foreground">到账充值库存</span>
            <span className="font-medium tabular-nums">{opt.base_points.toLocaleString()}</span>
          </div>
          {opt.bonus_points > 0 && (
          <div className="flex justify-between gap-2">
            <span className="text-muted-foreground">赠送库存</span>
            <span className="font-medium tabular-nums text-emerald-500">+{opt.bonus_points.toLocaleString()}</span>
          </div>
          )}
          <div className="flex justify-between gap-2 border-t border-border pt-2">
            <span className="font-medium">总到账</span>
            <span className="font-semibold tabular-nums">{totalPoints.toLocaleString()}</span>
          </div>
        </div>
        {crossesTier && opt.pricing_mode === 'quoted' && (
          <p className="mt-3 text-xs font-medium text-emerald-500">
            本单完成后升级为{tierLabel(opt.tier_at_order)}，本单即按 {((opt.tier_bonus_rate_bps || 0) / 100).toFixed(0)}% 赠送
          </p>
        )}
        {displayLabel && <p className="mt-3 text-xs text-muted-foreground">{displayLabel}</p>}
        <div className="mt-auto pt-4">
          {/* `data-purchase-focus` = 「去进货」要聚焦的**首个可选档位**(工单 §P1-1)。
              打在按钮上而不是卡片上:键盘与触屏都要能直接落到可操作元素(§4.2)。 */}
          <Button size="sm" className="w-full" disabled={disabled} data-purchase-focus={disabled ? undefined : ''}>
            <ShoppingCart className="w-4 h-4 mr-1" /> 立即进货
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// 库存说明(进货 tab 右侧 · 短说明)
function StockNotes({ showBonus = true }: { showBonus?: boolean }) {
  const notes = [
    { icon: Wallet, t: '充值库存', d: '可用于工具和发布' },
    ...(showBonus ? [{ icon: Gift, t: '赠送库存', d: '可用于工具和受控客户返利' }] : []),
    { icon: ArrowRightLeft, t: '线下划拨', d: '从充值库存扣除' },
  ];
  return (
    <Card className="lg:sticky lg:top-6">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-sm"><Info className="h-4 w-4 text-emerald-500" />库存说明</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {notes.map((n) => (
          <div key={n.t} className="flex gap-2.5">
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-muted"><n.icon className="h-3.5 w-3.5 text-muted-foreground" /></div>
            <div>
              <div className="text-sm font-medium">{n.t}</div>
              <div className="text-xs text-muted-foreground">{n.d}</div>
            </div>
          </div>
        ))}
        {showBonus && (
          <p className="border-t border-border pt-3 text-xs leading-relaxed text-muted-foreground">
            赠送库存不可用于媒体发布、提现或抵扣进货款；客户返利仅通过已绑定客户的返利规则发放。
          </p>
        )}
      </CardContent>
    </Card>
  );
}

export default function InventoryCenter() {
  const { user, authorizationScope } = useAuth();
  const identityScope = user ? authorizationScope : null;
  // [工单 P1-4] 前台必须**同时**显示两个钱包。
  // 事故形态:服务商充 ¥50,钱进了 agent_inventory_wallets(库存),
  // 而这一页只画库存、顶部只画 user_wallets —— 两边都没说"你有两个口袋",
  // 用户看到的就是"付了钱账户看不到"。
  const { channelTier, paidPoints, bonusPoints, frozenPoints, refreshBalance } = useWallet();
  const { getProcurementCatalog, createProcurementQuote } = usePricingSSOT();
  const isPlatformWarehouseMode = Boolean(user?.is_admin);
  const [balance, setBalance] = useState<Balance | null>(null);
  const [options, setOptions] = useState<PurchaseOption[]>([]);
  const [catalogVersion, setCatalogVersion] = useState('');
  const [purchaseMode, setPurchaseMode] = useState<'quoted' | 'legacy' | null>(null);
  const [catalogTierProgress, setCatalogTierProgress] = useState<ProcurementTierProgress | null>(null);
  const [purchaseError, setPurchaseError] = useState<string | null>(null);
  /*
   * [WO_243 甲] 后端说"这个账号不需要进货"时给的那句话(`409` +
   * `code: PLATFORM_DIRECT_NO_PROCUREMENT` 的 `message`)。
   *
   * 🔴 **只此一份,不在前端再写一句。** 这一格原来是页面里硬编码的一段话,
   *    而口径住在后端 —— 两份话迟早分家,**分家时两边各自看都对**,
   *    只有屏幕上那句是过期的。
   * 🔴 `null` 不等于"可以进货":它只是"还没拿到平台口径"。
   *    两者压成一种,就会在后端还没出声时把页面渲染成正常进货态。
   */
  const [platformDirectNotice, setPlatformDirectNotice] = useState<string | null>(null);
  const [purchasingKey, setPurchasingKey] = useState<string | null>(null);
  const purchaseBusyRef = useRef(false);
  const reloadRequestRef = useRef(0);
  const reloadAbortRef = useRef<AbortController | null>(null);
  const [customAmountYuan, setCustomAmountYuan] = useState('');
  const [customQuoteConfirmation, setCustomQuoteConfirmation] = useState<CustomQuoteConfirmation | null>(null);
  const [fixedQuoteConfirmation, setFixedQuoteConfirmation] = useState<FixedQuoteConfirmation | null>(null);
  const [txItems, setTxItems] = useState<InvTx[]>([]);
  const [loading, setLoading] = useState(true);
  const [readError, setReadError] = useState<string | null>(null);
  const [allocOpen, setAllocOpen] = useState(false);
  const [revokeOpen, setRevokeOpen] = useState(false);
  // 🔴 [工单 §P1-1] Tabs 原本是 `defaultValue`(非受控),没法从"去进货"按钮切过去。
  //    死按钮改成可执行入口的前提就是这个 tab 能被代码切换。
  const [activeTab, setActiveTab] = useState('purchase');
  const purchaseSectionRef = useRef<HTMLDivElement | null>(null);

  /** 「去进货」:切到进货区 + 聚焦首个可选档位(工单 §P1-1)。
   *  聚焦而不是只滚动 —— 键盘用户滚过去还是不知道焦点在哪(§4.2 要求键盘可完成)。 */
  const goPurchase = () => {
    setActiveTab('purchase');
    // 🔴 tab 内容是懒挂载的。原来写 `setTimeout(…, 0)` —— 那是在和 React 提交抢跑:
    //    Playwright 在 1440/390 上碰巧赢了,在 3840x2160 上就输了(档位更多、提交更晚),
    //    表现为"焦点时灵时不灵"。对键盘用户而言这就是**真的进不去**,不是测试挑剔。
    //    改成按帧重试到目标出现为止,最多 20 帧(约 300ms)后放弃。
    let frames = 0;
    const tryFocus = () => {
      const root = purchaseSectionRef.current;
      const target =
        root?.querySelector<HTMLElement>('[data-purchase-focus]') ||
        (document.getElementById('custom-purchase-amount') as HTMLElement | null);
      if (target) {
        target.scrollIntoView({ block: 'center', behavior: 'smooth' });
        target.focus();
        return;
      }
      if (++frames < 20) window.requestAnimationFrame(tryFocus);
    };
    window.requestAnimationFrame(tryFocus);
  };
  const [payInfo, setPayInfo] = useState<PayInfo | null>(null);
  // [#169 §2.1.2] 🔴 带 ?resume_order=<id> 打开时恢复那一单的支付弹窗。
  //   没有这一段,「复制链接到微信里打开」就是死链 —— 她照做了,然后看到一个空页面。
  const { resumed: resumedOrder, error: resumeError } = useResumeOrder(toInventoryPayInfo);
  useEffect(() => { if (resumedOrder) setPayInfo(resumedOrder.info); }, [resumedOrder]);
  useEffect(() => { if (resumeError) lazyToast.error(resumeError); }, [resumeError]);
  const jsapiBusyRef = useRef(false);

  useLayoutEffect(() => {
    reloadAbortRef.current?.abort();
    reloadAbortRef.current = null;
    reloadRequestRef.current += 1;
    setBalance(null);
    setOptions([]);
    setTxItems([]);
    setPurchaseError(null);
    setReadError(null);
    setLoading(identityScope !== null);
  }, [identityScope]);

  const reload = useCallback(async () => {
    if (identityScope === null) return;
    const requestId = ++reloadRequestRef.current;
    reloadAbortRef.current?.abort();
    const controller = new AbortController();
    reloadAbortRef.current = controller;
    setLoading(true);
    setPurchaseError(null);
    setReadError(null);
    try {
      /*
       * [WO_243 甲] 🔴 **admin 也要真发这个请求。**
       *    原来这里 `isPlatformWarehouseMode ? Promise.resolve(null) : ...` ——
       *    admin 根本不发,于是后端那句 `PLATFORM_DIRECT_NO_PROCUREMENT`
       *    **物理上到不了前端**,「用后端 message」这件事从一开始就够不着。
       *    (存在 ≠ 可达:后端把话说了,没人去听。)
       * 🔴 档位与下单的隐藏**不靠这条短路**,而是下面 `procurementBlocked` 那一处 ——
       *    请求发不发,和该不该给他下单,是两件事;搅在一起就是现在这个样子。
       */
      const catalogRequest = getProcurementCatalog();
      const [balanceResult, transactionsResult, catalogSettled] = await Promise.allSettled([
        agentApi.inventoryBalance(controller.signal, true),
        agentApi.inventoryTransactions(50, 0, controller.signal, true),
        catalogRequest,
      ]);
      if (requestId !== reloadRequestRef.current) return;
      const failedResources: string[] = [];
      if (balanceResult.status === 'fulfilled') setBalance(balanceResult.value);
      else failedResources.push('库存余额');
      if (transactionsResult.status === 'fulfilled') setTxItems(transactionsResult.value.items || []);
      else failedResources.push('库存流水');
      if (catalogSettled.status === 'rejected') failedResources.push('进货价目表');
      if (failedResources.length > 0) {
        setReadError(`${failedResources.join('、')}读取失败 · 已保留上次成功数据`);
      }
      if (catalogSettled.status === 'rejected') return;
      const catalogResult = catalogSettled.value;

      /*
       * [WO_243 甲] 平台直营账号不进货 —— **这句话由后端出**。
       * 🔴 按 `code` 判,不按 `status` 判:409 这一个状态码底下还住着
       *    `NO_PUBLISHED_PROCUREMENT`(目录没发布),那是**运维故障**,
       *    不是"这个账号不需要进货"。按状态码判会把一次故障渲染成一句安抚话,
       *    而故障就此没人看见。
       */
      if (!catalogResult.ok && catalogResult.code === 'PLATFORM_DIRECT_NO_PROCUREMENT') {
        setPlatformDirectNotice(catalogResult.message);
        setPurchaseMode(null);
        setOptions([]);
        setCatalogVersion('');
        setCatalogTierProgress(null);
        setPurchaseError(null);
        return;
      }
      setPlatformDirectNotice(null);

      if (catalogResult.ok) {
        setPurchaseMode('quoted');
        setOptions((catalogResult.data.items || []).map(toQuotedPurchaseOption));
        setCatalogVersion(catalogResult.data.catalog_version);
        setCatalogTierProgress(catalogResult.data.tier_progress || null);
        return;
      }

      // 唯一允许的旧链回退:双价目表 flag 明确关闭。其他定价错误必须留在新链修复。
      if (catalogResult.status === 503 && catalogResult.code === 'SSOT_DISABLED') {
        const legacy = await agentApi.purchaseOptions();
        if (requestId !== reloadRequestRef.current) return;
        setPurchaseMode('legacy');
        setOptions((legacy.options || []).map((option) => ({ ...option, pricing_mode: 'legacy' })));
        setCatalogVersion(legacy.catalog_version);
        setCatalogTierProgress(null);
        return;
      }

      setPurchaseMode('quoted');
      setPurchaseError(catalogResult.message);
      lazyToast.error(catalogResult.message);
    } catch (e: any) {
      if (requestId !== reloadRequestRef.current) return;
      const message = formatApiErrorForDisplay(e, '加载失败 · 请重试', 'agent');
      setPurchaseError(message);
      lazyToast.error(message);
    } finally {
      if (requestId === reloadRequestRef.current) setLoading(false);
      if (reloadAbortRef.current === controller) reloadAbortRef.current = null;
    }
  }, [getProcurementCatalog, identityScope, isPlatformWarehouseMode]);

  useEffect(() => {
    void reload();
    return () => {
      reloadAbortRef.current?.abort();
      reloadAbortRef.current = null;
      reloadRequestRef.current += 1;
    };
  }, [reload]);

  const continueJsapiPayment = useCallback(async (info: PayInfo) => {
    if (!isInWechatBrowser()) return;
    if (jsapiBusyRef.current) return;
    jsapiBusyRef.current = true;
    _savePendingJsapiOrder(info);
    try {
      const sp = new URLSearchParams(window.location.search);
      const code = sp.get('code');
      if (code) {
        await exchangeOpenid(code);
        sp.delete('code');
        sp.delete('state');
        const nextUrl = `${window.location.pathname}${sp.toString() ? `?${sp.toString()}` : ''}${window.location.hash}`;
        window.history.replaceState(null, '', nextUrl);
      }
      const result = await payJsapi(info.order_id, window.location.href);
      if (result.ok) {
        _clearPendingJsapiOrder();
        lazyToast.success('微信支付已完成 · 正在确认库存入账');
      } else if (result.errMsg !== '跳转授权中') {
        lazyToast.warning(result.errMsg || '支付未完成 · 订单已保留,可重新调起');
      }
    } catch (e: any) {
      lazyToast.error(e?.message || '微信支付调起失败 · 订单已保留');
    } finally {
      jsapiBusyRef.current = false;
    }
  }, []);

  useEffect(() => {
    const pending = _loadPendingJsapiOrder();
    if (!pending || !isInWechatBrowser()) return;
    setPayInfo(pending);
    void continueJsapiPayment(pending);
  }, [continueJsapiPayment]);

  const createLegacyPurchase = async (
    amountCents: number,
    optionId: string | undefined,
    quoteFingerprint: string,
  ) => {
    if (!catalogVersion) {
      lazyToast.error('价目表尚未加载完成，请刷新后重试');
      return;
    }
    try {
      const r = await agentApi.createPurchase({
        amount_cents: amountCents,
        option_id: optionId,
        expected_catalog_version: catalogVersion,
        expected_quote_fingerprint: quoteFingerprint,
      });
      setPayInfo(r);
      lazyToast.success(`订单已创建 · 请完成支付`);
      if (r.needs_openid && isInWechatBrowser()) {
        void continueJsapiPayment(r);
      }
    } catch (e: any) {
      if (e?.response?.status === 409) {
        setPayInfo(null);
        await reload();
        lazyToast.error('进货价格配置已更新，请查看最新档位后重新确认');
        return;
      }
      lazyToast.error(formatApiErrorForDisplay(e, '进货失败 · 请重试', 'agent'));
    }
  };

  const createQuotedPurchase = async (opt: QuotedPurchaseOption) => {
    if (purchaseBusyRef.current) return;
    purchaseBusyRef.current = true;
    setPurchasingKey(opt.product_code);
    try {
      const quoteResult = await createProcurementQuote(
        opt.product_code,
        1,
        createIdempotencyKey('procurement-quote'),
      );
      if (!quoteResult.ok) throw pricingErrorToError(quoteResult);
      if (quoteResult.data.cash_price_cents !== opt.amount_cents) {
        throw new Error('报价应付金额与所选档位不一致');
      }
      setFixedQuoteConfirmation({
        option: opt,
        priceQuoteId: quoteResult.data.quote_id,
        orderIdempotencyKey: createIdempotencyKey('procurement-order'),
        amountCents: quoteResult.data.cash_price_cents,
        basePoints: quoteResult.data.paid_inventory_points,
        bonusPoints: quoteResult.data.bonus_inventory_points,
        totalPoints: quoteResult.data.total_inventory_points ?? quoteResult.data.paid_inventory_points + quoteResult.data.bonus_inventory_points,
        tierAtOrder: quoteResult.data.tier_at_order,
        tierBonusRateBps: quoteResult.data.tier_bonus_rate_bps,
        crossesTierThreshold: quoteResult.data.crosses_tier_threshold,
      });
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '进货失败 · 请重新报价', 'agent'));
    } finally {
      purchaseBusyRef.current = false;
      setPurchasingKey(null);
    }
  };

  const confirmFixedQuotedPurchase = async () => {
    const quoted = fixedQuoteConfirmation;
    if (!quoted || purchaseBusyRef.current) return;
    purchaseBusyRef.current = true;
    setPurchasingKey('fixed-quote-confirm');
    try {
      const order = await agentApi.createPurchase({
        price_quote_id: quoted.priceQuoteId,
        channel: 'auto',
        idempotency_key: quoted.orderIdempotencyKey,
      });
      setFixedQuoteConfirmation(null);
      setPayInfo(order);
      lazyToast.success('订单已创建 · 请完成支付');
      if (order.needs_openid && isInWechatBrowser()) void continueJsapiPayment(order);
    } catch (error) {
      if (isRefreshableQuoteError(error)) {
        const refreshed = await createProcurementQuote(
          quoted.option.product_code,
          1,
          createIdempotencyKey('procurement-quote-refresh'),
        );
        if (!refreshed.ok) {
          lazyToast.error(refreshed.message || '重新报价失败 · 请稍后重试');
          setFixedQuoteConfirmation(null);
          return;
        }
        if (refreshed.data.cash_price_cents !== quoted.option.amount_cents) {
          throw new Error('重新报价应付金额与所选档位不一致');
        }
        setFixedQuoteConfirmation({
          ...quoted,
          priceQuoteId: refreshed.data.quote_id,
          orderIdempotencyKey: createIdempotencyKey('procurement-order'),
          amountCents: refreshed.data.cash_price_cents,
          basePoints: refreshed.data.paid_inventory_points,
          bonusPoints: refreshed.data.bonus_inventory_points,
          totalPoints: refreshed.data.total_inventory_points ?? refreshed.data.paid_inventory_points + refreshed.data.bonus_inventory_points,
          tierAtOrder: refreshed.data.tier_at_order,
          tierBonusRateBps: refreshed.data.tier_bonus_rate_bps,
          crossesTierThreshold: refreshed.data.crosses_tier_threshold,
        });
        lazyToast.info('报价已更新，请重新确认本次应付和到账算力');
        return;
      }
      lazyToast.error(formatApiErrorForDisplay(error, '进货失败 · 请重新报价', 'agent'));
    } finally {
      purchaseBusyRef.current = false;
      setPurchasingKey(null);
    }
  };

  const handlePurchase = (opt: PurchaseOption) => {
    if (opt.pricing_mode === 'quoted') {
      void createQuotedPurchase(opt);
      return;
    }
    void createLegacyPurchase(opt.amount_cents, opt.option_id, opt.quote_fingerprint);
  };

  const handleCustomPurchase = async () => {
    const amountCents = yuanInputToCents(customAmountYuan);
    if (amountCents == null) return lazyToast.error(`请输入大于 0、不超过 ${MAX_AGENT_PURCHASE_AMOUNT_YUAN} 元、最多两位小数的进货金额`);
    if (purchaseBusyRef.current) return;
    purchaseBusyRef.current = true;
    setPurchasingKey('custom-amount');
    try {
      if (purchaseMode === 'quoted') {
        const preview = await agentApi.previewPurchase({
          amount_cents: amountCents,
          idempotency_key: createIdempotencyKey('procurement-custom-quote'),
        });
        if (!preview.price_quote_id) throw new Error('自由金额报价未返回持久化报价 ID');
        if (preview.amount_cents !== amountCents) {
          throw new Error('自由金额报价改变了本次应付金额');
        }
        setCustomQuoteConfirmation({
          requestedAmountCents: amountCents,
          priceQuoteId: preview.price_quote_id,
          orderIdempotencyKey: createIdempotencyKey('procurement-custom-order'),
          amountCents: preview.amount_cents,
          basePoints: preview.base_points,
          bonusPoints: preview.bonus_points,
          totalPoints: preview.total_points,
          tierAtOrder: preview.tier_at_order,
          tierBonusRateBps: preview.tier_bonus_rate_bps,
          crossesTierThreshold: preview.crosses_tier_threshold,
        });
        return;
      }

      const preview = await agentApi.previewPurchase({ amount_cents: amountCents });
      if (preview.catalog_version !== catalogVersion) {
        await reload();
        lazyToast.error('进货价格配置已更新，请查看最新规则后重新确认');
        return;
      }
      await createLegacyPurchase(amountCents, undefined, preview.quote_fingerprint);
    } catch (e: any) {
      if (e?.response?.status === 409) {
        await reload();
        lazyToast.error('进货价格配置已更新，请查看最新规则后重新确认');
        return;
      }
      lazyToast.error(formatApiErrorForDisplay(e, '自由金额预览失败 · 请重试', 'agent'));
    } finally {
      purchaseBusyRef.current = false;
      setPurchasingKey(null);
    }
  };

  const confirmCustomQuotedPurchase = async () => {
    const quoted = customQuoteConfirmation;
    if (!quoted || purchaseBusyRef.current) return;
    purchaseBusyRef.current = true;
    setPurchasingKey('custom-amount-confirm');
    try {
      const order = await agentApi.createPurchase({
        price_quote_id: quoted.priceQuoteId,
        channel: 'auto',
        idempotency_key: quoted.orderIdempotencyKey,
      });
      setCustomQuoteConfirmation(null);
      setPayInfo(order);
      lazyToast.success('自由金额订单已创建 · 请完成支付');
      if (order.needs_openid && isInWechatBrowser()) {
        void continueJsapiPayment(order);
      }
    } catch (error) {
      if (isRefreshableQuoteError(error)) {
        try {
          const refreshed = await agentApi.previewPurchase({
            amount_cents: quoted.requestedAmountCents,
            idempotency_key: createIdempotencyKey('procurement-custom-quote-refresh'),
          });
          if (!refreshed.price_quote_id) throw new Error('自由金额报价未返回持久化报价 ID');
          if (refreshed.amount_cents !== quoted.requestedAmountCents) {
            throw new Error('重新报价改变了本次应付金额');
          }
          setCustomQuoteConfirmation({
            requestedAmountCents: quoted.requestedAmountCents,
            priceQuoteId: refreshed.price_quote_id,
            orderIdempotencyKey: createIdempotencyKey('procurement-custom-order'),
            amountCents: refreshed.amount_cents,
            basePoints: refreshed.base_points,
            bonusPoints: refreshed.bonus_points,
            totalPoints: refreshed.total_points,
            tierAtOrder: refreshed.tier_at_order,
            tierBonusRateBps: refreshed.tier_bonus_rate_bps,
            crossesTierThreshold: refreshed.crosses_tier_threshold,
          });
          lazyToast.info('报价已更新，请重新确认本次应付和到账算力');
        } catch (refreshError) {
          setCustomQuoteConfirmation(null);
          lazyToast.error(formatApiErrorForDisplay(refreshError, '重新报价失败 · 请稍后重试', 'agent'));
        }
        return;
      }
      lazyToast.error(formatApiErrorForDisplay(error, '进货失败 · 请重新报价', 'agent'));
    } finally {
      purchaseBusyRef.current = false;
      setPurchasingKey(null);
    }
  };

  // 赠送库存相关 UI 只在有赠送余额或有赠送配置时展示(余额 0 且无赠送档位 → 全隐藏)
  // Reserve the bonus column during the authoritative read. Otherwise the
  // column is inserted after first paint and pushes the mobile summary down.
  const showBonus = loading
    || (balance?.bonus_inventory_points ?? 0) > 0
    || options.some((o) => o.bonus_points > 0);
  /*
   * [WO_243 甲] 不可进货态。**两条来源都算**:
   *   · 后端说的(`PLATFORM_DIRECT_NO_PROCUREMENT`)—— 权威;
   *   · 本地 `is_admin` —— 兜底。
   * 🔴 为什么留本地这条:后端那笔(WO_241 甲)还没上线时,这个端点对 admin
   *    **会正常返回一份真目录**(`api/pricing_ssot_api.py:181` 的
   *    `_require_service_provider` 对 is_admin 直接放行)。只认后端的话,
   *    前端先上线就会把档位和下单按钮摆到平台账号面前 —— 那是钱面上的回退。
   *    ⇒ 权威归后端,**兜底不撤**;两条都指向"不给下单"。
   */
  const procurementBlocked = isPlatformWarehouseMode || platformDirectNotice !== null;

  const tierProgress = isPlatformWarehouseMode
    ? null
    : purchaseMode === 'quoted' ? catalogTierProgress : channelTier;
  const founderStatus = purchaseMode === 'legacy' ? Boolean(channelTier?.is_founder) : false;
  const founderRank = purchaseMode === 'legacy' ? channelTier?.founder_rank ?? null : null;
  const handlePayClose = useCallback(() => {
    setPayInfo(null);
    void reload();
  }, [reload]);

  return (
    <div className="container mx-auto py-6 lg:py-8 space-y-6 max-w-7xl">
      {/* 标题区 */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-muted"><Boxes className="h-5 w-5" /></div>
          <div className="min-w-0">
            <h1 className="text-xl font-bold sm:text-2xl">算力库存</h1>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {isPlatformWarehouseMode ? '平台仓库 · 发行 / 划拨 / 流水追踪' : '进货入库 · 划拨客户 · 流水追踪'}
            </p>
            {!isPlatformWarehouseMode && (
              <div className="mt-2 min-h-[72px] lg:min-h-9">
              {tierProgress ? (
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span>当前服务商等级</span>
                <ChannelTierBadge
                  tier={tierProgress.effective_tier}
                  enabled={tierProgress.enabled}
                  founder={founderStatus}
                  founderRank={founderRank}
                  compact
                />
                <span>{tierProgress.enabled ? `本次进货可享 ${bonusRateLabel(tierProgress.effective_bonus_rate ?? tierProgress.bonus_rate)}` : '等级奖励暂未启用'}</span>
                {tierProgress.override_active && <span>已享专项保底权益，自然升级仍继续</span>}
                {tierProgress.next_tier && (
                  <span>
                    近 12 个月净实付 ¥{Number(tierProgress.rolling_12m_yuan || 0).toLocaleString()}，距{tierLabel(tierProgress.next_tier)}还差 ¥{Number(tierProgress.gap_to_next_yuan || 0).toLocaleString()}
                  </span>
                )}
              </div>
              ) : purchaseMode === 'quoted' ? (
              <p className="text-xs text-muted-foreground">
                当前账户已完成价格配置
              </p>
              ) : null}
              </div>
            )}
          </div>
        </div>
        <Button variant="ghost" size="sm" onClick={reload} disabled={loading} className="shrink-0">
          <RefreshCw className={cn('w-4 h-4 mr-1', loading && 'animate-spin')} /> 刷新
        </Button>
      </div>

      {readError && (
        <div role="alert" className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-sm text-amber-700 dark:text-amber-300">
          {readError}；未返回的余额或流水不会按 0 处理。
        </div>
      )}

      {/* [工单 P1-4] 两个钱包并排 · 明写各自的用途与去向 */}
      <Card>
        <CardContent className="p-5 sm:p-6">
          <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
            <div className="flex flex-col gap-1">
              <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <Wallet className="h-3.5 w-3.5" />可用算力 · 你自己用功能时扣这里
              </span>
              <span className="text-3xl font-bold tabular-nums text-foreground">
                {formatPoints(paidPoints + bonusPoints)}
                <span className="ml-1 text-sm font-normal text-muted-foreground">算力</span>
              </span>
              <p className="text-[11px] text-muted-foreground">
                充值 {formatPoints(paidPoints)} · 赠送 {formatPoints(bonusPoints)}
                {frozenPoints > 0 && <> · 冻结中 {formatPoints(frozenPoints)}</>}
              </p>
            </div>
            <div className="flex flex-col gap-1 sm:border-l sm:border-border sm:pl-6">
              <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <Package className="h-3.5 w-3.5" />库存算力 · 用来分给客户和下级
              </span>
              <span className="text-3xl font-bold tabular-nums text-emerald-500">
                {balance
                  ? formatPoints(balance.paid_inventory_points + balance.bonus_inventory_points)
                  : '—'}
                <span className="ml-1 text-sm font-normal text-muted-foreground">算力</span>
              </span>
              <p className="text-[11px] text-muted-foreground">
                进货买入的算力放在这里，<strong>不会</strong>直接用于你自己的功能消耗；
                需要自己用请先「转为可用算力」。
              </p>
            </div>
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-border pt-4">
            <SelfUseDialog
              balance={balance}
              balanceLoading={loading}
              balanceError={Boolean(readError) && !balance}
              onRetryBalance={() => {
                void reload();
                void refreshBalance();
              }}
              onGoPurchase={goPurchase}
              onDone={() => {
                void reload();
                void refreshBalance();
              }}
            />
            <Link to="/agent/channel-partners" className="text-xs text-muted-foreground underline underline-offset-2">
              发展下级服务商 →
            </Link>
          </div>
        </CardContent>
      </Card>

      {/* 库存总览大卡 */}
      <Card>
        <CardContent className="p-5 sm:p-6">
          <div
            className={`grid grid-cols-1 gap-5 ${showBonus ? 'sm:grid-cols-3' : 'sm:grid-cols-2'}`}
            style={{ minHeight: 128 }}
          >
            <div className="flex flex-col gap-1">
              <span className="flex items-center gap-1.5 text-xs text-muted-foreground"><Wallet className="h-3.5 w-3.5" />{isPlatformWarehouseMode ? '平台仓库可售库存' : '充值库存 · 工具和发布可用'}</span>
              <span className="text-3xl font-bold tabular-nums text-emerald-500">
                {balance ? formatPoints(balance.paid_inventory_points) : '—'}
                <span className="ml-1 text-sm font-normal text-muted-foreground">算力</span>
              </span>
            </div>
            {showBonus && (
            <div className="flex flex-col gap-1 sm:border-l sm:border-border sm:pl-6">
              <span className="flex items-center gap-1.5 text-xs text-muted-foreground"><Gift className="h-3.5 w-3.5" />赠送库存 · 工具与客户返利可用</span>
              <span className="text-3xl font-bold tabular-nums text-foreground">
                {balance ? formatPoints(balance.bonus_inventory_points) : '—'}
                <span className="ml-1 text-sm font-normal text-muted-foreground">算力</span>
              </span>
            </div>
            )}
            <div className="flex flex-col gap-1.5 sm:border-l sm:border-border sm:pl-6">
              <span className="text-xs text-muted-foreground">库存状态</span>
              <div>{balance ? <AlertBadge level={balance.alert_level} /> : <Badge variant="outline">无法读取</Badge>}</div>
              <p className="text-[11px] text-muted-foreground">
                {isPlatformWarehouseMode ? '累计发行' : '累计进货'} {balance ? formatPoints(balance.total_purchased_points) : '—'} · 累计划拨 {balance ? formatPoints(balance.total_allocated_points) : '—'}
              </p>
            </div>
          </div>
          {/* 流程线 */}
          <div className="mt-5 flex flex-wrap items-center gap-x-2 gap-y-1.5 border-t border-border pt-4 text-xs text-muted-foreground">
            {[
              { icon: ShoppingCart, t: isPlatformWarehouseMode ? '库存发行' : '进货入库' },
              { icon: ArrowRightLeft, t: '划拨给客户' },
              { icon: ListChecks, t: '流水可追踪' },
            ].map((s, i) => (
              <span key={s.t} className="flex items-center gap-2">
                {i > 0 && <ArrowRight className="h-3.5 w-3.5 opacity-40" />}
                <span className="flex items-center gap-1.5"><s.icon className="h-3.5 w-3.5" />{s.t}</span>
              </span>
            ))}
          </div>
        </CardContent>
      </Card>

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList className="grid w-full grid-cols-3 sm:inline-flex sm:w-auto">
          <TabsTrigger value="purchase">{isPlatformWarehouseMode ? '仓库管理' : '进货'}</TabsTrigger>
          <TabsTrigger value="offline">线下划拨</TabsTrigger>
          <TabsTrigger value="history">流水</TabsTrigger>
        </TabsList>

        {/* 进货 */}
        <TabsContent value="purchase" className="pt-4" ref={purchaseSectionRef}>
          {procurementBlocked ? (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <Boxes className="h-5 w-5 text-emerald-500" />平台仓库管理
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                {/*
                  * [WO_243 甲] 🔴 **这句话只从后端来**(409 的 `message`)。
                  *    这里原本是一段前端硬编码的说明 —— 口径住在后端,
                  *    前端再写一份,两份迟早分家,而**分家时两边各自看都对**,
                  *    只有屏幕上那句是过期的。所以那一份删掉了,不做"兜底文案"。
                  * 🔴 拿不到时**明说拿不到**,不拿旧话顶上:
                  *    「后端还没出声」和「后端说了不用进货」是两回事,
                  *    压成一句就永远不知道那条链是不是通的。
                  */}
                {platformDirectNotice ? (
                  <div data-testid="platform-direct-notice" className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-4 text-sm leading-relaxed">
                    {platformDirectNotice}
                  </div>
                ) : (
                  <div data-testid="platform-direct-notice-pending" className="flex flex-col gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-4 text-sm leading-relaxed sm:flex-row sm:items-center sm:justify-between">
                    <span>尚未取到平台口径说明 · 下单入口已按兜底规则关闭</span>
                    <Button variant="outline" size="sm" onClick={() => void reload()} disabled={loading}>
                      <RefreshCw className={cn('mr-1 h-4 w-4', loading && 'animate-spin')} />重新加载
                    </Button>
                  </div>
                )}
                {/* 🔴 管理员入口只给 admin:这张卡现在也可能因为**后端的码**而出现在
                    非 admin 身上,把 /admin/* 链接摆给他是另一种错。 */}
                {isPlatformWarehouseMode && (
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="rounded-lg border border-border p-4">
                    <p className="font-medium">管理进货与零售价目表</p>
                    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">配置服务商进货档位、奖励规则和普通客户算力包；不产生平台自购订单。</p>
                    <Button asChild variant="outline" className="mt-3">
                      <Link to="/admin/pricing-center">进入算力定价中心</Link>
                    </Button>
                  </div>
                  <div className="rounded-lg border border-border p-4">
                    <p className="font-medium">用户与应急调整</p>
                    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">查看账户与算力状态。资金修正必须使用有理由、有流水、有审计的管理员操作。</p>
                    <Button asChild variant="outline" className="mt-3">
                      <Link to="/admin/users">进入用户管理</Link>
                    </Button>
                  </div>
                </div>
                )}
                <p className="text-xs leading-relaxed text-muted-foreground">
                  平台库存发行只记仓库数量和发行流水，不计入平台经营收入；真实支付成功后才进入经营收入账本。
                </p>
              </CardContent>
            </Card>
          ) : (
          <>
          <div className="mb-4 text-right text-xs text-muted-foreground">
            <a href={AGENT_AGREEMENT_URL} className="text-primary underline underline-offset-2">《服务商协议》</a>
          </div>
          {purchaseError && (
            <Card className="mb-4 border-destructive/40">
              <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="text-sm font-medium">进货价目表暂不可用</p>
                  <p className="mt-1 text-xs text-muted-foreground">{purchaseError}</p>
                </div>
                <Button variant="outline" size="sm" onClick={() => void reload()} disabled={loading}>
                  <RefreshCw className={cn('mr-1 h-4 w-4', loading && 'animate-spin')} />重新加载
                </Button>
              </CardContent>
            </Card>
          )}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
            <div className="lg:col-span-3">
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                {options.map((opt) => (
                  <PurchaseCard
                    key={opt.option_id}
                    opt={opt}
                    disabled={!catalogVersion || loading || purchasingKey !== null}
                    onBuy={() => handlePurchase(opt)}
                  />
                ))}
                {options.length === 0 && (
                  <p className="col-span-full py-8 text-center text-sm text-muted-foreground">暂无可选进货档位</p>
                )}
              </div>
              <Card className="mt-4">
                <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-end">
                  <div className="flex-1 space-y-1.5">
                    <Label htmlFor="custom-purchase-amount">自由金额进货（元）</Label>
                    <Input id="custom-purchase-amount" inputMode="decimal" max={MAX_AGENT_PURCHASE_AMOUNT_YUAN} value={customAmountYuan} onChange={(event) => setCustomAmountYuan(event.target.value)} placeholder={`最高 ${MAX_AGENT_PURCHASE_AMOUNT_YUAN} 元`} />
                    {purchaseMode === 'quoted' && <p className="text-xs text-muted-foreground">输入金额就是本次应付金额，系统按当前采购规则换算到账算力。</p>}
                  </div>
                  <Button onClick={handleCustomPurchase} disabled={!catalogVersion || loading || purchasingKey !== null}><ShoppingCart className="mr-1 h-4 w-4" />获取并确认报价</Button>
                </CardContent>
              </Card>
            </div>
            <aside className="lg:col-span-1"><StockNotes showBonus={showBonus} /></aside>
          </div>
          </>
          )}
        </TabsContent>

        <AlertDialog
          open={fixedQuoteConfirmation !== null}
          onOpenChange={(open) => {
            if (!open && !purchaseBusyRef.current) setFixedQuoteConfirmation(null);
          }}
        >
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>确认本次进货报价</AlertDialogTitle>
              <AlertDialogDescription>
                报价已锁定。请核对本次应付和到账算力，再创建待支付订单。
              </AlertDialogDescription>
            </AlertDialogHeader>
            <div className="grid gap-3 rounded-lg border bg-muted/30 p-4 sm:grid-cols-4">
              <div>
                <p className="text-xs text-muted-foreground">本次应付</p>
                <p className="mt-1 text-lg font-semibold tabular-nums">
                  {formatCents(fixedQuoteConfirmation?.amountCents ?? 0)}
                </p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">充值库存</p>
                <p className="mt-1 text-lg font-semibold tabular-nums">
                  {formatPoints(fixedQuoteConfirmation?.basePoints ?? 0)}
                </p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">赠送库存</p>
                <p className="mt-1 text-lg font-semibold tabular-nums text-emerald-500">
                  +{formatPoints(fixedQuoteConfirmation?.bonusPoints ?? 0)}
                </p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">总到账</p>
                <p className="mt-1 text-lg font-semibold tabular-nums">
                  {formatPoints(fixedQuoteConfirmation?.totalPoints ?? 0)}
                </p>
              </div>
            </div>
            {fixedQuoteConfirmation?.crossesTierThreshold && (
              <p className="rounded-md border border-emerald-500/30 bg-emerald-500/10 p-3 text-xs text-emerald-700 dark:text-emerald-300">
                本单完成后升级为{tierLabel(fixedQuoteConfirmation.tierAtOrder)}，本单即按 {((fixedQuoteConfirmation.tierBonusRateBps || 0) / 100).toFixed(0)}% 赠送。
              </p>
            )}
            {(fixedQuoteConfirmation?.bonusPoints ?? 0) > 0 && (
              <p className="text-xs text-muted-foreground">赠送库存可用于工具和受控客户返利，不可用于媒体发布、提现或抵扣进货款。</p>
            )}
            <AlertDialogFooter>
              <AlertDialogCancel disabled={purchasingKey === 'fixed-quote-confirm'}>取消</AlertDialogCancel>
              <AlertDialogAction
                disabled={purchasingKey === 'fixed-quote-confirm'}
                onClick={(event) => {
                  event.preventDefault();
                  void confirmFixedQuotedPurchase();
                }}
              >
                {purchasingKey === 'fixed-quote-confirm' ? '正在创建订单…' : '确认并创建订单'}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

        <AlertDialog
          open={customQuoteConfirmation !== null}
          onOpenChange={(open) => {
            if (!open && !purchaseBusyRef.current) setCustomQuoteConfirmation(null);
          }}
        >
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>确认本次自由金额进货</AlertDialogTitle>
              <AlertDialogDescription>
                报价已锁定，请确认后再创建待支付订单。
              </AlertDialogDescription>
            </AlertDialogHeader>
            <div className="grid gap-3 rounded-lg border bg-muted/30 p-4 sm:grid-cols-4">
              <div>
                <p className="text-xs text-muted-foreground">本次应付</p>
                <p className="mt-1 text-lg font-semibold tabular-nums">
                  {formatCents(customQuoteConfirmation?.amountCents ?? 0)}
                </p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">充值库存</p>
                <p className="mt-1 text-lg font-semibold tabular-nums">
                  {formatPoints(customQuoteConfirmation?.basePoints ?? 0)}
                </p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">赠送库存</p>
                <p className="mt-1 text-lg font-semibold tabular-nums text-emerald-500">
                  +{formatPoints(customQuoteConfirmation?.bonusPoints ?? 0)}
                </p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">总到账</p>
                <p className="mt-1 text-lg font-semibold tabular-nums">
                  {formatPoints(customQuoteConfirmation?.totalPoints ?? 0)}
                </p>
              </div>
            </div>
            {customQuoteConfirmation?.crossesTierThreshold && (
              <p className="rounded-md border border-emerald-500/30 bg-emerald-500/10 p-3 text-xs text-emerald-700 dark:text-emerald-300">
                本单完成后升级为{tierLabel(customQuoteConfirmation.tierAtOrder)}，本单即按 {((customQuoteConfirmation.tierBonusRateBps || 0) / 100).toFixed(0)}% 赠送。
              </p>
            )}
            {(customQuoteConfirmation?.bonusPoints ?? 0) > 0 && (
              <p className="text-xs text-muted-foreground">赠送库存可用于工具和受控客户返利，不可用于媒体发布、提现或抵扣进货款。</p>
            )}
            <AlertDialogFooter>
              <AlertDialogCancel disabled={purchasingKey === 'custom-amount-confirm'}>
                取消
              </AlertDialogCancel>
              <AlertDialogAction
                disabled={purchasingKey === 'custom-amount-confirm'}
                onClick={(event) => {
                  event.preventDefault();
                  void confirmCustomQuotedPurchase();
                }}
              >
                {purchasingKey === 'custom-amount-confirm' ? '正在创建订单…' : '确认并创建订单'}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

        {/* 线下划拨 */}
        <TabsContent value="offline" className="pt-4">
          <Card>
            <CardContent className="p-5">
              <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
                <div className="space-y-3">
                  <h3 className="text-sm font-semibold">划拨操作</h3>
                  <div className="space-y-2.5">
                    {[
                      { icon: Users, t: '选择客户', d: '搜手机号/客户名' },
                      { icon: Boxes, t: '划拨算力', d: '工具 / 发布 / 赠送' },
                      { icon: FileText, t: '备注', d: '可选 · 记录用途' },
                    ].map((s) => (
                      <div key={s.t} className="flex items-center gap-2.5">
                        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-muted"><s.icon className="h-3.5 w-3.5 text-muted-foreground" /></div>
                        <div className="text-sm">
                          <span className="font-medium">{s.t}</span>
                          <span className="ml-1 text-xs text-muted-foreground">· {s.d}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
                <div className="flex flex-col justify-center gap-3 rounded-lg border border-border bg-muted/30 p-4">
                  <div className="flex items-center justify-center gap-4 text-sm">
                    <span className="flex flex-col items-center gap-1"><Boxes className="h-5 w-5 text-emerald-500" /><span>算力库存</span></span>
                    <ArrowRight className="h-4 w-4 text-muted-foreground" />
                    <span className="flex flex-col items-center gap-1"><Users className="h-5 w-5" /><span>客户算力</span></span>
                  </div>
                  <p className="text-center text-xs text-muted-foreground">线下收款后给客户划拨 · 从你的库存扣除</p>
                </div>
              </div>
              <div className="mt-5 flex flex-wrap gap-2 border-t border-border pt-4">
                <Dialog open={allocOpen} onOpenChange={setAllocOpen}>
                  <DialogTrigger asChild>
                    <Button><ArrowRightLeft className="w-4 h-4 mr-1" /> 划拨给客户</Button>
                  </DialogTrigger>
                  <AllocateDialog onDone={() => { setAllocOpen(false); reload(); }} />
                </Dialog>
                <Dialog open={revokeOpen} onOpenChange={setRevokeOpen}>
                  <DialogTrigger asChild>
                    <Button variant="outline"><Undo2 className="w-4 h-4 mr-1" /> 撤回算力</Button>
                  </DialogTrigger>
                  <RevokeDialog onDone={() => { setRevokeOpen(false); reload(); }} />
                </Dialog>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* 流水 */}
        <TabsContent value="history" className="pt-4">
          <Card>
            <CardContent className="p-0">
              {txItems.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-16 text-center">
                  <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-muted"><Inbox className="h-6 w-6 text-muted-foreground/60" /></div>
                  <p className="text-sm font-medium">{empty('no_inventory_records')}</p>
                  <p className="mt-1 text-xs text-muted-foreground">进货、划拨、撤回都会记录在这里</p>
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border text-xs text-muted-foreground">
                        <th className="p-3 text-left font-medium whitespace-nowrap">时间</th>
                        <th className="p-3 text-left font-medium whitespace-nowrap">类型</th>
                        <th className="p-3 text-left font-medium whitespace-nowrap">池</th>
                        <th className="p-3 text-right font-medium whitespace-nowrap">算力变化</th>
                        <th className="p-3 text-left font-medium whitespace-nowrap">客户</th>
                        <th className="p-3 text-left font-medium whitespace-nowrap">订单</th>
                      </tr>
                    </thead>
                    <tbody>
                      {txItems.map((tx) => (
                        <tr key={tx.id} className="border-b border-border last:border-0">
                          <td className="p-3 text-xs text-muted-foreground whitespace-nowrap">{new Date(tx.created_at).toLocaleString()}</td>
                          <td className="p-3"><Badge variant="outline">{inventoryTransactionTypeLabel(tx.type, 'agent')}</Badge></td>
                          <td className="p-3 text-muted-foreground">{poolLabel(tx.pool, 'agent')}</td>
                          <td className={cn('p-3 text-right font-mono tabular-nums whitespace-nowrap', tx.points > 0 ? 'text-emerald-500' : 'text-red-500')}>
                            {tx.points > 0 ? '+' : ''}{tx.points}
                          </td>
                          <td className="p-3 text-xs text-muted-foreground">{tx.related_customer_user_id ?? '-'}</td>
                          <td className="p-3 text-xs text-muted-foreground">{tx.related_order_id ?? '-'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      {/* 支付对话框 · 创建订单后弹出 */}
      <Dialog open={!!payInfo} onOpenChange={(o) => { if (!o) setPayInfo(null); }}>
        <PayDialog
          info={payInfo}
          degraded={resumedOrder?.degraded}
          onClose={handlePayClose}
          onStartJsapi={continueJsapiPayment}
        />
      </Dialog>
    </div>
  );
}

function PayDialog({
  info,
  onClose,
  onStartJsapi,
  degraded,
}: {
  info: PayInfo | null;
  onClose: () => void;
  onStartJsapi: (info: PayInfo) => void;
  /** [#169 §2.1.2] 由 resume_order 恢复、但端点还没回 payment_url_mobile ⇒ 明说,不静默 */
  degraded?: boolean;
}) {
  const [status, setStatus] = useState<'pending' | 'paid' | 'failed'>('pending');
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollAbortRef = useRef<AbortController | null>(null);
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!info) return;
    let cancelled = false;
    setStatus('pending');
    // 对齐 RechargePage 模式 · 每 3s 轮询 /api/wallet/order-status/{id}
    // 该 endpoint 会主动 query 微信兜底 · 即使 callback 失败也能救援
    const retryDelay = (response: Response) => {
      const raw = response.headers.get('Retry-After')?.trim();
      const seconds = raw ? Number(raw) : NaN;
      if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
      const retryAt = raw ? Date.parse(raw) : Number.NaN;
      return Number.isFinite(retryAt) ? Math.max(0, retryAt - Date.now()) : 30_000;
    };
    const schedule = (delay: number) => {
      if (cancelled || document.visibilityState === 'hidden') return;
      if (pollRef.current) clearTimeout(pollRef.current);
      pollRef.current = setTimeout(() => { void poll(); }, delay);
    };
    const poll = async () => {
      if (cancelled || document.visibilityState === 'hidden') return;
      const controller = new AbortController();
      pollAbortRef.current = controller;
      let nextDelay = 3000;
      try {
        const r = await authFetch(`/api/wallet/order-status/${info.order_id}`, { signal: controller.signal });
        if (r.status === 429) nextDelay = retryDelay(r);
        if (!r.ok) return;
        const d = await r.json();
        if (cancelled || controller.signal.aborted) return;
        if (d?.data?.status === 'paid') {
          cancelled = true;
          if (pollRef.current) { clearTimeout(pollRef.current); pollRef.current = null; }
          setStatus('paid');
          lazyToast.success(`支付成功!充值库存 ${info.base_points.toLocaleString()}${info.bonus_points > 0 ? ` + 赠送库存 ${info.bonus_points.toLocaleString()}` : ''} 已入账`);
          closeTimerRef.current = setTimeout(onClose, 1500);
          return;
        }
      } catch {/* 网络错误不中断轮询 */}
      finally {
        if (pollAbortRef.current === controller) pollAbortRef.current = null;
        if (!cancelled && !controller.signal.aborted) schedule(nextDelay);
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        pollAbortRef.current?.abort();
        if (pollRef.current) { clearTimeout(pollRef.current); pollRef.current = null; }
      } else {
        schedule(0);
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    schedule(0);
    return () => {
      cancelled = true;
      document.removeEventListener('visibilitychange', onVisibility);
      pollAbortRef.current?.abort();
      pollAbortRef.current = null;
      if (pollRef.current) { clearTimeout(pollRef.current); pollRef.current = null; }
      if (closeTimerRef.current) { clearTimeout(closeTimerRef.current); closeTimerRef.current = null; }
    };
  }, [info, onClose]);

  // [#169 §4.2] 🔴 原来这里拼的是第三方 `https://api.qrserver.com/...` ——
  //   它被墙/超时时 <img> 静默失败,用户看到一个空框,而这与后端给没给 code_url 无关。
  //   仓内本来就有 `qrcode` 依赖(M3 门户二维码组件 等三处在用),已改为 PayExit 里**本地渲染**。
  //   这里只保留「有没有二维码源」这个布尔,给埋点用。
  const qrImage = info?.code_url || info?.payment_url_qrcode;

  // [WO_MOBILE_PAY_JUMP 2026-08-07 §2.1] 三个埋点 · fire-and-forget,不阻塞支付主链
  const payCtx = {
    surface: 'agent_inventory' as const,
    order_id: info?.order_id ?? null,
    channel: info?.actual_channel ?? null,
    has_mobile_url: Boolean(info?.payment_url_mobile),
    has_qr: Boolean(qrImage),
  };
  const armJumpTracking = usePayJumpTracking(Boolean(info) && status === 'pending', payCtx);
  const shownRef = useRef<string | null>(null);
  useEffect(() => {
    if (!info || status !== 'pending') return;
    if (shownRef.current === info.order_id) return;   // 轮询 3s 一跳 · 每单只记一次
    shownRef.current = info.order_id;
    trackPayDialogShown({
      surface: 'agent_inventory',
      order_id: info.order_id,
      channel: info.actual_channel,
      has_mobile_url: Boolean(info.payment_url_mobile),
      has_qr: Boolean(qrImage),
    });
  }, [info, status, qrImage]);

  // 🔴 hooks 全部调用完才允许 early return(hooks 顺序不能因 info 为空而变)
  if (!info) return null;

  return (
    <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>
          {status === 'paid' ? '✓ 支付成功' : '完成支付 · 订单 ' + info.order_id}
        </DialogTitle>
      </DialogHeader>
      <div className="space-y-4 text-center">
        <div>
          <div className="text-3xl font-bold">{formatCents(info.amount_cents)}</div>
          <p className="text-sm text-muted-foreground mt-1">
            支付成功后入账 充值库存 {info.base_points.toLocaleString()}{info.bonus_points > 0 ? ` + 赠送库存 ${info.bonus_points.toLocaleString()}` : ''}
          </p>
          <Badge variant="outline" className="mt-2">支付方式:{paymentChannelLabel(info.actual_channel)}</Badge>
        </div>

        {status === 'paid' && (
          <div className="bg-green-50 border border-green-300 p-4 rounded text-sm">
            算力已入账 · 即将关闭并刷新库存
          </div>
        )}

        {/* [#169 §2.1.1] 🔴 四段出口(JSAPI / 二维码 / H5 / 无链接红字)全部收进 PayExit。
            在这之前同一份口径在三个支付面各写一版 —— #169 矩阵实跑出
            BuyCredit 48 格里 14 格零可点、本页 6 格,同一个后端响应两页结论相反。
            现在渲染什么由 payExitMatrix.payExits() 单点决定,本页只传状态。
            🔴 本单事故就发生在这一页:iPhone 非微信浏览器进货,H5 跳出去 96 秒回来仍 pending;
            手机外部浏览器那一格的主路已换成「在微信里打开」。 */}
        <PayExit
          info={info}
          status={status}
          payCtx={payCtx}
          armJumpTracking={armJumpTracking}
          degradedNoMobileUrl={degraded}
          onStartJsapi={(o) => onStartJsapi(o as typeof info)}
        />
      </div>
      <DialogFooter className="flex-col gap-2">
        <Button onClick={onClose} variant={status === 'paid' ? 'default' : 'outline'} className="w-full">
          {status === 'paid' ? '关闭' : '关闭'}
        </Button>
        {status === 'pending' && (
          <p className="text-xs text-muted-foreground text-center">
            订单已保存为待支付 · 重新支付入口稍后在订单中心提供
          </p>
        )}
      </DialogFooter>
    </DialogContent>
  );
}

function CustomerPicker({
  selected,
  onSelect,
}: {
  selected: AgentCustomerLookupItem | null;
  onSelect: (customer: AgentCustomerLookupItem | null) => void;
}) {
  const [query, setQuery] = useState('');
  const [items, setItems] = useState<AgentCustomerLookupItem[]>([]);
  const [searching, setSearching] = useState(false);
  // 🔴 [工单 §P0-2/§P1-3] 读取失败 ≠ 没有关系。两者必须是**不同**的空态:
  //    读取失败给"重新查询"(保留搜索词),查无结果才说没找到。
  //    把读取失败显示成"没有关系"会让操作者误以为人不存在 —— §4.1 第 7 条钉这条。
  const [loadError, setLoadError] = useState(false);

  const search = async () => {
    const keyword = query.trim();
    if (!keyword) return lazyToast.error('请输入手机号、客户名或客户ID');
    setSearching(true);
    setLoadError(false);
    try {
      const res = await agentApi.lookupCustomers(keyword, { ownedOnly: true, limit: 8 });
      const next = res.items || [];
      setItems(next);
      if (next.length === 0) {
        // 无关系与账号不存在**共用同一句** —— 手机号可枚举,任何差异都是探测位(§P0-2)。
        lazyToast.error('没有找到该账号');
      }
    } catch (e: any) {
      setLoadError(true);
      setItems([]);
      lazyToast.error(formatApiErrorForDisplay(e, '客户搜索失败', 'agent'));
    } finally {
      setSearching(false);
    }
  };

  // 关系态徽标 —— 只有下行三态,**没有**"我的上游"(工单 §0.1 R5)。
  const relationBadge = (c: AgentCustomerLookupItem) => {
    if (c.binding_status === 'downstream_partner') return '我的下线服务商';
    if (c.binding_status === 'both') return '下线服务商 · 也是我的客户';
    return '当前客户';
  };
  const isProvider = (c: AgentCustomerLookupItem) =>
    c.target_identity === 'service_provider'
    || c.binding_status === 'downstream_partner'
    || c.binding_status === 'both';

  // 🔴 [工单 v2 §R1] 目标是**服务商**时标题只用 TA 自己的名字(手机号兜底),
  //    绝不用 `brand_name` —— 服务商名下的品牌是 TA **自己客户**的名字。
  //    生产实证 2026-08-17:133 搜 163 时标题显示成了 163 最新客户
  //    「贵州省禾椒香食品有限公司」。后端已把这一位从响应拿掉(brand_name=NULL),
  //    这里是**第二道**:即便后端将来回退,标题也不会再拿品牌名当服务商名。
  const customerTitle = (c: AgentCustomerLookupItem) =>
    isProvider(c)
      ? (c.display_name || c.phone_masked || `服务商 ${c.customer_user_id}`)
      : (c.brand_name || c.display_name || `客户 ${c.customer_user_id}`);

  // 去重键用 `customer_user_id`,不是数组下标(下标在重新搜索后会错位)。
  const visibleItems = items.filter((c) => c.customer_user_id !== selected?.customer_user_id);

  return (
    <div className="space-y-2">
      <Label>客户</Label>
      <div className="flex gap-2">
        <Input
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            onSelect(null);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') search();
          }}
          placeholder="输入手机号 / 客户名 / 客户ID 搜索"
        />
        <Button type="button" variant="outline" onClick={search} disabled={searching}>
          <Search className={cn('w-4 h-4 mr-1', searching && 'animate-spin')} />
          搜索
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        可以搜到你的客户和你的下线服务商；新增或变更关系请联系平台管理员。
      </p>

      {/* 🔴 [§P1-3 出口字典] 读取失败:给重试,且**不清空搜索词**(§4.5) */}
      {loadError && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
          <div className="font-medium">暂时无法读取关系</div>
          <p className="mt-1 text-xs text-muted-foreground">刚才没能查到结果，这不代表对方不存在。</p>
          <Button type="button" size="sm" variant="outline" className="mt-2" onClick={search}>
            重新查询
          </Button>
        </div>
      )}

      {selected && (
        <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold" data-testid="target-title">{customerTitle(selected)}</span>
            <Badge variant={isProvider(selected) ? 'outline' : 'default'}>{relationBadge(selected)}</Badge>
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            ID {selected.customer_user_id}
            {selected.phone_masked ? ` · ${selected.phone_masked}` : ''}
          </div>
          {/* [P1-B/P1-C] 识别结果留在**卡片里**用中性样式呈现,不弹红色 error toast ——
              「这是你的下线服务商」是正常识别结果,不是错误。
              去重把结果行滤掉后,headline 只剩这一处,所以必须在这儿显示。 */}
          {selected.headline && (
            <div className="mt-1 text-xs font-medium text-foreground">{selected.headline}</div>
          )}
          {/* 🔴 [§P0-3 第 4 条] 提交前必须看到"这次会发生什么":进哪个钱包、按什么价 */}
          {selected.effect_note && (
            <p className="mt-2 rounded bg-background/60 p-2 text-xs leading-relaxed">
              {selected.effect_note}
            </p>
          )}
          {/* 服务商的算力在**库存**里,不是客户可用余额 —— 显示 0 是把未知伪装成 0(§0.3)。
              🔴 [工单 v2 §R3] 客户这两个数改读客户自己的钱包(充值算力 / 赠送算力),
                 不再是已停写的信用钱包旧数;单账本后也没有"发布算力"这一格了。 */}
          {!isProvider(selected) && (
            <div className="mt-2 text-xs text-muted-foreground" data-testid="customer-balance">
              对方当前算力: 充值 {formatPoints(selected.tool_credit_points)} · 赠送 {formatPoints(selected.bonus_credit_points)}
            </div>
          )}
          {/* 出口:后端给什么就渲染什么,前端不自己发明动作(§P0-3 第 3 条) */}
          {(selected.secondary_actions?.length || selected.primary_action) && (
            <div className="mt-2 flex flex-wrap gap-2">
              {[selected.primary_action, ...(selected.secondary_actions || [])]
                .filter((a): a is NonNullable<typeof a> => !!a && !!a.route)
                .map((a) => (
                  <Link
                    key={a.action}
                    to={a.route as string}
                    className="text-xs text-primary underline underline-offset-2"
                  >
                    {a.label} →
                  </Link>
                ))}
            </div>
          )}
        </div>
      )}
      {/* 🔴 [P0 热修 §5] 同一个人渲染两次的真因:选中后**绿色选中卡**与**灰色结果行**
          同时在画面上,不是后端返了两条(后端 `_lookup_agent_customers` 已按
          `customer_user_id` 去重,`seen` 集合)。选中卡就是这个人的表示,
          结果列表里再留一份纯属重复 —— 按 `customer_user_id` 过滤掉,不用数组下标。 */}
      {visibleItems.length > 0 && (
        <div className="max-h-44 overflow-auto rounded-lg border border-border divide-y divide-border">
          {visibleItems.map((c) => (
            <button
              key={c.customer_user_id}
              type="button"
              onClick={() => onSelect(c)}
              className={cn(
                'w-full text-left px-3 py-2 hover:bg-muted/60 transition',
                selected?.customer_user_id === c.customer_user_id && 'bg-primary/10'
              )}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium">{customerTitle(c)}</span>
                <Badge variant={isProvider(c) ? 'outline' : 'default'}>{relationBadge(c)}</Badge>
              </div>
              <div className="mt-1 text-xs text-muted-foreground">
                ID {c.customer_user_id}
                {c.phone_masked ? ` · ${c.phone_masked}` : ''}
                {/* 副标题只在标题**不是** display_name 时补一份,否则同一个名字渲染两遍。
                    (§R1 之后服务商的标题就是 display_name,老条件会重复。) */}
                {c.display_name && customerTitle(c) !== c.display_name ? ` · ${c.display_name}` : ''}
              </div>
              {c.headline && (
                <div className="mt-1 text-[11px] text-muted-foreground">{c.headline}</div>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * [工单 P0-3] 库存 → 可用算力(自用)· 1:1
 *
 * 资金口径由 Owner 2026-08-12 拍板取「① 自用 1:1」,已回写 docs/SYSTEM_TRUTH/08_billing.md。
 * 这里刻意把 1:1 写在界面上:比例是资金口径,不能只活在后端注释里。
 */
function SelfUseDialog({
  balance,
  onDone,
  onGoPurchase,
  balanceLoading,
  balanceError,
  onRetryBalance,
}: {
  balance: Balance | null;
  onDone: () => void;
  /** 库存为 0 时的真实出口:切到进货区并聚焦第一个可选档位(工单 §P1-1)。 */
  onGoPurchase: () => void;
  balanceLoading: boolean;
  balanceError: boolean;
  onRetryBalance: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [paid, setPaid] = useState('');
  const [bonus, setBonus] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);

  const availablePaid = balance?.paid_inventory_points ?? 0;
  const availableBonus = balance?.bonus_inventory_points ?? 0;
  const hasInventory = availablePaid + availableBonus > 0;

  const submit = async () => {
    const paidPointsInput = parseInt(paid || '0', 10) || 0;
    const bonusPointsInput = parseInt(bonus || '0', 10) || 0;
    if (paidPointsInput <= 0 && bonusPointsInput <= 0) {
      return lazyToast.error('请填写要转换的算力');
    }
    if (reason.trim().length < 2) return lazyToast.error('请填写用途说明(至少 2 个字)');
    setBusy(true);
    try {
      const res = await authFetch('/api/agent/inventory/self-use', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          // 资金动作:同一个请求编号后端只认一次(重复提交撞唯一键报错,不会扣两次)
          'X-Request-ID': createIdempotencyKey('selfuse'),
        },
        body: JSON.stringify({
          paid_points: paidPointsInput,
          bonus_points: bonusPointsInput,
          reason: reason.trim(),
        }),
      });
      if (!res.ok) throw await res.json().catch(() => new Error('转换失败'));
      lazyToast.success('已转为可用算力');
      setOpen(false);
      setPaid('');
      setBonus('');
      setReason('');
      onDone();
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '转换失败 · 请重试', 'agent'));
    } finally {
      setBusy(false);
    }
  };

  // 🔴 [工单 §P1-1] 库存 0 时原来是 `disabled={!hasInventory}` 的**灰色死按钮** ——
  //    工单 §0.3 明令「禁止出现灰色死按钮」「禁止只有解释、没有动作的提示」。
  //    四个状态各自给一个能立刻点的下一步,且尺寸固定、文案换行不引起布局跳动(§4.2)。
  if (balanceLoading) {
    return (
      <Button size="sm" variant="outline" disabled aria-live="polite" data-testid="selfuse-loading">
        <RefreshCw className="mr-1 h-3.5 w-3.5 animate-spin" />正在读取库存
      </Button>
    );
  }
  if (balanceError) {
    return (
      <Button size="sm" variant="outline" onClick={onRetryBalance} data-testid="selfuse-retry">
        <RefreshCw className="mr-1 h-3.5 w-3.5" />重新读取
      </Button>
    );
  }
  if (!hasInventory) {
    return (
      <Button size="sm" variant="outline" onClick={onGoPurchase} data-testid="selfuse-go-purchase">
        <ShoppingCart className="mr-1 h-3.5 w-3.5" />去进货
      </Button>
    );
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" variant="outline" data-testid="selfuse-convert">
          <ArrowRightLeft className="mr-1 h-3.5 w-3.5" />转为可用算力
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>库存算力 → 可用算力(1:1)</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-2">
            <div>
              <Label className="text-xs">充值库存(可用 {formatPoints(availablePaid)})</Label>
              <Input type="number" min={0} value={paid} onChange={(e) => setPaid(e.target.value)} />
            </div>
            <div>
              <Label className="text-xs">赠送库存(可用 {formatPoints(availableBonus)})</Label>
              <Input type="number" min={0} value={bonus} onChange={(e) => setBonus(e.target.value)} />
            </div>
          </div>
          <div>
            <Label className="text-xs">用途说明</Label>
            <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="例如:自己跑诊断测试" />
          </div>
          <p className="text-xs text-muted-foreground">
            转换按 <strong>1:1</strong> 计算，转过去的算力用于你自己的功能消耗，
            <strong>不能再分销给客户或下级</strong>。转换会留下流水，可在下方流水里查到。
          </p>
        </div>
        <DialogFooter>
          <Button onClick={submit} disabled={busy}>确认转换</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * 划拨(给客户)与供货(给下线服务商)共用的弹窗。
 *
 * 🔴 [工单 v2 §R2] 2026-08-17 由三桶(工具算力 / 发布算力 / 赠送算力)收敛成
 *    **两格:充值算力 / 赠送算力**。
 *    原因:单账本收敛(2026-07-27,基线 `e4adbf2f6`)之后,「工具」与「发布」
 *    落的是**同一个** `user_wallets.paid_points`,「只能用于发布投放」这句提示
 *    教的是一个**已经不存在的区别**。UI 上留着三格,就是让操作者按一个不存在的
 *    规则去分配金额。
 *
 * 🔴 请求模型**不动**(后端 `AllocateOfflineRequest` 仍有 `publish_points`):
 *    充值算力 → `tool_points` · `publish_points` **恒 0** · 赠送 → `bonus_points`。
 *    废字段留在 API 里保向后兼容,只是前端不再产生非 0 值。
 */
function AllocateDialog({ onDone }: { onDone: () => void }) {
  const [selectedCustomer, setSelectedCustomer] = useState<AgentCustomerLookupItem | null>(null);
  const [recharge, setRecharge] = useState('');
  const [bonus, setBonus] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  // 🔴 [P0 热修 §1] 目标是下线服务商时走**供货**(进对方库存算力),不是客户划拨。
  //    对方能不能被操作由后端有向关系解析决定,这里只按它给的关系态选落点。
  const isDownstream =
    selectedCustomer?.binding_status === 'downstream_partner' ||
    selectedCustomer?.binding_status === 'both';

  const rechargePoints = parseInt(recharge || '0') || 0;
  const bonusPoints = parseInt(bonus || '0') || 0;
  // [P1-A] Owner 原话:「为什么多了个算力?表示的是总数吗?」—— 加一行合计消掉这个疑问。
  const totalPoints = rechargePoints + bonusPoints;

  const submit = async () => {
    if (!selectedCustomer) return lazyToast.error('请先搜索并选择对方');
    if (totalPoints <= 0) return lazyToast.error('请填写要划拨的算力');
    setBusy(true);
    try {
      if (isDownstream) {
        // 库存也只有充值 / 赠送两格 —— 与本弹窗的两格一一对应。
        await agentApi.supplyDownstream({
          downstream_user_id: selectedCustomer.customer_user_id,
          paid_points: rechargePoints,
          bonus_points: bonusPoints,
          description: note || undefined,
        });
        lazyToast.success('供货成功 · 算力已进对方的库存算力');
      } else {
        await agentApi.allocateOffline({
          customer_user_id: selectedCustomer.customer_user_id,
          // 充值算力 → tool_points;`publish_points` 恒 0(单账本后无独立发布池,
          // 字段保留只为向后兼容 —— 见本组件顶部注释与后端 docstring)。
          tool_points: rechargePoints,
          publish_points: 0,
          bonus_points: bonusPoints,
          description: note || undefined,
        });
        lazyToast.success('划拨成功 · 已从算力库存扣除');
      }
      onDone();
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, isDownstream ? '供货失败 · 请重试' : '划拨失败 · 请重试', 'agent'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogContent>
      <DialogHeader>
        <DialogTitle>{isDownstream ? '线下供货 · 从算力库存扣除' : '线下划拨 · 从算力库存扣除'}</DialogTitle>
      </DialogHeader>
      <div className="space-y-3">
        <CustomerPicker selected={selectedCustomer} onSelect={setSelectedCustomer} />
        {/* 🔴 [工单 v2 §R2] 两格 —— 与真实落点一一对应,不多不少。
            提示语只说**这次操作的实际效果**(进对方哪个口袋 · 从你哪个库存扣),
            不再教一个已经不存在的"工具 vs 发布"区别。 */}
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2" data-testid="allocate-points-grid">
          {/* 🔴 `htmlFor`/`id` 必须成对:没有关联的 <Label> 对读屏和键盘用户等于不存在,
              点标签也不会聚焦到输入框。(Playwright 的 getByLabel 拿不到,正是这个缺陷的信号。) */}
          <div>
            <Label htmlFor="alloc-recharge" className="text-xs">充值算力</Label>
            <Input
              id="alloc-recharge"
              data-testid="alloc-recharge"
              type="number"
              min={0}
              value={recharge}
              onChange={(e) => setRecharge(e.target.value)}
            />
            <p className="mt-1 text-[11px] leading-tight text-muted-foreground">
              {isDownstream
                ? '进对方的库存算力 · 从你的充值库存扣'
                : '进对方的可用算力 · 从你的充值库存扣'}
            </p>
          </div>
          <div>
            <Label htmlFor="alloc-bonus" className="text-xs">赠送算力</Label>
            <Input
              id="alloc-bonus"
              data-testid="alloc-bonus"
              type="number"
              min={0}
              value={bonus}
              onChange={(e) => setBonus(e.target.value)}
            />
            <p className="mt-1 text-[11px] leading-tight text-muted-foreground">
              {isDownstream
                ? '进对方的赠送库存 · 从你的赠送库存扣'
                : '进对方的赠送算力 · 对方消耗时优先扣 · 从你的赠送库存扣'}
            </p>
          </div>
        </div>
        <div
          className="rounded-md bg-muted/40 px-3 py-2 text-sm tabular-nums"
          data-testid="allocate-total"
        >
          本次共{isDownstream ? '供货' : '划拨'} <strong>{formatPoints(totalPoints)}</strong> 算力
        </div>
        <div><Label>备注</Label><Input value={note} onChange={(e) => setNote(e.target.value)} /></div>
        {selectedCustomer?.effect_note && (
          <p className="text-xs leading-relaxed text-muted-foreground">{selectedCustomer.effect_note}</p>
        )}
      </div>
      <DialogFooter>
        <Button onClick={submit} disabled={busy} data-testid="allocate-submit">
          {isDownstream ? '确认供货' : '确认划拨'}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}

/**
 * 线下撤回弹窗。
 *
 * 🔴 [Review-CTO 增量授权 2026-08-17 · 残留 1] 与 `AllocateDialog` **同形收敛**:
 *    三格(算力 / 发布算力 / 赠送算力)→ 两格(充值算力 / 赠送算力)。
 *    后端 `revoke-offline` 早已把 `tool_points + publish_points` 合并成一个
 *    `paid_points` 从客户 `user_wallets` 回收(单账本收敛 2026-07-27 `e4adbf2f6`),
 *    「发布算力」这一格撤的和「算力」那一格是同一个口袋 —— 分成两格只会让
 *    操作者按一个不存在的规则去拆金额,而撤回是**资金反向流**,拆错更难发现。
 *
 * 🔴 请求模型不动:充值算力 → `tool_points` · `publish_points` **恒 0** ·
 *    赠送 → `bonus_points`。废字段留在 API 里保向后兼容。
 */
function RevokeDialog({ onDone }: { onDone: () => void }) {
  const [selectedCustomer, setSelectedCustomer] = useState<AgentCustomerLookupItem | null>(null);
  const [recharge, setRecharge] = useState('');
  const [bonus, setBonus] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!selectedCustomer) return lazyToast.error('请先搜索并选择客户');
    setBusy(true);
    try {
      await agentApi.revokeOffline({
        customer_user_id: selectedCustomer.customer_user_id,
        // 充值算力 → tool_points;`publish_points` 恒 0(后端会把两者相加成
        // 一个 paid_points 回收,见 agent_revoke_offline)。
        tool_points: parseInt(recharge || '0') || 0,
        publish_points: 0,
        bonus_points: parseInt(bonus || '0') || 0,
        reason: reason || undefined,
      });
      lazyToast.success('撤回成功 · 严格按未消费上限');
      onDone();
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '撤回失败 · 请重试', 'agent'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogContent>
      <DialogHeader>
        <DialogTitle>线下撤回 · 只撤回未使用算力</DialogTitle>
      </DialogHeader>
      <div className="space-y-3">
        <CustomerPicker selected={selectedCustomer} onSelect={setSelectedCustomer} />
        {/* 🔴 两格 —— 与划拨弹窗、与真实回收落点一一对应。
            `htmlFor`/`id` 必须成对:旧版这三个 <Label> 一个都没关联,
            对读屏和键盘用户等于不存在(Playwright 的 getByLabel 拿不到,
            正是这个缺陷的信号)。 */}
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2" data-testid="revoke-points-grid">
          <div>
            <Label htmlFor="revoke-recharge" className="text-xs">充值算力</Label>
            <Input
              id="revoke-recharge"
              data-testid="revoke-recharge"
              type="number"
              min={0}
              value={recharge}
              onChange={(e) => setRecharge(e.target.value)}
            />
            <p className="mt-1 text-[11px] leading-tight text-muted-foreground">
              从对方的充值算力回收 · 退回你的充值库存
            </p>
          </div>
          <div>
            <Label htmlFor="revoke-bonus" className="text-xs">赠送算力</Label>
            <Input
              id="revoke-bonus"
              data-testid="revoke-bonus"
              type="number"
              min={0}
              value={bonus}
              onChange={(e) => setBonus(e.target.value)}
            />
            <p className="mt-1 text-[11px] leading-tight text-muted-foreground">
              从对方的赠送算力回收 · 退回你的赠送库存
            </p>
          </div>
        </div>
        <div><Label htmlFor="revoke-reason">原因</Label><Input id="revoke-reason" value={reason} onChange={(e) => setReason(e.target.value)} /></div>
        <p className="text-xs text-muted-foreground">
          实际撤回会按客户当前剩余算力和本次可撤上限自动取小值 · 已使用部分不可撤回
        </p>
      </div>
      <DialogFooter>
        <Button variant="destructive" onClick={submit} disabled={busy} data-testid="revoke-submit">确认撤回</Button>
      </DialogFooter>
    </DialogContent>
  );
}
