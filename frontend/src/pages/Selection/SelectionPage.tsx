import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { SelectionHeader } from './components/SelectionHeader';
import { KeywordCard } from './components/KeywordCard';
import { CategoryTabs } from './components/CategoryTabs';
import { StatsBar } from './components/StatsBar';
import { CustomKeywordInput } from './components/CustomKeywordInput';
import { BottomActionBar } from './components/BottomActionBar';
import { WaitingQuote } from './components/WaitingQuote';
import { TierSelector } from './components/TierSelector';
import { HelpHint } from '@/components/onboarding/HelpHint';
import { PricingTable } from './components/PricingTable';
import { ConfirmCelebration } from './components/ConfirmCelebration';
import { ExpiredPage } from './components/ExpiredPage';
import { ClusterCard } from './components/ClusterCard';
import type { ClusterData } from './components/ClusterCard';
// [阶段 2 · WO_GAPPLAN_RELOCATION_A 2026-08-10] 售前版交付计划(纯展示 · 不发请求)
import { GapPlanPreview } from '@/components/gapPlan/GapPlanPreview';
import type { GapPlanCustomerPreview } from '@/lib/gapPlanApi';
import { useInteractionTracker } from './hooks/useInteractionTracker';
import { useForceLightMode } from './hooks/useForceLightMode';
import { mountOpened, trackSubmittedKeywords } from '@/lib/customerEvents';
import { PrivacyNotice } from '@/components/customer/PrivacyNotice';
import type { ResolvedBrand } from '@/hooks/useWhitelabel';
import { isPricedKeyword, quoteBlockingMessage, quoteSummaryText, summarizeQuoteSelection } from './utils/quoteAnnotation';
import { ExcludedKeywordsPanel } from '@/components/quote/ExcludedKeywordsPanel';
import type { ExcludedKeyword, SuggestedKeyword } from '@/components/quote/ExcludedKeywordsPanel';
// [#178 2026-09-12] 「选中方向下的词全被排除」不再是死胡同:两条出口 + 就地说明原因
import { AllExcludedExitCard } from './components/AllExcludedExitCard';
import {
  readAllExcluded,
  reconcileFromPage,
  shouldClaimSubmitted,
  submitGateState,
  type AllExcludedState,
} from './utils/allExcludedExits';
import { toast } from 'sonner';
import { OssAttribution } from '@/components/common/OssAttribution';

interface KeywordItem {
  id: number;
  keyword: string;
  category: string;
  category_label: string;
  difficulty: number;
  recommended: boolean;
  recommendation_reason?: string;
  intent?: string;
}

interface PricingKeyword {
  id: number;
  keyword: string;
  category_label: string;
  recommendation_reason?: string;
  entry: { price: number; articles: number };
  standard: { price: number; articles: number };
  flagship: { price: number; articles: number };
  // 【§4.2】超级红海词:不出保证价 · 批量勾选/默认全选排除 · 普通报价确认链路不成交
  super_red_ocean?: boolean;
  super_red_ocean_level?: string;
  competition_ratio?: number;
  needs_review?: boolean;
}

interface TierInfo {
  label: string;
  target_share: number;
  ai_probability: string;
  stars: number;
  total_price: number;
  total_articles: number;
}

interface UnavailableKeyword {
  keyword: string;
  reason: string;
}

interface PricingData {
  generated_at: string;
  tiers: Record<string, TierInfo>;
  keywords: PricingKeyword[];
  // 数据断供词(不出价 · 提示稍后重试)
  unavailable_keywords?: UnavailableKeyword[];
  // [报价意图闸 2026-08-04] 不进付费交付的词(只读展示 · 不在 keywords 里 · 不计价)
  excluded_keywords?: ExcludedKeyword[];
  suggested_keywords?: SuggestedKeyword[];
  policy_gate_status?: string;
}

interface ClustersData {
  generated_at: string;
  clusters: ClusterData[];
  unclustered_keywords: unknown[];
  stats: {
    total_keywords: number;
    cluster_count: number;
    total_core: number;
    total_covered: number;
    total_variants: number;
  };
  tier_summaries: Record<string, {
    core_price: number;
    full_price: number;
    savings: number;
    total_articles: number;
  }>;
  // 数据断供词(不出价 · 提示稍后重试)
  unavailable_keywords?: UnavailableKeyword[];
}

interface SelectionContext {
  customer_persona: {
    type: string;
    description: string;
    search_behavior: string;
    decision_journey: string;
  };
  methodology: {
    steps: string[];
    summary: string;
  };
}

interface BusinessLine {
  id: number;
  name: string;
  description: string;
  example_scenarios: string[];
  is_selected: boolean;
}

interface PageData {
  brand_name: string;
  status: string;
  expires_at?: string;
  keywords?: KeywordItem[];
  business_lines?: BusinessLine[] | null;
  selected_ids?: number[];
  custom_keywords?: string[];
  pricing_data?: PricingData | null;
  clusters_data?: ClustersData | null;
  selected_tier?: string;
  pending_keywords?: string[];
  final_keyword_ids?: number[];
  confirmed_at?: string;
  confirmed_total_price?: number;
  keywords_submitted_at?: string;
  selection_context?: SelectionContext;
  // Phase B (CTO-15.11):激活后给客户门户链接 · ConfirmCelebration "查看数据看板"按钮
  portal_token?: string | null;
  // v3.6 白标 · 客户页下发契约 · [audit #13 2026-06-10] 去 owner_user_id(不暴露内部代理 user_id)·
  //   白标改读后端内联 whitelabel(已 customer-gate + 联系方式脱敏)
  whitelabel?: ResolvedBrand | null;
  branding_status?: 'platform' | 'approved_whitelabel';
  // [阶段 2 2026-08-10] 售前版交付计划。服务端只在 quoted / adding_keywords 两档下发,
  // 算不出来时是 null —— 客户页不因为它缺失而报错(它是加分项,不是主链路)。
  delivery_plan?: GapPlanCustomerPreview | null;
}

const TIER_META: Record<string, { label: string; ai_probability: string }> = {
  entry: { label: '入门版', ai_probability: '50%' },
  standard: { label: '标准版', ai_probability: '65%' },
  flagship: { label: '旗舰版', ai_probability: '75%' },
};

