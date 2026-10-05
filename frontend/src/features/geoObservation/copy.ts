/**
 * 用户可见文案单一来源（镜像 contracts/frontend_copy_and_race_v1.json）。
 * 规则：普通运营是主读者——准确、平实、直接，不堆黑话，也不幼儿化。
 * 每个模块优先回答：发生了什么 / 为什么值得关注 / 下一步能做什么。
 *
 * 所有服务商/客户可见字符串都从这里取；文案验收直接导出本文件。
 */

import type { OutcomeKey, StabilityStatus } from './types';

// —— 十类回答结果（copy 合同 outcome_labels，逐字一致）——
export const OUTCOME_LABELS: Record<OutcomeKey, string> = {
  recommended: '被直接推荐',
  conditionally_recommended: '满足条件后可能推荐',
  candidate_only: '进入候选名单',
  mentioned_only: '被提到，但未形成推荐',
  criteria_only: 'AI 只给出了筛选标准',
  refused_no_evidence: '因证据不足，AI 暂不推荐具体品牌',
  refused_risk: '因风险限制，AI 不提供具体推荐',
  not_mentioned: '未提到',
  entity_ambiguous: '品牌可能混淆，暂不判断',
  engine_error: '本次检测未完成',
};

/** 一句话解释每类结果的业务含义（用于 tooltip / 图例，不抢主叙事）。 */
export const OUTCOME_HINTS: Record<OutcomeKey, string> = {
  recommended: 'AI 明确把品牌作为答案或推荐对象，并给出正向理由。',
  conditionally_recommended: 'AI 在预算、地区、场景或资质等条件下才推荐。',
  /* 🔴 [#233] 否定句里的「明确推荐」也换掉:全站停止用「推荐」描述这个判定之后,
     只在否定句里留着它,会让读者以为还存在一个「明确推荐」的正面档。
     换成判定真正测到的东西 —— 有没有出现正面措辞。 */
  candidate_only: '品牌进入对比名单，但 AI 没有给出正面措辞。',
  mentioned_only: '名称出现，但不构成候选或推荐。',
  criteria_only: 'AI 不点名品牌，只给出应该怎么挑选的标准。',
  refused_no_evidence: 'AI 因为无法核实资质或资料不足，暂不点名。',
  refused_risk: 'AI 因为法律、投诉、医疗、财务等风险不点名。',
  not_mentioned: '回答有效、实体明确，但没有提到目标品牌。',
  entity_ambiguous: '出现了相似品牌或别名，无法可靠区分，本条不计入结论。',
  engine_error: '本次采集超时、解析失败或供应商异常，不计入结论。',
};

/** 有效观测口径外的两类（不计入正负分母）。 */
export const EXCLUDED_FROM_RATE: OutcomeKey[] = ['entity_ambiguous', 'engine_error'];

// —— 结果稳定度 ——
export const STABILITY_LABELS: Record<StabilityStatus, string> = {
  stable: '结果较稳定',
  watch: '有波动，建议继续观察',
  insufficient: '样本不足，暂不下结论',
  shifted: '模型升级，前后结果不宜直接比较',
};

// —— 指标业务名（copy 合同 metric_labels）——
export const METRIC_LABELS = {
  presence_rate: '被 AI 提到的比例',
  explicit_recommendation_rate: '被直接推荐的比例',
  conditional_recommendation_rate: '满足条件后可能推荐的比例',
  candidate_rate: '进入候选名单的比例',
  criteria_only_rate: '只给出筛选标准的比例',
  refusal_no_evidence_rate: '因证据不足未给出具体推荐的比例',
  refusal_risk_rate: '因风险限制未给出具体推荐的比例',
  share_of_voice: '样本内品牌提及占比',
  citation_rate: '回答中提供引用来源的比例',
  evidence_coverage_rate: '有可核验证据的比例',
  valid_observations: '本次分析样本数',
  data_updated_at: '数据更新到',
} as const;

