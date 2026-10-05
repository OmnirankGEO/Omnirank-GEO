/**
 * 小榜 FAB · 真浏览器碰撞与键盘可达(P1-3③)
 *
 * 静态锁(`tests/xiaobang_vnext_2026_08_18/test_fab_accessibility.py`)判的是
 * **常量与源码**;它证明不了浏览器真排出来的盒子。这一份判据打在
 * `boundingBox()` 上,量的是真像素。
 *
 * 🔴 这份文件第一版就抓到了我自己的一个错误前提:它按 `nav.fixed.bottom-0`
 *    找「移动端 tab bar」,**恒取不到** —— 因为 `MobileTabBar` 全仓
 *    `<MobileTabBar` 挂载点 0 个,根本不渲染。R2 交付里"FAB 压住 tab bar"
 *    那句话是错的。静态推理不会发现这件事,真浏览器一跑就露。
 *
 * 🔴 R3-P3 又抓到**第三种**"锚不对",比死锚更隐蔽:锚活着,但和被测对象
 *    **永不同屏**。`OnboardingChecklist` 圆球确实会渲染,可 `Layout.tsx`
 *    用 `!sandboxUI` 关掉小榜 FAB,用 `sandboxUI` 打开圆球 —— 互斥。
 *    R3-P2 把它算进"共存 CTA"是错的。本文件用一条**互斥实证**把这件事测出来。
 *
 * 判据分工(R3-P3 后):
 *   · 四档视口 / 反向对照 / DPR —— 用**合成条**模拟页面级 `fixed bottom-0`
 *     (选词 BottomActionBar / 报价预览 / 采集填写 / 素材确认),合成是**故意**的:
 *     它要的是"随便哪个页面来一条底部条都不能压住",不依赖某条路由碰巧有;
 *   · 真路由 `/agent/preview` —— 零合成,量该页自己的 bar,按身份分流;
 *   · 真实共存 CTA —— **零合成**,`NotificationBanner` 走它自己的渲染条件,
 *     且配一条"撤掉夹具则分母门必红"的反向对照。
 */
import { expect, test, type Page } from 'playwright/test'
import {
  installBackend,
  identityFrom,
  armNotificationBanner,
  triggerNotificationPoll,
  armSandboxChecklist,
} from './harness'

/** 验收门 §21 点名的三档宽度 + 一档 200% 缩放等效视口。 */
const VIEWPORTS = [
  { label: '320w', width: 320, height: 640 },
  { label: '390w', width: 390, height: 844 },
  { label: '768w', width: 768, height: 1024 },
  // 200% 浏览器缩放在**布局**意义上等价于 CSS 视口对折(390×844 → 195×422)。
  // 这是等效,不是真 zoom —— 命名里保留 @200% 但含义以此注释为准。
  { label: '390w@200%', width: 195, height: 422 },
] as const

const SYNTHETIC_BAR_ID = 'xbvnext-synthetic-bottom-bar'
const SYNTHETIC_BAR_HEIGHT = 64

type Box = { x: number; y: number; width: number; height: number }

function intersects(a: Box, b: Box): boolean {
  return (
    a.x < b.x + b.width &&
    b.x < a.x + a.width &&
    a.y < b.y + b.height &&
    b.y < a.y + a.height
  )
}

/** 注入一条与现役页面级底部条同形的固定操作条。 */
async function injectBottomBar(page: Page) {
  await page.evaluate(
    ({ id, height }) => {
      document.getElementById(id)?.remove()
      const bar = document.createElement('div')
      bar.id = id
      bar.style.cssText =
        `position:fixed;left:0;right:0;bottom:0;height:${height}px;z-index:20;background:#111;`
      bar.textContent = '主按钮'
      document.body.appendChild(bar)
    },
    { id: SYNTHETIC_BAR_ID, height: SYNTHETIC_BAR_HEIGHT },
  )
}

