/**
 * [工单 2026-08-03 ③] 问句标题层级 · 真渲染夹具。
 * 只被 Playwright 动态 import,不进生产 chunk;渲染的是**生产同一份组件**。
 */
import { createElement } from 'react';
import { createRoot } from 'react-dom/client';

import { EvidenceMatrix } from '@/features/publicReportPremium/components/sections/EvidenceMatrix';
import { PlatformPerformance } from '@/features/publicReportPremium/components/sections/PlatformPerformance';

const LONG_Q = '贵州省遵义市仁怀市（茅台镇）商务送礼酱香酒哪家靠谱？有没有支持小批量定制的源头厂家推荐';

// 🔴 字段名照抄 contract/types.ts,不猜:DataSection 三态是 ready/empty/unavailable
//    (第一版写 status:'ok' + PlatformRow 字段 → 组件渲染成"暂无数据",8 条锁全红)。
const ITEMS = [
  {
    rowKey: 'r1', question: LONG_Q, platformName: '通义千问',
    verdict: 'engine_error', answerExcerpt: null,
    citedDomains: [], evidenceLevel: null, testedAt: '2026-08-03T10:00:00Z',
  },
  {
    rowKey: 'r2', question: '贵州禾泉酒业是做什么的？', platformName: '通义千问',
    verdict: 'mentioned', answerExcerpt: '这是一段用于排版验证的答案正文，长度足够触发多行。',
    citedDomains: ['zhihu.com'], evidenceLevel: null, testedAt: '2026-08-03T10:01:00Z',
  },
] as any;

const PLATFORM_SECTION = {
  status: 'ready',
  data: [{
    platformName: '通义千问', validSamples: 8, detectionRatePct: 12.5,
    mentionRatePct: 0, recommendRatePct: 0, citationCount: 62,
    citationStatus: null,
  }],
} as any;

function mount(el: HTMLElement, node: React.ReactElement) {
  createRoot(el).render(node);
}

export async function mountReportSections(settleMs = 600): Promise<void> {
  const prev = document.getElementById('report-hierarchy-harness');
  if (prev) prev.remove();
  const host = document.createElement('div');
  host.id = 'report-hierarchy-harness';
  document.body.appendChild(host);

  const a = document.createElement('div');
  a.id = 'harness-platform';
  const b = document.createElement('div');
  b.id = 'harness-matrix';
  host.appendChild(a);
  host.appendChild(b);

  mount(a, createElement(PlatformPerformance, { section: PLATFORM_SECTION, evidenceItems: ITEMS }));
  mount(b, createElement(EvidenceMatrix, {
    section: { status: 'ready', data: { items: ITEMS, totalCount: ITEMS.length } },
  } as any));

  await new Promise((r) => setTimeout(r, settleMs));
}
