/**
 * H0 判据:同一浏览器里 A 退出、B 登录后,B 看不到 A 的小榜会话标题与消息正文。
 *
 * 工单:WORKORDER_XIAOBANG_SOLUTION_FIRST_AI_2026-08-20 §4 P0 / §8 S12 / §9.2。
 *
 * 反向对照(必须成对,拆锁自证写在交付单里):
 *   1. 把 `useXiaobangStorageNamespace()` 换回 `atob(token.split('.')[1])` → 两个账号
 *      同落 `..._uanon` → `A 的键必须是 u5011` 与 `B 的历史必须为空` 双双变红。
 *   2. 夹具自身活性:`assertTokenIsAtobHostile` 保证 token 真会噎死 atob;
 *      `登出不清小榜键` 保证泄漏前提真实存在(若哪天登出会清,本判据将不再是空转的绿)。
 */
import { expect, test } from 'playwright/test'
import {
  ACCOUNT_A,
  ACCOUNT_B,
  XIAOBANG_MESSAGE_KEY_PREFIX,
  XIAOBANG_SESSION_KEY_PREFIX,
  assertTokenIsAtobHostile,
  installIsolationBackend,
  mintProductionShapedToken,
  openAssistant,
  readXiaobangStorageKeys,
  seedBrowser,
  type IsolationAccount,
} from './isolation-harness'

const TOKEN_A = mintProductionShapedToken(ACCOUNT_A)
const TOKEN_B = mintProductionShapedToken(ACCOUNT_B)

/** 旧版留在盘上的公共池内容(线上真实存量的形状)。 */
const LEGACY_ANON_TITLE = '旧版uanon池里的标题·任何账号都不该看到'
const LEGACY_ANON_BODY = '旧版uanon池里的正文·任何账号都不该看到'

test('生产形状的 JWT 确实噎得死 atob —— 否则下面的变异对照是空的', async () => {
  assertTokenIsAtobHostile(TOKEN_A, 'ACCOUNT_A')
  assertTokenIsAtobHostile(TOKEN_B, 'ACCOUNT_B')
})

test('同浏览器 A 退出 → B 登录:B 看不到 A 的会话标题与正文,且各自落在自己的命名空间', async ({ page }) => {
  const active: { current: IsolationAccount } = { current: ACCOUNT_A }
  await installIsolationBackend(page, active)
  await seedBrowser(page, TOKEN_A)

  // ── A 登录并留下一段会话 ────────────────────────────────────────────
  await page.goto('/dashboard')
  await openAssistant(page)
  await page.locator('textarea').fill(ACCOUNT_A.firstMessage)
  await page.locator('button[title="发送"]').click()
  await expect(page.getByText(ACCOUNT_A.answer)).toBeVisible()

  // A 的会话确实落进了列表(否则后面「B 看不到」是空转的绿)
  await page.locator('button[title="历史记录"]').click()
  await expect(page.getByText(ACCOUNT_A.firstMessage.slice(0, 20))).toBeVisible()

  const keysAfterA = await readXiaobangStorageKeys(page)
  expect(keysAfterA.length, `A 必须真的写了小榜本地键;实得 ${JSON.stringify(keysAfterA)}`)
    .toBeGreaterThan(0)
  // 必须命中:全部落在 A 自己的命名空间
  for (const key of keysAfterA) {
    expect(key, 'A 的每一个小榜键都必须带 A 的 user id').toMatch(
      new RegExp(`^(${XIAOBANG_SESSION_KEY_PREFIX}|${XIAOBANG_MESSAGE_KEY_PREFIX})u${ACCOUNT_A.id}`),
    )
  }
  // 必须不命中:公共 anon 池一个键都不许有
  expect(keysAfterA.filter((key) => key.includes('uanon')), '不许出现 uanon 公共池').toEqual([])

  // ── A 退出 ────────────────────────────────────────────────────────
  // 照 `AuthContext.clearAuthScopedStorage` 真实做的那几件事:清 token、清 sessionStorage、
  // 清 interview_cache。它**不清**小榜键 —— 这正是泄漏得以发生的前提,下面当场证实。
  await page.evaluate(() => {
    localStorage.removeItem('omnirank_token')
    for (let i = localStorage.length - 1; i >= 0; i -= 1) {
      const key = localStorage.key(i)
      if (key === 'interview_cache' || key?.startsWith('interview_cache:')) {
        localStorage.removeItem(key)
      }
    }
    sessionStorage.clear()
  })
  const keysAfterLogout = await readXiaobangStorageKeys(page)
  expect(
    keysAfterLogout,
    '前提自证:登出不清小榜本地键 —— 若哪天清了,本判据会变成空转的绿,必须重写',
  ).toEqual(keysAfterA)

  // ── B 登录 ────────────────────────────────────────────────────────
  active.current = ACCOUNT_B
  await page.addInitScript((token) => {
    localStorage.setItem('omnirank_token', token)
  }, TOKEN_B)
  await page.evaluate((token) => localStorage.setItem('omnirank_token', token), TOKEN_B)
  await page.reload()
  await openAssistant(page)

  // B 的历史必须是空的
  await page.locator('button[title="历史记录"]').click()
  await expect(page.getByText('暂无历史对话')).toBeVisible()

  // A 的标题与正文都不许出现在 B 的任何一处 DOM 里
  await expect(page.getByText(ACCOUNT_A.firstMessage.slice(0, 20))).toHaveCount(0)
  await expect(page.getByText(ACCOUNT_A.answer)).toHaveCount(0)
  expect(await page.content()).not.toContain(ACCOUNT_A.answer)

  // ── B 自己写一段,证明两个池并存且互不覆盖 ─────────────────────────
  await page.locator('button[title="历史记录"]').click()
  await page.locator('textarea').fill(ACCOUNT_B.firstMessage)
  await page.locator('button[title="发送"]').click()
  await expect(page.getByText(ACCOUNT_B.answer)).toBeVisible()

  const keysAfterB = await readXiaobangStorageKeys(page)
  const aKeys = keysAfterB.filter((key) => key.includes(`u${ACCOUNT_A.id}`))
  const bKeys = keysAfterB.filter((key) => key.includes(`u${ACCOUNT_B.id}`))
  expect(aKeys.length, 'A 的旧键应当原样留在盘上(没被 B 顶掉,也没被 B 读到)').toBeGreaterThan(0)
  expect(bKeys.length, 'B 必须写进自己的命名空间').toBeGreaterThan(0)
  expect(keysAfterB.filter((key) => key.includes('uanon')), '不许出现 uanon 公共池').toEqual([])
  // 分母机械枚举:小榜键只能属于 A 或 B 两个命名空间,不许有第三种
  expect(
    keysAfterB.filter((key) => !aKeys.includes(key) && !bKeys.includes(key)),
    `小榜本地键只允许 u${ACCOUNT_A.id} / u${ACCOUNT_B.id} 两个命名空间`,
  ).toEqual([])
})

