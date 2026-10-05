/**
 * M3BackBanner — 旧版 / 工作台顶部 banner · 让代理 1 click 回 M3
 *
 * CTO-15.18 PM 干预 A.6(2026-04-28):
 * - M3 默认登录路由 + 顶部固定 button "切回旧版 →" 让代理 1 click 回 / · 双轨明示自然迁移
 * - 代理在 / 后看到顶部 banner "试试新版工作台 → /m3" · 1 click 清 optout 回 M3
 *
 * 显示条件:
 * - localStorage `omnirank_m3_optout='1'` 存在(代理之前主动切回过 · 隐含代理身份)
 * - 用户登录(有 user)且非 admin
 * - 本会话未点"稍后"
 * - 不显示给 public 路径
 *
 * 行为:
 * - 点 "试试新版" → 清 omnirank_m3_optout → navigate('/m3/sales/today')
 * - 点 "稍后" → 写 sessionStorage hide_m3_back_banner=1 → 本会话不再显示
 */
import { Link } from 'react-router-dom';
import { useState, useEffect } from 'react';
import { Sparkles, X } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';

const SESSION_HIDE_KEY = 'hide_m3_back_banner';

export function M3BackBanner() {
    const { user } = useAuth();
    const [hidden, setHidden] = useState<boolean>(false);
    const [optout, setOptout] = useState<boolean>(false);

    useEffect(() => {
        try {
            const sessionHidden = sessionStorage.getItem(SESSION_HIDE_KEY) === '1';
            const m3Optout = localStorage.getItem('omnirank_m3_optout') === '1';
            setHidden(sessionHidden);
            setOptout(m3Optout);
        } catch { /* ignore */ }
    }, []);

    if (hidden) return null;
    if (!optout) return null;
    if (!user) return null;
    if (user.is_admin) return null;

    const handleSwitchBack = () => {
        try { localStorage.removeItem('omnirank_m3_optout'); } catch { /* ignore */ }
        // 跳 M3 销售工作台(A.6 默认路由)
        window.location.href = '/m3/sales/today';
    };

    const handleHideBanner = () => {
        try { sessionStorage.setItem(SESSION_HIDE_KEY, '1'); } catch { /* ignore */ }
        setHidden(true);
    };

    return (
        <div
            role="region"
            aria-label="新版工作台提示"
            className="flex items-center gap-3 border-b border-primary/20 bg-primary/5 px-4 py-2 text-sm"
        >
            <Sparkles className="h-4 w-4 text-primary shrink-0" aria-hidden />
            <span className="flex-1 text-foreground">
                新版工作台已就绪 · 销售/交付一体化 · 9 步全程跟单
            </span>
            <button
                type="button"
                onClick={handleSwitchBack}
                className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1 text-xs font-medium text-primary-foreground hover:opacity-90 transition-opacity min-h-[32px]"
                aria-label="切到新版工作台"
            >
                试试新版 →
            </button>
            <Link
                to="/"
                onClick={handleHideBanner}
                className="text-xs text-muted-foreground hover:text-foreground"
                aria-label="本次不再显示"
            >
                稍后
            </Link>
            <button
                type="button"
                onClick={handleHideBanner}
                className="inline-flex items-center justify-center rounded-md p-1 text-muted-foreground hover:text-foreground"
                aria-label="关闭"
            >
                <X className="h-3.5 w-3.5" aria-hidden />
            </button>
        </div>
    );
}
