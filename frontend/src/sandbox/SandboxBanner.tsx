/**
 * SandboxBanner - 沙盒态顶部横幅
 * Stage 1 Batch 3 (2026-05-08)
 *
 * 显示规则:
 *   - isSandboxActive === true 才显示
 *   - 固定在 viewport 顶部, fixed top-0, z-50, 不抢 dialog 的 z-index
 *
 * 行为:
 *   - "退出教程" 按钮 → exitSandbox() · 全部 step 标完成 · reload
 */
import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog';
import { GraduationCap, X, Check, PartyPopper, BookOpen } from 'lucide-react';
import { useIsMobile } from '@/hooks/use-mobile';
import { FLOATING_Z } from '@/lib/floating-stack';
import { useSandboxState } from '@/sandbox/sandboxState';
import { resetTutorialStage } from '@/sandbox/tutorialStage';
import { resetSandboxQuoteState, resetSandboxWritingState } from '@/sandbox/mockData';
import { useOnboarding } from '@/context/OnboardingContext';
import { ONBOARDING_STEPS, TOTAL_STEPS } from '@/components/onboarding/OnboardingStepDefinitions';
import { fireSmallCelebration, fireBigCelebration } from '@/lib/confetti';
import { useScreenshotMode } from '@/sandbox/screenshotMode';

/** 沙盒"全部跑通过"localStorage 标记 · 用来在帮助中心隐藏"重走"卡片 / 真实工作台不再弹 */
const SANDBOX_COMPLETED_KEY = 'omnirank_sandbox_completed';

/** 每步完成的轻庆祝 · 打钩 + 进度点 + 小撒花 · 不遮挡操作 · 最后一步交给大终幕 */
function SandboxStepCelebration() {
    const { state } = useOnboarding();
    const completed = state.completed_steps;
    const prevRef = useRef<Set<string> | null>(null);
    const [celebrate, setCelebrate] = useState<{ index: number; label: string } | null>(null);
    const [fading, setFading] = useState(false);

    useEffect(() => {
        const cur = new Set(completed.filter(s => ONBOARDING_STEPS.some(st => st.step_id === s)));
        if (prevRef.current === null) { prevRef.current = cur; return; } // 首次挂载不庆祝(兼容脚本预置完成态)
        for (const st of ONBOARDING_STEPS) {
            if (cur.has(st.step_id) && !prevRef.current.has(st.step_id) && st.step_id !== 'first_monitoring') {
                setCelebrate({ index: st.index, label: st.label });
                setFading(false);
            }
        }
        prevRef.current = cur;
    }, [completed]);

    // 重走教程时, 步骤可能早已在 completed_steps 里(脚本/上次跑完留下的)· markStepCompleted 不产生变化 →
    // 上面的 diff 监听不会触发庆祝。这里额外监听显式"庆祝某步"事件, 保证重走也有彩带
    useEffect(() => {
        const handler = (e: Event) => {
            const stepId = (e as CustomEvent).detail?.stepId as string | undefined;
            const st = ONBOARDING_STEPS.find(s => s.step_id === stepId);
            if (st && st.step_id !== 'first_monitoring') {
                setCelebrate({ index: st.index, label: st.label });
                setFading(false);
            }
        };
        window.addEventListener('sandbox:step-celebrate', handler);
        return () => window.removeEventListener('sandbox:step-celebrate', handler);
    }, []);

    useEffect(() => {
        if (!celebrate) return;
        fireSmallCelebration();
        const tFade = window.setTimeout(() => setFading(true), 1900);
        const tHide = window.setTimeout(() => setCelebrate(null), 2400);
        return () => { window.clearTimeout(tFade); window.clearTimeout(tHide); };
    }, [celebrate]);

    if (!celebrate) return null;
    const total = ONBOARDING_STEPS.length;
    const left = total - celebrate.index;
    return (
        <div className={`pointer-events-none fixed inset-0 z-[200] flex items-center justify-center transition-opacity duration-500 ${fading ? 'opacity-0' : 'opacity-100'}`}>
            <div className="flex flex-col items-center gap-2.5 rounded-2xl border border-border bg-card px-10 py-7 shadow-2xl animate-in fade-in zoom-in-95 duration-300">
                <div className="flex h-14 w-14 items-center justify-center rounded-full bg-green-500/15 animate-in zoom-in-50 duration-500">
                    <Check className="h-7 w-7 text-green-500" strokeWidth={3} />
                </div>
                <div className="text-base font-semibold text-foreground">第 {celebrate.index} 步完成!</div>
                <div className="text-xs text-muted-foreground">{celebrate.label}</div>
                <div className="mt-1 flex gap-1.5">
                    {Array.from({ length: total }).map((_, i) => (
                        <span key={i} className={`h-2 w-2 rounded-full transition-colors ${i < celebrate.index ? 'bg-amber-500' : 'bg-muted'}`} />
                    ))}
                </div>
                {left > 0 && <div className="text-xs text-muted-foreground/80">还剩 {left} 步, 继续~</div>}
            </div>
        </div>
    );
}

