/**
 * Social Studio 订阅套餐页 V3.1
 *
 * 来源: docs/AI-CONTEXT/SOCIAL_STUDIO_PRICING_V3_1_EXECUTION_2026-05-10.md
 * 计划: .planning/phases/07-social-studio-subscription/PLAN.md F01
 *
 * 2026-05-13 重写 (老板自检 bug 反馈):
 *   1. 设计语言改 ss-* (原来用 shadcn token 看起来像 GEO 板块)
 *   2. 加 selectedPlanId 选中状态 + 点击 Card 切换 (原来高亮永远固定在 is_recommended=¥99)
 *   3. toast detail 防御 dict / array (FastAPI 422 / 后端 dict detail 会触发 React error #31)
 *   4. CTA 价格跟 billingCycle 动态切换 (原来年付 tab 但 CTA 写死月价)
 *   5. 显示当前订阅 ("当前 ✓") + 升级/降级文案
 *   6. partner 隐藏数字单价 (¥599/月起 容易和"联系顾问"语义打架)
 *
 * 2026-05-13 复用 GEO oauth (老板"GEO 已对接, 充值 vs 订阅信息不污染"):
 *   - 微信内首次购买:后端检测无 openid → 返 oauth_url(GEO build_oauth_authorize_url)
 *   - 前端跳 oauth_url → 微信跳回 /pricing-plans?code=...&state=v31_sub:plan:cycle:flag
 *   - 前端调 GEO POST /api/wallet/wechat-jsapi/exchange-openid 用 code 换 openid
 *   - 前端重 checkout 带 openid · openid 不入库 · 即用即扔
 *   - 删了 V3.1 专属 /api/oauth/wechat/* router + user_wechat_oauth 表 + migration_010
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Check, Sparkles, Crown, Building2, ArrowRight, Loader2, X } from 'lucide-react';
import { toast } from 'sonner';
import { authFetch } from '@/lib/api';
import { cn } from '@/lib/utils';
import { useSocialStudioTheme } from '@/lib/socialStudioTheme';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { invokeWxPay } from '@/lib/wechatJsapi';
import { USER_TERMS_URL, USER_TERMS_VERSION } from '@/lib/legalAgreements';

const PENDING_SUBSCRIPTION_TERMS_KEY = 'omnirank_pending_subscription_terms_acceptance';

// ========== 套餐结构(对齐 V3.1 api/subscription_api.py) ==========

interface SubscriptionPlanQuotas {
  light_chat: number;
  pro_write: number;
  super_write: number;
  web_search: number;
  video_minutes: number;
  video_single_cap: number;
  rewrite: number;
  video_breakdown: number;
  author_breakdown: number;
  review: number;
  monthly_plan: number;
  team_profile: number;
  workspace: number;
  knowledge_mb: number;
}

interface SubscriptionPlan {
  plan_id: string;
  display_name: string;
  monthly_yuan: number;
  yearly_yuan: number | null;
  first_month_yuan: number | null;
  is_recommended: boolean;
  quotas: SubscriptionPlanQuotas;
}

interface CurrentSubscription {
  plan_id: string | null;
  active: boolean;
}

// ========== 文案配置(用户任务视角,5 行,删"不限") ==========

const PLAN_COPY: Record<string, {
  tagline: string;
  pitch: string;
  suitable: string;
  icon: typeof Sparkles;
  bullets: (p: SubscriptionPlan) => string[];
}> = {
  free: {
    tagline: '先让小榜认识你',
    pitch: '回答几个问题,小榜整理你的资料,试写几条内容',
    suitable: '第一次来,先试试',
    icon: Sparkles,
    bullets: (p) => [
      `${p.quotas.light_chat} 次轻量写稿 + ${p.quotas.pro_write} 次专业写稿`,
      `${p.quotas.video_minutes} 分钟视频处理`,
      '资料卡 + 画像卡免费玩',
      '注册即送 3,888 算力',
    ],
  },
  personal: {
    tagline: '每周都有人陪你写',
    pitch: '日常想发什么,小榜帮你整理、写稿、改稿、记住反馈',
    suitable: '自己拍、自己发',
    icon: Sparkles,
    bullets: (p) => [
      `每月约 ${Math.round((p.quotas.light_chat + p.quotas.pro_write) / 1)} 条写稿`,
      `${p.quotas.video_minutes} 分钟视频处理(单条 ≤${p.quotas.video_single_cap} 分钟)`,
      `仿写 ${p.quotas.rewrite} 次 / 拆视频 ${p.quotas.video_breakdown} 次`,
      `本月计划 ${p.quotas.monthly_plan} 次 + 资料飞轮持续变准`,
    ],
  },
  growth: {
    tagline: '把一个小生意讲清楚',
    pitch: '产品、客户、案例、顾虑都被小榜记住,写稿不再从零开始',
    suitable: '老板本人 / 销售 / 门店 / 咨询服务',
    icon: Crown,
    bullets: (p) => [
      `每月约 ${p.quotas.light_chat} 条轻量 + ${p.quotas.pro_write} 条专业写稿`,
      `${p.quotas.video_minutes} 分钟视频(单条 ≤${p.quotas.video_single_cap} 分钟)`,
      `仿写 ${p.quotas.rewrite} 次 / 拆视频 ${p.quotas.video_breakdown} 次 / 拆博主体验 ${p.quotas.author_breakdown} 次`,
      `${p.quotas.workspace} 个客户空间 + 产品卡 + 案例库`,
    ],
  },
  agency: {
    tagline: '同时服务多个客户',
    pitch: '每个客户独立资料、产品、计划、框架库和交付记录',
    suitable: '代运营 / 销售团队 / 小工作室',
    icon: Building2,
    bullets: (p) => [
      `${p.quotas.workspace} 个客户独立空间`,
      `${p.quotas.video_minutes} 分钟视频(更高单条 ≤${p.quotas.video_single_cap} 分钟)`,
      `仿写 ${p.quotas.rewrite} 次 / 拆视频 ${p.quotas.video_breakdown} 次 / 完整拆博主 ${p.quotas.author_breakdown} 次`,
      `复盘 ${p.quotas.review} 次 + 计划 ${p.quotas.monthly_plan} 次 + 团队画像 ${p.quotas.team_profile} 次`,
    ],
  },
  partner: {
    tagline: '把系统变成交付工具',
    pitch: '白标导出 + 多客户空间 + 对公开票 + 客户交付记录',
    suitable: '代理商 / MCN / 专业服务商',
    icon: Building2,
    bullets: (p) => [
      `${p.quotas.workspace === -1 ? '按合同/按席位扩' : p.quotas.workspace + ' 个'} 客户空间 + 多席位`,
      `单席 ${p.quotas.video_minutes} 分钟视频(单条 ≤${p.quotas.video_single_cap} 分钟)`,
      `更高额度 + 拆博主 ${p.quotas.author_breakdown} 次/席`,
      '白标导出 + 对公开票 + 客户交付记录',
    ],
  },
  // 2026-05-14 老板要的 ¥0.1 测试套餐 · 用于真机测微信支付 → callback → 自动刷新链路
  // ⚠️ 测试完成请下架: UPDATE subscription_plans SET is_active=FALSE WHERE plan_id='dev_test';
  dev_test: {
    tagline: '🧪 测试链路专用',
    pitch: '老板真机测微信支付 + callback + 前端轮询自动刷新整链路 · 用完即删',
    suitable: '⚠️ 老板专属 · 测完下架',
    icon: Sparkles,
    bullets: () => [
      '极小额度 ¥0.1 · 不影响业务用户',
      '验证微信扫码 → callback → entitlements grant 链路',
      '验证前端轮询 /me 自动跳订阅管理页',
      '⚠️ 测试通过后将下架 · 普通用户看不到',
    ],
  },
};

// dev_test 不进 PLAN_ORDER (避免影响升降级 rank 判定 · 不算正常套餐)
const PLAN_ORDER = ['free', 'personal', 'growth', 'agency', 'partner'];

/**
 * 防御 React error #31: FastAPI 422 detail 是 array of dict, 后端可能返 dict detail,
 * 直接传给 toast.error(<object>) 会被 sonner 当 React child 渲染 → "Objects are not valid"
 */
