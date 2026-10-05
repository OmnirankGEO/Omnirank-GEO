/**
 * A 类判据 · **黄框在正常路径上还出不出现**(真浏览器 + 真 Vite 应用)
 * —— 工单 WO_PLAN_SIDE_MISMATCH_UPSTREAM_2026-09-03 §5/§7。
 *
 * ## 🔴 复现路径与工单描述的**不一样**,这一条是本单最重要的发现
 *
 * 工单(和我第一版 spec)以为是「她在关键词框里填了两行」。**不是。**
 * `#keywords` 那个 Textarea 只在 legacy(offensive)分支渲染
 * (`NewDiagnosis.tsx:2052` `{!isDefensiveFlow && (`),**defensive 下根本不在 DOM 里**。
 *
 * 真实路径:
 *   她在品牌名输入框里**从下拉里点选**那个老客户(`:1630` → `selectBrand` → `loadBrandData`)
 *   → 拉 `/api/brands/{id}/latest-diagnosis-params`
 *   → **用上一次诊断的关键词回填** `formData.keywords`(:433)
 *   → defensive 下这些行被迁成题且一律标 `offensive`
 *   → 后端 `validate_plan:279` 必抛 `plan_side_mismatch` ⇒ 黄框。
 *
 * 🔴 我一度以为"品牌名精确匹配就会自动回填"(:574 那条 effect)。**不对**:
 *    那条只 `setActiveBrandId`,**不调 `loadBrandData`** —— 回填只有走下拉点选那一条。
 *    结论(触发源是历史回填)是对的,**机制我第一版写错了**;
 *    错的机制会被下一个人当事实引用,所以订正留在这里而不是悄悄改掉。
 *
 * ⇒ **那两行不是她敲的,是系统从历史填的。** 她只是选了个老客户 + 选了"只测会问"。
 *   这也解释了为什么"删掉多出来的那几道"这个建议对她毫无意义 ——
 *   她根本没意识到题单里有那两道,更不知道它们从哪来。
 *
 * ## 断言口径
 *
 * 全部落在 **testid + aria 状态**,不比对句子(比句子 = 把判据钉在文案上,
 * 改一个字就红,而真正要锁的行为没被锁住)。唯一比句子的地方是
 * "黄框里那句话是**服务端给的**",那是 §15.8 的口径本身。
 */

const { test, expect } = require('playwright/test');

const MOCK = 'http://127.0.0.1:8018';

/** token 必须在应用加载**之前**落进 localStorage。 */
async function open(page, goal) {
    await page.addInitScript(() => {
        localStorage.setItem('omnirank_token', 'c14-fixture-token');
    });
    await page.goto(`/diagnosis/new?goal=${goal}`);
    // 🔴 首访引导弹窗会用一整块遮罩挡住**所有**交互(实测:点输入框报
    //    "div.fixed.inset-0.z-50 intercepts pointer events")。真实老用户点的就是"我已经用过"。
    //    🔴 必须**等它出现**再点:直接 `count()` 会在弹窗还没渲染时读到 0,
    //       于是这一步被静默跳过,后面每一次点击都被遮罩吃掉 —— 失败信息
    //       指向 `#brandName` "不可点",看起来像被测元素的问题,其实是弹窗。
    const skip = page.getByRole('button', { name: /我已经用过/ });
    await skip.waitFor({ state: 'visible', timeout: 15_000 }).catch(() => {});
    if (await skip.count()) {
        await skip.click();
        await page.getByRole('dialog').first()
            .waitFor({ state: 'detached', timeout: 10_000 }).catch(() => {});
    }
    // 🔴 活性断言:先证明我们**落在被测页面上**。
    //    夹具权限给不对时页面会渲染「当前账号没有此页面权限」,
    //    而后面每一条断言都会以"元素找不到"超时失败 —— 那是环境错,不是被测对象错,
    //    但两者的失败读数完全一样。这一行把它们分开。
    await expect(page.locator('#brandName')).toBeVisible({ timeout: 30_000 });
}

async function setScenario(page, name) {
    const res = await page.request.get(`${MOCK}/__scenario?name=${name}`);
    expect(res.ok()).toBeTruthy();
}

/** 从下拉里点选老客户 ⇒ 触发历史回填(这一步就是 Owner 那一格的全部操作)。 */
async function pickBrand(page, name) {
    // 🔴 用 `#brandName` 而不是 getByLabel:label 关联到的不是那个 input,
    //    `fill` 会"成功"但值仍是空串 —— 一个不报错的空操作。
    const input = page.locator('#brandName');
    await input.click();
    await input.pressSequentially(name, { delay: 30 });
    // 精确文本命中下拉项;题单里的建议题含品牌名但不是精确等值,所以 exact 能把它们排除。
    await page.getByText(name, { exact: true }).first().click();
}

const modeRadio = (page, title) => page.getByRole('radio', { name: new RegExp(title) });

