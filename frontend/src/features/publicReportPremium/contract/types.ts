/**
 * PublicReportPresentationV1 — 公开 GEO 诊断报告 · 展示层强类型契约
 *
 * 纪律(与后端 SSOT 对齐,证据见 docs/race/public-report-premium-race-a/T0_UNDERSTANDING.md):
 *  - 分数/等级/指标/平台数量只能来自后端;前端零硬编码业务结论、零等级映射表
 *  - 缺失值不是 0:所有可能缺失的指标均为 `number | null`,UI 渲染 "—"
 *  - 资料完整度(data_completeness)与 GEO 综合评分(geo score)是两个独立指标
 *  - 结构化区块统一用 DataSection<T> 三态:ready / empty(后端给了但为空或样本不足)
 *    / unavailable(后端数据通道尚未开放)——诚实呈现,不造数
 *  - 禁止任何内部标识:user_id / owner_user_id / agent_user_id / upstream_user_id /
 *    service_account_code / channel_account_code / cost_multiplier / 完整 token /
 *    诊断记录 ID / 内部异常堆栈 —— 本契约中不存在这些字段,映射层负责丢弃
 */

// ---------------------------------------------------------------------------
// 基础枚举
// ---------------------------------------------------------------------------

/** 证据等级(后端 services/report_evidence.py 口径) */
export type EvidenceLevel = 'A' | 'B' | 'C';

/**
 * 证据判定结果(客户可读措辞与统一观测 vNext 对齐)
 *
 * 与统一观测契约 geo-observation-contract-v1.1 target_outcome 的显式映射
 * (不粗暴合并;详见 docs/race/public-report-premium-race-a/PRESENTATION_CONTRACT.md):
 *  recommended                = vNext recommended                → 提及(正面措辞)
 *  conditionally_recommended  = vNext conditionally_recommended  → 提及(带条件措辞)
 *  candidate                  = vNext candidate_only             → 列入备选
 *  mentioned                  = vNext mentioned_only             → 仅提到
 *  criteria_only              = vNext criteria_only              → 只给标准
 *  refused_no_evidence        = vNext refused_no_evidence        → 证据不足拒答
 *  refused_risk               = vNext refused_risk               → 风险拒答
 *  not_mentioned              = vNext not_mentioned              → 未提到
 *  brand_confused             = vNext entity_ambiguous           → 品牌混淆
 *  engine_error               = vNext engine_error               → 引擎异常
 *  no_answer                  = 旧版空回答(response 为空)        → 无有效回答
 */
export type EvidenceVerdict =
    | 'recommended'
    | 'conditionally_recommended'
    | 'candidate'
    | 'mentioned'
    | 'criteria_only'
    | 'refused_no_evidence'
    | 'refused_risk'
    | 'not_mentioned'
    | 'brand_confused'
    | 'engine_error'
    | 'no_answer';

/** 关键发现类别 */
export type FindingCategory =
    | 'opportunity'           // 主要机会
    | 'risk'                  // 主要风险
    | 'profile_gap'           // 资料短板
    | 'source_gap'            // 信源短板
    | 'recommendation_blocker'; // 推荐阻断因素

/** 数据可信状态(与后端 level_meta.partial_sample / level_capped 对齐) */
export interface DataTrust {
    /** 存在空样本层,权重被重归一 */
    readonly partialSample: boolean;
    /** 单层覆盖导致等级被封顶(防过度承诺) */
    readonly levelCapped: boolean;
    /** 整体样本置信度 */
    readonly confidence: 'high' | 'medium' | 'low' | null;
}

// ---------------------------------------------------------------------------
// DataSection — 结构化区块三态
// ---------------------------------------------------------------------------

export type DataSection<T> =
    | { readonly status: 'ready'; readonly data: T }
    | { readonly status: 'empty'; readonly message: string }
    | { readonly status: 'unavailable'; readonly message: string };

// ---------------------------------------------------------------------------
// 报告身份区
// ---------------------------------------------------------------------------

/** 白标(仅后端明确批准的公开字段;联系方式已由后端脱敏) */
export interface PublicWhitelabel {
    readonly companyName: string | null;
    readonly productName: string | null;
    readonly logoUrl: string | null;
    readonly slogan: string | null;
    readonly brandColor: string | null;
}

