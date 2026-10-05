/**
 * 观测产品 API 客户端 —— 逐端点对齐 AI-3 真实路由/参数/DTO（见 AI3_RECONCILIATION.md）。
 * - 时间维度用 AI-3 真实 `granularity`（day|week|month），不发被后端忽略的 days。
 * - 洞察：创建用 `X-Request-Id` 请求头；轮询用品牌作用域路径 `/brands/{id}/insights/{job_id}`。
 * - 只暴露 AI-3 真实存在的端点；AI-2 治理写接口未交付 → 不在此声明，走 capability 层。
 */

import { httpTransport, type ObservationTransport, type RequestOptions } from './transport';
import type {
  AdminAggregateDiff,
  AdminModelShifts,
  AdminOverview,
  AdminPlatformHealth,
  BrandPlatforms,
  BrandSummary,
  BrandTrend,
  EvidenceDetail,
  Granularity,
  InsightJobStatus,
  OpportunitiesResponse,
  PublicBaselineResponse,
  QuestionsPage,
} from './types';

export function createObservationClient(transport: ObservationTransport = httpTransport) {
  const req = <T>(path: string, options?: RequestOptions) => transport.request<T>(path, options);
  const base = '/api/geo-observation';
  const admin = '/api/admin/geo-observation';
  const g = (granularity: Granularity) => `granularity=${granularity}`;

  return {
    // —— 服务商 / 客户私域 ——
    brandSummary: (brandId: number, granularity: Granularity, signal?: AbortSignal) =>
      req<BrandSummary>(`${base}/brands/${brandId}/summary?${g(granularity)}`, { signal }),

    brandTrend: (brandId: number, granularity: Granularity, signal?: AbortSignal) =>
      req<BrandTrend>(`${base}/brands/${brandId}/trend?${g(granularity)}`, { signal }),

    brandPlatforms: (brandId: number, granularity: Granularity, signal?: AbortSignal) =>
      req<BrandPlatforms>(`${base}/brands/${brandId}/platforms?${g(granularity)}`, { signal }),

    // 问题分页无时间维度（AI-3 只接 page/page_size）
    brandQuestions: (brandId: number, page: number, pageSize: number, signal?: AbortSignal) =>
      req<QuestionsPage>(`${base}/brands/${brandId}/questions?page=${page}&page_size=${pageSize}`, { signal }),

    evidence: (brandId: number, observationId: string, signal?: AbortSignal) =>
      req<EvidenceDetail>(`${base}/brands/${brandId}/evidence/${encodeURIComponent(observationId)}`, { signal }),

    opportunities: (brandId: number, granularity: Granularity, signal?: AbortSignal) =>
      req<OpportunitiesResponse>(`${base}/brands/${brandId}/opportunities?${g(granularity)}`, { signal }),

    // —— 语义洞察 ——
    // 创建：X-Request-Id 幂等键（同一意图复用）；无数据后端返回 200 + INSUFFICIENT_SAMPLES 信封（transport 会抛错）
    createInsight: (brandId: number, granularity: Granularity, requestId: string) =>
      req<InsightJobStatus>(`${base}/brands/${brandId}/insights?${g(granularity)}`, {
        method: 'POST',
        headers: { 'X-Request-Id': requestId },
      }),

    // 轮询：品牌作用域路径（跨租户 job_id 不可读）
    getInsight: (brandId: number, jobId: string, signal?: AbortSignal) =>
      req<InsightJobStatus>(`${base}/brands/${brandId}/insights/${encodeURIComponent(jobId)}`, { signal }),

    // —— 公共行业 ——
    industryBaseline: (industryKey: string, granularity: Granularity, signal?: AbortSignal) =>
      req<PublicBaselineResponse>(
        `${base}/industries/${encodeURIComponent(industryKey)}/baseline?${g(granularity)}`,
        { signal },
      ),

    // —— 管理员（AI-3 只读产品 API）——
    adminOverview: (signal?: AbortSignal) => req<AdminOverview>(`${admin}/overview`, { signal }),

    adminPlatformHealth: (signal?: AbortSignal) =>
      req<AdminPlatformHealth[]>(`${admin}/platform-health`, { signal }),

    adminModelShifts: (signal?: AbortSignal) => req<AdminModelShifts>(`${admin}/model-shifts`, { signal }),

    adminAggregateDiff: (signal?: AbortSignal) => req<AdminAggregateDiff>(`${admin}/aggregate-diff`, { signal }),

    adminContentOpportunities: (granularity: Granularity, signal?: AbortSignal) =>
      req<OpportunitiesResponse>(`${admin}/content-opportunities?${g(granularity)}`, { signal }),
  };
}

export type ObservationClient = ReturnType<typeof createObservationClient>;

/** 生产默认客户端（真实 HTTP）。 */
export const observationClient = createObservationClient();
