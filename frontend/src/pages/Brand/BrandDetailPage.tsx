/**
 * 统一品牌/客户详情页 — v4
 * mode='self' → 我的品牌
 * mode='client' → 客户详情
 * 同一个组件，通过 mode 控制标题和按钮
 */
import { useState, useEffect, useRef, useCallback } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
// [包H · U-9] 「发给客户」统一面板(四类 token 链接 + 人话前缀 + 一键重签发)。
import { CustomerLinksPanel } from '@/components/defensiveGeo/CustomerLinksPanel';
import { useEmbeddedNavigate } from '@/hooks/useEmbeddedNavigate';
import { ArrowLeft, Save, Trash2, Sparkles, RefreshCw, QrCode, MessageCircle, BookMarked, ChevronDown, ChevronUp, Link2, Copy, ExternalLink, Loader2, Send, ShieldCheck, WandSparkles } from 'lucide-react';
import { authApi } from '@/context/AuthContext';
import { useClientContext } from '@/context/ClientContext';
import { useUnsavedWarning } from '@/hooks/useUnsavedWarning';
import { cn } from '@/lib/utils';
/* [WO_253 P0] 保存按落库事实说话 / 服务期提示出声+出口 —— 口径各收一处。 */
import { saveFeedback } from '@/lib/saveFeedback';
import { noticeView, parseServiceNotice } from '@/lib/serviceNotice';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Separator } from '@/components/ui/separator';
import { SharePosterDialog } from '@/components/share/SharePosterDialog';
// [WO_260] 原 import clearCEndBrandCache(C 端抽屉的 sessionStorage 缓存键):只有废弃的 C 端抽屉读它,
//   在役页清它无可见效果,却把整个 C 端抽屉(6 条 /c/* 链接)拖进在役闭包 —— 三处调用一并删。
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { Badge } from '@/components/ui/badge';
import { IndustryBriefReviewCard } from '@/components/brand/IndustryBriefReviewCard';
import { IndustryCategorySelect } from '@/components/brand/IndustryCategorySelect';
// CTO-13.0 2026-04-19 S0.2: AI 智能填充抽成独立组件
import { AiFillDialog } from '@/components/brand/AiFillDialog';
// CTO-13.0 2026-04-19 S2.1: 5 维度业务画像编辑器
import { StructuredKnowledgeEditor } from '@/components/brand/StructuredKnowledgeEditor';
// 2026-06-02 GEO CTO:资料中心「图片素材」区(上传客户图片 → AI 识别 → 写作自动配图)
import { BrandImageGallery } from '@/components/brand/BrandImageGallery';
import { MarketInsightCard } from '@/components/brand/MarketInsightCard';
import { BrandWizardBanner } from '@/components/brand/BrandWizardBanner';
import type { AiFilledFields } from '@/components/brand/AiFillDialog';
// CTO-13.0 2026-04-19 S1.5: AI 顾问润色按钮
import { PolishButton } from '@/components/brand/PolishButton';
// v1_3 (CTO-15.1 2026-04-19): 动态价目表 + 大额 toast 确认
import { usePricing } from '@/context/PricingContext';
import {
  PRICING_UNAVAILABLE_LABEL, PricingUnavailableHint,
} from '@/components/pricing/PricingUnavailableHint';
import { useConfirmLargeDeduction } from '@/hooks/useConfirmLargeDeduction';
// CTO-15.21 v5 (2026-04-28 老板拍板) M3 废弃 · 蒸馏 M3 M3 客户工作台 8 区精华到 BrandDetailPage(client mode)
//   - DecisionBarBridge:决策条 + stage 主按钮 + 推荐下一步(拉 m3Api.getClientWorkbench)
//   - ToolGridCard:11 工具卡(看诊断/方案/选词/监测/写作/发布/月报/档案/门户/日志)
import { DecisionBarBridge } from '@/components/workbench/DecisionBarBridge';
import { ToolGridCard } from '@/components/workbench/ToolGrid';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { MaterialConfirmStatusBadge } from '@/components/workbench/MaterialConfirmStatusBadge';
import { buildWechatScript, materialConfirmApi, type ClientWorkbenchSnapshot, type MaterialConfirmStatusResponse, type MaterialsSummary } from '@/services/m3';
import { useMarkStepCompleted } from '@/hooks/useMarkStepCompleted';
import { useOrganization } from '@/context/OrganizationContext';

interface BrandDetailPageProps {
  overrideBrandId?: number;
  mode?: 'self' | 'client' | 'c-end';  // S3.2: c-end = C 端普通用户（/s/my-ip 复用此页）
}

// [2026-06-01 GEO 社媒遗产清除] GEO(self/client)只保留资料/诊断/询价类 tab;
// c-end 为兼容分支:当前 /s/* 已走 社媒工作台主页面,本页 c-end 非活跃路径(无人传 mode='c-end'),
// 仅保留其全 tab 定义不动,避免触碰社媒 CTO-13.0 territory(未作为活跃路径验收)
function detailTabsFor(mode: string): string[] {
  // [P0 客户资料中心] knowledge 拆为「业务事实 facts / 资料文件 files / 客户确认 confirm」3 区
  // c-end 兼容分支:facts/files 承接原 knowledge 内容;persona/content/corpus 社媒保留(非活跃路径)
  if (mode === 'c-end') return ['basic', 'facts', 'files', 'persona', 'content', 'diagnosis', 'corpus', 'interactions'];
  if (mode === 'self') return ['basic', 'facts', 'files'];
  return ['basic', 'facts', 'files', 'confirm', 'diagnosis', 'interactions'];  // client(4 资料区 + 诊断/询价记录)
}

function normalizeDetailTab(tab: string | null, mode: string): string {
  // [2026-06-02 兼容] 旧链接 ?tab=knowledge 已拆为 facts/files/confirm → 映射到 facts(业务事实·原 knowledge 主体),避免旧书签/外链跳回 basic
  if (tab === 'knowledge') tab = 'facts';
  const allowed = detailTabsFor(mode);
  return tab && allowed.includes(tab) ? tab : 'basic';
}

function parseProfileValue(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  const trimmed = value.trim();
  if (!trimmed) return '';
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return value;
  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

const PROFILE_VALUE_LABELS: Record<string, string> = {
  client: '客户',
  customer: '客户',
  background: '背景',
  context: '背景',
  challenge: '问题',
  problem: '问题',
  pain: '痛点',
  need: '需求',
  solution: '方案',
  result: '结果',
  outcome: '结果',
  effect: '效果',
  quote: '原话',
  testimonial: '评价',
  title: '身份',
  company: '公司',
  name: '名称',
  product: '产品',
  service: '服务',
  point: '要点',
  evidence: '依据',
};

const PROFILE_VALUE_ORDER = [
  'client', 'customer', 'title', 'company', 'name', 'background', 'context',
  'challenge', 'problem', 'pain', 'need', 'solution', 'result', 'outcome',
  'effect', 'quote', 'testimonial', 'product', 'service', 'point', 'evidence',
];

function profileItemText(value: unknown): string {
  const parsed = parseProfileValue(value);
  if (parsed === null || parsed === undefined) return '';
  if (typeof parsed === 'string') return parsed.trim();
  if (typeof parsed === 'number' || typeof parsed === 'boolean') return String(parsed);
  if (Array.isArray(parsed)) return parsed.map(profileItemText).filter(Boolean).join('、');
  if (typeof parsed === 'object') {
    const obj = parsed as Record<string, unknown>;
    const used = new Set<string>();
    const parts: string[] = [];
    for (const key of PROFILE_VALUE_ORDER) {
      if (!(key in obj)) continue;
      const text = profileItemText(obj[key]);
      if (!text) continue;
      used.add(key);
      parts.push(`${PROFILE_VALUE_LABELS[key] || key}: ${text}`);
    }
    if (parts.length) return parts.join('；');
    return Object.entries(obj)
      .filter(([key]) => !used.has(key))
      .map(([key, entryValue]) => {
        const text = profileItemText(entryValue);
        return text ? `${PROFILE_VALUE_LABELS[key] || key}: ${text}` : '';
      })
      .filter(Boolean)
      .join('；');
  }
  return '';
}

function profileText(value: unknown): string {
  const parsed = parseProfileValue(value);
  if (parsed === null || parsed === undefined) return '';
  if (typeof parsed === 'string') return parsed.trim();
  if (Array.isArray(parsed)) return parsed.map(profileItemText).filter(Boolean).join('\n');
  return profileItemText(parsed);
}

function brandDisplayNamesText(value: unknown): string {
  const parsed = parseProfileValue(value);
  if (Array.isArray(parsed)) {
    return parsed.filter((item): item is string => typeof item === 'string').join('\n');
  }
  if (typeof parsed !== 'string') return '';
  return parsed.split(/[\n,，;；]+/).map(item => item.trim()).filter(Boolean).join('\n');
}

function brandDisplayNamesPayload(value: string): string[] {
  return Array.from(new Set(
    value.split(/[\n,，;；]+/).map(item => item.trim()).filter(Boolean),
  ));
}

function parseStructuredKnowledge(value: unknown): Record<string, unknown> | null {
  const parsed = parseProfileValue(value);
  if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
    return parsed as Record<string, unknown>;
  }
  return null;
}

function profileFieldWithFallback(
  primary: unknown,
  structuredKnowledge: Record<string, unknown> | null,
  keys: string[],
): string {
  const primaryText = profileText(primary);
  if (primaryText) return primaryText;
  if (!structuredKnowledge) return '';
  for (const key of keys) {
    const fallback = profileText(structuredKnowledge[key]);
    if (fallback) return fallback;
  }
  return '';
}

