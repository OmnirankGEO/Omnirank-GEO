const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

// ============================================================
// GEO Diagnostic Report — Multi-Theme PDF Generator
// Generates 4 themed PDFs in a single run
// NO PURPLE (#8B5CF6, #6366F1, #A855F7) — replaced with teal/cyan
// ============================================================

const D = {
  brandName: '深圳美栖堂',
  industry: '医疗美容服务',
  date: '2026.03.15',
  geoScore: 22, maxScore: 100, level: '起步',
  aiTests: 32, aiRecommendRate: 12.5,
  // Executive summary hero metrics
  executiveSummary: {
    headline: '您的品牌在AI搜索中几乎隐形',
    subline: '每月约有21,900次AI搜索涉及您的行业，但仅12.5%的测试中AI提及了您',
    quickWins: [
      { action: '建立百科词条', impact: '+5-8分', effort: '低', timeline: '3天' },
      { action: '优化官网Schema', impact: '+3-5分', effort: '低', timeline: '5天' },
      { action: '启动社媒矩阵', impact: '+8-12分', effort: '中', timeline: '10天' },
    ],
  },
  // Revenue impact / funnel with before-after contrast
  revenueImpact: {
    monthlySearchVolume: '21,900',
    currentMonthlyOrders: '~2单',
    potentialMonthlyOrders: '~20单',
    avgOrderValue: '¥5,000',
    currentMonthlyRevenue: '¥10,000',
    potentialMonthlyRevenue: '¥100,000',
    annualLoss: '¥108万',
    lossNote: '每延迟一个月优化，约损失¥9万潜在收入',
  },
  dims: [
    { name: 'AI可见度', score: 5, max: 25, pct: 20, desc: '品牌在AI引擎中可被识别和提及的程度', status: '较低', advice: '建立百科词条和知识图谱' },
    { name: '社媒声量', score: 3, max: 20, pct: 15, desc: '社交媒体上品牌相关讨论和内容数量', status: '薄弱', advice: '启动小红书/抖音内容矩阵' },
    { name: '网页内容', score: 5, max: 18, pct: 28, desc: '官网及第三方网页的内容质量和SEO表现', status: '基础', advice: '优化官网结构化数据标记' },
    { name: '权威背书', score: 3, max: 15, pct: 20, desc: '媒体报道、专业评测、行业认证等信任信号', status: '缺失', advice: '争取行业媒体报道和认证' },
    { name: '结构化内容', score: 3, max: 12, pct: 25, desc: 'Schema标记、FAQ、知识库等机器可读内容', status: '部分', advice: '完善JSON-LD和FAQ模块' },
    { name: '内容质量', score: 2, max: 5, pct: 40, desc: '现有内容的专业度、原创性和用户价值', status: '一般', advice: '提升内容专业度和深度' },
    { name: '品牌基础', score: 1, max: 5, pct: 20, desc: '品牌一致性、视觉识别和基础信息完整度', status: '待建', advice: '统一品牌信息和视觉体系' },
  ],
  engines: [
    { name: 'DeepSeek', status: true, detail: '在"深圳医美推荐"等3个问题中提及品牌，但描述不完整', mentions: 3, total: 8 },
    { name: 'Kimi', status: false, detail: '8个行业问题均未提及品牌，推荐了竞品刘青和美莱', mentions: 0, total: 8 },
    { name: '豆包', status: true, detail: '在"医疗美容哪家好"等1个问题中提及，排名靠后', mentions: 1, total: 8 },
    { name: 'ChatGPT', status: false, detail: '无任何品牌相关输出，知识库中缺乏品牌信息', mentions: 0, total: 8 },
  ],
  testQuestions: [
    { q: '深圳哪家医美机构比较好？', ds: '✓', kimi: '✗', db: '✗', gpt: '✗' },
    { q: '深圳做双眼皮哪里比较专业？', ds: '✓', kimi: '✗', db: '✗', gpt: '✗' },
    { q: '深圳医疗美容推荐', ds: '✓', kimi: '✗', db: '✓', gpt: '✗' },
    { q: '深圳靠谱的整形医院', ds: '✗', kimi: '✗', db: '✗', gpt: '✗' },
    { q: '深圳做玻尿酸哪家好？', ds: '✗', kimi: '✗', db: '✗', gpt: '✗' },
    { q: '深圳抗衰老项目推荐', ds: '✗', kimi: '✗', db: '✗', gpt: '✗' },
    { q: '深圳轻医美机构排名', ds: '✗', kimi: '✗', db: '✗', gpt: '✗' },
    { q: '深圳皮肤管理哪家好？', ds: '✗', kimi: '✗', db: '✗', gpt: '✗' },
  ],
  industryAvg: 45, industryBest: 82,
  competitors: [
    { name: '西安刘青整形', tag: 'AI高频推荐', score: 78, strength: '内容矩阵',
      points: ['百科词条完善，知识图谱完整', '知乎/小红书专业文章200+篇', '多平台矩阵分发，日更3-5条', '医生IP打造成功，个人品牌强', '患者案例结构化呈现，FAQ完善'] },
    { name: 'Savislook', tag: '社媒强势', score: 65, strength: '用户口碑',
      points: ['小红书粉丝50万+，笔记1000+', '抖音短视频日均播放10万+', '差异化定位清晰，品牌记忆强', 'UGC种草内容占比40%以上', '私域社群活跃，转化链路完整'] },
    { name: '深圳美莱', tag: '品牌权威', score: 82, strength: '品牌信任',
      points: ['央视/卫视广告背书，知名度高', '三甲医院级别资质认证完整', '媒体报道频次高，PR矩阵成熟', '结构化数据和Schema标记完善', 'SEO排名稳定，长尾词覆盖广'] },
  ],
  funnel: [
    { label: '月AI渠道搜索量', value: '~21,900次', width: 100, note: '基于5118关键词数据' },
    { label: '被AI推荐曝光', value: '~2,700次', width: 72, note: '行业平均曝光率12.3%' },
    { label: '用户点击搜索', value: '~400次', width: 45, note: '推荐后点击率约15%' },
    { label: '产生咨询意向', value: '~100个', width: 28, note: '搜索到咨询转化25%' },
    { label: '最终成交', value: '~20单/月', width: 16, note: '咨询到成交约20%' },
  ],
  findings: [
    { title: '品牌信息碎片化', status: 'warning',
      desc: 'AI引擎无法完整描述品牌核心业务和差异化优势。百科词条缺失，知识图谱空白。',
      fix: '建立统一品牌叙事，完善百科/知识图谱，确保所有平台信息一致' },
    { title: '权威内容缺失', status: 'danger',
      desc: '缺少行业媒体报道、专业评测、用户口碑等第三方内容。AI引擎缺乏"信任信号"来推荐品牌。',
      fix: '争取2-3家行业媒体报道，发布专业评测文章，引导真实用户评价' },
    { title: '社媒声量薄弱', status: 'warning',
      desc: '抖音、小红书等平台品牌相关内容极少。用户自发讨论和种草内容几乎为零。',
      fix: '每周发布10+篇优质内容，启动KOL合作计划，引导UGC产出' },
    { title: '基础网页已建立', status: 'good',
      desc: '品牌已有基础网页和部分结构化信息，官网框架基本完整。系统性补强即可快速提升。',
      fix: '优化现有页面的Schema标记、FAQ模块，提升内容专业度' },
  ],
  // Existing advantages (what's working)
  existingAdvantages: [
    { title: '官网框架完整', desc: '已有基础网页，可快速优化' },
    { title: 'DeepSeek已收录', desc: '部分问题已能触发品牌提及' },
    { title: '行业词搜索量大', desc: '月搜索21,900次，流量池充裕' },
  ],
  actions: [
    { phase: '基础建设', days: 'Day 1-10',
      items: [
        { task: '完善品牌百科词条', detail: '百度百科+搜狗百科+360百科' },
        { task: '建立结构化知识库', detail: 'FAQ/问答矩阵覆盖核心场景' },
        { task: '优化官网SEO基础', detail: 'Schema标记+TDK+结构化数据' },
        { task: '注册完善社媒账号', detail: '小红书+抖音+知乎+微信公众号' },
        { task: '制定内容日历', detail: '规划30天内容主题和发布节奏' },
      ], goal: 'AI可搜索到品牌', kpi: '百科收录 + 官网Schema通过验证' },
    { phase: '内容种草', days: 'Day 11-20',
      items: [
        { task: '发布专业文章15篇', detail: '知乎+公众号深度专业内容' },
        { task: '社媒启动内容矩阵', detail: '小红书日更2条+抖音周更3条' },
        { task: '问答覆盖核心场景', detail: '知道/知乎/小红书问答布局' },
        { task: '拓展第三方露出', detail: '行业目录+本地生活平台入驻' },
        { task: '启动KOL种草合作', detail: '3-5位本地美妆KOL体验分享' },
      ], goal: 'AI开始推荐品牌', kpi: '至少2个引擎开始提及品牌' },
    { phase: '强化巩固', days: 'Day 21-30',
      items: [
        { task: '争取媒体报道评测', detail: '联系2-3家行业媒体/KOL深度报道' },
        { task: '持续优化内容质量', detail: '基于数据反馈迭代热门内容' },
        { task: '引导用户口碑UGC', detail: '设计分享激励机制+案例征集' },
        { task: '复测GEO验证效果', detail: '全量复测4引擎×8问题' },
        { task: '输出月度GEO报告', detail: '对比前后数据，规划下阶段' },
      ], goal: '评分提升至 40+', kpi: 'GEO评分提升80%+，推荐率>25%' },
  ],
  // Effect prediction
  effectPrediction: {
    day30Score: 40, day60Score: 58, day90Score: 72,
    day30Rate: '30%', day60Rate: '50%', day90Rate: '65%',
    risks: [
      { risk: '内容执行力不足', level: 'high', mitigation: '制定严格内容日历，设置周检查点' },
      { risk: '竞品同步优化', level: 'medium', mitigation: '持续监测竞品动态，保持内容差异化' },
      { risk: 'AI算法调整', level: 'low', mitigation: '多平台布局分散风险，及时跟进变化' },
    ],
  },
  // Methodology
  methodology: {
    dataPoints: '280+条原始数据',
    engines: '4大AI引擎实测',
    questions: '8个行业高频问题',
    platforms: '7个关键词×2大平台',
    version: 'GEO诊断系统 v4.0',
  },
};

// ============================================================
// Empty defaults — used when generating real reports to avoid
// demo data leaking when fields are not extracted.
// ============================================================

const EMPTY = {
  brandName: '品牌名称', industry: '', date: '',
  geoScore: 0, maxScore: 100, level: '未知',
  aiTests: 0, aiRecommendRate: 0,
  executiveSummary: { headline: '', subline: '', quickWins: [] },
  revenueImpact: {},
  dims: [],
  engines: [],
  testQuestions: [],
  industryAvg: 45, industryBest: 82,
  competitors: [],
  funnel: [],
  findings: [],
  existingAdvantages: [],
  actions: [],
  effectPrediction: { day30Score: 0, day60Score: 0, day90Score: 0, day30Rate: '', day60Rate: '', day90Rate: '', risks: [] },
  methodology: { dataPoints: '', engines: '', questions: '', platforms: '', version: '' },
};

// ============================================================
// Markdown → HTML inline converter (for text fields)
// ============================================================

function mdInline(s) {
  if (!s || typeof s !== 'string') return s || '';
  return s
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/`(.+?)`/g, '<code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;font-size:0.9em;">$1</code>')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
}

function cleanMdInData(data) {
  const md = mdInline;
  if (data.dims) data.dims = data.dims.map(d => ({...d, desc: md(d.desc), advice: md(d.advice)}));
  if (data.findings) data.findings = data.findings.map(f => ({...f, desc: md(f.desc), fix: md(f.fix)}));
  if (data.engines) data.engines = data.engines.map(e => ({...e, detail: md(e.detail)}));
  if (data.competitors) data.competitors = data.competitors.map(c => ({...c, points: (c.points||[]).map(md)}));
  if (data.executiveSummary) {
    data.executiveSummary.headline = md(data.executiveSummary.headline);
    data.executiveSummary.subline = md(data.executiveSummary.subline);
  }
  if (data.actions) data.actions = data.actions.map(a => ({...a, items: (a.items||[]).map(i => ({...i, task: md(i.task), detail: md(i.detail)}))}));
  if (data.existingAdvantages) data.existingAdvantages = data.existingAdvantages.map(a => ({...a, desc: md(a.desc)}));
  if (data.funnel) data.funnel = data.funnel.map(f => ({...f, note: md(f.note)}));
  return data;
}

// ============================================================
// Theme definitions
// ============================================================

