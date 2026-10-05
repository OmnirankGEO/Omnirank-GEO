/**
 * 缺口作战计划 · 前端接口客户端(P4)
 *
 * 🔴 前端**不做任何状态翻译**。label / explanation / tone / 按钮文案全部由服务端
 *    (config/gap_operation_labels.json → services/gap_operation_labels.py)下发。
 *    在这里再放一份中文字典 = 第二套 SSOT,两边迟早不一致;而且界面上出现
 *    未翻译内部枚举的那个 bug,恰恰是"前端自己兜底翻译"造出来的。
 *
 * 🔴 也**不做容量判断**。能不能"去写这篇"由服务端 actions 数组决定,
 *    前端只渲染它给的东西。合同判据 #10 的守卫在服务端,这里只是显示层。
 */
import api from '@/lib/api';

export type GapTone = 'green' | 'amber' | 'blue' | 'neutral' | 'red';

export interface GapStatus {
  code: string;
  label: string;
  explanation: string;
  tone: GapTone;
  tone_label?: string;
  icon?: string;
}

export interface GapAction {
  action_id: string;
  label: string;
  icon?: string;
  confirmation: boolean;
  enabled: boolean;
  implementation_phase?: string;
  hint?: string;
}

export interface GapEvidenceStage {
  key: string;
  label: string;
  done: boolean;
}

export interface GapEvidenceBlock {
  stages: GapEvidenceStage[];
  publication_url: string;
  published_at: string | null;
  checkback_schedule: Array<{ due_day: number; due_at: string; status: string }>;
  /** R9:回查未启用时的「计划中」话术。空串 = 无需提示。不是错误。 */
  notice: string;
}

export interface GapPlanItem {
  plan_item_id: string;
  ordinal: number;
  title: string;
  target_question: string;
  content_form: string;
  target_domain: string;
  status: GapStatus;
  gap: GapStatus | null;
  access: GapStatus | null;
  duplicate_of_item_id: string | null;
  rationale: { why?: string; what?: string; how?: string };
  actions: GapAction[];
}

export interface GapEvidencePlatform {
  platform_label: string;
  candidate_count: number;
  answer_count: number;
  last_observed_at: string | null;
  citation_domains: string[];
}

/** 执行闸(阶段 1)。open=false 时报价页**只渲染 hint 那一句**,不渲染整段面板。
 *  🔴 open 由服务端从 `Capacity.executable` 那一个属性派生。前端不得自己按
 *     available_articles / display_status 再判一次 —— 前端门禁有正则钉死这件事。 */
export interface GapExecutionGate {
  open: boolean;
  code: string;
  label: string;
  hint: string;
}

export interface GapPlanSnapshot {
  audience?: 'agent';
  /** 🔴 **可选,不是必填**(返工 WO_GAPPLAN_REWORK_2026-08-11 修 3)。
   *
   *  上一轮这里写的是 `execution_gate: GapExecutionGate`(必填)。那是一句**谎**:
   *  蓝绿热回滚窗口内,浏览器缓存的新 JS 会拿到旧槽的响应,而旧槽根本不下发这个字段。
   *  类型撒谎的直接后果是 **tsc 对未守卫的 `execution_gate.hint` 一声不吭** ——
   *  于是"build exit 0"被我当成了不白屏的证据,其实它什么都没证明。
   *
   *  改成可选之后,tsc 从"帮着掩盖"变成第二道守卫:任何未收窄的解引用当场编译不过。
   *  第一道守卫是 `GapPlanExecutionView.resolveExecutionMode` 的三分支分流。 */
  execution_gate?: GapExecutionGate;
  snapshot_id: string;
  snapshot_version: string;
  rule_version: string;
  authority_generation: number;
  quote_id: number;
  query_family: { display_query: string; observed_at: string | null };
  summary: {
    headline?: string;
    next_step?: string;
    status_counts?: Record<string, number>;
    customer_present?: boolean;
    total_candidates?: number;
    capacity_notice?: string;
  };
  /** 容量块由 P1 的 services/article_capacity_contract 下发,前端**不做任何算术**。
   *  🔴 display_status 只有三态(capacity_available / capacity_zero / capacity_reserved)——
   *     未付款由合同折算成 capacity_zero,前端不再单独认「报价尚未收款」第四态。
   *  🔴 篇数是**上限**不是完成率(semantics=upper_bound_0_to_capacity):
   *     不得做成进度条百分比,更不得为了显示 100% 去催生成文章。 */
  capacity: {
    contract_version: string;
    authorized_articles: number;
    reserved_articles: number;
    consumed_articles: number;
    available_articles: number;
    over_delivered_articles: number;
    display_status: 'capacity_available' | 'capacity_zero' | 'capacity_reserved';
    semantics: string;
    capacity_source: string;
    label: string;
    explanation: string;
    tone: GapTone;
    icon?: string;
  };
  /** 不足额与原因。用不满不是失败(counts_as_failure 恒 false),
   *  但说不出为什么会带上「原因待确认」——那条必须显示,不许藏。 */
  shortfall: {
    shortfall_articles: number;
    counts_as_failure: boolean;
    reasons: GapStatus[];
  };
  items: GapPlanItem[];
  evidence_platforms: GapEvidencePlatform[];
  evidence: Record<string, GapEvidenceBlock>;
  labels_version: string;
  generated_at: string | null;
}

