/**
 * 锁 6 的真渲染夹具 —— 只被 Playwright 动态 import,不进任何生产 chunk。
 *
 * 放在 tests/ 而不是 src/:既让 Vite 负责裸模块说明符(react / react-dom)的解析与
 * `@/` 别名,又不会把测试脚手架混进产物。渲染的是**生产同一份组件**,
 * 同一个 React、同一套 hooks —— 不是复刻一份行为。
 */
import { createElement } from 'react';
import { createRoot } from 'react-dom/client';

import { IdentityReviewPanel } from '@/pages/Monitoring/components/IdentityReviewPanel';

export async function mountIdentityReviewPanel(
  brandId: number,
  settleMs = 600,
): Promise<string> {
  const previous = document.getElementById('identity-review-harness');
  if (previous) previous.remove();

  const host = document.createElement('div');
  host.id = 'identity-review-harness';
  document.body.appendChild(host);

  createRoot(host).render(
    createElement(IdentityReviewPanel, { brandId, readOnly: true }),
  );

  await new Promise((resolve) => setTimeout(resolve, settleMs));
  return host.innerHTML;
}
