/**
 * P0.6 价格 sanity banner 用 · 中国城市 tier 分级
 *
 * 🔴 2026-08-09 订正(WO_VOCAB_CONVERGENCE §8):原注释写"权威源:tools/city_tier_map.py ·
 *    双端手工同步 · commit 必须同步" —— **这句从 2026-06-11 起就是假的**。
 *    那天 city_tier_map.py v2.0 重构删光了 Python 侧六个城市字典(TIER1/NEW_TIER1/TIER2/
 *    TIER3/TIER4/COUNTY_LIST 全部置为 frozenset()),改成后缀启发式:除"X县/X乡/X镇"外
 *    一律 tier3。本文件这份 206 项静态表**是全站唯一还活着的城市字典**,同步不了也不必同步。
 *    (它自己文件里也写着"前端 cityTierMap.ts 独立 SSOT · 不依赖本 Python"。)
 *
 * 🔴 本文件的 TIER_MULTIPLIER **不进算价**。2026-08-09 生产实证:
 *    · 全仓非测试代码里 getCityMultiplier / get_city_multiplier 调用点 = 0
 *    · keyword_value_scorer.py:1048 硬写 `"geo_multiplier": 1.0  # v2 地理已内化进竞争/成本 · 不再连乘`
 *    · keyword_price_cache 65/65 行 geo_multiplier = 1.0(min=max=1)
 *    真正参与定价的 city_tier 由 pricing_llm_assessor(LLM)直接输出,不查任何字典。
 *    这里的系数表保留只为 describeCityTier / isLowConfidenceCity 的**展示**语义。
 *    若将来要让系数重新进价,必须先走 08_billing.md 登记 —— 资金 SSOT 里目前没有这套系数。
 *
 * 消费点(全站仅一处):components/PriceSanityBanner.tsx —— 县级/乡镇给"低置信地域"提示。
 *
 * 老板 2026-04-24 批(系数定义,现仅用于展示):
 *  · 一线 1.0 / 新一线 0.8 / 二线 0.6 / 三线 0.45 / 四线 0.3 / 县级 0.2 / 乡镇 0.1
 *  · 全国 / 不限 / 空串 → tier1(中性)
 *  · 未知城市 fallback → tier3(保守偏低)
 */

export type CityTier =
  | 'tier1'
  | 'new_tier1'
  | 'tier2'
  | 'tier3'
  | 'tier4'
  | 'county'
  | 'township';

export const TIER_MULTIPLIER: Record<CityTier, number> = {
  tier1: 1.0,
  new_tier1: 0.8,
  tier2: 0.6,
  tier3: 0.45,
  tier4: 0.3,
  county: 0.2,
  township: 0.1,
};

const TIER1 = new Set(['北京', '上海', '广州', '深圳']);

const NEW_TIER1 = new Set([
  '成都', '杭州', '武汉', '西安', '苏州', '南京', '重庆', '天津',
  '青岛', '长沙', '郑州', '合肥', '东莞', '宁波', '佛山',
]);

const TIER2 = new Set([
  // 省会/副省级(不含一线/新一线)
  '昆明', '福州', '无锡', '厦门', '大连', '沈阳', '长春', '哈尔滨',
  '济南', '南昌', '南宁', '贵阳', '太原', '石家庄', '海口', '兰州',
  '呼和浩特', '乌鲁木齐', '银川', '西宁', '拉萨',
  // 强二线
  '温州', '绍兴', '泉州', '烟台', '嘉兴', '常州', '徐州', '惠州',
  '珠海', '中山', '台州', '金华',
]);

const TIER3 = new Set([
  // 地级市
  '保定', '唐山', '邯郸', '湖州', '潍坊', '淮安', '洛阳', '南阳', '许昌', '临沂',
  '南通', '扬州', '镇江', '盐城', '衡阳', '株洲', '湘潭', '岳阳', '漳州', '莆田',
  '宁德', '三明', '龙岩', '芜湖', '马鞍山', '蚌埠', '安庆', '阜阳', '汕头', '湛江',
  '茂名', '肇庆', '韶关', '清远', '揭阳', '梅州', '汕尾', '阳江', '河源', '潮州',
  '柳州', '桂林', '北海', '遵义', '绵阳', '南充', '德阳', '宜宾', '自贡', '泸州',
  '曲靖', '玉溪', '大理', '丽江', '红河',
]);

