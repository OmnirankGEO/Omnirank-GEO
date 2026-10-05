// E2 · 发布流「本行业 AI 真实引用媒体榜」(代理端鉴权,非 admin)。
//  数据源:GET /api/publish/media/effectiveness-board —— 与 admin E1 同 Row 结构,同综合有效分口径。
//  规则:无数据不显示该模块(has_data=false / 空 rows → 返回 null,不占位不报错)。
//  「一键加入方案」诚实说明:后端 Row 只给「实体名 + 有效分 + 可投放绑定态」,不含 media_id / inventory_id /
//   报价,无法直接写进现有 media_id 驱动的购物车。故此处做「展示 + 可投放标记 + 引导到媒体列表按名称加入」,
//   不伪造加购动作(避免把无 id 的实体塞进 cart 造成脏数据)。
//  [07-05 布局重排] 本组件挂在发布中心左栏底部(文章列表下方),媒体列表在右栏 —— 文案不带方位词。
import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, History, Loader2, RefreshCw, Trophy, Zap } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

type PanelEvidence = {
  '点名推荐次数'?: number;
  '平均排名'?: number | null;
  '引擎分布'?: string[];
  '媒体有效分'?: number | null;
  '可投放'?: string; // "已绑定" | "参考/待拓展"
};

type BoundMedia = {
  source: string; // 'media'(软文库)| 'wemedia'(自媒体库)
  name: string;   // 媒体库真名(点击引导用它搜索最准)
};

type PanelRow = {
  entity_key: string;
  entity_name: string;
  display_name?: string; // 中文展示名(绑定库真名 > 域名中文映射 > 原名)
  // [WO_MEDIA_BOARD_UX_CLOSURE §1] 一句话简介(后端域名目录 media_domain_directory 出)。
  // 有就 hover 出来 —— 代理看到「财富赢家网」也未必知道那是什么站。
  one_liner?: string;
  domain?: string;
  entity_type?: string;
  effectiveness_score?: number;
  mention_count?: number;
  vertical?: boolean; // 垂类特荐:本行业引用集中度显著高于全网基线(排序已上浮)
  is_platform?: boolean; // UGC 号生态平台:点击用平台名搜(出全部账号),不锁定绑定的单账号
  reasons?: string[]; // 数据说话:「答案采纳 N 次」「被 AI 明确引用 N 次」「覆盖 N 个引擎」
  bound_media?: BoundMedia[]; // 各库绑定明细,未绑定 → []
  evidence?: PanelEvidence;
};

type GeneralRow = {
  entity_key: string;
  display_name?: string;
  one_liner?: string; // [§1] 同上,后端域名目录出
  domain?: string;
  effectiveness_score?: number;
};

// [BUG-5 · T5 2026-07-27] 「发布 → 被引」转化率。后端 /media/effectiveness-board 早就返回了
// publish_to_citation,但前端全仓零消费 → 工单 T5「垂类去留用数据自证」根本做不到。
// 连接方式是我们发出去的那条 URL 本身(publish_url = 飞轮 geo_research_articles.url),
// 不是域级近似 —— 域级会把别人在同域发的文章算成我们的战果。
// 现状可能很难看(全部发布史仅 1 条 URL 拿到 1 次被引),但这正是要摆上台面的东西。
type ConversionMedia = {
  media_name?: string;
  published?: number;
  cited_articles?: number;
  citations?: number;
  citation_rate_pct?: number | null;
};

type PublishToCitation = {
  window_days?: number;
  degraded_reason?: string;
  total_published?: number;
  total_cited?: number;
  total_citations?: number;
  overall_citation_rate?: number | null;
  media?: ConversionMedia[];
};

type PanelResponse = {
  status?: string;
  has_data?: boolean;
  rows?: PanelRow[];
  publish_to_citation?: PublishToCitation | null;
  // 本行业榜下的「全网通用头部」小段(搜狐/知乎/百度系等任何行业都常被引用的大站)
  general_rows?: GeneralRow[];
  // 'industry' 本行业 | 'near_industry' 本行业无数据退相近行业(需明示) | 'all_industry_fallback' 退全行业 | 'all_industry'
  scope?: string;
  near_industry_key?: string; // scope='near_industry' 时命中的相近行业 key(内部)
  near_industry_label?: string; // 相近行业人话名(下划线还原 · 优先用于展示)
};

