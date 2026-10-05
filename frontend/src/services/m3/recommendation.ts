/**
 * M3 任务卡推荐文案生成
 *
 * 老板原话(§10):
 *   "代理打开系统后,应该知道今天最该做什么、为什么现在做、点哪里做、做完进入哪个阶段。"
 *
 * 把 stage / risk / timer 翻译成:
 *   - stateLine(状态人话 · 单行)
 *   - whyNow(为什么是现在 · 显式因)
 *   - scriptHint(微信话术建议)
 *   - cta(主按钮文案 + 安全说明 + 跳哪里)
 *
 * 集中在这 · 不让组件内 if/else 散落业务规则(老板元指令 17 第 12 项: 销售话术不硬编码)。
 *
 * 注意:
 *   - cta.target 是路由 path · 由组件层 navigate
 *   - 金额/积分不再写占位 ¥X(老板红线 · 用户看到 X 字面量是 bug)
 *     调用方需要展示金额时自己从 PricingContext 拼接,如 `${rec.cta.label} · ${cost} 积分`
 */

import type { LucideIcon } from 'lucide-react';
import {
  Stethoscope,
  FileSpreadsheet,
  Send,
  CheckCircle2,
  Wallet,
  PencilLine,
  RefreshCcw,
  ListChecks,
  Megaphone,
  LineChart,
} from 'lucide-react';
import type { SalesClientListItem } from './api';

export interface SalesRecommendation {
  stateLine: string;
  whyNow: string;
  scriptHint?: string;
  cta: {
    label: string;
    hint: string;
    icon?: LucideIcon;
    target: string; // 路由 / API · 由组件 navigate
  };
}

function clientPath(c: SalesClientListItem): string {
  return `/my-clients/${c.id}`;
}

function diagnosisPath(c: SalesClientListItem): string {
  return `/diagnosis/new?brand_id=${c.id}`;
}

function pricingPath(
  c: SalesClientListItem,
  options: { diagnosisId?: number | null; quoteId?: number | null; renewal?: boolean } = {},
): string {
  const params = new URLSearchParams({ brand_id: String(c.id) });
  if (options.diagnosisId) params.set('diagnosis_id', String(options.diagnosisId));
  if (options.quoteId) params.set('quote_id', String(options.quoteId));
  if (options.renewal) params.set('renewal', '1');
  return `/pricing?${params.toString()}`;
}

function monitoringPath(c: SalesClientListItem): string {
  return `/monitoring?brand_id=${c.id}`;
}

