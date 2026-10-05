/**
 * RaceLab — 请求竞态实验台(仅 harness/Playwright 使用)
 *
 * 在同一挂载页内切换 reportId,验证 ReportPage 的代际守卫:
 * 慢 A 后返回不得覆盖先到的 B;连续快速切换以最后一次为准。
 */
import { useMemo, useState } from 'react';
import { PublicReportPage } from '../components/ReportPage';
import { createHttpTransport } from '../transport/httpTransport';

export function RaceLab() {
    const [reportId, setReportId] = useState('100');
    const transport = useMemo(() => createHttpTransport(), []);

    return (
        <>
            <PublicReportPage transport={transport} reportId={reportId} shareToken={null} />
            <div
                data-testid="race-lab-bar"
                className="fixed bottom-3 right-3 z-50 flex items-center gap-2 rounded-lg border border-slate-300 bg-white p-2 shadow-lg print:hidden"
            >
                <button
                    type="button"
                    onClick={() => setReportId('100')}
                    className="rounded-md border border-slate-300 px-3 py-2 text-xs text-slate-700 hover:bg-slate-100"
                >
                    切到报告 A
                </button>
                <button
                    type="button"
                    onClick={() => setReportId('200')}
                    className="rounded-md bg-slate-900 px-3 py-2 text-xs font-medium text-white hover:bg-slate-700"
                >
                    切到报告 B
                </button>
                <button
                    type="button"
                    onClick={() => setReportId('invalid')}
                    className="rounded-md border border-rose-300 px-3 py-2 text-xs text-rose-700 hover:bg-rose-50"
                >
                    切到失效链接
                </button>
                <span className="text-xs text-slate-500">
                    当前:{reportId === '100' ? 'A' : reportId === '200' ? 'B' : '失效链接'}
                </span>
            </div>
        </>
    );
}
