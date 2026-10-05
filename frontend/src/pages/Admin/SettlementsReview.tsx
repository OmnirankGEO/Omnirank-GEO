/**
 * V3.5 W2 · Admin 结算审批中心
 *
 * 路由: /admin/settlements
 * 责任: 待审/已审/已打款列表 + approve/reject/mark_paid 操作
 * mark_paid 必须填 transfer_proof_url OR wire_transfer_no(W2 验收 gate #8)
 */
import { useEffect, useState } from 'react';
import { adminApi, formatCents } from '@/lib/v35w2Api';
import { formatApiErrorForDisplay } from '@/lib/api';
import { payoutMethodLabel } from '@/lib/v35Terminology';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogTrigger } from '@/components/ui/dialog';
import { Wallet, RefreshCw, Check, X, CreditCard } from 'lucide-react';
import { toast } from 'sonner';

interface SettlementItem {
  id: number;
  agent_user_id: number;
  agent_display_name?: string | null;
  agent_phone?: string | null;
  amount_cents: number;          // 服务方申请额(gross)
  net_amount_cents?: number;     // [1D] 实际应打款额(扣手续费/税后)· 老后端无此字段时回落 gross
  total_fee_cents?: number;      // [1D] 平台扣的手续费 + 税合计 · 老后端无此字段时按 gross-net 推
  payout_method: string;
  payout_account: string;
  status: string;
  created_at: string;
  ledger_count: number;
}

// [1D backward-compat · 部署前必接] 财务必须按 net(应付额)打款 · 不是 gross(申请额)· 否则多付 fees
function settlementAmounts(it: SettlementItem) {
  const gross = it.amount_cents;
  const net = it.net_amount_cents ?? gross;
  const fee = it.total_fee_cents ?? Math.max(0, gross - net);
  return { gross, net, fee };
}

const STATUS_LABEL: Record<string, string> = {
  pending: '待审核',
  approved: '已审核',
  paid: '已打款',
  rejected: '已驳回',
};

const STATUS_COLOR: Record<string, any> = {
  pending: 'secondary',
  approved: 'default',
  paid: 'outline',
  rejected: 'destructive',
};

