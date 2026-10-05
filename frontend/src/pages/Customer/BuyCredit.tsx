/**
 * V3.5 W3 · 购买算力(r5 修正:真实调 /api/wallet/recharge 走 SKU 路径)
 *
 * 路由: /customer/recharge
 * 责任: 显示平台为当前账户配置的可购买 SKU
 * 客户身份铁律:不显示 ¥X = N 积分 / 出厂价 / 平台成本 / 费率
 *
 * 资金链 SSOT:
 * - dual 开:已发布 retail catalog → 持久化 price_quote → wallet/recharge 只传 price_quote_id
 * - dual 关(503 SSOT_DISABLED):显式回退原 SKU snapshot 链,保持现役行为
 * - 结算主体和金额均由后端解析,客户端不传内部归属或价格字段
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { customerApi, formatCents, getV35ApiErrorCode } from '@/lib/v35w3Api';
import { agentApi } from '@/lib/v35w2Api';
import { paymentChannelLabel } from '@/lib/v35Terminology';
import { authFetch, formatApiErrorForDisplay } from '@/lib/api';
import {
  usePricingSSOT,
  type PricingError,
  type RetailCatalogItem,
  type RetailQuote,
} from '@/hooks/usePricingSSOT';
import { exchangeOpenid, payJsapi } from '@/lib/wechatJsapi';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { ArrowRight, Boxes, Calculator, ExternalLink, Package2, PlusCircle, RefreshCw, Settings2, ShoppingBag, Store, Zap } from 'lucide-react';
import { lazyToast } from '@/lib/lazyToast';
import { PayExit } from '@/components/payment/PayExit';
import { useResumeOrder } from '@/components/payment/useResumeOrder';
import { toBuyCreditPayInfo } from '@/components/payment/resumeShape';
import { useWallet } from '@/context/WalletContext';
import { useAuth } from '@/context/AuthContext';
import { WalletStatusNotice } from '@/components/wallet/WalletStatusNotice';
import { Checkbox } from '@/components/ui/checkbox';
import { USER_TERMS_URL, USER_TERMS_VERSION } from '@/lib/legalAgreements';
import { useOnlinePurchaseGate } from '@/hooks/useOnlinePurchaseGate';
import {
  ONLINE_PURCHASE_BLOCKED_HINT,
  ONLINE_PURCHASE_BLOCKED_MESSAGE,
} from '@/components/wallet/onlinePurchaseGate';
import {
  buildCleanOauthReturnUrl,
  isWechatUserAgent,
  selectRechargeChannel,
  shouldStartJsapiPayment,
} from './buyCreditPayment';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { safeRandomUUID } from '@/lib/safeRandomUUID';
import { payLinkTargetProps } from '@/lib/paymentEnv';
import { trackPayDialogShown, trackPayLinkClicked, usePayJumpTracking } from '@/lib/paymentTelemetry';

interface LegacySKU {
  pricing_mode: 'legacy';
  retail_sku_id: string;
  retail_sku_version: number;
  sku_template_id?: number | null;
  override_id?: number | null;  // [2026-06-06 1:N 白标包] 白标包行带 override_id · 无 override 的规格行为 null
  sku_key: string;
  category: string;
  display_name: string;
  subtitle?: string | null;
  promo_text?: string | null;
  scene?: string | null;
  points_granted: number;
  retail_cents: number;
  source: 'configured_catalog';
  bonus_points?: number;
  usage_examples?: string[];
}

interface QuotedSKU {
  pricing_mode: 'quoted';
  product_code: string;
  category: 'credit_pack';
  display_name: string;
  subtitle?: string | null;
  promo_text?: string | null;
  scene?: string | null;
  points_granted: number;
  bonus_points: number;
  retail_cents: number;
  usage_examples: string[];
}

type SKU = LegacySKU | QuotedSKU;

interface ProviderRetailPreview {
  retail_sku_id: string;
  display_name: string;
  subtitle?: string | null;
  points_granted: number;
  retail_cents: number;
  is_active: boolean;
}

interface PayInfo {
  order_id: string;
  amount_yuan: number;
  base_points: number;
  bonus_points: number;
  total_points: number;
  tier_label: string;
  actual_channel: string;
  code_url?: string;
  payment_url_qrcode?: string;
  payment_url_mobile?: string;
  needs_openid?: boolean;
}

// [2026-06-06 卖算力库存口径] 主售卖 = 算力包 · 场景功能包(scenario_pack)已下架,不再作主销售分类
const CATEGORY_LABELS: Record<string, { label: string; icon: any }> = {
  credit_pack: { label: '算力包', icon: Package2 },
  addon_pack: { label: '按需加购', icon: PlusCircle },
};

const PENDING_JSAPI_PAY_KEY = 'omnirank_pending_jsapi_recharge';
const QUOTE_REFRESH_CODES = new Set([
  'QUOTE_NOT_FOUND',
  'QUOTE_EXPIRED',
  'QUOTE_USED',
  'QUOTE_CONSUMED',
  'QUOTE_TYPE_MISMATCH',
  'QUOTE_MISMATCH',
  'QUOTE_INVALID',
]);

function createIdempotencyKey(prefix: string): string {
  // safeRandomUUID 自带三层退化(randomUUID → getRandomValues → Math.random),
  // 这里不必再包一层 typeof 守卫。
  return `${prefix}-${safeRandomUUID()}`;
}

function pricingErrorToError(error: PricingError): Error {
  return Object.assign(new Error(error.message), {
    code: error.code,
    response: { status: error.status, data: { detail: { code: error.code, message: error.message } } },
  });
}

function isRefreshableQuoteError(error: unknown): boolean {
  const code = getV35ApiErrorCode(error);
  return Boolean(code && QUOTE_REFRESH_CODES.has(code));
}

function toQuotedSKU(item: RetailCatalogItem): QuotedSKU {
  return {
    pricing_mode: 'quoted',
    product_code: item.product_code,
    category: 'credit_pack',
    display_name: item.display_name,
    subtitle: item.subtitle,
    promo_text: item.sales_pitch,
    scene: item.scene,
    points_granted: item.points_granted,
    bonus_points: item.bonus_points,
    retail_cents: item.final_price_cents,
    usage_examples: item.usage_examples || [],
  };
}

// 微信内 UA 检测 · 微信支付通道由后端 auto 路由决定
function _isWeChatBrowser(): boolean {
  if (typeof navigator === 'undefined') return false;
  return isWechatUserAgent(navigator.userAgent || '');
}

export default function BuyCredit() {
  const [confirmDialog, askConfirm] = useConfirmDialog();
  const { getRetailCatalog, createRetailQuote, createRetailCustomAmountQuote } = usePricingSSOT();
  const { user, isLoading: authLoading, authorizationScope } = useAuth();
  const identityScope = user ? authorizationScope : null;
  const wallet = useWallet();
  // [客户线上购买门控 2026-07-29] 可见性完全由后端下发的 can_purchase_online 决定。
  // canPurchase=false 时:自由充值区**整块不渲染**(不是置灰、不是点击弹窗),
  // 支付发起区换成提示卡;算力包的"立即购买"改为点击弹提示。
  const { canPurchase, guard, gateDialog } = useOnlinePurchaseGate();
  const isServiceProvider = !authLoading && Number(user?.agent_level ?? 0) >= 1;
  const [items, setItems] = useState<SKU[]>([]);
  const [providerPackages, setProviderPackages] = useState<ProviderRetailPreview[]>([]);
  const [loading, setLoading] = useState(true);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [digitalGoodsNotice, setDigitalGoodsNotice] = useState<string | null>(null);
  // [2026-06-06 1:N 白标包] 多个白标包可共享同一 sku_template_id · 购买态用 override_id/规格 复合 key 区分
  const [buyingKey, setBuyingKey] = useState<string | null>(null);
  const buyingRef = useRef(false);
  const reloadRequestRef = useRef(0);

  useLayoutEffect(() => {
    reloadRequestRef.current += 1;
    setItems([]);
    setProviderPackages([]);
    setCatalogError(null);
    setDigitalGoodsNotice(null);
  }, [identityScope]);
  const [payInfo, setPayInfo] = useState<PayInfo | null>(null);
  // [#169 §2.1.2] 🔴 带 ?resume_order=<id> 打开时恢复那一单的支付弹窗。
  //   没有这一段,「复制链接到微信里打开」就是死链 —— 她照做了,然后看到一个空页面。
  const { resumed: resumedOrder, error: resumeError } = useResumeOrder(toBuyCreditPayInfo);
  useEffect(() => { if (resumedOrder) setPayInfo(resumedOrder.info); }, [resumedOrder]);
  useEffect(() => { if (resumeError) lazyToast.error(resumeError); }, [resumeError]);
  const [acceptedRechargeTerms, setAcceptedRechargeTerms] = useState(false);
  const [customAmount, setCustomAmount] = useState('');
  const [customQuote, setCustomQuote] = useState<RetailQuote | null>(null);
  const [customTermsAcceptanceId, setCustomTermsAcceptanceId] = useState<string | null>(null);
  const [customQuoteLoading, setCustomQuoteLoading] = useState(false);

  const rememberPendingJsapiPay = useCallback((info: PayInfo) => {
    try { sessionStorage.setItem(PENDING_JSAPI_PAY_KEY, JSON.stringify(info)); } catch {}
  }, []);

  const clearPendingJsapiPay = useCallback(() => {
    try { sessionStorage.removeItem(PENDING_JSAPI_PAY_KEY); } catch {}
  }, []);

  const startJsapiPayment = useCallback(async (info: PayInfo) => {
    if (!shouldStartJsapiPayment(info, _isWeChatBrowser())) return;
    rememberPendingJsapiPay(info);
    try {
      const redirectUri = typeof window === 'undefined'
        ? undefined
        : buildCleanOauthReturnUrl(window.location.href);
      const result = await payJsapi(info.order_id, redirectUri);
      if (result.errMsg === '跳转授权中') {
        lazyToast.info('正在获取微信授权 · 授权后会自动调起支付');
        return;
      }
      if (result.ok) {
        lazyToast.success('微信支付已完成 · 正在确认入账');
        return;
      }
      lazyToast.warning(result.errMsg || '微信支付未完成 · 可重新点击调起');
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '微信支付调起失败 · 请重试', 'customer'));
    }
  }, [rememberPendingJsapiPay]);

  const reload = useCallback(async () => {
    if (authLoading) return;
    const requestId = ++reloadRequestRef.current;
    setLoading(true);
    setCatalogError(null);
    try {
      if (isServiceProvider) {
        const response = await agentApi.pricingSKUs();
        if (requestId !== reloadRequestRef.current) return;
        const activePackages = (response.items || [])
          .filter((item: ProviderRetailPreview) => item.is_active)
          .map((item: ProviderRetailPreview) => ({
            retail_sku_id: item.retail_sku_id,
            display_name: item.display_name,
            subtitle: item.subtitle,
            points_granted: item.points_granted,
            retail_cents: item.retail_cents,
            is_active: true,
          }));
        setProviderPackages(activePackages);
        setItems([]);
        setDigitalGoodsNotice(null);
        return;
      }

      setProviderPackages([]);
      const catalogResult = await getRetailCatalog();
      if (requestId !== reloadRequestRef.current) return;
      if (catalogResult.ok) {
        setItems((catalogResult.data.items || []).map(toQuotedSKU));
        setDigitalGoodsNotice(catalogResult.data.digital_goods_notice || null);
        return;
      }

      if (catalogResult.status === 503 && catalogResult.code === 'SSOT_DISABLED') {
        const legacy = await customerApi.rechargeSKUs();
        if (requestId !== reloadRequestRef.current) return;
        const legacyItems: LegacySKU[] = (legacy.items || [])
          .filter((item) => item.category !== 'scenario_pack')
          .map((item) => ({ ...item, pricing_mode: 'legacy' }));
        setItems(legacyItems);
        setDigitalGoodsNotice(null);
        return;
      }

      setCatalogError(catalogResult.message);
      lazyToast.error(catalogResult.message);
    } catch (e: any) {
      if (requestId !== reloadRequestRef.current) return;
      const fallback = isServiceProvider ? '客户算力包预览暂时无法读取 · 请重试' : '加载失败 · 请重试';
      setCatalogError(formatApiErrorForDisplay(e, fallback, isServiceProvider ? 'agent' : 'customer'));
      lazyToast.error(formatApiErrorForDisplay(e, fallback, isServiceProvider ? 'agent' : 'customer'));
    } finally {
      if (requestId === reloadRequestRef.current) setLoading(false);
    }
  }, [authLoading, getRetailCatalog, identityScope, isServiceProvider]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!cancelled) await reload();
    })();
    return () => {
      cancelled = true;
      reloadRequestRef.current += 1;
    };
  }, [reload]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const sp = new URLSearchParams(window.location.search);
    const code = sp.get('code');
    if (!code) return;
    const rawPending = sessionStorage.getItem(PENDING_JSAPI_PAY_KEY);
    if (!rawPending) return;
    let cancelled = false;

    (async () => {
      try {
        await exchangeOpenid(code);
        if (cancelled) return;
        const cleanUrl = buildCleanOauthReturnUrl(window.location.href);
        window.history.replaceState({}, document.title, cleanUrl);

        const restored = JSON.parse(rawPending) as PayInfo;
        setPayInfo(restored);
        await startJsapiPayment(restored);
      } catch (e: any) {
        lazyToast.error(formatApiErrorForDisplay(e, '微信授权失败 · 请重新下单', 'customer'));
      }
    })();
    return () => { cancelled = true; };
  }, [startJsapiPayment]);

  const grouped = items.reduce<Record<string, SKU[]>>((acc, s) => {
    (acc[s.category] = acc[s.category] || []).push(s);
    return acc;
  }, {});

  // Canonical 零售 SKU 身份与可选模板彻底分离；模板 id 不再参与行身份。
  const skuKey = (sku: SKU): string => {
    if (sku.pricing_mode === 'quoted') return `p${sku.product_code}`;
    return `r${sku.retail_sku_id}`;
  };

  const createQuotedRecharge = async (sku: QuotedSKU, channel: string, termsAcceptanceId: string) => {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const quoteResult = await createRetailQuote(
        sku.product_code,
        1,
        createIdempotencyKey('retail-quote'),
        true,
        termsAcceptanceId,
      );
      if (!quoteResult.ok) throw pricingErrorToError(quoteResult);

      try {
        return await customerApi.createSKURecharge({
          price_quote_id: quoteResult.data.quote_id,
          payment_method: 'wechat',
          channel,
          idempotency_key: createIdempotencyKey('retail-order'),
          terms_acceptance_id: termsAcceptanceId,
        });
      } catch (error) {
        if (attempt === 0 && isRefreshableQuoteError(error)) {
          lazyToast.info('报价已失效 · 正在获取最新报价');
          continue;
        }
        throw error;
      }
    }
    throw new Error('无法获取有效报价 · 请重新选择算力包');
  };

  const handleBuy = async (sku: SKU) => {
    if (buyingRef.current) return;
    // 门控拦在最前:被禁时只弹提示,绝不走到下单链路。
    if (!guard()) return;
    if (!acceptedRechargeTerms) {
      lazyToast.error('请先阅读并勾选《用户服务协议》中的购买与退款规则');
      return;
    }
    if (sku.pricing_mode === 'quoted' && digitalGoodsNotice) {
      const acknowledged = await askConfirm({
        title: '购买前请确认',
        description: `${digitalGoodsNotice}\n\n点击“确定”表示你已阅读并同意，再生成本次报价。`,
        confirmLabel: '确定',
      });
      if (!acknowledged) return;
    }
    buyingRef.current = true;
    setBuyingKey(skuKey(sku));
    try {
      const accepted = await customerApi.acceptLegalAgreements({
        terms_accepted: true,
        terms_version: USER_TERMS_VERSION,
        surface: 'customer-recharge',
      });
      const termsAcceptanceId = accepted.acceptance.acceptance_id;
      // 后端 auto 按真实 UA 路由:微信内 JSAPI / PC Native / 手机外部虎皮椒
      const channel = selectRechargeChannel(typeof navigator === 'undefined' ? '' : navigator.userAgent);
      const r = sku.pricing_mode === 'quoted'
        ? await createQuotedRecharge(sku, channel, termsAcceptanceId)
        : await customerApi.createSKURecharge({
            ...(sku.sku_template_id != null ? { sku_template_id: sku.sku_template_id } : {}),
            ...(sku.override_id != null ? { override_id: sku.override_id } : {}),
            retail_sku_id: sku.retail_sku_id,
            retail_sku_version: sku.retail_sku_version,
            channel,
            terms_acceptance_id: termsAcceptanceId,
          });
      if (!r.success || !r.data) throw new Error('下单返回异常');
      setPayInfo(r.data);
      if (shouldStartJsapiPayment(r.data, _isWeChatBrowser())) {
        await startJsapiPayment(r.data);
      }
    } catch (e: any) {
      // [UI 审计 P3] 走 formatApiErrorForDisplay · 词典词条覆盖 V3.5 8 error code
      lazyToast.error(formatApiErrorForDisplay(e, '下单失败 · 请重试', 'customer'));
    } finally {
      buyingRef.current = false;
      setBuyingKey(null);
    }
  };

  const parseCustomAmountCents = (): number | null => {
    const normalized = customAmount.trim();
    if (!/^\d+(?:\.\d{1,2})?$/.test(normalized)) return null;
    const cents = Math.round(Number(normalized) * 100);
    return Number.isSafeInteger(cents) && cents >= 100 && cents <= 1_000_000 ? cents : null;
  };

  const handleCustomQuote = async () => {
    if (customQuoteLoading || buyingRef.current) return;
    const amountCents = parseCustomAmountCents();
    if (amountCents == null) {
      lazyToast.error('请输入 1 至 10000 元，最多保留两位小数');
      return;
    }
    if (!acceptedRechargeTerms) {
      lazyToast.error('请先阅读并勾选《用户服务协议》中的购买与退款规则');
      return;
    }
    if (digitalGoodsNotice && !(await askConfirm({
      title: '充值前请确认',
      description: `${digitalGoodsNotice}\n\n点击“确定”后计算本次到账算力。`,
      confirmLabel: '确定',
    }))) return;
    setCustomQuoteLoading(true);
    setCustomQuote(null);
    setCustomTermsAcceptanceId(null);
    try {
      const accepted = await customerApi.acceptLegalAgreements({
        terms_accepted: true,
        terms_version: USER_TERMS_VERSION,
        surface: 'customer-recharge',
      });
      const result = await createRetailCustomAmountQuote(
        amountCents,
        createIdempotencyKey('retail-cash-quote'),
        true,
        accepted.acceptance.acceptance_id,
      );
      if (!result.ok) throw pricingErrorToError(result);
      setCustomQuote(result.data);
      setCustomTermsAcceptanceId(accepted.acceptance.acceptance_id);
    } catch (error: any) {
      lazyToast.error(formatApiErrorForDisplay(error, '暂时无法计算到账算力 · 请重试', 'customer'));
    } finally {
      setCustomQuoteLoading(false);
    }
  };

  const handleCustomBuy = async () => {
    if (!customQuote || buyingRef.current) return;
    if (!customTermsAcceptanceId) {
      setCustomQuote(null);
      lazyToast.info('购买确认已失效 · 请重新计算到账算力');
      return;
    }
    if (new Date(customQuote.price_valid_until).getTime() <= Date.now()) {
      setCustomQuote(null);
      lazyToast.info('本次方案已过期 · 请重新计算到账算力');
      return;
    }
    buyingRef.current = true;
    setBuyingKey('custom-amount');
    try {
      const channel = selectRechargeChannel(typeof navigator === 'undefined' ? '' : navigator.userAgent);
      const result = await customerApi.createSKURecharge({
        price_quote_id: customQuote.quote_id,
        payment_method: 'wechat',
        channel,
        idempotency_key: createIdempotencyKey('retail-cash-order'),
        terms_acceptance_id: customTermsAcceptanceId,
      });
      if (!result.success || !result.data) throw new Error('下单返回异常');
      setPayInfo(result.data);
      if (shouldStartJsapiPayment(result.data, _isWeChatBrowser())) {
        await startJsapiPayment(result.data);
      }
    } catch (error: any) {
      if (isRefreshableQuoteError(error)) setCustomQuote(null);
      lazyToast.error(formatApiErrorForDisplay(error, '下单失败 · 请重新计算后重试', 'customer'));
    } finally {
      buyingRef.current = false;
      setBuyingKey(null);
    }
  };

  const handlePayClose = useCallback(() => {
    clearPendingJsapiPay();
    setPayInfo(null);
    void reload();
  }, [clearPendingJsapiPay, reload]);

  return (
    <div className="container mx-auto py-6 space-y-6 max-w-6xl">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <ShoppingBag className="w-6 h-6" />
          <h1 className="text-2xl font-bold">购买算力</h1>
        </div>
        <Button variant="ghost" size="sm" onClick={reload}><RefreshCw className="w-4 h-4 mr-1" />刷新</Button>
      </div>

      <WalletStatusNotice
        status={wallet.status}
        errorMessage={wallet.errorMessage}
        lastUpdatedAt={wallet.lastUpdatedAt}
        onRetry={wallet.refreshBalance}
      />

      {!authLoading && isServiceProvider && (
        <div className="space-y-6">
          <section className="border-y border-border bg-muted/20 px-4 py-5 sm:px-6">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
              <div className="flex items-start gap-3">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
                  <Boxes className="h-5 w-5" />
                </div>
                <div>
                  <h2 className="font-semibold">服务商进货请前往算力库存</h2>
                  <p className="mt-1 max-w-2xl text-sm leading-relaxed text-muted-foreground">
                    当前页面是普通客户购买算力的入口。你为经营备货时，请到“算力库存”选择进货档位或自由金额进货。
                  </p>
                </div>
              </div>
              <div className="flex flex-col gap-2 sm:flex-row">
                <Button asChild>
                  <Link to="/agent/inventory"><Boxes className="mr-2 h-4 w-4" />前往算力库存<ArrowRight className="ml-2 h-4 w-4" /></Link>
                </Button>
                <Button asChild variant="outline">
                  <Link to="/agent/pricing"><Settings2 className="mr-2 h-4 w-4" />管理客户售价</Link>
                </Button>
              </div>
            </div>
          </section>

          <section className="space-y-3">
            <div className="flex flex-wrap items-end justify-between gap-2">
              <div>
                <h2 className="flex items-center gap-2 text-lg font-semibold"><Store className="h-5 w-5" />你的客户当前看到的算力包</h2>
                <p className="mt-1 text-sm text-muted-foreground">这里只做客户视角预览，不会在此购买，也不会显示你的进货成本。</p>
              </div>
              {providerPackages.length > 0 && <Badge variant="secondary">已上架 {providerPackages.length} 个</Badge>}
            </div>

            {loading && providerPackages.length === 0 && (
              <div className="rounded-md border py-10 text-center text-sm text-muted-foreground">正在读取客户算力包…</div>
            )}
            {!loading && catalogError && (
              <div role="alert" className="rounded-md border border-destructive/40 px-4 py-4 text-center">
                <p className="text-sm text-muted-foreground">{catalogError}{providerPackages.length > 0 ? ' · 当前保留上次成功结果。' : ' · 这不代表算力包为空。'}</p>
                <Button variant="outline" size="sm" className="mt-3" onClick={() => void reload()}>重新读取</Button>
              </div>
            )}
            {providerPackages.length > 0 && (
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
                {providerPackages.map((pack) => (
                  <Card key={pack.retail_sku_id}>
                    <CardHeader className="pb-3">
                      <CardTitle className="text-base">{pack.display_name}</CardTitle>
                      {pack.subtitle && <p className="text-xs leading-relaxed text-muted-foreground">{pack.subtitle}</p>}
                    </CardHeader>
                    <CardContent className="space-y-2">
                      <div className="text-2xl font-bold">{formatCents(pack.retail_cents)}</div>
                      <p className="text-sm text-muted-foreground">客户获得 <strong>{pack.points_granted.toLocaleString()}</strong> 算力</p>
                      <Badge variant="outline">客户可见 · 已上架</Badge>
                    </CardContent>
                  </Card>
                ))}
              </div>
            )}
            {!loading && !catalogError && providerPackages.length === 0 && (
              <div className="rounded-md border border-dashed px-4 py-10 text-center">
                <p className="text-sm font-medium">你还没有上架对客算力包</p>
                <p className="mt-1 text-xs text-muted-foreground">先设置客户能购买的算力和售价，再回到这里预览。</p>
                <Button asChild variant="outline" size="sm" className="mt-4">
                  <Link to="/agent/pricing"><Settings2 className="mr-2 h-4 w-4" />去设置客户售价</Link>
                </Button>
              </div>
            )}
          </section>
        </div>
      )}

      {!isServiceProvider && loading && items.length === 0 && (
        <Card><CardContent className="py-10 text-center text-sm text-muted-foreground">正在加载算力包…</CardContent></Card>
      )}

      {!isServiceProvider && (!loading || items.length > 0) && (!catalogError || items.length > 0) && (
        <Card className="bg-muted/30">
          <CardContent className="py-3 text-sm flex items-center gap-2">
            <span>由 OmniRank 平台提供服务 · 当前账户已完成价格配置</span>
          </CardContent>
        </Card>
      )}

      {!isServiceProvider && !loading && catalogError && (
        <Card>
          <CardContent className="py-8 text-center space-y-3">
            <p className="text-sm text-muted-foreground">{catalogError}{items.length > 0 ? ' · 当前保留上次成功结果。' : ' · 这不代表算力包为空。'}</p>
            <Button variant="outline" size="sm" onClick={() => void reload()}>重试</Button>
          </CardContent>
        </Card>
      )}

      {!isServiceProvider && (!loading || items.length > 0) && (!catalogError || items.length > 0) && (
      <div className="space-y-4">
        <div className="rounded-lg border border-brand/30 bg-brand/5 px-3.5 py-2.5 text-xs leading-relaxed text-muted-foreground">
          你购买的是<span className="font-medium text-foreground">算力</span> · 诊断、写作、监测、报告都能用,用法不限。
        </div>
        <label className="flex items-start gap-2 rounded-lg border border-border px-3.5 py-3 text-sm leading-relaxed">
          <Checkbox
            checked={acceptedRechargeTerms}
            onCheckedChange={(checked) => setAcceptedRechargeTerms(checked === true)}
            aria-label="确认用户服务协议中的购买与退款规则"
          />
          <span>
            我已阅读并同意
            <a href={USER_TERMS_URL} target="_blank" rel="noopener noreferrer" className="mx-1 text-primary hover:underline">
              《用户服务协议》
            </a>
            ({USER_TERMS_VERSION})，包括数字商品交付、平台售后责任与退款规则。
          </span>
        </label>
        {digitalGoodsNotice && (
          <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3.5 py-3 text-sm leading-relaxed text-amber-900 dark:text-amber-100">
            <span className="font-semibold">数字商品退款提示：</span>{digitalGoodsNotice}
            <div className="mt-1 text-xs opacity-80">点击“立即购买”后仍需再次明确确认，未确认不会生成报价或订单。</div>
          </div>
        )}
        {/* [客户线上购买门控 2026-07-29] canPurchase=false → 自由金额输入区
            **整块不渲染**(DOM 里根本不存在,不是 hidden / disabled),
            支付发起区替换为提示卡。判定用后端下发的最终结果,前端不自行拼两级逻辑。 */}
        {!canPurchase ? (
          <section
            className="rounded-lg border border-border bg-muted/30 px-4 py-5"
            data-testid="online-purchase-blocked-notice"
          >
            <div className="flex items-start gap-3">
              <Calculator className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" />
              <div className="min-w-0">
                <p className="font-semibold">{ONLINE_PURCHASE_BLOCKED_MESSAGE}</p>
                <p className="mt-1 text-sm leading-relaxed text-muted-foreground">
                  {ONLINE_PURCHASE_BLOCKED_HINT}
                </p>
              </div>
            </div>
          </section>
        ) : (
        <section className="border-y border-border py-5">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 font-semibold">
                <Calculator className="h-5 w-5 text-primary" />自由充值
              </div>
              <p className="mt-1 text-sm text-muted-foreground">
                输入本次支付金额，系统按当前账户价格计算到账算力。
              </p>
              <div className="mt-3 flex max-w-md items-center gap-2">
                <span className="text-lg font-semibold">¥</span>
                <Input
                  inputMode="decimal"
                  value={customAmount}
                  onChange={(event) => {
                    setCustomAmount(event.target.value);
                    setCustomQuote(null);
                    setCustomTermsAcceptanceId(null);
                  }}
                  placeholder="1.00 - 10000.00"
                  aria-label="自由充值金额"
                />
                <Button
                  variant="outline"
                  onClick={() => void handleCustomQuote()}
                  disabled={customQuoteLoading || buyingKey !== null}
                >
                  {customQuoteLoading ? '计算中...' : '计算到账'}
                </Button>
              </div>
            </div>
            {customQuote && (
              <div className="flex min-w-0 flex-col gap-3 border-l-0 border-border pl-0 lg:min-w-[320px] lg:border-l lg:pl-6">
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Zap className="h-4 w-4 text-primary" />本次到账
                </div>
                <div className="flex items-baseline justify-between gap-4">
                  <strong className="text-2xl">{customQuote.points_granted.toLocaleString()} 算力</strong>
                  <span className="text-sm text-muted-foreground">实付 {formatCents(customQuote.final_price_cents)}</span>
                </div>
                <Button onClick={() => void handleCustomBuy()} disabled={buyingKey !== null}>
                  {buyingKey === 'custom-amount' ? '处理中...' : '按此金额充值'}
                </Button>
              </div>
            )}
          </div>
        </section>
        )}
      <Tabs defaultValue={Object.keys(CATEGORY_LABELS)[0]}>
        <TabsList>
          {Object.entries(CATEGORY_LABELS).map(([k, v]) => (
            <TabsTrigger key={k} value={k}>{v.label}({grouped[k]?.length || 0})</TabsTrigger>
          ))}
        </TabsList>
        {Object.keys(CATEGORY_LABELS).map((cat) => (
          <TabsContent key={cat} value={cat} className="pt-4">
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {(grouped[cat] || []).map((sku) => (
                <Card key={skuKey(sku)} className="hover:border-primary transition">
                  <CardHeader>
                    <CardTitle className="text-base">{sku.display_name}</CardTitle>
                    {sku.subtitle && (
                      <p className="text-xs text-muted-foreground mt-1">{sku.subtitle}</p>
                    )}
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <div className="text-3xl font-bold">{formatCents(sku.retail_cents)}</div>
                    <div className="text-sm text-muted-foreground">
                      获得 <strong>{sku.points_granted.toLocaleString()}</strong> 算力
                    </div>
                    {(sku.bonus_points ?? 0) > 0 && (
                      <div className="text-sm text-amber-600">另赠送 <strong>{sku.bonus_points?.toLocaleString()}</strong> 算力</div>
                    )}
                    {sku.scene && (
                      <p className="text-xs text-muted-foreground">{sku.scene}</p>
                    )}
                    {sku.promo_text && (
                      <p className="text-xs text-primary">{sku.promo_text}</p>
                    )}
                    {(sku.usage_examples || []).map((example) => (
                      <p key={example} className="text-xs text-muted-foreground">{example}</p>
                    ))}
                    <Button
                      className="w-full"
                      onClick={() => handleBuy(sku)}
                      disabled={buyingKey !== null}
                      title={wallet.status !== 'ready' ? '余额读取失败不影响购买；请按套餐金额核对订单' : undefined}
                    >
                      {buyingKey === skuKey(sku) ? '处理中...' : '立即购买'}
                    </Button>
                  </CardContent>
                </Card>
              ))}
              {!grouped[cat]?.length && (
                <p className="text-muted-foreground text-sm col-span-full text-center py-8">
                  暂无可购买算力包
                </p>
              )}
            </div>
          </TabsContent>
        ))}
      </Tabs>
      </div>
      )}

      {/* 支付对话框 · 同 InventoryCenter PayDialog 模式 · 3s 轮询 order-status */}
      <Dialog open={!!payInfo} onOpenChange={(open) => { if (!open) handlePayClose(); }}>
        <PayDialog
          info={payInfo}
          degraded={resumedOrder?.degraded}
          onClose={handlePayClose}
          onStartJsapiPay={startJsapiPayment}
        />
      </Dialog>

      {/* 被禁客户点算力包"立即购买"时的提示弹窗 */}
      {gateDialog}
      {confirmDialog}
    </div>
  );
}

