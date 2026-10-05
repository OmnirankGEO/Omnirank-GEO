/**
 * 提现实名认证弹窗
 * 老代理首次提现前需完成 KYC
 */
import { useState } from 'react';
import { Loader2 } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { extractErrorMessage } from '@/lib/utils';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { toast } from 'sonner';

interface Props {
  open: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

export default function WithdrawalKycDialog({ open, onClose, onSuccess }: Props) {
  const [realName, setRealName] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const canSubmit = realName.trim().length >= 2 && !submitting;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      const res = await authFetch('/api/wallet/withdrawal-kyc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ real_name: realName.trim() }),
      });
      if (res.ok) {
        toast.success('实名认证成功');
        onSuccess();
        onClose();
        setRealName('');
      } else {
        const err = await res.json().catch(() => null);
        toast.error(extractErrorMessage(err, '认证失败，请稍后重试'));
      }
    } catch {
      toast.error('网络错误，请稍后重试');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>实名认证</DialogTitle>
          <DialogDescription>
            提现前需完成实名认证，请输入您的真实姓名
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 pt-2">
          <div className="space-y-2">
            <Label htmlFor="kyc-real-name">真实姓名</Label>
            <Input
              id="kyc-real-name"
              value={realName}
              onChange={(e) => setRealName(e.target.value)}
              placeholder="请输入身份证上的姓名"
              maxLength={20}
              autoFocus
            />
            {realName.length > 0 && realName.trim().length < 2 && (
              <p className="text-xs text-red-400">姓名至少 2 个字</p>
            )}
          </div>

          <Button
            className="w-full bg-foreground text-background hover:bg-foreground/90"
            size="lg"
            disabled={!canSubmit}
            onClick={handleSubmit}
          >
            {submitting ? (
              <>
                <Loader2 className="size-4 animate-spin mr-2" />
                提交中...
              </>
            ) : (
              '提交认证'
            )}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
