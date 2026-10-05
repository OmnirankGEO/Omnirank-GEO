import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, Database, RefreshCw, Send, ShieldCheck } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { formatApiErrorForDisplay } from '@/lib/api';
import {
  adminApi,
  type PricingPublicationStatus,
  type PricingReadinessResult,
} from '@/lib/v35w2Api';

interface AccountCodeImpact {
  target_service_count: number;
  target_channel_count: number;
  missing_service_count: number;
  missing_channel_count: number;
}

function businessBlockerText(blocker: string): string {
  const retail = /^retail\/agent=(\d+):\s*(.*)$/.exec(blocker);
  if (retail) {
    const [, agentId, detail] = retail;
    if (/售价必须高于当前有效成本/.test(detail)) {
      return `服务商 ${agentId}：有对客算力包售价不高于当前进货成本，请调高售价或下架该包`;
    }
    if (/有效成本不可用/.test(detail)) {
      return `服务商 ${agentId}：当前进货成本无法确认，请先检查其服务关系和进货规则`;
    }
    return `服务商 ${agentId}：${detail.replace(/SKU\s+\S+\s*/g, '')}`;
  }
  if (blocker.startsWith('procurement/PLATFORM_BASE:')) {
    return `服务商进货价目表：${blocker.split(':').slice(1).join(':').trim()}`;
  }
  return blocker
    .replace(/procurement\s*\/\s*PLATFORM_BASE/g, '服务商进货价目表')
    .replace(/PRICING_DUAL_SSOT_ENABLED/g, '新版定价功能')
    .replace(/CHANNEL_PRICING_ENABLED/g, '渠道定价功能')
    .replace(/PRICING_QUOTE_REQUIRED/g, '下单报价校验');
}

