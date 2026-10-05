import { Loader2, Check } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useWaitMessage, type WaitScene } from '@/hooks/useWaitMessage';

interface ThinkingStep {
  text: string;
  done: boolean;
}

interface SimulatedThinkingProps {
  steps: ThinkingStep[];
  isVisible: boolean;
  className?: string;
  /** v1.1 优化: 最后一个 active 步骤下方追加 rotating 安抚文字, 避免长任务 steps 走完后静止 */
  waitScene?: WaitScene;
}

export function SimulatedThinking({ steps, isVisible, className, waitScene }: SimulatedThinkingProps) {
  // 找到当前活跃步骤 (未完成); 用于决定是否轮换副文案
  const hasActive = steps.some(s => !s.done);
  const waitMsg = useWaitMessage(!!waitScene && hasActive && isVisible, waitScene || 'thinking');

  if (!isVisible || steps.length === 0) return null;

  return (
    <div className={cn(
      'flex flex-col gap-1.5 py-2 px-3 rounded-lg bg-card/50 border border-border/30',
      className
    )}>
      {steps.map((step, i) => {
        const isLastActive = !step.done && i === steps.length - 1;
        return (
          <div key={i} className="flex flex-col gap-1">
            <div className="flex items-center gap-2 text-sm">
              {step.done ? (
                <Check className="w-3.5 h-3.5 text-green-400 shrink-0" />
              ) : (
                <Loader2 className="w-3.5 h-3.5 text-foreground animate-spin shrink-0" />
              )}
              <span className={cn(
                step.done ? 'text-muted-foreground' : 'text-foreground'
              )}>
                {step.text}
              </span>
            </div>
            {/* 最后一个活跃步骤下方: 等几秒后开始轮换口语化安抚文字 */}
            {isLastActive && waitMsg && (
              <p
                key={waitMsg}
                className="text-xs text-muted-foreground/70 pl-5 animate-in fade-in duration-700"
              >
                {waitMsg}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}
