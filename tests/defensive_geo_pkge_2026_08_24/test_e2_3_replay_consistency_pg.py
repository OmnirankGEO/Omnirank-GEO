"""【E2-3 = Codex 二审 P1-F3】重放核一致性 + Owner 裁定「冻结前动态、冻结后钉死」。

Owner 2026-08-26 拍板:**动态**。
  · 确认 → 冻结之间平台账号配置可变,新冻结用**当时**的配置;
  · 已冻结的单,commit/release 必须打**冻结行上那个账号**,永不改道。
所以本文件不去把物理账号钉进合同 —— 那是被否掉的方向。要钉的是:
  ① 重放推导与已存预算不符 ⇒ **转人工**,不许静默复用(方向无关,Codex P1-F3);
  ② 那两条口径本身有判据在守(冻结后改配置不改道 / 冻结前改配置走新账号)。

为什么①是真洞
--------------
``publish/store.get_budget`` 的键是
``(tenant_owner_id, accepted_snapshot_id, service_projection_id, scope_key)``
—— **不含 hash**。所以一份 hash 不同的旧预算照样会被找到。修之前那一段是:

    existing = _store.get_budget(...)
    if existing is None:
        _store.insert_budget(...)          # ← existing 非空时什么都不比

于是重放会拿着**旧**预算 `mark_materialized`,而返回体里的
`executionBudgetSnapshotId` 是**新算的那个** —— 两者指向不同的行,
下游按返回值去查预算会查到一份从没落库的快照。
"""
from __future__ import annotations

import inspect
import re

import pytest

from tests.defensive_geo_pkge_2026_08_24 import _seed

pytestmark = pytest.mark.integration


def _one_pending_row(cur):
    from services.defensive_geo import activation_materializer as _mat
    rows = _mat._claim_pending(cur, limit=1)
    assert rows, "没有待物化的 outbox 行 —— 夹具没造起来,判据会因为错误的原因绿"
    return rows[0]


def _budget_of(cur, row):
    from services.defensive_geo import activation_materializer as _mat
    from services.defensive_geo.publish import execution_budget_policy as _policy
    from services.defensive_geo.publish import store as _store

    identity = _mat.frozen_identity(row)
    draft = _policy.derive(
        cur, tenant_owner_id=int(row["tenant_owner_id"]),
        accepted_snapshot_id=int(row["accepted_snapshot_id"]),
        funding_policy=identity["payer_funding_policy"],
        payer_user_id=identity["payer_user_id"],
    )
    return draft, _store.get_budget(
        cur, tenant_owner_id=int(row["tenant_owner_id"]),
        accepted_snapshot_id=int(row["accepted_snapshot_id"]),
        service_projection_id=draft.service_projection_id, scope_key=draft.scope_key)


def test_e2_3_replay_against_a_drifted_budget_goes_to_human_not_silent_reuse(db):
    """🔴 已存预算与本次推导不符 ⇒ 转人工。

    造法:先真物化一次(签出预算),再把库里那份预算的 ``budget_hash`` 改掉
    (= "上一次是用另一份推导签的"这一态),然后拿同一条 outbox 行再物化一次。

    修之前:`existing` 非空就什么都不比 —— 静默复用旧预算并 mark_materialized,
    返回体却报着新算的 snapshot id。
    """
    from services.defensive_geo import activation_materializer as _mat

    _seed.install_base_rows()
    _seed.seed_accepted_snapshot(publications=2, activate=True)

    # 🔴 seed 是在**它自己的连接**上提交的;db 夹具这条连接可能早已开着事务,
    #    那个快照里没有刚落的 outbox 行。先 rollback 拿一个新快照再 claim ——
    #    否则 _claim_pending 取空,判据会因为「夹具没造起来」而红,
    #    而不是因为被测行为(我第一版就这么红的)。
    db.rollback()
    cur = db.cursor()
    row = _one_pending_row(cur)
    first = _mat.materialize_one(cur, row)
    assert first["executionBudgetSnapshotId"], first

    # 把已存预算的指纹改掉 —— 模拟「上一次是用另一份推导签的」
    cur.execute(
        "UPDATE defgeo_provider_execution_budgets SET budget_hash = %s "
        "WHERE execution_budget_snapshot_id = %s",
        ("e2drift" + "0" * 57, first["executionBudgetSnapshotId"]))
    assert cur.rowcount == 1, "没改到那一行 —— 这条判据没造出漂移态"

    with pytest.raises(_mat._NeedsHuman) as err:
        _mat.materialize_one(cur, row)
    assert "budget_hash" in str(err.value), (
        "转人工了,但理由不是 budget_hash 漂移:%s" % err.value)


