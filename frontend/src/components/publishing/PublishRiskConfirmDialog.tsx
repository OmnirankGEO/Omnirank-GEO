import { useEffect, useState } from 'react';
import { AlertTriangle, Loader2, Ban } from 'lucide-react';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';
import { DuplicateConflictPanel } from '@/components/publishing/DuplicateConflictPanel';
import {
  isSubmitBlockedByConflicts, type DuplicateOrderBlock,
} from '@/contracts/duplicateOrderConflict';

interface Props {
  open: boolean;
  articleCount: number;
  mediaCount: number;
  estimatedPoints: number;
  submitting?: boolean;
  onCancel: () => void;
  onConfirm: () => void | Promise<void>;
  /**
   * [WO-BATCH-CONFLICT 2026-08-04] 后端预检返回的冲突组合清单。
   * 非空时弹窗进入「处理窗口」形态:列出冲突 + 给一键剔除,**不再是一个 toast**。
   */
  conflictBlock?: DuplicateOrderBlock | null;
  /** 剔除全部冲突组合。由父层改购物车 —— 算力是从购物车派生的,所以会自动重算。 */
  onRemoveConflicts?: () => void;
  /** 剔除后还剩几个「文章×媒体」组合。0 = 禁止提交空单。 */
  remainingAfterRemoval?: number;
  /** 沙盒教程模式：保留真实确认组件，但零计费、零外发且不要求治理审批。 */
  tutorialMode?: boolean;
  /**
   * [2026-07-30 T1] 清单里此前**已经分发过**的文章。
   *
   * 生产实测(§1.4):后端去重只挡「同文章 + 同媒体」—— 全库 0 行同文同媒并存活跃项,
   * 证明那道真在挡;但「已发布文章换一家媒体」是**合法的一文多投**,24 张订单就是
   * 这么产生的,真实扣费 29,640 算力。合法不等于用户想这么干,所以这条路必须
   * 在扣费前**被看见**。这里只提示、不阻断。
   */
  // 🔴 R6 补充①:第三态 `reported_unverified` —— 回执成功但没核实过。
  //    并进 'published' 会让这个弹窗对没核实过的文章印「已发布」。
  alreadyDistributed?: Array<{ articleId: number; articleTitle: string; state: 'published' | 'in_progress' | 'reported_unverified' }>;
}

const fmtPts = (n: number) => n >= 10000 ? `${(n / 10000).toFixed(1)}w` : n.toLocaleString();