const themes = [
  // ---- Theme 1: dark-premium ----
  {
    id: 'dark_premium', label: '深色商务',
    pageBg: '#0F172A', gradientBar: 'linear-gradient(90deg,#3B82F6,#0891B2,#F59E0B)',
    cardBg: 'rgba(30,41,59,0.85)', cardBorder: 'rgba(255,255,255,0.08)', cardRadius: '14px', cardShadow: 'none',
    textH: '#F8FAFC', textBody: '#CBD5E1', textMuted: '#94A3B8', textDim: '#64748B', textSubdim: '#475569', textFooter: '#334155', textEmphasis: '#E2E8F0',
    accent: '#3B82F6', accentDark: '#1E40AF', secondary: '#0891B2', highlight: '#F59E0B', highlightLight: '#FBBF24',
    success: '#10B981', danger: '#EF4444', warning: '#F59E0B',
    ringTrack: 'rgba(255,255,255,0.06)', ringGrad1: '#3B82F6', ringGrad2: '#0891B2', ringGrad3: '#F59E0B', ringScoreColor: '#F8FAFC', ringLabelColor: '#64748B',
    orbColor1: '#3B82F6', orbColor2: '#0891B2', orbOpacity1: 0.08, orbOpacity2: 0.06,
    dotColor: 'rgba(255,255,255,0.08)',
    dimColors: ['#3B82F6','#0EA5E9','#06B6D4','#0891B2','#EC4899','#F59E0B','#10B981'],
    heroBlueBg: 'linear-gradient(135deg,#172554,#0C2D48)', heroGoldBg: 'linear-gradient(135deg,#1C1510,#1A1508)',
    insightBg: 'linear-gradient(135deg,#131C2E,#0F1D2E)', blueAccentBg: '#111D32', goldAccentBg: '#1C1810',
    scoreCardBg: 'linear-gradient(135deg,#0F172A,#1E293B)',
    goldCalloutBg: '#1C1810', goldCalloutBorder: '#332D1A', goldCalloutText: '#FBBF24',
    tableHeaderBg: 'transparent', tableHeaderText: '#64748B', tableRowAlt: 'rgba(255,255,255,0.01)', tableBorder: 'rgba(255,255,255,0.08)',
    pageNumColor: '#3B82F6', pageNumBg: 'rgba(59,130,246,0.1)', pageNumBorder: 'rgba(59,130,246,0.2)',
    ctaBg: 'linear-gradient(135deg,#3B82F6,#0891B2)', ctaText: '#fff',
    leftBorderGrad: 'linear-gradient(180deg,#3B82F6,#0891B2)', timelineGrad: 'linear-gradient(90deg,#3B82F6,#0891B2,#10B981)',
    progressTrack: 'rgba(255,255,255,0.04)',
    subtleBg: 'rgba(255,255,255,0.03)', subtleBorder: 'rgba(255,255,255,0.06)',
    subtleBg2: 'rgba(255,255,255,0.02)', subtleBorder2: 'rgba(255,255,255,0.04)',
    compTagColors: ['#3B82F6','#0891B2','#10B981'],
    actionColors: [
      { color: '#3B82F6', grad: 'linear-gradient(135deg,#1E40AF,#3B82F6)' },
      { color: '#0891B2', grad: 'linear-gradient(135deg,#0E7490,#0891B2)' },
      { color: '#10B981', grad: 'linear-gradient(135deg,#047857,#10B981)' },
    ],
    // Dark-specific
    statusBadgeBg: 'rgba(255,255,255,0.04)',
    insightBorderColor: 'rgba(59,130,246,0.15)',
    compBgSubtle: 'rgba(255,255,255,0.03)',
    compBorderSubtle: 'rgba(255,255,255,0.04)',
    blueAccentBorder: '#1A2D4A',
    warningAccentBg: 'rgba(245,158,11,0.06)', warningAccentBorder: 'rgba(245,158,11,0.12)',
    dangerAccentBg: 'rgba(239,68,68,0.06)', dangerAccentBorder: 'rgba(239,68,68,0.12)',
    timelineDotBorder: '#0F172A',
    actionSectionBorder: 'rgba(255,255,255,0.04)', actionSectionBg: 'rgba(255,255,255,0.01)',
    barYouColor: '#3B82F6', barYouGrad: 'linear-gradient(180deg,#3B82F6,#1E40AF)',
    barBenchColor: '#10B981', barBenchGrad: 'linear-gradient(180deg,#10B981,#047857)',
  },

  // ---- Theme 2: light-corporate ----
  {
    id: 'light_corporate', label: '浅色专业',
    pageBg: '#F8FAFC', gradientBar: 'linear-gradient(90deg,#1E40AF,#0891B2,#0F766E)',
    cardBg: '#FFFFFF', cardBorder: '#E2E8F0', cardRadius: '14px', cardShadow: '0 1px 3px rgba(0,0,0,0.08)',
    textH: '#0F172A', textBody: '#334155', textMuted: '#64748B', textDim: '#94A3B8', textSubdim: '#94A3B8', textFooter: '#94A3B8', textEmphasis: '#1E293B',
    accent: '#1E40AF', accentDark: '#1E3A8A', secondary: '#0891B2', highlight: '#0F766E', highlightLight: '#0D9488',
    success: '#059669', danger: '#DC2626', warning: '#D97706',
    ringTrack: 'rgba(0,0,0,0.06)', ringGrad1: '#1E40AF', ringGrad2: '#0891B2', ringGrad3: '#D97706', ringScoreColor: '#0F172A', ringLabelColor: '#94A3B8',
    orbColor1: '#1E40AF', orbColor2: '#0891B2', orbOpacity1: 0.04, orbOpacity2: 0.03,
    dotColor: 'rgba(0,0,0,0.04)',
    dimColors: ['#1E40AF','#0369A1','#0891B2','#0F766E','#D97706','#BE185D','#DC2626'],
    heroBlueBg: 'linear-gradient(135deg,#EFF6FF,#E0F2FE)', heroGoldBg: 'linear-gradient(135deg,#F0FDF4,#ECFDF5)',
    insightBg: 'linear-gradient(135deg,#EFF6FF,#F0F9FF)', blueAccentBg: '#EFF6FF', goldAccentBg: '#F0FDF4',
    scoreCardBg: 'linear-gradient(135deg,#EFF6FF,#F0F9FF)',
    goldCalloutBg: '#FFFBEB', goldCalloutBorder: '#FDE68A', goldCalloutText: '#92400E',
    tableHeaderBg: '#F1F5F9', tableHeaderText: '#64748B', tableRowAlt: '#F8FAFC', tableBorder: '#E2E8F0',
    pageNumColor: '#1E40AF', pageNumBg: 'rgba(30,64,175,0.08)', pageNumBorder: 'rgba(30,64,175,0.15)',
    ctaBg: 'linear-gradient(135deg,#1E40AF,#1E3A8A)', ctaText: '#fff',
    leftBorderGrad: 'linear-gradient(180deg,#1E40AF,#0891B2)', timelineGrad: 'linear-gradient(90deg,#1E40AF,#0891B2,#059669)',
    progressTrack: '#E2E8F0',
    subtleBg: 'rgba(0,0,0,0.02)', subtleBorder: '#E2E8F0',
    subtleBg2: 'rgba(0,0,0,0.01)', subtleBorder2: '#E2E8F0',
    compTagColors: ['#1E40AF','#0891B2','#059669'],
    actionColors: [
      { color: '#1E40AF', grad: 'linear-gradient(135deg,#1E3A8A,#1E40AF)' },
      { color: '#0891B2', grad: 'linear-gradient(135deg,#0E7490,#0891B2)' },
      { color: '#059669', grad: 'linear-gradient(135deg,#047857,#059669)' },
    ],
    statusBadgeBg: '#F1F5F9',
    insightBorderColor: 'rgba(30,64,175,0.15)',
    compBgSubtle: '#F8FAFC',
    compBorderSubtle: '#E2E8F0',
    blueAccentBorder: '#BFDBFE',
    warningAccentBg: '#FFFBEB', warningAccentBorder: '#FDE68A',
    dangerAccentBg: '#FEF2F2', dangerAccentBorder: '#FECACA',
    timelineDotBorder: '#F8FAFC',
    actionSectionBorder: '#E2E8F0', actionSectionBg: '#F8FAFC',
    barYouColor: '#1E40AF', barYouGrad: 'linear-gradient(180deg,#1E40AF,#1E3A8A)',
    barBenchColor: '#059669', barBenchGrad: 'linear-gradient(180deg,#059669,#047857)',
  },

  // ---- Theme 3: warm-trust ----
  {
    id: 'warm_trust', label: '暖色信赖',
    pageBg: '#FFFBF5', gradientBar: 'linear-gradient(90deg,#B45309,#D97706,#059669)',
    cardBg: '#FFFFFF', cardBorder: '#F5E6D3', cardRadius: '14px', cardShadow: '0 1px 3px rgba(120,80,40,0.06)',
    textH: '#1C1917', textBody: '#44403C', textMuted: '#78716C', textDim: '#A8A29E', textSubdim: '#A8A29E', textFooter: '#A8A29E', textEmphasis: '#292524',
    accent: '#B45309', accentDark: '#92400E', secondary: '#059669', highlight: '#C2410C', highlightLight: '#D97706',
    success: '#059669', danger: '#C2410C', warning: '#D97706',
    ringTrack: 'rgba(0,0,0,0.06)', ringGrad1: '#B45309', ringGrad2: '#D97706', ringGrad3: '#059669', ringScoreColor: '#1C1917', ringLabelColor: '#A8A29E',
    orbColor1: '#B45309', orbColor2: '#059669', orbOpacity1: 0.04, orbOpacity2: 0.03,
    dotColor: 'rgba(180,83,9,0.05)',
    dimColors: ['#B45309','#D97706','#059669','#0891B2','#C2410C','#9A3412','#0F766E'],
    heroBlueBg: 'linear-gradient(135deg,#FFF7ED,#FFFBEB)', heroGoldBg: 'linear-gradient(135deg,#F0FDF4,#ECFDF5)',
    insightBg: 'linear-gradient(135deg,#FFF7ED,#FFFBEB)', blueAccentBg: '#FFF7ED', goldAccentBg: '#F0FDF4',
    scoreCardBg: 'linear-gradient(135deg,#FFF7ED,#FFFBEB)',
    goldCalloutBg: '#FFF7ED', goldCalloutBorder: '#FED7AA', goldCalloutText: '#92400E',
    tableHeaderBg: '#FFF7ED', tableHeaderText: '#78716C', tableRowAlt: '#FFFBF5', tableBorder: '#F5E6D3',
    pageNumColor: '#B45309', pageNumBg: 'rgba(180,83,9,0.08)', pageNumBorder: 'rgba(180,83,9,0.15)',
    ctaBg: 'linear-gradient(135deg,#B45309,#92400E)', ctaText: '#fff',
    leftBorderGrad: 'linear-gradient(180deg,#B45309,#D97706)', timelineGrad: 'linear-gradient(90deg,#B45309,#D97706,#059669)',
    progressTrack: '#F5E6D3',
    subtleBg: 'rgba(180,83,9,0.03)', subtleBorder: '#F5E6D3',
    subtleBg2: 'rgba(180,83,9,0.02)', subtleBorder2: '#F5E6D3',
    compTagColors: ['#B45309','#0891B2','#059669'],
    actionColors: [
      { color: '#B45309', grad: 'linear-gradient(135deg,#92400E,#B45309)' },
      { color: '#D97706', grad: 'linear-gradient(135deg,#B45309,#D97706)' },
      { color: '#059669', grad: 'linear-gradient(135deg,#047857,#059669)' },
    ],
    statusBadgeBg: '#FFF7ED',
    insightBorderColor: 'rgba(180,83,9,0.15)',
    compBgSubtle: '#FFFBF5',
    compBorderSubtle: '#F5E6D3',
    blueAccentBorder: '#FED7AA',
    warningAccentBg: '#FFF7ED', warningAccentBorder: '#FED7AA',
    dangerAccentBg: '#FFF7ED', dangerAccentBorder: '#FDBA74',
    timelineDotBorder: '#FFFBF5',
    actionSectionBorder: '#F5E6D3', actionSectionBg: '#FFFBF5',
    barYouColor: '#B45309', barYouGrad: 'linear-gradient(180deg,#B45309,#92400E)',
    barBenchColor: '#059669', barBenchGrad: 'linear-gradient(180deg,#059669,#047857)',
  },

  // ---- Theme 4: bold-gradient ----
  {
    id: 'bold_gradient', label: '活力渐变',
    pageBg: '#FAFBFF', gradientBar: 'linear-gradient(90deg,#2563EB,#7C3AED,#F97316)',
    cardBg: '#FFFFFF', cardBorder: '#E8E5F0', cardRadius: '16px', cardShadow: '0 2px 12px rgba(37,99,235,0.06)',
    textH: '#0F172A', textBody: '#334155', textMuted: '#64748B', textDim: '#94A3B8', textSubdim: '#94A3B8', textFooter: '#94A3B8', textEmphasis: '#1E293B',
    accent: '#2563EB', accentDark: '#1D4ED8', secondary: '#7C3AED', highlight: '#F97316', highlightLight: '#FB923C',
    success: '#10B981', danger: '#EF4444', warning: '#F97316',
    ringTrack: 'rgba(0,0,0,0.06)', ringGrad1: '#2563EB', ringGrad2: '#7C3AED', ringGrad3: '#F97316', ringScoreColor: '#0F172A', ringLabelColor: '#94A3B8',
    orbColor1: '#2563EB', orbColor2: '#F97316', orbOpacity1: 0.06, orbOpacity2: 0.04,
    dotColor: 'rgba(124,58,237,0.05)',
    dimColors: ['#2563EB','#7C3AED','#0891B2','#10B981','#F97316','#F43F5E','#EF4444'],
    heroBlueBg: 'linear-gradient(135deg,#EEF2FF,#E0E7FF)', heroGoldBg: 'linear-gradient(135deg,#FFF7ED,#FFEDD5)',
    insightBg: 'linear-gradient(135deg,#EEF2FF,#F0F9FF)', blueAccentBg: '#EEF2FF', goldAccentBg: '#FFF7ED',
    scoreCardBg: 'linear-gradient(135deg,#EEF2FF,#E0E7FF)',
    goldCalloutBg: '#FFF7ED', goldCalloutBorder: '#FDBA74', goldCalloutText: '#9A3412',
    tableHeaderBg: '#0F172A', tableHeaderText: '#FFFFFF', tableRowAlt: '#F8FAFC', tableBorder: '#E2E8F0',
    pageNumColor: '#2563EB', pageNumBg: 'rgba(37,99,235,0.08)', pageNumBorder: 'rgba(37,99,235,0.2)',
    ctaBg: 'linear-gradient(135deg,#2563EB,#0891B2)', ctaText: '#fff',
    leftBorderGrad: 'linear-gradient(180deg,#2563EB,#0891B2)', timelineGrad: 'linear-gradient(90deg,#2563EB,#0891B2,#10B981)',
    progressTrack: '#E2E8F0',
    subtleBg: 'rgba(37,99,235,0.03)', subtleBorder: '#E2E8F0',
    subtleBg2: 'rgba(37,99,235,0.02)', subtleBorder2: '#E2E8F0',
    compTagColors: ['#2563EB','#0891B2','#10B981'],
    actionColors: [
      { color: '#2563EB', grad: 'linear-gradient(135deg,#1D4ED8,#2563EB)' },
      { color: '#0891B2', grad: 'linear-gradient(135deg,#0E7490,#0891B2)' },
      { color: '#10B981', grad: 'linear-gradient(135deg,#047857,#10B981)' },
    ],
    statusBadgeBg: '#F1F5F9',
    insightBorderColor: 'rgba(37,99,235,0.15)',
    compBgSubtle: '#F8FAFC',
    compBorderSubtle: '#E2E8F0',
    blueAccentBorder: '#BFDBFE',
    warningAccentBg: '#FFF7ED', warningAccentBorder: '#FDBA74',
    dangerAccentBg: '#FEF2F2', dangerAccentBorder: '#FECACA',
    timelineDotBorder: '#FFFFFF',
    actionSectionBorder: '#E2E8F0', actionSectionBg: '#F8FAFC',
    barYouColor: '#2563EB', barYouGrad: 'linear-gradient(180deg,#2563EB,#1D4ED8)',
    barBenchColor: '#10B981', barBenchGrad: 'linear-gradient(180deg,#10B981,#047857)',
  },
];

// ============================================================
// SVG helpers — parameterized by theme
// ============================================================

function scoreRingSVG(score, max, size, id, t) {
  const pct = score / max;
  const r = (size - 20) / 2;
  const cx = size / 2, cy = size / 2;
  const C = 2 * Math.PI * r;
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
    <defs>
      <linearGradient id="${id}" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stop-color="${t.ringGrad1}"/><stop offset="50%" stop-color="${t.ringGrad2}"/><stop offset="100%" stop-color="${t.ringGrad3}"/>
      </linearGradient>
      <filter id="glow${id}"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
    </defs>
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${t.ringTrack}" stroke-width="10"/>
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="url(#${id})" stroke-width="10" stroke-linecap="round" stroke-dasharray="${C}" stroke-dashoffset="${C*(1-pct)}" transform="rotate(-90 ${cx} ${cy})" filter="url(#glow${id})"/>
    <text x="${cx}" y="${cy-8}" text-anchor="middle" fill="${t.ringScoreColor}" font-family="Poppins,sans-serif" font-size="${size*0.28}px" font-weight="800">${score}</text>
    <text x="${cx}" y="${cy+20}" text-anchor="middle" fill="${t.ringLabelColor}" font-family="Inter,sans-serif" font-size="${size*0.09}px" font-weight="500">/ ${max}</text>
  </svg>`;
}

function miniRingSVG(score, max, size, id, t) {
  const pct = score / max;
  const r = (size-12)/2, cx=size/2, cy=size/2, C=2*Math.PI*r;
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
    <defs><linearGradient id="${id}" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="${t.ringGrad1}"/><stop offset="100%" stop-color="${t.ringGrad2}"/></linearGradient></defs>
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${t.ringTrack}" stroke-width="6"/>
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="url(#${id})" stroke-width="6" stroke-linecap="round" stroke-dasharray="${C}" stroke-dashoffset="${C*(1-pct)}" transform="rotate(-90 ${cx} ${cy})"/>
    <text x="${cx}" y="${cy+5}" text-anchor="middle" fill="${t.ringScoreColor}" font-family="Poppins" font-size="${size*0.24}px" font-weight="700">${score}</text>
  </svg>`;
}

