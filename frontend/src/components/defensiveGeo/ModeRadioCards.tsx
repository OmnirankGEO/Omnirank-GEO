/**
 * 目标模式三卡 —— 规格 §9.2 + §9.8/§9.9 键盘合同。
 *
 * 键盘行为逐条来自 §9.8(不是通用 a11y 常识,是被点名的验收项):
 * - `Arrow/Home/End` **只在 radio group 内**移动选择与焦点,不把焦点甩到题族;
 * - `Space` 在聚焦 radio 时完成模式选择;
 * - `Enter` **不得**在 radio 上偷跑提交 —— 所以本组件对 Enter 一律
 *   `preventDefault()`,提交只允许发生在明确的「继续」按钮上;
 * - 状态不只靠颜色:选中态同时给出 ✓ 图标 + `aria-checked` + 文字。
 *
 * 🔴 推荐徽标只在**服务端签发过规则**时出现(§9.2 逐字)。
 *    当前系统**没有** start-context 端点(§15.1 未建),所以
 *    `recommendation` 恒为 undefined,徽标恒不显示 —— 这是正确的现状,
 *    不是"忘了做"。前端**不得**把 defensive 永久写成推荐项。
 */

import { useRef } from 'react';
import { Check } from 'lucide-react';
import { cn } from '@/lib/utils';

export type CampaignMode = 'defensive' | 'offensive' | 'hybrid';

/** 服务端签发的推荐(§15.1)。没有 ruleId/ruleVersion 就不是已签规则。 */
export interface SignedRecommendation {
    recommendedMode: CampaignMode | null;
    reason: string;
    ruleId: string;
    ruleVersion: string;
}

interface ModeOption {
    mode: CampaignMode;
    title: string;
    explainer: string;
}

/**
 * §9.2 三卡文案 + §0.5.5 U-1 定名。
 * 🔴 hybrid 那句在规格正文里写的是「主动获客」,U-1 已废该别名并定名「抢推荐」,
 *    本处按 U-1(§0.5 对正文有覆盖力)。
 */
const OPTIONS: ModeOption[] = [
    {
        mode: 'defensive',
        title: '先守住品牌',
        explainer:
            '有人点名问你、查你是否靠谱或拿你和竞品比较时,AI 能否认出并说清你',
    },
    {
        mode: 'offensive',
        title: '主动抢推荐',
        explainer:
            '客户没有点名品牌,只问“哪家好”时,AI 是否把你列入候选或推荐',
    },
    {
        mode: 'hybrid',
        title: '两条线一起看',
        explainer: '同一体检分别检查品牌防守与抢推荐,结果分开统计',
    },
];

export function modeOptions(): ReadonlyArray<ModeOption> {
    return OPTIONS;
}

interface Props {
    value: CampaignMode;
    onChange: (mode: CampaignMode) => void;
    /** 未签发时**必须**为 undefined/null —— 前端不得自造推荐。 */
    recommendation?: SignedRecommendation | null;
    disabled?: boolean;
}

/** 已签规则的判据:四项齐全且 recommendedMode 非空(§9.2 逐字)。 */
export function hasSignedRecommendation(
    rec: SignedRecommendation | null | undefined,
): rec is SignedRecommendation {
    return Boolean(
        rec && rec.recommendedMode && rec.ruleId && rec.ruleVersion && rec.reason,
    );
}

export function ModeRadioCards({ value, onChange, recommendation, disabled }: Props) {
    const refs = useRef<Array<HTMLButtonElement | null>>([]);
    const signed = hasSignedRecommendation(recommendation);

    const move = (from: number, to: number) => {
        const next = (to + OPTIONS.length) % OPTIONS.length;
        onChange(OPTIONS[next].mode);
        refs.current[next]?.focus();
    };

    const onKeyDown = (event: React.KeyboardEvent, index: number) => {
        switch (event.key) {
            case 'ArrowRight':
            case 'ArrowDown':
                event.preventDefault();
                move(index, index + 1);
                break;
            case 'ArrowLeft':
            case 'ArrowUp':
                event.preventDefault();
                move(index, index - 1);
                break;
            case 'Home':
                event.preventDefault();
                move(index, 0);
                break;
            case 'End':
                event.preventDefault();
                move(index, OPTIONS.length - 1);
                break;
            case ' ':
            case 'Spacebar':
                event.preventDefault();
                onChange(OPTIONS[index].mode);
                break;
            case 'Enter':
                // 🔴 §9.8:Enter 不得在 radio 上偷跑提交。
                event.preventDefault();
                break;
            default:
                break;
        }
    };

    return (
        <div
            role="radiogroup"
            aria-label="本次体检的目标"
            className="grid gap-3 md:grid-cols-3"
        >
            {OPTIONS.map((option, index) => {
                const selected = value === option.mode;
                const isRecommended = signed
                    && recommendation!.recommendedMode === option.mode;
                return (
                    <button
                        key={option.mode}
                        ref={(el) => { refs.current[index] = el; }}
                        type="button"
                        role="radio"
                        aria-checked={selected}
                        // 只有选中项进 Tab 序(roving tabindex)——
                        // 三个都可 Tab 会让键盘用户在组内被困住。
                        tabIndex={selected ? 0 : -1}
                        disabled={disabled}
                        onClick={() => onChange(option.mode)}
                        onKeyDown={(e) => onKeyDown(e, index)}
                        className={cn(
                            'flex min-h-[132px] flex-col items-start gap-2 rounded-lg border p-4 text-left transition',
                            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                            selected
                                ? 'border-primary bg-primary/5 ring-1 ring-primary'
                                : 'border-border hover:border-primary/40',
                            disabled && 'cursor-not-allowed opacity-60',
                        )}
                    >
                        <span className="flex w-full items-center gap-2">
                            {/* 状态不只靠颜色:给图标 + 下面的文字 */}
                            <span
                                aria-hidden
                                className={cn(
                                    'flex h-5 w-5 shrink-0 items-center justify-center rounded-full border',
                                    selected
                                        ? 'border-primary bg-primary text-primary-foreground'
                                        : 'border-muted-foreground/40',
                                )}
                            >
                                {selected && <Check className="h-3.5 w-3.5" />}
                            </span>
                            <span className="text-[15px] font-semibold">{option.title}</span>
                            {isRecommended && (
                                <span className="ml-auto rounded bg-primary/10 px-2 py-0.5 text-xs text-primary">
                                    推荐
                                </span>
                            )}
                        </span>
                        <span className="text-[13px] leading-6 text-muted-foreground">
                            {option.explainer}
                        </span>
                        <span className="sr-only">{selected ? '已选择' : '未选择'}</span>
                    </button>
                );
            })}
        </div>
    );
}

/**
 * `?goal=` **只预选、不锁死**(§9.2 逐字)。
 *
 * 未携带 / 值不合法 / 历史 deep link ⇒ 确定性回到 `offensive`,
 * 保证不带新字段的旧请求行为**字节不变**(ACT-01)。
 */
export function modeFromSearchParams(search: string): CampaignMode {
    const goal = new URLSearchParams(search).get('goal');
    if (goal === 'defensive' || goal === 'offensive' || goal === 'hybrid') {
        return goal;
    }
    return 'offensive';
}
