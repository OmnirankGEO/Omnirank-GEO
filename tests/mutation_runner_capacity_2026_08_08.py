"""变异 runner:证明 P1 容量合同的锁**有判别力**(2026-08-08)

跑法(仓库根目录):
    python tests/mutation_runner_capacity_2026_08_08.py

每个变异 = 往源码里注入一处**真实可能发生的退化**,然后要求指定的锁转红。
- 变异存活(锁仍全绿)= 该锁是摆设,当场打印它打偏在哪。
- 另配 3 条**反向对照**(不注入任何东西 / 注入无关改动),要求锁保持绿 ——
  单向断言证明不了判别力,恒红和恒真一样废。

Windows 三坑(2026-08-08 实测过,别再踩):
  ① 读写一律 newline='' + 原样回写,否则 CRLF↔LF 翻转会让"没变异"的文件也 diff;
  ② 写回后必须**清 __pycache__**,否则等字节长度的变异会被 pyc 时间戳骗过去;
  ③ 变异存活先分诊:是"锁弱"还是"这处变异其实是空操作"(注入前后字节相同 → 判据本身没生效)。
"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认 GBK,打印 ✅/🔴 会 UnicodeEncodeError 把整轮变异跑崩
# (2026-08-08 实测)。判据的输出不许因为终端编码而丢失。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]

CONTRACT_TEST = "tests/test_article_capacity_contract_2026_08_08.py"
META_TEST = "tests/test_article_count_meta_lock_2026_08_08.py"


class Mutation:
    def __init__(self, name, path, old, new, expect_red, why):
        self.name = name
        self.path = ROOT / path
        self.old = old
        self.new = new
        self.expect_red = expect_red      # 期望转红的测试文件(或 node id)
        self.why = why


MUTATIONS = [
    Mutation(
        "M1 容量上限退化成「必须完成量」",
        "services/article_capacity_contract.py",
        '    remaining = max(0, authorized - consumed - reserved)',
        '    remaining = max(0, authorized - reserved)',
        CONTRACT_TEST,
        "已交付不再扣减可用容量 → 372 那种「已用满」的单会永远显示还有额度",
    ),
    Mutation(
        "M2 超额被静默抹平",
        "services/article_capacity_contract.py",
        '    over_delivered = max(0, consumed - authorized)',
        '    over_delivered = 0',
        CONTRACT_TEST,
        "生产 12/137 张单的历史超额被藏起来,对账时看不见",
    ),
    Mutation(
        "M3 未付款也放行容量",
        "services/article_capacity_contract.py",
        '    if not quote_is_payable:',
        '    if False:',
        CONTRACT_TEST,
        "未付款单会出现「去写这篇」 —— P4 合同 §12 判据 #10 点名的那条",
    ),
    Mutation(
        "M4 显式 0 篇又被当假值吃掉",
        "tools/pricing_bands.py",
        '    if raw is None:\n        return max(0, int(when_missing))',
        '    if not raw:\n        return max(0, int(when_missing))',
        CONTRACT_TEST,
        "`or 1` 那个真 bug 复活:覆盖词 0 篇被抬成 1 篇(生产 218 行受影响)",
    ),
    Mutation(
        "M5 兜底阶梯偷偷回到 legacy 1/2/3",
        "tools/pricing_bands.py",
        '    mode = str(os.getenv(_FALLBACK_LADDER_ENV) or "").strip() or ARTICLE_LADDER_MAIN',
        '    mode = ARTICLE_LADDER_LEGACY_FALLBACK',
        CONTRACT_TEST,
        "兜底词篇数默认值被改回上线前口径(对外价格语义变化)",
    ),
    Mutation(
        "M6 品牌词阶梯被擅自改成主合同",
        "tools/pricing_bands.py",
        '_BRAND_KEYWORD_TIER_ARTICLES = {\n    "entry":    1,',
        '_BRAND_KEYWORD_TIER_ARTICLES = {\n    "entry":    5,',
        CONTRACT_TEST,
        "品牌词是否豁免阶梯归 Owner 拍板,代码里自裁 = 越权定价",
    ),
    Mutation(
        "M7 容量占用改成按 articles 计",
        "services/article_capacity_contract.py",
        '    SELECT COUNT(*) AS consumed FROM topics WHERE quote_id = %s',
        '    SELECT COUNT(*) AS consumed FROM articles WHERE quote_id = %s',
        CONTRACT_TEST,
        "重写草稿被算成容量占用 → 372 会算出 -121 的可用容量",
    ),
    Mutation(
        "M8 加入报价评估顺手挪了冻结指针",
        "services/quote_pricing_snapshot.py",
        '        # 🔴 刻意不写 quotes.active_pricing_snapshot_id / 不写 session.pricing_data:',
        '        cursor.execute("UPDATE quotes SET active_pricing_snapshot_id=%s WHERE id=%s",\n'
        '                       (snapshot["id"], quote_id))\n'
        '        # 🔴 刻意不写 quotes.active_pricing_snapshot_id / 不写 session.pricing_data:',
        CONTRACT_TEST,
        "客户已冻结的报价被评估请求顶掉 = 历史快照被重算(资金红线)",
    ),
    Mutation(
        "M9 候选夹带价格改成静默忽略",
        "services/quote_pricing_snapshot.py",
        '        leaked = sorted(key for key in source.keys() if key in banned)\n        if leaked:',
        '        leaked = []\n        if leaked:',
        CONTRACT_TEST,
        "前端塞进来的价格/篇数不再被拒 = 前端算钱",
    ),
    Mutation(
        "M10 元判据:往真实源码里塞第二套阶梯",
        META_TEST,
        '    L1_KNOWN_ORPHANS = ',   # 占位,真实注入在下方 _apply 里特判
        None,
        META_TEST,
        "第二套阶梯只要写进任意后端文件,元判据必须当场转红",
    ),
    # [开源 E3 · 前端 · 2026-10-01 · WO_322] M11 退役:锚点所在的旧 C 端 GEO 方案页整删
    Mutation(
        "M12 server.py 盲区清单被悄悄放宽",
        META_TEST,
        '    "inline_ast": 5,',
        '    "inline_ast": 99,',
        META_TEST,
        "清单数字一被人改大,等于给 server.py 开了后门 —— 数量锁必须当场转红",
    ),
]

# M10 用「往真实源码里塞一个第二套阶梯」来验元判据,而不是改白名单本身
M10_TARGET = ROOT / "services" / "article_capacity_contract.py"
M10_INJECT = '\n\n# MUTATION-PROBE\n_second_ladder_articles = {"entry": 1, "standard": 2, "flagship": 3}\n'


def _read(path: Path) -> str:
    return io.open(path, encoding="utf-8", newline="").read()


def _write(path: Path, text: str) -> None:
    io.open(path, "w", encoding="utf-8", newline="").write(text)


def _clear_pycache() -> None:
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _run(test_target: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_target, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "")[-1500:]


def _apply(mut: Mutation) -> tuple[Path, str] | None:
    """返回 (被改的文件, 原文)。注入后字节必须真的变了,否则是空操作。"""
    if mut.name.startswith("M10"):
        original = _read(M10_TARGET)
        _write(M10_TARGET, original + M10_INJECT)
        return M10_TARGET, original
    original = _read(mut.path)
    if original.count(mut.old) != 1:
        print(f"   ⚠️  锚点在 {mut.path.name} 命中 {original.count(mut.old)} 次(需恰好 1)→ 变异无法施加")
        return None
    mutated = original.replace(mut.old, mut.new)
    if mutated == original:
        print("   ⚠️  注入前后字节相同 = 空操作,这条变异证明不了任何事")
        return None
    _write(mut.path, mutated)
    return mut.path, original


def main() -> int:
    print("=" * 72)
    print("P1 容量合同 · 变异测试(判别力自证)")
    print("=" * 72)

    # ---- 反向对照 0:未变异时必须全绿,否则后面的"转红"毫无意义 ----
    _clear_pycache()
    green_contract, out = _run(CONTRACT_TEST)
    green_meta, out_meta = _run(META_TEST)
    print(f"\n反向对照 0 · 未变异基线:合同锁={'绿' if green_contract else '红'} "
          f"元判据锁={'绿' if green_meta else '红'}")
    if not (green_contract and green_meta):
        print("🔴 基线就是红的 —— 变异结果全部不可信,先修基线")
        print(out[-800:], out_meta[-800:])
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
                first_fail = next((line for line in tail.splitlines() if line.startswith("FAILED")), "")
                print(f"   ✅ 被杀  {first_fail[:110]}")
        finally:
            _write(path, original)
            _clear_pycache()

    # ---- 反向对照 1:全部还原后必须回到全绿(证明 runner 自己没弄脏工作树) ----
    back_contract, _ = _run(CONTRACT_TEST)
    back_meta, _ = _run(META_TEST)
    print(f"\n反向对照 1 · 还原后:合同锁={'绿' if back_contract else '红'} "
          f"元判据锁={'绿' if back_meta else '红'}")

    print("\n" + "=" * 72)
    print(f"杀死 {len(killed)}/{len(MUTATIONS)} · 存活 {len(survived)} · 跳过 {len(skipped)}")
    if survived:
        print("存活(锁需要补强):", survived)
    if skipped:
        print("跳过(锚点漂了,要按语义重定位):", skipped)
    print("=" * 72)
    if not (back_contract and back_meta):
        print("🔴 还原后没回到绿 —— runner 弄脏了工作树,结果不可信")
        return 2
    return 0 if not survived and not skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
