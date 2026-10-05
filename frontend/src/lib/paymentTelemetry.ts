/**
 * paymentTelemetry — 支付链路端上埋点
 * [WO_MOBILE_PAY_JUMP 2026-08-07 §2.1 · 先做,其余修理都靠它定案]
 *
 * 为什么非埋不可:2026-08-07 两个案例的服务端证据链**到"H5 链接已交到前端"为止全绿**
 * (虎皮椒下单成功、payment_url_mobile 已返回),而用户跳去 xunhupay 外域不经过我们的日志。
 * 服务端对"用户到底跳没跳出去"是**零可见度**的 —— 没有下面这三个点,
 * 下一次"点了没反应"还是只能猜。
 *
 * 三个点回答三个不同的问题:
 *   pay_dialog_shown     支付弹窗到底渲染出来了吗?渲染时手上有没有可跳的链接?
 *   pay_link_clicked     用户点到那个按钮了吗?(shown 有、clicked 无 = 按钮点不到/看不到)
 *   pay_jump_left_page   点完页面真的隐藏/卸载了吗?(clicked 有、left 无 = 跳转被系统拦了)
 *
 * 🔴 四条硬约束:
 *   1. fire-and-forget —— 埋点失败不得阻塞支付主链(整层包在 try/catch 里,不 await,不抛)
 *   2. clicked / left 必须**立刻**发(trackEventNow)。跳走之后队列就没机会 flush 了
 *   3. 不记任何金额以外的资金细节、不记 openid/token
 *   4. 每个订单每种事件只发一次 —— 轮询 3s 一跳,不去重会把 shown 刷成噪音
 */
import { useEffect, useRef } from 'react';
import { trackEvent, trackEventNow } from '@/lib/analytics';
import { isDesktopWechat, isMobileDevice, isWechatUa } from '@/lib/paymentEnv';

export const PAY_DIALOG_SHOWN = 'pay_dialog_shown';
export const PAY_LINK_CLICKED = 'pay_link_clicked';
export const PAY_JUMP_LEFT_PAGE = 'pay_jump_left_page';
/**
 * [#169 §2.1.3] 用户从收银台回来了、单还没付成。
 *
 * 🔴 为什么要第四个点:前三个点在 09-10 那一单上**全绿**
 *    (shown → clicked → left 齐全),而钱没到 —— 它们证明的是「我们把人送出去了」,
 *    证明不了「她在外面付成了没有」。这一条补的正是「回来了、仍 pending」这一段,
 *    没有它,下一次还是只能看到一条断在 left 的漏斗。
 */
export const PAY_RETURN_UNPAID = 'pay_return_unpaid';

/** 支付弹窗所在页面 · 出问题时一眼看出是哪条入口 */
export type PaySurface = 'agent_inventory' | 'customer_recharge' | 'social_recharge';

export interface PayContext {
  surface: PaySurface;
  /** 订单号 · 埋点与服务端订单日志靠它对齐 */
  order_id?: string | null;
  /** 后端最终选定的渠道(wechat_jsapi / wechat_native / xunhupay) */
  channel?: string | null;
  /** 手上有没有 H5 跳转链接 */
  has_mobile_url?: boolean;
  /** 手上有没有二维码 */
  has_qr?: boolean;
}

function envFields(): Record<string, unknown> {
  return {
    is_mobile: isMobileDevice(),
    is_wechat: typeof navigator !== 'undefined' && isWechatUa(navigator.userAgent),
    is_desktop_wechat: isDesktopWechat(),
    viewport_w: typeof window === 'undefined' ? null : window.innerWidth,
    viewport_h: typeof window === 'undefined' ? null : window.innerHeight,
  };
}

function payload(ctx: PayContext, extra?: Record<string, unknown>): Record<string, unknown> {
  return {
    surface: ctx.surface,
    order_id: ctx.order_id ?? null,
    channel: ctx.channel ?? null,
    has_mobile_url: Boolean(ctx.has_mobile_url),
    has_qr: Boolean(ctx.has_qr),
    ...envFields(),
    ...(extra || {}),
  };
}

