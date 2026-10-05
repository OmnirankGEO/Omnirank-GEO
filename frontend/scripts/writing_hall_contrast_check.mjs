/**
 * [写作质量总工单 2026-07-29 · D 组验收] Playwright 真渲染 + WCAG 对比度实测。
 *
 * 明/暗两种模式各渲染一次 scripts/writing_hall_contrast_harness.html
 * (它链接的是 vite build 真实产出的样式表 dist/assets/index-*.css,
 * 也就是线上发的那一份 —— 类名由 Tailwind JIT 从真源码扫出来),
 * 对每一处关键文字读 getComputedStyle 的实际颜色,沿 DOM 往上找到第一个
 * 不透明背景做 alpha 合成,再按 WCAG 2.1 相对亮度公式算对比度。
 *
 * 判定:正文/小字 >= 4.5:1;>=18.66px 或 >=14px+bold 的大字 >= 3:1。
 * 不接受肉眼"看着还行"。
 *
 * 两个已经踩过的坑,写在这防止下次重犯:
 *   1. Tailwind v4 的计算值是 oklch(...),正则抠数字会把 L/C/H 当成 R/G/B;
 *   2. `/透明度` 变体(bg-amber-50/60)的计算值是 oklab(...),必须单独处理。
 * 两次都会产出一堆**假 FAIL**,比没测更糟。
 *
 * 用法:
 *   cd frontend && node scripts/writing_hall_contrast_check.mjs [--out x.json] [--shots dir]
 */
import { chromium } from 'playwright';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import fs from 'node:fs';

const here = path.dirname(fileURLToPath(import.meta.url));
const harness = path.join(here, 'writing_hall_contrast_harness.html');

const args = process.argv.slice(2);
const outIndex = args.indexOf('--out');
const outFile = outIndex >= 0 ? args[outIndex + 1] : null;
const shotIndex = args.indexOf('--shots');
const shotDir = shotIndex >= 0 ? args[shotIndex + 1] : null;