async function boxOf(page: Page, selector: string): Promise<Box> {
  const box = await page.locator(selector).boundingBox()
  expect(box, `${selector} 没有 boundingBox —— 判据输入是空的`).not.toBeNull()
  return box as Box
}

async function fabBox(page: Page): Promise<Box> {
  const fab = page.getByRole('button', { name: '打开小榜 GEO 助手' })
  await expect(fab).toBeVisible()
  const box = await fab.boundingBox()
  expect(box, 'FAB 没有 boundingBox —— 判据输入是空的').not.toBeNull()
  return box as Box
}

type Overlay = Box & { tag: string }

/**
 * 扫页面上**真实**的底部固定浮层(排除合成条、排除小榜 FAB 自己)。
 *
 * 🔴 合成条显式排除:R3-P2 那版是先注入再扫,分母恒非空 —— 门是摆设。
 */
async function collectRealBottomOverlays(page: Page): Promise<Overlay[]> {
  return page.evaluate(({ synthId }) => {
    const out: { tag: string; x: number; y: number; width: number; height: number }[] = []
    document.querySelectorAll<HTMLElement>('body *').forEach((el) => {
      if (el.id === synthId || el.closest(`#${synthId}`)) return
      const cs = getComputedStyle(el)
      if (cs.position !== 'fixed' || cs.display === 'none' || cs.visibility === 'hidden') return
      if (parseFloat(cs.opacity || '1') < 0.05) return
      const r = el.getBoundingClientRect()
      if (r.width < 8 || r.height < 8) return
      // 🔴 判"贴底 CTA"要看**起点**在不在下半屏,不是终点。
      //    用 `r.bottom >= 半屏` 会把整屏铺满的 fixed 容器(top=0,bottom=满屏)
      //    也收进来 —— 它不是底部 CTA,而且 FAB 就在它里面,必然相交,
      //    收进来会让碰撞判据恒红,同时让"撤掉夹具分母应为 0"的反向对照恒假。
      if (r.top < window.innerHeight * 0.5) return
      if (el.getAttribute('aria-label') === '打开小榜 GEO 助手') return
      if (el.querySelector('[aria-label="打开小榜 GEO 助手"]')) return   // FAB 的包裹层
      out.push({
        tag: el.tagName + (el.id ? '#' + el.id : '') +
             (el.getAttribute('aria-label') ? '[' + el.getAttribute('aria-label') + ']' : ''),
        x: r.x, y: r.y, width: r.width, height: r.height,
      })
    })
    return out
  }, { synthId: SYNTHETIC_BAR_ID })
}

/** 分母门:一个真实 CTA 都没扫到时**报红**,不许"没冲突"其实是"没东西"。 */
function assertRealCtaDenominator(overlays: Overlay[]): void {
  if (overlays.length === 0) {
    throw new Error('底部真实共存 CTA 分母为 0 —— 本条什么都没验证')
  }
}

/**
 * 等抽屉的遮罩真正卸载。
 *
 * 🔴 R3-P4 ③:`member-mobile` 那条「空格打开」曾超时,单跑 3.3s 全绿 ——
 *    典型的**基础设施/时序红**,不是产品红。真因是 Sheet 的 backdrop
 *    (`ui/sheet.tsx:31` 的 `fixed inset-0 z-50`)在收起动画期间仍然挂着,
 *    拦住了随后的 focus / 按键。`toBeHidden()` 判的是抽屉内容,管不到遮罩。
 *    所以这里显式等遮罩计数归零 —— 不是加 sleep,是等一个确定的 DOM 事实。
 */
async function waitForDrawerOverlayGone(page: Page) {
  await expect(
    page.locator('div.fixed.inset-0.z-50'),
    '抽屉遮罩没卸载 —— 后续按键会被它吃掉(这是时序红,不是产品红)',
  ).toHaveCount(0)
}

