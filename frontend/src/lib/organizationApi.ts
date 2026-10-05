import { authFetch } from '@/lib/api';
import { ORGANIZATION_ERROR_CODE_LABELS, isPlainChinese } from '@/lib/organizationLabels';

export interface OrganizationErrorDetail {
  code: string;
  message: string;
  request_id?: string;
  retryable?: boolean;
  details?: Record<string, unknown>;
}

export class OrganizationApiError extends Error {
  readonly status: number;
  readonly detail: OrganizationErrorDetail;

  constructor(status: number, detail: OrganizationErrorDetail) {
    super(detail.message || '服务暂时不可用，请稍后重试');
    this.name = 'OrganizationApiError';
    this.status = status;
    this.detail = detail;
  }
}

/**
 * FastAPI 字段校验错误(422)的 `detail` 是**数组**,不是 `{code,message}`。
 *
 * [P0 2026-08-17] 后端已加了团队路径专属的 422 handler(server.py +
 * services/organization_validation_copy.py),会直接给出 `{code,message}` 人话。
 * 但灰度期间可能撞上旧后端,而**旧行为正是「组织服务暂不可用」的最大来源**
 * (员工少输一位验证码 → 屏幕上写系统故障)。所以前端也留一道解析:
 * 认出 422 数组 → 按字段名给人话。两头都接,任一头生效即可。
 */
const VALIDATION_FIELD_COPY: Record<string, string> = {
  code: '验证码是 6 位数字，请重新输入',
  password: '密码至少 6 位，请重新设置',
  display_name: '请填写你的姓名（不超过 80 字）',
  terms_accepted: '请先勾选同意用户协议',
  privacy_accepted: '请先勾选同意隐私政策',
  token: '邀请链接不完整，请使用团队负责人发给你的完整链接打开',
  short_code: '团队代码须为 4-12 位小写字母或数字',
  member_name: '员工名须为 1-32 位小写字母、数字、下划线、点或连字符',
  name: '团队名称不能为空，且不能超过 120 字',
  reason: '请填写原因',
  high_risk_reason: '请说明为什么要给他这些额外权限',
  target: '请填写员工的手机号、邮箱或登录用户名',
  request_id: '请求编号无效，请刷新页面后重试',
  expected_version: '页面数据已过期，请刷新后重试',
  limit_points: '算力上限请填 0 或正整数',
  threshold_points: '需要审批的算力阈值请填正整数',
};

function validationDetailFromArray(
  entries: unknown[],
  status: number,
): OrganizationErrorDetail | null {
  const fields: Array<{ field: string; type: string }> = [];
  let chosen: string | null = null;
  let missingOnly = entries.length > 0;
  for (const entry of entries) {
    if (!entry || typeof entry !== 'object') continue;
    const record = entry as Record<string, unknown>;
    const loc = Array.isArray(record.loc) ? record.loc : [];
    const type = typeof record.type === 'string' ? record.type : '';
    const field = [...loc].reverse().find(
      (item): item is string => typeof item === 'string'
        && !['body', 'query', 'path', 'header', 'cookie'].includes(item),
    ) || 'body';
    fields.push({ field, type });
    if (type !== 'missing') missingOnly = false;
    const copy = VALIDATION_FIELD_COPY[field];
    if (copy && chosen === null) chosen = copy;
  }
  if (!fields.length) return null;
  return {
    code: 'ORG_REQUEST_FIELD_INVALID',
    message: chosen || (missingOnly ? '有必填项没有填，请补齐后重试' : '表单有一项没填对，请检查后重试'),
    retryable: false,
    details: { fields, http_status: status },
  };
}

export interface OrganizationOverview {
  /** [P0 2026-08-17] 后端在「没有团队」时返回 200 + `has_organization: false`
   *  (不再是 404,那条 404 是每个无团队账号每次加载都必现的 console 噪音)。
   *  有团队时为 true。旧后端不带这个字段 → 按 `id` 是否存在判定。 */
  has_organization?: boolean;
  id: number;
  owner_user_id: number;
  name: string;
  status: 'active' | 'suspended' | 'dissolved';
  version: number;
  authority_version: number;
  viewer_is_owner: boolean;
  entitlement: {
    id: number;
    entitled_seats: number;
    occupied_seats: number;
    available_seats: number;
    product_catalog_version: string;
    source_sku: string;
  };
  identity: {
    actor_kind: 'owner' | 'member' | 'system';
    membership_id: number | null;
    authority_version: string;
    capabilities: string[];
  };
  assigned_brand_ids: number[];
  /** [C2 2026-08-17] 同一批客户的名字。员工端不再拿内部客户编号当界面文案。 */
  assigned_brands?: Array<{ id: number; name: string }>;
}