const TIER4 = new Set([
  '承德', '张家口', '秦皇岛', '沧州', '廊坊', '衡水', '邢台', '大同', '长治', '晋城',
  '赤峰', '通辽', '本溪', '丹东', '锦州', '营口', '阜新', '辽阳', '铁岭', '朝阳',
  '盘锦', '吉林', '四平', '辽源', '通化', '白山', '松原', '白城', '齐齐哈尔', '鹤岗',
  '双鸭山', '鸡西', '大庆', '伊春', '佳木斯', '七台河', '牡丹江', '黑河', '绥化',
  '运城', '忻州', '临汾', '吕梁', '晋中', '朔州', '文山', '普洱', '昭通', '临沧',
]);

/** 常见县级市 seed 50 · 含 P0.6 事故样本罗平 · 2 周后运营按 override 率扩 */
const COUNTY_LIST = new Set([
  '罗平', '宜兴', '昆山', '常熟', '太仓', '张家港', '江阴', '义乌', '永康', '瑞安',
  '乐清', '诸暨', '慈溪', '余姚', '海宁', '桐乡', '德清', '长兴', '安吉', '平湖',
  '嵊州', '新昌', '玉环', '温岭', '天台', '仙居', '三门', '奉化', '宁海', '象山',
  '莆田', '晋江', '石狮', '南安', '惠安', '安溪', '永春', '德化', '长乐',
  '福清', '平潭', '马龙', '师宗', '陆良', '会泽', '宣威', '富源', '沾益',
  '开原', '桦甸',
]);

/**
 * 获取城市 tier 分级
 *
 * 规则(按优先级):
 * 1. 空 / 全国 / 不限 → tier1(中性 · 老板批)
 * 2. 乡镇后缀(乡/镇) → township · 排除"镇江"等假阳性
 * 3. 去"市/县/区"后缀精确匹配字典
 * 4. 含"县"后缀 → county
 * 5. fallback → tier3(0.45)
 */
export function getCityTier(city: string): CityTier {
  if (!city || city === '全国' || city === '不限' || city.trim() === '') {
    return 'tier1';
  }
  const trimmed = city.trim();
  // 乡镇判断 · 排除 "镇江" 等二线城市假阳性
  if (/[^镇]镇$|乡$/.test(trimmed) && !trimmed.endsWith('镇江')) {
    // 纯"乡/镇"后缀(如"马过河乡"/"兴隆镇") · 但不是"镇江"
    if (trimmed !== '镇江' && (trimmed.endsWith('乡') || trimmed.endsWith('镇'))) {
      return 'township';
    }
  }

  const norm = trimmed.replace(/市$|县$|区$/, '');

  if (TIER1.has(norm)) return 'tier1';
  if (NEW_TIER1.has(norm)) return 'new_tier1';
  if (TIER2.has(norm)) return 'tier2';
  if (TIER3.has(norm)) return 'tier3';
  if (TIER4.has(norm)) return 'tier4';
  if (COUNTY_LIST.has(norm)) return 'county';

  // 后缀兜底判断
  if (trimmed.endsWith('县')) return 'county';

  // 未知城市 fallback:tier3(老板批 · 保守偏低不过度)
  return 'tier3';
}

export function getCityMultiplier(city: string): number {
  return TIER_MULTIPLIER[getCityTier(city)];
}

/** 是否低置信度地域(县级 / 乡镇)· 用于 P0.6 sanity banner 提示 */
export function isLowConfidenceCity(city: string): boolean {
  const tier = getCityTier(city);
  return tier === 'county' || tier === 'township';
}

/** 中文描述(UI 显示) */
export function describeCityTier(tier: CityTier): string {
  const map: Record<CityTier, string> = {
    tier1: '一线城市',
    new_tier1: '新一线城市',
    tier2: '二线城市',
    tier3: '三线城市',
    tier4: '四线城市',
    county: '县级',
    township: '乡镇',
  };
  return map[tier];
}