def test_e2_3_replay_against_an_identical_budget_is_still_idempotent(db):
    """🔴 配对的必须不命中:预算**没漂**时重放必须照常成功。

    少了它,一个「existing 非空一律转人工」的实现也能让上面那条绿 ——
    而那样每一次正常重放(worker 重启 / 一条 outbox 被重新 claim)都会进人工队列,
    比原来的洞更贵。
    """
    from services.defensive_geo import activation_materializer as _mat

    _seed.install_base_rows()
    _seed.seed_accepted_snapshot(publications=2, activate=True)

    # 🔴 seed 是在**它自己的连接**上提交的;db 夹具这条连接可能早已开着事务,
    #    那个快照里没有刚落的 outbox 行。先 rollback 拿一个新快照再 claim ——
    #    否则 _claim_pending 取空,判据会因为「夹具没造起来」而红,
    #    而不是因为被测行为(我第一版就这么红的)。
    db.rollback()
    cur = db.cursor()
    row = _one_pending_row(cur)
    first = _mat.materialize_one(cur, row)
    again = _mat.materialize_one(cur, row)
    assert again["executionBudgetSnapshotId"] == first["executionBudgetSnapshotId"], (
        "同一份推导重放却得到了不同的预算 id:%r vs %r" % (again, first))
    assert again["created"] is False, "重放又签了一张新预算:%r" % again


def test_e2_3_the_budget_hash_deliberately_excludes_the_physical_account(db):
    """Owner 裁定「冻结前动态」的**结构证据**:物理账号不在预算指纹里。

    这一条不是装饰:如果哪天有人把 ``payer_user_id`` 塞进 canonical,
    「确认之后平台账号配置一变,已签预算就对不上」就会重新出现 ——
    而那正是被 Owner 否掉的「静态锁定」方向。方向变了要**先改判据**,
    不该靠改实现悄悄改口径。
    """
    import inspect

    from services.defensive_geo.publish import execution_budget_policy as _policy

    src = inspect.getsource(_policy.derive)
    body = src.split('"""', 2)[-1]          # 去掉 docstring,只看代码
    canonical_start = body.index("hashlib.sha256")
    canonical = body[canonical_start:canonical_start + 900]
    assert "fundingPolicy" in canonical, (
        "资金方向不在指纹里 —— 那改资金方向就不会换预算 id 了")
    assert "payerUserId" not in canonical and "payer_user_id" not in canonical, (
        "物理/租户账号被塞进了预算指纹 —— 那是 Owner 否掉的「静态锁定」方向,"
        "要改口径请先来改这条判据:%s" % canonical[:200])


#: 「物理账号在冻结那一刻现取」这句声明的两种写法。Owner 裁定②的交付物**就是这句话**。
_FRESH_LOOKUP_PHRASES = ("物理账号冻结时现取", "冻结那一刻现取")

#: 会把那句话整个**反过来**的词。外选 MUT-EXTE2-10 用的就是 ``**并非**``。
_NEGATORS = ("并非", "而非", "而不是", "不是", "并不", "没有", "未")


