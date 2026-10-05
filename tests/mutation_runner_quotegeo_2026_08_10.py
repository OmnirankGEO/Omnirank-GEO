"""报价关键词双向反转修复 · 变异自检(工单 §6「判别力/变异至少杀死」六条)。

三关缺一即判无效变异:
  1. 锚点唯一命中(命中 0 或 >1 = 变异没打在预期位置);
  2. 落盘后文件真变了;
  3. 至少一条锁转红。零转红 = 锁抓不到这个 bug。

工单点名必须杀死的六条,逐条对应下面的 M1-M6:
  M1 删除「商场/商铺/招商」商业对象支持
  M2 把 `uncertain` 再次改成 `knowledge`
  M3 本地客户无地域词再次默认选中
  M4 跳过业务范围判断
  M5 把外地词当本地词
  M6 删除人工放行能力
M7-M11 是我自己补的:光杀这六条,几处最容易悄悄退化的地方仍然没有守卫。

🔴 文件读写一律走 bytes(Windows 上 read_text/write_text 会翻换行,本仓栽过两次)。
🔴 子进程钉 PYTHONPYCACHEPREFIX + 禁写字节码:被打断时残留的 .pyc 会让下一轮基线假红。

跑法:TEST_DATABASE_URL=... python tests/mutation_runner_quotegeo_2026_08_10.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]

POLICY = "services/commercial_query_policy.py"
DECISION = "services/keyword_delivery_decision.py"
EXPANDER = "tools/keyword_expander.py"

# API 锁要 import server(约 30s)。变异跑 11 轮 → 单独跑一次基线即可,
# 变异轮只跑纯逻辑那三个文件,不然一轮 6 分钟。
T_FAST = "tests/quotegeo_2026_08_10/test_quote_keyword_double_inversion_2026_08_10.py"
T_GEO = "tests/quotegeo_2026_08_10/test_geo_ratio_hint_is_not_a_gate.py"
T_BIZ = "tests/quotegeo_2026_08_10/test_business_axis_decisiveness.py"

_PYC_DIR = REPO / ".mutation_pycache_quotegeo"

# (编号, 说明, 文件, 锚点, 替换)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ── 工单 §6 点名六条 ────────────────────────────────────────────────
    ("M1", "§6①删掉「商场/商铺/招商」商业对象支持 → 退回事故现场:六条精准词全落空",
     POLICY,
     '    "商场", "商超", "超市", "市场",\n'
     '    "商铺", "店铺", "铺位", "旺铺", "档口", "摊位", "店面",\n',
     ""),

    ("M2", "§6②把 uncertain 再次压成 knowledge(R2 的原样复刻)",
     DECISION,
     "    if intent not in (INTENT_COMMERCIAL, INTENT_BRAND_DIRECT):\n"
     "        code, text = _INTENT_REASON.get(intent, _INTENT_REASON[INTENT_UNCERTAIN])\n",
     "    if intent not in (INTENT_COMMERCIAL, INTENT_BRAND_DIRECT):\n"
     "        code, text = _INTENT_REASON[INTENT_KNOWLEDGE]\n"),

    ("M3", "§6③本地客户的无地域全国泛词再次被默认选中(「商场推荐」回到付费交付)",
     DECISION,
     '    return (\n'
     '        GEO_TOO_BROAD,\n'
     '        "该词没有地域限定,面向全国;客户只服务本地市场,默认不计入交付",\n'
     '    )',
     '    return GEO_MATCHED, "该词没有地域限定"'),

    ("M4", "§6④跳过业务范围判断(「商业综合体设计公司」又能靠公司二字入选)",
     DECISION,
     "    scope_verdict, scope_evidence = classify_business_scope(kw, profile, advisory)\n",
     "    scope_verdict, scope_evidence = SCOPE_MATCHED, \"skipped\"\n"),

    ("M5", "§6⑤把外地词当本地词(上海词进深圳客户的交付)",
     DECISION,
     "    if geo._outside(kw):\n"
     "        return GEO_OUTSIDE_MARKET, \"关键词里的地域不在客户服务市场内\"\n",
     "    if False:\n"
     "        return GEO_OUTSIDE_MARKET, \"关键词里的地域不在客户服务市场内\"\n"),

    ("M6", "§6⑥删除人工放行能力(H1 被当成 H0 · 违反 §3.1)",
     DECISION,
     "    override_allowed = not hard_code\n",
     "    override_allowed = False\n"),

    # ── 我自己补的五条 ──────────────────────────────────────────────────
    ("M7", "T1 顺序被打乱:商业组合判定抢在知识题反例之前 → 商场招商流程变商业词",
     POLICY,
     "    if any(re.search(pattern, kw) for pattern in _NO_RECOMMEND_PATTERNS):\n"
     "        return False, \"KNOWLEDGE_PATTERN\"\n\n"
     "    # —— 商业对象 × 商业动作(T1)。必须排在上面全部知识题反例**之后** ——\n"
     "    has_venue = any(noun in kw for noun in _VENUE_NOUNS)\n"
     "    has_trade = any(action in kw for action in _TRADE_ACTIONS)\n",
     "    has_venue = any(noun in kw for noun in _VENUE_NOUNS)\n"
     "    has_trade = any(action in kw for action in _TRADE_ACTIONS)\n"
     "    if has_venue and has_trade:\n"
     "        return True, \"VENUE_TRADE_QUERY\"\n\n"
     "    if any(re.search(pattern, kw) for pattern in _NO_RECOMMEND_PATTERNS):\n"
     "        return False, \"KNOWLEDGE_PATTERN\"\n\n"),

    ("M8", "T1 组合判定退化成万能信号:「在哪」不再要求紧跟商业对象 → 误收商场洗手间在哪",
     POLICY,
     'rf"(?:{_VENUE_GROUP})(?:在哪儿?里?|有哪些|有几家|在什么地方|地址|电话|联系方式|怎么走)"',
     'r"(?:在哪儿?里?|有哪些|有几家|在什么地方|地址|电话|联系方式|怎么走)"'),

    ("M9", "业务轴否决力边界被抹掉:没声明业务范围也照样扣词 → 资料薄的客户零交付",
     DECISION,
     "        return self.declared and bool(self.tokens)\n",
     "        return bool(self.tokens)\n"),

    ("M10", "T6 老字段回到硬写(R4 的原样复刻:scope_match 恒 True)",
     EXPANDER,
     '            enriched["scope_match"] = not (\n'
     '                decision.business_scope == SCOPE_MISMATCHED\n'
     '                or decision.geo_scope != GEO_MATCHED\n'
     '            )\n',
     '            enriched["scope_match"] = True\n'),

    ("M11", "T2 待确认区被静默丢弃(rejected_keywords 不再回传)",
     EXPANDER,
     "        results[\"rejected_keywords\"] = rejected_all\n",
     "        results[\"rejected_keywords\"] = []\n"),
]


def _clean_pyc() -> None:
    shutil.rmtree(_PYC_DIR, ignore_errors=True)


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = str(_PYC_DIR)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_suite(targets: list[str]) -> bool:
    _clean_pyc()
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *targets, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=1800, env=_child_env())
    return r.returncode == 0


def main() -> int:
    if not os.environ.get("TEST_DATABASE_URL"):
        print("⚠️  没有 TEST_DATABASE_URL:API 锁会 skip。")

    print("→ 基线(含 API 锁,约 40s)…")
    if not run_suite(["tests/quotegeo_2026_08_10/"]):
        print("❌ 基线就红 —— 变异结果无意义,先修基线")
        return 2
    print("✅ 基线绿(全量)\n")

    fast = [T_FAST, T_GEO, T_BIZ]
    if not run_suite(fast):
        print("❌ 快集基线红")
        return 2

    results = []
    for mid, desc, rel, old, new in MUTATIONS:
        path = REPO / rel
        original = path.read_bytes()
        old_b, new_b = old.encode("utf-8"), new.encode("utf-8")
        hits = original.count(old_b)
        if hits != 1:
            results.append((mid, f"SKIP(锚点命中 {hits} ≠ 1)"))
            print(f"⚠️  {mid} SKIP:锚点命中 {hits} 次 · {desc}")
            continue
        mutated = original.replace(old_b, new_b)
        assert mutated != original
        try:
            path.write_bytes(mutated)
            green = run_suite(fast)
        finally:
            path.write_bytes(original)
        if path.read_bytes() != original:
            print(f"🔴 {mid} 还原失败!{rel} 与原文不一致,人工介入")
            return 3
        if green:
            results.append((mid, "SURVIVED"))
            print(f"❌ {mid} SURVIVED(变异后仍绿,锁抓不到):{desc}")
        else:
            results.append((mid, "KILLED"))
            print(f"✅ {mid} KILLED:{desc}")

    killed = sum(1 for _, s in results if s == "KILLED")
    survived = [m for m, s in results if s == "SURVIVED"]
    skipped = [m for m, s in results if s.startswith("SKIP")]
    print(f"\n==== 变异结果:{killed} killed / {len(survived)} survived / {len(skipped)} skip ====")
    if survived:
        print("SURVIVED:", ", ".join(survived))
        return 1
    if skipped:
        print("SKIP(锚点问题,不算通过):", ", ".join(skipped))
        return 1
    _clean_pyc()
    print("✅ 全部变异被击杀,锁有判别力")
    return 0


if __name__ == "__main__":
    sys.exit(main())
