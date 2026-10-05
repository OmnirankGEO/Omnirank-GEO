/**
 * mapDto — 后端 DTO → PublicReportPresentationV1 的 fail-closed 映射
 *
 * 铁律:
 *  - 异常形态(越界分数/非整数)不由前端修正 → 丢弃为 null 并记安全日志
 *  - score 与 level 是后端 SSOT 对偶:任一缺失/非法 → 两者一起不可用;
 *    前端不内嵌阈值表,不做"分数↔等级跨档冲突"推断(等级作不透明文本透传)
 *  - 安全日志只含字段名与原因,绝不包含原始值、token、内部 ID
 *  - 当前 DTO 未下发结构化区块时 → DataSection 'unavailable'(诚实呈现)
 *  - dto.presentation 由统一后端候选提供;非法或缺失区块独立降级
 */
import type { PublicReportDto } from '../contract/dto';
import type {
    IdentityCalibration,
    CompetitiveData,
    DataSection,
    DataTrust,
    DecisionFunnelData,
    EvidenceMatrixData,
    KeyFinding,
    MethodologyNotes,
    PlatformPerformance,
    PriorityAction,
    PublicReportPresentationV1,
    ThirtyDayWeek,
} from '../contract/types';

/** 安全日志:只记录字段名与原因类别,不含任何业务值 */
function logContractViolation(field: string, reason: string): void {
    if (typeof console !== 'undefined' && typeof console.warn === 'function') {
        console.warn(`[public-report] dto field dropped: ${field} (${reason})`);
    }
}

function asScore(value: unknown, field: string): number | null {
    if (value === null || value === undefined) return null;
    if (typeof value !== 'number' || !Number.isFinite(value) || !Number.isInteger(value)) {
        logContractViolation(field, 'not_int');
        return null;
    }
    if (value < 0 || value > 100) {
        logContractViolation(field, 'out_of_range');
        return null;
    }
    return value;
}

function asNonNegativeInt(value: unknown, field: string): number | null {
    if (value === null || value === undefined) return null;
    if (typeof value !== 'number' || !Number.isFinite(value) || !Number.isInteger(value) || value < 0) {
        logContractViolation(field, 'not_non_negative_int');
        return null;
    }
    return value;
}

function asStringOrNull(value: unknown): string | null {
    if (typeof value !== 'string') return null;
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : null;
}

function asIsoOrNull(value: unknown, field: string): string | null {
    const text = asStringOrNull(value);
    if (!text) return null;
    const time = Date.parse(text);
    if (Number.isNaN(time)) {
        logContractViolation(field, 'bad_date');
        return null;
    }
    return new Date(time).toISOString();
}

const SECTION_UNAVAILABLE =
    '该部分结构化数据尚未由数据通道开放,当前仅展示已有信息。';
const SECTION_EMPTY = '本次没有形成可展示的数据。';

// ---------------------------------------------------------------------------
// dto.presentation 的结构校验(形状见契约文档)
// ---------------------------------------------------------------------------

interface PresentationSections {
    funnel?: DataSection<DecisionFunnelData>;
    platforms?: DataSection<readonly PlatformPerformance[]>;
    findings?: DataSection<readonly KeyFinding[]>;
    evidence?: DataSection<EvidenceMatrixData>;
    competitive?: DataSection<CompetitiveData>;
    actions?: DataSection<readonly PriorityAction[]>;
    thirtyDayPlan?: DataSection<readonly ThirtyDayWeek[]>;
}

