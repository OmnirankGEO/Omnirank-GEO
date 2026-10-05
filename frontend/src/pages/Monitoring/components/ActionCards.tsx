/**
 * Quick action cards grid — 三级身份可见：all / agent / admin
 * CTO-13.0 → [#222 a1b 2026-09-15] 交付工具对**全员**开放(服务商与普通账号一致),
 * admin 保留危险操作;「数据回退」这一项也仍然挡着(破坏性,待 Owner)。
 * [2026-06-04 视觉] 重排为「4 个一级动作 + 更多工具(折叠)」· 0 功能改动 · onClick/可见性逐字保留
 */
import { useState } from 'react';
import { Card, CardContent } from '@/components/ui/card';
import { Plus, TrendingUp, Key, FileText, ClipboardList, RotateCcw, Clock, ShieldAlert, ArchiveRestore, CalendarClock, FileBarChart, FolderOpen, Wrench, ChevronDown } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { LazyFeatureTooltip as FeatureTooltip } from '@/components/onboarding/LazyFeatureTooltip';
import { HelpHint } from '@/components/onboarding/HelpHint';
import { isSandboxActive } from '@/sandbox/sandboxState';
import { useTutorialStage } from '@/sandbox/tutorialStage';
import { cn } from '@/lib/utils';

interface ActionCardsProps {
    scheduleEnabled: boolean;
    currentServiceDays: number | null;
    currentServiceStart: string | null;
    archivesCount: number;
    selectedClient: string;
    currentBrandId: number | undefined;
    /**
     * 可见性:admin 看全部 · 服务商与普通账号**看到的一样**,
     * 只差一张「数据回退」(破坏性动作,待 Owner)。
     *
     * 🔴 [#222 a1b] 这行原文说服务商看得比普通账号多 —— 改完之后它就**成了假话**。
     *    (这里**故意不原样引用**那句原文:术语闸和过期注释闸都在扫这个文件,
     *     引用一遍会把闸自己打红 —— 本轮我已经栽过三次。)
     *    过期注释不会红,只会误导下一个人:
     *    本轮已经被这类注释坑过两次(Layout 那句「Dashboard 已裁剪」)。
     */
    isAdmin: boolean;
    onAddKeyword: () => void;
    onTrend: () => void;
    onToken: () => void;
    onPublication: () => void;
    onLogs: () => void;
    onReportGen: () => void;
    onRollback: () => void;
    onSchedule: () => void;
    onServiceConfig: () => void;
    onClearData: () => void;
    onArchives: () => void;
}

/** 简单动作卡(无 tooltip 的通用卡) */
function ActionTile({ icon: Icon, iconBg, iconColor, title, desc, onClick, titleColor }: {
    icon: any; iconBg: string; iconColor: string; title: string; desc: string; onClick: () => void; titleColor?: string;
}) {
    return (
        <Card className="bg-card border border-border rounded-xl cursor-pointer transition-colors hover:border-foreground/20" onClick={onClick}>
            <CardContent className="flex items-center gap-3 p-4">
                <div className={cn('h-10 w-10 rounded-lg flex items-center justify-center shrink-0', iconBg)}>
                    <Icon className={cn('h-5 w-5', iconColor)} />
                </div>
                <div className="min-w-0">
                    <p className={cn('font-medium', titleColor || 'text-foreground')}>{title}</p>
                    <p className="text-xs text-muted-foreground">{desc}</p>
                </div>
            </CardContent>
        </Card>
    );
}

