"""§15 部分成功交付 · 锁(Review 裁定 2026-08-03)

背景数学:单卡成功率 ~0.9 时五张联合只有 0.9⁵ ≈ 59%。
生产实测 5 次 0 成品、**两次都是 4/5** —— 那不是质量问题,
是把**联合概率当成了门槛**,并且把已生成且已付费的 4 张主动丢掉。

§15 四条 + Review 点名的四对变异,逐条对应:

  §15-1 成品门槛 = 封面成功 且 成功卡 ≥ 一半;低于门槛维持全退
        → 变异对①「封面失败必全退」
  §15-2 达门槛 →「补齐中」:冻结不动,已生成卡全留,失败卡自动免费重抽 ≤2 轮
        → 变异对②「补齐中不得 commit 不得 release」
        → 变异对④「自动重抽不扣用户额度」
  §15-3 补齐全组才 commit —— "完成才扣"不破,只是"完成"可补齐
        → 变异对③「补齐恰好 commit 一次」
  §15-4 补不齐交还用户免费重抽;12h sweeper 死线兜底 —— 零新增资金路径

🔴 资金面的锁一律做成**账本探针**(数 commit/release 各被调了几次),
   不断言源码字符串 —— 换个写法就假绿的锁在资金面尤其危险。
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
from dataclasses import dataclass, field
from typing import List

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
REDRAW = ROOT / "services" / "geo_douyin" / "redraw.py"
SETTLE = ROOT / "services" / "geo_douyin" / "settlement.py"
PROD = ROOT / "services" / "geo_douyin" / "production_task.py"
# [WO_271 · 2026-09-23] 旧入口页 DouyinImagePost.tsx 已删(6b491ab23);读它的那一格改指作品详情页
DETAIL = ROOT / "frontend" / "src" / "pages" / "Writing" / "DouyinPostDetail.tsx"
WORKLIST = ROOT / "services" / "geo_douyin" / "keyword_worklist.py"


def _fn_code(path: pathlib.Path, name: str) -> str:
    """函数体源码,**docstring 已剥**。

    🔴 这个仓库里同一个坑踩过 5 次以上:断言命中的是**解释这件事的
       docstring/注释**,把真代码删掉锁照样绿。本文件写的时候又踩了两次 ——
       `_auto_fill` 的 docstring 里写着 "不用 redraw_one_card",
       `claim_task_settlement` 的 docstring 里写着 "先 SELECT 判…再 UPDATE",
       正好把两条"必须不出现"的断言喂饱了。所以一律先剥。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return ast.unparse(ast.Module(body=body, type_ignores=[])) if body else ""


def test_docstring_stripper_has_power():
    """必须不命中面:剥 docstring 这件事本身要有判别力,
    否则下面两条"必须不出现"的锁全是恒真的。"""
    raw = PROD.read_text(encoding="utf-8")
    assert "不用 redraw_one_card" in raw, "样本前提变了"
    assert "redraw_one_card" not in _fn_code(PROD, "_auto_fill")


# ═════════════════════════════════════════════════════════════
# §15-1 成品门槛(纯函数 · 行为锁)
# ═════════════════════════════════════════════════════════════
def _card(kind, ok=True, idx=0):
    return {"idx": idx, "kind": kind,
            "oss_key": f"k{idx}" if ok else "",
            "status": "ready" if ok else "failed"}


def _group(cover_ok, content_flags, closing_ok=True):
    rows = [_card("cover", cover_ok, 0)]
    rows += [_card("content", f, i + 1) for i, f in enumerate(content_flags)]
    rows.append(_card("closing", closing_ok, len(content_flags) + 1))
    return rows


def test_threshold_half_rounds_up():
    from services.geo_douyin.settlement import min_ok_required
    assert min_ok_required(5) == 3      # 一半向上取整
    assert min_ok_required(4) == 2
    assert min_ok_required(1) == 1
    assert min_ok_required(0) == 1      # 不能要求 0 张


def test_four_of_five_is_deliverable():
    """实测里反复出现的那一档:5 张成 4 张 → 必须能交付。
    这正是"5 次生产 0 成品"要被解决掉的那个 case。"""
    from services.geo_douyin.settlement import is_deliverable
    rows = _group(True, [True, True, False], closing_ok=True)   # 5 张成 4
    assert len(rows) == 5 and sum(1 for r in rows if r["status"] == "ready") == 4
    assert is_deliverable(rows) is True


