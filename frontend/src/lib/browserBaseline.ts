/**
 * [工单 2026-08-06 §4.4 第 4 条] 低于支持底线时,给一句人能看懂的升级提示。
 *
 * 🔴 「已决定不支持」≠「不管」。白屏或功能静默失效是把成本转嫁给客户还不告诉他,
 * 那是最坏的一种「不支持」。这里的提示正是铁律 [[feedback_hint_must_help_or_hide]]
 * 的**正向用法**:它告诉用户到底该升到哪个版本,所以它该显示。
 *
 * 底线(Owner 2026-08-06 拍板,SSOT = package.json 的 browserslist):
 *   Safari / iOS / iPadOS >= 16 · Chrome >= 90 · Edge >= 90 · 微信内置 webview
 *
 * ⚠️ 这里是**运行时**判定,靠 UA;package.json 的 browserslist 是**构建期**判定,靠 API 扫描。
 *   两者目的不同不能互相替代:构建期防我们写出超标代码,运行时告诉已经打不开的用户怎么办。
 *   数字写在两处是有意的重复,改底线时两处都要改 —— verify-browser-baseline.mjs 只锁前者,
 *   所以这里的常量单独配了 tests 里的一致性断言。
 */
export const BASELINE = {
  safari: 16,
  chrome: 90,
  edge: 90,
} as const;

export interface BaselineVerdict {
  supported: boolean;
  /** 检出的浏览器族,用于文案 */
  family: 'safari' | 'chrome' | 'edge' | 'wechat' | 'unknown';
  detected: number | null;
  /** 不支持时给出的升级指引;支持时为 null */
  message: string | null;
}

/**
 * 只在**确知**版本低于底线时才判不支持。认不出来的一律放行 ——
 * 宁可放进来一个可能不兼容的,也不要把一个好浏览器挡在门外。
 */
export function checkBrowserBaseline(ua: string = typeof navigator !== 'undefined' ? navigator.userAgent : ''): BaselineVerdict {
  const ok = (family: BaselineVerdict['family'], detected: number | null = null): BaselineVerdict =>
    ({ supported: true, family, detected, message: null });

  if (!ua) return ok('unknown');

  // 微信内置 webview:Android 端是 Chromium 内核,iOS 端就是系统 WKWebView(= iOS Safari 版本)。
  // 版本无从可靠取得,统一放行 —— 它是客户点开报价/报告分享链接的入口,误挡代价极高。
  if (/MicroMessenger/i.test(ua)) return ok('wechat');

  // Edge(Chromium)必须排在 Chrome 之前:它的 UA 同时含 Chrome/ 与 Edg/
  const edge = /\bEdg\/(\d+)/.exec(ua);
  if (edge) {
    const v = Number(edge[1]);
    return v >= BASELINE.edge ? ok('edge', v) : {
      supported: false, family: 'edge', detected: v,
      message: `你的 Edge 版本过低(${v})。请升级到 Edge ${BASELINE.edge} 或更高版本后再使用。`,
    };
  }

  // iOS 上的 Chrome(CriOS)实际内核是系统 WebKit,按 iOS 版本判
  const crios = /\bCriOS\/(\d+)/.exec(ua);
  const chrome = !crios && /\bChrome\/(\d+)/.exec(ua);
  if (chrome) {
    const v = Number(chrome[1]);
    return v >= BASELINE.chrome ? ok('chrome', v) : {
      supported: false, family: 'chrome', detected: v,
      message: `你的 Chrome 版本过低(${v})。请升级到 Chrome ${BASELINE.chrome} 或更高版本后再使用。`,
    };
  }

  // Safari / iOS WebKit:UA 里的 Version/x.y 是 Safari 版本
  if (/Safari\//.test(ua) || crios) {
    const ver = /\bVersion\/(\d+)(?:\.(\d+))?/.exec(ua);
    if (!ver) return ok('safari');
    const v = Number(`${ver[1]}.${ver[2] ?? 0}`);
    return v >= BASELINE.safari ? ok('safari', v) : {
      supported: false, family: 'safari', detected: v,
      message: `你的系统版本过低(Safari ${v})。请把 iPhone / iPad / Mac 升级到 Safari ${BASELINE.safari}（iOS/iPadOS 16）或更高版本，或改用 Chrome 浏览器打开。`,
    };
  }

  return ok('unknown');
}
