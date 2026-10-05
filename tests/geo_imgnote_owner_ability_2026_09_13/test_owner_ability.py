"""#193 判据 O1–O4 —— 组织 owner 必须能发图文。

现场:0913a 上线后 QA 服务商(组织 **owner**)点发布 ⇒ 403 ABILITY_NOT_GRANTED,
而销售成员反而能发。根因是**同一个谓词两处实现**:组织模型里 owner 的能力快照
刻意为空(权限来自"他就是 owner"),全仓其它地方都按 is_owner 绕,
唯独这里直接回快照 ⇒ 中间件放行、第二道拒绝。
"""
import pytest


class _Identity:
    def __init__(self, is_owner, capabilities=()):
        self.is_owner = is_owner
        self.capabilities = frozenset(capabilities)
        self.principal_user_id = 1


class _Req:
    def __init__(self, identity=None, is_admin=False):
        class _S:
            pass
        self.state = _S()
        self.state.organization_identity = identity
        self.state.user = {"user_id": 1, "is_admin": is_admin}


def _abilities(identity=None, is_admin=False):
    from api.geo_image_note_api import _granted_abilities
    return _granted_abilities(_Req(identity, is_admin))


# ── O1 owner 放行(快照为空也放行)────────────────────────────────────
def test_o1_owner_with_empty_snapshot_is_granted():
    """owner 的能力快照**刻意为空**(organization_service.py:2226-2227)。

    所以"回快照"对 owner 恒等于"什么都没有" —— 这正是线上那个 403。
    """
    from api.geo_image_note_api import ABILITY_PRODUCE, ABILITY_PUBLISH

    got = _abilities(_Identity(is_owner=True, capabilities=()))
    assert ABILITY_PRODUCE in got and ABILITY_PUBLISH in got


# ── O2 无能力成员仍然拒 ───────────────────────────────────────────────
def test_o2_member_without_the_ability_is_still_denied():
    """🔴 反向对照:修法不能变成"有组织就放行"。

    少了这条,把 `if identity is not None: return 全集` 也能让 O1 绿,
    而那等于把成员能力体系整个关掉。
    """
    from api.geo_image_note_api import ABILITY_PRODUCE, ABILITY_PUBLISH

    got = _abilities(_Identity(is_owner=False, capabilities=(ABILITY_PRODUCE,)))
    assert ABILITY_PRODUCE in got
    assert ABILITY_PUBLISH not in got, "无 publish 能力的成员被放行了"


# ── O3 有能力成员 / 无组织服务商 回归 ─────────────────────────────────
def test_o3_member_with_ability_and_solo_agent_unchanged():
    from api.geo_image_note_api import ABILITY_PRODUCE, ABILITY_PUBLISH

    member = _abilities(_Identity(is_owner=False,
                                  capabilities=(ABILITY_PRODUCE, ABILITY_PUBLISH)))
    assert {ABILITY_PRODUCE, ABILITY_PUBLISH} <= member

    solo = _abilities(None)                      # 无组织的服务商
    assert {ABILITY_PRODUCE, ABILITY_PUBLISH} <= solo

    admin = _abilities(None, is_admin=True)      # 平台权限
    assert {ABILITY_PRODUCE, ABILITY_PUBLISH} <= admin


# ── O4 七个调用点走同一个谓词(一个谓词一处)──────────────────────────
def test_o4_every_ability_guard_goes_through_one_predicate():
    """七个 `_guard(ability=)` 端点必须共用同一只能力解析器。

    本单的病因就是"同一谓词两处实现"。如果将来有人在某个端点上另算一次能力,
    这条会红 —— 而那正是复发的形状。
    """
    import io
    import pathlib
    import re

    src = io.open(pathlib.Path("api/geo_image_note_api.py"), encoding="utf-8").read()
    call_sites = re.findall(r"_guard\(request, action=\"[^\"]+\", ability=([A-Z_]+)\)", src)
    assert len(call_sites) == 7, "调用点数变了(%d)—— 分母变了就要重新确认" % len(call_sites)

    # `granted_abilities=` 只能由 `_granted_abilities(...)` 供给
    suppliers = re.findall(r"granted_abilities=([^,\n]+)", src)
    assert suppliers, "没找到能力注入点 —— 锚过期了"
    for s in suppliers:
        assert s.strip() == "_granted_abilities(request)", (
            "有人另算了一次能力:%s —— 同一谓词两处实现正是本单的病因" % s.strip())
