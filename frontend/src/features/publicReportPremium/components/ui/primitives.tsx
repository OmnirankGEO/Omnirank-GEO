/**
 * ui/primitives — 公开报告 UI 原语
 * 视觉纪律：白/浅灰页面 + 深墨摘要区；蓝/青绿/橙克制表达状态；
 * 圆角 ≤8px；状态不只靠颜色(文本/图标双编码)；缺失值显示 "—"。
 */
import type { ReactNode } from 'react';
import { Minus } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { EvidenceLevel, EvidenceVerdict } from '../../contract/types';

// ---------------------------------------------------------------------------
// Section — 页面区块(锚点 + 标题层级)
// ---------------------------------------------------------------------------

export function Section({
    id,
    eyebrow,
    title,
    subtitle,
    children,
    className,
}: {
    id: string;
    eyebrow?: string;
    title: string;
    subtitle?: string;
    children: ReactNode;
    className?: string;
}) {
    return (
        <section
            id={id}
            aria-labelledby={`${id}-title`}
            className={cn('scroll-mt-20 py-8 sm:py-10', className)}
        >
            <div className="mb-5">
                {eyebrow && (
                    <p className="mb-1 text-xs font-semibold uppercase tracking-wider text-sky-700">
                        {eyebrow}
                    </p>
                )}
                <h2 id={`${id}-title`} className="text-xl font-bold text-slate-900 sm:text-2xl">
                    {title}
                </h2>
                {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
            </div>
            {children}
        </section>
    );
}

// ---------------------------------------------------------------------------
// 缺失值
// ---------------------------------------------------------------------------

export function MissingValue({ label = '—' }: { label?: string }) {
    return (
        <span className="text-slate-400" aria-label="暂无数据">
            {label}
        </span>
    );
}

/** 数值或 "—"(缺失值不是 0) */
export function MetricText({
    value,
    suffix,
    className,
}: {
    value: number | string | null;
    suffix?: string;
    className?: string;
}) {
    if (value === null || value === undefined) {
        return (
            <span className={className}>
                <MissingValue />
            </span>
        );
    }
    // 中文单位(个/条/周…)前加空格；% 与 /100 类符号紧贴(评审 P2-5 排版口径)
    const needsSpace = suffix !== undefined && /[\u4e00-\u9fff]/.test(suffix);
    return (
        <span className={className}>
            {value}
            {suffix && (
                <span className="text-[0.75em] opacity-70">
                    {needsSpace ? ' ' : ''}
                    {suffix}
                </span>
            )}
        </span>
    );
}

// ---------------------------------------------------------------------------
// 等级徽章(颜色由后端 colorHex 驱动；无颜色时中性)
// ---------------------------------------------------------------------------

export function LevelBadge({
    label,
    colorHex,
    size = 'md',
}: {
    label: string;
    colorHex: string | null;
    size?: 'sm' | 'md' | 'lg';
}) {
    const sizeClass =
        size === 'lg'
            ? 'px-3 py-1 text-base'
            : size === 'sm'
              ? 'px-2 py-0.5 text-xs'
              : 'px-2.5 py-1 text-sm';
    return (
        <span
            className={cn('inline-flex items-center gap-1.5 rounded-md font-semibold', sizeClass)}
            style={
                colorHex
                    ? { backgroundColor: colorHex, color: '#ffffff' }
                    : { backgroundColor: '#334155', color: '#ffffff' }
            }
        >
            {label}
        </span>
    );
}

// ---------------------------------------------------------------------------
// 证据等级标签(主文案直接说人话；A/B/C 字母口径见"方法说明")
// ---------------------------------------------------------------------------

const EVIDENCE_LEVEL_STYLE: Record<EvidenceLevel, { label: string; className: string }> = {
    A: { label: '直接证据', className: 'bg-emerald-50 text-emerald-700 border-emerald-200' },
    B: { label: '参考证据', className: 'bg-sky-50 text-sky-700 border-sky-200' },
    C: { label: '待验证线索', className: 'bg-slate-100 text-slate-600 border-slate-200' },
};

export function EvidenceLevelTag({ level }: { level: EvidenceLevel | null }) {
    if (!level) {
        return (
            <span className="inline-flex items-center rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[11px] text-slate-400">
                未评级
            </span>
        );
    }
    const style = EVIDENCE_LEVEL_STYLE[level];
    return (
        <span
            className={cn(
                'inline-flex items-center rounded border px-1.5 py-0.5 text-[11px] font-medium',
                style.className,
            )}
        >
            {style.label}
        </span>
    );
}

// ---------------------------------------------------------------------------
// 判定标签(统一观测 vNext 口径 · 颜色+文字双编码)
// 措辞:提及(正面措辞) / 提及(带条件措辞) / 列入备选 / 仅提到 / 只给标准 /
//      证据不足拒答 / 风险拒答 / 未提到 / 品牌混淆 / 引擎异常 / 无有效回答
// ---------------------------------------------------------------------------

const VERDICT_STYLE: Record<
    EvidenceVerdict,
    { label: string; className: string; dot: string }
> = {
    recommended: {
        label: '提及(正面措辞)',
        className: 'bg-emerald-50 text-emerald-700 border-emerald-200',
        dot: 'bg-emerald-500',
    },
    conditionally_recommended: {
        label: '提及(带条件措辞)',
        className: 'bg-teal-50 text-teal-700 border-teal-200',
        dot: 'bg-teal-500',
    },
    candidate: {
        label: '列入备选',
        className: 'bg-sky-50 text-sky-700 border-sky-200',
        dot: 'bg-sky-500',
    },
    mentioned: {
        label: '仅提到',
        className: 'bg-amber-50 text-amber-700 border-amber-200',
        dot: 'bg-amber-500',
    },
    criteria_only: {
        label: '只给标准',
        className: 'bg-slate-100 text-slate-600 border-slate-300',
        dot: 'bg-slate-500',
    },
    refused_no_evidence: {
        label: '证据不足拒答',
        className: 'bg-orange-50 text-orange-700 border-orange-200',
        dot: 'bg-orange-400',
    },
    refused_risk: {
        label: '风险拒答',
        className: 'bg-rose-50 text-rose-700 border-rose-200',
        dot: 'bg-rose-500',
    },
    not_mentioned: {
        label: '未提到',
        className: 'bg-orange-50 text-orange-700 border-orange-200',
        dot: 'bg-orange-500',
    },
    brand_confused: {
        label: '品牌混淆',
        className: 'bg-amber-50 text-amber-800 border-amber-300',
        dot: 'bg-amber-600',
    },
    engine_error: {
        label: '引擎异常',
        className: 'bg-slate-100 text-slate-500 border-slate-200',
        dot: 'bg-slate-400',
    },
    no_answer: {
        label: '无有效回答',
        className: 'bg-slate-100 text-slate-500 border-slate-200',
        dot: 'bg-slate-400',
    },
};

export function VerdictTag({ verdict }: { verdict: EvidenceVerdict }) {
    const style = VERDICT_STYLE[verdict];
    return (
        <span
            className={cn(
                'inline-flex items-center gap-1.5 whitespace-nowrap rounded border px-1.5 py-0.5 text-[11px] font-medium',
                style.className,
            )}
        >
            <span aria-hidden="true" className={cn('h-1.5 w-1.5 rounded-full', style.dot)} />
            {style.label}
        </span>
    );
}

// ---------------------------------------------------------------------------
// EmptyBlock — 区块空态/未开放占位(诚实呈现，不伪造)
// ---------------------------------------------------------------------------

export function EmptyBlock({
    title = '暂无数据',
    message,
    tone = 'muted',
}: {
    title?: string;
    message?: string;
    tone?: 'muted' | 'info';
}) {
    return (
        <div
            className={cn(
                'rounded-lg border border-dashed px-5 py-8 text-center',
                tone === 'muted'
                    ? 'border-slate-200 bg-slate-50/60'
                    : 'border-sky-200 bg-sky-50/50',
            )}
        >
            <Minus className="mx-auto mb-2 h-5 w-5 text-slate-300" aria-hidden="true" />
            <p className="text-sm font-medium text-slate-600">{title}</p>
            {message && <p className="mx-auto mt-1 max-w-xl text-xs leading-5 text-slate-500">{message}</p>}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Card — 单层卡片(禁止卡片套卡片)
// ---------------------------------------------------------------------------

export function Card({
    children,
    className,
    as: Tag = 'div',
}: {
    children: ReactNode;
    className?: string;
    as?: 'div' | 'article' | 'li';
}) {
    return (
        <Tag
            className={cn(
                'rounded-lg border border-slate-200 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]',
                className,
            )}
        >
            {children}
        </Tag>
    );
}
