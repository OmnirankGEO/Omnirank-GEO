"""旧 sku_template_id 购买路径的身份、下架与竞态静态守护。

历史模板身份只能由 ``sku_template_id`` 唯一解析。单次加锁查询同时排除
下架/tombstone；无匹配或多匹配都 fail-closed，不能回落平台默认价。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_wallet_api_uses_one_locked_identity_query_without_count_race():
    src = (ROOT / "api" / "wallet_api.py").read_text(encoding="utf-8")
    start = src.index("def _lock_legacy_template_sku")
    end = src.index("def _assert_legacy_retail_snapshot_payable", start)
    resolver = src[start:end]
    assert "COUNT(" not in resolver, "禁止恢复 COUNT→SELECT 竞态"
    assert "o.sku_template_id=%s" in resolver
    assert "source_template_id=%s" not in resolver, "预填来源不得参与历史 SKU 身份"
    assert "o.is_active=TRUE AND o.deleted_at IS NULL" in resolver
    assert "LIMIT 2" in resolver and "FOR UPDATE OF o" in resolver
    assert "if len(rows) >= 2:" in resolver and "if not rows:" in resolver


def test_wallet_api_leftjoin_filters_is_active():
    src = (ROOT / "api" / "wallet_api.py").read_text(encoding="utf-8")
    assert "AND o.is_active = TRUE" in src, "wallet_api LEFT JOIN 漏 o.is_active → 误命中软删行报已下架"
    assert "AND o.deleted_at IS NULL" in src, "wallet_api LEFT JOIN 漏 deleted_at → tombstone 仍可取价"


def test_agent_pricing_get_sku_filters_is_active():
    src = (ROOT / "services" / "agent_pricing.py").read_text(encoding="utf-8")
    assert "AND o.is_active = TRUE" in src, "get_sku_for_customer LEFT JOIN 漏 o.is_active → 随机取软删价"
