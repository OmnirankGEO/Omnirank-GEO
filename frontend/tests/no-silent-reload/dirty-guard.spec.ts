import { expect, test, type Page, type Route } from 'playwright/test'

/**
 * [WO_NO_SILENT_RELOAD_DIRTY_GUARD 2026-08-16 §2] 脏表单守卫 —— **真浏览器行为锁**。
 *
 * 跑的是真 app:真 main.tsx 引导、真 installVersionPoll / installErrorReporter、
 * 真 pageshow 事件、真 location 变化。不 stub 任何被测模块。
 *
 * 锁1  dirty + bfcache 复活 → **不刷**、banner 出现、**输入仍在**(断言 input value)
 * 锁1R 🔴 反向:无 dirty 同样触发 → **照旧静默刷**(证明自愈没被修死)
 * 锁2  🔴 ② 的对照:非 dirty 刷完之后 URL **仍是原路径**(不是 `/`),query 也还在
 * 锁3  dirty + chunk 404 → 不刷、banner 出现、输入仍在
 * 锁3R 🔴 反向:无 dirty 的 chunk 404 → 照旧静默刷
 * 锁4  回归:60s 轮询那条**非静默**路径行为不变 —— 出 banner、**不自动刷**
 * 锁5  回归:30s cooldown 仍在(dirty 被拦下时不消耗配额,清掉 dirty 后仍能刷)
 */

const OLD_HASH = 'index-OLDOLDOLD'
const NEW_HASH = 'index-NEWNEWNEW'

/** 让 `/?_v=...` 返回**新版本**的 HTML,制造"版本不一致" */
async function mockVersionDrift(page: Page, drift: { on: boolean }) {
    await page.route('**/*', async (route: Route) => {
        const url = new URL(route.request().url())
        // 只劫持 versionPoll 那条带 _v= 的探测请求,其余一律放行给真 dev server
        if (url.searchParams.has('_v')) {
            return route.fulfill({
                status: 200,
                contentType: 'text/html',
                body: `<html><head><script type="module" crossorigin src="/assets/${drift.on ? NEW_HASH : OLD_HASH}.js"></script></head><body></body></html>`,
            })
        }
        return route.fallback()
    })
}

/** 把当前页面的 build id 钉成 OLD:注入一个 vite 形态的 script 标签 */
async function pinCurrentBuildId(page: Page) {
    await page.addInitScript((oldHash) => {
        const s = document.createElement('script')
        s.type = 'module'
        s.setAttribute('crossorigin', '')
        s.src = `/assets/${oldHash}.js`
        // 必须在 installVersionPoll 跑之前进 DOM —— 它启动时同步从 DOM 取 baseline
        document.addEventListener('readystatechange', () => {
            if (!document.querySelector(`script[src*="${oldHash}"]`)) document.head.appendChild(s)
        })
        const put = () => { if (document.head) document.head.appendChild(s) }
        if (document.head) put(); else document.addEventListener('DOMContentLoaded', put)
    }, OLD_HASH)
}

/** 夹具前提自证:钉进去的假 build id script 必须真的在 DOM 里,
 *  且 versionPoll 启动时读到的就是它。读不到 ⇒ `checkVersion` 第一行就 return,
 *  banner 不出、reload 也不发生 —— **每一条 bfcache 系的锁都会红,而且红得像被测代码坏了**。
 *  把它变成显式前提,失败时一眼能分清"夹具没钉住" vs "守卫真坏了"。 */
async function assertPinned(page: Page) {
    const pinned = await page.evaluate((h) =>
        !!document.querySelector(`script[type="module"][src*="/assets/${h}"]`), OLD_HASH)
    expect(pinned, '夹具前提:假 build id script 没进 DOM ⇒ versionPoll 的 baseline 为 null ⇒ 本锁测不到任何东西').toBe(true)
}

const START_PATH = '/login?from=qa&keep=1'

async function openApp(page: Page) {
    await page.goto(START_PATH)
    await expect(page.locator('input').first()).toBeVisible({ timeout: 20_000 })
}

/** 造 dirty:真的往输入框里打字(焦点也真的在里面) */
async function makeDirty(page: Page): Promise<string> {
    const input = page.locator('input').first()
    await input.click()
    await input.fill('用户填了一半的内容')
    return '用户填了一半的内容'
}

