#!/usr/bin/env python
"""P0 变异 runner:逐条破坏修复,门禁必须转红。

用法:
    TEST_DATABASE_URL=postgresql://geo_admin:pubtest@127.0.0.1:55640/geo_pubrec_test \
        python tests/pubrec_packages_2026_08_09/mutation_runner.py

三关:①改到字节 ②门禁转红且是点名那条 ③还原逐字节一致。
🔴 中断安全四层:finally / SIGINT+SIGTERM / atexit / 落盘备份 + 下一轮残留检测拒跑。
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = "tests/pubrec_packages_2026_08_09/"
BACKUP_DIR = ROOT / ".mutation_backup_pubrec"
REC = ROOT / "services" / "publish_recommendation.py"

_IN_FLIGHT: dict[Path, bytes] = {}


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _keep_backup(path: Path, original: bytes) -> None:
    BACKUP_DIR.mkdir(exist_ok=True)
    (BACKUP_DIR / path.name).write_bytes(original)


def _restore_all(reason: str = "") -> None:
    for path, original in list(_IN_FLIGHT.items()):
        try:
            path.write_bytes(original)
        except Exception as exc:                                   # noqa: BLE001
            print(f"!! 还原失败 {_rel(path)}: {exc}", file=sys.stderr)
        _IN_FLIGHT.pop(path, None)
        try:
            (BACKUP_DIR / path.name).unlink(missing_ok=True)
        except Exception:                                          # noqa: BLE001
            pass
    try:
        if BACKUP_DIR.exists() and not any(BACKUP_DIR.iterdir()):
            BACKUP_DIR.rmdir()
    except Exception:                                              # noqa: BLE001
        pass
    if reason:
        print(f"[已还原:{reason}]", file=sys.stderr)


def _abort_if_stale_backup() -> bool:
    if BACKUP_DIR.exists() and any(BACKUP_DIR.iterdir()):
        left = ", ".join(sorted(p.name for p in BACKUP_DIR.iterdir()))
        print(
            f"🔴 上一轮变异未正常收尾,{BACKUP_DIR.name}/ 里还留着:{left}\n"
            "   先 git diff 自查并手工恢复,确认干净后删掉该目录再跑。\n"
            "   (刻意不自动还原:上一轮状态不明,静默覆盖你的改动比拒跑更糟。)",
            file=sys.stderr,
        )
        return True
    return False


def _install_guards() -> None:
    atexit.register(_restore_all, "atexit")

    def _on_signal(signum, _frame):
        _restore_all(f"signal {signum}")
        sys.exit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass


MUTATIONS = [
    # 🔴 expect 第一版写的是"组合包非空"那条,结果 P1 存活 —— 分诊后发现不是锁弱,
    #    是**验收条件本身判别力不足**:去掉证据分修复后,price 那条修复仍能救 25 条,
    #    25 条照样凑得出 trial3/balanced5/authority8。真正对证据分敏感的是通过数那条。
    dict(id="P1", file=REC,
         expect="test_most_of_the_pool_survives_rescoring",
         task="证据分退回从 citation_rate 重算(池内蒸馏结果又被丢弃)",
         old="    if pooled_evidence is not None:\n"
             "        evidence_score = min(100.0, max(0.0, _num(pooled_evidence, 0)))",
         new="    if False:\n"
             "        evidence_score = min(100.0, max(0.0, _num(pooled_evidence, 0)))"),
    dict(id="P2", file=REC,
         expect="test_the_fixture_really_contains_both_price_shapes",
         task="价格回退判据改回 >=0(0 被当合法价,price/price1 永远走不到)",
         old="        if value > 0:\n            return value",
         new="        if value >= 0:\n            return value"),
    dict(id="P3", file=REC,
         expect="test_an_unpriced_medium_is_not_treated_as_cheap",
         task="没录价重新被打成 price_too_low(把缺失当取值)",
         old="    if price_known and price < 5:",
         new="    if price < 5:"),
    dict(id="P4", file=REC,
         expect="test_missing_price_gets_a_neutral_score_not_a_perfect_one",
         task="没录价给价格满分(奖励数据缺失)",
         old="        price_score = 50.0",
         new="        price_score = 100.0"),
    dict(id="P5", file=REC,
         expect="test_rescoring_is_still_a_filter_not_a_rubber_stamp",
         task="准入恒 True(闸拆了)",
         old="    is_recommendable = not risk_tags and effective_score >= 50",
         new="    is_recommendable = True"),
    # 🔴 真实数据里 evidence_score 最小 0.8,永远走不到 0 这一格 → 只能用构造用例验。
    dict(id="P6", file=REC,
         expect="test_a_pooled_evidence_of_zero_means_zero_not_missing",
         task="证据分改用真值判断(池内存的 0 会被当成「没有」→ 悄悄回退)",
         old="    if pooled_evidence is not None:",
         new="    if pooled_evidence:"),
    dict(id="P7", file=REC,
         expect="test_distill_side_behaviour_is_byte_for_byte_unchanged",
         task="蒸馏侧也改成读行内证据分(拿自己的输出喂自己)",
         old='        citation_rate = _num(row.get("citation_rate"), 0)',
         new='        citation_rate = _num(row.get("evidence_score"), 0)'),
]


def _run_suite() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", SUITE,
         "-q", "-p", "no:cacheprovider", "--no-header", "--tb=no", "-rf"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ),
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    if _abort_if_stale_backup():
        return 2
    if not os.environ.get("TEST_DATABASE_URL"):
        print("🔴 未设置 TEST_DATABASE_URL —— 套件会 ImportError 而不是因变异转红,拒跑。",
              file=sys.stderr)
        return 2
    _install_guards()

    green, out = _run_suite()
    if not green:
        print("🔴 基线就不是绿的,变异结果没有意义:\n" + out[-2500:], file=sys.stderr)
        return 2
    print("基线绿\n")

    killed, survived = [], []
    for mut in MUTATIONS:
        path: Path = mut["file"]
        original = path.read_bytes()
        text = original.decode("utf-8")
        hits = text.count(mut["old"])
        if hits != 1:
            survived.append((mut, f"锚点命中 {hits} 次(需恰好 1 次)—— 锚点失配,不是锁弱"))
            continue
        try:
            _IN_FLIGHT[path] = original
            _keep_backup(path, original)
            path.write_bytes(
                text.replace(mut["old"], mut["new"] + "  # MUTATION " + mut["id"]).encode("utf-8")
            )
            assert path.read_bytes() != original, "变异没有改到字节"
            ok, out = _run_suite()
            if ok:
                survived.append((mut, "套件仍然全绿"))
            elif mut["expect"] not in out:
                survived.append((mut, f"转红了但不是点名那条({mut['expect']} 未出现在失败集)"))
            else:
                killed.append(mut)
        finally:
            _restore_all()

    print("\n" + "=" * 62)
    for mut in killed:
        print(f"OK  {mut['id']} 被杀 · {mut['task']}")
    for mut, why in survived:
        print(f"XX  {mut['id']} 存活 · {mut['task']} · {why}")
    print("=" * 62)
    print(f"{len(killed)}/{len(MUTATIONS)} 条变异被杀死")
    return 0 if not survived else 1


if __name__ == "__main__":
    raise SystemExit(main())