test.describe('小榜 FAB · 真浏览器', () => {
  for (const vp of VIEWPORTS) {
    test(`${vp.label}:FAB 不与底部操作条相交、且完整在视口内`, async ({ page }, testInfo) => {
      await installBackend(page, identityFrom(testInfo))
      await page.setViewportSize({ width: vp.width, height: vp.height })
      await page.goto('/dashboard')
      await injectBottomBar(page)

      const fab = await fabBox(page)
      const bar = await boxOf(page, `#${SYNTHETIC_BAR_ID}`)

      expect(
        intersects(fab, bar),
        `${vp.label}:FAB ${JSON.stringify(fab)} 与底部条 ${JSON.stringify(bar)} 相交`,
      ).toBe(false)

      // 缩放后最容易越界的就是右下角浮层。
      expect(fab.x).toBeGreaterThanOrEqual(0)
      expect(fab.y).toBeGreaterThanOrEqual(0)
      expect(fab.x + fab.width).toBeLessThanOrEqual(vp.width + 1)
      expect(fab.y + fab.height).toBeLessThanOrEqual(vp.height + 1)
    })
  }

  test('反向对照:把 FAB 压回修复前的 bottom:20px,同一套检查必须报相交', async ({ page }, testInfo) => {
    // 🔴 没有这一条,「四档都不相交」与「相交函数恒返回 false」完全同形。
    //    20px 不是随手挑的 —— 它就是 R2 修复前 `Layout.tsx` 写死的 `bottom-5`。
    await installBackend(page, identityFrom(testInfo))
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/dashboard')
    await injectBottomBar(page)

    await page.evaluate(() => {
      const fab = document.querySelector<HTMLElement>('button[aria-label="打开小榜 GEO 助手"]')
      if (fab) fab.style.bottom = '20px'
    })
    const fab = await fabBox(page)
    const bar = await boxOf(page, `#${SYNTHETIC_BAR_ID}`)
    expect(
      intersects(fab, bar),
      '压回旧位置后仍判不相交 —— 碰撞判据是瞎的',
    ).toBe(true)
  })

  test('DPR 模拟(deviceScaleFactor=2 · 非浏览器缩放):FAB 仍不与底部操作条相交', async ({ page }, testInfo) => {
    // 🔴 **表述更正(Codex R2 会签)**:这一条测的是 **DPR(设备像素比)模拟**,
    //    **不是**浏览器 200% 缩放。`Emulation.setDeviceMetricsOverride` 的
    //    `deviceScaleFactor` 改的是物理像素/CSS 像素之比,浏览器缩放改的是
    //    CSS 视口尺寸与根字号 —— 两者影响的排版路径不同,不能互相冒充。
    //    真正的浏览器缩放 Playwright 没有直接 API;上面那档 `390w@200%` 用
    //    **CSS 视口对折**做等效(布局意义上等价),真 zoom 未覆盖,已写进未完成清单。
    await installBackend(page, identityFrom(testInfo))
    const client = await page.context().newCDPSession(page)
    await client.send('Emulation.setDeviceMetricsOverride', {
      width: 195, height: 422, deviceScaleFactor: 2, mobile: true,
    })
    await page.goto('/dashboard')
    await injectBottomBar(page)

    const fab = await fabBox(page)
    const bar = await boxOf(page, `#${SYNTHETIC_BAR_ID}`)
    expect(
      intersects(fab, bar),
      `DPR2:FAB ${JSON.stringify(fab)} 与底部条 ${JSON.stringify(bar)} 相交`,
    ).toBe(false)
    await client.send('Emulation.clearDeviceMetricsOverride')
  })

  test('键盘可达:聚焦入口按钮后回车能打开抽屉', async ({ page }, testInfo) => {
    await installBackend(page, identityFrom(testInfo))
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/dashboard')

    const fab = page.getByRole('button', { name: '打开小榜 GEO 助手' })
    await fab.focus()
    // 焦点必须真的落在 FAB 上 —— 否则下一步的回车测的是别的元素。
    await expect(fab).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('heading', { name: '小榜 · GEO 助手' })).toBeVisible()
  })

  test('键盘可达:抽屉自带的可拖拽 FAB 也能用空格打开', async ({ page }, testInfo) => {
    // 🔴 这一条才是修复前真正坏掉的那个按钮:抽屉挂载后入口换成 AgentFAB,
    //    它此前只绑指针事件,回车/空格完全没反应。
    await installBackend(page, identityFrom(testInfo))
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/dashboard')

    await page.getByRole('button', { name: '打开小榜 GEO 助手' }).click()
    await expect(page.getByRole('heading', { name: '小榜 · GEO 助手' })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.getByRole('heading', { name: '小榜 · GEO 助手' })).toBeHidden()
    await waitForDrawerOverlayGone(page)

    const draggable = page.getByRole('button', { name: '打开小榜 GEO 助手' })
    await draggable.focus()
    await expect(draggable).toBeFocused()
    await page.keyboard.press(' ')
    await expect(page.getByRole('heading', { name: '小榜 · GEO 助手' })).toBeVisible()
  })


  test('真路由 /agent/preview:FAB 不与该页真实的底部操作条相交(按身份分流)', async ({ page }, testInfo) => {
    // 🔴 Codex R3 P2 要的"真路由碰撞":不注入合成条,量的是
    //    `pages/Agent/QuotePreview.tsx` 自己渲染的 `fixed bottom-0 … sm:hidden z-40`。
    //    该条只在 sm 以下出现,所以必须用窄视口。
    //
    // 🔴 R3-P3 更正:上一版对**全部 4 身份**都要求这条 bar 存在,结果
    //    normal / member 全红 —— 这两个身份在 `/agent/preview` 看到的是
    //    「需要升级才能使用」升级墙(模块级 RBAC),页面根本没进业务态。
    //    处置不是放宽成"找不到就跳过"(那正是死锚没被发现的原因),
    //    而是**按身份分流并各自要证据**:能进的必须有 bar,进不去的必须看到墙。
    const identity = identityFrom(testInfo)
    await installBackend(page, identity)
    await page.setViewportSize({ width: 390, height: 844 })
    // QuotePreview 在 `!quoteData` 时 `return null`(底部条也就不存在)。
    // 它自带一条**内联数据**入口(?services=…),用它把页面喂到真实渲染态 ——
    // 这比给 harness 加一套报价 mock 更窄,也不影响另一个 spec。
    const services = encodeURIComponent(JSON.stringify([
      { name: 'GEO 诊断', quantity: 1, unit_price: 1000, subtotal: 1000 },
    ]))
    await page.goto(`/agent/preview?services=${services}`)

    if (identity === 'normal' || identity === 'member') {
      await expect(
        page.getByText('需要升级才能使用'),
        `${identity} 既没看到升级墙也没进业务态 —— 说不清这条为什么是空的`,
      ).toBeVisible()
      await expect(page.locator('div.fixed.bottom-0')).toHaveCount(0)
      return
    }

    const bar = page.locator('div.fixed.bottom-0').first()
    await expect(bar, '/agent/preview 上没渲染出底部操作条 —— 本条判据是空的').toBeVisible()
    const barBox = (await bar.boundingBox()) as Box
    const fab = await fabBox(page)

    expect(
      intersects(fab, barBox),
      `真路由:FAB ${JSON.stringify(fab)} 与 /agent/preview 底部条 ${JSON.stringify(barBox)} 相交`,
    ).toBe(false)
  })

  test('真实共存 CTA(零合成条):FAB 与 NotificationBanner 不相交', async ({ page }, testInfo) => {
    // 🔴 R3-P3(Review ②):这一条**不注入**合成条,并把它显式排除在分母外。
    //    上一轮为了让分母非空,先 injectBottomBar 再扫 —— 那是自己造一个对象
    //    来验"不与对象相交",分母门永远不会红,等于没有门。
    //    这里让 NotificationBanner 走**它自己的**渲染条件(真接口数据 +
    //    它自己的 visibilitychange handler)进入可见态。
    await installBackend(page, identityFrom(testInfo))
    await armNotificationBanner(page)
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/dashboard')
    await expect(page.getByRole('button', { name: '打开小榜 GEO 助手' })).toBeVisible()

    await triggerNotificationPoll(page)
    await expect(
      page.getByText('有 1 篇稿件等你确认'),
      'NotificationBanner 没浮出 —— 真实 CTA 夹具没生效',
    ).toBeVisible()

    const fab = await fabBox(page)
    const overlays = await collectRealBottomOverlays(page)
    assertRealCtaDenominator(overlays)
    expect(
      overlays.some((o) => o.tag.includes(SYNTHETIC_BAR_ID)),
      '分母里混进了合成条 —— 本条要求零合成',
    ).toBe(false)

    for (const overlay of overlays) {
      expect(
        intersects(fab, overlay),
        `FAB ${JSON.stringify(fab)} 与真实浮层 ${overlay.tag} ${JSON.stringify(overlay)} 相交`,
      ).toBe(false)
    }
  })

  test('分母门反向对照:不喂真实状态时,同一个门必须报红', async ({ page }, testInfo) => {
    // 🔴 Review ②「分母门必须能因真实 CTA 缺席而红」。
    //    上一条全绿有两种解释:(a) 真实 CTA 在且不相交;(b) 门是瞎的。
    //    这一条把夹具撤掉 —— 同一个 collect + 同一个门,必须抛。
    await installBackend(page, identityFrom(testInfo))
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/dashboard')
    await expect(page.getByRole('button', { name: '打开小榜 GEO 助手' })).toBeVisible()

    const overlays = await collectRealBottomOverlays(page)
    expect(
      overlays.length,
      `没喂夹具却扫到了真实底部 CTA:${JSON.stringify(overlays)} —— 那上一条的分母不是夹具带来的`,
    ).toBe(0)

    let threw = false
    try {
      assertRealCtaDenominator(overlays)
    } catch {
      threw = true
    }
    expect(threw, '分母门在分母为 0 时没报红 —— 门是瞎的').toBe(true)
  })

  test('互斥实证:沙盒态下圆球在、小榜 FAB 不在(所以它不进共存分母)', async ({ page }, testInfo) => {
    // 🔴 R3-P3 的第三个"锚不对"发现,与两次死锚同源但更隐蔽:
    //    组件确实会渲染,**但永远不和被测对象同屏**。
    //    `Layout.tsx:295/321` 用 `!sandboxUI` 关掉小榜 FAB 与抽屉,
    //    `Layout.tsx:336` 用 `sandboxUI` 打开 OnboardingChecklist —— 互斥。
    //    源码注释写得很清楚:「沙盒态下隐藏小榜 AI 助手 · 防真实账号 AI 数据漏出」。
    //    所以 R3-P2 把圆球算进"共存 CTA"是错的,几何再准也验不到东西。
    //    这条把互斥**测出来**,免得下次又有人凭源码里有 `<OnboardingChecklist />`
    //    就把它塞回分母。
    await installBackend(page, identityFrom(testInfo))
    await armSandboxChecklist(page)
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/dashboard')

    // 🔴 这里用 CSS 定位而不是 getByRole:沙盒态会弹 `SandboxIntroModal`(Radix),
    //    它给同级内容加 `aria-hidden="true"`,圆球就从**可访问性树**里消失了 ——
    //    `getByRole` 因此取不到。要判的是"渲染没渲染",所以按属性取。
    await expect(
      page.locator('button[aria-label^="上手任务"]'),
      '沙盒态下圆球没出现 —— 互斥的另一半没证到',
    ).toBeVisible()
    await expect(
      page.locator('button[aria-label="打开小榜 GEO 助手"]'),
      '沙盒态下小榜 FAB 竟然还在 —— 那互斥结论不成立,圆球得回到共存分母',
    ).toHaveCount(0)
  })
})
