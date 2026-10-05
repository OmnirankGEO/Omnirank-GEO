/**
 * 脏表单守卫 —— 「永不中断主流程」的落成机制(开发原则 SSOT A6-1)
 *
 * [WO_NO_SILENT_RELOAD_DIRTY_GUARD 2026-08-16]
 *
 * 病史:用户在监测中心填品牌,填到一半被自动刷新顶掉。根因不在监测中心,
 * 是两套**全局自愈机制**在用户没同意的情况下重载整页:
 *   A  versionPoll bfcache 复活(pageshow persisted)→ 版本不一致 → 静默 hardReload
 *      (而且跳首页,丢表单 + 丢路由双重伤害)
 *   B  部署替换 assets → 旧 bundle 懒加载 chunk 404 → 500ms 静默 location.reload()
 * 放大器:一天四班车,每次部署同时制造 A 的版本差和 B 的 stale chunk。
 *
 * 🔴 修法不是把自愈关掉。自愈有真用途 —— 微信 WebView 的激进缓存靠它兜底,
 *    关掉会让一批用户永远停在老版本。**修的是"不问自取"**:
 *      有人正在填东西 → 不许静默重载,改弹既有 banner,把选择权还给用户;
 *      没人在填     → 照旧静默重载,自愈行为一字不变。
 *
 * 判据形态:两个方向都要能拆红 ——
 *   拆掉守卫 → dirty 场景的锁必须红;
 *   反向:非 dirty 场景必须**仍然**静默 reload(证明没把自愈修死)。
 */

type DirtyProbe = () => boolean;

/** 组件注册进来的"我现在脏不脏"探针。key 用调用方给的稳定 id。 */
const probes = new Map<string, DirtyProbe>();

/**
 * 注册一个脏状态探针。返回注销函数(给 useEffect 的 cleanup 用)。
 *
 * 注册制而不是"全局扫 DOM":真实脏状态只有组件自己知道 ——
 * 受控组件的值在 React state 里,DOM 上的 value 属性未必反映它;
 * 富文本 / 自定义控件更是完全看不出来。
 */
export function registerDirtyProbe(id: string, probe: DirtyProbe): () => void {
    probes.set(id, probe);
    return () => {
        // 只删自己那一条:同一个 id 被后来的组件覆盖过就不该由旧组件删掉
        if (probes.get(id) === probe) probes.delete(id);
    };
}

/** 退化兜底:焦点在可输入元素上,一律视为"用户正在填"。
 *
 * 🔴 为什么必须有这一层:注册制覆盖不到还没接入的表单(全站表单逐步接入),
 * 而**没接入的表单被刷掉照样是事故**。兜底判据不看内容、只看焦点 ——
 * 它宁可多拦一次(用户看到 banner 自己点),也不漏掉一个正在输入的人。
 * 「多弹一次 banner」的代价 << 「一屏填好的资料没了」的代价。
 */
export function activeElementLooksEditable(): boolean {
    try {
        const el = document.activeElement as HTMLElement | null;
        if (!el) return false;
        const tag = (el.tagName || '').toUpperCase();
        if (tag === 'TEXTAREA') return true;
        if (tag === 'INPUT') {
            // 按钮类 input 不算(type=button/submit/checkbox/radio/file 不是"正在填字")
            const type = ((el as HTMLInputElement).type || 'text').toLowerCase();
            return !['button', 'submit', 'reset', 'checkbox', 'radio', 'file', 'image', 'range'].includes(type);
        }
        if (el.isContentEditable) return true;
        return false;
    } catch {
        return false;
    }
}

