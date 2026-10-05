import { Check } from 'lucide-react';
import { cn } from '@/lib/utils';

const STEPS = ['选题', '制作图文', '检查与修改'];
const SHORT_STEPS = ['选题', '制作', '检查'];
export function ImageNoteFlowSteps({ current, onBack, visitable }: { current: number; onBack?: (step: number) => void; visitable?: (1 | 2 | 3)[] }) {
    return <nav aria-label="图文制作步骤" className="grid grid-cols-3 gap-2 rounded-xl border border-border bg-card p-3" data-testid="image-note-flow-steps">
        {STEPS.map((label, i) => <button key={label} type="button"
            aria-current={current === i + 1 ? 'step' : undefined}
            disabled={!onBack || (visitable ? !visitable.some(step => step === i + 1) : i + 1 >= current)}
            onClick={() => onBack?.(i + 1)}
            className={cn('flex min-h-11 min-w-0 items-center gap-2 rounded-lg px-1 py-2 text-left text-xs sm:px-3 sm:text-sm',
                current === i + 1 ? 'bg-primary/10 font-semibold text-foreground' : 'text-muted-foreground',
                onBack && i + 1 < current && 'hover:bg-muted')}>
            <span className={cn('flex size-6 shrink-0 items-center justify-center rounded-full text-xs',
                current === i + 1 ? 'bg-primary text-primary-foreground' : 'bg-muted')}>
                {current > i + 1 ? <Check className="size-3.5" /> : i + 1}
            </span><span className="whitespace-nowrap sm:hidden">{SHORT_STEPS[i]}</span><span className="hidden sm:inline">{label}</span>
        </button>)}
    </nav>;
}
