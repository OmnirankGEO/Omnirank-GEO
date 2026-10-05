/**
 * 「团队与席位」模块的**枚举 → 人话**唯一映射表。
 *
 * [P0 2026-08-17 · 工单 §2 修法定式]
 *   每个枚举一张**完备映射表**;fallback **禁止 `|| rawValue`** —— 那就是英文出口
 *   (审计 B9 实锤:`statusLabel` 的 `|| status` 让后端任何漏网状态直接英文上屏)。
 *   漏网值一律渲染成「未知状态（xxx）」形态:用户看得懂「这是个我们没认出来的状态」,
 *   客服也拿得到原始码。
 *
 *   完备性由 `frontend/scripts/verify-organization-enum-labels.mjs` 机械上锁:
 *   它从**后端枚举定义**(services/organization_contract.py 的 LIMIT_KINDS /
 *   APPROVAL_ACTIONS / BILLABLE_FEATURE_CAPABILITIES、迁移 SQL 的 CHECK 约束、
 *   `_audit(action=...)` 全量 raise 点)取全集,与本文件逐项 diff,漏一项即红。
 *   ⚠️ 判据打**枚举定义全集**,不打手挑样例 —— 手挑必漏。
 */

/** 漏网枚举值的统一形态。绝不回落到裸英文。 */
export function unknownLabel(raw: unknown, noun = '状态'): string {
  const value = String(raw ?? '').trim();
  return value ? `未知${noun}（${value}）` : `未知${noun}`;
}

function pick(
  table: Record<string, string>,
  raw: unknown,
  noun: string,
): string {
  const value = String(raw ?? '').trim();
  return table[value] ?? unknownLabel(value, noun);
}

/* ── 成员 / 邀请状态 ───────────────────────────────────────────────────────
 * 成员来源:organization_memberships.status CHECK
 *   ('active','suspended','leaving','left','removed')
 * 邀请来源:organization_invites.status CHECK
 *   ('pending','accepted','revoked','expired')
 * 两者同渲染在成员表「状态」列,合并成一张表。 */
export const MEMBER_STATUS_LABELS: Record<string, string> = {
  active: '在职',
  suspended: '已暂停',
  leaving: '退出处理中',
  left: '已退出',
  removed: '已移除',
  pending: '待接受',
  accepted: '已接受',
  revoked: '已撤销',
  expired: '已到期',
};
export const memberStatusLabel = (raw: unknown) => pick(MEMBER_STATUS_LABELS, raw, '状态');

/* ── 使用上限 ──────────────────────────────────────────────────────────────
 * 来源:services/organization_contract.py LIMIT_KINDS
 *   ('daily_total','monthly_total','daily_feature','monthly_feature','custom') */
export const LIMIT_KIND_LABELS: Record<string, string> = {
  daily_total: '每日总上限',
  monthly_total: '每月总上限',
  daily_feature: '单功能日上限',
  monthly_feature: '单功能月上限',
  custom: '自定义时段上限',
};
export const limitKindLabel = (raw: unknown) => pick(LIMIT_KIND_LABELS, raw, '上限类型');

/** 来源:organization_spend_limits.status CHECK ('open','closing','closed') */
export const LIMIT_STATUS_LABELS: Record<string, string> = {
  open: '生效中',
  closing: '结算中',
  closed: '已结束',
};
export const limitStatusLabel = (raw: unknown) => pick(LIMIT_STATUS_LABELS, raw, '状态');

/* ── 功能代码 ──────────────────────────────────────────────────────────────
 * 来源:services/organization_contract.py BILLABLE_FEATURE_CAPABILITIES 的**全部 key**。
 * 这是「一次操作扣哪一项算力」的目录,不是人民币价目表。 */
export const FEATURE_CODE_LABELS: Record<string, string> = {
  geo_diagnosis: '品牌诊断',
  social_diagnosis: '社媒诊断',
  full_diagnosis: '全项诊断',
  report_regen: '重出报告',
  quote_generate: '报价方案',
  quote_keyword_append: '报价加词',
  quote_keyword_replace: '报价换词',
  keyword_expand: '关键词扩展',
  keyword_price: '关键词估价',
  brand_fill: '客户资料补全',
  autofill_brand: 'AI 联网补全客户资料',
  deep_analyze: '客户深度分析',
  article_gen: 'AI 写文章',
  article_rewrite: '文章改写',
  topic_gen: '文章标题',
  report_export: '导出报告',
  monitor_single: '效果监测',
  monitoring_keyword_daily: '监测词按天计费',
  scheduled_monitoring: '定时监测',
  geo_research_selfserve: '自助调研',
  media_publish: '发布投放',
  media_proxy_publish: '代发投放',
  mktg_moments_copy: '朋友圈文案',
  mktg_poster_basic: '海报（基础）',
  mktg_poster_pro: '海报（进阶）',
  mktg_bundle_std: '营销包（标准）',
  mktg_bundle_pro: '营销包（进阶）',
};
export const featureCodeLabel = (raw: unknown) => pick(FEATURE_CODE_LABELS, raw, '功能');

