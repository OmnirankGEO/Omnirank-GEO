"""白标价格边界守卫 · 2026-05-30 · 老板约束5

铁律:白标只承载品牌字段,绝不承载价格/系数。
  - 报价系数 → users.quote_markup_ratio(编辑入口 /account/profile)
  - 额度售价 → agent_sku_overrides.retail_cents(编辑入口 /agent/pricing)
  - whitelabel_settings / PUT /whitelabel / 授权位 三处都不许出现任何价格列/字段。

本测试锁死该边界,防未来误把价格塞进白标表混表。
schema + 端点都嵌在 api/referral_api.py 源码里(连 DB 才能跑真查询),故用源码断言。
"""
from __future__ import annotations

import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

# 价格相关片段(大小写不敏感)· 任何一个出现在白标 schema/端点 = 越界
_PRICE_TOKENS = (
    "price", "markup", "cents", "retail", "ratio",
    "wholesale", "commission", "_fee", "fee_", "amount_cents",
)


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


def _whitelabel_create_block(src: str) -> str:
    """截取 whitelabel_settings 的 CREATE TABLE 块(到下一个 CREATE TABLE 前 · 避免误纳 agent_quotes.total_price)。"""
    i = src.find("CREATE TABLE IF NOT EXISTS whitelabel_settings (")
    assert i > 0, "未找到 whitelabel_settings CREATE TABLE"
    nxt = src.find("CREATE TABLE", i + 20)
    return src[i: nxt if nxt > i else i + 1500]


def test_whitelabel_settings_table_has_no_price_column():
    block = _whitelabel_create_block(_read("api/referral_api.py")).lower()
    for tok in _PRICE_TOKENS:
        assert tok not in block, (
            f"whitelabel_settings 出现价格相关片段 '{tok}' · 白标禁承载价格(老板约束5)· "
            f"价格走 users.quote_markup_ratio / agent_sku_overrides.retail_cents"
        )


def test_whitelabel_put_field_map_has_no_price():
    """代理 PUT /whitelabel 的 field_map 只能是品牌字段 · 不许有价格。"""
    src = _read("api/referral_api.py")
    i = src.find("def update_whitelabel(")
    assert i > 0, "未找到 update_whitelabel"
    seg = src[i: i + 4000]
    fm = seg.find("field_map = {")
    assert fm > 0, "未找到 update_whitelabel field_map"
    fmblock = seg[fm: seg.find("}", fm)].lower()
    for tok in _PRICE_TOKENS:
        assert tok not in fmblock, f"PUT /whitelabel field_map 含价格字段 '{tok}' · 越界(约束5)"


def test_whitelabel_authz_fields_are_not_price():
    """_WL_AUTHZ_FIELDS 是 admin 授权位 · 不应混入价格字段。"""
    src = _read("api/referral_api.py")
    i = src.find("_WL_AUTHZ_FIELDS = frozenset({")
    assert i > 0, "未找到 _WL_AUTHZ_FIELDS"
    block = src[i: src.find("})", i)].lower()
    for tok in _PRICE_TOKENS:
        assert tok not in block, f"_WL_AUTHZ_FIELDS 含价格字段 '{tok}'"


def test_price_systems_live_outside_whitelabel():
    """正向锚:报价系数 / 额度售价 确实在各自的表/端点(不在白标),防有人把它们迁进白标。"""
    auth_src = _read("db/auth_db.py")
    assert "quote_markup_ratio" in auth_src, "报价系数应在 db/auth_db.py(users.quote_markup_ratio)"
    # retail_cents 属 agent_sku_overrides(迁移脚本定义)· 仅断言不在白标 schema
    wl_block = _whitelabel_create_block(_read("api/referral_api.py")).lower()
    assert "quote_markup_ratio" not in wl_block, "报价系数不许进白标表"
