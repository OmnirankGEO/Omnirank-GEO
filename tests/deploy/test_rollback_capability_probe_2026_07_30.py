"""锁:`[2.5.5/8]` 回滚能力探针(`scripts/deploy-blue-green.sh`)。

工单:`docs/AI-CONTEXT/WORKORDER_ROLLBACK_CAPABILITY_PROBE_2026-07-30.md` §4.1
裁定:`docs/AI-CONTEXT/REVIEW_VERDICT_ROLLBACK_PROBE_WORKORDER_2026-07-30.md` §7

🔴 **为什么不用源码串断言**:本仓既有的 deploy 脚本锁(`test_unified_release_gates.py` /
`test_inventory_pricing_deploy_nogo6.py`)全是 `"..." in source`。那种锁换个写法就绕过去,
而且**证明不了探针真会报警**。这里改成行为级:把探针整块按哨兵注释抽出来,
喂一个 stub `docker`,**真跑 bash**,断言它印出来的告警行本身。

覆盖的九条(工单 §4.1,裁定 §3.2 追加第 7 条):
  锁 1a 腿 A 命中(行为)      锁 1b 探针位置(执行行序 · 时序双向护栏)
  锁 2  反向对照(必须不报警)   锁 3  腿 B 命中
  锁 4  N/A 态                锁 5  不阻断
  锁 6  落痕可解析             锁 7  ProbeError ≠ N/A
  锁 8  NoPrevImage 独立标签    锁 9  连续两批升级文案

🔴 **判别力自证**:凡"造一个场景看它是否报警"的锁,都配了反向场景断言它**不报警**
(锁 2 是锁 1a 的反向;锁 9 的 `not_consecutive` 分支是它自己的反向)。
2026-07-30 当天有三条自造判据被证伪,都是"恒真判据"型 —— 所以锁先证明自己有判别力,
再作为证据。

⚠️ 守卫本身的判别力(注入不符 → 真的 fail-closed)**不在这个文件里**:
stub 里的 python 是罐头标记,验不到真守卫。那部分由 `expected ⊋ actual` 与
`actual ⊋ expected` 两个方向在生产真库上只读实证(见交付说明),
真守卫比对逻辑是 `dealer_inventory_resale.py:1803 actual_values != set(expected_values)`
= **精确相等**,两侧今天都硬拦。
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy-blue-green.sh"
BEGIN_MARK = "# ===== BEGIN rollback-capability-probe"
END_MARK = "# ===== END rollback-capability-probe"
SUMMARY_BEGIN = "# ===== BEGIN rollback-capability-summary"
SUMMARY_END = "# ===== END rollback-capability-summary"

DEPLOY_SHA = "a" * 40          # 本次候选
CUR_SHA = "b" * 40             # 现役(= 上一版)
PREV_SHA = "c" * 40            # 上上一版 = 腿 B 的靶子
OLDEST_SHA = "d" * 40

OK_MARKER = "RBPROBE_OK ['diagnosis', 'geo_article_v14', 'article_closed_loop', " \
            "'dealer_resale', 'geo_observation', 'monitoring_product']"
GUARDFAIL_MARKER = (
    "RBPROBE_GUARDFAIL RuntimeError [DealerResale SchemaCheck fail-closed] "
    "旧流水 CHECK 白名单不完整或被篡改 agent_inventory_transactions_type_check"
)
NA_MARKER = "RBPROBE_NA services.startup_schema_guards"

# Created 倒序:候选最新(它在 [2/6] 就已打标),然后现役,然后上上一版
DEFAULT_IMAGES = ";".join([
    f"{DEPLOY_SHA}|2026-07-30 16:29:09 +0800 CST",
    f"{CUR_SHA}|2026-07-30 16:01:26 +0800 CST",
    f"{PREV_SHA}|2026-07-30 15:00:11 +0800 CST",
    f"{OLDEST_SHA}|2026-07-30 12:00:00 +0800 CST",
])

STUB_DOCKER = r"""#!/usr/bin/env bash
# stub docker:只回答探针真正会问的那几个问题,并把每次调用记进 STUB_DOCKER_LOG。
set -u
printf '%s\n' "$*" >> "${STUB_DOCKER_LOG}"
cmd="${1:-}"; shift || true
case "$cmd" in
  exec)
    container="${1:-}"; shift || true
    if [ "${1:-}" = "printenv" ]; then
        printf '%s' "${STUB_DBURL-}"
        [ -n "${STUB_DBURL-}" ] || exit 1
        exit 0
    fi
    printf '%s\n' "${STUB_EXEC_PY_RESP-}"
    exit "${STUB_EXEC_PY_RC:-0}"
    ;;
  inspect)
    target="${1:-}"
    if [ "${STUB_INSPECT_FAIL:-0}" = "1" ]; then
        echo "Error: No such object: $target" >&2
        exit 1
    fi
    case "$target" in
      *-db) printf '%s \n' "${STUB_NET-}" ;;
      *)    printf '%s\n' "${STUB_CUR-}" ;;
    esac
    exit 0
    ;;
  images)
    printf '%s\n' "${STUB_IMAGES-}" | tr ';' '\n' | sed '/^$/d'
    exit 0
    ;;
  run)
    tag=""
    for a in "$@"; do
      case "$a" in omnirank-release:*) tag="${a#omnirank-release:}" ;; esac
    done
    var="STUB_RUN_RESP_${tag}"
    printf '%s\n' "${!var-}"
    exit 0
    ;;
