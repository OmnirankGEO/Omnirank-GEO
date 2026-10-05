/**
 * 沙盒预设数据 (Sandbox Mode)
 * Stage 1 Batch 4 (2026-05-18)
 *
 * 设计要点 (2026-05-18 走线上真实诊断后的最终版):
 *   - 沙盒 demo 客户 = 一路顺风出行服务 (brand_id 固定 111)
 *   - 真实诊断 id=311 在生产环境跑出: 26 分危急级 / 32 次 AI 测试 7 命中 / 21.9% 引用率
 *   - report_v2_client_md 36KB 直接落地 ./data/demoReport.md, vite ?raw 导入
 *   - 4 步教学结构，覆盖诊断、报价、写作发布、监测
 *
 * 4 步 教学对应接口:
 *   step 1 first_diagnosis        POST /api/diagnosis/start  (api · 走 axios)
 *                                 GET  /api/diagnosis/session/{sid}/status (fetch)
 *                                 GET  /api/diagnosis/{id}             (api)
 *                                 GET  /api/diagnosis/{id}/content     (api)
 *   step 2 first_quote            POST /api/quotes/... + /api/s/...
 *   step 3 first_article_publish  POST /api/articles/generate + /api/publish/...
 *   step 4 first_monitoring       POST /api/monitoring/run-stream + cell retry
 */
/* [SYNTHETIC-DEMO] 沙盒演示数据一律合成:品牌/城市/竞品全部虚构,不得放真实客户信息。 */
import demoReportMd from './data/demoReport.md?raw';
import {
    SANDBOX_LAGGARD_KEYWORD,
    SANDBOX_MAGIC_KEYWORDS,
    SANDBOX_OPT_KEYWORD,
} from './constants';
export { SANDBOX_LAGGARD_KEYWORD, SANDBOX_MAGIC_KEYWORDS, SANDBOX_OPT_KEYWORD } from './constants';

/** 沙盒固定 ID (跟真实生产数据对齐 · 一路顺风出行服务 prod id=311) */
export const SANDBOX_BRAND_ID = 111;
export const SANDBOX_DIAGNOSIS_ID = 311;
export const SANDBOX_QUOTE_ID = 'Q-SANDBOX-YILU';
export const SANDBOX_SESSION_ID = '一路顺风出行服务_sandbox_demo';

function nowIso(): string {
    return new Date().toISOString();
}

// ==========================================
// 沙盒内部进度态 (内存 · reload 即重置)
// ==========================================
export const sandboxProgress = {
    diagnosisStarted: false, // step 1
    quoteCreated: false,     // step 2
    articleWritten: false,   // step 3a
    published: false,        // step 3b
    monitoringSet: false,    // step 4
};

// ==========================================
// 客户预设数据 · 一路顺风出行服务 (生产 brand_id=111 · diagnosis id=311)
// ==========================================

const DEMO_BRAND_NAME = '一路顺风出行服务';
// 注: 措辞避开"租赁"二字 · 全站行业分类器 normalizeIndustry 会把"租赁"先归到"房产物业"
// (在"汽车出行"之前命中)· 用"用车/专车"既准确又能正确归类到汽车出行
const DEMO_INDUSTRY = '高端商务用车 / 豪华专车服务';
const DEMO_INDUSTRY_FULL = '互联网和相关服务 / 高端商务出行、企业用车服务';
const DEMO_CITY = '深圳';

const SANDBOX_BRAND_BASE = {
    id: SANDBOX_BRAND_ID,
    brand_code: 'BRD-YILU-DEMO',
    name: DEMO_BRAND_NAME,
    company_name: '云舟出行服务有限公司',
    industry: DEMO_INDUSTRY,
    industry_category: '汽车出行',
    cities: DEMO_CITY,
    contact: '张经理',
    notes: '教程模式 · 演示客户',
    status: 'active' as const,
    brand_status: 'active' as const,
    social_enabled: false,
    diagnosis_count: 1,
    latest_score: 26 as number | undefined,
    latest_diagnosis_id: SANDBOX_DIAGNOSIS_ID as number | null | undefined,
    quote_count: 0,
    quote_status: null,
    has_profile: true,
    completeness: 85,
    is_test: false,
    created_at: nowIso(),
    updated_at: nowIso(),
};

/**
 * Step 3/4 用真实素材的"魔法词"(2 个核心词) · 已有上榜成功视频, 教学时演示用
 * - 它们是合同 3 个词里的 2 个"核心词"(另 1 个是长尾词 SANDBOX_LAGGARD_KEYWORD)
 * - step4 监测里这 2 个达标(呼应视频), 长尾词未达标 → 补发救回
 * - 完整签约词集见 SANDBOX_CONTRACT_KEYWORDS · 报价/写作/监测都基于那 3 个
 */
/** 第 3 个合同词 · 偏长尾、起量慢 · step4 监测里它就是"未达标 → 补发救回"的那个
 *  跟 2 个核心魔法词一起构成"客户实际签约的 3 个词", 让报价/文章/监测全程对得上 */
/** 客户最终签约的 3 个词 (2 核心 + 1 长尾) · 报价/选词/写作/监测全程一致 */
export const SANDBOX_CONTRACT_KEYWORDS = [...SANDBOX_MAGIC_KEYWORDS, SANDBOX_LAGGARD_KEYWORD];

/** 候选词全集 · 20 个明确购买/比较/选型意图问题，供报价选择与 AI 扩词使用。 */
export const SANDBOX_ALL_CANDIDATE_KEYWORDS = [
    '深圳企业商务用车服务',
    '跨境电商专车接送',
    '外资企业差旅出行解决方案',
    '高端商务接送机服务商',
    '企业年度包车服务',
    '隐私安全商务出行服务',
    '全国跨城商务联运服务',
    '深圳商务接待用车哪家靠谱',
    '深圳公司长期包车怎么选',
    '深圳会议接送车队推荐',
    '深圳机场商务接送多少钱',
    '深圳埃尔法租车公司对比',
    '深圳外宾接待用车服务商',
    '深圳企业月结用车哪家好',
    '深圳商务车带司机价格',
    '深圳跨城包车公司推荐',
    '深圳高端接待车队性价比',
    ...SANDBOX_MAGIC_KEYWORDS,
    SANDBOX_LAGGARD_KEYWORD,
];

/** 沙盒 demo 客户预填表单 (诊断表单自动填用) */
export const SANDBOX_DEMO_PRESET = {
    brandName: DEMO_BRAND_NAME,
    industry: DEMO_INDUSTRY,
    city: DEMO_CITY,
    keywords: SANDBOX_ALL_CANDIDATE_KEYWORDS.join('\n'),
};

// ==========================================
// 客户档案系 (已不再教 step "录入客户" + "补全档案", 仅作 fallback 不让 UI 崩)
// ==========================================

export function getSandboxMyClientsList() {
    return { success: true, clients: [SANDBOX_BRAND_BASE], total: 1 };
}

export function getSandboxMyClientsCreate() {
    return {
        success: true,
        brand_id: SANDBOX_BRAND_ID,
        brand: SANDBOX_BRAND_BASE,
        message: '客户添加成功 (教程模式)',
    };
}

export function getSandboxMyClientDetail() {
    const profile = {
        business: '一路顺风出行服务专注高端商务出行 · 主要服务深圳地区跨境电商企业 / 外资公司 / 高管商务接送场景 · 提供接送机 / 包车 / 跨城联运 / 隐私安全出行解决方案',
        target_users: '深圳跨境电商企业 + 外资企业差旅负责人 + 高净值商务人士',
        persona_positioning: '高端商务出行专家 · 隐私安全 + 准时可靠',
        persona_tone: '专业 / 沉稳 / 服务感强 / 不张扬',
        company_intro: '云舟出行服务有限公司专注高端商务用车 · 服务深圳地区跨境电商 / 外资企业差旅需求 · 主打隐私安全 / 准时可靠 / 一站式跨城联运.',
        core_value: '让每一次商务出行都体面准时',
        selling_points: '专业商务车型 / 司机背调认证 / 隐私协议保护 / 全国跨城联运 / 企业月结发票',
        success_cases: '服务深圳超 200 家外资企业 / 跨境电商公司 · 月均订单 5000+ 单 · 客户复购率 85%+',
        testimonials: '"出差从来不操心车的问题, 准时干净有礼貌" — 某跨境电商运营总监',
        products: JSON.stringify(['企业商务接送机', '日常商务用车', '跨城联运专车', '企业年度包车', '高管私人车队', '会议活动用车']),
        pain_points: JSON.stringify(['客户搜行业词时 AI 不推荐我们', '同行恶意压价 / 资质参差不齐', '外资客户对司机背调要求高 / 准入门槛高']),
        competitors: JSON.stringify(['滴滴企业版', '神州专车', '首汽约车', '曹操企业版', '本地大巴租赁公司']),
    };
    return {
        success: true,
        brand: SANDBOX_BRAND_BASE,
        profile,
        clients: [SANDBOX_BRAND_BASE],
    };
}

export function getSandboxAiFillResponse() {
    return {
        success: true,
        data: {
            brand_name: DEMO_BRAND_NAME,
            industry: DEMO_INDUSTRY,
            city: DEMO_CITY,
            business: '一路顺风出行服务专注高端商务出行 · 主要服务深圳地区跨境电商企业 / 外资公司 / 高管商务接送场景.',
            target_users: '深圳跨境电商 + 外资企业差旅负责人 + 高净值商务人士',
            persona_positioning: '高端商务出行专家 · 隐私安全 + 准时可靠',
            persona_tone: '专业 / 沉稳 / 服务感强',
            products: ['企业商务接送机', '日常商务用车', '跨城联运专车', '企业年度包车', '会议活动用车'],
            pain_points: ['客户搜行业词 AI 不推荐我们', '同行压价资质参差', '外资客户准入门槛高'],
            competitors: ['滴滴企业版', '神州专车', '首汽约车', '曹操企业版'],
            company_intro: '云舟出行服务有限公司专注高端商务用车 · 服务深圳跨境电商 / 外资企业差旅 · 月均订单 5000+.',
            core_value: '让每一次商务出行都体面准时',
            selling_points: '专业车型 + 司机背调 + 隐私协议 + 跨城联运 + 企业月结',
            success_cases: '服务深圳超 200 家外资企业 · 客户复购率 85%+',
            testimonials: '"出差从来不操心车的问题" — 跨境电商运营总监',
            differentiation: '司机背调认证 + 隐私协议 + 一站式跨城联运 (vs 一般打车软件)',
            content_direction: '企业用车合规 + 司机管理 + 客户案例 + 行业知识科普',
            service_scope: 'local',
            business_type: 'B2B',
            city_scope: 'national',
            local_competitors: ['滴滴企业版深圳', '首汽约车深圳', '神州深圳'],
            market_insight: {
                authority_sources: ['官网', '客户案例', '行业协会'],
                hot_formats: ['客户访谈', '司机背景介绍', '企业用车指南'],
                my_differentiation: '高端商务 + 司机背调 + 隐私协议',
            },
            _confidences: {
                business: 'strong',
                target_users: 'strong',
                company_intro: 'strong',
            } as Record<string, 'strong' | 'medium' | 'weak'>,
        },
    };
}

export function getSandboxBrandSaveResponse() {
    return { success: true, message: '保存成功 (教程模式)' };
}

/** POST /api/diagnosis/autofill (api · 诊断页 AI 填写按钮)
 *  返预设一路顺风出行数据 (industry / keywords / clientLocation / additionalInfo / competitors) */
export function getSandboxDiagnosisAutofillResponse() {
    return {
        success: true,
        provider: 'sandbox',
        data: {
            industry: DEMO_INDUSTRY,
            keywords: SANDBOX_ALL_CANDIDATE_KEYWORDS,
            clientLocation: '深圳 (全国跨城联运)',
            additionalInfo: '云舟出行服务有限公司专注深圳地区高端商务用车 · 服务跨境电商 / 外资企业差旅场景 · 主打司机背调 + 隐私协议 + 全国跨城联运 + 企业月结发票.',
            competitors: ['滴滴企业版', '神州专车', '首汽约车', '曹操企业版'],
        },
    };
}

