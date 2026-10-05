/**
 * SandboxIntroModal · 沙盒首次进入 2 页 Wizard
 * Stage 1 Batch 4 (2026-05-18)
 *
 * 显示规则:
 *   - 沙盒态 + 首次显示 (omnirank_sandbox_intro_shown !== '1') · tutorialStage='modal'
 *   - 关闭后 tutorialStage 切到 'sidebar' · 让 sidebar 上的"品牌体检" spotlight 接力
 *
 * 2 页内容:
 *   页 1 · 欢迎 + 3 条安全感 + "先看流程总览 →"
 *   页 2 · 4 步流程总览 + "← 上一步" / "我准备好了, 开始第一步"
 *
 * "我先随便逛逛" 逃生口 · 任意页都能点 · exitSandbox 关闭
 */
import { useEffect, useState } from 'react';
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/hooks/use-mobile';
import {
    GraduationCap, ShieldCheck, Sparkles,
    Stethoscope, Calculator, BookOpen, Activity,
    ArrowRight, ArrowLeft,
} from 'lucide-react';
import { isSandboxActive } from './sandboxState';
import { setTutorialStage } from './tutorialStage';
import { useScreenshotMode } from './screenshotMode';

const INTRO_SHOWN_KEY = 'omnirank_sandbox_intro_shown';

function hasIntroBeenShown(): boolean {
    try { return localStorage.getItem(INTRO_SHOWN_KEY) === '1'; }
    catch { return true; }
}

function markIntroShown(): void {
    try { localStorage.setItem(INTRO_SHOWN_KEY, '1'); }
    catch { /* noop */ }
}

const STEPS_OVERVIEW = [
    {
        icon: Stethoscope,
        title: '1 · 品牌体检',
        summary: '填品牌名 + 关键词, AI 5 分钟出诊断报告',
        detail: '看品牌在 AI 搜索里的可见度',
    },
    {
        icon: Calculator,
        title: '2 · 出报价方案',
        summary: '基于体检结果给客户生成报价单',
        detail: '生成选词链接发给客户 · 客户选完后系统自动定价签约',
    },
    {
        icon: BookOpen,
        title: '3 · 写文章 + 发布',
        summary: 'AI 生成 GEO 优化文章 → 加发布购物车 → 选平台投放',
        detail: '一站式内容生产 + 多平台发布',
    },
    {
        icon: Activity,
        title: '4 · 加监测追踪',
        summary: '加监测词, 看 AI 推荐次数变化趋势',
        detail: '数据驱动下一轮优化',
    },
];