/* ── 审批 ──────────────────────────────────────────────────────────────────
 * 状态来源:organization_approval_requests.status CHECK
 *   ('pending','approved','rejected','expired','executing','executed','failed','revoked') */
export const APPROVAL_STATUS_LABELS: Record<string, string> = {
  pending: '待审批',
  approved: '已通过',
  rejected: '已拒绝',
  expired: '已过期',
  executing: '执行中',
  executed: '已执行',
  failed: '执行失败',
  revoked: '已撤回',
};
export const approvalStatusLabel = (raw: unknown) => pick(APPROVAL_STATUS_LABELS, raw, '状态');

/** 来源:services/organization_contract.py APPROVAL_ACTIONS（EXTERNAL_ACTIONS ∪ 4 条） */
export const APPROVAL_ACTION_LABELS: Record<string, string> = {
  'quote.send_external': '对外发送报价',
  'diagnosis_report.share_external': '对外分享诊断报告',
  'monitoring_report.share_external': '对外分享监测报告',
  'portal.issue_external_token': '生成客户查看链接',
  'publish.execute': '真实发布',
  'billing.execute_high_cost': '高额算力消费',
  'artifact.delete': '删除团队内容',
  'artifact.archive': '归档团队内容',
  'artifact.handoff': '交接团队内容',
};
export const approvalActionLabel = (raw: unknown) => pick(APPROVAL_ACTION_LABELS, raw, '操作');

/* ── 自动计划 ──────────────────────────────────────────────────────────────
 * 来源:organization_automatic_plans.status CHECK
 *   ('active','paused','completed','cancelled') */
export const PLAN_STATUS_LABELS: Record<string, string> = {
  active: '运行中',
  paused: '已暂停',
  completed: '已完成',
  cancelled: '已取消',
};
export const planStatusLabel = (raw: unknown) => pick(PLAN_STATUS_LABELS, raw, '状态');

/* ── 审计记录 ──────────────────────────────────────────────────────────────
 * 来源:services/organization_*.py 里全部 `_audit(action=...)` 站点(含
 * f-string 展开的 `member.{suspend|resume|remove}`、`approval.{approve|reject}`、
 * `automatic_plan.{active|paused|cancelled}`、`assignment.replace`)。 */
export const AUDIT_ACTION_LABELS: Record<string, string> = {
  'organization.create': '创建团队',
  'role.create': '新建角色',
  'role.update': '修改角色',
  'invite.create': '发出邀请',
  'invite.resend': '重发邀请',
  'invite.revoke': '撤销邀请',
  'invite.accept': '员工接受邀请',
  'invite.onboard': '员工开通账号',
  'invite.verify': '邀请验证',
  'invite.delivery_status': '邀请送达状态更新',
  'invite.access_policy.apply': '应用邀请里的权限设置',
  'invite.assignment.apply': '应用邀请里的客户分配',
  'member.suspend': '暂停员工',
  'member.resume': '恢复员工',
  'member.remove': '移除员工',
  'member.leave': '员工退出团队',
  'member.capabilities.override': '调整员工权限',
  'assignment.replace': '更新客户分配',
  'assignment.handoff': '客户交接',
  'assignment.brand_owner_cascade_revoke': '客户转出后自动收回分配',
  'limit.configure': '设置使用上限',
  'approval.submit': '提交审批申请',
  'approval.approve': '审批通过',
  'approval.reject': '审批拒绝',
  'approval.revoke': '撤回审批申请',
  'approval_policy.configure': '调整审批规则',
  'artifact.share_internal': '团队内共享内容',
  'artifact.revoke_share': '取消内容共享',
  'artifact.issue_public_token': '生成对外查看链接',
  'automatic_plan.create': '创建自动计划',
  'automatic_plan.active': '恢复自动计划',
  'automatic_plan.paused': '暂停自动计划',
  'automatic_plan.cancelled': '取消自动计划',
  'automatic_plan.completed': '自动计划已完成',
  'automatic_plan.cancel_brand_soft_delete': '客户删除后取消自动计划',
  'payer_policy.update': '调整员工费用代付设置',
  'billing.reserve': '冻结算力',
  'billing.settle': '结算算力',
  'billing.release': '退回冻结算力',
  'billing.force_release': '人工退回冻结算力',
  'billing.refund': '退款',
  'billing.quarantine_unknown': '结果待核对，费用暂挂',
};
export const auditActionLabel = (raw: unknown) => pick(AUDIT_ACTION_LABELS, raw, '操作');