function detailToText(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (typeof d === 'string' ? d : (d as { msg?: string })?.msg || JSON.stringify(d)))
      .join('; ');
  }
  if (detail && typeof detail === 'object') {
    const d = detail as { message?: string; error?: string; detail?: string };
    return d.message || d.error || d.detail || fallback;
  }
  return fallback;
}

// ========== 主组件 ==========

export default function SubscriptionPlansPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const themeStyle = useSocialStudioTheme();

  const [plans, setPlans] = useState<SubscriptionPlan[]>([]);
  const [currentSub, setCurrentSub] = useState<CurrentSubscription | null>(null);
  const [loading, setLoading] = useState(true);
  const [billingCycle, setBillingCycle] = useState<'monthly' | 'yearly'>('monthly');
  const [selectedPlanId, setSelectedPlanId] = useState<string | null>(null);
  const [submittingPlanId, setSubmittingPlanId] = useState<string | null>(null);
  // 2026-05-13 老板"动态回原价"修: 老用户拉 eligibility 判定是否可用 ¥9.9 首月
  // 不可用时前端隐藏"首月 ¥9.9"角标 + ctaLabel 显原价 · 防用户点了被 400 拒
  const [firstMonthEligible, setFirstMonthEligible] = useState<boolean>(true);
  // PC 扫码用 inline Dialog (B15 修)
  const [payDialog, setPayDialog] = useState<{ codeUrl: string; orderId: string; amountYuan: number; targetPlanId: string } | null>(null);
  // 2026-05-14 老板真机 ¥9.9 付款成功但页面没自动刷新 → P0: PC 扫码 Dialog 完全没轮询
  // 修: payDialog 起后每 3s 调 /api/subscription/me 检测 plan_id 变化 · 检测到 → 关 Dialog + reload
  const payPollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [paymentDone, setPaymentDone] = useState(false);
  const [acceptedPurchaseTerms, setAcceptedPurchaseTerms] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [plansRes, meRes, eligibilityRes] = await Promise.all([
          authFetch('/api/subscription/plans'),
          authFetch('/api/subscription/me').catch(() => null),
          authFetch('/api/subscription/first-month-eligibility').catch(() => null),
        ]);
        const plansData = await plansRes.json().catch(() => null);
        const meData = meRes ? await meRes.json().catch(() => null) : null;
        const eligibilityData = eligibilityRes ? await eligibilityRes.json().catch(() => null) : null;
        if (!alive) return;
        if (plansData?.success && Array.isArray(plansData.data)) {
          setPlans(plansData.data);
          // 默认选中: 用户当前套餐 > 推荐套餐 > growth
          const cur = meData?.success ? (meData.data as CurrentSubscription) : null;
          if (cur) setCurrentSub(cur);
          const defaultId =
            (cur?.active && cur.plan_id) ||
            plansData.data.find((p: SubscriptionPlan) => p.is_recommended)?.plan_id ||
            'growth';
          setSelectedPlanId(defaultId);
        }
        // 默认 eligible=true · 不可用时设 false
        if (eligibilityData?.success && eligibilityData.data?.eligible === false) {
          setFirstMonthEligible(false);
        }
      } catch (e: any) {
        toast.error(`加载套餐失败: ${e?.message || e}`);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  // 微信 oauth 跳转回来后 (2026-05-13 复用 GEO oauth 流程)
  // 微信会把 code 直接放在我们的 redirect_uri 上: /pricing-plans?code=xxx&state=v31_sub:plan:cycle:flag
  // 这里:1. 调 GEO /api/wallet/wechat-jsapi/exchange-openid 用 code 换 openid
  //      2. 拿 openid 重 checkout · 不入库 · 即用即扔
  useEffect(() => {
    const code = searchParams.get('code');
    const state = searchParams.get('state') || '';
    if (!code || !state.startsWith('v31_sub:')) return;

    // 清掉 query 防重复触发(state 也清, 防 React 重渲染时再 fire)
    const next = new URLSearchParams(searchParams);
    next.delete('code');
    next.delete('state');
    setSearchParams(next, { replace: true });

    // 解析 state: v31_sub:plan_id:billing_cycle:is_first_month_flag
    const parts = state.split(':');
    if (parts.length < 4) {
      toast.error('授权回调 state 异常,请重新选择套餐');
      return;
    }
    const [, planId, billingCycleFromState, isFirstMonthFlag] = parts;

    (async () => {
      try {
        // 步骤 1: 用 code 换 openid (复用 GEO 已 prod 跑通的端点)
        const exchangeRes = await authFetch('/api/wallet/wechat-jsapi/exchange-openid', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code }),
        });
        const exchangeData = await exchangeRes.json();
        const openid = exchangeData?.data?.openid;
        if (!openid) {
          toast.error(detailToText(exchangeData.detail, '换 openid 失败,请重试'));
          return;
        }

        // 步骤 2: 用 openid + 原计划信息重新发起 checkout
        const plan = plans.find((p) => p.plan_id === planId);
        if (!plan) {
          toast.error('套餐已下架,请重新选择');
          return;
        }
        setBillingCycle(billingCycleFromState === 'yearly' ? 'yearly' : 'monthly');
        setSelectedPlanId(planId);
        // 等 state 同步好后再 checkout
        setTimeout(() => {
          handleCheckout(plan, openid);
        }, 50);
      } catch (e: any) {
        toast.error(`授权回调处理失败: ${e?.message || e}`);
      }
    })();
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const _ = isFirstMonthFlag; // is_first_month 在 handleCheckout 里按 plan 自判
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plans, searchParams.toString()]);

  const handleCheckout = async (plan: SubscriptionPlan, openidOverride?: string) => {
    if (plan.plan_id === 'partner') {
      window.location.href = 'mailto:?subject=代理团队版咨询';
      return;
    }
    if (plan.plan_id === 'free') {
      try {
        setSubmittingPlanId(plan.plan_id);
        const res = await authFetch('/api/subscription/checkout', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ plan_id: 'free', billing_cycle: 'monthly' }),
        });
        const data = await res.json();
        if (data.success) toast.success('已激活免费体验');
        else toast.error(detailToText(data.detail, '激活失败'));
      } finally {
        setSubmittingPlanId(null);
      }
      return;
    }

    try {
      setSubmittingPlanId(plan.plan_id);
      let termsAcceptanceId = openidOverride
        ? sessionStorage.getItem(PENDING_SUBSCRIPTION_TERMS_KEY)
        : null;
      if (!termsAcceptanceId) {
        if (!acceptedPurchaseTerms) {
          toast.error('请先阅读并勾选《用户服务协议》中的订阅与退款规则');
          return;
        }
        const acceptanceRes = await authFetch('/api/auth/legal-agreements/accept', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            terms_accepted: true,
            terms_version: USER_TERMS_VERSION,
            surface: 'subscription-checkout',
          }),
        });
        const acceptanceData = await acceptanceRes.json();
        termsAcceptanceId = acceptanceData?.acceptance?.acceptance_id || null;
        if (!acceptanceRes.ok || !termsAcceptanceId) {
          toast.error(detailToText(acceptanceData?.detail, '协议确认记录失败，请重试'));
          return;
        }
        sessionStorage.setItem(PENDING_SUBSCRIPTION_TERMS_KEY, termsAcceptanceId);
      }
      // 2026-05-13 修: 用户不符合首月优惠时降级走 monthly 价 · 避免后端 400
      const isFirstMonth = plan.plan_id === 'personal'
        && plan.first_month_yuan !== null
        && firstMonthEligible;
      const reqBody: Record<string, unknown> = {
        plan_id: plan.plan_id,
        billing_cycle: billingCycle,
        is_first_month: isFirstMonth,
        terms_acceptance_id: termsAcceptanceId,
      };
      // 2026-05-13: oauth 回流后带上 openid 给后端
      if (openidOverride) reqBody.openid = openidOverride;
      const res = await authFetch('/api/subscription/checkout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(reqBody),
      });
      const data = await res.json();
      if (!data.success) {
        toast.error(detailToText(data.detail, '下单失败'));
        return;
      }
      // 2026-05-13 复用 GEO oauth: 微信内首次购买需 oauth 拿 openid
      // 后端返 oauth_url(直接是微信 connect URL),前端直接跳
      // 微信跳回 /pricing-plans?code=xxx&state=v31_sub:... 触发上面 useEffect
      if (data.data?.needs_wechat_oauth && data.data?.oauth_url) {
        toast.info('微信内支付需要先授权 · 跳转后自动回到本页继续');
        window.location.href = data.data.oauth_url;
        return;
      }
      if (data.data?.order_id) {
        sessionStorage.removeItem(PENDING_SUBSCRIPTION_TERMS_KEY);
      }
      if (data.data?.todo_b9_pending) {
        toast.info('支付通道接入中,稍后开放');
        return;
      }
      // 微信 V3 直连返 pay_payload(JSAPI / Native code_url / xunhupay url)
      const payload = data.data?.pay_payload;
      if (payload?.code_url) {
        setPayDialog({
          codeUrl: payload.code_url,
          orderId: data.data.order_id,
          amountYuan: data.data.amount_yuan,
          targetPlanId: plan.plan_id,
        });
      } else if (payload?.mweb_url) {
        window.location.href = payload.mweb_url;
      } else if (payload?.url) {
        window.location.href = payload.url;
      } else if (payload?.appId && payload?.package) {
        const result = await invokeWxPay(payload);
        if (result.ok) {
          toast.success('微信支付已完成 · 正在确认订阅状态');
        } else {
          toast.warning(result.errMsg || '支付未完成 · 可重新点击套餐继续');
        }
      } else {
        toast.error('支付链路异常');
      }
    } catch (e: any) {
      toast.error(`下单失败: ${e?.message || e}`);
    } finally {
      setSubmittingPlanId(null);
    }
  };

  const currentPlanId = currentSub?.active ? currentSub.plan_id : null;

  // 2026-05-14 P0 修(老板真机付 ¥9.9 没自动刷新):
  // payDialog 起后每 3s 查 /api/subscription/me · 检测到 plan_id 变为目标套餐即视为 callback 完成
  // 关 Dialog + toast 成功 + reload 主页
  useEffect(() => {
    if (!payDialog) {
      // 关闭时清轮询
      if (payPollTimerRef.current) {
        clearInterval(payPollTimerRef.current);
        payPollTimerRef.current = null;
      }
      return;
    }
    setPaymentDone(false);
    let tries = 0;
    const MAX_TRIES = 200; // 200 × 3s = 10 min · 微信支付二维码有效期 15 min · 留 buffer

    const recordedPlanBefore = currentPlanId;  // 付款前的 plan
    const targetPlanId = payDialog.targetPlanId;

    const poll = async () => {
      tries++;
      if (tries > MAX_TRIES) {
        if (payPollTimerRef.current) clearInterval(payPollTimerRef.current);
        payPollTimerRef.current = null;
        toast.error('支付超时 · 请刷新页面确认订阅状态 · 已扣款未生效请联系客服');
        return;
      }
      try {
        const res = await authFetch('/api/subscription/me');
        const data = await res.json();
        if (!data.success) return;
        const newPlanId = data.data?.plan_id;
        const isActive = !!data.data?.active;
        // 检测到目标套餐激活(plan_id 变成 targetPlanId · 或第一次有订阅)→ 支付成功
        if (isActive && newPlanId === targetPlanId && newPlanId !== recordedPlanBefore) {
          if (payPollTimerRef.current) clearInterval(payPollTimerRef.current);
          payPollTimerRef.current = null;
          setPaymentDone(true);
          toast.success(`支付成功 · ${data.data.display_name || targetPlanId} 已激活`);
          setCurrentSub(data.data);
          setTimeout(() => {
            setPayDialog(null);
            // 跳订阅管理页让用户看完整 entitlements
            navigate('/subscription/manage');
          }, 1500);
        }
      } catch { /* 静默重试 */ }
    };
    poll();   // 立即先查一次防 race
    payPollTimerRef.current = setInterval(poll, 3000);

    return () => {
      if (payPollTimerRef.current) {
        clearInterval(payPollTimerRef.current);
        payPollTimerRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payDialog?.orderId]);

  function ctaLabel(plan: SubscriptionPlan, isSelected: boolean): string {
    if (plan.plan_id === 'partner') return '联系代理顾问';
    if (currentPlanId === plan.plan_id) return '当前套餐';
    if (plan.plan_id === 'free') {
      return currentPlanId ? '降级到免费' : '免费开始';
    }
    // 2026-05-13 修: 用户不符合首月优惠时不再显"¥9.9 体验首月" · 走通用价格逻辑
    if (
      plan.plan_id === 'personal'
      && plan.first_month_yuan !== null
      && currentPlanId !== 'personal'
      && firstMonthEligible
    ) {
      return `¥${plan.first_month_yuan} 体验首月`;
    }
    const curIdx = currentPlanId ? PLAN_ORDER.indexOf(currentPlanId) : -1;
    const tgtIdx = PLAN_ORDER.indexOf(plan.plan_id);
    const verb = curIdx >= 0 && tgtIdx < curIdx ? '降级到' : currentPlanId ? '升级到' : '订阅';
    const priceYuan = billingCycle === 'yearly' && plan.yearly_yuan !== null
      ? Math.round(plan.yearly_yuan / 12)
      : plan.monthly_yuan;
    const cycleSuffix = billingCycle === 'yearly' && plan.yearly_yuan !== null ? '/月 (年付)' : '/月';
    return `${isSelected ? verb : '选择'} ¥${priceYuan}${cycleSuffix}`;
  }

  return (
    <div
      className="min-h-screen bg-[var(--ss-bg)] text-[var(--ss-text)]"
      style={themeStyle}
    >
      <div className="mx-auto max-w-7xl px-4 py-10 md:px-8 md:py-16">
        {/* 顶部返回 */}
        <div className="mb-6 flex items-center justify-between">
          <button
            type="button"
            onClick={() => navigate('/')}  // [WO_260] 原 /s(社媒首页,随 E3 删域)→ 在役首页
            className="flex items-center gap-1.5 rounded-full border border-[var(--ss-line)] bg-[var(--ss-panel)] px-3 py-1.5 text-xs text-[var(--ss-text-soft)] transition hover:bg-[var(--ss-hover)]"
          >
            <ArrowRight className="h-3.5 w-3.5 rotate-180" aria-hidden="true" />
            返回小榜
          </button>
          {currentPlanId && (
            <button
              type="button"
              onClick={() => navigate('/subscription/manage')}
              className="rounded-full border border-[var(--ss-line)] bg-[var(--ss-panel)] px-3 py-1.5 text-xs text-[var(--ss-text-soft)] transition hover:bg-[var(--ss-hover)]"
            >
              管理我的订阅 →
            </button>
          )}
        </div>

        <div className="mb-10 text-center">
          <h1 className="text-3xl font-semibold tracking-tight text-[var(--ss-text)] md:text-4xl">
            选一个最像你的小榜
          </h1>
          <p className="mt-3 text-sm leading-relaxed text-[var(--ss-muted)] md:text-base">
            月卡卖陪伴和配额,算力卖重成本能力,代理包卖交付效率
          </p>

          <div className="mt-6 inline-flex rounded-full border border-[var(--ss-line)] bg-[var(--ss-panel-soft)] p-1 text-xs">
            <button
              type="button"
              className={cn(
                'rounded-full px-4 py-1.5 transition-colors',
                billingCycle === 'monthly'
                  ? 'bg-[var(--ss-primary)] text-[var(--ss-primary-text)]'
                  : 'text-[var(--ss-muted)] hover:text-[var(--ss-text)]',
              )}
              onClick={() => setBillingCycle('monthly')}
            >
              按月
            </button>
            <button
              type="button"
              className={cn(
                'rounded-full px-4 py-1.5 transition-colors',
                billingCycle === 'yearly'
                  ? 'bg-[var(--ss-primary)] text-[var(--ss-primary-text)]'
                  : 'text-[var(--ss-muted)] hover:text-[var(--ss-text)]',
              )}
              onClick={() => setBillingCycle('yearly')}
            >
              按年(省 16-26%)
            </button>
          </div>
          <label className="mx-auto mt-5 flex max-w-2xl items-start justify-center gap-2 text-left text-xs leading-relaxed text-[var(--ss-muted)]">
            <input
              type="checkbox"
              checked={acceptedPurchaseTerms}
              onChange={(event) => setAcceptedPurchaseTerms(event.target.checked)}
              className="mt-0.5 shrink-0"
            />
            <span>
              我已阅读并同意
              <a href={USER_TERMS_URL} target="_blank" rel="noopener noreferrer" className="mx-1 text-[var(--ss-primary)] underline-offset-2 hover:underline">
                《用户服务协议》
              </a>
              ({USER_TERMS_VERSION})，理解订阅取消与退款需要分别处理。
            </span>
          </label>
        </div>

        {loading ? (
          <div className="flex h-96 items-center justify-center">
            <Loader2 className="h-6 w-6 animate-spin text-[var(--ss-muted)]" />
          </div>
        ) : (
          <div className="grid gap-4 sm:gap-5 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
            {plans.map((plan) => {
              const copy = PLAN_COPY[plan.plan_id];
              if (!copy) return null;
              const Icon = copy.icon;
              const isRecommended = plan.is_recommended;
              const isSelected = selectedPlanId === plan.plan_id;
              const isCurrent = currentPlanId === plan.plan_id;
              const yearlyShown = billingCycle === 'yearly' && plan.yearly_yuan !== null;
              const displayPriceYuan = yearlyShown && plan.yearly_yuan !== null
                ? plan.yearly_yuan / 12
                : plan.monthly_yuan;
              // 2026-05-13 修: 老用户(>30 天注册 / 已用过)看到的不应再有"首月 ¥9.9"角标
              const showFirstMonth =
                plan.plan_id === 'personal'
                && plan.first_month_yuan !== null
                && !isCurrent
                && firstMonthEligible;
              const isPartner = plan.plan_id === 'partner';

              return (
                <div
                  key={plan.plan_id}
                  role="button"
                  tabIndex={0}
                  onClick={() => setSelectedPlanId(plan.plan_id)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      setSelectedPlanId(plan.plan_id);
                    }
                  }}
                  aria-pressed={isSelected}
                  aria-label={`${plan.display_name} 套餐${isCurrent ? ',当前已订阅' : ''}${isRecommended ? ',推荐' : ''}${isSelected ? ',已选中' : ''}`}
                  className={cn(
                    'group relative cursor-pointer overflow-hidden rounded-2xl border bg-[var(--ss-panel)] p-5 text-left transition-all duration-150',
                    'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ss-focus)]',
                    isSelected
                      ? 'border-[var(--ss-primary)] shadow-[var(--ss-card-shadow)] ring-2 ring-[var(--ss-primary)]/30 lg:scale-[1.02]'
                      : 'border-[var(--ss-line)] hover:border-[var(--ss-line-soft)] hover:bg-[var(--ss-panel-softer)]',
                  )}
                >
                  {/* 角标:推荐 / 当前 / 首月 */}
                  <div className="absolute right-3 top-3 flex flex-col items-end gap-1.5">
                    {isCurrent && (
                      <span className="rounded-full bg-[var(--ss-success-bg)] px-2 py-0.5 text-[10px] font-medium text-[var(--ss-success)] ring-1 ring-[var(--ss-success-line)]">
                        当前 ✓
                      </span>
                    )}
                    {isRecommended && !isCurrent && (
                      <span className="rounded-full bg-[var(--ss-primary)] px-2 py-0.5 text-[10px] font-medium text-[var(--ss-primary-text)]">
                        推荐
                      </span>
                    )}
                    {showFirstMonth && (
                      <span className="rounded-full bg-[var(--ss-danger-bg)] px-2 py-0.5 text-[10px] font-medium text-[var(--ss-danger)] ring-1 ring-[var(--ss-danger-line)]">
                        首月 ¥{plan.first_month_yuan}
                      </span>
                    )}
                  </div>

                  <div className="flex flex-col gap-4">
                    <div className="flex items-center gap-2 text-[var(--ss-muted)]">
                      <Icon className="h-4 w-4" aria-hidden="true" />
                      <span className="text-xs uppercase tracking-wider">{plan.display_name}</span>
                    </div>

                    <div>
                      <h3 className="text-lg font-semibold leading-tight text-[var(--ss-text)]">
                        {copy.tagline}
                      </h3>
                      <p className="mt-1.5 text-sm leading-relaxed text-[var(--ss-muted)]">
                        {copy.pitch}
                      </p>
                    </div>

                    <div className="flex items-baseline gap-1">
                      {plan.monthly_yuan === 0 ? (
                        <span className="text-3xl font-semibold text-[var(--ss-text)]">¥0</span>
                      ) : isPartner ? (
                        <>
                          <span className="text-2xl font-semibold text-[var(--ss-text)]">按合同</span>
                          <span className="ml-1 text-sm text-[var(--ss-quiet)]">/席位</span>
                        </>
                      ) : (
                        <>
                          <span className="text-3xl font-semibold text-[var(--ss-text)]">
                            ¥{Math.round(displayPriceYuan)}
                          </span>
                          <span className="text-sm text-[var(--ss-quiet)]">/月</span>
                        </>
                      )}
                    </div>

                    {yearlyShown && plan.yearly_yuan !== null && plan.monthly_yuan > 0 && !isPartner && (
                      <div className="text-xs text-[var(--ss-quiet)]">
                        年付 ¥{plan.yearly_yuan} · 折合 ¥{Math.round(plan.yearly_yuan / 12)}/月
                      </div>
                    )}

                    <ul className="flex flex-col gap-2">
                      {copy.bullets(plan).map((b, i) => (
                        <li key={i} className="flex items-start gap-2 text-sm text-[var(--ss-text-soft)]">
                          <Check
                            className="mt-0.5 h-4 w-4 shrink-0 text-[var(--ss-success)]"
                            aria-hidden="true"
                          />
                          <span className="leading-relaxed">{b}</span>
                        </li>
                      ))}
                    </ul>

                    {copy.suitable && (
                      <div className="rounded-lg border border-[var(--ss-line-soft)] bg-[var(--ss-panel-soft)] px-3 py-2 text-xs text-[var(--ss-muted)]">
                        适合: {copy.suitable}
                      </div>
                    )}

                    <button
                      type="button"
                      disabled={submittingPlanId === plan.plan_id || (isCurrent && plan.plan_id !== 'partner')}
                      onClick={(e) => {
                        e.stopPropagation();
                        if (!isSelected) {
                          setSelectedPlanId(plan.plan_id);
                          return;
                        }
                        handleCheckout(plan);
                      }}
                      className={cn(
                        'mt-auto flex h-10 w-full items-center justify-center gap-1.5 rounded-xl text-sm font-medium transition',
                        'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--ss-focus)]',
                        isSelected && !isCurrent
                          ? 'bg-[var(--ss-primary)] text-[var(--ss-primary-text)] hover:bg-[var(--ss-primary-hover)]'
                          : 'border border-[var(--ss-line)] bg-transparent text-[var(--ss-text)] hover:bg-[var(--ss-hover)]',
                        isCurrent && plan.plan_id !== 'partner' && 'cursor-not-allowed opacity-60',
                      )}
                    >
                      {submittingPlanId === plan.plan_id ? (
                        <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                      ) : (
                        <>
                          {ctaLabel(plan, isSelected)}
                          {isSelected && !isCurrent && (
                            <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
                          )}
                        </>
                      )}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <div className="mx-auto mt-14 max-w-3xl space-y-3 text-xs leading-relaxed text-[var(--ss-muted)]">
          <p>· 月卡含的写稿/拆解次数用满后,继续操作会按 ¥0.04-¥15 的具体单价从算力扣,可随时升级套餐获更高额度。</p>
          <p>· 月卡权益按订阅周期重置,不滚存。重要任务请在月内用完。</p>
          <p>· 退订与退款分别处理：关闭续费后本期权益保留至到期；退款按原订单、法定事由和实际使用核验，普通消费者不预扣固定比例费用。</p>
          <p>· 价格变更对已购用户当前周期 + 1 续费周期内冻结,涨价提前 30 天通知。</p>
          <p>· 我们不承诺平台流量、播放、咨询、成交。小榜帮你把过程做清楚、把建议说透明。</p>
        </div>
      </div>

      {/* PC 扫码 inline Dialog · ss-* 风格 */}
      <Dialog open={!!payDialog} onOpenChange={(open) => !open && setPayDialog(null)}>
        <DialogContent
          className="border-[var(--ss-line)] bg-[var(--ss-panel)] text-[var(--ss-text)] sm:max-w-md"
          style={themeStyle}
        >
          <DialogHeader>
            <DialogTitle className="flex items-center justify-between text-[var(--ss-text)]">
              <span>微信扫码支付 ¥{payDialog?.amountYuan}</span>
              <button
                type="button"
                onClick={() => setPayDialog(null)}
                aria-label="关闭"
                className="text-[var(--ss-muted)] hover:text-[var(--ss-text)]"
              >
                <X className="h-4 w-4" />
              </button>
            </DialogTitle>
          </DialogHeader>
          <div className="flex flex-col items-center gap-4 py-4">
            {paymentDone ? (
              // 检测到 callback 完成 → 显成功态 (1.5s 后自动跳 /subscription/manage)
              <div className="flex flex-col items-center gap-3 py-6">
                <div className="grid h-14 w-14 place-items-center rounded-full bg-[var(--ss-success-bg)]">
                  <span className="text-2xl">✓</span>
                </div>
                <p className="text-base font-medium text-[var(--ss-text)]">支付成功</p>
                <p className="text-xs text-[var(--ss-muted)]">即将跳订阅管理页...</p>
              </div>
            ) : (
              <>
                {payDialog?.codeUrl && (
                  <div className="rounded-xl bg-white p-4">
                    <img
                      src={`https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=${encodeURIComponent(payDialog.codeUrl)}`}
                      alt="微信支付二维码"
                      className="size-[220px]"
                    />
                  </div>
                )}
                <div className="text-center text-xs text-[var(--ss-muted)]">
                  <p>请用微信扫码完成支付 · 15 分钟内有效</p>
                  <p className="mt-1">订单号: {payDialog?.orderId}</p>
                  <p className="mt-1 flex items-center justify-center gap-1">
                    <Loader2 className="h-3 w-3 animate-spin" />
                    <span>等待支付完成中 · 自动检测</span>
                  </p>
                </div>
              </>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