export function recommendForSalesCard(c: SalesClientListItem): SalesRecommendation {
  const name = c.name;

  // Stage 1 询价 — 引导去诊断(桥接版:跳旧版真实诊断页)
  if (c.stage === 1) {
    return {
      stateLine: `${name} 询过价 · 还没做诊断`,
      whyNow: '客户问价是高意向信号 · 越早出诊断转化率越高',
      scriptHint: `${name} 您好,我先免费帮您看一下贵品牌在 AI 搜索里的现状,最快 5 分钟出诊断报告。`,
      cta: {
        label: '去做诊断',
        hint: '点击会:打开诊断启动页 · 选品牌+关键词后消耗算力启动 · 失败自动退',
        icon: Stethoscope,
        target: diagnosisPath(c),
      },
    };
  }

  // Stage 2 诊断中 · 进客户详情看进度和历史
  if (c.stage === 2) {
    return {
      stateLine: `${name} 诊断进行中 · 4 引擎并行`,
      whyNow: '诊断在跑 · 别打断 · 完成后会自动通知',
      cta: {
        label: '看诊断进度',
        hint: '点击会:打开客户详情 · 看诊断/报告状态 · 不消耗算力',
        icon: ListChecks,
        target: clientPath(c),
      },
    };
  }

  // Stage 3 待报价 · 进入旧版报价中心,带上客户和诊断上下文
  if (c.stage === 3) {
    const diagId = c.latest_diagnosis_id;
    return {
      stateLine: `${name} 诊断已完成 · 等你出报价`,
      whyNow: '客户已查看诊断 · 现在打开报价页面最合适',
      scriptHint: `诊断报告您先看,我整理好报价后发您微信。`,
      cta: {
        label: '打开报价页面',
        hint: '点击会:打开报价页面 · 基于诊断生成三档报价 · 失败自动退',
        icon: FileSpreadsheet,
        target: pricingPath(c, { diagnosisId: diagId }),
      },
    };
  }

  // Stage 4 报价中(stalled / ready)· 打开报价中心(发链接 + 看进度)
  if (c.stage === 4) {
    const quoteId = c.quote_id;
    const target = pricingPath(c, { quoteId });
    if (c.risk === 'stalled') {
      return {
        stateLine: `${name} 报价已发 · 客户超 24h 没确认`,
        whyNow: '24h+ 无响应 · 流失风险中 · 建议电话沟通 + 发提醒链接',
        scriptHint: `${name} 您好~方案是否方便确认下呢,有任何疑问我马上回。`,
        cta: {
          label: '去催确认 / 看追踪',
          hint: '点击会:打开报价中心 · 复制选词链接发客户 · 不消耗算力',
          icon: Send,
          target,
        },
      };
    }
    return {
      stateLine: `${name} 报价已发 · 等客户确认`,
      whyNow: '报价刚发 · 客户在考虑期 · 持续观察打开动态',
      scriptHint: `方案已发您微信,有任何疑问随时 cue 我~`,
      cta: {
        label: '看报价追踪',
        hint: '点击会:打开报价中心 · 看客户选词进度 · 不消耗算力',
        icon: LineChart,
        target,
      },
    };
  }

  // Stage 5/6/7 服务中
  if (c.stage === 5) {
    return {
      stateLine: `${name} 服务中 · 写作进行中`,
      whyNow: '签了单进交付期 · 销售关注节点 · 写完后看上榜',
      cta: {
        label: '看交付进度',
        hint: '点击会:打开写作中心 · 不消耗算力',
        icon: PencilLine,
        target: `/writing?brand_id=${c.id}`,
      },
    };
  }
  if (c.stage === 6) {
    return {
      stateLine: `${name} 服务中 · 投放进行中`,
      whyNow: '内容已写完 · 渠道铺设阶段',
      cta: {
        label: '看投放进度',
        hint: '点击会:打开发布中心 · 不消耗算力',
        icon: Megaphone,
        target: `/publish?brand_id=${c.id}`,
      },
    };
  }
  if (c.stage === 7) {
    return {
      stateLine: `${name} 服务中 · 监测排名变动`,
      whyNow: '已铺渠道 · 4 引擎在监测 · 关注异常',
      cta: {
        label: '看监测看板',
        hint: '点击会:打开监测中心 · 不消耗算力',
        icon: LineChart,
        target: monitoringPath(c),
      },
    };
  }

  // Stage 8 报告(CTO-15.20 桥接重设计 · 跳旧版 /reports 工作面)
  if (c.stage === 8) {
    return {
      stateLine: `${name} 本月报告已生成`,
      whyNow: '月报新鲜 · 续费弹药就绪 · 适合发链接 + 借势聊续费',
      scriptHint: `${name} 这是本月数据看板,排名进步明显,后续要不要继续?`,
      cta: {
        label: '看本月报告',
        hint: '点击会:跳报告管理页 · 不消耗算力',
        icon: LineChart,
        target: `/reports?brand_id=${c.id}`,
      },
    };
  }

  // Stage 9 续费
  if (c.stage === 9) {
    return {
      stateLine: `${name} 服务即将到期 · ${c.timer ?? '续费窗内'}`,
      whyNow: '续费窗成功率最高 · 月报刚出 · 现在跟最有效',
      scriptHint: `${name} 服务快到期啦,这是上月数据,要不咱们续上不断档?`,
      cta: {
        label: '发续费方案',
        hint: '点击会:打开报价中心续费方案 · 不消耗算力',
        icon: RefreshCcw,
        target: pricingPath(c, { renewal: true }),
      },
    };
  }

  // Fallback
  return {
    stateLine: `${name} · 进客户工作台看详情`,
    whyNow: '当前阶段无紧急动作',
    cta: {
      label: '进客户工作台',
      hint: '点击会:打开客户详情 · 不消耗算力',
      icon: CheckCircle2,
      target: clientPath(c),
    },
  };
}

export const ICON_FOR_INTENT: Record<string, LucideIcon> = {
  diagnose: Stethoscope,
  quote: FileSpreadsheet,
  send: Send,
  pay: Wallet,
  write: PencilLine,
  publish: Megaphone,
  monitor: LineChart,
  renew: RefreshCcw,
};