export function getSandboxM3Customers() {
    return {
        success: true,
        customers: [{
            id: SANDBOX_BRAND_ID,
            name: SANDBOX_BRAND_BASE.name,
            brand_code: SANDBOX_BRAND_BASE.brand_code,
            industry: SANDBOX_BRAND_BASE.industry,
            industry_category: SANDBOX_BRAND_BASE.industry_category,
            cities: SANDBOX_BRAND_BASE.cities,
            diagnosis_count: sandboxProgress.diagnosisStarted ? 1 : 0,
            latest_score: sandboxProgress.diagnosisStarted ? 94 : null,
            latest_diagnosis_id: sandboxProgress.diagnosisStarted ? SANDBOX_DIAGNOSIS_ID : null,
            latest_quote: null,
            created_at: SANDBOX_BRAND_BASE.created_at,
            is_test: false,
        }],
    };
}

export function getSandboxBrandCompleteness() {
    return { success: true, score: 85, breakdown: {} };
}

export function getSandboxMyBrand() {
    return getSandboxMyClientDetail();
}

export function getSandboxClientContextList() {
    return { success: true, clients: [SANDBOX_BRAND_BASE] };
}

export function getSandboxClientContextDetail() {
    const detail = getSandboxMyClientDetail();
    return {
        success: true,
        context: {
            brand: detail.brand,
            profile: detail.profile,
            materials: null,
            relatedQuoteIds: [] as string[],
            socialProjects: [] as Array<{ id: number; name: string }>,
        },
    };
}

export function getSandboxBrandList() { return { data: [SANDBOX_BRAND_BASE] }; }
export function getSandboxBrandDetail() { return { data: SANDBOX_BRAND_BASE }; }

// ==========================================
// Step 1: 启动诊断 · 用真实生产 id=311 报告 (一路顺风出行 · 26 分危急级)
// ==========================================

/** 沙盒诊断启动时间 (内存) · 用于 status 轮询按 elapsed 推进度 */
let sandboxDiagStartTs: number | null = null;

/**
 * [#176 · 2026-09-12] 教程沙盒的启动页取价。
 *
 * 🔴 **为什么非有不可**:诊断启动页自 09-05 起「价只从服务端取」——
 *    POST /api/pricing/diagnosis-preview 拿不到 points + pricePreviewId 就
 *    **闸死「开始品牌体检」**。而沙盒拦截器没有这条规则 ⇒ 本地 501 ⇒
 *    客户端把它折成「这次没能算出价格」⇒ 教程走到这一步就死。
 *    Owner 09-12 现场演示正卡在这里。
 *
 * 🔴 `questionCount` **必须回显请求里的数**,不能写死:客户端
 *    `priceMatchesInput` 比的就是 `p.questionCount === ownQuestions.unique.length`,
 *    写死任何值都会在用户增删题目时立刻对不上,按钮重新闸死 —— 而且那一次
 *    屏幕上只会说「价格已过期」,看不出是 fixture 在骗人。
 *
 * 🔴 下面这三个数是**教程常量,不是价目**。不 import 生产计价常量
 *    (「前端不算钱」那条是对真钱说的;沙盒 fixture 允许自带数字,
 *     但别让它长成第二份计价规则)。
 */
const SANDBOX_PRICE_BASE = 650;
const SANDBOX_PRICE_FREE_QUESTIONS = 0;
const SANDBOX_PRICE_PER_EXTRA_QUESTION = 100;

export function buildSandboxDiagnosisPricePreview(config: { data?: unknown }) {
    const body = (config?.data || {}) as {
        questions?: unknown;
        aiOptimizeCustom?: unknown;
        mode?: unknown;
    };
    // 去重 + 去空白,与客户端 `collectOwnQuestions` 的口径一致(它比的是 unique 数)
    const raw = Array.isArray(body.questions) ? body.questions : [];
    const uniq = new Set<string>();
    for (const q of raw) {
        if (typeof q !== 'string') continue;
        const t = q.trim();
        if (t) uniq.add(t);
    }
    const questionCount = uniq.size;
    const extra = Math.max(0, questionCount - SANDBOX_PRICE_FREE_QUESTIONS)
        * SANDBOX_PRICE_PER_EXTRA_QUESTION;
    const points = SANDBOX_PRICE_BASE + extra;
    // 前缀 sandbox- 让任何看到这个 id 的人一眼知道它不是真的 price_preview
    const pricePreviewId = `sandbox-preview-${questionCount}-${body.aiOptimizeCustom ? 1 : 0}`;
    // 🔴 顶层返(后端就是顶层,没有 data 包)。客户端 `body?.data ?? body` 两种都认,照后端来。
    return {
        questionCount,
        mode: typeof body.mode === 'string' ? body.mode : null,
        points,
        estimate: points,
        pricePreviewId,
        breakdown: {
            base: SANDBOX_PRICE_BASE,
            extra,
            freeQuestions: SANDBOX_PRICE_FREE_QUESTIONS,
            perExtraQuestion: SANDBOX_PRICE_PER_EXTRA_QUESTION,
        },
    };
}

/**
 * [#176 §2.2] 教程沙盒的候选问题。非阻塞接口,但 501 会在演示画面上留一句
 * 「这次没能给出候选问题」—— 演示时不好看,顺车接上。
 *
 * 形状按客户端解析(`suggestQuestionsApi.ts`):`{ candidates: [{ question, side, layer }] }`,
 * `side` 用**后端词** growth/defensive(客户端 `sideFromBackend` 再映射成前端词)。
 * 题文围绕沙盒虚构品牌,不写任何真实品牌、不写承诺性话术。
 */
export function getSandboxSuggestQuestions(config: { data?: unknown }) {
    const body = (config?.data || {}) as { mode?: unknown };
    const mode = typeof body.mode === 'string' ? body.mode : '';
    const growth = [
        { question: `${DEMO_BRAND_NAME}的商务包车怎么收费?`, side: 'growth', layer: 'price' },
        { question: `${DEMO_BRAND_NAME}的司机有没有背景审查?`, side: 'growth', layer: 'trust' },
        { question: `企业月结用车一般选哪家?`, side: 'growth', layer: 'category' },
        { question: `跨城商务接送怎么安排比较稳?`, side: 'growth', layer: 'scenario' },
    ];
    const defensive = [
        { question: `${DEMO_BRAND_NAME}靠谱吗?`, side: 'defensive', layer: 'reputation' },
        { question: `${DEMO_BRAND_NAME}和同行比怎么样?`, side: 'defensive', layer: 'compare' },
        { question: `${DEMO_BRAND_NAME}有没有投诉?`, side: 'defensive', layer: 'risk' },
        { question: `${DEMO_BRAND_NAME}适合什么样的客户?`, side: 'defensive', layer: 'fit' },
    ];
    if (mode === 'growth') return { candidates: growth };
    if (mode === 'full') return { candidates: [...growth, ...defensive] };
    // 其他 mode:空 candidates,客户端按 empty 处理(它有自己的示例兜底)
    return { candidates: [] };
}

export function getSandboxDiagnosisStart() {
    sandboxProgress.diagnosisStarted = true;
    sandboxDiagStartTs = Date.now();
    return {
        session_id: SANDBOX_SESSION_ID,
        message: '诊断已启动 (教程模式)',
        diagnosis_id: SANDBOX_DIAGNOSIS_ID,
    };
}

/** 沙盒诊断状态 · 按 elapsed 渐进推进度 · 5 秒后才 done
 *  绕开 WebSocket 等真后端 (沙盒拦不到 ws://) 的卡死问题
 *  让用户在进度页看到诊断"在跑"的体感, 而不是秒跳完成
 *  Stage 1 Batch 4 fix (2026-05-18) · 之前秒返 done · 用户感知不到诊断过程 */
const SANDBOX_DIAG_DURATION_MS = 5000;

export function getSandboxDiagnosisSessionStatus() {
    const startTs = sandboxDiagStartTs ?? Date.now();
    const elapsed = Date.now() - startTs;
    const ratio = Math.min(1, elapsed / SANDBOX_DIAG_DURATION_MS);
    const progress = Math.round(ratio * 100);
    const done = ratio >= 1;
    return {
        found: true,
        done,
        progress,
        stage: done ? 'done' : 'running',
        message: done ? '诊断完成 (教程模式)' : '诊断进行中 (教程模式)',
        diagnosis_id: SANDBOX_DIAGNOSIS_ID,
        brand_id: SANDBOX_BRAND_ID,
    };
}

export function getSandboxDiagnosisProgress() {
    const startTs = sandboxDiagStartTs ?? Date.now();
    const elapsed = Date.now() - startTs;
    const ratio = Math.min(1, elapsed / SANDBOX_DIAG_DURATION_MS);
    const progress = Math.round(ratio * 100);
    const completed = Math.round(ratio * 10);
    const status = ratio >= 1 ? 'completed' : 'running';
    return {
        session_id: SANDBOX_SESSION_ID,
        progress,
        status,
        completed,
        total: 10,
        diagnosis_id: SANDBOX_DIAGNOSIS_ID,
        logs: [ratio >= 1 ? '[沙盒] 诊断完成' : `[沙盒] 诊断中 ${progress}%`],
    };
}

/** 跟生产 id=311 的字段对齐 (26 分危急级 · 32 次测试 7 次命中) */
export function getSandboxDiagnosisDetail() {
    return {
        id: SANDBOX_DIAGNOSIS_ID,
        session_id: SANDBOX_SESSION_ID,
        brand_id: SANDBOX_BRAND_ID,
        brand_name: DEMO_BRAND_NAME,
        industry: DEMO_INDUSTRY_FULL,
        industry_category: '汽车出行',
        total_score: 26,
        level: '危急级',
        diagnosis_type: 'geo',
        web_search_score: 0,
        platform_score: 0,
        content_quality_score: 0,
        authority_score: 0,
        brand_ownership_score: 0,
        ai_visibility_score: 0,
        ai_citation_score: 0,
        update_frequency_score: 0,
        ai_total_tests: 32,
        ai_detected_count: 7,
        ai_mention_rate: 21.9,
        ai_engines_tested: '["dashscope","deepseek","kimi","doubao"]',
        web_result_count: 12,
        brand_account_count: 0,
        competitor_count: 4,
        article_count: 0,
        report_md_path: '/sandbox/demoReport.md',
        keywords: JSON.stringify(SANDBOX_ALL_CANDIDATE_KEYWORDS),
        created_at: nowIso(),
    };
}

export function getSandboxDiagnosisContent() {
    return {
        content: demoReportMd,
        version: 'v2',
        audience: 'client',
        data_completeness_score: 85,
        data_completeness_breakdown: null,
        report_v2_error: null,
    };
}

// ==========================================
// Step 2 在线报价 · 沙盒状态机
// ==========================================

/** 沙盒报价会话内存状态 · 整个 step 2 共用 · reload 重置 */
type QuoteStage = 'idle' | 'created' | 'submitted' | 'priced' | 'quoted' | 'confirmed' | 'pending_payment' | 'paid';
const sandboxQuote = {
    stage: 'idle' as QuoteStage,
    token: '',
    selectedIds: [1, 2, 3] as number[],
};
// 客户提交业务方向/选词后的时间戳 · 用于沙盒里"等待报价 → 自动出报价"的几秒压缩
let sandboxQuoteSubmitTs = 0;

function makeQuoteToken(): string {
    if (!sandboxQuote.token) {
        sandboxQuote.token = 'sandbox-xm-' + Math.random().toString(36).slice(2, 8);
    }
    return sandboxQuote.token;
}

/**
 * 客户最终选的关键词 · 3 个 (跟 SANDBOX_CONTRACT_KEYWORDS 一致)
 * 报价 / 计算 / 写作 / 监测都基于这 3 个
 * 教学故事: "AI 扩出候选, 客户挑了 3 个有真实搜索意图的 (2 核心 + 1 长尾)"
 */
function selectedQuoteKeywords(): string[] {
    const candidates = buildSandboxSelectionCandidates();
    const selected = sandboxQuote.selectedIds
        .map((id) => candidates.find((candidate) => candidate.id === id)?.keyword)
        .filter((keyword): keyword is string => Boolean(keyword));
    return selected.length > 0 ? selected : SANDBOX_CONTRACT_KEYWORDS;
}

