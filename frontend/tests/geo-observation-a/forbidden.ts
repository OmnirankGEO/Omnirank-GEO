/**
 * 用户端禁用词清单（仅测试用，来自 copy 合同）。不进运行时 bundle。
 */

// 工程黑话：用户/服务商可见文本里不得出现（无中文解释层）。
export const FORBIDDEN_USER_TERMS = [
  'bps',
  'watermark',
  'outcome',
  'source_kind',
  'surface_key',
  'policy_version',
  'metric_version',
  'HMAC',
  'k-anonymity',
  'Pub/Sub',
  'worker',
  'provider_trace_id',
  'fanout',
];

// 无法验证的营销空话。
export const FORBIDDEN_MARKETING_TERMS = [
  '智能决策中枢',
  '认知资产跃迁',
  '全域势能',
  '增长飞轮赋能',
  '保证上榜',
];

// 跨客户 / 上游 / 内部成本 / provider trace —— DOM/storage 绝不出现。
export const FORBIDDEN_PRIVACY_TOKENS = [
  'owner_user_id',
  'agent_user_id',
  'upstream_user_id',
  'provider_trace_id',
  'cost_multiplier',
  'internal_cost',
  'service_account_code',
  'channel_account_code',
];

// 采集实现细节（代理/官方通道 surface note）—— 用户端绝不出现（section 六）。
export const FORBIDDEN_CHANNEL_DETAIL = ['秘塔', '检索代理', 'metaso', 'proxy'];
