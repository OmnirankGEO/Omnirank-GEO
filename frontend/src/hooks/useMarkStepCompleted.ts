import { useCallback } from 'react';
import { toast } from 'sonner';
import { useOnboarding } from '@/context/OnboardingContext';
import { ONBOARDING_STEPS } from '@/components/onboarding/OnboardingStepDefinitions';
import { isSandboxActive } from '@/sandbox/sandboxState';

/**
 * 在各页面 API success 后调用,自动勾选对应任务步骤(幂等)。
 *
 * Stage 1 Batch 2.5+: 完成一步后弹简短 toast 庆祝, 不再依赖 toast 引导下一步;
 * 用户在右下角任务清单卡里每个未完成步骤旁直接点"去做"按钮跳转。
 *
 * 用法:
 *   const markStep = useMarkStepCompleted();
 *   if (res.data.success) {
 *     markStep('enroll_client');
 *   }
 */
export function useMarkStepCompleted() {
    const { markStepCompleted, isStepCompleted } = useOnboarding();

    return useCallback(
        (stepId: string) => {
            if (isStepCompleted(stepId)) {
                // 沙盒重走教程: 步骤已在 completed_steps 里, markStepCompleted 无变化 → 彩带 diff 监听不触发。
                // 这里补派庆祝事件让重走也有彩带 (真实模式保持原幂等静默, 不受影响)
                if (isSandboxActive() && typeof window !== 'undefined') {
                    window.dispatchEvent(new CustomEvent('sandbox:step-celebrate', { detail: { stepId } }));
                }
                return; // 幂等
            }
            markStepCompleted(stepId);

            // 短 toast 庆祝当前步完成 · 下一步引导由 Checklist "去做" 按钮承担
            const step = ONBOARDING_STEPS.find((s) => s.step_id === stepId);
            if (step) {
                toast.success(`已完成: ${step.label}`, { duration: 3000 });
            }
        },
        [markStepCompleted, isStepCompleted],
    );
}