def _declares_fresh_lookup(text: str) -> bool:
    """这段源码里有没有**正面**声明「物理账号在冻结那一刻现取」。

    🔴 [外选 MUT-EXTE2-10] 子串在场是不够的:
       ``**并非**在**冻结那一刻现取**的`` 照样含那个子串。
       所以谓词看的是**那一句**:按中文句读 + 换行切句,取包含该短语的那一句,
       句内出现任何否定词即判 False。
       (同族:secret_string_cannot_be_substring_of_allowed_label ——
        「在场」从来证明不了「说的是这个意思」。)

    单点实现:下面两条判据共用这一个谓词。同一谓词写两处,必有一处没人验。
    """
    for phrase in _FRESH_LOOKUP_PHRASES:
        for sentence in re.split(r"[。;;\n]", text):
            if phrase in sentence and not any(neg in sentence for neg in _NEGATORS):
                return True
    return False


def test_e2_3_no_declaration_still_claims_the_payer_wallet_is_locked():
    """按裁定②:预算/outbox 不许再声称锁定物理付款账号,而要**正面**写明现取。

    谓词打在**文档串**上是有意的 —— 这一条要守的正是「那句话」。
    行为面由上面几条与发布链既有判据守;这里守的是**声明**,
    因为 Owner 的裁定②就是「删除/更正字段语义与声明」。

    🔴 [外选 MUT-EXTE2-10 收口] 上一版的谓词是 ``"冻结那一刻现取" in text`` ——
       **子串在场**。把那句话改成 ``**并非**在**冻结那一刻现取**的``,
       声明与行为(publish_funding 真现取)就完全相反了:它会把下一个读代码的人
       往被 Owner 否掉的「静态锁定」方向带,而这条锁一声不吭地绿着。
       现在换成**否定式免疫**的谓词,并由下一条判据自证它真的免疫。
    """
    from services.defensive_geo import activation_materializer as _mat
    from services.defensive_geo.publish import execution_budget_policy as _policy

    for mod in (_mat, _policy):
        assert _declares_fresh_lookup(inspect.getsource(mod)), (
            "%s 里没有一处**正面**明示「物理账号冻结时现取」—— "
            "要么这句话被删了,要么被写成了否定式。裁定②要求注释正面写清楚"
            % mod.__name__)


@pytest.mark.parametrize("module_path", [
    "services.defensive_geo.activation_materializer",
    "services.defensive_geo.publish.execution_budget_policy",
])
def test_e2_3_the_declaration_lock_is_immune_to_a_negated_sentence(module_path):
    """判别力自证:把那句话改成否定式 / 换掉,谓词必须判 **False**。

    这一条点名它守的规则:**在场 ≠ 说的是这个意思**。
    正样本逐字照搬外选 MUT-EXTE2-10 那一手(在短语前插 ``**并非**``)——
    谓词要是还判 True,上面那条声明锁就是在守空气,而它自己不会告诉你。

    两个模块都跑:声明锁的作用域是两处,自证的作用域就得是同样两处
    (只证一处 = 另一处的谓词从没被验过)。
    """
    import importlib

    mod = importlib.import_module(module_path)
    real = inspect.getsource(mod)
    assert _declares_fresh_lookup(real), "真源码就判不过 —— 先修上面那条"

    anchor = "在**冻结那一刻现取**"
    assert real.count(anchor) == 1, (
        "正样本的锚点在 %s 里命中 %d 次(要求 1)—— 锚点过期,这条自证会恒真地绿"
        % (module_path, real.count(anchor)))

    negated = real.replace(anchor, "**并非**" + anchor)
    assert not _declares_fresh_lookup(negated), (
        "把声明改成否定式之后谓词还判 True —— 这正是**子串在场**锁的经典失效:"
        "否定句照样含那个子串。外选 MUT-EXTE2-10 走的就是这条缝")

    replaced = real.replace(anchor, "由确认时刻锁定")
    assert not _declares_fresh_lookup(replaced), (
        "把那句话整个换成被 Owner 否掉的说法之后谓词还判 True —— "
        "谓词命中的不是那句话")


