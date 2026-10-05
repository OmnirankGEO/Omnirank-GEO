/**
 * 诊断只读价预览(#84 §3 · 订正二十一)。
 *
 * 🔴 **只用 POST,不用 GET。** 两者在「双轨开」时给出不同的价:
 *    GET 恒 `ai_optimized=False`,POST 用真实开关 ⇒ 3 道题时 GET 显 650、POST 绑 950。
 *    用 GET 显价 + 用 POST 绑提交 = **她看到 650、被扣 950**(与 #62 同形)。
 *    只用 POST ⇒ 显价与绑提交来自**同一个响应体**,「按钮上的价为唯一价」成为结构性的,
 *    而不是靠纪律维持。(Review 订正二十一采纳;GET 交 C 下一笔删或改薄壳。)
 *
 * 🔴 **前端不算钱**:本模块只发请求、只读响应,没有任何算术。
 *    原先 `NewDiagnosis.tsx` 那面镜子(`customExtraCost`)是同一条规则的第二份 ——
 *    订正十二一改规则两处必漂一处,而漂开那天不会有任何判据变红。阶段⑤ 一并删。
 *
 * 🔴 `questions` 必须与提交体的 `custom_questions` **是同一份**:
 *    服务端守卫(`server.py:3605` 一带)拿 `request.custom_questions` 去比哈希,
 *    比不上就 409。这里传别的东西 ⇒ 每次提交都 409,而错误看起来像"服务端抽风"。
 */
import { authFetch } from '@/lib/api';

export type DiagnosisScope = 'geo' | 'social' | 'full';

export interface DiagnosisPricePreview {
    questionCount: number;
    mode: string | null;
    points: number;
    /** 与 `points` 同源同值,供余额预检 —— **不是第二个数**,别让它变成第二个数。 */
    estimate: number;
    /** 提交体必须原样带上它(缺 ⇒ 400,对不上 ⇒ 409)。 */
    pricePreviewId: string;
    breakdown: {
        base: number;
        extra: number;
        freeQuestions: number;
        perExtraQuestion: number;
    };
}

export interface PriceFailure {
    /** `unavailable` = 价目读不到(503);`network` = 没拿到响应。两者都**不显示任何数字**。 */
    kind: 'unavailable' | 'network';
    message: string;
}

/**
 * 取价。**失败一律不返回数字** —— 宁可按钮上不显示价,也不显示一个我们并不知道的价
 * (同 #64:取不到价就一个字都不显示)。
 */
export async function fetchDiagnosisPrice(input: {
    questions: string[];
    aiOptimizeCustom: boolean;
    scope: DiagnosisScope;
    mode?: string;
    signal?: AbortSignal;
}): Promise<{ ok: true; data: DiagnosisPricePreview } | { ok: false; error: PriceFailure }> {
    try {
        const res = await authFetch('/api/pricing/diagnosis-preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                questions: input.questions,
                aiOptimizeCustom: input.aiOptimizeCustom,
                scope: input.scope,
                mode: input.mode,
            }),
            signal: input.signal,
        });
        if (res.status === 503) {
            return { ok: false, error: { kind: 'unavailable', message: '价目暂时读不到,稍后再试;没有扣除任何算力。' } };
        }
        if (!res.ok) {
            return { ok: false, error: { kind: 'network', message: '这次没能算出价格,稍后再试;没有扣除任何算力。' } };
        }
        const body = await res.json();
        const d = body?.data ?? body;
        if (!d || typeof d.points !== 'number' || typeof d.pricePreviewId !== 'string' || !d.pricePreviewId) {
            // 响应缺字段 ⇒ 当作没拿到价。**不猜**,也不拿 estimate 顶替 points。
            return { ok: false, error: { kind: 'network', message: '这次没能算出价格,稍后再试;没有扣除任何算力。' } };
        }
        return { ok: true, data: d as DiagnosisPricePreview };
    } catch (e) {
        if ((e as { name?: string })?.name === 'AbortError') {
            return { ok: false, error: { kind: 'network', message: '' } };
        }
        return { ok: false, error: { kind: 'network', message: '这次没能算出价格,稍后再试;没有扣除任何算力。' } };
    }
}

/**
 * 这份价还对得上当前输入吗。
 *
 * 🔴 判据钉的就是这一条:题集或开关一变,**在新价回来之前**按钮不可确认
 *    (继承今日 P0 班 `launch-confirm-blocked-stale` 的语义 —— 新形态可以换皮,语义不能丢)。
 *    比的是**服务端回显的 questionCount** 与当前题数:回显是响应自己带的,
 *    用它比就不会出现"我以为发的是 3 道、其实回来的是上一次 5 道那份"。
 */
export function priceMatchesInput(
    p: DiagnosisPricePreview | null, currentQuestionCount: number,
): boolean {
    if (!p) return false;
    return p.questionCount === Math.max(0, currentQuestionCount | 0);
}
