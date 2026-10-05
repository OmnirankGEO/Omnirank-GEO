/**
 * ManagedSheet — C 端托管套餐 Dialog (CTO-15.2 commit 19 · 老板拍方案 C)
 *
 * 不离开 /c/chat,弹 Dialog 显示用户托管套餐列表 + 详情。
 * 替代 CEndDrawer 原"托管套餐 → /managed"链接 (避免 C 端进代理 Layout 看到代理 Sidebar)。
 *
 * 状态机:
 *   list 态: 展示所有 campaigns,点单个卡片切到详情
 *   detail 态: 内嵌 CampaignDashboard + PendingReviewQueue,顶部"返回列表"按钮
 *   关闭 Dialog 时重置 selectedId
 */

import { useState, useEffect } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { ArrowLeft, Loader2, ChevronRight, Sparkles } from 'lucide-react';
import { cn } from '@/lib/utils';
import { managedApi, CampaignDashboard, PendingReviewQueue } from '@/components/managed';
import type { Campaign } from '@/components/managed/types';

const STATUS_BADGE: Record<string, { label: string; className: string }> = {
  active:               { label: '运行中', className: 'bg-emerald-500/15 text-emerald-700 border-emerald-500/30' },
  paused:               { label: '已暂停', className: 'bg-amber-500/15 text-amber-700 border-amber-500/30' },
  depleted:             { label: '算力用尽', className: 'bg-red-500/15 text-red-700 border-red-500/30' },
  keyword_blocked:      { label: '关键词屏蔽', className: 'bg-orange-500/15 text-orange-700 border-orange-500/30' },
  user_cancelled:       { label: '已取消', className: 'bg-muted text-muted-foreground' },
  dormancy_converted:   { label: '已转赠送', className: 'bg-purple-500/15 text-purple-700 border-purple-500/30' },
};

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function ManagedSheet({ open, onOpenChange }: Props) {
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  // 打开时拉列表
  useEffect(() => {
    if (!open) return;
    setLoading(true);
    managedApi.listCampaigns({})
      .then(r => setCampaigns(r.campaigns))
      .catch(() => setCampaigns([]))
      .finally(() => setLoading(false));
  }, [open]);

  // 关闭时重置详情态
  useEffect(() => {
    if (!open) setSelectedId(null);
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto p-0">
        <DialogHeader className="px-6 pt-6 pb-3 sticky top-0 bg-background z-10 border-b">
          <DialogTitle className="flex items-center gap-2">
            {selectedId ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setSelectedId(null)}
                className="h-7 -ml-2 text-sm"
              >
                <ArrowLeft className="h-3 w-3 mr-1" />返回列表
              </Button>
            ) : (
              <>
                <Sparkles className="h-4 w-4 text-amber-500" />
                <span>我的 GEO 托管套餐</span>
              </>
            )}
          </DialogTitle>
        </DialogHeader>

        <div className="px-6 pb-6">
          {selectedId ? (
            /* 详情态 */
            <div id="pending-reviews-anchor" className="space-y-4">
              <CampaignDashboard campaignId={selectedId} />
              <div>
                <h3 className="text-sm font-semibold mb-2">📋 待审文章</h3>
                <PendingReviewQueue campaignId={selectedId} />
              </div>
            </div>
          ) : loading ? (
            <div className="py-12 text-center">
              <Loader2 className="h-5 w-5 animate-spin mx-auto text-muted-foreground" />
            </div>
          ) : campaigns.length === 0 ? (
            <div className="py-12 text-center text-sm text-muted-foreground">
              暂无托管套餐
              <div className="mt-2 text-xs">在 AI 对话里说"帮我上榜 XX 关键词"即可创建</div>
            </div>
          ) : (
            <div className="space-y-2 mt-3">
              {campaigns.map(c => {
                const balance = c.balance_yuan;
                const balancePct = c.total_recharged_yuan > 0 ? (balance / c.total_recharged_yuan) * 100 : 0;
                const status = STATUS_BADGE[c.status] || { label: c.status, className: '' };
                return (
                  <Card
                    key={c.id}
                    className="cursor-pointer hover:border-primary/50 transition-colors"
                    onClick={() => setSelectedId(c.id)}
                  >
                    <CardContent className="p-3">
                      <div className="flex items-center justify-between gap-3">
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="font-semibold text-sm">{c.keyword}</span>
                            <Badge variant="outline" className={cn('text-[10px]', status.className)}>
                              {status.label}
                            </Badge>
                            {c.target_display_label && (
                              <span className="text-[10px] text-muted-foreground">{c.target_display_label}</span>
                            )}
                          </div>
                          <div className="text-xs text-muted-foreground mt-1">
                            余额 ¥{balance.toFixed(0)} / ¥{c.total_recharged_yuan.toFixed(0)} ({balancePct.toFixed(0)}%) ·
                            已发 {c.delivered_articles} 篇 ·
                            {c.mode === 'full_auto' ? '⚡ 全托管' : '🔒 半自动'}
                          </div>
                        </div>
                        <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0" />
                      </div>
                    </CardContent>
                  </Card>
                );
              })}

              {/* 提示 */}
              <div className="rounded border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-700 dark:text-amber-300 mt-3">
                💡 想新建套餐？关闭这里在对话里说"帮我上榜 XX 关键词"即可。
              </div>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
