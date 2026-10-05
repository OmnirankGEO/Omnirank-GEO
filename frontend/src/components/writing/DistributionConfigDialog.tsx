/**
 * v2.10 文章方向配比器 · 三态切换抽屉(老板 9 条 + Codex 4 轮复审锁定)
 *
 * 三态:
 *   - 系统推荐(默认):后端按行业自动分配 · 不显示具体篇数(防混)
 *   - 统一方向:全 N 篇一个文体(快捷模式 · 复用 v2.8 force_chinese 路径)
 *   - 自定义配比:六类文体 × 篇数 stepper · 实时校验合计 = configurable_count
 *
 * 老板拍点(全 A):
 *   1. UI 高级入口:小字 link 折叠(主调用方 WritingHall · 此组件被动渲染)
 *   2. ranking 不暴露:六类文体 family（无榜单工程词）
 *   3. 命名:user_choice_distribution
 *   4. 旧 endpoint hard 400(此处不调旧 endpoint · 调新 direction-plan / apply-distribution)
 *
 * Codex 锁定:
 *   - 0 工程词文案(0 LLM / ranking / style_code / content_ratios)
 *   - 自定义模式说明:"自定义配比只在六类文体内分配，合计必须等于可分配篇数"
 *   - 已生成场景双按钮:仅调整正文方向(不扣费) + 同步重写标题(扣 N×80)
 *   - 实际可重写 N 动态计算(排除 fixed/active/manual)
 */

import { useState, useEffect, useMemo } from 'react';
import { distributableDirections } from '@/pages/Writing/articleDirections';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { authFetch } from '@/lib/api';
import { toast } from 'sonner';
import { Loader2, Minus, Plus } from 'lucide-react';

/*
 * 🔴 [#185] 原来这里是**手写的第二份**方向清单(WritingHall 里还有一份)。
 *    #185 要加「防御型(公司词)」,只改一处就会出现"配比里能选、单篇下拉里没有"。
 *    改成从唯一清单派生。`auto`(系统推荐)不进配比器 —— 配比是给每一类分篇数,
 *    "系统推荐"不是一类,所以用 `distributableDirections()` 而不是全量。
 */
const USER_CHOICE_LABELS: { value: string; label: string; emoji: string }[] =
    distributableDirections().map(d => ({ value: d.value, label: d.label, emoji: d.emoji }));

type Mode = 'recommended' | 'uniform' | 'custom';

interface DirectionPlanResponse {
    total: number;
    fixed_count: number;
    active_locked_count: number;
    manual_locked_count: number;
    configurable_count: number;
    drift_warning: boolean;
    industry: string;
    recommended: Record<string, number>;
    user_choice_options: { choice: string; label: string; count: number; disabled: boolean }[];
}

interface Props {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    quoteId: number;
    hasExistingTitles: boolean;  // 已生成场景标记(双按钮模式)
    onApplied?: () => void;  // 应用成功后回调(刷新列表)
    // v2.10.2 P0-1:未生成 topics 时 · custom 配比保存到 WritingHall state · 等"批量生成标题"一并提交
    onSavePendingDistribution?: (distribution: Record<string, number> | null) => void;
}

