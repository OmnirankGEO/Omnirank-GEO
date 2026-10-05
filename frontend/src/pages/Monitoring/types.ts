/**
 * Monitoring page shared type definitions.
 */

export interface Client {
    quote_id: number;
    brand_id?: number;
    brand_name: string;
    industry: string;
    tier?: string;
    tier_name?: string;
    keyword_count: number;
    status: string;
    service_days?: number;
    service_start_date?: string;
}

export interface Keyword {
    id: number;
    keyword: string;
    target_brand: string;
    source: string;
    detection_rate?: number;
    rate_change?: number;
    last_tested?: string;
    // [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-8] 加两个「停了」态。
    //   病史:lifecycle 纯按历史推导、不看开关 → 关掉的词仍显示「监测中」,
    //   于是「监测中」和「已关闭」并排出现在同一行。后端已能返回这两个值,
    //   前端类型没跟上时 tsc 会把徽章分支判成恒假(TS2367)——真实 build 才拦得住。
    lifecycle?: 'pending' | 'deploying' | 'monitoring' | 'stopped_detected' | 'stopped_deploying';
    first_detected_date?: string;
    total_tests?: number;
    window_tests?: number;
    target_rate?: number;
    compliant_days?: number;
    remaining_days?: number | null;
    // [2026-06-04 纯履约口径] 还需达标天数 = service_days − compliant_days(后端注入)
    remaining_compliant?: number | null;
    // [Deploy-CTO 2026-05-30] 服务期日历剩余天数(合同起 + service_days − today · 后端 api/monitoring_api.py 注入)
    // 倒计时口径 = 日历剩余(老板 P0):remaining_days 是履约口径(service_days−已达标)易混 · 倒计时改用此字段
    service_remaining_days_natural?: number | null;
    // [WJ-24 2026-06-01 老板 A 双口径] 合同自然日历过期态(后端 monitoring_api 注入)· 配套主显字段
    service_expired?: boolean;
    service_overdue_days?: number | null;
    service_days?: number;
    is_compliant?: boolean;
    // CTO-15.23 2026-05-10 · A 方案 · 稳定达标(最近 7 天 ≥ 5 天 is_compliant=TRUE)
    // is_compliant 是单点判定 · is_stable 是连续性判定 · 区分"刚达标 vs 稳定达标"
    is_stable?: boolean;
    compliance_progress?: number;
    effective_rate?: number;
    is_today_dropped?: boolean;
    is_core?: boolean;
    cluster_id?: number | null;
    cluster_name?: string;
    // CTO-15.23 2026-05-09 · 代理自助监测订阅
    is_monitored?: boolean;
    monitoring_subscription_id?: number | null;
    monitoring_status?: string;
}

export interface Publication {
    id: number | string;
    source: 'mhz' | 'manual';
    platform_name: string;
    platform_url: string;
    article_title: string;
    publish_date: string;
    status: number;
    status_label: string;
    can_refund: boolean;
    refund?: {
        status: string;
        reason: string;
        created_at: string | null;
        admin_note: string;
    } | null;
}

export interface TokenInfo {
    token: string;
    expires_at: string;
    is_active: boolean;
}

export interface TrendData {
    date: string;
    rate: number | null;
}

export interface OperationLog {
    id: number;
    action: string;
    target_type: string;
    target_id: string;
    operator_id: string;
    details: string;
    created_at: string;
}

export interface MonitoringResult {
    id: number;
    keyword_id: number;
    keyword: string;
    platform: string;
    is_detected: boolean;
    rank_position: number | null;
    response_snippet: string;
    tested_at: string;
}

export interface ProgressLog {
    time: string;
    keyword: string;
    platform: string;
    status?: 'success' | 'error';
    error?: string;
    errorCode?: string;
    detected: boolean;
    snippet: string;
    fullResponse: string;
    expanded?: boolean;
    cellId?: number;
    cellState?: MonitoringCellState;
    planHash?: string;
    // [M-1 ①] 人工确认后的行:回写"已确认 · 已计入"标记
    identityConfirmed?: boolean;
}

export type MonitoringCellState =
    | 'queued'
    | 'running'
    | 'succeeded'
    | 'failed'
    | 'pending_identity'
    | 'unavailable'
    | 'pending_provider_confirmation';

export interface MonitoringCell {
    id: number;
    task_id: number;
    keyword_id: number;
    keyword_source: string;
    keyword?: string;
    keyword_snapshot?: string;
    platform: 'dashscope' | 'deepseek' | 'kimi' | 'doubao';
    is_planned: boolean;
    state: MonitoringCellState;
    plan_hash: string;
    fulfillment_state?: 'reserved' | 'covered' | 'admin_covered' | 'coverage_unknown' | 'released';
    attempt_count?: number;
    retry_count?: number;
    retry_coverage?: 'included' | 'requires_new_charge' | 'unknown';
    retry_max_attempts?: number;
    result_id?: number | null;
    error_code?: string | null;
    error_message?: string | null;
}
