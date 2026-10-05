import { useState } from 'react';

interface Props {
  onAdd: (keyword: string) => void;
  onBatchAdd?: (keywords: string[]) => void;
  placeholder?: string;
}

export function CustomKeywordInput({ onAdd, onBatchAdd, placeholder = '输入关键词，多个用逗号或换行分隔' }: Props) {
  const [value, setValue] = useState('');
  const [mode, setMode] = useState<'single' | 'batch'>('single');

  const handleAdd = () => {
    const trimmed = value.trim();
    if (!trimmed) return;

    if (mode === 'batch' || trimmed.includes(',') || trimmed.includes('，') || trimmed.includes('\n')) {
      // Parse batch: split by comma, Chinese comma, or newline
      const keywords = trimmed
        .split(/[,，\n]/)
        .map(s => s.trim())
        .filter(s => s.length > 0);
      const unique = [...new Set(keywords)];
      if (unique.length === 0) return;

      if (onBatchAdd) {
        onBatchAdd(unique);
      } else {
        // Fallback: call onAdd for each
        unique.forEach(kw => onAdd(kw));
      }
    } else {
      onAdd(trimmed);
    }
    setValue('');
    if (mode === 'batch') setMode('single');
  };

  return (
    <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 py-3">
      <div className="bg-white rounded-xl border border-gray-200 shadow-xs focus-within:border-blue-400 focus-within:shadow-md focus-within:shadow-blue-50 transition-all overflow-hidden">
        {mode === 'single' ? (
          <div className="flex gap-2 p-1.5 md:p-2">
            <input
              type="text"
              value={value}
              onChange={e => setValue(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleAdd()}
              placeholder={placeholder}
              className="flex-1 px-3 py-2.5 md:px-4 md:py-3 text-sm md:text-base bg-transparent focus:outline-hidden placeholder:text-gray-300"
            />
            <button
              onClick={() => setMode('batch')}
              className="px-3 py-2.5 text-xs text-gray-400 hover:text-blue-600 transition-colors whitespace-nowrap"
              title="切换到批量输入"
            >
              批量
            </button>
            <button
              onClick={handleAdd}
              disabled={!value.trim()}
              className="px-5 py-2.5 md:px-6 md:py-3 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 active:scale-95 disabled:opacity-30 disabled:cursor-not-allowed transition-all whitespace-nowrap"
            >
              + 添加
            </button>
          </div>
        ) : (
          <div className="p-3 md:p-4 space-y-2">
            <div className="flex items-center justify-between">
              <p className="text-xs text-gray-500">每行一个关键词，或用逗号分隔</p>
              <button
                onClick={() => setMode('single')}
                className="text-xs text-gray-400 hover:text-gray-600"
              >
                单个输入
              </button>
            </div>
            <textarea
              value={value}
              onChange={e => setValue(e.target.value)}
              placeholder={"南京儿童摄影\n南京婚纱照\n南京亲子照"}
              rows={4}
              className="w-full px-3 py-2.5 text-sm bg-gray-50 rounded-lg border border-gray-100 focus:outline-hidden focus:border-blue-300 resize-none placeholder:text-gray-300"
            />
            <div className="flex items-center justify-between">
              <span className="text-xs text-gray-400">
                {value.trim()
                  ? `${value.split(/[,，\n]/).map(s => s.trim()).filter(Boolean).length} 个关键词`
                  : ''}
              </span>
              <button
                onClick={handleAdd}
                disabled={!value.trim()}
                className="px-5 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 active:scale-95 disabled:opacity-30 disabled:cursor-not-allowed transition-all"
              >
                批量添加
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