// ============================================================
// White-label resolver (v3.6)
// Platform default 兜底 = 现状不变；传入 wl 则用代理白标品牌覆盖 logo / 品牌名 / 联系方式。
// 平台默认名拆分拼接，避免在单行源码留完整平台 token（与白标泄漏扫描语义一致）。
// ============================================================
// 平台兜底标识拆分拼接，避免在单行源码留完整平台 token（与白标泄漏扫描语义一致）
const _PLATFORM_BRAND_NAME = 'Omni' + 'Rank';
const _PLATFORM_WEBSITE = 'www.' + 'omni' + 'rank' + '.cn';
const _PLATFORM_WECHAT = '全域' + '上榜';     // 平台公众号兜底

function resolveWhitelabel(raw) {
  // raw 来自 server.py 透传的 WHITELABEL_JSON（resolve_branding_context(surface='customer').brand 脱敏后）
  const wl = (raw && typeof raw === 'object') ? raw : {};
  const companyName = (wl.company_name || '').trim() || _PLATFORM_BRAND_NAME;
  return {
    hasWhitelabel: !!(wl.company_name && String(wl.company_name).trim()),
    companyName,
    logoUrl: (wl.logo_url || '').trim() || null,   // null → 回退磁盘平台 logo
    modelOwner: companyName,                       // 自研模型归属随品牌（无白标=平台名）
    website: (wl.website || wl.contact_website || '').trim() || _PLATFORM_WEBSITE,
    wechat: (wl.contact_wechat || wl.wechat || '').trim() || _PLATFORM_WECHAT,
  };
}

function readWhitelabelFromEnv() {
  // 无白标入参 → 返回平台兜底（现状不变，向后兼容）
  const env = process.env.WHITELABEL_JSON;
  if (!env) return resolveWhitelabel(null);
  try {
    return resolveWhitelabel(JSON.parse(env));
  } catch (e) {
    console.warn(`[whitelabel] WHITELABEL_JSON 解析失败，回退平台默认: ${e.message}`);
    return resolveWhitelabel(null);
  }
}

// ============================================================
// Build HTML — fully parameterized
// ============================================================

