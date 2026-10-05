/**
 * GEO 调研监测后台 API client
 *
 * 对应后端 services/research_monitor 的 32 个 admin HTTP 接口
 * prefix: /api/admin/research-monitor/*
 *
 * 鉴权:复用主项目 authApi (axios 实例 · 自动注入 JWT)
 *
 * 5 大 group:
 *   - 行业管理(5)
 *   - 提示词管理(7)
 *   - 跑批管理(6)
 *   - 配置(4)
 *   - 审核(10)
 */
import { authApi } from '@/context/AuthContext';
import type { AxiosError } from 'axios';

// ==========================================
// 类型定义
// ==========================================

export interface Industry {
    id: number;
    name: string;
    slug: string;
    sort_order: number;
    active: boolean;
    created_at: string;
    updated_at: string;
    /** P14-v6: 该行业 active prompts 数 · 跑批 dialog 用于禁用无 prompts 行业 */
    active_prompt_count?: number;
    /** R2-1 (2026-07-05): 该行业最近一次调研落库时间 (ISO) · 从未跑为 null · 数据新鲜度提示用 */
    last_research_at?: string | null;
}

// R2 (2026-07-05): 行业别名归并记录 (admin 审核 · 用户行业原文 → 标准行业)
export interface IndustryAlias {
    id: number;
    normalized_alias: string;
    industry_id: number;
    industry_name: string | null;
    confidence: number | null;
    resolved_by: 'llm' | 'admin';
    reviewed_by: number | null;
    active: boolean;
    created_at: string | null;
    updated_at: string | null;
}

export interface Prompt {
    id: number;
    industry_id: number;
    prompt_text: string;
    sort_order: number;
    active: boolean;
    created_at: string;
    updated_at: string;
}

export interface ResearchRound {
    round_id: string;
    status: string;
    current_stage: string | null;
    triggered_by: string;
    started_at: string | null;
    finished_at: string | null;
    last_heartbeat_at: string | null;
    progress_json: Record<string, unknown>;
    summary_json: Record<string, unknown>;
    /** P14-v8: 从 snapshot_json.industries 解出 · 列表显示行业名用 */
    industries?: Array<{ id: number; name: string }>;
}

export interface RoundDetail extends ResearchRound {
    industries?: Array<{ id: number; name: string }>;
    items_total?: number;
    items_done?: number;
    cost_yuan?: number;
    error?: string | null;
    [key: string]: unknown;
}

export interface CostByItem {
    platform?: string;
    industry?: string;
    calls?: number;
    yuan?: number;
    [key: string]: unknown;
}

export interface PendingArticle {
    id: number;
    url: string;
    domain: string;
    title: string;
    primary_industry: string;
    cleaned_char_count: number;
    cleanliness_score: number | null;
    score_reason: string | null;
    domain_tier: string;
    is_duplicate: boolean;
    review_status: string;
    locked_by: string | null;
    locked_at: string | null;
    first_seen_round_id: string;
    fetched_at: string;
    total_citation_count: number;
}

export interface ArticleCitation {
    id?: number;
    platform?: string;
    prompt_text?: string;
    industry?: string;
    cited_at?: string;
    [key: string]: unknown;
}

export interface ArticleDetail {
    article: PendingArticle;
    cleaned_content: string | null;
    content_truncated?: boolean;
    citations: ArticleCitation[];
    review_history: ReviewLog[];
    oss_warning?: string;
}

export interface ReviewLog {
    id: number;
    article_id: number;
    action: string;
    prev_review_status: string;
    new_review_status: string;
    reason: string | null;
    note: string | null;
    operator_id: string;
    operator_name: string;
    operated_at: string;
    bulk_id: string | null;
    undone_by: string | null;
    undone_at: string | null;
}

export interface ConfigItem {
    key: string;
    value: unknown;
    description: string | null;
    updated_by: string | null;
    updated_at: string;
}

export interface BulkResult {
    ok: boolean;
    bulk_id: string;
    total: number;
    approved?: number;
    rejected?: number;
    skipped: number;
    locked_by_others: number;
    failed: number;
    failed_details: Array<{ article_id: number; reason: string }>;
}

