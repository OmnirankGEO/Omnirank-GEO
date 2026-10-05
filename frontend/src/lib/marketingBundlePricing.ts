/**
 * 「生成完整推广包」的 feature code —— **由分辨率决定,不是一个常数**。
 *
 * 服务端规则(两处,逐字相同):
 *   services/marketing/material_factory.py:36
 *   services/marketing/geo_factory.py:280
 *     return "mktg_bundle_pro" if resolution in ("2k", "4k") else "mktg_bundle_std"
 * 生产实价:std 650 / pro 1040(差 60%)⇒ 取错 code 就是显示一个错的价。
 *
 * 🔴 为什么单独成一个模块而不是写在页面里(Review 2026-09-05 注毒抓到):
 *   第一版把它写在 `GeoContentCenter.tsx` 内,判据只能锁两样东西 ——
 *   **常量集合**(`['2k','4k']`)与**调用形状**(`bundleFeatureCode(resolution)`)。
 *   于是把函数体改成 `return 'mktg_bundle_std'` 恒定值时,常量没动、调用也没动,
 *   **判据全绿而映射已经坏了**。锁"零件在不在"锁不住"零件干了什么"。
 *   抽成纯模块后,判据可以**逐个 resolution 求值**去比对后端规则 —— 锁的是行为。
 */
export const BUNDLE_RESOLUTIONS = ['1k', '2k', '4k'] as const;
export type BundleResolution = typeof BUNDLE_RESOLUTIONS[number];

export type BundleFeatureCode = 'mktg_bundle_pro' | 'mktg_bundle_std';

/** 与服务端同一条规则的第三份 —— 判据 `verify-p65-inline-price.mjs` A 段逐项比对三处。 */
export const BUNDLE_PRO_RESOLUTIONS: readonly BundleResolution[] = ['2k', '4k'];

export function bundleFeatureCode(res: BundleResolution | string): BundleFeatureCode {
    return (BUNDLE_PRO_RESOLUTIONS as readonly string[]).includes(res)
        ? 'mktg_bundle_pro'
        : 'mktg_bundle_std';
}
