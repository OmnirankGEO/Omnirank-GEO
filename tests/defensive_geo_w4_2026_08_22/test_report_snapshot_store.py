"""报告快照**真的被写进库**这件事的判据(#63 · Review §19④ · 2026-09-05)。

背景:`defgeo_report_snapshots` 在这之前**全仓零 INSERT**。读点(五卡)取不到行
就走「绑不了就隐藏」⇒ 五卡**永远留白**,而 08-21 的验收把那个留白读成了
「诚实留白」。`snapshot.py` 早已把冻结面、四条不变量都写全,**零调用者** ——
「修复从没接线,却因为存在而让人停止检查」。

本文件**不使用**本包的 `w4_db` / `cur` 夹具(不碰库):这里锁的是
「版本值怎么来」与「写入方有没有接在成功路径上」。
「brand 629 真诊断后 n_tup_ins>0」那条存在锁是**验收动作**,归验收,不在这里假装。
"""

from __future__ import annotations

import ast
import logging
import pathlib

import pytest

from services.defensive_geo.monitoring import snapshot_store as STORE

ROOT = pathlib.Path(__file__).resolve().parents[2]


# ══════════════════════════════════════════════════════════════════════════
# 版本值:可溯源真值,或显式哨兵 —— 不许有第三种
# ══════════════════════════════════════════════════════════════════════════

def test_release_version_reads_the_release_file(tmp_path):
    """有 sha ⇒ `release:<sha>`(Review §19②:唯一真实存在的版本概念)。"""
    f = tmp_path / "RELEASE_SHA"
    f.write_text("abc123def456", encoding="utf-8")
    assert STORE.release_version(str(f)) == "release:abc123def456"


def test_release_file_is_stripped_not_trusted_verbatim(tmp_path):
    """文件通常带尾随换行 —— 不 strip 就会写出 `release:abc\n`。"""
    f = tmp_path / "RELEASE_SHA"
    f.write_text("  abc123  " + chr(10), encoding="utf-8")
    assert STORE.release_version(str(f)) == "release:abc123"


@pytest.mark.parametrize("content,why", [
    (None, "文件不存在"),
    ("", "空文件"),
    ("   " + chr(10), "只有空白"),
])
def test_missing_or_blank_sha_gives_the_sentinel_and_warns(tmp_path, caplog, content, why):
    """🔴 [Review §19④ 反臂] 读不到 ⇒ **显式哨兵 + WARN**,不写空串。

    空串是最坏的那个:它在下游与「有版本、只是空的」完全同形,
    没有任何东西会因此发问。哨兵会。
    """
    f = tmp_path / "RELEASE_SHA"
    if content is not None:
        f.write_text(content, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        got = STORE.release_version(str(f))
    assert got == STORE.UNVERSIONED, why
    assert got != "", "写了空串 —— 与「有版本」同形"
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "%s 时没有 WARN —— 被正确捕获的失败不留痕迹,最该诊断的就最查不到" % why)


def test_the_sentinel_is_not_mistakable_for_a_real_version():
    """哨兵不许长得像版本号 —— 否则它会被当成真值读。"""
    assert STORE.UNVERSIONED == "unversioned"
    assert not STORE.UNVERSIONED.startswith("release:")


def test_it_reads_the_file_not_the_env_var(monkeypatch, tmp_path):
    """🔴 `RELEASE_SHA` **环境变量不存在**(实测 printenv 恒空)。

    按它取会稳定拿到空串 —— 而那条路径「看起来работает」:哨兵分支照走,
    只是永远拿不到真 sha。所以这条钉住:设了环境变量也不该改变结果。
    """
    monkeypatch.setenv("RELEASE_SHA", "env-should-be-ignored")
    f = tmp_path / "RELEASE_SHA"
    f.write_text("file-wins", encoding="utf-8")
    assert STORE.release_version(str(f)) == "release:file-wins"


