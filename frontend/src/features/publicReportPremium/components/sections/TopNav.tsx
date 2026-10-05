/**
 * TopNav — 顶部导航
 * 品牌 + 目录锚点 + 下载/打印(真实 window.print) + 返回顶部 + "数据怎么看"
 * 移动端折叠菜单(按钮可键盘操作,Esc 关闭)；打印时整栏隐藏。
 */
import { useEffect, useRef, useState } from 'react';
import { ArrowUp, BarChart3, CircleHelp, Menu, Moon, Printer, Sun, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { PublicWhitelabel } from '../../contract/types';
import type { PublicReportTheme } from '../ReportPage';

const NAV_ITEMS = [
    { href: '#overview', label: '概览' },
    { href: '#funnel', label: '决策漏斗' },
    { href: '#platforms', label: '平台表现' },
    { href: '#findings', label: '关键发现' },
    { href: '#evidence', label: '证据' },
    { href: '#actions', label: '行动建议' },
    { href: '#methodology', label: '方法说明' },
] as const;

function isSafeLogoUrl(value: string | null): value is string {
    if (!value) return false;
    const trimmed = value.trim();
    // 相对路径(同源)直接放行；绝对 URL 仅允许 https:(防 javascript:/data:/明文 http:)
    if (trimmed.startsWith('/')) return true;
    try {
        const url = new URL(trimmed, window.location.origin);
        return url.protocol === 'https:';
    } catch {
        return false;
    }
}

function BrandMark({
    whitelabel,
    brandingStatus,
}: {
    whitelabel: PublicWhitelabel | null;
    brandingStatus: 'approved_whitelabel' | 'platform' | null;
}) {
    const [logoFailed, setLogoFailed] = useState(false);
    const approved = brandingStatus === 'approved_whitelabel';
    const logoUrl = whitelabel?.logoUrl ?? null;
    const approvedLogo = approved && isSafeLogoUrl(logoUrl);
    const approvedName = approved ? whitelabel?.productName ?? whitelabel?.companyName : null;

    useEffect(() => {
        setLogoFailed(false);
    }, [logoUrl]);

    if (approvedLogo && !logoFailed) {
        return (
            <span className="flex min-w-0 items-center gap-2" data-testid="approved-whitelabel-brand">
                <img
                    src={logoUrl}
                    alt=""
                    className="h-7 w-auto max-w-[48px] shrink-0 object-contain sm:max-w-[128px]"
                    onError={() => setLogoFailed(true)}
                />
                {approvedName && (
                    <span
                        className="max-w-[72px] truncate text-sm font-semibold text-slate-900 sm:max-w-[180px]"
                        title={approvedName}
                    >
                        {approvedName}
                    </span>
                )}
            </span>
        );
    }
    return (
        <span className="flex min-w-0 items-center gap-2">
            {approvedName ? (
                <span className="max-w-[92px] truncate text-sm font-semibold text-slate-900 sm:max-w-[180px]" title={approvedName}>
                    {approvedName}
                </span>
            ) : (
                <>
                    <span className="flex h-8 w-8 items-center justify-center rounded-full bg-slate-900" aria-hidden="true">
                        <BarChart3 className="h-4 w-4 text-white" />
                    </span>
                    <span className="max-w-[96px] truncate text-sm font-semibold text-slate-900 sm:max-w-none">GEO 诊断报告</span>
                </>
            )}
        </span>
    );
}

export function TopNav({
    whitelabel,
    brandingStatus,
    onOpenHelp,
    theme,
    onThemeChange,
}: {
    whitelabel: PublicWhitelabel | null;
    brandingStatus: 'approved_whitelabel' | 'platform' | null;
    onOpenHelp: (invoker?: HTMLElement) => void;
    theme: PublicReportTheme;
    onThemeChange: (theme: PublicReportTheme) => void;
}) {
    const [menuOpen, setMenuOpen] = useState(false);
    const [showBackTop, setShowBackTop] = useState(false);
    const menuToggleRef = useRef<HTMLButtonElement | null>(null);

    useEffect(() => {
        const onScroll = () => setShowBackTop(window.scrollY > 480);
        onScroll();
        window.addEventListener('scroll', onScroll, { passive: true });
        return () => window.removeEventListener('scroll', onScroll);
    }, []);

    useEffect(() => {
        if (!menuOpen) return;
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') setMenuOpen(false);
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [menuOpen]);

    const handlePrint = () => {
        // 真实打印行为(浏览器打印/另存 PDF)，非假按钮
        window.print();
    };

    const scrollTop = () => window.scrollTo({
        top: 0,
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
    });

    return (
        <>
            <nav
                aria-label="报告导航"
                className="sticky top-0 z-40 border-b border-slate-200 bg-white/95 backdrop-blur print:hidden"
            >
                <div className="mx-auto flex h-14 w-full max-w-[1320px] items-center justify-between gap-3 px-4 sm:px-6 lg:px-10">
                    <a href="#overview" className="min-w-0 shrink focus-visible:outline-2 focus-visible:outline-sky-600">
                        <BrandMark whitelabel={whitelabel} brandingStatus={brandingStatus} />
                    </a>

                    {/* 桌面锚点 */}
                    <div className="hidden items-center gap-1 xl:flex" role="list">
                        {NAV_ITEMS.map((item) => (
                            <a
                                key={item.href}
                                href={item.href}
                                className="rounded-md px-2.5 py-1.5 text-sm text-slate-600 hover:bg-slate-100 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-sky-600"
                            >
                                {item.label}
                            </a>
                        ))}
                    </div>

                    <div className="flex items-center gap-1.5">
                        <button
                            type="button"
                            onClick={() => onThemeChange(theme === 'light' ? 'dark' : 'light')}
                            aria-label={theme === 'light' ? '切换为深色报告' : '切换为浅色报告'}
                            title={theme === 'light' ? '切换为深色报告' : '切换为浅色报告'}
                            className="inline-flex min-h-9 min-w-9 items-center justify-center rounded-md text-slate-600 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-sky-600"
                        >
                            {theme === 'light'
                                ? <Moon className="h-4 w-4" aria-hidden="true" />
                                : <Sun className="h-4 w-4" aria-hidden="true" />}
                        </button>
                        <button
                            type="button"
                            onClick={(e) => onOpenHelp(e.currentTarget)}
                            className="hidden min-h-9 items-center gap-1.5 rounded-md px-2.5 text-sm text-slate-600 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-sky-600 sm:inline-flex"
                        >
                            <CircleHelp className="h-4 w-4" aria-hidden="true" />
                            数据怎么看
                        </button>
                        <button
                            type="button"
                            onClick={handlePrint}
                            aria-label="下载 / 打印"
                            title="下载或打印报告"
                            className="inline-flex min-h-9 min-w-9 items-center justify-center gap-1.5 rounded-md bg-slate-900 px-2 text-sm font-medium text-white hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 sm:px-3"
                        >
                            <Printer className="h-4 w-4" aria-hidden="true" />
                            <span className="hidden sm:inline">下载 / 打印</span>
                        </button>
                        <button
                            type="button"
                            ref={menuToggleRef}
                            aria-label={menuOpen ? '关闭菜单' : '打开菜单'}
                            aria-expanded={menuOpen}
                            onClick={() => setMenuOpen((v) => !v)}
                            className="inline-flex min-h-9 min-w-9 items-center justify-center rounded-md text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-sky-600 xl:hidden"
                        >
                            {menuOpen ? <X className="h-5 w-5" aria-hidden="true" /> : <Menu className="h-5 w-5" aria-hidden="true" />}
                        </button>
                    </div>
                </div>

                {/* 移动端折叠菜单 */}
                {menuOpen && (
                    <div className="border-t border-slate-100 bg-white xl:hidden">
                        <div className="mx-auto grid w-full max-w-[1320px] gap-1 px-4 py-3 sm:px-6">
                            {NAV_ITEMS.map((item) => (
                                <a
                                    key={item.href}
                                    href={item.href}
                                    onClick={() => setMenuOpen(false)}
                                    className="rounded-md px-3 py-2 text-sm text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-sky-600"
                                >
                                    {item.label}
                                </a>
                            ))}
                            <button
                                type="button"
                                onClick={() => {
                                    setMenuOpen(false);
                                    // 菜单项随菜单关闭卸载，焦点还给常驻的菜单开关按钮
                                    onOpenHelp(menuToggleRef.current ?? undefined);
                                }}
                                className="rounded-md px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-sky-600"
                            >
                                数据怎么看
                            </button>
                        </div>
                    </div>
                )}
            </nav>

            {/* 返回顶部 */}
            <button
                type="button"
                onClick={scrollTop}
                aria-label="返回顶部"
                className={cn(
                    'fixed bottom-5 right-5 z-40 inline-flex h-10 w-10 items-center justify-center rounded-full border border-slate-200 bg-white text-slate-600 shadow-md transition-opacity motion-reduce:transition-none hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-sky-600 print:hidden',
                    showBackTop ? 'opacity-100' : 'pointer-events-none opacity-0',
                )}
            >
                <ArrowUp className="h-5 w-5" aria-hidden="true" />
            </button>
        </>
    );
}