// 3 个签约词的分档单价 · 按词逐个略有差异(竞争度不同)· 三档总价正好 = 入门 3000 / 标准 5000 / 旗舰 8000
const SANDBOX_ENTRY_PRICES = [900, 1000, 1100];      // 合计 3000
const SANDBOX_STANDARD_PRICES = [1500, 1700, 1800];  // 合计 5000
const SANDBOX_FLAGSHIP_PRICES = [2400, 2700, 2900];  // 合计 8000
const pickPrice = (arr: number[], i: number) => arr[i] ?? arr[arr.length - 1];

function buildSandboxPricingKeywords() {
    return selectedQuoteKeywords().map((kw, i) => ({
        id: i + 1,
        keyword: kw,
        category_label: '行业核心',
        recommendation_reason: '客户从候选词中挑选 · 真实搜索意图强',
        entry: { price: pickPrice(SANDBOX_ENTRY_PRICES, i), articles: 2 },
        standard: { price: pickPrice(SANDBOX_STANDARD_PRICES, i), articles: 4 },
        flagship: { price: pickPrice(SANDBOX_FLAGSHIP_PRICES, i), articles: 6 },
        intent: 'commercial',
        funnel_stage: 'decision',
        difficulty_score: 65 + i * 3,
        value_score: 75 + i * 2,
        search_volume: 12000 - i * 800,
        sem_price: 8 + i,
        effective_competition: 6 + i,
        search_probability: 0.55 + i * 0.04,
    }));
}

function buildSandboxPricingData() {
    const kws = buildSandboxPricingKeywords();
    const entryTotal = kws.reduce((s, k) => s + k.entry.price, 0);
    const standardTotal = kws.reduce((s, k) => s + k.standard.price, 0);
    const flagshipTotal = kws.reduce((s, k) => s + k.flagship.price, 0);
    return {
        generated_at: new Date().toISOString(),
        tiers: {
            entry: { label: '入门版', target_share: 0.10, ai_probability: '50%', stars: 3, total_price: entryTotal, total_articles: kws.length * 2 },
            standard: { label: '标准版', target_share: 0.20, ai_probability: '65%', stars: 4, total_price: standardTotal, total_articles: kws.length * 4 },
            flagship: { label: '旗舰版', target_share: 0.30, ai_probability: '75%', stars: 5, total_price: flagshipTotal, total_articles: kws.length * 6 },
        },
        keywords: kws,
    };
}

/** 客户选词页候选关键词 (selecting 阶段) · 20 个候选 · 3 个签约词排最前 + 标 recommended
 *  id 1/2/3 = 签约词, 跟 quoted 阶段 selected_ids [1,2,3] 对得上 */
function buildSandboxSelectionCandidates() {
    // 3 个签约词排最前 (id 1/2/3), 跟 quoted 阶段 selected_ids [1,2,3] 对得上
    const rest = SANDBOX_ALL_CANDIDATE_KEYWORDS.filter(k => !SANDBOX_CONTRACT_KEYWORDS.includes(k));
    const ordered = [...SANDBOX_CONTRACT_KEYWORDS, ...rest];
    return ordered.map((kw, i) => {
        const isContract = SANDBOX_CONTRACT_KEYWORDS.includes(kw);
        return {
            id: i + 1,
            keyword: kw,
            category: 'core',
            category_label: '行业核心',
            difficulty: 55 + (i % 4) * 6,
            recommended: isContract,
            recommendation_reason: isContract ? '真实搜索意图强 · 客户真的会这么搜这个词' : '',
            intent: 'commercial',
        };
    });
}

/** 客户选词页"业务方向"(真实业务: 后端 LLM 按品牌/行业/关键词自动生成 · 客户多选)
 *  example_scenarios = 该业务方向下挂的真实关键词(卡片下方标签)· 跟真实业务一致
 *  4 个业务方向覆盖全部候选词 · 3 个签约词(2 魔法 + 1 长尾)都归在"豪车配司机"方向 */
function buildSandboxBusinessLines() {
    return [
        { id: 1, name: '豪车配司机 · 高端专属用车', description: '埃尔法/豪华商务车 + 专业司机, 适合高端商务接待、外宾接送、长租包月', example_scenarios: [...SANDBOX_CONTRACT_KEYWORDS], is_selected: false },
        { id: 2, name: '商务接送机服务', description: '机场往返接送, 举牌等候、航班动态跟踪', example_scenarios: ['高端商务接送机服务商'], is_selected: false },
        { id: 3, name: '企业商务用车 · 差旅', description: '企业日常商务用车、外资差旅、年度包车', example_scenarios: ['深圳企业商务用车服务', '外资企业差旅出行解决方案', '企业年度包车服务', '隐私安全商务出行服务'], is_selected: false },
        { id: 4, name: '跨城 · 跨境联运', description: '全国跨城商务联运、跨境电商专车接送', example_scenarios: ['全国跨城商务联运服务', '跨境电商专车接送'], is_selected: false },
    ];
}

/** POST /api/s/{token}/submit-business-lines · 客户提交业务方向(真实业务的默认流程)
 *  真实业务: 系统按选中的业务方向自动选词 → 直接进"等待报价" · 不走二次关键词细选 */
export function getSandboxSelectionSubmitBusinessLines() {
    if (sandboxQuote.stage === 'idle' || sandboxQuote.stage === 'created') {
        sandboxQuote.stage = 'submitted';      // = keywords_submitted · 客户进"等待报价"
        sandboxQuoteSubmitTs = Date.now();      // 几秒后自动出报价(沙盒压缩代理算价耗时)
    }
    return { success: true, status: 'keywords_submitted' };
}

/** POST /api/s/{token}/submit-keywords · 老流程(business_lines 为空时)· 客户直接勾词提交 */
export function getSandboxSelectionSubmitKeywords(selectedIds: number[] = []) {
    const validIds = new Set(buildSandboxSelectionCandidates().map((candidate) => candidate.id));
    const normalizedIds = [...new Set(selectedIds)].filter((id) => validIds.has(id));
    if (normalizedIds.length > 0) {
        sandboxQuote.selectedIds = normalizedIds;
    }
    if (sandboxQuote.stage === 'idle' || sandboxQuote.stage === 'created') {
        sandboxQuote.stage = 'submitted';
        sandboxQuoteSubmitTs = Date.now();
    }
    return { success: true, status: 'keywords_submitted' };
}

/** POST /api/s/{token}/confirm-quote · 客户选档位 + 确认报价 → 确认态 */
export function getSandboxSelectionConfirmQuote() {
    sandboxQuote.stage = 'confirmed';
    return { success: true, status: 'confirmed' };
}

/** 状态机推进映射 · 沙盒内部 stage → 后端 status */
const QUOTE_STAGE_TO_STATUS: Record<QuoteStage, string> = {
    idle: 'selecting',
    created: 'selecting',
    submitted: 'keywords_submitted',
    priced: 'pricing_pending_review',
    quoted: 'quoted',
    confirmed: 'confirmed',
    pending_payment: 'pending_payment',
    paid: 'active',
};

/** 构建当前 sandbox session 的完整对象 · GET /api/s/{token} 返这个 */
export function getSandboxQuoteSession() {
    // 客户提交业务方向后 · 等 ~3s 自动出报价(沙盒压缩代理算价的真实耗时, 让客户端能继续往下走)
    if (sandboxQuote.stage === 'submitted' && sandboxQuoteSubmitTs > 0 && Date.now() - sandboxQuoteSubmitTs > 3000) {
        sandboxQuote.stage = 'quoted';
    }
    const stage = sandboxQuote.stage;
    const status = QUOTE_STAGE_TO_STATUS[stage];
    const pricingShown = stage === 'priced' || stage === 'quoted' || stage === 'confirmed' || stage === 'pending_payment' || stage === 'paid';
    const tierShown = stage === 'quoted' || stage === 'confirmed' || stage === 'pending_payment' || stage === 'paid';
    return {
        token: makeQuoteToken(),
        quote_id: 999001,
        brand_name: DEMO_BRAND_NAME,
        brand_id: SANDBOX_BRAND_ID,
        industry: DEMO_INDUSTRY,
        city: DEMO_CITY,
        status,
        selected_count: stage === 'idle' || stage === 'created' ? 0 : selectedQuoteKeywords().length,
        selected_tier: tierShown ? 'entry' : undefined,
        // 入门版价格 = kw0(900) + kw1(1000) + kw2(1100) = 3000/月 (3 个签约词 · 新品牌轻量启动)
        confirmed_total_price: tierShown ? buildSandboxPricingData().tiers.entry.total_price : undefined,
        final_price: stage === 'paid' ? buildSandboxPricingData().tiers.entry.total_price : undefined,
        created_at: nowIso(),
        updated_at: nowIso(),
        keywords_submitted_at: stage === 'submitted' || pricingShown ? nowIso() : undefined,
        confirmed_at: tierShown ? nowIso() : undefined,
        pricing_data: pricingShown ? buildSandboxPricingData() : null,
        // selecting 阶段: 真实业务默认走"业务方向选择"(后端 LLM 自动生成 business_lines)· 跟线上一致
        business_lines: status === 'selecting' ? buildSandboxBusinessLines() : undefined,
        // keywords 在 选词/等待报价 阶段都带上 · 等待页(WaitingQuote)按 selected_ids 过滤 keywords 显示"已选 N 个"
        keywords: (status === 'selecting' || status === 'keywords_submitted' || status === 'pricing_pending_review')
            ? buildSandboxSelectionCandidates() : undefined,
        selected_ids: stage === 'idle' || stage === 'created' ? [] : [...sandboxQuote.selectedIds],
        custom_keywords: [],
        // 客户最终签约的 3 个词 (2 核心魔法词 + 1 长尾) · 跟 step 3/4 真实素材匹配
        selected_business_lines: stage === 'idle' || stage === 'created' ? [] : selectedQuoteKeywords(),
    };
}

/** POST /api/keyword-selection/create · 创建会话
 *  注: 沙盒教程里"客户选完业务方向"不再用定时器自动推进, 改由"客户端选词录屏"播放结束后驱动
 *  (advanceSandboxClientSelect) · 让代理看完录屏才进入下一步 */
export function getSandboxQuoteCreate() {
    sandboxQuote.stage = 'created';
    sandboxQuote.token = ''; // 让 makeQuoteToken 生新
    sandboxQuote.selectedIds = [1, 2, 3];
    const token = makeQuoteToken();
    return { token, status: 'selecting' };
}

/** 沙盒教程: "客户端选业务方向"录屏播放结束后调用 → 推进到"已提交选词"(待计算报价)
 *  不设 sandboxQuoteSubmitTs · 避免触发 getSandboxQuoteSession 里 submitted→quoted 的自动跳转,
 *  让代理在"计算报价/审核"步骤手动操作 (跟原 create 定时器行为一致) */
export function advanceSandboxClientSelect() {
    if (sandboxQuote.stage === 'idle' || sandboxQuote.stage === 'created') {
        sandboxQuote.stage = 'submitted';
    }
}

/** 沙盒教程: "客户端选套餐确认"录屏播放结束后调用 → 推进到"已确认"(待签约) */
export function advanceSandboxClientConfirm() {
    if (sandboxQuote.stage === 'quoted') {
        sandboxQuote.stage = 'confirmed';
    }
}

/** GET /api/keyword-selection/list · 列表 · 沙盒里只展示当前会话 (如果已创建) */
export function getSandboxQuoteList() {
    if (sandboxQuote.stage === 'idle') return { sessions: [] };
    const s = getSandboxQuoteSession();
    return { sessions: [{ token: s.token, brand_name: s.brand_name, industry: s.industry, city: s.city, status: s.status, selected_count: s.selected_count, created_at: s.created_at }] };
}

/** POST /api/keyword-selection/{token}/generate-quote · 计算报价 */
export function getSandboxQuoteGenerate() {
    sandboxQuote.stage = 'priced';
    return { success: true, pricing_data: buildSandboxPricingData() };
}

/** POST /api/keyword-selection/{token}/approve-quote · 销售审核通过 → 发送给客户 */
export function getSandboxQuoteApprove() {
    sandboxQuote.stage = 'quoted';
    // 注: "客户选档位确认"不再用定时器自动推进, 改由"客户端选套餐录屏"播放结束驱动
    // (advanceSandboxClientConfirm)
    return { success: true };
}