/** 支付弹窗渲染出来了(每个订单一次) */
export function trackPayDialogShown(ctx: PayContext): void {
  try { trackEvent(PAY_DIALOG_SHOWN, payload(ctx)); } catch { /* 埋点不阻塞支付 */ }
}

/**
 * 用户点了支付链接/按钮。
 * 🔴 必须 trackEventNow:紧接着就要离开本页,等 debounce 就等不到了。
 */
export function trackPayLinkClicked(ctx: PayContext, extra?: { new_tab?: boolean; link_kind?: string }): void {
  try { trackEventNow(PAY_LINK_CLICKED, payload(ctx, extra)); } catch { /* 同上 */ }
}

/**
 * [#169 §2.1.3] 回到页面、订单仍未支付。`seconds_away` = 离开了多久。
 * 与前三点同样 fire-and-forget;用 trackEventNow —— 她可能马上又跳出去一次。
 */
export function trackPayReturnUnpaid(ctx: PayContext, extra: { seconds_away: number }): void {
  try { trackEventNow(PAY_RETURN_UNPAID, payload(ctx, extra)); } catch { /* 埋点不阻塞支付 */ }
}

/** 页面真的隐藏/卸载了 = 跳转确实发生了 */
export function trackPayJumpLeftPage(ctx: PayContext, extra?: { reason?: string }): void {
  try { trackEventNow(PAY_JUMP_LEFT_PAGE, payload(ctx, extra)); } catch { /* 同上 */ }
}

/**
 * 装 pagehide / visibilitychange 监听,点过支付链接之后离页就记一笔。
 *
 * 🔴 用 pagehide 而不是 beforeunload:iOS Safari 上 beforeunload 常常不触发
 *   (整个 iOS 这类 bug 都是"桌面 Chrome 永远测不出来"),pagehide 才是可靠的那个。
 * 🔴 只在 armed(用户点过链接)之后才记 —— 否则用户切个后台、锁个屏都会被当成"跳走了"。
 *
 * @param active 弹窗是否处于待支付态(false 时不装监听)
 * @param ctx    当前订单上下文
 * @returns arm  在点击 handler 里调一次,表示"从现在起离页算跳转成功"
 */
export function usePayJumpTracking(active: boolean, ctx: PayContext): () => void {
  const armedRef = useRef(false);
  const armedOrderRef = useRef<string | null>(null);
  const firedRef = useRef(false);
  const ctxRef = useRef(ctx);
  ctxRef.current = ctx;

  useEffect(() => {
    if (!active) return;
    // 🔴 只在 active 翻转时重置,**不能**把 ctx.order_id 放进 deps:
    //   社媒那条路是"先点按钮、建单在手势回调里" —— order_id 从 null 变成真单号,
    //   若跟着重置就会把刚 arm 的那一下抹掉,pay_jump_left_page 永远不发。
    armedRef.current = false;
    armedOrderRef.current = null;
    firedRef.current = false;

    const fire = (reason: string) => {
      if (!armedRef.current || firedRef.current) return;
      // arm 时已有单号、现在却换成了另一单 = 上一单遗留的 arm,不算这一单跳走
      const now = ctxRef.current.order_id ?? null;
      if (armedOrderRef.current && now && armedOrderRef.current !== now) return;
      firedRef.current = true;
      trackPayJumpLeftPage(ctxRef.current, { reason });
    };
    const onPageHide = () => fire('pagehide');
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') fire('visibilitychange');
    };

    window.addEventListener('pagehide', onPageHide);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('pagehide', onPageHide);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [active]);

  return () => {
    armedRef.current = true;
    armedOrderRef.current = ctxRef.current.order_id ?? null;
  };
}
