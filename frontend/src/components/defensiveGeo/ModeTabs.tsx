/**
 * ModeTabs — 三标签模式选择器(包一 · 订正三)。
 *
 * 取代 `ModeRadioCards` 的三张卡:Owner 定「页内三个标签,点标签即切换,题单区随之换题」。
 *
 * 🔴 **说明文案不在这里重打一遍**,从 `modeOptions()` 取。
 *    订正三要求「模式说明文案一字不删」——若这里抄一份,它与卡片那份就是同一段话的两个源,
 *    服务端/文案登记改了一处、另一处不跟,而**两处各自"看起来对"**。
 *    标签名(防守 / 增长 / 全面测试)是本组件新增的短名,说明仍是原文。
 *
 * 🔴 默认标签 = **增长**(offensive)—— 与今天生产缺省一致(`modeFromSearchParams` 无参回 offensive),
 *    旧链接旧书签行为不变。默认值不在本组件里定,由调用方的 state 决定,本组件只受控。
 */
import { cn } from '@/lib/utils';
import { modeOptions, hasSignedRecommendation, type CampaignMode, type SignedRecommendation } from './ModeRadioCards';

/** 标签短名。说明文案仍取自 modeOptions(),此处只给标签自己的名字。 */
const TAB_LABEL: Record<CampaignMode, string> = {
    defensive: '防守',
    offensive: '增长',
    hybrid: '全面测试',
};

interface Props {
    value: CampaignMode;
    onChange: (mode: CampaignMode) => void;
    /** 未签发时**必须**为 undefined/null —— 前端不得自造推荐(沿用三卡的同一条铁律)。 */
    recommendation?: SignedRecommendation | null;
    disabled?: boolean;
}

export function ModeTabs({ value, onChange, recommendation, disabled = false }: Props) {
    const options = modeOptions();
    const signed = hasSignedRecommendation(recommendation);

    const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
        if (disabled) return;
        const i = options.findIndex((o) => o.mode === value);
        if (i < 0) return;
        // 键盘可达:tablist 的标准是左右箭头切换,不是 Tab 逐个走。
        if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
            e.preventDefault();
            const next = e.key === 'ArrowRight'
                ? options[(i + 1) % options.length]
                : options[(i - 1 + options.length) % options.length];
            onChange(next.mode);
        }
    };

    return (
        <div className="space-y-2">
            <div
                role="tablist"
                aria-label="本次体检的目标"
                data-testid="launch-mode-tabs"
                onKeyDown={onKeyDown}
                className="flex w-full gap-1 rounded-lg bg-muted/50 p-1"
            >
                {options.map((o) => {
                    const selected = o.mode === value;
                    return (
                        <button
                            key={o.mode}
                            type="button"
                            role="tab"
                            id={`launch-mode-tab-${o.mode}`}
                            data-testid="launch-mode-tab"
                            data-mode={o.mode}
                            aria-selected={selected}
                            /* 每个标签控制**自己那一句**;选中那句沿用老 id
                               `launch-mode-explainer`(别处按它定位),其余各有各的 id。 */
                            aria-controls={selected ? 'launch-mode-explainer' : `launch-mode-desc-${o.mode}`}
                            disabled={disabled}
                            onClick={() => onChange(o.mode)}
                            className={cn(
                                'min-h-[40px] flex-1 rounded-md px-3 text-[14px] font-medium transition',
                                'disabled:cursor-not-allowed disabled:opacity-60',
                                selected
                                    ? 'bg-background text-foreground shadow-sm'
                                    : 'text-muted-foreground hover:text-foreground',
                            )}
                        >
                            {TAB_LABEL[o.mode]}
                            {signed && recommendation?.recommendedMode === o.mode ? (
                                <span data-testid="launch-mode-recommended"
                                    className="ml-1.5 rounded bg-primary/15 px-1.5 py-0.5 text-[11px] text-primary">
                                    建议
                                </span>
                            ) : null}
                        </button>
                    );
                })}
            </div>

            {/*
              * 🔴 [#200 §1.2] 三档说明**同时上屏**,不再只渲染选中那一档。
              *
              *   现场:三个标签下面只有一句话 —— 另外两档想知道是干什么的,
              *   必须先点一下。而"点一下才知道这是什么"正是第 11 条要清的那种猜。
              *
              *   文案仍然全部取自 `modeOptions()`,本文件**一个字都不重打**
              *   (`verify-pkg1-mode-tabs` B1 连注释一起查,抄进来就红)。
              *
              * 🔴 每一句前面带**它自己的标签名**:窄档三句会纵排成一列,
              *   那时"哪句对哪档"没法靠位置看出来。宽档略显重复,但重复的代价
              *   是两个字,含糊的代价是猜错一档 —— 两者不对等。
              *
              * 🔴 断点问它自己那一栏有多宽(本组件开 `@container`),不问页面:
              *   这块地方在 880px 那一档反而**变窄**(右边让出 340px 侧栏),
              *   照页面的断点切会正好切在最窄的时候。
              */}
            <div className="@container">
                <div className="grid gap-2 @min-[560px]:grid-cols-3"
                    data-testid="launch-mode-explainer-grid">
                    {options.map((o) => {
                        const selected = o.mode === value;
                        return (
                            <p
                                key={o.mode}
                                id={selected ? 'launch-mode-explainer' : `launch-mode-desc-${o.mode}`}
                                role="tabpanel"
                                aria-labelledby={`launch-mode-tab-${o.mode}`}
                                data-testid="launch-mode-explainer"
                                data-mode={o.mode}
                                data-selected={selected ? 'true' : 'false'}
                                className={cn(
                                    'text-[13px] leading-relaxed',
                                    selected ? 'text-foreground' : 'text-muted-foreground',
                                )}
                            >
                                <span className="font-medium" data-testid="launch-mode-explainer-name">
                                    {TAB_LABEL[o.mode]}
                                </span>
                                <span aria-hidden="true"> · </span>
                                <span data-testid="launch-mode-explainer-text">{o.explainer}</span>
                            </p>
                        );
                    })}
                </div>
            </div>
        </div>
    );
}
