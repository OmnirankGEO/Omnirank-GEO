/**
 * 我的托管套餐列表页 — /managed
 */

import { useEffect, useState } from 'react';
import { useEmbeddedNavigate } from '@/hooks/useEmbeddedNavigate';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Loader2, Sparkles, ChevronRight, Filter } from 'lucide-react';
import { cn } from '@/lib/utils';

import { managedApi } from '@/components/managed';
import type { Campaign, CampaignStatus } from '@/components/managed/types';

const STATUS_BADGE: Record<string, { label: string; className: string }> = {
  active:               { label: '运行中', className: 'bg-emerald-500/15 text-emerald-700 border-emerald-500/30' },
  paused:               { label: '已暂停', className: 'bg-amber-500/15 text-amber-700 border-amber-500/30' },
  depleted:             { label: '余额扣完', className: 'bg-red-500/15 text-red-700 border-red-500/30' },
  keyword_blocked:      { label: '关键词屏蔽', className: 'bg-orange-500/15 text-orange-700 border-orange-500/30' },
  user_cancelled:       { label: '已取消', className: 'bg-muted text-muted-foreground' },
  dormancy_converted:   { label: '已转赠送', className: 'bg-purple-500/15 text-purple-700 border-purple-500/30' },
};

export default function ManagedListPage() {
  const navigate = useEmbeddedNavigate();
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<CampaignStatus | 'all'>('all');

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  async function load() {
    setLoading(true);
    try {
      const r = await managedApi.listCampaigns(filter === 'all' ? {} : { status: filter });
      setCampaigns(r.campaigns);
    } catch {
      setCampaigns([]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="container max-w-5xl mx-auto py-6 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Sparkles className="h-6 w-6 text-amber-500" />
            我的 GEO 托管套餐
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            AI 全自动帮你打榜，余额扣完为止，永不过期
          </p>
        </div>
      </div>

      {/* 过滤器 */}
      <div className="flex items-center gap-2 text-sm">
        <Filter className="h-3 w-3 text-muted-foreground" />
        {(['all', 'active', 'paused', 'depleted'] as const).map(f => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={cn(
              'px-3 py-1 rounded-full text-xs',
              filter === f ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:bg-muted/70'
            )}
          >
            {f === 'all' ? '全部' : STATUS_BADGE[f]?.label}
          </button>
        ))}
      </div>

      {/* 列表 */}
      {loading ? (
        <div className="py-12 text-center"><Loader2 className="h-5 w-5 animate-spin mx-auto" /></div>
      ) : campaigns.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center space-y-3">
            <Sparkles className="h-10 w-10 mx-auto text-muted-foreground/30" />
            <p className="text-sm font-medium text-foreground">AI 全自动托管</p>
            <p className="text-xs text-muted-foreground max-w-sm mx-auto leading-relaxed">
              充值 → AI 自动撰写文章并发布 → 排名上升、余额按篇扣减。
              在 AI 助手对话里说"帮我上榜 XX 关键词"即可创建托管套餐。
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {campaigns.map(c => {
            const balance = c.balance_yuan;
            const balancePct = c.total_recharged_yuan > 0 ? (balance / c.total_recharged_yuan) * 100 : 0;
            const status = STATUS_BADGE[c.status] || { label: c.status, className: '' };
            return (
              <Card
                key={c.id}
                className="cursor-pointer hover:border-primary/50 transition-colors"
                onClick={() => navigate(`/managed/${c.id}`)}
              >
                <CardContent className="p-4">
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-semibold">{c.keyword}</span>
                        <Badge variant="outline" className={status.className}>{status.label}</Badge>
                        <span className="text-[10px] text-muted-foreground">{c.target_display_label}</span>
                      </div>
                      <div className="text-xs text-muted-foreground mt-1">
                        💰 余额 ¥{balance.toFixed(0)} / ¥{c.total_recharged_yuan.toFixed(0)} ({balancePct.toFixed(0)}%) ·
                        📄 已发 {c.delivered_articles} 篇 ·
                        {c.mode === 'full_auto' ? '⚡ 全托管' : '🔒 半自动'}
                      </div>
                    </div>
                    <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0" />
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {/* 提示 */}
      <div className="rounded border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-700 dark:text-amber-300">
        💡 想新建套餐？打开 AI 助手对话，说"帮我上榜 XX 关键词"即可。
        <Button size="sm" variant="link" className="text-amber-700" onClick={() => navigate('/')}>
          打开 AI 助手 →
        </Button>
      </div>
    </div>
  );
}
