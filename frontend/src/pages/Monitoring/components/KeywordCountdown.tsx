/**
 * Per-keyword service countdown display.
 *
 * [2026-06-04 纯履约口径 · 老板订正] 监测板块只显履约进度,不显合同自然日历倒计时
 *   主显「还需达标 {remainingCompliant} 天」= service_days − compliant_days(后端 remaining_compliant 字段)
 *   副显「已达标 N/M 天」= 履约进度(compliant_days/service_days · 保留每词达标区分)
 *   过期 / 已交付态由调用方(KeywordTable)外层处理,本组件只渲染"进行中"。
 *
 * [CTO-15.23 2026-05-10 老板 A 方案] isStable 区分"刚达标 vs 稳定达标"
 *   isStable=true → "稳定达标" Badge (最近 7 天 ≥ 5 天 is_compliant=TRUE)
 *   isStable=false + isCompliant=true → "刚达标" Badge (今天达标但还不稳定)
 *   isCompliant=false → 不显 Badge (已暂停 / 未达标 · 用现有灰点表达)
 */

export function KeywordCountdown({
    compliantDays, serviceDays, isCompliant, isStable, remainingCompliant,
}: {
    compliantDays: number;
    /** 履约达标天数配额 · 后端 service_days · [服务期 SSOT 2026-08-06] 调用方不许再 `|| 365` 兜底 */
    serviceDays: number | null;
    isCompliant: boolean;
    isStable?: boolean;
    remainingCompliant: number | null;
}) {
    // 配额没设就直说,不拿 365 编一个分母出来(编出来的分母正是"倒计时说还有一年"的来源)
    const compliantLine = (
        <div className="text-[10px] text-muted-foreground">
            {serviceDays ? `已达标 ${compliantDays}/${serviceDays}天` : `已达标 ${compliantDays}天 · 达标天数未设`}
        </div>
    );

    // 无后端还需达标数据(null)· 降级:不显误导的"剩余天数",只给达标进度
    if (remainingCompliant == null) {
        return (
            <div className="text-xs tabular-nums whitespace-nowrap">
                <span className="text-muted-foreground">服务进行中</span>
                {compliantLine}
            </div>
        );
    }

    // 正常:主显「还需达标 X 天」(履约口径)+ 达标徽章 + 副显「已达标 N/M」
    return (
        <div className="text-xs tabular-nums whitespace-nowrap">
            <div className="flex items-center gap-1">
                {isCompliant ? (
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse shrink-0" title="达标计时中" />
                ) : (
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-gray-400 shrink-0" title="未达标 · 还没开始达标计时" />
                )}
                <span className="font-semibold">还需达标 {remainingCompliant}天</span>
                {/* [v12 item8] 未首次达标(compliant_days=0)显【可见文字】"未达标·暂未开始计时"(不再只灰点 tooltip)·
                    此时 remainingCompliant=service_days(完整剩余服务天数)· 绝不显"已到期"。 */}
                {!isCompliant && compliantDays === 0 && (
                    <span
                        className="ml-1 px-1 py-0 rounded bg-gray-100 text-gray-600 text-[9px] font-medium border border-gray-200"
                        title="未首次达标 · 尚未开始消耗服务天数"
                    >
                        未达标·暂未开始计时
                    </span>
                )}
                {isCompliant && (
                    isStable ? (
                        <span
                            className="ml-1 px-1 py-0 rounded bg-emerald-100 text-emerald-700 text-[9px] font-medium border border-emerald-200"
                            title="最近 7 天 ≥ 5 天达标"
                        >
                            稳定达标
                        </span>
                    ) : (
                        <span
                            className="ml-1 px-1 py-0 rounded bg-amber-100 text-amber-700 text-[9px] font-medium border border-amber-200"
                            title="今天达标 · 最近 7 天达标天数 < 5 · 还在观察"
                        >
                            刚达标
                        </span>
                    )
                )}
            </div>
            {compliantLine}
        </div>
    );
}