// ==========================================
// 6 选 1 拒绝原因(老板拍板)
// ==========================================

export const REJECT_REASONS = {
    marketing: '营销文',
    reprint: '转载稿',
    outdated: '内容太老',
    off_topic: '主题不符',
    clean_fail: '清洗失败',
    other: '其他',
} as const;

export type RejectReason = keyof typeof REJECT_REASONS;

// ==========================================
// Phase 9 (2026-05-25) · 文章库新增类型 + 常量
// ==========================================

export const CONTENT_TYPE_LABEL = {
    video:    '视频/社媒',
    article:  '文章',
    encyc:    '百科',
    doc_tool: '文档/工具',
    ecom:     '电商',
    gov:      '政府/机构',
    other:    '其他/官网',
} as const;

export type ContentType = keyof typeof CONTENT_TYPE_LABEL;

export const ARTICLE_INTENT_LABEL = {
    ranking:     '榜单推荐型',
    tutorial:    '指南教程型',
    long_form:   '资讯/长文型',
    comparison:  '对比评测型',
    data_report: '数据报告型',
    policy:      '政策权威型',
    definition:  '定义百科型',
    faq:         'FAQ型',
} as const;

export type ArticleIntentType = keyof typeof ARTICLE_INTENT_LABEL;

export const REVIEW_STATUS_LABEL = {
    crawled:               '抓取中',          // 抓完未清洗
    in_library:            '已入库',          // Phase 9 默认可见
    auto_skipped:          '已跳过',          // 过短或重复
    imported_to_reference: '已归档',
    pending_review:        '待审 (旧)',       // 老数据
    approved:              '已审过 (旧)',
    rejected:              '已拒 (旧)',
} as const;

export type ReviewStatus = keyof typeof REVIEW_STATUS_LABEL;

export interface ArticleInLibrary {
    id: number;
    url: string;
    domain: string;
    title: string;
    primary_industry: string | null;
    content_type: ContentType | null;
    intent_type: ArticleIntentType | null;
    intent_confidence: number | null;
    intent_reason: string | null;
    intent_model: string | null;
    intent_classified_at: string | null;
    domain_tier: string;
    raw_char_count: number | null;
    cleaned_char_count: number | null;
    review_status: ReviewStatus;
    clean_status: string;
    is_duplicate: boolean;
    primary_article_id: number | null;
    total_citation_count: number;
    first_seen_round_id: string;
    fetched_at: string;
    expired: boolean | null;
    // P13 (2026-05-26): 后端 SELECT 加了 array_agg 子查询返回 · 该文章被哪些抓取平台引用过
    cited_by_platforms: string[];
}

// P13: 文章库批次条用 (顶部横向滚动条)
// P13-v2 (2026-05-26 老板): industries 从 string[] 改对象数组 · 漏斗式 批次→行业→文章
export interface ArticleBatchSummary {
    round_id: string;
    completed_at: string | null;
    articles_count: number;
    industries: Array<{ name: string; articles_count: number }>;
}

// P13-v3 (2026-05-26 老板): 按天聚合 · 替代 batch 视图 · 一天 union 多个 batch
export interface ArticleDaySummary {
    day: string;                                                        // YYYY-MM-DD
    latest_at: string | null;                                            // 该天最晚一篇文章 fetched_at
    articles_count: number;                                              // 该天 in_library 总数
    batch_ids: string[];                                                 // 该天所有 batch IDs (传给 /articles?batch_ids=)
    industries: Array<{ name: string; articles_count: number }>;         // 该天 union 的行业
}

export interface ArticleDetailFull {
    article: ArticleInLibrary & {
        url_hash?: string;
        oss_key_raw?: string;
        oss_key_cleaned?: string;
        content_hash?: string;
        clean_model?: string;
        clean_attempts?: number;
        last_cleaned_at?: string;
        reviewed_by?: string;
        reviewed_at?: string;
    };
    cleaned_content: string;
    raw_content: string;
    cleaned_truncated: boolean;
    citations: Array<{
        round_id: string;
        platform: string;
        prompt_id: number | null;
        prompt_text: string | null;
        rank: number | null;
        cited_at: string;
    }>;
    citations_count: number;
    is_in_reference: boolean;
    reference_id: number | null;
}