test('存量 uanon 池:修好之后,已鉴权用户不得再读到旧版留在盘上的公共历史', async ({ page }) => {
  // 🔴 这条打的是**真实迁移面**:线上此刻每一台落在那 37.7% 里的浏览器,盘上都躺着
  //    一份 `..._uanon` 历史(旧版写的)。修完之后它必须变成「读不到的死数据」,
  //    而不是「换个人登录照样能翻出来」。
  //
  //    也正因为把种子放进 `uanon` 而不是 `u5011`,这条才**真的有判别力**:
  //    上一版把种子放在 `u5011` —— 变异后命名空间是 `uanon`,照样读不到,判据恒绿。
  //    (「变异存活常因判据自己构造了中间值」——这里补上。)
  const active: { current: IsolationAccount } = { current: ACCOUNT_A }
  await installIsolationBackend(page, active)
  await seedBrowser(page, TOKEN_A)
  await page.addInitScript(({ sessionKey, msgKey, title, body }) => {
    localStorage.setItem(sessionKey, JSON.stringify([{
      id: 'xb_legacy_anon_1', title, updatedAt: 1_755_000_000_000, messageCount: 2,
      contextKey: 'unscoped',
    }]))
    localStorage.setItem(msgKey, JSON.stringify([
      { id: 'm1', role: 'user', content: title },
      { id: 'm2', role: 'assistant', content: body },
    ]))
  }, {
    sessionKey: `${XIAOBANG_SESSION_KEY_PREFIX}uanon`,
    msgKey: `${XIAOBANG_MESSAGE_KEY_PREFIX}uanon_xb_legacy_anon_1`,
    title: LEGACY_ANON_TITLE,
    body: LEGACY_ANON_BODY,
  })

  await page.goto('/dashboard')
  await openAssistant(page)

  // 历史面板必须是空的 —— 旧 uanon 池对已鉴权用户不可见
  await page.locator('button[title="历史记录"]').click()
  await expect(page.getByText('暂无历史对话')).toBeVisible()
  await expect(page.getByText(LEGACY_ANON_TITLE)).toHaveCount(0)
  expect(await page.content()).not.toContain(LEGACY_ANON_TITLE)
  expect(await page.content()).not.toContain(LEGACY_ANON_BODY)

  // 前提自证:种子确实还在盘上(证明是「没读到」,不是「压根没种进去」)
  const keys = await readXiaobangStorageKeys(page)
  expect(keys, '前提自证:uanon 种子必须真的在 localStorage 里').toContain(
    `${XIAOBANG_SESSION_KEY_PREFIX}uanon`,
  )

  // 这个账号自己写一段:必须落进 u<id>,一个字都不许写回 uanon
  await page.locator('button[title="历史记录"]').click()
  await page.locator('textarea').fill(ACCOUNT_A.firstMessage)
  await page.locator('button[title="发送"]').click()
  await expect(page.getByText(ACCOUNT_A.answer)).toBeVisible()

  const after = await readXiaobangStorageKeys(page)
  const own = after.filter((key) => key.includes(`u${ACCOUNT_A.id}`))
  expect(own.length, '本账号必须写进自己的命名空间').toBeGreaterThan(0)
  const anonAfter = await page.evaluate((key) => localStorage.getItem(key),
                                        `${XIAOBANG_SESSION_KEY_PREFIX}uanon`)
  expect(anonAfter, '旧 uanon 池不许被追写').not.toContain(ACCOUNT_A.firstMessage.slice(0, 20))
})

