import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.tsx'
import { ThemeProvider } from '@/context/ThemeContext'
import { maybeReloadOnChunkError } from '@/lib/errorReporter';
import { checkBrowserBaseline } from '@/lib/browserBaseline'
import './index.css'

// Phase 08.4 (CTO-15.23 2026-05-04) · P0 · Mobile 白屏诊断
// 手机端白屏但桌面 OK · 必有 mobile-specific 错误
// 加 global error handler · 把任何 boot 错误显示到 body · 不再白屏
function showBootError(label: string, err: unknown): void {
  try {
    const el = document.getElementById('root') || document.body;
    if (!el) return;
    const msg = err instanceof Error ? `${err.name}: ${err.message}\n\n${err.stack || ''}` : String(err);
    // [2026-07-22 sink census F50] label/UA/href 与 msg 一样过转义 · 补齐自 XSS 面
    const esc = (v: string) => v.replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]!));
    el.innerHTML = `
      <div style="padding:20px;font-family:-apple-system,sans-serif;background:#fff;color:#000;min-height:100dvh;">
        <h2 style="color:#dc2626;margin:0 0 8px;font-size:18px;">⚠️ 加载错误 · 请截图发开发</h2>
        <p style="color:#666;font-size:13px;margin:0 0 12px;">来源: ${esc(label)}</p>
        <pre style="background:#f5f5f5;padding:12px;border-radius:8px;font-size:11px;line-height:1.5;white-space:pre-wrap;word-break:break-word;color:#dc2626;border:1px solid #fca5a5;">${esc(msg)}</pre>
        <p style="color:#666;font-size:12px;margin:14px 0 0;">UA: ${esc(navigator.userAgent)}</p>
        <p style="color:#666;font-size:11px;margin:8px 0 0;">URL: ${esc(location.href)}</p>
      </div>
    `;
  } catch { /* 静默 · 至少 console */ }
}

window.addEventListener('error', (e) => {
  console.error('[boot] window.error', e);
  if (!document.querySelector('#root > *')) {
    showBootError('window.error', e.error || e.message);
  }
});
window.addEventListener('unhandledrejection', (e) => {
  console.error('[boot] unhandledrejection', e);
  if (!document.querySelector('#root > *')) {
    showBootError('unhandled promise', e.reason);
  }
});

// 沙盒: 客户选词页(SelectionPage)用原生 fetch 调 /api/s/* · 不走 authFetch · 默认拦不到
// 这里全局包一层 fetch: 沙盒态 + /api/s/ 路径时路由到沙盒 mock, 让客户分享链接在教程里真能用
import { isSandboxActive } from '@/sandbox/sandboxState'
import { tryFetchSandboxMockLazy } from '@/sandbox/lazySandboxInterceptor'
// 加载截图模式模块 · 注册 window.__screenshotMode console 开关
import '@/sandbox/screenshotMode'
try {
  const _origFetch = window.fetch.bind(window)
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    try {
      const url = typeof input === 'string' ? input : (input instanceof URL ? input.href : (input as Request).url)
      if (url && url.includes('/api/s/')) {
        // 沙盒分享链接(token 含 'sandbox-')即使在没激活沙盒的设备(同事/手机)打开也强制走 mock
        const isSandboxShareLink = /\/api\/s\/sandbox-/.test(url)
        if (isSandboxActive() || isSandboxShareLink) {
          const method = (init?.method || (input instanceof Request ? input.method : 'GET') || 'GET').toUpperCase()
          const mock = await tryFetchSandboxMockLazy(method, url, isSandboxShareLink, init)
          if (mock) return mock
        }
      }
    } catch { /* 拦截失败则放行真实 fetch */ }
    return _origFetch(input, init)
  }
} catch (err) {
  console.error('[boot] sandbox fetch patch failed', err)
}

// D.10 (CTO-15.18 · 2026-04-28):自然迁移率埋点 listener
import { installAnalyticsListener } from '@/lib/analytics'
try {
  installAnalyticsListener()
} catch (err) {
  console.error('[boot] installAnalyticsListener failed', err)
  // 非阻塞 · 继续 boot
}

// Deploy-CTO 2026-05-06:全局前端错误上报到后端 m3_analytics_events
// 跟 L27-38 的 boot 白屏 listener 互补 · 那个只在 root 未渲染时显示 · 这个上报历史数据
import { installErrorReporter } from '@/lib/errorReporter'
try {
  installErrorReporter()
} catch (err) {
  console.error('[boot] installErrorReporter failed', err)
}

// Deploy-CTO 2026-05-06:版本轮询 · 解决移动端用户没"强制刷新"功能 + 微信 WebView cache 老 HTML
// 60s HEAD `/` 比对 etag · 不一致 → 顶部 banner 提示刷新
// pageshow(bfcache 复活)/ visibilitychange · 立即比对
// [WO_NO_SILENT_RELOAD R1 2026-08-17] 全局「未提交输入」跟踪必须在引导期就装上:
//   它守的是**未注册表单**(全站逐步接入,今天大多数还没接)——
//   装晚了 = 用户已经开始打字但标志还没开始跟。
import { installDirtyInputTracking } from '@/lib/dirtyGuard'
try { installDirtyInputTracking() } catch (err) { console.error('[boot] installDirtyInputTracking failed', err) }
import { installVersionPoll } from '@/lib/versionPoll'
try {
  installVersionPoll()
} catch (err) {
  console.error('[boot] installVersionPoll failed', err)
}

