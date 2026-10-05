/**
 * TrialPassApplyDialog — L0 用户申请代理工作台试用（v3.2 Phase 3）
 *
 * 流程:
 *   1. 点击钱包页 / 抽屉的"申请代理体验"
 *   2. 弹窗确认 → POST /api/trial-pass/apply
 *   3. 提示等待审批
 */

import { useState } from 'react';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Loader2, Crown, Clock } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { toast } from 'sonner';

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess?: () => void;
}

export function TrialPassApplyDialog({ open, onOpenChange, onSuccess }: Props) {
  const [agreed, setAgreed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [reason, setReason] = useState('');

  const handleSubmit = async () => {
    if (!agreed || submitting) return;
    setSubmitting(true);
    try {
      const res = await authFetch('/api/trial-pass/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: reason.trim() || null }),
      });
      const data = await res.json();
      if (res.ok && data.success) {
        toast.success('申请已提交，等待审核');
        onOpenChange(false);
        onSuccess?.();
        if (window.parent !== window) {
          window.parent.postMessage({ type: 'wallet_changed' }, window.location.origin);
        }
      } else {
        toast.error(data.detail || '申请失败');
      }
    } catch (e: any) {
      toast.error(e.message || '网络错误');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Crown className="h-5 w-5 text-amber-500" />
            申请工作台试用
          </DialogTitle>
          <DialogDescription>
            体验 24 小时专业工作台，感受批量管理、白标导出等完整功能
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {/* 权益展示 */}
          <div className="rounded-lg border p-3 space-y-2 bg-gradient-to-br from-emerald-500/5 to-blue-500/5">
            <div className="text-xs font-medium text-muted-foreground">试用期内可使用</div>
            <ul className="text-sm space-y-1">
              <li className="flex items-center gap-2">
                <span className="text-emerald-500">✓</span>
                工作台完整界面（Sidebar 导航）
              </li>
              <li className="flex items-center gap-2">
                <span className="text-emerald-500">✓</span>
                白标 PDF 导出 + 批量内容生产
              </li>
              <li className="flex items-center gap-2">
                <span className="text-emerald-500">✓</span>
                客户 CRM + 数据面板
              </li>
            </ul>
          </div>

          {/* 费用说明 */}
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs space-y-1.5">
            <div className="font-medium text-amber-700 dark:text-amber-300 flex items-center gap-1.5">
              <Clock className="h-3.5 w-3.5" />
              费用说明
            </div>
            <div className="text-amber-700/90 dark:text-amber-300/90">
              本次申请占用 <span className="font-bold">260 算力</span>
            </div>
            <div className="text-amber-700/80 dark:text-amber-300/80 text-[11px]">
              审核通过后 24 小时有效 · 未通过则全额退还 260 算力
            </div>
          </div>

          {/* 可选说明 */}
          <div className="space-y-1.5">
            <label className="text-xs text-muted-foreground">
              简单说明申请原因（可选，帮助我们了解您的需求）
            </label>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="例：想体验批量生产功能..."
              className="w-full rounded-md border bg-background px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-ring"
              rows={2}
              maxLength={200}
            />
          </div>

          {/* 勾选同意 */}
          <div className="flex items-start gap-2">
            <Checkbox
              id="trial-agree"
              checked={agreed}
              onCheckedChange={(v) => setAgreed(v === true)}
            />
            <label htmlFor="trial-agree" className="text-xs leading-relaxed cursor-pointer">
              我已阅读并同意占用 260 算力申请试用，未通过则全额退还。
              通过后可使用工作台 24 小时，到期后自动回到客户对话版。
            </label>
          </div>
        </div>

        <DialogFooter className="gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
            取消
          </Button>
          <Button onClick={handleSubmit} disabled={!agreed || submitting}>
            {submitting ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : null}
            确认申请
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
