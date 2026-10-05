/**
 * PartnerApplicationsReview — Admin 代理申请审核台
 *
 * URL: /admin/partner-applications
 *
 * - 列表: 按 status 筛选 (默认 manual_review) + 分页
 * - 详情: Dialog 弹窗展示, 含 3 张身份证临时 URL + AI 风险 flag
 * - 操作: 通过 / 拒绝 / 要求补材料
 * - 权限: 仅 is_admin
 */

import { useCallback, useEffect, useState } from 'react';
import {
  Clock, CheckCircle2, XCircle, AlertTriangle, Loader2,
  Eye, ImageOff, RefreshCw,
} from 'lucide-react';
import { authFetch } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';

type AppStatus =
  | 'pending' | 'manual_review' | 'auto_approved'
  | 'approved' | 'rejected' | 'withdrawn';

interface ApplicationListItem {
  id: number;
  user_id: number;
  username?: string | null;
  display_name?: string | null;
  real_name: string;
  id_card_no_mask: string;
  promotion_scenes?: string[] | null;
  expected_monthly_customers?: string | null;
  remark?: string | null;
  ocr_confidence?: number | null;
  ai_risk_flags?: Array<{ code: string; detail: string }> | null;
  status: AppStatus;
  rejection_reason?: string | null;
  reviewed_by?: number | null;
  reviewed_at?: string | null;
  ip_address?: string | null;
  device_fingerprint?: string | null;
  created_at?: string | null;
}

interface ApplicationDetail extends ApplicationListItem {
  phone?: string | null;
  user_created_at?: string | null;
  ocr_result?: unknown;
  user_agent?: string | null;
}