export default function BrandDetailPage({ overrideBrandId, mode: modeOverride }: BrandDetailPageProps = {}) {
  const params = useParams();
  const [searchParams] = useSearchParams();
  const initialTabParam = searchParams.get('tab');
  const navigate = useEmbeddedNavigate();
  const { refreshClients, switchClient, currentBrandId } = useClientContext();
  const { isMember } = useOrganization();
  // 原生 confirm() 在部分 WebKit 会话里返回 false 而对话框根本不弹(生产实证:用户点 9 次
  // 归档、38 次 archive-preview 全 200,DELETE 一次都没发出)。一律走应用内弹窗。
  const [confirmDialog, askConfirm] = useConfirmDialog();
  const brandId = overrideBrandId || Number(params.id);
  const mode = modeOverride || 'client';
  // S3.2: c-end 和 self 数据源都是"我自己"，走 /api/my-brand；只有 client 走 /my-clients/:id
  const isSelfOrCend = mode === 'self' || mode === 'c-end';
  // [2026-06-03 一页式重排] (mode as string) 故意不做字面量类型守卫:c-end 兼容分支里原 Tabs 代码仍用
  // mode === 'client' / mode !== 'self' 判定,若 isCEnd 作守卫会把 mode 收窄成 'c-end' 触发 TS2367
  const isCEnd = (mode as string) === 'c-end';
  const { getTrustedCost, loading: pricingLoading, error: pricingError,
    refresh: pricingRefresh, retryNotBefore: pricingRetryNotBefore } = usePricing();
  const deepAnalyzeCost = getTrustedCost('deep_analyze');
  const confirmLarge = useConfirmLargeDeduction(1000);
  const markStep = useMarkStepCompleted();

  const [brand, setBrand] = useState<any>(null);
  /** [WO_267] 行业大类下拉:只有用户动过才进保存请求(空串 = 清空、任何 key = 人工选定) */
  const [categoryPick, setCategoryPick] = useState<{ touched: boolean; key: string }>({ touched: false, key: '' });
  const [profile, setProfile] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  /* [WO_253 ①] 保存成功后 +1,触发详情 effect 重拉 —— 回显**落库值**而不是本地表单值。 */
  const [detailNonce, setDetailNonce] = useState(0);
  const [saving, setSaving] = useState(false);
  const [showSharePoster, setShowSharePoster] = useState(false);

  // 表单状态
  const [form, setForm] = useState({
    name: '', industry: '', company_name: '', brand_display_names: '', cities: '',
    business: '', target_users: '', persona_positioning: '', persona_tone: '',
    // [CTO-13.0 2026-04-19 S1.1] ai-fill 深度营销字段
    company_intro: '', core_value: '', selling_points: '',
    success_cases: '', testimonials: '',
    // [2026-06-02 GEO CTO] 联系方式 4 字段(写作引流·媒体发布自动软化)
    contact_phone: '', contact_wechat: '', contact_website: '', contact_address: '',
  });
  const [products, setProducts] = useState<string[]>([]);
  const [painPoints, setPainPoints] = useState<string[]>([]);
  const [competitors, setCompetitors] = useState<string[]>([]);
  const [structuredKnowledge, setStructuredKnowledge] = useState<Record<string, unknown> | null>(null);

  // [CTO-15.9 2026-04-25 M1c T4] 市场洞察 E 组字段(对齐 brand_completeness market_insight 10 分)
  const [serviceScope, setServiceScope] = useState<string>('');  // local / national / hybrid
  const [localCompetitors, setLocalCompetitors] = useState<string[]>([]);
  const [marketInsight, setMarketInsight] = useState<Record<string, unknown>>({});  // {authority_sources, hot_formats, my_differentiation}
  // [CTO-15.9 M1b M5] business_type + city_scope 选器(驱动 keyword_expander 4 addon 分支)
  const [businessType, setBusinessType] = useState<string>('B2C');  // B2C / B2B / 政企
  const [cityScope, setCityScope] = useState<string>('local');  // local / national

  // [CTO-13.0 2026-04-19 S1.3] AI 填过字段高亮 Set · 用户手动改或保存后清空
  const [aiFilledFields, setAiFilledFields] = useState<Set<string>>(new Set());

  // Tab 状态
  const [activeTab, setActiveTab] = useState(() => normalizeDetailTab(initialTabParam, mode));
  const [workbenchSnapshot, setWorkbenchSnapshot] = useState<ClientWorkbenchSnapshot | null>(null);

  useEffect(() => {
    const nextTab = normalizeDetailTab(initialTabParam, mode);
    setActiveTab(prev => (prev === nextTab ? prev : nextTab));
  }, [initialTabParam, mode]);

  // 深度解析
  const [deepAnalyzing, setDeepAnalyzing] = useState(false);

  // [2026-06-03 一页式重排] 「让 AI 整理客户资料」一键整理:把基础信息/上传文件/图片梳理成统一写作资料
  const [organizing, setOrganizing] = useState(false);
  // [2026-06-04 一页式打磨] 基础信息「AI 帮填」默认收起 · 点卡头按钮展开(对齐图3 紧凑卡头)
  const [aiFillOpen, setAiFillOpen] = useState(false);

  // AI 填充：state / fillingRef / SimulatedThinking 已搬到 AiFillDialog 子组件（S0.2）
  // 主组件只保留 initialFormRef（脏数据检测）和 onAiFilled 回调
  const initialFormRef = useRef('');
  useEffect(() => { if (brand) initialFormRef.current = JSON.stringify(form); }, [brand]);
  useUnsavedWarning(!!brand && JSON.stringify(form) !== initialFormRef.current);

  useEffect(() => {
    setWorkbenchSnapshot(null);
  }, [brandId]);

  // 🆕 BUG-P0-4 admin 兜底 + BUG-P2-6 全局 selector 跟 URL 切换 (CTO-15.22 2026-05-03)
  // admin 默认 isAllClientsMode=true · currentBrandId=null · 进客户详情时上下文未切
  // → /diagnosis/new 等下游页 useClientContext 读不到当前客户 · prefill 失效 · selector 显示残留客户
  // 修法:client mode 进入详情页时自动 switchClient(brandId) · 让全局上下文跟 URL · 不影响 self/c-end 模式
  useEffect(() => {
    if (mode === 'client' && brandId > 0 && currentBrandId !== brandId) {
      switchClient(brandId);
    }
  }, [brandId, mode, currentBrandId, switchClient]);

  // 客户档案完整度 >= 80 时,自动勾选"补齐客户档案"步骤(任务 4 钩子)
  useEffect(() => {
    const completeness = Number(brand?.completeness ?? 0);
    if (completeness >= 80) {
      markStep('fill_client_profile');
    }
  }, [brand?.completeness, markStep]);

  // AiFillDialog 回调：AI 返回后更新表单
  // auto-fill (旧): brand_name/industry/city/business/target_users/persona_*/products/pain_points
  // ai-fill  (新 S1.1): industry/business/target_users/pain_points/competitors/
  //                    company_intro/core_value/selling_points/success_cases/testimonials/
  //                    structured_knowledge
  // S1.3: 收集被 AI 填写的字段 key 写进 aiFilledFields Set 用于高亮
  // B5 (CTO-15.9 session 3 · 2026-04-25 · M1c §A.6) ai-fill 后 LLM/启发式置信度
  const [aiConfidences, setAiConfidences] = useState<Record<string, 'strong' | 'medium' | 'weak'>>({});
  const handleAiFilled = useCallback((d: AiFilledFields) => {
    const filled = new Set<string>();
    // B5 · 把 ai-fill 返的 confidences 缓存到 state · ConfidenceBadge 字段右侧渲染
    if (d._confidences && typeof d._confidences === 'object') {
      setAiConfidences(d._confidences);
    }
    // 字符串字段映射表（dataKey → formKey）
    const strMap: Record<string, keyof typeof form> = {
      brand_name: 'name',
      industry: 'industry',
      city: 'cities',
      business: 'business',
      target_users: 'target_users',
      persona_positioning: 'persona_positioning',
      persona_tone: 'persona_tone',
      company_intro: 'company_intro',
      core_value: 'core_value',
      selling_points: 'selling_points',
      success_cases: 'success_cases',
      testimonials: 'testimonials',
    };
    setForm(f => {
      const next = { ...f };
      for (const [dk, fk] of Object.entries(strMap)) {
        const v = d[dk];
        if (typeof v === 'string' && v.trim()) {
          (next as any)[fk] = v;
          filled.add(fk);
        }
      }
      // brand_name 同时同步 company_name（原逻辑保留）
      if (typeof d.brand_name === 'string' && d.brand_name.trim()) {
        next.company_name = d.brand_name;
        filled.add('company_name');
      }
      return next;
    });
    if (Array.isArray(d.products) && d.products.length) { setProducts(d.products as string[]); filled.add('products'); }
    if (Array.isArray(d.pain_points) && d.pain_points.length) { setPainPoints(d.pain_points as string[]); filled.add('pain_points'); }
    if (Array.isArray(d.competitors) && d.competitors.length) { setCompetitors(d.competitors as string[]); filled.add('competitors'); }
    if (d.structured_knowledge && typeof d.structured_knowledge === 'object') {
      setStructuredKnowledge(d.structured_knowledge as Record<string, unknown>);
      filled.add('structured_knowledge');
    }
    // [CTO-15.9 M1c T4] 消费新 3 键:service_scope / local_competitors / market_insight
    if (typeof d.service_scope === 'string' && ['local', 'national', 'hybrid'].includes(d.service_scope)) {
      setServiceScope(d.service_scope);
      filled.add('service_scope');
    }
    if (Array.isArray(d.local_competitors) && d.local_competitors.length) {
      setLocalCompetitors(d.local_competitors as string[]);
      filled.add('local_competitors');
    }
    if (d.market_insight && typeof d.market_insight === 'object' && !Array.isArray(d.market_insight)) {
      setMarketInsight(d.market_insight as Record<string, unknown>);
      filled.add('market_insight');
    }
    // [CTO-15.9 M1b M5] business_type + city_scope(ai-fill 或对话式补齐都可能返)
    if (typeof d.business_type === 'string' && ['B2C', 'B2B', '政企'].includes(d.business_type)) {
      setBusinessType(d.business_type);
      filled.add('business_type');
    }
    if (typeof d.city_scope === 'string' && ['local', 'national'].includes(d.city_scope)) {
      setCityScope(d.city_scope);
      filled.add('city_scope');
    }
    setAiFilledFields(filled);  // 覆盖旧的（每次 AI 填充刷新高亮）
  }, []);

  // 用户手动改某字段 → 从高亮 Set 移除（表示已 review）
  const clearAiHighlight = useCallback((key: string) => {
    setAiFilledFields(prev => {
      if (!prev.has(key)) return prev;
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
  }, []);

  // Agent 填充事件监听
  useEffect(() => {
    const handler = (e: Event) => {
      const fields = (e as CustomEvent).detail;
      if (!fields || typeof fields !== 'object') return;
      setForm(prev => {
        const updated = { ...prev };
        if (fields.name) updated.name = fields.name;
        if (fields.industry) updated.industry = fields.industry;
        if (fields.business) updated.business = fields.business;
        if (fields.target_users) updated.target_users = fields.target_users;
        return updated;
      });
      if (fields.products && Array.isArray(fields.products)) setProducts(fields.products);
      if (fields.pain_points && Array.isArray(fields.pain_points)) setPainPoints(fields.pain_points);
    };
    window.addEventListener('agent-fill', handler);
    return () => window.removeEventListener('agent-fill', handler);
  }, []);

  // [CTO-13.0 2026-04-19 S1.4] brief 一键应用事件监听 → 设 aiFilledFields 触发 S1.3 高亮
  // ReviewCard dispatchEvent('brief-applied', { fields: [...] }) 后触发（ReviewCard 里 reload 已同步）
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (!detail?.fields || !Array.isArray(detail.fields)) return;
      const filled = new Set<string>(detail.fields);
      setAiFilledFields(filled);
    };
    window.addEventListener('brief-applied', handler);
    return () => window.removeEventListener('brief-applied', handler);
  }, []);

  // 加载详情（提取为 useCallback 供轮询复用，但也保留首次加载用 useEffect 触发）
  const reloadProfile = useCallback(async () => {
    if (!brandId) return;
    const endpoint = isSelfOrCend ? '/api/my-brand' : `/api/my-clients/${brandId}`;
    try {
      const res = await authApi.get(endpoint);
      const b = res.data.brand;
      const p = res.data.profile;
      setBrand(b);
      setCategoryPick({ touched: false, key: '' });
      setProfile(p);
      if (b) {
        const sk = parseStructuredKnowledge(p?.structured_knowledge);
        setForm({
          name: b.name || '', industry: b.industry || '',
          company_name: b.company_name || '', cities: b.cities || '',
          brand_display_names: brandDisplayNamesText(b.brand_display_names ?? p?.brand_display_names),
          business: profileText(p?.business), target_users: profileText(p?.target_users),
          persona_positioning: profileText(p?.persona_positioning),
          persona_tone: profileText(p?.persona_tone),
          // [CTO-13.0 2026-04-19 S1.1] 深度营销字段从 profile 读
          company_intro: profileFieldWithFallback(p?.company_intro, sk, ['company_intro', 'intro', 'brand_intro']),
          core_value: profileFieldWithFallback(p?.core_value, sk, ['core_value', 'unique_value', 'differentiation']),
          selling_points: profileFieldWithFallback(p?.selling_points, sk, ['selling_points', 'core_selling_points', 'advantages']),
          success_cases: profileFieldWithFallback(p?.success_cases, sk, ['success_cases', 'cases', 'case_studies']),
          testimonials: profileFieldWithFallback(p?.testimonials, sk, ['testimonials', 'reviews', 'customer_reviews']),
          contact_phone: profileText(p?.contact_phone),
          contact_wechat: profileText(p?.contact_wechat),
          contact_website: profileText(p?.contact_website),
          contact_address: profileText(p?.contact_address),
        });
        try { setProducts(JSON.parse(p?.products || '[]')); } catch { setProducts([]); }
        try { setPainPoints(JSON.parse(p?.pain_points || '[]')); } catch { setPainPoints([]); }
        try { setCompetitors(JSON.parse(p?.competitors || '[]')); } catch { setCompetitors([]); }
        setStructuredKnowledge(sk || null);
        // [CTO-15.9 M1c T4] 市场洞察 E 组字段加载
        setServiceScope(p?.service_scope || '');
        try {
          const lc = typeof p?.local_competitors === 'string'
            ? JSON.parse(p.local_competitors || '[]')
            : (p?.local_competitors || []);
          setLocalCompetitors(Array.isArray(lc) ? lc : []);
        } catch { setLocalCompetitors([]); }
        // market_insight flatten 自 industry_brief JSONB(authority_sources/hot_formats/my_differentiation)
        try {
          const brief = typeof p?.industry_brief === 'string'
            ? JSON.parse(p.industry_brief || '{}')
            : (p?.industry_brief || {});
          const mi: Record<string, unknown> = {};
          if (brief?.authority_sources) mi.authority_sources = brief.authority_sources;
          if (brief?.hot_formats) mi.hot_formats = brief.hot_formats;
          if (brief?.my_differentiation) mi.my_differentiation = brief.my_differentiation;
          setMarketInsight(mi);
        } catch { setMarketInsight({}); }
        // [CTO-15.9 M1b M5] business_type + city_scope
        setBusinessType(p?.business_type || 'B2C');
        setCityScope(p?.city_scope || 'local');
      }
    } catch {
      // 轮询失败静默，避免打扰用户
    }
  }, [brandId, mode]);

  useEffect(() => {
    if (!brandId) return;
    setLoading(true);
    const endpoint = isSelfOrCend ? '/api/my-brand' : `/api/my-clients/${brandId}`;
    const loadDetail = (retry = true) => {
      authApi.get(endpoint).then(res => {
        const b = res.data.brand;
        const p = res.data.profile;
        setBrand(b);
        setCategoryPick({ touched: false, key: '' });
        setProfile(p);
        if (b) {
          const sk = parseStructuredKnowledge(p?.structured_knowledge);
          setForm({
            name: b.name || '', industry: b.industry || '',
            company_name: b.company_name || '', cities: b.cities || '',
            brand_display_names: brandDisplayNamesText(b.brand_display_names ?? p?.brand_display_names),
            business: profileText(p?.business), target_users: profileText(p?.target_users),
            persona_positioning: profileText(p?.persona_positioning),
            persona_tone: profileText(p?.persona_tone),
            company_intro: profileFieldWithFallback(p?.company_intro, sk, ['company_intro', 'intro', 'brand_intro']),
            core_value: profileFieldWithFallback(p?.core_value, sk, ['core_value', 'unique_value', 'differentiation']),
            selling_points: profileFieldWithFallback(p?.selling_points, sk, ['selling_points', 'core_selling_points', 'advantages']),
            success_cases: profileFieldWithFallback(p?.success_cases, sk, ['success_cases', 'cases', 'case_studies']),
            testimonials: profileFieldWithFallback(p?.testimonials, sk, ['testimonials', 'reviews', 'customer_reviews']),
            contact_phone: profileText(p?.contact_phone),
            contact_wechat: profileText(p?.contact_wechat),
            contact_website: profileText(p?.contact_website),
            contact_address: profileText(p?.contact_address),
          });
          try { setProducts(JSON.parse(p?.products || '[]')); } catch { setProducts([]); }
          try { setPainPoints(JSON.parse(p?.pain_points || '[]')); } catch { setPainPoints([]); }
          try { setCompetitors(JSON.parse(p?.competitors || '[]')); } catch { setCompetitors([]); }
          setStructuredKnowledge(sk || null);
          // [CTO-15.9 M1c T4] 市场洞察 E 组字段加载(首次加载)
          setServiceScope(p?.service_scope || '');
          try {
            const lc = typeof p?.local_competitors === 'string'
              ? JSON.parse(p.local_competitors || '[]')
              : (p?.local_competitors || []);
            setLocalCompetitors(Array.isArray(lc) ? lc : []);
          } catch { setLocalCompetitors([]); }
          try {
            const brief = typeof p?.industry_brief === 'string'
              ? JSON.parse(p.industry_brief || '{}')
              : (p?.industry_brief || {});
            const mi: Record<string, unknown> = {};
            if (brief?.authority_sources) mi.authority_sources = brief.authority_sources;
            if (brief?.hot_formats) mi.hot_formats = brief.hot_formats;
            if (brief?.my_differentiation) mi.my_differentiation = brief.my_differentiation;
            setMarketInsight(mi);
          } catch { setMarketInsight({}); }
          // [CTO-15.9 M1b M5] business_type + city_scope
          setBusinessType(p?.business_type || 'B2C');
          setCityScope(p?.city_scope || 'local');
        }
      }).catch((err) => {
        const status = err?.response?.status;
        if (status === 404) {
          toast.error('该客户已删除或不存在');
          // S3.2: self 回首页,client 回客户列表
          // [WO_260] 原先 client 模式跳的是社媒客户列表 /s/clients(在役页的 bug),c-end 跳社媒首页;
          //   c-end 无调用方(见文件头),改为 client / c-end 都回在役的「我的客户」。
          navigate(mode === 'self' ? '/' : '/my-clients');
          return;
        }
        if (retry) {
          setTimeout(() => loadDetail(false), 500);
        } else {
          toast.error('加载失败');
        }
      }).finally(() => setLoading(false));
    };
    loadDetail();
  /* 🔴 [WO_253 ①] `detailNonce` 进依赖:保存成功后由保存处 +1,触发这只 effect 重拉详情,
     从而**回显落库值**。不把 `loadDetail` 提升出去 —— 那是更大的改动面,
     而本单只需要"再拉一次"这一个能力。 */
  }, [brandId, mode, detailNonce]);

  // 深度解析状态计算 + 自动轮询
  const briefStatus: string = profile?.industry_brief_status || 'idle';
  const briefStartedAt: string | null = profile?.industry_brief_started_at || null;
  // 后端用 datetime.now() 存本地时间（无时区后缀），前端直接按本地时间解析
  const briefElapsedMin = (() => {
    if (!briefStartedAt) return 0;
    const started = new Date(briefStartedAt).getTime();
    if (isNaN(started)) return 0;
    return Math.max(0, Math.floor((Date.now() - started) / 60000));
  })();
  // running / collecting_l1 / collecting_l2 / collecting_l3 都视为运行中
  const runningStatuses = ['running', 'collecting_l1', 'collecting_l2', 'collecting_l3'];
  // 阈值 15min 与后端 watchdog 兜底窗口一致：在 watchdog 触发之前给用户保留"运行中"视觉
  const briefIsRunning = runningStatuses.includes(briefStatus) && briefElapsedMin < 15;
  const briefIsTimeout = runningStatuses.includes(briefStatus) && briefElapsedMin >= 15;
  const briefIsDone = briefStatus === 'done';
  const briefIsFailed = briefStatus === 'failed';
  const briefStageLabel =
    briefStatus === 'collecting_l1' ? '采集行业知识' :
    briefStatus === 'collecting_l2' ? '采集品类知识' :
    briefStatus === 'collecting_l3' ? '本地市场分析' :
    'AI 收集中';

  // 运行中时每 30s 轮询一次，直到 done/failed/超时
  useEffect(() => {
    if (!briefIsRunning) return;
    // 后端已进入 running 状态，释放 deepAnalyzing 让按钮显示"收集中"
    if (deepAnalyzing) setDeepAnalyzing(false);
    const timer = setInterval(() => {
      reloadProfile();
    }, 30000);
    return () => clearInterval(timer);
  }, [briefIsRunning, reloadProfile, deepAnalyzing]);

  const handleSave = async () => {
    setSaving(true);
    const endpoint = isSelfOrCend ? '/api/my-brand' : `/api/my-clients/${brandId}`;
    // 只发送 BrandInfoUpdate 模型定义的字段，防止多余字段导致 422
    const payload: Record<string, any> = {
      name: form.name,
      industry: form.industry,
      company_name: form.company_name,
      brand_display_names: brandDisplayNamesPayload(form.brand_display_names),
      cities: form.cities,
      business: form.business,
      target_users: form.target_users,
      persona_positioning: form.persona_positioning,
      persona_tone: form.persona_tone,
      products: Array.isArray(products) ? products : [],
      pain_points: Array.isArray(painPoints) ? painPoints : [],
      competitors: Array.isArray(competitors) ? competitors : [],
      // [CTO-13.0 2026-04-19 S1.1] 深度营销字段落库
      company_intro: form.company_intro,
      core_value: form.core_value,
      selling_points: form.selling_points,
      success_cases: form.success_cases,
      testimonials: form.testimonials,
      // [2026-06-02 GEO CTO] 联系方式 4 字段
      contact_phone: form.contact_phone,
      contact_wechat: form.contact_wechat,
      contact_website: form.contact_website,
      contact_address: form.contact_address,
      // structured_knowledge 5 维度嵌套（ai-fill 填充；S2.x 提供 UI 编辑；保存时原样回写）
      structured_knowledge: structuredKnowledge,
      // [CTO-15.9 M1c T4] 市场洞察 E 组字段落库
      service_scope: serviceScope || undefined,
      local_competitors: localCompetitors.length > 0 ? localCompetitors : undefined,
      market_insight: Object.keys(marketInsight).length > 0 ? marketInsight : undefined,
      // [CTO-15.9 M1b M5] business_type + city_scope(驱动 keyword_expander 4 addon 分支)
      business_type: businessType || undefined,
      city_scope: cityScope || undefined,
    };
    if (categoryPick.touched) payload.industry_category = categoryPick.key;  // [WO_267] 动过才带
    try {
      const putRes = await authApi.put(endpoint, payload);
      initialFormRef.current = JSON.stringify(form);  // 清除 dirty 状态
      setAiFilledFields(new Set());  // S1.3: 保存后清空 AI 高亮
      await refreshClients();
      /*
       * 🔴 [WO_253 ① P0] **按落库事实说话,不按"请求没抛错"说话。**
       *    原来这里是无条件 `toast.success('已保存')` —— 提交了联系方式而后端没落库时,
       *    用户看到的是**绿色的「已保存」**(Owner 三图之一)。
       *    「请求成功」与「字段落库」是两件事:200 只说明这次调用被受理了。
       *    口径收在 `@/lib/saveFeedback` 一处,含三态(键不存在 = 后端还不报告 ⇒ 保持原行为)。
       */
      const fb = saveFeedback(payload as Record<string, unknown>, putRes?.data as Record<string, unknown>);
      if (fb.kind === 'missing') toast.error(fb.message);
      else toast.success(fb.message);
      /* 落库了就**重新拉详情回显落库值**,不拿本地表单值假装。 */
      if (fb.shouldReload) setDetailNonce((n) => n + 1);
    } catch (e: any) {
      // v1_3 (CTO-15.1 2026-04-19) 老板反馈：原 toast 只显示"保存失败"没原因
      // 现在把 HTTP 状态码 + detail 全暴露，方便诊断
      const status = e.response?.status;
      const detail = e.response?.data?.detail;
      const msg = e.response?.data?.message;
      let errorText = '保存失败';
      if (typeof detail === 'string') errorText = detail;
      else if (typeof msg === 'string') errorText = msg;
      else if (Array.isArray(detail)) {
        // Pydantic 422 错误数组（字段校验错）
        const fields = detail.map((d: any) => d?.loc?.join('.') || '?').join(', ');
        errorText = `字段校验失败: ${fields}`;
      }
      if (status) errorText = `[${status}] ${errorText}`;
      toast.error(errorText, { duration: 8000 });
      console.error('[Save error]', {
        status,
        data: e.response?.data,
        endpoint,
        payload,
      });
    }
    setSaving(false);
  };

  // 深度解析：调用 /deep-analyze 端点，后台异步（静默扣费 · 页面不前置告知额度）
  // 状态持久化在 profile.industry_brief_status，刷新/重进页面依然能看到运行中状态
  const handleDeepAnalyze = async () => {
    if (deepAnalyzing || briefIsRunning) return;
    if (deepAnalyzeCost == null) {
      toast.error(pricingError || '动态价目尚未确认，本次没有发起深度解析');
      return;
    }
    let profileId: number | string | undefined = profile?.id;

    // v3.8 CTO-15.0 UX 修：一键体验 — profile 未建 OR form 有 dirty 内容都先静默保存
    // 老板反馈："form 写了内容但没点保存，点开始解析被 toast 挡住需要先保存" → 让保存和解析合并为一次点击
    // PUT /api/my-brand 后端（L199-207）自带 profile auto-create，比 ensure-profile 更鲁棒
    const formDirty = brand && JSON.stringify(form) !== initialFormRef.current;
    if (isSelfOrCend && (!profileId || formDirty)) {
      try {
        // [CTO-13.0 2026-04-19 S1.1] 深度解析静默保存也同步 S1.1 新字段
        const payload = {
          name: form.name, industry: form.industry, company_name: form.company_name,
          brand_display_names: brandDisplayNamesPayload(form.brand_display_names),
          cities: form.cities, business: form.business, target_users: form.target_users,
          persona_positioning: form.persona_positioning, persona_tone: form.persona_tone,
          products: Array.isArray(products) ? products : [],
          pain_points: Array.isArray(painPoints) ? painPoints : [],
          competitors: Array.isArray(competitors) ? competitors : [],
          company_intro: form.company_intro,
          core_value: form.core_value,
          selling_points: form.selling_points,
          success_cases: form.success_cases,
          testimonials: form.testimonials,
          contact_phone: form.contact_phone,
          contact_wechat: form.contact_wechat,
          contact_website: form.contact_website,
          contact_address: form.contact_address,
          structured_knowledge: structuredKnowledge,
          // [CTO-15.9 M1c T4] 市场洞察字段也同步保存(深度解析静默保存)
          service_scope: serviceScope || undefined,
          local_competitors: localCompetitors.length > 0 ? localCompetitors : undefined,
          market_insight: Object.keys(marketInsight).length > 0 ? marketInsight : undefined,
          business_type: businessType || undefined,
          city_scope: cityScope || undefined,
          ...(categoryPick.touched ? { industry_category: categoryPick.key } : {}),  // [WO_267] 动过才带
        };
        await authApi.put('/api/my-brand', payload);
        initialFormRef.current = JSON.stringify(form);  // 清 dirty（和 handleSave 一致）
        // reload 拿最新 profile.id（PUT 可能刚 auto-create profile）
        const reloadRes = await authApi.get('/api/my-brand');
        const p = reloadRes.data?.profile;
        if (p) {
          setProfile(p);
          profileId = p.id;
        }
        await refreshClients();  // 同步 sidebar 品牌完善度
      } catch (e) {
        // 保存失败不阻断：仍尝试用现有 profileId 走解析
        console.warn('[deepAnalyze] 静默保存失败，继续尝试解析', e);
      }
    }

    if (!profileId) {
      toast.error('品牌档案初始化失败，请刷新页面重试或手动点击"保存"');
      return;
    }
    // v1_3 (CTO-15.1 2026-04-19): 去 window.confirm 弹窗 · 静默扣费口径(页面不前置告知额度)
    // 点击进 confirmLarge 大额 toast 确认（deepAnalyzeCost < 1000 直接执行不弹）
    const actionLabel = briefIsTimeout
      ? '重新发起深度解析（上次超时）'
      : briefIsDone
        ? '重新分析'
        : '开始深度解析（AI 联网搜索约 3-8 分钟）';
    const targetProfileId = profileId;  // capture 避免 5s toast 后 state 变
    confirmLarge(deepAnalyzeCost, () => { void doDeepAnalyze(targetProfileId); }, actionLabel);
  };

  const doDeepAnalyze = async (profileId: number | string | undefined) => {
    setDeepAnalyzing(true);
    try {
      const res = await authApi.post('/api/content/deep-analyze', {
        profile_id: profileId,
        city: form.cities || undefined,
      });
      if (res.data?.success) {
        toast.success('深度解析已开始，页面会自动刷新进度');
        // 保持 deepAnalyzing=true 直到 briefIsRunning 接管（useEffect 会清除）
        // 多次轮询确保拿到最新状态
        for (const delay of [1000, 3000, 6000, 10000, 15000, 20000, 30000]) {
          setTimeout(() => { reloadProfile(); }, delay);
        }
        // 兜底：最多 35 秒后释放（给后端足够时间更新状态）
        setTimeout(() => { setDeepAnalyzing(false); }, 35000);
        return; // 不走下面的 setDeepAnalyzing(false)
      } else {
        toast.error(res.data?.detail || '深度解析失败');
      }
    } catch (e: any) {
      const status = e?.response?.status;
      const msg = e?.response?.data?.detail || '深度解析失败';
      if (status === 409) {
        // 并发保护：已有任务在跑，保持禁用并轮询直到拿到运行态
        toast.info(typeof msg === 'string' ? msg : '已有任务在进行中');
        for (const delay of [500, 2000, 5000, 10000]) {
          setTimeout(() => { reloadProfile(); }, delay);
        }
        setTimeout(() => { setDeepAnalyzing(false); }, 15000);
        return; // 不走下面的 setDeepAnalyzing(false)
      } else if (status === 402) {
        // 算力不足:后端返结构化 detail {code, message},原先整坨 JSON.stringify 弹给用户 = 一坨乱码,
        // 等于"没有明确提醒"。统一弹人话引导文案(王姐口径·指向左侧菜单「购买算力」入口)。
        toast.error('算力不足 · 前往「购买算力」充值后即可继续');
      } else {
        // 其他错误:detail 可能是 string 或 {message} 对象 · 提取人话,不再显示原始 JSON
        const text = typeof msg === 'string' ? msg : (msg?.message || '深度解析失败');
        toast.error(text);
      }
    }
    setDeepAnalyzing(false);
  };

  // S0.2: handleAutoFill 已整体搬进 AiFillDialog 子组件
  // 本组件只通过 onFilled 回调接收结果（见 handleAiFilled）

  const handleDelete = async () => {
    try {
      const { data: preview } = await authApi.get(`/api/my-clients/${brandId}/archive-preview`);
      const counts = preview.associations;
      const ok = await askConfirm({
        title: `归档客户「${preview.object.display_name}」(#${preview.object.brand_id})？`,
        description: `关联 ${counts.profiles} 个档案、${counts.diagnoses} 条诊断、${counts.quotes} 份报价、${counts.topics} 个主题、${counts.articles} 篇文章。公开链接与员工分配会失效；业务数据保留，7 天内可恢复。`,
        confirmLabel: '归档',
        danger: true,
      });
      if (!ok) return;
      await authApi.delete(`/api/my-clients/${brandId}`, { params: { reason: '客户详情用户归档' } });
      await refreshClients();
      toast.success('客户已归档 · 7 天内可从回收站恢复');
      navigate('/my-clients');
    } catch (e: any) {
      const detail = e.response?.data?.detail;
      toast.error(typeof detail === 'string' ? detail : '删除失败');
    }
  };

  // [2026-06-03 一页式重排] 一键整理:调 /api/m3/material-confirm/clean
  // 后端会拉已上传文件(向量化)+ 现有 profile 作上下文整理为统一写作资料 · 成功后 reload
  // 传 notes 作整理指令(后端要求 raw_text 或 notes 至少一个非空 · 文件 KB 上下文也算),无文本时不阻断
  const handleOrganizeMaterials = async () => {
    if (organizing) return;
    setOrganizing(true);
    try {
      const result = await materialConfirmApi.clean({
        brand_id: brandId,
        notes: '把已填写的基础信息、上传的资料文件和图片统一梳理成写作资料',
      });
      await reloadProfile();
      // [B 联动 2026-06-03] 顶部「一键整理」后广播事件 · 下方写作资料确认区监听后自动刷新(无需手动点刷新)
      try { window.dispatchEvent(new CustomEvent('brand:materials-cleaned')); } catch { /* noop */ }
      if (result.warnings?.length) {
        toast.warning(result.warnings[0]);
      } else {
        toast.success('已整理，资料已更新');
      }
    } catch (error: any) {
      const detail = error?.response?.data?.detail || error?.response?.data?.message;
      toast.error(typeof detail === 'string' && detail.trim() ? detail : '整理失败，请稍后再试');
    } finally {
      setOrganizing(false);
    }
  };

  if (loading) return <div className="flex items-center justify-center h-64 text-muted-foreground text-sm">加载中...</div>;
  if (!brand) return <div className="flex items-center justify-center h-64 text-muted-foreground text-sm">未找到</div>;

  const tabTriggerClass =
    'h-9 flex-none min-w-[76px] rounded-lg px-3 text-xs data-[state=active]:bg-card data-[state=active]:shadow-sm sm:flex-1';
  const toolQuoteId = workbenchSnapshot?.quote?.id ?? null;
  const rawDiagnosisId = workbenchSnapshot?.diagnosis?.id ?? null;
  const toolDiagnosisId = rawDiagnosisId && rawDiagnosisId > 0 ? rawDiagnosisId : null;
  const toolPortalToken = workbenchSnapshot?.quote?.portal_token ?? null;

  // [2026-06-03 一页式重排] 头部进度条 + 状态徽章用的派生值
  const completenessValue = Number(brand?.completeness ?? brand?.geo_completeness ?? 0);
  const hasBasicInfo = !!(form.name && form.industry && form.cities);
  const hasBusinessProfile = !!(form.business || form.target_users || products.length || painPoints.length);

  return (
    <div className="mx-auto max-w-6xl px-3 pt-3 pb-28 sm:px-4 sm:pt-4 md:px-6 md:pt-6 xl:px-8">
      {/* 头部 */}
      <div className="mb-4 flex flex-col gap-3 sm:mb-5 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 flex-1 items-start gap-2 sm:gap-3">
          {mode === 'client' && (
            <Button variant="ghost" size="sm" className="h-10 w-10 shrink-0 p-0" onClick={() => navigate('/my-clients')}>
              <ArrowLeft className="h-4 w-4" />
            </Button>
          )}
          <div className="min-w-0 flex-1">
            <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              {mode === 'self' ? '品牌资料中心' : mode === 'c-end' ? 'IP 档案' : '客户资料中心'}
            </p>
            <h1 className="mt-1 truncate text-xl font-semibold tracking-tight text-foreground md:text-2xl">
              {mode === 'self' ? '我的品牌' : mode === 'c-end' ? '我的 IP' : form.name || '未命名客户'}
            </h1>
            {/* [P0 客户资料中心] 副标题 · 一眼明示资料同步用途 */}
            {!isCEnd && (
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">这些资料会同步用于 AI 体检、报价、写文章和客户报告</p>
            )}
            <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
              {form.industry && <span className="rounded-full border border-border/60 bg-card/60 px-2 py-1">{form.industry}</span>}
              {form.cities && <span className="rounded-full border border-border/60 bg-card/60 px-2 py-1">城市：{form.cities}</span>}
              {/* [2026-06-03 一页式重排] 资料状态徽章 · 按人话提示资料完善程度 · 仅 GEO */}
              {!isCEnd && <ProfileStatusBadge completeness={completenessValue} hasBasic={hasBasicInfo} hasBusiness={hasBusinessProfile} />}
            </div>
            {/* [2026-06-03 一页式重排] 资料完整度进度条 · 仅 GEO */}
            {!isCEnd && (
              <div className="mt-3 max-w-md">
                <div className="mb-1 flex items-center justify-between text-[11px] text-muted-foreground">
                  <span>资料完整度</span>
                  <span className="font-medium text-foreground">{completenessValue}%</span>
                </div>
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                  <div
                    className={cn(
                      'h-full rounded-full transition-all',
                      completenessValue >= 80 ? 'bg-emerald-500' : completenessValue >= 40 ? 'bg-amber-500' : 'bg-rose-500/70',
                    )}
                    style={{ width: `${Math.min(100, Math.max(0, completenessValue))}%` }}
                  />
                </div>
              </div>
            )}
          </div>
        </div>
        {/* GEO 保存改底部固定悬浮 bar · 此处保存按钮仅 c-end 兼容分支保留(原 Tabs 行为) */}
        {isCEnd && (activeTab === 'basic' || activeTab === 'persona' || activeTab === 'facts') && (
          <Button size="sm" className="hidden min-h-[44px] gap-1.5 text-sm sm:inline-flex sm:w-auto sm:min-w-[96px]" onClick={handleSave} disabled={saving}>
            <Save className="h-4 w-4" />{saving ? '保存中...' : '保存'}
          </Button>
        )}
      </div>

      {/* ============================================================
          [2026-06-03 一页式重排] GEO(client/self · !isCEnd):竖向 + 两列网格一页式布局
          c-end 兼容分支(社媒 IP)保留原 Tabs 渲染不动(板块边界)
          ============================================================ */}
      {!isCEnd ? (
        <>
          {/* 2. [client] 决策条 DecisionBarBridge(保留现有用法) */}
          {mode === 'client' && Number.isFinite(brandId) && brandId > 0 && (
            <div className="mb-4 md:mb-6">
              <DecisionBarBridge brandId={brandId} showBackToClient={false} onSnapshot={setWorkbenchSnapshot} />
            </div>
          )}

          {/* 2.5 [包H · U-9 2026-08-23] 「发给客户」统一面板。
              U-9 逐字:四类 token 链接从**统一面板**生成、自动带人话前缀、
              过期/撤销一键重新签发同对象新链接。
              🔴 只在 client 模式显示:self / c-end 是品牌自己看自己,
                 没有"发给客户"这件事。面板本身只读,重签发是显式一颗按钮。 */}
          {mode === 'client' && Number.isFinite(brandId) && brandId > 0 && (
            <div className="mb-4 rounded-2xl border border-border bg-card/50 p-4 md:mb-5">
              <CustomerLinksPanel brandId={brandId} />
            </div>
          )}

          {/* 3. 「让 AI 整理客户资料」卡 · 一键整理(client/self 都显) */}
          <div className="mb-4 flex flex-col gap-3 rounded-2xl border border-border bg-card/50 p-4 sm:flex-row sm:items-center sm:justify-between md:mb-5">
            <div className="flex items-start gap-3 min-w-0">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-amber-500/10 text-amber-400">
                <WandSparkles className="h-5 w-5" />
              </div>
              <div className="min-w-0">
                <h3 className="text-sm font-semibold text-foreground">让 AI 帮你整理客户资料</h3>
                <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                  把已有资料整理成体检、报价、写文章都会用的内容。
                </p>
              </div>
            </div>
            <Button
              size="sm"
              className="min-h-[40px] w-full shrink-0 gap-1.5 text-sm sm:w-auto sm:min-w-[104px]"
              onClick={handleOrganizeMaterials}
              disabled={organizing}
            >
              {organizing ? <Loader2 className="h-4 w-4 animate-spin" /> : <WandSparkles className="h-4 w-4" />}
              {organizing ? '整理中...' : '用 AI 帮你填'}
            </Button>
          </div>

          {/* 4. 锚点 chip 导航 · 点击 scrollIntoView 到对应卡 */}
          <div className="-mx-3 mb-4 flex gap-2 overflow-x-auto px-3 pb-1 sm:mx-0 sm:px-0 md:mb-5">
            {[
              { id: 'sec-basic', label: '基础信息' },
              { id: 'sec-facts', label: '业务画像' },
              { id: 'sec-files', label: '资料文件' },
              { id: 'sec-images', label: '图片素材' },
            ].map((a) => (
              <button
                key={a.id}
                type="button"
                onClick={() => document.getElementById(a.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
                className="shrink-0 rounded-full border border-border/60 bg-card/60 px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-border hover:text-foreground"
              >
                {a.label}
              </button>
            ))}
          </div>

          {/* 5. 两列网格:①基础信息 ②业务画像 ③资料文件 ④图片素材 */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* ① 基础信息卡(左) */}
            <section id="sec-basic" className="space-y-4 rounded-2xl border border-border border-l-2 border-l-brand/40 bg-muted/30 p-4 sm:p-5 lg:scroll-mt-24">
              <SecHead n={1} title="基础信息" sub="先把客户是谁、做什么填清楚" badge="必填" />

              {/* 补齐向导:一行纯文字提示 · 指向下方「用 AI 帮你填」 */}
              {brand && brandId && Number(brand.completeness ?? 0) < 80 && (
                <p className="rounded-lg border border-amber-500/25 bg-amber-500/5 px-3 py-2 text-xs leading-relaxed text-amber-500">
                  资料还差一些 · 下方「用 AI 帮你填」可一键补齐。
                </p>
              )}

              {/* [2026-06-04 老板「只保顶部一个 AI 主按钮」] AI 帮填收进低调折叠入口(默认收起)· 不再做卡头醒目按钮 */}
              <Collapsible open={aiFillOpen} onOpenChange={setAiFillOpen} className="rounded-xl border border-dashed border-border/50 bg-muted/10">
                <CollapsibleTrigger className="flex min-h-[40px] w-full items-center gap-2 px-3 py-2 text-left text-xs text-muted-foreground transition-colors hover:text-foreground">
                  <Sparkles className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <span className="flex-1">不想手填？用 AI 帮你填（输入公司名联网搜 / 粘贴资料自动提取）</span>
                  <ChevronDown className={cn('h-4 w-4 shrink-0 transition-transform', aiFillOpen && 'rotate-180')} />
                </CollapsibleTrigger>
                <CollapsibleContent className="px-1 pb-1">
                  <AiFillDialog brandId={brandId} onFilled={handleAiFilled} />
                </CollapsibleContent>
              </Collapsible>

              <FG label="品牌/业务名" aiFilled={aiFilledFields.has('name')}>
                <Input value={form.name} onChange={e => { setForm(f => ({ ...f, name: e.target.value })); clearAiHighlight('name'); }} />
              </FG>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <FG label="行业" aiFilled={aiFilledFields.has('industry')} confidence={aiConfidences['industry']}><Input value={form.industry} onChange={e => { setForm(f => ({ ...f, industry: e.target.value })); clearAiHighlight('industry'); }} placeholder="如：美容护肤" /></FG>
                <FG label="城市" aiFilled={aiFilledFields.has('cities')}><Input value={form.cities} onChange={e => { setForm(f => ({ ...f, cities: e.target.value })); clearAiHighlight('cities'); }} placeholder="如：杭州" /></FG>
              </div>
              <FG label="行业大类">
                <IndustryCategorySelect raw={brand?.industry_category} value={categoryPick.key}
                  touched={categoryPick.touched} onPick={(key) => setCategoryPick({ touched: true, key })} />
              </FG>
              <FG label="公司名称" aiFilled={aiFilledFields.has('company_name')}><Input value={form.company_name} onChange={e => { setForm(f => ({ ...f, company_name: e.target.value })); clearAiHighlight('company_name'); }} placeholder="如：佛山市XX有限公司（C 端博主可不填）" /></FG>
              <FG label="AI 搜索常用名">
                <Textarea
                  value={form.brand_display_names}
                  onChange={e => setForm(f => ({ ...f, brand_display_names: e.target.value }))}
                  placeholder={'每行一个，如：品牌简称、英文名'}
                  className="min-h-[84px] resize-y"
                />
                <p className="mt-1 text-xs leading-5 text-muted-foreground">用于识别 AI 回答中的品牌称呼。只填写确认属于该品牌的名称。</p>
              </FG>

              {/* 业务类型 + 地域范围选器 · 驱动关键词生成 */}
              <div className="space-y-3 rounded-xl border border-border p-3 sm:p-4">
                <div className="text-xs font-medium text-muted-foreground">业务类型（用于生成关键词）</div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  <div>
                    <div className="text-[11px] text-muted-foreground mb-1">目标客户类型</div>
                    <div className="flex gap-1.5 flex-wrap">
                      {[
                        { v: 'B2C', l: 'B2C · 终端消费者', hint: '饭店/装修/美容 等本地消费' },
                        { v: 'B2B', l: 'B2B · 企业采购', hint: '机械/SaaS/外贸 等企业决策' },
                        { v: '政企', l: '政企 · 合规采购', hint: '政府/国企/央企' },
                      ].map((o) => (
                        <button
                          key={o.v}
                          type="button"
                          onClick={() => { setBusinessType(o.v); clearAiHighlight('business_type'); }}
                          title={o.hint}
                          className={
                            businessType === o.v
                              ? 'px-3 py-1.5 rounded-md text-xs border border-indigo-500/50 bg-indigo-500/10 text-indigo-600'
                              : 'px-3 py-1.5 rounded-md text-xs border border-border text-muted-foreground hover:bg-muted/60'
                          }
                        >
                          {aiFilledFields.has('business_type') && businessType === o.v && <Sparkles className="inline h-3 w-3 mr-0.5 text-amber-500" />}
                          {o.l}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] text-muted-foreground mb-1">地域范围</div>
                    <div className="flex gap-1.5 flex-wrap">
                      {[
                        { v: 'local', l: '本地/区域', hint: '城市/省级服务' },
                        { v: 'national', l: '全国', hint: '跨省/纯线上' },
                      ].map((o) => (
                        <button
                          key={o.v}
                          type="button"
                          onClick={() => { setCityScope(o.v); clearAiHighlight('city_scope'); }}
                          title={o.hint}
                          className={
                            cityScope === o.v
                              ? 'px-3 py-1.5 rounded-md text-xs border border-indigo-500/50 bg-indigo-500/10 text-indigo-600'
                              : 'px-3 py-1.5 rounded-md text-xs border border-border text-muted-foreground hover:bg-muted/60'
                          }
                        >
                          {aiFilledFields.has('city_scope') && cityScope === o.v && <Sparkles className="inline h-3 w-3 mr-0.5 text-amber-500" />}
                          {o.l}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
              </div>

              {/* 联系方式 — 写文章可选插入·媒体发布自动软化 */}
              <div className="space-y-3 rounded-xl border border-border p-3 sm:p-4">
                <div>
                  <div className="text-xs font-medium">联系方式</div>
                  <div className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
                    这些信息可用于文章结尾引导客户联系。媒体发布时系统会自动处理，避免因为电话/微信导致审核不通过。
                  </div>
                </div>
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <FG label="电话"><Input value={form.contact_phone} onChange={e => setForm(f => ({ ...f, contact_phone: e.target.value }))} placeholder="如：400-xxx-xxxx 或手机号" /></FG>
                  <FG label="微信/企业微信"><Input value={form.contact_wechat} onChange={e => setForm(f => ({ ...f, contact_wechat: e.target.value }))} placeholder="微信号 / 企业微信" /></FG>
                  <FG label="官网"><Input value={form.contact_website} onChange={e => setForm(f => ({ ...f, contact_website: e.target.value }))} placeholder="如：https://www.example.com" /></FG>
                  <FG label="地址"><Input value={form.contact_address} onChange={e => setForm(f => ({ ...f, contact_address: e.target.value }))} placeholder="门店/公司地址" /></FG>
                </div>
              </div>
            </section>

            {/* ② 业务画像卡(右) */}
            <section id="sec-facts" className="space-y-4 rounded-2xl border border-border border-l-2 border-l-brand/40 bg-muted/30 p-4 sm:p-5 lg:scroll-mt-24">
              <SecHead n={2} title="业务画像" sub="诊断 / 报价 / 写文章主要看这里" badge="AI 主读" />

              {/* 一句话业务 / 目标客户 / 产品 / 痛点 / 竞品(从基础信息搬来) */}
              <FG
                label="一句话业务描述"
                aiFilled={aiFilledFields.has('business')}
                confidence={aiConfidences['business']}
                action={profile?.id && form.business ? (
                  <PolishButton
                    profileId={profile.id}
                    field="business"
                    currentValue={form.business}
                    label="一句话业务描述"
                    onAccept={(v) => { setForm(f => ({ ...f, business: v })); clearAiHighlight('business'); }}
                  />
                ) : null}
              >
                <Input value={form.business} onChange={e => { setForm(f => ({ ...f, business: e.target.value })); clearAiHighlight('business'); }} placeholder="告诉AI你做什么" />
              </FG>
              <FG
                label="目标客户"
                aiFilled={aiFilledFields.has('target_users')}
                confidence={aiConfidences['target_users']}
                action={profile?.id && form.target_users ? (
                  <PolishButton
                    profileId={profile.id}
                    field="target_users"
                    currentValue={form.target_users}
                    label="目标客户"
                    onAccept={(v) => { setForm(f => ({ ...f, target_users: v })); clearAiHighlight('target_users'); }}
                  />
                ) : null}
              >
                <Input value={form.target_users} onChange={e => { setForm(f => ({ ...f, target_users: e.target.value })); clearAiHighlight('target_users'); }} placeholder="如：25-45岁中产女性" />
              </FG>
              <FG label="产品/服务" aiFilled={aiFilledFields.has('products')}>
                <TagInput values={products} onChange={(v) => { setProducts(v); clearAiHighlight('products'); }} placeholder="输入后回车添加" />
              </FG>
              <FG label="客户痛点" aiFilled={aiFilledFields.has('pain_points')}>
                <TagInput values={painPoints} onChange={(v) => { setPainPoints(v); clearAiHighlight('pain_points'); }} placeholder="输入后回车添加" />
              </FG>
              <FG label="竞争对手" aiFilled={aiFilledFields.has('competitors')}>
                <TagInput values={competitors} onChange={(v: string[]) => { setCompetitors(v); clearAiHighlight('competitors'); }} placeholder="输入后回车添加" />
              </FG>

              {/* [2026-06-04 老板「只保顶部一个 AI 主按钮」] 深度行业解析整块默认折叠 · 收进展开(状态显在折叠标题上) */}
              <Collapsible className="rounded-2xl border border-dashed border-border/60 bg-muted/10">
                <CollapsibleTrigger className="flex min-h-[44px] w-full items-center justify-between gap-2 px-3 py-3 text-left sm:px-4">
                  <span className="inline-flex flex-wrap items-center gap-1.5 text-sm font-medium text-foreground">
                    <Sparkles className="h-4 w-4 shrink-0 text-amber-400" />
                    深度行业解析（进阶）
                    <span className="rounded-full border border-border/60 px-2 py-0.5 text-[10px] font-normal text-muted-foreground">给 AI 写作补行业知识</span>
                    {briefIsDone && !deepAnalyzing && <span className="rounded bg-emerald-500/10 px-1.5 py-0.5 text-[10px] text-emerald-400">✓ 已解析</span>}
                    {(briefIsRunning || deepAnalyzing) && <span className="rounded bg-amber-500/20 px-1.5 py-0.5 text-[10px] text-amber-300 animate-pulse">分析中</span>}
                  </span>
                  <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground transition-transform" />
                </CollapsibleTrigger>
                <CollapsibleContent className="px-3 pb-3 pt-0 sm:px-4">
              {/* 深度行业解析(联网生成行业知识,供 AI 写作引用) */}
              <div className="rounded-2xl border border-amber-500/20 bg-amber-500/5 p-3 sm:p-4">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 mb-1 flex-wrap">
                      <Sparkles className="h-4 w-4 text-amber-400 shrink-0" />
                      <span className="text-[11px] font-medium text-amber-400 tracking-wide">深度行业解析</span>

                      {(briefIsRunning || deepAnalyzing) && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 animate-pulse">
                          {deepAnalyzing && !briefIsRunning ? '启动中...' : `${briefStageLabel} · 已 ${briefElapsedMin} 分钟`}
                        </span>
                      )}
                      {briefIsDone && !deepAnalyzing && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400">✓ 已解析</span>
                      )}
                      {briefIsTimeout && !deepAnalyzing && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-rose-500/10 text-rose-400">超时待重试</span>
                      )}
                      {briefIsFailed && !deepAnalyzing && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-rose-500/10 text-rose-400">解析失败</span>
                      )}
                    </div>
                    <p className="text-[11px] text-muted-foreground/70 leading-relaxed">
                      {(briefIsRunning || deepAnalyzing)
                        ? 'AI 正在联网搜集行业数据，约 3-5 分钟完成。可以切换页面不影响后台任务，回来时会自动更新。'
                        : briefIsDone
                          ? '行业知识已就位。重新分析可刷新最新数据。'
                          : 'AI 联网搜索你所在行业的目标人群、竞品格局、获客路径、爆款内容，生成给 AI 创作用的行业知识库（不填充基本信息表单）'}
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant="outline"
                    className={cn(
                      "min-h-[44px] w-full shrink-0 border-amber-500/30 text-xs text-amber-400 hover:bg-amber-500/10 sm:w-auto",
                      (deepAnalyzing || briefIsRunning) && "opacity-60 cursor-not-allowed"
                    )}
                    onClick={handleDeepAnalyze}
                    disabled={deepAnalyzing || briefIsRunning || deepAnalyzeCost == null}
                    title={deepAnalyzeCost == null ? (pricingError || '正在读取动态价目') : undefined}
                  >
                    {deepAnalyzeCost == null ? (
                      pricingLoading ? '价目读取中…' : PRICING_UNAVAILABLE_LABEL
                    ) : (deepAnalyzing || briefIsRunning) ? (
                      <><RefreshCw className="h-3.5 w-3.5 animate-spin mr-1" />
                        {briefIsRunning ? `收集中 · ${briefElapsedMin}分钟` : '启动中...'}
                      </>
                    ) : briefIsDone ? (
                      <><Sparkles className="h-3.5 w-3.5 mr-1" />重新分析</>
                    ) : briefIsTimeout || briefIsFailed ? (
                      <><RefreshCw className="h-3.5 w-3.5 mr-1" />重试</>
                    ) : (
                      <><Sparkles className="h-3.5 w-3.5 mr-1" />AI 生成</>
                    )}
                  </Button>
                  {/* 🔴 [#199] 价目读不到:原因画在按钮外面 + 真能点的出口(三处同一实现) */}
                  {deepAnalyzeCost == null && !pricingLoading && (
                    <PricingUnavailableHint
                      error={pricingError}
                      retryNotBefore={pricingRetryNotBefore}
                      onRetry={() => { void pricingRefresh(); }}
                      className="mt-1"
                    />
                  )}
                </div>
              </div>
                </CollapsibleContent>
              </Collapsible>

              {/* 行业知识审核卡 */}
              {briefIsDone && profile?.industry_brief && profile?.id && (() => {
                const brief = typeof profile.industry_brief === 'string'
                  ? (() => { try { return JSON.parse(profile.industry_brief); } catch { return null; } })()
                  : profile.industry_brief;
                if (!brief || typeof brief !== 'object') return null;
                const partial = (() => {
                  const raw = profile.industry_brief_partial_fields;
                  if (Array.isArray(raw)) return raw as string[];
                  if (typeof raw === 'string') {
                    try { const v = JSON.parse(raw); return Array.isArray(v) ? v : null; } catch { return null; }
                  }
                  return null;
                })();
                return (
                  <IndustryBriefReviewCard
                    profileId={profile.id}
                    brief={brief}
                    confirmed={!!profile.industry_brief_confirmed}
                    partialFields={partial}
                    version={Number(profile.industry_brief_version || 0)}
                    onRefresh={reloadProfile}
                    readonly={isCEnd}
                  />
                );
              })()}

              {/* 深度营销资料 */}
              <DeepMarketingSection
                form={form}
                setForm={setForm}
                aiFilledFields={aiFilledFields}
                clearAiHighlight={clearAiHighlight}
                profileId={profile?.id}
              />

              {/* 5 维业务画像编辑器(GEO) */}
              <StructuredKnowledgeEditor
                value={structuredKnowledge as any}
                onChange={(v) => { setStructuredKnowledge(v as Record<string, unknown>); clearAiHighlight('structured_knowledge'); }}
                aiFilled={aiFilledFields.has('structured_knowledge')}
              />

              {/* 市场洞察(进阶 · 默认折叠) */}
              <Collapsible className="rounded-2xl border border-dashed border-border/60 bg-muted/10">
                <CollapsibleTrigger className="flex min-h-[44px] w-full items-center justify-between gap-2 px-3 py-3 text-left sm:px-4">
                  <span className="inline-flex flex-wrap items-center gap-1.5 text-sm font-medium text-foreground">
                    市场洞察（进阶）
                    <span className="rounded-full border border-border/60 px-2 py-0.5 text-[10px] font-normal text-muted-foreground">影响报价地域 · 写作差异化</span>
                  </span>
                  <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground transition-transform" />
                </CollapsibleTrigger>
                <CollapsibleContent className="px-1 pb-1 pt-0">
                  <MarketInsightCard
                    serviceScope={serviceScope}
                    setServiceScope={setServiceScope}
                    localCompetitors={localCompetitors}
                    setLocalCompetitors={setLocalCompetitors}
                    marketInsight={marketInsight}
                    setMarketInsight={setMarketInsight}
                    aiFilledFields={aiFilledFields}
                    clearAiHighlight={clearAiHighlight}
                  />
                </CollapsibleContent>
              </Collapsible>
            </section>

            {/* ③ 资料文件卡(左) */}
            <section id="sec-files" className="space-y-3 rounded-2xl border border-border border-l-2 border-l-brand/40 bg-muted/30 p-4 sm:p-5 lg:scroll-mt-24">
              <SecHead n={3} title="资料文件" sub="上传后自动同步至业务画像" badge="资料来源" />
              {brandId ? (
                <BrandKnowledgeManager brandId={brandId} embedded />
              ) : (
                <div className="text-xs text-muted-foreground py-8 text-center">请先保存基础信息再上传资料文件</div>
              )}
            </section>

            {/* ④ 图片素材卡(右) */}
            <section id="sec-images" className="space-y-3 rounded-2xl border border-border border-l-2 border-l-brand/40 bg-muted/30 p-4 sm:p-5 lg:scroll-mt-24">
              <SecHead n={4} title="图片素材" sub="上传门店 / 产品 / 案例图，写文章自动配图" badge="素材资产" />
              {brandId ? (
                <BrandImageGallery brandId={brandId} embedded />
              ) : (
                <div className="text-xs text-muted-foreground py-8 text-center">请先保存基础信息再上传图片</div>
              )}
            </section>
          </div>

          {/* 6. [client] 客户确认链接区 */}
          {mode === 'client' && Number.isFinite(brandId) && brandId > 0 && (
            <div className="mt-4 md:mt-5">
              <ClientMaterialConfirmCard
                brandId={brandId}
                brandName={form.name || brand?.name || ''}
                onMaterialsUpdated={reloadProfile}
              />
            </div>
          )}

          {/* 7. 「查看记录」折叠区(mode !== 'self' · 默认收起) */}
          {mode !== 'self' && (
            <Collapsible defaultOpen={false} className="mt-4 rounded-2xl border border-border bg-card/40 md:mt-5">
              <CollapsibleTrigger className="flex min-h-[48px] w-full items-center justify-between gap-2 rounded-2xl px-3 py-3 text-left transition hover:bg-accent/40 sm:px-4">
                <span className="inline-flex min-w-0 items-center gap-1.5 text-sm font-medium text-foreground">
                  <span className="truncate">查看记录</span>
                  <span className="hidden text-[11px] font-normal text-muted-foreground sm:inline">· 诊断记录和询价记录</span>
                </span>
                <ChevronDown className="h-4 w-4 text-muted-foreground transition-transform" aria-hidden />
              </CollapsibleTrigger>
              <CollapsibleContent className="space-y-5 px-3 pb-4 pt-1 sm:px-4">
                {/* 诊断记录 + GEO 业务概览 */}
                <div className="space-y-3">
                  <div className="-mx-1 flex gap-2 overflow-x-auto px-1 pb-1 sm:mx-0 sm:grid sm:grid-cols-3 sm:gap-3 sm:overflow-visible sm:px-0">
                    <SummaryCard label="诊断次数" value={brand?.diagnosis_count ?? 0} />
                    <SummaryCard label="文章数" value={brand?.article_count ?? 0} />
                    <SummaryCard label="已上榜关键词" value={brand?.ranked_keyword_count ?? 0} />
                  </div>
                  <DiagnosisTab brandId={brandId} />
                </div>
                {/* 询价记录 */}
                <div className="space-y-3">
                  <h3 className="text-sm font-medium text-foreground">询价记录</h3>
                  <InteractionsTab brandId={brandId} />
                </div>
              </CollapsibleContent>
            </Collapsible>
          )}

          {/* 8. [client] 更多客户动作 11 工具卡折叠(保留现有用法) */}
          {mode === 'client' && Number.isFinite(brandId) && brandId > 0 && (
            <Collapsible defaultOpen={false} className="mt-4 rounded-2xl border border-border bg-card/40 md:mt-5">
              <CollapsibleTrigger className="flex min-h-[48px] w-full items-center justify-between gap-2 rounded-2xl px-3 py-3 text-left transition hover:bg-accent/40 sm:px-4">
                <span className="inline-flex min-w-0 items-center gap-1.5 text-sm font-medium text-foreground">
                  <span className="truncate">更多客户动作</span>
                  <span className="hidden text-[11px] font-normal text-muted-foreground sm:inline">
                    · 诊断、报价、写作、发布和报告入口
                  </span>
                </span>
                <ChevronDown className="h-4 w-4 text-muted-foreground transition-transform" aria-hidden />
              </CollapsibleTrigger>
              <CollapsibleContent className="px-2 pb-3 pt-1 sm:px-3">
                <ToolGridCard
                  brandId={brandId}
                  quoteId={toolQuoteId}
                  diagnosisId={toolDiagnosisId}
                  portalToken={toolPortalToken}
                  title=""
                  className="border-0 bg-transparent shadow-none"
                />
              </CollapsibleContent>
            </Collapsible>
          )}
        </>
      ) : (
      /* ============================================================
         c-end 兼容分支(社媒 IP /s/my-ip)· 保留原 GEO 摘要卡 + 原 Tabs 渲染完全不动
         ============================================================ */
      <>
      <div className="-mx-3 mb-4 flex gap-2 overflow-x-auto px-3 pb-1 sm:mx-0 sm:grid sm:grid-cols-4 sm:gap-3 sm:overflow-visible sm:px-0 md:mb-5">
        <SummaryCard label="GEO资料评分" value={brand?.geo_completeness != null ? `${brand.geo_completeness}%` : '--'} />
        <SummaryCard label="诊断次数" value={brand?.diagnosis_count ?? 0} />
        <SummaryCard label="文章数" value={brand?.article_count ?? 0} />
        <SummaryCard label="已上榜关键词" value={brand?.ranked_keyword_count ?? 0} />
      </div>

      {/* Tabs */}
      <Tabs value={activeTab} onValueChange={setActiveTab} className="space-y-4">
        <div className="-mx-3 overflow-x-auto px-3 pb-1 sm:mx-0 sm:px-0">
          <TabsList className="inline-flex h-auto min-w-full w-max rounded-xl bg-muted/30 p-1">
            {/* [2026-06-02 P0 客户资料中心] basic→「基本资料」· knowledge 拆「业务事实/资料文件/客户确认」3 区 */}
            <TabsTrigger value="basic" className={tabTriggerClass}>基本资料</TabsTrigger>
            <TabsTrigger value="facts" className={tabTriggerClass}>业务事实</TabsTrigger>
            <TabsTrigger value="files" className={tabTriggerClass}>资料文件</TabsTrigger>
            {/* 客户确认仅客户详情(client)· self/c-end 无客户确认流程 */}
            {mode === 'client' && <TabsTrigger value="confirm" className={tabTriggerClass}>客户确认</TabsTrigger>}
            {/* [社媒遗产清除] IP 人设仅社媒 c-end 兼容分支 */}
            {isCEnd && <TabsTrigger value="persona" className={tabTriggerClass}>IP 人设</TabsTrigger>}
            {mode !== 'self' && (
              <>
                {/* 内容记录(社媒脚本)仅 c-end */}
                {isCEnd && (
                  <TabsTrigger value="content" className={tabTriggerClass}>
                    <span className="sm:hidden">内容</span><span className="hidden sm:inline">内容记录</span>
                  </TabsTrigger>
                )}
                <TabsTrigger value="diagnosis" className={tabTriggerClass}>
                  <span className="sm:hidden">诊断</span><span className="hidden sm:inline">诊断记录</span>
                </TabsTrigger>
                {/* 语料库仅 c-end */}
                {isCEnd && <TabsTrigger value="corpus" className={tabTriggerClass}>语料库</TabsTrigger>}
                {/* [D2] 互动记录 → 询价记录(GEO client · 关键词询价/竞品/备注 · 去社媒味「互动」);
                    c-end 兼容分支(/s/* 已走 社媒工作台主页面 · 非活跃路径)保留「互动记录」不变 */}
                <TabsTrigger value="interactions" className={tabTriggerClass}>
                  {isCEnd
                    ? (<><span className="sm:hidden">互动</span><span className="hidden sm:inline">互动记录</span></>)
                    : (<><span className="sm:hidden">询价</span><span className="hidden sm:inline">询价记录</span></>)}
                </TabsTrigger>
              </>
            )}
          </TabsList>
        </div>

        {/* 基本资料 */}
        <TabsContent value="basic" className="space-y-4">
          <p className="text-xs leading-relaxed text-muted-foreground">先把客户是谁、做什么、卖给谁填清楚。</p>
          {/* AI 智能填充 — 基本资料编辑内的辅助动作 */}
          <AiFillDialog brandId={brandId} onFilled={handleAiFilled} />

          {/* [CTO-15.9 M1c A1] 资料补齐 3 步向导 banner(completeness<80 自动显示) */}
          {brand && brandId && (
            <BrandWizardBanner
              brandId={brandId}
              form={{
                name: form.name,
                industry: form.industry,
                cities: form.cities,
                business: form.business,
              }}
              completeness={Number(brand.completeness ?? 0)}
              aiFilledFields={aiFilledFields}
              onAiFilled={handleAiFilled}
              onSave={handleSave}
              onStartDiagnosis={() => navigate(`/diagnosis/new?brand_id=${brandId}`)}
            />
          )}

          <FG label="品牌/业务名" aiFilled={aiFilledFields.has('name')}>
            <Input value={form.name} onChange={e => { setForm(f => ({ ...f, name: e.target.value })); clearAiHighlight('name'); }} />
          </FG>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <FG label="行业" aiFilled={aiFilledFields.has('industry')} confidence={aiConfidences['industry']}><Input value={form.industry} onChange={e => { setForm(f => ({ ...f, industry: e.target.value })); clearAiHighlight('industry'); }} placeholder="如：美容护肤" /></FG>
            <FG label="城市" aiFilled={aiFilledFields.has('cities')}><Input value={form.cities} onChange={e => { setForm(f => ({ ...f, cities: e.target.value })); clearAiHighlight('cities'); }} placeholder="如：杭州" /></FG>
          </div>
          <FG label="行业大类">
            <IndustryCategorySelect raw={brand?.industry_category} value={categoryPick.key}
              touched={categoryPick.touched} onPick={(key) => setCategoryPick({ touched: true, key })} />
          </FG>
          <FG label="公司名称" aiFilled={aiFilledFields.has('company_name')}><Input value={form.company_name} onChange={e => { setForm(f => ({ ...f, company_name: e.target.value })); clearAiHighlight('company_name'); }} placeholder="如：佛山市XX有限公司（C 端博主可不填）" /></FG>
          <FG label="AI 搜索常用名">
            <Textarea
              value={form.brand_display_names}
              onChange={e => setForm(f => ({ ...f, brand_display_names: e.target.value }))}
              placeholder={'每行一个，如：品牌简称、英文名'}
              className="min-h-[84px] resize-y"
            />
            <p className="mt-1 text-xs leading-5 text-muted-foreground">用于识别 AI 回答中的品牌称呼。只填写确认属于该品牌的名称。</p>
          </FG>
          <FG
            label="一句话业务描述"
            aiFilled={aiFilledFields.has('business')}
            confidence={aiConfidences['business']}
            action={profile?.id && form.business ? (
              <PolishButton
                profileId={profile.id}
                field="business"
                currentValue={form.business}
                label="一句话业务描述"
                onAccept={(v) => { setForm(f => ({ ...f, business: v })); clearAiHighlight('business'); }}
              />
            ) : null}
          >
            <Input value={form.business} onChange={e => { setForm(f => ({ ...f, business: e.target.value })); clearAiHighlight('business'); }} placeholder="告诉AI你做什么" />
          </FG>
          <FG
            label="目标客户"
            aiFilled={aiFilledFields.has('target_users')}
            confidence={aiConfidences['target_users']}
            action={profile?.id && form.target_users ? (
              <PolishButton
                profileId={profile.id}
                field="target_users"
                currentValue={form.target_users}
                label="目标客户"
                onAccept={(v) => { setForm(f => ({ ...f, target_users: v })); clearAiHighlight('target_users'); }}
              />
            ) : null}
          >
            <Input value={form.target_users} onChange={e => { setForm(f => ({ ...f, target_users: e.target.value })); clearAiHighlight('target_users'); }} placeholder="如：25-45岁中产女性" />
          </FG>
          <FG label="产品/服务" aiFilled={aiFilledFields.has('products')}>
            <TagInput values={products} onChange={(v) => { setProducts(v); clearAiHighlight('products'); }} placeholder="输入后回车添加" />
          </FG>
          <FG label="客户痛点" aiFilled={aiFilledFields.has('pain_points')}>
            <TagInput values={painPoints} onChange={(v) => { setPainPoints(v); clearAiHighlight('pain_points'); }} placeholder="输入后回车添加" />
          </FG>
          <FG label="竞争对手" aiFilled={aiFilledFields.has('competitors')}>
            <TagInput values={competitors} onChange={(v: string[]) => { setCompetitors(v); clearAiHighlight('competitors'); }} placeholder="输入后回车添加" />
          </FG>

          {/* [CTO-15.9 M1b M5] business_type + city_scope 选器 · 驱动 keyword_expander 4 addon 分支 */}
          <div className="space-y-3 rounded-xl border border-border p-3 sm:p-4">
            <div className="text-xs font-medium text-muted-foreground">业务类型（用于生成关键词）</div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <div className="text-[11px] text-muted-foreground mb-1">目标客户类型</div>
                <div className="flex gap-1.5 flex-wrap">
                  {[
                    { v: 'B2C', l: 'B2C · 终端消费者', hint: '饭店/装修/美容 等本地消费' },
                    { v: 'B2B', l: 'B2B · 企业采购', hint: '机械/SaaS/外贸 等企业决策' },
                    { v: '政企', l: '政企 · 合规采购', hint: '政府/国企/央企' },
                  ].map((o) => (
                    <button
                      key={o.v}
                      type="button"
                      onClick={() => { setBusinessType(o.v); clearAiHighlight('business_type'); }}
                      title={o.hint}
                      className={
                        businessType === o.v
                          ? 'px-3 py-1.5 rounded-md text-xs border border-indigo-500/50 bg-indigo-500/10 text-indigo-600'
                          : 'px-3 py-1.5 rounded-md text-xs border border-border text-muted-foreground hover:bg-muted/60'
                      }
                    >
                      {aiFilledFields.has('business_type') && businessType === o.v && <Sparkles className="inline h-3 w-3 mr-0.5 text-amber-500" />}
                      {o.l}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <div className="text-[11px] text-muted-foreground mb-1">地域范围</div>
                <div className="flex gap-1.5 flex-wrap">
                  {[
                    { v: 'local', l: '本地/区域', hint: '城市/省级服务' },
                    { v: 'national', l: '全国', hint: '跨省/纯线上' },
                  ].map((o) => (
                    <button
                      key={o.v}
                      type="button"
                      onClick={() => { setCityScope(o.v); clearAiHighlight('city_scope'); }}
                      title={o.hint}
                      className={
                        cityScope === o.v
                          ? 'px-3 py-1.5 rounded-md text-xs border border-indigo-500/50 bg-indigo-500/10 text-indigo-600'
                          : 'px-3 py-1.5 rounded-md text-xs border border-border text-muted-foreground hover:bg-muted/60'
                      }
                    >
                      {aiFilledFields.has('city_scope') && cityScope === o.v && <Sparkles className="inline h-3 w-3 mr-0.5 text-amber-500" />}
                      {o.l}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </div>

          {/* [2026-06-02 GEO CTO] 联系方式 — 写文章可选插入·媒体发布自动软化(防拒稿) */}
          <div className="space-y-3 rounded-xl border border-border p-3 sm:p-4">
            <div>
              <div className="text-xs font-medium">联系方式</div>
              <div className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
                这些信息可用于文章结尾引导客户联系。媒体发布时系统会自动处理，避免因为电话/微信导致审核不通过。
              </div>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <FG label="电话"><Input value={form.contact_phone} onChange={e => setForm(f => ({ ...f, contact_phone: e.target.value }))} placeholder="如：400-xxx-xxxx 或手机号" /></FG>
              <FG label="微信/企业微信"><Input value={form.contact_wechat} onChange={e => setForm(f => ({ ...f, contact_wechat: e.target.value }))} placeholder="微信号 / 企业微信" /></FG>
              <FG label="官网"><Input value={form.contact_website} onChange={e => setForm(f => ({ ...f, contact_website: e.target.value }))} placeholder="如：https://www.example.com" /></FG>
              <FG label="地址"><Input value={form.contact_address} onChange={e => setForm(f => ({ ...f, contact_address: e.target.value }))} placeholder="门店/公司地址" /></FG>
            </div>
          </div>

        </TabsContent>

        {/* 知识库 Tab（v1_2 Bug 2 修复：BrandKnowledgeManager 组件挂载）
            [CTO-13.3 2026-04-19] 深度营销资料从基本信息 Tab 挪来 —— 老板反馈：
            "深度营销资料是在知识库中，在这里（基本信息）重复了" */}
        {/* [P0 业务事实] 5 维画像 + 营销资料 + 深度行业解析 — 决定 AI 后面怎么写 / 诊断 / 报价 */}
        <TabsContent value="facts" className="space-y-4">
          <p className="text-xs leading-relaxed text-muted-foreground">这里决定 AI 后面怎么写、怎么诊断、怎么生成报价。</p>
          {/* 深度行业解析(联网生成行业知识,供 AI 写作引用)*/}
          <div className="rounded-2xl border border-amber-500/20 bg-amber-500/5 p-3 sm:p-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <div className="flex items-center gap-2 mb-1 flex-wrap">
                  <Sparkles className="h-4 w-4 text-amber-400 shrink-0" />
                  <span className="text-[11px] font-medium text-amber-400 tracking-wide">深度行业解析</span>

                  {(briefIsRunning || deepAnalyzing) && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 animate-pulse">
                      {deepAnalyzing && !briefIsRunning ? '启动中...' : `${briefStageLabel} · 已 ${briefElapsedMin} 分钟`}
                    </span>
                  )}
                  {briefIsDone && !deepAnalyzing && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400">✓ 已解析</span>
                  )}
                  {briefIsTimeout && !deepAnalyzing && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-rose-500/10 text-rose-400">超时待重试</span>
                  )}
                  {briefIsFailed && !deepAnalyzing && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-rose-500/10 text-rose-400">解析失败</span>
                  )}
                </div>
                <p className="text-[11px] text-muted-foreground/70 leading-relaxed">
                  {(briefIsRunning || deepAnalyzing)
                    ? 'AI 正在联网搜集行业数据，约 3-5 分钟完成。可以切换页面不影响后台任务，回来时会自动更新。'
                    : briefIsDone
                      ? '行业知识已就位。重新分析可刷新最新数据。'
                      : 'AI 联网搜索你所在行业的目标人群、竞品格局、获客路径、爆款内容，生成给 AI 创作用的行业知识库（不填充基本信息表单）'}
                </p>
              </div>
              <Button
                size="sm"
                variant="outline"
                className={cn(
                  "min-h-[44px] w-full shrink-0 border-amber-500/30 text-xs text-amber-400 hover:bg-amber-500/10 sm:w-auto",
                  (deepAnalyzing || briefIsRunning) && "opacity-60 cursor-not-allowed"
                )}
                onClick={handleDeepAnalyze}
                disabled={deepAnalyzing || briefIsRunning || deepAnalyzeCost == null}
                title={deepAnalyzeCost == null ? (pricingError || '正在读取动态价目') : undefined}
              >
                {deepAnalyzeCost == null ? (
                  pricingLoading ? '价目读取中…' : PRICING_UNAVAILABLE_LABEL
                ) : (deepAnalyzing || briefIsRunning) ? (
                  <><RefreshCw className="h-3.5 w-3.5 animate-spin mr-1" />
                    {briefIsRunning ? `收集中 · ${briefElapsedMin}分钟` : '启动中...'}
                  </>
                ) : briefIsDone ? (
                  <><Sparkles className="h-3.5 w-3.5 mr-1" />重新分析</>
                ) : briefIsTimeout || briefIsFailed ? (
                  <><RefreshCw className="h-3.5 w-3.5 mr-1" />重试</>
                ) : (
                  <><Sparkles className="h-3.5 w-3.5 mr-1" />开始解析</>
                )}
              </Button>
              {/* 🔴 [#199] 价目读不到:原因画在按钮外面 + 真能点的出口(三处同一实现) */}
              {deepAnalyzeCost == null && !pricingLoading && (
                <PricingUnavailableHint
                  error={pricingError}
                  retryNotBefore={pricingRetryNotBefore}
                  onRetry={() => { void pricingRefresh(); }}
                  className="mt-1"
                />
              )}
            </div>
          </div>

          {/* v3.7 知识库审核工作流（Phase 1-4 一波交付） · 只在知识库 Tab 显示 */}
          {briefIsDone && profile?.industry_brief && profile?.id && (() => {
            const brief = typeof profile.industry_brief === 'string'
              ? (() => { try { return JSON.parse(profile.industry_brief); } catch { return null; } })()
              : profile.industry_brief;
            if (!brief || typeof brief !== 'object') return null;
            const partial = (() => {
              const raw = profile.industry_brief_partial_fields;
              if (Array.isArray(raw)) return raw as string[];
              if (typeof raw === 'string') {
                try { const v = JSON.parse(raw); return Array.isArray(v) ? v : null; } catch { return null; }
              }
              return null;
            })();
            return (
              <IndustryBriefReviewCard
                profileId={profile.id}
                brief={brief}
                confirmed={!!profile.industry_brief_confirmed}
                partialFields={partial}
                version={Number(profile.industry_brief_version || 0)}
                onRefresh={reloadProfile}
                /* S3.3 · C 端 mode='c-end' 时禁用矫正反哺（影响全行业，C 端用户不懂影响面） */
                readonly={isCEnd}
              />
            );
          })()}

          {/* [CTO-13.0 2026-04-19 S1.1 → CTO-13.3 移位] 深度营销资料（ai-fill 顾问版产出，前端可编辑 + 保存）*/}
          <DeepMarketingSection
            form={form}
            setForm={setForm}
            aiFilledFields={aiFilledFields}
            clearAiHighlight={clearAiHighlight}
            profileId={profile?.id}
          />
          {/* [2026-06-01 社媒遗产清除] 5 维业务画像从「IP人设」tab 搬来 —— 它是 GEO 业务事实
              (写文章/诊断/报价用),非社媒。c-end 仍在 persona tab 保留,这里只对 GEO 显示不重复 */}
          {!isCEnd && (
            <StructuredKnowledgeEditor
              value={structuredKnowledge as any}
              onChange={(v) => { setStructuredKnowledge(v as Record<string, unknown>); clearAiHighlight('structured_knowledge'); }}
              aiFilled={aiFilledFields.has('structured_knowledge')}
            />
          )}
          {/* [P0 反馈] 市场洞察从「基本资料」移来 — 业务深度输入(报价地域系数/行业知识注入/差异化 prompt)· 默认折叠不占首屏 */}
          <Collapsible className="rounded-2xl border border-dashed border-border/60 bg-muted/10">
            <CollapsibleTrigger className="flex min-h-[44px] w-full items-center justify-between gap-2 px-3 py-3 text-left sm:px-4">
              <span className="inline-flex flex-wrap items-center gap-1.5 text-sm font-medium text-foreground">
                市场洞察（进阶）
                <span className="rounded-full border border-border/60 px-2 py-0.5 text-[10px] font-normal text-muted-foreground">影响报价地域 · 写作差异化</span>
              </span>
              <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground transition-transform" />
            </CollapsibleTrigger>
            <CollapsibleContent className="px-1 pb-1 pt-0">
              <MarketInsightCard
                serviceScope={serviceScope}
                setServiceScope={setServiceScope}
                localCompetitors={localCompetitors}
                setLocalCompetitors={setLocalCompetitors}
                marketInsight={marketInsight}
                setMarketInsight={setMarketInsight}
                aiFilledFields={aiFilledFields}
                clearAiHighlight={clearAiHighlight}
              />
            </CollapsibleContent>
          </Collapsible>
        </TabsContent>

        {/* [P0 资料文件] 上传官网/产品手册/案例 · 诊断、报价、写作共用一份 */}
        <TabsContent value="files" className="space-y-4">
          <p className="text-xs leading-relaxed text-muted-foreground">上传官网、产品手册、案例,AI 会从里面找可用资料。</p>
          {brandId ? (
            <>
              <BrandKnowledgeManager brandId={brandId} />
              <BrandImageGallery brandId={brandId} />
            </>
          ) : (
            <div className="text-xs text-muted-foreground py-8 text-center">请先保存基本资料再上传资料文件</div>
          )}
        </TabsContent>

        {/* [P0 客户确认] 客户提交的内容需代理确认后才生效(仅客户详情 client) */}
        {mode === 'client' && (
          <TabsContent value="confirm" className="space-y-4">
            <p className="text-xs leading-relaxed text-muted-foreground">客户补充的内容不会直接改资料,需要你确认后才生效。</p>
            {Number.isFinite(brandId) && brandId > 0 && (
              <ClientMaterialConfirmCard
                brandId={brandId}
                brandName={form.name || brand?.name || ''}
                onMaterialsUpdated={reloadProfile}
              />
            )}
          </TabsContent>
        )}

        {/* IP 人设 — [2026-06-01 社媒遗产清除] 仅社媒 c-end(/s/my-ip)保留原样;
            GEO 已移除(5 维业务画像搬去知识库 tab) */}
        {isCEnd && (
          <TabsContent value="persona" className="space-y-4">
            <PersonaSummary profile={profile} navigate={navigate} onInvite={() => setShowSharePoster(true)} />
            <StructuredKnowledgeEditor
              value={structuredKnowledge as any}
              onChange={(v) => { setStructuredKnowledge(v as Record<string, unknown>); clearAiHighlight('structured_knowledge'); }}
              aiFilled={aiFilledFields.has('structured_knowledge')}
            />
          </TabsContent>
        )}

        {/* 内容记录(社媒脚本)— [2026-06-01 社媒遗产清除] 仅社媒 c-end */}
        {isCEnd && (
          <TabsContent value="content" className="space-y-4">
            <ContentTab profileId={profile?.id} />
          </TabsContent>
        )}

        {/* 诊断记录(GEO)· [P0] GEO 业务概览(诊断/文章/关键词)从顶部摘要卡移来 · 资料中心首屏聚焦资料 */}
        {mode !== 'self' && (
          <TabsContent value="diagnosis" className="space-y-4">
            <div className="-mx-3 flex gap-2 overflow-x-auto px-3 pb-1 sm:mx-0 sm:grid sm:grid-cols-3 sm:gap-3 sm:overflow-visible sm:px-0">
              <SummaryCard label="诊断次数" value={brand?.diagnosis_count ?? 0} />
              <SummaryCard label="文章数" value={brand?.article_count ?? 0} />
              <SummaryCard label="已上榜关键词" value={brand?.ranked_keyword_count ?? 0} />
            </div>
            <DiagnosisTab brandId={brandId} />
          </TabsContent>
        )}

        {/* 语料库(社媒)— [2026-06-01 社媒遗产清除] 仅社媒 c-end */}
        {isCEnd && (
          <TabsContent value="corpus" className="space-y-4">
            <CorpusTab profileId={profile?.id} />
          </TabsContent>
        )}

        {/* 询价记录(关键词询价/竞品/备注 · c-end 兼容分支)*/}
        {mode !== 'self' && (
          <TabsContent value="interactions" className="space-y-4">
            <InteractionsTab brandId={brandId} />
          </TabsContent>
        )}
      </Tabs>
      </>
      )}

      {/* [WO_260] 原「社媒友情链接」(想给这个客户做社媒 IP / 短视频? 去社媒工作台 →)已删:社媒随 E3 删域。 */}

      {/* 危险操作 */}
      {mode === 'client' && !isMember && (
        <div className="mt-8 pt-6 border-t border-border/30">
          <Button variant="destructive" size="sm" className="h-8 text-xs" onClick={handleDelete}>
            <Trash2 className="h-3.5 w-3.5 mr-1" />删除客户
          </Button>
        </div>
      )}

      {profile?.id && (
        <SharePosterDialog
          open={showSharePoster}
          onClose={() => setShowSharePoster(false)}
          type="interview"
          params={{ profile_id: profile.id }}
        />
      )}

      {/* [2026-06-03 一页式重排] GEO 保存改底部固定悬浮 bar(client/self 都显 · PC + 移动)
          c-end 兼容分支保留原 activeTab 条件 + 移动端 only 行为 */}
      {!isCEnd ? (
        <div className="fixed inset-x-0 bottom-0 z-30 border-t border-border bg-background/95 p-3 backdrop-blur">
          <div className="mx-auto flex max-w-6xl items-center justify-end gap-3 md:px-6 xl:px-8">
            <span className="mr-auto hidden text-xs text-muted-foreground sm:inline">资料会同步用于体检、报价、写文章和客户报告</span>
            <Button className="min-h-[44px] w-full gap-1.5 sm:w-auto sm:min-w-[140px]" onClick={handleSave} disabled={saving}>
              <Save className="h-4 w-4" />{saving ? '保存中...' : (mode === 'client' ? '保存客户资料' : '保存品牌资料')}
            </Button>
          </div>
        </div>
      ) : (
        (activeTab === 'basic' || activeTab === 'persona' || activeTab === 'facts') && (
          <div className="fixed inset-x-0 bottom-0 z-30 border-t border-border bg-background/95 p-3 backdrop-blur sm:hidden">
            <Button className="min-h-[44px] w-full gap-1.5" onClick={handleSave} disabled={saving}>
              <Save className="h-4 w-4" />{saving ? '保存中...' : '保存'}
            </Button>
          </div>
        )
      )}
      {confirmDialog}
    </div>
  );
}

// ========== 子组件 ==========

// [2026-06-03 一页式重排] 资料状态徽章 · 按完整度 + 字段填写情况给人话提示(不露技术黑话)
function ProfileStatusBadge({ completeness, hasBasic, hasBusiness }: {
  completeness: number;
  hasBasic: boolean;
  hasBusiness: boolean;
}) {
  let label: string;
  let tone: string;
  if (completeness >= 80) {
    label = '资料齐全';
    tone = 'border-emerald-500/30 bg-emerald-500/10 text-emerald-500';
  } else if (!hasBasic) {
    label = '先补基础信息';
    tone = 'border-rose-500/30 bg-rose-500/10 text-rose-500';
  } else if (!hasBusiness) {
    label = '还差业务画像';
    tone = 'border-amber-500/30 bg-amber-500/10 text-amber-500';
  } else {
    label = '资料基本完善';
    tone = 'border-sky-500/30 bg-sky-500/10 text-sky-500';
  }
  return (
    <span className={cn('rounded-full border px-2 py-1 text-[11px] font-medium', tone)}>{label}</span>
  );
}

function SummaryCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="min-w-[132px] flex-1 rounded-xl border border-border bg-card p-3 text-center sm:min-w-0 sm:p-4">
      <p className="text-xl font-bold text-foreground sm:text-lg">{value}</p>
      <p className="text-xs text-muted-foreground">{label}</p>
    </div>
  );
}

// [2026-06-04 一页式标准卡头] 4 区块统一:圆形编号①②③④ + 标题 + 一句副标 + 右侧紧凑动作槽。
// 解决「基础信息标题和其他几块不一致/孤立」+ 对齐参考图清爽卡头标准。
function SecHead({ n, title, sub, action, badge }: {
  n: number;
  title: string;
  sub: string;
  action?: React.ReactNode;
  badge?: string;
}) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex min-w-0 items-center gap-2.5">
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary/10 text-xs font-semibold text-primary">{n}</span>
        <div className="min-w-0">
          <h2 className="text-sm font-semibold leading-tight text-foreground">{title}</h2>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">{sub}</p>
        </div>
      </div>
      {(badge || action) ? (
        <div className="flex shrink-0 items-center gap-2">
          {badge ? <span className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[10px] font-medium text-muted-foreground">{badge}</span> : null}
          {action}
        </div>
      ) : null}
    </div>
  );
}

function FG({ label, children, aiFilled, confidence, action }: {
  label: string;
  children: React.ReactNode;
  aiFilled?: boolean;
  /** B5 (CTO-15.9 session 3 · 2026-04-25 · M1c §A.6) ai-fill 后置信度 */
  confidence?: 'strong' | 'medium' | 'weak';
  action?: React.ReactNode;
}) {
  return (
    <div className={cn(
      'space-y-1.5',
      // [CTO-13.0 2026-04-19 S1.3] AI 填过字段高亮 · 琥珀 ring + 顶部 badge
      aiFilled && 'relative rounded-lg ring-1 ring-amber-500/40 bg-amber-500/5 p-2 -m-2',
    )}>
      <div className="flex items-center justify-between gap-2">
        <label className={cn(
          'text-[11px] font-medium tracking-wide uppercase flex items-center gap-1',
          aiFilled ? 'text-amber-400' : 'text-muted-foreground',
        )}>
          {label}
          {aiFilled && (
            <span className="text-[9px] px-1 py-px rounded bg-amber-500/20 text-amber-400 font-normal">✨ AI 填充 · 请检查</span>
          )}
          {/* B5 · ConfidenceBadge 内联 · weak 字段橙底 · 提示代理重点核验 */}
          {confidence && (
            <span
              className={cn(
                'text-[9px] px-1 py-px rounded font-normal ml-1',
                confidence === 'strong' && 'bg-emerald-500/20 text-emerald-500',
                confidence === 'medium' && 'bg-sky-500/20 text-sky-500',
                confidence === 'weak' && 'bg-orange-500/30 text-orange-500',
              )}
              title={
                confidence === 'strong' ? '资料明确给出 · 直接抽取' :
                confidence === 'medium' ? '资料推断得出 · 行业经验合并' :
                '资料几乎无据 · 需代理人工核验'
              }
            >
              {confidence === 'strong' ? '强' : confidence === 'medium' ? '中' : '弱 · 需复核'}
            </span>
          )}
        </label>
        {/* [CTO-13.0 2026-04-19 S1.5] action slot · 用于挂 PolishButton 等字段级 AI 按钮 */}
        {action && <div className="shrink-0">{action}</div>}
      </div>
      {children}
    </div>
  );
}

// [CTO-13.0 2026-04-19 S1.1] 深度营销资料 Section — 显示 ai-fill 顾问版深度字段
// 默认折叠，有任一字段有内容自动展开
type DeepMarketingForm = {
  company_intro: string;
  core_value: string;
  selling_points: string;
  success_cases: string;
  testimonials: string;
};

interface DeepMarketingSectionProps {
  form: DeepMarketingForm & Record<string, unknown>;
  setForm: (updater: (f: any) => any) => void;
  aiFilledFields?: Set<string>;
  clearAiHighlight?: (key: string) => void;
  profileId?: string | number;  // S1.5 PolishButton 需要
}

function DeepMarketingSection({ form, setForm, aiFilledFields, clearAiHighlight, profileId }: DeepMarketingSectionProps) {
  const hasAny = !!(form.company_intro || form.core_value || form.selling_points || form.success_cases || form.testimonials);
  const [open, setOpen] = useState(hasAny);

  // 有内容时自动展开一次（从后端 reload 后）
  useEffect(() => {
    if (hasAny) setOpen(true);
  }, [hasAny]);

  const fields: Array<{ key: keyof DeepMarketingForm; label: string; placeholder: string; multi?: boolean }> = [
    { key: 'company_intro',   label: '公司简介',        placeholder: '200 字内，突出优势 + 差异化（AI 顾问版会自动填）', multi: true },
    { key: 'core_value',      label: '核心价值主张',    placeholder: '一句话：你给客户带来的独特价值' },
    { key: 'selling_points',  label: '核心卖点',        placeholder: '分点列出（每行一条），AI 顾问版会自动生成', multi: true },
    { key: 'success_cases',   label: '成功案例摘要',    placeholder: '含客户名 + 效果数据', multi: true },
    { key: 'testimonials',    label: '客户证言',        placeholder: '客户评价原话', multi: true },
  ];
  const filledCount = fields.filter(({ key }) => !!String(form[key] || '').trim()).length;

  return (
    <div className="space-y-2 rounded-2xl border border-dashed border-border/60 bg-muted/10 p-3 sm:p-4">
      <button
        onClick={() => setOpen(!open)}
        className="flex min-h-[44px] w-full items-center gap-3 rounded-lg px-1 text-left transition-colors hover:bg-accent/30"
      >
        <Sparkles className="h-3.5 w-3.5 text-amber-400 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
            营销写作资料
            <span className="rounded-full border border-border/60 px-2 py-0.5 text-[10px] font-normal text-muted-foreground">
              已填 {filledCount}/5
            </span>
          </div>
          <div className="text-[11px] text-muted-foreground">写文章、报价和客户确认链接会读取这些内容</div>
        </div>
        <ChevronDown className={cn('h-3.5 w-3.5 text-muted-foreground shrink-0 transition-transform', open ? '' : '-rotate-90')} />
      </button>
      {open && (
        <div className="space-y-3 pt-1">
          {fields.map(({ key, label, placeholder, multi }) => {
            const val = (form[key] as string) || '';
            return (
              <FG
                key={key}
                label={label}
                aiFilled={aiFilledFields?.has(key)}
                action={profileId && val ? (
                  <PolishButton
                    profileId={profileId}
                    field={key}
                    currentValue={val}
                    label={label}
                    onAccept={(v) => { setForm(f => ({ ...f, [key]: v })); clearAiHighlight?.(key); }}
                  />
                ) : null}
              >
                {multi ? (
                  <textarea
                    value={val}
                    onChange={(e) => { setForm(f => ({ ...f, [key]: e.target.value })); clearAiHighlight?.(key); }}
                    placeholder={placeholder}
                    rows={3}
                    className="min-h-[96px] w-full resize-none rounded-lg border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                    maxLength={800}
                  />
                ) : (
                  <Input
                    value={val}
                    onChange={(e) => { setForm(f => ({ ...f, [key]: e.target.value })); clearAiHighlight?.(key); }}
                    placeholder={placeholder}
                  />
                )}
              </FG>
            );
          })}
        </div>
      )}
    </div>
  );
}

const MATERIAL_FIELD_LABELS: Record<string, string> = {
  company_intro: '公司介绍',
  core_value: '价值主张',
  usp: '差异化卖点',
  selling_points: '核心卖点',
  products: '产品服务',
  customers: '目标客户',
  cases: '案例',
  testimonials: '证言',
};

function materialFieldLabel(field: string): string {
  return MATERIAL_FIELD_LABELS[field] || field;
}

function materialConfirmFullUrl(path?: string | null): string {
  if (!path) return '';
  if (/^https?:\/\//i.test(path)) return path;
  return `${window.location.origin}${path}`;
}

function apiErrorMessage(error: any, fallback: string): string {
  const detail = error?.response?.data?.detail || error?.response?.data?.message;
  if (typeof detail === 'string' && detail.trim()) return detail;
  const msg = error?.message;
  if (typeof msg === 'string' && msg && !msg.includes('Request failed')) return msg;
  return fallback;
}

function ClientMaterialConfirmCard({
  brandId,
  brandName,
  onMaterialsUpdated,
  compact = false,
  onOpenKnowledge,
}: {
  brandId: number;
  brandName: string;
  onMaterialsUpdated?: () => void | Promise<void>;
  compact?: boolean;
  onOpenKnowledge?: () => void;
}) {
  const [status, setStatus] = useState<MaterialConfirmStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [generatingLink, setGeneratingLink] = useState(false);
  /* [WO_253 ②] 服务期提示**常驻**在链接旁,不是 toast 一闪而过 ——
     客户要拿着它去续费,一闪而过的东西当不了出口。 */
  const [serviceNotice, setServiceNotice] = useState<ReturnType<typeof noticeView>>(null);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const next = await materialConfirmApi.getStatus(brandId);
      setStatus(next);
    } catch (error: any) {
      // [D 兜底 2026-06-03] 读取失败显式置错误态 · 渲染失败提示 + 重试 · finally 必 setLoading(false) 不无限"读取中"
      setLoadError(true);
      toast.error(apiErrorMessage(error, '写作资料状态读取失败'));
    } finally {
      setLoading(false);
    }
  }, [brandId]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  // [B 联动 2026-06-03] 顶部「一键整理」成功后广播 brand:materials-cleaned · 这里自动重拉状态(无需手动点刷新)
  useEffect(() => {
    const h = () => { void loadStatus(); };
    window.addEventListener('brand:materials-cleaned', h);
    return () => window.removeEventListener('brand:materials-cleaned', h);
  }, [loadStatus]);

  const summary: MaterialsSummary | undefined = status?.materials_summary;
  const confirmUrl = materialConfirmFullUrl(status?.token_url);
  const filledCount = summary?.filled_count ?? 0;
  const totalFields = summary?.total_fields ?? 8;
  const missingFields = summary?.fields_missing?.slice(0, 4) || [];
  const canGenerateLink = status?.can_generate_link === true;
  const hasUsableMaterials = canGenerateLink || filledCount > 0 || status?.status === 'confirmed';

  const copyText = async (text: string, message = '已复制') => {
    if (!text) return;
    try {
      await navigator.clipboard?.writeText(text);
      toast.success(message);
    } catch {
      toast.error('复制失败，请手动复制');
    }
  };

  const handleGenerateLink = async () => {
    setGeneratingLink(true);
    try {
      const result = status?.status === 'feedback'
        ? await materialConfirmApi.resend(brandId)
        : await materialConfirmApi.generateLink(brandId, false);
      /*
       * 🔴 [WO_253 ② P0] 服务期信息**不是错误**。原来它走 catch ⇒
       *    链接明明生成了,用户只看到一句红色报错,既不知道怎么续费,也拿不到链接。
       *    「有话要说」≠「这次失败了」—— 把提示塞进 catch,代价是**把出口一起吞掉**。
       * 🔴 解析收在 `parseServiceNotice` 一处:认不出来就 `null`(不提示),
       *    绝不猜一个状态 —— 猜错会对客户说一句不成立的服务期结论。
       */
      setServiceNotice(noticeView(parseServiceNotice(
        (result as unknown as Record<string, unknown>)?.service_notice)));
      const nextUrl = materialConfirmFullUrl(result.url);
      if (nextUrl) {
        await copyText(nextUrl, '确认链接已生成并复制');
      } else {
        toast.success('确认链接已生成');
      }
      await loadStatus();
    } catch (error: any) {
      toast.error(apiErrorMessage(error, '确认链接生成失败'));
    } finally {
      setGeneratingLink(false);
    }
  };

  const copyWechatScript = async () => {
    if (!confirmUrl || !status) return;
    const script = buildWechatScript(status.brand_name || brandName, confirmUrl, status.status);
    await copyText(script, '微信话术已复制');
  };

  // [D 兜底 2026-06-03] 首次读取失败且无任何状态 → 显式失败卡 + 重试 · 不停在"读取中"
  if (loadError && !status && !loading) {
    return (
      <div className={cn(
        'rounded-xl border border-rose-500/30 bg-rose-500/5',
        compact ? 'p-3 sm:p-4' : 'p-4 sm:p-5',
      )}>
        <div className="flex flex-col items-center gap-2 py-4 text-center">
          <h3 className="text-sm font-semibold text-foreground">写作资料确认</h3>
          <p className="text-xs text-rose-500">加载失败，请点重试</p>
          <Button variant="outline" size="sm" className="h-8 text-xs" onClick={() => { void loadStatus(); }}>
            <RefreshCw className="h-3.5 w-3.5 mr-1" />重试
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className={cn(
      'rounded-xl border space-y-4',
      compact ? 'p-3 sm:p-4' : 'p-4 sm:p-5',
      hasUsableMaterials
        ? 'border-emerald-500/20 bg-emerald-500/5'
        : 'border-amber-500/25 bg-amber-500/5',
    )}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex items-start gap-3 min-w-0">
          <div className={cn(
            'h-10 w-10 rounded-lg flex items-center justify-center shrink-0',
            hasUsableMaterials ? 'bg-emerald-500/10 text-emerald-400' : 'bg-amber-500/10 text-amber-400',
          )}>
            {status?.status === 'confirmed' ? <ShieldCheck className="h-5 w-5" /> : <Link2 className="h-5 w-5" />}
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-foreground">写作资料确认</h3>
              {status ? (
                <MaterialConfirmStatusBadge status={status.status} />
              ) : (
                <Badge variant="outline" className="text-[11px]">读取中</Badge>
              )}
              {loading && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
            </div>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              {compact
                ? `写作大厅读取同一份资料 · ${filledCount}/${totalFields}项 · ${summary?.cases_count || 0}个案例`
                : '将客户资料整理成写作素材，生成 /m 确认链接发给客户确认，写作大厅会读取同一份资料。'}
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 sm:justify-end">
          {onOpenKnowledge && (
            <Button variant="outline" size="sm" className="h-8 text-xs" onClick={onOpenKnowledge}>
              <BookMarked className="h-3.5 w-3.5 mr-1" />知识库
            </Button>
          )}
          <Button
            size="sm"
            className="h-8 text-xs"
            onClick={handleGenerateLink}
            disabled={!canGenerateLink || generatingLink}
            variant={canGenerateLink ? 'default' : 'outline'}
          >
            {generatingLink ? <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" /> : <Send className="h-3.5 w-3.5 mr-1" />}
            {status?.status === 'feedback' ? '处理后重发' : status?.has_session ? '更新确认链' : '生成确认链'}
          </Button>
          <Button variant="ghost" size="sm" className="h-8 w-8 p-0" onClick={() => { void loadStatus(); }} disabled={loading}>
            <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} />
          </Button>
        </div>
      </div>

      {/*
        * 🔴 [WO_253 ②] 服务期提示 —— **出声 + 出口**。
        *    它与"链接展不展示"**完全解耦**:本块只负责说话,
        *    链接由它自己那块负责展示。原缺陷正是把两者绑在了一起
        *    (提示走 catch ⇒ 链接跟着没了)。
        */}
      {serviceNotice && (
        <div
          data-testid="service-notice"
          className={cn(
            'flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2 text-xs leading-relaxed',
            serviceNotice.tone === 'warning'
              ? 'border-amber-500/25 bg-amber-500/5 text-amber-500'
              : 'border-border/40 bg-background/40 text-info-secondary',
          )}
        >
          <span>{serviceNotice.text}</span>
          {serviceNotice.renewUrl && (
            <a
              data-testid="service-notice-renew"
              href={serviceNotice.renewUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="underline underline-offset-2 font-medium"
            >
              续费 →
            </a>
          )}
        </div>
      )}

      {/* [2026-06-03 入口去重] 还没整理过资料 → 引导用顶部「一键整理」(此处不再重复放整理按钮) */}
      {!canGenerateLink && status?.status === 'none' && (
        <div className="rounded-lg border border-amber-500/25 bg-amber-500/5 px-3 py-2 text-xs leading-relaxed text-amber-500">
          还没整理过这份资料 · 先点页面顶部「让 AI 帮你整理客户资料」，整理好后这里就能生成确认链了。整理完点右上角刷新看最新状态。
        </div>
      )}

      {!compact && (
        /* Phase 5 · grid 改 2x2(mobile) → 4 列(sm+)· 防 mobile 单列 stretch · 字号 token 化数字 vs label */
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="rounded-lg border border-border/40 bg-background/40 px-3 py-2">
            <div className="text-info-primary">{filledCount}/{totalFields}</div>
            <div className="text-info-tertiary">资料项</div>
          </div>
          <div className="rounded-lg border border-border/40 bg-background/40 px-3 py-2">
            <div className="text-info-primary">{summary?.selling_points_count || 0}</div>
            <div className="text-info-tertiary">卖点</div>
          </div>
          <div className="rounded-lg border border-border/40 bg-background/40 px-3 py-2">
            <div className="text-info-primary">{summary?.cases_count || 0}</div>
            <div className="text-info-tertiary">案例</div>
          </div>
          <div className="rounded-lg border border-border/40 bg-background/40 px-3 py-2">
            <div className="text-info-primary">{summary?.testimonials_count || 0}</div>
            <div className="text-info-tertiary">客户证言</div>
          </div>
        </div>
      )}

      {(!compact || status?.customer_notes) && (summary?.intro_excerpt || summary?.usp_excerpt || missingFields.length > 0 || status?.customer_notes) && (
        <div className="space-y-2 text-sm">
          {summary?.intro_excerpt && (
            <p className="line-clamp-2 text-muted-foreground">
              <span className="font-medium text-foreground">公司：</span>{summary.intro_excerpt}
            </p>
          )}
          {summary?.usp_excerpt && (
            <p className="line-clamp-2 text-muted-foreground">
              <span className="font-medium text-foreground">卖点：</span>{summary.usp_excerpt}
            </p>
          )}
          {missingFields.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-muted-foreground">待补</span>
              {missingFields.map(field => (
                <span key={field} className="rounded-md border border-border/50 bg-background/40 px-2 py-0.5 text-[11px] text-muted-foreground">
                  {materialFieldLabel(field)}
                </span>
              ))}
            </div>
          )}
          {status?.customer_notes && (
            <div className="rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-500">
              客户反馈：{status.customer_notes}
            </div>
          )}
        </div>
      )}

      {confirmUrl && (
        <div className="flex flex-col gap-2 rounded-lg border border-border/50 bg-background/50 px-3 py-2 sm:flex-row sm:items-center">
          <Link2 className="hidden h-4 w-4 text-muted-foreground sm:block" />
          <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">{confirmUrl}</span>
          <div className="flex shrink-0 items-center gap-1.5">
            <Button variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={() => { void copyText(confirmUrl, '确认链接已复制'); }}>
              <Copy className="h-3.5 w-3.5 mr-1" />复制
            </Button>
            <Button variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={() => { void copyWechatScript(); }}>
              <MessageCircle className="h-3.5 w-3.5 mr-1" />话术
            </Button>
            <a
              href={confirmUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex h-7 items-center rounded-md px-2 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
            >
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          </div>
        </div>
      )}

    </div>
  );
}

function BrandKnowledgeManager({ brandId, embedded }: { brandId: number; embedded?: boolean }) {
  const [confirmDialog, askConfirm] = useConfirmDialog();
  const [files, setFiles] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [uploading, setUploading] = useState(false);

  const fetchFiles = async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const res = await authApi.get(`/api/knowledge/client`, { params: { kb_id: String(brandId) } });
      setFiles(res.data.documents || []);
    } catch {
      // [D 兜底 2026-06-03] 读取失败显式置错误态 · 让下方渲染失败提示 + 重试 · 不停在"加载中"
      setLoadError(true);
      setFiles([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchFiles(); }, [brandId]);

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    // [2026-06-13 客户报障] 单文件 ≤ 20MB:超限当场明确拒绝。原先此入口无任何大小检查,35MB 大文件会过
    // nginx(50M)走到后端解析→慢/超时→只弹"可能仍在处理·请刷新"→客户以为系统 BUG。现前置硬拦+人话提示。
    const MAX_UPLOAD = 20 * 1024 * 1024;
    if (file.size > MAX_UPLOAD) {
      toast.error(`文件超过 20MB（当前 ${(file.size / 1024 / 1024).toFixed(1)}MB）· 请压缩或拆分后再上传`, { duration: 6000 });
      if (e.target) e.target.value = '';
      return;
    }
    setUploading(true);
    try {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('kb_type', 'client');
      formData.append('kb_id', String(brandId));
      await authApi.post('/api/knowledge/upload-file', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 300000,
      });
      toast.success('上传成功');
      fetchFiles();
    } catch (err: any) {
      // [2026-06-07 P0 fix] 区分超时/网络断 vs 真失败(对齐 WritingHall.uploadKnowledgeFiles)
      //   axios timeout → err.code='ECONNABORTED' or msg 含 'timeout' · 网络错 → 'Network Error'
      //   prod 实证后端 chunks 已入库 · 客户看到"失败"会重复上传 · UX 灾难 · 改"可能仍在处理"
      //   注:此组件无 30s 自动刷新(避免 unmount 写 state warning) · 提示用户手动刷新
      const msg = String(err?.message || '');
      const isTimeout = err?.code === 'ECONNABORTED' || /timeout|network/i.test(msg);
      if (isTimeout) {
        toast.info('文件较大或网络中断 · 服务器可能仍在处理 · 请稍后刷新查看结果', { duration: 6000 });
      } else {
        const errDetail = err.response?.data?.error || err.response?.data?.detail;
        toast.error(typeof errDetail === 'string' ? errDetail : '上传失败');
      }
    }
    setUploading(false);
    e.target.value = '';
  };

  const handleDelete = async (filename: string) => {
    if (!(await askConfirm({ title: `确定删除 "${filename}"？`, confirmLabel: '删除', danger: true }))) return;
    try {
      await authApi.delete(`/api/knowledge/client/${brandId}/${encodeURIComponent(filename)}`);
      toast.success('已删除');
      fetchFiles();
    } catch { toast.error('删除失败'); }
  };

  return (
    /* [2026-06-04] embedded:外层 section 已是卡(编号卡头+副标),这里去掉自带边框/标题/描述,避免卡中卡双标题 */
    <div className={embedded ? 'space-y-3' : 'space-y-3 rounded-2xl border border-border/60 bg-card/40 p-3 sm:p-4'}>
      <div className={cn('flex flex-col gap-3 sm:flex-row sm:items-center', embedded ? 'sm:justify-end' : 'sm:justify-between')}>
        {!embedded && (
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-foreground">客户资料文件</h3>
              <Badge variant="outline" className="text-[10px]">{files.length} 个文件</Badge>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              上传官网介绍、产品手册、案例、报价单，诊断、报价和写作会读取同一份资料。
            </p>
            <p className="mt-0.5 text-[11px] text-muted-foreground/70">
              支持 PDF / Word / PPT / TXT · 单个文件 ≤ 20MB（过大请压缩或拆分）
            </p>
          </div>
        )}
        <label className="flex min-h-[44px] w-full cursor-pointer items-center justify-center gap-2 rounded-xl border border-dashed border-border/70 px-4 py-2 text-xs text-muted-foreground transition-colors hover:bg-muted/30 sm:w-auto sm:min-w-[180px]">
          <input type="file" accept=".pdf,.docx,.txt,.md,.pptx" onChange={handleUpload} className="hidden" />
          {uploading ? (
            <><RefreshCw className="h-3.5 w-3.5 animate-spin" />上传中...</>
          ) : (
            <>上传客户资料</>
          )}
        </label>
      </div>

      {/* 文件列表 */}
      {loading ? (
        <p className="text-xs text-muted-foreground text-center py-4">加载中...</p>
      ) : loadError ? (
        /* [D 兜底 2026-06-03] 读取失败显式提示 + 重试 · 不无限"加载中" */
        <div className="rounded-xl border border-dashed border-rose-500/40 bg-rose-500/5 py-6 text-center">
          <p className="text-sm text-rose-500">加载失败，请点重试</p>
          <Button variant="outline" size="sm" className="mt-2 h-8 text-xs" onClick={() => { void fetchFiles(); }}>
            <RefreshCw className="h-3.5 w-3.5 mr-1" />重试
          </Button>
        </div>
      ) : files.length === 0 ? (
        /* [A 空状态可点 2026-06-03] 整块 label 包 hidden input · 点任意处即触发上传 · 复用 handleUpload */
        <label className="block cursor-pointer rounded-xl border border-dashed border-border/50 bg-background/40 py-6 text-center transition-colors hover:bg-muted/30">
          <input type="file" accept=".pdf,.docx,.txt,.md,.pptx" onChange={handleUpload} className="hidden" />
          <p className="text-sm text-muted-foreground">还没有上传资料文件 · 点这里上传</p>
          <p className="mt-1 text-xs text-muted-foreground/70">先放产品、案例和客户介绍，后面写作会少返工。</p>
          <p className="mt-0.5 text-[11px] text-muted-foreground/60">支持 PDF / Word / PPT / TXT · 单个文件 ≤ 20MB</p>
        </label>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border/30">
          {files.map((f: any, i: number) => (
            <div key={f.filename || i} className={cn(
              "flex flex-col gap-2 px-3 py-3 text-xs sm:flex-row sm:items-center sm:justify-between",
              i > 0 && "border-t border-border/20"
            )}>
              <div className="flex-1 min-w-0">
                <span className="text-foreground truncate block">{f.filename}</span>
                <span className="text-muted-foreground/60">{f.size ? `${(f.size / 1024).toFixed(0)} KB` : ''}</span>
              </div>
              <button onClick={() => handleDelete(f.filename)}
                className="min-h-[36px] cursor-pointer rounded-md border border-transparent bg-transparent px-2 text-left text-xs text-red-400/70 hover:border-red-400/20 hover:text-red-400 sm:ml-2 sm:text-center">
                删除
              </button>
            </div>
          ))}
        </div>
      )}
      {confirmDialog}
    </div>
  );
}

function TagInput({ values: rawValues, onChange, placeholder }: { values: string[] | string; onChange: (v: string[]) => void; placeholder: string }) {
  const values: string[] = Array.isArray(rawValues)
    ? rawValues
    : (typeof rawValues === 'string' && rawValues
        ? (rawValues as string).split(/[,，、]+/).map((s: string) => s.trim()).filter(Boolean)
        : []);
  const [input, setInput] = useState('');

  // 支持顿号、逗号、回车、空格分隔，实时添加
  const addTags = (text: string) => {
    const items = text.split(/[,，、\n\s]+/).map((s: string) => s.trim()).filter(Boolean);
    const newValues = [...values];
    for (const item of items) {
      if (!newValues.includes(item)) newValues.push(item);
    }
    if (newValues.length > values.length) onChange(newValues);
    setInput('');
  };

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value;
    // 检测分隔符触发自动添加（输入逗号、顿号时立即添加）
    if (/[,，、\n]/.test(val)) {
      addTags(val);
    } else {
      setInput(val);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && input.trim()) {
      e.preventDefault();
      addTags(input);
    }
  };

  // 失焦时自动添加
  const handleBlur = () => {
    if (input.trim()) addTags(input);
  };

  return (
    <div>
      <div className="flex flex-wrap gap-1.5 mb-2">
        {values.map((v: string, i: number) => (
          <span key={i} className="text-xs px-2 py-0.5 rounded-full border border-border/40 text-foreground flex items-center gap-1">
            {v}
            <button onClick={() => onChange(values.filter((_: string, j: number) => j !== i))}
              className="text-muted-foreground hover:text-foreground cursor-pointer bg-transparent border-none p-0 text-xs">x</button>
          </span>
        ))}
      </div>
      <Input value={input} onChange={handleChange} onKeyDown={handleKeyDown} onBlur={handleBlur}
        placeholder="输入后用逗号或顿号分隔，自动添加" className="h-8 text-xs" />
    </div>
  );
}