export interface ArticleListResponse {
    articles: ArticleInLibrary[];
    total: number;
    limit: number;
    offset: number;
    filters: Record<string, unknown>;
    // P13: 后端给每个 content_type 算了数量 · 前端 chips 显 (全部 706 / 文章 532 / 视频 52 ...)
    // key: content_type 字面值 ('article'/'video'/'__null__' 等) · 特殊 '__all__' 是总数
    counts_by_content_type: Record<string, number>;
}

export interface ArticleIntentDistributionItem {
    intent_type: ArticleIntentType;
    label: string;
    count: number;
    percent: number;
}

export interface ArticleIntentDistributionResponse {
    total: number;
    classified_total: number;
    counts: Record<string, number>;
    items: ArticleIntentDistributionItem[];
    unclassified_count: number;
}

export interface BulkArticleResult {
    deleted?: number;
    imported?: number;
    already_in?: number;
    skipped?: number;
    failed?: number;
    total: number;
    results?: Array<{
        article_id: number;
        ok: boolean;
        reference_id?: number;
        already_in?: boolean;
        error?: string;
    }>;
}

// ==========================================
// 内部:axios 错误统一转 Error(带 detail)
// ==========================================

interface ErrorResponseData {
    detail?: string;
    message?: string;
}

function unwrap<T>(promise: Promise<{ data: T }>): Promise<T> {
    return promise.then(r => r.data).catch((err: AxiosError<ErrorResponseData>) => {
        const status = err.response?.status;
        const data = err.response?.data;
        const detail = (data && typeof data === 'object')
            ? (data.detail || data.message)
            : undefined;
        const msg = detail || err.message || `HTTP ${status ?? '?'} 错误`;
        const e = new Error(typeof msg === 'string' ? msg : '请求失败');
        // 透传字段供调用方做更精细处理
        (e as Error & { status?: number; detail?: unknown }).status = status;
        (e as Error & { status?: number; detail?: unknown }).detail = detail;
        throw e;
    });
}

// ==========================================
// API base
// ==========================================

const BASE = '/api/admin/research-monitor';

// ==========================================
// API 方法
// ==========================================

