/**
 * TrialPassReviewPage — 代理审批试用申请页（v3.2 Phase 3）
 *
 * 路径: /agent/trial-pass-review
 * 代理查待审批的 L0 用户试用申请，点击批准/拒绝
 */

import { useEffect, useState } from 'react';
import { authFetch } from '@/lib/api';
import { toast } from 'sonner';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Loader2, UserPlus, CheckCircle, XCircle, Clock, Coins } from 'lucide-react';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

interface ReviewRecord {
  trial_id: number;
  recipient_user_id: number;
  recipient_name: string;
  recipient_registered_at: string | null;
  recipient_balance: number;
  cost_points: number;
  reward_points: number;
  applied_at: string | null;
}

function formatTime(iso: string | null): string {
  if (!iso) return '-';
  const d = new Date(iso);
  const now = Date.now();
  const diff = now - d.getTime();
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (diff < minute) return '刚刚';
  if (diff < hour) return `${Math.floor(diff / minute)} 分钟前`;
  if (diff < day) return `${Math.floor(diff / hour)} 小时前`;
  return `${Math.floor(diff / day)} 天前`;
}

export default function TrialPassReviewPage() {
  const [confirmDialog, askConfirm] = useConfirmDialog();
  const [records, setRecords] = useState<ReviewRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [processingId, setProcessingId] = useState<number | null>(null);

  const loadList = async () => {
    setLoading(true);
    try {
      const res = await authFetch('/api/trial-pass/pending-review');
      if (res.ok) {
        const data = await res.json();
        setRecords(data.records || []);
      }
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadList();
  }, []);

  const handleApprove = async (trial_id: number) => {
    if (!(await askConfirm({ title: '批准后该用户可使用服务方工作台 24 小时，您将获得 100 算力带看奖。确认批准吗？' }))) return;
    setProcessingId(trial_id);
    try {
      const res = await authFetch(`/api/trial-pass/${trial_id}/approve`, { method: 'POST' });
      const data = await res.json();
      if (res.ok && data.success) {
        toast.success(`已批准，到账 ${data.reward_points} 算力带看奖`);
        loadList();
      } else {
        toast.error(data.detail || '批准失败');
      }
    } catch (e: any) {
      toast.error(e.message || '网络错误');
    } finally {
      setProcessingId(null);
    }
  };

  const handleReject = async (trial_id: number) => {
    const reason = prompt('请简单说明拒绝原因（可选，会通知到用户）:') || '';
    if (!(await askConfirm({ title: '拒绝后用户 260 算力将全额退还，您不获得带看奖。确认吗？' }))) return;
    setProcessingId(trial_id);
    try {
      const res = await authFetch(`/api/trial-pass/${trial_id}/reject`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason }),
      });
      const data = await res.json();
      if (res.ok && data.success) {
        toast.success('已拒绝，算力已退还用户');
        loadList();
      } else {
        toast.error(data.detail || '操作失败');
      }
    } catch (e: any) {
      toast.error(e.message || '网络错误');
    } finally {
      setProcessingId(null);
    }
  };

  return (
    <div className="container max-w-3xl py-6 space-y-4">
      <div>
        <h1 className="text-xl font-semibold flex items-center gap-2">
          <UserPlus className="h-5 w-5" />
          服务方试用申请审批
        </h1>
        <p className="text-sm text-muted-foreground mt-1">
          审批通过后可获 100 算力带看奖 · 拒绝不扣用户算力
        </p>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-10 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin mr-2" />
          加载中...
        </div>
      ) : records.length === 0 ? (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            当前没有待审批的试用申请
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {records.map((r) => (
            <Card key={r.trial_id}>
              <CardContent className="p-4 space-y-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="space-y-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{r.recipient_name}</span>
                      <Badge variant="outline" className="text-[11px]">普通用户</Badge>
                    </div>
                    <div className="text-xs text-muted-foreground space-y-0.5">
                      <div className="flex items-center gap-1">
                        <Clock className="h-3 w-3" />
                        申请时间：{formatTime(r.applied_at)}
                      </div>
                      <div className="flex items-center gap-1">
                        <Coins className="h-3 w-3" />
                        当前余额：{r.recipient_balance.toLocaleString()} 算力
                      </div>
                    </div>
                  </div>
                  <div className="text-right text-xs space-y-0.5">
                    <div className="text-muted-foreground">批准后您可得</div>
                    <div className="text-emerald-600 font-semibold text-sm">+¥{(r.reward_points / 130).toFixed(2)}</div>
                  </div>
                </div>

                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleReject(r.trial_id)}
                    disabled={processingId !== null}
                    className="flex-1"
                  >
                    <XCircle className="h-4 w-4 mr-1" />
                    拒绝
                  </Button>
                  <Button
                    size="sm"
                    onClick={() => handleApprove(r.trial_id)}
                    disabled={processingId !== null}
                    className="flex-1"
                  >
                    {processingId === r.trial_id ? (
                      <Loader2 className="h-4 w-4 animate-spin mr-1" />
                    ) : (
                      <CheckCircle className="h-4 w-4 mr-1" />
                    )}
                    批准
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
      {confirmDialog}
    </div>
  );
}