export interface OrganizationRole {
  id: number;
  organization_id: number;
  code: string;
  name: string;
  is_owner_role: boolean;
  version: number;
  capabilities: string[];
}

export interface OrganizationMember {
  /** [P0-B ④] 完整登录名(如 sjkj-zhangwei)。列表要能看见并一键复制 ——
   *  团队长发邀请时看到过一次,事后找不到就只能靠记,记错一位员工就登不进来。 */
  login_name?: string | null;
  id: number;
  user_id: number;
  status: string;
  is_owner: boolean;
  version: number;
  capability_version: number;
  assignment_version: number;
  display_name: string;
  avatar_url?: string;
  role_id: number;
  role_name: string;
  role_code: string;
  assigned_brand_ids: number[];
  effective_capabilities: string[];
  capability_overrides: Record<string, 'allow' | 'deny'>;
}

export interface OrganizationInviteAccessPolicy {
  version: 'invite-access-policy-v1';
  brand_ids: number[];
  capability_overrides: Record<string, 'allow' | 'deny'>;
  artifact_scope: 'own' | 'assigned_team';
  daily_limit_points?: number | null;
  monthly_limit_points?: number | null;
  feature_limits: Record<string, { daily?: number; monthly?: number }>;
  high_risk_expansions: string[];
  high_risk_reason?: string | null;
}

export interface OrganizationInvite {
  id: number;
  organization_id: number;
  target_kind: 'phone' | 'email' | 'username';
  /** [P0 2026-08-17] 用户名式邀请的**最终登录名**(如 `jcfemw-qaseat01`)。
   *  成员表原来显示「用户名邀请 #16」——`#16` 是内部邀请 ID,对老板毫无意义。
   *  手机号/邮箱邀请为 null(联系方式仍然只以密文存库)。 */
  login_name?: string | null;
  role_id: number;
  status: 'pending' | 'accepted' | 'revoked' | 'expired';
  expires_at: string;
  resend_count: number;
  version: number;
  created_at: string;
  access_policy_hash?: string;
  access_policy: OrganizationInviteAccessPolicy;
}

export interface OrganizationLimit {
  id: number;
  membership_id: number;
  limit_kind: string;
  feature_code?: string | null;
  limit_points: number;
  reserved_points: number;
  consumed_points: number;
  refunded_points: number;
  period_start: string;
  period_end: string;
  status: string;
  policy_version: number;
}

export interface OrganizationApproval {
  id: number;
  requested_by_membership_id: number;
  requested_by_user_id: number;
  action_type: string;
  brand_id?: number | null;
  artifact_type?: string | null;
  artifact_id?: string | null;
  estimated_points: number;
  status: string;
  policy_version: number;
  approved_by_user_id?: number | null;
  expires_at: string;
  version: number;
  created_at: string;
}

export interface OrganizationApprovalPolicy {
  id: number;
  version: number;
  status: string;
  action_type: string;
  membership_id?: number | null;
  role_id?: number | null;
  feature_code?: string | null;
  public_scope?: string | null;
  min_points?: number | null;
  max_points?: number | null;
  threshold_points?: number | null;
  always_require_approval: boolean;
}

export interface AutomaticPlan {
  id: number;
  feature_code: string;
  work_kind: string;
  brand_id?: number | null;
  cadence_seconds: number;
  max_occurrences: number;
  scheduled_occurrences: number;
  settled_occurrences: number;
  total_budget_points: number;
  max_occurrence_points: number;
  reserved_budget_points: number;
  consumed_budget_points: number;
  refunded_budget_points: number;
  next_occurrence_at: string;
  ends_at: string;
  status: string;
  version: number;
}