const ENGINE_LABEL: Record<string, string> = {
  deepseek: 'DeepSeek',
  'deepseek-v3.2': 'DeepSeek',
  kimi: 'Kimi',
  'kimi-k2': 'Kimi',
  doubao: '豆包',
  doubao_app: '豆包',
  qwen: '通义千问',
  'qwen3-max': '通义千问',
};

function engineLabel(e: string) {
  return ENGINE_LABEL[e] || e;
}

function isPurchasable(row: PanelRow) {
  return (row.evidence?.['可投放'] || '') === '已绑定';
}

function scoreText(v?: number) {
  const num = Number(v || 0);
  if (!Number.isFinite(num)) return '0';
  return String(Math.round(num));
}

const SOURCE_LABEL: Record<string, string> = { media: '软文', wemedia: '自媒体' };

const GENERAL_MEDIA_LABELS: Record<string, string> = {
  'bohe.cn': '薄荷健康',
  'china.com': '中华网',
  'tech.china.com': '中华网',
  'mtz.china.com': '中华网',
  'ifeng.com': '凤凰网',
  'news.ifeng.com': '凤凰网',
  'gaokao.eol.cn': '中国教育在线',
  'pcauto.com.cn': '太平洋汽车网',
  'caifuhao.eastmoney.com': '东方财富号',
  'eastmoney.com': '东方财富',
  'ithome.com': 'IT之家',
  'chejiahao.autohome.com.cn': '车家号',
  'autohome.com.cn': '汽车之家',
};