test.describe('题单混侧:页面自己收敛,不弹黄框', () => {
    test.beforeEach(async ({ page }) => { await setScenario(page, 'ok'); });

    test('Owner 截图那一格:只测会问 + 选中有历史的老客户 ⇒ 自动改混合,零黄框', async ({ page }) => {
        await open(page, 'defensive');
        await pickBrand(page, '浙江岱林');

        // ① 自动收敛发生了,而且是**就地说明**不是错误
        await expect(page.getByTestId('plan-auto-hybrid-note')).toBeVisible({ timeout: 15_000 });
        await expect(modeRadio(page, '两条线一起看')).toHaveAttribute('aria-checked', 'true');

        // ② 🔴 主判据:黄框一个都没有
        await expect(page.getByTestId('plan-error')).toHaveCount(0);

        // ③ 条数是**算出来的**:历史里两条 ⇒ 说明里就是 2 道
        await expect(page.getByTestId('plan-auto-hybrid-note')).toContainText('2 道');
    });

    test('一键撤销可逆:改回只测会问 ⇒ 仍零黄框,再点回混合不卡住', async ({ page }) => {
        await open(page, 'defensive');
        await pickBrand(page, '浙江岱林');
        await expect(page.getByTestId('plan-auto-hybrid-note')).toBeVisible({ timeout: 15_000 });

        await page.getByTestId('plan-auto-hybrid-undo').click();
        await expect(modeRadio(page, '先守住品牌')).toHaveAttribute('aria-checked', 'true');
        await expect(page.getByTestId('plan-single-side-note')).toBeVisible();
        await expect(page.getByTestId('plan-error')).toHaveCount(0);

        // 🔴 死循环那一格:再点回混合必须真的回得去。
        await page.getByTestId('plan-single-side-undo').click();
        await expect(modeRadio(page, '两条线一起看')).toHaveAttribute('aria-checked', 'true');
        // 🔴 这一步**两句说明都不该有**:混合是她自己点的,
        //    再说一遍"已改成混合体检"就是对她撒谎(那句是给"我们替你改了"用的)。
        //    我第一版把这里写成"说明会回来",是**判据钉错了预期**,不是代码错。
        await expect(page.getByTestId('plan-auto-hybrid-note')).toHaveCount(0);
        await expect(page.getByTestId('plan-single-side-note')).toHaveCount(0);
        await expect(page.getByTestId('plan-error')).toHaveCount(0);
        // 🔴 活性断言:她的输入真的回到了**送出去的题单**里。
        //
        //    这条我换过三次尺子,前两把都是错的:
        //      · `getByText('生产消杀器械')` —— 题目不是文本节点,恒 0;
        //      · 读所有 `<textarea>` 的 value —— 页面上只有一个空的输入框,恒不含。
        //    两把尺子都给出"与代码无关的红"。**页面渲染形态不是这条命题的合适观测点。**
        //
        //    真正要证的是"这道题会被拿去体检",那就去看**真正发出去的请求体** ——
        //    它是这条命题的终态,渲染怎么变都不影响。
        const req = page.waitForRequest((r) => r.url().includes('/question-plans/preview')
            && r.method() === 'POST', { timeout: 20_000 });
        await page.locator('form button[type=submit]').first().click();
        const body = JSON.parse((await req).postData() || '{}');
        const sent = (body.questions || []);
        expect(sent.some((q) => (q.text || '').includes('生产消杀器械')
            && q.modeSide === 'offensive')).toBeTruthy();
    });

    test('她自己选单侧 ⇒ 选择器不跟她打架(不被自动切回混合)', async ({ page }) => {
        await open(page, 'hybrid');
        await pickBrand(page, '浙江岱林');

        await modeRadio(page, '先守住品牌').click();

        // 🔴 若自动收敛不认"她自己选的",这里会被立刻切回 hybrid ——
        //    屏幕上表现为"点了没反应",正是本单在修的那类症状。
        await expect(modeRadio(page, '先守住品牌')).toHaveAttribute('aria-checked', 'true');
        await expect(page.getByTestId('plan-single-side-note')).toBeVisible();
        await expect(page.getByTestId('plan-error')).toHaveCount(0);
    });

    test('反向对照:选没有历史的新客户 ⇒ 一句说明都不该冒出来', async ({ page }) => {
        await open(page, 'defensive');
        await pickBrand(page, '全新客户');

        // 🔴 没有这条,上面三条"说明可见"可能只是因为它**永远**可见。
        await expect(page.getByTestId('plan-auto-hybrid-note')).toHaveCount(0);
        await expect(page.getByTestId('plan-single-side-note')).toHaveCount(0);
        await expect(modeRadio(page, '先守住品牌')).toHaveAttribute('aria-checked', 'true');
    });
});

test.describe('保留的那一格:服务端在收敛之后仍拒绝', () => {
    test('注入 plan_side_mismatch ⇒ 黄框出现,且带一个能点的按钮', async ({ page }) => {
        await setScenario(page, 'side_mismatch');
        await open(page, 'hybrid');
        await pickBrand(page, '浙江岱林');
        // 🔴 用 `form button[type=submit]` 而不是按文案找:主 CTA 的字随价目状态变
        //    ('价目读取中…' / '价目不可用' / '信息齐了 · 点这里开始体检'),
        //    按文案钉会在与本单无关的状态下失败,那种红读起来像"被测行为坏了"。
        await page.locator('form button[type=submit]').first().click();

        const box = page.getByTestId('plan-error');
        await expect(box).toBeVisible({ timeout: 20_000 });
        // §15.8:话是服务端给的,前端不自己把 code 翻成中文
        await expect(page.getByTestId('plan-error-sentence')).toContainText('混进了另一类');
        // :83 提示二选一 —— 有明细就必须有能做事的按钮
        await expect(page.getByTestId('plan-error-fix')).toBeVisible();
    });
});
