/**
 * StagePrimaryButtonMap · 9 stage 主按钮映射
 *
 * CTO-15.21 v5 收口(2026-04-28 老板拍板):
 *   "M3 蒸馏出来的功能仅仅是在做引导 · 所有功能性按钮全部跳旧版"
 *
 * 红线:
 *   - 9 stage 主按钮 100% 跳旧版工作面:
 *     /diagnosis/new /diagnosis/progress/:id /diagnosis/report/:id
 *     /pricing(报价方案 + 自动填客户 brand_id + diagnosis_id + quote_id)
 *     /writing /publish /monitoring /reports /my-clients/:id
 *   - 0 个 /m3/* 跳转(M3 是测试版 · 不再普通代理可见)
 *   - 文案对代理友好(生成方案书 → 生成报价方案 等)
 */

import type { LucideIcon } from 'lucide-react';
import {
  Stethoscope,
  Loader,
  FileSpreadsheet,
  Send,
  Coins,
  PencilLine,
  Megaphone,
  LineChart,
  FileBarChart2,
  RefreshCw,
} from 'lucide-react';
import type { LifecycleStage, QuoteStatus, ServiceStatus } from './types';

// ============================================================
// 类型定义
// ============================================================

export interface PrimaryActionContext {
  brandId: number;
  brandName?: string;
  diagnosisId?: number;
  diagnosisSessionId?: string;
  diagnosisRunning?: boolean;
  quoteId?: number;
  quoteStatus?: QuoteStatus;
  serviceStatus?: ServiceStatus | null;
  monitoringSummary?: {
    total: number;
    qualified: number;
    unqualified: number;
    paving?: number;
  } | null;
  hasReportThisMonth?: boolean;
}

export interface SecondaryAction {
  label: string;
  target?: string;
  /** 用 onClick 时父组件接 ev 自定义动作(弹 Dialog / 复制等) */
  actionKey?: 'copy_payment_script' | 'open_activate_dialog' | 'open_materials_dialog' | 'invite_intake' | 'view_history';
}

export interface PrimaryAction {
  /** 用于 a11y / 数据埋点的 key(stage_step) */
  actionKey: string;
  icon: LucideIcon;
  title: string;
  subtitle: string;
  /** 单位:分(同 feature_pricing.cost_points)· 仅显示 · 不直接扣费 */
  costPoints?: number;
  /** 跳转目标路径 · null 时由父组件用 actionKey 自定义动作(弹 Dialog 等) */
  target: string | null;
  /** 跳旧版工作面 · 走桥接(C.4 旧版顶部加 M3 决策条) */
  bridgeToLegacy?: boolean;
  secondary: SecondaryAction[];
}

// ============================================================
// 主按钮映射 · 9 stage
// ============================================================

