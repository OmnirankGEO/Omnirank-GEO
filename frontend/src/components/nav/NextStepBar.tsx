/**
 * 主链「下一步」条(包三 §G)。三页共用**同一个**组件与**同一份**映射 ——
 * 每页各写一条,措辞迟早各走各的,而她会以为那是三个不同的流程。
 */
import { Link } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { mainChainNextStep } from './mainChainNextStep';

export function NextStepBar({ page }: { page: string }) {
    const step = mainChainNextStep(page);
    if (!step) return null;   // 未知页不渲染,不猜一个下一步
    return (
        <div
            data-testid="main-chain-next-step"
            data-page={page}
            className="mt-6 flex flex-col gap-2 rounded-xl border border-border bg-muted/30 p-3 text-[13px]
                       sm:flex-row sm:items-center sm:justify-between"
        >
            <div className="min-w-0">
                <p className="font-medium text-foreground">{step.doneLabel},接下来:{step.nextLabel}</p>
                <p className="mt-0.5 text-muted-foreground">{step.why}</p>
            </div>
            <Link
                to={step.to}
                data-testid="main-chain-next-step-link"
                className="inline-flex shrink-0 items-center justify-center gap-1 rounded-lg bg-foreground
                           px-3 py-2 text-[13px] font-medium text-background hover:bg-foreground/90"
            >
                {step.nextLabel}
                <ArrowRight className="h-3.5 w-3.5" />
            </Link>
        </div>
    );
}
