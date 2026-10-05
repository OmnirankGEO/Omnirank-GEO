/**
 * Methodology — 方法说明
 * 本次测了什么 / 时间范围 / 平台范围 / 分数口径 / Evidence A/B/C / 数据限制 /
 * 为什么缺失值不是 0 / 不代表永久排名。
 */
import { Card, Section } from '../ui/primitives';
import type { MethodologyNotes } from '../../contract/types';

export function Methodology({ notes }: { notes: MethodologyNotes }) {
    const rows: readonly { label: string; value: string | null }[] = [
        { label: '本次测了什么', value: notes.testedScope },
        { label: '数据时间范围', value: notes.timeRange },
        { label: '平台范围', value: notes.platformScope },
        { label: '分数计算口径', value: notes.scoreMethod },
    ];

    return (
        <Section
            id="methodology"
            eyebrow="可信度"
            title="方法说明"
            subtitle="看懂这份报告，只需要这一页。"
        >
            <Card className="p-4 sm:p-6">
                <dl className="grid gap-4 md:grid-cols-2">
                    {rows.map((row) => (
                        <div key={row.label}>
                            <dt className="text-xs font-semibold text-slate-500">{row.label}</dt>
                            <dd className="mt-1 break-words text-sm leading-6 text-slate-700">
                                {row.value ?? <span className="text-slate-400">暂无说明</span>}
                            </dd>
                        </div>
                    ))}
                </dl>

                <div className="mt-5 border-t border-slate-100 pt-4">
                    <h3 className="text-xs font-semibold text-slate-500">证据等级怎么看</h3>
                    <ul className="mt-2 grid gap-1.5 text-sm text-slate-700 sm:grid-cols-3">
                        <li className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2">
                            <span className="font-semibold text-emerald-700">A · 直接证据</span>
                            <span className="mt-0.5 block text-xs leading-5 text-emerald-800/80">回答明确点名品牌，可支撑具体结论。</span>
                        </li>
                        <li className="rounded-md border border-sky-200 bg-sky-50 px-3 py-2">
                            <span className="font-semibold text-sky-700">B · 参考证据</span>
                            <span className="mt-0.5 block text-xs leading-5 text-sky-800/80">品类或场景旁证，只作倾向性参考。</span>
                        </li>
                        <li className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
                            <span className="font-semibold text-slate-600">C · 待验证线索</span>
                            <span className="mt-0.5 block text-xs leading-5 text-slate-500">仅表示可能存在的线索或推测，必须进一步验证后才能下结论。</span>
                        </li>
                    </ul>
                </div>

                {notes.dataLimits.length > 0 && (
                    <div className="mt-5 border-t border-slate-100 pt-4">
                        <h3 className="text-xs font-semibold text-slate-500">数据限制</h3>
                        <ul className="mt-2 space-y-1.5">
                            {notes.dataLimits.map((limit, i) => (
                                <li key={i} className="flex gap-2 text-sm leading-6 text-slate-600">
                                    <span aria-hidden="true" className="mt-2.5 h-1 w-1 shrink-0 rounded-full bg-slate-300" />
                                    <span className="break-words">{limit}</span>
                                </li>
                            ))}
                        </ul>
                    </div>
                )}

                <div className="mt-5 rounded-md bg-slate-50 p-3 text-xs leading-5 text-slate-500">
                    <p>
                        为什么缺失值不是 0:「0」是一个测量结果，意味着"测了但没有"；而「—」表示本次没有形成有效测量。
                        把缺失当成 0 会系统性低估你的真实状态，因此本报告严格区分两者。
                    </p>
                    <p className="mt-1.5">
                        本报告为诊断时刻的快照，不代表任何平台的永久排名，也不构成效果承诺。
                    </p>
                    {notes.extraNotes.map((note, i) => (
                        <p key={i} className="mt-1.5">{note}</p>
                    ))}
                </div>
            </Card>
        </Section>
    );
}
