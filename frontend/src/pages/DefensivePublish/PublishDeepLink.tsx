/**
 * U-6 · 深链落点。
 *
 * 三个入口都落在这里:
 *   · 审批通过的通知(「去确认这一单」)
 *   · U-5 交付待办清单的「确认下一项」
 *   · preview 过期自动重建后的新确认页
 *
 * ══════════════════════════════════════════════════════════════════
 * 🔴 这一屏的职责是**永远给得出一个答案**
 * ══════════════════════════════════════════════════════════════════
 * §0.5.6 铁律:任何阻塞与错误必须自带解决方案。深链最怕的两件事都在这里挡住:
 *   · 落点不存在 ⇒ 白页。她不知道是链接坏了还是自己点错了。
 *   · 落点存在但功能没开 ⇒ 她按下确认拿到 403,而且开始怀疑钱。
 * 所以入口关着时这一屏**明说两件事**:这是"还没开放"不是"你操作错了";钱没动。
 *
 * 🔴 文案取 copy registry(`publish_entry_closed` 那一条,与后端 typed 拒绝
 *    用的是**同一条**),不在这里另写一句 —— 两处各写一份,迟早说得不一样。
 */

import { lazy, Suspense } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Info, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { publishEntryScreen } from './publishEntryGate';

/** 入口开着才需要把确认页拉进 bundle。 */
const MediaDecisionConfirm = lazy(() => import('./MediaDecisionConfirm'));
const PublishCommandStatus = lazy(() => import('./PublishCommandStatus'));

/**
 * 🔴 与后端 `copy_registry._REASON_EXPLANATION['publish_entry_closed']` 逐字相同。
 *    这里没有走 `DEFGEO_COPY` 是因为那份生成物只收包H 新增的条目;
 *    这一条是终审 P0-1 就存在的,`verify-defgeo-publish-entry-mirror.mjs`
 *    把它一起比对进去(同一道门,两条断言)。
 */
export const ENTRY_CLOSED_SENTENCE =
    '发布这一步还没有开放，暂时不能确认；没有扣除任何算力。';

function NotOpenNotice({ onBack }: { onBack: () => void }) {
    return (
        <section
            data-testid="publish-entry-not-open"
            role="status"
            className="mx-auto w-full max-w-xl space-y-3 p-4 lg:p-6"
        >
            <div className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3">
                <Info className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden />
                <p className="text-[13px] leading-relaxed text-amber-900">
                    {ENTRY_CLOSED_SENTENCE}
                </p>
            </div>
            {/* 恒一个出口 —— 没有下一步的说明等于死路 */}
            <Button type="button" variant="outline" size="sm" onClick={onBack}>
                回到这一单的待办清单
            </Button>
        </section>
    );
}

function Fallback() {
    return (
        <p className="flex items-center gap-2 p-4 text-[13px] text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            正在打开
        </p>
    );
}

export function PublishDecisionDeepLink() {
    const navigate = useNavigate();
    const { snapshotId } = useParams();
    if (publishEntryScreen() === 'not_open') {
        return <NotOpenNotice onBack={() => navigate('/defensive-geo/publish/todo')} />;
    }
    return (
        <Suspense fallback={<Fallback />}>
            <MediaDecisionConfirm key={snapshotId} />
        </Suspense>
    );
}

export function PublishCommandDeepLink() {
    const navigate = useNavigate();
    const { commandId } = useParams();
    if (publishEntryScreen() === 'not_open') {
        return <NotOpenNotice onBack={() => navigate('/defensive-geo/publish/todo')} />;
    }
    return (
        <Suspense fallback={<Fallback />}>
            <PublishCommandStatus key={commandId} />
        </Suspense>
    );
}
