from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_agent_channel_tier_me_endpoint_is_readonly_and_agent_only():
    src = read("api/agent_workbench_api.py")
    assert '@router.get("/channel-tier/me")' in src
    block = src.split('@router.get("/channel-tier/me")', 1)[1].split("@router.", 1)[0]
    assert "_require_agent" in block
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "evaluate_and_apply_tier", "complete_recharge")
    for token in forbidden:
        assert token not in block


def test_channel_tier_service_exposes_readonly_state_helper():
    src = read("services/channel_tier.py")
    assert "def get_agent_channel_tier_state" in src
    block = src.split("def get_agent_channel_tier_state", 1)[1].split("\ndef ", 1)[0]
    assert "agent_channel_tier_state" in block
    assert "SELECT" in block
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "evaluate_and_apply_tier")
    for token in forbidden:
        assert token not in block


def test_admin_business_profile_includes_channel_tier_block():
    src = read("services/admin_business_profile.py")
    assert "def _build_channel_tier" in src
    assert '"channel_tier": channel_tier' in src


def test_frontend_uses_shared_channel_tier_terminology_and_badge():
    terminology = ROOT / "frontend/src/lib/channelTierTerminology.ts"
    badge = ROOT / "frontend/src/components/agent/ChannelTierBadge.tsx"
    assert terminology.exists()
    assert badge.exists()
    t = terminology.read_text(encoding="utf-8")
    assert "CHANNEL_TIER_LABELS" in t
    assert "AGENT_LEVEL_LABELS" in t
    wrapper = read("frontend/src/pages/Admin/ChannelTierAdmin.tsx")
    panel = read("frontend/src/pages/Admin/ChannelTierPanel.tsx")
    assert "<ChannelTierPanel />" in wrapper
    assert "channelTierTerminology" in panel


def test_channel_tier_badge_is_agent_internal_only():
    frontend = ROOT / "frontend/src"
    forbidden_roots = (
        frontend / "pages" / "Portal",
        frontend / "pages" / "CEnd",
        frontend / "components" / "c_end",
    )
    offenders = []
    for root in forbidden_roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix not in {".ts", ".tsx"}:
                continue
            if "ChannelTierBadge" in path.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_admin_audit_snapshots_are_humanized_not_raw_json_only():
    helper = ROOT / "frontend/src/lib/auditTranslate.ts"
    assert helper.exists()
    h = helper.read_text(encoding="utf-8")
    assert "humanizeAuditSnapshot" in h
    assert "formatRawAuditSnapshot" in h
    user_mgmt = read("frontend/src/pages/Admin/UserManagement.tsx")
    audit_logs = read("frontend/src/pages/Admin/AuditLogs.tsx")
    assert "humanizeAuditSnapshot" in user_mgmt
    assert "humanizeAuditSnapshot" in audit_logs
