/**
 * #178 · 全排除死胡同的出口卡(客户页 `/s/<token>`)。
 *
 * Owner 09-12 图 1:客户自己选的业务方向下,词全被判成知识/百科类 ⇒ 老逻辑弹一句
 * 「请联系报价方补充商业选型问题」的 toast 就结束了 —— 客户被拒、且没有任何出口,
 * 连"联系报价方"这个动作在页面上都不存在。
 *
 * 所以这张卡的验收标准不是"有没有画出来",而是**它给的两条出口是不是真的通**:
 * ① 就地加一条商业问法 → 打 `POST /api/s/{token}/submit-keywords`
 *    (该端点允许态含 `business_lines_submitted`,见 `api/selection_api.py:1814`,
 *     所以这条出口落在一个真的允许态上,不是拼个按钮);
 * ② 让报价方补充 → 后端推通知给品牌 owner。
 *
 * 治理 SSOT(`docs/DECISION/GEO_COMMERCIAL_INTENT_GOVERNANCE_SSOT_2026-07-23.md`
 * :36-38 / :133-134)要求「必须提供两条出口」「不得是无出口的全局拒绝」。
 * §3.2「知识词不进付费交付」这条政策本单**不放宽** —— 排除照排除,只是不再是死路。
 */
import { CustomKeywordInput } from './CustomKeywordInput';
import {
    cardSubtitle,
    cardTitle,
    primaryExit,
    notifyState,
    rewriteExamples,
    type AllExcludedState,
    type ExcludedRow,
} from '../utils/allExcludedExits';

interface Props {
    state: AllExcludedState;
    brandName: string;
    category?: string;
    customKeywords: string[];
    onAddKeyword: (keyword: string) => void;
    onNotify: () => void;
    notifyInFlight?: boolean;
    notifyAlreadySent?: boolean;
}

function RowList({ rows }: { rows: ExcludedRow[] }) {
    return (
        <ul className="mt-2 space-y-1.5">
            {rows.map((row, idx) => (
                <li key={`${row.keyword}-${idx}`} className="text-xs break-words">
                    <span className="text-amber-800 line-through">{row.keyword}</span>
                    {row.reason ? <span className="text-amber-700 ml-2">· {row.reason}</span> : null}
                </li>
            ))}
        </ul>
    );
}

export function AllExcludedExitCard({
    state,
    brandName,
    category,
    customKeywords,
    onAddKeyword,
    onNotify,
    notifyInFlight,
    notifyAlreadySent,
}: Props) {
    const examples = rewriteExamples(brandName, category);
    // 主出口由 reason 决定(见 primaryExit 抬头):一条词都没生成时,该动的是报价方
    const primary = primaryExit(state);
    const notify = notifyState({
        notifySent: state.notifySent,
        alreadySent: notifyAlreadySent,
        inFlight: notifyInFlight,
    });

    return (
        <div className="max-w-lg md:max-w-2xl mx-auto px-5 pt-4" data-testid="all-excluded-card">
            {/* 🔴 必须是 flex 容器:下面两条出口用 order-1/order-2 排先后,
                而 `order` 只对 flex/grid 的直接子元素生效 —— 挂在普通 block 父元素上
                是一对**什么都不做**的类名(主出口顺序会静默失效)。 */}
            <div className="flex flex-col rounded-2xl border border-amber-200 bg-amber-50 p-4 md:p-5">
                <p className="text-sm md:text-base font-semibold text-amber-900" data-testid="all-excluded-title">
                    {cardTitle(state)}
                </p>
                <p className="text-xs text-amber-700 mt-1" data-testid="all-excluded-subtitle">
                    {cardSubtitle(state)}
                </p>

                {state.notDeliverable.length > 0 && <RowList rows={state.notDeliverable} />}

                {/* 🔴 两类 kind 文案必须分开:这一类澄清后可以重新提交,上一类不行。
                    混在一起会让客户对着一条不可改的词反复改。 */}
                {state.needsClarification.length > 0 && (
                    <div className="mt-3 rounded-xl border border-amber-200 bg-white/60 p-3">
                        <p className="text-xs font-medium text-amber-900">
                            这 {state.needsClarification.length} 个暂时无法确认（改写清楚后可以再提交）
                        </p>
                        <RowList rows={state.needsClarification} />
                    </div>
                )}

                {/* ───── 出口① 换成选型问法 ───── */}
                <div
                    className={`rounded-xl border border-amber-200 bg-white p-3 md:p-4 ${primary === 'add_keywords' ? 'mt-4 order-1' : 'mt-3 order-2'}`}
                    data-testid="exit-add-keywords"
                >
                    <p className="text-sm font-semibold text-gray-800">
                        {primary === 'add_keywords' ? '换成选型类问法就能交付' : '也可以自己加一条'}
                    </p>
                    <p className="text-xs text-gray-500 mt-1">
                        客户在做选择时才会问下面这类问题 —— 这种问法 AI 会在回答里推荐具体品牌：
                    </p>
                    <div className="flex flex-wrap gap-1.5 mt-2">
                        {examples.map((ex) => (
                            <span
                                key={ex}
                                className="text-xs px-2 py-1 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-100"
                            >
                                {ex}
                            </span>
                        ))}
                    </div>
                    <CustomKeywordInput
                        onAdd={onAddKeyword}
                        placeholder="按上面的样子写一条，比如「哪家好」「怎么选」"
                    />
                    {customKeywords.length > 0 && (
                        <p className="px-5 text-xs text-emerald-700">
                            已添加 {customKeywords.length} 条：{customKeywords.join('、')}
                        </p>
                    )}
                </div>

                {/* ───── 出口② 让报价方补充 ───── */}
                <div
                    className={`flex flex-wrap items-center gap-2 ${primary === 'notify' ? 'mt-4 order-1' : 'mt-3 order-2'}`}
                    data-testid="exit-notify"
                >
                    {notify.done ? (
                        <span
                            className="text-xs text-emerald-700 bg-emerald-50 border border-emerald-100 rounded-full px-3 py-1.5"
                            data-testid="notify-done"
                        >
                            ✅ {notify.label}，请稍等
                        </span>
                    ) : (
                        <button
                            type="button"
                            onClick={onNotify}
                            disabled={notify.disabled}
                            data-testid="notify-button"
                            className={primary === 'notify'
                                ? 'text-sm font-semibold text-white bg-amber-600 rounded-lg px-4 py-2 hover:bg-amber-700 active:scale-95 disabled:opacity-40 disabled:cursor-not-allowed transition-all'
                                : 'text-xs font-medium text-amber-900 bg-white border border-amber-300 rounded-full px-3 py-1.5 hover:bg-amber-100 disabled:opacity-40 disabled:cursor-not-allowed transition-colors'}
                        >
                            {notify.label}
                        </button>
                    )}
                    <span className="text-xs text-amber-700">
                        {primary === 'notify'
                            ? '这个方向下的问法要报价方先配好，点一下就通知他。'
                            : '不想自己想词？让报价方帮你补，补好会重新发给你。'}
                    </span>
                </div>
            </div>
        </div>
    );
}
