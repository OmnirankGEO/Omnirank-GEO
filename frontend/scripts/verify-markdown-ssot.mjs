/**
 * verify-markdown-ssot.mjs — 全站 Markdown 渲染 SSOT 构建门禁
 *
 * 口径：docs/AI-CONTEXT/MARKDOWN_SINK_CENSUS_2026-07-22（sink-based census）。
 * 任一违规即 exit 1：
 *  - components/SafeMarkdown.tsx 之外 import react-markdown（deferred 名单除外，
 *    每条带 owner 板块标注；板块 A/C 收尾后必须移除对应条目）
 *  - SafeMarkdown/deferred 之外 import remark-gfm（SSOT 内集中引用；
 *    patches/mdast-util-gfm-autolink-literal 已去 lookbehind，禁止二次扩散引用）
 *  - census 外新增 SafeMarkdown 直接出口 / wrapper 出口
 *  - 已登记出口被悄悄删除（防 census 漂移）
 *  - 关键 Markdown DTO 表达式脱离 SafeMarkdown JSX 边界
 *  - dangerouslySetInnerHTML 超出 trusted 白名单
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import ts from 'typescript'

const root = path.resolve(process.cwd(), 'src')

// SafeMarkdown 直接出口（census §2.A/2.B 已逐点判定）
// [开源 E3 · 前端 · 2026-10-01 · WO_322] 出口所在的整块宿主删除(社媒工作台 / 旧社媒操盘手 / M3 / 旧 agent 对话 UI / 顾问对话页 / 顾问管理页),
//   三张表同删 19 条(直接出口 11 · 包装出口 3 · DTO 表达式 5);逐条清单见交付单 E3F_FRONTEND_DELIVERY。
const directSurfaces = new Set([
  'components/TaskExecutionBoard.tsx',
  'components/xiaobang/XiaobangMessageList.tsx',
  'features/publicReportPremium/components/markdown/MarkdownView.tsx',
  'pages/Admin/AiOpsCenter/components/shared.tsx',
  'pages/Admin/HelpCenterAdmin/FAQItemEditPage.tsx',
  'pages/Admin/HelpCenterAdmin/FAQFeedbackTab.tsx',
  'pages/Admin/MarketingAdvisor/components/Effect.tsx',
  'pages/Admin/ResearchMonitor/ArticlesPanel.tsx',
  'pages/Admin/ResearchMonitor/CitationsPanel.tsx',
  'pages/Agent/AgreementPage.tsx',
  'pages/Employees/EmployeeHall.tsx',
  'pages/Employees/MeetingHistory.tsx',
  'pages/Employees/TaskBoard.tsx',
  'pages/Feedback/FeedbackPage.tsx',
  'pages/Help/HelpDocs.tsx',
  'pages/Help/HelpFAQ.tsx',
  'pages/Monitoring/components/IdentityReviewPanel.tsx',
  'pages/Monitoring/components/ProgressPanel.tsx',
  'pages/Partner/AgreementViewer.tsx',
  'pages/Partner/PartnerApplyStep3.tsx',
  'pages/Portal/PortalDashboard.tsx',
  'pages/Public/SharedReport.tsx',
  'pages/Reports/index.tsx',
  'pages/Workspace/Workspace.tsx',
  'pages/Writing/ImitatedArticleDetail.tsx',
  'pages/Writing/ReferenceLibrary.tsx',
  'pages/Writing/WritingCenter.tsx',
  'pages/Writing/WritingHall.tsx',
  // [2026-07-22 集成收尾] 原 deferred 三文件已走 SSOT（板块A 两诊断页 + 板块C MeetingRoom）
  'pages/Diagnosis/DiagnosisReport.tsx',
  'pages/Diagnosis/ReportV2View.tsx',
  'pages/Employees/MeetingRoom.tsx',
])

// wrapper 出口（经 MarkdownView / MarkdownBlock 间接受控;社媒工作台那个包装件随 E3 删 [开源 E3 · 前端 · 2026-10-01 · WO_322]）
const wrappedSurfaces = new Set([
  'features/publicReportPremium/components/sections/EvidenceMatrix.tsx',
  'features/publicReportPremium/components/sections/Narrative.tsx',
  'features/publicReportPremium/components/sections/PlatformPerformance.tsx',
  'features/publicReportPremium/components/sections/PriorityActions.tsx',
  'pages/Admin/AiOpsCenter/components/AiCommandConsole.tsx',
  'pages/Admin/AiOpsCenter/components/ReportViewer.tsx',
  'pages/Admin/AiOpsCenter/components/TaskDetailDrawer.tsx',
])

// 延期集成名单：允许暂时直用 react-markdown/remark-gfm，收尾后必须移除。
// [2026-07-22 集成收尾] 板块A 两诊断页 + 板块C MeetingRoom 已全部改走 SSOT
// 并移入 directSurfaces，名单清空（保留机制供未来延期使用）。
const deferredReactMarkdownSurfaces = new Map([
])

const knownDtoExpressions = new Map([
  ['pages/Public/SharedReport.tsx', ['report.content']],
  ['pages/Portal/PortalDashboard.tsx', ['detail.snippet', 'selectedReport.content']],
  ['pages/Reports/index.tsx', ['viewReport.content']],
  // [工单 C-2 T1 2026-07-27] 预览回退改为剥占位符兜底(仍在同一个 SafeMarkdown 出口内):
  // contentRendered ?? stripInternalPlaceholders(content) —— census 同步钉新表达式,
  // 防线语义不变:该 DTO 表达式脱离 SafeMarkdown JSX 边界即 build 失败。
  ['pages/Writing/WritingHall.tsx', ['previewData.contentRendered ?? stripInternalPlaceholders(previewData.content)']],
  ['pages/Writing/WritingCenter.tsx', ['previewArticle.content']],
  ['pages/Writing/ImitatedArticleDetail.tsx', ['article.content']],
  ['pages/Admin/MarketingAdvisor/components/Effect.tsx', ['reviewMd || weeklyMd']],
  ['pages/Admin/ResearchMonitor/ArticlesPanel.tsx', ['preprocessMarkdownForChinese(d.cleaned_content)']],
  ['pages/Admin/ResearchMonitor/CitationsPanel.tsx', ["preprocessMarkdownForChinese(answer.answer_text || '*(空回答)*')"]],
  ['pages/Monitoring/components/IdentityReviewPanel.tsx', ['item.identity_evidence_snippet || item.response_snippet']],
  ['pages/Monitoring/components/ProgressPanel.tsx', ['log.fullResponse']],
  ['pages/Feedback/FeedbackPage.tsx', ['aiAnswer']],
  ['pages/Admin/HelpCenterAdmin/FAQFeedbackTab.tsx', ['fb.ai_answer']],
])

// 非 Markdown 的 dInnerHTML 边界（census §2.C 已逐点登记理由）
const trustedInnerHtml = new Set([
  'components/publishing/AwaitingConfirmDialog.tsx', // escapeHtml 后受控 <mark> 高亮
  'pages/Login/LoginPage.tsx', // 同源认证接口 CAPTCHA SVG
  'pages/Public/SharedReport.tsx', // 后端报告整档(markdown 片段已过服务端 SSOT)
])

function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) return walk(full)
    return /\.tsx?$/.test(entry.name) ? [full] : []
  })
}

const errors = []
const seenDirect = new Set()
const seenWrapped = new Set()
const actualInnerHtml = new Set()

for (const file of walk(root)) {
  const rel = path.relative(root, file).split(path.sep).join('/')
  const source = fs.readFileSync(file, 'utf8')
  const ast = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const safeAliases = new Set()
  let importsWrapper = false

  for (const statement of ast.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)) continue
    const moduleName = statement.moduleSpecifier.text
    if (moduleName === 'react-markdown' && rel !== 'components/SafeMarkdown.tsx' && !deferredReactMarkdownSurfaces.has(rel)) {
      errors.push(`${rel}: imports react-markdown outside the SSOT (deferred? 需在 census 登记 owner)`)
    }
    if (moduleName === 'remark-gfm' && rel !== 'components/SafeMarkdown.tsx' && !deferredReactMarkdownSurfaces.has(rel)) {
      errors.push(`${rel}: imports remark-gfm outside the SSOT(只允许 SafeMarkdown 集中引用)`)
    }
    if (moduleName === '@/components/SafeMarkdown') {
      const alias = statement.importClause?.name?.text
      if (alias) safeAliases.add(alias)
      seenDirect.add(rel)
    }
    const namedImports = statement.importClause?.namedBindings
    const importsMarkdownBlock = moduleName === './shared'
      && namedImports && ts.isNamedImports(namedImports)
      && namedImports.elements.some((element) => element.name.text === 'MarkdownBlock')
    if (moduleName.endsWith('/markdown/MarkdownView') || importsMarkdownBlock) {
      importsWrapper = true
      seenWrapped.add(rel)
    }
  }

  const safeBlocks = []
  function visit(node) {
    if (ts.isJsxAttribute(node) && node.name.getText(ast) === 'dangerouslySetInnerHTML') {
      actualInnerHtml.add(rel)
    }
    if (ts.isJsxElement(node) && safeAliases.has(node.openingElement.tagName.getText(ast))) {
      safeBlocks.push(node.getText(ast))
    }
    ts.forEachChild(node, visit)
  }
  visit(ast)

  if (safeAliases.size && !directSurfaces.has(rel)) errors.push(`${rel}: new SafeMarkdown exit is missing from the census`)
  if (importsWrapper && !wrappedSurfaces.has(rel)) errors.push(`${rel}: new wrapped Markdown exit is missing from the census`)

  for (const expression of knownDtoExpressions.get(rel) || []) {
    if (!safeBlocks.some((block) => block.includes(expression))) {
      errors.push(`${rel}: known Markdown DTO expression is no longer inside SafeMarkdown: ${expression}`)
    }
  }
}

for (const rel of directSurfaces) {
  if (!seenDirect.has(rel)) errors.push(`${rel}: census says direct SSOT, but its import is missing`)
}
for (const rel of wrappedSurfaces) {
  if (!seenWrapped.has(rel)) errors.push(`${rel}: census says wrapped SSOT, but its wrapper import is missing`)
}
for (const rel of actualInnerHtml) {
  if (!trustedInnerHtml.has(rel)) errors.push(`${rel}: undocumented dangerouslySetInnerHTML boundary`)
}
for (const rel of trustedInnerHtml) {
  if (!actualInnerHtml.has(rel)) errors.push(`${rel}: stale dangerouslySetInnerHTML allow-list entry`)
}

// deferred 名单防漂移：文件若已不再引用 react-markdown，必须同步移出名单
for (const rel of deferredReactMarkdownSurfaces.keys()) {
  const file = path.join(root, rel)
  if (!fs.existsSync(file)) {
    errors.push(`${rel}: deferred entry targets a missing file`)
    continue
  }
  const text = fs.readFileSync(file, 'utf8')
  if (!/from\s+['"]react-markdown['"]/.test(text)) {
    errors.push(`${rel}: deferred surface no longer imports react-markdown; remove it from the deferred list`)
  }
}

if (errors.length) {
  console.error('Markdown SSOT census failed:\n' + errors.map((line) => `- ${line}`).join('\n'))
  process.exit(1)
}

console.log(
  `Markdown SSOT census passed: ${seenDirect.size} direct, ${seenWrapped.size} wrapped, `
  + `${deferredReactMarkdownSurfaces.size} deferred.`,
)