export function SelectionPage() {
  useForceLightMode();
  const { token } = useParams<{ token: string }>();
  const [data, setData] = useState<PageData | null>(null);
  // [SSOT v1.0 §3.3] 提交后被排除的知识词逐条可见(页面内联横幅,
  // 不用 alert —— iOS WebView 下原生弹窗可能不显示,可见性保证会失效)
  const [deliveryExcluded, setDeliveryExcluded] = useState<Array<{ keyword?: string; reason?: string }>>([]);
  // [#178] 全排除态(后端 all_excluded=true)。null = 不在这一态 —— 不由前端按计数推。
  const [allExcluded, setAllExcluded] = useState<AllExcludedState | null>(null);
  const [notifyInFlight, setNotifyInFlight] = useState(false);
  const [notifyAlreadySent, setNotifyAlreadySent] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const submitInFlightRef = useRef(false);

  // v3.6 白标 · /s/:token 客户场景 · [audit #13 2026-06-10] 白标改读后端内联 data.whitelabel
  //   (已 customer-gate + 联系方式脱敏)· 不再下发/按 owner_user_id 二次调白标(防匿名枚举代理画像)
  const brand: ResolvedBrand | null = data?.whitelabel ?? null;

  // Selection phase state
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [customKeywords, setCustomKeywords] = useState<string[]>([]);
  const [activeCategory, setActiveCategory] = useState('all');

  // Pricing phase state
  const [activeTier, setActiveTier] = useState('standard');
  const [checkedPricingIds, setCheckedPricingIds] = useState<Set<number>>(new Set());

  // Cluster mode state
  const [clusters, setClusters] = useState<ClusterData[]>([]);

  // Business line selection state (new flow)
  const [selectedBusinessLines, setSelectedBusinessLines] = useState<Set<number>>(new Set());
  const [businessLinesSubmitted, setBusinessLinesSubmitted] = useState(false);

  // Hover tracking
  const hoverTimers = useRef<Map<number, NodeJS.Timeout>>(new Map());

  const isClusterMode = !!(data?.clusters_data?.clusters?.length);

  const phase = useMemo(() => {
    if (!data) return 'selection';
    if (data.status === 'quoted' || data.status === 'adding_keywords') return 'pricing';
    if (data.status === 'confirmed') return 'confirmed';
    return 'selection';
  }, [data?.status]);

  const { track } = useInteractionTracker(token || '', phase);

  // 客户行为埋点 (CTO-C 2026-04-26 · feat/m3-customer-signals)
  // mount 即写 opened + 启动 dwell 计时器 (30s/120s)
  useEffect(() => {
    if (!token) return;
    const cleanup = mountOpened({ source: 'selection', rawToken: token });
    return () => cleanup();
  }, [token]);

  // Fetch page data
  const fetchData = useCallback(async () => {
    if (!token) return;
    try {
      const res = await fetch(`/api/s/${token}`);
      if (!res.ok) {
        if (res.status === 404) setError('链接不存在');
        else setError('加载失败');
        return;
      }
      const d: PageData = await res.json();
      setError('');
      setData(d);
      // [#178] 刷新/微信里重开也要看得见卡片。GET 没带这几个字段时**保留**已有态,
      //   不覆盖 —— 否则 POST 刚给的卡片会被紧随其后的这次 fetchData 抹掉。
      setAllExcluded(prev => reconcileFromPage(prev, d));

      // Initialize state from server data
      // 选词阶段：如果服务端没有已选ID，默认全选（损失厌恶：让客户去掉不想要的）
      if (d.selected_ids && d.selected_ids.length > 0) {
        setSelectedIds(new Set(d.selected_ids));
      } else if ((d.status === 'selecting' || d.status === 'business_lines_submitted') && d.keywords?.length) {
        setSelectedIds(new Set(d.keywords.map((k: { id: number }) => k.id)));
      }
      if (d.custom_keywords) setCustomKeywords(d.custom_keywords);
      if (d.selected_tier) setActiveTier(d.selected_tier);

      // Initialize business lines: respect server state (default unselected)
      if (d.business_lines?.length) {
        const preSelected = d.business_lines.filter(bl => bl.is_selected).map(bl => bl.id);
        setSelectedBusinessLines(new Set(preSelected));
        // [CTO-15.23 2026-05-12 BUG fix] 撤回回归态修复
        //   老板报:点"我想改一下选择"撤回后业务卡片仍被锁死无法再选
        //   后端 /withdraw-keywords 把 status 回退到 'selecting' · business_lines 的 is_selected
        //   保留(预填客户上次选择 · 损失厌恶)· 老逻辑只看 preSelected.length>0 就把 submitted=true
        //   导致撤回后业务卡仍 disabled · 底部按钮仍显示"已提交,请等待方案"
        //   修法:submitted 跟随 status · 仅 status 已迈过 'selecting' 时为 true
        //   'selecting'(初始 / 撤回后)→ 可重选 · 客户能勾掉/勾上业务线后再次提交
        setBusinessLinesSubmitted(d.status !== 'selecting' && preSelected.length > 0);
      } else {
        // 没有业务线数据时(异常态)强制重置 · 防陈旧 state 残留
        setBusinessLinesSubmitted(false);
      }

      // For pricing phase, check all pricing keyword ids by default
      if (d.pricing_data?.keywords) {
        // [§4.2] 默认全选(损失厌恶)排除超红海词 · 超红海只标记待报价
        const ids = d.selected_ids
          ? new Set(d.selected_ids)
          : new Set(d.pricing_data.keywords.filter(k => !k.super_red_ocean).map(k => k.id));
        setCheckedPricingIds(ids);
      }

      // Initialize cluster state from server data
      if (d.clusters_data?.clusters?.length) {
        if (d.status === 'quoted') {
          // 首次看到报价：全部展开、全部未选，客户主动选择想要的
          setClusters(d.clusters_data.clusters.map((c: ClusterData) => ({
            ...c,
            is_selected: false,
            core_keywords: c.core_keywords.map(kw => ({ ...kw, is_selected: false })),
          })));
        } else {
          // confirmed/active 等状态保留服务端的选择状态
          setClusters(d.clusters_data.clusters);
        }
      }
    } catch {
      setError('网络错误');
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    fetchData();
    track({ type: 'page_open' });
  }, [fetchData]);

  // Polling when waiting for quote
  useEffect(() => {
    if (!data || !['keywords_submitted', 'adding_keywords', 'pricing_pending_review'].includes(data.status)) return;
    const interval = setInterval(fetchData, 5000);
    return () => clearInterval(interval);
  }, [data?.status, fetchData]);

  // ===== Selection phase handlers =====
  const handleToggleKeyword = useCallback((id: number) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
        track({ type: 'keyword_deselect', keyword_id: id });
      } else {
        next.add(id);
        track({ type: 'keyword_select', keyword_id: id });
      }
      return next;
    });
  }, [track]);

  const handleHoverStart = useCallback((id: number) => {
    const timer = setTimeout(() => {
      track({ type: 'keyword_hover', keyword_id: id, data: { duration_ms: 2000 } });
    }, 2000);
    hoverTimers.current.set(id, timer);
  }, [track]);

  const handleHoverEnd = useCallback((id: number) => {
    const timer = hoverTimers.current.get(id);
    if (timer) {
      clearTimeout(timer);
      hoverTimers.current.delete(id);
    }
  }, []);

  const handleAddCustom = useCallback((keyword: string) => {
    setCustomKeywords(prev => [...prev, keyword]);
    track({ type: 'keyword_add', data: { keyword_text: keyword } });
  }, [track]);

  const handleSubmitKeywords = useCallback(async () => {
    if (!token || submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setSubmitting(true);
    try {
      const res = await fetch(`/api/s/${token}/submit-keywords`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          selected_ids: Array.from(selectedIds),
          custom_keywords: customKeywords,
        }),
      });
      if (res.ok) {
        track({ type: 'keywords_submit', data: { selected_ids: Array.from(selectedIds) } });
        // CTO-C 客户行为埋点 · 提交关键词
        if (token) trackSubmittedKeywords({ source: 'selection', rawToken: token });
        // [SSOT v1.0 §3.3] 数量变化必须可见:知识/百科类问法被排除时逐条告知原因
        const okData = await res.json().catch(() => null);
        const excluded = [
          ...(Array.isArray(okData?.delivery_excluded_keywords) ? okData.delivery_excluded_keywords : []),
          ...(Array.isArray(okData?.delivery_excluded_custom_keywords) ? okData.delivery_excluded_custom_keywords : []),
        ];
        setDeliveryExcluded(excluded);
        // [#178] 选中 + 自定义全被排除 ⇒ 后端 200 不落库,前端给出口卡而不是 toast
        const nextAllExcluded = readAllExcluded(okData);
        setAllExcluded(nextAllExcluded.allExcluded ? nextAllExcluded : null);
        fetchData();
      } else {
        const errData = await res.json().catch(() => null);
        toast.error(errData?.detail || `提交失败 (${res.status})，请刷新重试`);
      }
    } catch {
      toast.error('网络错误，请检查网络连接后重试');
    } finally {
      submitInFlightRef.current = false;
      setSubmitting(false);
    }
  }, [token, selectedIds, customKeywords, track, fetchData]);

  // Business line submit handler (new flow)
  const handleSubmitBusinessLines = useCallback(async () => {
    if (!token || selectedBusinessLines.size === 0 || submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setSubmitting(true);
    try {
      const res = await fetch(`/api/s/${token}/submit-business-lines`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          selected_business_line_ids: Array.from(selectedBusinessLines),
        }),
      });
      if (res.ok) {
        track({ type: 'business_lines_submit', data: { selected_ids: Array.from(selectedBusinessLines) } });
        // [SSOT v1.0 §3.3] 被排除的知识词逐条可见,不静默缩减
        const okData = await res.json().catch(() => null);
        const excluded = Array.isArray(okData?.delivery_excluded_keywords)
          ? okData.delivery_excluded_keywords : [];
        setDeliveryExcluded(excluded);
        // [#178] 全排除时**不许**挂「已提交,正在准备方案」—— 没有人在准备方案。
        const nextAllExcluded = readAllExcluded(okData);
        setAllExcluded(nextAllExcluded.allExcluded ? nextAllExcluded : null);
        setBusinessLinesSubmitted(!nextAllExcluded.allExcluded);
        fetchData();
      } else {
        const errData = await res.json().catch(() => null);
        const msg = errData?.detail || `提交失败 (${res.status})`;
        toast.error(typeof msg === 'string' ? msg : '提交失败，请刷新页面重试');
      }
    } catch (e) {
      toast.error('网络错误，请检查网络连接后重试');
    } finally {
      submitInFlightRef.current = false;
      setSubmitting(false);
    }
  }, [token, selectedBusinessLines, track, fetchData]);

  /**
   * [#178] 出口②:让报价方补充商业选型问法。
   *
   * 后端在 all_excluded 那一刻已经推过一次(`next_action.notify_sent`),这里是补发入口 ——
   * 客户等久了想再催一次。失败时照实说,不静默(静默失败会让客户以为催过了)。
   */
  const handleNotifyNoDeliverable = useCallback(async () => {
    if (!token || notifyInFlight) return;
    setNotifyInFlight(true);
    try {
      const res = await fetch(`/api/s/${token}/notify-no-deliverable`, { method: 'POST' });
      const body = await res.json().catch(() => null);
      if (res.ok && (body?.notify_sent === true || body?.already_sent === true)) {
        setNotifyAlreadySent(true);
      } else {
        toast.error('没能通知到报价方，请直接联系他，或在上方自己加一条选型问法');
      }
    } catch {
      toast.error('网络错误，没能通知到报价方，请稍后再试');
    } finally {
      setNotifyInFlight(false);
    }
  }, [token, notifyInFlight]);

  const toggleBusinessLine = useCallback((id: number) => {
    setSelectedBusinessLines(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleWithdraw = useCallback(async () => {
    if (!token) return;
    setSubmitting(true);
    try {
      const res = await fetch(`/api/s/${token}/withdraw-keywords`, { method: 'POST' });
      if (res.ok) {
        track({ type: 'keywords_withdraw' });
        fetchData();
      }
    } finally {
      setSubmitting(false);
    }
  }, [token, track, fetchData]);

  // ===== Pricing phase handlers =====
  const handleTierSwitch = useCallback((tier: string) => {
    track({ type: 'tier_switch', data: { from_tier: activeTier, to_tier: tier } });
    setActiveTier(tier);
  }, [activeTier, track]);

  const handleTierHoverStart = useCallback((tier: string) => {
    const timer = setTimeout(() => {
      track({ type: 'tier_hover', data: { tier, duration_ms: 3000 } });
    }, 3000);
    hoverTimers.current.set(-1 * (tier === 'entry' ? 1 : tier === 'standard' ? 2 : 3), timer);
  }, [track]);

  const handleTierHoverEnd = useCallback((tier: string) => {
    const key = -1 * (tier === 'entry' ? 1 : tier === 'standard' ? 2 : 3);
    const timer = hoverTimers.current.get(key);
    if (timer) {
      clearTimeout(timer);
      hoverTimers.current.delete(key);
    }
  }, []);

  const handleTogglePricingKeyword = useCallback((id: number) => {
    setCheckedPricingIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
        track({ type: 'price_keyword_deselect', keyword_id: id });
      } else {
        next.add(id);
        track({ type: 'price_keyword_reselect', keyword_id: id });
      }
      return next;
    });
  }, [track]);

  const handleAddMoreKeywords = useCallback(async (keyword: string) => {
    if (!token) return;
    setSubmitting(true);
    try {
      const res = await fetch(`/api/s/${token}/add-keywords`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ keywords: [keyword] }),
      });
      if (res.ok) {
        track({ type: 'add_more_keywords', data: { keyword_texts: [keyword] } });
        fetchData();
      }
    } finally {
      setSubmitting(false);
    }
  }, [token, track, fetchData]);

  const handleBatchAddKeywords = useCallback(async (keywords: string[]) => {
    if (!token || keywords.length === 0) return;
    setSubmitting(true);
    try {
      const res = await fetch(`/api/s/${token}/add-keywords`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ keywords }),
      });
      if (res.ok) {
        track({ type: 'add_more_keywords', data: { keyword_texts: keywords } });
        fetchData();
      }
    } finally {
      setSubmitting(false);
    }
  }, [token, track, fetchData]);

  const handleCancelAddKeywords = useCallback(async () => {
    if (!token) return;
    setSubmitting(true);
    try {
      const res = await fetch(`/api/s/${token}/cancel-add-keywords`, { method: 'POST' });
      if (res.ok) fetchData();
    } finally {
      setSubmitting(false);
    }
  }, [token, fetchData]);

  // Cluster mode computed values (must be before handleConfirmQuote which references them)
  const clusterSelectedCoreKeywords = useMemo(() => {
    return clusters
      .filter(c => c.is_selected)
      .flatMap(c => c.core_keywords);
  }, [clusters]);

  const clusterQuoteSummary = useMemo(() => {
    const tier = activeTier as 'entry' | 'standard' | 'flagship';
    return summarizeQuoteSelection(clusterSelectedCoreKeywords, tier, kw => kw.is_selected);
  }, [clusterSelectedCoreKeywords, activeTier]);

  const clusterTotalPrice = clusterQuoteSummary.totalPrice;

  const clusterDynamicTierPrices = useMemo(() => {
    const result: Record<string, number> = {};
    for (const t of ['entry', 'standard', 'flagship'] as const) {
      result[t] = clusters
        .filter(c => c.is_selected)
        .reduce((sum, c) => {
          return sum + c.core_keywords
            .filter(kw => kw.is_selected && isPricedKeyword(kw, t))
            .reduce((s, kw) => s + (kw[t]?.price || 0), 0);
        }, 0);
    }
    return result;
  }, [clusters]);

  const clusterSelectedCount = useMemo(() =>
    clusters.filter(c => c.is_selected).length
  , [clusters]);

  const clusterTotalCoreCount = useMemo(() =>
    clusters.filter(c => c.is_selected)
      .reduce((sum, c) => sum + c.core_keywords.filter(kw => kw.is_selected).length, 0)
  , [clusters]);

  const pricingQuoteSummary = useMemo(() => {
    if (!data?.pricing_data?.keywords) {
      return summarizeQuoteSelection([], activeTier as 'entry' | 'standard' | 'flagship', () => false);
    }
    const tier = activeTier as 'entry' | 'standard' | 'flagship';
    return summarizeQuoteSelection(data.pricing_data.keywords, tier, kw => checkedPricingIds.has(kw.id));
  }, [data?.pricing_data, activeTier, checkedPricingIds]);

  const handleConfirmQuote = useCallback(async () => {
    if (!token) return;
    const currentSummary = isClusterMode ? clusterQuoteSummary : pricingQuoteSummary;
    if (!currentSummary.canConfirm) {
      const blocking = quoteBlockingMessage(currentSummary);
      toast.error(blocking || '请选择可报价的核心词后再确认报价');
      return;
    }
    setSubmitting(true);
    try {
      // Build body — cluster mode sends cluster selections, flat mode sends keyword IDs
      const body: Record<string, unknown> = { tier: activeTier };
      if (isClusterMode) {
        const tierKey = activeTier as 'entry' | 'standard' | 'flagship';
        body.clusters_selection = clusters
          .filter(c => c.is_selected)
          .map(c => {
            const selectedCore = c.core_keywords.filter(kw => kw.is_selected && isPricedKeyword(kw, tierKey));
            // [§4.2] 覆盖词比例分母排除超红海(与保证价口径一致)
            const totalCorePrice = c.core_keywords.filter(kw => isPricedKeyword(kw, tierKey)).reduce((s, kw) => s + (kw[tierKey]?.price || 0), 0);
            const selectedCorePrice = selectedCore.reduce((s, kw) => s + (kw[tierKey]?.price || 0), 0);
            const coveredRatio = totalCorePrice > 0 ? selectedCorePrice / totalCorePrice : 0;
            const effectiveCoveredCount = Math.ceil(c.covered_keywords.length * coveredRatio);
            return {
              cluster_name: c.cluster_name,
              selected_core_ids: selectedCore.map(kw => kw.id),
              covered_count: effectiveCoveredCount,
            };
          });
      } else {
        const tierKey = activeTier as 'entry' | 'standard' | 'flagship';
        body.selected_keyword_ids = (data?.pricing_data?.keywords || [])
          .filter(kw => checkedPricingIds.has(kw.id) && isPricedKeyword(kw, tierKey))
          .map(kw => kw.id);
      }
      const res = await fetch(`/api/s/${token}/confirm-quote`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        track({ type: 'quote_confirm', data: { tier: activeTier, keyword_count: currentSummary.pricedCount } });
        // CTO-C 客户行为埋点 · 报价档位确认 (业务上等同二次提交)
        if (token) trackSubmittedKeywords({ source: 'selection', rawToken: token, metadata: { stage: 'confirm_tier' } });
        await new Promise(r => setTimeout(r, 500));
        await fetchData();
      } else {
        // [CTO-15.23 2026-05-06] race condition 优雅降级:
        // 销售在客户选词时撤回报价(quoted → pricing_pending_review)· 后端报"当前状态...不允许"
        // → fetchData 拉最新状态 + 友好提示 · UI 自动切回 WaitingQuote · 不弹丑陋 alert
        const errData = await res.json().catch(() => null);
        const detail = errData?.detail || '';
        const isStatusRace = /当前状态.*不允许|status.*不允许/.test(detail);
        if (isStatusRace) {
          await fetchData();  // 自动 refresh · UI 会按新 status 切到 WaitingQuote
          toast('销售正在调整价格 · 已为您刷新最新状态');
        } else {
          toast.error(detail || `确认失败 (${res.status})，请刷新重试`);
        }
      }
    } catch {
      toast.error('网络错误，请检查网络连接后重试');
    } finally {
      setSubmitting(false);
    }
  }, [token, activeTier, checkedPricingIds, data?.pricing_data, isClusterMode, clusters, clusterQuoteSummary, pricingQuoteSummary, track, fetchData]);

  // ===== Cluster mode handlers =====
  const handleToggleCluster = useCallback((clusterName: string) => {
    setClusters(prev => prev.map(c => {
      if (c.cluster_name !== clusterName) return c;
      const newSelected = !c.is_selected;
      return {
        ...c,
        is_selected: newSelected,
        // [§4.2] 整包勾选不带超红海词(需单独标记待报价)· 取消则全清
        core_keywords: c.core_keywords.map(kw =>
          newSelected && kw.super_red_ocean ? { ...kw, is_selected: false } : { ...kw, is_selected: newSelected }
        ),
      };
    }));
    track({ type: 'cluster_toggle', data: { cluster_name: clusterName } });
  }, [track]);

  const handleToggleCoreKeyword = useCallback((clusterName: string, keywordId: number) => {
    setClusters(prev => prev.map(c => {
      if (c.cluster_name !== clusterName) return c;
      const updatedCore = c.core_keywords.map(kw =>
        kw.id === keywordId ? { ...kw, is_selected: !kw.is_selected } : kw
      );
      // If all core keywords deselected, deselect the whole cluster
      const anySelected = updatedCore.some(kw => kw.is_selected);
      return { ...c, core_keywords: updatedCore, is_selected: anySelected };
    }));
    track({ type: 'core_keyword_toggle', keyword_id: keywordId, data: { cluster_name: clusterName } });
  }, [track]);

  const handleUpgradeCovered = useCallback((clusterName: string, keyword: string) => {
    setClusters(prev => prev.map(c => {
      if (c.cluster_name !== clusterName) return c;
      const coveredKw = c.covered_keywords.find(kw => kw.keyword === keyword && kw.upgradeable);
      if (!coveredKw || !coveredKw.id) return c;
      const newCore = [...c.core_keywords, {
        id: coveredKw.id,
        keyword: coveredKw.keyword,
        core_score: 0,
        search_volume: 0,
        is_selected: true,
        upgraded_from_covered: true,
        entry: coveredKw.entry,
        standard: coveredKw.standard,
        flagship: coveredKw.flagship,
      }];
      const newCovered = c.covered_keywords.filter(kw => kw.keyword !== keyword);
      return { ...c, core_keywords: newCore, covered_keywords: newCovered };
    }));
    track({ type: 'covered_upgrade', data: { cluster_name: clusterName, keyword } });
  }, [track]);

  const handleDemoteCore = useCallback((clusterName: string, keywordId: number) => {
    setClusters(prev => prev.map(c => {
      if (c.cluster_name !== clusterName) return c;
      const coreKw = c.core_keywords.find(kw => kw.id === keywordId && kw.upgraded_from_covered);
      if (!coreKw) return c;
      // Move back to covered
      const newCovered = [...c.covered_keywords, {
        keyword: coreKw.keyword,
        source: 'demoted',
        upgradeable: true,
        id: coreKw.id,
        entry: coreKw.entry,
        standard: coreKw.standard,
        flagship: coreKw.flagship,
      }];
      const newCore = c.core_keywords.filter(kw => kw.id !== keywordId);
      const anySelected = newCore.some(kw => kw.is_selected);
      return { ...c, core_keywords: newCore, covered_keywords: newCovered, is_selected: anySelected || c.is_selected };
    }));
    track({ type: 'core_demote', keyword_id: keywordId, data: { cluster_name: clusterName } });
  }, [track]);

  // ===== Category filtering =====
  const keywords = data?.keywords || [];
  const categories = useMemo(() => {
    const counts: Record<string, { label: string; count: number }> = {};
    for (const kw of keywords) {
      const cat = kw.category || 'general';
      if (!counts[cat]) counts[cat] = { label: kw.category_label || cat, count: 0 };
      counts[cat].count++;
    }
    return [
      { category: 'all', label: '全部', count: keywords.length },
      ...Object.entries(counts).map(([k, v]) => ({ category: k, ...v })),
    ];
  }, [keywords]);

  const filteredKeywords = useMemo(() => {
    if (activeCategory === 'all') return keywords;
    return keywords.filter(kw => (kw.category || 'general') === activeCategory);
  }, [keywords, activeCategory]);

  // ===== Pricing computed values =====
  // 每个套餐的动态价格（根据客户勾选的关键词）
  const dynamicTierPrices = useMemo(() => {
    if (!data?.pricing_data?.keywords) return {} as Record<string, number>;
    const result: Record<string, number> = {};
    for (const t of ['entry', 'standard', 'flagship'] as const) {
      result[t] = data.pricing_data.keywords
        .filter(kw => checkedPricingIds.has(kw.id) && isPricedKeyword(kw, t))
        .reduce((sum, kw) => sum + (kw[t]?.price || 0), 0);
    }
    return result;
  }, [data?.pricing_data, checkedPricingIds]);

  // [#178] 全排除态下提交键的闸 + 就地原因(两者同生同死:禁用必带一句话)。
  //   可交付数 = 客户新加的问法数 —— 目录里那些词已经被后端全排除了,再提交一遍还是全排除。
  const submitGate = useMemo(() => submitGateState({
    allExcluded: !!allExcluded,
    deliverableCount: customKeywords.length,
    submitting,
  }), [allExcluded, customKeywords.length, submitting]);

  // ===== Render =====
  if (loading) {
    return (
      <div className="min-h-[100dvh] pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] flex items-center justify-center bg-linear-to-b from-gray-50 to-white">
        <div className="text-center">
          <div className="w-14 h-14 md:w-16 md:h-16 mx-auto mb-4 rounded-2xl bg-white shadow-lg shadow-blue-100 flex items-center justify-center">
            <svg className="animate-spin w-6 h-6 md:w-7 md:h-7 text-blue-500" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
          </div>
          <p className="text-sm md:text-base text-gray-400">加载中...</p>
        </div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="min-h-[100dvh] pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] flex items-center justify-center bg-linear-to-b from-gray-50 to-white">
        <div className="text-center px-8">
          <div className="w-16 h-16 md:w-20 md:h-20 mx-auto mb-4 rounded-2xl bg-gray-100 flex items-center justify-center">
            <span className="text-3xl md:text-4xl">😔</span>
          </div>
          <p className="text-lg md:text-xl font-medium text-gray-700 mb-1">{error || '页面不存在'}</p>
          <p className="text-sm md:text-base text-gray-400">请联系您的专属顾问获取帮助</p>
        </div>
      </div>
    );
  }

  // Expired
  if (data.status === 'expired') {
    return <ExpiredPage brand={brand} />;
  }

  // [#178] 全排除出口卡。业务线步与选词步**同一张卡**(同一个谓词只许有一处实现,
  //   否则必有一处没人验);不在这一态时是 null,整页零变化。
  const allExcludedCard = allExcluded ? (
    <AllExcludedExitCard
      state={allExcluded}
      brandName={data.brand_name}
      category={data.business_lines?.find(bl => bl.is_selected)?.name}
      customKeywords={customKeywords}
      onAddKeyword={handleAddCustom}
      onNotify={handleNotifyNoDeliverable}
      notifyInFlight={notifyInFlight}
      notifyAlreadySent={notifyAlreadySent}
    />
  ) : null;

  // Confirmed / pending_payment / active / payment_overdue
  if (['confirmed', 'pending_payment', 'active', 'payment_overdue'].includes(data.status)) {
    const tierKey = data.selected_tier || 'standard';
    const tierMeta = TIER_META[tierKey] || TIER_META.standard;
    const finalIds = new Set(data.final_keyword_ids || []);
    const tier = tierKey as 'entry' | 'standard' | 'flagship';
    const totalArticles = data.pricing_data?.keywords
      ?.filter(kw => finalIds.has(kw.id))
      .reduce((sum, kw) => sum + (kw[tier]?.articles || 0), 0) || 0;

    const confirmedClusterCount = data.clusters_data?.clusters?.filter(c => c.is_selected).length;

    // All post-confirm statuses show the celebration page to the client
    return (
      <ConfirmCelebration
        brand={brand}
        brandName={data.brand_name}
        tierLabel={tierMeta.label}
        aiProbability={tierMeta.ai_probability}
        keywordCount={finalIds.size}
        totalArticles={totalArticles}
        totalPrice={data.confirmed_total_price || 0}
        clusterCount={confirmedClusterCount}
        portalToken={data.portal_token || undefined}
      />
    );
  }

  // Keywords submitted or pricing pending review - waiting for quote
  if (data.status === 'keywords_submitted' || data.status === 'pricing_pending_review') {
    const selectedKws = keywords.filter(kw => selectedIds.has(kw.id));
    return (
      <div className="min-h-[100dvh] pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] bg-linear-to-b from-gray-50 to-white">
        <SelectionHeader brand={brand} brandName={data.brand_name} phase="selection" context={data.selection_context} />
        {/* [SSOT v1.0 §3.3] 被排除的知识/百科类问法逐条可见(内联横幅,兼容 iOS) */}
        {deliveryExcluded.length > 0 && (
          <div className="mx-auto max-w-2xl px-4 pt-4">
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
              <p className="font-medium mb-2">
                以下 {deliveryExcluded.length} 个词属于知识/百科类问法，不会促使 AI 推荐具体品牌，未计入本次交付：
              </p>
              <ul className="space-y-1">
                {deliveryExcluded.map((item, idx) => (
                  <li key={idx} className="break-words">
                    · {item.keyword || ''}
                    {item.reason ? <span className="text-amber-700">（{item.reason}）</span> : null}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
        <WaitingQuote
          selectedKeywords={selectedKws}
          customKeywords={customKeywords}
          onWithdraw={data.status === 'keywords_submitted' ? handleWithdraw : undefined}
          withdrawing={submitting}
          reviewPending={data.status === 'pricing_pending_review'}
        />
      </div>
    );
  }

  // Quoted / adding_keywords - pricing phase
  if (data.status === 'quoted' || data.status === 'adding_keywords') {
    const pricingData = data.pricing_data;
    if (!pricingData && !isClusterMode) return null;

    const tierLabel = TIER_META[activeTier]?.label || '标准版';
    const tier = activeTier as 'entry' | 'standard' | 'flagship';

    return (
      <div className="min-h-[100dvh] pt-[env(safe-area-inset-top)] pb-40 bg-linear-to-b from-gray-50 to-white">
        <SelectionHeader brand={brand} brandName={data.brand_name} phase="pricing" context={data.selection_context} />

        <TierSelector
          tiers={pricingData?.tiers || {}}
          activeTier={activeTier}
          onSelect={handleTierSwitch}
          onHoverStart={handleTierHoverStart}
          onHoverEnd={handleTierHoverEnd}
          dynamicPrices={isClusterMode ? clusterDynamicTierPrices : dynamicTierPrices}
        />

        {/* 数据断供词提示(真实第一:不出价不兜底 · 稍后重新生成报价自动补算) */}
        {(() => {
          const unavailable = (isClusterMode
            ? data.clusters_data?.unavailable_keywords
            : pricingData?.unavailable_keywords) || [];
          if (!unavailable.length) return null;
          return (
            <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 py-2">
              <div className="bg-amber-50 border border-amber-200 rounded-2xl px-5 py-3">
                <p className="text-sm font-medium text-amber-700">
                  ⏳ {unavailable.length} 个词网络繁忙,本次暂时无法估价(未计入报价)
                </p>
                <p className="text-xs text-amber-600 mt-1">
                  {unavailable.slice(0, 5).map(u => u.keyword).join('、')}
                  {unavailable.length > 5 ? ` 等${unavailable.length}个` : ''}
                  · 稍后重新生成报价即可补上这些词
                </p>
              </div>
            </div>
          );
        })()}

        {/* [报价意图闸 2026-08-04] 不建议投放的词只读区(不计价 · 不静默消失 · 附可补的商业词) */}
        <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 py-2 empty:hidden">
          <ExcludedKeywordsPanel
            excluded={pricingData?.excluded_keywords}
            suggested={pricingData?.suggested_keywords}
            gateStatus={pricingData?.policy_gate_status}
          />
        </div>

        {isClusterMode ? (
          <>
            {/* Cluster mode stats bar */}
            <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 py-2">
              <div className="bg-linear-to-r from-blue-50 to-indigo-50 rounded-2xl px-5 py-3 md:py-4 flex items-center justify-between border border-blue-100">
                <div className="flex items-center gap-3">
                  <span className="text-lg">🎯</span>
                  <div>
                    <p className="text-sm md:text-base font-semibold text-gray-700">
                      为您定制了 {clusters.length} 个AI搜索主题包
                    </p>
                    <p className="text-xs text-gray-400 mt-0.5">
                      已选 {clusterSelectedCount}/{clusters.length} 个包 · {clusterTotalCoreCount} 个核心词
                    </p>
                  </div>
                </div>
                <div className="text-right">
                  <p className="text-xs text-gray-400">{tierLabel}总价</p>
                  {quoteBlockingMessage(clusterQuoteSummary) ? (
                    <p className="text-xs md:text-sm font-medium text-amber-600 max-w-[180px]">
                      {quoteSummaryText(clusterQuoteSummary, '合计')}
                    </p>
                  ) : clusterTotalPrice > 0 ? (
                    <p className="text-lg md:text-xl font-bold text-blue-600 tabular-nums">
                      ¥{clusterTotalPrice.toLocaleString()}
                    </p>
                  ) : (
                    <p className="text-sm font-medium text-gray-400">请选择核心词</p>
                  )}
                </div>
              </div>
            </div>

            {/* 全选/取消全选 */}
            <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 pt-2 flex items-center justify-end gap-2">
              <button
                onClick={() => setClusters(prev => prev.map(c => ({ ...c, is_selected: true, core_keywords: c.core_keywords.map(kw => kw.super_red_ocean ? { ...kw, is_selected: false } : { ...kw, is_selected: true }) })))}
                className="text-xs text-blue-600 hover:text-blue-800 hover:underline px-2 py-1"
              >
                全选
              </button>
              <span className="text-gray-300">|</span>
              <button
                onClick={() => setClusters(prev => prev.map(c => ({ ...c, is_selected: false, core_keywords: c.core_keywords.map(kw => ({ ...kw, is_selected: false })) })))}
                className="text-xs text-gray-500 hover:text-gray-700 hover:underline px-2 py-1"
              >
                取消全选
              </button>
            </div>

            {/* Cluster cards */}
            <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 py-2 space-y-3 md:space-y-4">
              {clusters.map((cluster, i) => (
                <ClusterCard
                  key={cluster.cluster_name}
                  cluster={cluster}
                  index={i}
                  activeTier={tier}
                  onToggleCluster={handleToggleCluster}
                  onToggleCoreKeyword={handleToggleCoreKeyword}
                  onUpgradeCovered={handleUpgradeCovered}
                  onDemoteCore={handleDemoteCore}
                />
              ))}
            </div>
          </>
        ) : (
          /* Flat mode (original) */
          <PricingTable
            keywords={pricingData!.keywords}
            activeTier={activeTier}
            checkedIds={checkedPricingIds}
            onToggle={handleTogglePricingKeyword}
            pendingKeywords={data.pending_keywords}
          />
        )}

        {/* [阶段 2 · WO_GAPPLAN_RELOCATION_A] 售前版交付计划。
            落点:紧接词/价格块之后、追加关键词区与 BottomActionBar 之前 ——
            与服务商侧在报价详情里的原落点在客户侧对称。
            🔴 数据随 GET /api/s/:token 一起下发,组件不发请求(客户 token-only 会话
               打 /api/quotes/ 会在 auth 中间件层 401)。
            🔴 actions 恒空、access 相关话术零下发,都由服务端 audience=customer 裁剪。 */}
        <GapPlanPreview plan={data.delivery_plan} />

        {data.status === 'quoted' && (
          <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 py-3">
            <details className="bg-white rounded-2xl border border-gray-100 shadow-xs overflow-hidden">
              <summary className="px-5 md:px-6 py-3.5 md:py-4 text-sm md:text-base text-gray-500 cursor-pointer hover:bg-gray-50 transition-colors">
                还想优化其他关键词？点击展开添加
              </summary>
              <div className="border-t border-gray-100">
                <CustomKeywordInput
                  onAdd={handleAddMoreKeywords}
                  onBatchAdd={handleBatchAddKeywords}
                  placeholder="输入新关键词，多个用逗号或换行分隔"
                />
              </div>
            </details>
          </div>
        )}

        {data.status === 'adding_keywords' && (
          <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 py-3 space-y-3">
            <div className="bg-linear-to-r from-amber-50 to-orange-50 rounded-2xl p-5 md:p-6 border border-amber-100">
              <div className="flex items-center justify-center gap-2 text-sm md:text-base text-amber-700 font-medium mb-3">
                <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
                已提交新关键词，等待顾问重新生成报价...
              </div>
              {(data.pending_keywords?.length ?? 0) > 0 && (
                <div className="bg-white/60 rounded-xl p-3 mt-2">
                  <p className="text-xs text-gray-500 mb-1.5">待处理关键词（{data.pending_keywords?.length ?? 0}个）：</p>
                  <div className="flex flex-wrap gap-1.5">
                    {(data.pending_keywords ?? []).map((kw: string, i: number) => (
                      <span key={i} className="text-xs bg-amber-100 text-amber-700 px-2 py-0.5 rounded-full">{kw}</span>
                    ))}
                  </div>
                </div>
              )}
              <div className="flex items-center justify-center gap-3 mt-3">
                <button
                  onClick={handleCancelAddKeywords}
                  disabled={submitting}
                  className="text-xs text-gray-500 hover:text-gray-700 underline"
                >
                  取消追加，返回报价
                </button>
              </div>
            </div>
            {/* 允许继续添加更多词 */}
            <details className="bg-white rounded-2xl border border-gray-100 shadow-xs overflow-hidden">
              <summary className="px-5 py-3 text-sm text-gray-500 cursor-pointer hover:bg-gray-50 transition-colors">
                继续添加更多关键词
              </summary>
              <div className="border-t border-gray-100">
                <CustomKeywordInput
                  onAdd={handleAddMoreKeywords}
                  onBatchAdd={handleBatchAddKeywords}
                  placeholder="输入新关键词，多个用逗号或换行分隔"
                />
              </div>
            </details>
          </div>
        )}

        <BottomActionBar
          selectedCount={isClusterMode ? clusterTotalCoreCount : checkedPricingIds.size}
          label="确认报价"
          onClick={handleConfirmQuote}
          loading={submitting}
          disabled={data.status !== 'quoted' || !(isClusterMode ? clusterQuoteSummary.canConfirm : pricingQuoteSummary.canConfirm)}
          secondaryInfo={isClusterMode
            ? `${tierLabel} · ${clusterSelectedCount}个主题包 · ${quoteSummaryText(clusterQuoteSummary)} · 累计达标30天`
            : `${tierLabel} · ${quoteSummaryText(pricingQuoteSummary)} · 累计达标30天`
          }
        />
      </div>
    );
  }

  // ========== Business Line Selection (new flow) ==========
  if (data.business_lines?.length) {
    return (
      <div className="min-h-[100dvh] pt-[env(safe-area-inset-top)] pb-40 bg-linear-to-b from-gray-50 to-white">
        <SelectionHeader brand={brand} brandName={data.brand_name} phase="selection" context={data.selection_context} />

        {allExcludedCard}

        {/* Header */}
        <div className="max-w-lg md:max-w-2xl mx-auto px-5 pt-6 pb-3">
          <h2 className="text-lg md:text-xl font-bold text-gray-800 flex items-center gap-1">
            请选择需要推广的业务
            <HelpHint title="为什么选「业务方向」而不是直接选关键词?">
              业务方向 = 你的一类服务 / 产品(比如「豪车配司机」)。选好方向后,
              <b>系统会自动为每个方向匹配该方向下真实会被搜的关键词</b>, 不用你一个个想词。
              <br />选得越准, 后面生成的文章和监测的词就越贴合你的真实业务。
            </HelpHint>
          </h2>
          <p className="text-sm text-gray-500 mt-1.5">
            选择您希望在 AI 搜索中获得推荐的业务方向，我们将为每个业务生成专属优化方案
          </p>
          <div className="flex items-center gap-2 mt-3">
            <span className="text-xs text-gray-400">
              已选 <span className="text-indigo-600 font-semibold text-sm">{selectedBusinessLines.size}</span> / {data.business_lines.length} 项业务
            </span>
            {selectedBusinessLines.size === data.business_lines.length ? (
              <button
                className="text-xs text-gray-400 hover:text-gray-600 underline"
                onClick={() => setSelectedBusinessLines(new Set())}
              >
                取消全选
              </button>
            ) : (
              <button
                className="text-xs text-indigo-500 hover:text-indigo-700 underline"
                onClick={() => setSelectedBusinessLines(new Set(data.business_lines!.map(bl => bl.id)))}
              >
                全选
              </button>
            )}
          </div>
        </div>

        {/* Business line cards */}
        <div className="max-w-lg md:max-w-2xl mx-auto px-5 py-2">
          <div className="flex flex-col gap-3">
            {data.business_lines.map(bl => {
              const isSelected = selectedBusinessLines.has(bl.id);
              return (
                <button
                  key={bl.id}
                  onClick={() => toggleBusinessLine(bl.id)}
                  className={`w-full text-left rounded-2xl border-2 transition-all duration-200 overflow-hidden ${
                    isSelected
                      ? 'border-indigo-500 bg-white shadow-md shadow-indigo-100'
                      : 'border-gray-100 bg-white hover:border-gray-200 hover:shadow-xs'
                  }`}
                >
                  <div className="px-5 py-4 md:px-6 md:py-5">
                    <div className="flex items-start gap-3">
                      {/* Checkbox */}
                      <div className={`w-6 h-6 rounded-lg flex items-center justify-center shrink-0 mt-0.5 transition-colors ${
                        isSelected ? 'bg-indigo-500' : 'bg-gray-100'
                      }`}>
                        {isSelected && (
                          <svg className="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                            <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                          </svg>
                        )}
                      </div>

                      {/* Content */}
                      <div className="flex-1 min-w-0">
                        <h3 className={`text-base md:text-lg font-semibold transition-colors ${
                          isSelected ? 'text-gray-900' : 'text-gray-600'
                        }`}>
                          {bl.name}
                        </h3>
                        {bl.description && (
                          <p className="text-sm text-gray-500 mt-1">{bl.description}</p>
                        )}
                        {bl.example_scenarios?.length > 0 && (
                          <div className="flex flex-wrap gap-1.5 mt-2.5">
                            {bl.example_scenarios.map((s, i) => (
                              <span key={i} className="text-xs px-2 py-0.5 rounded-full bg-gray-50 text-gray-500 border border-gray-100">
                                {s}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* 提交成功提示。
            🔴 [#178] 全排除时这条绿条**必须不挂**:没有人在准备方案(客户补商业问法
               或报价方补词才有下一步)。挂着它比原来的死胡同更有欺骗性 ——
               客户会安心去等一个永远不来的方案。判定走 shouldClaimSubmitted,
               不直接读 businessLinesSubmitted:后者会被紧随其后的 fetchData 重新置 true。 */}
        {shouldClaimSubmitted({
          status: data.status,
          allExcluded: !!allExcluded,
          businessLinesSubmitted,
        }) && (
          <div className="fixed top-0 left-0 right-0 z-50 bg-emerald-500 text-white text-center py-3 px-4 text-sm font-medium shadow-lg">
            ✅ 业务方向已提交！我们正在为您准备专属关键词方案，请稍候...
          </div>
        )}

        {allExcluded ? (
          /* [#178] 全排除 ⇒ 提交键改为提交「新增的商业问法」(打 submit-keywords,
             该端点允许 business_lines_submitted 态),而不是再提交一遍业务方向 ——
             再提交一遍业务方向会拿到一模一样的全排除结果,那才是真的死循环。 */
          <BottomActionBar
            selectedCount={customKeywords.length}
            label="提交新增问法 →"
            onClick={handleSubmitKeywords}
            loading={submitting}
            disabled={submitGate.disabled}
            disabledReason={submitGate.reason}
          />
        ) : (
          <BottomActionBar
            selectedCount={selectedBusinessLines.size}
            label={businessLinesSubmitted ? '✅ 已提交，请等待方案' : '确认业务选择 →'}
            onClick={handleSubmitBusinessLines}
            loading={submitting}
            disabled={selectedBusinessLines.size === 0 || businessLinesSubmitted}
          />
        )}
      </div>
    );
  }

  // ========== Keyword Selection (legacy flow) ==========
  return (
    <div className="min-h-[100dvh] pt-[env(safe-area-inset-top)] pb-40 bg-linear-to-b from-gray-50 to-white">
      <SelectionHeader brand={brand} brandName={data.brand_name} phase="selection" context={data.selection_context} />
      {allExcludedCard}
      <StatsBar selectedCount={selectedIds.size} totalCount={keywords.length} />

      <CategoryTabs
        categories={categories}
        activeCategory={activeCategory}
        onSelect={(cat) => {
          setActiveCategory(cat);
          track({ type: 'category_switch', data: { category: cat } });
        }}
      />

      {/* Keyword grid: 1 col mobile, 2 cols desktop */}
      <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 py-2">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2.5 md:gap-3">
          {filteredKeywords.map(kw => (
            <KeywordCard
              key={kw.id}
              keyword={kw}
              selected={selectedIds.has(kw.id)}
              onToggle={handleToggleKeyword}
              onHoverStart={handleHoverStart}
              onHoverEnd={handleHoverEnd}
            />
          ))}
        </div>
      </div>

      {customKeywords.length > 0 && (
        <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 pt-4">
          <div className="bg-white rounded-2xl border border-gray-100 shadow-xs overflow-hidden">
            <div className="px-5 md:px-6 py-3 md:py-4 border-b border-gray-50">
              <h4 className="text-sm md:text-base font-semibold text-gray-700">自定义关键词</h4>
              <p className="text-xs md:text-sm text-gray-400 mt-0.5">共 {customKeywords.length} 个</p>
            </div>
            <div className="divide-y divide-gray-50">
              {customKeywords.map((kw, i) => (
                <div key={i} className="flex items-center gap-3 px-5 md:px-6 py-3 md:py-3.5">
                  <span className="w-5 h-5 rounded-full bg-amber-50 text-amber-500 flex items-center justify-center shrink-0">
                    <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
                    </svg>
                  </span>
                  <span className="text-sm md:text-base text-gray-700 flex-1">{kw}</span>
                  <span className="text-xs text-amber-500 bg-amber-50 px-2 py-0.5 rounded-full">自定义</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      <CustomKeywordInput onAdd={handleAddCustom} />

      {/* WO_329 开源版署名位:页面根节点已留 pb-40,不被底部固定操作栏盖住;开关关 ⇒ 不渲染 */}
      <OssAttribution />

      <BottomActionBar
        selectedCount={selectedIds.size + customKeywords.length}
        label="确认关键词 →"
        onClick={handleSubmitKeywords}
        loading={submitting}
        disabled={submitGate.disabled}
        disabledReason={submitGate.reason}
      />
    </div>
  );
}
