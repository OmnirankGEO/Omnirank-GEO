/**
 * 套餐看板 — 单个套餐的完整运行视图
 *
 * 功能（v2 决策）：
 *   - 余额条 + 完成进度
 *   - 已交付文章数 / 已扣费 / 当前 SOV
 *   - AI 工作日志（managed_actions）
 *   - 暂停 / 恢复 / 加充按钮
 *   - 待审队列入口
 */

import { useEffect, useState, useCallback } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import {
  Loader2, Pause, Play, Plus, AlertTriangle, CheckCircle2, Clock,
  TrendingUp, Activity, FileText, X,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';

import * as managedApi from './api';
import type { CampaignDetail, CampaignAction } from './types';
import { TopUpDialog } from './TopUpDialog';

interface Props {
  campaignId: number;
  onClose?: () => void;
}

const STATUS_BADGE: Record<string, { label: string; className: string }> = {
  active:               { label: '运行中', className: 'bg-emerald-500/15 text-emerald-700 border-emerald-500/30' },
  paused:               { label: '已暂停', className: 'bg-amber-500/15 text-amber-700 border-amber-500/30' },
  depleted:             { label: '余额扣完', className: 'bg-red-500/15 text-red-700 border-red-500/30' },
  keyword_blocked:      { label: '关键词屏蔽', className: 'bg-orange-500/15 text-orange-700 border-orange-500/30' },
  user_cancelled:       { label: '已取消', className: 'bg-muted text-muted-foreground' },
  dormancy_converted:   { label: '已转赠送', className: 'bg-purple-500/15 text-purple-700 border-purple-500/30' },
};

export function CampaignDashboard({ campaignId, onClose }: Props) {
  const [detail, setDetail] = useState<CampaignDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [actLoading, setActLoading] = useState(false);
  const [topUpOpen, setTopUpOpen] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await managedApi.getCampaignDetail(campaignId);
      setDetail(data);
    } catch (e: any) {
      toast.error(e?.message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, [campaignId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function handlePause() {
    setActLoading(true);
    try {
      await managedApi.pauseCampaign(campaignId);
      toast.success('已暂停');
      void refresh();
    } catch (e: any) {
      toast.error(e?.message || '暂停失败');
    } finally {
      setActLoading(false);
    }
  }

  async function handleResume() {
    setActLoading(true);
    try {
      const r = await managedApi.resumeCampaign(campaignId);
      if (r.requires_re_estimate) {
        toast.info(`市场已变化，需重新评估：${r.reason}`);
      } else {
        toast.success('已恢复');
      }
      void refresh();
    } catch (e: any) {
      toast.error(e?.message || '恢复失败');
    } finally {
      setActLoading(false);
    }
  }

  if (loading && !detail) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-6 w-6 animate-spin" />
      </div>
    );
  }

  if (!detail) {
    return <div className="p-6 text-center text-sm text-muted-foreground">套餐不存在</div>;
  }

  const balancePercent = detail.total_recharged_yuan > 0
    ? (detail.balance_yuan / detail.total_recharged_yuan) * 100
    : 0;
  const consumedPercent = 100 - balancePercent;

  const statusInfo = STATUS_BADGE[detail.status] || { label: detail.status, className: '' };

  return (
    <div className="space-y-4">
      {/* 顶部信息 */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-start justify-between">
            <div>
              <CardTitle className="text-lg flex items-center gap-2">
                🎯 {detail.keyword}
                <Badge variant="outline" className={statusInfo.className}>{statusInfo.label}</Badge>
              </CardTitle>
              <div className="text-xs text-muted-foreground mt-1">
                {detail.target_display_label} ·
                创建于 {new Date(detail.created_at).toLocaleDateString('zh-CN')}
              </div>
            </div>
            {onClose && (
              <Button size="sm" variant="ghost" onClick={onClose}>
                <X className="h-4 w-4" />
              </Button>
            )}
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* 余额条 */}
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-xs text-muted-foreground">余额 / 总充值</span>
              <span className="text-xs font-medium">
                ¥{detail.balance_yuan.toFixed(0)} / ¥{detail.total_recharged_yuan.toFixed(0)}
              </span>
            </div>
            <Progress value={consumedPercent} className="h-2" />
            <div className="flex items-center justify-between mt-1 text-[11px] text-muted-foreground">
              <span>已用 ¥{detail.total_consumed_yuan.toFixed(0)} ({consumedPercent.toFixed(0)}%)</span>
              {detail.low_balance_warned && (
                <span className="text-amber-600 flex items-center gap-1">
                  <AlertTriangle className="h-3 w-3" /> 余额不足 20%
                </span>
              )}
            </div>
          </div>

          {/* 关键指标 */}
          <div className="grid grid-cols-3 gap-3 text-center">
            <div className="rounded border bg-muted/20 p-2">
              <FileText className="h-4 w-4 mx-auto text-muted-foreground" />
              <div className="text-xl font-bold mt-1">{detail.delivered_articles}</div>
              <div className="text-[10px] text-muted-foreground">已发文章</div>
            </div>
            <div className="rounded border bg-muted/20 p-2">
              <Activity className="h-4 w-4 mx-auto text-muted-foreground" />
              <div className="text-xl font-bold mt-1">{detail.consecutive_zero_detection_days}</div>
              <div className="text-[10px] text-muted-foreground">连续 0 检出天</div>
            </div>
            <div className="rounded border bg-muted/20 p-2">
              <TrendingUp className="h-4 w-4 mx-auto text-muted-foreground" />
              <div className="text-xl font-bold mt-1">{detail.check_frequency_per_day}/天</div>
              <div className="text-[10px] text-muted-foreground">监测频次</div>
            </div>
          </div>

          {/* 操作按钮 */}
          <div className="flex gap-2 flex-wrap">
            {detail.status === 'active' && (
              <Button size="sm" variant="outline" onClick={handlePause} disabled={actLoading}>
                <Pause className="h-3 w-3 mr-1" /> 暂停
              </Button>
            )}
            {detail.status === 'paused' && (
              <Button size="sm" variant="outline" onClick={handleResume} disabled={actLoading}>
                <Play className="h-3 w-3 mr-1" /> 恢复
              </Button>
            )}
            <Button size="sm" onClick={() => setTopUpOpen(true)}>
              <Plus className="h-3 w-3 mr-1" /> 加充
            </Button>
            {detail.pending_reviews.length > 0 && (
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  // 2026-04-19 commit 18: 老板截图反馈"点不动"→ 加 onClick 滚动到底部待审列表
                  document.getElementById('pending-reviews')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }}
              >
                <Clock className="h-3 w-3 mr-1" /> 待审 ({detail.pending_reviews.length})
              </Button>
            )}
          </div>
        </CardContent>
      </Card>

      {/* AI 工作日志 */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">AI 工作日志（最近 20 条）</CardTitle>
        </CardHeader>
        <CardContent>
          {detail.recent_actions.length === 0 ? (
            <div className="py-6 text-center text-xs text-muted-foreground">暂无日志</div>
          ) : (
            <div className="divide-y text-xs">
              {detail.recent_actions.map(a => (
                <ActionRow key={a.id} action={a} />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <TopUpDialog
        open={topUpOpen}
        onOpenChange={setTopUpOpen}
        campaignId={campaignId}
        onTopUp={refresh}
      />
    </div>
  );
}

function ActionRow({ action }: { action: CampaignAction }) {
  const ICON_MAP: Record<string, React.ReactNode> = {
    check_rank:                <Activity className="h-3 w-3 text-blue-500" />,
    replenish:                 <CheckCircle2 className="h-3 w-3 text-emerald-500" />,
    publish_failed:            <X className="h-3 w-3 text-red-500" />,
    skip_expensive:            <AlertTriangle className="h-3 w-3 text-amber-500" />,
    pending_review_created:    <Clock className="h-3 w-3 text-amber-500" />,
    auto_published_after_24h:  <CheckCircle2 className="h-3 w-3 text-emerald-500" />,
    user_approved:             <CheckCircle2 className="h-3 w-3 text-emerald-500" />,
    user_rejected:             <X className="h-3 w-3 text-red-500" />,
    user_withdrew:             <X className="h-3 w-3 text-amber-500" />,
    low_balance_warned:        <AlertTriangle className="h-3 w-3 text-amber-500" />,
    depleted:                  <AlertTriangle className="h-3 w-3 text-red-500" />,
    keyword_blocked_paused:    <AlertTriangle className="h-3 w-3 text-orange-500" />,
  };
  const LABEL_MAP: Record<string, string> = {
    check_rank: '检测排名',
    replenish: 'AI 已补发 1 篇',
    publish_failed: '发布失败',
    skip_expensive: '跳过超贵媒体',
    pending_review_created: 'AI 写完进入待审',
    auto_published_after_24h: '24h 未审 → 自动发布',
    user_approved: '用户审核通过',
    user_rejected: '用户拒绝重写',
    user_withdrew: '用户撤回',
    low_balance_warned: '余额低提醒',
    depleted: '余额扣完自动暂停',
    keyword_blocked_paused: '7 天 0 检出 → 暂停',
  };
  return (
    <div className="flex items-start gap-2 py-2">
      <div className="mt-0.5">{ICON_MAP[action.action_type] || <Activity className="h-3 w-3" />}</div>
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline justify-between gap-2">
          <span className="font-medium">{LABEL_MAP[action.action_type] || action.action_type}</span>
          <span className="text-[10px] text-muted-foreground shrink-0">
            {new Date(action.created_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
          </span>
        </div>
        {action.cost_yuan > 0 && (
          <div className="text-[10px] text-muted-foreground">
            消耗 ¥{action.cost_yuan.toFixed(2)}
          </div>
        )}
        {action.cost_yuan < 0 && (
          <div className="text-[10px] text-emerald-600">
            退还 ¥{Math.abs(action.cost_yuan).toFixed(2)}
          </div>
        )}
        {action.reason && (
          <div className="text-[10px] text-muted-foreground italic mt-0.5">{action.reason}</div>
        )}
      </div>
    </div>
  );
}