/** POST /api/keyword-selection/{token}/mark-paid · 标记付款 → 完成 step 2 在线报价路径 */
export function getSandboxQuoteMarkPaid() {
    sandboxQuote.stage = 'paid';
    // 通知 PricingCenter · 路径走完 · 重弹路径选择器让用户决定走另一条 / 进下一步
    if (typeof window !== 'undefined') {
        setTimeout(() => {
            window.dispatchEvent(new CustomEvent('sandbox:quote-path-done', { detail: { path: 'online' } }));
        }, 1500); // 让"在线报价流程完成"卡片先显示, 1.5s 后再弹路径选择
    }
    return { success: true, message: '付款已标记 · 关键词已进入写作大厅', keywords_created: selectedQuoteKeywords().length };
}

/** POST /api/keyword-selection/{token}/sales-confirm · 销售确认签约信息 → 等收款 */
export function getSandboxQuoteSalesConfirm() {
    sandboxQuote.stage = 'pending_payment';
    return { success: true };
}

/** POST /api/keywords/expand · AI 智能扩词 · 返 20 个商业候选 (含 3 个主流程签约词) */
export function getSandboxKeywordsExpand() {
    return {
        success: true,
        keywords: SANDBOX_ALL_CANDIDATE_KEYWORDS.map((kw, i) => ({
            keyword: kw,
            source: ['seed', 'ai_expand', 'sem', 'long_tail', 'competitor'][i % 5],
            // 3 个签约词标 long_tail (C 端搜索意图), 其他 B2B 标行业核心
            category: SANDBOX_CONTRACT_KEYWORDS.includes(kw) ? '长尾询价' : '行业核心',
            intent: 'commercial',
            geo_recommend: SANDBOX_CONTRACT_KEYWORDS.includes(kw),
            scope_match: true,
            default_selected: SANDBOX_CONTRACT_KEYWORDS.includes(kw),
            rejection_reason: SANDBOX_CONTRACT_KEYWORDS.includes(kw)
                ? ''
                : '沙盒候选仅展示，需操作员明确勾选后进入报价',
        })),
    };
}

/** 沙盒退出 / 重置时, 清空报价状态机 */
export function resetSandboxQuoteState() {
    sandboxQuote.stage = 'idle';
    sandboxQuote.token = '';
    sandboxQuote.selectedIds = [1, 2, 3];
}

// ==========================================
// Step 2 报价页支持接口 · 历史诊断 + 复测数据
// ==========================================

/** GET /api/history · 返一路顺风出行的真实诊断 (id=311) · 让在线报价页诊断 dropdown 有选项
 *  keywords 字段必填 · QuoteCenter 用户选完诊断后会自动从这里把关键词填进 coreKeywords 文本框 */
export function getSandboxHistoryForQuote() {
    return {
        success: true,
        data: [{
            id: SANDBOX_DIAGNOSIS_ID,
            session_id: SANDBOX_SESSION_ID,
            brand_id: SANDBOX_BRAND_ID,
            brand_name: DEMO_BRAND_NAME,
            industry: DEMO_INDUSTRY,
            industry_category: '汽车出行',
            total_score: 26,
            level: '危急级',
            created_at: nowIso(),
            diagnosis_type: 'geo',
            article_count: 0,
            report_md_path: '/sandbox/demoReport.md',
            keywords: JSON.stringify(SANDBOX_ALL_CANDIDATE_KEYWORDS),
        }],
    };
}

/** GET /api/diagnosis/{id}/retest-data · 诊断复测数据 · 返预设关键词城市 */
export function getSandboxRetestData() {
    return {
        brand_name: DEMO_BRAND_NAME,
        industry: DEMO_INDUSTRY,
        keywords: SANDBOX_ALL_CANDIDATE_KEYWORDS,
        client_location: DEMO_CITY,
        original_score: 26,
        original_level: '危急级',
        original_date: nowIso(),
    };
}

/** GET /api/quotes (报价列表) · 沙盒里返空让 city useEffect 不报错 */
export function getSandboxQuotesList() {
    return { items: [], data: [], total: 0 };
}

// ==========================================================================
// 2026-05-25:快速录单路径已废弃 · 整段 mock 删除
// 原本这里有:SANDBOX_QUICK_QUOTE_ID=999002 / getSandboxDistill / getSandboxKeywordsQuote /
//            getSandboxOfflineConfirm / getSandboxOfflineMarkPaid
// 对应的 API 路由拦截规则在 sandboxInterceptor.ts 里也同步删了
// ==========================================================================

// ==========================================================================
// Step 3 · AI 写文章 + 发布 · 沙盒状态机
// ==========================================================================
//
// 故事:
//   step 2 完成 → 3 个签约词进写作大厅 (project_id=999001)
//   代理点"生成标题" → 6 个标题就绪 (2 标题 × 3 词)
//   点"开始写作" → 6 篇文章并发生成, ~8s 模拟
//   全部完成 → 跳 /articles 列表
//   选一篇 → /publish 选代发媒介 → 加购物车 (6 次)
//   全部 6 篇加购 → 批量发布给媒介盒子 (沙盒不真发)
//   外发边界定位广告法目录命中 → 只修该处 → step 3 完成 → 引导 step 4
//
// 子阶段:
//   stage: 'idle' | 'in_hall' | 'titles_ready' | 'writing' | 'done' | 'in_cart' | 'submitted'
//
// 跟 quote 状态联动: paid 之后 stage 自动转 'in_hall'
// ==========================================================================

const SANDBOX_WRITING_PROJECT_ID = 999001;
const SANDBOX_WRITING_TASK_ID = 'sandbox-write-' + Math.random().toString(36).slice(2, 8);

type WritingStage = 'idle' | 'in_hall' | 'titles_ready' | 'writing' | 'done' | 'in_cart' | 'submitted';
const sandboxWriting = {
    stage: 'idle' as WritingStage,
    writeStartTs: 0,
    cartItemIds: new Set<number>(), // 已加购物车的 article id
    // step 4 补发闭环子流程
    optimizeStage: 'none' as 'none' | 'generated' | 'opt_writing' | 'opt_done' | 'recovered',
    optWriteStartTs: 0,
};

// step 4 补发: 坏 case 词「深圳埃尔法长租包月」(= 第 3 个合同词, 写作大厅 keyword id 40003)
// 补发是给这个"已签约但起量慢"的词再加 2 篇文章, 不是新增一个词
const SANDBOX_OPT_KW_ID = 40003;
const SANDBOX_OPT_TOPICS = [
    { id: 49001, keyword_id: SANDBOX_OPT_KW_ID, original_keyword: SANDBOX_OPT_KEYWORD, optimized_title: `${SANDBOX_OPT_KEYWORD}怎么算更划算? 长租包月 vs 按天租成本对比`, article_style: 'guide' },
    { id: 49002, keyword_id: SANDBOX_OPT_KW_ID, original_keyword: SANDBOX_OPT_KEYWORD, optimized_title: `深圳企业长租埃尔法包月真实案例·一个月用车成本全公开`, article_style: 'case' },
];

/** 补发 optimize topics · 状态按 optimizeStage + elapsed 推进 (pending→writing→completed) */
function getSandboxOptimizeTopics() {
    const st = sandboxWriting.optimizeStage;
    let completedCount = 0;
    let writing = false;
    if (st === 'opt_writing') {
        const ratio = Math.min(1, (Date.now() - sandboxWriting.optWriteStartTs) / 5000);
        completedCount = Math.floor(ratio * SANDBOX_OPT_TOPICS.length);
        if (completedCount >= SANDBOX_OPT_TOPICS.length) sandboxWriting.optimizeStage = 'opt_done';
        writing = true;
    } else if (st === 'opt_done' || st === 'recovered') {
        completedCount = SANDBOX_OPT_TOPICS.length;
    }
    return SANDBOX_OPT_TOPICS.map((t, i) => {
        const isCompleted = i < completedCount;
        const isWriting = writing && !isCompleted;
        return {
            ...t,
            status: isCompleted ? 'completed' : (isWriting ? 'writing' : 'pending'),
            article_id: isCompleted ? t.id + 1000 : undefined,
            completed_at: isCompleted ? nowIso() : undefined,
            reviewed_at: undefined,
            article_version: 1,
            is_optimize: true,
        };
    });
}

/** 沙盒检查 step 2 完成后自动进 in_hall · 在 mockData 内部任何写作接口被调用时 lazy 触发
 *  双重判断: (a) 内存里 sandboxQuote.stage='paid', 或 (b) localStorage onboarding 标了 first_quote 完成
 *  (b) 是为了刷新页面后内存丢失也能恢复 · 沙盒所有状态机都是内存变量, 刷新就 reset */
function isStep2CompletedFromLocalStorage(): boolean {
    if (typeof window === 'undefined') return false;
    try {
        const raw = window.localStorage.getItem('omnirank_onboarding_state');
        if (!raw) return false;
        const parsed = JSON.parse(raw);
        return Array.isArray(parsed.completed_steps) && parsed.completed_steps.includes('first_quote');
    } catch { return false; }
}

function ensureWritingStageInited() {
    if (sandboxWriting.stage !== 'idle') return;
    if (sandboxQuote.stage === 'paid' || isStep2CompletedFromLocalStorage()) {
        sandboxWriting.stage = 'in_hall';
    }
}

/** 6 个标题: 每个魔法词配 3 个不同切入角度 */
function getSandboxTopics() {
    const [kwA, kwB] = SANDBOX_MAGIC_KEYWORDS;
    const kwC = SANDBOX_LAGGARD_KEYWORD;
    // 3 个签约词各 2 篇 = 6 篇 · title 跟 SANDBOX_ARTICLE_CONTENTS (article_id = topic.id + 1000) 一一对应
    return [
        // kwA = 深圳租埃尔法配司机的公司
        { id: 30001, keyword_id: 40001, original_keyword: kwA, optimized_title: `${kwA}排名前三深度横评·商务接待选哪家?`, article_style: 'comparison' },
        { id: 30002, keyword_id: 40001, original_keyword: kwA, optimized_title: `深圳埃尔法配司机价格全解析·按天/月/项目租分别多少钱`,                article_style: 'guide' },
        // kwB = 深圳豪车配司机哪家好
        { id: 30003, keyword_id: 40002, original_keyword: kwB, optimized_title: `选深圳豪车配司机的 5 个避坑点·别只看价格`,                       article_style: 'guide' },
        { id: 30004, keyword_id: 40002, original_keyword: kwB, optimized_title: `${kwB}? 4 家深圳豪车租赁公司实测体验对比`,                       article_style: 'review' },
        // kwC = 深圳埃尔法长租包月 (长尾签约词 · step4 监测里起量慢、靠补发救回)
        { id: 30005, keyword_id: 40003, original_keyword: kwC, optimized_title: `深圳埃尔法长租包月服务详解·哪些企业适合长包`,                     article_style: 'guide' },
        { id: 30006, keyword_id: 40003, original_keyword: kwC, optimized_title: `深圳长租埃尔法包月的 5 大优势·商务用车降本指南`,                   article_style: 'guide' },
    ];
}

/** topic.status 真实口径: 'pending' | 'draft' | 'failed' | 'writing' | 'completed'
 *  in_hall / titles_ready 都映射到 'pending' (有标题待写状态 = pending 在 UI 上能勾选写作) */
function topicStatusForStage(stage: WritingStage): string {
    if (stage === 'in_hall') return 'pending';
    if (stage === 'titles_ready') return 'pending';
    if (stage === 'writing') return 'writing';
    return 'completed';
}

/** GET /api/writing/projects · 列表 · 沙盒只有 1 个项目 */
export function getSandboxWritingProjects() {
    ensureWritingStageInited();
    if (sandboxWriting.stage === 'idle') return { success: true, projects: [] };
    const stage = sandboxWriting.stage;
    // 后端口径: 'pending'(待生成标题) | 'titles_ready' | 'writing' | 'completed'
    const writingStatus =
        stage === 'in_hall' ? 'pending'
        : stage === 'titles_ready' ? 'titles_ready'
        : stage === 'writing' ? 'writing'
        : 'completed';
    return {
        success: true,
        projects: [{
            id: SANDBOX_WRITING_PROJECT_ID,
            quote_ids: [999001],
            brand_id: SANDBOX_BRAND_ID,
            brand_name: DEMO_BRAND_NAME,
            industry: DEMO_INDUSTRY,
            keyword_count: SANDBOX_CONTRACT_KEYWORDS.length,
            total_required_articles: 6,
            monthly_price: 3000,
            writing_status: writingStatus,
            confirmed_at: nowIso(),
        }],
    };
}

