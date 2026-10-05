/**
 * v3.4 全品牌托管弹窗 — 多关键词合并 + markup 1.2x 内嵌售价
 *
 * 核心交互（v2.1 锁定）：
 *   - 用户在物料中心配置完关键词后调起
 *   - 显示"品牌套餐 ¥X"最终售价（不拆 markup）
 *   - 子套餐展开看每个关键词进度
 *   - 含 5 引擎 + 每周复盘
 */

import { useEffect, useState } from 'react';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Loader2, Sparkles, ChevronDown, ChevronUp, Zap, ShieldCheck, AlertTriangle } from 'lucide-react';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';

import * as managedApi from './api';
import type { BrandPlanResponse, CampaignMode } from './types';

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  brandId: number;
  brandName?: string;
  keywords: string[];
  city?: string;
  industry?: string;
  uniformSov?: number;
  onCreated?: (packageId: number, campaignIds: number[]) => void;
}

export function BrandPlanDialog({
  open, onOpenChange, brandId, brandName, keywords, city, industry,
  uniformSov = 25, onCreated,
}: Props) {
  const [plan, setPlan] = useState<BrandPlanResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const [mode, setMode] = useState<CampaignMode>('semi_auto');
  const [agreementConsent, setAgreementConsent] = useState(false);
  const [brandVoiceConsent, setBrandVoiceConsent] = useState(false);
  const [showSubcampaigns, setShowSubcampaigns] = useState(false);

  useEffect(() => {
    if (!open || !keywords.length) return;
    void doEstimate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, brandId, JSON.stringify(keywords)]);

  async function doEstimate() {
    setLoading(true);
    setErr(null);
    try {
      const data = await managedApi.estimateBrand({
        keywords, brand_id: brandId, city, industry,
        uniform_target_sov_pct: uniformSov,
      });
      setPlan(data);
    } catch (e: any) {
      setErr(e?.message || '估算失败');
    } finally {
      setLoading(false);
    }
  }

  async function handleConfirm() {
    if (!plan) return;
    if (!agreementConsent || !brandVoiceConsent) {
      toast.error('请勾选服务协议和品牌口吻授权');
      return;
    }
    setSubmitting(true);
    try {
      const result = await managedApi.confirmBrandRecharge({
        brand_id: brandId,
        final_total_yuan: plan.final_total_yuan,
        raw_total_yuan: plan.raw_total_yuan,
        markup_factor: plan.markup_factor,
        subcampaigns: plan.subcampaigns.map(s => ({
          keyword: s.keyword,
          sov_pct: s.sov_pct,
          articles: s.articles,
          cost_yuan: s.cost_yuan,
        })),
        estimate_quoted_at: plan.estimate_quoted_at,
        mode,
        agreement_consent: agreementConsent,
        brand_voice_consent: brandVoiceConsent,
        agreement_version: 'v3.4-2026-04',
      });
      toast.success(`全品牌套餐已启动（含 ${result.campaign_ids.length} 个子套餐）`);
      onOpenChange(false);
      onCreated?.(result.brand_package_id, result.campaign_ids);
    } catch (e: any) {
      const code = e?.code;
      if (code === 'ESTIMATE_EXPIRED') {
        toast.error('报价已过期，正在重新估算...');
        void doEstimate();
      } else if (code === 'INSUFFICIENT_PAID_POINTS') {
        toast.error('充值算力不足，请先充值');
      } else {
        toast.error(e?.message || '启动失败');
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-amber-500" />
            🚀 {brandName || '品牌'} · 全品牌 AI 自动运营
          </DialogTitle>
          <DialogDescription>
            {keywords.length} 个关键词全自动运营 + 5 引擎联动 + 每周复盘调优
          </DialogDescription>
        </DialogHeader>

        {loading && !plan ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="h-6 w-6 animate-spin" />
            <span className="ml-2 text-sm text-muted-foreground">AI 正在评估 {keywords.length} 个关键词...</span>
          </div>
        ) : err ? (
          <div className="rounded border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-700">
            <AlertTriangle className="inline-block h-4 w-4 mr-1" />
            {err}
            <Button size="sm" variant="outline" className="ml-2" onClick={() => doEstimate()}>重试</Button>
          </div>
        ) : plan ? (
          <div className="space-y-4 max-h-[60vh] overflow-y-auto pr-2">

            {/* 模式切换 */}
            <div className={cn(
              'rounded-lg border-2 p-3 cursor-pointer',
              mode === 'full_auto' ? 'border-amber-500 bg-amber-500/10' : 'border-muted'
            )} onClick={() => setMode(mode === 'semi_auto' ? 'full_auto' : 'semi_auto')}>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 text-sm font-medium">
                  {mode === 'full_auto' ? <Zap className="h-4 w-4 text-amber-500" /> : <ShieldCheck className="h-4 w-4 text-muted-foreground" />}
                  {mode === 'semi_auto' ? '当前：半自动模式' : '当前：全托管模式'}
                </div>
                <Button variant="outline" size="sm" className="text-xs">
                  {mode === 'semi_auto' ? '⚡ 切到全托管' : '🔒 切回半自动'}
                </Button>
              </div>
            </div>

            {/* 主售价（不拆 markup）*/}
            <div className="rounded-lg border-2 border-primary bg-primary/5 p-4 text-center">
              <div className="text-xs text-muted-foreground">品牌套餐</div>
              <div className="text-3xl font-bold text-primary mt-1">¥{plan.final_total_yuan.toLocaleString()}</div>
              <div className="text-[11px] text-muted-foreground mt-2">
                包含 {plan.subcampaigns.length} 个关键词全自动运营 + 5 引擎联动 + 每周复盘
              </div>
            </div>

            {/* 子套餐展开 */}
            <div>
              <button
                className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                onClick={() => setShowSubcampaigns(v => !v)}
              >
                {showSubcampaigns ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                查看子套餐明细（{plan.subcampaigns.length} 个）
              </button>
              {showSubcampaigns && (
                <div className="mt-2 rounded border divide-y">
                  {plan.subcampaigns.map((sc, i) => {
                    // 单价按 markup 后显示（总和 = final_total_yuan，防 raw/markup 被客户倒推）
                    const displayPrice = Math.round(sc.cost_yuan * (plan.markup_factor || 1));
                    return (
                      <div key={i} className="flex items-center justify-between p-2 text-xs">
                        <div className="flex-1">
                          <div className="font-medium">{sc.keyword}</div>
                          <div className="text-[10px] text-muted-foreground">
                            {sc.sov_label} · 约 {sc.articles} 篇
                          </div>
                        </div>
                        <div className="text-right">
                          <div className="font-medium">¥{displayPrice}</div>
                          <div className="text-[10px] text-muted-foreground">{sc.estimated_uprank_days}</div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* 5 引擎说明 */}
            <div className="rounded border bg-muted/30 p-3 text-xs space-y-1">
              <div className="font-medium">📦 套餐含</div>
              <div>• 选题引擎（每周自动选题）</div>
              <div>• 创作引擎（GEO 优化文章生产）</div>
              <div>• 投放引擎（AI 动态决策媒体）</div>
              <div>• 监控引擎（每日排名 + 检出率追踪）</div>
              <div>• 复盘引擎（每周提炼品牌专属策略，护城河价值）</div>
            </div>

            {/* 协议 */}
            <div className="rounded border border-amber-500/30 bg-amber-500/5 p-3 text-[11px] text-amber-700 dark:text-amber-300">
              <div className="font-medium">⚠️ 重要说明</div>
              <div>• 充值即消费，AI 在套餐内自主执行</div>
              <div>• 余额扣完自动暂停，不过期</div>
              <div>• 想调整 / 加词 / 暂停，告诉 AI 即可</div>
              <div>• 报价 3 天有效</div>
            </div>

            <div className="space-y-2">
              <label className="flex items-start gap-2 text-xs">
                <Checkbox checked={agreementConsent} onCheckedChange={(v) => setAgreementConsent(v === true)} />
                <span>我已阅读并同意《全品牌托管协议 v3.4》</span>
              </label>
              <label className="flex items-start gap-2 text-xs">
                <Checkbox checked={brandVoiceConsent} onCheckedChange={(v) => setBrandVoiceConsent(v === true)} />
                <span>授权平台以我品牌名义生成 GEO 优化内容（24h 内可撤回未发）</span>
              </label>
            </div>

          </div>
        ) : null}

        <DialogFooter className="gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>取消</Button>
          <Button
            onClick={handleConfirm}
            disabled={submitting || !plan || !agreementConsent || !brandVoiceConsent}
          >
            {submitting && <Loader2 className="h-4 w-4 animate-spin mr-1" />}
            充值 ¥{plan?.final_total_yuan || 0} 并启动
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
