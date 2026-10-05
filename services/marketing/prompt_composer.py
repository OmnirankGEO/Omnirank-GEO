"""Versioned visual prompt composer for GEO acquisition images.

The composer is the single seam that assembles every image-generation brief.
Its version is frozen into each job's ``geo_snapshot`` (alongside
``contract_version`` and the strategy-teacher snapshot) so a historic job can
always be traced to the exact prompt template that produced it.

A :class:`VisualBrief` carries the eight director elements:
``task_type`` / ``subject`` / ``visual_style`` / ``composition_and_lighting`` /
``exact_visible_text`` / ``platform_ratio`` / ``brand_assets`` /
``privacy_and_truth_constraints``.  :meth:`VisualPromptComposer.compose` is a
pure deterministic template assembly: the same brief always yields the same
prompt string, with no model calls and no hidden state.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Tuple

PROMPT_COMPOSER_VERSION = "geo-vpc/2.0"

_ROLE = "视觉总监 / Prompt Composer"
# gpt-image-2 renders Chinese copy directly inside the image; the program must
# never composite text onto the picture afterwards (程序叠字 is forbidden).
_MODEL_INSTRUCTION = "由 gpt-image-2 直接完成中文文字、视觉与整体排版；禁止程序叠字感。"


@dataclass(frozen=True)
class VisualBrief:
    """Immutable eight-element brief consumed by :class:`VisualPromptComposer`."""

    task_type: str
    subject: dict
    visual_style: str
    composition_and_lighting: str
    exact_visible_text: dict
    platform_ratio: str
    brand_assets: dict
    privacy_and_truth_constraints: Tuple[str, ...] = field(default_factory=tuple)


class VisualPromptComposer:
    """Deterministic template assembly of a VisualBrief into a provider prompt."""

    version = PROMPT_COMPOSER_VERSION

    def compose(self, brief: VisualBrief) -> str:
        document = {
            "role": _ROLE,
            "composer_version": self.version,
            "model_instruction": _MODEL_INSTRUCTION,
            "task_type": str(brief.task_type),
            "subject": dict(brief.subject or {}),
            "visual_style": str(brief.visual_style or ""),
            "composition_and_lighting": str(brief.composition_and_lighting or ""),
            "exact_visible_text": dict(brief.exact_visible_text or {}),
            "platform_ratio": str(brief.platform_ratio or ""),
            "brand_assets": dict(brief.brand_assets or {}),
            "privacy_and_truth_constraints": [str(item) for item in brief.privacy_and_truth_constraints],
        }
        return "Create one finished marketing image from this immutable JSON brief:\n" + json.dumps(
            document, ensure_ascii=False, sort_keys=True
        )
