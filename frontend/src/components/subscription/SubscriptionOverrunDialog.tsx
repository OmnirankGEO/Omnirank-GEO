/**
 * Social Studio 超量弹窗 V3.1
 *
 * 来源: docs/AI-CONTEXT/SOCIAL_STUDIO_PRICING_V3_1_EXECUTION_2026-05-10.md §8
 * 计划: .planning/phases/07-social-studio-subscription/ADDON_OPTIMIZATION_V1.md (T3 重写)
 *
 * 设计原则:
 *   - 不阻断对话(CLAUDE.md 永远不中断对话铁律)
 *   - 不静默扣费
 *   - 永远给具体数字,永不"不限"(P0-2)
 *   - 4 选项: 继续扣积分 / 加购扩展包 / 升级套餐 / 取消
 *
 * 2026-05-13 重写 (ADDON_OPTIMIZATION_V1 T3):
 *   - 全 ss-* token (复用 useSocialStudioTheme)
 *   - handleAddon inline 调 POST /addon-buy 不跳路由(用户不离开当前页)
 *   - 同维度多 SKU 时拉 GET /addons?target_quota=X 让用户选档位
 *   - 加买成功后 onPaid 回调让原流程重试
 */
import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { Loader2, ArrowRight, X, CheckCircle2 } from 'lucide-react';
import { toast } from 'sonner';
import { useNavigate } from 'react-router-dom';
import { authFetch } from '@/lib/api';
import { cn } from '@/lib/utils';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from '@/components/ui/dialog';

interface OverrunHint {
  in_quota: boolean;
  quota_type: string;
  used?: number;
  limit?: number;
  would_consume?: number;
  fallback_points: number;
  fallback_yuan: number;
  addon_pack: { name: string; yuan: number; unit: string } | null;
  upgrade_to: string | null;
  upgrade_yuan: number | null;
  current_plan: string;
  no_active_subscription?: boolean;
}

interface AddonSku {
  addon_id: string;
  target_quota: string;
  amount: number;
  yuan_price: number;
  points_price: number;
  display_name: string;
  description?: string;
  best_for?: string;
}

const QUOTA_LABEL: Record<string, string> = {
  light_chat: '轻量写稿次数',
  pro_write: '专业写稿次数',
  super_write: '超级写稿次数',
  web_search: '联网调用次数',
  video_minutes: '视频处理分钟',
  rewrite: '仿写次数',
  video_breakdown: '拆视频次数',
  author_breakdown: '拆博主次数',
  review: '复盘次数',
  monthly_plan: '本月计划次数',
  team_profile: '团队画像次数',
};

const PLAN_LABEL: Record<string, string> = {
  free: '免费体验',
  personal: '个人创作者(¥49/月)',
  growth: '内容增长(¥99/月)',
  agency: '代运营(¥299/月)',
  partner: '代理团队(¥599 起/月)',
};

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

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  featureCode: string;
  videoMinutes?: number;
  onContinueWithPoints?: () => void;
  onPaidAddon?: () => void;
  onSkip?: () => void;
  /** ss-* token 注入 (Portal 内必传 · 来自 useSocialStudioTheme()) */
  themeStyle?: CSSProperties;
  /**
   * 'overrun' (默认 · 用尽时被动弹起 · "本月 X 用完了" 文案)
   * 'manual'  (从配额仪表盘主动点"加包" · "加配额" 文案 · 不显"继续扣积分"路径)
   * 2026-05-13 复查 Bug B 修
   */
  mode?: 'overrun' | 'manual';
}

