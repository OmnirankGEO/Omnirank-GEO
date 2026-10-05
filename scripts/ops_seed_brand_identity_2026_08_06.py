"""品牌实体档案落档脚本(WO_DELIVERY_FLYWHEEL_CLOSURE §2.2)· 默认 dry-run。

用途:把「一个真实公司散落在多个 brand_id」这件事登记成档案 + 映射,让归因、监测、
发布三张表能按同一个实体键聚合。

🔴 本脚本**只写两张新表**(`brand_identity_profiles` / `brand_identity_members`):
   - 不改 `brands` 任何一列(不写 parent_brand_id、不动 owner_user_id、不软删任何行);
   - 不改 quotes / articles / monitoring_* / mhz_* 任何一行;
   - 跨用户的商业归属变更**不在本脚本范围**(Owner 2026-08-06 裁定:先建档案+映射,不动归属)。
   回滚 = 删这两张表的对应行,归因自动退回按 brand_id 严格相等。

🔴 默认 `--dry-run`:打印将写入什么,不落库。真正写入要显式 `--apply`。

用法:
    python scripts/ops_seed_brand_identity_2026_08_06.py            # dry-run
    python scripts/ops_seed_brand_identity_2026_08_06.py --apply    # 落库

⚠️ 实测提醒(2026-08-06 生产只读验证):**实体归并对当前归因行数的增益是 0**
   (模拟归并前后都是 2 条)。真正卡住三家的是发布侧缺正文快照与历史监测行血缘缺失,
   不是品牌错配。登记档案的价值在于**防止未来错配**、以及让跨 brand_id 的报表能聚合 ——
   不要把它当成闭环没水的解药,那是两回事。
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

# 三家客户的实体档案。数据来源:2026-08-06 生产 brands 表只读快照。
# 新增客户往这里加;每个实体必须且只能有一个 primary。
SEED: list[dict[str, Any]] = [
    {
        "identity_key": "qzqz-shenzhen",
        "canonical_name": "QZQZ 美学定制",
        "short_name": "QZQZ",
        "historical_names": ["QZQZ 美学定制", "QZQZ木作美学定制"],
        "alias_whitelist": ["QZQZ", "QZQZ木作", "QZQZ美学定制", "QZQZ木作美学定制"],
        "region": "广东省深圳市",
        "business": "高端整木全屋定制 / 木作高定(住宅室内木作整装、实木定制家具设计生产安装)",
        "official_site": None,
        "members": [
            # 662 = 当前发布挂靠处(31 条已发布),定为 primary
            {"brand_id": 662, "member_role": "primary",
             "note": "当前发布挂靠(31 条 published);2026-07-15 建"},
            # 10 = 监测历史挂靠处(144 任务 / 7312 结果),但 0 发布
            {"brand_id": 10, "member_role": "duplicate",
             "note": "监测历史挂靠(144 任务 / 7312 结果 / 0 发布);2026-03-01 建"},
            {"brand_id": 428, "member_role": "duplicate",
             "note": "无发布无监测;2026-05-11 建"},
        ],
    },
    {
        "identity_key": "qishe-shenzhen",
        "canonical_name": "深圳市栖舍设计装饰有限公司",
        "short_name": "栖舍",
        "historical_names": ["深圳栖舍设计装修有限公司", "深圳市栖舍设计装饰有限公司"],
        "alias_whitelist": ["栖舍", "栖舍设计", "栖舍装饰", "栖舍装修"],
        "region": "广东省深圳市",
        "business": "建筑装饰 / 住宅室内设计(家装为主)",
        "official_site": None,
        "members": [
            {"brand_id": 615, "member_role": "primary",
             "note": "当前发布挂靠(53 条 published);2026-06-22 建"},
            {"brand_id": 586, "member_role": "duplicate",
             "note": "无发布无监测;2026-06-09 建"},
        ],
    },
    {
        "identity_key": "fuji-elevator-shenzhen",
        "canonical_name": "深圳市晨光富士电梯",
        "short_name": "晨光富士",
        "historical_names": ["深圳市晨光富士电梯"],
        "alias_whitelist": ["晨光富士", "晨光富士电梯", "富士电梯"],
        "region": "广东省深圳市龙华区",
        "business": "电梯制造、销售、安装维保",
        "official_site": None,
        "members": [
            {"brand_id": 289, "member_role": "primary",
             "note": "发布(59)与监测(102 任务)都在这里;2026-05-03 建"},
            {"brand_id": 474, "member_role": "duplicate",
             "note": "无发布无监测;2026-05-23 建"},
        ],
    },
]


def _validate() -> None:
    """落库前的自检 —— 与 DB 约束重复是**有意的**:脚本要在连库之前就报错。"""
    seen_keys: set[str] = set()
    seen_brands: set[int] = set()
    for entity in SEED:
        key = entity["identity_key"]
        assert key and key not in seen_keys, f"identity_key 重复: {key}"
        seen_keys.add(key)
        primaries = [m for m in entity["members"] if m["member_role"] == "primary"]
        assert len(primaries) == 1, f"{key} 必须且只能有 1 个 primary,实际 {len(primaries)}"
        for member in entity["members"]:
            bid = int(member["brand_id"])
            assert bid not in seen_brands, f"brand_id {bid} 被登记到多个实体"
            seen_brands.add(bid)
            assert member["member_role"] in ("primary", "duplicate")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正落库(默认只 dry-run)")
    args = parser.parse_args()

    _validate()

    print("=" * 74)
    print(f"品牌实体落档 · {'APPLY(会写库)' if args.apply else 'DRY-RUN(不写库)'}")
    print("=" * 74)
    for entity in SEED:
        print(f"\n实体 {entity['identity_key']} —— {entity['canonical_name']}")
        for member in entity["members"]:
            print(f"    brand_id={member['brand_id']:<5} {member['member_role']:<10} {member['note']}")

    if not args.apply:
        print("\n[dry-run 结束,未写库。确认无误后加 --apply]")
        return 0

    import json

    from db.connection import get_db

    written_profiles = written_members = 0
    with get_db() as conn:
        cur = conn.cursor()
        # brand_id 必须真实存在 —— 否则外键会报错,但先自己查一遍能给出人话原因
        all_ids = [m["brand_id"] for e in SEED for m in e["members"]]
        cur.execute("SELECT id FROM brands WHERE id = ANY(%s)", (all_ids,))
        existing = {int(dict(r)["id"]) for r in cur.fetchall()}
        missing = sorted(set(all_ids) - existing)
        if missing:
            print(f"\n🔴 以下 brand_id 在 brands 表里不存在,中止:{missing}")
            return 2

        for entity in SEED:
            cur.execute(
                """
                INSERT INTO brand_identity_profiles
                    (identity_key, canonical_name, short_name, historical_names,
                     alias_whitelist, region, business, official_site)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                ON CONFLICT (identity_key) DO UPDATE SET
                    canonical_name = EXCLUDED.canonical_name,
                    short_name     = EXCLUDED.short_name,
                    historical_names = EXCLUDED.historical_names,
                    alias_whitelist  = EXCLUDED.alias_whitelist,
                    region   = EXCLUDED.region,
                    business = EXCLUDED.business,
                    official_site = EXCLUDED.official_site,
                    updated_at = NOW()
                """,
                (entity["identity_key"], entity["canonical_name"], entity["short_name"],
                 json.dumps(entity["historical_names"], ensure_ascii=False),
                 json.dumps(entity["alias_whitelist"], ensure_ascii=False),
                 entity["region"], entity["business"], entity["official_site"]),
            )
            written_profiles += 1
            for member in entity["members"]:
                cur.execute(
                    """
                    INSERT INTO brand_identity_members
                        (brand_id, identity_key, member_role, note)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (brand_id) DO UPDATE SET
                        identity_key = EXCLUDED.identity_key,
                        member_role  = EXCLUDED.member_role,
                        note         = EXCLUDED.note,
                        updated_at   = NOW()
                    """,
                    (member["brand_id"], entity["identity_key"],
                     member["member_role"], member["note"]),
                )
                written_members += 1

    print(f"\n✅ 落库完成:实体档案 {written_profiles} 条 · 映射 {written_members} 条")
    print("   验收:SELECT identity_key, COUNT(*) FROM brand_identity_members GROUP BY 1;")
    return 0


if __name__ == "__main__":
    sys.exit(main())
