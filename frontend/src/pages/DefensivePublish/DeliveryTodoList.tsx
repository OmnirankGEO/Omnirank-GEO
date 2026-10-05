/**
 * U-5 · 交付待办清单(聚合页)。
 *
 * §0.5.5 U-5 逐字:「一问一稿一媒体的逐项 preview→confirm 保持独立
 * receipt/freeze 不变,但 UI **必须**提供「交付待办清单」聚合页:
 * 服务端批量预生成 decision snapshot,一页逐项(或勾选连续)确认。
 * **12~50 轮确认没有聚合页 = 验收红**」。
 *
 * 每屏三问在这一屏的答案(40 岁非技术销售基准):
 * - **我知道刚才发生了什么吗?** 顶部一句:这一单还剩几项要确认、总共多少算力,
 *   并且明说「每一项各自确认、各自计费,不会一次性全扣」。
 * - **我知道下一步点哪吗?** 每一行恰一个主行动;整页恰一个「确认下一项」。
 * - **我敢等吗?** 涉钱的数字逐项显示,零处由前端计算(全部来自服务端)。
 *
 * 🔴 这一页**不做批量提交**。聚合的是呈现,不是资金:
 *    把 N 笔独立冻结合并成一次提交是改资金合同,不是改 UI。
 *    「勾选连续」在这里的实现 = 确认完一项自动把下一项顶到最前,
 *    她仍然是一颗一颗按下去的。
 */

import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ClipboardList, Loader2, Wallet } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { DEFGEO_COPY } from '@/lib/defensiveGeoCopy';
import { formatApiErrorForDisplay } from '@/lib/api';
import { fetchDeliveryTodo } from './api';
import { ErrorNotice } from './components';
import {
    decisionDeepLink, orderTodoItems, pendingSummary,
    type DeliveryTodoItem, type DeliveryTodoResponse,
} from './deliveryTodo';

function TodoRow({
    item, onOpen,
}: { item: DeliveryTodoItem; onOpen: (id: string) => void }) {
    const pending = item.todoState === 'awaiting_confirm';
    return (
        <li
            data-testid="delivery-todo-row"
            data-todo-state={item.todoState}
            className={
                'flex flex-col gap-2 rounded-lg border p-3 lg:flex-row lg:items-center lg:gap-4 '
                + (pending ? 'border-amber-300 bg-amber-50/60' : 'border-border')
            }
        >
            <div className="min-w-0 flex-1">
                <p className="truncate text-[14px] font-medium">
                    {item.publicMediaName || item.planItemKey}
                </p>
                {/* 人话状态由服务端下发 —— 前端不把 todoState 画到屏幕上 */}
                <p className="text-[12px] text-muted-foreground">{item.todoLabel}</p>
            </div>
            <div className="flex items-center gap-2 lg:gap-4">
                <span
                    data-testid="delivery-todo-points"
                    className="whitespace-nowrap text-[13px] tabular-nums text-muted-foreground"
                >
                    {item.exactPoints} 算力
                </span>
                {pending && item.decisionSnapshotId ? (
                    <Button
                        type="button"
                        size="sm"
                        data-testid="delivery-todo-confirm"
                        onClick={() => onOpen(String(item.decisionSnapshotId))}
                    >
                        {DEFGEO_COPY.confirmNextPending}
                    </Button>
                ) : null}
            </div>
        </li>
    );
}

export default function DeliveryTodoList() {
    const navigate = useNavigate();
    const [params] = useSearchParams();
    const acceptedSnapshotId = Number(params.get('acceptedSnapshotId') || 0);

    const [data, setData] = useState<DeliveryTodoResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const load = useCallback(async () => {
        if (!acceptedSnapshotId) {
            setLoading(false);
            setError('这一单还没有可以确认的媒体方案。');
            return;
        }
        setLoading(true);
        setError(null);
        try {
            setData(await fetchDeliveryTodo(acceptedSnapshotId));
        } catch (e) {
            // 🔴 fallback 必须自带出口与"钱没动"那半句 —— 涉钱页面的失败态
            //    只说"取不到"会让她第一反应是"我的算力是不是已经扣了"。
            setError(formatApiErrorForDisplay(
                e, '这一单的待办清单这次没能取出来；没有扣除任何算力，稍后再试一次。', 'agent'));
        } finally {
            setLoading(false);
        }
    }, [acceptedSnapshotId]);

    useEffect(() => { void load(); }, [load]);

    const open = useCallback((snapshotId: string) => {
        navigate(decisionDeepLink(snapshotId));
    }, [navigate]);

    const summary = data ? pendingSummary(data) : null;
    const rows = data ? orderTodoItems(data.items) : [];

    return (
        <section
            data-testid="delivery-todo-page"
            className="mx-auto w-full max-w-3xl space-y-4 p-4 lg:max-w-4xl lg:p-6 2xl:max-w-5xl"
            aria-live="polite"
        >
            <header className="space-y-1">
                <h1 className="flex items-center gap-2 text-[17px] font-semibold lg:text-[19px]">
                    <ClipboardList className="h-4.5 w-4.5" aria-hidden />
                    这一单还要确认什么
                </h1>
                {/* 🔴 U-5「资金仍逐项」那半句必须在:她最怕的就是一按全扣 */}
                <p data-testid="delivery-todo-money-note" className="text-[13px] text-muted-foreground">
                    {DEFGEO_COPY.deliveryTodoIntro}
                </p>
            </header>

            {loading && (
                <p className="flex items-center gap-2 text-[13px] text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                    正在把这一单要确认的项目取出来
                </p>
            )}

            {error && !loading && (
                <div className="space-y-2">
                    <ErrorNotice text={error} />
                    <Button type="button" variant="outline" size="sm" onClick={() => void load()}>
                        再试一次
                    </Button>
                </div>
            )}

            {summary && !loading && !error && (
                <>
                    <div className="flex flex-wrap items-center gap-2 rounded-md bg-muted/50 p-3">
                        <Wallet className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                        <span data-testid="delivery-todo-pending" className="text-[14px] font-medium">
                            还有 {summary.countFromItems} 项等你确认
                        </span>
                        <span className="text-[13px] text-muted-foreground">
                            合计 {summary.pointsFromItems} 算力
                        </span>
                        {/* 顶部数字与逐项对不上时**画出来**,不静默以其中一个为准 */}
                        {!summary.consistent && (
                            <span
                                data-testid="delivery-todo-inconsistent"
                                className="text-[12px] text-amber-700"
                            >
                                以上面这份逐项清单为准
                            </span>
                        )}
                    </div>

                    {rows.length === 0 ? (
                        <p data-testid="delivery-todo-empty" className="text-[13px] text-muted-foreground">
                            {DEFGEO_COPY.deliveryTodoAllDone}
                        </p>
                    ) : (
                        <ul className="space-y-2">
                            {rows.map((item) => (
                                <TodoRow key={item.publishSlotId} item={item} onOpen={open} />
                            ))}
                        </ul>
                    )}
                </>
            )}
        </section>
    );
}