/**
 * [WO_218-a1] 教程里也要有「本次将扣 N」那个数。
 *
 * 🔴 **沙盒是第二个后端**:真详情端点 2026-09-18 起回包带 `charge`
 * (契约 `services/topic_gen_charge.py`),这份夹具不跟上的话,教程那一步会
 * **安静地没有价** —— 不报错、不变红,与生产分家而无人看见。
 * 🔴 这里**照抄契约的形状**,不自己发明字段;数按沙盒的 3 个关键词 × 目录基价 80 算,
 *    与 `charge_card()` 的定义一致(`estimated = base × keyword_count`)。
 *    这是**夹具**在复刻后端的算法,不是前端在算钱 —— 前端那一侧只读字段。
 */
function sandboxTopicGenCharge(keywordCount: number) {
    const base = 80;
    const count = Math.max(1, keywordCount);
    const total = base * count;
    return {
        feature_code: 'topic_gen',
        base_points: base,
        keyword_count: count,
        estimated_points: total,
        ceiling_points: total,
    };
}

/** GET /api/writing/projects/{id} · 项目详情 · 关键词 + 选题列表
 *  in_hall 阶段 topics=[] (还没点"批量生成标题")
 *  writing 阶段按 elapsed 部分 'writing' 部分 'completed'
 *  done/in_cart/submitted 全 completed */
export function getSandboxWritingProjectDetail() {
    ensureWritingStageInited();
    const stage = sandboxWriting.stage;
    if (stage === 'in_hall') {
        return {
            success: true,
            keywords: [
                { id: 40001, keyword: SANDBOX_MAGIC_KEYWORDS[0], required_articles: 2, recommended_platforms: '小红书,微信公众号,搜狐号', final_price: 2400 },
                { id: 40002, keyword: SANDBOX_MAGIC_KEYWORDS[1], required_articles: 2, recommended_platforms: '搜狐号,百家号,简书',     final_price: 2400 },
                { id: 40003, keyword: SANDBOX_LAGGARD_KEYWORD,   required_articles: 2, recommended_platforms: '搜狐号,百家号',           final_price: 1200 },
            ],
            topics: [],
            charge: sandboxTopicGenCharge(3),
        };
    }
    // 写作中 → 按 elapsed 算每个 topic 的状态 · 让 UI 显示渐进完成
    let completedCount = 6;
    if (stage === 'writing') {
        const elapsed = Date.now() - sandboxWriting.writeStartTs;
        const ratio = Math.min(1, elapsed / 8000);
        completedCount = Math.floor(ratio * 6);
        // 全部完成时, lazy 推到 done
        if (completedCount >= 6) sandboxWriting.stage = 'done';
    } else if (stage === 'titles_ready') {
        completedCount = 0;
    }
    const topics = getSandboxTopics().map((t, i) => {
        const isCompleted = i < completedCount;
        const isWriting = stage === 'writing' && !isCompleted;
        return {
            ...t,
            status: isCompleted ? 'completed' : (isWriting ? 'writing' : 'pending'),
            article_id: isCompleted ? t.id + 1000 : undefined,
            completed_at: isCompleted ? nowIso() : undefined,
            reviewed_at: undefined,
            article_version: 1,
            is_optimize: false,
        };
    });
    // 3 个签约词各 2 篇 = 6 篇 (40003 = 长尾词 深圳埃尔法长租包月, step4 靠补发再加 2 篇救回)
    const optimizing = sandboxWriting.optimizeStage !== 'none';
    const keywords = [
        { id: 40001, keyword: SANDBOX_MAGIC_KEYWORDS[0], required_articles: 2, recommended_platforms: '小红书,微信公众号,搜狐号', final_price: 2400 },
        { id: 40002, keyword: SANDBOX_MAGIC_KEYWORDS[1], required_articles: 2, recommended_platforms: '搜狐号,百家号,简书',     final_price: 2400 },
        { id: 40003, keyword: SANDBOX_LAGGARD_KEYWORD,   required_articles: optimizing ? 4 : 2, recommended_platforms: '搜狐号,百家号', final_price: 1200 },
    ];
    // step 4 补发: 给已签约但起量慢的「长租包月」再追加 2 篇文章 (挂在它名下, 不新增词)
    if (optimizing) {
        topics.push(...getSandboxOptimizeTopics());
    }
    return {
        success: true,
        keywords,
        topics,
        charge: sandboxTopicGenCharge(keywords.length),
    };
}

/** POST /api/writing/research-competitors/{id} · 联网搜竞品 · 沙盒返预设 5 个真实竞品 */
export function getSandboxResearchCompetitors() {
    return {
        success: true,
        competitors: [
            { name: '滴滴企业版', excluded: false, source: 'real' as const },
            { name: '神州专车', excluded: false, source: 'real' as const },
            { name: '首汽约车', excluded: false, source: 'real' as const },
            { name: '曹操企业版', excluded: false, source: 'real' as const },
            { name: '阳光出行', excluded: false, source: 'real' as const },
        ],
        mode: 'real',
    };
}

/** POST /api/writing/competitors/{id}/verify · 仅核验当前候选，不重新生成名单 */
export function getSandboxVerifyCompetitors() {
    const competitors = getSandboxResearchCompetitors().competitors.map((item) => ({
        ...item,
        name_verified: true,
        name_verification_method: 'sandbox_name_search_v1',
    }));
    return {
        status: 'success',
        mode: 'real',
        competitors,
        attempted_count: competitors.length,
        newly_verified: competitors.length,
        verified_count: competitors.length,
        pending_count: 0,
        pending_names: [],
        provider_error_count: 0,
    };
}

/** PUT /api/writing/competitors/{id}/mode · 模式切换持久化 */
export function getSandboxCompetitorsMode() {
    return { success: true };
}

/** GET /api/writing/competitors/{id} · 拉竞品列表 (loadCompetitors)
 *  初始返空 · mode='fictional' · 让用户从智选默认开始, 通过 spotlight 引导点严选触发 research */
export function getSandboxCompetitorsList() {
    return {
        success: true,
        competitors: [],
        mode: 'fictional',
    };
}

/** GET /api/writing/projects/{id}/knowledge-status · 知识库状态
 *  沙盒态: 初始返 'none' (红色 未补资料) · 教程引导用户走"上传"流程
 *  关键: quote_id 必须设, 让前端 status?.quote_id === selectedProject.id 命中本地缓存
 *  否则模拟上传后本地切到 confirmed 时, startWriting 又会重 fetch 把 'none' 拉回来 */
export function getSandboxWritingKnowledgeStatus() {
    return {
        success: true,
        status: 'none' as const,
        can_start_writing: false,
        brand_id: SANDBOX_BRAND_ID,
        quote_id: SANDBOX_WRITING_PROJECT_ID,
        token_url: null,
        message: '客户资料还未上传, 请先补充',
        materials_summary: {
            company_name: DEMO_BRAND_NAME,
            industry: DEMO_INDUSTRY,
            intro_excerpt: '',
            usp_excerpt: '',
            fields_filled: [],
            fields_missing: ['company_intro', 'products', 'usp', 'cases', 'tone'],
            filled_count: 0,
            total_fields: 5,
        },
    };
}

/** GET /api/knowledge/client/{brand_id} · 客户知识库文件 · 沙盒初始返空
 *  教程走完上传模拟后, 由前端本地 setKnowledgeFiles 补一个已向量化文件 · 让 hasIndexedKnowledge=true */
export function getSandboxKnowledgeFiles() {
    return {
        success: true,
        documents: [],
    };
}

/** POST /api/writing/generate-titles · 生成标题 · 推到 titles_ready
 *  字段口径: success + topic_count (WritingHall 用 success && topic_count > 0 判成功) */
export function getSandboxGenerateTitles() {
    ensureWritingStageInited();
    sandboxWriting.stage = 'titles_ready';
    return {
        success: true,
        topic_count: 6,
        message: '已为 3 个关键词生成 6 个候选标题',
        titles_count: 6,
    };
}

/** POST /api/writing/start-articles · 开始写作 · 推到 writing 阶段 + 记录时间戳 */
export function getSandboxStartArticles() {
    // step 4 补发: 已签约发过主文 (optimizeStage=generated) → 这次是补发壳子文
    if (sandboxWriting.optimizeStage === 'generated') {
        sandboxWriting.optimizeStage = 'opt_writing';
        sandboxWriting.optWriteStartTs = Date.now();
        return { success: true, message: `已开始生成 ${SANDBOX_OPT_TOPICS.length} 篇补发文章 (教程模式)`, task_id: SANDBOX_WRITING_TASK_ID };
    }
    sandboxWriting.stage = 'writing';
    sandboxWriting.writeStartTs = Date.now();
    return { success: true, message: '已开始生成 6 篇文章 (教程模式)', task_id: SANDBOX_WRITING_TASK_ID };
}

/** GET /api/writing/progress/{id} · 写作进度轮询 · 按 elapsed 推 0→100 · 总耗时 8 秒
 *  ≤2s: 0 篇完成, 6 篇 writing
 *  2-4s: 2 篇完成, 4 篇 writing
 *  4-6s: 4 篇完成, 2 篇 writing
 *  ≥8s: 6 篇全完成 · stage→done */
export function getSandboxWritingProgress() {
    // step 4 补发壳子文进度 (2 篇 · ~5s)
    if (sandboxWriting.optimizeStage === 'opt_writing' || sandboxWriting.optimizeStage === 'generated') {
        const optTopics = getSandboxOptimizeTopics(); // 触发状态推进 + 可能转 opt_done
        const completed = optTopics.filter(t => t.status === 'completed').length;
        const writingList = optTopics.filter(t => t.status !== 'completed');
        const completedList = optTopics.filter(t => t.status === 'completed');
        const total = SANDBOX_OPT_TOPICS.length;
        return {
            success: true,
            total,
            pending: optTopics.filter(t => t.status === 'pending').length,
            writing: writingList.length,
            completed,
            progress: Math.round((completed / total) * 100),
            writing_list: writingList,
            completed_list: completedList,
        };
    }
    const stage = sandboxWriting.stage;
    if (stage !== 'writing' && stage !== 'done' && stage !== 'in_cart' && stage !== 'submitted') {
        return { success: true, total: 6, pending: 6, writing: 0, completed: 0, progress: 0, writing_list: [], completed_list: [] };
    }
    const elapsed = Date.now() - sandboxWriting.writeStartTs;
    const DURATION = 8000;
    const ratio = Math.min(1, elapsed / DURATION);
    const completed = Math.floor(ratio * 6);
    const writing = 6 - completed;
    if (completed >= 6 && stage === 'writing') {
        sandboxWriting.stage = 'done';
    }
    const allTopics = getSandboxTopics();
    return {
        success: true,
        total: 6,
        pending: 0,
        writing,
        completed,
        progress: Math.round(ratio * 100),
        writing_list: allTopics.slice(completed).map(t => ({ ...t, status: 'writing' })),
        completed_list: allTopics.slice(0, completed).map(t => ({
            ...t,
            status: 'completed',
            article_id: t.id + 1000,
            completed_at: nowIso(),
        })),
    };
}

