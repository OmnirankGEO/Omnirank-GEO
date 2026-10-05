/**
 * MarketInsightCard — M1c T5 UI 层 · 市场洞察编辑卡
 *
 * CTO-15.9 2026-04-25
 *
 * 消费/编辑 brand_completeness E 组 5 字段:
 *   · service_scope(local/national/hybrid · radio)
 *   · local_competitors(string[] · 简单文本框行分隔)
 *   · authority_sources(string[] · 行业权威来源 · flatten 自 industry_brief)
 *   · hot_formats(string[] · 热门内容形式 · flatten 自 industry_brief)
 *   · my_differentiation(string · 差异化核心 · flatten 自 industry_brief)
 *
 * 设计:
 *  - 不做 wizard 3 步拆 · 一卡一次看 5 字段 · 代理扫视确认
 *  - AI 填过的字段走 aiFilledFields 高亮(和现有 FG 视觉一致)
 *  - 编辑后 clearAiHighlight 移除高亮(表示代理背书)
 *  - 与 /api/profiles/ai-fill json_template 返回字段名 1:1 对齐
 *  - 后端 brand_api.py _merge_market_insight_into_brief 非覆盖合并
 */

import { useMemo } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Target, Sparkles } from 'lucide-react';

interface Props {
  serviceScope: string;
  setServiceScope: (v: string) => void;
  localCompetitors: string[];
  setLocalCompetitors: (v: string[]) => void;
  marketInsight: Record<string, unknown>;
  setMarketInsight: (v: Record<string, unknown>) => void;
  aiFilledFields: Set<string>;
  clearAiHighlight: (key: string) => void;
}

function ensureStringArray(v: unknown): string[] {
  if (Array.isArray(v)) return v.filter(x => typeof x === 'string' && x.trim()).map(x => (x as string).trim());
  if (typeof v === 'string' && v.trim()) {
    return v.split(/[,，;；\n]/g).map(s => s.trim()).filter(Boolean);
  }
  return [];
}

function arrayToText(v: unknown): string {
  const arr = ensureStringArray(v);
  return arr.join('\n');
}

const SCOPE_OPTIONS: { value: string; label: string; hint: string }[] = [
  { value: 'local', label: '本地 / 区域', hint: '城市 / 省 / 某区域' },
  { value: 'national', label: '全国', hint: '跨省 · 不分地域' },
  { value: 'hybrid', label: '混合', hint: '本地 + 线上覆盖' },
];

