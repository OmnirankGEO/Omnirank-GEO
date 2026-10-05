// W7 · 飞轮环(Hero)——纯 SVG 自包含组件(禁用图表库画环)。
//  · 环本体 = 单 circle stroke-dasharray 五段染色
//  · 五节点卡 = 按角度 cos/sin 绝对定位的可点击 div(点击跳 tab)
//  · 流动圆点 ≤10 沿圆路径(SVG animateMotion),尊重 prefers-reduced-motion
//  · 中心 = 健康度文本层
//  · 移动端(narrow)降级为纵向五步列表
import { type ReactNode } from 'react';
import { cn } from '@/lib/utils';
import { fmtInt, usePrefersReducedMotion, type PanoramaNode } from './flywheelApi';

export type RingNodeView = {
  key: string;
  index: number; // 1..5
  label: string;
  value: ReactNode;
  sub?: string;
  color: string; // hex
  tabTarget?: string;
  /** [行业口径 2026-08-18] true = 选了具体行业,但这一环库里没有行业维度,显示的仍是全站数。
   *  必须在卡上标「全站」—— 顶部挂着行业选择器而数字是全站总数,就是口径误导。 */
  siteScope?: boolean;
};

// 五节点配色(与参考图方向一致:绿→蓝→琥珀→紫→橙)。
const NODE_COLORS: Record<string, string> = {
  collect: '#34d399',
  learn: '#38bdf8',
  review: '#fbbf24',
  apply: '#a78bfa',
  outcome: '#fb923c',
};

const NODE_ORDER = ['collect', 'learn', 'review', 'apply', 'outcome'];

const R = 150; // 环半径(viewBox 400)
const CX = 200;
const CY = 200;
const CIRC = 2 * Math.PI * R;
const GAP = 10; // 段间隙(用户坐标)
const SEG = CIRC / 5 - GAP;

function nodeAngle(i: number): number {
  // 从正上方(-90°)顺时针,每节点 72°。
  return (-90 + i * 72) * (Math.PI / 180);
}

