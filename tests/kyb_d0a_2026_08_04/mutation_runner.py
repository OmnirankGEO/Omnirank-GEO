"""[WO-KYB-ROUTING D0-a] 变异自检。

对每个变异校三关(缺一关就是自欺):
  1. 锚点在文件里【恰好命中 1 次】—— 命中 0 次是没改到,命中多次是改错了地方
  2. 写回后文件字节【真的变了】
  3. 期望的那几个用例【真的转红】—— 一个都没红 = 这条防线没被任何断言盖住

🔴 用 read_bytes/write_bytes,不用 read_text/write_text ——
   Windows 上 text 模式每跑一次就把 LF 翻成 CRLF,整包行尾翻转过一次。
🔴 变异跑期间禁止任何 git 写操作(stash/checkout),跑完必须 git diff 逐段复核 ——
   曾经有一个变异被烤进交付代码。
"""
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TESTS = os.path.join("tests", "kyb_d0a_2026_08_04")
DB = os.path.join("db", "meijiehezi_db.py")

ENV = {
    **os.environ,
    "TEST_DATABASE_URL": "postgresql://u:p@127.0.0.1:5432/geo_test",
    "PYTHONIOENCODING": "utf-8",
}

MUTATIONS = [
    {
        "id": "M1",
        "desc": "把 provider 塞回软文白名单(等于泄漏原样复现)",
        "edits": [(
            'MEDIA_PUBLIC_COLUMNS = (\n    "id", "media_name",',
            'MEDIA_PUBLIC_COLUMNS = (\n    "provider",\n    "id", "media_name",',
        )],
        # 注意:test_response_keys_equal_whitelist 在这条变异下【不会】红,
        # 因为它拿"响应键"和"白名单常量"比,而变异改的正是那个常量 —— 两边一起变。
        # 这不是漏洞,是分层:白名单自身写错由下面两条结构断言兜。
        "expect_red": [
            "test_internal_columns_absent_from_response_and_sql",
            "test_whitelists_disjoint_from_internal_set",
        ],
    },
    {
        "id": "M2",
        "desc": "白名单不跟库内真实列求交(库里没有的列照选 → 全新库 500)",
        "edits": [(
            "        cached = tuple(col for col in declared if col in actual)",
            "        cached = tuple(declared)",
        )],
        "expect_red": ["test_missing_declared_column_is_tolerated"],
    },
    {
        "id": "M3",
        "desc": "交集为空时 fail-open 退回 SELECT *(把漏洞放回来)",
        "edits": [(
            "        if not cached:\n            raise RuntimeError(",
            "        if not cached:\n            return \"*\"\n        if False:\n            raise RuntimeError(",
        )],
        "expect_red": ["test_empty_intersection_raises_instead_of_select_star"],
    },
    {
        "id": "M4",
        "desc": "_public_select_list 直接返回 *(整个白名单形同虚设)",
        "edits": [(
            '    return ", ".join(f\'"{col}"\' for col in cached)',
            '    return "*"',
        )],
        "expect_red": [
            "test_response_keys_equal_whitelist",
            "test_future_column_does_not_leak",
            "test_internal_columns_absent_from_response_and_sql",
        ],
    },
    {
        "id": "M5",
        "desc": "内部列集漏登记 hidden_by_dedupe",
        "edits": [(
            '    "hidden_by_dedupe",     # 双供应商去重内部态\n',
            "",
        )],
        # test_column_accounting_is_complete 用的是【用例自己声明】的 per-table 内部集,
        # 不读模块常量,所以这条变异动不到它 —— 期望里不列。
        "expect_red": ["test_per_table_internal_set_is_covered"],
    },
    {
        "id": "M6",
        "desc": "自媒体查询误用软文白名单(跨表接错线)",
        "edits": [(
            '_public_select_list(c, "mhz_wemedia", WEMEDIA_PUBLIC_COLUMNS)',
            '_public_select_list(c, "mhz_wemedia", MEDIA_PUBLIC_COLUMNS)',
        )],
        "expect_red": [
            "test_response_keys_equal_whitelist",
            "test_column_cache_is_keyed_per_table",
        ],
    },
    {
        "id": "M7",
        "desc": "短视频接口退回 SELECT *(工单没点名的那个出口)",
        "edits": [(
            '            f"SELECT {_cols} FROM mhz_short_video WHERE {where_sql}',
            '            f"SELECT * FROM mhz_short_video WHERE {where_sql}',
        )],
        "expect_red": [
            "test_response_keys_equal_whitelist",
            "test_internal_columns_absent_from_response_and_sql",
        ],
    },
    {
        "id": "M8",
        "desc": "列缓存不按表分键(软文的交集被自媒体复用)",
        "edits": [
            ("    cached = _PUBLIC_COLUMN_CACHE.get(table)",
             '    cached = _PUBLIC_COLUMN_CACHE.get("_shared_")'),
            ("        _PUBLIC_COLUMN_CACHE[table] = cached",
             '        _PUBLIC_COLUMN_CACHE["_shared_"] = cached'),
        ],
        "expect_red": ["test_column_cache_is_keyed_per_table"],
    },
    {
        "id": "M9",
        "desc": "白名单里写错一个列名(typo 锁)",
        "edits": [(
            '"weekend_publish", "authority_media", "special_industry", "is_active", "synced_at",',
            '"weekend_publsh", "authority_media", "special_industry", "is_active", "synced_at",',
        )],
        "expect_red": [
            "test_whitelist_is_subset_of_repo_ddl",
            "test_response_keys_equal_whitelist",
            "test_column_accounting_is_complete",
        ],
    },
    {
        "id": "M10",
        "desc": "软文主查询退回 SELECT *",
        "edits": [(
            '            f"SELECT {_cols} FROM mhz_media WHERE {where_sql}',
            '            f"SELECT * FROM mhz_media WHERE {where_sql}',
        )],
        "expect_red": [
            "test_response_keys_equal_whitelist",
            "test_future_column_does_not_leak",
        ],
    },
]


