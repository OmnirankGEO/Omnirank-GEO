/**
 * #89 · frontend-nogo 共享 fixture:**每个用例**自动断言「页面零崩溃」。
 *
 * ## 为什么要共享,而不是各 spec 自己写
 *
 * 实测本目录 14 个 spec 的三档分布:
 *
 * | 档 | 数量 | 说明 |
 * |---|---|---|
 * | 装了监听 **且有断言** | 1 | `wallet-auth.spec.ts`(两处 `expect(pageErrors).toEqual([])`) |
 * | 装了监听 **但无断言** | 1 | `defgeo-five-card-fallback.spec.ts` |
 * | 没装 | 12 | —— |
 *
 * 🔴 **中间那一档最危险:装了不断言,与根本没装,在报告里完全同形。**
 * 监听把崩溃收下来了,然后没有任何东西去看它 —— 于是「页面崩了」这件事
 * 既没有让用例红,也没有出现在任何输出里。
 * `defgeo-five-card-fallback.spec.ts:58` 自己的注释写得很准:
 *
 * > 页面崩进错误边界时,后面每一条断言都报「element(s) not found」——
 * > 与「元素确实没渲染」完全同形,会把人引去改被测组件。
 *
 * 它把 pageerror 收下来是为了**在失败时念出真因**,不是为了让崩溃本身判红。
 * 两个目的都要:所以这里既附到报告里,也断言为空。
 *
 * ## 用法
 *
 *   import { expect, test } from './_fixtures';   // 而不是 'playwright/test'
 *
 * `auto: true` ⇒ 不需要任何用例显式声明,全部 164 个用例自动生效。
 *
 * ## 例外
 *
 * 少数用例**故意**制造页面错误(测错误边界本身)。那种情况显式标注:
 *
 *   test('...', async ({ page }, testInfo) => {
 *     testInfo.annotations.push({ type: 'allow-pageerror', description: '为什么允许' });
 *     …
 *   });
 *
 * 🔴 标注必须写 `description`(为什么允许)—— 没有理由的例外等于把这道断言关掉,
 * 而关掉这件事本身不会留下任何痕迹。
 */
import { test as base, expect } from 'playwright/test';

export const test = base.extend<{ zeroPageErrors: void }>({
  zeroPageErrors: [
    async ({ page }, use, testInfo) => {
      const errors: string[] = [];
      page.on('pageerror', (e) => errors.push(String((e as Error)?.stack ?? e)));

      await use();

      if (errors.length) {
        // 先**附到报告**:即便本用例已因别的断言失败,真因也要看得见 ——
        // 这正是 defgeo-five-card-fallback 当初装监听的目的。
        await testInfo.attach('pageerror', {
          body: errors.join('\n--- 8< ---\n'),
          contentType: 'text/plain',
        });
      }

      const allowed = testInfo.annotations.some((a) => a.type === 'allow-pageerror');
      if (allowed) {
        const ann = testInfo.annotations.find((a) => a.type === 'allow-pageerror');
        expect(
          (ann?.description ?? '').trim().length,
          '🔴 `allow-pageerror` 例外必须写 description(为什么允许)—— ' +
            '没有理由的例外等于把这道断言关掉,而关掉这件事不会留下任何痕迹。',
        ).toBeGreaterThan(0);
        return;
      }

      expect(
        errors,
        `🔴 页面在本用例中崩了 ${errors.length} 次(未捕获异常)。\n` +
          '    崩溃之后每一条断言都会报「element(s) not found」,与「元素确实没渲染」同形,\n' +
          '    会把人引去改被测组件。真因见附件 pageerror。\n' +
          '    若本用例**故意**制造错误,用 ' +
          "`testInfo.annotations.push({ type: 'allow-pageerror', description: '理由' })` 标注。",
      ).toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };
export type { BrowserContext, Page, Route, Locator } from 'playwright/test';