/** 造 non-dirty:清空 + 失焦 + **显式表达"输入已落地"**。
 *
 * 🔴 R1 之后这里必须多一步 submit:清空输入框本身就是一次 `input` 事件,
 *   而全局脏标志的规则是「只有提交或导航才清除,**失焦不清**」——
 *   所以"把字删掉再点别处"在语义上仍然是"有未提交的编辑动作"。
 *
 * 🔴 [2026-08-18 · 本组锁偶发红的真因] 光 blur 一次**不够**:
 *   /login 渲染完成时 **app 自己会把焦点放进输入框**(实测 `FOCUS[刚渲染完] active=INPUT`,
 *   且 DOM 上并没有 autofocus 属性 ⇒ 是 React effect 干的)。
 *   于是"clean"这个前提跟 app 的聚焦 effect **赛跑**:effect 落在 blur 之后时,
 *   `activeElement` 又变回 INPUT ⇒ 退化兜底判定为 dirty ⇒ **出 banner 而不是静默刷**
 *   ⇒ 所有"期望照旧静默刷"的锁(锁1R+锁2 / 锁8)就红了。
 *   实测证据:不做 makeClean 时,8/8 全是 `banner=1 · reloadMs=-1`(60s 内根本不刷)。
 *   ⇒ 这不是守卫坏了,是**夹具没把它想表达的状态真正建立起来**。
 *   修法:轮询到焦点确实离开可输入元素为止,期间反复 blur;拿不到就让前提断言炸,
 *   而不是把它当成"守卫的锅"。
 */
async function makeClean(page: Page) {
    const input = page.locator('input').first()
    await input.fill('')
    await page.evaluate(() => document.dispatchEvent(new Event('submit', { bubbles: true })))
    await expect.poll(async () => page.evaluate(() => {
        const el = document.activeElement as HTMLElement | null
        el?.blur?.()
        document.body.focus()
        const now = document.activeElement as HTMLElement | null
        const tag = (now?.tagName || '').toUpperCase()
        return (tag === 'INPUT' || tag === 'TEXTAREA' || !!now?.isContentEditable) ? 'editable' : 'clean'
    }), { timeout: 10_000 }).toBe('clean')
    // 焦点稳定之后再清一次全局标志:上面的 blur/focus 不产生 input 事件,但求稳
    await page.evaluate(() => document.dispatchEvent(new Event('submit', { bubbles: true })))
}

/** 前提自证:此刻必须**真的**不脏(焦点不在可输入元素上)。
 *  期望"照旧静默刷"的锁在触发前调它 —— 前提不成立时红在这里,
 *  一眼看出是夹具没造出 clean 态,而不是自愈被修死了。 */
async function assertClean(page: Page) {
    const state = await page.evaluate(() => {
        const el = document.activeElement as HTMLElement | null
        const tag = (el?.tagName || '').toUpperCase()
        return (tag === 'INPUT' || tag === 'TEXTAREA' || !!el?.isContentEditable) ? 'editable' : 'clean'
    })
    expect(state, '夹具前提:此刻应为 non-dirty,但焦点仍在可输入元素上 ⇒ 守卫会(正确地)出 banner 而不是刷').toBe('clean')
}

/** 触发 bfcache 复活那条静默路 */
async function firePageShowPersisted(page: Page) {
    await page.evaluate(() => {
        window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))
    })
}

/** 触发 chunk 404 那条静默路(真发一个 unhandledrejection,不直接调内部函数) */
async function fireChunkError(page: Page) {
    await page.evaluate(() => {
        window.dispatchEvent(new PromiseRejectionEvent('unhandledrejection', {
            promise: Promise.reject(new Error('Failed to fetch dynamically imported module: /assets/x.js')),
            reason: new Error('Failed to fetch dynamically imported module: /assets/x.js'),
        }))
    })
    // 吞掉这条 rejection,避免污染其它断言
    await page.evaluate(() => { window.addEventListener('unhandledrejection', (e) => e.preventDefault()) })
}

const banner = (page: Page) => page.locator('#__omnirank_update_banner')

