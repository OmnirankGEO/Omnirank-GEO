/**
 * Narrative — 报告详述(客户版叙事 Markdown)
 *
 * 默认折叠为「查看完整分析」:首屏结论与漏斗已在结构化区块呈现,
 * 叙事区作为补充阅读(评审 P2-6)；打印模式下始终完整展开。
 * Markdown 只承载叙事；数值指标一律读结构化 DTO,禁止从 Markdown 解析数值。
 */
import { useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { Card, EmptyBlock, Section } from '../ui/primitives';
import { MarkdownView } from '../markdown/MarkdownView';
import { cn } from '@/lib/utils';

export function Narrative({ markdown }: { markdown: string | null }) {
    const [open, setOpen] = useState(false);

    return (
        <Section
            id="narrative"
            eyebrow="完整叙事"
            title="报告详述"
            subtitle="本区为叙事补充；所有数值指标以结构化区块为准。"
        >
            {!markdown ? (
                <EmptyBlock title="叙事内容暂未形成" message="本次诊断没有生成可展示的叙事内容。" />
            ) : (
                <>
                    <button
                        type="button"
                        onClick={() => setOpen((v) => !v)}
                        aria-expanded={open}
                        aria-controls="narrative-content"
                        className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-4 text-sm font-medium text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-sky-600 print:hidden"
                    >
                        {open ? '收起完整分析' : '查看完整分析'}
                        <ChevronDown
                            className={cn('h-4 w-4 transition-transform motion-reduce:transition-none', open && 'rotate-180')}
                            aria-hidden="true"
                        />
                    </button>
                    <div
                        id="narrative-content"
                        className={cn(open ? 'mt-4' : 'mt-4 hidden', 'print:mt-4 print:block')}
                    >
                        <Card className="p-4 sm:p-6">
                            <MarkdownView markdown={markdown} />
                        </Card>
                    </div>
                </>
            )}
        </Section>
    );
}
