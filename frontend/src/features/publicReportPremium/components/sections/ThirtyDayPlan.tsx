/**
 * ThirtyDayPlan — 30 天建议路径(时间轴)
 * 内容由后端数据驱动；无个性化数据时展示通用方法并显式标注，不冒充诊断结论。
 */
import { Card, Section } from '../ui/primitives';
import type { DataSection, ThirtyDayWeek } from '../../contract/types';
import { cn } from '@/lib/utils';

/** 通用方法(仅在后端无个性化路径时展示，且必须明示) */
const GENERIC_WEEKS: readonly ThirtyDayWeek[] = [
    { weekIndex: 1, theme: '补齐资料与信源', items: ['完善官方介绍与资质信息', '整理可核验的案例材料', '补齐主流平台品牌词条'], personalized: false },
    { weekIndex: 2, theme: '建设可信内容', items: ['发布案例与选购指南内容', '争取权威媒体报道'], personalized: false },
    { weekIndex: 3, theme: '发布与收录验证', items: ['确认内容被收录', '布局问答社区真实回答'], personalized: false },
    { weekIndex: 4, theme: '重新监测与复盘', items: ['对同一批问题复测', '对比指标变化并确定下轮重点'], personalized: false },
];

export function ThirtyDayPlan({ section }: { section: DataSection<readonly ThirtyDayWeek[]> }) {
    const weeks = section.status === 'ready' && section.data.length > 0 ? section.data : GENERIC_WEEKS;
    const isGeneric = !(section.status === 'ready' && section.data.length > 0) || weeks.every((w) => !w.personalized);

    return (
        <Section
            id="thirty-day"
            eyebrow="节奏建议"
            title="30 天建议路径"
            subtitle={
                isGeneric
                    ? '以下为通用方法路径，非个性化诊断结论；完成完整诊断后将替换为针对性安排。'
                    : '按周推进，每周聚焦一个主题。'
            }
        >
            {section.status !== 'ready' && (
                <p className="mb-4 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-500">
                    {section.message} 当前改以通用方法展示，不代表针对本品牌的诊断结论。
                </p>
            )}
            <ol className="relative grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                    {weeks.map((week) => (
                        <Card as="li" key={week.weekIndex} className={cn('relative p-4', isGeneric && 'border-dashed')}>
                            <div className="flex items-center gap-2">
                                <span
                                    className={cn(
                                        'flex h-7 w-7 items-center justify-center rounded-full text-xs font-bold',
                                        isGeneric ? 'bg-slate-100 text-slate-500' : 'bg-sky-700 text-white',
                                    )}
                                >
                                    {week.weekIndex}
                                </span>
                                <h3 className="text-sm font-semibold text-slate-800">第 {week.weekIndex} 周</h3>
                            </div>
                            <p className="mt-2 text-sm font-medium text-slate-700">{week.theme}</p>
                            <ul className="mt-2 space-y-1.5">
                                {week.items.map((item, i) => (
                                    <li key={i} className="flex gap-1.5 text-xs leading-5 text-slate-600">
                                        <span aria-hidden="true" className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-slate-300" />
                                        <span className="break-words">{item}</span>
                                    </li>
                                ))}
                            </ul>
                        </Card>
                    ))}
            </ol>
        </Section>
    );
}
