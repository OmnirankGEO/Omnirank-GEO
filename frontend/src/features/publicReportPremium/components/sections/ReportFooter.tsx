/**
 * ReportFooter — 页尾
 * 报告生成时间 / 数据版本 / 隐私提示 / 打印入口。
 * 不出现数据库 ID、完整 token、内部路径。
 */
import { Printer } from 'lucide-react';
import type { PublicWhitelabel } from '../../contract/types';

function formatDateTime(iso: string | null): string | null {
    if (!iso) return null;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return null;
    return d.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

export function ReportFooter({
    generatedAt,
    versionLabel,
}: {
    generatedAt: string | null;
    versionLabel: string;
    whitelabel: PublicWhitelabel | null;
}) {
    const generated = formatDateTime(generatedAt);

    return (
        <footer className="border-t border-slate-200 bg-slate-50 py-8 print:border-slate-300">
            <div className="mx-auto w-full max-w-[1320px] px-4 sm:px-6 lg:px-10">
                <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                    <div className="space-y-1.5 text-xs leading-5 text-slate-500">
                        <p>报告生成时间：{generated ?? '—'}</p>
                        <p>数据版本：{versionLabel}</p>
                        <p className="max-w-xl">
                            隐私提示：本页为只读报告，为便于顾问跟进服务，会记录打开与停留状态；
                            本页不包含也不需要任何登录信息。
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={() => window.print()}
                        className="inline-flex min-h-10 shrink-0 items-center gap-1.5 self-start rounded-md border border-slate-300 bg-white px-4 text-sm font-medium text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-sky-600 print:hidden"
                    >
                        <Printer className="h-4 w-4" aria-hidden="true" />
                        打印 / 另存 PDF
                    </button>
                </div>
            </div>
        </footer>
    );
}
