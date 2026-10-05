export type GeoIndustryOption = {
  value: string;
  label: string;
};

export const GEO_INDUSTRY_OPTIONS: GeoIndustryOption[] = [
  { value: 'tourism-hotel', label: '旅游酒店' },
  { value: 'real-estate', label: '房地产/房产' },
  { value: 'home-decor', label: '装修/家居建材' },
  { value: 'auto-mobility', label: '汽车/出行' },
  { value: 'education-training', label: '教育培训' },
  { value: 'medical-health', label: '医疗健康/医美' },
  { value: 'technology-digital', label: '科技数码/软件' },
  { value: 'food-beverage', label: '餐饮/食品' },
  { value: 'finance-insurance', label: '金融理财' },
  { value: 'legal-service', label: '法律服务' },
  { value: 'business-service', label: '企业服务/咨询' },
  { value: 'retail-ecommerce', label: '电商零售' },
  { value: 'manufacturing-industrial', label: '制造业/工业设备' },
];

export const GEO_INDUSTRY_OPTIONS_WITH_GENERAL: GeoIndustryOption[] = [
  { value: 'general', label: '通用/全部行业' },
  ...GEO_INDUSTRY_OPTIONS,
];

export function geoIndustryLabel(value: string): string {
  return GEO_INDUSTRY_OPTIONS_WITH_GENERAL.find((option) => option.value === value)?.label || value;
}
