/**
 * 「诊断对象」四项的共享定义(订正六 / 订正八)。
 *
 * 🔴 业务范围的中文标签**只在这里定义一次**:下拉的 `<option>` 与摘要行都从这里取。
 *    否则同一个词有两份 —— 改一处另一处不跟,而**屏幕上两处各自看起来都对**
 *    (订正八判据也点名了这一格:屏幕「区域生意」== 提交 `regional`,
 *     判据按 state 字段判、不按字符串全等;但那说的是判据,不是允许代码里存两份标签)。
 */

export type BusinessScope = 'regional' | 'national';

export interface ScopeOption {
    value: BusinessScope;
    /** 下拉里的完整说明 */
    optionLabel: string;
    /** 摘要行里的短名 */
    shortLabel: string;
}

export const BUSINESS_SCOPE_OPTIONS: readonly ScopeOption[] = [
    { value: 'regional', optionLabel: '区域生意（只在本地/周边接单）', shortLabel: '区域生意' },
    { value: 'national', optionLabel: '全国生意（全国接单）', shortLabel: '全国生意' },
];

export function scopeShortLabel(v: string | null | undefined): string {
    return BUSINESS_SCOPE_OPTIONS.find((o) => o.value === v)?.shortLabel ?? '';
}

/**
 * 四项是否已经齐到「可以压成一行摘要」。
 *
 * 🔴 城市只在**区域生意**下必填(订正六逐字:「行业必填;城市与范围在区域生意下必填」)——
 *    全国生意不需要城市,若也要求它非空,全国客户会永远进不了摘要态。
 */
export function subjectComplete(f: {
    brandName?: string | null;
    industry?: string | null;
    clientLocation?: string | null;
    businessScope?: string | null;
}): boolean {
    const has = (x?: string | null) => !!(x && x.trim());
    if (!has(f.brandName) || !has(f.industry)) return false;
    if (!has(f.businessScope)) return false;
    if (f.businessScope === 'regional' && !has(f.clientLocation)) return false;
    return true;
}