/**
 * 🔴 [2026-08-18] 本组锁**需要一台不被打满的机器**;超时值是耐心,不是断言。
 *
 * 观测(全量跑,单位=整轮):
 *   · 空闲机器:14 轮里 13 轮 10/10 绿(另 1 轮红,见下);单条隔离跑 6/6 绿
 *   · **人为把 CPU 打满**(两个 busy loop):5 轮里 4 轮红,且**每轮红的锁都不同**
 *     (锁1R+锁2 / 锁8 / 锁9 轮流)
 *
 * 抓到的真失败长这样:
 *   锁1R+锁2 → `expect.poll(_r).not.toBeNull()` 超时 · Received: null
 *   = "非 dirty 该照旧静默刷"没在时限内刷完。
 *
 * 查过并**排除**的两个猜测(写下来,省得下一个人再查一遍):
 *   ① 事件发早了、监听还没挂上 —— 不成立:`installVersionPoll()` 在 main.tsx 顶层
 *      (第 99 行)执行,远早于 `createRoot`(第 204 行);而 openApp 要等 input 可见
 *      = React 已渲染 ⇒ 监听必然已挂。
 *   ② `hardReload` 里 `requestHttpCacheRecovery()` 无限挂起 —— 不成立:它自带
 *      3s `AbortController` 且 catch 吞异常(`lib/cacheRecovery.ts`)。
 *
 * 🔴 **我把 20s→45s 试过了,没解决 CPU 打满下的红** —— 所以别把超时值当解药,
 *   也别再往上加。结论是:silent 路要串起 cache-recovery(dev 环境这一发必然打在
 *   vite proxy 上并 EACCES)+ SW/caches 清理 + 一次真实 dev server 整页重载,
 *   机器被占满时这条链就是跑不完。**跑得慢 ≠ 守卫坏了。**
 *
 * ⇒ 使用约定:**在没有别的重活的机器上跑**。变异重放脚本已据此加了两道保险 ——
 *   基线红就拒跑并打印红在哪、以及"红的必须是预期那条"(见
 *   `scripts/mutation_runner_silent_reload_behavior.mjs`),偶发红不会顶包成 ✅。
 *
 * 断言内容一字未改:仍要求 URL 必须长出 `_r`、pathname 仍是 /login、from/keep 都在。
 * 变异重放照样把它们打红(MU1→锁1、MU3→锁2)⇒ 放宽耐心没让锁变成恒真。
 */
test('锁1 dirty + bfcache 复活 → 不刷 · banner 出 · 输入仍在', async ({ page }) => {
    await pinCurrentBuildId(page)
    await mockVersionDrift(page, { on: true })
    await openApp(page)
    await assertPinned(page)
    const typed = await makeDirty(page)

    await firePageShowPersisted(page)
    await expect(banner(page), 'dirty 时应改弹 banner 把选择权还给用户').toBeVisible({ timeout: 30_000 })

    expect(new URL(page.url()).pathname, 'dirty 时不该发生任何跳转').toBe('/login')
    await expect(page.locator('input').first(), '🔴 用户填的内容被刷没了 —— 这正是本包要修的事故')
        .toHaveValue(typed)
})

test('锁1R+锁2 非 dirty → 照旧静默刷,且 URL 仍是原路径(不是 /)', async ({ page }) => {
    await pinCurrentBuildId(page)
    await mockVersionDrift(page, { on: true })
    await openApp(page)
    await assertPinned(page)
    await makeClean(page)
    await assertClean(page)

    await firePageShowPersisted(page)

    // 🔴 反向对照:自愈没被修死 —— 没人在填时必须照旧刷
    await expect
        .poll(() => new URL(page.url()).searchParams.get('_r'), { timeout: 45_000 })
        .not.toBeNull()

    const after = new URL(page.url())
    // 🔴 ② 的对照:保路由保 query,不许跳首页
    expect(after.pathname, 'hardReload 又跳回首页了 —— 丢表单之外还丢路由').toBe('/login')
    expect(after.searchParams.get('from'), '原 query 被丢了').toBe('qa')
    expect(after.searchParams.get('keep'), '原 query 被丢了').toBe('1')
})