const STATUS_BADGE: Record<AppStatus, { label: string; cls: string }> = {
  pending: { label: '待审核', cls: 'bg-amber-500/15 text-amber-400 border-amber-500/20' },
  manual_review: { label: '人工审核中', cls: 'bg-amber-500/15 text-amber-400 border-amber-500/20' },
  auto_approved: { label: 'AI 通过', cls: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/20' },
  approved: { label: '已通过', cls: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/20' },
  rejected: { label: '已拒绝', cls: 'bg-red-500/15 text-red-400 border-red-500/20' },
  withdrawn: { label: '已撤回', cls: 'bg-muted/30 text-muted-foreground border-border' },
};

const RISK_LABEL: Record<string, string> = {
  LOW_OCR_CONFIDENCE: 'OCR 置信度偏低',
  NAME_MISMATCH: '姓名不一致',
  IDCARD_MISMATCH: '身份证号不一致',
  NEW_ACCOUNT: '新账户 (<7 天)',
  SAME_IP_24H: '24h 同 IP 申请',
  SAME_DEVICE_AGENT: '同设备已有服务商',
  HAS_FRAUD_HISTORY: '历史 fraud 标记',
};

const STATUS_FILTERS: AppStatus[] = ['manual_review', 'pending', 'approved', 'rejected', 'withdrawn'];

export default function PartnerApplicationsReview() {
  const [status, setStatus] = useState<AppStatus>('manual_review');
  const [items, setItems] = useState<ApplicationListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [offset, setOffset] = useState(0);
  const [limit] = useState(50);

  const [detailOpen, setDetailOpen] = useState(false);
  const [detail, setDetail] = useState<ApplicationDetail | null>(null);
  const [idCardUrls, setIdCardUrls] = useState<Record<string, string | null>>({});
  const [detailLoading, setDetailLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await authFetch(
        `/api/admin/partner/applications?status=${status}&limit=${limit}&offset=${offset}`,
      );
      if (!resp.ok) return;
      const data = await resp.json();
      setItems((data?.applications || []) as ApplicationListItem[]);
      setTotal(data?.total || 0);
    } finally {
      setLoading(false);
    }
  }, [status, limit, offset]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const openDetail = async (appId: number) => {
    setDetailOpen(true);
    setDetail(null);
    setIdCardUrls({});
    setDetailLoading(true);
    try {
      const resp = await authFetch(`/api/admin/partner/applications/${appId}`);
      if (!resp.ok) return;
      const data = await resp.json();
      setDetail(data.application as ApplicationDetail);
      setIdCardUrls((data.id_card_urls || {}) as Record<string, string | null>);
    } finally {
      setDetailLoading(false);
    }
  };

  return (
    <div className="max-w-6xl mx-auto p-6 space-y-6">
      <div className="flex items-center gap-3">
        <div className="flex-1">
          <h1 className="text-lg font-semibold">服务商申请审核台</h1>
          <p className="text-xs text-muted-foreground mt-1">v1.1 审核制 · 共 {total} 条</p>
        </div>
        <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
          <RefreshCw className={['h-4 w-4 mr-1', loading ? 'animate-spin' : ''].join(' ')} />
          刷新
        </Button>
      </div>

      {/* 状态筛选 */}
      <div className="flex flex-wrap gap-2">
        {STATUS_FILTERS.map(s => (
          <button
            key={s}
            onClick={() => {
              setOffset(0);
              setStatus(s);
            }}
            className={[
              'px-3 py-1.5 rounded-full text-xs border transition-colors',
              s === status
                ? 'border-foreground bg-foreground/5 text-foreground font-medium'
                : 'border-border text-muted-foreground hover:text-foreground',
            ].join(' ')}
          >
            {STATUS_BADGE[s].label}
          </button>
        ))}
      </div>

      {/* 列表 */}
      <Card>
        <CardContent className="p-0">
          <div className="divide-y divide-border">
            {loading && items.length === 0 && (
              <div className="p-8 text-center">
                <Loader2 className="h-6 w-6 animate-spin mx-auto text-muted-foreground" />
              </div>
            )}

            {!loading && items.length === 0 && (
              <div className="p-8 text-center text-sm text-muted-foreground">
                暂无记录
              </div>
            )}

            {items.map(it => (
              <div
                key={it.id}
                className="p-3 hover:bg-muted/30 cursor-pointer flex items-start gap-3"
                onClick={() => openDetail(it.id)}
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-medium text-sm text-foreground">
                      {it.real_name}
                    </span>
                    <span className="text-xs text-muted-foreground font-mono">
                      {it.id_card_no_mask}
                    </span>
                    <Badge variant="outline" className={STATUS_BADGE[it.status].cls + ' text-[10px] px-1.5 py-0'}>
                      {STATUS_BADGE[it.status].label}
                    </Badge>
                    {it.ocr_confidence !== null && it.ocr_confidence !== undefined && (
                      <span
                        className={[
                          'text-[10px] px-1.5 py-0.5 rounded-full',
                          (it.ocr_confidence || 0) >= 0.9
                            ? 'bg-emerald-500/10 text-emerald-400'
                            : 'bg-amber-500/10 text-amber-400',
                        ].join(' ')}
                      >
                        OCR {(it.ocr_confidence * 100).toFixed(0)}%
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-muted-foreground mt-1">
                    {it.username || `user_id=${it.user_id}`}
                    {' · '}
                    {it.expected_monthly_customers && `月客户 ${it.expected_monthly_customers} · `}
                    {it.created_at?.slice(0, 16)?.replace('T', ' ')}
                  </div>

                  {it.ai_risk_flags && it.ai_risk_flags.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-2">
                      {it.ai_risk_flags.map((f, i) => (
                        <span
                          key={i}
                          className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20"
                        >
                          {RISK_LABEL[f.code] || f.code}
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                <Eye className="h-4 w-4 text-muted-foreground mt-1" />
              </div>
            ))}
          </div>

          {total > limit && (
            <div className="flex items-center justify-between p-3 border-t border-border text-xs">
              <span className="text-muted-foreground">
                {offset + 1} - {Math.min(offset + limit, total)} / {total}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - limit))}
                >
                  上一页
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset + limit >= total}
                  onClick={() => setOffset(offset + limit)}
                >
                  下一页
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* 详情 Dialog */}
      <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
        <DialogContent className="max-w-3xl max-h-[90vh] overflow-auto">
          <DialogHeader>
            <DialogTitle>服务商申请详情 {detail && `#${detail.id}`}</DialogTitle>
          </DialogHeader>

          {detailLoading && (
            <div className="py-8 text-center">
              <Loader2 className="h-6 w-6 animate-spin mx-auto text-muted-foreground" />
            </div>
          )}

          {detail && !detailLoading && (
            <DetailPanel
              detail={detail}
              idCardUrls={idCardUrls}
              onDone={async () => {
                setDetailOpen(false);
                await refresh();
              }}
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ========== 详情面板 ==========

function DetailPanel({
  detail,
  idCardUrls,
  onDone,
}: {
  detail: ApplicationDetail;
  idCardUrls: Record<string, string | null>;
  onDone: () => void;
}) {
  const [action, setAction] = useState<'none' | 'approve' | 'reject' | 'more'>('none');
  const [reason, setReason] = useState('');
  const [notes, setNotes] = useState('');
  const [message, setMessage] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canOperate = detail.status === 'pending' || detail.status === 'manual_review';

  const submit = async () => {
    if (!canOperate || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      let url = '';
      let body: Record<string, unknown> = {};
      if (action === 'approve') {
        url = `/api/admin/partner/applications/${detail.id}/approve`;
        body = { notes: notes || null };
      } else if (action === 'reject') {
        if (!reason.trim()) {
          setError('请填写拒绝原因');
          return;
        }
        url = `/api/admin/partner/applications/${detail.id}/reject`;
        body = { reason: reason.trim() };
      } else if (action === 'more') {
        if (!message.trim()) {
          setError('请填写要求补充的内容');
          return;
        }
        url = `/api/admin/partner/applications/${detail.id}/request-more`;
        body = { message: message.trim() };
      } else {
        return;
      }

      const resp = await authFetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        setError((err as { detail?: string })?.detail || '操作失败');
        return;
      }
      onDone();
    } catch (e) {
      setError((e as Error)?.message || '网络错误');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-4">
      {/* 身份证图片 */}
      <div>
        <div className="text-xs font-medium text-muted-foreground mb-2">
          身份证照片（临时签名 URL · 15 分钟有效）
        </div>
        <div className="grid grid-cols-3 gap-2">
          {(['front', 'back', 'selfie'] as const).map(side => (
            <div key={side} className="aspect-[3/2] bg-muted/30 border border-border rounded overflow-hidden flex items-center justify-center">
              {idCardUrls[side] ? (
                <img
                  src={idCardUrls[side]!}
                  alt={side}
                  className="w-full h-full object-contain"
                />
              ) : (
                <div className="flex flex-col items-center text-muted-foreground text-xs">
                  <ImageOff className="h-5 w-5 mb-1" />
                  {side === 'selfie' ? '未上传' : '无'}
                </div>
              )}
            </div>
          ))}
        </div>
        <div className="grid grid-cols-3 gap-2 text-[10px] text-center text-muted-foreground mt-1">
          <div>人像面</div>
          <div>国徽面</div>
          <div>手持</div>
        </div>
      </div>

      {/* 申请信息 */}
      <div className="grid grid-cols-2 gap-3 text-xs">
        <Info label="姓名" value={detail.real_name} />
        <Info label="身份证号" value={<span className="font-mono">{detail.id_card_no_mask}</span>} />
        <Info label="账号" value={detail.username || `user_id=${detail.user_id}`} />
        <Info label="手机" value={detail.phone || '—'} />
        <Info label="账户注册" value={detail.user_created_at?.slice(0, 10) || '—'} />
        <Info label="申请时间" value={detail.created_at?.slice(0, 16)?.replace('T', ' ') || '—'} />
        <Info label="预期月客户" value={detail.expected_monthly_customers || '—'} />
        <Info
          label="OCR 置信度"
          value={
            detail.ocr_confidence !== null && detail.ocr_confidence !== undefined
              ? `${((detail.ocr_confidence || 0) * 100).toFixed(0)}%`
              : '—'
          }
        />
        <Info label="IP" value={<span className="font-mono">{detail.ip_address || '—'}</span>} />
        <Info
          label="设备指纹"
          value={
            <span className="font-mono text-[10px]">
              {detail.device_fingerprint ? detail.device_fingerprint.slice(0, 16) + '…' : '—'}
            </span>
          }
        />
      </div>

      {detail.promotion_scenes && detail.promotion_scenes.length > 0 && (
        <div>
          <div className="text-xs font-medium text-muted-foreground mb-1">推广场景</div>
          <div className="flex flex-wrap gap-1.5">
            {detail.promotion_scenes.map(s => (
              <span key={s} className="text-xs px-2 py-0.5 rounded-full bg-foreground/5 border border-border">
                {s}
              </span>
            ))}
          </div>
        </div>
      )}

      {detail.remark && (
        <div>
          <div className="text-xs font-medium text-muted-foreground mb-1">备注</div>
          <div className="text-sm text-foreground bg-muted/20 p-2 rounded border border-border">
            {detail.remark}
          </div>
        </div>
      )}

      {/* AI 风险 */}
      {detail.ai_risk_flags && detail.ai_risk_flags.length > 0 && (
        <div>
          <div className="text-xs font-medium text-muted-foreground mb-2">AI 检测</div>
          <div className="space-y-1">
            {detail.ai_risk_flags.map((f, i) => (
              <div key={i} className="flex items-start gap-2 text-xs">
                <AlertTriangle className="h-3.5 w-3.5 text-amber-400 mt-0.5 shrink-0" />
                <div>
                  <span className="font-medium text-foreground">
                    {RISK_LABEL[f.code] || f.code}
                  </span>
                  <span className="text-muted-foreground"> · {f.detail}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 当前 status 摘要 */}
      <div
        className={[
          'flex items-center gap-2 p-3 rounded-lg border text-sm',
          STATUS_BADGE[detail.status].cls,
        ].join(' ')}
      >
        <CurrentStatusIcon status={detail.status} />
        <span>{STATUS_BADGE[detail.status].label}</span>
        {detail.reviewed_at && (
          <span className="text-xs text-muted-foreground ml-auto">
            审核于 {detail.reviewed_at.slice(0, 16).replace('T', ' ')}
          </span>
        )}
      </div>

      {detail.rejection_reason && (
        <div className="text-xs p-2 border border-border rounded">
          <div className="text-muted-foreground mb-1">备注 / 拒绝原因</div>
          <div className="text-foreground whitespace-pre-wrap">{detail.rejection_reason}</div>
        </div>
      )}

      {/* 操作区 */}
      {canOperate && (
        <div className="border-t border-border pt-4 space-y-3">
          <div className="text-sm font-medium">操作</div>

          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant={action === 'approve' ? 'default' : 'outline'}
              onClick={() => setAction('approve')}
            >
              <CheckCircle2 className="h-4 w-4 mr-1" />
              通过
            </Button>
            <Button
              size="sm"
              variant={action === 'reject' ? 'default' : 'outline'}
              onClick={() => setAction('reject')}
            >
              <XCircle className="h-4 w-4 mr-1" />
              拒绝
            </Button>
            <Button
              size="sm"
              variant={action === 'more' ? 'default' : 'outline'}
              onClick={() => setAction('more')}
            >
              <Clock className="h-4 w-4 mr-1" />
              要求补材料
            </Button>
          </div>

          {action === 'approve' && (
            <div className="space-y-1">
              <Label className="text-xs">审核备注（可选，进审计日志）</Label>
              <Textarea
                value={notes}
                onChange={e => setNotes(e.target.value)}
                placeholder="内部备注"
                rows={2}
                maxLength={500}
              />
            </div>
          )}
          {action === 'reject' && (
            <div className="space-y-1">
              <Label className="text-xs">拒绝原因 *</Label>
              <Textarea
                value={reason}
                onChange={e => setReason(e.target.value)}
                placeholder="如：身份证照片模糊、信息不一致等"
                rows={2}
                maxLength={500}
              />
            </div>
          )}
          {action === 'more' && (
            <div className="space-y-1">
              <Label className="text-xs">要求补充的内容 *</Label>
              <Input
                value={message}
                onChange={e => setMessage(e.target.value)}
                placeholder="如：请补上传手持照片 / 请重新拍摄清晰的人像面"
                maxLength={500}
              />
            </div>
          )}

          {error && (
            <div className="text-xs text-red-400 p-2 border border-red-500/30 bg-red-500/5 rounded">
              {error}
            </div>
          )}

          {action !== 'none' && (
            <div className="flex gap-2">
              <Button onClick={submit} disabled={submitting}>
                {submitting ? (
                  <Loader2 className="h-4 w-4 animate-spin mr-1" />
                ) : null}
                确认
                {action === 'approve' && '通过'}
                {action === 'reject' && '拒绝'}
                {action === 'more' && '要求补材料'}
              </Button>
              <Button variant="outline" onClick={() => setAction('none')}>
                取消
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Info({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-muted-foreground text-[10px] uppercase">{label}</div>
      <div className="mt-0.5 text-foreground">{value}</div>
    </div>
  );
}

function CurrentStatusIcon({ status }: { status: AppStatus }) {
  switch (status) {
    case 'pending':
    case 'manual_review':
      return <Clock className="h-4 w-4" />;
    case 'approved':
    case 'auto_approved':
      return <CheckCircle2 className="h-4 w-4" />;
    case 'rejected':
      return <XCircle className="h-4 w-4" />;
    case 'withdrawn':
      return <XCircle className="h-4 w-4" />;
  }
}
