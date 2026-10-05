/**
 * Social Studio 订阅管理页 V3.1 (F04)
 *
 * 来源: docs/AI-CONTEXT/SOCIAL_STUDIO_PRICING_V3_1_EXECUTION_2026-05-10.md §6.4
 * 计划: .planning/phases/07-social-studio-subscription/PLAN.md F04
 *
 * 2026-05-13 重写:
 *   1. 设计语言切换到 ss-* (社媒板块体系一致, 不再像 GEO)
 *   2. toast detail 防御 (后端 dict / array 不再触发 React error #31)
 *   3. confirm() 改 Dialog (与全站 ss-* 一致)
 *   4. 退订 / 升级 / 降级 加 Dialog 二次确认 + 当前订阅信息卡
 *
 * 4 路径:
 *   - 升级: 立即生效 + 补差价(显式公式)
 *   - 降级: 下月生效 + 不退差价
 *   - 退订与退款分离；退款按原订单、法定事由和实际使用证据核验
 *   - auto_renew toggle
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Loader2, ArrowRight, AlertTriangle, ArrowLeft } from 'lucide-react';
import { toast } from 'sonner';
import { authFetch } from '@/lib/api';
import { cn } from '@/lib/utils';
import { useSocialStudioTheme } from '@/lib/socialStudioTheme';
import { Switch } from '@/components/ui/switch';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Textarea } from '@/components/ui/textarea';
import QuotaDashboardCard from '@/components/subscription/QuotaDashboardCard';

interface MyData {
  subscription_id?: number;
  plan_id: string | null;
  display_name: string;
  active: boolean;
  started_at?: string;
  expires_at?: string;
  auto_renew?: boolean;
  monthly_yuan?: number;
  is_first_month?: boolean;
}

interface PlanInfo {
  plan_id: string;
  display_name: string;
  monthly_yuan: number;
  is_recommended: boolean;
}

const PLAN_ORDER = ['free', 'personal', 'growth', 'agency', 'partner'];

function detailToText(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (typeof d === 'string' ? d : (d as { msg?: string })?.msg || JSON.stringify(d)))
      .join('; ');
  }
  if (detail && typeof detail === 'object') {
    const d = detail as { message?: string; error?: string; detail?: string };
    return d.message || d.error || d.detail || fallback;
  }
  return fallback;
}

export default function SubscriptionManagementPage() {
  const navigate = useNavigate();
  const themeStyle = useSocialStudioTheme();

  const [me, setMe] = useState<MyData | null>(null);
  const [plans, setPlans] = useState<PlanInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(false);

  const [upgradeTarget, setUpgradeTarget] = useState<PlanInfo | null>(null);
  const [downgradeTarget, setDowngradeTarget] = useState<PlanInfo | null>(null);
  const [cancelDialog, setCancelDialog] = useState(false);
  const [cancelReason, setCancelReason] = useState('');
  const [requestRefund, setRequestRefund] = useState(false);

  useEffect(() => {
    loadAll();
  }, []);

  const loadAll = async () => {
    setLoading(true);
    try {
      const [meRes, plansRes] = await Promise.all([
        authFetch('/api/subscription/me').then((r) => r.json()),
        authFetch('/api/subscription/plans').then((r) => r.json()),
      ]);
      if (meRes.success) setMe(meRes.data);
      if (plansRes.success) setPlans(plansRes.data || []);
    } finally {
      setLoading(false);
    }
  };

  const doUpgrade = async (target: PlanInfo) => {
    setActionLoading(true);
    try {
      const res = await authFetch('/api/subscription/upgrade', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_plan_id: target.plan_id }),
      });
      const json = await res.json();
      if (json.success) {
        if (json.data?.todo_b9_pending) {
          toast.info(`差价 ¥${json.data.diff_yuan} · ${json.data.formula} · 支付通道接入中`);
        } else {
          toast.success(`已升级到 ${target.display_name}`);
          loadAll();
        }
      } else {
        toast.error(detailToText(json.detail, '升级失败'));
      }
    } finally {
      setActionLoading(false);
      setUpgradeTarget(null);
    }
  };

  const doDowngrade = async (target: PlanInfo) => {
    setActionLoading(true);
    try {
      const res = await authFetch('/api/subscription/downgrade', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_plan_id: target.plan_id }),
      });
      const json = await res.json();
      if (json.success) {
        toast.success('已记录降级请求,下次续费按新套餐生效');
        loadAll();
      } else {
        toast.error(detailToText(json.detail, '降级失败'));
      }
    } finally {
      setActionLoading(false);
      setDowngradeTarget(null);
    }
  };

  const handleCancelConfirm = async () => {
    setActionLoading(true);
    try {
      const res = await authFetch('/api/subscription/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          reason: cancelReason || null,
          request_refund: requestRefund,
          reason_category: requestRefund && withinSevenDays ? 'statutory_seven_day' : 'negotiated_other',
          evidence: { surface: 'subscription-management' },
        }),
      });
      const json = await res.json();
      if (json.success) {
        toast.success(json.data?.message || '已关闭自动续费');
        setCancelDialog(false);
        loadAll();
      } else {
        toast.error(detailToText(json.detail, '退订失败'));
      }
    } finally {
      setActionLoading(false);
    }
  };

  if (loading) {
    return (
      <div
        className="flex min-h-screen items-center justify-center bg-[var(--ss-bg)]"
        style={themeStyle}
      >
        <Loader2 className="h-6 w-6 animate-spin text-[var(--ss-muted)]" />
      </div>
    );
  }

  if (!me) {
    return (
      <div
        className="min-h-screen bg-[var(--ss-bg)] p-8 text-center text-[var(--ss-muted)]"
        style={themeStyle}
      >
        加载失败
      </div>
    );
  }

  const activePlan = me.active ? me.plan_id : 'free';
  const currentIdx = PLAN_ORDER.indexOf(activePlan || 'free');
  const upgradablePlans = plans.filter((p) => PLAN_ORDER.indexOf(p.plan_id) > currentIdx);
  const downgradablePlans = plans.filter(
    (p) => PLAN_ORDER.indexOf(p.plan_id) < currentIdx && p.plan_id !== 'free'
  );
  const expiresAt = me.expires_at ? new Date(me.expires_at) : null;
  const startedAt = me.started_at ? new Date(me.started_at) : null;
  const withinSevenDays = startedAt
    ? Date.now() - startedAt.getTime() < 7 * 86400000
    : false;

  return (
    <div
      className="min-h-screen bg-[var(--ss-bg)] text-[var(--ss-text)]"
      style={themeStyle}
    >
      <div className="mx-auto max-w-4xl space-y-6 p-4 md:p-8">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => navigate('/')}  // [WO_260] 原 /s(社媒首页,随 E3 删域)→ 在役首页
            aria-label="返回小榜"
            className="flex h-9 w-9 items-center justify-center rounded-full border border-[var(--ss-line)] bg-[var(--ss-panel)] text-[var(--ss-text-soft)] transition hover:bg-[var(--ss-hover)]"
          >
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          </button>
          <h1 className="text-2xl font-semibold text-[var(--ss-text)]">订阅管理</h1>
        </div>

        {/* 当前套餐 */}
        <section className="rounded-2xl border border-[var(--ss-line)] bg-[var(--ss-panel)] p-5 shadow-[var(--ss-soft-shadow)]">
          <h2 className="text-base font-medium text-[var(--ss-text-soft)]">当前套餐</h2>
          <div className="mt-3 flex items-center justify-between">
            <div>
              <p className="text-lg font-semibold text-[var(--ss-text)]">{me.display_name}</p>
              {me.monthly_yuan !== undefined && me.monthly_yuan > 0 && (
                <p className="mt-1 text-sm text-[var(--ss-muted)]">¥{me.monthly_yuan}/月</p>
              )}
              {expiresAt && (
                <p className="mt-1 text-xs text-[var(--ss-quiet)]">
                  到期: {expiresAt.toLocaleDateString('zh-CN')}
                </p>
              )}
            </div>
          </div>

          {me.active && (
            <div className="mt-4 flex items-center justify-between rounded-xl border border-[var(--ss-line-soft)] bg-[var(--ss-panel-soft)] p-3">
              <div className="text-sm">
                <p className="font-medium text-[var(--ss-text)]">自动续费</p>
                <p className="mt-0.5 text-xs text-[var(--ss-muted)]">
                  关闭后将不会自动扣款,到期需手动续费(首发不支持免密)
                </p>
              </div>
              <Switch
                checked={!!me.auto_renew}
                disabled={actionLoading}
                onCheckedChange={(checked) => {
                  if (!checked) {
                    setCancelDialog(true);
                  } else {
                    toast.info('开启自动续费需在续费页签订协议');
                    navigate('/subscription/sign');
                  }
                }}
              />
            </div>
          )}
        </section>

        {/* 本月配额仪表盘 (2026-05-13 ADDON_OPTIMIZATION_V1 T5) */}
        {me.active && (
          <QuotaDashboardCard themeStyle={themeStyle} onAddonPaid={loadAll} />
        )}

        {/* 升级 */}
        {upgradablePlans.length > 0 && (
          <section className="rounded-2xl border border-[var(--ss-line)] bg-[var(--ss-panel)] p-5 shadow-[var(--ss-soft-shadow)]">
            <h2 className="text-base font-medium text-[var(--ss-text-soft)]">升级套餐</h2>
            <p className="mt-1 text-xs text-[var(--ss-muted)]">
              立即生效,按剩余天数比例补差价。已购周期价格冻结保护。
            </p>
            <div className="mt-3 space-y-2">
              {upgradablePlans.map((p) => (
                <div
                  key={p.plan_id}
                  className="flex items-center justify-between rounded-xl border border-[var(--ss-line-soft)] bg-[var(--ss-panel-soft)] px-3 py-2.5"
                >
                  <div>
                    <p className="font-medium text-[var(--ss-text)]">{p.display_name}</p>
                    <p className="text-xs text-[var(--ss-muted)]">¥{p.monthly_yuan}/月</p>
                  </div>
                  <button
                    type="button"
                    disabled={actionLoading}
                    onClick={() => setUpgradeTarget(p)}
                    className={cn(
                      'flex h-8 items-center gap-1 rounded-full px-3 text-xs font-medium transition',
                      p.is_recommended
                        ? 'bg-[var(--ss-primary)] text-[var(--ss-primary-text)] hover:bg-[var(--ss-primary-hover)]'
                        : 'border border-[var(--ss-line)] text-[var(--ss-text)] hover:bg-[var(--ss-hover)]',
                      actionLoading && 'cursor-not-allowed opacity-60',
                    )}
                  >
                    升级 <ArrowRight className="h-3 w-3" aria-hidden="true" />
                  </button>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* 降级 */}
        {downgradablePlans.length > 0 && (
          <section className="rounded-2xl border border-[var(--ss-line)] bg-[var(--ss-panel)] p-5 shadow-[var(--ss-soft-shadow)]">
            <h2 className="text-base font-medium text-[var(--ss-text-soft)]">降级套餐</h2>
            <p className="mt-1 text-xs text-[var(--ss-muted)]">
              下次续费时生效,本月权益不变。差价不退。
            </p>
            <div className="mt-3 space-y-2">
              {downgradablePlans.map((p) => (
                <div
                  key={p.plan_id}
                  className="flex items-center justify-between rounded-xl border border-[var(--ss-line-soft)] bg-[var(--ss-panel-soft)] px-3 py-2.5"
                >
                  <div>
                    <p className="font-medium text-[var(--ss-text)]">{p.display_name}</p>
                    <p className="text-xs text-[var(--ss-muted)]">¥{p.monthly_yuan}/月</p>
                  </div>
                  <button
                    type="button"
                    disabled={actionLoading}
                    onClick={() => setDowngradeTarget(p)}
                    className={cn(
                      'flex h-8 items-center rounded-full border border-[var(--ss-line)] px-3 text-xs font-medium text-[var(--ss-text)] transition hover:bg-[var(--ss-hover)]',
                      actionLoading && 'cursor-not-allowed opacity-60',
                    )}
                  >
                    下月降级
                  </button>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* 退订 */}
        {me.active && me.plan_id !== 'free' && (
          <section className="rounded-2xl border border-[var(--ss-danger-line)] bg-[var(--ss-panel)] p-5 shadow-[var(--ss-soft-shadow)]">
            <h2 className="text-base font-medium text-[var(--ss-danger)]">退订</h2>
            <div className="mt-2 space-y-1 text-xs text-[var(--ss-muted)]">
              <p>· 关闭续费后，本期权益保留至到期</p>
              <p>· 退款按原订阅订单、法定事由和实际使用单独核验</p>
              <p>· 普通消费者不预扣固定百分比费用</p>
            </div>
            <button
              type="button"
              disabled={actionLoading}
              onClick={() => setCancelDialog(true)}
              className={cn(
                'mt-3 flex h-9 items-center rounded-full border border-[var(--ss-danger-line)] bg-[var(--ss-danger-bg)] px-3.5 text-xs font-medium text-[var(--ss-danger)] transition hover:opacity-90',
                actionLoading && 'cursor-not-allowed opacity-60',
              )}
            >
              退订
            </button>
          </section>
        )}
      </div>

      {/* 升级确认 */}
      <Dialog open={!!upgradeTarget} onOpenChange={(open) => !open && setUpgradeTarget(null)}>
        <DialogContent
          className="border-[var(--ss-line)] bg-[var(--ss-panel)] text-[var(--ss-text)]"
          style={themeStyle}
        >
          <DialogHeader>
            <DialogTitle>确认升级</DialogTitle>
          </DialogHeader>
          <div className="space-y-2 py-2 text-sm text-[var(--ss-text-soft)]">
            <p>
              升级到 <span className="font-medium text-[var(--ss-text)]">{upgradeTarget?.display_name}</span>{' '}
              (¥{upgradeTarget?.monthly_yuan}/月)?
            </p>
            <p className="text-xs text-[var(--ss-muted)]">
              立即生效,按剩余天数比例补差价。
            </p>
          </div>
          <DialogFooter>
            <button
              type="button"
              onClick={() => setUpgradeTarget(null)}
              disabled={actionLoading}
              className="rounded-full border border-[var(--ss-line)] px-4 py-1.5 text-sm text-[var(--ss-text)] hover:bg-[var(--ss-hover)]"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => upgradeTarget && doUpgrade(upgradeTarget)}
              disabled={actionLoading}
              className="rounded-full bg-[var(--ss-primary)] px-4 py-1.5 text-sm text-[var(--ss-primary-text)] hover:bg-[var(--ss-primary-hover)] disabled:opacity-60"
            >
              {actionLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : '确认升级'}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 降级确认 */}
      <Dialog open={!!downgradeTarget} onOpenChange={(open) => !open && setDowngradeTarget(null)}>
        <DialogContent
          className="border-[var(--ss-line)] bg-[var(--ss-panel)] text-[var(--ss-text)]"
          style={themeStyle}
        >
          <DialogHeader>
            <DialogTitle>确认降级</DialogTitle>
          </DialogHeader>
          <div className="space-y-2 py-2 text-sm text-[var(--ss-text-soft)]">
            <p>
              下月降级到 <span className="font-medium text-[var(--ss-text)]">{downgradeTarget?.display_name}</span>?
            </p>
            <p className="text-xs text-[var(--ss-muted)]">
              本月权益不变,差价不退。下次续费按新套餐生效。
            </p>
          </div>
          <DialogFooter>
            <button
              type="button"
              onClick={() => setDowngradeTarget(null)}
              disabled={actionLoading}
              className="rounded-full border border-[var(--ss-line)] px-4 py-1.5 text-sm text-[var(--ss-text)] hover:bg-[var(--ss-hover)]"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => downgradeTarget && doDowngrade(downgradeTarget)}
              disabled={actionLoading}
              className="rounded-full bg-[var(--ss-primary)] px-4 py-1.5 text-sm text-[var(--ss-primary-text)] hover:bg-[var(--ss-primary-hover)] disabled:opacity-60"
            >
              {actionLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : '确认降级'}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 退订确认 */}
      <Dialog open={cancelDialog} onOpenChange={setCancelDialog}>
        <DialogContent
          className="border-[var(--ss-line)] bg-[var(--ss-panel)] text-[var(--ss-text)]"
          style={themeStyle}
        >
          <DialogHeader>
            <DialogTitle>确认退订?</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 py-2">
            {withinSevenDays && (
              <div className="flex items-start gap-2 rounded-md border border-[var(--ss-danger-line)] bg-[var(--ss-danger-bg)] p-2.5 text-xs text-[var(--ss-danger)]">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                <p>当前处于支付后七日内；是否适用预付式消费七日退款，以首次同类服务和实际使用记录为准。</p>
              </div>
            )}
            <Textarea
              placeholder="退订理由(选填)"
              value={cancelReason}
              onChange={(e) => setCancelReason(e.target.value)}
              className="min-h-[60px] border-[var(--ss-line)] bg-[var(--ss-panel-soft)] text-sm text-[var(--ss-text)] placeholder:text-[var(--ss-quiet)]"
            />
            <label className="flex items-center gap-2 text-xs text-[var(--ss-text-soft)]">
                <input
                  type="checkbox"
                  checked={requestRefund}
                  onChange={(e) => setRequestRefund(e.target.checked)}
                />
                同时按原订阅订单申请退款（关闭续费本身不会自动退款）
              </label>
          </div>
          <DialogFooter>
            <button
              type="button"
              onClick={() => setCancelDialog(false)}
              disabled={actionLoading}
              className="rounded-full border border-[var(--ss-line)] px-4 py-1.5 text-sm text-[var(--ss-text)] hover:bg-[var(--ss-hover)]"
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleCancelConfirm}
              disabled={actionLoading}
              className="rounded-full bg-[var(--ss-danger)] px-4 py-1.5 text-sm text-white hover:opacity-90 disabled:opacity-60"
            >
              {actionLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : '确认退订'}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
