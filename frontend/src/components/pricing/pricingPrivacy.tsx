/**
 * pricingPrivacy — 报价页「演示隐私模式」单点状态
 *
 * [WO_QUOTE_COEFFICIENT_PRIVATE_TOPBAR 2026-08-11 · 返修 R2]
 *
 * 🔴 首轮把 revealed 关在 QuotePricingControlBar 的局部 state 里,结果只管住了顶栏自己。
 *    Review 实测:关键词行展开后的「为什么是这个价」(WhyThisPrice / priceRationale)
 *    照旧显示「单篇成本」「基础成本 = 建议篇数 × 单篇成本」「你账号的报价系数」——
 *    这正是当面演示时客户会点开看的那块,眼睛按钮对它完全无效。
 *    → 隐私态必须是**整页单点**,谁要遮谁来订阅,而不是每个组件各管一段。
 *
 * 🔴 fail-closed:没有 Provider 时 `usePricingPrivacy()` 返回 revealed=false(即隐藏)。
 *    新组件忘了包 Provider 只会「多遮一点」,不会「悄悄泄露」。
 *
 * 🔴 零持久化:不读也不写 localStorage / sessionStorage。刷新、新标签页、重开浏览器
 *    一律从隐藏开始(工单 §6「默认状态」)。
 */

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';

/** 敏感内容区统一占位(工单 §6:不是把数值改成 `*` 而保留字段名)· SSOT 在纯 TS 模块里 */
export { PRICING_MASK } from './pricingMask';

interface PricingPrivacyValue {
  revealed: boolean;
  setRevealed: (next: boolean) => void;
  toggleRevealed: () => void;
}

const FAIL_CLOSED: PricingPrivacyValue = {
  revealed: false,
  setRevealed: () => { /* 无 Provider 时不可切换 —— 只能保持隐藏 */ },
  toggleRevealed: () => { /* 同上 */ },
};

const PricingPrivacyContext = createContext<PricingPrivacyValue | null>(null);

export function PricingPrivacyProvider({
  quoteId, children,
}: {
  /** 当前报价单 id —— 换报价必须立刻回隐藏 */
  quoteId: number | null;
  children: ReactNode;
}) {
  const [revealed, setRevealedState] = useState(false);

  // 切报价 → 立刻回隐藏。用「渲染期同步派生」而不是 useEffect:
  // useEffect 会先提交一帧(新 quoteId + 旧 revealed=true),那一帧足够把上一份报价的
  // 系数/成本渲染出来并发出预览请求(验收 §11.9)。
  const [seenQuoteId, setSeenQuoteId] = useState<number | null>(quoteId);
  if (seenQuoteId !== quoteId) {
    setSeenQuoteId(quoteId);
    setRevealedState(false);
  }

  // 标签页离开后重新返回 → 恢复隐藏(演示中切走再切回来,不该还开着)
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === 'visible') setRevealedState(false);
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, []);

  const setRevealed = useCallback((next: boolean) => setRevealedState(next), []);
  const toggleRevealed = useCallback(() => setRevealedState(v => !v), []);

  return (
    <PricingPrivacyContext.Provider value={{ revealed, setRevealed, toggleRevealed }}>
      {children}
    </PricingPrivacyContext.Provider>
  );
}

export function usePricingPrivacy(): PricingPrivacyValue {
  return useContext(PricingPrivacyContext) ?? FAIL_CLOSED;
}