export const researchMonitorApi = {
    // === 行业管理(5) ===
    listIndustries: (params?: { include_inactive?: boolean }) =>
        unwrap(authApi.get<{ industries: Industry[] }>(`${BASE}/industries`, { params })),

    // P13-v7 (2026-05-27 老板): slug + sort_order 后端自动处理 · 前端可不传
    createIndustry: (data: { name: string; active?: boolean }) =>
        unwrap(authApi.post<{ industry: Industry }>(`${BASE}/industries`, data)),

    updateIndustry: (id: number, data: Partial<Industry>) =>
        unwrap(authApi.put<{ industry: Industry }>(`${BASE}/industries/${id}`, data)),

    deleteIndustry: (id: number) =>
        unwrap(authApi.delete<{ deleted: boolean; industry_id: number }>(`${BASE}/industries/${id}`)),

    reorderIndustries: (items: { id: number; sort_order: number }[]) =>
        unwrap(authApi.post<{ updated: number }>(`${BASE}/industries/reorder`, { items })),

    // === 行业别名归并审核 (R2 · admin) ===
    listIndustryAliases: () =>
        unwrap(authApi.get<{ aliases: IndustryAlias[] }>(`${BASE}/industries/aliases`)),

    updateIndustryAlias: (alias_id: number, industry_id: number) =>
        unwrap(authApi.put<{ ok: boolean; alias_id: number; industry_id: number }>(
            `${BASE}/industries/aliases/${alias_id}`,
            { industry_id },
        )),

    // === 提示词管理(7) ===
    listPrompts: (industry_id: number, params?: { include_inactive?: boolean }) =>
        unwrap(authApi.get<{ industry_id: number; prompts: Prompt[]; count: number }>(
            `${BASE}/prompts`,
            { params: { industry_id, ...params } }
        )),

    createPrompt: (data: { industry_id: number; prompt_text: string; sort_order?: number; active?: boolean }) =>
        unwrap(authApi.post<{ prompt: Prompt }>(`${BASE}/prompts`, data)),

    updatePrompt: (id: number, data: Partial<Prompt>) =>
        unwrap(authApi.put<{ prompt: Prompt }>(`${BASE}/prompts/${id}`, data)),

    deletePrompt: (id: number) =>
        unwrap(authApi.delete<{ deleted: boolean; prompt_id: number }>(`${BASE}/prompts/${id}`)),

    reorderPrompts: (industry_id: number, items: { id: number; sort_order: number }[]) =>
        unwrap(authApi.post<{ updated: number }>(`${BASE}/prompts/reorder`, { industry_id, items })),

    bulkCreatePrompts: (industry_id: number, prompts: string[]) =>
        unwrap(authApi.post<{ created: number; industry_id: number; prompts: Prompt[] }>(
            `${BASE}/prompts/bulk-create`,
            { industry_id, prompts }
        )),

    togglePrompt: (id: number) =>
        unwrap(authApi.post<{ prompt_id: number; active: boolean }>(`${BASE}/prompts/${id}/toggle`)),

    // === 跑批管理(6) ===
    listRounds: (params?: { status?: string; triggered_by?: string; limit?: number; offset?: number }) =>
        unwrap(authApi.get<{ rounds: ResearchRound[]; total: number; limit: number; offset: number }>(
            `${BASE}/rounds`,
            { params }
        )),

    getRoundDetail: (round_id: string) =>
        unwrap(authApi.get<RoundDetail>(`${BASE}/rounds/${round_id}`)),

    manualTriggerRound: (data?: { industry_ids?: number[]; note?: string }) =>
        unwrap(authApi.post<{ round_id: string; status: string; message: string }>(
            `${BASE}/rounds/manual-trigger`,
            data ?? {}
        )),

    cancelRound: (round_id: string) =>
        unwrap(authApi.post<{ round_id: string; status: string; cancelled_from: string; message: string }>(
            `${BASE}/rounds/${round_id}/cancel`
        )),

    resumeRound: (round_id: string) =>
        unwrap(authApi.post<{ round_id: string; status: string; resumed_from: string }>(
            `${BASE}/rounds/${round_id}/resume`
        )),

    getRoundCost: (round_id: string) =>
        unwrap(authApi.get<{ round_id: string; total_yuan: number; by_item: CostByItem[]; month_budget_remaining: number }>(
            `${BASE}/rounds/${round_id}/cost`
        )),

    // === 配置(4) ===
    listConfig: () =>
        unwrap(authApi.get<{ configs: ConfigItem[] }>(`${BASE}/config`)),

    getConfig: (key: string) =>
        unwrap(authApi.get<ConfigItem>(`${BASE}/config/${encodeURIComponent(key)}`)),

    updateConfig: (key: string, value: unknown, note?: string) =>
        unwrap(authApi.put<{ key: string; value: unknown; old_value: unknown; updated_by: string; updated_at: string }>(
            `${BASE}/config/${encodeURIComponent(key)}`,
            { value, note }
        )),

    resetConfig: (confirm_code: string, keys?: string[]) =>
        unwrap(authApi.post<{ reset_count: number; keys: string[] }>(
            `${BASE}/config/reset`,
            { confirm_code, keys }
        )),

    // === 审核(10) Phase 9 (2026-05-25) 整段删除 ===
    // 原因: 删审核工作流 · 后端审核 router / review_state 已物理删
    // 文章库 API 见 articlesApi (P06 · 7 endpoints)

    // === Phase 9 · 文章库 (7) ===
    listArticles: (params?: {
        batch_id?: string;
        batch_ids?: string;        // P13-v3: 多值 (逗号分隔) · 按天选了之后传该天所有 batch
        industry?: string;
        content_type?: ContentType;
        domain_tier?: string;
        review_status?: string;     // 默认 in_library · 可逗号分隔多值
        min_chars?: number;
        max_chars?: number;
        keyword?: string;
        sort?: 'citations_desc' | 'chars_desc' | 'fetched_desc' | 'fetched_asc';
        limit?: number;
        offset?: number;
    }) =>
        unwrap(authApi.get<ArticleListResponse>(`${BASE}/articles`, { params })),

    getArticleIntentDistribution: (params?: {
        batch_id?: string;
        batch_ids?: string;
        industry?: string;
        content_type?: ContentType;
        domain_tier?: string;
        review_status?: string;
        min_chars?: number;
        max_chars?: number;
        keyword?: string;
    }) =>
        unwrap(authApi.get<ArticleIntentDistributionResponse>(`${BASE}/articles/intent-distribution`, { params })),

    // P13: 文章库顶部横向批次条 · 拉最近 N 个批次的 summary (保留兼容)
    listArticleBatches: (params?: { limit?: number }) =>
        unwrap(authApi.get<{ batches: ArticleBatchSummary[]; total: number }>(
            `${BASE}/articles/batches`,
            { params },
        )),

    // P13-v3: 按天聚合 · 老板拍板顶部条用 (替代 batches 视图角色)
    listArticleDays: (params?: { limit?: number }) =>
        unwrap(authApi.get<{ days: ArticleDaySummary[]; total: number }>(
            `${BASE}/articles/days`,
            { params },
        )),

    getArticleDetail: (article_id: number) =>
        unwrap(authApi.get<ArticleDetailFull>(`${BASE}/articles/${article_id}`)),

    editArticle: (article_id: number, data: { cleaned_content: string; edit_note?: string }) =>
        unwrap(authApi.put<{ ok: boolean; article_id: number; oss_key_cleaned: string; cleaned_char_count: number }>(
            `${BASE}/articles/${article_id}`,
            data,
        )),

    updateArticleIntent: (article_id: number, data: { intent_type: ArticleIntentType | null; note?: string }) =>
        unwrap(authApi.put<{ ok: boolean; article_id: number; intent_type: ArticleIntentType | null }>(
            `${BASE}/articles/${article_id}/intent`,
            data,
        )),

    deleteArticle: (article_id: number) =>
        unwrap(authApi.delete<{ ok: boolean; deleted: boolean; article_id: number }>(
            `${BASE}/articles/${article_id}`,
        )),
    bulkDeleteArticles: (article_ids: number[]) =>
        unwrap(authApi.post<BulkArticleResult>(`${BASE}/articles/bulk-delete`, { article_ids })),

    // ==========================================
    // P14 · 引用明细 (admin-only · 不暴露给代理)
    // ==========================================

    listCitationIndustries: () =>
        unwrap(authApi.get<CitationIndustriesResp>(`${BASE}/citations/industries-summary`)),

    listCitationQueries: (params: {
        industry: string;
        q?: string;
        engine?: CitationEngine;
        batch_id?: string;
        limit?: number;
        offset?: number;
    }) => unwrap(authApi.get<CitationQueriesResp>(`${BASE}/citations/queries`, { params })),

    getCitationQueryDetail: (params: {
        industry: string;
        query: string;
        batch_id?: string;
        answers_per_engine?: number;
        engine?: CitationEngine;
        engine_offset?: number;
    }) => unwrap(authApi.get<CitationQueryDetailResp>(`${BASE}/citations/query-detail`, { params })),

    // P14-v10 · cron 状态
    getCronStatus: () => unwrap(authApi.get<CronStatus>(`${BASE}/rounds/cron-status`)),

    // P15.1 · 运行监控聚合
    getRoundLiveStatus: (round_id: string) =>
        unwrap(authApi.get<LiveStatus>(`${BASE}/rounds/${encodeURIComponent(round_id)}/live-status`)),
};