export function DistributionConfigDialog({
    open,
    onOpenChange,
    quoteId,
    hasExistingTitles,
    onApplied,
    onSavePendingDistribution,
}: Props) {
    const [loading, setLoading] = useState(false);
    const [applying, setApplying] = useState(false);
    const [mode, setMode] = useState<Mode>('recommended');
    const [planData, setPlanData] = useState<DirectionPlanResponse | null>(null);
    const [customCounts, setCustomCounts] = useState<Record<string, number>>({});

    // 加载 direction-plan(开 dialog 时)
    useEffect(() => {
        if (!open || !quoteId) return;
        const loadPlan = async () => {
            setLoading(true);
            try {
                const res = await authFetch(`/api/writing/projects/${quoteId}/direction-plan`);
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    toast.error(err.detail || '加载配比信息失败');
                    return;
                }
                const data: DirectionPlanResponse = await res.json();
                setPlanData(data);
                // 默认填入推荐分布到 custom(用户切到自定义时已有起点)
                setCustomCounts({ ...data.recommended });
            } catch (e) {
                toast.error('加载配比信息网络异常');
            } finally {
                setLoading(false);
            }
        };
        void loadPlan();
    }, [open, quoteId]);

    // 自定义模式合计
    const customSum = useMemo(() => {
        return Object.values(customCounts).reduce((acc, n) => acc + (n || 0), 0);
    }, [customCounts]);

    const configurableCount = planData?.configurable_count ?? 0;
    const customValid = customSum === configurableCount;

    // 实际可重写数(用于扣费提示 · sync_rewrite_titles=True 时)
    // configurable_count 已排除 fixed/active/manual · 故 N = configurable_count
    const actualRewriteCount = configurableCount;
    // 调整篇数
    const adjustCount = (choice: string, delta: number) => {
        setCustomCounts(prev => {
            const next = { ...prev, [choice]: Math.max(0, (prev[choice] || 0) + delta) };
            return next;
        });
    };

    // 应用配比
    // v2.10.1 Codex 复审 P0-3 修:mode='recommended' 不写过滤后的 distribution
    //   原行为:写入过滤后 distribution(已过滤 ranking)→ 系统保留方向永久消失
    //   修法:调专门 reset-to-recommended endpoint · 清非 manual 的 user_choice + source
    //         ArticleWriter 后续走 style_ratios 加权抽 · 含系统保留方向(ranking)
    const apply = async (syncRewriteTitles: boolean) => {
        if (!planData) return;

        setApplying(true);
        try {
            if (mode === 'recommended') {
                // v2.10.3 Codex 三审 P1 修:未生成 topics 时必须清 pendingDistribution
                //   否则用户保存了自定义配比 → 改恢复系统推荐 · 后端 reset 空操作 +
                //   前端 pendingDistribution 仍留 → "批量生成标题"还按旧配比走 → 用户感知错位
                if (!hasExistingTitles) {
                    onSavePendingDistribution?.(null);  // 清前端 pending state
                    toast.success('已恢复为系统推荐 · 批量生成将走系统比例');
                    onOpenChange(false);
                    return;
                }
                // 已生成 topics 场景:调后端 reset endpoint(清非 manual user_choice + source)
                const res = await authFetch(`/api/writing/projects/${quoteId}/reset-to-recommended`, {
                    method: 'POST',
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    toast.error(err.detail || '恢复系统推荐失败');
                    return;
                }
                const data = await res.json();
                toast.success(data.message || '已恢复为系统推荐');
                onOpenChange(false);
                onApplied?.();
                return;
            }

            if (mode === 'uniform') {
                // 统一方向不通过此接口处理 · 走 v2.8 generate-titles + user_style(WritingHall 已有)
                toast('统一方向请使用页面顶部"全部:[文体]"快捷模式');
                return;
            }

            // custom mode
            if (!customValid) {
                toast.error(`合计 ${customSum}/${configurableCount} · 必须等于可分配数`);
                return;
            }
            const distribution = { ...customCounts };
            // 过滤 0(后端不接受 0 · validate_user_choice_distribution 已严校验)
            Object.keys(distribution).forEach(k => {
                if (distribution[k] === 0) delete distribution[k];
            });

            // v2.10.2 P0-1 修:未生成 topics 时不调 apply-distribution(空操作)
            //   保存到 WritingHall pending state · 等"批量生成标题"时传给 generate-titles
            if (!hasExistingTitles) {
                onSavePendingDistribution?.(distribution);
                toast.success('配比已保存 · 点"批量生成标题"按此分配');
                onOpenChange(false);
                return;
            }

            const res = await authFetch(`/api/writing/projects/${quoteId}/apply-distribution`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    user_choice_distribution: distribution,
                    sync_rewrite_titles: syncRewriteTitles,
                }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                toast.error(err.detail || '应用配比失败');
                return;
            }
            const data = await res.json();
            toast.success(data.message || '配比已应用');
            onOpenChange(false);
            onApplied?.();
        } catch (e) {
            toast.error('应用配比网络异常');
        } finally {
            setApplying(false);
        }
    };

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
                <DialogHeader>
                    <DialogTitle>文章方向配比</DialogTitle>
                    <DialogDescription>
                        {loading ? '加载中...' : planData ? (
                            <span>
                                共 <b>{planData.total}</b> 篇
                                {planData.fixed_count + planData.active_locked_count + planData.manual_locked_count > 0 && (
                                    <span className="text-muted-foreground text-xs ml-2">
                                        (可分配 <b>{configurableCount}</b> 篇 ·
                                        {planData.fixed_count > 0 && ` 企业介绍 ${planData.fixed_count}`}
                                        {planData.active_locked_count > 0 && ` · 进行中 ${planData.active_locked_count}`}
                                        {planData.manual_locked_count > 0 && ` · 单篇已锁定 ${planData.manual_locked_count}`}
                                        )
                                    </span>
                                )}
                                {planData.drift_warning && (
                                    <span className="text-amber-600 text-xs ml-2">⚠ 数据偏移,以实际篇数为准</span>
                                )}
                            </span>
                        ) : null}
                    </DialogDescription>
                </DialogHeader>

                {loading ? (
                    <div className="flex items-center justify-center py-12">
                        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                    </div>
                ) : planData ? (
                    <div className="space-y-4">
                        {/* 三态 radio */}
                        <div className="space-y-2">
                            <label className="flex items-start gap-3 p-3 rounded border border-border hover:bg-muted/40 cursor-pointer">
                                <input
                                    type="radio"
                                    name="mode"
                                    value="recommended"
                                    checked={mode === 'recommended'}
                                    onChange={() => setMode('recommended')}
                                    className="mt-1"
                                />
                                <div className="flex-1">
                                    <div className="font-medium">🎯 系统推荐(默认)</div>
                                    <div className="text-xs text-muted-foreground mt-1">
                                        {/* v2.10.5 Codex 五审 P1 文案修:v2.10.4 拍 B 后首次生成不再创建企业介绍 fixed slot · 不再承诺"含企业介绍" */}
                                        按系统比例自动分配 {planData.total} 篇 · 按行业策略生成不同方向
                                    </div>
                                </div>
                            </label>

                            <label className="flex items-start gap-3 p-3 rounded border border-border hover:bg-muted/40 cursor-pointer opacity-60">
                                <input
                                    type="radio"
                                    name="mode"
                                    value="uniform"
                                    checked={mode === 'uniform'}
                                    onChange={() => setMode('uniform')}
                                    className="mt-1"
                                />
                                <div className="flex-1">
                                    <div className="font-medium">📌 统一方向</div>
                                    <div className="text-xs text-muted-foreground mt-1">
                                        全 {configurableCount} 篇用同一方向 · 请使用页面顶部"全部:[方向]"快捷模式
                                    </div>
                                </div>
                            </label>

                            <label className="flex items-start gap-3 p-3 rounded border border-border hover:bg-muted/40 cursor-pointer">
                                <input
                                    type="radio"
                                    name="mode"
                                    value="custom"
                                    checked={mode === 'custom'}
                                    onChange={() => setMode('custom')}
                                    className="mt-1"
                                />
                                <div className="flex-1">
                                    <div className="font-medium">⚙️ 自定义配比</div>
                                    <div className="text-xs text-muted-foreground mt-1">
                                        按方向手动设置篇数 · 合计 = 可分配数
                                    </div>
                                </div>
                            </label>
                        </div>

                        {/* 自定义配比 stepper */}
                        {mode === 'custom' && (
                            <div className="border border-border rounded p-3 space-y-2">
                                <div className="text-xs text-amber-600 mb-2 pb-2 border-b border-border">
                                    自定义配比只在六类文体内分配 · 合计必须等于可分配篇数
                                </div>
                                {USER_CHOICE_LABELS.map(opt => (
                                    <div key={opt.value} className="flex items-center justify-between gap-2">
                                        <div className="flex-1 text-sm">
                                            <span className="mr-2">{opt.emoji}</span>
                                            {opt.label}
                                        </div>
                                        <div className="flex items-center gap-1">
                                            <button
                                                type="button"
                                                onClick={() => adjustCount(opt.value, -1)}
                                                className="h-7 w-7 inline-flex items-center justify-center rounded border border-border hover:bg-muted disabled:opacity-30"
                                                disabled={(customCounts[opt.value] || 0) <= 0 || applying}
                                            >
                                                <Minus className="h-3.5 w-3.5" />
                                            </button>
                                            <span className="w-8 text-center text-sm font-mono">
                                                {customCounts[opt.value] || 0}
                                            </span>
                                            <button
                                                type="button"
                                                onClick={() => adjustCount(opt.value, +1)}
                                                className="h-7 w-7 inline-flex items-center justify-center rounded border border-border hover:bg-muted disabled:opacity-30"
                                                disabled={customSum >= configurableCount || applying}
                                            >
                                                <Plus className="h-3.5 w-3.5" />
                                            </button>
                                        </div>
                                    </div>
                                ))}
                                <div className={`mt-3 pt-2 border-t border-border text-sm font-medium ${customValid ? 'text-green-600' : 'text-amber-600'}`}>
                                    合计 {customSum} / {configurableCount} 篇 {customValid ? '✓' : '(需相等)'}
                                </div>
                            </div>
                        )}

                        {/* v2.10.2 Codex 复审 P1-5 修:系统推荐 mode 不再展示过滤后分布(误导用户) */}
                        {/* 改为说明:点应用会清除批量配比 · 让系统按完整 ratio 自动分配(含系统保留方向) */}
                        {mode === 'recommended' && (
                            <div className="border border-border rounded p-3 text-xs text-muted-foreground space-y-1">
                                <div>点击"应用"会将所有非手动锁定的 topic 恢复为系统推荐 · 完整按系统比例自动分配</div>
                                <div>已通过单篇下拉手动设置的 topic 会保留不变</div>
                            </div>
                        )}
                    </div>
                ) : null}

                <DialogFooter className="gap-2 flex-col sm:flex-row sm:justify-between">
                    <Button variant="outline" onClick={() => onOpenChange(false)} disabled={applying}>
                        取消
                    </Button>
                    {/* v2.10.2 Codex 复审 P1-5 修:recommended mode 单按钮(只清非 manual · 不扣费)
                       custom mode + 已生成场景才显示双按钮(仅调整方向 / 同步重写)
                       uniform mode disabled(指引用户用页面顶部 dropdown)
                       custom mode + 未生成场景:单按钮"应用配比"(distribution 写入 · 不重写) */}
                    {mode === 'recommended' && (
                        <Button
                            onClick={() => apply(false)}
                            disabled={applying}
                            title="清除非手动锁定的方向标记 · 让系统按完整 ratio 自动分配"
                        >
                            {applying ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : null}
                            恢复为系统推荐(不消耗算力)
                        </Button>
                    )}
                    {mode === 'uniform' && (
                        <Button disabled title="请使用页面顶部'全部:[文体]'快捷模式">
                            请用顶部 dropdown 快捷模式
                        </Button>
                    )}
                    {mode === 'custom' && (
                        hasExistingTitles ? (
                            <div className="flex gap-2 flex-col sm:flex-row">
                                <Button
                                    variant="outline"
                                    onClick={() => apply(false)}
                                    disabled={applying || !customValid}
                                    title="只改 DB 方向 · 不动现有标题文本"
                                >
                                    {applying ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : null}
                                    仅调整正文方向(不消耗算力)
                                </Button>
                                <Button
                                    onClick={() => apply(true)}
                                    disabled={applying || !customValid || actualRewriteCount === 0}
                                    title={`预计重写 ${actualRewriteCount} 个标题`}
                                >
                                    {applying ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : null}
                                    同步重写标题(预计重写 {actualRewriteCount} 个)
                                </Button>
                            </div>
                        ) : (
                            <Button
                                onClick={() => apply(false)}
                                disabled={applying || !customValid}
                                title="保存自定义配比 · 待你点'批量生成标题'按此配比生成"
                            >
                                {applying ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : null}
                                保存配比(待批量生成时按此分配)
                            </Button>
                        )
                    )}
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
