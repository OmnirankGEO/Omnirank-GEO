/**
 * WelcomeChoiceModal · 新代理首次登录二选一弹窗 (2026-05-22)
 *
 * 替换了老版 OnboardingWelcomeModal(3 张路径卡那个) · 老板拍板:
 *   - 新代理走"沙盒教程",不再让用户在 3 条真实路径里挑
 *   - 进沙盒前必须给用户一个明示选择,不能登录就直接弹沙盒(吓人)
 *
 * 显示条件 (全部满足才弹):
 *   1) localStorage 可用
 *   2) state.welcome_choice === undefined  (没做过选择)
 *   3) 已登录 (user 不为空)
 *   4) 是代理身份 (agentLevel ∈ L1 / L2 / paid)
 *   5) 桌面端 (window.innerWidth >= 768)
 *   6) 当前不在沙盒态 (防 reload 后重叠 SandboxIntroModal)
 *
 * 行为矩阵:
 *   "我是新代理(推荐)" → setWelcomeChoice('start') + enterSandbox() + reload
 *                       (reload 后 SandboxIntroModal 自动弹 + 拦截器立即生效)
 *   "我已经用过"        → setWelcomeChoice('never') + toast 提示帮助中心可重走
 *
 * 强制选择:
 *   - hideCloseButton  · 关掉内置 X
 *   - onPointerDownOutside.preventDefault · 点蒙层无效
 *   - onEscapeKeyDown.preventDefault · ESC 无效
 *   原因: 必须做一次明示选择, 不让用户"绕过去看一眼再决定"导致后续没有引导
 */
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog';
import { GraduationCap, Briefcase, ShieldCheck, ArrowRight } from 'lucide-react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import { useOnboarding } from '@/context/OnboardingContext';
import { useAuth } from '@/context/AuthContext';
import { useIsMobile } from '@/hooks/use-mobile';
import { enterSandbox, useSandboxState } from '@/sandbox/sandboxState';
import { useScreenshotMode } from '@/sandbox/screenshotMode';