/** 来源:IdentityContext.actor_kind —— owner / member / system */
export const ACTOR_KIND_LABELS: Record<string, string> = {
  owner: '老板',
  member: '员工',
  system: '系统',
};
export const actorKindLabel = (raw: unknown) => pick(ACTOR_KIND_LABELS, raw, '操作者');

/** 来源:services/organization_*.py 全部 `_audit(entity_type=...)` 站点 */
export const ENTITY_TYPE_LABELS: Record<string, string> = {
  brand: '客户',
  organization: '团队',
  organization_role: '角色',
  organization_invite: '邀请',
  organization_membership: '员工',
  organization_spend_limit: '使用上限',
  organization_approval_request: '审批申请',
  organization_approval_policy: '审批规则',
  organization_automatic_plan: '自动计划',
  organization_artifact_share: '内容共享',
  organization_charge_link: '算力扣费记录',
  organization_payer_policy: '费用代付设置',
};
export const entityTypeLabel = (raw: unknown) => pick(ENTITY_TYPE_LABELS, raw, '对象');

/** 邀请方式。来源:organization_invites.target_kind CHECK ('phone','email','username') */
export const INVITE_TARGET_KIND_LABELS: Record<string, string> = {
  phone: '手机号邀请',
  email: '邮箱邀请',
  username: '用户名邀请',
};
export const inviteTargetKindLabel = (raw: unknown) => pick(INVITE_TARGET_KIND_LABELS, raw, '邀请方式');

/** 结果可见范围。来源:invite access policy artifact_scope('own' / 'assigned_team') */
export const ARTIFACT_SCOPE_LABELS: Record<string, string> = {
  own: '只看他自己做的',
  assigned_team: '能看同一客户下其他同事做的',
};
export const artifactScopeLabel = (raw: unknown) => pick(ARTIFACT_SCOPE_LABELS, raw, '范围');

/* ── 后端错误码 → 人话(前端优先按 code 映射,message 只作兜底) ──────────────
 * [审计 B7/B8] 原来 `detail.message` 原样进 toast:FastAPI 默认的 "Not Found"、
 * "Internal Server Error" 和任何未汉化的后端 message 都会直接上屏。 */
export const ORGANIZATION_ERROR_CODE_LABELS: Record<string, string> = {
  HTTP_401: '登录已过期，请重新登录',
  HTTP_403: '你的账号没有这项权限，请找团队老板开通',
  HTTP_404: '没有找到对应的内容，请刷新页面重试',
  HTTP_405: '操作方式不对，请刷新页面重试',
  HTTP_408: '网络超时，请稍后重试',
  HTTP_413: '提交内容过大，请精简后重试',
  HTTP_429: '操作太频繁了，请稍后再试',
  HTTP_500: '服务暂时不可用，请稍后重试；若持续请联系客服',
  HTTP_502: '服务暂时不可用，请稍后重试',
  HTTP_503: '服务暂时不可用，请稍后重试',
  HTTP_504: '网络超时，请稍后重试',
};

/** 只有明确是中文人话的后端 message 才透传;否则按 code 兜底。
 *
 * 判据打**结构**不打词表(补一个词漏三个):
 *   ① 必须含中文 —— 挡住 FastAPI 默认的 "Not Found" / "Internal Server Error";
 *   ② 不许含**小写点分/下划线标识符** —— 挡住 `claim_token`、`monitor_single`、
 *      `billing.execute_high_cost`、`settle_charge_via_reconciliation` 这类变量/
 *      函数/枚举名混进句子;
 *   ③ 全大写的 `ORG_XXX` 是**故意**留给客服的错误码,不算英文露出,放行。
 */
export function isPlainChinese(text: unknown): boolean {
  const value = String(text ?? '').trim();
  if (!value) return false;
  if (!/[一-龥]/.test(value)) return false;
  if (/[a-z][a-z0-9]*[._][a-z0-9_.]+/.test(value)) return false;
  return true;
}