export default function SubscriptionOverrunDialog({
  open,
  onOpenChange,
  featureCode,
  videoMinutes = 0,
  onContinueWithPoints,
  onPaidAddon,
  onSkip,
  themeStyle,
  mode = 'overrun',
}: Props) {
  const navigate = useNavigate();
  const [hint, setHint] = useState<OverrunHint | null>(null);
  const [loading, setLoading] = useState(false);
  const [availableAddons, setAvailableAddons] = useState<AddonSku[]>([]);
  const [selectedAddonId, setSelectedAddonId] = useState<string | null>(null);
  const [buyingAddon, setBuyingAddon] = useState(false);
  const [paidSuccess, setPaidSuccess] = useState(false);

  // 1. 拉超量提示
  useEffect(() => {
    if (!open) return;
    let alive = true;
    setHint(null);
    setAvailableAddons([]);
    setSelectedAddonId(null);
    setPaidSuccess(false);
    (async () => {
      setLoading(true);
      try {
        const res = await authFetch('/api/subscription/quota-check', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ feature_code: featureCode, video_minutes: videoMinutes }),
        });
        const data = await res.json();
        if (alive && data.success) setHint(data.data);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [open, featureCode, videoMinutes]);

  // 2. 根据 quota_type 拉所有可买的 addon (同维度可能有多 SKU)
  useEffect(() => {
    if (!hint?.quota_type) return;
    let alive = true;
    (async () => {
      try {
        const res = await authFetch(
          `/api/subscription/addons?target_quota=${encodeURIComponent(hint.quota_type)}`
        );
        const data = await res.json();
        if (alive && data.success && Array.isArray(data.data)) {
          setAvailableAddons(data.data);
          // 默认选第一个 (按 sort_order + yuan_price 已排序 · 即"轻量包")
          if (data.data.length > 0) setSelectedAddonId(data.data[0].addon_id);
        }
      } catch { /* 静默 · 至少 hint.addon_pack 老逻辑可兜底 */ }
    })();
    return () => { alive = false; };
  }, [hint?.quota_type]);

  const upgradeAdvice = useMemo(() => {
    // 累加 2-3 包 ≥ 升级差价 → 提示"升级更划算"
    if (!hint?.upgrade_to || !hint.upgrade_yuan) return null;
    const selected = availableAddons.find((a) => a.addon_id === selectedAddonId);
    if (!selected) return null;
    const upgradeDiff = hint.upgrade_yuan;
    const addonsTo = Math.ceil(upgradeDiff / selected.yuan_price);
    if (addonsTo <= 1) return null;
    return `Tip: 再加 ${addonsTo - 1} 个本包(共 ¥${addonsTo * selected.yuan_price}) ≥ 升级 ${PLAN_LABEL[hint.upgrade_to] || hint.upgrade_to} 月卡(¥${upgradeDiff}/月)· 升级更划算`;
  }, [hint, availableAddons, selectedAddonId]);

  const handleUpgrade = () => {
    onOpenChange(false);
    navigate('/subscription/manage');
  };

  const handleAddon = async () => {
    if (!selectedAddonId) {
      toast.error('请先选择加包档位');
      return;
    }
    setBuyingAddon(true);
    try {
      const res = await authFetch('/api/subscription/addon-buy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ addon_id: selectedAddonId, payment_method: 'points' }),
      });
      const data = await res.json();
      if (!data.success) {
        toast.error(detailToText(data.detail, '加包失败'));
        return;
      }
      setPaidSuccess(true);
      toast.success(`已加 ${data.data.amount} 单位 ${QUOTA_LABEL[hint?.quota_type || ''] || ''}`);
      // 1.2s 后关闭 + 回调让原流程重试
      setTimeout(() => {
        onOpenChange(false);
        onPaidAddon?.();
      }, 1200);
    } catch (e: any) {
      toast.error(`加包失败: ${e?.message || e}`);
    } finally {
      setBuyingAddon(false);
    }
  };

  const handleContinue = () => {
    onOpenChange(false);
    onContinueWithPoints?.();
  };

  const handleSkip = () => {
    onOpenChange(false);
    onSkip?.();
  };

  if (!hint) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent
          className="border-[var(--ss-line)] bg-[var(--ss-panel)] text-[var(--ss-text)]"
          style={themeStyle}
        >
          <div className="flex h-32 items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-[var(--ss-muted)]" />
          </div>
        </DialogContent>
      </Dialog>
    );
  }

  const quotaLabel = QUOTA_LABEL[hint.quota_type] || hint.quota_type;
  const upgradePlanLabel = hint.upgrade_to ? PLAN_LABEL[hint.upgrade_to] : null;
  const selectedAddon = availableAddons.find((a) => a.addon_id === selectedAddonId);

  // Bug A 修: free 用户 9 SKU 都 min_plan_id='personal' · 加包必 403 · 提前拦
  const isFreeUserBlocked = hint.current_plan === 'free' && availableAddons.length > 0;

  // 不同 quota_type + mode 友好文案
  // Bug B 修: mode='manual' (主动加包) vs 'overrun' (用尽被动弹) 文案分流
  let title: string;
  let body: string;
  if (hint.no_active_subscription) {
    title = '此功能需要订阅才能使用';
    body = `这次操作预计消耗 ${hint.fallback_points} 算力。订阅月卡可获得每月固定额度,更划算。`;
  } else if (mode === 'manual') {
    // 主动加包模式 (从仪表盘点"加包"进来)
    title = `加 ${quotaLabel}`;
    if (hint.limit !== undefined && hint.limit > 0) {
      body = `当前本月 ${hint.used} / ${hint.limit} 已用 · 加包获得更多额度 · 本周期有效不滚存`;
    } else {
      body = '加包获得本周期额度 · 不滚存到下月';
    }
  } else {
    // overrun 模式 (用尽被动弹起)
    title = `本月${quotaLabel}用完了`;
    if (hint.quota_type === 'video_minutes') {
      title = '本月视频处理分钟用完了';
      body = `这条视频预计还需要 ${hint.would_consume} 分钟额度。`;
    } else if (hint.quota_type === 'author_breakdown') {
      title = '本月拆博主体验已用完';
      body = '继续可加购拆博主扩展包或升级套餐。';
    } else {
      body = `已用 ${hint.used} / ${hint.limit} 次,这次操作还需要 ${hint.would_consume} 次。`;
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="border-[var(--ss-line)] bg-[var(--ss-panel)] text-[var(--ss-text)] sm:max-w-md"
        style={themeStyle}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center justify-between gap-2 text-base text-[var(--ss-text)]">
            <span>{title}</span>
            <button
              type="button"
              onClick={() => onOpenChange(false)}
              aria-label="关闭"
              className="text-[var(--ss-muted)] hover:text-[var(--ss-text)]"
            >
              <X className="h-4 w-4" />
            </button>
          </DialogTitle>
          <DialogDescription className="text-xs leading-relaxed text-[var(--ss-muted)]">
            {body}
          </DialogDescription>
        </DialogHeader>

        {paidSuccess ? (
          // 加包成功内嵌
          <div className="space-y-2 py-6 text-center">
            <CheckCircle2 className="mx-auto h-10 w-10 text-[var(--ss-success)]" />
            <div className="text-sm font-medium text-[var(--ss-text)]">已加包 · 额度已发放</div>
            <div className="text-xs text-[var(--ss-muted)]">即将返回继续原操作...</div>
          </div>
        ) : isFreeUserBlocked ? (
          // Bug A + J 修: free 用户加包必 403 · 引导订阅 · 文案柔化
          <div className="flex flex-col gap-3 py-2">
            <div className="rounded-xl border border-[var(--ss-line-soft)] bg-[var(--ss-panel-soft)] p-3 text-sm leading-relaxed text-[var(--ss-text-soft)]">
              <p>免费体验已含 11 维基础额度,适合先试试。</p>
              <p className="mt-1.5 text-xs text-[var(--ss-muted)]">
                订阅个人创作者 ¥49/月(首月 ¥9.9 体验),即可解锁 9 类增量包 + 更大配额。
              </p>
            </div>
            <button
              type="button"
              onClick={() => {
                onOpenChange(false);
                navigate('/pricing-plans');
              }}
              className="flex h-11 w-full items-center justify-center gap-2 rounded-xl bg-[var(--ss-primary)] px-4 text-sm font-medium text-[var(--ss-primary-text)] transition hover:bg-[var(--ss-primary-hover)]"
            >
              <span>查看月卡套餐</span>
              <ArrowRight className="h-4 w-4" aria-hidden="true" />
            </button>
            <button
              type="button"
              onClick={() => onOpenChange(false)}
              className="h-10 w-full rounded-xl text-sm text-[var(--ss-muted)] hover:text-[var(--ss-text)]"
            >
              取消
            </button>
          </div>
        ) : (
          <div className="flex flex-col gap-3 py-2">
            {/* 同维度有多 SKU 时 · 加包档位选择 */}
            {availableAddons.length > 1 && (
              <div className="space-y-2 rounded-xl border border-[var(--ss-line-soft)] bg-[var(--ss-panel-soft)] p-3">
                <div className="text-xs text-[var(--ss-muted)]">选加包档位</div>
                <div className="space-y-1.5">
                  {availableAddons.map((sku) => {
                    const isSelected = sku.addon_id === selectedAddonId;
                    return (
                      <button
                        key={sku.addon_id}
                        type="button"
                        onClick={() => setSelectedAddonId(sku.addon_id)}
                        className={cn(
                          'flex w-full items-center justify-between rounded-lg border px-3 py-2 text-left text-sm transition',
                          isSelected
                            ? 'border-[var(--ss-primary)] bg-[var(--ss-panel)] ring-1 ring-[var(--ss-primary)]/30'
                            : 'border-[var(--ss-line)] bg-[var(--ss-panel)] hover:bg-[var(--ss-hover)]',
                        )}
                      >
                        <div className="min-w-0">
                          <div className="font-medium text-[var(--ss-text)]">{sku.display_name}</div>
                          {sku.best_for && (
                            <div className="mt-0.5 line-clamp-1 text-[10px] text-[var(--ss-quiet)]">
                              {sku.best_for}
                            </div>
                          )}
                        </div>
                        <div className="ml-2 shrink-0 text-right">
                          <div className="text-sm font-semibold text-[var(--ss-text)]">
                            ¥{sku.yuan_price}
                          </div>
                          <div className="text-[10px] text-[var(--ss-muted)]">
                            +{sku.amount} 单位
                          </div>
                        </div>
                      </button>
                    );
                  })}
                </div>
                {upgradeAdvice && (
                  <p className="rounded-md bg-[var(--ss-panel-softer)] px-2 py-1.5 text-[10px] leading-relaxed text-[var(--ss-muted)]">
                    {upgradeAdvice}
                  </p>
                )}
              </div>
            )}

            {/* 选项 1: 继续扣积分 (兜底价) · mode='manual' 时隐藏(用户主动加包不是用尽) */}
            {mode === 'overrun' && (
              <button
                type="button"
                onClick={handleContinue}
                disabled={loading || buyingAddon}
                className={cn(
                  'flex h-11 w-full items-center justify-between rounded-xl px-4 text-sm font-medium transition',
                  'bg-[var(--ss-primary)] text-[var(--ss-primary-text)] hover:bg-[var(--ss-primary-hover)]',
                  (loading || buyingAddon) && 'cursor-not-allowed opacity-60',
                )}
              >
                <span>继续(消耗 {hint.fallback_points} 算力)</span>
                <span className="text-xs opacity-80">¥{hint.fallback_yuan}</span>
              </button>
            )}

            {/* 选项 2: 加包 (inline 调 addon-buy · mode='manual' 时为主操作 · 提升视觉) */}
            {selectedAddon ? (
              <button
                type="button"
                onClick={handleAddon}
                disabled={buyingAddon}
                className={cn(
                  'flex h-11 w-full items-center justify-between rounded-xl px-4 text-sm font-medium transition',
                  // mode='manual' 时加包是主操作 (primary 视觉) · 'overrun' 时是次选 (outline 视觉)
                  mode === 'manual'
                    ? 'bg-[var(--ss-primary)] text-[var(--ss-primary-text)] hover:bg-[var(--ss-primary-hover)]'
                    : 'border border-[var(--ss-line)] bg-[var(--ss-panel-soft)] text-[var(--ss-text)] hover:bg-[var(--ss-hover)]',
                  buyingAddon && 'cursor-not-allowed opacity-60',
                )}
              >
                {buyingAddon ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    <span>加包中...</span>
                  </>
                ) : (
                  <>
                    <span>立刻加 {selectedAddon.display_name}</span>
                    <span className="text-xs text-[var(--ss-muted)]">
                      ¥{selectedAddon.yuan_price} · 算力扣
                    </span>
                  </>
                )}
              </button>
            ) : (
              hint.addon_pack && (
                <button
                  type="button"
                  onClick={() => {
                    onOpenChange(false);
                    navigate('/wallet?tab=addon');
                  }}
                  className="flex h-11 w-full items-center justify-between rounded-xl border border-[var(--ss-line)] bg-[var(--ss-panel-soft)] px-4 text-sm font-medium text-[var(--ss-text)] hover:bg-[var(--ss-hover)]"
                >
                  <span>加购 {hint.addon_pack.name}</span>
                  <span className="text-xs text-[var(--ss-muted)]">
                    ¥{hint.addon_pack.yuan} · {hint.addon_pack.unit}
                  </span>
                </button>
              )
            )}

            {/* 选项 3: 升级套餐 */}
            {upgradePlanLabel && hint.upgrade_yuan !== null && (
              <button
                type="button"
                onClick={handleUpgrade}
                className="flex h-11 w-full items-center justify-between rounded-xl border border-[var(--ss-line)] bg-[var(--ss-panel-soft)] px-4 text-sm font-medium text-[var(--ss-text)] hover:bg-[var(--ss-hover)]"
              >
                <span>升级到 {upgradePlanLabel}</span>
                <span className="flex items-center gap-1 text-xs text-[var(--ss-muted)]">
                  {hint.upgrade_to === 'agency'
                    ? '更高额度'
                    : hint.upgrade_to === 'partner'
                      ? '团队席位'
                      : '更多额度'}
                  <ArrowRight className="h-3 w-3" aria-hidden="true" />
                </span>
              </button>
            )}

            {/* 选项 4: 取消 / 换条短的 */}
            <button
              type="button"
              onClick={handleSkip}
              className="h-10 w-full rounded-xl text-sm text-[var(--ss-muted)] hover:text-[var(--ss-text)]"
            >
              {hint.quota_type === 'video_minutes' ? '换一条更短的视频' : '取消'}
            </button>
          </div>
        )}

        <DialogFooter className="text-[10px] text-[var(--ss-quiet)]">
          <span>不会自动消耗算力 · 选择前可随时关闭对话框</span>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
