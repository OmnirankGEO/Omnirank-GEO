import { useState, useEffect } from 'react';
import { authFetch } from '@/lib/api';

interface HesitationKeyword {
  keyword_id: number;
  keyword_text: string;
  toggle_count: number;
  hover_seconds: number;
  view_count: number;
  hesitation_score: number;
  level: string;
}

interface TimelineEvent {
  time: string;
  type: string;
  keyword_id?: number;
  keyword_text?: string;
  phase: string;
  data?: Record<string, unknown>;
}

interface AnalysisData {
  session: {
    status: string;
    visit_count: number;
    created_at: string;
    keywords_submitted_at?: string;
    confirmed_at?: string;
  };
  hesitation_keywords: HesitationKeyword[];
  timeline: TimelineEvent[];
  stats: {
    total_events: number;
    high_hesitation_count: number;
  };
}

const EVENT_LABELS: Record<string, string> = {
  page_open: '打开页面',
  keyword_select: '选择关键词',
  keyword_deselect: '取消选择',
  keyword_hover: '停留查看',
  keyword_add: '添加自定义词',
  category_switch: '切换分类',
  keywords_submit: '确认提交',
  keywords_withdraw: '撤回重选',
  tier_switch: '切换套餐',
  tier_hover: '查看套餐',
  price_keyword_deselect: '取消关键词(报价)',
  price_keyword_reselect: '重选关键词(报价)',
  add_more_keywords: '追加关键词',
  quote_confirm: '确认报价',
  page_leave: '离开页面',
};

const LEVEL_COLORS: Record<string, string> = {
  '果断': 'text-green-400 bg-green-500/10',
  '思考中': 'text-amber-400 bg-amber-500/10',
  '高犹豫': 'text-red-400 bg-red-500/10',
};

interface Props {
  token: string;
}

export function SelectionAnalysis({ token }: Props) {
  const [data, setData] = useState<AnalysisData | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [lastToken, setLastToken] = useState('');

  useEffect(() => {
    if (!expanded) return;
    if (data && token === lastToken) return;
    setLoading(true);
    setLastToken(token);
    authFetch(`/api/keyword-selection/${token}/analysis`)
      .then(res => res.json())
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [expanded, token, data, lastToken]);

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-4 py-3 bg-muted hover:bg-muted/80 transition-colors text-sm"
      >
        <span className="font-medium text-foreground">客户选词行为分析</span>
        <span className="text-muted-foreground">{expanded ? '▲' : '▼'}</span>
      </button>

      {expanded && (
        <div className="p-4">
          {loading && <p className="text-sm text-muted-foreground">加载中...</p>}
          {data && (
            <div className="space-y-4">
              {/* Basic stats */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
                <div className="bg-muted rounded-lg p-3">
                  <p className="text-muted-foreground">访问次数</p>
                  <p className="font-semibold text-lg">{data.session.visit_count}</p>
                </div>
                <div className="bg-muted rounded-lg p-3">
                  <p className="text-muted-foreground">总事件数</p>
                  <p className="font-semibold text-lg">{data.stats.total_events}</p>
                </div>
                <div className="bg-muted rounded-lg p-3">
                  <p className="text-muted-foreground">高犹豫词</p>
                  <p className="font-semibold text-lg text-red-400">{data.stats.high_hesitation_count}</p>
                </div>
                <div className="bg-muted rounded-lg p-3">
                  <p className="text-muted-foreground">首次打开</p>
                  <p className="font-medium text-xs">{data.session.created_at}</p>
                </div>
              </div>

              {/* High hesitation keywords */}
              {data.hesitation_keywords.filter(h => h.level === '高犹豫' || h.level === '思考中').length > 0 && (
                <div>
                  <h4 className="text-sm font-medium text-muted-foreground mb-2">需关注的关键词</h4>
                  <div className="space-y-2">
                    {data.hesitation_keywords
                      .filter(h => h.level === '高犹豫' || h.level === '思考中')
                      .slice(0, 10)
                      .map(h => (
                        <div key={h.keyword_id} className="bg-card border border-border rounded-lg p-3">
                          <div className="flex items-center justify-between mb-1">
                            <span className="text-sm font-medium">{h.keyword_text || `#${h.keyword_id}`}</span>
                            <span className={`text-xs px-2 py-0.5 rounded-full ${LEVEL_COLORS[h.level] || ''}`}>
                              犹豫度 {h.hesitation_score} ({h.level})
                            </span>
                          </div>
                          <div className="flex gap-4 text-xs text-muted-foreground">
                            <span>反复切换{h.toggle_count}次</span>
                            <span>停留{h.hover_seconds.toFixed(1)}秒</span>
                            <span>停留查看{h.view_count}次</span>
                          </div>
                          {/* Progress bar */}
                          <div className="mt-2 h-1.5 bg-muted rounded-full overflow-hidden">
                            <div
                              className={`h-full rounded-full ${h.level === '高犹豫' ? 'bg-red-400' : 'bg-amber-400'}`}
                              style={{ width: `${Math.min(h.hesitation_score * 10, 100)}%` }}
                            />
                          </div>
                        </div>
                      ))}
                  </div>
                </div>
              )}

              {/* Timeline */}
              <details>
                <summary className="text-sm text-muted-foreground cursor-pointer hover:text-foreground">
                  完整时间线（{data.timeline.length}条记录）
                </summary>
                <div className="mt-2 max-h-60 overflow-y-auto space-y-1">
                  {data.timeline.map((ev, i) => (
                    <div key={i} className="flex items-center gap-2 text-xs text-muted-foreground py-1 border-b border-border">
                      <span className="text-muted-foreground/60 w-32 shrink-0">{ev.time}</span>
                      <span>{EVENT_LABELS[ev.type] || ev.type}</span>
                      {ev.keyword_text && <span className="text-foreground font-medium">"{ev.keyword_text}"</span>}
                    </div>
                  ))}
                </div>
              </details>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
