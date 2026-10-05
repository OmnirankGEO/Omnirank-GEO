/**
 * P0.8 前端 · 关键词意图分类 warning 面板
 *
 * 挂载点:StepKeywordSelect.tsx · 关键词列表下方
 * 触发:keywords 变化(debounce 600ms) · 批量调 /api/keywords/classify-intent
 * 展示:danger/warn 词 + 3 改写建议 chip · 点击替换原词
 *
 * CTO-15.7 2026-04-24 · "装修报价没有套路"避坑词事故止血前端
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, AlertCircle, CheckCircle, Loader2, Sparkles } from 'lucide-react';
import { authFetch } from '@/lib/api';

type IntentType = 'brand_decision' | 'avoid_trap' | 'info' | 'noise';

interface IntentResult {
  intent: IntentType;
  brand_recall_rate_estimate: number;
  confidence: 'high' | 'medium' | 'low';
  warning: string | null;
  rewrite_suggestions: string[];
}

type IntentMap = Record<string, IntentResult>;

interface Props {
  keywords: string[];
  industry: string;
  city: string;
  brandName?: string;
  /** 点改写 chip 时触发 · 父组件替换关键词 */
  onReplaceKeyword?: (oldKw: string, newKw: string) => void;
  /** 展示最大警告词数(default 10) */
  maxDisplay?: number;
}

const INTENT_LABEL: Record<IntentType, string> = {
  brand_decision: '品牌决策',
  avoid_trap: '避坑教育',
  info: '信息科普',
  noise: '噪音/无关',
};

function healthLevel(rate: number): 'danger' | 'warn' | 'ok' {
  if (rate < 0.15) return 'danger';
  if (rate < 0.30) return 'warn';
  return 'ok';
}

export function KeywordIntentPanel({
  keywords,
  industry,
  city,
  brandName = '',
  onReplaceKeyword,
  maxDisplay = 10,
}: Props) {
  const [intentMap, setIntentMap] = useState<IntentMap>({});
  const [loading, setLoading] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastFetchedKeywordsRef = useRef<string>('');

  // debounce 600ms fetch
  useEffect(() => {
    if (!keywords || keywords.length === 0) {
      setIntentMap({});
      return;
    }
    const fp = keywords.slice().sort().join('|') + `::${industry}::${city}`;
    if (fp === lastFetchedKeywordsRef.current) return; // 同批次不重复

    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      lastFetchedKeywordsRef.current = fp;
      setLoading(true);
      try {
        const res = await authFetch('/api/keywords/classify-intent', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            keywords,
            industry,
            city,
            brand_name: brandName,
          }),
        });
        if (res.ok) {
          const data = await res.json();
          if (data?.results) setIntentMap(data.results);
        }
      } catch (_e) {
        // 静默失败 · 不阻塞代理工作流
      } finally {
        setLoading(false);
      }
    }, 600);

    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [keywords, industry, city, brandName]);

  // 筛选 danger/warn 词
  const problematic = useMemo(() => {
    const items: { keyword: string; result: IntentResult; level: 'danger' | 'warn' }[] = [];
    for (const kw of keywords) {
      const r = intentMap[kw];
      if (!r) continue;
      const lv = healthLevel(r.brand_recall_rate_estimate);
      if (lv !== 'ok') items.push({ keyword: kw, result: r, level: lv });
    }
    // danger 优先
    items.sort((a, b) =>
      a.level === b.level ? 0 : a.level === 'danger' ? -1 : 1,
    );
    return items.slice(0, maxDisplay);
  }, [keywords, intentMap, maxDisplay]);

  const totalAnalyzed = Object.keys(intentMap).length;
  const okCount = useMemo(
    () =>
      keywords.filter((k) => {
        const r = intentMap[k];
        return r && healthLevel(r.brand_recall_rate_estimate) === 'ok';
      }).length,
    [keywords, intentMap],
  );

  // 无数据状态
  if (keywords.length === 0) return null;

  return (
    <div className="mt-4 rounded-lg border border-border bg-muted/30 p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Sparkles className="h-4 w-4 text-brand" />
          关键词意图分析
          {loading && <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />}
        </div>
        {totalAnalyzed > 0 && (
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span className="flex items-center gap-1">
              <CheckCircle className="h-3 w-3 text-green-500" />
              {okCount} 高命中
            </span>
            {problematic.length > 0 && (
              <span className="flex items-center gap-1 text-amber-400">
                <AlertTriangle className="h-3 w-3" />
                {problematic.length} 待改
              </span>
            )}
          </div>
        )}
      </div>

      {problematic.length === 0 && totalAnalyzed > 0 && (
        <div className="text-xs text-green-500 flex items-center gap-1">
          <CheckCircle className="h-3 w-3" />
          所有词都是品牌决策型 · 预期命中良好
        </div>
      )}

      {problematic.length > 0 && (
        <div className="space-y-2">
          {problematic.map(({ keyword, result, level }) => (
            <div
              key={keyword}
              className={
                level === 'danger'
                  ? 'rounded-md border border-red-500/30 bg-red-500/5 p-2'
                  : 'rounded-md border border-amber-500/30 bg-amber-500/5 p-2'
              }
            >
              <div className="flex items-center justify-between gap-2 mb-1">
                <div className="flex items-center gap-2 min-w-0">
                  {level === 'danger' ? (
                    <AlertTriangle className="h-3.5 w-3.5 text-red-400 flex-shrink-0" />
                  ) : (
                    <AlertCircle className="h-3.5 w-3.5 text-amber-400 flex-shrink-0" />
                  )}
                  <span className="text-sm font-medium break-words leading-snug" title={keyword}>{keyword}</span>
                </div>
                <div className="flex items-center gap-1.5 flex-shrink-0">
                  <span
                    className={
                      level === 'danger'
                        ? 'text-xs text-red-400'
                        : 'text-xs text-amber-400'
                    }
                  >
                    {INTENT_LABEL[result.intent]} · 预期命中 {Math.round(result.brand_recall_rate_estimate * 100)}%
                  </span>
                </div>
              </div>
              {result.warning && (
                <div className="text-xs text-muted-foreground mb-1.5">
                  {result.warning}
                </div>
              )}
              {result.rewrite_suggestions.length > 0 && onReplaceKeyword && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-xs text-muted-foreground">建议改写:</span>
                  {result.rewrite_suggestions.map((sug) => (
                    <button
                      key={sug}
                      type="button"
                      onClick={() => onReplaceKeyword(keyword, sug)}
                      className="px-2 py-0.5 text-xs rounded-md bg-brand/10 hover:bg-brand/20 text-brand border border-brand/30 transition-colors"
                      title="点击用此改写替换原词"
                    >
                      {sug}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