esac
exit 0
"""


def _bash() -> str:
    """🔴 Windows 上 PATH 里的 `bash` 是 WSL 的,不认 `C:/…` 路径(实测 exit 127
    「No such file or directory」)。必须挑 Git bash;Linux 上照常拿 /bin/bash。"""
    override = os.environ.get("RBPROBE_BASH")
    if override:
        return override
    for candidate in (r"C:\Program Files\Git\bin\bash.exe",
                      r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(candidate).exists():
            return candidate
    found = shutil.which("bash")
    if found and not re.search(r"(System32|WindowsApps)", found, re.I):
        return found
    raise RuntimeError("找不到可用的 bash(WSL 的那个不算),设 RBPROBE_BASH 指过来")


def _probe_block() -> str:
    """按哨兵注释整块抽取探针实现。抽不到就红 —— 哨兵被删掉等于锁失去被测对象。"""
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert BEGIN_MARK in source and END_MARK in source, "探针哨兵注释缺失,锁失去被测对象"
    body = source.split(BEGIN_MARK, 1)[1].split(END_MARK, 1)[0]
    body = body.split("\n", 1)[1]  # 丢掉 BEGIN 那行的尾巴
    assert "run_fleet_schema_guards" in body, (
        "探针没经 run_fleet_schema_guards 取守卫集 —— 自己另列一份就是第三处漂移源"
    )
    return body


@pytest.fixture()
def harness(tmp_path):
    """跑一次探针,返回 (stdout, 落痕文件内容, docker 调用记录)。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_bytes(STUB_DOCKER.encode("utf-8"))
    docker.chmod(0o755)
    docker_log = tmp_path / "docker-calls.log"
    docker_log.write_text("", encoding="utf-8")
    probe_log = tmp_path / "rollback_capability.log"

    def run(*, exec_resp=OK_MARKER, exec_rc=0, images=DEFAULT_IMAGES, cur=CUR_SHA,
            net="geo_agentscope_default", dburl="postgresql://u:p@db:5432/geo_agentscope",
            inspect_fail=False, run_resp=None, seed_log=None, mutate=None):
        if seed_log is not None:
            probe_log.write_text(seed_log, encoding="utf-8")
        block = _probe_block()
        if mutate is not None:
            block = mutate(block)
        script = tmp_path / "harness.sh"
        script.write_bytes(("\n".join([
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            'ACTIVE="green"',
            'INACTIVE="blue"',
            f'DEPLOY_SHA="{DEPLOY_SHA}"',
            'BUILT_RELEASE_SHA="$DEPLOY_SHA"',
            'DB_CONTAINER="omnirank-db"',
            f'ROLLBACK_PROBE_LOG="{probe_log.as_posix()}"',
            block,
            "probe_rollback_capability || true",
            'echo "AFTER_PROBE_REACHED"',
            "",
        ])).encode("utf-8"))

        env = dict(os.environ)
        env["PATH"] = f"{bin_dir.as_posix()}:{env.get('PATH', '')}"
        env["STUB_DOCKER_LOG"] = docker_log.as_posix()
        env["STUB_EXEC_PY_RESP"] = exec_resp
        env["STUB_EXEC_PY_RC"] = str(exec_rc)
        env["STUB_IMAGES"] = images
        env["STUB_CUR"] = cur
        env["STUB_NET"] = net
        env["STUB_DBURL"] = dburl
        env["STUB_INSPECT_FAIL"] = "1" if inspect_fail else "0"
        for tag, resp in (run_resp or {PREV_SHA: OK_MARKER, DEPLOY_SHA: OK_MARKER}).items():
            env[f"STUB_RUN_RESP_{tag}"] = resp

        proc = subprocess.run(
            [_bash(), script.as_posix()], env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        # 🔴 harness 自己炸了会让每条断言都变成"输出里没有那句话" = 一堆假红。
        assert "[2.5.5/8]" in proc.stdout, (
            f"harness 没跑起来 rc={proc.returncode}\nstderr={proc.stderr[:2000]}"
        )
        return (
            proc.stdout,
            probe_log.read_text(encoding="utf-8") if probe_log.exists() else "",
            docker_log.read_text(encoding="utf-8"),
            proc.returncode,
        )

    return run


# ======================= 锁 1a:腿 A 命中(行为) =======================

def test_lock1a_leg_a_guard_failure_raises_the_alarm(harness):
    """现役槽(旧代码 + 新库)守卫抛异常 → 必须印出 GuardFail 告警行且点明 active。

    变异 ①(删 echo)/ ④′(腿 A 目标改成本次新构建镜像)都在这里转红。
    """
    out, _log, calls, _rc = harness(exec_resp=GUARDFAIL_MARKER)

    assert "🔴 [RollbackCapability:GuardFail]" in out, out
    assert "active=green" in out
    assert "legA=GuardFail" in out
    # 腿 A 必须真的探了现役槽,而不是探本次候选镜像(那是新码 × 新库 = 恒绿)
    assert "exec omnirank-green python -c" in calls, calls
    assert f"omnirank-release:{DEPLOY_SHA}" not in calls, "探到了本次候选镜像 = 验错对象"


def test_probe_never_touches_the_inactive_slot(harness):
    """探针一次也不碰 `omnirank-$INACTIVE` 槽 —— 这条把一个反直觉事实钉成可验证的:

    🔴 原稿变异 ④「腿 A 目标 $ACTIVE 改 $INACTIVE」在本插入点恒真(切流后两槽同镜像),
    已作废。**进一步**:把探针挪到 `:628`(变异 ④″)在**运行时同样什么都没改变** ——
    探针只读 `$ACTIVE` 槽和 `omnirank-release:*` 标签,而 `[2.6/8]` 启动候选容器既不动
    `$ACTIVE`、也不新增/删除 release 标签(候选镜像在 `[2/6]` 就已打标)。
    → ④″ 只能靠**位置锁**(锁 1b)抓,抓不到它的运行时后果,因为它没有运行时后果。
    真正"晚了恒绿"的形态是挪到 `[7/8]` 热回滚重建之后(变异 ④‴):那一步用候选镜像
    `--force-recreate` 重建了 `$ACTIVE` 槽,腿 A 才真的变成新代码 × 新库。
    """
    _out, _log, calls, _rc = harness(exec_resp=OK_MARKER)

    assert "omnirank-green" in calls, "夹具没跑到 $ACTIVE → 下面的断言会变成空断言"
    assert "omnirank-blue" not in calls, calls


# ======================= 锁 1b:探针位置(时序双向护栏) =======================

def _executable_lines() -> list[str]:
    """只留可执行行(去注释 / 去空行)——「加个注释提一下」不算把探针挪回来了。"""
    lines = []
    for raw in DEPLOY_SCRIPT.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def test_lock1b_probe_sits_between_migration_and_candidate_startup():
    """🔴 早了必然误判(守卫验的是迁移建完的对象)· 晚了必然恒绿。

    变异 ③(挪到 `[2.5/8]` 之前)、④″(挪到 `:628` candidate-web-up 之后)、
    ④‴(挪到 `[7/8]` 热回滚重建之后 —— 那之后连 $ACTIVE 槽都换成候选镜像了)
    都在这里转红。
    """
    lines = _executable_lines()
    calls = [i for i, line in enumerate(lines) if re.match(r"^probe_rollback_capability(?!\s*\()\b", line)]
    assert len(calls) == 1, f"探针调用点必须恰好一处,实际 {len(calls)} 处"
    call_at = calls[0]

    def index_of(pattern: str) -> int:
        hits = [i for i, line in enumerate(lines) if re.search(pattern, line)]
        assert hits, f"找不到锚点 {pattern}"
        return hits[0]

    migration_at = index_of(r'^echo "\[2\.5/8\] prestart migration')
    snapshot_gate_at = index_of(r"^require_inventory_snapshot_schema \|\| exit 1$")
    candidate_up_at = index_of(r'^echo "\[2\.6/8\] ')

    assert migration_at < call_at, "探针排在 [2.5/8] 迁移之前 → 对尚未建出的对象必然误判"
    assert snapshot_gate_at < call_at, "探针必须在 require_inventory_snapshot_schema 之后"
    assert call_at < candidate_up_at, (
        "探针排在 [2.6/8] 启动候选容器之后 → 此后 $INACTIVE 是新候选、"
        "[7/8] 之后 $ACTIVE 槽也被换成候选镜像 → 探什么都恒绿"
    )


# ======================= 锁 2:反向对照(必须不报警) =======================

def test_lock2_all_green_emits_no_red_label(harness):
    """🔴 判别力自证:两腿全过 → 必须是 OK,且输出里**不含**任何 🔴 告警标签。"""
    out, log, _calls, _rc = harness(exec_resp=OK_MARKER)

    assert "✅ [RollbackCapability:OK]" in out, out
    assert "🔴 [RollbackCapability:" not in out, out
    assert "⚪" not in out
    assert f"|OK|green|{PREV_SHA}" in log


# ======================= 锁 3:腿 B 命中 =======================

def test_lock3_leg_b_guard_failure_on_the_prev_image_raises_the_alarm(harness):
    """上上一版镜像的期望集与库不符 → 告警行必须点明 prev,且区分出是腿 B 红的。

    变异 ⑤(删腿 B)/ ⑥(prev 排除条件去掉 $DEPLOY_SHA → prev 选中本次候选 → 恒绿)
    都在这里转红。
    """
    out, log, calls, _rc = harness(
        exec_resp=OK_MARKER,
        run_resp={PREV_SHA: GUARDFAIL_MARKER, DEPLOY_SHA: OK_MARKER},
    )

    assert "🔴 [RollbackCapability:GuardFail]" in out, out
    assert f"prev={PREV_SHA[:8]}" in out, out
    # 精确到「腿 A 绿 / 腿 B 红」——否则删掉腿 B 或把 prev 指到候选镜像照样能混过
    assert "legA=OK legB=GuardFail" in out, out
    assert f"omnirank-release:{PREV_SHA}" in calls, calls
    assert f"|GuardFail|green|{PREV_SHA}" in log


def test_lock3_prev_excludes_the_candidate_image_of_this_batch(harness):
    """prev 必须同时排除现役与本次候选:候选镜像 Created 最新,漏排就会选中它。"""
    _out, _log, calls, _rc = harness(exec_resp=OK_MARKER)

    assert f"omnirank-release:{PREV_SHA}" in calls, calls
    assert f"omnirank-release:{DEPLOY_SHA}" not in calls, "腿 B 探到了本次候选镜像 = 恒绿"
    assert f"omnirank-release:{CUR_SHA}" not in calls, "腿 B 探到了现役镜像 = 与腿 A 重复"


# ======================= 锁 4:N/A 态 =======================

def test_lock4_missing_guard_module_is_not_applicable_without_red(harness):
    """回退到引入该模块之前的镜像 → ⚪ NotApplicable,且**不含 🔴**。

    没有这条,每次回退到老版本都会喜报式误警。
    """
    out, log, _calls, _rc = harness(
        exec_resp=NA_MARKER,
        run_resp={PREV_SHA: NA_MARKER, DEPLOY_SHA: NA_MARKER},
    )

    assert "⚪ [RollbackCapability:NotApplicable]" in out, out
    assert "🔴" not in out, out
    assert "|NotApplicable|" in log


# ======================= 锁 5:不阻断 =======================

def test_lock5_red_state_never_blocks_the_deploy(harness):
    """任一红态下脚本必须继续往下走(迁移已跑完,abort 只会把人留在坏窗口里)。"""
    out, _log, _calls, rc = harness(exec_resp=GUARDFAIL_MARKER)

    assert "🔴 [RollbackCapability:GuardFail]" in out
    assert "AFTER_PROBE_REACHED" in out, "探针把部署 abort 掉了 —— 语义必须是告警不是阻断"
    assert rc == 0


def test_lock5_call_site_keeps_the_non_blocking_contract():
    """调用点必须是 `probe_rollback_capability || true`,并且探针块里没有 exit。

    🔴 `exit` 不受调用点 `|| true` 保护(它直接结束整个 shell),所以两侧都要钉。
    """
    lines = _executable_lines()
    call = next(line for line in lines if re.match(r"^probe_rollback_capability(?!\s*\()\b", line))
    assert call == "probe_rollback_capability || true", call

    block = _probe_block()
    offenders = [
        line.strip() for line in block.splitlines()
        if re.search(r"(^|[;&|(\s])exit\b", line.split("#", 1)[0])
        and "sys.exit" not in line
    ]
    assert not offenders, f"探针块里出现 exit → 会把部署 abort:{offenders}"


# ======================= 锁 6:落痕 =======================

def test_lock6_persists_a_machine_parsable_line(harness):
    """部署日志会被下一次切流冲掉 → 必须另落宿主持久文件,且格式下批能 parse。"""
    _out, log, _calls, _rc = harness(exec_resp=GUARDFAIL_MARKER)

    rows = [row for row in log.strip().splitlines() if row]
    assert len(rows) == 1, log
    fields = rows[0].split("|")
    assert len(fields) == 5, fields
    ts, sha, state, active, prev = fields
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ts), ts
    assert sha == DEPLOY_SHA
    assert state == "GuardFail"
    assert active == "green"
    assert prev == PREV_SHA


