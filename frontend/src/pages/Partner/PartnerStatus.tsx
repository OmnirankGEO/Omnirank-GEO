/**
 * PartnerStatus — 代理申请状态查询页
 *
 * 展示:
 *   - 当前申请状态 (审核中 / 通过 / 拒绝 / 撤回)
 *   - 拒绝原因 / AI 风险 flag
 *   - 月度申请次数 + 冷静期提示
 *   - 已签协议列表
 *
 * 操作:
 *   - 撤回申请 (pending / manual_review 状态可撤)
 *   - 去申请 (无在审申请时)
 *   - 主动退代理 (代理身份)
 */

import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Clock, CheckCircle2, XCircle, Loader2, AlertTriangle, FileText } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { authFetch } from '@/lib/api';
import { useAuth } from '@/context/AuthContext';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { toast } from 'sonner';

type ApplicationStatus =
  | 'pending'
  | 'manual_review'
  | 'auto_approved'
  | 'approved'
  | 'rejected'
  | 'withdrawn';

interface ApplyStatus {
  current_agent_level: number;
  latest_application: {
    id: number;
    status: ApplicationStatus;
    rejection_reason?: string | null;
    ai_risk_flags?: Array<{ code: string; detail: string }> | null;
    created_at?: string | null;
    reviewed_at?: string | null;
  } | null;
  recent_rejected_count: number;
  monthly_apply_limit: number;
  can_apply_now: boolean;
}

interface AgreementItem {
  id: number;
  version: string;
  signed_name: string;
  signed_at: string | null;
  agreement_text_hash: string;
}

const STATUS_CONFIG: Record<ApplicationStatus, { label: string; cls: string; Icon: typeof Clock }> = {
  pending: { label: '待审核', cls: 'border-amber-500/30 bg-amber-500/5 text-amber-400', Icon: Clock },
  manual_review: { label: '人工审核中', cls: 'border-amber-500/30 bg-amber-500/5 text-amber-400', Icon: Clock },
  auto_approved: { label: '已通过（AI 自动）', cls: 'border-emerald-500/30 bg-emerald-500/5 text-emerald-400', Icon: CheckCircle2 },
  approved: { label: '已通过', cls: 'border-emerald-500/30 bg-emerald-500/5 text-emerald-400', Icon: CheckCircle2 },
  rejected: { label: '已拒绝', cls: 'border-red-500/30 bg-red-500/5 text-red-400', Icon: XCircle },
  withdrawn: { label: '已撤回', cls: 'border-muted/30 bg-muted/10 text-muted-foreground', Icon: XCircle },
};

const RISK_CODE_LABELS: Record<string, string> = {
  LOW_OCR_CONFIDENCE: 'OCR 置信度偏低',
  NAME_MISMATCH: '识别姓名与填写不一致',
  IDCARD_MISMATCH: '识别身份证号与填写不一致',
  NEW_ACCOUNT: '账户注册时间较短',
  SAME_IP_24H: '24 小时内同 IP 申请',
  SAME_DEVICE_AGENT: '同设备已有服务商账号',
  HAS_FRAUD_HISTORY: '历史有反作弊标记',
};

