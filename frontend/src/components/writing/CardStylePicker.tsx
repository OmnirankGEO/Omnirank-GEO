/**
 * 卡面风格选择器（四款预设 + 真实产出样例图）
 *
 * 🔴 为什么抽成共用组件：这一段原来只长在详情页的「再次创作」弹窗里。
 *    Owner 2026-08-04 指出「风格选择页我们之前不是预设了一些模板吗？现在没有这个部分」——
 *    对。四款预设（services/geo_douyin/card_templates.STYLE_PRESETS）、
 *    `/api/geo-douyin/styles` 端点、四张样例图 2026-08-02 就全部就绪，
 *    但**创作页一次都没有露出**：第一次做只能吃默认款，想换风格得先花 390 做一版、
 *    再花 100 重做。断的不是能力，是入口。
 *
 * 🔴 抽组件而不是复制粘贴：两页要显示的是**同一份**四款样例。复制一份出去，
 *    下次加第五款风格时必然只改一处 —— 这类"两份各自演化"是本仓反复栽过的坑。
 *
 * 🔴 样例图是我们自己生成的（同一个「深圳全屋定制哪家好」跑四款风格，
 *    prompt 就是 card_templates.build_cover_prompt 的真实产出），
 *    并排看差异只来自风格本身。不使用任何外部参考仓库的图片。
 */
import designTextSample from '@/assets/style-samples/design_text.jpg';
import tableReviewSample from '@/assets/style-samples/table_review.jpg';
import memoSample from '@/assets/style-samples/memo.jpg';
import photoOverlaySample from '@/assets/style-samples/photo_overlay.jpg';

export interface StyleOption {
    key: string;
    label: string;
    disabled: boolean;
    disabled_reason: string;
    density: string;
    needs_photo: boolean;
}

/**
 * 🔴 用 Record 而不是写死四个 <img>：某一款暂时没有样板图时优雅退化成纯文字选项，
 *    而不是裂图。
 */
export const STYLE_SAMPLES: Record<string, string> = {
    design_text: designTextSample,
    memo: memoSample,
    photo_overlay: photoOverlaySample,
    table_review: tableReviewSample,
};

/** 四款风格的一句话说明。用户看的是"我该选哪个"，不是密度档位这种工程词。 */
const STYLE_HINT: Record<string, string> = {
    design_text: '最通用，被引用的样本里六成是这一类',
    table_review: '多家横向对比、参数摆一起时用',
    memo: '经验贴、避坑清单的口吻',
    photo_overlay: '有客户实拍图才能用，现场感最强',
};

interface Props {
    styles: StyleOption[];
    value: string;
    onChange: (key: string) => void;
    /** 每行几款。弹窗里宽，横排四款；侧栏窄，两排两款。 */
    columns?: 2 | 4;
    /**
     * [#188] 画廊尺寸。`gallery` = 制作台那种「看清成品长什么样」的大图
     * (Owner 09-13:「风格这里必须让人看到成品是什么样」);
     * `control` = 原来的控件体量(弹窗/侧栏用),默认值,老调用点零影响。
     */
    size?: 'control' | 'gallery';
    /**
     * [#188] 不可用款的**出口**。禁猜清单第 4 条:不可选必须同屏给原因**与出口**。
     * 原因回答"为什么不能选",出口回答"那我该怎么办" —— 少了后者,
     * 用户只能盯着一个灰卡片(实拍叠字缺授权图就是这一格)。
     */
    onFixDisabled?: (style: StyleOption) => void;
    fixDisabledLabel?: string;
    /** 附在下方的说明。不传就用默认那句。 */
    footnote?: string;
    /** data-testid 前缀，让两个使用点各自可被定位。 */
    testIdPrefix?: string;
    /**
     * 外层类名。主要用来**限宽**。
     * 🔴 本机实测抓到的问题：样例图是 3:4 的，四款横排铺满创作页那条 1080px 的主列时，
     *    每张接近 250×290 —— 比页面上任何别的东西都大，整块读起来像图库不像选择器。
     *    所以调用方要给一个 max-w，让它回到"控件"的体量。
     */
    className?: string;
}