/**
 * ⚠️ 这条**不是锁,是前提/分母检查**:它证明「认证未完成时抽屉根本不挂载」,
 *    因此 `useXiaobangStorageNamespace` 里那条「未认证返回 null」的分支在**当前挂载路径下
 *    不承重**(变异 M2「未认证退回 uanon」在浏览器里跑不红 —— 等价变异,不是判据漏洞)。
 *    留着它是为了:哪天有人把抽屉挪到认证前也能挂载,这条会立刻变红。
 */
test('[前提检查·非锁] 认证未完成时抽屉不挂载 ⇒ 不会创建任何小榜本地键', async ({ page }) => {
  const active: { current: IsolationAccount } = { current: ACCOUNT_A }
  await installIsolationBackend(page, active)
  await seedBrowser(page, TOKEN_A)
  await page.route('**/api/auth/me', (route) => route.fulfill({
    status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'unavailable' }),
  }))

  await page.goto('/dashboard')
  await expect(page.getByRole('heading', { name: '小榜 · GEO 助手' })).toHaveCount(0)
  const keys = await readXiaobangStorageKeys(page)
  expect(keys, `未认证时不许建任何小榜本地键;实得 ${JSON.stringify(keys)}`).toEqual([])
})

/**
 * 🔴 跨标签页换号是**真实的、不刷新页面的**换号路径:
 *    `AuthContext:616` 监听 `storage`,拿到新 token 走 `reconcileExternalSession`。
 *
 *    ⚠️ 实测订正:`reconcileExternalSession` 会 `setUser(null) + setIsLoading(true)`,
 *    ProtectedRoute 因此进 loading 态、Layout 连带抽屉**会被卸载再重挂**
 *    (第一版这条判据断言"抽屉全程没被卸载",实跑当场红 —— 前提是错的)。
 *    ⇒ 结论:两条真实换号路径(重载 / 跨标签页)都以重挂收场,
 *      `useXiaobangSessions` 里那段**渲染期换池**属**纵深防御·非承重**,
 *      本套判据里没有任何一条驱动得到它(交付单 §弱点 已如实登记)。
 *    这条判据仍然值钱:它是**不刷新页面**的端到端隔离证明,与 test 2 的重载路径互补。
 */
test('跨标签页换号(不刷新):抽屉里 A 的会话必须当场消失,B 看到的是自己的空池', async ({ page }) => {
  const active: { current: IsolationAccount } = { current: ACCOUNT_A }
  await installIsolationBackend(page, active)
  await seedBrowser(page, TOKEN_A)

  await page.goto('/dashboard')
  await openAssistant(page)
  await page.locator('textarea').fill(ACCOUNT_A.firstMessage)
  await page.locator('button[title="发送"]').click()
  await expect(page.getByText(ACCOUNT_A.answer)).toBeVisible()
  await page.locator('button[title="历史记录"]').click()
  await expect(page.getByText(ACCOUNT_A.firstMessage.slice(0, 20))).toBeVisible()

  // 另一个标签页登录成 B:本标签页收到 storage 事件,抽屉不卸载。
  active.current = ACCOUNT_B
  await page.evaluate((token) => {
    const previous = localStorage.getItem('omnirank_token')
    localStorage.setItem('omnirank_token', token)
    window.dispatchEvent(new StorageEvent('storage', {
      key: 'omnirank_token', oldValue: previous, newValue: token, storageArea: localStorage,
    }))
  }, TOKEN_B)

  // B 的 /me 落地后重新打开抽屉(卸载→重挂是 AuthContext 的既有行为,见上面订正)
  await openAssistant(page)
  await page.locator('button[title="历史记录"]').click()
  await expect(page.getByText('暂无历史对话')).toBeVisible()
  // A 的标题与正文都不在 B 的任何一处 DOM 里
  await expect(page.getByText(ACCOUNT_A.firstMessage.slice(0, 20))).toHaveCount(0)
  await expect(page.getByText(ACCOUNT_A.answer)).toHaveCount(0)
  expect(await page.content()).not.toContain(ACCOUNT_A.answer)
  // A 的池仍原样留在盘上(证明是"换池"不是"清盘")
  const keys = await readXiaobangStorageKeys(page)
  expect(keys.filter((key) => key.includes(`u${ACCOUNT_A.id}`)).length,
         'A 的键必须还在盘上').toBeGreaterThan(0)
  expect(keys.filter((key) => key.includes('uanon')), '不许出现 uanon 公共池').toEqual([])
})
