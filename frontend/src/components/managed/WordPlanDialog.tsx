/**
 * v3.3 单词全权托管弹窗 — 用户视角"出现率"档位 + 自定义金额双轨
 *
 * 核心交互（来自 v2 决策）：
 *   - 4 档预设（入门/标准/旗舰/强势 → SOV 15/25/33/50）
 *   - 显眼按钮切换"全托管模式"（默认半自动）
 *   - 报价 3 天有效期（弹窗显示 + 后端校验）
 *   - 双勾选确认（服务协议 + 品牌口吻授权）
 *   - 自定义金额输入框 → AI 反算可达 SOV
 */

import { useState, useEffect, useRef } from 'react';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Loader2, Sparkles, Clock, AlertTriangle, ShieldCheck, Zap, Lock, ChevronDown } from 'lucide-react';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';

import * as managedApi from './api';
import type {
  EstimateResponse, TierKey, CampaignMode, PlatformRecommendation,
} from './types';
import { QuickRechargeDialog } from './QuickRechargeDialog';
import { useWallet } from '@/context/WalletContext';
import { useWaitMessage } from '@/hooks/useWaitMessage';

const POINTS_PER_YUAN = 130;

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  keyword: string;
  brandId: number;
  city?: string;
  industry?: string;
  initialTier?: TierKey;
  onCreated?: (campaignId: number) => void;
}

const TIER_LABELS: Record<TierKey, { name: string; emoji: string }> = {
  entry:    { name: '入门试水', emoji: '🌱' },
  standard: { name: '标准上榜', emoji: '⭐' },
  flagship: { name: '旗舰抢位', emoji: '🚀' },
  strong:   { name: '强势曝光', emoji: '🔥' },
  custom:   { name: '自定义',   emoji: '⚙️' },
};


