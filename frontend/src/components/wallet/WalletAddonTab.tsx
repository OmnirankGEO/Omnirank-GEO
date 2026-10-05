/**
 * Social Studio 钱包扩展包加购 tab V3.1 (F05)
 *
 * 来源: docs/AI-CONTEXT/SOCIAL_STUDIO_PRICING_V3_1_EXECUTION_2026-05-10.md §7.2-7.3
 * 计划: .planning/phases/07-social-studio-subscription/ADDON_OPTIMIZATION_V1.md T4
 *
 * 2026-05-13 重构 (ADDON_OPTIMIZATION_V1 T4):
 *   - 删硬编码 ADDONS 数组 · 改读 GET /api/subscription/addons
 *   - 按 target_quota 分组渲染 (6 维 × 1-2 SKU = 9 包)
 *   - 加 max_per_month 限制提示
 *   - 加 confirm dialog 改 inline 二次确认 (取消 confirm() 浏览器弹窗)
 *   - WalletPage 在 GEO 板块 · 保留 shadcn token 设计语言一致 (不强行 ss-* 化)
 */
import { useEffect, useMemo, useState } from 'react';
import { Loader2, Video, FileText, MessageSquare, Layers, Mic, BarChart3, Check, AlertCircle } from 'lucide-react';
import { toast } from 'sonner';
import { authFetch } from '@/lib/api';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

interface AddonSku {
  addon_id: string;
  target_quota: string;
  amount: number;
  yuan_price: number;
  points_price: number;
  display_name: string;
  description?: string;
  best_for?: string;
  max_per_month?: number;
  min_plan_id?: string;   // Bug H 修: 后端返用于前端 plan rank 判定
}

// plan rank · 与后端 buy_addon line ~889 plan_order 完全一致 (避免不同步)
const PLAN_RANK: Record<string, number> = {
  free: 0,
  personal: 1,
  growth: 2,
  agency: 3,
  partner: 4,
};

