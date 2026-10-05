/**
 * 机器语言合同检查 + 文案导出。
 * - 断言 OUTCOME_LABELS / METRIC_LABELS / STABILITY_LABELS 与冻结
 *   frontend_copy_and_race_v1.json 逐字一致。
 * - 导出全部用户可见字符串到 evidence/copy-export.json（文案验收物）。
 * - 扫描导出字符串无工程黑话与营销空话。
 * 纯 Node 断言，仅在 d1440 project 跑一次。
 */

import { test, expect } from 'playwright/test';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { OUTCOME_LABELS, METRIC_LABELS, STABILITY_LABELS, PAGE } from '../../src/features/geoObservation/copy';
import { FORBIDDEN_USER_TERMS, FORBIDDEN_MARKETING_TERMS } from './forbidden';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '../../..');
const contract = JSON.parse(
  readFileSync(
    path.join(repoRoot, 'docs/AI-CONTEXT/GEO_OBSERVATION_FLYWHEEL_VNEXT_2026-07-17/contracts/frontend_copy_and_race_v1.json'),
    'utf8',
  ),
);
const EVIDENCE = path.join(repoRoot, 'docs/AI-CONTEXT/GEO_OBSERVATION_FRONTEND_A/evidence');


test('十类结果标签逐字匹配冻结合同', () => {
  for (const [k, v] of Object.entries(contract.outcome_labels)) {
    expect(OUTCOME_LABELS[k as keyof typeof OUTCOME_LABELS], `outcome ${k}`).toBe(v);
  }
});

test('指标与稳定度业务标签匹配冻结合同', () => {
  const m = contract.metric_labels;
  expect(METRIC_LABELS.presence_rate).toBe(m.presence_rate_bps);
  expect(METRIC_LABELS.explicit_recommendation_rate).toBe(m.explicit_recommendation_rate_bps);
  expect(METRIC_LABELS.conditional_recommendation_rate).toBe(m.conditional_recommendation_rate_bps);
  expect(METRIC_LABELS.candidate_rate).toBe(m.candidate_rate_bps);
  expect(METRIC_LABELS.criteria_only_rate).toBe(m.criteria_only_rate_bps);
  expect(METRIC_LABELS.refusal_no_evidence_rate).toBe(m.refusal_no_evidence_rate_bps);
  expect(METRIC_LABELS.refusal_risk_rate).toBe(m.refusal_risk_rate_bps);
  expect(METRIC_LABELS.share_of_voice).toBe(m.share_of_voice_bps);
  expect(METRIC_LABELS.citation_rate).toBe(m.citation_rate_bps);
  expect(METRIC_LABELS.evidence_coverage_rate).toBe(m.evidence_coverage_rate_bps);
  expect(METRIC_LABELS.valid_observations).toBe(m.valid_observations);
  expect(METRIC_LABELS.data_updated_at).toBe(m.input_watermark);
  expect(STABILITY_LABELS.insufficient).toBe(m['stability_status=insufficient']);
  expect(STABILITY_LABELS.shifted).toBe(m['stability_status=shifted']);
});

test('导出全部用户可见字符串且无黑话/空话', () => {
  const strings: string[] = [];
  const collect = (obj: unknown) => {
    if (typeof obj === 'string') strings.push(obj);
    else if (Array.isArray(obj)) obj.forEach(collect);
    else if (obj && typeof obj === 'object') Object.values(obj).forEach(collect);
  };
  collect(OUTCOME_LABELS);
  collect(METRIC_LABELS);
  collect(STABILITY_LABELS);
  collect(PAGE);

  mkdirSync(EVIDENCE, { recursive: true });
  writeFileSync(
    path.join(EVIDENCE, 'copy-export.json'),
    JSON.stringify({ count: strings.length, strings: [...new Set(strings)].sort() }, null, 2),
    'utf8',
  );

  const joined = strings.join('\n');
  for (const term of FORBIDDEN_USER_TERMS) {
    // 逐字符/词命中即失败（用户端不应出现工程黑话）
    expect(joined.toLowerCase(), `用户文案含黑话 "${term}"`).not.toContain(term.toLowerCase());
  }
  for (const term of FORBIDDEN_MARKETING_TERMS) {
    expect(joined, `用户文案含空话 "${term}"`).not.toContain(term);
  }
});
