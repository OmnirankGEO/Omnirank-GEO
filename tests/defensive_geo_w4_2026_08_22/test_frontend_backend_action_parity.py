"""UI-37 跨层同源判据 —— 前端动作集合 == 后端 ``_STATE_ACTIONS``。

🔴 为什么这条判据在**后端** pytest 而不在前端 mjs
--------------------------------------------------
它要同时读两层:``frontend/src/lib/defensiveGeoActions.ts`` 与
``services/defensive_geo/presentation/registries.py``。

前端 build 链跑在 Dockerfile 的 ``frontend-builder`` 阶段,那一层只
``COPY frontend/ ./`` —— **后端源码根本不在**。所以一条引后端文件的锁进 build 链
只有两种下场(``verify-no-backend-refs-in-build-chain.mjs`` 的原话):

  ① 整个 ``npm run build`` ENOENT 挂掉;
  ② 有人给它加一个「够不到就 SKIP」的分支 —— 于是它每次构建都大声跳过,
     打印「这不是通过,是没跑」然后 exit 0。**构建绿 ≠ 判据过**。

②(裸奔判据)是 2026-08-17 清扫过的真实形态。所以正确解法不是给 mjs 加
try/catch,而是**把判据挪到够得着两层的那一侧** —— 后端 pytest 全仓可达。

🔴 判据走到哪,毒跟到哪
----------------------
搬家最容易丢的就是**自证**。原 mjs 的 ``--selftest`` 里有一发
``UI37/action-extra`` 注毒;搬过来的同时把毒也搬过来,并且补齐**另一个方向**
(action-missing)。只验一个方向的锁抓不到另一半 ——
本仓记过「双向判:多一个 / 少一个都必须红」。
"""

from __future__ import annotations

import re

import pytest

from services.defensive_geo.presentation import registries as REG
from tests.defensive_geo_w4_2026_08_22.conftest import ROOT

FRONTEND_ACTIONS_TS = ROOT / "frontend" / "src" / "lib" / "defensiveGeoActions.ts"

#: 与前端 mjs 同口径:剥掉整行注释与块注释续行,再按 ``key: '...'`` 抓。
#: 🔴 先剥注释 —— 病历里引用旧写法不该把自己判红(本仓记过的形态)。
# 🔴 [#118-9] 补 `/*` 起头这一档:原正则 `^\s*(\*|//)` 只认续行的 `*` 与 `//`,
#    **接不住单行 JSDoc** `/** … */`。实测 `v35w2Api.ts` 里一行
#    `/** … 上游线下供货 … */` 因此没被剥掉,让「内部术语泄漏」判据误报。
#    在共享工具上补,而不是在调用方绕开 —— 绕开就等于写了第二个剥注释器。
_COMMENT_LINE = re.compile(r"^\s*(/\*|\*|//)")
_KEY_LITERAL = re.compile(r"key:\s*'([a-z_]+)'")


def _strip_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not _COMMENT_LINE.match(line)
    )


def frontend_action_keys(source: str | None = None) -> set[str]:
    """从 TS 源码文本抓动作 key。**不执行 TS**,只做文本比对。"""
    if source is None:
        source = FRONTEND_ACTIONS_TS.read_text(encoding="utf-8")
    return set(_KEY_LITERAL.findall(_strip_comments(source)))


def backend_action_keys() -> set[str]:
    """后端全集 —— 从**真 registry 对象**取,不 regex 解析源码。

    🔴 直接读 ``_STATE_ACTIONS`` 而不是手抄三个 state:
       手抄的那份会在后端加一个 state 的那天静默漂移,
       而漂移当天不会有任何判据变红。
    """
    union: set[str] = set()
    for actions in REG._STATE_ACTIONS.values():
        union.update(actions)
    return union