export interface ReportIdentity {
    readonly brandName: string;
    /** 行业;后端未提供时为 null → UI 显示 "—" */
    readonly industry: string | null;
    /** 诊断时间 ISO */
    readonly diagnosedAt: string | null;
    /** 数据更新时间 ISO(report_v2_generated_at) */
    readonly dataUpdatedAt: string | null;
    /** 报告口径/版本,如 "v2 · 公开报告" */
    readonly reportVersionLabel: string;
    readonly whitelabel: PublicWhitelabel | null;
    readonly brandingStatus: 'approved_whitelabel' | 'platform' | null;
}

// ---------------------------------------------------------------------------
// 执行摘要
// ---------------------------------------------------------------------------

/** 等级(后端 FUNNEL_LEVEL_META 现算;颜色/文案由后端给,前端不自建映射) */
export interface ScoreLevel {
    readonly label: string;
    /** 后端 level_meta.color_hex;未提供时 null → UI 用中性色 */
    readonly colorHex: string | null;
    readonly summary: string | null;
    readonly businessMeaning: string | null;
}

export interface ExecutiveSummary {
    /** GEO 综合评分 0-100;fail-closed 缺失为 null → 显示 "—",绝不显示 0 */
    readonly geoScore: number | null;
    readonly scoreLevel: ScoreLevel | null;
    /** 资料完整度 0-100(独立指标,≠ GEO 评分) */
    readonly dataCompletenessScore: number | null;
    readonly dataCompletenessLevel: string | null;
    readonly dataCompletenessMissingSummary: string | null;
    /** 一句话结论(后端 module "1" conclusion_text / insight) */
    readonly headline: string | null;
    /** 本次实测平台数(后端派生;禁止前端硬编码 "4 大引擎") */
    readonly testedPlatformCount: number | null;
    /** 本次实测问题数 */
    readonly testedQuestionCount: number | null;
    /** 本次有效回答样本数 */
    readonly validAnswerCount: number | null;
    readonly trust: DataTrust;
    /** 下一步主要行动(标题级,详情见行动区) */
    readonly nextAction: string | null;
}

// ---------------------------------------------------------------------------
// 决策漏斗(层数与命名由后端给;当前 SSOT 为 3 层加权漏斗)
// ---------------------------------------------------------------------------

export interface FunnelLayer {
    readonly key: string;
    /** 后端层名,如 "品牌认知层" */
    readonly label: string;
    /** 普通话解释(后端 desc/business) */
    readonly description: string | null;
    readonly businessMeaning: string | null;
    /** 命中样本数 */
    readonly detected: number | null;
    /** 总样本数 */
    readonly total: number | null;
    /** 命中率 0-100(后端 rate_pct) */
    readonly ratePct: number | null;
    /** 该层对总分的贡献分(后端 score) */
    readonly score: number | null;
    /** 展示权重(后端 weight) */
    readonly weight: number | null;
    readonly dataSufficient: boolean;
    /**
     * [P0-5] 合并后的样本表述，如 "4/4 命中（样本较少，建议扩测到 8 题以上确认）"。
     * UI 必须用它取代"比率 + 独立的『样本不足』角标"那种左右脑互搏。
     */
    readonly sampleNote?: string | null;
    /** 命中率高但样本少 → 初步达标待扩测 */
    readonly provisional?: boolean;
    /** provisional 时取代大号百分比的标题文案，如 "初步达标 · 待扩测" */
    readonly headlineLabel?: string | null;
    readonly confidence: 'high' | 'medium' | 'low' | null;
}

export interface DecisionFunnelData {
    readonly layers: readonly FunnelLayer[];
    readonly totalScore: number | null;
    readonly trust: DataTrust;
}

// ---------------------------------------------------------------------------
// 平台表现
// ---------------------------------------------------------------------------