# ======================= 锁 7:ProbeError ≠ N/A(裁定 §3.2 追加) =======================

def test_lock7_probe_self_failure_is_probe_error_not_not_applicable(harness):
    """🔴 探针自身故障(网络查不到)必须是 ProbeError,且**不含 ⚪**。

    归 ⚪ 会让"探针坏了"和"探针不适用"长得一样 → 探针死掉后永远显示 ⚪ 而没人知道。
    没有这条锁,两态的区分只是文档上的承诺。
    """
    out, log, _calls, _rc = harness(exec_resp=OK_MARKER, net="")

    assert "🔴 [RollbackCapability:ProbeError]" in out, out
    assert "⚪" not in out, out
    assert "NotApplicable" not in out, out
    assert "|ProbeError|" in log


def test_lock7_docker_inspect_failure_is_also_probe_error(harness):
    """认不出现役版本(docker inspect 失败)同样是探针自身故障,不是"探针不适用"。"""
    out, _log, _calls, _rc = harness(exec_resp=OK_MARKER, inspect_fail=True)

    assert "🔴 [RollbackCapability:ProbeError]" in out, out
    assert "⚪" not in out


# ======================= 锁 8:NoPrevImage 独立标签 =======================

def test_lock8_missing_prev_image_gets_its_own_red_label(harness):
    """回退落点被磁盘治理清掉 → 独立标签(处置动作是**重建镜像**,不是跑回滚 SQL)。"""
    only_current = ";".join([
        f"{DEPLOY_SHA}|2026-07-30 16:29:09 +0800 CST",
        f"{CUR_SHA}|2026-07-30 16:01:26 +0800 CST",
    ])
    out, log, _calls, _rc = harness(exec_resp=OK_MARKER, images=only_current)

    assert "🔴 [RollbackCapability:NoPrevImage]" in out, out
    assert ":GuardFail" not in out, "标签混用了 —— 两态处置动作不同"
    assert "重建镜像" in out
    assert "|NoPrevImage|green|absent" in log


