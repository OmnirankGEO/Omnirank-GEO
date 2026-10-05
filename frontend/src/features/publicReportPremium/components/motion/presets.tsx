/**
 * motion/presets — 动效预设(全部一次性、短促、不打断阅读)
 *
 * 纪律：
 *  - 根组件必须包 <MotionConfig reducedMotion="user">
 *  - 禁止无限循环/跑马灯/闪烁/鼠标跟随；仅入场一次性动效
 *  - prefers-reduced-motion 下所有动画(含 opacity)全部停用、内容直接终态
 *    —— reducedMotion="user" 只禁 transform/layout,opacity 需自行处理，
 *       因此所有变体经 useSectionFadeUp/useStaggerChild/MotionExpand 获取
 */
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { AnimatePresence, motion, useReducedMotion, type Variants } from 'framer-motion';

const STATIC_VARIANTS: Variants = {
    hidden: { opacity: 1, y: 0 },
    show: { opacity: 1, y: 0 },
};

/** 区块入场：一次性淡入上移(reduced-motion → 静态) */
export function useSectionFadeUp(): Variants {
    const reduce = useReducedMotion();
    if (reduce) return STATIC_VARIANTS;
    return {
        hidden: { opacity: 0, y: 14 },
        show: {
            opacity: 1,
            y: 0,
            transition: { duration: 0.35, ease: 'easeOut' },
        },
    };
}

/** 子元素短 stagger 父容器(reduced-motion → 无 stagger) */
export function useStaggerParent(): Variants {
    const reduce = useReducedMotion();
    if (reduce) return { hidden: {}, show: {} };
    return {
        hidden: {},
        show: { transition: { staggerChildren: 0.06 } },
    };
}

/** 子元素入场(reduced-motion → 静态) */
export function useStaggerChild(): Variants {
    const reduce = useReducedMotion();
    if (reduce) return STATIC_VARIANTS;
    return {
        hidden: { opacity: 0, y: 8 },
        show: { opacity: 1, y: 0, transition: { duration: 0.25, ease: 'easeOut' } },
    };
}

/**
 * MotionExpand — 展开/收起容器(height+opacity 动画；reduced-motion → 纯条件渲染，零动画)
 */
export function MotionExpand({ open, children }: { open: boolean; children: ReactNode }) {
    const reduce = useReducedMotion();
    if (reduce) {
        return open ? <>{children}</> : null;
    }
    return (
        <AnimatePresence initial={false}>
            {open && (
                <motion.div
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: 0.2 }}
                    className="overflow-hidden"
                >
                    {children}
                </motion.div>
            )}
        </AnimatePresence>
    );
}

/**
 * 一次性数字计数(reduced-motion 时直接终值)。
 * 仅用于展示动画，数值来源仍是 props(不改动数据)。
 */
export function useCountUp(target: number | null, durationMs = 600): number | null {
    const reduceMotion = useReducedMotion();
    const [display, setDisplay] = useState<number | null>(target);
    const fromRef = useRef(0);

    useEffect(() => {
        if (target === null) {
            setDisplay(null);
            return;
        }
        if (reduceMotion || durationMs <= 0) {
            setDisplay(target);
            return;
        }
        const from = fromRef.current;
        const start = performance.now();
        let raf = 0;
        const tick = (now: number) => {
            const t = Math.min(1, (now - start) / durationMs);
            const eased = 1 - Math.pow(1 - t, 3);
            setDisplay(Math.round(from + (target - from) * eased));
            if (t < 1) {
                raf = requestAnimationFrame(tick);
            } else {
                fromRef.current = target;
            }
        };
        raf = requestAnimationFrame(tick);
        return () => cancelAnimationFrame(raf);
    }, [target, durationMs, reduceMotion]);

    return display;
}