# ══════════════════════════════════════════════════════════════════════════
# Owner 裁定① 的两条口径钉子:冻结前动态 / 冻结后钉死
#
# 这两条钉的是**行为已经对了**这件事(Review 通告:command.payer_user_id 链路已是如此)。
# 钉住它的理由不是怀疑现状,是怕将来有人"顺手统一"成现取 ——
# 那一改会让**已冻结的单改道**:钱冻在 A 账号上,commit 却去打 B,
# 按 (freeze_id, user_id) 定位不到 → 结不掉 → 冻结长挂。
# 这个后果与本仓 A-1 那个 P0 一模一样,只是换到了发布链。
# ══════════════════════════════════════════════════════════════════════════
def test_e2_3_settlement_targets_the_account_recorded_at_freeze_time(monkeypatch):
    """冻结**之后**改平台账号配置 ⇒ 结算仍打**冻结行上**那个账号。

    结构证据 + 行为证据两层:
      · 结构:结算原语的 ``user_id`` 必须来自 ``command`` 那一行,
        不许是任何形式的"现取"(env / 配置函数);
      · 行为:改掉配置之后再取 command,它上面那个 payer 一个字不变。
    """
    import ast
    import inspect
    import textwrap

    from services.defensive_geo.publish import publish_funding as _pf

    src = textwrap.dedent(inspect.getsource(_pf))
    tree = ast.parse(src)

    # 找结算原语那一处调用,看它的 user_id 实参从哪来
    targets = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "primitive":
            continue
        for kw in node.keywords:
            if kw.arg == "user_id":
                targets.append(ast.dump(kw.value))
    assert len(targets) == 1, (
        "结算原语的调用点不是恰好一处(%d)—— 分母塌了,这条锁在守空气" % len(targets))
    dumped = targets[0]
    assert "command" in dumped, (
        "结算的 user_id 不是从 command 那一行取的:%s —— "
        "一旦改成现取,已冻结的单会改道:钱冻在 A、commit 去打 B,"
        "按 (freeze_id, user_id) 定位不到就永远结不掉" % dumped[:200])
    for forbidden in ("_platform_service_user_id", "getenv", "environ"):
        assert forbidden not in dumped, (
            "结算的 user_id 里出现了「现取」的痕迹(%s):%s" % (forbidden, dumped[:200]))


def test_e2_3_a_new_freeze_uses_the_configuration_in_force_at_freeze_time():
    """冻结**之前**改配置 ⇒ 新单走**新**账号(这就是「动态」那一半)。

    结构证据:平台腿的冻结实参必须是**现取**的那个函数的返回值,
    而不是任何被冻进快照/预算的值。
    Owner 否掉了「把物理账号钉进不可变合同」的方向,所以这一条与上一条
    **必须同时成立** —— 少了任何一条,口径就滑向另一半。
    """
    import ast
    import inspect
    import textwrap

    from services.defensive_geo.publish import publish_funding as _pf

    src = textwrap.dedent(inspect.getsource(_pf))
    tree = ast.parse(src)
    fresh = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            name = getattr(node.value.func, "id", None) or getattr(node.value.func, "attr", None)
            if name == "_platform_service_user_id":
                fresh.append([t.id for t in node.targets if isinstance(t, ast.Name)])
    assert fresh and fresh[0] == ["platform_uid"], (
        "平台腿的账号不再是冻结那一刻现取的(%r)—— 那是被 Owner 否掉的静态方向;"
        "要改口径请先来改这条判据" % fresh)

    # 且它确实被当作 freeze_points 的收款账号用了(不是取了不用)
    used = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "freeze_points"
            and any(isinstance(a, ast.Name) and a.id == "platform_uid" for a in n.args)]
    assert used, "现取了平台账号却没拿它去冻 —— 取了不用等于没取"
