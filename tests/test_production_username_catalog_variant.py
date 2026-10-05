"""[WP6/NEEDS_PROD-1]生产形态 + 用户名式邀请迁移的 catalog 变体签名判别。

签名来源(受信三闸,见 organization_schema_contract 常量注释):
  闸1 只读导出的生产 catalog 重算指纹 == 已签 production_reanchor_payer_policies_v1;
  闸2 精确 delta 只有两条 target_kind CHECK 定义放宽;
  闸3 counts 不变(535/384/104)。

本判别锁住签名结果与 fail-closed 语义:签名只认这一个形态,任何漂移(counts 变、
指纹变、定义没放宽/放过头)都必须匹配不上。
"""
from services.organization_schema_contract import (
    ACCEPTED_CATALOG_VARIANTS,
    PRODUCTION_REANCHOR_PAYER_COUNTS,
    PRODUCTION_REANCHOR_PAYER_FINGERPRINT,
    PRODUCTION_REANCHOR_PAYER_USERNAME_COUNTS,
    PRODUCTION_REANCHOR_PAYER_USERNAME_FINGERPRINT,
    match_catalog_variant,
)


def test_production_username_variant_is_registered_and_matchable():
    assert "production_reanchor_payer_username_v1" in ACCEPTED_CATALOG_VARIANTS
    assert match_catalog_variant(
        PRODUCTION_REANCHOR_PAYER_USERNAME_COUNTS,
        PRODUCTION_REANCHOR_PAYER_USERNAME_FINGERPRINT,
    ) == "production_reanchor_payer_username_v1"


def test_pre_migration_production_shape_still_matches_its_own_variant():
    # 迁移前的生产形态必须仍然可匹配(灰度/回滚窗口内两态都得认)
    assert match_catalog_variant(
        PRODUCTION_REANCHOR_PAYER_COUNTS, PRODUCTION_REANCHOR_PAYER_FINGERPRINT
    ) == "production_reanchor_payer_policies_v1"


def test_username_variant_keeps_the_same_object_counts_as_production():
    # 放宽 CHECK 定义不增删对象:counts 必须与迁移前生产完全一致
    assert PRODUCTION_REANCHOR_PAYER_USERNAME_COUNTS == PRODUCTION_REANCHOR_PAYER_COUNTS
    assert PRODUCTION_REANCHOR_PAYER_USERNAME_COUNTS == {
        "columns": 535, "constraints": 384, "indexes": 104,
    }


def test_username_variant_fingerprint_differs_from_pre_migration():
    # 定义确实变了 → 指纹必须不同(否则说明迁移根本没生效或签错了形态)
    assert (PRODUCTION_REANCHOR_PAYER_USERNAME_FINGERPRINT
            != PRODUCTION_REANCHOR_PAYER_FINGERPRINT)


def test_fail_closed_on_any_drift():
    fp = PRODUCTION_REANCHOR_PAYER_USERNAME_FINGERPRINT
    # 指纹对但 counts 漂移 → 不认
    assert match_catalog_variant({"columns": 536, "constraints": 384, "indexes": 104}, fp) is None
    # counts 对但指纹漂移 → 不认
    assert match_catalog_variant(PRODUCTION_REANCHOR_PAYER_USERNAME_COUNTS, "0" * 64) is None


def test_every_variant_fingerprint_is_unique():
    fps = [f for _, f in ACCEPTED_CATALOG_VARIANTS.values()]
    assert len(fps) == len(set(fps)), "不同变体不得共用指纹(会让漂移蒙混过关)"