export function PricingPublicationPanel() {
  const requestSequence = useRef(0);
  const [publication, setPublication] = useState<PricingPublicationStatus | null>(null);
  const [readiness, setReadiness] = useState<PricingReadinessResult | null>(null);
  const [codes, setCodes] = useState<AccountCodeImpact | null>(null);
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [preparingCodes, setPreparingCodes] = useState(false);
  const [confirmDialog, askConfirm] = useConfirmDialog();

  const loadStatus = useCallback(async () => {
    const sequence = ++requestSequence.current;
    setLoading(true);
    try {
      const [publicationResponse, readinessResponse, codeResponse] = await Promise.all([
        adminApi.pricingPublicationStatus(),
        adminApi.pricingReadiness(),
        adminApi.accountCodeDryRun(),
      ]);
      if (sequence !== requestSequence.current) return;
      setPublication(publicationResponse.data);
      setReadiness(readinessResponse.data);
      setCodes(codeResponse.data);
    } catch (error) {
      if (sequence === requestSequence.current) {
        toast.error(formatApiErrorForDisplay(error, '加载价目生效状态失败', 'admin'));
      }
    } finally {
      if (sequence === requestSequence.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    return () => {
      requestSequence.current += 1;
    };
  }, [loadStatus]);

  const dryRun = async () => {
    setLoading(true);
    try {
      const response = await adminApi.pricingPublicationDryRun();
      setPublication(response.data);
      toast.success(
        response.data.publishable
          ? `检查完成 · ${response.data.needs_publication_count} 份价目待生效`
          : `检查完成 · 还有 ${response.data.blocked_count} 项问题需要处理`,
      );
    } catch (error) {
      toast.error(formatApiErrorForDisplay(error, '检查待生效价目失败', 'admin'));
    } finally {
      setLoading(false);
    }
  };

  const prepareCodes = async () => {
    const missing = (codes?.missing_service_count ?? 0) + (codes?.missing_channel_count ?? 0);
    if (missing <= 0) {
      toast.info('需要展示给客户的编号均已准备');
      return;
    }
    if (!(await askConfirm({
      title: `为 ${missing} 个账号补齐客户可见编号？`,
      description: '只补齐客户页面需要展示的匿名编号，不会新建或改变任何业务关系。',
      confirmLabel: '确认补齐',
    }))) return;
    setPreparingCodes(true);
    try {
      const response = await adminApi.accountCodePrepare();
      toast.success(`客户可见编号已补齐 · 新增 ${response.data.created_count} 个`);
      await loadStatus();
    } catch (error) {
      toast.error(formatApiErrorForDisplay(error, '补齐客户可见编号失败', 'admin'));
    } finally {
      setPreparingCodes(false);
    }
  };

  const publish = async () => {
    const cleanReason = reason.trim();
    if (!publication?.publishable) {
      toast.error('还有问题尚未处理，新价目暂不能生效');
      return;
    }
    if (!cleanReason) {
      toast.error('请填写本次调整原因');
      return;
    }
    if (!(await askConfirm({
      title: `让 ${publication.needs_publication_count} 份新价目生效？`,
      description: '只影响之后创建的新报价；已有报价、待支付订单和历史订单都保持原价。',
      confirmLabel: '确认生效',
    }))) return;
    setPublishing(true);
    try {
      const response = await adminApi.pricingPublicationPublish({
        reason: cleanReason,
        expected_epoch: publication.review_contract.config_epoch,
        reviewed_target_mode: publication.review_contract.target_mode,
        reviewed_service_user_ids: publication.review_contract.service_user_ids,
        reviewed_scopes: publication.review_contract.scopes,
      });
      toast.success(
        `新价目已生效 ${response.data.published_count} 份 · 另有 ${response.data.unchanged_count} 份无需更新`,
      );
      setReason('');
      await loadStatus();
    } catch (error) {
      toast.error(formatApiErrorForDisplay(error, '新价目未能生效 · 请重新检查', 'admin'));
    } finally {
      setPublishing(false);
    }
  };

  const blockers = publication?.blockers ?? [];
  const missingCodes = (codes?.missing_service_count ?? 0) + (codes?.missing_channel_count ?? 0);

  return (
    <Card className="border-primary/25">
      {confirmDialog}
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-base">
              <Database className="h-4 w-4" /> 让新价目生效
            </CardTitle>
            <p className="mt-1 text-sm text-muted-foreground">
              保存只是保留草稿；在这里确认生效后，新报价才会使用新价目。已有报价和订单不会改变。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={publication?.ready ? 'default' : 'secondary'}>
              {publication?.ready ? '全部已生效' : `${publication?.needs_publication_count ?? 0} 份价目待生效`}
            </Badge>
            <Badge variant={readiness?.ready ? 'default' : 'outline'}>
              {readiness?.ready ? '上线检查通过' : `上线前待处理 ${readiness?.blocker_count ?? '—'} 项`}
            </Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 text-sm md:grid-cols-4">
          <Metric label="当前配置版本" value={publication ? String(publication.config_epoch) : '—'} />
          <Metric label="需要同步的服务商" value={publication ? String(publication.target_service_count) : '—'} />
          <Metric label="尚未生效的价目" value={publication ? String(publication.needs_publication_count) : '—'} />
          <Metric label="待补客户可见编号" value={codes ? String(missingCodes) : '—'} />
        </div>

        {blockers.length > 0 && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-800 dark:text-amber-200">
            <div className="mb-1 flex items-center gap-2 font-medium">
              <AlertTriangle className="h-4 w-4" /> 以下问题处理后才能生效
            </div>
            <ul className="list-disc space-y-1 pl-5">
              {blockers.slice(0, 6).map((blocker) => <li key={blocker}>{businessBlockerText(blocker)}</li>)}
            </ul>
            <details className="mt-3 text-xs text-muted-foreground">
              <summary className="cursor-pointer select-none">查看技术详情（排障时使用）</summary>
              <ul className="mt-2 list-disc space-y-1 pl-5 font-mono">
                {blockers.slice(0, 6).map((blocker) => <li key={`technical-${blocker}`}>{blocker}</li>)}
              </ul>
            </details>
          </div>
        )}

        {publication?.publishable && publication.needs_publication_count === 0 && (
          <div className="flex items-center gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-700 dark:text-emerald-300">
            <CheckCircle2 className="h-4 w-4" /> 管理设置已全部同步到客户和服务商使用的价目。
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={dryRun} disabled={loading || publishing}>
            <RefreshCw className={`mr-1 h-4 w-4 ${loading ? 'animate-spin' : ''}`} /> 检查待生效内容
          </Button>
          <Button variant="outline" onClick={prepareCodes} disabled={preparingCodes || missingCodes === 0}>
            <ShieldCheck className="mr-1 h-4 w-4" />
            {preparingCodes ? '准备中…' : '补齐客户可见编号'}
          </Button>
        </div>

        <div className="flex flex-col gap-2 sm:flex-row">
          <Input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="调整原因（必填，例如：更新 7 月服务商与客户价目）"
            maxLength={500}
            aria-label="价目生效原因"
          />
          <Button
            onClick={publish}
            disabled={publishing || loading || !publication?.publishable || !reason.trim()}
            className="sm:min-w-32"
          >
            <Send className="mr-1 h-4 w-4" /> {publishing ? '生效中…' : '让全部新价目生效'}
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          本操作只更新价目，不会自动开启或关闭线上定价功能；功能状态仍由上线检查统一控制。
        </p>
      </CardContent>
    </Card>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border bg-muted/20 px-3 py-2">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-0.5 font-semibold tabular-nums">{value}</div>
    </div>
  );
}