/** IP 人设 tab — 只读展示 + 跳转入口 */
function PersonaSummary({ profile, navigate, onInvite }: {
  profile: any;
  navigate: (path: string) => void;
  onInvite: () => void;
}) {
  const [bgExpanded, setBgExpanded] = useState(false);

  const positioning = profile?.persona_positioning;
  const tone = profile?.persona_tone;
  const rawCatch = profile?.persona_catchphrases;
  const catchphrases: string[] = Array.isArray(rawCatch) ? rawCatch : (typeof rawCatch === 'string' && rawCatch ? rawCatch.split(/[,，、]+/).map((s: string) => s.trim()).filter(Boolean) : []);
  const rawQuotes = profile?.persona_golden_quotes;
  const goldenQuotes: string[] = Array.isArray(rawQuotes) ? rawQuotes : (typeof rawQuotes === 'string' && rawQuotes ? rawQuotes.split(/[,，、]+/).map((s: string) => s.trim()).filter(Boolean) : []);
  const background: string = profile?.persona_background || '';

  const hasAnyData = positioning || tone || catchphrases.length > 0 || goldenQuotes.length > 0 || background;

  if (!profile || !hasAnyData) {
    return (
      <div className="space-y-4">
        <div className="text-center py-8 space-y-3">
          <p className="text-sm text-muted-foreground">还没有画像数据，通过以下方式开始建档</p>
        </div>
        {/* [WO_260] 原三颗建档按钮跳社媒(/s 一族)已删:本组件只在 c-end「IP 人设」tab 渲染,c-end 无调用方。 */}
      </div>
    );
  }

  return (
    <div className="space-y-4 p-4 sm:p-6">
      {/* 画像摘要卡片 */}
      <div className="bg-card border border-border/50 rounded-xl p-4 space-y-3">
        {positioning && (
          <div className="space-y-1">
            <p className="text-info-tertiary font-semibold tracking-wider uppercase">人设定位</p>
            <p className="text-sm text-foreground">{positioning}</p>
          </div>
        )}

        {tone && (
          <div className="space-y-1">
            <p className="text-info-tertiary font-semibold tracking-wider uppercase">说话风格</p>
            <p className="text-sm text-foreground">{tone}</p>
          </div>
        )}

        {catchphrases.length > 0 && (
          <div className="space-y-1.5">
            <p className="text-info-tertiary font-semibold tracking-wider uppercase">口头禅</p>
            <div className="flex flex-wrap gap-1.5">
              {catchphrases.map((phrase, i) => (
                <Badge key={i} variant="secondary" className="text-xs">{phrase}</Badge>
              ))}
            </div>
          </div>
        )}

        {goldenQuotes.length > 0 && (
          <div className="space-y-1.5">
            <p className="text-info-tertiary font-semibold tracking-wider uppercase">金句</p>
            <div className="space-y-1.5">
              {goldenQuotes.map((quote, i) => (
                <div key={i} className="border-l-2 border-green-400/50 pl-3 text-sm text-muted-foreground italic">
                  "{quote}"
                </div>
              ))}
            </div>
          </div>
        )}

        {background && (
          <div className="space-y-1">
            <p className="text-info-tertiary font-semibold tracking-wider uppercase">背景故事</p>
            <div className={cn('text-sm text-muted-foreground', !bgExpanded && 'line-clamp-3')}>
              {background}
            </div>
            {background.length > 150 && (
              <button
                onClick={() => setBgExpanded(!bgExpanded)}
                className="text-xs text-muted-foreground/60 hover:text-foreground flex items-center gap-0.5"
              >
                {bgExpanded ? (<>收起 <ChevronUp className="size-3" /></>) : (<>展开 <ChevronDown className="size-3" /></>)}
              </button>
            )}
          </div>
        )}
      </div>

      {/* 跳转按钮 */}
      <div className="flex flex-wrap gap-3">
        {/* [WO_260] 同上,三颗跳社媒的按钮已删;「邀请面试」保留。 */}
        {profile?.id && (
          <Button size="sm" variant="outline" className="h-8 text-xs" onClick={onInvite}>
            <QrCode className="mr-1 h-3 w-3" />邀请面试
          </Button>
        )}
      </div>
    </div>
  );
}