export function CardStylePicker({
    styles, value, onChange, columns = 4, footnote, testIdPrefix = 'style',
    size = 'control', onFixDisabled, fixDisabledLabel = '去补这项 →',
    className = '',
}: Props) {
    if (styles.length === 0) return null;
    const gallery = size === 'gallery';
    return (
        <div className={`space-y-2 ${className}`}>
            {/* 🔴 [#188] 四列在 390px 上会把「表格测评卡」断成「表格测评 / 卡」、
                把不可用款那句原因压成六行 —— 本机 390 实测。窄屏一律两列。
                (columns=2 的老调用点不受影响:它本来就是两列。) */}
            <div className={`grid gap-2 ${columns === 2 ? 'grid-cols-2' : 'grid-cols-2 sm:grid-cols-4'}`}>
                {styles.map(s => (
                    <button
                        key={s.key}
                        type="button"
                        disabled={s.disabled}
                        onClick={() => onChange(s.key)}
                        title={s.disabled ? s.disabled_reason : STYLE_HINT[s.key] || ''}
                        aria-pressed={value === s.key}
                        /* 🔴 [Review 09-13 复核②] 不给可访问名的话,读屏会把
                           标签 + 原因 + 出口文字连成一长句念出来。显式给一个短名。 */
                        aria-label={s.disabled ? `${s.label}(暂不可用)` : s.label}
                        data-testid={`${testIdPrefix}-${s.key}`}
                        className={`overflow-hidden rounded-lg border text-left transition ${s.disabled
                            ? 'cursor-not-allowed opacity-45'
                            : value === s.key
                                ? 'cursor-pointer border-primary ring-1 ring-primary'
                                : 'cursor-pointer hover:border-muted-foreground/40'
                            }`}
                    >
                        {/* 样板图缺某一款时退化成纯文字，不裂图 */}
                        {STYLE_SAMPLES[s.key] && (
                            <img
                                src={STYLE_SAMPLES[s.key]}
                                alt={`${s.label}样例`}
                                className={`block w-full object-cover ${gallery ? "aspect-[3/4]" : ""}`}
                                data-testid={`${testIdPrefix}-sample-${s.key}`}
                            />
                        )}
                        <span className={`block px-1.5 pt-1 text-center ${gallery ? 'text-sm font-medium' : 'text-xs'}`}>
                            {s.label}
                        </span>
                        <span className={`block px-1.5 pb-1 text-center leading-tight text-muted-foreground ${gallery ? 'text-xs' : 'text-[10px]'}`}>
                            {s.disabled ? s.disabled_reason : STYLE_HINT[s.key] || ''}
                        </span>
                        {/* 🔴 不可用款给**出口**。注意不能嵌套 <button>(HTML 非法),
                            所以用 span + role=link,并 stopPropagation 免得点它变成"选中这一款"。 */}
                        {s.disabled && onFixDisabled && (
                            <span
                                role="link"
                                tabIndex={0}
                                data-testid={`${testIdPrefix}-fix-${s.key}`}
                                className="block cursor-pointer px-1.5 pb-1.5 text-center text-xs text-primary underline"
                                onClick={(e) => { e.stopPropagation(); onFixDisabled(s); }}
                                onKeyDown={(e) => {
                                    if (e.key === 'Enter' || e.key === ' ') {
                                        e.preventDefault(); e.stopPropagation(); onFixDisabled(s);
                                    }
                                }}
                            >
                                {fixDisabledLabel}
                            </span>
                        )}
                    </button>
                ))}
            </div>
            <p className="text-[11px] leading-relaxed text-muted-foreground">
                {footnote
                    ?? '样例是同一条「深圳全屋定制哪家好」跑四款风格的真实产出，差异只来自风格本身。'}
            </p>
        </div>
    );
}