def test_all_four_version_columns_share_one_source():
    """四个 `*_version` 今天没有各自的版本源(#109 另立),必须同源同值。

    分开写就是「同一谓词写四处」—— 哪天漂了不会有任何东西报错。
    """
    assert len(STORE._VERSION_COLUMNS) == 4
    assert set(STORE._VERSION_COLUMNS) == {
        "entity_resolver_version", "outcome_classifier_version",
        "evidence_extractor_version", "metric_definition_version"}


# ══════════════════════════════════════════════════════════════════════════
# 可达锁:写入方接在**成功**路径上
# ══════════════════════════════════════════════════════════════════════════

WRITER = "persist_report_snapshot"
HOST = "update_diagnosis_v2_in_db"
FAILURE_PATH = ROOT / "workflows" / "diagnosis_workflow.py"


def _calls_in(src: str, func_name: str, callee: str) -> list:
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if n.name != func_name:
            continue
        return [c for c in ast.walk(n)
                if isinstance(c, ast.Call)
                and getattr(c.func, "attr", getattr(c.func, "id", None)) == callee]
    return []


def test_the_writer_is_wired_into_the_v2_success_path():
    """🔴 接线锁:抽了模块不接线 = 判据驱动的是一份没人用的代码。

    这正是 #63 的**根因本身** —— `snapshot.py` 写得比谁都完整,零调用者。
    """
    src = (ROOT / "services" / "diagnosis_report_v2.py").read_text(encoding="utf-8")
    calls = _calls_in(src, HOST, WRITER)
    assert calls, "%s 里没有对 %s 的调用 —— 又一份没接线的修复" % (HOST, WRITER)


def test_the_writer_is_not_hung_on_the_failure_path():
    """🔴 `diagnosis_workflow.py:353` 那处只在**失败**路径写 report_v2_generated_at。

    把冻结写在那儿,等于只有出错的报告才有快照 —— 而它看起来「接线了」。
    """
    src = FAILURE_PATH.read_text(encoding="utf-8")
    assert WRITER not in src, (
        "快照写入出现在 %s —— 那是失败路径,不是冻结时刻" % FAILURE_PATH.name)


def test_the_wiring_detector_would_notice_if_the_call_vanished():
    """正样本自证:喂合成源(不碰真文件),检测器必须分得出接没接。"""
    good = "def %s(a):%s    %s(a)%s" % (HOST, chr(10), WRITER, chr(10))
    bad = "def %s(a):%s    return a%s" % (HOST, chr(10), chr(10))
    assert _calls_in(good, HOST, WRITER), "检测器对正常接线无反应 —— 尺子坏了"
    assert not _calls_in(bad, HOST, WRITER), "检测器对没接线也说有 —— 恒真"


def _face_keys(src: str) -> set:
    """`_frozen_face` 里真正被赋进 face 的键(**不是**文件里出现过的串)。"""
    keys = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.AnnAssign) or isinstance(n, ast.Assign):
            tgts = [n.target] if isinstance(n, ast.AnnAssign) else list(n.targets)
            named_face = any(isinstance(tg, ast.Name) and tg.id == "face" for tg in tgts)
            if named_face and isinstance(n.value, ast.Dict):
                for k in n.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
            for tg in tgts:
                if (isinstance(tg, ast.Subscript)
                        and isinstance(tg.value, ast.Name) and tg.value.id == "face"):
                    sl = tg.slice
                    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                        keys.add(sl.value)
                    elif isinstance(sl, ast.Name):
                        keys.update(STORE._VERSION_COLUMNS)
    return keys


def test_the_face_key_detector_is_not_fooled_by_a_mere_mention():
    """🔴 正样本自证:只**读**一个键不算赋值(毒 S4 就活在这个缝里)。"""
    only_read = "face = {'a': 1}" + chr(10) + "x = face['b']" + chr(10)
    got = _face_keys(only_read)
    assert "a" in got, "字面量键没收到 —— 尺子坏了"
    assert "b" not in got, "只读了 face['b'] 却算成赋值 —— 又是包含判定"


