/**
 * #63 · 五卡投影为空时,必须回落完整报告,而不是渲染五个「暂无结论」空壳。
 *
 * 诊断 692 的现场:`/defensive-geo/reports/692/presentation` 返 200 但 `cards: []`,
 * 而 `DiagnosisReport.tsx` 的闸只判 `isV2` ⇒ 走五卡视图 ⇒ 五张卡全写「暂无结论」,
 * **且永远走不到 `ReportV2View`** —— 331KB 完整内容一直在,只是被那一行挡住了。
 *
 * 🔴 判据必须打在**浏览器渲染结果**上,不能只测那个布尔条件:
 *    缺陷本体在 JSX 渲染分支里,纯逻辑判据在缺陷现场恒绿。
 */
import { expect, test, type Page, type Route } from './_fixtures';

const DIAG_ID = 692;

const agentUser = {
    id: 102,
    username: 'five-card-fallback-reviewer',
    display_name: '服务商',
    is_admin: false,
    is_active: 1,
    must_change_password: 0,
    agent_level: 1,
    roles: [{ id: 2, name: 'geo_agent', display_name: '服务商' }],
    permissions: ['diagnosis:read'],
    client_brand_ids: [77],
};

/** 完整报告正文 —— 必须以 V2 marker 开头,否则 isReportV2 为 false,回落就无处可去。 */
const FULL_REPORT = [
    '## 1 分钟结论',
    '',
    '这里是完整报告的正文,共有实质内容。',
    '',
    '## 详细分析',
    '',
    '第二节内容。',
].join('\n');

/** 一张真卡 —— 正样本臂用。 */
const ONE_CARD = {
    key: 'identity',
    question: 'AI 认得我吗?',
    levelLabel: '基本认得',
    levelTone: 'positive',
    state: 'ok',
    actions: [],
};

function json(route: Route, body: unknown, status = 200) {
    return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

type Mounted = { analyticsEvents: string[] };

async function mountReport(page: Page, cards: unknown[]): Promise<Mounted> {
    const analyticsEvents: string[] = [];

    // 🔴 页面崩进错误边界时,后面每一条断言都报「element(s) not found」——
    //    与「元素确实没渲染」完全同形,会把人引去改被测组件。
    //    把 pageerror 收下来,断言失败时直接念出真因(实测:两轮红都是崩,不是没渲染)。
    const pageErrors: string[] = [];
    page.on('pageerror', e => pageErrors.push(String((e as Error)?.stack ?? e)));

    await page.addInitScript(() => {
        localStorage.setItem('omnirank_token', 'local-intercept-only');
        // 首次引导弹窗的遮罩会吃掉一切点击/可见性判断 —— 预先关掉。
        const now = new Date().toISOString();
        localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
            version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [],
            dismissed_features: [], viewed_videos: [], first_seen_at: now, last_updated_at: now,
        }));
    });

    await page.route('**/api/**', async route => {
        const url = new URL(route.request().url());
        const path = url.pathname;

        if (path === '/api/auth/me') return json(route, { success: true, user: agentUser });

        if (path === '/api/analytics/event') {
            // 埋点是**批量**投递的:一个请求里可能带多条。逐条收,别只看第一条。
            try {
                const body = JSON.parse(route.request().postData() || '{}');
                const list = Array.isArray(body?.events) ? body.events : [body];
                for (const e of list) if (e?.event) analyticsEvents.push(String(e.event));
            } catch { /* 解析失败不影响被测行为,但会让事件臂红 —— 这是想要的 */ }
            return json(route, { success: true });
        }

        if (path === `/api/defensive-geo/reports/${DIAG_ID}/presentation`) {
            return json(route, {
                diagnosisId: DIAG_ID,
                isV2: true,
                campaignMode: 'defensive',
                modeUserLabel: '防守',
                customerPresentationSigned: true,
                unsignedPolicies: [],
                cards,
                projectionVersion: 'v1',
                questionPlanId: 'plan-1',
                questionPlanRevision: 1,
            });
        }

        if (path === `/api/diagnosis/${DIAG_ID}`) {
            // 🔴 `api` 是原生 axios:`res.data` **就是响应体**,没有解包层。
            //    代码写的是 `detail: detailRes.data` / `contentRes.data.content`,
            //    所以字段必须在**顶层**。包成 {success,data:{...}} 是「夹具供了
            //    生产不会供的东西」—— 页面会退成「报告内容暂未生成」,
            //    而那与「组件没渲染」在断言读数上同形。
            return json(route, {
                id: DIAG_ID, brand_name: '测试品牌', brand_id: 77,
                total_score: 67, level: 'B', created_at: '2026-09-01T00:00:00Z',
            });
        }
        if (path === `/api/diagnosis/${DIAG_ID}/content`) {
            return json(route, { content: FULL_REPORT });
        }
        if (path === `/api/diagnosis/${DIAG_ID}/type`) return json(route, { scope: 'geo' });

        // 🔴 报告页 lazy 渲染 `DiagnosisRecommendationBehavior`,它逐字读
        //    `data.summary.stability_status`(:90,**无可选链保护**)。兜底若不给 summary,
        //    整页进错误边界「页面出错了」—— 而那与「回落说明没渲染」在断言读数上同形
        //    (实测:反臂第一次红就是这个,快照才看出来是崩了不是没渲染)。
        if (path.startsWith('/api/geo-observation')) {
            // 该组件读 `data.summary.*`(:90-122)与 `data.next_actions.length`(:138),
            // 两者都**没有可选链**。夹具缺哪一个,页面就整页崩 —— 见上面的 pageerror 说明。
            return json(route, {
                status: 'ok',
                summary: {
                    stability_status: 'stable', stability_explanation: '',
                    valid_observations: 10, presence_rate_bps: 5000,
                    explicit_recommendation_rate_bps: 3000,
                },
                outcomes: [],
                next_actions: [],
                items: [], platforms: [],
                baseline: { stability_status: 'stable' },
            });
        }

        // 兜底:其余 API 一律成功空响应,避免无关请求把页面打进错误态
        return json(route, { success: true, data: {}, items: [], clients: [] });
    });

    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto(`/diagnosis/report/${DIAG_ID}`);
    try {
        await expect(page.getByText('诊断报告详情')).toBeVisible({ timeout: 20_000 });
    } catch (err) {
        throw new Error(`页面未渲染出报告卡片。pageerror=${JSON.stringify(pageErrors)}
原始:${String(err)}`);
    }
    return { analyticsEvents };
}

