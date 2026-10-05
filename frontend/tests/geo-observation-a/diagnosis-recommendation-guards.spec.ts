/**
 * #87 · 「AI 推荐行为」子模块:接口少给字段**不许崩整份诊断报告**。
 *
 * 背景:这个模块是 lazy 嵌进现役诊断报告的。它里面对 summary 的六个字段与
 * next_actions 的长度是不带守卫的直接取值 ⇒ 接口一次少给字段就抛 TypeError,
 * 整页进错误边界「页面出错了」,而且 lazy 加载崩的时机不固定。
 * (C 在 #63 域内实测:两轮判据红都是它崩的。)
 *
 * 🔴 断言的核心是 **零 pageerror**,不是"能看到某个字" ——
 *    「看到了兜底文案」与「压根没崩」是两件事:兜底文案可能在错误边界里也渲染得出来。
 *    所以每一臂都同时钉两样:①无未捕获异常 ②该出现的内容真的出现了。
 */
import { expect, test, type Page } from 'playwright/test';
import { installObservationMock, collectConsoleErrors, type Scenario } from './mock';

const HOME = '/geo-observation-preview.html';

/** 模块外壳的唯一锚 —— 不能用「AI 推荐行为」裸文本:预览页导航按钮也叫「诊断 · AI 推荐行为」,
 *  裸匹配会中 2 个(strict mode violation)。这个 hint 只在模块内出现一次。 */

async function openDiagnosis(page: Page, scenario: Scenario) {
    await installObservationMock(page, scenario);
    const errors = collectConsoleErrors(page);   // 已含 page.on('pageerror')
    await page.goto(`${HOME}#/diagnosis`);
    await page.waitForLoadState('networkidle');
    return errors;
}

/** 错误边界的文案 —— 出现它就等于整页崩了。 */
const BOUNDARY = /页面出错了|出错了|Something went wrong/;

test.describe('#87 缺字段不崩整页', () => {
    test('正样本臂:字段齐全时正常渲染(先证这套 harness 会亮)', async ({ page }) => {
        const errors = await openDiagnosis(page, 'happy');
        await expect(page.getByText('这个品牌当前在 AI 搜索里是被直接推荐')).toBeVisible();
        await expect(page.getByText(BOUNDARY)).toHaveCount(0);
        expect(errors, `happy 臂就有未捕获异常 ⇒ 后两臂的"没崩"不携带信息:\n${errors.join('\n')}`)
            .toEqual([]);
        // 这一臂必须真的渲染出统计块,否则"缺字段臂也没崩"可能只是两臂都没渲染
        await expect(page.getByTestId('dxrb-summary-missing')).toHaveCount(0);
    });

    test('🔴 缺字段臂:summary / outcomes / next_actions 整块缺席 ⇒ 不崩,子块自陈暂无', async ({ page }) => {
        const errors = await openDiagnosis(page, 'summary_missing_fields');

        // ① 零未捕获异常 —— 这是本卡的主命题
        expect(errors, `缺字段导致未捕获异常(#87 的原缺陷):\n${errors.join('\n')}`).toEqual([]);

        // ② 没有进错误边界
        await expect(page.getByText(BOUNDARY)).toHaveCount(0);

        // ③ 该子块自陈"暂无",而不是消失得无影无踪
        await expect(page.getByTestId('dxrb-summary-missing')).toBeVisible();

        // ④ 模块外壳仍在 —— 证明"没崩"不是因为整块没渲染
        await expect(page.getByText('这个品牌当前在 AI 搜索里是被直接推荐')).toBeVisible();
    });

    test('空数组臂:字段在但为空 ⇒ 同样不崩', async ({ page }) => {
        const errors = await openDiagnosis(page, 'summary_empty');
        expect(errors, `空数据臂出现未捕获异常:\n${errors.join('\n')}`).toEqual([]);
        await expect(page.getByText(BOUNDARY)).toHaveCount(0);
        await expect(page.getByText('这个品牌当前在 AI 搜索里是被直接推荐')).toBeVisible();
        // 空数据是**有** summary 的(只是值为 0/null)⇒ 不该出现缺字段兜底
        await expect(page.getByTestId('dxrb-summary-missing')).toHaveCount(0);
    });
});