def test_the_frozen_face_covers_every_field_the_module_declares():
    """🔴 冻结面必须**一项不少** —— 少冻一项,日后就无法从前五边界重建第六边界。

    上一版扫的是「文件里有没有这个字符串常量」。实测毒 S4(把
    `"input_watermark": cutoff.isoformat(),` 那行删掉)**存活** ——
    因为 INSERT 的取值那行还写着 `face["input_watermark"]`,常量照样命中。
    **包含判定第三次咬我**:证的是「这个名字出现过」,不是「这个键被赋了值」。
    改成只看 `face` 字典**真正被赋的键**(字面量键 + `face[...] = ...`)。

    分母取自 `snapshot.FROZEN_FIELDS` 本身,不手写清单 ——
    手写名单漏掉的那一项不会让任何判据变红。
    """
    from services.defensive_geo.monitoring import snapshot as SNAP

    src = (ROOT / "services" / "defensive_geo" / "monitoring"
           / "snapshot_store.py").read_text(encoding="utf-8")
    missing = sorted(set(SNAP.FROZEN_FIELDS) - _face_keys(src))
    assert not missing, "冻结面漏了:%s" % missing


# ══════════════════════════════════════════════════════════════════════════
# INSERT 实参:版本位真的取自 face,不是就地编一个
# ══════════════════════════════════════════════════════════════════════════

class _FakeCur:
    """按 SQL 内容分派的假 cursor;记下每次 execute 的实参。"""

    def __init__(self):
        self.calls = []
        self._last = ""

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._last = sql

    def fetchone(self):
        s = self._last
        if "FROM diagnosis_records" in s:
            return {"brand_id": 629, "owner": 42}
        if "NOW()" in s:
            import datetime
            return {"now": datetime.datetime(2026, 9, 5, 12, 0, 0,
                                             tzinfo=datetime.timezone.utc)}
        if "monitoring_run_cells" in s:
            return {"lo": None, "hashes": ["plan-hash-1"]}
        if "MAX(revision)" in s:
            return {"r": 0}
        return {}

    def fetchall(self):
        return []


def test_insert_actuals_carry_the_face_version_not_a_made_up_one():
    """🔴 [Review §19 P6] 锁的是 **INSERT 实参**,不是「release_version() 怎么算」。

    实测:把 INSERT 里 `face["entity_resolver_version"], face["outcome_classifier_version"]`
    换成 `"1.0.0", "1.0.0"` —— 13 条判据**全绿**。
    我锁了版本值怎么算、锁了 face 覆盖全字段,**没锁这两者之间那段数据流**;
    而伪 semver 正是 §19 明令禁止的那一样东西。

    伪 semver 比空卡片坏得多:空卡片让人继续查,
    一个**具体但错误**的溯源让人停止查并相信它。
    """
    import re

    cur = _FakeCur()
    STORE.persist_report_snapshot(cur, diagnosis_id=629, state="ready")
    inserts = [(s, p) for s, p in cur.calls if "INSERT INTO" in s]
    assert len(inserts) == 1, "预期恰 1 条 INSERT,实得 %d" % len(inserts)
    params = list(inserts[0][1] or [])
    expected = STORE.release_version()
    hits = [p for p in params if p == expected]
    assert len(hits) == len(STORE._VERSION_COLUMNS), (
        "INSERT 实参里取自 release_version() 的只有 %d 个,应为 %d —— "
        "有版本位是就地编的:%r" % (len(hits), len(STORE._VERSION_COLUMNS), params))
    semver = [p for p in params
              if isinstance(p, str) and re.fullmatch(r"\d+\.\d+\.\d+", p)]
    assert not semver, "INSERT 实参里出现伪 semver %r —— §19 明令禁止" % semver


def test_the_actuals_detector_would_catch_a_hardcoded_version():
    """正样本自证:把参数换成写死的 semver,上面那条必须红。"""
    import re

    fake_params = ("rpt-1-r1", 1, 42, 629, 629, "ready", "h" * 64,
                   "plan-hash-1", "plan-hash-1", None, None, None, "wm", [],
                   "1.0.0", "1.0.0", "1.0.0", "1.0.0", "v1")
    semver = [p for p in fake_params
              if isinstance(p, str) and re.fullmatch(r"\d+\.\d+\.\d+", p)]
    assert semver, "检测器认不出伪 semver —— 尺子坏了"
