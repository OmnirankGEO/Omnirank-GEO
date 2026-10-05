/**
 * 观测产品 API 的网络层 mock（仅测试用，绝不进任何生产 bundle）。
 *
 * section 七 纪律：
 *  - 只模拟 AI-3 **真实存在**的端点（路径/参数/DTO 对齐 api/geo_observation_product_api.py）。
 *  - 冻结 case 直接读 frontend_race_fixture_v1.json（只读，不改）；AI-3 有但 fixture 无的端点
 *    （trend/industry baseline/model-shifts）用**合成契约数据**，字段严格按 AI-3 DTO。
 *  - **未声明/不存在端点一律 501**（不再给未知 /api 返回空 200），从而任何误调不存在接口的测试转红。
 *  - AI-2 治理写接口（review-queue/action/platform PATCH）不 mock —— 前端 capability-pending 不调用它们。
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import type { Page, Route } from 'playwright/test';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '../../..');
const FIXTURE_PATH = path.join(
  repoRoot,
  'docs/AI-CONTEXT/GEO_OBSERVATION_FLYWHEEL_VNEXT_2026-07-17/contracts/frontend_race_fixture_v1.json',
);

export const fixture = JSON.parse(readFileSync(FIXTURE_PATH, 'utf8')) as any;
const C = fixture.cases;
const ERR = fixture.error_cases;

export type Scenario =
  | 'happy'
  | 'summary_503'
  | 'summary_403'
  | 'summary_409' // 演示冲突状态处理器（AI-2 写接入后由真实写触发）
  | 'summary_423' // 演示环境覆盖状态处理器
  | 'summary_empty' // valid_observations=0 + 全 null 比例 + insufficient
  | 'summary_missing_fields' // [#87] 200 但 summary / outcomes / next_actions 整块缺席 —— 崩整页的那种
  | 'insight_fail'
  | 'industry_insufficient';

export interface MockController {
  scenario: Scenario;
  counts: { insightCreate: number; insightPoll: number; unknownApi: number };
  lastCreateHeaders: Record<string, string>;
}

/** 结构化错误信封（AI-3 用 HTTPException detail={code,message}）。 */
function errEnvelope(route: Route, status: number, err: { code: string; message: string; retryable?: boolean }) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ detail: err }) });
}
function json(route: Route, status: number, body: unknown) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

// —— AI-3 有但冻结 fixture 未含的端点：合成契约数据（字段严格按 AI-3 DTO） ——
function synthTrend(granularity: string) {
  const days = ['2026-07-01', '2026-07-05', '2026-07-09', '2026-07-13', '2026-07-15', '2026-07-17'];
  // 两个非相邻断点（7/09 与 7/15），验证多断点分段：每个断点都切一段、都画参考线
  const SHIFTS = new Set(['2026-07-09', '2026-07-15']);
  const points = days.map((d, i) => ({
    bucket_start: d,
    bucket_end: d,
    valid_observations: 18 + i * 2,
    presence_rate_bps: 6200 + i * 120,
    explicit_recommendation_rate_bps: 2800 + i * 90,
    stability_status: SHIFTS.has(d) ? 'shifted' : 'watch',
    model_shift_marker: SHIFTS.has(d),
    confirmed_change: false,
  }));
  return {
    brand: C.brand_summary.response.brand,
    window: { label: granularity === 'week' ? '本周' : granularity === 'month' ? '本月' : '当日', start: days[0], end: days[days.length - 1] },
    metric_version: C.brand_summary.response.metric_version,
    granularity,
    points,
    comparison_note: '部分区间模型升级，跨升级点不宜直接比较。',
  };
}
function synthBaseline(industryKey: string, granularity: string, insufficient: boolean) {
  const scope = `行业「${industryKey}」· ${granularity}`;
  if (insufficient) {
    return { status: 'insufficient_samples', industry_key: industryKey, sample_scope: scope, message: '样本不足，暂不下结论。', baseline: null };
  }
  return {
    status: 'ok',
    industry_key: industryKey,
    sample_scope: scope,
    message: null,
    baseline: {
      industry_key: industryKey,
      window: { label: '当日', start: '2026-07-17', end: '2026-07-17' },
      metric_version: C.brand_summary.response.metric_version,
      valid_observations: 240,
      presence_rate_bps: 5400,
      explicit_recommendation_rate_bps: 2100,
      conditional_recommendation_rate_bps: 1600,
      criteria_only_rate_bps: 900,
      refusal_no_evidence_rate_bps: 1200,
      refusal_risk_rate_bps: 300,
      citation_rate_bps: 4200,
      evidence_coverage_rate_bps: 3800,
      stability_status: 'stable',
      sample_scope: scope,
    },
  };
}
function synthModelShifts() {
  // 严格按 AI-3 AdminModelShiftDTO：含必填 bucket_granularity。
  return {
    items: [
      { industry_key: 'elevator_service', platform_key: 'yuanbao', model_revision: 'hy3-2026w28', bucket_granularity: 'day', bucket_start: '2026-07-15', model_shift_index_bps: 2600, stability_status: 'shifted' },
      { industry_key: 'elevator_service', platform_key: 'deepseek', model_revision: 'v3-0714', bucket_granularity: 'day', bucket_start: '2026-07-14', model_shift_index_bps: 2100, stability_status: 'shifted' },
    ],
  };
}
// 证据带引用 + 带敏感采集 note：用于验证前端只渲染域名（无外链）且隐藏 note（section 六）。
// 严格按 AI-3 EvidenceDetailDTO：citations={domain,source_type,rank}；channel_disclosure={platform,note}。
function synthEvidence() {
  const e = C.evidence_detail.response;
  return {
    ...e,
    citations: [
      { domain: 'elevatorworld.com', source_type: 'citation', rank: 1 },
      { domain: 'sgs.gov.cn', source_type: 'source', rank: 2 },
    ],
    channel_disclosure: { platform: 'DeepSeek', note: '本条结果由 DeepSeek 模型配合秘塔检索代理获得' },
  };
}

