import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

// [R4 §D] 第五个 tab:回报过成功、服务端没核实过。少这一个 key,后端返
// `reported_success_unverified` 的文章在发布中心**四个 tab 里同时不见**。
export type ArticleStatusKey =
  | 'unpublished' | 'inProgress' | 'published' | 'reportedUnverified' | 'rejected';
export type ArticleStatusTone = 'neutral' | 'progress' | 'danger' | 'pending';

export interface ArticleStatusOption {
  key: ArticleStatusKey;
  label: string;
  count: number;
  tone?: ArticleStatusTone;
  icon?: ReactNode;
}

interface ArticleStatusFilterProps {
  options: ArticleStatusOption[];
  active: ArticleStatusKey;
  onChange: (key: ArticleStatusKey) => void;
  className?: string;
}

/**
 * 发布中心左栏「文章状态」分段筛选器(WO-PUBCENTER-LAYOUT-2026-08-04 · L2)。
 *
 * ## 它替掉了什么
 *
 * 四个状态(未分发/发布中/已分发/已拒稿)原先是**四段各占一截垂直空间**的堆叠分组,
 * 每段还带 `minHeight`(120/180/180/180px)。三组同时非空时机械下限就是
 * 120+180+180+180 = 660px,再加四个标题行和底下两块媒体建议 ≈ 760px+;
 * 而 13 寸笔记本的可用高度只有 700px 上下 —— 于是四组互相挤,谁都只露一条缝。
 * 这就是老板说的「一大堆内容全部挤在一起,争抢这个本来就不多的上下位置」。
 *
 * 改成筛选器之后,同一时刻只渲染一个状态的列表,列表 `flex-1 min-h-0` 吃满剩余高度,
 * 四个状态**不再瓜分**垂直空间,只占顶部这一行。
 *
 * ## 🔴 这不是"又加了一层 tab"
 *
 * CLAUDE.md 有铁律:「详情 tab 内必须用折叠面板,禁止再加二级 tab」。这里不违反 ——
 * tab 切的是**不同功能**(代发/自助发布/发布记录 是三套不同的操作),
 * 筛选器切的是**同一个集合的子集**(同一批文章按状态过滤),交互语义不是一回事。
 * 为了不让人在视觉上混淆,这里刻意做成 segmented control(一个带边框的浅色凹槽,
 * 选中项是凸起的实心块),而不是顶部那排 tab 的下划线样式;
 * 语义上用 `radiogroup`/`radio` 而不是 `tablist`/`tab`,读屏软件读出来也是"单选",不是"标签页"。
 *
 * ## 🔴 2026-07-30 工单 T2 的成果在这里必须继续成立
 *
 * T2 修的是:三个兄弟分组原本 `{xxx.length > 0 && (…)}` 条件渲染,空就整块消失,
 * 上游过滤器一削空,老板看到的是「之前的历史记录不见了」。当时的修法是"标题永远渲染(带 0)"。
 *
 * 分段控件把这条**加强**了:四个计数常驻一行、**永远同时可见**,
 * 不用滚动、也不用展开任何东西就知道"已分发有 13 篇"。
 * 所以下面渲染 count 时**不允许**出现 `count > 0 &&` 这类条件 —— 0 也要显示。
 */
export function ArticleStatusFilter({ options, active, onChange, className }: ArticleStatusFilterProps) {
  return (
    <div
      role="radiogroup"
      aria-label="按状态筛选文章"
      data-status-filter-root=""
      data-active-status={active}
      className={cn(
        'flex w-full shrink-0 items-stretch gap-0.5 rounded-md border border-border bg-secondary/40 p-0.5',
        className,
      )}
    >
      {options.map(opt => {
        const selected = opt.key === active;
        const toneText = {
          neutral: 'text-foreground',
          progress: 'text-blue-500 dark:text-blue-400',
          danger: 'text-red-500 dark:text-red-400',
          // [R4 §D] 待核实:琥珀色。不复用 progress 的蓝 —— 那会读成"还在跑",
          // 而这一档的意思恰恰是"没人在跑了,等人核实"。
          pending: 'text-amber-500 dark:text-amber-400',
        }[opt.tone || 'neutral'];
        return (
          <button
            key={opt.key}
            type="button"
            role="radio"
            aria-checked={selected}
            title={`${opt.label} ${opt.count} 篇`}
            onClick={() => onChange(opt.key)}
            data-status-filter={opt.key}
            data-status-filter-count={opt.count}
            data-status-filter-active={selected ? 'true' : 'false'}
            className={cn(
              'flex min-w-0 flex-1 items-center justify-center gap-1 rounded px-1 py-1.5 text-[11px] font-medium transition-colors',
              selected
                ? 'bg-background shadow-sm ' + toneText
                : 'text-muted-foreground hover:bg-background/60 hover:text-foreground',
            )}
          >
            {opt.icon}
            <span className="truncate">{opt.label}</span>
            {/* 计数常驻:0 也渲染。空组计数消失 = 回退 2026-07-30 T2。 */}
            <span
              data-status-filter-badge={opt.key}
              className={cn(
                'shrink-0 tabular-nums',
                selected ? 'opacity-90' : 'opacity-70',
              )}
            >
              {opt.count}
            </span>
          </button>
        );
      })}
    </div>
  );
}