export function WelcomeChoiceModal() {
    const { state, setWelcomeChoice, isAvailable } = useOnboarding();
    const { user } = useAuth();
    const { isSandbox } = useSandboxState();
    const screenshotMode = useScreenshotMode();
    // [2026-05-27] 移动端教程入口打开 · 删 768 阻断 · 改用 useIsMobile 切布局
    const isMobile = useIsMobile();

    /*
     * 🔴 [#222 a1b] 原来这里有 `&& isAgent` —— 普通账号**根本看不到新手引导**。
     *    Owner 09-15:普通账号权限 = 服务商(除经营后台)。引导走的是**主流程四步**
     *    (体检 / 报价 / 创作 / 发布),沙盒白名单里没有任何 `/agent/*` ——
     *    不会把人引到他进不去的页,所以没有理由只给一部分人看。
     */
    const shouldShow = (
        isAvailable
        && state.welcome_choice === undefined
        && !!user
        && !isSandbox
        && !screenshotMode  // 截图模式 · 不要弹欢迎窗口干扰拍摄
        && typeof window !== 'undefined'
        // 2026-05-27 删 innerWidth >= 768 限制 · 移动端也要弹强制二选一
    );

    if (!shouldShow) return null;

    const handlePickNew = () => {
        // 进沙盒 · welcome_choice 落 'start' · reload 后 SandboxIntroModal 接力
        setWelcomeChoice('start');
        enterSandbox();
        window.location.reload();
    };

    const handlePickExperienced = () => {
        setWelcomeChoice('never');
        toast.success('已关闭新手引导', {
            description: '随时可在「帮助中心」找回教程视频和沙盒重走入口',
            duration: 5000,
        });
    };

    return (
        <Dialog open={true}>
            <DialogContent
                hideCloseButton
                onPointerDownOutside={(e) => e.preventDefault()}
                onEscapeKeyDown={(e) => e.preventDefault()}
                /* PC 端原 max-w-2xl 居中 modal 不动
                   移动端全屏:w-screen + h-[100dvh](防 Safari 地址栏伸缩跳)+ rounded-none + 内容滚动
                   移动端 padding 用 safe-area 防 iPhone notch / home indicator 挡按钮 */
                className={cn(
                    'gap-0',
                    isMobile
                        ? 'w-screen h-[100dvh] max-w-none rounded-none overflow-y-auto p-5 pb-[max(1.25rem,env(safe-area-inset-bottom))] pt-[max(1.25rem,env(safe-area-inset-top))]'
                        : 'max-w-2xl'
                )}
            >
                {/* 标题 */}
                <DialogTitle className={cn(
                    'font-bold tracking-tight text-center',
                    isMobile ? 'text-lg mt-2' : 'text-xl'
                )}>
                    欢迎使用全域上榜 GEO 交付系统
                </DialogTitle>
                <DialogDescription className="text-center mt-1.5">
                    第一次来? 先跑一遍教程会更顺手
                </DialogDescription>

                {/* 两张选择卡 · 移动端单列上下堆叠 · PC 双列并排 */}
                <div className={cn(
                    'grid gap-3 mt-5',
                    isMobile ? 'grid-cols-1' : 'grid-cols-2'
                )}>
                    {/* 卡 1 · 新手 (推荐) */}
                    <button
                        type="button"
                        onClick={handlePickNew}
                        className="
                            group relative flex flex-col items-start gap-3 text-left
                            border-2 border-amber-500/40 bg-amber-500/5
                            rounded-xl p-5 cursor-pointer
                            transition-all hover:scale-[1.02]
                            hover:border-amber-500 hover:bg-amber-500/10 hover:shadow-lg
                            focus:outline-none focus:ring-2 focus:ring-amber-500 focus:ring-offset-2
                        "
                    >
                        {/* 推荐角标 */}
                        <span className="absolute -top-2 -right-2 bg-amber-500 text-white text-[10px] font-bold px-2 py-0.5 rounded-full shadow">
                            推荐
                        </span>
                        <div className="h-11 w-11 rounded-xl bg-amber-500/15 flex items-center justify-center">
                            <GraduationCap className="h-6 w-6 text-amber-600" />
                        </div>
                        <div className="space-y-1">
                            <div className="text-base font-semibold">我是新手</div>
                            <div className="text-xs text-muted-foreground leading-relaxed">
                                进教程模式 · 用演示数据跑通 4 步流程
                            </div>
                        </div>
                        <div className="text-[11px] text-amber-700 dark:text-amber-400 flex items-center gap-1 mt-auto">
                            <ShieldCheck className="h-3 w-3" />
                            <span>不消耗算力 · 不污染你的真实客户库</span>
                        </div>
                        <div className="absolute right-3 bottom-3 text-amber-600 opacity-0 group-hover:opacity-100 transition-opacity">
                            <ArrowRight className="h-4 w-4" />
                        </div>
                    </button>

                    {/* 卡 2 · 老用户 */}
                    <button
                        type="button"
                        onClick={handlePickExperienced}
                        className="
                            group relative flex flex-col items-start gap-3 text-left
                            border rounded-xl p-5 cursor-pointer
                            transition-all hover:scale-[1.02]
                            hover:border-foreground/40 hover:shadow-md
                            focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2
                        "
                    >
                        <div className="h-11 w-11 rounded-xl bg-muted flex items-center justify-center">
                            <Briefcase className="h-6 w-6 text-muted-foreground" />
                        </div>
                        <div className="space-y-1">
                            <div className="text-base font-semibold">我已经用过</div>
                            <div className="text-xs text-muted-foreground leading-relaxed">
                                直接进真实工作台 · 跳过新手引导
                            </div>
                        </div>
                        <div className="text-[11px] text-muted-foreground mt-auto">
                            随时可在帮助中心的视频教程主页重走教程
                        </div>
                        <div className="absolute right-3 bottom-3 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity">
                            <ArrowRight className="h-4 w-4" />
                        </div>
                    </button>
                </div>

                {/* 兜底说明 */}
                <p className="text-[11px] text-muted-foreground text-center mt-4">
                    选择后随时可以反悔 · 帮助中心 → 视频教程 → 重走新手教程
                </p>
            </DialogContent>
        </Dialog>
    );
}

export default WelcomeChoiceModal;
