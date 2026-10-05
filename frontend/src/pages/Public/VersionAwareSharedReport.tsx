import { useEffect, useMemo, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import { Loader2 } from 'lucide-react';

import LegacySharedReport from './SharedReport';
import { PublicReportPage, createHttpTransport } from '@/features/publicReportPremium';

type ReportMode = 'checking' | 'premium' | 'legacy';

/** Route adapter that keeps historical V1 links while making V2 use the new report. */
export default function VersionAwareSharedReport() {
    const { id = '' } = useParams<{ id: string }>();
    const [searchParams] = useSearchParams();
    const shareToken = searchParams.get('st');
    const [mode, setMode] = useState<ReportMode>('checking');
    const transport = useMemo(() => createHttpTransport(), []);

    useEffect(() => {
        if (!/^\d+$/.test(id)) {
            setMode('premium');
            return;
        }
        const controller = new AbortController();
        setMode('checking');
        const params = new URLSearchParams();
        if (shareToken) params.set('st', shareToken);
        const query = params.toString();

        void fetch(`/api/public/report/${encodeURIComponent(id)}${query ? `?${query}` : ''}`, {
            headers: { Accept: 'application/json' },
            signal: controller.signal,
        })
            .then(async (response) => {
                if (!response.ok) return null;
                try {
                    return await response.json() as unknown;
                } catch {
                    return null;
                }
            })
            .then((body) => {
                if (controller.signal.aborted) return;
                const report = body && typeof body === 'object' && 'report' in body
                    ? (body as { report?: unknown }).report
                    : null;
                const version = report && typeof report === 'object' && 'report_version' in report
                    ? (report as { report_version?: unknown }).report_version
                    : null;
                setMode(version === 'v1' ? 'legacy' : 'premium');
            })
            .catch(() => {
                if (!controller.signal.aborted) setMode('premium');
            });

        return () => controller.abort();
    }, [id, shareToken]);

    if (mode === 'legacy') return <LegacySharedReport />;
    if (mode === 'premium') {
        return <PublicReportPage transport={transport} reportId={id} shareToken={shareToken} />;
    }
    return (
        <div className="flex min-h-screen items-center justify-center bg-slate-50" role="status">
            <Loader2 className="h-7 w-7 animate-spin text-slate-500" aria-hidden="true" />
            <span className="sr-only">正在打开报告</span>
        </div>
    );
}
