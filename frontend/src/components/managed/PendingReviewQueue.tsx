/**
 * 半自动模式待审队列
 *
 * 用户操作：
 *   - 通过 → 立即发布
 *   - 拒绝 → AI 重写
 *   - 24h 不审 → 自动发布（系统）
 */

import { useEffect, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Loader2, CheckCircle2, X, Clock, FileText } from 'lucide-react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';

import * as managedApi from './api';
import type { PendingReview } from './types';

interface Props {
  /** 不传 campaignId 则取当前用户全部 */
  campaignId?: number;
  onChange?: () => void;
}

export function PendingReviewQueue({ campaignId, onChange }: Props) {
  const [reviews, setReviews] = useState<PendingReview[]>([]);
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState<number | null>(null);
  // 卡片可点击展开看全文
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
  // 拒绝弹窗状态
  const [rejectTarget, setRejectTarget] = useState<number | null>(null);
  const [rejectNote, setRejectNote] = useState('');

  const toggleExpand = (id: number) => {
    setExpandedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const refresh = async () => {
    setLoading(true);
    try {
      if (campaignId) {
        const detail = await managedApi.getCampaignDetail(campaignId);
        setReviews(detail.pending_reviews);
      } else {
        const r = await managedApi.getUserPendingReviews();
        setReviews(r.reviews);
      }
    } catch (e: any) {
      toast.error(e?.message || '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [campaignId]);

  async function handleApprove(rid: number) {
    setActing(rid);
    try {
      await managedApi.approveReview(rid);
      toast.success('已通过审核 → AI 立即发布');
      void refresh();
      onChange?.();
    } catch (e: any) {
      toast.error(e?.message || '审核失败');
    } finally {
      setActing(null);
    }
  }

  async function handleRejectConfirm() {
    if (rejectTarget === null) return;
    setActing(rejectTarget);
    try {
      await managedApi.rejectReview(rejectTarget, rejectNote);
      toast.success('已拒绝 → AI 下次 tick 重写');
      setRejectTarget(null);
      setRejectNote('');
      void refresh();
      onChange?.();
    } catch (e: any) {
      toast.error(e?.message || '拒绝失败');
    } finally {
      setActing(null);
    }
  }

  if (loading && reviews.length === 0) {
    return (
      <div className="py-12 text-center">
        <Loader2 className="h-5 w-5 animate-spin mx-auto" />
      </div>
    );
  }

  if (reviews.length === 0) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-muted-foreground">
          暂无待审文章
        </CardContent>
      </Card>
    );
  }

  return (
    <>
      <div id="pending-reviews" className="space-y-3">
        {reviews.map(r => {
          const remainingMs = new Date(r.auto_publish_at).getTime() - Date.now();
          const remainingHours = Math.max(0, Math.floor(remainingMs / 3600000));
          const isExpanded = expandedIds.has(r.id);

          return (
            <Card key={r.id} className="transition-colors hover:bg-muted/30">
              <CardHeader
                className="pb-2 cursor-pointer select-none"
                onClick={() => toggleExpand(r.id)}
                title={isExpanded ? '点击折叠' : '点击展开看全文'}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex-1 min-w-0">
                    <CardTitle className="text-sm flex items-center gap-2">
                      <FileText className="h-4 w-4" />
                      {r.title}
                    </CardTitle>
                    {r.keyword && (
                      <Badge variant="outline" className="mt-1 text-[10px]">{r.keyword}</Badge>
                    )}
                  </div>
                  <Badge variant="outline" className="text-[10px] gap-1">
                    <Clock className="h-3 w-3" />
                    {remainingHours > 0 ? `${remainingHours}h 后自动发` : '即将自动发'}
                  </Badge>
                </div>
              </CardHeader>
              <CardContent className="space-y-2 text-xs">
                <div
                  className={cn('text-muted-foreground cursor-pointer', !isExpanded && 'line-clamp-3')}
                  onClick={() => toggleExpand(r.id)}
                  title={isExpanded ? '点击折叠' : '点击展开看全文'}
                >
                  {r.content_preview}
                  {!isExpanded && r.content_preview && r.content_preview.length > 120 && (
                    <span className="text-sky-400 ml-1">[展开看全文]</span>
                  )}
                </div>

                {r.ai_reasoning && (
                  <div className="rounded border bg-amber-500/5 border-amber-500/20 p-2 text-[11px]">
                    <span className="font-medium">AI 写这篇的理由：</span>
                    {r.ai_reasoning}
                  </div>
                )}

                <div className="text-[11px] text-muted-foreground">
                  计划发布到：{r.platforms_to_publish.join(' / ')} · 预估 ¥{r.estimated_publish_cost_yuan.toFixed(0)}
                </div>

                <div className="flex gap-2 pt-2">
                  <Button
                    size="sm"
                    onClick={() => handleApprove(r.id)}
                    disabled={acting === r.id}
                    className="bg-emerald-600 hover:bg-emerald-700"
                  >
                    {acting === r.id ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <CheckCircle2 className="h-3 w-3 mr-1" />}
                    通过 → 立即发布
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => { setRejectTarget(r.id); setRejectNote(''); }}
                    disabled={acting === r.id}
                  >
                    <X className="h-3 w-3 mr-1" />
                    拒绝重写
                  </Button>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* 拒绝理由弹窗 */}
      <Dialog open={rejectTarget !== null} onOpenChange={v => { if (!v) setRejectTarget(null); }}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>拒绝理由</DialogTitle>
            <DialogDescription>告诉 AI 为什么拒绝，它会参考重写</DialogDescription>
          </DialogHeader>
          <textarea
            className="w-full min-h-[100px] rounded-lg border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring resize-none"
            placeholder="例如：语气太生硬，换个更亲切的表达"
            value={rejectNote}
            onChange={e => setRejectNote(e.target.value)}
            autoFocus
          />
          <DialogFooter>
            <Button variant="ghost" onClick={() => setRejectTarget(null)}>取消</Button>
            <Button onClick={handleRejectConfirm} disabled={acting !== null}>
              {acting !== null ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : null}
              确认拒绝
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
