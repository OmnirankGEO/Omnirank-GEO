/**
 * 沙盒状态管理 (Sandbox Mode)
 * Stage 1 Batch 3 (2026-05-08): 教程沙盒方案的全局开关
 *
 * 设计目标:
 *   - 用 localStorage 持久化, 刷新页面后沙盒态保持
 *   - 不依赖 React Context, 让 axios 拦截器(非 React 环境) 也能读
 *   - 提供 React Hook 让 UI 组件订阅
 *
 * 用户旅程:
 *   1. 代理在欢迎弹窗选"立即开始" → enterSandbox()
 *   2. 浏览器进入"教程模式": 所有 API 返回预设 mock 数据, 不真扣费/不污染
 *   3. 用户在真实 UI 里点真按钮, 但所有响应都是预设的
 *   4. 走完 4 步教程 → exitSandbox() → 退到真实工作台
 */
import { useEffect, useState } from 'react';

const STORAGE_KEY = 'omnirank_sandbox_active';

/** 当前是否在沙盒态 (任何位置都可读) */
export function isSandboxActive(): boolean {
    try {
        return localStorage.getItem(STORAGE_KEY) === '1';
    } catch {
        return false;
    }
}

/** 进入沙盒 */
export function enterSandbox(): void {
    try {
        localStorage.setItem(STORAGE_KEY, '1');
        // 触发 storage 事件 + 自定义事件让 React Hook 订阅者刷新
        window.dispatchEvent(new Event('sandbox:change'));
    } catch (e) {
        console.warn('[Sandbox] enter failed:', e);
    }
}

/**
 * 清空一切教程残留 · 让沙盒从 0/4 干净重走 (统一重置入口 · 防"顾此失彼")
 * 清: 完成步骤 / 跳过步骤 / dismiss 的 spotlight / 已学报价路径 / intro 已弹标记 / tutorialStage
 * 注: 只动 localStorage · 调用方应紧接 location.reload() 让 React state 从干净 storage 重载,
 *     reload 同时会重置沙盒报价/写作的内存状态机
 */
export function resetSandboxTutorialState(): void {
    try {
        localStorage.removeItem('omnirank_sandbox_intro_shown');
        localStorage.removeItem('omnirank_sandbox_quote_paths');
        localStorage.removeItem('omnirank_sandbox_tutorial_stage');
        const raw = localStorage.getItem('omnirank_onboarding_state');
        if (raw) {
            const parsed = JSON.parse(raw);
            localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
                ...parsed,
                completed_steps: [],
                skipped_steps: [],
                dismissed_features: [],
                last_updated_at: new Date().toISOString(),
            }));
        }
    } catch (e) {
        console.warn('[Sandbox] resetSandboxTutorialState failed:', e);
    }
}

/** 退出沙盒 */
export function exitSandbox(): void {
    try {
        localStorage.removeItem(STORAGE_KEY);
        window.dispatchEvent(new Event('sandbox:change'));
    } catch (e) {
        console.warn('[Sandbox] exit failed:', e);
    }
}

/** React Hook: 订阅沙盒态变化, 自动 re-render */
export function useSandboxState(): { isSandbox: boolean; enter: () => void; exit: () => void } {
    const [isSandbox, setIsSandbox] = useState<boolean>(isSandboxActive);

    useEffect(() => {
        const handler = () => setIsSandbox(isSandboxActive());
        window.addEventListener('sandbox:change', handler);
        window.addEventListener('storage', handler);  // 跨 tab 同步
        return () => {
            window.removeEventListener('sandbox:change', handler);
            window.removeEventListener('storage', handler);
        };
    }, []);

    return { isSandbox, enter: enterSandbox, exit: exitSandbox };
}
