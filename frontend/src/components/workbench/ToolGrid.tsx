/**
 * ToolGrid — 11 工具卡(CTO-15.20 桥接重设计 · A.5)
 *
 * 老板红线(2026-04-28):
 * - M3 = 入口导视台 / 旧版 = 工作面
 * - 11 工具卡跳旧版 · 不重写
 * - 第 4 卡"看 19 词监测" → /monitoring?brand_id=X 老板核心痛点
 *
 * 11 工具映射(handoff §A.5):
 *   1. 看诊断报告 → /diagnosis/report/:diagnosisId 旧版
 *   2. 看 / 改方案书 → /m3/quote/:quoteId/proposal M3 留
 *   3. 选词管理 → /m3/selection/:quoteId M3 留
 *   4. 看 19 词监测 → /monitoring?brand_id=X 旧版(P0 关键词显示)
 *   5. 趋势报表 → /monitoring?brand_id=X&view=trend 旧版
 *   6. 写文章 → /writing?brand_id=X 旧版
 *   7. 发布管理 → /publish?brand_id=X 旧版
 *   8. 月度报告 → /reports?brand_id=X 旧版
 *   9. 客户档案 → /my-clients/:brandId 旧版
 *   10. 客户门户 → 复制 portal_token URL
 *   11. 操作日志 → 折叠区内 M3 信号时间线 (无独立路由)
 */
