#!/usr/bin/env python
"""B 单变异 runner:逐条破坏行业过滤,门禁必须转红。

用法(必须带测试库):
    TEST_DATABASE_URL=postgresql://geo_admin:pubtest@127.0.0.1:55640/geo_pubfilter_test \
        python tests/pubfilter_industry_2026_08_08/mutation_runner.py

三关(与本仓既有 runner 同口径):
  1. **改到字节** —— old 串必须在文件里唯一命中,替换后字节确实变了;
  2. **门禁转红** —— 施加变异后套件必须失败,且失败的是**点名那条锁**;
  3. **还原逐字节一致** —— 跑完 `git status` 必须干净。

🔴 中断安全(四层):finally / SIGINT+SIGTERM / atexit / 落盘备份 + 下一轮残留检测拒跑。
   最坏的失败模式不是"变异存活",是**变异代码留在树里被当成实现提交** ——
   它只多一行 `# MUTATION`,肉眼极易漏。硬杀(SIGKILL)谁也挡不住,唯一能挡的是
   下一轮**拒跑**;残留时**不自动还原**,因为上一轮状态不明,静默覆盖别人的树更糟。
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = "tests/pubfilter_industry_2026_08_08/"
BACKUP_DIR = ROOT / ".mutation_backup_pubfilter"

PUBLISH_DB = ROOT / "db" / "publish_db.py"
PUBLISH_API = ROOT / "api" / "publish_api.py"

_IN_FLIGHT: dict[Path, bytes] = {}


# ────────────────────────── 还原机制 ──────────────────────────

def _rel(path: Path) -> str:
    """仓外路径不抛 —— 还原流程里任何一步抛异常都会把"还原"本身搞砸。"""
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
    """上一轮留下的备份 = 那一轮没正常收尾 = 工作树状态不明 → 拒跑。"""
    if BACKUP_DIR.exists() and any(BACKUP_DIR.iterdir()):
        left = ", ".join(sorted(p.name for p in BACKUP_DIR.iterdir()))
        print(
            f"🔴 上一轮变异未正常收尾,{BACKUP_DIR.name}/ 里还留着:{left}\n"
            "   工作树可能带着变异代码。请先 `git diff` 自查并手工恢复,\n"
            "   确认干净后删掉该目录再跑。(本 runner 刻意**不自动还原**:\n"
            "    上一轮状态不明,静默覆盖你的改动比拒跑更糟。)",
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


# ────────────────────────── 变异清单 ──────────────────────────

MUTATIONS = [
    # ── db/publish_db.py:分档本体 ──
    dict(id="M1", file=PUBLISH_DB, expect="test_empty_industry_candidates_do_not_leak",
         task="精确档条件改回旧口径(空行业候选重新穿透)",
         old='industry_cond="p.industry ILIKE %s",',
         new='industry_cond="(p.industry ILIKE %s OR COALESCE(p.industry, \'\') = \'\')",'),
    dict(id="M2", file=PUBLISH_DB, expect="test_a_restaurant_query_never_surfaces_car_media_on_the_fallback",
         task="兜底档放整池(从后门把洞再开一次)",
         old='industry_cond="COALESCE(p.industry, \'\') = \'\'",',
         new='industry_cond="TRUE",'),
    dict(id="M3", file=PUBLISH_DB, expect="test_empty_industry_candidates_do_not_leak",
         task="精确档结果被丢弃,永远走兜底",
         old="        if rows:\n            return rows",
         new="        if False:\n            return rows"),
    dict(id="M4", file=PUBLISH_DB, expect="test_fallback_tier_is_reachable_and_marked",
         task="兜底档谎称自己是精确命中",
         old='                industry_match="FALSE",',
         new='                industry_match="TRUE",'),
    dict(id="M5", file=PUBLISH_DB, expect="test_industry_is_stripped_before_matching",
         task="行业串不 strip(前端常带空格)",
         old='        industry = (industry or "").strip()',
         new='        industry = (industry or "")'),
    dict(id="M6", file=PUBLISH_DB, expect="test_no_industry_query_still_returns_the_whole_pool",
         task="无行业查询也被判成「不匹配」",
         old='                    industry_match="TRUE",\n                    industry_cond="TRUE",',
         new='                    industry_match="FALSE",\n                    industry_cond="TRUE",'),
    dict(id="M7", file=PUBLISH_DB, expect="test_fallback_tier_is_reachable_and_marked",
         task="兜底档直接返空(把「推错行业」换成「什么都没有」)",
         # 🔴 锚点必须唯一:`return [dict(r) for r in cursor.fetchall()]\n    finally:` 这个
         #    收尾形状在 publish_db.py 里出现 11 次(第一版就是这么失配的)。用兜底档
         #    独有的 `(limit,),` + 8 空格缩进锁定最后那一段。
         old="            (limit,),\n        )\n        return [dict(r) for r in cursor.fetchall()]",
         new="            (limit,),\n        )\n        return []"),
    # ── api/publish_api.py:接线 ──
    dict(id="M8", file=PUBLISH_API, expect="test_matched_tier_says_nothing",
         task="提示条件取反(精确命中时反而提示)",
         old="        and not any(c.get(\"industry_match\") for c in candidates)",
         new="        and any(c.get(\"industry_match\") for c in candidates)"),
    dict(id="M9", file=PUBLISH_API, expect="test_existing_notice_is_not_clobbered",
         task="兜底提示覆盖已有降级通知",
         old="        and not fallback_notice",
         new="        and True"),
    dict(id="M10", file=PUBLISH_API, expect="test_fallback_tier_reaches_the_user_as_a_notice",
         task="接线拆掉(DB 层照旧分档,但用户什么也看不到)",
         old='        fallback_notice = f"暂时没有与「{requested_industry}」直接对口的媒体，以下为通用推荐。"',
         new='        pass'),
]


# ────────────────────────── 执行 ──────────────────────────

def _run_suite() -> tuple[bool, str]:
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "--no-header", "--tb=no"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    if _abort_if_stale_backup():
        return 2
    if not os.environ.get("TEST_DATABASE_URL"):
        print("🔴 未设置 TEST_DATABASE_URL,套件会 ImportError 而不是因变异转红 —— 拒跑。",
              file=sys.stderr)
        return 2

    _install_guards()

    green, out = _run_suite()
    if not green:
        print("🔴 基线就不是绿的,变异结果没有意义:\n" + out[-2000:], file=sys.stderr)
        return 2
    print("基线绿 ✅\n")

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
            _IN_FLIGHT[path] = original          # ← 先登记
            _keep_backup(path, original)         # ← 再落盘备份
            path.write_bytes(                    # ← 最后才改文件
                text.replace(mut["old"], mut["new"] + "  # MUTATION " + mut["id"]).encode("utf-8")
            )
            assert path.read_bytes() != original, "变异没有改到字节"
            ok, out = _run_suite()
            if ok:
                survived.append((mut, "套件仍然全绿"))
            elif mut["expect"] not in out:
                survived.append((mut, f"转红了但不是点名那条锁({mut['expect']} 未出现在失败集)"))
            else:
                killed.append(mut)
        finally:
            _restore_all()

    print("\n" + "=" * 60)
    for mut in killed:
        print(f"✅ {mut['id']} 被杀 · {mut['task']}")
    for mut, why in survived:
        print(f"❌ {mut['id']} 存活 · {mut['task']} · {why}")
    print("=" * 60)
    print(f"{len(killed)}/{len(MUTATIONS)} 条变异被杀死")
    return 0 if not survived else 1


if __name__ == "__main__":
    raise SystemExit(main())
