#!/usr/bin/env python3
"""变异验证 · P3 文体规格卡(WO_P2P3_ARTICLE_STYLE_DEV 补章 R2/R3/R5)。

每条变异 = "这个包的某一部分被撤掉 / 写歪"的具体形态,跑一遍被它覆盖的锁**必须变红**。

三类变异:
  · **口径类**:改附件数字 / 改门槛 / 自己按 p_value 重判附卡 —— 抓"结论是推导还是手填";
  · **边界类**:把字数目标/标题要素/配图位塞进规格卡 —— 抓三条领地边界;
  · **接线类**:规格卡算出来但没拼进 prompt、只给榜单族、引擎别名被删 ——
    抓本包最容易"看起来做了其实没生效"的部分(别名那条是被自己的锁逼出来的真 bug)。

🔴 Windows 四坑已规避(与 P2 runner 同一套):字节读写 / `-B` 禁 pyc /
   存活先分诊 / **多行锚点按文件真实行尾对齐**(`_align_eol`)。

用法:
    TEST_DATABASE_URL=... python tests/mutation_runner_spec_cards_2026_08_08.py
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCKS = ["tests/test_article_type_spec_cards_2026_08_08.py"]

SPEC = "writing/article_type_spec_cards.py"
CANON = "writing/templates/canonical_family_templates.py"
GEN = "writing/article_generator_service.py"
COMMON = "writing/templates/common_rules.py"
REG = "writing/style_registry.py"
CFG = "writing/config.py"
SAN = "writing/body_internal_marker_sanitizer.py"


# (编号, 说明, 目标文件, 原文, 替换, 应当变红的用例 -k 表达式)
MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    # ---------------- 口径类 ----------------
    (
        "M01", "改附件数字(榜单小标题 97.62 → 60.0)—— 必备级掉成推荐",
        SPEC,
        '{"has_headings": 97.62, "has_list": 60.30, "has_comparison_table": 6.80,',
        '{"has_headings": 60.00, "has_list": 60.30, "has_comparison_table": 6.80,',
        "attachment_verbatim or structure_rates_match or self_audit",
    ),
    (
        "M02", "改实体数目标区间(P75 10 → 6)—— 补章 R2 的 5-10 被改掉",
        SPEC,
        "        5.0, 5.0, 10.0, 752,",
        "        5.0, 5.0, 6.0, 752,",
        "entity_target or spec_cards_match_the_attachment",
    ),
    (
        "M03", "附卡自己按 p_value 重判(丢掉一行 addendum=yes)—— 第二套判定逻辑",
        SPEC,
        '    EngineAddendum("ranking", "qwen", 401, "has_list", 78.55, 60.30, 18.25),\n',
        "",
        "engine_addenda_equal_the_attachment",
    ),
    (
        "M04", "标题类附卡混进正文附卡(丢弃留痕被删)—— 丢弃变成静默藏数据",
        SPEC,
        '    ("comparison", "qwen", "title_has_year", -26.97),\n',
        "",
        "engine_addenda_equal_the_attachment",
    ),
    # ---------------- 边界类 ----------------
    (
        "M05", "把字数目标写进规格卡(踩篇幅合同领地 = 第二套数字)",
        SPEC,
        '        f">={SPEC_LEVEL_RECOMMENDED_MIN:g}% 推荐 / 其余可选。"',
        '        f">={SPEC_LEVEL_RECOMMENDED_MIN:g}% 推荐 / 其余可选。"\n'
        '        f"全文不得低于 {card.body_chars_p75} 字的目标篇幅。"',
        "never_sets_a_length_target or self_audit",
    ),
    (
        "M06", "规格卡开始劝配图位(绕过配图闸,从写作侧造 [NEED_IMAGE])",
        SPEC,
        'PROMPT_EXCLUDED_FEATURES: Final[tuple[str, ...]] = ("has_image_reference",)',
        "PROMPT_EXCLUDED_FEATURES: Final[tuple[str, ...]] = ()",
        "never_pushes_image_slots or self_audit",
    ),
    (
        # 🔴 这条变异写歪了**两次**,两次都是空操作,靠"存活先分诊"才没冤枉锁:
        #   第一版:注入点在 import 之前 → 被 import 当场覆盖;
        #   第二版:在 import 之后重绑 `SPEC_LEVEL_REQUIRED_MIN` → 但分级是
        #           `level_for_rate()` 在 **P2 模块里**算的,重绑 P3 的名字它根本不看。
        # 真正的"脱钩"形态只有一种:P3 自己另起一套分级函数。
        "M07", "分级门槛脱钩(P3 自己另写一套 level_for_rate,与 P2 合同脱钩)",
        SPEC,
        'ARTICLE_SPEC_CARD_VERSION: Final = "geo-article-spec-card-v1.0"',
        'ARTICLE_SPEC_CARD_VERSION: Final = "geo-article-spec-card-v1.0"\n'
        "\n\ndef level_for_rate(rate):  # 变异:P3 自己另起一套分级(与 P2 脱钩)\n"
        "    v = float(rate or 0.0)\n"
        "    return 'required' if v >= 40.0 else ('recommended' if v >= 20.0 else 'optional')\n",
        "structure_rates_match or self_audit or thresholds_are_reused",
    ),
    # ---------------- 接线类(本包最容易假绿的地方) ----------------
    (
        "M08", "规格卡算出来但不拼进 prompt(**看起来做了其实没生效**)",
        GEN,
        '                system_prompt = system_prompt + "\\n\\n" + _spec_card_block',
        "                pass  # 规格卡算了但没用",
        "actually_reaches_the_body_prompt",
    ),
    (
        "M09", "规格卡只给榜单族(9 张卡缩成 1 张)",
        GEN,
        "        _family_code_for_spec = family_for_style(style_code)",
        '        _family_code_for_spec = (\n'
        '            "multi_brand_comparison"\n'
        '            if family_for_style(style_code) == "multi_brand_comparison" else None\n'
        '        )\n'
        '        if _family_code_for_spec is None:\n'
        '            _family_code_for_spec = "multi_brand_comparison"',
        "actually_reaches_the_body_prompt",
    ),
    (
        "M10", "删引擎别名表(dashscope→qwen)—— qwen 附卡被**静默**筛光",
        SPEC,
        'ENGINE_NAME_ALIASES: Final[dict[str, str]] = {"dashscope": "qwen"}',
        "ENGINE_NAME_ALIASES: Final[dict[str, str]] = {}",
        "platform_engine_names_are_aliased",
    ),
    (
        "M11", "渲染器把统计数字抄回 canonical(违反补章 R3 的分工)",
        CANON,
        "    from writing.article_type_spec_cards import build_spec_card_prompt\n",
        "    _ = 752  # 每实体字数\n    from writing.article_type_spec_cards import build_spec_card_prompt\n",
        "renderer_holds_no_statistics",
    ),
    (
        # 🔴 第一版只换了标题行,规则本体还在 = 空操作(锁当然不红)。改成真删整段。
        "M12", "winner_style 退回旧口径(删掉候选集/分场景默认与唯一第一的门槛)",
        CANON,
        "【组织方式默认（被引语料 N=5,753：候选集并列 37.98% / 分场景推荐 11.47% / 唯一第一 48.65%）】\n"
        "默认用**候选集并列**或**分场景推荐**二选一；**唯一第一只在有真实、可独立核验的外部事实\n"
        "支撑时才写**（如公开榜单名次、监管公示、可查证的行业统计），并当场写清该事实的来源、\n"
        "口径与时点。拿不出这种外部事实就不写唯一第一——注意语料里唯一第一占比高，\n"
        "**那是别人怎么写，不是我们可以无依据照做的理由**。\n",
        "",
        "winner_style",
    ),
    (
        "M13", "版本门禁失效(版本对不上也让覆盖生效)",
        SPEC,
        "            and versions.get(corpus_type) == ARTICLE_SPEC_CARD_VERSION",
        "            and True",
        "version_gate_constructed_shape",
    ),
    # ---------------- 年份口径收编 ----------------
    (
        "M14", "common_rules 又自己手抄一份年份口径",
        COMMON,
        '    "{_TITLE_YEAR_POSITION_RULE}", build_title_year_position_rule(),',
        '    "{_TITLE_YEAR_POSITION_RULE}", "- 标题可以带当前年份(30.7%/42.1%)。",',
        "body_side_year_rules",
    ),
    (
        "M15", "style_registry 恢复第三种判据",
        REG,
        "    _TITLE_YEAR_POSITION_RULE = build_title_year_position_rule()",
        '    _TITLE_YEAR_POSITION_RULE = (\n'
        '        "仅当主题确有时效性且 Evidence Pack 含对应时间证据时才在标题写年份"\n'
        "    )",
        "body_side_year_rules",
    ),
    (
        "M16", "config 年份写回死值(跨年静默过期)",
        CFG,
        '    "years": _year_strategy(),',
        '    "years": {"main": "2026年", "review": "2025年"},',
        "config_year_strategy_is_dynamic",
    ),
    # ---------------- 清洗器 ----------------
    (
        "M17", "清洗器不认规格卡时代的编制口径(内部口径漏进客户可见正文)",
        SAN,
        '    "已核验候选", "证据密度", "密度下限", "目标区间", "规格卡",',
        "",
        "sanitizer_strips_spec_card_era",
    ),
    (
        "M18", "覆盖率下限又写回一个独立字面量(同值不同源,早晚漂移)",
        CANON,
        "BUDGET_COVERAGE_FLOOR: Final = _coverage_floor()",
        "BUDGET_COVERAGE_FLOOR: Final = 0.85",
        "coverage_floor_is_single_sourced",
    ),
]


def _align_eol(fragment: str, text: str) -> str:
    """把锚点/替换文本的行尾对齐到目标文件的行尾(坑 4)。"""
    if "\r\n" in text and "\r\n" not in fragment:
        return fragment.replace("\n", "\r\n")
    return fragment


def _run(expr: str) -> tuple[bool, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
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
    print("✅ 基线绿(被覆盖的用例集)\n")

    killed, survived = 0, []
    for tag, desc, rel, old, new, expr in MUTATIONS:
        path = ROOT / rel
        text = originals[rel].decode("utf-8")
        old = _align_eol(old, text)
        new = _align_eol(new, text)
        hits = text.count(old)
        if hits != 1:
            print(f"⚠️  {tag} 锚点命中 {hits} 次(预期 1)—— 变异未应用,记作存活")
            survived.append((tag, desc, f"锚点命中 {hits} 次,变异未应用"))
            continue
        mutated = text.replace(old, new, 1)
        if mutated == text:
            print(f"⚠️  {tag} 变异后与原文逐字相同 —— 空操作,记作存活")
            survived.append((tag, desc, "变异是空操作"))
            continue
        path.write_bytes(mutated.encode("utf-8"))
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
