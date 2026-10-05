// 治理 §13 用户可见告警机器合同(前端契约)。
//
// 对齐后端 services/governance_contract.py::build_alert 的 7 字段合同:
//   { code, message, reason, impact, repair_hint, actions[], rule_version }
// 每个 action 至少含 { id, label }，可带 type ∈ {retry, nav, contact, dismiss}。
// 这是前端第一个消费"真 §13 形状"(含 impact + actions[].type)的契约;既有
// alertAction.ts 是每-告警 {action,target,permission,recovery} 形状,二者并存不冲突。
//
// 铁律(§1.6 / §13):红色错误码必须带下一步动作出口;没有 action 的告警一律 NO-GO。
// 供应商名/异常类只在后端进日志,不进这里任何字段(后端已保证)。

export const GOVERNANCE_ALERT_CONTRACT_VERSION = 'governance-alert-v1' as const;

export type GovernanceActionType = 'retry' | 'nav' | 'contact' | 'dismiss' | 'api';

/**
 * api 类动作在调用前需要向用户收集的字段(如「已发布 · 补录链接」要一个稿件链接)。
 * name 直接作为提交体的键 —— 后端合同怎么写，前端就怎么提交，不另立一套映射。
 */
export interface GovernanceAlertActionField {
  name: string;
  label?: string;
  /** 与 <input type> 同义；'url' 额外触发 http(s) 前缀校验。 */
  type?: 'text' | 'url' | 'textarea' | string;
  required?: boolean;
  placeholder?: string;
}

export interface GovernanceAlertAction {
  id: string;
  label: string;
  type?: GovernanceActionType | string;
  /**
   * 动作目标。**合同是权威** —— nav 直接当路由，api 直接当请求路径。
   * 前端绝不另写一份 URL 常量：后端换路径时合同会自己更新，硬编码会静默失效。
   */
  target?: string;
  /** api 动作的 HTTP 方法，缺省 POST。 */
  method?: string;
  /** api 动作的入参表单；有此项时先收集再提交。 */
  fields?: GovernanceAlertActionField[];
}

export interface GovernanceAlertContract {
  code: string;
  message: string;
  reason: string;
  impact: string;
  repair_hint?: string | null;
  /**
   * [写作质量总工单 2026-07-29 · D-4] `repair_hint` 里如果嵌了正文片段,主视线只放
   * 规范截断后的短版本(前后省略号),完整原文放这里 → 渲染成 tooltip。
   * 缺省时 tooltip 用 repair_hint 本身。
   */
  repair_hint_full?: string | null;
  actions: GovernanceAlertAction[];
  /**
   * [D-2] 规则 ID / 版本号对代理零意义,不占主视线:GovernanceAlert 把它收进
   * "开发者信息"折叠区。字段本身保留(排障要用),只是不再平铺。
   */
  rule_version?: string | null;
  // 各调用点附带的透传数值键(required/available/available_paid/conversation_id/
  // submitted_count 等)容许存在,不在契约核心里强约束。
  [extra: string]: unknown;
}

function isAction(value: unknown): value is GovernanceAlertAction {
  if (!value || typeof value !== 'object') return false;
  const a = value as Record<string, unknown>;
  return typeof a.id === 'string' && a.id.length > 0
    && typeof a.label === 'string' && a.label.length > 0;
}

/** 判定任意值是否为一个良构 §13 告警合同(7 键齐 + ≥1 合法动作)。 */
export function isGovernanceAlertContract(value: unknown): value is GovernanceAlertContract {
  if (!value || typeof value !== 'object') return false;
  const v = value as Record<string, unknown>;
  if (typeof v.code !== 'string' || v.code.length === 0) return false;
  if (typeof v.message !== 'string') return false;
  if (typeof v.reason !== 'string') return false;
  if (typeof v.impact !== 'string') return false;
  if (!Array.isArray(v.actions) || v.actions.length === 0) return false;
  return v.actions.every(isAction);
}

/**
 * 从一个 axios error(或任意响应体)里解析 §13 告警合同,解析不到返回 null。
 *
 * 兼容两种承载:
 *  - HTTPException:合同在 `error.response.data.detail`(FastAPI 把 detail 原样放这)
 *  - SSE / 直返:合同键平铺在 `error.response.data` 顶层(如监测/代发的 {status,error,...contract})
 * 不改动 axios 拦截器;拦截器只按 code/字符串 detail 分支,永不迭代 actions。
 */
export function parseGovernanceContract(error: unknown): GovernanceAlertContract | null {
  const resp = (error as { response?: { data?: unknown } })?.response;
  const data = resp?.data as Record<string, unknown> | undefined;
  if (!data) {
    // 也允许直接传一个响应体/合同对象
    return isGovernanceAlertContract(error) ? (error as GovernanceAlertContract) : null;
  }
  const detail = data.detail;
  if (isGovernanceAlertContract(detail)) return detail;
  if (isGovernanceAlertContract(data)) return data;
  return null;
}
