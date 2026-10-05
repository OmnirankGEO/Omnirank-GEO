/**
 * paymentEnv — 支付相关的浏览器形态判据(前端侧)
 * [WO_MOBILE_PAY_JUMP 2026-08-07 §2.2 / §2.3]
 *
 * 🔴 这份 token 表是 services/payment_routing.py 的**镜像**,不是第二套判据。
 *   两边不一致会造出"后端按 PC 发二维码、前端按微信内渲文案"这种自相矛盾的界面,
 *   所以 tests/test_mobile_pay_jump_2026_08_07.py 里有一条跨语言 token 对齐锁:
 *   改了这里不同步改 Python(或反过来)会当场转红。
 *
 * 判断"要不要开新窗口"的理由,写死在这免得下次又被改回去:
 *   移动端新窗口**零价值** —— 手机上没有并排两个窗口这回事,跳出去就是跳出去;
 *   而代价是实的:iOS Safari「阻止弹出式窗口」默认开,`target="_blank"` 会被拦,
 *   拦掉不报错、不进 catch,用户看到的就是"点了没反应"。
 *   桌面相反:同窗跳会把用户正在看的页面顶掉,新窗口才对。
 */

/** 桌面版微信内置浏览器 UA 标记 · 与 payment_routing.DESKTOP_WECHAT_UA_TOKENS 逐项对齐 */
export const DESKTOP_WECHAT_UA_TOKENS = ['windowswechat', 'macwechat'] as const;

/** 移动端 UA 标记 · 与 payment_routing.MOBILE_UA_TOKENS 逐项对齐 */
export const MOBILE_UA_TOKENS = [
  'android',
  'iphone',
  'ipad',
  'ipod',
  'mobile',
  'opera mini',
  'windows phone',
] as const;

const norm = (ua: string | null | undefined): string => (ua || '').toLowerCase();

/** 任意微信内置浏览器(手机 + 桌面) */
export function isWechatUa(ua: string | null | undefined): boolean {
  return norm(ua).includes('micromessenger');
}

/** Windows / Mac 桌面版微信内置浏览器 */
export function isDesktopWechatUa(ua: string | null | undefined): boolean {
  const s = norm(ua);
  return DESKTOP_WECHAT_UA_TOKENS.some((t) => s.includes(t));
}

/** 手机微信内置浏览器 —— 唯一能可靠调起 JSAPI 付款层的形态 */
export function isMobileWechatUa(ua: string | null | undefined): boolean {
  return isWechatUa(ua) && !isDesktopWechatUa(ua);
}

/** 移动端浏览器(含手机微信 · 不含桌面微信) */
export function isMobileUa(ua: string | null | undefined): boolean {
  const s = norm(ua);
  if (isDesktopWechatUa(ua)) return false;
  return MOBILE_UA_TOKENS.some((t) => s.includes(t));
}

function currentUa(): string {
  if (typeof navigator === 'undefined') return '';
  return navigator.userAgent || '';
}

export function isMobileDevice(): boolean {
  return isMobileUa(currentUa());
}

export function isDesktopWechat(): boolean {
  return isDesktopWechatUa(currentUa());
}

/**
 * 支付链接该不该开新窗口。
 * 移动端 false(同窗跳)· 桌面 true(保住当前页)。
 *
 * 用法(必须是这种形态,别拆成两个三元表达式):
 *   <a href={url} {...payLinkTargetProps()}>
 */
export function shouldOpenPayLinkInNewTab(ua?: string | null): boolean {
  return !isMobileUa(ua === undefined ? currentUa() : ua);
}

/** 直接摊进 <a> 的 props · 移动端不产出 target,桌面端产出 _blank + rel */
export function payLinkTargetProps(ua?: string | null): { target?: '_blank'; rel?: string } {
  return shouldOpenPayLinkInNewTab(ua)
    ? { target: '_blank', rel: 'noreferrer' }
    : {};
}
