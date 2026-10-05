from services.organization_schema_contract import (
    ACCEPTED_CATALOG_VARIANTS,
    EXPECTED_COUNTS,
    EXPECTED_FINGERPRINT,
    FRESH_PAYER_POLICIES_COUNTS,
    FRESH_PAYER_POLICIES_FINGERPRINT,
    FRESH_PAYER_USERNAME_COUNTS,
    FRESH_PAYER_USERNAME_FINGERPRINT,
    FRESH_PAYER_USERNAME_SHORT_CODE_COUNTS,
    FRESH_PAYER_USERNAME_SHORT_CODE_FINGERPRINT,
    LEGACY_ALL_ACCOUNTS_COUNTS,
    LEGACY_ALL_ACCOUNTS_FINGERPRINT,
    PRODUCTION_REANCHOR_SHORT_CODE_COUNTS,
    PRODUCTION_REANCHOR_SHORT_CODE_FINGERPRINT,
    PRODUCTION_FIXTURE_PAYER_COUNTS,
    PRODUCTION_FIXTURE_PAYER_FINGERPRINT,
    PRODUCTION_FIXTURE_REANCHOR_COUNTS,
    PRODUCTION_FIXTURE_REANCHOR_FINGERPRINT,
    PRODUCTION_REANCHOR_COUNTS,
    PRODUCTION_REANCHOR_FINGERPRINT,
    PRODUCTION_REANCHOR_PAYER_COUNTS,
    PRODUCTION_REANCHOR_PAYER_FINGERPRINT,
    match_catalog_variant,
)


def test_fresh_and_signed_production_catalogs_are_exactly_accepted():
    # 2026-07-28 轮换:团队短代码三条 DDL 补登记进 migration_manifest 后,
    # 跑完迁移链的新库必然带上 2 列 + 1 索引,fresh 参考形态前移到 536/384/105。
    assert (
        match_catalog_variant(EXPECTED_COUNTS, EXPECTED_FINGERPRINT)
        == "fresh_pg16_payer_policies_short_code_v1"
    )
    assert (
        match_catalog_variant(
            FRESH_PAYER_POLICIES_COUNTS,
            FRESH_PAYER_POLICIES_FINGERPRINT,
        )
        == "fresh_pg16_payer_policies_v1"
    )
    assert (
        match_catalog_variant(
            PRODUCTION_FIXTURE_PAYER_COUNTS,
            PRODUCTION_FIXTURE_PAYER_FINGERPRINT,
        )
        == "production_fixture_reanchor_payer_policies_v1"
    )
    assert (
        match_catalog_variant(
            LEGACY_ALL_ACCOUNTS_COUNTS,
            LEGACY_ALL_ACCOUNTS_FINGERPRINT,
        )
        == "fresh_pg16_all_accounts_v3"
    )
    assert (
        match_catalog_variant(
            PRODUCTION_FIXTURE_REANCHOR_COUNTS,
            PRODUCTION_FIXTURE_REANCHOR_FINGERPRINT,
        )
        == "production_fixture_reanchor_all_accounts_v3"
    )
    assert (
        match_catalog_variant(
            PRODUCTION_REANCHOR_COUNTS,
            PRODUCTION_REANCHOR_FINGERPRINT,
        )
        == "production_reanchor_all_accounts_v3"
    )
    assert (
        match_catalog_variant(
            PRODUCTION_REANCHOR_PAYER_COUNTS,
            PRODUCTION_REANCHOR_PAYER_FINGERPRINT,
        )
        == "production_reanchor_payer_policies_v1"
    )


def test_catalog_variant_matching_rejects_near_misses():
    wrong_counts = dict(PRODUCTION_REANCHOR_COUNTS, columns=416)
    assert match_catalog_variant(wrong_counts, PRODUCTION_REANCHOR_FINGERPRINT) is None
    assert match_catalog_variant(PRODUCTION_REANCHOR_COUNTS, EXPECTED_FINGERPRINT) is None
    assert match_catalog_variant(EXPECTED_COUNTS, "0" * 64) is None
    wrong_fixture_counts = dict(PRODUCTION_FIXTURE_REANCHOR_COUNTS, indexes=98)
    assert (
        match_catalog_variant(
            wrong_fixture_counts,
            PRODUCTION_FIXTURE_REANCHOR_FINGERPRINT,
        )
        is None
    )
    legacy_wrong_counts = dict(LEGACY_ALL_ACCOUNTS_COUNTS, constraints=366)
    assert match_catalog_variant(legacy_wrong_counts, LEGACY_ALL_ACCOUNTS_FINGERPRINT) is None
    production_payer_wrong_counts = dict(PRODUCTION_REANCHOR_PAYER_COUNTS, columns=534)
    assert (
        match_catalog_variant(
            production_payer_wrong_counts,
            PRODUCTION_REANCHOR_PAYER_FINGERPRINT,
        )
        is None
    )
    assert (
        match_catalog_variant(
            PRODUCTION_REANCHOR_PAYER_COUNTS,
            PRODUCTION_REANCHOR_PAYER_FINGERPRINT[:-1] + "0",
        )
        is None
    )


# ---------------------------------------------------------------------------
# [自愈式 DDL 治理 2026-07-28] 短代码形态的签名/轮换自验
#
# 这一组存在的理由:签一个新变体和"放宽这道门"在代码上长得几乎一样,区别只在于
# 邻近的错误形态还拒不拒绝。四类变异(指纹对但 counts 错 / counts 对但指纹错 /
# 多一索引 / 少一列)必须全部仍被拒绝,漏放任何一类 = 把签名写成了放宽。
# ---------------------------------------------------------------------------