async function responseDetail(response: Response): Promise<OrganizationErrorDetail> {
  let body: unknown;
  try { body = await response.json(); } catch { body = null; }
  const record = body && typeof body === 'object' ? body as Record<string, unknown> : {};

  // ① FastAPI 校验错误(detail 是数组)—— 按字段名翻成人话,别落到「服务不可用」。
  if (Array.isArray(record.detail)) {
    const parsed = validationDetailFromArray(record.detail, response.status);
    if (parsed) return parsed;
  }

  const nested = record.detail && typeof record.detail === 'object' && !Array.isArray(record.detail)
    ? record.detail as Record<string, unknown>
    : record;
  const code = typeof nested.code === 'string' ? nested.code : `HTTP_${response.status}`;

  // ② [审计 B7] message 不再无条件透传。优先级:
  //    后端中文人话 > 按 code 的中文兜底 > 按 HTTP 状态的中文兜底 > 通用兜底。
  //    非中文(FastAPI 默认 "Not Found")或夹带变量名的 message 一律不上屏。
  const rawMessage = typeof nested.message === 'string'
    ? nested.message
    : typeof record.detail === 'string' ? record.detail : '';
  const message = isPlainChinese(rawMessage)
    ? rawMessage
    : ORGANIZATION_ERROR_CODE_LABELS[code]
      ?? ORGANIZATION_ERROR_CODE_LABELS[`HTTP_${response.status}`]
      ?? '操作失败，请稍后重试';

  return {
    code,
    message,
    request_id: typeof nested.request_id === 'string' ? nested.request_id : undefined,
    retryable: nested.retryable === true,
    details: nested.details && typeof nested.details === 'object'
      ? nested.details as Record<string, unknown> : undefined,
  };
}

export async function organizationRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authFetch(`/api/organization${path}`, init);
  if (!response.ok) throw new OrganizationApiError(response.status, await responseDetail(response));
  return await response.json() as T;
}

export async function organizationAdminRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authFetch(`/api/admin/organization-product-config${path}`, init);
  if (!response.ok) throw new OrganizationApiError(response.status, await responseDetail(response));
  return await response.json() as T;
}

export async function organizationPublicRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  headers.set('Accept', 'application/json');
  const response = await fetch(`/api/public/organization${path}`, {
    ...init,
    method: init?.method || 'POST',
    headers,
    credentials: 'same-origin',
    cache: 'no-store',
    referrerPolicy: 'no-referrer',
  });
  if (!response.ok) throw new OrganizationApiError(response.status, await responseDetail(response));
  return await response.json() as T;
}

export function requestId(prefix: string): string {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}:${random}`;
}

export interface OrganizationPayerPolicy {
  organization_id: number;
  shared_payer_enabled: boolean;
  overage_enabled: boolean;
  per_action_limit_points: number | null;
  daily_limit_points: number | null;
  monthly_limit_points: number | null;
  policy_version: number;
  enabled_at?: string | null;
  disabled_at?: string | null;
  reason?: string | null;
  updated_at?: string | null;
}

export interface OrganizationPayerUsage {
  daily_overage_used_points: number;
  monthly_overage_used_points: number;
  daily_period_start: string;
  daily_period_end: string;
  monthly_period_start: string;
  monthly_period_end: string;
  daily_overage_remaining_points?: number | null;
  monthly_overage_remaining_points?: number | null;
}

export interface OrganizationPayerPolicyOwnerView {
  viewer: 'owner';
  global_shared_payer_flag: boolean;
  policy: OrganizationPayerPolicy;
  usage: OrganizationPayerUsage;
}

export interface OrganizationPayerPolicyMemberView {
  viewer: 'member';
  global_shared_payer_flag: boolean;
  shared_payer_enabled: boolean;
  overage_enabled: boolean;
  my_usage: OrganizationPayerUsage;
}

export type OrganizationPayerPolicyView = OrganizationPayerPolicyOwnerView | OrganizationPayerPolicyMemberView;

export interface PayerPolicyUpdateBody {
  shared_payer_enabled: boolean;
  overage_enabled: boolean;
  per_action_limit_points?: number | null;
  daily_limit_points?: number | null;
  monthly_limit_points?: number | null;
  reason: string;
  expected_version: number;
  request_id: string;
}

export interface PayerPolicyUpdateResult extends PayerPolicyUpdateBody {
  policy_version: number;
  organization_id: number;
  replayed: boolean;
}

/**
 * [P0 2026-08-17] 重新取回一条待接受邀请的链接。**只读,不重签、不作废旧链接。**
 * 老板丢了剪贴板(微信/换设备场景必然发生)时的唯一找回途径 —— 在此之前只能「重发」,
 * 而重发会把已经发给员工的那条链接直接作废。
 */
export function getInviteLink(inviteId: number): Promise<{ delivery_token: string | null }> {
  return organizationRequest<{ delivery_token: string | null }>(`/invites/${inviteId}/link`);
}

export function getPayerPolicy(): Promise<OrganizationPayerPolicyView> {
  return organizationRequest<OrganizationPayerPolicyView>('/payer-policy');
}

export function putPayerPolicy(body: PayerPolicyUpdateBody): Promise<PayerPolicyUpdateResult> {
  return organizationRequest<PayerPolicyUpdateResult>('/payer-policy', { method: 'PUT', body: JSON.stringify(body) });
}
