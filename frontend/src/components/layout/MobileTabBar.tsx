/**
 * GEO 业务页面 - 移动端底部导航栏
 * 仅在 < lg (1024px) 时显示
 * 模式参考: components/social/社媒底部导航.tsx
 */
import { useState } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { Home, Building2, Stethoscope, Calculator, Menu, X, UserCircle,
    History, Activity, Video, Settings, Users, ClipboardList } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useAuth } from '@/context/AuthContext';

interface SubItem {
    label: string;
    path: string;
    icon: React.ElementType;
    adminOnly?: boolean;
    module?: string;
}

interface NavTab {
    id: string;
    label: string;
    icon: React.ElementType;
    path?: string;          // 直达路径（无子菜单时）
    subItems?: SubItem[];   // 有子菜单时
}

const NAV_TABS: NavTab[] = [
    {
        id: 'home',
        label: '首页',
        icon: Home,
        path: '/',
    },
    {
        id: 'brand',
        label: '我的',
        icon: UserCircle,
        path: '/my-brand',
    },
    {
        id: 'diagnosis',
        label: '诊断',
        icon: Stethoscope,
        path: '/diagnosis/new',
    },
    {
        id: 'quote',
        label: '报价',
        icon: Calculator,
        path: '/pricing',
    },
    {
        id: 'more',
        label: '更多',
        icon: Menu,
        subItems: [
            { label: '历史记录', path: '/history', icon: History },
            { label: '监测中心', path: '/monitoring', icon: Activity },
            { label: '社媒操盘手', path: '/s', icon: Video },
            { label: '系统设置', path: '/settings', icon: Settings, adminOnly: true },
            { label: '用户管理', path: '/admin/users', icon: Users, adminOnly: true },
            { label: '审计日志', path: '/admin/audit', icon: ClipboardList, adminOnly: true },
        ],
    },
];

export function MobileTabBar() {
    const navigate = useNavigate();
    const location = useLocation();
    const { user } = useAuth();
    const isAdmin = user?.is_admin === true;
    const [showMore, setShowMore] = useState(false);

    const isActive = (tab: NavTab) => {
        if (tab.path) {
            if (tab.path === '/') return location.pathname === '/';
            return location.pathname.startsWith(tab.path);
        }
        // "更多" tab: 检查其子项是否命中
        return tab.subItems?.some(s => location.pathname.startsWith(s.path)) ?? false;
    };

    const handleTabClick = (tab: NavTab) => {
        if (tab.id === 'more') {
            setShowMore(!showMore);
            return;
        }
        setShowMore(false);
        if (tab.path) navigate(tab.path);
    };

    const handleSubItemClick = (path: string) => {
        setShowMore(false);
        navigate(path);
    };

    return (
        <>
            {/* 更多菜单遮罩 */}
            {showMore && (
                <div className="fixed inset-0 bg-black/30 z-40 lg:hidden" onClick={() => setShowMore(false)} />
            )}

            {/* 更多菜单面板 */}
            {showMore && (
                <div className="fixed bottom-16 left-0 right-0 z-50 lg:hidden bg-card border-t border-border rounded-t-2xl shadow-lg px-4 py-3 max-h-[60vh] overflow-y-auto">
                    <div className="flex items-center justify-between mb-3">
                        <span className="text-sm font-semibold text-foreground">更多功能</span>
                        <button onClick={() => setShowMore(false)} className="p-1 text-muted-foreground">
                            <X className="h-4 w-4" />
                        </button>
                    </div>
                    <div className="grid grid-cols-4 gap-3">
                        {NAV_TABS.find(t => t.id === 'more')?.subItems
                            ?.filter(s => !s.adminOnly || isAdmin)
                            .map(item => {
                                const Icon = item.icon;
                                const active = location.pathname.startsWith(item.path);
                                return (
                                    <button
                                        key={item.path}
                                        onClick={() => handleSubItemClick(item.path)}
                                        className={cn(
                                            'flex flex-col items-center gap-1.5 py-3 rounded-xl transition-colors',
                                            active ? 'bg-brand/10 text-brand' : 'text-muted-foreground active:bg-secondary'
                                        )}
                                    >
                                        <Icon className="h-5 w-5" />
                                        <span className="text-xs">{item.label}</span>
                                    </button>
                                );
                            })}
                    </div>
                </div>
            )}

            {/* 底部导航栏 */}
            <nav className="fixed bottom-0 left-0 right-0 z-50 lg:hidden bg-card border-t border-border">
                <div className="flex justify-around items-center h-16 px-1 max-w-lg mx-auto">
                    {NAV_TABS.map(tab => {
                        const Icon = tab.icon;
                        const active = isActive(tab);
                        return (
                            <button
                                key={tab.id}
                                onClick={() => handleTabClick(tab)}
                                className={cn(
                                    'flex flex-col items-center gap-0.5 px-3 py-1.5 rounded-lg transition-colors min-w-[56px]',
                                    active ? 'text-brand' : 'text-muted-foreground active:text-foreground'
                                )}
                            >
                                <Icon className={cn('h-5 w-5', active && 'text-brand')} />
                                <span className={cn('text-[11px]', active ? 'font-semibold text-brand' : 'font-normal')}>
                                    {tab.label}
                                </span>
                            </button>
                        );
                    })}
                </div>
                {/* iOS 安全区 */}
                <div className="h-[env(safe-area-inset-bottom)]" />
            </nav>
        </>
    );
}
