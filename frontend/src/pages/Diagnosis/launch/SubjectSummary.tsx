/**
 * 诊断对象四项的**摘要态**(订正六:压成一行「深圳 · 装修 · 区域生意 · 改」)。
 *
 * 🔴 四项一个不丢 —— 它们直接决定题单生成(区域生意出「城市 + 行业」题,
 *    全国出行业大词;填错命中率失真)。这里只改**呈现**:从四个常驻输入框
 *    压成一行文本 + 一个「改」;点「改」展开原表单,输入位仍是原来那四个。
 *
 * 🔴 品牌名不在这一行里 —— 它是页面第一格、常驻可编辑(订正十:
 *    品牌体检是全站第一个输入公司名的地方)。所以「四项在 DOM 中始终存在」
 *    这条判据里,品牌名走它自己的 input,另三项走本组件或展开后的表单。
 *
 * 🔴 摘要显示的值与提交的值必须同源:本组件**只渲染传进来的 formData 值**,
 *    不自己缓存、不自己格式化数字。订正六补(A)那条判据钉的就是
 *    「提交体四项逐字等于摘要行当刻渲染出的四个值」——
 *    若这里存一份快照,就会出现「她看到的 ≠ 她提交的」(与 #62 同形)。
 */
import { Pencil } from 'lucide-react';
import { scopeShortLabel } from './subjectFields';

interface Props {
    industry: string;
    clientLocation: string;
    businessScope: string;
    onEdit: () => void;
    disabled?: boolean;
}

export function SubjectSummary({ industry, clientLocation, businessScope, onEdit, disabled }: Props) {
    const scope = scopeShortLabel(businessScope);
    // 城市在全国生意下可以为空 —— 空就不占位,不显示一个孤零零的分隔点。
    const parts = [clientLocation?.trim(), industry?.trim(), scope].filter(Boolean);
    return (
        <div
            data-testid="subject-summary"
            className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border border-border bg-muted/40 px-3 py-2 text-[14px]"
        >
            {clientLocation?.trim() ? (
                <span data-testid="subject-summary-city" className="text-foreground">{clientLocation.trim()}</span>
            ) : null}
            {clientLocation?.trim() ? <span className="text-muted-foreground">·</span> : null}
            <span data-testid="subject-summary-industry" className="text-foreground">{industry?.trim()}</span>
            <span className="text-muted-foreground">·</span>
            <span data-testid="subject-summary-scope" className="text-foreground">{scope}</span>
            <button
                type="button"
                data-testid="subject-edit"
                onClick={onEdit}
                disabled={disabled}
                className="ml-auto inline-flex items-center gap-1 text-[13px] text-muted-foreground underline underline-offset-2 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
            >
                <Pencil className="h-3.5 w-3.5" aria-hidden />
                改
            </button>
            {/* 屏幕阅读器要能一口气读完这一行是什么,而不是四个孤立的词 */}
            <span className="sr-only">{`诊断对象:${parts.join(' · ')}。点「改」可以修改。`}</span>
        </div>
    );
}