class TestActionParity:
    def test_backend_registry_is_not_empty(self):
        """活性自证:分母不是空集。

        没有这一条,下面那条"两边相等"在**两边都空**时也会通过 ——
        本仓记过的零分母形态。
        """
        assert REG._STATE_ACTIONS, "后端 _STATE_ACTIONS 是空的 ⇒ 下面的相等判据零分母"
        assert backend_action_keys(), "后端动作全集为空 ⇒ 同源判据没有区分力"

    def test_frontend_parser_finds_something(self):
        """活性自证:TS 解析器真的抓到了东西。

        解析器瞎了的时候,前端集合恒为空 —— 那时"少了几个"会红得莫名其妙,
        而"多了几个"永远不会红。
        """
        assert FRONTEND_ACTIONS_TS.exists(), f"{FRONTEND_ACTIONS_TS} 不存在"
        assert frontend_action_keys(), (
            "从 defensiveGeoActions.ts 一个 key 都没抓到 —— 解析器多半瞎了,"
            "这时同源判据是假绿")

    def test_action_sets_are_exactly_equal(self):
        """UI-37 同源。**双向**判:少一个 / 多一个都红。

        · 少 = 后端会下发但前端没绑定 ⇒ 渲染成死按钮或被静默丢弃;
        · 多 = 前端画了个后端不认的按钮 ⇒ 在承诺一个不存在的能力。
        """
        fe, be = frontend_action_keys(), backend_action_keys()
        assert fe == be, (
            f"动作集合不同源。前端缺:{sorted(be - fe)};"
            f"前端多出:{sorted(fe - be)}"
        )

    # ── 判据走到哪,毒跟到哪:原 mjs --selftest 那一发搬过来并补齐反向 ──────

    def test_poison_extra_frontend_action_is_caught(self):
        """毒① 前端多绑一个后端不下发的动作 ⇒ 必须被抓到。

        (这一发就是原 ``verify-defensive-geo-actions.mjs --selftest`` 里的
        ``UI37/action-extra``,随判据一起搬家。)
        """
        poisoned = FRONTEND_ACTIONS_TS.read_text(encoding="utf-8") + (
            "\nconst poison = { key: 'delete_everything' };\n")
        fe = frontend_action_keys(poisoned)
        assert fe != backend_action_keys(), "多绑一个动作没被抓到 —— 这把锁是恒绿的"
        assert "delete_everything" in fe - backend_action_keys()

    def test_poison_missing_frontend_action_is_caught(self):
        """毒② 前端漏绑一个后端会下发的动作 ⇒ 必须被抓到(原 mjs **没有**这一发)。

        搬家时补齐的方向。只验"多"不验"少",接线被摘掉那天照样全绿。
        """
        victim = sorted(backend_action_keys())[0]
        src = FRONTEND_ACTIONS_TS.read_text(encoding="utf-8")
        poisoned = src.replace(f"key: '{victim}'", "key: 'renamed_away'", 1)
        assert poisoned != src, f"注毒锚点没命中(找不到 key: '{victim}')"
        fe = frontend_action_keys(poisoned)
        assert fe != backend_action_keys(), "漏绑一个动作没被抓到 —— 这把锁是恒绿的"
        assert victim in backend_action_keys() - fe

    def test_clean_source_is_not_flagged(self):
        """成对负样本:干净输入必须**不**命中(证明上面两发不是恒红)。"""
        assert frontend_action_keys() == backend_action_keys()


class TestGateStaysInTheBuildChain:
    """禁止的解法两条,各配一把锁。

    工单原话:「禁止的解法:给 mjs 加 try/catch-SKIP(裸奔判据)
    或把它从 build 链摘掉」。写成判据,而不是写在交付单里靠人记得。
    """

    def test_frontend_gate_is_still_wired_into_build(self):
        """摘出 build 链 = 锁还在文件里但永远不跑。"""
        import json

        pkg = json.loads(
            (ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
        assert "verify-defensive-geo-actions.mjs" in pkg["scripts"]["build"], (
            "UI-37 前端闸被摘出 build 链 —— 不接线的锁等于没有")

    #: 裸奔判据的**代码**特征。判的是跳过语义,不是 ``try`` 这个词本身 ——
    #: 禁 try 会误伤正当的错误处理。
    _SKIP_MARKERS = ("SKIP", "skip(", "跳过")

    @staticmethod
    def _gate_code() -> str:
        """🔴 **先剥注释再扫**。

        第一版直接扫原文,当场被自己的 docstring 判红 ——
        那段 docstring 写的正是「为什么不许加 SKIP」。
        本仓记过同一形态两次(引用裁决原文会让裸串结构锚判红),
        我在同一个改动里又踩了一次,所以这里把剥注释写死。
        """
        src = (ROOT / "frontend" / "scripts"
               / "verify-defensive-geo-actions.mjs").read_text(encoding="utf-8")
        return _strip_comments(src)

    def test_frontend_gate_has_no_skip_branch(self):
        """裸奔判据:够不到就 SKIP 然后 exit 0。"""
        code = self._gate_code()
        for marker in self._SKIP_MARKERS:
            assert marker not in code, (
                f"前端闸的**代码**里出现跳过语义「{marker}」—— 裸奔判据:"
                "每次构建大声跳过然后 exit 0,构建绿而判据没跑")

    def test_skip_detector_is_alive(self):
        """探测器活性自证:剥注释别剥过头,真代码里的 SKIP 必须还抓得到。

        没有这一条,上面那条"零命中"既可能是真没有,
        也可能是剥注释把整个文件都吃掉了(剥过头 ⇒ 恒绿)。
        """
        code = self._gate_code()
        assert len(code.strip()) > 500, (
            f"剥注释后只剩 {len(code.strip())} 字符 —— 多半剥过头了,"
            "这时「无 SKIP」是假绿")
        poisoned = code + "\ntry { x(); } catch { console.log('SKIP'); exit(0); }\n"
        assert any(m in poisoned for m in self._SKIP_MARKERS), "探测器抓不到真 SKIP 分支"

    def test_frontend_gate_no_longer_reaches_into_backend(self):
        """这条判据搬家的**原因**本身也要有锁:mjs 不许再引 services/**。

        拆红:把 registries.py 的读取加回 mjs —— 本判据立刻红,
        且 build 链的越界闸也会红(两道独立)。
        """
        src = (ROOT / "frontend" / "scripts"
               / "verify-defensive-geo-actions.mjs").read_text(encoding="utf-8")
        code = _strip_comments(src)
        for marker in (".py", "services/", "../services"):
            assert marker not in code, (
                f"前端闸又伸手到 frontend/ 之外(命中「{marker}」)—— "
                "frontend-builder 层读不到后端源码")
