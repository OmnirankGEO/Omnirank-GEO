/**
 * GEO诊断报告 MD → PDF 生成器（Demo）
 * 使用 marked 解析 Markdown + Playwright 渲染 PDF
 */

const fs = require('fs');
const path = require('path');
const { marked } = require('marked');
const { chromium } = require('playwright');

// ====== 报告 HTML 模板 ======
function buildHTML(bodyHTML, meta) {
    return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
/* ===== 基础排版 ===== */
@page {
    size: A4;
    margin: 0;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: "PingFang SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
    font-size: 11px;
    line-height: 1.7;
    color: #1e293b;
    background: #fff;
}

/* ===== 封面 ===== */
.cover {
    height: 100vh;
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 50%, #0c4a6e 100%);
    color: #fff;
    display: flex;
    flex-direction: column;
    justify-content: center;
    padding: 80px 72px;
    position: relative;
    overflow: hidden;
    page-break-after: always;
}
.cover::before {
    content: '';
    position: absolute;
    top: -120px; right: -120px;
    width: 500px; height: 500px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(56,189,248,0.15) 0%, transparent 70%);
}
.cover::after {
    content: '';
    position: absolute;
    bottom: -80px; left: -80px;
    width: 400px; height: 400px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(99,102,241,0.12) 0%, transparent 70%);
}
.cover-brand-label {
    font-size: 13px;
    letter-spacing: 4px;
    text-transform: uppercase;
    color: rgba(255,255,255,0.5);
    margin-bottom: 20px;
}
.cover-brand {
    font-size: 48px;
    font-weight: 800;
    margin-bottom: 16px;
    letter-spacing: 2px;
}
.cover-subtitle {
    font-size: 22px;
    font-weight: 300;
    color: rgba(255,255,255,0.8);
    margin-bottom: 48px;
}
.cover-score-box {
    display: inline-flex;
    align-items: baseline;
    gap: 12px;
    background: rgba(255,255,255,0.08);
    border: 1px solid rgba(255,255,255,0.15);
    border-radius: 16px;
    padding: 24px 40px;
    backdrop-filter: blur(8px);
    margin-bottom: 48px;
}
.cover-score-num {
    font-size: 72px;
    font-weight: 800;
    background: linear-gradient(135deg, #38bdf8, #818cf8);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}
.cover-score-total {
    font-size: 24px;
    color: rgba(255,255,255,0.4);
}
.cover-score-level {
    font-size: 16px;
    color: #fbbf24;
    border: 1px solid rgba(251,191,36,0.3);
    padding: 4px 16px;
    border-radius: 20px;
}
.cover-meta {
    margin-top: auto;
    padding-top: 40px;
    border-top: 1px solid rgba(255,255,255,0.1);
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    color: rgba(255,255,255,0.45);
}
.cover-logo {
    font-size: 16px;
    font-weight: 700;
    color: rgba(255,255,255,0.7);
    letter-spacing: 1px;
}

/* ===== 内容区 ===== */
.content {
    padding: 48px 56px;
}

/* ===== 标题 ===== */
h1 {
    font-size: 24px;
    font-weight: 800;
    color: #0f172a;
    margin: 48px 0 20px;
    padding-bottom: 12px;
    border-bottom: 3px solid #3b82f6;
    page-break-after: avoid;
}
h1:first-child { margin-top: 0; }
h2 {
    font-size: 19px;
    font-weight: 700;
    color: #1e40af;
    margin: 36px 0 14px;
    padding-left: 14px;
    border-left: 4px solid #3b82f6;
    page-break-after: avoid;
}
h3 {
    font-size: 15px;
    font-weight: 700;
    color: #334155;
    margin: 24px 0 10px;
    page-break-after: avoid;
}
h4 {
    font-size: 13px;
    font-weight: 700;
    color: #475569;
    margin: 20px 0 8px;
}

/* ===== 段落 / 列表 ===== */
p { margin: 8px 0; }
ul, ol { margin: 8px 0 8px 24px; }
li { margin: 3px 0; }
strong { color: #0f172a; }
hr {
    border: none;
    height: 1px;
    background: linear-gradient(90deg, #e2e8f0 0%, #cbd5e1 50%, #e2e8f0 100%);
    margin: 32px 0;
}

/* ===== 引用框 ===== */
blockquote {
    background: linear-gradient(135deg, #eff6ff, #f0f9ff);
    border-left: 4px solid #3b82f6;
    border-radius: 0 10px 10px 0;
    padding: 14px 20px;
    margin: 14px 0;
    font-size: 11px;
    color: #334155;
}
blockquote p { margin: 4px 0; }

/* ===== 表格 ===== */
table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    margin: 14px 0;
    font-size: 10.5px;
    border-radius: 8px;
    overflow: hidden;
    border: 1px solid #e2e8f0;
    page-break-inside: avoid;
}
thead {
    background: linear-gradient(135deg, #1e3a5f, #1e40af);
    color: #fff;
}
th {
    padding: 10px 14px;
    text-align: left;
    font-weight: 600;
    font-size: 10.5px;
    letter-spacing: 0.3px;
    white-space: nowrap;
}
td {
    padding: 9px 14px;
    border-top: 1px solid #f1f5f9;
    vertical-align: top;
}
tbody tr:nth-child(even) { background: #f8fafc; }
tbody tr:hover { background: #eff6ff; }

/* 表格中的 emoji 对齐 */
td:first-child, th:first-child { min-width: 80px; }

/* ===== 分数条 ===== */
.score-bar-bg {
    height: 8px;
    background: #e2e8f0;
    border-radius: 4px;
    overflow: hidden;
    margin-top: 4px;
}
.score-bar-fill {
    height: 100%;
    border-radius: 4px;
    background: linear-gradient(90deg, #3b82f6, #6366f1);
}

/* ===== 页脚 ===== */
.page-footer {
    position: fixed;
    bottom: 0;
    left: 0; right: 0;
    height: 36px;
    background: #f8fafc;
    border-top: 1px solid #e2e8f0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 56px;
    font-size: 9px;
    color: #94a3b8;
}

/* ===== 分页控制 ===== */
h1, h2 { page-break-after: avoid; }
table, blockquote { page-break-inside: avoid; }

/* ===== 隐藏报告中的编辑任务头部 ===== */
.content > h1:first-child,
.content > h2:first-of-type,
.content > ul:first-of-type {
    /* 保持显示，不隐藏 */
}
</style>
</head>
<body>

<!-- 封面 -->
<div class="cover">
    <div class="cover-brand-label">GEO 诊断报告</div>
    <div class="cover-brand">${meta.brandName}</div>
    <div class="cover-subtitle">${meta.industry}</div>
    <div class="cover-score-box">
        <span class="cover-score-num">${meta.score}</span>
        <span class="cover-score-total">/100</span>
        <span class="cover-score-level">${meta.level}</span>
    </div>
    <div class="cover-meta">
        <div>
            <div class="cover-logo">OmniRank AI | 全域上榜</div>
            <div style="margin-top:4px">GEO诊断系统 v4.0</div>
        </div>
        <div style="text-align:right">
            <div>诊断日期：${meta.date}</div>
            <div style="margin-top:4px">www.omnirank.cn</div>
        </div>
    </div>
</div>

<!-- 正文 -->
<div class="content">
${bodyHTML}
</div>

<!-- 页脚 -->
<div class="page-footer">
    <span>© 2026 全域上榜（OmniRank）版权所有</span>
    <span>${meta.brandName} | GEO诊断报告</span>
</div>

</body>
</html>`;
}

// ====== 从 MD 内容中提取元信息 ======
function extractMeta(mdContent) {
    const brandMatch = mdContent.match(/品牌名[：:](.+)/);
    const industryMatch = mdContent.match(/行业[：:](.+)/);
    const scoreMatch = mdContent.match(/GEO总分[：:](\d+)/);
    const levelMatch = mdContent.match(/等级[：:](.+)/);
    const dateMatch = mdContent.match(/诊断日期[：:]\**\s*(.+?)[\s|*]/);

    return {
        brandName: brandMatch ? brandMatch[1].trim() : '品牌名称',
        industry: industryMatch ? industryMatch[1].trim() : '',
        score: scoreMatch ? scoreMatch[1] : '0',
        level: levelMatch ? levelMatch[1].trim() : '',
        date: dateMatch ? dateMatch[1].trim() : new Date().toLocaleDateString('zh-CN'),
    };
}

// ====== 主流程 ======
async function main() {
    const mdPath = process.argv[2] || path.join(__dirname, '..', 'output', '深圳美栖堂_upgrade_117_20260315001305.md');
    const outputPath = process.argv[3] || mdPath.replace(/\.md$/, '.pdf');

    console.log(`📄 读取报告: ${mdPath}`);
    const mdContent = fs.readFileSync(mdPath, 'utf-8');

    // 提取元信息
    const meta = extractMeta(mdContent);
    console.log(`🏷️  品牌: ${meta.brandName} | 分数: ${meta.score} | 等级: ${meta.level}`);

    // 跳过 MD 中的"编辑任务"头部，从正式报告开始
    let reportMd = mdContent;
    const reportStart = mdContent.indexOf('# 🎯');
    if (reportStart > 0) {
        reportMd = mdContent.substring(reportStart);
    }

    // MD → HTML
    const bodyHTML = marked.parse(reportMd);

    // 构建完整 HTML
    const fullHTML = buildHTML(bodyHTML, meta);

    // 保存 HTML 用于调试
    const htmlPath = outputPath.replace(/\.pdf$/, '.html');
    fs.writeFileSync(htmlPath, fullHTML, 'utf-8');
    console.log(`🌐 HTML 已保存: ${htmlPath}`);

    // Playwright → PDF
    console.log(`🖨️  正在生成 PDF...`);
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined, args: ['--no-sandbox', '--disable-setuid-sandbox'] });
    const page = await browser.newPage();
    await page.setContent(fullHTML, { waitUntil: 'networkidle' });
    await page.pdf({
        path: outputPath,
        format: 'A4',
        printBackground: true,
        margin: { top: '0', bottom: '0', left: '0', right: '0' },
    });
    await browser.close();

    console.log(`✅ PDF 已生成: ${outputPath}`);
    console.log(`📊 文件大小: ${(fs.statSync(outputPath).size / 1024).toFixed(1)} KB`);
}

main().catch(err => {
    console.error('❌ 生成失败:', err);
    process.exit(1);
});