def test_cover_failure_is_never_deliverable():
    """🔴 变异对① 封面失败必全退。
    封面在预览、缩略图、发布封面三处都露脸,缺了对用户没价值。
    **这是硬条件,不是"算一张"** —— 哪怕其余全成也不交付。"""
    from services.geo_douyin.settlement import is_deliverable
    rows = _group(False, [True, True, True], closing_ok=True)   # 5 张成 4,但缺封面
    assert sum(1 for r in rows if r["status"] == "ready") == 4
    assert is_deliverable(rows) is False, "封面没出来却判成可交付"


def test_below_half_is_not_deliverable():
    """必须不命中面:成功卡不到一半 → 维持现行全退。"""
    from services.geo_douyin.settlement import is_deliverable
    rows = _group(True, [False, False, False], closing_ok=False)  # 5 张只成 1
    assert is_deliverable(rows) is False


def test_exactly_half_is_deliverable_boundary():
    """边界:恰好一半(向上取整后)要能交付,别把边界判反。"""
    from services.geo_douyin.settlement import is_deliverable, min_ok_required
    assert min_ok_required(5) == 3
    # 🔴 样本要自己数清楚:_group 的 closing 默认成功,也算一张。
    #    我第一版把 [True,False,False]+closing 当成"成 2 张",其实是 3 张 ——
    #    样本算错会让边界测试测的是**另一个边界**(等于没测)。
    two_ok = _group(True, [True, False, False], closing_ok=False)   # 封面+1内容 = 2
    assert sum(1 for r in two_ok if r["status"] == "ready") == 2
    assert is_deliverable(two_ok) is False                          # 2 < 3

    three_ok = _group(True, [True, False, False], closing_ok=True)  # 再加收尾 = 3
    assert sum(1 for r in three_ok if r["status"] == "ready") == 3
    assert is_deliverable(three_ok) is True                         # 3 >= 3


def test_card_without_oss_key_is_not_ok():
    """必须不命中面:status=ready 但没有图 key 的不算成功
    (否则"占位空串"会被当成一张真图,门槛判定直接失真)。"""
    from services.geo_douyin.settlement import card_is_ok
    assert card_is_ok({"status": "ready", "oss_key": "k"}) is True
    assert card_is_ok({"status": "ready", "oss_key": ""}) is False
    assert card_is_ok({"status": "failed", "oss_key": "k"}) is False


# ═════════════════════════════════════════════════════════════
# §15-2/3 结算账本(资金面 · 探针数次数)
# ═════════════════════════════════════════════════════════════
@dataclass
class _Ledger:
    committed: List[dict] = field(default_factory=list)
    released: List[dict] = field(default_factory=list)


@pytest.fixture
def ledger(monkeypatch):
    lg = _Ledger()

    async def fake_commit(**kw):
        lg.committed.append(kw)
        return {"ok": True}

    async def fake_release(**kw):
        lg.released.append(kw)
        return {"ok": True}

    monkeypatch.setattr("middleware.billing.commit_freeze", fake_commit)
    monkeypatch.setattr("middleware.billing.release_freeze", fake_release)
    return lg


def _install_post(monkeypatch, cards, *, task_status="completing", claimed=True):
    """装一套 DB 桩:post 的卡片 + 任务行 + 抢占结果。"""
    state = {"claims": 0, "post_status": []}

    def _claim(task_id):
        state["claims"] += 1
        # 真实语义:条件 UPDATE 只有第一次会命中
        if claimed and state["claims"] == 1:
            return {"freeze_id": 9001, "task_ref": "geo_douyin_post:1:x"}
        return None

    monkeypatch.setattr("db.geo_douyin_db.get_post",
                        lambda pid: {"id": pid, "cards": cards})
    monkeypatch.setattr("db.geo_douyin_db.get_task_by_post",
                        lambda pid: {"id": 55, "status": task_status})
    monkeypatch.setattr("db.geo_douyin_db.claim_task_settlement", _claim)
    monkeypatch.setattr("db.geo_douyin_db.set_post_status",
                        lambda pid, st: state["post_status"].append(st))
    return state


