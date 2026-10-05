"""adapter 注册表 + 平台矩阵 + Kimi 退出默认闸 + 只读健康聚合。

§3.1 / §6.4 铁律:
  - 新售默认全量矩阵不含 Kimi(任何默认轮 Kimi 调用次数严格为 0)。
  - 已售订单快照含 Kimi 时按订单权益履约;``default_platform_matrix`` 与
    ``order_platform_entitlements`` 是两个显式对象,新默认值不得追溯削减旧订单。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from pydantic import BaseModel, ConfigDict

from services.ai_surface_monitoring.adapters.base import BaseSurfaceAdapter
from services.ai_surface_monitoring.adapters.openai_compat import OpenAICompatibleAdapter
from services.ai_surface_monitoring.adapters.wrapped import WrappedResearchAdapter
from services.ai_surface_monitoring.contracts import SurfaceHealth
from services.ai_surface_monitoring.lineage import SURFACE_SPECS, get_surface_spec, resolve_runtime_surface
from services.ai_surface_monitoring.policy import ObservationPolicyReader, ObservationPolicySnapshot


class SurfaceMatrixRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface_key: str
    platform_key: str
    product_label: str
    enabled: bool               # 该表面是否为 policy 为其平台选定(经运行时替换解析后)实跑的表面
    default_matrix: bool        # 是否属于本包声明的默认四核
    legacy_read_only: bool
    base_weight_bps: int
    availability: str
    canonical_monitoring_platform: str
    # 复审 P1-4:该表面若是"被替换成的运行时表面",记录被替换来源(policy 原选表面);否则 None
    substituted_from: Optional[str] = None


class SurfaceMatrix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_version: str
    rows: List[SurfaceMatrixRow]


class OrderSurfaceConflictError(RuntimeError):
    """同一订单对**同一规范平台**含多个 surface → fail-closed(不按字母序擅自选一个;复审#2-R6)。"""


class OrderSurfaceUnavailableError(RuntimeError):
    """订单快照的 surface 现已 unavailable → 进入**人工替换/补偿流程**,绝不静默换通道(复审#2-R6)。"""


@dataclass(frozen=True)
class OrderPlatformEntitlement:
    """已售订单快照携带的**表面级**权益(复审 P1-6:不可变 surface_key 快照,非平台级)。

    绑定具体 surface_key(而非平台):只放行订单显式买过的那个表面,防止"订单含某平台 → 该平台
    其它/未启用/代理表面也放行"。最多用于历史 Kimi(other_explicit)明确兼容。
    """

    surface_keys: Set[str] = field(default_factory=set)

    def includes(self, surface_key: str) -> bool:
        return surface_key in self.surface_keys


# 与默认矩阵分离的显式对象:新售默认(不含 Kimi)
def default_platform_matrix(policy: ObservationPolicyReader) -> Set[str]:
    """从策略读出的新售默认启用平台集合(enabled=True 的 platform_key)。"""
    snap = policy.get_policy()
    return {p.platform_key for p in snap.platforms if p.enabled}


class AdapterRegistry:
    """管理 surface_key → adapter 实例;并提供矩阵 / 权益闸 / 健康。"""

    def __init__(
        self,
        policy: ObservationPolicyReader,
        adapters: Optional[Dict[str, BaseSurfaceAdapter]] = None,
    ):
        self._policy = policy
        self._adapters: Dict[str, BaseSurfaceAdapter] = adapters if adapters is not None else _build_default_adapters()

    @property
    def policy(self) -> ObservationPolicyReader:
        return self._policy

    # ---- adapter 访问 ----
    def get_adapter(self, surface_key: str) -> BaseSurfaceAdapter:
        adapter = self._adapters.get(surface_key)
        if adapter is None:
            raise KeyError(f"未注册 surface_key: {surface_key!r}")
        return adapter

    def registered_surfaces(self) -> List[str]:
        return list(self._adapters.keys())

    # ---- 策略读取:传 snapshot 复用同一份,避免同请求二次读 + 快照混用(复审 P2) ----
    def _snap(self, snapshot: Optional[ObservationPolicySnapshot] = None) -> ObservationPolicySnapshot:
        return snapshot if snapshot is not None else self._policy.get_policy()

    def runtime_chosen_by_platform(self, snapshot: Optional[ObservationPolicySnapshot] = None) -> Dict[str, str]:
        """platform → **运行时实跑**表面(经 resolve_runtime_surface 替换解析)。"""
        chosen = self._snap(snapshot).enabled_surface_by_platform()
        return {p: resolve_runtime_surface(sk)[0] for p, sk in chosen.items()}

    # ---- 权益闸 + 策略 surface 精确路由 + 运行时替换(P1-1/P1-4/P1-6) ----
    def is_surface_allowed(
        self,
        surface_key: str,
        entitlement: Optional[OrderPlatformEntitlement] = None,
        *,
        snapshot: Optional[ObservationPolicySnapshot] = None,
    ) -> bool:
        """LIVE 放行闸(复审#2-R6-2 P1-3:**live 绝不替换**)。只有"策略为该平台**直接选定**且本身
        active、非 legacy 的那个表面"无条件放行;其它必须 **surface 级**订单权益(且 active)。

        与 R6 的区别:branch1 用 policy **直接** chosen(``enabled_surface_by_platform``,不经
        ``resolve_runtime_surface`` 替换)。若当前 policy 选了不可用表面(如 deepseek_native_with_search),
        其替换目标(legacy)在 live 下**不被放行**(替换只用于只读回放,见 get_surface_matrix)。
        """
        spec = get_surface_spec(surface_key)
        direct_chosen = self._snap(snapshot).enabled_surface_by_platform()  # policy 原选,不替换
        # branch 1:该表面正是 policy 直接为其平台选定、且本身 active 非 legacy → 放行(无 live 替换)
        if (
            direct_chosen.get(spec.platform_key) == surface_key
            and spec.availability == "active"
            and not spec.legacy_read_only
        ):
            return True
        # branch 2:surface 级权益 + 该表面 active(绑 surface_key,不绑 platform;不可用表面绝不放行)
        return bool(entitlement and entitlement.includes(surface_key) and spec.availability == "active")

    def default_sampling_surfaces(self, snapshot: Optional[ObservationPolicySnapshot] = None) -> List[str]:
        """新售默认全量轮 LIVE 要跑的 surface_key = policy 每个启用平台**直接选定**、本身 active、非 legacy
        且已注册 adapter 的表面。

        复审#2-R6-2 P1-3:**live 不替换**。policy 选了不可用表面的平台在 live 下**不实跑**(而非替换成 legacy);
        该情况由 ``readiness`` 报 ``attention_required`` 提示 AI-2 直接把 policy 改成真实运行表面。替换
        (``resolve_runtime_surface``)只用于**只读回放 / 矩阵展示**(见 ``get_surface_matrix.substituted_from``)。
        """
        chosen = self._snap(snapshot).enabled_surface_by_platform()
        out: List[str] = []
        for platform_key, sk in chosen.items():
            spec = SURFACE_SPECS.get(sk)
            if spec is None or spec.legacy_read_only:
                continue  # 未知 / Kimi 永不进默认轮
            if spec.availability != "active":
                continue  # 当前 policy 选了不可用表面 → live 不替换、不实跑;readiness 报 attention_required
            if sk in self._adapters:
                out.append(sk)
        return out

    def sampling_surfaces_for_order(
        self, entitlement: OrderPlatformEntitlement,
        snapshot: Optional[ObservationPolicySnapshot] = None,
    ) -> List[str]:
        """已售订单要跑的 surface_key = **严格按不可变订单快照执行**(复审#2-R6-2 P1-1/P1-4)。

        铁律:有订单快照时**恰好**跑订单快照里的表面(经校验),**绝不**再追加当前默认矩阵未购买的平台
        (否则历史订单会跑未购买平台 → 额外未定价 API 成本 + 违反不可变权益)。默认矩阵只在**创建新订单**时
        物化进快照(见 ``materialize_new_order_surfaces``);此后执行只读快照。

        全 fail-closed(P1-4:未知/冲突/不可用/缺 adapter 统一不静默):
          - 未知 surface(非本包已知)→ ``OrderSurfaceUnavailableError``(不静默丢弃/换默认)。
          - 同订单同平台多 surface → ``OrderSurfaceConflictError``(不按字母序擅自选)。
          - surface 现已 unavailable → ``OrderSurfaceUnavailableError``(进人工替换/补偿,不静默换通道)。
          - active 但本 registry 缺 adapter(注册不一致)→ ``OrderSurfaceUnavailableError``。
        """
        order_by_platform: Dict[str, str] = {}
        for sk in sorted(entitlement.surface_keys):  # sorted 仅为确定性错误信息,不用于"擅自选一个"
            spec = SURFACE_SPECS.get(sk)
            if spec is None:
                raise OrderSurfaceUnavailableError(
                    f"订单 surface {sk!r} 未知(非本包已知表面);fail-closed,不静默丢弃/回落默认矩阵")
            plat = spec.canonical_monitoring_platform
            if plat in order_by_platform:
                raise OrderSurfaceConflictError(
                    f"订单对平台 {plat!r} 含多个 surface({order_by_platform[plat]!r} / {sk!r});"
                    f"fail-closed,不擅自选一个")
            if spec.availability != "active":
                raise OrderSurfaceUnavailableError(
                    f"订单 surface {sk!r} 现已不可用(availability={spec.availability!r});"
                    f"进入人工替换/补偿流程,不静默换通道")
            if sk not in self._adapters:
                raise OrderSurfaceUnavailableError(
                    f"订单 surface {sk!r} 为 active 但本 registry 未注册 adapter(注册不一致);"
                    f"无法运行 → fail-closed,不静默用默认表面替代")
            order_by_platform[plat] = sk
        # 复审#2-R6-3-R5 P1-2:**空快照 = 损坏订单**(付费订单却无任何可跑 surface)→ fail-closed,
        # 绝不当作"正常完成"(空权益 + 空批次此前零异常零结果)。
        if not order_by_platform:
            raise OrderSurfaceUnavailableError(
                "订单不可变快照为空(无任何可跑 surface)= 损坏订单;拒绝采集,不当作正常完成")
        # **严格快照**:不追加默认矩阵未购买平台
        return sorted(order_by_platform.values())

    def materialize_new_order_surfaces(
        self, extra_surface_keys: Optional[Set[str]] = None,
        snapshot: Optional[ObservationPolicySnapshot] = None,
    ) -> List[str]:
        """**创建新订单**时物化订单快照 surface 集 = 当前默认矩阵(live 直接 chosen,不替换)∪ 加购表面。
        **加购(``extra_surface_keys``)只新增默认未覆盖的规范平台**(复审#2-R6-3-R9):其规范平台若已被默认矩阵
        或其它加购覆盖 → fail-closed(``OrderSurfaceConflictError``),**绝不删除/替换默认已购通道**;换通道需**独立、
        明确的 replacement 契约**(绑定报价/确认/快照)。产出写入**不可变订单快照**;此后执行只用 ``sampling_surfaces_for_order``。

        与 R6 之前"执行期并集默认"的关键区别:默认矩阵只在**此处(创建期)**物化进快照,**执行期不再引用默认**。
        加购表面 fail-closed(未知/不可用/缺 adapter → raise),与执行期口径一致。

        复审#2-R6-2 收敛:创建期**同样受 readiness 闸约束**(与 live 默认轮对称)。当前 policy 非 ready 时
        ``default_sampling_surfaces`` 会**静默剔除**选了不可用表面的已启用平台(其 live fail-open 由 service 层
        readiness 闸兜底);但创建期若照此产出,会把**降级矩阵永久烙进不可变订单快照**(客户按四路付费却长期少一路,
        零信号)。故非 ready 直接 fail-closed,拒绝创建,不产出降级快照。
        """
        snap = self._snap(snapshot)
        rd = self.readiness(snap)
        if not rd.get("ready", False):
            raise OrderSurfaceUnavailableError(
                f"当前 policy readiness={rd.get('status')!r} 非 ready → 拒绝创建新订单快照"
                f"(默认矩阵不完整会把降级集烙进不可变快照;需 AI-2 先把 policy 改成真实运行表面);"
                f"problems={rd.get('problems')} substitutions={rd.get('substitutions')}")
        # 复审#2-R6-2-R2 防御:**独立**校验默认部分覆盖全部 enabled 非 legacy 平台条目(不依赖 readiness 是
        # 完美代理)。任一 enabled 非 legacy 条目的选定表面被默认轮剔除(legacy/不可用/缺 adapter)→ fail-closed,
        # 绝不把降级集(少一路)烙进不可变订单快照。
        default_part = self.default_sampling_surfaces(snap)
        default_surface_set = set(default_part)
        missing = sorted(p.platform_key for p in snap.platforms
                         if p.enabled and not p.legacy_read_only and p.surface_key not in default_surface_set)
        if missing:
            raise OrderSurfaceUnavailableError(
                f"默认矩阵未覆盖已启用平台 {missing}(其选定表面被默认轮剔除);拒绝创建降级订单快照")
        by_platform: Dict[str, str] = {}
        for sk in default_part:
            canon = get_surface_spec(sk).canonical_monitoring_platform
            if canon in by_platform:
                # 复审#2-R6-3-R7:默认部分对同一规范平台含多个 surface = canonical 冲突 → **fail-loud**
                # (与执行路径 sampling_surfaces_for_order 的 OrderSurfaceConflictError 对齐,绝不 canonical 去重静默丢一路)。
                raise OrderSurfaceConflictError(
                    f"默认矩阵对规范平台 {canon!r} 含多个 surface({by_platform[canon]!r} / {sk!r})= canonical 冲突;"
                    f"拒绝创建订单快照(不静默丢一路;与执行路径 fail-loud 对齐)")
            by_platform[canon] = sk
        for sk in sorted(extra_surface_keys or set()):
            spec = SURFACE_SPECS.get(sk)
            if spec is None or spec.availability != "active" or sk not in self._adapters:
                raise OrderSurfaceUnavailableError(
                    f"加购 surface {sk!r} 未知/不可用/缺 adapter;创建订单 fail-closed(不静默丢弃)")
            canon = spec.canonical_monitoring_platform
            if canon in by_platform:
                # 复审#2-R6-3-R9 P1-1:加购(extra_surface_keys)**只新增默认未覆盖的平台**,其规范平台若已被
                # 默认矩阵**或其它加购**覆盖 → fail-closed。加购绝不删除/静默替换默认已购通道(此前 by_platform[canon]=sk
                # 会把 deepseek 默认 legacy 静默换成加购 native)——换通道需**独立、明确的 replacement 契约**(绑定报价/确认/快照)。
                raise OrderSurfaceConflictError(
                    f"加购 surface {sk!r} 的规范平台 {canon!r} 已被默认矩阵/其它加购覆盖({by_platform[canon]!r})="
                    f"冲突;加购只新增未覆盖平台,不得删除/替换默认已购通道(换通道需独立 replacement 契约);fail-closed")
            by_platform[canon] = sk
        return sorted(by_platform.values())

    def readiness(self, snapshot: Optional[ObservationPolicySnapshot] = None) -> Dict[str, object]:
        """开闸前 readiness。**复审#2-R6 DeepSeek 裁定**:三态 status。

        - ``blocked``:某启用平台的 policy 选表面无可用 API 且无可用替换,或运行时表面未注册 adapter → 硬阻断。
        - ``attention_required``:**当前 policy** 选了不可用表面、要靠运行时替换才实跑(如仍写
          ``deepseek_native_with_search`` → 替换成 legacy)。替换只应用于**读历史 policy**;当前 policy 应
          直接改成真实运行表面。此态**不报 fully ready**(``ready=False``),提示 AI-2 修 policy version。
        - ``ready``:每个启用平台的 policy 选表面本身就是 active 运行时表面,无需替换。

        复审#2-R6-2-R2:``ready`` 必须 ⟺ **默认矩阵覆盖全部 enabled 非 legacy 平台**。故新增:启用平台选了
        **legacy 只读表面**(如把 other_explicit 授给真实平台条目)→ ``blocked``。否则 readiness 报 ready 但
        ``default_sampling_surfaces`` 会静默剔除该 legacy 表面 → materialize/live 默认轮少一路(fail-open)。
        """
        snap = self._snap(snapshot)
        chosen = snap.enabled_surface_by_platform()
        problems: List[str] = []
        substitutions: List[str] = []
        # 复审#2-R6-3 P1-2 防御:无任何可跑的启用平台 → blocked(policy 校验器已在构造期挡下全 disabled,
        # 此为防御:即便某快照绕过校验器,readiness 也绝不对"零平台"报 ready)。
        if not chosen:
            problems.append("无任何 enabled 平台可跑 → 不能开闸")
        canon_by: Dict[str, str] = {}   # 复审#2-R6-3-R7:规范平台 → 启用条目;检测 canonical 冲突
        for platform_key, sk in chosen.items():
            runtime_sk, sub = resolve_runtime_surface(sk)
            spec = SURFACE_SPECS.get(runtime_sk)
            if spec is None or spec.availability != "active":
                problems.append(f"platform {platform_key}: policy 选 {sk!r} 无可用 API,亦无可用替换 → 不能实跑")
            elif spec.legacy_read_only:
                problems.append(
                    f"platform {platform_key}: 选定运行时表面 {runtime_sk!r} 是 legacy 只读表面,不能作为启用平台的 live 表面"
                    f"(会被默认轮静默剔除 → 少一路);policy 应为该平台改选真实 live 表面")
            elif runtime_sk not in self._adapters:
                problems.append(f"platform {platform_key}: 运行时表面 {runtime_sk!r} 未注册 adapter")
            elif sub:
                substitutions.append(
                    f"platform {platform_key}: 当前 policy 选 {sk!r} 不可用,运行时替换为 {runtime_sk!r};"
                    f"当前 policy 应直接改为真实运行表面(替换仅用于读历史 policy)")
            else:
                # 复审#2-R6-3-R7:两个启用平台条目映射到**同一规范平台** = canonical 冲突(默认轮双采样 +
                # materialize canonical 去重丢一路);真实校验器由唯一 platform_key + 归属挡下,此为防御(绕过快照)。
                canon = spec.canonical_monitoring_platform
                if canon in canon_by and canon_by[canon] != platform_key:
                    problems.append(
                        f"多个启用平台条目({canon_by[canon]} / {platform_key})映射同一规范平台 {canon} = canonical 冲突"
                        f"(默认轮双采样 / materialize 丢一路)→ 不能开闸")
                canon_by[canon] = platform_key
        if problems:
            status = "blocked"
        elif substitutions:
            status = "attention_required"
        else:
            status = "ready"
        return {"ready": status == "ready", "status": status, "problems": problems,
                "substitutions": substitutions, "policy_version": snap.policy_version}

    # ---- 矩阵 ----
    def get_surface_matrix(self, snapshot: Optional[ObservationPolicySnapshot] = None) -> SurfaceMatrix:
        snap = self._snap(snapshot)
        weight_by_platform = {p.platform_key: p.base_weight_bps for p in snap.platforms}
        chosen = snap.enabled_surface_by_platform()          # platform → policy 原选 surface
        runtime_chosen = {p: resolve_runtime_surface(sk)[0] for p, sk in chosen.items()}  # → 运行时实跑
        # 每个运行时表面被哪个 policy 原选替换而来(用于 substituted_from)
        sub_from = {resolve_runtime_surface(sk)[0]: sk for sk in chosen.values() if resolve_runtime_surface(sk)[1]}
        rows: List[SurfaceMatrixRow] = []
        for sk, spec in SURFACE_SPECS.items():
            is_enabled = (runtime_chosen.get(spec.platform_key) == sk) and not spec.legacy_read_only
            rows.append(SurfaceMatrixRow(
                surface_key=sk,
                platform_key=spec.platform_key,
                product_label=spec.product_label,
                enabled=is_enabled,
                default_matrix=spec.default_enabled,
                # 复审#2-R6-3-R11 P2:legacy_read_only **只读 lineage**(surface spec),绝不用 policy 平台标记覆盖
                # (lineage 是 surface legacy 性质的唯一真相;此前 `or policy 平台 legacy` 会把 qwen 误标历史只读)。
                legacy_read_only=spec.legacy_read_only,
                base_weight_bps=weight_by_platform.get(spec.platform_key, 0),
                availability=spec.availability,
                canonical_monitoring_platform=spec.canonical_monitoring_platform,
                substituted_from=sub_from.get(sk),
            ))
        return SurfaceMatrix(policy_version=snap.policy_version, rows=rows)

    # ---- 只读健康 ----
    async def get_adapter_health(self, only_active: bool = True) -> List[SurfaceHealth]:
        """并发探针所有(active)adapter。只读反映真实 provider/model/surface,禁写回 policy。"""
        targets = [
            (sk, a) for sk, a in self._adapters.items()
            if (not only_active) or get_surface_spec(sk).availability == "active"
        ]

        async def _probe(sk: str, a: BaseSurfaceAdapter) -> SurfaceHealth:
            try:
                return await a.probe()
            except Exception as exc:
                spec = get_surface_spec(sk)
                from services.ai_surface_monitoring.contracts import utcnow
                return SurfaceHealth(
                    platform_key=spec.platform_key,
                    provider_key=spec.provider_key,
                    model_key=spec.default_model_key,
                    surface_key=sk,  # type: ignore[arg-type]
                    status="unknown",
                    checked_at=utcnow(),
                    error_code=f"probe_exception:{type(exc).__name__}",
                )

        return list(await asyncio.gather(*[_probe(sk, a) for sk, a in targets]))


def _build_default_adapters() -> Dict[str, BaseSurfaceAdapter]:
    """构造默认 adapter 集合(真实 fetch/http;测试用注入的 registry 或 adapters 覆盖)。"""
    adapters: Dict[str, BaseSurfaceAdapter] = {}
    # 包裹既有 research 引擎
    for sk in ("doubao_ark_api_search", "qwen_dashscope_search",
               "deepseek_dashscope_search_legacy", "other_explicit"):
        adapters[sk] = WrappedResearchAdapter(sk)
    # 新建 OpenAI 兼容
    adapters["yuanbao_hy3_tokenhub"] = OpenAICompatibleAdapter("yuanbao_hy3_tokenhub", verify_model_echo=True)
    # 复审 P1-5:deepseek 官方也严格校回显(不同模型不得混进同一时间序列)
    adapters["deepseek_native_no_search"] = OpenAICompatibleAdapter(
        "deepseek_native_no_search", verify_model_echo=True,
    )
    # 结构保留但无自动可用性的表面:用一个恒 unavailable 的占位 adapter,供矩阵/健康展示
    for sk in ("deepseek_native_with_search", "deepseek_metaso_proxy", "tencent_wsa_search",
               "yuanbao_app_verified", "manual_app_capture"):
        adapters[sk] = _UnavailableAdapter(sk)
    return adapters


class _UnavailableAdapter(BaseSurfaceAdapter):
    """无自动可用性的表面占位:collect 抛错(禁止路由),probe 恒 unavailable。"""

    async def _fetch(self, request, prompt_text):  # noqa: ANN001
        raise RuntimeError(f"surface {self.surface_key} 不可用(availability={self.spec.availability});禁止路由")