// ========== 驾驶舱 Tab 组件 ==========

function ContentTab({ profileId }: { profileId?: string }) {
  const [scripts, setScripts] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!profileId) { setLoading(false); return; }
    authApi.get('/api/content/scripts', { params: { profile_id: profileId, limit: 20 } })
      .then(res => setScripts(res.data?.scripts || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [profileId]);

  if (loading) return <div className="py-8 text-center text-sm text-muted-foreground">加载中...</div>;
  if (!scripts.length) return (
    <div className="py-12 text-center space-y-2">
      <p className="text-sm text-muted-foreground">暂无内容记录</p>
      <p className="text-xs text-muted-foreground/60">在创作工坊生成脚本后会显示在这里</p>
    </div>
  );

  return (
    <div className="space-y-3">
      {scripts.map((s: any) => (
        <div key={s.id} className="rounded-xl border border-border bg-card p-4">
          <h4 className="text-sm font-semibold text-foreground">{s.title || '未命名文案'}</h4>
          <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
            {(s.full_script || s.script_content || '').slice(0, 100)}
          </p>
          <div className="flex gap-2 mt-2 text-xs text-muted-foreground">
            {s.platform && <span>{s.platform}</span>}
            {s.content_type && <span>{s.content_type}</span>}
            {s.created_at && <span>{new Date(s.created_at).toLocaleDateString('zh-CN')}</span>}
          </div>
        </div>
      ))}
    </div>
  );
}