/**
 * [#87] 200 且信封合法,但 `summary` / `outcomes` / `next_actions` **整块缺席**。
 * 只保留 brand/window 这类外层字段 —— 足够让 `!data` 判空为假,从而走进渲染分支。
 * 这正是"有 data ≠ data 里每个字段都在"的那一格。
 */
function missingFieldsSummary() {
  const s = C.brand_summary.response;
  return { brand: s.brand, window: s.window, data_updated_at: null, metric_version: s.metric_version };
}

function emptySummary() {
  const s = C.brand_summary.response;
  return {
    brand: s.brand,
    window: s.window,
    data_updated_at: null,
    metric_version: s.metric_version,
    summary: {
      valid_observations: 0,
      presence_rate_bps: null, explicit_recommendation_rate_bps: null, conditional_recommendation_rate_bps: null,
      candidate_rate_bps: null, criteria_only_rate_bps: null, refusal_no_evidence_rate_bps: null,
      refusal_risk_rate_bps: null, not_mentioned_rate_bps: null, citation_rate_bps: null,
      evidence_coverage_rate_bps: null, share_of_voice_bps: null,
      stability_status: 'insufficient', stability_explanation: '样本不足，暂不下结论。',
    },
    comparison: { presence_change_bps: null, recommendation_change_bps: null, comparison_allowed: false, reason: 'insufficient_history' },
    outcomes: [],
    next_actions: [{ action: 'keep_observing', title: '继续观察', reason: '尚无足够观测数据，持续采集后再判断', requires_confirmation: false, may_charge: false }],
  };
}

export function collectConsoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    const t = m.text();
    if (/Failed to load resource/i.test(t)) return;
    errors.push(t);
  });
  page.on('pageerror', (e) => errors.push(String(e)));
  return errors;
}

