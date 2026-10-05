// CTO-B 2026-04-26 W5 · v2 咨询报告 HTML → PDF (light · A4)
// 决策点 3:网页 + PDF 共用同一套 HTML(由 services/report_html_renderer 生成)
//
// 用法:node generate_v2_pdf.js <input_html_path> <output_pdf_path>
//
// 不依赖 LLM 提取 · 不依赖主题选择 · 直接 Playwright print-to-PDF

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

async function main() {
  const [, , inputHtml, outputPdf] = process.argv;
  if (!inputHtml || !outputPdf) {
    console.error('Usage: node generate_v2_pdf.js <input_html> <output_pdf>');
    process.exit(2);
  }
  if (!fs.existsSync(inputHtml)) {
    console.error('Input HTML not found:', inputHtml);
    process.exit(2);
  }

  const html = fs.readFileSync(inputHtml, 'utf-8');
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.setContent(html, { waitUntil: 'networkidle' });
    // [2026-07-22 sink census B12] Chromium print 不排版闭合 <details> 的内容
    // (即使 CSS 把子元素 display:block)。PDF 是隔离导出文档,展开不影响网页报告。
    await page.locator('details').evaluateAll((nodes) => {
      nodes.forEach((node) => { node.open = true; });
    });
    // 给字体/任何脚本一点时间
    await page.waitForTimeout(300);

    await page.pdf({
      path: outputPdf,
      format: 'A4',
      margin: { top: '14mm', bottom: '14mm', left: '14mm', right: '14mm' },
      printBackground: true,
      preferCSSPageSize: false,
    });

    console.log('PDF generated:', outputPdf, 'size:', fs.statSync(outputPdf).size);
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error('PDF generation failed:', err);
  process.exit(1);
});
