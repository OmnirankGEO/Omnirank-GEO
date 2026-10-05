/**
 * H0 小榜本地会话隔离 · 真浏览器双账号底座。
 *
 * 🔴 这份夹具最关键的一点:**token 必须是生产真会签出来的形状**。
 *    本仓付过「夹具用的键是生产从来不会发的键 ⇒ 判据全绿而生产必挂」的费
 *    (2026-08-20 `user_id` vs `id`)。这里反过来:如果夹具随手写一个
 *    `'browser-token'` 之类的假串,旧代码 `atob(token.split('.')[1])` 一样会抛、
 *    一样落到 `uanon` —— 判据看起来能抓到变异,但抓到的是**假串**,
 *    不是「真 JWT 也会挂」这个真命题。
 *
 *    所以 `mintProductionShapedToken()` 严格照 `auth/jwt_utils.py:54-56 / 133-144` 复刻:
 *      base64url(urlsafe) + rstrip('=') + json.dumps(..., ensure_ascii=False)
 *    并且 `assertTokenIsAtobHostile()` 当场断言 payload 段里确实含 `-` 或 `_` ——
 *    没有这条,夹具哪天被换成纯 ASCII 显示名,变异就悄悄杀不动了。
 */
import { expect, type Page, type Route } from 'playwright/test'

export interface IsolationAccount {
  id: number
  username: string
  displayName: string
  /** 这个账号在抽屉里发的第一句话 —— 同时是它的会话标题。 */
  firstMessage: string
  /** 小榜给它的回答正文;跨账号泄漏时会连正文一起泄。 */
  answer: string
}

/**
 * 两个账号刻意都用**中文显示名 + 中文角色名**:这正是 `ensure_ascii=False`
 * 把非 ASCII 塞进 payload、逼出 base64url `-`/`_` 的真实生产条件。
 *
 * 🔴 显示名不是随手取的:是否噎死 atob **强烈依赖具体汉字**(本地实测 20 个真实风格
 *    中文显示名 × 400 组随机 uid/iat = 8000 样本,整体 37.7% 会噎死,且**按名字极度两极**
 *    ——「赵敏运营 / 杭州西子 / 广州鲸鸣」100%,「李明 / 张伟 / 陈静」0%)。
 *    这里挑的两个名字在**固定 iat/exp** 下逐字确定含 `-` / `_`;
 *    `assertTokenIsAtobHostile()` 每跑一次都当场复核,防止哪天被人改成 0% 的那一档
 *    ——那会让「恢复 atob 必须变红」这条反向对照**静默失效**。
 */
export const ACCOUNT_A: IsolationAccount = {
  id: 5011,
  username: 'iso-agent-a',
  displayName: '赵敏运营',
  firstMessage: 'A账号私密问题·员工席位怎么分配',
  answer: 'A账号的回答正文·不该被B看到',
}

export const ACCOUNT_B: IsolationAccount = {
  id: 5022,
  username: 'iso-agent-b',
  displayName: '杭州西子',
  firstMessage: 'B账号自己的问题·怎么发起品牌体检',
  answer: 'B账号的回答正文',
}

function base64urlNoPad(input: string): string {
  // Node 侧复刻 `base64.urlsafe_b64encode(...).rstrip(b'=')`
  return Buffer.from(input, 'utf-8')
    .toString('base64')
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
}

/** 照 `auth/jwt_utils.py` 的 payload 键与编码方式铸一个生产形状的 token。 */
export function mintProductionShapedToken(account: IsolationAccount): string {
  // 🔴 判据里不许有「今天」:iat/exp 用固定值。用 Date.now() 时 token 的字节会随
  //    运行时刻漂移,而「含不含 -/_」正是**逐字节**决定的 —— 那会让这把锁间歇性失效。
  //    前端从不验签也不读 exp(AuthContext 只把 token 原样送出),固定值不影响真实性。
  const IAT = 1_755_000_000
  const EXP = 2_050_000_000
  const header = base64urlNoPad(JSON.stringify({ alg: 'HS256', typ: 'JWT' }))
  const payload = base64urlNoPad(JSON.stringify({
    user_id: account.id,
    username: account.username,
    display_name: account.displayName,
    is_admin: false,
    roles: [{ id: 2, name: 'agent', display_name: '服务商' }],
    permissions: [
      'diagnosis:read', 'diagnosis:write', 'quote:read', 'quote:write',
      'writing:read', 'writing:write', 'monitoring:read', 'monitoring:write',
      'users:read', 'settings:read',
    ],
    client_brand_ids: [101],
    perm_version: 7,
    must_change_password: 0,
    iat: IAT,
    exp: EXP,
    team_context: null,
  }))
  // 签名段内容与判据无关(前端从不验签),但形状必须是三段。
  return `${header}.${payload}.${base64urlNoPad('iso-signature')}`
}

