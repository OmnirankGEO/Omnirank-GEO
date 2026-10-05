/**
 * WithdrawalReviewPage -- Admin 提现审核台
 *
 * URL: /admin/withdrawals
 *
 * - 顶部统计: 待审核数 + 本月已打款总额
 * - 状态筛选 tab: 全部 / 待审核 / 已批准 / 已拒绝 / 已打款
 * - 列表: 申请人 / 金额 / 手续费 / 到账 / 银行卡 / 状态 / 时间 / 操作
 * - 操作: 批准 / 拒绝(需原因) / 确认已打款(需银行回单号)
 */

import { Fragment, useCallback, useEffect, useState } from 'react';
import {
  Loader2, RefreshCw, CheckCircle2, XCircle, Banknote, Clock, ChevronDown, User, CreditCard, History,
} from 'lucide-react';
import { authFetch } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/ui/dialog';

// ---------- types ----------

type WithdrawalStatus = 'pending' | 'approved' | 'rejected' | 'paid';

interface WithdrawalItem {
  id: number;
  user_id: number;
  username: string;
  amount_yuan: string;
  fee_yuan: string;
  actual_yuan: string;
  bank_name: string;
  card_number_mask: string;
  status: WithdrawalStatus;
  reject_reason?: string | null;
  external_tx_ref?: string | null;
  created_at: string;
}

interface WithdrawalStats {
  pending_count: number;
  month_paid_total: string;
}

interface ListResponse {
  items: WithdrawalItem[];
  total: number;
  stats: WithdrawalStats;
}

// ---------- constants ----------

