interface Props {
  selectedCount: number;
  totalCount: number;
}

export function StatsBar({ selectedCount, totalCount }: Props) {
  const pct = totalCount > 0 ? Math.round((selectedCount / totalCount) * 100) : 0;

  return (
    <div className="sticky top-0 z-10 bg-white/80 backdrop-blur-xl border-b border-gray-100/80 shadow-xs">
      <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 py-3 md:py-4">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center justify-center w-7 h-7 md:w-8 md:h-8 rounded-full bg-blue-600 text-white text-xs md:text-sm font-bold shadow-md shadow-blue-200">
              {selectedCount}
            </span>
            <span className="text-sm md:text-base text-gray-500">
              / {totalCount} 词已选
            </span>
          </div>
          <span className="text-sm md:text-base font-semibold text-blue-600">{pct}%</span>
        </div>
        <div className="w-full h-2 md:h-2.5 bg-gray-100 rounded-full overflow-hidden">
          <div
            className="h-full bg-linear-to-r from-blue-500 to-indigo-500 rounded-full transition-all duration-500 ease-out"
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>
    </div>
  );
}