if (
    import.meta.env.DEV &&
    typeof window !== 'undefined' &&
    ['localhost', '127.0.0.1', '::1'].includes(window.location.hostname)
) {
    window.addEventListener('load', () => {
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker
                .getRegistrations()
                .then((registrations) => {
                    registrations.forEach((registration) => {
                        void registration.unregister();
                    });
                })
                .catch(() => undefined);
        }
        if ('caches' in window) {
            window.caches
                .keys()
                .then((keys) =>
                    Promise.all(
                        keys
                            .filter((key) => /workbox|precache|api-cache|omnirank/i.test(key))
                            .map((key) => window.caches.delete(key)),
                    ),
                )
                .catch(() => undefined);
        }
    });
}

// Deploy-CTO 2026-05-06:chunk error 检测正则 · React lazy 部署后 hash 失效 → 静默 reload
//
// 🔴 [WO_NO_SILENT_RELOAD 2026-08-16] 这里原本自带**第三份**正则拷贝 + 自己的 cooldown
//   + 自己的 window.location.reload(),完全绕开 errorReporter 那条共享路径。
//   后果:工单只点了 versionPoll 与 errorReporter 两条路,给它们装上守卫之后,
//   **这一条仍会静默刷掉用户的表单** —— 守卫会有一个洞,而且是最难发现的那种
//   (两个明面上的口都堵了,看起来"已经修好了")。
//   errorReporter 的注释里写着「别再造第四份拷贝」,而第三份一直就在这里。
//   ⇒ 改为复用 isChunkLoadError + maybeReloadOnChunkError:
//     判定、cooldown、守卫、reload 全部单点,想绕过守卫就没有第二条路可走。

class ErrorBoundary extends React.Component<
    { children: React.ReactNode },
    { error: Error | null }
> {
    state = { error: null as Error | null };
    static getDerivedStateFromError(error: Error) {
        // chunk error → 静默 reload(防循环 · sessionStorage 30s cooldown · 脏表单守卫)
        // 全部逻辑单点在 errorReporter.maybeReloadOnChunkError 里,这里只负责转交。
        try {
            maybeReloadOnChunkError(error?.message || '');
        } catch { /* ignore */ }
        return { error };
    }
    render() {
        if (this.state.error) {
            return (
                <div style={{ padding: 40, fontFamily: 'monospace' }}>
                    <h2 style={{ color: 'red' }}>页面渲染出错</h2>
                    <pre style={{ whiteSpace: 'pre-wrap', background: '#f5f5f5', padding: 16, borderRadius: 8 }}>
                        {this.state.error.message}
                        {'\n\n'}
                        {this.state.error.stack}
                    </pre>
                </div>
            );
        }
        return this.props.children;
    }
}

// [工单 2026-08-06 §4.4 第 4 条] 低于 Owner 拍板的支持底线 → 给一句明确的升级指引。
// 🔴 「不支持」不等于「白屏」。这段**必须在 React 渲染之前**跑:低版本浏览器很可能
// 连 App 的 chunk 都解析不了,等 React 起来再提示就已经晚了。
// 只在**确知**版本低于底线时才拦;认不出来的一律放行(误挡好浏览器的代价更高)。
let __baselineBlocked = false;
try {
  const verdict = checkBrowserBaseline();
  if (!verdict.supported && verdict.message) {
    const root = document.getElementById('root');
    if (root) {
      const esc = (v: string) => v.replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]!));
      root.innerHTML = `
        <div style="padding:24px;font-family:-apple-system,'PingFang SC',sans-serif;background:#fff;color:#111;min-height:100dvh;display:flex;align-items:center;justify-content:center;">
          <div style="max-width:420px;">
            <h1 style="font-size:18px;margin:0 0 12px;">浏览器版本过低,暂时打不开</h1>
            <p style="font-size:14px;line-height:1.7;color:#444;margin:0 0 16px;">${esc(verdict.message)}</p>
            <p style="font-size:12px;color:#888;margin:0;">升级后重新打开本页面即可正常使用。</p>
          </div>
        </div>`;
      __baselineBlocked = true;
    }
  }
} catch (err) {
  // 判定本身出错时一律放行,绝不因为探测代码有 bug 把用户挡在门外。
  console.error('[boot] browser baseline check failed', err);
}

if (!__baselineBlocked) try {
  ReactDOM.createRoot(document.getElementById('root')!).render(
      <React.StrictMode>
          <ErrorBoundary>
              <ThemeProvider>
                  <App />
              </ThemeProvider>
          </ErrorBoundary>
      </React.StrictMode>,
  )
} catch (err) {
  console.error('[boot] ReactDOM.render failed', err)
  showBootError('ReactDOM.createRoot.render', err)
}
