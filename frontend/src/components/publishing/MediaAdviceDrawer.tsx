import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode, RefObject } from 'react';
import { ChevronDown, ChevronUp, Lightbulb } from 'lucide-react';
import { cn } from '@/lib/utils';

export interface MediaAdviceSection {
  key: string;
  /** 收起态把手上不显示各块标题,只在展开后作为小标题出现;没有标题就直接铺内容。 */
  title?: string;
  node: ReactNode;
}

interface MediaAdviceDrawerProps {
  sections: MediaAdviceSection[];
  open: boolean;
  onToggle: () => void;
  /** 拖拽高度的持久化 key。**必须按用户维度**传(见下方 L4 说明)。 */
  storageKey: string;
  /** 量"能给抽屉多少高"的参照元素(左栏)。给了才允许拖。 */
  boundsRef?: RefObject<HTMLElement | null>;
  className?: string;
}

/** 抽屉展开后自身最小高度:够露出把手 + 一点内容,再小就没有展开的意义。 */
export const MIN_DRAWER_PX = 96;
/** 上方文章列表至少要留的高度 ≈ 3 行文章卡片。列表被拖到看不见比不能拖更糟。 */
export const MIN_LIST_PX = 168;
/** 没有存过高度时的默认展开高度。 */
export const DEFAULT_DRAWER_PX = 240;

/**
 * 把抽屉高度夹进 [MIN_DRAWER_PX, containerH - MIN_LIST_PX]。
 *
 * 🔴 `containerH` 小到连 `MIN_DRAWER_PX + MIN_LIST_PX` 都放不下时(超矮视口),
 * 上界会低于下界。此时**以上界为准**(优先保住列表),并且再兜一层 `Math.max(0, …)`
 * —— 工单 L4 明写"窗口 resize 时不许出现负高度"。
 * 导出成纯函数是为了能被单测直接打,不用先渲染组件。
 */
export function clampDrawerHeight(desired: number, containerH: number): number {
  const upper = containerH - MIN_LIST_PX;
  if (!Number.isFinite(desired) || !Number.isFinite(containerH)) return MIN_DRAWER_PX;
  if (upper <= MIN_DRAWER_PX) return Math.max(0, upper);
  return Math.max(MIN_DRAWER_PX, Math.min(desired, upper));
}

/**
 * 读回上次拖到的高度。
 *
 * 导出成纯函数(而不是埋在组件里)是为了能被行为断言直接打 ——
 * 工单 L4 明写「必须记住用户上次的高度,不记 = 每次进来重置,比没有更烦」,
 * 这条要求只能靠"读+写"的行为来证,靠 grep 源码里有没有 localStorage 是弱锁。
 *
 * 脏值(空串/非数字/0/负数)一律当没存过,返回 null 让调用方走默认值。
 */
export function readStoredHeight(storageKey: string): number | null {
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : null;
  } catch {
    return null;
  }
}

/** 写回拖完的高度。隐私模式写不进不影响功能,所以吞掉异常。 */
export function writeStoredHeight(storageKey: string, height: number): void {
  try {
    localStorage.setItem(storageKey, String(Math.round(height)));
  } catch {
    /* 隐私模式写不进不影响功能 */
  }
}

