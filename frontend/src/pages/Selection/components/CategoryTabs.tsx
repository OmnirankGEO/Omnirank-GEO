interface CategoryCount {
  category: string;
  label: string;
  count: number;
}

interface Props {
  categories: CategoryCount[];
  activeCategory: string;
  onSelect: (category: string) => void;
}

export function CategoryTabs({ categories, activeCategory, onSelect }: Props) {
  return (
    <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto overflow-x-auto scrollbar-hide px-5 py-3 md:py-4">
      <div className="flex gap-2 md:gap-2.5 min-w-max md:flex-wrap">
        {categories.map(cat => (
          <button
            key={cat.category}
            onClick={() => onSelect(cat.category)}
            className={`
              px-4 py-2 md:px-5 md:py-2.5 rounded-full text-sm font-medium whitespace-nowrap transition-all duration-200
              ${activeCategory === cat.category
                ? 'bg-blue-600 text-white shadow-md shadow-blue-200'
                : 'bg-white text-gray-500 border border-gray-200 hover:border-blue-300 hover:text-blue-600 hover:shadow-xs'
              }
            `}
          >
            {cat.label}
            <span className={`ml-1.5 ${activeCategory === cat.category ? 'text-blue-200' : 'text-gray-300'}`}>
              {cat.count}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
