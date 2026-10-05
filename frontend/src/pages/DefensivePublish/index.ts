/**
 * 防御型 GEO 发布 v2 前端三页(UI-34 / UI-35 / UI-36 + §0.5.5 U-3/U-4/U-5)。
 *
 * 🔴🔴 [包H R2 收口 2026-08-24] **`MediaDecisionConfirm` / `PublishCommandStatus`
 *      两个确认页不从这里再导出 —— 请不要"顺手"加回来。**
 *
 * 它们是**会冻钱**的屏。终审 P0-1 把客户入口关掉之后,通向它们的唯一合法路径是
 * `PublishDeepLink.tsx`(它先问 `publishEntryGate`,入口关着就渲染 typed 说明,
 * **不渲染确认页**),那里走的是相对路径 `./MediaDecisionConfirm`,不经本文件。
 *
 * 而这个 barrel 一旦再导出它们,`App.tsx` 就多出两条**零成本**的绕过面:
 *   `import { MediaDecisionConfirm } from '@/pages/DefensivePublish'`
 *   `import * as X from '@/pages/DefensivePublish'` → `<X.MediaDecisionConfirm />`
 * 两条的 specifier 里都不出现组件名。R1/R2 两轮 Review 亲毒都打在这类面上。
 *
 * 双树 census(本树 + 窗E 在途树)确认这两行**零生产消费方** —— 删掉是
 * **结构性消灭绕过面**,比在判据里再加一块 pattern 补丁强:没有面,就不用守。
 *
 * `test-defgeo-gate2-fixes.mjs` 里对应的 `viaBarrel` / `viaNamespace` 两格保留,
 * 降级为**复引入哨兵**:哪天有人把这两行加回来并在 App.tsx 用上,它们第一个叫。
 */
export { default as SettlementReviewQueue } from './SettlementReviewQueue';
export { usePublishStatusPolling } from './usePublishStatusPolling';
export { acceptStatusUpdate } from './statusVersionGuard';
export type { StatusUpdateVerdict, StatusVersionCarrier } from './statusVersionGuard';
