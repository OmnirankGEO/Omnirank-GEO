/**
 * SandboxRouteGuard · 沙盒态全局路由守卫
 * Stage 1 Batch 4 (2026-05-18)
 *
 * 解决问题:
 *   用户在沙盒里跑教学时, 新开 tab 打开 http://localhost:5173/ 或手动改 URL
 *   到不在沙盒白名单的页面 (如 /dashboard) · 之前会进去看到账号真实数据
 *
 * 设计:
 *   沙盒态下, 任何当前路径不在白名单 → 强制 navigate replace 到 /diagnosis/new
 *   (step 1 起点) · 这样从任何入口闯进非白名单都会被踢回教学路径
 *
 * 不渲染任何 UI · 只挂 effect · 必须放在 Layout 里 (Router 内部 + 沙盒 banner 同级)
 *
 * 公开页 (login / landing / portal / 公开报告链接) 不受影响 · 因为这些不会路由到 Layout
 */
import { useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useSandboxState } from './sandboxState';

// 沙盒态允许的路径前缀 · 教学相关页面 + 帮助中心
// 数据隔离已经在接口层做 (sandboxInterceptor 默认拦截所有非白名单 API · 返空)
// 路由这边只挡掉 dashboard / 后台管理这类 沙盒里没必要去的
const SANDBOX_ALLOWED_PREFIXES = [
    '/diagnosis',
    '/pricing',
    '/writing',
    '/publish',
    '/monitoring',
    '/help',
    '/my-clients',
    '/my-brand',
];

export function isSandboxAllowedPath(path: string): boolean {
    return SANDBOX_ALLOWED_PREFIXES.some(
        (prefix) => path === prefix || path.startsWith(prefix + '/'),
    );
}

export function SandboxRouteGuard() {
    const { isSandbox } = useSandboxState();
    const location = useLocation();
    const navigate = useNavigate();

    useEffect(() => {
        if (!isSandbox) return;
        if (isSandboxAllowedPath(location.pathname)) return;
        // 不在白名单 → 跳 step 1 起点 (诊断页)
        // replace 避免污染浏览器历史 · 用户点后退不会回到非白名单页
        navigate('/diagnosis/new', { replace: true });
    }, [isSandbox, location.pathname, navigate]);

    return null;
}

export default SandboxRouteGuard;
