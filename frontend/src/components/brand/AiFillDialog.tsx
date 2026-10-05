/**
 * AiFillDialog — AI 智能填充面板
 *
 * 历史：
 *   CTO-13.0 2026-04-19 S0.2 · 从 BrandDetailPage 抽出（纯提取零功能变化）
 *   CTO-13.0 2026-04-19 S1.1 · 加顾问选择 + 切换调 /api/profiles/ai-fill（知识库 RAG + 深度营销字段）
 *
 * 两个模式：
 *   mode='quick'   · 联网搜索/LLM 提取（调 /api/brand/auto-fill）— 短文本/公司名场景
 *   mode='advisor' · 顾问 RAG 深度填充（调 /api/profiles/ai-fill）— 已上传客户知识库或补充资料 ≥20 字
 *
 * 输出：onFilled 回调把所有字段传父组件（AiFilledFields 宽类型兼容两接口）
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/components/ui/dialog';
import { Sparkles, RefreshCw, Check } from 'lucide-react';
import { toast } from 'sonner';
import { usePricing } from '@/context/PricingContext';
import { FeatureCostBadge } from '@/components/FeatureCostBadge';
import { authApi } from '@/context/AuthContext';
import { authFetch } from '@/lib/api';
import { useSimulatedThinking } from '@/hooks/useSimulatedThinking';
import { SimulatedThinking } from '@/components/SimulatedThinking';
import { cn } from '@/lib/utils';

export interface AiFilledFields {
  brand_name?: string;
  industry?: string;
  city?: string;
  business?: string;
  target_users?: string;
  persona_positioning?: string;
  persona_tone?: string;
  products?: string[];
  pain_points?: string[];
  competitors?: string[];
  // ai-fill 深度营销字段
  company_intro?: string;
  core_value?: string;
  selling_points?: string;
  success_cases?: string;
  testimonials?: string;
  story_type?: string;
  differentiation?: string;
  content_direction?: string;
  structured_knowledge?: Record<string, unknown>;
  // [CTO-15.9 M1c T4 + M1b M5] market_insight + business_type
  service_scope?: string;  // local / national / hybrid
  local_competitors?: string[];
  market_insight?: Record<string, unknown>;  // {authority_sources, hot_formats, my_differentiation}
  business_type?: string;  // B2C / B2B / 政企
  city_scope?: string;  // local / national
  // B5 (CTO-15.9 session 3 · 2026-04-25 · M1c §A.6 ConfidenceBadge 挂载)
  // ai-fill 后端 _field_confidence → 顶层 confidences:{field: 'strong'|'medium'|'weak'}
  // 前端 BrandDetailPage / MarketInsightCard 字段右侧挂 ConfidenceBadge
  _confidences?: Record<string, 'strong' | 'medium' | 'weak'>;
  // 宽容兜底 · 未来 ai-fill 扩字段不需再改 type
  [extraKey: string]: unknown;
}

interface Advisor {
  id: string;
  name: string;
  avatar?: string;
  description?: string;
  specialty?: string;
  role?: string;
  industries?: string[];
  tags?: string[];
}

// GEO 广告营销适用的「推荐」专家白名单(单列一栏置顶)· 目前只「品牌广告表达顾问小舒」teacher_shu
// 其余 advisor 是社媒方向专家;以后接入更多广告/营销专家,往这里加 advisor id 即可拓展(无需改逻辑)
const RECOMMENDED_ADVISOR_IDS = ['teacher_shu'];

/** 拆成「推荐(广告营销)」与「更多专家」两组 · 各自保持原序 */
function splitAdvisors(list: Advisor[]): { recommended: Advisor[]; others: Advisor[] } {
  const recommended: Advisor[] = [];
  const others: Advisor[] = [];
  for (const adv of list) {
    if (RECOMMENDED_ADVISOR_IDS.includes(adv.id)) recommended.push(adv);
    else others.push(adv);
  }
  return { recommended, others };
}

interface Props {
  /** 调 ai-fill 需要 brand_id 来定位该客户的知识库；没有就只能走 quick 模式 */
  brandId?: number;
  /** 父组件用此回调接收 AI 填充结果 */
  onFilled: (fields: AiFilledFields) => void;
}

type FillMode = 'quick' | 'advisor';

