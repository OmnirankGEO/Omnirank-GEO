"""变异检验 · 进度回调不许触发重生成(WO 回调重复计费 2026-08-08 · 资金)。

每条变异 = 「有人把这个改回去/改坏」的一种具体写法。锁必须**当场转红**。
存活的先分诊「锁写松了」还是「变异是空操作」。

跑法(需要 TEST_DATABASE_URL 指向 throwaway PG):
    python tests/mutation_runner_progress_callback_2026_08_08.py

自坏防线:清 `__pycache__` + `-p no:cacheprovider` · 锚点命中 ≠1 报 ANCHOR_BAD ·
退出码 5 单独报 NO_TESTS · 写回用原始 bytes 逐字还原。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
LOCK_SUITE = "tests/test_progress_callback_no_rebill_2026_08_08.py"
SVC = "writing/article_generator_service.py"

#: (编号, 说明, 文件, 锚点, 替换)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "M01", "主模型分支改回裸调用(= 出事那天的形状:回调炸 → 判主模型失败 → 兜底重生成)",
        SVC,
        "                    self._emit_progress(completed, total, topic['title'], 'completed')\n"
        "                    return article\n",
        "                    if self.progress_callback:\n"
        "                        self.progress_callback(completed, total, topic['title'], 'completed')\n"
        "                    return article\n",
    ),
    (
        "M02", "兜底分支改回裸调用(改一处漏一处的形状)",
        SVC,
        "                        self._emit_progress(completed, total, topic['title'], 'completed')\n"
        "                        print(f\"    ✅ 兜底模型生成成功",
        "                        if self.progress_callback:\n"
        "                            self.progress_callback(completed, total, topic['title'], 'completed')\n"
        "                        print(f\"    ✅ 兜底模型生成成功",
    ),
    (
        "M03", "失败态回调改回裸调用",
        SVC,
        "                self._emit_progress(completed, total, topic['title'], 'failed')\n",
        "                if self.progress_callback:\n"
        "                    self.progress_callback(completed, total, topic['title'], 'failed')\n",
    ),
    (
        "M04", "_emit_progress 静默吞掉(不留痕 —— 下次没人查得出来)",
        SVC,
        "            print(\n                f\"    ⚠️ 进度回调异常(已忽略,不触发重生成) \"",
        "            pass\n            _unused = (\n                f\"    ⚠️ 进度回调异常(已忽略,不触发重生成) \"",
    ),
    (
        "M05", "_emit_progress 变空函数(回调永远不被调用 —— 进度条从此不动)",
        SVC,
        "        callback = self.progress_callback\n        if not callback:\n            return\n",
        "        callback = self.progress_callback\n        if True:\n            return\n",
    ),
    (
        "M06", "_emit_progress 把异常再抛出去(等于没吞)",
        SVC,
        "        except Exception as exc:  # noqa: BLE001 - 进度回调炸了不该让已出的文章作废\n",
        "        except Exception as exc:  # noqa: BLE001\n            raise\n",
    ),
    (
        "M07", "去掉「保存失败不许买第二次供应商调用」那道既有守卫(顺手改坏别的)",
        SVC,
        "                if has_fallback and not isinstance(last_error, ArticleSaveFailed):\n",
        "                if has_fallback:\n",
    ),
]


def _purge_pycache() -> None:
    for path in ROOT.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)


def _run_locks() -> str:
    _purge_pycache()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:randomly",
         "-p", "no:cacheprovider", LOCK_SUITE],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if proc.returncode == 5:
        return "NO_TESTS"
    return "KILLED" if proc.returncode != 0 else "SURVIVED"


def main() -> int:
    print("── 基线自检:未变异时锁必须全绿 ──")
    if _run_locks() != "SURVIVED":
        print("[ERR ] 基线就不是绿的 → 变异结果无意义,先修基线")
        return 2
    print("   OK 基线全绿\n")

    killed = bad = 0
    for code, note, rel, anchor, replacement in MUTATIONS:
        target = ROOT / rel
        original = target.read_bytes()
        text = original.decode("utf-8")
        hits = text.count(anchor)
        if hits != 1:
            print(f"{code}  [BAD ] ANCHOR_BAD  {note}(锚点命中 {hits} 次,应为 1)")
            bad += 1
            continue
        try:
            with open(target, "w", encoding="utf-8", newline="") as h:
                h.write(text.replace(anchor, replacement))
            verdict = _run_locks()
        finally:
            target.write_bytes(original)
        if verdict == "KILLED":
            killed += 1
        icon = {"KILLED": "[KILL]", "SURVIVED": "[LIVE]", "NO_TESTS": "[NONE]"}[verdict]
        print(f"{code}  {icon} {verdict:9} {note}")

    total = len(MUTATIONS)
    print(f"\n变异 {total} 条 · KILLED {killed} · ANCHOR_BAD {bad} · 其余 {total - killed - bad}")
    if killed != total:
        print("🔴 有变异没被杀死 —— 先分诊「锁写松了」还是「变异是空操作」")
        return 1
    print("✅ 全部 KILLED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
