/**
 * FloatingStack · 屏幕浮层叠放规则统一收口
 * [2026-05-27 移动端教程基础设施]
 *
 * 解决问题:
 *   - 小榜 FAB / 教程 FAB / OnboardingChecklist 圆球 / 沙盒 banner / coach mark
 *     之前各自硬编码 z-index 和 bottom 位置 · 移动端互相盖、互相挤
 *   - 新加浮层时没规则可循 · 容易冲撞已有元素
 *
 * 使用约定:
 *   - 任何新加 fixed/sticky 浮层 · 先来这里查 / 加层级
 *   - z-index 用 `FLOATING_Z.<key>` · 不要再写 z-50 / z-[60] 这种魔法数字
 *   - 右下角 FAB 用 `FLOATING_FAB.<key>` 取 bottom 偏移 · 避免叠到一起
 *
 * 层级原则(由低到高):
 *   sticky 内容 < FAB 圆球 < spotlight 蒙层 < popover < dialog < coach mark < 庆祝 < toast
 */

/**
 * z-index 全栈层级 · 由低到高 · 注释里写谁在用
 *
 * 留间隔 10 给未来插值空间 · 不要紧挨着排
 */
export const FLOATING_Z = {
    /** 反 · sticky bottom nav / mobile tab bar 预留 · 暂未用 */
    STICKY_BOTTOM_NAV: 20,
    /** 沙盒态顶 banner sticky · "教程模式 · 数据不影响真账号"
     *  [2026-05-27 fix] 升到 180(原 30)· 沙盒态 banner 是"退出沙盒" 唯一出口 · 必须高于 coach mark
     *  保持低于 SANDBOX_CELEBRATION (200) · 庆祝彩带瞬时动画仍能盖 banner */
    SANDBOX_BANNER: 180,
    /** OnboardingChecklist 圆球 + 平铺面板 · 右下角 / 桌面端常驻 */
    ONBOARDING_CHECKLIST: 40,
    /** 默认 FAB · 小榜 / 教程 FAB / 沙盒 spotlight ring/tip */
    FAB: 50,
    /** popover · HelpHint / 简单 tooltip · 覆盖 spotlight 蒙层 */
    POPOVER: 60,
    /** Radix Dialog 默认 · 不要用此值 · radix-ui/react-dialog 自管 z-50/z-100 */
    DIALOG: 100,
    /** 移动端 CoachMark 全屏遮罩 · 比 dialog 高 · 比庆祝低 */
    MOBILE_COACH_MARK: 150,
    /** 沙盒完成庆祝彩带 / 全屏顶层动画 */
    SANDBOX_CELEBRATION: 200,
    /** toast · sonner 默认 z-[2000] 自管 · 这里只为参考 */
    TOAST: 2000,
} as const

/**
 * 右下角浮按钮 bottom 偏移 · 单位 px
 *
 * 多个 FAB 同屏共存时 · 用这里的常量避免重叠。
 * 移动端 viewport 底部 safe-area-inset-bottom 一般 16-34px · 起步 24 留缓冲。
 *
 * 当前共存(由下到上):
 *   - 小榜 FAB         bottom: 100px(可拖拽 · 默认值)
 *   - 教程 FAB(待加)  bottom:  24px(移动端教程入口 FAB)
 *   - OnboardingChecklist 圆球(独立 · 见组件内部)
 *
 * 后续如果加第 N 个 FAB · 用常量算位 · 不要硬编码
 */
export const FLOATING_FAB = {
    /** 教程入口 FAB(移动端)· 右下角靠下 · 比小榜更近底
     *  ⚠️ **当前全仓零消费方**(2026-08-18 实测:除本文件外没有任何地方引用这两个常量)。
     *  它是给"待加"的教程 FAB 预留的位置,不是一个已存在的浮层 ——
     *  因此**不得**把它当作共存判据的分母;真加教程 FAB 时再连同判据一起接线。 */
    TUTORIAL_BOTTOM: 24,
    TUTORIAL_RIGHT: 24,
    /** 🔴 **撤销登记(2026-08-18 · Codex R2 会签指出)**:此处曾登记 M3 客户列表
     *  「新增客户」FAB(bottom-20/right-4/h-14)。**那是个死锚** ——
     *  `pages/M3/M3 客户列表页.tsx` 在 `App.tsx` 里**零路由**,而且 `/m3` 与 `/m3/*`
     *  都被 `<Route element={<Navigate to="/" replace />}>` 全量吞掉,页面永远进不去。
     *  拿它当"共存对象"算出来的不重叠,和拿 `MobileTabBar` 当遮挡源一样,
     *  是**同一种错的第二次**:判据锚在不渲染的东西上。已删,不再进任何分母。
     *  判据侧的防复发物 = tests 里的挂载性前置(锚必须真挂载,否则拒跑报红)。 */
    /** 小榜 FAB 默认位置(可拖拽 · 用户拖动后存 state)
     *  152 的由来不再是"躲开 M3 FAB",而是给窄屏页面级底部操作条
     *  (见 MOBILE_BOTTOM_BAR_HEIGHT)留出净空:64(条)+ 8(间距)之上再留余量,
     *  同时让 FAB 与 `NotificationBanner`(bottom-6 · 居中)竖直方向不打架。 */
    XIAOBANG_DEFAULT_BOTTOM: 152,
    XIAOBANG_DEFAULT_RIGHT: 24,
    /** 标准 FAB 尺寸 · 跟 AgentFAB 一致 · 移动端教程 FAB 也用这个 */
    SIZE: 48,
} as const

/**
 * 页面级底部固定操作条的保守高度(px)。
 *
 * 🔴 **更正(2026-08-18 · 真浏览器判据推翻了我先前的说法)**:
 *    本常量最初写成「移动端 tab bar 高度」,理由是 `MobileTabBar` 会挡住 FAB。
 *    那个理由**是错的** —— `MobileTabBar` 全仓 `<MobileTabBar` 挂载点 **0 个**
 *    (`PublishCenter.tsx:2991` 早就注明「MobileTabBar 全仓零引用」),
 *    它根本不渲染,谈不上遮挡。是 Playwright 里 `nav.fixed.bottom-0` 恒取不到
 *    才把这个错误前提暴露出来的;静态推理不会自己发现它。
 *
 * 真正会渲染、且会与右下角浮层争地盘的是**页面级**底部条:
 *   · `pages/Selection/components/BottomActionBar.tsx`  `fixed bottom-0 … z-20`
 *   · `pages/Agent/QuotePreview.tsx`                    `fixed bottom-0 … z-40 sm:hidden`
 *   · `pages/Intake/IntakeFillPage.tsx`                 `fixed bottom-0 … z-10`
 *   · `pages/MaterialConfirm/MaterialConfirmPage.tsx`   `fixed bottom-0 … z-[60]`
 * 它们高度由内容 + padding 决定(实测量级 56–72px),64 是留给右下角浮层的
 * 保守下限:bottom 小于本值 + 间距的浮层会压在这类条上。
 */
export const MOBILE_BOTTOM_BAR_HEIGHT = 64

/**
 * Tailwind `lg` 断点。上面那批底部条基本都是窄屏形态(`sm:hidden` / 移动端布局),
 * 拖拽钳制按这个断点区分「要不要给底部条让位」—— 断点各写各的就是下一次漂移。
 */
export const LG_BREAKPOINT = 1024

/**
 * 类型导出 · 防止外部传错 z-index key
 */
export type FloatingZKey = keyof typeof FLOATING_Z
export type FloatingFabKey = keyof typeof FLOATING_FAB
