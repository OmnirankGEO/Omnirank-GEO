/**
 * 微工单 WORKORDER_UI_DEDUCTION_TOAST 验收脚本 · Playwright 真渲染
 *
 * 前置：先在 frontend/ 起 dev server（端口默认 5311）
 *   npx vite --port 5311 --strictPort
 * 然后：
 *   node scripts/ui-verify/verify-deduction-toast.mjs
 *
 * 校验 5 组：
 *   A 四要素齐（容器 / 图标 / 会动的倒计时 / 取消按钮）+ 秒数真递减 + 进度条真走完
 *   B 倒计时走完自动执行
 *   C 点取消 → 立即 dismiss + 到点不执行不扣费
 *   D 小额直通（< 阈值不弹卡片，直接执行）
 *   E 移动端 375px 不溢出
 * 截图写入 ../../../agent-test-artifacts/ui-deduction-toast-2026-07-27/
 */
import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SHOTS = path.resolve(HERE, '../../../agent-test-artifacts/ui-deduction-toast-2026-07-27');
const URL = process.env.HARNESS_URL || 'http://localhost:5311/scripts/ui-verify/harness.html';

mkdirSync(SHOTS, { recursive: true });

const results = [];
const check = (name, pass, detail) => {
    results.push({ name, pass, detail });
    console.log(`${pass ? '✅' : '❌'} ${name}${detail ? ` — ${detail}` : ''}`);
};

const CARD = '[data-testid="deduction-countdown-toast"]';
const SECONDS = '[data-testid="deduction-countdown-seconds"]';
const BAR = '[data-testid="deduction-countdown-bar"]';
const CANCEL = '[data-testid="deduction-countdown-cancel"]';

async function setTheme(page, theme) {
    await page.evaluate((t) => {
        document.documentElement.classList.toggle('dark', t === 'dark');
    }, theme);
}

const browser = await chromium.launch();

// ─────────────────────────── A + B  桌面 · 递减 + 自动执行 ───────────────────────────
{
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    await page.goto(URL);
    await page.waitForSelector('[data-testid="trigger-large"]');
    await setTheme(page, 'dark');

    const t0 = Date.now();
    await page.click('[data-testid="trigger-large"]');
    await page.waitForSelector(CARD, { timeout: 2000 });

    // 四要素
    const card = page.locator(CARD);
    const hasIcon = await card.locator('svg').first().isVisible();
    const hasCancel = await page.locator(CANCEL).isVisible();
    const hasBar = await page.locator(BAR).isVisible();
    const labelText = (await card.locator('p').first().innerText()).trim();
    check('A1 容器渲染（卡片 + 圆角 + border + 阴影）', await card.isVisible(),
        await card.evaluate((el) => {
            const s = getComputedStyle(el);
            return `radius=${s.borderRadius} border=${s.borderTopWidth} shadow=${s.boxShadow !== 'none'} bg=${s.backgroundColor} blur=${s.backdropFilter}`;
        }));
    check('A2 左侧图标（Lucide Coins）', hasIcon);
    check('A3 主行动作文案', labelText.length > 0, labelText);
    check('A4 取消按钮（shadcn outline / sm）', hasCancel,
        await page.locator(CANCEL).evaluate((el) => el.className.includes('border-border') ? 'variant=outline' : el.className.slice(0, 60)));
    check('A5 底部进度条存在', hasBar);

    // 秒数逐秒递减 + 进度条 aria-valuenow 单调下降
    const series = [];
    const bars = [];
    for (let i = 0; i < 9; i++) {
        series.push({
            atMs: Date.now() - t0,
            text: (await page.locator(SECONDS).innerText().catch(() => '')).trim(),
        });
        // 进度条视觉进度 = Radix Indicator 的 translateX 占条宽比例（ui/progress.tsx 没把
        // value 透传给 Root，aria-valuenow 恒 0 不可用；这里量真实渲染出来的 transform）
        bars.push(await page.locator(BAR).evaluate((root) => {
            const ind = root.querySelector('div');
            if (!ind) return null;
            const m = new DOMMatrixReadOnly(getComputedStyle(ind).transform);
            const w = root.getBoundingClientRect().width;
            return w > 0 ? Math.round(((w + m.m41) / w) * 100) : null;
        }).catch(() => null));
        if (i === 0) {
            await page.screenshot({ path: path.join(SHOTS, '01_desktop_dark_t0.png') });
        }
        if (i === 4) {
            await page.screenshot({ path: path.join(SHOTS, '02_desktop_dark_mid.png') });
        }
        await page.waitForTimeout(500);
    }
    const secNums = series.map((s) => Number((s.text.match(/(\d+)/) || [])[1]));
    const distinct = [...new Set(secNums.filter((n) => Number.isFinite(n)))];
    const monotonic = secNums.every((n, i) => i === 0 || !Number.isFinite(n) || n <= secNums[i - 1]);
    check('A6 倒计时秒数真递减（非静态文字）', distinct.length >= 3 && monotonic,
        `观测序列 ${JSON.stringify(series.map((s) => s.text))}`);
    const barsClean = bars.filter((b) => Number.isFinite(b));
    const barMonotonic = barsClean.every((b, i) => i === 0 || b <= barsClean[i - 1] + 1);
    check('A7 进度条真线性走完（≈100% → ≈0%）',
        barsClean.length > 2 && barsClean[0] > 80 && barsClean[barsClean.length - 1] < 20 && barMonotonic,
        `渲染进度 ${JSON.stringify(barsClean)}`);

    // 走完自动执行
    await page.waitForTimeout(500);
    const log = (await page.locator('[data-testid="exec-log"]').innerText()).trim();
    check('B1 倒计时走完自动执行 onConfirm', log.includes('EXECUTED'), `log="${log}"`);
    check('B2 卡片已消失', (await page.locator(CARD).count()) === 0);

    // 亮色主题也截一张（生产 :root 默认亮色）
    await setTheme(page, 'light');
    await page.click('[data-testid="clear-log"]');
    await page.click('[data-testid="trigger-large"]');
    await page.waitForSelector(CARD);
    await page.waitForTimeout(1200);
    await page.screenshot({ path: path.join(SHOTS, '03_desktop_light.png') });
    await page.waitForTimeout(4500);
    await page.close();
}