export interface PlatformPerformance {
    /** 平台公共名称(通义千问/DeepSeek/豆包/Kimi/元宝),绝不露 provider/surface/内部模型标识 */
    readonly platformName: string;
    /** 有效样本数 */
    readonly validSamples: number | null;
    /** 品牌识别率 0-100(含品牌定向题：回答"AI 认不认识这个品牌") */
    readonly detectionRatePct: number | null;
    /**
     * 提及率 0-100 = (被推荐 + 仅提到) / 有效样本
     * 口径已排除品牌定向题(P1-6)，是推荐率的**超集**
     */
    readonly mentionRatePct: number | null;
    /** 推荐率 0-100 = 被推荐 / 有效样本(被推荐 ⊂ 被提及) */
    readonly recommendRatePct: number | null;
    /** 引用(被引用来源条数)；不适用/未采全时为 null，理由见 citationStatus */
    readonly citationCount: number | null;
    /** citationCount 为 null 时的原因(如无联网检索表面)；有数则为 null */
    readonly citationStatus?: string | null;
    /** 提及率/推荐率的分母(已排除品牌定向题) */
    readonly competitiveSamples?: number | null;
    /** 被排除的品牌定向题样本数 */
    readonly brandDirectedSamples?: number | null;
    /**
     * [WO 2026-08-07] 身份待确认的样本数 —— **不计入上面任何一个分母**。
     *
     * 这些格子引擎答了、答案完整,只是品牌名需要人确认一下(比如答案里写的是
     * 「阿强龙虾」而登记名是「阿强小龙虾」)。旧版把它们混进「部分失败」——
     * 什么都没失败,而真正该说的「这几格没进出现率」一个字没说。
     *
     * 🔴 0 时后端给 `null` 而不是 `0`,前端据此整块不渲染:
     * 零待确认的报告里不许多出任何一句废话。
     */
    readonly identityPendingSamples?: number | null;
    /** 数据状态,如 "稳定" / "样本不足" / "部分失败" / "部分待确认" */
    readonly dataStatus: string | null;
    readonly updatedAt: string | null;
}

// ---------------------------------------------------------------------------
// 关键发现
// ---------------------------------------------------------------------------

export interface KeyFinding {
    readonly category: FindingCategory;
    /** 结论文案(后端证据驱动,推测不得写成事实) */
    readonly text: string;
    readonly evidenceLevel: EvidenceLevel | null;
    /** 支撑样本数量 */
    readonly sampleCount: number | null;
    /** 对应平台或问题范围,如 "豆包 · 选购咨询类问题" */
    readonly scope: string | null;
    /** 可展开的证据摘要 */
    readonly detail: string | null;
    /** 数据不足提示 */
    readonly insufficientNote: string | null;
}

// ---------------------------------------------------------------------------
// 证据矩阵
// ---------------------------------------------------------------------------

export interface EvidenceItem {
    /** 行内唯一键(展示用途,非数据库 ID) */
    readonly rowKey: string;
    /** 监测短句/问题 */
    readonly question: string;
    /** 平台公共名称 */
    readonly platformName: string;
    readonly verdict: EvidenceVerdict;
    /** 回答节选;为空时 UI 显示明确占位 */
    readonly answerExcerpt: string | null;
    /** 引用域名(已脱敏为域名级) */
    readonly citedDomains: readonly string[];
    readonly evidenceLevel: EvidenceLevel | null;
    readonly testedAt: string | null;
}

export interface EvidenceMatrixData {
    readonly items: readonly EvidenceItem[];
    readonly totalCount: number;
}

// ---------------------------------------------------------------------------
// 竞争格局
// ---------------------------------------------------------------------------

export interface CompetitorItem {
    readonly name: string;
    /** 被共同提及次数 */
    readonly mentionCount: number;
    /** 被推荐次数;无可靠数据为 null */
    readonly recommendCount: number | null;
}

export interface CompetitiveData {
    /** 样本口径说明,如 "32 条有效 AI 回答 · 已排除 5 条品牌定向问答" */
    readonly sampleScope: string | null;
    readonly competitors: readonly CompetitorItem[];
    /** 本品牌被提及次数(有分母时才给比例) */
    readonly ownMentionCount: number | null;
    readonly ownRecommendCount: number | null;
    /** 无可靠分母 → 只显示次数,禁止拼百分比 */
    readonly hasReliableDenominator: boolean;
    readonly denominatorNote: string | null;
    /** 同行名单来源:本次诊断实测 / 近 90 天监测汇总 */
    readonly competitorSource?: 'diagnosis' | 'monitoring' | null;
    /** 名单为空的原因(区分"AI 真没点名同行"与"采集失败") */
    readonly emptyReason?: string | null;
    /**
     * [#236-c1a] 本次**被排除出竞争分母的「有效回答」条数**(不是题数:一道题 × 4 平台 = 4 条)。
     * 后端 `public_report_presentation.py` 按**实算**给,没排就给 0 —— 不再是常量口径。
     * 🔴 缺省(老报告)按 **0** 处理 ⇒ 不显示那句排除声明。
     *    **不许**回落成「按老样子显示」—— 那等于继续对客户宣称排过。
     */
    readonly excludedBrandDirectedCount?: number | null;
}

// ---------------------------------------------------------------------------
// 优先行动 & 30 天路径
// ---------------------------------------------------------------------------

