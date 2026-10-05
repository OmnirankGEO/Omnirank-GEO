/**
 * 未知路由兜底页(#165 F2 · 2026-09-09)。
 *
 * 🔴 **为什么需要它**:真人点测在生产上点到一条 `/quotes`(路由表里没有),
 *    屏幕给的是**纯白**。白屏没有任何信息:她分不清是网断了、权限不够、
 *    还是这个功能被下掉了 —— 三种猜测对应三种完全不同的下一步。
 *    那条死链本身已在同一单修掉,但**下一条死链一定还会出现**
 *    (站内 href/navigate 有几百处),所以兜底页比修单条链更重要。
 *
 * 🔴 它挂在**受保护路由组内**,不在顶层:
 *    · 挂组内 ⇒ 登录用户看到的是**带侧栏的**「页面不存在」,还能直接点去别处;
 *    · 顶层再挂一条就永远匹配不到(父路由 `path="/"` 对任何 URL 都是前缀,
 *      组内有 `*` 之后顶层那条就是死路由)—— 死路由不如不写。
 *    · 未登录访客走 `ProtectedRoute` 去登录页,与其它未知路径行为一致。
 */
import { useNavigate } from 'react-router-dom';
import { Button } from '@/components/ui/button';

export function NotFound() {
    const navigate = useNavigate();
    return (
        <div data-testid="route-not-found"
             className="flex min-h-[50vh] flex-col items-center justify-center gap-4 px-6 text-center">
            <div className="space-y-2">
                <p className="text-lg font-semibold text-foreground">这个页面不存在</p>
                {/* 🔴 不写「404」「Not Found」这类工程术语(全站文案规矩);
                    也不猜原因 —— 说「可能是链接过期」在链接本来就写错时是句假话。 */}
                <p className="text-sm text-muted-foreground">
                    地址可能打错了,或者这个功能已经换了位置。
                </p>
            </div>
            <div className="flex flex-wrap items-center justify-center gap-2">
                <Button data-testid="not-found-home" onClick={() => navigate('/')}>回首页</Button>
                <Button variant="outline" data-testid="not-found-back"
                        onClick={() => navigate(-1)}>回上一页</Button>
            </div>
        </div>
    );
}

export default NotFound;