/**
 * 客户售前版(WO_GAPPLAN_RELOCATION_A 阶段 2)。
 *
 * 🔴 它是 GapPlanSnapshot 的**真子集**,不是同一个类型加可选字段:
 *    服务端 `present_snapshot(audience="customer")` 根本不下发 capacity /
 *    shortfall / snapshot_id / authority_generation / execution_gate,
 *    item 里也没有 access / target_domain,actions 恒为空数组。
 *    类型写成子集,是为了让"客户组件里想读 actions"在 **tsc 阶段**就编译不过 ——
 *    运行时才发现的边界不算边界。
 *
 * 🔴 没有对应的 fetch 函数:这份数据随 `GET /api/s/{token}` 一起下发
 *    (见 api/selection_api.py::_build_customer_delivery_plan)。
 *    客户是 token-only 会话,`/api/quotes/` 在 auth 中间件层就 401,
 *    所以这里**故意不提供**任何指向 /api/quotes 的客户侧调用。
 */
export interface GapPlanCustomerItem {
  plan_item_id: string;
  ordinal: number;
  title: string;
  target_question: string;
  content_form: string;
  status: GapStatus;
  gap: GapStatus | null;
  duplicate_of_item_id: string | null;
  rationale: { why?: string; what?: string; how?: string };
}

export interface GapPlanCustomerPreview {
  audience: 'customer';
  quote_id: number;
  query_family: { display_query: string; observed_at: string | null };
  summary: {
    headline?: string;
    next_step?: string;
    customer_present?: boolean;
    total_candidates?: number;
  };
  items: GapPlanCustomerItem[];
  evidence_platforms: GapEvidencePlatform[];
  labels_version: string;
  generated_at: string | null;
}

/** 服务端错误合同(04 §7):永远带人话与可执行出口,不只返错误码。 */
export interface GapPlanError {
  code: string;
  label: string;
  message: string;
  tone: GapTone;
  keeps_last_snapshot: boolean;
  primary_action: GapAction;
  secondary_action: GapAction | null;
}

export function parseGapPlanError(err: unknown): GapPlanError | null {
  const detail = (err as { response?: { data?: { detail?: { error?: GapPlanError } } } })
    ?.response?.data?.detail;
  return detail?.error ?? null;
}

const base = (quoteId: number) => `/api/quotes/${quoteId}/delivery-plan`;

export async function fetchDeliveryPlan(quoteId: number): Promise<GapPlanSnapshot> {
  const { data } = await api.get(base(quoteId));
  return data.snapshot as GapPlanSnapshot;
}

export async function fetchPlanItem(
  quoteId: number,
  planItemId: string,
  expectedGeneration?: number,
) {
  const qs = expectedGeneration != null
    ? `?expected_authority_generation=${expectedGeneration}`
    : '';
  const { data } = await api.get(`${base(quoteId)}/items/${encodeURIComponent(planItemId)}${qs}`);
  return data;
}

export async function submitPublicationLink(
  quoteId: number,
  planItemId: string,
  payload: {
    publication_url: string;
    idempotency_key: string;
    expected_authority_generation?: number;
  },
): Promise<{ evidence: Record<string, GapEvidenceBlock>; replayed: boolean }> {
  const { data } = await api.post(
    `${base(quoteId)}/items/${encodeURIComponent(planItemId)}/publication-link`,
    payload,
  );
  return data;
}

export async function markDomainAccess(
  quoteId: number,
  planItemId: string,
  payload: { accessible: boolean; reason?: string; idempotency_key?: string },
): Promise<GapPlanSnapshot> {
  const { data } = await api.post(
    `${base(quoteId)}/items/${encodeURIComponent(planItemId)}/domain-access`,
    payload,
  );
  return data.snapshot as GapPlanSnapshot;
}

/** 深链:去写这篇。只预填定位,不触发生成/扣费/发布。 */
export function writingDeepLink(quoteId: number, planItemId: string, generation: number): string {
  return `/writing?quote_id=${quoteId}&plan_item_id=${encodeURIComponent(planItemId)}`
    + `&plan_generation=${generation}`;
}
