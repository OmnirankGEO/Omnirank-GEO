/**
 * OnboardingChecklist · Stage 1 Task 3 (2026-05-06)
 *
 * 右下角悬浮的 4 步任务卡 · 持续陪伴服务商跑通流程
 *
 * 显示条件 (3 个全满足才显):
 *   1) isAvailable (localStorage 可用)
 *   2) welcome_choice ∈ {'start','later'} (做过欢迎弹窗的非"never"选择)
 *   3) completed_steps.length < TOTAL_STEPS (未全部完成)
 *
 * 两种状态:
 *   - 收起 (默认): 56px 圆形按钮 + SVG 进度环 + 中央 X/4 数字
 *   - 展开: 360px 卡片 · 4 行任务列表 · 每行可点击跳转
 *
 * 完成庆祝:
 *   4/4 时触发 sonner toast + 5 秒后自动隐藏
 *   useRef 保证只触发一次, 防止 re-render 重复弹 toast
 *
 * 跳过引导:
 *   展开态底部 "跳过引导" → setWelcomeChoice('never') + 隐藏
 *
 * z-index 40 (Modal 是 50, 必须低于 Modal 避免遮蒙层)
 */
import { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { CheckCircle2, ChevronDown, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useOnboarding } from '@/context/OnboardingContext';
import { ONBOARDING_STEPS, TOTAL_STEPS } from './OnboardingStepDefinitions';
import { isAnyTooltipActive, TOOLTIP_ACTIVE_EVENT } from './tooltipActiveStore';
import { useIsMobile } from '@/hooks/use-mobile';
import { useSandboxState } from '@/sandbox/sandboxState';
import { setTutorialStage, type TutorialStage } from '@/sandbox/tutorialStage';
import { useScreenshotMode } from '@/sandbox/screenshotMode';

// 沙盒里 step_id → sidebar 阶段映射 · "去做"按钮切到对应阶段引导用户主动点 sidebar
const SANDBOX_SIDEBAR_STAGE_MAP: Record<string, TutorialStage> = {
    first_diagnosis: 'sidebar',
    first_quote: 'step2-sidebar',
    first_article_publish: 'step3-sidebar',
    first_monitoring: 'step4-sidebar',
};

export function OnboardingChecklist() {
    const navigate = useNavigate();
    const { state, setWelcomeChoice, isAvailable } = useOnboarding();
    // Stage 1 Batch 4 (2026-05-18) · 沙盒方案下 checklist 只在沙盒里显示
    // 退出沙盒 = 回真实工作台 = 不再有任何引导残留 (Layer 2 / 3 接管)
    const { isSandbox } = useSandboxState();
    // 2026-05-23 · 截图模式藏掉 · 拍真实工作台感觉的图时不应露任务卡圆球
    const screenshotMode = useScreenshotMode();

    // [2026-05-27] 移动端默认收起圆球态 · 避免完成 step 后展开挡屏
    //   (移动端 MobileSidebarCoach 已经引导下一步去哪 · checklist 展开多余)
    // PC 端保持原行为 · 默认展开让新代理看到完整任务列表
    const isMobile = useIsMobile();
    const [expanded, setExpanded] = useState(!isMobile);
    const [hidden, setHidden] = useState(false);
    const celebratedRef = useRef(false);

    // Stage 1 Batch 3 (2026-05-08) · FeatureTooltip 显示时自动收成圆球
    // 避免气泡 + checklist 在右下角视觉重叠 · 用户点完按钮 tooltip 消失后保留圆球态
    useEffect(() => {
        const sync = () => {
            if (isAnyTooltipActive()) setExpanded(false);
        };
        sync(); // mount 时先同步一次, 防止 tooltip 已经显示但还没派事件
        window.addEventListener(TOOLTIP_ACTIVE_EVENT, sync);
        return () => window.removeEventListener(TOOLTIP_ACTIVE_EVENT, sync);
    }, []);

    // Stage 1 Batch 4 (2026-05-18) · 圆球可拖动 y 轴 · 右边固定
    // 持久化到 localStorage · 避免页面跳转 / reload 后位置重置
    const BALL_Y_KEY = 'omnirank_checklist_y';
    const BALL_SIZE = 56;
    const SAFE_TOP = 64;    // 顶部留 sandbox banner 空间
    const SAFE_BOTTOM = 16;
    const [ballY, setBallY] = useState<number>(() => {
        try {
            const v = parseInt(localStorage.getItem(BALL_Y_KEY) || '', 10);
            return Number.isFinite(v) ? v : SAFE_TOP;
        } catch { return SAFE_TOP; }
    });
    const dragRef = useRef<{ dragging: boolean; offsetY: number }>({ dragging: false, offsetY: 0 });

    useEffect(() => {
        const onMove = (e: MouseEvent) => {
            if (!dragRef.current.dragging) return;
            const vh = window.innerHeight;
            const maxY = vh - BALL_SIZE - SAFE_BOTTOM;
            const newY = Math.max(SAFE_TOP, Math.min(maxY, e.clientY - dragRef.current.offsetY));
            setBallY(newY);
        };
        const onUp = () => {
            if (!dragRef.current.dragging) return;
            dragRef.current.dragging = false;
            try { localStorage.setItem(BALL_Y_KEY, String(ballY)); } catch { /* noop */ }
        };
        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', onUp);
        return () => {
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
        };
    }, [ballY]);

    // 展开方向自适应 · 球在屏幕上半 → 卡片向下 · 下半 → 向上
    const vh = typeof window !== 'undefined' ? window.innerHeight : 800;
    const openDownward = ballY + BALL_SIZE / 2 < vh / 2;

    // 每步完成时 checklist 自动展开 · 不自动收 · 等下个 spotlight 出现再被 tooltipActiveStore 收
    // 防止 user 跳转报告页时 6s timer 跑完, 用户错过看"下一步去哪"
    // [2026-05-27] 移动端不自动展开 · MobileSidebarCoach 已经引导下一步去哪 · 展开 checklist 多余且占屏
    const prevCompletedCount = useRef<number>(state.completed_steps.length);
    useEffect(() => {
        const cur = state.completed_steps.length;
        if (cur > prevCompletedCount.current && !isAnyTooltipActive() && !isMobile) {
            setExpanded(true);
        }
        prevCompletedCount.current = cur;
    }, [state.completed_steps.length, isMobile]);

    // Stage 1 Batch 4 (2026-05-18) · 重构 4 步后只能算"当前 step_id 列表"里的
    // 否则老 localStorage 残留 (enroll_client / fill_client_profile / first_publish)
    // 会让 7 ≥ 4 误判 isAllDone=true · 一进系统就弹"上手成功"
    const completed = ONBOARDING_STEPS.filter(s => state.completed_steps.includes(s.step_id)).length;
    const skipped = ONBOARDING_STEPS.filter(s => state.skipped_steps.includes(s.step_id)).length;
    const passed = completed + skipped;
    const isAllDone = passed >= TOTAL_STEPS;

    // 7/7 完成 → toast + 5 秒后隐藏
    // 注意:在 React.StrictMode 下组件会 mount/unmount/remount, useRef 值跨 mount 保持
    // 早期写法把 toast + timer 一起卡在 celebratedRef 后面 → StrictMode 第二次 mount 时
    // celebratedRef=true 不再启动新 timer, 5 秒后永远不消失。
    // 拆开:celebratedRef 只防 toast 重复, timer 每次 effect 重跑都重启
    //
    // Stage 1 Batch 4 fix (2026-05-18) · 庆祝必须 isSandbox=true 才触发
    // 否则退出沙盒后刷新, isAllDone 仍是 true → 每次刷新都弹 "上手成功!"
    useEffect(() => {
        if (!isSandbox || !isAllDone || hidden) return;

        // 仅首次进入 4/4 弹 toast
        if (!celebratedRef.current) {
            celebratedRef.current = true;
            void import('sonner').then(({ toast }) => {
                toast.success(`上手成功! ${TOTAL_STEPS} 步全部跑通`, { duration: 4000 });
            });
        }

        // timer 跟着 effect 生命周期, cleanup 会 cancel 上一次, remount 时重启
        const t = setTimeout(() => setHidden(true), 5000);
        return () => clearTimeout(t);
    }, [isSandbox, isAllDone, hidden]);

    const shouldShow = (
        isSandbox          // 只在沙盒态显示 · 真实工作台不再有引导残留
        && isAvailable
        && passed < TOTAL_STEPS
        // 沙盒本身是 entry point，不再依赖旧版欢迎选择状态。
    );

    // 沙盒外: 任何状态都不渲染 (球 / 庆祝卡都不要)
    // 否则退出沙盒后 isAllDone=true 会一直触发庆祝渲染 + toast
    if (!isSandbox) return null;
    // 截图模式藏 · 见 screenshotMode.ts
    if (screenshotMode) return null;
    // 已完成场景: shouldShow=false, 但还在 5 秒庆祝窗口期 → 仍然渲染
    // 用 hidden 兜底; 只要 hidden=true 就彻底不渲染
    if (hidden) return null;
    if (!shouldShow && !isAllDone) return null;

    const handleRowClick = (goto: string, stepId?: string) => {
        // 沙盒态下: 点"去做"不直接 navigate · 切到对应 sidebar 阶段引导用户主动点 sidebar
        if (isSandbox && stepId && SANDBOX_SIDEBAR_STAGE_MAP[stepId]) {
            setTutorialStage(SANDBOX_SIDEBAR_STAGE_MAP[stepId]);
            setExpanded(false); // 收起 checklist · sidebar spotlight 接力
            return;
        }
        // 真实工作台正常 navigate
        navigate(goto);
        setExpanded(false);
    };

    const handleSkip = () => {
        setWelcomeChoice('never');
        setHidden(true);
    };

    // ==== 收起态 · 56px 圆形按钮 + SVG 进度环 ====
    if (!expanded) {
        // SVG 进度环参数 · 进度 = (completed + skipped) / TOTAL
        const radius = 26;
        const circumference = 2 * Math.PI * radius;
        const progress = passed / TOTAL_STEPS;
        const dashOffset = circumference * (1 - progress);

        return (
            <button
                type="button"
                onMouseDown={(e) => {
                    // 仅左键启动拖动 · 记 offsetY (鼠标到球顶部的距离)
                    if (e.button !== 0) return;
                    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
                    dragRef.current = { dragging: true, offsetY: e.clientY - rect.top };
                }}
                onClick={(e) => {
                    // 拖动距离 > 5px 算拖动, 不触发展开 (避免拖完误展开)
                    if (Math.abs(e.clientY - ballY) > 5 && Math.abs(e.clientY - ballY - BALL_SIZE / 2) > 5) {
                        // 有可能是拖动 release · 但 dragRef 已 false · 这里粗略判断
                    }
                    setExpanded(true);
                }}
                aria-label={`上手任务 ${passed} / ${TOTAL_STEPS} · 点击展开 · 可拖动`}
                style={{ top: ballY, right: 24 }}
                className="
                    fixed z-40
                    h-14 w-14 rounded-full
                    bg-background border border-border shadow-lg
                    flex items-center justify-center
                    cursor-grab active:cursor-grabbing
                    transition-transform hover:scale-105
                    focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2
                "
            >
                {/* 进度环 SVG · 旋转 -90° 让起点在 12 点钟方向 */}
                <svg
                    className="absolute inset-0 -rotate-90"
                    width="56"
                    height="56"
                    viewBox="0 0 56 56"
                    aria-hidden="true"
                >
                    {/* 底圈 (灰) */}
                    <circle
                        cx="28"
                        cy="28"
                        r={radius}
                        fill="none"
                        stroke="hsl(var(--muted))"
                        strokeWidth="3"
                    />
                    {/* 进度圈 · 绿色(emerald-500) · 跟"已完成"行的对勾配色一致 */}
                    <circle
                        cx="28"
                        cy="28"
                        r={radius}
                        fill="none"
                        stroke="rgb(16 185 129)"
                        strokeWidth="3"
                        strokeDasharray={circumference}
                        strokeDashoffset={dashOffset}
                        strokeLinecap="round"
                        className="transition-[stroke-dashoffset] duration-300"
                    />
                </svg>
                {/* 中央数字 · completed+skipped / TOTAL */}
                <span className="text-xs font-semibold tabular-nums">
                    {passed}/{TOTAL_STEPS}
                </span>
            </button>
        );
    }

    // ==== 展开态 · 360px 卡片 · 位置跟随球 · 方向自适应不超屏 ====
    // 球在上半屏 → 卡片向下展开 (top 跟球对齐)
    // 球在下半屏 → 卡片向上展开 (bottom 跟球底部对齐)
    const cardStyle: React.CSSProperties = openDownward
        ? { top: ballY, right: 24 }
        : { bottom: vh - (ballY + BALL_SIZE), right: 24 };
    return (
        <div
            role="dialog"
            aria-label="上手任务清单"
            style={cardStyle}
            className="
                fixed z-40
                w-[360px] max-h-[60vh]
                bg-background border border-border rounded-xl shadow-2xl
                flex flex-col overflow-hidden
            "
        >
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b">
                <div className="flex items-center gap-2">
                    <Sparkles className="h-4 w-4 text-amber-500" />
                    <span className="text-sm font-medium">
                        上手任务 {passed}/{TOTAL_STEPS}
                    </span>
                </div>
                {/* 右上仅保留 "折叠" 按钮 · 不再放 X · 防误关闭 ·
                    永久关闭只能走底部 "跳过引导" 显式按钮 */}
                <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => setExpanded(false)}
                    aria-label="收起任务清单"
                    className="h-7 w-7"
                >
                    <ChevronDown className="h-4 w-4" />
                </Button>
            </div>

            {/* Body · 4 行任务 · 强制顺序 · 只有"下一个未完成 step"可点 */}
            <div className="flex-1 overflow-y-auto py-2">
                {(() => {
                    // 算出第一个未完成 (未 completed 且未 skipped) 的 step_id, 只有这个才显示"去做"
                    const nextPending = ONBOARDING_STEPS.find(
                        s => !state.completed_steps.includes(s.step_id) && !state.skipped_steps.includes(s.step_id)
                    );
                    return ONBOARDING_STEPS.map((step) => {
                    const isCompleted = state.completed_steps.includes(step.step_id);
                    const isSkipped = state.skipped_steps.includes(step.step_id);
                    const isPending = !isCompleted && !isSkipped;
                    const isNext = isPending && nextPending?.step_id === step.step_id;
                    const isLocked = isPending && !isNext;
                    return (
                        <div
                            key={step.step_id}
                            className="w-full flex items-start gap-3 px-4 py-2.5"
                        >
                            {/* 状态圆圈 · 完成=绿对勾, 跳过=灰对勾, 未做=数字 */}
                            <span
                                className={
                                    isCompleted
                                        ? 'flex h-5 w-5 items-center justify-center rounded-full bg-emerald-500 text-white shrink-0 mt-0.5'
                                        : isSkipped
                                          ? 'flex h-5 w-5 items-center justify-center rounded-full bg-muted-foreground/40 text-white shrink-0 mt-0.5'
                                          : 'flex h-5 w-5 items-center justify-center rounded-full bg-muted text-muted-foreground text-xs font-medium shrink-0 mt-0.5'
                                }
                            >
                                {isCompleted || isSkipped
                                    ? <CheckCircle2 className="h-3.5 w-3.5" />
                                    : step.index
                                }
                            </span>
                            {/* 文本 */}
                            <div className="flex-1 min-w-0">
                                <div className="flex items-center gap-1.5">
                                    <span
                                        className={
                                            isCompleted
                                                ? 'text-sm line-through text-muted-foreground'
                                                : isSkipped
                                                  ? 'text-sm line-through text-muted-foreground/70'
                                                  : 'text-sm font-semibold'
                                        }
                                    >
                                        {step.label}
                                    </span>
                                    {isSkipped && (
                                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
                                            已跳过
                                        </span>
                                    )}
                                </div>
                                <div className="text-xs text-muted-foreground mt-0.5">
                                    {step.description}
                                </div>
                            </div>
                            {/* 强制顺序: 只有"下一个未完成 step"显示"去做" · 后面的灰化"待解锁" */}
                            {isNext && (
                                <button
                                    type="button"
                                    onClick={() => handleRowClick(step.goto, step.step_id)}
                                    className="shrink-0 mt-0.5 px-2.5 py-1 rounded-md text-xs font-medium bg-amber-500 text-white hover:bg-amber-600 transition-colors focus:outline-none focus:ring-2 focus:ring-amber-500 focus:ring-offset-2"
                                >
                                    去做
                                </button>
                            )}
                            {isLocked && (
                                <span
                                    className="shrink-0 mt-0.5 px-2.5 py-1 rounded-md text-xs font-medium bg-muted text-muted-foreground/60 cursor-not-allowed"
                                    title="完成前面步骤才能解锁"
                                >
                                    待解锁
                                </span>
                            )}
                        </div>
                    );
                    });
                })()}
            </div>

            {/* Footer */}
            <div className="border-t px-4 py-2 flex justify-center">
                {isAllDone ? (
                    <span className="text-xs text-emerald-600">
                        已完成所有任务 · 即将隐藏
                    </span>
                ) : (
                    <button
                        type="button"
                        onClick={handleSkip}
                        className="
                            text-xs text-muted-foreground hover:text-foreground
                            transition-colors cursor-pointer
                            focus:outline-none focus:underline
                        "
                    >
                        跳过引导
                    </button>
                )}
            </div>
        </div>
    );
}

export default OnboardingChecklist;