function measure() {
  const encode = (v) => {
    const c = v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(v, 1 / 2.4) - 0.055;
    return Math.max(0, Math.min(255, Math.round(c * 255)));
  };
  const oklabToRgb = (L, a, b) => {
    const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
    const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
    const s_ = L - 0.0894841775 * a - 1.2914855480 * b;
    const l = l_ ** 3, m = m_ ** 3, s = s_ ** 3;
    return [
      encode(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
      encode(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
      encode(-0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s),
    ];
  };
  const parse = (raw0) => {
    const raw = String(raw0 || '').trim();
    if (!raw || raw === 'transparent') return [0, 0, 0, 0];
    const nums = (raw.match(/-?[\d.]+%?/g) || []).map((t) => (
      t.endsWith('%') ? parseFloat(t) / 100 : parseFloat(t)
    ));
    const alpha = nums.length > 3 ? nums[3] : 1;
    if (raw.startsWith('oklch')) {
      const [L, C, H] = nums;
      const h = ((H || 0) * Math.PI) / 180;
      return [...oklabToRgb(L, (C || 0) * Math.cos(h), (C || 0) * Math.sin(h)), alpha];
    }
    if (raw.startsWith('oklab')) {
      const [L, a, b] = nums;
      return [...oklabToRgb(L, a || 0, b || 0), alpha];
    }
    if (raw.startsWith('color(srgb')) {
      return [encode(nums[0]), encode(nums[1]), encode(nums[2]), alpha];
    }
    return [nums[0] || 0, nums[1] || 0, nums[2] || 0, alpha];
  };
  const srgb = (c) => {
    const v = c / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  };
  const lum = ([r, g, b]) => 0.2126 * srgb(r) + 0.7152 * srgb(g) + 0.0722 * srgb(b);
  const composite = (fg, bg) => {
    const a = fg.length > 3 ? fg[3] : 1;
    return [0, 1, 2].map((i) => Math.round(fg[i] * a + bg[i] * (1 - a)));
  };
  const effectiveBg = (el) => {
    const stack = [];
    let node = el;
    while (node) {
      const c = parse(getComputedStyle(node).backgroundColor);
      if (c[3] > 0) stack.push(c);
      if (c[3] >= 1) break;
      node = node.parentElement;
    }
    if (!stack.length) return [255, 255, 255];
    let base = stack[stack.length - 1].slice(0, 3);
    for (let i = stack.length - 2; i >= 0; i -= 1) base = composite(stack[i], base);
    return base;
  };
  const out = [];
  for (const el of document.querySelectorAll('[data-probe]')) {
    if (!el.textContent.trim()) continue;
    const cs = getComputedStyle(el);
    const bg = effectiveBg(el);
    const fg = composite(parse(cs.color), bg);
    const l1 = lum(fg);
    const l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const px = parseFloat(cs.fontSize);
    const bold = parseInt(cs.fontWeight, 10) >= 700;
    const large = px >= 18.66 || (px >= 14 && bold);
    out.push({
      probe: el.getAttribute('data-probe'),
      text: el.textContent.trim().slice(0, 24),
      fontSize: px,
      large,
      color: 'rgb(' + fg.join(',') + ')',
      background: 'rgb(' + bg.join(',') + ')',
      ratio: Math.round(ratio * 100) / 100,
      threshold: large ? 3 : 4.5,
      pass: ratio >= (large ? 3 : 4.5),
    });
  }
  return out;
}

// 样式表按 glob 取,并且**必须命中唯一一个** —— 第一版把 build hash 写死在
// harness 的 <link> 里,重新 build 之后 hash 变了,页面变成裸 HTML,
// 16 个探针全读出 #000 on #fff 然后打印"未达标项: 0"。那是标准的假绿。
const cssDir = path.join(here, '..', 'dist', 'assets');
const cssFiles = fs.existsSync(cssDir)
  ? fs.readdirSync(cssDir).filter((f) => /^index-.*\.css$/.test(f))
  : [];
if (cssFiles.length !== 1) {
  console.error(
    '找不到唯一的 dist/assets/index-*.css(命中 ' + cssFiles.length + ' 个)。' +
    '请先在 frontend/ 下跑 `npx vite build`:本检查必须量真实构建产物。'
  );
  process.exit(2);
}
const cssPath = path.join(cssDir, cssFiles[0]);
console.log('使用构建产物样式表: dist/assets/' + cssFiles[0]);

const browser = await chromium.launch();
const report = {};
let failures = 0;

for (const theme of ['light', 'dark']) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto('file://' + harness.replace(/\\/g, '/'));
  await page.addStyleTag({ path: cssPath });
  // 反假绿哨兵:样式真生效时 badge 一定有背景色。透明 = 没生效 = 拒绝出分。
  const sentinel = await page.evaluate(() => {
    const el = document.querySelector('[data-probe="badge-approved"]');
    return el ? getComputedStyle(el).backgroundColor : '';
  });
  if (!sentinel || sentinel === 'rgba(0, 0, 0, 0)') {
    console.error('样式表未生效(badge 背景透明) —— 拒绝输出任何"全绿"结论。');
    process.exit(2);
  }
  await page.evaluate((t) => {
    document.documentElement.classList.toggle('dark', t === 'dark');
    document.documentElement.setAttribute('data-theme', t);
  }, theme);
  await page.waitForTimeout(200);
  report[theme] = await page.evaluate(measure);
  failures += report[theme].filter((r) => !r.pass).length;
  if (shotDir) {
    fs.mkdirSync(shotDir, { recursive: true });
    await page.screenshot({ path: path.join(shotDir, 'writing_hall_' + theme + '.png'), fullPage: true });
  }
  await page.close();
}
await browser.close();

for (const theme of ['light', 'dark']) {
  console.log('\n=== ' + theme + ' ===');
  for (const r of report[theme]) {
    console.log(
      (r.pass ? 'PASS' : 'FAIL') + '  ' + r.ratio.toFixed(2).padStart(6) + ':1 (need ' +
      r.threshold + ':1)  ' + r.probe + '  ' + r.color + ' on ' + r.background + '  "' + r.text + '"'
    );
  }
}
console.log('\n未达标项: ' + failures);
if (outFile) fs.writeFileSync(outFile, JSON.stringify(report, null, 2), 'utf-8');
process.exit(failures ? 1 : 0);