export function AiFillDialog({ brandId, onFilled }: Props) {
  // [#65] 价目取自 feature_pricing SSOT;为空 = 该 code 还没配 ⇒ 不显示任何数字。
  const { getCost } = usePricing();
  const brandFillCost = getCost('brand_fill');
  const [mode, setMode] = useState<FillMode>('quick');
  const [fillText, setFillText] = useState('');
  const [filling, setFilling] = useState(false);
  const fillingRef = useRef(false);

  // advisor 模式：顾问列表
  const [advisors, setAdvisors] = useState<Advisor[]>([]);
  const [selectedAdvisor, setSelectedAdvisor] = useState<string>('');
  const [advisorsLoading, setAdvisorsLoading] = useState(false);
  // 顾问档：切到顾问时弹专家库选老师
  const [advisorPickerOpen, setAdvisorPickerOpen] = useState(false);

  // 渲染前对顾问做稳定排序 · 营销/广告/投放类置顶
  const { recommended: recommendedAdvisors, others: otherAdvisors } = useMemo(() => splitAdvisors(advisors), [advisors]);

  // 顾问卡(推荐栏额外加「推荐」角标)· 点选即选中并关弹窗
  const renderAdvisorCard = (adv: Advisor, isRec: boolean) => {
    const active = selectedAdvisor === adv.id;
    return (
      <button
        key={adv.id}
        type="button"
        onClick={() => { setSelectedAdvisor(adv.id); setAdvisorPickerOpen(false); }}
        className={cn(
          'relative flex items-center gap-1.5 rounded-lg border p-2 text-left text-[11px] transition-colors',
          active ? 'border-foreground bg-accent ring-1 ring-foreground/20' : 'border-border hover:bg-accent/40',
        )}
      >
        {isRec && (
          <span className="absolute right-1 top-1 rounded bg-primary/10 px-1 py-px text-[8px] font-medium leading-none text-primary">推荐</span>
        )}
        <span className="shrink-0 text-sm">{adv.avatar || '🎓'}</span>
        <div className="min-w-0 flex-1">
          <div className="truncate font-medium">{adv.name}</div>
          {(adv.specialty || adv.description) && (
            <div className="truncate text-[9px] text-muted-foreground">{adv.specialty || adv.description}</div>
          )}
        </div>
        {active && <Check className="h-3 w-3 shrink-0" />}
      </button>
    );
  };

  useEffect(() => {
    if (mode !== 'advisor' || advisors.length > 0) return;
    setAdvisorsLoading(true);
    authFetch('/api/advisors')
      .then(r => r.json())
      .then(data => {
        if (data?.success !== false && Array.isArray(data?.advisors)) {
          setAdvisors(data.advisors);
          if (!selectedAdvisor && data.advisors.length > 0) {
            // 默认选「推荐」白名单里的(小舒 teacher_shu),没有则退列表第一个
            const rec = data.advisors.find((a: Advisor) => RECOMMENDED_ADVISOR_IDS.includes(a.id));
            setSelectedAdvisor(rec?.id || data.advisors[0].id);
          }
        }
      })
      .catch(() => { /* 静默 */ })
      .finally(() => setAdvisorsLoading(false));
  }, [mode, advisors.length, selectedAdvisor]);

  // 切到「顾问」档时弹出专家库选老师(brandId 存在才弹 · 无 brandId 顾问档不可用)
  const handleModeChange = (toAdvisor: boolean) => {
    if (toAdvisor) {
      if (!brandId) {
        toast.info('请先保存客户资料，再用顾问档');
        return;
      }
      setMode('advisor');
      setAdvisorPickerOpen(true);
    } else {
      setMode('quick');
    }
  };

  const selectedAdvisorObj = advisors.find(a => a.id === selectedAdvisor);

  const { thinkingSteps: fillThinking, isThinking: isFillThinking } = useSimulatedThinking(filling, {
    steps: mode === 'advisor'
      ? ['加载客户知识库...', '调用顾问 RAG 检索...', '结构化提取营销资料...']
      : ['AI 正在联网搜索...', '提取品牌信息...', '生成填充数据...'],
    intervalMs: 1200,
  });

  const handleQuickFill = async (): Promise<AiFilledFields | null> => {
    const res = await authApi.post('/api/brand/auto-fill', { text: fillText }, { timeout: 90000 });
    if (res.data.success && res.data.data) {
      return res.data.data as AiFilledFields;
    }
    throw new Error(res.data?.error || 'AI 未能提取信息');
  };

  const handleAdvisorFill = async (): Promise<AiFilledFields | null> => {
    if (!brandId) throw new Error('缺少 brand_id，无法读取客户知识库');
    if (!selectedAdvisor) throw new Error('请先选择顾问');
    const res = await authApi.post('/api/profiles/ai-fill', {
      brand_id: brandId,
      advisor_id: selectedAdvisor,
      text: fillText.trim() || undefined,
    }, { timeout: 120000 });  // ai-fill 顾问 RAG 较慢，放宽到 120s
    if (res.data.success && res.data.data) {
      // B5 · 把后端 confidences 透传到 _confidences 字段(顶层)
      // 前端字段右侧 ConfidenceBadge 直接读 _confidences[fieldName]
      const filled = res.data.data as AiFilledFields;
      const conf = res.data.confidences;
      if (conf && typeof conf === 'object') {
        filled._confidences = conf as Record<string, 'strong' | 'medium' | 'weak'>;
      }
      return filled;
    }
    throw new Error(res.data?.error || 'AI 顾问返回异常');
  };

  const handleSubmit = async () => {
    if (mode === 'quick' && !fillText.trim()) {
      toast.error('请输入公司名或粘贴资料');
      return;
    }
    if (mode === 'advisor') {
      if (!brandId) {
        toast.error('该入口暂不支持顾问模式（缺 brand_id）');
        return;
      }
      if (!selectedAdvisor) {
        toast.error('请先选择一位顾问');
        return;
      }
    }
    if (fillingRef.current) { toast.info('AI 正在分析中，请稍候'); return; }
    fillingRef.current = true;
    setFilling(true);
    toast.info(
      mode === 'advisor'
        ? 'AI 顾问正在读取客户知识库 + 深度分析（约 30-60 秒）...'
        : 'AI 正在分析，切换页面不会中断',
    );
    try {
      const data = mode === 'advisor' ? await handleAdvisorFill() : await handleQuickFill();
      if (!data) throw new Error('AI 返回为空');
      try {
        onFilled(data);
        setFillText('');
      } catch { /* 组件已卸载 */ }
      const filledPersona = !!(data.persona_positioning || data.persona_tone);
      const deep = !!(data.company_intro || data.core_value || data.selling_points);
      toast.success(
        deep
          ? 'AI 已填充「基本信息 + 深度营销资料」，请逐项检查并保存'
          : filledPersona
            ? 'AI 已填充「基本信息」+「IP 人设」Tab，请切换 Tab 检查并保存'
            : 'AI 已填充「基本信息」Tab，请检查并保存',
      );
    } catch (e) {
      const err = e as { response?: { data?: { error?: string; detail?: string } }; code?: string; message?: string };
      const raw = err?.response?.data?.error || err?.response?.data?.detail || err?.message;
      const msg = typeof raw === 'string'
        ? raw
        : (err?.code === 'ECONNABORTED' ? 'AI 处理超时，请缩短内容重试' : 'AI 填充失败');
      toast.error(msg);
    }
    fillingRef.current = false;
    try { setFilling(false); } catch { /* 组件已卸载 */ }
  };

  return (
    <div className="rounded-xl border border-border/40 bg-muted/20 p-4 mb-5 space-y-3">
      {/* 模式切换 */}
      <div className="flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-muted-foreground" />
        <span className="text-[11px] font-medium text-muted-foreground tracking-wide">AI 智能填充</span>
        {/* 普通 / 顾问 滑动开关 · 顾问档需先保存客户资料(brandId) */}
        <div className="ml-auto flex items-center gap-2">
          <span className={cn('text-[11px] transition-colors', mode === 'quick' ? 'font-medium text-foreground' : 'text-muted-foreground')}>普通</span>
          <Switch
            checked={mode === 'advisor'}
            onCheckedChange={handleModeChange}
            disabled={!brandId}
            aria-label="切换普通 / 顾问填充"
            title={!brandId ? '请先保存客户资料，再用顾问档' : '顾问档读取该客户资料深度填充'}
          />
          <span className={cn('text-[11px] transition-colors', mode === 'advisor' ? 'font-medium text-foreground' : 'text-muted-foreground')}>顾问</span>
        </div>
      </div>

      {/* 模式描述 */}
      <p className="text-[11px] text-muted-foreground/70 leading-relaxed">
        {mode === 'advisor'
          ? '请一位 AI 老师 → 读取该客户资料（最好先上传过文档）+ 可选补充资料 → 深度整理营销资料（含 5 维度业务画像）'
          : '输入公司名自动搜索网上信息，或粘贴公司介绍 / 产品资料让 AI 提取关键信息'}
      </p>

      {/* advisor 模式：已选老师 · 点「换一位」重开专家库 */}
      {mode === 'advisor' && (
        <div className="flex items-center gap-2 rounded-lg border border-border bg-background/60 px-3 py-2">
          {advisorsLoading ? (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <RefreshCw className="h-3 w-3 animate-spin" /> 加载老师名单...
            </div>
          ) : advisors.length === 0 ? (
            <div className="text-xs text-muted-foreground">暂无可用老师</div>
          ) : selectedAdvisorObj ? (
            <>
              <span className="text-base shrink-0">{selectedAdvisorObj.avatar || '🎓'}</span>
              <div className="min-w-0 flex-1">
                <div className="text-xs font-medium text-foreground truncate">{selectedAdvisorObj.name}</div>
                {(selectedAdvisorObj.specialty || selectedAdvisorObj.description) && (
                  <div className="text-[10px] text-muted-foreground truncate">
                    {selectedAdvisorObj.specialty || selectedAdvisorObj.description}
                  </div>
                )}
              </div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-7 shrink-0 text-[11px]"
                onClick={() => setAdvisorPickerOpen(true)}
                disabled={filling}
              >
                换一位
              </Button>
            </>
          ) : (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-7 text-[11px]"
              onClick={() => setAdvisorPickerOpen(true)}
              disabled={filling}
            >
              请一位老师
            </Button>
          )}
        </div>
      )}

      {/* 专家库选择弹窗 · 营销/广告/投放类置顶 */}
      <Dialog open={advisorPickerOpen} onOpenChange={setAdvisorPickerOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>请一位 AI 老师</DialogTitle>
            <DialogDescription>老师会读取该客户资料，深度整理出营销素材。「推荐」栏是适合广告营销的老师，其余为社媒方向。</DialogDescription>
          </DialogHeader>
          {advisorsLoading ? (
            <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
              <RefreshCw className="h-4 w-4 animate-spin" /> 加载老师名单...
            </div>
          ) : (recommendedAdvisors.length === 0 && otherAdvisors.length === 0) ? (
            <div className="py-6 text-center text-sm text-muted-foreground">暂无可用老师</div>
          ) : (
            <div className="max-h-[360px] space-y-4 overflow-y-auto">
              {recommendedAdvisors.length > 0 && (
                <div className="space-y-1.5">
                  <div className="text-[11px] font-medium text-primary">推荐 · 广告营销</div>
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                    {recommendedAdvisors.map(adv => renderAdvisorCard(adv, true))}
                  </div>
                </div>
              )}
              {otherAdvisors.length > 0 && (
                <div className="space-y-1.5">
                  <div className="text-[11px] font-medium text-muted-foreground">更多专家（社媒方向）</div>
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                    {otherAdvisors.map(adv => renderAdvisorCard(adv, false))}
                  </div>
                </div>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* textarea */}
      <div className="flex gap-2 items-end">
        <textarea
          placeholder={
            mode === 'advisor'
              ? '补充资料（可选）：粘贴近期访谈、客户反馈、产品手册等能丰富 AI 分析的原材料'
              : '方式一：输入公司名，AI 联网搜索填充\n方式二：粘贴公司简介、产品手册、官网介绍等，AI 自动提取'
          }
          value={fillText}
          onChange={e => setFillText(e.target.value)}
          className="flex-1 text-xs bg-background border border-border rounded-lg px-3 py-2 min-h-[60px] max-h-[150px] resize-y outline-none focus:ring-1 focus:ring-foreground/20 placeholder:text-muted-foreground/40"
        />
        {/* 🔴 [#65 2026-09-05] 2026-06-03 拍板第 ② 段:废除前置确认,但**价格必须看得见**。
                  用 getCost 判空门控 —— FeatureCostBadge 拿不到价会渲染「价目待配置」
                  (给开发看的调试串),在 C 补上 feature_pricing 行之前那句会漏给用户。
                  取不到价就**一个字都不显示**,同 #64:宁可少说,不可说错。 */}
        {brandFillCost != null && (
          <span data-testid="ai-fill-cost" className="self-center mr-2">
            <FeatureCostBadge featureCode="brand_fill" variant="inline" />
          </span>
        )}
        <Button size="sm" className="h-9 text-xs shrink-0" onClick={handleSubmit} disabled={filling}>
          {filling ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : 'AI 分析'}
        </Button>
      </div>
      {isFillThinking && (
        <SimulatedThinking steps={fillThinking} isVisible={true} className="mt-2" waitScene="brandFill" />
      )}
    </div>
  );
}

export default AiFillDialog;