# ======================= 锁 9:连续两批升级文案 =======================

def test_lock9_consecutive_red_batches_escalate_the_wording(harness):
    """落痕文件必须能被下一批读到:单批红可能是操作者已知;连续多批说明没人处置。"""
    seeded = f"2026-07-30T08:00:00Z|{'e' * 40}|GuardFail|blue|{PREV_SHA}\n"
    out, log, _calls, _rc = harness(exec_resp=GUARDFAIL_MARKER, seed_log=seeded)

    assert "连续 2 批不可回退" in out, out
    assert len([row for row in log.strip().splitlines() if row]) == 2


def test_lock9_reverse_a_green_previous_batch_must_not_claim_consecutive(harness):
    """🔴 判别力自证:上一批是 OK → 本批红也**不得**出现「连续」字样。"""
    seeded = f"2026-07-30T08:00:00Z|{'e' * 40}|OK|blue|{PREV_SHA}\n"
    out, _log, _calls, _rc = harness(exec_resp=GUARDFAIL_MARKER, seed_log=seeded)

    assert "🔴 [RollbackCapability:GuardFail]" in out
    assert "连续" not in out, out


def test_lock9_streak_counts_only_the_trailing_red_run(harness):
    """中间夹了一批 OK 就重新计数 —— 否则"历史上红过"会被永久当成"连续红"。"""
    seeded = (
        f"2026-07-30T06:00:00Z|{'e' * 40}|GuardFail|blue|{PREV_SHA}\n"
        f"2026-07-30T07:00:00Z|{'f' * 40}|OK|blue|{PREV_SHA}\n"
        f"2026-07-30T08:00:00Z|{'0' * 40}|ProbeError|blue|{PREV_SHA}\n"
    )
    out, _log, _calls, _rc = harness(exec_resp=GUARDFAIL_MARKER, seed_log=seeded)

    assert "连续 2 批不可回退" in out, out