/**
 * 退出沙盒终幕弹窗
 * - B-1 完整走完(4/4): 大撒花 · 标题"教程跑通了" · 按钮[打开帮助中心][好的开始工作]
 * - B-2 中途退出(<4):  标题"已退出教程 · 完成 X/4 步" · 按钮[去帮助中心][关闭]
 *
 * 这个弹窗显示时沙盒已 exit · 真实数据已经在等接管 · 用户点完按钮才 reload 让 UI 替换
 */
function SandboxExitModal({
    completedCount,
    onClose,
}: {
    completedCount: number;
    onClose: () => void;
}) {
    const navigate = useNavigate();
    const isFullCompletion = completedCount >= TOTAL_STEPS;

    // B-1 进场放大撒花
    useEffect(() => {
        if (isFullCompletion) fireBigCelebration();
    }, [isFullCompletion]);

    // 任何一个出口点都 reload · 让沙盒残留 UI 被真实数据替换
    const goHelp = () => {
        // 跳视频教程主页 · 再 reload 让真实接口接管(否则会停留在沙盒缓存)
        try {
            sessionStorage.setItem('omnirank_pending_redirect', '/help/home');
        } catch { /* noop */ }
        window.location.href = '/help/home';
    };

    const goWork = () => {
        onClose();
        // 进真实工作台 · reload 拿真品牌数据
        navigate('/');
        setTimeout(() => window.location.reload(), 50);
    };

    return (
        <Dialog open={true}>
            <DialogContent
                hideCloseButton
                onPointerDownOutside={(e) => e.preventDefault()}
                onEscapeKeyDown={(e) => e.preventDefault()}
                className="max-w-md gap-0 text-center"
            >
                {/* 顶部图标 · 区分 B-1 / B-2 */}
                <div className="flex justify-center mb-3">
                    <div className={`h-16 w-16 rounded-2xl flex items-center justify-center ${
                        isFullCompletion ? 'bg-amber-500/15' : 'bg-muted'
                    }`}>
                        {isFullCompletion
                            ? <PartyPopper className="h-8 w-8 text-amber-600" />
                            : <BookOpen className="h-8 w-8 text-muted-foreground" />}
                    </div>
                </div>

                <DialogTitle className="text-lg font-bold">
                    {isFullCompletion
                        ? '教程跑通了 · 现在去签真客户吧'
                        : `已退出教程 · 完成 ${completedCount}/${TOTAL_STEPS} 步`}
                </DialogTitle>
                <DialogDescription className="mt-2 px-2">
                    {isFullCompletion
                        ? `${TOTAL_STEPS}/${TOTAL_STEPS} 步全部跑通,真实工作台已就绪`
                        : '没跑完的步骤随时可以在帮助中心重走教程'}
                </DialogDescription>

                {/* 进度点 · 仅 B-1 显示 4 个琥珀点回顾 */}
                {isFullCompletion && (
                    <div className="mt-4 flex justify-center gap-2">
                        {Array.from({ length: TOTAL_STEPS }).map((_, i) => (
                            <span key={i} className="h-2.5 w-2.5 rounded-full bg-amber-500" />
                        ))}
                    </div>
                )}

                {/* B-1 提示帮助中心可重温 · B-2 提示帮助中心可重走 */}
                <p className="text-xs text-muted-foreground mt-4 px-2">
                    {isFullCompletion
                        ? '「帮助中心」→「视频教程」可重温视频或重走教程'
                        : '「帮助中心」→「视频教程」→「重走新手教程」可重头开始'}
                </p>

                {/* 双按钮 */}
                <div className="mt-5 grid grid-cols-2 gap-3">
                    <Button variant="outline" onClick={goHelp}>
                        {isFullCompletion ? '打开帮助中心' : '去帮助中心'}
                    </Button>
                    <Button
                        onClick={goWork}
                        className="bg-amber-500 hover:bg-amber-600 text-white"
                    >
                        {isFullCompletion ? '好的, 开始工作' : '关闭'}
                    </Button>
                </div>
            </DialogContent>
        </Dialog>
    );
}