def test_incomplete_group_never_commits(monkeypatch, ledger):
    """🔴 变异对② 补齐中不得 commit、不得 release。
    还有卡没出来时,冻结必须**原封不动**躺着 —— 既不扣也不退。"""
    cards = _group(True, [True, False, True])          # 还差一张
    _install_post(monkeypatch, cards)
    from services.geo_douyin.settlement import try_settle
    verdict = asyncio.run(try_settle(1))
    assert verdict == "not_complete"
    assert ledger.committed == [], "补齐中却 commit 了 = 没补齐就扣钱"
    assert ledger.released == [], "补齐中却 release 了 = 用户的成品白丢"


def test_complete_group_commits_exactly_once(monkeypatch, ledger):
    """🔴 变异对③ 补齐恰好 commit 一次。
    补齐可能由系统自动重抽与用户手动重抽两条路触发,用户还能并发点 ——
    先查后改会两个都判"齐了"、两个都 commit = **扣两次钱**。"""
    cards = _group(True, [True, True, True])           # 全齐
    state = _install_post(monkeypatch, cards)
    from services.geo_douyin.settlement import try_settle

    v1 = asyncio.run(try_settle(1))
    v2 = asyncio.run(try_settle(1))                    # 模拟并发/重复触发
    v3 = asyncio.run(try_settle(1))
    assert v1 == "settled"
    assert (v2, v3) == ("already", "already")
    assert len(ledger.committed) == 1, \
        f"commit 了 {len(ledger.committed)} 次 —— 必须恰好一次"
    assert ledger.released == []
    assert state["claims"] == 3, "每次都该去抢占(抢不到才返回 already)"
    assert "ready" in state["post_status"]


def test_settlement_claim_is_conditional_update_not_read_then_write():
    """结构锁:抢占必须是**一条**带 WHERE 的 UPDATE,不能先 SELECT 再 UPDATE。

    🔴 断言打在 SQL 结构上:判定与占位分成两句,并发下必然重复 commit。
    """
    body = _fn_code(ROOT / "db" / "geo_douyin_db.py", "claim_task_settlement")
    sql = "\n".join(ln if ln.find("--") < 0 else ln[:ln.find("--")]
                    for ln in body.splitlines())
    assert "UPDATE geo_douyin_post_tasks" in sql
    assert "WHERE id = %s AND status = 'completing'" in sql, "抢占条件不在 UPDATE 里"
    assert "RETURNING" in sql, "抢不到要能区分出来"
    assert "SELECT" not in sql.upper(), "出现了先查后改 = 并发下会重复 commit"


def test_commit_failure_does_not_unclaim(monkeypatch, ledger):
    """必须不命中面:commit 抛异常时**不回滚抢占**。
    回滚会让下一次重试再抢一次、再 commit 一次 —— 反而制造重复扣款。
    冻结留在 frozen,12h sweeper 兜底(用户不吃亏)。"""
    cards = _group(True, [True, True, True])
    _install_post(monkeypatch, cards)

    async def boom(**kw):
        raise RuntimeError("billing down")

    monkeypatch.setattr("middleware.billing.commit_freeze", boom)
    from services.geo_douyin.settlement import try_settle
    assert asyncio.run(try_settle(1)) == "settled"
    # 再来一次必须抢不到(说明没回滚)
    assert asyncio.run(try_settle(1)) == "already"


# ═════════════════════════════════════════════════════════════
# §15-2 自动补齐不占用户额度(变异对④)
# ═════════════════════════════════════════════════════════════
def test_auto_fill_never_touches_user_quota():
    """🔴 变异对④ 自动重抽不扣用户额度。

    系统在补自己没做完的活,凭什么花用户那 10 次?
    判据打在**调用的是哪个函数**上:`_auto_fill` 必须走 render_one_card,
    绝不能走 redraw_one_card / bump_redraw_count(那两个才碰额度)。
    """
    body = _fn_code(PROD, "_auto_fill")
    assert "render_one_card" in body, "自动补齐没走生图入口"
    for banned in ("redraw_one_card", "bump_redraw_count", "REDRAW_LIMIT_PER_POST"):
        assert banned not in body, f"自动补齐碰了用户额度: {banned}"


def test_auto_fill_round_cap_is_two():
    from services.geo_douyin.settlement import AUTO_FILL_MAX_ROUNDS
    assert AUTO_FILL_MAX_ROUNDS == 2


