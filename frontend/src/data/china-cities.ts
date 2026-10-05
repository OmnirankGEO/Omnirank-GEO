/**
 * 中国城市坐标库（基于 2025 第一财经城市分级）
 * 1线 4 + 新1线 15 + 2线 30 + 3线 30 + 4线 30 + 5线 20 = 129 城市
 */

export interface CityCoord {
  name: string;
  province: string;
  tier: number;
  lng: number;
  lat: number;
}

export const ALL_CITIES: CityCoord[] = [
  // ===== 一线城市 (4) =====
  { name: '北京', province: '北京', tier: 1, lng: 116.407, lat: 39.904 },
  { name: '上海', province: '上海', tier: 1, lng: 121.473, lat: 31.230 },
  { name: '广州', province: '广东', tier: 1, lng: 113.264, lat: 23.129 },
  { name: '深圳', province: '广东', tier: 1, lng: 114.058, lat: 22.543 },

  // ===== 新一线城市 (15) =====
  { name: '成都', province: '四川', tier: 1, lng: 104.066, lat: 30.573 },
  { name: '重庆', province: '重庆', tier: 1, lng: 106.551, lat: 29.563 },
  { name: '杭州', province: '浙江', tier: 1, lng: 120.155, lat: 30.274 },
  { name: '武汉', province: '湖北', tier: 1, lng: 114.305, lat: 30.593 },
  { name: '苏州', province: '江苏', tier: 1, lng: 120.585, lat: 31.299 },
  { name: '西安', province: '陕西', tier: 1, lng: 108.940, lat: 34.341 },
  { name: '南京', province: '江苏', tier: 1, lng: 118.796, lat: 32.059 },
  { name: '长沙', province: '湖南', tier: 1, lng: 112.938, lat: 28.228 },
  { name: '天津', province: '天津', tier: 1, lng: 117.200, lat: 39.084 },
  { name: '郑州', province: '河南', tier: 1, lng: 113.625, lat: 34.747 },
  { name: '东莞', province: '广东', tier: 1, lng: 113.746, lat: 23.046 },
  { name: '青岛', province: '山东', tier: 1, lng: 120.383, lat: 36.067 },
  { name: '昆明', province: '云南', tier: 1, lng: 102.833, lat: 25.019 },
  { name: '宁波', province: '浙江', tier: 1, lng: 121.550, lat: 29.874 },
  { name: '合肥', province: '安徽', tier: 1, lng: 117.227, lat: 31.821 },

  // ===== 二线城市 (30) =====
  { name: '佛山', province: '广东', tier: 2, lng: 113.122, lat: 23.009 },
  { name: '沈阳', province: '辽宁', tier: 2, lng: 123.432, lat: 41.808 },
  { name: '济南', province: '山东', tier: 2, lng: 117.000, lat: 36.675 },
  { name: '无锡', province: '江苏', tier: 2, lng: 120.312, lat: 31.491 },
  { name: '厦门', province: '福建', tier: 2, lng: 118.089, lat: 24.479 },
  { name: '福州', province: '福建', tier: 2, lng: 119.296, lat: 26.074 },
  { name: '温州', province: '浙江', tier: 2, lng: 120.699, lat: 28.000 },
  { name: '哈尔滨', province: '黑龙江', tier: 2, lng: 126.642, lat: 45.757 },
  { name: '大连', province: '辽宁', tier: 2, lng: 121.614, lat: 38.914 },
  { name: '石家庄', province: '河北', tier: 2, lng: 114.514, lat: 38.042 },
  { name: '贵阳', province: '贵州', tier: 2, lng: 106.630, lat: 26.647 },
  { name: '南宁', province: '广西', tier: 2, lng: 108.366, lat: 22.817 },
  { name: '金华', province: '浙江', tier: 2, lng: 119.649, lat: 29.079 },
  { name: '常州', province: '江苏', tier: 2, lng: 119.974, lat: 31.811 },
  { name: '珠海', province: '广东', tier: 2, lng: 113.576, lat: 22.271 },
  { name: '惠州', province: '广东', tier: 2, lng: 114.416, lat: 23.112 },
  { name: '嘉兴', province: '浙江', tier: 2, lng: 120.755, lat: 30.746 },
  { name: '南昌', province: '江西', tier: 2, lng: 115.858, lat: 28.683 },
  { name: '中山', province: '广东', tier: 2, lng: 113.392, lat: 22.517 },
  { name: '保定', province: '河北', tier: 2, lng: 115.465, lat: 38.874 },
  { name: '兰州', province: '甘肃', tier: 2, lng: 103.834, lat: 36.061 },
  { name: '台州', province: '浙江', tier: 2, lng: 121.421, lat: 28.656 },
  { name: '徐州', province: '江苏', tier: 2, lng: 117.284, lat: 34.205 },
  { name: '太原', province: '山西', tier: 2, lng: 112.549, lat: 37.870 },
  { name: '绍兴', province: '浙江', tier: 2, lng: 120.580, lat: 30.030 },
  { name: '烟台', province: '山东', tier: 2, lng: 121.448, lat: 37.464 },
  { name: '廊坊', province: '河北', tier: 2, lng: 116.683, lat: 39.538 },
  { name: '潍坊', province: '山东', tier: 2, lng: 119.142, lat: 36.706 },
  { name: '临沂', province: '山东', tier: 2, lng: 118.356, lat: 35.104 },
  { name: '长春', province: '吉林', tier: 2, lng: 125.324, lat: 43.886 },

  // ===== 三线城市 (30) =====
  { name: '洛阳', province: '河南', tier: 3, lng: 112.454, lat: 34.619 },
  { name: '唐山', province: '河北', tier: 3, lng: 118.180, lat: 39.631 },
  { name: '遵义', province: '贵州', tier: 3, lng: 106.937, lat: 27.725 },
  { name: '银川', province: '宁夏', tier: 3, lng: 106.278, lat: 38.487 },
  { name: '芜湖', province: '安徽', tier: 3, lng: 118.376, lat: 31.326 },
  { name: '泉州', province: '福建', tier: 3, lng: 118.676, lat: 24.874 },
  { name: '漳州', province: '福建', tier: 3, lng: 117.647, lat: 24.513 },
  { name: '呼和浩特', province: '内蒙古', tier: 3, lng: 111.749, lat: 40.842 },
  { name: '乌鲁木齐', province: '新疆', tier: 3, lng: 87.617, lat: 43.793 },
  { name: '赣州', province: '江西', tier: 3, lng: 114.935, lat: 25.831 },
  { name: '揭阳', province: '广东', tier: 3, lng: 116.373, lat: 23.550 },
  { name: '南通', province: '江苏', tier: 3, lng: 120.894, lat: 31.980 },
  { name: '盐城', province: '江苏', tier: 3, lng: 120.163, lat: 33.347 },
  { name: '湛江', province: '广东', tier: 3, lng: 110.359, lat: 21.271 },
  { name: '邯郸', province: '河北', tier: 3, lng: 114.539, lat: 36.625 },
  { name: '汕头', province: '广东', tier: 3, lng: 116.681, lat: 23.354 },
  { name: '济宁', province: '山东', tier: 3, lng: 116.587, lat: 35.414 },
  { name: '镇江', province: '江苏', tier: 3, lng: 119.425, lat: 32.188 },
  { name: '宜昌', province: '湖北', tier: 3, lng: 111.286, lat: 30.691 },
  { name: '襄阳', province: '湖北', tier: 3, lng: 112.122, lat: 32.009 },
  { name: '柳州', province: '广西', tier: 3, lng: 109.416, lat: 24.325 },
  { name: '绵阳', province: '四川', tier: 3, lng: 104.679, lat: 31.468 },
  { name: '湖州', province: '浙江', tier: 3, lng: 120.086, lat: 30.894 },
  { name: '衡阳', province: '湖南', tier: 3, lng: 112.572, lat: 26.893 },
  { name: '株洲', province: '湖南', tier: 3, lng: 113.134, lat: 27.828 },
  { name: '岳阳', province: '湖南', tier: 3, lng: 113.133, lat: 29.357 },
  { name: '南阳', province: '河南', tier: 3, lng: 112.528, lat: 32.991 },
  { name: '威海', province: '山东', tier: 3, lng: 122.116, lat: 37.510 },
  { name: '桂林', province: '广西', tier: 3, lng: 110.290, lat: 25.274 },
  { name: '海口', province: '海南', tier: 3, lng: 110.350, lat: 20.020 },

  // ===== 四线城市 (30) =====
  { name: '连云港', province: '江苏', tier: 4, lng: 119.222, lat: 34.596 },
  { name: '咸阳', province: '陕西', tier: 4, lng: 108.709, lat: 34.330 },
  { name: '九江', province: '江西', tier: 4, lng: 115.993, lat: 29.712 },
  { name: '新乡', province: '河南', tier: 4, lng: 113.884, lat: 35.303 },
  { name: '许昌', province: '河南', tier: 4, lng: 113.852, lat: 34.036 },
  { name: '商丘', province: '河南', tier: 4, lng: 115.650, lat: 34.437 },
  { name: '黄冈', province: '湖北', tier: 4, lng: 114.872, lat: 30.453 },
  { name: '荆州', province: '湖北', tier: 4, lng: 112.239, lat: 30.335 },
  { name: '常德', province: '湖南', tier: 4, lng: 111.699, lat: 29.032 },
  { name: '北海', province: '广西', tier: 4, lng: 109.120, lat: 21.481 },
  { name: '泸州', province: '四川', tier: 4, lng: 105.443, lat: 28.872 },
  { name: '德阳', province: '四川', tier: 4, lng: 104.398, lat: 31.128 },
  { name: '宜宾', province: '四川', tier: 4, lng: 104.643, lat: 28.752 },
  { name: '南充', province: '四川', tier: 4, lng: 106.111, lat: 30.837 },
  { name: '大庆', province: '黑龙江', tier: 4, lng: 125.104, lat: 46.589 },
  { name: '包头', province: '内蒙古', tier: 4, lng: 109.840, lat: 40.658 },
  { name: '宝鸡', province: '陕西', tier: 4, lng: 107.238, lat: 34.362 },
  { name: '安庆', province: '安徽', tier: 4, lng: 117.044, lat: 30.509 },
  { name: '蚌埠', province: '安徽', tier: 4, lng: 117.389, lat: 32.917 },
  { name: '阜阳', province: '安徽', tier: 4, lng: 115.814, lat: 32.890 },
  { name: '秦皇岛', province: '河北', tier: 4, lng: 119.600, lat: 39.935 },
  { name: '三亚', province: '海南', tier: 4, lng: 109.508, lat: 18.248 },
  { name: '西宁', province: '青海', tier: 4, lng: 101.778, lat: 36.617 },
  { name: '邢台', province: '河北', tier: 4, lng: 114.508, lat: 37.068 },
  { name: '宿迁', province: '江苏', tier: 4, lng: 118.275, lat: 33.963 },
  { name: '信阳', province: '河南', tier: 4, lng: 114.075, lat: 32.130 },
  { name: '上饶', province: '江西', tier: 4, lng: 117.943, lat: 28.455 },
  { name: '邵阳', province: '湖南', tier: 4, lng: 111.469, lat: 27.239 },
  { name: '吉林', province: '吉林', tier: 4, lng: 126.553, lat: 43.837 },
  { name: '拉萨', province: '西藏', tier: 4, lng: 91.132, lat: 29.660 },

  // ===== 五线城市 (20) =====
  { name: '鹤壁', province: '河南', tier: 5, lng: 114.297, lat: 35.748 },
  { name: '濮阳', province: '河南', tier: 5, lng: 115.029, lat: 35.762 },
  { name: '漯河', province: '河南', tier: 5, lng: 114.017, lat: 33.582 },
  { name: '鹰潭', province: '江西', tier: 5, lng: 117.069, lat: 28.260 },
  { name: '来宾', province: '广西', tier: 5, lng: 109.222, lat: 23.750 },
  { name: '广安', province: '四川', tier: 5, lng: 106.633, lat: 30.456 },
  { name: '巴中', province: '四川', tier: 5, lng: 106.747, lat: 31.868 },
  { name: '雅安', province: '四川', tier: 5, lng: 103.001, lat: 29.988 },
  { name: '白山', province: '吉林', tier: 5, lng: 126.424, lat: 41.943 },
  { name: '鸡西', province: '黑龙江', tier: 5, lng: 130.970, lat: 45.295 },
  { name: '鹤岗', province: '黑龙江', tier: 5, lng: 130.298, lat: 47.350 },
  { name: '七台河', province: '黑龙江', tier: 5, lng: 131.003, lat: 45.771 },
  { name: '丽江', province: '云南', tier: 5, lng: 100.227, lat: 26.855 },
  { name: '大理', province: '云南', tier: 5, lng: 100.225, lat: 25.606 },
  { name: '铜陵', province: '安徽', tier: 5, lng: 117.812, lat: 30.945 },
  { name: '淮北', province: '安徽', tier: 5, lng: 116.798, lat: 33.956 },
  { name: '贺州', province: '广西', tier: 5, lng: 111.567, lat: 24.404 },
  { name: '崇左', province: '广西', tier: 5, lng: 107.365, lat: 22.377 },
  { name: '乌兰察布', province: '内蒙古', tier: 5, lng: 113.133, lat: 41.000 },
  { name: '松原', province: '吉林', tier: 5, lng: 124.825, lat: 45.142 },
];