// ── 全局「未提交输入」标志(R1)─────────────────────────────────────────────
//
// 🔴 Codex 2026-08-17 两个真机复现,都打在同一个设计缺陷上:
//   ① 用户在未注册表单里打了字 → 点别处失焦 → 切出去 → bfcache 复活 → **输入没了**
//   ② chunk 404 排下 500ms 定时器 → 用户在这 500ms 内开始打字 → 照刷
//   根因:退化兜底只看 `document.activeElement`,**失焦即判净**。
//   而"打了一半还没提交"这件事跟焦点在哪毫无关系 —— 焦点是瞬时的,未提交是持续的。
//
// ⇒ 改成**事件驱动的持续标志**:任何 input/change 一发生就置脏,
//   只有"提交"或"导航"才清除,**失焦不清**。
//   activeElement 那条保留作为它本来擅长的场景(刚聚焦还没敲键盘时也别刷)。
let _uncommittedInput = false;

/** 用户开始输入了。由全局 input/change 监听调用,也可被组件显式调用。 */
export function markUncommittedInput(): void {
    _uncommittedInput = true;
}

/** 输入已经落地(提交成功 / 主动放弃 / 路由切走)⇒ 可以清除。
 *
 * 🔴 **失焦不算落地** —— 那正是 Codex ① 复现的形态。
 */
export function clearUncommittedInput(): void {
    _uncommittedInput = false;
}

export function hasUncommittedInput(): boolean {
    return _uncommittedInput;
}

/** 装全局监听:一次性,幂等。由 main.tsx 引导时调用。 */
let _installed = false;
export function installDirtyInputTracking(): void {
    if (typeof window === 'undefined' || _installed) return;
    _installed = true;
    const onEdit = (e: Event) => {
        const t = e.target as HTMLElement | null;
        if (!t) return;
        const tag = (t.tagName || '').toUpperCase();
        if (tag === 'TEXTAREA' || tag === 'INPUT' || t.isContentEditable) markUncommittedInput();
    };
    // capture:有些组件会 stopPropagation,冒泡阶段可能收不到
    document.addEventListener('input', onEdit, true);
    document.addEventListener('change', onEdit, true);
    // 提交与导航 = 输入已落地 ⇒ 清除
    document.addEventListener('submit', () => clearUncommittedInput(), true);
    window.addEventListener('popstate', () => clearUncommittedInput());
    // SPA 路由切换没有 popstate,history.pushState 要包一层
    try {
        const origPush = history.pushState;
        history.pushState = function (...args: Parameters<typeof origPush>) {
            clearUncommittedInput();
            return origPush.apply(this, args);
        };
    } catch { /* 某些环境 history 不可写,忽略 */ }
}

/** 现在有没有"不能被打断"的用户输入。 */
export function isAnythingDirty(): boolean {
    for (const probe of probes.values()) {
        try {
            if (probe()) return true;
        } catch {
            /* 探针自己抛了不能拖垮守卫 —— 但也不能因此判成"干净" */
        }
    }
    // 🔴 三条并联,任一为真即脏:
    //   注册探针(组件自己知道)/ 全局未提交标志(失焦后仍然为真)/ 焦点在可输入元素上
    return _uncommittedInput || activeElementLooksEditable();
}

/** 只给测试与自检用:当前注册了几个探针(证明接线真的接上了,不是空 registry)。 */
export function __dirtyProbeCount(): number {
    return probes.size;
}

/**
 * 所有**静默** reload 的唯一入口。
 *
 * @param doReload  真要重载时执行的动作(各条路自己的重载方式不同)
 * @param onBlocked 被守卫拦下时的替代动作(弹 banner,把选择权还给用户)
 * @returns 是否真的执行了重载
 *
 * 🔴 用户自己点的重载(banner 的「立即刷新」、报错页的「重新加载」按钮)**不走这里** ——
 *    那是用户的决定,守卫无权拦。守卫只管"没问就重载"的那些路。
 */
export function guardedSilentReload(doReload: () => void, onBlocked?: () => void): boolean {
    if (isAnythingDirty()) {
        try {
            onBlocked?.();
        } catch {
            /* banner 弹不出来也不能因此把用户的输入刷掉 */
        }
        return false;
    }
    doReload();
    return true;
}
