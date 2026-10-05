# -*- coding: utf-8 -*-
"""变异自检:证明 REWORK-DIAG-QGATE 的判别锁真的有判别力。

跑法(在仓库根)::

    PYTHONPATH=. python tests/selftest_qgate_rework_mutations.py

每条变异做三件事(缺一不算过):
  1. **锚点存在性** —— old 串必须在源码里唯一命中。找不到 = 锚点漂移,当场红;
  2. **非 no-op 自检** —— 变异后源码必须真的变了(踩过:撤一行但别的路径仍命中);
  3. **锁必须转红** —— 指定的测试在变异后必须 FAILED。仍绿 = 锁没打到点上。

外加一条**死函数扫描**:新增的公开函数必须能 grep 到 ≥1 个非测试真实调用点
(返工单 §4.1 · 防"零调用零测试照样过审")。扫描前先剥 `#` 注释与 docstring,
避免"断言命中的是解释它的注释"那类假绿。
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "services" / "diagnosis_question_quality.py"
SCOPE = ROOT / "services" / "diagnosis_business_scope.py"
LOCKS = "tests/test_diagnosis_question_gate_rework_2026_08_03.py"

# (编号, 说明, [(文件, old, new), ...], 必须转红的测试)
# 用**编辑列表**而不是单条 old/new:有些回归要同时动两处才成立(见 M11)。
# 只改一处就转红的话,反而说明那一处不是真正的防线所在。
MUTATIONS = [
    (
        "M1-R1", "has_geo_qualifier 的 token 命中改回 `city in raw`(返工单点名的变异锚点)",
        [(GATE,
        "    if any(token in raw for token in geo_tokens(city)):\n        return True",
        "    _c = normalize_city(city)\n    if _c and _c in raw:\n        return True",
        )],
        ["test_r1_has_geo_qualifier_accepts_any_token_not_verbatim_whole_string"],
    ),
    (
        "M2-R1", "normalize_city 不再剥括号(退回 07-26 行为)",
        [(GATE,
        "    outer, _inner = _strip_brackets(raw)",
        "    outer, _inner = raw, []",
        )],
        ["test_r1_normalize_city_handles_bracket_forms"],
    ),
    (
        # 🔴 这里不能写成 `if False:` —— 那是 **no-op 变异**:
        # ``normalize_admin_name`` 的后缀表里本来就没有 镇/街道/乡/村,
        # 走不走这个分支结果都是"茅台镇"。要证明锁有判别力,变异必须**真的**
        # 把后缀剥掉(那才是 §7.2 要防的那个回归)。
        "M3-R1", "镇级 token 被剥后缀(茅台镇→茅台 · §7.2 品牌碰撞)",
        [(GATE,
        "    if chunk.endswith(_SUBCITY_KEEP_WHOLE):\n        return chunk",
        "    if chunk.endswith(_SUBCITY_KEEP_WHOLE):\n        return chunk[:-1]",
        )],
        ["test_r1_town_level_token_keeps_suffix_no_brand_collision"],
    ),
    (
        "M4-R1", "enforce 里把原始 city 换回归一后的 town(镇级 token 丢失)",
        [(GATE,
        "and not has_geo_qualifier(text, city):",
        "and not has_geo_qualifier(text, town):",
        )],
        ["test_e2e_hequan_llm_questions_survive_the_gate"],
    ),
    (
        "M5-R2", "模板池改回只用第一个品类词(返工单点名的变异锚点)",
        [(GATE,
        "            trade = trades[idx % len(trades)]",
        "            trade = trades[0]",
        )],
        ["test_r2_pools_rotate_by_trade_not_by_suffix"],
    ),
    (
        "M6-R2", "distill_trades 退回单值(只留最短那个)",
        [(GATE,
        "    if picked:\n        return picked",
        "    if picked:\n        return picked[:1]",
        )],
        ["test_r2_multiple_trades_distilled_from_keywords"],
    ),
    (
        "M7-R2", "拆掉品牌名泄漏防护",
        [(GATE,
        "        if any(core in kw for core in cores):",
        "        if False:",
        )],
        ["test_r2_brand_name_never_leaks_into_trades"],
    ),
    (
        "M8-R2", "回落后缀轮转时不再 log(静默回落)",
        [(GATE,
        "    if len(trades) < 2:\n        logger.info(",
        "    if False:\n        logger.info(",
        )],
        ["test_r2_fallback_to_suffix_rotation_is_logged_not_silent"],
    ),
    (
        "M9-R2", "品类词自带地名时仍强加城市前缀(双地名鬼话)",
        [(GATE,
        '        return "" if any(tok in trade for tok in tokens) else geo',
        "        return geo",
        )],
        ["test_r2_trade_carrying_its_own_geo_is_not_double_prefixed"],
    ),
    (
        "M10-R3", "brands.city_scope='national' 不再覆盖表单值",
        [(SCOPE,
        "            return SCOPE_NATIONAL\n    except Exception as err:",
        "            return business_scope\n    except Exception as err:",
        )],
        ["test_r3_brand_city_scope_national_overrides_form_regional"],
    ),
    (
        # 🔴 这条必须**同时改两处**才成立 —— 单改任一处都是 no-op:
        #   · 只让 brand_scope 权威化:早退还在 → 表单选 national 的根本走不到查库,
        #     且 normalize('local')=='regional' 与传入的 regional 同义 → 行为不变;
        #   · 只删早退:条件仍要求 =='national',拿到 'local' 就跳过 → 行为不变。
        # 两处一起改 = **严格按返工单字面**实现的样子,而那正是"修 A 坏 B"的形状。
        "M11-R3", "严格按原单字面:city_scope 有值即权威(死默认 'local' 会把表单选的 national 打回)",
        [
            (SCOPE,
             "    if normalize_business_scope(business_scope) == SCOPE_NATIONAL:\n"
             "        return business_scope  # 已经是 national,无需再查库\n",
             ""),
            (SCOPE,
             "        if brand_scope and normalize_business_scope(brand_scope) == SCOPE_NATIONAL:\n",
             "        if brand_scope:\n            return normalize_business_scope(brand_scope)\n"
             "        if False:\n"),
        ],
        ["test_r3_dead_default_local_must_not_demote_an_explicit_national_form"],
    ),
    (
        "M12-R4", "拆掉补地域分支,退回整条替换",
        [(GATE,
        '        if problems == ["missing_geo_qualifier"] and town:',
        "        if False:",
        )],
        ["test_r4_geo_only_problem_is_prefixed_keeping_the_trade_words",
         "test_r4_repairs_distinguish_prefixed_from_replaced"],
    ),
    (
        "M13-R4", "补地域分支放宽到所有问题类型(服务名称式短语也只补前缀)",
        [(GATE,
        '        if problems == ["missing_geo_qualifier"] and town:',
        '        if "missing_geo_qualifier" in problems and town:',
        )],
        ["test_r4_multi_problem_question_is_still_fully_replaced"],
    ),
]

# 新增公开函数 → 必须有的非测试真实调用点
NEW_PUBLIC_FUNCS = {
    "geo_tokens": ["services/diagnosis_question_quality.py"],
    "distill_trades": ["services/diagnosis_question_quality.py"],
    "brand_core_name": ["services/diagnosis_question_quality.py"],
    "resolve_effective_business_scope": ["workflows/diagnosis_workflow.py"],
}


def read_raw(path: Path) -> str:
    """读文件但**不做换行翻译**(``newline=""``)。

    🔴 不能用 ``Path.read_text`` / ``write_text`` 的默认行为:Windows 上
    ``write_text`` 会把 ``\\n`` 翻成 ``\\r\\n``,而读进来时通用换行又把
    ``\\r\\n`` 折成 ``\\n`` —— 这个自检脚本每跑一次,就会把它"改回原样"的
    那几个源文件整份翻成 CRLF。本仓生产树是 LF,那等于每跑一次自检就制造
    一份全文件行尾 diff(后续任何包碰同一文件都整文件冲突)。
    🔴 也不能用 ``read_text(newline="")`` —— 那个参数是 **Python 3.13+**,
    生产容器是 3.12,本机 3.14 上跑得通是假证据。``open()`` 各版本都支持。
    """
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def write_raw(path: Path, text: str) -> None:
    """写文件但**不做换行翻译**——原文什么行尾,写回去还是什么行尾。"""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def strip_comments_and_docstrings(source: str) -> str:
    """剥掉 `#` 注释与字符串字面量(含 docstring)后再做结构断言。

    踩过多次:断言命中的是**解释这段代码的注释**,把真代码删掉照样绿。
    """
    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except tokenize.TokenError:
        return source
    return "\n".join(out)


def run_tests(names: list[str]) -> tuple[bool, str]:
    """跑指定测试;返回 (是否全绿, 输出尾部)。"""
    expr = " or ".join(names)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", LOCKS, "-q", "-k", expr, "--no-header"],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",  # Windows 默认 gbk 会在中文断言消息上炸
        env={**__import__("os").environ, "PYTHONPATH": ".", "PYTHONIOENCODING": "utf-8"},
    )
    return proc.returncode == 0, (proc.stdout or "")[-400:]


def main() -> int:
    failures: list[str] = []

    # ── 0. 变异前必须全绿(基线不绿的话所有"转红"都没意义)─────────────────
    green, tail = run_tests(["test_"])
    if not green:
        print("🔴 基线就不绿,变异自检无意义:\n" + tail)
        return 1
    print("✅ 基线全绿")

    # ── 0.5 行尾守卫:本包文件必须是 LF(生产树口径)────────────────────
    # 🔴 2026-08-03 返修:第一版交付的 5 个文件全是 CRLF,`diagnosis_workflow.py`
    #    语义只改 7 行却带了 5,554 行行尾噪音 —— 后续任何包碰这文件都整文件冲突。
    #    病根就是本脚本用了 ``Path.write_text``(Windows 上 \n → \r\n)。
    # 🔴 判据必须用**字节计数**,不能用 `grep -c $'\r$'` —— 实测那个判据在本仓
    #    Git Bash 下对**纯 LF 文件**报出满屏 CRLF(假阳性),差点让我拿它反驳返修单。
    print("\n── 行尾守卫(字节计数 · 非 grep)──")
    for rel in (
        "services/diagnosis_question_quality.py",
        "services/diagnosis_business_scope.py",
        "workflows/diagnosis_workflow.py",
        "tests/test_diagnosis_question_gate_rework_2026_08_03.py",
        "tests/selftest_qgate_rework_mutations.py",
    ):
        raw = (ROOT / rel).read_bytes()
        cr = raw.count(b"\r")
        if cr:
            failures.append(f"行尾: {rel} 有 {cr} 个 CR(生产树是 LF)")
            print(f"  🔴 {rel}: CR={cr}")
        else:
            print(f"  ✅ {rel}: CR=0 · LF={raw.count(chr(10).encode())}")
    # 反向对照:判据必须真的能报出 CRLF,否则上面全是假绿
    if b"\r\n".count(b"\r") != 1:
        failures.append("行尾判据自身失效")
    print("  ✅ 反向对照:同一判据对 b'\\r\\n' 报 CR=1(判据有判别力)")

    # ── 1. 死函数扫描:新增公开函数必须有非测试真实调用点 ────────────────
    print("\n── 死函数扫描(剥注释/docstring 后)──")
    for func, expected_files in NEW_PUBLIC_FUNCS.items():
        callers: list[str] = []
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith(("tests/", ".venv/")) or "site-packages" in rel:
                continue
            try:
                code = strip_comments_and_docstrings(read_raw(path))
            except Exception:
                continue
            # 调用点 = `func(` 出现,且不是它自己的 def
            for m in re.finditer(rf"\b{re.escape(func)}\s*\(", code):
                before = code[max(0, m.start() - 12):m.start()]
                if before.rstrip().endswith("def"):
                    continue
                callers.append(rel)
                break
        callers = sorted(set(callers))
        if not callers:
            failures.append(f"死函数: {func} 零真实调用点(测试不算)")
            print(f"  🔴 {func}: 零调用点")
        else:
            print(f"  ✅ {func}: {callers}")
        for expected in expected_files:
            if expected not in callers:
                failures.append(f"{func} 期望在 {expected} 有调用点,实际 {callers}")

    # ── 2. 逐条变异 ────────────────────────────────────────────────────
    print("\n── 变异(每条:锚点存在 → 真改了 → 锁转红)──")
    for tag, desc, edits, must_red in MUTATIONS:
        originals = {path: read_raw(path) for path, _, _ in edits}
        drifted = False
        pending: dict[Path, str] = dict(originals)
        for path, old, new in edits:
            hits = pending[path].count(old)
            if hits != 1:
                failures.append(f"{tag} 锚点命中 {hits} 次(必须恰好 1 次)——锚点漂移了")
                print(f"  🔴 {tag} 锚点命中 {hits} 次: {desc}")
                drifted = True
                break
            pending[path] = pending[path].replace(old, new, 1)
        if drifted:
            continue
        if all(pending[p] == originals[p] for p in originals):
            failures.append(f"{tag} 是 no-op 变异(源码没变)")
            print(f"  🔴 {tag} no-op")
            continue
        for path, text in pending.items():
            write_raw(path, text)
        try:
            green, tail = run_tests(must_red)
        finally:
            for path, text in originals.items():
                write_raw(path, text)
        if green:
            failures.append(f"{tag} 变异后锁仍绿 —— 锁没打到点上,重写: {desc}")
            print(f"  🔴 {tag} 仍绿: {desc}")
        else:
            print(f"  ✅ {tag} 已转红: {desc}")

    # ── 3. 复原后必须重新全绿(证明自检没把仓库改坏)─────────────────────
    green, tail = run_tests(["test_"])
    if not green:
        failures.append("复原后不再全绿 —— 自检脚本把源码改坏了")
        print("\n🔴 复原后不绿:\n" + tail)
    else:
        print("\n✅ 复原后仍全绿")

    print("\n" + "=" * 60)
    if failures:
        print(f"🔴 变异自检失败 {len(failures)} 条:")
        for f in failures:
            print("   -", f)
        return 1
    print(f"✅ 变异自检全过:{len(MUTATIONS)} 条变异全部转红 · "
          f"{len(NEW_PUBLIC_FUNCS)} 个新函数全有真实调用点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