export default function PartnerStatus() {
  const [confirmDialog, askConfirm] = useConfirmDialog();
  const { user } = useAuth();
  const isAgent = (user?.agent_level ?? 0) >= 1;

  const [status, setStatus] = useState<ApplyStatus | null>(null);
  const [agreements, setAgreements] = useState<AgreementItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [withdrawing, setWithdrawing] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [s, a] = await Promise.all([
        authFetch('/api/partner/apply/status').then(r => (r.ok ? r.json() : null)),
        authFetch('/api/partner/agreements/mine').then(r => (r.ok ? r.json() : null)),
      ]);
      setStatus(s as ApplyStatus | null);
      setAgreements(((a?.agreements || []) as AgreementItem[]));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleWithdraw = async () => {
    if (!status?.latest_application || withdrawing) return;
    if (!(await askConfirm({ title: '确认撤回本次申请？撤回后可重新提交，但 30 天内被拒/撤回累计 3 次将进入冷静期。', danger: true }))) {
      return;
    }
    setWithdrawing(true);
    try {
      const resp = await authFetch(`/api/partner/apply/${status.latest_application.id}/withdraw`, {
        method: 'POST',
      });
      if (resp.ok) {
        await refresh();
      } else {
        const err = await resp.json().catch(() => ({}));
        toast.error((err as { detail?: string })?.detail || '撤回失败');
      }
    } finally {
      setWithdrawing(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const latest = status?.latest_application;
  const cfg = latest ? STATUS_CONFIG[latest.status] : null;

  return (
    <div className="max-w-2xl mx-auto p-6 space-y-6">
      <div>
        <h1 className="text-lg font-semibold">合作伙伴 · 申请状态</h1>
        <p className="text-xs text-muted-foreground mt-1">
          当前身份：{isAgent ? <span className="text-foreground">服务商</span> : '普通用户'}
        </p>
      </div>

      {/* 当前申请 */}
      {latest && cfg ? (
        <Card className={cfg.cls + ' border'}>
          <CardContent className="py-4 space-y-3">
            <div className="flex items-center gap-2">
              <cfg.Icon className="h-5 w-5" />
              <span className="font-medium">{cfg.label}</span>
              <span className="text-xs text-muted-foreground ml-auto">
                #{latest.id} · 提交于 {latest.created_at?.slice(0, 16)?.replace('T', ' ')}
              </span>
            </div>

            {latest.rejection_reason && (
              <div className="text-xs">
                <div className="text-muted-foreground mb-1">
                  {latest.rejection_reason.startsWith('[需补充]') ? '审核员反馈：' : '拒绝原因：'}
                </div>
                <div className="text-foreground whitespace-pre-wrap">{latest.rejection_reason}</div>
              </div>
            )}

            {latest.ai_risk_flags && latest.ai_risk_flags.length > 0 && (
              <div className="text-xs space-y-1">
                <div className="text-muted-foreground">AI 检测到以下待核验项：</div>
                {latest.ai_risk_flags.map((f, i) => (
                  <div key={i} className="flex items-start gap-1.5">
                    <AlertTriangle className="h-3 w-3 text-amber-400 mt-0.5 shrink-0" />
                    <span>
                      <span className="text-foreground">{RISK_CODE_LABELS[f.code] || f.code}</span>
                      {' · '}
                      <span className="text-muted-foreground">{f.detail}</span>
                    </span>
                  </div>
                ))}
              </div>
            )}

            {(latest.status === 'pending' || latest.status === 'manual_review') && (
              <div className="flex gap-2 pt-2 border-t border-border/30">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleWithdraw}
                  disabled={withdrawing}
                >
                  {withdrawing ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : null}
                  撤回申请
                </Button>
              </div>
            )}
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="py-4 text-sm text-muted-foreground">
            您还未提交过服务商申请。
          </CardContent>
        </Card>
      )}

      {/* 冷静期提示 */}
      {status && status.recent_rejected_count > 0 && (
        <Card className="border-amber-500/20 bg-amber-500/5">
          <CardContent className="py-3 text-xs">
            30 天内被拒 / 撤回 {status.recent_rejected_count} /{' '}
            {status.monthly_apply_limit} 次
            {status.recent_rejected_count >= status.monthly_apply_limit && (
              <span className="text-amber-400">
                {' '}· 已达上限，需等待冷静期结束才能再次申请
              </span>
            )}
          </CardContent>
        </Card>
      )}

      {/* 可以申请 */}
      {status?.can_apply_now && !isAgent && (
        <div className="flex gap-3">
          <Button asChild className="flex-1">
            <Link to="/partner/apply">提交新申请</Link>
          </Button>
          <Button asChild variant="outline">
            <Link to="/partner/about">查看权益</Link>
          </Button>
        </div>
      )}

      {/* 已签协议列表 */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <FileText className="h-4 w-4" />
            已签协议
          </CardTitle>
        </CardHeader>
        <CardContent>
          {agreements.length === 0 ? (
            <p className="text-sm text-muted-foreground">暂无签署记录</p>
          ) : (
            <div className="space-y-2">
              {agreements.map(a => (
                <div
                  key={a.id}
                  className="flex items-center gap-3 p-2 border border-border rounded-md"
                >
                  <div className="flex-1">
                    <div className="text-sm font-medium">
                      服务商申请协议 {a.version}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      签署人 {a.signed_name} · {a.signed_at?.slice(0, 16)?.replace('T', ' ')}
                    </div>
                    <div className="text-[10px] text-muted-foreground font-mono truncate">
                      hash: {a.agreement_text_hash.slice(0, 16)}…
                    </div>
                  </div>
                  <Button asChild variant="outline" size="sm">
                    <Link to={`/partner/agreement/${a.version}`}>查看</Link>
                  </Button>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
      {confirmDialog}
    </div>
  );
}