export function WordPlanDialog({ open, onOpenChange, keyword, brandId, city, industry, initialTier, onCreated }: Props) {
  const [estimate, setEstimate] = useState<EstimateResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // 用户选择（2026-04-18: 继承卡片上的选中档，不再强制从 standard 开始）
  const [selectedTier, setSelectedTier] = useState<TierKey>(initialTier ?? 'standard');

  // 每次打开时同步 initialTier（用户可能先选入门、取消、再开又选旗舰）
  useEffect(() => {
    if (open && initialTier) setSelectedTier(initialTier);
  }, [open, initialTier]);

  // 滚动区内容变化时重新检测下滑提示（比如 estimate 加载完成后内容变长）
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const check = () => {
      setShowScrollHint(el.scrollHeight - (el.scrollTop + el.clientHeight) > 40);
    };
    // 延迟一点给 DOM 渲染完成
    const t = setTimeout(check, 200);
    return () => clearTimeout(t);
  }, [estimate, loading, selectedTier]);
  const [customAmount, setCustomAmount] = useState<string>('');
  const [mode, setMode] = useState<CampaignMode>('semi_auto');
  const [maxPerArticle, setMaxPerArticle] = useState(200);
  const [checkFreq, setCheckFreq] = useState(1);

  const [agreementConsent, setAgreementConsent] = useState(false);
  const [brandVoiceConsent, setBrandVoiceConsent] = useState(false);

  // 积分不足时就地弹出的快速充值 Dialog
  // [BUG-2 2026-07-27] 前端预检用【充值算力全集】publishPointsAvailable
  //   = paid + tool_credit + publish_credit + commission。
  // 原来只看 paidPoints(仅平台 user_wallets.paid),V3.5 客户的钱在信用钱包里 → 恒判"余额不足",
  // 明明有钱却被前端拦下弹充值。它是原判据的严格超集,只会减少误拦,不会放过真不足。
  const { publishPointsAvailable, refreshBalance, status: walletStatus } = useWallet();
  const [quickRechargeOpen, setQuickRechargeOpen] = useState(false);

  // 滚动区引用 + 下滑提示状态
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [showScrollHint, setShowScrollHint] = useState(false);

  // 估算过程的动态安抚文案（10-30s 长任务，避免"AI 正在评估市场..."一动不动）
  const estimateWaitMsg = useWaitMessage(loading && !estimate, 'estimate');

  // 拉取估算（打开时 + 自定义金额变化时）
  useEffect(() => {
    if (!open || !keyword) return;
    void doEstimate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, keyword]);

  async function doEstimate(extraBudget?: number) {
    setLoading(true);
    setErr(null);
    try {
      const payload: managedApi.EstimatePayload = {
        keyword, city, industry,
        max_per_article_yuan: maxPerArticle,
        check_frequency_per_day: checkFreq,
        mode: extraBudget ? 'by_budget' : 'by_target_sov',
      };
      if (extraBudget && extraBudget > 0) {
        payload.budget_yuan = extraBudget;
      } else {
        payload.target_sov_pct = 25;
      }
      const data = await managedApi.estimate(payload);
      setEstimate(data);
    } catch (e: any) {
      setErr(e?.message || '估算失败');
    } finally {
      setLoading(false);
    }
  }

  // 自定义金额触发反算（防抖）
  useEffect(() => {
    if (!open) return;
    const n = parseFloat(customAmount);
    if (Number.isFinite(n) && n > 0) {
      const t = window.setTimeout(() => doEstimate(n), 500);
      return () => window.clearTimeout(t);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [customAmount]);

  // 当前选择的方案数据
  function currentPlan(): { sov: number; label: string; cost: number; articles: number; tier: TierKey } | null {
    if (!estimate) return null;
    if (selectedTier === 'custom') {
      const c = estimate.custom_input;
      if (!c) return null;
      return {
        sov: c.achievable_sov_pct,
        label: c.matched_tier_label,
        cost: parseFloat(customAmount) || 0,
        articles: c.achievable_articles,
        tier: 'custom',
      };
    }
    const t = estimate.tier_options[selectedTier];
    if (!t) return null;
    return {
      sov: t.sov_pct,
      label: t.label,
      cost: t.cost_yuan,
      articles: t.articles,
      tier: selectedTier,
    };
  }

  async function handleConfirm() {
    if (!estimate) return;
    const plan = currentPlan();
    if (!plan || !plan.cost) {
      toast.error('请选择方案或输入自定义金额');
      return;
    }
    if (!agreementConsent || !brandVoiceConsent) {
      toast.error('请勾选服务协议和品牌口吻授权');
      return;
    }

    if (walletStatus !== 'ready') {
      toast.error('暂时无法确认充值算力余额，请先重新读取余额；本次套餐尚未启动，也没有扣钱');
      return;
    }

    // 前端预检余额 - 不够就地弹 QuickRechargeDialog (不再 toast 后让用户自己跳充值)
    const requiredPoints = plan.cost * POINTS_PER_YUAN;
    if (publishPointsAvailable < requiredPoints) {
      setQuickRechargeOpen(true);
      return;
    }

    setSubmitting(true);
    try {
      const result = await managedApi.confirmRecharge({
        keyword,
        brand_id: brandId,
        target_sov_pct: plan.sov,
        target_display_label: plan.label,
        tier_label: plan.tier,
        selected_amount_yuan: plan.cost,
        mode,
        max_per_article_yuan: maxPerArticle,
        check_frequency_per_day: checkFreq,
        estimate_quoted_at: estimate.estimate_quoted_at,
        current_plan: {
          target_sov_pct: plan.sov,
          platform_mix: estimate.platform_mix_recommended,
          mode,
        },
        agreement_consent: agreementConsent,
        brand_voice_consent: brandVoiceConsent,
        agreement_version: 'v3.3-2026-04',
      });
      toast.success(`套餐已启动（id=${result.campaign_id}）`);
      onOpenChange(false);
      onCreated?.(result.campaign_id);
    } catch (e: any) {
      const code = e?.code;
      if (code === 'ESTIMATE_EXPIRED') {
        toast.error('报价已过 3 天有效期，正在重新估算...');
        void doEstimate();
      } else if (code === 'DUPLICATE_CAMPAIGN') {
        toast.error('您已有该词的活跃套餐，请先暂停或换其他词');
      } else if (code === 'INSUFFICIENT_PAID_POINTS') {
        // 后端兜底: 前端预检已做, 这里仍有可能并发竞争 → 就地弹充值 Dialog
        setQuickRechargeOpen(true);
      } else {
        toast.error(e?.message || '启动失败');
      }
    } finally {
      setSubmitting(false);
    }
  }

  const plan = currentPlan();

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-amber-500" />
            "{keyword}" — AI 帮你算方案
          </DialogTitle>
          <DialogDescription>
            选择目标"出现率"或自定义金额，AI 会按系统真实定价算出方案
          </DialogDescription>
        </DialogHeader>

        {loading && !estimate ? (
          <div className="flex flex-col items-center justify-center py-12 gap-2">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            <span className="text-sm text-muted-foreground">AI 正在评估市场…</span>
            {estimateWaitMsg && (
              <span
                key={estimateWaitMsg}
                className="text-xs text-muted-foreground/70 animate-in fade-in duration-700 max-w-sm text-center"
              >
                {estimateWaitMsg}
              </span>
            )}
          </div>
        ) : err ? (
          <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-700">
            <AlertTriangle className="inline-block h-4 w-4 mr-1" />
            {err}
            <Button size="sm" variant="outline" className="ml-2" onClick={() => doEstimate()}>重试</Button>
          </div>
        ) : estimate ? (
          <div className="relative">
          <div
            ref={scrollRef}
            className="space-y-4 max-h-[60vh] overflow-y-auto pr-2 scroll-smooth"
            onScroll={e => {
              const el = e.currentTarget;
              // 离底部还有 >40px 认为还能继续下滑
              setShowScrollHint(el.scrollHeight - (el.scrollTop + el.clientHeight) > 40);
            }}
          >

            {/* 评估摘要 */}
            <div className="rounded-lg border bg-muted/30 p-3 text-xs space-y-1">
              <div className="flex items-center justify-between">
                <span>📊 竞争度：<strong>{['极低','较低','中等','激烈','极高'][estimate.competition_level - 1]}</strong>（{estimate.competition_count} 个有效竞品）</span>
                <span className="text-amber-600 dark:text-amber-400">
                  <Clock className="inline-block h-3 w-3 mr-1" />
                  报价至 {new Date(estimate.estimate_valid_until).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
                </span>
              </div>
              <div>⏱ 预估上榜时间：{estimate.estimated_uprank_days}</div>
            </div>

            {/* 模式切换（显眼） */}
            <div className={cn(
              'rounded-lg border-2 p-3 cursor-pointer transition-colors',
              mode === 'full_auto'
                ? 'border-amber-500 bg-amber-500/10'
                : 'border-muted bg-muted/20'
            )} onClick={() => setMode(mode === 'semi_auto' ? 'full_auto' : 'semi_auto')}>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  {mode === 'full_auto' ? <Zap className="h-4 w-4 text-amber-500" /> : <ShieldCheck className="h-4 w-4 text-muted-foreground" />}
                  <span className="text-sm font-medium">
                    {mode === 'semi_auto' ? '当前：半自动模式（AI 写完请您审核）' : '当前：全托管模式（AI 自主发布无需审核）'}
                  </span>
                </div>
                <Button variant="outline" size="sm" className="text-xs">
                  {mode === 'semi_auto' ? '⚡ 切到全托管' : '🔒 切回半自动'}
                </Button>
              </div>
              <div className="text-[11px] text-muted-foreground mt-1">
                {mode === 'semi_auto'
                  ? '推荐首次使用 — 您 24h 不审默认自动发布'
                  : '杀手级模式 — AI 全自主，您只看战报'}
              </div>
            </div>

            {/* 4 档预设 */}
            <div>
              <div className="text-xs text-muted-foreground mb-2">选择目标（用户视角的"出现率"）</div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                {(['entry', 'standard', 'flagship', 'strong'] as TierKey[]).map((key) => {
                  const t = estimate.tier_options[key];
                  const tierMeta = TIER_LABELS[key];
                  if (!t) return null;
                  return (
                    <div
                      key={key}
                      className={cn(
                        'rounded-lg border-2 p-3 cursor-pointer transition-all',
                        selectedTier === key
                          ? 'border-primary bg-primary/5 shadow-sm'
                          : 'border-muted hover:border-muted-foreground/30'
                      )}
                      onClick={() => setSelectedTier(key)}
                    >
                      <div className="text-xs font-medium flex items-center gap-1">
                        <span>{tierMeta.emoji}</span> {tierMeta.name}
                        {t.recommended && <span className="text-[9px] text-amber-500">⭐</span>}
                      </div>
                      <div className="text-[11px] text-muted-foreground mt-1">{t.label}</div>
                      <div className="text-sm font-semibold mt-2">{t.articles} 篇</div>
                      <div className="text-base font-bold text-primary">¥{t.cost_yuan}</div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* 自定义金额 */}
            <div className={cn(
              'rounded-lg border-2 p-3 transition-all',
              selectedTier === 'custom' ? 'border-primary bg-primary/5' : 'border-dashed border-muted'
            )}>
              <label className="text-xs text-muted-foreground flex items-center gap-1">
                ⚙️ 自定义金额（AI 反算可达档位）
              </label>
              <div className="flex items-center gap-2 mt-1">
                <span className="text-sm">¥</span>
                <input
                  type="number"
                  className="flex-1 rounded-md border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                  placeholder="例如 800"
                  value={customAmount}
                  onChange={(e) => {
                    setCustomAmount(e.target.value);
                    if (e.target.value) setSelectedTier('custom');
                  }}
                />
                {estimate.custom_input && selectedTier === 'custom' && (
                  <div className="text-xs text-muted-foreground">
                    → 约 <strong>{estimate.custom_input.achievable_articles} 篇</strong>{' '}
                    ({estimate.custom_input.matched_tier_label})
                  </div>
                )}
              </div>
            </div>

            {/* 当前选择详情 */}
            {plan && plan.cost > 0 && (
              <div className="rounded-lg border bg-card p-3 space-y-1.5">
                <div className="text-xs font-medium text-muted-foreground">📦 当前方案</div>
                <div className="text-sm">
                  <strong>{plan.label}</strong>（约 <strong>{plan.articles} 篇</strong> GEO 文章）
                </div>
                <div className="text-sm">
                  💰 总价 <span className="text-base font-bold text-primary">¥{plan.cost}</span>
                </div>
                {estimate.platform_mix_recommended.length > 0 && (
                  <div className="text-[11px] text-muted-foreground">
                    🎯 AI 推荐媒体：{estimate.platform_mix_recommended.slice(0, 5).map((p: PlatformRecommendation) => p.platform || p.media_name).filter(Boolean).join(' / ')}
                  </div>
                )}
              </div>
            )}

            {/* 高级设置 */}
            <details className="rounded border bg-muted/10">
              <summary className="cursor-pointer px-3 py-2 text-xs text-muted-foreground">
                ⚙️ 高级设置（可选，告诉 AI 也能改）
              </summary>
              <div className="p-3 space-y-3 text-xs border-t">
                <div className="flex items-center justify-between">
                  <label>监测频次</label>
                  <select
                    className="rounded border bg-background px-2 py-1"
                    value={checkFreq}
                    onChange={(e) => setCheckFreq(parseInt(e.target.value))}
                  >
                    <option value={1}>1 次/天</option>
                    <option value={2}>2 次/天</option>
                    <option value={3}>3 次/天</option>
                  </select>
                </div>
                <div className="flex items-center justify-between">
                  <label>单篇上限（防 AI 选超贵媒体）</label>
                  <div className="flex items-center gap-1">
                    <span>¥</span>
                    <input
                      type="number"
                      className="w-20 rounded border bg-background px-2 py-1"
                      value={maxPerArticle}
                      onChange={(e) => setMaxPerArticle(parseFloat(e.target.value) || 200)}
                    />
                  </div>
                </div>
              </div>
            </details>

            {/* 说明 */}
            <div className="rounded border border-amber-500/30 bg-amber-500/5 p-3 text-[11px] text-amber-700 dark:text-amber-300 space-y-1">
              <div className="font-medium flex items-center gap-1">
                <AlertTriangle className="h-3 w-3" /> 重要说明
              </div>
              <div>• 充值即消费，套餐内 AI 自主执行（不退款）</div>
              <div>• 余额扣完 AI 自动暂停（不过期，永久保留）</div>
              <div>• 12 个月无操作余额自动转赠送算力</div>
              <div>• 想加充 / 调方案，对话告诉 AI 即可</div>
              <div>• 报价 3 天有效，过期需重新评估</div>
            </div>

            {/* 双勾选 */}
            <div className="space-y-2">
              <label className="flex items-start gap-2 cursor-pointer text-xs">
                <Checkbox checked={agreementConsent} onCheckedChange={(v) => setAgreementConsent(v === true)} />
                <span>我已阅读并同意《单词全权托管协议》（服务规则 + 充值即消费）</span>
              </label>
              <label className="flex items-start gap-2 cursor-pointer text-xs">
                <Checkbox checked={brandVoiceConsent} onCheckedChange={(v) => setBrandVoiceConsent(v === true)} />
                <span className="flex items-center gap-1">
                  <Lock className="h-3 w-3 text-muted-foreground" />
                  授权平台以我品牌名义生成 GEO 优化内容（24h 内可在看板撤回未发文章）
                </span>
              </label>
            </div>

          </div>

          {/* 下滑提示 — 仅在内容超出视口时显示, 用户滚动到底部自动隐藏 */}
          {showScrollHint && (
            <button
              type="button"
              onClick={() => {
                scrollRef.current?.scrollBy({ top: 240, behavior: 'smooth' });
              }}
              className="absolute bottom-2 right-4 pointer-events-auto bg-foreground/10 hover:bg-foreground/20 backdrop-blur rounded-full p-1.5 border border-border animate-scroll-hint"
              aria-label="向下滚动查看更多"
              title="向下滚动查看更多"
            >
              <ChevronDown className="h-3.5 w-3.5 text-foreground" />
            </button>
          )}
          </div>
        ) : null}

        <DialogFooter className="gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
            取消
          </Button>
          <Button
            onClick={handleConfirm}
            disabled={submitting || !plan || !plan.cost || !agreementConsent || !brandVoiceConsent}
            className="bg-primary"
          >
            {submitting ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : null}
            充值 ¥{plan?.cost || 0} 并启动
          </Button>
        </DialogFooter>
      </DialogContent>

      {/* 积分不足时就地弹出快速充值（不跳转钱包页）*/}
      <QuickRechargeDialog
        open={quickRechargeOpen}
        onOpenChange={setQuickRechargeOpen}
        requiredYuan={plan?.cost || 0}
        purposeHint="启动托管套餐需要"
        onPaid={async () => {
          await refreshBalance();
          // 余额到账后自动重试启动
          void handleConfirm();
        }}
      />
    </Dialog>
  );
}
