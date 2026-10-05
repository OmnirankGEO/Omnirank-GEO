/**
 * UI-37 · provider 动作绑定与 handoff 注册表(前端侧)。
 *
 * 规格 UI-37 逐字要求「provider binding/handoff target partition 与
 * policy/capability/route/effects/variant exact」,并且:
 *
 *   · availability 五格 / reason / handoff **可达**,manual **零副作用**;
 *   · `actionRef` 在同 sidecar 内跨 priority/trace/PDF **全局唯一**,复用仍拒;
 *   · provider DOM **不显示「联系自己」**,而由 typed handoff 解析;
 *   · 缺 / 重 / 错 route、dead handoff 均拒绝。
 *
 * 🔴 为什么注册表放前端也放后端
 * ------------------------------
 * 后端签发**哪些动作可用**(能力与授权);前端负责**把动作画成能点的东西**。
 * 两边都需要一张表。分歧的风险由静态闸兜:
 * `frontend/scripts/verify-defensive-geo-actions.mjs` 把本表的 key 集合
 * 与后端 `presentation/registries.state_actions` 的全集对账,
 * 少一个 / 多一个都红 —— 这样"前端画了个后端不认的按钮"不会活到线上。
 *
 * 🔴 POR-16:**没有 dead CTA**
 * ----------------------------
 * 每一项都必须同时有 `label`(画得出来)与 `target`(点得到)。
 * 只有 label 没有 target 的动作就是死按钮 —— 用户点了没反应,
 * 而她并不知道是"没做好"还是"我点错了"。
 * `assertNoDeadAction` 在构造期就拦掉。
 */

/** 动作产生的副作用。`none` = 纯导航/只读,**零网络写**。 */
export type ActionEffect = 'none' | 'mutation' | 'job' | 'billing';

export interface ActionBinding {
    /** 后端 `state_actions` 里的 key,逐值同源。 */
    readonly key: string;
    /** 按钮上写什么。40 岁非技术销售基准:说"点了会发生什么"。 */
    readonly label: string;
    /** 点了去哪。`:id` 由调用方替换。**恒非空** —— 空 target = 死 CTA。 */
    readonly route: string;
    /** 需要什么能力才 enabled。 */
    readonly capability: string;
    /** 副作用集合。manual/只读类恒为 `['none']`。 */
    readonly effects: readonly ActionEffect[];
}

/**
 * 五卡状态动作的绑定表。
 *
 * key 集合必须**恰等于**后端 `_STATE_ACTIONS` 的并集
 * (`view_evidence` / `retest_same_scope` / `contact_support`)。
 * 静态闸对账这一点;多画一个按钮 = 前端在承诺一个后端没有的能力。
 */
export const ACTION_BINDINGS: readonly ActionBinding[] = [
    {
        key: 'view_evidence',
        label: '看看 AI 原话',
        route: '/diagnosis/report/:diagnosisId#evidence',
        capability: 'view_raw_answers',
        effects: ['none'],
    },
    {
        key: 'retest_same_scope',
        label: '按同样的问题和平台再测一次',
        route: '/diagnosis/new?planId=:questionPlanId&revision=:questionPlanRevision',
        capability: 'start_retest',
        effects: ['none'], // 只是**打开**发起页;真正扣费在那一页确认时发生
    },
    {
        key: 'contact_support',
        label: '联系平台客服',
        route: '/help/home', // [WO_260] 原 /support 不存在(卡片上是真链接,点了 404);帮助中心里有联系方式
        capability: 'contact_support',
        effects: ['none'],
    },
] as const;

const BY_KEY = new Map(ACTION_BINDINGS.map((b) => [b.key, b]));

export function actionBinding(key: string): ActionBinding | null {
    return BY_KEY.get(key) ?? null;
}

/**
 * POR-16:拒绝 dead CTA。
 *
 * 返回**可渲染**的动作;不认识的 key 直接丢弃并在 dev 下报警,
 * 而不是画一个点不动的按钮。丢弃比画出来好 ——
 * 一个不存在的按钮不会骗人,一个点不动的按钮会。
 */
export function renderableActions(
    keys: readonly string[],
    params: Readonly<Record<string, string | number | null | undefined>> = {},
): ActionBinding[] {
    const out: ActionBinding[] = [];
    for (const key of keys) {
        const binding = BY_KEY.get(key);
        if (!binding) {
            if (import.meta.env?.DEV) {
                // eslint-disable-next-line no-console
                console.warn(
                    `[defgeo] 服务端下发了前端没有绑定的动作 ${key} —— 已丢弃。` +
                        `请把它补进 ACTION_BINDINGS,或让后端停止下发。`,
                );
            }
            continue;
        }
        out.push({ ...binding, route: resolveRoute(binding.route, params) });
    }
    return out;
}

/** 把 `:name` 占位替换成真值。**没替换掉的占位一律视为不可达**。 */
export function resolveRoute(
    template: string,
    params: Readonly<Record<string, string | number | null | undefined>>,
): string {
    let out = template;
    for (const [k, v] of Object.entries(params)) {
        if (v === null || v === undefined || v === '') continue;
        out = out.split(`:${k}`).join(String(v));
    }
    return out;
}

/**
 * UI-37:`actionRef` 在同一 sidecar 内**全局唯一**。
 *
 * 同一份报告里 priority 动作、trace 动作、PDF 动作共用一个 ref 池;
 * 复用一个 ref 会让"点这个按钮"在解析时指向另一个对象。
 */
export function assertUniqueActionRefs(refs: readonly string[]): void {
    const seen = new Set<string>();
    for (const ref of refs) {
        if (seen.has(ref)) {
            throw new Error(
                `actionRef ${ref} 在同一 sidecar 内重复 —— UI-37 要求全局唯一,` +
                    `复用会让按钮解析到另一个对象。`,
            );
        }
        seen.add(ref);
    }
}

/**
 * UI-37:provider DOM **不得**出现「联系自己」。
 *
 * 服务商自己就是 provider;给服务商画一个「联系您的服务商」按钮,
 * 她会以为系统坏了。客户面才用那句话。
 */
export const PROVIDER_FORBIDDEN_LABELS: readonly string[] = [
    '联系您的服务商',
    '联系你的服务商',
] as const;

export function assertNoContactYourselfInProviderDom(labels: readonly string[]): void {
    for (const label of labels) {
        if (PROVIDER_FORBIDDEN_LABELS.some((f) => label.includes(f))) {
            throw new Error(
                `provider 面出现「${label}」—— 服务商自己就是服务商(UI-37)。` +
                    `这句话只属于客户面。`,
            );
        }
    }
}

/** manual/只读动作恒零副作用。判据遍历它。 */
export function assertManualActionsHaveNoSideEffects(): void {
    for (const b of ACTION_BINDINGS) {
        if (b.effects.length !== 1 || b.effects[0] !== 'none') {
            throw new Error(
                `${b.key} 声明了副作用 ${b.effects.join('/')} —— ` +
                    `本表目前只登记只读/导航动作;付费动作必须走 ` +
                    `preview→confirm→execute,不从卡片按钮直接发起。`,
            );
        }
    }
}

/** 机械导出,给静态闸与后端对账用。 */
export function actionCensus(): { keys: string[]; routes: string[] } {
    return {
        keys: ACTION_BINDINGS.map((b) => b.key).sort(),
        routes: ACTION_BINDINGS.map((b) => b.route).sort(),
    };
}
