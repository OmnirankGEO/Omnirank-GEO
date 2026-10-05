/**
 * GEO 统一观测飞轮 vNext · Frontend-A 候选
 * 产品 DTO 类型 —— 严格镜像 AI-3 真实只读契约（api/geo_observation_product_api.py +
 * schemas/geo_observation_product.py @ feat/geo-observation-analytics-2026-07-17）。
 *
 * 铁律：
 * - 只消费后端算好的 value/count/*_bps/status；前端不算任何 SSOT 指标。
 * - 比例字段可空（null≠0）：无数据是 null，绝不当 0%。
 * - 不发明 AI-3 不存在的字段/端点；AI-2 未交付的写接口走 capability 层（隐藏写控件）。
 */

// ============ 枚举（与 AI-3 Literal 一致） ============

export type OutcomeKey =
  | 'recommended'
  | 'conditionally_recommended'
  | 'candidate_only'
  | 'mentioned_only'
  | 'criteria_only'
  | 'refused_no_evidence'
  | 'refused_risk'
  | 'not_mentioned'
  | 'entity_ambiguous'
  | 'engine_error';

export type StabilityStatus = 'stable' | 'watch' | 'insufficient' | 'shifted';

/** AI-3 真实 granularity（非 7/30/90 天）。 */
export type Granularity = 'day' | 'week' | 'month';

// ============ 共享值对象 ============

export interface WindowDTO {
  label: string;
  start: string;
  end: string;
}

export interface BrandRef {
  brand_id: number;
  display_name: string;
}

export interface OutcomeCount {
  outcome: OutcomeKey;
  count: number;
}

export interface NextAction {
  action: string;
  title: string;
  reason: string;
  requires_confirmation: boolean;
  may_charge: boolean;
}

// ============ 服务商私域 · summary ============

export interface BrandSummaryMetrics {
  valid_observations: number;
  // 全部比例可空：无数据是 null，不是 0%
  presence_rate_bps: number | null;
  explicit_recommendation_rate_bps: number | null;
  conditional_recommendation_rate_bps: number | null;
  candidate_rate_bps: number | null;
  criteria_only_rate_bps: number | null;
  refusal_no_evidence_rate_bps: number | null;
  refusal_risk_rate_bps: number | null;
  not_mentioned_rate_bps: number | null;
  citation_rate_bps: number | null;
  evidence_coverage_rate_bps: number | null;
  share_of_voice_bps: number | null;
  stability_status: StabilityStatus;
  stability_explanation: string;
}

export interface Comparison {
  presence_change_bps: number | null;
  recommendation_change_bps: number | null;
  comparison_allowed: boolean;
  reason: string | null;
}

export interface BrandSummary {
  brand: BrandRef;
  window: WindowDTO;
  data_updated_at: string | null;
  metric_version: string;
  summary: BrandSummaryMetrics;
  comparison: Comparison;
  outcomes: OutcomeCount[];
  next_actions: NextAction[];
}

// ============ 服务商私域 · trend（真实模型漂移契约） ============

export interface TrendPoint {
  bucket_start: string;
  bucket_end: string;
  valid_observations: number;
  presence_rate_bps: number;
  explicit_recommendation_rate_bps: number;
  stability_status: StabilityStatus;
  /** 该点是否落在模型升级断点（后端确认）。 */
  model_shift_marker: boolean;
  confirmed_change: boolean;
}

export interface BrandTrend {
  brand: BrandRef;
  window: WindowDTO;
  metric_version: string;
  granularity: Granularity;
  points: TrendPoint[];
  /** 后端给的跨升级点不可比说明；无则 null。 */
  comparison_note: string | null;
}

// ============ 服务商私域 · platforms ============

export interface PlatformItem {
  platform_key: string;
  display_name: string;
  /** 采集通道实现细节；用户端按 section 六 不展示，仅内部/管理员参考。 */
  surface_note: string | null;
  valid_observations: number;
  presence_rate_bps: number;
  explicit_recommendation_rate_bps: number;
  stability_status: StabilityStatus;
  updated_at: string | null;
}

export interface HistoricalPlatform {
  platform_key: string;
  display_name: string;
  status: 'historical';
  last_observed_at: string | null;
}

export interface BrandPlatforms {
  items: PlatformItem[];
  historical_platforms: HistoricalPlatform[];
}

// ============ 服务商私域 · questions + evidence ============

export interface QuestionItem {
  observation_id: string;
  question: string;
  outcome: OutcomeKey;
  matched_text: string | null;
  position: number | null;
  citations: number;
  changed: boolean;
  evidence_available: boolean;
}

export interface QuestionsPage {
  page: number;
  page_size: number;
  total: number;
  items: QuestionItem[];
}

/** AI-3 只给域名 + 类型（隐私：无完整 URL/标题）。 */
export interface Citation {
  domain: string;
  source_type: 'citation' | 'source';
  rank: number | null;
}

export interface ChannelDisclosure {
  platform: string;
  note: string | null;
}

export interface EvidenceNextAction {
  action: string;
  title: string;
  may_charge: boolean;
}

export interface EvidenceDetail {
  observation_id: string;
  question: string;
  outcome: OutcomeKey;
  answer_excerpt: string | null;
  matched_text: string | null;
  citations: Citation[];
  evidence_gap: string[];
  next_action: EvidenceNextAction | null;
  channel_disclosure: ChannelDisclosure;
}