#: 本次轮换/新增涉及的三个 536-537 形态,逐个过同一套变异。
_SHORT_CODE_SHAPES = (
    ("fresh_pg16_payer_policies_short_code_v1", EXPECTED_COUNTS, EXPECTED_FINGERPRINT),
    (
        "fresh_pg16_payer_policies_username_short_code_v1",
        FRESH_PAYER_USERNAME_SHORT_CODE_COUNTS,
        FRESH_PAYER_USERNAME_SHORT_CODE_FINGERPRINT,
    ),
    (
        "production_reanchor_short_code_v1",
        PRODUCTION_REANCHOR_SHORT_CODE_COUNTS,
        PRODUCTION_REANCHOR_SHORT_CODE_FINGERPRINT,
    ),
)


def test_short_code_shapes_match_their_own_variant_exactly():
    for name, counts, fingerprint in _SHORT_CODE_SHAPES:
        assert match_catalog_variant(counts, fingerprint) == name, name


def test_short_code_shapes_reject_all_four_mutation_classes():
    for name, counts, fingerprint in _SHORT_CODE_SHAPES:
        # ① 指纹对,counts 错(少一列)—— 迁移只跑了一半的典型形态
        assert match_catalog_variant(
            dict(counts, columns=counts["columns"] - 1), fingerprint
        ) is None, f"{name}: 少一列被放行"

        # ② 指纹对,counts 错(多一索引)—— 又一段自愈 DDL 偷偷建了索引
        assert match_catalog_variant(
            dict(counts, indexes=counts["indexes"] + 1), fingerprint
        ) is None, f"{name}: 多一索引被放行"

        # ③ counts 对,指纹错 —— 对象数没变但定义/顺序/谓词漂了
        assert match_catalog_variant(counts, "0" * 64) is None, f"{name}: 指纹全错被放行"
        assert match_catalog_variant(
            counts, fingerprint[:-1] + ("0" if fingerprint[-1] != "0" else "1")
        ) is None, f"{name}: 指纹差一位被放行"

        # ④ 约束数漂移 —— CHECK 被删/加
        assert match_catalog_variant(
            dict(counts, constraints=counts["constraints"] - 1), fingerprint
        ) is None, f"{name}: 少一约束被放行"


def test_short_code_rotation_did_not_widen_the_gate():
    # 轮换前的 fresh 形态(534/384/104)与轮换后(536/384/105)必须是**两个**形态,
    # 不能出现"新指纹配旧 counts"或"旧指纹配新 counts"被认成同一个的情况。
    assert EXPECTED_COUNTS != FRESH_PAYER_POLICIES_COUNTS
    assert EXPECTED_FINGERPRINT != FRESH_PAYER_POLICIES_FINGERPRINT
    assert match_catalog_variant(FRESH_PAYER_POLICIES_COUNTS, EXPECTED_FINGERPRINT) is None
    assert match_catalog_variant(EXPECTED_COUNTS, FRESH_PAYER_POLICIES_FINGERPRINT) is None

    # 短代码是 +2 列 +1 索引、约束不变(纯 additive,没动任何 CHECK/FK)
    assert EXPECTED_COUNTS["columns"] == FRESH_PAYER_POLICIES_COUNTS["columns"] + 2
    assert EXPECTED_COUNTS["indexes"] == FRESH_PAYER_POLICIES_COUNTS["indexes"] + 1
    assert EXPECTED_COUNTS["constraints"] == FRESH_PAYER_POLICIES_COUNTS["constraints"]


def test_username_axis_stayed_pinned_through_the_rotation():
    # 🔴 轮换前 FRESH_PAYER_USERNAME_COUNTS 写的是 `= EXPECTED_COUNTS` 别名。
    # 若保留别名,EXPECTED_COUNTS 一轮换就会把这个 534 时期的变体静默改成
    # "536 counts + 534 指纹"——一个永远匹配不上的死变体,而且没有任何测试会报红。
    assert FRESH_PAYER_USERNAME_COUNTS == {"columns": 534, "constraints": 384, "indexes": 104}
    assert (
        match_catalog_variant(FRESH_PAYER_USERNAME_COUNTS, FRESH_PAYER_USERNAME_FINGERPRINT)
        == "fresh_pg16_payer_policies_username_v1"
    )


def test_username_and_base_short_code_shapes_are_distinct_fingerprints():
    # 两者 counts 完全相同(536/384/105),只差两条 target_kind CHECK 定义 →
    # 必须靠指纹区分。若指纹相同,说明其中一个签错了形态。
    assert FRESH_PAYER_USERNAME_SHORT_CODE_COUNTS == EXPECTED_COUNTS
    assert FRESH_PAYER_USERNAME_SHORT_CODE_FINGERPRINT != EXPECTED_FINGERPRINT


def test_every_accepted_variant_has_a_unique_fingerprint():
    fingerprints = [fingerprint for _, fingerprint in ACCEPTED_CATALOG_VARIANTS.values()]
    assert len(fingerprints) == len(set(fingerprints)), "变体共用指纹会让漂移蒙混过关"


def test_production_short_code_variant_is_untouched_by_this_package():
    # 红线:production_reanchor_short_code_v1 是按三闸签好的当前生产形态,
    # 本包不得改动它——改了等于把刚解开的容器启动门重新锁上。
    assert PRODUCTION_REANCHOR_SHORT_CODE_COUNTS == {
        "columns": 537, "constraints": 384, "indexes": 105,
    }
    assert PRODUCTION_REANCHOR_SHORT_CODE_FINGERPRINT == (
        "bacbb6077597fc440a71420730d6200a6036413008033fe1dfb257ea4c28ff5f"
    )
