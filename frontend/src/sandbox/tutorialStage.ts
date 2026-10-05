/**
 * Sandbox Tutorial Stage · 跨页面 spotlight 接力的状态机
 * Stage 1 Batch 4 (2026-05-18)
 *
 * 问题:
 *   沙盒里教学链跨多个页面 (Modal → sidebar → diagnosis page 多步)
 *   需要一个全局可读可写 + 持久 (reload 不丢) + 跨组件订阅 的状态
 *
 * 设计:
 *   - localStorage 持久 · key='omnirank_sandbox_tutorial_stage'
 *   - 自定义事件 'sandbox-tutorial-stage:change' 触发订阅刷新
 *   - 暴露 getStage / setStage / useTutorialStage React Hook
 *
 * 阶段:
 *   modal       · SandboxIntroModal 显示中 (modal 关闭后切到 sidebar)
 *   sidebar     · 引导用户点 sidebar "品牌体检"
 *   page-intro  · 进入 /diagnosis/new · spotlight 在顶部 GEO 诊断 banner
 *   brand-name  · spotlight 在品牌名 input
 *   autofill    · spotlight 在 AI 填写按钮
 *   submit      · spotlight 在开始 GEO 诊断按钮
 *   done        · step 1 完成 (诊断已启动 · 跳进度页) · 不再显示任何引导
 *
 * 沙盒退出时调用 resetStage() 重置, 下次进沙盒能重新走一遍
 */
import { useEffect, useState } from 'react';

export type TutorialStage =
    | 'modal'
    // === step 1 (品牌体检) ===
    | 'sidebar'         // sidebar 引导用户点"品牌体检" (老名字, 保留兼容)
    | 'page-intro'
    | 'brand-name'
    | 'autofill'
    | 'submit'
    // === step 2 (报价方案) ===
    // 2026-05-25:产品决定砍掉"快速录单"+"报告页一键报价" 2 个通道 · 只留"在线报价"单路径
    // 删了所有 step2-quick-* 子阶段 (~14 个) · 现在 step2 走线性流程:sidebar → page → online-form
    | 'step2-sidebar'   // sidebar 引导用户点"报价方案"
    | 'step2-page'      // 进 /pricing 后由页面内部接管(只剩在线报价单一路径)
    | 'step2-online-form'    // 在线报价表单阶段
    // === step 3 (写文章 + 发布) ===
    | 'step3-sidebar'
    | 'step3-page'
    | 'step3-explain-modes'   // 讲解 严选/优选/智选 区别 + 引导点严选
    | 'step3-pick-real'       // (废弃 · 兼容老 localStorage)
    | 'step3-show-competitors'// 展示严选搜出来的 5 家真实竞品
    | 'step3-upload-kb'       // 引导点知识库 "展开" 按钮
    | 'step3-upload-kb-click' // 引导点 "点击或拖入客户资料文件" 上传区 (上传完直接跳 gen-titles)
    | 'step3-expand-titles'   // 标题刚生成 · 引导用户点关键词行展开标题
    | 'step3-show-titles'     // 标题已展开 · 讲解可以重新生成 / 修改
    | 'step3-start-writing'   // 标题就绪后 · 引导点 "开始写作"
    | 'step3-show-articles'   // 6 篇写完后 · 讲解可以预览/重写文章
    | 'step3-go-publish'      // 看完文章后 · 引导点 "发布"
    // === PublishCenter 子阶段 ===
    | 'step3-pub-pick-article'  // 引导选第 1 篇文章
    | 'step3-pub-pick-media'    // 引导选第 1 个媒介
    | 'step3-pub-add-cart'      // 引导点 "加入购物车"
    | 'step3-pub-batch-send'    // 6 篇齐了 · 引导点 "一键发布"
    /*
     * 🔴 [#191 · 2026-09-13] 已退役:'step3-pub-await-process' /
     *    'step3-pub-await-edit' / 'step3-pub-await-confirm'(待确认弹窗那条子链)。
     *    88134ebd7(2026-07-27)把沙盒发布改成「成功后直接开结果说明、不再等媒介审核」,
     *    产生方随之消失 —— 这三个阶段**七周不可达**,消费者却还留在
     *    AwaitingConfirmDialog 与 Monitoring 里(#190 的类门照出来的)。
     *
     *    🔴 故意**不留**「兼容老 localStorage」条目:消费者已全部删除,
     *    留在 VALID_STAGES 里只会让旧值通过校验、然后**全站没有任何引导** ——
     *    那正是 G1a 要抓的「走到那儿没有下文」。退役后旧值校验失败,
     *    getTutorialStage() 退回 `modal`,教程从头开始(与任何未知值同一条路)。
     *    影响面:仅沙盒教程,且只影响 2026-07-27 前就卡在这一段的浏览器。
     *
     *    🔴 上面那三个名字**故意带单引号**:枚举器取阶段名只认联合体成员形态
     *    (竖线 + 空格 + 单引号),所以注释里的它们不进分母。
     *    而一旦有人把那条正则放松回「文件里出现过的带引号的串」,
     *    这三个名字就会被算回 ALL_STAGES、却不在 VALID_STAGES 里,互校当场红。
     *    **别把这三个名字改成反引号** —— 那会把这道埋伏拆掉。
     */
    | 'step3-gen-titles'      // 引导点 批量生成标题
    // === step 4 (监测出现率 + 补救闭环) ===
    | 'step4-sidebar'         // sidebar 引导点"监测中心"
    | 'step4-page'            // (兼容老值) 进 /monitoring 后由页面接管
    | 'step4-intro'           // spotlight 顶部"快进 10 天"横幅 · 讲词自动进监测
    | 'step4-rate'            // spotlight 出现率列 · 讲核心指标=出现率不是排名
    | 'step4-good'            // spotlight 两个魔法词(达标绿) · 呼应视频见效
    | 'step4-bad'             // spotlight 坏 case 词(未达标红)
    | 'step4-fix'             // spotlight "选中未达标"按钮 · 点它选中坏 case
    | 'step4-send'            // spotlight 底部"追加文章"浮动栏 · 点它打开补发弹窗
    | 'step4-opt-confirm'     // 补发弹窗 · 锁死 · 强制点"确认并跳转"
    | 'step4-opt-write'       // 写作 optimize tab · spotlight "开始写作"(补发壳子文)
    | 'step4-opt-gopublish'   // 写作 optimize tab · 写完 · spotlight "去发布"
    | 'step4-opt-batch'       // 发布页 · 自动配好购物车 · spotlight "一键发布"
    | 'step4-recovered'       // 回监测页 · 坏 case 词已达标 · spotlight 这一行
    | 'step4-token-card'      // spotlight Token 管理卡片 · 引导给客户生成白标链接
    | 'step4-token-share'     // Token 弹窗打开 · 讲白标链接怎么发给客户看实时数据
    | 'step4-finish'          // 监测页弹收尾总结窗 · 教程全部走完
    | 'done';

