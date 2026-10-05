"""判据 · WO-A ①:部署链那一步「先 seed 同步 → 后重建索引」+ 两种失败形态各自告警。

## 这一份要证明的三件事

1. **接线在部署脚本里**(结构锚)。工单说的现状是「三个文件零命中」——
   那三个文件确实零命中,但部署链跑的是 ``scripts/deploy-blue-green.sh``。
   所以锚打在**真正会被执行的那个脚本**上,并且成对:必须命中新形态,
   **且必须不再命中**上一版那种「``|| true`` 吞退出码 + grep 一行字判成功」。
2. **两种 fail-closed 形态各落一条告警行**(真库):抛异常 / 返回非零,
   在 ``ai_ops_alerts`` 里是两条可区分的记录 —— 报「失败了」而不说是哪一种,
   收到的人还得自己去猜。
3. **顺序**:seed 同步在重建之前。反过来就是拿旧答案建新 release,而且**全绿**。

## 反向对照

每条「必须命中」都配了「必须不命中」:
``test_deploy_chain_no_longer_swallows_the_exit_code`` 是 ① 的反面,
``test_a_clean_run_resolves_the_previous_failure_alert`` 是 ② 的反面
(告警只会拉响不会恢复的话,状态位会永久停在失败,下一个人只会无视它)。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from . import conftest as vnext_conftest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy-blue-green.sh"
AI_OPS_MIGRATION = ROOT / "scripts" / "migration_ai_ops_center_2026_07_01.sql"


# ══════════════════════════════════════════════════════════════════════════
# 底座
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module", autouse=True)
def _ai_ops_tables():
    """告警表**不手写** —— 直接跑要上线的那份迁移(同 conftest 的纪律)。

    手抄一份精简 DDL 就是第二套表定义:列一漏、CHECK 一少,判据全绿而生产照炸。
    """
    import psycopg2

    conn = psycopg2.connect(vnext_conftest.EXACT_THROWAWAY_URL)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(AI_OPS_MIGRATION.read_text(encoding="utf-8"))
    finally:
        conn.close()
    yield


@pytest.fixture()
def clean_alerts():
    """每条用例自带干净分母:留着上一条的告警行,断言会被别人的红顶绿。"""
    def _wipe():
        from db.connection import get_connection

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM ai_ops_alerts WHERE rule_key IN %s",
                (tuple(_rules()),),
            )
            conn.commit()
        finally:
            conn.close()

    _wipe()
    yield _wipe
    _wipe()


def _rules() -> tuple[str, ...]:
    from services.kb_release_ops import KB_STATUS_ALERT_RULES

    return KB_STATUS_ALERT_RULES


def _alert_rows(rule_key: str | None = None) -> list[dict]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        if rule_key:
            cur.execute(
                "SELECT rule_key, fingerprint, status, title, detail, payload "
                "FROM ai_ops_alerts WHERE rule_key = %s ORDER BY id", (rule_key,))
        else:
            cur.execute(
                "SELECT rule_key, fingerprint, status, title, detail, payload "
                "FROM ai_ops_alerts ORDER BY id")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _post_deploy_block() -> str:
    """按内容锚切出 Post-Deploy 那一段。

    🔴 不按行号切:行号会漂,而这个脚本随每次部署改动一直在长。
    """
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    start = text.index("[Post-Deploy] 小榜知识 release")
    end = text.index("BEGIN rollback-capability-summary", start)
    return text[start:end]


# ══════════════════════════════════════════════════════════════════════════
# ① 结构锚:部署链里真的有这一步
# ══════════════════════════════════════════════════════════════════════════

def test_deploy_chain_calls_the_kb_release_step():
    """必须命中:部署脚本调 ``python -m services.kb_release_ops`` 并带上 release SHA。

    断链变异(把这一行注释掉 / 改回 ``tools.xiaobang_kb_indexer``)⇒ 本条转红。
    """
    block = _post_deploy_block()
    # 🔴 不能只搜模块名:注释里、兜底告警那一支里都提到它,把主调用整条换掉
    #    判据照样绿(变异 N01 存活实测)。锚打**真正跑并且被取退出码的那一次调用**。
    invocation = re.search(
        r"KB_OUT=\$\(docker exec [^\n]*python -m services\.kb_release_ops", block)
    assert invocation, "部署链里没有那次会被取结果的重建调用:\n" + block[:600]
    assert '--release-sha "$DEPLOY_SHA"' in block, "重建没绑本次 release SHA"


def test_the_step_runs_against_a_container_that_is_already_up():
    """顺序:这一步必须排在**新实例已启动**之后 —— 否则 docker exec 打不进去。

    这条不是形式主义:``docker exec`` 到一个没起来的容器返 125,
    而 125 恰好落进「步骤未能启动」那一支 —— 每次部署都会报警,狼来了。
    """
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    started = text.index('[2.6/8] 启动 $INACTIVE web')
    step = text.index("[Post-Deploy] 小榜知识 release")
    assert started < step, "KB release 步排在了新实例启动之前"


def test_deploy_chain_handles_both_fail_closed_shapes():
    """两种失败形态在脚本里**分开处理**:

    * 步骤跑起来了自己 fail-closed(rc=20/21)⇒ 容器内已告警,脚本只复述;
    * 步骤压根没跑起来 ⇒ 容器内没人来得及告警,脚本用 ``--report-failure`` 补一条。
    """
    block = _post_deploy_block()
    from services.kb_release_ops import EXIT_REBUILD_STAGE, EXIT_SEED_STAGE

    assert str(EXIT_SEED_STAGE) in block and str(EXIT_REBUILD_STAGE) in block, (
        "脚本没按步骤自定义的退出码分支 —— 20/21 与「没跑起来」会混成一支")
    assert "--report-failure" in block, "步骤没跑起来时没有任何人告警"


def test_deploy_chain_no_longer_swallows_the_exit_code():
    """🔴 反向对照 —— 上一版的两个坏形态必须**不再出现**在这一段里。

    ``|| true`` 让非零与零无差别;``grep -q 索引完成`` 判成功会被 ``tail -8`` 切掉
    (生产 8 类 source_type ⇒ 那一行排在末 8 行之外)⇒ 成功也报失败。
    这两条是「每次部署要人工重建」的真因,写回去这条就转红。
    """
    block = _post_deploy_block()
    assert "|| true" not in block, "退出码又被吞了"
    assert 'grep -q "索引完成"' not in block, "又改回按日志文本判成功"
    assert "tools.xiaobang_kb_indexer" not in block.split("🔴", 1)[-1].split("echo \"\"", 1)[-1], (
        "又直接调 indexer,绕开了会告警的那一层")
    # 退出码真的被捕获了(这是「不吞」的正面证据,不是只证明坏形态消失)。
    assert re.search(r"\|\|\s*KB_RC=\$\?", block), "没有捕获退出码"


# ══════════════════════════════════════════════════════════════════════════
# ① 行为:顺序 + 两种形态各自落库
# ══════════════════════════════════════════════════════════════════════════

def test_seed_sync_runs_before_rebuild(clean_alerts):
    """顺序打在**真函数**上:两个被调方各记一笔,序列必须是 seed → rebuild。"""
    from services.kb_release_ops import STAGE_REBUILD, STAGE_SEED, run_kb_release_step

    calls: list[str] = []
    result = run_kb_release_step(
        release_sha="0" * 40,
        seed_sync=lambda: (calls.append(STAGE_SEED), (0, {"published_faq_rows": 3}))[1],
        rebuild=lambda: (calls.append(STAGE_REBUILD), (0, {"inserted": 7}))[1],
    )
    assert calls == [STAGE_SEED, STAGE_REBUILD], calls
    assert result["ok"] is True and result["exit_code"] == 0, result


def test_a_raised_failure_lands_an_alert_row(clean_alerts):
    """形态 A:被调方**抛异常** ⇒ 落一条 firing 告警,且指纹指到出事的那一段。"""
    from services.kb_release_ops import (
        EXIT_REBUILD_STAGE, KB_RELEASE_ALERT_RULE, SHAPE_RAISED,
        STAGE_REBUILD, run_kb_release_step,
    )

    def _boom():
        raise RuntimeError("术语门拒绝写入:seed 里有旧词")

    result = run_kb_release_step(
        release_sha="a" * 40,
        seed_sync=lambda: (0, {"published_faq_rows": 3}),
        rebuild=_boom,
    )
    assert result["ok"] is False
    assert result["shape"] == SHAPE_RAISED
    assert result["exit_code"] == EXIT_REBUILD_STAGE

    rows = [r for r in _alert_rows(KB_RELEASE_ALERT_RULE) if r["status"] == "firing"]
    assert len(rows) == 1, rows
    assert rows[0]["fingerprint"] == STAGE_REBUILD
    assert rows[0]["payload"]["shape"] == SHAPE_RAISED
    assert "术语门拒绝写入" in rows[0]["detail"], rows[0]["detail"]


def test_a_nonzero_return_lands_a_distinguishable_alert_row(clean_alerts):
    """形态 B:被调方**返回非零** ⇒ 也落告警,而且 payload 能跟形态 A 分开。

    分不开的话,收告警的人看到「重建失败」还得自己去猜是崩了还是空 release ——
    两者的处置完全不同(前者查日志,后者查取数)。
    """
    from services.kb_release_ops import (
        EXIT_SEED_STAGE, KB_RELEASE_ALERT_RULE, SHAPE_NONZERO,
        SHAPE_RAISED, STAGE_SEED, run_kb_release_step,
    )

    result = run_kb_release_step(
        release_sha="b" * 40,
        seed_sync=lambda: (1, {"published_faq_rows": 0}),
        rebuild=lambda: pytest.fail("seed 失败之后不该继续重建"),
    )
    assert result["ok"] is False
    assert result["shape"] == SHAPE_NONZERO != SHAPE_RAISED
    assert result["exit_code"] == EXIT_SEED_STAGE

    rows = [r for r in _alert_rows(KB_RELEASE_ALERT_RULE) if r["status"] == "firing"]
    assert len(rows) == 1, rows
    assert rows[0]["fingerprint"] == STAGE_SEED
    assert rows[0]["payload"]["shape"] == SHAPE_NONZERO
    assert rows[0]["payload"]["returned_code"] == 1


def test_a_clean_run_resolves_the_previous_failure_alert(clean_alerts):
    """反向对照:成功一次要把旧告警收掉。

    只会拉响不会恢复的告警 = 状态位永久停在「失败」= 下一个人直接无视它。
    """
    from services.kb_release_ops import KB_RELEASE_ALERT_RULE, run_kb_release_step

    def _boom():
        raise RuntimeError("先制造一条 firing")

    run_kb_release_step(release_sha="c" * 40,
                        seed_sync=lambda: (0, {}), rebuild=_boom)
    assert [r for r in _alert_rows(KB_RELEASE_ALERT_RULE) if r["status"] == "firing"]

    run_kb_release_step(release_sha="c" * 40,
                        seed_sync=lambda: (0, {"published_faq_rows": 3}),
                        rebuild=lambda: (0, {"inserted": 9}))
    assert not [r for r in _alert_rows(KB_RELEASE_ALERT_RULE) if r["status"] == "firing"]


def test_launch_failure_reported_by_the_deploy_script_also_lands(clean_alerts):
    """第三种形态(只可能由脚本报):步骤压根没启动。

    容器内没有任何人来得及告警 —— 这条路径要是不通,那类失败就只剩
    一份下次切流就被冲掉的部署日志。
    """
    from services.kb_release_ops import (
        KB_RELEASE_ALERT_RULE, SHAPE_LAUNCH_FAILED, report_external_failure,
    )

    assert report_external_failure(
        shape=SHAPE_LAUNCH_FAILED, detail="docker exec rc=125", release_sha="d" * 40) is True
    rows = [r for r in _alert_rows(KB_RELEASE_ALERT_RULE) if r["status"] == "firing"]
    assert len(rows) == 1 and rows[0]["fingerprint"] == "launch", rows
    assert rows[0]["payload"]["shape"] == SHAPE_LAUNCH_FAILED


# ══════════════════════════════════════════════════════════════════════════
# ① 默认被调方:空 release / 空 seed 都算失败
# ══════════════════════════════════════════════════════════════════════════

def test_default_rebuild_calls_an_empty_release_a_failure(monkeypatch):
    """「重建成功却一行都没插」在退出码上跟真成功一模一样 —— 这里把它判失败。"""
    import tools.xiaobang_kb_indexer as indexer
    from services.kb_release_ops import default_rebuild

    monkeypatch.setattr(indexer, "rebuild_release",
                        lambda sha=None: {"deleted": 130, "inserted": 0, "manifest": {}})
    code, detail = default_rebuild("e" * 40)
    assert code != 0, "空 release 被当成了成功"
    assert detail["inserted"] == 0


def test_default_rebuild_passes_a_real_release_through(monkeypatch):
    """反向对照:非空 release 必须返 0 —— 否则上一条可以靠「一律判失败」通过。"""
    import tools.xiaobang_kb_indexer as indexer
    from services.kb_release_ops import default_rebuild

    monkeypatch.setattr(indexer, "rebuild_release",
                        lambda sha=None: {"deleted": 130, "inserted": 146, "manifest": {"x": 1}})
    code, detail = default_rebuild("e" * 40)
    assert code == 0 and detail["inserted"] == 146


def test_default_seed_sync_calls_an_empty_faq_table_a_failure():
    """真库:一条已发布 FAQ 都没有 ⇒ 非零。

    打的是真 SQL,不是 stub —— 「published_faq_rows」这个分母塌了,
    接着重建就会产出空 faq release,而那一步自己不会报错。
    """
    from db.connection import get_connection
    from services.kb_release_ops import default_seed_sync

    from api.faq_api import init_faq_tables
    init_faq_tables()

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE faq_items SET is_published = FALSE")
        conn.commit()
    finally:
        conn.close()
    try:
        code, detail = default_seed_sync()
        assert code != 0, "已发布 FAQ 为 0 却判成功"
        assert detail["published_faq_rows"] == 0
    finally:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE faq_items SET is_published = TRUE")
            conn.commit()
        finally:
            conn.close()

    code, detail = default_seed_sync()
    assert code == 0 and detail["published_faq_rows"] > 0, (code, detail)
