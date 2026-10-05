/**
 * 复用趋势折线（presence / recommendation 两条线 + 模型升级断点参考线）。
 * - 断点来自后端**每点** `model_shift_marker`（可多个），不自造 model_shift_at/after_model_shift。
 * - 每个断点都切一段：断点前后不连成一条可比曲线（多段渲染，段间不连线）。
 * - 参考线画在每个断点；只给第一个断点文字标注，避免多标签重叠/裁切。
 * - 顶部/右侧留足边距，保证参考线标注与 Y 轴百分比在窄屏完整显示。
 */

import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip as RTooltip,
  CartesianGrid,
  ReferenceLine,
} from 'recharts';
import { METRIC_LABELS } from '../copy';
import { bpsToNumber, formatShortDate } from '../format';
import type { TrendPoint } from '../types';

// 自定义 tooltip：多段渲染时只展示当前 x 处非空的两条线（presence/recommendation），
// 不为其它段的 null 系列渲染多余的"—"行。
function TrendTooltip(props: {
  active?: boolean;
  label?: string;
  payload?: { dataKey?: string | number; value?: number | null }[];
}) {
  const { active, label, payload } = props;
  if (!active || !payload) return null;
  const live = payload.filter((it) => it.value !== null && it.value !== undefined);
  const presence = live.find((it) => String(it.dataKey).startsWith('p'));
  const rec = live.find((it) => String(it.dataKey).startsWith('r'));
  if (!presence && !rec) return null;
  return (
    <div
      style={{
        background: 'hsl(var(--popover))',
        border: '1px solid hsl(var(--border))',
        borderRadius: 8,
        fontSize: 12,
        padding: '6px 10px',
        color: 'hsl(var(--popover-foreground))',
      }}
    >
      <div style={{ marginBottom: 2 }}>{label}</div>
      {presence ? (
        <div style={{ color: '#10b981' }}>
          {METRIC_LABELS.presence_rate}: {presence.value}%
        </div>
      ) : null}
      {rec ? (
        <div style={{ color: '#3b82f6' }}>
          {METRIC_LABELS.explicit_recommendation_rate}: {rec.value}%
        </div>
      ) : null}
    </div>
  );
}

export function TrendChart({ points }: { points: TrendPoint[] }) {
  // 段号：每遇到一个 model_shift_marker（且非首点）就 +1，从而每个断点都断开
  let seg = 0;
  const withSeg = points.map((p, i) => {
    if (p.model_shift_marker && i > 0) seg += 1;
    return { p, seg };
  });
  const segIds = Array.from(new Set(withSeg.map((x) => x.seg)));

  // 每个点按其段号写入 p{seg}/r{seg} 键；不同段用不同键 → 段间天然断开、不连线
  const data = withSeg.map(({ p, seg: s }) => {
    const row: Record<string, number | string | null> = { label: formatShortDate(p.bucket_start) };
    row[`p${s}`] = bpsToNumber(p.presence_rate_bps);
    row[`r${s}`] = bpsToNumber(p.explicit_recommendation_rate_bps);
    return row;
  });

  // 每个断点的 x 标签（画参考线）；只第一个带文字。
  // 与分段一致只取 i>0 的断点：首桶即断点时无前段可断，不画孤立参考线。
  const shiftLabels = points
    .filter((p, i) => p.model_shift_marker && i > 0)
    .map((p) => formatShortDate(p.bucket_start));
  const hasShift = shiftLabels.length > 0;

  return (
    <>
      <div className="h-56 w-full" aria-label="趋势折线图" role="img">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 28, right: 20, left: 8, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
            <XAxis dataKey="label" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} tickLine={false} />
            <YAxis
              tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }}
              tickLine={false}
              axisLine={false}
              width={52}
              domain={[0, 100]}
              tickFormatter={(v: number) => `${v}%`}
            />
            <RTooltip content={<TrendTooltip />} />
            {shiftLabels.map((x, i) => (
              <ReferenceLine
                key={`${x}-${i}`}
                x={x}
                stroke="hsl(217 91% 60%)"
                strokeDasharray="4 4"
                label={
                  i === 0
                    ? { value: '口径断点', fontSize: 10, fill: 'hsl(var(--muted-foreground))', position: 'top', dy: -4 }
                    : undefined
                }
              />
            ))}
            {segIds.map((s) => (
              <Line
                key={`p${s}`}
                type="monotone"
                dataKey={`p${s}`}
                stroke="#10b981"
                strokeWidth={2}
                strokeDasharray={s > 0 ? '5 3' : undefined}
                dot={{ r: 2 }}
                connectNulls={false}
                isAnimationActive={false}
              />
            ))}
            {segIds.map((s) => (
              <Line
                key={`r${s}`}
                type="monotone"
                dataKey={`r${s}`}
                stroke="#3b82f6"
                strokeWidth={2}
                strokeDasharray={s > 0 ? '5 3' : undefined}
                dot={{ r: 2 }}
                connectNulls={false}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-4 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full bg-emerald-500" /> {METRIC_LABELS.presence_rate}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full bg-blue-500" /> {METRIC_LABELS.explicit_recommendation_rate}
        </span>
        {hasShift ? <span className="text-muted-foreground/80">虚线为口径断点之后区间，不与前段直接比较</span> : null}
      </div>
    </>
  );
}
