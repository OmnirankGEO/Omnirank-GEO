/**
 * 沙盒 axios 拦截器 (Sandbox Mode)
 * Stage 1 Batch 3 (2026-05-08)
 *
 * 工作原理:
 *   1. installSandboxInterceptor(instance) 给任意 axios 实例打上 request + response 拦截器
 *   2. request 拦截器: 沙盒态时匹配 URL → 命中规则 reject 一个带 marker 的特殊错误
 *      (因为 axios 不允许 request interceptor 直接 short-circuit 返响应)
 *   3. response error 拦截器: 识别 marker → 把 mock payload 包装成 200 返给调用方
 *      调用方拿到 res.data 跟真实接口完全一致
 *
 * 必须装在两个实例上:
 *   - api      (lib/api.ts)        · /api/m3/customers /api/brands 等
 *   - authApi  (context/AuthContext) · /api/my-clients /api/my-brand 等
 *
 * 没匹中规则的 API 一律在浏览器本地 fail-closed, 不触达真实业务后端。
 * 只有精确登记的只读会话/权限接口可以透传。
 */
import type { AxiosInstance, InternalAxiosRequestConfig, AxiosResponse } from 'axios';
import { isSandboxActive } from './sandboxState';
import {
    getSandboxMyClientsList,
    getSandboxMyClientsCreate,
    getSandboxMyClientDetail,
    getSandboxM3Customers,
    getSandboxBrandCompleteness,
    getSandboxMyBrand,
    getSandboxBrandList,
    getSandboxBrandDetail,
    getSandboxClientContextList,
    getSandboxAiFillResponse,
    getSandboxBrandSaveResponse,
    getSandboxDiagnosisAutofillResponse,
    getSandboxClientContextDetail,
    getSandboxDiagnosisStart,
    buildSandboxDiagnosisPricePreview,
    getSandboxSuggestQuestions,
    getSandboxDiagnosisProgress,
    getSandboxDiagnosisSessionStatus,
    getSandboxDiagnosisDetail,
    getSandboxDiagnosisContent,
    getSandboxQuoteList,
    getSandboxQuoteCreate,
    getSandboxQuoteSession,
    getSandboxSelectionSubmitBusinessLines,
    getSandboxSelectionSubmitKeywords,
    getSandboxSelectionConfirmQuote,
    getSandboxQuoteGenerate,
    getSandboxQuoteApprove,
    getSandboxQuoteMarkPaid,
    getSandboxQuoteSalesConfirm,
    getSandboxKeywordsExpand,
    getSandboxHistoryForQuote,
    getSandboxRetestData,
    getSandboxQuotesList,
    // step 2 quick path
    // 2026-05-25 删:快速录单路径已废弃 (getSandboxDistill / getSandboxKeywordsQuote /
    //                getSandboxOfflineConfirm / getSandboxOfflineMarkPaid 全删)
    // step 3
    getSandboxWritingProjects,
    getSandboxWritingProjectDetail,
    getSandboxWritingKnowledgeStatus,
    getSandboxKnowledgeFiles,
    getSandboxResearchCompetitors,
    getSandboxVerifyCompetitors,
    getSandboxCompetitorsMode,
    getSandboxCompetitorsList,
    getSandboxGenerateTitles,
    getSandboxStartArticles,
    getSandboxWritingProgress,
    getSandboxArticlesList,
    getSandboxArticleDetail,
    getSandboxPublishMediaList,
    getSandboxPublishMediaRecommend,
    getSandboxArticleMediaRecommendation,
    getSandboxPlacementArticles,
    getSandboxPublishDecisionSnapshot,
    getSandboxMeijieheziPublishBatch,
    getSandboxAwaitingConfirmations,
    getSandboxPendingUserActions,
    getSandboxReportNotPublished,
    getSandboxRecordPublishUrl,
    getSandboxAwaitingDetail,
    getSandboxConfirmAwaiting,
    getSandboxMeijieheziSyncedOrders,
    // step 4 监测
    getSandboxMonitoringClients,
    getSandboxMonitoringKeywords,
    getSandboxMonitoringTrend,
    getSandboxOptimizePreview,
    getSandboxOptimizeGenerate,
    getSandboxPortalTokenGet,
    getSandboxPortalTokenCreate,
    SANDBOX_BRAND_ID,
} from './mockData';

const MARKER = '__SANDBOX_MOCK__';

interface SandboxMockMeta {
    [MARKER]: true;
    payload: unknown;
    status: number;
    statusText: string;
}

type RuleFn = (config: InternalAxiosRequestConfig) => unknown;

interface Rule {
    test: (method: string, url: string) => boolean;
    build: RuleFn;
    /** Stage 1 Batch 4 (2026-05-18) · 模拟真实接口耗时, 让用户看到"思考中" / "处理中"动画
     *  默认 0 立刻返 · LLM 类接口建议 1500-3000ms */
    delayMs?: number;
}

interface SandboxAction {
    id: 'retry' | 'continue_tutorial' | 'exit_sandbox';
    label: string;
}

