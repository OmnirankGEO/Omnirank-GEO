"""[价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 变异清单 —— 证明用例真能抓住这个 bug。

只有正向断言的测试分不清「判据生效」和「判据恒真」。这里逐条把新逻辑改坏,
用例**必须转红**;没转红 = 那条判据是摆设。

跑法:
  ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=<测试库> PYTHONIOENCODING=utf-8 \
    python tests/mutation_runner_price_lock_promise.py

三态输出(🔴 别把 SKIP 读成 SURVIVED):
  KILLED    变异后用例转红 = 判据有判别力 ✅
  SURVIVED  变异后用例仍全绿 = 🔴 判据是摆设
  SKIP      变异锚点在源码里命中数 ≠ 1 = 这次没改成任何东西,**不是**结论

行尾纪律:所有写回一律 newline=""(Path.write_text 会按平台改行尾,
把整份文件写成 CRLF —— 用例照样绿而 diff 整片飘红,一天坑过两个执行方)。
"""
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST = "tests/test_price_lock_promise_2026_08_05.py"

# (名字, 相对路径, 原文, 变异后)
MUTATIONS = [
    (
        "M1 lock_fields 没锁时也编一个日期(= 修之前的行为)",
        "services/price_lock_promise.py",
        'return {"price_locked_until": None, "price_lock_note": NO_LOCK_NOTE}',
        'return {"price_locked_until": "2026-08-12", "price_lock_note": None}',
    ),
    (
        "M2 to_lock_date 去掉「已过期不算锁」的 fail-closed 守卫",
        "services/price_lock_promise.py",
        "    if parsed < date.today():\n        return None",
        "    if False:\n        return None",
    ),
    (
        "M3 flat 路径写完缓存不登记锁期(事实传不出来)",
        "tools/batch_pricing.py",
        "                    _record_locked(_lock_map, _to_cache, _expires)",
        "                    pass  # mutated",
    ),
    (
        "M4 flat 路径无条件登记锁期(不看写没写缓存 = 本 bug 的形状)",
        "tools/batch_pricing.py",
        "    _lock_map: dict = {}\n    for _row in scored_cached:\n"
        "        _record_locked(_lock_map, [_row[\"keyword\"]], _row.get(\"_cache_expires_at\"))\n\n"
        "    # ========== 合并：保持原始关键词顺序 ==========",
        "    _lock_map: dict = {}\n    from datetime import timedelta as _td\n"
        "    for _kw in keywords:\n"
        "        _record_locked(_lock_map, [_kw], (datetime.now() + _td(days=7)).isoformat())\n\n"
        "    # ========== 合并：保持原始关键词顺序 ==========",
    ),
    (
        "M5 命中缓存的词不带 expires_at(锁期只能靠猜)",
        "tools/batch_pricing.py",
        '            "_cache_expires_at": c.get("expires_at"),\n        })\n\n'
        '    # [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 逐词锁期 map(只收录真锁住的词)',
        '            "_cache_expires_at": None,\n        })\n\n'
        '    # [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 逐词锁期 map(只收录真锁住的词)',
    ),
    (
        "M6 爆价护栏剥离后仍按【整批 scored_new】登记锁期(按单不按词)",
        "tools/batch_pricing.py",
        "                    _record_locked(_lock_map, _to_cache, _expires)",
        "                    _record_locked(_lock_map, scored_new, _expires)",
    ),
    (
        "M7 cluster compat DTO 写死锁期日期",
        "api/selection_api.py",
        "            **_lock_fields(lock_map, kw_text),",
        '            "price_locked_until": "2026-08-12", "price_lock_note": None,',
    ),
    (
        "M8 save_keyword_prices_cache 不返回 expires_at",
        "db/diagnosis_db.py",
        "        # [价格锁承诺 2026-08-05] commit 之后才返回 —— 返回值 = 「这批词真锁住了,锁到这个时刻」\n"
        "        return expires_at",
        "        return None",
    ),
    (
        "M9 get_cached_keyword_prices 不透出 expires_at",
        "db/diagnosis_db.py",
        "                'expires_at': row.get('expires_at'),",
        "                'expires_at': None,",
    ),
    (
        "M10 save_llm_keyword_prices_cache 不返回 expires_at",
        "db/diagnosis_db.py",
        "        # [价格锁承诺 2026-08-05] commit 之后才返回(同老表口径)\n        return expires_at",
        "        return None",
    ),
    (
        "M11 get_llm_cached_keyword_prices 不透出 expires_at",
        "db/diagnosis_db.py",
        '                "expires_at": row.get("expires_at"),',
        '                "expires_at": None,',
    ),
    # [开源 E3 · B2 · 2026-09-28] M12 的靶子 agents/social_agent.py 随 E3 删除,该毒退役
    (
        "M13 P0-D 信任快照也去写共享缓存(拿跨客户污染换好看的日期)",
        "tools/batch_pricing.py",
        "    if trust_price_active:\n        return False",
        "    if False:\n        return False",
    ),
]


def read(rel):
    with open(os.path.join(REPO, rel), "r", encoding="utf-8", newline="") as f:
        return f.read()


def write(rel, text):
    # 🔴 newline="" —— 不许让 Python 按平台改行尾
    with open(os.path.join(REPO, rel), "w", encoding="utf-8", newline="") as f:
        f.write(text)


def run_tests():
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    p = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main():
    rc, out = run_tests()
    if rc != 0:
        print("🔴 基线就是红的,变异结果无意义。先修基线:")
        print(out[-3000:])
        return 1
    _m = re.search(r"(\d+) passed", out)
    print(f"基线 ✅ 绿 · {_m.group(1) if _m else '?'} 用例\n")

    killed, survived, skipped = [], [], []
    for name, rel, old, new in MUTATIONS:
        src = read(rel)
        n = src.count(old)
        if n != 1:
            # 🔴 命中数 ≠1 标 SKIP,不标 SURVIVED —— 这两个的含义完全相反
            skipped.append((name, f"锚点在 {rel} 命中 {n} 次(需恰好 1 次)"))
            print(f"SKIP     {name}  ← 锚点命中 {n} 次")
            continue
        write(rel, src.replace(old, new, 1))
        try:
            mrc, mout = run_tests()
        finally:
            write(rel, src)   # 无论如何还原
        if mrc != 0:
            killed.append(name)
            print(f"KILLED   {name}")
        else:
            survived.append(name)
            print(f"SURVIVED {name}   🔴 判据是摆设")

    # 还原后必须回到基线绿(证明 runner 自己没把源码改坏留在树上)
    rc2, out2 = run_tests()
    print(f"\n还原后基线:{'✅ 绿' if rc2 == 0 else '🔴 红 —— runner 自己污染了工作树'}")
    print(f"\n=== KILLED {len(killed)} / SURVIVED {len(survived)} / SKIP {len(skipped)} "
          f"(共 {len(MUTATIONS)}) ===")
    for n, why in skipped:
        print(f"  SKIP: {n} —— {why}")
    for n in survived:
        print(f"  SURVIVED: {n}")
    return 0 if (not survived and not skipped and rc2 == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
