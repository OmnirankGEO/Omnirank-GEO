"""
test_keyword_expander_matrix.py · B4 (CTO-15.9 session 3 · 2026-04-25)

M1b §M2 + 2026-07-23 Owner 商业意图红线落地验证:
  - 6 层关键词矩阵 _KEYWORD_LAYERS_MATRIX 6 关键词全在
  - 行业 4 模板 _resolve_industry_template 子串匹配正确
  - 4 模板各 prompt 含核心约束词
  - 业务类型 addon(已有 6 组合)向后兼容

防退化:不允许漏 6 层中任一层 · 不允许 4 模板 prompt 突变
"""
from __future__ import annotations

import pytest


REQUIRED_LAYERS = [
    "类目服务商层",
    "场景方案层",
    "地域转化层",
    "价格询盘层",
    "方案比较层",
    "证据限定选择层",
]


def test_six_layers_matrix_complete():
    """6 层关键词矩阵关键词全部在"""
    from tools.keyword_expander import _KEYWORD_LAYERS_MATRIX
    for layer in REQUIRED_LAYERS:
        assert layer in _KEYWORD_LAYERS_MATRIX, f"6 层矩阵缺:{layer}"


def test_six_layers_have_ratio_guidance():
    """每层应给占比指引(代理可看 prompt 知道侧重)"""
    from tools.keyword_expander import _KEYWORD_LAYERS_MATRIX
    # 六层都应给出比例，防止模型退化成单一的知识问句列表。
    pct_count = _KEYWORD_LAYERS_MATRIX.count("%")
    assert pct_count >= 5, f"6 层矩阵应至少 5 处占比标识 · 实有 {pct_count}"


def test_six_layers_are_purchase_decisions_not_knowledge_questions():
    from tools.keyword_expander import _KEYWORD_LAYERS_MATRIX

    assert "品牌、服务商、方案、价格或比较选项" in _KEYWORD_LAYERS_MATRIX
    assert "不能生成知识问答" in _KEYWORD_LAYERS_MATRIX
    assert "流程说明" in _KEYWORD_LAYERS_MATRIX


@pytest.mark.parametrize("industry,expected_template", [
    # local_service
    ("家装", "local_service"),
    ("装修公司", "local_service"),
    ("美容医美", "local_service"),
    ("北京餐饮店", "local_service"),
    ("婚纱摄影", "local_service"),
    ("少儿教育培训", "local_service"),
    ("律师事务所", "local_service"),
    ("口腔医院", "local_service"),
    ("家政服务", "local_service"),
    # consumer
    ("电商美妆", "consumer"),
    ("零食品牌", "consumer"),
    ("服装品牌", "consumer"),
    ("数码产品", "consumer"),
    # B2B
    ("SaaS 企业服务", "B2B"),
    ("CRM 软件", "B2B"),
    ("ERP 系统", "B2B"),
    ("外贸服务", "B2B"),
    # industrial
    ("机械设备", "industrial"),
    ("化工原料", "industrial"),
    ("精密制造", "industrial"),
    ("工业品", "industrial"),
    ("重工业", "industrial"),
    # 不命中
    ("其他", None),
    ("综合服务", None),
    ("", None),
])
def test_industry_template_matching(industry, expected_template):
    """行业子串匹配 · 优先级:industrial > B2B > consumer > local_service"""
    from tools.keyword_expander import _resolve_industry_template, _INDUSTRY_TEMPLATE_ADDONS
    addon = _resolve_industry_template(industry)
    if expected_template is None:
        assert addon == "", f"行业 '{industry}' 不应命中模板 · 实命中 {addon[:50]}"
    else:
        expected_addon = _INDUSTRY_TEMPLATE_ADDONS[expected_template]
        assert addon == expected_addon, f"行业 '{industry}' 应命中 {expected_template}"


def test_industrial_template_demands_supplier_or_solution_options():
    """工业采购可以问哪家好，但必须落到厂家、供应商或方案。"""
    from tools.keyword_expander import _INDUSTRY_TEMPLATE_ADDONS
    industrial = _INDUSTRY_TEMPLATE_ADDONS["industrial"]
    assert "厂家" in industrial
    assert "供应商" in industrial
    assert "方案报价" in industrial
    assert "禁止只问参数" in industrial


def test_b2b_template_demands_supplier_or_solution_options():
    """B2B 关键词必须服务于服务商、方案和报价决策。"""
    from tools.keyword_expander import _INDUSTRY_TEMPLATE_ADDONS
    b2b = _INDUSTRY_TEMPLATE_ADDONS["B2B"]
    assert "服务商" in b2b
    assert "供应商" in b2b
    assert "报价" in b2b
    assert "必须明确索要供给选项" in b2b


def test_local_service_template_emphasizes_geo():
    """本地服务模板应强调地域转化层"""
    from tools.keyword_expander import _INDUSTRY_TEMPLATE_ADDONS
    local = _INDUSTRY_TEMPLATE_ADDONS["local_service"]
    assert "地域" in local or "{城市}" in local


def test_business_type_addon_backward_compat():
    """B4 不破坏 business_type 6 组合 addon"""
    from tools.keyword_expander import _resolve_business_type_addon
    # 6 组合
    cases = [
        ("B2C", "local"),
        ("B2C", "national"),
        ("B2B", "local"),
        ("B2B", "national"),
        ("政企", "local"),
        ("政企", "national"),
    ]
    for bt, cs in cases:
        addon = _resolve_business_type_addon(bt, cs)
        assert addon, f"({bt}, {cs}) addon 缺失"
    # fallback 到 B2C/local
    addon_unknown = _resolve_business_type_addon("XXX", "YYY")
    addon_default = _resolve_business_type_addon("B2C", "local")
    assert addon_unknown == addon_default, "未知组合应 fallback B2C/local"


def test_keyword_expand_combined_context_order():
    """验 combined_context 拼接顺序:6 层矩阵(基)+= 行业模板 += business_type += industry_context"""
    import inspect
    from tools import keyword_expander
    src = inspect.getsource(keyword_expander)
    # 找到 combined_context 的赋值/+= 语句
    # 期望:
    #   combined_context = _KEYWORD_LAYERS_MATRIX  (基)
    #   if industry_template_addon: combined_context += industry_template_addon
    #   if business_type_addon: combined_context += business_type_addon
    #   if industry_context: combined_context += industry_context
    base_idx = src.find("combined_context = _KEYWORD_LAYERS_MATRIX")
    template_add_idx = src.find("combined_context += industry_template_addon")
    bt_add_idx = src.find("combined_context += business_type_addon")
    ind_ctx_add_idx = src.find("combined_context += industry_context")
    assert base_idx > 0, "combined_context 应以 _KEYWORD_LAYERS_MATRIX 起手"
    assert template_add_idx > base_idx, "行业模板应在基之后追加"
    assert bt_add_idx > template_add_idx, "business_type 应在行业模板之后追加"
    assert ind_ctx_add_idx > bt_add_idx, "industry_context 应在 business_type 之后追加"
