import { type ReactNode, useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  BarChart3,
  CheckCircle2,
  ClipboardCheck,
  Clock3,
  Database,
  FileText,
  Layers3,
  LineChart,
  PlayCircle,
  RadioTower,
  RefreshCw,
  RotateCw,
  ShieldCheck,
  Sparkles,
  Target,
  Users,
  X,
  XCircle,
} from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Textarea } from '@/components/ui/textarea';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { authFetch } from '@/lib/api';
import { GEO_INDUSTRY_OPTIONS_WITH_GENERAL, geoIndustryLabel } from '@/lib/geoIndustries';
import { cn } from '@/lib/utils';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
// [W7] 飞轮后台整页重造:4 tab 壳(飞轮全景 / 反哺成果 / 数据健康 / 高级)。
// 「高级」= 未改动的旧控制台 AdvancedFlywheelConsole,其余 3 tab 为新组件。
import { PanoramaTab } from '@/components/flywheel/PanoramaTab';
import { FeedbackTab } from '@/components/flywheel/FeedbackTab';
import { DataHealthTab } from '@/components/flywheel/DataHealthTab';
// [E1] 行业媒体有效性榜(含逐行溯源点开)· admin-only · additive 新 tab。
import { MediaEffectivenessBoard } from '@/components/flywheel/MediaEffectivenessBoard';
import { invalidateFlywheelCache } from '@/components/flywheel/flywheelApi';

const API_BASE = '/api/admin/geo-placement-flywheel';

type Coverage = {
  entities: number;
  inventory_mappings: number;
  score_snapshots: number;
  by_reference_status?: Record<string, number>;
  // [B1-5] 按实体去重的同分母口径(候选总数/可投放/仅参考可相加)。后端 get_media_flywheel_coverage 新增。
  scored_entities?: number;
  purchasable_entities?: number;
  by_reference_status_distinct?: Record<string, number>;
};

// [B3-1] 引擎权重候选(Stage 7 按行业引用份额生成 → 人工审核 → UPSERT geo_engine_weights)
type EngineWeightCandidate = {
  id: number;
  industry: string;
  engine: string;
  current_weight?: number | null;
  suggested_weight: number;
  evidence?: Record<string, unknown> | null;
  status: string;
  created_at?: string;
};

type SourceSignal = {
  domain?: string;
  industry_key?: string;
  balanced_weight?: number;
  answer_adopted_count?: number;
  cited_count?: number;
  // [T3] 明确引用展示口径 = 答案采纳 + 明确引用(后端 explicit_cited_count);cited_count 仅明确引用,留作打分,不作展示。
  explicit_cited_count?: number;
  search_only_count?: number;
  reference_only_count?: number;
  engine_count?: number;
  prompt_count?: number;
  last_seen_at?: string;
};

type AnswerMetricSummary = {
  total_rows?: number;
  unique_sources?: number;
  engine_count?: number;
  prompt_count?: number;
  answer_adopted_rows?: number;
  explicit_cited_rows?: number;
  search_exposed_rows?: number;
  reference_only_rows?: number;
  rejected_noise_rows?: number;
  total_credit?: number;
  answer_adoption_rate?: number;
  explicit_citation_rate?: number;
  search_exposure_rate?: number;
  last_updated_at?: string;
};

type AnswerMetricRow = {
  engine?: string;
  domain?: string;
  source_url?: string;
  total_rows?: number;
  unique_sources?: number;
  prompt_count?: number;
  engine_count?: number;
  answer_adopted_rows?: number;
  explicit_cited_rows?: number;
  search_exposed_rows?: number;
  total_credit?: number;
  last_updated_at?: string;
};

type AnswerMetricResponse = {
  status: string;
  summary?: AnswerMetricSummary;
  by_engine?: AnswerMetricRow[];
  top_sources?: AnswerMetricRow[];
  shadow_only?: boolean;
  production_takeover?: boolean;
};

type MediaEntity = {
  entity_key?: string;
  canonical_name?: string;
  domain?: string;
  industry_key?: string;
  shadow_score?: number;
  evidence_score?: number;
  inventory_score?: number;
  outcome_score?: number;
  reference_status?: string;
  is_purchasable?: boolean;
  reasons?: string[];
  evidence?: Record<string, unknown>;
};

type MediaBindingCandidate = {
  id?: number;
  entity_key?: string;
  industry_key?: string;
  media_source?: string;
  inventory_id?: number;
  media_name?: string;
  inventory_url?: string;
  match_method?: string;
  match_confidence?: number;
  can_approve?: boolean;
  status?: string;
  risk_flags?: string[];
  evidence?: Record<string, unknown>;
  review_note?: string;
  updated_at?: string;
  // [P0-1 2026-08-15] 后端对待审候选实时重算后附带的拦截原因(空 = 实时判定可通过)。
  // 与 can_approve / risk_flags 一样来自 evaluate_candidate_live,不是另一份判据。
  live_block_reason?: string;
};

// [P0-2 2026-08-15] 批量审核的失败项。后端一直返回 failed[],前端过去只取 length、
// 把原因整个丢掉 —— 于是「已通过 0 条,4 条未成功」说不出所以然,只能去生产查库才知道
// 是供应商下架。media_name 是后端补的:一键通过是跨行业全库选集,前端列表只有 300 条,
// 靠 candidate_id 映射不回名字。
type BindingReviewFailure = {
  candidate_id?: number;
  error?: string;
  media_name?: string;
  entity_key?: string;
  industry_key?: string;
  media_source?: string;
};

type TakeoverGate = {
  ready?: boolean;
  status_label?: string;
  blockers?: string[];
  next_action?: string;
  approved_binding_count?: number;
  whitelist?: Record<string, unknown>;
  metrics?: Record<string, number>;
  production_takeover?: boolean;
};

type MediaTakeoverPolicy = {
  id?: number;
  industry_key?: string;
  status?: string;
  whitelist?: Record<string, unknown>;
  gate_summary?: TakeoverGate;
  review_note?: string;
  reviewed_at?: string;
  disabled_at?: string;
};

type TakeoverGateResponse = {
  status: string;
  gate?: TakeoverGate;
  policy?: MediaTakeoverPolicy | null;
  approved_bindings?: MediaBindingCandidate[];
  production_takeover?: boolean;
};

type StrategyVersion = {
  id: number;
  industry_key?: string;
  strategy_version?: string;
  status?: string;
  style_family?: string;
  guidance?: string;
  evidence_score?: number;
  outcome_score?: number;
  confidence?: number;
  reviewed_at?: string;
  activated_at?: string;
  activated_by?: number;
  archived_at?: string;
  review_note?: string;
  source_summary?: Record<string, number>;
};

type StrategyAuditEvent = {
  id: number;
  strategy_id?: number;
  industry_key?: string;
  action?: string;
  actor_id?: number;
  from_status?: string;
  to_status?: string;
  note?: string;
  created_at?: string;
};

type ApiListResponse<T> = {
  status: string;
  items?: T[];
  preview?: T[];
  coverage?: Coverage;
  item?: T | null;
  candidate?: StrategyVersion;
  strategy_row?: StrategyVersion | null;
  input_counts?: Record<string, number>;
  style_distribution?: Record<string, number>;
  dry_run?: boolean;
  loaded?: number;
  loaded_entities?: number;
  candidate_count?: number;
  sampled?: number;
  written?: number;
  skipped?: number;
  would_write?: number;
  cap_hit?: boolean;
};

type ArticleStructureGroup = {
  count?: number;
  domain_count?: number;
  feature_share?: Record<string, number>;
  examples?: Array<{ title?: string; domain?: string; url?: string; source_weight?: number }>;
};

type ArticleStructureLift = {
  feature?: string;
  label?: string;
  adopted_share?: number;
  control_share?: number;
  lift?: number;
  recommended?: boolean;
};

type ArticleStructureResponse = ApiListResponse<unknown> & {
  sample_status?: string;
  // [飞轮收尾包④ §3H-2 · 2026-08-01] 空态字段:区分「真没有」和「被门滤掉了」。
  // 这两种情况在旧接口里长得一模一样(都是空 groups),处置却完全相反 ——
  // 前者要等数据,后者要如实告诉用户"现在看的是降一级的样本"。
  effective_corpus_grade?: string;
  corpus_grade_degraded?: boolean;
  corpus_grade_notice?: string;
  signal_measured_rows?: number;
  signal_unmeasured_rows?: number;
  groups?: {
    adopted_group?: ArticleStructureGroup;
    cited_group?: ArticleStructureGroup;
    search_only_control_group?: ArticleStructureGroup;
    reference_group?: ArticleStructureGroup;
  };
  feature_lift?: ArticleStructureLift[];
  recommended_structure_rules?: string[];
  // V4 · 行业化 LLM 建议(GET 恒只返缓存/ null;POST llm_rules:true 才生成)。
  recommended_structure_rules_llm?: string[] | null;
  warnings?: string[];
};

type DataHealth = {
  level: 'green' | 'yellow' | 'red';
  label: string;
  summary: string;
  reasons?: string[];
  next_action?: string;
  can_preview?: boolean;
  can_save_shadow?: boolean;
  can_activate?: boolean;
  metrics?: Record<string, number | boolean>;
  impact_scope?: Record<string, boolean>;
};

type HealthResponse = {
  status: string;
  health?: DataHealth;
};

type FlywheelObservability = {
  mode?: 'off' | 'admin_shadow' | 'advisory';
  latest_round_id?: string | null;
  latest_round_status?: string;
  latest_round_finished_at?: string | null;
  raw_rows?: number;
  stats_rows?: number;
  latest_stats_at?: string | null;
  latest_stats_age_hours?: number | null;
  candidate_count?: number;
  failed_resumable_count?: number;
  total_cost_yuan?: number;
  candidates_available?: boolean;
  shadow_only?: boolean;
  production_takeover?: boolean;
};

type FlywheelAdvisoryCandidate = {
  industry?: string;
  engine?: string;
  platform?: string;
  citation_rate?: number;
  citation_count?: number;
  total_queries?: number;
  avg_position?: number;
  sample_queries?: string[];
  suggestion?: string;
  source?: {
    type?: string;
    round_id?: string | null;
    last_updated?: string | null;
    industry?: string;
  };
};

type FlywheelAdvisoryResponse = {
  status: string;
  mode?: 'off' | 'admin_shadow' | 'advisory';
  items?: FlywheelAdvisoryCandidate[];
  observability?: FlywheelObservability;
};

type MediaFilter = 'all' | 'purchasable' | 'reference' | 'highEvidence' | 'lowQuality' | 'sharedHost';

async function parseError(res: Response) {
  try {
    const data = await res.json();
    const detail = (data?.detail_contract ?? data?.detail) || data?.message;
    if (typeof detail === 'string') return detail;
    if (detail?.message) return String(detail.message);
    if (detail?.code === 'health_gate_blocked') return '数据健康未通过，不能启用或回滚策略';
    return `请求失败 ${res.status}`;
  } catch {
    return `请求失败 ${res.status}`;
  }
}

async function apiGet<T>(path: string): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`, {
    method: 'POST',
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

async function apiPut<T>(path: string, body?: unknown): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`, {
    method: 'PUT',
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

function n(value: unknown, digits = 1) {
  const num = Number(value || 0);
  if (!Number.isFinite(num)) return '0';
  return num.toFixed(digits).replace(/\.0$/, '');
}

function pct(value: unknown, digits = 1) {
  const num = Number(value || 0);
  if (!Number.isFinite(num)) return '0%';
  return `${(num * 100).toFixed(digits).replace(/\.0$/, '')}%`;
}

function fmtDate(value?: string) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { hour12: false });
}

const statusLabel: Record<string, string> = {
  shadow: '待审核数据',
  approved: '已审核',
  active: '当前使用',
  rejected: '已驳回',
  archived: '已归档',
};

const advisoryModeLabel: Record<string, string> = {
  off: '关闭',
  admin_shadow: '只给运营看（不影响业务）',
  advisory: '允许当参考建议',
};

const advisoryModeHint: Record<string, string> = {
  off: '只显示健康摘要，不给顾问建议。',
  admin_shadow: '后台可看候选建议，不影响写作、投放或计费。',
  advisory: '顾问可以参考候选建议，仍不自动接管业务。',
};

const auditActionLabel: Record<string, string> = {
  approve: '审核通过',
  reject: '审核驳回',
  activate: '启用策略',
  rollback: '回滚策略',
};

const bindingStatusLabel: Record<string, string> = {
  candidate: '待审核',
  approved: '已通过',
  rejected: '已驳回',
};

const bindingMethodLabel: Record<string, string> = {
  domain_exact: '域名匹配',
  name_alias: '名称匹配',
};

function bindingStatus(candidate: MediaBindingCandidate) {
  return candidate.status || 'candidate';
}

function hostnameFromUrl(value?: string) {
  if (!value) return '';
  try {
    return new URL(value).hostname.replace(/^www\./, '');
  } catch {
    return '';
  }
}

function bindingVisibleName(candidate: MediaBindingCandidate) {
  const name = (candidate.media_name || '').trim();
  if (name) return name;
  const host = hostnameFromUrl(candidate.inventory_url);
  return host || '待核对媒体资源';
}

function bindingEntityLabel(candidate: MediaBindingCandidate) {
  const industryName = geoIndustryLabel(candidate.industry_key || 'general');
  return industryName ? `${industryName}来源线索` : '飞轮来源线索';
}

function bindingSourceLabel(candidate: MediaBindingCandidate) {
  return candidate.media_source === 'mhz_wemedia' ? '自媒体库存' : '媒体库存';
}

function isRecommendedBinding(candidate: MediaBindingCandidate) {
  return bindingStatus(candidate) === 'candidate'
    && !!candidate.can_approve
    && Number(candidate.match_confidence || 0) >= 0.9
    && !(candidate.risk_flags || []).length;
}

function bindingReviewTone(candidate: MediaBindingCandidate) {
  const status = bindingStatus(candidate);
  if (status === 'approved') {
    return {
      label: '已通过',
      description: '已写入飞轮审核数据。',
      className: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-100',
    };
  }
  if (status === 'rejected') {
    return {
      label: '已驳回',
      description: '不会进入媒体建议。',
      className: 'border-rose-500/30 bg-rose-500/10 text-rose-100',
    };
  }
  if (isRecommendedBinding(candidate)) {
    return {
      label: '建议通过',
      description: '系统匹配度高，人工核对名称和行业即可。',
      className: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-100',
    };
  }
  if (!candidate.can_approve) {
    return {
      label: '证据不足',
      description: '不建议直接通过，先补充来源或换候选。',
      className: 'border-amber-500/30 bg-amber-500/10 text-amber-100',
    };
  }
  return {
    label: '需人工核对',
    description: '请确认账号主体、价格和行业是否匹配。',
    className: 'border-sky-500/30 bg-sky-500/10 text-sky-100',
  };
}

function bindingRiskSummary(candidate: MediaBindingCandidate) {
  const risks = (candidate.risk_flags || []).filter(Boolean);
  if (risks.length) return risks.join('；');
  if (!candidate.can_approve) return '证据不足，建议继续核对后再通过。';
  return '暂无明显风险，仍需人工确认账号主体、价格和行业适配度。';
}

const takeoverPolicyStatusLabel: Record<string, string> = {
  ready_shadow: '准备完成',
  draft: '草稿',
  disabled: '已停用',
  archived: '历史记录',
};

const styleLabel: Record<string, string> = {
  ranking: '榜单推荐',
  comparison: '对比决策',
  guide: '攻略指南',
  faq: '问答覆盖',
  case: '案例背书',
  data_report: '数据报告',
};

function summarizeJobResult(data?: ApiListResponse<unknown> | null) {
  if (!data) return '';
  const loaded = Number(data.loaded ?? 0);
  const written = Number(data.written ?? 0);
  // [review fix] 只有备货类任务(绑定候选/实体回填)响应里才有 preview/items 预览列表;
  // 分析类响应(文章结构研究)没有该数组,之前恒显示「展示 0 条」误导为数据丢失——
  // 实际分析结果完整渲染在下方卡片,此处改为不数不存在的列表。
  const previewList = (data.preview || data.items) as unknown[] | undefined;
  if (data.dry_run) {
    return previewList
      ? `预览完成 · 读取 ${loaded} 条 · 展示 ${previewList.length} 条`
      : `预览完成 · 读取 ${loaded} 条 · 分析结果已在下方展示`;
  }
  if (written === 0) {
    return `保存 0 条 · 读取 ${loaded} 条 · 请先检查行业、轮次或源数据过滤`;
  }
  return `已保存 ${written} 条 · 读取 ${loaded} 条`;
}

function healthTone(level?: string): 'default' | 'green' | 'amber' {
  if (level === 'green') return 'green';
  if (level === 'yellow' || level === 'red') return 'amber';
  return 'default';
}

// [平台域口径 2026-08-15] 与后端 services 层绑定候选模块里的同名常量 SHARED_PLATFORM_DOMAINS
// **逐字同源**(后端是 SSOT;`tests/platform_domain_match_2026_08_15/test_frontend_list_parity.py`
// 解析本数组做漂移锁,两边不一致直接转红)。
// ⚠️ 这里刻意不写后端文件全路径:`tests/test_media_binding_api_static.py` 有一条
//    「前端文件里不许出现工程术语」的静态锁,那个路径里的词会被它扫到(我已经踩过一次)。
// 修前这里只有 6 项、且与后端口径不一致 —— 后端当时是 host 级精确相等,前端是后缀匹配,
// 于是同一个域在「共享平台」筛选里和风险标里结论相反。
// 元素一律是**注册域**;这里用后缀匹配,对注册域名单而言与后端「折注册域后精确相等」等价
// (`notcsdn.net` 两边都判 False)。
const SHARED_PLATFORM_DOMAINS = [
  '163.com',
  '360kuai.com',
  '52hrtt.com',
  'autohome.com.cn',
  'baidu.com',
  'bilibili.com',
  'chooseauto.com.cn',
  'csdn.net',
  'ctrip.com',
  'digitaling.com',
  'dongchedi.com',
  'douban.com',
  'douyin.com',
  'eastmoney.com',
  'iesdouyin.com',
  'ifeng.com',
  'jianshu.com',
  'mafengwo.cn',
  'meipian.cn',
  'pcauto.com.cn',
  'qctt.cn',
  'qq.com',
  'sina.cn',
  'sina.com.cn',
  'smzdm.com',
  'sohu.com',
  'taobao.com',
  'toutiao.com',
  'weibo.cn',
  'weibo.com',
  'xcar.com.cn',
  'xueqiu.com',
  'yoojia.com',
  'zcool.com.cn',
  'zhihu.com',
];