function isObject(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * 校验一个 DataSection 包装。data 的深度校验随后端契约冻结后加强;
 * 当前原则:包装非法 → unavailable(不误展示)。
 */
function parseSection<T>(
    value: unknown,
    field: string,
    validateData: (data: unknown) => data is T,
): DataSection<T> {
    if (!isObject(value) || typeof value.status !== 'string') {
        logContractViolation(field, 'bad_section_wrap');
        return { status: 'unavailable', message: SECTION_UNAVAILABLE };
    }
    if (value.status === 'ready' && validateData(value.data)) {
        return { status: 'ready', data: value.data };
    }
    if (value.status === 'empty') {
        return { status: 'empty', message: SECTION_EMPTY };
    }
    if (value.status === 'unavailable') {
        return { status: 'unavailable', message: SECTION_UNAVAILABLE };
    }
    logContractViolation(field, 'bad_section_payload');
    return { status: 'unavailable', message: SECTION_UNAVAILABLE };
}

const isNullableString = (value: unknown): value is string | null =>
    value === null || typeof value === 'string';
const isNullableScore = (value: unknown): value is number | null =>
    value === null || (typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100);
const isNullableCount = (value: unknown): value is number | null =>
    value === null || (typeof value === 'number' && Number.isInteger(value) && value >= 0);
const isConfidence = (value: unknown): value is 'high' | 'medium' | 'low' | null =>
    value === null || value === 'high' || value === 'medium' || value === 'low';
const isEvidenceLevel = (value: unknown): value is 'A' | 'B' | 'C' | null =>
    value === null || value === 'A' || value === 'B' || value === 'C';
const isStringArray = (value: unknown): value is readonly string[] =>
    Array.isArray(value) && value.every((item) => typeof item === 'string');
const isNullableIso = (value: unknown): value is string | null =>
    value === null || (typeof value === 'string' && !Number.isNaN(Date.parse(value)));
/** 可选字段：老后端不带该键(undefined) 也合法，不因新增契约把整段判成 unavailable */
const isOptionalString = (value: unknown): value is string | null | undefined =>
    value === undefined || isNullableString(value);
const isOptionalCount = (value: unknown): value is number | null | undefined =>
    value === undefined || isNullableCount(value);

function validTrust(value: unknown): boolean {
    return isObject(value)
        && typeof value.partialSample === 'boolean'
        && typeof value.levelCapped === 'boolean'
        && isConfidence(value.confidence);
}

function validFunnelLayer(value: unknown): boolean {
    return isObject(value)
        && typeof value.key === 'string'
        && typeof value.label === 'string'
        && isNullableString(value.description)
        && isNullableString(value.businessMeaning)
        && isNullableCount(value.detected)
        && isNullableCount(value.total)
        && isNullableScore(value.ratePct)
        && isNullableScore(value.score)
        && isNullableScore(value.weight)
        && typeof value.dataSufficient === 'boolean'
        // [P0-5] 合并样本表述（老后端不带这些键 → undefined 合法）
        && isOptionalString(value.sampleNote)
        && (value.provisional === undefined || typeof value.provisional === 'boolean')
        && isOptionalString(value.headlineLabel)
        && isConfidence(value.confidence)
        && !(value.detected !== null && value.total !== null && value.detected > value.total);
}

const validFunnel = (d: unknown): d is DecisionFunnelData =>
    isObject(d)
    && Array.isArray(d.layers)
    && d.layers.every(validFunnelLayer)
    && isNullableScore(d.totalScore)
    && validTrust(d.trust);

function validPlatform(value: unknown): boolean {
    return isObject(value)
        && typeof value.platformName === 'string'
        && isNullableCount(value.validSamples)
        && isNullableScore(value.detectionRatePct)
        && isNullableScore(value.mentionRatePct)
        && isNullableScore(value.recommendRatePct)
        && isNullableCount(value.citationCount)
        // [P0-3 ④] citationCount=null 时的原因（如"无联网检索表面 → 引用数不适用"）
        && isOptionalString(value.citationStatus)
        // [P1-6] 提及率/推荐率分母（已排除品牌定向题）与被排除的样本数
        && isOptionalCount(value.competitiveSamples)
        && isOptionalCount(value.brandDirectedSamples)
        // [WO_UNKNOWN_DENOMINATOR_DISCLOSURE 2026-08-07] 身份待确认样本数。
        // 🔴 少了这一行,后端给的字段过不了 DTO 校验 —— 整块明示会静默消失,
        //    而且**不会有任何报错**(校验是布尔函数,不合格就整段降级)。
        //    复审点名要把映射层纳入范围,就是这个原因。
        && isOptionalCount(value.identityPendingSamples)
        && isNullableString(value.dataStatus)
        && isNullableIso(value.updatedAt);
}

function validCalibration(value: unknown): boolean {
    // 🔴 可选段:不存在(匿名视角服务端裁剪掉了)也算合法。
    //    存在时必须整段合规 —— 半截数据会让服务商看到点不动的按钮。
    if (value === undefined || value === null) return true;
    if (!isObject(value)) return false;
    if (typeof value.decisionEndpoint !== 'string') return false;
    if (typeof value.pendingCount !== 'number') return false;
    if (!Array.isArray(value.items)) return false;
    return value.items.every((item) => isObject(item)
        && typeof item.question === 'string'
        && typeof item.platformName === 'string'
        && typeof item.engine === 'string'
        && typeof item.decisionVersion === 'number'
        && Array.isArray(item.similarNames)
        && isNullableString(item.answerExcerpt));
}

const validPlatforms = (d: unknown): d is readonly PlatformPerformance[] =>
    Array.isArray(d) && d.every(validPlatform);

const FINDING_CATEGORIES = new Set([
    'opportunity', 'risk', 'profile_gap', 'source_gap', 'recommendation_blocker',
]);
function validFinding(value: unknown): boolean {
    return isObject(value)
        && typeof value.category === 'string'
        && FINDING_CATEGORIES.has(value.category)
        && typeof value.text === 'string'
        && isEvidenceLevel(value.evidenceLevel)
        && isNullableCount(value.sampleCount)
        && isNullableString(value.scope)
        && isNullableString(value.detail)
        && isNullableString(value.insufficientNote);
}

const validFindings = (d: unknown): d is readonly KeyFinding[] =>
    Array.isArray(d) && d.every(validFinding);

const EVIDENCE_VERDICTS = new Set([
    // 统一观测 vNext 全量 11 类(与 contract/types.ts EvidenceVerdict 一一对应)
    'recommended',
    'conditionally_recommended',
    'candidate',
    'mentioned',
    'criteria_only',
    'refused_no_evidence',
    'refused_risk',
    'not_mentioned',
    'brand_confused',
    'engine_error',
    'no_answer',
]);
function validEvidenceItem(value: unknown): boolean {
    return isObject(value)
        && typeof value.rowKey === 'string'
        && typeof value.question === 'string'
        && typeof value.platformName === 'string'
        && typeof value.verdict === 'string'
        && EVIDENCE_VERDICTS.has(value.verdict)
        && isNullableString(value.answerExcerpt)
        && isStringArray(value.citedDomains)
        && isEvidenceLevel(value.evidenceLevel)
        && isNullableIso(value.testedAt);
}

const validEvidence = (d: unknown): d is EvidenceMatrixData =>
    isObject(d)
    && Array.isArray(d.items)
    && d.items.every(validEvidenceItem)
    && typeof d.totalCount === 'number'
    && Number.isInteger(d.totalCount)
    && d.totalCount >= d.items.length;

function validCompetitor(value: unknown): boolean {
    return isObject(value)
        && typeof value.name === 'string'
        && typeof value.mentionCount === 'number'
        && Number.isInteger(value.mentionCount)
        && value.mentionCount >= 0
        && isNullableCount(value.recommendCount);
}

const validCompetitive = (d: unknown): d is CompetitiveData =>
    isObject(d)
    && isNullableString(d.sampleScope)
    && Array.isArray(d.competitors)
    && d.competitors.every(validCompetitor)
    && isNullableCount(d.ownMentionCount)
    && isNullableCount(d.ownRecommendCount)
    && typeof d.hasReliableDenominator === 'boolean'
    && isNullableString(d.denominatorNote)
    // [P1-7] 同行名单来源 + 名单为空的原因（区分"AI 真没点名"与"采集失败"）
    && (d.competitorSource === undefined || d.competitorSource === null
        || d.competitorSource === 'diagnosis' || d.competitorSource === 'monitoring')
    && isOptionalString(d.emptyReason)
    // [#236-c1a] 老报告没有这个字段 ⇒ undefined 合法,渲染侧按 0 处理(不显示排除声明)
    && isOptionalCount(d.excludedBrandDirectedCount);

function validAction(value: unknown): boolean {
    return isObject(value)
        && typeof value.priorityLabel === 'string'
        && typeof value.title === 'string'
        && isNullableString(value.why)
        && isStringArray(value.evidenceRowKeys)
        && isNullableString(value.impactScope)
        && isNullableString(value.suggestedPeriod)
        && isNullableString(value.detailMd);
}

const validActions = (d: unknown): d is readonly PriorityAction[] =>
    Array.isArray(d) && d.every(validAction);

function validWeek(value: unknown): boolean {
    return isObject(value)
        && (value.weekIndex === 1 || value.weekIndex === 2 || value.weekIndex === 3 || value.weekIndex === 4)
        && typeof value.theme === 'string'
        && isStringArray(value.items)
        && typeof value.personalized === 'boolean';
}

const validThirtyDay = (d: unknown): d is readonly ThirtyDayWeek[] =>
    Array.isArray(d) && d.every(validWeek);

// ---------------------------------------------------------------------------
// presentation.identity / summary / methodology(可选增强载荷,非 DataSection)
// ---------------------------------------------------------------------------

interface PresentationIdentity {
    industry?: string | null;
}

interface SummaryEnrichment {
    headline?: string | null;
    testedPlatformCount?: number | null;
    /**
     * [#236-c1c] 本次**去重题面数**,由后端 `sampling["questionCount"]` 发出 ——
     * 与方法说明「本次实测 N 个问题」、证据矩阵每平台那个 N **同源**。
     * 🔴 不是关键词数:#700 真客户报告上顶部显示 1(关键词数)、方法说明 3、
     *    每平台 3 —— 顶部那个 1 与谁都对不上。
     */
    testedQuestionCount?: number | null;
    validAnswerCount?: number | null;
    trust?: DataTrust;
    nextAction?: string | null;
}

function validPresentationIdentity(value: unknown): value is PresentationIdentity {
    return isObject(value) && (value.industry === undefined || isNullableString(value.industry));
}

function validSummaryEnrichment(value: unknown): value is SummaryEnrichment {
    return isObject(value)
        && (value.headline === undefined || isNullableString(value.headline))
        && (value.testedPlatformCount === undefined || isNullableCount(value.testedPlatformCount))
        && (value.testedQuestionCount === undefined || isNullableCount(value.testedQuestionCount))
        && (value.validAnswerCount === undefined || isNullableCount(value.validAnswerCount))
        && (value.trust === undefined || validTrust(value.trust))
        && (value.nextAction === undefined || isNullableString(value.nextAction));
}

function validMethodologyPayload(value: unknown): value is MethodologyNotes {
    return isObject(value)
        && isNullableString(value.testedScope)
        && isNullableString(value.timeRange)
        && isNullableString(value.platformScope)
        && isNullableString(value.scoreMethod)
        && isStringArray(value.dataLimits)
        && isStringArray(value.extraNotes);
}

function parsePresentationSections(raw: unknown): PresentationSections {
    if (!isObject(raw)) return {};
    const sections: PresentationSections = {};
    if ('funnel' in raw) sections.funnel = parseSection(raw.funnel, 'presentation.funnel', validFunnel);
    if ('platforms' in raw) sections.platforms = parseSection(raw.platforms, 'presentation.platforms', validPlatforms);
    if ('findings' in raw) sections.findings = parseSection(raw.findings, 'presentation.findings', validFindings);
    if ('evidence' in raw) sections.evidence = parseSection(raw.evidence, 'presentation.evidence', validEvidence);
    if ('competitive' in raw) sections.competitive = parseSection(raw.competitive, 'presentation.competitive', validCompetitive);
    if ('actions' in raw) sections.actions = parseSection(raw.actions, 'presentation.actions', validActions);
    if ('thirtyDayPlan' in raw) sections.thirtyDayPlan = parseSection(raw.thirtyDayPlan, 'presentation.thirtyDayPlan', validThirtyDay);
    return sections;
}

// ---------------------------------------------------------------------------
// 主映射
// ---------------------------------------------------------------------------

export function mapDtoToPresentation(dto: PublicReportDto): PublicReportPresentationV1 {
    const parsedScore = asScore(dto.score, 'score');
    const parsedLevel = asStringOrNull(dto.level);
    const scoreLevelPairInvalid = (parsedScore === null) !== (parsedLevel === null);
    if (scoreLevelPairInvalid) {
        logContractViolation('score_level_pair', 'incomplete_pair');
    }
    const score = scoreLevelPairInvalid ? null : parsedScore;
    const level = scoreLevelPairInvalid ? null : parsedLevel;

    const completeness = asScore(dto.data_completeness_score, 'data_completeness_score');
    const breakdown = isObject(dto.data_completeness_breakdown)
        ? dto.data_completeness_breakdown
        : null;
    /*
     * 🔴 [#236-c1c] `keyword_count` 现在**没有任何消费方**(顶部那个数已改读后端的题数)。
     *    但这一行保留:`asNonNegativeInt` 不抛错,它的作用是**记录契约违规**
     *    (`logContractViolation`)—— 字段还在 DTO 里,停掉这个信号不在本单授权内。
     *    去掉无用的变量绑定,只留副作用,免得它看起来像「有人在用这个值」。
     */
    asNonNegativeInt(dto.keyword_count, 'keyword_count');
    const version = asStringOrNull(dto.report_version);
    const whitelabel = isObject(dto.whitelabel) ? dto.whitelabel : null;

    const sections = parsePresentationSections(dto.presentation);
    const unavailable = <T,>(): DataSection<T> => ({
        status: 'unavailable',
        message: SECTION_UNAVAILABLE,
    });

    // presentation.identity / summary / methodology(可选增强载荷;非法 → 降级 null 并记安全日志)
    const rawPresentation = isObject(dto.presentation) ? dto.presentation : null;
    let presentationIdentity: PresentationIdentity | null = null;
    let summaryEnrichment: SummaryEnrichment | null = null;
    let methodologyPayload: MethodologyNotes | null = null;
    let calibrationPayload: IdentityCalibration | null = null;
    if (rawPresentation) {
        if ('identity' in rawPresentation) {
            if (validPresentationIdentity(rawPresentation.identity)) {
                presentationIdentity = rawPresentation.identity;
            } else {
                logContractViolation('presentation.identity', 'bad_payload');
            }
        }
        if ('summary' in rawPresentation) {
            if (validSummaryEnrichment(rawPresentation.summary)) {
                summaryEnrichment = rawPresentation.summary;
            } else {
                logContractViolation('presentation.summary', 'bad_payload');
            }
        }
        if ('methodology' in rawPresentation) {
            if (validMethodologyPayload(rawPresentation.methodology)) {
                methodologyPayload = rawPresentation.methodology;
            } else {
                logContractViolation('presentation.methodology', 'bad_payload');
            }
        }
        // [WO 2026-08-07] 服务商视角才有的校准段。匿名视角这个键**根本不存在**
        // (服务端裁剪),所以下面这个 `in` 为假 → calibrationPayload 保持 null
        // → 整块不渲染。前端不判身份,只看数据在不在。
        if ('calibration' in rawPresentation) {
            if (validCalibration(rawPresentation.calibration)) {
                calibrationPayload = (rawPresentation.calibration as IdentityCalibration | null) ?? null;
            } else {
                logContractViolation('presentation.calibration', 'bad_payload');
            }
        }
    }

    // 可信状态:优先 presentation.summary.trust;缺省回落漏斗同源 trust(SSOT 同对象,非重算)
    const fallbackTrust: DataTrust =
        sections.funnel?.status === 'ready'
            ? sections.funnel.data.trust
            : { partialSample: false, levelCapped: false, confidence: null };

    return {
        contractVersion: 'public-report-presentation/v1',
        identity: {
            brandName: asStringOrNull(dto.brand_name) ?? '—',
            industry: presentationIdentity?.industry ?? null,
            diagnosedAt: asIsoOrNull(dto.created_at, 'created_at'),
            dataUpdatedAt: asIsoOrNull(dto.report_v2_generated_at, 'report_v2_generated_at'),
            reportVersionLabel: version === 'v2' ? 'v2 · 公开报告' : version === 'v1' ? 'v1 · 公开报告' : '—',
            whitelabel: whitelabel
                ? {
                      companyName: asStringOrNull(whitelabel.company_name),
                      productName: asStringOrNull(whitelabel.product_name),
                      logoUrl: asStringOrNull(whitelabel.logo_url),
                      slogan: asStringOrNull(whitelabel.slogan),
                      brandColor: asStringOrNull(whitelabel.brand_color),
                  }
                : null,
            brandingStatus:
                dto.branding_status === 'approved_whitelabel' || dto.branding_status === 'platform'
                    ? dto.branding_status
                    : null,
        },
        summary: {
            geoScore: score,
            scoreLevel: level ? { label: level, colorHex: null, summary: null, businessMeaning: null } : null,
            dataCompletenessScore: completeness,
            dataCompletenessLevel: breakdown ? asStringOrNull(breakdown.level) : null,
            dataCompletenessMissingSummary: breakdown ? asStringOrNull(breakdown.missing_summary) : null,
            headline: summaryEnrichment?.headline ?? null,
            testedPlatformCount: summaryEnrichment?.testedPlatformCount ?? null, // 禁止硬编码 "4 大引擎"
            /*
             * 🔴 [#236-c1c] 这一行原来是 `keywordCount` —— **根本没读后端字段**,
             *    而紧挨着的上一行 `testedPlatformCount` 是读的。一个读后端、一个自己顶,挨着写的。
             *    后果:#700 真客户报告顶部显示「真实问题 1 个」(该品牌只有 1 个关键词),
             *    而方法说明说 3、四个平台各说 3 —— 同一页三个数,顶部那个与谁都对不上。
             * 🔴 回落到 `null` 而不是 `keywordCount`:拿关键词数冒充题数正是本单要治的病,
             *    在「后端没给」时再顶一次,等于把缺陷留一条后路。
             */
            testedQuestionCount: summaryEnrichment?.testedQuestionCount ?? null,
            validAnswerCount: summaryEnrichment?.validAnswerCount ?? null,
            trust: summaryEnrichment?.trust ?? fallbackTrust,
            nextAction: summaryEnrichment?.nextAction ?? null,
        },
        funnel: sections.funnel ?? unavailable(),
        platforms: sections.platforms ?? unavailable(),
        findings: sections.findings ?? unavailable(),
        evidence: sections.evidence ?? unavailable(),
        competitive: sections.competitive ?? unavailable(),
        actions: sections.actions ?? unavailable(),
        thirtyDayPlan: sections.thirtyDayPlan ?? unavailable(),
        narrativeMd: asStringOrNull(dto.content),
        methodology: methodologyPayload ?? {
            testedScope: null,
            timeRange: null,
            platformScope: null,
            scoreMethod: null,
            dataLimits: [],
            extraNotes: [],
        },
        calibration: calibrationPayload,
    };
}