export interface PriorityAction {
    /** 优先级标签(后端 P0/P1/P2 或 1-5 序号) */
    readonly priorityLabel: string;
    readonly title: string;
    /** 为什么做 */
    readonly why: string | null;
    /** 对应证据(证据行 rowKey 列表,UI 可跳证据区) */
    readonly evidenceRowKeys: readonly string[];
    /** 预计影响范围(后端措辞,不伪造收益) */
    readonly impactScope: string | null;
    /** 建议周期(后端措辞,不伪造天数承诺) */
    readonly suggestedPeriod: string | null;
    /** 可展开执行说明(Markdown) */
    readonly detailMd: string | null;
}

export interface ThirtyDayWeek {
    readonly weekIndex: 1 | 2 | 3 | 4;
    readonly theme: string;
    readonly items: readonly string[];
    /** true = 后端个性化数据;false = 通用方法(必须明示,不冒充个性化结论) */
    readonly personalized: boolean;
}

// ---------------------------------------------------------------------------
// 方法说明
// ---------------------------------------------------------------------------

export interface MethodologyNotes {
    /** 本次实际测试了什么(后端叙事或结构化描述) */
    readonly testedScope: string | null;
    /** 数据时间范围描述 */
    readonly timeRange: string | null;
    /** 平台范围描述(派生,不写死数量) */
    readonly platformScope: string | null;
    /** 分数计算口径说明 */
    readonly scoreMethod: string | null;
    /** 数据限制 */
    readonly dataLimits: readonly string[];
    /** 附加说明段落 */
    readonly extraNotes: readonly string[];
}

// ---------------------------------------------------------------------------
// 顶层展示模型
// ---------------------------------------------------------------------------

export interface PublicReportPresentationV1 {
    readonly contractVersion: 'public-report-presentation/v1';
    readonly identity: ReportIdentity;
    readonly summary: ExecutiveSummary;
    readonly funnel: DataSection<DecisionFunnelData>;
    readonly platforms: DataSection<readonly PlatformPerformance[]>;
    readonly findings: DataSection<readonly KeyFinding[]>;
    readonly evidence: DataSection<EvidenceMatrixData>;
    readonly competitive: DataSection<CompetitiveData>;
    readonly actions: DataSection<readonly PriorityAction[]>;
    readonly thirtyDayPlan: DataSection<readonly ThirtyDayWeek[]>;
    /** 客户版叙事 Markdown(report.content);仅叙事,数值不得从中解析 */
    readonly narrativeMd: string | null;
    readonly methodology: MethodologyNotes;
    /**
     * 待确认校准 —— **只有已登录且归属的服务商**才拿得到这个键。
     * 匿名 token 访客的响应体里根本不存在它(服务端裁剪,不是前端隐藏)。
     * 🔴 前端渲染条件就是"它在不在",**不许**再判一次身份 —— 那是第二处口径。
     */
    readonly calibration?: IdentityCalibration | null;
}

// ---------------------------------------------------------------------------
// 页面五态
// ---------------------------------------------------------------------------

export type PublicReportPageState =
    | { readonly kind: 'loading' }
    | { readonly kind: 'ready'; readonly report: PublicReportPresentationV1 }
    | { readonly kind: 'not_ready'; readonly message: string }
    | { readonly kind: 'invalid_link'; readonly message: string }
    | { readonly kind: 'error'; readonly message: string };


// ---------------------------------------------------------------------------
// 待确认校准(**只对已登录且归属的服务商出流**)
// ---------------------------------------------------------------------------

/**
 * [WO 2026-08-07 · Owner 拍板] 同一个分享链接两种视角。
 *
 * 🔴 这一段是**服务端裁剪**的产物:匿名 token 访客的响应体里根本没有
 * `calibration` 这个键。所以前端的渲染条件就是"数据在不在" ——
 * **不许**在前端再判一次身份:那会成为第二处口径,两处一旦不一致就是漏洞。
 */
export interface IdentityCalibrationItem {
    /** 这次问的是什么(客户能看懂的原话) */
    readonly question: string;
    /** 哪个平台答的(展示名) */
    readonly platformName: string;
    /** 确认端点按 (question, engine) 定位单元格,所以要原始引擎键 */
    readonly engine: string;
    /** 乐观锁版本(未处理过的格是 0) */
    readonly decisionVersion: number;
    /** 这次回答里出现的相近名字 */
    readonly similarNames: readonly string[];
    /** 回答节选(判断依据) */
    readonly answerExcerpt: string | null;
}

export interface IdentityCalibration {
    /** 确认端点(与工作台同一条后端路径,不开第二条写路径) */
    readonly decisionEndpoint: string;
    readonly pendingCount: number;
    readonly items: readonly IdentityCalibrationItem[];
}
