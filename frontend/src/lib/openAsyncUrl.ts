/**
 * openAsyncUrl — 在用户手势的同步调用栈里打开"需要异步获取"的 URL
 *
 * [WO_IOS_TOUCH_UX 2026-08-05]
 * 与 copyUtils.copyAsyncText 同源的问题、不同的解法:
 *
 *   剪贴板可以用 ClipboardItem(Promise 载荷) —— 授权在手势时刻完成,文本晚到没关系。
 *   **window.open 没有这种载荷机制**,所以只能"先占住窗口,拿到 URL 再把它导航过去"。
 *
 * 病灶原样(本仓 3 处):
 *   const res = await authFetch('/api/.../share-link')   ← 手势凭证在这一行就没了
 *   window.open(data.share_url, '_blank')                ← Safari 静默拦掉,不报错不进 catch
 * 桌面 Chrome 手势上下文不过期,所以这类 bug 在桌面**永远测不出来**。
 *
 * 🔴 两个反直觉的点,写死在这里免得下次又踩:
 *   1. `window.open(url, '_blank', 'noopener')` 按规范**返回 null** —— 想拿到窗口引用就
 *      不能传 noopener。安全性改用手动置 `win.opener = null` 兜(等效)。
 *   2. 占位窗口必须写点东西进去。iOS 上 about:blank 空白页看着就是"卡住了",
 *      用户会在 URL 到手之前把它关掉。
 */

export interface AsyncOpenResult {
    /** 新窗口是否成功打开并导航过去 */
    ok: boolean;
    /** 拿到的 URL;null = 取 URL 这步就失败了(接口失败) */
    url: string | null;
    /** url=null 时,getUrl 抛出的 Error.message */
    errorMessage?: string;
    /** ok=false 且 url 非空 = 弹窗被系统拦了 → 调用方必须给可点击的链接兜底 */
    blocked?: boolean;
}

/** 占位窗口里的过渡内容 —— 不写的话 iOS 上是一张白页,看着像坏了 */
const PLACEHOLDER_HTML =
    '<!doctype html><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<title>正在打开…</title>' +
    '<body style="margin:0;display:flex;align-items:center;justify-content:center;' +
    'height:100vh;font:16px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;color:#666">' +
    '正在打开，请稍候…</body>';

/**
 * ⚠️ 必须在事件 handler 的同步阶段调用(前面不能有任何 await),
 *    否则占位窗口这一步本身就会被拦,等于没修。
 */
export async function openAsyncUrl(getUrl: () => Promise<string>): Promise<AsyncOpenResult> {
    // —— 手势同步栈:先把窗口占住 ——
    // 不传 noopener(传了返回 null),改为下面手动断 opener。
    let win: Window | null = null;
    try {
        win = window.open('', '_blank');
    } catch {
        win = null;
    }
    if (win) {
        // 等效于 noopener:切断新页面对 opener 的反向引用
        try { win.opener = null; } catch { /* 跨源保护下可能抛,忽略 */ }
        try { win.document.write(PLACEHOLDER_HTML); win.document.close(); } catch { /* 同上 */ }
    }

    let url: string;
    try {
        url = await getUrl();
    } catch (err) {
        // 取 URL 失败 → 别把空白占位窗留在那儿
        if (win) { try { win.close(); } catch { /* ignore */ } }
        return {
            ok: false,
            url: null,
            errorMessage: err instanceof Error && err.message ? err.message : undefined,
        };
    }

    if (win && !win.closed) {
        try {
            // replace 而非 href:不给占位页留一条历史,用户点返回不会退回空白页
            win.location.replace(url);
            return { ok: true, url };
        } catch {
            try { win.close(); } catch { /* ignore */ }
        }
    }

    // 走到这里 = 占位窗没开成(被拦)或用户已把它关掉。
    // 🔴 不在这里 fallback 到 location.href —— 那会把用户当前页面顶掉,
    //    正在填的表单/正在看的报告就没了。交给调用方给可点击链接。
    return { ok: false, url, blocked: true };
}