function isSharedPlatformDomain(domain?: string) {
  const d = (domain || '').toLowerCase();
  return SHARED_PLATFORM_DOMAINS.some((host) => d === host || d.endsWith(`.${host}`));
}

function mediaStatusLabel(item: MediaEntity) {
  if (item.is_purchasable) return '可投放';
  if (String(item.reference_status || '').includes('reject')) return '低质量';
  return '仅作参考';
}

function mediaStatusTone(item: MediaEntity): 'default' | 'secondary' {
  return item.is_purchasable ? 'default' : 'secondary';
}

function mediaSystemJudgement(item: MediaEntity) {
  const reasons = (item.reasons || []).filter(Boolean);
  if (reasons.length) return reasons.slice(0, 2).join('；');
  if (item.is_purchasable) return '已匹配可投放资源，仍需人工核对价格和适配度。';
  return '暂无可投放资源，只作为写作参考或媒体拓展线索。';
}

function canUseForWriting(item: MediaEntity) {
  return Number(item.evidence_score || 0) > 0 && !String(item.reference_status || '').includes('reject');
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function numberFrom(...values: unknown[]) {
  for (const value of values) {
    const num = Number(value || 0);
    if (Number.isFinite(num) && num !== 0) return num;
  }
  return 0;
}

function mediaEvidenceSummary(item: MediaEntity) {
  const evidence = asRecord(item.evidence);
  const citationRollup = asRecord(evidence.citation_rollup);
  return {
    engines: numberFrom(citationRollup.engine_count, evidence.engine_count),
    prompts: numberFrom(citationRollup.prompt_count, evidence.prompt_count),
    answerAdopted: numberFrom(citationRollup.answer_adopted_count, evidence.answer_adopted_count),
    cited: numberFrom(citationRollup.cited_count, evidence.cited_count),
    searchOnly: numberFrom(citationRollup.search_only_count, evidence.search_only_count),
    referenceOnly: numberFrom(citationRollup.reference_only_count, evidence.reference_only_count),
    inventoryMatches: numberFrom(evidence.inventory_match_count, evidence.inventory_matches),
    purchasableMatches: numberFrom(evidence.purchasable_match_count, evidence.purchasable_matches),
  };
}

function mediaRiskNotes(item: MediaEntity) {
  const risks: string[] = [];
  if (isSharedPlatformDomain(item.domain)) {
    risks.push('这是共享平台域名，里面可能有很多账号或作者，不能直接等同于一个可投放媒体。');
  }
  if (!item.is_purchasable) {
    risks.push('还没有匹配到可采购媒体资源，不能直接安排投放。');
  }
  if (String(item.reference_status || '').includes('reject')) {
    risks.push('系统判断质量较低，暂不建议进入写作策略。');
  }
  if (Number(item.evidence_score || 0) < 30) {
    risks.push('引用证据较弱，建议等待更多调研轮次验证。');
  }
  return risks.length ? risks : ['暂无明显风险，但启用前仍需人工核对行业适配度。'];
}

function mediaNextAction(item: MediaEntity) {
  if (item.is_purchasable) {
    return '可进入人工核对：确认媒体资源、价格、账号主体和客户行业是否匹配。';
  }
  if (canUseForWriting(item)) {
    return '可先作为文章写作参考，提炼其标题结构、表达方式和信息组织方法；投放前继续寻找可采购资源。';
  }
  return '先不用于写作和投放，建议继续补充调研数据或查看同类更高证据来源。';
}

function parseWhitelistText(value: string) {
  const items = value
    .split(/[\n,，;；\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  return { customer_ids: items };
}

function whitelistToText(value: unknown) {
  const record = asRecord(value);
  const all = [
    ...(Array.isArray(record.customer_ids) ? record.customer_ids : []),
    ...(Array.isArray(record.brand_ids) ? record.brand_ids : []),
    ...(Array.isArray(record.agent_ids) ? record.agent_ids : []),
  ];
  return all.map(String).join('\n');
}

const mediaFilterOptions: Array<{ key: MediaFilter; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'purchasable', label: '可投放' },
  { key: 'reference', label: '仅作参考' },
  { key: 'highEvidence', label: '高证据' },
  { key: 'lowQuality', label: '低质量' },
  { key: 'sharedHost', label: '共享平台域名' },
];

function DetailMetric({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="rounded-md border bg-background/60 p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-2 text-lg font-semibold">{value}</p>
      {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function MediaDetailDrawer({ item, onClose }: { item: MediaEntity; onClose: () => void }) {
  const summary = mediaEvidenceSummary(item);
  const risks = mediaRiskNotes(item);
  const evidenceRows = [
    { label: '答案采纳', value: summary.answerAdopted, hint: 'AI 回答明确采用了这个来源。' },
    { label: '明确引用', value: summary.cited, hint: '回答中出现了明确来源标记或引用关系。' },
    { label: '搜索曝光', value: summary.searchOnly, hint: '只出现在搜索候选里，权重较低。' },
    { label: '原文参考', value: summary.referenceOnly, hint: '抓到正文，可作低权重参考。' },
    { label: '可投放资源', value: summary.purchasableMatches, hint: '已经通过媒体资源匹配的数量。' },
  ];

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/50" role="dialog" aria-modal="true">
      <button className="absolute inset-0 cursor-default" aria-label="关闭媒体/站点详情遮罩" onClick={onClose} />
      <aside className="relative z-10 flex h-full w-full max-w-2xl flex-col overflow-hidden border-l bg-background shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b p-5">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-xl font-semibold">媒体/站点详情</h2>
              <Badge variant={mediaStatusTone(item)}>{mediaStatusLabel(item)}</Badge>
            </div>
            <p className="mt-2 text-sm text-muted-foreground">
              {item.canonical_name || item.domain || '未命名媒体/站点'}
              {item.domain ? ` · ${item.domain}` : ''}
            </p>
          </div>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="关闭媒体/站点详情">
            <X className="h-4 w-4" />
          </Button>
        </div>

        <div className="space-y-5 overflow-y-auto p-5">
          <section className="rounded-lg border bg-card/60 p-4">
            <p className="text-sm font-medium">系统判断</p>
            <p className="mt-2 text-sm text-muted-foreground">{mediaSystemJudgement(item)}</p>
          </section>

          <section className="grid gap-3 sm:grid-cols-2">
            <DetailMetric label="综合建议分" value={n(item.shadow_score)} hint="综合证据、资源、反馈后的内部参考分。" />
            <DetailMetric label="引用证据分" value={n(item.evidence_score)} hint="答案采纳与明确引用越多，分数越高。" />
            <DetailMetric label="可投放资源分" value={n(item.inventory_score)} hint="是否能匹配到真实可采购媒体资源。" />
            <DetailMetric label="发布反馈分" value={n(item.outcome_score)} hint="后续发布效果回流后才会逐步变准。" />
          </section>

          <section className="rounded-lg border bg-card/60 p-4">
            <p className="text-sm font-medium">证据明细</p>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              {evidenceRows.map((row) => (
                <DetailMetric key={row.label} label={row.label} value={n(row.value, 0)} hint={row.hint} />
              ))}
            </div>
            <p className="mt-3 text-xs text-muted-foreground">
              覆盖 {n(summary.engines, 0)} 个引擎、{n(summary.prompts, 0)} 个问题；长来源列表已做公平折算。
            </p>
          </section>

          <section className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-lg border bg-card/60 p-4">
              <p className="text-sm font-medium">能否用于写作</p>
              <p className="mt-2 text-sm text-muted-foreground">
                {canUseForWriting(item) ? '可以作为写作参考，但需要结合客户资料和行业事实。' : '暂不建议作为写作依据。'}
              </p>
            </div>
            <div className="rounded-lg border bg-card/60 p-4">
              <p className="text-sm font-medium">能否投放</p>
              <p className="mt-2 text-sm text-muted-foreground">
                {item.is_purchasable ? '已匹配可投放资源，仍需人工核对。' : '暂未匹配可投放资源，只作参考。'}
              </p>
            </div>
          </section>

          <section className="rounded-lg border bg-card/60 p-4">
            <p className="text-sm font-medium">风险提示</p>
            <ul className="mt-3 space-y-2 text-sm text-muted-foreground">
              {risks.map((risk) => (
                <li key={risk}>- {risk}</li>
              ))}
            </ul>
          </section>

          <section className="rounded-lg border bg-card/60 p-4">
            <p className="text-sm font-medium">下一步建议</p>
            <p className="mt-2 text-sm text-muted-foreground">{mediaNextAction(item)}</p>
          </section>

          <details className="rounded-lg border bg-background/60">
            <summary className="cursor-pointer px-4 py-3 text-sm text-muted-foreground">
              技术明细（仅排查问题时查看）
            </summary>
            <pre className="max-h-60 overflow-auto border-t p-4 text-xs text-muted-foreground">
              {JSON.stringify({ evidence: item.evidence || {}, reasons: item.reasons || [] }, null, 2)}
            </pre>
          </details>
        </div>
      </aside>
    </div>
  );
}

function StatCard({ label, value, hint, tone = 'default' }: { label: string; value: string | number; hint: string; tone?: 'default' | 'green' | 'amber' }) {
  return (
    <div className={cn(
      'rounded-lg border bg-card/70 p-4',
      tone === 'green' && 'border-emerald-500/30 bg-emerald-500/5',
      tone === 'amber' && 'border-amber-500/30 bg-amber-500/5',
    )}>
      <p className="text-xs text-muted-foreground">{label}</p>
      <div className="mt-2 text-2xl font-semibold tracking-normal">{value}</div>
      <p className="mt-1 text-xs text-muted-foreground">{hint}</p>
    </div>
  );
}

function ReviewStepCard({
  index,
  title,
  description,
  status,
  tone = 'default',
}: {
  index: number;
  title: string;
  description: string;
  status: string;
  tone?: 'default' | 'green' | 'amber';
}) {
  return (
    <div className={cn(
      'rounded-lg border bg-card/60 p-3',
      tone === 'green' && 'border-emerald-500/30 bg-emerald-500/5',
      tone === 'amber' && 'border-amber-500/30 bg-amber-500/5',
    )}>
      <div className="flex items-center justify-between gap-3">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-background text-xs font-semibold">
          {index}
        </span>
        <Badge variant={tone === 'green' ? 'default' : 'secondary'} className="shrink-0">
          {status}
        </Badge>
      </div>
      <h3 className="mt-3 text-sm font-semibold">{title}</h3>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">{description}</p>
    </div>
  );
}

function ActionWithHint({
  children,
  hint,
  tone = 'default',
}: {
  children: ReactNode;
  hint: string;
  tone?: 'default' | 'green' | 'amber';
}) {
  return (
    <div className="flex min-w-[142px] flex-col gap-1">
      {children}
      <span className={cn(
        'text-[11px] leading-4 text-muted-foreground',
        tone === 'green' && 'text-emerald-300',
        tone === 'amber' && 'text-amber-300',
      )}>
        {hint}
      </span>
    </div>
  );
}

function JobMetric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-md border bg-background/50 p-3">
      <p className="text-[11px] text-muted-foreground">{label}</p>
      <p className="mt-1 text-lg font-semibold">{value}</p>
    </div>
  );
}

function clampPercent(value: unknown) {
  const num = Number(value || 0);
  if (!Number.isFinite(num)) return 0;
  return Math.max(0, Math.min(100, num));
}

function ProgressBar({ value, tone = 'emerald' }: { value: number; tone?: 'emerald' | 'cyan' | 'amber' | 'violet' }) {
  return (
    <div className="h-2 overflow-hidden rounded-full bg-slate-900/80">
      <div
        className={cn(
          'h-full rounded-full transition-all',
          tone === 'emerald' && 'bg-emerald-400',
          tone === 'cyan' && 'bg-cyan-400',
          tone === 'amber' && 'bg-amber-400',
          tone === 'violet' && 'bg-violet-400',
        )}
        style={{ width: `${clampPercent(value)}%` }}
      />
    </div>
  );
}

function FlywheelStageCard({
  icon,
  title,
  subtitle,
  value,
  progress,
  status,
  tone = 'emerald',
  onClick,
  actionLabel,
  connected,
}: {
  icon: ReactNode;
  title: string;
  subtitle: string;
  value: string | number;
  progress: number;
  status: string;
  tone?: 'emerald' | 'cyan' | 'amber' | 'violet';
  onClick?: () => void;
  actionLabel?: string;
  connected?: boolean;
}) {
  const clickable = typeof onClick === 'function';
  return (
    <div
      role={clickable ? 'button' : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={onClick}
      onKeyDown={clickable ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick?.(); } } : undefined}
      className={cn(
        'rounded-2xl border bg-card/70 p-4 shadow-sm',
        tone === 'emerald' && 'border-emerald-500/25',
        tone === 'cyan' && 'border-cyan-500/25',
        tone === 'amber' && 'border-amber-500/25',
        tone === 'violet' && 'border-violet-500/25',
        clickable && 'cursor-pointer transition hover:bg-card focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/60',
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className={cn(
          'flex h-10 w-10 items-center justify-center rounded-xl',
          tone === 'emerald' && 'bg-emerald-500/10 text-emerald-300',
          tone === 'cyan' && 'bg-cyan-500/10 text-cyan-300',
          tone === 'amber' && 'bg-amber-500/10 text-amber-300',
          tone === 'violet' && 'bg-violet-500/10 text-violet-300',
        )}>
          {icon}
        </div>
        <Badge variant="secondary" className="shrink-0">{status}</Badge>
      </div>
      <div className="mt-4">
        <p className="text-sm text-muted-foreground">{title}</p>
        <p className="mt-1 text-2xl font-semibold tracking-normal">{value}</p>
        <p className="mt-2 min-h-10 text-xs leading-5 text-muted-foreground">{subtitle}</p>
      </div>
      <div className="mt-4 space-y-2">
        <ProgressBar value={progress} tone={tone} />
        <div className="flex items-center justify-between">
          {connected === false ? (
            <p className="text-[11px] text-amber-300/80">待接通 · 未生成影子数据</p>
          ) : (
            <p className="text-[11px] text-muted-foreground">准备度 {Math.round(clampPercent(progress))}%</p>
          )}
          {clickable && (
            <span className="text-[11px] font-medium text-emerald-300">{actionLabel || '去处理 →'}</span>
          )}
        </div>
      </div>
    </div>
  );
}

function InsightRow({
  icon,
  title,
  detail,
  metric,
}: {
  icon: ReactNode;
  title: string;
  detail: string;
  metric?: string | number;
}) {
  return (
    <div className="flex gap-3 rounded-xl border bg-background/50 p-3">
      <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-300">
        {icon}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <p className="font-medium">{title}</p>
          {metric !== undefined && <Badge variant="outline">{metric}</Badge>}
        </div>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">{detail}</p>
      </div>
    </div>
  );
}

function ReviewQueueItem({
  label,
  value,
  hint,
  tone = 'default',
}: {
  label: string;
  value: string | number;
  hint: string;
  tone?: 'default' | 'amber' | 'green';
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl border bg-background/50 p-3">
      <div>
        <p className="text-sm font-medium">{label}</p>
        <p className="mt-1 text-xs text-muted-foreground">{hint}</p>
      </div>
      <Badge
        variant={tone === 'green' ? 'default' : 'secondary'}
        className={cn(tone === 'amber' && 'border-amber-500/40 bg-amber-500/10 text-amber-200')}
      >
        {value}
      </Badge>
    </div>
  );
}

function structureGroupCount(result: ArticleStructureResponse | null, key: 'adopted_group' | 'cited_group' | 'search_only_control_group' | 'reference_group') {
  return Number(result?.groups?.[key]?.count || 0);
}

function articleStructureLoaded(result: ArticleStructureResponse | null) {
  return Number(result?.loaded || 0);
}

function articleStructureReferenceCount(result: ArticleStructureResponse | null) {
  return structureGroupCount(result, 'search_only_control_group') + structureGroupCount(result, 'reference_group');
}

function articleStructureEmptyMessage(result: ArticleStructureResponse | null) {
  const loaded = articleStructureLoaded(result);
  if (!result) return '正在等待文章结构数据；可点击“预览文章结构”。';
  if (loaded > 0) return `已读取 ${loaded} 篇文章，暂未形成可推荐结构；可以继续积累样本。`;
  return '没有读到可分析的文章。请检查文章结构接口是否能读取正式文章表和调研文章缓存。';
}

// [W7] 旧「飞轮全面深度体验」控制台整体保留为「高级」tab(6 内部 tab + 27 区块一字不改)。
function AdvancedFlywheelConsole() {
  // askConfirm(不叫 confirm,避免遮蔽全局 window.confirm 触发 TS build fail):
  // 原生 window.confirm/prompt 在 iOS 上不弹窗直接返回 falsy → 审核按钮点了无反应。
  const [confirmDialog, askConfirm] = useConfirmDialog();
  // 总览卡片可点击跳转对应 tab(受控 Tabs);运营从"看到数字"直达"该做的事"。
  const [activeTab, setActiveTab] = useState('advisory');
  const [industry, setIndustry] = useState('general');
  const [operatorNote, setOperatorNote] = useState('');
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [health, setHealth] = useState<DataHealth | null>(null);
  // [防漂移] 调研数据 vs 飞轮影子层新鲜度;后端 /bridge/health 返回 stale + 滞后小时。fail-soft 失败即 null。
  const [bridgeHealth, setBridgeHealth] = useState<any>(null);
  const [sourceSignals, setSourceSignals] = useState<SourceSignal[]>([]);
  const [answerMetrics, setAnswerMetrics] = useState<AnswerMetricResponse | null>(null);
  const [mediaItems, setMediaItems] = useState<MediaEntity[]>([]);
  const [mediaBindingCandidates, setMediaBindingCandidates] = useState<MediaBindingCandidate[]>([]);
  const [takeoverGate, setTakeoverGate] = useState<TakeoverGateResponse | null>(null);
  const [takeoverWhitelistText, setTakeoverWhitelistText] = useState('');
  const [takeoverNote, setTakeoverNote] = useState('');
  const [advisoryObservability, setAdvisoryObservability] = useState<FlywheelObservability | null>(null);
  const [advisoryCandidates, setAdvisoryCandidates] = useState<FlywheelAdvisoryCandidate[]>([]);
  const [mediaFilter, setMediaFilter] = useState<MediaFilter>('all');
  const [selectedMediaKey, setSelectedMediaKey] = useState('');
  const [mediaDetailOpen, setMediaDetailOpen] = useState(false);
  const [bindingReviewNotes, setBindingReviewNotes] = useState<Record<number, string>>({});
  const [engineWeightCandidates, setEngineWeightCandidates] = useState<EngineWeightCandidate[]>([]);
  const [engineWeightNotes, setEngineWeightNotes] = useState<Record<number, string>>({});
  const [strategies, setStrategies] = useState<StrategyVersion[]>([]);
  const [activeStrategy, setActiveStrategy] = useState<StrategyVersion | null>(null);
  const [candidateInfo, setCandidateInfo] = useState<ApiListResponse<StrategyVersion> | null>(null);
  const [articleStructure, setArticleStructure] = useState<ArticleStructureResponse | null>(null);
  const [strategyAuditEvents, setStrategyAuditEvents] = useState<StrategyAuditEvent[]>([]);
  const [confirmAction, setConfirmAction] = useState<{ type: 'activate' | 'rollback'; strategy: StrategyVersion } | null>(null);
  const [confirmNote, setConfirmNote] = useState('');
  const [lastJob, setLastJob] = useState<ApiListResponse<unknown> | null>(null);
  const [busy, setBusy] = useState('');
  // [答案实体] 结果侧品牌实体雷达 · shadow-only · 与 media-entity(媒体实体)/answer-adoption(来源采纳)边界见交付说明
  const [answerEntitySummary, setAnswerEntitySummary] = useState<{
    entities: Array<{ entity_key: string; entity_name: string; mention_count: number; engine_count: number; engines: string[]; avg_recommendation_rank: number | null; avg_confidence: number }>;
    top_recommendation_reasons: Array<{ reason: string; count: number }>;
  } | null>(null);
  const [answerEntityHealth, setAnswerEntityHealth] = useState<{
    answer_text_coverage: number; extraction_coverage: number; entity_total: number;
    low_confidence_ratio: number; entity_source_linkable_ratio: number;
    last_updated: string | null; facts_last_7d: number; facts_last_30d: number;
  } | null>(null);
  const [answerEntityBusy, setAnswerEntityBusy] = useState('');
  // [T5] 预览(dry_run)返回的预计成本(pending 组 × 单组 LLM 估价),给运营/老板授权首跑参考。
  const [answerEntityCostEstimate, setAnswerEntityCostEstimate] = useState<{
    pending_groups?: number; estimated_cost_cny?: number; cost_per_call_cny?: number; human_note?: string; model?: string;
  } | null>(null);
  // [T6] 全库跨行业「建议通过」条数(一键通过按钮 N 来源);[T6b] 近 7 天自动通过明细。
  const [recommendedCount, setRecommendedCount] = useState<number | null>(null);
  const [autoApprovedRecent, setAutoApprovedRecent] = useState<{ count?: number; items?: unknown[] } | null>(null);
  // [P0-2 2026-08-15] 上一次批量审核的失败明细。之前只把 failed.length 塞进 toast 文案,
  // 原因随手丢掉 → 「4 条未成功」永远查不出是为什么。这里留在页面上,可折叠。
  const [bindingFailures, setBindingFailures] = useState<{
    scope: string; failures: BindingReviewFailure[]; approved: number;
  } | null>(null);
  const [bindingFailuresOpen, setBindingFailuresOpen] = useState(true);

  const queryIndustry = useMemo(() => encodeURIComponent(industry.trim()), [industry]);
  // [B3-2] 写作策略作用域跟随右上角行业选择器(general 仍是默认)。修前写死 'general' →
  // 策略生成/列表/审核/启用全链路只在 general 生效,行业策略无法生产。
  const queryWritingScope = useMemo(() => encodeURIComponent(industry.trim()), [industry]);
  const activationBlocked = health?.can_activate === false;

  const loadStrategyAudit = useCallback(async (strategyId: number) => {
    try {
      const data = await apiGet<ApiListResponse<StrategyAuditEvent>>(`/writing/strategy-versions/${strategyId}/audit?limit=20`);
      setStrategyAuditEvents(data.items || []);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作历史加载失败');
    }
  }, []);

  const loadAll = useCallback(async () => {
    setBusy('load');
    // [review fix · C4] 本控制台的写动作(审核绑定/引擎权重/批量通过/撤销)完成后都会 loadAll();这些写会改
    // 全景 pending 计数 / insight facts,而全景/反哺/看板 tab 走 apiGetCached。此处单一 choke 统一失效跨 tab
    // 共享缓存,否则切到全景 tab 60s 内仍显示旧数(点了没反应假象)。(自身数据下方走 apiGet 不缓存,不受影响。)
    invalidateFlywheelCache([
      '/flywheel-panorama', '/writing/flywheel-insight', '/writing/evolution-board', '/writing/outcome-summary',
    ]);
    try {
      const [coverageRes, healthRes, sourceRes, adoptionRes, mediaRes, bindingRes, takeoverRes, advisoryObsRes, advisoryCandidateRes, strategyRes, activeRes, articleStructureRes, engineWeightRes, bridgeHealthRes, recommendedCountRes, autoApprovedRecentRes] = await Promise.all([
        apiGet<ApiListResponse<Coverage>>('/media/coverage'),
        apiGet<HealthResponse>(`/health?industry=${queryIndustry}`),
        apiGet<ApiListResponse<SourceSignal>>(`/source-signals/rollup?industry=${queryIndustry}&limit=50`),
        apiGet<AnswerMetricResponse>(`/answer-adoption/summary?industry=${queryIndustry}&limit=20`),
        apiGet<ApiListResponse<MediaEntity>>(`/media/shadow-recommendations?industry=${queryIndustry}&limit=50`),
        apiGet<ApiListResponse<MediaBindingCandidate>>(`/media/binding-candidates?industry=${queryIndustry}&limit=100`),
        apiGet<TakeoverGateResponse>(`/media/takeover-gate?industry=${queryIndustry}`),
        apiGet<FlywheelObservability>('/advisory/observability'),
        apiGet<FlywheelAdvisoryResponse>(`/advisory/candidates?industry=${queryIndustry}&limit=20`),
        apiGet<ApiListResponse<StrategyVersion>>(`/writing/strategy-versions?industry_key=${queryWritingScope}&status=&limit=50`),
        apiGet<ApiListResponse<StrategyVersion>>(`/writing/strategy-active?industry_key=${queryWritingScope}`),
        apiGet<ArticleStructureResponse>(`/writing/article-structure/analyze?industry_key=${queryWritingScope}&limit=300&min_chars=500`),
        apiGet<ApiListResponse<EngineWeightCandidate>>('/engine-weight-candidates?status=candidate&limit=100'),
        apiGet<any>('/bridge/health').catch(() => null),
        apiGet<{ recommended_count?: number; min_confidence?: number }>('/media/binding-candidates/recommended-count').catch(() => null),
        apiGet<{ count?: number; items?: unknown[] }>('/media/binding-candidates/auto-approved-recent?days=7').catch(() => null),
      ]);
      setCoverage(coverageRes.coverage || null);
      setHealth(healthRes.health || null);
      setSourceSignals(sourceRes.items || []);
      setAnswerMetrics(adoptionRes || null);
      setMediaItems(mediaRes.items || []);
      setMediaBindingCandidates(bindingRes.items || []);
      setTakeoverGate(takeoverRes || null);
      setAdvisoryObservability(advisoryObsRes || null);
      setAdvisoryCandidates(advisoryCandidateRes.items || []);
      if (takeoverRes?.policy?.whitelist) {
        setTakeoverWhitelistText((prev) => prev || whitelistToText(takeoverRes.policy?.whitelist));
      }
      setStrategies(strategyRes.items || []);
      setActiveStrategy(activeRes.item || null);
      setArticleStructure(articleStructureRes || null);
      setEngineWeightCandidates(engineWeightRes.items || []);
      setBridgeHealth(bridgeHealthRes?.health ?? null);
      setRecommendedCount(recommendedCountRes?.recommended_count ?? null);
      setAutoApprovedRecent(autoApprovedRecentRes ?? null);
      if (activeRes.item?.id) {
        await loadStrategyAudit(activeRes.item.id);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '加载失败');
    } finally {
      setBusy('');
    }
  }, [loadStrategyAudit, queryIndustry, queryWritingScope]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const runJob = async (label: string, fn: () => Promise<ApiListResponse<unknown>>) => {
    setBusy(label);
    try {
      const data = await fn();
      setLastJob(data);
      const message = summarizeJobResult(data);
      const loaded = Number(data.loaded ?? 0);
      const written = Number(data.written ?? 0);
      if (loaded === 0 || (!data.dry_run && written === 0)) {
        toast.warning(message);
      } else {
        toast.success(message);
      }
      if (!data.dry_run) await loadAll();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    } finally {
      setBusy('');
    }
  };

  const loadAnswerEntities = useCallback(async () => {
    try {
      const [summary, healthResp] = await Promise.all([
        apiGet<typeof answerEntitySummary>(`/answer-entities/summary?industry=${encodeURIComponent(industry)}&limit=20`),
        apiGet<{ health: typeof answerEntityHealth }>(`/answer-entities/health`),
      ]);
      setAnswerEntitySummary(summary);
      setAnswerEntityHealth(healthResp?.health ?? null);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '加载答案实体失败');
    }
  }, [industry]);

  const runAnswerEntityJob = async (label: string, dryRun: boolean) => {
    setAnswerEntityBusy(label);
    try {
      const data = await apiPost<{
        status?: string; mode?: string; pending_extract?: number; facts_written?: number;
        cost_estimate?: { pending_groups?: number; estimated_cost_cny?: number; cost_per_call_cny?: number; human_note?: string; model?: string };
      }>(
        '/answer-entities/rebuild', { industry, limit: dryRun ? 500 : 200, dry_run: dryRun },
      );
      if (dryRun) {
        setAnswerEntityCostEstimate(data.cost_estimate ?? null);
        toast.success(`待抽取 ${data.pending_extract ?? 0} 条(预览不调 LLM)`);
      } else {
        toast.success(data.mode === 'background' ? '已受理后台抽取,稍后刷新查看' : '抽取完成');
      }
      await loadAnswerEntities();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    } finally {
      setAnswerEntityBusy('');
    }
  };

  useEffect(() => {
    if (activeTab === 'answer-entities') void loadAnswerEntities();
  }, [activeTab, loadAnswerEntities]);

  const updateAdvisoryMode = async (mode: 'off' | 'admin_shadow' | 'advisory') => {
    let confirm = '';
    if (mode === 'advisory') {
      const ok = await askConfirm({
        title: '启用「顾问可参考」模式',
        description: '开启后只开放后台建议，不会接管写作、投放或计费。确认启用吗？',
        confirmLabel: '确认启用',
        cancelLabel: '取消',
      });
      if (!ok) {
        toast.warning('已取消启用');
        return;
      }
      confirm = 'ENABLE_FLYWHEEL_ADVISORY';
    }
    setBusy('advisory-mode');
    try {
      await apiPut('/advisory-mode', { mode, confirm });
      toast.success(`飞轮顾问模式已设为：${advisoryModeLabel[mode]}`);
      await loadAll();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '模式保存失败');
    } finally {
      setBusy('');
    }
  };

  const generateStrategy = async (persist: boolean) => {
    setBusy(persist ? 'persist-strategy' : 'preview-strategy');
    try {
      const data = await apiPost<ApiListResponse<StrategyVersion>>('/writing/strategy-generate-from-data', {
        industry_key: industry,
        limit: 300,
        persist,
      });
      if (data.candidate && operatorNote.trim()) {
        data.candidate.review_note = operatorNote.trim();
      }
      setCandidateInfo(data);
      toast.success(persist ? '策略版本已保存，等待审核' : '策略候选已生成');
      if (persist) await loadAll();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '生成策略失败');
    } finally {
      setBusy('');
    }
  };

  const rebuildStyleFeatures = async (dryRun: boolean) => {
    await runJob(dryRun ? 'style-preview' : 'style-write', () => apiPost('/writing/style-features/rebuild', {
      industry_key: industry,
      limit: 300,
      min_chars: 500,
      dry_run: dryRun,
    }));
  };

  const analyzeArticleStructure = async () => {
    await runJob('article-structure-preview', async () => {
      const data = await apiPost<ArticleStructureResponse>('/writing/article-structure/analyze', {
        industry_key: industry,
        limit: 300,
        min_chars: 500,
        dry_run: true,
      });
      setArticleStructure(data);
      return data;
    });
  };

  // V4 · 手动生成行业化 LLM 结构建议(llm_rules:true 才调 LLM);成功后清缓存让其它 tab 读到新建议。
  const refreshLlmStructureRules = async () => {
    const ok = await askConfirm({
      title: '刷新行业化结构建议?',
      description: '将调用 AI 基于该行业的采纳样本生成结构化写作建议(平台承担 AI 成本),仅生成建议、不改线上写作规则。',
      confirmLabel: '生成建议',
    });
    if (!ok) return;
    setBusy('article-structure-llm');
    try {
      const data = await apiPost<ArticleStructureResponse>('/writing/article-structure/analyze', {
        industry_key: industry,
        limit: 300,
        min_chars: 500,
        llm_rules: true,
      });
      // [review fix · C5] 后端 run_rebuild_guarded 互斥:有分析在跑时返回 {status:'in_progress'}(无 feature_lift),
      // 不能 setArticleStructure 覆盖成空态,否则结构分析区被清空。提示稍后重试即可。
      if ((data as { status?: string })?.status === 'in_progress') {
        toast.info('结构分析正在进行中,请稍后再点「刷新行业化建议」');
        return;
      }
      setArticleStructure(data);
      invalidateFlywheelCache(['/writing/article-structure/analyze']);
      toast.success('已生成行业化结构建议');
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '生成行业化建议失败');
    } finally {
      setBusy('');
    }
  };

  const rebuildBindingCandidates = async (dryRun: boolean) => {
    await runJob(dryRun ? 'binding-preview' : 'binding-write', () => apiPost('/media/binding-candidates/rebuild', {
      industry,
      limit: 80,
      dry_run: dryRun,
    }));
  };

  const reviewBindingCandidate = async (candidate: MediaBindingCandidate, decision: 'approve' | 'reject') => {
    if (!candidate.id) return;
    const note = (bindingReviewNotes[candidate.id] || '').trim();
    if (!note) {
      toast.warning('请先填写审核备注');
      return;
    }
    if (decision === 'approve') {
      const ok = await askConfirm({
        title: '确认通过这个媒体绑定？',
        description: '本操作只写入飞轮审核数据，不会接管线上投放。',
        confirmLabel: '通过绑定',
        cancelLabel: '取消',
      });
      if (!ok) return;
    }
    setBusy(`binding-${decision}-${candidate.id}`);
    try {
      await apiPost(`/media/binding-candidates/${candidate.id}/review`, {
        decision,
        review_note: note,
      });
      toast.success(decision === 'approve' ? '媒体绑定已通过审核' : '媒体绑定已驳回');
      await loadAll();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '审核失败');
    } finally {
      setBusy('');
    }
  };

  // [B3-1] 引擎权重候选审核:approve → UPSERT geo_engine_weights(生效)· 需备注 + 二次确认
  const reviewEngineWeightCandidate = async (candidate: EngineWeightCandidate, decision: 'approve' | 'reject') => {
    if (!candidate.id) return;
    const note = (engineWeightNotes[candidate.id] || '').trim();
    if (note.length < 2) {
      toast.warning('请先填写审核备注');
      return;
    }
    if (decision === 'approve') {
      const ok = await askConfirm({
        title: '确认采用这个引擎权重？',
        description: '通过后会更新全局引擎权重（影响媒体推荐评分口径），不改报价、不扣费、不自动发布。',
        confirmLabel: '采用权重',
        cancelLabel: '取消',
      });
      if (!ok) return;
    }
    setBusy(`ewc-${decision}-${candidate.id}`);
    try {
      await apiPost(`/engine-weight-candidates/${candidate.id}/review`, {
        decision,
        review_note: note,
      });
      toast.success(decision === 'approve' ? '引擎权重已采用' : '引擎权重候选已驳回');
      await loadAll();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '审核失败');
    } finally {
      setBusy('');
    }
  };

  const previewTakeoverGate = async () => {
    setBusy('takeover-preview');
    try {
      const data = await apiPost<TakeoverGateResponse>('/media/takeover-gate/preview', {
        industry,
        whitelist: parseWhitelistText(takeoverWhitelistText),
      });
      setTakeoverGate(data);
      toast.success(data.gate?.ready ? '接管准备条件已满足，但仍不会接管线上' : '已完成接管条件预览');
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '预览失败');
    } finally {
      setBusy('');
    }
  };

  const saveTakeoverGate = async () => {
    const note = takeoverNote.trim();
    if (!note) {
      toast.warning('请填写保存接管准备的审核备注');
      return;
    }
    const ok = await askConfirm({
      title: '确认保存接管准备？',
      description: '本操作只保存审核记录，不会接管线上推荐。',
      confirmLabel: '保存',
      cancelLabel: '取消',
    });
    if (!ok) return;
    setBusy('takeover-save');
    try {
      const data = await apiPost<TakeoverGateResponse>('/media/takeover-gate/save', {
        industry,
        whitelist: parseWhitelistText(takeoverWhitelistText),
        review_note: note,
      });
      setTakeoverGate(data);
      toast.success('接管准备已保存，仍需老板单独授权才能接线');
      await loadAll();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '保存失败');
    } finally {
      setBusy('');
    }
  };

  const disableTakeoverGate = async () => {
    const note = takeoverNote.trim();
    if (!note) {
      toast.warning('请填写停用接管准备的备注');
      return;
    }
    setBusy('takeover-disable');
    try {
      const data = await apiPost<TakeoverGateResponse>('/media/takeover-gate/disable', {
        industry,
        review_note: note,
      });
      setTakeoverGate(data);
      toast.success('接管准备已停用');
      await loadAll();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '停用失败');
    } finally {
      setBusy('');
    }
  };

  const reviewStrategy = async (strategy: StrategyVersion, decision: 'approve' | 'reject') => {
    setBusy(`${decision}-${strategy.id}`);
    try {
      await apiPost(`/writing/strategy-versions/${strategy.id}/review`, {
        decision,
        note: operatorNote || (decision === 'approve' ? '管理员审核通过' : '管理员驳回，需补充证据'),
      });
      toast.success(decision === 'approve' ? '策略已审核通过' : '策略已驳回');
      await loadAll();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '审核失败');
    } finally {
      setBusy('');
    }
  };

  const openStrategyConfirm = async (strategy: StrategyVersion, type: 'activate' | 'rollback') => {
    setConfirmAction({ strategy, type });
    setConfirmNote(operatorNote.trim());
    await loadStrategyAudit(strategy.id);
  };

  const submitStrategyConfirm = async () => {
    if (!confirmAction) return;
    const note = confirmNote.trim();
    if (!note) {
      toast.warning('必须填写操作备注');
      return;
    }
    if (activationBlocked) {
      toast.error('数据健康未通过，不能启用或回滚策略');
      return;
    }
    const { strategy, type } = confirmAction;
    setBusy(`${type}-${strategy.id}`);
    const endpoint = type === 'activate'
      ? `/writing/strategy-versions/${strategy.id}/activate`
      : `/writing/strategy-versions/${strategy.id}/rollback`;
    try {
      await apiPost(endpoint, { note });
      toast.success(type === 'activate' ? '策略已启用为当前内部策略：后续该行业写作会把它作为结构参考，文章仍需正常审核后发布，投放/报价/扣费不受影响' : '已回滚到选中的历史策略');
      setConfirmAction(null);
      setConfirmNote('');
      await loadAll();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    } finally {
      setBusy('');
    }
  };

  const byStatus = coverage?.by_reference_status || {};
  // [B1-5] 口径统一:优先用按实体去重的同分母数字(scored_entities 为分母,
  // purchasable_entities + by_reference_status_distinct 各状态可相加);缺新字段时回退旧口径(向后兼容)。
  const byStatusDistinct = coverage?.by_reference_status_distinct;
  const scoredEntities = coverage?.scored_entities ?? coverage?.score_snapshots ?? 0;
  const purchasable = coverage?.purchasable_entities ?? byStatus.purchasable ?? 0;
  const referenceOnly = byStatusDistinct?.reference_only ?? byStatus.reference_only ?? 0;
  const totalEntities = coverage?.entities || 0;
  const healthMetrics = health?.metrics || {};
  const adoptionSummary = answerMetrics?.summary || {};
  const gate = takeoverGate?.gate || null;
  const takeoverPolicy = takeoverGate?.policy || null;
  const takeoverBlockers = gate?.blockers || [];
  const approvedBindingCount = Number(gate?.approved_binding_count ?? takeoverGate?.approved_bindings?.length ?? 0);
  const advisoryMode = advisoryObservability?.mode || 'off';
  const advisoryIsOpen = advisoryMode !== 'off';
  const latestStatsAge = advisoryObservability?.latest_stats_age_hours;
  const topAdvisoryCandidates = advisoryCandidates.slice(0, 3);
  const rawRows = Number(healthMetrics.raw_citation_rows || adoptionSummary.total_rows || advisoryObservability?.raw_rows || 0);
  const articleRows = Number(healthMetrics.article_body_count || 0);
  const adoptionRows = Number(adoptionSummary.answer_adopted_rows || healthMetrics.answer_adoption_count || 0);
  const strategyPendingCount = strategies.filter((item) => ['shadow', 'draft', 'pending', 'review_pending'].includes(String(item.status || ''))).length;
  const strategyApprovedCount = strategies.filter((item) => ['approved', 'active'].includes(String(item.status || ''))).length;
  const bindingPendingCount = mediaBindingCandidates.filter((item) => bindingStatus(item) === 'candidate').length;
  const dataProgress = health?.level === 'green' ? 100 : health?.level === 'yellow' ? 68 : rawRows > 0 ? 48 : 18;
  const mediaProgress = mediaBindingCandidates.length
    ? Math.round((approvedBindingCount / Math.max(mediaBindingCandidates.filter((item) => bindingStatus(item) !== 'deleted').length, 1)) * 100)
    : mediaItems.length ? 42 : 12;
  const strategyProgress = activeStrategy
    ? 100
    : strategyApprovedCount > 0 ? 78 : strategyPendingCount > 0 ? 54 : articleStructureLoaded(articleStructure) > 0 ? 34 : 10;
  const reviewProgress = gate?.ready ? 82 : approvedBindingCount > 0 || strategyPendingCount > 0 ? 55 : 22;
  const citationRateLabel = pct(adoptionSummary.explicit_citation_rate || 0);
  const adoptionRateLabel = pct(adoptionSummary.answer_adoption_rate || 0);
  const exposureRateLabel = pct(adoptionSummary.search_exposure_rate || 0);
  const topSource = sourceSignals[0];
  const topCandidate = topAdvisoryCandidates[0];
  const filteredMediaItems = useMemo(() => mediaItems.filter((item) => {
    if (mediaFilter === 'purchasable') return !!item.is_purchasable;
    if (mediaFilter === 'reference') return !item.is_purchasable;
    if (mediaFilter === 'highEvidence') return Number(item.evidence_score || 0) >= 70;
    if (mediaFilter === 'lowQuality') return Number(item.evidence_score || 0) < 40 || String(item.reference_status || '').includes('reject');
    if (mediaFilter === 'sharedHost') return isSharedPlatformDomain(item.domain);
    return true;
  }), [mediaFilter, mediaItems]);
  const selectedMedia = useMemo(
    () => mediaItems.find((item) => item.entity_key === selectedMediaKey) || filteredMediaItems[0] || null,
    [filteredMediaItems, mediaItems, selectedMediaKey],
  );
  const bindingReviewBuckets = useMemo(() => {
    const pending = mediaBindingCandidates.filter((item) => bindingStatus(item) === 'candidate');
    const recommended = pending.filter(isRecommendedBinding);
    const needsReview = pending.filter((item) => !isRecommendedBinding(item));
    const approved = mediaBindingCandidates.filter((item) => bindingStatus(item) === 'approved');
    const rejected = mediaBindingCandidates.filter((item) => bindingStatus(item) === 'rejected');
    return { pending, recommended, needsReview, approved, rejected };
  }, [mediaBindingCandidates]);

  // [P0-2 2026-08-15] 两个批量按钮的结果播报唯一出口。
  //   修前:`approveAllRecommended` 全失败也走 toast.success(绿勾) —— 「已通过 0 条,4 条未成功」
  //   配一个绿勾,读起来像成功;同文件另一个按钮却用 toast.warning,两个按钮口径还不一致。
  //   现在统一成三档,并且把失败明细落到页面上(toast 会自己消失,原因不能只活在 toast 里)。
  const reportBindingBatchResult = (
    scope: string, approved: number, failures: BindingReviewFailure[],
  ) => {
    setBindingFailures(failures.length ? { scope, failures, approved } : null);
    setBindingFailuresOpen(true);
    if (!failures.length) {
      toast.success(`已通过 ${approved} 条媒体绑定`);
      return;
    }
    if (approved === 0) {
      // 全失败 = 这次点击什么都没做成,不许显示成功样式
      toast.error(`${failures.length} 条全部未成功，一条也没通过 · 原因见下方「未成功明细」`);
      return;
    }
    toast.warning(`已通过 ${approved} 条，${failures.length} 条未成功 · 原因见下方「未成功明细」`);
  };

  // B 方案批量决策:AI 已分组(置信度≥90% 且无风险 = 建议通过),人工一键确认整批。
  // 仍是人工触发的审核动作;灰区(需核对组)保持逐条处理。
  const batchApproveRecommended = async () => {
    const targets = bindingReviewBuckets.recommended
      .map((c) => Number(c.id || 0))
      .filter(Boolean);
    if (!targets.length) {
      toast.info('当前没有可一键通过的高置信度候选');
      return;
    }
    const ok = await askConfirm({
      title: `一键通过 ${targets.length} 条高置信度媒体绑定？`,
      description: 'AI 判断这些候选的名称与域名都能对上（置信度≥90%，无风险提示）。只写入飞轮审核数据，不会接管线上投放；通过后仍可在审核记录里查到每一条。',
      confirmLabel: `通过这 ${targets.length} 条`,
      cancelLabel: '再看看',
    });
    if (!ok) return;
    setBusy('binding-batch');
    try {
      const data = await apiPost<{
        succeeded?: { candidate_id: number }[];
        failed?: BindingReviewFailure[];
      }>('/media/binding-candidates/batch-review', {
        candidate_ids: targets,
        decision: 'approve',
        review_note: 'AI 高置信度匹配（≥90%，无风险提示），批量人工确认通过',
      });
      const okCount = (data.succeeded || []).length;
      const failures = data.failed || [];
      reportBindingBatchResult('本行业一键通过', okCount, failures);
      await loadAll();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '批量审核失败');
    } finally {
      setBusy('');
    }
  };

  // [T6] 一键通过「全部行业」的 AI 高置信「建议通过」候选(服务端选集,跨行业)。
  // 只写后台审核数据,不改线上推荐/不扣费/不发布;灰区候选仍逐条人工。
  const approveAllRecommended = async () => {
    const total = recommendedCount ?? 0;
    if (!total) {
      toast.info('当前没有可一键通过的高置信度候选');
      return;
    }
    const ok = await askConfirm({
      title: `一键通过全部行业的 ${total} 条建议通过候选？`,
      description: `将通过全部 ${total} 条 AI 高置信候选(所有行业)。只影响后台数据完整性,不改线上推荐、不扣费、不发布。灰区候选(置信<90% 或有风险)仍需逐条人工。`,
      confirmLabel: `通过这 ${total} 条`,
      cancelLabel: '取消',
    });
    if (!ok) return;
    setBusy('binding-approve-all');
    try {
      const data = await apiPost<{
        approved_count?: number; failed_count?: number; selected?: number;
        failed?: BindingReviewFailure[];
      }>('/media/binding-candidates/approve-all-recommended', {});
      const approved = Number(data.approved_count || 0);
      reportBindingBatchResult('全部行业一键通过', approved, data.failed || []);
      await loadAll();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '批量审核失败');
    } finally {
      setBusy('');
    }
  };

  // [T6b] 撤销一条已通过绑定:删除对应投放映射,候选置回待审核;不影响接管授权。
  const revokeBinding = async (candidate: MediaBindingCandidate) => {
    if (!candidate.id) return;
    const ok = await askConfirm({
      title: '撤销这条绑定?',
      description: '撤销这条绑定?会删除对应投放映射,候选置回待审核。不影响接管授权。',
      confirmLabel: '撤销',
      cancelLabel: '取消',
    });
    if (!ok) return;
    setBusy(`binding-revoke-${candidate.id}`);
    try {
      await apiPost(`/media/binding-candidates/${candidate.id}/revoke`, { review_note: '管理员撤销' });
      toast.success('已撤销绑定,候选置回待审核');
      await loadAll();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '撤销失败');
    } finally {
      setBusy('');
    }
  };

  // "下一步"引导:告诉运营现在最该做的一件事,并给一个直达按钮。
  const nextStep = useMemo(() => {
    const rec = bindingReviewBuckets.recommended.length;
    const pending = bindingReviewBuckets.pending.length;
    if (rec > 0) {
      return {
        text: `AI 已核对好 ${rec} 条高置信度媒体绑定，名称与域名都能对上，可一键通过。`,
        action: 'batch' as const,
        button: `一键通过 ${rec} 条`,
      };
    }
    if (pending > 0) {
      return {
        text: `有 ${pending} 条候选需要人工核对（AI 置信度不足或有风险提示），请逐条判断"是不是同一个媒体、适不适合该行业"。`,
        action: 'media' as const,
        button: '去逐条核对',
      };
    }
    if (!mediaBindingCandidates.length) {
      return {
        text: '还没有媒体绑定候选。让 AI 先把调研发现的媒体和媒介库存自动匹配一遍，再回来审核。',
        action: 'media' as const,
        button: '去生成候选',
      };
    }
    if (strategyPendingCount > 0) {
      return {
        text: `媒体绑定已处理完，还有 ${strategyPendingCount} 条写作策略等待审核。`,
        action: 'writing' as const,
        button: '去审核策略',
      };
    }
    return {
      text: '当前没有待办。等下一轮调研产出新候选后，这里会提示你该做什么。',
      action: null,
      button: '',
    };
  }, [bindingReviewBuckets, mediaBindingCandidates.length, strategyPendingCount]);

  const renderBindingCandidateCard = (candidate: MediaBindingCandidate) => {
    const id = Number(candidate.id || 0);
    const status = bindingStatus(candidate);
    const tone = bindingReviewTone(candidate);
    const confidence = Number(candidate.match_confidence || 0) * 100;

    return (
      <div
        key={`${candidate.entity_key}-${candidate.media_source}-${candidate.inventory_id}`}
        className="rounded-2xl border bg-background/45 p-4"
      >
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="secondary" className={cn('border', tone.className)}>
                {tone.label}
              </Badge>
              <Badge variant="outline">{bindingSourceLabel(candidate)}</Badge>
              <Badge variant="outline">{bindingMethodLabel[candidate.match_method || ''] || '系统匹配'}</Badge>
            </div>
            <h3 className="mt-3 text-lg font-semibold">{bindingVisibleName(candidate)}</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              {bindingEntityLabel(candidate)} · 匹配置信度 {n(confidence)}%
            </p>
          </div>
          <div className="rounded-xl border bg-card/70 px-3 py-2 text-sm text-muted-foreground lg:max-w-xs">
            {tone.description}
          </div>
        </div>

        <div className="mt-4 grid gap-3 md:grid-cols-[1fr_1.1fr]">
          <div className="rounded-xl border bg-card/50 p-3">
            <p className="text-xs font-medium text-muted-foreground">AI 判断</p>
            <p className="mt-2 text-sm leading-6">{bindingRiskSummary(candidate)}</p>
            {candidate.inventory_url && (
              <a
                href={candidate.inventory_url}
                target="_blank"
                rel="noreferrer"
                className="mt-3 inline-flex text-xs text-emerald-300 hover:underline"
              >
                打开媒体资源页面
              </a>
            )}
          </div>
          <div>
            <Textarea
              value={bindingReviewNotes[id] || ''}
              onChange={(e) => setBindingReviewNotes((prev) => ({ ...prev, [id]: e.target.value }))}
              placeholder="审核备注：说明为什么通过或驳回"
              className="min-h-[92px]"
              disabled={!id || status !== 'candidate'}
            />
            <div className="mt-2 flex flex-wrap gap-2">
              <Button
                size="sm"
                onClick={() => reviewBindingCandidate(candidate, 'approve')}
                disabled={!id || !candidate.can_approve || status !== 'candidate' || !!busy}
              >
                通过绑定
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => reviewBindingCandidate(candidate, 'reject')}
                disabled={!id || status !== 'candidate' || !!busy}
              >
                驳回绑定
              </Button>
            </div>
            <p className="mt-2 text-[11px] leading-4 text-muted-foreground">
              通过后只写入飞轮审核数据，不会接管线上投放。
            </p>
          </div>
        </div>

        <details className="mt-3 rounded-xl border bg-slate-950/35">
          <summary className="cursor-pointer px-3 py-2 text-xs text-muted-foreground">
            技术明细（排查时查看）
          </summary>
          <div className="grid gap-2 border-t px-3 py-3 text-xs text-muted-foreground sm:grid-cols-2">
            <div>来源线索: {candidate.entity_key || '-'}</div>
            <div>库存编号: {candidate.inventory_id || '-'}</div>
            <div>库存表: {candidate.media_source || '-'}</div>
            <div>更新时间: {fmtDate(candidate.updated_at)}</div>
          </div>
          {candidate.evidence && (
            <pre className="max-h-48 overflow-auto border-t p-3 text-xs text-muted-foreground">
              {JSON.stringify(candidate.evidence, null, 2)}
            </pre>
          )}
        </details>
      </div>
    );
  };

  return (
    <div className="mx-auto w-full max-w-[1540px] space-y-6 p-4 sm:p-6">
      <div className="overflow-hidden rounded-3xl border bg-gradient-to-br from-slate-950 via-slate-950 to-emerald-950/35 p-5 shadow-sm sm:p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="max-w-3xl">
            <div className="flex items-center gap-3">
              <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-emerald-500/30 bg-emerald-500/10">
                <RadioTower className="h-6 w-6 text-emerald-300" />
              </div>
              <div>
                <p className="text-xs font-medium uppercase tracking-[0.28em] text-emerald-300">GEO Flywheel</p>
                <h1 className="mt-1 text-2xl font-bold tracking-normal sm:text-3xl">GEO 数据飞轮</h1>
              </div>
            </div>
            <p className="mt-4 text-sm leading-6 text-muted-foreground">
              把 AI 回答、引用来源、媒体候选和文章策略沉淀成内部顾问建议。所有启用必须人工审核；启用的写作策略会作为后续写作的结构参考，投放、报价、扣费不受影响，也不会自动发布客户可见内容。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <select
              aria-label="选择行业"
              value={industry}
              onChange={(e) => setIndustry(e.target.value)}
              className="h-10 w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 shadow-sm outline-none transition-colors hover:bg-slate-900 focus-visible:ring-2 focus-visible:ring-emerald-500 sm:w-56"
            >
              {GEO_INDUSTRY_OPTIONS_WITH_GENERAL.map((option) => (
                <option key={option.value} value={option.value} className="bg-slate-950 text-slate-100">
                  {option.label}
                </option>
              ))}
            </select>
            <Button variant="outline" onClick={loadAll} disabled={!!busy} className="rounded-xl">
              <RefreshCw className={cn('mr-2 h-4 w-4', busy === 'load' && 'animate-spin')} />
              刷新
            </Button>
            <p className="w-full text-[11px] text-muted-foreground">
              通用/全部行业 = 跨行业聚合口径;选择具体行业查看该行业明细。数据按具体行业沉淀。
            </p>
          </div>
        </div>

        <div className="mt-6 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <FlywheelStageCard
            icon={<Database className="h-5 w-5" />}
            title="数据采集"
            value={rawRows.toLocaleString()}
            subtitle={`已读取 ${articleRows.toLocaleString()} 篇正文，区分采纳、引用和曝光。`}
            progress={dataProgress}
            status={health?.label || '检查中'}
            tone="emerald"
            connected={rawRows > 0}
            onClick={() => setActiveTab('sources')}
            actionLabel="看证据来源 →"
          />
          <FlywheelStageCard
            icon={<Users className="h-5 w-5" />}
            title="媒体候选"
            value={scoredEntities.toLocaleString()}
            subtitle={`${purchasable.toLocaleString()} 个可投放，${referenceOnly.toLocaleString()} 个仅作参考（共 ${totalEntities.toLocaleString()} 个媒体实体）。`}
            progress={mediaProgress}
            status={approvedBindingCount > 0 ? '已有审核' : '待审核'}
            tone="cyan"
            connected={mediaItems.length > 0 || mediaBindingCandidates.length > 0}
            onClick={() => setActiveTab('media')}
            actionLabel="去看媒体建议 →"
          />
          <FlywheelStageCard
            icon={<FileText className="h-5 w-5" />}
            title="写作策略"
            value={strategies.length.toLocaleString()}
            subtitle={`待审 ${strategyPendingCount} 个，已审 ${strategyApprovedCount} 个，启用后作为后续写作的结构参考。`}
            progress={strategyProgress}
            status={activeStrategy ? '有当前策略' : '待审核'}
            tone="violet"
            connected={strategyPendingCount > 0 || strategyApprovedCount > 0 || !!activeStrategy}
            onClick={() => setActiveTab('writing')}
            actionLabel="去看内容策略 →"
          />
          <FlywheelStageCard
            icon={<ClipboardCheck className="h-5 w-5" />}
            title="人工审核"
            value={(bindingPendingCount + strategyPendingCount + takeoverBlockers.length).toLocaleString()}
            subtitle="上线前必须人工确认；不会自动发布、投放或改写线上规则。"
            progress={reviewProgress}
            status={gate?.ready ? '准备完成' : '需要复核'}
            tone="amber"
            connected={bindingPendingCount > 0 || strategyPendingCount > 0 || !!gate?.ready}
            onClick={() => setActiveTab('calibration')}
            actionLabel="去审核 →"
          />
        </div>
      </div>

      <div className="flex flex-col gap-3 rounded-2xl border border-emerald-500/35 bg-emerald-500/10 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-sm leading-6 text-emerald-100">
          <span className="font-semibold">下一步：</span>
          {nextStep.text}
        </p>
        {nextStep.action && (
          <Button
            size="sm"
            className="shrink-0"
            disabled={!!busy}
            onClick={() => {
              if (nextStep.action === 'batch') {
                void batchApproveRecommended();
              } else {
                setActiveTab(nextStep.action);
              }
            }}
          >
            {nextStep.button}
          </Button>
        )}
      </div>

      <div className="rounded-2xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm leading-6 text-amber-100">
        真实第一：飞轮只做内部顾问。启用的写作策略会作为后续写作的结构参考；当前页面不会改线上投放推荐、报价、扣费或客户可见内容，文章仍需正常审核后才发布。
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.4fr_0.9fr]">
        <div className="rounded-3xl border bg-card/70 p-4 sm:p-5">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <div className="flex items-center gap-2">
                <Sparkles className="h-4 w-4 text-emerald-300" />
                <h2 className="text-lg font-semibold">今天飞轮学到了什么</h2>
              </div>
              <p className="mt-1 text-sm text-muted-foreground">
                这里把后端数据翻译成人能判断的运营结论，而不是只堆接口字段。
              </p>
            </div>
            <Badge variant="secondary">行业：{geoIndustryLabel(industry)}</Badge>
          </div>
          <div className="mt-4 grid gap-3">
            <InsightRow
              icon={<LineChart className="h-4 w-4" />}
              title="AI 更看重能被明确引用的内容"
              detail={`当前真实采纳率 ${adoptionRateLabel}，明确引用率 ${citationRateLabel}，搜索曝光率 ${exposureRateLabel}。曝光只算低权重参考，不会被误判成采纳。`}
              metric={adoptionRateLabel}
            />
            <InsightRow
              icon={<Target className="h-4 w-4" />}
              title={topCandidate?.platform ? `${topCandidate.platform} 有稳定候选信号` : '等待下一轮调研产出建议'}
              detail={topCandidate?.suggestion || '飞轮会把多平台引用表现转成候选建议；没有足够样本时不会硬编结论。'}
              metric={topCandidate?.citation_rate ? pct(topCandidate.citation_rate) : undefined}
            />
            <InsightRow
              icon={<Layers3 className="h-4 w-4" />}
              title={topSource?.domain ? `高频来源：${topSource.domain}` : '来源证据仍在聚合'}
              detail={topSource?.domain
                ? `该来源覆盖 ${Number(topSource.engine_count || 0)} 个引擎、${Number(topSource.prompt_count || 0)} 个问题，可进入媒体或写作侧人工判断。`
                : '来源证据需要经过答案采纳（被 AI 直接采用）、明确引用（被 AI 点名引用）和正文可用性校验后，才会进入候选列表。'}
              metric={topSource?.balanced_weight ? n(topSource.balanced_weight, 2) : undefined}
            />
          </div>
        </div>

        <div className="rounded-3xl border bg-card/70 p-4 sm:p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="flex items-center gap-2">
                <ShieldCheck className="h-4 w-4 text-emerald-300" />
                <h2 className="text-lg font-semibold">待人工审核</h2>
              </div>
              <p className="mt-1 text-sm text-muted-foreground">所有接入动作都停在这里，不自动上线。</p>
            </div>
            <Badge variant={gate?.ready ? 'default' : 'secondary'}>{gate?.ready ? '可复核' : '未就绪'}</Badge>
          </div>
          <div className="mt-4 grid gap-3">
            <ReviewQueueItem
              label="媒体绑定"
              value={bindingPendingCount}
              hint="候选媒体需要确认主体、资源和客户行业是否匹配。"
              tone={bindingPendingCount > 0 ? 'amber' : 'green'}
            />
            <ReviewQueueItem
              label="写作策略"
              value={strategyPendingCount}
              hint="策略只能先生成候选，审核后才可设为内部当前策略。"
              tone={strategyPendingCount > 0 ? 'amber' : 'green'}
            />
            <ReviewQueueItem
              label="上线前检查"
              value={takeoverBlockers.length || (gate?.ready ? '通过' : '待预览')}
              hint="这里只检查准备条件，真正接管仍需老板单独授权。"
              tone={takeoverBlockers.length ? 'amber' : 'green'}
            />
            <div className="rounded-xl border border-emerald-500/25 bg-emerald-500/5 p-3">
              <p className="text-sm font-medium text-emerald-100">当前线上影响：无</p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                {advisoryModeHint[advisoryMode]} 客户看不到这里的实验数据，线上写作和投放规则不会被自动替换。
              </p>
            </div>
          </div>
        </div>
      </div>

      <div className={cn(
        'rounded-3xl border bg-card/70 p-4 sm:p-5',
        health?.level === 'green' && 'border-emerald-500/30 bg-emerald-500/5',
        health?.level === 'yellow' && 'border-amber-500/30 bg-amber-500/5',
        health?.level === 'red' && 'border-red-500/30 bg-red-500/5',
      )}>
        <div className="grid gap-5 xl:grid-cols-[0.8fr_1.2fr]">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <Activity className="h-4 w-4 text-emerald-300" />
              <h2 className="font-semibold">飞轮健康</h2>
              <Badge variant={health?.level === 'green' ? 'default' : 'secondary'}>{health?.label || '检查中'}</Badge>
            </div>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">
              {health?.summary || '正在读取当前行业的数据健康信息。'}
            </p>
            {!!health?.reasons?.length && (
              <p className="mt-2 text-xs leading-5 text-muted-foreground">下一步：{health.next_action}</p>
            )}
          </div>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <JobMetric label="原始引用" value={rawRows.toLocaleString()} />
            <JobMetric label="可用正文" value={articleRows.toLocaleString()} />
            <JobMetric label="答案采纳" value={adoptionRows.toLocaleString()} />
            <JobMetric label="明确引用" value={Number(adoptionSummary.explicit_cited_rows || healthMetrics.explicit_citation_count || 0).toLocaleString()} />
            <JobMetric label="搜索曝光" value={Number(adoptionSummary.search_exposed_rows || healthMetrics.search_exposure_count || 0).toLocaleString()} />
            <JobMetric label="去重后的独立来源" value={Number(adoptionSummary.unique_sources || 0).toLocaleString()} />
            <JobMetric label="抓取质量差的正文" value={Number(healthMetrics.failed_body_count || 0).toLocaleString()} />
            <JobMetric label="重复组" value={Number(healthMetrics.legacy_duplicate_groups || 0).toLocaleString()} />
          </div>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
        <StatCard label="媒体实体" value={coverage?.entities || 0} hint="标准化媒体/站点实体" tone="green" />
        <StatCard label="可投放媒体/站点" value={purchasable} hint="已匹配真实媒体资源" />
        <StatCard label="仅参考媒体/站点" value={referenceOnly} hint="可指导写作或拓展媒体" />
        <StatCard label="来源证据" value={sourceSignals.length} hint={`${geoIndustryLabel(industry)} 聚合域名`} />
        <StatCard label="策略版本" value={strategies.length} hint="待审核 / 已审核 / 当前使用" tone="amber" />
      </div>

      <div className="rounded-3xl border border-cyan-500/20 bg-gradient-to-br from-cyan-500/10 via-card/70 to-emerald-500/5 p-4 sm:p-5">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div className="max-w-3xl">
            <div className="flex flex-wrap items-center gap-2">
              <BarChart3 className="h-4 w-4 text-cyan-300" />
              <h2 className="text-lg font-semibold">AI 从调研里总结的媒体/来源建议</h2>
              <Badge variant="secondary">人工复核后使用</Badge>
            </div>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">
              飞轮把调研监测结果浓缩成“来源表现、平台表现、写作参考”。这些内容只给运营判断，不直接进入客户输出。
            </p>
            <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
              <JobMetric label="采集样本" value={Number(advisoryObservability?.raw_rows || 0).toLocaleString()} />
              <JobMetric label="AI 总结条数" value={Number(advisoryObservability?.stats_rows || 0).toLocaleString()} />
              <JobMetric label="候选建议" value={Number(advisoryObservability?.candidate_count || 0).toLocaleString()} />
              <JobMetric label="最近更新" value={fmtDate(advisoryObservability?.latest_stats_at || undefined)} />
            </div>
          </div>
          <div className="w-full rounded-2xl border bg-background/50 p-4 xl:max-w-md">
            <div className="flex items-center gap-2">
              <Clock3 className="h-4 w-4 text-emerald-300" />
              <p className="font-medium">运行模式</p>
            </div>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">
              {advisoryModeHint[advisoryMode]}
            </p>
            <p className="mt-2 text-xs text-muted-foreground">
              最近轮次：{advisoryObservability?.latest_round_id || '暂无'} · 距更新 {latestStatsAge == null ? '-' : `${n(latestStatsAge)} 小时`}
            </p>
          </div>
        </div>

        <div className="mt-4 grid gap-3 lg:grid-cols-3">
          {topAdvisoryCandidates.map((candidate) => (
            <div key={`${candidate.platform}-${candidate.engine}-${candidate.industry}`} className="rounded-2xl border bg-background/50 p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="font-semibold">{candidate.platform || '候选来源'}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{candidate.engine || 'AI 引擎'} · {geoIndustryLabel(candidate.industry || industry)}</p>
                </div>
                <Badge variant="outline">{pct(candidate.citation_rate || 0)}</Badge>
              </div>
              <p className="mt-3 text-sm leading-6 text-muted-foreground">
                {candidate.suggestion || '该来源在最近采集里出现稳定引用信号，可作为媒体或写作侧的候选参考。'}
              </p>
              <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-muted-foreground">
                <span>引用 {Number(candidate.citation_count || 0)} 次</span>
                <span>均位 {candidate.avg_position ? n(candidate.avg_position) : '-'}</span>
              </div>
            </div>
          ))}
          {!topAdvisoryCandidates.length && (
            <div className="rounded-2xl border border-dashed bg-background/40 p-4 text-sm text-muted-foreground lg:col-span-3">
              还没有 AI 总结出的建议。请等下一轮调研完成，或切换到有数据的行业查看。
            </div>
          )}
        </div>
      </div>

      {lastJob && (
        <div className={cn(
          'rounded-2xl border bg-card/70 p-4',
          Number(lastJob.loaded ?? 0) === 0 && 'border-amber-500/30 bg-amber-500/5',
        )}>
          <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <h3 className="font-semibold">最近任务结果</h3>
              <p className="mt-1 text-sm text-muted-foreground">{summarizeJobResult(lastJob)}</p>
            </div>
            <Badge variant={lastJob.dry_run ? 'secondary' : 'default'}>
              {lastJob.dry_run ? '只读预览' : '已保存'}
            </Badge>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <JobMetric label="读取数据" value={Number(lastJob.loaded ?? 0)} />
            <JobMetric label="展示/采样" value={Number(lastJob.sampled ?? (lastJob.preview || lastJob.items || []).length)} />
            <JobMetric label="保存数量" value={Number(lastJob.written ?? 0)} />
            <JobMetric label="跳过数据" value={Number(lastJob.skipped ?? 0)} />
          </div>
          <details className="mt-3 rounded-md border bg-background/50">
            <summary className="cursor-pointer px-3 py-2 text-sm text-muted-foreground">
              技术明细（仅排查问题时查看）
            </summary>
            <pre className="max-h-56 overflow-auto border-t p-3 text-xs text-muted-foreground">
              {JSON.stringify(lastJob, null, 2)}
            </pre>
          </details>
        </div>
      )}

      {bridgeHealth?.stale && (
        <div className="rounded-2xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm leading-6 text-amber-200">
          <p className="font-semibold">{bridgeHealth.stale_message}</p>
          {bridgeHealth.lag_hours != null && (
            <p className="mt-1 text-xs leading-5 text-amber-100/80">影子层滞后约 {Math.round(bridgeHealth.lag_hours)} 小时,请触发一次飞轮桥接补数据</p>
          )}
        </div>
      )}

      <Tabs value={activeTab} onValueChange={setActiveTab} className="space-y-4">
        <TabsList className="flex h-auto w-full max-w-5xl flex-wrap justify-start gap-2 rounded-2xl bg-muted/50 p-2">
          <TabsTrigger value="advisory" className="rounded-xl">总览</TabsTrigger>
          <TabsTrigger value="media" className="rounded-xl">媒体建议</TabsTrigger>
          <TabsTrigger value="writing" className="rounded-xl">内容策略</TabsTrigger>
          <TabsTrigger value="sources" className="rounded-xl">证据来源</TabsTrigger>
          <TabsTrigger value="calibration" className="rounded-xl">人工审核</TabsTrigger>
          <TabsTrigger value="answer-entities" className="rounded-xl">答案实体</TabsTrigger>
        </TabsList>

        <TabsContent value="answer-entities" className="space-y-4">
          <div className="rounded-lg border bg-card/60 p-4">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
              <div>
                <h2 className="text-lg font-semibold">答案实体雷达（真实推荐图谱）</h2>
                <p className="text-sm text-muted-foreground">
                  从 AI 回答里抽取被真实推荐的品牌/公司 + 排名 + 理由 + 哪些引擎推。只读 shadow，不接管线上推荐、不进客户页、不展示 AI 原文。
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <ActionWithHint hint="只计数不调 LLM">
                  <Button variant="outline" onClick={() => runAnswerEntityJob('ae-preview', true)} disabled={!!answerEntityBusy}>
                    <RotateCw className="mr-2 h-4 w-4" />
                    预览答案实体
                  </Button>
                </ActionWithHint>
                <ActionWithHint hint="后台 LLM 抽取，写入 shadow" tone="amber">
                  <Button onClick={() => runAnswerEntityJob('ae-write', false)} disabled={!!answerEntityBusy}>
                    <Database className="mr-2 h-4 w-4" />
                    保存答案实体 shadow
                  </Button>
                </ActionWithHint>
              </div>
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <JobMetric label="回答覆盖率" value={pct(answerEntityHealth?.answer_text_coverage || 0)} />
              <JobMetric label="实体抽取覆盖率" value={pct(answerEntityHealth?.extraction_coverage || 0)} />
              <JobMetric label="实体总数" value={Number(answerEntityHealth?.entity_total || 0)} />
              <JobMetric label="低置信占比" value={pct(answerEntityHealth?.low_confidence_ratio || 0)} />
              <JobMetric label="可关联来源比例" value={pct(answerEntityHealth?.entity_source_linkable_ratio || 0)} />
              <JobMetric label="近 7 天新增" value={Number(answerEntityHealth?.facts_last_7d || 0)} />
              <JobMetric label="近 30 天新增" value={Number(answerEntityHealth?.facts_last_30d || 0)} />
              <JobMetric label="最近更新时间" value={fmtDate(answerEntityHealth?.last_updated || undefined)} />
            </div>
            {answerEntityCostEstimate?.human_note && (
              <p className="mt-3 text-[11px] leading-5 text-amber-300">
                预计成本:{answerEntityCostEstimate.human_note}
              </p>
            )}
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <div className="overflow-hidden rounded-lg border">
              <div className="border-b bg-muted/30 px-4 py-3">
                <h3 className="font-semibold">行业 Top 推荐实体</h3>
                <p className="mt-1 text-xs text-muted-foreground">被 AI 推荐次数、覆盖引擎数、平均推荐排名（admin 面可见百分比/排名）。</p>
              </div>
              <table className="w-full text-sm">
                <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3">实体</th>
                    <th className="px-4 py-3">推荐次数</th>
                    <th className="px-4 py-3">覆盖引擎</th>
                    <th className="px-4 py-3">平均排名</th>
                  </tr>
                </thead>
                <tbody>
                  {(answerEntitySummary?.entities || []).map((row) => (
                    <tr key={row.entity_key} className="border-t">
                      <td className="px-4 py-3 font-medium">{row.entity_name}</td>
                      <td className="px-4 py-3">{row.mention_count}</td>
                      <td className="px-4 py-3">{row.engine_count}（{(row.engines || []).join('、') || '-'}）</td>
                      <td className="px-4 py-3">{row.avg_recommendation_rank ?? '-'}</td>
                    </tr>
                  ))}
                  {!(answerEntitySummary?.entities || []).length && (
                    <tr><td colSpan={4} className="px-4 py-8 text-center text-muted-foreground">暂无答案实体。请先「预览答案实体」，再「保存答案实体 shadow」。</td></tr>
                  )}
                </tbody>
              </table>
            </div>

            <div className="overflow-hidden rounded-lg border">
              <div className="border-b bg-muted/30 px-4 py-3">
                <h3 className="font-semibold">推荐理由 Top 词</h3>
                <p className="mt-1 text-xs text-muted-foreground">竞品/实体被 AI 推荐时最常出现的理由（写作要点信号，见消费口路线图）。</p>
              </div>
              <div className="flex flex-wrap gap-2 p-4">
                {(answerEntitySummary?.top_recommendation_reasons || []).map((r) => (
                  <Badge key={r.reason} variant="secondary">{r.reason}（{r.count}）</Badge>
                ))}
                {!(answerEntitySummary?.top_recommendation_reasons || []).length && (
                  <p className="text-sm text-muted-foreground">暂无理由聚合。</p>
                )}
              </div>
            </div>
          </div>
        </TabsContent>

        <TabsContent value="advisory" className="space-y-4">
          <div className="rounded-lg border bg-card/60 p-4">
            <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
              <div className="max-w-3xl">
                <div className="flex flex-wrap items-center gap-2">
                  <ShieldCheck className="h-4 w-4 text-emerald-300" />
                  <h2 className="text-lg font-semibold">运行健康与候选来源</h2>
                  <Badge variant={advisoryIsOpen ? 'default' : 'secondary'}>
                    {advisoryModeLabel[advisoryMode]}
                  </Badge>
                </div>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">
                  这里只展示内部数据是否能给运营提供参考。不会自动改写文章、不会自动推荐投放、不会触发扣费。
                </p>
                <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <JobMetric label="最近采集" value={advisoryObservability?.latest_round_id ? '已完成' : '暂无'} />
                  <JobMetric label="原始记录" value={Number(advisoryObservability?.raw_rows || 0)} />
                  <JobMetric label="统计结果" value={Number(advisoryObservability?.stats_rows || 0)} />
                  <JobMetric label="候选建议" value={Number(advisoryObservability?.candidate_count || 0)} />
                  <JobMetric label="最近更新时间" value={fmtDate(advisoryObservability?.latest_stats_at || undefined)} />
                  <JobMetric label="距上次更新" value={latestStatsAge == null ? '-' : `${n(latestStatsAge)} 小时`} />
                  <JobMetric label="本轮成本" value={`¥${n(advisoryObservability?.total_cost_yuan || 0, 2)}`} />
                  <JobMetric label="需检查轮次" value={Number(advisoryObservability?.failed_resumable_count || 0)} />
                </div>
              </div>

              <div className="w-full rounded-lg border bg-background/50 p-4 xl:max-w-md">
                <p className="font-medium">顾问参考模式</p>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">
                  {advisoryModeHint[advisoryMode]}
                </p>
                <div className="mt-4 grid gap-2">
                  {(['off', 'admin_shadow', 'advisory'] as const).map((mode) => (
                    <Button
                      key={mode}
                      variant={advisoryMode === mode ? 'default' : 'outline'}
                      onClick={() => updateAdvisoryMode(mode)}
                      disabled={!!busy}
                      className="justify-start"
                    >
                      {advisoryModeLabel[mode]}
                    </Button>
                  ))}
                </div>
                <p className="mt-3 text-[11px] leading-5 text-muted-foreground">
                  “顾问可参考”需要二次确认；开启后也只是后台建议，不会接管线上写作或投放。
                </p>
              </div>
            </div>
          </div>

          <div className="rounded-lg border bg-card/60 p-4">
            <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
              <div>
                <h2 className="text-lg font-semibold">候选来源明细</h2>
                <p className="text-sm text-muted-foreground">
                  基于最近采集结果生成，供运营判断哪些来源在不同 AI 引擎里更容易被引用。
                </p>
              </div>
              <Badge variant="secondary">仅后台参考</Badge>
            </div>

            <div className="mt-4 overflow-hidden rounded-lg border">
              <table className="w-full text-sm">
                <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3">建议来源</th>
                    <th className="px-4 py-3">AI 引擎</th>
                    <th className="px-4 py-3">引用率</th>
                    <th className="px-4 py-3">引用次数</th>
                    <th className="px-4 py-3">平均位置</th>
                    <th className="px-4 py-3">依据</th>
                  </tr>
                </thead>
                <tbody>
                  {advisoryCandidates.map((candidate) => (
                    <tr key={`${candidate.platform}-${candidate.engine}-${candidate.industry}`} className="border-t align-top">
                      <td className="px-4 py-3">
                        <div className="font-medium">{candidate.platform || '-'}</div>
                        <div className="mt-1 text-xs text-muted-foreground">{candidate.suggestion || '可作为内部参考。'}</div>
                      </td>
                      <td className="px-4 py-3">{candidate.engine || '-'}</td>
                      <td className="px-4 py-3">{pct(candidate.citation_rate || 0)}</td>
                      <td className="px-4 py-3">{Number(candidate.citation_count || 0)}</td>
                      <td className="px-4 py-3">{candidate.avg_position ? n(candidate.avg_position) : '-'}</td>
                      <td className="px-4 py-3 text-xs text-muted-foreground">
                        <div>{candidate.source?.type === 'flywheel_stats' ? '飞轮统计' : '内部统计'}</div>
                        <div>{fmtDate(candidate.source?.last_updated || undefined)}</div>
                      </td>
                    </tr>
                  ))}
                  {!advisoryCandidates.length && (
                    <tr>
                      <td colSpan={6} className="px-4 py-8 text-center text-muted-foreground">
                        {advisoryMode === 'off'
                          ? '当前模式关闭。可先查看健康摘要；如需候选建议，请开启“仅后台观察”。'
                          : '暂无候选建议。请等待下一轮采集完成或切换行业查看。'}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </TabsContent>

        <TabsContent value="media" className="space-y-4">
          <div className="rounded-lg border bg-card/60 p-4">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
              <div>
                <h2 className="text-lg font-semibold">媒体/站点候选</h2>
                <p className="text-sm text-muted-foreground">从已加权的来源证据生成媒体/站点候选，只保存为待审核数据。</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <ActionWithHint hint="只读预览，不保存">
                  <Button variant="outline" onClick={() => runJob('media-preview', () => apiPost('/media/rebuild-from-source-signals', { industry, limit: 100, dry_run: true }))} disabled={!!busy}>
                    <PlayCircle className="mr-2 h-4 w-4" />
                    预览媒体/站点候选
                  </Button>
                </ActionWithHint>
                <ActionWithHint hint="保存为待审核数据" tone="amber">
                  <Button onClick={() => runJob('media-write', () => apiPost('/media/rebuild-from-source-signals', { industry, limit: 100, dry_run: false }))} disabled={!!busy || health?.can_save_shadow === false}>
                    <Database className="mr-2 h-4 w-4" />
                    保存为待审核数据
                  </Button>
                </ActionWithHint>
              </div>
            </div>
          </div>

          <div className="rounded-lg border bg-card/60 p-4">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
              <div>
                <h2 className="text-lg font-semibold">媒体绑定候选</h2>
                <p className="text-sm text-muted-foreground">
                  把媒体/站点候选与发布通道真实库存做人工审核绑定。通过后只更新飞轮审核数据，不会接管线上投放。
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <ActionWithHint hint="只读预览，不保存">
                  <Button variant="outline" onClick={() => rebuildBindingCandidates(true)} disabled={!!busy}>
                    <PlayCircle className="mr-2 h-4 w-4" />
                    预览绑定候选
                  </Button>
                </ActionWithHint>
                <ActionWithHint hint="写入待审核候选，不接管线上投放" tone="amber">
                  <Button onClick={() => rebuildBindingCandidates(false)} disabled={!!busy || health?.can_save_shadow === false}>
                    <Database className="mr-2 h-4 w-4" />
                    写入待审核候选
                  </Button>
                </ActionWithHint>
              </div>
            </div>

            <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
              <div className="space-y-3">
                <div className="rounded-2xl border bg-emerald-500/5 p-4">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
                    <div>
                      <p className="text-sm font-semibold text-emerald-100">AI 已完成库存匹配，人工只做最终确认</p>
                      <p className="mt-1 text-sm leading-6 text-muted-foreground">
                        不需要到发布管理手工查编号；系统已用媒介库存自动匹配媒体名称、域名和来源线索。请只判断“这是不是同一个媒体/账号、是否适合该行业”。
                      </p>
                    </div>
                    <div className="grid grid-cols-3 gap-2 text-center text-xs sm:min-w-[300px]">
                      <div className="rounded-xl border bg-background/60 p-3">
                        <div className="text-2xl font-bold">{bindingReviewBuckets.pending.length}</div>
                        <div className="mt-1 text-muted-foreground">待审核</div>
                      </div>
                      <div className="rounded-xl border bg-background/60 p-3">
                        <div className="text-2xl font-bold text-emerald-300">{bindingReviewBuckets.recommended.length}</div>
                        <div className="mt-1 text-muted-foreground">建议通过</div>
                      </div>
                      <div className="rounded-xl border bg-background/60 p-3">
                        <div className="text-2xl font-bold text-amber-300">{bindingReviewBuckets.needsReview.length}</div>
                        <div className="mt-1 text-muted-foreground">需核对</div>
                      </div>
                    </div>
                  </div>
                </div>

                {bindingReviewBuckets.recommended.length > 0 && (
                  <div className="space-y-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-2 text-sm font-medium text-emerald-100">
                        <CheckCircle2 className="h-4 w-4" />
                        建议通过（AI 已核对，置信度≥90% 且无风险提示）
                      </div>
                      <Button
                        size="sm"
                        disabled={!!busy}
                        onClick={() => void batchApproveRecommended()}
                      >
                        一键通过这 {bindingReviewBuckets.recommended.length} 条
                      </Button>
                    </div>
                    {bindingReviewBuckets.recommended.map(renderBindingCandidateCard)}
                  </div>
                )}

                {bindingReviewBuckets.needsReview.length > 0 && (
                  <div className="space-y-3">
                    <div className="flex items-center gap-2 text-sm font-medium text-amber-100">
                      <ShieldCheck className="h-4 w-4" />
                      需要人工核对
                    </div>
                    {bindingReviewBuckets.needsReview.map(renderBindingCandidateCard)}
                  </div>
                )}

                {!mediaBindingCandidates.length && (
                  <div className="rounded-2xl border border-dashed bg-card/40 p-8 text-center text-muted-foreground">
                    暂无媒体绑定候选。请先点击“预览绑定候选”，确认结果后再写入待审核候选。
                  </div>
                )}
              </div>

              <div className="space-y-3">
                <div className="rounded-2xl border bg-card/60 p-4">
                  <p className="font-semibold">审核进度</p>
                  <div className="mt-4 grid gap-3">
                    <JobMetric label="已通过" value={bindingReviewBuckets.approved.length} />
                    <JobMetric label="已驳回" value={bindingReviewBuckets.rejected.length} />
                    <JobMetric label="当前线上影响" value="0" />
                  </div>
                  <p className="mt-3 text-xs leading-5 text-muted-foreground">
                    这一步只更新飞轮审核数据。即使通过绑定，也不会自动安排投放、报价、扣费或替换线上策略。
                  </p>
                  <Button
                    className="mt-3 w-full"
                    disabled={!recommendedCount || !!busy}
                    onClick={() => void approveAllRecommended()}
                  >
                    一键通过全部行业的建议通过（{recommendedCount ?? 0} 条）
                  </Button>
                  <p className="mt-2 text-[11px] leading-4 text-muted-foreground">
                    跨全部行业的 AI 高置信候选(置信≥90% 且无风险);灰区候选仍需逐条人工。
                  </p>
                </div>

                {/* [P0-2 2026-08-15] 未成功明细。后端一直返回 failed[{candidate_id,error}],
                    前端过去只取计数 —— 于是「4 条未成功」只能靠去生产查库才知道是供应商下架。 */}
                {bindingFailures && bindingFailures.failures.length > 0 && (
                  <div
                    data-testid="binding-failure-panel"
                    className="rounded-2xl border border-amber-500/40 bg-amber-500/5 p-4"
                  >
                    <button
                      type="button"
                      data-testid="binding-failure-toggle"
                      className="flex w-full items-center justify-between gap-2 text-left"
                      onClick={() => setBindingFailuresOpen((v) => !v)}
                    >
                      <span className="text-sm font-semibold text-amber-100">
                        未成功明细 · {bindingFailures.scope} · {bindingFailures.failures.length} 条
                      </span>
                      <span className="text-xs text-muted-foreground">
                        {bindingFailuresOpen ? '收起' : '展开'}
                      </span>
                    </button>
                    {bindingFailuresOpen && (
                      <div className="mt-3 space-y-2">
                        {bindingFailures.failures.map((f, idx) => (
                          <div
                            key={`bind-fail-${f.candidate_id ?? idx}`}
                            data-testid="binding-failure-item"
                            className="rounded-xl border bg-background/45 p-3"
                          >
                            <div className="flex items-center justify-between gap-2">
                              <span className="min-w-0 truncate text-sm font-medium">
                                {f.media_name || `候选 #${f.candidate_id ?? '-'}`}
                              </span>
                              <Badge variant="secondary">
                                {geoIndustryLabel(f.industry_key || '') || f.industry_key || '通用'}
                              </Badge>
                            </div>
                            <p className="mt-1 text-xs leading-5 text-amber-100/90">
                              {f.error || '未提供原因'}
                            </p>
                          </div>
                        ))}
                        <p className="text-[11px] leading-4 text-muted-foreground">
                          这些候选留在待审列表,可逐条处理。「库存不可采购」通常是供应商已下架 ——
                          下一次重新统计时它们会自动退出「建议通过」。
                        </p>
                      </div>
                    )}
                  </div>
                )}

                {(bindingReviewBuckets.approved.length > 0 || bindingReviewBuckets.rejected.length > 0) && (
                  <div className="rounded-2xl border bg-card/60 p-4">
                    <p className="font-semibold">最近处理</p>
                    <div className="mt-3 space-y-2">
                      {[...bindingReviewBuckets.approved, ...bindingReviewBuckets.rejected].slice(0, 5).map((candidate) => (
                        <div key={`${candidate.entity_key}-${candidate.media_source}-${candidate.inventory_id}-done`} className="rounded-xl border bg-background/45 p-3">
                          <div className="flex items-center justify-between gap-2">
                            <span className="min-w-0 truncate text-sm font-medium">{bindingVisibleName(candidate)}</span>
                            <Badge variant={bindingStatus(candidate) === 'approved' ? 'default' : 'secondary'}>
                              {bindingStatusLabel[bindingStatus(candidate)] || bindingStatus(candidate)}
                            </Badge>
                          </div>
                          <p className="mt-1 text-xs text-muted-foreground">{fmtDate(candidate.updated_at)}</p>
                          {bindingStatus(candidate) === 'approved' && (
                            <Button
                              size="sm"
                              variant="outline"
                              className="mt-2"
                              disabled={!!busy}
                              onClick={() => void revokeBinding(candidate)}
                            >
                              撤销
                            </Button>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>

          <div className={cn(
            'rounded-lg border bg-card/60 p-4',
            gate?.ready ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-amber-500/30 bg-amber-500/5',
          )}>
            <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
              <div className="max-w-3xl">
                <div className="flex flex-wrap items-center gap-2">
                  <ShieldCheck className="h-4 w-4 text-emerald-300" />
                  <h2 className="text-lg font-semibold">上线前人工闸门</h2>
                  <Badge variant={gate?.ready ? 'default' : 'secondary'}>
                    {gate?.status_label || '等待预览'}
                  </Badge>
                </div>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">
                  当前不接管线上。这里仅检查未来是否具备进入媒体推荐的准备条件；真正改线上推荐必须老板单独授权。
                </p>
                <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <JobMetric label="已通过媒体绑定" value={approvedBindingCount} />
                  <JobMetric label="答案采纳记录" value={Number(gate?.metrics?.answer_adopted_rows || 0)} />
                  <JobMetric label="明确引用记录" value={Number(gate?.metrics?.explicit_cited_rows || 0)} />
                  <JobMetric label="当前线上影响" value="0" />
                </div>
                <div className="mt-4 rounded-md border bg-background/50 p-3">
                  <p className="text-sm font-medium">系统判断</p>
                  <p className="mt-2 text-sm text-muted-foreground">
                    {gate?.next_action || '请先预览接管条件。'}
                  </p>
                  {takeoverBlockers.length > 0 && (
                    <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-muted-foreground">
                      {takeoverBlockers.map((blocker) => (
                        <li key={blocker}>{blocker}</li>
                      ))}
                    </ul>
                  )}
                  {takeoverPolicy && (
                    <p className="mt-2 text-xs text-muted-foreground">
                      当前记录：{takeoverPolicyStatusLabel[takeoverPolicy.status || ''] || '待保存'} · 最近审核 {fmtDate(takeoverPolicy.reviewed_at || takeoverPolicy.disabled_at)}
                    </p>
                  )}
                </div>
              </div>

              <div className="w-full space-y-3 xl:max-w-md">
                <div>
                  <p className="mb-2 text-sm font-medium">白名单范围</p>
                  <Textarea
                    value={takeoverWhitelistText}
                    onChange={(e) => setTakeoverWhitelistText(e.target.value)}
                    placeholder="每行填写一个客户、品牌或代理标识；不填则不能保存接管准备"
                    className="min-h-[92px]"
                  />
                </div>
                <div>
                  <p className="mb-2 text-sm font-medium">审核备注</p>
                  <Textarea
                    value={takeoverNote}
                    onChange={(e) => setTakeoverNote(e.target.value)}
                    placeholder="说明为什么准备接管、白名单范围和风险边界"
                    className="min-h-[76px]"
                  />
                </div>
                <div className="flex flex-wrap gap-2">
                  <ActionWithHint hint="只读预览，不保存">
                    <Button variant="outline" onClick={previewTakeoverGate} disabled={!!busy}>
                      <PlayCircle className="mr-2 h-4 w-4" />
                      预览接管条件
                    </Button>
                  </ActionWithHint>
                  <ActionWithHint hint="保存准备记录，不接管线上" tone="amber">
                    <Button onClick={saveTakeoverGate} disabled={!!busy || !gate?.ready}>
                      <Database className="mr-2 h-4 w-4" />
                      保存接管准备
                    </Button>
                  </ActionWithHint>
                  <ActionWithHint hint="关闭准备状态" tone="amber">
                    <Button variant="outline" onClick={disableTakeoverGate} disabled={!!busy}>
                      <XCircle className="mr-2 h-4 w-4" />
                      停用接管准备
                    </Button>
                  </ActionWithHint>
                </div>
                <p className="text-[11px] leading-5 text-muted-foreground">
                  等待老板单独授权前，所有结果只进入飞轮审核记录，不会影响投放推荐、写作规则、报价或扣费。
                </p>
              </div>
            </div>
          </div>

          <div className="flex flex-wrap gap-2">
            {mediaFilterOptions.map((option) => (
              <Button
                key={option.key}
                size="sm"
                variant={mediaFilter === option.key ? 'default' : 'outline'}
                onClick={() => setMediaFilter(option.key)}
              >
                {option.label}
              </Button>
            ))}
          </div>

          <div className="overflow-hidden rounded-lg border">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-3">媒体/站点</th>
                  <th className="px-4 py-3">状态</th>
                  <th className="px-4 py-3">综合建议分</th>
                  <th className="px-4 py-3">引用证据分</th>
                  <th className="px-4 py-3">可投放资源</th>
                  <th className="px-4 py-3">系统判断</th>
                  <th className="px-4 py-3">详情</th>
                </tr>
              </thead>
              <tbody>
                {filteredMediaItems.map((item) => (
                  <tr
                    key={`${item.entity_key}-${item.industry_key}`}
                    className={cn(
                      'border-t',
                      selectedMediaKey === item.entity_key && 'bg-emerald-500/5',
                    )}
                  >
                    <td className="px-4 py-3">
                      <div className="font-medium">{item.canonical_name || item.domain}</div>
                      <div className="text-xs text-muted-foreground">{item.domain || '-'}</div>
                    </td>
                    <td className="px-4 py-3">
                      <Badge variant={mediaStatusTone(item)}>
                        {mediaStatusLabel(item)}
                      </Badge>
                    </td>
                    <td className="px-4 py-3">{n(item.shadow_score)}</td>
                    <td className="px-4 py-3">{n(item.evidence_score)}</td>
                    <td className="px-4 py-3">{n(item.inventory_score)}</td>
                    <td className="max-w-xl px-4 py-3 text-xs text-muted-foreground">
                      {mediaSystemJudgement(item)}
                    </td>
                    <td className="px-4 py-3">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          setSelectedMediaKey(item.entity_key || '');
                          setMediaDetailOpen(true);
                        }}
                      >
                        查看详情
                      </Button>
                    </td>
                  </tr>
                ))}
                {!filteredMediaItems.length && (
                  <tr>
                    <td colSpan={7} className="px-4 py-8 text-center text-muted-foreground">暂无媒体/站点候选，请先预览来源证据并保存待审核数据。</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {mediaDetailOpen && selectedMedia && (
            <MediaDetailDrawer item={selectedMedia} onClose={() => setMediaDetailOpen(false)} />
          )}
        </TabsContent>

        <TabsContent value="sources" className="space-y-4">
          <div className="rounded-lg border bg-card/60 p-4">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
              <div>
                <h2 className="text-lg font-semibold">来源证据重建</h2>
                <p className="text-sm text-muted-foreground">答案采纳、明确引用、搜索曝光、原文抓取分层计权，长来源列表会做公平折算。</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <ActionWithHint hint="只读预览，不保存">
                  <Button variant="outline" onClick={() => runJob('source-preview', () => apiPost('/source-signals/rebuild', { industry, limit: 1000, dry_run: true }))} disabled={!!busy}>
                    <RotateCw className="mr-2 h-4 w-4" />
                    预览来源证据
                  </Button>
                </ActionWithHint>
                <ActionWithHint hint="保存为待审核数据" tone="amber">
                  <Button onClick={() => runJob('source-write', () => apiPost('/source-signals/rebuild', { industry, limit: 1000, dry_run: false }))} disabled={!!busy || health?.can_preview === false}>
                    <Database className="mr-2 h-4 w-4" />
                    保存来源证据
                  </Button>
                </ActionWithHint>
                <ActionWithHint hint="只读预览，不保存">
                  <Button variant="outline" onClick={() => runJob('adoption-preview', () => apiPost('/answer-adoption/rebuild', { industry, limit: 50000, dry_run: true }))} disabled={!!busy}>
                    <PlayCircle className="mr-2 h-4 w-4" />
                    预览真实采纳指标
                  </Button>
                </ActionWithHint>
                <ActionWithHint hint="写入指标表，不接管线上" tone="amber">
                  <Button onClick={() => runJob('adoption-write', () => apiPost('/answer-adoption/rebuild', { industry, limit: 50000, dry_run: false }))} disabled={!!busy || health?.can_preview === false}>
                    <Database className="mr-2 h-4 w-4" />
                    保存真实采纳指标
                  </Button>
                </ActionWithHint>
              </div>
            </div>
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <div className="overflow-hidden rounded-lg border">
              <div className="border-b bg-muted/30 px-4 py-3">
                <h3 className="font-semibold">按引擎拆分</h3>
                <p className="mt-1 text-xs text-muted-foreground">看不同 AI 引擎里，答案采纳、明确引用和搜索曝光的比例。</p>
              </div>
              <table className="w-full text-sm">
                <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3">引擎</th>
                    <th className="px-4 py-3">答案采纳</th>
                    <th className="px-4 py-3">明确引用</th>
                    <th className="px-4 py-3">搜索曝光</th>
                    <th className="px-4 py-3">独立来源</th>
                  </tr>
                </thead>
                <tbody>
                  {(answerMetrics?.by_engine || []).map((row) => (
                    <tr key={row.engine || 'unknown'} className="border-t">
                      <td className="px-4 py-3 font-medium">{row.engine || '未知'}</td>
                      <td className="px-4 py-3">{row.answer_adopted_rows || 0}</td>
                      <td className="px-4 py-3">{row.explicit_cited_rows || 0}</td>
                      <td className="px-4 py-3">{row.search_exposed_rows || 0}</td>
                      <td className="px-4 py-3">{row.unique_sources || 0}</td>
                    </tr>
                  ))}
                  {!(answerMetrics?.by_engine || []).length && (
                    <tr>
                      <td colSpan={5} className="px-4 py-8 text-center text-muted-foreground">暂无真实采纳指标。请先预览或保存真实采纳指标。</td>
                    </tr>
                  )}
                </tbody>
              </table>
              <p className="px-4 py-2 text-[11px] text-muted-foreground">
                豆包 / Kimi 不返回原生引用标记,采纳按答案正文 [n] 标记推断;无标记的行计为搜索曝光,不代表未被使用。
              </p>
            </div>

            <div className="overflow-hidden rounded-lg border">
              <div className="border-b bg-muted/30 px-4 py-3">
                <h3 className="font-semibold">高价值来源</h3>
                <p className="mt-1 text-xs text-muted-foreground">优先看被答案采纳或明确引用的来源，不再按单纯网页数量排序。</p>
              </div>
              <table className="w-full text-sm">
                <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3">域名</th>
                    <th className="px-4 py-3">答案采纳</th>
                    <th className="px-4 py-3">明确引用</th>
                    <th className="px-4 py-3">搜索曝光</th>
                    <th className="px-4 py-3">覆盖</th>
                  </tr>
                </thead>
                <tbody>
                  {(answerMetrics?.top_sources || []).map((row) => (
                    <tr key={row.domain || row.source_url || 'unknown'} className="border-t">
                      <td className="px-4 py-3">
                        <div className="font-medium">{row.domain || '未知'}</div>
                        <div className="max-w-xs truncate text-xs text-muted-foreground">{row.source_url || '-'}</div>
                      </td>
                      <td className="px-4 py-3">{row.answer_adopted_rows || 0}</td>
                      <td className="px-4 py-3">{row.explicit_cited_rows || 0}</td>
                      <td className="px-4 py-3">{row.search_exposed_rows || 0}</td>
                      <td className="px-4 py-3">{row.engine_count || 0} 引擎 · {row.prompt_count || 0} 问题</td>
                    </tr>
                  ))}
                  {!(answerMetrics?.top_sources || []).length && (
                    <tr>
                      <td colSpan={5} className="px-4 py-8 text-center text-muted-foreground">暂无高价值来源。真实采纳指标写入后会显示。</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className="overflow-hidden rounded-lg border">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-3">域名</th>
                  <th className="px-4 py-3">权重</th>
                  <th className="px-4 py-3">答案采纳</th>
                  <th className="px-4 py-3" title="AI 回答里真用了这个来源(答案采纳 + 明确引用)">明确引用</th>
                  <th className="px-4 py-3">搜索曝光/抓取参考</th>
                  <th className="px-4 py-3">覆盖范围</th>
                  <th className="px-4 py-3">最近出现</th>
                </tr>
              </thead>
              <tbody>
                {sourceSignals.map((row) => (
                  <tr key={`${row.domain}-${row.industry_key}`} className="border-t">
                    <td className="px-4 py-3 font-medium">{row.domain || '-'}</td>
                    <td className="px-4 py-3">{n(row.balanced_weight, 3)}</td>
                    <td className="px-4 py-3">{row.answer_adopted_count || 0}</td>
                    <td className="px-4 py-3">{row.explicit_cited_count ?? row.cited_count ?? 0}</td>
                    <td className="px-4 py-3">{row.search_only_count || 0} / {row.reference_only_count || 0}</td>
                    <td className="px-4 py-3">{row.engine_count || 0} 引擎 · {row.prompt_count || 0} 问题</td>
                    <td className="px-4 py-3 text-xs text-muted-foreground">{fmtDate(row.last_seen_at)}</td>
                  </tr>
                ))}
                {!sourceSignals.length && (
                  <tr>
                    <td colSpan={7} className="px-4 py-8 text-center text-muted-foreground">暂无来源证据。先从调研监测原始数据做只读预览。</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </TabsContent>

        <TabsContent value="writing" className="space-y-4">
          <div className="grid gap-4 xl:grid-cols-[1fr_360px]">
            <div className="space-y-4 rounded-lg border bg-card/60 p-4">
              <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
              <div>
                <h2 className="text-lg font-semibold">写作策略版本</h2>
                  <p className="text-sm text-muted-foreground">
                    写作策略使用全部文章，不跟随右上角行业筛选；先抽取文章风格，再结合引用来源和发布结果生成策略。
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <ActionWithHint hint="只读预览，不保存">
                    <Button variant="outline" onClick={() => rebuildStyleFeatures(true)} disabled={!!busy}>
                      <RotateCw className="mr-2 h-4 w-4" />
                      预览文章风格
                    </Button>
                  </ActionWithHint>
                  <ActionWithHint hint="保存为待审核数据" tone="amber">
                    <Button variant="outline" onClick={() => rebuildStyleFeatures(false)} disabled={!!busy || health?.can_save_shadow === false}>
                      <Database className="mr-2 h-4 w-4" />
                      保存文章风格
                    </Button>
                  </ActionWithHint>
                  <ActionWithHint hint="只生成候选">
                    <Button variant="outline" onClick={() => generateStrategy(false)} disabled={!!busy}>
                      <Sparkles className="mr-2 h-4 w-4" />
                      生成策略建议
                    </Button>
                  </ActionWithHint>
                  <ActionWithHint hint="保存后需要管理员审核" tone="green">
                    <Button onClick={() => generateStrategy(true)} disabled={!!busy}>
                      <FileText className="mr-2 h-4 w-4" />
                      保存为待审核策略
                    </Button>
                  </ActionWithHint>
                </div>
              </div>
              <div className="rounded-lg border border-emerald-500/25 bg-emerald-500/5 p-3 text-sm text-emerald-50">
                <div className="font-medium">文章学习口径</div>
                <p className="mt-1 text-muted-foreground">
                  写作策略使用全部文章，不跟随右上角行业筛选。系统先读研究文章，再补充正式生成过的文章；采纳或明确引用的文章权重更高。
                </p>
              </div>
              <div className="rounded-lg border bg-background/40 p-4">
                <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="font-semibold">文章结构研究</h3>
                      <Badge variant={articleStructure?.sample_status === 'ready' ? 'default' : 'secondary'}>
                        {articleStructure?.sample_status === 'ready' ? '样本可参考' : '样本观察中'}
                      </Badge>
                      {/* [包④ §3H-2] 等级被降级时如实说明,不让页面看起来"什么都没有" */}
                      {articleStructure?.corpus_grade_degraded && articleStructure?.corpus_grade_notice && (
                        <Badge variant="outline" data-testid="corpus-grade-degraded">
                          {articleStructure.corpus_grade_notice}
                        </Badge>
                      )}
                    </div>
                    {/* 🔴 我方生成稿身上没有调研信号,它们的 0 是"不适用"不是"实测为 0"。
                        不说清楚的话,页面会被读成"我们写的文章一篇都没被引用" —— 与事实相反。 */}
                    {(articleStructure?.signal_unmeasured_rows ?? 0) > 0 && (
                      <p className="mt-1 text-xs text-muted-foreground" data-testid="unmeasured-note">
                        其中 {articleStructure?.signal_unmeasured_rows} 篇是我方生成稿，
                        没有对应的调研信号（不是“采纳为 0”，是这批数据不适用该指标）。
                      </p>
                    )}
                    <p className="mt-2 text-sm leading-6 text-muted-foreground">
                      使用全部文章做整体写法研究；有采纳或明确引用的文章优先加权，正式生成过的文章用于补齐整体样本。当前只生成结构研究和策略候选，不会改线上写作规则。
                    </p>
                  </div>
                  <ActionWithHint hint="只读预览，不保存">
                    <Button variant="outline" onClick={analyzeArticleStructure} disabled={!!busy}>
                      <PlayCircle className="mr-2 h-4 w-4" />
                      预览文章结构
                    </Button>
                  </ActionWithHint>
                </div>

                <div className="mt-4 grid gap-3 sm:grid-cols-4">
                  <JobMetric label="已读取文章" value={articleStructureLoaded(articleStructure)} />
                  <JobMetric label="采纳样本" value={structureGroupCount(articleStructure, 'adopted_group')} />
                  <JobMetric label="明确引用样本" value={structureGroupCount(articleStructure, 'cited_group')} />
                  <JobMetric label="对照/参考样本" value={articleStructureReferenceCount(articleStructure)} />
                </div>

                <div className="mt-4 overflow-hidden rounded-lg border">
                  <table className="w-full text-sm">
                    <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
                      <tr>
                        <th className="px-4 py-3">结构特征</th>
                        <th className="px-4 py-3">采纳组占比</th>
                        <th className="px-4 py-3">对照组占比</th>
                        <th className="px-4 py-3">结构提升倍数</th>
                        <th className="px-4 py-3">建议</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(articleStructure?.feature_lift || []).slice(0, 8).map((row) => (
                        <tr key={row.feature || row.label} className="border-t">
                          <td className="px-4 py-3 font-medium">{row.label || row.feature || '-'}</td>
                          <td className="px-4 py-3">{pct(row.adopted_share)}</td>
                          <td className="px-4 py-3">{pct(row.control_share)}</td>
                          <td className="px-4 py-3">{n(row.lift, 2)} 倍</td>
                          <td className="px-4 py-3">
                            <Badge variant={row.recommended ? 'default' : 'secondary'}>
                              {row.recommended ? '可进入候选' : '继续观察'}
                            </Badge>
                          </td>
                        </tr>
                      ))}
                      {!(articleStructure?.feature_lift || []).length && (
                        <tr>
                          <td colSpan={5} className="px-4 py-8 text-center text-muted-foreground">
                            {articleStructureEmptyMessage(articleStructure)}
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>

                {!!(articleStructure?.warnings || []).length && (
                  <div className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100">
                    {(articleStructure?.warnings || []).join('；')}
                  </div>
                )}
                {(() => {
                  // V4 · 有 LLM 行业化建议优先渲染,否则回退静态可进入候选规则。
                  const llmRules = articleStructure?.recommended_structure_rules_llm || [];
                  const staticRules = articleStructure?.recommended_structure_rules || [];
                  const useLlm = llmRules.length > 0;
                  const rules = useLlm ? llmRules : staticRules;
                  const sampleReady = articleStructure?.sample_status === 'ready';
                  return (
                    <div className="mt-3 rounded-md border border-emerald-500/30 bg-emerald-500/5 p-3 text-sm">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="font-medium">{useLlm ? '行业化建议(AI)' : '可进入候选的结构规则'}</div>
                        <ActionWithHint hint={sampleReady ? '调用 AI 基于该行业采纳样本生成建议' : '样本不足,暂不能生成'}>
                          <Button variant="outline" size="sm" onClick={refreshLlmStructureRules} disabled={!!busy || !sampleReady}>
                            <Sparkles className="mr-2 h-4 w-4" />
                            刷新行业化建议
                          </Button>
                        </ActionWithHint>
                      </div>
                      {rules.length ? (
                        <ul className="mt-2 space-y-1 text-muted-foreground">
                          {rules.map((rule) => (
                            <li key={rule}>- {rule}</li>
                          ))}
                        </ul>
                      ) : (
                        <p className="mt-2 text-muted-foreground">
                          {sampleReady ? '点击「刷新行业化建议」由 AI 基于该行业采纳样本生成结构建议。' : '样本观察中:采纳样本达标后可生成行业化建议。'}
                        </p>
                      )}
                    </div>
                  );
                })()}
              </div>
              <Textarea
                value={operatorNote}
                onChange={(e) => setOperatorNote(e.target.value)}
                placeholder="审核备注、行业边界、禁用表达或样本说明"
                className="min-h-[82px]"
              />

              <div className="overflow-hidden rounded-lg border">
                <table className="w-full text-sm">
                  <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
                    <tr>
                      <th className="px-4 py-3">版本</th>
                      <th className="px-4 py-3">状态</th>
                      <th className="px-4 py-3">风格</th>
                      <th className="px-4 py-3">置信度</th>
                      <th className="px-4 py-3">证据/结果分</th>
                      <th className="px-4 py-3">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {strategies.map((row) => (
                      <tr key={row.id} className="border-t">
                        <td className="max-w-xs px-4 py-3">
                          <div className="font-medium">{row.strategy_version}</div>
                          <div className="line-clamp-2 text-xs text-muted-foreground">{row.guidance}</div>
                          {(row.status === 'shadow' || row.status === 'pending_review') && (
                            <Badge variant="secondary" className="mt-1">待审核策略</Badge>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <Badge variant={row.status === 'active' ? 'default' : 'secondary'}>
                            {statusLabel[row.status || ''] || row.status}
                          </Badge>
                        </td>
                        <td className="px-4 py-3">{styleLabel[row.style_family || ''] || row.style_family || '-'}</td>
                        <td className="px-4 py-3">{n((row.confidence || 0) * 100)}%</td>
                        <td className="px-4 py-3">{n(row.evidence_score, 2)} / {n(row.outcome_score, 2)}</td>
                        <td className="px-4 py-3">
                          <div className="flex flex-wrap gap-2">
                            {row.status === 'shadow' && (
                              <>
                                <Button size="sm" variant="outline" onClick={() => reviewStrategy(row, 'approve')} disabled={!!busy}>
                                  <CheckCircle2 className="mr-1 h-3.5 w-3.5" />
                                  通过
                                </Button>
                                <Button size="sm" variant="ghost" onClick={() => reviewStrategy(row, 'reject')} disabled={!!busy}>
                                  <XCircle className="mr-1 h-3.5 w-3.5" />
                                  驳回
                                </Button>
                              </>
                            )}
                            {row.status === 'approved' && (
                              <Button size="sm" onClick={() => openStrategyConfirm(row, 'activate')} disabled={!!busy || activationBlocked}>
                                <ShieldCheck className="mr-1 h-3.5 w-3.5" />
                                启用
                              </Button>
                            )}
                            {row.status === 'archived' && (
                              <Button size="sm" variant="outline" onClick={() => openStrategyConfirm(row, 'rollback')} disabled={!!busy || activationBlocked}>
                                <RotateCw className="mr-1 h-3.5 w-3.5" />
                                回滚
                              </Button>
                            )}
                            {row.status === 'active' && <span className="text-xs text-emerald-400">当前策略</span>}
                            <Button size="sm" variant="ghost" onClick={() => loadStrategyAudit(row.id)} disabled={!!busy}>
                              操作历史
                            </Button>
                          </div>
                        </td>
                      </tr>
                    ))}
                    {!strategies.length && (
                      <tr>
                        <td colSpan={6} className="px-4 py-8 text-center text-muted-foreground">暂无策略版本。请先预览文章风格，再保存文章风格，最后生成待审核策略。</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="space-y-4">
              {confirmAction && (
                <div className={cn(
                  'rounded-lg border bg-card/60 p-4',
                  confirmAction.type === 'activate' ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-amber-500/30 bg-amber-500/5',
                )}>
                  <div className="flex items-center gap-2">
                    {confirmAction.type === 'activate' ? (
                      <ShieldCheck className="h-4 w-4 text-emerald-400" />
                    ) : (
                      <RotateCw className="h-4 w-4 text-amber-300" />
                    )}
                    <h3 className="font-semibold">
                      {confirmAction.type === 'activate' ? '启用前确认' : '回滚前确认'}
                    </h3>
                  </div>
                  <div className="mt-3 space-y-2 text-sm">
                    <p className="font-medium">{confirmAction.strategy.strategy_version}</p>
                    <p className="text-muted-foreground">
                      {confirmAction.type === 'activate'
                        ? '启用后该策略会作为本行业后续写作的结构参考（写作大厅默认套用，代理可手动关闭）；文章仍需正常审核后发布，投放、报价、扣费不受影响。'
                        : '回滚会把该历史版本重新设为当前内部策略，并归档当前正在使用的版本。'}
                    </p>
                    {activationBlocked && (
                      <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-100">
                        数据健康为红/黄灯，暂不能启用或回滚：{health?.summary || '请先修复数据健康问题'}
                      </div>
                    )}
                    <Textarea
                      value={confirmNote}
                      onChange={(e) => setConfirmNote(e.target.value)}
                      placeholder="必须填写操作备注，例如：已复核文章风格和禁用表达，确认启用"
                      className="min-h-[90px]"
                    />
                    <div className="flex flex-wrap gap-2">
                      <Button onClick={submitStrategyConfirm} disabled={!!busy || activationBlocked || !confirmNote.trim()}>
                        {confirmAction.type === 'activate' ? '确认启用' : '确认回滚'}
                      </Button>
                      <Button
                        variant="ghost"
                        onClick={() => {
                          setConfirmAction(null);
                          setConfirmNote('');
                        }}
                        disabled={!!busy}
                      >
                        取消
                      </Button>
                    </div>
                  </div>
                </div>
              )}

              <div className="rounded-lg border bg-card/60 p-4">
                <div className="flex items-center gap-2">
                  <ShieldCheck className="h-4 w-4 text-emerald-400" />
                  <h3 className="font-semibold">当前使用的内部策略</h3>
                </div>
                {activeStrategy ? (
                  <div className="mt-3 space-y-2 text-sm">
                    <div className="font-medium">{activeStrategy.strategy_version}</div>
                    <div className="text-muted-foreground">{activeStrategy.guidance}</div>
                    <div className="text-xs text-muted-foreground">启用时间：{fmtDate(activeStrategy.activated_at)}</div>
                  </div>
                ) : (
                  <p className="mt-3 text-sm text-muted-foreground">当前行业还没有启用内部策略。启用前不会影响线上写作规则。</p>
                )}
              </div>
              <div className="rounded-lg border bg-card/60 p-4">
                <div className="flex items-center gap-2">
                  <FileText className="h-4 w-4 text-emerald-400" />
                  <h3 className="font-semibold">操作历史</h3>
                </div>
                {strategyAuditEvents.length ? (
                  <div className="mt-3 space-y-3">
                    {strategyAuditEvents.map((event) => (
                      <div key={event.id} className="rounded-md border bg-background/40 p-3 text-sm">
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-medium">{auditActionLabel[event.action || ''] || event.action || '操作记录'}</span>
                          <span className="text-xs text-muted-foreground">{fmtDate(event.created_at)}</span>
                        </div>
                        <div className="mt-1 text-xs text-muted-foreground">
                          {statusLabel[event.from_status || ''] || event.from_status || '-'} → {statusLabel[event.to_status || ''] || event.to_status || '-'} · 操作人 {event.actor_id || '-'}
                        </div>
                        {event.note && <p className="mt-2 text-xs leading-5 text-muted-foreground">{event.note}</p>}
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="mt-3 text-sm text-muted-foreground">选择策略后查看操作历史。</p>
                )}
              </div>
              <div className="rounded-lg border bg-card/60 p-4">
                <div className="flex items-center gap-2">
                  <Sparkles className="h-4 w-4 text-emerald-400" />
                  <h3 className="font-semibold">最近候选</h3>
                </div>
                {candidateInfo?.candidate ? (
                  <div className="mt-3 space-y-2 text-sm">
                    <Badge variant="secondary">{styleLabel[candidateInfo.candidate.style_family || ''] || candidateInfo.candidate.style_family}</Badge>
                    <p className="text-muted-foreground">{candidateInfo.candidate.guidance}</p>
                    <p className="text-xs text-muted-foreground">
                      输入样本：文章风格 {candidateInfo.input_counts?.style_features || 0} · 来源证据 {candidateInfo.input_counts?.source_signals || 0} · 发布结果 {candidateInfo.input_counts?.outcome_signals || 0}
                    </p>
                  </div>
                ) : (
                  <p className="mt-3 text-sm text-muted-foreground">点击生成候选后显示。</p>
                )}
              </div>
            </div>
          </div>
        </TabsContent>

        <TabsContent value="calibration" className="space-y-4">
          <div className="rounded-lg border bg-card/60 p-5">
            <div className="flex items-center gap-2">
              <Activity className="h-5 w-5 text-emerald-400" />
              <h2 className="text-lg font-semibold">校准与反馈闭环</h2>
            </div>
            <div className="mt-4 grid gap-4 md:grid-cols-3">
              <div className="rounded-lg border bg-background/40 p-4">
                <h3 className="font-medium">预测</h3>
                <p className="mt-2 text-sm text-muted-foreground">媒体推荐、写作策略、引用预期都记录为版本化预测。</p>
              </div>
              <div className="rounded-lg border bg-background/40 p-4">
                <h3 className="font-medium">结果</h3>
                <p className="mt-2 text-sm text-muted-foreground">发布状态、AI 引用变化、监测分数变化进入 outcome 事件。</p>
              </div>
              <div className="rounded-lg border bg-background/40 p-4">
                <h3 className="font-medium">修正</h3>
                <p className="mt-2 text-sm text-muted-foreground">偏差超过阈值时生成建议，管理员确认后进入下一轮待审核策略。</p>
              </div>
            </div>
            <div className="mt-4 rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-4 text-sm text-emerald-100">
              第二期口径：自动修正只生成候选和校准建议，不直接改生产写作或投放策略。
            </div>
            <div className="mt-4 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
              <span>本周自动通过</span>
              <Badge variant="secondary">{autoApprovedRecent?.count ?? 0} 条</Badge>
              <span className="text-xs">近 7 天 AI 高置信自动通过的媒体绑定,可在媒体建议页逐条撤销。</span>
            </div>
          </div>

          {/* [B3-1] 引擎权重候选审核区 */}
          <div className="rounded-lg border bg-card/60 p-5">
            <div className="flex items-center gap-2">
              <Activity className="h-5 w-5 text-emerald-400" />
              <h2 className="text-lg font-semibold">引擎权重候选（待人工审核）</h2>
            </div>
            <p className="mt-2 text-sm text-muted-foreground">
              这些是按各行业真实引用份额算出的引擎权重建议。通过后会更新媒体推荐的评分口径；不改报价、不扣费、不自动发布。
            </p>
            {engineWeightCandidates.length === 0 ? (
              <p className="mt-4 text-sm text-muted-foreground">
                暂无待审核的引擎权重候选（每轮调研聚合后会按行业引用份额自动生成）。
              </p>
            ) : (
              <div className="mt-4 space-y-3">
                {engineWeightCandidates.map((c) => {
                  const ev = (c.evidence || {}) as Record<string, number>;
                  const cur = typeof c.current_weight === 'number' ? c.current_weight.toFixed(2) : '—';
                  const sug = Number(c.suggested_weight || 0).toFixed(2);
                  const sharePct = ev.citation_share != null ? `${Math.round(Number(ev.citation_share) * 100)}%` : '—';
                  return (
                    <div key={c.id} className="rounded-xl border bg-background/40 p-4">
                      <div className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="font-medium">{c.engine}</span>
                        <span className="text-muted-foreground">· {geoIndustryLabel(c.industry) || c.industry}</span>
                        <span>现行 {cur} → 建议 <b className="text-emerald-300">{sug}</b></span>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">
                        证据：该行业引用份额 {sharePct} · 引用 {ev.engine_citations ?? '—'} 次 / 样本 {ev.samples ?? '—'}
                      </p>
                      <div className="mt-2">
                        <Textarea
                          value={engineWeightNotes[c.id] || ''}
                          onChange={(e) => setEngineWeightNotes((prev) => ({ ...prev, [c.id]: e.target.value }))}
                          placeholder="审核备注：说明为什么采用或驳回这个权重"
                          className="min-h-[72px]"
                        />
                        <div className="mt-2 flex flex-wrap gap-2">
                          <Button size="sm" onClick={() => reviewEngineWeightCandidate(c, 'approve')} disabled={!!busy}>
                            采用权重
                          </Button>
                          <Button size="sm" variant="outline" onClick={() => reviewEngineWeightCandidate(c, 'reject')} disabled={!!busy}>
                            驳回
                          </Button>
                        </div>
                        <p className="mt-2 text-[11px] leading-4 text-muted-foreground">
                          采用后更新全局引擎权重（媒体推荐评分口径），不接管线上投放，也不会自动发布客户可见内容。
                        </p>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

        </TabsContent>
      </Tabs>
      {confirmDialog}
    </div>
  );
}

// [W7] 新顶层 4 tab 壳:默认落「飞轮全景」· 右上角共享行业选择器 · 「高级」= 原控制台。
export default function GeoPlacementFlywheel() {
  const [topTab, setTopTab] = useState('panorama');
  const [industry, setIndustry] = useState('general');

  return (
    <div className="mx-auto w-full max-w-[1440px] space-y-4 p-4 sm:p-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-xl font-semibold">GEO 数据飞轮</h1>
            <Badge variant="secondary" className="text-emerald-300">反哺进化</Badge>
          </div>
          <p className="mt-0.5 text-sm text-muted-foreground">
            系统自动从调研数据学习并反哺写作 / 投放 · 所有上线变更必须人工确认。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground">行业</span>
          <Select value={industry} onValueChange={setIndustry}>
            <SelectTrigger className="w-[190px]">
              <SelectValue placeholder="选择行业" />
            </SelectTrigger>
            <SelectContent>
              {GEO_INDUSTRY_OPTIONS_WITH_GENERAL.map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      <Tabs value={topTab} onValueChange={setTopTab} className="space-y-4">
        <TabsList className="flex h-auto w-full max-w-2xl flex-wrap justify-start gap-2 rounded-2xl bg-muted/50 p-2">
          <TabsTrigger value="panorama" className="rounded-xl">飞轮全景</TabsTrigger>
          <TabsTrigger value="feedback" className="rounded-xl">反哺成果</TabsTrigger>
          <TabsTrigger value="media-board" className="rounded-xl">有效性榜</TabsTrigger>
          <TabsTrigger value="health" className="rounded-xl">数据健康</TabsTrigger>
          <TabsTrigger value="advanced" className="rounded-xl">高级</TabsTrigger>
        </TabsList>

        <TabsContent value="panorama" className="space-y-4">
          <PanoramaTab industry={industry} industryLabel={geoIndustryLabel(industry) || industry} onNavigate={setTopTab} />
        </TabsContent>
        <TabsContent value="media-board" className="space-y-4">
          <MediaEffectivenessBoard industry={industry} />
        </TabsContent>
        <TabsContent value="feedback" className="space-y-4">
          <FeedbackTab industry={industry} onNavigate={setTopTab} />
        </TabsContent>
        <TabsContent value="health" className="space-y-4">
          <DataHealthTab industry={industry} onNavigate={setTopTab} />
        </TabsContent>
        <TabsContent value="advanced" className="space-y-4">
          <AdvancedFlywheelConsole />
        </TabsContent>
      </Tabs>
    </div>
  );
}