export function FlywheelRing({
  nodes,
  healthPercent,
  healthLabel,
  onNodeClick,
  industryScoped = false,
}: {
  nodes: PanoramaNode[];
  healthPercent: number;
  healthLabel: string;
  onNodeClick?: (key: string) => void;
  /** 后端是否真按行业过滤了。false(全站口径)时不标任何角标 —— 与本改动前逐字同样式。 */
  industryScoped?: boolean;
}) {
  const reduced = usePrefersReducedMotion();

  // 组装五节点视图(按固定顺序,缺项 fail-soft 显示 0)。
  const byKey = new Map(nodes.map((n) => [n.key, n]));
  const nodeViews: RingNodeView[] = NODE_ORDER.map((key, idx) => {
    const n = byKey.get(key);
    const value = n ? fmtInt(n.value) : '0';
    let sub: string | undefined;
    if (key === 'review') sub = '今日待办';
    else if (key === 'apply') sub = '已生效';
    else if (key === 'outcome') sub = '30/60 天验证';
    else if (key === 'collect') sub = '真实调研行';
    else if (key === 'learn') sub = '来源信号';
    return {
      key,
      index: idx + 1,
      label: n?.label || key,
      value,
      sub,
      color: NODE_COLORS[key] || '#94a3b8',
      tabTarget: key,
      // 只有「选了具体行业」且「该环确实是全站口径」两个条件同时成立才标。
      siteScope: industryScoped && n?.industry_scope === 'site',
    };
  });

  // 流动圆点(≤8),尊重 reduced-motion。
  const dotCount = 8;
  const dots = Array.from({ length: dotCount }, (_, i) => i);

  return (
    <div className="w-full">
      {/* PC / 平板:SVG 环 */}
      <div className="relative mx-auto hidden aspect-square w-full max-w-[520px] md:block">
        <svg viewBox="0 0 400 400" className="absolute inset-0 h-full w-full" role="img" aria-label="飞轮五节点环">
          <defs>
            {/* 流动圆点参考路径(完整圆) */}
            <path
              id="flywheel-ring-path"
              d={`M ${CX} ${CY - R} A ${R} ${R} 0 1 1 ${CX - 0.01} ${CY - R}`}
              fill="none"
            />
          </defs>

          {/* 底环(淡) */}
          <circle cx={CX} cy={CY} r={R} fill="none" stroke="currentColor" strokeWidth={14} className="text-muted/40" />

          {/* 五段染色 */}
          <g transform={`rotate(-90 ${CX} ${CY})`}>
            {nodeViews.map((nv, i) => (
              <circle
                key={nv.key}
                cx={CX}
                cy={CY}
                r={R}
                fill="none"
                stroke={nv.color}
                strokeWidth={14}
                strokeLinecap="round"
                strokeDasharray={`${SEG} ${CIRC - SEG}`}
                strokeDashoffset={-(i * (CIRC / 5))}
                opacity={0.9}
              />
            ))}
          </g>

          {/* 流动圆点(reduced-motion 时静止分布) */}
          {dots.map((d) => {
            const begin = `${(d * (4 / dotCount)).toFixed(2)}s`;
            const angle = nodeAngle(d * (5 / dotCount));
            const sx = CX + R * Math.cos(angle);
            const sy = CY + R * Math.sin(angle);
            return (
              <circle key={d} r={3.2} fill="#e2f5d0" cx={reduced ? sx : undefined} cy={reduced ? sy : undefined} opacity={0.85}>
                {!reduced && (
                  <animateMotion dur="4s" begin={begin} repeatCount="indefinite" rotate="auto">
                    <mpath href="#flywheel-ring-path" />
                  </animateMotion>
                )}
              </circle>
            );
          })}

          {/* 中心健康度 */}
          <text x={CX} y={CY - 18} textAnchor="middle" className="fill-muted-foreground" fontSize={15}>
            飞轮健康度
          </text>
          <text x={CX} y={CY + 22} textAnchor="middle" className="fill-emerald-400" fontSize={46} fontWeight={700}>
            {healthPercent}%
          </text>
          <text x={CX} y={CY + 46} textAnchor="middle" className="fill-muted-foreground" fontSize={12}>
            {healthLabel}
          </text>
        </svg>

        {/* 节点卡:按角度绝对定位 */}
        {nodeViews.map((nv) => {
          const angle = nodeAngle(nv.index - 1);
          const left = 50 + 50 * (R / (R + 42)) * Math.cos(angle);
          const top = 50 + 50 * (R / (R + 42)) * Math.sin(angle);
          return (
            <button
              key={nv.key}
              type="button"
              onClick={() => onNodeClick?.(nv.tabTarget || nv.key)}
              className="absolute w-[128px] -translate-x-1/2 -translate-y-1/2 rounded-xl border border-border/60 bg-card/90 px-3 py-2 text-left shadow-lg backdrop-blur transition hover:border-foreground/30 hover:bg-card focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              style={{ left: `${left}%`, top: `${top}%` }}
            >
              <div className="flex items-center gap-1.5">
                <span
                  className="grid h-5 w-5 shrink-0 place-items-center rounded-full text-[11px] font-bold text-background"
                  style={{ backgroundColor: nv.color }}
                >
                  {nv.index}
                </span>
                <span className="truncate text-[12px] font-medium text-muted-foreground">{nv.label}</span>
                {nv.siteScope && (
                  <span
                    className="ml-auto shrink-0 rounded bg-amber-500/15 px-1 py-0.5 text-[10px] leading-none text-amber-300"
                    title="该环在库里没有行业维度,这个数是全站口径,不随上方行业选择变化"
                  >
                    全站
                  </span>
                )}
              </div>
              <div className="mt-1 text-lg font-semibold leading-tight text-foreground">{nv.value}</div>
              {nv.sub && <div className="text-[11px] text-muted-foreground">{nv.sub}</div>}
            </button>
          );
        })}
      </div>

      {/* 移动端:纵向五步列表 */}
      <ol className="space-y-2 md:hidden">
        {nodeViews.map((nv, i) => (
          <li key={nv.key}>
            <button
              type="button"
              onClick={() => onNodeClick?.(nv.tabTarget || nv.key)}
              className="flex w-full items-center gap-3 rounded-xl border border-border/60 bg-card/80 px-3 py-2.5 text-left transition hover:border-foreground/30"
            >
              <span
                className="grid h-7 w-7 shrink-0 place-items-center rounded-full text-xs font-bold text-background"
                style={{ backgroundColor: nv.color }}
              >
                {nv.index}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-xs text-muted-foreground">
                  {nv.label}
                  {nv.siteScope && (
                    <span
                      className="ml-1.5 rounded bg-amber-500/15 px-1 py-0.5 text-[10px] leading-none text-amber-300"
                      title="该环在库里没有行业维度,这个数是全站口径,不随上方行业选择变化"
                    >
                      全站
                    </span>
                  )}
                </span>
                <span className="text-base font-semibold text-foreground">{nv.value}</span>
              </span>
              {i < nodeViews.length - 1 && <span className="text-muted-foreground">↓</span>}
            </button>
          </li>
        ))}
        <li className={cn('rounded-xl border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-center')}>
          <span className="text-sm text-muted-foreground">飞轮健康度 </span>
          <span className="text-lg font-bold text-emerald-400">{healthPercent}%</span>
          <span className="text-sm text-muted-foreground"> · {healthLabel}</span>
        </li>
      </ol>
    </div>
  );
}
