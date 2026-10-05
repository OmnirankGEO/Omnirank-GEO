# -*- coding: utf-8 -*-
"""WO_268 · 服务商申请协议文件必须**在树里、且进得了镜像**。

🔴 事故形状:`a02225e31`(07-12 anchor 快照,根提交)把 `docs/条款/` 及 5 个文件名按
   cp437 双重编码写进了树(`docs/µ¥íµ¼╛/…`)。`.dockerignore` 先 `docs/**` 全排、再只放行
   **正确名** `!docs/条款/**` ⇒ 乱码目录被排除 ⇒ 镜像里没有协议 ⇒ 服务商申请第 3 步拉协议 404、
   `/apply` 服务端重读协议也 404。07-12 起 0 申请 0 签署,没有任何东西红过。

判据两条(对应工单 (a)(b)),都**按代码真实读取的那条路径**认,不按猜的名字:
  (a) `_load_agreement_text(CURRENT_AGREEMENT_VERSION)` 读到的那个文件在 **HEAD 的树**里、非空;
  (b) 那个文件在**构建上下文**里 = 树里的文件 ∩ 未被 `.dockerignore` 排除。
      🔴 (b) 不能写成「这条路径会不会被排除」:那样目录改名时它照样绿
         (`docs/条款/x` 这条路径本身一直是放行的,问题是文件已经不在这条路径上了)。

反臂:目录改成任意别名 ⇒ (a)(b) 同时红;`.dockerignore` 删掉放行 ⇒ (b) 红。
对照臂:同一套函数、没动过的输入 ⇒ 绿(防「恒红的尺子」)。
尺子本身(`_dockerignore.py`)用**真 BuildKit 的判定**校:两棵全树 15589 个文件零分歧,
其中有区分力的若干条冻在下面的 `_BUILDKIT_FACTS`。
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from ._dockerignore import (
    build_context_files,
    compile_patterns,
    is_excluded,
    read_patterns,
)

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

#: 「条款」的 UTF-8 字节按 cp437 解读 —— 就是 07-12 那次写进树里的目录名。
MOJIBAKE_DIR = "条款".encode("utf-8").decode("cp437")


# ══════════════════════════════════════════════════════════════════
# 取数:HEAD 的树 / 代码真实读取的路径 / 现役 .dockerignore
# ══════════════════════════════════════════════════════════════════

def _head_tree() -> list:
    """HEAD 的树(不是工作区):构建机 checkout 的就是它。git 不可用 ⇒ 当场抛错(红),不跳过。"""
    raw = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-r", "-z", "--name-only", "HEAD"],
                         capture_output=True, check=True).stdout
    return [p.decode("utf-8") for p in raw.split(b"\0") if p]


def _blob_size(rel: str) -> int:
    out = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-s", "HEAD:" + rel],
                         capture_output=True, check=True).stdout
    return int(out.strip())


def _live_dockerignore() -> str:
    return (ROOT / ".dockerignore").read_text(encoding="utf-8")


def _load_current(pa, monkeypatch):
    """调真 `_load_agreement_text`,并记下它**实际读的**是哪个文件(不猜文件名)。"""
    seen = []
    real = Path.read_text

    def spy(self, *a, **kw):
        seen.append(Path(self))
        return real(self, *a, **kw)

    with monkeypatch.context() as m:
        m.setattr(Path, "read_text", spy)
        text = pa._load_agreement_text(pa.CURRENT_AGREEMENT_VERSION)
    assert len(seen) == 1, "loader 应只读一个文件,实际读了 %r" % seen
    return text, seen[0]


def _rel(p: Path) -> str:
    return p.resolve().relative_to(ROOT).as_posix()


def _check(tracked, dockerignore_text: str, rel: str):
    """(a)(b) 两条判据的**唯一**实现 —— 真判据、反臂、对照臂都走它。"""
    in_tree = rel in set(tracked)
    in_context = rel in build_context_files(tracked, dockerignore_text)
    return in_tree, in_context


@pytest.fixture(scope="module")
def pa():
    import api.partner_api as partner_api
    return partner_api


# ══════════════════════════════════════════════════════════════════
# 真判据(同时是对照臂:没动过的输入必须绿)
# ══════════════════════════════════════════════════════════════════

def test_a_current_agreement_is_a_nonempty_file_in_head_tree(pa, monkeypatch):
    text, read_path = _load_current(pa, monkeypatch)
    rel = _rel(read_path)
    assert text.strip(), "协议正文为空:%s" % rel
    # v2.3+ 是「服务商申请协议」;「代理合作协议」只给历史版本回退用(partner_api 注释原文)
    assert read_path.name.startswith("服务商申请协议_"), (
        "当前版本 %s 读到的不是服务商申请协议,而是 %s(落到了历史回退名)"
        % (pa.CURRENT_AGREEMENT_VERSION, read_path.name))
    in_tree, _ = _check(_head_tree(), _live_dockerignore(), rel)
    assert in_tree, "代码读的 %s 不在 HEAD 的树里(工作区有、没提交?)" % rel
    assert _blob_size(rel) > 0, "树里的 %s 是空文件" % rel


def test_b_current_agreement_reaches_docker_build_context(pa, monkeypatch):
    _, read_path = _load_current(pa, monkeypatch)
    rel = _rel(read_path)
    in_tree, in_context = _check(_head_tree(), _live_dockerignore(), rel)
    assert in_tree and in_context, (
        "代码读的 %s:在树里=%s,进构建上下文=%s —— 镜像里不会有它,"
        "服务商申请第 3 步与 /apply 都会 404" % (rel, in_tree, in_context))


def test_b_every_tracked_file_under_agreement_dir_reaches_context(pa):
    dir_rel = _rel(pa._AGREEMENT_DIR)
    tracked = _head_tree()
    files = [p for p in tracked if p.startswith(dir_rel + "/")]
    assert files, "HEAD 的树里 %s/ 下一个文件都没有" % dir_rel
    ctx = build_context_files(tracked, _live_dockerignore())
    missing = [p for p in files if p not in ctx]
    assert not missing, "这些协议文件进不了镜像:%r" % missing


# ══════════════════════════════════════════════════════════════════
# 反臂(牙证)+ 配对的对照臂 —— 与真判据走同一个 `_check` / 同一个 loader
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("alias", [MOJIBAKE_DIR, "条款_bak", "clauses"])
def test_reverse_arm_renamed_dir_turns_a_and_b_red(pa, monkeypatch, alias):
    _, read_path = _load_current(pa, monkeypatch)
    rel = _rel(read_path)
    dir_rel = _rel(pa._AGREEMENT_DIR)
    tracked = _head_tree()
    renamed = [("docs/%s/" % alias) + p[len(dir_rel) + 1:] if p.startswith(dir_rel + "/") else p
               for p in tracked]
    assert renamed != tracked, "反臂没下成:改名前后树一样"
    assert _check(renamed, _live_dockerignore(), rel) == (False, False)
    # 对照臂:同一个函数、没改名的树 ⇒ 绿
    assert _check(tracked, _live_dockerignore(), rel) == (True, True)


@pytest.mark.parametrize("alias", [MOJIBAKE_DIR, "clauses"])
def test_reverse_arm_loader_404_when_dir_renamed(pa, monkeypatch, tmp_path, alias):
    from fastapi import HTTPException

    _, read_path = _load_current(pa, monkeypatch)
    # 毒:文件原样拷进一个**改了名**的目录,loader 仍按代码里的名字找
    bad = tmp_path / "bad" / "docs" / alias
    bad.mkdir(parents=True)
    shutil.copyfile(read_path, bad / read_path.name)
    monkeypatch.setattr(pa, "_AGREEMENT_DIR", tmp_path / "bad" / "docs" / pa._AGREEMENT_DIR.name)
    with pytest.raises(HTTPException) as ei:
        pa._load_agreement_text(pa.CURRENT_AGREEMENT_VERSION)
    assert ei.value.status_code == 404
    # 对照臂:同一招 monkeypatch、目录名不改 ⇒ 读得到(证明红不是 monkeypatch 本身弄坏的)
    good = tmp_path / "good" / "docs" / pa._AGREEMENT_DIR.name
    good.mkdir(parents=True)
    shutil.copyfile(read_path, good / read_path.name)
    monkeypatch.setattr(pa, "_AGREEMENT_DIR", good)
    assert pa._load_agreement_text(pa.CURRENT_AGREEMENT_VERSION).strip()


def test_reverse_arm_dockerignore_without_exception_turns_b_red(pa, monkeypatch):
    _, read_path = _load_current(pa, monkeypatch)
    rel = _rel(read_path)
    live = _live_dockerignore()
    dir_rel = _rel(pa._AGREEMENT_DIR)
    poisoned_lines = [ln for ln in live.splitlines()
                      if not ln.strip().startswith("!" + dir_rel)]
    assert len(poisoned_lines) < len(live.splitlines()), "毒没下成:现役 .dockerignore 里找不到放行行"
    tracked = _head_tree()
    assert _check(tracked, "\n".join(poisoned_lines), rel) == (True, False)
    assert _check(tracked, live, rel) == (True, True)     # 对照臂


# ══════════════════════════════════════════════════════════════════
# 尺子本身:移植的匹配器 vs 真 BuildKit(期望值**不是**本匹配器算的)
# ══════════════════════════════════════════════════════════════════

#: 冻结输入:跑 BuildKit 那一刻的 `.dockerignore`(= db862889a 的那一份,LF)。
_FIXTURE = HERE / "dockerignore_2026-09-23.fixture"
_FIXTURE_SHA256 = "c23e1affedbbac458d9fecf819e44c04c24794b9b94fb9a3a6e10da56feaf75a"

#: (路径, BuildKit 是否放进上下文)—— 2026-09-23 本机 Docker 29.2.1 / BuildKit,
#: `FROM scratch; COPY . /` 全树导出实测(修复树 6845/8798、基线树 6833/8791,移植版零分歧)。
#: 每条都挑的是**有区分力**的语义点,注释写它区分的是什么。
_BUILDKIT_FACTS = [
    ("requirements.txt", True),                        # *.txt 排后 !requirements*.txt 取反
    ("scripts/research/naive_timestamp_columns_prod_2026-08-17.txt", True),  # *.txt 只锚根(gitignore 会排)
    ("scripts/geo_report_warm_trust.pdf", True),       # *.pdf 同理只锚根
    ("db/migrate_channel_tier.py", True),              # db/migrate_*.py 排后单文件取反
    ("db/migrate_v3.py", False),
    ("scripts/_wob40_poison.py", False),               # scripts/_*.py
    ("cache/leader_lock.py", True),                    # cache/* 排后 !cache/*.py 取反
    ("docs/AI-CONTEXT/CTO_HANDOFF.md", False),         # docs/** 深层
    ("docs/条款/隐私政策.md", True),                   # docs/** 排后 !docs/条款/** 取反
    ("docs/%s/隐私政策.md" % MOJIBAKE_DIR, False),     # 乱码目录(基线树实测)
    # ↓ 这两条**只能经父目录命中**(文件本身不匹配模式):不收它们,匹配器里最容易写错的
    #   「父目录命中」那段一格都没人测 —— 上面 docs/条款 那条同时被 `!docs/条款/**` 直接命中。
    (".planning/PROJECT.md", False),                   # 字面目录模式 `.planning`
    ("agent-test-artifacts-round2/ROUND2_REPORT.md", False),  # 通配目录模式 `agent-test-artifacts*`
]


def test_ruler_fixture_is_the_one_buildkit_ran_against():
    raw = _FIXTURE.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == _FIXTURE_SHA256, (
        "冻结的 .dockerignore 副本被改过 —— 下面那张 BuildKit 判定表只对原件成立")


@pytest.mark.parametrize("path,in_context", _BUILDKIT_FACTS)
def test_ruler_matches_buildkit(path, in_context):
    pats = compile_patterns(read_patterns(_FIXTURE.read_text(encoding="utf-8")))
    assert (not is_excluded(path, pats)) is in_context, (
        "移植的匹配器与真 BuildKit 不一致:%s BuildKit=%s" % (path, "进" if in_context else "排除"))