function normalizeDomain(value?: string) {
  return (value || '')
    .trim()
    .toLowerCase()
    .replace(/^https?:\/\//, '')
    .replace(/^www\./, '')
    .split('/')[0];
}

function mediaLabelFromDomain(value?: string) {
  const domain = normalizeDomain(value);
  if (!domain) return '';
  if (GENERAL_MEDIA_LABELS[domain]) return GENERAL_MEDIA_LABELS[domain];
  const suffix = Object.keys(GENERAL_MEDIA_LABELS).find((key) => domain.endsWith(`.${key}`));
  return suffix ? GENERAL_MEDIA_LABELS[suffix] : '';
}

function looksLikeDomain(value: string) {
  const domain = normalizeDomain(value);
  return /^[a-z0-9.-]+\.[a-z]{2,}$/.test(domain);
}

function generalMediaDisplayName(row: GeneralRow) {
  const display = (row.display_name || '').trim();
  const domain = (row.domain || row.entity_key || '').trim();
  const mapped = mediaLabelFromDomain(display) || mediaLabelFromDomain(domain);
  if (mapped && (!display || looksLikeDomain(display) || display === domain)) return mapped;
  return display || mapped || domain;
}

/** 点击目标(Codex P1):优先当前 tab 的绑定 → 任一库绑定(带 source 让上层切对应 tab)
 *  → 未绑定退实体名括号前主体模糊搜(「知乎(知+)」→「知乎」)。 */
function clickTarget(row: PanelRow, activeTab: string): { name: string; source?: string } {
  const list = (row.bound_media || []).filter((b) => b && (b.name || '').trim());
  const hit = list.find((b) => b.source === activeTab) || list[0];
  if (hit) {
    // UGC 平台行:用平台名搜(出该平台下全部可买账号,代理挑对口的),不锁定绑定的
    // 单账号——账号有自己的垂直属性,可能与当前行业不对口(建筑装饰 ≠ 化妆品推荐官)。
    // 绑定库别(source)仍有效,保留用于切对 tab。独立媒体行:绑定名 = 该资源真名,最准。
    if (row.is_platform) {
      const platform = (row.display_name || '').split(/[(（]/)[0].trim();
      if (platform) return { name: platform, source: hit.source };
    }
    return { name: hit.name.trim(), source: hit.source };
  }
  // 未绑定:中文展示名比域名在媒体库里好搜得多(「搜狐新闻」vs「news.sohu.com」)。
  const name = (row.display_name || row.entity_name || '').trim();
  const head = name.split(/[(（]/)[0].trim();
  return { name: head || name };
}

export function MediaEffectivenessPanel({
  industry,
  activeTab = 'media',
  onFindMedia,
  onStartResearch,
  refreshKey,
  onOpenRounds,
  brandId,
}: {
  industry?: string;
  /** [WO_267] 带上品牌:主榜按品牌上下文判大类(已鉴权才吃,无权照旧不 403) */
  brandId?: number | null;
  /** 当前媒体列表 tab:'media'(软文)|'wemedia'(自媒体)。点击引导优先搜当前库的绑定。 */
  activeTab?: 'media' | 'wemedia';
  /** 点击榜行 → 引导到媒体列表搜索。source 有值 = 绑定库别(与当前 tab 不同时上层应切 tab 再搜)。
   *  [§1 点击闭环] meta 带上这一行的域名与一句话简介:搜不到时上层要给**显式空态**
   *  (「该站为 AI 常引用头部站点,媒体库暂无对应可购渠道」),而不是一句通用的"无匹配媒体"
   *  —— 后者会让代理以为是自己搜错了字。meta 可选,老调用方不传行为不变。 */
  onFindMedia?: (name: string, source?: string, meta?: { domain?: string; oneLiner?: string }) => void;
  /** [R 批 · U6] 「⚡ 点亮本行业调研」入口。传入即在空态/fallback 态显示 CTA,点击由上层拉起确认弹窗。 */
  onStartResearch?: (industry: string) => void;
  /** [R 批 · U6] 付费完成后父层 bump 此值 → 本组件重新拉榜(无客户端缓存,后端已 invalidate)。 */
  refreshKey?: number;
  /** [§3.3 历史可回看] 打开「历轮调研记录」。传入即渲染入口 ——
   *  花过的算力永远找得回凭证,不能只有完成那一刻的一句 toast。 */
  onOpenRounds?: () => void;
}) {
  const [rows, setRows] = useState<PanelRow[]>([]);
  const [generalRows, setGeneralRows] = useState<GeneralRow[]>([]);
  // [BUG-5 · T5] 数据已在同一个 response 里,解包即可 —— 零新增请求。
  const [conversion, setConversion] = useState<PublishToCitation | null>(null);
  const [hasData, setHasData] = useState(false);
  const [scope, setScope] = useState('');
  const [nearIndustry, setNearIndustry] = useState('');
  const [loading, setLoading] = useState(false);
  // [FIX-2 状态机] loadError:一次 load 抛错置 true、成功置 false。错误态独立渲染「加载不出来·重试」,
  // 【绝不显示付费 CTA、绝不 return null 消失】,与真空态(has_data=false)严格区分。
  const [loadError, setLoadError] = useState(false);
  // [FIX-2 状态机 · 取代旧 loadedOnce] settledInd = 最近一次"已结算"load 对应的行业名。
  // 渲染以 settledInd === 当前行业 为闸:① 首次/切行业未回前 → null(不闪空态,承接旧 #14 守卫)
  // ② 行业 A→B 快切时,B 未结算前不拿 A 的旧帧硬套 B 标题 ③ 同行业 refresh(付费后 bump)期间
  // settledInd 仍等于当前行业 → 保留上一帧不闪没。空字符串 = 尚未结算任何行业。
  const [settledInd, setSettledInd] = useState('');
  // [FIX-2 竞态守卫] 每次 load 自增;await 后写 state 前比对,行业快切时旧行业旧响应一律丢弃。
  const seqRef = useRef(0);
  // [低分辨率适配 · 老板 07-05] 矮视口(1366×768 笔记本/小窗口)默认收起整个榜,
  // 把纵向空间还给下方筛选和媒体列表;标题行保留一行随时点开。高视口默认展开。
  const [open, setOpen] = useState<boolean>(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return true;
    return !window.matchMedia('(max-height: 720px)').matches;
  });
  // 展开态默认只显前 5 行(榜行带理由两层高,30 行在任何分辨率都太吃纵向空间)。
  const [showAll, setShowAll] = useState(false);

  const load = useCallback(async () => {
    const ind = (industry || '').trim();
    // [FIX-2] 入口自增序号:此后任何 await 落地、写 state 前都比对 seqRef,序号变了即丢弃
    // (行业已切 / 已重拉),旧行业的旧帧不许挂到新标题下。
    const seq = ++seqRef.current;
    if (!ind) {
      // 早退(无行业):清掉上一行业全部残帧,与正常路径保持一致。否则旧 scope=fallback、
      // 旧 generalRows 残留会在后续恢复渲染时把标题/内容套错。无 await → 无需序号守卫。
      setRows([]);
      setGeneralRows([]);
      setConversion(null);
      setScope('');
      setHasData(false);
      setLoadError(false);
      setSettledInd('');
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const q = new URLSearchParams({ industry: ind, weeks: '4', limit: '30' });
      if (brandId) q.set('brand_id', String(brandId));
      const res = await authFetch(`/api/publish/media/effectiveness-board?${q}`);
      if (seq !== seqRef.current) return; // 行业已切,旧响应作废
      if (!res.ok) throw new Error('load failed');
      const data = (await res.json()) as PanelResponse;
      if (seq !== seqRef.current) return; // await json 后再次守卫
      const list = (data?.rows || []).filter(Boolean);
      setRows(list);
      setGeneralRows((data?.general_rows || []).filter(Boolean));
      setConversion(data?.publish_to_citation ?? null);
      setScope(data?.scope || '');
      setNearIndustry(data?.near_industry_label || data?.near_industry_key || '');
      setHasData(!!data?.has_data && list.length > 0);
      setLoadError(false); // 成功 → 清错误态
      setSettledInd(ind);
    } catch {
      if (seq !== seqRef.current) return; // 旧行业的失败不许覆盖新行业
      // fail-soft:发布流是核心链路,榜单只是辅助,失败不阻断代理发布;
      // 但不再静默隐藏(return null),改为可重试错误态(不显付费 CTA)。
      setRows([]);
      setGeneralRows([]);
      setConversion(null);
      setScope(''); // catch 也重置 scope,否则残留 all_industry_fallback 致恢复渲染标题错乱
      setHasData(false);
      setLoadError(true);
      setSettledInd(ind);
    } finally {
      // 旧序号不许关掉 loading(新请求还在飞),用 if 而非 return 避免 no-unsafe-finally。
      if (seq === seqRef.current) setLoading(false);
    }
  }, [industry, brandId]);

  // [R 批 · U6] refreshKey 变化(付费完成)→ 强制重拉,拿点亮后的真·本行业榜。
  useEffect(() => {
    load();
  }, [load, refreshKey]);

  // ===== [FIX-2] 榜面板请求状态机:idle→loading→ready|true_empty|error→refreshing =====
  // 渲染以 settledInd(最近一次结算的行业)为闸,严格区分 error / 真空 / 未结算三态,
  // 任何一态都不再无差别 return null 消失,行业快切时旧帧也不挂到新标题下。
  const ind = (industry || '').trim();
  const settledForInd = ind !== '' && settledInd === ind;
  const hasBoard = hasData && rows.length > 0;

  // (a) 错误态:独立可重试条。【绝不显示付费 CTA、绝不 return null 消失】——传/不传 onStartResearch 都显。
  // 重试期间(loading)显 spinner + 禁用按钮,点击重跑 load。
  if (settledForInd && loadError) {
    return (
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b-2 border-border/40 bg-muted/20 px-3 py-2.5 sm:px-4">
        <Trophy className="size-4 text-muted-foreground" />
        <span className="text-xs text-muted-foreground">榜单暂时加载不出来</span>
        {loading && <Loader2 className="size-3 animate-spin text-muted-foreground" />}
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={loading}
          onClick={() => load()}
          className="ml-auto h-7 gap-1 px-2.5 text-xs"
        >
          <RefreshCw className="size-3.5" />
          点此重试
        </Button>
      </div>
    );
  }

  // (b) 真空态:后端 has_data=false,本行业确实还没跑过调研(区别于 error)。不 return null 消失。
  // 传 onStartResearch → 显点亮 CTA;不传 → 纯文案空态(仍常驻,不依赖 onStartResearch)。
  // refresh(付费后 refreshKey bump,settledInd 仍等于当前行业)期间此态 + spinner 保留,数据回来再切榜
  // ——正是「空态→点亮」主场景,不闪没。
  if (settledForInd && !hasBoard) {
    return (
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b-2 border-emerald-500/20 bg-emerald-500/5 px-3 py-2.5 sm:px-4">
        <Trophy className="size-4 text-emerald-400" />
        <span className="text-xs text-muted-foreground">还没跑过「{industry}」的 AI 调研,榜单是空的</span>
        {loading && <Loader2 className="size-3 animate-spin text-muted-foreground" />}
        {/* [§3.3] 空态也给历轮入口:跑过但失败/归并进已有行业的轮次,凭证同样要找得回。 */}
        {onOpenRounds && (
          <button
            type="button"
            onClick={onOpenRounds}
            className="flex items-center gap-1 rounded border border-border/60 px-1.5 py-0.5 text-[10px] text-muted-foreground transition-colors hover:border-emerald-400/50 hover:text-foreground"
            data-testid="media-board-rounds-entry-empty"
          >
            <History className="size-3" />
            历轮调研
          </button>
        )}
        {onStartResearch && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={loading}
            onClick={() => onStartResearch(ind)}
            className="ml-auto h-7 gap-1 border-emerald-500/40 px-2.5 text-xs text-emerald-300 hover:bg-emerald-500/10 hover:text-emerald-200"
          >
            <Zap className="size-3.5" />
            点亮本行业调研
          </Button>
        )}
      </div>
    );
  }

  // (c) 未结算当前行业(首次 load 未回 / 切行业加载中)→ 中性 null,不闪空态、不套旧帧(承接旧 #14 守卫)。
  if (!settledForInd) return null;

  // 到这里必为:settledForInd && !loadError && hasBoard(有榜正文;真空/错误已在上面拦截)。
  // [整合保留 · 8e36430e] 排序 SSOT = 后端(get_publish_media_board:可投放→垂类特荐→有效分→引用量)。
  // 前端二次重排只按「可投放→有效分」会吃掉后端的「垂类特荐」层和「引用量」tiebreak → 直接信后端顺序。
  const sorted = rows;
  const purchasableCount = sorted.filter(isPurchasable).length;
  // 本行业无调研数据 → 后端退相近行业(near)或全行业(fallback);标题/文案如实改口,绝不冒充本行业。
  const isNear = scope === 'near_industry';
  const isFallback = scope === 'all_industry_fallback';
  const COLLAPSED_ROWS = 5;
  const visibleRows = showAll ? sorted : sorted.slice(0, COLLAPSED_ROWS);
  // [BUG-5 · T5] 只展示真发过的媒体(published>0);没有任何发布记录时整段不渲染(fail-soft)。
  // degraded_reason 非空 = 后端聚合降级,数据不可信,同样不展示(不给假数)。
  const conversionRows = (conversion && !conversion.degraded_reason ? conversion.media || [] : [])
    .filter((m) => m && Number(m.published || 0) > 0)
    .slice(0, 5);
  const overallRatePct =
    typeof conversion?.overall_citation_rate === 'number'
      ? Math.round(conversion.overall_citation_rate * 1000) / 10
      : null;

  return (
    // [07-05 布局二修] 单一滚动容器 flex-col:头部常驻(shrink-0)+ 正文独立滚动(flex-1 min-h-0)。
    // 根 max-h 收敛整体高度:内容短→自适应不留空白;内容长→正文内滚,头部标题始终可见。
    // 不再套外层 overflow(双层抢滚轮=划不动的根因)。
    <div className="flex max-h-[44dvh] flex-col overflow-hidden border-b-2 border-emerald-500/20 bg-emerald-500/5">
      <div
        className="flex shrink-0 cursor-pointer flex-wrap items-center gap-2 px-3 py-2.5 sm:px-4"
        onClick={() => setOpen((p) => !p)}
      >
        <Trophy className="size-4 text-emerald-400" />
        <span className="text-sm font-medium">{isNear ? '相近行业 AI 真实引用媒体榜' : isFallback ? 'AI 真实引用媒体榜(全行业)' : '本行业 AI 真实引用媒体榜'}</span>
        <Badge variant="secondary" className="px-1.5 py-0 text-[10px]">{industry}</Badge>
        {isNear && (
          <Badge className="border-amber-500/30 bg-amber-500/15 px-1.5 py-0 text-[10px] text-amber-300">
            本行业数据积累中 · 相近行业{nearIndustry ? `「${nearIndustry}」` : ''}参考
          </Badge>
        )}
        {isFallback && (
          <Badge className="border-amber-500/30 bg-amber-500/15 px-1.5 py-0 text-[10px] text-amber-300">
            本行业数据积累中 · 全行业参考
          </Badge>
        )}
        {purchasableCount > 0 && (
          <Badge className="border-emerald-500/30 bg-emerald-500/15 px-1.5 py-0 text-[10px] text-emerald-300">
            {purchasableCount} 家可投放
          </Badge>
        )}
        {loading && <Loader2 className="size-3 animate-spin text-muted-foreground" />}
        {/* [§3.3] 历轮调研入口。stopPropagation:它是标题行的子节点,不点开/收起榜。 */}
        {onOpenRounds && (
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); onOpenRounds(); }}
            className="ml-auto flex items-center gap-1 rounded border border-border/60 px-1.5 py-0.5 text-[10px] text-muted-foreground transition-colors hover:border-emerald-400/50 hover:text-foreground"
            data-testid="media-board-rounds-entry"
          >
            <History className="size-3" />
            历轮调研
          </button>
        )}
        <span className={cn('flex items-center gap-1 text-[10px] text-muted-foreground', !onOpenRounds && 'ml-auto')}>
          {open ? '收起' : '展开'}
          {open ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
        </span>
      </div>

      {/* [R 批 · #11] fallback(全行业退榜)态的「点亮本行业调研」CTA 提到常驻区(不随折叠体隐藏):
          矮视口(≤720px)整榜默认收起,唯一入口若埋在 {open && …} 折叠体里在 1366×768 就看不到。
          这是 fallback 态最常用的主入口,折叠态也必须常驻可见;是 header 的兄弟节点,点击不触发折叠。 */}
      {isFallback && onStartResearch && (industry || '').trim() && (
        <div className="shrink-0 px-3 pb-2 sm:px-4">
          <button
            type="button"
            onClick={() => onStartResearch((industry || '').trim())}
            className="flex w-full items-center justify-center gap-1.5 rounded-md border border-emerald-500/40 bg-emerald-500/10 py-1.5 text-[11px] font-medium text-emerald-300 transition-colors hover:bg-emerald-500/20 hover:text-emerald-200"
          >
            <Zap className="size-3.5" />
            现在展示的是全行业参考榜 · 点亮后升级为你行业的专属榜
          </button>
        </div>
      )}

      {open && (
        // 正文独立滚动区:flex-1 填满头部下方剩余(受根 max-h-44dvh 收敛),min-h-0 才能内滚。
        // 头部是 flex 兄弟节点(非本容器内),故滚动时天然常驻,无需 sticky。
        <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto px-3 pb-3 sm:px-4">
          <p className="text-[10px] leading-relaxed text-muted-foreground">
            {isNear
              ? `本行业调研数据还在积累中,先展示相近行业${nearIndustry ? `「${nearIndustry}」` : ''}被 AI 搜索引擎真实引用的媒体榜作参考。`
              : isFallback
              ? '本行业调研数据还在积累中,先展示全行业被 AI 搜索引擎真实引用的媒体榜作参考。'
              : '这些媒体在本行业被 AI 搜索引擎真实引用为答案来源,综合有效分越高越值得投放。'}
            <span className="text-emerald-400">「可投放」</span>的可在媒体列表按名称查找并加入方案。
            {onFindMedia && <span className="text-emerald-400">点击任意一行即可在媒体列表搜索。</span>}
          </p>
          {/* [#11] fallback 点亮 CTA 已提到头部常驻区(见上),此处不再重复渲染,避免展开态两颗按钮。 */}
          {visibleRows.map((row, idx) => {
            const evidence = row.evidence || {};
            const engines = (evidence['引擎分布'] || []).filter(Boolean);
            const purchasable = isPurchasable(row);
            const target = clickTarget(row, activeTab);
            // [Codex P2] 「可投放」标注库别:软文 tab 上看到自媒体库的绑定不再误以为软文资源。
            const boundSources = (row.bound_media || []).map((b) => b?.source).filter(Boolean);
            const purchasableLabel = purchasable && boundSources.length > 0
              ? `可投放·${boundSources.map((s) => SOURCE_LABEL[s] || s).join('/')}`
              : evidence['可投放'];
            return (
              <div
                key={row.entity_key}
                /* [§1] hover 先说这是个什么站(one_liner),再说点击会发生什么。
                   目录没覆盖到 → one_liner 为空 → 退化为原来那句,行为不变。 */
                title={[
                  (row.one_liner || '').trim(),
                  onFindMedia ? `点击在媒体列表搜索「${target.name}」${target.source && target.source !== activeTab ? `(在${SOURCE_LABEL[target.source] || target.source}库,会自动切换)` : ''}` : '',
                ].filter(Boolean).join('\n') || undefined}
                onClick={onFindMedia ? () => onFindMedia(target.name, target.source, { domain: row.domain, oneLiner: row.one_liner }) : undefined}
                className={cn(
                  'flex items-start gap-2 rounded-md border px-2.5 py-1.5 text-xs',
                  purchasable
                    ? 'border-emerald-500/30 bg-emerald-500/10'
                    : 'border-border/60 bg-background/60',
                  onFindMedia && 'cursor-pointer transition-colors hover:border-emerald-400/50 hover:bg-emerald-500/15',
                )}
              >
                <span className="mt-0.5 w-4 shrink-0 text-[10px] font-semibold text-muted-foreground">{idx + 1}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="font-medium break-words leading-snug">{row.display_name || row.entity_name}</span>
                    {row.domain && (row.display_name || row.entity_name) !== row.domain && (
                      <span className="text-[9px] text-muted-foreground/70">{row.domain}</span>
                    )}
                    {row.vertical && (
                      <Badge className="border-amber-500/30 bg-amber-500/15 px-1 py-0 text-[9px] text-amber-300">本行业特荐</Badge>
                    )}
                    <Badge
                      variant="secondary"
                      className="px-1 py-0 text-[9px]"
                      title="AI 推荐指数:综合被 AI 引用的证据、内容质量、可投放匹配度和发布后效果,越高越值得投放"
                    >
                      有效分 {scoreText(row.effectiveness_score)}
                    </Badge>
                    {Number(row.mention_count || 0) > 0 && (
                      <span className="text-[10px] text-muted-foreground">被推荐 {Math.round(Number(row.mention_count))} 次</span>
                    )}
                  </div>
                  {(row.reasons || []).length > 0 && (
                    <div className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground">
                      {(row.reasons || []).join(' · ')}
                    </div>
                  )}
                  {engines.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {engines.map((e) => (
                        <span key={e} className="rounded border border-border/60 bg-muted/40 px-1 py-0 text-[9px] leading-4 text-muted-foreground">
                          {engineLabel(e)}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                {evidence['可投放'] && (
                  <Badge
                    variant={purchasable ? 'default' : 'secondary'}
                    className={cn('shrink-0 px-1.5 py-0 text-[9px]', purchasable && 'border-emerald-500/30 bg-emerald-500/20 text-emerald-300')}
                  >
                    {purchasableLabel}
                  </Badge>
                )}
              </div>
            );
          })}

          {sorted.length > COLLAPSED_ROWS && (
            <button
              type="button"
              onClick={() => setShowAll((p) => !p)}
              className="w-full rounded border border-dashed border-border/60 py-1 text-center text-[10px] text-muted-foreground transition-colors hover:border-emerald-400/40 hover:text-foreground"
            >
              {showAll ? `收起 · 只看前 ${COLLAPSED_ROWS}` : `展开全部 ${sorted.length} 家`}
            </button>
          )}

          {/* [两段式 · 2026-07-05 老板拍板] 全网通用头部:任何行业都常被 AI 引用的大站,
              不进本行业主榜(避免淹没垂类)但单独一段可见,点击同样引导下方搜索。 */}
          {(scope === 'industry' || isNear || isFallback || scope === 'all_industry') && generalRows.length > 0 && (
            <div className="mt-2 border-t border-border/40 pt-2">
              <div className="mb-1 text-[10px] font-medium text-muted-foreground">
                全网通用头部 · 任何行业都常被 AI 引用
              </div>
              <div className="flex flex-wrap gap-1.5">
                {generalRows.map((g) => {
                  const label = generalMediaDisplayName(g);
                  const term = label.split(/[(（]/)[0].trim() || label;
                  return (
                    <button
                      key={g.entity_key}
                      type="button"
                      title={[
                        (g.one_liner || '').trim(),
                        onFindMedia ? `点击在媒体列表搜索「${term}」` : '',
                      ].filter(Boolean).join('\n') || undefined}
                      onClick={onFindMedia ? () => onFindMedia(term, undefined, { domain: g.domain, oneLiner: g.one_liner }) : undefined}
                      className={cn(
                        'rounded border border-border/60 bg-background/60 px-1.5 py-0.5 text-[10px] text-muted-foreground',
                        onFindMedia && 'cursor-pointer transition-colors hover:border-emerald-400/50 hover:text-foreground',
                      )}
                    >
                      {label} <span className="text-muted-foreground/60">{scoreText(g.effectiveness_score)}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* [BUG-5 · T5 2026-07-27] 「发布 → 被引」转化率。
              上面的榜说"AI 引用谁",这一段说"我们投进去的那条 URL 到底被引了没" ——
              两张放一起,垂类该不该继续投才是数据自证,不是感觉。
              fail-soft(与本组件既有规则一致):无数据整段不渲染,不占位不报错。
              数据来自同一个 response,零新增请求。 */}
          {conversionRows.length > 0 && (
            <div className="mt-2 border-t border-border/40 pt-2">
              <div className="mb-1 flex flex-wrap items-baseline gap-1.5">
                <span className="text-[10px] font-medium text-muted-foreground">发布 → 被引 转化率</span>
                {overallRatePct !== null && (
                  <span
                    className={cn(
                      'text-[10px] font-semibold',
                      overallRatePct > 0 ? 'text-emerald-500 dark:text-emerald-400' : 'text-muted-foreground',
                    )}
                    title="我们已发布且拿到发布链接的文章里,被 AI 搜索引擎真实引用过的占比"
                  >
                    整体 {overallRatePct}%
                  </span>
                )}
                <span className="text-[10px] text-muted-foreground/70">
                  已发 {conversion?.total_published ?? 0} 篇 · 被引 {conversion?.total_cited ?? 0} 篇
                </span>
              </div>
              <div className="space-y-1">
                {conversionRows.map((m) => {
                  const pct = typeof m.citation_rate_pct === 'number' ? m.citation_rate_pct : null;
                  return (
                    <div
                      key={String(m.media_name)}
                      className="flex items-center gap-2 rounded border border-border/60 bg-background/60 px-2 py-1 text-[10px]"
                    >
                      <span className="min-w-0 flex-1 truncate text-foreground">{m.media_name}</span>
                      <span className="shrink-0 text-muted-foreground">发 {m.published ?? 0}</span>
                      <span className="shrink-0 text-muted-foreground">引 {m.cited_articles ?? 0}</span>
                      <span
                        className={cn(
                          'w-10 shrink-0 text-right font-semibold tabular-nums',
                          pct !== null && pct > 0
                            ? 'text-emerald-600 dark:text-emerald-400'
                            : 'text-muted-foreground',
                        )}
                      >
                        {pct === null ? '--' : `${pct}%`}
                      </span>
                    </div>
                  );
                })}
              </div>
              <p className="mt-1 text-[10px] leading-relaxed text-muted-foreground/70">
                按我们实际发出去的那条链接统计(不是按域名估算)。
                {overallRatePct === 0 && '当前这批发布还没有拿到 AI 引用 —— 换媒体或换选题比继续投更值得。'}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