export function SandboxIntroModal() {
    const [open, setOpen] = useState(false);
    const [page, setPage] = useState<1 | 2>(1);
    const [isSandbox, setIsSandbox] = useState<boolean>(isSandboxActive);
    const screenshotMode = useScreenshotMode();
    // [2026-05-27] 移动端走全屏 Dialog · 底部按钮 sticky · PC 行为不动
    const isMobile = useIsMobile();

    useEffect(() => {
        const sync = () => setIsSandbox(isSandboxActive());
        window.addEventListener('sandbox:change', sync);
        window.addEventListener('storage', sync);
        return () => {
            window.removeEventListener('sandbox:change', sync);
            window.removeEventListener('storage', sync);
        };
    }, []);

    useEffect(() => {
        if (isSandbox && !hasIntroBeenShown()) {
            setOpen(true);
            setPage(1);
        }
    }, [isSandbox]);

    // 2026-05-23 截图模式整段不弹 (拍摄时不应被引导窗口干扰)
    if (screenshotMode) return null;
    if (!isSandbox || !open) return null;

    const handleFinish = () => {
        markIntroShown();
        setOpen(false);
        // [2026-05-27 撤回 step1-A · 改 B 方案] 跨端统一走 sidebar stage
        // 移动端的 sidebar 引导由 MobileSidebarCoach 接管(2 阶段:点汉堡 → 点 sidebar 项)
        setTutorialStage('sidebar');
    };

    return (
        <Dialog open={open} onOpenChange={(v) => { if (!v) handleFinish(); }}>
            <DialogContent className={cn(
                'gap-0 [&>button]:hidden',
                isMobile
                    /* [2026-05-27] 沙盒态 banner z-180 fixed top-0 占 36px ·
                       全屏 Dialog 自己 pt 要加 banner 高度 + safe-area-top notch
                       否则 Dialog 头部内容(标题等)被 banner 盖 */
                    ? 'w-screen h-[100dvh] max-w-none rounded-none overflow-y-auto flex flex-col pb-[max(1rem,env(safe-area-inset-bottom))] pt-[calc(env(safe-area-inset-top)+2.75rem)]'
                    : 'max-w-xl'
            )}>
                {/* ===== 页 1 · 欢迎 + 安全感 ===== */}
                {page === 1 && (
                    <>
                        <div className="flex items-center justify-center pt-6 pb-3">
                            <div className="h-14 w-14 rounded-2xl bg-amber-100 dark:bg-amber-500/20 flex items-center justify-center">
                                <GraduationCap className="h-7 w-7 text-amber-600" />
                            </div>
                        </div>

                        <DialogTitle className="text-center text-lg font-bold tracking-tight">
                            欢迎进入教程模式
                        </DialogTitle>
                        <DialogDescription className="text-center text-sm text-muted-foreground mt-1 px-4">
                            接下来 4 步, 带你跑通 OmniRank 给客户做 GEO 服务的全流程
                        </DialogDescription>

                        <div className="my-4 mx-4 rounded-xl border border-amber-500/20 bg-amber-500/5 p-3 space-y-2">
                            <div className="flex items-start gap-2 text-xs">
                                <ShieldCheck className="h-4 w-4 text-amber-600 mt-0.5 shrink-0" />
                                <span><strong>不会真消耗算力</strong> · 教程里所有数据都是预设的演示数据</span>
                            </div>
                            <div className="flex items-start gap-2 text-xs">
                                <ShieldCheck className="h-4 w-4 text-amber-600 mt-0.5 shrink-0" />
                                <span><strong>不会污染你的真实客户库</strong> · 退出教程后真实工作台一切如旧</span>
                            </div>
                            <div className="flex items-start gap-2 text-xs">
                                <Sparkles className="h-4 w-4 text-amber-600 mt-0.5 shrink-0" />
                                <span><strong>演示客户:一路顺风出行服务</strong> · 用真实跑过的 GEO 诊断报告做演示 (26 分 / 危急级)</span>
                            </div>
                        </div>

                        {/* 2026-05-22: 删除 "我先随便逛逛" 逃生口 ·
                            前置 WelcomeChoiceModal 已经让用户做过明示选择 · 此处再放逃生口反而矛盾
                            [2026-05-27] 移动端 mt-auto 推按钮到底 · 配合 DialogContent flex flex-col 全屏 */}
                        <div className={cn(
                            'px-4 pb-4 flex items-center justify-end',
                            isMobile && 'mt-auto pt-3 border-t bg-background sticky bottom-0'
                        )}>
                            <Button onClick={() => setPage(2)} className="bg-amber-500 hover:bg-amber-600 text-white min-h-11">
                                先看流程总览
                                <ArrowRight className="h-3.5 w-3.5 ml-1" />
                            </Button>
                        </div>
                    </>
                )}

                {/* ===== 页 2 · 4 步流程总览 ===== */}
                {page === 2 && (
                    <>
                        <div className="pt-5 pb-2 px-5">
                            <DialogTitle className="text-base font-bold tracking-tight">
                                接下来 4 步, 教你怎么用 OmniRank 服务一个客户
                            </DialogTitle>
                            <DialogDescription className="text-xs text-muted-foreground mt-1">
                                教程全程过完 30 分钟 · 真实使用时一个客户的服务周期 1-2 周
                            </DialogDescription>
                        </div>

                        <div className="px-4 pb-3 space-y-2">
                            {STEPS_OVERVIEW.map((step) => (
                                <div
                                    key={step.title}
                                    className="flex items-start gap-3 p-3 rounded-lg border border-border bg-muted/20"
                                >
                                    <div className="h-8 w-8 rounded-lg bg-amber-500/15 flex items-center justify-center shrink-0">
                                        <step.icon className="h-4 w-4 text-amber-600" />
                                    </div>
                                    <div className="flex-1 min-w-0">
                                        <div className="text-sm font-semibold leading-5">{step.title}</div>
                                        <div className="text-xs text-foreground/80 mt-0.5 leading-4">{step.summary}</div>
                                        <div className="text-[11px] text-muted-foreground mt-0.5 leading-4">{step.detail}</div>
                                    </div>
                                </div>
                            ))}
                        </div>

                        {/* [2026-05-27] 移动端 mt-auto + sticky bottom · 防按钮被 Safari 地址栏挡 */}
                        <div className={cn(
                            'px-4 pb-4 flex items-center justify-between border-t pt-3',
                            isMobile && 'mt-auto bg-background sticky bottom-0'
                        )}>
                            <Button variant="outline" size={isMobile ? 'default' : 'sm'} onClick={() => setPage(1)} className={cn(isMobile && 'min-h-11')}>
                                <ArrowLeft className="h-3.5 w-3.5 mr-1" />
                                上一步
                            </Button>
                            <Button onClick={handleFinish} className={cn('bg-amber-500 hover:bg-amber-600 text-white', isMobile && 'min-h-11')}>
                                我准备好了, 开始第一步
                                <ArrowRight className="h-3.5 w-3.5 ml-1" />
                            </Button>
                        </div>
                    </>
                )}
            </DialogContent>
        </Dialog>
    );
}

export default SandboxIntroModal;