// ==========================================
// P14 · 引用明细 类型 (跟后端 schema 一一对齐)
// ==========================================

export type CitationEngine = '豆包' | 'Kimi' | 'DeepSeek' | '千问';

export const CITATION_ENGINES: CitationEngine[] = ['豆包', 'Kimi', 'DeepSeek', '千问'];

export interface CitationIndustrySummary {
    industry: string;
    query_count: number;
    engine_count: number;       // 归一后 0~4
    citation_count: number;
    last_cited_at: string | null;
}

export interface CitationIndustriesResp {
    industries: CitationIndustrySummary[];
    total: number;
}

export interface CitationQuerySummary {
    query: string;
    citation_count: number;
    engine_count: number;
    last_cited_at: string | null;
    engines: Partial<Record<CitationEngine, number>>;  // 归一名 → 计数
}

export interface CitationQueriesResp {
    industry: string;
    queries: CitationQuerySummary[];
    total: number;
    limit: number;
    offset: number;
}

export interface CitationCite {
    raw_id: number;
    platform: string | null;
    position: number | null;
    url: string;
    title: string | null;
    excerpt: string | null;
    article_id: number | null;  // P14 v2 · LEFT JOIN article_citations · 有则可跳文章库
}

export interface CitationAnswer {
    answer_md5: string;
    answer_text: string;
    batch_id: string | null;
    raw_engine: string | null;  // 原始 engine 字段 · tooltip 排查脏数据用
    created_at: string | null;
    cite_count: number;
    cites: CitationCite[];
}

