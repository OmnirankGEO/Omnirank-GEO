// W7 · GEO 写作飞轮「后台整页重造」共享 API / 类型 / 格式化工具。
// 与 GeoPlacementFlywheel.tsx 的 AdvancedFlywheelConsole 共用同一 authFetch + API_BASE,
// 保证「数字同源」(W7-5 护栏 #4):飞轮全景/反哺成果/数据健康三个新 tab 的计数全部走此层。
import { useEffect, useState } from 'react';
import { authFetch } from '@/lib/api';

export const API_BASE = '/api/admin/geo-placement-flywheel';
// 采纳 / 驳回 / 回滚 走既有「写作文体控制台」双闸端点(不同前缀,不新造发布通道)。
export const STYLE_CONTROL_BASE = '/api/writing/style-control';

async function parseError(res: Response): Promise<string> {
  try {
    const data = await res.json();
    const detail = (data?.detail ?? data?.message) as unknown;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object') {
      const obj = detail as Record<string, unknown>;
      if (typeof obj.message === 'string') return obj.message;
      if (typeof obj.code === 'string') return obj.code;
    }
    return `请求失败 ${res.status}`;
  } catch {
    return `请求失败 ${res.status}`;
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`, { method: 'POST', body: JSON.stringify(body || {}) });
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

// 写作文体控制台(采纳/驳回/回滚)——不同前缀。
export async function scGet<T>(path: string): Promise<T> {
  const res = await authFetch(`${STYLE_CONTROL_BASE}${path}`);
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

export async function scPost<T>(path: string, body?: unknown): Promise<T> {
  const res = await authFetch(`${STYLE_CONTROL_BASE}${path}`, { method: 'POST', body: JSON.stringify(body || {}) });
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

// ========== V7 · 前端请求收口(内存缓存 + sessionStorage 持久层 + in-flight 去重 + SWR)==========
// 只包只读 GET(apiGet 层)。写动作(scPost activate/retire/rollback、POST outcome-backfill / llm_rules)
// 不缓存;写完成后调 invalidateFlywheelCache 让下次读穿透。scGet('/versions') 走乐观锁,永不缓存。
// key = 完整 path(已含 industry_key / limit 查询串,天然按行业分桶)。

type CacheEntry = { data: unknown; ts: number };

const CACHE_PREFIX = 'fw:cache:';
const memCache = new Map<string, CacheEntry>();
const inflight = new Map<string, Promise<unknown>>();
// 失效代际:每次 invalidateFlywheelCache 自增。后台 SWR 刷新在开始时记录代际,回填前校验;
// 若期间发生过失效(如刚点了采纳),丢弃这次陈旧回填,避免 read-fill-after-invalidate 让看板显示旧值。
let cacheEpoch = 0;

const DEFAULT_TTL = 60_000;
// 前缀匹配 → TTL(毫秒)。可调。
const TTL_RULES: Array<{ match: string; ttl: number }> = [
  { match: '/flywheel-panorama', ttl: 60_000 },
  { match: '/writing/evolution-board', ttl: 60_000 },
  { match: '/writing/article-evolution-cycles', ttl: 60_000 },
  { match: '/writing/article-structure/analyze', ttl: 600_000 },
  { match: '/answer-adoption', ttl: 300_000 },
  { match: '/flywheel-trends', ttl: 600_000 },
  { match: '/writing/outcome-summary', ttl: 300_000 },
  { match: '/writing/flywheel-insight', ttl: 60_000 },
];

function ttlFor(path: string): number {
  for (const r of TTL_RULES) if (path.startsWith(r.match)) return r.ttl;
  return DEFAULT_TTL;
}

function readSession(path: string): CacheEntry | null {
  try {
    const raw = sessionStorage.getItem(CACHE_PREFIX + path);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CacheEntry;
    if (parsed && typeof parsed.ts === 'number') return parsed;
  } catch {
    /* sessionStorage 不可用 / JSON 坏 → 忽略 */
  }
  return null;
}

function writeSession(path: string, entry: CacheEntry) {
  try {
    sessionStorage.setItem(CACHE_PREFIX + path, JSON.stringify(entry));
  } catch {
    /* 配额 / 隐私模式禁用 → 内存层仍生效,忽略 */
  }
}

/**
 * 带缓存的只读 GET。命中未过期直接返缓存;命中已过期(SWR)先返旧值同时后台刷新;
 * 未命中则发请求。并发同 path 只发一次(in-flight 去重)。
 */
export async function apiGetCached<T>(path: string, opts?: { ttl?: number; swr?: boolean }): Promise<T> {
  const ttl = opts?.ttl ?? ttlFor(path);
  const swr = opts?.swr ?? true;
  const now = Date.now();

  let entry = memCache.get(path);
  if (!entry) {
    const s = readSession(path);
    if (s) {
      entry = s;
      memCache.set(path, s);
    }
  }

  const fetchFresh = (): Promise<T> => {
    const existing = inflight.get(path);
    if (existing) return existing as Promise<T>;
    const myEpoch = cacheEpoch;
    const p = apiGet<T>(path)
      .then((data) => {
        // 仅当期间无失效(代际未变)才回填,否则丢弃陈旧回填(防 read-fill-after-invalidate)
        if (cacheEpoch === myEpoch) {
          const fresh: CacheEntry = { data, ts: Date.now() };
          memCache.set(path, fresh);
          writeSession(path, fresh);
        }
        return data;
      })
      .finally(() => {
        // [收尾] 只删自己这代 in-flight,防 invalidate 清表后新代请求被旧 finally 误删
        if (inflight.get(path) === p) inflight.delete(path);
      });
    inflight.set(path, p as Promise<unknown>);
    return p;
  };

  if (entry) {
    if (now - entry.ts < ttl) return entry.data as T;
    if (swr) {
      // 已过期:先返旧值,后台静默刷新缓存(失败保留旧值)。
      void fetchFresh().catch(() => {
        /* 后台刷新失败 → 保留旧缓存,下次再试 */
      });
      return entry.data as T;
    }
    return fetchFresh();
  }
  return fetchFresh();
}

/** 清缓存(内存 + sessionStorage + in-flight)。传前缀只清匹配项;不传清全部。写动作后调用。 */
export function invalidateFlywheelCache(prefixes?: string[]) {
  cacheEpoch++;  // 代际自增 → 正在后台刷新的陈旧回填将被丢弃
  const matchAll = !prefixes || prefixes.length === 0;
  const hit = (path: string) => matchAll || prefixes!.some((p) => path.startsWith(p));

  for (const key of Array.from(memCache.keys())) if (hit(key)) memCache.delete(key);
  for (const key of Array.from(inflight.keys())) if (hit(key)) inflight.delete(key);

  try {
    const toRemove: string[] = [];
    for (let i = 0; i < sessionStorage.length; i++) {
      const k = sessionStorage.key(i);
      if (k && k.startsWith(CACHE_PREFIX) && hit(k.slice(CACHE_PREFIX.length))) toRemove.push(k);
    }
    toRemove.forEach((k) => sessionStorage.removeItem(k));
  } catch {
    /* sessionStorage 不可用 → 忽略 */
  }
}

// ========== 类型 ==========

export type PanoramaNode = {
  key: string; // collect / learn / review / apply / outcome
  label: string;
  value: number;
  detail?: Record<string, number>;
  citation_rate?: number;
  /** [行业口径 2026-08-18] 'industry' = 本数已按所选行业过滤;'site' = 该环库里没有行业维度,仍是全站数。
   *  🔴 选了具体行业时,'site' 的环**必须**在 UI 上标「全站口径」——
   *  顶部挂着行业选择器却显示全站总数,正是本次要修的口径误导(汽车 1.8 万 vs 全站 23.9 万)。
   *  后端未返回该字段(老版本)时按 undefined 处理:不标 = 与改动前逐字同样式。 */
  industry_scope?: 'industry' | 'site';
};

export type PanoramaResponse = {
  status?: string;
  nodes: PanoramaNode[];
  citation_rate?: number;
  citation_rate_known?: boolean;
  pending_total: number;
  /** [P0 2026-08-16] true = 后端 fail-soft 吞掉了取数失败,上面这些 0 是「没查出来」不是「真的是 0」。
   *  必须渲染「数据加载失败」,绝不能渲染「0 · 运行正常」—— 2026-08-16 全站被锁排死时
   *  本面板正是靠一片 0 把事故伪装成了正常态。 */
  degraded?: boolean;
  degraded_reasons?: string[];
  /** 请求的行业('all' = 全站口径) */
  industry_key?: string;
  /** true = 后端真按行业过滤了(有行业列的三环);false = 全站口径 */
  industry_scoped?: boolean;
  /** 实际用于比对的行业别名全集(中文名 + 英文 key + 别名),便于复现取数 */
  industry_values?: string[];
  /** 选了行业时,仍为全站口径的环 key(库里没有行业维度,不硬造) */
  site_scope_nodes?: string[];
};

export type TrendPoint = {
  week: string;
  total: number;
  cited: number;
  citation_rate: number;
  cumulative: number;
};

export type TrendsResponse = { weeks: number; series: TrendPoint[] };

export type ReviewSummary = {
  verdict?: 'replace' | 'keep' | 'observe' | string;
  reason?: string;
  avg_current_score?: number;
  avg_candidate_score?: number;
  score_delta?: number;
  candidate_band?: string;
  dual_model?: boolean;
  reviewer_agreement?: boolean;
  diff_summary?: string[];
  // V2 · 评语(后端 fail-soft 填充,可能为空数组)。commentary_side=评语归属版本(keep 时为 current)。
  strengths?: string[];
  risks?: string[];
  one_line_advice?: string;
  commentary_side?: 'current' | 'candidate' | 'tie' | string;
  // 若后端未来把逐维分聚合进 summary,前端自动渲染五维条;当前为空则回退总分对比。
  current_dims?: Record<string, number>;
  candidate_dims?: Record<string, number>;
  holdout_lift_used?: boolean;
};

export type EvolutionCard = {
  style_code: string;
  style_name: string;
  current_version_id?: string | null;
  current_version_label?: string;
  current_activated_at?: string | null;
  candidate_version_id?: string | null;
  candidate_created_at?: string | null;
  candidate_evidence_samples?: number;
  train_adopted?: number;
  train_control?: number;
  latest_sim_id?: number | null;
  latest_sim_status?: string | null;
  review_summary?: ReviewSummary;
  board_state: string;
  board_state_label: string;
};

export type EvolutionBoardResponse = {
  industry_key: string;
  cards: EvolutionCard[];
  has_candidate_count: number;
  recommend_replace_count: number;
  // [2026-08-01 刷屏修] 阻断原因由看板级出一次,卡内只留短标签「数据不可裁决」。
  data_blocked?: boolean;
  data_blocked_reasons?: string[];
  data_blocked_reason?: string;
  data_blocked_card_count?: number;
};

export type ArticleEvolutionCycle = {
  id: number;
  cycle_key: string;
  cycle_version: string;
  state: 'review_ready' | 'approved' | 'no_change' | 'rejected' | string;
  truth_level?: string;
  data_health?: { state?: string; decision_status?: string; blockers?: string[] };
  corpus_summary?: Record<string, number>;
  review_summary?: Record<string, unknown>;
  experiment_summary?: Record<string, unknown>;
  question_summary?: Record<string, number>;
  recommendations?: Array<{ priority?: string; action?: string; reason?: string }>;
  started_at?: string;
  reviewed_at?: string | null;
  review_note?: string | null;
};

export type ArticleEvolutionCyclesResponse = {
  status?: string;
  items: ArticleEvolutionCycle[];
  count: number;
};

export type ArticleReviewQueueItem = {
  id: number;
  title?: string;
  style_family?: string;
  publication_profile?: string;
  article_review_status?: string;
  article_human_review_status?: string | null;
  article_review?: {
    quality_score?: number;
    explanation?: string;
    hard_failures?: Array<{ code?: string }>;
    warnings?: Array<{ code?: string }>;
  };
  evidence_manifest_hash?: string | null;
  brand_name?: string;
  industry?: string;
  created_at?: string;
};

export type ArticleReviewQueueResponse = {
  status?: string;
  items: ArticleReviewQueueItem[];
  count: number;
};

export type SimulationDetail = {
  id: number;
  style_code: string;
  industry_key: string;
  version_id?: string | null;
  topic_title: string;
  demo_brand_name?: string;
  current_article: string;
  candidate_article: string;
  current_word_count?: number;
  candidate_word_count?: number;
  structure_guidance_state?: string;
  user_message_identical?: boolean | null;
  review_summary?: ReviewSummary;
  status?: string;
  created_at?: string;
  // V1 · 后端已算好的结构差异(候选版新增/缺失的结构骨架)。
  structure_diff?: { added: string[]; missing: string[] };
};

// V1 · 该文体历史对比列表行(不含大正文,含结构差异 / 状态 / 评审结论)。
export type SimulationListRow = {
  id: number;
  style_code?: string;
  industry_key?: string;
  version_id?: string | null;
  topic_title?: string;
  status?: string;
  created_at?: string;
  structure_diff?: { added: string[]; missing: string[] };
  review_summary?: ReviewSummary;
};

export type StyleSimulationsResponse = {
  status?: string;
  shadow_only?: boolean;
  simulations: SimulationListRow[];
};

export type OutcomeMeasure = {
  style_code: string;
  style_name?: string;
  new_version_id?: string;
  old_version_id?: string;
  age_days?: number;
  new_articles?: number;
  old_articles?: number;
  new_rate?: number;
  old_rate?: number;
  comparable?: boolean;
  insufficient_data?: boolean;
  effect_decision?: 'PASS' | 'FAIL' | 'INCONCLUSIVE' | string;
  truth_level?: string;
  reason?: string;
  by_ai_surface?: Record<string, Array<Record<string, unknown>>>;
  rollback_suggestion?: {
    suggest_rollback?: boolean;
    confident?: boolean;
    drop_pct?: number;
    reason?: string;
  };
  // V3 · 一句话评语(生产暂为空;为空时回退黄灯数字规则,绝不编造)。
  commentary?: string;
};

export type OutcomeBackfillResponse = {
  mode?: string;
  eligible_versions?: number;
  measures: OutcomeMeasure[];
};

// V7 · GET 读写分离:页面加载走 outcome-summary(只读,不触发回写)。
export type OutcomeSummaryResponse = {
  status?: string;
  shadow_only?: boolean;
  mode?: string;
  eligible_versions?: number;
  measures: OutcomeMeasure[];
  note?: string;
};

export type FeatureLift = {
  feature?: string;
  label?: string;
  adopted_share?: number;
  control_share?: number;
  lift?: number;
  recommended?: boolean;
};

export type ArticleStructureResponse = {
  loaded?: number;
  feature_lift?: FeatureLift[];
  sample_status?: string;
  recommended_structure_rules?: string[];
  // V4 · 行业化 LLM 建议(GET 恒只返缓存/ null;POST llm_rules:true 才生成)。
  recommended_structure_rules_llm?: string[] | null;
  warnings?: string[];
};

// V5 · 飞轮总汇总 insight(AI 一段总评 + 逐条洞察)。数字来自 facts,文案来自后端。
export type FlywheelInsightFacts = {
  collected?: number;
  learned?: number;
  pending_review?: number;
  applied_versions?: number;
  citation_rate?: number;
  citation_rate_trend?: number;
  trend_weeks?: number;
  has_candidate_count?: number;
  recommend_replace_count?: number;
  board_state_counts?: Record<string, number>;
  eligible_outcome_versions?: number;
  rollback_flags?: number;
  health_can_activate?: boolean;
  health_blockers?: number;  // [收尾] 后端返 len(blockers) 数量(number),非字符串数组
  health_summary?: string;
};

export type FlywheelInsightResponse = {
  status?: string;
  shadow_only?: boolean;
  industry_key?: string;
  facts?: FlywheelInsightFacts;
  data_available?: boolean;
  insights?: string[];
  summary?: string;
  generated?: boolean;
  cached?: boolean;
  updated_at?: number | null;
};

export type EngineMetricRow = {
  engine?: string;
  answer_adopted_rows?: number;
  answer_adopted_count?: number;
  explicit_cited_rows?: number;
  explicit_cited_count?: number;
  search_exposed_rows?: number;
  unique_sources?: number;
};

export type AnswerAdoptionSummary = {
  summary?: Record<string, number>;
  by_engine?: EngineMetricRow[];
};

export type BridgeHealth = {
  last_raw_at?: string | null;
  last_source_signal_at?: string | null;
  last_answer_metric_at?: string | null;
  last_media_entity_at?: string | null;
  lag_hours?: number | null;
  stale?: boolean;
  stale_message?: string;
  last_bridge_status?: string | null;
  last_success_round_id?: string | null;
};

export type CorpusExportResponse = {
  items: Array<{ title?: string; source_url?: string; domain?: string; content?: string }>;
  count?: number;
  daily_remaining?: number;
  capped?: boolean;
};

export type QueryIntentCoverage = {
  coverage?: number;
  total_queries?: number;
  classified_queries?: number;
  unclassified_queries?: number;
  distribution?: Record<string, number>;
  backfill_running?: boolean;
};

// ========== 格式化 ==========

export function fmtInt(v: unknown): string {
  const num = Number(v ?? 0);
  if (!Number.isFinite(num)) return '0';
  return Math.round(num).toLocaleString('en-US');
}

/** 分数(0-1)→ 百分比字符串。 */
export function fmtPct(v: unknown, digits = 1): string {
  const num = Number(v ?? 0);
  if (!Number.isFinite(num)) return '0%';
  return `${(num * 100).toFixed(digits).replace(/\.0$/, '')}%`;
}

/** 0-100 分数直接展示(评审分)。 */
export function fmtScore(v: unknown): string {
  const num = Number(v ?? 0);
  if (!Number.isFinite(num)) return '0';
  return num.toFixed(0);
}

export function fmtDate(v?: string | null): string {
  if (!v) return '—';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** epoch(秒或毫秒,自动识别)→ 人话时间;insight.updated_at 用。 */
export function fmtEpoch(v?: number | null): string {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  const ms = Number(v) < 1e12 ? Number(v) * 1000 : Number(v);
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** V6 · 候选版人话提示(MM-DD · N 篇采纳范文蒸馏),替换裸日期。 */
export function candidateHint(createdAt?: string | null, evidenceSamples?: number): string {
  const parts: string[] = [];
  if (createdAt) {
    const d = new Date(createdAt);
    if (!Number.isNaN(d.getTime())) {
      const pad = (n: number) => String(n).padStart(2, '0');
      parts.push(`${pad(d.getMonth() + 1)}-${pad(d.getDate())}`);
    }
  }
  const n = Number(evidenceSamples || 0);
  if (n > 0) parts.push(`${n} 篇采纳范文蒸馏`);
  return parts.length ? parts.join(' · ') : '—';
}

/** 滞后小时 → 人话(数据健康主区禁裸 lag_hours)。 */
export function lagHuman(hours?: number | null): string {
  if (hours == null || !Number.isFinite(Number(hours))) return '—';
  const h = Math.round(Number(hours));
  if (h < 24) return `${h} 小时`;
  const days = Math.floor(h / 24);
  const rest = h % 24;
  return rest ? `${days} 天 ${rest} 小时` : `${days} 天`;
}

/** 客户端触发 Markdown 下载(无第三方依赖)。 */
export function downloadTextFile(filename: string, content: string, mime = 'text/markdown;charset=utf-8') {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 尊重 prefers-reduced-motion(全站动效护栏)。 */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    setReduced(mq.matches);
    const handler = () => setReduced(mq.matches);
    mq.addEventListener?.('change', handler);
    return () => mq.removeEventListener?.('change', handler);
  }, []);
  return reduced;
}
