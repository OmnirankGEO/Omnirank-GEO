/**
 * V3.5 W2 · 代理工厂结算中心
 *
 * 路由: /agent/settlement
 * 责任: 5 池余额 + 可提 ledger items + 提现申请
 * 待抵扣(clawback)为红色风险卡 · 不和可提混算
 */
import { useEffect, useState } from 'react';
import { agentApi, formatCents } from '@/lib/v35w2Api';
import { formatApiErrorForDisplay } from '@/lib/api';
import { cn } from '@/lib/utils';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Wallet, RefreshCw, ArrowDownToLine, ArrowRight, Inbox, Repeat } from 'lucide-react';
import { toast } from 'sonner';

interface Balance {
  frozen_cents: number;
  available_cents: number;
  pending_cents: number;
  paid_cents: number;
  clawback_pending_cents: number;
  tax_withholding_total_cents: number;
}

interface LedgerItem {
  id: number;
  source: string;
  recharge_order_id?: string | null;
  customer_user_id?: number | null;
  customer_paid_cents: number;
  amount_cents: number;
  tax_withholding_cents: number;
  status: string;
  frozen_at?: string | null;
  settle_at?: string | null;
  settled_at?: string | null;
  manual_review_required: boolean;
  note?: string | null;
}

// 次级状态小卡
function StatTile({ label, value, dotClass, valueClass, risk }: {
  label: string; value: number; dotClass: string; valueClass?: string; risk?: boolean;
}) {
  return (
    <div className={cn('rounded-xl border p-3', risk ? 'border-red-500/30 bg-red-500/5' : 'border-border bg-card')}>
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', dotClass)} />{label}
      </p>
      <p className={cn('mt-1.5 text-lg font-bold tabular-nums', valueClass || 'text-foreground')}>{formatCents(value)}</p>
    </div>
  );
}

function Line({ k, v, muted }: { k: string; v: string; muted?: boolean }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-muted-foreground">{k}</span>
      <span className={cn('tabular-nums', muted ? 'text-muted-foreground' : 'font-medium text-foreground')}>{v}</span>
    </div>
  );
}