const RULES: Array<Rule> = [
    // === authApi 走的接口 ===
    {
        test: (m, u) => m === 'GET' && /^\/api\/notifications(\?|$|\/)/.test(u),
        build: () => ({ success: true, items: [], notifications: [], unread_count: 0, total: 0 }),
    },
    // 客户列表 (MyClientsPage 主拉)
    {
        test: (m, u) => m === 'GET' && /^\/api\/my-clients(\?|$)/.test(u),
        build: () => getSandboxMyClientsList(),
    },
    // 创建客户 (step 1 触发点)
    {
        test: (m, u) => m === 'POST' && u === '/api/my-clients',
        build: () => getSandboxMyClientsCreate(),
    },
    // 删除客户 (沙盒里点删除直接返成功 · 不真删)
    {
        test: (m, u) => m === 'DELETE' && /^\/api\/my-clients\/\d+(\?|$)/.test(u),
        build: () => ({ success: true, message: '已删除 (教程模式)' }),
    },
    // 单客户详情 (跳详情页时拉)
    {
        test: (m, u) => m === 'GET' && /^\/api\/my-clients\/\d+(\?|$)/.test(u),
        build: () => getSandboxMyClientDetail(),
    },
    // BrandDetailPage 加载
    {
        test: (m, u) => m === 'GET' && /^\/api\/my-brand(\?|$)/.test(u),
        build: () => getSandboxMyBrand(),
    },
    // AI 一键补齐 (step 2 触发点)
    {
        test: (m, u) => m === 'POST' && /^\/api\/profiles\/ai-fill(\?|$)/.test(u),
        build: () => getSandboxAiFillResponse(),
    },
    // 保存档案 (PUT)
    {
        test: (m, u) => m === 'PUT' && /^\/api\/my-clients\/\d+(\?|$)/.test(u),
        build: () => getSandboxBrandSaveResponse(),
    },
    {
        test: (m, u) => m === 'PUT' && /^\/api\/my-brand(\?|$)/.test(u),
        build: () => getSandboxBrandSaveResponse(),
    },

    // === api 走的接口 ===
    // M3 BFF 客户列表
    {
        test: (m, u) => m === 'GET' && /^\/api\/m3\/customers(\?|$)/.test(u),
        build: () => getSandboxM3Customers(),
    },
    // 完整度打分
    {
        test: (m, u) => m === 'GET' && /^\/api\/brands\/\d+\/completeness(\?|$)/.test(u),
        build: () => getSandboxBrandCompleteness(),
    },
    // 老接口兼容 (clients-context / brands)
    {
        test: (m, u) => m === 'GET' && u.startsWith('/api/client-context/list'),
        build: () => getSandboxClientContextList(),
    },
    {
        test: (m, u) => m === 'GET' && /^\/api\/client-context\/\d+(\?|$)/.test(u),
        build: () => getSandboxClientContextDetail(),
    },
    {
        test: (m, u) => m === 'GET' && /^\/api\/brands(\?|$)/.test(u),
        build: () => getSandboxBrandList(),
    },
    {
        test: (m, u) => m === 'GET' && /^\/api\/brands\/\d+(\?|$|\/)/.test(u),
        build: () => getSandboxBrandDetail(),
    },

    // === Step 1: 诊断 (新 4 步教学的 step 1, 老 step 3) ===
    // [#176 · 2026-09-12] 🔴 启动页取价。**没有这条规则教程就走不下去**:
    //   启动页自 09-05 起「价只从服务端取」,拿不到 points + pricePreviewId 就闸死
    //   「开始品牌体检」;沙盒里这个接口原来落到未登记分支 ⇒ 501 ⇒ 客户端折成
    //   「这次没能算出价格」,而拦截器那句「尚未接入教程」到不了屏幕。
    //   questionCount 必须回显请求里的数(客户端 priceMatchesInput 比的就是它)。
    {
        test: (m, u) => m === 'POST' && /^\/api\/pricing\/diagnosis-preview(\?|$)/.test(u),
        build: (cfg) => buildSandboxDiagnosisPricePreview(cfg),
    },
    // [#176 §2.2] 候选问题。非阻塞,但 501 会在演示画面上留一句
    //   「这次没能给出候选问题」,顺车接上。side 用后端词 growth/defensive。
    {
        test: (m, u) => m === 'POST' && /^\/api\/diagnosis\/suggest-questions(\?|$)/.test(u),
        build: (cfg) => getSandboxSuggestQuestions(cfg),
    },
    // AI 填写按钮 (NewDiagnosis 页 · 拦截 LLM 调用 · 直接返预设 · 2.5s 模拟思考)
    {
        test: (m, u) => m === 'POST' && /^\/api\/diagnosis\/autofill(\?|$)/.test(u),
        build: () => getSandboxDiagnosisAutofillResponse(),
        delayMs: 2500,
    },
    // 启动诊断 · 立刻返 · 让用户秒跳到进度页看 5s 进度推进
    // Stage 1 Batch 4 fix (2026-05-18) · 之前是 start 卡 5s + status 秒 done
    // 用户体感是按钮转 5s 然后报告秒出, 进度页一闪而过 → 改成 start 立刻返, status 渐进推
    {
        test: (m, u) => m === 'POST' && /^\/api\/diagnosis\/start(\?|$)/.test(u),
        build: () => getSandboxDiagnosisStart(),
    },
    // DiagnosisProgress 页轮询 · 按 elapsed 0%→100% · 总耗时 5 秒
    {
        test: (m, u) => m === 'GET' && /^\/api\/diagnosis\/session\/[^/]+\/status/.test(u),
        build: () => getSandboxDiagnosisSessionStatus(),
    },
    // 老 progress endpoint (备用)
    {
        test: (m, u) => m === 'GET' && /^\/api\/diagnosis\/progress\//.test(u),
        build: () => getSandboxDiagnosisProgress(),
    },
    // 报告内容 (优先于详情, 因为 /content 后缀也匹配 \d+ 那条)
    {
        test: (m, u) => m === 'GET' && /^\/api\/diagnosis\/\d+\/content/.test(u),
        build: () => getSandboxDiagnosisContent(),
    },
    // 当前诊断报告的「疑似提到」人工确认。只在内存里更新演示格, 不写库、不调模型。
    {
        test: (m, u) => m === 'GET' && /^\/api\/diagnosis\/\d+\/brand-cells(\?|$)/.test(u),
        build: (cfg) => buildSandboxDiagnosisBrandCells(cfg),
    },
    {
        test: (m, u) => m === 'POST' && /^\/api\/diagnosis\/\d+\/brand-cells\/decision(\?|$)/.test(u),
        build: (cfg) => buildSandboxDiagnosisBrandDecision(cfg),
    },
    // 诊断记录详情
    {
        test: (m, u) => m === 'GET' && /^\/api\/diagnosis\/\d+(\?|$)/.test(u),
        build: () => getSandboxDiagnosisDetail(),
    },

    // === Step 2 在线报价 (OnlineQuoteFlow) ===
    // 报价会话列表
    {
        test: (m, u) => m === 'GET' && /^\/api\/keyword-selection\/list/.test(u),
        build: () => getSandboxQuoteList(),
    },
    // 创建报价会话 · 模拟客户 3s 后选完关键词
    {
        test: (m, u) => m === 'POST' && u === '/api/keyword-selection/create',
        build: () => getSandboxQuoteCreate(),
        delayMs: 800,
    },
    // 关键词扩展 (AI 自动扩词)
    {
        test: (m, u) => m === 'POST' && u === '/api/keywords/expand',
        build: () => getSandboxKeywordsExpand(),
        delayMs: 1500,
    },
    // 计算报价 (生成三档)
    {
        test: (m, u) => m === 'POST' && /^\/api\/keyword-selection\/[^/]+\/generate-quote/.test(u),
        build: () => getSandboxQuoteGenerate(),
        delayMs: 2000,
    },
    // 销售审核 → 发送给客户 · 模拟客户 3s 后选档位确认
    {
        test: (m, u) => m === 'POST' && /^\/api\/keyword-selection\/[^/]+\/approve-quote/.test(u),
        build: () => getSandboxQuoteApprove(),
        delayMs: 800,
    },
    // 销售代客户确认 (跳过客户操作)
    {
        test: (m, u) => m === 'POST' && /^\/api\/keyword-selection\/[^/]+\/sales-confirm/.test(u),
        build: () => getSandboxQuoteSalesConfirm(),
    },
    // 标记付款 → step 2 完成
    {
        test: (m, u) => m === 'POST' && /^\/api\/keyword-selection\/[^/]+\/mark-paid/.test(u),
        build: () => getSandboxQuoteMarkPaid(),
        delayMs: 1000,
    },
    // 单关键词审计 (AI 校正价格 · 沙盒里直接返成功)
    {
        test: (m, u) => m === 'POST' && /^\/api\/keyword-selection\/[^/]+\/audit-keyword\/\d+/.test(u),
        build: () => ({ success: true, audit_status: 'passed', audit_note: '价格合理' }),
        delayMs: 1000,
    },
    // 删除会话内的关键词 (沙盒里返成功不真删)
    {
        test: (m, u) => m === 'DELETE' && /^\/api\/keyword-selection\/[^/]+\/keywords\/\d+/.test(u),
        build: () => ({ success: true }),
    },
    // 撤回报价 (回到 review 状态)
    {
        test: (m, u) => m === 'POST' && /^\/api\/keyword-selection\/[^/]+\/recall/.test(u),
        build: () => ({ success: true }),
    },
    // 状态回退 (重新计算等)
    {
        test: (m, u) => m === 'POST' && /^\/api\/keyword-selection\/[^/]+\/revert-status/.test(u),
        build: () => ({ success: true, status: 'pricing_pending_review' }),
    },
    // 报价系数只预览/冻结沙盒快照, 不改变底层成本。
    {
        test: (m, u) => m === 'GET' && /^\/api\/quotes\/\d+\/coefficient-preview(\?|$)/.test(u),
        build: (cfg) => buildSandboxQuoteCoefficientPreview(cfg),
    },
    {
        test: (m, u) => m === 'POST' && /^\/api\/quotes\/\d+\/coefficient(\?|$)/.test(u),
        build: (cfg) => buildSandboxQuoteCoefficientSave(cfg),
    },
    // 单会话详情 (GET /api/s/{token}) · 按状态机推进
    {
        test: (m, u) => m === 'GET' && /^\/api\/s\/[^/]+(\?|$)/.test(u),
        build: () => getSandboxQuoteSession(),
    },
    // === 客户端选词页 (SelectionPage · /s/:token) 的客户操作 ===
    // 客户提交业务方向 → 进"等待报价"(真实业务默认流程)
    {
        test: (m, u) => m === 'POST' && /^\/api\/s\/[^/]+\/submit-business-lines/.test(u),
        build: () => getSandboxSelectionSubmitBusinessLines(),
        delayMs: 600,
    },
    // 客户提交选词(老流程, business_lines 为空时)→ 进"等待报价"
    {
        test: (m, u) => m === 'POST' && /^\/api\/s\/[^/]+\/submit-keywords/.test(u),
        build: (cfg) => getSandboxSelectionSubmitKeywords(bodyNumberArray(cfg, 'selected_ids')),
        delayMs: 600,
    },
    // 客户选档位 + 确认报价 → 确认态
    {
        test: (m, u) => m === 'POST' && /^\/api\/s\/[^/]+\/confirm-quote/.test(u),
        build: () => getSandboxSelectionConfirmQuote(),
        delayMs: 600,
    },
    // 客户端其它操作(加词 / 撤回等)· 沙盒返成功不改主流程
    {
        test: (m, u) => m === 'POST' && /^\/api\/s\/[^/]+\/(add-keywords|cancel-add-keywords|withdraw-keywords)/.test(u),
        build: () => ({ success: true }),
    },
    // 提炼诊断数据 (扩词前调) · 沙盒返预设
    {
        test: (m, u) => m === 'POST' && /^\/api\/distill\/\d+/.test(u),
        build: () => ({ success: true, distilled: { target_audience: '25-45 岁科技爱好者', core_business: '智能硬件全品类生态', industry: '智能硬件' } }),
    },
    // 历史诊断列表 (报价页诊断 dropdown 用) · 返小米诊断 id=308
    {
        test: (m, u) => m === 'GET' && /^\/api\/history(\?|$)/.test(u),
        build: () => getSandboxHistoryForQuote(),
    },
    // 诊断复测数据 (选诊断后自动填关键词/城市)
    {
        test: (m, u) => m === 'GET' && /^\/api\/diagnosis\/\d+\/retest-data/.test(u),
        build: () => getSandboxRetestData(),
    },
    // 报价列表 (报价页 city 来源)
    {
        test: (m, u) => m === 'GET' && /^\/api\/quotes(\?|$)/.test(u),
        build: () => getSandboxQuotesList(),
    },

    // ===========================================================
    // 2026-05-25 删除 Step 2 快速录单路径整组拦截器
    // 删了:/api/distill/:id · /api/keywords/quote · /api/quotes/:id/offline-confirm · /api/quotes/:id/offline-mark-paid
    // ===========================================================
    // 单个 quote 详情 (沙盒里返入门版报价 shell · 跟合同 3 词 ¥3000/月 一致)
    // 在线报价的 QuoteHistoryList 也调这个 · 用线上路径的 999001
    {
        test: (m, u) => m === 'GET' && /^\/api\/quotes\/\d+(\?|$)/.test(u),
        build: () => ({ success: true, id: 999001, brand_name: '一路顺风出行服务', industry: '高端商务用车 / 豪华专车服务', total_price: 3000, tier: 'entry' }),
    },
    // quote 状态回退 (签约页"回退到上一步"按钮)· 沙盒为演示流程, 回退会 reload 重置内存态 → 明确拦下不执行
    {
        test: (m, u) => m === 'POST' && /^\/api\/quotes\/\d+\/revert-status/.test(u),
        build: () => ({ success: false, detail: '教程模式为演示流程, 不支持回退到上一步' }),
    },
    // 7 天价格缓存清空 (QuoteCenter 有这个按钮 · 沙盒里直接返成功)
    {
        test: (m, u) => m === 'DELETE' && /^\/api\/keyword-selection\/[^/]+\/price-cache/.test(u),
        build: () => ({ success: true }),
    },
    // 客户上下文列表 (QuoteCenter 拉客户 · 用 .name 字段)
    {
        test: (m, u) => m === 'GET' && /^\/api\/client-context\/list/.test(u),
        build: () => ({
            success: true,
            clients: [{
                id: 111,
                name: '一路顺风出行服务',
                brand_name: '一路顺风出行服务',
                industry: '高端商务用车 / 豪华专车服务',
                industry_category: '汽车出行',
                latest_score: 26,
                cities: '深圳',
            }],
        }),
    },

    // ===========================================================
    // Step 3 · AI 写文章 + 发布 · 写作大厅 + 发布通道代发
    // ===========================================================
    // 写作项目列表 (WritingHall 主拉)
    {
        test: (m, u) => m === 'GET' && /^\/api\/writing\/projects(\?|$)/.test(u),
        build: () => getSandboxWritingProjects(),
    },
    // 写作项目详情 · 关键词 + 选题列表
    {
        test: (m, u) => m === 'GET' && /^\/api\/writing\/projects\/\d+(\?|$)/.test(u),
        build: () => getSandboxWritingProjectDetail(),
    },
    // 知识库状态 · 沙盒永远 confirmed (避免被卡在补资料环节)
    {
        test: (m, u) => m === 'GET' && /^\/api\/writing\/projects\/\d+\/knowledge-status/.test(u),
        build: () => getSandboxWritingKnowledgeStatus(),
    },
    // 客户知识库文件 (loadKnowledgeFiles) · 沙盒返 1 个已向量化的文件 · 让 hasIndexedKnowledge=true
    {
        test: (m, u) => m === 'GET' && /^\/api\/knowledge\/client\/\d+/.test(u),
        build: () => getSandboxKnowledgeFiles(),
    },
    // 联网搜竞品 · 严选模式下点击会触发 · 沙盒返 5 个真实竞品 (1.5s 模拟搜索)
    {
        test: (m, u) => m === 'POST' && /^\/api\/writing\/research-competitors\/\d+/.test(u),
        build: () => getSandboxResearchCompetitors(),
        delayMs: 1500,
    },
    // 对现有候选补齐名称来源，不重新生成或替换名单
    {
        test: (m, u) => m === 'POST' && /^\/api\/writing\/competitors\/\d+\/verify/.test(u),
        build: () => getSandboxVerifyCompetitors(),
        delayMs: 1200,
    },
    // 竞品模式切换持久化
    {
        test: (m, u) => m === 'PUT' && /^\/api\/writing\/competitors\/\d+\/mode/.test(u),
        build: () => getSandboxCompetitorsMode(),
    },
    // 竞品列表 (loadCompetitors)
    {
        test: (m, u) => m === 'GET' && /^\/api\/writing\/competitors\/\d+(\?|$)/.test(u),
        build: () => getSandboxCompetitorsList(),
    },
    // 竞品排除状态切换 (拉黑名单)
    {
        test: (m, u) => m === 'PUT' && /^\/api\/writing\/competitors\/\d+\/exclude/.test(u),
        build: () => ({ success: true }),
    },
    // 生成标题 · 2s 模拟 AI 思考
    {
        test: (m, u) => m === 'POST' && /^\/api\/writing\/generate-titles/.test(u),
        build: () => getSandboxGenerateTitles(),
        delayMs: 2000,
    },
    // 开始写作 · 立刻返, 进度页轮询展示耗时感
    {
        test: (m, u) => m === 'POST' && /^\/api\/writing\/start-articles/.test(u),
        build: () => getSandboxStartArticles(),
    },
    // 写作进度轮询
    {
        test: (m, u) => m === 'GET' && /^\/api\/writing\/progress\/\d+/.test(u),
        build: () => getSandboxWritingProgress(),
    },
    // 文章列表 (WritingCenter)
    {
        test: (m, u) => m === 'GET' && /^\/api\/articles(\?|$)/.test(u),
        build: () => getSandboxArticlesList(),
    },
    // 文章详情 (preview)
    {
        test: (m, u) => m === 'GET' && /^\/api\/articles\/\d+(\?|$)/.test(u),
        build: (cfg) => {
            const match = (cfg.url || '').match(/\/api\/articles\/(\d+)/);
            const id = match ? parseInt(match[1], 10) : 0;
            return getSandboxArticleDetail(id);
        },
    },
    // 文章编辑保存 (敏感词修改后)
    {
        test: (m, u) => (m === 'PUT' || m === 'POST') && /^\/api\/articles\/\d+(\?|$)/.test(u),
        build: () => ({ success: true, message: '文章已保存 (教程模式)' }),
    },
    // D8: 质量/证据提示只修命中段落, 文章其余内容保留。
    {
        test: (m, u) => m === 'POST' && /^\/api\/articles\/\d+\/repair-finding(\?|$)/.test(u),
        build: (cfg) => buildSandboxArticleRepair(cfg),
        delayMs: 700,
    },
    {
        test: (m, u) => m === 'POST' && /^\/api\/topics\/\d+\/mark-reviewed(\?|$)/.test(u),
        build: () => ({
            success: true,
            status: 'reviewed',
            advisory_continued: true,
            message: '已记录本次人工决定；教程模式未写入真实数据',
        }),
    },
    // 代发媒介列表 (PublishCenter 浏览媒介)
    {
        test: (m, u) => m === 'GET' && /^\/api\/publish\/media(\?|$)/.test(u),
        build: () => getSandboxPublishMediaList(),
    },
    // 代发媒介过滤选项
    {
        test: (m, u) => m === 'GET' && /^\/api\/publish\/media\/filters/.test(u),
        build: () => ({ success: true, resource_types: ['article'], price_ranges: [{ min: 0, max: 1000 }] }),
    },
    // 发布通道 mirror endpoint
    {
        test: (m, u) => m === 'GET' && /^\/api\/meijiehezi\/media(\?|$)/.test(u),
        build: () => getSandboxPublishMediaList(),
    },
    // 发布通道价格加价比例 (markup)
    {
        test: (m, u) => m === 'GET' && /^\/api\/meijiehezi\/markup/.test(u),
        build: () => ({ status: 'success', ratio: 1 }),
    },
    // 发布通道筛选选项 · 注意 news_resources / areas / portal_medias 都是字符串数组(直接渲染)
    {
        test: (m, u) => m === 'GET' && /^\/api\/meijiehezi\/media\/filters/.test(u),
        build: () => ({
            status: 'success',
            areas: ['全国', '深圳'],
            resource_types: ['门户网站', '自媒体'],
            news_resources: ['1', '0'],
            portal_medias: ['搜狐', '新浪', '凤凰', '百度', '腾讯', '今日头条'],
            resource_type_names: ['门户首页', '门户内频道', '百家号', '头条号'],
            geo_platforms: [],
            special_industries: [],
        }),
    },
    // 自媒体筛选选项
    {
        test: (m, u) => m === 'GET' && /^\/api\/meijiehezi\/wemedia\/filters/.test(u),
        build: () => ({ status: 'success', platforms: [], industries: [], provinces: [], geo_platforms: [] }),
    },
    // 自媒体列表 (沙盒 wemedia tab 用 · 返空让 UI 不报错)
    {
        test: (m, u) => m === 'GET' && /^\/api\/meijiehezi\/wemedia(\?|$)/.test(u),
        build: () => ({ status: 'success', media: [], total: 0, pages: 0 }),
    },
    // ML 推荐 (PublishCenter 调用 · 按 industry 查)
    {
        test: (m, u) => m === 'GET' && /^\/api\/publish\/media\/recommend-v2/.test(u),
        build: () => getSandboxPublishMediaRecommend(),
    },
    // 深度分析 · 沙盒返空让 UI 不卡
    {
        test: (m, u) => m === 'POST' && /^\/api\/publish\/media\/deep-analyze/.test(u),
        build: () => ({ status: 'success', success: true, analysis: '教程模式无深度分析' }),
    },
    // 单篇文章 AI 媒体推荐 · 选完文章 1.5s 自动触发 · 不 mock 会打真后端报"推荐生成失败"
    {
        test: (m, u) => m === 'POST' && /^\/api\/publish\/articles\/\d+\/media-recommendation/.test(u),
        build: () => getSandboxArticleMediaRecommendation(),
    },
    // 文章列表 (PublishCenter 用 placement/articles/{quote_id})
    {
        test: (m, u) => m === 'GET' && /^\/api\/placement\/articles\/\d+/.test(u),
        build: () => getSandboxPlacementArticles(),
    },
    // 首单检查 (新用户首次发布提示)
    {
        test: (m, u) => m === 'GET' && /^\/api\/publish\/check-first-order/.test(u),
        build: () => ({ success: true, is_first_order: false }),
    },
    // 发布页会在首屏自动恢复这些后台状态。显式返回本地空态，避免教程页面
    // 因非核心可选能力不断报错；这不是对用户主动发布动作的假成功。
    {
        test: (m, u) => m === 'GET' && /^\/api\/publish\/research\/active-task(\?|$)/.test(u),
        build: (cfg) => ({
            status: 'success',
            success: true,
            industry_key: new URL(cfg.url || '', window.location.origin).searchParams.get('industry') || '',
            active_task: null,
        }),
    },
    {
        test: (m, u) => m === 'GET' && /^\/api\/meijiehezi\/published-articles(\?|$)/.test(u),
        build: () => ({ status: 'success', success: true, article_ids: [] }),
    },
    {
        test: (m, u) => m === 'GET' && /^\/api\/meijiehezi\/rejected-articles(\?|$)/.test(u),
        build: () => ({ status: 'success', success: true, article_ids: [] }),
    },
    {
        test: (m, u) => m === 'GET' && /^\/api\/meijiehezi\/article-publish-stats(\?|$)/.test(u),
        build: () => ({ status: 'success', success: true, stats: {} }),
    },
    // 教程主路径不注入媒介审核或治理审批。真实外发边界仍由生产接口处理。
    {
        test: (m, u) => m === 'GET' && /^\/api\/meijiehezi\/awaiting-confirmations(\?|$)/.test(u),
        build: () => getSandboxAwaitingConfirmations(),
    },
    // 待确认详情 (含文章原文 + 敏感词)
    {
        test: (m, u) => m === 'GET' && /^\/api\/meijiehezi\/awaiting-confirmations\/\d+\/detail/.test(u),
        build: (cfg) => {
            const m = (cfg.url || '').match(/awaiting-confirmations\/(\d+)\/detail/);
            const id = m ? parseInt(m[1], 10) : 0;
            return getSandboxAwaitingDetail(id);
        },
    },
    // 确认 / 取消订单
    {
        test: (m, u) => m === 'POST' && /^\/api\/meijiehezi\/confirm\/\d+/.test(u),
        build: () => getSandboxConfirmAwaiting(),
    },
    // 教程不把生产事故工单冒充成普通用户的必经任务。
    {
        test: (m, u) => m === 'GET' && /^\/api\/meijiehezi\/pending-user-actions(\?|$)/.test(u),
        build: () => getSandboxPendingUserActions(),
    },
    {
        test: (m, u) => m === 'POST' && /^\/api\/meijiehezi\/items\/\d+\/report-not-published/.test(u),
        build: (cfg) => {
            const m = (cfg.url || '').match(/items\/(\d+)\/report-not-published/);
            return getSandboxReportNotPublished(m ? parseInt(m[1], 10) : 0);
        },
    },
    {
        test: (m, u) => m === 'POST' && /^\/api\/meijiehezi\/items\/\d+\/record-publish-url/.test(u),
        build: (cfg) => {
            const m = (cfg.url || '').match(/items\/(\d+)\/record-publish-url/);
            return getSandboxRecordPublishUrl(m ? parseInt(m[1], 10) : 0);
        },
    },
    // 投放决策快照 · "继续投放"前先存 · 不 mock 会落兜底(缺 snapshot_id)报"投放确认保存失败"
    {
        test: (m, u) => m === 'POST' && /^\/api\/publish\/decision-snapshots/.test(u),
        build: () => getSandboxPublishDecisionSnapshot(),
    },
    // ⚠️ 关键拦截 · 发布通道批量发布 · 沙盒里绝对不能真发
    {
        test: (m, u) => m === 'POST' && /^\/api\/meijiehezi\/publish\/batch/.test(u),
        build: () => getSandboxMeijieheziPublishBatch(),
        delayMs: 1500, // 让用户看到 "提交中..." 转圈
    },
    // 已同步订单列表 (publish/history 媒介代发 tab)
    {
        test: (m, u) => m === 'GET' && /^\/api\/meijiehezi\/synced-orders/.test(u),
        build: () => getSandboxMeijieheziSyncedOrders(),
    },
    // 历史发布记录 (自助 tab) · 沙盒返空
    {
        test: (m, u) => m === 'GET' && /^\/api\/publish\/self-publish/.test(u),
        build: () => ({ success: true, items: [], total: 0 }),
    },
    // 已发布文章 sync (没用沙盒里不真发)
    {
        test: (m, u) => m === 'POST' && /^\/api\/meijiehezi\/published-articles/.test(u),
        build: () => ({ success: true, synced: 0 }),
    },

    // === Step 4: 监测出现率 + 补救闭环 ===
    // 监测客户列表 (一路顺风)
    {
        test: (m, u) => m === 'GET' && /^\/api\/monitoring\/clients(\?|$)/.test(u),
        build: () => getSandboxMonitoringClients(),
    },
    // 监测词列表 (6 个词 · 快进 10 天形态)
    {
        test: (m, u) => m === 'GET' && /^\/api\/monitoring\/clients\/\d+\/keywords/.test(u),
        build: () => getSandboxMonitoringKeywords(),
    },
    // 趋势曲线 (10 天爬升)
    {
        test: (m, u) => m === 'GET' && /^\/api\/monitoring\/trend/.test(u),
        build: () => getSandboxMonitoringTrend(),
    },
    // 监测页首屏自动读取的辅助数据。它们都在浏览器内返回明确空态，
    // 不触达真实后端，也不把尚未教学实现的 mutation 伪装成成功。
    {
        test: (m, u) => m === 'GET' && /^\/api\/publications\/\d+(\?|$)/.test(u),
        build: () => ({ status: 'success', success: true, publications: [], snapshot_missing: false }),
    },
    {
        test: (m, u) => m === 'GET' && /^\/api\/logs(\?|$)/.test(u),
        build: () => ({ status: 'success', success: true, logs: [], total: 0, snapshot_missing: false }),
    },
    {
        test: (m, u) => m === 'GET' && /^\/api\/monitoring\/rollback\/tasks(\?|$)/.test(u),
        build: () => ({ status: 'success', success: true, tasks: [], total: 0, snapshot_missing: false }),
    },
    {
        test: (m, u) => m === 'GET' && /^\/api\/monitoring\/archives(\?|$)/.test(u),
        build: () => ({ status: 'success', success: true, archives: [], snapshot_missing: false }),
    },
    {
        test: (m, u) => m === 'GET' && /^\/api\/monitoring\/schedule(\?|$)/.test(u),
        build: () => ({
            status: 'success',
            success: true,
            monitoring_enabled: false,
            jobs: [],
            snapshot_missing: false,
        }),
    },
    {
        test: (m, u) => m === 'GET' && /^\/api\/monitoring\/client\/\d+\/monitoring-config(\?|$)/.test(u),
        build: () => ({
            status: 'success',
            success: true,
            monitoring_enabled: false,
            monitoring_interval_hours: 24,
            monitoring_start_hour: 8,
            snapshot_missing: false,
        }),
    },
    {
        test: (m, u) => m === 'GET' && /^\/api\/monitoring\/tasks\?/.test(u),
        build: () => buildSandboxMonitoringTaskList(),
    },
    {
        test: (m, u) => m === 'GET' && /^\/api\/monitoring\/tasks\/\d+(\?|$)/.test(u),
        build: () => buildSandboxMonitoringTaskDetail(),
    },
    {
        test: (m, u) => m === 'POST' && /^\/api\/monitoring\/tasks\/\d+\/cells\/\d+\/retry(\?|$)/.test(u),
        build: (cfg) => buildSandboxMonitoringCellRetry(cfg),
        delayMs: 500,
    },
    {
        test: (m, u) => m === 'GET' && /^\/api\/monitoring\/identity-reviews\?/.test(u),
        build: () => buildSandboxIdentityReviews(),
    },
    {
        test: (m, u) => m === 'POST' && /^\/api\/monitoring\/identity-reviews\/\d+\/decision(\?|$)/.test(u),
        build: (cfg) => buildSandboxIdentityDecision(cfg),
    },
    {
        test: (m, u) => m === 'POST' && /^\/api\/monitoring\/run(\?|$)/.test(u),
        build: () => ({
            status: 'success',
            success: true,
            task_id: SANDBOX_MONITORING_TASK_ID,
            message: '教程监测已在本地完成，未调用真实引擎',
        }),
        delayMs: 600,
    },
    // 切换套餐档位 (PUT) · 沙盒返成功 · 不真改 (前端按 status==='success' 判)
    {
        test: (m, u) => m === 'PUT' && /^\/api\/monitoring\/tier/.test(u),
        build: () => ({ status: 'success', success: true }),
    },
    // 智能补足预览 · 本地返回真实 DTO 形状，让教程可确认并继续。
    {
        test: (m, u) => m === 'GET' && /^\/api\/writing\/optimize-preview\/\d+(\?|$)/.test(u),
        build: (cfg) => {
            const keywordId = Number((cfg.url || '').match(/optimize-preview\/(\d+)/)?.[1] || 0);
            return getSandboxOptimizePreview(keywordId);
        },
        delayMs: 250,
    },
    // 追加优化文章 (补救入口) · 沙盒不真生成 · 返成功让前端跳写作页 optimize tab
    {
        test: (m, u) => m === 'POST' && /^\/api\/writing\/optimize-generate/.test(u),
        build: (cfg) => getSandboxOptimizeGenerate(
            cfg.data && typeof cfg.data === 'object'
                ? cfg.data as { keyword_id?: number; plan_version?: string; plan_hash?: string }
                : undefined,
        ),
    },
    // 客户门户白标 token (教程收尾步: 给客户发实时数据链接)
    {
        test: (m, u) => m === 'GET' && /^\/api\/portal\/tokens\/\d+/.test(u),
        build: () => getSandboxPortalTokenGet(),
    },
    {
        test: (m, u) => m === 'POST' && /^\/api\/portal\/tokens(\?|$)/.test(u),
        build: () => getSandboxPortalTokenCreate(),
    },
];

