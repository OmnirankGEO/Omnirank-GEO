/**
 * 在线报价管道 — 销售端操作页面
 *
 * 纯线上流程：选择客户 → 扩展关键词 → 生成链接 → 客户选词 → 计算报价 → 审核发送 → 客户确认 → 签约收款
 * 与报价中心（线下）共享客户数据和定价引擎，但操作逻辑围绕「客户链接」展开
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useDirtyForm } from '@/hooks/useDirtyForm';
import { createPortal } from 'react-dom';
import { useSearchParams } from 'react-router-dom';
import { useClientContext } from '@/context/ClientContext';
import { shouldSwitchClient } from '@/pages/Publishing/publishClientScope';
import { toast } from 'sonner';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  ArrowRight, Check, Clock, Copy, ExternalLink, Loader2,
  Plus, RefreshCw, Search, Sparkles, Trash2,
  FileText, Send, CreditCard, AlertTriangle,
  Globe, Info, ChevronRight, Link2, Brain, User, X, RotateCcw,
} from 'lucide-react';
import { useIsMobile } from '@/hooks/useIsMobile';
import { useAuth } from '@/context/AuthContext';
import api, { formatApiErrorForDisplay } from '@/lib/api';
import type { Brand, DiagnosisRecord } from '@/lib/api';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { copyToClipboard } from '@/lib/copyUtils';
// [WO_QUOTE_LIST_DATE 2026-08-05] 单据日期口径单点(禁 updated_at 占日期位)
import { formatQuoteOrderDate, quoteOrderDateSortKey } from '@/lib/quoteOrderDate';
import { SUPER_RED_OCEAN_COPY, GUARANTEE_UNAVAILABLE_COPY } from '@/lib/wangjieTerminology';
import { WhyThisPrice } from './components/WhyThisPrice';
import { GapPlanExecution } from '@/components/gapPlan/GapPlanExecution';
import { QuoteProgressStages } from './components/QuoteProgressStages';
// P0.6 价格 sanity(CTO-15.7 2026-04-24 罗平事故)
import { analyzePriceSanity } from '@/components/PriceSanityBanner';
import { useMarkStepCompleted } from '@/hooks/useMarkStepCompleted';
import { FeatureTooltip } from '@/components/onboarding/FeatureTooltip';
import { isSandboxActive } from '@/sandbox/sandboxState';
import { useTutorialStage } from '@/sandbox/tutorialStage';
import { SandboxVideoSlot } from '@/sandbox/SandboxVideoSlot';
import { advanceSandboxClientSelect, advanceSandboxClientConfirm } from '@/sandbox/mockData';
/* CTO-15.23 Phase 4 · Density System v1 接入 · 老板 5/22 "文字密密麻麻"收口
 * - ExpandableMeta 收 recommendation_reason 双端(mobile line-clamp-2 + PC Tooltip line-clamp-2)
 *   · 统一短摘要 + "看详情"/tap 行为 · mobile 体感跟 PC 一致 */
import { ExpandableMeta } from '@/components/density';
import { CoveredKeywordsFade } from '@/pages/Selection/components/CoveredKeywordsFade';
import { useOrganization } from '@/context/OrganizationContext';
import { ActionableAlert } from '@/components/ui/actionable-alert';
import { ExcludedKeywordsPanel } from '@/components/quote/ExcludedKeywordsPanel';
import type { ExcludedKeyword, SuggestedKeyword } from '@/components/quote/ExcludedKeywordsPanel';
import QuotePricingControlBar from '@/components/pricing/QuotePricingControlBar';
import type { CurrentQuoteContext } from '@/components/pricing/QuotePricingControlBar';
import { PricingPrivacyProvider } from '@/components/pricing/pricingPrivacy';

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

interface Session {
  token: string;
  quote_id: number;
  brand_name: string;
  industry: string;
  city: string;
  status: string;
  selected_count: number;
  is_quote_placeholder?: boolean;
  selected_tier?: string;
  confirmed_total_price?: number;
  final_price?: number;
  expires_at?: string;
  created_at?: string;
  // 🔴 [WO_QUOTE_LIST_DATE 2026-08-05] `updated_at` 刻意**不进这个类型**:
  //    它是"行最后被写的时间",不是业务时间,曾被当单据日期渲染(会话 194:
  //    07-23 的单显示成 2026/8/4)。从类型里删掉 = 以后再想把它放回日期位
  //    会**编译不过**,而不是靠注释提醒。接口仍然返回它,只是本页不消费。
  keywords_submitted_at?: string;
  confirmed_at?: string;
}

interface TierInfo {
  label: string;
  target_share: number;
  ai_probability: string;
  stars: number;
  total_price: number;
  total_articles: number;
}

interface PricingKeyword {
  id: number;
  keyword: string;
  category_label: string;
  recommendation_reason?: string;
  entry: { price: number; articles: number };
  standard: { price: number; articles: number };
  flagship: { price: number; articles: number };
  intent?: string;
  // [SSOT business-governance-master §17.1] 当报价 DTO 携带统一引擎判定时，展示层
  // 必须以它为准；红标 isRedFlag 不得与 CommercialQueryPolicy 的可交付结论矛盾。
  commercial_delivery_eligible?: boolean;
  funnel_stage?: string;
  // [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 只有本次**真写进共享缓存**(或真命中未过期缓存)
  //   的词才有锁期;没锁的词后端显式给 null + price_lock_note 一句人话。
  //   渲染铁律:null 时**不许**显示任何锁定日期(前端自己算 +7 天 = 把刚修掉的假承诺又造回来)。
  price_locked_until?: string | null;
  price_lock_note?: string | null;
  // 定价维度
  difficulty_score?: number;
  value_score?: number;
  search_volume?: number;
  sem_price?: number;
  effective_competition?: number;
  search_probability?: number;
  // 审计结果
  audit_status?: string;
  audit_note?: string;
  price_before_audit?: number;
  // [2026-06-08] 超红海词:需单独报价(后端 selection_api 透传 · 三档价已归0 · 前端不裸 ¥0)
  super_red_ocean?: boolean;
  should_quote?: boolean;
  // [报价解释层一期 2026-06-13] 需人工复核(证据不足/模型分歧)· 护栏剥保证价(参考价待人工核)
  //   guarantee_unavailable 仅代理端可见(已从客户面脱敏)· 三档价已归 0 · 前端显「参考价·待核」不裸 ¥0
  needs_review?: boolean;
  guarantee_unavailable?: boolean;
}

interface PricingData {
  generated_at: string;
  tiers: { entry: TierInfo; standard: TierInfo; flagship: TierInfo };
  keywords: PricingKeyword[];
  // [真实第一] 数据断供词(不出价不兜底 · 重新计算报价自动补算)
  unavailable_keywords?: { keyword: string; reason: string }[];
  // [报价意图闸 2026-08-04] 不进付费交付的词(只读展示 · 不在 keywords 里 · 不计价)
  excluded_keywords?: ExcludedKeyword[];
  suggested_keywords?: SuggestedKeyword[];
  policy_gate_status?: string;
}

/* QuoteCoefficientPreview 已随「本次报价系数」编辑器迁到
   @/components/pricing/QuoteCoefficientEditor(WO_QUOTE_COEFFICIENT_PRIVATE_TOPBAR 2026-08-11)。
   本文件不再持有该类型 —— 想把系数编辑器搬回 Step4 正文会先在这里编译不过。 */

interface ExpandedKeyword {
  keyword: string;
  source: string;
  category: string;
  intent?: string;
  geo_recommend?: boolean;
  scope_match?: boolean;
  default_selected?: boolean;
  rejection_reason?: string;
  // [报价纠偏工单 2026-07-26 P0-4] 物理禁选只留四条硬边界，其余可人工放行
  hard_block?: boolean;
  hard_block_reason?: string;
  human_override_allowed?: boolean;
  /** [P1-7] 疑似竞品词 advisory 标注（不丢弃） */
  competitor_suspect?: boolean;
  advisory_reason?: string;
  /* ── [工单 GEO_COMMERCIAL_DOUBLE_INVERSION 2026-08-09 · T5] 三轴结构化决策 ──
     老的 geo_recommend / scope_match 两个布尔说不清"为什么"，于是全部原因被压成
     一句「该问法通常不会让 AI 推荐具体品牌、服务商、产品或方案」——
     地域过宽、业务不匹配、需澄清、真知识题四种完全不同的处置被显示成同一句话，
     操作员看不出该放行、该改词还是该删（R5）。现在按 reason_code 分组。 */
  commercial_intent?: 'commercial' | 'brand_direct' | 'knowledge' | 'uncertain' | 'seo_fragment';
  business_scope?: 'matched' | 'mismatched' | 'uncertain';
  geo_scope?: 'matched' | 'too_broad' | 'outside_market' | 'uncertain';
  reason_code?: string;
  reason_text?: string;
  reason_group?: string;
  business_scope_evidence?: string;
  geo_scope_evidence?: string;
  delivery_policy_version?: string;
}

/** [T5] 待确认区分组 —— 键与后端 `services/keyword_delivery_decision.GROUP_*` 逐字对应。
 *  🔴 前端**不做第二次分类**（工单 §8）：只按后端下发的 `reason_group` 归位，
 *     未知分组一律落 needs_confirm，绝不静默丢弃。 */
const REVIEW_GROUP_ORDER = [
  'geo_too_broad',
  'scope_mismatch',
  'needs_confirm',
  'knowledge',
  'geo_outside',
] as const;

const REVIEW_GROUP_META: Record<string, { title: string; hint: string; tone: string }> = {
  geo_too_broad: {
    title: '地域范围过宽',
    hint: '商业意图成立，但客户只服务本地市场；这些词面向全国，默认不计入交付。',
    tone: 'text-amber-400 border-amber-500/30 bg-amber-500/5',
  },
  scope_mismatch: {
    title: '与当前业务不匹配',
    hint: '这些是真实的商业问法，但求的东西不在客户已确认的业务范围内。',
    tone: 'text-orange-400 border-orange-500/30 bg-orange-500/5',
  },
  needs_confirm: {
    title: '需要确认客户是否会这样问',
    hint: '看不出是不是客户真实的成交问法，或还缺一项信息；你确认后可以放行。',
    tone: 'text-sky-400 border-sky-500/30 bg-sky-500/5',
  },
  knowledge: {
    title: '知识/教程类，不建议计入交付',
    hint: 'AI 回答这类问题时通常只讲概念，不会点名商家。',
    tone: 'text-muted-foreground border-border bg-muted/20',
  },
  geo_outside: {
    title: '地域超出服务范围',
    hint: '这些词写的是别的城市/地区，超出客户当前服务市场。',
    tone: 'text-rose-400 border-rose-500/30 bg-rose-500/5',
  },
};

function reviewGroupOf(keyword: ExpandedKeyword): string {
  const group = String(keyword.reason_group || '');
  if (group && REVIEW_GROUP_META[group]) return group;
  // 老后端（未升级）只有 geo_recommend/scope_match 两个布尔 —— 派生一个近似分组，
  // 保证混部期间不会有词消失。
  if (keyword.geo_recommend === false) return 'knowledge';
  if (keyword.scope_match === false) return 'needs_confirm';
  return 'needs_confirm';
}

/** [工单 P0-1] 范围锁定裁决结果（可人工修改后确认） */
interface ScopeLock {
  version?: string;
  service_market: string[];
  market_level: string;
  business_type: string;
  city_scope?: string;
  buyer_persona?: string;
  real_query_seeds?: string[];
  source?: string;
  used_diagnosis?: boolean;
  diagnosis_id?: number | null;
  reason?: string;
}

const MARKET_LEVEL_LABELS: Record<string, string> = {
  district: '街边店 / 到店（可下钻街道）',
  city: '城市级服务商（区级少量，禁街道）',
  regional: '区域型 / 城市群（只到城市级）',
  national: '全国 / 跨境（地域词 0-20%，只到城市级）',
};

interface DistilledData {
  target_audience?: string;
  core_business?: string;
  geographic_focus?: string;
  selling_points?: string[];
  unique_value?: string;
  use_scenarios?: string[];
  competitor_names?: string[];
  seed_keywords?: string[];
  target_cities?: string[];
  industry?: string;
}

/* ------------------------------------------------------------------ */
/*  Constants                                                          */
/* ------------------------------------------------------------------ */

const STEPS = [
  { key: 'generate', label: '生成 & 发送链接', icon: Link2, guide: '选择客户，AI 扩展关键词，生成选词链接发送给客户' },
  { key: 'customer_select', label: '客户选词', icon: FileText, guide: '客户打开链接选择关键词，提交后系统自动通知您' },
  { key: 'calculate', label: '计算报价', icon: RefreshCw, guide: '客户已选好关键词，一键算出每个词的报价' },
  { key: 'review', label: '审核 & 发送', icon: Search, guide: '审核关键词价格，删除异常词，确认后发送报价给客户' },
  { key: 'customer_confirm', label: '客户确认', icon: Check, guide: '客户选择套餐并确认报价，确认后进入签约阶段' },
  { key: 'payment', label: '签约收款', icon: CreditCard, guide: '确认签约金额，输入确认码完成收款，关键词自动进入写作大厅' },
] as const;

type StepKey = typeof STEPS[number]['key'];

const STATUS_TO_STEP: Record<string, StepKey> = {
  selecting: 'customer_select',
  business_lines_submitted: 'customer_select',  // 客户已选业务方向，操作员继续选词
  keywords_submitted: 'calculate',
  pricing_pending_review: 'review',
  adding_keywords: 'review',
  quoted: 'customer_confirm',
  confirmed: 'payment',
  pending_payment: 'payment',
  active: 'payment',
};

