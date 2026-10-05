/**
 * 启动按钮可用性的**纯逻辑**(门二返修 G2)。
 *
 * 单独一个无 React 依赖的模块 —— 与同目录 `launchSteps.ts` 同一理由:
 * 塞在 1900 行的 .tsx 里,判据要么去 bundle 整棵 React 依赖树,
 * 要么只能退回读源码串(读串证明不了分支真的会那样走)。
 *
 * 🔴 legacy 语义**逐字保持**:`offensive` 时结论完全由
 *    `isFormComplete`(品牌名 + 行业 + legacy 核心搜索问题合法)与
 *    `totalDiagnosisCost != null`(legacy 动态价目已读到)决定,
 *    与返修前的表达式等价。判据两向都钉死。
 */

export type LaunchMode = 'defensive' | 'offensive' | 'hybrid';

export interface LaunchGateInput {
    mode: LaunchMode;
    /** legacy 完整度:品牌名 + 行业 + 核心搜索问题合法。 */
    isFormComplete: boolean;
    /** legacy 动态价目;未读到时为 null。 */
    totalDiagnosisCost: number | null;
    /** 防御题单里**文本非空**的题数。 */
    defensiveQuestionCount: number;
    /** 是否已选中客户品牌(防御体检必须绑定具体品牌档案)。 */
    hasBrandId: boolean;
    loading: boolean;
    /** 题单预览在飞。🔴 [P0 2026-09-01] 它必须是**独立入参**而不是并进 `loading`:
     *  判据要能单独打这一格(正反两臂),并进去就分不出「谁把按钮拦住了」。
     *  现状是它只挡编辑器、不挡按钮 ⇒ 9 秒能连点 14 次,每点真发一次 preview。 */
    planPending?: boolean;
    /** [阶段② · 订正十补] 名字命中「自动打成测试客户」规则且她还没确认。 */
    testNameUnacknowledged?: boolean;
    /** [阶段⑤] 服务端价还没回来,或回来的那份对不上当前题数。 */
    priceNotReady?: boolean;
    /**
     * 🔴 [订正二十九] 搜索词超限(>20 条 或 单条 >50 字)。
     * 服务端 `validate_keywords` 会 422,而那条报错长得像系统故障、也不指是哪一条。
     * 「给可见提示」不配「拦住提交」等于把红字挂在那儿没人理:她照点,照样撞 422。
     */
    searchTermsInvalid?: boolean;
}

/** 是否走防御/混合流。`offensive` 是默认,也是所有旧链接的落点。 */
export function isDefensiveMode(mode: LaunchMode): boolean {
    return mode !== 'offensive';
}

/**
 * 启动按钮是否被拦住。
 *
 * 防御/混合:题单 ≥1 道。
 *   —— 不再要求 legacy 的「核心搜索问题」:她填的是题单编辑器,
 *      再要求把同样的问题在下面抄一遍,是把 CUR-06 的两套输入债务还给用户。
 *   —— 也不拿 legacy 的 `totalDiagnosisCost` 拦:防御流的价钱由 run-preview
 *      服务端下发,legacy 价目读没读到与它无关。
 *   —— 🔴 [P0 2026-09-01] **不再拿 `hasBrandId` 拦**。
 *      原来防御/混合下没选已有品牌就 disabled,而品牌下拉自己写着
 *      「无匹配品牌 · 继续输入将创建新品牌」—— UI 已经**许诺**会建档,
 *      按钮却是死的,且屏幕上零个字解释为什么(实测:被拦那一屏比能点那一屏
 *      只多出那句承诺 + 3 道自动生成的题)。legacy 一直是「新品牌直接填,
 *      系统自动建档」,防御线不该更差。
 *      现在:放行 ⇒ 点击时由 `runDefensivePlanPreview` 先走**现役**建档原语
 *      (`POST /api/my-clients` → `get_or_create_brand`,与 legacy 同源)拿到
 *      brandId 再 preview。`hasBrandId` 仍留在入参里 —— 它是调用方的真实状态,
 *      只是不再是**拦**的理由。
 *
 * offensive:与返修前**逐字等价**。
 */
export function isLaunchBlocked(input: LaunchGateInput): boolean {
    if (input.loading || input.planPending) return true;
    /**
     * [包一阶段② · 订正十补] 名字会被后端自动打成**测试客户**、而她还没确认 ⇒ 先别建。
     *
     * 为什么挡在这么前面:这件事与模式无关,防守链和 legacy 链都要挡;
     * 而且它一旦发生就**不可逆地**影响这个客户的归类(不进真客户统计/KPI),
     * 事后没有任何提示告诉她。挡一下 + 一句人话,比事后解释便宜得多。
     */
    if (input.testNameUnacknowledged) return true;
    /**
     * [阶段⑤ · 订正七④] 价没就绪 ⇒ 不许确认。
     * 这条继承今日 P0 班 `launch-confirm-blocked-stale` 的**语义**(新形态换皮,语义不丢):
     * 题单变了而价没重算时按钮必须是灰的,否则又回到「看到旧价、按新题单扣钱」。
     */
    if (input.priceNotReady) return true;
    if (input.searchTermsInvalid) return true;
    if (isDefensiveMode(input.mode)) {
        return !(input.defensiveQuestionCount >= 1);
    }
    /**
     * 🔴 [#143④ **已撤回** 2026-09-07] 这里曾经加过一条「实跑题数 < 1 就拦」。
     *
     * **它是个回归,没上线就撤了。** 我的推理是:增长模式 `suggestedDefensive` 恒为 `[]`
     * ⇒ 0 道自定义题就是「0 道题的体检」。前半句是真的,**后半句我从没验过** ——
     * 我拿**前端**的一个空数组推断了**后端**的行为,从没打开过工作流。
     *
     * 真相在 `workflows/diagnosis_workflow.py:881`:
     *   「custom 为空: 总是走 system 8 题(toggle 无意义)」
     * 题由 `analyze_client_business` 蒸馏(`real_questions`,`[:8]`),取不到还有硬编码兜底。
     * ⇒ **「0 道题的体检」不存在**;0 道自定义题是**最常见的默认流程**。
     * 那条闸会拦住每一个不填题的用户。
     *
     * 判据改成**正向**锁:0 自定义题**必须可以启动**(见 verify-p0 的 C2)。
     */
    return !input.isFormComplete || input.totalDiagnosisCost == null;
}