function DiagnosisTab({ brandId }: { brandId: number }) {
  const [records, setRecords] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  /* 2026-05-22 BUG 3 修:活跃诊断 session(localStorage)· 显"生成中"占位行
   * NewDiagnosis 提交时 setItem · DiagnosisProgress 完成时 removeItem
   * 跨设备/清缓存看不到 · 同浏览器够用(老板 case:同手机切出去回来) */
  const [activeSession, setActiveSession] = useState<{ sessionId: string; brandId: number | null; brandName: string; startedAt: number } | null>(null);
  const navigate = useEmbeddedNavigate();

  useEffect(() => {
    /* Codex 复审 P2 修(2026-05-22):brandId 切换时 · 先清旧 active session state
     * 防 A 客户 active session 残留 · 切到 B 时显出 A 的占位行 */
    setActiveSession(null);

    /* 先 read localStorage · 用于 fetch 完后做"已完成则清"判断 */
    let pendingSession: { sessionId: string; brandId: number | null; brandName: string; startedAt: number } | null = null;
    try {
      const raw = localStorage.getItem('active_diagnosis_session');
      if (raw) {
        const s = JSON.parse(raw);
        const expired = Date.now() - (s.startedAt || 0) > 60 * 60 * 1000;
        if (expired) {
          localStorage.removeItem('active_diagnosis_session');
        } else if (s.brandId === brandId) {
          pendingSession = s;
        }
      }
    } catch { /* 静默 */ }

    authApi.get('/api/history', { params: { brand_id: brandId, limit: 20, days: 0 } })
      .then(res => {
        const data = res.data;
        const recs = Array.isArray(data) ? data : data?.records || data?.data || [];
        setRecords(recs);

        /* Codex 审 P2 修:records 已含同 session_id record → 诊断已完成
         * 但 DiagnosisProgress 没机会 removeItem(网络断/直接关 tab)· 占位最多误留 1h
         * 修:fetch records 后比对 · 命中清 localStorage + 不 setActiveSession */
        if (pendingSession) {
          const completed = recs.some((r: any) => r.session_id === pendingSession!.sessionId);
          if (completed) {
            try { localStorage.removeItem('active_diagnosis_session'); } catch { /* 静默 */ }
          } else {
            setActiveSession(pendingSession);
          }
        }
      })
      .catch(() => {
        /* fetch 失败 · 仍显占位(更保守 · 防告诉用户"没有"而实际跑着)*/
        if (pendingSession) setActiveSession(pendingSession);
      })
      .finally(() => setLoading(false));
  }, [brandId]);

  if (loading) return <div className="py-8 text-center text-sm text-muted-foreground">加载中...</div>;
  if (!records.length && !activeSession) return (
    <div className="py-12 text-center space-y-3">
      <p className="text-sm text-muted-foreground">还没做过品牌体检</p>
      <Button size="sm" className="h-8 text-xs" onClick={() => navigate('/diagnosis/new')}>
        开始第一次诊断
      </Button>
    </div>
  );

  const latestRecord = records[0];
  const activeElapsedSec = activeSession ? Math.floor((Date.now() - activeSession.startedAt) / 1000) : 0;
  const activeElapsedLabel = activeSession ? `${Math.floor(activeElapsedSec / 60)}分${activeElapsedSec % 60}秒` : '';

  return (
    <div className="space-y-3">
      {/* 2026-05-22 BUG 3:活跃诊断占位行 · 跳 progress page · 让用户看到"正在生成" */}
      {activeSession && (
        <div
          onClick={() => navigate(`/diagnosis/progress/${activeSession.sessionId}`)}
          className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 cursor-pointer hover:bg-amber-500/10 transition-colors"
        >
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 min-w-0">
              <Loader2 className="h-4 w-4 text-amber-400 animate-spin shrink-0" />
              <h4 className="text-sm font-semibold text-foreground break-words leading-snug">{activeSession.brandName}</h4>
            </div>
            <span className="text-xs font-medium text-amber-400 shrink-0">生成中</span>
          </div>
          <p className="text-xs text-muted-foreground mt-1">
            已用 {activeElapsedLabel} · 点击查看进度
          </p>
        </div>
      )}

      {/* 一键复测 */}
      {latestRecord && (
        <div className="flex justify-end">
          <Button size="sm" className="h-8 text-xs"
            onClick={() => navigate(`/diagnosis/new?retest=1&original_id=${latestRecord.id}&brand_name=${encodeURIComponent(latestRecord.brand_name || '')}&original_score=${latestRecord.total_score ?? latestRecord.score ?? ''}`)}>
            <RefreshCw className="h-3.5 w-3.5 mr-1" />一键复测
          </Button>
        </div>
      )}
      {records.map((r: any) => (
        <div
          key={r.id}
          onClick={() => navigate(`/diagnosis/report/${r.id}`)}
          className="rounded-xl border border-border bg-card p-4 cursor-pointer hover:bg-muted/50 transition-colors"
        >
          <div className="flex items-center justify-between">
            <h4 className="text-sm font-semibold text-foreground">{r.brand_name || '诊断记录'}</h4>
            <span className={cn(
              'text-sm font-bold',
              (r.total_score ?? r.score ?? 0) >= 60 ? 'text-green-400' : 'text-yellow-400',
            )}>
              {r.total_score ?? r.score ?? '--'}分
            </span>
          </div>
          <p className="text-xs text-muted-foreground mt-1">
            {r.level || r.diagnosis_scope || ''}
            {r.created_at && ` · ${new Date(r.created_at).toLocaleDateString('zh-CN')}`}
          </p>
        </div>
      ))}
    </div>
  );
}