import { useNavigate } from 'react-router-dom';
import {
    FileBarChart2, FileSpreadsheet, ListChecks, LineChart, TrendingUp,
    PencilLine, Megaphone, FileText, User, Link as LinkIcon, ScrollText,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';

interface ToolItem {
    key: string;
    group: 'sales' | 'delivery' | 'asset';
    icon: LucideIcon;
    title: string;
    subtitle: string;
    target?: string;
    actionKey?: 'copy_portal_link' | 'scroll_to_logs';
    /** 是否旧版工作面(用于桥接埋点) */
    bridgeToLegacy?: boolean;
    accent?: 'default' | 'primary';
    disabled?: boolean;
    disabledReason?: string;
}

export interface ToolGridProps {
    brandId: number;
    /** 当前客户 quote_id · 决定 #2 #3 卡是否可点 */
    quoteId?: number | null;
    /** 当前客户 latest diagnosis_id · 决定 #1 卡是否可点 */
    diagnosisId?: number | null;
    /** 客户门户 token · #10 卡复制用 */
    portalToken?: string | null;
    /** 操作日志区滚动锚 callback · #11 卡 */
    onScrollToLogs?: () => void;
    className?: string;
}

export function ToolGrid({
    brandId,
    quoteId,
    diagnosisId,
    portalToken,
    onScrollToLogs,
    className,
}: ToolGridProps) {
    const navigate = useNavigate();

    const trackBridge = (target: string, key: string) => {
        if (typeof window === 'undefined') return;
        // [WO_260] 原先按 `!target.startsWith('/m3/')` 判「是不是跳旧版」—— 下面 11 张卡的 target
        //   已全是在役页,那个条件恒真;M3 路由随 E3 删,不再留 `/m3/` 前缀判断。
        const w = window as unknown as { __m3Analytics?: { track?: (e: string, p?: unknown) => void } };
        w.__m3Analytics?.track?.('tool_grid_jump', {
            brand_id: brandId,
            tool_key: key,
            target,
        });
    };

    const tools: ToolItem[] = [
        {
            key: 'diagnosis_report',
            group: 'sales',
            icon: FileBarChart2,
            title: '看诊断报告',
            subtitle: 'GEO 评分 + 4 引擎实测 + 竞品分析',
            target: diagnosisId ? `/diagnosis/report/${diagnosisId}` : undefined,
            bridgeToLegacy: true,
            disabled: !diagnosisId,
            disabledReason: '该客户还没做过诊断',
        },
        {
            key: 'view_proposal',
            group: 'sales',
            icon: FileSpreadsheet,
            title: '看 / 改报价方案',
            subtitle: '三档算价 · 加词 · 改主题包',
            target: quoteId ? `/pricing?brand_id=${brandId}&quote_id=${quoteId}` : undefined,
            bridgeToLegacy: true,
            disabled: !quoteId,
            disabledReason: '该客户还没生成报价方案',
            accent: 'primary',
        },
        {
            key: 'selection_manage',
            group: 'sales',
            icon: ListChecks,
            title: '选词与确认',
            subtitle: '发选词链 + 看勾词进度',
            target: quoteId ? `/pricing?brand_id=${brandId}&quote_id=${quoteId}` : undefined,
            bridgeToLegacy: true,
            disabled: !quoteId,
            disabledReason: '该客户还没生成报价方案',
        },
        {
            key: 'monitoring',
            group: 'delivery',
            icon: LineChart,
            title: '看监测排名',
            subtitle: '4 引擎每日跑 · 词条达标率(老板核心)',
            target: `/monitoring?brand_id=${brandId}`,
            bridgeToLegacy: true,
            accent: 'primary',
        },
        {
            key: 'monitoring_trend',
            group: 'delivery',
            icon: TrendingUp,
            title: '趋势报表',
            subtitle: '14 天排名变化 · 上榜曲线',
            target: `/monitoring?brand_id=${brandId}&view=trend`,
            bridgeToLegacy: true,
        },
        {
            key: 'writing',
            group: 'delivery',
            icon: PencilLine,
            title: '写文章',
            subtitle: '写作大厅 · 审稿 · 重写',
            target: `/writing?brand_id=${brandId}`,
            bridgeToLegacy: true,
        },
        {
            key: 'publish',
            group: 'delivery',
            icon: Megaphone,
            title: '发布管理',
            subtitle: '知乎 / 百家号 / 头条 + 截图证据',
            target: `/publish?brand_id=${brandId}`,
            bridgeToLegacy: true,
        },
        {
            key: 'reports',
            group: 'delivery',
            icon: FileText,
            title: '月度报告',
            subtitle: '周 / 月 / 季 / 年报 · PDF 导出',
            target: `/reports?brand_id=${brandId}`,
            bridgeToLegacy: true,
        },
        {
            key: 'client_profile',
            group: 'asset',
            icon: User,
            title: '客户档案',
            subtitle: '基础资料 / 知识库 / 写作资料',
            target: `/my-clients/${brandId}`,
            bridgeToLegacy: true,
        },
        {
            key: 'portal_link',
            group: 'asset',
            icon: LinkIcon,
            title: '客户门户',
            subtitle: '复制 portal 链接发给客户',
            actionKey: 'copy_portal_link',
            disabled: !portalToken,
            disabledReason: '该客户还没生成门户 token',
        },
        {
            key: 'audit_logs',
            group: 'asset',
            icon: ScrollText,
            title: '操作日志',
            subtitle: '改价 / 加词 / 激活 时间线',
            actionKey: 'scroll_to_logs',
        },
    ];

    const handleClick = (t: ToolItem) => {
        if (t.disabled) {
            if (t.disabledReason) toast.info(t.disabledReason);
            return;
        }
        if (t.actionKey === 'copy_portal_link' && portalToken) {
            const url = `${window.location.origin}/portal/${portalToken}`;
            navigator.clipboard.writeText(url).then(
                () => toast.success('客户门户链接已复制'),
                () => toast.error('复制失败'),
            );
            return;
        }
        if (t.actionKey === 'scroll_to_logs') {
            if (onScrollToLogs) onScrollToLogs();
            else toast.info('操作日志在历史区折叠面板里');
            return;
        }
        if (t.target) {
            trackBridge(t.target, t.key);
            navigate(t.target);
        }
    };

    const groups: Array<{ key: ToolItem['group']; label: string }> = [
        { key: 'sales', label: '销售转化' },
        { key: 'delivery', label: '内容交付' },
        { key: 'asset', label: '客户资产' },
    ];

    return (
        <div className={cn('space-y-4', className)} role="list" aria-label="客户工具网格">
            {groups.map((group) => {
                const items = tools.filter((t) => t.group === group.key);
                return (
                    <section key={group.key} className="space-y-2">
                        <h4 className="text-[11px] font-medium text-muted-foreground">{group.label}</h4>
                        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
                            {items.map((t) => {
                                const Icon = t.icon;
                                return (
                                    <button
                                        key={t.key}
                                        type="button"
                                        onClick={() => handleClick(t)}
                                        disabled={t.disabled}
                                        className={cn(
                                            'min-h-[76px] rounded-lg border bg-card p-3 text-left transition-colors sm:min-h-[88px]',
                                            'hover:bg-muted/40 hover:border-foreground/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                                            t.disabled && 'opacity-50 cursor-not-allowed hover:bg-card hover:border-border',
                                        )}
                                        role="listitem"
                                        aria-label={`${t.title} · ${t.subtitle}`}
                                        title={t.disabled ? t.disabledReason : undefined}
                                    >
                                        <div className="flex items-start gap-2.5">
                                            <Icon className={cn('h-4 w-4 shrink-0 mt-0.5', t.accent === 'primary' ? 'text-primary' : 'text-muted-foreground')} aria-hidden />
                                            <div className="min-w-0 space-y-0.5">
                                                <div className="flex items-center gap-1.5 text-sm font-medium text-foreground">
                                                    {t.title}
                                                    {t.accent === 'primary' && (
                                                        <span className="rounded-full bg-primary/10 px-1.5 py-px text-[10px] font-medium leading-none text-primary">
                                                            推荐
                                                        </span>
                                                    )}
                                                </div>
                                                <div className="line-clamp-2 text-[11px] leading-snug text-muted-foreground">
                                                    {t.subtitle}
                                                </div>
                                            </div>
                                        </div>
                                    </button>
                                );
                            })}
                        </div>
                    </section>
                );
            })}
        </div>
    );
}

interface ToolGridCardProps {
    brandId: number;
    quoteId?: number | null;
    diagnosisId?: number | null;
    portalToken?: string | null;
    onScrollToLogs?: () => void;
    title?: string;
    className?: string;
}

/** 包装版:Card 外壳 + ToolGrid · 给客户详情页折叠区"这个客户还能做什么"用 */
export function ToolGridCard({
    brandId,
    quoteId,
    diagnosisId,
    portalToken,
    onScrollToLogs,
    title = '这个客户还能做什么',
    className,
}: ToolGridCardProps) {
    return (
        <Card className={className}>
            <CardContent className="p-4 space-y-3">
                {title && (
                  <div className="space-y-1">
                    <h3 className="text-sm font-semibold text-foreground inline-flex items-center gap-1.5">
                        {title}
                    </h3>
                    <p className="text-[11px] text-muted-foreground">
                        需要深挖时再打开,不影响上方主流程。
                    </p>
                  </div>
                )}
                <ToolGrid
                    brandId={brandId}
                    quoteId={quoteId}
                    diagnosisId={diagnosisId}
                    portalToken={portalToken}
                    onScrollToLogs={onScrollToLogs}
                />
            </CardContent>
        </Card>
    );
}