export function ActionCards({
    scheduleEnabled,
    currentServiceDays,
    archivesCount,
    isAdmin,
    onAddKeyword,
    onTrend,
    onToken,
    onPublication,
    onLogs,
    onReportGen,
    onRollback,
    onSchedule,
    onServiceConfig,
    onClearData,
    onArchives,
}: ActionCardsProps) {
    const navigate = useNavigate();
    /*
     * 🔴 [#222 a1b] Owner 09-15:普通账号权限 = 服务商(**除经营后台**)。
     *    这一段原来用 `agentOrAdmin` 挡掉四张卡,而它们是**交付工具**
     *    (上一版注释自己就是这么写的),不是经营后台(进货/定价/结算/推广/协议)。
     *    ⇒ 交付设置 / 客户实时数据链接 / 媒体投放 / 操作日志 **对全员开放**。
     *    「操作日志」尤其:它给非 admin 看的本来就只有**自己的**操作。
     *
     *    🔴 [#222 a1b' · 2026-09-16] 「数据回退」原来还挡着(`isAdmin || isAgent`),
     *    Owner 已确认放开:删的是**自己账号的**监测历史,与服务商同权;
     *    `RollbackDialog` 里「此操作不可恢复」那句原话保留,风险由提示承担、不由身份承担。
     *    ⇒ 这里不再有「是不是服务商」这一维,`isAgent` 整个 prop 一并摘掉
     *      —— 留着一个没人读的身份 prop,下一个人会以为这儿还有分流。
     *    仍然只给 admin 的是 `adminOnly`(加词 / 清除数据 / 数据归档),那是**系统参数**,不是身份差。
     */
    // adminOnly:    仅 admin(危险操作 / 系统参数)
    const adminOnly = isAdmin;
    const [showMore, setShowMore] = useState(false);
    // 沙盒教程收尾步: 高亮 Token 管理卡片, 引导给客户发实时数据链接
    const tutorialStage = useTutorialStage();
    const tokenCardSpotlight = isSandboxActive() && tutorialStage === 'step4-token-card';

    // 更多工具数量(用于按钮标注)· 按可见性算
    const moreCount = 2 /* 趋势报表 + 报告管理 */
        + 2 /* 媒体投放 + 操作日志 —— [#222 a1b] 全员可见 */
        + 1 /* 数据回退 —— [#222 a1b'] Owner 已放开,全员可见 */
        + (adminOnly ? 2 : 0) /* 添加关键词 + 清除数据 */
        + (adminOnly && archivesCount > 0 ? 1 : 0); /* 数据归档 */

    return (
        <div className="space-y-3">
            {/* === 一级动作(4 个 · 下一步该做什么) === */}
            <div>
                <p className="mb-2 text-xs font-medium text-muted-foreground">下一步操作</p>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    {/* 交付设置(原服务期设置)· [#222 a1b] 全员 ·
                        移到首位(老板 2026-06-05:交付基础设置优先) */}
                    {/* [客户反馈⑥ 2026-08-09] 旧文案 "监测/门户有效期 N 天" 把
                        **累计达标天数配额**(quotes.service_days,单位是"达标天")
                        说成了"有效期"(听起来是日历天)。同一个界面上另一处显示的
                        "服务期至 YYYY-MM-DD" 才是日历,两套时间语义互相打架 ——
                        老板看到的"还有一年"就是这么来的。这里只改措辞,不改任何取值。
                        🔴 没写成工单建议的「累计达标 N/M 天」:客户端层面**没有 N**
                        (compliant_days 是逐词的,没有单一的客户级累计值),
                        凑一个聚合出来就是编数字。 */}
                    {/* 交付设置 · [#222 a1b] 全员 */}
                    <ActionTile icon={CalendarClock} iconBg="bg-amber-500/10" iconColor="text-amber-600" title="交付设置" desc={currentServiceDays ? `累计达标配额 ${currentServiceDays}天` : '设置达标周期'} onClick={onServiceConfig} />
                    {/* 生成报告 · 全员 */}
                    <ActionTile icon={FileBarChart} iconBg="bg-green-500/10" iconColor="text-green-600" title="生成报告" desc="周报/月报/季报/年报" onClick={onReportGen} />
                    {/* 定时监测 · 全员(带 HelpHint) */}
                    <Card className="bg-card border border-blue-500/20 rounded-xl cursor-pointer transition-colors hover:border-blue-500/40" onClick={onSchedule}>
                        <CardContent className="flex items-center gap-3 p-4">
                            <div className="h-10 w-10 rounded-lg bg-blue-500/10 flex items-center justify-center shrink-0">
                                <Clock className="h-5 w-5 text-blue-600" />
                            </div>
                            <div className="min-w-0">
                                <p className="font-medium text-foreground flex items-center gap-1">
                                    定时监测
                                    <HelpHint title="定时监测是干嘛的?">
                                        开启后系统<b>每天自动</b>去 AI 里搜你的词、记录出现率, 不用你手动点"监测"。
                                        <br />出现率是天天在变的, 开了定时监测才能<b>持续追踪趋势</b>、及时发现哪个词掉了。
                                        <br />默认关闭, 按需自己开;每次自动跑也按规则计费。
                                    </HelpHint>
                                </p>
                                <p className="text-xs text-muted-foreground">
                                    {scheduleEnabled ? '每天自动执行' : '设置自动监测'}
                                </p>
                            </div>
                        </CardContent>
                    </Card>
                    {/* 客户门户 · [#222 a1b] 全员(给客户发实时数据链接 = 交付动作,不是经营后台)
                        带 FeatureTooltip 沙盒高亮 */}
                    <FeatureTooltip
                        featureId="sandbox_step4_token_card"
                        stepId="first_monitoring"
                        title="最后一步:让客户自己看实时数据"
                        content={'你不用每次手动导数据给客户。点这里生成一个客户专属的"白标链接", 发给客户, 他打开就能随时看自己词的实时出现率看板(不用登录、看不到别的客户)。\n\n点这张卡片打开看看。'}
                        side="bottom"
                        wrapClassName="relative block"
                        disabled={!tokenCardSpotlight}
                    >
                        <Card className="bg-card border border-border rounded-xl cursor-pointer transition-colors hover:border-foreground/20" onClick={onToken}>
                            <CardContent className="flex items-center gap-3 p-4">
                                <div className="h-10 w-10 rounded-lg bg-purple-500/10 flex items-center justify-center shrink-0">
                                    <Key className="h-5 w-5 text-purple-600" />
                                </div>
                                <div className="min-w-0">
                                    <p className="font-medium text-foreground flex items-center gap-1">
                                        客户门户
                                        <HelpHint title="客户门户白标链接是什么?">
                                            给客户生成一个<b>专属链接</b>, 发给他后他打开就能<b>随时自己看</b>这些词的实时出现率看板和报告 ——
                                            不用登录、看不到别的客户、页面不露你的后台。
                                            <br />「白标」= 去掉我们的品牌痕迹, 像是你自己的服务页。适合定期给客户交付效果。
                                        </HelpHint>
                                    </p>
                                    <p className="text-xs text-muted-foreground">客户门户白标链接</p>
                                </div>
                            </CardContent>
                        </Card>
                    </FeatureTooltip>
                </div>
            </div>

            {/* === 更多工具(折叠 · 低频不与主操作同级) === */}
            <div>
                <button
                    type="button"
                    onClick={() => setShowMore((v) => !v)}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
                >
                    <Wrench className="h-3.5 w-3.5" />
                    更多工具{moreCount > 0 ? ` (${moreCount})` : ''}
                    <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', showMore && 'rotate-180')} />
                </button>
                {showMore && (
                    <div className="mt-3 grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
                        {/* 趋势报表 · 全员 */}
                        <ActionTile icon={TrendingUp} iconBg="bg-blue-500/10" iconColor="text-blue-600" title="趋势报表" desc="看排名变化" onClick={onTrend} />
                        {/* 报告管理 · 全员 */}
                        <ActionTile icon={FolderOpen} iconBg="bg-indigo-500/10" iconColor="text-indigo-600" title="报告管理" desc="查看和编辑已生成报告" onClick={() => navigate('/reports')} />
                        {/* 媒体投放 · [#222 a1b] 全员(交付凭证,不是经营后台) */}
                        <ActionTile icon={FileText} iconBg="bg-orange-500/10" iconColor="text-orange-600" title="媒体投放" desc="录入投放记录（交付凭证）" onClick={onPublication} />
                        {/* 操作日志 · [#222 a1b] 全员 —— 非 admin 看到的本来就只有**自己的**操作 */}
                        <ActionTile icon={ClipboardList} iconBg="bg-muted" iconColor="text-muted-foreground" title="操作日志" desc={isAdmin ? '查看所有操作' : '查看自己的操作'} onClick={onLogs} />
                        {/* 数据回退 · [#222 a1b'] Owner 09-16 放开:删的是自己账号的监测历史,与服务商同权。
                            不可逆的风险由 RollbackDialog 的二次确认承担,不由身份承担。 */}
                        <ActionTile icon={RotateCcw} iconBg="bg-red-500/10" iconColor="text-red-600" title="数据回退" desc="回退错误监测数据" onClick={onRollback} />
                        {/* 添加关键词 · 仅 admin(老板 2026-06-05:非 admin 加词走报价/合同/服务变更流程) */}
                        {adminOnly && (
                            <ActionTile icon={Plus} iconBg="bg-green-500/10" iconColor="text-green-600" title="添加关键词" desc="想监测哪些词" onClick={onAddKeyword} />
                        )}
                        {/* 清除数据 · 仅 admin */}
                        {adminOnly && (
                            <ActionTile icon={ShieldAlert} iconBg="bg-red-500/10" iconColor="text-red-600" title="清除数据" desc="清空监测重新开始" onClick={onClearData} titleColor="text-red-400" />
                        )}
                        {/* 数据归档 · 仅 admin 且有归档 */}
                        {adminOnly && archivesCount > 0 && (
                            <ActionTile icon={ArchiveRestore} iconBg="bg-amber-500/10" iconColor="text-amber-600" title="数据归档" desc="恢复已清除数据" onClick={onArchives} />
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}