const STATUS_BADGE: Record<WithdrawalStatus, { label: string; cls: string }> = {
  pending:  { label: '待审核', cls: 'bg-amber-500/15 text-amber-400 border-amber-500/20' },
  approved: { label: '已批准', cls: 'bg-blue-500/15 text-blue-400 border-blue-500/20' },
  rejected: { label: '已拒绝', cls: 'bg-red-500/15 text-red-400 border-red-500/20' },
  paid:     { label: '已打款', cls: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/20' },
};

const STATUS_TABS: Array<{ key: WithdrawalStatus | 'all'; label: string }> = [
  { key: 'all',      label: '全部' },
  { key: 'pending',  label: '待审核' },
  { key: 'approved', label: '已批准' },
  { key: 'rejected', label: '已拒绝' },
  { key: 'paid',     label: '已打款' },
];

// ---------- helpers ----------

function fmtMoney(v: string | number): string {
  const n = typeof v === 'string' ? parseFloat(v) : v;
  if (isNaN(n)) return '0.00';
  return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtTime(iso: string): string {
  return iso?.slice(0, 16)?.replace('T', ' ') || '--';
}

// ---------- component ----------

export default function WithdrawalReviewPage() {
  const [tab, setTab] = useState<WithdrawalStatus | 'all'>('pending');
  const [items, setItems] = useState<WithdrawalItem[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<WithdrawalStats>({ pending_count: 0, month_paid_total: '0' });
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const limit = 20;

  // dialogs
  const [rejectDialog, setRejectDialog] = useState<WithdrawalItem | null>(null);
  const [rejectReason, setRejectReason] = useState('');
  const [paidDialog, setPaidDialog] = useState<WithdrawalItem | null>(null);
  const [paidRef, setPaidRef] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [detailData, setDetailData] = useState<any>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ page: String(page), limit: String(limit) });
      if (tab !== 'all') params.set('status', tab);
      const resp = await authFetch(`/api/admin/withdrawals?${params}`);
      if (!resp.ok) return;
      const data: ListResponse = await resp.json();
      setItems(data.items || []);
      setTotal(data.total || 0);
      if (data.stats) setStats(data.stats);
    } finally {
      setLoading(false);
    }
  }, [tab, page]);

  useEffect(() => { refresh(); }, [refresh]);

  // ---------- actions ----------

  const doAction = async (url: string, body?: Record<string, unknown>) => {
    setSubmitting(true);
    setActionError(null);
    try {
      const resp = await authFetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        setActionError((err as { detail?: string })?.detail || '操作失败');
        return false;
      }
      return true;
    } catch (e) {
      setActionError((e as Error)?.message || '网络错误');
      return false;
    } finally {
      setSubmitting(false);
    }
  };

  const handleApprove = async (item: WithdrawalItem) => {
    const ok = await doAction(`/api/admin/withdrawals/${item.id}/approve`);
    if (ok) refresh();
  };

  const handleRejectConfirm = async () => {
    if (!rejectDialog) return;
    if (rejectReason.trim().length < 2) {
      setActionError('拒绝原因至少 2 个字');
      return;
    }
    const ok = await doAction(`/api/admin/withdrawals/${rejectDialog.id}/reject`, {
      reason: rejectReason.trim(),
    });
    if (ok) {
      setRejectDialog(null);
      setRejectReason('');
      refresh();
    }
  };

  const handlePaidConfirm = async () => {
    if (!paidDialog) return;
    if (!paidRef.trim()) {
      setActionError('请填写银行回单号');
      return;
    }
    const ok = await doAction(`/api/admin/withdrawals/${paidDialog.id}/paid`, {
      external_tx_ref: paidRef.trim(),
    });
    if (ok) {
      setPaidDialog(null);
      setPaidRef('');
      refresh();
    }
  };

  const toggleDetail = async (id: number) => {
    if (expandedId === id) {
      setExpandedId(null);
      setDetailData(null);
      return;
    }
    setExpandedId(id);
    setDetailLoading(true);
    try {
      const resp = await authFetch(`/api/admin/withdrawals/${id}/detail`);
      if (resp.ok) {
        setDetailData(await resp.json());
      }
    } finally {
      setDetailLoading(false);
    }
  };

  const totalPages = Math.ceil(total / limit);

  return (
    <div className="max-w-6xl mx-auto p-6 space-y-6">
      {/* header */}
      <div className="flex items-center gap-3">
        <div className="flex-1">
          <h1 className="text-lg font-semibold">提现审核台</h1>
          <p className="text-xs text-muted-foreground mt-1">共 {total} 条</p>
        </div>
        <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
          <RefreshCw className={['h-4 w-4 mr-1', loading ? 'animate-spin' : ''].join(' ')} />
          刷新
        </Button>
      </div>

      {/* stats cards */}
      <div className="grid grid-cols-2 gap-4">
        <Card>
          <CardContent className="p-4 flex items-center gap-3">
            <div className="h-10 w-10 rounded-lg bg-amber-500/10 flex items-center justify-center">
              <Clock className="h-5 w-5 text-amber-400" />
            </div>
            <div>
              <div className="text-xs text-muted-foreground">待审核</div>
              <div className="text-xl font-semibold text-amber-400">{stats.pending_count}</div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 flex items-center gap-3">
            <div className="h-10 w-10 rounded-lg bg-emerald-500/10 flex items-center justify-center">
              <Banknote className="h-5 w-5 text-emerald-400" />
            </div>
            <div>
              <div className="text-xs text-muted-foreground">本月已打款</div>
              <div className="text-xl font-semibold text-emerald-400">
                ¥{fmtMoney(stats.month_paid_total)}
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* status tabs */}
      <div className="flex flex-wrap gap-2">
        {STATUS_TABS.map(t => (
          <button
            key={t.key}
            onClick={() => { setPage(1); setTab(t.key); }}
            className={[
              'px-3 py-1.5 rounded-full text-xs border transition-colors',
              t.key === tab
                ? 'border-foreground bg-foreground/5 text-foreground font-medium'
                : 'border-border text-muted-foreground hover:text-foreground',
            ].join(' ')}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* table */}
      <Card>
        <CardContent className="p-0 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-xs text-muted-foreground">
                <th className="px-3 py-2 text-left font-medium">申请人</th>
                <th className="px-3 py-2 text-right font-medium">金额</th>
                <th className="px-3 py-2 text-right font-medium">手续费</th>
                <th className="px-3 py-2 text-right font-medium">到账</th>
                <th className="px-3 py-2 text-left font-medium">银行卡</th>
                <th className="px-3 py-2 text-center font-medium">状态</th>
                <th className="px-3 py-2 text-left font-medium">申请时间</th>
                <th className="px-3 py-2 text-left font-medium">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {loading && items.length === 0 && (
                <tr>
                  <td colSpan={8} className="p-8 text-center">
                    <Loader2 className="h-6 w-6 animate-spin mx-auto text-muted-foreground" />
                  </td>
                </tr>
              )}

              {!loading && items.length === 0 && (
                <tr>
                  <td colSpan={8} className="p-8 text-center text-muted-foreground">
                    暂无记录
                  </td>
                </tr>
              )}

              {items.map(it => (
                <Fragment key={it.id}>
                <tr
                  className="hover:bg-muted/30 cursor-pointer"
                  onClick={() => toggleDetail(it.id)}
                >
                  <td className="px-3 py-2 text-foreground">
                    <div className="flex items-center gap-1.5">
                      <ChevronDown className={`size-3.5 text-muted-foreground transition-transform ${expandedId === it.id ? 'rotate-180' : ''}`} />
                      {it.username}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-right font-mono">¥{fmtMoney(it.amount_yuan)}</td>
                  <td className="px-3 py-2 text-right font-mono text-muted-foreground">
                    ¥{fmtMoney(it.fee_yuan)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono font-medium">
                    ¥{fmtMoney(it.actual_yuan)}
                  </td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {it.bank_name} {it.card_number_mask}
                  </td>
                  <td className="px-3 py-2 text-center">
                    <Badge
                      variant="outline"
                      className={STATUS_BADGE[it.status].cls + ' text-[10px] px-1.5 py-0'}
                    >
                      {STATUS_BADGE[it.status].label}
                    </Badge>
                  </td>
                  <td className="px-3 py-2 text-xs text-muted-foreground whitespace-nowrap">
                    {fmtTime(it.created_at)}
                  </td>
                  <td className="px-3 py-2" onClick={e => e.stopPropagation()}>
                    <ActionCell
                      item={it}
                      submitting={submitting}
                      onApprove={() => handleApprove(it)}
                      onReject={() => {
                        setActionError(null);
                        setRejectReason('');
                        setRejectDialog(it);
                      }}
                      onPaid={() => {
                        setActionError(null);
                        setPaidRef('');
                        setPaidDialog(it);
                      }}
                    />
                  </td>
                </tr>

                {/* 展开详情面板 */}
                {expandedId === it.id && (
                  <tr>
                    <td colSpan={8} className="px-4 py-4 bg-muted/10 border-b border-border">
                      {detailLoading ? (
                        <div className="flex justify-center py-4">
                          <Loader2 className="size-5 animate-spin text-muted-foreground" />
                        </div>
                      ) : detailData ? (
                        <div className="grid grid-cols-3 gap-6 text-sm">
                          {/* 代理信息 */}
                          <div className="space-y-2">
                            <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground mb-2">
                              <User className="size-3.5" />
                              代理信息
                            </div>
                            <div className="space-y-1.5">
                              <div><span className="text-muted-foreground">用户名：</span><span className="text-foreground">{detailData.username}</span></div>
                              <div><span className="text-muted-foreground">实名：</span><span className="text-foreground font-medium">{detailData.real_name || '未认证'}</span></div>
                              <div><span className="text-muted-foreground">手机号：</span><span className="text-foreground">{detailData.user_phone || '未绑定'}</span></div>
                              <div><span className="text-muted-foreground">昵称：</span><span className="text-foreground">{detailData.display_name || '--'}</span></div>
                            </div>
                          </div>

                          {/* 银行卡信息（打款用） */}
                          <div className="space-y-2">
                            <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground mb-2">
                              <CreditCard className="size-3.5" />
                              银行卡信息（打款用）
                            </div>
                            <div className="space-y-1.5">
                              <div><span className="text-muted-foreground">持卡人：</span><span className="text-foreground font-medium">{detailData.card_holder}</span></div>
                              <div><span className="text-muted-foreground">开户行：</span><span className="text-foreground">{detailData.bank_name}</span></div>
                              <div>
                                <span className="text-muted-foreground">卡号：</span>
                                <span className="text-foreground font-mono font-medium">{detailData.card_number_full}</span>
                              </div>
                              <div><span className="text-muted-foreground">预留手机：</span><span className="text-foreground">{detailData.card_phone}</span></div>
                            </div>
                          </div>

                          {/* 提现历史 + 佣金来源 */}
                          <div className="space-y-2">
                            <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground mb-2">
                              <History className="size-3.5" />
                              历史统计
                            </div>
                            <div className="space-y-1.5">
                              <div><span className="text-muted-foreground">历史提现：</span><span className="text-foreground">{detailData.history_paid_count} 笔，合计 ¥{fmtMoney(detailData.history_paid_total)}</span></div>
                            </div>
                            {detailData.recent_commissions?.length > 0 && (
                              <div className="mt-3">
                                <div className="text-xs text-muted-foreground mb-1.5">近期佣金来源：</div>
                                <div className="space-y-1 max-h-32 overflow-y-auto">
                                  {detailData.recent_commissions.map((c: any, i: number) => (
                                    <div key={i} className="text-xs flex justify-between">
                                      <span className="text-muted-foreground">
                                        {c.source_phone || c.source_username} · L{c.level}{c.level === 1 ? '直推' : '间推'}
                                      </span>
                                      <span className="text-foreground">¥{fmtMoney(c.amount_yuan)}</span>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                          </div>
                        </div>
                      ) : (
                        <div className="text-center text-muted-foreground text-sm">加载失败</div>
                      )}
                    </td>
                  </tr>
                )}
                </Fragment>
              ))}
            </tbody>
          </table>

          {/* pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between p-3 border-t border-border text-xs">
              <span className="text-muted-foreground">
                第 {page} / {totalPages} 页 (共 {total} 条)
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page <= 1}
                  onClick={() => setPage(p => p - 1)}
                >
                  上一页
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page >= totalPages}
                  onClick={() => setPage(p => p + 1)}
                >
                  下一页
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* reject dialog */}
      <Dialog
        open={!!rejectDialog}
        onOpenChange={open => { if (!open) setRejectDialog(null); }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>拒绝提现申请</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <Label className="text-xs">拒绝原因 *</Label>
              <Input
                className="mt-1"
                value={rejectReason}
                onChange={e => setRejectReason(e.target.value)}
                placeholder="请输入拒绝原因 (至少 2 个字)"
                maxLength={500}
              />
            </div>
            {actionError && (
              <div className="text-xs text-red-400 p-2 border border-red-500/30 bg-red-500/5 rounded">
                {actionError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRejectDialog(null)} disabled={submitting}>
              取消
            </Button>
            <Button
              variant="destructive"
              onClick={handleRejectConfirm}
              disabled={submitting}
            >
              {submitting && <Loader2 className="h-4 w-4 animate-spin mr-1" />}
              确认拒绝
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* paid dialog */}
      <Dialog
        open={!!paidDialog}
        onOpenChange={open => { if (!open) setPaidDialog(null); }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>确认已打款</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <Label className="text-xs">银行回单号 *</Label>
              <Input
                className="mt-1"
                value={paidRef}
                onChange={e => setPaidRef(e.target.value)}
                placeholder="请输入银行转账回单号"
                maxLength={200}
              />
            </div>
            {actionError && (
              <div className="text-xs text-red-400 p-2 border border-red-500/30 bg-red-500/5 rounded">
                {actionError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setPaidDialog(null)} disabled={submitting}>
              取消
            </Button>
            <Button onClick={handlePaidConfirm} disabled={submitting}>
              {submitting && <Loader2 className="h-4 w-4 animate-spin mr-1" />}
              确认打款
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ---------- action cell ----------

function ActionCell({
  item,
  submitting,
  onApprove,
  onReject,
  onPaid,
}: {
  item: WithdrawalItem;
  submitting: boolean;
  onApprove: () => void;
  onReject: () => void;
  onPaid: () => void;
}) {
  switch (item.status) {
    case 'pending':
      return (
        <div className="flex gap-1.5">
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs text-emerald-400 border-emerald-500/30 hover:bg-emerald-500/10"
            disabled={submitting}
            onClick={onApprove}
          >
            <CheckCircle2 className="h-3.5 w-3.5 mr-1" />
            批准
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs text-red-400 border-red-500/30 hover:bg-red-500/10"
            disabled={submitting}
            onClick={onReject}
          >
            <XCircle className="h-3.5 w-3.5 mr-1" />
            拒绝
          </Button>
        </div>
      );
    case 'approved':
      return (
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-xs"
          disabled={submitting}
          onClick={onPaid}
        >
          <Banknote className="h-3.5 w-3.5 mr-1" />
          确认已打款
        </Button>
      );
    case 'rejected':
      return (
        <span className="text-xs text-muted-foreground" title={item.reject_reason || ''}>
          {item.reject_reason
            ? (item.reject_reason.length > 20
                ? item.reject_reason.slice(0, 20) + '...'
                : item.reject_reason)
            : '--'}
        </span>
      );
    case 'paid':
      return (
        <span className="text-xs text-muted-foreground font-mono" title="银行回单号">
          {item.external_tx_ref || '--'}
        </span>
      );
    default:
      return null;
  }
}
