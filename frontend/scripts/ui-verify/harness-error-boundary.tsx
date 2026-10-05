/**
 * 验收 harness · GlobalErrorBoundary 的 chunk 自愈(真挂载真边界真抛错)。
 *
 * 覆盖三种情形(用 query 参数选):
 *   ?mode=chunk&cooldown=1  → 冷却期内不 reload,只看过渡页有没有渲染出来
 *   ?mode=other&cooldown=1  → **反向对照**:非 chunk 错误必须仍走原来的报错页
 *                             (否则等于把所有错误都吞了)
 *   ?mode=chunk             → 不设冷却,验 sessionStorage 冷却键真的被写入(= 排了 reload)
 *
 * 不打后端、不碰真实路由。
 */
import React from 'react';
import ReactDOM from 'react-dom/client';
import { GlobalErrorBoundary } from '@/App';
import '@/index.css';

const params = new URLSearchParams(window.location.search);
const mode = params.get('mode') || 'chunk';
const COOLDOWN_KEY = 'omnirank_chunk_reload_at';

// 冷却期内 maybeReloadOnChunkError 会直接返回、不排 reload —— 这样页面不会在
// 断言前跳走,可以安稳地看渲染结果。
if (params.get('cooldown') === '1') {
    sessionStorage.setItem(COOLDOWN_KEY, String(Date.now()));
} else {
    sessionStorage.removeItem(COOLDOWN_KEY);
}

const CHUNK_MESSAGE =
    'Failed to fetch dynamically imported module: https://omnirank.top/assets/UserManagement-H3HhPu79.js';

function Boom(): React.ReactElement {
    throw mode === 'chunk' ? new TypeError(CHUNK_MESSAGE) : new Error('普通业务异常 · 不该被当成 chunk');
}

ReactDOM.createRoot(document.getElementById('root')!).render(
    <GlobalErrorBoundary>
        <Boom />
    </GlobalErrorBoundary>,
);