// 省会映射
export const PROVINCE_CAPITAL: Record<string, CityCoord> = {};
const CAPS: Record<string, string> = {
  '北京':'北京','上海':'上海','天津':'天津','重庆':'重庆','广东':'广州','浙江':'杭州',
  '江苏':'南京','四川':'成都','湖北':'武汉','山东':'济南','河南':'郑州','湖南':'长沙',
  '福建':'福州','安徽':'合肥','江西':'南昌','陕西':'西安','辽宁':'沈阳','云南':'昆明',
  '贵州':'贵阳','广西':'南宁','山西':'太原','河北':'石家庄','吉林':'长春','黑龙江':'哈尔滨',
  '甘肃':'兰州','海南':'海口','内蒙古':'呼和浩特','新疆':'乌鲁木齐','宁夏':'银川',
  '青海':'西宁','西藏':'拉萨',
};
for (const [p, c] of Object.entries(CAPS)) {
  const city = ALL_CITIES.find(x => x.name === c);
  if (city) PROVINCE_CAPITAL[p] = city;
}

export function findCityCoord(cityName: string, province?: string): CityCoord | null {
  if (!cityName && !province) return null;
  const cleaned = (cityName || '').replace(/[市区县州盟地区自治州]/g, '');
  const exact = ALL_CITIES.find(c => c.name === cleaned || c.name === cityName);
  if (exact) return exact;
  const fuzzy = ALL_CITIES.find(c => cleaned.includes(c.name) || c.name.includes(cleaned));
  if (fuzzy) return fuzzy;
  if (province) return PROVINCE_CAPITAL[province] || null;
  return null;
}

export const TIER_LABELS: Record<number, string> = { 1: '一线/新一线', 2: '二线', 3: '三线', 4: '四线', 5: '五线' };
export const TIER_COLORS: Record<number, string> = { 1: '#f43f5e', 2: '#f97316', 3: '#eab308', 4: '#22c55e', 5: '#06b6d4' };
export function getTierColor(tier: number): string { return TIER_COLORS[tier] || '#06b6d4'; }