export default function SettlementCenter() {
  const [balance, setBalance] = useState<Balance | null>(null);
  const [items, setItems] = useState<LedgerItem[]>([]);
  const [totalAvailable, setTotalAvailable] = useState(0);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(false);
  const [redeemOpen, setRedeemOpen] = useState(false);  // [3C] 利润换算力弹窗
  // GAPS#4 已绑定收款账户(null = 未绑定)
  const [payoutAccount, setPayoutAccount] = useState<{
    bank_name?: string; account_holder?: string; account_no_masked?: string;
    bank?: string; account_tail?: string;
  } | null>(null);
  const [payoutLoadError, setPayoutLoadError] = useState(false);  // 区分"接口加载失败"与"未绑定"

  const reload = async () => {
    setLoading(true);
    try {
      const [b, a] = await Promise.all([
        agentApi.settlementBalance(),
        agentApi.settlementAvailable(),
      ]);
      setBalance(b as Balance);
      setItems(a.items || []);
      setTotalAvailable(a.total_amount_cents || 0);
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '加载失败 · 请重试', 'agent'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { reload(); }, []);

  // GAPS#4 挂载时拉已绑定收款账户(失败/未绑定均显"申请时填写")
  useEffect(() => {
    agentApi.payoutAccount()
      .then((acc) => { setPayoutAccount(acc ?? null); setPayoutLoadError(false); })
      .catch(() => { setPayoutAccount(null); setPayoutLoadError(true); });
  }, []);

  const canWithdraw = totalAvailable > 0;
  const clawback = balance?.clawback_pending_cents ?? 0;
  // 不可提现原因(派生 · 仅用现有数据)
  const blockReason =
    (balance?.frozen_cents ?? 0) > 0 ? `${formatCents(balance!.frozen_cents)} 还在 T+3 冻结期,到期后转可结算`
    : (balance?.pending_cents ?? 0) > 0 ? `${formatCents(balance!.pending_cents)} 提现申请处理中`
    : '暂无可结算金额';

  return (
    <div className="container mx-auto py-6 lg:py-8 space-y-6 max-w-7xl">
      {/* 标题区 */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-muted"><Wallet className="h-5 w-5" /></div>
          <div className="min-w-0">
            <h1 className="text-xl font-bold sm:text-2xl">提现结算</h1>
            <p className="mt-0.5 text-xs text-muted-foreground">成交回款 · 税费抵扣 · 提现打款</p>
          </div>
        </div>
        <Button variant="ghost" size="sm" onClick={reload} disabled={loading} className="shrink-0">
          <RefreshCw className={cn('w-4 h-4 mr-1', loading && 'animate-spin')} /> 刷新
        </Button>
      </div>

      {/* 顶部:可提现主指标 + 4 次级状态 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="border-emerald-500/30 bg-emerald-500/5 lg:col-span-1">
          <CardContent className="p-5">
            <p className="flex items-center gap-1.5 text-xs text-muted-foreground"><Wallet className="h-3.5 w-3.5" />可提现金额</p>
            <p className="mt-2 text-4xl font-bold tabular-nums text-emerald-500">{formatCents(balance?.available_cents ?? 0)}</p>
            <p className="mt-2 text-xs text-muted-foreground">{items.length > 0 ? `来自 ${items.length} 笔可结算订单` : '暂无可结算订单'}</p>
          </CardContent>
        </Card>
        <div className="grid grid-cols-2 gap-3 lg:col-span-2 lg:grid-cols-4">
          <StatTile label="冻结中(T+3)" value={balance?.frozen_cents ?? 0} dotClass="bg-blue-500" />
          <StatTile label="申请中" value={balance?.pending_cents ?? 0} dotClass="bg-blue-500" />
          <StatTile label="已打款" value={balance?.paid_cents ?? 0} dotClass="bg-muted-foreground" valueClass="text-muted-foreground" />
          <StatTile
            label="待抵扣"
            value={clawback}
            dotClass={clawback > 0 ? 'bg-red-500' : 'bg-muted-foreground'}
            valueClass={clawback > 0 ? 'text-red-500' : 'text-muted-foreground'}
            risk={clawback > 0}
          />
        </div>
      </div>

      {/* 结算流程条 */}
      <Card>
        <CardContent className="flex flex-col gap-2 py-4 sm:flex-row sm:items-center sm:gap-4">
          <span className="shrink-0 text-xs font-medium text-foreground/80">结算流程</span>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-2 text-xs text-muted-foreground">
            {['客户付款', 'T+3 冻结', '可结算', '申请提现', '打款完成'].map((s, i) => (
              <span key={s} className="flex items-center gap-2">
                {i > 0 && <ArrowRight className="h-3.5 w-3.5 opacity-40" />}
                <span className="rounded-md bg-muted px-2.5 py-1 text-foreground/70">{s}</span>
              </span>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* 累计预扣税(轻量摘要行) */}
      <Card>
        <CardContent className="flex items-center justify-between gap-3 py-3">
          <span className="text-sm text-muted-foreground">累计预扣税(汇总 · 已从可结算金额中扣除)</span>
          <span className="font-mono text-sm tabular-nums">{formatCents(balance?.tax_withholding_total_cents ?? 0)}</span>
        </CardContent>
      </Card>

      {/* 明细表 + 提现助手 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* 可提现明细 */}
        <Card className="lg:col-span-2">
          <CardHeader className="flex flex-row items-center justify-between gap-3">
            <div>
              <CardTitle className="text-base">可提现明细</CardTitle>
              <p className="mt-1 text-xs text-muted-foreground">共 {items.length} 条已可结算 · 总额 {formatCents(totalAvailable)}</p>
            </div>
            <Button onClick={() => setOpen(true)} disabled={!canWithdraw} className="shrink-0">
              <ArrowDownToLine className="w-4 h-4 mr-1" /> 申请提现
            </Button>
          </CardHeader>
          <CardContent className="p-0">
            {items.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-muted"><Inbox className="h-6 w-6 text-muted-foreground/60" /></div>
                <p className="text-sm font-medium">暂无可提现订单</p>
                <p className="mt-1 text-xs text-muted-foreground">客户付款 T+3 冻结到期后会出现在这里</p>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-xs text-muted-foreground">
                      <th className="p-3 text-left font-medium whitespace-nowrap">编号</th>
                      <th className="p-3 text-left font-medium whitespace-nowrap">订单</th>
                      <th className="p-3 text-right font-medium whitespace-nowrap">客户付</th>
                      <th className="p-3 text-right font-medium whitespace-nowrap">税预扣</th>
                      <th className="p-3 text-right font-medium whitespace-nowrap">可结算</th>
                      <th className="p-3 text-left font-medium whitespace-nowrap">可结算时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((it) => (
                      <tr key={it.id} className="border-b border-border last:border-0">
                        <td className="p-3 font-mono text-xs text-muted-foreground">#{it.id}</td>
                        <td className="p-3 text-xs text-muted-foreground">{it.recharge_order_id ?? '-'}</td>
                        <td className="p-3 text-right font-mono tabular-nums whitespace-nowrap">{formatCents(it.customer_paid_cents)}</td>
                        <td className="p-3 text-right font-mono tabular-nums whitespace-nowrap text-muted-foreground">{formatCents(it.tax_withholding_cents)}</td>
                        <td className="p-3 text-right font-mono font-semibold tabular-nums whitespace-nowrap text-emerald-500">{formatCents(it.amount_cents)}</td>
                        <td className="p-3 text-xs text-muted-foreground whitespace-nowrap">{it.settled_at ? new Date(it.settled_at).toLocaleString() : '-'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardContent>
        </Card>

        {/* 提现助手 */}
        <aside className="lg:col-span-1">
          <Card className="lg:sticky lg:top-6">
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-sm"><ArrowDownToLine className="h-4 w-4 text-emerald-500" />提现助手</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="rounded-lg border border-border bg-muted/30 p-3">
                <p className="text-xs text-muted-foreground">当前可提现</p>
                <p className="mt-1 text-2xl font-bold tabular-nums text-emerald-500">{formatCents(totalAvailable)}</p>
                <p className="mt-0.5 text-xs text-muted-foreground">{items.length} 笔可结算订单</p>
              </div>
              <div className="space-y-2 text-sm">
                <Line k="累计预扣税" v={formatCents(balance?.tax_withholding_total_cents ?? 0)} />
                {(() => {
                  if (payoutLoadError) return <Line k="提现账户" v="暂未加载,请刷新重试" muted />;
                  if (!payoutAccount) return <Line k="提现账户" v="未绑定提现账户" muted />;
                  const bank = payoutAccount.bank_name || payoutAccount.bank || '';
                  const tail = payoutAccount.account_no_masked || payoutAccount.account_tail || '';
                  const v = bank && tail ? `已绑定 · ${bank} 尾号${tail}`
                    : tail ? `已绑定提现账户 · 尾号${tail}`
                    : bank ? `已绑定 · ${bank}`
                    : '已绑定提现账户';
                  return <Line k="提现账户" v={v} />;
                })()}
              </div>
              {!canWithdraw && (
                <div className="rounded-lg border border-border bg-muted/30 p-3 text-xs">
                  <p className="font-medium text-foreground/80">暂不可提现</p>
                  <p className="mt-1 text-muted-foreground">{blockReason}</p>
                </div>
              )}
              <Button className="w-full" disabled={!canWithdraw} onClick={() => setOpen(true)}>
                <ArrowDownToLine className="w-4 h-4 mr-1" /> 申请提现
              </Button>
              {/* [3C] 利润换算力:可结算利润等价换库存算力(继续做单)· 与提现并列 */}
              <Button variant="outline" className="w-full" disabled={!canWithdraw} onClick={() => setRedeemOpen(true)}>
                <Repeat className="w-4 h-4 mr-1" /> 或 换算力进货
              </Button>
              <p className="text-[11px] text-muted-foreground leading-relaxed">
                想继续做单?可把可结算利润按进货折扣<span className="text-foreground">等价换成库存算力</span> · 即时到账 · 不扣手续费 / 税。
              </p>
            </CardContent>
          </Card>
        </aside>
      </div>

      {/* 提现申请弹窗(受控 · 两处入口共用) */}
      <Dialog open={open} onOpenChange={setOpen}>
        <RequestDialog max={totalAvailable} onDone={() => { setOpen(false); reload(); }} />
      </Dialog>

      {/* [3C] 利润换算力弹窗 */}
      <Dialog open={redeemOpen} onOpenChange={setRedeemOpen}>
        <RedeemDialog max={totalAvailable} onDone={() => { setRedeemOpen(false); reload(); }} />
      </Dialog>
    </div>
  );
}

function RequestDialog({ max, onDone }: { max: number; onDone: () => void }) {
  const [amount, setAmount] = useState(String((max / 100).toFixed(2)));
  const [bank, setBank] = useState('');
  const [account, setAccount] = useState('');
  const [holder, setHolder] = useState('');
  const [invoice, setInvoice] = useState(false);
  const [busy, setBusy] = useState(false);
  // [3D] 提现报价三段预览(debounce · 金额变化拉 quote · 提交前看「提 / 扣 / 到账」)
  const [quote, setQuote] = useState<Awaited<ReturnType<typeof agentApi.withdrawalQuote>> | null>(null);
  const [quoting, setQuoting] = useState(false);

  useEffect(() => {
    const yuan = parseFloat(amount);
    if (!yuan || yuan <= 0) { setQuote(null); return; }
    let cancelled = false;
    setQuoting(true);
    const t = setTimeout(async () => {
      try {
        const q = await agentApi.withdrawalQuote(yuan);
        if (!cancelled) setQuote(q);
      } catch {
        if (!cancelled) setQuote(null);
      } finally {
        if (!cancelled) setQuoting(false);
      }
    }, 350);
    return () => { cancelled = true; clearTimeout(t); };
  }, [amount]);

  const submit = async () => {
    setBusy(true);
    try {
      const cents = Math.round(parseFloat(amount) * 100);
      if (cents <= 0 || cents > max) throw new Error(`金额必须在 0 ~ ${formatCents(max)} 之间`);
      if (!bank || !account || !holder) throw new Error('银行 / 账号 / 户名必填');
      await agentApi.createSettlementRequest({
        amount_cents: cents,
        bank_name: bank,
        bank_account: account,
        account_holder: holder,
        invoice_required: invoice,
      });
      toast.success('提现申请已提交 · 等待财务审核');
      onDone();
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '提交失败 · 请重试', 'agent'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogContent>
      <DialogHeader>
        <DialogTitle>提现申请</DialogTitle>
      </DialogHeader>
      <div className="space-y-3">
        <div>
          <Label>金额(元)· 上限 {formatCents(max)}</Label>
          <Input type="number" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} />
          <p className="text-xs text-muted-foreground mt-1">系统按时序自动选取可结算明细锁定 · 同一明细不重复锁定</p>
        </div>
        {/* [3D] 提现报价三段:提 / 扣 / 到账(提交前透明 · 防到账与预期不符) */}
        {quoting && <p className="text-xs text-muted-foreground">正在计算到账金额…</p>}
        {quote && !quoting && (
          <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-3 space-y-1.5">
            <Line k="提现金额" v={formatCents(quote.gross_cents)} />
            <Line k="平台服务费" v={`- ${formatCents(quote.platform_fee_cents)}`} muted />
            {quote.tax_cents > 0 && <Line k="代扣税" v={`- ${formatCents(quote.tax_cents)}`} muted />}
            <div className="flex justify-between gap-2 border-t border-border pt-1.5">
              <span className="text-sm font-medium">实际到账</span>
              <span className="text-lg font-bold tabular-nums text-emerald-500">{formatCents(quote.net_cents)}</span>
            </div>
            {!quote.sufficient && (
              <p className="text-xs text-red-500">⚠️ 超过可提现余额 {formatCents(quote.available_cents)}</p>
            )}
          </div>
        )}
        <div><Label>开户银行</Label><Input value={bank} onChange={(e) => setBank(e.target.value)} placeholder="如:招商银行" /></div>
        <div><Label>银行账号</Label><Input value={account} onChange={(e) => setAccount(e.target.value)} /></div>
        <div><Label>账户姓名</Label><Input value={holder} onChange={(e) => setHolder(e.target.value)} /></div>
        <div className="flex items-center gap-2">
          <input type="checkbox" checked={invoice} onChange={(e) => setInvoice(e.target.checked)} id="inv" />
          <Label htmlFor="inv">需要发票</Label>
        </div>
      </div>
      <DialogFooter>
        <Button onClick={submit} disabled={busy || quoting || (!!quote && !quote.sufficient)}>
          {quoting ? '计算中…' : '提交申请'}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}

// [3C] 利润换算力弹窗:可结算利润按出厂折扣等价换 paid_inventory 算力(即时 · 无审核 · 不扣 fees/税)
// 后端无 GET 预览端点 · 确认后 POST 返回精确 inventory_points_granted 再展示
function RedeemDialog({ max, onDone }: { max: number; onDone: () => void }) {
  const [amount, setAmount] = useState(String((max / 100).toFixed(2)));
  const [busy, setBusy] = useState(false);
  const [granted, setGranted] = useState<number | null>(null);
  // [P2-2] 当前进货折扣率(算力/元)· 从进货档反推(利润换算力与代理进货同口径 · 无现成 GET 预览端点)
  const [rate, setRate] = useState<number | null>(null);
  useEffect(() => {
    agentApi.purchaseOptions().then(r => {
      const o = (r.options || [])[0] as any;
      const bp = o?.base_points ?? o?.base ?? 0;
      if (o && o.amount_cents > 0 && bp > 0) setRate((bp / o.amount_cents) * 100);
    }).catch(() => {});
  }, []);
  // [P2-1] 换前估算(floor · 约 · 以确认后实际到账为准 · 后端按 calc_redeem_points 精算)
  const yuan = parseFloat(amount);
  const estPoints = rate && yuan > 0 ? Math.floor(yuan * rate) : null;

  const submit = async () => {
    setBusy(true);
    try {
      const yuan = parseFloat(amount);
      if (!yuan || yuan <= 0 || Math.round(yuan * 100) > max) {
        throw new Error(`金额必须在 0 ~ ${formatCents(max)} 之间`);
      }
      const r = await agentApi.redeemFromCommission(yuan);
      const n = r.inventory_points_granted ?? 0;
      setGranted(n);
      toast.success(`已换得 ${n.toLocaleString()} 算力 · 已入库存`);
      setTimeout(onDone, 1200);  // 给用户看一眼结果再关
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '换算力失败 · 请重试', 'agent'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogContent>
      <DialogHeader>
        <DialogTitle>利润换算力(进货)</DialogTitle>
      </DialogHeader>
      <div className="space-y-3">
        <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-3 text-xs text-muted-foreground leading-relaxed">
          把可结算利润按当前进货折扣<span className="text-foreground font-medium">等价换成可用库存算力</span> · 即时到账 · 不走银行 · 不扣手续费 / 税。
          <br />适合想继续做单的服务商(等于用税前利润进货)· <span className="text-amber-500">换出后不可逆</span>。
        </div>
        <div>
          <Label>换算力金额(元)· 上限 {formatCents(max)}</Label>
          <Input type="number" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} disabled={granted !== null} />
          {rate && (
            <p className="text-xs text-muted-foreground mt-1">当前进货折扣 · 1 元 ≈ {Math.round(rate).toLocaleString()} 算力</p>
          )}
        </div>
        {/* [P2-1] 换前估算(确认后以实际到账为准) */}
        {estPoints !== null && granted === null && (
          <div className="rounded-lg border border-border bg-muted/30 p-3 text-center">
            <p className="text-xs text-muted-foreground">预计换得</p>
            <p className="text-xl font-bold tabular-nums">≈ {estPoints.toLocaleString()} 算力</p>
            <p className="text-[11px] text-muted-foreground mt-0.5">约 · 以确认后实际到账为准</p>
          </div>
        )}
        {granted !== null && (
          <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-3 text-center">
            <p className="text-xs text-muted-foreground">已换得</p>
            <p className="text-2xl font-bold tabular-nums text-emerald-500">{granted.toLocaleString()} 算力</p>
          </div>
        )}
      </div>
      <DialogFooter>
        <Button onClick={submit} disabled={busy || granted !== null}>
          <Repeat className="w-4 h-4 mr-1" /> 确认换算力
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}
