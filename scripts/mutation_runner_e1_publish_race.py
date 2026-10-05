#!/usr/bin/env python
"""E1-1 · 「删掉 WHERE 里任一条件,该条件那一臂必红」—— 一条件一发。

工单要求逐条证明 ``mark_external_start`` 那条 CAS 的每个谓词都**承重**。
做法不是把条件整行删掉(参数占位会错位、SQL 直接语法错,那样红的是语法不是缺陷),
而是把它**中和**成恒真:``X`` → ``(X OR TRUE)``。语义上等价于「这个条件不再约束」,
而 SQL 仍然合法、``%s`` 参数个数不变 —— 红出来的才是缺陷本身。

🔴 锚点必须双行:``AND c.settled_at IS NULL`` 在本文件里出现 **2 次**
   (另一处缩进 16 空格,11 空格的锚会被它**子串命中**)。单行锚在这里是错的。

分母 = 工单B 发布包(含新增 E1 族) + w3 那条 ``mark_external_start`` 判据
(两个包用各自的 throwaway PG16)。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mutation_tree_lock import tree_lock  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".e1run"
TARGET = ROOT / "services" / "defensive_geo" / "publish" / "store.py"

# [门9 · 2026-08-27] DSN **可由环境变量覆盖**,默认值逐字不变。
# 起因是本轮实测:DSN 写死 = 每个窗口被钉在同一批**共享**库上,而共享库会
# 「漂到代码前面」(别的窗口把一条本仓没有的 CHECK 加了上去,我的包在那库上红、
# 冷建的自有库上全绿)。全员自有冷建库是 Review 2026-08-27 的常设规矩。
DSN_WOB = os.environ.get(
    "E1_DSN_WOB", "postgresql://geo_admin:testpw@localhost:55850/geo_defgeo_wob_test")
DSN_W3 = os.environ.get(
    "E1_DSN_W3", "postgresql://geo_admin:testpw@localhost:55475/geo_defgeo_w3c_test")

DENOMINATOR: list[tuple[str, str]] = [
    ("tests/defgeo_wob_publish_funding_2026_08_25", DSN_WOB),
    ("tests/defensive_geo_w3_2026_08_21/test_wp6_pg_constraints.py", DSN_W3),
]

#: 一条件一发。``expect`` = 这一发**必须**红的判据(精确子串匹配)。
MUTATIONS: list[dict] = [
    {
        "id": "E1M-1", "cond": "external_start_at IS NULL(至多一次)",
        "from": ("         WHERE c.publish_command_id = %s\n"
                 "           AND c.external_start_at IS NULL\n"),
        "to": ("         WHERE c.publish_command_id = %s\n"
               "           AND (c.external_start_at IS NULL OR TRUE)\n"),
        "expect": ["test_mark_external_start_is_at_most_once"],
    },
    {
        "id": "E1M-2", "cond": "settled_at IS NULL(钱已收尾不得外发)",
        "from": ("           AND c.external_start_at IS NULL\n"
                 "           AND c.settled_at IS NULL\n"),
        "to": ("           AND c.external_start_at IS NULL\n"
               "           AND (c.settled_at IS NULL OR TRUE)\n"),
        "expect": ["test_e1_02_a_settled_command_cannot_mark"],
    },
    {
        "id": "E1M-3", "cond": "funding_state 在允许集",
        "from": ("           AND c.settled_at IS NULL\n"
                 "           AND c.funding_state = ANY(%s)\n"),
        "to": ("           AND c.settled_at IS NULL\n"
               "           AND (c.funding_state = ANY(%s) OR TRUE)\n"),
        "expect": ["test_e1_03_a_non_dispatchable_funding_state_cannot_mark"],
    },
    {
        "id": "E1M-4", "cond": "command_state 在允许集",
        "from": ("           AND c.funding_state = ANY(%s)\n"
                 "           AND c.command_state = ANY(%s)\n"),
        "to": ("           AND c.funding_state = ANY(%s)\n"
               "           AND (c.command_state = ANY(%s) OR TRUE)\n"),
        "expect": ["test_e1_04_a_non_dispatchable_command_state_cannot_mark"],
    },
    {
        "id": "E1M-5", "cond": "EXISTS(outbox 仍被我这次租约持有)· fencing",
        "from": "                    AND o.claim_token = %s)\n",
        "to": "                    AND (o.claim_token = %s OR TRUE))\n",
        "expect": ["test_e1_01_a_stale_lease_cannot_mark"],
    },
]

#: E1-2:计数与写入同命运。目标文件是 worker,不是 store —— 每发自带 file。
WORKER = ROOT / "services" / "defensive_geo" / "publish" / "publish_worker.py"
MUTATIONS += [
    {
        "id": "E1M-6", "cond": "[E1-2] defer 计数改回无条件 +1", "file": WORKER,
        "from": '                deferred += int(_defer(int(row["id"]), token, str(exc)))\n',
        "to": ('                deferred += 1\n'
               '                _defer(int(row["id"]), token, str(exc))\n'),
        "expect": ["test_e1_30_a_defer_that_wrote_nothing_is_not_counted"],
    },
    {
        "id": "E1M-7", "cond": "[E1-2] quarantine 计数改回无条件 +1", "file": WORKER,
        "from": ('                quarantined += '
                 'int(_quarantine_row(int(row["id"]), token, str(exc)))\n'),
        "to": ('                quarantined += 1\n'
               '                _quarantine_row(int(row["id"]), token, str(exc))\n'),
        "expect": ["test_e1_32_a_quarantine_that_wrote_nothing_is_not_counted"],
    },
]

_NODE = re.compile(r"^(?:FAILED|ERROR)\s+(tests/\S+|\S*\.py::\S+)")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _one(target: str, dsn: str) -> tuple[set[str], int]:
    env = dict(os.environ, TEST_DATABASE_URL=dsn, PYTHONIOENCODING="utf-8")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "-ra", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env)
    out = r.stdout + r.stderr
    red = {m.group(1).replace("\\", "/").split("/")[-1] for m in
           (_NODE.match(ln) for ln in out.splitlines()) if m}
    m = re.search(r"(\d+) passed", out)
    return red, int(m.group(1)) if m else 0


def run_all() -> tuple[set[str], int]:
    red: set[str] = set()
    green = 0
    for target, dsn in DENOMINATOR:
        r, g = _one(target, dsn)
        red |= r
        green += g
    return red, green


def _restore(original: bytes, target: Path = None) -> None:
    target = target or TARGET
    tmp = target.with_suffix(target.suffix + ".mutrestore")
    tmp.write_bytes(original)
    os.replace(tmp, target)
    back = target.read_bytes()
    if sha(back) != sha(original):
        raise SystemExit("🔴 还原后逐字节不一致 —— 停,人工看")


def _insertion_type(muts) -> list[str]:
    """替换**包含**锚的发 —— 本文件那份在盘实证判不了它们。

    见 post-train 工具单 ``WO_POSTTRAIN_TOOL_ONDISK_PROOF_UNIFY_2026-08-27.md``。
    """
    return [m["id"] for m in muts if m["from"] in m["to"]]


def main() -> int:
    with tree_lock("mutation_runner_e1_publish_race"):
        STATE.mkdir(exist_ok=True)
        # 🔴 [post-train 工具单 WO_POSTTRAIN_TOOL_ONDISK_PROOF_UNIFY_2026-08-27]
        #    本文件下面那段在盘实证是**引擎那份的副本**,且停在旧规则
        #    (``readback_needle_gone``)。旧规则默认了「替换不含锚」——
        #    MUT-EXTE2-10 就是反例:替换包含锚,毒完全落盘却被判成"没落盘"。
        #    统一到 ``mutation_runner_extsel_2026_08_26._on_disk_proof``
        #    (逐字节等价)要重跑这 7 发 ⇒ 归 post-train。在那之前**关死盲区**:
        #    宁可停机,也不给一个可信的错数。
        #    正样本先跑 —— 闸没有正样本就只是一句"我检查过了"。
        assert _insertion_type([{"id": "POS", "from": "a", "to": "xax"}]) == ["POS"], (
            "🔴 插入型探针自己就不成立 —— 这道闸是装饰")
        _ins = _insertion_type(MUTATIONS)
        if _ins:
            raise SystemExit(
                f"🔴 {_ins} 是**插入型**变异(替换包含锚),本文件的在盘实证判不了 —— "
                "先做工具单 WO_POSTTRAIN_TOOL_ONDISK_PROOF_UNIFY_2026-08-27 再跑")
        base_red, base_green = run_all()
        print(f"基线:绿 {base_green} / 红 {len(base_red)} {sorted(base_red) or ''}")
        if base_red:
            raise SystemExit("🔴 基线不是全绿 —— 后面全部作废")

        results = []
        bad = 0
        for mut in MUTATIONS:
            target = mut.get("file", TARGET)
            original = target.read_bytes()
            needle = mut["from"].encode("utf-8")
            repl = mut["to"].encode("utf-8")
            hits = original.count(needle)
            if hits != 1:
                raise SystemExit(
                    f"🔴 {mut['id']} 锚点命中 {hits} 次(要求 1)—— 停下报 Review")
            mutated = original.replace(needle, repl, 1)
            if mutated == original:
                raise SystemExit(f"🔴 {mut['id']} 替换后无变化 = no-op,停")

            backup = target.with_suffix(target.suffix + ".mutbak")
            backup.write_bytes(original)
            target.write_bytes(mutated)
            disk = target.read_bytes()
            proof = {
                "anchor_hits": hits,
                "sha_before": sha(original)[:16], "sha_after": sha(disk)[:16],
                "readback_needle_gone": needle not in disk,
                "readback_repl_present": repl in disk,
            }
            proof["ok"] = bool(proof["anchor_hits"] == 1
                               and proof["sha_before"] != proof["sha_after"]
                               and proof["readback_needle_gone"]
                               and proof["readback_repl_present"])
            syntax_ok = True
            try:
                ast.parse(disk.decode("utf-8"))
            except SyntaxError:
                syntax_ok = False
            try:
                if not proof["ok"]:
                    raise SystemExit(f"🔴 {mut['id']} 变异没真落盘:{proof}")
                if not syntax_ok:
                    raise SystemExit(f"🔴 {mut['id']} 变异后语法错 —— 红的会是语法不是缺陷")
                red, green = run_all()
            finally:
                _restore(original, target)
                backup.unlink(missing_ok=True)

            new_red = sorted(red - base_red)
            missing = [e for e in mut["expect"] if not any(e in r for r in new_red)]
            ok = bool(new_red) and not missing
            results.append({**{k: mut[k] for k in ("id", "cond", "expect")},
                            "red": new_red, "green": green, "base_green": base_green,
                            "on_disk_proof": proof, "verdict": "KILLED" if ok else "PROBLEM"})
            flag = "💀 杀" if ok else "🔴 有问题"
            print(f"{flag} {mut['id']} [{mut['cond']}] 红 {len(new_red)} / 绿 {green}")
            for r in new_red:
                print(f"      {r}")
            if missing:
                print(f"    🔴 点名必须红的没红:{missing} —— 这个条件**不承重**,停下报 Review")
                bad += 1
            if not new_red:
                print("    🔴 一条都没红 —— 删掉这个条件没有任何判据发现,停下报 Review")
                bad += 1

        (STATE / "e1_condition_mutations.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print("═" * 72)
        print(f"一条件一发:{len(MUTATIONS)} 发 · 杀 "
              f"{sum(1 for r in results if r['verdict'] == 'KILLED')} · 异常 {bad}")
        return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