# ======================= 锁 10:收尾摘要置顶(裁定 §3 追加要求) =======================

def _summary_block() -> str:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert SUMMARY_BEGIN in source and SUMMARY_END in source, "摘要哨兵注释缺失"
    body = source.split(SUMMARY_BEGIN, 1)[1].split(SUMMARY_END, 1)[0]
    return body.split("\n", 1)[1]


def _run_summary(tmp_path, state: str, headline: str) -> list[str]:
    script = tmp_path / "summary.sh"
    script.write_bytes(("\n".join([
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'INACTIVE="blue"; INACTIVE_PORT=8001; ACTIVE="green"; ACTIVE_PORT=8002',
        'DEPLOY_SHA="x"; PROJECT_DIR="/p"; REVIEWED_ROLLBACK_SCRIPT_SHA256="y"',
        f'ROLLBACK_PROBE_STATE="{state}"',
        f'ROLLBACK_PROBE_HEADLINE="{headline}"',
        _summary_block(),
        "",
    ])).encode("utf-8"))
    proc = subprocess.run(
        [_bash(), script.as_posix()], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert "部署完成" in proc.stdout, f"摘要 harness 没跑起来: {proc.stderr[:1500]}"
    return [line for line in proc.stdout.splitlines() if line.strip()]


@pytest.mark.parametrize("state", ["GuardFail", "NoPrevImage", "ProbeError"])
def test_lock10_red_headline_is_the_first_content_line_of_the_summary(tmp_path, state):
    """🔴 部署日志会被下一次切流冲掉,操作者往往只扫尾部 —— 排在后面等于没报。"""
    headline = f"🔴 [RollbackCapability:{state}] active=green(bbbbbbbb) prev=cccccccc"
    lines = _run_summary(tmp_path, state, headline)

    body = [line for line in lines if not line.strip().startswith("====")]
    assert body[0].strip() == headline, f"红态告警行没排在摘要第一行:{body[:3]}"
    assert body[1].strip() == "部署完成"


@pytest.mark.parametrize("state", ["OK", "NotApplicable", ""])
def test_lock10_reverse_green_state_does_not_hijack_the_summary_head(tmp_path, state):
    """🔴 判别力自证:绿态(以及探针根本没跑的空态)不得抢摘要头部。

    少了这条反向,「无条件把探针行放第一行」也能让上面那条过。
    """
    headline = "✅ [RollbackCapability:OK] active=green(bbbbbbbb) prev=cccccccc"
    lines = _run_summary(tmp_path, state, headline)

    body = [line for line in lines if not line.strip().startswith("====")]
    assert body[0].strip() == "部署完成", body[:3]
    if state == "":
        assert all("RollbackCapability" not in line for line in body), body
    else:
        assert any(line.strip().startswith("回滚能力探针:") for line in body), body


# ======================= §6「探不到什么」必须写进日志 =======================

def test_scope_disclaimers_are_printed_every_run(harness):
    """防止下一任把"探针绿"读成"所有契约都安全"(裁定 §5.1 追加两条)。"""
    out, _log, _calls, _rc = harness(exec_resp=OK_MARKER)

    assert "FLEET_SCHEMA_GUARDS 六道" in out
    for needle in ("探不到 1", "探不到 2", "探不到 3", "探不到 4", "探不到 5"):
        assert needle in out, needle
    assert "旧 release 目录" in out          # 裁定 §5.1 第 4 条
    assert "跳多版回退" in out               # 裁定 §5.1 第 5 条


def test_db_password_never_reaches_the_docker_run_command_line(harness):
    """裁定 §3.3:`-e DATABASE_URL` 只给变量名,值由 docker 从环境继承。"""
    secret = "postgresql://geo_admin:s3cr3t-probe-canary@db:5432/geo_agentscope"
    _out, _log, calls, _rc = harness(exec_resp=OK_MARKER, dburl=secret)

    assert "-e DATABASE_URL" in calls, calls
    assert "s3cr3t-probe-canary" not in calls, "口令进了 docker run 命令行 → 宿主 ps aux 可见"


def test_probe_never_imports_server():
    """禁忌:ROLE 未设时 `import server` 会 _run_sql_migrations() = 在生产容器里跑迁移。"""
    code_only = "\n".join(line.split("#", 1)[0] for line in _probe_block().splitlines())
    # 只禁"真的把 server 拉起来"的形态;注释/文案里提 server.py 不算(否定说法也会被 grep
    # 命中,所以这里按形态匹配而不是按词匹配)。
    for forbidden in ("import server", "from server", "-m server", "python server"):
        assert forbidden not in code_only, forbidden
