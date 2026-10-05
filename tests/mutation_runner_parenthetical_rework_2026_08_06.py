"""变异检验 · 括号品牌 P0 返工(行政区划结构闸)· 2026-08-06。

返工单 `WO_DIAGNOSIS_PARENTHETICAL_REWORK_2026-08-06.md` §1 + 复审 P1/P2。

跑法(需 throwaway PG,与锁套件同一个):
    TEST_DATABASE_URL=postgresql://... python tests/mutation_runner_parenthetical_rework_2026_08_06.py

判据套件 = **两套一起跑**(resolver + mentionfit):行政区划闸落在 resolver,
mentionfit 的前提断言锁钉的是同一行为的另一面 —— 只跑一套会漏掉
"改了 A 让 B 静默转向"。

🔴 自坏防线(返工单 §4 + 复审 P2):
  1. 每次跑前清 `__pycache__`,子进程带 `-B` + `PYTHONDONTWRITEBYTECODE=1`
     —— 缓存会把「已杀死」误报成「存活」(单向偏差);
  2. 锚点命中次数必须恰为 1,否则 ANCHOR_BAD 整体失败(抓不到 = 变异没落盘);
  3. 退出码 5 单独报 NO_TESTS,不许算成杀死;
  4. **输出全 ASCII 标记 + stdout 显式切 UTF-8** —— 复审实测:Windows 默认 GBK
     控制台会被 emoji/中文打崩(UnicodeEncodeError),标准 runner 上跑不到结果。
     我在 Git Bash 里跑所以没撞上 —— **"我这儿能跑"不是判据**。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

try:  # 见 docstring 第 4 条;只换 ASCII 标记不够,中文说明照样会崩
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - 老 Python / 非 TTY
    pass

ROOT = Path(__file__).resolve().parent.parent
LOCK_SUITES = [
    "tests/geo_monitoring_content_loop/test_brand_identity_resolver.py",
    "tests/test_diagnosis_mention_variant_and_question_fitness_2026_08_06.py",
    "tests/test_parenthetical_rebuild_scope_2026_08_06.py",
]

RESOLVER = "services/brand_identity_resolver.py"
MENTIONFIT = "tests/test_diagnosis_mention_variant_and_question_fitness_2026_08_06.py"

SUFFIX_LINE = '    "省", "市", "区", "县", "镇", "乡", "街道", "旗", "盟",'
STRUCT_BODY = (
    "        for chunk in _admin_chunks(text):\n"
    "            for suffix in _ADMINISTRATIVE_CHUNK_SUFFIXES:\n"
    "                if chunk.endswith(suffix) and len(chunk) > len(suffix):\n"
    "                    return True\n"
)
GATE_CALL = (
    "    if not explicit_alias and _looks_like_administrative_address(candidate):\n"
    "        return None\n"
)
ALIAS_PREFIX_NEW = (
    '    r"^(?:简称|品牌简称|品牌名|英文名|英文简称|又称|又名|亦称|别名)'
    '\\s*[:：]?\\s*", re.I'
)
ALIAS_PREFIX_OLD = '    r"^(?:简称|品牌简称|英文名|英文简称|又称)\\s*[:：]?\\s*", re.I'

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    ("R01", "行政区划结构闸整条删掉(龙岗区平湖/茅台镇又进可信别名)",
     RESOLVER, GATE_CALL, "    if False:\n        return None\n"),

    ("R02", "后缀表漏掉「区」(区级复合地名穿透)",
     RESOLVER, SUFFIX_LINE, '    "省", "市", "县", "镇", "乡", "街道", "旗", "盟",'),

    ("R03", "后缀表漏掉「镇」(茅台镇穿透)",
     RESOLVER, SUFFIX_LINE, '    "省", "市", "区", "县", "乡", "街道", "旗", "盟",'),

    ("R04", "[复审P1] 后缀表加「村」-> 乡村基被切成'乡村'而误杀(真品牌)",
     RESOLVER, SUFFIX_LINE,
     '    "省", "市", "区", "县", "镇", "乡", "村", "街道", "旗", "盟",'),

    ("R05", "[复审P1] 退回字符级黑名单(我第一版写法 · 误杀 6 个真实品牌)",
     RESOLVER, STRUCT_BODY,
     "        if any(m in text for m in _ADMINISTRATIVE_CHUNK_SUFFIXES):\n"
     "            return True\n"),

    ("R06", "恒拒(把括号别名全杀了)-- 反向对照必须抓住",
     RESOLVER,
     "def _parenthetical_identity_alias(raw: str) -> str | None:\n"
     '    """Return a parenthetical identity alias, never a location/legal qualifier."""\n',
     "def _parenthetical_identity_alias(raw: str) -> str | None:\n"
     '    """Return a parenthetical identity alias, never a location/legal qualifier."""\n'
     "    return None\n"),

    ("R07", "显式别名逃生口失效(explicit_alias 恒假 · 又名:城市之光被误杀)",
     RESOLVER,
     "    explicit_alias = bool(_ALIAS_PREFIX_RE.match(candidate))\n",
     "    explicit_alias = False\n"),

    ("R08", "别名前缀表退回旧版(缺「又名」)",
     RESOLVER, ALIAS_PREFIX_NEW, ALIAS_PREFIX_OLD),

    ("R10", "[三轮] 名录全覆盖判据删掉 -> 裸城市名(中山/朝阳)又穿透",
     RESOLVER,
     "    return _decomposes_into_administrative_names(normalize_brand_name(text))\n",
     "    return False\n"),

    ("R11", "[三轮] 名录退回 93 项手工窄表 -> 十二城全穿透",
     RESOLVER,
     "        from tools.keyword_value_scorer import _CITY_PREFIXES\n",
     "        raise ImportError('mutated')\n"
     "        from tools.keyword_value_scorer import _CITY_PREFIXES\n"),

    ("R12", "[三轮 · 复审2] 把全量名录也喂给剥前缀路 -> 洛阳钼业被剥成钼业",
     RESOLVER,
     "def _without_administrative_prefix(value: str) -> str:\n"
     "    for prefix in _administrative_prefix_variants():\n",
     "def _without_administrative_prefix(value: str) -> str:\n"
     "    for prefix in sorted(_administrative_name_set(), key=len, reverse=True):\n"),

    # 🔴 原 R13(把 _ADMIN_NAME_MIN_LEN 2->1)实测 **SURVIVED,但那不是锁松** ——
    #    名录里本来就没有 1 字地名,改了这个常量集合与切分结果都不变,
    #    是一条**惰性变异**(改了不产生行为差)。惰性变异留在集合里等于虚报覆盖,
    #    换成真正会复现二轮事故的那一条:全覆盖判据退化成"以地名开头"。
    ("R13", "[三轮] 全覆盖退化成 startswith(二轮误杀六个真品牌的那个写法)",
     RESOLVER,
     "    reachable = [False] * (len(normalized) + 1)\n"
     "    reachable[0] = True\n",
     "    return any(normalized.startswith(n) for n in names)\n"
     "    reachable = [False] * (len(normalized) + 1)\n"
     "    reachable[0] = True\n"),

    ("R14", "[三轮复查] 自治州/盟/地区补名单删掉 -> 甘孜/凉山/黔东南又穿透",
     RESOLVER,
     "        _PREFECTURE_LEVEL_EXTRA_NAMES,\n",
     ""),

    ("R15", "[三轮复查] 补名单也塞进剥前缀窄表 -> 延边敖东被剥成敖东",
     RESOLVER,
     "    variants = set(_ADMINISTRATIVE_PREFIXES)\n",
     "    variants = set(_ADMINISTRATIVE_PREFIXES) | set(_PREFECTURE_LEVEL_EXTRA_NAMES)\n"),

    ("R09", "mentionfit 前提断言翻回旧行为(P0 修复被悄悄回退时不转红)",
     MENTIONFIT,
     '    assert "龙岗区平湖" not in long_bracket.all_trusted_names, (',
     '    assert "龙岗区平湖" in long_bracket.all_trusted_names, ('),
]


def _purge_pycache() -> None:
    for path in ROOT.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)


def _run_locks() -> str:
    _purge_pycache()
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-x", "-p", "no:randomly",
         "-p", "no:cacheprovider", *LOCK_SUITES],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if proc.returncode == 5:
        return "NO_TESTS"
    return "KILLED" if proc.returncode != 0 else "SURVIVED"


def main() -> int:
    if not os.getenv("TEST_DATABASE_URL"):
        print("[ERR ] 需要 TEST_DATABASE_URL(与锁套件同一个 throwaway PG)")
        return 2

    print("---- 基线自检:未变异时两套锁必须全绿 ----")
    baseline = _run_locks()
    if baseline != "SURVIVED":
        print(f"[ERR ] 基线就不是绿的({baseline}) -> 变异结果无意义,先修基线")
        return 2
    print("[OK  ] 基线全绿\n")

    results: list[tuple[str, str, str]] = []
    for code, note, rel_path, anchor, replacement in MUTATIONS:
        target = ROOT / rel_path
        original = target.read_text(encoding="utf-8")
        occurrences = original.count(anchor)
        if occurrences != 1:
            results.append((code, "ANCHOR_BAD", f"{note}(锚点命中 {occurrences} 次,应为 1)"))
            print(f"{code}  [BAD ] ANCHOR_BAD  {note}(命中 {occurrences} 次)")
            continue
        try:
            # newline="" —— 不让 write 按平台改行尾(改成 CRLF 会让后续锚点全部失配)
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(original.replace(anchor, replacement))
            verdict = _run_locks()
        finally:
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(original)
        icon = {"KILLED": "[KILL]", "SURVIVED": "[LIVE]", "NO_TESTS": "[NONE]"}[verdict]
        results.append((code, verdict, note))
        print(f"{code}  {icon} {verdict:9} {note}")

    _purge_pycache()
    killed = sum(1 for _, v, _ in results if v == "KILLED")
    total = len(results)
    print(f"\n变异 {total} 条 · 杀死 {killed} · 存活/异常 {total - killed}")
    bad = [(c, v, n) for c, v, n in results if v != "KILLED"]
    if bad:
        print("[FAIL] 下列变异未被任何锁抓住 —— 锁是摆设,必须补硬:")
        for code, verdict, note in bad:
            print(f"   {code} [{verdict}] {note}")
        return 1
    print("[PASS] 全部杀死")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
