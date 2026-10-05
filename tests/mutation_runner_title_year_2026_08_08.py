#!/usr/bin/env python3
"""变异验证 · P2 标题年份分文体条件默认(WO_P2P3_ARTICLE_STYLE_DEV 补章 R1)。

每条变异 = "这个包的某一部分被撤掉 / 写歪"的具体形态。跑一遍被它覆盖的锁,
**必须变红**;红了才说明那条锁有判别力。

变异集刻意分成两类:
  · **口径类**(M01/M07/M09/M10):把合同里的数据或规则改歪 —— 抓的是"结论是推导的还是手填的";
  · **接线类**(M02-M06/M11/M12):把某一层从合同上摘下来自己写 —— 抓的是本包的**主病**
    (改动前四层各写一份年份口径、三种意见)。接线类要是杀不死,这个包等于没做。

🔴 Windows 四坑已规避(第 4 条是本单新踩出来的):
  1. **按字节读写**(`read_bytes/write_bytes`)—— `read_text/write_text` 在 Windows 上
     是 LF→CRLF 单向阀,"原样复原"会把整个文件行尾翻一遍;复原后逐字节断言。
  2. 子进程一律 `-B` + `PYTHONDONTWRITEBYTECODE=1` —— `__pycache__` 按 (mtime,size) 判失效,
     等字节变异会让子进程继续跑变异版 .pyc,表现成"复原后基线仍红"。
  3. 变异存活先分诊「锁弱 vs 变异是空操作」,别直接判锁弱。
  4. **多行锚点必须按文件真实行尾对齐**:本仓工作树是 CRLF,而源码里手写的锚点是 LF,
     于是跨行锚点恒 0 命中 —— 本单第一轮 M08 就是这么"存活"的,分诊出来才知道是
     锚点没应用、不是锁弱。`_align_eol()` 把锚点的行尾对齐到文件的行尾再匹配。

用法:
    TEST_DATABASE_URL=... python tests/mutation_runner_title_year_2026_08_08.py
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCKS = (
    "tests/test_c5_title_year_rules_2026_07_28.py "
    "tests/test_title_element_contract_2026_08_08.py"
).split()

CONTRACT = "writing/title_element_contract.py"
TOPICGEN = "writing/keyword_topic_generator.py"
STYLE = "writing/article_style_contract.py"
CANON = "writing/templates/canonical_family_templates.py"
REGISTRY = "services/article_experiment_registry.py"
WRITER = "writing/article_writer.py"


# (编号, 说明, 目标文件, 原文, 替换, 应当变红的用例 -k 表达式)
MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    # ---------------- 口径类 ----------------
    (
        "M01",
        "把榜单卡年份率改到门槛以下(59.06 → 39.0)—— 榜单族应当掉出 on",
        CONTRACT,
        '"year": 59.06, "region": 22.91, "industry_marker": 71.11,',
        '"year": 39.00, "region": 22.91, "industry_marker": 71.11,',
        "three_states or attachment_verbatim or key_numbers or deep_spec or "
        "deterministic_fallback or evidence_first_fallback or experiment_preregistration",
    ),
    (
        "M07",
        "推荐门槛 40 → 70(与附件 threshold_contract 脱钩)",
        CONTRACT,
        "SPEC_LEVEL_RECOMMENDED_MIN: Final = 40.0",
        "SPEC_LEVEL_RECOMMENDED_MIN: Final = 70.0",
        "thresholds or levels_agree or three_states or deep_spec or deterministic_fallback",
    ),
    (
        "M09",
        "主卡排序前提被破坏(案例族两张卡写成 N 升序)—— R-A 失效",
        CONTRACT,
        '"case_data_roi", ("case_study", "data_report"),',
        '"case_data_roi", ("data_report", "case_study"),',
        "rule_a_primary_card or self_audit or three_states",
    ),
    (
        "M10",
        "代理映射抬高默认(other 残余桶年份率 19.92 → 45.0)",
        CONTRACT,
        '"year": 19.92, "region": 13.13, "industry_marker": 53.74,',
        '"year": 45.00, "region": 13.13, "industry_marker": 53.74,',
        "proxy_mapping or attachment_verbatim or self_audit",
    ),
    (
        "M13",
        "年份写死 2026(跨年必翻车 —— 仓里 config.py / v9 都栽过)",
        CONTRACT,
        "    return datetime.now().year",
        "    return 2026",
        "year_is_dynamic",
    ),
    # ---------------- 接线类(本包主病) ----------------
    (
        "M02",
        "深档榜单规格把年份句写死回旧文案(不读合同)—— **改动前的原形态**",
        CANON,
        "；{_ranking_title_year_clause()}；",
        "；**默认不含年份（年度主题除外，且不作开头）**；",
        "deep_spec",
    ),
    (
        "M03",
        "选题 prompt 手抄一段等价年份文案(不读合同渲染)—— 抄一份就会再漂移",
        TOPICGEN,
        "{title_year_rule_block}",
        "- 榜单族默认带当年年份;其余文体默认不写年份,且一律不作标题开头。",
        "year_rule_block_is_rendered_by_contract",
    ),
    (
        "M04",
        "兜底主表给一个 off 族(证据型问答)手写回年份 —— 与合同当场不一致",
        TOPICGEN,
        'f"{{kw}}怎么判断好不好？常见问题一次说清",',
        'f"{{kw}}怎么判断好不好？{year}年常见问题一次说清",',
        "deterministic_fallback",
    ),
    (
        "M05",
        "兜底主表把年份挪回标题开头(违反硬规则④)",
        TOPICGEN,
        'f"{{kw}}怎么选？{_y_cmp}这 3 点最容易踩坑",',
        'f"{_y_cmp}{{kw}}怎么选？这 3 点最容易踩坑",',
        "deterministic_fallback",
    ),
    (
        "M06",
        "evidence-first 硬兜底恢复旧口径(指南带年份、榜单不带)—— 与语料方向相反",
        STYLE,
        '"multi_brand_comparison": f"{subject}服务商怎么比较？{y}同口径证据与适用场景",',
        '"multi_brand_comparison": f"{subject}服务商怎么比较？同口径证据与适用场景",',
        "evidence_first_fallback",
    ),
    (
        "M08",
        "兜底年份片段不读合同,改成恒返回年份(接线断成常量)",
        CONTRACT,
        "    if not deterministic_year_default(code):\n        return \"\"",
        "    if False:\n        return \"\"",
        "deterministic_fallback or conditional_takes_the_conservative_side",
    ),
    (
        "M11",
        "实验登记表改回手敲字面量(与合同靠命名巧合对齐,改值即漂移)",
        REGISTRY,
        '    TITLE_YEAR_EXPERIMENT_DIMENSION,',
        '    "title_elements_hardcoded",',
        "experiment_preregistration",
    ),
    (
        "M14",
        "S1 软检查改回不分文体(对五族的正确产物恒报警 —— 改动前的原形态)",
        WRITER,
        r"    if _year_expected and not re.search(r'20\d{2}\s*年?', title_region):",
        r"    if not re.search(r'20\d{2}\s*年?', title_region):",
        "s1_soft_check",
    ),
    (
        "M12",
        "要素表删掉「不是勾选清单」那句 —— 四件强塞防线失守(工单 P2-2)",
        CONTRACT,
        '        "🔴 **这是默认值不是勾选清单**:',
        '        "🔴 **四件套要素尽量凑齐**:',
        "explainable_not_a_checklist or contract_exposes_no_blocking_api",
    ),
]


def _align_eol(fragment: str, text: str) -> str:
    """把锚点/替换文本的行尾对齐到目标文件的行尾(坑 4)。"""
    if "\r\n" in text and "\r\n" not in fragment:
        return fragment.replace("\n", "\r\n")
    return fragment


def _run(expr: str) -> tuple[bool, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # 坑 2
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *LOCKS, "-q", "--no-header",
         "-p", "no:randomly", "-k", expr],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=1800,
    )
    return proc.returncode == 0, (proc.stdout or "")[-600:]


def main() -> int:
    if not os.environ.get("TEST_DATABASE_URL"):
        print("[ABORT] 需要 TEST_DATABASE_URL(conftest 在 import 业务代码前就要它)")
        return 2

    originals = {rel: (ROOT / rel).read_bytes() for rel in {m[2] for m in MUTATIONS}}

    covered = " or ".join(sorted({m[5] for m in MUTATIONS}))
    green, tail = _run(covered)
    if not green:
        print("[ABORT] 基线就是红的,变异结论无意义\n" + tail)
        return 2
    print(f"✅ 基线绿(被覆盖的用例集)\n")

    killed, survived = 0, []
    for tag, desc, rel, old, new, expr in MUTATIONS:
        path = ROOT / rel
        text = originals[rel].decode("utf-8")
        old = _align_eol(old, text)
        new = _align_eol(new, text)
        hits = text.count(old)
        if hits != 1:
            # 坑 3 的第一档分诊:锚点没唯一命中 = 变异根本没应用,不是"锁弱"。
            print(f"⚠️  {tag} 锚点命中 {hits} 次(预期 1)—— 变异未应用,记作存活")
            survived.append((tag, desc, f"锚点命中 {hits} 次,变异未应用"))
            continue
        mutated = text.replace(old, new, 1)
        if mutated == text:
            print(f"⚠️  {tag} 变异后与原文逐字相同 —— 空操作,记作存活")
            survived.append((tag, desc, "变异是空操作"))
            continue
        path.write_bytes(mutated.encode("utf-8"))  # 坑 1
        try:
            ok, tail = _run(expr)
        finally:
            path.write_bytes(originals[rel])
            assert path.read_bytes() == originals[rel], f"{rel} 复原失败(逐字节)"
        if ok:
            print(f"❌ {tag} 存活:{desc}\n{tail}")
            survived.append((tag, desc, "锁未转红"))
        else:
            killed += 1
            print(f"✅ {tag} 被杀:{desc}")

    # 复原后基线必须仍绿(证明整轮没把树改脏)
    green_again, tail = _run(covered)
    print()
    print(f"复原后基线:{'绿' if green_again else '红 ← 树被改脏了'}")
    if not green_again:
        print(tail)
    print(f"杀死 {killed}/{len(MUTATIONS)}")
    for tag, desc, why in survived:
        print(f"  存活 {tag}({why}):{desc}")
    return 0 if (killed == len(MUTATIONS) and green_again) else 1


if __name__ == "__main__":
    raise SystemExit(main())
