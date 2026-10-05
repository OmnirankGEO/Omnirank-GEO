/** Canonical subscription agreement confirmation; evidence is persisted server-side. */
import { useState } from 'react';
import { Check, ExternalLink, FileText, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { authFetch } from '@/lib/api';
import { USER_TERMS_URL, USER_TERMS_VERSION } from '@/lib/legalAgreements';
import { cn } from '@/lib/utils';

interface Props {
  onSigned?: (version: string) => void;
  showHeader?: boolean;
}

function detailText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object' && 'message' in value) {
    const message = (value as { message?: unknown }).message;
    if (typeof message === 'string') return message;
  }
  return '协议确认失败，请重试';
}

export default function SubscriptionSigningPage({ onSigned, showHeader = true }: Props) {
  const [agreed, setAgreed] = useState(false);
  const [signing, setSigning] = useState(false);

  const handleSign = async () => {
    if (!agreed) {
      toast.error('请先阅读并勾选当前《用户服务协议》');
      return;
    }
    setSigning(true);
    try {
      const response = await authFetch('/api/auth/legal-agreements/accept', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          terms_accepted: true,
          terms_version: USER_TERMS_VERSION,
          surface: 'subscription-autorenew',
        }),
      });
      const payload = await response.json();
      if (!response.ok || !payload?.acceptance?.acceptance_id) {
        throw new Error(detailText(payload?.detail));
      }
      toast.success(`已记录《用户服务协议》${USER_TERMS_VERSION} 确认凭证`);
      if (onSigned) onSigned(USER_TERMS_VERSION);
      else window.location.href = '/pricing-plans';
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '协议确认失败，请重试');
    } finally {
      setSigning(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl p-4 md:p-8">
      {showHeader && (
        <div className="mb-6">
          <h1 className="text-2xl font-semibold tracking-tight">订阅购买前协议确认</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            本页记录版本、正文哈希、时间、IP 与浏览器信息；本地缓存不作为签约证据。
          </p>
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <FileText className="h-4 w-4" />
            用户服务协议 {USER_TERMS_VERSION}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 text-sm leading-relaxed text-foreground/90">
          <ul className="list-disc space-y-2 pl-5">
            <li>关闭自动续费与申请本期退款是两个独立动作。</li>
            <li>法定退款、重复扣款、未到账、未交付或系统故障由平台核验证据并执行，责任服务方不能拒绝。</li>
            <li>其他协商退款由订单所示的直属责任服务方审核；退款不自动穿透到其上游订单。</li>
            <li>退款按原支付路径和实际履行证据处理，普通消费者不固定扣除 5%。</li>
            <li>数字服务已开始履行或已消费的部分按法律规定和可审计使用记录判断。</li>
          </ul>
          <a
            href={USER_TERMS_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-primary underline-offset-4 hover:underline"
          >
            阅读当前协议完整正文 <ExternalLink className="h-3.5 w-3.5" />
          </a>
        </CardContent>
      </Card>

      <div className="mt-6 space-y-3">
        <label className="flex cursor-pointer items-start gap-2.5 rounded-md border border-border/40 bg-card/50 p-3">
          <input
            type="checkbox"
            checked={agreed}
            onChange={(event) => setAgreed(event.target.checked)}
            className="mt-0.5 h-4 w-4"
          />
          <span className="text-sm leading-relaxed">
            我已阅读并同意《用户服务协议》{USER_TERMS_VERSION}，理解订阅取消、退款申请和证据核验是不同步骤。
          </span>
        </label>

        <Button
          size="lg"
          className={cn('w-full', !agreed && 'opacity-60')}
          disabled={!agreed || signing}
          onClick={handleSign}
        >
          {signing ? <Loader2 className="h-4 w-4 animate-spin" /> : (
            <><Check className="mr-2 h-4 w-4" />确认协议并继续</>
          )}
        </Button>
        <p className="text-center text-xs text-muted-foreground">
          具体购买仍需在结算页再次确认，并生成仅限该笔购买使用的一次性凭证。
        </p>
      </div>
    </div>
  );
}
