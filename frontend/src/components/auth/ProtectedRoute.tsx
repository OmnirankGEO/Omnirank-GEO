/**
 * 路由守卫组件
 * 未登录 → 重定向到 /login
 * 需要改密 → 重定向到 /change-password
 * 无模块权限 → 重定向到首页（不再显示死胡同）
 * v1_2 (CTO-14.0 2026-04-19): requiresAgent 参数 — 代理端专属路由普通用户(L0)自动跳 /c/chat
 *   修复"L0 用户删 URL 后缀直接进代理端"权限漏洞
 * v1_3 (CTO-15.13 AI-3 2026-04-26): /api/auth/me 5xx 软失败时不踢登录页
 *   token 在 + user 还没拿到 + authSoftFailed=true 时显示"认证服务暂时不可用"fallback + 重试按钮
 *   不直接 Navigate /login(避免后端抖动把活跃用户踢回登录)
 */
import { useState } from 'react';
import { Link, Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '@/context/AuthContext';
import { allowsDemoSnapshotModule, useActiveDemoSelection } from '@/lib/demoMode';
import { isSandboxAllowedPath } from '@/sandbox/SandboxRouteGuard';
import { useSandboxState } from '@/sandbox/sandboxState';

interface ProtectedRouteProps {
    children: React.ReactNode;
    requiredModule?: string;
    requiredPermission?: string;
    /** v1_2: 代理端专属路由（/writing /publish /pricing /monitoring 等），L0 自动跳 /c/chat */
    requiresAgent?: boolean;
    /** [单1] 资金/商业身份专属面(库存·进货价·结算·推广):只认**本人**是服务商,
     *  代作业的员工一律不放行。见下方 requiresAgent 分支注释。 */
    ownerIdentityOnly?: boolean;
    /** Optional product-specific login entry, used by standalone products. */
    loginPath?: string;
}

export function ProtectedRoute({ children, requiredModule, requiredPermission, requiresAgent, ownerIdentityOnly, loginPath = '/login' }: ProtectedRouteProps) {
    const { isAuthenticated, isLoading, user, token, authSoftFailed, retryAuth, hasModule, hasPermission } = useAuth();
    const location = useLocation();
    const activeDemoSelection = useActiveDemoSelection();
    const { isSandbox } = useSandboxState();
    const [retrying, setRetrying] = useState(false);

    // 加载中
    if (isLoading) {
        return (
            <div className="fixed inset-0 z-50 flex justify-center items-center text-lg text-muted-foreground bg-background">
                加载中...
            </div>
        );
    }

    // CTO-15.13 AI-3 软失败 fallback:token 在但 user 没拿到(/api/auth/me 5xx 重试 2 次仍失败)
    // 不踢登录页 · 显示"认证服务暂时不可用 · 重试"
    // 必须放在 isAuthenticated 判断之前(否则 isAuthenticated=false 会先 Navigate /login)
    if (token && !user && authSoftFailed) {
        const handleRetry = async () => {
            setRetrying(true);
            try {
                await retryAuth();
            } finally {
                setRetrying(false);
            }
        };
        return (
            <div className="flex min-h-[calc(100dvh-3rem)] flex-col items-center justify-center gap-4 bg-background text-foreground px-6">
                <div className="text-base text-muted-foreground">暂时无法确认登录状态</div>
                <div className="text-sm text-muted-foreground/80 text-center max-w-md">
                    可能是限流、服务故障或网络中断。系统已停止自动重试并保留当前页面；请稍后手动重试。
                </div>
                <button
                    type="button"
                    disabled={retrying}
                    onClick={handleRetry}
                    className="px-4 py-2 rounded-md border border-border bg-card text-sm hover:bg-accent disabled:opacity-50"
                >
                    {retrying ? '正在重试…' : '重试'}
                </button>
                <a
                    href="/login"
                    className="text-xs text-muted-foreground/60 hover:text-muted-foreground underline-offset-2 hover:underline"
                >
                    或重新登录
                </a>
            </div>
        );
    }

    // 未登录
    if (!isAuthenticated) {
        return <Navigate to={loginPath} state={{ from: location }} replace />;
    }

    // 首登强制改密
    if (user?.must_change_password === 1 && location.pathname !== '/change-password') {
        return <Navigate to="/change-password" replace />;
    }

    const permissionDenied = (kind: 'agent' | 'module' | 'permission') => (
        <div className="flex min-h-[calc(100dvh-3rem)] flex-col items-center justify-center gap-4 bg-background px-6 text-center text-foreground">
            <div className="max-w-lg space-y-2">
                <h1 className="text-xl font-semibold">
                    {kind === 'agent' ? '当前账号没有服务商权限' : '当前账号没有此页面权限'}
                </h1>
                <p className="text-sm text-muted-foreground">
                    {kind === 'agent'
                        ? '这是服务商经营后台，普通账号不能进入。若你已开通服务商，请联系管理员核对账号身份。'
                        : '页面没有丢失，你的账号尚未获授权。请联系团队管理员开通相应角色或模块权限。'}
                </p>
                <p className="text-xs text-muted-foreground">本次仅阻止访问，没有修改任何数据或产生费用。</p>
            </div>
            <div className="flex flex-wrap justify-center gap-3">
                <Link className="inline-flex min-h-11 items-center rounded-md border border-border bg-card px-4 text-sm hover:bg-accent" to="/">
                    返回主菜单
                </Link>
                <Link className="inline-flex min-h-11 items-center rounded-md px-4 text-sm text-primary hover:bg-accent" to="/help">
                    查看帮助与联系方式
                </Link>
            </div>
        </div>
    );

    // 服务商身份只读 /api/auth/me.agent_level；管理员保留经营巡检/代运营入口，
    // 其商业主体由后端解析为独立的平台直营账号，绝不使用管理员个人账号。
    if (requiresAgent && !user?.is_admin) {
        const isAgent = (user?.agent_level ?? 0) >= 1;
        // [单1] 组织员工自己的 agent_level 恒为 0(他不是服务商,服务商是他所在
        // 组织的 owner),旧判定会把服务商雇的员工挡在所有服务商作业面外。
        // 现额外接受"他代作业的商业主体是服务商"。
        //
        // 但**资金/商业身份面除外**:库存、进货价与毛利、结算提现、推广佣金
        // 在组织能力模型里全是 owner-only(inventory.* / pricing.cost_and_margin_view /
        // settlements.* / withdrawals.* / referral.manage 都在 OWNER_ONLY_CAPABILITIES,
        // 角色配不上去)。这些面只认本人身份,代作业不构成放行理由。
        const operatingForAgent = Boolean(user?.operating_for_agent);
        const passes = isAgent || (operatingForAgent && !ownerIdentityOnly);
        if (!passes) {
            return permissionDenied('agent');
        }
    }

    // 模块权限检查（2026-04-17 P1-4: 原注释"业务模块对所有登录用户开放"让 67 条 requiredModule 形同虚设）
    // admin 自动通过（hasModule 内部已处理）
    // 非 admin 用户：检查是否有该模块任意级别的权限
    const ADMIN_ONLY_MODULES = ['users', 'roles', 'audit'];
    if (requiredModule) {
        if (ADMIN_ONLY_MODULES.includes(requiredModule) && !user?.is_admin) {
            return permissionDenied('module');
        }
        const hasDemoSnapshotAccess = Boolean(
            activeDemoSelection && allowsDemoSnapshotModule(requiredModule),
        );
        // 教程只展示固定预设数据；所有 API 仍由 sandbox interceptor 返回零副作用合同。
        // 因此普通账号可进入教程白名单里的真实页面外壳，但不能借此获得真实模块权限。
        const hasSandboxTutorialAccess = isSandbox && isSandboxAllowedPath(location.pathname);
        if (
            !ADMIN_ONLY_MODULES.includes(requiredModule)
            && !hasModule(requiredModule)
            && !hasDemoSnapshotAccess
            && !hasSandboxTutorialAccess
        ) {
            return permissionDenied('module');
        }
    }

    // 精确权限检查（仅管理功能使用）
    if (requiredPermission && !hasPermission(requiredPermission)) {
        return permissionDenied('permission');
    }

    return <>{children}</>;
}