export function PublishRiskConfirmDialog({
  open,
  articleCount,
  mediaCount,
  estimatedPoints,
  submitting = false,
  onCancel,
  onConfirm,
  tutorialMode = false,
  alreadyDistributed = [],
  conflictBlock = null,
  onRemoveConflicts,
  remainingAfterRemoval = 0,
}: Props) {
  const [confirmed, setConfirmed] = useState(false);

  useEffect(() => {
    if (open) setConfirmed(false);
  }, [open]);

  const conflicts = conflictBlock?.conflicts ?? [];
  // 🔴 判定走契约里那个唯一函数,弹窗不自己再写一份 —— 写两份迟早打架。
  const hasConflicts = isSubmitBlockedByConflicts(conflictBlock);
  // 🔴 剔除后一个组合都不剩 = 空单,必须禁止提交(提交空单会建 0 项订单)。
  const wouldBeEmpty = hasConflicts && remainingAfterRemoval <= 0;

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !submitting) onCancel(); }}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {hasConflicts
              ? <Ban className="size-5 text-destructive" />
              : <AlertTriangle className="size-5 text-amber-500" />}
            {hasConflicts
              ? '有组合发不出去'
              : (tutorialMode ? '确认模拟发布' : '确认这次投放')}
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          {hasConflicts && conflictBlock && (
            <DuplicateConflictPanel
              block={conflictBlock}
              remainingAfterRemoval={remainingAfterRemoval}
              disabled={submitting}
              onRemoveConflicts={onRemoveConflicts}
            />
          )}

          {tutorialMode ? (
            <div className="space-y-3">
              <div className="rounded-lg border border-emerald-500/35 bg-emerald-500/10 p-3 text-sm text-foreground">
                <div className="font-medium text-emerald-600 dark:text-emerald-300">
                  教程演练 · 0 算力 · 0 外发 · 无需管理员审批
                </div>
                <p className="mt-1 text-muted-foreground">
                  将模拟提交 {articleCount} 篇文章到 {mediaCount} 个渠道。完成后直接进入效果监测教学。
                </p>
              </div>
              <p className="text-sm text-muted-foreground">
                这一步只帮助你熟悉真实发布按钮和确认位置，不会创建订单或调用外部平台。
              </p>
            </div>
          ) : (
            <>
              {alreadyDistributed.length > 0 && (
                <div
                  data-testid="already-distributed-warning"
                  data-already-distributed-count={alreadyDistributed.length}
                  className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm"
                >
                  <div className="font-medium text-foreground">
                    这批里有 {alreadyDistributed.length} 篇此前已经分发过
                  </div>
                  <ul className="mt-1 space-y-0.5 text-xs text-muted-foreground">
                    {alreadyDistributed.slice(0, 5).map(item => (
                      <li key={item.articleId}>
                        · {item.articleTitle || `文章 ${item.articleId}`}
                        <span className="ml-1">
                          （{item.state === 'published' ? '已发布'
                            : item.state === 'reported_unverified'
                              // 🔴 R6 补充①:这一档此前被并进「已发布」,于是这个弹窗
                              //    会对一篇从没核实过的文章印「已发布」。要说的是
                              //    "你发过、别再发",用它自己的话说就够了。
                              ? '浏览器回报成功 · 平台核实中'
                              : '发布中'}）
                        </span>
                      </li>
                    ))}
                    {alreadyDistributed.length > 5 && (
                      <li>· 其余 {alreadyDistributed.length - 5} 篇…</li>
                    )}
                  </ul>
                  <p className="mt-1.5 text-xs text-muted-foreground">
                    同一篇发到**不同**媒体是允许的，会按新媒体正常计费；
                    发到已经发过的那家会被系统挡下、不扣费。确认要继续吗？
                  </p>
                </div>
              )}
              <div className="rounded-lg border border-amber-400/40 bg-amber-50/70 p-3 text-sm text-amber-950 dark:bg-amber-950/25 dark:text-amber-100">
                这次小榜按你的文章、行业样本和当前可发媒体，帮你选了{' '}
                <span className="font-semibold">{mediaCount}</span> 家媒体，预计消耗{' '}
                <span className="font-semibold">{fmtPts(estimatedPoints)}</span> 算力。
                {articleCount > 1 && (
                  <Badge variant="outline" className="ml-2 border-amber-400/40 bg-background/70 text-[10px]">
                    {articleCount} 篇文章
                  </Badge>
                )}
              </div>

              <div className="space-y-2 text-sm leading-relaxed text-muted-foreground">
                <div className="font-medium text-foreground">请确认：</div>
                <p>1. 媒体发布、收录、AI 引用、排名和转化结果，会受到平台审核、算法变化、文章质量和竞争环境影响。</p>
                <p>2. 小榜提供的是基于历史数据和当前资源的推荐，平台不保证任何具体的排名、曝光、转化效果，也不承诺具体上榜、收录、排名或成交。</p>
                <p>3. 投放后我们会记录执行结果，用真实反馈优化下一次推荐。</p>
              </div>

              <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border bg-background/70 p-3 text-sm">
                <Checkbox checked={confirmed} onCheckedChange={(v) => setConfirmed(v === true)} className="mt-0.5" />
                <span>我已确认预算和风险</span>
              </label>
            </>
          )}

          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <Button type="button" variant="outline" disabled={submitting} onClick={onCancel}>
              返回选择
            </Button>
            <Button
              type="button"
              data-testid="confirm-publish-button"
              // 🔴 冲突未处理前不许直接提交:直接提交只会再撞同一批冲突。
              //   剔除后无剩余(wouldBeEmpty)更要禁 —— 那是空单。
              disabled={(!tutorialMode && !confirmed) || submitting || hasConflicts}
              onClick={onConfirm}
              className="gap-1.5"
            >
              {submitting && <Loader2 className="size-4 animate-spin" />}
              {tutorialMode ? '继续模拟发布' : '我已确认预算和风险，继续投放'}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