const STATUS_LABELS: Record<string, { text: string; color: string }> = {
  selecting: { text: '客户选词中', color: 'bg-blue-500/10 text-blue-400 border border-blue-500/20' },
  business_lines_submitted: { text: '客户已选业务方向', color: 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' },
  keywords_submitted: { text: '待计算报价', color: 'bg-yellow-500/10 text-yellow-400 border border-yellow-500/20' },
  pricing_pending_review: { text: '待审核价格', color: 'bg-orange-500/10 text-orange-400 border border-orange-500/20' },
  adding_keywords: { text: '客户追加词', color: 'bg-orange-500/10 text-orange-400 border border-orange-500/20' },
  quoted: { text: '客户确认中', color: 'bg-purple-500/10 text-purple-400 border border-purple-500/20' },
  confirmed: { text: '待签约', color: 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' },
  pending_payment: { text: '待收款', color: 'bg-amber-500/10 text-amber-400 border border-amber-500/20' },
  payment_overdue: { text: '收款超期', color: 'bg-red-500/10 text-red-400 border border-red-500/20' },
  active: { text: '已完成', color: 'bg-green-500/10 text-green-400 border border-green-500/20' },
  draft: { text: '待生成链接', color: 'bg-muted text-muted-foreground border border-border' },
  expired: { text: '已过期', color: 'bg-muted text-muted-foreground border border-border' },
};

/* ------------------------------------------------------------------ */
/*  标准行业分类 + 智能归类 —— **表与逻辑都在 `@/lib/industries`**         */
/*  🔴 [WO_250 2026-09-20] 这里原本有**第二份**表和第二个 normalizeIndustry。  */
/*     两份并不相同(OQF 多一个「珠宝饰品」类、金融服务写的是 `复核` 而非      */
/*     `审计`、服装纺织三个词不同、兜底一个返原文一个返「其他」)——           */
/*     「重复的同一张表」本身就是个未验断言,量过才知道它们早已各自演化。      */
/*     合表决定与差异清单见交付单;兜底用参数保住本页原行为(`'其他'`)。       */
/* ------------------------------------------------------------------ */
import { INDUSTRY_CATEGORIES, normalizeIndustry as normalizeIndustryShared } from '@/lib/industries';

/** 本页兜底返「其他」(原行为);分类表与匹配规则一律走公共 SSOT。 */
function normalizeIndustry(raw: string | undefined | null): string {
  return normalizeIndustryShared(raw, '其他');
}

function preferIndustryFromBusinessText(current: string, ...parts: Array<string | undefined | null>): string {
  const inferred = normalizeIndustry(parts.filter(Boolean).join(' '));
  if (inferred === '其他' || inferred === current) return current || '其他';
  if (!current || current === '其他' || current === '服装纺织') return inferred;
  return current;
}

function buildFallbackExpandedKeywords(params: {
  coreKeywords: string[];
  industry: string;
  city: string;
  brandName: string;
  targetCount: number;
}): ExpandedKeyword[] {
  const city = (params.city || '').split(/[,，、\s]/).map(s => s.trim()).find(Boolean) || '';
  const normalizedIndustry = normalizeIndustry(params.industry);
  const baseCores = params.coreKeywords
    .map(k => k.trim())
    .filter(Boolean)
    .slice(0, 8);
  const suffixes = [
    '推荐', '品牌推荐', '厂家推荐', '供应商推荐',
    '哪家好', '哪个好', '服务商对比', '品牌对比',
    '价格', '报价', '性价比', '口碑排名',
  ];
  const industrySeeds: Record<string, string[]> = {
    珠宝饰品: ['翡翠首饰定制', '中高端翡翠饰品', '翡翠礼品定制', '翡翠私人定制哪家好', '翡翠饰品品牌推荐'],
    建筑建材: ['本地装修公司推荐', '装修报价', '建材供应商推荐', '装修方案报价'],
    信息技术: ['软件开发服务商推荐', '企业数字化方案服务商对比', '系统集成公司推荐'],
  };
  const candidates: string[] = [];

  for (const suffix of suffixes) {
    for (const core of baseCores) {
      candidates.push(`${core}${suffix}`);
      if (city && city !== '全国') candidates.push(`${city}${core}${suffix}`);
    }
  }
  candidates.push(...(industrySeeds[normalizedIndustry] || []));

  const seen = new Set<string>();
  return candidates
    .map(k => k.replace(/\s+/g, '').trim())
    .filter(k => {
      if (!k || k.length < 3 || seen.has(k)) return false;
      seen.add(k);
      return true;
    })
    .slice(0, Math.max(8, params.targetCount))
    .map(keyword => ({
      keyword,
      source: '本地兜底（待确认）',
      category: '待人工确认',
      intent: 'unverified',
      geo_recommend: false,
      scope_match: true,
      default_selected: false,
    }));
}

function minimumExpansionCandidateCount(coreKeywords: string[], targetCount: number): number {
  const uniqueSeedCount = new Set(coreKeywords.map(k => k.trim().toLowerCase()).filter(Boolean)).size;
  return Math.min(Math.max(0, targetCount), Math.max(8, Math.min(20, uniqueSeedCount * 5)));
}

function mergeExpandedKeywordCandidates(
  groups: Array<Array<Partial<ExpandedKeyword>>>,
  limit: number,
): ExpandedKeyword[] {
  const merged: ExpandedKeyword[] = [];
  const seen = new Set<string>();
  for (const group of groups) {
    for (const item of group) {
      const keyword = typeof item?.keyword === 'string' ? item.keyword.trim() : '';
      const key = keyword.toLowerCase();
      if (keyword.length < 3 || seen.has(key)) continue;
      seen.add(key);
      merged.push({
        keyword,
        source: typeof item.source === 'string' && item.source ? item.source : 'AI扩展',
        category: typeof item.category === 'string' && item.category ? item.category : '通用',
        intent: typeof item.intent === 'string' ? item.intent : undefined,
        geo_recommend: typeof item.geo_recommend === 'boolean' ? item.geo_recommend : undefined,
        scope_match: typeof item.scope_match === 'boolean' ? item.scope_match : undefined,
        default_selected: typeof item.default_selected === 'boolean' ? item.default_selected : undefined,
        rejection_reason: typeof item.rejection_reason === 'string' ? item.rejection_reason : undefined,
        hard_block: typeof item.hard_block === 'boolean' ? item.hard_block : undefined,
        hard_block_reason: typeof item.hard_block_reason === 'string' ? item.hard_block_reason : undefined,
        human_override_allowed:
          typeof item.human_override_allowed === 'boolean' ? item.human_override_allowed : undefined,
        competitor_suspect:
          typeof item.competitor_suspect === 'boolean' ? item.competitor_suspect : undefined,
        advisory_reason: typeof item.advisory_reason === 'string' ? item.advisory_reason : undefined,
        // [T5] 三轴决策原样透传（前端不重算、不改判）
        commercial_intent: item.commercial_intent,
        business_scope: item.business_scope,
        geo_scope: item.geo_scope,
        reason_code: typeof item.reason_code === 'string' ? item.reason_code : undefined,
        reason_text: typeof item.reason_text === 'string' ? item.reason_text : undefined,
        reason_group: typeof item.reason_group === 'string' ? item.reason_group : undefined,
        business_scope_evidence:
          typeof item.business_scope_evidence === 'string' ? item.business_scope_evidence : undefined,
        geo_scope_evidence:
          typeof item.geo_scope_evidence === 'string' ? item.geo_scope_evidence : undefined,
        delivery_policy_version:
          typeof item.delivery_policy_version === 'string' ? item.delivery_policy_version : undefined,
      });
      if (merged.length >= limit) return merged;
    }
  }
  return merged;
}

function mergeRecommendedAndReviewCandidates(
  recommendedGroups: Array<Array<Partial<ExpandedKeyword>>>,
  reviewGroups: Array<Array<Partial<ExpandedKeyword>>>,
  recommendedLimit: number,
): ExpandedKeyword[] {
  const recommended = mergeExpandedKeywordCandidates(recommendedGroups, recommendedLimit);
  const recommendedKeys = new Set(recommended.map(item => item.keyword.toLowerCase()));
  const reviewItems = mergeExpandedKeywordCandidates(
    reviewGroups,
    Number.MAX_SAFE_INTEGER,
  ).filter(item => !recommendedKeys.has(item.keyword.toLowerCase()));
  return [...recommended, ...reviewItems];
}

function recommendedKeywordSet(keywords: ExpandedKeyword[]): Set<string> {
  return new Set(
    keywords
      .filter(isDefaultSelectedKeyword)
      .map(keyword => keyword.keyword),
  );
}

/**
 * 🔴 [包三 · 报价方案] **超出服务范围的词一票否决默认勾选。**
 *
 * 改前 `default_selected === true` 短路在最前面 ⇒ 后端只要给了 true,
 * 一个客户**根本不服务的地区**的词也会默认勾上、直接进报价。
 * 代理未必逐条看那一长串词,于是把不该卖的东西默认卖了出去 —— 这是钱的缺陷,不是文案缺陷。
 *
 * 新口径:超范围是**否决项**,压过任何"建议勾选"的信号;
 * 她仍可手动勾(超范围不是硬禁选,`isHardBlockedKeyword` 才是),只是不再替她默认决定。
 */
function isOutOfScopeKeyword(keyword: ExpandedKeyword): boolean {
  return String(keyword.reason_group || '') === 'geo_outside'
    || keyword.scope_match === false;
}

function isDefaultSelectedKeyword(keyword: ExpandedKeyword): boolean {
  if (isOutOfScopeKeyword(keyword)) return false;
  return keyword.default_selected === true
    || (keyword.default_selected === undefined
      && keyword.geo_recommend === true
      && keyword.scope_match !== false);
}

/** [工单 P0-4] 是否物理禁选：只有四条硬边界（法律/资金/越权/数据完整性）才禁。 */
function isHardBlockedKeyword(keyword: ExpandedKeyword): boolean {
  if (keyword.hard_block === true) return true;
  if (keyword.human_override_allowed === false) return true;
  return false;
}

/* ------------------------------------------------------------------ */
/*  Pipeline Stepper                                                   */
/* ------------------------------------------------------------------ */

function PipelineStepper({ currentStep }: { currentStep: StepKey }) {
  const currentIdx = STEPS.findIndex(s => s.key === currentStep);
  return (
    <div className="-mx-1 mb-3 flex items-center gap-1 overflow-x-auto px-1 pb-2 [scrollbar-width:none]">
      {STEPS.map((step, i) => {
        const Icon = step.icon;
        const done = i < currentIdx;
        const active = i === currentIdx;
        return (
          <div key={step.key} className="flex items-center shrink-0">
            <div className={`flex min-h-9 items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium transition-all sm:min-h-8 ${
              done ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' :
              active ? 'bg-foreground text-background shadow-md' :
              'bg-muted text-muted-foreground'
            }`}>
              {done ? <Check className="w-3.5 h-3.5" /> : <Icon className="w-3.5 h-3.5" />}
              <span className={active ? 'inline whitespace-nowrap' : 'hidden whitespace-nowrap xl:inline'}>{step.label}</span>
            </div>
            {i < STEPS.length - 1 && (
              <ChevronRight className={`w-4 h-4 mx-0.5 ${i < currentIdx ? 'text-emerald-400' : 'text-muted-foreground/50'}`} />
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Step Guide                                                         */
/* ------------------------------------------------------------------ */

function StepGuide({ currentStep }: { currentStep: StepKey }) {
  const step = STEPS.find(s => s.key === currentStep);
  if (!step) return null;
  const Icon = step.icon;
  return (
    <div className="mb-4 flex items-start gap-3 rounded-xl border border-border bg-card px-3 py-3 sm:px-4">
      <div className="w-8 h-8 rounded-full bg-brand flex items-center justify-center shrink-0 mt-0.5">
        <Icon className="w-4 h-4 text-white" />
      </div>
      <div>
        <p className="text-sm font-semibold text-foreground">{step.label}</p>
        <p className="text-xs text-muted-foreground mt-0.5">{step.guide}</p>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Session Sidebar                                                    */
/* ------------------------------------------------------------------ */

function SessionSidebar({
  sessions, selected, onSelect, onNew, onDelete, onBatchDelete, canArchive,
}: {
  sessions: Session[];
  selected?: string;
  onSelect: (token: string) => void;
  onNew: () => void;
  onDelete: (quoteId: number) => void;
  onBatchDelete: (quoteIds: number[]) => void;
  canArchive: boolean;
}) {
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [industryFilter, setIndustryFilter] = useState('');
  const [selectMode, setSelectMode] = useState(false);
  const [checkedTokens, setCheckedTokens] = useState<Set<string>>(new Set());

  // [WO_NO_SILENT_RELOAD_DIRTY_GUARD 2026-08-16 ①] 报价单侧接脏表单守卫。
  //   这里护的是**已勾选但还没提交的批量选择** —— 它和输入框一样是"用户做了一半的活",
  //   而 activeElement 那条退化兜底看不到它(勾选之后焦点通常不在输入元素上)。
  //   注册制存在的意义就在这种地方:只有组件自己知道自己脏没脏。
  useDirtyForm('quote.batch-selection', () => selectMode && checkedTokens.size > 0);

  // 2026-05-09 修(老板报"点击无法删除") · 替换 window.confirm
  // 真因:Chrome/Edge 用户曾勾过"不再显示对话框" → 后续 confirm() 自动返 false 不弹 · 老板感知点击无效
  // 修法:用 useConfirmDialog(应用内 AlertDialog · 不被浏览器拦)
  const [confirmDialog, confirm] = useConfirmDialog();

  // 提取已出现的标准行业分类（按出现频率排序）
  const industryCounts: Record<string, number> = {};
  sessions.forEach(s => {
    const cat = normalizeIndustry(s.industry);
    industryCounts[cat] = (industryCounts[cat] || 0) + 1;
  });
  const industries = Object.entries(industryCounts)
    .sort((a, b) => b[1] - a[1])
    .map(([cat, count]) => ({ cat, count }));

  const filtered = sessions.filter(s => {
    if (statusFilter && s.status !== statusFilter) return false;
    if (industryFilter && normalizeIndustry(s.industry) !== industryFilter) return false;
    if (searchText) {
      const q = searchText.toLowerCase();
      const name = (s.brand_name || '').toLowerCase();
      const ind = (s.industry || '').toLowerCase();
      if (!name.includes(q) && !ind.includes(q)) return false;
    }
    return true;
  });

  return (
    <>
    <div className="h-full w-full shrink-0 overflow-y-auto border-r border-border bg-muted md:w-72">
      <div className="p-3 border-b border-border bg-card">
        <Button onClick={onNew} size="sm" variant="secondary" className="w-full gap-1 bg-foreground/90 hover:bg-foreground text-background">
          <Plus className="w-4 h-4" /> 新建报价
        </Button>
      </div>

      {/* 筛选区 */}
      <div className="p-2 border-b border-border space-y-1.5 bg-card">
        <div className="relative">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
          <Input
            value={searchText}
            onChange={e => setSearchText(e.target.value)}
            placeholder="搜索客户名称 / 行业"
            className="h-8 text-xs pl-7 pr-2"
          />
        </div>
        <div className="flex gap-1.5">
          <select
            value={statusFilter}
            onChange={e => setStatusFilter(e.target.value)}
            className="flex-1 h-7 text-xs border border-border rounded px-1.5 bg-card text-foreground"
          >
            <option value="">全部状态</option>
            {Object.entries(STATUS_LABELS).map(([k, v]) => (
              <option key={k} value={k}>{v.text}</option>
            ))}
          </select>
          <select
            value={industryFilter}
            onChange={e => setIndustryFilter(e.target.value)}
            className="flex-1 h-7 text-xs border border-border rounded px-1.5 bg-card text-foreground"
          >
            <option value="">全部行业</option>
            {industries.map(({ cat, count }) => (
              <option key={cat} value={cat}>{cat} ({count})</option>
            ))}
          </select>
        </div>
        {(searchText || statusFilter || industryFilter) && (
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-muted-foreground">{filtered.length}/{sessions.length} 条结果</span>
            <button
              onClick={() => { setSearchText(''); setStatusFilter(''); setIndustryFilter(''); }}
              className="text-[10px] text-brand hover:underline"
            >清除筛选</button>
          </div>
        )}
      </div>

      {/* 多选操作栏 */}
      {canArchive && filtered.length > 0 && (
        <div className="px-2 py-1.5 border-b border-border bg-card flex items-center justify-between">
          {selectMode ? (
            <>
              <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                <input
                  type="checkbox"
                  checked={checkedTokens.size === filtered.length && filtered.length > 0}
                  onChange={e => {
                    if (e.target.checked) setCheckedTokens(new Set(filtered.map(s => s.token)));
                    else setCheckedTokens(new Set());
                  }}
                  className="rounded"
                />
                全选
              </label>
              <div className="flex items-center gap-1.5">
                {checkedTokens.size > 0 && (
                  <button
                    type="button"
                    onClick={async () => {
                      const selectedSessions = sessions.filter(session => checkedTokens.has(session.token));
                      const quoteIds = selectedSessions.map(session => session.quote_id);
                      try {
                        const previews = await Promise.all(
                          quoteIds.map(quoteId => api.get(`/api/quotes/${quoteId}/archive-preview`).then(response => response.data)),
                        );
                        const totals = previews.reduce(
                          (sum, preview) => ({
                            sessions: sum.sessions + preview.associations.selection_sessions,
                            keywords: sum.keywords + preview.associations.keywords,
                            topics: sum.topics + preview.associations.topics,
                            articles: sum.articles + preview.associations.articles,
                          }),
                          { sessions: 0, keywords: 0, topics: 0, articles: 0 },
                        );
                        const protectedCount = previews.filter(preview => preview.protected).length;
                        const ok = await confirm({
                          title: `归档选中的 ${checkedTokens.size} 个报价?`,
                          description: `将影响 ${totals.sessions} 个会话、${totals.keywords} 个关键词、${totals.topics} 个主题、${totals.articles} 篇文章；数据保留并可恢复。${protectedCount ? `其中 ${protectedCount} 个受订单状态保护，将拒绝归档。` : ''}`,
                          confirmLabel: `归档 ${checkedTokens.size} 个`,
                          danger: true,
                        });
                        if (!ok) return;
                        onBatchDelete(quoteIds);
                        setCheckedTokens(new Set());
                        setSelectMode(false);
                      } catch (error: any) {
                        toast.error(formatApiErrorForDisplay(error, '无法读取批量归档影响'));
                      }
                    }}
                    className="text-xs text-red-500 hover:text-red-400 font-medium"
                  >
                    归档({checkedTokens.size})
                  </button>
                )}
                <button onClick={() => { setSelectMode(false); setCheckedTokens(new Set()); }} className="text-xs text-muted-foreground hover:text-foreground">
                  取消
                </button>
              </div>
            </>
          ) : (
            <button onClick={() => setSelectMode(true)} className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1">
              <Trash2 className="w-3 h-3" /> 批量归档
            </button>
          )}
        </div>
      )}

      <div className="divide-y">
        {filtered.length === 0 && (
          <div className="p-6 text-center">
            <Globe className="w-8 h-8 text-muted-foreground/40 mx-auto mb-2" />
            <p className="text-sm text-muted-foreground">{sessions.length === 0 ? '暂无在线报价记录' : '无匹配结果'}</p>
            <p className="text-xs text-muted-foreground/60 mt-1">{sessions.length === 0 ? '点击上方按钮开始' : '尝试调整筛选条件'}</p>
          </div>
        )}
        {filtered.map(s => {
          const st = STATUS_LABELS[s.status] || { text: s.status, color: 'bg-muted text-muted-foreground border border-border' };
          return (
            <div
              key={s.token}
              onClick={() => selectMode
                ? setCheckedTokens(prev => { const next = new Set(prev); next.has(s.token) ? next.delete(s.token) : next.add(s.token); return next; })
                : onSelect(s.token)
              }
              className={`p-3 cursor-pointer hover:bg-card transition-colors ${
                selected === s.token && !selectMode ? 'bg-card border-l-3 border-brand' : ''
              } ${selectMode && checkedTokens.has(s.token) ? 'bg-red-500/10' : ''}`}
            >
              <div className="flex items-center gap-2">
                {selectMode && (
                  <input
                    type="checkbox"
                    checked={checkedTokens.has(s.token)}
                    readOnly
                    className="rounded shrink-0"
                  />
                )}
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-foreground break-words leading-snug" title={s.brand_name || ''}>{s.brand_name || '未命名'}</p>
                  <div className="flex items-center gap-2 mt-1">
                    <Badge className={`text-xs ${st.color}`}>{st.text}</Badge>
                    {s.selected_count > 0 && (
                      <span className="text-xs text-muted-foreground">{s.selected_count}词</span>
                    )}
                  </div>
                  <div className="flex items-center justify-between mt-0.5">
                    <div className="min-w-0">
                      {s.industry && <p className="text-xs text-muted-foreground break-words leading-snug">{normalizeIndustry(s.industry)}</p>}
                      {/* [WO_QUOTE_LIST_DATE 2026-08-05] 单据日期走业务时间,不再用
                          updated_at —— 那是"行最后被写的时间",会话 194 因此把 07-23
                          的老单显示成 2026/8/4。口径单点见 lib/quoteOrderDate。 */}
                      <p className="text-xs text-muted-foreground/60">
                        {formatQuoteOrderDate(s)}
                      </p>
                    </div>
                    {canArchive && !selectMode && (
                      <button
                        type="button"
                        onClick={async e => {
                          e.stopPropagation();
                          e.preventDefault();
                          try {
                            const { data: preview } = await api.get(`/api/quotes/${s.quote_id}/archive-preview`);
                            const counts = preview.associations;
                            const ok = await confirm({
                              title: `归档「${preview.object.display_name}」报价 #${preview.object.quote_id}?`,
                              description: `关联 ${counts.selection_sessions} 个会话、${counts.keywords} 个关键词、${counts.topics} 个主题、${counts.articles} 篇文章。公开链接会失效，数据保留并可恢复。${preview.blocked_reason || ''}`,
                              confirmLabel: '归档报价',
                              danger: true,
                            });
                            if (ok) onDelete(s.quote_id);
                          } catch (error: any) {
                            toast.error(formatApiErrorForDisplay(error, '无法读取归档影响'));
                          }
                        }}
                        className="shrink-0 p-1 rounded hover:bg-red-500/20 text-muted-foreground/50 hover:text-red-500 transition-colors"
                        title="删除"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    )}
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
    {confirmDialog}
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  Mobile Session Switcher                                            */
/* ------------------------------------------------------------------ */

function MobileSessionSwitcher({
  sessions, selected, isNew, onSelect, onNew,
}: {
  sessions: Session[];
  selected?: string;
  isNew: boolean;
  onSelect: (token: string) => void;
  onNew: () => void;
}) {
  const selectedSession = selected ? sessions.find(s => s.token === selected) : undefined;
  const status = selectedSession
    ? (STATUS_LABELS[selectedSession.status] || { text: selectedSession.status, color: 'bg-muted text-muted-foreground border border-border' })
    : null;

  return (
    <div className="mb-3 rounded-xl border border-border bg-card p-3 md:hidden">
      <div className="mb-2 flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs text-muted-foreground">报价单</p>
          <p className="text-sm font-semibold text-foreground break-words leading-snug" title={selectedSession?.brand_name || ''}>
            {isNew ? '新建报价' : (selectedSession?.brand_name || '选择历史报价')}
          </p>
        </div>
        <Button
          onClick={onNew}
          size="sm"
          variant={isNew ? 'secondary' : 'outline'}
          className="min-h-10 shrink-0 gap-1.5 px-3"
        >
          <Plus className="h-4 w-4" />
          新建
        </Button>
      </div>

      <select
        value={selected || ''}
        onChange={e => { if (e.target.value) onSelect(e.target.value); }}
        className="min-h-11 w-full rounded-lg border border-border bg-background px-3 text-sm text-foreground outline-none"
      >
        <option value="" disabled>{sessions.length > 0 ? '切换历史报价' : '暂无历史报价'}</option>
        {sessions.map(s => (
          <option key={s.token} value={s.token}>
            {s.brand_name || '未命名客户'} · {(STATUS_LABELS[s.status]?.text || s.status)} · {s.selected_count || 0}词
          </option>
        ))}
      </select>

      {selectedSession && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
          {status && <span className={`rounded-full px-2 py-0.5 ${status.color}`}>{status.text}</span>}
          <span>{normalizeIndustry(selectedSession.industry)}</span>
          <span>{selectedSession.selected_count || 0}词</span>
          {/* [WO_QUOTE_LIST_DATE 2026-08-05] 同上,详情头也走业务时间。 */}
          {formatQuoteOrderDate(selectedSession) && (
            <span>{formatQuoteOrderDate(selectedSession)}</span>
          )}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 1: Customer Info + Keyword Expansion + Generate Link          */
/*  与报价中心功能对齐：客户选择、诊断分析、智能扩词、手动加词             */
/* ------------------------------------------------------------------ */

function Step1Generate({
  onSessionCreated,
}: {
  onSessionCreated: (token: string) => void;
}) {
  // ---- 客户 & 诊断 ----
  const [brands, setBrands] = useState<Brand[]>([]);
  /**
   * 🔴 [#180 2026-09-12 · Owner 扩到两页] 「当前客户」的**唯一真相源是左上角**。
   *
   *    本页原来自己 `useState` 一份 `selectedBrandId`,与左上角完全割裂 ——
   *    与发布中心同一个病:两处各记一个客户,用户看到的和提交的可以是两个人。
   *
   *    🔴 **为什么这一版保留页面上那个下拉、只换真相源**:
   *       它被教程的 coach mark 罩着(`sandbox_quote_select_brand`,文案是
   *       「点输入框, 在下拉里选…」)。直接删掉,教程第二步会当场指着一个不存在的控件 ——
   *       那正是 #176 刚修完的那类事故(镜像面被主线改动悄悄弄坏,而且是在真人面前)。
   *       所以先把它变成**同一个真相源的第二个视图**:选择即 `switchClient`,
   *       两处永远一致;等教程那一步的文案与沙盒客户列表一起改,再撤控件。
   *       这一条已写进交付物"未评估/下一步"。
   */
  const { currentBrandId, switchClient, clients } = useClientContext();
  /** [#180-B] 当前客户名只用来**显示**跟随关系,不作为任何选择入口。 */
  const currentClientName = clients.find(c => c.id === currentBrandId)?.name || '';
  const tutorialStage = useTutorialStage();
  const selectedBrandId = currentBrandId ?? null;
  /** 改选客户 = 改左上角。页面不再自己存一份。 */
  const selectBrand = useCallback((bid: number | null) => {
    if (bid === null) { switchClient(null); return; }
    if (shouldSwitchClient({ deepLinkBrandId: bid, currentBrandId: currentBrandId ?? null })) {
      switchClient(bid);
    }
  }, [currentBrandId, switchClient]);
  const [diagnoses, setDiagnoses] = useState<DiagnosisRecord[]>([]);
  const [selectedDiagnosisId, setSelectedDiagnosisId] = useState<number | null>(null);
  // 2026-05-11 [Social-CTO-13.0 + GEO P0#1 merge] 客户选择 combobox · 输入实时过滤 + 显示"X天前诊断"hint
  const [brandSearchQuery, setBrandSearchQuery] = useState('');
  const [brandDropdownOpen, setBrandDropdownOpen] = useState(false);
  const brandComboboxRef = useRef<HTMLDivElement>(null);

  // ---- 表单字段 ----
  const [brandName, setBrandName] = useState('');
  const [industry, setIndustry] = useState('');
  const [customIndustryMode, setCustomIndustryMode] = useState(false);
  const [city, setCity] = useState('');
  const [coreKeywords, setCoreKeywords] = useState('');

  // 当 industry 被外部 prefill 改成 catalog 值时 · 自动退出 custom mode (防 brand 切换后残留)
  useEffect(() => {
    if (industry && INDUSTRY_CATEGORIES.some(c => c.label === industry)) {
      setCustomIndustryMode(false);
    }
  }, [industry]);

  // ---- AI 分析 ----
  const [distilledData, setDistilledData] = useState<DistilledData | null>(null);
  const [distilling, setDistilling] = useState(false);

  useEffect(() => {
    const corrected = preferIndustryFromBusinessText(industry, brandName, coreKeywords, distilledData?.core_business);
    if (corrected !== industry) setIndustry(corrected);
  }, [brandName, coreKeywords, distilledData?.core_business, industry]);

  // ---- 关键词扩展 ----
  const [expanded, setExpanded] = useState<ExpandedKeyword[]>([]);
  const [selectedKws, setSelectedKws] = useState<Set<string>>(new Set());
  const [expanding, setExpanding] = useState(false);
  const keywordRequestGenerationRef = useRef(0);
  // [工单 P0-1] 范围锁定裁决：服务端裁决 → 露出给操作员 → 可改可确认 → 回传复用
  const [scopeLock, setScopeLock] = useState<ScopeLock | null>(null);
  const [scopeLockConfirmed, setScopeLockConfirmed] = useState(false);
  // [工单 P0-4] 操作员人工放行进付费交付的词（记审计）
  const [releasedKws, setReleasedKws] = useState<Set<string>>(new Set());

  // ---- 手动添加 & AI 追加 ----
  const [manualInput, setManualInput] = useState('');
  const [additionalInput, setAdditionalInput] = useState('');
  const [addingMore, setAddingMore] = useState(false);
  const [lastAddedCount, setLastAddedCount] = useState(0);

  // ---- 创建链接 ----
  const [creating, setCreating] = useState(false);

  // 客户或诊断切换后，上一上下文的候选词不得继续显示，也不得被迟到响应写回。
  useEffect(() => {
    keywordRequestGenerationRef.current += 1;
    setExpanded([]);
    setSelectedKws(new Set());
    setExpanding(false);
    setAddingMore(false);
    setLastAddedCount(0);
    // 换客户/换诊断 = 换范围，上一份裁决与人工放行记录一律作废
    setScopeLock(null);
    setScopeLockConfirmed(false);
    setReleasedKws(new Set());
  }, [selectedBrandId, selectedDiagnosisId]);

  // ---- 加载品牌列表 ----
  useEffect(() => {
    api.get('/api/client-context/list')
      .then(res => {
        const items = res.data?.clients || res.data?.data || res.data?.items || res.data || [];
        setBrands(Array.isArray(items) ? items : []);
      })
      .catch(() => {});
  }, []);

  // CTO-15.21 v5 收口:URL ?brand_id=X 深链 · 自动选客户(brands list 加载完后触发)
  // [2026-05-26 P1.1 fix] 8cef6f35 砍 QuoteCenter 后老链接 ?diagnosis_id= / ?quote_id= 还在用
  //   兼容:无 brand_id 时从 diagnosis_id / quote_id 反推(各 API 返回都含 brand_id)
  const [searchParams] = useSearchParams();
  useEffect(() => {
    if (brands.length === 0) return;

    const trySelect = (bid: number) => {
      if (!Number.isFinite(bid) || bid <= 0) return;
      if (selectedBrandId === bid) return;
      if (brands.some(b => b.id === bid)) {
        selectBrand(bid);
      }
    };

    const urlBrandId = searchParams.get('brand_id');
    if (urlBrandId) {
      trySelect(parseInt(urlBrandId, 10));
      return;
    }

    // 老链接兼容 · 从 quote_id 反推
    const urlQuoteId = searchParams.get('quote_id');
    if (urlQuoteId) {
      const qid = parseInt(urlQuoteId, 10);
      if (Number.isFinite(qid) && qid > 0) {
        // 异步反推 · 拿到再 select(组件已 mounted · 不用担心 race)
        import('@/services/m3/api').then(({ getQuoteDetail }) => {
          getQuoteDetail(qid).then(detail => {
            const bid = detail?.quote?.brand_id;
            if (bid) trySelect(bid);
          });
        });
      }
      return;
    }

    // 老链接兼容 · 从 diagnosis_id 反推
    const urlDiagnosisId = searchParams.get('diagnosis_id');
    if (urlDiagnosisId) {
      const did = parseInt(urlDiagnosisId, 10);
      if (Number.isFinite(did) && did > 0) {
        import('@/lib/api').then(({ diagnosisApi }) => {
          diagnosisApi.getDetail(did).then(res => {
            const bid = res?.data?.brand_id;
            if (bid) trySelect(bid);
          }).catch(() => { /* silent · 反推失败用户手动选 */ });
        });
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, brands, selectedBrandId]);

  // 2026-05-11 客户 combobox 点击外部关闭 dropdown
  useEffect(() => {
    if (!brandDropdownOpen) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (brandComboboxRef.current && !brandComboboxRef.current.contains(e.target as Node)) {
        setBrandDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [brandDropdownOpen]);

  // ---- 品牌切换 → 加载诊断 + 自动填充 ----
  useEffect(() => {
    if (!selectedBrandId) {
      setDiagnoses([]);
      setSelectedDiagnosisId(null);
      return;
    }
    const brand = brands.find(b => b.id === selectedBrandId);
    if (brand) {
      setBrandName(brand.name);
      if (brand.industry) setIndustry(normalizeIndustry(brand.industry));
    }
    // 加载诊断报告
    api.get('/api/history', { params: { brand: brand?.name, limit: 20 } })
      .then(res => {
        const list = res.data?.data || [];
        setDiagnoses(Array.isArray(list) ? list : []);
      })
      .catch(() => setDiagnoses([]));
    // 从最近报价获取城市
    api.get('/api/quotes', { params: { brand_id: selectedBrandId, limit: 1 } })
      .then(res => {
        const quotes = res.data?.items || res.data?.data || [];
        if (quotes[0]?.city && !city) setCity(quotes[0].city);
      })
      .catch(() => {});
  }, [selectedBrandId, brands]);

  // ---- 诊断报告切换 → 自动填充 ----
  useEffect(() => {
    if (!selectedDiagnosisId) return;
    const diag = diagnoses.find(d => d.id === selectedDiagnosisId);
    if (diag) {
      if (diag.brand_name) setBrandName(diag.brand_name);
      if (diag.industry) setIndustry(normalizeIndustry(diag.industry));
    }
    // 从诊断详情获取关键词和城市
    api.get(`/api/diagnosis/${selectedDiagnosisId}/retest-data`)
      .then(res => {
        const d = res.data;
        if (d.keywords) {
          const kws = Array.isArray(d.keywords) ? d.keywords : [d.keywords];
          setCoreKeywords(kws.join('\n'));
        }
        if (d.client_location && !city) setCity(d.client_location);
      })
      .catch(() => {});
  }, [selectedDiagnosisId]);

  // ---- 智能分析（distill）----
  const handleDistill = async () => {
    if (!selectedDiagnosisId) return;
    setDistilling(true);
    try {
      const { data: resp } = await api.post(`/api/distill/${selectedDiagnosisId}`);
      // [CTO-15.23 2026-05-20] 后端 server.py:10939/10964 返 {success, cached, data: keyword_data}
      // 嵌套 envelope · 直读顶层会 silent fail · 跟 QuoteCenter.tsx:263 校验同款
      if (!resp?.success || !resp?.data) {
        throw new Error('蒸馏结果为空');
      }
      const d = resp.data;
      setDistilledData(d);
      // 自动填充核心关键词
      if (d.seed_keywords?.length > 0) {
        const nextCoreKeywords = d.seed_keywords.join('\n');
        setCoreKeywords(nextCoreKeywords);
        setIndustry(prev => preferIndustryFromBusinessText(prev, nextCoreKeywords, d.core_business, d.industry));
      } else if (d.use_scenarios?.length > 0) {
        const seeds = [...(d.use_scenarios || []).slice(0, 3), ...(d.competitor_names || []).slice(0, 2)];
        const nextCoreKeywords = seeds.join('\n');
        setCoreKeywords(nextCoreKeywords);
        setIndustry(prev => preferIndustryFromBusinessText(prev, nextCoreKeywords, d.core_business, d.industry));
      }
      // 自动填充城市
      if (!city && d.target_cities?.length > 0) {
        setCity(d.target_cities.join(', '));
      } else if (!city && d.geographic_focus) {
        setCity(d.geographic_focus);
      }
      // 自动填充行业
      if ((!industry || industry === '其他' || industry === '服装纺织') && d.industry) {
        setIndustry(prev => preferIndustryFromBusinessText(normalizeIndustry(prev), d.industry, d.core_business, d.seed_keywords?.join(' ')));
      }
    } catch (e: any) {
      toast.error('分析失败: ' + (e?.response?.data?.detail || e.message));
    } finally {
      setDistilling(false);
    }
  };

  // ---- 构建 business_scope 上下文 ----
  const buildBusinessScope = () => {
    if (!distilledData) return '';
    const parts: string[] = [];
    if (distilledData.core_business) parts.push(`核心业务: ${distilledData.core_business}`);
    if (distilledData.target_audience) parts.push(`目标客户: ${distilledData.target_audience}`);
    if (distilledData.unique_value) parts.push(`核心优势: ${distilledData.unique_value}`);
    if (distilledData.selling_points?.length) parts.push(`卖点: ${distilledData.selling_points.join('、')}`);
    if (distilledData.use_scenarios?.length) parts.push(`使用场景: ${distilledData.use_scenarios.join('、')}`);
    if (distilledData.competitor_names?.length) parts.push(`主要竞品: ${distilledData.competitor_names.join('、')}`);
    return parts.join('；');
  };

  // ---- 扩展关键词 ----
  // lockOverride: 「确认并重新扩词」时直接把刚改过的裁决传进来 —— setState 是异步的，
  // 靠 scopeLockConfirmed 的闭包值会读到旧的 false，操作员改了也发不出去。
  const handleExpand = async (lockOverride?: ScopeLock) => {
    const cores = coreKeywords.split(/[,，\n]/).map(s => s.trim()).filter(Boolean);
    if (cores.length === 0) return;
    const requestGeneration = ++keywordRequestGenerationRef.current;
    setExpanded([]);
    setSelectedKws(new Set());
    setLastAddedCount(0);
    setExpanding(true);
    try {
      const { data } = await api.post('/api/keywords/expand', {
        core_keywords: cores,
        industry,
        city,
        business_scope: buildBusinessScope(),
        target_count: 50,
        brand_name: brandName,  // [CTO-15.23 2026-05-11] 品牌锚定 · 防扩出泛市场词
        // [工单 P0-1 2026-07-26] 范围锁定：服务端据 brand_id/diagnosis_id 取资料 +
        //   诊断摘要做裁决；操作员已确认过的裁决直接回传复用（人改的优先）。
        brand_id: selectedBrandId || undefined,
        diagnosis_id: selectedDiagnosisId || undefined,
        scope_lock: lockOverride ?? (scopeLockConfirmed && scopeLock ? scopeLock : undefined),
      });
      if (requestGeneration !== keywordRequestGenerationRef.current) return;
      // 操作员确认过的裁决不被服务端回包覆盖（放行权归用户）
      if (data?.scope_lock && !lockOverride) setScopeLock(data.scope_lock as ScopeLock);
      const received = Array.isArray(data?.keywords) ? data.keywords : [];
      const rejected = Array.isArray(data?.rejected_keywords) ? data.rejected_keywords : [];
      const minimumExpected = minimumExpansionCandidateCount(cores, 50);
      const localSupplement = buildFallbackExpandedKeywords({
        coreKeywords: cores,
        industry,
        city,
        brandName,
        targetCount: 50,
      });
      const kws = mergeRecommendedAndReviewCandidates(
        [received],
        received.length >= minimumExpected && data?.success !== false
          ? [rejected]
          : [localSupplement, rejected],
        50,
      );
      const recommendedCount = recommendedKeywordSet(kws).size;
      const pendingReviewCount = Math.max(0, kws.length - recommendedCount);
      setExpanded(kws);
      setSelectedKws(recommendedKeywordSet(kws));
      const serverSupplemented = Number(data?.summary?.supplemented || 0);
      if (
        recommendedCount < minimumExpected
        || data?.success === false
        || serverSupplemented > 0
        || data?.summary?.underfilled === true
      ) {
        toast.warning(
          `当前 ${recommendedCount} 个自动推荐候选、${pendingReviewCount} 个待人工确认；`
          + '可继续追加，也可由操作员勾选后发送。',
        );
      }
    } catch (e: any) {
      if (requestGeneration !== keywordRequestGenerationRef.current) return;
      const fallback = buildFallbackExpandedKeywords({
        coreKeywords: cores,
        industry,
        city,
        brandName,
        targetCount: 20,
      });
      if (fallback.length > 0) {
        setExpanded(fallback);
        setSelectedKws(recommendedKeywordSet(fallback));
        toast.warning('AI扩词连接中断，已展示待人工确认的基础候选词；请勾选合适内容后再发送。');
      } else {
        toast.error('扩词失败: ' + formatApiErrorForDisplay(e, '网络异常，请稍后重试'));
      }
    } finally {
      if (requestGeneration === keywordRequestGenerationRef.current) setExpanding(false);
    }
  };

  // ---- 关键词选择 ----
  const toggleKw = (kw: string) => {
    setSelectedKws(prev => {
      const next = new Set(prev);
      next.has(kw) ? next.delete(kw) : next.add(kw);
      return next;
    });
  };

  // ---- 手动添加关键词 ----
  const handleAddManual = () => {
    const newKws = manualInput.split(/[\n,，、;；]/).map(s => s.trim()).filter(Boolean);
    if (newKws.length === 0) return;
    const existingSet = new Set(expanded.map(k => k.keyword.toLowerCase()));
    const unique = newKws.filter(kw => !existingSet.has(kw.toLowerCase()));
    if (unique.length === 0) {
      setLastAddedCount(-1); // indicate all duplicates
      setTimeout(() => setLastAddedCount(0), 3000);
      return;
    }
    const newEntries: ExpandedKeyword[] = unique.map(kw => ({
      keyword: kw,
      source: '手动添加',
      category: '客户提供',
    }));
    setExpanded(prev => [...prev, ...newEntries]);
    setSelectedKws(prev => {
      const next = new Set(prev);
      unique.forEach(kw => next.add(kw));
      return next;
    });
    setManualInput('');
    setLastAddedCount(unique.length);
    setTimeout(() => setLastAddedCount(0), 3000);
  };

  // ---- AI 追加扩词 ----
  const handleAddMore = async () => {
    if (!additionalInput.trim()) return;
    const additionalCores = additionalInput.split(/[,，\n]/).map(s => s.trim()).filter(Boolean);
    const requestGeneration = ++keywordRequestGenerationRef.current;
    setAddingMore(true);
    try {
      const { data } = await api.post('/api/keywords/expand', {
        core_keywords: additionalCores,
        industry,
        city,
        business_scope: buildBusinessScope(),
        target_count: 20,
        brand_name: brandName,  // [CTO-15.23 2026-05-11] 品牌锚定 · 防扩出泛市场词
        // [工单 P0-1] 追加扩词沿用同一份范围裁决，避免同一单里两套地域策略
        brand_id: selectedBrandId || undefined,
        diagnosis_id: selectedDiagnosisId || undefined,
        scope_lock: scopeLock || undefined,
      });
      if (requestGeneration !== keywordRequestGenerationRef.current) return;
      const received = Array.isArray(data?.keywords) ? data.keywords : [];
      const rejected = Array.isArray(data?.rejected_keywords) ? data.rejected_keywords : [];
      const minimumExpected = minimumExpansionCandidateCount(additionalCores, 20);
      const localSupplement = buildFallbackExpandedKeywords({
        coreKeywords: additionalCores,
        industry,
        city,
        brandName,
        targetCount: 20,
      });
      const newKws = mergeRecommendedAndReviewCandidates(
        [received],
        received.length >= minimumExpected && data?.success !== false
          ? [rejected]
          : [localSupplement, rejected],
        20,
      );
      const recommendedCount = recommendedKeywordSet(newKws).size;
      const pendingReviewCount = Math.max(0, newKws.length - recommendedCount);
      const existingSet = new Set(expanded.map(k => k.keyword.toLowerCase()));
      const unique = newKws.filter(kw => !existingSet.has(kw.keyword.toLowerCase()));
      if (unique.length > 0) {
        setExpanded(prev => [...prev, ...unique]);
        setSelectedKws(prev => {
          const next = new Set(prev);
          unique
            .filter(isDefaultSelectedKeyword)
            .forEach(kw => next.add(kw.keyword));
          return next;
        });
        setLastAddedCount(unique.length);
        const serverSupplemented = Number(data?.summary?.supplemented || 0);
        if (recommendedCount < minimumExpected || data?.success === false || serverSupplemented > 0) {
          toast.warning(
            `本次追加 ${recommendedCount} 个自动推荐候选、${pendingReviewCount} 个待人工确认。`,
          );
        }
      } else {
        setLastAddedCount(-1);
      }
      setAdditionalInput('');
      setTimeout(() => setLastAddedCount(0), 3000);
    } catch (e: any) {
      if (requestGeneration !== keywordRequestGenerationRef.current) return;
      const fallback = buildFallbackExpandedKeywords({
        coreKeywords: additionalCores,
        industry,
        city,
        brandName,
        targetCount: 10,
      });
      const existingSet = new Set(expanded.map(k => k.keyword.toLowerCase()));
      const unique = fallback.filter(kw => !existingSet.has(kw.keyword.toLowerCase()));
      if (unique.length > 0) {
        setExpanded(prev => [...prev, ...unique]);
        setSelectedKws(prev => {
          const next = new Set(prev);
          unique
            .filter(isDefaultSelectedKeyword)
            .forEach(kw => next.add(kw.keyword));
          return next;
        });
        setLastAddedCount(unique.length);
        toast.warning('AI扩词连接中断，已追加待人工确认候选词；请手动勾选后使用。');
      } else {
        toast.error('追加失败: ' + formatApiErrorForDisplay(e, '网络异常，请稍后重试'));
      }
    } finally {
      if (requestGeneration === keywordRequestGenerationRef.current) setAddingMore(false);
    }
  };

  // ---- 生成客户选词链接 ----
  const handleCreate = async () => {
    if (selectedKws.size === 0 || !brandName.trim()) return;
    setCreating(true);
    try {
      const keywords = expanded
        // [工单 2026-07-26 P0-4 · 裁决 D8 放行权归用户] 引擎判"未进入交付"的词
        // 默认不选;操作员**显式放行**(releasedKws)后可进快照,并带上放行理由留审计。
        // 物理禁选只剩四条硬边界(法律/资金/越权/数据完整性),那些词永远进不来。
        .filter(k => selectedKws.has(k.keyword) && !isHardBlockedKeyword(k))
        .map(k => ({
          keyword: k.keyword,
          category: k.category || 'general',
          source: k.source || '',
          recommendation_reason: '',
          geo_recommend: k.geo_recommend,
          scope_match: k.scope_match,
          default_selected: k.default_selected,
          rejection_reason: k.rejection_reason || '',
          // [T5 · T6] 三轴决策随快照落库：以后要复盘"这个词当初为什么进/没进"，
          // 看的是这三根轴 + reason_code，而不是猜两个布尔的含义。
          commercial_intent: k.commercial_intent,
          business_scope: k.business_scope,
          geo_scope: k.geo_scope,
          reason_code: k.reason_code || '',
          delivery_policy_version: k.delivery_policy_version || '',
          review_override_reason: (
            k.geo_recommend === false
            || k.scope_match === false
            || k.default_selected === false
          )
            ? (releasedKws.has(k.keyword)
              // 放行审计要说清**放行了哪一类判定**，只写"操作员放行"复盘时等于没写。
              ? `操作员在「${REVIEW_GROUP_META[reviewGroupOf(k)]?.title || '待确认'}」区明确放行并决定继续`
                + (k.reason_code ? `(原判定 ${k.reason_code})` : '')
              : '操作员在候选词页面明确勾选并决定继续')
            : '',
          review_version: 'keyword-delivery-decision-v1',
        }));
      const { data } = await api.post('/api/keyword-selection/create', {
        brand_id: selectedBrandId || undefined,
        brand_name: brandName,
        industry,
        city,
        keywords,
      });
      onSessionCreated(data.token);
    } catch (e: any) {
      toast.error('创建失败: ' + (e?.response?.data?.detail || e.message));
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="space-y-4">
      {/* ===== 客户信息 + 诊断选择 ===== */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2">
            <User className="w-4 h-4 text-brand" />
            客户信息
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {/* 客户选择 + 诊断报告 */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {/*
                🔴 [#180-B · Review 09-13 裁定 B] 页面上那个「选择客户」下拉**已撤**。
                当前客户的唯一来源是左上角(真实态 `ClientSwitcherSidebar`,
                沙盒态由 `ClientContext` 进入时自动选中教程客户)。

                🔴 为什么撤得起:#180 当时保留它,是因为沙盒态下它是**唯一**
                能设当前客户的控件(侧栏在沙盒态渲染的是静态卡、context 不认沙盒),
                撤了教程第二步会指着一个不存在的控件。那个前提现在由
                `ClientContext` 的沙盒自动选中解除了 —— 撤除的前提是**先核后撤**,
                不是到点照做(E7 反臂当初写的就是这两个条件)。

                🔴 教程客户固定只有一个,让用户在只有一个选项的下拉里"选"它,
                是 Owner 禁猜规则第 3 款的非必要动作。
            */}
            <div data-testid="quote-client-from-sidebar" className="text-xs text-muted-foreground">
                当前客户跟随左上角{currentClientName ? `:${currentClientName}` : ''}
            </div>
            <div>
              <label className="text-xs text-muted-foreground mb-1 block">诊断报告（可选）</label>
              <div className="flex gap-2">
                {/*
                    🔴 [#180-B B3] 这颗 coach mark 现在**接管第二步的页内引导**。
                    撤掉客户下拉之前,第二步落到页面后唯一的引导是那个下拉上的
                    `sandbox_quote_select_brand`;而阶段 `step2-online-form`
                    (PricingCenter 进页面就把阶段推到它)**全仓零消费者** ——
                    撤了下拉,第二步的页内引导就归零:侧栏把人送到 /pricing,然后没有下文。
                    所以显示条件改成**认这个阶段**,引导锚到用户真要碰的第一个控件。
                    🔴 文案同步:客户已经替他选好了,别再让他找"选客户"。
                */}
                <FeatureTooltip
                  featureId="sandbox_quote_select_diagnosis"
                  stepId="first_quote"
                  title="第二步: 选刚才跑的诊断报告"
                  content="教程客户「一路顺风出行服务」已经替你选好了(左上角) · 直接选刚才那份 26 分危急级诊断, 系统会按报告自动选关键词"
                  side="bottom"
                  disabled={!isSandboxActive()
                      || tutorialStage !== 'step2-online-form'
                      || !!selectedDiagnosisId}
                >
                  <select
                    className="flex-1 border border-border rounded-md px-3 py-2 text-sm bg-card disabled:bg-muted disabled:text-muted-foreground w-full"
                    value={selectedDiagnosisId || ''}
                    onChange={e => setSelectedDiagnosisId(e.target.value ? Number(e.target.value) : null)}
                    disabled={diagnoses.length === 0}
                  >
                    <option value="">{diagnoses.length === 0 ? '请先选择客户' : '-- 选择诊断报告 --'}</option>
                    {diagnoses.map(d => (
                      <option key={d.id} value={d.id}>
                        {d.created_at?.split('T')[0]} | {d.level} ({d.total_score}分)
                      </option>
                    ))}
                  </select>
                </FeatureTooltip>
                {selectedDiagnosisId && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={handleDistill}
                    disabled={distilling}
                    className="shrink-0 gap-1"
                  >
                    {distilling ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Brain className="w-3.5 h-3.5" />}
                    智能分析
                  </Button>
                )}
              </div>
            </div>
          </div>

          {/* 品牌名 + 行业 + 城市 */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div>
              <label className="text-xs text-muted-foreground mb-1 block">品牌名称 *</label>
              <Input value={brandName} onChange={e => setBrandName(e.target.value)} placeholder="品牌名称" />
            </div>
            <div>
              <label className="text-xs text-muted-foreground mb-1 block">行业</label>
              <select
                value={
                  customIndustryMode
                    ? '__custom__'
                    : (INDUSTRY_CATEGORIES.some(c => c.label === industry) ? industry : (industry ? '__custom__' : ''))
                }
                onChange={e => {
                  const v = e.target.value;
                  if (v === '__custom__') {
                    setCustomIndustryMode(true);
                    setIndustry('');
                    return;
                  }
                  setCustomIndustryMode(false);
                  setIndustry(v);
                }}
                className="w-full h-9 text-sm border border-border rounded-md px-2 bg-card text-foreground"
              >
                <option value="">请选择行业</option>
                {INDUSTRY_CATEGORIES.map(c => (
                  <option key={c.label} value={c.label}>{c.label}</option>
                ))}
                <option value="__custom__">其他（自定义）</option>
              </select>
              {(customIndustryMode || (!INDUSTRY_CATEGORIES.some(c => c.label === industry) && industry !== '')) && (
                <Input value={industry} onChange={e => setIndustry(e.target.value)} placeholder="输入行业名称" className="mt-1" autoFocus />
              )}
            </div>
            <div>
              <label className="text-xs text-muted-foreground mb-1 block">城市</label>
              <Input value={city} onChange={e => setCity(e.target.value)} placeholder="城市" />
            </div>
          </div>

          {/* 智能分析结果预览 */}
          {distilledData && (
            <div className="bg-card border border-border rounded-xl p-3 text-sm space-y-2">
              {distilledData.target_audience && (
                <p><span className="text-muted-foreground">目标人群：</span>{distilledData.target_audience}</p>
              )}
              {distilledData.use_scenarios && distilledData.use_scenarios.length > 0 && (
                <div className="flex items-center gap-1 flex-wrap">
                  <span className="text-muted-foreground">使用场景：</span>
                  {distilledData.use_scenarios.slice(0, 3).map((s, i) => (
                    <Badge key={i} variant="outline" className="text-xs">{s}</Badge>
                  ))}
                </div>
              )}
              {distilledData.selling_points && distilledData.selling_points.length > 0 && (
                <div className="flex items-center gap-1 flex-wrap">
                  <span className="text-muted-foreground">核心卖点：</span>
                  {distilledData.selling_points.slice(0, 4).map((s, i) => (
                    <Badge key={i} className="text-xs bg-purple-500/10 text-purple-400 border border-purple-500/20">{s}</Badge>
                  ))}
                </div>
              )}
              {distilledData.competitor_names && distilledData.competitor_names.length > 0 && (
                <div className="flex items-center gap-1 flex-wrap">
                  <span className="text-muted-foreground">竞品：</span>
                  {distilledData.competitor_names.slice(0, 4).map((s, i) => (
                    <Badge key={i} variant="secondary" className="text-xs">{s}</Badge>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* 核心关键词 + 扩词按钮 */}
          <div>
            <label className="text-xs text-muted-foreground mb-1 block">核心关键词（3-5个，逗号或换行分隔）</label>
            <textarea
              className="w-full border rounded-md p-2 text-sm min-h-[80px] resize-none"
              placeholder="例如：AI搜索优化、GEO优化、豆包搜索排名"
              data-testid="quote-core-keywords"
              value={coreKeywords}
              onChange={e => {
                keywordRequestGenerationRef.current += 1;
                setCoreKeywords(e.target.value);
                setExpanded([]);
                setSelectedKws(new Set());
                setExpanding(false);
              }}
            />
          </div>
          <FeatureTooltip
            featureId="sandbox_quote_expand_btn"
            stepId="first_quote"
            title="第三步: 点这里 AI 智能扩词"
            content="基于诊断关键词 · AI 扩出更多相关词供选择 (沙盒 1.5 秒模拟)"
            side="bottom"
            disabled={!isSandboxActive() || !selectedDiagnosisId || expanded.length > 0}
          >
            <Button
              data-testid="quote-expand-btn"
              onClick={() => handleExpand()}
              disabled={!coreKeywords.trim() || expanding}
              className="gap-1"
            >
              {expanding ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
              {expanding ? 'AI 扩词中...' : 'AI 智能扩词'}
            </Button>
          </FeatureTooltip>
        </CardContent>
      </Card>

      {/* ===== 范围锁定裁决(工单 P0-1)· 可人工修改 + 确认 ===== */}
      {scopeLock && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2">
              <Search className="w-4 h-4 text-brand" />
              选词范围锁定
              <Badge variant="outline" className="text-[10px]">
                {scopeLock.used_diagnosis ? '已用诊断报告' : '仅用客户资料'}
              </Badge>
              {scopeLockConfirmed && (
                <Badge variant="secondary" className="text-[10px]">已确认</Badge>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-xs text-muted-foreground">
              这决定推什么颗粒度的词。判错了直接改，改完点「确认并重新扩词」。
              {scopeLock.reason ? ` 依据：${scopeLock.reason}` : ''}
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div>
                <label className="text-xs text-muted-foreground mb-1 block">服务市场（城市，逗号分隔）</label>
                <input
                  type="text"
                  className="w-full border border-border rounded-md px-2 py-2 text-sm bg-card"
                  placeholder="如：深圳（全国业务可留空）"
                  value={(scopeLock.service_market || []).join('、')}
                  onChange={e => {
                    const market = e.target.value.split(/[,，、\s]+/).map(s => s.trim()).filter(Boolean);
                    setScopeLock({ ...scopeLock, service_market: market });
                    setScopeLockConfirmed(false);
                  }}
                />
              </div>
              <div>
                <label className="text-xs text-muted-foreground mb-1 block">市场层级</label>
                <select
                  className="w-full border border-border rounded-md px-2 py-2 text-sm bg-card"
                  value={scopeLock.market_level}
                  onChange={e => {
                    setScopeLock({ ...scopeLock, market_level: e.target.value });
                    setScopeLockConfirmed(false);
                  }}
                >
                  {Object.entries(MARKET_LEVEL_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="text-xs text-muted-foreground mb-1 block">业务类型</label>
                <select
                  className="w-full border border-border rounded-md px-2 py-2 text-sm bg-card"
                  value={scopeLock.business_type}
                  onChange={e => {
                    setScopeLock({ ...scopeLock, business_type: e.target.value });
                    setScopeLockConfirmed(false);
                  }}
                >
                  {['B2C', 'B2B', '政企'].map(value => (
                    <option key={value} value={value}>{value}</option>
                  ))}
                </select>
              </div>
            </div>
            {scopeLock.buyer_persona && (
              <p className="text-xs text-muted-foreground">买家画像：{scopeLock.buyer_persona}</p>
            )}
            {(scopeLock.real_query_seeds || []).length > 0 && (
              <div className="flex flex-wrap gap-1">
                <span className="text-xs text-muted-foreground mr-1">真实问法参照：</span>
                {(scopeLock.real_query_seeds || []).slice(0, 8).map(seed => (
                  <Badge key={seed} variant="outline" className="text-[10px]">{seed}</Badge>
                ))}
              </div>
            )}
            <div className="flex gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={expanding}
                onClick={() => {
                  setScopeLockConfirmed(true);
                  handleExpand(scopeLock);
                }}
              >
                {expanding ? <Loader2 className="w-4 h-4 animate-spin mr-1" /> : null}
                确认并重新扩词
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* ===== 关键词选择 + 手动/AI追加 + 生成链接 ===== */}
      {expanded.length > 0 && (() => {
        {/* [SSOT geo-commercial-intent-governance-v1.0 §3.2] 知识/百科类问法
            (geo_recommend===false)物理分离到「未进入交付」区,不可勾选、
            不可改选回付费交付;范围争议词(scope_match===false)仍可由
            操作员确认继续(advisory)。 */}
        // [T5] 分区判据改用后端三轴决策：进入交付 = default_selected，其余按 reason_group
        // 落到各自的原因区。老后端（只有两个布尔）走 reviewGroupOf 的兼容派生。
        const isDelivered = (k: ExpandedKeyword) =>
          (k.default_selected === true || (k.default_selected === undefined && isDefaultSelectedKeyword(k)))
          || releasedKws.has(k.keyword);
        const deliverableKws = expanded.filter(isDelivered);
        const reviewKws = expanded.filter(k => !isDelivered(k));
        const reviewGroups = REVIEW_GROUP_ORDER
          .map(group => ({ group, items: reviewKws.filter(k => reviewGroupOf(k) === group) }))
          .filter(entry => entry.items.length > 0);
        const releaseGroup = (items: ExpandedKeyword[], title: string) => {
          const releasable = items.filter(k => !isHardBlockedKeyword(k));
          if (releasable.length === 0) return;
          setReleasedKws(prev => {
            const next = new Set(prev);
            releasable.forEach(k => next.add(k.keyword));
            return next;
          });
          setSelectedKws(prev => {
            const next = new Set(prev);
            releasable.forEach(k => next.add(k.keyword));
            return next;
          });
          toast.success(`已放行「${title}」${releasable.length} 个词进入交付（已记录人工放行）`);
        };
        return (
        <Card>
          <CardHeader className="pb-3 flex flex-row items-center justify-between">
            <CardTitle className="text-base flex items-center gap-2">
              候选关键词
              <Badge variant="secondary" className="text-xs">已选 {selectedKws.size}/{deliverableKws.length}</Badge>
              {/* [T5] 每个原因区各自计数 —— 不再是一个笼统的「N 个未进入交付」 */}
              {reviewGroups.map(({ group, items }) => (
                <Badge
                  key={group}
                  variant="outline"
                  data-testid={`kwgroup-badge-${group}`}
                  className={`text-[10px] ${REVIEW_GROUP_META[group].tone}`}
                >
                  {items.length} 个{REVIEW_GROUP_META[group].title}
                </Badge>
              ))}
            </CardTitle>
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={() => {
                if (selectedKws.size === deliverableKws.length) {
                  setSelectedKws(new Set());
                } else {
                  setSelectedKws(new Set(deliverableKws.map(k => k.keyword)));
                }
              }}>
                {selectedKws.size === deliverableKws.length ? '全不选' : '全选'}
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* 关键词网格(已进入交付 + 人工放行的候选) */}
            <div
              data-testid="kwgroup-delivered"
              className="max-h-96 overflow-y-auto grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-2"
            >
              {deliverableKws.map(kw => {
                const selected = selectedKws.has(kw.keyword);
                const noRecommend = kw.scope_match === false && !releasedKws.has(kw.keyword);
                return (
                  <label
                    key={kw.keyword}
                    className={`flex items-center gap-2 p-2 rounded-lg border cursor-pointer transition-colors ${
                      noRecommend
                        ? selected
                          ? 'border-amber-500/40 bg-amber-500/10'
                          : 'border-amber-500/20 bg-amber-500/5 hover:bg-amber-500/10'
                        : selected
                          ? 'border-brand bg-brand/5'
                          : 'border-border hover:bg-muted'
                    }`}
                    title={noRecommend ? (kw.rejection_reason || '该关键词需要操作员复核') : ''}
                  >
                    <Checkbox
                      checked={selected}
                      onCheckedChange={() => toggleKw(kw.keyword)}
                    />
                    <span className={`text-sm flex-1 break-words leading-snug ${noRecommend ? 'text-amber-400' : ''}`} title={kw.keyword}>{kw.keyword}</span>
                    {/* [工单 P1-7] 疑似竞品词只标注不丢弃 */}
                    {kw.competitor_suspect && (
                      <Badge variant="outline" className="text-[10px] shrink-0 text-amber-400 border-amber-500/30" title={kw.advisory_reason || ''}>疑似竞品</Badge>
                    )}
                    {releasedKws.has(kw.keyword) && (
                      <Badge variant="outline" className="text-[10px] shrink-0 text-amber-400 border-amber-500/30">人工放行</Badge>
                    )}
                    {noRecommend ? (
                      <Badge variant="outline" className="text-[10px] shrink-0 text-amber-400 border-amber-500/30">范围需复核</Badge>
                    ) : (
                      <Badge variant="outline" className="text-[10px] shrink-0">{kw.category || kw.source}</Badge>
                    )}
                  </label>
                );
              })}
            </div>

            {/* [T5] 待确认区：按**真实原因**分组，每组给出各自的人话与动作。
                以前这里是一个笼统的「未进入付费交付」+ 一句「通常不会推荐」，
                地域过宽 / 业务不匹配 / 需澄清 / 真知识题被混成一堆（R5）。
                物理禁选仍然只保留四条 H0 硬边界（法律/资金/越权/数据完整性）。 */}
            {reviewGroups.map(({ group, items }) => {
              const meta = REVIEW_GROUP_META[group];
              const releasableCount = items.filter(k => !isHardBlockedKeyword(k)).length;
              return (
                <div
                  key={group}
                  data-testid={`kwgroup-${group}`}
                  className={`rounded-lg border p-3 space-y-2 ${meta.tone}`}
                >
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <p className="text-xs font-medium">
                      <span data-testid={`kwgroup-title-${group}`}>{meta.title}</span>
                      （{items.length}）
                      <span className="ml-1 font-normal opacity-80">{meta.hint}</span>
                    </p>
                    {releasableCount > 0 && (
                      <Button
                        size="sm"
                        variant="outline"
                        data-testid={`kwgroup-release-all-${group}`}
                        className="h-6 px-2 text-[10px] shrink-0"
                        onClick={() => releaseGroup(items, meta.title)}
                      >
                        全部放行到交付({releasableCount})
                      </Button>
                    )}
                  </div>
                  <div className="max-h-40 overflow-y-auto space-y-1">
                    {items.map(kw => {
                      const hardBlocked = isHardBlockedKeyword(kw);
                      return (
                        <div
                          key={kw.keyword}
                          data-testid={`kwrow-${group}`}
                          data-keyword={kw.keyword}
                          className="flex flex-wrap items-start justify-between gap-x-2 gap-y-1 text-xs"
                        >
                          <span className="break-words leading-snug min-w-0 flex-1">{kw.keyword}</span>
                          <span className="flex items-center gap-2 shrink-0">
                            <span
                              className="text-[10px] opacity-80 max-w-[22rem] break-words text-right"
                              data-testid={`kwreason-${group}`}
                            >
                              {hardBlocked
                                ? (kw.hard_block_reason || '触发硬边界，不可放行')
                                : (kw.reason_text || kw.rejection_reason || meta.hint)}
                            </span>
                            {hardBlocked ? (
                              <Badge variant="outline" className="text-[10px] border-border">不可放行</Badge>
                            ) : (
                              <Button
                                size="sm"
                                variant="outline"
                                data-testid={`kwrelease-${group}`}
                                className="h-6 px-2 text-[10px]"
                                onClick={() => {
                                  setReleasedKws(prev => new Set(prev).add(kw.keyword));
                                  setSelectedKws(prev => new Set(prev).add(kw.keyword));
                                  toast.success(`已放行「${kw.keyword}」进入交付候选（已记录人工放行）`);
                                }}
                              >
                                放行到交付
                              </Button>
                            )}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}

            {/* 成功提示 */}
            {lastAddedCount > 0 && (
              <p className="text-sm text-green-400 animate-in fade-in">
                成功添加 {lastAddedCount} 个新关键词！已自动勾选
              </p>
            )}
            {lastAddedCount === -1 && (
              <p className="text-sm text-amber-400 animate-in fade-in">所有关键词已存在，无需重复添加</p>
            )}

            {/* 手动添加 */}
            <div className="border border-dashed border-border rounded-xl p-3 bg-muted">
              <p className="text-xs text-muted-foreground mb-2 font-medium">手动添加关键词</p>
              <div className="flex gap-2">
                <textarea
                  className="flex-1 border border-border rounded-md p-2 text-sm min-h-[60px] resize-none bg-card"
                  placeholder="每行一个或用逗号分隔"
                  value={manualInput}
                  onChange={e => setManualInput(e.target.value)}
                />
                <Button size="sm" onClick={handleAddManual} disabled={!manualInput.trim()} className="self-end">
                  <Plus className="w-4 h-4 mr-1" /> 添加
                </Button>
              </div>
            </div>

            {/* AI 追加扩词 */}
            <div className="border border-dashed border-brand/30 rounded-xl p-3 bg-brand/5">
              <p className="text-xs text-brand mb-2 font-medium">AI 智能追加</p>
              <div className="flex gap-2">
                <Input
                  placeholder="输入补充方向，例如：价格对比、竞品分析..."
                  value={additionalInput}
                  onChange={e => setAdditionalInput(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && handleAddMore()}
                  className="bg-card"
                />
                <Button size="sm" onClick={handleAddMore} disabled={addingMore || !additionalInput.trim()} className="shrink-0 gap-1">
                  {addingMore ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Sparkles className="w-3.5 h-3.5" />}
                  {addingMore ? '生成中...' : '追加'}
                </Button>
              </div>
            </div>

            {/* 生成链接 */}
            <div className="pt-2 border-t">
              <div className="flex items-center gap-2 mb-3 px-1">
                <Info className="w-4 h-4 text-brand shrink-0" />
                <p className="text-xs text-muted-foreground">
                  点击下方按钮生成客户专属选词链接。客户打开后可以调整选词，提交后系统会自动通知您进入下一步。
                </p>
              </div>
              <FeatureTooltip
                featureId="sandbox_quote_create_btn"
                stepId="first_quote"
                title="生成报价链接发给客户"
                content="教程模式 · 已经帮你选好客户和关键词 · 点这里生成链接 (客户操作会自动模拟 · 你跟着代理这边的步骤走就行)"
                side="bottom"
                disabled={!isSandboxActive()}
              >
                <Button
                  data-testid="quote-create-selection"
                  onClick={handleCreate}
                  disabled={selectedKws.size === 0 || creating || !brandName.trim()}
                  className="w-full gap-2 bg-foreground text-background hover:bg-foreground/90 h-11"
                  size="lg"
                >
                  {creating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Link2 className="w-4 h-4" />}
                  生成客户选词链接 ({selectedKws.size}词)
                </Button>
              </FeatureTooltip>
            </div>
          </CardContent>
        </Card>
        );
      })()}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 2: Customer Selection (waiting)                               */
/* ------------------------------------------------------------------ */

function Step2CustomerSelect({
  session, sessionData, onRefresh,
}: {
  session: Session;
  sessionData: any;
  onRefresh: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const link = `${window.location.origin}/s/${session.token}`;

  const handleCopy = async () => {
    const ok = await copyToClipboard(link);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } else {
      toast.error('复制失败');
    }
  };

  const keywords = sessionData?.keywords || [];
  const selectedIds = new Set(sessionData?.selected_ids || []);

  return (
    <div className="space-y-4">
      <Card className="border border-border rounded-xl">
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2">
            <Link2 className="w-4 h-4 text-brand" />
            客户选词链接
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {/* 🔴 [包三 · 报价方案] 生成链接后明说「发给客户,客户看到什么」。
              改前这里只有一个链接框和「复制」—— 她拿到一串 URL,不知道该发给谁、
              对方点开会看到什么、会不会看到成本或内部参数。
              不知道对方看到什么,她就不敢发;而不敢发,这条链路就断在这儿。 */}
          <div data-testid="quote-link-what-customer-sees"
            className="rounded-xl border border-border bg-muted/30 p-3 text-[13px] leading-relaxed">
            <p className="font-medium text-foreground">把这条链接发给客户。</p>
            <p className="text-muted-foreground mt-1">
              客户点开后看到:你选的这些词、每个词的价格、合计金额,可以自己勾掉不要的,然后提交。
              <span className="text-foreground">不会看到</span>你的成本和内部评分,也不需要注册或登录。
            </p>
            <p className="text-muted-foreground mt-1">客户提交后你会收到通知,再由你审核确认。</p>
          </div>
          <div className="flex gap-2">
            <Input value={link} readOnly className="bg-muted text-sm font-mono" />
            <Button variant="outline" size="sm" onClick={handleCopy} className="gap-1 shrink-0">
              {copied ? <Check className="w-4 h-4 text-green-500" /> : <Copy className="w-4 h-4" />}
              {copied ? '已复制' : '复制'}
            </Button>
            <Button variant="outline" size="sm" onClick={() => window.open(link, '_blank')} className="shrink-0">
              <ExternalLink className="w-4 h-4" />
            </Button>
          </div>
          <div className="bg-card border border-border rounded-xl p-3">
            <p className="text-sm text-foreground font-medium mb-1">操作提示</p>
            <ol className="text-xs text-muted-foreground space-y-1 list-decimal list-inside">
              <li>复制上方链接，通过微信/邮件发送给客户</li>
              <li>客户打开链接后可以勾选/取消关键词</li>
              <li>客户点击「确认选词」后，此页面会自动更新</li>
            </ol>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3 flex flex-row items-center justify-between">
          <CardTitle className="text-base flex items-center gap-2">
            {session.status === 'selecting' || session.status === 'business_lines_submitted' ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin text-brand" />
                {session.status === 'business_lines_submitted'
                  ? '客户已选业务方向，等待选词...'
                  : '等待客户选词中...'}
              </>
            ) : (
              <>
                <Check className="w-4 h-4 text-green-500" />
                客户已提交选词 ({selectedIds.size}词)
              </>
            )}
          </CardTitle>
          <Button variant="ghost" size="sm" onClick={onRefresh}>
            <RefreshCw className="w-4 h-4" />
          </Button>
        </CardHeader>
        {(session.status === 'selecting' || session.status === 'business_lines_submitted') && (
          <CardContent>
            {isSandboxActive() && (
              <SandboxVideoSlot
                src="/sandbox/client-select-business.mp4"
                title="选业务方向"
                hint="这一步在客户那边:客户打开你发的链接,看到我们为他生成的业务方向,勾选想推广的方向后提交。看完这段录屏,系统就会按方向选好关键词,你这边继续算价。"
                doneLabel="客户已提交选词"
                doneSub="正在进入「计算报价」…"
                onComplete={() => { advanceSandboxClientSelect(); onRefresh(); }}
              />
            )}
            <div className="flex items-center gap-3 py-8 justify-center text-muted-foreground">
              <Clock className="w-5 h-5" />
              <span className="text-sm">
                {session.status === 'business_lines_submitted'
                  ? '客户已选择业务方向。页面每 8 秒自动刷新，客户提交选词后会立即更新'
                  : '页面每 8 秒自动刷新，客户提交后会立即更新'}
              </span>
            </div>
          </CardContent>
        )}
        {session.status !== 'selecting' && session.status !== 'business_lines_submitted' && keywords.length > 0 && (
          <CardContent>
            <div className="max-h-[300px] overflow-y-auto space-y-1">
              {keywords.filter((k: any) => selectedIds.has(k.id)).map((kw: any) => (
                <div key={kw.id} className="flex items-center gap-2 p-1.5 text-sm bg-green-500/10 border border-green-500/20 rounded">
                  <Check className="w-3.5 h-3.5 text-green-500" />
                  <span>{kw.keyword}</span>
                </div>
              ))}
            </div>
          </CardContent>
        )}
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 3: Calculate Prices                                           */
/* ------------------------------------------------------------------ */

function Step3Calculate({
  session, onDone,
}: {
  session: Session;
  onDone: () => void;
}) {
  const [calculating, setCalculating] = useState(false);
  const [progress, setProgress] = useState('');
  const markStep = useMarkStepCompleted();

  const handleCalculate = async () => {
    setCalculating(true);
    setProgress('正在为每个词算价…');
    try {
      await api.post(`/api/keyword-selection/${session.token}/generate-quote`, {}, { timeout: 600000 });
      // markStep 已挪到收款完成时 · 防 step 2 在签约前提前打勾
      setProgress('报价计算完成！');
      setTimeout(onDone, 500);
    } catch (e: any) {
      setProgress('');
      toast.error('计算失败: ' + (e?.response?.data?.detail || e.message));
    } finally {
      setCalculating(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">计算报价</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          客户已确认 <span className="font-bold text-brand">{session.selected_count}</span> 个关键词。
          点击下方按钮,系统会为每个关键词算出报价。
        </p>
        <div className="bg-muted rounded-xl p-3 text-xs text-muted-foreground space-y-1">
          <p>计算过程包含：</p>
          <ul className="list-disc list-inside space-y-0.5 ml-1">
            <li>市场数据查询（搜索热度、广告竞争度）</li>
            <li>AI 搜索竞争度分析（竞品数量、内容质量）</li>
            <li>大模型商业价值评估（意图分类、市场规模）</li>
          </ul>
          <p className="text-amber-400 mt-2">预计耗时 2-5 分钟，请耐心等待</p>
        </div>
        <FeatureTooltip
          featureId="sandbox_quote_calculate_btn"
          stepId="first_quote"
          title="点这里计算三档报价"
          content="客户已选好关键词 · 点这里让 AI 算出入门 / 标准 / 旗舰 三档价格 (约 2 秒)"
          side="bottom"
          disabled={!isSandboxActive()}
        >
          <Button
            onClick={handleCalculate}
            disabled={calculating}
            className="gap-2 bg-foreground text-background hover:bg-foreground/90"
            size="lg"
          >
            {calculating ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            {calculating ? '计算中，请勿关闭页面...' : '一键计算报价'}
          </Button>
        </FeatureTooltip>
        {progress && !calculating && (
          <p className="text-sm text-green-400">{progress}</p>
        )}
        {/* [报价解释层一期 2026-06-13 返修] 首次「一键计算报价」也挂阶段式进度(纯体验层 · 非真 SSE · 替原单行 spinner) */}
        {calculating && <QuoteProgressStages className="mt-2" />}
      </CardContent>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 4: Review Prices                                              */
/* ------------------------------------------------------------------ */

function Step4Review({
  session, pricingData, clustersData, onApprove, onRefreshSession, coefficientBusy = false,
}: {
  session: Session;
  pricingData: PricingData | null;
  clustersData?: { clusters: { cluster_name: string; business_tag: string; core_keywords: { keyword: string; is_selected: boolean }[]; covered_keyword_count: number; covered_keywords?: { keyword: string; source: string; merge_reason?: string; upgradeable?: boolean; id?: number; entry?: { price: number; articles: number }; standard?: { price: number; articles: number }; flagship?: { price: number; articles: number } }[]; variant_count?: number; pricing: Record<string, { core_price: number; full_price: number; savings: number; core_articles: number }> }[]; tier_summaries?: Record<string, { core_price: number; total_articles: number }> } | null;
  onApprove: () => void;
  onRefreshSession: () => void;
  /** 页顶「修改本次报价系数」正在预览/保存 —— 期间禁止把报价发出去。
   *  原来这个联动是 Step4 内部两个 state 直连,编辑器移到顶栏后由 OnlineQuoteFlow 提上来。 */
  coefficientBusy?: boolean;
}) {
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [batchDeleting, setBatchDeleting] = useState(false);
  const [approving, setApproving] = useState(false);
  const [recalculating, setRecalculating] = useState(false);
  const [expandedKw, setExpandedKw] = useState<number | null>(null);
  const [auditingId, setAuditingId] = useState<number | null>(null);
  const [batchAuditing, setBatchAuditing] = useState(false);
  const [batchAuditProgress, setBatchAuditProgress] = useState('');
  const { overview, isMember, loading: organizationLoading, error: organizationError } = useOrganization();
  const organizationAuthorityReady = !organizationLoading && !organizationError;
  const canRecalculateQuote = organizationAuthorityReady
    && (!isMember || Boolean(overview?.identity.capabilities.includes('quote.create')));
  const markStep = useMarkStepCompleted();
  // [2026-06-06] 应用内居中确认弹窗(替换原生 confirm · 居中 + 不被"不再显示对话框"静默拦截)
  const [confirmDialog, confirm] = useConfirmDialog();

  const isAddingKeywords = session.status === 'adding_keywords';

  /* [WO_QUOTE_MEDIA_MIX 2026-08-12 · Review P1-1 返修] 投放组合上下文,每份报价拉一次。
     🔴 判红原文:「后端算法零生产调用;前端仍使用固定全局比例」。这就是那条缺失的接线。
     🔴 拉失败**不设兜底假值**:mixContext 保持 undefined,文案自动降级成
        「当前按全行业平均值估算」—— 宁可说得保守,也不许显示一个像是按行业算出来的数。 */
  /* [#225 a1 ②] 交付口径。默认**不指定**,由服务端给的 `delivery_perspective` 兜底 ——
     前端写死一个默认值等于把后端的默认悄悄改掉(它今天是 self_media,明天可能不是)。 */
  const [perspective, setPerspective] = useState<string>('');
  const [mixContext, setMixContext] = useState<
    {
      ratioUsed?: number; douyinShare?: number; ratioSource?: string;
      deliveryPerspective?: string; postsEstimateTotal?: number;
    } | undefined
  >(undefined);
  useEffect(() => {
    if (!session.quote_id) { setMixContext(undefined); return; }
    let cancelled = false;
    setMixContext(undefined);   // 切报价先清空,不让上一份的比例串场
    (async () => {
      try {
        const { data } = await api.get(`/api/quotes/${session.quote_id}/media-mix`
          + (perspective ? `?perspective=${encodeURIComponent(perspective)}` : ''));
        if (cancelled) return;
        const total = Number(data?.capacity_total) || 0;
        const douyin = Number(data?.mix?.douyin_doubao_only) || 0;
        setMixContext({
          ratioUsed: Number(data?.ratio?.used) || undefined,
          douyinShare: total > 0 ? douyin / total : 0,
          ratioSource: data?.ratio?.source,
          /* [#225 a1 ②] 口径与按口径估算的**条**数,原样透传给文案层。
             🔴 `posts_per_slot_bps` 故意**不取**:它是内部运营参数,取了就会有人显示它。 */
          deliveryPerspective: data?.delivery_perspective,
          postsEstimateTotal: Number.isFinite(Number(data?.posts_estimate_total))
            ? Number(data.posts_estimate_total) : undefined,
        });
      } catch {
        if (!cancelled) setMixContext(undefined);   // 见上:不兜假值
      }
    })();
    return () => { cancelled = true; };
    /* 🔴 `perspective` 必须进依赖数组:不进的话切了口径不会重取,
       屏幕上那句话永远停在第一次的口径上,而下拉看起来是好用的。 */
  }, [session.quote_id, pricingData?.generated_at, perspective]);

  // adding_keywords 状态但还没有 pricingData → 需要先计算
  if (!pricingData) {
    return (
      <Card>
        <CardContent className="pt-6 space-y-4">
          {isAddingKeywords && (
            <ActionableAlert
              contract={{ action: 'calculate_quote', target: '/api/keyword-selection/{selection_token}/generate-quote', permission: 'quote.create', recovery: 'retry_after_pricing_source_recovers' }}
              title="客户追加了新关键词"
              description="重新计算会保留已有关键词价格（7 天缓存），仅评估新词。"
              icon={<AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-400" />}
              className="border-amber-500/20 bg-amber-500/10 text-amber-500"
              allowed={canRecalculateQuote}
            />
          )}
          <p className="text-sm text-muted-foreground">报价数据尚未生成，请先计算报价。</p>
          {canRecalculateQuote && <Button
            onClick={async () => {
              setRecalculating(true);
              try {
                await api.post(`/api/keyword-selection/${session.token}/generate-quote`, {}, { timeout: 600000 });
                // markStep 已挪到收款完成时
                onRefreshSession();
              } catch (e: any) {
                toast.error('计算失败: ' + (e?.response?.data?.detail || e.message));
              } finally { setRecalculating(false); }
            }}
            disabled={recalculating}
            className="gap-2 bg-foreground text-background hover:bg-foreground/90"
          >
            {recalculating ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            {recalculating ? '计算中，请勿关闭...' : '计算报价'}
          </Button>}
          {/* [报价解释层一期 2026-06-13] 算价阶段式进度(纯体验层 · 非真 SSE) */}
          {recalculating && <QuoteProgressStages className="mt-2" />}
        </CardContent>
      </Card>
    );
  }

  const tiers = pricingData.tiers;
  const keywords = pricingData.keywords;

  const handleDelete = async (kwId: number) => {
    const ok = await confirm({ title: '删除该关键词?', description: '删除后不可恢复。', confirmLabel: '删除', danger: true });
    if (!ok) return;
    setDeletingId(kwId);
    try {
      await api.delete(`/api/keyword-selection/${session.token}/keywords/${kwId}`);
      onRefreshSession();
    } catch (e: any) {
      toast.error('删除失败: ' + (e?.response?.data?.detail || e.message));
    } finally {
      setDeletingId(null);
    }
  };

  const handleBatchDelete = async () => {
    if (selectedIds.size === 0) return;
    const ok = await confirm({ title: `删除选中的 ${selectedIds.size} 个关键词?`, description: '删除后不可恢复。', confirmLabel: '删除', danger: true });
    if (!ok) return;
    setBatchDeleting(true);
    try {
      const ids = Array.from(selectedIds);
      let failCount = 0;
      // 串行删除：避免并发写入同一 session 导致竞态覆盖
      for (const id of ids) {
        try {
          await api.delete(`/api/keyword-selection/${session.token}/keywords/${id}`);
        } catch {
          failCount++;
        }
      }
      if (failCount > 0) {
        toast.error(`${ids.length - failCount} 个删除成功，${failCount} 个失败`);
      }
      setSelectedIds(new Set());
      onRefreshSession();
    } catch (e: any) {
      toast.error('批量删除失败: ' + (e?.response?.data?.detail || e.message));
    } finally {
      setBatchDeleting(false);
    }
  };

  const toggleSelect = (id: number) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (selectedIds.size === keywords.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(keywords.map(kw => kw.id)));
    }
  };

  const handleApprove = async () => {
    // P0.6 价格 sanity 前置拦截(CTO-15.7 2026-04-24 罗平事故止血)
    const totalMonthly = session?.final_price ?? session?.confirmed_total_price ?? 0;
    const sanity = analyzePriceSanity(totalMonthly, session?.industry ?? '', session?.city ?? '');
    if (sanity.warnings.length > 0) {
      const okWarn = await confirm({
        title: '⚠️ 该报价触发价格警告',
        description: sanity.warnings.map(w => '· ' + w).join('  ') + '  · 是否仍然发送给客户?',
        confirmLabel: '仍然发送',
        danger: true,
      });
      if (!okWarn) return;
    }
    const okSend = await confirm({ title: '发送报价给客户?', description: '客户将收到三个套餐方案并选择其一。', confirmLabel: '发送' });
    if (!okSend) return;
    setApproving(true);
    try {
      const { data } = await api.post(`/api/keyword-selection/${session.token}/approve-quote`);
      toast.success(data?.already_sent ? '报价此前已发送给客户' : '报价已发送 · 客户打开链接即可查看');
      onApprove();
    } catch (e: any) {
      toast.error('发送失败: ' + (e?.response?.data?.detail || e.message));
    } finally {
      setApproving(false);
    }
  };

  /**
   * 🔴 [包三 · 报价方案] 重算只留**这一个**入口。
   *
   * 改前有两处:这里,和下面按钮里的一段内联 handler —— 打同一个端点、做同一件事,
   * 却各写一套文案(一处说「重新计算报价」、一处说「重新算价 + LLM 审计」)。
   * 同一件事两个说法,她无法判断是不是同一个操作;而改文案时必然只改一处。
   *
   * 文案口径:按钮说**做什么**,不说内部机制。「三维评分引擎」「LLM 审计」是工程术语,
   * 对她没有信息量 —— 她要判断的只有"要不要花 2-5 分钟重算"。
   */
  const handleRecalculate = async () => {
    const ok = await confirm({
      title: '重新算一遍价格?',
      description: '会花 2-5 分钟重新评估每个词的价格。7 天内算过的词价格不变(价格锁)。发现价格明显不对时用它。',
      confirmLabel: '重新算一遍',
    });
    if (!ok) return;
    setRecalculating(true);
    try {
      await api.post(`/api/keyword-selection/${session.token}/generate-quote`, {}, { timeout: 600000 });
      // markStep 已挪到收款完成时
      onRefreshSession();
      toast.success('价格已重新算过');
    } catch (e: any) {
      toast.error('重新计算失败: ' + (e?.response?.data?.detail || e.message));
    } finally {
      setRecalculating(false);
    }
  };

  const handleAuditKeyword = async (kwId: number, kwName: string) => {
    const ok = await confirm({ title: `让 AI 复核「${kwName}」的报价?`, description: '系统将调用大模型审核该词的公开市场行情,并根据实际竞争情况修正价格。约需 30-60 秒。', confirmLabel: '开始复核' });
    if (!ok) return;
    setAuditingId(kwId);
    try {
      const { data } = await api.post(`/api/keyword-selection/${session.token}/audit-keyword/${kwId}`, {}, { timeout: 120000 });
      if (data.price_changed) {
        toast.success(`复核完成:「${kwName}」价格从 ¥${data.old_price} 调整为 ¥${data.new_price}`, { description: data.audit_note || undefined });
      } else {
        toast.success(`复核完成:「${kwName}」价格合理,不用调`, { description: data.audit_note || undefined });
      }
      onRefreshSession();
    } catch (e: any) {
      toast.error('复核失败: ' + (e?.response?.data?.detail || e.message));
    } finally {
      setAuditingId(null);
    }
  };

  const handleBatchAudit = async () => {
    const anomalies = keywords.filter(kw => {
      if (kw.audit_status) return false;
      const price = kw.price_before_audit ?? kw.standard.price;
      const prices = keywords.map(k => k.price_before_audit ?? k.standard.price);
      const a = prices.reduce((s, p) => s + p, 0) / prices.length;
      const sd = Math.sqrt(prices.reduce((s, p) => s + (p - a) ** 2, 0) / prices.length);
      return sd > 0 && Math.abs(price - a) > 2 * sd;
    });
    if (anomalies.length === 0) return;
    const ok = await confirm({ title: `让 AI 复核这 ${anomalies.length} 个价格可疑的词?`, description: `逐个复核,大约要 ${anomalies.length * 30}-${anomalies.length * 60} 秒。`, confirmLabel: '开始复核' });
    if (!ok) return;

    setBatchAuditing(true);
    setBatchAuditProgress(`正在复核 ${anomalies.length} 个词的报价…`);
    const results = await Promise.allSettled(
      anomalies.map(kw =>
        api.post(`/api/keyword-selection/${session.token}/audit-keyword/${kw.id}`, {}, { timeout: 120000 })
      )
    );
    const adjusted = results.filter(r => r.status === 'fulfilled' && r.value.data.price_changed).length;
    const failed = results.filter(r => r.status === 'rejected').length;
    setBatchAuditing(false);
    setBatchAuditProgress('');
    toast.success(`复核完成:${anomalies.length} 个可疑的词里,${adjusted} 个价格调整了${failed > 0 ? `，${failed} 个失败` : ''}。`);
    onRefreshSession();
  };

  // 异常检测 — 用复核前原始价格计算统计基准，避免审计后基准漂移
  const originalPrices = keywords.map(k => k.price_before_audit ?? k.standard.price);
  const avg = originalPrices.length > 0 ? originalPrices.reduce((a, b) => a + b, 0) / originalPrices.length : 0;
  const std = originalPrices.length > 2
    ? Math.sqrt(originalPrices.reduce((s, p) => s + (p - avg) ** 2, 0) / originalPrices.length)
    : avg * 0.5;
  const isAnomaly = (kw: PricingKeyword) => {
    if (kw.audit_status) return false; // 已复核的词不再标黄
    const price = kw.price_before_audit ?? kw.standard.price;
    return std > 0 && Math.abs(price - avg) > 2 * std;
  };
  const anomalyCount = keywords.filter(isAnomaly).length;

  // 红标：无效词检测（无厘头/完全不会被AI推荐商家的词）
  const isRedFlag = (kw: PricingKeyword) => {
    const text = kw.keyword.trim();
    // [SSOT business-governance-master §17.1] 统一引擎判定在场时以它为准：
    // CommercialQueryPolicy 认定可交付的词，展示层红标绝不与之矛盾。
    if (kw.commercial_delivery_eligible === true) return false;
    // 纯数字
    if (/^\d+$/.test(text)) return true;
    // 纯符号 / 无中文且过短
    if (/^[^a-zA-Z\u4e00-\u9fff]+$/.test(text) && text.length <= 4) return true;
    // 中文字符数
    const cnChars = (text.match(/[\u4e00-\u9fff]/g) || []).length;
    // ≤2个中文字的通用词（如"你好""天气"），非商业意图 → AI不会推荐商家
    if (cnChars <= 2 && kw.intent !== 'transactional' && kw.intent !== 'commercial') return true;
    // 搜索量为0 且 无商业意图
    if ((kw.search_volume === 0 || kw.search_volume == null) && kw.intent === 'informational' && (kw.value_score ?? 1) <= 0.85) return true;
    return false;
  };
  const redFlagCount = keywords.filter(isRedFlag).length;

  const intentLabels: Record<string, string> = {
    transactional: '交易型',
    commercial: '商业型',
    informational: '信息型',
    comparison: '对比型',
    navigational: '导航型',
    local: '本地型',
  };
  const funnelLabels: Record<string, string> = {
    decision: '决策阶段',
    consideration: '考虑阶段',
    awareness: '认知阶段',
  };

  // [CTO-15.23 2026-05-13 UI batch fix] Social CTO 诊断 3.2:
  //   报价价格 ¥1,911 带尾数不专业 · 展示层 round 整十(后台精确价仍保留)
  const roundPrice = (p: number): number => Math.round((p || 0) / 10) * 10;

  // [CTO-15.23 2026-05-26 v6 工厂模式] 三档 hover 解释 · 仅显示话术(销售环节人话翻译)
  //   出现率精确数据走 ai_probability 字段 · 占有率类术语口径见 SOV_ALLOWLIST.md
  // [2026-06-05] 话术对齐 50/65/75:入门问2次1次/标准问3次2次/旗舰问4次3次 · 去"强势占位"(霸榜措辞已移除)
  const tierHoverInfo: Record<string, { rate: string; positioning: string }> = {
    入门版: { rate: '问 2 次约出现 1 次', positioning: '基础曝光 · 入门试水' },
    标准版: { rate: '问 3 次约出现 2 次', positioning: '稳定曝光 · 主推套餐' },
    旗舰版: { rate: '问 4 次约出现 3 次', positioning: '高频曝光 · 旗舰抢位' },
  };

  return (
    <>
      {confirmDialog}
      <div className="space-y-4">
      {/* 🔴 [WO_QUOTE_COEFFICIENT_PRIVATE_TOPBAR 2026-08-11] 原来这里是一整块「本次报价系数」Card
          (售价系数 / 调整原因 / 三档换算 / 关键词标准价 / 冻结说明)。服务商当面演示报价时,
          客户直接从"审核与发送"正文里看到内部加价逻辑 → 已按工单 §8 整块移除。
          编辑入口唯一化到页顶 QuotePricingControlBar 的「修改本次报价系数」标签,
          默认隐藏 + 星号遮蔽。**这里不许再放第二套本次系数编辑器**(工单 §8:页面上不得同时出现两套)。 */}
      {/* 客户追加词提示 */}
      {isAddingKeywords && (
        <ActionableAlert
          contract={{ action: 'recalculate_quote', target: '/api/keyword-selection/{selection_token}/generate-quote', permission: 'quote.create', recovery: 'retry_after_pricing_source_recovers' }}
          title="客户追加了新关键词"
          description="重新计算可将新词纳入报价，已有词价格保持不变。"
          icon={<AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-400" />}
          className="border-amber-500/20 bg-amber-500/10 text-amber-500"
          allowed={canRecalculateQuote}
          actionLabel={recalculating ? '计算中…' : '重新计算含新词'}
          onAction={handleRecalculate}
          actionDisabled={recalculating}
        />
      )}

      {/* [真实第一] 数据断供词:不出价不兜底(兜底价会成对外承诺) · 重新计算自动补算 */}
      {(pricingData.unavailable_keywords?.length ?? 0) > 0 && (
        <ActionableAlert
          contract={{ action: 'retry_pricing', target: '/api/keyword-selection/{selection_token}/generate-quote', permission: 'quote.create', recovery: 'retry_or_review_missing_keyword_evidence' }}
          title={`${pricingData.unavailable_keywords!.length} 个词网络繁忙，本次未能估价（未计入报价）`}
          description={<>
              {pricingData.unavailable_keywords!.slice(0, 5).map(u => u.keyword).join('、')}
              {pricingData.unavailable_keywords!.length > 5 ? ` 等${pricingData.unavailable_keywords!.length}个` : ''}
              · 点击「重新计算」补上这些词，发给客户前请确认报价完整。
          </>}
          icon={<AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-400" />}
          className="border-amber-500/20 bg-amber-500/10 text-amber-500"
          allowed={canRecalculateQuote}
          actionLabel={recalculating ? '计算中…' : '重新计算'}
          onAction={handleRecalculate}
          actionDisabled={recalculating}
        />
      )}

      {/* [报价意图闸 2026-08-04] 不建议投放的词只读区(不计价 · 附候选池里现成可用的商业词) */}
      <ExcludedKeywordsPanel
        excluded={pricingData.excluded_keywords}
        suggested={pricingData.suggested_keywords}
        gateStatus={pricingData.policy_gate_status}
      />

      {redFlagCount > 0 && (
        <div className="flex items-start gap-2 bg-red-500/10 border border-red-500/20 rounded-lg px-4 py-3">
          <AlertTriangle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />
          <div className="text-sm text-red-400">
            <p className="font-medium">发现 {redFlagCount} 个无效关键词（红标）</p>
            <p className="text-xs mt-0.5">这些词没有搜索量或不是有效说法,AI 平台不会推荐,建议直接删掉。</p>
          </div>
        </div>
      )}

      {anomalyCount > 0 && (
        <div className="flex items-start gap-2 bg-yellow-500/10 border border-yellow-500/20 rounded-lg px-4 py-3">
          <AlertTriangle className="w-4 h-4 text-yellow-400 mt-0.5 shrink-0" />
          <div className="text-sm text-yellow-400 flex-1">
            <p className="font-medium">发现 {anomalyCount} 个价格异常词（偏离均值 2 倍标准差）</p>
            <p className="text-xs mt-0.5">
              {batchAuditing
                ? batchAuditProgress
                : '点开可疑的那行看价格是怎么算的,也可以让 AI 一次复核全部。'}
            </p>
          </div>
          <Button
            size="sm"
            onClick={handleBatchAudit}
            disabled={batchAuditing}
            className="gap-1 shrink-0 bg-yellow-600 hover:bg-yellow-700 text-white"
          >
            {batchAuditing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Brain className="w-4 h-4" />}
            {batchAuditing ? `${batchAuditProgress.split('：')[0] || '复核中'}` : '一键重新评估'}
          </Button>
        </div>
      )}

      {/* 主题包概览（cluster mode only） */}
      {clustersData?.clusters && clustersData.clusters.length > 0 && (
        <Card className="border-blue-500/20 bg-blue-500/10">
          <CardHeader className="pb-2">
            <CardTitle className="text-base flex items-center gap-2">
              📦 主题包概览
              <Badge variant="secondary" className="text-xs">{clustersData.clusters.length} 个包</Badge>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {clustersData.clusters.map((cl, i) => {
              const selected = cl.core_keywords.filter(k => k.is_selected);
              const stdPricing = cl.pricing?.standard;
              return (
                <div key={i} className="bg-card rounded-lg px-3 py-2 border border-blue-500/20">
                  <div className="flex items-center justify-between">
                    <div className="min-w-0">
                      <span className="text-sm font-medium text-foreground">
                        {i === 0 ? '🎯' : i === 1 ? '🔍' : i === 2 ? '📊' : '💡'} {cl.cluster_name}
                      </span>
                      <span className="text-xs text-muted-foreground ml-2">
                        {selected.length} 核心词{cl.covered_keyword_count > 0 ? ` · ${cl.covered_keyword_count} 相关搜索参考` : ''}
                      </span>
                    </div>
                    <div className="text-right shrink-0 ml-3">
                      <span className="text-sm font-semibold text-blue-400 tabular-nums">
                        ¥{(stdPricing?.core_price || 0).toLocaleString()}
                      </span>
                      {stdPricing && stdPricing.savings > 0 && (
                        <span className="text-xs text-muted-foreground ml-1 line-through">¥{stdPricing.full_price.toLocaleString()}</span>
                      )}
                    </div>
                  </div>
                  {/* [2026-06-07 批B Option A] 报价页同义覆盖词全词展示 · 原价划线→免费(数据来自 clusters_data) */}
                  {cl.covered_keywords && cl.covered_keywords.length > 0 && (
                    <CoveredKeywordsFade
                      coveredKeywords={cl.covered_keywords}
                      variantCount={0}
                      activeTier="standard"
                    />
                  )}
                </div>
              );
            })}
          </CardContent>
        </Card>
      )}

      {/* 三档总价 */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {(['entry', 'standard', 'flagship'] as const).map(tier => {
          const t = tiers[tier];
          if (!t) return null;
          const isStd = tier === 'standard';
          return (
            <Card key={tier} className={`border border-border rounded-xl hover:border-border transition-all duration-200 ${isStd ? 'border-brand ring-1 ring-brand/20' : ''}`}>
              <CardContent className="pt-4 text-center">
                <p className="text-sm text-muted-foreground">{t.label}</p>
                <p className="text-2xl font-bold mt-1">
                  ¥{t.total_price?.toLocaleString()}
                  <span className="text-sm font-normal text-muted-foreground"> · 累计达标30天</span>
                </p>
                <div className="flex justify-center gap-3 mt-2 text-xs text-muted-foreground">
                  <span>AI概率 {t.ai_probability}</span>
                  {/* 🔴 [#225 a1 ②] 这个数是**槽**(合同分配的交付额度,= Σ 每词 articles,
                      与 `/media-mix` 的 `capacity_total` 同源),原来标「篇」。
                      同一屏下面「本次交付 N 槽 · 按…约需 M 条左右」会和它打架 ——
                      文案锁要求同屏「槽」只指合同分配、「篇/条」只指要做的内容。 */}
                  <span>{t.total_articles} 槽</span>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/*
        * 🔴 [#225 a1 ②] 「发布口径」—— **整单一个**,不是每词一个。
        *
        *  工单说「在『为什么是这个价』加一行下拉」,那是位置的口语描述。
        *  落到代码上不能照做:`WhyThisPrice` 在两个地方渲染,而且**都在关键词循环里**
        *  (卡片版 / 表格版)—— 放进去会变成「每个关键词一个下拉」,
        *  而口径是整单的一件事。所以控件放在整单级(这里),
        *  它改的 `mixContext` 会传给下面每一个「为什么是这个价」。
        *
        * 🔴 这一块**只在服务商端**渲染:`OnlineQuoteFlow` 客户面零引用,
        *    客户页走 `pages/Selection/**`(结构锁钉着它不许引用换算字段)。
        */}
      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-muted/30 px-3 py-2" data-testid="quote-delivery-perspective">
        <span className="text-xs font-medium text-foreground">发布口径</span>
        <select
          className="h-7 rounded border border-border bg-background px-2 text-xs"
          data-testid="quote-perspective-select"
          value={perspective || mixContext?.deliveryPerspective || ''}
          onChange={(e) => setPerspective(e.target.value)}
        >
          <option value="self_media">自媒体为主</option>
          <option value="portal">门户为主</option>
          <option value="mixed">混合</option>
        </select>
        {/* 🔴 JSX 不认 markdown:这里原来写 `**怎么发**`,星号会原样显示在屏幕上。
            要强调就用元素,不要用符号。 */}
        <span className="text-[11px] text-muted-foreground">
          换的是<span className="font-medium text-foreground">怎么发</span>,
          不动槽数、不动价;下面每个词的「投放组合」跟着它变。
        </span>
      </div>

      {/* 明细表 */}
      <Card>
        <CardHeader className="flex flex-col items-start gap-2 pb-2 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
            <CardTitle className="text-base">关键词明细 ({keywords.length}个)</CardTitle>
            {selectedIds.size > 0 && (
              <div className="flex flex-wrap items-center gap-2 sm:ml-2">
                <span className="text-xs text-muted-foreground">已选 {selectedIds.size} 个</span>
                <Button
                  variant="ghost" size="sm"
                  onClick={handleBatchDelete}
                  disabled={batchDeleting}
                  className="text-red-400 hover:text-red-400 hover:bg-red-500/10 h-7 px-2 text-xs gap-1"
                >
                  {batchDeleting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
                  批量删除
                </Button>
                <Button
                  variant="ghost" size="sm"
                  onClick={() => setSelectedIds(new Set())}
                  className="text-muted-foreground h-7 px-2 text-xs"
                >
                  取消选择
                </Button>
              </div>
            )}
          </div>
          <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
            {redFlagCount > 0 && <span className="text-red-500">{redFlagCount} 个无效词标红</span>}
            {redFlagCount > 0 && anomalyCount > 0 && '，'}
            {anomalyCount > 0 && <span className="text-yellow-400">{anomalyCount} 个异常词标黄</span>}
            {/* [Social CTO 诊断 2.3] ⓘ 图标 + 蓝色强化 · 透明定价 SSOT 入口可见 */}
            {redFlagCount === 0 && anomalyCount === 0 && (
              <>
                <Info className="w-3 h-3 text-blue-400" />
                <span className="text-blue-400">点击行查看定价依据</span>
              </>
            )}
          </span>
        </CardHeader>
        <CardContent className="px-3 pb-4 sm:px-6">
          <div className="mb-2 flex items-center gap-1 text-[11px] text-muted-foreground sm:hidden">
            <Info className="w-3 h-3" />
            点开关键词卡片可查看定价依据
          </div>
          <div className="space-y-2 sm:hidden">
            {keywords.map(kw => {
              const anomaly = isAnomaly(kw);
              const red = isRedFlag(kw);
              const isSro = !!kw.super_red_ocean;  // [2026-06-08] 超红海 · 需单独报价
              const isInfoOnly = !isSro && kw.should_quote === false;  // [2026-06-08] 信息型 · 不报价(对齐 PricingTable)
              const isGuaranteeUnavail = !isSro && !isInfoOnly && !!kw.guarantee_unavailable;  // [报价解释层 2026-06-13] 护栏剥保证价·参考价待人工核
              const isExpanded = expandedKw === kw.id;
              const cardTone = red
                ? 'border-red-500/30 bg-red-500/10'
                : anomaly ? 'border-yellow-500/30 bg-yellow-500/10' : 'border-border bg-card';
              return (
                <div
                  key={kw.id}
                  data-testid="quote-kw-expand"
                  className={`rounded-xl border p-3 ${cardTone}`}
                  onClick={() => setExpandedKw(isExpanded ? null : kw.id)}
                >
                  <div className="flex items-start gap-2">
                    <input
                      type="checkbox"
                      checked={selectedIds.has(kw.id)}
                      onClick={e => e.stopPropagation()}
                      onChange={() => toggleSelect(kw.id)}
                      className="mt-1 h-4 w-4 rounded border-muted-foreground/30 accent-red-500"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <ChevronRight className={`h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform ${isExpanded ? 'rotate-90' : ''}`} />
                        {red && <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-red-500" />}
                        {!red && anomaly && <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-yellow-500" />}
                        <span className="text-sm font-medium text-foreground break-words leading-snug" title={kw.keyword}>{kw.keyword}</span>
                        {isSro && (
                          <span className="shrink-0 rounded-full border border-red-100 bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-600">
                            {SUPER_RED_OCEAN_COPY.priceLabel}
                          </span>
                        )}
                        {isInfoOnly && (
                          <span className="shrink-0 rounded-full border border-border bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                            信息型 · 不报价
                          </span>
                        )}
                        {isGuaranteeUnavail && (
                          <span className="shrink-0 rounded-full border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-medium text-amber-600">
                            {GUARANTEE_UNAVAILABLE_COPY.label}
                          </span>
                        )}
                      </div>
                      {/* CTO-15.23 Phase 4 · mobile recommendation_reason · ExpandableMeta preview 30 字 + "看详情"
                       * 原:line-clamp-2 cursor-pointer 整卡 tap 展开 · 不直观
                       * 新:短摘要 + "看详情" link · tap 弹 popover 看全文 · click-outside 关 */}
                      <div className="mt-1" onClick={(e) => e.stopPropagation()}>
                        <ExpandableMeta text={kw.recommendation_reason} mode="preview" maxChars={30} iconLabel="推荐理由" />
                      </div>
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={e => { e.stopPropagation(); handleDelete(kw.id); }}
                      disabled={deletingId === kw.id}
                      className="h-9 w-9 shrink-0 p-0 text-red-400 hover:text-red-400"
                    >
                      {deletingId === kw.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                    </Button>
                  </div>

                  {isSro ? (
                    <div className="mt-3 rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-center text-xs font-medium text-red-600">
                      {SUPER_RED_OCEAN_COPY.priceLabel} · 竞争极高 · 不计入套餐总价 · 请单独核价
                    </div>
                  ) : isInfoOnly ? (
                    <div className="mt-3 rounded-lg border border-border bg-muted px-3 py-2 text-center text-xs font-medium text-muted-foreground">
                      信息型 · 不报价(不计入套餐总价)
                    </div>
                  ) : isGuaranteeUnavail ? (
                    <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-center text-xs font-medium text-amber-600">
                      {GUARANTEE_UNAVAILABLE_COPY.badge} · 不计入套餐总价
                    </div>
                  ) : (
                    <div className="mt-3 grid grid-cols-3 gap-2">
                      {/* [Social CTO 诊断 3.2] 卡片价格 round 整十展示 */}
                      {[
                        ['入门版', kw.entry.price],
                        ['标准版', kw.standard.price],
                        ['旗舰版', kw.flagship.price],
                      ].map(([label, price]) => (
                        <div key={label} className="rounded-lg border border-border/70 bg-background/40 px-2 py-2 text-center">
                          <p className="text-[10px] text-muted-foreground">{label}</p>
                          <p className="mt-0.5 text-sm font-semibold text-foreground">
                            ¥{roundPrice(Number(price)).toLocaleString()}
                          </p>
                        </div>
                      ))}
                    </div>
                  )}

                  <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
                    <Badge variant="outline" className="text-xs">
                      {intentLabels[kw.intent || ''] || kw.intent || '-'}
                    </Badge>
                    {kw.audit_status ? (
                      <Badge variant="outline" className="border-green-500/30 px-1 text-[10px] text-green-400">
                        已复核
                      </Badge>
                    ) : anomaly ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={e => { e.stopPropagation(); handleAuditKeyword(kw.id, kw.keyword); }}
                        disabled={auditingId === kw.id}
                        className="min-h-9 gap-1 text-xs text-yellow-400 hover:bg-yellow-500/20 hover:text-yellow-400"
                      >
                        {auditingId === kw.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Brain className="h-3.5 w-3.5" />}
                        {auditingId === kw.id ? '复核中' : '重新评估'}
                      </Button>
                    ) : null}
                  </div>

                  {isExpanded && (
                    <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                      <div className="rounded border border-border bg-background/50 p-2">
                        <p className="text-muted-foreground">市场热度</p>
                        <p className="text-sm font-medium">{kw.search_volume ?? '-'}</p>
                      </div>
                      <div className="rounded border border-border bg-background/50 p-2">
                        <p className="text-muted-foreground">竞争强度</p>
                        <p className="text-sm font-medium">{kw.effective_competition ?? '-'}</p>
                      </div>
                      <div className="rounded border border-border bg-background/50 p-2">
                        <p className="text-muted-foreground">优化难度</p>
                        <p className="text-sm font-medium">{kw.difficulty_score ?? '-'}</p>
                      </div>
                      <div className="rounded border border-border bg-background/50 p-2">
                        <p className="text-muted-foreground">商业价值</p>
                        <p className="text-sm font-medium">{kw.value_score ?? '-'}</p>
                      </div>
                      <div className="col-span-2 rounded border border-border bg-background/50 p-2">
                        <p className="text-muted-foreground">用户阶段</p>
                        <p className="text-sm font-medium">{funnelLabels[kw.funnel_stage || ''] || '-'}</p>
                      </div>
                      {kw.audit_status && (
                        <div className="col-span-2 rounded border border-green-500/20 bg-green-500/10 p-2 text-green-400">
                          <span className="font-medium">AI 复核结果:</span>
                          {kw.audit_note || '审核完毕'}
                        </div>
                      )}
                      {/* [报价解释层一期 2026-06-13] 为什么是这个价 · 人话拆解 */}
                      <WhyThisPrice kw={kw} className="col-span-2 mt-1" mixContext={mixContext} />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          <div className="hidden overflow-x-auto rounded-lg border border-border/60 sm:block">
            {/* [Social CTO 诊断 3.3] min-w 920 → 1060 防旗舰列被截 */}
            <TooltipProvider delay={150}>
            <table className="min-w-[1060px] w-full text-sm">
              <thead>
                <tr className="border-b text-muted-foreground">
                  <th className="text-left py-2 pl-2 w-8" onClick={e => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={keywords.length > 0 && selectedIds.size === keywords.length}
                      onChange={toggleSelectAll}
                      className="rounded border-muted-foreground/30 w-3.5 h-3.5 cursor-pointer accent-red-500"
                    />
                  </th>
                  <th className="text-left py-2">关键词</th>
                  <th className="text-left py-2">
                    {/* Phase 4 改:hint 从 hover-only 改 "悬停/点击 看详情" · ExpandableMeta 双端均支持 */}
                    <span className="inline-flex items-center gap-1">
                      推荐理由
                      <Tooltip>
                        <TooltipTrigger className="inline-flex appearance-none items-center bg-transparent p-0 text-inherit">
                          <Info className="w-3 h-3 text-muted-foreground/60 cursor-help" />
                        </TooltipTrigger>
                        <TooltipContent>悬停或点击「看详情」查看全文</TooltipContent>
                      </Tooltip>
                    </span>
                  </th>
                  <th className="text-center py-2 w-20">意图</th>
                  {/* [Social CTO 诊断 2.4] 三档命名加 Tooltip · 复用 SOV → 出现率话术(非代理不裸露 %) */}
                  {(['入门版', '标准版', '旗舰版'] as const).map(tierName => (
                    <th key={tierName} className="text-right py-2 w-24">
                      <Tooltip>
                        <TooltipTrigger className="inline-flex appearance-none items-center gap-1 bg-transparent p-0 text-inherit cursor-help">
                          {tierName}
                          <Info className="w-3 h-3 text-muted-foreground/60" />
                        </TooltipTrigger>
                        <TooltipContent className="max-w-[260px]">
                          <div className="space-y-1 text-left">
                            <div className="font-medium">{tierName} · {tierHoverInfo[tierName].positioning}</div>
                            <div>📊 {tierHoverInfo[tierName].rate}</div>
                          </div>
                        </TooltipContent>
                      </Tooltip>
                    </th>
                  ))}
                  <th className="text-center py-2 w-12">删除</th>
                  <th className="text-center py-2 w-28">复核</th>
                </tr>
              </thead>
              <tbody>
                {keywords.map(kw => {
                  const anomaly = isAnomaly(kw);
                  const red = isRedFlag(kw);
                  const isSro = !!kw.super_red_ocean;  // [2026-06-08] 超红海 · 需单独报价
                  const isInfoOnly = !isSro && kw.should_quote === false;  // [2026-06-08] 信息型 · 不报价(对齐 PricingTable)
                  const isGuaranteeUnavail = !isSro && !isInfoOnly && !!kw.guarantee_unavailable;  // [报价解释层 2026-06-13] 护栏剥保证价·参考价待人工核
                  const isExpanded = expandedKw === kw.id;
                  const rowColor = red
                    ? 'bg-red-500/10 hover:bg-red-500/20'
                    : anomaly ? 'bg-yellow-500/10 hover:bg-yellow-500/20' : 'hover:bg-muted';
                  return (
                    <React.Fragment key={kw.id}>
                      <tr
                        data-testid="quote-kw-expand"
                        className={`border-b cursor-pointer transition-colors ${rowColor}`}
                        onClick={() => setExpandedKw(isExpanded ? null : kw.id)}
                      >
                        <td className="py-2 pl-2 w-8" onClick={e => e.stopPropagation()}>
                          <input
                            type="checkbox"
                            checked={selectedIds.has(kw.id)}
                            onChange={() => toggleSelect(kw.id)}
                            className="rounded border-muted-foreground/30 w-3.5 h-3.5 cursor-pointer accent-red-500"
                          />
                        </td>
                        <td className="py-2">
                          <div className="flex items-center gap-1.5">
                            <ChevronRight className={`w-3.5 h-3.5 text-muted-foreground transition-transform ${isExpanded ? 'rotate-90' : ''}`} />
                            {red && <AlertTriangle className="w-3.5 h-3.5 text-red-500 shrink-0" />}
                            {!red && anomaly && <AlertTriangle className="w-3.5 h-3.5 text-yellow-500 shrink-0" />}
                            <span className="break-words leading-snug max-w-[280px]" title={kw.keyword}>{kw.keyword}</span>
                            {isSro && (
                              <span className="ml-1 shrink-0 rounded-full border border-red-100 bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-600">
                                {SUPER_RED_OCEAN_COPY.priceLabel}
                              </span>
                            )}
                            {isInfoOnly && (
                              <span className="ml-1 shrink-0 rounded-full border border-border bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                                信息型 · 不报价
                              </span>
                            )}
                            {isGuaranteeUnavail && (
                              <span className="ml-1 shrink-0 rounded-full border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-medium text-amber-600">
                                {GUARANTEE_UNAVAILABLE_COPY.label}
                              </span>
                            )}
                          </div>
                        </td>
                        <td className="text-left text-xs text-muted-foreground max-w-[300px]" onClick={e => e.stopPropagation()}>
                          {/* CTO-15.23 Phase 4 · 桌面 recommendation_reason · ExpandableMeta preview 40 字
                           * 原:shadcn Tooltip hover 展开 · mobile 不工作
                           * 新:统一 ExpandableMeta · PC hover 150ms 弹 · mobile tap 弹 + click-outside 关
                           * align="end" 防 popover 在右侧表格列向右溢出 */}
                          <ExpandableMeta text={kw.recommendation_reason} mode="preview" maxChars={40} iconLabel="推荐理由" align="end" />
                        </td>
                        <td className="text-center">
                          <Badge variant="outline" className="text-xs">
                            {intentLabels[kw.intent || ''] || kw.intent || '-'}
                          </Badge>
                        </td>
                        {/* [Social CTO 诊断 3.2] 价格 round 整十展示(后台精确价不动 · 仅展示层) */}
                        {/* [2026-06-08] 超红海词不出保证价 · 显示「需深度报价」不裸 ¥0 */}
                        <td className="text-right">{isSro || isInfoOnly || isGuaranteeUnavail ? <span className="text-muted-foreground">—</span> : `¥${roundPrice(kw.entry.price).toLocaleString()}`}</td>
                        <td className="text-right font-medium">{isSro ? <span className="text-xs font-medium text-red-600">{SUPER_RED_OCEAN_COPY.priceLabel}</span> : isInfoOnly ? <span className="text-xs text-muted-foreground">信息型·不报价</span> : isGuaranteeUnavail ? <span className="text-xs font-medium text-amber-600">{GUARANTEE_UNAVAILABLE_COPY.label}</span> : `¥${roundPrice(kw.standard.price).toLocaleString()}`}</td>
                        <td className="text-right">{isSro || isInfoOnly || isGuaranteeUnavail ? <span className="text-muted-foreground">—</span> : `¥${roundPrice(kw.flagship.price).toLocaleString()}`}</td>
                        <td className="text-center" onClick={e => e.stopPropagation()}>
                          <Button
                            variant="ghost" size="sm"
                            onClick={() => handleDelete(kw.id)}
                            disabled={deletingId === kw.id}
                            className="text-red-400 hover:text-red-400 h-7 w-7 p-0"
                          >
                            {deletingId === kw.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
                          </Button>
                        </td>
                        <td className="text-center" onClick={e => e.stopPropagation()}>
                          {kw.audit_status ? (
                            <Badge variant="outline" className="text-[10px] border-green-500/30 text-green-400 px-1">
                              已复核
                            </Badge>
                          ) : anomaly ? (
                            <Button
                              variant="ghost" size="sm"
                              onClick={() => handleAuditKeyword(kw.id, kw.keyword)}
                              disabled={auditingId === kw.id}
                              className="text-yellow-400 hover:text-yellow-400 hover:bg-yellow-500/20 h-7 px-1.5 text-xs gap-0.5"
                            >
                              {auditingId === kw.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Brain className="w-3.5 h-3.5" />}
                              {auditingId === kw.id ? '复核中' : '重新评估'}
                            </Button>
                          ) : (
                            <span className="inline-block h-7" />
                          )}
                        </td>
                      </tr>
                      {/* 展开：定价维度详情 */}
                      {isExpanded && (
                        <tr className={red ? 'bg-red-500/5' : anomaly ? 'bg-yellow-500/5' : 'bg-muted/50'}>
                          <td colSpan={9} className="px-4 py-3">
                            <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3 md:grid-cols-5 md:gap-3">
                              <div className="bg-card rounded p-2 border border-border">
                                <p className="text-muted-foreground">市场热度</p>
                                <p className="font-medium text-sm">{kw.search_volume ?? '-'}</p>
                              </div>
                              <div className="bg-card rounded p-2 border border-border">
                                <p className="text-muted-foreground">竞争强度</p>
                                <p className="font-medium text-sm">{kw.effective_competition ?? '-'}</p>
                              </div>
                              <div className="bg-card rounded p-2 border border-border">
                                <p className="text-muted-foreground">优化难度</p>
                                <p className="font-medium text-sm">{kw.difficulty_score ?? '-'}</p>
                              </div>
                              <div className="bg-card rounded p-2 border border-border">
                                <p className="text-muted-foreground">商业价值</p>
                                <p className="font-medium text-sm">{kw.value_score ?? '-'}</p>
                              </div>
                              <div className="bg-card rounded p-2 border border-border">
                                <p className="text-muted-foreground">用户阶段</p>
                                <p className="font-medium text-sm">{funnelLabels[kw.funnel_stage || ''] || '-'}</p>
                              </div>
                            </div>
                            {/* 审计结果 */}
                            {kw.audit_status && (
                              <div className={`mt-2 rounded px-3 py-2 text-xs ${
                                kw.audit_status.startsWith('adjusted') || kw.audit_status.startsWith('deep_probed')
                                  ? 'bg-green-500/10 border border-green-500/20 text-green-400'
                                  : 'bg-blue-500/10 border border-blue-500/20 text-blue-400'
                              }`}>
                                <span className="font-medium">AI 复核结果:</span>
                                {kw.audit_note || '审核完毕'}
                                {kw.price_before_audit != null && (
                                  <span className="ml-2 text-muted-foreground">（复核前 ¥{kw.price_before_audit}）</span>
                                )}
                              </div>
                            )}
                            {/* [报价解释层一期 2026-06-13] 为什么是这个价 · 人话拆解 */}
                            <WhyThisPrice kw={kw} className="mt-2" mixContext={mixContext} />
                            <p className="text-[10px] text-muted-foreground mt-2">
                              综合市场数据、AI 搜索竞争分析、大模型商业价值评估三维定价
                            </p>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
            </TooltipProvider>
          </div>
        </CardContent>
      </Card>

      {/* 操作栏 · [CTO-15.23 2026-05-22] 撤回 sticky bottom · 老板报"底下还是有遮罩"
        * 跟 v9→v10 mobile 色块同款 BUG(feedback_ios_sticky_zoom_trap):
        * iOS Safari pinch zoom 时 sticky bottom 绑 visualViewport 不缩放 → 底部 bar 视觉"卡屏幕"
        * 老 mobile = sticky bottom-3 bg-background/95 shadow-lg backdrop-blur → 整条"遮罩"
        * 新:取消 sticky · 改 inline flow(跟 NewDiagnosis v10 撤 sticky 一致)
        * 桌面布局保持(原 sm:static 无变化)
        */}
      <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:mt-0">
        {/* [包三] 这颗按钮原来内联了第二套重算逻辑 + 第二套文案(同一个端点)。
            已收口到上面唯一的 handleRecalculate —— 同一件事只留一个说法、一个改动点。 */}
        <Button
          onClick={handleRecalculate}
          disabled={recalculating || approving}
          variant="outline"
          className="min-h-11 gap-2 border-amber-500/40 text-amber-500 hover:bg-amber-500/10 sm:min-h-9 sm:w-auto"
          title="重新算一遍每个词的价格 · 发现价格明显不对时用它"
        >
          {recalculating ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          {recalculating ? '正在重新算…' : '重新算一遍价格'}
        </Button>
        <FeatureTooltip
          featureId="sandbox_quote_approve_btn"
          stepId="first_quote"
          title="审核通过 + 发给客户"
          content="检查关键词价格无异常后, 点这里把报价发给客户 (客户选档位的操作会自动模拟)"
          side="bottom"
          disabled={!isSandboxActive()}
        >
          <Button
            onClick={handleApprove}
            disabled={approving || keywords.length === 0 || recalculating || coefficientBusy}
            className="min-h-11 w-full gap-2 bg-foreground text-background hover:bg-foreground/90 sm:w-auto"
          >
            {approving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            确认无误，发送报价给客户
          </Button>
        </FeatureTooltip>
      </div>
    </div>
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 5: Customer Confirmation                                      */
/* ------------------------------------------------------------------ */

function Step5CustomerConfirm({
  session, sessionData, onRefresh,
}: {
  session: Session;
  sessionData: any;
  onRefresh: () => void;
}) {
  const tier = sessionData?.selected_tier;
  const price = sessionData?.confirmed_total_price;
  const [recalling, setRecalling] = useState(false);
  const [confirmDialog, confirm] = useConfirmDialog();

  const handleRecall = async () => {
    const ok = await confirm({ title: '撤回报价?', description: '撤回后可修改关键词和价格,修改完需重新发送给客户。', confirmLabel: '撤回', danger: true });
    if (!ok) return;
    setRecalling(true);
    try {
      await api.post(`/api/keyword-selection/${session.token}/recall`);
      onRefresh();
    } finally {
      setRecalling(false);
    }
  };

  return (
    <>
      {confirmDialog}
      <Card>
      <CardHeader className="pb-3 flex flex-row items-center justify-between">
        <CardTitle className="text-base flex items-center gap-2">
          {session.status === 'quoted' ? (
            <><Loader2 className="w-4 h-4 animate-spin text-brand" />等待客户确认套餐...</>
          ) : (
            <><Check className="w-4 h-4 text-green-500" />客户已确认</>
          )}
        </CardTitle>
        <Button variant="ghost" size="sm" onClick={onRefresh}><RefreshCw className="w-4 h-4" /></Button>
      </CardHeader>
      <CardContent>
        {session.status === 'quoted' ? (
          <div className="space-y-3">
            {isSandboxActive() && (
              <SandboxVideoSlot
                src="/sandbox/client-select-tier.mp4"
                title="选套餐确认"
                hint="这一步在客户那边:客户看到入门/标准/旗舰三档报价(对应不同 AI 出现率和费用),选好档位后点确认。看完这段录屏,就进入签约收款。"
                doneLabel="客户已确认套餐"
                doneSub="正在进入「签约收款」…"
                onComplete={() => { advanceSandboxClientConfirm(); onRefresh(); }}
              />
            )}
            <div className="flex items-center gap-3 py-6 justify-center text-muted-foreground">
              <Clock className="w-5 h-5" />
              <span className="text-sm">报价已发送给客户，页面每 8 秒自动刷新</span>
            </div>
            <div className="bg-muted rounded-xl p-3 text-xs text-muted-foreground">
              <p className="font-medium mb-1 text-foreground">客户端操作：</p>
              <ol className="list-decimal list-inside space-y-0.5">
                <li>客户打开选词链接，查看三个套餐方案</li>
                <li>选择一个套餐（入门版 / 标准版 / 旗舰版）</li>
                <li>点击「确认报价」，此页面会自动更新</li>
              </ol>
            </div>
            <Button
              variant="outline"
              size="sm"
              className="w-full text-amber-400 border-amber-500/20 hover:bg-amber-500/20"
              onClick={handleRecall}
              disabled={recalling}
            >
              {recalling ? <Loader2 className="w-4 h-4 animate-spin mr-1" /> : null}
              撤回报价，修改后重发
            </Button>
          </div>
        ) : (
          <div className="bg-green-500/10 border border-green-500/20 rounded-xl p-4 space-y-2">
            <p className="text-sm">
              套餐：<span className="font-bold text-green-400">
                {tier === 'entry' ? '入门版' : tier === 'standard' ? '标准版' : '旗舰版'}
              </span>
            </p>
            {price && (
              <p className="text-sm">确认总价：<span className="font-bold text-lg text-green-400">¥{Number(price).toLocaleString()} · 累计达标30天</span></p>
            )}
            <p className="text-xs text-muted-foreground">确认时间：{sessionData?.confirmed_at || '-'}</p>
          </div>
        )}
      </CardContent>
    </Card>
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 6: Contract & Payment                                         */
/* ------------------------------------------------------------------ */

function Step6Payment({ session, sessionData, onRefresh }: { session: Session; sessionData: any; onRefresh: () => void }) {
  const { user } = useAuth();
  const isAdmin = user?.is_admin === true;
  const [finalPrice, setFinalPrice] = useState('');
  const [serviceMonths, setServiceMonths] = useState('1');
  const [salesNotes, setSalesNotes] = useState('');
  const [confirmCode, setConfirmCode] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [paying, setPaying] = useState(false);
  const [reverting, setReverting] = useState(false);
  const [done, setDone] = useState(session.status === 'active');
  // Stage 1 Batch 4 (2026-05-18) · 标 step 2 完成 · 必须等收款完成才标 · 不在前面提前标
  const markStep = useMarkStepCompleted();
  const [confirmDialog, confirm] = useConfirmDialog();

  const handleRevertStatus = async () => {
    const ok = await confirm({ title: '回退到上一步?', description: '此操作将记录在操作日志中。', confirmLabel: '回退' });
    if (!ok) return;
    setReverting(true);
    try {
      const { data } = await api.post(`/api/keyword-selection/${session.token}/revert-status`);
      if (data.success) {
        toast.success(`已回退: ${data.old_status} → ${data.new_status}`);
        onRefresh();
      } else {
        toast.error(data.detail || data.error || '回退失败');
      }
    } catch (e: any) {
      toast.error('回退失败: ' + (e?.response?.data?.detail || e.message));
    } finally {
      setReverting(false);
    }
  };

  useEffect(() => {
    if (sessionData?.confirmed_total_price && !finalPrice) {
      setFinalPrice(String(Math.round(sessionData.confirmed_total_price)));
    }
  }, [sessionData]);

  // Stage 1 Batch 4 (2026-05-18) · 沙盒态自动填确认码 · 让收款按钮可点
  useEffect(() => {
    if (isSandboxActive() && !confirmCode) {
      setConfirmCode('888888');
    }
  }, [confirmCode]);

  const handleSalesConfirm = async () => {
    if (!finalPrice) return;
    setConfirming(true);
    try {
      await api.post(`/api/keyword-selection/${session.token}/sales-confirm`, {
        final_price: Number(finalPrice),
        service_months: Number(serviceMonths),
        sales_notes: salesNotes || undefined,
      });
      onRefresh();
      toast.success('签约信息已确认，请输入确认码完成收款');
    } catch (e: any) {
      toast.error('确认失败: ' + (e?.response?.data?.detail || e.message));
    } finally {
      setConfirming(false);
    }
  };

  const handleMarkPaid = async () => {
    setPaying(true);
    try {
      const { data } = await api.post(`/api/keyword-selection/${session.token}/mark-paid`, { confirm_code: confirmCode });
      setDone(true);
      toast.success(`收款已确认！${data.keywords_created || 0}个关键词已自动进入写作大厅。`);
      // 真实代理: 在线路径走完即标 step 2 完成
      // 沙盒: 不在这里标 · 由 PricingCenter 的路径选择器 onFinish 控 · 让用户选"再试另一条 / 跳过"
      if (!isSandboxActive()) {
        markStep('first_quote');
      }
    } catch (e: any) {
      toast.error('操作失败: ' + (e?.response?.data?.detail || e.message));
    } finally {
      setPaying(false);
    }
  };

  if (done) {
    return (
      <Card className="border border-green-500/20 rounded-xl">
        <CardContent className="pt-8 pb-8 text-center space-y-3">
          <div className="w-16 h-16 rounded-full bg-green-500/10 border border-green-500/20 flex items-center justify-center mx-auto">
            <Check className="w-8 h-8 text-green-400" />
          </div>
          <p className="text-lg font-bold text-green-400">在线报价流程完成</p>
          <p className="text-sm text-muted-foreground">关键词已自动推送到写作大厅，AI 写作系统将开始内容创作</p>
          <Button variant="outline" size="sm" className="mt-4 border-amber-500/30 text-amber-400 hover:bg-amber-500/10" onClick={handleRevertStatus} disabled={reverting}>
            <RotateCcw className="mr-1 h-3 w-3" />
            {reverting ? '回退中...' : '回退到上一步'}
          </Button>
        </CardContent>
      </Card>
    );
  }

  const isPendingPayment = session.status === 'pending_payment' || session.status === 'payment_overdue';

  // [CTO-15.23 2026-05-11 P0#6] 客户已选词清单 · 老板痛点:签约页只有总价 + 确认码 · 缺词/价/篇数
  const selectedTier = (sessionData?.selected_tier as 'entry' | 'standard' | 'flagship') || 'standard';
  const finalIdsSet = new Set<number>(sessionData?.final_keyword_ids || []);
  const clusters = (sessionData?.clusters_data?.clusters || []) as Array<{
    cluster_name: string;
    business_tag?: string;
    is_selected?: boolean;
    confirmed_covered_count?: number;
    core_keywords: Array<{ id: number; keyword: string; is_selected?: boolean; entry?: { price: number; articles: number }; standard?: { price: number; articles: number }; flagship?: { price: number; articles: number } }>;
  }>;
  // 平铺模式:从 pricing_data.keywords 按 final_keyword_ids 筛
  const flatKeywords = (sessionData?.pricing_data?.keywords || []) as Array<{
    id: number;
    keyword: string;
    category_label?: string;
    entry?: { price: number; articles: number };
    standard?: { price: number; articles: number };
    flagship?: { price: number; articles: number };
  }>;
  const tierLabelMap: Record<string, string> = { entry: '入门版', standard: '标准版', flagship: '旗舰版' };
  const tierLabel = tierLabelMap[selectedTier] || '标准版';

  const isClusterMode = clusters.length > 0;
  const selectedClusters = isClusterMode ? clusters.filter(c => c.is_selected || c.core_keywords?.some(kw => kw.is_selected)) : [];
  const flatSelected = !isClusterMode ? flatKeywords.filter(kw => finalIdsSet.has(kw.id)) : [];

  const totalKeywords = isClusterMode
    ? selectedClusters.reduce((s, c) => s + (c.core_keywords?.filter(kw => kw.is_selected).length || 0), 0)
    : flatSelected.length;
  const totalArticles = isClusterMode
    ? selectedClusters.reduce((s, c) => s + (c.core_keywords?.filter(kw => kw.is_selected).reduce((ss, kw) => ss + (kw[selectedTier]?.articles || 0), 0) || 0), 0)
    : flatSelected.reduce((s, kw) => s + (kw[selectedTier]?.articles || 0), 0);
  const totalCovered = isClusterMode
    ? selectedClusters.reduce((s, c) => s + (c.confirmed_covered_count || 0), 0)
    : 0;

  return (
    <>
      {confirmDialog}
      <div className="space-y-4">
      {/* [P0#6] 客户已选词清单 */}
      {(totalKeywords > 0) && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center justify-between">
              <span>客户已选词清单</span>
              <span className="text-xs font-normal text-muted-foreground">
                {tierLabel} · {totalKeywords}核心词
                {totalCovered > 0 && ` · ${totalCovered}相关搜索参考`}
                {' · '}{totalArticles}篇
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {isClusterMode ? (
              <div className="space-y-3 max-h-[40vh] overflow-y-auto">
                {selectedClusters.map(cluster => {
                  const sel = (cluster.core_keywords || []).filter(kw => kw.is_selected);
                  const clusterPrice = sel.reduce((s, kw) => s + (kw[selectedTier]?.price || 0), 0);
                  const clusterArticles = sel.reduce((s, kw) => s + (kw[selectedTier]?.articles || 0), 0);
                  return (
                    <div key={cluster.cluster_name} className="bg-muted/30 rounded-lg p-3 border border-border">
                      <div className="flex items-center justify-between mb-2">
                        <p className="text-sm font-semibold">
                          🎯 {cluster.business_tag || cluster.cluster_name}
                          <span className="text-xs font-normal text-muted-foreground ml-2">
                            {sel.length}词 · {clusterArticles}篇
                          </span>
                        </p>
                        <p className="text-sm font-bold text-brand tabular-nums">
                          ¥{clusterPrice.toLocaleString()}
                        </p>
                      </div>
                      <div className="space-y-1">
                        {sel.map(kw => (
                          <div key={kw.id} className="flex items-center justify-between text-xs gap-2">
                            <span className="text-foreground/80 break-words leading-snug flex-1" title={kw.keyword}>• {kw.keyword}</span>
                            <span className="text-muted-foreground tabular-nums shrink-0">
                              {kw[selectedTier]?.articles || 0}篇 · ¥{(kw[selectedTier]?.price || 0).toLocaleString()}
                            </span>
                          </div>
                        ))}
                      </div>
                      {(cluster.confirmed_covered_count || 0) > 0 && (
                        <p className="text-[10px] text-muted-foreground mt-2">
                          + {cluster.confirmed_covered_count} 个相关搜索参考(顺带覆盖 · 不单独监测 · 不承诺达标)
                        </p>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="space-y-1 max-h-[40vh] overflow-y-auto">
                {flatSelected.map(kw => (
                  <div key={kw.id} className="flex items-center justify-between text-sm px-3 py-2 rounded hover:bg-muted/30">
                    <span className="text-foreground/80 break-words leading-snug flex-1" title={kw.keyword}>• {kw.keyword}</span>
                    <span className="text-muted-foreground tabular-nums shrink-0 ml-3">
                      {kw[selectedTier]?.articles || 0}篇 · ¥{(kw[selectedTier]?.price || 0).toLocaleString()}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* [P4 缺口作战计划 2026-08-08] 交付计划区块。
          落点按合同 §3.1:紧接「客户已选词清单」之后、签约确认之前。
          🔴 未付款 / 容量 0 时它自己显示成研究预览态(不出现「去写这篇」)——
             那个判断在**服务端**,这里不做任何容量判断,只挂载。
          [阶段 1 · 2026-08-10] 组件改名 GapPlanSection → GapPlanExecution,
          并在执行闸未开时自己折成一句提示(分档逻辑在组件内,挂载点不判断)。 */}
      {session.quote_id ? <GapPlanExecution quoteId={Number(session.quote_id)} /> : null}

      <Card>
      <CardHeader className="pb-3"><CardTitle className="text-base">签约确认</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <div>
            <label className="text-xs text-muted-foreground mb-1 block">签约金额 (元 · 累计达标30天)</label>
            <Input type="number" value={finalPrice} onChange={e => setFinalPrice(e.target.value)} disabled={isPendingPayment} />
          </div>
          <div>
            <label className="text-xs text-muted-foreground mb-1 block">服务周期 (月)</label>
            <Input type="number" value={serviceMonths} onChange={e => setServiceMonths(e.target.value)} min="1" disabled={isPendingPayment} />
          </div>
          <div>
            <label className="text-xs text-muted-foreground mb-1 block">备注</label>
            <Input value={salesNotes} onChange={e => setSalesNotes(e.target.value)} placeholder="可选" disabled={isPendingPayment} />
          </div>
        </div>

        {session.status === 'confirmed' && (
          <FeatureTooltip
            featureId="sandbox_quote_sales_confirm_btn"
            stepId="first_quote"
            title="客户已选入门版 · 确认签约"
            content="确认签约信息 (沙盒已自动填好金额 ¥3,000) · 然后进入收款环节"
            side="bottom"
            disabled={!isSandboxActive() || session.status !== 'confirmed'}
          >
            <Button onClick={handleSalesConfirm} disabled={confirming || !finalPrice} className="gap-1">
              {confirming ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
              确认签约信息
            </Button>
          </FeatureTooltip>
        )}

        <div className="border-t pt-4">
          {/* 管理员需要确认码（内控），普通用户直接收款 */}
          {isAdmin ? (
            <>
              <label className="text-xs text-muted-foreground mb-1 block">收款确认码（管理员内控）</label>
              <div className="flex gap-3 items-end">
                <div className="flex-1">
                  <Input
                    value={confirmCode}
                    onChange={e => setConfirmCode(e.target.value)}
                    placeholder={isPendingPayment ? '输入确认码完成收款' : '签约确认后输入确认码'}
                    disabled={!isPendingPayment}
                  />
                </div>
                <FeatureTooltip
                  featureId="sandbox_quote_mark_paid_btn"
                  stepId="first_quote"
                  title="点这里完成收款 · 跑通在线报价"
                  content="教程已自动填好确认码 (888888) · 点击后关键词自动进入写作大厅, step 2 完成"
                  side="bottom"
                  disabled={!isSandboxActive() || !isPendingPayment}
                >
                  <Button
                    onClick={handleMarkPaid}
                    disabled={paying || !isPendingPayment || !confirmCode.trim()}
                    className="gap-1"
                  >
                    {paying ? <Loader2 className="w-4 h-4 animate-spin" /> : <CreditCard className="w-4 h-4" />}
                    确认收款 → 进入写作大厅
                  </Button>
                </FeatureTooltip>
              </div>
            </>
          ) : (
            <div className="flex gap-3 items-end">
              {/* 2026-05-24 BUGFIX: 非 admin 代理也要有沙盒引导
                  原本只有 admin 走 if 分支的两个按钮包了 FeatureTooltip · 代理走 else 啥也看不到 */}
              <FeatureTooltip
                featureId="sandbox_quote_agent_mark_paid_btn"
                stepId="first_quote"
                title="点这里完成收款 · 跑通在线报价"
                content="客户已经确认报价 · 这一步代理点确认收款 · 关键词自动进入写作大厅, step 2 完成"
                side="bottom"
                disabled={!isSandboxActive() || !isPendingPayment}
              >
                <Button
                  onClick={handleMarkPaid}
                  disabled={paying || !isPendingPayment}
                  className="gap-1 w-full sm:w-auto"
                >
                  {paying ? <Loader2 className="w-4 h-4 animate-spin" /> : <CreditCard className="w-4 h-4" />}
                  确认收款 → 进入写作大厅
                </Button>
              </FeatureTooltip>
            </div>
          )}
          {!isPendingPayment && (
            <p className="text-xs text-muted-foreground mt-1">请先点击"确认签约信息"，然后完成收款</p>
          )}
          {(isPendingPayment || session.status === 'confirmed') && (
            <div className="mt-3 flex justify-end">
              <Button variant="ghost" size="sm" className="text-amber-400 hover:text-amber-300 hover:bg-amber-500/10" onClick={handleRevertStatus} disabled={reverting}>
                <RotateCcw className="mr-1 h-3 w-3" />
                {reverting ? '回退中...' : '回退到上一步'}
              </Button>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
    </div>
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  Customer Link Bar — 常驻在所有步骤顶部                               */
/* ------------------------------------------------------------------ */

function CustomerLinkBar({ token }: { token: string }) {
  const [copied, setCopied] = useState(false);
  const link = `${window.location.origin}/s/${token}`;

  const handleCopy = async () => {
    const ok = await copyToClipboard(link);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } else {
      toast.error('复制失败');
    }
  };

  return (
    <div className="flex items-center gap-2 mb-2 px-3 py-2 bg-card border border-border rounded-xl">
      <Link2 className="w-4 h-4 text-brand shrink-0" />
      <span className="text-xs text-brand shrink-0">客户链接</span>
      <input
        value={link}
        readOnly
        className="flex-1 bg-transparent text-xs font-mono text-foreground outline-hidden min-w-0 truncate"
      />
      <Button variant="ghost" size="sm" onClick={handleCopy} className="h-6 px-2 gap-1 text-xs text-brand hover:text-brand-hover shrink-0">
        {copied ? <Check className="w-3 h-3 text-green-500" /> : <Copy className="w-3 h-3" />}
        {copied ? '已复制' : '复制'}
      </Button>
      <Button variant="ghost" size="sm" onClick={() => window.open(link, '_blank')} className="h-6 px-2 text-brand hover:text-brand-hover shrink-0">
        <ExternalLink className="w-3 h-3" />
      </Button>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Main Page                                                          */
/* ------------------------------------------------------------------ */

export default function OnlineQuoteFlow({ topBarSlot = null }: { topBarSlot?: HTMLElement | null } = {}) {
  const { overview, isMember, loading: organizationLoading, error: organizationError } = useOrganization();
  const canArchiveQuote = !organizationLoading && !organizationError
    && (!isMember || Boolean(overview?.identity.capabilities.includes('team.output_handoff')));
  // 报价编辑权限 —— 与 Step4Review 的 canRecalculateQuote 同一口径(既有 quote.create,不新增权限位)
  const canEditQuotePricing = !organizationLoading && !organizationError
    && (!isMember || Boolean(overview?.identity.capabilities.includes('quote.create')));
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selectedToken, setSelectedToken] = useState<string | null>(null);
  const [sessionData, setSessionData] = useState<any>(null);
  const [pricingData, setPricingData] = useState<PricingData | null>(null);
  const [isNew, setIsNew] = useState(false);
  // 页顶「修改本次报价系数」在途 → Step4 的「发送报价给客户」置灰(原 Step4 内部联动上提)
  const [coefficientBusy, setCoefficientBusy] = useState(false);
  const isMobile = useIsMobile();
  const pollRef = useRef<ReturnType<typeof setInterval>>();

  const loadSessions = useCallback(async () => {
    try {
      // [WO_QUOTE_LIST_DATE 2026-08-05] 🔴 limit 必须跟着日期口径一起调,不能只改显示。
      //   后端 `list_selection_sessions` 是 `ORDER BY s.updated_at DESC LIMIT %s` ——
      //   **截断集按 updated_at 选,页面按业务时间排**。生产实测(2026-08-08 只读取证,
      //   端点可见集合 133 条):按 updated_at 取前 50 与按业务时间取前 50,
      //   **差 20 条**(20 条该在第一页的被挤掉、20 条老单占着位)= 第一页 40% 是错的。
      //   而且 RBAC 过滤发生在 DB LIMIT **之后**(api/selection_api.py:2019 过滤那 50 条),
      //   所以非管理员的单子若不在全局前 50,现在**一条都看不到**。
      //   把 limit 提到覆盖全表即可两个问题一起消。
      //   ⚠️ 残留:根治要把后端 ORDER BY 也换成业务时间(db/diagnosis_db.py:8204)——
      //     那个文件被 p1capacity / brandlatest 双声明,本包不碰,已在交付说明里出条目。
      const [sessionRes, quoteRes] = await Promise.allSettled([
        api.get('/api/keyword-selection/list', { params: { limit: 500 } }),
        api.get('/api/quotes', { params: { limit: 500 } }),
      ]);
      const sessionList: Session[] = sessionRes.status === 'fulfilled'
        ? (sessionRes.value.data.sessions || [])
        : [];
      const sessionQuoteIds = new Set(sessionList.map(s => Number(s.quote_id)).filter(Boolean));
      const quoteItems = quoteRes.status === 'fulfilled'
        ? (quoteRes.value.data?.items || quoteRes.value.data?.data || [])
        : [];
      const missingQuoteSessions: Session[] = quoteItems
        .filter((q: any) => q?.id && !sessionQuoteIds.has(Number(q.id)))
        .map((q: any) => ({
          token: `quote:${q.id}`,
          quote_id: Number(q.id),
          brand_name: q.brand_name || '未命名客户',
          industry: q.industry || '',
          city: q.city || '',
          status: q.status || 'draft',
          selected_count: Number(q.total_keywords || 0),
          selected_tier: q.tier,
          confirmed_total_price: q.confirmed_total_price,
          final_price: q.paid_amount || q.monthly_price,
          created_at: q.created_at,
          confirmed_at: q.confirmed_at,
          is_quote_placeholder: true,
        }));
      // [WO_QUOTE_LIST_DATE 2026-08-05] 排序键与**显示**同源。原来按 updated_at 排、
      // 按 updated_at 显示,两边一致所以看不出问题;显示改成业务时间后排序若不跟着改,
      // 列表会变成"上面写 07-23、下面写 08-06"的看不懂顺序。
      const merged = [...sessionList, ...missingQuoteSessions].sort(
        (a, b) => quoteOrderDateSortKey(b) - quoteOrderDateSortKey(a),
      );
      setSessions(merged);
    } catch (e) {
      console.error('[OnlineQuoteFlow] 加载报价列表失败:', e);
    }
  }, []);

  const initializedRef = useRef(false);
  useEffect(() => {
    loadSessions().then(() => {
      // 首次加载：没有选中 session 时自动进入新建模式，避免大片空白
      // 沙盒模式 · 不自动 setIsNew · 让用户主动点击中央"新建报价"按钮 (有 spotlight 引导)
      if (!initializedRef.current) {
        initializedRef.current = true;
        if (!selectedToken && !isSandboxActive()) setIsNew(true);
      }
    });
  }, [loadSessions]);

  const loadSessionDetail = useCallback(async (token: string) => {
    try {
      const { data } = await api.get(`/api/s/${token}`);
      setSessionData(data);
      setPricingData(data.pricing_data || null);
      const count = (data.selected_ids?.length || 0) + (data.custom_keywords?.length || 0);
      setSessions(prev => prev.map(s => s.token === token ? { ...s, status: data.status, selected_count: count } : s));
    } catch { /* ignore */ }
  }, []);

  // Poll when waiting for customer
  useEffect(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    if (!selectedToken) return;
    const session = sessions.find(s => s.token === selectedToken);
    if (session && ['selecting', 'business_lines_submitted', 'quoted'].includes(session.status)) {
      pollRef.current = setInterval(() => loadSessionDetail(selectedToken), 8000);
    }
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [selectedToken, sessions, loadSessionDetail]);

  const createSessionFromQuotePlaceholder = useCallback(async (quoteId: number): Promise<string | null> => {
    try {
      const { data } = await api.post('/api/keyword-selection/create', { quote_id: quoteId });
      const token = data?.token;
      if (!token) throw new Error('未返回选词链接');
      await loadSessions();
      return token;
    } catch (e: any) {
      toast.error('恢复历史报价失败: ' + formatApiErrorForDisplay(e, '请稍后重试'));
      return null;
    }
  }, [loadSessions]);

  const handleSelect = async (token: string) => {
    if (token.startsWith('quote:')) {
      const quoteId = Number(token.slice('quote:'.length));
      if (!quoteId) return;
      const realToken = await createSessionFromQuotePlaceholder(quoteId);
      if (!realToken) return;
      setSelectedToken(realToken);
      setIsNew(false);
      loadSessionDetail(realToken);
      return;
    }
    setSelectedToken(token);
    setIsNew(false);
    loadSessionDetail(token);
  };

  const handleNewQuote = () => {
    setIsNew(true);
    setSelectedToken(null);
    setSessionData(null);
    setPricingData(null);
  };

  const handleSessionCreated = (token: string) => {
    setIsNew(false);
    setSelectedToken(token);
    loadSessions();
    loadSessionDetail(token);
  };

  const selectedSession = sessions.find(s => s.token === selectedToken);
  const currentStep: StepKey = isNew
    ? 'generate'
    : selectedSession
      ? (STATUS_TO_STEP[selectedSession.status] || 'generate')
      : 'generate';

  // [CTO-15.23 2026-05-05] 删除失败不再静默 · toast 显示具体错误 + 强制刷新 sidebar
  // 老板反馈: 删除按钮点击后无法删除 · 根因 catch 静默吞错(任何 401/403/500/网络错都看不到)
  const handleDelete = async (quoteId: number) => {
    try {
      await api.delete(`/api/quotes/${quoteId}`, { data: { reason: '报价中心用户归档' } });
      const removed = sessions.find(session => session.quote_id === quoteId);
      setSessions(prev => prev.filter(s => s.quote_id !== quoteId));
      if (removed && selectedToken === removed.token) {
        setSelectedToken(null);
        setSessionData(null);
        setPricingData(null);
      }
      toast.success('报价已归档 · 数据完整保留', {
        description: '误操作可立即撤销。',
        duration: 12000,
        action: {
          label: '撤销',
          onClick: () => {
            void api.post(`/api/quotes/${quoteId}/restore`)
              .then(() => {
                toast.success('报价已恢复');
                return loadSessions();
              })
              .catch(error => toast.error(formatApiErrorForDisplay(error, '恢复报价失败')));
          },
        },
      });
    } catch (e: any) {
      console.error('[OnlineQuoteFlow] 删除会话失败:', e);
      toast.error(formatApiErrorForDisplay(e, '归档失败'));
      // 失败后强制重拉 · 防本地状态与服务端不一致
      loadSessions();
    }
  };

  const handleBatchDelete = async (quoteIds: number[]) => {
    const results = await Promise.allSettled(
      quoteIds.map(quoteId => api.delete(`/api/quotes/${quoteId}`, { data: { reason: '报价中心批量归档' } }))
    );
    const archivedQuoteIds: number[] = [];
    const failedQuoteIds: { quoteId: number; reason: string }[] = [];
    results.forEach((r, i) => {
      if (r.status === 'fulfilled') {
        archivedQuoteIds.push(quoteIds[i]);
      } else {
        const reason = formatApiErrorForDisplay(r.reason, '未知错误');
        failedQuoteIds.push({ quoteId: quoteIds[i], reason });
        console.error(`[OnlineQuoteFlow] 批量归档失败 quote_id=${quoteIds[i]}:`, r.reason);
      }
    });
    const selectedQuoteId = sessions.find(session => session.token === selectedToken)?.quote_id;
    setSessions(prev => prev.filter(s => !archivedQuoteIds.includes(s.quote_id)));
    if (selectedQuoteId && archivedQuoteIds.includes(selectedQuoteId)) {
      setSelectedToken(null);
      setSessionData(null);
      setPricingData(null);
    }
    if (archivedQuoteIds.length > 0) {
      toast.success(`已归档 ${archivedQuoteIds.length} 个报价 · 数据均保留`);
    }
    if (failedQuoteIds.length > 0) {
      const firstReason = failedQuoteIds[0].reason;
      toast.error(`${failedQuoteIds.length} 个归档失败: ${firstReason}`);
      loadSessions(); // 失败后强制重拉
    }
  };

  const handleRefresh = useCallback(() => {
    if (selectedToken) {
      loadSessionDetail(selectedToken);
      loadSessions();
    }
  }, [selectedToken, loadSessionDetail, loadSessions]);

  const handleCoefficientBusyChange = useCallback((busy: boolean) => setCoefficientBusy(busy), []);

  /* [WO_QUOTE_COEFFICIENT_PRIVATE_TOPBAR 2026-08-11 §7] 当前报价上下文**单点**在这里派生。
     顶部控制栏不自己拉 session / quote 列表 —— 否则切客户后顶栏会停在上一客户的系数上。 */
  const quotePricingContext: CurrentQuoteContext = {
    quoteId: selectedSession?.quote_id ?? null,
    status: selectedSession?.status ?? null,
    isPlaceholder: Boolean(selectedSession?.is_quote_placeholder),
    pricingGeneratedAt: pricingData?.generated_at ?? null,
    canEditQuote: canEditQuotePricing,
    readOnly: false,
  };

  /* [R3 2026-08-11] 报价设置栏:**在本组件的 React 树里渲染**(于是同步拿到 selectedSession /
     pricingData,切客户不会慢一帧显示上一客户系数,也照常吃到下面的 PricingPrivacyProvider),
     但 DOM 通过 portal 落到 PricingCenter 的固定槽位 —— 那里与滚动区同级,不随内容滚动。
     🔴 拿不到槽位时**退化成内联渲染**(fail-open):宁可位置不理想,也不能整个入口消失。 */
  const pricingControlBar = (
    <QuotePricingControlBar
      context={quotePricingContext}
      onQuoteRefresh={handleRefresh}
      onCoefficientBusyChange={handleCoefficientBusyChange}
    />
  );

  return (
    /* [WO_QUOTE_COEFFICIENT_PRIVATE_TOPBAR 2026-08-11 · 返修 R2] 隐私态整页单点。
       返修前它只是顶栏的局部 state,于是 Step4 关键词展开区的「为什么是这个价」
       (单篇成本 / 基础成本公式 / 账号报价系数)完全不受眼睛控制 —— 演示时就从那儿漏。
       Provider 同时负责:切报价回隐藏(渲染期同步派生)· 标签页返回回隐藏 · 零持久化。 */
    <PricingPrivacyProvider quoteId={selectedSession?.quote_id ?? null}>
    {topBarSlot ? createPortal(pricingControlBar, topBarSlot) : pricingControlBar}
    <div className="relative flex h-full min-h-0 overflow-hidden md:h-[calc(100dvh-64px)]">
      {!isMobile && (
        <SessionSidebar
          sessions={sessions}
          selected={selectedToken || undefined}
          onSelect={handleSelect}
          onNew={handleNewQuote}
          onDelete={handleDelete}
          onBatchDelete={handleBatchDelete}
          canArchive={canArchiveQuote}
        />
      )}

      <div className="min-w-0 flex-1 overflow-y-auto overflow-x-hidden">
        {/* 顶栏 */}
        <div className="bg-card border-b border-border px-3 py-2 md:px-5">
          <div className="flex items-center gap-3">
            <div className="h-8 w-8 rounded-lg bg-brand/10 flex items-center justify-center shrink-0">
              <Globe className="w-4 h-4 text-brand" />
            </div>
            <div className="min-w-0 flex-1">
              <h1 className="text-lg font-bold text-foreground leading-tight">在线报价</h1>
              <p className="text-[11px] text-muted-foreground line-clamp-2 break-words leading-snug">
                选择客户 → 扩展关键词 → 生成链接 → 客户选词 → 计算报价 → 审核发送 → 客户确认 → 签约收款
              </p>
            </div>
          </div>
        </div>

        <div className="px-3 pb-20 pt-3 sm:px-4 md:px-5 md:pb-6">
          <div>
            {/* 报价设置栏在**这里没有 DOM**:它被 portal 到了 PricingCenter 的固定槽位
                (见下方 pricingControlBar 与 §R3 注释)。留这条注释是防止有人看不到挂载点
                又在这里补一个 —— 页面上会出现两套报价设置入口。 */}

            {isMobile && (
              <MobileSessionSwitcher
                sessions={sessions}
                selected={selectedToken || undefined}
                isNew={isNew}
                onSelect={handleSelect}
                onNew={handleNewQuote}
              />
            )}

            <PipelineStepper currentStep={currentStep} />

            {/* 客户链接常驻栏 — 所有步骤可见 */}
            {!isNew && selectedSession && <CustomerLinkBar token={selectedSession.token} />}

            {(isNew || selectedSession) && <StepGuide currentStep={currentStep} />}

            {isNew && <Step1Generate onSessionCreated={handleSessionCreated} />}

            {!isNew && selectedSession && currentStep === 'customer_select' && (
              <Step2CustomerSelect session={selectedSession} sessionData={sessionData} onRefresh={handleRefresh} />
            )}
            {!isNew && selectedSession && currentStep === 'calculate' && (
              <Step3Calculate session={selectedSession} onDone={handleRefresh} />
            )}
            {!isNew && selectedSession && currentStep === 'review' && (
              <Step4Review session={selectedSession} pricingData={pricingData} clustersData={sessionData?.clusters_data} onApprove={handleRefresh} onRefreshSession={handleRefresh} coefficientBusy={coefficientBusy} />
            )}
            {!isNew && selectedSession && currentStep === 'customer_confirm' && (
              <Step5CustomerConfirm session={selectedSession} sessionData={sessionData} onRefresh={handleRefresh} />
            )}
            {!isNew && selectedSession && currentStep === 'payment' && (
              <Step6Payment session={selectedSession} sessionData={sessionData} onRefresh={handleRefresh} />
            )}

            {!isNew && !selectedSession && (
              <div className="text-center text-muted-foreground py-20 px-4">
                <Globe className="w-12 h-12 text-muted-foreground/30 mx-auto mb-3" />
                <FeatureTooltip
                  featureId="sandbox_quote_new_btn"
                  stepId="first_quote"
                  title="点这里给一路顺风出行新建一份报价"
                  content="教程模式 · 客户操作会自动模拟 · 你跟着代理这边的步骤走就行"
                  side="bottom"
                  disabled={!isSandboxActive()}
                >
                  <Button
                    onClick={handleNewQuote}
                    className="mb-3 gap-2"
                  >
                    <Plus className="w-4 h-4" /> 新建报价
                  </Button>
                </FeatureTooltip>
                <div className="flex items-center justify-center gap-3 mt-2">
                  {sessions.length > 0 && (
                    isMobile ? (
                      <p className="text-sm text-muted-foreground/60">可在上方切换历史报价</p>
                    ) : (
                      <p className="text-sm text-muted-foreground/60">或从左侧选择历史报价记录</p>
                    )
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
    </PricingPrivacyProvider>
  );
}
