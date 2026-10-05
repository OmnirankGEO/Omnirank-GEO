/**
 * 行动跳转目标与默认导航。
 * - 组件的 onNavigate 由最终集成者接线到现役 router；未接线时用一个"真实"的默认跳转兜底，
 *   保证按钮永不为死控件（去写作/去发布真的会跳）。
 * - 只带公开安全上下文（brand_id / diagnosis_id），绝不带 owner/上游/成本。
 */

export type NavTarget = 'writing' | 'publish';

const ROUTE: Record<NavTarget, string> = {
  writing: '/writing',
  publish: '/publish',
};

const SAFE_KEYS = ['brandId', 'diagnosisId'] as const;

export function navigateToTool(target: NavTarget, ctx: Record<string, unknown> = {}): void {
  const params = new URLSearchParams();
  for (const key of SAFE_KEYS) {
    const v = ctx[key];
    if (v !== undefined && v !== null) {
      params.set(key === 'brandId' ? 'brand_id' : 'diagnosis_id', String(v));
    }
  }
  const q = params.toString();
  const href = `${ROUTE[target]}${q ? `?${q}` : ''}`;
  if (typeof window !== 'undefined') window.location.assign(href);
}

/** 组件内统一取导航函数：优先用注入的 onNavigate，否则用默认真实跳转。 */
export function resolveNavigate(
  onNavigate?: (target: NavTarget, ctx: Record<string, unknown>) => void,
): (target: NavTarget, ctx: Record<string, unknown>) => void {
  return onNavigate ?? navigateToTool;
}
