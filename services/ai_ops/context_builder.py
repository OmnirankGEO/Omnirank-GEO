"""
AI Ops 上下文构造 · 2026-07-01

把任务 + 反馈拼成给 Codex 的 ops_context.md。所有外来文本(反馈描述/小榜回答/指令)
过 redaction.redact() 兜底脱敏。当前生产锚点只做背景,一律标注"需生产复验",
不把本地/手册当已上线真值。

页面路径只能 best-effort:faq_feedback 当前无 page_path 列,精确追踪需改前端提交 payload + schema。
截图:context 里只放 OSS key + 提示;真要把图交给 Codex,由 Worker 先把签名 URL 下载成
本地文件再 `codex exec --image <local_file>`(不把签名 URL 直接塞进 context)。
"""

from typing import Optional

from services.ai_ops import redaction

_PROJECT_RULES = """## Project Rules
- 先读 docs/AI-CONTEXT/GEO_PROJECT_FULL_MANUAL/CURRENT_GEO_PROJECT_FULL_MANUAL_2026-06-30.md 拿产品边界。
- 生产真值以 SSH 实测 active commit / 前端 route / 运行 flag 为准;本地 HEAD ≠ 生产 HEAD。
- 不执行生产 SSH。不写生产 DB。
- 不碰钱包/退款/结算/提现/佣金相关代码,除非任务明确要求且已有审批。
- 红线文件不改:middleware/billing.py / db/connection.py / auth/middleware.py / auth/jwt_utils.py。
"""

_EXPECTED_OUTPUT = """## Expected Output
- Root cause(根因)
- Files inspected(看过哪些文件)
- Proposed fix(修复方案)
- Patch or no-patch reason(给 diff 或说明为何不改)
- Tests run(跑了哪些测试)
- Residual risk(残留风险)
"""


def build_ops_context(task: dict, feedback: Optional[dict] = None) -> str:
    """生成 ops_context.md 文本(已脱敏)。"""
    kind = task.get("kind", "")
    lines = ["# AI Ops Context", "", "## Task"]
    lines += [
        f"- task_id: {task.get('id', '')}",
        f"- kind: {kind}",
        f"- risk_level: {task.get('risk_level', '')}",
        f"- priority: {task.get('priority', '')}",
        f"- instruction: {redaction.redact(task.get('instruction', '') or '')}",
        "",
    ]

    # 反馈上下文优先从任务快照(source_context.feedback)取,Runner 不回读 faq_feedback 业务表。
    if feedback is None:
        _ctx = task.get("source_context_jsonb") or {}
        if isinstance(_ctx, dict) and isinstance(_ctx.get("feedback"), dict):
            feedback = _ctx["feedback"]

    if feedback:
        lines += [
            "## Feedback",
            f"- feedback_id: {feedback.get('id', '')}",
            f"- urgency: {feedback.get('urgency', '')}",
            f"- submitter_identity: {feedback.get('submitter_identity', '') or ''}",
            f"- page_path: {feedback.get('page_path') or '(best-effort · faq_feedback 无 page_path 列 · 未知)'}",
            f"- screenshot_key: {feedback.get('screenshot_key') or feedback.get('screenshot_url') or '(无)'}",
            "  (若需看图:Worker 会把签名 URL 下载成本地文件后 --image 传入,不在此暴露 URL)",
            "",
            "### 用户描述",
            redaction.redact((feedback.get("message", "") or "").strip()) or "(空)",
            "",
        ]
        ai_answer = (feedback.get("ai_answer", "") or "").strip()
        if ai_answer:
            lines += ["### 小榜先答(仅参考)", redaction.redact(ai_answer), ""]

    # 源事件补充上下文(排除已单独渲染的 feedback 快照,避免重复)
    ctx = task.get("source_context_jsonb") or {}
    if isinstance(ctx, dict):
        rest = {k: v for k, v in ctx.items() if k not in ("feedback",)}
        if rest:
            cleaned = redaction.redact_dict(rest)
            lines += ["## Source Context (non-sensitive)"]
            for k, v in cleaned.items():
                lines.append(f"- {k}: {v}")
            lines.append("")

    lines += [
        "## Current Production Anchor (需生产复验 · 勿当已上线真值)",
        "- active 容器/HEAD/flag 以最新 SSH 实测为准;排障前先查当前 active HEAD。",
        "",
        _PROJECT_RULES,
        "",
        _EXPECTED_OUTPUT,
    ]
    text = "\n".join(lines)
    # 整体再兜底脱敏一次(防模板拼接引入的任何残留)
    return redaction.redact(text)
