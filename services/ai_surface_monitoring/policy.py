"""消费 AI-2 业务策略配置(``geo_observation_policy``)· Protocol + fake(Codex #7)。

铁律:
  - AI-1 **只消费**已发布策略,**不拥有**存储/CAS/admin API/migration/审计/env override/epoch;
    这些全归 AI-2。本包**不建第二套配置 SSOT**。
  - 用 Protocol + fake 消费,**不等待** AI-2 最终函数名;集成阶段用真实实现替换 fake。
  - 实时 provider/model 健康**不是**可写配置;由 AI-1 的 platform_health DTO 只读产出,禁止写回 policy。

字段与机器契约 ``policy_schema`` 对齐(platforms / source_base_weights_bps / sampling_budget /
k 匿名 / 保留期 / anomaly / feature_flags)。AI-1 实际消费:平台矩阵(enabled/weight/surface)、
sampling_budget、feature_flags、source_base_weights。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.ai_surface_monitoring.contracts import SurfaceKey
from services.ai_surface_monitoring.lineage import SURFACE_SPECS, default_matrix_surfaces


class PlatformPolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform_key: str
    enabled: bool
    base_weight_bps: int = Field(ge=0, le=10000)  # 契约 platform_policy_fields '0..10000'(复审#2-R6-3-R3:字段级约束,与 budget/source_weights 平齐)
    surface_key: Optional[SurfaceKey] = None  # enabled 时必填;legacy 只读目录行可空
    legacy_read_only: bool = False

    @model_validator(mode="after")
    def _enabled_requires_surface(self):
        # 契约:enabled 平台必须指定 surface_key(否则该平台会被静默从默认轮丢掉;复审 finding 9)。
        # 复审#2-R6-3-R5 订正:契约的"surface_key 可空"**仅限 legacy 只读目录行(即 disabled)**;
        # **enabled 平台一律必须有 surface_key**(含 enabled legacy)——否则声称 N 路、订单实际少一路(静默少交付)。
        if self.enabled and not self.surface_key:
            raise ValueError(
                f"platform {self.platform_key!r}: enabled=True 必须指定 surface_key(契约要求;legacy 目录行可空仅限 disabled)")
        return self


class SourceBaseWeightsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 机器契约 policy_schema.source_base_weight_fields 各 constraint '0..10000'(复审#2-R6-3-R3:与
    # sampling_budget 平齐落 Field 约束;此前只对齐字段名不校验范围)。
    research: int = Field(default=10000, ge=0, le=10000)
    paid_diagnosis: int = Field(default=4000, ge=0, le=10000)
    monitoring: int = Field(default=7000, ge=0, le=10000)


class SamplingBudgetV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 机器契约 policy_schema.sampling_budget_fields 约束(复审#2-R6-2 / R6-3-R2:**四个字段全部**落约束,
    # 此前只查了 max_calls_per_round → 其余三个畸形值 0/负被静默接受,daily 上限按字面比较使 try_reserve 恒
    # 返回 None → 全站静默停采)。<=0/负 → 构造期 fail-closed loud 拒绝,消除"<=0=不限 vs 全阻断"跨字段歧义。
    max_calls_per_round: int = Field(default=5000, gt=0)                # 契约 ">0"
    max_calls_per_day: int = Field(default=20000, gt=0)                # 契约 ">0"
    max_cost_micros_per_day: int = Field(default=1_000_000_000, gt=0)  # 契约 ">0"(默认 ¥1000/日)
    max_retry_calls_per_request: int = Field(default=2, ge=0)          # 契约 ">=0"


class ObservationFeatureFlagsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingest_enabled: bool = False
    promotion_enabled: bool = False
    aggregation_enabled: bool = False
    product_enabled: bool = False


class ObservationPolicySnapshot(BaseModel):
    """AI-2 发布的一份策略快照(AI-1 只读)。

    字段必须**逐项覆盖机器契约 policy_schema.writable_fields**(11 项),否则 AI-2 的完整合法
    policy 输入本模型会因 extra='forbid' 报 extra_forbidden(复审 P1-6)。治理类 6 字段
    (max_single_brand_share_bps / public_min_* / retention_days / anomaly_*)AI-1 不消费,但必须容纳。
    """

    model_config = ConfigDict(extra="forbid")

    policy_version: str
    platforms: List[PlatformPolicyV1]
    source_base_weights_bps: SourceBaseWeightsV1
    sampling_budget: SamplingBudgetV1
    # —— 以下治理字段 AI-1 只容纳、不消费(归 AI-2/AI-3),但必须存在以消费完整 policy ——
    max_single_brand_share_bps: int = 1000
    public_min_independent_brands: int = 3
    public_min_source_types: int = 2
    retention_days: int = 180
    anomaly_confirmation_numerator: int = 2
    anomaly_confirmation_denominator: int = 3
    feature_flags: ObservationFeatureFlagsV1

    @model_validator(mode="after")
    def _validate_ranges_and_sums(self):
        # 复审#2-R6-3 P1-2:**完整执行**机器契约 policy_schema.checks(此前只查了 <=10000,漏了唯一/
        # 至少一个启用/恰等 10000/正权重/归属 → 畸形 policy 被签成 ready、materialize 出降级快照)。
        for p in self.platforms:
            if not (0 <= p.base_weight_bps <= 10000):
                raise ValueError(f"platform {p.platform_key}: base_weight_bps {p.base_weight_bps} 超 0..10000")
        # 契约 check①:platform_key 唯一
        keys = [p.platform_key for p in self.platforms]
        if len(keys) != len(set(keys)):
            dup = sorted({k for k in keys if keys.count(k) > 1})
            raise ValueError(f"platform_key 必须唯一(契约),重复:{dup}")
        # 契约 check②:**enabled 平台**(含 enabled legacy;复审#2-R6-3-R5)需 surface_key(PlatformPolicyV1 已查)
        # + **正** base_weight_bps。契约"enabled platform requires non-null surface_key and positive base_weight_bps"
        # 不对 legacy 豁免;仅 disabled legacy 目录行可 surface=null/weight=0。
        for p in self.platforms:
            if p.enabled and p.base_weight_bps <= 0:
                raise ValueError(f"platform {p.platform_key}: enabled 平台 base_weight_bps 必须 > 0(契约,含 enabled legacy)")
        # 契约 check③:至少一个非 legacy 平台 enabled
        if not any(p.enabled and not p.legacy_read_only for p in self.platforms):
            raise ValueError("至少一个非 legacy 平台必须 enabled(契约 checks)")
        # 契约 check④:全目录 base_weight_bps 合计**恰**等于 10000(不是 <=)
        total = sum(p.base_weight_bps for p in self.platforms)
        if total != 10000:
            raise ValueError(f"平台 base_weight_bps 全目录合计必须恰等于 10000,实为 {total}(契约)")
        # 完整性(复审 P1-2 明列):enabled 平台选定 surface 的**归属平台**必须 == 该平台条目
        #   (防"doubao/qwen/deepseek/yuanbao 全指向 qwen surface"或把 legacy 表面授给真实平台的畸形 policy)
        for p in self.platforms:
            if p.enabled and p.surface_key:
                sspec = SURFACE_SPECS.get(p.surface_key)
                if sspec is not None and sspec.platform_key != p.platform_key:
                    raise ValueError(
                        f"platform {p.platform_key}: 选定 surface {p.surface_key!r} 归属平台 "
                        f"{sspec.platform_key!r},归属不符(契约完整性)")
        # 复审#2-R6-3-R9 P1-2:``legacy_read_only`` 双 SSOT 一致性——policy 条目与 lineage SURFACE_SPECS 对该 surface
        #   的 legacy_read_only 定义必须完全一致(**两方向**不一致都拒绝)。防"policy 标 legacy 只读却仍进 live 采集"
        #   (readiness/default_sampling 用 surface spec 判 legacy,忽略 policy 字段 → 假 ready)或反向借非 legacy 冒充。
        #   lineage 为该 surface legacy 性质的唯一真相。
        for p in self.platforms:
            if p.surface_key:
                sspec = SURFACE_SPECS.get(p.surface_key)
                if sspec is not None and bool(sspec.legacy_read_only) != bool(p.legacy_read_only):
                    raise ValueError(
                        f"platform {p.platform_key}: policy.legacy_read_only={p.legacy_read_only} 与 surface "
                        f"{p.surface_key!r} 定义 legacy_read_only={sspec.legacy_read_only} 不一致(lineage 为准;双 SSOT 须一致)")
            else:
                # 复审#2-R6-3-R11 P2:**surface_key=null 目录行也必须按 lineage 校验**——该平台是否**确属 legacy**
                #   (SURFACE_SPECS 中该 platform_key 是否有 legacy 只读 surface)。防"disabled qwen + null + legacy=true"
                #   伪造历史只读(此前只在 surface 非空时校验 → null 目录行绕过 → 矩阵误标 qwen 历史只读)。
                platform_is_legacy = any(
                    s.legacy_read_only for s in SURFACE_SPECS.values() if s.platform_key == p.platform_key)
                if bool(p.legacy_read_only) != platform_is_legacy:
                    raise ValueError(
                        f"platform {p.platform_key}: surface_key=null 目录行 legacy_read_only={p.legacy_read_only} 与 "
                        f"lineage 该平台 legacy 性质({platform_is_legacy})不符(lineage 为准;双 SSOT 须一致)")
        if not (0 <= self.max_single_brand_share_bps <= 10000):
            raise ValueError("max_single_brand_share_bps 超 0..10000")
        if self.public_min_independent_brands < 3:
            raise ValueError("public_min_independent_brands 必须 >= 3")
        if self.public_min_source_types < 2:
            raise ValueError("public_min_source_types 必须 >= 2")
        if self.retention_days <= 0:
            raise ValueError("retention_days 必须 > 0")
        if self.anomaly_confirmation_numerator <= 0:
            raise ValueError("anomaly_confirmation_numerator 必须 > 0")
        if self.anomaly_confirmation_denominator < self.anomaly_confirmation_numerator:
            raise ValueError("anomaly_confirmation_denominator 必须 >= numerator")
        return self

    def enabled_platform_map(self) -> Dict[str, PlatformPolicyV1]:
        return {p.platform_key: p for p in self.platforms if p.enabled}

    def enabled_surface_by_platform(self) -> Dict[str, str]:
        """策略选定的"每个启用平台的表面"(P1-1:surface_key 由 policy 精确指定,非代码默认)。"""
        out: Dict[str, str] = {}
        for p in self.platforms:
            if p.enabled and p.surface_key:
                out[p.platform_key] = p.surface_key
        return out


class ObservationPolicyReader(Protocol):
    """AI-1 消费策略的唯一接口。集成阶段由 AI-2 的真实实现替换 fake。"""

    def get_policy(self) -> ObservationPolicySnapshot:
        """返回当前已发布策略快照。读失败必须 raise → 调用方 fail-closed(不用无法证明新鲜的缓存)。"""
        ...


# ---------------------------------------------------------------------------
# 确定性权重重归一(§8.3):全目录 base 可合计 10000;运行时对 enabled 且有权益的集合重归一到 10000
# ---------------------------------------------------------------------------


def renormalize_to_10000(base_weights_bps: Dict[str, int]) -> Dict[str, int]:
    """把一组 base weight(bps)确定性重归一到合计 10000。

    - 输入为空或总和<=0 → 返回空(调用方须处理"无可用平台")。
    - 用最大余数法保证整数和恰为 10000,且对相同输入幂等、与 dict 顺序无关(按 key 排序打破平局)。
    任何平台因故障/权益被禁用时,不得留下"启用平台权重和 < 10000 却假称万分制"。
    """
    items = [(k, int(v)) for k, v in base_weights_bps.items() if int(v) > 0]
    total = sum(v for _, v in items)
    if not items or total <= 0:
        return {}
    # 先按比例取整,再用最大余数法补足到 10000
    raw = []
    allocated = 0
    for k, v in items:
        exact = v * 10000 / total
        floor = int(exact)
        raw.append([k, floor, exact - floor])
        allocated += floor
    shortfall = 10000 - allocated
    # 余数大者优先;平局按 key 升序(确定性)
    raw.sort(key=lambda x: (-x[2], x[0]))
    for i in range(shortfall):
        raw[i % len(raw)][1] += 1
    return {k: w for k, w, _ in raw}


# ---------------------------------------------------------------------------
# fake:默认新售矩阵(Kimi off / 元宝 Hy3 on)+ §8.3 默认权重
# ---------------------------------------------------------------------------


def _default_platforms() -> List[PlatformPolicyV1]:
    """从 lineage SURFACE_SPECS 派生默认平台矩阵(default_enabled 决定 enabled)。

    权重:四核默认 doubao 3500 / qwen 2500 / deepseek 2500 / yuanbao 1500(合计 10000);
    Kimi enabled=False,base_weight_bps=0,legacy_read_only=True(退出默认,历史只读)。
    """
    weight_by_platform = {"doubao": 3500, "qwen": 2500, "deepseek": 2500, "yuanbao": 1500}
    seen: set = set()
    out: List[PlatformPolicyV1] = []
    # 默认矩阵表面
    for sk in default_matrix_surfaces():
        spec = SURFACE_SPECS[sk]
        if spec.platform_key in seen:
            continue
        seen.add(spec.platform_key)
        out.append(PlatformPolicyV1(
            platform_key=spec.platform_key,
            enabled=True,
            base_weight_bps=weight_by_platform.get(spec.platform_key, 0),
            surface_key=sk,
            legacy_read_only=False,
        ))
    # Kimi(other_explicit)显式 legacy off
    out.append(PlatformPolicyV1(
        platform_key="kimi",
        enabled=False,
        base_weight_bps=0,
        surface_key=None,
        legacy_read_only=True,
    ))
    return out


class FakeObservationPolicy:
    """内存 fake:仅测试/dev 显式注入使用(生产由 AI-2 真实 reader 替换)。

    ``fail_reads=True`` 模拟策略不可读以验证 fail-closed。功能开关**默认全 OFF**(生产安全);
    测试需采集时传 ``ingest_enabled=True``。``platforms`` 可覆盖以验证策略 surface 精确路由(P1-1)。
    """

    def __init__(
        self,
        snapshot: Optional[ObservationPolicySnapshot] = None,
        fail_reads: bool = False,
        *,
        ingest_enabled: bool = False,
        platforms: Optional[List[PlatformPolicyV1]] = None,
        sampling_budget: Optional[SamplingBudgetV1] = None,
    ):
        self._snapshot = snapshot or ObservationPolicySnapshot(
            policy_version="fake-surface-matrix-v1",
            platforms=platforms if platforms is not None else _default_platforms(),
            source_base_weights_bps=SourceBaseWeightsV1(),
            sampling_budget=sampling_budget or SamplingBudgetV1(),
            feature_flags=ObservationFeatureFlagsV1(
                ingest_enabled=ingest_enabled,
                promotion_enabled=False,
                aggregation_enabled=False,
                product_enabled=False,
            ),
        )
        self.fail_reads = fail_reads

    def get_policy(self) -> ObservationPolicySnapshot:
        if self.fail_reads:
            raise RuntimeError("simulated policy read failure")
        return self._snapshot


def budget_limits_from_policy(snapshot: ObservationPolicySnapshot):
    """从策略快照派生 cost_policy.BudgetLimits(每日 micros/调用上限 + 重试上限)。"""
    from services.ai_surface_monitoring.cost_policy import BudgetLimits

    sb = snapshot.sampling_budget
    return BudgetLimits(
        max_cost_micros_per_day=int(sb.max_cost_micros_per_day) if sb.max_cost_micros_per_day is not None else None,
        max_calls_per_day=int(sb.max_calls_per_day) if sb.max_calls_per_day is not None else None,
        max_retry_calls_per_request=int(sb.max_retry_calls_per_request or 0),
        max_calls_per_round=int(sb.max_calls_per_round) if sb.max_calls_per_round is not None else None,
    )