// —— 页面级文案 ——
export const PAGE = {
  workbench: {
    title: '统一观测',
    subtitle: '看 AI 搜索里，这个品牌被提到、被推荐还是被拒绝，以及下一步该做什么。',
    pickBrand: '先选择一个客户',
    pickBrandHint: '观测数据按客户读取。请选择你负责的客户，避免误看其他客户数据。',
    tabs: {
      overview: '总览',
      platforms: '平台表现',
      questions: '问题与证据',
      competition: '竞争格局',
      actions: '行动建议',
    },
    generateInsight: '生成洞察',
    regenerateInsight: '重新生成洞察',
    export: '导出概览',
  },
  overview: {
    whatHappened: '本周期结果分布',
    distributionHint: '按 AI 的推荐行为对每一次问答归类，帮你分清"被推荐""进候选""被拒绝"。',
    comparisonBlockedTitle: '本周期暂不与上周期直接比较',
    trendTitle: '趋势',
    trendHint: '模型升级期间，前后口径不一致，不能直接当作效果涨跌。',
  },
  platforms: {
    title: '各平台表现',
    hint: '不同 AI 平台的结果可能不同，分开看更准。',
    historical: '历史平台',
    historicalHint: '不再进入默认检测，仅保留历史记录供查询。',
    // section 六.7：用户端只用统一通用说明，不暴露具体代理/官方实现细节。
    collectionNote: '不同平台可能采用官方接口或搜索增强方式采集，口径变化期间趋势将分段展示。',
  },
  questions: {
    title: '问题与证据',
    hint: '每条问题的回答结果、命中的品牌文字和可查看的原始证据。',
    viewEvidence: '查看证据',
    changed: '较上次有变化',
    noEvidence: '这条暂时没有可展示的证据。',
  },
  competition: {
    title: '竞争格局',
    hint: '看本品牌在样本里被提及的相对多少，以及可能混淆的相似品牌。',
    sovTitle: '样本内品牌提及占比',
    sovHint: '本品牌被提及次数占样本内全部品牌被提及次数的比例，反映样本内热度，不是市场份额；不展示其他客户的品牌名单。',
    ambiguousTitle: '可能混淆的品牌（需人工确认）',
    ambiguousHint: '名称或别名相似，系统暂不合并，请人工确认是否为同一家。',
    // total 来自 AI-3 questions 计数，为该品牌**累计**（无时间窗），故不能标成"本周期"，避免与同屏按周期口径的 SOV 打架。
    scanLimitNote: (scanned: number, total: number) =>
      `仅扫描了最近 ${scanned} 条问题（该品牌累计共 ${total} 条），可能有遗漏；完整列表请到「问题与证据」逐页查看。`,
    // 与 scanLimitNote 口径一致：易混淆派生自"最近扫描的问题"（全时段累计，无周期窗），不标"本周期"。
    noAmbiguous: '在最近扫描的问题中，未发现明显易混淆的相似品牌。',
  },
  actions: {
    title: '下一步行动',
    hint: '按优先级给出建议，每条都说明原因。点击只会带你去对应入口，不会自动执行，也不会额外扣费。',
    noCharge: '不额外扣费',
    needConfirm: '需你确认后再执行',
    goWriting: '去写作',
    goPublish: '去发布',
    priority: '优先级',
  },
  evidence: {
    title: '证据详情',
    ownData: '以下是这个客户本次问答的原始内容。',
    answerExcerpt: 'AI 回答（节选）',
    noAnswerExcerpt: '本条暂无可展示的回答节选。',
    matched: '命中的品牌文字',
    citations: '引用来源',
    gap: '还缺哪些证据',
    channel: '本条结果的采集通道',
    close: '关闭',
  },
  insight: {
    generating: '正在生成洞察…',
    unavailable: 'AI 洞察暂不可用，图表和证据仍可正常查看。',
    retry: '重试',
    completedTitle: 'AI 洞察',
    basisTitle: '主要依据',
    onlyExplains: '洞察只用于解释和建议，具体动作仍需你确认。',
  },
  admin: {
    title: '观测治理中心',
    subtitle: '查看三来源样本状态、平台健康与模型漂移。治理写操作待接口就绪后接入，当前为只读。',
    tabs: {
      overview: '运行概览',
      platforms: '平台与模型',
      governance: '样本治理',
      industry: '行业趋势',
      opportunities: '内容与媒体机会',
    },
    readiness: {
      ready: '正常',
      attention_required: '需要关注',
      unavailable: '暂不可用',
    },
    kpi: {
      pending_review: '待审核晋升',
      private_only: '仅本客户可见',
      rejected: '已拒绝晋升',
      withdrawn: '已撤回来源',
      stuck_claims: '卡住的处理任务',
      reconciler_delay: '对账延迟',
      aggregate_updated: '聚合更新于',
      policy_version: '配置版本',
    },
    healthReadOnly: '平台健康为只读，由系统自动探测，不能在这里手工修改。',
    // 治理写接口（审核晋升/撤回、平台启停）由治理服务提供，尚未接入 → 只读 + 明确说明，不显示会 404 的按钮。
    governancePending: '样本晋升审核、撤回等写操作由治理接口提供，接口就绪后在此接入。当前为只读，仅展示各状态数量。',
    platformWritePending: '平台启停由治理配置接口提供，接口就绪后在此接入。当前为只读状态展示。',
    modelShiftTitle: '模型版本漂移',
    modelShiftHint: '各平台模型版本变化引起的口径断点，用于解释趋势分段，不解释为客户涨跌。',
    aggregateDiffTitle: '新旧口径对照',
  },
  methodology: {
    title: '方法说明',
    subtitle: '这些数字怎么来、多久采一次、什么时候因为样本太少不展示。',
    outcomesTitle: '十类回答结果',
    samplingTitle: '采样频率',
    limitsTitle: '已知局限',
    updatedAt: '更新于',
    version: '口径版本',
  },
  states: {
    loading: '正在加载…',
    empty: '暂无数据',
    error: '这份数据暂时没拿到，稍后再试。主流程不受影响。',
    forbidden: '你没有查看这项数据的权限。',
    conflict: '数据已被其他操作更新，请刷新后重试。',
    envOverride: '当前设置由运行环境统一管理，不能在这里修改。',
    unavailable: '观测数据暂不可用，请稍后重试。',
    insufficient: '样本不足，暂不下结论。',
    insufficientHint: '样本、独立来源或时间跨度还不够，继续积累后才会给出结论。',
    // section 六.8：不永久硬编码"模型升级"；标题中性化，具体说明来自后端字段。
    dataCaliberUpdated: '数据口径已更新',
    channelCollectionNote: '不同平台可能采用官方接口或搜索增强方式采集，口径变化期间趋势将分段展示。',
    retry: '重试',
    expand: '展开全部',
    collapse: '收起',
  },
} as const;

// 注：用户端禁用词（bps/watermark/outcome/... 与营销空话）清单仅用于测试期扫描，
// 定义在 tests/geo-observation-a/forbidden.ts，不进运行时 bundle。
