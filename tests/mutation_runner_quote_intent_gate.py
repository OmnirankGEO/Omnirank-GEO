"""变异测试:证明 test_quote_intent_gate_2026_08_04.py 的每条断言都有判别力。

## 为什么必须有这个

只有正向断言的测试分不清"闸生效"与"判据恒真"。变异测试的做法是:
把被测源码故意改坏一处 → 测试**必须变红**。改坏了还全绿 = 那条断言是摆设。

## 判红绿只看 pytest 返回码,不解析输出

2026-08-04 的教训:上一轮变异 13/14 报"没杀",真因是给 pytest 传了
`-rf` 和 `-rE` 两个 flag,后者覆盖前者 → FAILED 一条都不打印 →
按输出解析出的"红集"恒空 → 每个变异都被判成"没杀"。
返回码没有这个歧义:0=全绿,非0=有红。

跑法:  python tests/mutation_runner_quote_intent_gate.py
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "services" / "quote_intent_gate.py"
TESTS = ROOT / "tests" / "test_quote_intent_gate_2026_08_04.py"

# (名字, 原文片段, 替换成)  —— 每条都是"把闸改坏"的一种方式
MUTATIONS = [
    ("闸从不剔词(还原事故本身)",
     "            if _policy_excluded(decision):",
     "            if False:"),
    ("闸把所有词都剔掉",
     "            if _policy_excluded(decision):",
     "            if True:"),
    ("品牌词豁免失效",
     "        if kw in protected:",
     "        if False:"),
    ("引擎不可用时假装核验过",
     "        return list(keywords), [], GATE_UNAVAILABLE",
     "        return list(keywords), [], GATE_ACTIVE"),
    ("单词判定抛错就丢词",
     "        except Exception as exc:  # 单词判定异常 → 不误杀\n"
     "            print(f\"  [报价意图闸] 「{kw}」判定异常({exc})· 按可报价处理\")\n"
     "        quotable.append(kw)",
     "        except Exception as exc:  # 单词判定异常 → 不误杀\n"
     "            print(f\"  [报价意图闸] 「{kw}」判定异常({exc})· 按可报价处理\")\n"
     "            continue\n"
     "        quotable.append(kw)"),
    ("原因文案直接吐工程术语",
     "    return POLICY_EXCLUDE_REASON_TEXT.get(intent_type, POLICY_EXCLUDE_REASON_FALLBACK)",
     "    return intent_type"),
    ("markdown 永远为空(静默缺词)",
     "    if not policy_excluded:\n        return \"\"",
     "    if True:\n        return \"\""),
    ("没剔词也硬挂一段 markdown",
     "    if not policy_excluded:\n        return \"\"",
     "    if False:\n        return \"\""),
    ("降级时不再明示未核验",
     "    if gate_status == GATE_UNAVAILABLE:",
     "    if False:"),
    ("建议区把知识题也推给代理",
     "            if _policy_excluded(decision):\n                continue\n"
     "        except Exception:\n            continue  # 判不出来的不推荐(建议区宁缺勿滥)",
     "            pass\n"
     "        except Exception:\n            continue  # 判不出来的不推荐(建议区宁缺勿滥)"),
    ("建议区重复推已经用掉的词",
     "        if not text or text in used:",
     "        if not text:"),
    ("被剔词漏进计价明细",
     "    pricing_data[\"excluded_keywords\"] = excluded",
     "    pricing_data[\"excluded_keywords\"] = excluded\n"
     "    pricing_data.setdefault(\"keywords\", []).extend(excluded)"),
    ("剔了词却不给可补的商业词",
     "    if excluded:\n        quoted_now =",
     "    if False:\n        quoted_now ="),
    ("零可报词时静默放行(落 ¥0 报价单)",
     "    if scored or not (engine_output or {}).get(\"policy_excluded_keywords\"):\n        return None",
     "    return None\n    if scored or not (engine_output or {}).get(\"policy_excluded_keywords\"):\n        return None"),
    ("还有可报词也强行阻断",
     "    if scored or not (engine_output or {}).get(\"policy_excluded_keywords\"):\n        return None",
     "    if not (engine_output or {}).get(\"policy_excluded_keywords\"):\n        return None"),
]


def run_tests() -> int:
    """返回 pytest 退出码。0=全绿。"""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", str(TESTS),
         "-q", "--no-header", "--noconftest", "-p", "no:cacheprovider", "--tb=no"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode


def main() -> int:
    original = TARGET.read_text(encoding="utf-8")

    # ---- 反向对照:不改代码时必须全绿。不绿就说明基线坏了,后面的"杀"全无意义 ----
    print("[基线] 不施加任何变异 …", end=" ", flush=True)
    if run_tests() != 0:
        print("❌ 基线就是红的 —— 先修测试,变异结果不可信")
        return 1
    print("✅ 全绿")

    survived, killed = [], []
    try:
        for name, old, new in MUTATIONS:
            if old not in original:
                print(f"[跳过] {name} —— 锚点串在源码里找不到(源码改过?锚点该更新)")
                survived.append(f"{name}(锚点失效)")
                continue
            TARGET.write_text(original.replace(old, new, 1), encoding="utf-8")
            rc = run_tests()
            if rc != 0:
                print(f"[杀死] {name}")
                killed.append(name)
            else:
                print(f"[存活] ⚠️ {name} —— 改坏了测试还全绿,这条没有断言守着")
                survived.append(name)
    finally:
        TARGET.write_text(original, encoding="utf-8")

    # ---- 复原自检:恢复后必须仍然全绿(证明上面的 finally 真的把文件还原了)----
    print("[复原] 还原源码后重跑 …", end=" ", flush=True)
    if run_tests() != 0:
        print("❌ 还原失败,源码可能已被变异污染!")
        return 1
    print("✅ 全绿")

    print(f"\n杀死 {len(killed)}/{len(MUTATIONS)}")
    if survived:
        print("存活(= 没有断言守着,需要补测试):")
        for s in survived:
            print(f"  - {s}")
        return 1
    print("✅ 全部变异都被杀死 —— 每条断言都有判别力")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
