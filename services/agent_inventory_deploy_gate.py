"""代理进货快照部署/回滚安全门。

决策函数无 DB、Docker 或文件副作用；蓝绿 shell 只负责采集可验证事实。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Optional, Sequence

from services.agent_inventory_pricing import INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY


LEGACY_INVENTORY_RUNTIME_CAPABILITY = "legacy-no-inventory-snapshot-v1"


@dataclass(frozen=True)
class RollbackDecision:
    allowed: bool
    code: str
    reason: str


def _deny(code: str, reason: str) -> RollbackDecision:
    return RollbackDecision(False, code, reason)


def evaluate_inventory_rollback(
    *,
    marker_count: int,
    writer_generation: int,
    unsettled_generation2_orders: int,
    active_image: str,
    target_image: str,
    active_capability: str,
    target_capability: str,
    phase_evidence_fresh: bool = False,
    phase_legacy_image: Optional[str] = None,
    phase_candidate_image: Optional[str] = None,
    phase_legacy_capability: Optional[str] = None,
    phase_candidate_capability: Optional[str] = None,
) -> RollbackDecision:
    """判定是否可以启动 rollback target 并切流。

    - legacy binary 只能在未激活、generation=1、零未结算 gen2 时使用；
    - 只要 fence=2、marker 已激活或存在未结算 gen2，目标必须是
      与 active 同 image 的快照 binary。
    """
    if marker_count not in (0, 1):
        return _deny("MARKER_UNPROVEN", "cutover marker 状态不是严格 0/1")
    if writer_generation not in (1, 2):
        return _deny("GENERATION_UNPROVEN", "writer generation 不是严格 1/2")
    if unsettled_generation2_orders < 0:
        return _deny("GEN2_COUNT_INVALID", "未结算 generation=2 订单数非法")
    if not active_image or not target_image:
        return _deny("IMAGE_UNPROVEN", "active/target image identity 缺失")
    if not active_capability or not target_capability:
        return _deny("CAPABILITY_UNPROVEN", "active/target capability 缺失")
    if marker_count == 1 and writer_generation != 2:
        return _deny("CUTOVER_GENERATION_MISMATCH", "marker 已存在但 writer fence 不是 generation=2")

    same_snapshot_image = (
        active_image == target_image
        and active_capability == INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY
        and target_capability == INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY
    )
    snapshot_required = (
        marker_count == 1
        or writer_generation == 2
        or unsettled_generation2_orders > 0
    )
    if snapshot_required:
        if not same_snapshot_image:
            return _deny(
                "SNAPSHOT_TARGET_REQUIRED",
                "marker/generation/gen2 订单要求保留新 binary 或切同 image 快照热备",
            )
        return RollbackDecision(True, "SAME_IMAGE_SNAPSHOT_ALLOWED", "同 image 快照 binary 可安全消费 gen2 订单")

    if target_capability == INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY:
        if not same_snapshot_image:
            return _deny("SNAPSHOT_IMAGE_MISMATCH", "快照 target 与 active image 不同")
        return RollbackDecision(True, "SAME_IMAGE_SNAPSHOT_ALLOWED", "同 image 快照热备可切换")

    if target_capability != LEGACY_INVENTORY_RUNTIME_CAPABILITY:
        return _deny("TARGET_CAPABILITY_UNKNOWN", "target 既非快照 binary 也非可证明 legacy binary")
    if active_capability != INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY:
        return _deny("ACTIVE_CAPABILITY_INVALID", "Phase A active 不是快照候选 binary")
    if not phase_evidence_fresh:
        return _deny("PHASE_EVIDENCE_STALE", "Phase A rollback 证据缺失或过期")

    evidence_matches = (
        phase_legacy_image == target_image
        and phase_candidate_image == active_image
        and phase_legacy_capability == target_capability
        and phase_candidate_capability == active_capability
    )
    if not evidence_matches:
        return _deny("PHASE_EVIDENCE_MISMATCH", "Phase A image/capability 证据与当前双槽位不一致")
    return RollbackDecision(True, "LEGACY_PHASE_A_ALLOWED", "零 gen2 订单且证据完整，允许 Phase A 回旧 binary")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate agent inventory rollback safety")
    parser.add_argument("--marker-count", type=int, required=True)
    parser.add_argument("--writer-generation", type=int, required=True)
    parser.add_argument("--unsettled-generation2-orders", type=int, required=True)
    parser.add_argument("--active-image", required=True)
    parser.add_argument("--target-image", required=True)
    parser.add_argument("--active-capability", required=True)
    parser.add_argument("--target-capability", required=True)
    parser.add_argument("--phase-evidence-fresh", type=int, choices=(0, 1), default=0)
    parser.add_argument("--phase-legacy-image", default="")
    parser.add_argument("--phase-candidate-image", default="")
    parser.add_argument("--phase-legacy-capability", default="")
    parser.add_argument("--phase-candidate-capability", default="")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    decision = evaluate_inventory_rollback(
        marker_count=args.marker_count,
        writer_generation=args.writer_generation,
        unsettled_generation2_orders=args.unsettled_generation2_orders,
        active_image=args.active_image,
        target_image=args.target_image,
        active_capability=args.active_capability,
        target_capability=args.target_capability,
        phase_evidence_fresh=bool(args.phase_evidence_fresh),
        phase_legacy_image=args.phase_legacy_image,
        phase_candidate_image=args.phase_candidate_image,
        phase_legacy_capability=args.phase_legacy_capability,
        phase_candidate_capability=args.phase_candidate_capability,
    )
    print(f"{decision.code}|{decision.reason}")
    return 0 if decision.allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
