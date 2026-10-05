/**
 * 状态色 → 现役语义主题 token(B8 · 合同 §13.1 / §13.5)
 *
 * 🔴 设计包原型是暗色的,那只是信息结构示意,**不是生产视觉规范**。
 *    生产默认亮色 + `.dark` 双态(frontend/src/index.css `:root` / `.dark`),
 *    所以这里一律用 Tailwind 语义类,让 token 自己切换,
 *    **不硬编码任何原型色值**(锁 test-gap-plan-theme.mjs 会扫 #xxxxxx 命中即红)。
 *
 * 🔴 合同 §11:状态不能只靠颜色 —— 每个 tone 同时给 dot / text / bg 三层,
 *    而组件里状态徽章始终带文字标签,颜色只是叠加信息。
 */
import type { GapTone } from '@/lib/gapPlanApi';

export interface ToneClasses {
  /** 徽章整体 */
  badge: string;
  /** 状态圆点 */
  dot: string;
  /** 正文强调色 */
  text: string;
}

/**
 * 琥珀 = 等操作,**不是**危险红(合同 §11 明令)。
 * 红色只留给"真正失败且有明确重试动作"的系统错误。
 */
const TONES: Record<GapTone, ToneClasses> = {
  green: {
    badge: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
    dot: 'bg-emerald-500',
    text: 'text-emerald-700 dark:text-emerald-300',
  },
  amber: {
    badge: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300',
    dot: 'bg-amber-500',
    text: 'text-amber-700 dark:text-amber-300',
  },
  blue: {
    badge: 'border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300',
    dot: 'bg-sky-500',
    text: 'text-sky-700 dark:text-sky-300',
  },
  neutral: {
    badge: 'border-border bg-muted/50 text-muted-foreground',
    dot: 'bg-muted-foreground/60',
    text: 'text-muted-foreground',
  },
  red: {
    badge: 'border-destructive/30 bg-destructive/10 text-destructive',
    dot: 'bg-destructive',
    text: 'text-destructive',
  },
};

export function toneClasses(tone: GapTone | undefined): ToneClasses {
  return TONES[tone ?? 'neutral'] ?? TONES.neutral;
}