// ===== 6 篇教程模式占位文章 =====
// 内容刻意保持简短并明确标注教程模式，避免用户把占位稿当成真实交付。
function buildDemoArticle(opts: {
    title: string;
    keyword: string;
    hasSensitive: boolean;
    article_style: string;
    sensitivePhrase?: string;
}): { title: string; content: string; keyword: string; hasSensitive: boolean } {
    const banner = `> **教程模式 · 这是占位文章，不是真实 AI 输出**
> 真实使用时，AI 会根据已上传的客户资料、问题复杂度和可用证据自适应展开；资料充分时可生成深度长文，资料不足时宁可更短，也不重复注水。
> 文章会自然回答购买问题，并在有真实来源时用自然语言引用；不会向客户正文暴露内部审核标记。
> 这里只是给你看一下"文章生成流程"长什么样, 内容是模板.`;
    const sensitiveLine = opts.hasSensitive
        ? `\n\n> 教程提示：这篇占位稿含 **「${opts.sensitivePhrase || '最便宜'}」**。草稿仍可保存，真正对外发布前系统会定位这一处并提供局部修改；其余成功内容不会重写。`
        : '';
    return {
        title: opts.title,
        keyword: opts.keyword,
        hasSensitive: opts.hasSensitive,
        content: `# ${opts.title}

${banner}

---

## 文章结构示例 (${opts.article_style})

- **引子**: 客户痛点切入 — *AI 会根据客户行业自动生成*
- **中段**: 客户卖点 + 真实案例 — *AI 会引用知识库里的客户资料*
- **结尾**: 按文章目标自然收束 — *仅在本次生成明确开启时才加入客户联系方式*

## 真实场景 AI 会输出的内容

1. ✅ 围绕购买问题「${opts.keyword}」直接回答
2. ✅ 按资料规模自适应展开，不截断、不重复凑字数
3. ✅ 只引用实际提供或搜索到的真实来源
4. ✅ 适配目标平台风格并保留人工编辑权
5. ✅ 普通质量问题只标注具体位置，可局部 AI 修复或人工继续${sensitiveLine}

---

*以上是教程模式的演示占位 · 字数 / 排版与真实输出不同 · 仅用于让你体验"看文章 → 重写 → 发布"的操作流程*`,
    };
}

const SANDBOX_ARTICLE_CONTENTS: Record<number, { title: string; content: string; keyword: string; hasSensitive: boolean }> = {
    31001: buildDemoArticle({
        title: '深圳租埃尔法配司机的公司排名前三深度横评·商务接待选哪家?',
        keyword: SANDBOX_MAGIC_KEYWORDS[0],
        hasSensitive: false,
        article_style: '横评对比型',
    }),
    31002: buildDemoArticle({
        title: '深圳埃尔法配司机价格全解析·按天/月/项目租分别多少钱',
        keyword: SANDBOX_MAGIC_KEYWORDS[0],
        hasSensitive: false,
        article_style: '价格科普型',
    }),
    31003: buildDemoArticle({
        title: '选深圳豪车配司机的 5 个避坑点·别只看价格',
        keyword: SANDBOX_MAGIC_KEYWORDS[1],
        hasSensitive: false,
        article_style: '避坑指南型',
    }),
    31004: buildDemoArticle({
        title: '深圳豪车配司机哪家好? 4 家深圳豪车租赁公司实测体验对比',
        keyword: SANDBOX_MAGIC_KEYWORDS[1],
        hasSensitive: true,
        article_style: '横评对比型',
        sensitivePhrase: '最便宜',
    }),
    31005: buildDemoArticle({
        title: '深圳埃尔法长租包月服务详解·哪些企业适合长包',
        keyword: SANDBOX_LAGGARD_KEYWORD,
        hasSensitive: false,
        article_style: '科普指南型',
    }),
    31006: buildDemoArticle({
        title: '深圳长租埃尔法包月的 5 大优势·商务用车降本指南',
        keyword: SANDBOX_LAGGARD_KEYWORD,
        hasSensitive: false,
        article_style: '避坑指南型',
    }),
};

/** GET /api/articles?project_id=999001 · 文章列表 · 6 篇 */
export function getSandboxArticlesList() {
    ensureWritingStageInited();
    const stage = sandboxWriting.stage;
    if (stage !== 'done' && stage !== 'in_cart' && stage !== 'submitted') {
        return { success: true, articles: [] };
    }
    return {
        success: true,
        articles: Object.entries(SANDBOX_ARTICLE_CONTENTS).map(([idStr, a]) => {
            const id = parseInt(idStr, 10);
            return {
                id,
                title: a.title,
                content: a.content,
                status: 'completed',
                batch_id: 'sandbox-batch-1',
                keyword: a.keyword,
                word_count: a.content.length,
                created_at: nowIso(),
                project_id: SANDBOX_WRITING_PROJECT_ID,
                in_cart: sandboxWriting.cartItemIds.has(id),
            };
        }),
    };
}

/** GET /api/articles/{id} · 单篇文章详情
 *  WritingHall 的 previewArticle 用 data.article.{title,content} 取值 · 必须嵌套在 article 字段下 */
export function getSandboxArticleDetail(articleId: number) {
    const a = SANDBOX_ARTICLE_CONTENTS[articleId];
    if (!a) return { success: false, detail: '文章不存在' };
    const articleObj = {
        id: articleId,
        title: a.title,
        content: a.content,
        status: 'completed',
        keyword: a.keyword,
        word_count: a.content.length,
        created_at: nowIso(),
    };
    return {
        success: true,
        article: articleObj,
        // 顶层也带 (兼容其他读取方式)
        ...articleObj,
    };
}

// ===== 6 个代发媒介 · 字段对齐生产 MhzMediaItem · 取典型门户/自媒体作为演示 =====
const SANDBOX_PROXY_MEDIA = [
    {
        id: 50001, media_name: '搜狐号·汽车出行频道', price: 320, price1: 320, price2: 480,
        resource_type: '门户网站', resource_type_name: '门户内频道', area: '全国', portal_media: '搜狐',
        inclusion_rate: '85%', publish_rate: '95%', avg_time: 6, pc_weight: 7, m_weight: 8,
        news_resource: 1, link_type: 1, remark: '搜狐母频道, 百度收录快, 适合行业横评',
        description: 'PC 端 SEO 收录强 · AI 搜索引用率高', url: '#', recommended_for_article: 31001,
    },
    {
        id: 50002, media_name: '新浪汽车·出行综合频道', price: 520, price1: 520, price2: 780,
        resource_type: '门户网站', resource_type_name: '门户内频道', area: '全国', portal_media: '新浪',
        inclusion_rate: '92%', publish_rate: '98%', avg_time: 4, pc_weight: 8, m_weight: 8,
        news_resource: 1, link_type: 1, remark: '新浪一级频道, 权威度高',
        description: '新浪权威媒体 · 适合品牌背书类文章', url: '#', recommended_for_article: 31002,
    },
    {
        id: 50003, media_name: '凤凰网·商务出行', price: 280, price1: 280, price2: 420,
        resource_type: '门户网站', resource_type_name: '门户内频道', area: '全国', portal_media: '凤凰',
        inclusion_rate: '88%', publish_rate: '96%', avg_time: 8, pc_weight: 7, m_weight: 7,
        news_resource: 1, link_type: 1, remark: '凤凰旗下, 适合商务案例分享',
        description: '门户级品牌曝光', url: '#', recommended_for_article: 31003,
    },
    {
        id: 50004, media_name: '百家号·汽车出行原创号', price: 350, price1: 350, price2: 500,
        resource_type: '自媒体', resource_type_name: '百家号', area: '全国', portal_media: '百度',
        inclusion_rate: '95%', publish_rate: '99%', avg_time: 2, pc_weight: 9, m_weight: 9,
        news_resource: 1, link_type: 1, remark: '百度系最高权重, AI 搜索引用首选',
        description: '百度系收录最快 · AI 引用首选', url: '#', recommended_for_article: 31004,
    },
    {
        id: 50005, media_name: '今日头条·汽车出行号', price: 380, price1: 380, price2: 550,
        resource_type: '自媒体', resource_type_name: '头条号', area: '全国', portal_media: '今日头条',
        inclusion_rate: '90%', publish_rate: '97%', avg_time: 3, pc_weight: 8, m_weight: 9,
        news_resource: 1, link_type: 1, remark: '头条原创号, 移动端流量大',
        description: '头条系移动端推荐', url: '#', recommended_for_article: 31005,
    },
    {
        id: 50006, media_name: '腾讯网·新闻中心', price: 850, price1: 850, price2: 1200,
        resource_type: '门户网站', resource_type_name: '门户首页', area: '全国', portal_media: '腾讯',
        inclusion_rate: '98%', publish_rate: '99%', avg_time: 4, pc_weight: 10, m_weight: 9,
        news_resource: 1, link_type: 1, remark: '腾讯权威新闻源, 收录率最高',
        description: '腾讯新闻权威源 · 收录最快', url: '#', recommended_for_article: 31006,
    },
];

/** GET /api/publish/media · 代发媒介列表 · 同时返 status='success' (PublishCenter 用这个判) + success */
export function getSandboxPublishMediaList() {
    return {
        status: 'success',
        success: true,
        media: SANDBOX_PROXY_MEDIA,
        total: SANDBOX_PROXY_MEDIA.length,
        pages: 1,
    };
}

/** GET /api/publish/media/recommend-v2?industry=X · 行业推荐 · 用于 PublishCenter
 *  返回 media_vertical / media_generic / wemedia_vertical / wemedia_generic
 *  沙盒里全 6 个媒介都塞 media_vertical (代发软文垂直推荐) */
export function getSandboxPublishMediaRecommend() {
    const mapMedia = (m: typeof SANDBOX_PROXY_MEDIA[number]) => ({
        media_id: m.id,
        id: m.id,
        media_name: m.media_name,
        price: m.price,
        resource_type: m.resource_type,
        description: m.description,
        url: m.url,
        recommendation_score: 95,
    });
    // [媒体平衡 2026-07-29] 沙盒也要能走通「AI 最常引用的网站」+「建议怎么搭配」两块,
    // 否则教程里这两块永远不出现,前端改动也没法在沙盒下验收。
    const all = SANDBOX_PROXY_MEDIA.map(mapMedia);
    const isBigPlatform = (n: string) => ['搜狐', '新浪', '网易', '头条', '知乎', '博客园'].some(k => n.includes(k));
    return {
        status: 'success',
        success: true,
        matched_industry: DEMO_INDUSTRY,
        media_vertical: all.filter(m => !isBigPlatform(m.media_name)),
        media_generic: all.filter(m => isBigPlatform(m.media_name)),
        wemedia_vertical: [],
        wemedia_generic: [],
        ai_citation_trunk: {
            window_days: 180,
            total_citations: 5238,
            degraded_reason: '',
            domains: [
                { domain: 'sohu.com', citation_count: 1969, strength_label: '引用强', role_label: '大平台', self_serve: false, inventory_keyword: '搜狐' },
                { domain: '163.com', citation_count: 1483, strength_label: '引用强', role_label: '大平台', self_serve: false, inventory_keyword: '网易' },
                { domain: 'cnblogs.com', citation_count: 961, strength_label: '引用中', role_label: '技术社区', self_serve: true, inventory_keyword: '博客园' },
                { domain: 'zhihu.com', citation_count: 619, strength_label: '引用中', role_label: '问答和公众号', self_serve: true, inventory_keyword: '知乎' },
                { domain: 'toutiao.com', citation_count: 425, strength_label: '引用中', role_label: '大平台', self_serve: false, inventory_keyword: '头条' },
            ],
        },
        combination_plan: {
            advisory: true,
            trunk_slots: 2,
            vertical_slots: 3,
            reason: '客户问「新能源车怎么选」这类问题时，AI 最常引用 sohu.com 38%、cnblogs.com 21%、zhihu.com 18%（近 180 天共 131 次引用）',
            slots: [],
            substitutions: [
                { domain: 'cnblogs.com', substitution_note: 'AI 常引用的 cnblogs.com（技术社区）我们暂时买不到发布位，已换成同类的 CSDN' },
            ],
            self_serve_offers: [
                { domain: 'zhihu.com', self_serve_action: { id: 'self_serve_publish', label: '用自己的知乎账号发（内容我们出）', target: 'publish_center_self_serve' } },
            ],
        },
    };
}

/** POST /api/publish/articles/{id}/media-recommendation · 单篇文章 AI 媒体推荐
 *  PublishCenter 选完文章 1.5s 自动触发 (CTO-15.23 合并 main 时进来的新功能)
 *  沙盒没 mock 会打真后端 → "推荐生成失败" toast · 这里补 mock · 复用 6 个代发媒介
 *  返回结构对齐前端 loadDeepAnalysis: status/article_summary/packages[].items[] */
