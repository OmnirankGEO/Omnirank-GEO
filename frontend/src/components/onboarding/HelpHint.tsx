/**
 * HelpHint · 就地情境帮助 (新手教程第 4 层:难懂处的"?"解释)
 *
 * 难懂的字段/指标旁放一个灰色小"?" · 桌面悬浮、移动端点击 → 弹 popover 给人话版详细解释。
 * - 跟 FeatureTooltip(沙盒强引导高亮)、TutorialVideoButton(看视频)区分: 这是日常常驻的就地解释
 * - 无依赖实现(项目没装 radix popover)· portal + fixed 定位 · 不被表格/卡片的 overflow 裁切
 * - 交互: 桌面悬浮 peek + 点击 pin;移动端点击 pin;点外面 / Esc / 移开关闭;弹层可停留阅读、可选中
 * - 无障碍: 按钮 aria-label + aria-expanded · 键盘可聚焦、Esc 关
 * - 沙盒态隐藏: 避免跟 Layer 1 spotlight 撞车 (沙盒里有强引导, 不需要这层)
 *
 * 用法:
 *   <HelpHint title="AI 出现率">这是指你的品牌被 AI 推荐到的概率…(支持 JSX)</HelpHint>
 */
import { ReactNode, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { HelpCircle } from 'lucide-react';
import { isSandboxActive } from '@/sandbox/sandboxState';

type Side = 'top' | 'bottom' | 'left' | 'right';

interface HelpHintProps {
    /** popover 标题 · 可选 */
    title?: string;
    /** popover 正文 · 人话解释 · 支持 JSX(可放小例子/列表/链接) */
    children: ReactNode;
    /** 期望弹出方向 · 默认 bottom · 空间不够会自动翻转 */
    side?: Side;
    /** "?"图标的无障碍标签 · 默认用 title 或 "查看说明" */
    label?: string;
    /** 额外 className · 贴合上下文(调字号/对齐) */
    className?: string;
}

const WIDTH = 288;
const EST_HEIGHT = 140;
const GAP = 8;

function pickSide(rect: DOMRect, preferred: Side): Side {
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const fits: Record<Side, boolean> = {
        top: rect.top >= EST_HEIGHT + GAP,
        bottom: vh - rect.bottom >= EST_HEIGHT + GAP,
        left: rect.left >= WIDTH + GAP,
        right: vw - rect.right >= WIDTH + GAP,
    };
    if (fits[preferred]) return preferred;
    for (const s of ['bottom', 'top', 'right', 'left'] as Side[]) if (fits[s]) return s;
    return preferred;
}

function cardStyle(rect: DOMRect, side: Side): React.CSSProperties {
    const vw = window.innerWidth;
    const clampLeft = (l: number) => Math.max(8, Math.min(vw - WIDTH - 8, l));
    switch (side) {
        case 'top':
            return { position: 'fixed', left: clampLeft(rect.left + rect.width / 2 - WIDTH / 2), top: rect.top - GAP, transform: 'translateY(-100%)', width: WIDTH };
        case 'left':
            return { position: 'fixed', left: rect.left - GAP, top: rect.top + rect.height / 2, transform: 'translate(-100%, -50%)', width: WIDTH };
        case 'right':
            return { position: 'fixed', left: rect.right + GAP, top: rect.top + rect.height / 2, transform: 'translateY(-50%)', width: WIDTH };
        case 'bottom':
        default:
            return { position: 'fixed', left: clampLeft(rect.left + rect.width / 2 - WIDTH / 2), top: rect.bottom + GAP, width: WIDTH };
    }
}

export function HelpHint({ title, children, side = 'bottom', label, className }: HelpHintProps) {
    const triggerRef = useRef<HTMLButtonElement>(null);
    const [open, setOpen] = useState(false);
    const pinnedRef = useRef(false); // 点击固定 · 不随移开关闭(移动端/想看久点)
    const [rect, setRect] = useState<DOMRect | null>(null);
    const [bestSide, setBestSide] = useState<Side>(side);
    const closeTimer = useRef<number | undefined>(undefined);
    const id = useId();

    const measure = useCallback(() => {
        if (!triggerRef.current) return;
        const r = triggerRef.current.getBoundingClientRect();
        setRect(r);
        setBestSide(pickSide(r, side));
    }, [side]);

    const show = useCallback(() => {
        window.clearTimeout(closeTimer.current);
        measure();
        setOpen(true);
    }, [measure]);

    const scheduleHide = useCallback(() => {
        if (pinnedRef.current) return;
        window.clearTimeout(closeTimer.current);
        closeTimer.current = window.setTimeout(() => setOpen(false), 200);
    }, []);

    const close = useCallback(() => {
        window.clearTimeout(closeTimer.current);
        pinnedRef.current = false;
        setOpen(false);
    }, []);

    useLayoutEffect(() => { if (open) measure(); }, [open, measure]);

    useEffect(() => {
        if (!open) return;
        const reposition = () => measure();
        window.addEventListener('scroll', reposition, true);
        window.addEventListener('resize', reposition);
        const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close(); };
        window.addEventListener('keydown', onKey);
        const onDocDown = (e: MouseEvent) => {
            const t = e.target as Node;
            if (triggerRef.current?.contains(t)) return;
            if (document.getElementById(id)?.contains(t)) return;
            close();
        };
        document.addEventListener('mousedown', onDocDown);
        return () => {
            window.removeEventListener('scroll', reposition, true);
            window.removeEventListener('resize', reposition);
            window.removeEventListener('keydown', onKey);
            document.removeEventListener('mousedown', onDocDown);
        };
    }, [open, measure, close, id]);

    useEffect(() => () => window.clearTimeout(closeTimer.current), []);

    // 沙盒态不显示 · 避免跟 Layer 1 spotlight 重叠
    if (isSandboxActive()) return null;

    const popover = open && rect ? createPortal(
        <div
            id={id}
            role="tooltip"
            style={cardStyle(rect, bestSide)}
            className="z-[60] rounded-lg border border-border bg-popover p-3 text-popover-foreground shadow-xl animate-in fade-in-0 zoom-in-95 duration-150"
            onMouseEnter={() => window.clearTimeout(closeTimer.current)}
            onMouseLeave={scheduleHide}
        >
            {title && <div className="mb-1 text-xs font-semibold text-foreground">{title}</div>}
            <div className="text-xs leading-5 text-muted-foreground [&_b]:font-medium [&_b]:text-foreground [&_strong]:font-medium [&_strong]:text-foreground">
                {children}
            </div>
        </div>,
        document.body,
    ) : null;

    return (
        <>
            <button
                ref={triggerRef}
                type="button"
                aria-label={label || title || '查看说明'}
                aria-expanded={open}
                className={`inline-flex shrink-0 cursor-help items-center justify-center align-middle text-muted-foreground/60 transition-colors hover:text-brand focus:text-brand focus:outline-none ${className ?? ''}`}
                onMouseEnter={show}
                onMouseLeave={scheduleHide}
                onFocus={show}
                onBlur={scheduleHide}
                onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    if (open && pinnedRef.current) { close(); }
                    else { pinnedRef.current = true; show(); }
                }}
            >
                <HelpCircle className="h-3.5 w-3.5" />
            </button>
            {popover}
        </>
    );
}

export default HelpHint;
