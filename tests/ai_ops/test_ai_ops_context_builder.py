"""services/ai_ops/context_builder.py 测试(纯函数,无 DB)。"""
from services.ai_ops import context_builder


def test_context_includes_task_and_rules():
    task = {"id": 12, "kind": "diagnose", "risk_level": "L0", "priority": "P1",
            "instruction": "查一下报价页 500"}
    md = context_builder.build_ops_context(task, None)
    assert "task_id: 12" in md
    assert "kind: diagnose" in md
    assert "Project Rules" in md
    assert "不执行生产 SSH" in md
    assert "Expected Output" in md


def test_context_includes_feedback_and_redacts_secrets():
    task = {"id": 1, "kind": "diagnose", "risk_level": "L0", "priority": "P2", "instruction": ""}
    feedback = {
        "id": 99, "urgency": "high", "submitter_identity": "agent",
        "message": "报错里带了 DATABASE_URL=postgresql://u:leakpw@h/db",
        "ai_answer": "",
    }
    md = context_builder.build_ops_context(task, feedback)
    assert "feedback_id: 99" in md
    assert "leakpw" not in md          # 脱敏生效
    assert "报错里带了" in md


def test_context_page_path_best_effort():
    task = {"id": 2, "kind": "diagnose", "risk_level": "L0", "priority": "P3", "instruction": ""}
    feedback = {"id": 5, "urgency": "low", "message": "x", "ai_answer": ""}
    md = context_builder.build_ops_context(task, feedback)
    assert "page_path:" in md
    assert "best-effort" in md         # 明示 page_path 只是 best-effort


def test_context_from_source_context_snapshot():
    # feedback 不作为参数传,而是从 task.source_context.feedback 快照取(Runner 路径 · 不读 DB)
    task = {
        "id": 7, "kind": "diagnose", "risk_level": "L0", "priority": "P1", "instruction": "",
        "source_context_jsonb": {
            "feedback": {
                "id": 42, "urgency": "high", "message": "监测页崩了",
                "ai_answer": "", "screenshot_key": "feedback/10/x.png",
            }
        },
    }
    md = context_builder.build_ops_context(task)   # 不传 feedback 参数
    assert "feedback_id: 42" in md
    assert "监测页崩了" in md
    assert "feedback/10/x.png" in md


def test_context_marks_prod_anchor_needs_recheck():
    task = {"id": 3, "kind": "fix", "risk_level": "L1", "priority": "P2", "instruction": "修"}
    md = context_builder.build_ops_context(task, None)
    assert "需生产复验" in md          # 不把手册/本地当已上线真值