/**
 * 夹具自检:payload 段必须含 `-` 或 `_`,否则浏览器 `atob()` 不会抛,
 * 「恢复 atob 解析必须变红」这条反向对照就是空的。
 */
export function assertTokenIsAtobHostile(token: string, label: string) {
  const payloadSegment = token.split('.')[1]
  expect(payloadSegment, `${label}: token 必须有 payload 段`).toBeTruthy()
  expect(
    /[-_]/.test(payloadSegment),
    `${label}: payload 段必须含 base64url 专有字符(-/_),否则 atob 不会抛、变异杀不动`,
  ).toBe(true)
}

export const XIAOBANG_SESSION_KEY_PREFIX = 'omnirank_xiaobang_sessions_'
export const XIAOBANG_MESSAGE_KEY_PREFIX = 'omnirank_xiaobang_msgs_'

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

function userFor(account: IsolationAccount) {
  return {
    id: account.id,
    username: account.username,
    display_name: account.displayName,
    is_admin: false,
    is_active: 1,
    must_change_password: 0,
    agent_level: 1,
    roles: [{ id: 2, name: 'agent', display_name: '服务商' }],
    permissions: [
      'diagnosis:read', 'diagnosis:write', 'quote:read', 'quote:write',
      'writing:read', 'writing:write', 'monitoring:read', 'monitoring:write',
      'users:read', 'settings:read',
    ],
    client_brand_ids: [101],
    permission_version: 7,
  }
}

function sse(body: string): string {
  return [
    ': ready',
    '',
    'event: text',
    `data: ${JSON.stringify({ delta: body })}`,
    '',
    'event: meta',
    `data: ${JSON.stringify({ sources: [], confidence: 'high' })}`,
    '',
    'event: done',
    'data: {}',
    '',
  ].join('\n')
}

/**
 * 装后端 mock。`activeAccount` 是**可变引用** —— 切账号时改它一格,
 * 不用重装路由(重装会丢掉已经建立的 localStorage 状态,那正是本判据要保留的东西)。
 */
export function installIsolationBackend(page: Page, activeAccount: { current: IsolationAccount }) {
  return page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const account = activeAccount.current

    if (path === '/api/auth/me') return json(route, { success: true, user: userFor(account) })
    if (path === '/api/xiaobang/chat') {
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: sse(account.answer),
      })
    }
    if (path === '/api/xiaobang/context') {
      return json(route, {
        success: true,
        context: {
          brand_id: null, brand_name: null, current_page: '/dashboard',
          page_name: '今日工作台', data_updated_at: null, has_customer: false,
        },
        operation_plan: null,
      })
    }
    if (path === '/api/organization/overview') return json(route, { detail: 'not found' }, 404)
    if (path === '/api/client-context/list') return json(route, { success: true, clients: [] })
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 })
    if (path.includes('/agreement')) return json(route, { success: true, required: false })
    if (path === '/api/wallet') {
      return json(route, { success: true, balance: 100, available_balance: 100 })
    }
    return json(route, {
      success: true, data: {}, items: [], clients: [], brands: [], records: [], total: 0,
    })
  })
}

/**
 * 播种浏览器初始状态。
 *
 * 🔴 `omnirank_onboarding_state` 必须播:不播的话新手引导弹窗会盖在 FAB 上
 *    (`data-state="open"` 的 overlay 拦截点击),判据红在「点不到按钮」——
 *    那是**夹具没跑起来**,不是产品缺陷,两者必须分开。
 */
export function seedBrowser(page: Page, token: string) {
  return page.addInitScript((value) => {
    localStorage.setItem('omnirank_token', value)
    localStorage.setItem('sidebar-collapsed', 'false')
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'returning',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: '2026-08-01T00:00:00Z',
      last_updated_at: '2026-08-10T00:00:00Z',
    }))
  }, token)
}

/** 打开抽屉(与 xiaobang-unified 用同一套入口选择器)。 */
export async function openAssistant(page: Page) {
  const labelled = page.getByRole('button', { name: '打开小榜 GEO 助手' })
  if (await labelled.count()) {
    await labelled.click()
  } else {
    await page.locator('button.fixed.touch-none.select-none').click()
  }
  await expect(page.getByRole('heading', { name: '小榜 · GEO 助手' })).toBeVisible()
}

/** 读出当前 localStorage 里所有小榜命名空间键(判据的分母来源,机械枚举不手挑)。 */
export function readXiaobangStorageKeys(page: Page) {
  return page.evaluate(({ sessionPrefix, messagePrefix }) => {
    const keys: string[] = []
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i)
      if (!key) continue
      if (key.startsWith(sessionPrefix) || key.startsWith(messagePrefix)) keys.push(key)
    }
    return keys.sort()
  }, { sessionPrefix: XIAOBANG_SESSION_KEY_PREFIX, messagePrefix: XIAOBANG_MESSAGE_KEY_PREFIX })
}