test('锁3 dirty + chunk 404 → 不刷 · banner 出 · 输入仍在', async ({ page }) => {
    await openApp(page)
    const typed = await makeDirty(page)

    await fireChunkError(page)
    await expect(banner(page), 'chunk 那条路 dirty 时也要出 banner').toBeVisible({ timeout: 15_000 })
    // 那条路是 setTimeout 500ms 后才 reload,等够时间再断言"没刷"
    await page.waitForTimeout(1500)
    expect(new URL(page.url()).pathname).toBe('/login')
    await expect(page.locator('input').first()).toHaveValue(typed)
})

test('锁3R 非 dirty 的 chunk 404 → 照旧静默刷', async ({ page }) => {
    await openApp(page)
    await makeClean(page)
    await assertClean(page)

    let navigated = false
    page.on('framenavigated', (f) => { if (f === page.mainFrame()) navigated = true })
    await fireChunkError(page)

    await expect.poll(() => navigated, { timeout: 20_000 }).toBe(true)
})

test('锁4 回归:60s 轮询那条非静默路径行为不变(出 banner,不自动刷)', async ({ page }) => {
    await pinCurrentBuildId(page)
    await mockVersionDrift(page, { on: true })
    await openApp(page)
    await assertPinned(page)
    await makeClean(page)          // 干净状态 —— 证明 banner 不是守卫拦出来的

    // visibilitychange 走的就是 silent=false 那条(与 60s 轮询同一分支)
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))

    await expect(banner(page), '非静默路径本来就该出 banner').toBeVisible({ timeout: 15_000 })
    await page.waitForTimeout(1200)
    expect(new URL(page.url()).searchParams.get('_r'),
        '🔴 非静默路径不许自动刷 —— 那条路是合规的,一行都不该被本包改动').toBeNull()
})

test('锁5 回归:被守卫拦下不消耗 30s cooldown(清掉 dirty 后仍能刷)', async ({ page }) => {
    await openApp(page)
    await makeDirty(page)

    await fireChunkError(page)                 // 被拦
    await page.waitForTimeout(800)
    expect(new URL(page.url()).pathname).toBe('/login')

    await makeClean(page)
    await assertClean(page)
    let navigated = false
    page.on('framenavigated', (f) => { if (f === page.mainFrame()) navigated = true })
    await fireChunkError(page)                 // 同一 30s 窗口内再来一次

    await expect
        .poll(() => navigated, { timeout: 20_000 })
        .toBe(true)   // 🔴 拦下那次若写了 cooldown,这次会被判成"刚刷过"而永远不刷
})

/**
 * ── R1/R2 · Codex 2026-08-17 两个真机复现,原样固化成锁 ──────────────────
 *
 * 它们打在同一个设计缺陷上:退化兜底只看 `document.activeElement`,**失焦即判净**。
 * 而"打了一半还没提交"跟焦点在哪毫无关系 —— 焦点是瞬时的,未提交是持续的。
 */

test('锁6 🔴 Codex 复现①:打字后**失焦**,bfcache 复活仍不许刷', async ({ page }) => {
    await pinCurrentBuildId(page)
    await mockVersionDrift(page, { on: true })
    await openApp(page)
    await assertPinned(page)

    const typed = await makeDirty(page)
    // 关键差别:只 blur,不清空 —— 用户点了别处,但字还在
    await page.evaluate(() => { (document.activeElement as HTMLElement | null)?.blur() })
    await page.evaluate(() => document.body.focus())

    await firePageShowPersisted(page)
    await expect(banner(page), '失焦之后就判成"没人在填"了 —— 真机上这一刀正好砍在用户身上')
        .toBeVisible({ timeout: 15_000 })
    expect(new URL(page.url()).pathname).toBe('/login')
    await expect(page.locator('input').first()).toHaveValue(typed)
})

test('锁7 🔴 Codex 复现②:chunk 排下 500ms 定时器**之后**才开始打字,也不许刷', async ({ page }) => {
    await openApp(page)
    await makeClean(page)          // 触发那一刻确实是干净的

    await fireChunkError(page)     // 此时守卫放行,排下 500ms 定时器
    // 在这 500ms 窗口内开始输入 —— 真机复现的正是这一段
    const input = page.locator('input').first()
    await input.click()
    await input.fill('窗口内才开始打的字')

    await page.waitForTimeout(1800)
    expect(new URL(page.url()).pathname,
        '500ms 是个**窗口**不是瞬间 —— 排定时器时没人填,不代表触发时也没人填').toBe('/login')
    await expect(input).toHaveValue('窗口内才开始打的字')
    await expect(banner(page), '被拦下之后应改弹 banner').toBeVisible({ timeout: 10_000 })
})

