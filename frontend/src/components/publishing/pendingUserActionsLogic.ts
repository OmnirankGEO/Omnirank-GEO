/**
 * §13 合同渲染器的纯逻辑层 —— 刻意与 TSX 分家。
 *
 * 仓库没有前端单测框架(无 vitest/jest),把"合同怎么规整""表单怎么校验""HTTP 状态码
 * 怎么收场"这三块判断从组件里拆出来,scripts/test-publication-contract-logic.mjs
 * 就能**真的执行**它们并断言行为,而不是只对源码做静态字符串匹配。
 *
 * 本文件同样**没有任何 reject_code 分支** —— 只认合同形状与 action.type。
 */
import type {
  GovernanceAlertAction,
  GovernanceAlertActionField,
  GovernanceAlertContract,
} from '@/contracts/governanceAlert';

/** 兜底出口:合同缺失/动作全不可执行时,至少让用户能找到人。/feedback 是站内真实路由。 */
export const CONTACT_ACTION: GovernanceAlertAction = {
  id: 'contact_support',
  label: '联系客服',
  type: 'nav',
  target: '/feedback',
};

/** 只有这两种 type 是"点了真能走通"的;其余(含未来新增的怪 type)一律不算出口。 */
export function isExecutable(action: GovernanceAlertAction | null | undefined): boolean {
  if (!action) return false;
  return (action.type === 'api' || action.type === 'nav') && Boolean(action.target);
}

export interface PendingItemLike {
  reject_code?: string | null;
  reject_user_message?: string | null;
  reject_contract?: GovernanceAlertContract | null;
}

/**
 * 把一条待办规整成"一定能渲染出东西"的合同。
 *
 * 绝不允许渲染成空白卡片 —— 那是把死胡同从后端搬到前端。合同缺失时用后端已经写好的
 * 人话 reject_user_message 兜底;一个可执行动作都没有时补「联系客服」。
 */
export function normalizeContract(item: PendingItemLike): GovernanceAlertContract {
  const raw = item.reject_contract;
  const usable = raw && typeof raw === 'object' && typeof raw.message === 'string' && raw.message.length > 0;
  const base: GovernanceAlertContract = usable
    ? { ...(raw as GovernanceAlertContract) }
    : {
        code: item.reject_code || 'PUBLICATION_PENDING_USER_ACTION',
        message: item.reject_user_message || '这条发布任务需要你确认一下。',
        reason: '',
        impact: '',
        actions: [],
      };
  const actions = Array.isArray(base.actions) ? base.actions : [];
  const hasExit = actions.some(isExecutable);
  return { ...base, actions: hasExit ? actions : [...actions, CONTACT_ACTION] };
}

/** 单字段校验。url 类型额外要 http(s) 前缀(与后端 400 的判据保持一致)。 */
export function fieldIsMissing(field: GovernanceAlertActionField, value: string): string {
  const v = (value || '').trim();
  if (field.required && !v) return `请填写${field.label || field.name}`;
  if (v && field.type === 'url' && !/^https?:\/\//i.test(v)) return '链接要以 http:// 或 https:// 开头';
  if (v.length > 2000) return '内容太长了，请精简到 2000 字以内';
  return '';
}

/**
 * 提交体的键**直接来自合同 fields[].name**,不做映射表 ——
 * 后端改字段名时合同会自己更新,前端映射表则会静默错位。note 是全局统一的补充说明。
 */
export function buildSubmitBody(
  fields: GovernanceAlertActionField[],
  values: Record<string, string>,
): Record<string, string> {
  const body: Record<string, string> = { note: (values.note || '').trim() };
  fields.forEach((f) => { body[f.name] = (values[f.name] || '').trim(); });
  return body;
}

/**
 * 后端故意把"不是你的/已消失"做成 404 反枚举;这里按状态码给不同收场。
 * stale=true 表示这条已经不该留在列表里,调用方要刷新。
 */
export function messageForStatus(status: number, detail: string): { text: string; stale: boolean } {
  if (status === 409) return { text: detail || '这条已经处理过了，列表帮你刷新了一下。', stale: true };
  if (status === 404) return { text: detail || '这条发布任务已经不在待确认里了。', stale: true };
  if (status === 400) return { text: detail || '填写的内容没通过校验，请检查后重试。', stale: false };
  return { text: detail || '操作没成功，请稍后重试。', stale: false };
}