const STORAGE_KEY = 'omnirank_sandbox_tutorial_stage';
const EVENT_NAME = 'sandbox-tutorial-stage:change';

const VALID_STAGES: TutorialStage[] = [
    'modal',
    'sidebar', 'page-intro', 'brand-name', 'autofill', 'submit',
    'step2-sidebar', 'step2-page', 'step2-online-form',
    'step3-sidebar', 'step3-page', 'step3-explain-modes', 'step3-pick-real', 'step3-show-competitors',
    'step3-upload-kb', 'step3-upload-kb-click', 'step3-gen-titles',
    'step3-expand-titles', 'step3-show-titles', 'step3-start-writing', 'step3-show-articles', 'step3-go-publish',
    'step3-pub-pick-article', 'step3-pub-pick-media', 'step3-pub-add-cart', 'step3-pub-batch-send',
    'step4-sidebar', 'step4-page',
    'step4-intro', 'step4-rate', 'step4-good', 'step4-bad', 'step4-fix', 'step4-send',
    'step4-opt-confirm', 'step4-opt-write', 'step4-opt-gopublish', 'step4-opt-batch', 'step4-recovered',
    'step4-token-card', 'step4-token-share', 'step4-finish',
    'done',
];

export function getTutorialStage(): TutorialStage {
    try {
        const v = localStorage.getItem(STORAGE_KEY);
        if (v && (VALID_STAGES as string[]).includes(v)) {
            return v as TutorialStage;
        }
    } catch { /* noop */ }
    return 'modal';
}

export function setTutorialStage(stage: TutorialStage): void {
    try {
        localStorage.setItem(STORAGE_KEY, stage);
    } catch { /* noop */ }
    if (typeof window !== 'undefined') {
        window.dispatchEvent(new Event(EVENT_NAME));
    }
}

export function resetTutorialStage(): void {
    try { localStorage.removeItem(STORAGE_KEY); }
    catch { /* noop */ }
    if (typeof window !== 'undefined') {
        window.dispatchEvent(new Event(EVENT_NAME));
    }
}

/** React Hook: 订阅阶段变化, 自动 re-render */
export function useTutorialStage(): TutorialStage {
    const [stage, setStage] = useState<TutorialStage>(getTutorialStage);

    useEffect(() => {
        const sync = () => setStage(getTutorialStage());
        window.addEventListener(EVENT_NAME, sync);
        window.addEventListener('storage', sync); // 跨 tab 同步
        return () => {
            window.removeEventListener(EVENT_NAME, sync);
            window.removeEventListener('storage', sync);
        };
    }, []);

    return stage;
}

export const TUTORIAL_STAGE_EVENT = EVENT_NAME;
