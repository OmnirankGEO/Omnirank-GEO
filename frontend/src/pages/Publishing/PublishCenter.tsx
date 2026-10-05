/**
 * PublishCenter — 发布中心
 * 代发 + 自发合在一个页面，顶部 Tab 切换
 * 左侧选文章，右侧选媒体/平台
 * 支持 URL 参数 ?articles=1,2,3 预选文章
 */

import { useState, useEffect, useCallback, useMemo, useRef, lazy, Suspense } from 'react';
import { publicationLayout, publicationEntry, positiveId, advanceClientHandoff, type ClientHandoff } from '@/pages/Writing/imageNoteFlow';
import { publicationTime } from './publicationTime';
import { lazyToast } from '@/lib/lazyToast';
import { useSearchParams } from 'react-router-dom';
import { useIsMobile } from '@/hooks/use-mobile';
import { pauseMobileCoach, resumeMobileCoach } from '@/sandbox/mobileCoachPauseStore';
import { BridgeBanner } from '@/components/workbench/BridgeBanner';
// [XO-03 · R3-P7 ③] 小榜 deep link 预填:URL 只带 opaque xint_,页面向服务端换回
// 已授权的预填数据再填表。文案全部来自服务端(见 lib/xiaobangPrefill.ts 顶部注释)。
// [WO-B ① 2026-08-20] 取数/落参数/降级三件事抽进 useXiaobangPrefill,
// 与 WritingWorkspace、GeoContentCenter **共用同一份实现**;呈现抽进
// XiaobangPrefillRegion。本页原有的 data-testid 与行为一个字没变 ——
// 抽出来的目的正是让本页那一组既有浏览器判据同时守住新接的两页。
import { XiaobangPrefillRegion } from '@/components/xiaobang/XiaobangPrefillRegion';
import { useXiaobangPrefill } from '@/hooks/useXiaobangPrefill';
import { PendingUserActions } from '@/components/publishing/PendingUserActions';
// [2026-07-30 工单 T1/T2/T3] 作用域纯逻辑 + 三个呈现件从本文件抽出:
// 数字对不上(全选 26 / 未分发 5)与"历史记录不见了"都是**集合口径**问题,
// 留在 JSX 里就只能靠源码串断言;抽出来才能拿真数据逐条核。
import {
  partitionArticles, selectAllUnpublishedTarget, isSelectAllChecked,
  toggleSelectAllUnpublished, cartArticlesAlreadyDistributed,
} from '@/pages/Publishing/publishCenterScopeLogic';
// [客户反馈①② 2026-08-09] 两个纯输入口径抽成模块:品牌搜索过滤 / 地区备注媒体信号。
import {
  filterPublishProjects, mediaNeedsRegionRemark, cartMediaNeedsRegionRemark,
  REGION_REMARK_ENABLED, buildRegionRemark, REGION_NAME_MAX_LEN,
} from '@/pages/Publishing/publishCenterInputs';
import { ArticleStatusFilter } from '@/components/publishing/ArticleStatusFilter';
import type { ArticleStatusKey } from '@/components/publishing/ArticleStatusFilter';
import { MediaAdviceDrawer } from '@/components/publishing/MediaAdviceDrawer';
import { HiddenArticlesNotice } from '@/components/publishing/HiddenArticlesNotice';
import { StrictPresubmitPanel } from '@/components/publishing/StrictPresubmitPanel';
import {
  parseStrictPresubmitBlock, blockedMediaIds, applyStrictMediaSwitch,
  type StrictPresubmitBlock,
} from '@/contracts/strictMediaPresubmit';
import {
  parseDuplicateOrderBlock, applyConflictRemoval, remainingAfterRemoval,
  type DuplicateOrderBlock,
} from '@/contracts/duplicateOrderConflict';
import { useEmbeddedNavigate } from '@/hooks/useEmbeddedNavigate';
import { useAuth } from '@/context/AuthContext';
import { authFetch } from '@/lib/api';
import {
  reconcilePage, canGoNext, canGoPrev, shouldClearIndustry, isStalePacket,
} from './mediaListPaging';
import { useClientContext } from '@/context/ClientContext';
import {
  pickProjectForBrand, resolveDeepLinkBrand, shouldSwitchClient, scopeState,
  quoteIdsForBrand,
  type ProjectRef,
} from './publishClientScope';
import { Button } from '@/components/ui/button';
import { Badge, badgeVariants } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import {
  Send, Loader2, Search, CheckCircle2, AlertCircle,
  ChevronDown, ChevronUp, ChevronLeft, ChevronRight,
  Sparkles, ShoppingCart, RefreshCw,
  DollarSign, XCircle,
  Check, History, Brain, Target,
  ShieldCheck,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import type { AwaitingItem } from '@/components/publishing/AwaitingConfirmDialog';
// [E2] 发布流「本行业 AI 真实引用媒体榜」· 代理端鉴权 · 无数据自隐藏。
import { MediaEffectivenessPanel } from '@/components/publishing/MediaEffectivenessPanel';
// [WO_MEDIA_BOARD_UX_CLOSURE 2026-08-05] §2 提示明细抽屉 / §3 本轮调研结果报表。
import { ArticleAdvisoryDrawer } from '@/components/publishing/ArticleAdvisoryDrawer';
import { ResearchRoundReportDialog } from '@/components/publishing/ResearchRoundReportDialog';
import { useMarkStepCompleted } from '@/hooks/useMarkStepCompleted';
import { HelpHint } from '@/components/onboarding/HelpHint';
import { useOnboarding } from '@/context/OnboardingContext';
import { LazyFeatureTooltip as FeatureTooltip } from '@/components/onboarding/LazyFeatureTooltip';
import { isSandboxActive } from '@/sandbox/sandboxState';
import { useTutorialStage, setTutorialStage } from '@/sandbox/tutorialStage';
import {
  canShowSandboxManualGovernance,
  getSandboxPublishGuide,
} from '@/sandbox/publishTutorialContract';
import { SANDBOX_MAGIC_KEYWORDS } from '@/sandbox/constants';
import {
    WEIGHT_RANGE_MAX, collapsedFilterSummary, weightHeader, weightHint,
    MEDIA_SORT_OPTIONS,
} from './mediaTableLabels';
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog';
/* CTO-15.23 Phase 2/2.1 · Density System v1 接入 · 老板 5/22 报"文字密密麻麻"收口
 * - InfoBadgeRow(2.1 替代 2.0 InfoLegendPopover):图标 inline 陈列 + 单独 hover/tap 解释
 *   · 老板 5/22 反馈"徽章图例 文案生涩 · button 包让人不知道里面什么"
 *   · 改用:图标直接摆出来 + cursor-help + tooltip popover(PC hover / mobile tap)
 * - BadgeOverflowGroup(Phase 3 用)· 给 Monitoring/KeywordTable 列分级 + 复合 chip
 * - ExpandableMeta 收 row 末列长备注 → preview 短摘要 + 看详情 */
import { InfoBadgeRow, BadgeOverflowGroup } from '@/components/density';
import type { BadgeItem } from '@/components/density';
import type { ExpandableMetaProps } from '@/components/density/ExpandableMeta';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

const PublishHistory = lazy(() => import('./PublishHistory').then(m => ({ default: m.PublishHistory })));
const GeoResearchCenter = lazy(() => import('@/pages/GeoResearch/GeoResearchCenter').then(m => ({ default: m.default })));
const PlacementCenter = lazy(() => import('@/pages/Placement/PlacementCenter').then(m => ({ default: m.default })));
const AwaitingConfirmDialog = lazy(() => import('@/components/publishing/AwaitingConfirmDialog').then(m => ({ default: m.AwaitingConfirmDialog })));
const ResearchSelfserveDialog = lazy(() => import('@/components/publishing/ResearchSelfserveDialog').then(m => ({ default: m.ResearchSelfserveDialog })));
const PublishRiskConfirmDialog = lazy(() => import('@/components/publishing/PublishRiskConfirmDialog').then(m => ({ default: m.PublishRiskConfirmDialog })));
const ExpandableMetaImpl = lazy(() => import('@/components/density/ExpandableMeta').then(m => ({ default: m.ExpandableMeta })));

function ExpandableMeta(props: ExpandableMetaProps) {
  const text = props.text?.trim();
  const maxChars = props.maxChars ?? 30;
  const fallback = text
    ? <span className={props.className}>{text.length > maxChars ? `${text.slice(0, maxChars)}...` : text}</span>
    : <>{props.emptyFallback ?? '-'}</>;

  return (
    <Suspense fallback={fallback}>
      <ExpandableMetaImpl {...props} />
    </Suspense>
  );
}

// [2026-07-05 老板拍板] 老「推荐媒体」框退役,由「AI 真实引用媒体榜」接替(恢复改 true)。
// 同开关控制 AI 推荐 1.5s 自动触发:结果只显示在老框里,框不显则不再自动烧后端推荐。
const LEGACY_MEDIA_REC_PANEL = false;
// [2026-07-05 老板拍板] 发布参谋 / 投放管理中心顶栏入口隐藏(功能被引用媒体榜与发布记录覆盖)。
// 路由 /geo-research /placement 与 mode 渲染分支保留(直链可达,零删除),恢复改 true。
const SHOW_ADVISOR_PLACEMENT_TABS = false;
const ManualPublicationForm = lazy(() => import('./ManualPublicationForm').then(m => ({ default: m.ManualPublicationForm })));
// [svideo lane · 2026-07-04] 短视频面板：自包含（上传向导 + 账号市场 + 提交），懒加载
const ShortVideoPanel = lazy(() => import('./ShortVideoPanel').then(m => ({ default: m.ShortVideoPanel })));
/* [#195] 图文档的列表视图。lazy:这一档只有点进去才看,和短视频面板同法。 */
const ImageNoteList = lazy(() => import('./ImageNoteList').then(m => ({ default: m.ImageNoteList })));
/** [#186] 图文发布面板(极简两步)。Owner #184 X2 拍板恢复图文线后重建。 */
/*
 * 🔴 [#188 · Review 09-13] `ImageNotePublishPanel` **已删除**。
 *    图文发布从"来发布中心用批量面板"改成"在制作台的作品卡上行内发布"
 *    (设计 §2 区 E:作品就在眼前,不该让人去另一个页面把它重新找一遍)。
 *    两条发布路同时留着 = 用户从哪条走全看运气,而只有一条被判据守着。
 *
 * 🔴 但 `'imagenote'` 这一档**留在状态机里**。#150 撤它时只删了按钮与面板,
 *    `?media_type=imagenote` 的深链照样把 proxyType 切过去、然后**什么都不渲染** ——
 *    用户拿到一片空白,比 500 更难懂(那次是判据 G5 抓到的)。
 *    #186 发出去的链就是这个形状,可能已经在别人的收藏夹里。
 *    所以这一档现在渲染的是**一句指路**,不是空白,也不是第二条发布路。
 */

// ========== Types ==========

interface WritingProject {
  id: number;
  brand_id?: number;
  brand_name: string;
  industry: string;
  keyword_count: number;
  quote_ids?: number[];
}

/**
 * 这一篇来自哪一份报价(#210)。
 *
 * 🔴 一处实现,**每一行文章**都用它 —— 五个分档各写一遍的话,
 *    有一天会有一档忘了跟着改,而那一档看起来仍然"正常"。
 * 🔴 只在**这个客户有不止一份报价**时出现:只有一份时它对谁都没有信息量,
 *    而没有信息量的标签只会让本来就密的那一行更难读。
 */
function ArticleQuoteTag({ quoteId, show }: { quoteId?: number; show: boolean }) {
  if (!show || !quoteId) return null;
  return (
    <span className="text-[10px] text-muted-foreground/80 shrink-0"
      data-testid="article-quote-tag" data-quote-id={quoteId}>
      报价 #{quoteId}
    </span>
  );
}

interface ArticleItem {
  id: number;
  title: string;
  keyword: string;
  /**
   * 这篇来自**哪一份报价**(#210)。
   * 🔴 取自"我们是用哪个 quote_id 拉回来的",不是回包里的字段 ——
   *    回包不一定带它,而"这篇属于哪份报价"这件事,发请求的那一刻我们就知道。
   */
  quoteId: number;
  article_style: string;
  style_code?: string;
  style_family?: string;
  article_id: number;
  status: string;
  is_optimize?: boolean;
  article_review_status?: string;
  article_human_review_status?: string | null;
  publication_eligible: boolean;
  publication_eligibility_reason?: string;
  publication_eligibility_message?: string;
  // [发布门三态拆分 2026-07-31 · 工单 §4.1] 三个正交字段。
  review_state?: 'not_run' | 'computed';
  advisory_state?: 'none' | 'open' | 'acknowledged' | 'repaired';
  advisory_open_count?: number;
  // [P3 并入项 1 · 2026-08-01] 'tenant_or_funds' → 'operator_hard'(纯改名·成员集合不变)。
  publication_h0_state?: 'clear' | 'legal_hard' | 'platform_profile_hard' | 'operator_hard';
}

const ARTICLE_STYLE_LABELS: Record<string, string> = {
  evidence_qa: '证据型问答', qa_recommendation: '证据型问答', 问答FAQ: '证据型问答',
  multi_brand_comparison: '选购与多品牌比较', comparison_review: '选购与多品牌比较',
  recommendation_review: '选购与多品牌比较', ranking_v2: '选购与多品牌比较', authority_ranking: '选购与多品牌比较',
  implementation_guide: '方法与实施指南', buying_guide: '方法与实施指南', 方法指南: '方法与实施指南',
  trend_policy_risk: '趋势、政策与风险分析', risk_compliance: '趋势、政策与风险分析', trojan_horse: '趋势、政策与风险分析',
  case_data_roi: '案例、数据与 ROI', price_roi: '案例、数据与 ROI', data_report: '案例、数据与 ROI',
  company_facts: '企业事实与品牌说明', brand_softarticle: '企业事实与品牌说明', company_profile: '企业事实与品牌说明',
};

function humanizeArticleStyle(raw?: string): string {
  return raw ? (ARTICLE_STYLE_LABELS[raw] || raw) : '';
}

function PublicationReviewBadge({ article, onOpenAdvisory }: {
  article: ArticleItem;
  /** [WO §2] 传入 → 「N 条提示」徽章变成可点入口(打开明细抽屉)。
   *  不传则维持纯展示(写作大厅等其它调用方行为不变)。 */
  onOpenAdvisory?: (article: ArticleItem) => void;
}) {
  if (article.publication_eligible) {
    // 🔴 [三态拆分 2026-07-31] 三态之前这里只有 eligible 一个 bool,于是"可发布"
    // 一律写成「审核通过」。三态放开 review_not_run 之后,1110 篇**从没跑过机审**
    // 的存量文章也会 eligible=true —— 再写「审核通过」就是**让徽章说谎**
    // (本仓 07-30 刚判过同型:"按钮 3 与标签 5 语义本就不同,强求相等=让按钮说谎")。
    // 可发布 ≠ 审核通过,必须按 review_state / advisory_state 分别如实说。
    if (article.review_state === 'not_run') {
      return <Badge className="bg-slate-100 text-slate-700 dark:bg-slate-500/20 dark:text-slate-200 text-[10px] px-1 py-0" title="这篇还没跑过机审;机审不是发布前置条件，可先看稿或直接发布">已生成 · 可先看稿</Badge>;
    }
    if (article.advisory_state === 'open') {
      const n = article.advisory_open_count || 0;
      const label = n > 0 ? `有 ${n} 处可优化` : '有可优化处';
      // 🔴 [WO §2] 这颗徽章此前**全站没有任何明细入口** —— 看得到"3 条提示"却点不开、
      //    不知道是哪 3 条、也没法处理。铁律:提示要么帮人解决问题,要么不显示。
      //    有 onOpenAdvisory 才变可点(否则保持原样,不给点了没反应的东西)。
      if (onOpenAdvisory) {
        return (
          <button
            type="button"
            title="点开看是哪几条提示,可以让 AI 修、自己改,或先忽略"
            data-testid="advisory-badge-entry"
            onClick={(e) => { e.stopPropagation(); onOpenAdvisory(article); }}
            className={cn(
              badgeVariants({ variant: 'default' }),
              'bg-sky-100 text-sky-900 dark:bg-sky-500/20 dark:text-sky-200 text-[10px] px-1 py-0',
              'cursor-pointer hover:bg-sky-200 dark:hover:bg-sky-500/30',
            )}
          >
            {label} ›
          </button>
        );
      }
      return <Badge className="bg-sky-100 text-sky-900 dark:bg-sky-500/20 dark:text-sky-200 text-[10px] px-1 py-0" title="普通质量提示，不影响发布">{label}</Badge>;
    }
    if (article.publication_eligibility_reason === 'brand_story_review_recommended') {
      return <Badge className="bg-sky-100 text-sky-900 dark:bg-sky-500/20 dark:text-sky-200 text-[10px] px-1 py-0" title="建议人工过目，但不影响发布">建议过目 · 可发布</Badge>;
    }
    const label = article.article_human_review_status === 'approved' ? '人工签发' : '可进入投放准备';
    return <Badge className="bg-emerald-100 text-emerald-900 dark:bg-emerald-500/20 dark:text-emerald-200 text-[10px] px-1 py-0">{label}</Badge>;
  }
  // [写作质量总工单 2026-07-29 · D-3] 与写作大厅同一口径:草稿从不被"阻断",
  // 只是对外发布前需要改一处。红色"已阻断"会让代理以为文章废了、钱白花了。
  // [D-1] 亮色配色对补齐 dark: 变体,暗色卡片上不再是深字压深底。
  // 三态之后"不可选"只剩真硬门三类。旧的兜底文案「待审核」不能留:未审核现在
  // 是可发布的(走上面 not_run 分支),再把硬门写成「待审核」会把用户引到
  // "跑一次审核就好了",而实际要做的是改正文/换渠道/重新签发。
  const label = article.publication_h0_state === 'legal_hard' ? '发布前需修改'
    : article.publication_h0_state === 'platform_profile_hard' ? '该渠道需重写'
      : article.publication_h0_state === 'operator_hard' ? '需要你确认一处问题'
        : article.article_review_status === 'blocked' ? '发布前需修改'
          : article.article_review_status === 'rewrite_required' ? '建议重写'
            : '需重新审核确认';
  return <Badge className="bg-amber-100 text-amber-900 dark:bg-amber-500/20 dark:text-amber-100 text-[10px] px-1 py-0" title={article.publication_eligibility_message || article.publication_eligibility_reason}>{label}</Badge>;
}

/**
 * [WO_PUBLISH_DISPATCH Part② 2026-08-17] 行业筛选 chip。
 *
 * `key` 是 L1 大类(约 20 个,后端 `services/media_industry_taxonomy.py` 的
 * `L1_ORDER`),不是目录原始串;`count` 是**当前其余筛选条件下**该大类的媒体数。
 * 后端已经把 0 家的大类过滤掉了,前端不需要也不许再自己补一个「不限之外全渲染」的兜底。
 */
export interface IndustryFacet {
  key: string;
  count: number;
}

interface MediaItem {
  id: number;
  media_name: string;
  platform: string;
  category: string;
  our_price_points: number;
  our_price_yuan: number;
  inclusion_rate: string;
  avg_publish_time: string;
  pc_weight: number;
  mobile_weight: number;
  news_source: string;
  can_geo: number;
  remark: string;
  link_type: string;
  case_link: string;
  engines?: string[];
  geo_score?: number;
  reason?: string;
}

interface MhzMediaItem {
  id: number;
  media_name: string;
  // [D0-b] price/price1/price2(进货价)已不再由后端返回,改收服务端算好的售价算力
  price_points: number;
  is_sweet_spot?: boolean;
  area: string;
  portal_media: string;
  resource_type_name: string;
  resource_type: string;
  inclusion_rate: number;
  publish_rate: string;
  avg_time: number;
  pc_weight: number;
  m_weight: number;
  news_resource: number;
  link_type: number;
  remark: string;
  case_link: string;
  entrance_level: number;
  entrance_link?: string;  // [CTO-15.9] 入口链接(可选 · 后端某些媒体项有 · 前端 2 处 render)
  geo_rank: number;
  geo_rank_platform: string;
  weekend_publish: number;
  authority_media: number;
  special_industry: number;
  our_price_yuan: number;
  our_price_points: number;
  contact_policy?: string;  // [2026-06-02] 联系方式策略 none/website_only/full_contact(默认 none)
}

interface RecMediaItem {
  media_id: number;
  media_name: string;
  platform: string;
  category: string;
  // [D0-b] 推荐链同样不再返回进货价
  price_points: number;
  our_price_points: number;
  our_price_yuan: number;
  inclusion_rate: string;
  engines: string[];
  geo_score: number;
  reason: string;
  /** [WP12 P0-1] 真实被引域权重(飞轮观测),不是人工白名单 */
  ai_citation_strength?: number;
  ai_citation_label?: string;
  ai_citation_count?: number;
  ai_citation_source?: string;
  ai_citation_domain?: string;
  ai_citation_family?: string;
}

/** [WP12 P0-1] 渠道建议:全部 advisory,每条都带可执行下一步,不阻断发布 */
interface ChannelAdvisory {
  code: string;
  message: string;
  reason?: string;
  impact?: string;
  repair_hint?: string;
  rule_version?: string;
}

// [媒体平衡 T1] AI 引用主干：被引最强的通用域（门户/技术社区/UGC 问答）
interface TrunkDomain {
  domain: string;
  citation_count: number;
  strength_label?: string;
  role_label?: string;
  self_serve?: boolean;
  inventory_keyword?: string;
}

// [媒体平衡 T2] 问题族真实被引 mix 折算出的默认组合（advisory，代理可改）
interface CombinationSlot {
  domain: string;
  role_label?: string;
  is_trunk?: boolean;
  share_pct?: number;
  citations?: number;
  fulfilled_by?: string;
  substituted?: boolean;
  substitution_note?: string;
  self_serve_action?: { id: string; label: string; target?: string; platform?: string } | null;
}
interface CombinationPlan {
  advisory?: boolean;
  trunk_slots?: number;
  vertical_slots?: number;
  reason?: string;
  slots?: CombinationSlot[];
  substitutions?: CombinationSlot[];
  self_serve_offers?: CombinationSlot[];
}

// ========== Helpers ==========

const formatAvgTime = (seconds: number) => publicationTime(seconds, 'seconds');

const NEWS_RESOURCE_MAP: Record<number, string> = { 0: '非新闻源', 1: '百度新闻源', 2: '其他新闻源' };

const WM_PLATFORM_LOGOS: Record<string, string> = {
  '百家号': 'https://baijiahao.baidu.com/favicon.ico',
  '搜狐网': 'https://www.sohu.com/favicon.ico',
  '今日头条': 'https://www.toutiao.com/favicon.ico',
  '微信公众号': 'https://mp.weixin.qq.com/favicon.ico',
  '微博': 'https://weibo.com/favicon.ico',
  '知乎号': 'https://static.zhihu.com/heifetz/favicon.ico',
  '腾讯号': 'https://om.qq.com/favicon.ico',
  '网易号': 'https://www.163.com/favicon.ico',
  '哔哩哔哩': 'https://www.bilibili.com/favicon.ico',
  'UC头条': 'https://www.uc.cn/favicon.ico',
  '一点资讯': 'https://www.yidianzixun.com/favicon.ico',
  '东方财富号': 'https://www.eastmoney.com/favicon.ico',
  '凤凰号': 'https://www.ifeng.com/favicon.ico',
  '豆瓣': 'https://www.douban.com/favicon.ico',
  '车家号': 'https://www.autohome.com.cn/favicon.ico',
  '雪球号': 'https://xueqiu.com/favicon.ico',
  '懂车帝': 'https://www.dongchedi.com/favicon.ico',
  '新浪号': 'https://www.sina.com.cn/favicon.ico',
};
const WM_PLATFORM_COLORS: Record<string, string> = {
  '百家号': 'bg-blue-600', '搜狐网': 'bg-orange-500', '今日头条': 'bg-red-600',
  '微信公众号': 'bg-green-600', '微博': 'bg-red-500', '知乎号': 'bg-blue-500',
  '腾讯号': 'bg-blue-500', '网易号': 'bg-red-600', '哔哩哔哩': 'bg-pink-500',
  'UC头条': 'bg-orange-600', '一点资讯': 'bg-red-500', '东方财富号': 'bg-amber-600',
  '凤凰号': 'bg-yellow-600', '豆瓣': 'bg-green-700', '车家号': 'bg-blue-700',
  '雪球号': 'bg-blue-500', '懂车帝': 'bg-blue-600', '新浪号': 'bg-orange-600',
  '其他': 'bg-zinc-500',
};

const fmtFans = (n: number) => {
  if (!n || n <= 0) return '-';
  if (n >= 10000) return `${(n / 10000).toFixed(0)}万`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}千`;
  return String(n);
};

// ========== [CTO-15.23 2026-05-18 v2-F] GEO 真权威 UI helpers ==========
// geo_rank_platform 字段:逗号分隔 a-f 字母(z 不算)· a=DS / b=豆 / c=通 / d=腾 / e=文 / f=Kimi

const GEO_ENGINE_LABELS: Record<string, string> = {
  a: 'DS', b: '豆', c: '通', d: '腾', e: '文', f: 'K',
};

function countGeoEngines(geoRankPlatform?: string | null): number {
  if (!geoRankPlatform) return 0;
  const codes = new Set(geoRankPlatform.split(',').map(c => c.trim().toLowerCase()));
  return ['a', 'b', 'c', 'd', 'e', 'f'].filter(c => codes.has(c)).length;
}

function getGeoEngineLabels(geoRankPlatform?: string | null): string {
  if (!geoRankPlatform) return '';
  const codes = geoRankPlatform.split(',').map(c => c.trim().toLowerCase()).filter(c => GEO_ENGINE_LABELS[c]);
  return codes.map(c => GEO_ENGINE_LABELS[c]).join('');
}

/** GEO 引擎覆盖徽章:6/6 绿 / 5/6 蓝 / 4/6 琥珀 · <4 不显示(噪音) */
function GeoEngineBadge({ geoRankPlatform }: { geoRankPlatform?: string | null }) {
  const cnt = countGeoEngines(geoRankPlatform);
  if (cnt < 4) return null;
  const cls = cnt >= 6
    ? 'bg-emerald-500/20 text-emerald-400'
    : cnt === 5
      ? 'bg-blue-500/20 text-blue-400'
      : 'bg-amber-500/20 text-amber-400';
  const labels = getGeoEngineLabels(geoRankPlatform);
  return (
    <span
      className={cn('shrink-0 px-1 py-0 text-[9px] rounded font-bold', cls)}
      title={`AI 搜索引擎覆盖 ${cnt}/6 · ${labels} · 数字越高 GEO 收录越稳`}
    >
      🤖{cnt}/6
    </span>
  );
}

/** 性价比之王 Badge · ¥30-60 + GEO 引擎覆盖 ≥4 · SSH 实证 58.8% 真实成功率
 * [v2-F Phase 9 改 2026-05-18] 老板报"甜点不知道什么意思" · 改成"性价比" 更通俗自解释
 */
// [D0-b] ¥30-60 的判据建立在**进货价**上,而进货价不再出后端 → 判断挪到服务端,
// 前端只收 is_sweet_spot 布尔;引擎覆盖数仍是公开字段,继续在前端合并判断。
function SweetSpotBadge({ sweetSpot, geoRankPlatform }: { sweetSpot?: boolean; geoRankPlatform?: string | null }) {
  const cnt = countGeoEngines(geoRankPlatform);
  if (!(sweetSpot && cnt >= 4)) return null;
  return (
    <span
      className="shrink-0 px-1 py-0 text-[9px] rounded bg-rose-500/20 text-rose-300 font-bold"
      title="GEO 性价比之王 · ¥30-60 价格区 + 多 AI 引擎覆盖 · SSH 实证 58.8% 成功率"
    >
      🔥 性价比
    </span>
  );
}

/** [CTO-15.23 Phase 2.1 改 2026-05-22] 媒体表格图例 · 图标 inline 陈列 + 单独 hover/tap 解释
 * 老板 5/22 反馈:
 *   v1 → "徽章图例 button + popover" 文案生涩 · 用户不知道按钮里面什么
 *   v2 → 图标直接摆出来 + 每个 cursor-help + tooltip(PC hover / mobile tap)· 用户看到就懂
 * 跟 v1 区别:不再有"徽章图例(6)" button · 6 个 chip 直接陈列(顺序:V → GEO → 6 引擎 → 性价比 → 低价 → 慎选)
 * tooltip 内容比 v1 popover 缩水(单 chip 一句话 · 不分组) · 但用户 hover 时间短 · 体感更直接
 * 历史版本归档:v0 6 chip 文字平铺 · v1 InfoLegendPopover button · 见 git show 28bffa80 */
/*
 * 🔴 [#199] 权重列宽:**跟着有没有区间走**。区间没到(常量 null)时列头还是四个字,
 *    多撑 28px 只会把右边的「新闻源」挤得更窄 —— 为一个还不存在的文字预留宽度,
 *    是拿现在的可读性换将来的。等 199-d1 到了、列头变成「电脑权重 · 0–N」再加宽。
 */
const WEIGHT_COL_W = WEIGHT_RANGE_MAX === null ? '58px' : '78px';

function MediaBadgeLegend() {
  return (
    <div className="flex items-center gap-2 px-4 py-1.5 border-b border-border/30 bg-secondary/20">
      {/* 🔴 [#199] 开可见短标签:改前这一行只有四个光秃秃的 chip,
          名字与说明全在 hover 弹层里 —— 手机上根本没有 hover。 */}
      <InfoBadgeRow
        showLabel
        gap="gap-3"
        items={[
          {
            key: 'v',
            badge: <span className="px-1 py-0 rounded bg-blue-500/20 text-blue-400 font-bold text-[10px]">V</span>,
            label: '权威媒体',
            description: '权威媒体认证 · 高公信力新闻源',
          },
          {
            key: 'geo',
            badge: <span className="px-1 py-0 rounded bg-green-500/20 text-green-400 font-bold text-[10px]">GEO</span>,
            label: '被 AI 引擎引用',
            description: 'GEO 调研观测到该媒体被 AI 搜索引擎引用',
          },
          {
            key: 'engine6',
            badge: <span className="px-1 py-0 rounded bg-emerald-500/20 text-emerald-400 font-bold text-[10px]">🤖6/6</span>,
            label: '6 个 AI 引擎全覆盖',
            description: '主流 AI 引擎都引用过',
          },
          {
            key: 'sweet',
            badge: <span className="px-1 py-0 rounded bg-rose-500/20 text-rose-300 font-bold text-[10px]">🔥 性价比</span>,
            label: '性价比之王',
            description: '¥30-60 价格区 + GEO≥4 引擎 · 实测 59% 收录',
          },
          // [2026-07-28 Owner 拍板] 低价警告 / 极低价慎选 两个图例已下线。
          //   原口径「¥5-15 区仅 17% 收录率」「<¥5 收录率近零」是**只有媒介盒子一家时**测出来的,
          //   快易播接入后新增 3.4 万条媒体、定价档完全不同(例:博客园 ¥4 但备注"GEO 收录好、有排名"),
          //   用老供应商的价格-质量关系去套新渠道会大面积误伤。
          //   媒体优劣改由**飞轮真实被引数据**驱动推荐,不再用价格当质量代理变量。
        ]}
      />
    </div>
  );
}

/** [CTO-15.23 2026-05-18 老板拍方案 B] 文章卡片"已发 X 家 · 进行中 Y · 已拒 Z" 小标签
 * 老板原话:"提交完毕后他又回到了未分发,实际应该是发布中才对,或者有标签显示发布到哪些媒体"
 */
function ArticlePublishBadges({ stat }: { stat?: {
  published: number;
  in_progress: number;
  rejected: number;
  published_media: string[];
  in_progress_media: string[];
} | undefined }) {
  if (!stat) return null;
  const hasAny = stat.published > 0 || stat.in_progress > 0 || stat.rejected > 0;
  if (!hasAny) return null;
  return (
    <div className="flex flex-wrap gap-1 mt-0.5">
      {stat.published > 0 && (
        <span
          className="shrink-0 px-1 py-0 text-[9px] rounded bg-emerald-500/20 text-emerald-400 font-medium"
          title={`已发 ${stat.published} 家: ${stat.published_media.join(', ')}${stat.published > stat.published_media.length ? ` 等 ${stat.published} 家` : ''}`}
        >
          ✓ 已发 {stat.published}
        </span>
      )}
      {stat.in_progress > 0 && (
        <span
          className="shrink-0 px-1 py-0 text-[9px] rounded bg-blue-500/20 text-blue-400 font-medium"
          title={`进行中 ${stat.in_progress} 家: ${stat.in_progress_media.join(', ')}${stat.in_progress > stat.in_progress_media.length ? ` 等 ${stat.in_progress} 家` : ''}`}
        >
          ⏳ 进行中 {stat.in_progress}
        </span>
      )}
      {stat.rejected > 0 && (
        <span
          className="shrink-0 px-1 py-0 text-[9px] rounded bg-red-500/15 text-red-400 font-medium"
          title={`被 ${stat.rejected} 家媒体拒稿 · 可重新选媒体发布`}
        >
          ✗ 已拒 {stat.rejected}
        </span>
      )}
    </div>
  );
}

// ========== 购物车 ==========

interface CartMediaItem {
  id: number;
  name: string;
  costPoints: number;
  costYuan: number;
  mediaType: 'mhz' | 'wemedia';
  /** [客户反馈② 2026-08-09] 该媒体只能靠下单备注指定地区(加购时算好,旧车为 undefined) */
  needsRegionRemark?: boolean;
}

interface CartItem {
  articleId: number;
  articleTitle: string;
  keyword: string;
  mediaList: CartMediaItem[];
  recommendationPayload?: Record<string, any>;
  evidencePayload?: Record<string, any>;
  recommendationLevel?: number;
  analysisVersion?: string;
}

const CART_KEY = 'omnirank_publish_cart';

function loadCart(projectId: number | null): CartItem[] {
  try {
    const raw = JSON.parse(localStorage.getItem(CART_KEY) || '{}');
    if (raw.projectId === projectId) {
      // 兼容旧数据：补上 mediaType 默认值
      return (raw.items || []).map((item: CartItem) => ({
        ...item,
        mediaList: item.mediaList.map(m => ({ ...m, mediaType: m.mediaType || 'mhz' as const })),
      }));
    }
  } catch {}
  return [];
}
function saveCart(projectId: number | null, items: CartItem[]) {
  localStorage.setItem(CART_KEY, JSON.stringify({ projectId, items, updatedAt: Date.now() }));
}

// [FIX-1] 自助调研进度轮询上移父层(关窗 / 刷新 / 换设备也能完成 toast + 刷新榜·验收①②③④)。
const SELFRES_POLL_INTERVAL_MS = 8000;
const SELFRES_POLL_OVERRUN_INTERVAL_MS = 30_000;
const SELFRES_POLL_TIMEOUT_MS = 35 * 60_000;     // 排队不计,只从真正 running 起算(#12)
const SELFRES_MAX_POLL_ERRORS = 4;               // 5xx/网络抖动连续 N 次才停(#13)
const SELFRES_STATUS_LABEL: Record<string, string> = {
  queued: '排队中', running: '调研进行中', completed: '已完成', failed: '未完成', timeout: '超时', cancelled: '已取消',
};
// [FIX-1] 失败文案不超售:release 可能失败等 freeze_sweeper 12h 兜底,不一律说"已退还",统一"将自动退还"。
function humanizeSelfresFailure(reason?: string | null): string {
  const r = (reason || '').toLowerCase();
  if (r.includes('board_not_lit')) return '本次未采集到足够本行业数据,算力将自动退还(可在钱包查看)';
  if (r.includes('insufficient')) return '算力不足,已取消本次调研';
  return '本次调研未完成,算力将自动退还(可在钱包查看)';
}

// ========== Component ==========

export function PublishCenter() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { user } = useAuth();
  const navigate = useEmbeddedNavigate();
  const markStep = useMarkStepCompleted();
  const { isStepCompleted } = useOnboarding();
  // [2026-05-27] 移动端教程文案分支 · PC "右侧" / 移动 上下堆叠
  const isMobile = useIsMobile();
  const prevCartLen = useRef<number | null>(null);
  const preselected = searchParams.get('articles')?.split(',').map(Number).filter(Boolean) || [];
  const preQuoteId = searchParams.get('quote_id') ? Number(searchParams.get('quote_id')) : null;
  // 重发参数
  const preArticleId = searchParams.get('article_id') ? Number(searchParams.get('article_id')) : null;
  const preBrandId = searchParams.get('brand_id') ? Number(searchParams.get('brand_id')) : null;
  // [共享主人翁制 · GEO 图文 2026-08-03] 创作中心「去发布投放」跳过来时携带的作品 id
  const preGeoPostId = searchParams.get('geo_post_id')
    ? Number(searchParams.get('geo_post_id')) : null;
  const republishFromSn = searchParams.get('republish_from') || null;

  // ── [XO-03 · R3-P7 ③ / WO-B ① 2026-08-20] 小榜预填 ─────────────────────
  // 🔴 填表走的是**既有那套** preBrandId/preArticleId/articles 参数链,不另写一套
  //    选择逻辑(Refactor-not-Rewrite:那套已经被现有判据覆盖过)。
  //    intent 参数**留在 URL 里**,刷新/后退/换设备才能按 §12.2 恢复。
  // 🔴 映射表的键必须是后端 `_FORM_PREFILL_FIELDS` 真会写的那几个;
  //    写一个后端从不产出的键不会报错,只会安静地什么都不填。
  const {
    prefill: xiaobangPrefill,
    error: xiaobangPrefillError,
  } = useXiaobangPrefill({
    numbers: {
      brand_id: 'brand_id',
      article_id: 'article_id',
      // [R3-P11 ①] GEO 图文落进本页**既有**的 geo_post_id 参数
      geo_post_id: 'geo_post_id',
      // [R3-P11 ①] quote 之前是半截链:服务端冻结了、本页也在读,就是没人接上
      quote_id: 'quote_id',
    },
    // 🔴 [WO-B ① 2026-08-20] 这里原本还有 `lists: { article_ids: 'articles' }` ——
    //    **半截链**:后端 `_FORM_PREFILL_FIELDS` 从来不产出 `article_ids`
    //    (`_SELECTION_ALLOWLIST` 里也没有它),所以 `prefillNumberList` 恒返空数组,
    //    `articles=1,2,3` 这条多篇预填**从来没有生效过**。
    //    留着它是"看起来支持、实际不支持";所以先摘掉这个死消费方
    //    (运行时零行为变化 —— 它本来就什么都不填)。
    //    要真支持多篇深链,得同时扩 `_SELECTION_ALLOWLIST` + `_FORM_PREFILL_FIELDS`
    //    并对**每一个** article id 逐个过对象级鉴权 —— 那是新的一格,交付单已上报。
  });

  // 初始 mode 来自 URL ?mode=manual 等 · 默认 proxy
  // 🔴 [WO_273 · Owner 09-23「直接退役」] 浏览器插件自助发布整块退役:自助这一档从模式联合里删掉(不是隐藏)。
  //    老链 ?mode=self 不在白名单里 ⇒ 落回 proxy(代发),不会进一个没有渲染分支的白屏。
  const initialModeParam = searchParams.get('mode');
  const initialMode: 'proxy' | 'history' | 'advisor' | 'placement' | 'manual' =
    initialModeParam === 'manual' || initialModeParam === 'history' ||
    initialModeParam === 'advisor' || initialModeParam === 'placement'
      ? initialModeParam
      : 'proxy';
  const [mode, setMode] = useState<'proxy' | 'history' | 'advisor' | 'placement' | 'manual'>(initialMode);
  // [CTO-15.23 2026-05-05] sm 全屏分步流 · proxy/self 两面板挤压 → step 切换
  // article: 选文章全屏 · media: 选媒体全屏 · md+ 永远双面板并排不受影响
  const [mobileStep, setMobileStep] = useState<'article' | 'media'>('article');
  // [CTO-15.23 2026-05-05] proxy 模式批量发布 · checkbox 多选 · activeArticle 仍 driver 推荐
  // 空 = 单文章模式(行为同前) · 有 = 批量模式(N 篇 × 同组媒体一次加 N 条 cart)
  const [batchArticleIds, setBatchArticleIds] = useState<Set<number>>(new Set());
  // [2026-04-30] 重发跳转时带的 media_type 参数：wemedia → 默认进自媒体 tab
  // [svideo lane · 2026-07-04] 新增 'svideo' 短视频子 tab（自包含 ShortVideoPanel，不进共享购物车）
  /**
   * 🔴 [#150 2026-09-08] `'imagenote'` 这一档随 tab 一并从**状态机**里去掉。
   *    只删按钮与面板是不够的:`?media_type=imagenote` 的深链仍会把 proxyType 切过去,
   *    然后**什么都不渲染** —— 用户拿到一片空白,比原来的 500 更难懂。
   *    (是本单判据 G5 抓到的:它判的是"这个词在去注释源里一处不剩",不是"按钮不见了"。)
   */
  /**
   * 🔴 [#186 2026-09-13] `'imagenote'` 这一档**回来了**。
   *
   *    #150 撤它的理由是「后端表不存在、0 调用、点进去只能 500」;
   *    #184 §6 已证伪那条理由(表早在、从未写入,真因是普通生产路径不建 revision),
   *    Owner 09-12 拍板恢复图文线。所以这不是"把撤掉的东西又加回来",
   *    是**前提变了之后按新前提重判** —— 撤除当初就是一个预测,到点要拿真交付物核。
   *    (判据 G5 已随之重锚:从"这一档不许在"改成"它在,但旧的必 400 提交口不许回来"。)
   */
  const entry = publicationEntry(searchParams);
  const initialProxyType = entry.type;
  const [proxyType, setProxyType] = useState<'article' | 'wemedia' | 'svideo'>(initialProxyType);
  const imageNoteMode = proxyType === 'svideo' && entry.content === 'imagenote';
  const flowLayout = publicationLayout(mode, proxyType, mobileStep);
  // A tab is a navigable location: refresh/back must restore the same publishing lane.
  useEffect(() => { setProxyType(initialProxyType); }, [initialProxyType]);
  const chooseProxyType = (type: typeof proxyType) => {
    const q = new URLSearchParams(searchParams);
    q.set('media_type', type === 'article' ? 'media' : type);
    if (type !== 'svideo') { q.delete('geo_post_id'); q.delete('content_type'); }
    setSearchParams(q);
    setProxyType(type);
  };
  const chooseVideoContent = (content: 'imagenote' | 'video') => {
    const q = new URLSearchParams(searchParams);
    q.set('media_type', 'svideo'); q.set('content_type', content);
    if (content === 'video') q.delete('geo_post_id');
    setSearchParams(q);
  };
  const [filtersExpanded, setFiltersExpanded] = useState(false);

  // [R 批 · U6] 「⚡ 点亮本行业调研」确认弹窗 + 付费完成后重拉本行业榜。
  const [researchDialogOpen, setResearchDialogOpen] = useState(false);
  const [researchDialogIndustry, setResearchDialogIndustry] = useState('');
  const [researchRefreshKey, setResearchRefreshKey] = useState(0);

  // ===== [WO_MEDIA_BOARD_UX_CLOSURE 2026-08-05] 三件套的本地状态 =====
  // §2 「N 条提示」明细抽屉:徽章此前全站没有入口(看得到点不开)。
  const [advisoryArticle, setAdvisoryArticle] = useState<ArticleItem | null>(null);
  const [advisoryOpen, setAdvisoryOpen] = useState(false);
  const openAdvisoryFor = useCallback((a: ArticleItem) => {
    setAdvisoryArticle(a);
    setAdvisoryOpen(true);
  }, []);
  // §3 本轮调研结果报表 / 历轮回看。roundReportId 为空 = 先进历轮列表。
  const [roundReportOpen, setRoundReportOpen] = useState(false);
  const [roundReportId, setRoundReportId] = useState('');
  // §1 点击闭环:从榜点过来的那次搜索,搜不到时要给**显式空态**
  // (「该站为 AI 常引用头部站点,媒体库暂无对应可购渠道」),
  // 而不是通用的"无匹配媒体" —— 后者会让代理以为是自己搜错了字。
  const [boardSearchOrigin, setBoardSearchOrigin] = useState<
    { term: string; oneLiner: string; domain: string } | null
  >(null);

  // [WO §1 点击闭环] 媒体库空结果的文案。从榜点过来的那次搜索必须如实说清楚
  // "不是你搜错了字,是这家站我们目前没有可购渠道" —— 通用的"无匹配媒体"会让代理
  // 反复改关键词试(死点击的第二种形态)。非榜来源的搜索保持原文案,行为不变。
  const renderMediaEmpty = (currentSearch: string) => {
    const origin = boardSearchOrigin;
    if (!origin || !currentSearch || origin.term !== currentSearch) {
      return <div className="text-center py-12 text-sm text-muted-foreground">无匹配媒体</div>;
    }
    return (
      <div className="px-4 py-10 text-center" data-testid="media-board-click-empty">
        <div className="text-sm text-foreground">「{origin.term}」在媒体库里暂无对应可购渠道</div>
        <div className="mx-auto mt-1.5 max-w-md text-xs leading-relaxed text-muted-foreground">
          该站为 AI 常引用头部站点{origin.oneLiner ? ` · ${origin.oneLiner}` : ''},但我们目前没有它的可投放资源。
          先投榜上标了「可投放」的那几家,效果是一样在赌 AI 会不会引用。
          {origin.domain ? <span className="ml-1 text-muted-foreground/70">({origin.domain})</span> : null}
        </div>
      </div>
    );
  };

  // 左侧：文章
  const [projects, setProjects] = useState<WritingProject[]>([]);
  /**
   * 🔴 [#180 2026-09-12] 「当前客户」只有一个入口:**左上角**(ClientContext)。
   *
   *    Owner 09-12 图 3:「这两个选择是完全割裂的,应该统一客户选择入口」。
   *    割裂的代价不是多一个下拉框 —— 老代码自己维护一份 selectedProject,
   *    三个分支都落空时兜底 `list[0]`(与左上角毫无关系的第一个项目),
   *    于是页面在 A 上工作、左上角显示 B,而提交体的 brand_id 取页面这一份:
   *    「显示对、记错人」。同一条教训在 DouyinPostDetail 的注释里有生产实证。
   *
   *    所以这里从 state 改成**派生值**:它没有 setter,任何人想改客户都只能去改
   *    左上角 —— 「同一个谓词只许有一处实现」在 UI 层的落法。
   *    页面级下拉(原 :3413-3495)整块已撤,连同 projectSearchQuery /
   *    projectDropdownOpen / projectComboboxRef 与那个点外关闭的 effect。
   */
  const { currentBrandId, clients, isAllClientsMode, clientContext, switchClient } = useClientContext();
  const clientHandoff = useRef<ClientHandoff | null>(null);
  const clientScope = useMemo(() => scopeState({
    isAllClientsMode,
    currentBrandId: currentBrandId ?? null,
    projects: projects as unknown as ProjectRef[],
  }), [isAllClientsMode, currentBrandId, projects]);
  const selectedProject = clientScope.projectId;
  const [articles, setArticles] = useState<ArticleItem[]>([]);
  /*
   * 🔴 [#210] 只看某一份报价。**默认 null = 全部** ——
   *    真客户那次就是因为一次只看一份,另一份下的 18 篇一篇都看不见。
   *    筛选是收窄,收窄得由用户主动做,不能是缺省。
   */
  const [quoteFilter, setQuoteFilter] = useState<number | null>(null);
  /* 只有一份报价时不显示「报价 #N」标签:它对谁都没有信息量。 */
  const quoteTagVisible = clientScope.quoteIds.length > 1;
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set(preselected));
  const [loadingArticles, setLoadingArticles] = useState(false);
  const [onlyOptimize, setOnlyOptimize] = useState(false);

  // 代发：媒体（旧接口 state 保留但不再用于代发模式）
  const [mediaType, setMediaType] = useState<'media' | 'wemedia'>('media');
  // 推荐媒体 V2 — 软文和自媒体同时加载
  const [channelAdvisories, setChannelAdvisories] = useState<ChannelAdvisory[]>([]);
  const [citationSignal, setCitationSignal] = useState<string>('');
  // [媒体平衡 T1/T2 2026-07-29] 通用主干不再藏 + 问题族组合(advisory)
  const [citationTrunk, setCitationTrunk] = useState<TrunkDomain[]>([]);
  const [combinationPlan, setCombinationPlan] = useState<CombinationPlan | null>(null);
  // [P1-5b] D6-B:该品牌真实被引的发布域(advisory;样本不足显示「暂无足够样本」)
  /** [WO_267] recommend-v2 回的调研开通状态;not_open ⇒ 明说「该行业的调研尚未开通」,不再匹到邻居行业 */
  const [researchStatus, setResearchStatus] = useState<{ status?: string; category_name?: string | null } | null>(null);
  const [brandRecoFeedback, setBrandRecoFeedback] = useState<{
    available: boolean; reason?: string; citation_total?: number; min_sample?: number;
    distinct_domains?: number; distinct_providers?: number; coverage_note?: string;
    domains?: Array<{ domain: string; citations: number; providers: string[] }>;
  } | null>(null);
  const [recMediaVertical, setRecMediaVertical] = useState<RecMediaItem[]>([]);
  const [recMediaGeneric, setRecMediaGeneric] = useState<RecMediaItem[]>([]);
  const [recWemediaVertical, setRecWemediaVertical] = useState<RecMediaItem[]>([]);
  const [recWemediaGeneric, setRecWemediaGeneric] = useState<RecMediaItem[]>([]);
  const [artRecIndustry, setArtRecIndustry] = useState('');

  // [FIX-1] 自助调研在飞任务(轮询 / 进行中指示归父层,弹窗只是视图)。
  const [activeResearchTask, setActiveResearchTask] = useState<
    { taskId: number; status: string; overrun: boolean; industryKey: string } | null
  >(null);

  // [FIX-1] 服务端恢复源:选中客户/行业变化 → 查本人本行业在飞任务(清 localStorage / 换设备 / 刷新也能恢复·验收③④)。
  useEffect(() => {
    if (mode !== 'proxy') return;
    const proj = projects.find(p => p.id === selectedProject);
    const ind = (proj?.industry || artRecIndustry || '').trim();
    if (!ind) return;
    /* [WO_267] 带上品牌:行业判定吃品牌上下文,与点亮弹窗 /draft、历轮 /rounds 同一份输入 ——
       否则同一品牌两个端点判出的大类可能不同,在飞任务就恢复不回来 */
    const activeTaskQuery = new URLSearchParams({ industry: ind });
    if (proj?.brand_id) activeTaskQuery.set('brand_id', String(proj.brand_id));
    let cancelled = false;
    let controller: AbortController | null = null;
    const load = async () => {
      if (cancelled || document.hidden || controller) return;
      const activeController = new AbortController();
      controller = activeController;
      try {
        const res = await authFetch(`/api/publish/research/active-task?${activeTaskQuery}`, { signal: activeController.signal });
        if (cancelled || activeController.signal.aborted || !res.ok) return;
        const d = await res.json();
        if (cancelled || activeController.signal.aborted) return;
        const at = d?.active_task;
        const ik = String(d?.industry_key || ind);
        if (at?.task_id) {
          setActiveResearchTask(prev =>
            prev?.taskId === at.task_id
              ? prev
              : { taskId: at.task_id, status: String(at.status || 'queued'), overrun: false, industryKey: ik });
        } else {
          // [GEO-GREEN-CAN-001] 当前行业无在飞任务:清掉挂在别的行业上的旧任务,
          //   否则从行业 A(有任务)切到行业 B(无任务)后,头部/弹窗仍显示 A 的进度
          //   并阻断 B 的正常开始。只清 industryKey 与当前行业不一致的残留任务。
          setActiveResearchTask(prev => (prev && prev.industryKey !== ik ? null : prev));
        }
      } catch { /* 恢复失败静默,不打扰用户 */ }
      finally { if (controller === activeController) controller = null; }
    };
    const onVisibility = () => {
      if (document.hidden) controller?.abort();
      else void load();
    };
    document.addEventListener('visibilitychange', onVisibility);
    void load();
    return () => {
      cancelled = true;
      controller?.abort();
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProject, mode, projects, artRecIndustry]);

  // [FIX-1] 后台轮询(activeResearchTask 存在即跑,弹窗开关无关 → 关窗 / 刷新也能完成 toast + 刷新榜·验收①②)。
  //   搬自 ResearchSelfserveDialog:#12(排队不计超时·runningStartedAt 只从 running 起)/#13(403·404 立停,
  //   5xx·网络抖动连续 N 次才停)/ 每个 await 后 stale 守卫(FIX-7 在父层等效落实)。
  useEffect(() => {
    const taskId = activeResearchTask?.taskId;
    if (mode !== 'proxy' || !taskId) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let controller: AbortController | null = null;
    // [FIX1-04 覆审 FE-1] 不 seed 恢复时刻:首个 poll(line 708)据服务端 started_at 锚定真实 running 起点。
    //   旧版 `status==='running' ? Date.now() : null` 会把恢复态锚死当下,使 line 708 的 started_at 逻辑被
    //   `runningStartedAt === null` 闸死永不执行 → 恢复长跑任务重获满 35min 窗口(FIX1-04 目标未达成)。
    let runningStartedAt: number | null = null;
    let isOverrun = !!activeResearchTask?.overrun;
    let consecutiveErrors = 0;
    const schedule = (ms: number) => {
      if (!stopped && !document.hidden) timer = setTimeout(poll, ms);
    };
    const clearTask = () => setActiveResearchTask(prev => (prev?.taskId === taskId ? null : prev));
    const bump = (status: string) =>
      setActiveResearchTask(prev => (prev && prev.taskId === taskId ? { ...prev, status, overrun: isOverrun } : prev));
    const onErr = () => {
      consecutiveErrors += 1;
      if (consecutiveErrors >= SELFRES_MAX_POLL_ERRORS) {
        // [GEO-GREEN-CAN-002] 达到连续错误上限后不再静默 stopped 停死(旧逻辑会让"进行中"
        //   指示器永久空转、无完成 toast、无榜刷新)。改为降到低频 backoff 继续重试并保留任务身份,
        //   后端短时抖动恢复后即可自动收到 completed/terminal → 正常清任务 + 刷新榜。
        consecutiveErrors = 0;
        schedule(SELFRES_POLL_OVERRUN_INTERVAL_MS);
        return;
      }
      schedule(isOverrun ? SELFRES_POLL_OVERRUN_INTERVAL_MS : SELFRES_POLL_INTERVAL_MS);
    };
    const poll = async () => {
      if (stopped || document.hidden || controller) return;
      const activeController = new AbortController();
      controller = activeController;
      let res: Response;
      try { res = await authFetch(`/api/publish/research/task/${taskId}`, { signal: activeController.signal }); }
      catch {
        if (controller === activeController) controller = null;
        if (stopped || activeController.signal.aborted) return;
        onErr(); return;
      }
      if (controller === activeController) controller = null;
      if (stopped || activeController.signal.aborted) return;
      if (!res.ok) {
        if (res.status === 403 || res.status === 404) { stopped = true; clearTask(); return; }
        onErr(); return;
      }
      consecutiveErrors = 0;
      let d: { status?: string; failed_reason?: string | null; started_at?: string | null; round_id?: string | null };
      try { d = await res.json(); } catch { if (stopped) return; onErr(); return; }
      if (stopped) return;
      const status = String(d?.status || '');
      // [FIX1-04] running 起点优先用服务端 started_at:换设备/刷新恢复一个已跑很久的任务时,overrun 按真实
      //   已运行时长计(不被恢复时刻重置 → 长跑任务不会重获满 35min 窗口)。started_at 缺失才回退恢复时刻。
      if (status === 'running' && runningStartedAt === null) {
        const parsed = d?.started_at ? Date.parse(d.started_at) : NaN;
        runningStartedAt = Number.isFinite(parsed) ? parsed : Date.now();
      }
      bump(status);
      if (status === 'completed') {
        stopped = true;
        // [WO §3.5] toast 只是**入口不是终点**:完成那一刻给一条能点进报表的 toast。
        // 生产实证过的病:反哺其实成功了(29 条引用落库),但用户只看到一句
        // "已点亮",归并进已有行业时肉眼零变化 → 花 3900 算力像什么都没发生。
        const rid = String(d?.round_id || '');
        if (rid) {
          lazyToast.success('本行业榜已点亮 · 点这里看本轮结果', {
            duration: 12000,
            action: { label: '看结果', onClick: () => { setRoundReportId(rid); setRoundReportOpen(true); } },
          });
        } else {
          lazyToast.success('本行业榜已点亮');
        }
        setResearchRefreshKey(k => k + 1);
        clearTask();
        return;
      }
      if (status === 'failed' || status === 'timeout' || status === 'cancelled') {
        stopped = true;
        lazyToast.error(humanizeSelfresFailure(d?.failed_reason));
        clearTask();
        return;
      }
      if (runningStartedAt !== null && Date.now() - runningStartedAt > SELFRES_POLL_TIMEOUT_MS) {
        if (!isOverrun) { isOverrun = true; bump(status); }
        schedule(SELFRES_POLL_OVERRUN_INTERVAL_MS);
        return;
      }
      schedule(isOverrun ? SELFRES_POLL_OVERRUN_INTERVAL_MS : SELFRES_POLL_INTERVAL_MS);
    };
    const onVisibility = () => {
      if (document.hidden) {
        if (timer) clearTimeout(timer);
        timer = null;
        controller?.abort();
        controller = null;
      } else {
        schedule(0);
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    void poll();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      controller?.abort();
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeResearchTask?.taskId, mode]);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [deepAnalysis, setDeepAnalysis] = useState<any>(null);
  const [deepAnalysisLoading, setDeepAnalysisLoading] = useState(false);
  const [selectedPackageType, setSelectedPackageType] = useState('balanced');
  const [recPanelOpen, setRecPanelOpen] = useState(false);
  const [market, setMarket] = useState<MediaItem[]>([]);
  const [mPage, setMPage] = useState(1);
  const [mTotal, setMTotal] = useState(0);
  const [mPages, setMPages] = useState(0);
  const [mSearch, setMSearch] = useState('');
  const [mSearchInput, setMSearchInput] = useState('');
  const [mCategory, setMCategory] = useState('');
  const [mPlatform, setMPlatform] = useState('');
  const [mNewsSource, setMNewsSource] = useState('');
  const [mGeo, setMGeo] = useState(false);
  const [mHighValue, setMHighValue] = useState(false);
  const [mEngine, setMEngine] = useState('');
  const [engineMedia, setEngineMedia] = useState<MediaItem[]>([]);
  const [engineTotal, setEngineTotal] = useState(0);
  const [loadingEngine, setLoadingEngine] = useState(false);
  const [mSort, setMSort] = useState('our_price_points_asc');
  const [loadingM, setLoadingM] = useState(false);
  const [filterData, setFilterData] = useState<{
    industries: string[]; regions: string[]; platforms: string[]; news_sources: string[];
  }>({ industries: [], regions: [], platforms: [], news_sources: [] });
  const [selMedia, setSelMedia] = useState<Set<number>>(new Set());
  const [mediaMap, setMediaMap] = useState<Map<number, MediaItem>>(new Map());

  const [publishedArticleIds, setPublishedArticleIds] = useState<Set<number>>(new Set());
  // 🔴 R6 补充①:`/published-articles` 从 R2 §② 起就分开返回了「已核实」与
  //    「回报成功未核实」两个集合,前端**一直没接**。不接的后果不是少个功能,
  //    而是 stats 未到货时(首屏 / stats 失败 / 切客户空窗)分组走的那条 fallback
  //    路上,未核实的自报直接进「已发布」——一条完全静默的旧路。
  const [verifiedPublishedArticleIds, setVerifiedPublishedArticleIds] = useState<Set<number>>(new Set());
  const [reportedUnverifiedArticleIds, setReportedUnverifiedArticleIds] = useState<Set<number>>(new Set());
  // 已拒稿过的文章 ID(任一媒体下单中被拒过)。用于"已拒稿"独立分组,优先级高于"已分发"。
  const [rejectedArticleIds, setRejectedArticleIds] = useState<Set<number>>(new Set());

  // [CTO-15.23 2026-05-18 老板报"提交后回到未分发"] 文章发布状态全量统计 ·
  // 拆 4 分组:已发布(有 status=2)/ 发布中(只 status=0/1)/ 已拒稿(只 -1/-2)/ 未分发(无 order)
  // 同时给卡片显示"已发 X 家"标签 · 解决"操作很怪"用户体感
  type ArticlePublishStat = {
    status: 'published' | 'in_progress' | 'rejected' | 'none';
    published: number;
    in_progress: number;
    rejected: number;
    published_media: string[];
    in_progress_media: string[];
  };
  const [articlePublishStats, setArticlePublishStats] = useState<Map<number, ArticlePublishStat>>(new Map());
  // [WO-PUBCENTER-LAYOUT 2026-08-04 · L2] 左栏当前看哪个状态的文章。
  // (原来这里还有个 rejectedGroupOpen 管"已拒稿分组展开没有"。四段堆叠改成分段筛选器后
  //  它变成只写不读的死 state —— 它的两处职责已分别由本 state 的初值和上面那处
  //  setArticleStatusFilter('rejected') 承担,所以整个删掉,不留空壳。)
  // 四个状态不再各占一截垂直空间(旧做法四段堆叠 + minHeight 120/180/180/180px,
  // 三组同时非空时机械下限 660px+,13 寸笔记本根本放不下),改成同一时刻只渲染一个,
  // 列表 flex-1 吃满剩余高度。四个计数由 ArticleStatusFilter 常驻显示,不靠滚动就能看见。
  //
  // 默认落在"未分发"(主战场);带 republish_from 从重发链路跳进来时直接落到"已拒稿",
  // 保住原来 rejectedGroupOpen 自动展开 + scrollIntoView 定位那条行为。
  const [articleStatusFilter, setArticleStatusFilter] = useState<ArticleStatusKey>(
    republishFromSn ? 'rejected' : 'unpublished',
  );
  // [WO-PUBCENTER-LAYOUT 2026-08-04 · L3] 底部媒体建议抽屉。默认收起 —— 收起态只占一行把手。
  const [mediaAdviceOpen, setMediaAdviceOpen] = useState(false);
  // L4 拖拽高度按用户维度存:同一台电脑换账号不继承上一个人的高度。
  const mediaAdviceStorageKey = `publish_center_media_advice_h_v1:${user?.id ?? 'anon'}`;
  // 抽屉能占多高要拿左栏实际高度来夹,拿不到就不允许拖(clamp 会退化)。
  const leftPaneRef = useRef<HTMLDivElement | null>(null);
  // [折叠 2026-07-28 老板拍板] 左栏纵向空间本来就挤,不折叠时可视区被各分组瓜分到几乎看不清。
  // 全部分组 + 两块媒体建议一律可折叠,选择记进 localStorage(用户"想打开哪个自己打开",
  // 每次进页面都要重折一遍就等于没做)。默认值取"当下最常用的展开、回顾性的收起"。
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(() => {
    // [WO-PUBCENTER-LAYOUT 2026-08-04 · L2] unpublished / inProgress / published 三个 key 已删:
    // 代发左栏的四个状态改成分段筛选器后没有"折叠"这回事了,留着就是没人读的死键。
    // 老用户 localStorage 里存着的旧键会被 `{...fallback, ...JSON.parse(raw)}` 原样带进来,
    // 但没有代码读它们 —— 不影响功能,也不值得为此写一次迁移。
    const fallback: Record<string, boolean> = {
      t1Trunk: true,        // 媒体建议 · 默认收起
      t2Combo: true,        // 同上(老板明确要求这块别占位)
      // [WO_273] 原 selfPublished(自助 tab 的「已代发」折叠)随自助 tab 一并删;老 localStorage 里的键同上,无人读。
    };
    try {
      const raw = localStorage.getItem('publish_center_collapsed_v1');
      return raw ? { ...fallback, ...JSON.parse(raw) } : fallback;
    } catch { return fallback; }
  });
  const toggleCollapsed = (key: string) => {
    setCollapsed(prev => {
      const next = { ...prev, [key]: !prev[key] };
      try { localStorage.setItem('publish_center_collapsed_v1', JSON.stringify(next)); } catch { /* 隐私模式写不进不影响功能 */ }
      return next;
    });
  };

  // 待确认 item（mhz 返回 203/204/205 confirm code 时挂起的订单项）
  // [2026-04-30 重写] 新版用 awaitingDialogOpen 管理弹窗整体开关，
  // 不再用 activeAwaitingItem（旧版只能看一条）
  const [awaitingItems, setAwaitingItems] = useState<AwaitingItem[]>([]);
  const [awaitingDialogOpen, setAwaitingDialogOpen] = useState(false);
  const [awaitingNotifiedIds, setAwaitingNotifiedIds] = useState<Set<number>>(new Set());
  const awaitingConfirmRequestActiveRef = useRef(false);

  const [isFirst, setIsFirst] = useState(false);
  const [disc, setDisc] = useState(1.0);
  const [agreed, setAgreed] = useState(false);
  const [discOpen, setDiscOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // 代发：外部发布通道
  const [mhzMedia, setMhzMedia] = useState<MhzMediaItem[]>([]);
  const [mhzPage, setMhzPage] = useState(1);
  const [mhzTotal, setMhzTotal] = useState(0);
  const [mhzPages, setMhzPages] = useState(0);
  const [mhzSearch, setMhzSearch] = useState('');
  const [mhzSearchInput, setMhzSearchInput] = useState('');
  const [mhzArea, setMhzArea] = useState('');
  const [mhzResourceType, setMhzResourceType] = useState('');
  const [mhzNewsResource, setMhzNewsResource] = useState('');
  // [CTO-15.23 2026-05-18 v2-F Phase 8] 默认排序改 "GEO 真权威↓" · 让低质媒体自动沉底
  const [mhzSort, setMhzSort] = useState('geo_authority_score_desc');
  const [mhzLoading, setMhzLoading] = useState(false);
  const [mhzFilters, setMhzFilters] = useState<{ areas: string[]; resource_types: string[]; news_resources: string[]; portal_medias: string[]; resource_type_names: string[]; geo_platforms: { code: string; name: string }[]; special_industries: { code: number; name: string }[] }>({ areas: [], resource_types: [], news_resources: [], portal_medias: [], resource_type_names: [], geo_platforms: [], special_industries: [] });
  const [mhzSelMedia, setMhzSelMedia] = useState<Set<number>>(new Set());
  const [mhzMediaMap, setMhzMediaMap] = useState<Map<number, MhzMediaItem>>(new Map());
  const [mhzSubmitting, setMhzSubmitting] = useState(false);
  const [mhzPriceMin, setMhzPriceMin] = useState('');
  const [mhzPriceMax, setMhzPriceMax] = useState('');
  const [mhzPriceRange, setMhzPriceRange] = useState<[number, number]>([0, 0]);
  const [mhzPortalMedia, setMhzPortalMedia] = useState('');
  const [mhzResourceTypeName, setMhzResourceTypeName] = useState('');
  const [mhzGeoPlatform, setMhzGeoPlatform] = useState('');
  const [mhzSpecialIndustry, setMhzSpecialIndustry] = useState(-1);

  /*
   * 🔴 [#199 §1.3] 折叠按钮上那一行的输入。
   *
   *    自核结论(工单要求打印):`mhzTotal` 取自 `/api/meijiehezi/media` 的 `d.total`,
   *    而那次请求把 search / area / resource_type / news_resource / portal_media /
   *    resource_type_name / points_min / points_max / geo_platform / special_industry
   *    **全部**带上了 ⇒ `total` 是**筛选后**的数。
   *    所以文案写「符合 N 家」,改前那句「全量 N 家」是**错的说法**
   *    —— 一个筛过的数被说成"全量",人会以为库里只有这么多。
   *
   * 🔴 排序**永远列出**:它不会因为"没改过"就不影响结果。不列的话,
   *    一个从没点开过高级搜索的人永远不知道这张表是按什么排的。
   */
  const mediaFilterSummary = useMemo(() => {
    const active: [string, string][] = [];
    if (mhzSearch) active.push(['搜索', mhzSearch]);
    if (mhzArea) active.push(['地区', mhzArea]);
    if (mhzResourceType) active.push(['频道', mhzResourceType]);
    if (mhzNewsResource) active.push(['新闻源', mhzNewsResource]);
    if (mhzPortalMedia) active.push(['综合门户', mhzPortalMedia]);
    if (mhzResourceTypeName) active.push(['资源类型', mhzResourceTypeName]);
    if (mhzGeoPlatform) active.push(['GEO 平台', mhzGeoPlatform]);
    if (mhzSpecialIndustry >= 0) active.push(['特别行业', mhzSpecialIndustry === 1 ? '仅特别行业' : '不含特别行业']);
    if (mhzPriceRange[0] > 0 || mhzPriceRange[1] > 0) {
      active.push(['价格', `${mhzPriceRange[0] || 0}–${mhzPriceRange[1] || '不限'}`]);
    }
    return { sort: mhzSort, activeFilters: active, total: mhzTotal || 0, totalIsFiltered: true };
  }, [mhzSearch, mhzArea, mhzResourceType, mhzNewsResource, mhzPortalMedia,
    mhzResourceTypeName, mhzGeoPlatform, mhzSpecialIndustry, mhzPriceRange, mhzSort, mhzTotal]);

  // 代发：自媒体推广
  const [wmMedia, setWmMedia] = useState<any[]>([]);
  const [wmPage, setWmPage] = useState(1);
  /**
   * [#181 2026-09-12] 筛选变更序号。每改一次筛选就 +1;facets 回包带着它发出时的序号
   * 回来,对不上就整包丢弃。
   * 🔴 AbortController 不够:facets 与列表是**两个**请求,列表那个有自己的 controller,
   *    facets 那个此前谁也不管 —— 两次筛选连点时,第一次的 facets 包可能在第二次之后
   *    才回来,把**上一次**的判断按在这一次的状态上。
   */
  const wmEpochRef = useRef(0);
  const mhzEpochRef = useRef(0);
  const [wmTotal, setWmTotal] = useState(0);
  const [wmPages, setWmPages] = useState(0);
  const [wmSearch, setWmSearch] = useState('');
  const [wmSearchInput, setWmSearchInput] = useState('');
  const [wmPlatform, setWmPlatform] = useState('');
  const [wmIndustry, setWmIndustry] = useState('');
  const [wmProvince, setWmProvince] = useState('');
  const [wmSort, setWmSort] = useState('id_asc');
  const [wmLoading, setWmLoading] = useState(false);
  // [WO_PUBLISH_DISPATCH Part② 2026-08-17] 行业筛选从「243 个斜杠组合原始串平铺」
  //   改成「L1 大类 + facet 计数」。服务端按当前其余筛选动态算,0 家的大类不下发,
  //   所以这里的类型是 {key,count}[] 而不是 string[] —— chip 上必须能显示数量,
  //   「选了就是空结果」这件事才在点之前就看得见。
  const [wmFilters, setWmFilters] = useState<{ platforms: string[]; industries: IndustryFacet[]; provinces: string[]; geo_platforms: { code: string; name: string }[] }>({ platforms: [], industries: [], provinces: [], geo_platforms: [] });
  const [wmSelMedia, setWmSelMedia] = useState<Set<number>>(new Set());
  const [wmMediaMap, setWmMediaMap] = useState<Map<number, any>>(new Map());
  const [wmPriceMin, setWmPriceMin] = useState('');
  const [wmPriceMax, setWmPriceMax] = useState('');
  const [wmPriceRange, setWmPriceRange] = useState<[number, number]>([0, 0]);
  const [wmGeoPlatform, setWmGeoPlatform] = useState('');
  const [wmFansRange, setWmFansRange] = useState<[number, number]>([0, 0]);
  const [wmAuthorityMedia, setWmAuthorityMedia] = useState(-1);
  const [wmSubmitting, setWmSubmitting] = useState(false);

  // 购物车
  const [cart, setCart] = useState<CartItem[]>([]);
  // P1.2 (CTO-15.23 2026-05-04) · 移动端默认展开购物车 · 让用户看到内容 + 删除按钮
  const [cartOpen, setCartOpen] = useState(() => {
    if (typeof window === 'undefined') return false;
    return window.innerWidth < 768;
  });
  const [cartSubmitting, setCartSubmitting] = useState(false);
  // [客户反馈② 2026-08-09] 地区备注(非必选)· 只在购物车里出现"只能靠下单备注指定地区"
  //   的媒体时才露出;不命中的媒体这一栏根本不渲染。
  // 🔴 [2026-08-14] 语义改为**只存地区名**(如「深圳」),不再是备注全文。
  //   契约格式 `列举网指定地区xx` 由 buildRegionRemark() 在提交时拼 ——
  //   用户永远不该看见、更不该手打那个前缀(手打就会拼错,479/480 就是这么死的)。
  const [regionRemark, setRegionRemark] = useState('');
  // 🔴 [2026-08-14 · Owner 定的交互] 不做常驻顶部入口 —— 列举网在全网 5 万条活跃媒体里
  //   只有 1 条,为 1/50798 的场景占一条固定栏位性价比不合理。
  //   改为:**加购时命中才弹窗**;购物车里再给一行可点修改的摘要作补救路径。
  const [regionDialogOpen, setRegionDialogOpen] = useState(false);
  const [regionDraft, setRegionDraft] = useState('');
  const [riskConfirmOpen, setRiskConfirmOpen] = useState(false);
  // [2026-07-30 T3] 严审媒体预审拦截载荷。后端一直在发,前端此前只取 message 弹 toast,
  // 于是"既不知道哪里不合格,也没有可点的下一步"。
  const [presubmitBlock, setPresubmitBlock] = useState<StrictPresubmitBlock | null>(null);
  // [WO-BATCH-CONFLICT 2026-08-04] 预检返回的冲突组合清单(整批一次给全)。
  const [conflictBlock, setConflictBlock] = useState<DuplicateOrderBlock | null>(null);
  // [2026-05-28] 风险确认 dialog 打开时 pause MobileCoachMark · 关闭 resume ·
  //   跟 WritingHall 预览同 pattern · 防 MobileCoachMark 挖洞还指原"一键发布"按钮(被 dialog 盖)
  useEffect(() => {
    if (riskConfirmOpen) pauseMobileCoach();
    else resumeMobileCoach();
  }, [riskConfirmOpen]);
  const [activeArticle, setActiveArticle] = useState<ArticleItem | null>(null);
  const [showInsufficientDialog, setShowInsufficientDialog] = useState<{ balance: number; required: number } | null>(null);

  // 任务 4 钩子:监控购物车 0 -> >=1 跳变,自动勾选"加入投放购物车"步骤
  // 首次挂载若 cart 已非空(老代理 localStorage 已有内容),也直接 mark
  // 沙盒态: 跳过此 hook · step 3 完成由 一键发布成功后 + 视频 modal 显式控制
  useEffect(() => {
    if (isSandboxActive()) return;
    if (prevCartLen.current === null) {
      if (cart.length >= 1 && !isStepCompleted('first_article_publish')) {
        markStep('first_article_publish');
      }
      prevCartLen.current = cart.length;
      return;
    }
    if (prevCartLen.current === 0 && cart.length >= 1) {
      markStep('first_article_publish');
    }
    prevCartLen.current = cart.length;
  }, [cart.length, markStep, isStepCompleted]);

  // [#180] 页面级客户下拉已撤 ⇒ 它的"点外关闭"effect 一并删除。
  //   只删 JSX 不删 effect 的话,会留下一个监听 document mousedown 的死 effect。

  // 加载项目 + 首单 + 筛选
  useEffect(() => {
    if (mode === 'history') return;
    const controller = new AbortController();
    authFetch('/api/writing/projects', { signal: controller.signal }).then(r => r.json()).then(d => {
      if (controller.signal.aborted) return;
      const list = d.projects || [];
      setProjects(list);
      if (preGeoPostId) {
        // 🔴 [2026-08-10 P1] 带着**具体内容**跳过来时,绝不许 `list[0]` 兜底接管。
        //    原来这里没有这一支:从创作中心跳来只带 geo_post_id、不带 brand_id,
        //    于是 preBrandId 为空 → 直落 list[0](与本次内容毫不相干的第一个项目)
        //    → 内容被记到别的真实客户名下。生产实证:svideo 3 条里 2 条这么错的。
        //    这里**宁可不选**(留空让用户自己挑),也不替他猜一个 ——
        //    猜错的代价是把 A 的内容发到 B 名下,而"没选"用户一眼就看见。
        //    (真正的兜底在服务端:它按 geo_post_id 反查权威归属并以自己为准。)
        //
        // 🔴 [#180] 这里原本还有一支 `else if (list.length > 0) setSelectedProject(list[0].id)`
        //    —— **已删**。那是"显示对、记错人"的来源:左上角选了 B,页面默默落到 A。
        //    现在找不到对应项目就由 scopeState 给出 no_project 那一格,界面明说。
      }
    }).catch(() => {});
    authFetch('/api/publish/check-first-order', { signal: controller.signal }).then(r => r.json()).then(d => {
      if (controller.signal.aborted) return;
      if (d.status === 'success') { setIsFirst(d.is_first_order); setDisc(d.discount); }
    }).catch(() => {});
    return () => controller.abort();
  }, [mode, preBrandId, preQuoteId]);

  // [CTO-15.23 2026-05-18] 文章发布状态全量刷新(已分发 + 已拒稿 + 详细 stats 一次拉)
  // 抽 helper 让 submit 发布成功后能复用 refetch · 防 stale state 让文章错回"未分发"
  const refetchPublishStatus = useCallback((signal?: AbortSignal) => {
    authFetch('/api/meijiehezi/published-articles', { signal }).then(r => r.json()).then(d => {
      if (signal?.aborted) return;
      if (d.status === 'success') {
        // `article_ids` 保持占位语义(别重复发),核实位单独走两个新集合。
        setPublishedArticleIds(new Set(d.article_ids || []));
        setVerifiedPublishedArticleIds(new Set(d.verified_published_article_ids || []));
        setReportedUnverifiedArticleIds(new Set(d.reported_success_unverified_article_ids || []));
      }
    }).catch(() => {});
    authFetch('/api/meijiehezi/rejected-articles', { signal }).then(r => r.json()).then(d => {
      if (signal?.aborted) return;
      if (d.status === 'success') setRejectedArticleIds(new Set(d.article_ids || []));
    }).catch(() => {});
    // 新接口:详细 status breakdown + 媒体名 · 用于 4 分组 + 卡片标签
    authFetch('/api/meijiehezi/article-publish-stats', { signal }).then(r => r.json()).then(d => {
      if (signal?.aborted) return;
      if (d.status === 'success' && d.stats) {
        const m = new Map<number, ArticlePublishStat>();
        for (const [aid, stat] of Object.entries(d.stats)) {
          m.set(Number(aid), stat as ArticlePublishStat);
        }
        setArticlePublishStats(m);
      }
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (mode === 'history') return;
    const controller = new AbortController();
    refetchPublishStatus(controller.signal);
    return () => controller.abort();
  }, [mode, refetchPublishStatus]);

  /**
   * 🔴 [#180] 深链**反向同步左上角** —— 单独一个 effect,不塞在取项目的 .then 里。
   *
   *    方向很重要:不是页面自己记一个别的客户,而是**让左上角跟着深链走**,
   *    两处永远一致,用户看到的和提交的才是同一个人。
   *    🔴 为什么必须独立:塞在取项目那个 .then 里时,它只在**取项目回来的那一刻**
   *       跑一次,而那会儿 ClientContext 的品牌列表往往还没到,
   *       `switchClient` 立不住(provider 要等列表确认这个品牌仍归属本人)。
   *       独立成 effect 并把 `clients.length` 放进依赖,列表一到就会再判一次。
   */
  useEffect(() => {
    if (mode === 'history') return;
    if (clients.length === 0) return;          // 列表没到,switchClient 立不住
    const key = `${preBrandId || ''}:${preQuoteId || ''}:${preGeoPostId || ''}`;
    if (!preBrandId && !preQuoteId && !preGeoPostId) {
      clientHandoff.current = null;
      return;
    }
    const deepBrand = resolveDeepLinkBrand({
      preBrandId, preQuoteId, projects: projects as unknown as ProjectRef[],
    });
    if (!deepBrand || !clients.some(c => positiveId(c.id) === deepBrand)) return;
    const decision = advanceClientHandoff(clientHandoff.current, key, deepBrand, currentBrandId ?? null);
    clientHandoff.current = decision.state;
    if (decision.action === 'clear') {
      const q = new URLSearchParams(searchParams);
      q.delete('brand_id'); q.delete('quote_id'); q.delete('geo_post_id');
      setSearchParams(q, { replace: true });
    }
    if (decision.action === 'switch' && shouldSwitchClient({ deepLinkBrandId: deepBrand, currentBrandId: currentBrandId ?? null })) {
      switchClient(deepBrand as number);
    }
  }, [mode, preBrandId, preQuoteId, preGeoPostId, projects, clients.length, currentBrandId, switchClient, searchParams, setSearchParams]);

  // 加载文章（支持同一品牌多个报价单合并）
  useEffect(() => {
    if (mode === 'history' || !selectedProject) return;
    const controller = new AbortController();
    const proj = projects.find(p => p.id === selectedProject);
    /*
     * 🔴 [#210 · Owner 09-14 点名 P1] 取的是**当前客户的全部有效报价**,不是选中那一份。
     *
     *    病根:`scopeState` 用 `.find()` 选出"默认那一份",而取文那条 SQL 是
     *    `WHERE t.quote_id = %s` —— 一次只看一份。真客户取证(210-d1):
     *    品牌 17 有两份有效报价,94 下 18 篇、378 下 1 篇;用户打开的是 378,
     *    于是他刚补的和重写的那几篇**一篇都看不见**。文章没丢,丢的是取文的作用域。
     *
     *    `quote_ids` 那个兜底留着:`/api/writing/projects` 目前不回这个字段
     *    (`get_writing_projects` 一份报价一行),但它是既有约定,回来了也该认。
     *    真正兜底到 `[selectedProject]` 的只剩"连 brand_id 都没有"的极端情形。
     */
    const scopeQuoteIds = clientScope.quoteIds.length
      ? clientScope.quoteIds
      : (proj?.quote_ids ?? [selectedProject]);
    const quoteIds: number[] = scopeQuoteIds;
    setLoadingArticles(true);
    Promise.all(quoteIds.map(qid =>
      authFetch(`/api/placement/articles/${qid}`, { signal: controller.signal }).then(r => r.json())
        .then(d => (d.articles ?? d ?? []).map((a: any) => ({ ...a, __quoteId: qid })))
        .catch(() => [])
    )).then(results => {
      if (controller.signal.aborted) return;
      const raw = results.flat();
      const mapped: ArticleItem[] = raw
        .filter((a: any) => a.status === 'completed' || a.article_id)
        .map((a: any) => ({
          id: a.topic_id ?? a.id,
          title: a.title ?? a.optimized_title ?? '',
          keyword: a.keyword ?? '',
          quoteId: a.__quoteId,
          article_style: a.article_style ?? '',
          style_code: a.style_code,
          style_family: a.style_family,
          article_id: a.article_id ?? a.id,
          status: a.status ?? 'completed',
          is_optimize: a.is_optimize ?? false,
          article_review_status: a.article_review_status,
          article_human_review_status: a.article_human_review_status,
          publication_eligible: a.publication_eligible === true || (isSandboxActive() && a.publication_eligible !== false),
          publication_eligibility_reason: a.publication_eligibility_reason,
          publication_eligibility_message: a.publication_eligibility_message,
          review_state: a.review_state,
          advisory_state: a.advisory_state,
          advisory_open_count: a.advisory_open_count,
          publication_h0_state: a.publication_h0_state,
        }));
      setArticles(mapped);
      if (preArticleId) {
        // 重发模式:保留全部文章列表,自动选中目标文章并展开"已拒稿"分组
        // (列表保留是为了用户能看到上下文 + 自由切换其他文章)
        const match = mapped.find(a => a.article_id === preArticleId || a.id === preArticleId);
        if (match) {
          if (match.publication_eligible) {
            setSelectedIds(new Set([match.id]));
            setActiveArticle(match);
          } else {
            lazyToast.error(match.publication_eligibility_message || '该文章尚未通过发布审核');
          }
          // [WO-PUBCENTER-LAYOUT 2026-08-04 · L2] 原来是 setRejectedGroupOpen(true) 展开拒稿分组;
          // 四段堆叠改成分段筛选器后,"展开那一组"的等价动作就是"把筛选切到那一组"。
          // 不改这里的话:带 article_id 进来 → 目标文章在已拒稿里 → 筛选却停在未分发,
          // 用户看不到刚被选中的那篇(下面 isTarget 的 scrollIntoView 也就无从触发)。
          setArticleStatusFilter('rejected');
        }
      }
      if (!preArticleId && preselected.length > 0) {
        setSelectedIds(new Set(preselected.filter(id => mapped.some(a => a.publication_eligible && (a.id === id || a.article_id === id)))));
      }
    }).finally(() => { if (!controller.signal.aborted) setLoadingArticles(false); });
    return () => controller.abort();
  }, [mode, selectedProject, projects, preArticleId, clientScope.quoteIds]);

  // ===== 沙盒态: 进 publish 时清空购物车 / 选中态 · 让用户从 0 开始一篇篇手动加 =====
  // spotlight 引导第一轮 (选文章 → 选媒体 → 加入购物车), 之后由用户复用流程。
  // 草稿和加入购物车不做质量硬拦；法律目录只在真正外发边界定位并提供局部修复。
  const tutorialStage = useTutorialStage();
  const sandboxActive = isSandboxActive();
  const sandboxPublishGuide = getSandboxPublishGuide(tutorialStage, user);
  const showManualGovernance = canShowSandboxManualGovernance(sandboxActive, user);
  const sandboxPublishSetupRef = useRef(false);
  useEffect(() => {
    if (sandboxActive && !showManualGovernance && mode === 'manual') {
      setMode('proxy');
    }
  }, [mode, sandboxActive, showManualGovernance]);

  useEffect(() => {
    if (!sandboxActive || sandboxPublishSetupRef.current) return;
    if (!selectedProject) return;
    // step 4 补发: 进发布页时直接把 2 篇补发文配好媒介加进购物车 · 只引导一键发布
    if (tutorialStage === 'step4-opt-batch') {
      const optArts = articles.filter(a => a.is_optimize);
      if (optArts.length === 0) return; // 补发文还没加载好, 等下一轮
      sandboxPublishSetupRef.current = true;
      const OPT_MEDIA: Record<number, { id: number; name: string; price: number }> = {
        50001: { id: 50001, name: '搜狐号·汽车出行频道', price: 320 },
        50002: { id: 50002, name: '百家号·汽车出行原创号', price: 350 },
      };
      const optCart: CartItem[] = optArts.map(art => {
        const aid = art.article_id || art.id;
        const m = OPT_MEDIA[aid];
        return {
          articleId: aid,
          articleTitle: art.title,
          keyword: art.keyword || '',
          mediaList: m ? [{ id: m.id, name: m.name, costPoints: Math.round(m.price * 10), costYuan: m.price, mediaType: 'mhz' as const }] : [],
        };
      });
      setCart(optCart);
      setCartOpen(true);
      return;
    }
    if (articles.length < 6) return;
    sandboxPublishSetupRef.current = true;
    // 教程进入时强制重置购物车 + 选中态, 让用户从 0 开始
    setCart([]);
    setActiveArticle(null);
    setMhzSelMedia(new Set());
    setMhzMediaMap(new Map());
    setBatchArticleIds(new Set());
    try { localStorage.removeItem(CART_KEY); } catch { /* noop */ }
    // 进 publish 页第 1 个 spotlight: 选文章
    setTutorialStage('step3-pub-pick-article');
  }, [articles, sandboxActive, selectedProject, tutorialStage]);

  // [2026-05-27] 移动端沙盒 · 用户点 checkbox 选了文章 · 但 checkbox stopPropagation 导致
  //   外层 div 的 setActiveArticle + setMobileStep('media') 没触发 · 用户卡在文章列表看不到媒介区
  //   修:监听 batchArticleIds 变化 · 沙盒移动端自动 setActiveArticle + setMobileStep('media')
  useEffect(() => {
    if (!isMobile || !sandboxActive) return;
    if (tutorialStage !== 'step3-pub-pick-article') return;
    if (activeArticle) return; // 已有激活的不重设
    if (batchArticleIds.size === 0) return;
    const firstId = Array.from(batchArticleIds)[0];
    const firstArticle = articles.find(a => a.id === firstId);
    if (firstArticle) {
      setActiveArticle(firstArticle);
      setMobileStep('media');
    }
  }, [isMobile, sandboxActive, batchArticleIds, tutorialStage, articles, activeArticle]);

  // 沙盒 stage 自动推进 · 第 1 篇用户手动加，其余文章自动配媒介，随后进入批量外发。
  const sandboxAutoFillRef = useRef(false);
  useEffect(() => {
    if (!sandboxActive) return;
    // 同一轮内推进
    if (tutorialStage === 'step3-pub-pick-article' && activeArticle) {
      setTutorialStage('step3-pub-pick-media');
    } else if (tutorialStage === 'step3-pub-pick-media' && mhzSelMedia.size > 0) {
      setTutorialStage('step3-pub-add-cart');
    } else if (cart.length >= 6 && tutorialStage !== 'step3-pub-batch-send') {
      setTutorialStage('step3-pub-batch-send');
    }
  }, [sandboxActive, tutorialStage, activeArticle, mhzSelMedia.size, cart.length]);

  // 用户加完第 1 篇 · 自动帮他把剩下的文章与推荐媒介加入购物车。
  useEffect(() => {
    if (!sandboxActive || sandboxAutoFillRef.current) return;
    if (cart.length !== 1) return;
    if (articles.length < 6) return;
    sandboxAutoFillRef.current = true;

    const MEDIA_PAIRING: Record<number, { id: number; name: string; price: number }> = {
      31001: { id: 50001, name: '搜狐号·汽车出行频道', price: 320 },
      31002: { id: 50002, name: '新浪汽车·出行综合频道', price: 520 },
      31003: { id: 50003, name: '凤凰网·商务出行', price: 280 },
      31004: { id: 50004, name: '百家号·汽车出行原创号', price: 350 },
      31005: { id: 50005, name: '今日头条·汽车出行号', price: 380 },
      31006: { id: 50006, name: '腾讯网·新闻中心', price: 850 },
    };

    const userAddedId = cart[0].articleId;
    const remaining = articles.filter(a => (a.article_id || a.id) !== userAddedId);

    // 自动把剩下 5 篇都加进 cart；外发边界会只定位需修的法律表达。
    const newItems: CartItem[] = remaining.map(art => {
      const aid = art.article_id || art.id;
      const m = MEDIA_PAIRING[aid];
      return {
        articleId: aid,
        articleTitle: art.title,
        keyword: art.keyword || '',
        mediaList: m ? [{
          id: m.id,
          name: m.name,
          costPoints: Math.round(m.price * 10),
          costYuan: m.price,
          mediaType: 'mhz' as const,
        }] : [],
      };
    });
    setCart(prev => [...prev, ...newItems]);
    // 1.2s 后单一 toast (给"已加入购物车"toast 时间消失)
    // [2026-05-27] 移动端不弹 toast · MobileCoachMark sheet 已有完整引导 · toast z 高于 sheet 会盖
    window.setTimeout(() => {
      setTutorialStage('step3-pub-batch-send');
      if (!isMobile) {
        lazyToast.info(
          `教程模式为节省时间，已自动帮你把剩下 ${remaining.length} 篇配好渠道并加入清单；接下来点击“模拟发布”`,
          { duration: 9000 },
        );
      }
    }, 1200);
  }, [sandboxActive, cart.length, articles, isMobile]);

  // 沙盒态: 监听 step3 发布完成 · 触发视频占位 modal
  const [sandboxVideoModalOpen, setSandboxVideoModalOpen] = useState(false);
  useEffect(() => {
    const onPublished = () => {
      if (!sandboxActive) return;
      setSandboxVideoModalOpen(true);
    };
    window.addEventListener('sandbox:step3-published', onPublished);
    return () => window.removeEventListener('sandbox:step3-published', onPublished);
  }, [sandboxActive]);

  // 沙盒态由本地成功事件收束；真实媒介审核仅保留在非教程发布链。

  // 推荐媒体 V2（同时加载软文+自媒体，不依赖 proxyType）
  const loadArtRecs = useCallback((signal?: AbortSignal) => {
    const p = projects.find(x => x.id === selectedProject);
    const industry = p?.industry;
    if (!industry) {
      setRecMediaVertical([]); setRecMediaGeneric([]);
      setRecWemediaVertical([]); setRecWemediaGeneric([]);
      setChannelAdvisories([]); setCitationSignal('');
      setCitationTrunk([]); setCombinationPlan(null);
      setBrandRecoFeedback(null); setResearchStatus(null);
      setArtRecIndustry(''); return;
    }
    const q = new URLSearchParams({ limit: '8' });
    q.set('industry', industry);
    // [P1-5b] 带上品牌:后端据此返回该品牌真实被引发布域(D6-B advisory)
    if (p?.brand_id) q.set('brand_id', String(p.brand_id));
    authFetch(`/api/publish/media/recommend-v2?${q}`, { signal }).then(r => r.json()).then(d => {
      if (signal?.aborted) return;
      if (d.status === 'success') {
        const mapRec = (m: RecMediaItem & { id?: number }) => ({ ...m, media_id: m.media_id ?? m.id ?? 0 });
        setRecMediaVertical((d.media_vertical || []).map(mapRec));
        setRecMediaGeneric((d.media_generic || []).map(mapRec));
        setRecWemediaVertical((d.wemedia_vertical || []).map(mapRec));
        setRecWemediaGeneric((d.wemedia_generic || []).map(mapRec));
        setChannelAdvisories(Array.isArray(d.channel_advisories) ? d.channel_advisories : []);
        setCitationSignal(d.citation_ranking_signal || '');
        setCitationTrunk(Array.isArray(d.ai_citation_trunk?.domains) ? d.ai_citation_trunk.domains : []);
        setCombinationPlan(d.combination_plan || null);
        setBrandRecoFeedback(d.brand_reco_feedback || null);
        setResearchStatus(d.research_status && typeof d.research_status === 'object' ? d.research_status : null);
        setArtRecIndustry(d.matched_industry || industry);
      }
    }).catch(() => {});
  }, [selectedProject, projects]);
  useEffect(() => {
    if (mode !== 'proxy' || !selectedProject) return;
    const controller = new AbortController();
    loadArtRecs(controller.signal);
    return () => controller.abort();
  }, [loadArtRecs, mode, selectedProject]);

  const mapRecommendationItem = (item: any) => ({
    media_id: item.media_id,
    media_name: item.media_name,
    platform: item.platform_name || '',
    category: item.tier || '',
    // [D0-b] 后端已投影成售价算力;price_yuan 是售价元(不是进货价),保留无碍
    price_points: item.price_points || item.cost_points || 0,
    our_price_yuan: item.price_yuan || 0,
    our_price_points: item.cost_points || 0,
    inclusion_rate: '',
    engines: [],
    geo_score: item.fit_score || 0,
    reason: Array.isArray(item.evidence) ? item.evidence.join('；') : '',
    evidence: Array.isArray(item.evidence) ? item.evidence : [],
    risk_note: item.risk_note || '',
    fit_score: item.fit_score || 0,
    package_title: item.package_title,
    package_type: item.package_type,
  });

  // AI 推荐为辅助层：默认不抢媒体筛选和浏览，用户需要时再加载。
  // [CTO-15.23 2026-05-18 v2-F Phase 7] 自动触发 AI 推荐 · article_publish_analysis 全期才 8 行
  // 即"AI 推荐"按钮被代理完全忽略 · 1.5s 防抖触发 · 解放代理决策成本
  const autoLoadedArticleIdRef = useRef<number | null>(null);

  const loadDeepAnalysis = useCallback((forceRefresh = false) => {
    if (!activeArticle) return;
    setDeepAnalysisLoading(true);
    const articleId = activeArticle.article_id || activeArticle.id;
    authFetch(`/api/publish/articles/${articleId}/media-recommendation`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        article_title: activeArticle.title,
        article_keyword: activeArticle.keyword || '',
        brand_industry: projects.find(x => x.id === selectedProject)?.industry || '',
        include_wemedia: true,
        force_refresh: forceRefresh,
      }),
    }).then(r => r.json()).then(d => {
      if (d.status === 'success') {
        const summary = d.article_summary || {};
        const packageItems = (d.packages || []).flatMap((pkg: any) =>
          (pkg.items || []).map((item: any) => ({ ...item, package_title: pkg.title, package_type: pkg.package_type }))
        );
        const nextPackages = d.packages || [];
        const defaultPackage = nextPackages.find((pkg: any) => pkg.package_type === 'balanced') || nextPackages[0];
        if (defaultPackage?.package_type) setSelectedPackageType(defaultPackage.package_type);
        setDeepAnalysis({
          article_type_label: summary.article_type_label || '分析',
          article_summary: summary,
          industry: summary.industry || '',
          semantic_keywords: summary.semantic_keywords || [],
          recommended_types: (d.packages || []).map((pkg: any) => pkg.title).filter(Boolean),
          strategy: summary.publish_goal || '已根据文章语义、行业证据和媒体池生成推荐组合。',
          avoid_types: [],
          matched_media: packageItems.filter((item: any) => item.media_source !== 'wemedia').map(mapRecommendationItem),
          matched_wemedia: packageItems.filter((item: any) => item.media_source === 'wemedia').map(mapRecommendationItem),
          recommendation_level: summary.recommendation_level,
          fallback_notice: d.fallback_notice || '',
          packages: nextPackages,
        });
      } else {
        lazyToast.error(d.detail || '推荐生成失败');
      }
    }).catch(() => {
      lazyToast.error('推荐生成请求失败');
    }).finally(() => setDeepAnalysisLoading(false));
  }, [activeArticle, projects, selectedProject]);

  // 切换文章只重置辅助推荐，不自动调用 AI，避免推荐层打断原媒体浏览流程。
  useEffect(() => {
    setDeepAnalysis(null);
    setSelectedPackageType('balanced');
    if (activeArticle && mode === 'proxy') setRecPanelOpen(false);
    // [CTO-15.23 2026-05-18 v2-F Phase 7] 切换文章时清 ref · 让新文章触发一次自动推荐
    autoLoadedArticleIdRef.current = null;
  }, [activeArticle, mode]);

  // [CTO-15.23 2026-05-18 v2-F Phase 7] AI 推荐自动触发 · 老板报"全期才 8 行推荐历史"
  // 选完文章 1.5s 后自动跑 deepAnalysis · 每个 article 只触发 1 次(ref guard 防重复)
  // 用户主动点 refresh / force 仍走 loadDeepAnalysis(true)
  useEffect(() => {
    // [2026-07-05] 老框退役后结果无处展示,不再自动烧后端推荐(手动入口也随框一起退役)。
    if (!LEGACY_MEDIA_REC_PANEL) return;
    if (!activeArticle || !selectedProject || mode !== 'proxy') return;
    if (deepAnalysis || deepAnalysisLoading) return;
    const articleId = activeArticle.article_id || activeArticle.id;
    if (autoLoadedArticleIdRef.current === articleId) return;
    const t = setTimeout(() => {
      // 双重 guard:发起前再确认一次没并发触发
      if (autoLoadedArticleIdRef.current === articleId) return;
      autoLoadedArticleIdRef.current = articleId;
      loadDeepAnalysis(false);
    }, 1500);
    return () => clearTimeout(t);
  }, [activeArticle, selectedProject, mode, deepAnalysis, deepAnalysisLoading, loadDeepAnalysis]);

  // 市场
  const loadM = useCallback((signal?: AbortSignal) => {
    setLoadingM(true);
    const parts = mSort.split('_');
    const dir = parts.pop()!;
    const by = parts.join('_');
    const q = new URLSearchParams({ page: String(mPage), limit: '20', sort_by: by, sort_dir: dir, media_type: mediaType });
    if (mSearch) q.set('search', mSearch);
    if (mCategory) q.set('category', mCategory);
    if (mPlatform) q.set('platform', mPlatform);
    if (mNewsSource) q.set('news_source', mNewsSource);
    if (mGeo) q.set('can_geo', 'true');
    if (mHighValue) q.set('high_value', 'true');
    authFetch(`/api/publish/media?${q}`, { signal }).then(r => r.json()).then(d => {
      if (signal?.aborted) return;
      if (d.status === 'success') { setMarket(d.media || []); setMTotal(d.total || 0); setMPages(d.pages || 0); }
    }).catch(() => {}).finally(() => { if (!signal?.aborted) setLoadingM(false); });
  }, [mPage, mSearch, mCategory, mPlatform, mNewsSource, mGeo, mHighValue, mSort, mediaType]);
  useEffect(() => {
    if (mode !== 'proxy' || !LEGACY_MEDIA_REC_PANEL) return;
    const controller = new AbortController();
    loadM(controller.signal);
    return () => controller.abort();
  }, [loadM, mode]);

  // 切换软文/自媒体时刷新筛选选项
  useEffect(() => {
    if (mode !== 'proxy' || !LEGACY_MEDIA_REC_PANEL) return;
    const controller = new AbortController();
    authFetch(`/api/publish/media/filters?media_type=${mediaType}`, { signal: controller.signal }).then(r => r.json()).then(d => {
      if (controller.signal.aborted) return;
      if (d.status === 'success') setFilterData({
        industries: d.industries || [], regions: d.regions || [],
        platforms: d.platforms || [], news_sources: d.news_sources || [],
      });
    }).catch(() => {});
    setMCategory(''); setMPlatform(''); setMNewsSource(''); setMPage(1);
    return () => controller.abort();
  }, [mediaType, mode]);

  // [D0-b 方案 A] 加价比例的拉取整块撤掉。
  // 展示价改由服务端算成 price_points 直接返回,前端不再需要 markup,
  // /api/meijiehezi/markup 也已收窄成 admin-only —— 这里再拉会 403。

  // 外部发布通道：加载筛选选项
  useEffect(() => {
    if (mode !== 'proxy' || proxyType !== 'article') return;
    const controller = new AbortController();
    // [#181 第 4 条] 与自媒体 tab 对称:陈旧 filters 包一律丢弃(防漂移)
    mhzEpochRef.current += 1;
    const epoch = mhzEpochRef.current;
    authFetch('/api/meijiehezi/media/filters', { signal: controller.signal }).then(r => r.json()).then(d => {
      if (controller.signal.aborted) return;
      if (isStalePacket(epoch, mhzEpochRef.current)) return;
      if (d.status === 'success') setMhzFilters({ areas: d.areas || [], resource_types: d.resource_types || [], news_resources: d.news_resources || [], portal_medias: d.portal_medias || [], resource_type_names: d.resource_type_names || [], geo_platforms: d.geo_platforms || [], special_industries: d.special_industries || [] });
    }).catch(() => {});
    return () => controller.abort();
  }, [mode, proxyType]);

  // 自媒体：加载筛选选项
  // [WO_PUBLISH_DISPATCH Part② 2026-08-17] 行业 chip 是 **facet**,依赖当前其余筛选,
  //   所以这个 effect 不再只在切 tab 时跑一次 —— 切平台/地区/价格/粉丝档都要重算。
  //   🔴 industry 自己**不进**依赖:facet 的分母刻意不含 industry,否则选中一个大类
  //   后其余大类全变 0、chip 全消失,用户就换不回去了。
  useEffect(() => {
    if (mode !== 'proxy' || proxyType !== 'wemedia') return;
    const controller = new AbortController();
    // [#181] 这一次筛选变更的序号。回包对不上就整包丢弃。
    wmEpochRef.current += 1;
    const epoch = wmEpochRef.current;
    const q = new URLSearchParams();
    if (wmSearch) q.set('search', wmSearch);
    if (wmPlatform) q.set('platform', wmPlatform);
    if (wmProvince) q.set('province', wmProvince);
    if (wmPriceRange[0] > 0) q.set('points_min', String(wmPriceRange[0]));
    if (wmPriceRange[1] > 0) q.set('points_max', String(wmPriceRange[1]));
    if (wmGeoPlatform) q.set('geo_platform', wmGeoPlatform);
    if (wmFansRange[0] > 0) q.set('fans_min', String(wmFansRange[0]));
    if (wmFansRange[1] > 0) q.set('fans_max', String(wmFansRange[1]));
    if (wmAuthorityMedia >= 0) q.set('authority_media', String(wmAuthorityMedia));
    const qs = q.toString();
    authFetch(`/api/meijiehezi/wemedia/filters${qs ? `?${qs}` : ''}`, { signal: controller.signal }).then(r => r.json()).then(d => {
      if (controller.signal.aborted) return;
      if (d.status !== 'success') return;
      // [#181] 陈旧包丢弃:两次筛选连点时,第一次的 facets 可能后回来。
      if (isStalePacket(epoch, wmEpochRef.current)) return;
      const industries: IndustryFacet[] = d.industries || [];
      setWmFilters({ platforms: d.platforms || [], industries, provinces: d.provinces || [], geo_platforms: d.geo_platforms || [] });
      // 选中的大类在新条件下已经 0 家(chip 被隐藏)→ 自动退回「不限」。
      // 不做这一步的话,用户会看到一个空列表 + 一个看不见的生效中筛选,无从下手。
      //
      // 🔴 [#181 根因] 这里**只清行业,绝不碰页码**。
      //    老代码顺手 `setWmPage(1)` —— 那是全文件唯一一处在 click handler 之外改页码,
      //    且不受列表请求的 AbortController 约束:用户点「索引」后抢在 facets 回包前
      //    点「下一页」,慢包回来就把他打回第 1 页 —— 客户反馈的"点了下一页还是当前页"。
      //    行业清掉后列表 effect 会因依赖变化重取;当前页若越界,由列表 .then 里
      //    按**回显的 pages** 钳位(见 loadWmMedia)。两件事分开,各有各的判据。
      if (shouldClearIndustry({ selected: wmIndustry, facetKeys: industries.map(x => x.key) })) {
        setWmIndustry('');
      }
    }).catch(() => {});
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, proxyType, wmSearch, wmPlatform, wmProvince, wmPriceRange, wmGeoPlatform, wmFansRange, wmAuthorityMedia]);

  // 外部发布通道：加载媒体列表
  const loadMhzMedia = useCallback((signal?: AbortSignal) => {
    setMhzLoading(true);
    const mhzParts = mhzSort.split('_');
    const sd = mhzParts.pop()!;
    const sb = mhzParts.join('_');
    const q = new URLSearchParams({ page: String(mhzPage), limit: '20', sort_by: sb, sort_dir: sd });
    if (mhzSearch) q.set('search', mhzSearch);
    if (mhzArea) q.set('area', mhzArea);
    if (mhzResourceType) q.set('resource_type', mhzResourceType);
    if (mhzNewsResource) q.set('news_resource', mhzNewsResource);
    if (mhzPortalMedia) q.set('portal_media', mhzPortalMedia);
    if (mhzResourceTypeName) q.set('resource_type_name', mhzResourceTypeName);
    if (mhzPriceRange[0] > 0) q.set('points_min', String(mhzPriceRange[0]));
    if (mhzPriceRange[1] > 0) q.set('points_max', String(mhzPriceRange[1]));
    if (mhzGeoPlatform) q.set('geo_platform', mhzGeoPlatform);
    if (mhzSpecialIndustry >= 0) q.set('special_industry', String(mhzSpecialIndustry));
    const requested = mhzPage;
    authFetch(`/api/meijiehezi/media?${q}`, { signal }).then(r => r.json()).then(d => {
      if (signal?.aborted) return;
      if (d.status === 'success') {
        setMhzMedia(d.media || []); setMhzTotal(d.total || 0);
        // [#181 第 4 条] 软文 tab 对称处理 —— 防漂移(同一个谓词两处,必有一处没人验)
        const st = reconcilePage(requested, d);
        setMhzPages(st.pages);
        if (st.page !== requested) setMhzPage(st.page);
      }
    }).catch(() => {}).finally(() => { if (!signal?.aborted) setMhzLoading(false); });
  }, [mhzPage, mhzSearch, mhzArea, mhzResourceType, mhzNewsResource, mhzSort, mhzPriceRange, mhzPortalMedia, mhzResourceTypeName, mhzGeoPlatform, mhzSpecialIndustry]);
  useEffect(() => {
    if (mode !== 'proxy' || proxyType !== 'article') return;
    const controller = new AbortController();
    loadMhzMedia(controller.signal);
    return () => controller.abort();
  }, [loadMhzMedia, mode, proxyType]);

  // 自媒体：加载媒体列表
  const loadWmMedia = useCallback((signal?: AbortSignal) => {
    setWmLoading(true);
    const wmParts = wmSort.split('_');
    const sd = wmParts.pop()!;
    const sb = wmParts.join('_');
    const q = new URLSearchParams({ page: String(wmPage), limit: '20', sort_by: sb, sort_dir: sd });
    if (wmSearch) q.set('search', wmSearch);
    if (wmPlatform) q.set('platform', wmPlatform);
    if (wmIndustry) q.set('industry', wmIndustry);
    if (wmProvince) q.set('province', wmProvince);
    if (wmPriceRange[0] > 0) q.set('points_min', String(wmPriceRange[0]));
    if (wmPriceRange[1] > 0) q.set('points_max', String(wmPriceRange[1]));
    if (wmGeoPlatform) q.set('geo_platform', wmGeoPlatform);
    if (wmFansRange[0] > 0) q.set('fans_min', String(wmFansRange[0]));
    if (wmFansRange[1] > 0) q.set('fans_max', String(wmFansRange[1]));
    if (wmAuthorityMedia >= 0) q.set('authority_media', String(wmAuthorityMedia));
    const requested = wmPage;
    authFetch(`/api/meijiehezi/wemedia?${q}`, { signal }).then(r => r.json()).then(d => {
      if (signal?.aborted) return;
      if (d.status === 'success') {
        setWmMedia(d.media || []); setWmTotal(d.total || 0);
        /**
         * 🔴 [#181] 用**这一次响应回显的** page/pages 校准,不用旧 state。
         *    切筛选后到回包前 `wmPages` 还是上一次的值:照它放行会请求一个越界页,
         *    后端返空 `media` ⇒ 屏幕看起来"没翻"。
         *    `reconcilePage` 越界时钳到最后一页,并且**不在这里发新请求** ——
         *    页码一变列表 effect 自己会重取。
         */
        const st = reconcilePage(requested, d);
        setWmPages(st.pages);
        if (st.page !== requested) setWmPage(st.page);
      }
    }).catch(() => {}).finally(() => { if (!signal?.aborted) setWmLoading(false); });
  }, [wmPage, wmSearch, wmPlatform, wmIndustry, wmProvince, wmSort, wmPriceRange, wmGeoPlatform, wmFansRange, wmAuthorityMedia]);
  useEffect(() => {
    if (mode !== 'proxy' || proxyType !== 'wemedia') return;
    const controller = new AbortController();
    loadWmMedia(controller.signal);
    return () => controller.abort();
  }, [loadWmMedia, mode, proxyType]);

  // [CTO-15.23 2026-05-05] 切 mode 时 reset mobile step 到 article(否则切到新 mode 仍停留 media 步骤)
  // 同时清批量选 · 防误带到新 mode
  useEffect(() => { setMobileStep('article'); setBatchArticleIds(new Set()); }, [mode]);

  // 待确认 item 轮询：5 秒拉一次，新增的 item 弹 toast 并打开弹框
  const loadAwaitingConfirmations = useCallback(async (signal: AbortSignal) => {
    if (awaitingConfirmRequestActiveRef.current) return { retryAfterMs: 5000 };
    awaitingConfirmRequestActiveRef.current = true;
    try {
      const res = await authFetch('/api/meijiehezi/awaiting-confirmations', { signal });
      if (res.status === 429) {
        const raw = res.headers.get('Retry-After');
        const seconds = raw && /^\d+$/.test(raw) ? Number(raw) : NaN;
        const dateDelay = raw && !Number.isFinite(seconds) ? Date.parse(raw) - Date.now() : NaN;
        return { retryAfterMs: Math.max(1000, Number.isFinite(seconds) ? seconds * 1000 : (Number.isFinite(dateDelay) ? dateDelay : 5000)) };
      }
      if (!res.ok) throw new Error('awaiting confirmations unavailable');
      const data = await res.json();
      if (signal.aborted) return { retryAfterMs: 0 };
      const items: AwaitingItem[] = data?.items || [];
      setAwaitingItems(items);

      setAwaitingNotifiedIds(prev => {
        const next = new Set(prev);
        const fresh = items.filter(it => !prev.has(it.item_id));
        if (fresh.length > 0) {
          lazyToast.warning(`有 ${fresh.length} 篇文章需要您确认是否发布`, {
            description: '点击页面顶部的「待确认」按钮查看详情并修改文章',
            duration: 6000,
          });
          setAwaitingDialogOpen(true);
        }
        for (const it of items) next.add(it.item_id);
        for (const id of Array.from(prev)) {
          if (!items.find(it => it.item_id === id)) next.delete(id);
        }
        return next;
      });
      return { retryAfterMs: 5000 };
    } finally {
      awaitingConfirmRequestActiveRef.current = false;
    }
  }, []);

  useEffect(() => {
    // 教程只练习成功主路径，不轮询或弹出生产媒介审核任务。
    if (mode !== 'proxy' || sandboxActive) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let inFlight: AbortController | null = null;
    let failureCount = 0;

    const schedule = (delay: number) => {
      if (disposed || document.hidden) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(run, delay);
    };
    const run = async () => {
      if (disposed || document.hidden || inFlight) return;
      const activeController = new AbortController();
      inFlight = activeController;
      let nextDelay = 5000;
      try {
        const result = await loadAwaitingConfirmations(activeController.signal);
        failureCount = 0;
        nextDelay = result.retryAfterMs || 5000;
      } catch {
        if (!activeController.signal.aborted) {
          failureCount += 1;
          nextDelay = Math.min(60000, 5000 * (2 ** Math.min(failureCount, 4)));
        }
      } finally {
        if (inFlight === activeController) inFlight = null;
        if (!activeController.signal.aborted) schedule(nextDelay);
      }
    };
    const onVisibility = () => {
      if (document.hidden) {
        if (timer) clearTimeout(timer);
        timer = null;
        inFlight?.abort();
        inFlight = null;
      } else {
        schedule(0);
      }
    };

    document.addEventListener('visibilitychange', onVisibility);
    schedule(0);
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      inFlight?.abort();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [loadAwaitingConfirmations, mode, sandboxActive]);

  // 待确认列表空了(全部处理完)→ 自动关弹窗, 不让用户盯着空的"没有待确认订单"
  useEffect(() => {
    if (awaitingItems.length === 0 && awaitingDialogOpen) setAwaitingDialogOpen(false);
  }, [awaitingItems.length, awaitingDialogOpen]);

  // 操作
  const toggleMedia = (m: MediaItem) => {
    setSelMedia(p => { const n = new Set(p); n.has(m.id) ? n.delete(m.id) : n.add(m.id); return n; });
    setMediaMap(p => { const n = new Map(p); n.set(m.id, m); return n; });
  };
  const toggleMhzMedia = (m: MhzMediaItem) => {
    setMhzSelMedia(p => { const n = new Set(p); n.has(m.id) ? n.delete(m.id) : n.add(m.id); return n; });
    setMhzMediaMap(p => { const n = new Map(p); n.set(m.id, m); return n; });
  };
  const toggleRecMedia = (rec: RecMediaItem) => {
    const id = rec.media_id;
    const isSelected = mhzSelMedia.has(id);
    // 构造 MhzMediaItem 写入 mhzMediaMap（保证购物车能查到价格）
    const asMhz: MhzMediaItem = {
      id,
      media_name: rec.media_name,
      price_points: rec.price_points || rec.our_price_points || 0,
      is_sweet_spot: !!(rec as any).is_sweet_spot,
      area: '', portal_media: '', resource_type_name: rec.category || '',
      resource_type: '', inclusion_rate: parseFloat(rec.inclusion_rate) || 0,
      publish_rate: '', avg_time: 0, pc_weight: 0, m_weight: 0,
      news_resource: 0, link_type: 0, remark: '', case_link: '',
      entrance_level: 0, geo_rank: 0, geo_rank_platform: '',
      weekend_publish: 0, authority_media: 0, special_industry: 0,
      our_price_yuan: rec.our_price_yuan, our_price_points: rec.our_price_points,
    };
    setMhzSelMedia(p => { const n = new Set(p); isSelected ? n.delete(id) : n.add(id); return n; });
    setMhzMediaMap(p => { const n = new Map(p); isSelected ? n.delete(id) : n.set(id, asMhz); return n; });
  };
  const toggleRecWemedia = (rec: RecMediaItem) => {
    const id = rec.media_id;
    const isSelected = wmSelMedia.has(id);
    const asWm = {
      id, toutiao_name: rec.media_name, media_name: rec.media_name,
      platform: rec.platform || '', industry: rec.category || '',
      fans_num: (rec as any).fans_num || 0, price_points: rec.price_points || rec.our_price_points || 0,
      our_price_yuan: rec.our_price_yuan, our_price_points: rec.our_price_points,
    };
    setWmSelMedia(p => { const n = new Set(p); isSelected ? n.delete(id) : n.add(id); return n; });
    setWmMediaMap(p => { const n = new Map(p); isSelected ? n.delete(id) : n.set(id, asWm); return n; });
  };
  const addRecommendationPackage = (pkg: any) => {
    const items = Array.isArray(pkg?.items) ? pkg.items : [];
    if (items.length === 0) return;

    setMhzSelMedia(prev => {
      const next = new Set(prev);
      items.filter((item: any) => item.media_source !== 'wemedia').forEach((item: any) => next.add(Number(item.media_id)));
      return next;
    });
    setMhzMediaMap(prev => {
      const next = new Map(prev);
      items.filter((item: any) => item.media_source !== 'wemedia').forEach((item: any) => {
        const rec = mapRecommendationItem(item);
        next.set(rec.media_id, {
          id: rec.media_id,
          media_name: rec.media_name,
          price_points: rec.price_points || rec.our_price_points || 0,
          area: '', portal_media: '', resource_type_name: rec.category || '',
          resource_type: '', inclusion_rate: parseFloat(rec.inclusion_rate) || 0,
          publish_rate: '', avg_time: 0, pc_weight: 0, m_weight: 0,
          news_resource: 0, link_type: 0, remark: '', case_link: '',
          entrance_level: 0, geo_rank: 0, geo_rank_platform: '',
          weekend_publish: 0, authority_media: 0, special_industry: 0,
          our_price_yuan: rec.our_price_yuan, our_price_points: rec.our_price_points,
        });
      });
      return next;
    });
    setWmSelMedia(prev => {
      const next = new Set(prev);
      items.filter((item: any) => item.media_source === 'wemedia').forEach((item: any) => next.add(Number(item.media_id)));
      return next;
    });
    setWmMediaMap(prev => {
      const next = new Map(prev);
      items.filter((item: any) => item.media_source === 'wemedia').forEach((item: any) => {
        const rec = mapRecommendationItem(item);
        next.set(rec.media_id, {
          id: rec.media_id,
          toutiao_name: rec.media_name,
          media_name: rec.media_name,
          platform: rec.platform || '',
          industry: rec.category || '',
          fans_num: 0,
          price_points: rec.price_points || rec.our_price_points || 0,
          our_price_yuan: rec.our_price_yuan,
          our_price_points: rec.our_price_points,
        });
      });
      return next;
    });
    lazyToast.success(`已加入 ${items.length} 个媒体`);
  };
  const toggleWmMedia = (m: any) => {
    setWmSelMedia(p => { const n = new Set(p); n.has(m.id) ? n.delete(m.id) : n.add(m.id); return n; });
    setWmMediaMap(p => { const n = new Map(p); n.set(m.id, m); return n; });
  };
  const selectPublishArticle = (article: ArticleItem) => {
    if (!article.publication_eligible) {
      lazyToast.error(article.publication_eligibility_message || '该文章尚未通过发布审核');
      return;
    }
    setActiveArticle(article);
    setMobileStep('media');
  };
  const toggleBatchArticle = (article: ArticleItem) => {
    if (!article.publication_eligible) {
      lazyToast.error(article.publication_eligibility_message || '该文章尚未通过发布审核');
      return;
    }
    setBatchArticleIds(prev => {
      const next = new Set(prev);
      if (next.has(article.id)) next.delete(article.id); else next.add(article.id);
      return next;
    });
  };

  // 费用
  const selMediaList = Array.from(selMedia).map(id => mediaMap.get(id)).filter(Boolean) as MediaItem[];
  const artCount = selectedIds.size;
  const medCount = selMediaList.length;
  const pubCount = artCount * medCount;
  const rawPts = selMediaList.reduce((s, m) => s + m.our_price_points, 0) * artCount;
  const totalPts = Math.round(rawPts * disc);
  const totalYuan = Math.round(totalPts / 130);

  // [D0-b 方案 A] 费用直接用后端算好的 price_points,前端不再做乘法。
  // 🔴 顺带修对一处口径:原来是 ceil(Σprice × markup × 130)(**先求和再 ceil**),
  //    而服务端权威口径 _recompute_publish_charge 是**逐条 ceil 再求和**。
  //    N 个媒体最多差 N-1 算力,前端一直显示偏低 —— A-8-1 那条
  //    「客户端报价与服务端权威价不一致」告警多半就是它。改成逐条相加后两边逐位一致。
  const POINTS_PER_YUAN = 130;
  const mhzSelList = Array.from(mhzSelMedia).map(id => mhzMediaMap.get(id)).filter(Boolean) as MhzMediaItem[];
  const mhzTotalPoints = mhzSelList.reduce((s, m) => s + (m.price_points || 0), 0);

  // [媒体平衡 T2] 「建议 vs 已选」实时对照 —— 勾一个媒体这里的数字就要动。
  // 分类关键词来自后端返回的 AI 引用主干表(inventory_keyword),不在前端硬编码平台名单,
  // 否则又变成被废除的 _GENERIC_PLATFORMS 那套。拿不到主干表时整块不渲染。
  const trunkKeywords = useMemo(
    () => citationTrunk.map(t => (t.inventory_keyword || '').trim()).filter(Boolean),
    [citationTrunk],
  );
  const comboProgress = useMemo(() => {
    let big = 0;
    let industry = 0;
    for (const m of mhzSelList) {
      const name = m?.media_name || '';
      if (trunkKeywords.some(kw => name.includes(kw))) big += 1;
      else industry += 1;
    }
    const suggestBig = combinationPlan?.trunk_slots ?? 0;
    const suggestIndustry = combinationPlan?.vertical_slots ?? 0;
    return {
      big, industry, suggestBig, suggestIndustry,
      matched: big >= suggestBig && industry >= suggestIndustry,
      total: mhzSelList.length,
    };
  }, [mhzSelList, trunkKeywords, combinationPlan]);

  // 自媒体费用
  const wmSelList = Array.from(wmSelMedia).map(id => wmMediaMap.get(id)).filter(Boolean) as any[];
  const wmTotalPoints = wmSelList.reduce((s: number, m: any) => s + (m.price_points || 0), 0);

  // 自媒体代发提交
  const handleWmProxy = async () => {
    if (wmSelList.length === 0) { lazyToast.error('请选择至少一个媒体'); return; }
    const firstArt = articles.find(a => selectedIds.has(a.id));
    if (!firstArt) { lazyToast.error('请先选择文章'); return; }
    const curProj = projects.find(p => p.id === selectedProject);
    setWmSubmitting(true);
    // [P0-D 幂等键] 每次点击生成新 UUID — 双击/网络重试都用同一个 ID
    const wmRequestId = safeRandomUUID();
    try {
      const res = await authFetch('/api/meijiehezi/publish', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          article_id: firstArt.article_id || firstArt.id,
          article_title: firstArt.title,
          media_ids: wmSelList.map((m: any) => m.id),
          media_names: wmSelList.map((m: any) => m.toutiao_name || m.media_name),
          cost_points: wmSelList.map((m: any) => m.price_points || 0),
          // [D0-b] cost_yuan 是进货价,不再上送(服务端 A-8-1 起就不信客户端价,只用于观测)
          cost_yuan: [],
          media_type: 'wemedia',
          brand_name: curProj?.brand_name || '',
          brand_id: curProj?.brand_id || null,
          republish_from_sn: republishFromSn || undefined,
          request_id: wmRequestId,
        }),
      });
      const d = await res.json();
      if (d.status === 'success') {
        lazyToast.success(`订单已提交 (${d.total_items} 个媒体)`);
        setWmSelMedia(new Set());
        setWmMediaMap(new Map());
      } else {
        const err = d.detail_contract ?? d.detail;
        if (typeof err === 'object' && err !== null) {
          if (err.code === 'INSUFFICIENT_PAID_POINTS') {
            // 2026-05-17 后端 detail 返 available_pay = paid + commission (赠送不可用) · 前端原读 available_paid undefined
            const availPay = err.available_pay ?? err.available_paid ?? 0;
            lazyToast.error('充值算力 / 服务收益不足', {
              description: `需要 ${err.required} 算力 · 当前可用 ${availPay} 算力 · 赠送算力不可用于媒体发布`,
            });
          } else if (err.code === 'INSUFFICIENT_POINTS') {
            const availPay = err.available_paid ?? err.available ?? 0;
            lazyToast.error(`算力不足`, { description: `需要 ${err.required} · 当前可用 ${availPay}` });
          } else if (err.code === 'DUPLICATE_ORDER') {
            lazyToast.error(err.message || '已有进行中的订单，请勿重复提交');
          } else {
            lazyToast.error(err.message || '操作失败');
          }
        } else {
          lazyToast.error(err || d.message || '提交失败');
        }
      }
    } catch { lazyToast.error('网络错误'); } finally { setWmSubmitting(false); }
  };

  // 外部发布通道代发提交
  const handleMhzProxy = async () => {
    if (mhzSelList.length === 0) { lazyToast.error('请选择至少一个媒体'); return; }
    const firstArt = articles.find(a => selectedIds.has(a.id));
    if (!firstArt) { lazyToast.error('请先选择文章'); return; }
    const curProj = projects.find(p => p.id === selectedProject);
    setMhzSubmitting(true);
    // [P0-D 幂等键]
    const mhzRequestId = safeRandomUUID();
    try {
      const res = await authFetch('/api/meijiehezi/publish', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          article_id: firstArt.article_id || firstArt.id,
          article_title: firstArt.title,
          media_ids: mhzSelList.map(m => m.id),
          media_names: mhzSelList.map(m => m.media_name),
          cost_points: mhzSelList.map(m => m.price_points || 0),
          // [D0-b] 同上:不再上送进货价
          cost_yuan: [],
          brand_name: curProj?.brand_name || '',
          brand_id: curProj?.brand_id || null,
          republish_from_sn: republishFromSn || undefined,
          request_id: mhzRequestId,
        }),
      });
      const d = await res.json();
      if (d.status === 'success') {
        lazyToast.success(`订单已提交 (${d.total_items} 个媒体)`);
        setMhzSelMedia(new Set());
        setMhzMediaMap(new Map());
      } else {
        const err = d.detail_contract ?? d.detail;
        if (typeof err === 'object' && err !== null) {
          if (err.code === 'INSUFFICIENT_PAID_POINTS') {
            // 2026-05-17 后端 detail 返 available_pay = paid + commission (赠送不可用) · 前端原读 available_paid undefined
            const availPay = err.available_pay ?? err.available_paid ?? 0;
            lazyToast.error('充值算力 / 服务收益不足', {
              description: `需要 ${err.required} 算力 · 当前可用 ${availPay} 算力 · 赠送算力不可用于媒体发布`,
            });
          } else if (err.code === 'INSUFFICIENT_POINTS') {
            const availPay = err.available_paid ?? err.available ?? 0;
            lazyToast.error(`算力不足`, { description: `需要 ${err.required} · 当前可用 ${availPay}` });
          } else if (err.code === 'DUPLICATE_ORDER') {
            lazyToast.error(err.message || '已有进行中的订单，请勿重复提交');
          } else {
            lazyToast.error(err.message || '操作失败');
          }
        } else {
          lazyToast.error(err || d.message || '提交失败');
        }
      }
    } catch { lazyToast.error('网络错误'); } finally { setMhzSubmitting(false); }
  };

  // [2026-04-30 已删除] handleProxy 函数与对应的 /api/publish/orders/batch 老接口
  // 已废弃 —— 当前所有代发提交统一走 handleCartBatchPublish 调用批量发布接口

  const fmtPts = (n: number) => n >= 10000 ? `${(n / 10000).toFixed(1)}w` : n.toLocaleString();
  const recommendationSourceLabel = (level?: number, notice = '') => {
    if (notice.includes('AI 分析缓存')) return '缓存分析';
    if (notice.includes('历史推荐池')) return '历史推荐';
    if (level === 1) return '实时分析';
    if (level === 2) return '缓存分析';
    return '基础推荐';
  };
  const recommendationPackages = Array.isArray(deepAnalysis?.packages) ? deepAnalysis.packages : [];
  const activeRecPackage = recommendationPackages.find((pkg: any) => pkg.package_type === selectedPackageType) || recommendationPackages[0];
  const activePackageType = activeRecPackage?.package_type || selectedPackageType;
  const showAdvancedMediaSearch = true;

  // 从 localStorage 恢复购物车（切换项目时）
  useEffect(() => {
    setCart(loadCart(selectedProject));
  }, [selectedProject]);

  // 购物车变更时同步 localStorage
  useEffect(() => {
    if (selectedProject) saveCart(selectedProject, cart);
  }, [cart, selectedProject]);

  /* 🔴 切客户要把报价筛选归零:停在上一个客户的报价号上,列表会恒空
     —— 而那看起来和"这个客户没有文章"一模一样。 */
  useEffect(() => { setQuoteFilter(null); }, [currentBrandId]);

  // 已在购物车中的文章 ID 集合
  const cartArticleIds = new Set(cart.map(c => c.articleId));

  // [2026-07-30 T1/T2] 过滤链 + 四分组 + "每道过滤各藏了几篇" 一次算清。
  // 口径与此前逐字一致(articles → preselected → onlyOptimize → 购物车),
  // 唯一的新增是 `hidden` —— 没有它,用户永远无法自己回答"我的文章去哪了"。
  const scope = partitionArticles({
    articles, preselected, onlyOptimize, quoteFilter,
    cartArticleIds, stats: articlePublishStats,
    publishedArticleIds, rejectedArticleIds,
    verifiedPublishedArticleIds, reportedUnverifiedArticleIds,
  });
  const availableArticles = scope.availableArticles;
  // [CTO-15.23 2026-05-13 全量隔离修 v2] 三组分类:已分发 > 已拒稿 > 未分发(优先级从高到低)
  // 老板报"一拒一成功只看到拒" → 原优先级 拒稿 > 分发 让一文多投只要有 1 拒就归"已拒稿"
  // 修后:有任何 status=0/1/2 在分发管道中就归"已分发";全失败才归"已拒稿"(可重发)
  //
  // 后端接口语义(已配套修):
  //   /published-articles = 任一 order status IN (0,1,2) 的文章(分发管道中含发布中)
  //                         防 BUG:status=1 在跑文章不能错标"未分发"导致重复下单
  //   /rejected-articles  = 有 -1/-2/3 但无 status IN (0,1,2) 的文章(自然不与 published 重叠)
  // [CTO-15.23 2026-05-18 老板拍方案 B] 拆 4 分组 + 卡片标签
  // 已发布(任一 status=2)> 发布中(只 0/1)> 已拒稿(只 -1/-2)> 未分发(无 order)
  // articlePublishStats 来自 /api/meijiehezi/article-publish-stats(submit 后 refetch)
  // fallback:stats 还没加载时 · 退到老 published/rejected 集合 · 兼容初始 mount
  const _getStat = (a: ArticleItem) => articlePublishStats.get(a.article_id || a.id);
  const publishedArticles = scope.published;
  const inProgressArticles = scope.inProgress;
  // [R4 §D] 第五桶接进 UI —— 不接的话这些文章不在任何一个 tab 里,等于消失。
  const reportedUnverifiedArticles = scope.reportedUnverified;
  const rejectedArticles = scope.rejected;
  const unpublishedArticles = scope.unpublished;
  // [2026-07-30 T1] 「全选」的候选集 —— 只能是屏幕上那批未分发文章。
  // 原实现读 `articles`(项目全集),于是"未分发 (5)"点一下变"已选 26",
  // 选中的 21 篇用户根本看不见;它们进购物车后是一次性扣费,生产实测
  // 单媒体中位 3900 算力,所以这不是文案问题而是资金面的 P1。
  const selectAllTarget = selectAllUnpublishedTarget(scope);
  // [2026-07-30 T1] 清单里此前已分发过的文章 —— 提交前明示,不阻断(§1.4 实测:
  // 换媒体重投合法且真扣费,后端那道去重只挡"同文同媒")。
  const cartAlreadyDistributed = cartArticlesAlreadyDistributed(cart, {
    stats: articlePublishStats, publishedArticleIds,
    verifiedPublishedArticleIds, reportedUnverifiedArticleIds,
  });
  // [2026-07-30 T3] 有几家被拦媒体真的还在购物车里 —— 有才挂「换成非严审媒体」,
  // 否则那个按钮点了什么也不会发生(§13:出口必须真能走通)。
  const strictSwitchableCount = presubmitBlock
    ? (() => {
        const drop = new Set(blockedMediaIds(presubmitBlock));
        const entry = cart.find(c => c.articleId === presubmitBlock.article_id);
        return entry ? entry.mediaList.filter(m => drop.has(m.id)).length : 0;
      })()
    : 0;

  // 加入购物车（合并软文+自媒体选中项）
  // 沙盒态: 不在这里查敏感词 · 敏感词由发布通道审核环节(handleCartBatchPublish 后)拦下 · 跟真实业务一致
  const addToCart = async () => {
    // [CTO-15.23 2026-05-05] 批量模式:batchArticleIds 有内容 = 多文章共用同组媒体 · 一次加 N 条 cart
    // 单文章模式:沿用 activeArticle(行为同前)
    const articlesToAdd = batchArticleIds.size > 0
      ? articles.filter(a => batchArticleIds.has(a.id))
      : (activeArticle ? [activeArticle] : []);
    if (articlesToAdd.length === 0) { lazyToast.error('请先选择文章'); return; }
    if (isSandboxActive()) {
      const { sandboxAddToCart } = await import('@/sandbox/mockData');
      for (const art of articlesToAdd) {
        sandboxAddToCart(art.article_id || art.id); // 给状态机记账
      }
    }
    const mhzItems: CartMediaItem[] = mhzSelList.map(m => ({
      id: m.id, name: m.media_name, costPoints: m.price_points || 0, costYuan: 0, mediaType: 'mhz' as const,
      // [客户反馈② 2026-08-09] 信号在加购这一刻算(此处才有完整的 remark 字段);
      //   购物车落 localStorage,旧车没有这个字段 → 读侧再按 name 兜一次。
      needsRegionRemark: mediaNeedsRegionRemark(m),
    }));
    const wmItems: CartMediaItem[] = wmSelList.map((m: any) => ({
      id: m.id, name: m.toutiao_name || m.media_name, costPoints: m.price_points || 0, costYuan: 0, mediaType: 'wemedia' as const,
      needsRegionRemark: mediaNeedsRegionRemark({ media_name: m.toutiao_name || m.media_name, remark: m.remark }),
    }));
    const mediaList = [...mhzItems, ...wmItems];
    if (mediaList.length === 0) { lazyToast.error('请选择至少一个媒体'); return; }
    const recommendationPayload = deepAnalysis ? {
      packages: deepAnalysis.packages || [],
      selected_package_type: activePackageType,
      selected_package: activeRecPackage || null,
      fallback_notice: deepAnalysis.fallback_notice || '',
    } : {};
    const evidencePayload = deepAnalysis ? {
      article_summary: deepAnalysis.article_summary || {},
      industry: deepAnalysis.industry || '',
      semantic_keywords: deepAnalysis.semantic_keywords || [],
      recommendation_level: deepAnalysis.recommendation_level || 3,
    } : {};
    // 跳过购物车里已存在该 articleId 的(防重复加)
    setCart(prev => {
      const existingIds = new Set(prev.map(c => c.articleId));
      const newRecords = articlesToAdd
        .filter(art => !existingIds.has(art.article_id || art.id))
        .map(art => ({
          articleId: art.article_id || art.id,
          articleTitle: art.title,
          keyword: art.keyword || '',
          mediaList,
          recommendationPayload,
          evidencePayload,
          recommendationLevel: deepAnalysis?.recommendation_level || 3,
          analysisVersion: 'publish-rec-v2.3',
        }));
      return [...prev, ...newRecords];
    });
    setActiveArticle(null);
    setBatchArticleIds(new Set());
    setMhzSelMedia(new Set());
    setMhzMediaMap(new Map());
    setWmSelMedia(new Set());
    setWmMediaMap(new Map());
    if (articlesToAdd.length > 1) {
      lazyToast.success(`已批量加入购物车 (${articlesToAdd.length} 篇 × ${mediaList.length} 媒体)`);
    } else {
      lazyToast.success('已加入购物车');
    }
    // 🔴 [2026-08-14] 命中列举网 → 就地弹窗问地区,不做常驻入口。
    //   已经填过就不再打扰(购物车摘要行可随时改)。
    if (REGION_REMARK_ENABLED && mediaList.some(m => m.needsRegionRemark) && !regionRemark.trim()) {
      setRegionDraft('');
      setRegionDialogOpen(true);
    }
  };

  // 从购物车删除
  const removeFromCart = (articleId: number) => {
    setCart(prev => prev.filter(c => c.articleId !== articleId));
  };

  // 购物车统计
  const cartTotalArticles = cart.length;
  // [客户反馈② 2026-08-09] 购物车里"只能靠下单备注指定地区"的媒体(去重后的名字)。
  //   空数组 = 这一栏不渲染 —— 验收②「不命中的媒体无此输入」靠的就是这个。
  const regionRemarkMediaNames = useMemo(() => Array.from(new Set(
    cart.flatMap(c => c.mediaList.filter(cartMediaNeedsRegionRemark).map(m => m.name))
  )), [cart]);
  const cartTotalMedia = cart.reduce((s, c) => s + c.mediaList.length, 0);
  const cartTotalPoints = cart.reduce((s, c) => s + c.mediaList.reduce((ss, m) => ss + m.costPoints, 0), 0);
  const cartUniqueMediaCount = new Set(cart.flatMap(c => c.mediaList.map(m => `${m.mediaType}-${m.id}`))).size;

  const newClientRequestId = (prefix: string) => {
    return `${prefix}-${safeRandomUUID()}`;
  };

  const buildCartMediaSnapshots = () => {
    const mhz = new Map<string, Record<string, any>>();
    const wemedia = new Map<string, Record<string, any>>();
    cart.forEach(c => {
      c.mediaList.forEach(m => {
        const item = {
          article_id: c.articleId,
          article_title: c.articleTitle,
          media_id: m.id,
          media_name: m.name,
          cost_points: m.costPoints,
          cost_yuan: m.costYuan,
          media_type: m.mediaType,
        };
        const key = `${m.mediaType}-${m.id}-${c.articleId}`;
        if (m.mediaType === 'wemedia') wemedia.set(key, item);
        else mhz.set(key, item);
      });
    });
    return {
      selected_media: Array.from(mhz.values()),
      selected_wemedia: Array.from(wemedia.values()),
    };
  };

  const buildDecisionSnapshotPayload = () => {
    const curProj = projects.find(p => p.id === selectedProject);
    const brandId = curProj?.brand_id || preBrandId || selectedProject || 0;
    const quoteId = preQuoteId || curProj?.quote_ids?.[0] || selectedProject || undefined;
    const levels = cart.map(c => c.recommendationLevel || 3);
    const { selected_media, selected_wemedia } = buildCartMediaSnapshots();
    return {
      brand_id: brandId,
      quote_id: quoteId,
      article_ids: cart.map(c => c.articleId),
      recommendation_payload: {
        cart_items: cart.map(c => ({
          article_id: c.articleId,
          article_title: c.articleTitle,
          keyword: c.keyword,
          media_count: c.mediaList.length,
          recommendation: c.recommendationPayload || {},
        })),
      },
      evidence_payload: {
        cart_items: cart.map(c => ({
          article_id: c.articleId,
          article_title: c.articleTitle,
          evidence: c.evidencePayload || {},
        })),
      },
      selected_media,
      selected_wemedia,
      estimated_points: cartTotalPoints,
      disclaimer_version: 'publish-risk-v1',
      terms_version: '2026-05-08',
      analysis_version: cart.find(c => c.analysisVersion)?.analysisVersion || 'publish-rec-v2.3',
      recommendation_level: levels.length > 0 ? Math.min(...levels) : 3,
      request_id: newClientRequestId('publish-risk'),
    };
  };

  // 一键发布：先弹风险确认；真正发布由确认弹窗触发。
  const handleCartBatchPublish = () => {
    if (cart.length === 0) { lazyToast.error('购物车为空'); return; }
    setRiskConfirmOpen(true);
  };

  const submitCartBatchPublish = async () => {
    if (cart.length === 0) { lazyToast.error('购物车为空'); return; }
    setCartSubmitting(true);
    let publishRequestId = '';
    try {
      const snapshotRes = await authFetch('/api/publish/decision-snapshots', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildDecisionSnapshotPayload()),
      });
      const snapshotData = await snapshotRes.json().catch(() => ({}));
      if (!snapshotRes.ok || snapshotData.status !== 'success' || !snapshotData.snapshot_id) {
        const msg = (snapshotData.detail_contract ?? snapshotData.detail) || snapshotData.message || `状态码 ${snapshotRes.status}`;
        lazyToast.error(`投放确认保存失败，已停止发布。\n${msg}`);
        lazyToast.error('投放确认保存失败，未继续发布');
        return;
      }
      publishRequestId = String(snapshotData.publish_request_id || snapshotData.snapshot_id);

      const res = await authFetch('/api/meijiehezi/publish/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          items: cart.map(c => ({
            article_id: c.articleId,
            article_title: c.articleTitle,
            media_ids: c.mediaList.map(m => m.id),
            media_names: c.mediaList.map(m => m.name),
            cost_points: c.mediaList.map(m => m.costPoints),
            cost_yuan: c.mediaList.map(m => m.costYuan),
            media_types: c.mediaList.map(m => m.mediaType),
            // [客户反馈② 2026-08-09] 只有真含"需要地区备注"媒体的那几篇才带备注上送;
            //   其余篇逐位不变(后端空备注 → 请求体不加键)。
            // 🔴 [2026-08-14] 拼**契约格式**再上送:`列举网指定地区{地区名}`。
            //   regionRemark 里只有地区名,前缀由 buildRegionRemark 加 ——
            //   479/480 死在"自由文本不在枚举里",所以格式绝不交给用户手打。
            //   后端仍有两道闸(白名单 fail-fast + 按媒体逐条判定),前端拼错也发不出去。
            order_remark: REGION_REMARK_ENABLED && c.mediaList.some(cartMediaNeedsRegionRemark)
              ? buildRegionRemark(regionRemark)
              : '',
          })),
          request_id: publishRequestId,
        }),
      });
      const d = await res.json();
      if (d.status === 'success') {
        lazyToast.success(d.message || `已提交 ${d.total_articles} 篇文章`);
        setCart([]);
        setCartOpen(false);
        setRiskConfirmOpen(false);
        localStorage.removeItem(CART_KEY);
        if (isSandboxActive()) {
          if (tutorialStage === 'step4-opt-batch') {
            // step 4 补发发布完成 → 标记坏词恢复达标 → 回监测页看效果(in-app 导航保留内存态)
            const { sandboxMarkOptimizeRecovered } = await import('@/sandbox/mockData');
            sandboxMarkOptimizeRecovered();
            setTutorialStage('step4-recovered');
            lazyToast.success('补发文章已提交 · 又过了几天, 回监测页看看效果 (教程模式)', { duration: 3500 });
            window.setTimeout(() => navigate('/monitoring'), 600);
          } else {
            // 沙盒态由本地成功事件直接打开结果说明，不创建或等待媒介审核任务。
            setTutorialStage('step3-pub-batch-send');
          }
        } else {
          // [CTO-15.23 2026-05-18 老板拍方案 B] 提交成功后立刻 refetch 状态
          // 防 stale state 让刚提交的文章错回"未分发"分组 · 应该跳到"发布中"
          refetchPublishStatus();
        }
      } else if (d.code === 'INSUFFICIENT_PAID_POINTS' || d.code === 'INSUFFICIENT_POINTS') {
        if (sandboxActive) {
          lazyToast.error('教程模拟发布状态异常，请返回后重新开始本步骤。');
        } else {
          setShowInsufficientDialog({ balance: d.available_paid || d.balance || 0, required: d.required || cartTotalPoints });
        }
      } else {
        const detail = d.detail_contract ?? d.detail;
        // [2026-07-30 T3] 严审预审拦截 → 升级为可交互面板(逐媒体原因 + 后端给的三个动作)。
        // 拦在扣费之前,所以购物车原样保留:用户修完/换档后直接重提交,不用从头再选一遍。
        const block = parseStrictPresubmitBlock(detail);
        if (block) {
          setPresubmitBlock(block);
          setRiskConfirmOpen(false);
          setCartOpen(false);
          return;
        }
        // [WO-BATCH-CONFLICT 2026-08-04] 冲突不再是一个 toast:留在确认弹窗里开处理窗口,
        //   列出全部冲突组合 + 给一键剔除。购物车原样保留(预检拦在扣费前,没花钱),
        //   剔除后算力由 cartTotalPoints 从购物车派生自动重算。
        const conflict = parseDuplicateOrderBlock(detail);
        if (conflict) {
          setConflictBlock(conflict);
          return;   // 保持 riskConfirmOpen,窗口就地变成处理窗口
        }
        if (detail && typeof detail === 'object' && detail.code === 'DUPLICATE_ORDER') {
          lazyToast.error(detail.message || '这批里有组合无法提交');
        } else {
          lazyToast.error((typeof detail === 'string' ? detail : detail?.message) || d.message || '提交失败');
        }
      }
    } catch { lazyToast.error('网络错误'); } finally { setCartSubmitting(false); }
  };

  return (
    // [WO-PUBCENTER-LAYOUT 2026-08-04 · L1] 高度不再自己算。
    //
    // 旧值 `min-h-[calc(100dvh-7rem)] … md:h-[calc(100dvh-7rem)]` 把"顶部导航 + 外边距"
    // 写死成 7rem=112px。生产实测(1366×768 与 1249×881 两档)Header 实际 h-12=48px、
    // 外壳给本页的 <main> 是 100dvh-48px —— 本页比它矮 **64px**,露出的就是外壳的
    // bg-card,也就是老板说的"底部还有一部分黑色卡片留白"。差值与视口高度无关,恒 64px。
    //
    // 换成 `h-full`:高度完全由外壳的 <main> 决定(Layout.tsx 已把 /publish 归进
    // isFullHeightPage → SidebarInset 定高 + <main> flex-1 min-h-0)。
    // 谁也不再猜头部高度,Header 以后改高改矮这里都不用跟着改。
    //
    // `overflow-hidden` 去掉 `md:` 前缀:窄屏以前只有 `min-h-`(下限)没有上限,
    // 父链高度不确定 → 子元素的 overflow-y-auto 不裁剪 → 容器被内容撑开(真溢出)。
    // 现在窄屏同样定高 + 裁剪,滚动一律由内部列表自己负责。
    // `pb-24` 一并去掉:它是给底部固定条留的位,但 MobileTabBar 全仓零引用,
    // 购物车条是本容器内 sticky bottom-0 的在流元素,不需要额外让位。
    <div className="flex h-full min-h-0 max-w-full flex-col overflow-hidden p-2 sm:p-4 md:p-6">
      {/* CTO-15.20 桥接 banner */}
      {!(mode === 'proxy' && imageNoteMode) && <BridgeBanner />}
      {/* [XO-03 · R3-P7 ③] §12.3 那批字段必须在主按钮上方同屏可见。
          终态/已执行的 intent 不再显示确认区 —— §12.1:同一 intent 任一时刻
          只能有一个可用确认 CTA,抽屉那边已经有了。 */}
      <XiaobangPrefillRegion
        prefill={xiaobangPrefill}
        error={xiaobangPrefillError}
        onBackToAssistant={() => navigate('/dashboard')}
      />
      {/* [§13 出口可达性 2026-07-26] "挂起等你确认"的发布任务 + 可点的合同动作。
          放在发布中心最顶部：合同自己的 view_orders 动作、以及出口开启时发的站内信
          都指向这里，用户点进来第一眼就能看到，不用再翻。没有待办时组件自渲染为 null。 */}
      {!sandboxActive && <PendingUserActions className="mb-3" />}
      {/* [2026-07-30 T3] 严审媒体被拦 —— 面板放最顶部,提交失败后第一眼就能看到。
          🔴 这里没有、也不许有"人工审核放行 / 忽略继续投放":这道门拦的不是
          "我们觉得不合规",而是"这家历史拒稿率高,现在发大概率白花钱"。 */}
      {presubmitBlock && (
        <StrictPresubmitPanel
          className="mb-3"
          block={presubmitBlock}
          canSwitchMedia={strictSwitchableCount > 0}
          onAction={(action) => {
            if (action.id === 'switch_to_non_strict_media') {
              const drop = blockedMediaIds(presubmitBlock);
              setCart(prev => applyStrictMediaSwitch(prev, presubmitBlock.article_id, drop));
              setPresubmitBlock(null);
              lazyToast.success(`已移除被拦的 ${drop.length} 家严审媒体，其余媒体可正常投放`);
              return true;
            }
            // AI 修复 / 手动改都在写作大厅(span 级修复卡片与编辑器都在那)。
            // 目前没有"直达某篇文章"的路由参数,只能带项目 —— 这条缺口已记 backlog。
            navigate(`/writing?quote_id=${selectedProject || ''}`);
            return true;
          }}
        />
      )}
      {sandboxActive && (
        <section
          data-testid="sandbox-publish-guide"
          data-audience={sandboxPublishGuide.audience}
          className="mb-3 border-y border-emerald-500/30 bg-emerald-500/10 px-3 py-3 sm:px-4"
        >
          <div className="flex items-start gap-3">
            <ShieldCheck className="mt-0.5 size-5 shrink-0 text-emerald-500" />
            <div className="min-w-0 flex-1">
              <div className="font-semibold text-foreground">{sandboxPublishGuide.title}</div>
              <p className="mt-1 text-sm leading-6 text-muted-foreground">
                {sandboxPublishGuide.description}
              </p>
              <p className="mt-1 text-sm font-medium text-emerald-600 dark:text-emerald-300">
                下一步：{sandboxPublishGuide.nextAction}
              </p>
            </div>
          </div>
          <ol className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
            {sandboxPublishGuide.steps.map((step, index) => {
              const number = index + 1;
              const active = number === sandboxPublishGuide.activeStep;
              const complete = number < sandboxPublishGuide.activeStep;
              return (
                <li
                  key={step}
                  className={cn(
                    'flex min-h-10 items-center gap-2 border px-2.5 py-2 text-xs',
                    active
                      ? 'border-emerald-500/60 bg-emerald-500/15 font-semibold text-foreground'
                      : 'border-border bg-background/40 text-muted-foreground',
                  )}
                >
                  <span className={cn(
                    'inline-flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold',
                    active || complete
                      ? 'bg-emerald-500 text-slate-950'
                      : 'bg-muted text-muted-foreground',
                  )}>
                    {complete ? <Check className="size-3" /> : number}
                  </span>
                  <span>{step}</span>
                </li>
              );
            })}
          </ol>
        </section>
      )}
      {/* 顶部 */}
      <div className="shrink-0 border-b border-border pb-3 flex flex-col items-stretch gap-3 sm:flex-row sm:items-center">
        <h1 className="text-xl font-bold sm:text-lg">发布中心</h1>
        {/* [WP6 收尾 2026-08-17] 与发布记录同一口径标注:本页的计数是发布尝试,
            合同口径的"已发布在线"只在门户的六阶段元组里给。 */}
        {!(mode === 'proxy' && imageNoteMode) && <span data-testid="publish-center-basis"
              className="ml-2 rounded bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground">
          计数按发布尝试(未按合同去重)
        </span>}
        <div className="flex max-w-full flex-wrap gap-1 rounded-lg bg-secondary p-1 sm:p-0.5 lg:flex-nowrap lg:overflow-x-auto">
          <button onClick={() => setMode('proxy')}
            className={`inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium transition-colors sm:px-4 sm:py-1.5 ${mode === 'proxy' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}>
            <DollarSign className="size-3.5" /> 代发
          </button>
          <button onClick={() => setMode('history')}
            className={`inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium transition-colors sm:px-4 sm:py-1.5 ${mode === 'history' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}>
            <History className="size-3.5" /> <span className="sm:hidden">记录</span><span className="hidden sm:inline">发布记录</span>
          </button>
          {SHOW_ADVISOR_PLACEMENT_TABS && (<>
          <button onClick={() => setMode('advisor')}
            className={`inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium transition-colors sm:px-4 sm:py-1.5 ${mode === 'advisor' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}>
            <Brain className="size-3.5" /> <span className="sm:hidden">参谋</span><span className="hidden sm:inline">发布参谋</span>
          </button>
          <button onClick={() => setMode('placement')}
            className={`inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium transition-colors sm:px-4 sm:py-1.5 ${mode === 'placement' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}>
            <Target className="size-3.5" /> <span className="sm:hidden">投放</span><span className="hidden sm:inline">投放管理中心</span>
          </button>
          </>)}
          {showManualGovernance && <button onClick={() => setMode('manual')}
            data-testid="publish-manual-governance-tab"
            className={`inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium transition-colors sm:px-4 sm:py-1.5 ${mode === 'manual' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}>
            <CheckCircle2 className="size-3.5" /> <span className="sm:hidden">确认</span><span className="hidden sm:inline">手动确认</span>
          </button>}
        </div>
        {mode === 'proxy' && !imageNoteMode && (
          activeResearchTask ? (
            // [FIX-1] 关弹窗后的常驻"进行中"指示(验收①):点击重开弹窗看进度。完成/失败由父层后台轮询 toast。
            <button
              type="button"
              onClick={() => {
                const ind = (projects.find(p => p.id === selectedProject)?.industry || artRecIndustry || '').trim();
                if (ind) setResearchDialogIndustry(ind);
                setResearchDialogOpen(true);
              }}
              className="inline-flex w-fit shrink-0 items-center gap-1.5 rounded-full border border-emerald-500/50 bg-emerald-500/15 px-3 py-1.5 text-xs font-medium text-emerald-200 transition-colors hover:bg-emerald-500/25"
              title="点击查看本行业调研进度"
            >
              <Loader2 className="size-3.5 animate-spin" />
              本行业调研进行中 · {SELFRES_STATUS_LABEL[activeResearchTask.status] || '进行中'}
            </button>
          ) : (
            <button
              type="button"
              onClick={() => {
                const ind = (projects.find(p => p.id === selectedProject)?.industry || artRecIndustry || '').trim();
                if (!ind) {
                  lazyToast.error('当前客户缺少行业信息');
                  return;
                }
                setResearchDialogIndustry(ind);
                setResearchDialogOpen(true);
              }}
              className="inline-flex w-fit shrink-0 items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-xs font-medium text-emerald-300 transition-colors hover:bg-emerald-500/20 hover:text-emerald-200"
            >
              <Sparkles className="size-3.5" />
              点亮调研
            </button>
          )
        )}
        {/* [WO_267] recommend-v2 回 not_open ⇒ 在调研入口旁明说,不再匹到邻居行业。
            🔴 不放推荐面板里:那块由 LEGACY_MEDIA_REC_PANEL(=false)整块关着,放进去等于永远看不见 ——
               第一版就放在那里,渲染臂 P1 抓出来的。 */}
        {mode === 'proxy' && !imageNoteMode && researchStatus?.status === 'not_open' && (
          <span className="shrink-0 text-xs text-amber-600" data-testid="research-not-open">该行业的调研尚未开通</span>
        )}
        {!sandboxActive && awaitingItems.length > 0 && (
          <button
            onClick={() => setAwaitingDialogOpen(true)}
            className="inline-flex w-fit items-center gap-1.5 rounded-full border border-amber-400/60 bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-900 hover:bg-amber-100 sm:ml-2 dark:bg-amber-950/40 dark:text-amber-200 dark:hover:bg-amber-900/40"
            title="点击查看全部待确认列表"
          >
            <span className="size-1.5 rounded-full bg-amber-500 animate-pulse" />
            待确认 ({awaitingItems.length})
          </button>
        )}
        <span className="ml-auto hidden text-xs text-muted-foreground lg:block">
          {mode === 'proxy' ? '帮你发，花算力'
            : mode === 'history' ? '查看历史发布记录'
            : mode === 'advisor' ? 'GEO 调研 & 数据洞察'
            : mode === 'manual' ? '平台外发稿 · 补录证据'
            : '按品牌规划投放 + 白标报价'}
        </span>
      </div>

      {/* [CTO-15.23 2026-05-05] B1 全屏分步流 sm 步骤指示器 · md+ 隐藏(双面板并排不需要)
          [svideo lane · 2026-07-04] 短视频子 tab 无"选文章"步骤(交付=先传视频)，步骤条隐藏 */}
      {flowLayout.articlePane && (
        <div className="md:hidden shrink-0 flex items-center gap-2 px-1 py-2 border-b border-border">
          {mobileStep === 'media' && (
            <button
              type="button"
              onClick={() => setMobileStep('article')}
              className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs font-medium text-foreground hover:bg-secondary"
            >
              <ChevronLeft className="size-3.5" /> 返回选文章
            </button>
          )}
          <div className="flex-1 flex items-center gap-2 text-xs text-muted-foreground">
            <span className={cn(
              "inline-flex items-center justify-center size-5 rounded-full text-[10px] font-bold",
              mobileStep === 'article' ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground"
            )}>1</span>
            <span className={mobileStep === 'article' ? "text-foreground font-medium" : ""}>选文章</span>
            <ChevronRight className="size-3 text-muted-foreground" />
            <span className={cn(
              "inline-flex items-center justify-center size-5 rounded-full text-[10px] font-bold",
              mobileStep === 'media' ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground"
            )}>2</span>
            <span className={mobileStep === 'media' ? "text-foreground font-medium" : ""}>选媒体</span>
          </div>
          {mobileStep === 'article' && (
            mode === 'proxy' && activeArticle
          ) && (
            <button
              type="button"
              onClick={() => setMobileStep('media')}
              className="inline-flex items-center gap-1 rounded-md bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground hover:bg-primary/90"
            >
              下一步 <ChevronRight className="size-3.5" />
            </button>
          )}
        </div>
      )}

      {/* 沙盒教程进度横幅 · 6 篇齐了 + stage 推到 batch-send 后显示 */}
      {sandboxActive && cart.length >= 6 && tutorialStage === 'step3-pub-batch-send' && (
        <div className="mb-2 border-y border-emerald-500/35 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-700 dark:text-emerald-200">
          <div className="font-semibold">发布清单已准备好 6/6 篇</div>
          <div className="mt-0.5 text-emerald-600/90 dark:text-emerald-300">
            点击底部“模拟发布”完成演练；教程不会外发、扣算力或创建审批任务。
          </div>
        </div>
      )}

      <div className="flex min-h-0 w-full min-w-0 flex-1 flex-col gap-3 overflow-visible md:flex-row md:gap-0 md:overflow-hidden">
        {/* ===== 左侧:文章 =====
            [svideo lane · 2026-07-04] 短视频子 tab 不需要文章列表(交付=先传视频不是选文章)，
            隐藏左面板让上传向导+账号市场占满全宽，避免大姐困惑"发视频为什么要先选文章" */}
        {flowLayout.articlePane && (
        // [CTO-15.23 2026-05-05] B1 全屏分步流:sm 下 step=article 才显 · md+ 永远显
        // 老板反馈"两面板挤压拥挤" · 整屏聚焦 1 件事:文章列表能看到 5-7 篇而不是 1-2 篇
        <div ref={leftPaneRef} data-left-pane="" className={cn(
          "w-full min-w-0 shrink-0 rounded-lg border border-border md:w-72 md:rounded-none md:border-y-0 md:border-l-0 md:border-r lg:w-80 flex-col overflow-hidden md:flex md:max-h-full",
          mobileStep === 'article' ? "flex flex-1" : "hidden"
        )}>
          <div className="p-3 border-b border-border space-y-2">
            <div className="flex items-center justify-between gap-2">
              {/* [CTO-15.23 2026-05-17 老板报"PC 端选择文章标题冗余"]
                  PC 端用户能直接看到下方项目选择 + 文章列表 · 不需要"选择文章"标题
                  移动端因 layout step 切换需要指明区域作用 · 保留 */}
              <div className="text-sm font-medium md:hidden">选择文章</div>
              {/* [CTO-15.23 2026-05-05] proxy 模式批量发布:全选/反选 · 选完点"加入购物车"批量生成 N 条 */}
              {mode === 'proxy' && articles.length > 0 && (
                <button
                  type="button"
                  data-testid="proxy-select-all"
                  onClick={() => {
                    setBatchArticleIds(prev => toggleSelectAllUnpublished(prev, selectAllTarget));
                  }}
                  className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground md:ml-auto"
                >
                  <Checkbox checked={isSelectAllChecked(batchArticleIds, selectAllTarget)} className="size-3.5 pointer-events-none" />
                  <span>{batchArticleIds.size > 0 ? `已选 ${batchArticleIds.size}` : selectAllTarget.label}</span>
                </button>
              )}
            </div>
            {/* 🔴 [#180 2026-09-12] 页面级客户下拉**整块已撤**(原 :3413-3495,
                抄自 Quote/OnlineQuoteFlow.tsx 的可搜索 combobox)。
                Owner 09-12 图 3:「这两个选择是完全割裂的,应该统一客户选择入口;
                这里直接参考左上角就行,还能省空间」。
                原位只留一行**只读**的当前客户;不一致的可能性就此消失。 */}
            <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="publish-client-scope">
              {clientScope.kind === 'ready' ? (
                <span className="text-muted-foreground" data-testid="publish-current-client">
                  当前客户:<span className="text-foreground font-medium">{clientContext?.brand?.name || ''}</span>
                  {clientContext?.brand?.industry
                    ? <span className="ml-1 text-muted-foreground/80">· {clientContext.brand.industry}</span>
                    : null}
                </span>
              ) : (
                <>
                  <span className="text-amber-600" data-testid="publish-scope-hint">{clientScope.message}</span>
                  {/* 「全部客户」/ 还没选客户:给一个**迷你选择**,复用左上角同一份 clients。
                      🔴 选中即 `switchClient` —— 它改的是左上角那一份状态,
                         不是页面自己再记一份(那正是本单要消灭的第二份真相)。 */}
                  {(clientScope.kind === 'all_clients' || clientScope.kind === 'no_client') && clients.length > 0 && (
                    <select
                      data-testid="publish-mini-client-picker"
                      className="h-7 rounded border border-input bg-background px-2 text-xs"
                      value=""
                      onChange={(e) => {
                        const bid = Number(e.target.value);
                        if (bid > 0) switchClient(bid);
                      }}
                    >
                      <option value="">选择客户…</option>
                      {clients.map(c => (
                        <option key={c.id} value={c.id}>{c.name}</option>
                      ))}
                    </select>
                  )}
                  {clientScope.kind === 'no_project' && (
                    <a className="underline text-xs" data-testid="publish-goto-writing" href="/writing">
                      去 AI 写文章
                    </a>
                  )}
                </>
              )}
            </div>
          </div>
          {loadingArticles ? (
            <div className="flex-1 flex justify-center py-12"><Loader2 className="size-5 animate-spin text-muted-foreground" /></div>
          ) : articles.length === 0 ? (
            <div className="flex-1 p-4 text-center text-sm text-muted-foreground">{selectedProject ? '暂无已完成文章' : '请先选择项目'}</div>
          ) : mode === 'proxy' ? (<>
            {/* [2026-07-30 T2] "我的文章去哪了" —— 上游三道过滤器全是静默的,
                没有这一行,用户只能来问人。购物车那部分不可由「显示全部」清除。 */}
            <HiddenArticlesNotice
              hidden={scope.hidden}
              onShowAll={() => {
                if (preselected.length > 0) {
                  const next = new URLSearchParams(searchParams);
                  next.delete('articles');
                  setSearchParams(next, { replace: true });
                }
                if (onlyOptimize) setOnlyOptimize(false);
              }}
            />
            {/* [WO-PUBCENTER-LAYOUT 2026-08-04 · L2] 四状态分段筛选器。
                旧做法四段各占一截垂直空间(flex 3/2/2/2 + minHeight 120/180/180/180px):
                三组同时非空时机械下限就是 660px,加四个标题行和底下的媒体建议 ≈ 760px+,
                而 13 寸笔记本(1366×768)可用高度只有 700px 上下 —— 谁都只露一条缝,
                这就是老板说的"一大堆内容全部挤在一起,争抢本来就不多的上下位置"。
                现在四个状态只占顶上这一行,计数常驻(0 也显示 · 2026-07-30 T2 不回退,
                而且比 T2 更强:四个数同时可见,不用滚动也不用展开),下面的列表吃满剩余高度。 */}
            {/*
              * 🔴 [#210] 这个客户有不止一份报价时给一条筛选 —— 但**默认「全部」**。
              *    Owner 09-14 报的是"补的文章没传进发布中心":真相是页面一次只取
              *    被选中那一份报价的文章,另一份下的 18 篇看不见。
              *    所以默认必须是全部;按单份看是**收窄**,由用户主动点。
              *    不用原生 select(#201 正在清那个),用与本页其它筛选一致的芯片。
              */}
            {clientScope.quoteIds.length > 1 && (
              <div className="mx-2 mt-2 flex items-center gap-1.5 overflow-x-auto"
                data-testid="publish-quote-filter">
                <span className="text-[11px] text-muted-foreground shrink-0">报价:</span>
                <button type="button" data-testid="publish-quote-chip" data-quote="all"
                  onClick={() => setQuoteFilter(null)}
                  className={cn('shrink-0 rounded px-2 py-0.5 text-[11px] transition-colors',
                    quoteFilter === null ? 'bg-primary text-primary-foreground'
                      : 'text-muted-foreground hover:text-foreground')}>
                  全部({clientScope.quoteIds.length} 个报价)
                </button>
                {clientScope.quoteIds.map(qid => (
                  <button key={qid} type="button" data-testid="publish-quote-chip" data-quote={String(qid)}
                    onClick={() => setQuoteFilter(qid)}
                    className={cn('shrink-0 rounded px-2 py-0.5 text-[11px] transition-colors',
                      quoteFilter === qid ? 'bg-primary text-primary-foreground'
                        : 'text-muted-foreground hover:text-foreground')}>
                    #{qid}
                  </button>
                ))}
              </div>
            )}
            <ArticleStatusFilter
              className="mx-2 mt-2"
              active={articleStatusFilter}
              onChange={setArticleStatusFilter}
              options={[
                { key: 'unpublished', label: '未分发', count: unpublishedArticles.length },
                {
                  key: 'inProgress',
                  label: '发布中',
                  count: inProgressArticles.length,
                  tone: 'progress',
                  icon: inProgressArticles.length > 0 ? <Loader2 className="size-3 animate-spin" /> : undefined,
                },
                { key: 'published', label: '已分发', count: publishedArticles.length },
                {
                  key: 'reportedUnverified',
                  label: '待核实',
                  count: reportedUnverifiedArticles.length,
                  tone: 'pending',
                },
                { key: 'rejected', label: '已拒稿', count: rejectedArticles.length, tone: 'danger' },
              ]}
            />
            {/* 单列表容器:同一时刻只渲染选中状态那一组,flex-1 min-h-0 吃满剩余高度。
                🔴 这里不许再出现 minHeight。2026-05-05 那次"加 minHeight 防挤压"就是补丁,
                百分比 flex + 硬下限在小容器里必然打架 —— 补丁没解根因,所以症状复发。 */}
            <div
              data-article-list-pane=""
              data-active-status={articleStatusFilter}
              className="flex min-h-0 flex-1 flex-col overflow-y-auto p-2 space-y-1"
            >
              {articleStatusFilter === 'unpublished' && (
                unpublishedArticles.length === 0 ? (
                  <div className="text-center text-xs text-muted-foreground py-4">全部已分发</div>
                ) : unpublishedArticles.map((a, articleIdx) => {
                  const articleRowEl = (
                  <div key={a.id} onClick={() => selectPublishArticle(a)}
                    className={cn('flex items-start gap-2 px-2 py-2 rounded-md transition-colors', !a.publication_eligible && 'opacity-60 cursor-not-allowed', a.publication_eligible && 'cursor-pointer',
                      activeArticle?.id === a.id ? 'bg-primary/10 border border-primary/40' : 'hover:bg-secondary/50')}>
                    {/* [CTO-15.23 2026-05-14] 复选框 click area 扩 44x44(WCAG)· 老板报"小圈圈难点 · 移动端尤其" */}
                    <div
                      role="checkbox"
                      aria-checked={batchArticleIds.has(a.id)}
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleBatchArticle(a);
                      }}
                      className="flex items-center justify-center shrink-0 -my-2 -ml-2 px-3 py-2 cursor-pointer hover:bg-primary/5 rounded-l-md"
                    >
                      <Checkbox
                        checked={batchArticleIds.has(a.id)}
                        className="size-4 pointer-events-none"
                      />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="text-sm leading-snug line-clamp-2 break-words" title={a.title}>{a.title}</div>
                      <div className="flex gap-1.5 mt-1">
                        {(a.style_family || a.style_code || a.article_style) && <Badge variant="secondary" className="text-[10px] px-1 py-0">{humanizeArticleStyle(a.style_family || a.style_code || a.article_style)}</Badge>}
                        <PublicationReviewBadge article={a} onOpenAdvisory={openAdvisoryFor} />
                        {a.is_optimize && (
                          <Badge className="bg-orange-500/15 text-orange-400 border border-orange-500/30 text-[10px] px-1 py-0">优化</Badge>
                        )}
                        <span className="text-[10px] text-muted-foreground">{a.keyword}</span>
                        <ArticleQuoteTag quoteId={a.quoteId} show={quoteTagVisible} />
                      </div>
                      <ArticlePublishBadges stat={_getStat(a)} />
                    </div>
                  </div>
                  );
                  // 沙盒: 仅第 1 篇引导 (后面由教程自动加 + 最后 1 篇敏感词手动加)
                  // [2026-05-27] 接受 step3-go-publish 也弹 · 防 useEffect 切 stage 时序差让引导漏掉
                  if (isSandboxActive() && articleIdx === 0 && (tutorialStage === 'step3-pub-pick-article' || tutorialStage === 'step3-go-publish') && cart.length === 0) {
                    return (
                      <FeatureTooltip
                        key={`sandbox-art-${a.id}`}
                        featureId="sandbox_pub_pick_article"
                        stepId="first_article_publish"
                        title="第一步: 选第 1 篇文章"
                        content={isMobile
                          ? '点击文章卡片 → 继续选择这篇文章推荐的代发媒介\n教程会让你完整体验 1 次"选文章→选媒体→加购物车", 剩下的会自动帮你加'
                          : '点击文章行 → 右侧会显示该篇 AI 推荐的代发媒介\n教程会让你完整体验 1 次"选文章→选媒体→加购物车", 剩下的会自动帮你加'}
                        side="right"
                        wrapClassName="relative block"
                      >
                        {articleRowEl}
                      </FeatureTooltip>
                    );
                  }
                  return articleRowEl;
                })
              )}
              {/* [CTO-15.23 2026-05-18 老板拍方案 B] 发布中 · 优先级高于已分发(用户刚提交最关心进度)。
                  空组的"本项目暂无发布中的文章"照样出(2026-07-30 T2:空即不渲染会让老板
                  以为"历史记录不见了"),计数则由上面的分段控件常驻显示。 */}
              {articleStatusFilter === 'inProgress' && (
                inProgressArticles.length === 0 ? (
                  <div data-article-list-empty="inProgress" className="text-center text-xs text-muted-foreground py-4">本项目暂无发布中的文章</div>
                ) : inProgressArticles.map(a => (
                    <div key={a.id} onClick={() => selectPublishArticle(a)}
                      className={cn('flex items-start gap-2 px-2 py-1.5 rounded-md cursor-pointer transition-colors',
                        activeArticle?.id === a.id ? 'bg-blue-500/15 border border-blue-500/40' : 'border border-transparent hover:bg-blue-500/5 hover:border-blue-500/20')}>
                      <div
                        role="checkbox"
                        aria-checked={batchArticleIds.has(a.id)}
                        onClick={(e) => {
                          e.stopPropagation();
                          toggleBatchArticle(a);
                        }}
                        className="flex items-center justify-center shrink-0 -my-1.5 -ml-2 px-3 py-2 cursor-pointer hover:bg-primary/5 rounded-l-md"
                      >
                        <Checkbox checked={batchArticleIds.has(a.id)} className="size-4 pointer-events-none" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="text-xs leading-snug line-clamp-2 break-words" title={a.title}>{a.title}</div>
                        <div className="flex gap-1.5 mt-0.5">
                          <span className="text-[10px] text-muted-foreground">{a.keyword}</span>
                          <ArticleQuoteTag quoteId={a.quoteId} show={quoteTagVisible} />
                        </div>
                        <ArticlePublishBadges stat={_getStat(a)} />
                      </div>
                    </div>
                  ))
              )}
              {/* 已分发 · P1.3 (CTO-15.23 2026-05-04)。原来占 40% 高度 + minHeight 180px,
                  现在不再分走固定比例 —— 选中它时它就吃满整个列表区。 */}
              {articleStatusFilter === 'published' && (
                publishedArticles.length === 0 ? (
                  <div data-article-list-empty="published" className="text-center text-xs text-muted-foreground py-4">本项目暂无已分发文章</div>
                ) : publishedArticles.map(a => (
                    <div key={a.id} onClick={() => selectPublishArticle(a)}
                      className={cn('flex items-start gap-2 px-2 py-1.5 rounded-md cursor-pointer transition-colors opacity-60',
                        activeArticle?.id === a.id ? 'bg-primary/10 border border-primary/40 opacity-100' : 'hover:bg-secondary/50')}>
                      {/* [CTO-15.23 2026-05-14] 复选框 click area 扩大 · 跟未分发一致 */}
                      <div
                        role="checkbox"
                        aria-checked={batchArticleIds.has(a.id)}
                        onClick={(e) => {
                          e.stopPropagation();
                          toggleBatchArticle(a);
                        }}
                        className="flex items-center justify-center shrink-0 -my-1.5 -ml-2 px-3 py-2 cursor-pointer hover:bg-primary/5 rounded-l-md"
                      >
                        <Checkbox
                          checked={batchArticleIds.has(a.id)}
                          className="size-4 pointer-events-none"
                        />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="text-xs leading-snug line-clamp-2 break-words" title={a.title}>{a.title}</div>
                        <div className="flex gap-1.5 mt-0.5">
                          <span className="text-[10px] text-muted-foreground">{a.keyword}</span>
                          <ArticleQuoteTag quoteId={a.quoteId} show={quoteTagVisible} />
                        </div>
                        <ArticlePublishBadges stat={_getStat(a)} />
                      </div>
                    </div>
                  ))
              )}
              {/* 已拒稿 · 重发跳转(带 republish_from)时 articleStatusFilter 初值直接就是
                  'rejected',下面 isTarget 那行 scrollIntoView 的定位行为原样保留。 */}
              {articleStatusFilter === 'reportedUnverified' && (
                reportedUnverifiedArticles.length === 0 ? (
                  <div data-article-list-empty="reportedUnverified" className="text-center text-xs text-muted-foreground py-4">本项目暂无待核实文章</div>
                ) : reportedUnverifiedArticles.map(a => {
                      const articleKey = a.article_id || a.id;
                      const isTarget = preArticleId && articleKey === preArticleId;
                      return (
                        <div
                          key={a.id}
                          data-article-id={articleKey}
                          ref={el => { if (isTarget && el) el.scrollIntoView({ behavior: 'smooth', block: 'center' }); }}
                          onClick={() => selectPublishArticle(a)}
                          className={cn(
                            'flex items-start gap-2 px-2 py-1.5 rounded-md cursor-pointer transition-colors border',
                            activeArticle?.id === a.id
                              ? 'bg-amber-500/10 border-amber-500/50'
                              : 'border-transparent hover:bg-amber-500/5 hover:border-amber-500/20',
                            isTarget && 'ring-1 ring-amber-400/60'
                          )}
                        >
                          <Checkbox
                            checked={batchArticleIds.has(a.id)}
                            onClick={(e) => {
                              e.stopPropagation();
                              toggleBatchArticle(a);
                            }}
                            className="mt-0.5 size-3.5 shrink-0"
                          />
                          <div className="min-w-0 flex-1">
                            <div className="text-xs leading-snug line-clamp-2 break-words" title={a.title}>{a.title}</div>
                            <div className="flex gap-1.5 mt-0.5 items-center">
                              {/* 文案与后端 PUBLICATION_LABELS 同一句,不另造说法 */}
                              <Badge className="bg-amber-500/15 text-amber-300 border border-amber-500/30 text-[10px] px-1 py-0">浏览器曾回报成功 · 尚未核实</Badge>
                              <span className="text-[10px] text-muted-foreground">{a.keyword}</span>
                              <ArticleQuoteTag quoteId={a.quoteId} show={quoteTagVisible} />
                            </div>
                            <ArticlePublishBadges stat={_getStat(a)} />
                          </div>
                        </div>
                      );
                    })
              )}
              {articleStatusFilter === 'rejected' && (
                rejectedArticles.length === 0 ? (
                  <div data-article-list-empty="rejected" className="text-center text-xs text-muted-foreground py-4">本项目暂无已拒稿文章</div>
                ) : rejectedArticles.map(a => {
                      const articleKey = a.article_id || a.id;
                      const isTarget = preArticleId && articleKey === preArticleId;
                      return (
                        <div
                          key={a.id}
                          data-article-id={articleKey}
                          ref={el => { if (isTarget && el) el.scrollIntoView({ behavior: 'smooth', block: 'center' }); }}
                          onClick={() => selectPublishArticle(a)}
                          className={cn(
                            'flex items-start gap-2 px-2 py-1.5 rounded-md cursor-pointer transition-colors border',
                            activeArticle?.id === a.id
                              ? 'bg-red-500/10 border-red-500/50'
                              : 'border-transparent hover:bg-red-500/5 hover:border-red-500/20',
                            isTarget && 'ring-1 ring-red-400/60'
                          )}
                        >
                          <Checkbox
                            checked={batchArticleIds.has(a.id)}
                            onClick={(e) => {
                              e.stopPropagation();
                              toggleBatchArticle(a);
                            }}
                            className="mt-0.5 size-3.5 shrink-0"
                          />
                          <div className="min-w-0 flex-1">
                            <div className="text-xs leading-snug line-clamp-2 break-words" title={a.title}>{a.title}</div>
                            <div className="flex gap-1.5 mt-0.5 items-center">
                              <Badge className="bg-red-500/15 text-red-400 border border-red-500/30 text-[10px] px-1 py-0">已拒稿</Badge>
                              <span className="text-[10px] text-muted-foreground">{a.keyword}</span>
                              <ArticleQuoteTag quoteId={a.quoteId} show={quoteTagVisible} />
                            </div>
                            <ArticlePublishBadges stat={_getStat(a)} />
                          </div>
                        </div>
                      );
                    })
              )}
            </div>
          </>) : null}
          {mode === 'proxy' && (
            /* [WO-PUBCENTER-LAYOUT 2026-08-04 · L3] 三块辅助信息合成一条底部抽屉。
               原来 T1 / T2 / 媒体榜 各自是一块独立面板竖着摞,收起态就各占一行,
               展开更是把文章列表挤没 —— 生产实测:项目里一篇文章都没有时,
               这三块已经吃满整个左栏。现在收起态只占抽屉把手那一行。

               🔴 **没有移到右栏**。老板明确否掉过:「如果选A，移到右边，右边的可见
               又会变低。小屏幕直接看不到下面的选择。」这里只在左栏内部做纵向折叠。
               🔴 工单 §4 L3 点名的是 T2 + 媒体榜;T1 同类(都是"选媒体时的参考"),
               留它在外面单独占一行的话"收起态只占 1 行"就只是数字达标、
               老板看到的还是好几行 —— 所以一并收进来。
               [媒体平衡 2026-07-29] 这几块必须挂在**现役**面板上:老「推荐媒体」框
               LEGACY_MEDIA_REC_PANEL=false 已于 2026-07-05 整框退役,挂那儿等于永不渲染。 */
            <MediaAdviceDrawer
              open={mediaAdviceOpen}
              onToggle={() => setMediaAdviceOpen(v => !v)}
              storageKey={mediaAdviceStorageKey}
              boundsRef={leftPaneRef}
              sections={[
  /* [媒体平衡 T1] AI 最常引用的网站 —— 搜狐/网易/博客园/知乎才是撑起 AI 答案的
      来源,过去被"垂直优先"的排序盖住了,代理必须先看见它们。
      这一块是**纯展示**:不设 onClick、cursor-default、无 hover、无按钮边框,
      让人一眼看出它不可点(工单验收 6)。 */
  ...(proxyType === 'article' && citationTrunk.length > 0 ? [{ key: 't1', node: (
    <div data-testid="t1-trunk" className="rounded-md border border-border px-3 py-2 space-y-1">
      <button
        type="button"
        onClick={() => toggleCollapsed('t1Trunk')}
        className="w-full text-xs font-medium text-foreground flex items-center justify-between hover:text-primary transition-colors"
      >
        <span>AI 最常引用的网站</span>
        {collapsed.t1Trunk ? <ChevronDown className="size-3" /> : <ChevronUp className="size-3" />}
      </button>
      {!collapsed.t1Trunk && (<>
      <div className="text-xs text-muted-foreground">
        AI 回答问题时，最常从下面这些网站取材。发在这些网站上更容易被 AI 引用。
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 pt-0.5">
        {citationTrunk.map(t => (
          <span key={t.domain} data-testid="t1-badge"
            className="text-xs cursor-default select-none">
            <span className="text-foreground font-medium">{t.domain}</span>
            <span className="text-muted-foreground ml-1">AI 引用 {t.citation_count} 次</span>
            {t.self_serve && (
              <span className="text-muted-foreground ml-1">· 可以自己发</span>
            )}
          </span>
        ))}
      </div>
      </>)}
    </div>
  ) }] : []),

  /* [P1-5b D6-B 2026-08-14] 该品牌真实被引的发布域(账本 body_proof 口径,纯展示 advisory)。
      样本不足时如实显示「暂无足够样本」,不显示空榜假象;比率类数字带
      「已配对子样本」限定(工单红线 6,不许剥离)。 */
  ...(proxyType === 'article' && brandRecoFeedback ? [{ key: 'd6b', node: (
    <div data-testid="d6b-brand-cited" className="rounded-md border border-border px-3 py-2 space-y-1">
      <span className="text-xs font-medium text-foreground">这个品牌已被 AI 引用过的发布位置</span>
      {brandRecoFeedback.available && (brandRecoFeedback.domains?.length ?? 0) > 0 ? (<>
        <div className="text-xs text-muted-foreground">
          按真实监测已配对子样本统计(近 {`90`} 天,不代表全量监测面):这些网站上发的内容已被 AI 引用过本品牌,优先续投。
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 pt-0.5">
          {(brandRecoFeedback.domains || []).map(d => (
            <span key={d.domain} className="text-xs cursor-default select-none">
              <span className="text-foreground font-medium">{d.domain}</span>
              <span className="text-muted-foreground ml-1">被引 {d.citations} 次</span>
            </span>
          ))}
        </div>
      </>) : brandRecoFeedback.reason === 'insufficient_sample' ? (
        /* [R2-6] 小样本诚实态:只报观察值,不给「优先续投」结论;两出口都是真动作 */
        <div className="text-xs text-muted-foreground" data-testid="d6b-insufficient">
          已被 AI 引用 {brandRecoFeedback.citation_total ?? 0}/{brandRecoFeedback.min_sample ?? 30} 次,暂作观察,不构成续投依据。
          出口:继续发布与监测积累样本;或
          <a href="/monitoring" className="ml-1 underline text-foreground">人工查看原始监测记录</a>。
        </div>
      ) : (
        <div className="text-xs text-muted-foreground">暂无足够样本 —— 发布并开启监测后,这里会显示已被 AI 引用过本品牌的位置。</div>
      )}
    </div>
  ) }] : []),

  /* [媒体平衡 T2] 建议组合 + **随勾选联动**的已选对照 */
  ...(proxyType === 'article' && combinationPlan?.reason ? [{ key: 't2', node: (
    <div data-testid="t2-combo" className="rounded-md border border-border px-3 py-2 space-y-1">
      <button
        type="button"
        onClick={() => toggleCollapsed('t2Combo')}
        className="w-full text-xs font-medium text-foreground flex items-center justify-between hover:text-primary transition-colors"
      >
        <span>这篇建议怎么搭配媒体</span>
        {collapsed.t2Combo ? <ChevronDown className="size-3" /> : <ChevronUp className="size-3" />}
      </button>
      {!collapsed.t2Combo && (<>
      <div className="text-xs text-muted-foreground">{combinationPlan.reason}</div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 pt-0.5">
        <span className="text-xs" data-testid="t2-suggest">
          <span className="text-muted-foreground">建议投</span>
          <span className="text-foreground font-medium ml-1">
            大平台 {comboProgress.suggestBig} 个 + 行业网站 {comboProgress.suggestIndustry} 个
          </span>
        </span>
        <span className="text-xs" data-testid="t2-current">
          <span className="text-muted-foreground">你已选</span>
          <span className={cn('font-medium ml-1',
            comboProgress.matched ? 'text-foreground' : 'text-amber-700 dark:text-amber-400')}>
            大平台 {comboProgress.big} 个 + 行业网站 {comboProgress.industry} 个
          </span>
        </span>
      </div>
      {(combinationPlan.substitutions || []).map(s => (
        <div key={`sub-${s.domain}`} className="text-xs text-amber-700 dark:text-amber-400">
          {s.substitution_note}
        </div>
      ))}
      {(combinationPlan.self_serve_offers || []).map(s => (
        <div key={`self-${s.domain}`} className="text-xs text-muted-foreground">
          {s.self_serve_action?.label}
        </div>
      ))}
      </>)}
    </div>
  ) }] : []),

  /* [E2] 本行业 AI 真实引用媒体榜。无数据时组件自渲染为 null,这里不用再挡一层。 */
  { key: 'board', node: (
              <MediaEffectivenessPanel
                industry={projects.find(p => p.id === selectedProject)?.industry || artRecIndustry || ''}
                brandId={projects.find(p => p.id === selectedProject)?.brand_id ?? null}
                activeTab={proxyType === 'wemedia' ? 'wemedia' : 'media'}
                refreshKey={researchRefreshKey}
                onOpenRounds={() => { setRoundReportId(''); setRoundReportOpen(true); }}
                onFindMedia={(name, source, meta) => {
                  if (!name) return;
                  // [§1 点击闭环] 记下这次搜索是从榜点过来的 + 那一行的域名/简介,
                  // 媒体库搜不到时据此给显式空态(见 boardEmptyHint)。
                  setBoardSearchOrigin({
                    term: name,
                    oneLiner: String(meta?.oneLiner || ''),
                    domain: String(meta?.domain || ''),
                  });
                  const target = source || (proxyType === 'wemedia' ? 'wemedia' : 'media');
                  if (target === 'wemedia') {
                    const switched = proxyType !== 'wemedia';
                    if (switched) setProxyType('wemedia');
                    setWmSearchInput(name); setWmSearch(name); setWmPage(1);
                    lazyToast.success(switched ? `该媒体在自媒体推广库,已切换并搜索「${name}」` : `已在媒体列表搜索「${name}」`);
                  } else {
                    const switched = proxyType === 'wemedia';
                    if (switched) setProxyType('article');
                    setMhzSearchInput(name); setMhzSearch(name); setMhzPage(1);
                    lazyToast.success(switched ? `该媒体在软文发布库,已切换并搜索「${name}」` : `已在媒体列表搜索「${name}」`);
                  }
                }}
              />
  ) },
              ]}
            />
          )}
          <div className="p-3 border-t border-border text-xs text-muted-foreground shrink-0">
            {`共 ${availableArticles.length} 篇可选`}
          </div>
        </div>
        )}

        {/* ===== 右侧：代发 ===== */}
        {mode === 'proxy' && (
          <div className={cn(
            "min-h-0 min-w-0 flex-1 flex-col overflow-visible md:flex md:overflow-hidden",
            // [svideo lane · 2026-07-04] 短视频 tab 移动端强制可见：无"选文章"步骤，
            // 否则 mobileStep==='article' 时右容器 hidden，手机上点了短视频却看不到面板
            flowLayout.mediaVisible ? "flex" : "hidden"
          )}>
            {/* 软文/自媒体 切换 */}
            <div className="px-3 sm:px-4 py-2 border-b border-border flex items-center gap-1 shrink-0 overflow-x-auto">
              <button onClick={() => chooseProxyType('article')}
                className={cn('shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-xs font-medium transition-colors', proxyType === 'article' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-secondary')}>
                软文价格
              </button>
              <button onClick={() => chooseProxyType('wemedia')}
                className={cn('shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-xs font-medium transition-colors', proxyType === 'wemedia' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-secondary')}>
                自媒体推广
              </button>
              <button onClick={() => chooseProxyType('svideo')}
                className={cn('shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-xs font-medium transition-colors', proxyType === 'svideo' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-secondary')}>
                短视频
              </button>
                {/* 🔴 [#150 2026-09-08] 「图文笔记」tab **按 Review #150 裁定撤下**。
                    Deploy 09-08 取证:其后端表在生产**根本不存在**(不是「表空」是「无表」)、
                    开关未设、`image-notes` 任意路径 **0 次调用** —— 用户点进去只能得到 500。
                    撤的是产品面;后端去留是另一张卡(WO_150 §8)。 */}
            </div>


            {/* [svideo lane · 2026-07-04] 短视频子 tab：自包含面板（上传视频向导 + 短视频账号市场 + 提交）。
                独立于软文/自媒体，不进共享购物车/快照。交付逻辑不同（先有视频再投），左边是上传向导。 */}
            {/* 图文笔记面板随 tab 一并撤下(#150) */}

            {proxyType === 'svideo' && <div className="flex flex-wrap items-center gap-2 border-b p-3 sm:px-4" aria-label="短视频内容类型">
              <Button variant={imageNoteMode ? 'default' : 'outline'} className="min-h-11" aria-pressed={imageNoteMode}
                data-testid="publish-svideo-imagenote" onClick={() => chooseVideoContent('imagenote')}>已制作图文</Button>
              <Button variant={!imageNoteMode ? 'default' : 'outline'} className="min-h-11" aria-pressed={!imageNoteMode}
                data-testid="publish-svideo-upload" onClick={() => chooseVideoContent('video')}>上传视频</Button>
              <p className="text-sm text-muted-foreground">{imageNoteMode ? '选择已做好的图文，发到抖音账号。' : '上传视频文件，再选择账号投放。'}</p>
            </div>}
            {imageNoteMode && (
              /* The image-note lane reuses the publishing center: finished work
                 on the left, the existing account directory and sole publisher
                 on the right. Creation only hands off the selected work.
                 Client scope stays currentBrandId, never a second scope inferred
                 from selectedProject. */
              <Suspense fallback={<div className="flex flex-1 items-center justify-center text-muted-foreground"><Loader2 className="mr-2 size-5 animate-spin" />加载中…</div>}>
                <ImageNoteList key={currentBrandId ?? 'all'} brandId={currentBrandId ?? null} preGeoPostId={preGeoPostId}
                  onSelectPost={pid => {
                    if (preGeoPostId === pid) return;
                    const q = new URLSearchParams(searchParams);
                    q.set('media_type', 'svideo'); q.set('content_type', 'imagenote');
                    q.set('geo_post_id', String(pid));
                    if (currentBrandId) q.set('brand_id', String(currentBrandId));
                    setSearchParams(q, { replace: true });
                  }}
                  onSelectClient={bid => {
                    const q = new URLSearchParams(searchParams);
                    q.set('brand_id', String(bid));
                    setSearchParams(q, { replace: true });
                    switchClient(bid);
                  }} />
              </Suspense>
            )}

            {proxyType === 'svideo' && !imageNoteMode && (
              <Suspense fallback={<div className="flex flex-1 items-center justify-center text-muted-foreground"><Loader2 className="mr-2 size-5 animate-spin" />加载中…</div>}>
                <ShortVideoPanel
                  brandId={currentBrandId ?? null}
                  brandName={clients.find(c => c.id === currentBrandId)?.name || projects.find(p => p.brand_id === currentBrandId)?.brand_name}
                  industry={projects.find(p => p.id === selectedProject)?.industry}
                  // [共享主人翁制 · GEO 图文 2026-08-03] 从创作中心「去发布投放」跳过来:
                  // /publish?media_type=svideo&geo_post_id=<id> → 面板自己去取图组与文案。
                  // 沿用本页既有的 URL 参数预填约定(brand_id / article_id / mode …),不另造入口。
                  geoPostId={null}
                  // 🔴 [2026-08-10 P1] 面板预填拿到服务端权威归属后回报上来 ——
                  //    **必须接住并真的改选中项目**,否则又是"下发字段中途被丢掉":
                  //    面板显示的是内容真正的客户,提交走的却是这里选中的另一个品牌。
                  //    找不到对应项目就**不动**(宁可不选,也不替用户猜一个)。
                  onGeoBrandResolved={(bid) => {
                    // 🔴 [#180] 服务端说这条内容属于谁,就**把左上角切过去** ——
                    //    老写法只改页面自己那份选择,左上角仍停在别人身上,
                    //    于是又回到"两处不一致"。
                    if (shouldSwitchClient({ deepLinkBrandId: bid, currentBrandId: currentBrandId ?? null })) {
                      switchClient(bid);
                    }
                  }}
                />
              </Suspense>
            )}

            {/* 推荐媒体面板
                [CTO-15.23 2026-05-14] 按 proxyType 切换:article tab 只看软文推荐 · wemedia tab 只看自媒体推荐
                防 article tab 仅有自媒体推荐时面板空 → 用户困惑
                [2026-07-05 老板拍板] 整框退役(LEGACY_MEDIA_REC_PANEL=false),由上方引用媒体榜接替 */}
            {LEGACY_MEDIA_REC_PANEL && activeArticle && (
              (proxyType === 'article' && (recMediaVertical.length > 0 || recMediaGeneric.length > 0)) ||
              (proxyType === 'wemedia' && (recWemediaVertical.length > 0 || recWemediaGeneric.length > 0))
            ) && (
              <div className="shrink-0 border-b-2 border-primary/20 bg-primary/10">
                {/* 标题栏（始终显示） */}
                <div className="flex cursor-pointer flex-col gap-2 px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between sm:px-4" onClick={() => setRecPanelOpen(p => !p)}>
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <Sparkles className="size-4 text-primary" />
                    <span className="text-sm font-medium">推荐媒体</span>
                    {artRecIndustry && <Badge variant="secondary" className="text-[10px] px-1.5 py-0">{artRecIndustry}</Badge>}
                    {/* [CTO-15.23 2026-05-18 v2-F Phase 9] 推荐逻辑 explainer · 老板问"推荐媒体的逻辑是什么" */}
                    <span
                      className="text-[10px] text-muted-foreground/70 cursor-help"
                      title={"推荐逻辑:\n1. 按客户行业查 GEO 调研 17 行业 × 4 AI 引擎的历史来源曝光排名\n2. 平台 score 排序 · 取每平台价格最低且 GEO 引擎覆盖最多的媒体\n3. 行业头部白名单兜底(汽车/装修/教育/医疗/科技 等 14 类)\n4. 90 天历史去重 · 同代理近期发过的媒体不重复推\n注：媒体优劣以 GEO 数据飞轮的真实被引数据为准,不按价格预判。"}
                    >
                      ⓘ 推荐逻辑
                    </span>
                    {!recPanelOpen && <span className="text-[10px] text-muted-foreground">点击展开</span>}
                    {recPanelOpen && !deepAnalysis && <span className="text-[10px] text-muted-foreground">辅助推荐,不影响下方筛选</span>}
                    {recPanelOpen && deepAnalysis && <span className="text-[10px] text-muted-foreground">文章语义辅助</span>}
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {recPanelOpen && deepAnalysis && (
                      <Button variant="outline" size="sm" className="shrink-0 text-xs h-7 px-2 gap-1"
                        disabled={deepAnalysisLoading || !activeArticle}
                        onClick={(e) => { e.stopPropagation(); loadDeepAnalysis(true); }}>
                        {deepAnalysisLoading ? <Loader2 className="size-3 animate-spin" /> : <RefreshCw className="size-3" />}
                        重新分析
                      </Button>
                    )}
                    {recPanelOpen && deepAnalysis && (
                      <Button variant="ghost" size="sm" className="shrink-0 text-xs h-7 px-2 text-muted-foreground"
                        onClick={(e) => { e.stopPropagation(); setDeepAnalysis(null); }}>
                        隐藏分析
                      </Button>
                    )}
                    {recPanelOpen && !deepAnalysis && (
                      <Button variant="outline" size="sm" className="shrink-0 text-xs h-7 px-3 gap-1"
                        disabled={deepAnalysisLoading || !activeArticle}
                        onClick={(e) => { e.stopPropagation(); loadDeepAnalysis(); }}>
                        {deepAnalysisLoading ? <Loader2 className="size-3 animate-spin" /> : <Sparkles className="size-3" />}
                        AI 推荐
                      </Button>
                    )}
                    {recPanelOpen ? <ChevronUp className="size-4 text-muted-foreground" /> : <ChevronDown className="size-4 text-muted-foreground" />}
                  </div>
                </div>
                {/* 内容区（可折叠） */}
                {recPanelOpen && <div className="max-h-[24dvh] space-y-3 overflow-y-auto px-3 pb-3 sm:px-4 md:max-h-[34vh]">

                {/* AI 辅助推荐结果 */}
                {deepAnalysis ? (
                  <div className="space-y-2.5">
                    <div className="px-3 py-2 rounded-lg bg-background/80 border border-primary/20 space-y-2">
                      <div className="flex items-center gap-2 text-[11px] font-medium text-primary">
                        <Brain className="size-3.5" />
                        <span>AI 已读这篇文章</span>
                        <Badge variant="outline" className="text-[10px] px-1.5 py-0 border-primary/30 text-primary">
                          {recommendationSourceLabel(deepAnalysis.recommendation_level, deepAnalysis.fallback_notice || '')}
                        </Badge>
                      </div>
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <Badge className="text-[10px] px-1.5 py-0 bg-primary/15 text-primary border-primary/30">{deepAnalysis.article_type_label || '分析'}</Badge>
                        {deepAnalysis.industry && (
                          <Badge variant="secondary" className="text-[10px] px-1.5 py-0">{deepAnalysis.industry}</Badge>
                        )}
                        {Array.isArray(deepAnalysis.semantic_keywords) && deepAnalysis.semantic_keywords.slice(0, 5).map((kw: string) => (
                          <Badge key={kw} variant="outline" className="text-[10px] px-1.5 py-0">{kw}</Badge>
                        ))}
                      </div>
                      <div className="text-xs leading-relaxed">{deepAnalysis.strategy}</div>
                      {deepAnalysis.fallback_notice && (
                        <div className="text-[10px] text-muted-foreground">{deepAnalysis.fallback_notice}</div>
                      )}
                      {Array.isArray(deepAnalysis.avoid_types) && deepAnalysis.avoid_types.length > 0 && (
                        <div className="text-[10px] text-orange-400">不建议：{deepAnalysis.avoid_types.join('、')}</div>
                      )}
                    </div>
                    {recommendationPackages.length > 0 ? (
                      <div className="space-y-2">
                        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                          <div className="grid grid-cols-3 gap-1 rounded-md border border-border bg-background/80 p-1">
                            {recommendationPackages.map((pkg: any) => {
                              const selected = activePackageType === pkg.package_type;
                              return (
                                <button
                                  key={pkg.package_type}
                                  type="button"
                                  onClick={() => setSelectedPackageType(pkg.package_type)}
                                  className={cn(
                                    'min-w-0 rounded px-2 py-1 text-[11px] font-medium transition-colors',
                                    selected ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-secondary hover:text-foreground'
                                  )}
                                >
                                  <span className="block truncate">{pkg.title}</span>
                                  <span className={cn('block text-[10px]', selected ? 'text-primary-foreground/70' : 'text-muted-foreground')}>
                                    {fmtPts(pkg.estimated_points || 0)} 算力
                                  </span>
                                </button>
                              );
                            })}
                          </div>
                          {activeRecPackage && (
                            <Button
                              type="button"
                              size="sm"
                              className="h-8 shrink-0 gap-1 text-xs"
                              onClick={() => addRecommendationPackage(activeRecPackage)}
                            >
                              <ShoppingCart className="size-3.5" />
                              加入组合
                            </Button>
                          )}
                        </div>
                        {activeRecPackage && (
                          <div className="overflow-hidden rounded-lg border border-border bg-background/70">
                            <div className="flex items-center justify-between gap-2 bg-secondary/50 px-3 py-2 text-xs">
                              <div className="min-w-0">
                                <div className="font-medium break-words leading-snug" title={activeRecPackage.title}>{activeRecPackage.title}</div>
                                {activeRecPackage.reason && (
                                  <div className="text-[10px] text-muted-foreground break-words leading-snug" title={activeRecPackage.reason}>{activeRecPackage.reason}</div>
                                )}
                              </div>
                              <Badge variant="secondary" className="shrink-0 text-[10px] px-1.5 py-0">
                                {(activeRecPackage.items || []).length} 家
                              </Badge>
                            </div>
                            <div className="divide-y divide-border/50">
                              {(activeRecPackage.items || []).map((item: any) => {
                                const rec = mapRecommendationItem(item);
                                const isMhz = item.media_source !== 'wemedia';
                                const selected = isMhz ? mhzSelMedia.has(rec.media_id) : wmSelMedia.has(rec.media_id);
                                const pts = rec.price_points || rec.our_price_points || 0;
                                const score = Number(rec.fit_score || rec.geo_score || 0);
                                const evidence = Array.isArray(rec.evidence) ? rec.evidence.filter(Boolean) : [];
                                return (
                                  <div
                                    key={`${item.media_source || 'media'}-${rec.media_id}-${activePackageType}`}
                                    onClick={() => isMhz ? toggleRecMedia(rec) : toggleRecWemedia(rec)}
                                    className={cn(
                                      'grid cursor-pointer grid-cols-[minmax(0,1fr)_24px] gap-2 px-3 py-2 text-xs transition-colors',
                                      selected ? 'bg-primary/10' : 'hover:bg-secondary/30'
                                    )}
                                  >
                                    <div className="min-w-0 space-y-1">
                                      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                                        <span className="font-medium break-words leading-snug" title={rec.media_name}>{rec.media_name}</span>
                                        <Badge variant="outline" className={cn('text-[9px] px-1 py-0', isMhz ? 'border-blue-500/30 text-blue-400' : 'border-green-500/30 text-green-400')}>
                                          {isMhz ? '软文' : '自媒体'}
                                        </Badge>
                                        {score > 0 && (
                                          <Badge variant="secondary" className="text-[9px] px-1 py-0">{Math.round(score)}分</Badge>
                                        )}
                                        {pts > 0 && <span className="text-[10px] text-muted-foreground">{fmtPts(pts)} 算力</span>}
                                      </div>
                                      {evidence.length > 0 && (
                                        <div className="line-clamp-2 text-[10px] leading-relaxed text-muted-foreground">
                                          {evidence.slice(0, 2).join('；')}
                                        </div>
                                      )}
                                      {rec.risk_note && (
                                        <div className="text-[10px] leading-relaxed text-orange-400">{rec.risk_note}</div>
                                      )}
                                    </div>
                                    <Checkbox checked={selected} onClick={(e) => e.stopPropagation()} onCheckedChange={() => isMhz ? toggleRecMedia(rec) : toggleRecWemedia(rec)} className="mt-1 size-3.5 justify-self-end" />
                                  </div>
                                );
                              })}
                            </div>
                          </div>
                        )}
                      </div>
                    ) : (
                      (Array.isArray(deepAnalysis.matched_media) && deepAnalysis.matched_media.length > 0 ||
                        Array.isArray(deepAnalysis.matched_wemedia) && deepAnalysis.matched_wemedia.length > 0) && (
                        <div className="rounded-lg border border-border overflow-hidden">
                          <div className="grid grid-cols-[minmax(0,1fr)_48px_64px_32px] gap-2 bg-secondary/50 px-3 py-1.5 text-[10px] font-medium text-muted-foreground sm:grid-cols-[1fr_60px_80px_40px]">
                            <span>媒体</span><span>类型</span><span className="text-right">价格</span><span></span>
                          </div>
                          {[...(deepAnalysis.matched_media || []).map((r: any) => ({...r, _type: 'media'})),
                            ...(deepAnalysis.matched_wemedia || []).map((r: any) => ({...r, _type: 'wemedia'}))
                          ].map((rec: any) => {
                            const isMhz = rec._type === 'media';
                            const selected = isMhz ? mhzSelMedia.has(rec.media_id) : wmSelMedia.has(rec.media_id);
                            const pts = rec.price_points || rec.our_price_points || 0;
                            return (
                              <div key={`${rec._type}-${rec.media_id}`}
                                onClick={() => isMhz ? toggleRecMedia(rec) : toggleRecWemedia(rec)}
                                className={cn('grid grid-cols-[minmax(0,1fr)_48px_64px_32px] items-center gap-2 border-t border-border/50 px-3 py-2 text-xs cursor-pointer transition-colors sm:grid-cols-[1fr_60px_80px_40px]',
                                  selected ? 'bg-primary/10' : 'hover:bg-secondary/30')}>
                                <span className="font-medium break-words leading-snug" title={rec.media_name}>{rec.media_name}</span>
                                <Badge variant="outline" className={cn('text-[9px] px-1 py-0 w-fit', isMhz ? 'border-blue-500/30 text-blue-400' : 'border-green-500/30 text-green-400')}>
                                  {isMhz ? '软文' : '自媒体'}
                                </Badge>
                                <span className="text-right text-muted-foreground">{pts > 0 ? `${fmtPts(pts)}算力` : '-'}</span>
                                <Checkbox checked={selected} onClick={(e) => e.stopPropagation()} onCheckedChange={() => isMhz ? toggleRecMedia(rec) : toggleRecWemedia(rec)} className="size-3.5 ml-auto" />
                              </div>
                            );
                          })}
                        </div>
                      )
                    )}
                  </div>
                ) : (<>
                {/* 粗略推荐（深度分析前显示）
                    [CTO-15.23 2026-05-14] 老板报"软文 tab 不应显示自媒体推荐 · 自媒体 tab 不应显示软文推荐"
                    按 proxyType 切换:article 软文 tab 只显软文推荐 · wemedia 自媒体 tab 只显自媒体推荐 */}
                <div className="space-y-2">
                  {/* 软文推荐(仅软文 tab) */}
                  {/* [WP12 P0-1] 渠道建议 —— advisory,每条带下一步,不阻断发布 */}
                  {channelAdvisories.length > 0 && (
                    <div className="space-y-1">
                      {channelAdvisories.map(adv => (
                        <div key={adv.code}
                          className="text-[11px] leading-relaxed rounded-md border border-border bg-muted/40 px-2.5 py-1.5">
                          <span className="font-medium text-foreground">{adv.message}</span>
                          {adv.repair_hint && (
                            <span className="text-muted-foreground"> {adv.repair_hint}</span>
                          )}
                        </div>
                      ))}
                    </div>
                  )}

                  {proxyType === 'article' && (recMediaVertical.length > 0 || recMediaGeneric.length > 0) && (
                    <div className="space-y-1.5">
                      <div className="text-[10px] text-muted-foreground font-medium tracking-wide">
                        软文推荐
                        {citationSignal === 'observed_citation_v2g' && (
                          <span className="ml-1.5 font-normal">按真实被引数据排序</span>
                        )}
                      </div>
                      <div className="flex items-center gap-1.5 flex-wrap">
                        {[...recMediaVertical, ...recMediaGeneric].map(rec => {
                          const selected = mhzSelMedia.has(rec.media_id);
                          const pts = rec.price_points || rec.our_price_points || 0;
                          const isVertical = recMediaVertical.some(v => v.media_id === rec.media_id);
                          const citeLabel = rec.ai_citation_source === 'observed' ? rec.ai_citation_label : '';
                          return (
                            <span key={rec.media_id} onClick={() => toggleRecMedia(rec)}
                              title={rec.reason || ''}
                              className={cn('text-xs px-2.5 py-1 rounded-md cursor-pointer whitespace-nowrap transition-all border inline-flex items-center gap-1.5',
                                selected ? 'bg-primary text-primary-foreground border-primary shadow-sm' : 'bg-background text-foreground border-border hover:border-primary/50')}>
                              {isVertical && <span className="size-1.5 rounded-full bg-primary shrink-0" />}
                              {rec.media_name}
                              {citeLabel && (
                                <span className={cn('text-[10px] px-1 rounded',
                                  selected ? 'text-primary-foreground/80' : 'text-foreground/70 bg-muted')}>
                                  {citeLabel}{(rec.ai_citation_count || 0) > 0 ? ` ${rec.ai_citation_count}` : ''}
                                </span>
                              )}
                              {pts > 0 && <span className={cn('text-[10px]', selected ? 'text-primary-foreground/70' : 'text-muted-foreground')}>{fmtPts(pts)}</span>}
                            </span>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  {/* 自媒体推荐(仅自媒体 tab) */}
                  {proxyType === 'wemedia' && (recWemediaVertical.length > 0 || recWemediaGeneric.length > 0) && (
                    <div className="space-y-1.5">
                      <div className="text-[10px] text-muted-foreground font-medium tracking-wide">自媒体推荐</div>
                      <div className="flex items-center gap-1.5 flex-wrap">
                        {[...recWemediaVertical, ...recWemediaGeneric].map(rec => {
                          const selected = wmSelMedia.has(rec.media_id);
                          const pts = rec.price_points || rec.our_price_points || 0;
                          return (
                            <span key={rec.media_id} onClick={() => toggleRecWemedia(rec)}
                              className={cn('text-xs px-2.5 py-1 rounded-md cursor-pointer whitespace-nowrap transition-all border inline-flex items-center gap-1.5',
                                selected ? 'bg-primary text-primary-foreground border-primary shadow-sm' : 'bg-background text-foreground border-border hover:border-primary/50')}>
                              {rec.media_name}
                              <span className={cn('text-[10px]', selected ? 'text-primary-foreground/70' : 'text-muted-foreground')}>{rec.platform}</span>
                              {pts > 0 && <span className={cn('text-[10px]', selected ? 'text-primary-foreground/70' : 'text-muted-foreground')}>{fmtPts(pts)}</span>}
                            </span>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>
                </>)}
                </div>}
              </div>
            )}

            {/* ===== 软文价格 ===== */}
            {proxyType === 'article' && (<>
            {/* 筛选区域 — 默认收起，点击展开 */}
            <div className="border-b border-border shrink-0 relative">
              {/* [CTO-15.23 2026-05-13 BUG fix] 老板报:筛选展开后底部"加入购物车"按钮消失
                  真因:桌面端原 max-h-[800px] 绝对像素 · 笔记本 768px viewport 装不下
                       筛选 800 + 推荐媒体 + 文章列表 + 分页 + 底部栏 > viewport · 父级
                       md:overflow-hidden 让溢出部分完全不可见
                  修法:桌面端改 md:max-h-[50vh] vh 自适应 · 展开时 overflow-y-auto 内部滚动
                       不再挤父级 flex · 文章列表 / 底部栏保留 viewport 空间 */}
              <div className={cn(
                'px-3 sm:px-4 space-y-2 transition-[max-height,padding] duration-300',
                showAdvancedMediaSearch ? 'py-3' : 'py-0',
                showAdvancedMediaSearch
                  ? (filtersExpanded
                      ? 'max-h-[65vh] md:max-h-[50vh] overflow-y-auto'
                      : 'max-h-[5rem] md:max-h-[4.5rem] overflow-hidden')
                  : 'max-h-0 overflow-hidden'
              )}>
              {/* [CTO-15.23 2026-05-14] 推荐媒体提到第一排 · 老板优先决策诉求(GEO 排名) */}
              {mhzFilters.geo_platforms.length > 0 && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-muted-foreground shrink-0 w-14 pt-0.5">推荐媒体:</span>
                  <div className="flex flex-wrap gap-1">
                    <span onClick={() => { setMhzGeoPlatform(''); setMhzPage(1); }}
                      className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', !mhzGeoPlatform ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>不限</span>
                    {mhzFilters.geo_platforms.map(p => (
                      <span key={p.code} onClick={() => { setMhzGeoPlatform(p.code); setMhzPage(1); }}
                        className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', mhzGeoPlatform === p.code ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{p.name}</span>
                    ))}
                  </div>
                </div>
              )}
              {/* 频道 */}
              <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] items-start gap-2">
                <span className="text-xs text-muted-foreground shrink-0 pt-1">频道:</span>
                <div className="flex gap-1 overflow-x-auto pb-1 sm:flex-wrap sm:overflow-visible sm:pb-0">
                  <span onClick={() => { setMhzResourceTypeName(''); setMhzPage(1); }}
                    className={cn('shrink-0 whitespace-nowrap text-xs px-2 py-0.5 rounded cursor-pointer', !mhzResourceTypeName ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>不限</span>
                  {mhzFilters.resource_type_names.map(t => (
                    <span key={t} onClick={() => { setMhzResourceTypeName(t); setMhzPage(1); }}
                      className={cn('shrink-0 whitespace-nowrap text-xs px-2 py-0.5 rounded cursor-pointer', mhzResourceTypeName === t ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{t}</span>
                  ))}
                </div>
              </div>
              {/* 综合门户 */}
              <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] items-start gap-2">
                <span className="text-xs text-muted-foreground shrink-0 pt-1">综合门户:</span>
                <div className="flex gap-1 overflow-x-auto pb-1 sm:flex-wrap sm:overflow-visible sm:pb-0">
                  <span onClick={() => { setMhzPortalMedia(''); setMhzPage(1); }}
                    className={cn('shrink-0 whitespace-nowrap text-xs px-2 py-0.5 rounded cursor-pointer', !mhzPortalMedia ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>不限</span>
                  {mhzFilters.portal_medias.map(p => (
                    <span key={p} onClick={() => { setMhzPortalMedia(p); setMhzPage(1); }}
                      className={cn('shrink-0 whitespace-nowrap text-xs px-2 py-0.5 rounded cursor-pointer', mhzPortalMedia === p ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{p}</span>
                  ))}
                </div>
              </div>
              {/* 地区 */}
              <div className="flex items-start gap-2">
                <span className="text-xs text-muted-foreground shrink-0 w-14 pt-0.5">地区:</span>
                <div className="flex flex-wrap gap-1">
                  <span onClick={() => { setMhzArea(''); setMhzPage(1); }}
                    className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', !mhzArea ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>不限</span>
                  {mhzFilters.areas.map(a => (
                    <span key={a} onClick={() => { setMhzArea(a); setMhzPage(1); }}
                      className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', mhzArea === a ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{a}</span>
                  ))}
                </div>
              </div>
              {/* 新闻源 */}
              <div className="flex items-start gap-2">
                <span className="text-xs text-muted-foreground shrink-0 w-14 pt-0.5">新闻源:</span>
                <div className="flex flex-wrap gap-1">
                  <span onClick={() => { setMhzNewsResource(''); setMhzPage(1); }}
                    className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', !mhzNewsResource ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>不限</span>
                  {mhzFilters.news_resources.filter(n => n !== '0').map(n => (
                    <span key={n} onClick={() => { setMhzNewsResource(n); setMhzPage(1); }}
                      className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', mhzNewsResource === n ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{n === '1' ? '百度新闻源' : n}</span>
                  ))}
                </div>
              </div>
              {/* 特别行业 */}
              {mhzFilters.special_industries.length > 0 && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-muted-foreground shrink-0 w-14 pt-0.5">特别行业:</span>
                  <div className="flex flex-wrap gap-1">
                    <span onClick={() => { setMhzSpecialIndustry(-1); setMhzPage(1); }}
                      className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', mhzSpecialIndustry < 0 ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>不限</span>
                    {mhzFilters.special_industries.map(si => (
                      <span key={si.code} onClick={() => { setMhzSpecialIndustry(si.code); setMhzPage(1); }}
                        className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', mhzSpecialIndustry === si.code ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{si.name}</span>
                    ))}
                  </div>
                </div>
              )}
              {/* 价格（积分） */}
              <div className="flex items-start gap-2">
                <span className="text-xs text-muted-foreground shrink-0 w-14 pt-0.5">价格:</span>
                <div className="flex flex-wrap items-center gap-1">
                  {([
                    { label: '不限', min: 0, max: 0 },
                    { label: '0-6500', min: 0.01, max: 50 },
                    { label: '6500-13000', min: 51, max: 100 },
                    { label: '13000-26000', min: 101, max: 200 },
                    { label: '26000以上', min: 200.01, max: 0 },
                  ] as const).map(p => {
                    const active = mhzPriceRange[0] === p.min && mhzPriceRange[1] === p.max;
                    return (
                      <span key={p.label} onClick={() => { setMhzPriceRange([p.min, p.max]); setMhzPriceMin(''); setMhzPriceMax(''); setMhzPage(1); }}
                        className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', active ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{p.label}</span>
                    );
                  })}
                  <input type="number" placeholder="最低算力" value={mhzPriceMin} onChange={e => setMhzPriceMin(e.target.value)}
                    className="w-16 h-5 text-xs px-1.5 rounded border border-border bg-background text-foreground" />
                  <span className="text-xs text-muted-foreground">-</span>
                  <input type="number" placeholder="最高算力" value={mhzPriceMax} onChange={e => setMhzPriceMax(e.target.value)}
                    className="w-16 h-5 text-xs px-1.5 rounded border border-border bg-background text-foreground" />
                  <span onClick={() => {
                    const minPts = parseFloat(mhzPriceMin) || 0;
                    const maxPts = parseFloat(mhzPriceMax) || 0;
                    setMhzPriceRange([minPts > 0 ? minPts / 130 : 0, maxPts > 0 ? maxPts / 130 : 0]);
                    setMhzPage(1);
                  }} className="text-xs px-2 py-0.5 rounded cursor-pointer bg-secondary text-foreground hover:bg-secondary/80">确定</span>
                </div>
              </div>
              {/* 排序 */}
              <div className="flex items-start gap-2">
                <span className="text-xs text-muted-foreground shrink-0 w-14 pt-0.5">排序:</span>
                <div className="flex flex-wrap gap-1">
                  {/* 🔴 [#199 a2] 排序档位**从 `MEDIA_SORT_OPTIONS` 渲染** —— 芯片与折叠行那句话
                      必须是同一个源。改前这里写死一份、`mediaTableLabels` 手写一份,
                      于是 9 个档位里有 4 个在折叠行上被说成「默认顺序」(其中一个还是键拼错)。 */}
                  {MEDIA_SORT_OPTIONS.map((s) => (
                    <span key={s.value} data-testid="mhz-sort-chip" data-sort-value={s.value}
                      onClick={() => { setMhzSort(s.value); setMhzPage(1); }}
                      className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', mhzSort === s.value ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{s.chip}</span>
                  ))}
                </div>
              </div>
              </div>{/* end inner overflow container */}
              {/* 渐隐遮罩（收起时） */}
              {!filtersExpanded && showAdvancedMediaSearch && (
                <div className="absolute bottom-6 left-0 right-0 h-8 bg-gradient-to-t from-card to-transparent pointer-events-none" />
              )}
              <button
                type="button"
                onClick={() => setFiltersExpanded(prev => !prev)}
                className="w-full flex items-center justify-center gap-1 py-1 text-[11px] text-muted-foreground hover:text-foreground transition-colors cursor-pointer bg-transparent border-none"
              >
                {filtersExpanded ? (
                  <><ChevronUp className="h-3 w-3" /> 收起高级搜索</>
                ) : (
                  <><ChevronDown className="h-3 w-3" /> {collapsedFilterSummary(mediaFilterSummary)}</>
                )}
              </button>
            </div>{/* end filter wrapper */}

            {showAdvancedMediaSearch && (<>
            {/* 搜索栏 */}
            <div className="px-3 sm:px-4 py-2 border-b border-border flex flex-wrap gap-2 shrink-0">
              <div className="relative flex-1">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 size-4 text-muted-foreground" />
                <Input placeholder="搜索媒体名称..." value={mhzSearchInput} onChange={e => setMhzSearchInput(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') { setMhzSearch(mhzSearchInput); setMhzPage(1); } }} className="pl-9 h-8 text-sm" />
              </div>
              <Button size="sm" variant="outline" className="h-8" onClick={() => { setMhzSearch(mhzSearchInput); setMhzPage(1); }}>搜索</Button>
              <button onClick={() => loadMhzMedia()} className="p-1.5 rounded-md hover:bg-secondary" title="刷新">
                <RefreshCw className={cn('size-3.5 text-muted-foreground', mhzLoading && 'animate-spin')} />
              </button>
              <span className="text-xs text-muted-foreground self-center shrink-0">共 {mhzTotal} 家</span>
            </div>

            {/* 媒体表格 */}
              <div className="flex min-h-[420px] flex-col overflow-hidden md:min-h-0 md:flex-1">
              <div className="flex-1 overflow-auto px-2 py-2 md:px-0 md:py-0">
              <div className="space-y-2 md:hidden">
                {mhzLoading ? (
                  <div className="flex justify-center py-12"><Loader2 className="size-5 animate-spin text-muted-foreground" /></div>
                ) : mhzMedia.length === 0 ? (
                  renderMediaEmpty(mhzSearch)
                ) : (
                  /* [2026-05-27] 移动端教程引导 · 包第 1 张 MhzCard ·
                     原 FeatureTooltip 挂桌面表格行(md:block 隐藏在移动端) · MobileCoachMark 测不到 rect · 必须 fallback */
                  mhzMedia.map((m, mediaIdx) => {
                    const card = <MhzCard key={m.id} m={m} sel={mhzSelMedia.has(m.id)} onT={() => toggleMhzMedia(m)} />
                    if (isSandboxActive() && mediaIdx === 0 && tutorialStage === 'step3-pub-pick-media' && cart.length === 0) {
                      return (
                        <FeatureTooltip
                          key={`sandbox-media-mobile-${m.id}`}
                          featureId="sandbox_pub_pick_media_mobile"
                          stepId="first_article_publish"
                          title="第二步: 选 1 个媒介"
                          content={'媒介有不同价格 / 收录率 / 出稿时间, 看预算和需求选\n点这张卡选中, 或直接点下方「选这个媒介 →」'}
                          side="bottom"
                          wrapClassName="relative block"
                          spotlightPadding={{ top: 8, right: 8, bottom: 8, left: 12 }}
                          nextLabel="选这个媒介 →"
                          onNext={() => { if (!mhzSelMedia.has(m.id)) toggleMhzMedia(m); setTutorialStage('step3-pub-add-cart'); }}
                        >
                          {card}
                        </FeatureTooltip>
                      )
                    }
                    return card
                  })
                )}
              </div>
              <div className="hidden min-w-full md:block">
              {/* [CTO-15.23 2026-05-18 v2-F Phase 9] 媒体表格图例 · 让 V/GEO/6/6/性价比/低价 自解释 */}
              <MediaBadgeLegend />
              {/* 表头 */}
              <div className="grid gap-2 px-4 py-1.5 text-[11px] text-muted-foreground border-b border-border bg-card shrink-0 sticky top-0 z-10" style={{ gridTemplateColumns: `32px minmax(200px,2.2fr) 70px 50px 80px ${WEIGHT_COL_W} ${WEIGHT_COL_W} 70px minmax(120px,1.5fr)`, minWidth: 820 + 2 * (parseInt(WEIGHT_COL_W, 10) - 50) }}>
                <span></span>
                <span>媒体名称</span>
                {/* 2026-05-17 老板"重新梳理成本":价格 = 原价 × 1.5 × 130 积分/元 · 扣充值/佣金 · 不扣赠送 */}
                <span className="text-right cursor-help" title={`按所选媒体结算 · 提交前会显示本次需扣算力\n消耗:充值算力 / 服务收益\n赠送算力不可用于媒体发布`}>
                  价格(算力) <span className="text-muted-foreground/50">ⓘ</span>
                </span>
                <span className="text-right inline-flex items-center justify-end gap-0.5">
                  收录率
                  <HelpHint title="这几个媒体指标怎么看?" side="bottom">
                    <b>收录率</b>:文章发上去后被搜索引擎 / AI 收录的比例, 越高越容易被搜到、被 AI 引用。
                    <br /><b>平均出稿时间</b>:从下单到文章正式上线的平均时长。
                    {/* [#199] 权重那一句搬到权重列自己的 HelpHint 里了 —— 同一件事两处说,改一处就会打架 */}
                    <br />预算有限时, 优先挑<b>收录率高 + 权重高</b>的。
                  </HelpHint>
                </span>
                <span className="text-right">平均出稿时间</span>
                {/*
                  * 🔴 [#199] 权重两列**各挂自己的** HelpHint,并带量纲。
                  *    改前:表头只有四个字,唯一说明藏在**收录率**那一列的 hint 里 ——
                  *    看「7」和「8」的人不知道满分多少、0 是"最差"还是"没数据"。
                  * 🔴 区间数字**不许猜**:等 199-d1 只读取证给出 `max(pc_weight)`,
                  *    取证前 `WEIGHT_RANGE_MAX` 保持 null、界面只写方向与 0 的含义。
                  */}
                <span className="text-right inline-flex items-center justify-end gap-0.5"
                  data-testid="media-th-pc-weight">
                  {weightHeader('pc')}
                  <HelpHint title="电脑权重怎么看?" side="bottom">{weightHint()}</HelpHint>
                </span>
                <span className="text-right inline-flex items-center justify-end gap-0.5"
                  data-testid="media-th-m-weight">
                  {weightHeader('mobile')}
                  <HelpHint title="移动权重怎么看?" side="bottom">{weightHint()}</HelpHint>
                </span>
                <span>新闻源</span>
                <span>备注</span>
              </div>
              {/* 表内容 */}
              <div>
                {mhzLoading ? (
                  <div className="flex justify-center py-12"><Loader2 className="size-5 animate-spin text-muted-foreground" /></div>
                ) : mhzMedia.length === 0 ? (
                  renderMediaEmpty(mhzSearch)
                ) : (
                  mhzMedia.map((m, mediaIdx) => {
                    const mediaRowEl = (
                    <div key={m.id} onClick={() => toggleMhzMedia(m)}
                      className={cn('grid gap-2 px-4 py-2 text-xs border-b border-border/50 cursor-pointer transition-colors items-center',
                        mhzSelMedia.has(m.id) ? 'bg-primary/5' : 'hover:bg-secondary/30')}
                      style={{ gridTemplateColumns: `32px minmax(200px,2.2fr) 70px 50px 80px ${WEIGHT_COL_W} ${WEIGHT_COL_W} 70px minmax(120px,1.5fr)`, minWidth: 820 + 2 * (parseInt(WEIGHT_COL_W, 10) - 50) }}>
                      <div role="checkbox" aria-checked={mhzSelMedia.has(m.id)} onClick={(e) => { e.stopPropagation(); toggleMhzMedia(m); }} className="flex items-center justify-center self-stretch -mx-2 px-2 cursor-pointer hover:bg-primary/10 rounded">
                        <Checkbox checked={mhzSelMedia.has(m.id)} className="size-3.5 pointer-events-none" />
                      </div>
                      <div className="min-w-0 flex flex-wrap items-center gap-1">
                        <span className="text-sm font-medium break-words leading-snug" title={m.media_name}>{m.media_name}</span>
                        {m.entrance_link && (
                          <a href={m.entrance_link} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()}
                            className="text-[10px] text-blue-400 hover:underline shrink-0">(入口)</a>
                        )}
                        {m.authority_media === 1 && <span className="shrink-0 px-1 py-0 text-[9px] rounded bg-blue-500/20 text-blue-400 font-bold" title="权威媒体认证">V</span>}
                        {m.geo_rank > 0 && <span className="shrink-0 px-1 py-0 text-[9px] rounded bg-green-500/20 text-green-400 font-bold" title="GEO 调研中被 AI 引擎引用">GEO</span>}
                        {/* [CTO-15.23 2026-05-18 v2-F] 真权威 4 件套:引擎覆盖徽章 + 甜点 Badge + 低价警告 */}
                        <GeoEngineBadge geoRankPlatform={m.geo_rank_platform} />
                        <SweetSpotBadge sweetSpot={!!m.is_sweet_spot} geoRankPlatform={m.geo_rank_platform} />
                      </div>
                      <span className="text-right text-primary font-semibold">{(m.price_points || 0).toLocaleString()}</span>
                      <span className="text-right text-muted-foreground">{m.inclusion_rate || '-'}</span>
                      <span className="text-right text-muted-foreground">{formatAvgTime(m.avg_time)}</span>
                      <span className="text-right text-muted-foreground">{m.pc_weight || '-'}</span>
                      <span className="text-right text-muted-foreground">{m.m_weight || '-'}</span>
                      <span className="text-muted-foreground break-words leading-snug">{NEWS_RESOURCE_MAP[m.news_resource] ?? '非新闻源'}</span>
                      {/* CTO-15.23 Phase 2:长备注 inline → ExpandableMeta preview · 防末列 popover 溢出右屏用 align="end" */}
                      <div onClick={(e) => e.stopPropagation()}>
                        <ExpandableMeta text={m.remark} mode="preview" maxChars={12} iconLabel="备注" align="end" />
                      </div>
                    </div>
                    );
                    // 沙盒: 仅第 1 篇引导
                    // [2026-05-28] 移动端 disable 这份 PC FeatureTooltip · 移动端走 line 3018 MhzCard 那份
                    //   避免两个 FeatureTooltip 同时 mount 渲染 2 个 MobileCoachMark sheet
                    if (!isMobile && isSandboxActive() && mediaIdx === 0 && tutorialStage === 'step3-pub-pick-media' && cart.length === 0) {
                      return (
                        <FeatureTooltip
                          key={`sandbox-media-${m.id}`}
                          featureId="sandbox_pub_pick_media"
                          stepId="first_article_publish"
                          title="第二步: 选 1 个媒介"
                          content={'媒介有不同价格 / 收录率 / 出稿时间, 看预算和需求选\n点这一行任意处选中, 或直接点下方「选这个媒介 →」'}
                          side="left"
                          wrapClassName="relative block"
                          spotlightPadding={{ top: 8, right: 8, bottom: 8, left: 12 }}
                          nextLabel="选这个媒介 →"
                          onNext={() => { if (!mhzSelMedia.has(m.id)) toggleMhzMedia(m); setTutorialStage('step3-pub-add-cart'); }}
                        >
                          {mediaRowEl}
                        </FeatureTooltip>
                      );
                    }
                    return mediaRowEl;
                  })
                )}
              </div>
              </div>
              </div>
              {/* 分页 */}
              {mhzPages > 1 && (
                <div className="flex items-center justify-between gap-2 px-3 sm:px-4 py-1.5 border-t border-border shrink-0">
                  {/* 🔴 [#181 第 3 条] 请求在途时两个键都不给点:切筛选后到回包前
                      `pages` 还是上一次的值,照它放行就会请求一个越界页 ⇒ 后端返空列表
                      ⇒ 屏幕看起来"没翻"。判定走纯函数,两个 tab 同一份实现。 */}
                  <Button variant="ghost" size="sm" disabled={!canGoPrev({ page: mhzPage, loading: mhzLoading })} onClick={() => setMhzPage(p => p - 1)}><ChevronLeft className="size-4" /> 上一页</Button>
                  <span className="text-xs text-muted-foreground">{mhzPage}/{mhzPages} (共 {mhzTotal} 条)</span>
                  <Button variant="ghost" size="sm" disabled={!canGoNext({ page: mhzPage, pages: mhzPages, loading: mhzLoading })} onClick={() => setMhzPage(p => p + 1)}>下一页 <ChevronRight className="size-4" /></Button>
                </div>
              )}
            </div>
            </>)}

            {/* 底部栏 */}
            <div className="sticky bottom-0 z-20 flex shrink-0 flex-col gap-2 border-t border-border bg-card/95 px-3 py-3 pb-3 backdrop-blur sm:flex-row sm:items-center sm:justify-between sm:px-4 md:static md:pb-3">
              <div className="min-w-0 space-y-1 text-sm sm:space-y-0">
                {/* [CTO-15.23 2026-05-05] 批量模式优先文案 · 单文章 fallback */}
                {batchArticleIds.size > 0
                  ? <span className="block">已选 <span className="font-medium">{batchArticleIds.size}</span> 篇文章 · 共用同组媒体</span>
                  : activeArticle
                    ? <span className="block line-clamp-2 break-words" title={activeArticle.title}>文章: <span className="font-medium">{activeArticle.title}</span></span>
                    : <span className="text-muted-foreground">请先在左侧选择文章</span>}
                <span className="block text-xs text-muted-foreground sm:ml-3 sm:inline">
                  {mhzSelList.length > 0 && <>软文 {mhzSelList.length} 个 · {fmtPts(mhzTotalPoints)} 算力</>}
                  {mhzSelList.length > 0 && wmSelList.length > 0 && <span className="mx-1">/</span>}
                  {wmSelList.length > 0 && <>自媒体 {wmSelList.length} 个 · {fmtPts(wmTotalPoints)} 算力</>}
                  {(mhzSelList.length > 0 || wmSelList.length > 0) && (
                    <span className="block text-[10px] text-muted-foreground/70 mt-0.5 sm:inline sm:ml-2">
                      · 扣充值算力 / 服务收益 · <span className="text-amber-500/80">赠送算力不可用</span>
                      <HelpHint title="算力为什么分三种?哪种能用?" className="ml-0.5 align-middle">
                        系统里算力分三种:<b>充值算力</b>(你付钱买的)、<b>服务收益</b>(推荐 / 成交赚的)、<b>赠送算力</b>(注册或活动白送的)。
                        <br />媒体发布等消耗操作只认<b>充值算力和服务收益</b>;赠送算力只能用于体验类功能, 不能抵发布费用。
                        <br />余额不够时, 去「我的钱包」充值。
                      </HelpHint>
                    </span>
                  )}
                </span>
                {/* [2026-06-02 GEO CTO · Codex 复审] 联系方式软提示:按 contact_policy 三态区分(不阻断·后端自动软化) */}
                {(() => {
                  const _all = [...mhzSelList, ...wmSelList];
                  const _noneCnt = _all.filter((m: any) => ((m?.contact_policy || 'none') === 'none')).length;
                  const _siteCnt = _all.filter((m: any) => (m?.contact_policy === 'website_only')).length;
                  if (_noneCnt === 0 && _siteCnt === 0) return null;
                  return (
                    <span className="mt-1.5 block rounded-md border border-amber-300/60 bg-amber-50 px-2 py-1 text-[10px] leading-relaxed text-amber-700">
                      {_noneCnt > 0 && <span className="block">· 你选的媒体里有 {_noneCnt} 个不能放联系方式，系统会自动改成更稳妥的说法（如「搜索品牌名」）。</span>}
                      {_siteCnt > 0 && <span className="block">· 有 {_siteCnt} 个只能放官网，不会放电话/微信。</span>}
                      <span className="block text-amber-600/80">如需展示完整联系方式，请选择支持联系方式的媒体。</span>
                    </span>
                  );
                })()}
              </div>
              <FeatureTooltip
                featureId="sandbox_publish_add_cart"
                stepId="first_article_publish"
                title="第三步: 点 加入购物车"
                content={'选好文章 + 媒介, 点这里加进购物车\n加完后教程会自动帮你把剩下 5 篇加好, 接着就能 一键发布'}
                side="top"
                mobileTargetSelector="[data-mobile-coach='publish-add-cart-button']"
                wrapClassName="relative block w-full sm:inline-block sm:w-auto"
                disabled={!isSandboxActive() || tutorialStage !== 'step3-pub-add-cart'}
              >
                <Button onClick={addToCart}
                  data-mobile-coach="publish-add-cart-button"
                  disabled={(batchArticleIds.size === 0 && !activeArticle) || (mhzSelList.length === 0 && wmSelList.length === 0)}
                  className="w-full gap-1.5 sm:w-auto">
                  <ShoppingCart className="size-4" />
                  {batchArticleIds.size > 1 ? `批量加入 (${batchArticleIds.size} 篇)` : '加入购物车'}
                </Button>
              </FeatureTooltip>
            </div>
            </>)}

            {/* ===== 自媒体推广 ===== */}
            {proxyType === 'wemedia' && (<>
            {/* 筛选区域 — 默认收起，点击展开 */}
            <div className="border-b border-border shrink-0 relative">
              {/* [CTO-15.23 2026-05-13 BUG fix] 自媒体筛选展开同样 vh 化 · 防底部按钮挤出 viewport */}
              <div className={cn(
                'px-3 sm:px-4 py-3 space-y-2 transition-[max-height] duration-300',
                filtersExpanded
                  ? 'max-h-[65vh] md:max-h-[50vh] overflow-y-auto'
                  : 'max-h-[5rem] md:max-h-[4.5rem] overflow-hidden'
              )}>
              {/* 平台 */}
              <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] items-start gap-2">
                <span className="text-xs text-muted-foreground shrink-0 pt-1">平台:</span>
                <div className="flex gap-1 overflow-x-auto pb-1 sm:flex-wrap sm:overflow-visible sm:pb-0">
                  <span onClick={() => { setWmPlatform(''); setWmPage(1); }}
                    className={cn('shrink-0 whitespace-nowrap text-xs px-2 py-0.5 rounded cursor-pointer', !wmPlatform ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>不限</span>
                  {wmFilters.platforms.map(p => (
                    <span key={p} onClick={() => { setWmPlatform(p); setWmPage(1); }}
                      className={cn('shrink-0 whitespace-nowrap text-xs px-2 py-0.5 rounded cursor-pointer', wmPlatform === p ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{p}</span>
                  ))}
                </div>
              </div>
              {/* 行业 */}
              <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] items-start gap-2">
                <span className="text-xs text-muted-foreground shrink-0 pt-1">行业:</span>
                <div className="flex gap-1 overflow-x-auto pb-1 sm:flex-wrap sm:overflow-visible sm:pb-0">
                  <span onClick={() => { setWmIndustry(''); setWmPage(1); }}
                    className={cn('shrink-0 whitespace-nowrap text-xs px-2 py-0.5 rounded cursor-pointer', !wmIndustry ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>不限</span>
                  {/* [WO_PUBLISH_DISPATCH Part② 2026-08-17] L1 大类 + 数量。
                      0 家的大类后端就不下发,这里没有「隐藏」分支可写 —— 渲染即非 0。 */}
                  {wmFilters.industries.map(i => (
                    <span key={i.key} data-testid="wm-industry-chip" data-industry-key={i.key} data-industry-count={i.count}
                      onClick={() => { setWmIndustry(i.key); setWmPage(1); }}
                      className={cn('shrink-0 whitespace-nowrap text-xs px-2 py-0.5 rounded cursor-pointer', wmIndustry === i.key ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{i.key} {i.count}</span>
                  ))}
                </div>
              </div>
              {/* 地区 */}
              <div className="flex items-start gap-2">
                <span className="text-xs text-muted-foreground shrink-0 w-14 pt-0.5">地区:</span>
                <div className="flex flex-wrap gap-1">
                  <span onClick={() => { setWmProvince(''); setWmPage(1); }}
                    className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', !wmProvince ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>不限</span>
                  {wmFilters.provinces.map(p => (
                    <span key={p} onClick={() => { setWmProvince(p); setWmPage(1); }}
                      className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', wmProvince === p ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{p}</span>
                  ))}
                </div>
              </div>
              {/* 价格（积分） */}
              <div className="flex items-start gap-2">
                <span className="text-xs text-muted-foreground shrink-0 w-14 pt-0.5">价格:</span>
                <div className="flex flex-wrap items-center gap-1">
                  {([
                    { label: '不限', min: 0, max: 0 },
                    { label: '0-6500', min: 0.01, max: 50 },
                    { label: '6500-13000', min: 51, max: 100 },
                    { label: '13000-26000', min: 101, max: 200 },
                    { label: '26000以上', min: 200.01, max: 0 },
                  ] as const).map(p => {
                    const active = wmPriceRange[0] === p.min && wmPriceRange[1] === p.max;
                    return (
                      <span key={p.label} onClick={() => { setWmPriceRange([p.min, p.max]); setWmPriceMin(''); setWmPriceMax(''); setWmPage(1); }}
                        className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', active ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{p.label}</span>
                    );
                  })}
                  <input type="number" placeholder="最低算力" value={wmPriceMin} onChange={e => setWmPriceMin(e.target.value)}
                    className="w-16 h-5 text-xs px-1.5 rounded border border-border bg-background text-foreground" />
                  <span className="text-xs text-muted-foreground">-</span>
                  <input type="number" placeholder="最高算力" value={wmPriceMax} onChange={e => setWmPriceMax(e.target.value)}
                    className="w-16 h-5 text-xs px-1.5 rounded border border-border bg-background text-foreground" />
                  <span onClick={() => {
                    const minPts = parseFloat(wmPriceMin) || 0;
                    const maxPts = parseFloat(wmPriceMax) || 0;
                    setWmPriceRange([minPts > 0 ? minPts / 130 : 0, maxPts > 0 ? maxPts / 130 : 0]);
                    setWmPage(1);
                  }} className="text-xs px-2 py-0.5 rounded cursor-pointer bg-secondary text-foreground hover:bg-secondary/80">确定</span>
                </div>
              </div>
              {/* 官方自媒体 */}
              <div className="flex items-start gap-2">
                <span className="text-xs text-muted-foreground shrink-0 w-14 pt-0.5">官方认证:</span>
                <div className="flex flex-wrap gap-1">
                  {([
                    { label: '不限', value: -1 },
                    { label: '官方自媒体', value: 1 },
                    { label: '非官方', value: 0 },
                  ] as const).map(a => (
                    <span key={a.value} onClick={() => { setWmAuthorityMedia(a.value); setWmPage(1); }}
                      className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', wmAuthorityMedia === a.value ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{a.label}</span>
                  ))}
                </div>
              </div>
              {/* 推荐媒体（GEO排名） */}
              {wmFilters.geo_platforms.length > 0 && (
              <div className="flex items-start gap-2">
                <span className="text-xs text-muted-foreground shrink-0 w-14 pt-0.5">推荐媒体:</span>
                <div className="flex flex-wrap gap-1">
                  <span onClick={() => { setWmGeoPlatform(''); setWmPage(1); }}
                    className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', !wmGeoPlatform ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>不限</span>
                  {wmFilters.geo_platforms.map(p => (
                    <span key={p.code} onClick={() => { setWmGeoPlatform(p.code); setWmPage(1); }}
                      className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', wmGeoPlatform === p.code ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{p.name}</span>
                  ))}
                </div>
              </div>
              )}
              {/* 粉丝数 */}
              <div className="flex items-start gap-2">
                <span className="text-xs text-muted-foreground shrink-0 w-14 pt-0.5">粉丝数:</span>
                <div className="flex flex-wrap gap-1">
                  {([
                    { label: '不限', min: 0, max: 0 },
                    { label: '0-1000', min: 0, max: 1000 },
                    { label: '1千-5千', min: 1000, max: 5000 },
                    { label: '5千-1万', min: 5000, max: 10000 },
                    { label: '1万以上', min: 10000, max: 0 },
                  ] as const).map(f => {
                    const active = wmFansRange[0] === f.min && wmFansRange[1] === f.max;
                    return (
                      <span key={f.label} onClick={() => { setWmFansRange([f.min, f.max]); setWmPage(1); }}
                        className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', active ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{f.label}</span>
                    );
                  })}
                </div>
              </div>
              {/* 排序 */}
              <div className="flex items-start gap-2">
                <span className="text-xs text-muted-foreground shrink-0 w-14 pt-0.5">排序:</span>
                <div className="flex flex-wrap gap-1">
                  {([
                    { label: '默认', value: 'id_asc' },
                    { label: '价格↑', value: 'price_asc' },
                    { label: '价格↓', value: 'price_desc' },
                    { label: '出稿时间↑', value: 'avg_time_asc' },
                    { label: '出稿率↓', value: 'p_rate_desc' },
                  ] as const).map((s) => (
                    <span key={s.value} onClick={() => { setWmSort(s.value); setWmPage(1); }}
                      className={cn('text-xs px-2 py-0.5 rounded cursor-pointer', wmSort === s.value ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}>{s.label}</span>
                  ))}
                </div>
              </div>
              </div>{/* end inner overflow container */}
              {!filtersExpanded && (
                <div className="absolute bottom-6 left-0 right-0 h-8 bg-gradient-to-t from-card to-transparent pointer-events-none" />
              )}
              <button
                type="button"
                onClick={() => setFiltersExpanded(prev => !prev)}
                className="w-full flex items-center justify-center gap-1 py-1 text-[11px] text-muted-foreground hover:text-foreground transition-colors cursor-pointer bg-transparent border-none"
              >
                {filtersExpanded ? (
                  <><ChevronUp className="h-3 w-3" /> 收起筛选</>
                ) : (
                  <><ChevronDown className="h-3 w-3" /> 展开全部筛选</>
                )}
              </button>
            </div>{/* end filter wrapper */}

            {/* 搜索栏 */}
            <div className="px-3 sm:px-4 py-2 border-b border-border flex flex-wrap gap-2 shrink-0">
              <div className="relative flex-1">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 size-4 text-muted-foreground" />
                <Input placeholder="搜索媒体名称..." value={wmSearchInput} onChange={e => setWmSearchInput(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') { setWmSearch(wmSearchInput); setWmPage(1); } }} className="pl-9 h-8 text-sm" />
              </div>
              <Button size="sm" variant="outline" className="h-8" onClick={() => { setWmSearch(wmSearchInput); setWmPage(1); }}>搜索</Button>
              <button onClick={() => loadWmMedia()} className="p-1.5 rounded-md hover:bg-secondary" title="刷新">
                <RefreshCw className={cn('size-3.5 text-muted-foreground', wmLoading && 'animate-spin')} />
              </button>
              <span className="text-xs text-muted-foreground self-center shrink-0">共 {wmTotal} 家</span>
            </div>

            {/* 自媒体表格 */}
            <div className="flex min-h-[420px] flex-col overflow-hidden md:min-h-0 md:flex-1">
              <div className="flex-1 overflow-auto px-2 py-2 md:px-0 md:py-0">
              <div className="space-y-2 md:hidden">
                {wmLoading ? (
                  <div className="flex justify-center py-12"><Loader2 className="size-5 animate-spin text-muted-foreground" /></div>
                ) : wmMedia.length === 0 ? (
                  renderMediaEmpty(wmSearch)
                ) : (
                  wmMedia.map((m: any) => (
                    <WmCard key={m.id} m={m} sel={wmSelMedia.has(m.id)} onT={() => toggleWmMedia(m)} />
                  ))
                )}
              </div>
              <div className="hidden min-w-full md:block">
              {/* 表头 */}
              <div className="grid gap-2 px-4 py-1.5 text-[10px] text-muted-foreground border-b border-border sticky top-0 bg-card z-10" style={{ gridTemplateColumns: '32px minmax(200px,2.4fr) 70px 55px 70px 50px 80px 60px minmax(100px,1.5fr)', minWidth: 820 }}>
                <span></span>
                <span>媒体名称</span>
                <span>平台</span>
                <span>行业</span>
                {/* 2026-05-17 老板"重新梳理成本":价格 = 原价 × 1.5 × 130 积分/元 · 扣充值/佣金 · 不扣赠送 */}
                <span className="text-right cursor-help" title={`按所选媒体结算 · 提交前会显示本次需扣算力\n消耗:充值算力 / 服务收益\n赠送算力不可用于媒体发布`}>
                  价格(算力) <span className="text-muted-foreground/50">ⓘ</span>
                </span>
                <span className="text-right">出稿率</span>
                <span>出稿时间</span>
                <span className="text-right">粉丝数</span>
                <span>备注</span>
              </div>
              {/* 表内容 */}
              <div>
                {wmLoading ? (
                  <div className="flex justify-center py-12"><Loader2 className="size-5 animate-spin text-muted-foreground" /></div>
                ) : wmMedia.length === 0 ? (
                  renderMediaEmpty(wmSearch)
                ) : (
                  wmMedia.map((m: any) => (
                    <div key={m.id} onClick={() => toggleWmMedia(m)}
                      className={cn('grid gap-2 px-4 py-2 text-xs cursor-pointer border-b border-border/50 hover:bg-secondary/30 items-center',
                        wmSelMedia.has(m.id) && 'bg-primary/5')}
                      style={{ gridTemplateColumns: '32px minmax(200px,2.4fr) 70px 55px 70px 50px 80px 60px minmax(100px,1.5fr)', minWidth: 820 }}>
                      {/* Checkbox */}
                      <span><Checkbox checked={wmSelMedia.has(m.id)} onClick={(e) => e.stopPropagation()} onCheckedChange={() => toggleWmMedia(m)} className="size-3.5" /></span>
                      {/* Name with badges */}
                      <div className="flex items-center gap-2 min-w-0">
                        {WM_PLATFORM_LOGOS[m.platform] ? (
                          <img src={WM_PLATFORM_LOGOS[m.platform]} alt="" className="shrink-0 w-6 h-6 rounded object-contain"
                            onError={(e) => { const t = e.currentTarget; t.style.display = 'none'; t.nextElementSibling?.classList.remove('hidden'); }} />
                        ) : null}
                        <span className={cn('shrink-0 w-6 h-6 rounded flex items-center justify-center text-[10px] text-white font-bold', WM_PLATFORM_LOGOS[m.platform] ? 'hidden' : '', WM_PLATFORM_COLORS[m.platform] || 'bg-zinc-500')}>
                          {m.platform?.charAt(0) || '?'}
                        </span>
                        <div className="min-w-0 flex items-center gap-1 flex-wrap">
                          <span className="font-medium break-words leading-snug" title={m.toutiao_name || m.media_name}>{m.toutiao_name || m.media_name}</span>
                          {m.entrance_link && (
                            <a href={m.entrance_link} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()}
                              className="text-[10px] text-blue-400 hover:underline shrink-0">(入口)</a>
                          )}
                          {m.authority_media === 1 && <span className="shrink-0 px-1 py-0 text-[9px] rounded bg-blue-500/20 text-blue-400 font-bold">V</span>}
                          {m.geo_rank > 0 && <span className="shrink-0 px-1 py-0 text-[9px] rounded bg-green-500/20 text-green-400 font-bold">GEO</span>}
                        </div>
                      </div>
                      {/* Platform */}
                      <span className="text-muted-foreground">{m.platform}</span>
                      {/* Industry */}
                      <span className="text-muted-foreground">{m.industry}</span>
                      {/* Price */}
                      <span className="text-right font-medium text-primary">{(m.price_points || 0).toLocaleString()}</span>
                      {/* Publish rate */}
                      <span className="text-right text-muted-foreground">{m.p_rate || '-'}</span>
                      {/* Avg time */}
                      <span className="text-muted-foreground">{formatAvgTime(m.avg_time)}</span>
                      {/* Fans */}
                      <span className="text-right text-muted-foreground">{fmtFans(m.fans_num)}</span>
                      {/* Remark · CTO-15.23 Phase 2:长备注 → ExpandableMeta preview · align="end" 防末列右屏溢出 */}
                      <div onClick={(e) => e.stopPropagation()}>
                        <ExpandableMeta text={m.remark} mode="preview" maxChars={12} iconLabel="备注" align="end" />
                      </div>
                    </div>
                  ))
                )}
              </div>
              </div>
              </div>
              {/* 分页 */}
              {wmPages > 1 && (
                <div className="flex items-center justify-between px-4 py-1.5 border-t border-border shrink-0">
                  <Button variant="ghost" size="sm" disabled={!canGoPrev({ page: wmPage, loading: wmLoading })} onClick={() => setWmPage(p => p - 1)}><ChevronLeft className="size-4" /> 上一页</Button>
                  <span className="text-xs text-muted-foreground">{wmPage}/{wmPages} (共 {wmTotal} 条)</span>
                  <Button variant="ghost" size="sm" disabled={!canGoNext({ page: wmPage, pages: wmPages, loading: wmLoading })} onClick={() => setWmPage(p => p + 1)}>下一页 <ChevronRight className="size-4" /></Button>
                </div>
              )}
            </div>

            {/* 底部栏 */}
            <div className="sticky bottom-0 z-20 flex shrink-0 flex-col gap-2 border-t border-border bg-card/95 px-3 py-3 pb-3 backdrop-blur sm:flex-row sm:items-center sm:justify-between sm:px-4 md:static md:pb-3">
              <div className="min-w-0 space-y-1 text-sm sm:space-y-0">
                {/* [CTO-15.23 2026-05-05] 批量模式优先 · 单文章 fallback */}
                {batchArticleIds.size > 0
                  ? <span className="block">已选 <span className="font-medium">{batchArticleIds.size}</span> 篇文章 · 共用同组媒体</span>
                  : activeArticle
                    ? <span className="block line-clamp-2 break-words" title={activeArticle.title}>文章: <span className="font-medium">{activeArticle.title}</span></span>
                    : <span className="text-muted-foreground">请先在左侧选择文章</span>}
                <span className="block text-xs text-muted-foreground sm:ml-3 sm:inline">
                  {mhzSelList.length > 0 && <>软文 {mhzSelList.length} 个 · {fmtPts(mhzTotalPoints)} 算力</>}
                  {mhzSelList.length > 0 && wmSelList.length > 0 && <span className="mx-1">/</span>}
                  {wmSelList.length > 0 && <>自媒体 {wmSelList.length} 个 · {fmtPts(wmTotalPoints)} 算力</>}
                  {(mhzSelList.length > 0 || wmSelList.length > 0) && (
                    <span className="block text-[10px] text-muted-foreground/70 mt-0.5 sm:inline sm:ml-2">
                      · 扣充值算力 / 服务收益 · <span className="text-amber-500/80">赠送算力不可用</span>
                      <HelpHint title="算力为什么分三种?哪种能用?" className="ml-0.5 align-middle">
                        系统里算力分三种:<b>充值算力</b>(你付钱买的)、<b>服务收益</b>(推荐 / 成交赚的)、<b>赠送算力</b>(注册或活动白送的)。
                        <br />媒体发布等消耗操作只认<b>充值算力和服务收益</b>;赠送算力只能用于体验类功能, 不能抵发布费用。
                        <br />余额不够时, 去「我的钱包」充值。
                      </HelpHint>
                    </span>
                  )}
                </span>
              </div>
              <Button onClick={addToCart}
                disabled={(batchArticleIds.size === 0 && !activeArticle) || (mhzSelList.length === 0 && wmSelList.length === 0)}
                className="w-full gap-1.5 sm:w-auto">
                <ShoppingCart className="size-4" />
                {batchArticleIds.size > 1 ? `批量加入 (${batchArticleIds.size} 篇)` : '加入购物车'}
              </Button>
            </div>
            </>)}

          </div>
        )}

        {mode === 'history' && (
          <div className="flex-1 overflow-auto">
            <Suspense fallback={<div className="flex justify-center py-12"><Loader2 className="size-5 animate-spin text-muted-foreground" /></div>}>
              <PublishHistory />
            </Suspense>
          </div>
        )}

        {/* v1_2 (CTO-14.0 Bug 6): advisor 和 placement 拆成独立 Tab */}
        {mode === 'advisor' && (
          <div className="flex-1 min-w-0 overflow-auto p-0 md:p-4">
            <Suspense fallback={<div className="flex justify-center py-12"><Loader2 className="size-5 animate-spin text-muted-foreground" /></div>}>
              <GeoResearchCenter />
            </Suspense>
          </div>
        )}

        {mode === 'placement' && (
          <div className="flex-1 overflow-auto p-4">
            <Suspense fallback={<div className="flex justify-center py-12"><Loader2 className="size-5 animate-spin text-muted-foreground" /></div>}>
              <PlacementCenter />
            </Suspense>
          </div>
        )}

        {mode === 'manual' && showManualGovernance && (
          <div className="flex-1 overflow-auto">
            <Suspense fallback={<div className="flex justify-center py-12"><Loader2 className="size-5 animate-spin text-muted-foreground" /></div>}>
              <ManualPublicationForm
                defaultArticleId={preArticleId ?? undefined}
                defaultQuoteId={preQuoteId ?? undefined}
                onSubmitted={() => { /* 可切到 history tab 供用户看 */ }}
              />
            </Suspense>
          </div>
        )}
      </div>

      {/* 底部购物车栏
          [svideo lane · 2026-07-04] 短视频子 tab 隐藏(短视频不进共享购物车，面板有自己的结算栏，
          双栏叠加会让用户困惑；切回软文/自媒体 tab 购物车照常显示，内容不丢) */}
      {flowLayout.articleCart && cart.length > 0 && (
        <div className="sticky bottom-0 z-30 shrink-0 border-t border-border bg-card/95 shadow-2xl shadow-black/30 backdrop-blur">
          {/* [客户反馈② 2026-08-09] 地区备注 · 非必选 · 仅对需要的媒体提示。
              上游下单接口一直支持备注,我方从来没填过 —— 客户要"发到某某地区"只能靠它。 */}
          {/* 🔴 [2026-08-14] 常驻输入条已撤 —— 改为加购时弹窗(见 addToCart)。
              这里只留一行**摘要 + 可点修改**的补救路径:填错了要能改,
              否则用户只能把媒体删掉重加。仅在购物车真含列举网时渲染。 */}
          {regionRemarkMediaNames.length > 0 && REGION_REMARK_ENABLED && (
            <div className="border-b border-border bg-amber-500/5 px-3 py-1.5 sm:px-4" data-testid="region-remark-bar">
              <button
                type="button"
                data-testid="region-remark-edit"
                onClick={() => { setRegionDraft(regionRemark); setRegionDialogOpen(true); }}
                className="flex w-full items-center gap-2 text-left text-[11px] leading-snug text-muted-foreground hover:text-foreground"
              >
                <span className="shrink-0 font-medium text-amber-700 dark:text-amber-400">
                  {regionRemarkMediaNames.join('、')}
                </span>
                {regionRemark.trim() ? (
                  <span>投放地区：<strong className="text-foreground">{regionRemark.trim()}</strong></span>
                ) : (
                  <span>未指定投放地区，将按对方默认地区发布</span>
                )}
                <span className="ml-auto shrink-0 underline">修改</span>
              </button>
            </div>
          )}
          {/* [下单备注 P0 · 2026-08-10] 停用期间的替代提示 —— 2026-08-14 已开闸,
              但闸随时可能因真单失败被 MHZ_ORDER_REMARK_ENABLED=0 关回去,
              所以这条分支保留:关闸时用户仍要知道为什么指定不了地区、下一步做什么。 */}
          {regionRemarkMediaNames.length > 0 && !REGION_REMARK_ENABLED && (
            <div className="border-b border-border bg-amber-500/5 px-3 py-2 sm:px-4" data-testid="region-remark-disabled-notice">
              <p className="text-[11px] leading-snug text-muted-foreground">
                {regionRemarkMediaNames.join('、')} 这类媒体不按地区分站。
                <span className="font-medium text-amber-700 dark:text-amber-400">「指定投放地区」正在与发布通道对齐规则，暂时停用</span>
                ，本次将按对方默认地区发布。若这单必须落在指定地区，建议先换一家按地区分站的媒体。
              </p>
            </div>
          )}
          {/* 收起状态 */}
          <div className="flex cursor-pointer flex-col gap-2 px-3 py-2.5 pb-2.5 sm:flex-row sm:items-center sm:justify-between sm:px-4 sm:pb-2.5" onClick={() => setCartOpen(!cartOpen)}>
            <div className="flex min-w-0 flex-wrap items-center gap-2 sm:gap-3 text-sm">
              <ShoppingCart className="size-4 text-primary" />
              <span>{cartTotalArticles} 篇文章</span>
              <span className="text-muted-foreground">·</span>
              <span>{cartTotalMedia} 个媒体</span>
              <span className="text-muted-foreground">·</span>
              <span className="font-medium text-primary">
                {sandboxActive ? '教程演练 · 0 算力' : `${fmtPts(cartTotalPoints)} 算力`}
              </span>
            </div>
            <div className="flex items-center gap-2 sm:justify-end">
              <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); setCartOpen(!cartOpen); }}>
                {cartOpen ? <ChevronDown className="size-4" /> : <ChevronUp className="size-4" />}
                {cartOpen ? '收起' : '展开'}
              </Button>
              <FeatureTooltip
                featureId={tutorialStage === 'step4-opt-batch' ? 'sandbox_opt_batch_send' : 'sandbox_publish_batch_send'}
                stepId={tutorialStage === 'step4-opt-batch' ? 'first_monitoring' : 'first_article_publish'}
                title={tutorialStage === 'step4-opt-batch' ? '补发文章 · 模拟发布' : '第四步：完成模拟发布'}
                content={tutorialStage === 'step4-opt-batch'
                  ? '教程已经帮你把这 2 篇补发文配好渠道并加入清单。\n\n点击“模拟发布”完成演练，随后回到效果监测查看变化。教程不外发、不扣算力。'
                  : '教程模式为了节省时间，已经帮你把剩下 5 篇配好渠道并加入清单。\n\n点击“模拟发布”完成操作演练。教程不会外发、扣算力或创建审批任务。'}
                side="left"
                /* [2026-05-27] flex-1 让 button 父级宽度跟随 · wrap span inline-block 测错 ·
                   用 selector 直接定位按钮本体 */
                mobileTargetSelector="[data-mobile-coach='publish-batch-send-button']"
                disabled={!isSandboxActive() || (tutorialStage !== 'step3-pub-batch-send' && tutorialStage !== 'step4-opt-batch')}
              >
                <Button
                  data-mobile-coach="publish-batch-send-button"
                  onClick={(e) => { e.stopPropagation(); handleCartBatchPublish(); }} disabled={cartSubmitting}
                  className="min-h-11 flex-1 gap-1.5 sm:min-h-0 sm:flex-none">
                  {cartSubmitting ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
                  {sandboxActive ? '模拟发布' : '一键发布'}
                </Button>
              </FeatureTooltip>
            </div>
          </div>
          {/* 展开详情 */}
          {cartOpen && (
            <div className="border-t border-border max-h-48 overflow-y-auto">
              {cart.map(c => (
                <div key={c.articleId} className="px-4 py-2 flex items-center gap-3 text-sm hover:bg-secondary/30">
                  <button onClick={(e) => { e.stopPropagation(); removeFromCart(c.articleId); }}
                    className="shrink-0 p-1.5 rounded hover:bg-destructive/10 text-destructive" title="删除">
                    <XCircle className="size-4" />
                  </button>
                  <div className="flex-1 min-w-0 overflow-hidden">
                    <span className="font-medium line-clamp-2 break-words block" title={c.articleTitle}>{c.articleTitle}</span>
                    <span className="text-xs text-muted-foreground line-clamp-2 break-words block">
                      {c.mediaList.map(m => `${m.name}${m.mediaType === 'wemedia' ? '[自]' : ''}`).join(', ')}
                      {' · '}
                      {sandboxActive
                        ? '教程渠道 · 0 算力'
                        : `${fmtPts(c.mediaList.reduce((s, m) => s + m.costPoints, 0))} 算力`}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 余额不足弹窗 */}
      {showInsufficientDialog && !sandboxActive && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={() => setShowInsufficientDialog(null)}>
          <div className="bg-card rounded-lg p-6 max-w-sm w-full mx-4 shadow-xl" onClick={e => e.stopPropagation()}>
            <h3 className="text-lg font-bold mb-4">算力不足</h3>
            <div className="space-y-2 text-sm mb-6">
              <div className="flex justify-between"><span className="text-muted-foreground">本次发布需要</span><span className="font-medium">{fmtPts(showInsufficientDialog.required)} 算力</span></div>
              <div className="flex justify-between"><span className="text-muted-foreground">当前余额</span><span className="font-medium">{fmtPts(showInsufficientDialog.balance)} 算力</span></div>
              <div className="flex justify-between"><span className="text-muted-foreground">还差</span><span className="font-medium text-destructive">{fmtPts(showInsufficientDialog.required - showInsufficientDialog.balance)} 算力</span></div>
            </div>
            <div className="flex gap-3">
              <Button variant="outline" className="flex-1" onClick={() => setShowInsufficientDialog(null)}>取消</Button>
              <Button className="flex-1 gap-1.5" onClick={() => { setShowInsufficientDialog(null); navigate('/wallet'); }}>
                去充值 <ChevronRight className="size-4" />
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* 🔴 [2026-08-14] 指定投放地区弹窗 —— 加购命中列举网时自动弹,
          购物车摘要行「修改」也能再打开。
          设计要点(Owner 2026-08-14 定):
            · 只问**地区名**一个输入框;契约前缀 `列举网指定地区` 由代码拼,不给用户看;
            · 可跳过 —— 该字段供应商侧非必填,跳过 = 对方默认地区,必须说清后果,
              不能让人以为跳过就发不了(Owner 铁律:提示要么帮人解决要么不显示);
            · 不做常驻顶部入口 —— 列举网在 5 万条活跃媒体里只有 1 条。 */}
      <Dialog open={regionDialogOpen} onOpenChange={setRegionDialogOpen}>
        <DialogContent className="max-w-sm" data-testid="region-remark-dialog">
          <DialogTitle>指定投放地区</DialogTitle>
          <DialogDescription className="text-xs">
            你选的 {regionRemarkMediaNames.join('、') || '列举网'} 可以指定投放到某个地区。
            不填也能发，由对方默认分配地区。
          </DialogDescription>
          <input
            autoFocus
            data-testid="region-remark-input"
            type="text"
            maxLength={REGION_NAME_MAX_LEN}
            value={regionDraft}
            onChange={e => setRegionDraft(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') { setRegionRemark(regionDraft.trim()); setRegionDialogOpen(false); }
            }}
            placeholder="例：深圳"
            className="mt-2 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus:border-primary focus:outline-none"
          />
          <div className="mt-4 flex justify-end gap-2">
            <Button
              variant="ghost"
              size="sm"
              data-testid="region-remark-skip"
              onClick={() => { setRegionRemark(''); setRegionDialogOpen(false); }}
            >
              跳过（按对方默认地区）
            </Button>
            <Button
              size="sm"
              data-testid="region-remark-confirm"
              onClick={() => { setRegionRemark(regionDraft.trim()); setRegionDialogOpen(false); }}
            >
              确定
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {riskConfirmOpen && (
        <Suspense fallback={null}>
          <PublishRiskConfirmDialog
            open
            articleCount={cartTotalArticles}
            mediaCount={cartUniqueMediaCount || cartTotalMedia}
            // 🔴 算力直接来自 cartTotalPoints,而它是从 cart 派生的 reduce ——
            //   剔除冲突组合改的就是 cart,所以这里**结构性**自动重算,不存副本。
            estimatedPoints={cartTotalPoints}
            submitting={cartSubmitting}
            alreadyDistributed={cartAlreadyDistributed}
            conflictBlock={conflictBlock}
            remainingAfterRemoval={
              conflictBlock ? remainingAfterRemoval(cart, conflictBlock.conflicts) : 0
            }
            onRemoveConflicts={() => {
              if (!conflictBlock) return;
              const next = applyConflictRemoval(cart, conflictBlock.conflicts);
              const removed = conflictBlock.conflicts.length;
              const left = next.reduce((s, c) => s + c.mediaList.length, 0);
              // 落盘由 cart 的 useEffect 统一做(与 applyStrictMediaSwitch 那处一致),
              // 这里不重复写 localStorage —— 写两处迟早漂。
              setCart(next);
              setConflictBlock(null);
              lazyToast.success(`已去掉 ${removed} 个冲突组合，其余 ${left} 个可以继续投放`);
            }}
            onCancel={() => { setConflictBlock(null); setRiskConfirmOpen(false); }}
            onConfirm={submitCartBatchPublish}
            tutorialMode={sandboxActive}
          />
        </Suspense>
      )}

      {/* 待确认弹框（mhz 返回 confirm code 时挂起）—— 列表式 + 内联编辑器 */}
      {awaitingDialogOpen && !sandboxActive && (
        <Suspense fallback={null}>
          <AwaitingConfirmDialog
            items={awaitingItems}
            open
            onClose={() => setAwaitingDialogOpen(false)}
            onResolved={() => {
              if (mode !== 'proxy') return;
              const controller = new AbortController();
              void loadAwaitingConfirmations(controller.signal);
            }}
          />
        </Suspense>
      )}

      {/* [WO §2] 「N 条提示」明细抽屉。徽章点开 → 逐条给「AI 一键修复 / 去编辑 / 忽略」。
          修完/忽略后拿后端重算的计数回填列表徽章(计数 SSOT 在后端,前端不自算)。 */}
      <ArticleAdvisoryDrawer
        open={advisoryOpen}
        onOpenChange={setAdvisoryOpen}
        articleId={advisoryArticle?.article_id}
        topicId={advisoryArticle?.id}
        articleTitle={advisoryArticle?.title}
        onEdit={() => {
          setAdvisoryOpen(false);
          // 🔴 落到**该项目的写作大厅**(与本页既有「去写作大厅」同一条路径),
          //    不是 `?article_id=`：WritingHall 只认 quote_id/tab 两个参数,
          //    编一个它不读的参数出来 = 按钮看着能用、点了却停在默认项目上。
          navigate(`/writing?quote_id=${selectedProject || ''}`);
        }}
        onResolved={({ articleId, advisoryOpenCount, advisoryState }) => {
          setArticles(prev => prev.map(a => (
            a.article_id === articleId
              ? { ...a, advisory_open_count: advisoryOpenCount, advisory_state: (advisoryState || a.advisory_state) as ArticleItem['advisory_state'] }
              : a
          )));
        }}
      />

      {/* [WO §3] 本轮调研结果报表 / 历轮回看。roundReportId 为空 → 先进历轮列表。 */}
      <ResearchRoundReportDialog
        open={roundReportOpen}
        onOpenChange={setRoundReportOpen}
        roundId={roundReportId}
        industry={projects.find(p => p.id === selectedProject)?.industry || artRecIndustry || ''}
        brandId={projects.find(p => p.id === selectedProject)?.brand_id ?? null}
      />

      {/* [R 批 · U6] 「⚡ 点亮本行业调研」确认弹窗:免费预览 → 付费发起 → 进度轮询,完成后 bump refreshKey 重拉本行业榜 */}
      {researchDialogOpen && (
        <Suspense fallback={null}>
          <ResearchSelfserveDialog
            open
            onOpenChange={setResearchDialogOpen}
            brandId={projects.find(p => p.id === selectedProject)?.brand_id ?? null}
            industry={researchDialogIndustry}
            onCompleted={() => setResearchRefreshKey(k => k + 1)}
            // [FIX-1] 进度受控 + 轮询归父层:弹窗只是视图,关窗/刷新也能完成 toast+刷新榜。
            activeTask={activeResearchTask
              ? { taskId: activeResearchTask.taskId, status: activeResearchTask.status, overrun: activeResearchTask.overrun }
              : null}
            onStarted={(taskId, status, industryKey) =>
              setActiveResearchTask(prev =>
                prev?.taskId === taskId
                  ? prev
                  : { taskId, status, overrun: false, industryKey: industryKey || researchDialogIndustry })
            }
          />
        </Suspense>
      )}

      {/* ===== 沙盒 step 3 完成 · 视频占位 modal ===== */}
      <Dialog open={sandboxVideoModalOpen} onOpenChange={setSandboxVideoModalOpen}>
        <DialogContent className="max-w-2xl">
          <DialogTitle className="text-lg flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-amber-500" />
            这就是发文章的最终效果
          </DialogTitle>
          <DialogDescription className="text-sm mt-2 leading-6">
            刚才发的 6 篇文章被各平台收录后, AI 就会"读到"它们. 下面是真实案例: 在 AI 里搜这两个词, 客户的公司都被 AI 主动推荐了出来.
          </DialogDescription>

          <div className="my-4">
            <video
              src="/sandbox/demo-ranking-case.mp4"
              controls
              autoPlay
              muted
              playsInline
              className="w-full rounded-lg border border-border bg-black"
            />
            <div className="mt-2 text-xs text-muted-foreground leading-5 text-center">
              真实录屏 · 在 AI 里搜 <strong>"{SANDBOX_MAGIC_KEYWORDS[0]}"</strong> 和 <strong>"{SANDBOX_MAGIC_KEYWORDS[1]}"</strong>, 客户的公司都被推荐了出来
            </div>
          </div>

          <div className="text-xs text-muted-foreground mb-3 leading-5">
            <strong>下一步:</strong> 想知道你的词有没有被 AI 推荐? 第四步加监测词, 每天帮你盯着排名变化.
          </div>

          <div className="flex justify-end">
            <Button
              className="bg-amber-500 hover:bg-amber-600 text-white"
              onClick={() => {
                setSandboxVideoModalOpen(false);
                // 沙盒: 走到这一步 (一键发布成功 + 看完视频占位) 才算 step 3 真完成
                markStep('first_article_publish');
                // 切到 step4-sidebar · 引导用户点排名监测
                import('@/sandbox/tutorialStage').then(({ setTutorialStage }) => {
                  setTutorialStage('step4-sidebar');
                });
              }}
            >
              <Send className="h-4 w-4 mr-1.5" />
              去看效果 (第四步)
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ========== 媒体卡片 ==========
function MCard({ m, sel, top, d, first, onT }: { m: MediaItem; sel: boolean; top?: boolean; d: number; first: boolean; onT: () => void }) {
  const pts = Math.round(m.our_price_points * d);
  return (
    <div onClick={onT} className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${sel ? 'border-primary/50 bg-primary/5' : 'border-border hover:bg-secondary/30'}`}>
      <Checkbox checked={sel} onClick={(e) => e.stopPropagation()} onCheckedChange={onT} className="mt-0.5 size-3.5" />
      <div className="flex-1 min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium break-words leading-snug" title={m.media_name}>{m.media_name}</span>
          {top && <Badge className="text-[10px] px-1.5 py-0 bg-primary/10 text-primary border-primary/20">推荐</Badge>}
          {m.can_geo === 1 && <Badge variant="outline" className="text-[10px] px-1 py-0 text-green-600 border-green-300">GEO</Badge>}
        </div>
        <div className="flex flex-wrap gap-x-2 gap-y-0.5 mt-1 text-[10px] text-muted-foreground">
          {m.category && <span>{m.category}</span>}
          {m.news_source && <span>{m.news_source}</span>}
          {m.inclusion_rate && <span>收录{m.inclusion_rate}</span>}
          {m.avg_publish_time && <span>出稿用时：{publicationTime(m.avg_publish_time)}</span>}
          {m.pc_weight > 0 && <span>PC:{m.pc_weight}</span>}
          {m.mobile_weight > 0 && <span>M:{m.mobile_weight}</span>}
          {m.link_type && <span>{m.link_type}</span>}
        </div>
        {m.engines && m.engines.length > 0 && (
          <div className="flex gap-1 mt-1">{m.engines.slice(0, 4).map(e => <Badge key={e} variant="outline" className="text-[10px] px-1 py-0">{e}</Badge>)}</div>
        )}
        {m.reason && <div className="text-[10px] text-muted-foreground mt-1">{m.reason}</div>}
        {/* Phase 4.2 · MCard mobile 备注接 ExpandableMeta · 跟桌面表格备注列双端真统一 */}
        {m.remark && !m.reason && (
          <div className="mt-1" onClick={(e) => e.stopPropagation()}>
            <ExpandableMeta text={m.remark} mode="preview" maxChars={30} iconLabel="备注" />
          </div>
        )}
      </div>
      <div className="text-right shrink-0">
        {first && d < 1 && <div className="text-[10px] text-muted-foreground line-through">{m.our_price_points.toLocaleString()}</div>}
        <div className="text-sm font-semibold">{pts.toLocaleString()}</div>
        <div className="text-[10px] text-muted-foreground">算力</div>
      </div>
    </div>
  );
}

// ========== 外部发布媒体卡片 ==========
function MhzCard({ m, sel, onT }: { m: MhzMediaItem; sel: boolean; onT: () => void }) {
  return (
    <div onClick={onT} className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${sel ? 'border-primary/50 bg-primary/5' : 'border-border hover:bg-secondary/30'}`}>
      <div role="checkbox" aria-checked={sel} onClick={(e) => { e.stopPropagation(); onT(); }} className="flex items-center justify-center -m-1.5 p-1.5 cursor-pointer rounded hover:bg-primary/10">
        <Checkbox checked={sel} className="mt-0.5 size-3.5 pointer-events-none" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-medium break-words leading-snug" title={m.media_name}>{m.media_name}</span>
          {/* [CTO-15.23 2026-05-05] 跟桌面表格 V/GEO badge 对齐(原 mobile 卡片漏了) */}
          {m.authority_media === 1 && <span className="shrink-0 px-1 py-0 text-[9px] rounded bg-blue-500/20 text-blue-400 font-bold">V</span>}
          {m.geo_rank > 0 && <span className="shrink-0 px-1 py-0 text-[9px] rounded bg-green-500/20 text-green-400 font-bold">GEO</span>}
          {/* [CTO-15.23 2026-05-18 v2-F] 真权威 4 件套对齐桌面 */}
          <GeoEngineBadge geoRankPlatform={m.geo_rank_platform} />
          <SweetSpotBadge sweetSpot={!!m.is_sweet_spot} geoRankPlatform={m.geo_rank_platform} />
          {m.area && <Badge variant="secondary" className="text-[10px] px-1 py-0">{m.area}</Badge>}
          {m.resource_type && <Badge variant="secondary" className="text-[10px] px-1 py-0">{m.resource_type}</Badge>}
          {m.news_resource && <Badge variant="outline" className="text-[10px] px-1 py-0">{m.news_resource}</Badge>}
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 mt-1 text-[10px] text-muted-foreground">
          {m.inclusion_rate && <span>收录率: {m.inclusion_rate}</span>}
          {m.avg_time && <span>出稿用时：{formatAvgTime(m.avg_time)}</span>}
          {m.pc_weight > 0 && <span>PC权重: {m.pc_weight}</span>}
          {m.m_weight > 0 && <span>移动权重: {m.m_weight}</span>}
          {m.link_type && <span>链接: {m.link_type}</span>}
        </div>
        {/* Phase 4.2 · MhzCard mobile 备注接 ExpandableMeta · 跟桌面表格备注列双端真统一 */}
        {m.remark && (
          <div className="mt-1" onClick={(e) => e.stopPropagation()}>
            <ExpandableMeta text={m.remark} mode="preview" maxChars={30} iconLabel="备注" />
          </div>
        )}
      </div>
      <div className="text-right shrink-0">
        <div className="text-sm font-semibold text-primary">{(m.price_points || 0).toLocaleString()} 算力</div>
      </div>
    </div>
  );
}

function WmCard({ m, sel, onT }: { m: any; sel: boolean; onT: () => void }) {
  const name = m.toutiao_name || m.media_name || '未命名媒体';
  const points = m.price_points || 0;
  return (
    <div onClick={onT} className={cn('flex items-start gap-3 rounded-lg border p-3 cursor-pointer transition-colors', sel ? 'border-primary/50 bg-primary/5' : 'border-border hover:bg-secondary/30')}>
      <Checkbox checked={sel} onClick={(e) => e.stopPropagation()} onCheckedChange={onT} className="mt-0.5 size-3.5 shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          {WM_PLATFORM_LOGOS[m.platform] ? (
            <img src={WM_PLATFORM_LOGOS[m.platform]} alt="" className="size-6 shrink-0 rounded object-contain"
              onError={(e) => { const t = e.currentTarget; t.style.display = 'none'; t.nextElementSibling?.classList.remove('hidden'); }} />
          ) : null}
          <span className={cn('size-6 shrink-0 rounded flex items-center justify-center text-[10px] text-white font-bold', WM_PLATFORM_LOGOS[m.platform] ? 'hidden' : '', WM_PLATFORM_COLORS[m.platform] || 'bg-zinc-500')}>
            {m.platform?.charAt(0) || '?'}
          </span>
          <span className="text-sm font-medium break-words leading-snug" title={name}>{name}</span>
          {m.authority_media === 1 && <Badge variant="outline" className="px-1 py-0 text-[10px] text-blue-400 border-blue-500/30">认证</Badge>}
          {m.geo_rank > 0 && <Badge variant="outline" className="px-1 py-0 text-[10px] text-green-400 border-green-500/30">GEO</Badge>}
        </div>
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          {m.platform && <span>{m.platform}</span>}
          {m.industry && <span>{m.industry}</span>}
          {m.p_rate && <span>出稿率 {m.p_rate}</span>}
          {m.avg_time && <span>出稿 {formatAvgTime(m.avg_time)}</span>}
          {m.fans_num > 0 && <span>粉丝 {fmtFans(m.fans_num)}</span>}
        </div>
        {/* Phase 4.2 · WmCard mobile 备注接 ExpandableMeta · 跟桌面表格备注列双端真统一 */}
        {m.remark && (
          <div className="mt-1" onClick={(e) => e.stopPropagation()}>
            <ExpandableMeta text={m.remark} mode="preview" maxChars={30} iconLabel="备注" />
          </div>
        )}
      </div>
      <div className="shrink-0 text-right">
        <div className="text-sm font-semibold text-primary">{points.toLocaleString()}</div>
        <div className="text-[10px] text-muted-foreground">算力</div>
      </div>
    </div>
  );
}