export default function SettlementsReview() {
  const [tab, setTab] = useState('pending');
  const [items, setItems] = useState<SettlementItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);

  const reload = async (status?: string) => {
    setLoading(true);
    try {
      const r = await adminApi.settlementsList(status, 100, 0);
      setItems(r.items || []);
      setTotal(r.total || 0);
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '加载失败 · 请重试', 'admin'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { reload(tab); }, [tab]);

  return (
    <div className="container mx-auto py-6 space-y-6 max-w-7xl">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Wallet className="w-6 h-6" />
          <h1 className="text-2xl font-bold">服务收益打款审核</h1>
        </div>
        <Button variant="ghost" size="sm" onClick={() => reload(tab)}>
          <RefreshCw className="w-4 h-4 mr-1" /> 刷新
        </Button>
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          {Object.entries(STATUS_LABEL).map(([k, v]) => (
            <TabsTrigger key={k} value={k}>{v}</TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value={tab} className="pt-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">共 {total} 条</CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/50">
                    <th className="text-left p-3">编号</th>
                    <th className="text-left p-3">服务方</th>
                    <th className="text-right p-3">应付额(申请 / 扣费)</th>
                    <th className="text-left p-3">渠道</th>
                    <th className="text-right p-3">账号</th>
                    <th className="text-right p-3">结算明细数</th>
                    <th className="text-left p-3">状态</th>
                    <th className="text-left p-3">提交时间</th>
                    <th className="text-right p-3">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.length === 0 && (
                    <tr><td colSpan={9} className="text-center p-6 text-muted-foreground">无{STATUS_LABEL[tab]}申请</td></tr>
                  )}
                  {items.map((it) => (
                    <tr key={it.id} className="border-b">
                      <td className="p-3 font-mono text-xs">#{it.id}</td>
                      <td className="p-3">
                        {it.agent_display_name || `服务方编号 ${it.agent_user_id}`}
                        {it.agent_phone && <div className="text-xs text-muted-foreground">{it.agent_phone}</div>}
                      </td>
                      <td className="p-3 text-right font-mono">
                        {(() => {
                          const { gross, net, fee } = settlementAmounts(it);
                          if (fee <= 0) return <span className="font-semibold">{formatCents(gross)}</span>;
                          return (
                            <div className="leading-tight">
                              <div className="font-semibold text-emerald-600">应付 {formatCents(net)}</div>
                              <div className="text-[11px] text-muted-foreground">申请 {formatCents(gross)} · 扣 {formatCents(fee)}</div>
                            </div>
                          );
                        })()}
                      </td>
                      <td className="p-3 text-xs">{payoutMethodLabel(it.payout_method)}</td>
                      <td className="p-3 text-right text-xs font-mono">{it.payout_account}</td>
                      <td className="p-3 text-right text-xs">{it.ledger_count}</td>
                      <td className="p-3"><Badge variant={STATUS_COLOR[it.status] as any}>{STATUS_LABEL[it.status]}</Badge></td>
                      <td className="p-3 text-xs">{new Date(it.created_at).toLocaleString()}</td>
                      <td className="p-3 text-right">
                        <ActionButtons item={it} onDone={() => reload(tab)} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function ActionButtons({ item, onDone }: { item: SettlementItem; onDone: () => void }) {
  const [rejectOpen, setRejectOpen] = useState(false);
  const [paidOpen, setPaidOpen] = useState(false);

  if (item.status === 'pending') {
    return (
      <div className="flex gap-1 justify-end">
        <Button size="sm" variant="outline" onClick={async () => {
          try {
            await adminApi.settlementPatch(item.id, { action: 'approve' });
            toast.success('已审核通过');
            onDone();
          } catch (e: any) {
            toast.error(formatApiErrorForDisplay(e, '审核失败 · 请重试', 'admin'));
          }
        }}>
          <Check className="w-4 h-4 mr-1" /> 审核
        </Button>
        <Dialog open={rejectOpen} onOpenChange={setRejectOpen}>
          <DialogTrigger asChild>
            <Button size="sm" variant="ghost" className="text-red-600">
              <X className="w-4 h-4 mr-1" /> 驳回
            </Button>
          </DialogTrigger>
          <RejectDialog id={item.id} onDone={() => { setRejectOpen(false); onDone(); }} />
        </Dialog>
      </div>
    );
  }

  if (item.status === 'approved') {
    return (
      <Dialog open={paidOpen} onOpenChange={setPaidOpen}>
        <DialogTrigger asChild>
          <Button size="sm" variant="default">
            <CreditCard className="w-4 h-4 mr-1" /> 标记已打款
          </Button>
        </DialogTrigger>
        <MarkPaidDialog item={item} onDone={() => { setPaidOpen(false); onDone(); }} />
      </Dialog>
    );
  }

  return <span className="text-xs text-muted-foreground">-</span>;
}

function RejectDialog({ id, onDone }: { id: number; onDone: () => void }) {
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  return (
    <DialogContent>
      <DialogHeader><DialogTitle>驳回提现申请 #{id}</DialogTitle></DialogHeader>
      <div>
        <Label>驳回原因</Label>
        <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="必填" />
      </div>
      <DialogFooter>
        <Button variant="destructive" disabled={busy || !reason} onClick={async () => {
          setBusy(true);
          try {
            await adminApi.settlementPatch(id, { action: 'reject', reject_reason: reason });
            toast.success('已驳回');
            onDone();
          } catch (e: any) {
            toast.error(formatApiErrorForDisplay(e, '操作失败 · 请重试', 'admin'));
          } finally { setBusy(false); }
        }}>确认驳回</Button>
      </DialogFooter>
    </DialogContent>
  );
}

function MarkPaidDialog({ item, onDone }: { item: SettlementItem; onDone: () => void }) {
  const [proofUrl, setProofUrl] = useState('');
  const [wireNo, setWireNo] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const { gross, net, fee } = settlementAmounts(item);
  return (
    <DialogContent>
      <DialogHeader><DialogTitle>标记已打款 · 申请 #{item.id}</DialogTitle></DialogHeader>
      {/* [1D 部署前必接] 应付额醒目 · 财务按 net 打款不是 gross · 否则多付手续费/税 */}
      <div className="rounded-md bg-emerald-500/10 border border-emerald-500/20 p-3">
        <div className="text-xs text-muted-foreground">实际应打款金额(已扣手续费 / 税)</div>
        <div className="text-2xl font-bold text-emerald-600 font-mono">{formatCents(net)}</div>
        {fee > 0 && (
          <div className="text-xs text-muted-foreground mt-1">
            服务方申请 {formatCents(gross)} · 平台扣 {formatCents(fee)} · 请按 <span className="text-emerald-600 font-semibold">应付额 {formatCents(net)}</span> 打款
          </div>
        )}
      </div>
      <div className="space-y-3">
        <div>
          <Label>凭证链接</Label>
          <Input value={proofUrl} onChange={(e) => setProofUrl(e.target.value)} placeholder="https://..." />
        </div>
        <div>
          <Label>银行流水号</Label>
          <Input value={wireNo} onChange={(e) => setWireNo(e.target.value)} placeholder="如:WT202605260001" />
        </div>
        <div>
          <Label>备注</Label>
          <Input value={note} onChange={(e) => setNote(e.target.value)} />
        </div>
        <p className="text-xs text-red-600">⚠️ 至少填写凭证链接或银行流水号其中一项</p>
      </div>
      <DialogFooter>
        <Button disabled={busy || (!proofUrl && !wireNo)} onClick={async () => {
          setBusy(true);
          try {
            await adminApi.settlementPatch(item.id, {
              action: 'mark_paid',
              transfer_proof_url: proofUrl || undefined,
              wire_transfer_no: wireNo || undefined,
              admin_note: note || undefined,
            });
            toast.success('已标记打款');
            onDone();
          } catch (e: any) {
            toast.error(formatApiErrorForDisplay(e, '操作失败 · 请重试', 'admin'));
          } finally { setBusy(false); }
        }}>确认</Button>
      </DialogFooter>
    </DialogContent>
  );
}