// ============ 机会（私域 + 公共 + 管理员） ============

export type ContentType =
  | 'case_study'
  | 'comparison_method'
  | 'qualification_evidence'
  | 'faq'
  | 'data_report';

export interface Opportunity {
  opportunity_id: string;
  priority: number;
  topic: string;
  recommended_content_type: ContentType | string;
  recommended_evidence: string[];
  recommended_media_pattern: string[];
  reason: string;
  sample_size: number;
  stability: 'stable' | 'watch' | 'insufficient';
  auto_action_allowed: false;
}

export interface OpportunitiesResponse {
  items: Opportunity[];
}

// ============ 公共行业基线 ============

export interface IndustryBaseline {
  industry_key: string;
  window: WindowDTO;
  metric_version: string;
  valid_observations: number;
  presence_rate_bps: number;
  explicit_recommendation_rate_bps: number;
  conditional_recommendation_rate_bps: number;
  criteria_only_rate_bps: number;
  refusal_no_evidence_rate_bps: number;
  refusal_risk_rate_bps: number;
  citation_rate_bps: number;
  evidence_coverage_rate_bps: number;
  stability_status: StabilityStatus;
  sample_scope: string;
}

/** 公共层用 status 字段判样本不足，不用 HTTP 状态。 */
export interface PublicBaselineResponse {
  status: 'ok' | 'insufficient_samples';
  industry_key: string;
  sample_scope: string;
  message: string | null;
  baseline: IndustryBaseline | null;
}

// ============ 语义洞察任务 ============

export type InsightJobState = 'pending' | 'running' | 'completed' | 'failed';

export interface InsightJobStatus {
  job_id: string;
  state: InsightJobState;
  request_id?: string | null;
  progress_percent?: number | null;
  summary?: string | null;
  evidence_refs?: string[] | null;
  allowed_actions?: string[] | null;
}

// ============ 管理员治理 DTO（AI-3 真实） ============

export type AdminReadiness = 'ready' | 'attention_required' | 'unavailable';
export type RuntimeHealth = 'healthy' | 'degraded' | 'unavailable' | 'unknown';

export interface AdminPlatformHealth {
  platform: string;
  enabled_by_policy: boolean;
  runtime_health: RuntimeHealth;
}

export interface AdminCounts {
  pending_review: number;
  private_only: number;
  rejected: number;
  withdrawn: number;
  stuck_claims: number;
}

export interface CollectionReadiness {
  mode: 'existing_collectors_reconciled' | 'native_sampling_driver';
  status: AdminReadiness;
  problems: string[];
  reconciler_last_success_at: string | null;
  reconciler_backlog: number;
  source_watermarks: Record<string, string | null>;
  duplicate_collection_jobs: string[];
  policy_version: number | null;
}

export interface AdminOverview {
  readiness: AdminReadiness;
  counts: AdminCounts;
  reconciler_delay_seconds: number;
  aggregate_updated_at: string | null;
  policy_version: number | null;
  platform_health: AdminPlatformHealth[];
  collection_readiness: CollectionReadiness;
}

export interface AdminModelShift {
  industry_key: string;
  platform_key: string | null;
  model_revision: string | null;
  bucket_start: string;
  model_shift_index_bps: number;
  stability_status: StabilityStatus;
}

export interface AdminModelShifts {
  items: AdminModelShift[];
}

export interface AdminAggregateDiffItem {
  industry_key: string;
  metric: string;
  shadow_bps: number;
  legacy_bps: number | null;
  delta_bps: number | null;
  diff_reason: string | null;
}

export interface AdminAggregateDiff {
  items: AdminAggregateDiffItem[];
  note: string | null;
}

// ============ 结构化错误 ============

export type ObservationErrorCode =
  | 'FORBIDDEN'
  | 'VERSION_CONFLICT'
  | 'ENV_OVERRIDE_ACTIVE'
  | 'OBSERVATION_UNAVAILABLE'
  | 'SEMANTIC_INSIGHT_UNAVAILABLE'
  | 'INSUFFICIENT_SAMPLES'
  | string;

export interface ObservationErrorBody {
  code: ObservationErrorCode;
  message: string;
  retryable?: boolean;
}

/** transport 抛出的统一错误：携带 HTTP status + 结构化 body。 */
export class ObservationApiError extends Error {
  status: number;
  code: ObservationErrorCode;
  retryable: boolean;
  constructor(status: number, body: Partial<ObservationErrorBody> & { message?: string }) {
    super(body.message || `observation error ${status}`);
    this.name = 'ObservationApiError';
    this.status = status;
    this.code = body.code || httpStatusToCode(status);
    this.retryable = body.retryable ?? status >= 500;
  }
}

export function httpStatusToCode(status: number): ObservationErrorCode {
  switch (status) {
    case 403:
      return 'FORBIDDEN';
    case 409:
      return 'VERSION_CONFLICT';
    case 423:
      return 'ENV_OVERRIDE_ACTIVE';
    case 503:
      return 'OBSERVATION_UNAVAILABLE';
    default:
      return `HTTP_${status}`;
  }
}
