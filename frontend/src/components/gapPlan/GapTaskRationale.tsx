/**
 * 任务卡的「内容」列 —— 纯展示,零写操作
 * (WO_GAPPLAN_RELOCATION_A 2026-08-10 · 阶段 2)
 *
 * 🔴 为什么拆出来:`GapTaskCard` 内建「去写这篇 / 已发布填链接 / 渠道不可用」
 *    三个写操作按钮,整体复用到客户页 = 把执行入口渲染给客户。
 *    真正两边都要的只有「标题 + 为什么写 + 写什么(+ 怎么发)」这一块,
 *    所以拆的是这一块,不是给 GapTaskCard 加一个 readOnly 开关 ——
 *    开关会被将来某次改动忘掉,拆开则**客户侧与那些按钮之间没有代码路径**。
 *
 * 🔴 本组件不认识 action、不认识容量、不发任何请求。它只吃 item 里的文案字段。
 */
import type { GapPlanItem } from '@/lib/gapPlanApi';

interface Props {
  item: Pick<GapPlanItem, 'title' | 'rationale' | 'duplicate_of_item_id'>;
}

export function GapTaskRationale({ item }: Props) {
  return (
    <>
      <p className="text-sm font-medium text-foreground break-words">{item.title}</p>
      <dl className="mt-2 space-y-1 text-xs text-muted-foreground">
        {item.rationale.why && (
          <div className="flex gap-1.5">
            <dt className="shrink-0 text-foreground/70">为什么写</dt>
            <dd className="break-words">{item.rationale.why}</dd>
          </div>
        )}
        {item.rationale.what && (
          <div className="flex gap-1.5">
            <dt className="shrink-0 text-foreground/70">写什么</dt>
            <dd className="break-words">{item.rationale.what}</dd>
          </div>
        )}
        {item.rationale.how && (
          <div className="flex gap-1.5">
            <dt className="shrink-0 text-foreground/70">怎么发</dt>
            <dd className="break-words">{item.rationale.how}</dd>
          </div>
        )}
      </dl>
      {item.duplicate_of_item_id && (
        <p className="mt-2 text-xs text-muted-foreground">
          与「第 {item.duplicate_of_item_id} 篇」覆盖同一个缺口，已合并。
        </p>
      )}
    </>
  );
}

export default GapTaskRationale;