test('正样本臂:cards 非空时**仍然**走五卡视图,回落说明不出现', async ({ page }) => {
    // 🔴 没有这一臂,「反臂绿」也可能是因为**永远回落**(五卡视图彻底废掉)——
    //    那是比原缺陷更坏的结果,而两者在反臂读数上完全同形。
    await mountReport(page, [ONE_CARD]);

    await expect(page.getByRole('heading', { name: 'AI 认得我吗?' })).toBeVisible();
    await expect(page.getByText('摘要卡片这次没能生成')).toHaveCount(0);
});

test('反臂:cards 为空时回落完整报告,且不出现「暂无结论」空壳', async ({ page }) => {
    await mountReport(page, []);

    // ① 五个空壳一个都不许有 —— 逐个卡片标题点名,不靠「暂无结论」这一个词
    //    (那个词换了别的说法,只查词的断言就恒绿了)。
    for (const q of ['AI 认得我吗?', 'AI 会推荐我吗?', '客户换种问法还能找到我吗?',
                     'AI 把我和谁放在一起?', 'AI 的说法有依据吗?']) {
        await expect(page.getByRole('heading', { name: q })).toHaveCount(0);
    }
    await expect(page.getByText('暂无结论')).toHaveCount(0);

    // ② 回落说明在,文案与 copy 登记一致(不手写第二份口径)
    await expect(page.getByText('摘要卡片这次没能生成，下面是完整报告，内容一条都不少。')).toBeVisible();

    // ③ 🔴 活性断言:完整报告**真的渲染出来了**。
    //    只断言「空壳没了」不够 —— 一个把整块内容都不渲染的改动会让 ① ② 全绿,
    //    而用户看到的是一片空白,比五个空壳更糟。
    await expect(page.getByText('这里是完整报告的正文,共有实质内容。')).toBeVisible();
});

test('回落只上报一次事件(供数据侧统计发生面)', async ({ page }) => {
    const { analyticsEvents } = await mountReport(page, []);

    // 埋点 debounce 5s;给足时间并容忍批量投递。
    await expect
        .poll(() => analyticsEvents.filter(e => e === 'defgeo_five_card_fallback').length,
              { timeout: 15_000 })
        .toBe(1);

    // 🔴 「恰好 1 次」不是洁癖:effect 随每次重渲染再跑,不去重就把**一次**回落
    //    报成 N 次,统计出来的是渲染次数不是「多少份报告踩到」——
    //    两个数在图表上长得一样,而只有后者能回答影响面。
});
