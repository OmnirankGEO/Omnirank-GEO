// [WO_MEDIA_BOARD_UX_CLOSURE §2] 「N 条提示 · 可发布」徽章的明细出口。
//
// 病根(Owner 截图):发布中心那颗 `{n} 条提示 · 可发布` 徽章**全站没有任何入口** ——
// 代理看到"3 条提示"却点不开、不知道是哪 3 条、更没法处理。铁律
// (feedback_hint_must_help_or_hide):**提示要么帮人解决问题,要么不显示**。
//
// 🔴 语义红线(工单点名):这里如实说「提示」。任何文案都不许把它包装成「审核通过」
//    —— 提示是"我们机审看到的几处可改进",不是"审核结论"。
//
// 🔴 三个动作全部复用既有端点,**不新造第二套口径**:
//    · AI 修复 → POST /api/articles/{id}/repair-finding(免费额度 + 高风险闸都在服务端)
//    · 去编辑 → 跳既有文章编辑页
//    · 忽略   → POST /api/articles/batch-advisory-continue(落库口径在 topics 层,
//               与写作大厅"忽略并继续"同一条链,天然留痕)
import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, Check, Loader2, Pencil, Sparkles, X } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { lazyToast } from '@/lib/lazyToast';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { cn } from '@/lib/utils';

export type AdvisorySpan = {
  matched_text?: string;
  excerpt?: string;
  ai_repairable?: boolean;
  ai_repair_block_reason?: string | null;
};

export type AdvisoryCard = {
  code: string;
  severity?: string;
  count?: number;
  title?: string;
  message?: string;
  source?: string;
  spans?: AdvisorySpan[];
  repairable_count?: number;
  ai_repairable_count?: number;
};

type AdvisoryResponse = {
  success?: boolean;
  article_id?: number;
  topic_id?: number | null;
  title?: string;
  advisory_state?: string;
  advisory_open_count?: number;
  cards?: AdvisoryCard[];
  ai_repair_blocked?: boolean;
};

const SEVERITY_LABEL: Record<string, string> = {
  hard: '需要处理',
  soft: '建议处理',
  advisory: '可选优化',
  warning: '可选优化',
};