export function getPrimaryAction(
  stage: LifecycleStage,
  ctx: PrimaryActionContext,
): PrimaryAction {
  const bid = ctx.brandId;

  switch (stage) {
    // Stage 1 · 询价(无诊断 · 无 quote)
    case 1: {
      // 诊断在跑 → 看进度(跳旧版 /diagnosis/progress/:id)
      if (ctx.diagnosisRunning && ctx.diagnosisSessionId) {
        return {
          actionKey: 'stage1_diagnosis_running',
          icon: Loader,
          title: '看诊断进度',
          subtitle: '4 引擎并发跑分中 · 约 2-3 分钟',
          target: `/diagnosis/progress/${ctx.diagnosisSessionId}`,
          bridgeToLegacy: true,
          secondary: [
            { label: '补客户资料', target: `/my-clients/${bid}` },
            { label: '看历史诊断', actionKey: 'view_history' },
          ],
        };
      }
      return {
        actionKey: 'stage1_start_diagnosis',
        icon: Stethoscope,
        title: '启动 GEO 诊断',
        subtitle: '4 引擎跑客户当前 AI 推荐情况',
        costPoints: 5,
        target: `/diagnosis/new?brand_id=${bid}`,
        bridgeToLegacy: true,
        secondary: [
          { label: '补客户资料', target: `/my-clients/${bid}` },
          { label: '看历史诊断', actionKey: 'view_history' },
        ],
      };
    }

    // Stage 2 · 诊断中(跳旧版 /diagnosis/progress/:id)
    case 2: {
      if (ctx.diagnosisSessionId) {
        return {
          actionKey: 'stage2_progress',
          icon: Loader,
          title: '看诊断进度',
          subtitle: '4 引擎并发跑分中 · 约 2-3 分钟',
          target: `/diagnosis/progress/${ctx.diagnosisSessionId}`,
          bridgeToLegacy: true,
          secondary: [
            { label: '补客户资料', target: `/my-clients/${bid}` },
            { label: '看历史诊断', actionKey: 'view_history' },
          ],
        };
      }
      return {
        actionKey: 'stage2_no_session',
        icon: Loader,
        title: '诊断进行中',
        subtitle: '请稍候',
        target: null,
        secondary: [
          { label: '看历史诊断', actionKey: 'view_history' },
        ],
      };
    }

    // Stage 3 · 待报价(跳旧版 /pricing · 自动填客户 + 诊断关联)
    case 3: {
      const target = ctx.diagnosisId
        ? `/pricing?brand_id=${bid}&diagnosis_id=${ctx.diagnosisId}`
        : `/pricing?brand_id=${bid}`;
      return {
        actionKey: 'stage3_create_quote',
        icon: FileSpreadsheet,
        title: '生成报价方案',
        subtitle: '基于诊断 · 三档算价 · 自动填客户信息',
        costPoints: 3,
        target,
        bridgeToLegacy: true,
        secondary: [
          {
            label: '看诊断报告',
            target: ctx.diagnosisId ? `/diagnosis/report/${ctx.diagnosisId}` : undefined,
          },
          { label: '重做诊断', target: `/diagnosis/new?brand_id=${bid}` },
        ],
      };
    }

    // Stage 4 · 报价中 · 细分:draft / confirmed / pending_payment(跳旧版 /pricing · 自动填客户 + 报价关联)
    case 4: {
      const quoteHref = ctx.quoteId
        ? `/pricing?brand_id=${bid}&quote_id=${ctx.quoteId}`
        : `/pricing?brand_id=${bid}`;
      // draft · 客户还没勾词 → 发选词链
      if (ctx.quoteStatus === 'draft') {
        return {
          actionKey: 'stage4_send_proposal',
          icon: Send,
          title: '发报价给客户',
          subtitle: '复制选词链接 + 微信话术',
          target: quoteHref,
          bridgeToLegacy: true,
          secondary: [
            { label: '改方案 / 加词', target: quoteHref },
            { label: '看诊断报告', target: ctx.diagnosisId ? `/diagnosis/report/${ctx.diagnosisId}` : undefined },
          ],
        };
      }
      // confirmed · 客户已勾词 → 催付款
      if (ctx.quoteStatus === 'confirmed') {
        return {
          actionKey: 'stage4_chase_payment',
          icon: Coins,
          title: '催客户付款',
          subtitle: '客户已确认 · 等付款激活服务期',
          target: null,
          secondary: [
            { label: '复制催付款话术', actionKey: 'copy_payment_script' },
            { label: '改方案', target: quoteHref },
          ],
        };
      }
      // pending_payment · 代理已 offline-confirm · 等收款
      if (ctx.quoteStatus === 'pending_payment') {
        return {
          actionKey: 'stage4_activate',
          icon: Coins,
          title: '激活服务期',
          subtitle: '线下确认收款 · 标已付 · 设月数 · 激活',
          target: null,
          secondary: [
            { label: '看付款记录', target: `/my-clients/${bid}` },
          ],
        };
      }
      // fallback
      return {
        actionKey: 'stage4_default',
        icon: Send,
        title: '看报价方案',
        subtitle: '客户报价正在沟通中',
        target: quoteHref,
        bridgeToLegacy: true,
        secondary: [
          { label: '复制催付款话术', actionKey: 'copy_payment_script' },
        ],
      };
    }

    // Stage 5 · 写作准备 / 写作中(服务期已激活)
    case 5: {
      return {
        actionKey: 'stage5_writing',
        icon: PencilLine,
        title: '整理客户资料 · 进写作',
        subtitle: '粘贴客户资料 · AI 整理 · 客户确认后开写',
        target: `/writing?brand_id=${bid}`,
        bridgeToLegacy: true,
        secondary: [
          { label: '邀请客户补资料', actionKey: 'invite_intake' },
          { label: '改写作要求', target: `/writing?brand_id=${bid}` },
        ],
      };
    }

    // Stage 6 · 投放期(部分发布)
    case 6: {
      return {
        actionKey: 'stage6_publish',
        icon: Megaphone,
        title: '看投放队列',
        subtitle: '知乎 / 百家号 / 今日头条 发布管理',
        target: `/publish?brand_id=${bid}`,
        bridgeToLegacy: true,
        secondary: [
          { label: '补发布证据', target: `/publish?brand_id=${bid}` },
          { label: '回写作大厅', target: `/writing?brand_id=${bid}` },
        ],
      };
    }

    // Stage 7 · 监测中(老板核心痛点 · 19 词 8 列表)
    case 7: {
      const m = ctx.monitoringSummary;
      const subtitle = m
        ? `${m.total} 词 · ${m.qualified} 达标 / ${m.unqualified} 未达标${m.paving ? ` / ${m.paving} 铺量` : ''}`
        : '4 引擎每日监测排名';
      return {
        actionKey: 'stage7_monitoring',
        icon: LineChart,
        title: '看监测',
        subtitle,
        target: `/monitoring?brand_id=${bid}`,
        bridgeToLegacy: true,
        secondary: [
          { label: '看趋势报表', target: `/monitoring?brand_id=${bid}&view=trend` },
          { label: '回投放管理', target: `/publish?brand_id=${bid}` },
        ],
      };
    }

    // Stage 8 · 报告期(月报已出)
    case 8: {
      return {
        actionKey: 'stage8_report',
        icon: FileBarChart2,
        title: '生成月度报告',
        subtitle: '周报 / 月报 / 季报 / 年报 · 客户门户分享',
        target: `/reports?brand_id=${bid}`,
        bridgeToLegacy: true,
        secondary: [
          { label: '看历史报告', target: `/reports?brand_id=${bid}` },
          { label: '看监测', target: `/monitoring?brand_id=${bid}` },
        ],
      };
    }

    // Stage 9 · 续费窗(T-21)
    case 9: {
      return {
        actionKey: 'stage9_renewal',
        icon: RefreshCw,
        title: '发续费方案',
        subtitle: '基于本期 KPI · 推送新报价',
        target: `/pricing?brand_id=${bid}&renewal=1`,
        bridgeToLegacy: true,
        secondary: [
          { label: '看本期 KPI', target: `/reports?brand_id=${bid}` },
          { label: '看监测', target: `/monitoring?brand_id=${bid}` },
        ],
      };
    }

    // 兜底
    default: {
      return {
        actionKey: 'stage_unknown',
        icon: Stethoscope,
        title: '启动 GEO 诊断',
        subtitle: '从诊断开始',
        target: `/diagnosis/new?brand_id=${bid}`,
        bridgeToLegacy: true,
        secondary: [],
      };
    }
  }
}
