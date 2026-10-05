/**
 * 积分钱包页
 * 展示双钱包余额、交易记录
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useIsMounted } from '@/hooks/useIsMounted';
import { useEmbeddedNavigate } from '@/hooks/useEmbeddedNavigate';
import { Wallet, Gift, ArrowUpRight, ArrowDownRight, RotateCcw, Coins, TrendingUp, Loader2, Hourglass, Clock, CreditCard, ChevronDown } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { cn } from '@/lib/utils';
import { useFeatureCost } from '@/context/PricingContext';
import { useWallet } from '@/context/WalletContext';
import { useAuth } from '@/context/AuthContext';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import WithdrawalDialog from './WithdrawalDialog';
import WithdrawalKycDialog from './WithdrawalKycDialog';
import WithdrawalHistory from './WithdrawalHistory';
// V3.3.1 服务费卡片(组件内部自检 L2 + 总开关 · 非 L2/未启用时不渲染)
import { ServiceFeeWalletCard } from '@/components/wallet/ServiceFeeWalletCard';
import { HelpHint } from '@/components/onboarding/HelpHint';
import { WalletStatusNotice } from '@/components/wallet/WalletStatusNotice';
import { useOnlinePurchaseGate } from '@/hooks/useOnlinePurchaseGate';

// ========== 类型定义 ==========

interface Transaction {
  id: string;
  created_at: string;
  type: string;
  feature_name: string | null;
  amount: number;
  balance_after: number;
  brand_name?: string | null;   // WJ-25 流水来源:后端 LEFT JOIN brands 取客户名
  description?: string | null;  // WJ-25 流水来源:point_transactions.description
  freeze_status?: string | null;
}

interface TransactionPage {
  items: Transaction[];
  total: number;
  page: number;
  limit: number;
}

const TYPE_CONFIG: Record<string, { label: string; className: string; icon: typeof ArrowUpRight }> = {
  recharge:              { label: '充值',     className: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/20', icon: ArrowUpRight },
  consume:               { label: '消费',     className: 'bg-red-500/15 text-red-400 border-red-500/20',             icon: ArrowDownRight },
  refund:                { label: '退款',     className: 'bg-blue-500/15 text-blue-400 border-blue-500/20',          icon: RotateCcw },
  commission:            { label: '服务收益', className: 'bg-purple-500/15 text-purple-400 border-purple-500/20',    icon: Coins },
  commission_settlement: { label: '收益入账', className: 'bg-purple-500/15 text-purple-400 border-purple-500/20',    icon: Coins },
  bonus:                 { label: '赠送',     className: 'bg-amber-500/15 text-amber-400 border-amber-500/20',       icon: Gift },
  freeze:                { label: '先占用',     className: 'bg-amber-500/15 text-amber-300 border-amber-500/20',       icon: Hourglass },
  release:               { label: '占用退回',     className: 'bg-sky-500/15 text-sky-400 border-sky-500/20',             icon: RotateCcw },
  withdrawal_freeze:     { label: '提现处理中', className: 'bg-amber-500/15 text-amber-300 border-amber-500/20',       icon: CreditCard },
  withdrawal_reject:     { label: '提现退回', className: 'bg-blue-500/15 text-blue-400 border-blue-500/20',          icon: RotateCcw },
  withdrawal_paid:       { label: '提现打款', className: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/20', icon: CreditCard },
  legacy_commission_migration: { label: '收益迁移', className: 'bg-purple-500/15 text-purple-400 border-purple-500/20', icon: Coins },
  // [BUG-2] 充值转入可用额度 · 中性色(既不是收入也不是支出 · 金额后端已归 0 显示 '--')
  v35_migrated_to_customer_credit: { label: '转入可用算力', className: 'bg-sky-500/15 text-sky-400 border-sky-500/20', icon: ArrowUpRight },
};

// [BUG-2 2026-07-27] 迁移流水不再对客户整条隐藏。
// 后端 _decorate_wallet_transaction_for_display 已把它翻成"转入可用算力"的人话(金额记 0),
// 前端再藏一层会让客户对不上账 —— 那正是这次投诉"钱少了"的成因。
// 仍隐藏的只剩 committed 的 freeze(与 consume 成对出现,显示会造成双扣视觉)。
const isCustomerVisibleTransaction = (tx: Transaction) => (
  !(tx.type === 'freeze' && tx.freeze_status === 'committed')
);

const EXCHANGE_RATE = 130;

// ========== 主组件 ==========

export default function WalletPage() {
  const navigate = useEmbeddedNavigate();
  const [searchParams] = useSearchParams();
  const { paidPoints, commissionPoints, bonusPoints, frozenPoints, totalPoints, cnyEquivalent, agentLevel, deductionPreference, loading: walletLoading, status: walletStatus, errorMessage: walletError, lastUpdatedAt: walletLastUpdatedAt, refreshBalance, chargeNotifyLevel, setChargeNotifyLevel, paidPointsDisplayed, bonusPointsDisplayed, customerCredit } = useWallet();
  // V3.5 客户授权额度池(2026-06-08 P0)· 顶部"充值算力 / 赠送算力"必须合并 customer_credit
  // 否则客户充值进 customer_agent_credit_wallets 后看顶部=0 反馈"没到账"
  const hasCustomerCredit = (customerCredit.toolCreditPoints + customerCredit.publishCreditPoints + customerCredit.bonusCreditPoints) > 0;
  const isAgent = agentLevel === 'L1' || agentLevel === 'L2';
  const { user: authUser } = useAuth();
  // [r9] 人民币折算仅 admin 财务后台可见 · 普通用户和服务商前台都只看工具算力
  const showYuan = authUser?.is_admin === true;
  const requestedTab = searchParams.get('tab');
  const defaultWalletTab = requestedTab === 'withdrawals' && isAgent
    ? 'withdrawals'
    : 'transactions';
  const [savingPref, setSavingPref] = useState(false);
  const [freezeDetail, setFreezeDetail] = useState<any>(null);

  // [P5b] 扣费提醒设置(全员可用,非代理也能选)
  const [savingNotify, setSavingNotify] = useState(false);
  const effectiveNotifyLevel = chargeNotifyLevel ?? 'each'; // 未设置时 UI 默认高亮"每次都提醒"
  const handleNotifyLevelChange = async (next: 'each' | 'quiet' | 'off') => {
    if (savingNotify || next === chargeNotifyLevel) return;
    setSavingNotify(true);
    try {
      await setChargeNotifyLevel(next);
    } finally {
      setSavingNotify(false);
    }
  };

  // -- 提现相关状态 --
  const [kycVerified, setKycVerified] = useState<boolean | null>(null);
  const [bankCardCount, setBankCardCount] = useState<number | null>(null);
  const [withdrawalDialogOpen, setWithdrawalDialogOpen] = useState(false);
  const [kycDialogOpen, setKycDialogOpen] = useState(false);
  const [commissionExpanded, setCommissionExpanded] = useState(false);
  const [withdrawalExpanded, setWithdrawalExpanded] = useState(false);

  // 2026-04-18 v3.4 扣费偏好切换（仅代理）
  const handlePreferenceChange = async (next: 'default' | 'agent_friendly') => {
    if (!isAgent || next === deductionPreference || savingPref) return;
    setSavingPref(true);
    try {
      const res = await authFetch('/api/wallet/preference', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ deduction_preference: next }),
      });
      if (res.ok) {
        await refreshBalance();
      }
    } finally {
      setSavingPref(false);
    }
  };

  // A.9-B (CTO-15.9 session 3 · 2026-04-25 · 老板拍板"默认关 · 代理同意才扣")
  // 自动 monitor 24h after publish 开关 · 仅代理可切
  const [autoMonitorEnabled, setAutoMonitorEnabled] = useState(false);
  // 🔴 [#64 P0 2026-09-05] 只显示**算力**,不显示人民币。本文件 r9 那条注释写着:
  //    「人民币折算仅 admin 财务后台可见 · 普通用户和服务商前台都只看工具算力」。
  //    原文案写死了一个人民币单价 —— 那是平台**进货成本原值**(4 引擎全量单次),属内部参数,禁泄。
  //    价从 feature_pricing 动态取;取不到就**一个数字都不显示**(宁可少说,不可说错)。
  //    code 取 `scheduled_monitoring`:自动监测走 batch_monitor.run_client_monitoring(:1031),
  //    与手动流式的 `monitor_single` 不是同一个 code。
  const autoMonitorPoints = useFeatureCost('scheduled_monitoring');
  const [autoMonitorLoaded, setAutoMonitorLoaded] = useState(false);
  const [savingAutoMonitor, setSavingAutoMonitor] = useState(false);

  useEffect(() => {
    if (!isAgent) return;
    authFetch('/api/wallet/auto-monitor-preference')
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d?.success) setAutoMonitorEnabled(!!d.data?.enabled);
      })
      .catch(() => { /* 静默 */ })
      .finally(() => setAutoMonitorLoaded(true));
  }, [isAgent]);

  const handleAutoMonitorToggle = async (next: boolean) => {
    if (!isAgent || savingAutoMonitor || next === autoMonitorEnabled) return;
    setSavingAutoMonitor(true);
    try {
      const res = await authFetch('/api/wallet/auto-monitor-preference', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ auto_monitor_after_publish: next }),
      });
      if (res.ok) {
        setAutoMonitorEnabled(next);
      }
    } finally {
      setSavingAutoMonitor(false);
    }
  };

  const TX_LIMIT = 20;
  const isMounted = useIsMounted();

  // -- 提现前置数据（代理） --
  const fetchWithdrawalPrereqs = useCallback(async () => {
    if (!isAgent) return;
    try {
      const [kycRes, cardsRes] = await Promise.all([
        authFetch('/api/wallet/withdrawal-kyc'),
        authFetch('/api/wallet/bank-cards'),
      ]);
      if (!isMounted()) return;
      if (kycRes.ok) {
        const kycData = await kycRes.json();
        setKycVerified(kycData.verified ?? true);
      } else {
        setKycVerified(false);
      }
      if (cardsRes.ok) {
        const cardsData = await cardsRes.json();
        const list = cardsData.cards ?? cardsData ?? [];
        setBankCardCount(list.length);
      } else {
        setBankCardCount(0);
      }
    } catch {
      // 静默：按钮退化到禁用状态
    }
  }, [isAgent, isMounted]);

  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [txPage, setTxPage] = useState(1);
  const [txTotal, setTxTotal] = useState(0);
  const [txLoading, setTxLoading] = useState(false);
  const [txStatus, setTxStatus] = useState<'initial' | 'loading' | 'ready' | 'stale' | 'error'>('initial');
  const [txError, setTxError] = useState<string | null>(null);
  const [txLastUpdatedAt, setTxLastUpdatedAt] = useState<string | null>(null);
  const txLastUpdatedAtRef = useRef<string | null>(null);
  const txRequestSequence = useRef(0);

  const fetchTransactions = useCallback(async (page: number) => {
    const requestSequence = ++txRequestSequence.current;
    setTxLoading(true);
    setTxStatus('loading');
    setTxError(null);
    try {
      const res = await authFetch(`/api/wallet/transactions?page=${page}&limit=${TX_LIMIT}`);
      if (!isMounted() || requestSequence !== txRequestSequence.current) return;
      if (!res.ok) {
        if (res.status === 401) throw new Error('登录状态已过期，请重新登录后查看交易记录');
        if (res.status === 403) throw new Error('当前账号没有查看交易记录的权限，请联系管理员');
        if (res.status === 429) throw new Error('交易记录查询太频繁，请稍后再试');
        throw new Error(res.status >= 500 ? '交易记录服务暂时不可用，请稍后重试' : '暂时无法读取交易记录，请重试');
      }
      const data: TransactionPage = await res.json().catch(() => { throw new Error('交易记录响应无法读取，请重试'); });
      if (!Array.isArray(data?.items) || typeof data.total !== 'number') {
        throw new Error('交易记录响应不完整，请重试');
      }
      if (!isMounted() || requestSequence !== txRequestSequence.current) return;
      setTransactions(data.items);
      setTxTotal(data.total);
      setTxPage(typeof data.page === 'number' ? data.page : page);
      setTxStatus('ready');
      const updatedAt = new Date().toISOString();
      txLastUpdatedAtRef.current = updatedAt;
      setTxLastUpdatedAt(updatedAt);
    } catch (error) {
      if (!isMounted() || requestSequence !== txRequestSequence.current) return;
      setTxStatus(txLastUpdatedAtRef.current ? 'stale' : 'error');
      setTxError(error instanceof Error ? error.message : '网络连接异常，请检查网络后重试交易记录');
    } finally {
      if (isMounted() && requestSequence === txRequestSequence.current) setTxLoading(false);
    }
  }, [isMounted]);

  useEffect(() => {
    fetchTransactions(1);
  }, [fetchTransactions]);

  useEffect(() => {
    fetchWithdrawalPrereqs();
  }, [fetchWithdrawalPrereqs]);

  // 冻结明细（代理专属）
  useEffect(() => {
    if (!isAgent) return;
    authFetch('/api/wallet/freeze-detail')
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (isMounted() && data) setFreezeDetail(data); })
      .catch(() => {});
  }, [isAgent, isMounted]);

  const totalPages = Math.max(1, Math.ceil(txTotal / TX_LIMIT));

  const agentLevelLabel: Record<string, string> = {
    free: '普通用户',
    paid: '普通用户',
    L1: '服务方',
    L2: '服务方',
  };

  // [客户线上购买门控 2026-07-29] 被禁客户点"充值"改为弹提示,不跳购买页。
  const { guard: guardOnlinePurchase, gateDialog: onlinePurchaseGateDialog } = useOnlinePurchaseGate();
  const canUseBalance = walletStatus === 'ready';
  const canDisplayBalance = walletStatus === 'ready' || walletStatus === 'stale'
    || (walletStatus === 'loading' && walletLastUpdatedAt !== null);

  return (
    <div className="space-y-6 p-3 sm:p-4 md:p-6 pb-24 lg:pb-6">
      {/* 标题 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-foreground">钱包与交易记录</h1>
          <p className="text-sm text-muted-foreground mt-1">
            这里是账户总账；当前功能可用算力请到“服务算力”查看 · {agentLevelLabel[agentLevel] ?? '普通用户'}
          </p>
        </div>
        <Button
          className="bg-foreground text-background hover:bg-foreground/90"
          size="lg"
          /* 充值入口统一走新版 BuyCredit(/customer/recharge):
             · 价格与服务主体均由后端平台配置解析
             · 无商业配置的老用户走 PLATFORM_DIRECT
             · 支付已统一:微信内 JSAPI / PC Native / 手机外部 xunhupay(后端统一 UA)
             老 /wallet/recharge 路由保留兼容老二维码/分享链接 · 内部有废弃 banner + handleRecharge 强引导 */
          title={!canUseBalance ? '余额读取失败不影响进入购买算力' : undefined}
          onClick={() => guardOnlinePurchase(() => navigate('/customer/recharge'))}
        >
          充值
        </Button>
      </div>

      <WalletStatusNotice
        status={walletStatus}
        errorMessage={walletError}
        lastUpdatedAt={walletLastUpdatedAt}
        onRetry={refreshBalance}
      />

      {walletStatus === 'loading' && !walletLastUpdatedAt && (
        <div className="flex items-center gap-2 rounded-lg border border-border bg-card px-4 py-5 text-sm text-muted-foreground" role="status">
          <Loader2 className="size-5 animate-spin" /> 正在读取余额…
        </div>
      )}

      {/* === 主余额卡（v3.4 折叠式：合计在上，三轨明细在下，仅有冻结才显示使用中） === */}
      {canDisplayBalance && <Card>
        <CardContent className="py-5">
          <div className="flex flex-col gap-4">
            {/* 顶栏：可用合计 */}
            <div className="flex items-baseline gap-3">
              <span className="text-sm text-muted-foreground">可用合计</span>
              <span className="text-3xl font-bold text-foreground">
                {walletLoading ? <Loader2 className="size-5 animate-spin inline" /> : totalPoints.toLocaleString()}
              </span>
              {showYuan && <span className="text-xs text-muted-foreground">约 ¥{cnyEquivalent}(仅管理员可见)</span>}
            </div>

            {/* 三轨明细 */}
            <div className="grid grid-cols-3 gap-3">
              <div className="rounded-lg bg-muted/40 p-3">
                <div className="flex items-center gap-1.5 text-xs text-muted-foreground mb-1">
                  <Wallet className="size-3.5" />
                  充值算力
                </div>
                <div className="text-lg font-semibold text-foreground">
                  {walletLoading ? '…' : paidPointsDisplayed.toLocaleString()}
                </div>
                <p className="text-[10px] text-muted-foreground mt-0.5 leading-tight">
                  {/* [V3.5 v8 P0 2026-06-08 老板拍板]充值算力可换所有功能(含发布)·赠送算力只可工具 */}
                  {/* publish 子池优先用于发布(代理可单独配额控制)· tool 子池可兜底发布 */}
                  真金白银 / 打折充入 · 可换所有功能(含发布)
                  {hasCustomerCredit && (
                    <>
                      <br />
                      <span className="text-blue-400">
                        含当前功能可用算力 {(customerCredit.toolCreditPoints + customerCredit.publishCreditPoints).toLocaleString()}
                        {customerCredit.publishCreditPoints > 0 && (
                          <> · 其中发布专用 {customerCredit.publishCreditPoints.toLocaleString()}(优先用于发布)</>
                        )}
                      </span>
                    </>
                  )}
                </p>
              </div>

              {(isAgent || commissionPoints > 0) && (
              <div className="rounded-lg bg-emerald-500/5 border border-emerald-500/20 p-3">
                <div className="flex items-center gap-1.5 text-xs text-emerald-400 mb-1">
                  <Coins className="size-3.5" />
                  服务收益
                </div>
                <div className="text-lg font-semibold text-emerald-300">
                  {walletLoading ? '…' : commissionPoints.toLocaleString()}
                </div>
                <p className="text-[10px] text-muted-foreground mt-0.5 leading-tight">
                  推荐 / 成交赚的 · 所有功能都能用 · 达到提现门槛后可提现到银行卡
                </p>
                {/* 提现按钮（代理专属，内嵌佣金卡片） */}
                {isAgent && kycVerified !== null && bankCardCount !== null && (
                  <div className="mt-2 pt-2 border-t border-emerald-500/10">
                    {!kycVerified ? (
                      <button
                        disabled={!canUseBalance}
                        onClick={() => { if (canUseBalance) setKycDialogOpen(true); }}
                        className="min-h-11 px-2.5 py-1 text-[11px] rounded-md border border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/10 transition-colors disabled:cursor-not-allowed disabled:opacity-50 sm:min-h-0"
                      >
                        实名认证后提现
                      </button>
                    ) : bankCardCount === 0 ? (
                      <button
                        disabled={!canUseBalance}
                        onClick={() => { if (canUseBalance) navigate('/wallet/bank-cards'); }}
                        className="min-h-11 px-2.5 py-1 text-[11px] rounded-md border border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/10 transition-colors disabled:cursor-not-allowed disabled:opacity-50 sm:min-h-0"
                      >
                        绑定银行卡后提现
                      </button>
                    ) : commissionPoints >= 13000 ? (
                      <button
                        disabled={!canUseBalance}
                        onClick={() => { if (canUseBalance) setWithdrawalDialogOpen(true); }}
                        className="min-h-11 px-2.5 py-1 text-[11px] rounded-md bg-emerald-500/20 border border-emerald-500/30 text-emerald-300 hover:bg-emerald-500/30 transition-colors font-medium disabled:cursor-not-allowed disabled:opacity-50 sm:min-h-0"
                      >
                        提现
                      </button>
                    ) : (
                      <span className="text-[11px] text-muted-foreground">
                        收益不足，暂不可提现
                      </span>
                    )}
                  </div>
                )}
              </div>
              )}

              <div className="rounded-lg bg-muted/40 p-3">
                <div className="flex items-center gap-1.5 text-xs text-muted-foreground mb-1">
                  <Gift className="size-3.5" />
                  赠送算力
                  <HelpHint title="赠送算力能干嘛?为啥有时用不了?">
                    算力分三种:<b>充值算力</b>(你付钱买的)、<b>服务收益</b>(推荐 / 成交赚的)、<b>赠送算力</b>(注册或活动白送的)。
                    <br />赠送算力<b>只能用于体验类功能</b>(如品牌诊断), <b>媒体发布等消耗操作不认赠送算力</b>, 只扣充值 / 服务收益。
                    <br />所以有时显示有赠送算力、发布却提示算力不足 —— 去充值即可。
                    <br />另外:服务收益如果选择转成赠送算力, <b>系统会多给 20%</b>(本金走服务收益可提现, 多送的 20% 进赠送算力仅限消费)。
                  </HelpHint>
                </div>
                <div className="text-lg font-semibold text-foreground">
                  {walletLoading ? '…' : bonusPointsDisplayed.toLocaleString()}
                </div>
                <p className="text-[10px] text-muted-foreground mt-0.5 leading-tight">
                  注册 / 活动白送的 · 仅限体验类功能 · 媒体发布不能用 · 不可退现
                  {hasCustomerCredit && customerCredit.bonusCreditPoints > 0 && (
                    <>
                      <br />
                      <span className="text-blue-400">含当前功能赠送算力 {customerCredit.bonusCreditPoints.toLocaleString()}</span>
                    </>
                  )}
                </p>
              </div>
            </div>

            {/* 🔴 [包三 · 钱包] 原来这块挂 `!isAgent` —— 恰好把「有多少算力正被占用」
                对**主用户(服务方)**藏起来了(FH-032:冻结 650 两个钱包页都不显示)。
                她看到的可用余额比实际少,却找不到少的那部分去哪了。
                算力被占用这件事与她是不是服务方无关,所有人都该看见。 */}
            {frozenPoints > 0 && (
              <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 flex items-center gap-2">
                <Hourglass className="size-4 text-amber-400 shrink-0" />
                <span className="text-sm font-medium text-amber-400">
                  {frozenPoints.toLocaleString()} 使用中
                </span>
                <span className="text-xs text-muted-foreground">
                  · 任务在跑,先占用这些算力;跑失败会退回来
                </span>
              </div>
            )}

          </div>
        </CardContent>
      </Card>}

      {/* 服务收益待入账（可折叠，默认收起） */}
      {isAgent && freezeDetail?.pending_commissions?.length > 0 && (
        <Card>
          <button
            onClick={() => setCommissionExpanded(!commissionExpanded)}
            className="w-full px-6 py-4 flex items-center justify-between text-left"
          >
            <div className="flex items-center gap-2">
              <Clock className="size-4 text-cyan-400" />
              <span className="text-base font-semibold text-cyan-400">服务收益待入账</span>
              <Badge className="text-[10px] bg-cyan-500/15 text-cyan-400 border-cyan-500/20">
                {freezeDetail.pending_commissions.length} 笔
              </Badge>
              <span className="text-xs text-muted-foreground ml-1">
                合计 ¥{freezeDetail.pending_commissions.reduce((s: number, c: any) => s + c.amount_yuan, 0).toFixed(2)}
              </span>
            </div>
            <ChevronDown className={cn("size-4 text-muted-foreground transition-transform", commissionExpanded && "rotate-180")} />
          </button>
          {commissionExpanded && (
            <CardContent className="pt-0 space-y-3">
              <p className="text-xs text-muted-foreground pb-2">下级充值后 3 天结算(先留 3 天观察期,确认不退款再自动入账到服务收益)</p>
              {freezeDetail.pending_commissions.map((pc: any, i: number) => (
                <div key={i} className="flex items-start justify-between gap-3 pb-3 border-b border-border/30 last:border-0 last:pb-0">
                  <div className="space-y-1">
                    <div className="text-sm text-foreground">
                      来自 <span className="font-medium">{pc.source_display}</span> 的充值
                      <Badge className="ml-2 text-[10px] bg-cyan-500/15 text-cyan-400 border-cyan-500/20">
                        {pc.level === 1 ? '直推收益' : '间推收益'}
                      </Badge>
                    </div>
                    {pc.available_at && (
                      <div className="text-xs text-muted-foreground">
                        预计 {formatTime(pc.available_at)} 解冻 · 还剩 {formatCountdown(pc.available_at)}
                      </div>
                    )}
                  </div>
                  <div className="text-right shrink-0">
                    <div className="text-sm font-semibold text-cyan-400">+¥{pc.amount_yuan}</div>
                    <div className="text-[10px] text-muted-foreground">{pc.points.toLocaleString()} 算力</div>
                  </div>
                </div>
              ))}
            </CardContent>
          )}
        </Card>
      )}

      {/* 提现进度追踪（可折叠，默认收起） */}
      {isAgent && freezeDetail?.pending_withdrawals?.length > 0 && (
        <Card>
          <button
            onClick={() => setWithdrawalExpanded(!withdrawalExpanded)}
            className="w-full px-6 py-4 flex items-center justify-between text-left"
          >
            <div className="flex items-center gap-2">
              <CreditCard className="size-4 text-amber-400" />
              <span className="text-base font-semibold text-amber-400">提现进度</span>
              <Badge className="text-[10px] bg-amber-500/15 text-amber-400 border-amber-500/20">
                {freezeDetail.pending_withdrawals.length} 笔处理中
              </Badge>
              <span className="text-xs text-muted-foreground ml-1">
                合计 ¥{freezeDetail.pending_withdrawals.reduce((s: number, w: any) => s + w.amount_yuan, 0).toFixed(2)}
              </span>
            </div>
            <ChevronDown className={cn("size-4 text-muted-foreground transition-transform", withdrawalExpanded && "rotate-180")} />
          </button>
          {withdrawalExpanded && (
          <CardContent className="pt-0 space-y-4">
            {freezeDetail.pending_withdrawals.map((w: any) => (
              <div key={w.id} className="rounded-lg border border-border/40 bg-muted/20 p-4 space-y-3">
                {/* 提现概要 */}
                <div className="flex items-center justify-between">
                  <div>
                    <span className="text-lg font-semibold text-foreground">¥{w.amount_yuan}</span>
                    <span className="text-xs text-muted-foreground ml-2">{w.bank_name} {w.card_mask}</span>
                  </div>
                  <Badge className={cn(
                    'text-xs',
                    w.status === 'pending'
                      ? 'bg-amber-500/15 text-amber-400 border-amber-500/20'
                      : 'bg-blue-500/15 text-blue-400 border-blue-500/20',
                  )}>
                    {w.status === 'pending' ? '待审核' : '已批准 · 待打款'}
                  </Badge>
                </div>
                <div className="text-xs text-muted-foreground">
                  手续费 ¥{w.fee_yuan} · 实际到账 ¥{w.actual_yuan} · 先占用 {w.points_deducted?.toLocaleString()} 算力
                </div>

                {/* 时间轴进度 */}
                <div className="relative pl-5 space-y-0">
                  {/* Step 1: 提交申请 — 已完成 */}
                  <div className="relative pb-4">
                    <div className="absolute left-[-14px] top-[2px] size-3 rounded-full bg-emerald-500 ring-2 ring-emerald-500/20" />
                    <div className="absolute left-[-9px] top-[14px] w-0.5 h-full bg-border/60" />
                    <div className="text-xs">
                      <span className="text-emerald-400 font-medium">提交申请</span>
                      <span className="text-muted-foreground ml-2">{w.created_at ? formatTime(w.created_at) : ''}</span>
                    </div>
                    <div className="text-[11px] text-muted-foreground">这笔收益已提交提现,等审核通过后打款</div>
                  </div>

                  {/* Step 2: 审核 */}
                  <div className="relative pb-4">
                    <div className={cn(
                      "absolute left-[-14px] top-[2px] size-3 rounded-full ring-2",
                      w.status === 'approved'
                        ? "bg-emerald-500 ring-emerald-500/20"
                        : "bg-amber-500 ring-amber-500/20 animate-pulse"
                    )} />
                    <div className="absolute left-[-9px] top-[14px] w-0.5 h-full bg-border/60" />
                    <div className="text-xs">
                      <span className={w.status === 'approved' ? 'text-emerald-400 font-medium' : 'text-amber-400 font-medium'}>
                        {w.status === 'approved' ? '审核通过' : '审核中'}
                      </span>
                    </div>
                    <div className="text-[11px] text-muted-foreground">
                      {w.status === 'approved' ? '管理员已批准，等待打款' : '管理员将在 1-3 个工作日内审核'}
                    </div>
                  </div>

                  {/* Step 3: 打款 — 未到 */}
                  <div className="relative">
                    <div className={cn(
                      "absolute left-[-14px] top-[2px] size-3 rounded-full ring-2",
                      "bg-muted ring-border/40"
                    )} />
                    <div className="text-xs">
                      <span className="text-muted-foreground">打款到银行卡</span>
                    </div>
                    <div className="text-[11px] text-muted-foreground">审核通过后，款项将转入您的银行账户</div>
                  </div>
                </div>
              </div>
            ))}
          </CardContent>
          )}
        </Card>
      )}

      {/* [P5b] 扣费提醒设置（全员可用） */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">扣费提醒设置</CardTitle>
          <p className="text-xs text-muted-foreground">花算力时怎么提醒你 · 每一笔都会记到账单，随时能查</p>
        </CardHeader>
        <CardContent className="space-y-2">
          {([
            { value: 'each' as const, label: '每次都提醒', desc: '每花一笔都弹一下提示' },
            { value: 'quiet' as const, label: '安静一点', desc: '不弹提示 · 只在通知铃铛亮个小点' },
            { value: 'off' as const, label: '不主动提醒', desc: '完全安静 · 想看随时来这页查账单' },
          ]).map((opt) => (
            <label
              key={opt.value}
              className={cn(
                'flex items-start gap-3 rounded-lg border p-3 cursor-pointer transition-colors',
                effectiveNotifyLevel === opt.value
                  ? 'border-foreground bg-muted/30'
                  : 'border-border hover:bg-muted/20',
                savingNotify && 'opacity-60 pointer-events-none',
              )}
            >
              <input
                type="radio"
                name="charge-notify-level"
                value={opt.value}
                checked={effectiveNotifyLevel === opt.value}
                onChange={() => handleNotifyLevelChange(opt.value)}
                className="mt-1"
              />
              <div className="flex-1">
                <div className="text-sm font-medium text-foreground">{opt.label}</div>
                <div className="text-xs text-muted-foreground mt-1">{opt.desc}</div>
              </div>
            </label>
          ))}
        </CardContent>
      </Card>

      {/* 扣费偏好（L1+ 可见） */}
      {isAgent && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">扣费偏好</CardTitle>
            <p className="text-xs text-muted-foreground">AI 功能和媒体发布扣算力的顺序，不影响总消耗</p>
          </CardHeader>
          <CardContent className="space-y-2">
            <label className={cn(
              "flex items-start gap-3 rounded-lg border p-3 cursor-pointer transition-colors",
              deductionPreference === 'default'
                ? 'border-foreground bg-muted/30'
                : 'border-border hover:bg-muted/20',
              savingPref && 'opacity-60 pointer-events-none',
            )}>
              <input
                type="radio"
                name="deduction-pref"
                value="default"
                checked={deductionPreference === 'default'}
                onChange={() => handlePreferenceChange('default')}
                className="mt-1"
              />
              <div className="flex-1">
                <div className="text-sm font-medium text-foreground">
                  先扣赠送 → 服务收益 → 充值 <span className="text-xs text-muted-foreground">（默认 · 充值的钱最后扣）</span>
                </div>
                <div className="text-xs text-muted-foreground mt-1">
                  充值的钱留到最后才扣 · 先把赠送和服务收益用掉更划算
                </div>
              </div>
            </label>

            <label className={cn(
              "flex items-start gap-3 rounded-lg border p-3 cursor-pointer transition-colors",
              deductionPreference === 'agent_friendly'
                ? 'border-foreground bg-muted/30'
                : 'border-border hover:bg-muted/20',
              savingPref && 'opacity-60 pointer-events-none',
            )}>
              <input
                type="radio"
                name="deduction-pref"
                value="agent_friendly"
                checked={deductionPreference === 'agent_friendly'}
                onChange={() => handlePreferenceChange('agent_friendly')}
                className="mt-1"
              />
              <div className="flex-1">
                <div className="text-sm font-medium text-foreground">
                  先扣赠送 → 充值 → 服务收益 <span className="text-xs text-emerald-400">（服务方友好 · 留收益提现权）</span>
                </div>
                <div className="text-xs text-muted-foreground mt-1">
                  服务收益最后才扣 · 保留把收益提现到银行卡的权利
                </div>
              </div>
            </label>
          </CardContent>
        </Card>
      )}

      {/* A.9-B (CTO-15.9 session 3 · 2026-04-25 · 老板拍板) 自动监测 24h 开关 · 仅代理 */}
      {isAgent && autoMonitorLoaded && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">自动监测(发布 24h 后)</CardTitle>
            <p className="text-xs text-muted-foreground">
              {autoMonitorPoints != null
                ? `开启后 · 文章首次发布 24 小时后自动跑 1 次 4 个 AI 平台的监测(${autoMonitorPoints.toLocaleString()} 算力 · 完成才扣)· 不影响手动监测`
                : '开启后 · 文章首次发布 24 小时后自动跑 1 次 4 个 AI 平台的监测(完成才扣算力)· 不影响手动监测'}
            </p>
          </CardHeader>
          <CardContent>
            <label className={cn(
              "flex items-start gap-3 rounded-lg border p-3 cursor-pointer transition-colors",
              autoMonitorEnabled
                ? 'border-foreground bg-emerald-500/10'
                : 'border-border hover:bg-muted/20',
              savingAutoMonitor && 'opacity-60 pointer-events-none',
            )}>
              <input
                type="checkbox"
                checked={autoMonitorEnabled}
                onChange={(e) => handleAutoMonitorToggle(e.target.checked)}
                className="mt-1 size-4"
              />
              <div className="flex-1">
                <div className="text-sm font-medium text-foreground">
                  {autoMonitorEnabled ? '已开启 · 自动监测进行中' : '未开启(默认)'}
                </div>
                <div className="text-xs text-muted-foreground mt-1">
                  {autoMonitorEnabled
                    ? `所有名下品牌的新发布文章 · 24h 后系统会自动跑监测 · 看 AI 是否引用 · ${autoMonitorPoints != null ? `每次扣 ${autoMonitorPoints.toLocaleString()} 算力` : '完成才扣算力'}`
                    : '默认关闭 · 需手动在监测页点"立即监测"按钮 · 开关后系统才自动扣费'}
                </div>
              </div>
            </label>
          </CardContent>
        </Card>
      )}

      {/* 交易记录 / 提现记录 */}
      <Card>
        <Tabs defaultValue={defaultWalletTab}>
          <CardHeader>
            <TabsList>
              <TabsTrigger value="transactions">交易记录</TabsTrigger>
              {isAgent && <TabsTrigger value="withdrawals">提现记录</TabsTrigger>}
            </TabsList>
          </CardHeader>
          <CardContent>
            <TabsContent value="transactions" className="mt-0">
              {txStatus === 'stale' && (
                <div className="mb-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-muted-foreground">
                  交易记录可能已过期，当前保留上次成功结果。{txError}
                  <Button type="button" variant="outline" size="sm" className="ml-3" onClick={() => void fetchTransactions(txPage)}>重试</Button>
                </div>
              )}
              {txStatus === 'error' ? (
                <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm">
                  <p className="font-medium text-foreground">交易记录暂时无法读取</p>
                  <p className="mt-1 text-muted-foreground">{txError}</p>
                  <Button type="button" variant="outline" size="sm" className="mt-3" onClick={() => void fetchTransactions(txPage)}>
                    重新读取交易记录
                  </Button>
                </div>
              ) : txLoading && txLastUpdatedAt === null ? (
                <div className="flex items-center justify-center py-12">
                  <Loader2 className="size-6 animate-spin text-muted-foreground" />
                </div>
              ) : transactions.length === 0 ? (
                <div className="text-center py-12 text-muted-foreground text-sm">
                  {txStatus === 'stale' ? '上次成功读取时没有交易记录' : '暂无交易记录'}
                </div>
              ) : (
                <>
                  {/* 桌面端表格 */}
                  <div className="hidden sm:block overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-border text-muted-foreground">
                          <th className="text-left py-3 px-2 font-medium">时间</th>
                          <th className="text-left py-3 px-2 font-medium">类型</th>
                          <th className="text-left py-3 px-2 font-medium">功能</th>
                          <th className="text-right py-3 px-2 font-medium">算力变动</th>
                          <th className="text-right py-3 px-2 font-medium">余额</th>
                        </tr>
                      </thead>
                      <tbody>
                        {transactions
                          // [P1-9 fix 2026-05-23] 隐藏已 committed 的 freeze 流水 · 防双扣视觉(余额对但用户看两条 -650 像被扣两次)
                          // V3.5 内部迁移后端已隐藏,这里保留兜底防旧缓存/异常响应露出负数内部流水
                          .filter(isCustomerVisibleTransaction)
                          .map((tx) => {
                          // [CTO-15.4 2026-04-20 P2-1] 原 fallback consume 导致"测试账号充值 +96,112" 显示红色消费误标
                          // 按 amount 正负兜底判定:未知 type + 正金额 → recharge(绿) · 负金额 → consume(红)
                          const rawConfig = TYPE_CONFIG[tx.type];
                          const config = rawConfig ?? (tx.amount > 0 ? TYPE_CONFIG.recharge : TYPE_CONFIG.consume);
                          const Icon = config.icon;
                          return (
                            <tr key={tx.id} className="border-b border-border/50 hover:bg-muted/30 transition-colors">
                              <td className="py-3 px-2 text-muted-foreground">
                                {formatTime(tx.created_at)}
                              </td>
                              <td className="py-3 px-2">
                                <Badge className={cn('gap-1', config.className)}>
                                  <Icon className="size-3" />
                                  {config.label}
                                </Badge>
                              </td>
                              <td className="py-3 px-2 text-foreground">
                                {(() => {
                                  // [WJ-25 2026-05-31] 流水补来源:品牌名 · 功能 · 摘要 · 优雅降级
                                  // feature_name 后端取自 description(常与 description 相同)· 去重避免重复展示同一句
                                  const brand = (tx.brand_name || '').trim();
                                  const rawFeat = (tx.feature_name || '').trim();
                                  const rawDesc = (tx.description || '').trim();
                                  const feat = rawFeat;
                                  const desc = rawDesc;
                                  const parts: string[] = [];
                                  if (brand) parts.push(brand);
                                  if (feat) parts.push(feat);
                                  if (desc && desc !== feat) parts.push(desc);
                                  return parts.length > 0 ? (
                                    <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
                                      {parts.map((p, i) => (
                                        <span key={i} className="inline-flex items-center gap-1.5">
                                          {i > 0 && <span className="text-muted-foreground/50">·</span>}
                                          <span className={i === 0 && brand ? 'font-medium' : 'text-muted-foreground'}>{p}</span>
                                        </span>
                                      ))}
                                    </span>
                                  ) : '--';
                                })()}
                              </td>
                              <td className={cn(
                                'py-3 px-2 text-right font-medium tabular-nums',
                                tx.amount === 0 ? 'text-muted-foreground' : tx.amount > 0 ? 'text-emerald-400' : 'text-red-400'
                              )}>
                                {tx.amount === 0 ? '--' : `${tx.amount > 0 ? '+' : ''}${tx.amount.toLocaleString()}`}
                              </td>
                              <td className="py-3 px-2 text-right text-muted-foreground tabular-nums">
                                {tx.balance_after.toLocaleString()}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>

                  {/* 移动端列表 */}
                  <div className="sm:hidden space-y-3">
                    {transactions
                      // 桌面同款 freeze committed 隐藏(P1-9)
                      .filter(isCustomerVisibleTransaction)
                      .map((tx) => {
                      // [CTO-15.4 2026-04-20] amount 正负兜底 · 防"测试账号充值 +xxx" 误标消费
                      const rawConfig = TYPE_CONFIG[tx.type];
                      const config = rawConfig ?? (tx.amount > 0 ? TYPE_CONFIG.recharge : TYPE_CONFIG.consume);
                      const Icon = config.icon;
                      const rawFeat = (tx.feature_name || '').trim();
                      const rawDesc = (tx.description || '').trim();
                      const feat = rawFeat;
                      const desc = rawDesc;
                      const mainText = feat || desc || config.label;
                      return (
                        <div key={tx.id} className="flex items-center justify-between py-3 border-b border-border/50">
                          <div className="flex items-center gap-3">
                            <div className={cn('size-8 rounded-lg flex items-center justify-center', config.className)}>
                              <Icon className="size-4" />
                            </div>
                            <div className="min-w-0">
                              {/* [WJ-25 2026-05-31] 移动端流水来源:品牌名 + 功能/摘要 · 优雅降级 */}
                              {(tx.brand_name || '').trim() && (
                                <p className="text-xs font-medium text-foreground truncate">
                                  {(tx.brand_name || '').trim()}
                                </p>
                              )}
                              <p className="text-sm font-medium text-foreground truncate">
                                {mainText}
                              </p>
                              <p className="text-xs text-muted-foreground">{formatTime(tx.created_at)}</p>
                            </div>
                          </div>
                          <div className="text-right">
                            <p className={cn(
                              'text-sm font-medium tabular-nums',
                              tx.amount === 0 ? 'text-muted-foreground' : tx.amount > 0 ? 'text-emerald-400' : 'text-red-400'
                            )}>
                              {tx.amount === 0 ? '--' : `${tx.amount > 0 ? '+' : ''}${tx.amount.toLocaleString()}`}
                            </p>
                            <p className="text-xs text-muted-foreground tabular-nums">
                              余额 {tx.balance_after.toLocaleString()}
                            </p>
                          </div>
                        </div>
                      );
                    })}
                  </div>

                  {/* 分页 */}
                  {totalPages > 1 && (
                    <div className="flex items-center justify-center gap-2 mt-4 pt-4 border-t border-border/50">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={txPage <= 1 || txLoading}
                        onClick={() => fetchTransactions(txPage - 1)}
                      >
                        上一页
                      </Button>
                      <span className="text-sm text-muted-foreground tabular-nums">
                        {txPage} / {totalPages}{txLoading ? ' ...' : ''}
                      </span>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={txPage >= totalPages || txLoading}
                        onClick={() => fetchTransactions(txPage + 1)}
                      >
                        下一页
                      </Button>
                    </div>
                  )}
                </>
              )}
            </TabsContent>

            {isAgent && (
              <TabsContent value="withdrawals" className="mt-0">
                <WithdrawalHistory />
              </TabsContent>
            )}
          </CardContent>
        </Tabs>
      </Card>

      {/* V3.3.1 服务费(L2 + 总开关 ON 时显示 · 组件内部自检) */}
      <ServiceFeeWalletCard />

      {/* 提现弹窗 */}
      <WithdrawalDialog
        open={withdrawalDialogOpen}
        onClose={() => setWithdrawalDialogOpen(false)}
        commissionPoints={commissionPoints}
        onSuccess={() => {
          refreshBalance();
          fetchWithdrawalPrereqs();
          fetchTransactions(1);
          authFetch('/api/wallet/freeze-detail').then(r => r.ok ? r.json() : null).then(d => { if (d) setFreezeDetail(d); }).catch(() => {});
        }}
      />

      {/* KYC 弹窗 */}
      <WithdrawalKycDialog
        open={kycDialogOpen}
        onClose={() => setKycDialogOpen(false)}
        onSuccess={() => {
          setKycVerified(true);
          fetchWithdrawalPrereqs();
        }}
      />
      {onlinePurchaseGateDialog}
    </div>
  );
}

// ========== 工具函数 ==========

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch {
    return iso;
  }
}

function formatCountdown(isoTarget: string): string {
  try {
    const diff = new Date(isoTarget).getTime() - Date.now();
    if (diff <= 0) return '即将解冻';
    const hours = Math.floor(diff / 3600000);
    const days = Math.floor(hours / 24);
    const remainHours = hours % 24;
    if (days > 0) return `${days}天${remainHours}小时`;
    return `${remainHours}小时`;
  } catch {
    return '';
  }
}