export function ArticleAdvisoryDrawer({
  open,
  onOpenChange,
  articleId,
  topicId,
  articleTitle,
  onEdit,
  onResolved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 文章 id(repair-finding / advisory 明细都按它取) */
  articleId?: number;
  /** 选题 id(忽略动作落库在 topics 层;拿不到就不渲染忽略按钮) */
  topicId?: number | null;
  articleTitle?: string;
  /** 「去编辑」由上层决定跳哪(发布中心与写作大厅入口不同);不传则不渲染该按钮 */
  onEdit?: (articleId: number) => void;
  /** 计数变化(修复/忽略成功)→ 把**重新取回的**计数交给上层,列表徽章即时刷新。
   *  刻意回传后端的新值而不是让上层自己减一:计数 SSOT 在后端
   *  (`_advisory_state`,与徽章同源),前端自算必然漂。 */
  onResolved?: (next: { articleId: number; advisoryOpenCount: number; advisoryState: string }) => void;
}) {
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [data, setData] = useState<AdvisoryResponse | null>(null);
  // 正在修复的 span key(code::matched_text),避免连点重复下单
  const [repairing, setRepairing] = useState<string | null>(null);
  const [ignoring, setIgnoring] = useState(false);
  // onResolved 走 ref:上层多半传的是内联箭头函数,直接进 load 的依赖会让
  // load 每次渲染都换新身份 → useEffect([open, load]) 每帧重拉一次明细。
  const onResolvedRef = useRef(onResolved);
  useEffect(() => { onResolvedRef.current = onResolved; }, [onResolved]);

  const load = useCallback(async (notify = false) => {
    if (!articleId) return;
    setLoading(true);
    try {
      const res = await authFetch(`/api/articles/${articleId}/advisory`);
      if (!res.ok) throw new Error('load failed');
      const d = (await res.json()) as AdvisoryResponse;
      setData(d);
      setLoadError(false);
      // notify=true 只在"处理完一条之后"那次重取时回调 —— 打开抽屉那次不该
      // 触发上层刷新(没有任何变化,徒增一次列表重渲染)。
      if (notify) {
        onResolvedRef.current?.({
          articleId,
          advisoryOpenCount: Number(d?.advisory_open_count || 0),
          advisoryState: String(d?.advisory_state || ''),
        });
      }
    } catch {
      // fail-soft:抽屉打不开明细不该把发布链路带崩,给可重试错误态。
      setData(null);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [articleId]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  const cards = data?.cards || [];
  const openCount = Number(data?.advisory_open_count || 0);
  const effectiveTopicId = topicId ?? data?.topic_id ?? null;

  const repairSpan = async (card: AdvisoryCard, span: AdvisorySpan) => {
    const matched = (span.matched_text || '').trim();
    if (!articleId || !matched) return;
    const key = `${card.code}::${matched}`;
    setRepairing(key);
    try {
      const res = await authFetch(`/api/articles/${articleId}/repair-finding`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ finding_code: card.code, matched_text: matched }),
      });
      const d = await res.json().catch(() => ({}));
      if (res.ok && d?.success) {
        lazyToast.success('已修复这一处');
        await load(true);
      } else {
        // 服务端 §13 合同:失败一定带人话原因 + 下一步,原样透出,不自造文案。
        lazyToast.error(d?.message || d?.detail?.message || '这一处没能自动修好,可以自己改或先忽略');
      }
    } catch {
      lazyToast.error('网络不稳,这一处没修成,可以稍后再试');
    } finally {
      setRepairing(null);
    }
  };

  const ignoreAll = async () => {
    if (!effectiveTopicId) return;
    setIgnoring(true);
    try {
      const res = await authFetch('/api/articles/batch-advisory-continue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic_ids: [effectiveTopicId] }),
      });
      const d = await res.json().catch(() => ({}));
      if (res.ok && d?.success) {
        lazyToast.success('已记录:这些提示你选择先不处理');
        await load(true);
      } else {
        lazyToast.error(d?.message || d?.detail?.message || '没能记录,请稍后再试');
      }
    } catch {
      lazyToast.error('网络不稳,没能记录,请稍后再试');
    } finally {
      setIgnoring(false);
    }
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-md" data-testid="article-advisory-drawer">
        <SheetHeader>
          <SheetTitle className="text-base">
            这篇的 {openCount > 0 ? `${openCount} 条` : ''}提示
          </SheetTitle>
          <SheetDescription className="text-xs">
            {/* 🔴 语义红线:说提示,不说审核结论。 */}
            这些是机器通读全文后看到的几处可以改进的地方,<span className="font-medium">不影响发布</span>。
            {articleTitle ? `当前文章:${articleTitle}` : ''}
          </SheetDescription>
        </SheetHeader>

        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-4 pb-4">
          {loading && (
            <div className="flex items-center gap-2 py-6 text-xs text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin" />
              正在取这篇的提示明细…
            </div>
          )}

          {!loading && loadError && (
            <div className="flex flex-wrap items-center gap-2 rounded-md border border-border/60 bg-muted/20 px-3 py-2.5 text-xs">
              <AlertCircle className="size-3.5 text-muted-foreground" />
              <span className="text-muted-foreground">提示明细暂时取不到</span>
              <Button size="sm" variant="outline" className="ml-auto h-7 px-2.5 text-xs" onClick={() => void load()}>
                点此重试
              </Button>
            </div>
          )}

          {/* 真空态:计数与明细同源(后端同一个 _advisory_state),这里为空就是真的没有了。 */}
          {!loading && !loadError && cards.length === 0 && (
            <div className="flex items-center gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2.5 text-xs text-emerald-700 dark:text-emerald-300">
              <Check className="size-3.5" />
              这篇现在没有待处理的提示了。
            </div>
          )}

          {!loading && !loadError && cards.map((card) => {
            const spans = (card.spans || []).filter(Boolean);
            return (
              <div key={card.code} className="rounded-md border border-border/60 bg-background/60 p-2.5" data-testid="advisory-card">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-xs font-medium">{card.title || card.code}</span>
                  <Badge variant="secondary" className="px-1 py-0 text-[9px]">
                    {SEVERITY_LABEL[String(card.severity || '')] || '可选优化'}
                  </Badge>
                  {Number(card.count || 0) > 1 && (
                    <span className="text-[10px] text-muted-foreground">{card.count} 处</span>
                  )}
                </div>
                {card.message && (
                  <p className="mt-1 text-[10px] leading-relaxed text-muted-foreground">{card.message}</p>
                )}
                <div className="mt-1.5 space-y-1">
                  {spans.map((span, i) => {
                    const matched = (span.matched_text || '').trim();
                    const key = `${card.code}::${matched}`;
                    const busy = repairing === key;
                    // 🔴 能不能给 AI 修,由**服务端**标注(高风险文章/高风险片段一律 false),
                    //    前端只照做。端点自己还会再拒一次 —— 两层同源,不是两份口径。
                    const canAiFix = !data?.ai_repair_blocked && !!span.ai_repairable && !!matched;
                    return (
                      <div key={`${key}-${i}`} className="rounded border border-border/50 bg-muted/20 px-2 py-1.5">
                        <div className="text-[10px] leading-relaxed text-foreground/90 break-words">
                          {matched || span.excerpt || '(这一处没能定位到具体文字)'}
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-1.5">
                          {canAiFix ? (
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={busy}
                              onClick={() => void repairSpan(card, span)}
                              className="h-6 gap-1 px-2 text-[10px]"
                              data-testid="advisory-ai-fix-btn"
                            >
                              {busy ? <Loader2 className="size-3 animate-spin" /> : <Sparkles className="size-3" />}
                              AI 一键修复<span className="text-muted-foreground">·免费</span>
                            </Button>
                          ) : (
                            <span className="text-[10px] text-muted-foreground">
                              {/* 不给假按钮:这一处 AI 不能改,就直说该走哪条路。 */}
                              {matched ? '这一处需要你自己改或走人工签发' : '这一处定位不到原文,只能自己改'}
                            </span>
                          )}
                          {onEdit && articleId && (
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => onEdit(articleId)}
                              className="h-6 gap-1 px-2 text-[10px]"
                              data-testid="advisory-goto-edit-btn"
                            >
                              <Pencil className="size-3" />
                              去编辑
                            </Button>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>

        {/* 忽略(留痕):落库走 topics 层,与写作大厅"忽略并继续"同一条链。
            拿不到 topic_id → 不渲染(不给点了没反应的按钮)。 */}
        {!loading && !loadError && cards.length > 0 && effectiveTopicId && (
          <div className="shrink-0 border-t border-border/60 px-4 py-3">
            <Button
              size="sm"
              variant="outline"
              disabled={ignoring}
              onClick={() => void ignoreAll()}
              className={cn('h-8 w-full gap-1 text-xs')}
              data-testid="advisory-ignore-btn"
            >
              {ignoring ? <Loader2 className="size-3.5 animate-spin" /> : <X className="size-3.5" />}
              这些先不处理(会记录下来)
            </Button>
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}