export function getSandboxArticleMediaRecommendation() {
    const items = SANDBOX_PROXY_MEDIA.map((m, i) => ({
        media_id: m.id,
        media_name: m.media_name,
        platform_name: m.portal_media,
        tier: m.resource_type_name,
        price_yuan: m.price,
        cost_points: m.price,
        fit_score: 95 - i * 2,
        media_source: m.resource_type === '自媒体' ? 'wemedia' : 'portal',
        evidence: [m.remark, m.description].filter(Boolean),
        risk_note: '',
    }));
    return {
        status: 'success',
        article_summary: {
            article_type_label: '行业横评',
            industry: DEMO_INDUSTRY,
            semantic_keywords: [...SANDBOX_MAGIC_KEYWORDS, '高端商务用车', '专业司机'],
            publish_goal: '面向 AI 搜索引用 · 优先权威门户 + 高收录自媒体, 提升品牌在 AI 回答里的出现率',
            recommendation_level: 3,
        },
        packages: [
            {
                title: '均衡组合 · 权威门户 + 高收录自媒体',
                package_type: 'balanced',
                items,
            },
        ],
        fallback_notice: '',
    };
}

/** GET /api/placement/articles/{quote_id} · PublishCenter 用这个拉文章列表 */
export function getSandboxPlacementArticles() {
    ensureWritingStageInited();
    if (sandboxWriting.stage !== 'done' && sandboxWriting.stage !== 'in_cart' && sandboxWriting.stage !== 'submitted') {
        return { success: true, articles: [] };
    }
    const articles: Array<Record<string, unknown>> = Object.entries(SANDBOX_ARTICLE_CONTENTS).map(([idStr, a]) => {
        const id = parseInt(idStr, 10);
        return {
            id,
            topic_id: id - 1000, // 30001-30006
            title: a.title,
            keyword: a.keyword,
            article_id: id, // 在 PublishCenter ArticleItem 里 article_id 是关键
            article_style: 'guide',
            status: 'completed',
            is_optimize: false,
        };
    });
    // step 4 补发: 补发文写完后(opt_done/recovered)· 把 2 篇补发文也放进发布列表
    if (sandboxWriting.optimizeStage === 'opt_done' || sandboxWriting.optimizeStage === 'recovered') {
        for (const t of SANDBOX_OPT_TOPICS) {
            const aid = t.id + 1000; // 50001 / 50002
            articles.push({
                id: aid,
                topic_id: t.id,
                title: t.optimized_title,
                keyword: SANDBOX_OPT_KEYWORD,
                article_id: aid,
                article_style: t.article_style,
                status: 'completed',
                is_optimize: true,
            });
        }
    }
    return { success: true, articles };
}

/** 沙盒里加购物车 · 推 stage → in_cart (当 6 篇都加完) */
export function sandboxAddToCart(articleId: number) {
    sandboxWriting.cartItemIds.add(articleId);
    if (sandboxWriting.cartItemIds.size >= 6) {
        sandboxWriting.stage = 'in_cart';
    }
    return { success: true, in_cart_count: sandboxWriting.cartItemIds.size };
}

/** POST /api/publish/decision-snapshots · 投放决策快照 · "继续投放"前先存一份
 *  (CTO-15.23 合并 main 带来的新步骤)· 前端要 status='success' + snapshot_id 才继续发布
 *  沙盒不 mock 会落到通用兜底(没 snapshot_id)→ "投放确认保存失败" alert · 这里补 mock */
export function getSandboxPublishDecisionSnapshot() {
    const id = Date.now();
    return {
        status: 'success',
        success: true,
        snapshot_id: id,
        publish_request_id: 'sandbox-pub-' + id.toString(36),
    };
}

/** POST /api/meijiehezi/publish/batch · 教程发布成功。
 *  教程只练真实 UI 操作，不创建订单、审核、外发或计费副作用。 */
export function getSandboxMeijieheziPublishBatch() {
    sandboxWriting.stage = 'submitted';
    if (typeof window !== 'undefined') {
        window.setTimeout(() => {
            window.dispatchEvent(new CustomEvent('sandbox:step3-published', {
                detail: {
                    article_count: 6,
                    provider_calls: 0,
                    billed_points: 0,
                    approval_tasks: 0,
                },
            }));
        }, 600);
    }
    return {
        status: 'success',
        success: true,
        request_id: 'sandbox-req-' + Math.random().toString(36).slice(2, 8),
        total_articles: 6,
        charged_points: 0,
        billed_points: 0,
        provider_calls: 0,
        approval_tasks: 0,
        tutorial: true,
        message: '模拟发布完成 · 0 算力、0 外发、无需管理员审批',
    };
}

/** GET /api/meijiehezi/awaiting-confirmations · 待确认列表
 *  主教程永远为空：外发法律修复属于真实发布边界，不是普通用户教程必经步骤。 */
export function getSandboxAwaitingConfirmations() {
    return { status: 'success', items: [], total: 0 };
}

/** GET /api/meijiehezi/awaiting-confirmations/{itemId}/detail · 待确认详情 (含文章原文 + 敏感词列表) */
export function getSandboxAwaitingDetail(itemId: number) {
    if (itemId !== 60004) {
        return { status: 'error', detail: '未找到该待确认项' };
    }
    const a = SANDBOX_ARTICLE_CONTENTS[31004];
    return {
        status: 'success',
        item: {
            item_id: 60004,
            order_id: 70001,
            article_id: 31004,
            article_title: '深圳豪车配司机哪家好? 4 家深圳豪车租赁公司实测体验对比',
            media_id: 50004,
            media_name: '百家号·汽车出行原创号',
            media_type: 'mhz',
            brand_id: SANDBOX_BRAND_ID,
            pending_codes: [203],
            pending_msg: '对外发布前发现 1 处广告法绝对化表达「最便宜」；可只修改这一处后继续，其余内容保持不变',
            awaiting_since: nowIso(),
        },
        article: {
            id: 31004,
            content: a.content,
        },
        sensitive_keywords: ['最便宜'],
    };
}

/** POST /api/meijiehezi/confirm/{itemId} · 用户在 dialog 里点 "保存并仍然发布" / "取消订单"
 *  body: { action: 'confirm' | 'cancel' }
 *  沙盒: 标记 resolved · awaiting 列表下次返空 · 触发 step3-published 让 video modal 弹 */
export function getSandboxConfirmAwaiting() {
    if (typeof window !== 'undefined') {
        setTimeout(() => {
            window.dispatchEvent(new CustomEvent('sandbox:step3-published', {
                detail: { article_count: 6 },
            }));
        }, 800);
    }
    return { status: 'success', refunded_points: 0 };
}

/** GET /api/meijiehezi/synced-orders · 历史 · 沙盒返 6 条已发成功 */
export function getSandboxMeijieheziSyncedOrders() {
    if (sandboxWriting.stage !== 'submitted') return { success: true, items: [], total: 0 };
    const items = Object.entries(SANDBOX_ARTICLE_CONTENTS).map(([idStr, a], i) => {
        const id = parseInt(idStr, 10);
        const media = SANDBOX_PROXY_MEDIA.find(m => m.recommended_for_article === id);
        return {
            id: 60000 + i,
            order_sn: 'SANDBOX-' + (60000 + i),
            title: a.title,
            media_name: media?.media_name || '未知媒介',
            resource_id: media?.id || 0,
            price: media?.price || 0,
            status: 1, // 发布中
            url: '',
            created_at: nowIso(),
            published_at: null,
            order_remark: '教程模式 · 演示用',
        };
    });
    return { success: true, items, total: items.length };
}

/** 沙盒退出 / 重置 step 3 状态 */
export function resetSandboxWritingState() {
    sandboxWriting.stage = 'idle';
    sandboxWriting.writeStartTs = 0;
    sandboxWriting.cartItemIds.clear();
    sandboxWriting.optimizeStage = 'none';
    sandboxWriting.optWriteStartTs = 0;
}

// ==========================================
// step 4 监测出现率 + 补救闭环
// 设计: 快进 10 天演示数据 · 6 个词(2 魔法词达标呼应视频 + 4 陪衬 + 1 坏 case 未达标)
// 达标线 50%(入门档) · lifecycle 全 monitoring · source 全 confirmed(签约自动进监测)
// ==========================================

const SANDBOX_MONITOR_QUOTE_ID = 999001;
const SANDBOX_FAST_FORWARD_DAYS = 10;
const SANDBOX_MONITOR_SERVICE_DAYS = 30;   // 按月卖 · 一期 30 天

/** N 天前的 ISO 时间(服务起算日 = 快进前) */
function daysAgoIso(n: number): string {
    return new Date(Date.now() - n * 86400000).toISOString();
}

/** 6 个监测词 · 出现率/达标按设计稿 · id 用 7200x */
// 监测词条 = 客户签约的 3 个词 (跟报价/文章一致, 不多不少)
const SANDBOX_MONITOR_KEYWORDS = [
    { id: 72001, keyword: SANDBOX_MAGIC_KEYWORDS[0], rate: 81 }, // 深圳租埃尔法配司机的公司 · 核心词 · 达标
    { id: 72002, keyword: SANDBOX_MAGIC_KEYWORDS[1], rate: 74 }, // 深圳豪车配司机哪家好 · 核心词 · 达标
    { id: 72006, keyword: SANDBOX_LAGGARD_KEYWORD,   rate: 35 }, // 深圳埃尔法长租包月 · 长尾词 · 未达标 → 补发救回
];

const SANDBOX_MONITOR_TARGET_RATE = 50;

/** GET /api/monitoring/clients · 沙盒返 1 个客户(一路顺风) */
export function getSandboxMonitoringClients() {
    return {
        status: 'success',
        clients: [{
            quote_id: SANDBOX_MONITOR_QUOTE_ID,
            brand_id: SANDBOX_BRAND_ID,
            brand_name: DEMO_BRAND_NAME,
            industry: DEMO_INDUSTRY,
            /*
             * [WO_251 §4] 教程也要走**现值**那条路 —— 沙盒是第二个后端,
             * 不补这两列的话,教程里永远只执行"字段缺失 ⇒ 回落快照"那一支。
             * 🔴 这里**故意与快照取同值**:教程要的是一份前后一致的演示,
             *    而"现值 ≠ 快照"那种有分辨力的局面属于判据
             *    (`verify-client-display.mjs`),不属于给人看的教程。
             *    ⇒ 沙盒证的是这条路**跑到了**,不是它**分得清** —— 两件事,别混。
             */
            brand_current_name: DEMO_BRAND_NAME,
            brand_current_industry: DEMO_INDUSTRY,
            tier: 'entry',
            tier_name: '入门版',
            keyword_count: SANDBOX_MONITOR_KEYWORDS.length,
            status: 'active',
            service_days: SANDBOX_MONITOR_SERVICE_DAYS,
            service_start_date: daysAgoIso(SANDBOX_FAST_FORWARD_DAYS),
        }],
    };
}

/** GET /api/monitoring/clients/{quoteId}/keywords · 沙盒返 6 个词(快进 10 天形态)
 *  补发完成后(optimizeStage=recovered)· 坏 case 词「深圳埃尔法长租包月」从 35% 恢复到 58% 达标 */
export function getSandboxMonitoringKeywords() {
    const recovered = sandboxWriting.optimizeStage === 'recovered';
    const fastForwardDays = recovered ? SANDBOX_FAST_FORWARD_DAYS + 8 : SANDBOX_FAST_FORWARD_DAYS;
    const keywords = SANDBOX_MONITOR_KEYWORDS.map(k => {
        // 补发后坏 case 词出现率回升
        const rate = (recovered && k.id === 72006) ? 58 : k.rate;
        const isCompliant = rate >= SANDBOX_MONITOR_TARGET_RATE;
        return {
            id: k.id,
            keyword: k.keyword,
            target_brand: DEMO_BRAND_NAME,
            source: 'confirmed',           // 签约自动进监测
            detection_rate: rate,
            effective_rate: rate,
            rate_change: (recovered && k.id === 72006) ? 23 : (isCompliant ? 8 : 3), // 补发后坏词大涨
            lifecycle: 'monitoring' as const,
            target_rate: SANDBOX_MONITOR_TARGET_RATE,
            is_compliant: isCompliant,
            compliance_progress: Math.min(100, Math.round((rate / SANDBOX_MONITOR_TARGET_RATE) * 100)),
            remaining_days: SANDBOX_MONITOR_SERVICE_DAYS - fastForwardDays,
            compliant_days: isCompliant ? 4 : 0,
            service_days: SANDBOX_MONITOR_SERVICE_DAYS,
            total_tests: 40,
            window_tests: 28,
            first_detected_date: daysAgoIso(fastForwardDays),
            last_tested: nowIso(),
            is_core: k.id <= 72002,
        };
    });
    return {
        status: 'success',
        keywords,
        service_days: SANDBOX_MONITOR_SERVICE_DAYS,
        service_start_date: daysAgoIso(SANDBOX_FAST_FORWARD_DAYS),
    };
}

