/**
 * 4 步上手任务清单定义
 *
 * Stage 1 Task 3 (2026-05-06) · 抽共享常量
 * Stage 1 Batch 1 (2026-05-06) · 加 canSkip / skipDangerous 强引导
 * Stage 1 Batch 4 (2026-05-18) · 7 步 → 4 步重构
 *   - 删 enroll_client (代理日常先诊断后建客户, 新品牌可在诊断页直接填)
 *   - 删 fill_client_profile (补档案合并到 AI 写文章 / 报价里, 数据同步)
 *   - 合并 first_article + first_publish → first_article_publish (一个 step 教"写+发"流水线)
 *
 * 字段说明:
 *   step_id        - 与 OnboardingContext.completed_steps 里存的字符串一一对应
 *   index          - 1..4 用于 UI 序号显示
 *   label          - 任务名 (Modal + Checklist 显示)
 *   description    - 任务说明 (Checklist 行的副文本)
 *   icon           - lucide-react 图标组件 (禁用 emoji)
 *   goto           - 点击行跳转的前端路由
 *   videoCode      - 关联视频教程 code · 对应剪辑同事的清单 V1 / V2a / V3 / V4
 *   canSkip        - 用户能不能跳过这步; false = 必做, 强引导只显示"必做(无法跳过)"
 *   skipDangerous  - canSkip=true 时才有意义; true = 跳过会影响后续教程, 弹危险确认
 */
import {
    Stethoscope, Calculator, BookOpen, Activity,
    type LucideIcon,
} from 'lucide-react';

export interface OnboardingStep {
    step_id: string;
    index: number;
    label: string;
    description: string;
    icon: LucideIcon;
    goto: string;
    videoCode?: string;
    canSkip: boolean;
    skipDangerous?: boolean;
}

export const ONBOARDING_STEPS: OnboardingStep[] = [
    {
        step_id: 'first_diagnosis',
        index: 1,
        label: '30 秒发起品牌诊断',
        description: '填品牌名 + 关键词, AI 出 GEO 诊断报告',
        icon: Stethoscope,
        goto: '/diagnosis/new',
        videoCode: 'V1',
        canSkip: false,
    },
    {
        step_id: 'first_quote',
        index: 2,
        label: '出报价 + 签约收款',
        description: '在报价页选档位生成报价单 → 客户选档位 → 签约 → 收款 → 关键词进入写作大厅',
        icon: Calculator,
        goto: '/pricing',
        videoCode: 'V2a',
        canSkip: false,
    },
    {
        step_id: 'first_article_publish',
        index: 3,
        label: 'AI 写文章 + 发布',
        description: 'AI 生成 GEO 优化文章 → 加发布购物车 → 选平台投放',
        icon: BookOpen,
        goto: '/writing',
        videoCode: 'V3',
        canSkip: false,
    },
    {
        step_id: 'first_monitoring',
        index: 4,
        label: '看效果:盯出现率 + 补救',
        description: '看哪些词被 AI 推荐了、哪些没达标要补发',
        icon: Activity,
        goto: '/monitoring',
        videoCode: 'V4',
        canSkip: false,
    },
];

export const TOTAL_STEPS = ONBOARDING_STEPS.length;