// target_quota → 友好分组配置(图标 + 标题)
const QUOTA_GROUP: Record<string, { title: string; icon: typeof Video }> = {
  video_minutes: { title: '视频处理', icon: Video },
  pro_write: { title: '专业写稿', icon: FileText },
  rewrite: { title: '仿写改写', icon: MessageSquare },
  video_breakdown: { title: '拆视频', icon: Layers },
  author_breakdown: { title: '拆博主', icon: Mic },
  review: { title: '内容复盘', icon: BarChart3 },
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

export default function WalletAddonTab() {
  const [addons, setAddons] = useState<AddonSku[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);   // Bug C 修: 三态
  const [buyingId, setBuyingId] = useState<string | null>(null);
  const [confirmAddon, setConfirmAddon] = useState<AddonSku | null>(null);
  // Bug H 修: 拉用户当前订阅判 plan rank · 防 free 用户买不了的包仍点击
  // Bug N (第三轮审) 修: 加 meLoading 防 race · loadAddons 先完成时 currentPlanId=null
  // 已订阅用户被误判 free · 整页显"需订阅" 横幅 + 9 包灰 · 严重 UX 问题
  const [currentPlanId, setCurrentPlanId] = useState<string | null>(null);
  const [isActive, setIsActive] = useState<boolean>(false);
  const [meLoading, setMeLoading] = useState(true);

  const loadAddons = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const res = await authFetch('/api/subscription/addons');
      if (!res.ok) {
        setLoadError(`加载失败 (${res.status})`);
        return;
      }
      const data = await res.json();
      if (data.success && Array.isArray(data.data)) {
        setAddons(data.data);
      } else {
        setLoadError('返回数据格式异常');
      }
    } catch (e: any) {
      setLoadError(e?.message || '网络错误');
    } finally {
      setLoading(false);
    }
  };

  const loadMe = async () => {
    setMeLoading(true);
    try {
      const res = await authFetch('/api/subscription/me');
      const data = await res.json();
      if (data.success) {
        setCurrentPlanId(data.data?.plan_id || null);
        setIsActive(!!data.data?.active);
      } else {
        // Bug N: 不静默 fail · 加 warn 让 console 可见
        console.warn('[WalletAddonTab] /me returned not success:', data);
      }
    } catch (e) {
      console.warn('[WalletAddonTab] /me fetch error:', e);
    } finally {
      setMeLoading(false);
    }
  };

  // 拉所有可买的 addon + 用户订阅
  useEffect(() => {
    void loadAddons();
    void loadMe();
  }, []);

  // 按 target_quota 分组
  const grouped = useMemo(() => {
    const map: Record<string, AddonSku[]> = {};
    for (const a of addons) {
      if (!map[a.target_quota]) map[a.target_quota] = [];
      map[a.target_quota].push(a);
    }
    return map;
  }, [addons]);

  const handleConfirmBuy = async () => {
    if (!confirmAddon) return;
    const addon = confirmAddon;
    setBuyingId(addon.addon_id);
    setConfirmAddon(null);
    try {
      const res = await authFetch('/api/subscription/addon-buy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ addon_id: addon.addon_id, payment_method: 'points' }),
      });
      const json = await res.json();
      if (json.success) {
        toast.success(`${addon.display_name} 加购成功 · +${addon.amount} 单位已发放`);
      } else {
        toast.error(detailToText(json.detail, '加购失败'));
      }
    } catch (e: any) {
      toast.error(`加购失败: ${e?.message || e}`);
    } finally {
      setBuyingId(null);
    }
  };

  // Bug N 修: addons 和 me 都加载完才渲染 · 防 race condition
  if (loading || meLoading) {
    return (
      <div className="flex h-48 items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  // Bug C 修: 加载失败时显错误 + 重试按钮 · 不再静默 fail 误导"暂无扩展包"
  if (loadError) {
    return (
      <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-6 text-center">
        <AlertCircle className="mx-auto mb-2 h-5 w-5 text-destructive" />
        <p className="text-sm text-foreground">加载扩展包失败</p>
        <p className="mt-1 text-xs text-muted-foreground">{loadError}</p>
        <Button variant="outline" size="sm" className="mt-3" onClick={() => void loadAddons()}>
          重试
        </Button>
      </div>
    );
  }

  if (addons.length === 0) {
    return (
      <div className="rounded-xl border border-border/40 bg-muted/20 p-6 text-center text-sm text-muted-foreground">
        <AlertCircle className="mx-auto mb-2 h-5 w-5" />
        暂无可加购的扩展包
      </div>
    );
  }

  const currentRank = currentPlanId ? PLAN_RANK[currentPlanId] ?? 0 : 0;

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-border/40 bg-muted/20 p-3 text-xs leading-relaxed text-muted-foreground">
        <p>· 月卡 quotas 用满后可加扩展包补充本周期算力,本月底随订阅周期清零不滚存</p>
        <p>· 加包用钱包"算力"扣 · 微信付通道即将上线 (T6)</p>
        <p>· 单包月度上限防滥用 · 累加多包 ≈ 升级月卡价时,系统会建议直接升级</p>
      </div>

      {/* Bug H 修: free 用户 (或未订阅) 显引导横幅 · 9 包都灰色 (后端拦也是 403) */}
      {(!isActive || currentPlanId === 'free') && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-xs leading-relaxed">
          <p className="font-medium text-amber-700 dark:text-amber-400">
            ⚠️ 加包功能需先订阅月卡
          </p>
          <p className="mt-1 text-muted-foreground">
            免费体验已含 11 维基础算力 · 订阅 personal ¥49/月(首月 ¥9.9)解锁 9 类增量包。
            <a href="/pricing-plans" className="ml-1 text-foreground underline-offset-2 hover:underline">
              查看套餐 →
            </a>
          </p>
        </div>
      )}

      {Object.entries(grouped).map(([quotaType, skus]) => {
        const groupInfo = QUOTA_GROUP[quotaType] || { title: quotaType, icon: Video };
        const Icon = groupInfo.icon;
        return (
          <div key={quotaType}>
            <div className="mb-3 flex items-center gap-2">
              <Icon className="h-4 w-4 text-muted-foreground" />
              <h3 className="text-sm font-medium">{groupInfo.title}</h3>
              <span className="text-xs text-muted-foreground">({skus.length} 档)</span>
            </div>

            <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
              {skus.map((a) => {
                const isBuying = buyingId === a.addon_id;
                // Bug H 修: plan rank 不够 → 灰色禁用 · 防 403
                const requiredRank = a.min_plan_id ? (PLAN_RANK[a.min_plan_id] ?? 0) : 0;
                const planBlocked = !isActive || currentRank < requiredRank;
                return (
                  <Card
                    key={a.addon_id}
                    className={cn(
                      'border-border/60 transition',
                      planBlocked && 'opacity-50',
                    )}
                  >
                    <CardContent className="space-y-3 p-4">
                      <div className="flex items-baseline justify-between">
                        <span className="text-lg font-semibold">¥{a.yuan_price}</span>
                        <span className="text-xs text-muted-foreground">
                          {a.points_price.toLocaleString()} 算力
                        </span>
                      </div>

                      <div className="flex items-start gap-1.5 text-sm text-foreground/90">
                        <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
                        <span className="leading-relaxed">{a.display_name}</span>
                      </div>

                      {a.best_for && (
                        <p className="text-xs leading-relaxed text-muted-foreground">
                          {a.best_for}
                        </p>
                      )}

                      {a.max_per_month && a.max_per_month < 5 && (
                        <div className="text-[10px] text-amber-600 dark:text-amber-400">
                          本月限购 {a.max_per_month} 次
                        </div>
                      )}

                      {planBlocked ? (
                        <Button
                          size="sm"
                          variant="outline"
                          className={cn('w-full')}
                          disabled
                          title={`需 ${a.min_plan_id || 'personal'} 套餐才能购买`}
                        >
                          需订阅 {a.min_plan_id || 'personal'}
                        </Button>
                      ) : (
                        <Button
                          size="sm"
                          variant="outline"
                          className={cn('w-full')}
                          disabled={isBuying}
                          onClick={() => setConfirmAddon(a)}
                        >
                          {isBuying ? <Loader2 className="h-4 w-4 animate-spin" /> : `加购 ¥${a.yuan_price}`}
                        </Button>
                      )}
                    </CardContent>
                  </Card>
                );
              })}
            </div>
          </div>
        );
      })}

      {/* 二次确认弹窗 (替代 window.confirm 浏览器原生) */}
      {confirmAddon && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
          <Card className="w-[90%] max-w-sm border-border bg-background">
            <CardContent className="space-y-3 p-5">
              <h3 className="font-medium">确认加购</h3>
              <div className="space-y-1 text-sm">
                <p>{confirmAddon.display_name}</p>
                <p className="text-xs text-muted-foreground">
                  消耗 {confirmAddon.points_price.toLocaleString()} 算力(¥{confirmAddon.yuan_price})·
                  +{confirmAddon.amount} 单位 · 本月有效
                </p>
              </div>
              <div className="flex gap-2 pt-1">
                <Button variant="outline" size="sm" className="flex-1" onClick={() => setConfirmAddon(null)}>
                  取消
                </Button>
                <Button size="sm" className="flex-1" onClick={handleConfirmBuy}>
                  确认加购
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