// ─────────────────────────── C  取消 ───────────────────────────
{
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    await page.goto(URL);
    await page.waitForSelector('[data-testid="trigger-large"]');
    await setTheme(page, 'dark');
    await page.click('[data-testid="trigger-large"]');
    await page.waitForSelector(CARD);
    await page.waitForTimeout(1500);
    await page.screenshot({ path: path.join(SHOTS, '04_before_cancel.png') });
    await page.click(CANCEL);
    await page.waitForTimeout(400);
    const goneFast = (await page.locator(CARD).count()) === 0;
    await page.screenshot({ path: path.join(SHOTS, '05_after_cancel.png') });
    check('C1 点取消立即 dismiss（<0.4s）', goneFast);
    // 等过原定 5s 执行点 + 余量
    await page.waitForTimeout(5000);
    const log = (await page.locator('[data-testid="exec-log"]').innerText()).trim();
    check('C2 取消后到点不执行不扣费', !log.includes('EXECUTED'), `log="${log}"`);
    await page.close();
}

// ─────────────────────────── D  小额直通 ───────────────────────────
{
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    await page.goto(URL);
    await page.waitForSelector('[data-testid="trigger-small"]');
    await page.click('[data-testid="trigger-small"]');
    await page.waitForTimeout(300);
    const log = (await page.locator('[data-testid="exec-log"]').innerText()).trim();
    check('D1 小额（300 < 阈值 2000）直接执行', log.includes('EXECUTED-SMALL'), `log="${log}"`);
    check('D2 小额不弹倒计时卡片', (await page.locator(CARD).count()) === 0);
    await page.close();
}

// ─────────────────────────── E  移动端 375px ───────────────────────────
{
    const page = await browser.newPage({ viewport: { width: 375, height: 812 }, deviceScaleFactor: 2 });
    await page.goto(URL);
    await page.waitForSelector('[data-testid="trigger-large"]');
    await setTheme(page, 'dark');
    await page.click('[data-testid="trigger-large"]');
    await page.waitForSelector(CARD);
    await page.waitForTimeout(1200);
    await page.screenshot({ path: path.join(SHOTS, '06_mobile_375.png') });

    const box = await page.locator(CARD).boundingBox();
    const overflow = await page.evaluate(() => ({
        doc: document.documentElement.scrollWidth,
        win: window.innerWidth,
    }));
    check('E1 卡片不溢出视口', box.x >= 0 && box.x + box.width <= 375.5,
        `box x=${box.x.toFixed(1)} w=${box.width.toFixed(1)}`);
    check('E2 页面无横向滚动', overflow.doc <= overflow.win, JSON.stringify(overflow));

    // 主文案与取消按钮不互相挤压/裁剪
    const geo = await page.evaluate((sel) => {
        const card = document.querySelector(sel.card);
        const label = card.querySelector('p');
        const cancel = card.querySelector(sel.cancel);
        const lb = label.getBoundingClientRect();
        const cb = cancel.getBoundingClientRect();
        return {
            labelLines: Math.round(lb.height / parseFloat(getComputedStyle(label).lineHeight)),
            labelClipped: label.scrollWidth > label.clientWidth + 1,
            overlap: lb.right > cb.left + 0.5,
            cancelW: cb.width,
            cancelH: cb.height,
        };
    }, { card: CARD, cancel: CANCEL });
    check('E3 文案未被裁剪 / 未与取消按钮重叠', !geo.labelClipped && !geo.overlap, JSON.stringify(geo));
    await page.close();
}

await browser.close();

const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} 通过 · 截图目录: ${SHOTS}`);
if (failed.length) {
    console.log('FAILED: ' + failed.map((f) => f.name).join(' | '));
    process.exit(1);
}