function PayDialog({
  info,
  onClose,
  onStartJsapiPay,
  degraded,
}: {
  info: PayInfo | null;
  onClose: () => void;
  onStartJsapiPay: (info: PayInfo) => Promise<void>;
  /** [#169 §2.1.2] 由 resume_order 恢复、但端点还没回 payment_url_mobile ⇒ 明说,不静默 */
  degraded?: boolean;
}) {
  const [status, setStatus] = useState<'pending' | 'paid'>('pending');
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollAbortRef = useRef<AbortController | null>(null);
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!info) return;
    let cancelled = false;
    setStatus('pending');
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
          lazyToast.success(`支付成功!${info.total_points.toLocaleString()} 算力已入账`);
          closeTimerRef.current = setTimeout(onClose, 1500);
          return;
        }
      } catch { /* 网络错误按完成后间隔继续，不重叠 */ }
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

  // [#169 §4.2] 🔴 原来这里拼第三方 `https://api.qrserver.com/...`,被墙/超时时
  //   <img> 静默失败、用户看到空框,而这与后端给没给 code_url 无关。
  //   已改为 PayExit 里用仓内 `qrcode` 依赖**本地渲染**;这里只留「有没有二维码源」给埋点。
  const qrImage = info?.code_url || info?.payment_url_qrcode;

  // [WO_MOBILE_PAY_JUMP 2026-08-07 §2.1] 三个埋点 · fire-and-forget,不阻塞支付主链
  // 🔴 声明必须排在下面那个"1 秒自动跳"effect **之前** —— 那个 effect 要用 armJumpTracking。
  const payCtx = {
    surface: 'customer_recharge' as const,
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
      surface: 'customer_recharge',
      order_id: info.order_id,
      channel: info.actual_channel,
      has_mobile_url: Boolean(info.payment_url_mobile),
      has_qr: Boolean(qrImage),
    });
  }, [info, status, qrImage]);

  // [2026-06-07 P4] 虎皮椒移动端闭环:微信内浏览器 + actual_channel='xunhupay' + payment_url_mobile
  //   → 1 秒后自动跳虎皮椒 H5 收银台(用户先看到金额+按钮再跳 · 不强吓人)
  //   · 防循环跳:sessionStorage 标记 order_id 已跳一次 · 用户跳回来不会再被踢走
  //   · 非微信内浏览器走原 outline 按钮(用户主动点)
  //   · 老板要求"应自动跳转或提供明确可用按钮" → 双保险:1 秒自动跳 + 主按钮兜底(下方 JSX)
  useEffect(() => {
    if (!info) return;
    if (status !== 'pending') return;
    if (info.actual_channel !== 'xunhupay') return;
    if (!info.payment_url_mobile) return;
    if (!_isWeChatBrowser()) return;
    const jumpedKey = `xunhupay_jumped_${info.order_id}`;
    if (sessionStorage.getItem(jumpedKey)) return;
    sessionStorage.setItem(jumpedKey, '1');
    const t = setTimeout(() => {
      // [WO_MOBILE_PAY_JUMP 2026-08-07 §2.1] 自动跳也要留痕 · 否则这条路的漏斗断在 shown
      armJumpTracking();
      trackPayLinkClicked(
        {
          surface: 'customer_recharge',
          order_id: info.order_id,
          channel: info.actual_channel,
          has_mobile_url: true,
          has_qr: false,
        },
        { new_tab: false, link_kind: 'wechat_auto_redirect' },
      );
      // 微信内 location.href(微信内 window.open 多被拦)
      window.location.href = info.payment_url_mobile!;
    }, 1000);
    return () => clearTimeout(t);
  }, [info, status]);

  // 🔴 hooks 全部调用完才允许 early return(hooks 顺序不能因 info 为空而变)
  if (!info) return null;

  return (
    <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>{status === 'paid' ? '✓ 支付成功' : '完成支付'}</DialogTitle>
        <DialogDescription>
          核对金额和到账算力后再继续。关闭弹窗不会自动确认支付，支付结果以订单记录为准。
        </DialogDescription>
      </DialogHeader>
      <div className="space-y-4 text-center">
        <div>
          <div className="text-3xl font-bold">¥{info.amount_yuan.toFixed(2)}</div>
          <p className="text-sm text-muted-foreground mt-1">
            支付成功后获得 {info.total_points.toLocaleString()} 算力
          </p>
          <Badge variant="outline" className="mt-2">支付方式:{paymentChannelLabel(info.actual_channel)}</Badge>
        </div>
        {status === 'paid' && (
          <div className="bg-green-50 border border-green-300 p-4 rounded text-sm">
            算力已入账 · 即将关闭并刷新
          </div>
        )}
        {/* [#169 §2.1.1] 🔴 全部支付出口收进 PayExit,口径由 payExitMatrix 单点决定。
            本页原来有五段各自的条件(两条提示 + 二维码 + JSAPI 按钮 + 两种 H5),
            #169 矩阵实跑出 **48 格里 14 格零可点、其中 10 格连一句解释都没有** ——
            成因是 L963 微信内主动排除二维码、而自动跳与主按钮又都要求 payment_url_mobile,
            那个字段一空三条出口同时消失。
            🔴 上面那条「微信内 xunhupay 1 秒自动跳」的 effect **逐字保留**(08-07 锁钉着),
               它在 return 之前,不在本次替换范围内。 */}
        <PayExit
          info={info}
          status={status}
          payCtx={payCtx}
          armJumpTracking={armJumpTracking}
          degradedNoMobileUrl={degraded}
          onStartJsapi={(o) => void onStartJsapiPay(o as typeof info)}
        />
      </div>
      <DialogFooter>
        <Button onClick={onClose} variant={status === 'paid' ? 'default' : 'outline'} className="w-full">
          {status === 'paid' ? '关闭' : '关闭(订单保留)'}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}
