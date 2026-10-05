/**
 * 后端公开报告 JSON DTO(当前真实契约,GET /api/public/report/{id}?st=...)
 *
 * 事实来源:api/share_api.py:752-924 + tests/test_public_report_web_backend.py
 * 统一候选已由后端下发 presentation;映射层(mapDto.ts)仍对所有字段做防御性校验,
 * 后端漂移时 fail-closed 到 "—"/unavailable,绝不自行修正业务值。
 */

/** 成功响应的 report 对象(后端已脱敏,内部字段不下发) */
export interface PublicReportDto {
    readonly whitelabel?: {
        readonly company_name?: string | null;
        readonly product_name?: string | null;
        readonly logo_url?: string | null;
        readonly slogan?: string | null;
        readonly brand_color?: string | null;
    } | null;
    readonly branding_status?: string | null;
    readonly brand_name?: string | null;
    readonly score?: unknown; // int 0-100 | null;异常形态由映射层 fail-closed
    readonly level?: unknown; // string | null
    readonly content?: string | null;
    readonly html_content?: string | null;
    readonly has_html?: boolean;
    readonly created_at?: string | null;
    readonly keyword_count?: unknown;
    readonly report_version?: string | null;
    readonly audience?: string | null;
    readonly data_completeness_score?: unknown;
    readonly data_completeness_breakdown?: {
        readonly level?: string | null;
        readonly missing_summary?: string | null;
    } | null;
    readonly report_v2_generated_at?: string | null;
    /** V2 结构化展示载荷；缺失或非法时各区块独立 fail-closed。 */
    readonly presentation?: unknown;
}

/** 成功态 */
export interface PublicReportSuccessBody {
    readonly status: 'success';
    readonly report: PublicReportDto;
}

/** not_ready(客户报告尚未就绪;HTTP 200) */
export interface PublicReportNotReadyBody {
    readonly status: 'not_ready';
    readonly not_ready?: boolean;
    readonly code?: string;
    readonly message?: string;
    readonly detail?: { readonly code?: string; readonly message?: string } | string;
}

/** pending(报告生成中;HTTP 200) */
export interface PublicReportPendingBody {
    readonly status: 'pending';
    readonly pending?: boolean;
    readonly message?: string;
}

export type PublicReportResponseBody =
    | PublicReportSuccessBody
    | PublicReportNotReadyBody
    | PublicReportPendingBody
    | { readonly detail?: unknown };
