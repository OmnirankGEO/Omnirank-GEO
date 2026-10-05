"""[WO-ACCEPTANCE-3FIX-2026-08-05 项2] 标题双地名 + 词尾重复 · 变异自检。

三关缺一即判无效变异:锚点唯一命中(命中 != 1 记 **SKIP** 不记 SURVIVED)、
落盘后文件真变了、至少一个预期用例转红。

变异清单必须覆盖两个方向:
  · **退回本 bug**(不去重)—— 证明锁抓得到事故;
  · **矫枉过正**(该加的地名不加 / 正常重复也删)—— 证明锁不是一味求"少一个字"。
只有前一半的清单会纵容一个"永远返回空前缀"的实现通过。

跑法: PYTHONIOENCODING=utf-8 TEST_DATABASE_URL=... python tests/mutation_acc3fix_title_hygiene.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

HYG = "services/geo_title_hygiene.py"
TE = "services/geo_douyin/title_engine.py"
IP = "services/geo_douyin/image_pipeline.py"
TD = "writing/topic_dispatcher.py"

PY_TESTS = "tests/test_acc3fix_title_hygiene_2026_08_05.py"

MUTATIONS: list[tuple[str, str, str, str, str, list[str]]] = [
    # ---------------- 方向一:退回本 bug(不去重) ----------------
    ("B1", "🔴 地名去重失效(退回 post 14 的形态:城市无条件叠加)",
     HYG,
     '    if c in kw:\n        return ""\n',
     '    if False:\n        return ""\n',
     # 🔴 这条**不能**指望深圳那几条用例:token 命中会兜住它(两条路径重叠)。
     #    能且只能由单字简称那条抓 —— 这也正好证明整串命中不是冗余判据。
     ["test_literal_city_hit_covers_untokenizable_values__must_hit"]),

    ("B2", "🔴 token 级去重失效(整串相等才算命中 —— 诊断 531 那轮的老判据)",
     HYG,
     '    for token in geo_tokens(c):\n        if token and token in kw:\n            return ""\n',
     '    for token in ():\n        if token and token in kw:\n            return ""\n',
     ["test_city_already_in_keyword_is_not_prefixed_again__must_hit"]),

    ("B3", "🔴 词尾去重失效(退回「哪家好…哪家好」)",
     HYG,
     '    for tail in QUESTION_TAILS:\n        if kw.endswith(tail):\n            trimmed = kw[: -len(tail)]\n',
     '    for tail in ():\n        if kw.endswith(tail):\n            trimmed = kw[: -len(tail)]\n',
     ["test_duplicate_question_tail_removed__must_hit",
      "test_production_accident_title_has_city_once__must_hit"]),

    ("B4", "🔴 build_title 绕过共用件,改回裸 format(事故本体那一行)",
     TE,
     '    raw = compose_title(tpl.pattern, city=city, keyword=kw, n=n)\n',
     '    raw = tpl.pattern.format(city=_norm(city) if city else "", kw=kw, n=n)\n',
     ["test_production_accident_title_has_city_once__must_hit",
      "test_routed_sites_actually_call_it__must_hit"]),

    ("B5", "🔴 build_city_matrix 绕过共用件",
     TE,
     '        raw = compose_title(tpl.pattern, city=c, keyword=keyword, n=n)\n',
     '        raw = tpl.pattern.format(city=c, kw=_norm(keyword), n=n)\n',
     ["test_matrix_cell_matching_keyword_city_not_doubled__must_hit",
      "test_routed_sites_actually_call_it__must_hit"]),

    ("B6", "🔴 build_hashtags 的 base 改回裸拼(标签里两个深圳)",
     TE,
     '    base = join_city_keyword(city, kw) if city else kw\n',
     '    base = f"{_norm(city)}{kw}" if city else kw\n',
     ["test_hashtags_have_no_doubled_city_or_tail__must_hit",
      "test_routed_sites_actually_call_it__must_hit"]),

    ("B7", "🔴 生图 prompt 的主题词改回裸拼(「主题：深圳深圳全屋定制…」)",
     IP,
     '    locale = join_city_keyword(city, keyword)\n',
     '    locale = f"{city}{keyword}".strip()\n',
     ["test_image_prompt_locale_not_doubled__must_hit",
      "test_routed_sites_actually_call_it__must_hit"]),

    ("B8", "🔴 writing 侧对比稿标题改回裸 f-string(第 5 个调用点脱钩)",
     TD,
     '                "title": _compose_geo_title(\n'
     '                    "{year}{city}{kw}怎么选？证据核验与避坑清单",\n'
     '                    city=region, keyword=industry, year=year),\n',
     '                "title": f"{year}{region}{industry}怎么选？证据核验与避坑清单",\n',
     ["test_routed_sites_actually_call_it__must_hit"]),

    # ---------------- 方向二:矫枉过正(该加的不加 / 正常重复也删) ----------------
    ("B9", "🔴 一刀切:城市前缀永远不加(从一个错走到另一个错 · 工单 §2.5 第 3 行)",
     HYG,
     '    c = _norm(city)\n    if not c:\n        return ""\n',
     '    c = _norm(city)\n    return ""\n    if not c:\n        return ""\n',
     ["test_city_absent_from_keyword_is_still_prefixed__must_not_hit",
      "test_control_group_title_unchanged__must_not_hit",
      "test_keyword_without_city_keeps_city_in_matrix__must_hit",
      "test_hashtags_still_carry_city_when_absent__must_not_hit"]),

    ("B10", "🔴 粗暴归一:不看模板后面是什么,一律砍掉关键词尾巴",
     HYG,
     '    if not any(nxt.startswith(t) for t in QUESTION_TAILS):\n        return kw\n',
     '    if False:\n        return kw\n',
     ["test_tail_kept_when_template_is_not_a_question_tail__must_not_hit"]),

    ("B11", "🔴 白名单塞进通用词(「好用」→「越用越好用」这种正常重复会被误伤)",
     HYG,
     '    "推荐", "排名", "排行榜", "十大", "前十名",\n',
     '    "推荐", "排名", "排行榜", "十大", "前十名", "好用",\n',
     ["test_tail_whitelist_has_no_generic_words__must_not_hit",
      "test_normal_repetition_in_body_untouched__must_not_hit"]),

    ("B12", "整条关键词是后缀时被剪成空串(标题里没有品类词了)",
     HYG,
     '            return trimmed or kw\n',
     '            return trimmed\n',
     ["test_tail_only_keyword_not_emptied__must_not_hit"]),

    # ---------------- 方向三:锁本身失效 ----------------
    ("B13", "🔴 绕过扫描的正则被打瘸(全仓一处都扫不到 = 「全部已登记」恒真)",
     PY_TESTS,
     '    r"\\{\\s*(?:city|town|region|地域)\\s*\\}\\s*"\n',
     '    r"\\{\\s*(?:__never_matches__)\\s*\\}\\s*"\n',
     ["test_scan_is_not_empty__must_hit",
      "test_scan_catches_a_brand_new_bypassing_file__must_hit"]),

    ("B14", "共用件的 geo_tokens 依赖被换成空实现(契约钉不住 = 静默退化)",
     HYG,
     'from services.diagnosis_question_quality import geo_tokens\n',
     'def geo_tokens(v):\n    return ()\n',
     ["test_city_already_in_keyword_is_not_prefixed_again__must_hit"]),
]


def read_src(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write_src(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def run_locks() -> tuple[int, str]:
    env = dict(os.environ)
    env.setdefault("TEST_DATABASE_URL",
                   "postgresql://geo_test:geo_test@127.0.0.1:5432/test_geo_agentscope")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(
        [sys.executable, "-m", "pytest", PY_TESTS, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    return (1 if p.returncode else 0), (p.stdout or "") + (p.stderr or "")


def red_markers(output: str) -> set[str]:
    marks: set[str] = set()
    for line in output.splitlines():
        if line.startswith(("FAILED ", "ERROR ")) and "::" in line:
            marks.add(line.split("::")[-1].split()[0].split("[")[0])
    return marks


def main() -> int:
    rc, out = run_locks()
    if rc != 0:
        print("基线不绿,先修基线:\n" + out[-3000:])
        return 2
    print("基线 GREEN(反向对照:未变异的树必须全绿)")
    print("=" * 72)

    killed, survived, skipped = 0, [], []
    for mid, desc, rel, old, new, expect in MUTATIONS:
        path = REPO / rel
        original = read_src(path)
        hits = original.count(old)
        if hits != 1:
            print(f"[{mid}] ⏭️ SKIP · 锚点命中 {hits} 次(需恰好 1 次): {desc}")
            skipped.append((mid, desc, f"锚点命中 {hits} 次"))
            continue
        mutated = original.replace(old, new, 1)
        if mutated == original:
            print(f"[{mid}] ⏭️ SKIP · 落盘后文件没变(no-op): {desc}")
            skipped.append((mid, desc, "文件未改变"))
            continue

        write_src(path, mutated)
        try:
            m_rc, m_out = run_locks()
        finally:
            write_src(path, original)
        assert read_src(path) == original, f"[{mid}] 还原失败,树被污染了"

        reds = red_markers(m_out)
        hit = sorted(reds & set(expect))
        if m_rc == 0:
            print(f"[{mid}] ❌ 存活(零转红): {desc}")
            survived.append((mid, desc, "零转红"))
        elif not hit:
            print(f"[{mid}] ⚠️ 转红但不是预期那些: {desc}\n"
                  f"        预期 {expect} / 实际 {sorted(reds)}")
            survived.append((mid, desc, f"红的是 {sorted(reds)}"))
        else:
            killed += 1
            print(f"[{mid}] ✅ 被杀 (含预期 {hit}): {desc}")

    print("=" * 72)
    print(f"变异结果: {killed} 杀 / {len(survived)} 存活 / {len(skipped)} 跳过 "
          f"(共 {len(MUTATIONS)})")
    for tag, rows in (("存活", survived), ("跳过(变异没打上,不算证据)", skipped)):
        if rows:
            print(f"{tag}:")
            for mid, desc, why in rows:
                print(f"  - {mid} [{why}] {desc}")
    return 1 if (survived or skipped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
