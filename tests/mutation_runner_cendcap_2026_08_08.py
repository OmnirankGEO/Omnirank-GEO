"""变异 runner:证明「C 端三档容量下发」这一包的锁**有判别力**(2026-08-08)

跑法(仓库根目录):
    python tests/mutation_runner_cendcap_2026_08_08.py

结构与 tests/mutation_runner_capacity_2026_08_08.py 一致(同一套 Windows 三坑处理):
  ① newline='' 原样回写,不让 CRLF↔LF 翻转造出假 diff;
  ② 每次跑前清 __pycache__,等字节变异骗不过 pyc;
  ③ 变异存活先分诊:锁弱 vs 空操作。

反向对照:未变异基线必须全绿 + 全部还原后必须回到全绿。
"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]

CAP_TEST = "tests/test_cend_plan_capacity_2026_08_08.py"
META_TEST = "tests/test_article_count_meta_lock_2026_08_08.py"


class Mutation:
    def __init__(self, name, path, old, new, expect_red, why):
        self.name = name
        self.path = ROOT / path
        self.old = old
        self.new = new
        self.expect_red = expect_red
        self.why = why


MUTATIONS = [
    Mutation(
        "M1 未报价的词兜底回 1 篇",
        "services/c_end_plan_capacity.py",
        "CAPACITY_WHEN_NOT_QUOTED = 0",
        "CAPACITY_WHEN_NOT_QUOTED = 1",
        CAP_TEST,
        "「没报到价」被当成「1 篇」——`or 1` 那个老 bug 换个位置复活",
    ),
    Mutation(
        "M2 显式 0 篇被抬成 1",
        "services/c_end_plan_capacity.py",
        "        return normalize_article_capacity(nested.get(\"articles\"), when_missing=CAPACITY_WHEN_NOT_QUOTED)",
        "        return normalize_article_capacity(nested.get(\"articles\") or 1, when_missing=CAPACITY_WHEN_NOT_QUOTED)",
        CAP_TEST,
        "覆盖词的 0 篇容量被吃掉(P1 语义 0..capacity 被破坏)",
    ),
    Mutation(
        "M3 幂等性丢失:读取侧覆盖生成侧",
        "services/c_end_plan_capacity.py",
        "            if field in item:\n                # 幂等:已下发过就不覆盖(也防止读取侧把生成侧的值改掉)\n                continue",
        "            if False:\n                continue",
        CAP_TEST,
        "历史快照被读取时重算 —— 08_billing §7.5 直接违反",
    ),
    Mutation(
        "M4 同名词改成后到先得",
        "services/c_end_plan_capacity.py",
        "                if name and name not in index:",
        "                if name:",
        CAP_TEST,
        "同一个词出现两次时取值不稳定,方案页刷新一次容量就变",
    ),
    Mutation(
        "M5 附赠词不再下发容量",
        "services/c_end_plan_capacity.py",
        '        for bucket in ("core_keywords", "covered_keywords"):',
        '        for bucket in ("core_keywords",):',
        CAP_TEST,
        "附赠词回落成 0 篇(生产实测 42/42 个附赠词容量都 > 0)",
    ),
    Mutation(
        "M6 strong 档漏发",
        "services/c_end_plan_capacity.py",
        'CEND_PLAN_TIERS = ("entry", "standard", "flagship", "strong")',
        'CEND_PLAN_TIERS = ("entry", "standard", "flagship")',
        CAP_TEST,
        "托管第 4 档没有容量,下游只能再自己算一份",
    ),
    Mutation(
        "M7 扁平形态不认了",
        "services/c_end_plan_capacity.py",
        "    if flat in kw_obj:",
        "    if False:",
        CAP_TEST,
        "batch_pricing 两种承载形态只认一种 → 另一条链静默 0 篇",
    ),
    Mutation(
        "M8 生产者没接线",
        "tools/c_end_cost_estimate.py",
        "    return attach_tier_article_capacity(result)",
        "    return result",
        CAP_TEST,
        "函数写对了但方案里根本没有这三个字段(今年第三次踩的坑)",
    ),
    # [开源 E3 · B3c G4 · 2026-09-28] M9(读取端点没接线)退役:C 端 GEO 方案任务读取端点所在文件整文件删除,锚点不在
    # [开源 E3 · 前端 · 2026-10-01 · WO_322] M10 / M11 / M12 退役:三处变异的锚点所在文件(旧 C 端 GEO 方案页、旧对话 UI 的方案卡)整删
    Mutation(
        "M13 价格反推判据锚点缩回 `_price`",
        "tests/test_article_count_meta_lock_2026_08_08.py",
        r'PRICE_DERIVED_RE = re.compile(r"Math\.round\([\s\S]{0,160}?[Pp]rice[\s\S]{0,160}?/\s*\d+\s*\)")',
        r'PRICE_DERIVED_RE = re.compile(r"Math\.round\([\s\S]{0,160}?_price[\s\S]{0,160}?/\s*\d+\s*\)")',
        META_TEST,
        "判据自己退化回漏检形态 —— 反向对照必须把它顶红",
    ),
    Mutation(
        "M14 非零兜底锚点缩回只认 required_articles",
        "tests/test_article_count_meta_lock_2026_08_08.py",
        r'    r"(?:required_articles|articles_needed)[^;]{0,300}?(?:\|\||\?\?)\s*([1-9]\d*)"',
        r'    r"required_articles[^;]{0,300}?(?:\|\||\?\?)\s*([1-9]\d*)"',
        META_TEST,
        "锚点退化 → `articles_needed ?? 1` 那类又能溜过去",
    ),
]


def _read(path: Path) -> str:
    with io.open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _clear_pycache() -> None:
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _run(test_target: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_target, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "")[-1500:]


def _apply(mut: Mutation):
    original = _read(mut.path)
    hits = original.count(mut.old)
    if hits != 1:
        print(f"   ⚠️  锚点在 {mut.path.name} 命中 {hits} 次(需恰好 1)→ 变异无法施加")
        return None
    mutated = original.replace(mut.old, mut.new)
    if mutated == original:
        print("   ⚠️  注入前后字节相同 = 空操作,这条变异证明不了任何事")
        return None
    _write(mut.path, mutated)
    return mut.path, original


def main() -> int:
    print("=" * 72)
    print("C 端三档容量下发 · 变异测试(判别力自证)")
    print("=" * 72)

    _clear_pycache()
    green_cap, out_cap = _run(CAP_TEST)
    green_meta, out_meta = _run(META_TEST)
    print(f"\n反向对照 0 · 未变异基线:容量下发锁={'绿' if green_cap else '红'} "
          f"元判据锁={'绿' if green_meta else '红'}")
    if not (green_cap and green_meta):
        print("🔴 基线就是红的 —— 变异结果全部不可信,先修基线")
        print(out_cap[-800:], out_meta[-800:])
        return 2

    killed, survived, skipped = [], [], []
    for mut in MUTATIONS:
        print(f"\n── {mut.name} ──\n   预期:{mut.why}")
        applied = _apply(mut)
        if applied is None:
            skipped.append(mut.name)
            continue
        path, original = applied
        try:
            _clear_pycache()
            ok, tail = _run(mut.expect_red)
            if ok:
                survived.append(mut.name)
                print("   🔴 变异存活 —— 锁没打到这里(先分诊:锁弱?还是判据打偏了?)")
            else:
                killed.append(mut.name)
                first_fail = next((l for l in tail.splitlines() if l.startswith("FAILED")), "")
                print(f"   ✅ 被杀  {first_fail[:110]}")
        finally:
            _write(path, original)
            _clear_pycache()

    back_cap, _ = _run(CAP_TEST)
    back_meta, _ = _run(META_TEST)
    print(f"\n反向对照 1 · 还原后:容量下发锁={'绿' if back_cap else '红'} "
          f"元判据锁={'绿' if back_meta else '红'}")

    print("\n" + "=" * 72)
    print(f"杀死 {len(killed)}/{len(MUTATIONS)} · 存活 {len(survived)} · 跳过 {len(skipped)}")
    if survived:
        print("存活(锁需要补强):", survived)
    if skipped:
        print("跳过(锚点漂了,要按语义重定位):", skipped)
    print("=" * 72)
    if not (back_cap and back_meta):
        print("🔴 还原后没回到绿 —— runner 弄脏了工作树,结果不可信")
        return 2
    return 0 if not survived and not skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