function sandboxSupplementPlanHash(keywordId: number): string {
    const seed = Math.max(0, Number(keywordId) || 0).toString(16).padStart(8, '0');
    return seed.repeat(8).slice(0, 64);
}

/** GET /api/writing/optimize-preview/{keywordId}
 *  教程只在浏览器本地生成可执行建议；不调用 provider、不查真实客户数据，也不扣算力。 */
export function getSandboxOptimizePreview(keywordId: number) {
    const matched = SANDBOX_MONITOR_KEYWORDS.find((item) => item.id === keywordId);
    const keyword = matched?.keyword || SANDBOX_LAGGARD_KEYWORD;
    const recentRate = matched?.rate ?? 35;
    const suggestedArticles = recentRate < SANDBOX_MONITOR_TARGET_RATE ? 2 : 0;
    const stylePlan = suggestedArticles > 0
        ? { guide: 1, comparison: 1 }
        : {};

    return {
        status: 'success',
        preview: {
            keyword_id: keywordId,
            keyword,
            quote_id: SANDBOX_MONITOR_QUOTE_ID,
            suggested_articles: suggestedArticles,
            reason_code: suggestedArticles > 0 ? 'rate_gap' : 'on_target',
            reason_text: suggestedArticles > 0
                ? `原计划已完成，但近 7 天出现率 ${recentRate}% 低于目标 ${SANDBOX_MONITOR_TARGET_RATE}%`
                : '原计划已完成且近 7 天出现率已达标，暂不建议补发',
            style_plan: stylePlan,
            estimated_points: 0,
            plan_version: 'sandbox-supplement-plan-v1',
            plan_hash: sandboxSupplementPlanHash(keywordId),
            target_rate: SANDBOX_MONITOR_TARGET_RATE,
            recent_rate: recentRate,
        },
        provider_calls: 0,
        billed_points: 0,
    };
}

/** GET /api/monitoring/trend · 沙盒返 10 天爬升曲线(整体出现率) */
export function getSandboxMonitoringTrend() {
    const data = [];
    for (let i = SANDBOX_FAST_FORWARD_DAYS; i >= 0; i--) {
        const d = new Date(Date.now() - i * 86400000);
        // 从 ~8% 爬到 ~60%(整体均值), 前几天铺量低、后面上来
        const progress = (SANDBOX_FAST_FORWARD_DAYS - i) / SANDBOX_FAST_FORWARD_DAYS;
        const rate = Math.round(8 + progress * 52);
        data.push({ date: `${d.getMonth() + 1}/${d.getDate()}`, rate });
    }
    return { status: 'success', trend: data };
}

/** 客户门户白标 token · 教程收尾步用 · 让代理学会"给客户发实时数据链接"
 *  token 固定值, expires 30 天后, is_active=1 (有效) */
function buildSandboxPortalToken() {
    return {
        token: 'sandbox-portal-mazhaosuccess',
        expires_at: new Date(Date.now() + 30 * 86400000).toISOString(),
        is_active: 1,
        quote_id: 999001,
        brand_name: DEMO_BRAND_NAME,
    };
}

/** GET /api/portal/tokens/{quoteId} · 拉客户门户 token · 沙盒直接返一个有效 token (打开弹窗即显示) */
export function getSandboxPortalTokenGet() {
    return { status: 'success', token: buildSandboxPortalToken() };
}

/** POST /api/portal/tokens · 生成/重新生成客户门户 token · 沙盒返同一个 demo token */
export function getSandboxPortalTokenCreate() {
    return { status: 'success', data: buildSandboxPortalToken() };
}

/** POST /api/writing/optimize-generate · 沙盒不真生成 · 标记进入补发态 · 返成功让前端跳写作页 optimize tab
 *  补发逻辑上发生在"主文已发布之后", 所以把写作 stage 顶到 submitted —
 *  否则脚本直跳 step4 时 stage 还停在 in_hall, projectDetail 会提前 return 空 topics, 补发列表出不来 */
export function getSandboxOptimizeGenerate(request?: {
    keyword_id?: number;
    plan_version?: string;
    plan_hash?: string;
}) {
    sandboxWriting.stage = 'submitted';
    sandboxWriting.optimizeStage = 'generated';
    const keywordId = Number(request?.keyword_id || 72006);
    const planVersion = request?.plan_version || 'sandbox-supplement-plan-v1';
    const planHash = request?.plan_hash || sandboxSupplementPlanHash(keywordId);
    return {
        status: 'success',
        keyword_id: keywordId,
        quote_id: SANDBOX_MONITOR_QUOTE_ID,
        topic_ids: [73901, 73902],
        created: 2,
        plan_version: planVersion,
        plan_hash: planHash,
        provider_calls: 0,
        billed_points: 0,
        message: '已为未达标词条创建补发任务 (教程模式不真生成)',
    };
}

/** step 4 补发发布成功 → 标记坏 case 词已"恢复达标"(再快进几天) · 让监测页那行变绿达标 */
export function sandboxMarkOptimizeRecovered() {
    sandboxWriting.optimizeStage = 'recovered';
}

/** 当前是否在补发子流程中 (供 UI 判断) */
export function getSandboxOptimizeStage() {
    return sandboxWriting.optimizeStage;
}

// ============================================================
// [§13 出口可达性 2026-07-26] 挂起等用户确认的发布任务
//
// 两份合同刻意用**两个不同的 reject_code**（代发卡单 + 内容漂移）：
// 通用渲染器必须两份都渲染正确，而组件里不能出现任何 reject_code 分支。
// 第三份是"合同缺失/动作认不出"的兜底样本 —— 必须仍渲染出人话 + 联系客服，
// 不能出现空白卡片（那是把死胡同从后端搬到前端）。
// ============================================================

let sandboxPendingActionResolved: Record<number, string> = {};

/** POST 出口动作后标记该条已表态，列表随即变成"等待人工核实"。 */
export function markSandboxPendingActionClaimed(itemId: number, claim: string) {
    sandboxPendingActionResolved[itemId] = claim;
}

export function resetSandboxPendingActions() {
    sandboxPendingActionResolved = {};
}

/** 治理合同独立样本。保留给组件合同测试，不进入普通教程主路径。 */
export function getSandboxPendingUserActionExamples() {
    const claimOf = (id: number) => sandboxPendingActionResolved[id] || null;
    const claimAt = (id: number) => (sandboxPendingActionResolved[id] ? nowIso() : null);
    const items = [
        {
            item_id: 339, order_id: 300,
            media_name: '咸宁新闻网（GEO可发排名）', media_type: 'mhz',
            cost_points: 3900, status: 'awaiting_action',
            reject_code: 'PUBLISH_AWAITING_SYNC_UNRESOLVED',
            reject_user_message: '这条发到「咸宁新闻网」的稿件, 平台一直没拿到发布回执, 需要你确认一下。',
            reject_contract: {
                code: 'PUBLISH_AWAITING_SYNC_UNRESOLVED',
                message: '这条发到「咸宁新闻网」的稿件, 平台一直没拿到发布回执, 需要你确认一下。',
                reason: '媒体方当时回执了「已接收」, 但没有返回可追踪的订单号, 已等待 19 天, 平台无法自动判断稿件最终有没有发出去。',
                impact: '这条发布任务停在这里, 没有再重复提交、也没有再扣新的费用; 原扣的算力仍在占用中。',
                repair_hint: '如果你在该媒体上没找到这篇稿件, 点「确认未发布 · 退还算力」, 我们人工核实后按原路退回算力; 如果已经发出来了, 点「已发布 · 补录链接」把链接补上即可结案。',
                rule_version: 'publish-awaiting-sync-exit-v1',
                actions: [
                    { id: 'report_not_published', type: 'api', method: 'POST', label: '确认未发布 · 退还算力', target: '/api/meijiehezi/items/339/report-not-published' },
                    { id: 'record_publish_url', type: 'api', method: 'POST', label: '已发布 · 补录链接', target: '/api/meijiehezi/items/339/record-publish-url', fields: [{ name: 'publish_url', type: 'url', label: '稿件链接', required: true, placeholder: 'https://...' }] },
                    { id: 'view_orders', type: 'nav', label: '查看发布任务', target: '/publishing' },
                ],
            },
            user_exit_claim: claimOf(339), user_exit_claim_at: claimAt(339),
            awaiting_sync_since: '2026-07-07T17:38:04.211420',
            article_id: 1107, article_title: '2026年深圳口碑装修公司推荐榜TOP8',
        },
        {
            item_id: 412, order_id: 351,
            media_name: '网易号·家居频道', media_type: 'wemedia',
            cost_points: 2600, status: 'awaiting_action',
            reject_code: 'PUBLISH_CONTENT_DRIFT',
            reject_user_message: '系统在准备期间优化了这篇稿件, 需要重新确认一次即可发布。',
            reject_contract: {
                code: 'PUBLISH_CONTENT_DRIFT',
                message: '系统在准备期间优化了这篇稿件, 需要重新确认一次即可发布。',
                reason: '稿件的正文在下单后被系统自动优化过, 与下单时冻结的版本不一致。',
                impact: '本次没有发布, 也没有扣新的费用; 原订单已挂起等你确认。',
                repair_hint: '点「重新准备并审核」, 系统会用最新稿件重新生成一份待发版本并送审, 通过后即可再次提交。',
                rule_version: 'publish-content-drift-v1',
                actions: [
                    { id: 'reprepare_and_review', type: 'api', method: 'POST', label: '重新准备并审核', target: '/api/meijiehezi/orders/351/re-prepare' },
                    { id: 'view_article', type: 'nav', label: '先看看稿件', target: '/writing?article_id=1204' },
                ],
            },
            user_exit_claim: claimOf(412), user_exit_claim_at: claimAt(412),
            awaiting_sync_since: null,
            article_id: 1204, article_title: '2026 全屋定制怎么选 · 5 家主流品牌横评',
        },
        {
            // 兜底样本: 合同缺失 + 动作是认不出的 type → 仍要渲染出人话与"联系客服"
            item_id: 999, order_id: 380,
            media_name: '某地方门户', media_type: 'mhz',
            cost_points: 1200, status: 'awaiting_action',
            reject_code: 'PUBLISH_UNKNOWN_FUTURE_CODE',
            reject_user_message: '这条发布任务遇到了平台还没归类的情况, 需要人工看一下。',
            reject_contract: { code: 'PUBLISH_UNKNOWN_FUTURE_CODE', message: '这条发布任务遇到了平台还没归类的情况, 需要人工看一下。', reason: '', impact: '', actions: [{ id: 'weird', type: 'telepathy', label: '心灵感应' }] },
            user_exit_claim: claimOf(999), user_exit_claim_at: claimAt(999),
            awaiting_sync_since: null,
            article_id: 1301, article_title: '装修避坑指南 2026 版',
        },
    ];
    return { status: 'success', items, total: items.length };
}

/** GET /api/meijiehezi/pending-user-actions · 教程不注入生产事故或治理待办。 */
export function getSandboxPendingUserActions() {
    return { status: 'success', success: true, items: [], total: 0 };
}

/** POST /api/meijiehezi/items/{id}/report-not-published · 申报未发布(只表态, 不动钱) */
export function getSandboxReportNotPublished(itemId: number) {
    markSandboxPendingActionClaimed(itemId, 'not_published');
    return { status: 'success', success: true, claim: 'not_published', refunded: 0, message: '已收到你的确认, 我们会人工核实媒体侧是否真的没有发布, 核实后按原路退回算力。' };
}

/** POST /api/meijiehezi/items/{id}/record-publish-url · 补录链接结案 */
export function getSandboxRecordPublishUrl(itemId: number) {
    markSandboxPendingActionClaimed(itemId, 'published');
    return { status: 'success', success: true, claim: 'published', refunded: 0, message: '已按「已发布」结案, 链接已补录。' };
}