function matchRule(method: string, url: string): Rule | null {
    const m = method.toUpperCase();
    for (const r of RULES) {
        if (r.test(m, url)) return r;
    }
    return null;
}

function delay(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

const SANDBOX_MONITORING_TASK_ID = 990001;

type SandboxDiagnosisCell = {
    question: string;
    engine: string;
    state: 'YES' | 'NO' | 'PENDING_IDENTITY' | 'PROVIDER_UNKNOWN' | 'NOT_COLLECTED';
    brand_verdict: string | null;
    brand_detected: boolean;
    matched_text: string | null;
    candidates: string[];
    evidence_snippet: string | null;
    answer_summary: string | null;
    answer_full: string | null;
    answer_truncated: boolean;
    detection_reason: string | null;
    detection_method: string | null;
    identity_review_state: string;
    identity_decision_version: number;
    evidence_hash: string;
    human_decision: {
        action?: string;
        selected_name?: string | null;
        reason?: string | null;
    } | null;
};

const sandboxDiagnosisCells: SandboxDiagnosisCell[] = [
    {
        question: '深圳商务接待用车哪家服务更稳定？',
        engine: 'deepseek',
        state: 'YES',
        brand_verdict: '一路顺风出行服务',
        brand_detected: true,
        matched_text: '一路顺风出行',
        candidates: ['一路顺风出行服务'],
        evidence_snippet: '回答明确列出了一路顺风出行服务，并说明其商务接待车型与司机服务。',
        answer_summary: '明确提到目标品牌。',
        answer_full: '在深圳商务接待场景中，一路顺风出行服务提供埃尔法等车型与司机服务，可结合行程、人数和接待规格进一步询价。',
        answer_truncated: false,
        detection_reason: '目标品牌全称或已确认别名精确出现',
        detection_method: 'exact_alias',
        identity_review_state: 'confirmed',
        identity_decision_version: 1,
        evidence_hash: 'sandbox-diagnosis-cell-1',
        human_decision: null,
    },
    {
        question: '深圳租埃尔法配司机的公司怎么选？',
        engine: 'kimi',
        state: 'PENDING_IDENTITY',
        brand_verdict: null,
        brand_detected: false,
        matched_text: '一路顺风',
        candidates: ['一路顺风出行服务'],
        evidence_snippet: '回答中出现“一路顺风”，上下文指向商务用车服务，但名称不完整，需要人工顺手确认。',
        answer_summary: '名称不完整，疑似提到目标品牌。',
        answer_full: '如果重视商务接待车型和司机响应，可以继续核验一路顺风的车辆档期、服务范围和报价口径，再与其他候选比较。',
        answer_truncated: false,
        detection_reason: '出现短名称，存在同名可能',
        detection_method: 'partial_alias',
        identity_review_state: 'pending',
        identity_decision_version: 1,
        evidence_hash: 'sandbox-diagnosis-cell-2',
        human_decision: null,
    },
    {
        question: '深圳企业长租埃尔法包月有哪些选择？',
        engine: 'doubao',
        state: 'NO',
        brand_verdict: null,
        brand_detected: false,
        matched_text: null,
        candidates: [],
        evidence_snippet: '本条回答没有提到目标品牌。',
        answer_summary: '未提到目标品牌。',
        answer_full: '本条回答介绍了企业长租的一般选择，没有出现目标品牌名称。',
        answer_truncated: false,
        detection_reason: '未发现目标品牌或别名',
        detection_method: 'no_match',
        identity_review_state: 'not_required',
        identity_decision_version: 1,
        evidence_hash: 'sandbox-diagnosis-cell-3',
        human_decision: null,
    },
];

function diagnosisIdFromConfig(config: InternalAxiosRequestConfig): number {
    const match = (config.url || '').match(/\/api\/diagnosis\/(\d+)/);
    return match ? Number(match[1]) : 990001;
}

function diagnosisAggregates() {
    const totals = sandboxDiagnosisCells.reduce<Record<string, number>>((acc, cell) => {
        acc[cell.state] = (acc[cell.state] || 0) + 1;
        return acc;
    }, {});
    const counted = (totals.YES || 0) + (totals.NO || 0);
    return {
        dimension_stats: { brand_visibility: totals },
        totals: { ...totals, COUNTED: counted },
    };
}

function buildSandboxDiagnosisBrandCells(config: InternalAxiosRequestConfig) {
    return {
        status: 'success',
        success: true,
        data: {
            diagnosis_id: diagnosisIdFromConfig(config),
            brand_id: SANDBOX_BRAND_ID,
            cells: sandboxDiagnosisCells.map((cell) => ({ ...cell })),
            aggregates: diagnosisAggregates(),
            score: 68,
            level: 'B',
        },
    };
}

function buildSandboxDiagnosisBrandDecision(config: InternalAxiosRequestConfig) {
    const cell = sandboxDiagnosisCells.find((candidate) => candidate.state === 'PENDING_IDENTITY');
    if (cell) {
        cell.state = 'YES';
        cell.brand_detected = true;
        cell.brand_verdict = '一路顺风出行服务';
        cell.identity_review_state = 'confirmed';
        cell.identity_decision_version += 1;
        cell.human_decision = {
            action: 'confirm_yes',
            selected_name: '一路顺风出行服务',
            reason: '教程中的本地人工确认',
        };
    }
    return {
        status: 'success',
        success: true,
        cell: cell ? { ...cell } : undefined,
        aggregates: diagnosisAggregates(),
        score: 72,
        level: 'B',
        diagnosis_id: diagnosisIdFromConfig(config),
        provider_calls: 0,
        billed_points: 0,
    };
}

function queryNumber(config: InternalAxiosRequestConfig, key: string, fallback: number): number {
    const data = bodyRecord(config);
    const bodyValue = Number(data[key]);
    if (Number.isFinite(bodyValue) && bodyValue > 0) return bodyValue;
    try {
        const url = new URL(config.url || '', 'https://sandbox.local');
        const value = Number(url.searchParams.get(key));
        return Number.isFinite(value) && value > 0 ? value : fallback;
    } catch {
        return fallback;
    }
}

function bodyRecord(config: InternalAxiosRequestConfig): Record<string, unknown> {
    const data = config.data;
    if (data && typeof data === 'object' && !Array.isArray(data)) {
        return data as Record<string, unknown>;
    }
    if (typeof data === 'string') {
        try {
            const parsed = JSON.parse(data);
            return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
                ? parsed as Record<string, unknown>
                : {};
        } catch {
            return {};
        }
    }
    return {};
}

function bodyNumberArray(config: InternalAxiosRequestConfig, key: string): number[] {
    const value = bodyRecord(config)[key];
    if (!Array.isArray(value)) return [];
    return value
        .map((item) => Number(item))
        .filter((item) => Number.isInteger(item) && item > 0);
}

function buildSandboxQuoteCoefficientPreview(config: InternalAxiosRequestConfig) {
    const coefficient = Math.min(3, Math.max(0.5, queryNumber(config, 'coefficient', 1)));
    const scale = (amount: number) => Math.round(amount * coefficient);
    return {
        old_coefficient: 1,
        new_coefficient: coefficient,
        calculation_version: 'sandbox-quote-coefficient-v1',
        summaries: {
            entry: { total_price: scale(3000), total_articles: 3 },
            standard: { total_price: scale(5000), total_articles: 6 },
            flagship: { total_price: scale(8000), total_articles: 9 },
        },
        keywords: [
            { id: 40001, keyword: '深圳租埃尔法配司机的公司', entry: scale(900), standard: scale(1500), flagship: scale(2400) },
            { id: 40002, keyword: '深圳豪车配司机哪家好', entry: scale(1000), standard: scale(1700), flagship: scale(2700) },
            { id: 40003, keyword: '深圳埃尔法长租包月', entry: scale(1100), standard: scale(1800), flagship: scale(2900) },
        ],
        keyword_count: 3,
        snapshot_hash: `sandbox-coefficient-${coefficient.toFixed(2)}`,
    };
}

function buildSandboxQuoteCoefficientSave(config: InternalAxiosRequestConfig) {
    return {
        success: true,
        status: 'saved',
        old_coefficient: 1,
        new_coefficient: queryNumber(config, 'coefficient', 1),
        snapshot_hash: `sandbox-coefficient-saved-${Date.now()}`,
        audit_recorded: true,
        message: '教程报价快照已在浏览器内冻结，未写入真实报价',
    };
}

function buildSandboxArticleRepair(config: InternalAxiosRequestConfig) {
    const articleId = Number((config.url || '').match(/\/api\/articles\/(\d+)/)?.[1] || 0);
    return {
        success: true,
        status: 'repaired',
        article_id: articleId,
        repaired_scope: 'matched_span_only',
        preserved_other_content: true,
        provider_calls: 0,
        billed_points: 0,
        message: '只修复了提示所在段落，其余正文保持不变',
    };
}

const sandboxMonitoringCells = [
    {
        id: 991001,
        task_id: SANDBOX_MONITORING_TASK_ID,
        keyword_id: 40001,
        keyword_source: 'confirmed',
        keyword: '深圳租埃尔法配司机的公司',
        keyword_snapshot: '深圳租埃尔法配司机的公司',
        platform: 'deepseek',
        is_planned: true,
        state: 'succeeded',
        plan_hash: 'sandbox-plan-deepseek',
        fulfillment_state: 'covered',
        attempt_count: 1,
        retry_count: 0,
        retry_coverage: 'included',
        retry_max_attempts: 2,
        result_id: 992001,
        error_code: null,
        error_message: null,
    },
    {
        id: 991002,
        task_id: SANDBOX_MONITORING_TASK_ID,
        keyword_id: 40002,
        keyword_source: 'confirmed',
        keyword: '深圳豪车配司机哪家好',
        keyword_snapshot: '深圳豪车配司机哪家好',
        platform: 'kimi',
        is_planned: true,
        state: 'pending_identity',
        plan_hash: 'sandbox-plan-kimi',
        fulfillment_state: 'covered',
        attempt_count: 1,
        retry_count: 0,
        retry_coverage: 'included',
        retry_max_attempts: 2,
        result_id: 992002,
        error_code: 'brand_identity_unresolved',
        error_message: '回答疑似提到品牌，顺手确认后再计入统计',
    },
    {
        id: 991003,
        task_id: SANDBOX_MONITORING_TASK_ID,
        keyword_id: 40003,
        keyword_source: 'confirmed',
        keyword: '深圳埃尔法长租包月',
        keyword_snapshot: '深圳埃尔法长租包月',
        platform: 'doubao',
        is_planned: true,
        state: 'failed',
        plan_hash: 'sandbox-plan-doubao',
        fulfillment_state: 'covered',
        attempt_count: 1,
        retry_count: 0,
        retry_coverage: 'included',
        retry_max_attempts: 2,
        result_id: null,
        error_code: 'provider_timeout',
        error_message: '本格连接超时，可只重试这一格',
    },
] as const;

let sandboxIdentityReviews = [
    {
        id: 993001,
        keyword: '深圳豪车配司机哪家好',
        platform: 'kimi',
        response_snippet: '回答中出现“一路顺风”，并描述了商务用车服务，名称不完整。',
        identity_candidates: ['一路顺风出行服务'],
        identity_evidence_snippet: '一路顺风可提供商务接待车型及司机服务，建议继续核对车辆档期和服务区域。',
        identity_evidence_hash: 'sandbox-monitor-identity-1',
        identity_decision_version: 1,
        tested_at: new Date().toISOString(),
    },
];

function buildSandboxMonitoringTaskList() {
    return {
        status: 'success',
        success: true,
        tasks: [{
            id: SANDBOX_MONITORING_TASK_ID,
            brand_id: SANDBOX_BRAND_ID,
            status: 'completed',
            total_tests: sandboxMonitoringCells.length,
            completed_tests: sandboxMonitoringCells.length,
            created_at: new Date().toISOString(),
        }],
        total: 1,
    };
}

function buildSandboxMonitoringTaskDetail() {
    return {
        status: 'success',
        success: true,
        task: {
            id: SANDBOX_MONITORING_TASK_ID,
            brand_id: SANDBOX_BRAND_ID,
            status: 'completed',
            total_tests: sandboxMonitoringCells.length,
            completed_tests: sandboxMonitoringCells.length,
        },
        cells: sandboxMonitoringCells.map((cell) => ({ ...cell })),
        results: [
            {
                id: 992001,
                keyword: '深圳租埃尔法配司机的公司',
                platform: 'deepseek',
                is_detected: true,
                response_snippet: '回答提到一路顺风出行服务。',
                full_response: '回答提到一路顺风出行服务，并建议结合车型和行程询价。',
                tested_at: new Date().toISOString(),
                identity_review_state: 'confirmed',
            },
            {
                id: 992002,
                keyword: '深圳豪车配司机哪家好',
                platform: 'kimi',
                is_detected: false,
                response_snippet: '回答疑似提到目标品牌，等待人工确认。',
                full_response: '回答中出现“一路顺风”，但名称不完整，等待人工确认。',
                tested_at: new Date().toISOString(),
                identity_review_state: 'pending',
            },
        ],
    };
}

function buildSandboxMonitoringCellRetry(config: InternalAxiosRequestConfig) {
    const cellId = Number((config.url || '').match(/\/cells\/(\d+)\/retry/)?.[1] || 0);
    const cell = sandboxMonitoringCells.find((item) => item.id === cellId);
    return {
        status: 'success',
        success: true,
        cell: cell ? { ...cell, state: 'succeeded', error_code: null, error_message: null } : null,
        data: { state: 'succeeded' },
        provider_calls: 0,
        billed_points: 0,
        message: '教程中只重试了这一格，其他成功结果保持不变',
    };
}

function buildSandboxIdentityReviews() {
    return { status: 'success', success: true, items: sandboxIdentityReviews.map((item) => ({ ...item })) };
}

function buildSandboxIdentityDecision(config: InternalAxiosRequestConfig) {
    const itemId = Number((config.url || '').match(/identity-reviews\/(\d+)\/decision/)?.[1] || 0);
    sandboxIdentityReviews = sandboxIdentityReviews.filter((item) => item.id !== itemId);
    return {
        status: 'success',
        success: true,
        review_id: itemId,
        provider_calls: 0,
        billed_points: 0,
        message: '人工确认已在教程内生效',
    };
}

function buildSandboxMonitoringSse(): string {
    const events = [
        {
            type: 'start',
            total: sandboxMonitoringCells.length,
            task_id: SANDBOX_MONITORING_TASK_ID,
            cells: sandboxMonitoringCells.map((cell) => ({ ...cell, state: 'running' })),
        },
        {
            type: 'detail',
            completed: 1,
            keyword: '深圳租埃尔法配司机的公司',
            platform: 'deepseek',
            status: 'success',
            detected: true,
            snippet: '回答提到一路顺风出行服务。',
            full_response: '回答提到一路顺风出行服务，并建议结合车型和行程询价。',
            cell_id: 991001,
            cell_state: 'succeeded',
            plan_hash: 'sandbox-plan-deepseek',
        },
        {
            type: 'detail',
            completed: 2,
            keyword: '深圳豪车配司机哪家好',
            platform: 'kimi',
            status: 'error',
            error: '品牌名称需要人工顺手确认',
            error_code: 'brand_identity_unresolved',
            detected: false,
            snippet: '回答疑似提到目标品牌。',
            full_response: '回答中出现“一路顺风”，但名称不完整。',
            cell_id: 991002,
            cell_state: 'pending_identity',
            plan_hash: 'sandbox-plan-kimi',
        },
        {
            type: 'detail',
            completed: 3,
            keyword: '深圳埃尔法长租包月',
            platform: 'doubao',
            status: 'error',
            error: '本格连接超时，可只重试这一格',
            error_code: 'provider_timeout',
            detected: false,
            snippet: '',
            full_response: '',
            cell_id: 991003,
            cell_state: 'failed',
            plan_hash: 'sandbox-plan-doubao',
        },
        {
            type: 'complete',
            task_id: SANDBOX_MONITORING_TASK_ID,
            cells: sandboxMonitoringCells.map((cell) => ({ ...cell })),
        },
    ];
    return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('');
}

/**
 * 沙盒只允许精确登记的会话与只读壳层请求访问真实后端。
 * 业务 mutation、客户数据与 provider 入口绝不因宽泛 prefix 误穿透。
 */
const SANDBOX_EXACT_BYPASS = [
    { method: /^(GET|POST)$/, path: /^\/api\/auth\/(login|refresh|logout|me)(\?|$)/ },
    { method: /^GET$/, path: /^\/auth\/me(\?|$)/ },
    { method: /^GET$/, path: /^\/api\/wallet(\?|$|\/)/ },
    { method: /^GET$/, path: /^\/api\/feature-pricing(\?|$|\/)/ },
    { method: /^GET$/, path: /^\/api\/permissions\/[^?]+(\?|$)/ },
    { method: /^GET$/, path: /^\/api\/team\/[^?]+(\?|$)/ },
    { method: /^GET$/, path: /^\/api\/organization\/overview(\?|$)/ },
    { method: /^GET$/, path: /^\/api\/sl\/[^?]+(\?|$)/ },
];

function isBypassed(method: string, url: string): boolean {
    const normalizedMethod = method.toUpperCase();
    return SANDBOX_EXACT_BYPASS.some(
        (entry) => entry.method.test(normalizedMethod) && entry.path.test(url),
    );
}

function sandboxRecoveryActions(): SandboxAction[] {
    return [
        { id: 'retry', label: '重试当前操作' },
        { id: 'continue_tutorial', label: '返回当前教学步骤' },
        { id: 'exit_sandbox', label: '退出教程并使用真实系统' },
    ];
}

/**
 * 未登记接口必须显式失败。旧行为对未知 GET 返空、未知 mutation 返成功，
 * 会把真实缺口伪装成“按钮没反应”，也可能让教学步骤错误前进。
 */
function buildUnhandledResponse(method: string, path: string): unknown {
    const message = '这个操作尚未接入教程，真实请求已被本地拦截';
    const detail = {
        code: 'SANDBOX_ROUTE_NOT_IMPLEMENTED',
        message,
        reason: `${method.toUpperCase()} ${path} 没有沙盒响应合同`,
        repair_hint: '重试当前步骤；若仍出现，可退出教程继续使用真实系统',
        actions: sandboxRecoveryActions(),
        rule_version: 'sandbox-transport-v2',
    };
    return {
        success: false,
        status: 'error',
        code: detail.code,
        message,
        detail,
        actions: detail.actions,
    };
}

/**
 * 给 fetch 调用 (authFetch / 原生 fetch) 提供沙盒命中检查
 * 命中时返合成 Response, 不命中返 null 让真实 fetch 接手
 */
export async function tryFetchSandboxMock(
    method: string,
    url: string,
    /** force=true 时跳过"沙盒是否激活"检查 · 用于客户分享链接(sandbox- token)在任意设备(同网络同事/手机)打开都能用 */
    force = false,
    init?: RequestInit,
): Promise<Response | null> {
    if (!force && !isSandboxActive()) return null;
    // 把完整 URL 截成 pathname (匹配规则用 path 不带 host)
    let path = url;
    try {
        if (/^https?:\/\//i.test(url)) {
            const u = new URL(url);
            path = u.pathname + u.search;
        }
    } catch { /* ignore */ }

    // 1. 精确只读/会话白名单走真后端
    if (isBypassed(method, path)) return null;

    // 2. 监测流必须返回真正的 text/event-stream，不能用 JSON 200 冒充。
    if (
        method.toUpperCase() === 'POST'
        && /^\/api\/monitoring\/run-stream(\?|$)/.test(path)
    ) {
        await delay(350);
        return new Response(buildSandboxMonitoringSse(), {
            status: 200,
            statusText: 'OK (sandbox stream)',
            headers: {
                'Content-Type': 'text/event-stream; charset=utf-8',
                'Cache-Control': 'no-store',
                'x-sandbox-mock': '1',
            },
        });
    }

    // 3. 命中具体规则 → 返预设数据
    const rule = matchRule(method, path);
    if (rule) {
        let data: unknown = init?.body;
        if (typeof data === 'string') {
            try {
                data = JSON.parse(data);
            } catch {
                // Non-JSON request bodies are kept intact for route-specific handling.
            }
        }
        const payload = rule.build({
            method,
            url: path,
            data,
            headers: init?.headers,
        } as InternalAxiosRequestConfig);
        if (typeof window !== 'undefined') {
            // eslint-disable-next-line no-console
            console.debug(`[Sandbox] mocked (fetch) ${method.toUpperCase()} ${path} →`, payload);
        }
        if (rule.delayMs && rule.delayMs > 0) await delay(rule.delayMs);
        return new Response(JSON.stringify(payload), {
            status: 200,
            statusText: 'OK (sandbox)',
            headers: { 'Content-Type': 'application/json', 'x-sandbox-mock': '1' },
        });
    }

    // 4. 未命中接口显式失败，防真数据漏出，也防假成功。
    if (!path.startsWith('/api/')) return null; // 非 /api/ 路径不拦 (静态资源等)
    const unhandledPayload = buildUnhandledResponse(method, path);
    if (typeof window !== 'undefined') {
        // eslint-disable-next-line no-console
        console.warn(`[Sandbox] unhandled route ${method.toUpperCase()} ${path}`);
    }
    return new Response(JSON.stringify(unhandledPayload), {
        status: 501,
        statusText: 'Sandbox route not implemented',
        headers: {
            'Content-Type': 'application/json',
            'x-sandbox-mock': '1',
            'x-sandbox-unhandled': '1',
        },
    });
}

const INSTALLED = new WeakSet<AxiosInstance>();

export async function interceptSandboxRequest(
    config: InternalAxiosRequestConfig,
): Promise<InternalAxiosRequestConfig> {
    if (!isSandboxActive()) return config;
    const url = config.url || '';
    const method = (config.method || 'get').toUpperCase();

    // 1. 精确只读/会话白名单走真后端
    if (isBypassed(method, url)) return config;

    // 2. 命中具体规则 → reject 带 mock payload
    const rule = matchRule(method, url);
    if (rule) {
        const payload = rule.build(config);
        const err = new Error('[Sandbox] short-circuit');
        (err as unknown as SandboxMockMeta).__SANDBOX_MOCK__ = true;
        (err as unknown as SandboxMockMeta).payload = payload;
        (err as unknown as SandboxMockMeta).status = 200;
        (err as unknown as SandboxMockMeta).statusText = 'OK (sandbox)';
        (err as { config?: InternalAxiosRequestConfig }).config = config;
        if (rule.delayMs && rule.delayMs > 0) await delay(rule.delayMs);
        return Promise.reject(err);
    }

    // 3. 未登记业务接口显式 501，绝不触达后端、绝不假成功。
    if (url.startsWith('/api/')) {
        const err = new Error('[Sandbox] route not implemented');
        (err as unknown as SandboxMockMeta).__SANDBOX_MOCK__ = true;
        (err as unknown as SandboxMockMeta).payload = buildUnhandledResponse(method, url);
        (err as unknown as SandboxMockMeta).status = 501;
        (err as unknown as SandboxMockMeta).statusText = 'Sandbox route not implemented';
        (err as { config?: InternalAxiosRequestConfig }).config = config;
        return Promise.reject(err);
    }

    // 4. 非 /api/ 路径不拦
    return config;
}

export function resolveSandboxResponse(error: unknown): Promise<AxiosResponse> {
    const meta = error as SandboxMockMeta;
    if (meta && meta[MARKER] === true) {
        const cfg = (error as { config?: InternalAxiosRequestConfig }).config
            || ({} as InternalAxiosRequestConfig);
        const fakeResp: AxiosResponse = {
            data: meta.payload,
            status: meta.status,
            statusText: meta.statusText,
            headers: { 'x-sandbox-mock': '1' },
            config: cfg,
            request: undefined,
        };
        if (typeof window !== 'undefined') {
            // eslint-disable-next-line no-console
            console.debug(
                `[Sandbox] mocked ${cfg.method?.toUpperCase()} ${cfg.url} →`,
                meta.payload,
            );
        }
        if (meta.status >= 400) {
            const sandboxError = new Error(
                (meta.payload as { message?: string })?.message || meta.statusText,
            ) as Error & {
                config?: InternalAxiosRequestConfig;
                response?: AxiosResponse;
                isAxiosError?: boolean;
            };
            sandboxError.config = cfg;
            sandboxError.response = fakeResp;
            sandboxError.isAxiosError = true;
            return Promise.reject(sandboxError);
        }
        return Promise.resolve(fakeResp);
    }
    return Promise.reject(error);
}

export function installSandboxInterceptor(instance: AxiosInstance): void {
    if (INSTALLED.has(instance)) return;
    INSTALLED.add(instance);
    instance.interceptors.request.use(interceptSandboxRequest);
    instance.interceptors.response.use(
        (response) => response,
        resolveSandboxResponse,
    );
}

export const SANDBOX_IDS = {
    brand_id: SANDBOX_BRAND_ID,
};
