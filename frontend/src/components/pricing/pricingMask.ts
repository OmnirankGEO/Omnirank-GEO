/**
 * 报价隐私遮罩占位符 · 单点 SSOT
 *
 * [WO_QUOTE_COEFFICIENT_PRIVATE_TOPBAR 2026-08-11]
 * 刻意单独一个**纯 TS 模块**(不带 React):`priceRationale.ts` 明确声明「无 React 依赖」,
 * 若让它去 import `pricingPrivacy.tsx` 就把 React 拖进纯构建器了;
 * 而两边各写一个 '********' 字面量迟早飘。
 */
export const PRICING_MASK = '********';