function CorpusTab({ profileId }: { profileId?: string }) {
  const [corpus, setCorpus] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!profileId) { setLoading(false); return; }
    authApi.get('/api/social/corpus/my', { params: { profile_id: profileId, limit: 20 } })
      .then(res => setCorpus(res.data?.corpus || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [profileId]);

  if (loading) return <div className="py-8 text-center text-sm text-muted-foreground">加载中...</div>;
  if (!corpus.length) return (
    <div className="py-12 text-center space-y-2">
      <p className="text-sm text-muted-foreground">暂无语料</p>
      <p className="text-xs text-muted-foreground/60">上传音视频或粘贴文字，AI 会学习说话风格</p>
    </div>
  );

  return (
    <div className="space-y-3">
      {corpus.map((c: any) => (
        <div key={c.id} className="rounded-xl border border-border bg-card p-4">
          <h4 className="text-sm font-semibold text-foreground">{c.source_name || '语料'}</h4>
          <div className="flex gap-2 mt-1 text-xs text-muted-foreground">
            <span>{c.source_type === 'audio' ? '音频' : c.source_type === 'text' ? '文字' : c.source_type}</span>
            {c.word_count > 0 && <span>{c.word_count}字</span>}
            {c.created_at && <span>{new Date(c.created_at).toLocaleDateString('zh-CN')}</span>}
          </div>
        </div>
      ))}
    </div>
  );
}

