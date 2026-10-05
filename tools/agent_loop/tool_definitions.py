"""Tool schemas for the Social Studio agent loop.

The objects in TOOL_SCHEMAS are product contracts, not implementation code.
Adapters in later steps must conform to these names and JSON schemas.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, TypedDict


RiskLevel = Literal["low", "medium", "high"]


class ToolSchema(TypedDict):
    name: str
    description: str
    parameters: dict[str, Any]
    requires_confirmation: bool
    risk_level: RiskLevel
    cost_estimate_points: int


CONCEPT_ENUM = [
    "business_identity",
    "target_customer",
    "offer_and_proof",
    "voice_style",
    "guardrails",
]

OUTPUT_MODE_ENUM = ["full", "outline", "analysis", "score", "list_competitors"]


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required or [],
    }


TOOL_SCHEMAS: list[ToolSchema] = [
    {
        "name": "tikhub_search_topics",
        "description": "Search public short-video samples or hot topics by platform.",
        "parameters": _obj(
            {
                "industry": {"type": "string", "minLength": 1},
                "platform": {
                    "type": "string",
                    "minLength": 1,
                    "description": "douyin/xiaohongshu/wechat_channels/tiktok/bilibili/douyin_hot",
                },
                "market": {"type": "string"},
                "language": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            ["industry", "platform"],
        ),
        "requires_confirmation": False,
        "risk_level": "medium",
        "cost_estimate_points": 6,
    },
    {
        "name": "tikhub_get_account",
        "description": "Fetch public creator account signals for style research.",
        "parameters": _obj(
            {
                "platform": {"type": "string", "minLength": 1},
                "account_id": {"type": "string"},
                "profile_url": {"type": "string"},
            },
            ["platform"],
        ),
        "requires_confirmation": False,
        "risk_level": "medium",
        "cost_estimate_points": 5,
    },
    {
        "name": "tikhub_parse_video",
        "description": "Parse a public video URL into transcript and engagement signals.",
        "parameters": _obj(
            {
                "url": {"type": "string", "minLength": 8},
                "need_asr": {"type": "boolean"},
                "language": {"type": "string"},
            },
            ["url"],
        ),
        "requires_confirmation": False,
        "risk_level": "medium",
        "cost_estimate_points": 8,
    },
    {
        "name": "metaso_web_search",
        "description": "Search the web for recent public facts with source URLs.",
        "parameters": _obj(
            {
                "query": {"type": "string", "minLength": 2},
                "market": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            ["query"],
        ),
        "requires_confirmation": False,
        "risk_level": "medium",
        "cost_estimate_points": 5,
    },
    {
        "name": "keyword_explore",
        "description": "Expand search keywords and user-intent clusters.",
        "parameters": _obj(
            {
                "seed": {"type": "string", "minLength": 1},
                "industry": {"type": "string"},
                "platform": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            ["seed"],
        ),
        "requires_confirmation": False,
        "risk_level": "low",
        "cost_estimate_points": 2,
    },
    {
        "name": "internal_memory_query",
        "description": "Read approved profile memories by concept and query.",
        "parameters": _obj(
            {
                "profile_id": {"type": "string", "minLength": 1},
                "concept": {"type": "string", "enum": CONCEPT_ENUM},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            ["profile_id", "concept"],
        ),
        "requires_confirmation": False,
        "risk_level": "low",
        "cost_estimate_points": 1,
    },
    {
        "name": "internal_profile_get",
        "description": "Read selected client profile and social-field data.",
        "parameters": _obj(
            {
                "profile_id": {"type": "string", "minLength": 1},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 30,
                },
            },
            ["profile_id", "fields"],
        ),
        "requires_confirmation": False,
        "risk_level": "low",
        "cost_estimate_points": 1,
    },
    {
        "name": "internal_history_search",
        "description": "Search this client's prior agent sessions and summaries.",
        "parameters": _obj(
            {
                "profile_id": {"type": "string", "minLength": 1},
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            ["profile_id", "query"],
        ),
        "requires_confirmation": False,
        "risk_level": "low",
        "cost_estimate_points": 1,
    },
    {
        "name": "web_visit",
        "description": "Visit a safe public URL after SSRF and HTML cleaning.",
        "parameters": _obj(
            {
                "url": {"type": "string", "minLength": 8},
                "purpose": {"type": "string", "minLength": 1},
                "max_chars": {"type": "integer", "minimum": 500, "maximum": 12000},
            },
            ["url", "purpose"],
        ),
        "requires_confirmation": False,
        "risk_level": "high",
        "cost_estimate_points": 4,
    },
    {
        "name": "time_now",
        "description": "Return current date and time for a timezone.",
        "parameters": _obj(
            {
                "timezone": {"type": "string", "default": "Asia/Shanghai"},
            }
        ),
        "requires_confirmation": False,
        "risk_level": "low",
        "cost_estimate_points": 0,
    },
    {
        "name": "industry_knowledge_query",
        "description": "Query internal industry knowledge and playbooks.",
        "parameters": _obj(
            {
                "industry": {"type": "string", "minLength": 1},
                "question": {"type": "string", "minLength": 1},
                "country": {"type": "string"},
                "language": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            ["industry", "question"],
        ),
        "requires_confirmation": False,
        "risk_level": "medium",
        "cost_estimate_points": 3,
    },
    {
        "name": "update_profile_taboo",
        "description": "Propose or save content taboos for this client.",
        "parameters": _obj(
            {
                "profile_id": {"type": "string", "minLength": 1},
                "taboo": {"type": "string", "minLength": 1},
                "source": {"type": "string", "enum": ["user_explicit", "ai_inferred"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            ["profile_id", "taboo", "source"],
        ),
        "requires_confirmation": False,
        "risk_level": "medium",
        "cost_estimate_points": 0,
    },
    {
        "name": "update_profile_memory",
        "description": "Propose a canonical client memory update.",
        "parameters": _obj(
            {
                "profile_id": {"type": "string", "minLength": 1},
                "concept": {"type": "string", "enum": CONCEPT_ENUM},
                "text": {"type": "string", "minLength": 1},
                "source": {"type": "string", "enum": ["user_explicit", "ai_inferred"]},
                "supersedes_event_id": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            ["profile_id", "concept", "text", "source"],
        ),
        "requires_confirmation": True,
        "risk_level": "medium",
        "cost_estimate_points": 0,
    },
    {
        "name": "confirm_memory_conflict",
        "description": "Ask user to resolve conflicting client memories.",
        "parameters": _obj(
            {
                "profile_id": {"type": "string", "minLength": 1},
                "concept": {"type": "string", "enum": CONCEPT_ENUM},
                "old_event_id": {"type": "string", "minLength": 1},
                "new_text": {"type": "string", "minLength": 1},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 4,
                },
            },
            ["profile_id", "concept", "old_event_id", "new_text", "options"],
        ),
        "requires_confirmation": True,
        "risk_level": "medium",
        "cost_estimate_points": 0,
    },
    {
        "name": "archive_memory_event",
        "description": "Archive a stale or rejected client memory event.",
        "parameters": _obj(
            {
                "profile_id": {"type": "string", "minLength": 1},
                "event_id": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
            },
            ["profile_id", "event_id", "reason"],
        ),
        "requires_confirmation": False,
        "risk_level": "medium",
        "cost_estimate_points": 0,
    },
    {
        "name": "update_profile_field",
        "description": "Update a concrete profile field after user confirmation.",
        "parameters": _obj(
            {
                "profile_id": {"type": "string", "minLength": 1},
                "field": {"type": "string", "minLength": 1},
                "value": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Scalar text or JSON-serialized value.",
                },
                "source": {"type": "string", "enum": ["user_explicit", "ai_inferred"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            ["profile_id", "field", "value", "source"],
        ),
        "requires_confirmation": True,
        "risk_level": "medium",
        "cost_estimate_points": 0,
    },
    {
        "name": "cost_estimate_and_confirm",
        "description": "Estimate agent cost and ask user to confirm if high.",
        "parameters": _obj(
            {
                "profile_id": {"type": "string"},
                "planned_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 20,
                },
                "estimated_points": {"type": "integer", "minimum": 0},
                "output_mode": {"type": "string", "enum": OUTPUT_MODE_ENUM},
                "reason": {"type": "string"},
            },
            ["planned_tools", "estimated_points", "output_mode"],
        ),
        "requires_confirmation": True,
        "risk_level": "low",
        "cost_estimate_points": 0,
    },
    {
        # P0-8 fix · SSOT §2.4b 明文要求的 archive_plan write tool
        "name": "archive_plan",
        "description": "Cancel or archive an active multi-step plan.",
        "parameters": _obj(
            {
                "plan_id": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
                "new_status": {
                    "type": "string",
                    "enum": ["cancelled", "completed", "superseded"],
                },
            },
            ["plan_id", "reason", "new_status"],
        ),
        "requires_confirmation": False,
        "risk_level": "low",
        "cost_estimate_points": 0,
    },
    {
        # P0-8 fix · SSOT §2.4b 明文要求的 update_plan write tool(用户中途改主意)
        "name": "update_plan",
        "description": "Update the steps or current step of an active plan.",
        "parameters": _obj(
            {
                "plan_id": {"type": "string", "minLength": 1},
                "new_state_json": {
                    "type": "string",
                    "description": "JSON-encoded updated plan state (steps / current_step / summary).",
                    "minLength": 2,
                },
                "reason": {"type": "string", "minLength": 1},
            },
            ["plan_id", "new_state_json", "reason"],
        ),
        "requires_confirmation": True,
        "risk_level": "medium",
        "cost_estimate_points": 0,
    },
]


def get_tool_schema(name: str) -> ToolSchema:
    for schema in TOOL_SCHEMAS:
        if schema["name"] == name:
            return deepcopy(schema)  # type: ignore[return-value]
    raise KeyError(f"Unknown agent-loop tool: {name}")


def openai_tools(names: list[str] | None = None) -> list[dict[str, Any]]:
    selected = TOOL_SCHEMAS if names is None else [get_tool_schema(name) for name in names]
    return [
        {
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema["description"],
                "parameters": schema["parameters"],
            },
        }
        for schema in selected
    ]


def validate_tool_schema(schema: ToolSchema) -> None:
    required_top = {
        "name",
        "description",
        "parameters",
        "requires_confirmation",
        "risk_level",
        "cost_estimate_points",
    }
    missing = required_top - set(schema)
    if missing:
        raise ValueError(f"{schema.get('name', '<unknown>')} missing {sorted(missing)}")
    if len(schema["description"]) > 80:
        raise ValueError(f"{schema['name']} description too long")
    if schema["risk_level"] not in ("low", "medium", "high"):
        raise ValueError(f"{schema['name']} invalid risk_level")
    if not isinstance(schema["requires_confirmation"], bool):
        raise ValueError(f"{schema['name']} requires_confirmation must be bool")
    if not isinstance(schema["cost_estimate_points"], int) or schema["cost_estimate_points"] < 0:
        raise ValueError(f"{schema['name']} invalid cost_estimate_points")
    params = schema["parameters"]
    if params.get("type") != "object":
        raise ValueError(f"{schema['name']} parameters must be object")
    if "properties" not in params or "required" not in params:
        raise ValueError(f"{schema['name']} parameters need properties and required")


def validate_all_tool_schemas() -> None:
    names = [schema["name"] for schema in TOOL_SCHEMAS]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate tool names")
    # v1.3 wire-up · 18 → 20(加 archive_plan + update_plan · P0-8 fix)
    # E3 第 0 片 · 20 → 19(口播局部重写工具随社媒删除)
    if len(TOOL_SCHEMAS) != 19:
        raise ValueError(f"Expected 19 tool schemas, got {len(TOOL_SCHEMAS)}")
    for schema in TOOL_SCHEMAS:
        validate_tool_schema(schema)
    openai_payload = openai_tools()
    if len(openai_payload) != 19:
        raise ValueError("OpenAI tool payload length mismatch")


validate_all_tool_schemas()