export function MarketInsightCard({
  serviceScope,
  setServiceScope,
  localCompetitors,
  setLocalCompetitors,
  marketInsight,
  setMarketInsight,
  aiFilledFields,
  clearAiHighlight,
}: Props) {
  const authoritySources = useMemo(
    () => ensureStringArray((marketInsight || {}).authority_sources),
    [marketInsight]
  );
  const hotFormats = useMemo(
    () => ensureStringArray((marketInsight || {}).hot_formats),
    [marketInsight]
  );
  const myDifferentiation =
    typeof (marketInsight || {}).my_differentiation === 'string'
      ? ((marketInsight as Record<string, unknown>).my_differentiation as string)
      : '';

  const updateMarketInsight = (key: string, value: unknown) => {
    const next = { ...(marketInsight || {}) };
    if (value === '' || value === null || (Array.isArray(value) && value.length === 0)) {
      delete next[key];
    } else {
      next[key] = value;
    }
    setMarketInsight(next);
  };

  const highlightClass = (fieldKey: string) =>
    aiFilledFields.has(fieldKey)
      ? 'border-amber-500/50 bg-amber-500/5'
      : '';

  return (
    <Card className="border border-emerald-500/20 shadow-none">
      <CardHeader className="p-4 pb-2">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <Target className="h-4 w-4 text-emerald-500" />
          市场洞察
          <Badge variant="secondary" className="text-[10px] font-normal">
            完善度 E 组 · 10 分
          </Badge>
          {(aiFilledFields.has('service_scope') ||
            aiFilledFields.has('local_competitors') ||
            aiFilledFields.has('market_insight')) && (
            <Badge className="text-[10px] font-normal bg-amber-500/15 text-amber-600 border-amber-500/30">
              <Sparkles className="h-3 w-3 mr-1" />
              AI 建议 · 请审阅
            </Badge>
          )}
        </CardTitle>
        <p className="text-[11px] text-muted-foreground leading-relaxed">
          驱动 GEO 报价地域系数 / 行业知识注入 / 关键词拓词质量 · AI 填完代理扫视确认
        </p>
      </CardHeader>
      <CardContent className="p-4 pt-2 space-y-4">
        {/* service_scope · radio */}
        <div className="space-y-1.5">
          <Label className="text-xs font-medium">服务地域范围</Label>
          <div className={`flex flex-wrap gap-2 rounded-md ${highlightClass('service_scope')}`}>
            {SCOPE_OPTIONS.map(opt => (
              <button
                key={opt.value}
                type="button"
                onClick={() => {
                  setServiceScope(opt.value);
                  clearAiHighlight('service_scope');
                }}
                className={
                  serviceScope === opt.value
                    ? 'px-3 py-1.5 rounded-md text-xs border border-emerald-500/50 bg-emerald-500/10 text-emerald-600'
                    : 'px-3 py-1.5 rounded-md text-xs border border-border text-muted-foreground hover:bg-muted/60'
                }
                title={opt.hint}
              >
                {opt.label}
              </button>
            ))}
            {serviceScope && (
              <button
                type="button"
                onClick={() => {
                  setServiceScope('');
                  clearAiHighlight('service_scope');
                }}
                className="px-2 py-1.5 rounded-md text-[11px] text-muted-foreground hover:text-destructive"
              >
                清空
              </button>
            )}
          </div>
          {serviceScope && (
            <p className="text-[11px] text-muted-foreground">
              {SCOPE_OPTIONS.find(o => o.value === serviceScope)?.hint}
            </p>
          )}
        </div>

        {/* local_competitors · textarea 行分隔 */}
        <div className="space-y-1.5">
          <Label className="text-xs font-medium">本地 / 同区域竞品（每行一个）</Label>
          <textarea
            value={localCompetitors.join('\n')}
            onChange={e => {
              const list = e.target.value.split('\n').map(s => s.trim()).filter(Boolean);
              setLocalCompetitors(list);
              clearAiHighlight('local_competitors');
            }}
            rows={3}
            placeholder="例:&#10;隔壁王府家常菜&#10;东门口老北京"
            className={`w-full px-3 py-2 rounded-md border bg-background text-xs resize-y ${highlightClass('local_competitors')}`}
          />
          {localCompetitors.length > 0 && (
            <p className="text-[11px] text-muted-foreground">已填 {localCompetitors.length} 个</p>
          )}
        </div>

        {/* authority_sources · textarea 行分隔 */}
        <div className="space-y-1.5">
          <Label className="text-xs font-medium">行业权威信息源（每行一个）</Label>
          <textarea
            value={arrayToText(authoritySources)}
            onChange={e => {
              const list = e.target.value.split('\n').map(s => s.trim()).filter(Boolean);
              updateMarketInsight('authority_sources', list);
              clearAiHighlight('market_insight');
            }}
            rows={3}
            placeholder="例:&#10;中国家装协会&#10;安居客行业报告&#10;央视家居频道"
            className={`w-full px-3 py-2 rounded-md border bg-background text-xs resize-y ${highlightClass('market_insight')}`}
          />
        </div>

        {/* hot_formats · textarea 行分隔 */}
        <div className="space-y-1.5">
          <Label className="text-xs font-medium">AI 搜索答案常见形式（每行一个）</Label>
          <textarea
            value={arrayToText(hotFormats)}
            onChange={e => {
              const list = e.target.value.split('\n').map(s => s.trim()).filter(Boolean);
              updateMarketInsight('hot_formats', list);
              clearAiHighlight('market_insight');
            }}
            rows={3}
            placeholder="例:&#10;装修避坑攻略&#10;选材指南&#10;案例实拍"
            className={`w-full px-3 py-2 rounded-md border bg-background text-xs resize-y ${highlightClass('market_insight')}`}
          />
        </div>

        {/* my_differentiation · textarea */}
        <div className="space-y-1.5">
          <Label className="text-xs font-medium">本品牌差异化核心（一句话）</Label>
          <Input
            value={myDifferentiation}
            onChange={e => {
              updateMarketInsight('my_differentiation', e.target.value);
              clearAiHighlight('market_insight');
            }}
            placeholder="例:祖传 30 年秘方 + 全透明操作间 + 招牌宫保鸡丁日销 200+"
            className={highlightClass('market_insight')}
          />
          <p className="text-[11px] text-muted-foreground">用于 GEO 文章差异化 prompt · 建议 &lt; 60 字</p>
        </div>
      </CardContent>
    </Card>
  );
}

export default MarketInsightCard;