export async function installObservationMock(page: Page, initial: Scenario = 'happy'): Promise<MockController> {
  const ctrl: MockController = {
    scenario: initial,
    counts: { insightCreate: 0, insightPoll: 0, unknownApi: 0 },
    lastCreateHeaders: {},
  };

  await page.route('**/api/**', async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname;
    const m = req.method();

    // —— 服务商私域 ——
    if (/\/api\/geo-observation\/brands\/\d+\/summary$/.test(p)) {
      if (ctrl.scenario === 'summary_503') return errEnvelope(route, 503, ERR['503']);
      if (ctrl.scenario === 'summary_403') return errEnvelope(route, 403, ERR['403']);
      if (ctrl.scenario === 'summary_409') return errEnvelope(route, 409, ERR['409']);
      if (ctrl.scenario === 'summary_423') return errEnvelope(route, 423, ERR['423']);
      if (ctrl.scenario === 'summary_empty') return json(route, 200, emptySummary());
      // [#87] 接口 200 但少给字段 —— 消费方裸取会抛 TypeError,整份诊断报告页进错误边界。
      //       这不是假想:C 在 #63 域内实测两轮判据红都是它崩的。
      if (ctrl.scenario === 'summary_missing_fields') return json(route, 200, missingFieldsSummary());
      return json(route, 200, C.brand_summary.response);
    }
    const empty = ctrl.scenario === 'summary_empty';
    if (/\/api\/geo-observation\/brands\/\d+\/trend$/.test(p)) {
      if (empty) {
        const t = synthTrend(url.searchParams.get('granularity') || 'day');
        return json(route, 200, { ...t, points: [], comparison_note: null });
      }
      return json(route, 200, synthTrend(url.searchParams.get('granularity') || 'day'));
    }
    if (/\/api\/geo-observation\/brands\/\d+\/platforms$/.test(p)) {
      if (empty) return json(route, 200, { items: [], historical_platforms: [] });
      return json(route, 200, C.platforms.response);
    }
    if (/\/api\/geo-observation\/brands\/\d+\/questions$/.test(p)) {
      if (empty) return json(route, 200, { page: 1, page_size: 20, total: 0, items: [] });
      return json(route, 200, C.questions.response);
    }
    if (/\/api\/geo-observation\/brands\/\d+\/evidence\//.test(p)) {
      return json(route, 200, synthEvidence());
    }
    if (/\/api\/geo-observation\/brands\/\d+\/opportunities$/.test(p)) {
      if (empty) return json(route, 200, { items: [] });
      return json(route, 200, C.opportunities.response);
    }

    // 洞察：创建（POST /insights）与轮询（GET /insights/{job_id}，品牌作用域）
    if (/\/api\/geo-observation\/brands\/\d+\/insights$/.test(p) && m === 'POST') {
      ctrl.counts.insightCreate += 1;
      ctrl.lastCreateHeaders = req.headers();
      if (ctrl.scenario === 'insight_fail') return errEnvelope(route, 503, C.semantic_insight_job.failed.response);
      return json(route, 202, C.semantic_insight_job.create.response);
    }
    if (/\/api\/geo-observation\/brands\/\d+\/insights\/[^/]+$/.test(p) && m === 'GET') {
      ctrl.counts.insightPoll += 1;
      if (ctrl.counts.insightPoll <= 1) return json(route, 200, C.semantic_insight_job.running.response);
      return json(route, 200, C.semantic_insight_job.completed.response);
    }

    // —— 公共行业基线 ——
    if (/\/api\/geo-observation\/industries\/[^/]+\/baseline$/.test(p)) {
      const key = decodeURIComponent(p.match(/industries\/([^/]+)\/baseline/)?.[1] || '');
      const gran = url.searchParams.get('granularity') || 'day';
      return json(route, 200, synthBaseline(key, gran, ctrl.scenario === 'industry_insufficient'));
    }
    if (/\/api\/geo-observation\/industries\/[^/]+\/content-opportunities$/.test(p)) {
      return json(route, 200, C.opportunities.response);
    }

    // —— 管理员 ——
    if (/\/api\/admin\/geo-observation\/overview$/.test(p)) {
      return json(route, 200, C.admin_overview.response);
    }
    if (/\/api\/admin\/geo-observation\/model-shifts$/.test(p)) {
      return json(route, 200, synthModelShifts());
    }
    if (/\/api\/admin\/geo-observation\/content-opportunities$/.test(p)) {
      return json(route, 200, C.opportunities.response);
    }

    // 未声明/不存在端点：501（不给未知 API 返回空 200；任何误调都会转红）
    ctrl.counts.unknownApi += 1;
    return errEnvelope(route, 501, { code: 'NOT_IMPLEMENTED', message: `mock 未声明该端点：${m} ${p}` });
  });

  return ctrl;
}
