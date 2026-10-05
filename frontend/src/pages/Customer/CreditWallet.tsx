/**
 * V3.5 W3 · 客户算力钱包
 *
 * 路由: /customer/wallet
 * 责任: tool/publish/bonus 三池 + 最近使用 + 平台服务状态
 * 客户身份铁律:不出现"积分"/"¥X = N 积分"/"目标出现率"/"SOV"
 */
import { useEffect, useRef, useState } from 'react';
import { customerApi } from '@/lib/v35w3Api';
import { transactionDisplay } from '@/lib/v35Terminology';
import { formatApiErrorForDisplay } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Briefcase, Wallet, ShoppingBag, RefreshCw } from 'lucide-react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import { useWallet } from '@/context/WalletContext';
import { WalletStatusNotice } from '@/components/wallet/WalletStatusNotice';

interface Summary {
  tool_credit_points: number;
  publish_credit_points: number;
  bonus_credit_points: number;
  total_purchased_points: number;
  service: {
    service_status: 'platform_managed';
    configuration_status: 'ready' | 'review_required' | 'unavailable';
    account_configured: boolean;
    dispute_pending: boolean;
  };
  recent_transactions: Array<any>;
}

export default function CreditWallet() {
  const [s, setS] = useState<Summary | null>(null);
  const [loading, setLoading] = useState(true);
  const [summaryStatus, setSummaryStatus] = useState<'initial' | 'loading' | 'ready' | 'stale' | 'error'>('initial');
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const summaryRequestSequence = useRef(0);
  const hasSummaryRef = useRef(false);
  const wallet = useWallet();
  // [3E] 查看全部额度流水(2A 失败退费 / 2B 长任务释放退回)· 分页
  const [expanded, setExpanded] = useState(false);
  const [moreTx, setMoreTx] = useState<any[]>([]);
  const [txOffset, setTxOffset] = useState(0);
  const [txTotal, setTxTotal] = useState(0);
  const [txLoading, setTxLoading] = useState(false);

  const reload = async () => {
    const requestSequence = ++summaryRequestSequence.current;
    setLoading(true);
    setSummaryStatus('loading');
    setSummaryError(null);
    try {
      const r = await customerApi.creditSummary();
      if (requestSequence !== summaryRequestSequence.current) return;
      setS(r);
      hasSummaryRef.current = true;
      setSummaryStatus('ready');
    } catch (e: any) {
      if (requestSequence !== summaryRequestSequence.current) return;
      const message = formatApiErrorForDisplay(e, '服务算力暂时无法读取 · 请重试', 'customer');
      setSummaryError(message);
      setSummaryStatus(hasSummaryRef.current ? 'stale' : 'error');
      toast.error(message);
    } finally {
      if (requestSequence === summaryRequestSequence.current) setLoading(false);
    }
  };

  // [3E] 查看全部额度流水(含 2A 工具失败退费 / 2B 长任务失败释放退回 · 分页)
  const loadMoreTx = async (reset = false) => {
    setTxLoading(true);
    try {
      const offset = reset ? 0 : txOffset;
      const r = await customerApi.creditTransactions(20, offset);
      setMoreTx(prev => reset ? (r.items || []) : [...prev, ...(r.items || [])]);
      setTxTotal(r.total || 0);
      setTxOffset(offset + 20);
      setExpanded(true);
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '加载流水失败 · 请重试', 'customer'));
    } finally {
      setTxLoading(false);
    }
  };

  useEffect(() => {
    void reload();
    return () => { summaryRequestSequence.current += 1; };
  }, []);

  const txList = expanded ? moreTx : (s?.recent_transactions ?? []);

  return (
    <div className="container mx-auto py-6 space-y-6 max-w-5xl">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Wallet className="w-6 h-6" />
          <h1 className="text-2xl font-bold">当前服务可用算力</h1>
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" size="sm" onClick={reload}><RefreshCw className="w-4 h-4 mr-1" />刷新</Button>
          {wallet.status === 'ready' ? (
            <Button asChild>
              <Link to="/customer/recharge"><ShoppingBag className="w-4 h-4 mr-1" />购买算力</Link>
            </Button>
          ) : (
            <Button disabled title="请先成功读取账户余额，再进入购买"><ShoppingBag className="w-4 h-4 mr-1" />购买算力</Button>
          )}
        </div>
      </div>

      <p className="rounded-lg border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
        本页说明当前服务能使用的算力；账户充值、赠送、冻结和全部交易请查看 <Link className="text-primary underline underline-offset-4" to="/wallet">钱包与交易记录</Link>。
      </p>

      <WalletStatusNotice
        status={wallet.status}
        errorMessage={wallet.errorMessage}
        lastUpdatedAt={wallet.lastUpdatedAt}
        onRetry={wallet.refreshBalance}
      />

      {summaryStatus === 'error' && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm">
          <p className="font-medium">服务算力明细暂时无法读取</p>
          <p className="mt-1 text-muted-foreground">{summaryError}；这不代表算力为 0，也不会自动扣钱。</p>
          <Button variant="outline" size="sm" className="mt-3" onClick={() => void reload()}>重试读取服务算力</Button>
        </div>
      )}
      {summaryStatus === 'stale' && (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-4 text-sm">
          当前显示上次成功读取的服务算力，数据可能已过期。{summaryError}
        </div>
      )}
      {loading && !s && <div className="rounded-lg border border-border p-6 text-center text-sm text-muted-foreground" role="status">正在读取服务算力…</div>}

      {s && (
        <Card className="bg-muted/30">
          <CardContent className="py-3 flex items-center gap-2">
            <Briefcase className="w-4 h-4 text-muted-foreground" />
            <span className="text-sm">由 OmniRank 平台提供服务</span>
          </CardContent>
        </Card>
      )}

      {(wallet.status === 'ready' || wallet.status === 'stale' || wallet.lastUpdatedAt !== null) && <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <PoolCard
          label="总算力"
          value={wallet.totalPoints}
          desc="与右上角钱包一致"
        />
        <PoolCard
          label="充值算力"
          value={wallet.paidPointsDisplayed}
          desc="诊断 / 写作 / 监测 / 报告 / 发布"
        />
        <PoolCard
          label="赠送算力"
          value={wallet.bonusPointsDisplayed}
          desc="仅用于功能 · 不可发布"
          warn={wallet.bonusPointsDisplayed > 0}
        />
      </div>}

      {s && <Card>
        <CardHeader>
          <CardTitle className="text-base">最近使用</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
          <table className="min-w-[640px] text-sm">
            <thead>
              <tr className="border-b bg-muted/50">
                <th className="w-[140px] text-left p-3">时间</th>
                <th className="min-w-[260px] text-left p-3">说明</th>
                <th className="w-[130px] text-left p-3">类型</th>
                <th className="w-[90px] text-right p-3">变动</th>
              </tr>
            </thead>
            <tbody>
              {!txList.length && (
                <tr><td colSpan={4} className="p-10">
                  <div className="flex flex-col items-center gap-2 text-muted-foreground">
                    <Wallet className="w-8 h-8 opacity-25" />
                    <p className="text-sm">暂无算力流水</p>
                    <p className="text-xs text-muted-foreground/60">完成一次诊断、写作或发布后，这里会出现记录</p>
                  </div>
                </td></tr>
              )}
              {txList.map((tx, i) => (
                <tr key={i} className="border-b">
                  <td className="p-3 text-xs whitespace-nowrap">{tx.created_at ? new Date(tx.created_at).toLocaleString() : '-'}</td>
                  <td className="p-3 break-words">{tx.description || '-'}</td>
                  <td className="p-3"><Badge variant="outline" className="whitespace-nowrap">{transactionDisplay(tx.type, tx.pool, 'customer')}</Badge></td>
                  <td className={`p-3 text-right font-mono ${tx.points > 0 ? 'text-green-600' : 'text-red-600'}`}>
                    {tx.points > 0 ? '+' : ''}{tx.points}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
          {/* [3E] 查看全部流水(含失败退费 / 长任务释放退回)· 分页加载 */}
          {(txList.length > 0 || expanded) && (
            <div className="flex justify-center p-3 border-t">
              {!expanded ? (
                <Button variant="ghost" size="sm" onClick={() => loadMoreTx(true)} disabled={txLoading}>
                  {txLoading ? '加载中…' : '查看全部流水'}
                </Button>
              ) : moreTx.length < txTotal ? (
                <Button variant="ghost" size="sm" onClick={() => loadMoreTx(false)} disabled={txLoading}>
                  {txLoading ? '加载中…' : `加载更多(${moreTx.length}/${txTotal})`}
                </Button>
              ) : (
                <span className="text-xs text-muted-foreground">已显示全部 {txTotal} 条</span>
              )}
            </div>
          )}
        </CardContent>
      </Card>}
    </div>
  );
}

function PoolCard({ label, value, desc, warn }: { label: string; value: number; desc: string; warn?: boolean }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm text-muted-foreground">{label}</CardTitle>
      </CardHeader>
      <CardContent>
        <div className={`text-2xl font-bold ${warn ? 'text-amber-600' : ''}`}>
          {value.toLocaleString()}
        </div>
        <p className="text-xs text-muted-foreground mt-1">{desc}</p>
      </CardContent>
    </Card>
  );
}