function InteractionsTab({ brandId }: { brandId: number }) {
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!brandId) return;
    authApi.get('/api/agent/draft-workspace', { params: { brand_id: brandId } })
      .then(res => setItems(res.data?.drafts || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [brandId]);

  if (loading) return <div className="py-8 text-center text-muted-foreground text-sm">加载中...</div>;
  if (!items.length) return <div className="py-8 text-center text-muted-foreground text-sm">暂无询价记录，客户的关键词询价、竞品分析和备注会显示在这里。</div>;

  const TYPE_LABELS: Record<string, string> = {
    keyword_price: '关键词询价',
    competitor: '竞品分析',
    note: '备注',
    preference: '偏好',
  };

  return (
    <div className="space-y-3">
      {items.map((item: any) => (
        <div key={item.id} className="rounded-xl border border-border bg-card p-4">
          <div className="flex items-center justify-between">
            <h4 className="text-sm font-semibold text-foreground">{item.item_key}</h4>
            <span className="text-[11px] px-2 py-0.5 rounded-full bg-muted text-muted-foreground">
              {TYPE_LABELS[item.item_type] || item.item_type}
            </span>
          </div>
          {item.item_type === 'keyword_price' && item.item_data && (
            <div className="flex gap-4 mt-2 text-xs text-muted-foreground">
              <span>入门 ¥{item.item_data.entry?.price || '?'}</span>
              <span>标准 ¥{item.item_data.standard?.price || '?'}</span>
              <span>旗舰 ¥{item.item_data.flagship?.price || '?'}</span>
            </div>
          )}
          <div className="flex gap-2 mt-1 text-[11px] text-muted-foreground/60">
            <span>来源: {item.source === 'ai_chat' ? 'AI对话' : item.source}</span>
            {item.created_at && <span>{new Date(item.created_at).toLocaleDateString('zh-CN')}</span>}
            {item.expires_at && <span>有效至 {new Date(item.expires_at).toLocaleDateString('zh-CN')}</span>}
          </div>
        </div>
      ))}
    </div>
  );
}
