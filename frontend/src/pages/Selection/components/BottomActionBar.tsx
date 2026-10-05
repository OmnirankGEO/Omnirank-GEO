interface Props {
  selectedCount: number;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  loading?: boolean;
  secondaryInfo?: string;
  /**
   * [#178 2026-09-12] 点不动时**就地**说明为什么。
   *
   * 这个条里的按钮本来在 `selectedCount === 0` 时就会自己 disabled,而且不说话 ——
   * 客户看到的是一个灰掉的「确认」,和死按钮没有区别。原因必须画在按钮旁边:
   * 不是 toast(会飘走)、不是 title(手机上点不出来)。
   */
  disabledReason?: string;
}

export function BottomActionBar({ selectedCount, label, onClick, disabled, loading, secondaryInfo, disabledReason }: Props) {
  const isDisabled = disabled || loading || selectedCount === 0;
  return (
    <div className="fixed bottom-0 left-0 right-0 z-20 safe-area-pb">
      <div className="bg-white/70 backdrop-blur-xl border-t border-gray-200/80">
        <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 py-4 md:py-5 flex items-center justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center justify-center w-7 h-7 md:w-8 md:h-8 rounded-full bg-blue-100 text-blue-700 text-xs md:text-sm font-bold">
                {selectedCount}
              </span>
              <span className="text-sm md:text-base text-gray-600 font-medium">词已选</span>
            </div>
            {secondaryInfo && (
              <p className="text-xs md:text-sm text-gray-400 mt-1 truncate">{secondaryInfo}</p>
            )}
          </div>
          {isDisabled && disabledReason && (
            <p className="text-xs text-amber-700 text-right max-w-[52%] shrink" data-testid="bottom-bar-disabled-reason">
              {disabledReason}
            </p>
          )}
          <button
            onClick={onClick}
            disabled={isDisabled}
            className="
              px-7 py-3 md:px-8 md:py-3.5 text-sm md:text-base font-semibold text-white rounded-xl
              bg-linear-to-r from-blue-600 to-blue-700
              hover:from-blue-700 hover:to-blue-800
              active:scale-95
              disabled:opacity-40 disabled:cursor-not-allowed disabled:active:scale-100
              transition-all duration-200
              shadow-lg shadow-blue-200/50
              flex items-center gap-2 whitespace-nowrap
            "
          >
            {loading && (
              <svg className="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
            )}
            {label}
          </button>
        </div>
      </div>
    </div>
  );
}
