"""变异验证:`[2.5.5/8]` 回滚能力探针的锁到底有没有判别力。

工单 `WORKORDER_ROLLBACK_CAPABILITY_PROBE_2026-07-30.md` §4.2 / §4.3
裁定 `REVIEW_VERDICT_ROLLBACK_PROBE_WORKORDER_2026-07-30.md` §7 第 1 条

跑法:
    TEST_DATABASE_URL=... python scripts/mutate_rollback_capability_probe_2026_07_30.py

纪律(每条都是踩过的):
- 🛑 **不用 `git checkout` 还原** —— 会冲掉工作树里未提交的改动。用 `read_bytes` 备份、
  `write_bytes` 写回,`finally` 里做**字节级 + sha256** 双重校验。
- 🛑 **不用 `Path.write_text`** —— Windows 上会把 `\n` 翻成 `\r\n`,一次"还原"就把整个
  `deploy-blue-green.sh` 变成 CRLF → sha256 全变、门禁全红,而 diff 看不出真改动。
- 🔴 每个变异先跑 `bash -n`。语法错会让所有锁一起红 —— 那是假信号,不是判别力。
- 🔴 变异存活**不许悄悄删掉**:写进 `EXPECTED_SURVIVORS` 并说明判别力由哪两处证明。
  报告里也不许把「N 转红 + 1 声明存活」合并写成「变异全转红」。
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "scripts" / "deploy-blue-green.sh"
LOCK_FILES = [
    "tests/deploy/test_rollback_capability_probe_2026_07_30.py",
    "tests/deploy/test_rollback_probe_guard_recipe_directions_2026_07_30.py",
]

# 调用点整块(含它自己的注释),位置变异靠搬这一整块
CALL_BLOCK = '''
# 🔴 位置铁律:必须在 `[2.5/8]` 迁移**之后**(守卫验的是迁移建完的对象,排在迁移前必然
#    误判)、且在 `[2.6/8]` 启动候选容器**之前**(那之后 $INACTIVE 变成新候选、
#    `[7/8]` 之后连 $ACTIVE 槽都被换成候选镜像 → 探什么都恒绿)。
#    `|| true` 是语义的一部分:告警不阻断(迁移已跑完,abort 只会把人留在坏窗口里)。
probe_rollback_capability || true
'''

LEG_A_ORIGINAL = '''rollback_probe_leg_a() {
    docker exec "omnirank-$ACTIVE" python -c "$(rollback_probe_python_body)" 2>&1
}'''

HEADLINE_ECHO = '    echo "  $ROLLBACK_PROBE_HEADLINE"\n'

LEG_B_CALL = '            out_b=$(rollback_probe_leg_b "$prev" || true)\n'

PREV_EXCLUDE = '        | grep -v "^${cur}$" | grep -v "^${DEPLOY_SHA}$" | head -1\n'


def _move_call_after(source: str, anchor: str) -> str:
    """把调用块整块搬到 anchor 之后。anchor 必须唯一。"""
    assert source.count(CALL_BLOCK) == 1, "调用块不唯一,位置变异无法机械施加"
    stripped = source.replace(CALL_BLOCK, "\n")
    assert stripped.count(anchor) == 1, f"锚点不唯一: {anchor!r}"
    return stripped.replace(anchor, anchor + CALL_BLOCK, 1)


def _move_call_before(source: str, anchor: str) -> str:
    assert source.count(CALL_BLOCK) == 1
    stripped = source.replace(CALL_BLOCK, "\n")
    assert stripped.count(anchor) == 1, f"锚点不唯一: {anchor!r}"
    return stripped.replace(anchor, CALL_BLOCK + anchor, 1)


def mutation_1_silence_the_alarm(source: str) -> str:
    """① 把告警改成静默(删掉印告警行的那句 echo)。

    只验"部署通过"的锁抓不到它:谁删掉 warning 照样绿。
    """
    assert source.count(HEADLINE_ECHO) == 1
    return source.replace(HEADLINE_ECHO, "    : # muted\n", 1)


def mutation_2_abort_instead_of_warn(source: str) -> str:
    """② 把 🔴 态改成 `exit 1`(语义从"告警"变成"阻断")。

    迁移已经跑完了,`exit 1` 撤不回迁移,只会把操作者留在坏窗口里且连候选容器都没起。
    """
    assert source.count(HEADLINE_ECHO) == 1
    return source.replace(
        HEADLINE_ECHO,
        HEADLINE_ECHO + '    case "$state" in OK|NotApplicable) ;; *) exit 1 ;; esac\n',
        1,
    )


def mutation_3_probe_before_migration(source: str) -> str:
    """③ 探针挪到 `[2.5/8]` 迁移**之前** → 对尚未建出的对象必然误判(部署白 halt)。"""
    return _move_call_before(
        source, 'echo "[2.5/8] prestart migration + schema verification..."\n'
    )


def mutation_4p_leg_a_targets_this_batch_candidate_image(source: str) -> str:
    """④′ 腿 A 目标改成**本次新构建镜像** → 新代码 × 新库 = 恒绿。

    这才是"验错对象"的真形态:探针看起来在跑、六道全过、报告漂亮,
    而对"现役失去可重启性 / 回退不可用"零覆盖。
    (原稿的变异 ④「$ACTIVE 改 $INACTIVE」已作废 —— 在本插入点两槽同镜像,它恒真。)
    """
    assert source.count(LEG_A_ORIGINAL) == 1
    return source.replace(
        LEG_A_ORIGINAL,
        '''rollback_probe_leg_a() {
    docker run --rm --entrypoint python \\
        "omnirank-release:$BUILT_RELEASE_SHA" -c "$(rollback_probe_python_body)" 2>&1
}''',
        1,
    )


def mutation_4pp_probe_after_candidate_web_up(source: str) -> str:
    """④″ 探针挪到 `:628` `assert_infra_identity_unchanged "candidate-web-up"` 之后。"""
    return _move_call_after(source, 'assert_infra_identity_unchanged "candidate-web-up"\n')


def mutation_4ppp_probe_after_hot_rollback_rebuild(source: str) -> str:
    """④‴(执行方补)探针挪到 `[7/8]` 热回滚重建之后。

    🔴 这才是"晚了恒绿"的**真形态**:`[7/8]:1020-1029` 用候选镜像 `--force-recreate`
    重建了 `omnirank-$ACTIVE` 槽,那之后 `docker exec omnirank-$ACTIVE` 跑的是新代码
    → 腿 A 变成新代码 × 新库 = 恒绿。
    挪到 `:628`(④″)在运行时其实什么都没改变(见报告),所以单靠 ④″ 证明不了
    "晚了会恒绿"这件事真的存在。
    """
    return _move_call_after(source, 'assert_infra_identity_unchanged "hot-rollback-rebuild"\n')


def mutation_5_drop_leg_b(source: str) -> str:
    """⑤ 删掉腿 B(桩成绿)→ 只做腿 A 就交 = 只关了一半。"""
    assert source.count(LEG_B_CALL) == 1
    return source.replace(LEG_B_CALL, '            out_b="RBPROBE_OK [\'stubbed\']"\n', 1)


def mutation_6_prev_stops_excluding_this_batch(source: str) -> str:
    """⑥ prev 的排除条件去掉 `$DEPLOY_SHA` —— **工单原稿的真实 BUG**。

    候选镜像在 `[2/6]` 就已打标而且 Created 最新 → prev 会选中候选自己
    → 腿 B 整条恒绿(不是"与腿 A 重复",是连情形 ② 都零覆盖)。
    """
    assert source.count(PREV_EXCLUDE) == 1
    return source.replace(
        PREV_EXCLUDE, '        | grep -v "^${cur}$" | head -1\n', 1
    )


SUMMARY_RED_LINE = '    *) echo "  $ROLLBACK_PROBE_HEADLINE" ;;\n'


def mutation_7_red_headline_loses_the_summary_head(source: str) -> str:
    """⑦(执行方补)把摘要里的红态告警行从**第一行**挪到最后。

    裁定 §3 追加的置顶要求若没有锁,就只是文档上的承诺:部署日志会被下一次切流冲掉,
    操作者往往只扫一眼尾部 —— 排在后面等于没报。
    """
    assert source.count(SUMMARY_RED_LINE) == 1
    stripped = source.replace(SUMMARY_RED_LINE, "    *) ;;\n", 1)
    anchor = ('echo "  热回滚: $ACTIVE ($ACTIVE_PORT) · 同 image / 同快照能力 / 保持运行"\n')
    assert stripped.count(anchor) == 1
    return stripped.replace(
        anchor,
        anchor + 'case "$ROLLBACK_PROBE_STATE" in ""|OK|NotApplicable) ;; '
                 '*) echo "  $ROLLBACK_PROBE_HEADLINE" ;; esac\n',
        1,
    )


MUTATIONS = [
    ("① 告警改静默", mutation_1_silence_the_alarm, "锁 1a"),
    ("② 🔴 态改 exit 1", mutation_2_abort_instead_of_warn, "锁 5"),
    ("③ 探针挪到 [2.5/8] 之前", mutation_3_probe_before_migration, "锁 1b"),
    ("④′ 腿 A 探本次候选镜像", mutation_4p_leg_a_targets_this_batch_candidate_image, "锁 1a"),
    ("④″ 探针挪到 :628 candidate-web-up 之后", mutation_4pp_probe_after_candidate_web_up, "锁 1b"),
    ("④‴ 探针挪到 [7/8] 热回滚重建之后", mutation_4ppp_probe_after_hot_rollback_rebuild, "锁 1b"),
    ("⑤ 删腿 B", mutation_5_drop_leg_b, "锁 3"),
    ("⑥ prev 漏排 $DEPLOY_SHA", mutation_6_prev_stops_excluding_this_batch, "锁 3"),
    ("⑦ 红态告警行不再置顶", mutation_7_red_headline_loses_the_summary_head, "锁 10"),
]

# 目前为空。若有变异存活,必须在这里登记 + 写清判别力由哪两处证明(§4.3)。
EXPECTED_SURVIVORS: dict[str, str] = {}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_locks() -> tuple[bool, list[str]]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *LOCK_FILES, "-q", "-p", "no:cacheprovider",
         "--no-header", "-x" if False else "--tb=no"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    failed = [
        line.split("::", 1)[1].split(" ", 1)[0]
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED ") and "::" in line
    ]
    return proc.returncode == 0, failed


def _bash_ok() -> bool:
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", "/bin/bash", "bash"):
        if candidate in ("bash",) or Path(candidate).exists():
            proc = subprocess.run([candidate, "-n", str(TARGET)], capture_output=True)
            return proc.returncode == 0
    raise RuntimeError("找不到 bash")


def main() -> int:
    original = TARGET.read_bytes()
    original_hash = _sha256(original)
    print(f"基线 {TARGET.name} sha256={original_hash}")

    green, failed = _run_locks()
    if not green:
        print(f"❌ 未变异时锁就不是全绿:{failed}")
        TARGET.write_bytes(original)
        return 1
    print("✅ 未变异基线:锁全绿\n")

    results = []
    try:
        for name, mutate, expected_lock in MUTATIONS:
            source = original.decode("utf-8")
            mutated = mutate(source)
            assert mutated != source, f"{name} 没有真的改到东西"
            TARGET.write_bytes(mutated.encode("utf-8"))

            if not _bash_ok():
                results.append((name, "INVALID", expected_lock, ["bash -n 语法错"]))
                print(f"⚠️  {name}: bash -n 失败 → 这个变异无效(全红是假信号)")
                TARGET.write_bytes(original)
                continue

            passed, failed = _run_locks()
            verdict = "SURVIVED" if passed else "KILLED"
            results.append((name, verdict, expected_lock, failed))
            mark = "✅" if verdict == "KILLED" else "🔴"
            print(f"{mark} {name}: {verdict} (期望 {expected_lock} 转红) 转红={failed or '无'}")
            TARGET.write_bytes(original)
    finally:
        TARGET.write_bytes(original)
        restored = TARGET.read_bytes()
        assert restored == original, "还原后字节不一致"
        assert _sha256(restored) == original_hash, "还原后 sha256 不一致"
        print(f"\n还原校验:字节一致 ✅ sha256={_sha256(restored)} ✅")

    survivors = [name for name, verdict, _lock, _f in results if verdict == "SURVIVED"]
    invalid = [name for name, verdict, _lock, _f in results if verdict == "INVALID"]
    print("\n===== 汇总 =====")
    print(f"变异总数 {len(results)} · 转红 {len(results) - len(survivors) - len(invalid)} "
          f"· 存活 {len(survivors)} · 无效 {len(invalid)}")
    for name in survivors:
        note = EXPECTED_SURVIVORS.get(name)
        print(f"  存活: {name} · 登记说明: {note or '🔴 未登记 —— 必须按 §4.3 写清判别力来源'}")
    unexpected = [n for n in survivors if n not in EXPECTED_SURVIVORS]
    return 1 if (unexpected or invalid) else 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
