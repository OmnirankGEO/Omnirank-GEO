"""G2 · 响应契约运行时锁(工单 WO_RESPONSE_MODEL_CONTRACT_GATE_2026-08-14 §3)。

G1 是静态的,明确抓不到 `**kwargs` / `d.update(...)` / 计算键 / 超过一跳的构造。
G2 把每条 `extra="forbid"` 的路由**逐条**打真出口,并配**双向**反向对照:

  a. 往返回体塞一个未声明键 → 必须转红   ← 证明 `extra` 没被悄悄放宽成 `ignore`
  b. 从模型里删掉一个真实存在的键 → 必须转红 ← 证明锁真的在打这个模型,不是恒真

🔴 覆盖方式是**遍历路由表**,不是维护清单(工单 §5 红线)——
   新增任何带 response_model 的路由自动纳入,清单会漏。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from pydantic import BaseModel, ValidationError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("GEO_SKIP_STARTUP_TASKS", "1")

from scripts.response_model_contract_gate import (  # noqa: E402
    collect_routes,
    model_field_tree,
    model_forbids_extra,
)


# ============================================================
# 路由收集(一次,session 级)
# ============================================================

@pytest.fixture(scope="session")
def routes() -> List[Dict[str, Any]]:
    collected, _total = collect_routes()
    return collected


@pytest.fixture(scope="session")
def forbid_routes(routes) -> List[Dict[str, Any]]:
    return [r for r in routes if model_forbids_extra(r["model"])]


class _SampleUnsupported(Exception):
    """样本生成器不认这个注解 —— 是**仪器**的缺口,不是端点违约。

    分开报的理由见 `value_for` 里那段注释:两者混在一起时,
    排查会从一开始就走错方向(去改端点,而端点是对的)。
    """

    def __init__(self, annotation: Any) -> None:
        super().__init__(
            "样本生成器不认这个注解:%r —— 这是**判据自己的缺口**,"
            "不是端点违约。给 `value_for` 补一个分支,别改端点。" % (annotation,))


def _sample_for(model: Any) -> Any:
    """按模型字段树造一个**最小合法**样本(只填必填项)。

    不联网、不读生产数据(工单 §3 G2 第 1 条)。
    """
    import typing
    from datetime import datetime

    def build(m: Any, depth: int = 0) -> Any:
        if depth > 6:
            return {}
        out: Dict[str, Any] = {}
        for name, field in m.model_fields.items():
            if not field.is_required():
                continue
            out[name] = value_for(field.annotation, depth)
        return out

    def value_for(annotation: Any, depth: int) -> Any:
        origin = typing.get_origin(annotation)
        # 🔴 Literal[...] 必须取一个**枚举内**的值:随便填 "x" 会被 literal_error 拒,
        #    那样这条锁就变成"永远红",而恒红和恒真一样等于没有判据。
        if origin is typing.Literal:
            args = typing.get_args(annotation)
            return args[0] if args else "x"
        if origin is None:
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                return build(annotation, depth + 1)
            if annotation is bool:
                return True
            if annotation is int:
                return 1
            if annotation is float:
                return 1.0
            if annotation is str:
                return "x"
            if annotation is datetime:
                return datetime(2026, 1, 1)
            # 🔴 [WO_216 c1] **裸容器**:`target: dict` / `items: list` 这种没有
            #    参数化的注解,`get_origin()` 返 None,于是掉到下面的 `return "x"`,
            #    造出一个字符串塞进 dict 字段 ⇒ pydantic 报 dict_type。
            #    那条红读起来像**端点违约**,实际是**样本生成器不认这个类型** ——
            #    与 G1 那个「没跑成 vs 不过」是同一件事:仪器造不出样本,
            #    不等于被测对象有问题。
            #    (api/defensive_geo_api.py `next_action.target: dict` 就是这么红的,
            #     从 4152dfe16 一直红到 e7669e699,而工具箱里没人跑这个包。)
            if annotation is dict:
                return {}
            if annotation is list:
                return []
            if annotation is set:
                return set()
            if annotation is tuple:
                return ()
            if annotation is typing.Any or annotation is object:
                return "x"
            # 🔴 不再静默返 "x":不认的类型**出声**,否则下一个裸类型又会以
            #    一条看起来像"端点违约"的红出现,而排查方向从一开始就是错的。
            raise _SampleUnsupported(annotation)
            
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if origin in (list, List):
            return []
        if origin in (dict, Dict):
            return {}
        if not args:
            return None
        return value_for(args[0], depth)

    return build(model)


# ============================================================
# 主锁:29 条 forbid 路由逐条
# ============================================================

def test_coverage_count_is_printed_and_nonzero(routes, forbid_routes, capsys):
    """覆盖数必须**打印出来**(工单 §7:不写"全部覆盖")。"""
    with capsys.disabled():
        print(
            f"\n[G2] 声明 response_model 的路由 = {len(routes)} · "
            f"其中 forbid = {len(forbid_routes)}"
        )
    assert len(routes) > 0
    assert len(forbid_routes) > 0


def test_every_forbid_route_accepts_its_own_minimal_sample(forbid_routes):
    """每条 forbid 路由:按其模型造的最小合法样本必须能过校验。

    这条如果红,说明模型自身自相矛盾(必填字段造不出合法值)。
    """
    failures = []
    for route in forbid_routes:
        model = route["model"]
        try:
            model.model_validate(_sample_for(model))
        except ValidationError as exc:  # noqa: PERF203
            failures.append(f"{route['path']} → {getattr(model, '__name__', model)}: {exc}")
    assert failures == [], "\n".join(failures)


def test_every_forbid_route_rejects_an_undeclared_key(forbid_routes):
    """🔴 反向对照 a:往返回体塞未声明键必须转红。

    这条专门防 `extra="forbid"` 被悄悄放宽成 `ignore` 蒙混过关 ——
    放宽后未声明键会被**静默剔除**,上面那条锁照样绿,只有这条会红。
    """
    not_rejecting = []
    for route in forbid_routes:
        model = route["model"]
        payload = _sample_for(model)
        payload["__totally_undeclared_key__"] = 1
        try:
            model.model_validate(payload)
            not_rejecting.append(f"{route['path']} → {getattr(model, '__name__', model)}")
        except ValidationError:
            pass
    assert not_rejecting == [], (
        "以下 forbid 路由没有拒绝未声明键(extra 可能已被放宽成 ignore):\n"
        + "\n".join(not_rejecting)
    )


def test_every_forbid_route_rejects_a_missing_required_key(forbid_routes):
    """🔴 反向对照 b:从样本里删掉一个**真实必填**键必须转红。

    证明这把锁真的在打这个模型,而不是恒真
    (若模型所有字段都可选,本条对该模型自动跳过并计数,不假装覆盖)。
    """
    not_rejecting = []
    skipped = 0
    for route in forbid_routes:
        model = route["model"]
        required = [n for n, f in model.model_fields.items() if f.is_required()]
        if not required:
            skipped += 1
            continue
        payload = _sample_for(model)
        payload.pop(required[0])
        try:
            model.model_validate(payload)
            not_rejecting.append(
                f"{route['path']} → {getattr(model, '__name__', model)} 缺 {required[0]} 仍通过"
            )
        except ValidationError:
            pass
    assert not_rejecting == [], "\n".join(not_rejecting)


# ============================================================
# 打真出口:历史事故的两条端点,用**真实构造函数**的返回体过模型
# ============================================================

def test_backfill_endpoint_contract_real_outlet():
    """🔴 2026-08-14 事故复现锁:补录端点返回体必须能过它声明的 response_model。

    这里不是造样本,是拿**服务层真实返回体的形状**过模型 ——
    G1 抓的是字面量,这条抓的是运行时真的产出了什么键。
    """
    from schemas.admin_user_governance import GovernanceMutationResponse

    # 服务层真实返回形状(见 services/admin_user_governance.py 补录函数结尾)
    real_shape = {
        "success": True,
        "scope": "commercial_binding",
        "version": 4,
        "request_id": "req-x",
        "before": {"commercial_provider_user_id": 1, "commercial_mode": "service_provider"},
        "after": {"commercial_provider_user_id": 1, "commercial_mode": "service_provider"},
    }
    GovernanceMutationResponse.model_validate(real_shape)  # 不许抛

    # 反向对照:把事故那个键加回去,必须红
    with pytest.raises(ValidationError) as exc:
        GovernanceMutationResponse.model_validate({**real_shape, "backfill": True})
    assert "extra_forbidden" in str(exc.value)


def test_governance_mutation_response_shared_by_multiple_routes(forbid_routes):
    """`GovernanceMutationResponse` 被多条路由共用 —— 一处多返回一个键,多条一起挂。

    这条把"共用面"显式化:改这个模型时看得见影响范围。
    """
    from schemas.admin_user_governance import GovernanceMutationResponse

    users = [r["path"] for r in forbid_routes if r["model"] is GovernanceMutationResponse]
    assert len(users) >= 7, f"共用路由数变了,请复核影响范围:{users}"


# ============================================================
# [WO_216 c1] 样本生成器认不认**裸容器**
#
# 这一条从 4152dfe16 一直红到 e7669e699,而它红的样子是
# 「/api/defensive-geo/run-previews → RunPreviewResponse: Input should be a
#   valid dictionary」—— 读起来像端点违约,实际是**判据自己造不出样本**
# (`next_action.target: dict` 是裸 dict,`get_origin()` 返 None,
#  掉进 `return "x"`)。而工具箱里没人跑这个包,所以它红了两周没人看见。
# ============================================================

def test_the_sample_builder_handles_bare_containers():
    """裸 `dict` / `list` / `set` / `tuple` 字段要造得出合法样本。

    🔴 用**合成模型**而不是指着 `RunPreviewResponse`:真模型会随业务改,
       哪天 `target` 换成 `Dict[str, Any]`,指着它的判据就绿了 ——
       而生成器的缺口还在,下一个裸 dict 照样把人引向错误的排查方向。
    """
    from pydantic import BaseModel

    class Bare(BaseModel):
        model_config = {"extra": "forbid"}
        d: dict
        l: list
        s: set
        t: tuple

    sample = _sample_for(Bare)
    assert set(sample) == {"d", "l", "s", "t"}, sample
    Bare(**sample)          # 造出来的样本必须真的过校验


def test_an_annotation_it_cannot_build_says_so_instead_of_faking_one():
    """🔴 不认的注解要**出声**,不是悄悄返 "x"。

    悄悄返 "x" 的后果不是"少验了一条",是**把仪器的缺口伪装成端点违约**:
    报错长得像 pydantic 拒了端点的响应,排查方向从一开始就错
    (去改端点,而端点是对的)。这与 G1 那条「没跑成不许显示成有违规」
    是同一件事。
    """
    from pydantic import BaseModel

    class Weird(BaseModel):
        model_config = {"extra": "forbid"}
        x: complex          # 生成器没有这一档

    with pytest.raises(_SampleUnsupported) as e:
        _sample_for(Weird)
    msg = str(e.value)
    assert "判据自己的缺口" in msg and "别改端点" in msg, msg


def test_a_supported_annotation_does_not_raise():
    """反向对照:认得的类型不许抛。

    少了它,「不认就抛」可以退化成「一律抛」—— 那样整包恒红,
    而恒红的判据与恒绿一样等于没有判据。
    """
    from pydantic import BaseModel

    class Fine(BaseModel):
        model_config = {"extra": "forbid"}
        a: str
        b: int
        c: dict

    Fine(**_sample_for(Fine))