test('锁8 反向:全局脏标志被"提交"清掉之后,自愈恢复静默刷(没把自愈修死)', async ({ page }) => {
    await pinCurrentBuildId(page)
    await mockVersionDrift(page, { on: true })
    await openApp(page)
    await assertPinned(page)

    await makeDirty(page)
    await page.evaluate(() => { (document.activeElement as HTMLElement | null)?.blur() })
    // 模拟"提交"(表单 submit 事件) + 清空输入 —— 输入已落地
    await page.evaluate(() => {
        document.querySelectorAll('input').forEach(i => { (i as HTMLInputElement).value = '' })
        document.dispatchEvent(new Event('submit', { bubbles: true }))
    })
    // 🔴 与 makeClean 同因:/login 的聚焦 effect 可能把焦点抢回 input,
    //    那样退化兜底会(正确地)判 dirty ⇒ 出 banner 不刷 ⇒ 本锁红得像自愈被修死。
    await expect.poll(async () => page.evaluate(() => {
        const el = document.activeElement as HTMLElement | null
        el?.blur?.(); document.body.focus()
        const now = document.activeElement as HTMLElement | null
        const tag = (now?.tagName || '').toUpperCase()
        return (tag === 'INPUT' || tag === 'TEXTAREA' || !!now?.isContentEditable) ? 'editable' : 'clean'
    }), { timeout: 10_000 }).toBe('clean')
    await assertClean(page)

    await firePageShowPersisted(page)
    await expect
        .poll(() => new URL(page.url()).searchParams.get('_r'), { timeout: 20_000 })
        .not.toBeNull()
})

/**
 * ── 锁9 [2026-08-18 补] 退化兜底(`activeElement`)单独成锁 ────────────────
 *
 * 🔴 为什么现在才补:变异重放实测 **MU4(拆掉 activeElement 兜底)打不红** ——
 *   锁1/6/7 这些 dirty 锁全都先"打了字",于是 R1 加的**全局未提交标志**已经为真,
 *   兜底那一层拆掉与否它们都绿。⇒ 工单 §1 明文要求的那条退化判据
 *   (「`document.activeElement` 是 input/textarea/contenteditable 亦视为 dirty」)
 *   **今天没有任何锁在守**,谁把它删了都不会有人知道。
 *
 * 本锁刻意把那一层**单独隔离**出来:聚焦但**一个字都不打**
 *   ⇒ 未提交标志为 false、注册探针也不脏 ⇒ 判 dirty 的**唯一**依据就是 activeElement。
 * 它守的真实场景:用户点开输入框正在想怎么写(尤其移动端弹起键盘那几秒),
 * 此时切出去查一眼资料再回来 —— 不许把他正打开的这一屏刷掉。
 */
test('锁9 聚焦但没打字(仅 activeElement 兜底)+ bfcache 复活 → 仍不许静默刷', async ({ page }) => {
    await pinCurrentBuildId(page)
    await mockVersionDrift(page, { on: true })
    await openApp(page)
    await assertPinned(page)

    // 先把全局未提交标志显式清干净:本锁要隔离的是**兜底那一层**,
    // 若标志为真,拆掉兜底照样绿 —— 那就又变成一条恒真锁。
    await page.evaluate(() => document.dispatchEvent(new Event('submit', { bubbles: true })))

    // 只聚焦,不输入(不用 click().fill(),避免产生任何 input/change 事件)
    await page.locator('input').first().focus()
    const focusedTag = await page.evaluate(() => (document.activeElement?.tagName || '').toUpperCase())
    expect(focusedTag, '夹具前提:焦点必须真的在 input 上,否则本锁测的不是兜底那一层').toBe('INPUT')

    await firePageShowPersisted(page)

    await expect(banner(page), '聚焦中的输入框 = 有人正要填 —— 该出 banner 让他自己决定')
        .toBeVisible({ timeout: 15_000 })
    expect(new URL(page.url()).pathname, '不许被静默刷走').toBe('/login')
    expect(new URL(page.url()).searchParams.get('_r'), '出现 _r 就说明真刷了').toBeNull()
})