def test_redraw_module_still_has_zero_billing():
    """🔴 §15 引入结算之后,重抽模块**仍然**一行计费代码都没有。
    结算走 API → settlement 的方向,不是 redraw → settlement。
    (这条是既有锁的延伸面:新功能最容易从这里破口。)

    🔴 判据必须同时看**模块名和导入的符号名**。变异实测:
       `from middleware import billing` 的 `node.module` 是 `"middleware"`,
       **不含 "billing"** —— 只查模块名的版本让这个变异活了下来。
    """
    tree = ast.parse(REDRAW.read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                imported.append(node.module)
            # ← 这一行是变异抓出来补的
            imported.extend(f"{node.module or ''}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    bad = [m for m in imported
           if "billing" in m or "wallet" in m or "settlement" in m]
    assert not bad, f"重抽模块引入了计费/结算模块: {bad}"


#: `commit_freeze` 允许出现的模块 —— **封闭集合**，加一个都要在这里显式登记。
#:
#: 🔴 [返工 2026-08-18] 新增 `contract_worker.py`。这不是"把锁放宽"，
#:    要说清它为什么仍然守得住：
#:
#:      · 原不变式是「**制作链**的 commit 只有一个出口」。它**仍然成立** ——
#:        合同链的制作走 `production_task.run_image_post_production(contract_task=...)`，
#:        commit 还是在 `production_task.py` 那一句，worker 不碰它；
#:      · `contract_worker` 里的 `commit_freeze` 结算的是**另一个资金对象**：
#:        `mhz_publish_order_items.freeze_id`（投放冻结），
#:        与制作任务的 `geo_douyin_post_tasks.freeze_id` 是两笔钱、两张表。
#:        投放链在此之前**根本没有 commit 出口**（只 freeze 不结算，
#:        正是 Codex P0-06 报的"资金闭环没成立"）。
#:
#:    所以这是「不变式仍成立、只是多了一条**新的**链」——
#:    按"搬家要改断言、不要退役"的口径，断言从"两个模块"改成"三个模块"，
#:    集合仍然封闭：第四个出口出现照样红。
ALLOWED_COMMIT_FREEZE_MODULES = {
    "production_task.py",      # 制作链成功路径（原有）
    "settlement.py",           # §15 部分成功结算（原有）
    "contract_worker.py",      # 投放链结算（返工 2026-08-18 新增，见上）
}


def _strip_comments_and_docstrings(src: str) -> str:
    """去掉注释行**与 docstring**,只留会执行的代码。

    🔴 [第 4 棒] 原来只去 `#` 注释。于是 `contract_seams.py` 的一句
       **docstring 病历**(讲"`commit_freeze` 返回 success=False 时怎么办")
       被当成了一个新的 commit 出口 —— 这条锁在告警一件不存在的事。

    🔴 **断言一个字没改、白名单一个也没加**:改的只是"什么算一次出现"。
       docstring 不会执行,它不可能是 commit 出口;而任何**真的**调用、
       乃至一句 `from middleware.billing import commit_freeze`,
       仍然在这条锁的射程里(裸串匹配保留,只是不再看 docstring)。
       —— 与其把文档改成不许写出函数名,不如让锁看对地方。
    """
    import ast as _ast

    lines = src.splitlines()
    drop: set[int] = set()
    try:
        tree = _ast.parse(src)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.Module, _ast.ClassDef, _ast.FunctionDef,
                                     _ast.AsyncFunctionDef)):
                continue
            body = list(getattr(node, "body", []) or [])
            if not body:
                continue
            first = body[0]
            if isinstance(first, _ast.Expr) and isinstance(first.value, _ast.Constant) \
                    and isinstance(first.value.value, str):
                end = getattr(first, "end_lineno", first.lineno) or first.lineno
                drop.update(range(first.lineno, end + 1))
    return "\n".join(ln for i, ln in enumerate(lines, start=1)
                     if i not in drop and not ln.strip().startswith("#"))


def test_settlement_is_the_only_extra_commit_site():
    """必须不命中面：除了登记在案的三处，任何模块都不许 commit。
    多一个 commit 出口就多一条重复扣款的可能。"""
    hits = []
    for f in (ROOT / "services" / "geo_douyin").glob("*.py"):
        code = _strip_comments_and_docstrings(f.read_text(encoding="utf-8"))
        if "commit_freeze" in code:
            hits.append(f.name)
    assert set(hits) == ALLOWED_COMMIT_FREEZE_MODULES, \
        f"commit_freeze 出现在意料之外的模块: {hits}"