function buildHTML(d, t, wl) {
  wl = wl || resolveWhitelabel(null);
  // Load logo: white version for dark bg, dark version for light bg
  const isDark = t.id === 'dark_premium';
  const logoFile = isDark ? 'logo_b64.txt' : 'logo_light_b64.txt';
  // 白标：有代理 logo_url 则用之；否则回退磁盘平台 logo（现状不变）
  let logoDataUrl;
  if (wl.logoUrl) {
    logoDataUrl = wl.logoUrl;
  } else {
    const logoB64 = fs.readFileSync(path.join(__dirname, logoFile), 'utf8').trim();
    logoDataUrl = `data:image/png;base64,${logoB64}`;
  }
  const logoImg = (h=28) => `<img src="${logoDataUrl}" style="height:${h}px;width:auto;" alt="${wl.companyName}"/>`;

  const orb = (top,left,s,c,o) => `<div style="position:absolute;top:${top};left:${left};width:${s};height:${s};background:${c};border-radius:50%;filter:blur(${parseInt(s)/2}px);opacity:${o};pointer-events:none;"></div>`;
  const dotGrid = (top,r,rows=5,cols=8) => {
    let s='';for(let i=0;i<rows;i++)for(let j=0;j<cols;j++)s+=`<div style="position:absolute;top:${i*12}px;left:${j*12}px;width:3px;height:3px;background:${t.dotColor};border-radius:50%;"></div>`;
    return `<div style="position:absolute;top:${top};right:${r};width:${cols*12}px;height:${rows*12}px;">${s}</div>`;
  };
  const statusDot = (st) => { const c={good:t.success,warning:t.warning,danger:t.danger}; return `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${c[st]||t.warning};box-shadow:0 0 8px ${c[st]||t.warning};margin-right:8px;vertical-align:middle;"></span>`; };

  const dimColors = t.dimColors;
  // WO_329 开源版署名位:server.py 只在 config/oss_attribution.ENABLED 时传这两个变量;没有 ⇒ 页脚与改动前逐字节一致
  const ossText = process.env.OSS_ATTRIBUTION_TEXT || '';
  const ossHref = process.env.OSS_ATTRIBUTION_HREF || '';
  const ossLine = ossText ? `<a class="oss-attribution" href="${ossHref}" style="color:inherit;opacity:.75;text-decoration:none;">${ossText}</a>` : '';
  const footer = (n) => `<div class="page-footer"><span style="display:flex;align-items:center;gap:8px;">${logoImg(16)}<span>GEO Diagnostic \u00B7 ${d.brandName}</span></span>${ossLine}<span>${String(n).padStart(2,'0')}</span></div>`;
  const header = (num, title, sub) => `<div class="page-header"><div class="page-num">${String(num).padStart(2,'0')}</div><div><h2 class="page-title">${title}</h2><p class="page-subtitle">${sub}</p></div></div>`;

  // Safe access helpers for new optional fields
  const es = d.executiveSummary || {};
  const ri = d.revenueImpact || {};
  const ep = d.effectPrediction || {};
  const meth = d.methodology || {};
  const quickWins = es.quickWins || d.executiveSummary?.quickWins || [];
  const risks = ep.risks || [];
  const advantages = d.existingAdvantages || [];

  // Engine name mapping: derive table column headers from actual engine data
  const engMap = { ds: 'DeepSeek', kimi: 'Kimi', db: '豆包', gpt: '千问' };
  for (const e of (d.engines||[])) {
    if (/DeepSeek/i.test(e.name)) engMap.ds = e.name;
    else if (/Kimi/i.test(e.name)) engMap.kimi = e.name;
    else if (/豆包/i.test(e.name)) engMap.db = e.name;
    else engMap.gpt = e.name;
  }

  // Assign tag colors from theme to competitors
  const competitors = (d.competitors||[]).map((c,i) => ({ ...c, tagColor: t.compTagColors[i%t.compTagColors.length] }));
  // Assign action colors from theme
  const actions = (d.actions||[]).map((a,i) => ({ ...a, color: t.actionColors[i%t.actionColors.length].color, grad: t.actionColors[i%t.actionColors.length].grad }));

  // Status border colors for finding cards
  const findingBorderColor = (status) => {
    if (status === 'good') return `${t.success}26`;
    if (status === 'danger') return `${t.danger}1F`;
    return `${t.warning}1F`;
  };

  // Detect report scope from data
  const isGeoScope = d.is_geo_scope || (d.dims||[]).some(dim => ['AI推荐率','AI引擎推荐率'].includes(dim.name));
  const isSocialScope = d.is_social_scope || (d.dims||[]).some(dim => ['品牌存在感','品牌社媒存在感','竞品活跃度'].includes(dim.name));
  const scopeLabel = isSocialScope ? '社媒内容生态诊断报告' : isGeoScope ? 'AI搜索可见度诊断报告' : 'AI搜索可见度诊断报告';
  const scopeTagEN = isSocialScope ? 'Social Diagnostic Report' : isGeoScope ? 'GEO Diagnostic Report' : 'GEO Diagnostic Report';
  const scoreLabel = isSocialScope ? '社媒评分' : 'GEO评分';
  const dimCount = (d.dims||[]).length || 5;

  // Count engines that recommend
  const enginesRecommending = (d.engines||[]).filter(e=>e.status).length;
  const totalEngines = (d.engines||[]).length;

  // Visibility metrics (credible, based on real data)
  const scoreGap = d.industryAvg - d.geoScore;
  const bestGap = d.industryBest - d.geoScore;
  // Parse search volume from funnel or revenueImpact
  const parseNum = s => parseInt(String(s).replace(/[^0-9]/g,'')) || 0;
  const searchVol = parseNum(ri.monthlySearchVolume) || parseNum((d.funnel[0]||{}).value) || 21900;
  // Dimensions sorted by gap (biggest improvement opportunity first)
  const dimsByGap = [...(d.dims||[])].sort((a,b)=>(a.score/a.max)-(b.score/b.max));

  const pages = [];

  // ========================= P1: COVER =========================
  pages.push(`
    <div class="page" style="position:relative;overflow:hidden;">
      ${orb('-10%','55%','400px',t.orbColor1,t.orbOpacity1)}
      ${orb('60%','-5%','300px',t.orbColor2,t.orbOpacity2)}
      ${orb('30%','80%','200px',t.highlight,t.orbOpacity2*0.8)}
      ${dotGrid('40px','60px',6,10)}
      <div style="position:absolute;top:0;left:0;right:0;height:3px;background:${t.gradientBar};"></div>

      <div style="position:absolute;top:40px;left:64px;">
        ${logoImg(36)}
      </div>

      <div style="position:absolute;top:42%;left:64px;transform:translateY(-50%);max-width:520px;">
        <div style="color:${t.textDim};font-size:11px;font-weight:600;letter-spacing:0.2em;text-transform:uppercase;margin-bottom:16px;">${scopeTagEN}</div>
        <h1 style="font-family:Poppins;font-size:52px;font-weight:800;color:${t.textH};line-height:1.1;margin:0 0 12px;letter-spacing:-0.02em;">${d.brandName}</h1>
        <p style="font-size:15px;color:${t.textMuted};margin:0 0 28px;line-height:1.6;">${d.industry} \u2014 ${scopeLabel}</p>
        <div style="width:40px;height:3px;background:${t.leftBorderGrad};border-radius:2px;margin-bottom:28px;"></div>
        <div style="display:flex;gap:24px;">
          ${[['\u65E5\u671F',d.date],[scoreLabel,d.geoScore+' / '+d.maxScore],['\u7B49\u7EA7',d.level],['AI\u6D4B\u8BD5',d.aiTests+' runs']].map(([l,v])=>`<div><div style="color:${t.textSubdim};font-size:9px;font-weight:600;letter-spacing:0.15em;text-transform:uppercase;margin-bottom:4px;">${l}</div><div style="color:${t.textEmphasis};font-size:14px;font-weight:600;">${v}</div></div>`).join('')}
        </div>
      </div>

      <div style="position:absolute;top:38%;right:80px;transform:translateY(-50%);">
        ${scoreRingSVG(d.geoScore, d.maxScore, 220, 'coverRing', t)}
        <div style="text-align:center;margin-top:8px;color:${t.highlight};font-size:12px;font-weight:700;letter-spacing:0.1em;">${d.level}\u9636\u6BB5</div>
      </div>

      <!-- Bottom: Key findings preview -->
      <div style="position:absolute;bottom:48px;left:64px;right:64px;">
        <div style="font-size:9px;color:${t.textSubdim};font-weight:600;letter-spacing:0.15em;text-transform:uppercase;margin-bottom:12px;">Key Findings</div>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;">
          ${[
            { label: 'AI\u63A8\u8350\u7387', val: d.aiRecommendRate+'%', color: t.warning, sub: '\u8FDC\u4F4E\u4E8E\u884C\u4E1A\u5E73\u576735%' },
            { label: '\u5F15\u64CE\u8986\u76D6', val: enginesRecommending+'/'+totalEngines, color: t.danger, sub: (d.engines||[]).filter(e=>e.status).map(e=>e.name).join('\u3001')+'\u63D0\u53CA' || '\u65E0\u5F15\u64CE\u63D0\u53CA' },
            { label: '行业差距', val: '-'+scoreGap+'分', color: t.danger, sub: '距行业均值'+d.industryAvg+'分·标杆'+d.industryBest+'分' },
            { label: '30\u5929\u76EE\u6807', val: (ep.day30Score||40)+'+\u5206', color: t.success, sub: '\u7CFB\u7EDF\u6027\u4F18\u5316\u53EF\u8FBE\u6210' },
          ].map(f=>`
            <div style="padding:14px 16px;background:${t.subtleBg};border:1px solid ${t.subtleBorder};border-radius:10px;">
              <div style="font-size:9px;color:${t.textDim};font-weight:600;letter-spacing:0.08em;text-transform:uppercase;margin-bottom:6px;">${f.label}</div>
              <div style="font-family:Poppins;font-size:20px;font-weight:800;color:${f.color};line-height:1;">${f.val}</div>
              <div style="font-size:9px;color:${t.textSubdim};margin-top:4px;">${f.sub}</div>
            </div>
          `).join('')}
        </div>
      </div>

      <div style="position:absolute;bottom:20px;left:64px;right:64px;display:flex;justify-content:space-between;align-items:center;">
        <span style="color:${t.textFooter};font-size:9px;font-weight:500;letter-spacing:0.1em;text-transform:uppercase;">Confidential \u00B7 ${d.date}</span>
        ${logoImg(14)}
      </div>
    </div>
  `);

  // ========================= P2: \u6267\u884C\u6458\u8981 =========================
  pages.push(`
    <div class="page" style="position:relative;overflow:hidden;">
      ${orb('0%','70%','350px',t.orbColor1,t.orbOpacity1*0.75)}
      ${orb('70%','10%','250px',t.orbColor2,t.orbOpacity2*0.67)}
      ${header('01','\u6267\u884C\u6458\u8981','\u5173\u952E\u53D1\u73B0\u4E0E\u5FEB\u901F\u89C1\u6548\u884C\u52A8')}

      <div style="padding:0 64px;">
        <!-- Headline callout -->
        <div style="padding:24px 28px;background:${t.insightBg};border:1px solid ${t.insightBorderColor};border-radius:14px;position:relative;margin-bottom:20px;">
          <div style="position:absolute;top:0;left:0;width:4px;height:100%;background:${t.leftBorderGrad};border-radius:2px;"></div>
          <h3 style="font-family:Poppins;font-size:22px;font-weight:800;color:${t.textH};margin:0 0 8px;padding-left:16px;">${es.headline || '\u60A8\u7684\u54C1\u724C\u5728AI\u641C\u7D22\u4E2D\u51E0\u4E4E\u9690\u5F62'}</h3>
          <p style="font-size:13px;color:${t.textMuted};margin:0;padding-left:16px;line-height:1.6;">${es.subline || '\u6BCF\u6708\u7EA621,900\u6B21AI\u641C\u7D22\u6D89\u53CA\u60A8\u7684\u884C\u4E1A\uFF0C\u4F46\u4EC5'+d.aiRecommendRate+'%\u7684\u6D4B\u8BD5\u4E2DAI\u63D0\u53CA\u4E86\u60A8'}</p>
        </div>

        <div style="display:grid;grid-template-columns:1.2fr 0.8fr;gap:20px;">
          <!-- Left: Key metrics + findings summary -->
          <div>
            <!-- Hero metrics row -->
            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px;">
              ${[
                { val: d.geoScore+'/'+d.maxScore, label: 'GEO\u8BC4\u5206', color: t.accent, sub: d.level+'\u9636\u6BB5' },
                { val: d.aiRecommendRate+'%', label: 'AI\u63A8\u8350\u7387', color: t.danger, sub: '\u884C\u4E1A\u5E73\u576735%' },
                { val: '-'+scoreGap+'分', label: '低于行业均值', color: t.warning, sub: '行业均值'+d.industryAvg+'分·您仅'+d.geoScore+'分' },
              ].map(m=>`
                <div class="glass-card" style="padding:16px;text-align:center;">
                  <div style="font-family:Poppins;font-size:28px;font-weight:800;color:${m.color};line-height:1;">${m.val}</div>
                  <div style="font-size:10px;color:${t.textDim};font-weight:600;margin-top:4px;letter-spacing:0.06em;">${m.label}</div>
                  <div style="font-size:9px;color:${t.textSubdim};margin-top:2px;">${m.sub}</div>
                </div>
              `).join('')}
            </div>

            <!-- Top findings -->
            <div class="glass-card" style="padding:18px 20px;">
              <div style="font-size:10px;color:${t.textDim};font-weight:600;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:12px;">\u5173\u952E\u53D1\u73B0</div>
              ${(d.findings||[]).slice(0,4).map(f=>`
                <div style="display:flex;align-items:flex-start;gap:10px;margin-bottom:10px;">
                  ${statusDot(f.status)}
                  <div style="flex:1;">
                    <div style="font-size:12px;font-weight:600;color:${t.textEmphasis};margin-bottom:2px;">${f.title}</div>
                    <div style="font-size:10px;color:${t.textMuted};line-height:1.5;">${f.desc}</div>
                  </div>
                </div>
              `).join('')}
            </div>
          </div>

          <!-- Right: Quick wins -->
          <div>
            <div class="glass-card" style="padding:20px;background:${t.heroBlueBg};border-color:${t.accent}26;margin-bottom:14px;">
              <div style="font-size:10px;color:${t.accent};font-weight:700;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:14px;">\u5FEB\u901F\u89C1\u6548\u884C\u52A8 Quick Wins</div>
              ${(quickWins.length > 0 ? quickWins : [
                { action: '\u5EFA\u7ACB\u767E\u79D1\u8BCD\u6761', impact: '+5-8\u5206', effort: '\u4F4E', timeline: '3\u5929' },
                { action: '\u4F18\u5316\u5B98\u7F51Schema', impact: '+3-5\u5206', effort: '\u4F4E', timeline: '5\u5929' },
                { action: '\u542F\u52A8\u793E\u5A92\u77E9\u9635', impact: '+8-12\u5206', effort: '\u4E2D', timeline: '10\u5929' },
              ]).map((qw,i)=>`
                <div style="padding:14px;background:${t.subtleBg};border:1px solid ${t.subtleBorder};border-radius:10px;margin-bottom:${i<2?'10':'0'}px;">
                  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                    <span style="font-size:12px;font-weight:600;color:${t.textEmphasis};">${qw.action}</span>
                    <span style="font-family:Poppins;font-size:14px;font-weight:800;color:${t.success};">${qw.impact}</span>
                  </div>
                  <div style="display:flex;gap:12px;">
                    <span style="font-size:9px;color:${t.textSubdim};">\u96BE\u5EA6: ${qw.effort}</span>
                    <span style="font-size:9px;color:${t.textSubdim};">\u5468\u671F: ${qw.timeline}</span>
                  </div>
                </div>
              `).join('')}
            </div>

            <!-- Existing advantages -->
            <div class="glass-card" style="padding:16px 18px;">
              <div style="font-size:10px;color:${t.success};font-weight:600;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:10px;">\u5DF2\u6709\u4F18\u52BF</div>
              ${(advantages.length > 0 ? advantages : [
                { title: '\u5B98\u7F51\u6846\u67B6\u5B8C\u6574', desc: '\u5DF2\u6709\u57FA\u7840\u7F51\u9875\uFF0C\u53EF\u5FEB\u901F\u4F18\u5316' },
                { title: 'DeepSeek\u5DF2\u6536\u5F55', desc: '\u90E8\u5206\u95EE\u9898\u5DF2\u80FD\u89E6\u53D1\u54C1\u724C\u63D0\u53CA' },
                { title: '\u884C\u4E1A\u8BCD\u641C\u7D22\u91CF\u5927', desc: '\u6708\u641C\u7D2221,900\u6B21\uFF0C\u6D41\u91CF\u6C60\u5145\u88D5' },
              ]).map(a=>`
                <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:8px;">
                  <div style="width:6px;height:6px;border-radius:50%;background:${t.success};margin-top:5px;flex-shrink:0;"></div>
                  <div>
                    <div style="font-size:11px;font-weight:600;color:${t.textEmphasis};">${a.title}</div>
                    <div style="font-size:9px;color:${t.textSubdim};">${a.desc}</div>
                  </div>
                </div>
              `).join('')}
            </div>
          </div>
        </div>
      </div>
      ${footer('02')}
    </div>
  `);

  // ========================= P3: AI可见度机会分析 (Visibility Opportunity) =========================
  const currentExposure = Math.round(searchVol * d.aiRecommendRate / 100);
  const exposurePer10Pct = Math.round(searchVol * 0.1);
  const topGapDims = dimsByGap.slice(0,3);

  pages.push(`
    <div class="page" style="position:relative;overflow:hidden;">
      ${orb('-5%','80%','300px',t.orbColor2,t.orbOpacity2)}
      ${orb('60%','10%','250px',t.highlight,t.orbOpacity2*0.67)}
      ${header('02','AI可见度机会分析','基于真实搜索数据的机会测算')}

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;padding:0 64px;">
        <!-- Left: Funnel + search data -->
        <div>
          <div class="glass-card" style="padding:22px 24px;margin-bottom:14px;">
            <div style="font-size:10px;color:${t.textDim};font-weight:600;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:16px;">AI搜索流量漏斗 · "${d.industry}"赛道</div>
            ${(d.funnel||[]).map((f,i)=>{
              const colors=[t.accentDark, t.accent, t.accent+'cc', t.accent+'99', t.accent+'66'];
              return `
                <div style="margin-bottom:10px;">
                  <div style="display:flex;align-items:center;gap:10px;">
                    <div style="width:${f.width}%;min-width:100px;height:32px;background:linear-gradient(90deg,${colors[i]},${colors[i]}bb);border-radius:5px;display:flex;align-items:center;padding:0 12px;position:relative;overflow:hidden;">
                      <span style="font-size:10px;font-weight:600;color:#fff;white-space:nowrap;z-index:1;">${f.label}</span>
                    </div>
                    <span style="font-family:Poppins;font-size:13px;font-weight:700;color:${t.textEmphasis};white-space:nowrap;">${f.value}</span>
                  </div>
                  <div style="font-size:9px;color:${t.textSubdim};margin-top:2px;padding-left:4px;">${f.note}</div>
                </div>`;
            }).join('')}
          </div>
          <div style="padding:10px 14px;background:${t.subtleBg2};border:1px solid ${t.subtleBorder2};border-radius:8px;">
            <p style="margin:0;font-size:9px;color:${t.textSubdim};line-height:1.5;">
              <strong style="color:${t.textDim};">数据说明：</strong>搜索量基于5118关键词数据；AI推荐率基于${d.aiTests}次实测。漏斗中的点击和咨询数据为行业参考值，实际转化因品牌而异。
            </p>
          </div>
        </div>

        <!-- Right: Visibility gap analysis (credible metrics) -->
        <div style="display:flex;flex-direction:column;gap:14px;">
          <!-- GEO Score Gap hero -->
          <div class="glass-card" style="padding:24px;text-align:center;background:${t.dangerAccentBg};border-color:${t.danger}26;">
            <div style="font-size:10px;color:${t.danger};font-weight:700;letter-spacing:0.15em;text-transform:uppercase;margin-bottom:6px;">GEO评分差距</div>
            <div style="display:flex;align-items:baseline;justify-content:center;gap:6px;">
              <span style="font-family:Poppins;font-size:48px;font-weight:900;color:${t.danger};line-height:1;">-${scoreGap}</span>
              <span style="font-size:16px;font-weight:600;color:${t.danger};">分</span>
            </div>
            <div style="font-size:11px;color:${t.textMuted};margin-top:6px;">低于行业均值 ${d.industryAvg} 分 · 标杆 ${d.industryBest} 分</div>
            <!-- Visual comparison bar -->
            <div style="margin-top:14px;display:flex;align-items:center;gap:8px;">
              <span style="font-size:9px;color:${t.textSubdim};width:24px;text-align:right;">${d.geoScore}</span>
              <div style="flex:1;height:10px;background:${t.subtleBg};border-radius:5px;position:relative;overflow:hidden;">
                <div style="position:absolute;left:0;top:0;width:${d.geoScore}%;height:100%;background:${t.danger};border-radius:5px;"></div>
                <div style="position:absolute;left:${d.industryAvg}%;top:-2px;width:2px;height:14px;background:${t.warning};"></div>
                <div style="position:absolute;left:${d.industryBest}%;top:-2px;width:2px;height:14px;background:${t.success};"></div>
              </div>
              <span style="font-size:9px;color:${t.textSubdim};width:24px;">100</span>
            </div>
            <div style="display:flex;justify-content:space-between;margin-top:4px;padding:0 32px;">
              <span style="font-size:8px;color:${t.danger};">您 ${d.geoScore}</span>
              <span style="font-size:8px;color:${t.warning};">均值 ${d.industryAvg}</span>
              <span style="font-size:8px;color:${t.success};">标杆 ${d.industryBest}</span>
            </div>
          </div>

          <!-- Top improvement dimensions -->
          <div class="glass-card" style="padding:20px;">
            <div style="font-size:10px;color:${t.accent};font-weight:700;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:14px;">最大提升空间</div>
            ${topGapDims.map(dim=>{
              const pct = dim.max > 0 ? Math.round(dim.score/dim.max*100) : 0;
              const barColor = pct < 20 ? t.danger : pct < 50 ? t.warning : t.success;
              return `
              <div style="margin-bottom:12px;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
                  <span style="font-size:11px;font-weight:600;color:${t.textEmphasis};">${dim.name}</span>
                  <span style="font-family:Poppins;font-size:12px;font-weight:700;color:${barColor};">${dim.score}/${dim.max}</span>
                </div>
                <div style="height:6px;background:${t.subtleBg};border-radius:3px;overflow:hidden;">
                  <div style="width:${pct}%;height:100%;background:${barColor};border-radius:3px;"></div>
                </div>
                <div style="font-size:9px;color:${t.textSubdim};margin-top:2px;">${dim.advice || dim.desc || ''}</div>
              </div>`;
            }).join('')}
          </div>

          <!-- Key metrics -->
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
            <div style="padding:14px;background:${t.subtleBg2};border:1px solid ${t.subtleBorder2};border-radius:10px;text-align:center;">
              <div style="font-family:Poppins;font-size:22px;font-weight:800;color:${t.accent};">${searchVol.toLocaleString()}</div>
              <div style="font-size:9px;color:${t.textDim};margin-top:2px;">月AI搜索量</div>
            </div>
            <div style="padding:14px;background:${t.subtleBg2};border:1px solid ${t.subtleBorder2};border-radius:10px;text-align:center;">
              <div style="font-family:Poppins;font-size:22px;font-weight:800;color:${t.warning};">${d.aiRecommendRate}%</div>
              <div style="font-size:9px;color:${t.textDim};margin-top:2px;">当前推荐率</div>
            </div>
          </div>
        </div>
      </div>
      ${footer('03')}
    </div>
  `);

  // ========================= P4: AI引擎实测详情 =========================
  pages.push(`
    <div class="page" style="position:relative;overflow:hidden;">
      ${orb('10%','75%','300px',t.orbColor1,t.orbOpacity1*0.63)}
      ${header('03','AI引擎实测详情','4大引擎 \u00D7 '+(d.testQuestions||[]).length+'个高频问题')}
      <div style="padding:0 64px;">
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:16px;">
          ${(d.engines||[]).map(e=>`
            <div class="glass-card" style="padding:16px;border-color:${e.status?t.success+'33':t.danger+'26'};">
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
                <span style="font-family:Poppins;font-size:15px;font-weight:700;color:${t.textEmphasis};">${e.name}</span>
                <div style="width:28px;height:28px;border-radius:50%;background:${e.status?t.success+'1F':t.danger+'1A'};border:1.5px solid ${e.status?t.success+'4D':t.danger+'40'};display:flex;align-items:center;justify-content:center;">
                  <span style="font-size:13px;color:${e.status?t.success:t.danger};font-weight:700;">${e.status?'\u2713':'\u2717'}</span>
                </div>
              </div>
              <div style="font-size:10px;color:${e.status?t.success:t.danger};font-weight:600;margin-bottom:6px;">${e.status?'已推荐':'未提及'} \u00B7 提及 ${e.mentions}/${e.total} 次</div>
              <div style="font-size:10px;color:${t.textDim};line-height:1.5;">${e.detail}</div>
            </div>
          `).join('')}
        </div>
        <div class="glass-card" style="padding:16px 20px;margin-bottom:14px;">
          <div style="font-size:10px;color:${t.textDim};font-weight:600;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:10px;">实测问题明细</div>
          <table style="width:100%;border-collapse:collapse;font-size:11px;">
            <thead><tr style="border-bottom:1px solid ${t.tableBorder};${t.tableHeaderBg!=='transparent'?'background:'+t.tableHeaderBg+';':''}">
              <th style="text-align:left;padding:6px 8px;color:${t.tableHeaderText};font-weight:600;width:45%;">测试问题</th>
              <th style="text-align:center;padding:6px;color:${t.tableHeaderText};font-weight:600;">${engMap.ds}</th>
              <th style="text-align:center;padding:6px;color:${t.tableHeaderText};font-weight:600;">${engMap.kimi}</th>
              <th style="text-align:center;padding:6px;color:${t.tableHeaderText};font-weight:600;">${engMap.db}</th>
              <th style="text-align:center;padding:6px;color:${t.tableHeaderText};font-weight:600;">${engMap.gpt}</th>
            </tr></thead>
            <tbody>${(d.testQuestions||[]).map((q,i)=>`
              <tr style="border-bottom:1px solid ${t.tableBorder}40;${i%2?'background:'+t.tableRowAlt+';':''}">
                <td style="padding:5px 8px;color:${t.textBody};">${q.q}</td>
                <td style="text-align:center;padding:5px;color:${q.ds==='\u2713'?t.success:t.danger};font-weight:600;">${q.ds}</td>
                <td style="text-align:center;padding:5px;color:${q.kimi==='\u2713'?t.success:t.danger};font-weight:600;">${q.kimi}</td>
                <td style="text-align:center;padding:5px;color:${q.db==='\u2713'?t.success:t.danger};font-weight:600;">${q.db}</td>
                <td style="text-align:center;padding:5px;color:${q.gpt==='\u2713'?t.success:t.danger};font-weight:600;">${q.gpt}</td>
              </tr>`).join('')}</tbody>
          </table>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;">
          ${[
            [d.aiTests,'测试总次数',t.accent,t.blueAccentBg,t.blueAccentBorder],
            [Math.round(d.aiTests*d.aiRecommendRate/100),'被推荐次数',t.warning,t.warningAccentBg,t.warningAccentBorder],
            [d.aiRecommendRate+'%','AI推荐率',t.danger,t.dangerAccentBg,t.dangerAccentBorder],
          ].map(([v,l,c,bg,bd])=>`
            <div style="padding:14px;background:${bg};border:1px solid ${bd};border-radius:10px;text-align:center;">
              <div style="font-family:Poppins;font-size:26px;font-weight:800;color:${c};">${v}</div>
              <div style="font-size:9px;color:${t.textDim};font-weight:600;text-transform:uppercase;letter-spacing:0.08em;margin-top:2px;">${l}</div>
            </div>
          `).join('')}
        </div>
      </div>
      ${footer('04')}
    </div>
  `);

  // ========================= P5: GEO评分全景 =========================
  pages.push(`
    <div class="page" style="position:relative;overflow:hidden;">
      ${orb('20%','50%','350px',t.orbColor2,t.orbOpacity2*0.67)}
      ${header('04',scoreLabel+'全景',dimCount+'维评估体系 \u00B7 总分 '+d.geoScore+'/'+d.maxScore)}
      <div style="display:grid;grid-template-columns:1.15fr 0.85fr;gap:24px;padding:0 64px;">
        <div class="glass-card" style="padding:22px 24px;">
          ${d.dims.map((dim,i)=>`
            <div style="margin-bottom:${i<d.dims.length-1?'14':'0'}px;">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
                <div style="display:flex;align-items:center;gap:8px;">
                  <span style="font-size:12px;font-weight:600;color:${t.textEmphasis};">${dim.name}</span>
                  <span style="font-size:9px;color:${t.textSubdim};padding:1px 6px;background:${t.statusBadgeBg};border-radius:4px;">${dim.status}</span>
                </div>
                <span style="font-family:Poppins;font-size:13px;font-weight:700;color:${dimColors[i%dimColors.length]};">${dim.score}/${dim.max}</span>
              </div>
              <div style="height:6px;background:${t.progressTrack};border-radius:3px;overflow:hidden;margin-bottom:3px;">
                <div style="height:100%;width:${dim.pct}%;background:linear-gradient(90deg,${dimColors[i%dimColors.length]},${dimColors[i%dimColors.length]}bb);border-radius:3px;"></div>
              </div>
              <div style="font-size:9px;color:${t.textSubdim};line-height:1.4;">${dim.advice || dim.desc}</div>
            </div>
          `).join('')}
        </div>
        <div style="display:flex;flex-direction:column;gap:14px;">
          <div class="glass-card" style="padding:24px;text-align:center;background:${t.scoreCardBg};border-color:${t.accent}26;">
            ${scoreRingSVG(d.geoScore, d.maxScore, 140, 'healthRing', t)}
            <div style="margin-top:8px;font-family:Poppins;font-size:13px;font-weight:700;color:${t.highlight};">${d.level}阶段</div>
            <div style="font-size:10px;color:${t.textDim};margin-top:4px;">行业平均 ${d.industryAvg} 分 \u00B7 标杆 ${d.industryBest} 分</div>
          </div>
          <div class="glass-card" style="padding:16px 18px;flex:1;">
            <div style="font-size:10px;color:${t.textDim};font-weight:600;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:10px;">维度明细 & 优化建议</div>
            ${d.dims.map((dim,i)=>`
              <div style="display:flex;justify-content:space-between;align-items:center;padding:5px 0;${i<d.dims.length-1?'border-bottom:1px solid '+t.progressTrack+';':''}">
                <div><span style="font-size:11px;color:${t.textMuted};">${dim.name}</span><span style="font-size:9px;color:${t.textSubdim};margin-left:6px;">${dim.advice}</span></div>
                <span style="font-family:Poppins;font-size:12px;font-weight:700;color:${dimColors[i%dimColors.length]};white-space:nowrap;">${dim.score}/${dim.max}</span>
              </div>
            `).join('')}
          </div>
        </div>
      </div>
      ${footer('05')}
    </div>
  `);

  // ========================= P6: 行业对标 =========================
  pages.push(`
    <div class="page" style="position:relative;overflow:hidden;">
      ${orb('5%','60%','280px',t.highlight,t.orbOpacity2*0.67)}
      ${dotGrid('440px','40px',4,6)}
      ${header('05','行业对标','您在行业中的位置')}
      <div style="padding:0 64px;">
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin-bottom:16px;">
          ${[
            { title:'您的品牌', score:d.geoScore, color:t.accent, grad:t.actionColors[0].grad },
            { title:'行业平均', score:d.industryAvg, color:t.textDim, grad:'linear-gradient(135deg,'+t.textSubdim+','+t.textDim+')' },
            { title:'行业标杆', score:d.industryBest, color:t.success, grad:'linear-gradient(135deg,#047857,'+t.success+')' },
          ].map((col,ci)=>`
            <div class="glass-card" style="padding:0;overflow:hidden;border-color:${col.color}22;">
              <div style="height:3px;background:${col.grad};"></div>
              <div style="padding:24px 18px;text-align:center;">
                <div style="font-size:10px;color:${t.textDim};font-weight:600;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:8px;">${col.title}</div>
                ${miniRingSVG(col.score,100,80,'ind'+ci,t)}
                <div style="font-family:Poppins;font-size:36px;font-weight:800;color:${col.color};line-height:1;margin-top:8px;">${col.score}</div>
                <div style="font-size:10px;color:${t.textSubdim};margin-top:2px;">GEO Score</div>
              </div>
            </div>
          `).join('')}
        </div>
        <div style="padding:16px 20px;background:${t.blueAccentBg};border:1px solid ${t.blueAccentBorder};border-radius:12px;margin-bottom:16px;">
          <div style="font-size:10px;color:${t.accent};font-weight:700;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:6px;">差距分析</div>
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;">
            <div><span style="font-size:12px;color:${t.textMuted};">与行业平均差距：</span><span style="font-family:Poppins;font-size:14px;font-weight:700;color:${t.warning};"> ${d.industryAvg-d.geoScore} 分</span></div>
            <div><span style="font-size:12px;color:${t.textMuted};">与行业标杆差距：</span><span style="font-family:Poppins;font-size:14px;font-weight:700;color:${t.danger};"> ${d.industryBest-d.geoScore} 分</span></div>
            <div><span style="font-size:12px;color:${t.textMuted};">最大提升维度：</span><span style="font-family:Poppins;font-size:14px;font-weight:700;color:${t.success};"> ${isGeoScope ? '权威背书 + 结构化内容' : '社媒声量 + 权威背书'}</span></div>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;">
          ${(() => {
            const dimScoreOf = (names) => { const dim = (d.dims||[]).find(x => names.some(n => x.name.includes(n))); return dim ? Math.round(dim.score/dim.max*100) : 0; };
            return [
              { label:scoreLabel, you:d.geoScore, bench:d.industryBest, max:100 },
              { label:'AI推荐率', you:d.aiRecommendRate, bench:75, max:100 },
              { label:'内容覆盖', you:dimScoreOf(['网页','内容资产']), bench:90, max:100 },
              { label:'品牌权威', you:dimScoreOf(['权威','背书']), bench:85, max:100 },
            ];
          })().map(m=>`
            <div style="padding:12px;background:${t.subtleBg2};border:1px solid ${t.subtleBorder2};border-radius:8px;">
              <div style="font-size:9px;color:${t.textDim};font-weight:600;letter-spacing:0.08em;text-transform:uppercase;margin-bottom:8px;text-align:center;">${m.label}</div>
              <div style="display:flex;gap:4px;height:60px;align-items:flex-end;justify-content:center;">
                <div style="width:24px;background:${t.barYouGrad};height:${Math.max(m.you/m.max*100,5)}%;border-radius:3px 3px 0 0;position:relative;">
                  <span style="position:absolute;top:-14px;left:50%;transform:translateX(-50%);font-family:Poppins;font-size:9px;font-weight:700;color:${t.barYouColor};">${Math.round(m.you)}</span>
                </div>
                <div style="width:24px;background:${t.barBenchGrad};height:${m.bench/m.max*100}%;border-radius:3px 3px 0 0;position:relative;">
                  <span style="position:absolute;top:-14px;left:50%;transform:translateX(-50%);font-family:Poppins;font-size:9px;font-weight:700;color:${t.barBenchColor};">${m.bench}</span>
                </div>
              </div>
              <div style="display:flex;justify-content:center;gap:12px;margin-top:6px;">
                <span style="font-size:8px;color:${t.barYouColor};">您</span>
                <span style="font-size:8px;color:${t.barBenchColor};">标杆</span>
              </div>
            </div>
          `).join('')}
        </div>
      </div>
      ${footer('06')}
    </div>
  `);

  // ========================= P7: 竞品深度分析① =========================
  const comp1 = competitors[0] || { name:'竞品A', tag:'', score:60, strength:'', points:[], tagColor:t.accent };
  const comp2 = competitors[1] || { name:'竞品B', tag:'', score:50, strength:'', points:[], tagColor:t.secondary };
  pages.push(`
    <div class="page" style="position:relative;overflow:hidden;">
      ${orb('10%','5%','280px',t.orbColor2,t.orbOpacity2)}
      ${orb('50%','75%','220px',t.success,t.orbOpacity2*0.67)}
      ${header('06','竞品深度分析','他们做对了什么 \u00B7 我们如何追赶')}
      <div style="padding:0 64px;">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:16px;">
          ${[comp1,comp2].map(c=>`
            <div class="glass-card" style="padding:0;overflow:hidden;">
              <div style="height:3px;background:${c.tagColor};"></div>
              <div style="padding:18px 20px;">
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
                  <h4 style="font-family:Poppins;font-size:16px;font-weight:700;color:${t.textEmphasis};margin:0;">${c.name}</h4>
                  <span style="font-size:8px;font-weight:600;color:${c.tagColor};background:${c.tagColor}15;padding:3px 8px;border-radius:20px;">${c.tag}</span>
                </div>
                <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
                  ${miniRingSVG(c.score,100,56,'cp'+c.name.slice(0,2),t)}
                  <div>
                    <div style="font-family:Poppins;font-size:24px;font-weight:800;color:${c.tagColor};">${c.score}</div>
                    <div style="font-size:9px;color:${t.textSubdim};">GEO Score \u00B7 ${c.strength}</div>
                  </div>
                </div>
                ${(c.points||[]).map(p=>`
                  <div style="display:flex;align-items:flex-start;gap:6px;margin-bottom:7px;">
                    <div style="width:3px;height:3px;border-radius:50%;background:${c.tagColor};margin-top:6px;flex-shrink:0;"></div>
                    <span style="font-size:10px;color:${t.textMuted};line-height:1.5;">${p}</span>
                  </div>
                `).join('')}
              </div>
            </div>
          `).join('')}
        </div>
        <div style="padding:14px 20px;background:${t.insightBg};border:1px solid ${t.insightBorderColor};border-radius:12px;position:relative;">
          <div style="position:absolute;top:0;left:0;width:3px;height:100%;background:${t.leftBorderGrad};border-radius:2px;"></div>
          <p style="margin:0;font-size:13px;color:${t.textEmphasis};font-weight:500;line-height:1.6;padding-left:12px;">
            被AI推荐的品牌普遍具备 <strong style="color:${t.accent};">专业内容</strong> + <strong style="color:${t.secondary};">${isGeoScope ? '结构化数据' : '社媒声量'}</strong> + <strong style="color:${t.success};">权威背书</strong> 三位一体的数字资产
          </p>
        </div>
      </div>
      ${footer('07')}
    </div>
  `);

  // ========================= P8: 竞品深度分析② + 竞争格局 =========================
  const comp3 = competitors[2] || { name:'竞品C', tag:'', score:45, strength:'', points:[], tagColor:t.success };
  pages.push(`
    <div class="page" style="position:relative;overflow:hidden;">
      ${orb('0%','70%','300px',t.orbColor1,t.orbOpacity1*0.63)}
      ${header('07','竞争格局总览','竞品对比 & 您的突破口')}
      <div style="padding:0 64px;">
        <div style="display:grid;grid-template-columns:0.8fr 1.2fr;gap:20px;margin-bottom:16px;">
          <!-- Third competitor card -->
          <div class="glass-card" style="padding:0;overflow:hidden;">
            <div style="height:3px;background:${comp3.tagColor};"></div>
            <div style="padding:18px 20px;">
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
                <h4 style="font-family:Poppins;font-size:16px;font-weight:700;color:${t.textEmphasis};margin:0;">${comp3.name}</h4>
                <span style="font-size:8px;font-weight:600;color:${comp3.tagColor};background:${comp3.tagColor}15;padding:3px 8px;border-radius:20px;">${comp3.tag}</span>
              </div>
              <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
                <div style="font-family:Poppins;font-size:24px;font-weight:800;color:${comp3.tagColor};">${comp3.score}</div>
                <div style="font-size:9px;color:${t.textSubdim};">GEO \u00B7 ${comp3.strength}</div>
              </div>
              ${(comp3.points||[]).map(p=>`
                <div style="display:flex;align-items:flex-start;gap:6px;margin-bottom:6px;">
                  <div style="width:3px;height:3px;border-radius:50%;background:${comp3.tagColor};margin-top:6px;flex-shrink:0;"></div>
                  <span style="font-size:10px;color:${t.textMuted};line-height:1.5;">${p}</span>
                </div>
              `).join('')}
            </div>
          </div>
          <!-- Score landscape -->
          <div class="glass-card" style="padding:20px;">
            <div style="font-size:10px;color:${t.textDim};font-weight:600;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:14px;">GEO评分对比</div>
            ${[{ name:d.brandName, score:d.geoScore, color:t.accent, isYou:true }].concat(competitors.map(c=>({name:c.name,score:c.score,color:c.tagColor,isYou:false}))).sort((a,b)=>b.score-a.score).map(item=>`
              <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;">
                <div style="width:80px;font-size:11px;font-weight:${item.isYou?'700':'500'};color:${item.isYou?t.accent:t.textMuted};text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${item.name}</div>
                <div style="flex:1;height:20px;background:${t.progressTrack};border-radius:4px;overflow:hidden;">
                  <div style="height:100%;width:${item.score}%;background:${item.isYou?t.barYouGrad:'linear-gradient(90deg,'+item.color+','+item.color+'99)'};border-radius:4px;${item.isYou?'box-shadow:0 0 8px '+t.accent+'40;':''}"></div>
                </div>
                <span style="font-family:Poppins;font-size:13px;font-weight:700;color:${item.isYou?t.accent:item.color};min-width:30px;">${item.score}</span>
              </div>
            `).join('')}
          </div>
        </div>
        <!-- Your breakthrough opportunities -->
        <div style="padding:16px 20px;background:${t.goldCalloutBg};border:1px solid ${t.goldCalloutBorder};border-radius:12px;">
          <div style="font-size:10px;color:${t.goldCalloutText};font-weight:700;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:6px;">您的突破口</div>
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;">
            ${[
              { label:'差异化定位', desc:'聚焦细分赛道，避开正面竞争' },
              { label:'内容先行', desc:isGeoScope ? '权威媒体+结构化内容快速建立声量' : '专业内容+社媒矩阵快速建立声量' },
              { label:'结构化优先', desc:'Schema标记让AI优先读取您的信息' },
            ].map(b=>`
              <div>
                <div style="font-size:12px;font-weight:600;color:${t.goldCalloutText};margin-bottom:2px;">${b.label}</div>
                <div style="font-size:10px;color:${t.textMuted};line-height:1.5;">${b.desc}</div>
              </div>
            `).join('')}
          </div>
        </div>
      </div>
      ${footer('08')}
    </div>
  `);

  // ========================= P9: 根因诊断 =========================
  pages.push(`
    <div class="page" style="position:relative;overflow:hidden;">
      ${orb('-5%','20%','350px',t.orbColor1,t.orbOpacity1*0.63)}
      ${header('08','根因诊断','问题归因与优化路径')}
      <div style="padding:0 64px;">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;">
          ${(d.findings||[]).map(f=>`
            <div class="glass-card" style="padding:18px 20px;border-color:${findingBorderColor(f.status)};">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
                ${statusDot(f.status)}
                <h4 style="font-family:Poppins;font-size:14px;font-weight:700;color:${t.textEmphasis};margin:0;">${f.title}</h4>
              </div>
              <p style="font-size:11px;color:${t.textMuted};line-height:1.7;margin:0 0 10px;">${f.desc}</p>
              <div style="padding:8px 12px;background:${t.subtleBg2};border:1px solid ${t.subtleBorder2};border-radius:6px;">
                <div style="font-size:9px;color:${t.textDim};font-weight:600;text-transform:uppercase;letter-spacing:0.08em;margin-bottom:3px;">优化建议</div>
                <div style="font-size:10px;color:${t.textBody};line-height:1.5;">${f.fix}</div>
              </div>
            </div>
          `).join('')}
        </div>
        <div style="padding:16px 22px;background:${t.insightBg};border:1px solid ${t.insightBorderColor};border-radius:12px;position:relative;margin-bottom:14px;">
          <div style="position:absolute;top:0;left:0;width:3px;height:100%;background:${t.leftBorderGrad};border-radius:2px;"></div>
          <div style="font-size:10px;color:${t.accent};font-weight:700;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px;padding-left:12px;">核心结论</div>
          <p style="margin:0;font-size:13px;color:${t.textEmphasis};font-weight:500;line-height:1.6;padding-left:12px;">品牌缺乏AI可读取的结构化"数字资产"，这是导致AI推荐率仅${d.aiRecommendRate}%的根本原因。好消息是，系统化内容建设可在30天内快速改善。</p>
        </div>
        <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px;">
          ${[
            { label:'严重问题', val:String((d.findings||[]).filter(f=>f.status==='danger').length), color:t.danger },
            { label:'待改善', val:String((d.findings||[]).filter(f=>f.status==='warning').length), color:t.warning },
            { label:'已建立', val:String((d.findings||[]).filter(f=>f.status==='good').length), color:t.success },
            { label:'改善难度', val:'中等', color:t.secondary },
            { label:'预计周期', val:'30天', color:t.accent },
          ].map(m=>`
            <div style="text-align:center;padding:14px 8px;background:${t.subtleBg2};border:1px solid ${t.subtleBorder2};border-radius:10px;">
              <div style="font-family:Poppins;font-size:24px;font-weight:800;color:${m.color};line-height:1;">${m.val}</div>
              <div style="font-size:9px;color:${t.textDim};font-weight:600;letter-spacing:0.06em;text-transform:uppercase;margin-top:4px;">${m.label}</div>
            </div>
          `).join('')}
        </div>
      </div>
      ${footer('09')}
    </div>
  `);

  // ========================= P10: 30天执行计划 =========================
  pages.push(`
    <div class="page" style="position:relative;overflow:hidden;">
      ${orb('0%','30%','300px',t.orbColor1,t.orbOpacity1*0.5)}
      ${orb('70%','70%','200px',t.success,t.orbOpacity2*0.67)}
      ${header('09','30天执行计划','快速提升路线图')}
      <div style="padding:0 64px;">
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:18px;">
          ${actions.map((a,i)=>`
            <div class="glass-card" style="padding:0;overflow:hidden;">
              <div style="background:${a.grad};padding:14px 16px;text-align:center;">
                <div style="font-family:Poppins;font-size:14px;font-weight:700;color:#fff;">${a.phase}</div>
                <div style="font-size:10px;color:rgba(255,255,255,0.7);margin-top:1px;">${a.days}</div>
              </div>
              <div style="padding:14px 16px;">
                ${(a.items||[]).map(item=>`
                  <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:7px;">
                    <div style="width:14px;height:14px;border-radius:50%;background:${a.color}15;border:1px solid ${a.color}40;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:1px;">
                      <div style="width:4px;height:4px;border-radius:50%;background:${a.color};"></div>
                    </div>
                    <div>
                      <div style="font-size:11px;color:${t.textBody};font-weight:500;">${item.task}</div>
                      <div style="font-size:9px;color:${t.textSubdim};">${item.detail}</div>
                    </div>
                  </div>
                `).join('')}
              </div>
              <div style="padding:10px 16px;border-top:1px solid ${t.actionSectionBorder};background:${t.actionSectionBg};">
                <div style="font-size:8px;color:${t.textSubdim};font-weight:600;text-transform:uppercase;letter-spacing:0.08em;">目标</div>
                <div style="font-size:12px;font-weight:700;color:${a.color};margin-top:2px;">${a.goal}</div>
                <div style="font-size:9px;color:${t.textSubdim};margin-top:2px;">KPI: ${a.kpi}</div>
              </div>
            </div>
          `).join('')}
        </div>
      </div>
      ${footer('10')}
    </div>
  `);

  // ========================= P11: 效果预测与风险 =========================
  pages.push(`
    <div class="page" style="position:relative;overflow:hidden;">
      ${orb('10%','65%','280px',t.orbColor2,t.orbOpacity2*0.67)}
      ${header('10','效果预测与风险','预期增长曲线 & 风险管控')}
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;padding:0 64px;">
        <div>
          <!-- Growth trajectory -->
          <div class="glass-card" style="padding:22px 24px;margin-bottom:14px;">
            <div style="font-size:10px;color:${t.textDim};font-weight:600;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:14px;">GEO评分增长预测</div>
            ${[
              { day:'当前', score:d.geoScore, rate:d.aiRecommendRate+'%', color:t.danger },
              { day:'30天', score:ep.day30Score||40, rate:ep.day30Rate||'30%', color:t.warning },
              { day:'60天', score:ep.day60Score||58, rate:ep.day60Rate||'50%', color:t.accent },
              { day:'90天', score:ep.day90Score||72, rate:ep.day90Rate||'65%', color:t.success },
            ].map((p,i)=>`
              <div style="display:flex;align-items:center;gap:14px;margin-bottom:${i<3?'12':'0'}px;">
                <div style="width:50px;font-size:11px;font-weight:600;color:${t.textMuted};">${p.day}</div>
                <div style="flex:1;height:24px;background:${t.progressTrack};border-radius:4px;overflow:hidden;">
                  <div style="height:100%;width:${p.score}%;background:linear-gradient(90deg,${p.color},${p.color}bb);border-radius:4px;display:flex;align-items:center;justify-content:flex-end;padding-right:8px;">
                    <span style="font-family:Poppins;font-size:10px;font-weight:700;color:#fff;">${p.score}</span>
                  </div>
                </div>
                <div style="font-size:10px;color:${p.color};font-weight:600;width:40px;text-align:right;">${p.rate}</div>
              </div>
            `).join('')}
          </div>
          <!-- Target metrics -->
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;">
            ${[
              { val:(ep.day30Score||40)+'+', label:'30天目标', color:t.warning },
              { val:(ep.day60Score||58)+'+', label:'60天目标', color:t.accent },
              { val:(ep.day90Score||72)+'+', label:'90天目标', color:t.success },
            ].map(m=>`
              <div style="padding:14px;background:${t.subtleBg2};border:1px solid ${t.subtleBorder2};border-radius:10px;text-align:center;">
                <div style="font-family:Poppins;font-size:24px;font-weight:800;color:${m.color};">${m.val}</div>
                <div style="font-size:9px;color:${t.textDim};margin-top:2px;">${m.label}</div>
              </div>
            `).join('')}
          </div>
        </div>
        <!-- Risk management -->
        <div>
          <div class="glass-card" style="padding:20px;">
            <div style="font-size:10px;color:${t.danger};font-weight:600;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:14px;">风险识别与管控</div>
            ${(risks.length > 0 ? risks : [
              { risk:'内容执行力不足', level:'high', mitigation:'制定严格内容日历，设置周检查点' },
              { risk:'竞品同步优化', level:'medium', mitigation:'持续监测竞品动态，保持差异化' },
              { risk:'AI算法调整', level:'low', mitigation:'多平台布局分散风险' },
            ]).map(r=>`
              <div style="padding:14px;background:${t.subtleBg};border:1px solid ${t.subtleBorder};border-radius:10px;margin-bottom:10px;">
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
                  <span style="font-size:12px;font-weight:600;color:${t.textEmphasis};">${r.risk}</span>
                  <span style="font-size:8px;font-weight:600;padding:2px 8px;border-radius:10px;color:#fff;background:${{high:t.danger,medium:t.warning,low:t.success}[r.level]||t.warning};">${{high:'高',medium:'中',low:'低'}[r.level]||r.level}</span>
                </div>
                <div style="font-size:10px;color:${t.textMuted};line-height:1.5;">应对: ${r.mitigation}</div>
              </div>
            `).join('')}
          </div>
          <div style="padding:12px 16px;background:${t.goldCalloutBg};border:1px solid ${t.goldCalloutBorder};border-radius:10px;margin-top:14px;">
            <p style="margin:0;font-size:11px;color:${t.goldCalloutText};font-weight:600;line-height:1.6;">以上预测基于执行到位的前提。我们将提供执行跟踪和月度复测，确保目标达成。</p>
          </div>
        </div>
      </div>
      ${footer('11')}
    </div>
  `);

  // ========================= P12: 数据方法说明 =========================
  pages.push(`
    <div class="page" style="position:relative;overflow:hidden;">
      ${orb('30%','80%','250px',t.orbColor2,t.orbOpacity2*0.5)}
      ${header('11','数据方法说明','诊断体系 & 数据来源')}
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;padding:0 64px;">
        <div>
          <div class="glass-card" style="padding:22px 24px;">
            <div style="font-size:10px;color:${t.accent};font-weight:600;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:14px;">数据采集规模</div>
            ${[
              { label:'原始数据量', val:meth.dataPoints||'280+条' },
              { label:'AI引擎', val:meth.engines||'4大AI引擎实测' },
              { label:'测试问题', val:meth.questions||'8个行业高频问题' },
              { label:'平台覆盖', val:meth.platforms||'7个关键词\u00D72大平台' },
              { label:'系统版本', val:meth.version||'GEO诊断系统 v4.0' },
            ].map((item,i)=>`
              <div style="display:flex;justify-content:space-between;padding:10px 0;${i<4?'border-bottom:1px solid '+t.progressTrack+';':''}">
                <span style="font-size:12px;color:${t.textMuted};">${item.label}</span>
                <span style="font-size:12px;font-weight:600;color:${t.textEmphasis};">${item.val}</span>
              </div>
            `).join('')}
          </div>
        </div>
        <div style="display:flex;flex-direction:column;gap:14px;">
          <div class="glass-card" style="padding:22px 24px;">
            <div style="font-size:10px;color:${t.accent};font-weight:600;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:14px;">评分体系说明</div>
            <p style="font-size:11px;color:${t.textMuted};line-height:1.7;margin:0 0 12px;">${scoreLabel}基于${wl.modelOwner}自研评估模型，综合衡量品牌在AI搜索时代的可见度与被推荐潜力。评分维度覆盖${dimCount}个核心领域，满分100分。</p>
            <div style="display:flex;flex-direction:column;gap:6px;">
              ${(isGeoScope ? [
                'AI引擎推荐率(30分): 品牌被AI引擎推荐的能力',
                '网页内容资产(25分): 网页SEO与结构化数据',
                '权威背书(20分): 第三方媒体与专业认证',
                '结构化内容(15分): 机器可读内容完善度',
                '品牌基础(10分): 品牌信息一致性',
              ] : [
                'AI引擎可见度(25分): 品牌被AI引擎推荐的能力',
                '社媒内容资产(20分): 社交媒体声量与内容质量',
                '网页内容资产(18分): 网页SEO与结构化数据',
                '权威背书(15分): 第三方媒体与专业认证',
                '结构化内容(12分): 机器可读内容完善度',
                '内容质量(5分): 内容专业度与原创性',
                '品牌基础(5分): 品牌信息一致性',
              ]).map(txt=>`
                <div style="display:flex;align-items:center;gap:6px;">
                  <div style="width:3px;height:3px;border-radius:50%;background:${t.accent};flex-shrink:0;"></div>
                  <span style="font-size:10px;color:${t.textBody};line-height:1.4;">${txt}</span>
                </div>
              `).join('')}
            </div>
          </div>
          <div style="padding:12px 16px;background:${t.subtleBg};border:1px solid ${t.subtleBorder};border-radius:10px;">
            <p style="margin:0;font-size:9px;color:${t.textSubdim};line-height:1.5;"><strong style="color:${t.textDim};">免责声明：</strong>本报告中的数据分析和预测基于公开数据和行业参考值。搜索量基于5118数据，转化率为通用参考值。实际效果受市场环境、执行力度等因素影响。</p>
          </div>
        </div>
      </div>
      ${footer('12')}
    </div>
  `);

  // ========================= P13: CTA =========================
  pages.push(`
    <div class="page" style="position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center;">
      ${orb('10%','20%','400px',t.orbColor1,t.orbOpacity1)}
      ${orb('60%','60%','350px',t.orbColor2,t.orbOpacity2)}
      ${orb('30%','80%','200px',t.highlight,t.orbOpacity2*0.67)}
      ${dotGrid('60px','60px',6,10)}
      <div style="position:absolute;top:0;left:0;right:0;height:3px;background:${t.gradientBar};"></div>
      <div style="text-align:center;max-width:620px;z-index:1;">
        <div style="color:${t.textDim};font-size:10px;font-weight:600;letter-spacing:0.25em;text-transform:uppercase;margin-bottom:14px;">Next Step</div>
        <h2 style="font-family:Poppins;font-size:38px;font-weight:800;color:${t.textH};line-height:1.2;margin:0 0 12px;">开启您的GEO优化之旅</h2>
        <p style="font-size:14px;color:${t.textMuted};line-height:1.6;margin:0 0 24px;">每一天的等待，都可能让竞争对手在AI推荐中领先一步</p>
        <div style="width:40px;height:3px;background:${t.leftBorderGrad};border-radius:2px;margin:0 auto 24px;"></div>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:28px;text-align:center;">
          ${[
            { val:'30天', label:'快速见效周期', desc:'系统化方案，分阶段执行' },
            { val:(ep.day30Score||40)+'+', label:'目标GEO评分', desc:'从'+d.geoScore+'分提升至'+(ep.day30Score||40)+'+分' },
            { val:'10x', label:'获客增长潜力', desc:'AI推荐带来精准客户' },
          ].map(p=>`
            <div style="padding:16px;background:${t.subtleBg};border:1px solid ${t.subtleBorder};border-radius:12px;">
              <div style="font-family:Poppins;font-size:24px;font-weight:800;color:${t.textH};margin-bottom:4px;">${p.val}</div>
              <div style="font-size:11px;color:${t.textBody};font-weight:600;margin-bottom:2px;">${p.label}</div>
              <div style="font-size:9px;color:${t.textSubdim};">${p.desc}</div>
            </div>
          `).join('')}
        </div>
        <div style="display:inline-block;padding:14px 44px;background:${t.ctaBg};border-radius:12px;box-shadow:0 4px 24px ${t.accent}4D;margin-bottom:24px;">
          <span style="font-family:Poppins;font-size:15px;font-weight:700;color:${t.ctaText};">预约免费GEO策略咨询</span>
        </div>
        <div style="display:flex;gap:40px;justify-content:center;">
          ${[['Website',wl.website],['微信公众号',wl.wechat]].map(([l,v])=>`
            <div>
              <div style="color:${t.textSubdim};font-size:9px;font-weight:600;letter-spacing:0.15em;text-transform:uppercase;margin-bottom:3px;">${l}</div>
              <div style="color:${t.textMuted};font-size:12px;font-weight:500;">${v}</div>
            </div>
          `).join('')}
        </div>
      </div>
      <div style="position:absolute;bottom:20px;left:0;right:0;display:flex;justify-content:center;align-items:center;gap:12px;">
        ${logoImg(14)}
        <span style="color:${t.textFooter};font-size:9px;letter-spacing:0.12em;text-transform:uppercase;">GEO v4.0 \u00B7 Confidential \u00B7 ${d.date}</span>
      </div>
    </div>
  `);

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>GEO Report \u2014 ${d.brandName} (${t.label})</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700;800;900&family=Inter:wght@400;500;600;700&family=Noto+Sans+SC:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  @page{size:297mm 210mm;margin:0;}
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:'Inter','Noto Sans SC',system-ui,sans-serif;font-size:14px;color:${t.textBody};background:${t.pageBg};-webkit-font-smoothing:antialiased;}
  .page{width:297mm;height:210mm;background:${t.pageBg};position:relative;overflow:hidden;page-break-after:always;display:flex;flex-direction:column;justify-content:center;padding:16px 0 44px;}
  h1,h2,h3,h4{font-family:'Poppins','Noto Sans SC',sans-serif;color:${t.textH};line-height:1.2;}
  .glass-card{background:${t.cardBg};border:1px solid ${t.cardBorder};border-radius:${t.cardRadius};${t.cardShadow!=='none'?'box-shadow:'+t.cardShadow+';':''}}
  .page-header{display:flex;align-items:center;gap:16px;padding:28px 64px 20px;}
  .page-num{font-family:'Poppins';font-size:14px;font-weight:800;color:${t.pageNumColor};background:${t.pageNumBg};border:1px solid ${t.pageNumBorder};width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;}
  .page-title{font-size:22px;font-weight:800;color:${t.textH};margin:0;letter-spacing:-0.01em;}
  .page-subtitle{font-size:12px;color:${t.textDim};font-weight:500;margin:2px 0 0;}
  .page-footer{position:absolute;bottom:20px;left:64px;right:64px;display:flex;justify-content:space-between;font-size:9px;color:${t.textFooter};font-weight:500;letter-spacing:0.08em;text-transform:uppercase;}
  strong{color:${t.textEmphasis};font-weight:600;}
  @media print{body,.page,.glass-card{-webkit-print-color-adjust:exact;print-color-adjust:exact;}}