export function SandboxBanner() {
    const { isSandbox, exit } = useSandboxState();
    const { isStepCompleted, setWelcomeChoice } = useOnboarding();
    const screenshotMode = useScreenshotMode();
    // [2026-05-27] 移动端只做文案/按钮瘦身 · 不动 z-index / 不重设计形态
    const isMobile = useIsMobile();
    // 退出终幕弹窗状态 · null = 不显示 · 数字 = 弹窗中展示的完成步数
    const [exitModal, setExitModal] = useState<number | null>(null);

    // 2026-05-23: 截图模式下整段顶部黄条 + 撒花 + 退出弹窗全藏 · 视觉上跟真实工作台一致
    if (screenshotMode) return null;
    if (!isSandbox && exitModal === null) return null;

    const handleExit = () => {
        // 2026-05-22 BUGFIX:不再循环 markStepCompleted 补标 · 否则会触发 SandboxStepCelebration
        // 监听到 completed_steps 新增, 在退出弹窗上叠加"第 X 步完成!"小撒花 (老板视频反馈)
        // 改用 welcome_choice='never' 关闭真实工作台的引导:
        //   - WelcomeChoiceModal 见 welcome_choice 有值 → 不弹
        //   - OnboardingChecklist 内部判断 welcome_choice ∈ {start,later} → 'never' 时不显示
        let completedCount = 0;
        for (const step of ONBOARDING_STEPS) {
            if (isStepCompleted(step.step_id)) completedCount += 1;
        }
        setWelcomeChoice('never');

        exit();
        // 重置 tutorial stage + 报价状态机 + 写作状态机 + 已学路径 · 下次"重走教程"从头开始
        resetTutorialStage();
        resetSandboxQuoteState();
        resetSandboxWritingState();
        try {
            localStorage.removeItem('omnirank_sandbox_quote_paths');
            // 清掉沙盒选中的演示客户(brand_id=111 是 mock)· 否则退出后真实页面仍停在"一路顺风"幻影客户上,
            // 诊断表单还会被预填沙盒数据 · 清掉后 reload 会重新解析到用户真实品牌
            localStorage.removeItem('omnirank_current_brand_id');
            localStorage.removeItem('currentProjectId');
            // 全程跑通才记 completed 标记 · 中途退出不算
            if (completedCount >= TOTAL_STEPS) {
                localStorage.setItem(SANDBOX_COMPLETED_KEY, '1');
            }
        } catch { /* noop */ }

        // 替代原来的 toast · 弹一个终幕 Dialog · 用户点按钮决定去哪
        setExitModal(completedCount);
    };

    return (
        <>
            {isSandbox && (
                <div
                    className="fixed top-0 left-0 right-0 bg-amber-500 text-white shadow-md"
                    style={{ zIndex: FLOATING_Z.SANDBOX_BANNER }}
                >
                    {/* [2026-05-27] 移动端 px-2 缩 padding · 文案/按钮短版 · 防 375px 屏挤爆/换行 */}
                    <div className={`max-w-7xl mx-auto flex items-center justify-between text-sm ${isMobile ? 'px-2 py-1.5' : 'px-4 py-2'}`}>
                        <div className="flex items-center gap-2 min-w-0">
                            <GraduationCap className="h-4 w-4 shrink-0" />
                            <span className={`font-medium truncate ${isMobile ? 'text-xs' : ''}`}>
                                {isMobile ? '教程模式 · 数据预设' : '教程模式 · 当前所有数据均为预设, 不会真消耗算力 / 不污染你的客户库'}
                            </span>
                        </div>
                        <Button
                            variant="ghost"
                            size="sm"
                            onClick={handleExit}
                            className={`text-white hover:bg-amber-600 hover:text-white shrink-0 ${isMobile ? 'h-7 px-2 text-xs' : 'h-7'}`}
                        >
                            <X className="h-3.5 w-3.5 mr-1" />
                            {isMobile ? '退出' : '退出教程, 进真实工作台'}
                        </Button>
                    </div>
                </div>
            )}
            {/* 2026-05-22: 沙盒退出后立即 unmount · 防止后续 state 变化触发"第 X 步完成"庆祝叠加 */}
            {isSandbox && <SandboxStepCelebration />}
            {exitModal !== null && (
                <SandboxExitModal
                    completedCount={exitModal}
                    onClose={() => setExitModal(null)}
                />
            )}
        </>
    );
}

export default SandboxBanner;
