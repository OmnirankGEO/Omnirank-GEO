/**
 * 验收用临时 harness（不提交）· 真渲染真组件：
 *   全局 <Toaster />（production 同配置 richColors + top-center）
 *   + 真 useConfirmLargeDeduction（真阈值 / 真 5s / 真取消语义）
 * onConfirm 只写一行状态文本，不打后端、不扣任何算力。
 */
import React from 'react';
import ReactDOM from 'react-dom/client';
import { ThemeProvider } from '@/context/ThemeContext';
import { Toaster } from '@/components/ui/sonner';
import { useConfirmLargeDeduction } from '@/hooks/useConfirmLargeDeduction';
import { Button } from '@/components/ui/button';
import '@/index.css';

function Harness() {
    const confirmLarge = useConfirmLargeDeduction(2000);
    const [log, setLog] = React.useState<string[]>([]);
    const push = (m: string) => setLog((p) => [...p, m]);

    return (
        <div className="min-h-dvh bg-background p-6 space-y-4">
            <h1 className="text-lg font-semibold">useConfirmLargeDeduction harness</h1>
            <div className="flex flex-wrap gap-3">
                <Button
                    data-testid="trigger-large"
                    onClick={() =>
                        confirmLarge(2730, () => push('EXECUTED'), '将扣 2,730 算力写 7 篇文章')
                    }
                >
                    大额触发 (2,730)
                </Button>
                <Button
                    variant="secondary"
                    data-testid="trigger-small"
                    onClick={() => confirmLarge(300, () => push('EXECUTED-SMALL'))}
                >
                    小额直通 (300)
                </Button>
                <Button variant="ghost" data-testid="clear-log" onClick={() => setLog([])}>
                    清空
                </Button>
            </div>
            <pre data-testid="exec-log" className="rounded-lg border p-3 text-xs min-h-16">
                {log.join('\n')}
            </pre>
            <Toaster richColors position="top-center" />
        </div>
    );
}

ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
        <ThemeProvider>
            <Harness />
        </ThemeProvider>
    </React.StrictMode>,
);
