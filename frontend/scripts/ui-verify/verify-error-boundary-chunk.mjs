/**
 * 判别锁 · 部署后旧 chunk 失效必须静默自愈,而不是甩一页 stack trace。
 *
 * 前置：先在 frontend/ 起 dev server
 *   npx vite --port 5311 --strictPort
 *   ⚠️ 确认该端口上的是**本树**的服务器（本仓常有多个 worktree 抢同一端口，
 *      --strictPort 失败但别人的服务器仍会返回 200，会验到错的代码）。
 * 运行：node scripts/ui-verify/verify-error-boundary-chunk.mjs
 *   可用 HARNESS_BASE 覆盖基地址。
 */
import { chromium } from 'playwright';

const BASE = process.env.HARNESS_BASE || 'http://localhost:5311';
const URL_OF = (q) => `${BASE}/scripts/ui-verify/harness-error-boundary.html?${q}`;
const COOLDOWN_KEY = 'omnirank_chunk_reload_at';

const results = [];
const check = (name, ok, detail = '') => {
    results.push({ name, ok });
    console.log(`${ok ? '✅' : '❌'} ${name}${detail ? ' — ' + detail : ''}`);
};

const browser = await chromium.launch();
try {
    // ---- A: chunk 错误 → 人话过渡页，不出现 stack ----
    {
        const page = await browser.newPage();
        await page.goto(URL_OF('mode=chunk&cooldown=1'), { waitUntil: 'networkidle' });
        const body = await page.locator('body').innerText();
        check('A1 chunk 错误渲染人话过渡页', body.includes('正在更新到新版本'), body.slice(0, 60));
        check('A2 不再出现「页面出错了」标题', !body.includes('页面出错了'));
        check('A3 不再暴露 stack trace', !body.includes('stack trace') && !body.includes('Failed to fetch'));
        check('A4 提供「立即刷新」出口', body.includes('立即刷新'));
        await page.close();
    }

    // ---- B: 反向对照 —— 非 chunk 错误必须仍走原报错页 ----
    {
        const page = await browser.newPage();
        await page.goto(URL_OF('mode=other&cooldown=1'), { waitUntil: 'networkidle' });
        const body = await page.locator('body').innerText();
        check('B1 普通异常仍显示原报错页', body.includes('页面出错了'), body.slice(0, 60));
        check('B2 普通异常没被误判成 chunk', !body.includes('正在更新到新版本'));
    }

    // ---- C: 无冷却时真的排了 reload（冷却键被写入） ----
    {
        const page = await browser.newPage();
        // reload 会让页面重来一次；只断言冷却键被写入即可证明 maybeReloadOnChunkError 生效。
        await page.goto(URL_OF('mode=chunk'), { waitUntil: 'domcontentloaded' });
        await page.waitForFunction(
            (key) => Number(sessionStorage.getItem(key) || 0) > 0,
            COOLDOWN_KEY,
            { timeout: 5000 },
        ).then(() => check('C1 无冷却时写入冷却键（= 已排 reload）', true))
            .catch(() => check('C1 无冷却时写入冷却键（= 已排 reload）', false, '5s 内未写入'));
    }
} finally {
    await browser.close();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} 通过`);
if (failed.length) {
    console.log('FAILED: ' + failed.map((f) => f.name).join(' | '));
    process.exit(1);
}