def test_the_commit_site_lock_still_sees_a_real_new_outlet():
    """锁的自证:上面那条放宽了"什么算一次出现",必须证明它**没被放松**。

    往一个不在白名单里的模块**真加一句调用**(内存里改,不落盘),
    这条锁必须能看见它。看不见 = 上面的 docstring 豁免开得太大。
    """
    fake_module = ("\"\"\"这段 docstring 里提到 commit_freeze,不该算数。\"\"\"\n"
                   "def f():\n"
                   "    return 1\n")
    assert "commit_freeze" not in _strip_comments_and_docstrings(fake_module), \
        "docstring 豁免没生效"

    real_call = ("\"\"\"docstring 提 commit_freeze。\"\"\"\n"
                 "from middleware.billing import commit_freeze\n"
                 "async def f():\n"
                 "    return await commit_freeze(freeze_id=1)\n")
    assert "commit_freeze" in _strip_comments_and_docstrings(real_call), \
        "真调用被 docstring 豁免一并吃掉了 —— 锁被放松了,不是看对地方"


def test_production_commit_site_is_still_exactly_one():
    """🔴 上面那条放宽之后必须补的**窄断言**：制作链的 commit 出口仍然唯一。

    没有这一条，「三个模块」会被读成「worker 也可以结制作链的钱」——
    而那正是原不变式要挡的。这里把原命题**原样保留**在它真正的作用域上。
    """
    worker = (ROOT / "services" / "geo_douyin" / "contract_worker.py").read_text(
        encoding="utf-8")
    code = "\n".join(ln for ln in worker.splitlines()
                     if not ln.strip().startswith("#"))
    assert "run_publish_submit_once" in code, "投放提交入口不见了 —— 判据失去分母"
    head, tail = code.split("async def run_publish_submit_once", 1)
    assert "commit_freeze(" not in head, (
        "contract_worker 在投放提交之外的地方 commit —— 制作链的钱不该由 worker 结")
    assert "commit_freeze(" in tail, "投放链没有 commit 出口 —— 资金闭环又断了"


# ═════════════════════════════════════════════════════════════
# §15-4 占位与下标对齐(补不齐时交还用户重抽的前提)
# ═════════════════════════════════════════════════════════════
def test_failed_card_keeps_index_slot():
    """🔴 失败卡在 oss_keys 里必须留**空串占位**。
    删掉会让后面所有卡下标整体前移 —— 用户点"重抽第 3 张"会抽到第 4 张。"""
    from services.geo_douyin.settlement import pending_indices
    cards = _group(True, [True, False, True])
    assert pending_indices(cards) == [2], "待补下标算错"
    # 占位语义:失败卡的 oss_key 是空串而不是被移除
    assert cards[2]["oss_key"] == ""
    assert len(cards) == 5


def test_completing_state_is_not_stalled_and_not_active():
    """补齐中:球在用户那边,可以合法地停很久(12h sweeper 才是死线)。
    → 不能判成"卡住了"(UI 会说中断),也不该继续轮询(白刷)。"""
    from services.geo_douyin.task_progress import describe_task_progress
    out = describe_task_progress(
        {"status": "completing", "stage": "images",
         "progress_done": 4, "progress_total": 5,
         "started_at": 1.0, "updated_at": 1.0, "created_at": 1.0},
        now=1.0 + 86400)           # 隔了一天
    assert out["state"] == "completing"
    assert out["stalled"] is False, "补齐中被判成卡住 → UI 会误报中断"
    assert out["active"] is False, "补齐中还继续轮询 = 白刷"


def test_entry_page_shows_completing_badge():
    """前端要认这个新状态 —— 已经有成品了,绝不能显示成失败。"""
    # [WO_271] 改指后继:补齐中的作品在详情页明说「还有 N 张没出来，补齐了再发」;
    #   左栏列表里它归「制作中」(keyword_worklist 把 completing 归 making),绝不落进「没做成」。
    import re
    src = DETAIL.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?<!:)//[^\n]*", "", src)
    assert "post?.status === 'completing'" in src and "补齐了再发" in src, "没有给补齐中的条目任何说明"
    worklist = WORKLIST.read_text(encoding="utf-8")
    assert re.search(r'status in \{"generating", "completing"\}:\s*state = "making"', worklist), \
        "列表里补齐中不再归「制作中」—— 已经有成品了,绝不能显示成失败"
