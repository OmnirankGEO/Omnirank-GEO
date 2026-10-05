/**
 * v3.3 / v3.4 GEO 全自动托管 — TypeScript 类型定义
 *
 * 对接后端 api/managed_campaign_api.py 端点响应
 */

// ============================================================
// 估算响应（POST /api/managed/estimate）
// ============================================================

export type TierKey = 'entry' | 'standard' | 'flagship' | 'strong' | 'custom';
export type CampaignMode = 'semi_auto' | 'full_auto';
export type CampaignStatus =
  | 'active'
  | 'paused'
  | 'depleted'
  | 'keyword_blocked'
  | 'user_cancelled'
  | 'dormancy_converted';

export interface TierOption {
  sov_pct: number;
  label: string;          // "经常被推荐"
  articles: number;
  cost_yuan: number;
  cost_yuan_range?: [number, number];
  detection_rate_pct?: number;
  recommended?: boolean;
  error?: string;
}

export interface CustomEstimate {
  achievable_sov_pct: number;
  achievable_articles: number;
  detection_rate_30d_pct: number;
  matched_tier_label: string;
  breakdown: {
    monitoring_yuan: number;
    write_publish_yuan: number;
  };
  avg_article_cost_yuan?: number;
}

export interface PlatformRecommendation {
  platform?: string;
  media_name?: string;
  our_price_yuan?: number;
  our_price_points?: number;
  inclusion_rate?: string;
  engines?: string[];
  geo_score?: number;
  reason?: string;
}

export interface EstimateResponse {
  plan_card: true;
  keyword: string;
  city: string;
  industry: string;
  competition_level: number;       // 1-5
  competition_count: number;       // effective_competition (A + B*0.5 + C*0.3)
  competition_confidence: number;
  // 2026-04-17: 真实竞品池来源明细（走 metaso+ABCD 时有值）
  competition_raw_count?: number;  // metaso 原始返回条数（含噪声）
  competition_breakdown?: { A?: number; B?: number; C?: number; D?: number };  // LLM 分类
  competition_source?: 'metaso_llm_classify' | 'llm_flash_estimate' | 'fallback';
  estimated_uprank_days: string;   // "7-14 天"
  tier_options: Record<TierKey, TierOption>;
  custom_input: CustomEstimate | null;
  platform_mix_recommended: PlatformRecommendation[];
  default_mode: CampaignMode;
  mode_switch_label: string;
  estimate_quoted_at: string;       // ISO
  estimate_valid_until: string;     // ISO
  estimate_validity_note: string;
  max_per_article_yuan: number;
  check_frequency_per_day: number;
  note: string;
  requires_user_confirm: boolean;
  user_id?: number;
}

// ============================================================
// 套餐主体
// ============================================================

export interface Campaign {
  id: number;
  user_id: number;
  brand_id: number;
  keyword: string;
  target_sov_pct: number;
  target_display_label: string;
  tier_label: TierKey;

  initial_recharge_yuan: number;
  total_recharged_yuan: number;
  total_consumed_yuan: number;
  balance_yuan: number;             // 计算字段

  mode: CampaignMode;
  max_per_article_yuan: number;
  check_frequency_per_day: number;

  status: CampaignStatus;
  delivered_articles: number;
  consecutive_zero_detection_days: number;
  low_balance_warned: boolean;

  estimate_quoted_at: string | null;
  estimate_valid_until: string | null;

  current_plan: Record<string, unknown> | null;

  authorized_at: string;
  paused_at: string | null;
  depleted_at: string | null;
  created_at: string;
  last_active_at: string;
}

export interface CampaignAction {
  id: number;
  action_type: string;
  action_detail: Record<string, unknown> | null;
  cost_points: number;
  cost_yuan: number;
  result: string;
  reason: string | null;
  created_at: string;
}

export interface CampaignDetail extends Campaign {
  recent_actions: CampaignAction[];
  pending_reviews: PendingReview[];
}

// ============================================================
// 待审队列
// ============================================================

export interface PendingReview {
  id: number;
  campaign_id: number;
  article_id: number | null;
  title: string;
  content_preview: string;
  full_content?: string;
  platforms_to_publish: string[];
  ai_reasoning: string;
  estimated_publish_cost_yuan: number;
  status: 'pending' | 'approved' | 'rejected' | 'withdrawn' | 'auto_published' | 'expired';
  auto_publish_at: string;
  reviewed_at: string | null;
  reviewed_by_user_id: number | null;
  review_note: string | null;
  created_at: string;
  keyword?: string;
}

// ============================================================
// v3.4 全品牌套餐
// ============================================================

export interface BrandPlanSubcampaign {
  keyword: string;
  sov_pct: number;
  sov_label: string;
  articles: number;
  cost_yuan: number;
  competition_level: number;
  estimated_uprank_days: string;
}

export interface BrandPlanResponse {
  brand_plan_card: true;
  brand_id: number;
  user_id: number;
  subcampaigns: BrandPlanSubcampaign[];
  total_articles: number;
  raw_total_yuan: number;
  markup_factor: number;
  final_total_yuan: number;
  estimate_quoted_at: string;
  estimate_valid_until: string;
  estimate_validity_note: string;
  default_mode: CampaignMode;
  include_review_engine: boolean;
  note: string;
  requires_user_confirm: boolean;
}

export interface BrandPackage {
  id: number;
  user_id: number;
  brand_id: number;
  total_price_yuan: number;
  raw_cost_yuan: number;
  markup_factor: number;
  campaign_ids: number[];
  status: 'active' | 'paused' | 'completed';
  last_review_at: string | null;
  next_review_at: string | null;
  authorized_at: string;
  created_at: string;
}

export interface BrandDashboard {
  brand_id: number;
  brand_package: BrandPackage;
  subcampaigns: Campaign[];
  total_balance_yuan: number;
  delivered_articles: number;
  this_week_insights: string;
  brand_strategy: Record<string, unknown> | null;
}
