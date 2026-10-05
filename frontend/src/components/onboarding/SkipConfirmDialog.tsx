/**
 * 跳过引导步骤确认弹窗
 * Stage 1 Batch 1 (2026-05-06)
 *
 * 两种文案变体, 由 dangerous prop 决定:
 *   - dangerous=false (一般跳过): 提示"会标记已跳过, 可稍后回来补做"
 *   - dangerous=true  (危险跳过): 警告"这步是后续基础, 跳过后教程演示效果会受影响"
 *
 * 真正控制是否能跳过的是 OnboardingStepDefinitions 里的 canSkip 字段,
 * 此组件只负责 "已经允许跳过 → 弹窗二次确认" 的最后一道防线
 */
import {
    AlertDialog,
    AlertDialogAction,
    AlertDialogCancel,
    AlertDialogContent,
    AlertDialogDescription,
    AlertDialogFooter,
    AlertDialogHeader,
    AlertDialogTitle,
} from '@/components/ui/alert-dialog';

export interface SkipConfirmDialogProps {
    /** 弹窗开关 */
    open: boolean;
    /** 受控变更回调 (用户点蒙层 / Esc / 取消都会触发) */
    onOpenChange: (open: boolean) => void;
    /** 用户确认跳过后回调 (上层在此调用 skipStep) */
    onConfirm: () => void;
    /** 是否危险跳过 (会显示更严肃的警告文案) */
    dangerous: boolean;
    /** 步骤名称, 嵌入文案显示 */
    stepLabel: string;
}

export function SkipConfirmDialog({
    open,
    onOpenChange,
    onConfirm,
    dangerous,
    stepLabel,
}: SkipConfirmDialogProps) {
    return (
        <AlertDialog open={open} onOpenChange={onOpenChange}>
            <AlertDialogContent>
                <AlertDialogHeader>
                    <AlertDialogTitle>
                        {dangerous ? '跳过这一步会影响后续教程' : '确定要跳过这一步吗?'}
                    </AlertDialogTitle>
                    <AlertDialogDescription>
                        {dangerous
                            ? `"${stepLabel}" 是后续步骤的重要基础, 跳过后续教程的演示效果可能会受影响, 真的要跳过吗?`
                            : `跳过后这一步会标记为"已跳过"而不是"已完成", 可以稍后回来补做。`}
                    </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                    <AlertDialogCancel>取消</AlertDialogCancel>
                    <AlertDialogAction onClick={onConfirm}>
                        {dangerous ? '我知道, 仍要跳过' : '确认跳过'}
                    </AlertDialogAction>
                </AlertDialogFooter>
            </AlertDialogContent>
        </AlertDialog>
    );
}

export default SkipConfirmDialog;