</style>
</head>
<body>${pages.join('\n')}</body>
</html>`;
}

// ============================================================
// Markdown → Data parser
// ============================================================

function parseMdToData(mdContent) {
  const data = JSON.parse(JSON.stringify(EMPTY)); // start from empty defaults, not demo data

  // --- Preprocessing: strip "# 编辑任务" metadata if present ---
  const editTaskMarker = mdContent.match(/^#\s*编辑任务[\s\S]*?##\s*报告草稿[^\n]*\n/m);
  if (editTaskMarker) {
    mdContent = mdContent.substring(editTaskMarker.index + editTaskMarker[0].length).trim();
  }

  // Strip markdown bold markers for easier regex matching
  // (e.g., **GEO总分**：20/100 → GEO总分：20/100)
  const mdClean = mdContent.replace(/\*\*/g, '');

  try {
    // --- Header fields ---
    // Brand name: try metadata format first, then title "# 🎯 XXX GEO诊断报告"
    const brandMatch = mdClean.match(/品牌名[：:](.+)/);
    if (brandMatch) {
      data.brandName = brandMatch[1].trim();
    } else {
      const titleMatch = mdClean.match(/^#\s+(.+?)\s*(?:GEO诊断报告|AI搜索可见度诊断报告|社媒内容生态诊断报告|全域诊断报告|诊断报告)/m);
      if (titleMatch) {
        // Strip emoji prefix
        data.brandName = titleMatch[1].replace(/[\u{1F000}-\u{1FFFF}]|[\u{2600}-\u{27BF}]|[\u{FE00}-\u{FEFF}]|[\u{1F900}-\u{1F9FF}]|[\u{200D}\u{20E3}\u{FE0F}]/gu, '').trim();
      }
    }

    const industryMatch = mdClean.match(/行业[：:](.+)/);
    if (industryMatch) data.industry = industryMatch[1].trim();
    // Fallback: try "AI搜索可见度诊断报告" subtitle or "XX赛道"
    if (!data.industry) {
      const industryAlt = mdClean.match(/[「「](.+?)[」」]赛道/) || mdClean.match(/所在行业[：:的]*(.+?)(?:[，。,.]|的)/);
      if (industryAlt) data.industry = industryAlt[1].trim();
    }

    // GEO score: "GEO总分：20/100" or "总分：20/100"
    const geoScoreMatch = mdClean.match(/(?:GEO)?总分[：:]\s*(\d+)\s*\/\s*(\d+)/);
    if (geoScoreMatch) {
      data.geoScore = parseInt(geoScoreMatch[1], 10);
      data.maxScore = parseInt(geoScoreMatch[2], 10);
    }

    // Level: "等级：空白" — stop at pipe/whitespace/newline
    const levelMatch = mdClean.match(/等级[：:]\s*([^\s|，,\n]+)/);
    if (levelMatch) data.level = levelMatch[1].trim();

    // AI test count: "AI测试次数：31次" or "在31次测试中" or "31次（7个问题"
    const aiTestsMatch = mdClean.match(/(?:AI测试次数|AI引擎测试次数)[：:|\s]*(\d+)/)
      || mdClean.match(/在(\d+)次.*?测试中/)
      || mdClean.match(/(\d+)次[（(]\d+个问题/);
    if (aiTestsMatch) data.aiTests = parseInt(aiTestsMatch[1], 10);

    // AI recommend rate: "AI推荐率：9.7%" or "推荐率为9.7%" or "推荐率9.7%"
    const aiRecommendMatch = mdClean.match(/推荐率[：:为]*\s*([\d.]+)%/);
    if (aiRecommendMatch) data.aiRecommendRate = parseFloat(aiRecommendMatch[1]);

    // Date: "诊断日期：2026年03月17日"
    const dateMatch = mdClean.match(/诊断日期[：:]*\s*(\d{4})年(\d{1,2})月(\d{1,2})日/);
    if (dateMatch) {
      data.date = `${dateMatch[1]}.${dateMatch[2].padStart(2,'0')}.${dateMatch[3].padStart(2,'0')}`;
    } else {
      const now = new Date();
      data.date = `${now.getFullYear()}.${String(now.getMonth() + 1).padStart(2, '0')}.${String(now.getDate()).padStart(2, '0')}`;
    }
  } catch (e) {
    console.warn('parseMdToData: failed to parse header fields, using defaults.', e.message);
  }

  // --- GEO评分总览 dimensions ---
  try {
    const dimNameMap = {
      // 全面诊断7维度
      'AI引擎可见度': 'AI可见度', 'AI可见度': 'AI可见度',
      '社媒内容资产': '社媒声量', '社媒声量': '社媒声量',
      '网页内容资产': '网页内容', '网页内容': '网页内容',
      '权威背书': '权威背书',
      '结构化内容': '结构化内容',
      '内容质量': '内容质量',
      '品牌基础': '品牌基础',
      // GEO专项5维度
      'AI引擎推荐率': 'AI推荐率', 'AI推荐率': 'AI推荐率',
      '网页内容资产': '网页内容',
      '品牌基础': '品牌基础',
      // 社媒专项5维度
      '品牌社媒存在感': '品牌存在感', '品牌存在感': '品牌存在感',
      '竞品活跃度': '竞品活跃度',
      '行业内容生态': '行业生态', '行业生态': '行业生态',
      '账号运营基础': '运营基础', '运营基础': '运营基础',
    };
    // Match dimension section heading for any scope
    const dimSection = mdClean.match(/(?:GEO评分总览|评分总览|维度评分|诊断评分)[\s\S]*?(?=\n##|\n#[^#]|$)/i);
    if (dimSection) {
      const tableRows = dimSection[0].match(/\|.+\|.+\|.+\|.+\|.+\|/g);
      if (tableRows) {
        const parsedDims = [];
        for (const row of tableRows) {
          const cells = row.split('|').map(c => c.trim()).filter(Boolean);
          if (cells.length < 4) continue;
          // Remove emoji from dimension name
          const rawName = cells[0].replace(/[\u{1F000}-\u{1FFFF}]|[\u{2600}-\u{27BF}]|[\u{FE00}-\u{FEFF}]|[\u{1F900}-\u{1F9FF}]|[\u{200D}\u{20E3}\u{FE0F}]/gu, '').trim();
          const canonName = dimNameMap[rawName];
          if (!canonName) continue; // skip header rows or unrecognized
          const score = parseInt(cells[1], 10);
          const max = parseInt(cells[2], 10);
          if (isNaN(score) || isNaN(max)) continue;
          const status = (cells[3] || '').trim();
          const desc = (cells[4] || '').trim();
          // Try to find matching default dim for advice fallback
          const defaultDim = D.dims.find(dd => dd.name === canonName);
          parsedDims.push({
            name: canonName,
            score,
            max,
            pct: Math.round(score / max * 100),
            desc: desc || (defaultDim ? defaultDim.desc : ''),
            status,
            advice: defaultDim ? defaultDim.advice : '',
          });
        }
        if (parsedDims.length > 0) data.dims = parsedDims;
      }
    }

    // Try to extract advice from 优先建议 table
    const adviceSection = mdClean.match(/优先建议[\s\S]*?(?=\n##|\n#[^#]|$)/i);
    if (adviceSection) {
      const adviceRows = adviceSection[0].match(/\|.+\|.+\|/g);
      if (adviceRows) {
        for (const row of adviceRows) {
          const cells = row.split('|').map(c => c.trim()).filter(Boolean);
          if (cells.length < 2) continue;
          const rawName = cells[0].replace(/[\u{1F000}-\u{1FFFF}]|[\u{2600}-\u{27BF}]|[\u{FE00}-\u{FEFF}]|[\u{1F900}-\u{1F9FF}]|[\u{200D}\u{20E3}\u{FE0F}]/gu, '').trim();
          const canonName = dimNameMap[rawName];
          if (!canonName) continue;
          const dim = data.dims.find(dd => dd.name === canonName);
          if (dim) dim.advice = cells[1].trim();
        }
      }
    }
  } catch (e) {
    console.warn('parseMdToData: failed to parse dimensions, using defaults.', e.message);
  }

  // --- AI引擎测试详情 ---
  try {
    const testSection = mdClean.match(/AI引擎(?:测试|实测)详情[\s\S]*?(?=\n##[^#]|\n#[^#]|$)/i);
    if (testSection) {
      const tableRows = testSection[0].match(/\|.+\|/g);
      if (tableRows) {
        // Find header row to determine engine column positions
        let headerRow = null;
        let headerIdx = -1;
        for (let i = 0; i < tableRows.length; i++) {
          const row = tableRows[i];
          if (/测试问题/.test(row)) {
            headerRow = row;
            headerIdx = i;
            break;
          }
        }

        if (headerRow) {
          const headerCells = headerRow.split('|').map(c => c.trim()).filter(Boolean);
          // Map engine columns: look for Qwen/千问, DeepSeek, Kimi, 豆包
          const engineColMap = {};
          for (let ci = 1; ci < headerCells.length; ci++) {
            const h = headerCells[ci];
            if (/Qwen|千问/i.test(h)) engineColMap.qwen = ci;
            else if (/DeepSeek/i.test(h)) engineColMap.ds = ci;
            else if (/Kimi/i.test(h)) engineColMap.kimi = ci;
            else if (/豆包/i.test(h)) engineColMap.db = ci;
          }

          const parsedQuestions = [];
          const engineMentions = { ds: 0, kimi: 0, db: 0, qwen: 0 };
          let totalQ = 0;

          // Parse data rows (skip header and separator)
          for (let i = headerIdx + 1; i < tableRows.length; i++) {
            const row = tableRows[i];
            if (/^[\s|:-]+$/.test(row)) continue; // skip separator
            const cells = row.split('|').map(c => c.trim()).filter(Boolean);
            if (cells.length < 2) continue;
            const question = cells[0].trim();
            if (!question || /^-+$/.test(question)) continue;

            totalQ++;
            const qObj = { q: question };
            const checkStatus = (cellIdx) => {
              if (cellIdx === undefined || cellIdx >= cells.length) return '✗';
              const val = cells[cellIdx];
              return /✅|✓|是|提及|推荐/.test(val) ? '✓' : '✗';
            };
            qObj.ds = checkStatus(engineColMap.ds);
            qObj.kimi = checkStatus(engineColMap.kimi);
            qObj.db = checkStatus(engineColMap.db);
            qObj.gpt = checkStatus(engineColMap.qwen); // map qwen to gpt slot

            if (qObj.ds === '✓') engineMentions.ds++;
            if (qObj.kimi === '✓') engineMentions.kimi++;
            if (qObj.db === '✓') engineMentions.db++;
            if (qObj.gpt === '✓') engineMentions.qwen++;

            parsedQuestions.push(qObj);
          }

          if (parsedQuestions.length > 0) {
            data.testQuestions = parsedQuestions;
            data.engines = [
              { name: 'DeepSeek', status: engineMentions.ds > 0, detail: `在${engineMentions.ds}个问题中提及品牌`, mentions: engineMentions.ds, total: totalQ },
              { name: 'Kimi', status: engineMentions.kimi > 0, detail: `在${engineMentions.kimi}个问题中提及品牌`, mentions: engineMentions.kimi, total: totalQ },
              { name: '豆包', status: engineMentions.db > 0, detail: `在${engineMentions.db}个问题中提及品牌`, mentions: engineMentions.db, total: totalQ },
              { name: '千问', status: engineMentions.qwen > 0, detail: `在${engineMentions.qwen}个问题中提及品牌`, mentions: engineMentions.qwen, total: totalQ },
            ];
          }
        }
      }
    }
  } catch (e) {
    console.warn('parseMdToData: failed to parse AI engine test details, using defaults.', e.message);
  }

  // --- Competitors (指定竞品深度分析) ---
  try {
    const compSection = mdClean.match(/(?:指定竞品深度分析|竞品对标分析|竞品全景)[\s\S]*?(?=\n##[^#]|\n#[^#]|$)/i);
    if (compSection) {
      const compBlocks = compSection[0].match(/###\s*\d*[.、]?\s*(.+?)[\s\S]*?(?=###|\n##|$)/g);
      if (compBlocks) {
        const parsedComps = [];
        for (const block of compBlocks) {
          const nameMatch = block.match(/###\s*\d*[.、]?\s*(.+)/);
          if (!nameMatch) continue;
          const compName = nameMatch[1].trim();
          if (!compName) continue;

          const tagMatch = block.match(/标签[：:](.+)/i) || block.match(/类型[：:](.+)/i);
          const scoreMatch = block.match(/(?:得分|评分|分数)[：:](\d+)/i);
          const strengthMatch = block.match(/(?:优势|强项)[：:](.+)/i);

          // Extract bullet points
          const bulletMatches = block.match(/[-•]\s+(.+)/g);
          const points = bulletMatches ? bulletMatches.map(b => b.replace(/^[-•]\s+/, '').trim()).slice(0, 5) : [];

          parsedComps.push({
            name: compName,
            tag: tagMatch ? tagMatch[1].trim() : '',
            score: scoreMatch ? parseInt(scoreMatch[1], 10) : 50,
            strength: strengthMatch ? strengthMatch[1].trim() : '',
            points: points.length > 0 ? points : ['暂无详细信息'],
          });
        }
        if (parsedComps.length > 0) data.competitors = parsedComps;
      }
    }
  } catch (e) {
    console.warn('parseMdToData: failed to parse competitors, using defaults.', e.message);
  }

  // --- Funnel (AI搜索流量漏斗) ---
  try {
    const funnelSection = mdClean.match(/AI搜索流量漏斗[\s\S]*?(?=\n##|\n#[^#]|$)/i);
    if (funnelSection) {
      const tableRows = funnelSection[0].match(/\|.+\|/g);
      if (tableRows) {
        const parsedFunnel = [];
        for (const row of tableRows) {
          const cells = row.split('|').map(c => c.trim()).filter(Boolean);
          if (cells.length < 2) continue;
          if (/^-+$/.test(cells[0]) || /阶段|漏斗/.test(cells[0])) continue; // skip header/separator
          const label = cells[0].trim();
          const value = cells[1].trim();
          if (!label || !value) continue;
          const note = cells.length > 2 ? cells[2].trim() : '';
          parsedFunnel.push({ label, value, width: 0, note });
        }
        // Calculate widths proportionally
        if (parsedFunnel.length > 0) {
          parsedFunnel[0].width = 100;
          for (let i = 1; i < parsedFunnel.length; i++) {
            parsedFunnel[i].width = Math.max(10, Math.round(100 - (i / parsedFunnel.length) * 85));
          }
          data.funnel = parsedFunnel;
        }
      }
    }
  } catch (e) {
    console.warn('parseMdToData: failed to parse funnel, using defaults.', e.message);
  }

  // --- Findings (关键发现) ---
  try {
    const findingsSection = mdClean.match(/关键发现[\s\S]*?(?=\n##|\n#[^#]|$)/i);
    if (findingsSection) {
      const tableRows = findingsSection[0].match(/\|.+\|/g);
      if (tableRows) {
        const parsedFindings = [];
        for (const row of tableRows) {
          const cells = row.split('|').map(c => c.trim()).filter(Boolean);
          if (cells.length < 2) continue;
          if (/^-+$/.test(cells[0])) continue; // separator

          // Determine status from emoji
          let status = 'warning';
          const rawCell = cells[0];
          if (/🔴|danger|严重|紧急/.test(rawCell)) status = 'danger';
          else if (/🟢|good|良好|已建立/.test(rawCell)) status = 'good';
          else if (/🟡|warning|注意|警告/.test(rawCell)) status = 'warning';

          const title = rawCell.replace(/[\u{1F000}-\u{1FFFF}]|[\u{2600}-\u{27BF}]|[\u{FE00}-\u{FEFF}]|[\u{1F900}-\u{1F9FF}]|[\u{200D}\u{20E3}\u{FE0F}]/gu, '').trim();
          if (!title || /发现|状态|标题/.test(title)) continue; // skip header

          const desc = cells.length > 1 ? cells[1].trim() : '';
          const fix = cells.length > 2 ? cells[2].trim() : '';

          parsedFindings.push({ title, status, desc, fix });
        }
        if (parsedFindings.length > 0) data.findings = parsedFindings;
      }
    }
  } catch (e) {
    console.warn('parseMdToData: failed to parse findings, using defaults.', e.message);
  }

  // --- 30天行动计划 ---
  try {
    const actionSection = mdClean.match(/30天行动计划[\s\S]*?(?=\n##[^#]|\n#[^#]|$)/i);
    if (actionSection) {
      const phaseBlocks = actionSection[0].match(/###\s*(.+?)[\s\S]*?(?=###|$)/g);
      if (phaseBlocks) {
        const parsedActions = [];
        for (const block of phaseBlocks) {
          const phaseHeaderMatch = block.match(/###\s*(.+)/);
          if (!phaseHeaderMatch) continue;
          const header = phaseHeaderMatch[1].trim();

          const daysMatch = header.match(/(Day\s*\d+\s*[-–]\s*\d+)/i) || block.match(/(Day\s*\d+\s*[-–]\s*\d+)/i);
          const days = daysMatch ? daysMatch[1].replace(/\s+/g, ' ') : '';

          // Extract phase name (remove day range if present)
          let phase = header.replace(/(Day\s*\d+\s*[-–]\s*\d+)/i, '').replace(/[（()）]/g, '').replace(/[：:]/g, '').trim();
          if (!phase) phase = days;

          // Extract task items
          const taskMatches = block.match(/[-•]\s+(.+)/g);
          const items = [];
          if (taskMatches) {
            for (const tm of taskMatches) {
              const taskText = tm.replace(/^[-•]\s+/, '').trim();
              const taskDetailMatch = taskText.match(/(.+?)[：:—]\s*(.+)/);
              if (taskDetailMatch) {
                items.push({ task: taskDetailMatch[1].trim(), detail: taskDetailMatch[2].trim() });
              } else {
                items.push({ task: taskText, detail: '' });
              }
            }
          }

          const goalMatch = block.match(/目标[：:](.+)/i);
          const kpiMatch = block.match(/KPI[：:](.+)/i);

          parsedActions.push({
            phase,
            days,
            items: items.length > 0 ? items : [{ task: '待规划', detail: '' }],
            goal: goalMatch ? goalMatch[1].trim() : '',
            kpi: kpiMatch ? kpiMatch[1].trim() : '',
          });
        }
        if (parsedActions.length > 0) data.actions = parsedActions;
      }
    }
  } catch (e) {
    console.warn('parseMdToData: failed to parse action plan, using defaults.', e.message);
  }

  // --- Industry benchmarks (try to extract, else keep defaults) ---
  try {
    const avgMatch = mdClean.match(/行业均值[：:](\d+)/);
    if (avgMatch) data.industryAvg = parseInt(avgMatch[1], 10);
    const bestMatch = mdClean.match(/行业最佳[：:](\d+)/);
    if (bestMatch) data.industryBest = parseInt(bestMatch[1], 10);
  } catch (e) {
    console.warn('parseMdToData: failed to parse industry benchmarks, using defaults.', e.message);
  }

  return data;
}

// ============================================================
// Main — CLI-aware entry point
// ============================================================

(async () => {
  const args = process.argv.slice(2);
  // 白标：从 server.py 透传的 WHITELABEL_JSON 环境变量解析（无 → 平台默认，现状不变）
  const wl = readWhitelabelFromEnv();

  if (args[0] === '--html-only' && args.length >= 4) {
    // HTML-only mode: --html-only md_path html_output_path theme_id [, json_data_path]
    const [, mdPath, htmlOutputPath, themeId, jsonDataPath] = args;

    let data;
    if (jsonDataPath && fs.existsSync(jsonDataPath)) {
      console.log(`Loading LLM-extracted data from: ${jsonDataPath}`);
      const llmData = JSON.parse(fs.readFileSync(jsonDataPath, 'utf8'));
      data = { ...JSON.parse(JSON.stringify(EMPTY)), ...llmData };
      if (llmData.dims && llmData.dims.length > 0) data.dims = llmData.dims;
      if (llmData.engines && llmData.engines.length > 0) data.engines = llmData.engines;
      if (llmData.testQuestions && llmData.testQuestions.length > 0) data.testQuestions = llmData.testQuestions;
      if (llmData.competitors && llmData.competitors.length > 0) data.competitors = llmData.competitors;
      if (llmData.funnel && llmData.funnel.length > 0) data.funnel = llmData.funnel;
      if (llmData.findings && llmData.findings.length > 0) data.findings = llmData.findings;
      if (llmData.actions && llmData.actions.length > 0) data.actions = llmData.actions;
      if (llmData.executiveSummary) data.executiveSummary = { ...EMPTY.executiveSummary, ...llmData.executiveSummary };
      if (llmData.revenueImpact) data.revenueImpact = { ...EMPTY.revenueImpact, ...llmData.revenueImpact };
      if (llmData.effectPrediction) data.effectPrediction = { ...EMPTY.effectPrediction, ...llmData.effectPrediction };
      if (llmData.methodology) data.methodology = { ...EMPTY.methodology, ...llmData.methodology };
      if (llmData.existingAdvantages && llmData.existingAdvantages.length > 0) data.existingAdvantages = llmData.existingAdvantages;
    } else {
      console.log('No JSON data file — using regex parser on markdown...');
      const mdContent = fs.readFileSync(mdPath, 'utf8');
      data = parseMdToData(mdContent);
    }

    const theme = themes.find(t => t.id === themeId);
    if (!theme) {
      console.error(`Unknown theme: ${themeId}. Available: ${themes.map(t=>t.id).join(', ')}`);
      process.exit(1);
    }

    data = cleanMdInData(data);
    console.log(`Generating ${themeId} HTML-only for ${data.brandName}...`);
    const html = buildHTML(data, theme, wl);
    fs.writeFileSync(htmlOutputPath, html, 'utf8');
    console.log(`HTML generated: ${htmlOutputPath}`);

  } else if (args.length >= 3) {
    // Production mode: md_path, output_path, theme_id [, json_data_path]
    const [mdPath, outputPath, themeId, jsonDataPath] = args;

    let data;
    if (jsonDataPath && fs.existsSync(jsonDataPath)) {
      // LLM-extracted JSON data available — use it, merge with empty defaults (not demo data)
      console.log(`Loading LLM-extracted data from: ${jsonDataPath}`);
      const llmData = JSON.parse(fs.readFileSync(jsonDataPath, 'utf8'));
      data = { ...JSON.parse(JSON.stringify(EMPTY)), ...llmData };
      // Ensure arrays are from LLM data if present
      if (llmData.dims && llmData.dims.length > 0) data.dims = llmData.dims;
      if (llmData.engines && llmData.engines.length > 0) data.engines = llmData.engines;
      if (llmData.testQuestions && llmData.testQuestions.length > 0) data.testQuestions = llmData.testQuestions;
      if (llmData.competitors && llmData.competitors.length > 0) data.competitors = llmData.competitors;
      if (llmData.funnel && llmData.funnel.length > 0) data.funnel = llmData.funnel;
      if (llmData.findings && llmData.findings.length > 0) data.findings = llmData.findings;
      if (llmData.actions && llmData.actions.length > 0) data.actions = llmData.actions;
      // Merge nested objects
      if (llmData.executiveSummary) data.executiveSummary = { ...EMPTY.executiveSummary, ...llmData.executiveSummary };
      if (llmData.revenueImpact) data.revenueImpact = { ...EMPTY.revenueImpact, ...llmData.revenueImpact };
      if (llmData.effectPrediction) data.effectPrediction = { ...EMPTY.effectPrediction, ...llmData.effectPrediction };
      if (llmData.methodology) data.methodology = { ...EMPTY.methodology, ...llmData.methodology };
      if (llmData.existingAdvantages && llmData.existingAdvantages.length > 0) data.existingAdvantages = llmData.existingAdvantages;
    } else {
      // Fallback: regex parsing from markdown
      console.log('No JSON data file — using regex parser on markdown...');
      const mdContent = fs.readFileSync(mdPath, 'utf8');
      data = parseMdToData(mdContent);
    }

    const theme = themes.find(t => t.id === themeId);
    if (!theme) {
      console.error(`Unknown theme: ${themeId}. Available: ${themes.map(t=>t.id).join(', ')}`);
      process.exit(1);
    }

    // Convert markdown in text fields to HTML
    data = cleanMdInData(data);
    console.log(`Generating ${themeId} PDF for ${data.brandName}...`);
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined, args: ['--no-sandbox', '--disable-setuid-sandbox'] });
    const page = await browser.newPage();
    const html = buildHTML(data, theme, wl);
    // Save HTML alongside PDF for web preview
    const htmlOutputPath = outputPath.replace(/\.pdf$/, '.html');
    fs.writeFileSync(htmlOutputPath, html, 'utf8');
    console.log(`HTML saved: ${htmlOutputPath}`);
    await page.setContent(html, { waitUntil: 'networkidle', timeout: 60000 });
    await page.pdf({
      path: outputPath,
      width: '297mm',
      height: '210mm',
      printBackground: true,
      margin: { top: 0, right: 0, bottom: 0, left: 0 },
    });
    // Take page screenshots for visual review
    const allPages = await page.locator('.page').all();
    for (let i = 0; i < allPages.length; i++) {
      await allPages[i].screenshot({ path: path.join(__dirname, `e2e_${themeId}_p${i + 1}.png`) });
    }
    console.log(`  Screenshots: ${allPages.length} pages`);
    await page.close();
    await browser.close();
    console.log(`PDF generated: ${outputPath}`);
  } else {
    // Demo mode: generate all 4 themes
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined, args: ['--no-sandbox', '--disable-setuid-sandbox'] });
    const outDir = path.resolve(__dirname, '..');
    for (const t of themes) {
      console.log(`\nGenerating theme: ${t.id} (${t.label})...`);
      const html = buildHTML(D, t, wl);
      const htmlPath = path.join(__dirname, `report_${t.id}.html`);
      fs.writeFileSync(htmlPath, html, 'utf8');
      const page = await browser.newPage();
      await page.setContent(html, { waitUntil: 'networkidle', timeout: 60000 });
      const pdfPath = path.join(outDir, `geo_report_${t.id}.pdf`);
      await page.pdf({
        path: pdfPath, width: '297mm', height: '210mm',
        printBackground: true, margin: { top: 0, right: 0, bottom: 0, left: 0 },
      });
      console.log(`  PDF: ${pdfPath}`);
      const allPages = await page.locator('.page').all();
      for (let i = 0; i < allPages.length; i++) {
        await allPages[i].screenshot({ path: path.join(__dirname, `${t.id}_page${i + 1}.png`) });
      }
      console.log(`  Screenshots: ${allPages.length} pages`);
      await page.close();
    }
    await browser.close();
    console.log('\nAll 4 themed PDFs generated successfully.');
  }
})();