/**
 * 发布中心左栏底部「媒体建议」抽屉(WO-PUBCENTER-LAYOUT-2026-08-04 · L3 + L4)。
 *
 * ## L3:三块辅助信息合成一条把手
 *
 * 左栏底部原先竖着摞了三块**各自独立**的辅助面板:
 *   1. 「AI 最常引用的网站」(T1 trunk)
 *   2. 「这篇建议怎么搭配媒体」(T2 combo)
 *   3. 「本行业 AI 真实引用媒体榜」(MediaEffectivenessPanel)
 *
 * 每块都自带标题行,收起态也各占一行,展开更是把文章列表挤到只剩一条缝 ——
 * 生产实测:即使项目里**一篇文章都没有**,这三块也已经吃满整个左栏。
 *
 * 工单 §4 L3 点名的是后两块;这里把 T1 一起收进来,理由是它和另外两块同类
 * (都是"选媒体时的参考信息"),留它在外面单独占一行的话,
 * "收起态只占 1 行"这个验收就只是数字上达标、老板看到的还是好几行。
 *
 * 🔴 **不许移到右栏**。老板已明确否掉:「如果选A，移到右边，右边的可见又会变低。
 * 小屏幕直接看不到下面的选择。」本组件只在左栏内做纵向折叠。
 *
 * 🔴 展开也遮不住底部购物车/一键发布条:那条是**页面级**元素(PublishCenter 里
 * 与左右两栏并列的 sticky 兄弟节点),本抽屉在左栏内部,结构上够不着它。
 *
 * ## L4:展开态可拖,且记得住
 *
 * 老板原话「让用户可以拖动，以及收缩，这样他可以自由变化」。
 * 拖动条只在展开时出现,拖完把像素高度写进 localStorage —— 工单明写
 * 「必须记住用户上次的高度,不记 = 每次进来重置,比没有更烦」。
 * `storageKey` 由调用方按用户维度拼(同一台电脑换账号不该继承上一个人的高度)。
 */
export function MediaAdviceDrawer({
  sections,
  open,
  onToggle,
  storageKey,
  boundsRef,
  className,
}: MediaAdviceDrawerProps) {
  const [height, setHeight] = useState<number>(() => readStoredHeight(storageKey) ?? DEFAULT_DRAWER_PX);
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef<{ startY: number; startH: number } | null>(null);

  // 换账号(storageKey 变)时重新读一次,别让上一个人的高度粘住。
  useEffect(() => {
    setHeight(readStoredHeight(storageKey) ?? DEFAULT_DRAWER_PX);
  }, [storageKey]);

  const containerHeight = useCallback(
    () => boundsRef?.current?.getBoundingClientRect().height ?? 0,
    [boundsRef],
  );

  // 窗口/左栏尺寸变化时按当前容器重新夹一次 —— 否则从大屏切小屏后
  // 抽屉会把列表整个吃掉(甚至算出负高度)。
  useEffect(() => {
    const el = boundsRef?.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => {
      setHeight(prev => clampDrawerHeight(prev, el.getBoundingClientRect().height));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [boundsRef]);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!open) return;
    e.preventDefault();
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    dragRef.current = { startY: e.clientY, startH: height };
    setDragging(true);
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (!d) return;
    // 把手在抽屉**上沿**:往上拖(clientY 变小)= 抽屉变高。
    const next = d.startH + (d.startY - e.clientY);
    setHeight(clampDrawerHeight(next, containerHeight()));
  };

  const endDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return;
    (e.target as HTMLElement).releasePointerCapture?.(e.pointerId);
    dragRef.current = null;
    setDragging(false);
    writeStoredHeight(storageKey, height);
  };

  return (
    <div
      data-media-advice-drawer=""
      data-drawer-open={open ? 'true' : 'false'}
      className={cn('flex shrink-0 flex-col border-t border-border', className)}
      style={open ? { height: Math.round(height) } : undefined}
    >
      {open && (
        <div
          role="separator"
          aria-orientation="horizontal"
          aria-label="拖动调整媒体建议高度"
          data-drawer-resizer=""
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          className={cn(
            'group flex h-2 shrink-0 cursor-row-resize items-center justify-center',
            dragging ? 'bg-primary/20' : 'hover:bg-secondary',
          )}
        >
          <div className="h-0.5 w-8 rounded-full bg-border group-hover:bg-primary/50" />
        </div>
      )}
      {/* 收起态的全部可见高度就是这一行把手。 */}
      <button
        type="button"
        onClick={onToggle}
        data-drawer-handle=""
        aria-expanded={open}
        className="flex w-full shrink-0 items-center justify-between px-3 py-1.5 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-secondary/50 hover:text-foreground"
      >
        <span className="flex items-center gap-1.5">
          <Lightbulb className="size-3" />
          媒体建议与榜单
        </span>
        {open ? <ChevronUp className="size-3" /> : <ChevronDown className="size-3" />}
      </button>
      {open && (
        <div data-drawer-body="" className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2 pb-2">
          {sections.map(s => (
            <div key={s.key} data-drawer-section={s.key}>
              {s.node}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