def run_tests():
    p = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", TESTS, "-q", "--tb=no", "-rf"],
        cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )
    failed = set(re.findall(r"FAILED\s+\S+::([\w]+)", p.stdout))
    return p.returncode, failed, p.stdout


def main():
    path = os.path.join(ROOT, DB)
    original = open(path, "rb").read()

    rc, failed, out = run_tests()
    if rc != 0:
        print("🔴 基线就不是绿的,变异结果无意义\n" + out[-2500:])
        return 1
    print(f"基线 ✅ 全绿({DB} {len(original)} 字节)\n")

    results = []
    for mut in MUTATIONS:
        src = original.decode("utf-8")
        ok_anchor = True
        for anchor, repl in mut["edits"]:
            n = src.count(anchor)
            if n != 1:
                print(f"🔴 {mut['id']} 锚点命中 {n} 次(必须恰好 1 次),跳过")
                ok_anchor = False
                break
            src = src.replace(anchor, repl, 1)
        if not ok_anchor:
            results.append((mut["id"], "锚点无效", set()))
            continue

        mutated = src.encode("utf-8")
        if mutated == original:
            print(f"🔴 {mut['id']} 写回后文件没变 → 是个 no-op 变异")
            results.append((mut["id"], "文件没变", set()))
            continue

        open(path, "wb").write(mutated)
        try:
            _rc, mfailed, _out = run_tests()
        finally:
            open(path, "wb").write(original)

        # 🔴 严格:期望的每一条都必须红。只要"至少一条红"就放行,
        #    期望里那些永远不会红的条目会一直躺着,真正的杀手哪天没了也看不出来。
        want = set(mut["expect_red"])
        missing = want - mfailed
        if not missing:
            status = f"✅ 转红 {sorted(want)}"
        elif mfailed:
            status = f"🔴 期望未全红!没红={sorted(missing)} 实际红={sorted(mfailed)}"
        else:
            status = "🔴 一条都没红 —— 这条防线没被任何断言盖住"
        print(f"{mut['id']} {status}  —— {mut['desc']}")
        results.append((mut["id"], status, mfailed))

    # 收尾:确认文件已还原、且基线仍绿
    assert open(path, "rb").read() == original, "🔴 文件没还原干净"
    rc2, _f2, out2 = run_tests()
    bad = [r for r in results if not r[1].startswith("✅")]
    print(f"\n还原后基线:{'✅ 绿' if rc2 == 0 else '🔴 红'}")
    print(f"变异 {len(results) - len(bad)}/{len(results)} 被杀")
    if bad:
        print("未被杀:", [r[0] for r in bad])
    return 0 if (not bad and rc2 == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