export interface CitationEngineGroup {
    engine: CitationEngine;
    total_answers: number;
    answers: CitationAnswer[];
    has_more: boolean;
    offset: number;
    limit: number;
}

export interface CitationQueryDetailResp {
    industry: string;
    query: string;
    engines: CitationEngineGroup[];
}

// P15.1: 运行监控 live-status
export interface LiveStatusPlatform {
    platform: string;
    label: string;
    status: 'success' | 'failed' | 'partial' | 'running' | 'unknown';
    success: number;
    failed: number;
    pending: number;
    total: number;
    citations: number;
    latest_error: string | null;
    can_retry: boolean;
}

export interface LiveStatusStage {
    stage: string;
    label: string;
    status: 'done' | 'running' | 'pending';
    done: number;
    total: number;
}

export interface LiveStatusError {
    platform: string;
    label: string;
    count: number;
    sample: string;
}

// P14.1 C3+: stage 3 全路径 reasons + 失败样本 (running 实时 / 完成态从 summary_json.stage_3 fallback)
export interface LiveStatusStage3ErrorSample {
    article_id?: number | null;
    title?: string;
    url: string;
    reason: string;          // skipped_jina_failed / skipped_oss_failed / failed_unknown
    error_type: string;      // timeout / connect / rate_limit / auth / server_5xx / other / unknown
    error_message: string;
}

export interface LiveStatusStage3Summary {
    total_urls?: number | null;
    processed?: number | null;
    jina_requests?: number | null;
    crawled_new?: number;
    // P14.2 C1: 跨行业复用 (本行业首次引用别行业已抓的 URL · 复制内容字段不重 Jina)
    crawled_dup?: number;
    reused_existing?: number;
    skipped_short?: number;
    skipped_jina_failed?: number;
    skipped_oss_failed?: number;
    failed_unknown?: number;
    error_samples?: LiveStatusStage3ErrorSample[];
}

export interface LiveStatusIntentSummary {
    classified: number;
    failed: number;
    total: number;
    model?: string | null;
    error_samples?: LiveStatusStage3ErrorSample[];
}

export interface LiveStatus {
    round: {
        round_id: string;
        status: string;
        industry_names: string[];
        current_stage: string | null;
        stage_label: string;
        started_at: string | null;
        finished_at: string | null;
        last_heartbeat_at: string | null;
        heartbeat_age_seconds: number | null;
        error_message: string | null;
    };
    overall: { percent: number; label: string };
    platforms: LiveStatusPlatform[];
    stage_progress: LiveStatusStage[];
    errors: LiveStatusError[];
    articles_done: number;
    // P14.1 C3+: running 时来自 progress_json · 完成态从 summary_json.stage_3 兜底
    stage_3_summary: LiveStatusStage3Summary | null;
    intent_summary: LiveStatusIntentSummary | null;
}

// P14-v10: 自动跑批 cron 状态 (GET /rounds/cron-status)
export interface CronStatus {
    enabled: boolean;
    days: string;          // '1,16' 等
    hour: number;          // 0-23
    next_run_at: string | null;
    timezone: string;
}

export default researchMonitorApi;
