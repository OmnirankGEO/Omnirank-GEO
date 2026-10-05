/**
 * [Bug3修复] 统一跨浏览器剪贴板工具
 * navigator.clipboard 在非HTTPS环境（如 http://localhost:1688）下会被浏览器阻止
 * 提供 execCommand('copy') 降级方案
 *
 * [WO_WHITELABEL_COPY_UX 项2 2026-08-05] 新增 copyAsyncText:
 * iOS Safari / 微信 webview 要求剪贴板写入发生在用户手势的**同步调用栈**里。
 * 「先 await 接口再 copyToClipboard」的写法,await 之后手势上下文已失效,
 * writeText 与 execCommand 两条路都会被系统拒 —— 桌面 Chrome 没这个限制,
 * 所以这类 bug 在桌面永远测不出来。凡是"链接要先问后端"的复制,一律走 copyAsyncText。
 */
export async function copyToClipboard(text: string): Promise<boolean> {
    // 优先使用现代 API
    if (navigator.clipboard && window.isSecureContext) {
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch {
            // 降级到 execCommand
        }
    }

    // 降级方案：textarea + execCommand
    try {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.left = '-9999px';
        textarea.style.top = '-9999px';
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(textarea);
        return ok;
    } catch {
        console.error('[copyToClipboard] 所有复制方式均失败');
        return false;
    }
}

export interface AsyncCopyResult {
    /** 剪贴板写入是否成功 */
    ok: boolean;
    /**
     * 拿到的文本;null = 取文本这步就失败了(接口失败)。
     * ok=false 且 text 非空 → 调用方必须把 text 以可选中形式展示出来
     * (ManualCopyDialog),不许只丢一句"复制失败"。
     */
    text: string | null;
    /** text=null 时,getText 抛出的 Error.message(供调用方展示具体失败原因) */
    errorMessage?: string;
}

/**
 * 在用户手势的同步调用栈里复制"需要异步获取"的文本。
 *
 * 关键点:getText() 与 ClipboardItem 的构造都发生在当前同步栈里 ——
 * Safari 允许 ClipboardItem 的载荷是一个 Promise,写入授权在手势时刻就完成,
 * 文本晚点到没关系。桌面 Chrome 不支持 Promise 载荷时自动回落
 * 「await 后再写」的旧路(桌面手势上下文不过期,旧路是好的)。
 *
 * ⚠️ 必须在事件 handler 的同步阶段调用(前面不能有任何 await)。
 */
export async function copyAsyncText(getText: () => Promise<string>): Promise<AsyncCopyResult> {
    // 文本只取一次:两条复制路径共享同一个 promise,失败也不会重复打接口
    const textPromise = getText();
    // 防未处理拒绝告警(真正的错误处理在下方 await 处)
    textPromise.catch(() => {});

    if (
        typeof navigator !== 'undefined' && navigator.clipboard &&
        typeof navigator.clipboard.write === 'function' &&
        typeof ClipboardItem !== 'undefined' && window.isSecureContext
    ) {
        try {
            const blobPromise = textPromise.then(
                (t) => new Blob([t], { type: 'text/plain' }),
            );
            // 防 unhandledRejection:write 被拒时可能没人消费这个载荷分支
            blobPromise.catch(() => {});
            const item = new ClipboardItem({ 'text/plain': blobPromise });
            await navigator.clipboard.write([item]);
            return { ok: true, text: await textPromise };
        } catch {
            // Safari 之外的浏览器可能不接受 Promise 载荷 / 权限被拒 → 走旧路
        }
    }

    let text: string;
    try {
        text = await textPromise;
    } catch (err) {
        return {
            ok: false,
            text: null,
            errorMessage: err instanceof Error && err.message ? err.message : undefined,
        };
    }
    return { ok: await copyToClipboard(text), text };
}
