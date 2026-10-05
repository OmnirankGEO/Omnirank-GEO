"""变异检验 · WO_DIAGNOSIS_MENTION_VARIANT_AND_QUESTION_FITNESS 2026-08-06。

每条变异 = 「有人把这个修改回去/改坏」的一种具体写法。锁必须**当场转红**。
全部 KILLED 才算锁有判别力;任何一条 SURVIVED = 那条锁是摆设,必须补硬。

跑法(需要一个 throwaway PG,与锁套件同一个):
    TEST_DATABASE_URL=postgresql://... python tests/mutation_runner_mention_variant_2026_08_06.py

🔴 三条自坏防线(都是本周实证踩过的坑,写进 runner 而不是靠人记得):

  1. **`.pyc` 缓存会把「已杀死」误报成「存活」**(flywheel 包 2026-08-06 实证,
     而且是**单向偏差** —— 只会让人以为锁没用,反过来不会)。每次跑前清
     ``__pycache__``,且用 ``-p no:cacheprovider`` 关掉 pytest 自己的缓存。
  2. **锚点必须先自检**:锚字符串在源文件里出现次数 ≠ 1 就报 ANCHOR_BAD 并整体
     失败。锚点抓不到 = 这条变异根本没落盘,"全部 KILLED" 是假的。
  3. **SKIP ≠ SURVIVED,也 ≠ KILLED**:pytest 退出码 5(没收集到用例)单独报
     NO_TESTS,不许算成杀死 —— 那是判据没跑,不是判据生效。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# 🔴 [复审 P2 · 2026-08-06] Windows 默认 GBK 控制台会被中文/符号输出打崩
# (UnicodeEncodeError),标准 runner 上根本跑不到结果。既换 ASCII 标记,
# 也把 stdout 显式切 UTF-8 —— 只做一半仍会死在中文说明上。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - 老 Python / 非 TTY
    pass

ROOT = Path(__file__).resolve().parent.parent
LOCK_SUITE = "tests/test_diagnosis_mention_variant_and_question_fitness_2026_08_06.py"

NEAR_MISS = "services/brand_name_near_miss.py"
RESOLVER = "services/brand_identity_resolver.py"
NOMINATION = "services/nomination_question_policy.py"
QUALITY = "services/diagnosis_question_quality.py"
KEYWORDS = "tools/keyword_generator.py"
BRAND_API = "api/brand_api.py"
POLICY = "services/commercial_query_policy.py"

#: (编号, 说明, 文件, 锚点, 替换)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ── §1 近失检出层 ────────────────────────────────────────────────────
    (
        "M01", "删掉长度下限闸(2 字词之间差一字就算变体)",
        NEAR_MISS,
        "        if len(normalized) < NEAR_MISS_MIN_LENGTH:\n            continue\n",
        "        if False:\n            continue\n",
    ),
    (
        "M02", "删掉地名闸(「深圳」可当品牌变体候选 = 重开 08-05 假阳性)",
        NEAR_MISS,
        "        if _is_geographic_only(normalized) or _is_generic_only(display):\n            continue\n",
        "        if _is_generic_only(display):\n            continue\n",
    ),
    (
        "M03", "删掉 rejected 过滤(人工判过「不是」的名字又回到待确认队列)",
        NEAR_MISS,
        "        if normalized in rejected_norm:\n            continue\n",
        "        if False:\n            continue\n",
    ),
    (
        "M04", "基准改回 all_trusted_names(括号城市又成了近失基准)",
        NEAR_MISS,
        "    trusted = [\n"
        "        *(getattr(identity, \"canonical_names\", ()) or ()),\n"
        "        *(getattr(identity, \"trusted_aliases\", ()) or ()),\n"
        "    ]\n",
        "    trusted = list(getattr(identity, \"all_trusted_names\", ()) or ())\n",
    ),
    (
        "M05", "受限扩展上限 4 → 40(「包含就算」)",
        NEAR_MISS,
        "NEAR_MISS_MAX_EXTENSION = 4",
        "NEAR_MISS_MAX_EXTENSION = 40",
    ),
    (
        "M06", "整条受限扩展规则删掉(≤5 字品牌的门店形态又没出口)",
        NEAR_MISS,
        "                if (\n"
        "                    0 < extension <= NEAR_MISS_MAX_EXTENSION\n"
        "                    and trusted_normalized in normalized\n"
        "                ):\n",
        "                if False:\n",
    ),
    (
        "M07", "近失候选写进 matched_alias(命中语义污染 · 下游会升格成明确推荐)",
        RESOLVER,
        "                    evidence_snippet=local.evidence_snippet,\n"
        "                    near_miss_alias=(matched_text if source_span is not None else None),\n",
        "                    evidence_snippet=local.evidence_snippet,\n"
        "                    matched_alias=(matched_text if source_span is not None else None),\n",
    ),
    (
        "M08", "幻觉名(原文里没有)也当近失候选带出去",
        RESOLVER,
        "near_miss_alias=(matched_text if source_span is not None else None),",
        "near_miss_alias=matched_text,",
    ),
    (
        "M09", "放宽复核校验(_is_plausible_verified_match 恒真)= 未确认就算提到",
        RESOLVER,
        "def _is_plausible_verified_match(matched_text: str, identity: BrandIdentity) -> bool:\n"
        "    \"\"\"Validate a source-exact verifier YES against trusted or legal short forms.\"\"\"\n",
        "def _is_plausible_verified_match(matched_text: str, identity: BrandIdentity) -> bool:\n"
        "    \"\"\"Validate a source-exact verifier YES against trusted or legal short forms.\"\"\"\n"
        "    return True\n",
    ),
    (
        "M10", "存量格不再现算近失(待确认卡片恒零候选)",
        "services/diagnosis_identity_review.py",
        "    if not candidates and identity is not None:\n"
        "        candidate_pool = [\n",
        "    if False:\n"
        "        candidate_pool = [\n",
    ),
    # ── §2 提名型闸 ──────────────────────────────────────────────────────
    (
        "M11", "提名闸不再先委托商业闸(⊂ 关系断掉 · 闸叠错层)",
        NOMINATION,
        "    decision = evaluate(normalized, brand_name=brand_name)\n"
        "    if not decision.commercial_delivery_eligible:\n"
        "        return False, REASON_NOT_COMMERCIAL\n",
        "    decision = evaluate(normalized, brand_name=brand_name)\n"
        "    if False:\n"
        "        return False, REASON_NOT_COMMERCIAL\n",
    ),
    (
        "M12", "裸「有哪些」加回信号表(方法论题整条漏进来)",
        NOMINATION,
        '    rf"(?:{_PROVIDER_NOUN_GROUP}).{{0,4}}有哪些",',
        '    r"有哪些",',
    ),
    (
        "M13", "出题层只查商业闸,不查提名闸(工单案例题原样留在诊断里)",
        KEYWORDS,
        "        if is_nomination_question is None:\n            return True\n",
        "        if True:\n            return True\n",
    ),
    (
        "M14", "模板池把「怎么选？」加回去(出口修了兜底池还在产)",
        QUALITY,
        '        "{geo}{trade}哪几家口碑好？",',
        '        "{geo}{trade}怎么选？",',
    ),
    (
        "M15", "降级兜底池把「一般怎么收费？」加回去",
        KEYWORDS,
        '            f"{trade}哪家性价比高？推荐几家",',
        '            f"{trade}一般怎么收费？",',
    ),
    (
        "M16", "调研词兜底池把方法论词加回去",
        BRAND_API,
        '        f"{local_service}哪家好",',
        '        f"{local_service}怎么选",',
    ),
    (
        "M17", "动了报价侧 SSOT 语义(工单 §2.1 明令不许碰)",
        POLICY,
        '    r"注意事项|注意什么|需要什么",',
        '    r"注意事项|注意什么|需要什么|怎么选|不踩坑",',
    ),
    # ── §3 地名重复拼接 ──────────────────────────────────────────────────
    (
        "M18", "无后缀省市串不再拆分(「贵州贵阳」又回到单 token)",
        QUALITY,
        "        if tail.startswith(prefix) and len(tail) - len(prefix) >= 2:\n"
        "            return [prefix, tail[len(prefix):]]\n",
        "        if False:\n"
        "            return [prefix, tail[len(prefix):]]\n",
    ),
    (
        "M19", "_admin_chunks 不再吐拆分结果(第一道防线断)",
        QUALITY,
        "        chunks.extend(_split_suffixless_geo(tail))\n",
        "        pass\n",
    ),
    (
        "M20", "拼接不再去重(第二道防线断 · 重复地名复现)",
        QUALITY,
        "    segments = [seg for seg in _split_suffixless_geo(prefix) if seg]\n"
        "    if any(seg in body for seg in segments):\n"
        "        return body\n",
        "    segments = []\n"
        "    if segments:\n"
        "        return body\n",
    ),
    (
        "M21", "拼接改成永不加前缀(恒真陷阱 · 不含地名的题也不再补地域)",
        QUALITY,
        '    if prefix in body:\n        return body\n',
        '    if True:\n        return body\n',
    ),
]


def _purge_pycache() -> None:
    """清 ``__pycache__`` —— 不清会把「已杀死」误报成「存活」(单向偏差)。"""
    for path in ROOT.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)


def _run_locks() -> str:
    """跑锁套件;返回 KILLED / SURVIVED / NO_TESTS。"""
    _purge_pycache()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:randomly",
         "-p", "no:cacheprovider", LOCK_SUITE],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if proc.returncode == 5:
        return "NO_TESTS"
    return "KILLED" if proc.returncode != 0 else "SURVIVED"


def main() -> int:
    if not os.getenv("TEST_DATABASE_URL"):
        print("[ERR ] 需要 TEST_DATABASE_URL(与锁套件同一个 throwaway PG)")
        return 2

    print("── 基线自检:未变异时锁必须全绿 ──")
    baseline = _run_locks()
    if baseline != "SURVIVED":
        print(f"[ERR ] 基线就不是绿的({baseline}) → 变异结果无意义,先修基线")
        return 2
    print("   ✅ 基线全绿\n")

    results: list[tuple[str, str, str]] = []
    for code, note, rel_path, anchor, replacement in MUTATIONS:
        target = ROOT / rel_path
        original = target.read_text(encoding="utf-8")
        occurrences = original.count(anchor)
        if occurrences != 1:
            # 锚点抓不到 = 这条变异没落盘。报错,不许静默当成 KILLED。
            results.append((code, "ANCHOR_BAD", f"{note}(锚点命中 {occurrences} 次,应为 1)"))
            print(f"{code}  [BAD ] ANCHOR_BAD  {note}(命中 {occurrences} 次)")
            continue
        try:
            # 🔴 newline="" —— Path.write_text 会按平台改行尾,把整份源文件写成
            #    CRLF 会让后续锚点全部失配(媒体榜包 2026-08-05 实证)。
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
